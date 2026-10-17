"""HDC routes: Projects, stages, drawings, stage library, owner payments.

Moved verbatim from hdc_erp.py; each handler keeps its
original @app.route decorator and endpoint name.
"""

import os
from uuid import uuid4

from flask import abort, current_app, flash, jsonify, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required
from sqlalchemy import func
from werkzeug.utils import secure_filename

from hdc.config import get_runtime_settings
from hdc.extensions import _money_only, _money_write_required, db
from hdc.models.accounts import Account, OwnerPayment
from hdc.models.cashflow import CashFlowParty
from hdc.models.materials import Material, SupplierSite
from hdc.models.projects import Project, Stage, StageDefinition, StageDrawing, StageRateHistory
from hdc.models.subcontract import Subcontractor
from hdc.models.workforce import TimeEntry, Worker
from hdc.services.accounts import _ACCOUNT_COMPANY_TYPES, _accounts_post_owner_receipt, _accounts_set_void_by_source
from hdc.services.aggregation import _apply_aggregated_project_costs, _apply_aggregated_stage_costs, _running_projects_receivable_rows
from hdc.services.lookups import _next_project_code
from hdc.services.parties import (
    known_client_party_names, link_project_to_party, party_by_name,
    party_for_project, party_name_key,
)
from hdc.services.permissions import may_access_path, permission_for, permission_landing_url
from hdc.services.record_permissions import record_allowed
from hdc.services.purchase import _site_supplier_supplied_rows
from hdc.services.receipts import _owner_payment_recent_entries, _receipt_company_profile
from hdc.services.reporting import _build_stage_event_ledger
from hdc.services.subcontract import _log_subcontract_event, assign_subcontractors_from_form, selected_subcontractor_ids
from hdc.services.timekeeping import _has_recent_duplicate
from hdc.utils.dates import _pkt_now_naive, _pkt_today
from hdc.utils.format import _activity_at_for, _amount_to_words, _flt, _is_pdf_upload, _parse_date

def _link_client_party(project, client_name, *, party_id=None, phone=None):
    """Ensure the project's client exists in Parties and link the exact row.

    ``party_id`` is the party the operator started from (Parties → New Project,
    or the party ledger).  It is honoured only while the submitted client name
    still describes the same person; a changed name is re-resolved, so a
    project can never end up pointing at somebody it is not for.

    Runs in a savepoint so a failure undoes only the party writes, never the
    project being saved; the failure is logged, not hidden.

    Returns ``(ok, party, created)``.  ``ok`` is False only when the Party side
    failed — the project itself is still saved and the operator is warned.
    """
    from flask import current_app
    from hdc.services.cashflow_register import ensure_party
    from hdc.services.party_categories import ensure_party_has_category

    name = (client_name or '').strip()
    party = db.session.get(CashFlowParty, int(party_id)) if party_id else None
    if party is not None and name and party_name_key(party.name) != party_name_key(name):
        party = None  # the client was changed on the form — resolve the new name
    if not name and party is not None:
        name = (party.name or '').strip()
        project.client = name[:100]
    if not name:
        project.client_party_id = None
        return True, None, False
    try:
        with db.session.begin_nested():
            created = False
            if party is None:
                party, created = ensure_party(
                    name,
                    party_type='client',
                    phone=(phone or project.client_phone or '').strip() or None,
                    note=f"Client / Owner of project {project.project_code} — {project.name}"[:300],
                )
            else:
                # Enrich, never overwrite: a hidden party is revived because a
                # new project is real activity, an untyped party becomes the
                # client it functionally is, and a missing category is filled
                # exactly like the project-first flow does.
                if phone and not (party.phone or '').strip():
                    party.phone = phone.strip()[:40]
                if not party.is_active:
                    party.is_active = True
                if (party.party_type or 'other').strip().lower() == 'other':
                    party.party_type = 'client'
                ensure_party_has_category(party)
            link_project_to_party(project, party)
        return True, party, created
    except Exception:
        current_app.logger.exception('Could not link client %r to Parties for project %s',
                                     client_name, project.project_code)
        return False, None, False


