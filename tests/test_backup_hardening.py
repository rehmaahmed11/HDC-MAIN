"""Backup hardening: coverage, integrity, retention, pre-flight and the CLI.

The recovery suite (``tests/test_recovery.py``) pins the *safety gates* —
corrupt archives must never reach live data.  This module pins the *coverage*
and *operability* half that discussion turned up:

* a backup is more than the database (photos, drawings);
* it carries a manifest so it can be proven restorable before it is needed;
* retention keeps a ladder instead of "the last ten clicks";
* destructive actions take their own safety snapshot first;
* the whole thing can run unattended from cron.

Everything here builds its own throwaway database and instance folder, so
production data is never touched.
"""

import json
import os
import subprocess
import sys
import unittest
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from qa_support import IsolatedAppTest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

from hdc.services import backups
from hdc.services.backups import (
    BACKUP_DB_ARCNAME,
    BACKUP_MANIFEST_ARCNAME,
    BACKUP_STAGE_DRAWINGS_ARC,
    BACKUP_TOOL_PHOTOS_ARC,
    PREFLIGHT_PREFIX,
    _backup_health,
    _create_backup_zip,
    _create_preflight_backup,
    _prune_saved_backups_policy,
    _verify_backup_zip,
)
from hdc.core.admin import _restore_from_backup_zip
from hdc.utils.dates import _pkt_now_naive


def _stamp(moment):
    return f"hdc_backup_{moment.strftime('%Y%m%d_%H%M%S')}_t{moment.strftime('%H%M%S')}.zip"


def _write_bogus_backup(folder, moment, size=64):
    """Drop a plausible-looking archive with a believable timestamp."""
    path = Path(folder) / _stamp(moment)
    path.write_bytes(b'x' * size)
    when = moment.timestamp()
    os.utime(path, (when, when))
    return path


