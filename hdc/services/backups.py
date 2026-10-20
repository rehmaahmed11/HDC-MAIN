"""HDC services.backups — moved verbatim from hdc_erp.py.

See MODULARIZATION_PLAN.md for the module map.

Backup shape (a single ``.zip`` per point in time)
-------------------------------------------------
``hdc_erp.db``              consistent SQLite snapshot (online backup API)
``project_estimations.json`` the legacy estimation store, when present
``hdc_data_export.xlsx``    every table as a sheet (human readable, optional)
``files/tool-images/…``     tool photos, when present
``files/stage_drawings/…``  stage drawings, when present
``backup_manifest.json``    schema version, kind, SHA-256 of every member

Everything below is additive: archives written before the manifest existed
("legacy" archives) still restore, and every function keeps the call
signature it had before, with new behaviour behind keyword arguments that
default to the old behaviour (or to the safe option).
"""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import openpyxl

from hdc.config import BASE_DIR, get_runtime_settings
from hdc.utils.dates import PKT_ZONE, _pkt_now_naive
from hdc.utils.format import _quote_ident, _safe_sheet_name

# --- archive layout -------------------------------------------------------
BACKUP_DB_ARCNAME = 'hdc_erp.db'
BACKUP_ESTIMATION_ARCNAME = 'project_estimations.json'
BACKUP_XLSX_ARCNAME = 'hdc_data_export.xlsx'
BACKUP_MANIFEST_ARCNAME = 'backup_manifest.json'
BACKUP_FILES_PREFIX = 'files/'
BACKUP_TOOL_PHOTOS_ARC = BACKUP_FILES_PREFIX + 'tool-images'
BACKUP_STAGE_DRAWINGS_ARC = BACKUP_FILES_PREFIX + 'stage_drawings'

BACKUP_MANIFEST_SCHEMA = 1
# Tables a usable HDC database must contain; anything less is not a restore
# source and must never be pointed at live data.
BACKUP_REQUIRED_TABLES = ('hdc_user', 'hdc_project', 'hdc_account', 'hdc_account_txn')

# ``hdc_preflight_<reason>_<timestamp>_<hex>.zip`` — taken automatically right
# before something destructive, never by hand.
PREFLIGHT_PREFIX = 'hdc_preflight_'
_PREFLIGHT_REASONS = ('restore', 'wipe', 'deploy', 'maintenance', 'manual')

# Retention ladder (grandfather-father-son).  ``max_bytes`` defaults to 0,
# which means "no byte cap" — see ``_backup_health`` for the disk warning.
DEFAULT_KEEP_RECENT = 10
DEFAULT_KEEP_DAILY = 7
DEFAULT_KEEP_WEEKLY = 4
DEFAULT_KEEP_MONTHLY = 6
DEFAULT_MAX_BYTES = 0

# Health thresholds.
DEFAULT_MAX_AGE_HOURS = 26.0
DISK_LOW_WATER_BYTES = 128 * 1024 * 1024        # warn below this much free space
DISK_SHARE_WATERMARK = 0.40                     # warn when backups pass 40% of the disk

# ``hdc_backup_20260101_020000_ab12cd34.zip`` / the preflight equivalent.
_STAMP_RE = re.compile(r'hdc_(?:backup|preflight)_(\d{8})_(\d{6})_')
_SLUG_RE = re.compile(r'[^a-z0-9]+')


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def _settings(settings=None):
    """Explicit settings win; otherwise the active app's, or the defaults."""
    return settings if settings is not None else get_runtime_settings()


def _ensure_backup_dir(settings=None):
    st = _settings(settings)
    os.makedirs(st.backup_dir, exist_ok=True)
    return st.backup_dir


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def _digest(path):
    return {'sha256': _sha256_file(path), 'bytes': os.path.getsize(path)}


def _iter_files(root):
    """Yield ``(absolute_path, relative_posix_path)`` for every file under root."""
    if not root or not os.path.isdir(root):
        return
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            if os.path.isfile(full):
                yield full, os.path.relpath(full, root).replace(os.sep, '/')


