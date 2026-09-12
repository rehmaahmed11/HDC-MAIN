"""HDC routes: User management and event recorder.

Moved verbatim from hdc_erp.py; each handler keeps its
original @app.route decorator and endpoint name.
"""

from datetime import datetime

from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from werkzeug.security import generate_password_hash

from hdc.extensions import _admin_only, db
from hdc.models.auth import ActivityLog, HDCUser
from hdc.utils.format import _is_strong_password, _parse_date

def register(app):
    """Register User management and event recorder."""
    # â”€â”€ User Management â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    @app.route('/hdc/users', methods=['GET', 'POST'])
    @login_required
    def hdc_users():
        if _admin_only(): return redirect(url_for('hdc_dashboard'))
        if request.method == 'POST':
            action = request.form.get('action','add')
            if action == 'add':
                uname = request.form.get('username','').strip()
                raw_pwd = request.form.get('password', '')
                ok_pwd, pwd_msg = _is_strong_password(raw_pwd)
                if not uname:
                    flash('Username is required.', 'warning')
                elif HDCUser.query.filter_by(username=uname).first():
                    flash('Username already exists.', 'danger')
                elif not ok_pwd:
                    flash(pwd_msg, 'danger')
                else:
                    db.session.add(HDCUser(
                        username=uname,
                        password_hash=generate_password_hash(raw_pwd),
                        role=request.form.get('role','manager')))
                    db.session.commit()
                    flash(f'User "{uname}" created.', 'success')
            elif action == 'delete':
                uid = request.form.get('user_id', type=int)
                u   = HDCUser.query.get(uid)
                if u and u.id != current_user.id:
                    db.session.delete(u); db.session.commit()
                    flash('User deleted.', 'success')
                else:
                    flash("Cannot delete your own account.", 'warning')
            elif action == 'reset_password':
                uid = request.form.get('user_id', type=int)
                u   = HDCUser.query.get(uid)
                if u:
                    new_pwd = request.form.get('new_password', '')
                    ok_pwd, pwd_msg = _is_strong_password(new_pwd)
                    if not ok_pwd:
                        flash(pwd_msg, 'danger')
                    else:
                        u.password_hash = generate_password_hash(new_pwd)
                        db.session.commit()
                        flash(f'Password reset for {u.username}.', 'success')
            return redirect(url_for('hdc_users'))
        users = HDCUser.query.order_by(HDCUser.created_at).all()
        return render_template('users/users.html', users=users)


    @app.route('/hdc/event-recorder')
    @login_required
    def hdc_event_recorder():
        if _admin_only():
            return redirect(url_for('hdc_dashboard'))

        date_from_raw = (request.args.get('date_from') or '').strip()
        date_to_raw = (request.args.get('date_to') or '').strip()
        filter_user_id = request.args.get('user_id', type=int)
        filter_event = (request.args.get('event_type') or '').strip().lower()
        filter_entity = (request.args.get('entity_type') or '').strip().lower()
        show_internal = (request.args.get('show_internal') or '').strip().lower() in ('1', 'true', 'yes', 'on')

        q = ActivityLog.query
        if date_from_raw:
            d1 = _parse_date(date_from_raw, fallback=None)
            if d1:
                q = q.filter(ActivityLog.created_at >= datetime.combine(d1, datetime.min.time()))
        if date_to_raw:
            d2 = _parse_date(date_to_raw, fallback=None)
            if d2:
                q = q.filter(ActivityLog.created_at <= datetime.combine(d2, datetime.max.time()))
        if filter_user_id:
            q = q.filter(ActivityLog.user_id == filter_user_id)
        if filter_event:
            q = q.filter(ActivityLog.action_type == filter_event)
        if filter_entity:
            q = q.filter(ActivityLog.entity_type.ilike(f'%{filter_entity}%'))
        if not show_internal:
            q = q.filter(ActivityLog.entity_type != 'system')

        logs = q.order_by(ActivityLog.created_at.desc(), ActivityLog.id.desc()).limit(600).all()
        users = HDCUser.query.order_by(HDCUser.username.asc()).all()
        event_options = ['create', 'update', 'void', 'login', 'logout', 'payment', 'delivery', 'usage']

        return render_template('users/event_recorder.html',
            logs=logs,
            users=users,
            event_options=event_options,
            date_from=date_from_raw,
            date_to=date_to_raw,
            filter_user_id=filter_user_id,
            filter_event=filter_event,
            filter_entity=filter_entity,
            show_internal=show_internal
        )
