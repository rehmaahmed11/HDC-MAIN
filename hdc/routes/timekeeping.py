"""HDC routes: Attendance / timekeeping day sheet and corrections.

Moved verbatim from hdc_erp.py; each handler keeps its
original @app.route decorator and endpoint name.
"""

from datetime import datetime, timedelta

from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func

from hdc.extensions import db
from hdc.models.projects import Project, Stage
from hdc.models.workforce import AttendanceDay, AttendanceMark, TimeEntry, Worker
from hdc.services.audit import log_action
from hdc.services.lookups import _trade_options
from hdc.services.timekeeping import _calc_time_wage, _parse_attendance_entries_payload, _recalculate_attendance_day, _sync_work_ledger_for_time_entry, _timekeeping_status_dataset, _void_orphan_work_ledgers_for_time_entry
from hdc.utils.dates import _pkt_now_naive, _pkt_today
from hdc.utils.format import _activity_at_for, _flt, _parse_date

def register(app):
    """Register Attendance / timekeeping day sheet and corrections."""
    # â”€â”€ Attendance â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    @app.route('/hdc/attendance', methods=['GET', 'POST'])
    @app.route('/hdc/timekeeping', methods=['GET', 'POST'])
    @login_required
    def hdc_attendance():
        if request.method == 'POST':
            if (request.form.get('bulk_mode') or '').strip() == '1':
                entry_date = _parse_date(request.form.get('date'))
                workers_bulk = (Worker.query
                                .filter_by(active_status=True)
                                .order_by(Worker.name.asc(), Worker.id.asc())
                                .all())
                day_start = datetime.combine(entry_date, datetime.min.time())
                day_end = datetime.combine(entry_date, datetime.max.time())
                updated_workers = 0
                skipped_workers = 0

                for wk in workers_bulk:
                    wid = int(wk.id)
                    row_status = (request.form.get(f'status_{wid}') or 'not_assigned').strip().lower()
                    project_id = request.form.get(f'project_id_{wid}', type=int)
                    stage_id = request.form.get(f'stage_id_{wid}', type=int)
                    working_hours = max(0.0, float(_flt(request.form.get(f'working_hours_{wid}'), 0.0) or 0.0))
                    overtime_hours = max(0.0, float(_flt(request.form.get(f'overtime_hours_{wid}'), 0.0) or 0.0))
                    row_allocations = _parse_attendance_entries_payload(request.form.get(f'allocations_{wid}'))
                    row_notes = (request.form.get(f'remarks_{wid}') or '').strip()

                    if row_status not in ('present', 'absent', 'leave', 'not_assigned'):
                        row_status = 'not_assigned'

                    active_entries = (TimeEntry.query
                                      .filter(TimeEntry.worker_id == wid,
                                              TimeEntry.is_void == False,
                                              TimeEntry.check_in >= day_start,
                                              TimeEntry.check_in <= day_end)
                                      .order_by(TimeEntry.check_in.asc(), TimeEntry.id.asc())
                                      .all())
                    mark = AttendanceMark.query.filter_by(worker_id=wid, date=entry_date).first()

                    if row_status == 'present':
                        normalized = []
                        total_hours = 0.0
                        if row_allocations:
                            for alloc in row_allocations:
                                pid = int(alloc.get('project_id') or 0)
                                sid = int(alloc.get('stage_id') or 0)
                                hrs = max(0.0, float(_flt(alloc.get('hours'), 0.0) or 0.0))
                                if (not pid) or (not sid) or hrs <= 0:
                                    normalized = []
                                    break
                                stg = Stage.query.get(sid)
                                if (not stg) or (int(stg.project_id or 0) != int(pid or 0)):
                                    normalized = []
                                    break
                                normalized.append({'project_id': pid, 'stage_id': sid, 'hours': hrs})
                                total_hours += hrs
                        else:
                            total_hours = working_hours + overtime_hours
                            if project_id and stage_id and total_hours > 0:
                                stg = Stage.query.get(stage_id)
                                if stg and (int(stg.project_id or 0) == int(project_id or 0)):
                                    normalized = [{'project_id': int(project_id), 'stage_id': int(stage_id), 'hours': total_hours}]

                        if (not normalized) or total_hours <= 0 or total_hours > 24:
                            skipped_workers += 1
                            continue

                        for te in active_entries:
                            te.is_void = True
                            te.void_reason = 'Replaced by bulk attendance sheet'
                            te.voided_at = _pkt_now_naive()
                            _sync_work_ledger_for_time_entry(te)
                            _void_orphan_work_ledgers_for_time_entry(te, reason='Replaced by bulk attendance sheet')

                        running_hours = 0.0
                        regular_done = 0.0
                        for row in normalized:
                            hrs = row['hours']
                            remaining_regular = max(0.0, 8.0 - regular_done)
                            regular_hours = min(hrs, remaining_regular)
                            overtime_part = max(0.0, hrs - regular_hours)
                            check_in = day_start + timedelta(hours=running_hours)
                            check_out = check_in + timedelta(hours=hrs)
                            wage = _calc_time_wage(wk, regular_hours, overtime_part, 0.0, work_date=entry_date)
                            te = TimeEntry(
                                worker_id=wid,
                                project_id=row['project_id'],
                                stage_id=row['stage_id'],
                                check_in=check_in,
                                check_out=check_out,
                                hours=hrs,
                                overtime=overtime_part,
                                qty_sqft=0.0,
                                wage_calculated=wage,
                                legacy_calc=False,
                                attendance_id=None,
                                activity_at=check_in
                            )
                            db.session.add(te)
                            db.session.flush()
                            _sync_work_ledger_for_time_entry(te)
                            running_hours += hrs
                            regular_done += regular_hours

                        if mark:
                            mark.status = 'present'
                            mark.notes = row_notes
                            mark.activity_at = _activity_at_for(entry_date)
                        else:
                            db.session.add(AttendanceMark(
                                worker_id=wid,
                                date=entry_date,
                                status='present',
                                notes=row_notes,
                                activity_at=_activity_at_for(entry_date)
                            ))
                        updated_workers += 1
                        continue

                    for te in active_entries:
                        te.is_void = True
                        te.void_reason = f'Bulk sheet set as {row_status.replace("_", " ")}'
                        te.voided_at = _pkt_now_naive()
                        _sync_work_ledger_for_time_entry(te)
                        _void_orphan_work_ledgers_for_time_entry(te, reason=f'Bulk sheet set as {row_status.replace("_", " ")}')

                    if row_status in ('absent', 'leave'):
                        if mark:
                            mark.status = row_status
                            mark.notes = row_notes
                            mark.activity_at = _activity_at_for(entry_date)
                        else:
                            db.session.add(AttendanceMark(
                                worker_id=wid,
                                date=entry_date,
                                status=row_status,
                                notes=row_notes,
                                activity_at=_activity_at_for(entry_date)
                            ))
                        updated_workers += 1
                    else:
                        if mark:
                            db.session.delete(mark)

                db.session.flush()
                for wk in workers_bulk:
                    _recalculate_attendance_day(int(wk.id), entry_date)
                db.session.commit()
                flash(f'Bulk attendance sheet saved for {updated_workers} worker(s). Skipped {skipped_workers} invalid row(s).', 'success')
                return redirect(url_for('hdc_attendance', sheet_date=entry_date.isoformat()))

            entry_status = (request.form.get('entry_status') or 'present').strip().lower()
            wid = request.form.get('worker_id', type=int)
            if not wid:
                flash('Please select a valid worker.', 'danger')
                return redirect(url_for('hdc_attendance'))

            entry_date = _parse_date(request.form.get('date'))

            if entry_status == 'absent':
                day_start = datetime.combine(entry_date, datetime.min.time())
                day_end = datetime.combine(entry_date, datetime.max.time())
                has_time = TimeEntry.query.filter(
                    TimeEntry.worker_id == wid,
                    TimeEntry.is_void == False,
                    TimeEntry.check_in >= day_start,
                    TimeEntry.check_in <= day_end
                ).first()
                if has_time:
                    flash('This worker already has a present entry on selected date.', 'warning')
                    return redirect(url_for('hdc_attendance'))

                mark = AttendanceMark.query.filter_by(worker_id=wid, date=entry_date).first()
                notes = (request.form.get('absent_notes') or '').strip()
                if mark:
                    mark.status = 'absent'
                    mark.notes = notes
                    mark.activity_at = _activity_at_for(entry_date)
                else:
                    db.session.add(AttendanceMark(
                        worker_id=wid,
                        date=entry_date,
                        status='absent',
                        notes=notes,
                        activity_at=_activity_at_for(entry_date)
                    ))
                db.session.commit()
                worker = Worker.query.get(wid)
                worker_label = worker.name if worker else f'Worker #{wid}'
                log_action(
                    current_user,
                    'update',
                    f'{current_user.username.title()} updated attendance of {worker_label}: status set to Absent on {entry_date.isoformat()}',
                    'attendance',
                    wid
                )
                db.session.commit()
                flash('Absent marked successfully.', 'success')
                return redirect(url_for('hdc_attendance'))

            day_start = datetime.combine(entry_date, datetime.min.time())
            day_end = datetime.combine(entry_date, datetime.max.time())
            existing = TimeEntry.query.filter(
                TimeEntry.worker_id == wid,
                TimeEntry.is_void == False,
                TimeEntry.check_in >= day_start,
                TimeEntry.check_in <= day_end
            ).first()
            if existing:
                flash('Duplicate attendance blocked: this worker already has attendance for this date.', 'warning')
                return redirect(url_for('hdc_attendance'))

            items = _parse_attendance_entries_payload(request.form.get('entries_json'))
            if not items:
                flash('Please add at least one site/stage entry with hours.', 'danger')
                return redirect(url_for('hdc_attendance'))

            total_hours = 0.0
            normalized = []
            for row in items:
                pid = int(row.get('project_id') or 0)
                sid = int(row.get('stage_id') or 0)
                hrs = _flt(row.get('hours'), 0.0)
                if not pid or not sid:
                    flash('Each entry must include a valid site and stage.', 'danger')
                    return redirect(url_for('hdc_attendance'))
                if hrs <= 0:
                    flash('Hours must be greater than 0 for every entry.', 'danger')
                    return redirect(url_for('hdc_attendance'))
                stage = Stage.query.get(sid)
                if (not stage) or stage.project_id != pid:
                    flash('Selected stage does not belong to selected site/project.', 'danger')
                    return redirect(url_for('hdc_attendance'))
                normalized.append({'project_id': pid, 'stage_id': sid, 'hours': hrs})
                total_hours += hrs

            if total_hours > 24:
                flash('Total hours cannot exceed 24 in a single day.', 'danger')
                return redirect(url_for('hdc_attendance'))

            worker = Worker.query.get_or_404(wid)
            day_row = AttendanceDay.query.filter_by(worker_id=wid, date=entry_date).first()
            if not day_row:
                day_row = AttendanceDay(
                    worker_id=wid,
                    date=entry_date,
                    total_hours=0.0,
                    day_value=0.0,
                    overtime_hours=0.0,
                    entry_count=0,
                    is_void=False
                )
                db.session.add(day_row)
                db.session.flush()
            else:
                day_row.is_void = False

            running_hours = 0.0
            regular_done = 0.0
            for row in normalized:
                hrs = row['hours']
                remaining_regular = max(0.0, 8.0 - regular_done)
                regular_hours = min(hrs, remaining_regular)
                overtime_hours = max(0.0, hrs - regular_hours)
                ci = day_start + timedelta(hours=running_hours)
                co = ci + timedelta(hours=hrs)
                wage = _calc_time_wage(worker, regular_hours, overtime_hours, 0.0, work_date=entry_date)
                te = TimeEntry(
                    worker_id=wid,
                    project_id=row['project_id'],
                    stage_id=row['stage_id'],
                    check_in=ci,
                    check_out=co,
                    hours=hrs,
                    overtime=overtime_hours,
                    qty_sqft=0.0,
                    wage_calculated=wage,
                    legacy_calc=False,
                    attendance_id=day_row.id,
                    activity_at=ci
                )
                db.session.add(te)
                db.session.flush()
                _sync_work_ledger_for_time_entry(te)
                running_hours += hrs
                regular_done += regular_hours

            summary = _recalculate_attendance_day(wid, entry_date)
            mark = AttendanceMark.query.filter_by(worker_id=wid, date=entry_date).first()
            if mark and mark.status == 'absent':
                db.session.delete(mark)
            worker_name = worker.name if worker else f'Worker #{wid}'
            entries_txt = []
            for row in normalized:
                proj = Project.query.get(row['project_id'])
                stg = Stage.query.get(row['stage_id'])
                entries_txt.append(f"{proj.name if proj else row['project_id']} -> {stg.name if stg else row['stage_id']} ({float(row['hours']):.2f} hrs)")
            log_action(
                current_user,
                'create',
                f'{current_user.username.title()} created attendance for {worker_name}: ' + '; '.join(entries_txt),
                'attendance',
                wid
            )
            db.session.commit()
            flash(f"Attendance saved: {summary['total_hours']:.2f}h, Day={summary['day']:.0f}, OT={summary['overtime']:.2f}h.", 'success')
            return redirect(url_for('hdc_attendance'))

        projects = Project.query.all()
        workers = Worker.query.filter_by(active_status=True).all()
        trade_options = _trade_options()
        stages = Stage.query.all()
        status_cards = _timekeeping_status_dataset(_pkt_today(), 'assigned')

        filter_project_id = request.args.get('project_id', type=int)
        filter_stage_id = request.args.get('stage_id', type=int)
        filter_worker_id = request.args.get('worker_id', type=int)
        filter_worker_name = (request.args.get('worker_name') or '').strip()
        filter_trade = (request.args.get('trade') or '').strip()
        filter_date_from = request.args.get('date_from')
        filter_date_to = request.args.get('date_to')
        filter_show_voided = (request.args.get('show_voided') or '').strip().lower() in ('1', 'true', 'on', 'yes')

        q = (db.session.query(TimeEntry, Worker, Project)
             .join(Worker, TimeEntry.worker_id == Worker.id)
             .join(Project, TimeEntry.project_id == Project.id))
        if not filter_show_voided:
            q = q.filter(TimeEntry.is_void == False)

        if filter_project_id:
            q = q.filter(TimeEntry.project_id == filter_project_id)
        if filter_stage_id:
            q = q.filter(TimeEntry.stage_id == filter_stage_id)
        if filter_worker_id:
            q = q.filter(TimeEntry.worker_id == filter_worker_id)
        if filter_worker_name:
            q = q.filter(Worker.name.ilike(f"%{filter_worker_name}%"))
        if filter_trade:
            q = q.filter(Worker.role_type.ilike(f"%{filter_trade}%"))
        if filter_date_from:
            q = q.filter(TimeEntry.check_in >= datetime.strptime(filter_date_from, '%Y-%m-%d'))
        if filter_date_to:
            q = q.filter(TimeEntry.check_in <= datetime.strptime(filter_date_to, '%Y-%m-%d') + timedelta(days=1))

        has_filters = any([
            filter_project_id, filter_stage_id, filter_worker_id,
            filter_worker_name, filter_trade, filter_date_from, filter_date_to,
            filter_show_voided
        ])
        if has_filters:
            records = q.order_by(TimeEntry.check_in.desc()).all()
        else:
            records = q.order_by(TimeEntry.check_in.desc()).limit(180).all()

        grouped = {}
        for te, wk, proj in records:
            key = (wk.id, te.check_in.date())
            row = grouped.get(key)
            if not row:
                row = {
                    'date': te.check_in.date(),
                    'worker': wk,
                    'entries': [],
                    'total_hours': 0.0,
                    'overtime': 0.0,
                    'total_wage': 0.0,
                    'active_count': 0,
                    'void_count': 0
                }
                grouped[key] = row
            row['entries'].append({
                'id': te.id,
                'project': proj,
                'stage': te.stage,
                'hours': float(te.hours or 0.0),
                'overtime': float(te.overtime or 0.0),
                'wage': float(te.wage_calculated or 0.0),
                'is_void': bool(te.is_void),
                'check_in': te.check_in
            })
            if te.is_void:
                row['void_count'] += 1
            else:
                row['active_count'] += 1
                row['total_hours'] += float(te.hours or 0.0)
                row['overtime'] += float(te.overtime or 0.0)
                row['total_wage'] += float(te.wage_calculated or 0.0)

        attendance_rows = list(grouped.values())
        attendance_rows.sort(key=lambda r: (r['date'], (r['worker'].name or '').lower()), reverse=True)
        for row in attendance_rows:
            row['day'] = 1 if row['total_hours'] >= 8.0 else 0
            row['status'] = 'Voided' if row['active_count'] == 0 else 'Active'
            row['entries'].sort(key=lambda e: e['check_in'])

        sheet_date = _parse_date(request.args.get('sheet_date'), fallback=_pkt_today())
        day_start = datetime.combine(sheet_date, datetime.min.time())
        day_end = datetime.combine(sheet_date, datetime.max.time())
        active_workers = (Worker.query
                          .filter_by(active_status=True)
                          .order_by(Worker.name.asc(), Worker.id.asc())
                          .all())
        worker_ids = [int(w.id) for w in active_workers]
        day_entries = (TimeEntry.query
                       .filter(TimeEntry.worker_id.in_(worker_ids),
                               TimeEntry.is_void == False,
                               TimeEntry.check_in >= day_start,
                               TimeEntry.check_in <= day_end)
                       .order_by(TimeEntry.worker_id.asc(), TimeEntry.check_in.asc(), TimeEntry.id.asc())
                       .all()) if worker_ids else []
        marks = (AttendanceMark.query
                 .filter(AttendanceMark.worker_id.in_(worker_ids),
                         AttendanceMark.date == sheet_date)
                 .all()) if worker_ids else []
        mark_map = {int(m.worker_id): m for m in marks}
        entry_map = {}
        for te in day_entries:
            entry_map.setdefault(int(te.worker_id), []).append(te)

        daily_sheet_rows = []
        for wk in active_workers:
            rows = entry_map.get(int(wk.id), [])
            mk = mark_map.get(int(wk.id))
            if rows:
                total_hours = sum(float(r.hours or 0.0) for r in rows)
                ot_hours = sum(float(r.overtime or 0.0) for r in rows)
                working_hours = max(0.0, total_hours - ot_hours)
                projects_txt = ', '.join(list(dict.fromkeys([(r.project.name if r.project else '-') for r in rows])))
                stages_txt = ', '.join(list(dict.fromkeys([(r.stage.name if r.stage else '-') for r in rows])))
                status_txt = 'Present'
                remarks_txt = (mk.notes if mk and (mk.notes or '').strip() else '-')
            elif mk and (mk.status or '').strip().lower() in ('absent', 'leave'):
                total_hours = 0.0
                ot_hours = 0.0
                working_hours = 0.0
                projects_txt = '-'
                stages_txt = '-'
                status_txt = (mk.status or '').strip().capitalize()
                remarks_txt = (mk.notes or '-')
            else:
                total_hours = 0.0
                ot_hours = 0.0
                working_hours = 0.0
                projects_txt = '-'
                stages_txt = '-'
                status_txt = 'Not Assigned'
                remarks_txt = '-'
            allocations = [{
                'project_id': int(r.project_id or 0),
                'stage_id': int(r.stage_id or 0),
                'hours': float(r.hours or 0.0)
            } for r in rows]
            daily_sheet_rows.append({
                'worker': wk,
                'status': status_txt,
                'project_name': projects_txt,
                'stage_name': stages_txt,
                'working_hours': working_hours,
                'overtime_hours': ot_hours,
                'remarks': remarks_txt,
                'allocations': allocations
            })

        return render_template('timekeeping/timekeeping.html',
            projects=projects, workers=workers, stages=stages, attendance_rows=attendance_rows,
            today=_pkt_today().isoformat(),
            sheet_date=sheet_date.isoformat(),
            daily_sheet_rows=daily_sheet_rows,
            status_cards_date=status_cards['status_date'].isoformat(),
            assigned_count=status_cards['assigned_count'],
            absent_count=status_cards['absent_count'],
            not_assigned_count=status_cards['not_assigned_count'],
            filter_project_id=filter_project_id,
            filter_stage_id=filter_stage_id,
            filter_worker_id=filter_worker_id,
            filter_worker_name=filter_worker_name,
            filter_trade=filter_trade,
            filter_date_from=filter_date_from,
            filter_date_to=filter_date_to,
            filter_show_voided=filter_show_voided,
            has_filters=has_filters,
            trade_options=trade_options)


    @app.route('/hdc/timekeeping/status')
    @login_required
    def hdc_timekeeping_status():
        status_date = _parse_date(request.args.get('status_date'), fallback=_pkt_today())
        status_view = (request.args.get('status_view') or 'assigned').strip().lower()
        data = _timekeeping_status_dataset(status_date, status_view)
        return render_template('timekeeping/timekeeping_status.html',
            status_date=data['status_date'].isoformat(),
            status_view=data['status_view'],
            assigned_count=data['assigned_count'],
            absent_count=data['absent_count'],
            not_assigned_count=data['not_assigned_count'],
            status_rows=data['status_rows']
        )


    @app.route('/hdc/timekeeping/<int:tid>/edit', methods=['GET', 'POST'])
    @login_required
    def hdc_edit_attendance(tid):
        t = TimeEntry.query.get_or_404(tid)
        if t.is_void:
            flash('Voided time entry cannot be edited.', 'warning')
            return redirect(url_for('hdc_attendance'))

        projects = Project.query.all()
        stages = Stage.query.all()
        if request.method == 'POST':
            pid = request.form.get('project_id', type=int)
            sid = request.form.get('stage_id', type=int)
            hours = _flt(request.form.get('hours'), 0.0)
            if not pid or not sid:
                flash('Site/project and stage are required.', 'danger')
                return redirect(url_for('hdc_edit_attendance', tid=tid))
            if hours <= 0:
                flash('Hours must be greater than 0.', 'danger')
                return redirect(url_for('hdc_edit_attendance', tid=tid))

            stage = Stage.query.get(sid)
            if (not stage) or (stage.project_id != pid):
                flash('Selected stage does not belong to selected project.', 'danger')
                return redirect(url_for('hdc_edit_attendance', tid=tid))

            work_date = t.check_in.date()
            day_start = datetime.combine(work_date, datetime.min.time())
            day_end = datetime.combine(work_date, datetime.max.time())
            other_day_total = (db.session.query(func.sum(TimeEntry.hours))
                               .filter(TimeEntry.worker_id == t.worker_id,
                                       TimeEntry.is_void == False,
                                       TimeEntry.id != t.id,
                                       TimeEntry.check_in >= day_start,
                                       TimeEntry.check_in <= day_end)
                               .scalar()) or 0.0
            if float(other_day_total) + float(hours) > 24.0:
                flash('Total hours cannot exceed 24 in a single day.', 'danger')
                return redirect(url_for('hdc_edit_attendance', tid=tid))

            old_project = Project.query.get(t.project_id)
            old_stage = Stage.query.get(t.stage_id) if t.stage_id else None
            old_hours = float(t.hours or 0.0)
            t.project_id = pid
            t.stage_id = sid
            t.hours = hours
            t.qty_sqft = 0.0
            _recalculate_attendance_day(t.worker_id, work_date)
            new_project = Project.query.get(pid)
            new_stage = Stage.query.get(sid)
            worker_name = t.worker.name if t.worker else f'Worker #{t.worker_id}'
            log_action(
                current_user,
                'update',
                (
                    f'{current_user.username.title()} updated attendance of {worker_name}: '
                    f'project {(old_project.name if old_project else "-")} -> {(new_project.name if new_project else "-")}; '
                    f'stage {(old_stage.name if old_stage else "-")} -> {(new_stage.name if new_stage else "-")}; '
                    f'hours {old_hours:.2f} hrs -> {float(hours):.2f} hrs'
                ),
                'attendance',
                t.id
            )
            db.session.commit()
            flash('Attendance entry updated.', 'success')
            return redirect(url_for('hdc_attendance'))

        return render_template('timekeeping/timekeeping_edit.html',
            t=t,
            projects=projects,
            stages=stages,
            today=t.check_in.date().isoformat()
        )


    @app.route('/hdc/timekeeping/<int:tid>/delete', methods=['POST'])
    @login_required
    def hdc_delete_attendance(tid):
        t = TimeEntry.query.get_or_404(tid)
        if t.is_void:
            flash('Time entry already voided.', 'info')
            return redirect(url_for('hdc_attendance'))
        reason = (request.form.get('void_reason') or '').strip() or 'Voided by user'
        t.is_void = True
        t.void_reason = reason
        t.voided_at = _pkt_now_naive()
        _sync_work_ledger_for_time_entry(t)
        _void_orphan_work_ledgers_for_time_entry(t, reason=reason)
        _recalculate_attendance_day(t.worker_id, t.check_in.date())
        worker_name = t.worker.name if t.worker else f'Worker #{t.worker_id}'
        log_action(
            current_user,
            'void',
            f'{current_user.username.title()} voided attendance of {worker_name}: {float(t.hours or 0.0):.2f} hrs on {t.check_in.date().isoformat()}',
            'attendance',
            t.id
        )
        db.session.commit()
        flash('Time entry voided.', 'success')
        return redirect(url_for('hdc_attendance'))


    @app.route('/hdc/timekeeping/<int:tid>/reactivate', methods=['POST'])
    @login_required
    def hdc_reactivate_attendance(tid):
        t = TimeEntry.query.get_or_404(tid)
        if not t.is_void:
            flash('Time entry is already active.', 'info')
            return redirect(url_for('hdc_attendance', show_voided=1))

        work_date = t.check_in.date()
        day_start = datetime.combine(work_date, datetime.min.time())
        day_end = datetime.combine(work_date, datetime.max.time())
        day_total_without_t = (db.session.query(func.sum(TimeEntry.hours))
                               .filter(TimeEntry.worker_id == t.worker_id,
                                       TimeEntry.is_void == False,
                                       TimeEntry.check_in >= day_start,
                                       TimeEntry.check_in <= day_end)
                               .scalar()) or 0.0
        if float(day_total_without_t) + float(t.hours or 0.0) > 24.0:
            flash('Cannot reactivate: total day hours would exceed 24.', 'warning')
            return redirect(url_for('hdc_attendance', show_voided=1))

        t.is_void = False
        t.void_reason = None
        t.voided_at = None
        _recalculate_attendance_day(t.worker_id, work_date)
        worker_name = t.worker.name if t.worker else f'Worker #{t.worker_id}'
        log_action(
            current_user,
            'update',
            f'{current_user.username.title()} reactivated attendance of {worker_name}: {float(t.hours or 0.0):.2f} hrs on {work_date.isoformat()}',
            'attendance',
            t.id
        )
        db.session.commit()
        flash('Time entry reactivated.', 'success')
        return redirect(url_for('hdc_attendance', show_voided=1))