def _tool_photo_dir(app=None):
    """Where tool photos live, with or without a Flask app context.

    Imported lazily: ``services.tool_photos`` reaches into the models, and the
    backup service must stay importable from a bare cron/CLI process.
    """
    try:
        from hdc.services.tool_photos import tool_photo_dir
        return tool_photo_dir(app)
    except Exception:                            # pragma: no cover - defensive
        return os.path.join(BASE_DIR, 'static', 'hdc', 'tool-images')


def _backup_filename():
    return f"hdc_backup_{_pkt_now_naive().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:12]}.zip"


def _slug(value):
    text = _SLUG_RE.sub('-', str(value or '').strip().lower()).strip('-')
    return (text or 'manual')[:24]


def _backup_moment(row):
    """Best-effort PKT moment for a backup row: filename stamp, else mtime."""
    match = _STAMP_RE.search(row.get('name') or '')
    if match:
        try:
            return datetime.strptime(match.group(1) + match.group(2), '%Y%m%d%H%M%S')
        except ValueError:
            pass
    modified = row.get('modified')
    if isinstance(modified, datetime):
        return modified.replace(tzinfo=None)
    return _pkt_now_naive()


def _bucket_keys(moment):
    """(day, ISO week, month) keys used by the retention ladder."""
    iso = moment.isocalendar()
    return (
        moment.strftime('%Y-%m-%d'),
        f'{iso[0]}-W{iso[1]:02d}',
        moment.strftime('%Y-%m'),
    )


def _safe_join(root, relpath):
    """Join ``relpath`` under ``root``, refusing anything that escapes it."""
    target = os.path.abspath(os.path.join(root, relpath or ''))
    base = os.path.abspath(root)
    if not (target == base or target.startswith(base + os.sep)):
        return None
    return target


def _safe_extract_zip(zip_path, dest):
    """Extract ``zip_path`` into ``dest``, refusing traversal members.

    Mirrors the guard that has always protected the restore route so an
    archive can never write outside the directory it was unpacked into.
    """
    with zipfile.ZipFile(zip_path, 'r') as zf:
        for member in zf.infolist():
            mname = (member.filename or '').replace('\\', '/')
            if not mname or mname.startswith('/') or '..' in mname.split('/'):
                raise ValueError(f'Unsafe backup member path: {member.filename}')
            if _safe_join(dest, mname) is None:
                raise ValueError(f'Unsafe extraction target: {member.filename}')
        zf.extractall(dest)


def _check_backup_database(path):
    """Read-only integrity gate for a candidate database. Returns error list."""
    if not os.path.isfile(path):
        return ['Backup database is missing.'], 0
    errors = []
    con = None
    try:
        con = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
        if con.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            errors.append('Backup database failed integrity checking.')
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if not set(BACKUP_REQUIRED_TABLES).issubset(tables):
            missing = sorted(set(BACKUP_REQUIRED_TABLES) - tables)
            errors.append('Backup database is missing required HDC tables: '
                          + ', '.join(missing))
        if con.execute('PRAGMA foreign_key_check').fetchone() is not None:
            errors.append('Backup database contains broken foreign keys.')
        return errors, len(tables)
    except sqlite3.Error as exc:
        return [f'Backup database is invalid or unreadable ({exc}).'], 0
    except Exception as exc:                     # pragma: no cover - defensive
        return [f'Backup database could not be inspected ({exc}).'], 0
    finally:
        try:
            if con is not None:
                con.close()
        except Exception:                        # pragma: no cover - defensive
            pass


