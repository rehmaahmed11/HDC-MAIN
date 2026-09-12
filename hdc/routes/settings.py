"""HDC routes: Settings, backups, maintenance and misc APIs.

Moved verbatim from hdc_erp.py; each handler keeps its
original @app.route decorator and endpoint name.
"""

import os
import tempfile

from flask import abort, current_app, flash, redirect, render_template, request, send_file, url_for
from flask_login import login_required, logout_user

from hdc.config import _BACKUP_DIR
from hdc.core.admin import _WIPE_TARGETS, _restore_from_backup_zip, _restore_from_paths, _run_admin_maintenance, _wipe_selected_targets
from hdc.extensions import _admin_only, db
from hdc.services.accounts import _run_accounts_backfill
from hdc.services.backups import _backup_filename, _cleanup_backup_temp_artifacts, _create_backup_zip, _list_backups, _prune_saved_backups

def register(app):
    """Register Settings, backups, maintenance and misc APIs."""
    @app.route('/hdc/settings', methods=['GET', 'POST'])
    @login_required
    def hdc_settings():
        if _admin_only(): return redirect(url_for('hdc_dashboard'))
        if request.method == 'POST':
            action = (request.form.get('action') or '').strip()
            try:
                if action == 'create_backup':
                    name = _backup_filename()
                    dst = os.path.join(_BACKUP_DIR, name)
                    _create_backup_zip(dst)
                    _cleanup_backup_temp_artifacts()
                    flash(f'Backup created: {name} (contains DB + XLSX export).', 'success')
                    return redirect(url_for('hdc_settings'))

                if action == 'prune_backups':
                    keep_latest = request.form.get('keep_latest', type=int)
                    info = _prune_saved_backups(keep_latest=(keep_latest if keep_latest is not None else 10))
                    cleaned = _cleanup_backup_temp_artifacts()
                    flash(
                        f'Backup cleanup complete. Kept latest {info["keep"]}, deleted {info["deleted"]} backup(s), '
                        f'errors {info["errors"]}. Temp cleaned: {cleaned["files"]} file(s), {cleaned["dirs"]} folder(s).',
                        ('warning' if (info['errors'] or 0) > 0 else 'success')
                    )
                    return redirect(url_for('hdc_settings'))

                if action == 'run_maintenance':
                    pid = request.form.get('project_id', type=int)
                    stats = _run_admin_maintenance(project_id=pid or None)
                    flash(
                        f'Maintenance completed. Subcontract link updates: {stats["subcontract_link_updates"]}, workers reconciled: {stats["workers_processed"]}.',
                        'success'
                    )
                    return redirect(url_for('hdc_settings'))

                if action == 'accounts_backfill':
                    stats = _run_accounts_backfill()
                    flash(
                        f'Accounts backfill complete. Created rows: {stats.get("created", 0)}, '
                        f'Skipped: {stats.get("skipped", 0)}, Errors: {stats.get("errors", 0)}.',
                        ('warning' if (stats.get('errors', 0) or 0) > 0 else 'success')
                    )
                    return redirect(url_for('hdc_settings'))

                if action == 'restore_saved':
                    name = os.path.basename((request.form.get('backup_name') or '').strip())
                    if not name:
                        flash('Please select a backup to restore.', 'warning')
                        return redirect(url_for('hdc_settings'))
                    src = os.path.join(_BACKUP_DIR, name)
                    if not os.path.exists(src):
                        flash('Selected backup file not found.', 'danger')
                        return redirect(url_for('hdc_settings'))
                    _restore_from_backup_zip(src)
                    logout_user()
                    flash('Backup restored successfully. Please login again.', 'success')
                    return redirect(url_for('hdc_login'))

                if action == 'delete_backup':
                    name = os.path.basename((request.form.get('backup_name') or '').strip())
                    if (not name) or (not name.lower().endswith('.zip')):
                        flash('Please select a valid backup file to delete.', 'warning')
                        return redirect(url_for('hdc_settings'))
                    src = os.path.join(_BACKUP_DIR, name)
                    if not os.path.exists(src):
                        flash('Selected backup file not found.', 'danger')
                        return redirect(url_for('hdc_settings'))
                    try:
                        os.remove(src)
                        flash(f'Backup deleted: {name}', 'success')
                    except Exception as ex_rm:
                        flash(f'Unable to delete backup: {ex_rm}', 'danger')
                    return redirect(url_for('hdc_settings'))

                if action == 'restore_upload':
                    up = request.files.get('backup_file')
                    if not up or not up.filename:
                        flash('Please choose a backup file (.zip or .db).', 'warning')
                        return redirect(url_for('hdc_settings'))
                    ext = os.path.splitext(up.filename)[1].lower()
                    with tempfile.TemporaryDirectory(dir=_BACKUP_DIR) as tmpdir:
                        upload_path = os.path.join(tmpdir, 'uploaded' + ext)
                        up.save(upload_path)
                        if ext == '.zip':
                            _restore_from_backup_zip(upload_path)
                        elif ext == '.db':
                            _restore_from_paths(upload_path)
                        else:
                            flash('Unsupported file type. Use .zip or .db', 'danger')
                            return redirect(url_for('hdc_settings'))
                    logout_user()
                    flash('Backup restored successfully. Please login again.', 'success')
                    return redirect(url_for('hdc_login'))

                if action == 'wipe_data':
                    selected = request.form.getlist('wipe_targets')
                    if 'all' in selected:
                        selected = list(_WIPE_TARGETS.keys())
                    selected = [k for k in selected if k in _WIPE_TARGETS]
                    confirm_phrase = (request.form.get('wipe_confirm_text') or '').strip().upper()
                    confirm_ok = request.form.get('wipe_confirm_check') == '1'
                    if not selected:
                        flash('Select at least one data group to wipe.', 'warning')
                        return redirect(url_for('hdc_settings'))
                    if confirm_phrase != 'WIPE' or not confirm_ok:
                        flash('Wipe cancelled. Tick confirmation and type WIPE exactly.', 'danger')
                        return redirect(url_for('hdc_settings'))
                    users_wiped = 'users' in selected
                    info = _wipe_selected_targets(selected)
                    flash(
                        f'Wipe completed. Groups: {len(info["targets"])}, tables cleared: {info["tables"]}.',
                        'success'
                    )
                    if users_wiped:
                        logout_user()
                        flash('Users were wiped. Login with bootstrap admin credentials.', 'warning')
                        return redirect(url_for('hdc_login'))
                    return redirect(url_for('hdc_settings'))
            except Exception as ex:
                flash(f'Settings operation failed: {ex}', 'danger')
                return redirect(url_for('hdc_settings'))
        return render_template('settings/settings.html', backups=_list_backups(), wipe_targets=_WIPE_TARGETS)


    @app.route('/hdc/settings/backup/<path:filename>/download')
    @login_required
    def hdc_settings_backup_download(filename):
        if _admin_only(): return redirect(url_for('hdc_dashboard'))
        safe_name = os.path.basename(filename or '')
        if not safe_name.lower().endswith('.zip'):
            abort(404)
        p = os.path.join(_BACKUP_DIR, safe_name)
        if not os.path.exists(p):
            abort(404)
        return send_file(p, as_attachment=True, download_name=safe_name)


    @app.route('/hdc/admin/maintenance/run', methods=['POST'])
    @login_required
    def hdc_admin_maintenance_run():
        if _admin_only():
            return redirect(url_for('hdc_dashboard'))
        pid = request.form.get('project_id', type=int)
        try:
            stats = _run_admin_maintenance(project_id=pid or None)
            flash(
                f'Maintenance completed. Subcontract link updates: {stats["subcontract_link_updates"]}, workers reconciled: {stats["workers_processed"]}.',
                'success'
            )
        except Exception as ex:
            db.session.rollback()
            current_app.logger.error('Admin maintenance failed: %s', ex)
            flash(f'Maintenance failed: {ex}', 'danger')
        return redirect(request.referrer or url_for('hdc_settings'))
