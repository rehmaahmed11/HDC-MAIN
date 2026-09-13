#!/usr/bin/env python3
"""Tests for the PythonAnywhere installer and the WSGI file it generates.

The point of ``ops/pythonanywhere/install_deploy_hook.py`` is that nobody has
to edit a path by hand or paste a snippet into /var/www again, so these tests
run the real CLI against a throwaway repo and a throwaway ``/var/www``, then
actually *execute* the WSGI file it wrote:

* the CLI creates the secret, writes the WSGI file, keeps the old file's
  ``os.environ`` lines, backs it up, and is idempotent;
* ``--check`` / ``--dry-run`` change nothing;
* the generated WSGI file routes /deploy to the hook and everything else to
  the app -- and still answers /deploy when the app import is broken;
* end to end with real git: install, push a commit, POST a signed webhook,
  and the server checkout fast-forwards and reloads.

Run:  python3 tests/test_pa_setup.py
"""

import hashlib
import hmac
import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

sys.path.insert(0, os.path.join(ROOT, 'ops', 'pythonanywhere'))
import install_deploy_hook as installer  # noqa: E402

GIT = shutil.which('git')
MARKER = installer.MARKER
SECRET = 'test-hook-secret'

FILES_TO_COPY = (
    'deploy_hook.py',
    'wsgi_dispatch_snippet.py',
    os.path.join('ops', 'pythonanywhere', 'install_deploy_hook.py'),
)

FAKE_APP = '''"""Stand-in for the real Flask app factory."""


def create_app():
    def application(environ, start_response):
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"real hdc app v1: " + environ.get("PATH_INFO", "/").encode()]

    return application
'''

FAKE_APP_V2 = FAKE_APP.replace('v1', 'v2')

GITIGNORE = 'deploy.log\ndeploy_secret.txt\n__pycache__/\n*.bak*\n'


def read(path):
    with open(path, encoding='utf-8') as handle:
        return handle.read()


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as handle:
        handle.write(text)


def make_fake_repo(root, name='HDC-MAIN'):
    """A repo shaped like HDC-MAIN, with a fake hdc.app.create_app()."""
    repo = os.path.join(root, name)
    for rel in FILES_TO_COPY:
        dest = os.path.join(repo, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(os.path.join(ROOT, rel), dest)
    write(os.path.join(repo, 'hdc', '__init__.py'), '')
    write(os.path.join(repo, 'hdc', 'app.py'), FAKE_APP)
    write(os.path.join(repo, '.gitignore'), GITIGNORE)
    return repo


def run_cli(repo, *extra, env=None):
    script = os.path.join(repo, 'ops', 'pythonanywhere', 'install_deploy_hook.py')
    full_env = dict(os.environ)
    full_env.pop('HDC_REPO_DIR', None)
    full_env.pop('HDC_WSGI_FILE', None)
    full_env.update(env or {})
    return subprocess.run(
        [sys.executable, script, '--repo', repo, '--username', 'bob'] + list(extra),
        capture_output=True, text=True, env=full_env,
    )


def load_wsgi(path, module_name='generated_wsgi'):
    """Import a generated WSGI file the way PythonAnywhere would.

    deploy_hook / hdc are purged first so each generated file resolves them
    from its own repo path rather than from a module cached by another test.
    """
    for name in list(sys.modules):
        if name == 'deploy_hook' or name.startswith(('hdc', 'generated_wsgi')):
            del sys.modules[name]
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def call_app(app, path='/hdc/login', method='GET', body=b'', signature=None):
    environ = {
        'REQUEST_METHOD': method,
        'PATH_INFO': path,
        'CONTENT_LENGTH': str(len(body)),
        'wsgi.input': io.BytesIO(body),
        'SERVER_NAME': 'bob.pythonanywhere.com',
        'wsgi.url_scheme': 'https',
    }
    if signature:
        environ['HTTP_X_HUB_SIGNATURE_256'] = signature
    status = {}

    def start_response(code, headers):
        status['code'] = code
        status['headers'] = headers

    payload = b''.join(app(environ, start_response))
    return status.get('code'), payload


class TempCaseMixin:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.drop_modules)
        self.root = self.tmp.name
        self.repo = make_fake_repo(self.root)
        self.var_www = os.path.join(self.root, 'var-www')
        os.makedirs(self.var_www)
        self.wsgi = os.path.join(self.var_www, 'bob_pythonanywhere_com_wsgi.py')
        self.secret_file = os.path.join(self.repo, 'deploy_secret.txt')

    def drop_modules(self):
        for var in ('HDC_REPO_DIR', 'HDC_WSGI_FILE', 'HDC_PA_USERNAME'):
            os.environ.pop(var, None)
        for name in list(sys.modules):
            if name.startswith(('generated_wsgi', 'deploy_hook', 'hdc')):
                del sys.modules[name]
        repo = getattr(self, 'repo', None)
        for entry in [repo, repo and os.path.join(repo, 'hdc')]:
            while entry and entry in sys.path:
                sys.path.remove(entry)

    def install(self, *extra):
        result = run_cli(self.repo, '--var-www', self.var_www, *extra)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout


