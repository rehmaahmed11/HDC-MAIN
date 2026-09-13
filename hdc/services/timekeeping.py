"""HDC services.timekeeping — moved verbatim from hdc_erp.py.

See MODULARIZATION_PLAN.md for the module map.
"""

import json
from datetime import datetime, timedelta

from flask import current_app
from sqlalchemy import or_

from hdc.core.flags import _runtime_flag_get, _runtime_flag_set
from hdc.extensions import db
from hdc.models.projects import Project
from hdc.models.workforce import Attendance, AttendanceDay, AttendanceMark, LabourLedger, TimeEntry, Worker, WorkerRate
from hdc.services.ledger import _worker_tip_expenses
from hdc.utils.dates import _pkt_now_naive
from hdc.utils.format import _activity_at_for, _flt

def _parse_attendance_entries_payload(raw_payload):
    try:
        rows = json.loads(raw_payload or '[]')
    except Exception:
        return []
    if not isinstance(rows, list):
        return []
    items = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            pid = int(row.get('project_id') or 0)
            sid = int(row.get('stage_id') or 0)
        except Exception:
            pid = 0
            sid = 0
        qty = _flt(row.get('qty_sqft'), 0.0)
        items.append({
            'project_id': pid,
            'stage_id': sid,
            'hours': _flt(row.get('hours'), 0.0),
            # Performed quantity for per-sqft workers. Without this a per-sqft
            # worker is paid rate x 0 and earns nothing (LABOUR_AUDIT #5).
            'qty_sqft': max(0.0, qty),
        })
    return items


def _recalculate_attendance_day(worker_id, work_date):
    day_start = datetime.combine(work_date, datetime.min.time())
    day_end = datetime.combine(work_date, datetime.max.time())
    entries = (TimeEntry.query
               .filter(TimeEntry.worker_id == worker_id,
                       TimeEntry.is_void == False,
                       TimeEntry.check_in >= day_start,
                       TimeEntry.check_in <= day_end)
               .order_by(TimeEntry.check_in.asc(), TimeEntry.id.asc())
               .all())
    day_row = AttendanceDay.query.filter_by(worker_id=worker_id, date=work_date).first()
    if not entries:
        if day_row:
            day_row.total_hours = 0.0
            day_row.day_value = 0.0
            day_row.overtime_hours = 0.0
            day_row.entry_count = 0
            day_row.is_void = True
            day_row.updated_at = _pkt_now_naive()
        return {'total_hours': 0.0, 'day': 0.0, 'overtime': 0.0}

    if not day_row:
        day_row = AttendanceDay(worker_id=worker_id, date=work_date, is_void=False)
        db.session.add(day_row)
        db.session.flush()

    worker = Worker.query.get(worker_id)
    total_hours = 0.0
    regular_done = 0.0
    base_dt = datetime.combine(work_date, datetime.min.time())
    for te in entries:
        hours = max(0.0, _flt(te.hours, 0.0))
        remaining_regular = max(0.0, 8.0 - regular_done)
        regular_hours = min(hours, remaining_regular)
        overtime_hours = max(0.0, hours - regular_hours)

        # Link to the AttendanceDay summary in its own column. Writing this to
        # ``attendance_id`` used to masquerade as a legacy hdc_attendance link
        # and silently dropped that attendance wage from the worker's earnings
        # and from project/stage labour cost (LABOUR_AUDIT #6).
        te.attendance_day_id = day_row.id
        te.check_in = base_dt + timedelta(hours=total_hours)
        te.check_out = te.check_in + timedelta(hours=hours)
        te.overtime = overtime_hours
        te.wage_calculated = _calc_time_wage(worker, regular_hours, overtime_hours, te.qty_sqft or 0.0, work_date=work_date)
        te.activity_at = te.check_in
        _sync_work_ledger_for_time_entry(te)

        total_hours += hours
        regular_done += regular_hours

    total_overtime = max(0.0, total_hours - 8.0)
    day_row.total_hours = total_hours
    day_row.day_value = 1.0 if total_hours >= 8.0 else 0.0
    day_row.overtime_hours = total_overtime
    day_row.entry_count = len(entries)
    day_row.is_void = False
    day_row.updated_at = _pkt_now_naive()
    return {'total_hours': total_hours, 'day': day_row.day_value, 'overtime': total_overtime}


