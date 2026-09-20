"""HDC routes: Money Center — unified smooth handling of all money from Accounts.

This is the new primary entry point for handling all money types (in/out/transfer)
from the Accounts section. It consolidates every money flow documented in
hdc/services/money_hub.py into one smooth UX.

Routes:
  /hdc/accounts/money-center     Money Center page — all flows, pendings, quick entry
  /hdc/accounts/money-center/api  JSON API for money center data

The existing routes (/hdc/accounts, /hdc/accounts/manage, /hdc/cashflow/register, etc.)
remain untouched — Money Center is an additional smooth hub that links to them.
"""

import json
from datetime import timedelta

from flask import flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from hdc.extensions import _admin_only, db
from hdc.models.accounts import Account, AccountTransaction
from hdc.models.cashflow import CashDayLock
from hdc.models.projects import Project, Stage
from hdc.services.accounts import (
    _account_dashboard_kpis,
    _account_intent_field_matrix,
    _create_accounts_transaction_with_sync,
)
from hdc.services.accounts_manage import list_manage_accounts, manage_summary
from hdc.services.cashflow_register import day_lock_state, day_positions, day_totals, register_rows, register_summary
from hdc.services.money_hub import (
    get_all_money_flows,
    get_money_accounts,
    get_money_flow_diagram,
    get_money_flows_grouped,
    get_money_kpis,
    get_pending_payables_detailed,
    get_smooth_entry_config,
)
from hdc.utils.dates import _pkt_today
from hdc.utils.money import from_minor


def _money_center_context():
    today = _pkt_today()
    grouped = get_money_flows_grouped()
    pending = get_pending_payables_detailed()
    accounts_info = get_money_accounts()
    kpis = get_money_kpis()
    entry_config = get_smooth_entry_config()
    intent_matrix = _account_intent_field_matrix()
    diagram = get_money_flow_diagram()

    # Active accounts for dropdowns
    try:
        active_accounts = list_manage_accounts(show='active', include_auto_person=True)
    except Exception:
        active_accounts = []

    # Company accounts for quick transfer
    company_accounts = accounts_info.get("company_accounts", [])

    # Projects and stages for selectors
    try:
        projects = Project.query.filter(Project.is_void == False).order_by(Project.name.asc()).all()
    except Exception:
        projects = []
    try:
        stages = Stage.query.filter(Stage.is_void == False).order_by(Stage.name.asc()).all()
    except Exception:
        stages = []

    # Today's register summary
    today_summary = {'total_in': 0.0, 'total_out': 0.0, 'total_transfer': 0.0, 'net': 0.0, 'count': 0}
    try:
        rows = register_rows(date_from=today, date_to=today, limit=None)
        today_summary = register_summary(rows)
    except Exception:
        pass

    # Day lock state
    lock_today = None
    try:
        lock_today = day_lock_state(today)
    except Exception:
        pass

    # Recent transactions (last 20)
    recent_txns = []
    try:
        recent_txns = (AccountTransaction.query
                       .filter(AccountTransaction.is_void == False)
                       .order_by(AccountTransaction.id.desc())
                       .limit(20).all())
    except Exception:
        pass

    # Summary for hub
    try:
        summary = manage_summary(active_accounts)
    except Exception:
        summary = {'total_accounts': 0, 'active_count': 0, 'negative_count': 0, 'negative_accounts': []}

    return {
        'today': today.isoformat(),
        'grouped_flows': grouped,
        'all_flows': get_all_money_flows(),
        'pending': pending,
        'accounts_info': accounts_info,
        'company_accounts': company_accounts,
        'all_accounts': active_accounts,
        'kpis': kpis,
        'entry_config': entry_config,
        'entry_config_json': json.dumps(entry_config),
        'intent_matrix': intent_matrix,
        'intent_matrix_json': json.dumps(intent_matrix),
        'diagram': diagram,
        'projects': projects,
        'stages': stages,
        'today_summary': today_summary,
        'lock_today': lock_today,
        'recent_txns': recent_txns,
        'summary': summary,
        'flows_count': len(get_all_money_flows()),
        'in_count': len(grouped.get('in', [])),
        'out_count': len(grouped.get('out', [])),
        'transfer_count': len(grouped.get('transfer', [])),
    }


