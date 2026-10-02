"""HDC routes: User management and event recorder.

Moved verbatim from hdc_erp.py; each handler keeps its
original @app.route decorator and endpoint name.
"""

from datetime import datetime
import json

from flask import abort, flash, jsonify, redirect, render_template, request, url_for
from sqlalchemy import String, cast, or_
from flask_login import current_user, login_required

from hdc.extensions import _admin_only, db
from hdc.models.auth import ActivityLog, HDCUser
from hdc.models.projects import Project, Stage
from hdc.services.audit import log_action
from hdc.services.password_vault import assign_password, vault_problem, viewable_password
from hdc.services.permissions import PAGE_CATALOG, PAGE_TREE, permission_editor_grants
from hdc.services.record_permissions import (record_catalog, record_grant_cards, record_label,
    record_models, record_permissions_from_form, parse_report_sections, report_section_page_grants)
from hdc.utils.format import _is_strong_password, _parse_date


def _private_json(payload, status=200):
    """JSON response that no browser or proxy may keep (it can carry a password)."""
    response = jsonify(payload)
    response.status_code = status
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, private'
    response.headers['Pragma'] = 'no-cache'
    return response


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

def _save_access_from_form(user, form):
    user.record_scope_enabled = form.get('record_scope_enabled') == '1'
    record_grants = (record_permissions_from_form(form)
                     if user.record_scope_enabled else None)
    user.record_permissions_json = (json.dumps(record_grants, separators=(',', ':'))
                                    if record_grants is not None else None)
    # Strict data mode must never silently inherit broad role/page defaults.
    custom = form.get('custom_permissions') == '1' or user.record_scope_enabled
    if custom:
        permissions = _permissions_from_form(form)
        # Cascade reporting sections keep their report pages reachable; the
        # page map is saved in sync so both gates agree after one save.
        if user.record_scope_enabled and record_grants:
            for page, flags in report_section_page_grants(record_grants).items():
                entry = permissions.setdefault(page, {'read': False, 'write': False})
                entry['read'] = entry['read'] or flags['read']
                entry['write'] = entry['write'] or flags['write']
                if entry['write']:
                    entry['read'] = True
        user.permissions_json = json.dumps(permissions, separators=(',', ':'))
    else:
        user.permissions_json = None
    user.stage_scope_enabled = form.get('stage_scope_enabled') == '1'
    if user.stage_scope_enabled:
        read_ids, write_ids = _stage_access_from_form(form)
        user.allowed_stage_ids_json = json.dumps(read_ids)
        user.write_stage_ids_json = json.dumps(write_ids)
    else:
        user.allowed_stage_ids_json = None
        user.write_stage_ids_json = None


def _cascade_options():
    """Dropdown data for the Project access cascade on the users page."""
    projects = Project.query.order_by(Project.project_code.asc(), Project.name.asc()).all()
    project_options = [
        {'id': p.id, 'label': f'{p.project_code} · {p.name}' if getattr(p, 'project_code', None) else p.name}
        for p in projects]
    stage_rows = db.session.query(Stage.id, Stage.project_id, Stage.name).order_by(
        Stage.name.asc(), Stage.id.asc()).all()
    stages_by_project = {}
    stage_projects = {}
    stage_labels = {}
    for stage_id, project_id, name in stage_rows:
        stage_projects[stage_id] = project_id
        stage_labels[stage_id] = name
        stages_by_project.setdefault(project_id, []).append({'id': stage_id, 'label': name})
    return project_options, stages_by_project, stage_projects, stage_labels