def _has_recent_duplicate(model, seconds=12, timestamp_field='created_at', **eq_fields):
    q = db.session.query(model)
    for key, val in eq_fields.items():
        q = q.filter(getattr(model, key) == val)
    if hasattr(model, 'is_void') and 'is_void' not in eq_fields:
        q = q.filter(getattr(model, 'is_void') == False)
    ts_col = getattr(model, timestamp_field, None)
    if ts_col is not None:
        q = q.filter(ts_col >= (_pkt_now_naive() - timedelta(seconds=seconds)))
    return q.first() is not None


def _reconcile_worker_tip_ledger(worker):
    tips = _worker_tip_expenses(worker)
    for exp in tips:
        # Primary identity check: every reconciled tip ledger row carries
        # the originating expense id in its notes as ``TIP_EXPENSE_ID:N``.
        # Use that as the duplicate guard instead of fuzzy
        # date/amount/project matching so we never duplicate a tip into
        # the wrong worker's ledger again.
        exp_tag = f'TIP_EXPENSE_ID:{exp.id}'
        # Tag must end the string, or be followed by a non-digit delimiter
        # so TIP_EXPENSE_ID:1 cannot accidentally match TIP_EXPENSE_ID:10.
        exp_tag_clauses = [
            LabourLedger.notes.ilike(f'%{exp_tag}'),
            LabourLedger.notes.ilike(f'%{exp_tag} %'),
            LabourLedger.notes.ilike(f'%{exp_tag}|%'),
            LabourLedger.notes.ilike(f'%{exp_tag},%'),
            LabourLedger.notes.ilike(f'%{exp_tag};%'),
            LabourLedger.notes.ilike(f'%{exp_tag}/%'),
            LabourLedger.notes.ilike(f'%{exp_tag}-%'),
        ]
        exists = (LabourLedger.query
                  .filter(
                      LabourLedger.worker_id == worker.id,
                      LabourLedger.entry_type == 'tip',
                      LabourLedger.is_void == False,
                      or_(*exp_tag_clauses)
                  )
                  .first())
        if exists:
            continue
        # Belt-and-suspenders (2026-04-25): also block if there is already an
        # active tip ledger row for this worker on the same date and amount,
        # even if it doesn't carry the TIP_EXPENSE_ID tag yet (legacy rows
        # written by the accounts payment flow before the bug fix). Without
        # this guard the reconciler would write a second tip row and the
        # worker's balance would silently go over-paid by the tip amount.
        legacy = (LabourLedger.query
                  .filter(
                      LabourLedger.worker_id == worker.id,
                      LabourLedger.entry_type == 'tip',
                      LabourLedger.is_void == False,
                      LabourLedger.date == exp.date,
                      LabourLedger.amount == float(exp.amount or 0.0),
                  )
                  .first())
        if legacy:
            # Backfill the tag on the legacy row so future reconciler runs
            # find it via the primary check above.
            existing_notes = (legacy.notes or '').strip()
            if f'TIP_EXPENSE_ID:{exp.id}' not in existing_notes:
                legacy.notes = (existing_notes + ' | ' if existing_notes else '') + f'TIP_EXPENSE_ID:{exp.id}'
            continue
        notes = (exp.remarks or '') + f' | TIP_EXPENSE_ID:{exp.id}'
        db.session.add(LabourLedger(
            worker_id=worker.id,
            entry_type='tip',
            amount=float(exp.amount or 0.0),
            date=exp.date,
            activity_at=exp.activity_at or _activity_at_for(exp.date),
            project_id=exp.project_id,
            stage_id=exp.stage_id,
            notes=notes
        ))


def _worker_rate_on(worker, on_date=None):
    """
    Resolve worker wage type/rate effective on the provided date.
    Falls back to current worker fields if no history record matches.
    """
    if on_date:
        wr = (WorkerRate.query
              .filter(WorkerRate.worker_id == worker.id,
                      WorkerRate.effective_from <= on_date)
              .order_by(WorkerRate.effective_from.desc(), WorkerRate.id.desc())
              .first())
        if wr:
            return (wr.wage_type or worker.wage_type), float(wr.rate or 0.0)

    # Fallback to current profile values
    wtype = worker.wage_type or 'daily'
    if wtype == 'hourly':
        return wtype, float(worker.hourly_rate or worker.hourly_wage or 0.0)
    if wtype == 'per_sqft':
        return wtype, float(worker.rate_per_sqft or 0.0)
    return 'daily', float(worker.base_daily_wage or 0.0)