def register(app):
    """Register Projects, stages, drawings, stage library, owner payments."""
    def drawing_return_url(drawing):
        parent = drawing.stage
        if parent and record_allowed(current_user, 'hdc_project', parent.project_id):
            destination = url_for('hdc_project_detail', pid=parent.project_id)
            if may_access_path(current_user, destination) is not False:
                return destination
        return permission_landing_url(current_user)

    def _stage_form_subcontractor_pool():
        return Subcontractor.query.order_by(Subcontractor.name.asc(), Subcontractor.id.asc()).all()

    def _stage_form_assign_subs(stage):
        """Assign subcontractors picked on the Add/Edit Stage form.

        Returns False when the submitted terms are invalid; in that case no
        subcontractor is touched, so the stage edit itself can still be saved.
        """
        if not selected_subcontractor_ids(request.form):
            return True
        if _money_only():
            flash('Your role cannot assign subcontractors; stage saved without them.', 'warning')
            return True
        assigned, skipped, error = assign_subcontractors_from_form(stage, request.form, prefix='sub_')
        if error:
            flash(error, 'danger')
            return False
        for reason in skipped:
            flash(reason, 'warning')
        if assigned:
            flash('Subcontractors assigned: ' + ', '.join(sub.name for sub in assigned) + '.', 'success')
        return True

    # --- Projects --------------------------------------------------------------
    @app.route('/hdc/projects')
    @login_required
    def hdc_projects():
        page = max(1, request.args.get('page', type=int) or 1)
        per_page = 25
        q = Project.query.order_by(Project.created_at.desc(), Project.id.desc())
        pg_total_items = q.count()
        pg_total_pages = max(1, (pg_total_items + per_page - 1) // per_page)
        page = min(page, pg_total_pages)
        projects = q.offset((page - 1) * per_page).limit(per_page).all()
        if permission_for(current_user, 'project_financials') is not False:
            _apply_aggregated_project_costs(projects)
        # Client → Party ledger links for this page: direct link first, then
        # the case-insensitive name match legacy projects use.
        client_party_links = {}
        try:
            linked_ids = {int(p.client_party_id) for p in projects
                          if p.client_party_id}
            if linked_ids:
                for p in projects:
                    if p.client_party_id:
                        client_party_links[int(p.id)] = int(p.client_party_id)
            name_keys = {}
            for p in projects:
                if p.client_party_id:
                    continue
                key = party_name_key(p.client or '')
                if key:
                    name_keys.setdefault(key, []).append(int(p.id))
            if name_keys:
                rows = (CashFlowParty.query
                        .filter(func.lower(func.trim(CashFlowParty.name)).in_(list(name_keys)))
                        .order_by(CashFlowParty.is_active.desc(), CashFlowParty.id.asc())
                        .all())
                by_key = {}
                for row in rows:
                    key = party_name_key(row.name)
                    if key and key not in by_key:
                        by_key[key] = int(row.id)
                for key, ids in name_keys.items():
                    if key in by_key:
                        for project_id in ids:
                            client_party_links[project_id] = by_key[key]
        except Exception:
            client_party_links = {}
        return render_template('projects/projects.html',
                               projects=projects,
                               client_party_links=client_party_links,
                               pg_page=page,
                               pg_total_pages=pg_total_pages,
                               pg_total_items=pg_total_items,
                               pg_per_page=per_page,
                               pg_endpoint='hdc_projects',
                               pg_url_kwargs={},
                               pg_query={},
                               pg_label='projects')


    @app.route('/hdc/projects/add', methods=['GET', 'POST'])
    @login_required
    def hdc_add_project():
        if request.method == 'POST':
            code = (request.form.get('project_code','') or '').strip().upper()
            if not code or Project.query.filter_by(project_code=code).first():
                code = _next_project_code()
            # The party the operator started from (Parties → New Project).
            # A blank client name is filled from it, so the form can be opened
            # with the party already chosen.
            posted_party_id = request.form.get('party_id', type=int)
            party_start = (db.session.get(CashFlowParty, int(posted_party_id))
                           if posted_party_id else None)
            client_name = (request.form.get('client','') or '').strip()
            if not client_name and party_start is not None:
                client_name = (party_start.name or '').strip()
            client_phone = (request.form.get('client_phone','') or '').strip()
            if not client_phone and party_start is not None:
                client_phone = (party_start.phone or '').strip()
            name = (request.form.get('name','') or '').strip()
            if not name:
                name = " ".join(x for x in [
                    client_name,
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
                client=client_name,
                client_phone=client_phone,
                location=request.form.get('location','').strip(),
                total_constructed_sqft=total_sqft,
                owner_rate_per_sqft=owner_rate,
                owner_lump_sum=owner_lump,
                contract_type=inferred_contract_type,
                start_date=_parse_date(request.form.get('start_date')),
                status=request.form.get('status','active'),
                planned_start=_parse_date(request.form.get('start_date')),
                planned_end=_parse_date(request.form.get('planned_end')) if request.form.get('planned_end') else None)
            db.session.add(p)
            db.session.flush()
            # ── Integrate with Parties: link the client's exact party row ────
            linked, client_party, party_created = (
                _link_client_party(p, client_name, party_id=posted_party_id, phone=client_phone)
                if (client_name or posted_party_id) else (True, None, False))
            db.session.commit()
            if client_name and linked and client_party is not None:
                how = ('created in Parties' if party_created
                       else f'linked to the existing Party ledger ({client_party.name})')
                flash(f'Project "{p.name}" added. Client "{client_name}" {how}.', 'success')
            elif client_name and linked:
                flash(f'Project "{p.name}" added. Client "{client_name}" linked to Parties.', 'success')
            elif client_name:
                flash(f'Project "{p.name}" added, but client "{client_name}" could not be linked to '
                      f'Parties. Check the server log and re-save the project.', 'warning')
            else:
                flash(f'Project "{p.name}" added.', 'success')
            return redirect(url_for('hdc_project_detail', pid=p.id))
        client_options = known_client_party_names()
        initial_client = (request.args.get('client') or '').strip()
        # Party-first creation: /hdc/projects/add?party_id=<id> (the Parties
        # directory and ledger both link here) or ?client=<name>.
        initial_party = None
        party_id = request.args.get('party_id', type=int)
        if party_id:
            initial_party = db.session.get(CashFlowParty, int(party_id))
        if initial_party is None and initial_client:
            initial_party = party_by_name(initial_client)
        initial_phone = ''
        if initial_party is not None:
            initial_client = (initial_party.name or '').strip()
            initial_phone = (initial_party.phone or '').strip() or ''
        return render_template('projects/add_project.html', today=_pkt_today().isoformat(),
                               client_options=client_options,
                               initial_client=initial_client,
                               initial_party=initial_party,
                               initial_party_id=(initial_party.id if initial_party is not None else ''),
                               initial_phone=initial_phone)


    @app.route('/hdc/projects/<int:pid>')
    @login_required
    def hdc_project_detail(pid):
        p        = Project.query.get_or_404(pid)
        workers  = Worker.query.filter_by(active_status=True).all() if permission_for(current_user, 'timekeeping') is not False else []
        stages   = Stage.query.filter_by(project_id=pid).order_by(Stage.id).all() if permission_for(current_user, 'stages') is not False else []
        _apply_aggregated_stage_costs(stages)
        if permission_for(current_user, 'project_financials') is not False:
            _apply_aggregated_project_costs([p])
        stage_defs = (StageDefinition.query
                      .filter_by(project_id=pid, active_status=True)
                      .order_by(StageDefinition.default_order, StageDefinition.id)
                      .all())
        materials  = Material.query.filter_by(is_active=True).all()
        subcontractor_pool = (Subcontractor.query.order_by(Subcontractor.name.asc(), Subcontractor.id.asc()).all()
                              if permission_for(current_user, 'subcontractors') is not False else [])
        recent_time_entries = (TimeEntry.query
                               .filter(TimeEntry.project_id == pid, TimeEntry.is_void == False)
                               .order_by(TimeEntry.check_in.desc(), TimeEntry.id.desc())
                               .limit(10)
                               .all()) if (permission_for(current_user, 'timekeeping') is not False or permission_for(current_user, 'timekeeping_records') is not False) else []
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
                          .all()) if permission_for(current_user, 'project_receipts') is not False else []
        owner_payments_voided = (OwnerPayment.query
                                 .filter_by(project_id=pid, is_void=True)
                                 .order_by(OwnerPayment.voided_at.desc(), OwnerPayment.id.desc())
                                 .all()) if permission_for(current_user, 'project_receipts') is not False else []
        receiving_accounts = (Account.query
                              .filter(
                                  Account.is_void == False,
                                  func.lower(func.coalesce(Account.status, 'active')) == 'active',
                                  func.lower(func.coalesce(Account.type, '')).in_(_ACCOUNT_COMPANY_TYPES)
                              )
                              .order_by(Account.name.asc(), Account.id.asc())
                              .all()) if permission_for(current_user, 'project_receipts', 'write') is not False else []
        project_receivable_rows = _running_projects_receivable_rows() if permission_for(current_user, 'project_receipts') is not False else []
        # ── Resolve linked party for this project's client ─────────────────
        # Direct link first (party-first creation / the sync backfill), then
        # the case-insensitive name match used by legacy projects.
        client_party = None
        client_party_id = None
        client_statement = None
        try:
            from hdc.services.parties import party_ledger as _party_ledger
            client_party = party_for_project(p)
            if client_party:
                client_party_id = int(client_party.id)
                try:
                    client_statement = _party_ledger(client_party, include_void=False)
                except Exception:
                    client_statement = None
        except Exception:
            client_party = None
            client_statement = None

        # Site ⇄ supplier tracking, read the other way round: the Purchase
        # section knows which suppliers this project drew material from, and
        # which are only assigned to it so far.
        site_supplier_rows = _site_supplier_supplied_rows(project_id=p.id)
        site_assigned_suppliers = []
        try:
            for link in SupplierSite.query.filter(SupplierSite.project_id == p.id).all():
                supplier = link.supplier
                if not supplier or supplier.is_void:
                    continue
                site_assigned_suppliers.append({
                    'id': supplier.id,
                    'name': supplier.name,
                    'phone': supplier.phone or '',
                    'note': link.note or '',
                    'supplied': any(int(r['supplier_id']) == int(supplier.id) for r in site_supplier_rows),
                })
        except Exception:
            site_assigned_suppliers = []
        site_assigned_suppliers.sort(key=lambda r: r['name'].lower())
        return render_template('projects/project_detail.html',
            p=p, workers=workers, stages=stages,
            site_supplier_rows=site_supplier_rows,
            site_assigned_suppliers=site_assigned_suppliers,
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
            today=_pkt_today().isoformat(),
            client_party=client_party,
            client_party_id=client_party_id,
            client_statement=client_statement)


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
            financial_fields = {'total_sqft', 'owner_rate', 'lump_sum', 'contract_type'}
            if financial_fields.intersection(request.form) and permission_for(current_user, 'project_financials', 'write') is False:
                abort(403)
            old_client = (p.client or '').strip()
            # The client decides whose ledger the project's money lands in, so
            # changing it needs the same write right as the contract figures.
            if ('client' in request.form
                    and request.form.get('client', '').strip() != old_client
                    and permission_for(current_user, 'project_financials', 'write') is False):
                abort(403)
            for field in ('name', 'client', 'client_phone', 'location', 'status'):
                if field in request.form:
                    setattr(p, field, request.form.get(field, '').strip())
            if permission_for(current_user, 'project_financials', 'write') is not False:
                for field, column in (('total_sqft', 'total_constructed_sqft'),
                                      ('owner_rate', 'owner_rate_per_sqft'), ('lump_sum', 'owner_lump_sum')):
                    if field in request.form:
                        setattr(p, column, _flt(request.form.get(field)))
                if 'contract_type' in request.form:
                    p.contract_type = request.form['contract_type']
            if 'planned_end' in request.form:
                p.planned_end = _parse_date(request.form.get('planned_end')) if request.form.get('planned_end') else None
            # ── Keep Parties in sync when the client changes ─────────────
            new_client = (p.client or '').strip()
            linked = True
            refiled = 0
            name_changed = new_client.lower() != old_client.lower()
            if new_client:
                # Re-resolve when the name changed, or when a legacy project
                # has no direct party link yet — the first edit backfills it.
                if name_changed or p.client_party_id is None:
                    linked, _party, _created = _link_client_party(
                        p, new_client,
                        # A changed name must resolve to the new owner, never
                        # to the party the project previously pointed at.
                        party_id=(None if name_changed else p.client_party_id),
                        phone=(p.client_phone or '').strip() or None)
                if linked and name_changed and old_client:
                    from hdc.services.parties import rename_owner_client
                    refiled = rename_owner_client(p, old_client, new_client)
            else:
                p.client_party_id = None
            db.session.commit()
            if linked:
                note = f' {refiled} ledger row(s) moved to the new client name.' if refiled else ''
                flash(f'Project updated.{note}', 'success')
            else:
                flash(f'Project updated, but client "{new_client}" could not be linked to Parties. '
                      f'Check the server log and re-save the project.', 'warning')
            return redirect(url_for('hdc_project_detail', pid=pid))
        client_options = known_client_party_names()
        return render_template('projects/edit_project.html', p=p, client_options=client_options,
                               client_party=party_for_project(p))


    @app.route('/hdc/projects/<int:pid>/owner_payment', methods=['POST'])
    @login_required
    @_money_write_required()
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
    @_money_write_required()
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
    @_money_write_required()
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


    # --- Stages ----------------------------------------------------------------
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
            db.session.add(s); db.session.flush()
            subs_ok = _stage_form_assign_subs(s)
            db.session.commit()
            flash(f'Stage "{s.name}" added.', 'success')
            if not subs_ok:
                flash('Subcontractors were not assigned because of the error above — add them from the stage\'s Subcontractors panel.', 'warning')
                return redirect(url_for('hdc_project_detail', pid=pid) + f'#stage-subs-{s.id}')
            return redirect(url_for('hdc_project_detail', pid=pid))
        # For add form, no allocation yet
        return render_template('projects/stage_form.html', p=p, stage=None, defs=defs, mode='add',
                               subcontractor_pool=_stage_form_subcontractor_pool(), form={},
                               sqft_summary={'total': 0, 'allocated': 0, 'remaining': None, 'has_cap': False})


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
            # Validate sqft reduction against already allocated
            if new_qty and new_qty > 0:
                try:
                    allocated = float(s.subcontracted_sqft or 0.0)
                except Exception:
                    allocated = 0.0
                if allocated > 0 and new_qty < allocated - 1e-6:
                    flash(f'Cannot reduce stage sqft to {new_qty:,.0f} because {allocated:,.0f} sqft is already allocated to subcontractors. Remove or reduce subcontractor sqft first.', 'danger')
                    return redirect(url_for('hdc_edit_stage', sid=s.id))
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
            subs_ok = _stage_form_assign_subs(s)  # invalid terms change nothing
            db.session.commit()
            flash('Stage updated.', 'success')
            if not subs_ok:
                flash('New subcontractors were not assigned because of the error above.', 'warning')
                return redirect(url_for('hdc_edit_stage', sid=s.id))
            return redirect(url_for('hdc_project_detail', pid=p.id))
        # Provide sqft summary for edit form
        try:
            sqft_summary = s.sqft_allocation_summary
        except Exception:
            sqft_summary = {'total': float(s.qty_sqft or 0), 'allocated': 0, 'remaining': None, 'has_cap': False}
        return render_template('projects/stage_form.html', p=p, stage=s, defs=[], mode='edit',
                               subcontractor_pool=_stage_form_subcontractor_pool(), form={},
                               sqft_summary=sqft_summary)


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
    @_money_write_required()
    def hdc_stage_status(sid):
        s = Stage.query.get_or_404(sid)
        old_status = (s.status or '').strip().lower()
        s.status = request.form.get('status', s.status)
        new_status = (s.status or '').strip().lower()
        auto_note = ''
        auto_sub = (request.form.get('auto_sub_complete') or '').strip() == '1'
        if new_status in ('completed', 'complete') and s.assigned_subcontractors and not auto_sub:
            sub_chk = next((sub for sub in s.assigned_subcontractors
                            if float(sub.effective_progress_percentage or 0) < 100), None)
            if sub_chk and float(sub_chk.effective_progress_percentage or 0.0) < 100.0:
                db.session.rollback()
                flash(f'Cannot complete stage "{s.name}" because subcontractor "{sub_chk.name}" is at {float(sub_chk.effective_progress_percentage or 0.0):.2f}%. Update Sub Completion % to 100 or use force complete action.', 'danger')
                return redirect(url_for('hdc_project_detail', pid=s.project_id))
        if auto_sub and new_status in ('completed', 'complete') and s.assigned_subcontractors:
            for sub in s.assigned_subcontractors:
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
        for member in s.assigned_subcontractors if old_status != new_status else []:
            _log_subcontract_event(
                sub=member,
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
    @_money_write_required()
    def hdc_stage_sub_progress(sid):
        s = Stage.query.get_or_404(sid)
        members = s.assigned_subcontractors
        sub_id = request.form.get('subcontractor_id', type=int)
        sub = next((member for member in members if member.id == sub_id), None)
        if not sub_id and len(members) == 1:
            sub = members[0]  # Legacy single-subcontractor forms remain valid.
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
            path = os.path.join(get_runtime_settings().stage_drawings_dir, stored)
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
        path = os.path.join(get_runtime_settings().stage_drawings_dir, d.stored_name or '')
        if not os.path.exists(path):
            flash('Drawing file not found on disk.', 'danger')
            return redirect(drawing_return_url(d))
        return send_file(path, mimetype='application/pdf', as_attachment=False, download_name=d.original_name)


    @app.route('/hdc/stage/drawing/<int:did>/delete', methods=['POST'])
    @login_required
    def hdc_stage_drawing_delete(did):
        d = StageDrawing.query.get_or_404(did)
        destination = drawing_return_url(d)
        path = os.path.join(get_runtime_settings().stage_drawings_dir, d.stored_name or '')
        db.session.delete(d)
        db.session.commit()
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception as ex:
            current_app.logger.warning('Could not remove stage drawing file: %s', ex)
        flash('Drawing deleted.', 'success')
        return redirect(destination)


    @app.route('/hdc/stage/drawing/<int:did>/replace', methods=['POST'])
    @login_required
    def hdc_stage_drawing_replace(did):
        d = StageDrawing.query.get_or_404(did)
        destination = drawing_return_url(d)
        f = request.files.get('drawing_file')
        if not _is_pdf_upload(f):
            flash('Please upload a valid PDF file.', 'warning')
            return redirect(destination)
        old_path = os.path.join(get_runtime_settings().stage_drawings_dir, d.stored_name or '')
        new_original = secure_filename(f.filename or 'drawing.pdf') or 'drawing.pdf'
        new_stored = f"{uuid4().hex}.pdf"
        new_path = os.path.join(get_runtime_settings().stage_drawings_dir, new_stored)
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
        return redirect(destination)


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
        page = max(1, request.args.get('page', type=int) or 1)
        per_page = 25
        q = Stage.query
        if project_id:
            q = q.filter(Stage.project_id == project_id)
        pg_total_items = q.count()
        pg_total_pages = max(1, (pg_total_items + per_page - 1) // per_page)
        page = min(page, pg_total_pages)
        stages = q.order_by(Stage.id.desc()).offset((page - 1) * per_page).limit(per_page).all()
        _apply_aggregated_stage_costs(stages)
        return render_template('projects/stage_overview.html',
            stages=stages,
            projects=projects,
            selected_project=project_id,
            pg_page=page,
            pg_total_pages=pg_total_pages,
            pg_total_items=pg_total_items,
            pg_per_page=per_page,
            pg_endpoint='hdc_stage_overview',
            pg_url_kwargs={},
            pg_query=({'project_id': project_id} if project_id else {}),
            pg_label='stages'
        )


    @app.route('/hdc/api/project_stages/<int:pid>')
    @login_required
    def hdc_api_project_stages(pid):
        stages = Stage.query.filter_by(project_id=pid).order_by(Stage.id).all()
        return jsonify([{'id': s.id, 'name': s.name} for s in stages])
