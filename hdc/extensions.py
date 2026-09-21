"""HDC extensions — moved verbatim from hdc_erp.py.

See MODULARIZATION_PLAN.md for the module map.
"""

import os
import secrets
import sqlite3
from functools import wraps

from flask import abort, current_app, flash, jsonify, redirect, request, session, url_for
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


_MONEY_ROLES = frozenset({'admin', 'accountant'})
_MONEY_ACCESS_MESSAGE = 'Admin/Accountant access required.'


def _money_only():
    """Return True when the current user must not make operational money writes."""
    role = (getattr(current_user, 'role', None) or '').strip().lower()
    if not current_user.is_authenticated or role not in _MONEY_ROLES:
        flash(_MONEY_ACCESS_MESSAGE, 'danger')
        return True
    return False


def _money_write_required(*, api=False):
    """Guard unsafe methods before the handler can query or mutate records.

    Apply below ``@login_required`` on operational money routes (including
    mixed GET/POST views). Existing read access and admin-only guards stay
    unchanged. JSON APIs return 403 rather than redirecting to an HTML page.
    """
    def decorate(view):
        @wraps(view)
        def guarded(*args, **kwargs):
            if request.method not in ('GET', 'HEAD', 'OPTIONS') and _money_only():
                if api:
                    return jsonify(ok=False, message=_MONEY_ACCESS_MESSAGE), 403
                return redirect(url_for('hdc_dashboard'))
            return view(*args, **kwargs)
        return guarded
    return decorate


# â”€â”€ Login Manager â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def load_user(uid):
    from hdc.models.auth import HDCUser
    return db.session.get(HDCUser, int(uid))