# --------------------------------------------------------------------------
# creating backups
# --------------------------------------------------------------------------
def _create_backup_xlsx(dst_path, settings=None):
    st = _settings(settings)
    db_path = st.db_path
    wb = openpyxl.Workbook()
    if wb.active:
        wb.remove(wb.active)

    # Metadata sheet
    ws_meta = wb.create_sheet(title='README')
    ws_meta.append(['Generated At (PKT)', _pkt_now_naive().strftime('%Y-%m-%d %H:%M:%S')])
    ws_meta.append(['Database Path', db_path])
    ws_meta.append(['Note', 'This file is a tabular export backup (not a direct restore source).'])

    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute("""
        SELECT name
        FROM sqlite_master
        WHERE type='table' AND name NOT LIKE 'sqlite_%'
        ORDER BY name
    """)
    table_names = [r[0] for r in cur.fetchall()]

    used_sheet_names = {'README'}
    for tname in table_names:
        sheet_name = _safe_sheet_name(tname, fallback='Table')
        base_name = sheet_name
        suffix = 2
        while sheet_name in used_sheet_names:
            tail = f'_{suffix}'
            sheet_name = (base_name[: max(1, 31 - len(tail))] + tail)
            suffix += 1
        used_sheet_names.add(sheet_name)

        ws = wb.create_sheet(title=sheet_name)
        cur.execute(f"PRAGMA table_info({_quote_ident(tname)})")
        cols = [r[1] for r in cur.fetchall()]
        if not cols:
            ws.append(['(no columns detected)'])
            continue
        ws.append(cols)
        ws.freeze_panes = 'A2'

        col_csv = ', '.join(_quote_ident(c) for c in cols)
        q = f"SELECT {col_csv} FROM {_quote_ident(tname)}"
        for row in cur.execute(q):
            out = []
            for v in row:
                if isinstance(v, (bytes, bytearray)):
                    out.append(v.hex())
                else:
                    out.append(v)
            ws.append(out)

    con.close()
    wb.save(dst_path)