def _calc_time_wage(worker, hours, overtime, qty_sqft=0.0, work_date=None):
    hours = _flt(hours, 0.0)
    overtime = _flt(overtime, 0.0)
    qty_sqft = _flt(qty_sqft, 0.0)
    wtype, base_rate = _worker_rate_on(worker, work_date)
    if wtype == 'per_sqft':
        return max(0.0, base_rate * qty_sqft)
    if wtype == 'hourly':
        # Overtime is paid at straight time (same rate as regular hours), which
        # is the company's practice -- but it must be paid at all. Previously
        # the OT hours were dropped entirely for hourly workers
        # (LABOUR_AUDIT #9).
        return max(0.0, base_rate * (hours + overtime))
    full_day = min(1.0, hours / 8.0) if hours > 0 else 0.0
    hourly = (base_rate / 8.0) if base_rate > 0 else 0.0
    return max(0.0, base_rate * full_day + (overtime or 0) * hourly)


def _sync_work_ledger_for_time_entry(te):
    if not te:
        return
    row = LabourLedger.query.filter_by(time_entry_id=te.id, entry_type='work').first()
    if te.is_void:
        if row and not row.is_void:
            row.is_void = True
            row.void_reason = te.void_reason or 'Time entry voided'
            row.voided_at = te.voided_at or _pkt_now_naive()
        return
    if not row:
        row = LabourLedger(
            worker_id=te.worker_id,
            entry_type='work',
            time_entry_id=te.id
        )
        db.session.add(row)
    row.worker_id = te.worker_id
    row.project_id = te.project_id
    row.stage_id = te.stage_id
    row.amount = float(te.wage_calculated or 0.0)
    row.date = te.check_in.date()
    row.activity_at = te.check_in
    row.notes = f'Time entry {te.check_in.date()}'
    row.is_void = False
    row.void_reason = None
    row.voided_at = None


def _void_orphan_work_ledgers_for_time_entry(te, reason='Time entry voided'):
    if not te:
        return
    cands = (LabourLedger.query
             .filter(
                 LabourLedger.worker_id == te.worker_id,
                 LabourLedger.entry_type == 'work',
                 LabourLedger.is_void == False,
                 LabourLedger.time_entry_id.is_(None),
                 LabourLedger.date == te.check_in.date(),
                 LabourLedger.project_id == te.project_id
             )
             .all())
    target_amt = float(te.wage_calculated or 0.0)
    for row in cands:
        if abs(float(row.amount or 0.0) - target_amt) <= 0.01:
            row.is_void = True
            row.void_reason = reason
            row.voided_at = _pkt_now_naive()


def _repair_worker_work_ledger_links(worker_id):
    rows = (LabourLedger.query
            .filter(
                LabourLedger.worker_id == worker_id,
                LabourLedger.entry_type == 'work',
                LabourLedger.is_void == False,
                LabourLedger.time_entry_id.is_(None)
            )
            .all())
    for row in rows:
        match = (TimeEntry.query
                 .filter(
                     TimeEntry.worker_id == row.worker_id,
                     TimeEntry.is_void == False,
                     TimeEntry.project_id == row.project_id,
                     TimeEntry.check_in >= datetime.combine(row.date, datetime.min.time()),
                     TimeEntry.check_in <= datetime.combine(row.date, datetime.max.time())
                 )
                 .order_by(TimeEntry.check_in.asc())
                 .all())
        linked = None
        for te in match:
            if abs(float(te.wage_calculated or 0.0) - float(row.amount or 0.0)) <= 0.01:
                linked = te
                break
        if linked:
            row.time_entry_id = linked.id
            row.stage_id = linked.stage_id
        else:
            row.is_void = True
            row.void_reason = 'Auto-void orphaned work ledger (missing active time entry)'
            row.voided_at = _pkt_now_naive()


