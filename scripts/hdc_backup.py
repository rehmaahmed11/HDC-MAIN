#!/usr/bin/env python3
"""HDC backup CLI — the command a scheduled task runs.

The settings screen can make a backup, but only when somebody remembers to
click it.  This script is the unattended half: it creates, prunes, verifies
and reports on backups without a browser, without a request and without
booting the Flask app (so a scheduled run can never run migrations, hold a
write transaction or fail because a template changed).

Everything it writes is produced by ``hdc.services.backups`` — the same code
the screen uses — so an archive made at 02:00 by cron restores exactly like
one made by hand.

Usage
-----
    # nightly: snapshot + retention in one go
    python3 scripts/hdc_backup.py create --prune

    # a fast, small snapshot (no workbook, no photos) every hour
    python3 scripts/hdc_backup.py create --no-xlsx --no-files --prune

    # what have we got, and is it fresh?
    python3 scripts/hdc_backup.py list
    python3 scripts/hdc_backup.py status
    python3 scripts/hdc_backup.py status --json

    # prove an archive is restorable BEFORE you need it
    python3 scripts/hdc_backup.py verify hdc_instance/backups/hdc_backup_....zip

    # apply retention without taking a new backup
    python3 scripts/hdc_backup.py prune --keep-daily 7 --keep-weekly 4 --keep-monthly 6

Paths
-----
By default the script uses the same paths the app does
(``HDC_INSTANCE_DIR`` / ``HDC_DB_PATH``, else ``<repo>/hdc_instance``).
Override with ``--instance`` / ``--db`` when the checkout and the data live
apart, which is exactly the layout worth having.

Tunables (environment)
----------------------
    HDC_BACKUP_KEEP_RECENT / _DAILY / _WEEKLY / _MONTHLY   retention ladder
    HDC_BACKUP_MAX_BYTES                                   hard byte cap
    HDC_BACKUP_LOCK_STALE_SECONDS                          lock reclaim age

Exit status is 0 on success, 1 on failure (so a scheduled task that fails
shows up as a failed task instead of a silent gap).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from hdc.config import load_settings                       # noqa: E402
from hdc.services import backups                           # noqa: E402
from hdc.services.backups import (                         # noqa: E402
    DEFAULT_KEEP_DAILY,
    DEFAULT_KEEP_MONTHLY,
    DEFAULT_KEEP_RECENT,
    DEFAULT_KEEP_WEEKLY,
    DEFAULT_MAX_AGE_HOURS,
    DEFAULT_MAX_BYTES,
)

LOCK_NAME = '.hdc-backup.lock'
DEFAULT_LOCK_STALE_SECONDS = 2 * 60 * 60


# --------------------------------------------------------------------------
# presentation helpers
# --------------------------------------------------------------------------
def human_bytes(value):
    size = float(value or 0)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if size < 1024.0 or unit == 'TB':
            return f'{size:,.1f} {unit}' if unit != 'B' else f'{int(size)} B'
        size /= 1024.0
    return f'{size:,.1f} TB'


def resolve(args):
    """App settings for this run, honouring --instance/--db over the env."""
    return load_settings(instance_dir=args.instance, db_path=args.db)


def emit(payload, as_json):
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return payload


class BackupLock:
    """Coarse inter-run lock so a slow backup cannot overlap the next one."""

    def __init__(self, directory, stale_seconds=DEFAULT_LOCK_STALE_SECONDS):
        self.path = os.path.join(directory, LOCK_NAME)
        self.stale_seconds = stale_seconds
        self.held = False

    def __enter__(self):
        if os.path.exists(self.path):
            try:
                age = time.time() - os.path.getmtime(self.path)
                if age < self.stale_seconds:
                    return None
                os.remove(self.path)
            except OSError:
                return None
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, 'w') as handle:
                handle.write(f'{os.getpid()} {time.time():.0f}\n')
            self.held = True
        except OSError:
            return None
        return self

    def __exit__(self, *_exc):
        if self.held:
            try:
                os.remove(self.path)
            except OSError:
                pass
        return False


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def cmd_create(args):
    settings = resolve(args)
    backups._ensure_backup_dir(settings)
    with BackupLock(settings.backup_dir) as lock:
        if lock is None:
            payload = {'ok': True, 'skipped': True,
                       'reason': 'another backup is already running'}
            emit(payload, args.json)
            if not args.json:
                print('Backup skipped: another backup is already running.')
            return 0

        name = args.name or backups._backup_filename()
        dst = os.path.join(settings.backup_dir, name)
        started = time.time()
        try:
            backups._create_backup_zip(
                dst, settings=settings,
                include_xlsx=not args.no_xlsx,
                include_files=not args.no_files,
                kind=args.kind,
            )
        except Exception as exc:
            payload = {'ok': False, 'error': str(exc), 'path': dst}
            emit(payload, args.json)
            print(f'Backup FAILED: {exc}', file=sys.stderr)
            return 1

        report = backups._verify_backup_zip(dst)
        payload = {
            'ok': bool(report['ok']),
            'name': name,
            'path': dst,
            'bytes': os.path.getsize(dst),
            'seconds': round(time.time() - started, 2),
            'kind': args.kind,
            'tables': report['table_count'],
            'files': report['file_count'],
            'errors': report['errors'],
        }
        if not report['ok']:
            payload['ok'] = False
            emit(payload, args.json)
            print('Backup created but FAILED verification: ' + '; '.join(report['errors']),
                  file=sys.stderr)
            return 1

        if args.prune:
            prune_info = backups._prune_saved_backups_policy(
                settings=settings, **_retention_kwargs(args))
            payload['prune'] = prune_info
            if prune_info['errors']:
                payload['warnings'] = [f"{prune_info['errors']} backup(s) could not "
                                       f"be deleted"]

    emit(payload, args.json)
    if not args.json:
        print(f"Backup OK: {payload['name']} ({human_bytes(payload['bytes'])}, "
              f"{payload['tables']} tables, {payload['files']} file(s), "
              f"{payload['seconds']}s)")
        if payload.get('prune'):
            print(f"  pruned: kept {payload['prune']['kept']}, "
                  f"deleted {payload['prune']['deleted']}")
    return 0


def _retention_kwargs(args):
    """Retention settings from the CLI, falling back to the ladder defaults.

    ``create --prune`` shares this with the ``prune`` sub-command, so the
    attributes are read defensively rather than assumed.
    """
    def pick(name, default):
        value = getattr(args, name, None)
        return default if value is None else value

    return {
        'keep_recent': pick('keep_recent', DEFAULT_KEEP_RECENT),
        'keep_daily': pick('keep_daily', DEFAULT_KEEP_DAILY),
        'keep_weekly': pick('keep_weekly', DEFAULT_KEEP_WEEKLY),
        'keep_monthly': pick('keep_monthly', DEFAULT_KEEP_MONTHLY),
        'max_bytes': pick('max_bytes', DEFAULT_MAX_BYTES),
    }


def cmd_prune(args, quiet=False):
    settings = resolve(args)
    info = backups._prune_saved_backups_policy(settings=settings, **_retention_kwargs(args))
    if not quiet:
        emit(info, args.json)
        if not args.json:
            print(f"Retention: kept {info['kept']}, deleted {info['deleted']}, "
                  f"errors {info['errors']} "
                  f"({human_bytes(info['bytes_before'])} -> "
                  f"{human_bytes(info['bytes_after'])})")
    return 1 if info['errors'] else 0


def cmd_list(args):
    settings = resolve(args)
    rows = backups._list_backups(settings)
    if args.json:
        emit([{k: v for k, v in row.items() if k != 'path'} for row in rows], True)
    else:
        if not rows:
            print(f'No backups in {settings.backup_dir}')
        else:
            print(f'{len(rows)} backup(s) in {settings.backup_dir}\n')
            print(f"{'name':<52} {'size':>10}  {'taken (PKT)':<20} origin")
            for row in rows:
                print(f"{row['name'][:51]:<52} {human_bytes(row['size']):>10}  "
                      f"{row['moment'].strftime('%Y-%m-%d %H:%M:%S'):<20} "
                      f"{'auto' if row['preflight'] else 'manual'}")
    return 0


def cmd_verify(args):
    worst = 0
    reports = []
    for target in args.paths:
        report = backups._verify_backup_zip(target)
        reports.append(report)
        if not report['ok']:
            worst = 1
    if args.json:
        emit(reports, True)
    else:
        for report in reports:
            label = report['name']
            if report['ok']:
                note = ' (legacy archive: no checksums)' if report['legacy'] else ''
                print(f"OK   {label} — {report['table_count']} tables, "
                      f"{len(report['entries'])} member(s) checksummed"
                      f"{', taken ' + report['created_at_pkt'] if report['created_at_pkt'] else ''}"
                      f"{note}")
            else:
                print(f"FAIL {label}: " + '; '.join(report['errors']))
            for warning in report['warnings']:
                print(f"       warning: {warning}")
    return worst


def cmd_status(args):
    settings = resolve(args)
    health = backups._backup_health(settings, max_age_hours=args.max_age_hours)
    health['ok'] = not health['warnings']
    if args.json:
        emit(health, True)
    else:
        print(f"Backups      : {health['count']} ({human_bytes(health['total_bytes'])})")
        print(f"Newest       : {health['newest_name'] or '—'}"
              f"{' (' + str(health['age_hours']) + ' h ago)' if health['age_hours'] is not None else ''}")
        print(f"Expected     : every {args.max_age_hours:g} h")
        print(f"Backup folder: {health['backup_dir']}")
        if health['disk_total_bytes']:
            print(f"Disk         : {human_bytes(health['disk_free_bytes'])} free of "
                  f"{human_bytes(health['disk_total_bytes'])}")
        for warning in health['warnings']:
            print(f"  WARNING    : {warning}")
        print('Status       : ' + ('OK' if health['ok'] else 'NEEDS ATTENTION'))
    if health['warnings']:
        return 0 if args.exit_zero else 1
    return 0


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------
def build_parser():
    parser = argparse.ArgumentParser(
        prog='hdc_backup.py',
        description='Create, prune, verify and report on HDC backups.')
    parser.add_argument('--instance', help='instance directory (overrides HDC_INSTANCE_DIR)')
    parser.add_argument('--db', help='database file (overrides HDC_DB_PATH)')
    parser.add_argument('--json', action='store_true', help='machine-readable output')
    subparsers = parser.add_subparsers(dest='command', required=True)

    create = subparsers.add_parser('create', help='take a backup now')
    create.add_argument('--name', help='archive file name (default: generated)')
    create.add_argument('--kind', default='scheduled', help="recorded kind, e.g. 'scheduled'")
    create.add_argument('--no-xlsx', action='store_true',
                        help='skip the workbook export (faster, much smaller)')
    create.add_argument('--no-files', action='store_true',
                        help='skip tool photos and stage drawings')
    create.add_argument('--prune', action='store_true',
                        help='apply the retention ladder afterwards')
    create.set_defaults(func=cmd_create)

    prune = subparsers.add_parser('prune', help='apply retention without a new backup')
    prune.add_argument('--keep-recent', type=int)
    prune.add_argument('--keep-daily', type=int)
    prune.add_argument('--keep-weekly', type=int)
    prune.add_argument('--keep-monthly', type=int)
    prune.add_argument('--max-bytes', type=int, help='hard cap on total backup bytes')
    prune.set_defaults(func=cmd_prune)

    listing = subparsers.add_parser('list', help='list saved backups')
    listing.set_defaults(func=cmd_list)

    verify = subparsers.add_parser('verify', help='prove archives are restorable')
    verify.add_argument('paths', nargs='+')
    verify.set_defaults(func=cmd_verify)

    status = subparsers.add_parser('status', help='freshness and disk report')
    status.add_argument('--max-age-hours', type=float, default=DEFAULT_MAX_AGE_HOURS)
    status.add_argument('--exit-zero', action='store_true',
                        help='always exit 0 (report-only monitoring)')
    status.set_defaults(func=cmd_status)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        result = args.func(args)
    except KeyboardInterrupt:                   # pragma: no cover - interactive
        print('interrupted', file=sys.stderr)
        return 130
    # Every sub-command returns an int; guard so a stray return value can
    # never turn into a bogus (and silent) exit status for a scheduled task.
    return result if isinstance(result, int) else 0


if __name__ == '__main__':
    sys.exit(main())
