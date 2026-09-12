"""HDC core.bootstrap — moved verbatim from hdc_erp.py.

See MODULARIZATION_PLAN.md for the module map.
"""

import os
import threading

from flask import current_app
from sqlalchemy import func
from werkzeug.security import generate_password_hash

from hdc.config import INSTANCE_DIR, _DB_STORE
from hdc.core.flags import _runtime_flag_get, _runtime_flag_set
from hdc.core.schema import _ensure_accounts_schema, _ensure_owner_payment_void_schema, _ensure_purchase_v2_schema, _ensure_runtime_flags_table, _ensure_timeentry_unique_indexes, _run_migrations
from hdc.extensions import db
from hdc.models.accounts import ExpenseCategory
from hdc.models.auth import HDCUser
from hdc.models.workforce import WorkerTrade
from hdc.services.accounts import _backfill_accounts_scope_from_references, _backfill_owner_payment_receiving_accounts, _bootstrap_accounts_backfill_once, _mark_auto_generated_person_accounts
from hdc.services.subcontract import _ensure_subcontract_labour_attendance_schema, _ensure_subcontract_payment_void_schema
from hdc.services.timekeeping import _migrate_attendance_to_time_entries, _reconcile_all_time_entries_once
from hdc.utils.format import _is_strong_password

def _migrate_legacy_done_markers_to_db():
    marker_to_flag = {
        'time_entry_migration.done': 'time_entry_migration_done',
        'time_entry_dedupe.done': 'time_entry_dedupe_done',
    }
    for fname, fkey in marker_to_flag.items():
        marker = os.path.join(INSTANCE_DIR, fname)
        if os.path.exists(marker):
            if _runtime_flag_get(fkey) != '1':
                _runtime_flag_set(fkey, '1')
            try:
                os.remove(marker)
            except OSError:
                pass


def _bootstrap_hdc():
    db.create_all()
    _run_migrations()
    _ensure_subcontract_labour_attendance_schema()
    _ensure_purchase_v2_schema()
    _ensure_owner_payment_void_schema()
    _ensure_subcontract_payment_void_schema()
    _ensure_accounts_schema()
    _ensure_runtime_flags_table()
    _migrate_legacy_done_markers_to_db()
    _migrate_attendance_to_time_entries()
    _reconcile_all_time_entries_once()
    _ensure_timeentry_unique_indexes()
    admin_username = (os.environ.get('HDC_BOOTSTRAP_ADMIN_USERNAME') or 'admin').strip() or 'admin'
    if not HDCUser.query.filter_by(username=admin_username).first():
        admin_pwd = os.environ.get('HDC_BOOTSTRAP_ADMIN_PASSWORD', '').strip()
        if not admin_pwd:
            admin_pwd = (os.environ.get('HDC_DEFAULT_ADMIN_PASSWORD') or 'Admin@1234').strip()
        ok_pwd, pwd_msg = _is_strong_password(admin_pwd)
        if not ok_pwd:
            raise RuntimeError(f'Invalid bootstrap admin password: {pwd_msg}')
        db.session.add(HDCUser(
            username=admin_username,
            password_hash=generate_password_hash(admin_pwd),
            role='admin'))
        db.session.commit()
        print(f"[HDC ERP] Initial admin created: {admin_username}")
    if not WorkerTrade.query.filter_by(active_status=True).first():
        default_trades = [
            'Mason', 'Labor', 'Carpenter', 'Steel Fixer',
            'Electrician', 'Plumber', 'Painter', 'Tile Fixer', 'Supervisor'
        ]
        for tr in default_trades:
            exists = db.session.query(WorkerTrade).filter(func.lower(WorkerTrade.name) == tr.lower()).first()
            if exists:
                exists.active_status = True
            else:
                db.session.add(WorkerTrade(name=tr, active_status=True))
        db.session.commit()
    if not ExpenseCategory.query.filter_by(active_status=True).first():
        default_categories = ['Food', 'Transport', 'Material', 'Labour', 'Vehicle', 'Misc', 'Tip']
        for cat in default_categories:
            exists = db.session.query(ExpenseCategory).filter(func.lower(ExpenseCategory.name) == cat.lower()).first()
            if exists:
                exists.active_status = True
            else:
                db.session.add(ExpenseCategory(name=cat, active_status=True))
        db.session.commit()
    _bootstrap_accounts_backfill_once()
    _backfill_owner_payment_receiving_accounts()
    _backfill_accounts_scope_from_references()
    _mark_auto_generated_person_accounts()


_HDC_BOOTSTRAP_DONE = False


_HDC_BOOTSTRAP_LOCK = threading.Lock()


def _ensure_bootstrap_once(app=None, force=False):
    global _HDC_BOOTSTRAP_DONE
    if _HDC_BOOTSTRAP_DONE and not force:
        return
    with _HDC_BOOTSTRAP_LOCK:
        if _HDC_BOOTSTRAP_DONE and not force:
            return
        _app_obj = (app if app is not None
                   else current_app._get_current_object())
        with _app_obj.app_context():
            _bootstrap_hdc()
        if not os.path.exists(_DB_STORE):
            raise RuntimeError(f'Database file was not created at required path: {_DB_STORE}')
        _HDC_BOOTSTRAP_DONE = True




def reset_bootstrap_for_tests():
    """Allow a fresh bootstrap (test-only: fresh app + scratch DB)."""
    global _HDC_BOOTSTRAP_DONE
    _HDC_BOOTSTRAP_DONE = False
