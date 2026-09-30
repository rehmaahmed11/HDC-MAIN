"""HDC routes: User management and event recorder.

Moved verbatim from hdc_erp.py; each handler keeps its
original @app.route decorator and endpoint name.
"""

from datetime import datetime
import json

from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from werkzeug.security import generate_password_hash

from hdc.extensions import _admin_only, db
from hdc.models.auth import ActivityLog, HDCUser
from hdc.models.projects import Project, Stage
from hdc.services.permissions import PAGE_CATALOG, PAGE_TREE, parse_permissions
from hdc.utils.format import _is_strong_password, _parse_date


def _permissions_from_form(form):
    permissions = {}
    for page in PAGE_CATALOG:
        read = form.get(f'read_{page["id"]}') == '1'
        write = read and form.get(f'write_{page["id"]}') == '1'
        permissions[page['id']] = {'read': read, 'write': write}
    return permissions


def _selected_stage_ids_from_json(raw):
    try:
        return {int(value) for value in json.loads(raw or '[]') if str(value).isdigit()}
    except (TypeError, ValueError):
        return set()


def _selected_stage_ids(form, field_name):
    values = form.getlist(field_name)
    result = set()
    for value in values:
        try:
            result.add(int(value))
        except (TypeError, ValueError):
            continue
    if result:
        valid = {row[0] for row in Stage.query.with_entities(Stage.id).filter(Stage.id.in_(result)).all()}
        result.intersection_update(valid)
    return sorted(result)


def _stage_access_from_form(form):
    write_ids = set(_selected_stage_ids(form, 'write_stage_ids'))
    read_ids = set(_selected_stage_ids(form, 'read_stage_ids')) | write_ids
    return sorted(read_ids), sorted(write_ids)

def register(app):
    """Register User management and event recorder."""
    # --- User Management -------------------------------------------------------
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
                    role = (request.form.get('role') or 'manager').strip().lower()
                    if role not in {'admin', 'manager', 'accountant', 'staff'}:
                        role = 'manager'
                    user = HDCUser(
                        username=uname,
                        password_hash=generate_password_hash(raw_pwd),
                        role=role)
                    if request.form.get('custom_permissions') == '1':
                        user.permissions_json = json.dumps(_permissions_from_form(request.form), separators=(',', ':'))
                    if request.form.get('stage_scope_enabled') == '1':
                        user.stage_scope_enabled = True
                        read_ids, write_ids = _stage_access_from_form(request.form)
                        user.allowed_stage_ids_json = json.dumps(read_ids)
                        user.write_stage_ids_json = json.dumps(write_ids)
                    db.session.add(user)
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
            elif action == 'configure_permissions':
                uid = request.form.get('user_id', type=int)
                user = db.session.get(HDCUser, uid) if uid else None
                if not user:
                    flash('User not found.', 'danger')
                else:
                    new_role = (request.form.get('role') or '').strip().lower()
                    if new_role in {'admin', 'manager', 'accountant', 'staff'}:
                        if user.id == current_user.id and new_role != (user.role or '').strip().lower():
                            flash('You cannot change your own role.', 'warning')
                        elif user.id != current_user.id:
                            user.role = new_role
                    if request.form.get('custom_permissions') != '1':
                        user.permissions_json = None
                        user.stage_scope_enabled = request.form.get('stage_scope_enabled') == '1'
                        if user.stage_scope_enabled:
                            read_ids, write_ids = _stage_access_from_form(request.form)
                            user.allowed_stage_ids_json = json.dumps(read_ids)
                            user.write_stage_ids_json = json.dumps(write_ids)
                        else:
                            user.allowed_stage_ids_json = None
                            user.write_stage_ids_json = None
                        db.session.commit()
                        if user.stage_scope_enabled:
                            flash(f'Role defaults restored with a stage limit for {user.username}.', 'success')
                        else:
                            flash(f'Role defaults restored for {user.username}.', 'success')
                    else:
                        user.permissions_json = json.dumps(_permissions_from_form(request.form), separators=(',', ':'))
                        user.stage_scope_enabled = request.form.get('stage_scope_enabled') == '1'
                        if user.stage_scope_enabled:
                            read_ids, write_ids = _stage_access_from_form(request.form)
                            user.allowed_stage_ids_json = json.dumps(read_ids)
                            user.write_stage_ids_json = json.dumps(write_ids)
                        else:
                            user.allowed_stage_ids_json = None
                            user.write_stage_ids_json = None
                        db.session.commit()
                        flash(f'Access saved for {user.username}.', 'success')
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
        stages = (db.session.query(Stage, Project)
                  .join(Project, Project.id == Stage.project_id)
                  .order_by(Project.name.asc(), Stage.name.asc(), Stage.id.asc()).all())
        read_stage_ids_by_user = {}
        write_stage_ids_by_user = {}
        for user in users:
            write_ids = _selected_stage_ids_from_json(user.write_stage_ids_json)
            read_stage_ids_by_user[user.id] = _selected_stage_ids_from_json(user.allowed_stage_ids_json) | write_ids
            write_stage_ids_by_user[user.id] = write_ids
        return render_template('users/users.html', users=users, stages=stages,
            page_tree=PAGE_TREE,
            permissions_by_user={user.id: parse_permissions(user) for user in users},
            read_stage_ids_by_user=read_stage_ids_by_user,
            write_stage_ids_by_user=write_stage_ids_by_user)


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
