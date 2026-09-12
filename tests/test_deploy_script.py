#!/usr/bin/env python3
"""End-to-end test of the PythonAnywhere deploy script.

This is the test that encodes the promise the deployment has to keep: a push to
Git updates **all the code** on the server, and **no database, no instance file
and no backup** is touched.  A throwaway clone of a throwaway "origin" stands in
for the PythonAnywhere checkout, with a fake virtualenv, so no Flask install or
network access is needed.

Run:  python3 tests/test_deploy_script.py
"""

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
DEPLOY_SH = os.path.join(ROOT, 'ops', 'pythonanywhere', 'deploy.sh')
RECEIVER = os.path.join(ROOT, 'deploy_receiver.py')
DB_SAFETY = os.path.join(ROOT, 'scripts', 'check_db_safety.py')


def git(cwd, *args):
    return subprocess.run(
        ['git', '-C', cwd, *args], check=True, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT).stdout.decode().strip()


def file_hash(path):
    if not os.path.exists(path):
        return None
    with open(path, 'rb') as handle:
        return hashlib.sha256(handle.read()).hexdigest()


@unittest.skipUnless(shutil.which('git') and shutil.which('bash'), 'needs git and bash')
class DeployScriptTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = self.tmp.name
        self.origin = os.path.join(base, 'origin.git')
        git(base, 'init', '-q', '--bare', 'origin.git')
        git(self.origin, 'symbolic-ref', 'HEAD', 'refs/heads/main')
        seed = os.path.join(base, 'seed')
        os.makedirs(seed)
        git(seed, 'init', '-q')
        git(seed, 'symbolic-ref', 'HEAD', 'refs/heads/main')
        git(seed, 'config', 'user.email', 'test@example.com')
        git(seed, 'config', 'user.name', 'Test')
        # A miniature "repository" with the files deploy.sh expects, plus the
        # real guard script and receiver so they are exercised as written.
        for rel in ('hdc', 'scripts', 'static/hdc/js/pages', 'templates/hdc', 'ops'):
            os.makedirs(os.path.join(seed, rel), exist_ok=True)
        shutil.copy(DB_SAFETY, os.path.join(seed, 'scripts', 'check_db_safety.py'))
        shutil.copy(RECEIVER, os.path.join(seed, 'deploy_receiver.py'))
        with open(os.path.join(seed, '.gitignore'), 'w', encoding='utf-8') as h:
            h.write('hdc_instance/\n*.db\nbackups/\n')
        with open(os.path.join(seed, 'requirements.txt'), 'w', encoding='utf-8') as h:
            h.write('Flask>=3,<4\n')
        with open(os.path.join(seed, 'wsgi.py'), 'w', encoding='utf-8') as h:
            h.write('app = None\n')
        with open(os.path.join(seed, 'hdc_erp.py'), 'w', encoding='utf-8') as h:
            h.write('app = None\n')
        with open(os.path.join(seed, 'hdc', 'app.py'), 'w', encoding='utf-8') as h:
            h.write('''class _App:
    def test_client(self):
        raise AssertionError('must not boot in this test')


def create_app():
    return _App()
''')
        with open(os.path.join(seed, 'scripts', 'check_layers.py'), 'w',
                  encoding='utf-8') as h:
            h.write('print("LAYERS: OK")\n')
        with open(os.path.join(seed, 'scripts', 'reorganize_frontend.py'), 'w',
                  encoding='utf-8') as h:
            h.write('print("FRONTEND: OK")\n')
        with open(os.path.join(seed, 'static', 'hdc', 'js', 'pages', 'core.js'),
                  'w', encoding='utf-8') as h:
            h.write('var a = 1;\n')
        git(seed, 'add', '-A')
        git(seed, 'commit', '-q', '-m', 'initial')
        git(seed, 'push', '-q', self.origin, 'main')
        self.first_sha = git(seed, 'rev-parse', 'HEAD')

        # The "server": a clone of origin, with live data that must survive.
        self.app_dir = os.path.join(base, 'HDC-MAIN')
        git(base, 'clone', '-q', self.origin, self.app_dir)
        git(self.app_dir, 'config', 'user.email', 'test@example.com')
        git(self.app_dir, 'config', 'user.name', 'Test')
        self.instance = os.path.join(base, 'HDC_INSTANCE')
        os.makedirs(self.instance)
        self.db_path = os.path.join(self.instance, 'hdc_erp.db')
        self.connection = sqlite3.connect(self.db_path)
        self.connection.execute('CREATE TABLE payroll (id INTEGER PRIMARY KEY, note TEXT)')
        self.connection.executemany('INSERT INTO payroll (note) VALUES (?)',
                                   [('42 days logged',), ('PKR 500,000',)])
        self.connection.commit()
        self.connection.close()
        self.db_hash_before = file_hash(self.db_path)

        # Data that lives *inside* the checkout -- the dangerous layout where a
        # careless `git clean -fdx` would destroy the production database.
        self.in_repo_instance = os.path.join(self.app_dir, 'hdc_instance')
        os.makedirs(self.in_repo_instance)
        with open(os.path.join(self.in_repo_instance, 'notes.txt'), 'w',
                  encoding='utf-8') as h:
            h.write('do not delete me\n')

        # Fake virtualenv: real python (for the guard/JSON), stub pip.
        self.venv = os.path.join(base, 'venv')
        os.makedirs(os.path.join(self.venv, 'bin'))
        os.symlink(sys.executable, os.path.join(self.venv, 'bin', 'python'))
        self.pip_marker = os.path.join(base, 'pip-ran')
        with open(os.path.join(self.venv, 'bin', 'pip'), 'w', encoding='utf-8') as h:
            h.write(f'#!/usr/bin/env bash\ntouch "{self.pip_marker}"\nexit 0\n')
        os.chmod(os.path.join(self.venv, 'bin', 'pip'), 0o755)

        self.wsgi_file = os.path.join(base, 'wsgi_config.py')
        with open(self.wsgi_file, 'w', encoding='utf-8') as h:
            h.write('# pythonanywhere wsgi file\n')
        self.state_file = os.path.join(self.instance, 'deploy', 'deploy_state.json')

        self.env_file = os.path.join(base, 'production.env')
        with open(self.env_file, 'w', encoding='utf-8') as h:
            h.write('\n'.join([
                'HDC_ENV=prod',
                f'HDC_APP_DIR={self.app_dir}',
                f'HDC_INSTANCE_DIR={self.instance}',
                f'HDC_DB_PATH={self.db_path}',
                f'HDC_VENV_PATH={self.venv}',
                'HDC_DEPLOY_BRANCH=main',
                f'HDC_WSGI_FILE={self.wsgi_file}',
                f'HDC_DEPLOY_STATE_FILE={self.state_file}',
                f'HDC_DEPLOY_LOCK_DIR={os.path.join(base, "deploy.lock")}',
                '',
            ]))
        # New upstream commit to deploy.
        with open(os.path.join(seed, 'hdc', 'feature.py'), 'w', encoding='utf-8') as h:
            h.write('NEW_CODE = True\n')
        git(seed, 'add', '-A')
        git(seed, 'commit', '-q', '-m', 'add feature')
        git(seed, 'push', '-q', self.origin, 'main')
        self.second_sha = git(seed, 'rev-parse', 'HEAD')

    # -- helpers ----------------------------------------------------------

    def deploy(self, expect_success=True, extra_env=None):
        env = dict(os.environ, HOME=self.tmp.name,
                   HDC_DEPLOY_ENV_FILE=self.env_file,
                   HDC_DEPLOY_SYNTAX_CHECK='0', HDC_DEPLOY_BOOT_CHECK='0',
                   HDC_SKIP_PIP='1', HDC_DEPLOY_SOURCE='webhook')
        env.update(extra_env or {})
        result = subprocess.run(['bash', DEPLOY_SH], cwd=self.tmp.name, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True)
        if expect_success and result.returncode != 0:
            self.fail(f'deploy failed ({result.returncode}):\n{result.stdout}')
        if not expect_success and result.returncode == 0:
            self.fail(f'deploy unexpectedly succeeded:\n{result.stdout}')
        return result.stdout

    def data_is_intact(self):
        self.assertEqual(file_hash(self.db_path), self.db_hash_before,
                         'the production database changed during a code deploy')
        connection = sqlite3.connect(self.db_path)
        try:
            rows = connection.execute('SELECT note FROM payroll ORDER BY id').fetchall()
        finally:
            connection.close()
        self.assertEqual([r[0] for r in rows], ['42 days logged', 'PKR 500,000'])
        with open(os.path.join(self.in_repo_instance, 'notes.txt'),
                  encoding='utf-8') as h:
            self.assertEqual(h.read(), 'do not delete me\n')

    def read_state(self):
        with open(self.state_file, encoding='utf-8') as h:
            return json.load(h)

    # -- tests ------------------------------------------------------------

    def test_deploy_updates_code_and_preserves_databases(self):
        reload_before = os.stat(self.wsgi_file).st_mtime_ns
        out = self.deploy()
        reload_after = os.stat(self.wsgi_file).st_mtime_ns
        self.assertGreater(reload_after, reload_before,
                           'the deploy should touch the WSGI file to reload the site')
        self.assertTrue(os.path.exists(os.path.join(self.app_dir, 'hdc', 'feature.py')))
        self.assertEqual(git(self.app_dir, 'rev-parse', 'HEAD'), self.second_sha)
        self.assertIn('backing up database', out)
        self.assertIn('Database backup and integrity check completed', out)
        self.data_is_intact()
        self.assertEqual(self.read_state()['status'], 'ok')
        self.assertEqual(self.read_state()['sha'], self.second_sha)
        # The receiver keeps a last-known-good copy outside the checkout.
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name,
                                                    'hdc_deploy_receiver.py')))

    def test_repeat_webhook_delivery_is_a_no_op(self):
        self.deploy()
        out = self.deploy()
        self.assertIn('nothing to do', out)
        self.assertEqual(git(self.app_dir, 'rev-parse', 'HEAD'), self.second_sha)
        self.data_is_intact()

    def test_pip_runs_only_when_requirements_change(self):
        self.deploy()
        self.assertFalse(os.path.exists(self.pip_marker),
                         'pip ran even though requirements.txt was unchanged')
        with open(os.path.join(self.tmp.name, 'seed', 'requirements.txt'), 'w',
                  encoding='utf-8') as h:
            h.write('Flask>=3,<4\nopenpyxl>=3.1,<4\n')
        git(os.path.join(self.tmp.name, 'seed'), 'add', '-A')
        git(os.path.join(self.tmp.name, 'seed'), 'commit', '-q', '-m', 'new dependency')
        git(os.path.join(self.tmp.name, 'seed'), 'push', '-q', self.origin, 'main')
        # HDC_SKIP_PIP is what made pip silent above; unset it to see autoskip.
        self.deploy(expect_success=True,
                    extra_env={'HDC_SKIP_PIP': '0', 'HDC_PIP_AUTOSKIP': '1'})
        self.assertTrue(os.path.exists(self.pip_marker),
                        'pip should run when requirements.txt changed')

    def test_deploy_refuses_when_a_database_is_committed_upstream(self):
        with open(os.path.join(self.tmp.name, 'seed', 'hdc_erp.db'), 'wb') as h:
            h.write(b'SQLITE format dump of production data')
        git(os.path.join(self.tmp.name, 'seed'), 'add', '-f', 'hdc_erp.db')
        git(os.path.join(self.tmp.name, 'seed'), 'commit', '-q', '-m', 'oops, committed the DB')
        git(os.path.join(self.tmp.name, 'seed'), 'push', '-q', self.origin, 'main')
        out = self.deploy(expect_success=False)
        self.assertIn('DB safety FAILED', out)
        self.assertIn('hdc_erp.db', out)
        self.assertEqual(git(self.app_dir, 'rev-parse', 'HEAD'), self.first_sha,
                         'code must not move when the incoming commit would own the DB')
        self.data_is_intact()
        self.assertEqual(self.read_state()['status'], 'failed')

    def test_local_tracked_changes_abort_the_deploy(self):
        with open(os.path.join(self.app_dir, 'wsgi.py'), 'a', encoding='utf-8') as h:
            h.write('# hotfixed on the server, should never happen\n')
        out = self.deploy(expect_success=False)
        self.assertIn('Tracked local changes', out)
        self.assertEqual(git(self.app_dir, 'rev-parse', 'HEAD'), self.first_sha)
        with open(os.path.join(self.app_dir, 'wsgi.py'), encoding='utf-8') as handle:
            self.assertIn('# hotfixed on the server', handle.read())
        self.data_is_intact()

    def test_rollback_revision_restores_old_code_but_not_data(self):
        self.deploy()
        out = self.deploy(extra_env={'HDC_DEPLOY_REVISION': self.first_sha,
                                     'HDC_DEPLOY_FORCE': '1'})
        self.assertIn('rolling the working tree back', out)
        self.assertFalse(os.path.exists(os.path.join(self.app_dir, 'hdc', 'feature.py')))
        self.data_is_intact()
        self.assertEqual(self.read_state()['status'], 'ok')

    def test_lock_prevents_two_concurrent_deploys(self):
        lock = os.path.join(self.tmp.name, 'deploy.lock')
        os.makedirs(lock)
        out = self.deploy(expect_success=False)
        self.assertIn('already running', out)
        self.assertFalse(os.path.exists(os.path.join(self.app_dir, 'hdc', 'feature.py')))
        # The foreign lock must survive the aborted run.
        self.assertTrue(os.path.isdir(lock))
        self.data_is_intact()

    def test_failure_before_config_is_still_reported_to_the_receiver(self):
        """A missing production.env must not look like an endless "running" deploy."""
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        result = subprocess.run(
            ['bash', DEPLOY_SH], cwd=self.tmp.name,
            env=dict(os.environ, HOME=self.tmp.name,
                     HDC_DEPLOY_ENV_FILE=os.path.join(self.tmp.name, 'absent.env'),
                     HDC_DEPLOY_STATE_FILE=self.state_file,
                     HDC_DEPLOY_SOURCE='webhook'),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Missing deployment environment file', result.stdout)
        state = self.read_state()
        self.assertEqual(state['status'], 'failed')
        self.assertIn('exit', state['message'])
        # The foreign-looking state file must be the only thing written.
        self.assertFalse(os.path.exists(os.path.join(self.app_dir, 'hdc', 'feature.py')))

    def test_backup_is_written_before_deploy_and_db_untouched(self):
        self.deploy()
        backups = os.listdir(os.path.join(self.instance, 'backups', 'deployments'))
        self.assertTrue(any(name.startswith('hdc_erp-') and name.endswith('.db')
                           for name in backups), backups)
        backup = next(os.path.join(self.instance, 'backups', 'deployments', name)
                     for name in backups
                     if name.startswith('hdc_erp-') and name.endswith('.db'))
        connection = sqlite3.connect(backup)
        try:
            count = connection.execute('SELECT COUNT(*) FROM payroll').fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(count, 2)
        self.data_is_intact()


if __name__ == '__main__':
    unittest.main(verbosity=2)
