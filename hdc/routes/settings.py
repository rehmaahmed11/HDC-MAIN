"""HDC routes: Settings, backups, maintenance and misc APIs.

Moved verbatim from hdc_erp.py; each handler keeps its
original @app.route decorator and endpoint name.
"""

import os
import tempfile

from flask import abort, current_app, flash, redirect, render_template, request, send_file, url_for
from flask_login import login_required, logout_user

from hdc.config import get_runtime_settings
from hdc.core.admin import _WIPE_TARGETS, _restore_from_backup_zip, _restore_from_paths, _run_admin_maintenance, _wipe_selected_targets
from hdc.extensions import _admin_only, db
from hdc.services.accounts import _run_accounts_backfill
from hdc.services.backups import (
    _backup_filename,
    _backup_health,
    _cleanup_backup_temp_artifacts,
    _create_backup_zip,
    _create_preflight_backup,
    _list_backups,
    _prune_saved_backups_policy,
    _verify_backup_zip,
)

def _flash_file_restore(report):
    """Tell the operator what happened to photos/drawings after a restore."""
    if not isinstance(report, dict):
        return
    copied = report.get('copied') or 0
    errors = report.get('errors') or []
    if copied:
        flash(f'Restored {copied} operator file(s) (tool photos, stage drawings).', 'info')
    for message in errors:
        flash(f'Operator files: {message}', 'warning')


def _safety_snapshot(reason):
    """Take — and report on — an automatic backup before something destructive.

    Never blocks the operation it guards: if the snapshot fails the operator
    is warned loudly and can then decide, rather than being locked out of a
    restore because the backup machinery is the thing that is broken.
    """
    try:
        info = _create_preflight_backup(reason)
    except Exception as exc:                     # pragma: no cover - defensive
        info = {'ok': False, 'name': None, 'error': str(exc)}
    if info.get('ok'):
        flash(f"Safety backup saved before this change: {info['name']}", 'info')
    else:
        flash('WARNING: no safety backup could be taken before this change '
              f"({info.get('error') or 'unknown error'}). Continue only if you "
              'are certain.', 'warning')
    return info


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
                    dst = os.path.join(get_runtime_settings().backup_dir, name)
                    _create_backup_zip(dst)
                    _cleanup_backup_temp_artifacts()
                    flash(f'Backup created: {name} (contains DB + XLSX export).', 'success')
                    return redirect(url_for('hdc_settings'))

                if action == 'prune_backups':
                    keep_latest = request.form.get('keep_latest', type=int)
                    info = _prune_saved_backups_policy(
                        keep_recent=(keep_latest if keep_latest is not None else 10)
                    )
                    cleaned = _cleanup_backup_temp_artifacts()
                    flash(
                        f'Backup cleanup complete. Kept {info["kept"]} backup(s) '
                        f'(latest {info["keep_recent"]}, then one per day/week/month up to '
                        f'{info["keep_daily"]}/{info["keep_weekly"]}/{info["keep_monthly"]}), '
                        f'deleted {info["deleted"]}, errors {info["errors"]}. '
                        f'Temp cleaned: {cleaned["files"]} file(s), {cleaned["dirs"]} folder(s).',
                        ('warning' if (info['errors'] or 0) > 0 else 'success')
                    )
                    return redirect(url_for('hdc_settings'))

                if action == 'verify_backup':
                    name = os.path.basename((request.form.get('backup_name') or '').strip())
                    if not name:
                        flash('Please select a backup to verify.', 'warning')
                        return redirect(url_for('hdc_settings'))
                    src = os.path.join(get_runtime_settings().backup_dir, name)
                    if not os.path.exists(src):
                        flash('Selected backup file not found.', 'danger')
                        return redirect(url_for('hdc_settings'))
                    report = _verify_backup_zip(src)
                    if report['ok']:
                        extra = ' (older archive: no checksums recorded)' if report['legacy'] else ''
                        flash(f'Backup verified OK: {name} — {report["table_count"]} tables, '
                              f'{len(report["entries"])} file(s) checksummed'
                              f'{", " + report["created_at_pkt"] if report["created_at_pkt"] else ""}'
                              f'.{extra}', 'success')
                    else:
                        flash(f'Backup FAILED verification: {name} — ' + '; '.join(report['errors']), 'danger')
                    for warning in report['warnings']:
                        flash(f'{name}: {warning}', 'warning')
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
                    src = os.path.join(get_runtime_settings().backup_dir, name)
                    if not os.path.exists(src):
                        flash('Selected backup file not found.', 'danger')
                        return redirect(url_for('hdc_settings'))
                    _safety_snapshot('restore')
                    restored = _restore_from_backup_zip(src)
                    _flash_file_restore(restored)
                    logout_user()
                    flash('Backup restored successfully. Please login again.', 'success')
                    return redirect(url_for('hdc_login'))

                if action == 'delete_backup':
                    name = os.path.basename((request.form.get('backup_name') or '').strip())
                    if (not name) or (not name.lower().endswith('.zip')):
                        flash('Please select a valid backup file to delete.', 'warning')
                        return redirect(url_for('hdc_settings'))
                    src = os.path.join(get_runtime_settings().backup_dir, name)
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
                    if ext not in ('.zip', '.db'):
                        flash('Unsupported file type. Use .zip or .db', 'danger')
                        return redirect(url_for('hdc_settings'))
                    _safety_snapshot('restore')
                    with tempfile.TemporaryDirectory(dir=get_runtime_settings().backup_dir) as tmpdir:
                        upload_path = os.path.join(tmpdir, 'uploaded' + ext)
                        up.save(upload_path)
                        if ext == '.zip':
                            restored = _restore_from_backup_zip(upload_path)
                        else:
                            restored = _restore_from_paths(upload_path)
                    _flash_file_restore(restored)
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
                    _safety_snapshot('wipe')
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
        return render_template('settings/settings.html', backups=_list_backups(),
                               backup_health=_backup_health(), wipe_targets=_WIPE_TARGETS)


    @app.route('/hdc/settings/backup/<path:filename>/download')
    @login_required
    def hdc_settings_backup_download(filename):
        if _admin_only(): return redirect(url_for('hdc_dashboard'))
        safe_name = os.path.basename(filename or '')
        if not safe_name.lower().endswith('.zip'):
            abort(404)
        p = os.path.join(get_runtime_settings().backup_dir, safe_name)
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
