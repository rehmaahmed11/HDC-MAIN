"""HDC routes: JSON API: /api/accounts/*.

Moved verbatim from hdc_erp.py; each handler keeps its
original @app.route decorator and endpoint name.
"""

from flask import jsonify, request
from flask_login import login_required
from sqlalchemy import func, or_

from hdc.extensions import _admin_only, db
from hdc.models.accounts import Account, AccountTransaction
from hdc.services.accounts import _ACCOUNT_TYPES, _account_dashboard_kpis, _account_dashboard_subgroups, _account_group_mode_for_row, _account_pending_snapshot, _account_transaction_history, _account_txn_to_dict, _accounts_forensic_report, _accounts_reconciliation_snapshot, _create_account, _create_accounts_transaction_with_sync, _list_accounts_with_balances, _resolve_account_type
from hdc.utils.format import _flt, _parse_date
from hdc.utils.normalize import _normalize_account_group, _normalize_account_mode, _normalize_account_tx_direction, _normalize_name_ci

def register(app):
    """Register JSON API: /api/accounts/*."""
    # â”€â”€ Bootstrap â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    @app.route('/api/accounts/create_account', methods=['POST'])
    @login_required
    def api_accounts_create_account():
        if _admin_only():
            return jsonify(ok=False, message='Admin access required.'), 403
        payload = request.get_json(silent=True) or request.form
        acc_group = _normalize_account_group(payload.get('account_group'))
        acc_mode = _normalize_account_mode(payload.get('account_mode'))
        acc_type = _resolve_account_type(group=acc_group, mode=acc_mode, explicit_type=payload.get('type'))
        row, msg = _create_account(
            payload.get('name'),
            acc_type,
            opening_balance=_flt(payload.get('opening_balance'), 0.0),
            bank_name=payload.get('bank_name'),
            account_number=payload.get('account_number'),
            iban=payload.get('iban'),
            account_mode=acc_mode,
        )
        if not row:
            return jsonify(ok=False, message=(msg or 'Unable to create account.')), 400
        db.session.commit()
        acc_group, acc_mode = _account_group_mode_for_row(row)
        return jsonify(ok=True, account={
            'id': int(row.id),
            'name': row.name,
            'type': row.type,
            'account_group': acc_group,
            'account_mode': acc_mode,
            'opening_balance': float(row.opening_balance or 0.0),
            'bank_name': (row.bank_name or ''),
            'account_number': (row.account_number or ''),
            'iban': (row.iban or ''),
            'auto_generated': bool(row.auto_generated),
            'auto_source': (row.auto_source or ''),
            'created_at': (row.created_at.isoformat(sep=' ') if row.created_at else ''),
        })


    @app.route('/api/accounts/list_accounts_with_balances', methods=['GET'])
    @login_required
    def api_accounts_list_accounts_with_balances():
        if _admin_only():
            return jsonify(ok=False, message='Admin access required.'), 403
        include_inactive = (str(request.args.get('include_inactive') or '').strip().lower() in ('1', 'true', 'yes', 'on'))
        return jsonify(ok=True, items=_list_accounts_with_balances(include_inactive=include_inactive))


    @app.route('/api/accounts/account/<int:account_id>', methods=['PUT', 'DELETE'])
    @login_required
    def api_accounts_account_item(account_id):
        if _admin_only():
            return jsonify(ok=False, message='Admin access required.'), 403
        row = Account.query.get_or_404(account_id)
        if request.method == 'DELETE':
            has_txn = db.session.query(AccountTransaction.id).filter(
                AccountTransaction.is_void == False,
                or_(AccountTransaction.from_account_id == row.id, AccountTransaction.to_account_id == row.id)
            ).first() is not None
            if has_txn:
                row.status = 'inactive'
                row.is_void = True
                db.session.commit()
                return jsonify(ok=True, deleted=False, message='Account has transactions and was archived instead.')
            db.session.delete(row)
            db.session.commit()
            return jsonify(ok=True, deleted=True)

        payload = request.get_json(silent=True) or request.form
        nm = _normalize_name_ci(payload.get('name') or row.name)
        current_group, current_mode = _account_group_mode_for_row(row)
        acc_group = _normalize_account_group(payload.get('account_group') or current_group)
        acc_mode = _normalize_account_mode(payload.get('account_mode') or current_mode)
        tp = _resolve_account_type(group=acc_group, mode=acc_mode, explicit_type=payload.get('type') or row.type)
        opening = _flt(payload.get('opening_balance'), row.opening_balance)
        bank_name = _normalize_name_ci(payload.get('bank_name') or row.bank_name or '')
        account_number = (payload.get('account_number') or row.account_number or '').strip()
        iban = (payload.get('iban') or row.iban or '').strip()
        if not nm:
            return jsonify(ok=False, message='Account name is required.'), 400
        if tp not in _ACCOUNT_TYPES:
            return jsonify(ok=False, message=f'Account type must be one of: {", ".join(_ACCOUNT_TYPES)}.'), 400
        if acc_mode == 'bank':
            if not bank_name or not account_number:
                return jsonify(ok=False, message='bank_name and account_number are required for bank accounts.'), 400
        else:
            bank_name = ''
            account_number = ''
            iban = ''
        dup = (Account.query
               .filter(
                   Account.id != row.id,
                   Account.is_void == False,
                   func.lower(func.trim(Account.name)) == nm.lower()
               ).first())
        if dup:
            return jsonify(ok=False, message='Another account with this name already exists.'), 400
        row.name = nm
        row.type = tp
        row.opening_balance = float(opening or 0.0)
        row.bank_name = bank_name or None
        row.account_number = account_number or None
        row.iban = iban or None
        row.status = (payload.get('status') or row.status or 'active').strip().lower()
        db.session.commit()
        acc_group, acc_mode = _account_group_mode_for_row(row)
        return jsonify(ok=True, account={
            'id': int(row.id),
            'name': row.name,
            'type': row.type,
            'account_group': acc_group,
            'account_mode': acc_mode,
            'opening_balance': float(row.opening_balance or 0.0),
            'bank_name': (row.bank_name or ''),
            'account_number': (row.account_number or ''),
            'iban': (row.iban or ''),
            'status': (row.status or 'active'),
        })


    @app.route('/api/accounts/create_transaction', methods=['POST'])
    @login_required
    def api_accounts_create_transaction():
        if _admin_only():
            return jsonify(ok=False, message='Admin access required.'), 403
        payload = request.get_json(silent=True) or request.form
        ok, msg, rows = _create_accounts_transaction_with_sync(payload)
        if not ok:
            return jsonify(ok=False, message=(msg or 'Unable to create transaction.')), 400
        return jsonify(ok=True, created_count=len(rows), items=[_account_txn_to_dict(r) for r in rows])


    @app.route('/api/accounts/transaction_history', methods=['GET'])
    @login_required
    def api_accounts_transaction_history():
        if _admin_only():
            return jsonify(ok=False, message='Admin access required.'), 403
        account_id = request.args.get('account_id', type=int)
        account_group = (request.args.get('account_group') or '').strip()
        account_group = (_normalize_account_group(account_group) if account_group else '')
        project_id = request.args.get('project_id', type=int)
        stage_id = request.args.get('stage_id', type=int)
        tx_type = (request.args.get('transaction_type') or request.args.get('type') or '').strip().lower()
        tx_direction = _normalize_account_tx_direction((request.args.get('direction') or '').strip().lower())
        category = (request.args.get('category') or '').strip().lower()
        group_id = (request.args.get('group_id') or '').strip()
        reference_id = (request.args.get('reference_id') or '').strip()
        limit = request.args.get('limit', type=int) or 500
        date_from = _parse_date((request.args.get('date_from') or '').strip(), fallback=None)
        date_to = _parse_date((request.args.get('date_to') or '').strip(), fallback=None)
        rows = _account_transaction_history(
            account_id=(account_id or None),
            account_group=(account_group or None),
            date_from=date_from,
            date_to=date_to,
            project_id=(project_id or None),
            stage_id=(stage_id or None),
            tx_type=(tx_type or None),
            tx_direction=(tx_direction or None),
            category=(category or None),
            group_id=(group_id or None),
            reference_id=(reference_id or None),
            limit=limit
        )
        return jsonify(ok=True, count=len(rows), items=[_account_txn_to_dict(r) for r in rows])


    @app.route('/api/accounts/dashboard_summary', methods=['GET'])
    @login_required
    def api_accounts_dashboard_summary():
        if _admin_only():
            return jsonify(ok=False, message='Admin access required.'), 403
        date_from = _parse_date((request.args.get('date_from') or '').strip(), fallback=None)
        date_to = _parse_date((request.args.get('date_to') or '').strip(), fallback=None)
        return jsonify(ok=True, items=_account_dashboard_kpis(date_from=date_from, date_to=date_to))


    @app.route('/api/accounts/dashboard_subgroups', methods=['GET'])
    @login_required
    def api_accounts_dashboard_subgroups():
        if _admin_only():
            return jsonify(ok=False, message='Admin access required.'), 403
        metric = (request.args.get('metric') or '').strip().lower()
        return jsonify(ok=True, items=_account_dashboard_subgroups(metric))


    @app.route('/api/accounts/pending_context', methods=['GET'])
    @login_required
    def api_accounts_pending_context():
        if _admin_only():
            return jsonify(ok=False, message='Admin access required.'), 403
        tx_type = (request.args.get('type') or request.args.get('transaction_type') or '').strip().lower()
        related_entity_type = (request.args.get('related_entity_type') or '').strip().lower()
        related_entity_id = request.args.get('related_entity_id', type=int) or 0
        project_id = request.args.get('project_id', type=int) or 0
        stage_id = request.args.get('stage_id', type=int) or 0
        snap = _account_pending_snapshot(
            tx_type,
            related_entity_type=related_entity_type,
            related_entity_id=related_entity_id,
            project_id=project_id,
            stage_id=stage_id
        )
        return jsonify(ok=True, item=snap)


    @app.route('/api/accounts/reconciliation_summary', methods=['GET'])
    @login_required
    def api_accounts_reconciliation_summary():
        if _admin_only():
            return jsonify(ok=False, message='Admin access required.'), 403
        limit = request.args.get('limit', type=int) or 50
        limit = max(1, min(int(limit), 200))
        items = _accounts_reconciliation_snapshot(limit=limit)
        total_issues = int(sum(int(i.get('count') or 0) for i in items))
        return jsonify(ok=True, total_issues=total_issues, items=items)


    @app.route('/api/accounts/forensic_report', methods=['GET'])
    @login_required
    def api_accounts_forensic_report():
        if _admin_only():
            return jsonify(ok=False, message='Admin access required.'), 403
        limit = request.args.get('limit', type=int) or 100
        limit = max(1, min(int(limit), 500))
        report = _accounts_forensic_report(limit=limit)
        total_orphans = int(sum(int(i.get('count') or 0) for i in report.get('orphans', [])))
        total_recon = int(sum(int(i.get('count') or 0) for i in report.get('reconciliation', [])))
        return jsonify(ok=True, total_issues=(total_orphans + total_recon), report=report)