def _reconcile_worker_time_entries(worker_id):
    day_entries = (TimeEntry.query
                   .filter(TimeEntry.worker_id == worker_id, TimeEntry.is_void == False)
                   .order_by(TimeEntry.check_in.asc(), TimeEntry.id.asc())
                   .all())
    # Merge historical split rows where OT was mistakenly saved as a second row
    # on the same project/stage immediately after base duty.
    for i in range(len(day_entries) - 1):
        base = day_entries[i]
        ot = day_entries[i + 1]
        if base.is_void or ot.is_void:
            continue
        if base.project_id != ot.project_id or base.stage_id != ot.stage_id:
            continue
        if base.check_in.date() != ot.check_in.date():
            continue
        if ot.check_in != base.check_out:
            continue
        if float(ot.overtime or 0.0) <= 0 or abs(float(ot.hours or 0.0) - float(ot.overtime or 0.0)) > 0.01:
            continue
        base.check_out = ot.check_out
        base.hours = float(base.hours or 0.0) + float(ot.hours or 0.0)
        base.overtime = float(base.overtime or 0.0) + float(ot.overtime or 0.0)
        base.wage_calculated = float(base.wage_calculated or 0.0) + float(ot.wage_calculated or 0.0)
        _sync_work_ledger_for_time_entry(base)
        ot.is_void = True
        ot.void_reason = 'Auto-merged with same project/stage base entry'
        ot.voided_at = _pkt_now_naive()
        _sync_work_ledger_for_time_entry(ot)
        _void_orphan_work_ledgers_for_time_entry(ot, reason=ot.void_reason)

    entries = (TimeEntry.query
               .filter(TimeEntry.worker_id == worker_id, TimeEntry.is_void == False)
               .order_by(TimeEntry.check_in.asc(), TimeEntry.id.desc())
               .all())
    keep_map = {}
    for te in entries:
        key = (te.worker_id, te.project_id, te.stage_id, te.check_in)
        if key not in keep_map:
            keep_map[key] = te
            _sync_work_ledger_for_time_entry(te)
            continue
        te.is_void = True
        te.void_reason = 'Auto-void duplicate time entry (same worker/project/stage/start time)'
        te.voided_at = _pkt_now_naive()
        _sync_work_ledger_for_time_entry(te)
        _void_orphan_work_ledgers_for_time_entry(te, reason=te.void_reason)


def _reconcile_all_time_entries_once():
    if _runtime_flag_get('time_entry_dedupe_done') == '1':
        return
    try:
        worker_ids = [wid for (wid,) in db.session.query(TimeEntry.worker_id).distinct().all() if wid]
        for wid in worker_ids:
            _reconcile_worker_time_entries(wid)
            _repair_worker_work_ledger_links(wid)
        db.session.commit()
        _runtime_flag_set('time_entry_dedupe_done', '1')
    except Exception as ex:
        current_app.logger.warning('Time-entry reconciliation skipped due to error: %s', ex)
        db.session.rollback()


