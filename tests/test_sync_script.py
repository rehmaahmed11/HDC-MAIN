#!/usr/bin/env python3
"""Tests for ops/pythonanywhere/sync.sh, the fast iteration path.

`deploy.sh` is the careful path and has its own suite. `sync.sh` is the one you
run ten times a day while iterating, so what matters here is that it does the
narrow job correctly: fast-forward the checkout, reinstall only when
`requirements.txt` actually changed, request a reload only when it is asked
to, refuse to clobber a dirty checkout, and never report a clean syntax check
that did not run.

A stub `bin/python` inside a fake virtualenv records pip invocations, so these
tests never touch the network.

Run:  python3 tests/test_sync_script.py
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
SYNC_SH = os.path.join(ROOT, 'ops', 'pythonanywhere', 'sync.sh')


def git(cwd, *args):
    return subprocess.run(
        ['git', '-C', cwd, *args], check=True, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT).stdout.decode().strip()


@unittest.skipUnless(shutil.which('git') and shutil.which('bash'), 'needs git and bash')
class SyncScriptTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = self.tmp.name
        self.home = os.path.join(base, 'home')
        os.makedirs(self.home)

        # A throwaway origin, seeded with the minimum layout sync.sh expects.
        self.origin = os.path.join(base, 'origin.git')
        git(base, 'init', '-q', '--bare', 'origin.git')
        git(self.origin, 'symbolic-ref', 'HEAD', 'refs/heads/main')
        seed = os.path.join(base, 'seed')
        os.makedirs(seed)
        git(seed, 'init', '-q')
        git(seed, 'symbolic-ref', 'HEAD', 'refs/heads/main')
        git(seed, 'config', 'user.email', 'test@example.com')
        git(seed, 'config', 'user.name', 'Test')
        for rel in ('hdc', 'scripts'):
            os.makedirs(os.path.join(seed, rel), exist_ok=True)
        with open(os.path.join(seed, 'hdc', 'app.py'), 'w', encoding='utf-8') as h:
            h.write('VALUE = 1\n')
        for rel, body in (('wsgi.py', 'app = None\n'),
                          ('hdc_erp.py', 'app = None\n'),
                          ('deploy_receiver.py', 'application = None\n'),
                          ('scripts/check_layers.py', 'print("LAYERS: OK")\n')):
            with open(os.path.join(seed, rel), 'w', encoding='utf-8') as h:
                h.write(body)
        with open(os.path.join(seed, 'requirements.txt'), 'w', encoding='utf-8') as h:
            h.write('Flask>=3,<4\n')
        git(seed, 'add', '-A')
        git(seed, 'commit', '-q', '-m', 'initial')
        git(seed, 'push', '-q', self.origin, 'main')

        self.app_dir = os.path.join(base, 'HDC-MAIN')
        git(base, 'clone', '-q', self.origin, self.app_dir)
        git(self.app_dir, 'config', 'user.email', 'test@example.com')
        git(self.app_dir, 'config', 'user.name', 'Test')

        # Fake virtualenv: real interpreter, but pip is recorded rather run.
        self.pip_log = os.path.join(base, 'pip.log')
        self.venv = os.path.join(base, 'venv')
        os.makedirs(os.path.join(self.venv, 'bin'))
        wrapper = os.path.join(self.venv, 'bin', 'python')
        with open(wrapper, 'w', encoding='utf-8') as h:
            h.write(
                '#!/usr/bin/env bash\n'
                'if [[ "${1:-}" == "-m" && "${2:-}" == "pip" ]]; then\n'
                '  printf "%s\\n" "$*" >> "$HDC_TEST_PIP_LOG"\n'
                '  exit 0\n'
                'fi\n'
                f'exec {sys.executable!r} "$@"\n'
            )
        os.chmod(wrapper, 0o755)

        self.wsgi_touch = os.path.join(base, 'pythonanywhere_wsgi.py')
        with open(self.wsgi_touch, 'w', encoding='utf-8') as h:
            h.write('# pythonanywhere wsgi file\n')

        self.env_file = os.path.join(base, 'production.env')
        with open(self.env_file, 'w', encoding='utf-8') as h:
            h.write('\n'.join([
                'HDC_ENV=test',
                f'HDC_APP_DIR={self.app_dir}',
                f'HDC_VENV_PATH={self.venv}',
                f'HDC_WSGI_FILE={self.wsgi_touch}',
                'HDC_DEPLOY_BRANCH=main',
            ]) + '\n')

    # -- helpers ---------------------------------------------------------

    def run_sync(self, *args, env_file=True):
        env = dict(os.environ)
        # Isolate: never source the machine's real ~/.config/hdc/production.env.
        env['HOME'] = self.home
        env['XDG_CONFIG_HOME'] = os.path.join(self.home, '.config')
        for key in ('HDC_APP_DIR', 'HDC_VENV_PATH', 'HDC_WSGI_FILE',
                    'HDC_DEPLOY_BRANCH'):
            env.pop(key, None)
        if env_file:
            env['HDC_DEPLOY_ENV_FILE'] = self.env_file
        else:
            env['HDC_DEPLOY_ENV_FILE'] = os.path.join(self.home, 'absent.env')
        env['HDC_TEST_PIP_LOG'] = self.pip_log
        return subprocess.run(['bash', SYNC_SH, *args], env=env, cwd=ROOT,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True)

    def push_upstream(self, *changes):
        """Commit `changes` (relative path -> content) on origin/main."""
        work = os.path.join(self.tmp.name, 'upstream')
        if not os.path.isdir(work):
            subprocess.run(['git', 'clone', '-q', self.origin, work], check=True,
                           cwd=self.tmp.name)
            git(work, 'config', 'user.email', 'test@example.com')
            git(work, 'config', 'user.name', 'Test')
        for rel, body in changes:
            full = os.path.join(work, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, 'w', encoding='utf-8') as h:
                h.write(body)
        git(work, 'add', '-A')
        git(work, 'commit', '-q', '-m', 'upstream change')
        git(work, 'push', '-q', self.origin, 'main')
        return git(work, 'rev-parse', 'HEAD')

    def pip_calls(self):
        if not os.path.exists(self.pip_log):
            return []
        with open(self.pip_log, encoding='utf-8') as h:
            return [line for line in h.read().splitlines() if line.strip()]

    # -- the sync itself -------------------------------------------------

    def test_fast_forwards_and_reports_the_commits(self):
        incoming = self.push_upstream(('hdc/app.py', 'VALUE = 2\n'))
        result = self.run_sync()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(git(self.app_dir, 'rev-parse', 'HEAD'), incoming)
        self.assertIn('Synced', result.stdout)
        self.assertIn('Syntax check', result.stdout)
        self.assertIn('skipping pip install', result.stdout)
        self.assertEqual(self.pip_calls(), [])

    def test_pip_runs_only_when_requirements_txt_changed(self):
        self.push_upstream(('requirements.txt', 'Flask>=3,<4\nopenpyxl>=3.1,<4\n'))
        result = self.run_sync()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.pip_calls(), ['-m pip install -q -r requirements.txt'])
        # A second sync of the same commit must not reinstall again.
        result = self.run_sync()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.pip_calls(), ['-m pip install -q -r requirements.txt'])

    def test_pip_option_forces_a_reinstall(self):
        result = self.run_sync('--pip')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('installing', result.stdout)
        self.assertEqual(len(self.pip_calls()), 1)

    def test_reload_touches_the_wsgi_file(self):
        before = os.stat(self.wsgi_touch).st_mtime_ns
        self.push_upstream(('hdc/app.py', 'VALUE = 3\n'))
        result = self.run_sync()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('Reload requested', result.stdout)
        self.assertGreater(os.stat(self.wsgi_touch).st_mtime_ns, before)

    def test_no_reload_leaves_the_wsgi_file_alone(self):
        before = os.stat(self.wsgi_touch).st_mtime_ns
        self.push_upstream(('hdc/app.py', 'VALUE = 4\n'))
        result = self.run_sync('--no-reload')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('--no-reload', result.stdout)
        self.assertEqual(os.stat(self.wsgi_touch).st_mtime_ns, before)

    def test_branch_option_syncs_another_branch(self):
        work = os.path.join(self.tmp.name, 'feature')
        git(self.origin, 'push', '.', 'main:refs/heads/hotfix')
        subprocess.run(['git', 'clone', '-q', self.origin, work], check=True,
                       cwd=self.tmp.name)
        git(work, 'config', 'user.email', 'test@example.com')
        git(work, 'config', 'user.name', 'Test')
        git(work, 'checkout', '-q', '-B', 'hotfix', 'origin/hotfix')
        with open(os.path.join(work, 'hotfix.txt'), 'w', encoding='utf-8') as h:
            h.write('fix\n')
        git(work, 'add', '-A')
        git(work, 'commit', '-q', '-m', 'hotfix commit')
        git(work, 'push', '-q', self.origin, 'hotfix')

        result = self.run_sync('--branch', 'hotfix')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(git(self.app_dir, 'rev-parse', '--abbrev-ref', 'HEAD'), 'hotfix')
        self.assertTrue(os.path.exists(os.path.join(self.app_dir, 'hotfix.txt')))

    # -- guardrails ------------------------------------------------------

    def test_refuses_a_dirty_checkout(self):
        incoming = self.push_upstream(('hdc/app.py', 'VALUE = 5\n'))
        with open(os.path.join(self.app_dir, 'hdc', 'app.py'), 'w', encoding='utf-8') as h:
            h.write('VALUE = 999  # hotfix made on the server\n')
        result = self.run_sync()
        self.assertEqual(result.returncode, 1)
        self.assertIn('Uncommitted tracked changes', result.stdout)
        self.assertNotEqual(git(self.app_dir, 'rev-parse', 'HEAD'), incoming)

    def test_dry_run_changes_nothing(self):
        incoming = self.push_upstream(('hdc/app.py', 'VALUE = 6\n'))
        before = git(self.app_dir, 'rev-parse', 'HEAD')
        result = self.run_sync('--dry-run')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('dry-run:', result.stdout)
        self.assertIn(incoming[:7], result.stdout)
        self.assertEqual(git(self.app_dir, 'rev-parse', 'HEAD'), before)

    def test_fails_loudly_when_a_check_target_is_missing(self):
        # The syntax check is the only gate this path runs, so a missing
        # target has to be an error rather than a silent "Can't list".
        subprocess.run(['git', '-C', self.app_dir, 'rm', '-q', 'wsgi.py'], check=True)
        git(self.app_dir, 'commit', '-q', '-m', 'drop wsgi.py')
        result = self.run_sync('--no-reload')
        self.assertEqual(result.returncode, 1)
        self.assertIn('Expected path is missing', result.stdout)
        self.assertIn('never ran', result.stdout)

    def test_syntax_error_is_caught_before_the_reload(self):
        self.push_upstream(('deploy_receiver.py', 'def broken(:\n'))
        result = self.run_sync()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn('SyntaxError', result.stdout)
        self.assertNotIn('Reload requested', result.stdout)

    def test_unknown_option_is_a_usage_error(self):
        result = self.run_sync('--frobnicate')
        self.assertEqual(result.returncode, 2)
        self.assertIn('Unknown option', result.stdout)

    def test_help_lists_the_options(self):
        result = self.run_sync('--help')
        self.assertEqual(result.returncode, 0, result.stdout)
        for flag in ('--branch', '--no-reload', '--pip', '--dry-run'):
            self.assertIn(flag, result.stdout)

    def test_runs_without_an_env_file_when_app_dir_is_exported(self):
        env_backup = git(self.app_dir, 'rev-parse', 'HEAD')
        os.environ.pop('HDC_APP_DIR', None)
        proc = subprocess.run(
            ['bash', SYNC_SH, '--dry-run'],
            env={**{k: v for k, v in os.environ.items() if k != 'HDC_APP_DIR'},
                 'HOME': self.home,
                 'HDC_DEPLOY_ENV_FILE': os.path.join(self.home, 'absent.env'),
                 'XDG_CONFIG_HOME': os.path.join(self.home, '.config'),
                 'HDC_APP_DIR': self.app_dir,
                 'HDC_VENV_PATH': self.venv},
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn('no env file', proc.stdout)
        self.assertEqual(env_backup, git(self.app_dir, 'rev-parse', 'HEAD'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
