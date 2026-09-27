"""HDC core.admin — moved verbatim from hdc_erp.py.

See MODULARIZATION_PLAN.md for the module map.
"""

import os
import shutil
import sqlite3
import tempfile
from pathlib import Path
import zipfile

from sqlalchemy import text

from hdc.config import get_runtime_settings
from hdc.core.bootstrap import _bootstrap_hdc, _ensure_bootstrap_once
from hdc.extensions import db
from hdc.models.workforce import TimeEntry
from hdc.services.audit import _audit_paused
from hdc.services.subcontract import _reconcile_subcontract_links
from hdc.services.timekeeping import _reconcile_worker_time_entries, _repair_worker_work_ledger_links

def _validate_restore_database(path):
    """Read-only checks must finish before any target file/session is touched."""
    if not os.path.isfile(path):
        raise ValueError('Backup database is missing.')
    try:
        with sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True) as con:
            if con.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise ValueError('Backup database failed integrity checking.')
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            required = {'hdc_user', 'hdc_project', 'hdc_account', 'hdc_account_txn'}
            if not required.issubset(tables):
                raise ValueError('Backup database is missing required HDC tables.')
            if con.execute('PRAGMA foreign_key_check').fetchone() is not None:
                raise ValueError('Backup database contains broken foreign keys.')
    except sqlite3.Error as exc:
        raise ValueError('Backup database is invalid or unreadable.') from exc


def _restore_from_paths(db_path, estimation_path=None):
    # Both raw .db uploads and ZIPs take this path. An extension is not proof
    # that an upload is a database. Never replace live data before validation.
    _validate_restore_database(db_path)
    settings = get_runtime_settings()
    target_db = settings.db_path
    db.session.remove()
    db.engine.dispose()
    # Keep an independent WAL-aware recovery snapshot until migrations succeed.
    # Operators must quiesce other workers for a restore (see hardening guide).
    # Atomic replacement requires snapshots on the database's filesystem,
    # which need not be the same mount as HDC_INSTANCE_DIR.
    with tempfile.TemporaryDirectory(prefix='restore-', dir=os.path.dirname(target_db)) as tmp:
        recovery = os.path.join(tmp, 'previous.db')
        candidate = os.path.join(tmp, 'candidate.db')
        for source, destination in ((target_db, recovery), (db_path, candidate)):
            with sqlite3.connect(Path(source).resolve().as_uri() + '?mode=ro', uri=True) as src:
                with sqlite3.connect(destination) as dst:
                    src.backup(dst)
        old_estimation = None
        if os.path.exists(settings.estimation_store):
            old_estimation = Path(settings.estimation_store).read_bytes()

        def replace_database(source):
            db.session.remove()
            db.engine.dispose()
            for sidecar in (target_db + '-wal', target_db + '-shm'):
                if os.path.exists(sidecar):
                    os.remove(sidecar)
            os.replace(source, target_db)

        try:
            replace_database(candidate)
            if estimation_path and os.path.exists(estimation_path):
                shutil.copy2(estimation_path, settings.estimation_store)
            _ensure_bootstrap_once(force=True)
        except Exception:
            replace_database(recovery)
            if old_estimation is not None:
                Path(settings.estimation_store).write_bytes(old_estimation)
            elif os.path.exists(settings.estimation_store):
                os.remove(settings.estimation_store)
            raise


def _restore_from_backup_zip(zip_path):
    tmpdir = tempfile.mkdtemp(prefix='restore-extract-', dir=get_runtime_settings().instance_dir)
    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            for member in zf.infolist():
                mname = (member.filename or '').replace('\\', '/')
                if not mname or mname.startswith('/') or '..' in mname.split('/'):
                    raise ValueError(f'Unsafe backup member path: {member.filename}')
                dest = os.path.abspath(os.path.join(tmpdir, mname))
                if not (dest == os.path.abspath(tmpdir) or dest.startswith(os.path.abspath(tmpdir) + os.sep)):
                    raise ValueError(f'Unsafe extraction target: {member.filename}')
            zf.extractall(tmpdir)
        db_file = os.path.join(tmpdir, 'hdc_erp.db')
        if not os.path.exists(db_file):
            raise ValueError('Backup ZIP must contain hdc_erp.db')
        try:
            con = sqlite3.connect(db_file)
            cur = con.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='hdc_user'")
            has_user = cur.fetchone() is not None
            cur.close()
            con.close()
        except Exception as ex:
            raise ValueError(f'Backup DB verification failed: {ex}')
        if not has_user:
            raise ValueError('Invalid backup: hdc_user table not found. This backup appears empty or corrupted.')
        est_file = os.path.join(tmpdir, 'project_estimations.json')
        _restore_from_paths(db_file, est_file if os.path.exists(est_file) else None)
    finally:
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass


_WIPE_TARGETS = {
    'projects': {
        'label': 'Projects, Stages, Owner Payments',
        'tables': [
            'hdc_stage_drawing',
            'hdc_stage_rate_history',
            'hdc_stage',
            'hdc_stage_definition',
            'hdc_owner_payment',
            'hdc_project',
        ],
    },
    'workforce': {
        'label': 'Workers, Attendance, Timekeeping, Payroll',
        'tables': [
            'hdc_payroll_item',
            'hdc_payroll_run',
            'hdc_attendance_mark',
            'hdc_attendance_day',
            'hdc_time_entry',
            'hdc_attendance',
            'hdc_labour_rate_history',
            'hdc_labour_ledger',
            'hdc_worker_rate',
            'hdc_worker',
        ],
    },
    'materials': {
        'label': 'Materials, Purchases, Material Usage',
        'tables': [
            'hdc_usage_log_v2',
            'hdc_delivery',
            'hdc_supplier_ledger',
            'hdc_purchase_v2',
            'hdc_material_v2',
            'hdc_supplier',
            'hdc_material_usage',
            'hdc_purchase',
            'hdc_material',
        ],
    },
    'expenses': {
        'label': 'Expenses',
        'tables': [
            'hdc_expense',
        ],
    },
    'office_management': {
        'label': 'Office Management (Staff, Attendance, Office Expenses, Allowances, Categories)',
        'tables': [
            'hdc_office_staff_ledger',
            'hdc_office_staff_attendance',
            'hdc_staff_allowance',
            'hdc_allowance_category',
            'hdc_office_staff',
            'hdc_office_expense_category',
            'hdc_office_expense',
        ],
    },
    'personal_management': {
        'label': 'Personal Management (Personal Expenses + Categories)',
        'tables': [
            'hdc_personal_expense',
            'hdc_personal_expense_category',
        ],
    },
    'subcontract': {
        'label': 'Subcontractors, Events, Payments, Labour',
        'tables': [
            'hdc_subcontract_labour_payment',
            'hdc_subcontract_labour_worker',
            'hdc_subcontract_event',
            'hdc_subcontract_labour_attendance',
            'hdc_subcontract_attendance',
            'hdc_subcontract_payment',
            'hdc_subcontractor',
        ],
    },
    'formulas': {
        'label': 'Formulas and Estimations',
        'tables': [
            'hdc_estimation_stage',
            'hdc_estimation',
            'hdc_custom_formula',
        ],
    },
    'masters': {
        'label': 'Master Lists (Trades, Expense Categories)',
        'tables': [
            'hdc_worker_trade',
            'hdc_expense_category',
        ],
    },
    'alerts': {
        'label': 'Alerts and Activity Logs',
        'tables': [
            'hdc_alert',
            'hdc_activity_log',
            'hdc_user_activity',
        ],
    },
    'accounts': {
        'label': 'Accounts and Ledger',
        'tables': [
            'hdc_account_txn',
            'hdc_account',
            'hdc_runtime_flag',
        ],
    },
    'users': {
        'label': 'Users',
        'tables': [
            'hdc_user',
        ],
    },
}


def _wipe_selected_targets(target_keys):
    keys = [k for k in (target_keys or []) if k in _WIPE_TARGETS]
    if not keys:
        return {'targets': [], 'tables': 0}

    tables = []
    for k in keys:
        for t in _WIPE_TARGETS[k]['tables']:
            if t not in tables:
                tables.append(t)

    db.session.execute(text('PRAGMA foreign_keys=OFF'))
    try:
        for tbl in tables:
            db.session.execute(text(f'DELETE FROM {tbl}'))
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    finally:
        db.session.execute(text('PRAGMA foreign_keys=ON'))
        db.session.commit()

    # Recreate essential admin/default masters if they were wiped.
    # This is system reseed work; keep it out of user-facing activity logs.
    with _audit_paused():
        _bootstrap_hdc()

    # Reclaim space after large wipes.
    with db.engine.connect() as conn:
        conn.execute(text('VACUUM'))
        conn.commit()

    return {'targets': keys, 'tables': len(tables)}


def _run_admin_maintenance(project_id=None):
    stats = {
        'subcontract_link_updates': 0,
        'workers_processed': 0
    }
    stats['subcontract_link_updates'] = int(_reconcile_subcontract_links() or 0)
    q = db.session.query(TimeEntry.worker_id).filter(TimeEntry.worker_id.isnot(None))
    if project_id:
        q = q.filter(TimeEntry.project_id == project_id)
    worker_ids = [wid for (wid,) in q.distinct().all() if wid]
    for wid in worker_ids:
        _reconcile_worker_time_entries(wid)
        _repair_worker_work_ledger_links(wid)
    db.session.commit()
    stats['workers_processed'] = len(worker_ids)
    return stats