def _timekeeping_status_dataset(status_date, status_view='assigned'):
    status_view = (status_view or 'assigned').strip().lower()
    if status_view not in ('assigned', 'not_assigned', 'absent'):
        status_view = 'assigned'

    workers = Worker.query.filter_by(active_status=True).all()
    active_worker_ids = [w.id for w in workers]
    day_start = datetime.combine(status_date, datetime.min.time())
    day_end = datetime.combine(status_date, datetime.max.time())

    assigned_count = 0
    absent_count = 0
    not_assigned_count = 0
    status_rows = []

    if not active_worker_ids:
        return dict(
            status_date=status_date,
            status_view=status_view,
            assigned_count=assigned_count,
            absent_count=absent_count,
            not_assigned_count=not_assigned_count,
            status_rows=status_rows
        )

    assigned_latest = {}
    assigned_entry_counts = {}
    assigned_ids = set()

    assigned_records = (db.session.query(TimeEntry, Worker, Project)
                        .join(Worker, TimeEntry.worker_id == Worker.id)
                        .join(Project, TimeEntry.project_id == Project.id)
                        .filter(Worker.active_status == True,
                                TimeEntry.is_void == False,
                                TimeEntry.check_in >= day_start,
                                TimeEntry.check_in <= day_end)
                        .order_by(TimeEntry.check_in.desc())
                        .all())

    for te, wk, pr in assigned_records:
        wid = wk.id
        assigned_ids.add(wid)
        assigned_entry_counts[wid] = assigned_entry_counts.get(wid, 0) + 1
        if wid not in assigned_latest:
            assigned_latest[wid] = (te, wk, pr)

    has_before_ids = {
        wid for (wid,) in db.session.query(TimeEntry.worker_id)
        .filter(TimeEntry.worker_id.in_(active_worker_ids),
                TimeEntry.is_void == False,
                TimeEntry.check_in < day_start)
        .distinct()
        .all()
    }
    has_any_ids = {
        wid for (wid,) in db.session.query(TimeEntry.worker_id)
        .filter(TimeEntry.worker_id.in_(active_worker_ids), TimeEntry.is_void == False)
        .distinct()
        .all()
    }
    absent_marks = (db.session.query(AttendanceMark)
                    .filter(AttendanceMark.worker_id.in_(active_worker_ids),
                            AttendanceMark.date == status_date,
                            AttendanceMark.status == 'absent')
                    .all())
    absent_mark_by_worker = {int(m.worker_id): m for m in absent_marks}
    manual_absent_ids = set(absent_mark_by_worker)

    absent_ids = ((has_before_ids - assigned_ids) | manual_absent_ids) - assigned_ids
    not_assigned_ids = (set(active_worker_ids) - has_any_ids) - manual_absent_ids

    assigned_count = len(assigned_ids)
    absent_count = len(absent_ids)
    not_assigned_count = len(not_assigned_ids)

    if status_view == 'assigned':
        for wid, (te, wk, pr) in assigned_latest.items():
            status_rows.append({
                'worker_code': wk.worker_code,
                'worker_name': wk.name,
                'trade': wk.role_type or '',
                'project': pr.name if pr else '-',
                'stage': te.stage.name if te.stage_id and te.stage else '-',
                'entries_count': assigned_entry_counts.get(wid, 1),
                # row traceability: the latest time entry of the day
                'entry': te,
            })
        status_rows.sort(key=lambda r: (r.get('worker_name') or '').lower())
    elif status_view == 'absent':
        worker_map = {w.id: w for w in workers}
        for wid in sorted(absent_ids, key=lambda x: (worker_map.get(x).name or '').lower() if worker_map.get(x) else ''):
            wk = worker_map.get(wid)
            if not wk:
                continue
            status_rows.append({
                'worker_code': wk.worker_code,
                'worker_name': wk.name,
                'trade': wk.role_type or '',
                'project': '-',
                'stage': '-',
                'entries_count': '-',
                'mark': absent_mark_by_worker.get(int(wid)),
            })
    else:
        worker_map = {w.id: w for w in workers}
        for wid in sorted(not_assigned_ids, key=lambda x: (worker_map.get(x).name or '').lower() if worker_map.get(x) else ''):
            wk = worker_map.get(wid)
            if not wk:
                continue
            status_rows.append({
                'worker_code': wk.worker_code,
                'worker_name': wk.name,
                'trade': wk.role_type or '',
                'project': '-',
                'stage': '-',
                'entries_count': '-'
            })

    return dict(
        status_date=status_date,
        status_view=status_view,
        assigned_count=assigned_count,
        absent_count=absent_count,
        not_assigned_count=not_assigned_count,
        status_rows=status_rows
    )


def _migrate_attendance_to_time_entries():
    """Convert legacy attendance into time entries (one-time)."""
    if _runtime_flag_get('time_entry_migration_done') == '1':
        return
    try:
        records = Attendance.query.order_by(Attendance.id).all()
        for a in records:
            if TimeEntry.query.filter_by(attendance_id=a.id).first():
                continue
            check_in = datetime.combine(a.date, datetime.strptime('09:00', '%H:%M').time())
            hours = float(a.hours_worked or 0)
            check_out = check_in + timedelta(hours=hours)
            te = TimeEntry(
                worker_id=a.worker_id,
                project_id=a.project_id,
                stage_id=a.stage_id,
                check_in=check_in,
                check_out=check_out,
                hours=hours,
                overtime=float(a.overtime_hours or 0),
                wage_calculated=float(a.total_wage or 0),
                legacy_calc=True,
                attendance_id=a.id,
                activity_at=check_in
            )
            db.session.add(te)
        db.session.commit()
        _runtime_flag_set('time_entry_migration_done', '1')
    except Exception:
        db.session.rollback()