def _cascade_branches(user, cards, project_options, stages_by_project, stage_projects, stage_labels):
    """Rebuild project → stage → reporting branches from saved grants."""
    project_card = next((card for card in cards if card.get('id') == 'hdc_project'), None)
    stage_card = next((card for card in cards if card.get('id') == 'hdc_stage'), None)
    project_rows = {int(row['id']): row for row in (project_card['records'] if project_card else [])}
    stage_rows = {int(row['id']): row for row in (stage_card['records'] if stage_card else [])}
    sections = parse_report_sections(user)
    project_labels = {option['id']: option['label'] for option in project_options}
    used = (set(project_rows) | set(sections) |
            {stage_projects[sid] for sid in stage_rows if sid in stage_projects})
    branches = []
    ordered_pids = [pid for pid in (option['id'] for option in project_options) if pid in used]
    ordered_pids += sorted(pid for pid in used if pid not in project_labels)
    for pid in ordered_pids:
        project_row = project_rows.get(pid)
        section_map = sections.get(pid, {})
        known_stage_ids = [stage['id'] for stage in stages_by_project.get(pid, [])]
        granted_stage_ids = [sid for sid in stage_rows if stage_projects.get(sid) == pid]
        stage_ids = [sid for sid in known_stage_ids
                     if sid in granted_stage_ids or sid in section_map]
        stage_ids += sorted(sid for sid in granted_stage_ids if sid not in known_stage_ids)
        stage_entries = []
        for sid in stage_ids:
            stage_row = stage_rows.get(sid)
            stage_entries.append({
                'id': sid, 'label': stage_labels.get(sid, f'#{sid} (record no longer exists)'),
                'read': bool(stage_row and stage_row.get('read')),
                'write': bool(stage_row and stage_row.get('write')),
                'delete': bool(stage_row and stage_row.get('delete')),
                'sections': section_map.get(sid, {})})
        if 0 in section_map:
            stage_entries.append({'id': 0, 'label': '(Whole project)', 'read': False,
                                  'write': False, 'delete': False, 'sections': section_map[0]})
        branches.append({
            'id': pid, 'label': project_labels.get(pid, f'#{pid} (record no longer exists)'),
            'exists': pid in project_labels,
            'read': bool(project_row and project_row.get('read')),
            'write': bool(project_row and project_row.get('write')),
            'delete': bool(project_row and project_row.get('delete')),
            'stages': stage_entries})
    orphan_stages = sorted(sid for sid in stage_rows if sid not in stage_projects)
    if orphan_stages:
        branches.append({
            'id': None, 'label': 'Removed stages (no longer exist)', 'exists': False,
            'read': False, 'write': False, 'delete': False,
            'stages': [{'id': sid, 'label': stage_rows[sid].get('label') or f'#{sid}',
                        'read': bool(stage_rows[sid].get('read')),
                        'write': bool(stage_rows[sid].get('write')),
                        'delete': bool(stage_rows[sid].get('delete')),
                        'sections': {}} for sid in orphan_stages]})
    return branches


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
                    user = HDCUser(username=uname, role=role)
                    assign_password(user, raw_pwd)
                    _save_access_from_form(user, request.form)
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
            elif action == 'set_status':
                uid = request.form.get('user_id', type=int)
                user = db.session.get(HDCUser, uid) if uid else None
                requested_status = (request.form.get('status') or '').strip().lower()
                if not user:
                    flash('User not found.', 'danger')
                elif requested_status not in {'active', 'suspended'}:
                    flash('Choose a valid account status.', 'warning')
                elif user.id == current_user.id and requested_status == 'suspended':
                    flash('You cannot suspend your own account.', 'warning')
                else:
                    should_be_active = requested_status == 'active'
                    if user.is_active == should_be_active:
                        flash(f'{user.username} is already {requested_status}.', 'info')
                    elif (not should_be_active and (user.role or '').strip().lower() == 'admin' and
                          HDCUser.query.filter_by(role='admin', is_active=True).count() <= 1):
                        flash('The last active administrator cannot be suspended.', 'warning')
                    else:
                        user.is_active = should_be_active
                        # Invalidate already-issued sessions when suspending;
                        # they stay invalid even after later reactivation.
                        if not should_be_active:
                            user.auth_version = int(user.auth_version or 0) + 1
                        verb = 'activated' if should_be_active else 'suspended'
                        log_action(current_user, 'update',
                                   f'Account for {user.username} was {verb}.',
                                   'user_account', user.id)
                        db.session.commit()
                        flash(f'User "{user.username}" {verb}.', 'success')
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
                    _save_access_from_form(user, request.form)
                    db.session.commit()
                    if user.record_scope_enabled:
                        flash(f'Exact access saved for {user.username}. Only assigned pages and records are accessible.', 'success')
                    elif user.permissions_json:
                        flash(f'Access saved for {user.username}.', 'success')
                    elif user.stage_scope_enabled:
                        flash(f'Role defaults restored with a stage limit for {user.username}.', 'success')
                    else:
                        flash(f'Role defaults restored for {user.username}.', 'success')
            elif action == 'reset_password':
                uid = request.form.get('user_id', type=int)
                user = db.session.get(HDCUser, uid) if uid else None
                if not user:
                    flash('User not found.', 'danger')
                else:
                    new_pwd = request.form.get('new_password', '')
                    confirm_pwd = request.form.get('confirm_password', '')
                    ok_pwd, pwd_msg = _is_strong_password(new_pwd)
                    if not ok_pwd:
                        flash(pwd_msg, 'danger')
                    elif new_pwd != confirm_pwd:
                        flash('The new password and confirmation do not match.', 'danger')
                    else:
                        assign_password(user, new_pwd)
                        user.auth_version = int(user.auth_version or 0) + 1
                        log_action(current_user, 'update',
                                   f'Password reset for user {user.username}.',
                                   'user_account', user.id)
                        db.session.commit()
                        flash(f'Password reset for {user.username}. Share the new password securely.', 'success')
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
        catalog = record_catalog()
        record_cards_by_user = {user.id: record_grant_cards(user, catalog) for user in users}
        project_options, stages_by_project, stage_projects, stage_labels = _cascade_options()
        return render_template('users/users.html', users=users, stages=stages,
            password_vault_problem=vault_problem(),
            record_catalog=catalog,
            record_cards_by_user=record_cards_by_user,
            project_options=project_options,
            stages_by_project=stages_by_project,
            cascade_branches_by_user={
                user.id: _cascade_branches(user, record_cards_by_user[user.id], project_options,
                                           stages_by_project, stage_projects, stage_labels)
                for user in users},
            page_tree=PAGE_TREE,
            permissions_by_user={user.id: permission_editor_grants(user) for user in users},
            read_stage_ids_by_user=read_stage_ids_by_user,
            write_stage_ids_by_user=write_stage_ids_by_user)


    @app.route('/hdc/users/<int:user_id>/password', methods=['POST'])
    @login_required
    def hdc_user_password_reveal(user_id):
        """Show one account's current password to an administrator (JSON).

        POST-only so it needs the CSRF token and can never be triggered by a
        link, a prefetch or a crawler.  Each successful view is written to the
        event recorder *before* the password leaves the server; the log entry
        names who looked at whose account but never contains the password.
        """
        if (getattr(current_user, 'role', '') or '').strip().lower() != 'admin':
            return _private_json({'ok': False, 'message': 'Admin access required.'}, 403)
        user = db.session.get(HDCUser, user_id)
        if user is None:
            return _private_json({'ok': False, 'message': 'User not found.'}, 404)
        password = viewable_password(user)
        if password is None:
            problem = vault_problem()
            if problem:
                message = 'Password viewing is unavailable. ' + problem
            elif not user.password_vault:
                message = (f'No viewable copy is saved for {user.username}: the password was set before '
                           'password viewing existed. Set a new password to make it viewable.')
            else:
                message = (f'The saved copy for {user.username} no longer matches the current password. '
                           'Set a new password to make it viewable.')
            return _private_json({'ok': False, 'message': message}, 409)
        log_action(current_user, 'view', f'Password viewed for user {user.username}.',
                   'user_account', user.id)
        db.session.commit()
        return _private_json({'ok': True, 'password': password})


    @app.route('/hdc/users/access-data')
    @login_required
    def hdc_user_access_data():
        if _admin_only():
            abort(403)
        table = (request.args.get('resource') or '').strip()
        model = record_models().get(table)
        if model is None:
            return jsonify(ok=False, message='Unknown data type.'), 400
        search = (request.args.get('search') or '').strip()[:100]
        page = min(10000, max(1, request.args.get('page', type=int) or 1))
        query = model.query
        if search:
            term = search.removeprefix('#')
            fields = [cast(model.id, String).ilike('%' + term + '%')]
            for column in model.__table__.columns:
                if column.key in ('name', 'project_code', 'worker_code', 'staff_code',
                                  'subcontractor_code', 'description', 'notes', 'original_name',
                                  'date', 'check_in', 'project_id', 'stage_id', 'worker_id', 'staff_id'):
                    fields.append(cast(getattr(model, column.key), String).ilike('%' + term + '%'))
            query = query.filter(or_(*fields))
        rows = query.order_by(model.id.desc()).offset((page - 1) * 25).limit(26).all()
        return jsonify(ok=True, records=[{'id': str(row.id), 'label': record_label(row)} for row in rows[:25]],
                       has_more=len(rows) > 25, next_page=page + 1)


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
        event_options = ['create', 'update', 'void', 'login', 'logout', 'view', 'payment', 'delivery', 'usage']

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
