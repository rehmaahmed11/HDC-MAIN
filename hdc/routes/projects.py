"""HDC routes: Projects, stages, drawings, stage library, owner payments.

Moved verbatim from hdc_erp.py; each handler keeps its
original @app.route decorator and endpoint name.
"""

import os
from uuid import uuid4

from flask import abort, current_app, flash, jsonify, redirect, render_template, request, send_file, url_for
from flask_login import login_required
from sqlalchemy import func
from werkzeug.utils import secure_filename

from hdc.config import STAGE_DRAWINGS_DIR
from hdc.extensions import db
from hdc.models.accounts import Account, OwnerPayment
from hdc.models.materials import Material
from hdc.models.projects import Project, Stage, StageDefinition, StageDrawing, StageRateHistory
from hdc.models.subcontract import Subcontractor
from hdc.models.workforce import TimeEntry, Worker
from hdc.services.accounts import _ACCOUNT_COMPANY_TYPES, _accounts_post_owner_receipt, _accounts_set_void_by_source
from hdc.services.aggregation import _apply_aggregated_project_costs, _apply_aggregated_stage_costs, _running_projects_receivable_rows
from hdc.services.lookups import _next_project_code
from hdc.services.receipts import _owner_payment_recent_entries, _receipt_company_profile
from hdc.services.reporting import _build_stage_event_ledger
from hdc.services.subcontract import _log_subcontract_event
from hdc.services.timekeeping import _has_recent_duplicate
from hdc.utils.dates import _pkt_now_naive, _pkt_today
from hdc.utils.format import _activity_at_for, _amount_to_words, _flt, _is_pdf_upload, _parse_date