class InstallerCliTestCase(TempCaseMixin, unittest.TestCase):
    def test_fresh_install_writes_secret_wsgi_and_the_webhook_values(self):
        out = self.install()

        self.assertTrue(os.path.isfile(self.secret_file))
        secret = read(self.secret_file).strip()
        self.assertTrue(secret)
        mode = stat.S_IMODE(os.stat(self.secret_file).st_mode)
        self.assertEqual(mode, 0o600)

        self.assertTrue(os.path.isfile(self.wsgi))
        text = read(self.wsgi)
        self.assertIn(MARKER, text)
        self.assertIn(self.repo, text)                      # repo path baked in
        self.assertIn('def application(environ, start_response)', text)
        compile(text, self.wsgi, 'exec')                    # and it is valid python

        self.assertIn('https://bob.pythonanywhere.com/deploy', out)
        self.assertIn(secret, out)                          # value to paste in GitHub
        self.assertIn('application/json', out)
        self.assertIn('Reload', out)

    def test_no_placeholders_survive_anywhere(self):
        self.install()
        for path in (self.wsgi, os.path.join(self.repo, 'deploy_hook.py')):
            self.assertNotIn('YOURUSERNAME', read(path))

    def test_second_run_changes_nothing(self):
        self.install()
        first = read(self.wsgi)
        out = self.install()
        self.assertIn('unchanged', out)
        self.assertEqual(read(self.wsgi), first)
        self.assertEqual([], [p for p in os.listdir(self.var_www) if '.bak-' in p])

    def test_existing_env_lines_are_carried_over_and_old_file_backed_up(self):
        write(self.wsgi, (
            '# old PythonAnywhere WSGI file\n'
            'import os\n'
            'os.environ["HDC_DB_PATH"] = "/home/bob/hdc_instance/hdc.db"\n'
            'os.environ.setdefault("HDC_ENV", "prod")\n'
            'from legacy_thing import application  # dropped on purpose\n'
        ))
        self.install()
        text = read(self.wsgi)
        self.assertIn('os.environ["HDC_DB_PATH"] = "/home/bob/hdc_instance/hdc.db"', text)
        self.assertIn('os.environ.setdefault("HDC_ENV", "prod")', text)
        self.assertNotIn('legacy_thing', text)

        backups = [p for p in os.listdir(self.var_www) if '.bak-' in p]
        self.assertEqual(len(backups), 1)
        self.assertIn('legacy_thing', read(os.path.join(self.var_www, backups[0])))

    def test_carried_over_lines_are_not_duplicated_on_a_second_run(self):
        write(self.wsgi, 'import os\nos.environ["HDC_ENV"] = "prod"\n')
        self.install()
        self.install('--force')
        self.assertEqual(read(self.wsgi).count('os.environ["HDC_ENV"] = "prod"'), 1)
        self.assertEqual(read(self.wsgi).count(installer.BEGIN), 1)

    def test_check_writes_nothing(self):
        out = self.install('--check')
        self.assertFalse(os.path.isfile(self.wsgi))
        self.assertFalse(os.path.isfile(self.secret_file))
        self.assertIn('nothing was written', out)
        self.assertIn('hook mounted     : no', out)

    def test_check_after_install_reports_the_hook_is_mounted(self):
        self.install()
        out = self.install('--check')
        self.assertIn('hook mounted     : yes', out)
        self.assertIn('secret file      :', out)

    def test_dry_run_shows_the_file_without_writing_it(self):
        out = self.install('--dry-run')
        self.assertIn('would write', out)
        self.assertIn('nothing was written', out)
        self.assertFalse(os.path.isfile(self.wsgi))
        self.assertFalse(os.path.isfile(self.secret_file))

    def test_print_secret_reuses_the_same_secret(self):
        self.install()
        result = run_cli(self.repo, '--var-www', self.var_www, '--print-secret')
        self.assertEqual(result.stdout.strip(), read(self.secret_file).strip())

    def test_restore_puts_the_backup_back(self):
        write(self.wsgi, 'application = "the original"\n')
        self.install()
        self.assertIn(MARKER, read(self.wsgi))
        result = run_cli(self.repo, '--var-www', self.var_www, '--restore')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('the original', read(self.wsgi))
        self.assertNotIn(MARKER, read(self.wsgi))

    def test_explicit_wsgi_file_flag_is_honoured(self):
        target = os.path.join(self.root, 'custom_wsgi.py')
        self.install('--wsgi-file', target)
        self.assertTrue(os.path.isfile(target))
        self.assertFalse(os.path.isfile(self.wsgi))

    def test_missing_snippet_is_a_clear_error(self):
        os.remove(os.path.join(self.repo, 'wsgi_dispatch_snippet.py'))
        result = run_cli(self.repo, '--var-www', self.var_www)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('wsgi_dispatch_snippet.py', result.stdout + result.stderr)


