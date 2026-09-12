#!/usr/bin/env python3
"""Tests for scripts/reset_admin_password.py, the lost-login escape hatch.

The bootstrap in `hdc/core/bootstrap.py` only creates the first admin when no
row for that username exists yet, so re-deploying with a new
`HDC_BOOTSTRAP_ADMIN_PASSWORD` never changes an existing password. This script
is the supported way to do it, which makes two properties worth pinning:

* it edits whichever database `HDC_DB_PATH` points at (not a copy), and
* the password it writes is the one the real login form accepts.

Run:  python3 tests/test_admin_password_cli.py
"""

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
SCRIPT = os.path.join(ROOT, 'scripts', 'reset_admin_password.py')
STRONG = 'Correct#Horse2026'

try:                                    # the script needs the app package
    import flask                        # noqa: F401
    HAVE_FLASK = True
except ImportError:                     # pragma: no cover - CI installs Flask
    HAVE_FLASK = False


@unittest.skipUnless(HAVE_FLASK, 'needs Flask + the hdc package installed')
class ResetAdminPasswordTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = self.tmp.name
        self.db_path = os.path.join(base, 'hdc_erp.db')
        self.instance = os.path.join(base, 'instance')
        self.env = {
            'PYTHONPATH': ROOT,
            'HDC_ENV': 'test',
            'HDC_SECRET_KEY': 'test-only-secret',
            'HDC_DB_PATH': self.db_path,
            'HDC_INSTANCE_DIR': self.instance,
            'HDC_BOOTSTRAP_ADMIN_USERNAME': 'admin',
            'HDC_BOOTSTRAP_ADMIN_PASSWORD': 'Boot@12345',
        }

    # -- helpers ---------------------------------------------------------

    def run_cli(self, *args, stdin=None):
        env = {**{k: v for k, v in os.environ.items() if not k.startswith('HDC_')},
               **self.env}
        return subprocess.run([sys.executable, SCRIPT, *args], env=env, cwd=ROOT,
                              input=stdin, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True)

    def users(self):
        if not os.path.exists(self.db_path):
            return {}
        con = sqlite3.connect(self.db_path)
        try:
            return dict(con.execute(
                'SELECT username, password_hash FROM hdc_user').fetchall())
        finally:
            con.close()

    # -- behaviour -------------------------------------------------------

    def test_show_identifies_the_database_and_users(self):
        result = self.run_cli('--show')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn(self.db_path, result.stdout)
        # the bootstrap had to run first, which is exactly what --show tells you
        self.assertIn('username=', result.stdout)

    def test_password_is_written_to_the_configured_database_and_logins_work(self):
        result = self.run_cli('--username', 'admin', '--password', STRONG)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('password updated', result.stdout)
        self.assertIn(self.db_path, result.stdout)

        hashes = self.users()
        self.assertIn('admin', hashes)
        from werkzeug.security import check_password_hash
        self.assertTrue(check_password_hash(hashes['admin'], STRONG))
        self.assertFalse(check_password_hash(hashes['admin'], 'Boot@12345'))

        # and the real form accepts it: fetch the CSRF token, then log in
        from hdc.app import create_app
        with mock.patch.dict(os.environ, self.env):
            app = create_app()
            client = app.test_client()
            page = client.get('/hdc/login')
            token = page.text.split('name="_csrf_token" value="', 1)[1].split('"', 1)[0]
            ok = client.post('/hdc/login', data={
                'username': 'admin', 'password': STRONG, '_csrf_token': token})
            self.assertIn(ok.status_code, (200, 302))
            self.assertNotIn('Invalid username or password', ok.text)
            self.assertIn('Logout', client.get('/hdc/', follow_redirects=True).text)

            # a still-logged-in session skips the form, so check the old
            # password from a fresh client
            fresh = app.test_client()
            stale_page = fresh.get('/hdc/login')
            stale_token = stale_page.text.split(
                'name="_csrf_token" value="', 1)[1].split('"', 1)[0]
            bad = fresh.post('/hdc/login', data={
                'username': 'admin', 'password': 'Boot@12345', '_csrf_token': stale_token})
            self.assertIn('Invalid username or password', bad.text)

    def test_weak_password_is_refused_and_changes_nothing(self):
        self.run_cli('--username', 'admin', '--password', STRONG)
        before = self.users()
        result = self.run_cli('--username', 'admin', '--password', 'short')
        self.assertEqual(result.returncode, 2)
        self.assertIn('Password must be at least', result.stdout)
        self.assertEqual(self.users(), before)

    def test_password_is_required(self):
        result = self.run_cli('--username', 'admin')
        self.assertEqual(result.returncode, 2)
        self.assertIn('provide --password or --prompt', result.stdout)

    def test_prompt_reads_the_password_from_stdin(self):
        result = self.run_cli('--username', 'admin', '--prompt',
                              stdin=f'{STRONG}\n{STRONG}\n')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('password updated', result.stdout)
        self.assertNotIn(STRONG, result.stdout)   # never echoed back

    def test_prompt_mismatch_changes_nothing(self):
        self.run_cli('--username', 'admin', '--password', STRONG)
        before = self.users()
        result = self.run_cli('--username', 'admin', '--prompt', stdin='aaa111#BBB\nzzz222#CCC\n')
        self.assertEqual(result.returncode, 2)
        self.assertIn('do not match', result.stdout)
        self.assertEqual(self.users(), before)

    def test_missing_user_is_created_with_the_requested_role(self):
        self.run_cli('--username', 'admin', '--password', STRONG)
        result = self.run_cli('--username', 'sitekeeper', '--password',
                              'Manager#2026x', '--role', 'manager')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("created for 'sitekeeper'", result.stdout)
        if not os.path.exists(self.db_path):
            self.fail('database was not created')
        con = sqlite3.connect(self.db_path)
        try:
            rows = dict(con.execute('SELECT username, role FROM hdc_user').fetchall())
        finally:
            con.close()
        self.assertEqual(rows.get('sitekeeper'), 'manager')

    def test_role_is_left_alone_when_not_requested(self):
        self.run_cli('--username', 'admin', '--password', STRONG)
        con = sqlite3.connect(self.db_path)
        before = con.execute('SELECT role FROM hdc_user WHERE username="admin"').fetchone()[0]
        con.close()
        self.run_cli('--username', 'admin', '--password', 'Another#Pass2026')
        con = sqlite3.connect(self.db_path)
        after = con.execute('SELECT role FROM hdc_user WHERE username="admin"').fetchone()[0]
        con.close()
        self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main(verbosity=2)
