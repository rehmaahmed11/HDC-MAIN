"""HDC services.audit — moved verbatim from hdc_erp.py.

See MODULARIZATION_PLAN.md for the module map.
"""

import json
from contextlib import contextmanager
from datetime import date, datetime

from flask import has_request_context, request
from flask_login import current_user
from sqlalchemy import event, inspect as sa_inspect

from hdc.extensions import db
from hdc.models.auth import ActivityLog, UserActivity
from hdc.models.projects import Project, Stage
from hdc.models.workforce import Worker
from hdc.utils.dates import _pkt_now_naive
from hdc.utils.format import _flt

_AUDIT_EXCLUDE_TABLES = {
    'hdc_user_activity',
    'hdc_runtime_flag'
}


_AUDIT_INTERNAL_TABLES = {
    'hdc_attendance_day',
    'hdc_labour_ledger'
}


_AUDIT_NOISY_UPDATE_FIELDS = {
    'hdc_time_entry': {'attendance_id', 'check_in', 'check_out', 'overtime', 'wage_calculated', 'activity_at'}
}


@contextmanager
def _audit_paused():
    depth = int(db.session.info.get('_audit_pause_depth', 0) or 0)
    db.session.info['_audit_pause_depth'] = depth + 1
    db.session.info['_audit_paused'] = True
    try:
        yield
    finally:
        new_depth = max(0, int(db.session.info.get('_audit_pause_depth', 1)) - 1)
        db.session.info['_audit_pause_depth'] = new_depth
        if new_depth == 0:
            db.session.info.pop('_audit_paused', None)


def _audit_actor_snapshot():
    if has_request_context() and getattr(current_user, 'is_authenticated', False):
        try:
            return int(current_user.id), str(current_user.username or '')
        except Exception:
            return None, 'system'
    return None, 'system'


def _audit_path():
    if has_request_context():
        try:
            return str(request.path or '')
        except Exception:
            return ''
    return ''


def _audit_repr(v):
    if isinstance(v, (datetime, date)):
        txt = v.isoformat(sep=' ') if isinstance(v, datetime) else v.isoformat()
    elif v is None:
        txt = ''
    else:
        txt = str(v)
    txt = txt.strip()
    if len(txt) > 80:
        txt = txt[:77] + '...'
    return txt


def _audit_entity_id(obj):
    val = getattr(obj, 'id', None)
    if val is None:
        return ''
    return str(val)


def _audit_entity_type(obj):
    return str(getattr(obj, '__tablename__', obj.__class__.__name__) or obj.__class__.__name__)


def _audit_change_map(obj):
    changed = {}
    try:
        insp = sa_inspect(obj)
        for attr in insp.mapper.column_attrs:
            key = attr.key
            hist = insp.attrs[key].history
            if not hist.has_changes():
                continue
            old_val = hist.deleted[0] if hist.deleted else None
            new_val = hist.added[0] if hist.added else getattr(obj, key, None)
            changed[key] = {'old': _audit_repr(old_val), 'new': _audit_repr(new_val)}
    except Exception:
        return {}
    return changed


def _audit_summary(event_type, entity_type, entity_id, changed=None):
    prefix = f"{event_type.title()} {entity_type}"
    if entity_id:
        prefix += f" #{entity_id}"
    changed = changed or {}
    if not changed:
        return prefix
    keys = list(changed.keys())[:6]
    suffix = ', '.join(keys)
    if len(changed.keys()) > 6:
        suffix += ', ...'
    return f"{prefix} ({suffix})"


def _audit_name_by_id(model_cls, obj_id, fallback='-'):
    try:
        if obj_id in (None, ''):
            return fallback
        rid = int(obj_id)
        row = db.session.get(model_cls, rid)
        if not row:
            return fallback
        return str(getattr(row, 'name', None) or getattr(row, 'username', None) or f'#{rid}')
    except Exception:
        return fallback