def register(app):
    """Register Projects, stages, drawings, stage library, owner payments."""
    # â”€â”€ Projects â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    @app.route('/hdc/projects')
    @login_required
    def hdc_projects():
        projects = Project.query.order_by(Project.created_at.desc()).all()
        _apply_aggregated_project_costs(projects)
        return render_template('projects/projects.html', projects=projects)


    @app.route('/hdc/projects/add', methods=['GET', 'POST'])
    @login_required
    def hdc_add_project():
        if request.method == 'POST':
            code = (request.form.get('project_code','') or '').strip().upper()
            if not code or Project.query.filter_by(project_code=code).first():
                code = _next_project_code()
            name = (request.form.get('name','') or '').strip()
            if not name:
                name = " ".join(x for x in [
                    (request.form.get('client','') or '').strip(),
                    (request.form.get('location','') or '').strip()
                ] if x) or code
            total_sqft = _flt(request.form.get('total_sqft'))
            owner_rate = _flt(request.form.get('owner_rate'))
            owner_lump = _flt(request.form.get('lump_sum'))
            # Contract type is inferred on add form (field removed).
            if owner_lump > 0 and owner_rate > 0 and total_sqft > 0:
                inferred_contract_type = 'mixed'
            elif owner_lump > 0 and not (owner_rate > 0 and total_sqft > 0):
                inferred_contract_type = 'lump_sum'
            else:
                inferred_contract_type = 'sqft'
            p = Project(
                project_code=code,
                name=name,
                client=request.form.get('client','').strip(),
                client_phone=request.form.get('client_phone','').strip(),
                location=request.form.get('location','').strip(),
                total_constructed_sqft=total_sqft,
                owner_rate_per_sqft=owner_rate,
                owner_lump_sum=owner_lump,
                contract_type=inferred_contract_type,
                start_date=_parse_date(request.form.get('start_date')),
                status=request.form.get('status','active'),
                planned_start=_parse_date(request.form.get('start_date')),
                planned_end=_parse_date(request.form.get('planned_end')) if request.form.get('planned_end') else None)
            db.session.add(p); db.session.commit()
            flash(f'Project "{p.name}" added.', 'success')
            return redirect(url_for('hdc_project_detail', pid=p.id))
        return render_template('projects/add_project.html', today=_pkt_today().isoformat())


    @app.route('/hdc/projects/<int:pid>')
    @login_required
    def hdc_project_detail(pid):
        p        = Project.query.get_or_404(pid)
        workers  = Worker.query.filter_by(active_status=True).all()
        stages   = Stage.query.filter_by(project_id=pid).order_by(Stage.id).all()
        _apply_aggregated_stage_costs(stages)
        _apply_aggregated_project_costs([p])
        stage_defs = (StageDefinition.query
                      .filter_by(project_id=pid, active_status=True)
                      .order_by(StageDefinition.default_order, StageDefinition.id)
                      .all())
        materials  = Material.query.filter_by(is_active=True).all()
        subcontractor_pool = Subcontractor.query.order_by(Subcontractor.name.asc(), Subcontractor.id.asc()).all()
        recent_time_entries = (TimeEntry.query
                               .filter(TimeEntry.project_id == pid, TimeEntry.is_void == False)
                               .order_by(TimeEntry.check_in.desc(), TimeEntry.id.desc())
                               .limit(10)
                               .all())
        sqft_stages = [s for s in stages if (str(s.contract_basis or '').strip().lower() == 'per sq ft') and float(s.qty_sqft or 0.0) > 0]
        if sqft_stages:
            project_total_sqft = float(sum(float(s.qty_sqft or 0.0) for s in sqft_stages))
            owner_sqft_charges = float(sum((float(s.effective_rate or 0.0) * float(s.qty_sqft or 0.0)) for s in sqft_stages))
        else:
            project_total_sqft = float(p.total_constructed_sqft or 0.0)
            owner_sqft_charges = project_total_sqft * float(p.owner_rate_per_sqft or 0.0)
        owner_sqft_rate = (owner_sqft_charges / project_total_sqft) if project_total_sqft > 0 else 0.0
        sqft_metrics_valid = bool(project_total_sqft > 0 and owner_sqft_rate > 0)
        cost_per_sqft = (float(p.total_cost or 0.0) / project_total_sqft) if project_total_sqft > 0 else 0.0
        profit_per_sqft = ((owner_sqft_charges - float(p.total_cost or 0.0)) / project_total_sqft) if project_total_sqft > 0 else 0.0
        owner_payments = (OwnerPayment.query
                          .filter_by(project_id=pid, is_void=False)
                          .order_by(OwnerPayment.date.desc(), OwnerPayment.id.desc())
                          .all())
        owner_payments_voided = (OwnerPayment.query
                                 .filter_by(project_id=pid, is_void=True)
                                 .order_by(OwnerPayment.voided_at.desc(), OwnerPayment.id.desc())
                                 .all())
        receiving_accounts = (Account.query
                              .filter(
                                  Account.is_void == False,
                                  func.lower(func.coalesce(Account.status, 'active')) == 'active',
                                  func.lower(func.coalesce(Account.type, '')).in_(_ACCOUNT_COMPANY_TYPES)
                              )
                              .order_by(Account.name.asc(), Account.id.asc())
                              .all())
        project_receivable_rows = _running_projects_receivable_rows()
        return render_template('projects/project_detail.html',
            p=p, workers=workers, stages=stages,
            stage_defs=stage_defs, materials=materials,
            subcontractor_pool=subcontractor_pool,
            receiving_accounts=receiving_accounts,
            project_receivable_rows=project_receivable_rows,
            owner_payments=owner_payments,
            owner_payments_voided=owner_payments_voided,
            recent_time_entries=recent_time_entries,
            project_total_sqft=project_total_sqft,
            owner_sqft_charges=owner_sqft_charges,
            owner_sqft_rate=owner_sqft_rate,
            sqft_metrics_valid=sqft_metrics_valid,
            cost_per_sqft=cost_per_sqft,
            profit_per_sqft=profit_per_sqft,
            show_cost_split=(request.args.get('show_cost_split') == '1'),
            today=_pkt_today().isoformat())


    @app.route('/hdc/stage/<int:sid>/ledger')
    @login_required
    def hdc_stage_ledger(sid):
        s = Stage.query.get_or_404(sid)
        rows, totals, grand_total = _build_stage_event_ledger(s)
        stage_sqft = float(s.qty_sqft or 0.0)
        stage_is_sqft = (str(s.contract_basis or '').strip().lower() == 'per sq ft') and stage_sqft > 0
        stage_owner_sqft_rate = float(s.effective_rate or 0.0) if stage_is_sqft else 0.0
        stage_owner_sqft_charges = stage_sqft * stage_owner_sqft_rate if stage_is_sqft else 0.0
        stage_cost_per_sqft = (float(grand_total or 0.0) / stage_sqft) if stage_sqft > 0 else 0.0
        stage_profit_per_sqft = ((stage_owner_sqft_charges - float(grand_total or 0.0)) / stage_sqft) if stage_sqft > 0 else 0.0
        return render_template('projects/stage_ledger.html',
            s=s,
            p=s.project,
            rows=rows,
            totals=totals,
            grand_total=grand_total,
            stage_sqft=stage_sqft,
            stage_is_sqft=stage_is_sqft,
            stage_owner_sqft_rate=stage_owner_sqft_rate,
            stage_owner_sqft_charges=stage_owner_sqft_charges,
            stage_cost_per_sqft=stage_cost_per_sqft,
            stage_profit_per_sqft=stage_profit_per_sqft,
            show_cost_split=(request.args.get('show_cost_split') == '1')
        )


    @app.route('/hdc/projects/<int:pid>/edit', methods=['GET', 'POST'])
    @login_required
    def hdc_edit_project(pid):
        p = Project.query.get_or_404(pid)
        if request.method == 'POST':
            p.name                  = request.form.get('name','').strip()
            p.client                = request.form.get('client','').strip()
            p.client_phone          = request.form.get('client_phone','').strip()
            p.location              = request.form.get('location','').strip()
            p.total_constructed_sqft= _flt(request.form.get('total_sqft'))
            p.owner_rate_per_sqft   = _flt(request.form.get('owner_rate'))
            p.owner_lump_sum        = _flt(request.form.get('lump_sum'))
            p.contract_type         = request.form.get('contract_type','sqft')
            p.status                = request.form.get('status','active')
            p.planned_end           = _parse_date(request.form.get('planned_end')) if request.form.get('planned_end') else None
            db.session.commit()
            flash('Project updated.', 'success')
            return redirect(url_for('hdc_project_detail', pid=pid))
        return render_template('projects/edit_project.html', p=p)


    @app.route('/hdc/projects/<int:pid>/owner_payment', methods=['POST'])
    @login_required
    def hdc_add_owner_payment(pid):
        prj = Project.query.get_or_404(pid)
        pay_date = _parse_date(request.form.get('date'))
        amount = _flt(request.form.get('amount'))
        received_to_account_id = request.form.get('received_to_account_id', type=int)
        if amount <= 0:
            flash('Payment amount must be greater than zero.', 'danger')
            return redirect(url_for('hdc_project_detail', pid=pid))
        recv_acc = None
        if received_to_account_id:
            recv_acc = Account.query.get(int(received_to_account_id))
        if (not recv_acc) or recv_acc.is_void or str(recv_acc.status or 'active').strip().lower() != 'active' \
           or str(recv_acc.type or '').strip().lower() not in _ACCOUNT_COMPANY_TYPES:
            flash('Select a valid active receiving account (company/cash/bank).', 'danger')
            return redirect(url_for('hdc_project_detail', pid=pid))
        remarks = (request.form.get('remarks','') or '').strip()
        if _has_recent_duplicate(
            OwnerPayment,
            project_id=pid,
            received_to_account_id=int(recv_acc.id),
            amount=amount,
            date=pay_date,
            remarks=remarks
        ):
            flash('Duplicate owner payment prevented (same values submitted too quickly).', 'warning')
            return redirect(url_for('hdc_project_detail', pid=pid))
        op_row = OwnerPayment(
            project_id=pid, amount=amount,
            date=pay_date,
            received_to_account_id=int(recv_acc.id),
            activity_at=_activity_at_for(pay_date),
            remarks=remarks)
        db.session.add(op_row)
        db.session.flush()
        ok_txn, msg_txn, _ = _accounts_post_owner_receipt(op_row, project=prj, commit=False)
        if not ok_txn:
            db.session.rollback()
            flash(msg_txn or 'Unable to post owner payment in unified accounts.', 'danger')
            return redirect(url_for('hdc_project_detail', pid=pid))
        db.session.commit()
        flash('Payment recorded.', 'success')
        return redirect(url_for('hdc_project_detail', pid=pid))


    @app.route('/hdc/projects/<int:pid>/owner_payment/<int:oid>/receipt')
    @login_required
    def hdc_owner_payment_receipt(pid, oid):
        op = OwnerPayment.query.get_or_404(oid)
        if int(op.project_id or 0) != int(pid):
            abort(404)
        prj = Project.query.get(op.project_id) if op.project_id else None
        receipt_id = f"RCPT-OP-{op.id:08d}"
        return render_template('accounts/transaction_receipt.html',
            company_profile=_receipt_company_profile(),
            receipt_id=receipt_id,
            created_at=(op.activity_at or op.created_at or _pkt_now_naive()),
            tx_type='Owner Payment Receipt',
            party_name=((prj.client if prj else '') or 'Client'),
            project_name=(prj.name if prj else '-'),
            stage_name='-',
            account_used=((op.received_to_account.name if op.received_to_account else 'Company Cash')),
            amount=float(op.amount or 0.0),
            amount_words=_amount_to_words(op.amount or 0.0),
            note=(op.remarks or ''),
            reference_id=f'owner_payment#{op.id}',
            recent_entries=_owner_payment_recent_entries(pid, exclude_id=op.id, limit=5),
            recent_entries_title='Last 5 Owner Payment Entries',
            back_url=url_for('hdc_project_detail', pid=pid),
            print_label='Print / Save PDF'
        )


    @app.route('/hdc/projects/<int:pid>/owner_payment/<int:oid>/delete', methods=['POST'])
    @login_required
    def hdc_delete_owner_payment(pid, oid):
        op = OwnerPayment.query.get_or_404(oid)
        if int(op.project_id or 0) != int(pid):
            flash('Payment does not belong to selected project.', 'danger')
            return redirect(url_for('hdc_project_detail', pid=pid))
        if op.is_void:
            flash('Payment is already voided.', 'info')
            return redirect(url_for('hdc_project_detail', pid=pid))
        op.is_void = True
        op.void_reason = (request.form.get('void_reason') or '').strip() or 'Voided by user'
        op.voided_at = _pkt_now_naive()
        _accounts_set_void_by_source('owner_payment', op.id, True)
        db.session.commit()
        flash('Payment voided.', 'warning')
        return redirect(url_for('hdc_project_detail', pid=pid))


    @app.route('/hdc/projects/<int:pid>/owner_payment/<int:oid>/restore', methods=['POST'])
    @login_required
    def hdc_restore_owner_payment(pid, oid):
        op = OwnerPayment.query.get_or_404(oid)
        if int(op.project_id or 0) != int(pid):
            flash('Payment does not belong to selected project.', 'danger')
            return redirect(url_for('hdc_project_detail', pid=pid))
        if not op.is_void:
            flash('Payment is already active.', 'info')
            return redirect(url_for('hdc_project_detail', pid=pid))
        op.is_void = False
        op.void_reason = None
        op.voided_at = None
        _accounts_set_void_by_source('owner_payment', op.id, False)
        db.session.commit()
        flash('Payment restored.', 'success')
        return redirect(url_for('hdc_project_detail', pid=pid))


    # â”€â”€ Stages â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    @app.route('/hdc/projects/<int:pid>/stage/add', methods=['GET', 'POST'])
    @login_required
    def hdc_add_stage(pid):
        p    = Project.query.get_or_404(pid)
        defs = (StageDefinition.query
                .filter_by(project_id=pid, active_status=True)
                .order_by(StageDefinition.default_order, StageDefinition.id)
                .all())
        if request.method == 'POST':
            def_id = request.form.get('definition_id', type=int) or None
            name   = request.form.get('name','').strip()
            if def_id and not name:
                sd   = StageDefinition.query.filter_by(id=def_id, project_id=pid).first()
                name = sd.name if sd else name
            basis  = request.form.get('contract_basis','')
            rate   = _flt(request.form.get('rate_per_sqft'))
            disc   = _flt(request.form.get('discount_per_sqft'))
            qty    = _flt(request.form.get('qty_sqft'))
            lump   = _flt(request.form.get('lump_sum_value'))
            est_cost = _flt(request.form.get('estimated_cost'))
            progress = _flt(request.form.get('progress'))
            start_d  = _parse_date(request.form.get('start_date')) if request.form.get('start_date') else None
            end_d    = _parse_date(request.form.get('end_date')) if request.form.get('end_date') else None
            if not basis and (rate > 0 or qty > 0):
                basis = 'Per Sq Ft'
            s = Stage(
                project_id=pid, definition_id=def_id, name=name,
                status=request.form.get('status','Active'),
                estimated_cost=est_cost, progress=progress,
                start_date=start_d, end_date=end_d,
                contract_basis=basis, rate_per_sqft=rate,
                discount_per_sqft=disc, qty_sqft=qty, lump_sum_value=lump,
                original_contract_basis=basis, original_rate_per_sqft=rate,
                original_discount_per_sqft=disc, original_qty_sqft=qty,
                original_lump_sum_value=lump)
            db.session.add(s); db.session.commit()
            flash(f'Stage "{s.name}" added.', 'success')
            return redirect(url_for('hdc_project_detail', pid=pid))
        return render_template('projects/stage_form.html', p=p, stage=None, defs=defs, mode='add')


    @app.route('/hdc/stage/<int:sid>/edit', methods=['GET', 'POST'])
    @login_required
    def hdc_edit_stage(sid):
        s = Stage.query.get_or_404(sid)
        p = s.project
        if request.method == 'POST':
            new_basis = request.form.get('contract_basis','')
            new_rate  = _flt(request.form.get('rate_per_sqft'))
            new_disc  = _flt(request.form.get('discount_per_sqft'))
            new_qty   = _flt(request.form.get('qty_sqft'))
            new_lump  = _flt(request.form.get('lump_sum_value'))
            changed   = (new_basis != s.contract_basis or new_rate != s.rate_per_sqft or
                         new_disc != s.discount_per_sqft or new_qty != s.qty_sqft or new_lump != s.lump_sum_value)
            if changed:
                db.session.add(StageRateHistory(
                    stage_id=s.id,
                    old_contract_basis=s.contract_basis, new_contract_basis=new_basis,
                    old_rate_per_sqft=s.rate_per_sqft, new_rate_per_sqft=new_rate,
                    old_discount_per_sqft=s.discount_per_sqft, new_discount_per_sqft=new_disc,
                    old_qty_sqft=s.qty_sqft, new_qty_sqft=new_qty,
                    old_lump_sum_value=s.lump_sum_value, new_lump_sum_value=new_lump,
                    reason=request.form.get('reason','')))
            s.name           = request.form.get('name','').strip() or s.name
            s.status         = request.form.get('status', s.status)
            s.estimated_cost = _flt(request.form.get('estimated_cost'))
            s.progress       = _flt(request.form.get('progress'))
            s.start_date     = _parse_date(request.form.get('start_date')) if request.form.get('start_date') else None
            s.end_date       = _parse_date(request.form.get('end_date')) if request.form.get('end_date') else None
            s.contract_basis = new_basis; s.rate_per_sqft = new_rate
            s.discount_per_sqft = new_disc; s.qty_sqft = new_qty; s.lump_sum_value = new_lump
            db.session.commit()
            flash('Stage updated.', 'success')
            return redirect(url_for('hdc_project_detail', pid=p.id))
        return render_template('projects/stage_form.html', p=p, stage=s, defs=[], mode='edit')


    @app.route('/hdc/stage/<int:sid>/delete', methods=['POST'])
    @login_required
    def hdc_delete_stage(sid):
        s = Stage.query.get_or_404(sid)
        pid = s.project_id
        db.session.delete(s); db.session.commit()
        flash('Stage deleted.', 'success')
        return redirect(url_for('hdc_project_detail', pid=pid))


    @app.route('/hdc/stage/<int:sid>/status', methods=['POST'])
    @login_required
    def hdc_stage_status(sid):
        s = Stage.query.get_or_404(sid)
        old_status = (s.status or '').strip().lower()
        s.status = request.form.get('status', s.status)
        new_status = (s.status or '').strip().lower()
        auto_note = ''
        auto_sub = (request.form.get('auto_sub_complete') or '').strip() == '1'
        if new_status in ('completed', 'complete') and s.assigned_subcontractor_id and not auto_sub:
            sub_chk = s.assigned_subcontractor
            if sub_chk and float(sub_chk.effective_progress_percentage or 0.0) < 100.0:
                s.status = old_status or s.status
                flash(f'Cannot complete stage "{s.name}" because subcontractor "{sub_chk.name}" is at {float(sub_chk.effective_progress_percentage or 0.0):.2f}%. Update Sub Completion % to 100 or use force complete action.', 'danger')
                return redirect(url_for('hdc_project_detail', pid=s.project_id))
        if auto_sub and new_status in ('completed', 'complete') and s.assigned_subcontractor_id:
            sub = s.assigned_subcontractor
            if sub:
                old_pct = float(sub.work_done_percentage or 0.0)
                if old_pct < 100.0:
                    sub.work_done_percentage = 100.0
                    _log_subcontract_event(
                        sub=sub,
                        event_type='progress',
                        from_value=f'{old_pct:.2f}%',
                        to_value='100.00%',
                        notes=f'Auto-updated on stage completion ({s.name})',
                        project_id=s.project_id,
                        stage_id=s.id
                    )
                    auto_note = f' Subcontractor progress auto-set to 100% ({sub.name}).'
        if old_status != new_status and s.assigned_subcontractor:
            _log_subcontract_event(
                sub=s.assigned_subcontractor,
                event_type='status',
                from_value=old_status or '-',
                to_value=new_status or '-',
                notes=f'Stage status changed: {s.name}',
                project_id=s.project_id,
                stage_id=s.id
            )
        db.session.commit()
        flash(f'Stage marked {s.status}.{auto_note}', 'success')
        return redirect(url_for('hdc_project_detail', pid=s.project_id))


    @app.route('/hdc/stage/<int:sid>/sub-progress', methods=['POST'])
    @login_required
    def hdc_stage_sub_progress(sid):
        s = Stage.query.get_or_404(sid)
        sub = s.assigned_subcontractor
        if not sub:
            flash('No subcontractor assigned to this stage.', 'warning')
            return redirect(url_for('hdc_project_detail', pid=s.project_id))
        pct = _flt(request.form.get('work_done_percentage'))
        pct = max(0.0, min(100.0, float(pct or 0.0)))
        old_pct = float(sub.work_done_percentage or 0.0)
        sub.work_done_percentage = pct
        _log_subcontract_event(
            sub=sub,
            event_type='progress',
            from_value=f'{old_pct:.2f}%',
            to_value=f'{pct:.2f}%',
            notes=f'Stage-level completion update ({s.name})',
            project_id=s.project_id,
            stage_id=s.id
        )
        db.session.commit()
        flash(f'Subcontractor completion updated to {pct:.2f}% for stage "{s.name}".', 'success')
        return redirect(url_for('hdc_project_detail', pid=s.project_id))


    @app.route('/hdc/stage/<int:sid>/drawings/upload', methods=['POST'])
    @login_required
    def hdc_stage_drawings_upload(sid):
        s = Stage.query.get_or_404(sid)
        files = request.files.getlist('drawings')
        uploaded = 0
        for f in files:
            if not f or not (f.filename or '').strip():
                continue
            if not _is_pdf_upload(f):
                continue
            original = secure_filename(f.filename or 'drawing.pdf') or 'drawing.pdf'
            stored = f"{uuid4().hex}.pdf"
            path = os.path.join(STAGE_DRAWINGS_DIR, stored)
            f.save(path)
            db.session.add(StageDrawing(
                stage_id=s.id,
                original_name=original,
                stored_name=stored
            ))
            uploaded += 1
        db.session.commit()
        if uploaded:
            flash(f'{uploaded} PDF drawing(s) uploaded.', 'success')
        else:
            flash('No valid PDF selected.', 'warning')
        return redirect(url_for('hdc_project_detail', pid=s.project_id))


    @app.route('/hdc/stage/drawing/<int:did>/view')
    @login_required
    def hdc_stage_drawing_view(did):
        d = StageDrawing.query.get_or_404(did)
        path = os.path.join(STAGE_DRAWINGS_DIR, d.stored_name or '')
        if not os.path.exists(path):
            flash('Drawing file not found on disk.', 'danger')
            return redirect(url_for('hdc_project_detail', pid=d.stage.project_id))
        return send_file(path, mimetype='application/pdf', as_attachment=False, download_name=d.original_name)


    @app.route('/hdc/stage/drawing/<int:did>/delete', methods=['POST'])
    @login_required
    def hdc_stage_drawing_delete(did):
        d = StageDrawing.query.get_or_404(did)
        pid = d.stage.project_id
        path = os.path.join(STAGE_DRAWINGS_DIR, d.stored_name or '')
        db.session.delete(d)
        db.session.commit()
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception as ex:
            current_app.logger.warning('Could not remove stage drawing file: %s', ex)
        flash('Drawing deleted.', 'success')
        return redirect(url_for('hdc_project_detail', pid=pid))


    @app.route('/hdc/stage/drawing/<int:did>/replace', methods=['POST'])
    @login_required
    def hdc_stage_drawing_replace(did):
        d = StageDrawing.query.get_or_404(did)
        pid = d.stage.project_id
        f = request.files.get('drawing_file')
        if not _is_pdf_upload(f):
            flash('Please upload a valid PDF file.', 'warning')
            return redirect(url_for('hdc_project_detail', pid=pid))
        old_path = os.path.join(STAGE_DRAWINGS_DIR, d.stored_name or '')
        new_original = secure_filename(f.filename or 'drawing.pdf') or 'drawing.pdf'
        new_stored = f"{uuid4().hex}.pdf"
        new_path = os.path.join(STAGE_DRAWINGS_DIR, new_stored)
        f.save(new_path)
        d.original_name = new_original
        d.stored_name = new_stored
        d.updated_at = _pkt_now_naive()
        db.session.commit()
        try:
            if os.path.exists(old_path):
                os.remove(old_path)
        except Exception as ex:
            current_app.logger.warning('Could not remove replaced stage drawing file: %s', ex)
        flash('Drawing replaced successfully.', 'success')
        return redirect(url_for('hdc_project_detail', pid=pid))


    @app.route('/hdc/projects/<int:pid>/bulk_stages', methods=['POST'])
    @login_required
    def hdc_bulk_add_stages(pid):
        Project.query.get_or_404(pid)
        ids = request.form.getlist('definition_ids', type=int)
        added = 0
        for did in ids:
            sd = StageDefinition.query.filter_by(id=did, project_id=pid, active_status=True).first()
            if sd:
                db.session.add(Stage(project_id=pid, definition_id=did, name=sd.name))
                added += 1
        db.session.commit()
        flash(f'{added} stage(s) added.', 'success')
        return redirect(url_for('hdc_project_detail', pid=pid))


    # Stage Library
    @app.route('/hdc/stage-library', methods=['GET', 'POST'])
    @login_required
    def hdc_stage_library():
        pid = request.args.get('project_id', type=int)
        if request.method == 'POST':
            pid = request.form.get('project_id', type=int) or pid
        if not pid:
            flash('Select a project to manage its stage library.', 'warning')
            return redirect(url_for('hdc_projects'))
        project = Project.query.get_or_404(pid)

        if request.method == 'POST':
            action = request.form.get('action','add')
            if action == 'add':
                name = request.form.get('name','').strip()
                exists = StageDefinition.query.filter(
                    StageDefinition.project_id == pid,
                    func.lower(StageDefinition.name) == name.lower()
                ).first() if name else None
                if name and not exists:
                    db.session.add(StageDefinition(
                        project_id=pid,
                        name=name,
                        default_order=_flt(request.form.get('order',0)),
                        active_status=True
                    ))
                    db.session.commit()
                    flash(f'"{name}" added to library.', 'success')
                else:
                    flash('Name empty or already exists.', 'warning')
            elif action == 'edit':
                sd = StageDefinition.query.filter_by(
                    id=request.form.get('def_id', type=int),
                    project_id=pid
                ).first()
                new_name = (request.form.get('name') or '').strip()
                if not sd:
                    flash('Stage definition not found.', 'danger')
                elif not new_name:
                    flash('Stage name is required.', 'warning')
                else:
                    dupe = StageDefinition.query.filter(
                        StageDefinition.project_id == pid,
                        func.lower(StageDefinition.name) == new_name.lower(),
                        StageDefinition.id != sd.id
                    ).first()
                    if dupe:
                        flash('Another stage with this name already exists.', 'warning')
                    else:
                        sd.name = new_name
                        sd.default_order = _flt(request.form.get('order', sd.default_order))
                        db.session.commit()
                        flash('Stage definition updated.', 'success')
            elif action == 'delete':
                sd = StageDefinition.query.filter_by(
                    id=request.form.get('def_id', type=int),
                    project_id=pid
                ).first()
                if sd:
                    used = Stage.query.filter_by(definition_id=sd.id).count()
                    if used > 0:
                        sd.active_status = False
                        db.session.commit()
                        flash('Stage has existing data, so it was suspended (not deleted).', 'warning')
                    else:
                        db.session.delete(sd); db.session.commit()
                        flash('Stage definition removed.', 'success')
            elif action == 'toggle':
                sd = StageDefinition.query.filter_by(
                    id=request.form.get('def_id', type=int),
                    project_id=pid
                ).first()
                if sd:
                    sd.active_status = not bool(sd.active_status)
                    db.session.commit()
                    flash(f'Stage definition {"reactivated" if sd.active_status else "suspended"}.', 'success')
            return redirect(url_for('hdc_stage_library', project_id=pid))
        defs = (StageDefinition.query
                .filter_by(project_id=pid)
                .order_by(StageDefinition.default_order, StageDefinition.id)
                .all())
        return render_template('projects/stage_library.html', defs=defs, project=project)


    @app.route('/hdc/stages')
    @login_required
    def hdc_stage_overview():
        projects = Project.query.order_by(Project.name).all()
        project_id = request.args.get('project_id', type=int)
        q = Stage.query
        if project_id:
            q = q.filter(Stage.project_id == project_id)
        stages = q.order_by(Stage.id.desc()).all()
        _apply_aggregated_stage_costs(stages)
        return render_template('projects/stage_overview.html',
            stages=stages,
            projects=projects,
            selected_project=project_id
        )


    @app.route('/hdc/api/project_stages/<int:pid>')
    @login_required
    def hdc_api_project_stages(pid):
        stages = Stage.query.filter_by(project_id=pid).order_by(Stage.id).all()
        return jsonify([{'id': s.id, 'name': s.name} for s in stages])
