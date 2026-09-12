#!/usr/bin/env python3
"""Tests for ops/arena/sync_server.py, the sandbox<->local git pairer.

This server is a write-capable endpoint, so the guards are the point:

* it refuses to serve anything at all with no ``SYNC_TOKEN``;
* unauthenticated or wrongly-authenticated requests get ``401``;
* only the one configured repository name is reachable, and ``..``/NUL in the
  path never reach ``git-http-backend``;
* and a real ``git clone`` + ``git push`` round trip still works.

The guard tests drive the WSGI callable directly (no socket). The round trip
starts the server as a subprocess on an ephemeral port with a throwaway seed
repository, so nothing here touches the real checkout or the network.

Run:  python3 tests/test_arena_sync_server.py
"""

import base64
import contextlib
import io
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
SERVER = os.path.join(ROOT, 'ops', 'arena', 'sync_server.py')


def git(cwd, *args):
    return subprocess.run(
        ['git', '-C', cwd, *args], check=True, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT).stdout.decode().strip()


def load_server(env):
    """Import sync_server with its module-level config taken from `env`."""
    import importlib.util
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        spec = importlib.util.spec_from_file_location('hdc_sync_server', SERVER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def call(application, path, method='GET', auth=None, query=''):
    """Invoke the WSGI app directly and return (status, body)."""
    environ = {
        'REQUEST_METHOD': method,
        'PATH_INFO': path,
        'QUERY_STRING': query,
        'SERVER_NAME': 'test',
        'SERVER_PORT': '80',
        'SERVER_PROTOCOL': 'HTTP/1.1',
        'HTTP_HOST': 'test',
        'wsgi.input': io.BytesIO(b''),
        'CONTENT_LENGTH': '0',
    }
    if auth is not None:
        raw = base64.b64encode(f'{auth[0]}:{auth[1]}'.encode()).decode()
        environ['HTTP_AUTHORIZATION'] = f'Basic {raw}'
    captured = {}

    def start_response(status, headers):
        captured['status'] = status
        captured['headers'] = dict(headers)

    body = b''.join(application(environ, start_response))
    return captured['status'], body


@unittest.skipUnless(shutil.which('git'), 'needs git')
class SyncServerGuardTest(unittest.TestCase):
    """Path and authentication gating, exercised without a socket."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = os.path.join(self.tmp.name, 'hdc.git')
        self.module = load_server({
            'SYNC_TOKEN': 's3cret-token',
            'SYNC_REPO': self.repo,
            'SYNC_SEED_FROM': '',
            'SYNC_PORT': '0',
        })

    def test_no_token_means_no_service(self):
        module = load_server({'SYNC_TOKEN': '', 'SYNC_REPO': self.repo, 'SYNC_PORT': '0'})
        status, body = call(module.application, '/hdc.git/info/refs')
        self.assertEqual(status, '500 Internal Server Error')
        self.assertIn(b'SYNC_TOKEN', body)
        # and main() refuses to start rather than serving an open repo
        self.assertEqual(module.main(), 2)

    def test_unauthenticated_request_is_rejected(self):
        status, _ = call(self.module.application, '/hdc.git/info/refs')
        self.assertEqual(status, '401 Unauthorized')

    def test_wrong_token_is_rejected(self):
        status, _ = call(self.module.application, '/hdc.git/info/refs',
                         auth=('arena', 'wrong-token'))
        self.assertEqual(status, '401 Unauthorized')

    def test_wrong_username_is_rejected(self):
        status, _ = call(self.module.application, '/hdc.git/info/refs',
                         auth=('someone-else', 's3cret-token'))
        self.assertEqual(status, '401 Unauthorized')

    def test_only_the_configured_repo_name_is_served(self):
        for path in ('/etc/passwd', '/other.git/info/refs', '/hdc.git/../../etc/passwd',
                     '/hdc.git/info/\x00refs', '/hdcX.git/info/refs'):
            status, body = call(self.module.application, path,
                                auth=('arena', 's3cret-token'))
            self.assertTrue(status.startswith('404'), f'{path} -> {status} {body[:80]!r}')
            self.assertNotIn(b'root:', body)

    def test_health_path_needs_no_repo(self):
        status, body = call(self.module.application, '/')
        self.assertEqual(status, '200 OK')
        self.assertIn(b'hdc sync server ok', body)

    def test_seed_repo_stays_bare_and_accepts_http_pushes(self):
        with contextlib.redirect_stdout(io.StringIO()):   # keep the log readable
            self.module.seed_repo()
        self.assertTrue(os.path.exists(os.path.join(self.repo, 'HEAD')))
        for key in ('http.receivepack', 'receive.denyCurrentBranch'):
            value = git(self.repo, 'config', '--get', key)
            self.assertIn(value, ('true', 'ignore'))
        # a bare repo has no worktree to clobber
        self.assertEqual(git(self.repo, 'config', '--get', 'core.bare'), 'true')


@unittest.skipUnless(shutil.which('git') and shutil.which('bash'), 'needs git and bash')
class SyncServerRoundTripTest(unittest.TestCase):
    """A real clone/push through the running server, on 127.0.0.1 only."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = self.tmp.name
        self.token = 'roundtrip-token'

        seed = os.path.join(base, 'seed')
        os.makedirs(seed)
        git(base, 'init', '-q', seed)
        git(seed, 'symbolic-ref', 'HEAD', 'refs/heads/main')
        git(seed, 'config', 'user.email', 'test@example.com')
        git(seed, 'config', 'user.name', 'Test')
        with open(os.path.join(seed, 'README.md'), 'w', encoding='utf-8') as h:
            h.write('# seed\n')
        git(seed, 'add', '-A')
        git(seed, 'commit', '-q', '-m', 'seed commit')

        self.repo = os.path.join(base, 'arena-sync', 'hdc.git')
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            self.port = probe.getsockname()[1]

        env = {**os.environ, 'SYNC_TOKEN': self.token, 'SYNC_REPO': self.repo,
               'SYNC_SEED_FROM': seed, 'SYNC_PORT': str(self.port)}
        self.server = subprocess.Popen(
            [sys.executable, SERVER], env=env, cwd=ROOT,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(self._stop)

        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                        f'http://127.0.0.1:{self.port}/', timeout=1) as response:
                    if response.status == 200:
                        return
            except Exception:
                if self.server.poll() is not None:
                    self.fail(f'server exited: {self.server.stdout.read()}')
                time.sleep(0.2)
        self.fail('sync server did not come up within 20s')

    def _stop(self):
        if self.server.poll() is None:
            self.server.terminate()
            try:
                self.server.wait(timeout=10)
            except subprocess.TimeoutExpired:      # pragma: no cover
                self.server.kill()
                self.server.wait(timeout=10)

    def url(self, path='/hdc.git'):
        return f'http://arena:{self.token}@127.0.0.1:{self.port}{path}'

    def test_clone_then_push_back(self):
        laptop = os.path.join(self.tmp.name, 'laptop')
        subprocess.run(['git', 'clone', '-q', self.url(), laptop], check=True,
                       cwd=self.tmp.name)
        self.assertEqual(git(laptop, 'log', '-1', '--format=%s'), 'seed commit')
        self.assertEqual(git(laptop, 'rev-parse', '--abbrev-ref', 'HEAD'), 'main')
        git(laptop, 'config', 'user.email', 'lap@top')
        git(laptop, 'config', 'user.name', 'Laptop')
        with open(os.path.join(laptop, 'FROM_LAPTOP.md'), 'w', encoding='utf-8') as h:
            h.write('pushed from the laptop side\n')
        git(laptop, 'add', '-A')
        git(laptop, 'commit', '-q', '-m', 'from laptop')
        git(laptop, 'push', '-q', 'origin', 'HEAD:refs/heads/main')

        # the seeded bare repo -- what the sandbox side reads -- now has it
        self.assertEqual(git(laptop, 'rev-parse', 'HEAD'),
                         git(self.repo, 'rev-parse', 'refs/heads/main'))
        git(self.tmp.name, 'init', '-q', 'agent-side')
        agent = os.path.join(self.tmp.name, 'agent-side')
        git(agent, 'remote', 'add', 'origin', self.url())
        subprocess.run(['git', '-C', agent, 'fetch', '-q', 'origin', 'main'], check=True)
        self.assertIn('from laptop', git(agent, 'log', '-1', '--format=%s', 'FETCH_HEAD'))

    def test_clone_without_a_token_fails(self):
        target = os.path.join(self.tmp.name, 'nope')
        result = subprocess.run(
            ['git', 'clone', f'http://127.0.0.1:{self.port}/hdc.git', target],
            cwd=self.tmp.name, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'})
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertTrue('could not read Username' in result.stdout
                        or 'Authentication failed' in result.stdout, result.stdout)
        self.assertFalse(os.path.exists(os.path.join(target, '.git')),
                         'an anonymous clone must not succeed')


if __name__ == '__main__':
    unittest.main(verbosity=2)
