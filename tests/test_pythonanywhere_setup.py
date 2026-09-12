"""Offline tests: never connect to PythonAnywhere or mutate a real configuration."""
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('pa_setup', Path(__file__).resolve().parents[1] / 'ops/pythonanywhere/setup.py')
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class SetupTests(unittest.TestCase):
    def test_private_roundtrip_and_shell_literals(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'production.env'
            values = {'HDC_SECRET_KEY': 'abc$HOME`whoami`"xyz', 'HDC_APP_DIR': '/home/a b'}
            setup.write_private(path, setup.serialize(values))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(setup.read_env(path), values)
            output = subprocess.check_output(['bash', '-c', 'source "$1"; printf %s "$HDC_SECRET_KEY"', '_', str(path)], text=True)
            self.assertEqual(output, values['HDC_SECRET_KEY'])

    def test_reject_unrepresentable_values(self):
        for value in ("a'b", 'a\nb', 'a\rb', 'a\x00b'):
            with self.assertRaises(ValueError):
                setup.serialize({'HDC_SECRET_KEY': value})
        with self.assertRaises(ValueError):
            setup.serialize({'bad key': 'abc'})

    def test_backup_and_replace_preserve_original(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'production.env'
            path.write_text('original')
            setup.backup(path)
            setup.write_private(path, 'new')
            backups = list(Path(directory).glob('production.env.backup-*'))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(), 'original')
            self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.read_text(), 'new')

    def test_never_overwrites_zip_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / 'HDC-MAIN'
            project.mkdir()
            db = project / 'live.db'
            db.write_bytes(b'existing data')
            with patch.object(setup, 'run') as run:
                with self.assertRaisesRegex(ValueError, 'not a Git checkout'):
                    setup.main(project=project)
            run.assert_not_called()
            self.assertEqual(db.read_bytes(), b'existing data')

    def test_placeholder_detection(self):
        self.assertFalse(setup.usable('replace-with-a-long-random-secret'))
        self.assertFalse(setup.usable('/home/yourname/HDC-MAIN'))
        self.assertTrue(setup.usable('existing-valid-secret'))

    def test_credentials_content_and_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / setup.CREDENTIALS_FILE
            text = setup.build_credentials(
                domain='me.pythonanywhere.com',
                webhook_secret='a' * 64,
                deploy_token='tok-123',
                admin=('admin', 'Sup3r$ecret'),
                log_dir='/home/me/hdc_instance/deploy',
            )
            setup.write_private(path, text)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            content = path.read_text()
            for needle in (
                    'https://me.pythonanywhere.com/deploy/github',
                    'https://me.pythonanywhere.com/deploy/health',
                    'https://github.com/rehmaahmed11/HDC-MAIN/settings/hooks',
                    'application/json',
                    'Just the push event',
                    'a' * 64,
                    'tok-123',
                    'Sup3r$ecret',
                    '/home/me/hdc_instance/deploy/deploy.log',
            ):
                self.assertIn(needle, content)

    def test_credentials_omit_admin_for_existing_database(self):
        text = setup.build_credentials('me.pythonanywhere.com', 'a' * 64, 'tok-123')
        self.assertNotIn('Password:', text)
        self.assertNotIn('Fresh-INSTALL LOGIN', text)

    def test_strong_password_policy(self):
        seen = set()
        for _ in range(100):
            password = setup.strong_password()
            self.assertNotIn(password, seen)
            seen.add(password)
            self.assertGreaterEqual(len(password), 12)
            self.assertRegex(password, r'[A-Z]')
            self.assertRegex(password, r'[a-z]')
            self.assertRegex(password, r'[0-9]')
            self.assertRegex(password, r'[^A-Za-z0-9]')
            # Must survive the single-quoted production.env serialization.
            self.assertNotIn("'", password)
            self.assertIn(password, setup.serialize({'HDC_BOOTSTRAP_ADMIN_PASSWORD': password}))


if __name__ == '__main__':
    unittest.main()
