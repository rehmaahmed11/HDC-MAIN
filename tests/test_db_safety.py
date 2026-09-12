#!/usr/bin/env python3
"""Tests for scripts/check_db_safety.py -- the "code only, never data" guard.

Run:  python3 tests/test_db_safety.py
"""

import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
SCRIPT = os.path.join(ROOT, 'scripts', 'check_db_safety.py')


def git(cwd, *args):
    return subprocess.run(['git', '-C', cwd, *args], check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True).stdout.strip()


def run_check(*args, cwd=None):
    return subprocess.run([sys.executable, SCRIPT, *args], cwd=cwd,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def output(result):
    return (result.stdout or '') + (result.stderr or '')


class CheckDbSafetyTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = os.path.join(self.tmp.name, 'repo')
        os.makedirs(self.repo)
        git(self.repo, 'init', '-q')
        git(self.repo, 'config', 'user.email', 'test@example.com')
        git(self.repo, 'config', 'user.name', 'Test')
        self.write('.gitignore', 'hdc_instance/\n*.db\nbackups/\n')
        self.write('wsgi.py', 'app = None\n')
        git(self.repo, 'add', '-A')
        git(self.repo, 'commit', '-q', '-m', 'init')

    def write(self, rel, text):
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write(text)
        return path

    def commit(self, rel):
        git(self.repo, 'add', '-A', '--', rel)
        git(self.repo, 'commit', '-q', '-m', f'add {rel}')
        return git(self.repo, 'rev-parse', 'HEAD')

    # -- clean repository --------------------------------------------------

    def test_clean_repo_passes(self):
        result = run_check('--root', self.repo)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('DB safety: OK', result.stdout)

    def test_protected_path_outside_repo_is_reported_safe(self):
        outside = os.path.join(self.tmp.name, 'HDC_INSTANCE', 'hdc_erp.db')
        os.makedirs(os.path.dirname(outside), exist_ok=True)
        with open(outside, 'w', encoding='utf-8') as handle:
            handle.write('live')
        result = run_check('--root', self.repo, '--protect', outside)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('outside the repository', result.stdout)

    def test_untracked_but_unignored_path_is_warned(self):
        # Not ignored by this repo's rules, so it could be committed by
        # accident and a `git clean` would remove it.
        inside = self.write('data/live_data.bin', 'live')
        result = run_check('--root', self.repo, '--protect', inside)
        self.assertEqual(result.returncode, 0, output(result))
        self.assertIn('NOT', output(result))
        self.assertIn('add it to .gitignore', output(result))

    def test_ignored_in_repo_path_is_safe(self):
        inside = self.write('hdc_instance/hdc_erp.db', 'live')
        result = run_check('--root', self.repo, '--protect', inside)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('untracked and git-ignored', result.stdout)

    # -- things that must fail --------------------------------------------

    def test_tracked_database_fails(self):
        path = self.write('hdc_erp.db', 'production data')
        os.chmod(path, 0o600)
        # The ignore rules must be bypassed to reproduce a real mistake.
        git(self.repo, 'add', '-f', '--', 'hdc_erp.db')
        git(self.repo, 'commit', '-q', '-m', 'oops')
        result = run_check('--root', self.repo)
        self.assertEqual(result.returncode, 1)
        self.assertIn('DB safety FAILED', output(result))
        self.assertIn('hdc_erp.db', output(result))

    def test_wal_and_backup_files_fail(self):
        for rel in ('hdc/data.db-wal', 'backups/hdc_erp.zip', 'snapshot.sqlite3'):
            with self.subTest(rel=rel):
                self.write(rel, 'bytes')
                git(self.repo, 'add', '-f', '--', rel)
                git(self.repo, 'commit', '-q', '-m', f'add {rel}')
                result = run_check('--root', self.repo)
                self.assertEqual(result.returncode, 1, rel)
                git(self.repo, 'rm', '-q', '--cached', rel)
                git(self.repo, 'commit', '-q', '-m', f'untrack {rel}')

    def test_tracked_protected_database_fails_even_outside_the_index_scan(self):
        self.write('hdc_instance/hdc_erp.db', 'live')
        git(self.repo, 'add', '-f', '--', 'hdc_instance/hdc_erp.db')
        git(self.repo, 'commit', '-q', '-m', 'instance dir committed')
        result = run_check('--root', self.repo, '--protect',
                           os.path.join(self.repo, 'hdc_instance', 'hdc_erp.db'))
        self.assertEqual(result.returncode, 1)
        self.assertIn('TRACKED', output(result))

    def test_no_fail_flag_reports_without_blocking(self):
        self.write('hdc_erp.db', 'data')
        git(self.repo, 'add', '-f', '--', 'hdc_erp.db')
        git(self.repo, 'commit', '-q', '-m', 'oops')
        result = run_check('--root', self.repo, '--no-fail')
        self.assertEqual(result.returncode, 0)
        self.assertIn('DB safety FAILED', output(result))

    def test_ref_mode_scans_an_incoming_commit_only(self):
        before = git(self.repo, 'rev-parse', 'HEAD')
        self.write('backups/dump.zip', 'archive')
        git(self.repo, 'add', '-f', '--', 'backups/dump.zip')
        git(self.repo, 'commit', '-q', '-m', 'incoming bad commit')
        # The bad file is in the working tree/index now, so the default mode
        # fails, while the *previously deployed* commit is still clean -- that
        # is exactly the deploy-time check.
        self.assertEqual(run_check('--root', self.repo).returncode, 1)
        good = run_check('--root', self.repo, '--ref', before)
        self.assertEqual(good.returncode, 0, good.stdout)
        bad = run_check('--root', self.repo, '--ref', 'HEAD')
        self.assertEqual(bad.returncode, 1)

    def test_missing_repository_explains_itself(self):
        plain = tempfile.mkdtemp(dir=self.tmp.name)
        result = run_check('--root', plain)
        self.assertEqual(result.returncode, 1)
        self.assertIn('not a Git repository', output(result))

    def test_source_archive_style_data_is_flagged(self):
        # The historical failure mode for this repo: a source zip that carried
        # the live database.
        self.write('attached_assets/source.zip', 'zip')
        git(self.repo, 'add', '-f', '--', 'attached_assets/source.zip')
        git(self.repo, 'commit', '-q', '-m', 'source archive')
        result = run_check('--root', self.repo)
        self.assertEqual(result.returncode, 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
