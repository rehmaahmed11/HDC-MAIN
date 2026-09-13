#!/usr/bin/env python3
"""Tests for the minimal auto-deploy webhook (deploy_hook.py).

The hook is stdlib-only: GitHub pushes to main -> ``git pull`` -> touch the
WSGI file to reload.  Two layers are pinned here:

* unit tests with mocked git, covering the gating (bad signature rejected,
  non-main refs ignored, missing secret reported) and the failure hints;
* one integration test against a *real* local git clone, so the actual
  ``git pull --ff-only`` path and the auto-detected REPO_DIR are exercised
  end to end without touching the network.

Detection matters because nothing in deploy_hook.py is meant to be edited by
hand: the repo folder comes from where the file lives, and the WSGI path from
the username of the account running it.

Run:  python3 tests/test_deploy_hook.py
"""

import hashlib
import hmac
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import deploy_hook  # noqa: E402

SECRET = 'test-hook-secret'
GIT = shutil.which('git')


def sign(body, secret=SECRET):
    return 'sha256=' + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def make_environ(method='GET', body=b'', signature=None, path='/deploy'):
    env = {
        'REQUEST_METHOD': method,
        'PATH_INFO': path,
        'CONTENT_LENGTH': str(len(body)),
        'wsgi.input': io.BytesIO(body),
    }
    if signature is not None:
        env['HTTP_X_HUB_SIGNATURE_256'] = signature
    return env


def push_payload(branch='main'):
    return json.dumps({'ref': f'refs/heads/{branch}', 'after': 'c' * 40}).encode()


class CallMixin:
    def call(self, environ):
        def start_response(status, _headers):
            self.status = status

        return b''.join(deploy_hook.application(environ, start_response))

    def read_log(self):
        with open(deploy_hook.LOG_FILE) as handle:
            return handle.read()