def register(app):
    @app.route('/hdc/accounts/money-center', methods=['GET', 'POST'])
    @login_required
    def hdc_money_center():
        if _admin_only():
            return redirect(url_for('hdc_dashboard'))

        if request.method == 'POST':
            action = (request.form.get('action') or '').strip().lower()
            if action == 'quick_transaction':
                tool_rental_id = request.form.get('tool_rental_id', type=int)
                if tool_rental_id:
                    try:
                        from hdc.models.tool_rental import ToolRental
                        from hdc.services.tool_rental import post_tool_rental_payment_to_accounts
                        rental = ToolRental.query.get(tool_rental_id)
                        if not rental:
                            flash('Tool rental not found.', 'danger')
                            return redirect(url_for('hdc_money_center'))
                        amount = float(request.form.get('amount') or 0.0)
                        receiving_account_id = request.form.get('to_account_id', type=int) or request.form.get('from_account_id', type=int)
                        if not receiving_account_id:
                            flash('Receiving company account required for tool rental income.', 'danger')
                            return redirect(url_for('hdc_money_center'))
                        from hdc.models.tool_rental import ToolRentalPayment
                        from hdc.utils.dates import _pkt_now_naive
                        from hdc.utils.format import _parse_date
                        rental_id = rental.id
                        note_text = request.form.get('note') or 'Tool rental payment via Money Center for rental #{}'.format(rental_id)
                        pay_row = ToolRentalPayment(
                            rental_id=rental.id,
                            amount=amount,
                            date=_parse_date(request.form.get('date')) or _pkt_today(),
                            receiving_account_id=receiving_account_id,
                            notes=note_text,
                            is_void=False,
                            created_at=_pkt_now_naive()
                        )
                        db.session.add(pay_row)
                        db.session.flush()
                        ok, msg = post_tool_rental_payment_to_accounts(pay_row, commit=True)
                        if ok:
                            flash('Tool rental payment recorded: Rs. {:.2f} for rental #{} posted to unified ledger.'.format(amount, rental_id), 'success')
                        else:
                            flash('Failed: {}'.format(msg), 'danger')
                        return redirect(url_for('hdc_money_center'))
                    except Exception as ex:
                        db.session.rollback()
                        flash('Tool rental payment failed: {}'.format(ex), 'danger')
                        return redirect(url_for('hdc_money_center'))

                payload = {
                    'date': (request.form.get('date') or _pkt_today().isoformat()),
                    'type': (request.form.get('type') or request.form.get('transaction_type') or '').strip(),
                    'amount': (request.form.get('amount') or '0'),
                    'from_account_id': (request.form.get('from_account_id') or ''),
                    'to_account_id': (request.form.get('to_account_id') or ''),
                    'executed_by_account_id': (request.form.get('executed_by_account_id') or request.form.get('from_account_id') or ''),
                    'project_id': (request.form.get('project_id') or ''),
                    'stage_id': (request.form.get('stage_id') or ''),
                    'related_entity_type': (request.form.get('related_entity_type') or ''),
                    'related_entity_id': (request.form.get('related_entity_id') or ''),
                    'party_name': (request.form.get('party_name') or ''),
                    'note': (request.form.get('note') or ''),
                    'reference_id': (request.form.get('reference_id') or ''),
                    'expense_category_id': (request.form.get('expense_category_id') or ''),
                    'office_target': (request.form.get('office_target') or ''),
                    'office_expense_category': (request.form.get('office_expense_category') or ''),
                    'excess_tip_amount': (request.form.get('excess_tip_amount') or '0'),
                    'excess_advance_amount': (request.form.get('excess_advance_amount') or '0'),
                    'settle_shortfall': (request.form.get('settle_shortfall') or ''),
                }
                ok, msg, rows = _create_accounts_transaction_with_sync(payload)
                if ok:
                    tx_type = payload.get("type", "")
                    amt = payload.get("amount", "")
                    flash('Transaction recorded: {} row(s) posted to unified ledger. Direction: {} | Amount: Rs. {} | Single source of truth maintained.'.format(len(rows), tx_type, amt), 'success')
                else:
                    flash('Failed: {}'.format(msg), 'danger')
                return redirect(url_for('hdc_money_center'))

        return render_template('accounts/money_center.html', **_money_center_context())

    @app.route('/hdc/accounts/money-center/api/flows', methods=['GET'])
    @login_required
    def hdc_money_center_api_flows():
        if _admin_only():
            return jsonify(ok=False, message='Admin access required.'), 403
        direction = (request.args.get('direction') or '').strip().lower()
        grouped = get_money_flows_grouped()
        if direction in ('in', 'out', 'transfer'):
            return jsonify(ok=True, direction=direction, flows=grouped.get(direction, []))
        return jsonify(ok=True, grouped=grouped, all=get_all_money_flows())

    @app.route('/hdc/accounts/money-center/api/pending', methods=['GET'])
    @login_required
    def hdc_money_center_api_pending():
        if _admin_only():
            return jsonify(ok=False, message='Admin access required.'), 403
        pending = get_pending_payables_detailed()
        return jsonify(ok=True, **pending)

    @app.route('/hdc/accounts/money-center/api/kpis', methods=['GET'])
    @login_required
    def hdc_money_center_api_kpis():
        if _admin_only():
            return jsonify(ok=False, message='Admin access required.'), 403
        kpis = get_money_kpis()
        return jsonify(ok=True, kpis=kpis)

    @app.route('/hdc/accounts/money-center/api/accounts', methods=['GET'])
    @login_required
    def hdc_money_center_api_accounts():
        if _admin_only():
            return jsonify(ok=False, message='Admin access required.'), 403
        info = get_money_accounts()
        return jsonify(ok=True, **info)

    @app.route('/hdc/accounts/money-center/api/entry-config', methods=['GET'])
    @login_required
    def hdc_money_center_api_entry_config():
        if _admin_only():
            return jsonify(ok=False, message='Admin access required.'), 403
        return jsonify(ok=True, config=get_smooth_entry_config(), intent_matrix=_account_intent_field_matrix())

    @app.route('/hdc/accounts/money-center/api/quick-post', methods=['POST'])
    @login_required
    def hdc_money_center_api_quick_post():
        if _admin_only():
            return jsonify(ok=False, message='Admin access required.'), 403
        payload = request.get_json(silent=True) or request.form.to_dict()
        ok, msg, rows = _create_accounts_transaction_with_sync(payload)
        if not ok:
            return jsonify(ok=False, message=msg), 400
        return jsonify(ok=True, created_count=len(rows), message='Posted to unified ledger', rows=[
            {
                'id': r.id,
                'date': r.date.isoformat() if r.date else '',
                'amount': float(r.amount or 0.0),
                'type': r.type,
                'from_account_id': r.from_account_id,
                'to_account_id': r.to_account_id,
                'group_id': r.group_id,
            } for r in rows
        ])

    @app.route('/hdc/accounts/money-center/api/diagram', methods=['GET'])
    @login_required
    def hdc_money_center_api_diagram():
        if _admin_only():
            return jsonify(ok=False, message='Admin access required.'), 403
        return jsonify(ok=True, diagram=get_money_flow_diagram())

    # Entity options APIs for smooth Money Center selectors
    @app.route('/hdc/api/workers', methods=['GET'])
    @login_required
    def hdc_api_workers_options():
        if _admin_only():
            return jsonify(ok=False, message='Admin access required.'), 403
        try:
            from hdc.models.workforce import Worker
            workers = Worker.query.filter(Worker.active_status == True).order_by(Worker.name.asc()).limit(500).all()
            items = []
            for w in workers:
                code = w.worker_code or 'W-{}'.format(w.id)
                label = '{} ({})'.format(w.name, code)
                items.append({'id': w.id, 'name': w.name, 'label': label, 'code': code})
            return jsonify(ok=True, items=items)
        except Exception as ex:
            return jsonify(ok=False, message=str(ex), items=[])

    @app.route('/hdc/api/suppliers', methods=['GET'])
    @login_required
    def hdc_api_suppliers_options():
        if _admin_only():
            return jsonify(ok=False, message='Admin access required.'), 403
        try:
            from hdc.models.materials import Supplier
            suppliers = Supplier.query.filter(Supplier.is_void == False).order_by(Supplier.name.asc()).limit(500).all()
            items = []
            for s in suppliers:
                code = s.phone or 'SUP-{}'.format(s.id)
                label = '{} ({})'.format(s.name, code)
                items.append({'id': s.id, 'name': s.name, 'label': label, 'code': code})
            return jsonify(ok=True, items=items)
        except Exception as ex:
            return jsonify(ok=False, message=str(ex), items=[])

    @app.route('/hdc/api/subcontractors', methods=['GET'])
    @login_required
    def hdc_api_subcontractors_options():
        if _admin_only():
            return jsonify(ok=False, message='Admin access required.'), 403
        try:
            from hdc.models.subcontract import Subcontractor
            subs = Subcontractor.query.order_by(Subcontractor.name.asc()).limit(500).all()
            items = []
            for s in subs:
                code = s.subcontractor_code or 'SUB-{}'.format(s.id)
                label = '{} ({})'.format(s.name, code)
                items.append({'id': s.id, 'name': s.name, 'label': label, 'code': code, 'project_id': s.project_id, 'stage_id': s.stage_id})
            return jsonify(ok=True, items=items)
        except Exception as ex:
            return jsonify(ok=False, message=str(ex), items=[])

    @app.route('/hdc/api/office_staff', methods=['GET'])
    @login_required
    def hdc_api_office_staff_options():
        if _admin_only():
            return jsonify(ok=False, message='Admin access required.'), 403
        try:
            from hdc.models.office import OfficeStaff
            staff = OfficeStaff.query.filter(OfficeStaff.is_void == False).order_by(OfficeStaff.name.asc()).limit(500).all()
            items = []
            for s in staff:
                code = s.staff_code or 'OFF-{}'.format(s.id)
                label = '{} ({})'.format(s.name, code)
                items.append({'id': s.id, 'name': s.name, 'label': label, 'code': code})
            return jsonify(ok=True, items=items)
        except Exception as ex:
            return jsonify(ok=False, message=str(ex), items=[])

    @app.route('/hdc/api/expense_categories', methods=['GET'])
    @login_required
    def hdc_api_expense_categories_options():
        if _admin_only():
            return jsonify(ok=False, message='Admin access required.'), 403
        try:
            from hdc.models.expenses import ExpenseCategory
            cats = ExpenseCategory.query.filter(ExpenseCategory.active_status == True).order_by(ExpenseCategory.name.asc()).all()
            items = [{'id': c.id, 'name': c.name, 'label': c.name} for c in cats]
            return jsonify(ok=True, items=items, categories=[c.name for c in cats])
        except Exception as ex:
            return jsonify(ok=False, message=str(ex), items=[])

    @app.route('/hdc/api/project_stages/<int:project_id>', methods=['GET'])
    @login_required
    def hdc_api_project_stages_options(project_id):
        if _admin_only():
            return jsonify(ok=False, message='Admin access required.'), 403
        try:
            from hdc.models.projects import Stage
            stages = Stage.query.filter(Stage.project_id == project_id, Stage.is_void == False).order_by(Stage.name.asc()).all()
            items = [{'id': s.id, 'name': s.name, 'label': s.name} for s in stages]
            return jsonify(items)
        except Exception:
            return jsonify([])