class BackupCoverageTest(IsolatedAppTest):
    """What goes into an archive, and what comes back out."""

    def _archive(self, name='test.zip', **kwargs):
        path = Path(self.tmp.name) / name
        with self.app.app_context():
            _create_backup_zip(str(path), **kwargs)
        return path

    def test_archive_records_a_manifest_and_verifies(self):
        archive = self._archive()
        with zipfile.ZipFile(archive) as zf:
            names = zf.namelist()
        self.assertIn(BACKUP_DB_ARCNAME, names)
        self.assertIn(BACKUP_MANIFEST_ARCNAME, names)

        report = _verify_backup_zip(str(archive))
        self.assertTrue(report['ok'], report['errors'])
        self.assertFalse(report['legacy'])
        self.assertIn(BACKUP_DB_ARCNAME, report['entries'])
        self.assertGreater(report['table_count'], 10)
        self.assertEqual(report['kind'], 'manual')

    def test_operator_files_are_backed_up_and_restored(self):
        """Tool photos and stage drawings live outside the DB — until now."""
        photos = Path(self.tmp.name) / 'photos'
        photos.mkdir()
        (photos / 'tool-1.webp').write_bytes(b'PHOTO-BYTES')
        drawings = Path(self.tmp.name) / 'stage_drawings'
        drawings.mkdir(exist_ok=True)
        (drawings / 'plan.pdf').write_bytes(b'PDF-BYTES')

        with self.app.app_context(), \
                self._photos_at(photos):
            archive = self._archive('with-files.zip')
        with zipfile.ZipFile(archive) as zf:
            names = zf.namelist()
        self.assertIn(f'{BACKUP_TOOL_PHOTOS_ARC}/tool-1.webp', names)
        self.assertIn(f'{BACKUP_STAGE_DRAWINGS_ARC}/plan.pdf', names)

        (photos / 'tool-1.webp').unlink()
        (drawings / 'plan.pdf').unlink()

        with self.app.app_context(), \
                self._photos_at(photos):
            report = _restore_from_backup_zip(str(archive))

        self.assertEqual(report['copied'], 2, report)
        self.assertEqual((photos / 'tool-1.webp').read_bytes(), b'PHOTO-BYTES')
        self.assertEqual((drawings / 'plan.pdf').read_bytes(), b'PDF-BYTES')
        self.assertEqual(report['errors'], [])

    def test_tampered_archive_fails_verification(self):
        archive = self._archive('good.zip')
        tampered = Path(self.tmp.name) / 'tampered.zip'
        with zipfile.ZipFile(archive) as src:
            members = {name: src.read(name) for name in src.namelist()}
        members[BACKUP_DB_ARCNAME] += b'corruption-after-the-fact'
        with zipfile.ZipFile(tampered, 'w') as out:
            for name, data in members.items():
                out.writestr(name, data)

        report = _verify_backup_zip(str(tampered))
        self.assertFalse(report['ok'])
        self.assertTrue(any('Checksum mismatch' in e for e in report['errors']),
                        report['errors'])

    def test_legacy_archive_without_a_manifest_still_restores(self):
        """Archives written before the manifest existed must keep working."""
        archive = self._archive('modern.zip')
        legacy = Path(self.tmp.name) / 'legacy.zip'
        with zipfile.ZipFile(archive) as src:
            items = [(n, src.read(n)) for n in src.namelist()
                     if n != BACKUP_MANIFEST_ARCNAME]
        with zipfile.ZipFile(legacy, 'w') as out:
            for name, data in items:
                out.writestr(name, data)

        report = _verify_backup_zip(str(legacy))
        self.assertTrue(report['ok'], report['errors'])
        self.assertTrue(report['legacy'])

        with self.app.app_context():
            _restore_from_backup_zip(str(legacy))
        # The app still serves pages off the restored database.
        self.assertEqual(self.client.get('/hdc/projects').status_code, 200)

    def test_unsafe_archives_are_still_rejected(self):
        bad = Path(self.tmp.name) / 'escape.zip'
        with zipfile.ZipFile(bad, 'w') as zf:
            zf.writestr('../escape.db', b'nope')
        with self.app.app_context():
            with self.assertRaises(ValueError):
                _restore_from_backup_zip(str(bad))

    def _photos_at(self, folder):
        """Point the photo directory at ``folder`` for the duration."""
        return patch.object(backups, '_tool_photo_dir', lambda app=None: str(folder))


class BackupRetentionTest(IsolatedAppTest):
    """Grandfather-father-son: history must survive a burst of backups."""

    def test_ladder_keeps_history_instead_of_only_the_newest(self):
        folder = Path(self.tmp.name) / 'backups'
        folder.mkdir(exist_ok=True)
        now = datetime(2026, 3, 31, 2, 0, 0)
        for day in range(60):
            _write_bogus_backup(folder, now - timedelta(days=day))

        with self.app.app_context():
            info = _prune_saved_backups_policy(keep_recent=3, keep_daily=5,
                                               keep_weekly=3, keep_monthly=2)

        remaining = sorted(p.name for p in folder.glob('*.zip'))
        self.assertGreater(info['kept'], 5, info)
        self.assertLessEqual(info['kept'], 13, info)
        self.assertEqual(len(remaining), info['kept'])
        # The newest archive is always there.
        self.assertIn(_stamp(now), remaining)
        # …and so is a monthly rung: something a full month old, which plain
        # "keep the last N" would have thrown away.
        self.assertTrue(any(_stamp(now - timedelta(days=day)) in remaining
                            for day in range(30, 60)),
                        f'no monthly history survived: {remaining}')

    def test_byte_cap_never_deletes_the_newest_backup(self):
        folder = Path(self.tmp.name) / 'backups'
        folder.mkdir(exist_ok=True)
        for day in range(5):
            _write_bogus_backup(folder, datetime(2026, 3, 1 + day, 2, 0, 0), size=1000)

        with self.app.app_context():
            info = _prune_saved_backups_policy(max_bytes=1500)

        remaining = sorted(p.name for p in folder.glob('*.zip'))
        self.assertEqual(info['kept'], 1, info)
        self.assertLessEqual(info['bytes_after'], 1500)
        self.assertEqual(remaining, [_stamp(datetime(2026, 3, 5, 2, 0, 0))])

    def test_prune_is_a_no_op_when_there_is_nothing_to_prune(self):
        folder = Path(self.tmp.name) / 'backups'
        folder.mkdir(exist_ok=True)
        with self.app.app_context():
            info = _prune_saved_backups_policy()
        self.assertEqual(info['kept'], 0)
        self.assertEqual(info['deleted'], 0)