class HookTestCase(CallMixin, unittest.TestCase):
    """Unit tests: REPO_DIR is a fake checkout, git is mocked."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.mkdir(os.path.join(self.tmp.name, '.git'))  # so it reads as a checkout
        self.wsgi = os.path.join(self.tmp.name, 'app_wsgi.py')
        with open(self.wsgi, 'w') as handle:
            handle.write('# placeholder wsgi\n')
        secret_file = os.path.join(self.tmp.name, 'deploy_secret.txt')
        with open(secret_file, 'w') as handle:
            handle.write(SECRET + '\n')
        self.patchers = [
            mock.patch.object(deploy_hook, 'REPO_DIR', self.tmp.name),
            mock.patch.object(deploy_hook, 'WSGI_FILE', self.wsgi),
            mock.patch.object(deploy_hook, 'SECRET_FILE', secret_file),
            mock.patch.object(deploy_hook, 'LOG_FILE',
                              os.path.join(self.tmp.name, 'deploy.log')),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.status = None

    # --- status page ---------------------------------------------------
    def test_get_before_any_deploy(self):
        body = self.call(make_environ('GET'))
        self.assertEqual(self.status, '200 OK')
        self.assertIn(b'no deploys yet', body)

    def test_get_reports_the_detected_configuration(self):
        body = self.call(make_environ('GET')).decode()
        self.assertIn(self.tmp.name, body)          # repo dir
        self.assertIn(self.wsgi, body)              # wsgi file
        self.assertIn('deploy_secret.txt present', body)
        self.assertIn('git       : present but unusable', body)  # fake .git dir

    def test_health_and_status_aliases_also_answer(self):
        for path in ('/deploy/health', '/deploy/status'):
            body = self.call(make_environ('GET', path=path))
            self.assertEqual(self.status, '200 OK')
            self.assertIn(b'HDC deploy hook', body)

    def test_get_shows_last_lines_of_log(self):
        with open(deploy_hook.LOG_FILE, 'w') as handle:
            for i in range(10):
                handle.write(f'line {i}\n')
        body = self.call(make_environ('GET'))
        self.assertEqual(self.status, '200 OK')
        self.assertNotIn(b'line 0', body)
        self.assertIn(b'line 9', body)

    # --- gating --------------------------------------------------------
    def test_post_with_bad_signature_is_rejected(self):
        payload = push_payload()
        body = self.call(make_environ('POST', payload, signature='sha256=wrong'))
        self.assertEqual(self.status, '401 Unauthorized')
        self.assertIn(b'bad signature', body)
        self.assertIn('REJECTED', self.read_log())

    def test_post_without_signature_is_rejected(self):
        payload = push_payload()
        body = self.call(make_environ('POST', payload))
        self.assertEqual(self.status, '401 Unauthorized')
        self.assertIn(b'bad signature', body)

    def test_post_to_other_branch_is_ignored(self):
        payload = push_payload('feature')
        body = self.call(make_environ('POST', payload, sign(payload)))
        self.assertEqual(self.status, '200 OK')
        self.assertIn(b'ignored', body)

    # --- secret handling ----------------------------------------------
    def test_missing_secret_file_answers_503_with_the_fix(self):
        os.remove(deploy_hook.SECRET_FILE)
        payload = push_payload()
        body = self.call(make_environ('POST', payload, sign(payload)))
        self.assertEqual(self.status, '503 Service Unavailable')
        self.assertIn(b'install_deploy_hook.py', body)
        self.assertIn('deploy_secret.txt missing', self.read_log())

    # --- happy path ----------------------------------------------------
    def test_post_to_main_pulls_and_reloads(self):
        payload = push_payload()
        with mock.patch.object(deploy_hook.subprocess, 'check_output') as check_output, \
                mock.patch.object(deploy_hook.os, 'utime') as utime:
            check_output.side_effect = [
                b'',                                   # fetch
                b'Updating abc..def\nFast-forward\n',   # pull
                b'abc1234\n',                           # rev-parse
            ]
            body = self.call(make_environ('POST', payload, sign(payload)))
        self.assertEqual(self.status, '200 OK')
        self.assertEqual(body, b'deployed abc1234')
        pull_call = check_output.call_args_list[1]
        self.assertEqual(pull_call.args[0][:4], ['git', 'pull', '--ff-only', 'origin'])
        self.assertEqual(pull_call.kwargs.get('cwd'), self.tmp.name)
        utime.assert_called_once_with(deploy_hook.WSGI_FILE, None)
        self.assertIn('OK deployed abc1234', self.read_log())

    def test_deploy_touching_the_wsgi_file_is_what_reloads_the_site(self):
        payload = push_payload()
        before = os.stat(self.wsgi).st_mtime
        os.utime(self.wsgi, (before - 5000, before - 5000))
        with mock.patch.object(deploy_hook.subprocess, 'check_output') as check_output:
            check_output.side_effect = [b'', b'Fast-forward\n', b'deadbee\n']
            self.call(make_environ('POST', payload, sign(payload)))
        self.assertEqual(self.status, '200 OK')
        self.assertGreater(os.stat(self.wsgi).st_mtime, before - 5000)

    def test_missing_wsgi_file_still_deploys_but_says_it_did_not_reload(self):
        os.remove(self.wsgi)
        payload = push_payload()
        with mock.patch.object(deploy_hook.subprocess, 'check_output') as check_output:
            check_output.side_effect = [b'', b'Fast-forward\n', b'deadbee\n']
            body = self.call(make_environ('POST', payload, sign(payload)))
        self.assertEqual(self.status, '200 OK')
        self.assertIn(b'NOT reloaded', body)
        self.assertIn('WARN', self.read_log())

    # --- failure hints -------------------------------------------------
    def _fail_with(self, output):
        payload = push_payload()
        error = subprocess.CalledProcessError(1, ['git', 'pull'], output=output)
        with mock.patch.object(deploy_hook.subprocess, 'check_output') as check_output:
            check_output.side_effect = error
            return self.call(make_environ('POST', payload, sign(payload)))

    def test_failed_pull_returns_500_and_logs(self):
        body = self._fail_with(b'CONFLICT boom')
        self.assertEqual(self.status, '500 Internal Server Error')
        self.assertIn(b'deploy failed', body)
        self.assertIn('FAILED', self.read_log())

    def test_dirty_tree_failure_names_the_stash_fix(self):
        self._fail_with(b'error: Your local changes to the following files '
                        b'would be overwritten by merge')
        log = self.read_log()
        self.assertIn('FAILED', log)
        self.assertIn('git stash', log)

    def test_diverged_history_failure_names_the_reset_fix(self):
        self._fail_with(b'fatal: Not possible to fast-forward, aborting.')
        self.assertIn('reset --hard', self.read_log())

    def test_not_a_git_checkout_is_reported_clearly(self):
        shutil.rmtree(os.path.join(self.tmp.name, '.git'))
        payload = push_payload()
        body = self.call(make_environ('POST', payload, sign(payload)))
        self.assertEqual(self.status, '500 Internal Server Error')
        self.assertIn(b'not a git checkout', body)


class DetectionTestCase(unittest.TestCase):
    """Nothing in deploy_hook.py should need hand-editing."""

    VARS = ('HDC_WSGI_FILE', 'HDC_REPO_DIR', 'HDC_PA_USERNAME', 'USER', 'LOGNAME')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.var_www = os.path.join(self.tmp.name, 'var-www')
        os.makedirs(self.var_www)
        saved = {var: os.environ.get(var) for var in self.VARS}
        for var in self.VARS:
            os.environ.pop(var, None)

        def restore():
            for var, value in saved.items():
                if value is None:
                    os.environ.pop(var, None)
                else:
                    os.environ[var] = value

        self.addCleanup(restore)

    def write_wsgi(self, *names):
        for name in names:
            with open(os.path.join(self.var_www, name), 'w') as handle:
                handle.write('# wsgi\n')

    def test_repo_dir_is_the_folder_holding_the_file(self):
        self.assertEqual(
            deploy_hook.detect_repo_dir(),
            os.path.dirname(os.path.abspath(deploy_hook.__file__)),
        )

    def test_repo_dir_env_var_wins(self):
        with mock.patch.dict(os.environ, {'HDC_REPO_DIR': '/srv/hdc'}):
            self.assertEqual(deploy_hook.detect_repo_dir(), '/srv/hdc')

    def test_wsgi_file_follows_the_username(self):
        self.write_wsgi('rehmanahmed92yd_pythonanywhere_com_wsgi.py')
        with mock.patch.dict(os.environ, {'USER': 'rehmanahmed92yd'}):
            self.assertEqual(
                deploy_hook.detect_wsgi_file(self.var_www),
                os.path.join(self.var_www, 'rehmanahmed92yd_pythonanywhere_com_wsgi.py'),
            )

    def test_single_wsgi_file_is_used_even_with_an_unusual_name(self):
        self.write_wsgi('www_rehmanahmed92yd_pythonanywhere_com_wsgi.py')
        with mock.patch.dict(os.environ, {'USER': 'someone-else'}):
            self.assertEqual(
                os.path.basename(deploy_hook.detect_wsgi_file(self.var_www)),
                'www_rehmanahmed92yd_pythonanywhere_com_wsgi.py',
            )

    def test_username_is_read_out_of_a_wsgi_filename(self):
        self.assertEqual(
            deploy_hook.username_from_wsgi_name('/var/www/bob_pythonanywhere_com_wsgi.py'),
            'bob',
        )

    def test_missing_var_www_falls_back_to_the_conventional_path(self):
        empty = os.path.join(self.tmp.name, 'nope')  # /var/www not readable/absent
        with mock.patch.dict(os.environ, {'USER': 'bob'}):
            self.assertEqual(
                deploy_hook.detect_wsgi_file(empty),
                os.path.join(empty, 'bob_pythonanywhere_com_wsgi.py'),
            )


@unittest.skipUnless(GIT, 'git is not installed')
class RealGitDeployTestCase(CallMixin, unittest.TestCase):
    """End-to-end against a real local clone (no network, no GitHub)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = self.tmp.name
        self.origin = os.path.join(root, 'origin.git')
        self.work = os.path.join(root, 'work')
        self.server = os.path.join(root, 'server')
        self.wsgi = os.path.join(root, 'wsgi.py')
        with open(self.wsgi, 'w') as handle:
            handle.write('# placeholder\n')

        self.git(['init', '--bare', '-b', 'main', self.origin])
        self.git(['clone', self.origin, self.work])
        self.commit_in(self.work, 'app.py', 'print("v1")\n', 'v1')
        self.git(['push', 'origin', 'main'], cwd=self.work)
        self.git(['clone', self.origin, self.server])

        secret_file = os.path.join(root, 'deploy_secret.txt')
        with open(secret_file, 'w') as handle:
            handle.write(SECRET + '\n')
        self.patchers = [
            mock.patch.object(deploy_hook, 'REPO_DIR', self.server),
            mock.patch.object(deploy_hook, 'WSGI_FILE', self.wsgi),
            mock.patch.object(deploy_hook, 'SECRET_FILE', secret_file),
            mock.patch.object(deploy_hook, 'LOG_FILE', os.path.join(root, 'deploy.log')),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.status = None

    def git(self, args, cwd=None):
        return subprocess.check_output(
            [GIT, '-c', 'user.name=HDC Test', '-c', 'user.email=test@example.com',
             '-c', 'commit.gpgsign=false'] + args,
            cwd=cwd, stderr=subprocess.STDOUT,
        ).decode(errors='replace')

    def commit_in(self, repo, name, content, message):
        with open(os.path.join(repo, name), 'w') as handle:
            handle.write(content)
        self.git(['add', name], cwd=repo)
        self.git(['commit', '-m', message], cwd=repo)
        return self.git(['rev-parse', 'HEAD'], cwd=repo).strip()

    def head(self, repo):
        return self.git(['rev-parse', 'HEAD'], cwd=repo).strip()

    def test_push_then_webhook_fast_forwards_the_server_and_reloads(self):
        expected = self.commit_in(self.work, 'app.py', 'print("v2")\n', 'v2')
        self.git(['push', 'origin', 'main'], cwd=self.work)
        self.assertNotEqual(self.head(self.server), expected)  # not deployed yet

        before = os.stat(self.wsgi).st_mtime
        os.utime(self.wsgi, (before - 5000, before - 5000))

        payload = push_payload()
        body = self.call(make_environ('POST', payload, sign(payload)))

        self.assertEqual(self.status, '200 OK')
        self.assertIn(b'deployed ' + expected[:7].encode(), body)
        self.assertEqual(self.head(self.server), expected)
        with open(os.path.join(self.server, 'app.py')) as handle:
            self.assertIn('v2', handle.read())
        self.assertGreater(os.stat(self.wsgi).st_mtime, time.time() - 60)
        self.assertIn(f'OK deployed {expected[:7]}', self.read_log())

    def test_a_second_push_deploys_again(self):
        for version in ('v2', 'v3'):
            expected = self.commit_in(self.work, 'app.py', f'print("{version}")\n', version)
            self.git(['push', 'origin', 'main'], cwd=self.work)
            payload = push_payload()
            self.call(make_environ('POST', payload, sign(payload)))
            self.assertEqual(self.status, '200 OK')
            self.assertEqual(self.head(self.server), expected)

    def test_dirty_server_checkout_fails_loudly_instead_of_half_deploying(self):
        self.commit_in(self.work, 'app.py', 'print("v2")\n', 'v2')
        self.git(['push', 'origin', 'main'], cwd=self.work)
        with open(os.path.join(self.server, 'app.py'), 'w') as handle:
            handle.write('edited by hand on the server\n')

        payload = push_payload()
        body = self.call(make_environ('POST', payload, sign(payload)))
        self.assertEqual(self.status, '500 Internal Server Error')
        self.assertIn(b'git stash', body)
        self.assertIn('FAILED', self.read_log())
        with open(os.path.join(self.server, 'app.py')) as handle:
            self.assertIn('edited by hand', handle.read())  # nothing lost

    def test_push_to_another_branch_does_not_touch_the_server(self):
        self.git(['checkout', '-b', 'feature'], cwd=self.work)
        expected = self.commit_in(self.work, 'app.py', 'print("v2")\n', 'v2')
        self.git(['push', 'origin', 'feature'], cwd=self.work)
        payload = push_payload('feature')
        body = self.call(make_environ('POST', payload, sign(payload)))
        self.assertEqual(self.status, '200 OK')
        self.assertIn(b'ignored', body)
        self.assertNotEqual(self.head(self.server), expected)


if __name__ == '__main__':
    unittest.main()
