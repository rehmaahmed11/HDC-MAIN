"""HDC routes: Subcontractors, labour teams, ledgers and stage shifts.

Moved verbatim from hdc_erp.py; each handler keeps its
original @app.route decorator and endpoint name.
"""

from datetime import datetime

from flask import abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from hdc.extensions import db
from hdc.models.accounts import Expense
from hdc.models.projects import Project, Stage
from hdc.models.subcontract import SubcontractAttendance, SubcontractEvent, SubcontractLabourAttendance, SubcontractLabourPayment, SubcontractLabourWorker, SubcontractPayment, Subcontractor
from hdc.services.accounts import _accounts_post_subcontract_labour_payment_row, _accounts_post_subcontract_payment_row
from hdc.services.lookups import _ensure_expense_category
from hdc.services.receipts import _receipt_company_profile
from hdc.services.subcontract import _ensure_subcontract_baseline_events, _ensure_subcontract_labour_attendance_schema, _log_subcontract_event, _next_subcontractor_code, _subcontract_scope_stages, _subcontract_stage_snapshot
from hdc.services.timekeeping import _has_recent_duplicate
from hdc.utils.dates import _pkt_now_naive, _pkt_today
from hdc.utils.format import _activity_at_for, _amount_to_words, _flt, _parse_date

