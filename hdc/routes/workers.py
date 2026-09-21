"""HDC routes: Workers, trades, ledgers, advances, payments and rates.

Moved verbatim from hdc_erp.py; each handler keeps its
original @app.route decorator and endpoint name.
"""

import re

from flask import abort, flash, redirect, render_template, request, url_for
from flask_login import login_required
from sqlalchemy import func

from hdc.extensions import _money_write_required, db
from hdc.models.accounts import Expense
from hdc.models.projects import Project, Stage
from hdc.models.workforce import LabourLedger, LabourRateHistory, TimeEntry, Worker, WorkerRate, WorkerTrade
from hdc.services.accounts import _accounts_post_labour_ledger_row, _accounts_set_void_by_source, _accounts_upsert_labour_ledger_txn
from hdc.services.ledger import _linked_expense_for_labour_ledger, _worker_payable_snapshot
from hdc.services.lookups import _ensure_expense_category, _trade_options
from hdc.services.receipts import _receipt_company_profile
from hdc.services.timekeeping import _has_recent_duplicate, _reconcile_worker_time_entries, _reconcile_worker_tip_ledger, _repair_worker_work_ledger_links
from hdc.utils.dates import _pkt_now_naive, _pkt_today
from hdc.utils.format import _activity_at_for, _amount_to_words, _flt, _parse_date
from hdc.utils.normalize import _normalize_trade_name

