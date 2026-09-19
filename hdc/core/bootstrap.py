"""HDC core.bootstrap — moved verbatim from hdc_erp.py.

See MODULARIZATION_PLAN.md for the module map.
"""

import os
import threading

from flask import current_app
from sqlalchemy import func, inspect as sa_inspect
from werkzeug.security import generate_password_hash

from hdc.config import get_runtime_settings
from hdc.core.flags import _runtime_flag_get, _runtime_flag_set
from hdc.core.schema import _ensure_accounts_schema, _ensure_cashflow_schema, _ensure_owner_payment_void_schema, _ensure_purchase_v2_schema, _ensure_runtime_flags_table, _ensure_timeentry_attendance_day_schema, _ensure_timeentry_unique_indexes, _ensure_tool_rental_schema, _run_migrations
from hdc.extensions import db
from hdc.models.accounts import ExpenseCategory
from hdc.models.auth import HDCUser
from hdc.models.workforce import WorkerTrade
from hdc.services.account_classification import backfill_account_classification
from hdc.services.accounts import _backfill_accounts_scope_from_references, _backfill_owner_payment_receiving_accounts, _bootstrap_accounts_backfill_once, _mark_auto_generated_person_accounts
from hdc.services.cashflow_register import _ensure_cashflow_seed_data
from hdc.services.subcontract import _ensure_subcontract_labour_attendance_schema, _ensure_subcontract_payment_void_schema
from hdc.services.timekeeping import _migrate_attendance_to_time_entries, _reconcile_all_time_entries_once
from hdc.utils.format import _is_strong_password

def _migrate_legacy_done_markers_to_db():
    instance_dir = get_runtime_settings().instance_dir
    marker_to_flag = {
        'time_entry_migration.done': 'time_entry_migration_done',
        'time_entry_dedupe.done': 'time_entry_dedupe_done',
    }
    for fname, fkey in marker_to_flag.items():
        marker = os.path.join(instance_dir, fname)
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
    _ensure_timeentry_attendance_day_schema()
    _ensure_subcontract_labour_attendance_schema()
    _ensure_purchase_v2_schema()
    _ensure_owner_payment_void_schema()
    _ensure_subcontract_payment_void_schema()
    _ensure_accounts_schema()
    _ensure_cashflow_schema()
    _ensure_tool_rental_schema()
    _ensure_runtime_flags_table()
    _migrate_legacy_done_markers_to_db()
    _migrate_attendance_to_time_entries()
    _reconcile_all_time_entries_once()
    _ensure_timeentry_unique_indexes()
    admin_username = (os.environ.get('HDC_BOOTSTRAP_ADMIN_USERNAME') or 'admin').strip() or 'admin'
    if not HDCUser.query.filter_by(username=admin_username).first():
        environment = (os.environ.get('HDC_ENV') or 'dev').strip().lower()
        admin_pwd = os.environ.get('HDC_BOOTSTRAP_ADMIN_PASSWORD', '').strip()
        if not admin_pwd and environment in ('prod', 'production'):
            raise RuntimeError(
                'HDC_BOOTSTRAP_ADMIN_PASSWORD must be set before '
                'creating the first production admin.'
            )
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
    # Cash Flow v2: seed the register's category / party vocabulary and
    # classify legacy accounts.  Both are idempotent and purely additive, so a
    # failure here is logged at debug level and never blocks startup.
    #
    # Note there is deliberately no boot-time backfill of
    # ``hdc_account_txn.amount_minor``: new rows get their paisa mirror from a
    # model listener, and every read falls back to the legacy float column via
    # ``_tx_minor_expr()`` when the mirror is absent.  Historical data is
    # therefore reconciled correctly without a migration pass.
    try:
        _ensure_cashflow_seed_data()
    except Exception as ex:
        current_app.logger.debug('Cash flow seed skipped: %s', ex)
    try:
        from hdc.models.accounts import Account
        backfill_account_classification(Account, db, commit=False)
    except Exception as ex:
        current_app.logger.debug('Account classification backfill skipped: %s', ex)


_HDC_BOOTSTRAP_DONE = False  # legacy compatibility flag; state is per app below


_HDC_BOOTSTRAP_LOCK = threading.Lock()


def _bootstrap_state(app_obj):
    return app_obj.extensions.setdefault(
        'hdc_bootstrap_state', {'done': False}
    )


def _ensure_bootstrap_once(app=None, force=False):
    """Bootstrap the active app/database once, independently per app.

    The old implementation used one process-global flag and one process-global
    DB path.  That was unsafe for app-factory tests and for admin tooling that
    opens more than one database in a process.
    """
    global _HDC_BOOTSTRAP_DONE
    _app_obj = (app if app is not None
                else current_app._get_current_object())
    state = _bootstrap_state(_app_obj)
    if state['done'] and not force:
        return
    with _HDC_BOOTSTRAP_LOCK:
        if state['done'] and not force:
            return
        with _app_obj.app_context():
            _bootstrap_hdc()
            if not sa_inspect(db.engine).has_table('hdc_user'):
                raise RuntimeError('Database bootstrap did not create the hdc_user table.')
        state['done'] = True
        # Keep the old exported name meaningful for legacy callers, while it
        # no longer controls whether another app is bootstrapped.
        _HDC_BOOTSTRAP_DONE = True


def reset_bootstrap_for_tests(app=None):
    """Reset bootstrap state for one app (or the active app in tests)."""
    global _HDC_BOOTSTRAP_DONE
    if app is None:
        try:
            app = current_app._get_current_object()
        except RuntimeError:
            app = None
    if app is not None:
        _bootstrap_state(app)['done'] = False
    _HDC_BOOTSTRAP_DONE = False