def register(app):
    """Register Subcontractors, labour teams, ledgers and stage shifts."""
    @app.route('/hdc/projects/<int:pid>/add_subcontractor', methods=['POST'])
    @login_required
    def hdc_add_subcontractor(pid):
        Project.query.get_or_404(pid)
        stage_id = request.form.get('stage_id', type=int)
        if stage_id:
            stage = Stage.query.get(stage_id)
            if (not stage) or (stage.project_id != pid):
                flash('Selected stage does not belong to this project.', 'danger')
                return redirect(url_for('hdc_project_detail', pid=pid))
        name = (request.form.get('name','') or '').strip()
        if not name:
            flash('Subcontractor name is required.', 'warning')
            return redirect(url_for('hdc_project_detail', pid=pid))
        code = _next_subcontractor_code()
        while Subcontractor.query.filter(func.lower(Subcontractor.subcontractor_code) == code.lower()).first():
            code = _next_subcontractor_code()
        sub = Subcontractor(
            subcontractor_code=code,
            project_id=pid, stage_id=None,
            name=name,
            phone=(request.form.get('phone','') or '').strip(),
            work_type='',
            contract_type='lump_sum',
            rate_per_sqft=0.0,
            total_sqft=0.0,
            lump_sum_amount=0.0,
            retention_percentage=0.0,
            work_done_percentage=0.0)
        db.session.add(sub)
        db.session.flush()
        _log_subcontract_event(
            sub=sub,
            event_type='create',
            to_value=f'{sub.subcontractor_code or ""} {sub.name}',
            notes='Subcontractor profile created'
        )
        db.session.commit()
        flash(f'Subcontractor added ({code}).', 'success')
        return redirect(url_for('hdc_project_detail', pid=pid))


    @app.route('/hdc/subcontractor/<int:sid>/pay', methods=['POST'])
    @login_required
    def hdc_pay_subcontractor(sid):
        sub = Subcontractor.query.get_or_404(sid)
        return_to = (request.form.get('return_to') or '').strip().lower()
        pay_date = _parse_date(request.form.get('date'))
        amount = _flt(request.form.get('amount'))
        settle_shortfall = (request.form.get('settle_shortfall') or '').strip().lower() in ('1', 'true', 'on', 'yes')
        notes = (request.form.get('notes','') or '').strip()
        project_id = request.form.get('project_id', type=int)
        stage_id = request.form.get('stage_id', type=int)
        stg = None
        if stage_id:
            stg = Stage.query.get(stage_id)
            if not stg:
                flash('Selected stage is invalid.', 'danger')
                return redirect(url_for('hdc_subcontractor_payment_page', sid=sub.id))
            if project_id and stg.project_id != project_id:
                flash('Selected stage does not belong to selected project.', 'danger')
                return redirect(url_for('hdc_subcontractor_payment_page', sid=sub.id))
            project_id = stg.project_id
        if project_id and (not Project.query.get(project_id)):
            flash('Selected project is invalid.', 'danger')
            return redirect(url_for('hdc_subcontractor_payment_page', sid=sub.id))
        if amount <= 0:
            flash('Payment amount must be greater than zero.', 'danger')
            return redirect(url_for('hdc_subcontractor_payment_page', sid=sub.id))

        allowed_balance = float(sub.contract_balance or 0.0)
        scope_name = 'All Stages'
        if stg:
            snap = _subcontract_stage_snapshot(sub, stg)
            allowed_balance = float(snap.get('contract_balance') or snap.get('balance') or 0.0)
            scope_name = stg.name

        if amount > (allowed_balance + 1e-6):
            flash(f'Payment exceeds remaining contract balance ({allowed_balance:,.0f} PKR). Overpayment is blocked to avoid duplicate cost.', 'danger')
            return redirect(url_for('hdc_subcontractor_payment_page', sid=sub.id))

        payment_part = min(amount, allowed_balance)
        settlement_part = 0.0
        if settle_shortfall and payment_part < allowed_balance:
            settlement_part = allowed_balance - payment_part

        scope_project_id = project_id or (stg.project_id if stg else sub.project_id)
        scope_stage_id = stage_id if stg else None
        if (settlement_part > 0) and not scope_project_id:
            flash('Select project/stage scope for settlement posting.', 'warning')
            return redirect(url_for('hdc_subcontractor_payment_page', sid=sub.id))

        pay_row = None
        if payment_part > 0:
            if _has_recent_duplicate(
                SubcontractPayment,
                subcontractor_id=sid,
                project_id=project_id,
                stage_id=stage_id,
                entry_type='payment',
                amount=payment_part,
                date=pay_date,
                notes=notes
            ):
                flash('Duplicate subcontract payment prevented (same values submitted too quickly).', 'warning')
                return redirect(url_for('hdc_subcontractor_payment_page', sid=sub.id))
            pay_row = SubcontractPayment(
                subcontractor_id=sid,
                project_id=project_id,
                stage_id=stage_id,
                entry_type='payment',
                amount=payment_part,
                date=pay_date,
                activity_at=_activity_at_for(pay_date),
                notes=notes
            )
            db.session.add(pay_row)
            db.session.flush()
            ok_txn, msg_txn, _ = _accounts_post_subcontract_payment_row(pay_row, subcontractor_name=sub.name, commit=False)
            if not ok_txn:
                db.session.rollback()
                flash(msg_txn or 'Unable to post subcontract payment in unified accounts.', 'danger')
                return redirect(url_for('hdc_subcontractor_payment_page', sid=sub.id))
            _log_subcontract_event(
                sub=sub,
                event_type='payment',
                amount=payment_part,
                notes=f'{notes or "Subcontract payment recorded"} | Scope: {scope_name}',
                project_id=project_id,
                stage_id=stage_id
            )

        if settlement_part > 0:
            settlement_notes = (notes + ' | ' if notes else '') + f'Subcontract settlement shortfall for {sub.name} | SETTLE_SUBCONTRACTOR_ID:{sid}'
            if _has_recent_duplicate(
                SubcontractPayment,
                subcontractor_id=sid,
                project_id=project_id,
                stage_id=stage_id,
                entry_type='settlement',
                amount=settlement_part,
                date=pay_date,
                notes=settlement_notes
            ):
                flash('Duplicate subcontract settlement prevented.', 'warning')
                return redirect(url_for('hdc_subcontractor_payment_page', sid=sub.id))
            db.session.add(SubcontractPayment(
                subcontractor_id=sid,
                project_id=project_id,
                stage_id=stage_id,
                entry_type='settlement',
                amount=settlement_part,
                date=pay_date,
                activity_at=_activity_at_for(pay_date),
                notes=settlement_notes
            ))
            settlement_cat = _ensure_expense_category('Settlement')
            if _has_recent_duplicate(
                Expense,
                project_id=scope_project_id,
                stage_id=scope_stage_id,
                category_id=(settlement_cat.id if settlement_cat else None),
                amount=-settlement_part,
                date=pay_date,
                remarks=settlement_notes
            ):
                flash('Duplicate subcontract settlement expense prevented.', 'warning')
                return redirect(url_for('hdc_subcontractor_payment_page', sid=sub.id))
            db.session.add(Expense(
                project_id=scope_project_id,
                stage_id=scope_stage_id,
                category_id=(settlement_cat.id if settlement_cat else None),
                amount=-settlement_part,
                date=pay_date,
                activity_at=_activity_at_for(pay_date),
                remarks=settlement_notes
            ))
            _log_subcontract_event(
                sub=sub,
                event_type='settlement',
                amount=settlement_part,
                notes=f'Shortfall settled to close payable | Scope: {scope_name}',
                project_id=scope_project_id,
                stage_id=scope_stage_id
            )
        db.session.commit()
        if settlement_part > 0:
            flash(
                f'Payment recorded: {payment_part:,.0f} PKR cash paid and {settlement_part:,.0f} PKR shortfall settled.',
                'success'
            )
        else:
            flash(f'Payment to {sub.name} recorded.', 'success')
        if payment_part > 0 and pay_row is not None and pay_row.id:
            return redirect(url_for('hdc_subcontractor_payment_receipt', sid=sub.id, pid=pay_row.id))
        if return_to == 'ledger':
            return redirect(url_for('hdc_subcontractor_ledger', sid=sub.id))
        if return_to == 'payment_page':
            return redirect(url_for('hdc_subcontractor_payment_page', sid=sub.id))
        if sub.project_id:
            return redirect(url_for('hdc_project_detail', pid=sub.project_id))
        return redirect(url_for('hdc_subcontractor_ledger', sid=sub.id))


    @app.route('/hdc/subcontractor/<int:sid>/payment/<int:pid>/receipt')
    @login_required
    def hdc_subcontractor_payment_receipt(sid, pid):
        sub = Subcontractor.query.get_or_404(sid)
        row = SubcontractPayment.query.get_or_404(pid)
        if row.subcontractor_id != sid:
            abort(404)
        receipt_id = f'RCPT-SP-{row.id:08d}'
        project_name = (row.project.name if row.project else '-')
        stage_name = (row.stage.name if row.stage else '-')
        recent = (
            SubcontractPayment.query
            .filter(SubcontractPayment.subcontractor_id == sid, SubcontractPayment.id != row.id, SubcontractPayment.is_void == False)
            .order_by(SubcontractPayment.activity_at.desc(), SubcontractPayment.id.desc())
            .limit(5).all()
        )
        recent_entries = [{
            'date': (r.date.strftime('%Y-%m-%d') if r.date else '-'),
            'type': (r.entry_type or 'payment').title(),
            'direction': 'pay',
            'party': sub.name,
            'amount': float(r.amount or 0),
            'receipt_url': url_for('hdc_subcontractor_payment_receipt', sid=sid, pid=r.id)
        } for r in recent]
        return render_template('accounts/transaction_receipt.html',
            company_profile=_receipt_company_profile(),
            receipt_id=receipt_id,
            created_at=(row.activity_at or _pkt_now_naive()),
            tx_type=f'Subcontractor {(row.entry_type or "Payment").title()} Receipt',
            party_name=sub.name,
            project_name=project_name,
            stage_name=stage_name,
            account_used='-',
            amount=float(row.amount or 0),
            amount_words=_amount_to_words(row.amount or 0),
            note=(row.notes or ''),
            reference_id=f'subcontract_payment#{row.id}',
            recent_entries=recent_entries,
            recent_entries_title=f'Last 5 Subcontractor Entries – {sub.name}',
            back_url=url_for('hdc_subcontractor_payment_page', sid=sid),
            print_label='Print / Save PDF'
        )


    @app.route('/hdc/subcontractor/<int:sid>/payments')
    @login_required
    def hdc_subcontractor_payment_page(sid):
        sub = Subcontractor.query.get_or_404(sid)
        stage_rows = _subcontract_scope_stages(sub)
        project_map = {}
        stage_options = []
        stage_snapshots = {}
        for st in stage_rows:
            if st.project:
                project_map[st.project.id] = st.project
            snap = _subcontract_stage_snapshot(sub, st)
            stage_snapshots[str(st.id)] = {
                'project_id': st.project_id,
                'stage_name': st.name,
                'project_name': st.project.name if st.project else f'Project#{st.project_id}',
                'payable': float(snap.get('payable') or 0.0),
                'paid': float(snap.get('paid') or 0.0),
                'settled': float(snap.get('settled') or 0.0),
                'cleared': float(snap.get('cleared') or 0.0),
                'balance': float(snap.get('balance') or 0.0),
                'contract_balance': float(snap.get('contract_balance') or 0.0),
                'live_balance': float(snap.get('live_balance') or 0.0),
                'advance_paid': float(snap.get('advance_paid') or 0.0),
                'progress': float(snap.get('progress') or 0.0),
                'contract': float(snap.get('contract') or 0.0)
            }
            stage_options.append(st)
        projects = sorted(project_map.values(), key=lambda p: (p.name or '').lower())

        payments = (SubcontractPayment.query
                    .filter(SubcontractPayment.subcontractor_id == sub.id, SubcontractPayment.is_void == False)
                    .order_by(SubcontractPayment.date.desc(), SubcontractPayment.id.desc())
                    .all())
        return render_template('subcontractors/subcontractor_payment.html',
            sub=sub,
            projects=projects,
            stage_options=stage_options,
            stage_snapshots=stage_snapshots,
            payments=payments
        )


    @app.route('/hdc/subcontractor/<int:sid>/attendance', methods=['POST'])
    @login_required
    def hdc_subcontractor_attendance(sid):
        sub = Subcontractor.query.get_or_404(sid)
        att_date = _parse_date(request.form.get('date'))
        present_count = request.form.get('present_count', type=int)
        work_done_pct = _flt(request.form.get('work_done_pct'))
        notes = (request.form.get('notes', '') or '').strip()

        if present_count is None:
            present_count = 0
        present_count = max(0, int(present_count))
        work_done_pct = max(0.0, min(100.0, float(work_done_pct or 0.0)))

        row = SubcontractAttendance.query.filter_by(subcontractor_id=sid, date=att_date).first()
        if row:
            old_pc = int(row.present_count or 0)
            old_pct = float(row.work_done_pct or 0.0)
            row.present_count = present_count
            row.work_done_pct = work_done_pct
            row.notes = notes
            row.activity_at = _activity_at_for(att_date)
            msg = 'Subcontract attendance updated.'
        else:
            db.session.add(SubcontractAttendance(
                subcontractor_id=sid,
                date=att_date,
                present_count=present_count,
                work_done_pct=work_done_pct,
                notes=notes,
                activity_at=_activity_at_for(att_date)
            ))
            msg = 'Subcontract attendance recorded.'
            old_pc = 0
            old_pct = 0.0
        _log_subcontract_event(
            sub=sub,
            event_type='attendance',
            from_value=f'P{old_pc} +{old_pct:.2f}%',
            to_value=f'P{present_count} +{work_done_pct:.2f}%',
            notes=f'{att_date.isoformat()} | {notes or "-"}'
        )
        db.session.commit()
        flash(msg, 'success')
        if sub.project_id:
            return redirect(url_for('hdc_project_detail', pid=sub.project_id))
        return redirect(url_for('hdc_subcontractor_ledger', sid=sub.id))


    @app.route('/hdc/subcontractor/<int:sid>/labour_attendance', methods=['POST'])
    @login_required
    def hdc_subcontractor_labour_attendance(sid):
        sub = Subcontractor.query.get_or_404(sid)
        return_to = (request.form.get('return_to') or '').strip().lower()
        att_date = _parse_date(request.form.get('date'))

        def _sub_scope_ids():
            scope_stage_ids = set()
            scope_project_ids = set()
            if sub.project_id:
                scope_project_ids.add(int(sub.project_id))
                stage_ids = (db.session.query(Stage.id)
                             .filter(Stage.project_id == sub.project_id)
                             .all())
                scope_stage_ids.update(int(sidv) for (sidv,) in stage_ids if sidv)
            if sub.stage_id:
                scope_stage_ids.add(int(sub.stage_id))
                sub_stage = Stage.query.get(sub.stage_id)
                if sub_stage and sub_stage.project_id:
                    scope_project_ids.add(int(sub_stage.project_id))
            assigned_stage_rows = (Stage.query
                                   .filter(Stage.assigned_subcontractor_id == sub.id)
                                   .all())
            for srow in assigned_stage_rows:
                scope_stage_ids.add(int(srow.id))
                if srow.project_id:
                    scope_project_ids.add(int(srow.project_id))
            hist_stage_ids = (db.session.query(SubcontractLabourAttendance.stage_id)
                              .filter(SubcontractLabourAttendance.subcontractor_id == sub.id)
                              .distinct()
                              .all())
            for (sidv,) in hist_stage_ids:
                if sidv:
                    scope_stage_ids.add(int(sidv))
            if scope_stage_ids:
                hist_stage_rows = Stage.query.filter(Stage.id.in_(list(scope_stage_ids))).all()
                for srow in hist_stage_rows:
                    if srow.project_id:
                        scope_project_ids.add(int(srow.project_id))
            return scope_project_ids, scope_stage_ids

        scope_project_ids, scope_stage_ids = _sub_scope_ids()

        if (request.form.get('bulk_mode') or '').strip() == '1':
            worker_ids_raw = request.form.getlist('worker_ids')
            scoped_workers = (SubcontractLabourWorker.query
                              .filter(SubcontractLabourWorker.subcontractor_id == sub.id)
                              .all())
            worker_map = {int(w.id): w for w in scoped_workers}
            updated_rows = 0
            skipped_rows = 0

            for wid_raw in worker_ids_raw:
                try:
                    wid = int(wid_raw)
                except Exception:
                    skipped_rows += 1
                    continue

                labour_worker = worker_map.get(wid)
                if not labour_worker:
                    skipped_rows += 1
                    continue

                row_status = (request.form.get(f'attendance_status_{wid}') or 'not_assigned').strip().lower()
                if row_status not in ('present', 'absent', 'not_assigned'):
                    row_status = 'not_assigned'
                notes = (request.form.get(f'notes_{wid}') or '').strip()
                project_id = request.form.get(f'project_id_{wid}', type=int)
                stage_id = request.form.get(f'stage_id_{wid}', type=int)
                entered_hours = max(0.0, float(_flt(request.form.get(f'working_hours_{wid}'), 0.0) or 0.0))
                existing_rows = (SubcontractLabourAttendance.query
                                 .filter(SubcontractLabourAttendance.subcontractor_id == sub.id,
                                         SubcontractLabourAttendance.worker_id == labour_worker.id,
                                         SubcontractLabourAttendance.date == att_date)
                                 .order_by(SubcontractLabourAttendance.id.asc())
                                 .all())

                if row_status == 'not_assigned':
                    for er in existing_rows:
                        db.session.delete(er)
                    updated_rows += 1
                    continue

                if not project_id or not stage_id:
                    skipped_rows += 1
                    continue
                stg = Stage.query.get(stage_id)
                if (not stg) or (int(stg.project_id or 0) != int(project_id or 0)):
                    skipped_rows += 1
                    continue
                if scope_project_ids and int(project_id) not in scope_project_ids:
                    skipped_rows += 1
                    continue
                if scope_stage_ids and int(stg.id) not in scope_stage_ids:
                    skipped_rows += 1
                    continue
                if row_status == 'present' and entered_hours <= 0:
                    skipped_rows += 1
                    continue

                total_hours = entered_hours if row_status == 'present' else 0.0
                regular_hours = min(8.0, total_hours)
                overtime_hours = max(0.0, total_hours - 8.0)
                labour_count = 1 if row_status == 'present' else 0
                wage_rate = max(0.0, float(labour_worker.daily_wage or 0.0))
                total_labour_paid = wage_rate if row_status == 'present' else 0.0
                attendance_status = row_status.capitalize()

                if existing_rows:
                    row = existing_rows[0]
                    old_count = int(row.labour_count or 0)
                    old_paid = float(row.total_labour_paid or 0.0)
                    for extra in existing_rows[1:]:
                        db.session.delete(extra)
                    row.project_id = project_id
                    row.stage_id = stg.id
                    row.worker_id = labour_worker.id
                    row.labour_count = labour_count
                    row.wage_rate = wage_rate
                    row.total_labour_paid = total_labour_paid
                    row.attendance_status = attendance_status
                    row.working_hours = regular_hours
                    row.overtime_hours = overtime_hours
                    row.notes = notes
                    row.activity_at = _activity_at_for(att_date)
                    row.updated_at = _pkt_now_naive()
                else:
                    old_count = 0
                    old_paid = 0.0
                    db.session.add(SubcontractLabourAttendance(
                        subcontractor_id=sub.id,
                        project_id=project_id,
                        stage_id=stg.id,
                        worker_id=labour_worker.id,
                        date=att_date,
                        labour_count=labour_count,
                        wage_rate=wage_rate,
                        total_labour_paid=total_labour_paid,
                        attendance_status=attendance_status,
                        working_hours=regular_hours,
                        overtime_hours=overtime_hours,
                        notes=notes,
                        activity_at=_activity_at_for(att_date),
                        created_at=_pkt_now_naive(),
                        updated_at=_pkt_now_naive()
                    ))

                _log_subcontract_event(
                    sub=sub,
                    event_type='labour_attendance',
                    from_value=f'L{old_count} | Cost {old_paid:.2f}',
                    to_value=f'L{labour_count} | Cost {total_labour_paid:.2f}',
                    amount=total_labour_paid,
                    notes=f'{att_date.isoformat()} | {stg.name} | Worker: {labour_worker.name} | {attendance_status} | Hrs:{regular_hours:.2f} OT:{overtime_hours:.2f} | {notes or "-"}',
                    project_id=project_id,
                    stage_id=stg.id
                )
                updated_rows += 1

            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                flash('Duplicate labour attendance detected. Existing rows were kept unchanged.', 'warning')
                return redirect(url_for('hdc_subcontractor_attendance_page', sid=sub.id))
            flash(f'Register saved for {updated_rows} worker(s). Skipped {skipped_rows} invalid row(s).', 'success')
            return redirect(url_for('hdc_subcontractor_attendance_page', sid=sub.id, sheet_date=att_date.isoformat()))

        project_id = request.form.get('project_id', type=int)
        stage_id = request.form.get('stage_id', type=int)
        worker_id = request.form.get('worker_id', type=int)
        labour_count = request.form.get('labour_count', type=int)
        wage_rate = _flt(request.form.get('wage_rate'))
        total_labour_paid = _flt(request.form.get('total_labour_paid'))
        attendance_status = (request.form.get('attendance_status', 'Present') or 'Present').strip().lower()
        working_hours = _flt(request.form.get('working_hours'))
        notes = (request.form.get('notes', '') or '').strip()

        if labour_count is None:
            labour_count = 0
        labour_count = max(0, int(labour_count))
        wage_rate = max(0.0, float(wage_rate or 0.0))
        total_labour_paid = max(0.0, float(total_labour_paid or 0.0))
        total_hours = max(0.0, float(working_hours or 0.0))
        working_hours = min(8.0, total_hours)
        overtime_hours = max(0.0, total_hours - 8.0)
        if attendance_status not in ('present', 'absent', 'not_assigned'):
            attendance_status = 'present'
        attendance_status = attendance_status.capitalize()
        if attendance_status == 'Not_assigned':
            attendance_status = 'Not Assigned'

        if attendance_status == 'Not Assigned':
            flash('Use the register sheet to mark Not Assigned.', 'warning')
            return redirect(url_for('hdc_subcontractor_attendance_page', sid=sub.id))

        if not project_id:
            flash('Project is required for labour attendance.', 'warning')
            return redirect(url_for('hdc_subcontractor_attendance_page', sid=sub.id))
        if not stage_id:
            flash('Stage is required for labour attendance.', 'warning')
            return redirect(url_for('hdc_subcontractor_attendance_page', sid=sub.id))

        stg = Stage.query.get(stage_id) if stage_id else None
        if not stg:
            flash('No stage selected for labour attendance.', 'warning')
            return redirect(url_for('hdc_subcontractor_attendance_page', sid=sub.id))
        if int(stg.project_id or 0) != int(project_id or 0):
            flash('Selected stage does not belong to selected project.', 'danger')
            return redirect(url_for('hdc_subcontractor_attendance_page', sid=sub.id))

        if scope_project_ids and int(project_id) not in scope_project_ids:
            flash('Selected project is outside subcontractor assigned scope.', 'danger')
            return redirect(url_for('hdc_subcontractor_attendance_page', sid=sub.id))
        if scope_stage_ids and int(stg.id) not in scope_stage_ids:
            flash('Selected stage is outside subcontractor assigned scope.', 'danger')
            return redirect(url_for('hdc_subcontractor_attendance_page', sid=sub.id))

        labour_worker = None
        if worker_id:
            labour_worker = SubcontractLabourWorker.query.get(worker_id)
            if not labour_worker or labour_worker.subcontractor_id != sub.id:
                flash('Selected labour worker is invalid for this subcontractor.', 'danger')
                return redirect(url_for('hdc_subcontractor_attendance_page', sid=sub.id))

        if total_labour_paid <= 0 and labour_count > 0 and wage_rate > 0:
            total_labour_paid = float(labour_count) * float(wage_rate)
        if wage_rate <= 0 and labour_count > 0 and total_labour_paid > 0:
            wage_rate = float(total_labour_paid) / float(labour_count)

        row = SubcontractLabourAttendance.query.filter_by(
            subcontractor_id=sub.id,
            stage_id=stg.id,
            worker_id=labour_worker.id if labour_worker else None,
            date=att_date
        ).first()
        if labour_worker:
            conflict_q = SubcontractLabourAttendance.query.filter(
                SubcontractLabourAttendance.subcontractor_id == sub.id,
                SubcontractLabourAttendance.worker_id == labour_worker.id,
                SubcontractLabourAttendance.date == att_date
            )
            if row:
                conflict_q = conflict_q.filter(SubcontractLabourAttendance.id != row.id)
            conflict_row = conflict_q.first()
            if conflict_row:
                conflict_stage = conflict_row.stage.name if conflict_row.stage else f'Stage#{conflict_row.stage_id}'
                flash(
                    f'Duplicate not allowed: "{labour_worker.name}" already has attendance on {att_date.isoformat()} in {conflict_stage}.',
                    'warning'
                )
                return redirect(url_for('hdc_subcontractor_attendance_page', sid=sub.id))
        if row:
            old_count = int(row.labour_count or 0)
            old_paid = float(row.total_labour_paid or 0.0)
            row.project_id = project_id
            row.worker_id = labour_worker.id if labour_worker else None
            row.labour_count = labour_count
            row.wage_rate = wage_rate
            row.total_labour_paid = total_labour_paid
            row.attendance_status = attendance_status
            row.working_hours = working_hours
            row.overtime_hours = overtime_hours
            row.notes = notes
            row.activity_at = _activity_at_for(att_date)
            row.updated_at = _pkt_now_naive()
            msg = 'Subcontract labour attendance updated.'
        else:
            if _has_recent_duplicate(
                SubcontractLabourAttendance,
                subcontractor_id=sub.id,
                project_id=project_id,
                stage_id=stg.id,
                worker_id=labour_worker.id if labour_worker else None,
                date=att_date,
                labour_count=labour_count,
                wage_rate=wage_rate,
                total_labour_paid=total_labour_paid,
                attendance_status=attendance_status,
                working_hours=working_hours,
                overtime_hours=overtime_hours,
                notes=notes
            ):
                flash('Duplicate subcontract labour attendance prevented (same values submitted too quickly).', 'warning')
                return redirect(url_for('hdc_subcontractor_attendance_page', sid=sub.id))
            db.session.add(SubcontractLabourAttendance(
                subcontractor_id=sub.id,
                project_id=project_id,
                stage_id=stg.id,
                worker_id=labour_worker.id if labour_worker else None,
                date=att_date,
                labour_count=labour_count,
                wage_rate=wage_rate,
                total_labour_paid=total_labour_paid,
                attendance_status=attendance_status,
                working_hours=working_hours,
                overtime_hours=overtime_hours,
                notes=notes,
                activity_at=_activity_at_for(att_date),
                created_at=_pkt_now_naive(),
                updated_at=_pkt_now_naive()
            ))
            old_count = 0
            old_paid = 0.0
            msg = 'Subcontract labour attendance recorded.'

        _log_subcontract_event(
            sub=sub,
            event_type='labour_attendance',
            from_value=f'L{old_count} | Cost {old_paid:.2f}',
            to_value=f'L{labour_count} | Cost {total_labour_paid:.2f}',
            amount=total_labour_paid,
            notes=f'{att_date.isoformat()} | {stg.name} | Worker: {(labour_worker.name if labour_worker else "Bulk Entry")} | {attendance_status} | Hrs:{working_hours:.2f} OT:{overtime_hours:.2f} | {notes or "-"}',
            project_id=project_id,
            stage_id=stg.id
        )
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash('Duplicate labour attendance detected. Existing row was kept unchanged.', 'warning')
            return redirect(url_for('hdc_subcontractor_attendance_page', sid=sub.id))
        flash(msg, 'success')
        if return_to == 'ledger':
            return redirect(url_for('hdc_subcontractor_ledger', sid=sub.id))
        return redirect(url_for('hdc_subcontractor_attendance_page', sid=sub.id))


    @app.route('/hdc/subcontractor/<int:sid>/labour_attendance/<int:rid>/delete', methods=['POST'])
    @login_required
    def hdc_subcontractor_labour_attendance_delete(sid, rid):
        sub = Subcontractor.query.get_or_404(sid)
        row = SubcontractLabourAttendance.query.get_or_404(rid)
        if row.subcontractor_id != sub.id:
            flash('Attendance row does not belong to this subcontractor.', 'danger')
            return redirect(url_for('hdc_subcontractor_attendance_page', sid=sub.id))
        old_count = int(row.labour_count or 0)
        old_paid = float(row.total_labour_paid or 0.0)
        old_date = row.date
        old_stage_id = row.stage_id
        old_stage_name = row.stage.name if row.stage else f'Stage#{row.stage_id}'
        old_worker_name = row.worker.name if row.worker else 'Bulk Entry'
        db.session.delete(row)
        _log_subcontract_event(
            sub=sub,
            event_type='labour_attendance_delete',
            from_value=f'L{old_count} | Cost {old_paid:.2f}',
            to_value='Deleted',
            amount=0.0,
            notes=f'{old_date.isoformat() if old_date else "-"} | {old_stage_name} | Worker: {old_worker_name} | Attendance row deleted',
            project_id=sub.project_id,
            stage_id=old_stage_id
        )
        db.session.commit()
        flash('Subcontract labour attendance entry deleted.', 'success')
        return redirect(url_for('hdc_subcontractor_attendance_page', sid=sub.id))


    @app.route('/hdc/subcontractor/<int:sid>/workers', methods=['POST'])
    @login_required
    def hdc_subcontractor_add_worker(sid):
        sub = Subcontractor.query.get_or_404(sid)
        name = (request.form.get('name') or '').strip()
        phone = (request.form.get('phone') or '').strip()
        trade = (request.form.get('trade') or '').strip()
        daily_wage = max(0.0, _flt(request.form.get('daily_wage')))
        if not name:
            flash('Worker name is required.', 'warning')
            return redirect(url_for('hdc_subcontractor_attendance_page', sid=sub.id))
        exists = SubcontractLabourWorker.query.filter(
            SubcontractLabourWorker.subcontractor_id == sub.id,
            func.lower(SubcontractLabourWorker.name) == name.lower(),
            SubcontractLabourWorker.active_status == True
        ).first()
        if exists:
            flash('A labour worker with this name already exists for this subcontractor.', 'warning')
            return redirect(url_for('hdc_subcontractor_attendance_page', sid=sub.id))

        w = SubcontractLabourWorker(
            subcontractor_id=sub.id,
            name=name,
            phone=phone,
            trade=trade,
            daily_wage=daily_wage,
            active_status=True
        )
        db.session.add(w)
        _log_subcontract_event(
            sub=sub,
            event_type='labour_worker_add',
            to_value=name,
            notes=f'Labour worker added | trade={trade or "-"} | wage={daily_wage:.2f}'
        )
        db.session.commit()
        flash(f'Labour worker "{name}" added.', 'success')
        return redirect(url_for('hdc_subcontractor_attendance_page', sid=sub.id))


    @app.route('/hdc/subcontractor/<int:sid>/workers/<int:wid>/edit', methods=['POST'])
    @login_required
    def hdc_subcontractor_worker_edit(sid, wid):
        sub = Subcontractor.query.get_or_404(sid)
        w = SubcontractLabourWorker.query.get_or_404(wid)
        if w.subcontractor_id != sub.id:
            flash('Worker does not belong to this subcontractor.', 'danger')
            return redirect(url_for('hdc_subcontractor_attendance_page', sid=sub.id))
        old_name = w.name or ''
        old_trade = w.trade or ''
        old_wage = float(w.daily_wage or 0.0)
        name = (request.form.get('name') or '').strip()
        if not name:
            flash('Worker name is required.', 'warning')
            return redirect(url_for('hdc_subcontractor_attendance_page', sid=sub.id))
        w.name = name
        w.phone = (request.form.get('phone') or '').strip()
        w.trade = (request.form.get('trade') or '').strip()
        w.daily_wage = max(0.0, _flt(request.form.get('daily_wage')))
        _log_subcontract_event(
            sub=sub,
            event_type='labour_worker_edit',
            from_value=f'{old_name}|{old_trade}|{old_wage:.2f}',
            to_value=f'{w.name}|{w.trade or "-"}|{float(w.daily_wage or 0.0):.2f}',
            notes='Labour worker profile edited'
        )
        db.session.commit()
        flash(f'Worker "{w.name}" updated.', 'success')
        return redirect(url_for('hdc_subcontractor_attendance_page', sid=sub.id))


    @app.route('/hdc/subcontractor/<int:sid>/workers/<int:wid>/toggle', methods=['POST'])
    @login_required
    def hdc_subcontractor_worker_toggle(sid, wid):
        sub = Subcontractor.query.get_or_404(sid)
        w = SubcontractLabourWorker.query.get_or_404(wid)
        if w.subcontractor_id != sub.id:
            flash('Worker does not belong to this subcontractor.', 'danger')
            return redirect(url_for('hdc_subcontractor_attendance_page', sid=sub.id))
        w.active_status = not bool(w.active_status)
        _log_subcontract_event(
            sub=sub,
            event_type='labour_worker_status',
            from_value='active' if not w.active_status else 'suspended',
            to_value='active' if w.active_status else 'suspended',
            notes=f'Worker {w.name}'
        )
        db.session.commit()
        flash(f'Worker "{w.name}" is now {"active" if w.active_status else "suspended"}.', 'success')
        return redirect(url_for('hdc_subcontractor_attendance_page', sid=sub.id))


    @app.route('/hdc/subcontractor/<int:sid>/workers/<int:wid>/pay', methods=['POST'])
    @login_required
    def hdc_subcontractor_worker_pay(sid, wid):
        sub = Subcontractor.query.get_or_404(sid)
        w = SubcontractLabourWorker.query.get_or_404(wid)
        if w.subcontractor_id != sub.id:
            flash('Worker does not belong to this subcontractor.', 'danger')
            return redirect(url_for('hdc_subcontractor_attendance_page', sid=sub.id))
        amount = max(0.0, _flt(request.form.get('amount')))
        pay_date = _parse_date(request.form.get('date'))
        notes = (request.form.get('notes') or '').strip()
        if amount <= 0:
            flash('Payment amount must be greater than zero.', 'warning')
            return redirect(url_for('hdc_subcontractor_worker_ledger', sid=sub.id, wid=w.id))
        if amount > (w.payable_balance or 0.0) + 0.01:
            flash('Payment exceeds worker payable balance.', 'danger')
            return redirect(url_for('hdc_subcontractor_worker_ledger', sid=sub.id, wid=w.id))
        if _has_recent_duplicate(
            SubcontractLabourPayment,
            subcontractor_id=sub.id,
            worker_id=w.id,
            amount=amount,
            date=pay_date,
            notes=notes
        ):
            flash('Duplicate worker payment prevented (same values submitted too quickly).', 'warning')
            return redirect(url_for('hdc_subcontractor_worker_ledger', sid=sub.id, wid=w.id))
        pay_entry = SubcontractLabourPayment(
            subcontractor_id=sub.id,
            worker_id=w.id,
            amount=amount,
            date=pay_date,
            notes=notes,
            is_void=False,
            activity_at=_activity_at_for(pay_date)
        )
        db.session.add(pay_entry)
        db.session.flush()
        ok_txn, msg_txn, _ = _accounts_post_subcontract_labour_payment_row(
            pay_entry, worker_name=w.name, subcontractor_name=sub.name, commit=False
        )
        if not ok_txn:
            db.session.rollback()
            flash(msg_txn or 'Unable to post labour payment in unified accounts.', 'danger')
            return redirect(url_for('hdc_subcontractor_worker_ledger', sid=sub.id, wid=w.id))
        _log_subcontract_event(
            sub=sub,
            event_type='labour_worker_payment',
            amount=amount,
            notes=f'Worker {w.name} | {notes or "-"}'
        )
        db.session.commit()
        flash(f'Payment recorded for {w.name}.', 'success')
        return redirect(url_for('hdc_subcontractor_worker_ledger', sid=sub.id, wid=w.id))


    @app.route('/hdc/subcontractor/<int:sid>/workers/<int:wid>/ledger')
    @login_required
    def hdc_subcontractor_worker_ledger(sid, wid):
        sub = Subcontractor.query.get_or_404(sid)
        w = SubcontractLabourWorker.query.get_or_404(wid)
        if w.subcontractor_id != sub.id:
            flash('Worker does not belong to this subcontractor.', 'danger')
            return redirect(url_for('hdc_subcontractor_attendance_page', sid=sub.id))
        from_raw = (request.args.get('date_from') or '').strip()
        to_raw = (request.args.get('date_to') or '').strip()
        date_from = _parse_date(from_raw, fallback=None) if from_raw else None
        date_to = _parse_date(to_raw, fallback=None) if to_raw else None
        if date_from and date_to and date_from > date_to:
            date_from, date_to = date_to, date_from
        aq = SubcontractLabourAttendance.query.filter(
            SubcontractLabourAttendance.subcontractor_id == sub.id,
            SubcontractLabourAttendance.worker_id == w.id
        )
        pq = SubcontractLabourPayment.query.filter(
            SubcontractLabourPayment.subcontractor_id == sub.id,
            SubcontractLabourPayment.worker_id == w.id
        )
        if date_from:
            aq = aq.filter(SubcontractLabourAttendance.date >= date_from)
            pq = pq.filter(SubcontractLabourPayment.date >= date_from)
        if date_to:
            aq = aq.filter(SubcontractLabourAttendance.date <= date_to)
            pq = pq.filter(SubcontractLabourPayment.date <= date_to)
        attendance_rows = aq.order_by(SubcontractLabourAttendance.date.desc(), SubcontractLabourAttendance.id.desc()).all()
        payment_rows = pq.order_by(SubcontractLabourPayment.date.desc(), SubcontractLabourPayment.id.desc()).all()
        earned = sum(float(r.total_labour_paid or 0.0) for r in attendance_rows)
        paid = sum(float(p.amount or 0.0) for p in payment_rows)
        payable = max(0.0, earned - paid)
        return render_template('subcontractors/subcontractor_worker_ledger.html',
            sub=sub,
            worker=w,
            attendance_rows=attendance_rows,
            payment_rows=payment_rows,
            earned=earned,
            paid=paid,
            payable=payable,
            selected_from=from_raw,
            selected_to=to_raw
        )


    @app.route('/hdc/subcontractor/<int:sid>/attendance_page')
    @login_required
    def hdc_subcontractor_attendance_page(sid):
        sub = Subcontractor.query.get_or_404(sid)
        filter_worker_id = request.args.get('worker_id', type=int)
        show_inactive = request.args.get('show_inactive', type=int) == 1
        sheet_date = _parse_date(request.args.get('sheet_date'), fallback=_pkt_today())
        status_view = (request.args.get('status_view') or 'assigned').strip().lower()
        if status_view not in ('assigned', 'absent', 'not_assigned'):
            status_view = 'assigned'
        status_project_id = request.args.get('status_project_id', type=int)
        status_stage_id = request.args.get('status_stage_id', type=int)
        raw_from = (request.args.get('date_from') or '').strip()
        raw_to = (request.args.get('date_to') or '').strip()
        date_from = _parse_date(raw_from, fallback=None) if raw_from else None
        date_to = _parse_date(raw_to, fallback=None) if raw_to else None
        if date_from and date_to and date_from > date_to:
            date_from, date_to = date_to, date_from
        scope_stage_ids = set()
        scope_project_ids = set()
        if sub.project_id:
            scope_project_ids.add(int(sub.project_id))
            sub_project_stage_ids = (db.session.query(Stage.id)
                                     .filter(Stage.project_id == sub.project_id)
                                     .all())
            scope_stage_ids.update(int(sidv) for (sidv,) in sub_project_stage_ids if sidv)
        if sub.stage_id:
            scope_stage_ids.add(int(sub.stage_id))
            stg = Stage.query.get(sub.stage_id)
            if stg and stg.project_id:
                scope_project_ids.add(int(stg.project_id))
        assigned_stages = (Stage.query
                           .filter(Stage.assigned_subcontractor_id == sub.id)
                           .all())
        for srow in assigned_stages:
            scope_stage_ids.add(int(srow.id))
            if srow.project_id:
                scope_project_ids.add(int(srow.project_id))
        hist_stage_ids = (db.session.query(SubcontractLabourAttendance.stage_id)
                          .filter(SubcontractLabourAttendance.subcontractor_id == sub.id)
                          .distinct()
                          .all())
        for (sidv,) in hist_stage_ids:
            if sidv:
                scope_stage_ids.add(int(sidv))
        if scope_stage_ids:
            hist_stages = Stage.query.filter(Stage.id.in_(list(scope_stage_ids))).all()
            for srow in hist_stages:
                if srow.project_id:
                    scope_project_ids.add(int(srow.project_id))

        stage_options = []
        if scope_stage_ids:
            stage_options = (Stage.query
                             .filter(Stage.id.in_(list(scope_stage_ids)))
                             .order_by(Stage.project_id.asc(), Stage.id.asc())
                             .all())
        project_options = []
        if scope_project_ids:
            project_options = (Project.query
                               .filter(Project.id.in_(list(scope_project_ids)))
                               .order_by(Project.name.asc(), Project.id.asc())
                               .all())
        selected_project_id = None
        if sub.project_id and int(sub.project_id) in scope_project_ids:
            selected_project_id = int(sub.project_id)
        elif sub.stage_id:
            sub_stage = Stage.query.get(sub.stage_id)
            if sub_stage and sub_stage.project_id and int(sub_stage.project_id) in scope_project_ids:
                selected_project_id = int(sub_stage.project_id)
        if selected_project_id is None and project_options:
            selected_project_id = int(project_options[0].id)
        wq = SubcontractLabourWorker.query.filter(SubcontractLabourWorker.subcontractor_id == sub.id)
        if not show_inactive:
            wq = wq.filter(SubcontractLabourWorker.active_status == True)
        labour_workers = wq.order_by(SubcontractLabourWorker.name.asc(), SubcontractLabourWorker.id.asc()).all()
        all_workers = (SubcontractLabourWorker.query
                       .filter(SubcontractLabourWorker.subcontractor_id == sub.id)
                       .order_by(SubcontractLabourWorker.name.asc(), SubcontractLabourWorker.id.asc())
                       .all())
        lq = SubcontractLabourAttendance.query.filter(SubcontractLabourAttendance.subcontractor_id == sub.id)
        if filter_worker_id:
            lq = lq.filter(SubcontractLabourAttendance.worker_id == filter_worker_id)
        if date_from:
            lq = lq.filter(SubcontractLabourAttendance.date >= date_from)
        if date_to:
            lq = lq.filter(SubcontractLabourAttendance.date <= date_to)
        labour_rows = lq.order_by(SubcontractLabourAttendance.date.desc(), SubcontractLabourAttendance.id.desc()).all()
        earned_q = db.session.query(
            SubcontractLabourAttendance.worker_id,
            func.coalesce(func.sum(SubcontractLabourAttendance.total_labour_paid), 0.0)
        ).filter(
            SubcontractLabourAttendance.subcontractor_id == sub.id,
            SubcontractLabourAttendance.worker_id.isnot(None)
        )
        paid_q = db.session.query(
            SubcontractLabourPayment.worker_id,
            func.coalesce(func.sum(SubcontractLabourPayment.amount), 0.0)
        ).filter(
            SubcontractLabourPayment.subcontractor_id == sub.id
        )
        if date_from:
            earned_q = earned_q.filter(SubcontractLabourAttendance.date >= date_from)
            paid_q = paid_q.filter(SubcontractLabourPayment.date >= date_from)
        if date_to:
            earned_q = earned_q.filter(SubcontractLabourAttendance.date <= date_to)
            paid_q = paid_q.filter(SubcontractLabourPayment.date <= date_to)
        earned_map = {
            int(wid): float(total or 0.0)
            for wid, total in earned_q.group_by(SubcontractLabourAttendance.worker_id).all()
            if wid
        }
        paid_map = {
            int(wid): float(total or 0.0)
            for wid, total in paid_q.group_by(SubcontractLabourPayment.worker_id).all()
            if wid
        }
        worker_stats = []
        for w in all_workers:
            earned = float(earned_map.get(w.id, 0.0) or 0.0)
            paid = float(paid_map.get(w.id, 0.0) or 0.0)
            worker_stats.append({'worker': w, 'earned': earned, 'paid': paid, 'payable': max(0.0, earned - paid)})

        sheet_worker_ids = [int(w.id) for w in labour_workers]
        day_rows = (SubcontractLabourAttendance.query
                    .filter(SubcontractLabourAttendance.subcontractor_id == sub.id,
                            SubcontractLabourAttendance.date == sheet_date,
                            SubcontractLabourAttendance.worker_id.in_(sheet_worker_ids))
                    .order_by(SubcontractLabourAttendance.id.desc())
                    .all()) if sheet_worker_ids else []
        day_map = {}
        for r in day_rows:
            if not r.worker_id:
                continue
            wid = int(r.worker_id)
            if wid not in day_map:
                day_map[wid] = r

        daily_sheet_rows = []
        for w in labour_workers:
            r = day_map.get(int(w.id))
            if r:
                status_txt = (r.attendance_status or 'Present').strip()
                normalized = status_txt.lower()
                if normalized not in ('present', 'absent', 'not assigned'):
                    status_txt = 'Absent'
                total_hours = float(r.working_hours or 0.0) + float(r.overtime_hours or 0.0)
                project_id = int(r.project_id) if r.project_id else None
                stage_id = int(r.stage_id) if r.stage_id else None
                remarks_txt = (r.notes or '')
            else:
                status_txt = 'Not Assigned'
                total_hours = 0.0
                project_id = None
                stage_id = None
                remarks_txt = ''
            daily_sheet_rows.append({
                'worker': w,
                'status': status_txt,
                'project_id': project_id,
                'stage_id': stage_id,
                'working_hours': total_hours,
                'remarks': remarks_txt
            })

        assigned_count = 0
        absent_count = 0
        not_assigned_count = 0
        status_rows = []
        project_map = {int(p.id): p for p in project_options}
        stage_map = {int(s.id): s for s in stage_options}
        for row in daily_sheet_rows:
            worker = row['worker']
            status_txt = (row.get('status') or 'Not Assigned').strip()
            normalized = status_txt.lower()
            if normalized == 'present':
                bucket = 'assigned'
                assigned_count += 1
            elif normalized == 'absent':
                bucket = 'absent'
                absent_count += 1
            else:
                bucket = 'not_assigned'
                not_assigned_count += 1

            if status_project_id and int(row.get('project_id') or 0) != int(status_project_id):
                continue
            if status_stage_id and int(row.get('stage_id') or 0) != int(status_stage_id):
                continue
            if bucket != status_view:
                continue

            p = project_map.get(int(row['project_id'])) if row.get('project_id') else None
            s = stage_map.get(int(row['stage_id'])) if row.get('stage_id') else None
            total_hours = float(row.get('working_hours') or 0.0)
            status_rows.append({
                'worker_id': int(worker.id),
                'worker_name': worker.name,
                'worker_trade': worker.trade or '-',
                'status': 'Assigned' if bucket == 'assigned' else ('Absent' if bucket == 'absent' else 'Not Assigned'),
                'project_id': int(row['project_id']) if row.get('project_id') else None,
                'stage_id': int(row['stage_id']) if row.get('stage_id') else None,
                'project_name': p.name if p else '-',
                'stage_name': s.name if s else '-',
                'working_hours': min(8.0, total_hours),
                'overtime_hours': max(0.0, total_hours - 8.0),
                'remarks': row.get('remarks') or '-'
            })

        return render_template('subcontractors/subcontractor_attendance.html',
            sub=sub,
            sheet_date=sheet_date.isoformat(),
            status_view=status_view,
            status_project_id=status_project_id,
            status_stage_id=status_stage_id,
            assigned_count=assigned_count,
            absent_count=absent_count,
            not_assigned_count=not_assigned_count,
            status_rows=status_rows,
            project_options=project_options,
            selected_project_id=selected_project_id,
            stage_options=stage_options,
            labour_workers=labour_workers,
            all_workers=all_workers,
            daily_sheet_rows=daily_sheet_rows,
            worker_stats=worker_stats,
            labour_rows=labour_rows,
            selected_worker_id=filter_worker_id,
            selected_from=raw_from,
            selected_to=raw_to,
            show_inactive=show_inactive
        )


    @app.route('/hdc/subcontractor/<int:sid>/progress', methods=['POST'])
    @login_required
    def hdc_subcontractor_progress(sid):
        sub = Subcontractor.query.get_or_404(sid)
        pct = _flt(request.form.get('work_done_percentage'))
        old_pct = float(sub.work_done_percentage or 0.0)
        sub.work_done_percentage = max(0.0, min(100.0, float(pct or 0.0)))
        _log_subcontract_event(
            sub=sub,
            event_type='progress',
            from_value=f'{old_pct:.2f}%',
            to_value=f'{float(sub.work_done_percentage or 0.0):.2f}%',
            notes='Manual progress update'
        )
        db.session.commit()
        flash(f'Work done updated for {sub.name}.', 'success')
        if sub.project_id:
            return redirect(url_for('hdc_project_detail', pid=sub.project_id))
        return redirect(url_for('hdc_subcontractors'))


    @app.route('/hdc/subcontractor/<int:sid>/ledger')
    @login_required
    def hdc_subcontractor_ledger(sid):
        _ensure_subcontract_labour_attendance_schema()
        sub = Subcontractor.query.get_or_404(sid)
        event_type = (request.args.get('event_type') or '').strip().lower()
        stage_filter_id = request.args.get('stage_id', type=int)
        date_from = None
        date_to = None
        raw_from = (request.args.get('date_from') or '').strip()
        raw_to = (request.args.get('date_to') or '').strip()
        try:
            if raw_from:
                date_from = datetime.strptime(raw_from, '%Y-%m-%d').date()
        except Exception:
            date_from = None
        try:
            if raw_to:
                date_to = datetime.strptime(raw_to, '%Y-%m-%d').date()
        except Exception:
            date_to = None
        if date_from and date_to and date_from > date_to:
            date_from, date_to = date_to, date_from

        q = SubcontractEvent.query.filter(SubcontractEvent.subcontractor_id == sub.id)
        if event_type:
            q = q.filter(func.lower(SubcontractEvent.event_type) == event_type)
        if date_from:
            q = q.filter(SubcontractEvent.created_at >= datetime.combine(date_from, datetime.min.time()))
        if date_to:
            q = q.filter(SubcontractEvent.created_at <= datetime.combine(date_to, datetime.max.time()))
        rows = q.order_by(SubcontractEvent.created_at.asc(), SubcontractEvent.id.asc()).all()
        q_base = SubcontractEvent.query.filter(SubcontractEvent.subcontractor_id == sub.id)
        if date_from:
            q_base = q_base.filter(SubcontractEvent.created_at >= datetime.combine(date_from, datetime.min.time()))
        if date_to:
            q_base = q_base.filter(SubcontractEvent.created_at <= datetime.combine(date_to, datetime.max.time()))
        base_rows = q_base.order_by(SubcontractEvent.created_at.asc(), SubcontractEvent.id.asc()).all()
        type_map = {
            'create': 'Profile Created',
            'shift': 'Shift Assigned',
            'reassign': 'Stage Reassigned',
            'unassign': 'Shift Back To Company',
            'status': 'Stage Status',
            'payment': 'Payment',
            'tip': 'Tip',
            'settlement': 'Settlement',
            'attendance': 'Attendance',
            'labour_attendance': 'Labour Attendance',
            'labour_worker_add': 'Worker Added',
            'labour_worker_edit': 'Worker Edited',
            'labour_worker_status': 'Worker Status',
            'labour_worker_payment': 'Worker Payment',
            'progress': 'Progress',
            'price_update': 'Price Update'
        }
        events = []
        for r in rows:
            detail_parts = []
            if r.from_value:
                detail_parts.append(f'From: {r.from_value}')
            if r.to_value:
                detail_parts.append(f'To: {r.to_value}')
            if r.notes:
                detail_parts.append(r.notes)
            events.append({
                'dt': r.created_at,
                'type': type_map.get((r.event_type or '').strip().lower(), (r.event_type or 'Event').title()),
                'detail': ' | '.join(detail_parts) if detail_parts else '-',
                'amount': float(r.amount or 0.0)
            })
        att_q = SubcontractAttendance.query.filter(SubcontractAttendance.subcontractor_id == sub.id)
        pay_q = SubcontractPayment.query.filter(SubcontractPayment.subcontractor_id == sub.id, SubcontractPayment.is_void == False)
        lab_q = SubcontractLabourAttendance.query.filter(SubcontractLabourAttendance.subcontractor_id == sub.id)
        if stage_filter_id:
            lab_q = lab_q.filter(SubcontractLabourAttendance.stage_id == stage_filter_id)
        if date_from:
            att_q = att_q.filter(SubcontractAttendance.date >= date_from)
            pay_q = pay_q.filter(SubcontractPayment.date >= date_from)
            lab_q = lab_q.filter(SubcontractLabourAttendance.date >= date_from)
        if date_to:
            att_q = att_q.filter(SubcontractAttendance.date <= date_to)
            pay_q = pay_q.filter(SubcontractPayment.date <= date_to)
            lab_q = lab_q.filter(SubcontractLabourAttendance.date <= date_to)
        att_rows = att_q.all()
        pay_rows = pay_q.all()
        lab_rows = lab_q.order_by(SubcontractLabourAttendance.date.desc(), SubcontractLabourAttendance.id.desc()).all()

        def _pct(v):
            try:
                return float(str(v or '').replace('%', '').strip() or 0.0)
            except Exception:
                return 0.0
        progress_events = [r for r in base_rows if (r.event_type or '').lower() == 'progress']
        progress_delta_signed = sum(_pct(r.to_value) - _pct(r.from_value) for r in progress_events)
        progress_delta_abs = sum(abs(_pct(r.to_value) - _pct(r.from_value)) for r in progress_events)

        payment_rows = [p for p in pay_rows if (p.entry_type or 'payment').strip().lower() != 'settlement']
        settlement_rows = [p for p in pay_rows if (p.entry_type or 'payment').strip().lower() == 'settlement']
        kpis = {
            'events_count': len(rows),
            'attendance_days': sum(1 for r in att_rows if (r.present_count or 0) > 0),
            'attendance_men': sum(int(r.present_count or 0) for r in att_rows),
            'progress_added': progress_delta_signed,
            'progress_changes_abs': progress_delta_abs,
            'attendance_progress': sum(float(r.work_done_pct or 0.0) for r in att_rows),
            'payments_total': sum(float(p.amount or 0.0) for p in payment_rows),
            'settled_total': sum(float(p.amount or 0.0) for p in settlement_rows),
            'shifts_count': sum(1 for r in base_rows if (r.event_type or '').lower() in ('shift', 'reassign')),
            'unassign_count': sum(1 for r in base_rows if (r.event_type or '').lower() == 'unassign'),
            'price_updates': sum(1 for r in base_rows if (r.event_type or '').lower() == 'price_update'),
            'labour_days': sum(1 for r in lab_rows if (r.labour_count or 0) > 0),
            'labour_men_total': sum(int(r.labour_count or 0) for r in lab_rows),
            'labour_cost_total': sum(float(r.total_labour_paid or 0.0) for r in lab_rows)
        }
        kpis['labour_avg_daily_cost'] = (kpis['labour_cost_total'] / kpis['labour_days']) if kpis['labour_days'] > 0 else 0.0
        kpis['labour_avg_men_per_day'] = (kpis['labour_men_total'] / kpis['labour_days']) if kpis['labour_days'] > 0 else 0.0
        payable_now = float(sub.payable_amount or 0.0)
        cleared_now = float(sub.total_cleared or 0.0)
        kpis['payment_coverage_pct'] = (cleared_now / payable_now * 100.0) if payable_now > 0 else 0.0
        kpis['sub_margin_payable_basis'] = payable_now - float(kpis['labour_cost_total'] or 0.0)
        kpis['sub_margin_paid_basis'] = cleared_now - float(kpis['labour_cost_total'] or 0.0)
        kpis['sub_margin_contract_basis'] = float(sub.contract_value or 0.0) - float(kpis['labour_cost_total'] or 0.0)
        kpis['sub_profit_loss_total'] = float(kpis['sub_margin_contract_basis'] or 0.0)
        owner_rate = float(sub.stage_rel.effective_rate or 0.0) if sub.stage_rel and (sub.stage_rel.contract_basis or '') == 'Per Sq Ft' else 0.0
        sub_rate = float(sub.rate_per_sqft or 0.0) if (sub.contract_type or '') == 'sqft' else 0.0
        kpis['rate_spread_sqft'] = (owner_rate - sub_rate) if (owner_rate > 0 and sub_rate > 0) else None
        owner_qty = float(sub.stage_rel.qty_sqft or 0.0) if sub.stage_rel else 0.0
        sub_qty = float(sub.total_sqft or 0.0) if (sub.contract_type or '') == 'sqft' else 0.0
        kpis['owner_qty'] = owner_qty if owner_qty > 0 else None
        kpis['sub_qty'] = sub_qty if sub_qty > 0 else None
        kpis['qty_gap'] = (owner_qty - sub_qty) if (owner_qty > 0 and sub_qty > 0) else None
        kpis['stage_completed_but_sub_lt100'] = bool(
            sub.stage_rel and (sub.stage_rel.status or '').strip().lower() in ('completed', 'complete')
            and float(sub.effective_progress_percentage or 0.0) < 100.0
        )
        stage_options = []
        if sub.project_id:
            stage_options = Stage.query.filter(Stage.project_id == sub.project_id).order_by(Stage.id.asc()).all()
        elif sub.stage_id:
            stg = Stage.query.get(sub.stage_id)
            if stg:
                stage_options = [stg]

        return render_template('subcontractors/subcontractor_ledger.html',
            sub=sub,
            events=events,
            labour_rows=lab_rows,
            stage_options=stage_options,
            kpis=kpis,
            selected_event_type=event_type,
            selected_stage_id=stage_filter_id,
            selected_from=raw_from,
            selected_to=raw_to
        )


    @app.route('/hdc/subcontractor/<int:sid>/events/rebuild', methods=['POST'])
    @login_required
    def hdc_subcontractor_events_rebuild(sid):
        sub = Subcontractor.query.get_or_404(sid)
        if (getattr(current_user, 'role', '') or '').lower() != 'admin':
            flash('Only admin can rebuild subcontractor history.', 'danger')
            return redirect(url_for('hdc_subcontractor_ledger', sid=sid))
        created = _ensure_subcontract_baseline_events(sub)
        db.session.commit()
        flash(f'Subcontractor history rebuilt. Added {created} missing baseline event(s).', 'success')
        return redirect(url_for('hdc_subcontractor_ledger', sid=sid))


    @app.route('/hdc/stage/<int:sid>/shift/subcontractor', methods=['POST'])
    @login_required
    def hdc_stage_shift_to_subcontractor(sid):
        stg = Stage.query.get_or_404(sid)
        sub_id = request.form.get('subcontractor_id', type=int)
        if not sub_id:
            flash('Please select a subcontractor.', 'warning')
            return redirect(url_for('hdc_project_detail', pid=stg.project_id))
        sub = Subcontractor.query.get(sub_id)
        if not sub:
            flash('Selected subcontractor is invalid.', 'danger')
            return redirect(url_for('hdc_project_detail', pid=stg.project_id))
        from_desc = 'unassigned'
        if stg.assigned_subcontractor:
            prev = stg.assigned_subcontractor
            from_desc = f'{prev.subcontractor_code or ("SUB-" + str(prev.id))} {prev.name}'
        if sub.stage_id and sub.stage_id != stg.id:
            prev_stage = Stage.query.get(sub.stage_id)
            if prev_stage and prev_stage.assigned_subcontractor_id == sub.id:
                prev_stage.execution_mode = 'company'
                prev_stage.assigned_subcontractor_id = None
                _log_subcontract_event(
                    sub=sub,
                    event_type='unassign',
                    from_value=prev_stage.name,
                    to_value='company',
                    notes='Auto-unassigned from previous stage due to reassignment',
                    project_id=prev_stage.project_id,
                    stage_id=prev_stage.id
                )

        # Optional subcontract pricing overrides while shifting.
        old_terms = f'{sub.contract_type}|R{float(sub.rate_per_sqft or 0):.2f}|Q{float(sub.total_sqft or 0):.2f}|L{float(sub.lump_sum_amount or 0):.2f}|Ret{float(sub.retention_percentage or 0):.2f}%'
        ctype = (request.form.get('contract_type') or sub.contract_type or 'lump_sum').strip().lower()
        if ctype not in ('lump_sum', 'sqft'):
            ctype = 'lump_sum'
        sub.contract_type = ctype
        raw_rate = (request.form.get('rate_per_sqft') or '').strip()
        raw_qty = (request.form.get('total_sqft') or '').strip()
        raw_lump = (request.form.get('lump_sum_amount') or '').strip()
        raw_ret = (request.form.get('retention_pct') or '').strip()
        if raw_rate:
            sub.rate_per_sqft = _flt(raw_rate)
        if raw_qty:
            sub.total_sqft = _flt(raw_qty)
        if raw_lump:
            sub.lump_sum_amount = _flt(raw_lump)
        if raw_ret:
            sub.retention_percentage = _flt(raw_ret)
        if sub.contract_type == 'sqft' and (sub.total_sqft or 0) <= 0 and (stg.qty_sqft or 0) > 0:
            sub.total_sqft = float(stg.qty_sqft or 0.0)

        prev_assigned = stg.assigned_subcontractor
        prev_assigned_id = stg.assigned_subcontractor_id
        if prev_assigned and prev_assigned.id != sub.id and prev_assigned.stage_id == stg.id:
            prev_assigned.stage_id = None
            _log_subcontract_event(
                sub=prev_assigned,
                event_type='unassign',
                from_value=stg.name,
                to_value='reassigned',
                notes=f'Stage reassigned to {sub.subcontractor_code or ("SUB-" + str(sub.id))} {sub.name}',
                project_id=stg.project_id,
                stage_id=stg.id
            )

        stg.execution_mode = 'subcontractor'
        stg.assigned_subcontractor_id = sub.id
        sub.project_id = stg.project_id
        sub.stage_id = stg.id
        ev_type = 'shift'
        if prev_assigned_id and prev_assigned_id != sub.id:
            ev_type = 'reassign'
        _log_subcontract_event(
            sub=sub,
            event_type=ev_type,
            from_value=from_desc,
            to_value=f'{stg.name}',
            notes=f'Shifted stage to subcontractor | type={sub.contract_type} rate={float(sub.rate_per_sqft or 0):.2f} sqft={float(sub.total_sqft or 0):.2f} lump={float(sub.lump_sum_amount or 0):.2f}',
            project_id=stg.project_id,
            stage_id=stg.id
        )
        _log_subcontract_event(
            sub=sub,
            event_type='price_update',
            from_value=old_terms,
            to_value=f'{sub.contract_type}',
            amount=float(sub.contract_value or 0.0),
            notes=f'Rate {float(sub.rate_per_sqft or 0):.2f} | Sqft {float(sub.total_sqft or 0):.2f} | Lump {float(sub.lump_sum_amount or 0):.2f} | Ret {float(sub.retention_percentage or 0):.2f}%',
            project_id=stg.project_id,
            stage_id=stg.id
        )
        db.session.commit()
        owner_rate = float(stg.effective_rate or 0.0) if (stg.contract_basis or '') == 'Per Sq Ft' else 0.0
        sub_rate = float(sub.rate_per_sqft or 0.0) if (sub.contract_type or '') == 'sqft' else 0.0
        margin_note = ''
        if owner_rate > 0 and sub_rate > 0:
            margin_note = f' Owner/Sub rate spread: {owner_rate - sub_rate:,.2f} per sqft.'
            if sub_rate > owner_rate:
                flash(
                    f'Warning: subcontract rate ({sub_rate:,.2f}) is higher than owner rate ({owner_rate:,.2f}). '
                    f'This stage may run at a loss.',
                    'warning'
                )
        flash(f'Stage "{stg.name}" shifted to subcontractor {sub.name}.{margin_note}', 'success')
        return redirect(url_for('hdc_project_detail', pid=stg.project_id))


    @app.route('/hdc/stage/<int:sid>/shift/company', methods=['POST'])
    @login_required
    def hdc_stage_shift_to_company(sid):
        stg = Stage.query.get_or_404(sid)
        prev_sub = stg.assigned_subcontractor
        stg.execution_mode = 'company'
        stg.assigned_subcontractor_id = None
        if prev_sub and prev_sub.stage_id == stg.id:
            prev_sub.stage_id = None
            _log_subcontract_event(
                sub=prev_sub,
                event_type='unassign',
                from_value=stg.name,
                to_value='company',
                notes='Shifted back to company execution',
                project_id=stg.project_id,
                stage_id=stg.id
            )
        db.session.commit()
        flash(f'Stage "{stg.name}" shifted to company execution.', 'success')
        return redirect(url_for('hdc_project_detail', pid=stg.project_id))


    @app.route('/hdc/subcontractors', methods=['GET', 'POST'])
    @login_required
    def hdc_subcontractors():
        if request.method == 'POST':
            name = (request.form.get('name') or '').strip()
            if not name:
                flash('Subcontractor name is required.', 'warning')
                return redirect(url_for('hdc_subcontractors'))
            code = _next_subcontractor_code()
            while Subcontractor.query.filter(func.lower(Subcontractor.subcontractor_code) == code.lower()).first():
                code = _next_subcontractor_code()

            sub = Subcontractor(
                subcontractor_code=code,
                project_id=None,
                stage_id=None,
                name=name,
                phone=(request.form.get('phone') or '').strip(),
                work_type='',
                contract_type='lump_sum',
                rate_per_sqft=0.0,
                total_sqft=0.0,
                lump_sum_amount=0.0,
                retention_percentage=0.0,
                work_done_percentage=0.0
            )
            db.session.add(sub)
            db.session.flush()
            _log_subcontract_event(
                sub=sub,
                event_type='create',
                to_value=f'{sub.subcontractor_code or ""} {sub.name}',
                notes='Subcontractor profile created'
            )
            db.session.commit()
            flash(f'Subcontractor added ({code}).', 'success')
            return redirect(url_for('hdc_subcontractors'))

        sub_id = request.args.get('sub_id', type=int)
        project_id = request.args.get('project_id', type=int)
        stage_id = request.args.get('stage_id', type=int)

        q = Subcontractor.query
        if sub_id:
            q = q.filter(Subcontractor.id == sub_id)
        if project_id:
            q = q.filter(Subcontractor.project_id == project_id)
        if stage_id:
            q = q.filter(Subcontractor.stage_id == stage_id)

        subcontractors = q.order_by(Subcontractor.created_at.desc(), Subcontractor.id.desc()).all()
        all_subcontractors = Subcontractor.query.order_by(Subcontractor.name.asc(), Subcontractor.id.asc()).all()
        projects = Project.query.order_by(Project.name.asc(), Project.id.asc()).all()

        stage_q = Stage.query
        if project_id:
            stage_q = stage_q.filter(Stage.project_id == project_id)
        stages = stage_q.order_by(Stage.name.asc(), Stage.id.asc()).all()

        return render_template('subcontractors/subcontractors.html',
            subcontractors=subcontractors,
            all_subcontractors=all_subcontractors,
            projects=projects,
            stages=stages,
            selected_sub_id=sub_id,
            selected_project_id=project_id,
            selected_stage_id=stage_id
        )
