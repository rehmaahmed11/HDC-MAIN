"""HDC routes: Payroll runs, salary cards and history.

Moved verbatim from hdc_erp.py; each handler keeps its
original @app.route decorator and endpoint name.
"""

import calendar as pycal
from datetime import date, datetime, timedelta

from flask import flash, redirect, render_template, request, url_for
from flask_login import login_required
from sqlalchemy import func

from hdc.extensions import db
from hdc.models.workforce import LabourLedger, PayrollItem, PayrollRun, TimeEntry, Worker
from hdc.services.accounts import _accounts_post_labour_ledger_row
from hdc.services.timekeeping import _has_recent_duplicate
from hdc.utils.dates import _pkt_now, _pkt_now_naive, _pkt_today
from hdc.utils.format import _flt, _parse_date

def register(app):
    """Register Payroll runs, salary cards and history."""
    # Ã¢â€â‚¬Ã¢â€â‚¬ Payroll Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
    @app.route('/hdc/payroll')
    @login_required
    def hdc_payroll():
        return render_template('payroll/payroll.html')


    @app.route('/hdc/payroll/generate', methods=['GET', 'POST'])
    @login_required
    def hdc_payroll_generate():
        if request.method == 'POST':
            action = (request.form.get('action') or 'generate').strip().lower()

            if action == 'pay_worker':
                run_id = request.form.get('run_id', type=int)
                worker_id = request.form.get('worker_id', type=int)
                run = PayrollRun.query.get_or_404(run_id)
                item = PayrollItem.query.filter_by(run_id=run.id, worker_id=worker_id).first()
                if not item:
                    flash('Payroll item not found for selected worker.', 'warning')
                    return redirect(url_for('hdc_payroll_generate', run_id=run.id))
                note_key = f'Payroll run #{run.id} '
                already_paid = float(db.session.query(func.coalesce(func.sum(LabourLedger.amount), 0.0))
                                     .filter(
                                         LabourLedger.worker_id == worker_id,
                                         LabourLedger.entry_type == 'payment',
                                         LabourLedger.is_void == False,
                                         LabourLedger.notes.ilike(f'%{note_key}%')
                                     ).scalar() or 0.0)
                payable = float(item.net_pay or 0.0)
                balance = max(0.0, payable - already_paid)
                amount_in = _flt(request.form.get('amount'))
                amount = amount_in if amount_in > 0 else balance
                if amount <= 0 or balance <= 0:
                    flash('No payable balance left for this worker in this payroll run.', 'info')
                    return redirect(url_for('hdc_payroll_generate', run_id=run.id))
                if amount > balance + 1e-6:
                    flash(f'Amount exceeds payroll balance ({balance:,.0f} PKR).', 'warning')
                    return redirect(url_for('hdc_payroll_generate', run_id=run.id))
                pay_date = _pkt_today()
                notes = f'Payroll run #{run.id} manual payment ({run.date_from} to {run.date_to})'
                if _has_recent_duplicate(
                    LabourLedger,
                    worker_id=worker_id,
                    entry_type='payment',
                    amount=amount,
                    date=pay_date,
                    notes=notes
                ):
                    flash('Duplicate payroll payment prevented.', 'warning')
                    return redirect(url_for('hdc_payroll_generate', run_id=run.id))
                pay_row = LabourLedger(
                    worker_id=worker_id,
                    entry_type='payment',
                    amount=amount,
                    date=pay_date,
                    project_id=None,
                    stage_id=None,
                    activity_at=_pkt_now_naive(),
                    notes=notes
                )
                db.session.add(pay_row)
                db.session.flush()
                ok_txn, msg_txn, _ = _accounts_post_labour_ledger_row(pay_row, worker_name=(item.worker.name if item.worker else ''), commit=False)
                if not ok_txn:
                    db.session.rollback()
                    flash(msg_txn or 'Unable to post payroll payment in unified accounts.', 'danger')
                    return redirect(url_for('hdc_payroll_generate', run_id=run.id))
                db.session.commit()
                flash(f'Payroll payment recorded: {amount:,.0f} PKR.', 'success')
                return redirect(url_for('hdc_payroll_generate', run_id=run.id))

            if action == 'pay_all':
                run_id = request.form.get('run_id', type=int)
                run = PayrollRun.query.get_or_404(run_id)
                items = PayrollItem.query.filter_by(run_id=run.id).all()
                note_key = f'Payroll run #{run.id} '
                created = 0
                total_paid_now = 0.0
                for it in items:
                    wid = int(it.worker_id or 0)
                    if not wid:
                        continue
                    already_paid = float(db.session.query(func.coalesce(func.sum(LabourLedger.amount), 0.0))
                                         .filter(
                                             LabourLedger.worker_id == wid,
                                             LabourLedger.entry_type == 'payment',
                                             LabourLedger.is_void == False,
                                             LabourLedger.notes.ilike(f'%{note_key}%')
                                         ).scalar() or 0.0)
                    payable = float(it.net_pay or 0.0)
                    balance = max(0.0, payable - already_paid)
                    if balance <= 0:
                        continue
                    notes = f'Payroll run #{run.id} bulk payment ({run.date_from} to {run.date_to})'
                    pay_row = LabourLedger(
                        worker_id=wid,
                        entry_type='payment',
                        amount=balance,
                        date=_pkt_today(),
                        project_id=None,
                        stage_id=None,
                        activity_at=_pkt_now_naive(),
                        notes=notes
                    )
                    db.session.add(pay_row)
                    db.session.flush()
                    wrow = Worker.query.get(wid)
                    ok_txn, msg_txn, _ = _accounts_post_labour_ledger_row(pay_row, worker_name=(wrow.name if wrow else ''), commit=False)
                    if not ok_txn:
                        db.session.rollback()
                        flash(msg_txn or 'Unable to post payroll payment in unified accounts.', 'danger')
                        return redirect(url_for('hdc_payroll_generate', run_id=run.id))
                    created += 1
                    total_paid_now += balance
                db.session.commit()
                if created:
                    flash(f'Pay All completed: {created} worker(s), {total_paid_now:,.0f} PKR paid.', 'success')
                else:
                    flash('No pending payroll balances found for this run.', 'info')
                return redirect(url_for('hdc_payroll_generate', run_id=run.id))

            date_from = _parse_date(request.form.get('date_from'))
            date_to = _parse_date(request.form.get('date_to'))
            start_dt = datetime.combine(date_from, datetime.min.time())
            end_dt = datetime.combine(date_to, datetime.max.time())

            entries = TimeEntry.query.filter(
                TimeEntry.is_void == False,
                TimeEntry.check_in >= start_dt,
                TimeEntry.check_in <= end_dt
            ).all()
            by_worker = {}
            for t in entries:
                info = by_worker.setdefault(t.worker_id, {'hours': 0.0, 'ot': 0.0, 'gross': 0.0})
                info['hours'] += float(t.hours or 0)
                info['ot'] += float(t.overtime or 0)
                info['gross'] += float(t.wage_calculated or 0)

            # Ensure payroll run contains all active workers for clearer salary-card output.
            for w in Worker.query.filter_by(active_status=True).all():
                by_worker.setdefault(w.id, {'hours': 0.0, 'ot': 0.0, 'gross': 0.0})

            run = PayrollRun(date_from=date_from, date_to=date_to, run_date=_pkt_today())
            db.session.add(run); db.session.commit()

            total_amount = 0.0
            for wid, info in by_worker.items():
                advances = sum(l.amount for l in LabourLedger.query.filter_by(worker_id=wid, entry_type='advance', is_void=False)
                               .filter(LabourLedger.date >= date_from, LabourLedger.date <= date_to).all())
                net = max(0.0, info['gross'] - advances)
                item = PayrollItem(
                    run_id=run.id, worker_id=wid,
                    total_hours=info['hours'], total_overtime=info['ot'],
                    gross_amount=info['gross'], advance_deducted=advances, net_pay=net
                )
                db.session.add(item)
                total_amount += net
            run.total_amount = total_amount
            db.session.commit()
            flash('Payroll run created. No worker was paid automatically.', 'success')
            return redirect(url_for('hdc_payroll_generate', run_id=run.id))

        run_id = request.args.get('run_id', type=int)
        runs = PayrollRun.query.order_by(PayrollRun.run_date.desc()).all()
        selected_run = PayrollRun.query.get(run_id) if run_id else None
        items = PayrollItem.query.filter_by(run_id=run_id).all() if run_id else []

        payroll_summary_rows = []
        calendar_days = []
        day_status_rows = []
        worker_calendar_rows = []
        calendar_mode = (request.args.get('calendar_mode') or 'range').strip().lower()
        calendar_month = (request.args.get('calendar_month') or '')
        cal_label = ''
        totals = {
            'workers': 0,
            'work_entries': 0,
            'work_days': 0,
            'absent_days': 0,
            'not_assigned_days': 0,
            'total_wage': 0.0,
            'total_advance': 0.0,
            'total_payable': 0.0,
            'total_paid': 0.0,
            'total_balance': 0.0
        }

        if selected_run:
            range_start = selected_run.date_from
            range_end = selected_run.date_to
            if range_start > range_end:
                range_start, range_end = range_end, range_start

            # Calendar window can be payroll range or full month view.
            cal_start = range_start
            cal_end = range_end
            if calendar_mode == 'month':
                base_month = calendar_month or range_start.strftime('%Y-%m')
                try:
                    y, m = [int(x) for x in base_month.split('-')]
                    first_day = date(y, m, 1)
                    last_day = date(y, m, pycal.monthrange(y, m)[1])
                    cal_start, cal_end = first_day, last_day
                    calendar_month = base_month
                except Exception:
                    calendar_mode = 'range'
                    calendar_month = range_start.strftime('%Y-%m')
            else:
                calendar_mode = 'range'
                calendar_month = range_start.strftime('%Y-%m')

            cal_label = f"{cal_start.isoformat()} to {cal_end.isoformat()}"

            workers = Worker.query.filter_by(active_status=True).order_by(Worker.name).all()
            worker_ids = [w.id for w in workers]
            totals['workers'] = len(workers)

            start_dt = datetime.combine(range_start, datetime.min.time())
            end_dt = datetime.combine(range_end, datetime.max.time())
            cal_start_dt = datetime.combine(cal_start, datetime.min.time())
            cal_end_dt = datetime.combine(cal_end, datetime.max.time())

            entries_period = (TimeEntry.query
                              .filter(TimeEntry.worker_id.in_(worker_ids),
                                      TimeEntry.is_void == False,
                                      TimeEntry.check_in >= start_dt,
                                      TimeEntry.check_in <= end_dt)
                              .order_by(TimeEntry.check_in.asc())
                              .all()) if worker_ids else []

            entries_calendar = (TimeEntry.query
                                .filter(TimeEntry.worker_id.in_(worker_ids),
                                        TimeEntry.is_void == False,
                                        TimeEntry.check_in >= cal_start_dt,
                                        TimeEntry.check_in <= cal_end_dt)
                                .order_by(TimeEntry.check_in.asc())
                                .all()) if worker_ids else []

            first_entry_raw = (db.session.query(TimeEntry.worker_id, func.min(func.date(TimeEntry.check_in)))
                               .filter(TimeEntry.worker_id.in_(worker_ids), TimeEntry.is_void == False)
                               .group_by(TimeEntry.worker_id)
                               .all()) if worker_ids else []
            first_entry_date_by_worker = {}
            for wid, dval in first_entry_raw:
                try:
                    first_entry_date_by_worker[wid] = datetime.strptime(str(dval), '%Y-%m-%d').date()
                except Exception:
                    pass

            worked_dates_period = {}
            work_entries_count = {}
            overtime_total = {}
            gross_total = {}
            for t in entries_period:
                wset = worked_dates_period.setdefault(t.worker_id, set())
                wset.add(t.check_in.date())
                work_entries_count[t.worker_id] = work_entries_count.get(t.worker_id, 0) + 1
                overtime_total[t.worker_id] = overtime_total.get(t.worker_id, 0.0) + float(t.overtime or 0.0)
                gross_total[t.worker_id] = gross_total.get(t.worker_id, 0.0) + float(t.wage_calculated or 0.0)

            worked_dates_calendar = {}
            for t in entries_calendar:
                worked_dates_calendar.setdefault(t.worker_id, set()).add(t.check_in.date())

            # Calendar day list
            d = cal_start
            while d <= cal_end:
                calendar_days.append(d)
                d += timedelta(days=1)

            # Day-wise work / absent / not assigned summary
            for d in calendar_days:
                work_cnt = 0
                absent_cnt = 0
                not_assigned_cnt = 0
                for w in workers:
                    w_dates = worked_dates_calendar.get(w.id, set())
                    if d in w_dates:
                        work_cnt += 1
                    else:
                        first_d = first_entry_date_by_worker.get(w.id)
                        if first_d and first_d < d:
                            absent_cnt += 1
                        else:
                            not_assigned_cnt += 1
                day_status_rows.append({
                    'date': d,
                    'work_entries': work_cnt,
                    'absent_entries': absent_cnt,
                    'not_assigned_entries': not_assigned_cnt
                })
                totals['work_days'] += work_cnt
                totals['absent_days'] += absent_cnt
                totals['not_assigned_days'] += not_assigned_cnt

            worker_map = {w.id: w for w in workers}
            for wid, w in worker_map.items():
                w_days = worked_dates_period.get(wid, set())
                worked_day_count = len(w_days)
                gross = float(gross_total.get(wid, 0.0))
                ot = float(overtime_total.get(wid, 0.0))
                per_day_wage = (gross / worked_day_count) if worked_day_count > 0 else 0.0
                advances = sum(float(l.amount or 0.0) for l in LabourLedger.query
                               .filter_by(worker_id=wid, entry_type='advance', is_void=False)
                               .filter(LabourLedger.date >= range_start, LabourLedger.date <= range_end)
                               .all())
                payable = max(0.0, gross - advances)
                run_note_key = f'Payroll run #{selected_run.id} '
                paid = sum(float(l.amount or 0.0) for l in LabourLedger.query
                           .filter_by(worker_id=wid, entry_type='payment', is_void=False)
                           .filter(LabourLedger.notes.ilike(f'%{run_note_key}%'))
                           .all())
                balance = max(0.0, payable - paid)
                status = 'Not Paid' if (payable > 0 and balance > 0.01) else 'Paid'

                absent_entries = 0
                not_assigned_entries = 0
                first_d = first_entry_date_by_worker.get(wid)
                for d in calendar_days:
                    if d in worked_dates_calendar.get(wid, set()):
                        continue
                    if first_d and first_d < d:
                        absent_entries += 1
                    else:
                        not_assigned_entries += 1

                payroll_summary_rows.append({
                    'worker': w,
                    'work_entries': int(work_entries_count.get(wid, 0)),
                    'worked_days': worked_day_count,
                    'per_day_wage': per_day_wage,
                    'overtime_total': ot,
                    'total_wage': gross,
                    'advance': advances,
                    'payable': payable,
                    'paid': paid,
                    'balance': balance,
                    'payment_status': status,
                    'absent_entries': absent_entries,
                    'not_assigned_entries': not_assigned_entries
                })

                row_statuses = []
                w_dates = worked_dates_calendar.get(wid, set())
                for d in calendar_days:
                    if d in w_dates:
                        code = 'W'
                    else:
                        if first_d and first_d < d:
                            code = 'A'
                        else:
                            code = 'N'
                    row_statuses.append({'date': d, 'code': code})
                worker_calendar_rows.append({'worker': w, 'statuses': row_statuses})

                totals['work_entries'] += int(work_entries_count.get(wid, 0))
                totals['total_wage'] += gross
                totals['total_advance'] += advances
                totals['total_payable'] += payable
                totals['total_paid'] += paid
                totals['total_balance'] += balance

            payroll_summary_rows.sort(key=lambda r: (r['worker'].name or '').lower())
            worker_calendar_rows.sort(key=lambda r: (r['worker'].name or '').lower())

        return render_template('payroll/payroll_generate.html',
            runs=runs,
            selected_run=selected_run,
            items=items,
            payroll_summary_rows=payroll_summary_rows,
            calendar_days=calendar_days,
            day_status_rows=day_status_rows,
            worker_calendar_rows=worker_calendar_rows,
            calendar_mode=calendar_mode,
            calendar_month=calendar_month,
            cal_label=cal_label,
            totals=totals,
            today=_pkt_today().isoformat()
        )


    @app.route('/hdc/payroll/<int:run_id>/delete', methods=['POST'])
    @login_required
    def hdc_payroll_delete(run_id):
        run = PayrollRun.query.get_or_404(run_id)
        items = PayrollItem.query.filter_by(run_id=run.id).all()
        worker_ids = [it.worker_id for it in items if it.worker_id]
        deleted_ledgers = 0

        # Remove payroll payment entries linked with run-id note pattern.
        if worker_ids:
            note_key = f'Payroll run #{run.id} '
            ledgers = (LabourLedger.query
                       .filter(LabourLedger.worker_id.in_(worker_ids),
                               LabourLedger.entry_type == 'payment',
                               LabourLedger.is_void == False,
                               LabourLedger.notes.ilike(f'%{note_key}%'))
                       .all())
            for l in ledgers:
                db.session.delete(l)
                deleted_ledgers += 1

        db.session.delete(run)
        db.session.commit()

        if deleted_ledgers > 0:
            flash(f'Payroll run #{run.id} deleted with {deleted_ledgers} linked payment entries.', 'success')
        else:
            flash(f'Payroll run #{run.id} deleted. (No linked payment entries found for auto-cleanup.)', 'warning')
        return redirect(url_for('hdc_payroll_salary_cards_page'))


    @app.route('/hdc/payroll/salary-cards')
    @login_required
    def hdc_payroll_salary_cards_page():
        run_id = request.args.get('run_id', type=int)
        worker_id = request.args.get('worker_id', type=int)
        runs = PayrollRun.query.order_by(PayrollRun.run_date.desc()).all()
        selected_run = PayrollRun.query.get(run_id) if run_id else None
        workers = Worker.query.order_by(Worker.name).all()
        salary_rows = []
        if selected_run:
            item_by_worker = {it.worker_id: it for it in PayrollItem.query.filter_by(run_id=selected_run.id).all()}
            for w in workers:
                if worker_id and worker_id != w.id:
                    continue
                it = item_by_worker.get(w.id)
                salary_rows.append({
                    'worker': w,
                    'net_pay': float(it.net_pay or 0.0) if it else 0.0
                })
        return render_template('payroll/payroll_salary_cards.html',
            runs=runs,
            selected_run=selected_run,
            salary_rows=salary_rows,
            workers=workers,
            filter_worker_id=worker_id
        )


    @app.route('/hdc/payroll/history')
    @login_required
    def hdc_payroll_history():
        runs = PayrollRun.query.order_by(PayrollRun.run_date.desc()).all()
        return render_template('payroll/payroll_history.html', runs=runs)


    @app.route('/hdc/payroll/<int:run_id>/salary-cards')
    @login_required
    def hdc_payroll_salary_cards(run_id):
        run = PayrollRun.query.get_or_404(run_id)
        worker_id = request.args.get('worker_id', type=int)
        auto_print = request.args.get('autoprint', '1')

        start_dt = datetime.combine(run.date_from, datetime.min.time())
        end_dt = datetime.combine(run.date_to, datetime.max.time())
        label_period = f"{run.date_from.isoformat()} to {run.date_to.isoformat()}"

        cards = []
        total_workers = 0
        totals = {
            'work_entries': 0,
            'worked_days': 0,
            'overtime_total': 0.0,
            'total_wage': 0.0,
            'advance': 0.0,
            'payable': 0.0,
            'paid': 0.0,
            'balance': 0.0
        }

        workers_q = Worker.query
        if worker_id:
            workers_q = workers_q.filter(Worker.id == worker_id)
        workers = workers_q.order_by(Worker.name).all()

        for w in workers:
            total_workers += 1

            entry_rows = TimeEntry.query.filter(
                TimeEntry.worker_id == w.id,
                TimeEntry.is_void == False,
                TimeEntry.check_in >= start_dt,
                TimeEntry.check_in <= end_dt
            ).all()
            work_entries = len(entry_rows)
            worked_days = len({e.check_in.date() for e in entry_rows})
            overtime_total = sum(float(e.overtime or 0.0) for e in entry_rows)
            total_wage = sum(float(e.wage_calculated or 0.0) for e in entry_rows)
            advance = sum(float(l.amount or 0.0) for l in LabourLedger.query.filter(
                LabourLedger.worker_id == w.id,
                LabourLedger.entry_type == 'advance',
                LabourLedger.is_void == False,
                LabourLedger.date >= run.date_from,
                LabourLedger.date <= run.date_to
            ).all())
            payable = max(0.0, total_wage - advance)

            run_note_key = f'Payroll run #{run.id} '
            paid = sum(float(l.amount or 0.0) for l in LabourLedger.query.filter(
                LabourLedger.worker_id == w.id,
                LabourLedger.entry_type == 'payment',
                LabourLedger.is_void == False,
                LabourLedger.notes.ilike(f"%{run_note_key}%")
            ).all())
            balance = max(0.0, payable - paid)
            per_day_wage = (total_wage / worked_days) if worked_days > 0 else 0.0
            status = 'Paid' if balance <= 0.01 else 'Not Paid'

            cards.append({
                'worker': w,
                'work_entries': work_entries,
                'worked_days': worked_days,
                'per_day_wage': per_day_wage,
                'overtime_total': overtime_total,
                'total_wage': total_wage,
                'advance': advance,
                'payable': payable,
                'paid': paid,
                'balance': balance,
                'status': status
            })

            totals['work_entries'] += work_entries
            totals['worked_days'] += worked_days
            totals['overtime_total'] += overtime_total
            totals['total_wage'] += total_wage
            totals['advance'] += advance
            totals['payable'] += payable
            totals['paid'] += paid
            totals['balance'] += balance

        cards.sort(key=lambda c: (c['worker'].name or '').lower())
        title = 'Salary Card' if worker_id else 'Salary Cards Report'
        return render_template('payroll/payroll_salary_cards_print.html',
            run=run,
            cards=cards,
            title=title,
            total_workers=total_workers,
            totals=totals,
            label_period=label_period,
            now=_pkt_now().strftime('%Y-%m-%d %H:%M'),
            auto_print=(str(auto_print).strip() != '0')
        )