class HelperTestCase(unittest.TestCase):
    def test_preserved_lines_skips_comments_and_the_generated_block(self):
        old = '\n'.join([
            installer.BEGIN,
            'import os',
            'os.environ.setdefault("HDC_REPO_DIR", "/old/path")',   # stale pin
            installer.END,
            '# os.environ["COMMENTED_OUT"] = "nope"',
            'os.environ["KEEP"] = "yes"',
            'os.environ["KEEP"] = "yes"',
            'os.environ.get("NOT_AN_ASSIGNMENT")',
            'from something import application',
            installer.MARKER,
            'os.environ["FROM_THE_SNIPPET"] = "no"',
        ])
        self.assertEqual(installer.preserved_lines(old), ['os.environ["KEEP"] = "yes"'])

    def test_build_wsgi_text_pins_the_repo_path(self):
        text = installer.build_wsgi_text('# snippet body\n', '/home/bob/HDC-MAIN', '')
        self.assertIn("HDC_REPO_DIR_DEFAULT = '/home/bob/HDC-MAIN'", text)
        self.assertIn('# snippet body', text)
        self.assertTrue(text.startswith(installer.BEGIN))


class GeneratedWsgiTestCase(TempCaseMixin, unittest.TestCase):
    """Execute the generated file: /deploy -> hook, everything else -> app."""

    def test_deploy_goes_to_the_hook_and_the_rest_to_the_app(self):
        self.install()
        app = load_wsgi(self.wsgi).application

        status, body = call_app(app, '/deploy')
        self.assertEqual(status, '200 OK')
        self.assertIn(b'HDC deploy hook', body)
        self.assertIn(b'no deploys yet', body)
        self.assertIn(self.repo.encode(), body)   # it found the repo by itself

        status, body = call_app(app, '/deploy/health')
        self.assertIn(b'HDC deploy hook', body)

        status, body = call_app(app, '/hdc/login')
        self.assertEqual(status, '200 OK')
        self.assertIn(b'real hdc app v1: /hdc/login', body)

    def test_hook_is_told_to_touch_the_file_that_is_actually_running(self):
        # No /var/www guessing: the generated file hands the hook its own path.
        self.install()
        module = load_wsgi(self.wsgi)
        self.assertEqual(module.deploy_hook.WSGI_FILE, os.path.abspath(self.wsgi))
        self.assertEqual(module.deploy_hook.REPO_DIR, self.repo)

        status, body = call_app(module.application, '/deploy')
        self.assertIn(b'(found)', body)          # the status page agrees
        self.assertNotIn(b'MISSING - hook is not installed', body)

    def test_hook_rejects_a_bad_signature_from_inside_the_generated_file(self):
        self.install()
        app = load_wsgi(self.wsgi).application
        payload = json.dumps({'ref': 'refs/heads/main'}).encode()
        status, body = call_app(app, '/deploy', 'POST', payload, 'sha256=nope')
        self.assertEqual(status, '401 Unauthorized')
        self.assertIn(b'bad signature', body)

    def test_deploy_still_answers_when_the_app_itself_is_broken(self):
        self.install()
        write(os.path.join(self.repo, 'hdc', 'app.py'), 'def create_app(:\n')  # syntax error
        app = load_wsgi(self.wsgi).application

        status, body = call_app(app, '/deploy')
        self.assertIn(b'HDC deploy hook', body)   # the page you need most

        status, body = call_app(app, '/hdc/login')
        self.assertEqual(status, '500 Internal Server Error')
        self.assertIn(b'failed to start', body)
        self.assertIn(b'/deploy', body)

    def test_repo_is_found_from_the_wsgi_filename_without_any_flag(self):
        # Simulate PA: no HDC_REPO_DIR and a different USER -- the username
        # comes from the WSGI file's own name, and the repo from ~/HDC-MAIN.
        home = os.path.join(self.root, 'home', 'bob')
        target = os.path.join(home, 'HDC-MAIN')
        os.makedirs(home, exist_ok=True)
        shutil.copytree(self.repo, target, dirs_exist_ok=True)
        shutil.copyfile(os.path.join(self.repo, 'wsgi_dispatch_snippet.py'), self.wsgi)

        real_expanduser = os.path.expanduser
        with mock.patch.dict(os.environ,
                             {'HDC_REPO_DIR': '', 'USER': 'someone-else', 'LOGNAME': ''}):
            module = load_wsgi(self.wsgi, 'generated_wsgi_named')
            self.assertEqual(module._username(), 'bob')
            with mock.patch('os.path.expanduser',
                            side_effect=lambda p: home if p == '~bob' else real_expanduser(p)):
                self.assertEqual(module._detect_repo_dir(), target)