def _audit_humanize_timeentry(obj, event_type, changed):
    worker_name = _audit_name_by_id(Worker, getattr(obj, 'worker_id', None), fallback=f"Worker #{getattr(obj, 'worker_id', '')}")
    project_name = _audit_name_by_id(Project, getattr(obj, 'project_id', None), fallback='-')
    stage_name = _audit_name_by_id(Stage, getattr(obj, 'stage_id', None), fallback='-')
    hours = _flt(getattr(obj, 'hours', 0.0), 0.0)
    ot = _flt(getattr(obj, 'overtime', 0.0), 0.0)
    work_date = ''
    try:
        work_date = obj.check_in.date().isoformat() if getattr(obj, 'check_in', None) else ''
    except Exception:
        work_date = ''

    def _fmt_hours(v):
        try:
            return f"{float(_flt(v, 0.0)):.2f} hrs"
        except Exception:
            return f"{v}"

    actor = 'User'
    try:
        if has_request_context() and getattr(current_user, 'is_authenticated', False):
            actor = str(current_user.username or 'User').title()
    except Exception:
        pass

    if event_type == 'create':
        payload = {
            'worker': worker_name,
            'project': project_name,
            'stage': stage_name,
            'hours': f'{hours:.2f}',
            'overtime': f'{ot:.2f}',
            'date': work_date
        }
        summary = f"{actor} created attendance for {worker_name}: {project_name} -> {stage_name}, {_fmt_hours(hours)}"
        if ot > 0:
            summary += f" (OT {_fmt_hours(ot)})"
        if work_date:
            summary += f" on {work_date}"
        return summary, payload

    if event_type == 'update':
        parts = []
        if 'project_id' in changed:
            old_id = changed['project_id'].get('old')
            new_id = changed['project_id'].get('new')
            parts.append(f"project {_audit_name_by_id(Project, old_id, '-')} -> {_audit_name_by_id(Project, new_id, '-')}")
        if 'stage_id' in changed:
            old_id = changed['stage_id'].get('old')
            new_id = changed['stage_id'].get('new')
            parts.append(f"stage {_audit_name_by_id(Stage, old_id, '-')} -> {_audit_name_by_id(Stage, new_id, '-')}")
        if 'hours' in changed:
            parts.append(f"hours {_fmt_hours(changed['hours'].get('old', '0'))} -> {_fmt_hours(changed['hours'].get('new', '0'))}")
        if 'overtime' in changed:
            parts.append(f"overtime {_fmt_hours(changed['overtime'].get('old', '0'))} -> {_fmt_hours(changed['overtime'].get('new', '0'))}")
        if 'is_void' in changed:
            parts.append(f"status {'Voided' if str(changed['is_void'].get('new', '')).strip() in ('1', 'true', 'True') else 'Active'}")
        if not parts:
            parts.append('auto sync update')
        summary = f"{actor} updated attendance of {worker_name}: " + '; '.join(parts)
        if work_date:
            summary += f" | date {work_date}"
        payload = {'worker': worker_name, 'date': work_date, 'changes': changed}
        return summary, payload

    return _audit_summary(event_type, 'hdc_time_entry', _audit_entity_id(obj), changed), changed


def _audit_humanize_attendance_mark(obj, event_type, changed):
    worker_name = _audit_name_by_id(Worker, getattr(obj, 'worker_id', None), fallback=f"Worker #{getattr(obj, 'worker_id', '')}")
    status = str(getattr(obj, 'status', '') or '').strip().title() or '-'
    mark_date = ''
    try:
        mark_date = obj.date.isoformat() if getattr(obj, 'date', None) else ''
    except Exception:
        mark_date = ''

    if event_type == 'create':
        payload = {'worker': worker_name, 'status': status, 'date': mark_date}
        return f"Attendance status set: {worker_name} = {status}" + (f" on {mark_date}" if mark_date else ''), payload

    if event_type == 'update':
        old_status = (changed.get('status') or {}).get('old', '')
        new_status = (changed.get('status') or {}).get('new', '')
        if old_status or new_status:
            txt = f"Attendance status changed: {worker_name} {str(old_status).title() or '-'} -> {str(new_status).title() or '-'}"
        else:
            txt = f"Attendance mark updated: {worker_name}"
        if mark_date:
            txt += f" on {mark_date}"
        return txt, {'worker': worker_name, 'date': mark_date, 'changes': changed}

    return _audit_summary(event_type, 'hdc_attendance_mark', _audit_entity_id(obj), changed), changed


def _audit_custom_summary_payload(obj, event_type, changed):
    entity_type = _audit_entity_type(obj)
    if entity_type == 'hdc_time_entry':
        return _audit_humanize_timeentry(obj, event_type, changed or {})
    if entity_type == 'hdc_attendance_mark':
        return _audit_humanize_attendance_mark(obj, event_type, changed or {})
    return _audit_summary(event_type, entity_type, _audit_entity_id(obj), changed), changed