class BackupPreflightTest(IsolatedAppTest):
    """Destructive actions must protect themselves first."""

    def _backups(self):
        folder = Path(self.tmp.name) / 'backups'
        folder.mkdir(exist_ok=True)
        return folder

    def test_preflight_backup_is_fast_and_marked(self):
        info = None
        with self.app.app_context():
            info = _create_preflight_backup('restore')
        self.assertTrue(info['ok'], info)
        self.assertTrue(info['name'].startswith(PREFLIGHT_PREFIX + 'restore_'), info)
        self.assertTrue(Path(info['path']).exists())
        with zipfile.ZipFile(info['path']) as zf:
            self.assertNotIn('hdc_data_export.xlsx', zf.namelist())   # fast path

    def test_preflight_never_raises(self):
        with self.app.app_context():
            healthy = _create_preflight_backup('restore')
        self.assertTrue(healthy['ok'], healthy)
        # A failing backup must be reported, never thrown at the operator.
        with self.app.app_context(), \
                patch.object(backups, '_create_backup_zip',
                             side_effect=RuntimeError('boom')):
            failed = _create_preflight_backup('wipe')
        self.assertFalse(failed['ok'])
        self.assertIn('boom', failed['error'])

    def test_restore_takes_a_safety_backup_first(self):
        folder = self._backups()
        with self.app.app_context():
            _create_backup_zip(str(folder / 'restore-me.zip'))
        before = {p.name for p in folder.glob('*.zip')}

        self.form('/hdc/settings', action='restore_saved', backup_name='restore-me.zip')

        created = {p.name for p in folder.glob('*.zip')} - before
        self.assertTrue(any(n.startswith(PREFLIGHT_PREFIX + 'restore_') for n in created),
                        created)

    def test_wipe_takes_a_safety_backup_first(self):
        folder = self._backups()
        before = {p.name for p in folder.glob('*.zip')}

        self.form('/hdc/settings', action='wipe_data', wipe_targets='expenses',
                  wipe_confirm_text='WIPE', wipe_confirm_check='1')

        created = {p.name for p in folder.glob('*.zip')} - before
        self.assertTrue(any(n.startswith(PREFLIGHT_PREFIX + 'wipe_') for n in created),
                        created)

    def test_settings_screen_reports_backup_health(self):
        page = self.client.get('/hdc/settings').get_data(as_text=True)
        self.assertIn('Backup status', page)
        self.assertIn('Needs attention', page)       # nothing backed up yet

        folder = self._backups()
        fresh = 'hdc_backup_' + _pkt_now_naive().strftime('%Y%m%d_%H%M%S') + '_abc.zip'
        with self.app.app_context():
            _create_backup_zip(str(folder / fresh))
        page = self.client.get('/hdc/settings').get_data(as_text=True)
        self.assertIn('Up to date', page)
        self.assertNotIn('No backups have been taken yet', page)

    def test_settings_screen_can_verify_a_backup(self):
        folder = self._backups()
        with self.app.app_context():
            _create_backup_zip(str(folder / 'verify-me.zip'))
        self.form('/hdc/settings', action='verify_backup', backup_name='verify-me.zip')
        page = self.client.get('/hdc/settings').get_data(as_text=True)
        self.assertIn('Backup verified OK', page)

    def test_health_flags_missing_and_stale_backups(self):
        with self.app.app_context():
            health = _backup_health()
        self.assertEqual(health['count'], 0)
        self.assertTrue(health['stale'])
        self.assertTrue(any('No backups' in w for w in health['warnings']))

        _write_bogus_backup(self._backups(), datetime(2026, 1, 1, 2, 0, 0))
        with self.app.app_context():
            health = _backup_health(now=datetime(2026, 1, 5, 2, 0, 0))
        self.assertEqual(health['count'], 1)
        self.assertTrue(health['stale'])
        self.assertAlmostEqual(health['age_hours'], 96.0, places=1)