@unittest.skipUnless(GIT, 'git is not installed')
class EndToEndTestCase(TempCaseMixin, unittest.TestCase):
    """install -> push -> signed webhook -> server pulls and reloads."""

    def setUp(self):
        super().setUp()
        self.git_env = ['-c', 'user.name=HDC Test', '-c', 'user.email=test@example.com',
                        '-c', 'commit.gpgsign=false', '-c', 'init.defaultBranch=main']
        self.origin = os.path.join(self.root, 'origin.git')
        self.git(['init', '--bare', '-b', 'main', self.origin])
        # The fake repo becomes the developer's clone; PA gets its own clone.
        self.git(['init', '-b', 'main', self.repo], cwd=None)
        self.git(['remote', 'add', 'origin', self.origin], cwd=self.repo)
        self.git(['add', '-A'], cwd=self.repo)
        self.git(['commit', '-m', 'initial'], cwd=self.repo)
        self.git(['push', '-u', 'origin', 'main'], cwd=self.repo)

        self.server = os.path.join(self.root, 'server', 'HDC-MAIN')
        os.makedirs(os.path.dirname(self.server), exist_ok=True)
        self.git(['clone', self.origin, self.server])

    def git(self, args, cwd=None):
        return subprocess.check_output(
            [GIT] + self.git_env + args, cwd=cwd, stderr=subprocess.STDOUT,
        ).decode(errors='replace')

    def head(self, repo):
        return self.git(['rev-parse', 'HEAD'], cwd=repo).strip()

    def test_full_cycle_deploys_a_push(self):
        # Install on the "server" exactly as a user would: one command, no edits.
        result = run_cli(self.server, '--var-www', self.var_www, '--username', 'bob')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        server_wsgi = os.path.join(self.var_www, 'bob_pythonanywhere_com_wsgi.py')
        self.assertTrue(os.path.isfile(server_wsgi))
        secret = read(os.path.join(self.server, 'deploy_secret.txt')).strip()

        # Developer pushes a change.
        write(os.path.join(self.repo, 'hdc', 'app.py'), FAKE_APP_V2)
        self.git(['add', '-A'], cwd=self.repo)
        self.git(['commit', '-m', 'v2'], cwd=self.repo)
        self.git(['push', 'origin', 'main'], cwd=self.repo)
        pushed = self.head(self.repo)
        self.assertNotEqual(self.head(self.server), pushed)

        # GitHub calls the webhook.
        app = load_wsgi(server_wsgi, 'generated_wsgi_e2e').application
        payload = json.dumps({'ref': 'refs/heads/main', 'after': pushed}).encode()
        signature = 'sha256=' + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        status, body = call_app(app, '/deploy', 'POST', payload, signature)

        self.assertEqual(status, '200 OK')
        self.assertIn(b'deployed ' + pushed[:7].encode(), body)
        self.assertEqual(self.head(self.server), pushed)
        self.assertIn('v2', read(os.path.join(self.server, 'hdc', 'app.py')))

        # The site now serves the new code, and /deploy shows the deploy.
        status, body = call_app(app, '/hdc/login')
        self.assertIn(b'real hdc app v2', body)
        status, body = call_app(app, '/deploy')
        self.assertIn(f'OK deployed {pushed[:7]}'.encode(), body)
        self.assertIn(b'clean', body)

    def test_webhook_with_the_wrong_secret_is_rejected(self):
        self.install()
        payload = json.dumps({'ref': 'refs/heads/main'}).encode()
        bad = 'sha256=' + hmac.new(b'not-the-secret', payload, hashlib.sha256).hexdigest()
        app = load_wsgi(self.wsgi).application
        status, body = call_app(app, '/deploy', 'POST', payload, bad)
        self.assertEqual(status, '401 Unauthorized')
        self.assertIn(b'bad signature', body)


if __name__ == '__main__':
    unittest.main()