def _audit_is_noise_event(event_type, entity_type, changed):
    if entity_type in _AUDIT_INTERNAL_TABLES:
        return True
    if event_type != 'update':
        return False
    keys = set((changed or {}).keys())
    if not keys:
        return True
    noisy_fields = _AUDIT_NOISY_UPDATE_FIELDS.get(entity_type, set())
    if noisy_fields and keys.issubset(noisy_fields):
        return True
    return False


def _record_user_activity(event_type, entity_type, entity_id='', summary='', changed=None, force_commit=False):
    if db.session.info.get('_audit_paused'):
        return None
    try:
        uid, uname = _audit_actor_snapshot()
        row = UserActivity(
            user_id=uid,
            username=uname or 'system',
            event_type=str(event_type or 'system')[:20],
            entity_type=str(entity_type or 'unknown')[:80],
            entity_id=str(entity_id or '')[:80],
            changed_fields=json.dumps(changed or {}, ensure_ascii=False),
            summary=(summary or _audit_summary(event_type, entity_type, entity_id, changed))[:500],
            request_path=_audit_path()[:255]
        )
        db.session.info['_audit_skip'] = True
        db.session.add(row)
        if force_commit:
            db.session.commit()
        return row
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
        return None
    finally:
        db.session.info.pop('_audit_skip', None)


def _capture_user_activity_after_flush(session_obj, flush_context):
    if session_obj.info.get('_audit_paused') or session_obj.info.get('_audit_skip') or session_obj.info.get('_audit_reentry'):
        return
    uid, uname = _audit_actor_snapshot()
    path = _audit_path()
    rows = []

    def _track(obj, event_type):
        entity_type = _audit_entity_type(obj)
        if entity_type in _AUDIT_EXCLUDE_TABLES:
            return
        entity_id = _audit_entity_id(obj)
        changed = {}
        if event_type == 'update':
            changed = _audit_change_map(obj)
            if not changed:
                return
        if _audit_is_noise_event(event_type, entity_type, changed):
            return
        summary_txt, payload = _audit_custom_summary_payload(obj, event_type, changed)
        row = UserActivity(
            user_id=uid,
            username=(uname or 'system')[:80],
            event_type=event_type,
            entity_type=entity_type[:80],
            entity_id=entity_id[:80],
            changed_fields=json.dumps(payload or changed, ensure_ascii=False),
            summary=(summary_txt or _audit_summary(event_type, entity_type, entity_id, changed))[:500],
            request_path=path[:255]
        )
        rows.append(row)

    try:
        for obj in list(session_obj.new):
            _track(obj, 'create')
        for obj in list(session_obj.dirty):
            if obj in session_obj.new or obj in session_obj.deleted:
                continue
            try:
                if not session_obj.is_modified(obj, include_collections=False):
                    continue
            except Exception:
                pass
            _track(obj, 'update')
        for obj in list(session_obj.deleted):
            _track(obj, 'delete')

        if rows:
            session_obj.info['_audit_reentry'] = True
            for row in rows:
                session_obj.add(row)
    finally:
        session_obj.info.pop('_audit_reentry', None)


def log_action(user, action_type, description, entity_type, entity_id=''):
    try:
        uname = ''
        uid = None
        if user is not None:
            uid = getattr(user, 'id', None)
            uname = str(getattr(user, 'username', '') or '').strip()
        if not uname and has_request_context():
            uname = str(getattr(current_user, 'username', '') or '').strip()
            uid = uid or getattr(current_user, 'id', None)
        if not uname:
            uname = 'system'
        row = ActivityLog(
            user_id=uid,
            username=uname,
            action_type=(action_type or 'action')[:40],
            description=(description or '-')[:4000],
            entity_type=(entity_type or 'unknown')[:80],
            entity_id=str(entity_id or '')[:80],
            created_at=_pkt_now_naive()
        )
        db.session.add(row)
    except Exception:
        pass




_AUDIT_EVENTS_REGISTERED = False


def register_audit_events():
    """Attach the audit after_flush listener (called by the app factory)."""
    global _AUDIT_EVENTS_REGISTERED
    if _AUDIT_EVENTS_REGISTERED:
        return
    event.listen(db.session.__class__, "after_flush",
                 _capture_user_activity_after_flush)
    _AUDIT_EVENTS_REGISTERED = True