class BackupCliTest(IsolatedAppTest):
    """`scripts/hdc_backup.py` — the thing a scheduled task actually runs."""

    maxDiff = None

    def _cli(self, *argv, expect=0):
        proc = subprocess.run(
            [sys.executable, os.path.join('scripts', 'hdc_backup.py'),
             '--instance', self.tmp.name, '--db', self.path, *argv],
            cwd=ROOT, capture_output=True, text=True, timeout=300)
        if expect is not None:
            self.assertEqual(proc.returncode, expect,
                             f'stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}')
        return proc

    def test_create_verifies_and_reports(self):
        proc = self._cli('--json', 'create', '--prune', '--no-xlsx')
        payload = json.loads(proc.stdout)
        self.assertTrue(payload['ok'], payload)
        self.assertEqual(payload['errors'], [])
        self.assertEqual(payload['kind'], 'scheduled')
        self.assertGreater(payload['tables'], 10)
        self.assertTrue(Path(payload['path']).exists())
        self.assertIn('prune', payload)

    def test_list_and_verify_roundtrip(self):
        created = json.loads(self._cli('--json', 'create', '--no-xlsx').stdout)
        listing = self._cli('list')
        self.assertIn(created['name'], listing.stdout)

        verified = self._cli('verify', created['path'])
        self.assertIn('OK', verified.stdout)
        self.assertIn(created['name'], verified.stdout)

    def test_verify_fails_loudly_on_a_broken_archive(self):
        broken = Path(self.tmp.name) / 'broken.zip'
        broken.write_bytes(b'not a zip at all')
        proc = self._cli('verify', str(broken), expect=1)
        self.assertIn('FAIL', proc.stdout)

    def test_status_reports_missing_backups(self):
        proc = self._cli('status', '--exit-zero')
        self.assertIn('NEEDS ATTENTION', proc.stdout)

        self._cli('create', '--no-xlsx', '--no-files')
        ok = self._cli('status')
        self.assertIn('Status       : OK', ok.stdout)

    def test_prune_applies_the_ladder(self):
        folder = Path(self.tmp.name) / 'backups'
        folder.mkdir(exist_ok=True)
        now = datetime(2026, 5, 30, 2, 0, 0)
        for day in range(20):
            _write_bogus_backup(folder, now - timedelta(days=day))
        proc = self._cli('--json', 'prune', '--keep-recent', '2',
                         '--keep-daily', '3', '--keep-weekly', '2',
                         '--keep-monthly', '1')
        info = json.loads(proc.stdout)
        self.assertGreater(info['deleted'], 0)
        self.assertGreater(info['kept'], 0)
        self.assertLess(info['kept'], 20)

    def test_a_second_concurrent_backup_is_skipped_not_doubled(self):
        folder = Path(self.tmp.name) / 'backups'
        folder.mkdir(exist_ok=True)
        (folder / '.hdc-backup.lock').write_text('12345 0\n')
        proc = self._cli('--json', 'create')
        payload = json.loads(proc.stdout)
        self.assertTrue(payload['skipped'])
        self.assertEqual(sorted(p.name for p in folder.glob('*.zip')), [])

    def test_stale_lock_is_reclaimed(self):
        folder = Path(self.tmp.name) / 'backups'
        folder.mkdir(exist_ok=True)
        lock = folder / '.hdc-backup.lock'
        lock.write_text('12345 0\n')
        old = (datetime.now() - timedelta(days=1)).timestamp()
        os.utime(lock, (old, old))
        payload = json.loads(self._cli('--json', 'create', '--no-xlsx').stdout)
        self.assertTrue(payload['ok'], payload)
        self.assertNotIn('skipped', payload)


if __name__ == '__main__':
    unittest.main()
