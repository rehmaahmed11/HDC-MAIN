"""HDC routes: Login, logout and the root redirect.

Moved verbatim from hdc_erp.py; each handler keeps its
original @app.route decorator and endpoint name.
"""

from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user
from werkzeug.security import check_password_hash

from hdc.models.auth import HDCUser
from hdc.services.audit import _record_user_activity, log_action

def register(app):
    """Register Login, logout and the root redirect."""
    # â”€â”€ Auth â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    @app.route('/hdc/login', methods=['GET', 'POST'])
    def hdc_login():
        if current_user.is_authenticated:
            return redirect(url_for('hdc_dashboard'))
        if request.method == 'POST':
            u = HDCUser.query.filter_by(username=request.form.get('username','').strip()).first()
            if u and check_password_hash(u.password_hash, request.form.get('password','')):
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
