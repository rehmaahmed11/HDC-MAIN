"""HDC routes: Login, logout and the root redirect.

Moved verbatim from hdc_erp.py; each handler keeps its
original @app.route decorator and endpoint name.
"""

import os
import threading
import time

from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user
from werkzeug.security import check_password_hash

from hdc.models.auth import HDCUser
from hdc.services.audit import _record_user_activity, log_action

def register(app):
    """Register Login, logout and the root redirect."""
    # This small limiter is per worker. Add a shared proxy limiter for a
    # multi-worker deployment, but never leave login unlimited.
    login_state = {'lock': threading.Lock(), 'attempts': {}}
    login_window = int(os.environ.get('HDC_LOGIN_WINDOW_SECONDS', '300') or 300)
    login_max_attempts = int(os.environ.get('HDC_LOGIN_MAX_ATTEMPTS', '5') or 5)
    login_lockout = int(os.environ.get('HDC_LOGIN_LOCKOUT_SECONDS', '900') or 900)

    def _login_key():
        username = (request.form.get('username') or '').strip().lower()
        return f"{request.remote_addr or 'unknown'}:{username}"

    def _login_blocked(key):
        now = time.monotonic()
        with login_state['lock']:
            row = login_state['attempts'].get(key)
            if not row:
                return False
            if row['blocked_until'] > now:
                return True
            if row['window_start'] + login_window <= now:
                login_state['attempts'].pop(key, None)
            return False

    def _record_login_failure(key):
        now = time.monotonic()
        window = login_window
        max_attempts = login_max_attempts
        lockout = login_lockout
        with login_state['lock']:
            row = login_state['attempts'].get(key)
            if not row or row['window_start'] + window <= now:
                row = {'window_start': now, 'count': 0, 'blocked_until': 0.0}
            row['count'] += 1
            if row['count'] >= max_attempts:
                row['blocked_until'] = now + lockout
            login_state['attempts'][key] = row

    def _clear_login_failures(key):
        with login_state['lock']:
            login_state['attempts'].pop(key, None)
    # â”€â”€ Auth â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    @app.route('/hdc/login', methods=['GET', 'POST'])
    def hdc_login():
        if current_user.is_authenticated:
            return redirect(url_for('hdc_dashboard'))
        if request.method == 'POST':
            login_key = _login_key()
            if _login_blocked(login_key):
                flash('Too many failed login attempts. Please try again later.', 'danger')
                return render_template('auth/login.html')
            u = HDCUser.query.filter_by(username=request.form.get('username','').strip()).first()
            if u and check_password_hash(u.password_hash, request.form.get('password','')):
                _clear_login_failures(login_key)
                login_user(u)
                log_action(u, 'login', f'User {u.username} logged in.', 'session', u.id)
                _record_user_activity(
                    event_type='login',
                    entity_type='session',
                    entity_id=str(u.id),
                    summary=f'User login: {u.username}',
                    changed={},
                    force_commit=True
                )
                return redirect(url_for('hdc_dashboard'))
            _record_login_failure(login_key)
            flash('Invalid username or password.', 'danger')
        return render_template('auth/login.html')


    @app.route('/hdc/logout', methods=['POST'])
    @login_required
    def hdc_logout():
        log_action(current_user, 'logout', f'User {current_user.username} logged out.', 'session', current_user.id)
        _record_user_activity(
            event_type='logout',
            entity_type='session',
            entity_id=str(getattr(current_user, 'id', '') or ''),
            summary=f'User logout: {getattr(current_user, "username", "unknown")}',
            changed={},
            force_commit=True
        )
        logout_user()
        return redirect(url_for('hdc_login'))


    @app.route('/')
    def hdc_root():
        return redirect(url_for('hdc_dashboard'))