def _create_backup_zip(dst_path, settings=None, include_xlsx=True,
                       include_files=True, kind='manual', notes=None):
    """Write one self-describing backup archive.

    ``include_xlsx`` can be switched off for routine/scheduled runs (the
    workbook is the slow, bulky, purely-convenience member).  ``include_files``
    picks up the operator files that live outside the database: tool photos
    and stage drawings.
    """
    st = _settings(settings)
    manifest = {
        'schema': BACKUP_MANIFEST_SCHEMA,
        'kind': str(kind or 'manual'),
        'created_at_pkt': _pkt_now_naive().strftime('%Y-%m-%d %H:%M:%S'),
        'generator': 'hdc.services.backups',
        'source_db_path': st.db_path,
        'instance_dir': st.instance_dir,
        'entries': {},
        'counts': {'files': 0, 'tables': 0},
        'notes': [str(n) for n in (notes or [])],
    }
    os.makedirs(os.path.dirname(os.path.abspath(dst_path)) or '.', exist_ok=True)

    xlsx_path = None
    db_snapshot_path = None
    db_fd, db_snapshot_path = tempfile.mkstemp(prefix='hdc_db_snapshot_', suffix='.db',
                                               dir=st.instance_dir)
    os.close(db_fd)
    try:
        # Take a consistent SQLite snapshot so WAL data is included in backup DB.
        src = sqlite3.connect(st.db_path)
        table_count = 0
        try:
            dst = sqlite3.connect(db_snapshot_path)
            try:
                src.backup(dst)
                dst.commit()
                # Sanity-check snapshot integrity and presence of critical tables.
                cur = dst.cursor()
                for t in BACKUP_REQUIRED_TABLES:
                    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (t,))
                    if cur.fetchone() is None:
                        raise RuntimeError(f'Backup snapshot missing required table: {t}')
                cur.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                            "AND name NOT LIKE 'sqlite_%'")
                table_count = cur.fetchone()[0]
                cur.close()
            finally:
                dst.close()
        finally:
            src.close()
        manifest['counts']['tables'] = table_count

        if include_xlsx:
            fd, xlsx_path = tempfile.mkstemp(prefix='hdc_data_export_', suffix='.xlsx',
                                             dir=st.instance_dir)
            os.close(fd)
            _create_backup_xlsx(xlsx_path, settings=st)

        with zipfile.ZipFile(dst_path, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
            zf.write(db_snapshot_path, arcname=BACKUP_DB_ARCNAME)
            manifest['entries'][BACKUP_DB_ARCNAME] = _digest(db_snapshot_path)

            if os.path.exists(st.estimation_store):
                zf.write(st.estimation_store, arcname=BACKUP_ESTIMATION_ARCNAME)
                manifest['entries'][BACKUP_ESTIMATION_ARCNAME] = _digest(st.estimation_store)

            if include_xlsx and xlsx_path and os.path.exists(xlsx_path):
                zf.write(xlsx_path, arcname=BACKUP_XLSX_ARCNAME)
                manifest['entries'][BACKUP_XLSX_ARCNAME] = _digest(xlsx_path)

            if include_files:
                for source_root, arc_root in (
                    (_tool_photo_dir(), BACKUP_TOOL_PHOTOS_ARC),
                    (st.stage_drawings_dir, BACKUP_STAGE_DRAWINGS_ARC),
                ):
                    for full, rel in _iter_files(source_root):
                        arcname = f'{arc_root}/{rel}'
                        zf.write(full, arcname=arcname)
                        manifest['entries'][arcname] = _digest(full)
                        manifest['counts']['files'] += 1

            # Written last, from memory, so it can describe everything above.
            zf.writestr(BACKUP_MANIFEST_ARCNAME,
                        json.dumps(manifest, indent=2, sort_keys=True))
    finally:
        for stale in (db_snapshot_path, xlsx_path):
            try:
                if stale and os.path.exists(stale):
                    os.remove(stale)
            except Exception:
                pass
    return dst_path


def _create_preflight_backup(reason='manual', settings=None):
    """Snapshot taken automatically right before something destructive.

    Never raises and never blocks the operation it protects: a failure here
    must not stop a restore or a deploy, it only has to be reported.
    """
    slug = _slug(reason)
    if slug not in _PREFLIGHT_REASONS:
        slug = 'manual'
    st = _settings(settings)
    try:
        _ensure_backup_dir(st)
        name = (f'{PREFLIGHT_PREFIX}{slug}_'
                f"{_pkt_now_naive().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}.zip")
        dst = os.path.join(st.backup_dir, name)
        _create_backup_zip(dst, settings=st, include_xlsx=False,
                           include_files=True, kind=f'preflight:{slug}',
                           notes=[f'automatic safety snapshot before {slug}'])
        return {'ok': True, 'name': name, 'path': dst, 'error': None}
    except Exception as exc:
        return {'ok': False, 'name': None, 'path': None, 'error': str(exc)}


# --------------------------------------------------------------------------
# listing, inspecting, verifying
# --------------------------------------------------------------------------
def _list_backups(settings=None):
    st = _settings(settings)
    rows = []
    try:
        names = os.listdir(st.backup_dir)
    except OSError:
        names = []
    for name in names:
        if not name.lower().endswith('.zip'):
            continue
        p = os.path.join(st.backup_dir, name)
        try:
            st_ = os.stat(p)
            # Always display backup file times in Pakistan Standard Time.
            modified_pkt = datetime.fromtimestamp(st_.st_mtime, PKT_ZONE)
            rows.append({
                'name': name,
                'size': st_.st_size,
                'modified': modified_pkt,
                'path': p,
                'preflight': name.startswith(PREFLIGHT_PREFIX),
            })
        except OSError:
            continue
    rows.sort(key=lambda r: r['modified'], reverse=True)
    for row in rows:
        row['moment'] = _backup_moment(row)
    return rows


def _blank_report(path):
    return {
        'ok': False,
        'path': str(path or ''),
        'name': os.path.basename(str(path or '')),
        'legacy': False,
        'kind': None,
        'created_at_pkt': None,
        'table_count': 0,
        'file_count': 0,
        'entries': {},
        'errors': [],
        'warnings': [],
    }


def _verify_extracted_backup(extract_dir, report=None):
    """Check an already-unpacked archive: manifest checksums + DB integrity.

    Split out from :func:`_verify_backup_zip` so the restore path can verify
    the copy it has already unpacked instead of unpacking a second time.
    """
    report = report if report is not None else _blank_report(extract_dir)
    db_file = os.path.join(extract_dir, BACKUP_DB_ARCNAME)
    if not os.path.exists(db_file):
        report['errors'].append('Archive does not contain hdc_erp.db.')
        return report

    manifest = None
    manifest_path = os.path.join(extract_dir, BACKUP_MANIFEST_ARCNAME)
    if os.path.exists(manifest_path):
        try:
            manifest = json.loads(Path(manifest_path).read_text(encoding='utf-8'))
        except Exception as exc:
            report['errors'].append(f'Manifest is unreadable: {exc}')
    else:
        report['legacy'] = True
        report['warnings'].append(
            'No backup_manifest.json (archive written before checksums were '
            'recorded) — database integrity still checked.')

    if manifest:
        report['kind'] = manifest.get('kind')
        report['created_at_pkt'] = manifest.get('created_at_pkt')
        report['file_count'] = (manifest.get('counts') or {}).get('files', 0)
        for arcname, meta in sorted((manifest.get('entries') or {}).items()):
            target = _safe_join(extract_dir, arcname)
            if target is None:
                report['errors'].append(f'Manifest entry escapes the archive: {arcname}')
                continue
            if not os.path.isfile(target):
                report['errors'].append(f'Manifest lists a missing member: {arcname}')
                continue
            expected = (meta or {}).get('sha256')
            if expected and expected != _sha256_file(target):
                report['errors'].append(f'Checksum mismatch for {arcname}')
            else:
                report['entries'][arcname] = meta

    errors, table_count = _check_backup_database(db_file)
    report['errors'].extend(errors)
    report['table_count'] = table_count
    report['ok'] = not report['errors']
    return report


def _verify_backup_zip(zip_path, settings=None):
    """Prove an archive is restorable before anyone relies on it.

    Unpacks into a scratch directory (never into the live instance), checks
    the manifest checksums when the archive has one, and runs the same
    integrity/foreign-key gate the restore path uses.
    """
    report = _blank_report(zip_path)
    if not os.path.isfile(report['path']):
        report['errors'].append('Backup file not found.')
        return report

    tmpdir = tempfile.mkdtemp(prefix='hdc-verify-')
    try:
        try:
            _safe_extract_zip(report['path'], tmpdir)
        except (zipfile.BadZipFile, ValueError, OSError) as exc:
            report['errors'].append(f'Unreadable or unsafe archive: {exc}')
            return report
        return _verify_extracted_backup(tmpdir, report)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _backup_health(settings=None, max_age_hours=DEFAULT_MAX_AGE_HOURS, now=None):
    """One dict the settings screen uses to answer "are we protected?"."""
    st = _settings(settings)
    rows = _list_backups(st)
    total = sum(r['size'] for r in rows)
    newest = rows[0] if rows else None
    reference = now or _pkt_now_naive()

    age_hours = None
    if newest:
        age_hours = round(max(0.0, (reference - newest['moment']).total_seconds()) / 3600.0, 2)

    try:
        usage = shutil.disk_usage(st.backup_dir)
        disk_free, disk_total = usage.free, usage.total
    except Exception:                            # pragma: no cover - defensive
        disk_free, disk_total = 0, 0

    warnings = []
    if not rows:
        warnings.append('No backups have been taken yet.')
    if age_hours is not None and age_hours > float(max_age_hours or 0):
        warnings.append(
            f'Newest backup is {age_hours:.1f} h old (expected daily).')
    if disk_total and total > disk_total * DISK_SHARE_WATERMARK:
        warnings.append('Backups now use more than '
                        f'{int(DISK_SHARE_WATERMARK * 100)}% of the disk.')
    if disk_free and disk_free < DISK_LOW_WATER_BYTES:
        warnings.append('Less than '
                        f'{DISK_LOW_WATER_BYTES // (1024 * 1024)} MB free on the backup disk.')

    return {
        'count': len(rows),
        'total_bytes': total,
        'newest': newest['moment'] if newest else None,
        'newest_name': newest['name'] if newest else None,
        'newest_preflight': bool(newest and newest['preflight']),
        'age_hours': age_hours,
        'stale': bool(not rows or (age_hours is not None
                                   and age_hours > float(max_age_hours or 0))),
        'max_age_hours': float(max_age_hours or 0),
        'disk_free_bytes': disk_free,
        'disk_total_bytes': disk_total,
        'warnings': warnings,
        'backup_dir': st.backup_dir,
    }


# --------------------------------------------------------------------------
# retention
# --------------------------------------------------------------------------
def _prune_saved_backups(keep_latest=10, settings=None, max_bytes=0):
    """Legacy count-based pruning: keep the newest ``keep_latest`` archives.

    Preserved because it is what the settings screen has always done and what
    the recovery tests pin.  New code should prefer
    :func:`_prune_saved_backups_policy`, which keeps a time ladder.
    """
    st = _settings(settings)
    try:
        keep = int(keep_latest or 0)
    except Exception:
        keep = 10
    keep = max(0, min(200, keep))
    rows = _list_backups(st)
    try:
        cap = int(max_bytes or 0)
    except Exception:
        cap = 0
    deleted = 0
    errors = 0
    kept_bytes = 0
    for position, row in enumerate(rows):
        within_count = position < keep
        over_cap = bool(cap) and kept_bytes >= cap
        # The newest archive is the last line of defence: keep it regardless.
        if within_count and (not over_cap or position == 0):
            kept_bytes += row['size']
            continue
        if not _remove_backup(row):
            errors += 1
        else:
            deleted += 1
    return {'keep': keep, 'deleted': deleted, 'errors': errors}


def _remove_backup(row):
    try:
        if os.path.exists(row['path']):
            os.remove(row['path'])
        return True
    except Exception:
        return False


def _prune_saved_backups_policy(settings=None, keep_recent=DEFAULT_KEEP_RECENT,
                                keep_daily=DEFAULT_KEEP_DAILY,
                                keep_weekly=DEFAULT_KEEP_WEEKLY,
                                keep_monthly=DEFAULT_KEEP_MONTHLY,
                                max_bytes=DEFAULT_MAX_BYTES):
    """Grandfather-father-son retention.

    Keeps the newest ``keep_recent`` archives outright, then preserves one
    archive per distinct day/week/month up to the ladder limits, then — if a
    byte cap is set — drops the oldest survivors until the total fits.  The
    single newest archive is never deleted, however small the cap.
    """
    st = _settings(settings)
    try:
        cap = int(max_bytes or 0)
    except Exception:
        cap = 0
    rows = _list_backups(st)

    keep = []
    seen = set()

    def admit(row):
        if row['name'] in seen:
            return
        seen.add(row['name'])
        keep.append(row)

    # 1. Flat safety floor: the most recent N, whatever the calendar says.
    for row in rows[: max(0, int(keep_recent or 0))]:
        admit(row)

    # 2. Time ladder: the newest archive standing in each day / week / month.
    limits = [int(keep_daily or 0), int(keep_weekly or 0), int(keep_monthly or 0)]
    used = [set(), set(), set()]
    taken = [0, 0, 0]
    for row in rows:
        keys = _bucket_keys(row['moment'])
        for slot in range(3):
            if taken[slot] >= limits[slot] or keys[slot] in used[slot]:
                continue
            used[slot].add(keys[slot])
            taken[slot] += 1
            admit(row)

    # 3. Byte cap, oldest first, newest archive protected.
    keep.sort(key=lambda r: r['moment'], reverse=True)
    if cap > 0:
        running = 0
        bounded = []
        for row in keep:
            if bounded and running + row['size'] > cap:
                continue
            running += row['size']
            bounded.append(row)
        keep = bounded or keep[:1]

    deleted = 0
    errors = 0
    keep_names = {r['name'] for r in keep}
    for row in rows:
        if row['name'] in keep_names:
            continue
        if not _remove_backup(row):
            errors += 1
        else:
            deleted += 1

    return {
        'keep_recent': max(0, int(keep_recent or 0)),
        'keep_daily': limits[0],
        'keep_weekly': limits[1],
        'keep_monthly': limits[2],
        'max_bytes': cap,
        'kept': len(keep),
        'deleted': deleted,
        'errors': errors,
        'bytes_before': sum(r['size'] for r in rows),
        'bytes_after': sum(r['size'] for r in keep),
    }


def _prune_saved_backups_smart(settings=None, max_age_hours=DEFAULT_MAX_AGE_HOURS):
    """Retention for unattended (scheduled) runs: ladder + configured cap.

    Reads ``HDC_BACKUP_KEEP_*`` / ``HDC_BACKUP_MAX_BYTES`` so a server can
    tune retention without touching code.
    """
    def _env_int(name, default):
        try:
            return int((os.environ.get(name) or '').strip() or default)
        except Exception:
            return default

    return _prune_saved_backups_policy(
        settings=settings,
        keep_recent=_env_int('HDC_BACKUP_KEEP_RECENT', DEFAULT_KEEP_RECENT),
        keep_daily=_env_int('HDC_BACKUP_KEEP_DAILY', DEFAULT_KEEP_DAILY),
        keep_weekly=_env_int('HDC_BACKUP_KEEP_WEEKLY', DEFAULT_KEEP_WEEKLY),
        keep_monthly=_env_int('HDC_BACKUP_KEEP_MONTHLY', DEFAULT_KEEP_MONTHLY),
        max_bytes=_env_int('HDC_BACKUP_MAX_BYTES', DEFAULT_MAX_BYTES),
    )


# --------------------------------------------------------------------------
# restoring files that live beside the database
# --------------------------------------------------------------------------
def _restore_backup_files(extract_dir, settings=None, app=None):
    """Copy operator files out of an unpacked archive, newest-wins merge.

    Photos and drawings are merged, never swept: a restore must not delete
    an upload that is newer than the archive.
    """
    st = _settings(settings)
    report = {'copied': 0, 'dirs': [], 'errors': []}
    for arc_root, dest_root in (
        (BACKUP_TOOL_PHOTOS_ARC, _tool_photo_dir(app)),
        (BACKUP_STAGE_DRAWINGS_ARC, st.stage_drawings_dir),
    ):
        source = os.path.join(extract_dir, *arc_root.split('/'))
        if not os.path.isdir(source):
            continue
        try:
            os.makedirs(dest_root, exist_ok=True)
        except OSError as exc:
            report['errors'].append(f'Cannot create {dest_root}: {exc}')
            continue
        for full, rel in _iter_files(source):
            target = _safe_join(dest_root, rel)
            if target is None:                  # pragma: no cover - guard
                report['errors'].append(f'Unsafe restore target: {rel}')
                continue
            try:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                shutil.copy2(full, target)
                report['copied'] += 1
            except OSError as exc:
                report['errors'].append(f'Cannot restore {rel}: {exc}')
        report['dirs'].append(dest_root)
    return report


def _cleanup_backup_temp_artifacts(settings=None):
    st = _settings(settings)
    removed_files = 0
    removed_dirs = 0
    # Temporary snapshot/export files that may remain after interrupted backups.
    try:
        for name in os.listdir(st.instance_dir):
            p = os.path.join(st.instance_dir, name)
            if not os.path.isfile(p):
                continue
            if (name.startswith('hdc_db_snapshot_') and name.endswith('.db')) or \
               (name.startswith('hdc_data_export_') and name.endswith('.xlsx')):
                try:
                    os.remove(p)
                    removed_files += 1
                except Exception:
                    pass
    except Exception:
        pass
    # Temporary extraction folders that may remain after interrupted restore.
    try:
        for name in os.listdir(BASE_DIR):
            p = os.path.join(BASE_DIR, name)
            if (not os.path.isdir(p)) or (not name.startswith('_restore_extract_')):
                continue
            try:
                shutil.rmtree(p, ignore_errors=True)
                removed_dirs += 1
            except Exception:
                pass
    except Exception:
        pass
    # Verification scratch folders live in the system temp dir, not here, but
    # an interrupted run can still leave one behind inside the instance.
    try:
        for name in os.listdir(st.backup_dir):
            p = os.path.join(st.backup_dir, name)
            if os.path.isdir(p) and name.startswith('hdc-verify-'):
                shutil.rmtree(p, ignore_errors=True)
                removed_dirs += 1
    except Exception:
        pass
    return {'files': removed_files, 'dirs': removed_dirs}
