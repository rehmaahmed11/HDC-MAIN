"""HDC extensions — moved verbatim from hdc_erp.py.

See MODULARIZATION_PLAN.md for the module map.
"""

import os
import secrets
import sqlite3

from flask import abort, current_app, flash, request, session
from flask_login import current_user
from sqlalchemy import event, inspect as sa_inspect
from sqlalchemy.engine import Engine

from hdc.utils.dates import _fmt_pkt
from flask_login import LoginManager
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
login_manager = LoginManager()
login_manager.login_view = "hdc_login"
login_manager.login_message_category = "warning"

@event.listens_for(Engine, "connect")
def _sqlite_fast_pragmas(dbapi_connection, connection_record):
    try:
        if not isinstance(dbapi_connection, sqlite3.Connection):
            return
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA temp_store=MEMORY")
        cur.execute("PRAGMA cache_size=-20000")
        cur.close()
    except Exception:
        # Keep startup resilient even if pragma tuning fails.
        pass


def _csrf_token():
    tok = session.get('_csrf_token')
    if not tok:
        tok = secrets.token_urlsafe(32)
        session['_csrf_token'] = tok
    return tok


def _inject_alert_count():
    from hdc.models.accounts import Alert
    try:
        count = Alert.query.filter_by(resolved=False).count()
    except Exception:
        count = 0
    return dict(alert_count=count, fmt_pkt=_fmt_pkt, csrf_token=_csrf_token())


def _ensure_db_runtime_ready():
    from hdc.core.bootstrap import _ensure_bootstrap_once
    # Inspect the active SQLAlchemy engine rather than a module-global path.
    # This keeps runtime self-healing correct for every factory-created app.
    try:
        if sa_inspect(db.engine).has_table('hdc_user'):
            return None
        _ensure_bootstrap_once(force=True)
        return None
    except Exception as ex:
        current_app.logger.error('Runtime DB self-heal failed: %s', ex)
        abort(500, description='Database schema check failed and auto-recovery failed.')


def _csrf_protect():
    if request.method not in ('POST', 'PUT', 'PATCH', 'DELETE'):
        return None
    ep = (request.endpoint or '').strip()
    if ep in ('static',):
        return None
    sent = (request.form.get('_csrf_token') or
            request.headers.get('X-CSRFToken') or
            request.headers.get('X-CSRF-Token') or '').strip()
    if not sent and request.is_json:
        payload = request.get_json(silent=True) or {}
        if isinstance(payload, dict):
            sent = str(payload.get('_csrf_token') or '').strip()
    tok = (session.get('_csrf_token') or '').strip()
    if (not sent) or (not tok) or (sent != tok):
        abort(400, description='CSRF token missing or invalid.')
    return None


def _admin_only():
    if current_user.role != 'admin':
        flash('Admin access required.', 'danger')
        return True
    return False


# â”€â”€ Login Manager â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def load_user(uid):
    from hdc.models.auth import HDCUser
    return db.session.get(HDCUser, int(uid))