def register(app):
    """Register Workers, trades, ledgers, advances, payments and rates."""
    # --- Workers ---------------------------------------------------------------
    @app.route('/hdc/workers', methods=['GET', 'POST'])
    @login_required
    @_money_write_required()
    def hdc_workers():
        if request.method == 'POST':
            code = request.form.get('worker_code','').strip()
            name = (request.form.get('name') or '').strip()
            role_type = (request.form.get('role_type') or '').strip()
            if not code or not name:
                flash('Worker code and name are required.', 'warning')
            elif Worker.query.filter_by(worker_code=code).first():
                flash('Worker code already exists.', 'danger')
            elif not role_type:
                flash('Please select trade from Trades list.', 'warning')
            else:
                valid_trade = db.session.query(WorkerTrade).filter(
                    func.lower(WorkerTrade.name) == role_type.lower(),
                    WorkerTrade.active_status == True
                ).first()
                if not valid_trade:
                    flash('Selected trade is not available. Please add it in Trades section first.', 'warning')
                    return redirect(url_for('hdc_workers'))
                w = Worker(worker_code=code,
                    name=name,
                    role_type=valid_trade.name,
                    base_daily_wage=_flt(request.form.get('daily_wage')),
                    wage_type=request.form.get('wage_type','daily'),
                    hourly_rate=_flt(request.form.get('hourly_rate')),
                    rate_per_sqft=_flt(request.form.get('rate_per_sqft')),
                    active_status=True)
                db.session.add(w); db.session.commit()
                flash(f'Worker "{w.name}" added.', 'success')
            return redirect(url_for('hdc_workers'))
        workers = Worker.query.order_by(Worker.created_at.desc()).all()
        return render_template('workers/workers.html', workers=workers, trade_options=_trade_options())


    @app.route('/hdc/trades', methods=['GET', 'POST'])
    @login_required
    @_money_write_required()
    def hdc_trades():
        if request.method == 'POST':
            action = (request.form.get('action') or '').strip()
            if action == 'add_trade':
                trade_name = _normalize_trade_name(request.form.get('trade_name'))
                if not trade_name:
                    flash('Trade name is required.', 'warning')
                else:
                    existing = db.session.query(WorkerTrade).filter(func.lower(WorkerTrade.name) == trade_name.lower()).first()
                    if existing:
                        if not existing.active_status:
                            existing.active_status = True
                            db.session.commit()
                            flash(f'Trade "{trade_name}" restored.', 'success')
                        else:
                            flash('Trade already exists.', 'warning')
                    else:
                        db.session.add(WorkerTrade(name=trade_name, active_status=True))
                        db.session.commit()
                        flash(f'Trade "{trade_name}" added.', 'success')
            elif action == 'edit_trade':
                trade_id = request.form.get('trade_id', type=int)
                trade = WorkerTrade.query.get(trade_id)
                new_name = _normalize_trade_name(request.form.get('trade_name'))
                if not trade:
                    flash('Trade not found.', 'danger')
                elif not new_name:
                    flash('Trade name is required.', 'warning')
                else:
                    dupe = db.session.query(WorkerTrade).filter(func.lower(WorkerTrade.name) == new_name.lower(), WorkerTrade.id != trade.id).first()
                    if dupe:
                        flash('Another trade with this name already exists.', 'warning')
                    else:
                        old_name = trade.name or ''
                        trade.name = new_name
                        Worker.query.filter(func.lower(Worker.role_type) == (old_name or '').lower()).update(
                            {Worker.role_type: new_name}, synchronize_session=False
                        )
                        db.session.commit()
                        flash(f'Trade updated to "{new_name}".', 'success')
            elif action == 'delete_trade':
                trade_id = request.form.get('trade_id', type=int)
                trade = WorkerTrade.query.get(trade_id)
                if not trade:
                    flash('Trade not found.', 'danger')
                else:
                    trade.active_status = False
                    db.session.commit()
                    flash(f'Trade "{trade.name}" removed from active list.', 'success')
            return redirect(url_for('hdc_trades'))
        trades = WorkerTrade.query.filter_by(active_status=True).order_by(WorkerTrade.name).all()
        return render_template('workers/trades.html', trades=trades)


    @app.route('/hdc/workers/<int:wid>/toggle', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_toggle_worker(wid):
        w = Worker.query.get_or_404(wid)
        w.active_status = not w.active_status
        db.session.commit()
        flash(f'Worker "{w.name}" {"activated" if w.active_status else "suspended"}.', 'success')
        return redirect(url_for('hdc_workers'))


    @app.route('/hdc/workers/<int:wid>/edit', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_edit_worker(wid):
        w = Worker.query.get_or_404(wid)
        code = (request.form.get('worker_code') or '').strip()
        name = (request.form.get('name') or '').strip()
        role_type = (request.form.get('role_type') or '').strip()
        wage_type = (request.form.get('wage_type') or 'daily').strip()
        active_status = (request.form.get('active_status') or 'active').strip()

        if not code or not name:
            flash('Worker code and name are required.', 'warning')
            return redirect(url_for('hdc_workers'))

        code_exists = Worker.query.filter(Worker.worker_code == code, Worker.id != wid).first()
        if code_exists:
            flash('Worker code already exists for another worker.', 'danger')
            return redirect(url_for('hdc_workers'))

        valid_trade = db.session.query(WorkerTrade).filter(
            func.lower(WorkerTrade.name) == role_type.lower(),
            WorkerTrade.active_status == True
        ).first()
        if not valid_trade:
            flash('Selected trade is not available. Please use Trades section.', 'warning')
            return redirect(url_for('hdc_workers'))

        w.worker_code = code
        w.name = name
        w.role_type = valid_trade.name
        w.wage_type = wage_type if wage_type in ('daily', 'hourly', 'per_sqft') else 'daily'
        w.base_daily_wage = _flt(request.form.get('daily_wage'))
        w.hourly_rate = _flt(request.form.get('hourly_rate'))
        w.rate_per_sqft = _flt(request.form.get('rate_per_sqft'))
        w.active_status = (active_status == 'active')
        db.session.commit()
        flash(f'Worker "{w.name}" updated.', 'success')
        return redirect(url_for('hdc_workers'))


    @app.route('/hdc/workers/<int:wid>/ledger')
    @login_required
    def hdc_worker_ledger(wid):
        w = Worker.query.get_or_404(wid)
        filter_show_voided = (request.args.get('show_voided') or '1').strip().lower() in ('1', 'true', 'on', 'yes')
        page = max(1, request.args.get('page', type=int) or 1)
        try:
            per_page = int(request.args.get('per_page', type=int) or 10)
        except Exception:
            per_page = 10
        per_page = max(10, min(per_page, 200))
        _reconcile_worker_time_entries(wid)
        _repair_worker_work_ledger_links(wid)
        _reconcile_worker_tip_ledger(w)
        db.session.commit()
        projects = Project.query.all()
        workers = Worker.query.order_by(Worker.name).all()
        # Full ledger ordered chronologically — running balance must reflect ALL
        # entries from the start, regardless of which page is shown.
        full_ledger = (LabourLedger.query
                       .filter(LabourLedger.worker_id == wid)
                       .order_by(LabourLedger.activity_at.asc(), LabourLedger.id.asc())
                       .all())
        if not filter_show_voided:
            full_ledger = [e for e in full_ledger if not bool(getattr(e, 'is_void', False))]
        running_balance_map = {}
        running_balance = 0.0
        for entry in full_ledger:
            delta = 0.0
            if not bool(getattr(entry, 'is_void', False)):
                et = str(getattr(entry, 'entry_type', '') or '').strip().lower()
                amt = float(getattr(entry, 'amount', 0.0) or 0.0)
                if et == 'work':
                    # Read the wage from the time entry it mirrors, so the
                    # running balance is driven by the same source of truth as
                    # the payable snapshot. Orphaned or duplicated 'work' rows
                    # used to inflate/deflate this column while the "Balance"
                    # KPI stayed correct (LABOUR_AUDIT #10).
                    te = (TimeEntry.query.get(entry.time_entry_id)
                          if entry.time_entry_id else None)
                    delta = 0.0 if (te is None or te.is_void) else float(te.wage_calculated or 0.0)
                elif et in ('advance', 'payment', 'settlement'):
                    delta = -amt
                # tip → delta stays 0 (gratis cash, neutral on the worker's
                # owed balance — see _worker_payable_snapshot for the rationale).
            running_balance += delta
            running_balance_map[int(getattr(entry, 'id', 0) or 0)] = float(running_balance)
        pg_total_items = len(full_ledger)
        pg_total_pages = max(1, (pg_total_items + per_page - 1) // per_page) if pg_total_items else 1
        page = min(page, pg_total_pages)
        start = (page - 1) * per_page
        ledger_entries = full_ledger[start:start + per_page]
        time_entries = (TimeEntry.query
                        .filter(TimeEntry.worker_id == wid, TimeEntry.is_void == False)
                        .order_by(TimeEntry.check_in.asc(), TimeEntry.id.asc())
                        .all())
        snap = _worker_payable_snapshot(wid)
        tip_rows = (LabourLedger.query
                    .filter(
                        LabourLedger.worker_id == wid,
                        LabourLedger.entry_type == 'tip',
                        LabourLedger.is_void == False
                    )
                    .order_by(LabourLedger.activity_at.asc(), LabourLedger.id.asc())
                    .all())
        settlement_rows = (LabourLedger.query
                           .filter(
                               LabourLedger.worker_id == wid,
                               LabourLedger.entry_type == 'settlement',
                               LabourLedger.is_void == False
                           )
                           .order_by(LabourLedger.activity_at.asc(), LabourLedger.id.asc())
                           .all())
        pg_query = {
            'show_voided': (1 if filter_show_voided else 0),
            'per_page': per_page,
        }
        return render_template('workers/worker_ledger.html',
            w=w,
            projects=projects,
            workers=workers,
            ledger_entries=ledger_entries,
            filter_show_voided=filter_show_voided,
            running_balance_map=running_balance_map,
            time_entries=time_entries,
            snap=snap,
            tip_rows=tip_rows,
            settlement_rows=settlement_rows,
            pg_page=page,
            pg_total_pages=pg_total_pages,
            pg_total_items=pg_total_items,
            pg_per_page=per_page,
            pg_endpoint='hdc_worker_ledger',
            pg_url_kwargs={'wid': wid},
            pg_query=pg_query,
        )


    @app.route('/hdc/workers/<int:wid>/advance', methods=['GET', 'POST'])
    @login_required
    @_money_write_required()
    def hdc_worker_advance(wid):
        w = Worker.query.get_or_404(wid)
        projects = Project.query.all()
        if request.method == 'POST':
            entry_date = _parse_date(request.form.get('date'))
            amount = _flt(request.form.get('amount'))
            if amount <= 0:
                flash('Advance amount must be greater than zero.', 'danger')
                return redirect(url_for('hdc_worker_advance', wid=wid))
            project_id = request.form.get('project_id', type=int) or None
            stage_id = request.form.get('stage_id', type=int) or None
            notes = (request.form.get('notes','') or '').strip()
            if _has_recent_duplicate(
                LabourLedger,
                worker_id=wid,
                entry_type='advance',
                amount=amount,
                date=entry_date,
                project_id=project_id,
                stage_id=stage_id,
                notes=notes
            ):
                flash('Duplicate advance prevented (same values submitted too quickly).', 'warning')
                return redirect(url_for('hdc_worker_ledger', wid=wid))
            adv_row = LabourLedger(
                worker_id=wid, entry_type='advance',
                amount=amount,
                date=entry_date,
                activity_at=_activity_at_for(entry_date),
                project_id=project_id,
                stage_id=stage_id,
                notes=notes)
            db.session.add(adv_row)
            db.session.flush()
            ok_txn, msg_txn, _ = _accounts_post_labour_ledger_row(adv_row, worker_name=w.name, commit=False)
            if not ok_txn:
                db.session.rollback()
                flash(msg_txn or 'Unable to post advance in unified accounts.', 'danger')
                return redirect(url_for('hdc_worker_ledger', wid=wid))
            db.session.commit()
            flash(f'Advance of {_flt(request.form.get("amount")):,.0f} recorded for {w.name}.', 'success')
            return redirect(url_for('hdc_worker_ledger', wid=wid))
        stages = Stage.query.all()
        return render_template('workers/worker_advance.html', w=w, projects=projects, stages=stages, today=_pkt_today().isoformat())


    @app.route('/hdc/workers/<int:wid>/payment', methods=['GET', 'POST'])
    @login_required
    @_money_write_required()
    def hdc_worker_payment(wid):
        w = Worker.query.get_or_404(wid)
        projects = Project.query.all()
        stages = Stage.query.all()
        snap = _worker_payable_snapshot(wid)
        if request.method == 'POST':
            entry_date = _parse_date(request.form.get('date'))
            amount = _flt(request.form.get('amount'))
            project_id = request.form.get('project_id', type=int) or None
            stage_id = request.form.get('stage_id', type=int) or None
            settle_shortfall = (request.form.get('settle_shortfall') or '').strip().lower() in ('1', 'true', 'on', 'yes')
            overpay_as_tip = (request.form.get('overpay_as_tip') or '').strip().lower() in ('1', 'true', 'on', 'yes')
            overpay_as_advance = (request.form.get('overpay_as_advance') or '').strip().lower() in ('1', 'true', 'on', 'yes')
            notes = (request.form.get('notes','') or '').strip()
            payment_row = None
            tip_row = None
            advance_row = None
            if amount <= 0:
                flash('Payment amount must be greater than zero.', 'danger')
                return redirect(url_for('hdc_worker_payment', wid=wid))

            current = _worker_payable_snapshot(wid)
            payable_now = float(current['payable'] or 0.0)
            payment_part = min(amount, payable_now)
            overpay_part = max(0.0, amount - payment_part)
            tip_part = 0.0
            advance_part = 0.0
            settlement_part = 0.0
            if settle_shortfall and amount < payable_now:
                settlement_part = payable_now - amount

            if overpay_as_tip and overpay_as_advance:
                flash('Select only one overpayment option: Tip or Excess as Advance.', 'warning')
                return redirect(url_for('hdc_worker_payment', wid=wid))

            if overpay_part > 0:
                if overpay_as_tip:
                    tip_part = overpay_part
                elif overpay_as_advance:
                    advance_part = overpay_part
                else:
                    flash('Amount exceeds payable. Please choose Tip or Excess as Advance for the extra amount.', 'danger')
                    return redirect(url_for('hdc_worker_payment', wid=wid))
            elif overpay_as_tip or overpay_as_advance:
                flash('Tip/Advance overpayment option applies only when amount exceeds payable.', 'warning')
                return redirect(url_for('hdc_worker_payment', wid=wid))

            if tip_part > 0 or settlement_part > 0:
                if not project_id or not stage_id:
                    flash('For tip or shortfall settlement, select both project and stage for proper stage-level trace.', 'warning')
                    return redirect(url_for('hdc_worker_payment', wid=wid))
                stg = Stage.query.get(stage_id)
                if (not stg) or (stg.project_id != project_id):
                    flash('Selected stage does not belong to selected project.', 'danger')
                    return redirect(url_for('hdc_worker_payment', wid=wid))

            if payment_part > 0:
                if _has_recent_duplicate(
                    LabourLedger,
                    worker_id=wid,
                    entry_type='payment',
                    amount=payment_part,
                    date=entry_date,
                    project_id=project_id,
                    stage_id=stage_id,
                    notes=notes
                ):
                    flash('Duplicate payment prevented (same values submitted too quickly).', 'warning')
                    return redirect(url_for('hdc_worker_ledger', wid=wid))
                payment_row = LabourLedger(
                    worker_id=wid, entry_type='payment',
                    amount=payment_part,
                    date=entry_date,
                    activity_at=_activity_at_for(entry_date),
                    project_id=project_id,
                    stage_id=stage_id,
                    notes=notes or f'Worker payment for {w.name}'
                )
                db.session.add(payment_row)

            if tip_part > 0:
                tip_cat = _ensure_expense_category('Tip')
                # The reconciler (_reconcile_worker_tip_ledger) keys on
                # TIP_EXPENSE_ID, so the tag has to be written into BOTH the
                # expense remarks and the ledger row notes -- exactly like the
                # accounts payment flow does. Tagging only with TIP_WORKER_ID
                # left the row surviving on a date+amount fallback, and editing
                # the tip then produced a duplicate (LABOUR_AUDIT #2).
                tip_base = (notes + ' | ' if notes else '') + f'Tip for {w.name} via settlement overpayment | TIP_WORKER_ID:{wid}'
                # Duplicate guards are deliberately remarks-independent: the
                # final remarks now carry a per-expense id, so comparing them
                # would never match a repeat submission.
                if _has_recent_duplicate(
                    Expense,
                    project_id=project_id,
                    stage_id=stage_id,
                    tip_worker_id=wid,
                    category_id=(tip_cat.id if tip_cat else None),
                    amount=tip_part,
                    date=entry_date
                ):
                    flash('Duplicate tip expense prevented.', 'warning')
                    return redirect(url_for('hdc_worker_ledger', wid=wid))
                tip_expense = Expense(
                    project_id=project_id,
                    stage_id=stage_id,
                    tip_worker_id=wid,
                    category_id=(tip_cat.id if tip_cat else None),
                    amount=tip_part,
                    date=entry_date,
                    activity_at=_activity_at_for(entry_date),
                    remarks=tip_base
                )
                db.session.add(tip_expense)
                db.session.flush()   # capture the id for the TIP_EXPENSE_ID tag
                tip_remarks = tip_base + f' | TIP_EXPENSE_ID:{tip_expense.id}'
                tip_expense.remarks = tip_remarks
                if _has_recent_duplicate(
                    LabourLedger,
                    worker_id=wid,
                    entry_type='tip',
                    amount=tip_part,
                    date=entry_date,
                    project_id=project_id,
                    stage_id=stage_id
                ):
                    flash('Duplicate tip ledger record prevented.', 'warning')
                    return redirect(url_for('hdc_worker_ledger', wid=wid))
                tip_row = LabourLedger(
                    worker_id=wid,
                    entry_type='tip',
                    amount=tip_part,
                    date=entry_date,
                    activity_at=_activity_at_for(entry_date),
                    project_id=project_id,
                    stage_id=stage_id,
                    notes=tip_remarks
                )
                db.session.add(tip_row)

            if advance_part > 0:
                advance_notes = (notes + ' | ' if notes else '') + f'Auto advance from overpayment for {w.name}'
                if _has_recent_duplicate(
                    LabourLedger,
                    worker_id=wid,
                    entry_type='advance',
                    amount=advance_part,
                    date=entry_date,
                    project_id=project_id,
                    stage_id=stage_id,
                    notes=advance_notes
                ):
                    flash('Duplicate advance prevented (same values submitted too quickly).', 'warning')
                    return redirect(url_for('hdc_worker_ledger', wid=wid))
                advance_row = LabourLedger(
                    worker_id=wid,
                    entry_type='advance',
                    amount=advance_part,
                    date=entry_date,
                    activity_at=_activity_at_for(entry_date),
                    project_id=project_id,
                    stage_id=stage_id,
                    notes=advance_notes
                )
                db.session.add(advance_row)

            if settlement_part > 0:
                settlement_remarks = (notes + ' | ' if notes else '') + f'Settlement shortfall for {w.name} | SETTLE_WORKER_ID:{wid}'
                settlement_cat = _ensure_expense_category('Settlement')
                if _has_recent_duplicate(
                    Expense,
                    project_id=project_id,
                    stage_id=stage_id,
                    category_id=(settlement_cat.id if settlement_cat else None),
                    amount=-settlement_part,
                    date=entry_date,
                    remarks=settlement_remarks
                ):
                    flash('Duplicate settlement expense prevented.', 'warning')
                    return redirect(url_for('hdc_worker_ledger', wid=wid))
                db.session.add(Expense(
                    project_id=project_id,
                    stage_id=stage_id,
                    category_id=(settlement_cat.id if settlement_cat else None),
                    amount=-settlement_part,
                    date=entry_date,
                    activity_at=_activity_at_for(entry_date),
                    remarks=settlement_remarks
                ))
                if _has_recent_duplicate(
                    LabourLedger,
                    worker_id=wid,
                    entry_type='settlement',
                    amount=settlement_part,
                    date=entry_date,
                    project_id=project_id,
                    stage_id=stage_id,
                    notes=settlement_remarks
                ):
                    flash('Duplicate settlement ledger record prevented.', 'warning')
                    return redirect(url_for('hdc_worker_ledger', wid=wid))
                db.session.add(LabourLedger(
                    worker_id=wid,
                    entry_type='settlement',
                    amount=settlement_part,
                    date=entry_date,
                    activity_at=_activity_at_for(entry_date),
                    project_id=project_id,
                    stage_id=stage_id,
                    notes=settlement_remarks
                ))

            db.session.flush()
            for row in [payment_row, tip_row, advance_row]:
                if not row:
                    continue
                ok_txn, msg_txn, _ = _accounts_post_labour_ledger_row(row, worker_name=w.name, commit=False)
                if not ok_txn:
                    db.session.rollback()
                    flash(msg_txn or 'Unable to post worker payment in unified accounts.', 'danger')
                    return redirect(url_for('hdc_worker_payment', wid=wid))
            db.session.commit()
            if tip_part > 0 and settlement_part > 0:
                flash(
                    f'Payment recorded: {payment_part:,.0f} PKR cash paid, {tip_part:,.0f} PKR posted as Tip, and {settlement_part:,.0f} PKR shortfall settled to stage.',
                    'success'
                )
            elif advance_part > 0 and settlement_part > 0:
                flash(
                    f'Payment recorded: {payment_part:,.0f} PKR paid against payable, {advance_part:,.0f} PKR saved as Advance, and {settlement_part:,.0f} PKR shortfall settled to stage.',
                    'success'
                )
            elif settlement_part > 0:
                flash(
                    f'Payment recorded: {payment_part:,.0f} PKR cash paid and {settlement_part:,.0f} PKR shortfall settled to stage.',
                    'success'
                )
            elif tip_part > 0:
                flash(
                    f'Payment recorded: {payment_part:,.0f} PKR settled worker payable and {tip_part:,.0f} PKR posted as Tip expense.',
                    'success'
                )
            elif advance_part > 0:
                flash(
                    f'Payment recorded: {payment_part:,.0f} PKR settled worker payable and {advance_part:,.0f} PKR saved as worker advance.',
                    'success'
                )
            else:
                flash(f'Payment of {payment_part:,.0f} PKR recorded for {w.name}.', 'success')
            receipt_row = payment_row or tip_row or advance_row
            if receipt_row and receipt_row.id:
                return redirect(url_for('hdc_worker_payment_receipt', wid=wid, lid=receipt_row.id))
            return redirect(url_for('hdc_worker_ledger', wid=wid))
        return render_template('workers/worker_payment.html',
            w=w,
            projects=projects,
            stages=stages,
            today=_pkt_today().isoformat(),
            payable_amount=snap['payable'],
            earned_amount=snap['earned'],
            advanced_amount=snap['advanced'],
            paid_amount=snap['paid'],
            settled_amount=snap['settled']
        )


    @app.route('/hdc/workers/<int:wid>/payment/<int:lid>/receipt')
    @login_required
    def hdc_worker_payment_receipt(wid, lid):
        w = Worker.query.get_or_404(wid)
        row = LabourLedger.query.get_or_404(lid)
        if row.worker_id != wid:
            abort(404)
        receipt_id = f'RCPT-WP-{row.id:08d}'
        project_name = (row.project.name if row.project else '-')
        stage_name = (row.stage.name if row.stage else '-')
        recent = (
            LabourLedger.query
            .filter(LabourLedger.worker_id == wid, LabourLedger.id != row.id, LabourLedger.is_void == False)
            .order_by(LabourLedger.activity_at.desc(), LabourLedger.id.desc())
            .limit(5).all()
        )
        recent_entries = [{
            'date': (r.date.strftime('%Y-%m-%d') if r.date else '-'),
            'type': (r.entry_type or '-').title(),
            'direction': 'pay',
            'party': w.name,
            'amount': float(r.amount or 0),
            'receipt_url': url_for('hdc_worker_payment_receipt', wid=wid, lid=r.id)
        } for r in recent]
        return render_template('accounts/transaction_receipt.html',
            company_profile=_receipt_company_profile(),
            receipt_id=receipt_id,
            created_at=(row.activity_at or _pkt_now_naive()),
            tx_type=f'Worker {(row.entry_type or "Payment").title()} Receipt',
            party_name=w.name,
            project_name=project_name,
            stage_name=stage_name,
            account_used='-',
            amount=float(row.amount or 0),
            amount_words=_amount_to_words(row.amount or 0),
            note=(row.notes or ''),
            reference_id=f'worker_ledger#{row.id}',
            recent_entries=recent_entries,
            recent_entries_title=f'Last 5 Ledger Entries – {w.name}',
            back_url=url_for('hdc_worker_ledger', wid=wid),
            print_label='Print / Save PDF'
        )


    @app.route('/hdc/workers/<int:wid>/ledger/<int:lid>/edit', methods=['GET', 'POST'])
    @login_required
    @_money_write_required()
    def hdc_worker_ledger_edit(wid, lid):
        w = Worker.query.get_or_404(wid)
        row = LabourLedger.query.get_or_404(lid)
        if row.worker_id != wid:
            flash('Ledger entry does not belong to selected worker.', 'danger')
            return redirect(url_for('hdc_worker_ledger', wid=wid))
        if row.entry_type == 'work':
            flash('Work entries cannot be edited.', 'warning')
            return redirect(url_for('hdc_worker_ledger', wid=wid))
        if row.is_void:
            flash('Voided entry cannot be edited.', 'warning')
            return redirect(url_for('hdc_worker_ledger', wid=wid))

        projects = Project.query.all()
        stages = Stage.query.all()
        if request.method == 'POST':
            entry_date = _parse_date(request.form.get('date'))
            amount = _flt(request.form.get('amount'))
            if amount <= 0:
                flash('Amount must be greater than zero.', 'danger')
                return redirect(url_for('hdc_worker_ledger_edit', wid=wid, lid=lid))
            # Resolve the mirrored expense BEFORE the row is mutated: the lookup
            # matches on date/amount, and tips/settlements are mirrored into
            # hdc_expense. Leaving the expense behind meant an edited tip lost
            # its identity and the reconciler wrote a second one
            # (LABOUR_AUDIT #13).
            linked = _linked_expense_for_labour_ledger(row)
            old_tip_tag = ''
            if linked is not None:
                m = re.search(r'TIP_EXPENSE_ID:\d+', row.notes or '')
                old_tip_tag = m.group(0) if m else ''

            row.amount = amount
            row.date = entry_date
            row.activity_at = _activity_at_for(entry_date)
            row.project_id = request.form.get('project_id', type=int) or None
            row.stage_id = request.form.get('stage_id', type=int) or None
            new_notes = (request.form.get('notes') or '').strip()
            # Never let an edit drop the tag that identifies the tip's expense;
            # without it the reconciler can only guess by date+amount.
            if old_tip_tag and old_tip_tag not in new_notes:
                new_notes = (new_notes + ' | ' if new_notes else '') + old_tip_tag
            row.notes = new_notes

            if linked is not None:
                linked.amount = (float(amount) if row.entry_type == 'tip'
                                 else -float(amount))
                linked.date = entry_date
                linked.activity_at = _activity_at_for(entry_date)
                linked.project_id = row.project_id
                linked.stage_id = row.stage_id

            ok_txn, msg_txn, _ = _accounts_upsert_labour_ledger_txn(w, row, commit=False)
            if not ok_txn:
                db.session.rollback()
                flash(msg_txn or 'Unable to sync ledger entry to accounts.', 'danger')
                return redirect(url_for('hdc_worker_ledger_edit', wid=wid, lid=lid))
            db.session.commit()
            extra = ' Linked tip/settlement expense updated too.' if linked is not None else ''
            flash('Ledger entry updated.' + extra, 'success')
            return redirect(url_for('hdc_worker_ledger', wid=wid))
        return render_template('workers/worker_ledger_entry_edit.html', w=w, entry=row, projects=projects, stages=stages)


    @app.route('/hdc/workers/<int:wid>/ledger/<int:lid>/void', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_worker_ledger_void(wid, lid):
        Worker.query.get_or_404(wid)
        row = LabourLedger.query.get_or_404(lid)
        if row.worker_id != wid:
            flash('Ledger entry does not belong to selected worker.', 'danger')
            return redirect(url_for('hdc_worker_ledger', wid=wid))
        if row.entry_type == 'work':
            flash('Work entries cannot be voided.', 'warning')
            return redirect(url_for('hdc_worker_ledger', wid=wid))
        if row.is_void:
            flash('Ledger entry is already voided.', 'info')
            return redirect(url_for('hdc_worker_ledger', wid=wid))
        reason = (request.form.get('void_reason') or '').strip() or 'Voided by user'
        row.is_void = True
        row.void_reason = reason
        row.voided_at = _pkt_now_naive()
        # Tips and settlements are mirrored into hdc_expense. If the expense is
        # left alive the tip reconciler re-creates the tip on the next page load
        # (LABOUR_AUDIT #3) or the project keeps a write-off the worker owes
        # again -- so void the expense together with the ledger row.
        linked = _linked_expense_for_labour_ledger(row)
        if linked is not None and not linked.is_void:
            linked.is_void = True
            linked.void_reason = reason
            linked.voided_at = row.voided_at
        _accounts_set_void_by_source(f'labour_ledger_{row.entry_type}', row.id, True)
        _accounts_set_void_by_source('worker_payment', row.id, True)
        db.session.commit()
        extra = ' Linked tip/settlement expense voided as well.' if linked is not None else ''
        flash('Ledger entry voided successfully.' + extra, 'success')
        return redirect(url_for('hdc_worker_ledger', wid=wid))


    @app.route('/hdc/workers/<int:wid>/ledger/<int:lid>/restore', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_worker_ledger_restore(wid, lid):
        Worker.query.get_or_404(wid)
        row = LabourLedger.query.get_or_404(lid)
        if row.worker_id != wid:
            flash('Ledger entry does not belong to selected worker.', 'danger')
            return redirect(url_for('hdc_worker_ledger', wid=wid))
        if row.entry_type not in ('advance', 'payment', 'tip', 'settlement'):
            flash('Only advance/payment/tip/settlement entries can be restored manually.', 'warning')
            return redirect(url_for('hdc_worker_ledger', wid=wid))
        if not row.is_void:
            flash('Ledger entry is already active.', 'info')
            return redirect(url_for('hdc_worker_ledger', wid=wid))
        row.is_void = False
        row.void_reason = None
        row.voided_at = None
        # Bring the mirrored expense back with it, otherwise the tip reconciler
        # would treat the active tip as unsynced and project cost would stay
        # reduced for a settlement that is owed again (LABOUR_AUDIT #3).
        linked = _linked_expense_for_labour_ledger(row)
        if linked is not None and linked.is_void:
            linked.is_void = False
            linked.void_reason = None
            linked.voided_at = None
        _accounts_set_void_by_source(f'labour_ledger_{row.entry_type}', row.id, False)
        _accounts_set_void_by_source('worker_payment', row.id, False)
        db.session.commit()
        extra = ' Linked tip/settlement expense restored as well.' if linked is not None else ''
        flash('Ledger entry restored successfully.' + extra, 'success')
        return redirect(url_for('hdc_worker_ledger', wid=wid))


    @app.route('/hdc/workers/<int:wid>/rate', methods=['GET', 'POST'])
    @login_required
    @_money_write_required()
    def hdc_worker_rate(wid):
        w = Worker.query.get_or_404(wid)
        if request.method == 'POST':
            wage_type = request.form.get('wage_type','daily')
            new_rate = _flt(request.form.get('new_rate'))
            eff_from = _parse_date(request.form.get('effective_from'))
            old_wage_type = w.wage_type or 'daily'
            old_rate = w.base_daily_wage if old_wage_type == 'daily' else (w.hourly_rate if old_wage_type == 'hourly' else w.rate_per_sqft)

            # Ensure historical baseline exists so backdated entries can resolve prior rates.
            if not WorkerRate.query.filter_by(worker_id=wid).first():
                baseline_from = w.created_at.date() if w.created_at else _pkt_today()
                db.session.add(WorkerRate(
                    worker_id=wid, wage_type=old_wage_type, rate=old_rate,
                    effective_from=baseline_from,
                    reason='Initial baseline (auto snapshot)'
                ))

            db.session.add(WorkerRate(
                worker_id=wid, wage_type=wage_type, rate=new_rate,
                effective_from=eff_from,
                reason=request.form.get('reason','')))
            if wage_type == 'daily':
                db.session.add(LabourRateHistory(
                    worker_id=wid, old_rate=old_rate, new_rate=new_rate,
                    effective_from=eff_from,
                    reason=request.form.get('reason','')))
            w.wage_type = wage_type
            if wage_type == 'daily':
                w.base_daily_wage = new_rate
            elif wage_type == 'hourly':
                w.hourly_rate = new_rate
            else:
                w.rate_per_sqft = new_rate
            db.session.commit()
            flash(f'Rate updated from {old_rate:,.0f} ? {new_rate:,.0f}.', 'success')
            return redirect(url_for('hdc_worker_ledger', wid=wid))
        return render_template('workers/worker_rate.html', w=w, today=_pkt_today().isoformat())
