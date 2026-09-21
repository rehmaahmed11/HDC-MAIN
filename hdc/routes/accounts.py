"""HDC routes: Unified Accounts pages and personal management.

Moved verbatim from hdc_erp.py; each handler keeps its
original @app.route decorator and endpoint name.
"""

from datetime import datetime

from flask import abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func, or_

from hdc.extensions import _money_write_required, _admin_only, db
from hdc.models.accounts import Account, AccountTransaction, ExpenseCategory, PersonalExpense, PersonalExpenseCategory
from hdc.models.materials import Supplier
from hdc.models.office import OfficeStaff
from hdc.models.projects import Project, Stage
from hdc.models.subcontract import Subcontractor
from hdc.models.workforce import Worker
from hdc.services.accounts import _ACCOUNT_LOAN_INTENTS, _ACCOUNT_TXN_CATEGORIES, _ACCOUNT_TXN_FORM_OPTIONS, _ACCOUNT_TXN_TYPES, _ACCOUNT_TYPES, _account_dashboard_kpis, _account_dashboard_subgroups, _account_entity_label, _account_group_mode_for_row, _account_intent_field_matrix, _account_kpi_detail_context, _account_ledger_rows, _account_reverse_transaction_group, _account_running_balance_rows, _account_transaction_history, _account_tx_direction_for_type, _account_txn_group_rows, _accounts_reconciliation_findings, _accounts_set_void_by_source, _accounts_toggle_transaction_void_state, _accounts_update_manual_transaction, _create_account, _create_accounts_transaction_with_sync, _list_accounts_with_balances, _resolve_account_type
from hdc.services.aggregation import _running_projects_receivable_rows
from hdc.services.ledger import _office_staff_ledger_snapshot, _personal_expense_month, _personal_expense_total
from hdc.services.receipts import _account_receipt_recent_entries, _receipt_company_profile
from hdc.utils.dates import _pkt_now_naive, _pkt_today
from hdc.utils.format import _amount_to_words, _flt, _parse_date
from hdc.utils.money import sync_money_fields
from hdc.services.loans import attach_ledger_transaction, find_open_loan
from hdc.utils.normalize import _normalize_account_group, _normalize_account_mode, _normalize_account_tx_direction, _normalize_account_tx_type, _normalize_name_ci, _normalize_related_entity_type

def register(app):
    """Register Unified Accounts pages and personal management."""
    @app.route('/hdc/accounts', methods=['GET', 'POST'])
    @login_required
    def hdc_accounts():
        if _admin_only():
            return redirect(url_for('hdc_dashboard'))

        if request.method == 'POST':
            action = (request.form.get('action') or '').strip().lower()
            if action == 'create_account':
                name = (request.form.get('name') or '').strip()
                acc_group = _normalize_account_group(request.form.get('account_group'))
                acc_mode = _normalize_account_mode(request.form.get('account_mode'))
                acc_type = _resolve_account_type(
                    group=acc_group,
                    mode=acc_mode,
                    explicit_type=request.form.get('type')
                )
                opening = _flt(request.form.get('opening_balance'), 0.0)
                row, msg = _create_account(
                    name,
                    acc_type,
                    opening_balance=opening,
                    bank_name=request.form.get('bank_name'),
                    account_number=request.form.get('account_number'),
                    iban=request.form.get('iban'),
                    account_mode=acc_mode,
                )
                if not row:
                    flash(msg or 'Unable to create account.', 'danger')
                else:
                    db.session.commit()
                    flash(f'Account created: {row.name}.', 'success')
                return redirect(url_for('hdc_accounts'))

            if action == 'update_account':
                account_id = request.form.get('account_id', type=int)
                row = Account.query.get(account_id) if account_id else None
                if not row or row.is_void:
                    flash('Account not found.', 'danger')
                    return redirect(url_for('hdc_accounts'))
                nm = _normalize_name_ci(request.form.get('name') or row.name)
                acc_group = _normalize_account_group(request.form.get('account_group') or _account_group_mode_for_row(row)[0])
                acc_mode = _normalize_account_mode(request.form.get('account_mode') or _account_group_mode_for_row(row)[1])
                tp = _resolve_account_type(
                    group=acc_group,
                    mode=acc_mode,
                    explicit_type=request.form.get('type') or row.type
                )
                opening = _flt(request.form.get('opening_balance'), row.opening_balance)
                bank_name = _normalize_name_ci(request.form.get('bank_name') or row.bank_name or '')
                account_number = (request.form.get('account_number') or row.account_number or '').strip()
                iban = (request.form.get('iban') or row.iban or '').strip()
                if tp not in _ACCOUNT_TYPES:
                    flash(f'Account type must be one of: {", ".join(_ACCOUNT_TYPES)}.', 'danger')
                    return redirect(url_for('hdc_accounts'))
                if acc_mode == 'bank' and ((not bank_name) or (not account_number)):
                    flash('Bank name and account number are required for bank accounts.', 'danger')
                    return redirect(url_for('hdc_accounts'))
                if acc_mode != 'bank':
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
                    flash('Another account with this name already exists.', 'danger')
                    return redirect(url_for('hdc_accounts'))
                row.name = nm
                row.type = tp
                row.opening_balance = float(opening or 0.0)
                sync_money_fields(row, 'opening_balance', 'opening_balance_minor')
                row.bank_name = bank_name or None
                row.account_number = account_number or None
                row.iban = iban or None
                db.session.commit()
                flash(f'Account updated: {row.name}.', 'success')
                return redirect(url_for('hdc_accounts'))

            if action == 'delete_account':
                account_id = request.form.get('account_id', type=int)
                row = Account.query.get(account_id) if account_id else None
                if not row or row.is_void:
                    flash('Account not found.', 'danger')
                    return redirect(url_for('hdc_accounts'))
                has_txn = db.session.query(AccountTransaction.id).filter(
                    AccountTransaction.is_void == False,
                    or_(AccountTransaction.from_account_id == row.id, AccountTransaction.to_account_id == row.id)
                ).first() is not None
                if has_txn:
                    row.status = 'inactive'
                    row.is_void = True
                    db.session.commit()
                    flash('Account has transactions, so it was archived instead of deleted.', 'warning')
                    return redirect(url_for('hdc_accounts'))
                db.session.delete(row)
                db.session.commit()
                flash('Account deleted.', 'success')
                return redirect(url_for('hdc_accounts'))

            if action == 'suspend_account':
                account_id = request.form.get('account_id', type=int)
                row = Account.query.get(account_id) if account_id else None
                if (not row) or row.is_void:
                    flash('Account not found.', 'danger')
                    return redirect(url_for('hdc_accounts'))
                row.status = 'inactive'
                db.session.commit()
                flash(f'Account suspended: {row.name}.', 'warning')
                return redirect(url_for('hdc_accounts'))

            if action == 'activate_account':
                account_id = request.form.get('account_id', type=int)
                row = Account.query.get(account_id) if account_id else None
                if (not row) or row.is_void:
                    flash('Account not found.', 'danger')
                    return redirect(url_for('hdc_accounts'))
                row.status = 'active'
                db.session.commit()
                flash(f'Account activated: {row.name}.', 'success')
                return redirect(url_for('hdc_accounts'))

            if action == 'void_transaction':
                txn_id = request.form.get('transaction_id', type=int)
                reason = (request.form.get('void_reason') or '').strip() or 'Voided from Accounts'
                ok, msg, cnt = _accounts_toggle_transaction_void_state(txn_id, make_void=True, reason=reason, actor=current_user)
                if not ok:
                    flash(msg or 'Unable to void transaction.', 'danger')
                else:
                    flash(f'Transaction voided ({cnt} row{"s" if cnt != 1 else ""}).', 'success')
                return redirect(request.referrer or url_for('hdc_accounts'))

            if action == 'restore_transaction':
                txn_id = request.form.get('transaction_id', type=int)
                ok, msg, cnt = _accounts_toggle_transaction_void_state(txn_id, make_void=False, reason='', actor=current_user)
                if not ok:
                    flash(msg or 'Unable to restore transaction.', 'danger')
                else:
                    flash(f'Transaction restored ({cnt} row{"s" if cnt != 1 else ""}).', 'success')
                return redirect(request.referrer or url_for('hdc_accounts'))

            if action == 'update_transaction':
                txn_id = request.form.get('transaction_id', type=int)
                payload = {
                    'date': (request.form.get('date') or '').strip(),
                    'type': (request.form.get('type') or '').strip(),
                    'amount': request.form.get('amount'),
                    'from_account_id': request.form.get('from_account_id'),
                    'to_account_id': request.form.get('to_account_id'),
                    'executed_by_account_id': request.form.get('executed_by_account_id') or request.form.get('from_account_id'),
                    'project_id': request.form.get('project_id'),
                    'stage_id': request.form.get('stage_id'),
                    'related_entity_type': request.form.get('related_entity_type'),
                    'related_entity_id': request.form.get('related_entity_id'),
                    'party_name': request.form.get('party_name'),
                    'note': request.form.get('note'),
                    'reference_id': request.form.get('reference_id'),
                }
                ok, msg, _ = _accounts_update_manual_transaction(txn_id, payload)
                if not ok:
                    flash(msg or 'Unable to update transaction.', 'danger')
                else:
                    flash('Transaction updated successfully.', 'success')
                return redirect(request.referrer or url_for('hdc_accounts'))

            if action == 'reverse_transaction':
                txn_id = request.form.get('transaction_id', type=int)
                reason = (request.form.get('reverse_reason') or '').strip()
                ok, msg, cnt = _account_reverse_transaction_group(txn_id, reason=reason)
                if not ok:
                    flash(msg or 'Unable to create reversal entry.', 'danger')
                else:
                    flash(f'Reversal entry posted ({cnt} row{"s" if cnt != 1 else ""}).', 'success')
                return redirect(request.referrer or url_for('hdc_accounts'))

            if action == 'create_transaction':
                raw_tx_type = (request.form.get('type') or request.form.get('transaction_type') or '').strip().lower()
                req_tx_type = _normalize_account_tx_type(raw_tx_type)
                if req_tx_type == 'advance_to_person' and raw_tx_type not in _ACCOUNT_LOAN_INTENTS:
                    flash('Advance To Worker is removed from Accounts. Use Worker payment/ledger instead.', 'warning')
                    return redirect(url_for('hdc_accounts'))

                # Loan movements: what the loan ledger needs is checked *before*
                # the money is posted, so a repayment against nobody is refused
                # outright instead of leaving a posting with no loan behind it.
                loan_effect = None
                loan_ledger_type = None
                if raw_tx_type in _ACCOUNT_LOAN_INTENTS:
                    loan_effect, loan_ledger_type = _ACCOUNT_LOAN_INTENTS[raw_tx_type]
                    loan_party = _normalize_name_ci(request.form.get('party_name'))
                    if not loan_party:
                        flash('A loan needs the person\u2019s name \u2014 fill the Party field.', 'danger')
                        return redirect(url_for('hdc_accounts'))
                    if loan_effect in ('repay', 'recover'):
                        loan_direction = 'received' if loan_effect == 'repay' else 'given'
                        if find_open_loan(loan_party, loan_direction) is None:
                            label = 'Loan Taken' if loan_effect == 'repay' else 'Loan Given'
                            flash(
                                'No open loan for "%s" \u2014 record it as %s first '
                                '(or open it in Accounts \u2192 Loans), then book the %s.'
                                % (loan_party, label,
                                   'repayment' if loan_effect == 'repay' else 'recovery'),
                                'danger')
                            return redirect(url_for('hdc_accounts'))

                payload = {
                    'date': (request.form.get('date') or '').strip(),
                    'type': (loan_ledger_type if loan_effect else
                             (request.form.get('type') or request.form.get('transaction_type'))),
                    'amount': request.form.get('amount'),
                    'from_account_id': request.form.get('from_account_id'),
                    'to_account_id': request.form.get('to_account_id'),
                    'executed_by_account_id': request.form.get('executed_by_account_id'),
                    'project_id': request.form.get('project_id'),
                    'stage_id': request.form.get('stage_id'),
                    'related_entity_type': request.form.get('related_entity_type'),
                    'related_entity_id': request.form.get('related_entity_id'),
                    'party_name': request.form.get('party_name'),
                    'category': request.form.get('category'),
                    'note': request.form.get('note'),
                    'reference_id': request.form.get('reference_id'),
                    'excess_tip_amount': request.form.get('excess_tip_amount'),
                    'excess_advance_amount': request.form.get('excess_advance_amount'),
                    'settle_shortfall': request.form.get('settle_shortfall'),
                    'expense_category_id': request.form.get('expense_category_id'),
                    'office_target': request.form.get('office_target'),
                    'office_expense_category': request.form.get('office_expense_category'),
                }
                # Duplicate detection is enforced inside _create_accounts_transaction_with_sync
                # using a typed comparison + recent-window check (avoids the string/int mismatch
                # the previous broad query had).
                ok, msg, rows = _create_accounts_transaction_with_sync(payload)
                if not ok:
                    flash(msg or 'Unable to create transaction.', 'danger')
                else:
                    if loan_effect and rows:
                        try:
                            attach_ledger_transaction(
                                rows[0], loan_effect,
                                principal_amount=request.form.get('loan_principal_amount'),
                                interest_amount=request.form.get('loan_interest_amount'),
                                actor=current_user, commit=True)
                        except ValueError as exc:
                            # The ledger row is posted (it is real money and must
                            # not be silently dropped); say what to do next.
                            flash('Transaction recorded, but the loan ledger could not be '
                                  'updated: %s Fix that in Accounts \u2192 Loans.' % exc, 'warning')
                    flash('Transaction recorded (%d row%s%s).'
                          % (len(rows), 's' if len(rows) != 1 else '',
                             ', linked to the loan ledger' if loan_effect else ''), 'success')
                    last_id = max([int(getattr(r, 'id', 0) or 0) for r in (rows or [])] or [0])
                    if last_id > 0:
                        return redirect(url_for('hdc_accounts', print_txn_id=last_id))
                return redirect(url_for('hdc_accounts'))

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
        metric = (request.args.get('metric') or '').strip().lower()
        drill_account_id = request.args.get('drill_account_id', type=int)
        drill_tx_type = (request.args.get('drill_tx_type') or '').strip().lower()
        effective_account_id = drill_account_id or account_id
        effective_tx_type = drill_tx_type or tx_type
        date_from = _parse_date((request.args.get('date_from') or '').strip(), fallback=None)
        date_to = _parse_date((request.args.get('date_to') or '').strip(), fallback=None)
        show_auto_person = (str(request.args.get('show_auto_person') or '').strip().lower() in ('1', 'true', 'yes', 'on'))
        print_txn_id = request.args.get('print_txn_id', type=int)
        edit_txn_id = request.args.get('edit_txn_id', type=int)

        edit_txn = None
        if edit_txn_id:
            edit_txn = AccountTransaction.query.get(edit_txn_id)
            if not edit_txn or edit_txn.is_void:
                flash('Transaction not found or voided.', 'danger')
                return redirect(url_for('hdc_accounts'))

        accounts = _list_accounts_with_balances()
        form_accounts = _list_accounts_with_balances(include_inactive=True)
        projects_list = Project.query.order_by(Project.name.asc()).all()
        workers_list = Worker.query.order_by(Worker.active_status.desc(), Worker.name.asc()).all()
        office_staff_list = OfficeStaff.query.order_by(OfficeStaff.active_status.desc(), OfficeStaff.name.asc()).all()
        suppliers_list = Supplier.query.filter(Supplier.is_void == False).order_by(Supplier.name.asc()).all()
        subcontractors_list = Subcontractor.query.order_by(Subcontractor.name.asc()).all()
        worker_option_rows = []
        for w in workers_list:
            worker_option_rows.append({
                'id': int(w.id),
                'label': f'{w.worker_code or ("W-" + str(w.id))} | {w.name} | {w.role_type or "Worker"}'
                         + (' [Suspended]' if not bool(getattr(w, 'active_status', True)) else '')
            })
        supplier_option_rows = []
        for s in suppliers_list:
            supplier_option_rows.append({
                'id': int(s.id),
                'label': f'{s.name}'
            })
        subcontractor_option_rows = []
        for s in subcontractors_list:
            code = (s.subcontractor_code or f'SUB-{s.id}')
            stage_name = (s.stage_rel.name if getattr(s, 'stage_rel', None) else '')
            project_name = ''
            project_id_for_option = int(getattr(s, 'project_id', 0) or 0)
            stage_id_for_option = int(getattr(s, 'stage_id', 0) or 0)
            if getattr(s, 'stage_rel', None) and getattr(s.stage_rel, 'project', None):
                project_name = s.stage_rel.project.name or ''
                if not project_id_for_option:
                    project_id_for_option = int(getattr(s.stage_rel, 'project_id', 0) or 0)
            scope = ' / '.join([x for x in [project_name, stage_name] if x]).strip()
            subcontractor_option_rows.append({
                'id': int(s.id),
                'label': f'{code} | {s.name}' + (f' | {scope}' if scope else ''),
                'project_id': (project_id_for_option or None),
                'stage_id': (stage_id_for_option or None),
            })
        office_staff_option_rows = []
        for s in office_staff_list:
            snap = _office_staff_ledger_snapshot(s.id)
            pending_amt = max(0.0, float(snap.get('balance') or 0.0))
            office_staff_option_rows.append({
                'id': int(s.id),
                'label': f'{s.staff_code or ("OFF-" + str(s.id))} | {s.name} | Pending: {pending_amt:,.0f}',
            })
        project_owner_names = sorted({
            _normalize_name_ci(p.client or '')
            for p in projects_list
            if _normalize_name_ci(p.client or '')
        })
        project_client_meta = {}
        name_to_client_acc = {}
        for a in (form_accounts or []):
            nm = _normalize_name_ci(a.get('name') or '')
            if not nm:
                continue
            if str(a.get('type') or '').strip().lower() == 'client':
                name_to_client_acc[nm.lower()] = a
        for p in projects_list:
            cn = _normalize_name_ci(getattr(p, 'client', '') or '')
            acc = name_to_client_acc.get(cn.lower()) if cn else None
            project_client_meta[str(int(p.id))] = {
                'project_id': int(p.id),
                'project_name': (p.name or ''),
                'client_name': cn,
                'account_id': (int(acc.get('id')) if acc and acc.get('id') else None),
                'account_label': ((acc.get('name') or '') + ' (Client)' if acc else ''),
                'balance': (float(acc.get('current_balance') or 0.0) if acc else 0.0),
            }
        if not show_auto_person:
            accounts = [a for a in accounts if not (a.get('type') == 'person' and bool(a.get('auto_generated')))]
        inactive_accounts = _list_accounts_with_balances(include_inactive=True)
        inactive_accounts = [a for a in inactive_accounts if str(a.get('status') or '').strip().lower() == 'inactive']
        if not show_auto_person:
            inactive_accounts = [a for a in inactive_accounts if not (a.get('type') == 'person' and bool(a.get('auto_generated')))]
        history_rows = _account_transaction_history(
            account_id=effective_account_id or None,
            account_group=(account_group or None),
            date_from=date_from,
            date_to=date_to,
            tx_direction=(tx_direction or None),
            project_id=project_id or None,
            stage_id=stage_id or None,
            tx_type=(effective_tx_type or None),
            category=(category or None),
            group_id=(group_id or None),
            reference_id=(reference_id or None),
            limit=600
        )
        voided_rows = (AccountTransaction.query
                       .filter(AccountTransaction.is_void == True)
                       .order_by(AccountTransaction.date.desc(), AccountTransaction.id.desc())
                       .limit(200)
                       .all())
        running_map = _account_running_balance_rows(history_rows, account_id=(effective_account_id or None))
        kpis = _account_dashboard_kpis(date_from=date_from, date_to=date_to)
        latest_print_txn = None
        if print_txn_id:
            cand = AccountTransaction.query.get(int(print_txn_id))
            if cand and not cand.is_void:
                latest_print_txn = cand
        subgroup_cards = _account_dashboard_subgroups(metric)
        project_receivable_rows = _running_projects_receivable_rows()
        personal_expense_category_names = sorted({
            (r.name or '').strip()
            for r in PersonalExpenseCategory.query
            .filter(PersonalExpenseCategory.active_status == True)
            .all()
            if (r.name or '').strip()
        }, key=lambda x: x.lower())
        personal_expense_party_names = sorted({
            _normalize_name_ci(r.beneficiary_name or '')
            for r in PersonalExpense.query
            .filter(PersonalExpense.is_void == False)
            .all()
            if _normalize_name_ci(r.beneficiary_name or '')
        }, key=lambda x: x.lower())
        return render_template('accounts/accounts.html',
            accounts=accounts,
            form_accounts=form_accounts,
            inactive_accounts=inactive_accounts,
            projects=projects_list,
            project_owner_names=project_owner_names,
            stages=Stage.query.order_by(Stage.project_id.asc(), Stage.name.asc()).all(),
            workers=workers_list,
            suppliers=suppliers_list,
            subcontractors=subcontractors_list,
            worker_option_rows=worker_option_rows,
            supplier_option_rows=supplier_option_rows,
            subcontractor_option_rows=subcontractor_option_rows,
            office_staff_option_rows=office_staff_option_rows,
            project_client_meta=project_client_meta,
            history_rows=history_rows,
            voided_rows=voided_rows,
            running_map=running_map,
            account_kpis=kpis,
            subgroup_cards=subgroup_cards,
            project_receivable_rows=project_receivable_rows,
            active_metric=metric,
            drill_account_id=drill_account_id,
            drill_tx_type=drill_tx_type,
            intent_matrix=_account_intent_field_matrix(),
            txn_types=_ACCOUNT_TXN_TYPES,
            txn_form_options=_ACCOUNT_TXN_FORM_OPTIONS,
            account_types=_ACCOUNT_TYPES,
            txn_categories=_ACCOUNT_TXN_CATEGORIES,
            filter_account_id=account_id,
            filter_account_group=account_group,
            filter_project_id=project_id,
            filter_stage_id=stage_id,
            filter_tx_type=tx_type,
            filter_direction=tx_direction,
            filter_category=category,
            filter_group_id=group_id,
            filter_reference_id=reference_id,
            show_auto_person=show_auto_person,
            filter_date_from=(date_from.isoformat() if date_from else ''),
            filter_date_to=(date_to.isoformat() if date_to else ''),
            latest_print_txn=latest_print_txn,
            account_tx_direction_for_type=_account_tx_direction_for_type,
            expense_categories=ExpenseCategory.query.filter(ExpenseCategory.active_status == True).order_by(ExpenseCategory.name.asc()).all(),
            personal_expense_category_names=personal_expense_category_names,
            personal_expense_party_names=personal_expense_party_names,
            office_expense_categories=[],
            edit_txn=edit_txn,
            today=_pkt_today().isoformat()
        )


    # —— Personal Management ——————————————————————————————————————————————————————
    @app.route('/hdc/personal-management')
    @login_required
    def hdc_personal_management():
        personal_expense_total = _personal_expense_total()
        personal_expense_month = _personal_expense_month()
        personal_categories = (PersonalExpenseCategory.query
                               .filter(PersonalExpenseCategory.active_status == True)
                               .order_by(PersonalExpenseCategory.name.asc())
                               .all())
        personal_expense_categories_count = int(PersonalExpenseCategory.query.filter(PersonalExpenseCategory.active_status == True).count() or 0)
        unique_beneficiaries = int(
            db.session.query(func.count(func.distinct(PersonalExpense.beneficiary_name)))
            .filter(PersonalExpense.is_void == False)
            .scalar() or 0
        )
        return render_template('accounts/personal_management.html',
            personal_expense_total=personal_expense_total,
            personal_expense_month=personal_expense_month,
            personal_categories=personal_categories,
            personal_expense_categories_count=personal_expense_categories_count,
            unique_beneficiaries=unique_beneficiaries,
            today=_pkt_today().isoformat()
        )


    @app.route('/hdc/personal-management/expenses', methods=['GET', 'POST'])
    @login_required
    @_money_write_required()
    def hdc_personal_expenses():
        if request.method == 'POST':
            action = (request.form.get('action') or '').strip().lower()
            if action == 'add':
                flash('Direct add is disabled. Please record personal expenses from Accounts.', 'warning')
                return redirect(url_for('hdc_personal_expenses'))
    
        # GET: List expenses
        page = request.args.get('page', default=1, type=int) or 1
        page = max(1, page)
        per_page = 25
        date_from = _parse_date((request.args.get('date_from') or '').strip(), fallback=None)
        date_to = _parse_date((request.args.get('date_to') or '').strip(), fallback=None)
        category_filter = (request.args.get('category') or '').strip()
    
        q = PersonalExpense.query
        if (request.args.get('show_void') or '').strip().lower() != 'yes':
            q = q.filter(PersonalExpense.is_void == False)
        if date_from:
            q = q.filter(PersonalExpense.date >= date_from)
        if date_to:
            q = q.filter(PersonalExpense.date <= date_to)
        if category_filter:
            q = q.filter(func.lower(func.trim(func.coalesce(PersonalExpense.category, ''))) == category_filter.lower())
    
        rows = q.order_by(PersonalExpense.date.desc(), PersonalExpense.id.desc()).paginate(page=page, per_page=per_page)
        categories = PersonalExpenseCategory.query.filter(PersonalExpenseCategory.active_status == True).order_by(PersonalExpenseCategory.name.asc()).all()
    
        return render_template('accounts/personal_expenses.html',
            rows=rows,
            categories=categories,
            filter_date_from=(date_from.isoformat() if date_from else ''),
            filter_date_to=(date_to.isoformat() if date_to else ''),
            filter_category=category_filter,
            show_void=(request.args.get('show_void') or '').strip().lower() == 'yes',
            today=_pkt_today().isoformat()
        )


    @app.route('/hdc/personal-management/expenses/<int:eid>/void', methods=['GET', 'POST'])
    @login_required
    @_money_write_required()
    def hdc_personal_expense_void(eid):
        row = PersonalExpense.query.get_or_404(eid)
        if request.method == 'POST':
            void_reason = (request.form.get('void_reason') or '').strip()
            if not void_reason:
                flash('Void reason is required.', 'warning')
                return redirect(url_for('hdc_personal_expenses'))
        
            row.is_void = True
            row.void_reason = void_reason
            row.voided_at = _pkt_now_naive()
            _accounts_set_void_by_source('personal_expense', row.id, True)
            db.session.commit()
            flash(f'Personal expense to {row.beneficiary_name} voided.', 'success')
            return redirect(url_for('hdc_personal_expenses'))
    
        return render_template('accounts/personal_expense_void.html', row=row)


    @app.route('/hdc/personal-management/categories', methods=['GET', 'POST'])
    @login_required
    @_money_write_required()
    def hdc_personal_expense_categories():
        if request.method == 'POST':
            action = (request.form.get('action') or '').strip().lower()
        
            if action == 'add':
                name = (request.form.get('name') or '').strip()
                description = (request.form.get('description') or '').strip()
            
                if not name:
                    flash('Category name is required.', 'warning')
                    return redirect(url_for('hdc_personal_expense_categories'))
            
                existing = PersonalExpenseCategory.query.filter(
                    func.lower(PersonalExpenseCategory.name) == name.lower()
                ).first()
                if existing:
                    flash('A category with that name already exists.', 'danger')
                    return redirect(url_for('hdc_personal_expense_categories'))
            
                cat = PersonalExpenseCategory(
                    name=name,
                    description=description,
                    active_status=True
                )
                db.session.add(cat)
                db.session.commit()
                flash(f'Personal expense category "{name}" created.', 'success')
                return redirect(url_for('hdc_personal_expense_categories'))
    
        categories = PersonalExpenseCategory.query.order_by(PersonalExpenseCategory.name.asc()).all()
        return render_template('accounts/personal_expense_categories.html', categories=categories)


    @app.route('/hdc/personal-management/categories/<int:cat_id>/suspend', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_personal_category_suspend(cat_id):
        cat = PersonalExpenseCategory.query.get_or_404(cat_id)
        cat.active_status = False
        db.session.commit()
        flash(f'Category "{cat.name}" suspended.', 'success')
        return redirect(url_for('hdc_personal_expense_categories'))


    @app.route('/hdc/personal-management/categories/<int:cat_id>/activate', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_personal_category_activate(cat_id):
        cat = PersonalExpenseCategory.query.get_or_404(cat_id)
        cat.active_status = True
        db.session.commit()
        flash(f'Category "{cat.name}" activated.', 'success')
        return redirect(url_for('hdc_personal_expense_categories'))


    @app.route('/hdc/accounts/<int:account_id>/ledger')
    @login_required
    def hdc_account_ledger(account_id):
        if _admin_only():
            return redirect(url_for('hdc_dashboard'))
        account = Account.query.get_or_404(account_id)
        if account.is_void:
            abort(404)
        rows = _account_ledger_rows(account.id)
        return render_template('accounts/account_ledger.html',
            account=account,
            rows=rows
        )


    @app.route('/hdc/accounts/transactions/<int:txn_id>/edit', methods=['GET', 'POST'])
    @login_required
    def hdc_account_transaction_edit(txn_id):
        if _admin_only():
            return redirect(url_for('hdc_dashboard'))
        row = AccountTransaction.query.get_or_404(txn_id)
        rows = _account_txn_group_rows(row)
        if len(rows) != 1:
            flash('Grouped/split transactions cannot be edited directly. Void/reverse and recreate.', 'warning')
            return redirect(url_for('hdc_accounts_entries'))
        if row.is_void:
            flash('Voided transaction cannot be edited.', 'warning')
            return redirect(url_for('hdc_accounts_entries'))

        if request.method == 'POST':
            payload = {
                'date': (request.form.get('date') or '').strip(),
                'type': (request.form.get('type') or '').strip(),
                'amount': request.form.get('amount'),
                'from_account_id': request.form.get('from_account_id'),
                'to_account_id': request.form.get('to_account_id'),
                'executed_by_account_id': request.form.get('executed_by_account_id') or request.form.get('from_account_id'),
                'project_id': request.form.get('project_id'),
                'stage_id': request.form.get('stage_id'),
                'related_entity_type': request.form.get('related_entity_type'),
                'related_entity_id': request.form.get('related_entity_id'),
                'party_name': request.form.get('party_name'),
                'note': request.form.get('note'),
                'reference_id': request.form.get('reference_id'),
            }
            ok, msg, _ = _accounts_update_manual_transaction(txn_id, payload)
            if not ok:
                flash(msg or 'Unable to update transaction.', 'danger')
                return redirect(url_for('hdc_account_transaction_edit', txn_id=txn_id))
            flash('Transaction updated successfully.', 'success')
            return redirect(url_for('hdc_accounts_entries'))

        return render_template('accounts/accounts_transaction_edit.html',
            txn=row,
            accounts=_list_accounts_with_balances(include_inactive=True),
            projects=Project.query.order_by(Project.name.asc()).all(),
            stages=Stage.query.order_by(Stage.project_id.asc(), Stage.name.asc()).all(),
            txn_types=_ACCOUNT_TXN_TYPES
        )


    @app.route('/hdc/accounts/entries', methods=['GET', 'POST'])
    @login_required
    def hdc_accounts_entries():
        if _admin_only():
            return redirect(url_for('hdc_dashboard'))

        if request.method == 'POST':
            action = (request.form.get('action') or '').strip().lower()
            if action == 'update_transaction':
                txn_id = request.form.get('transaction_id', type=int)
                payload = {
                    'date': (request.form.get('date') or '').strip(),
                    'type': (request.form.get('type') or '').strip(),
                    'amount': request.form.get('amount'),
                    'from_account_id': request.form.get('from_account_id'),
                    'to_account_id': request.form.get('to_account_id'),
                    'executed_by_account_id': request.form.get('executed_by_account_id') or request.form.get('from_account_id'),
                    'project_id': request.form.get('project_id'),
                    'stage_id': request.form.get('stage_id'),
                    'related_entity_type': request.form.get('related_entity_type'),
                    'related_entity_id': request.form.get('related_entity_id'),
                    'party_name': request.form.get('party_name'),
                    'note': request.form.get('note'),
                    'reference_id': request.form.get('reference_id'),
                }
                ok, msg, _ = _accounts_update_manual_transaction(txn_id, payload)
                if not ok:
                    flash(msg or 'Unable to update transaction.', 'danger')
                else:
                    flash('Transaction updated successfully.', 'success')
                return redirect(url_for('hdc_accounts_entries'))
            return redirect(url_for('hdc_accounts_entries'))

        edit_txn_id = request.args.get('edit_txn_id', type=int)
        edit_txn = None
        if edit_txn_id:
            edit_txn = AccountTransaction.query.get(edit_txn_id)
            if not edit_txn or edit_txn.is_void:
                flash('Transaction not found or voided.', 'danger')
                return redirect(url_for('hdc_accounts_entries'))

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
        party_name = (request.args.get('party_name') or '').strip()
        worker_id_f = request.args.get('worker_id', type=int)
        date_from = _parse_date((request.args.get('date_from') or '').strip(), fallback=None)
        date_to = _parse_date((request.args.get('date_to') or '').strip(), fallback=None)
        print_txn_id = request.args.get('print_txn_id', type=int)
        show_auto_person = (str(request.args.get('show_auto_person') or '').strip().lower() in ('1', 'true', 'yes', 'on'))
        page = max(1, request.args.get('page', type=int) or 1)
        try:
            per_page = int(request.args.get('per_page', type=int) or 50)
        except Exception:
            per_page = 50
        per_page = max(10, min(per_page, 200))

        txn_form_options = list(_ACCOUNT_TXN_FORM_OPTIONS)
        if edit_txn and edit_txn.type not in [t['value'] for t in txn_form_options]:
            dir = _account_tx_direction_for_type(edit_txn.type)
            label = edit_txn.type.replace('_', ' ').title()
            txn_form_options.append({'value': edit_txn.type, 'label': label, 'direction': dir})

        accounts = _list_accounts_with_balances()
        form_accounts = _list_accounts_with_balances(include_inactive=True)
        if not show_auto_person:
            accounts = [a for a in accounts if not (a.get('type') == 'person' and bool(a.get('auto_generated')))]
            form_accounts = [a for a in form_accounts if not (a.get('type') == 'person' and bool(a.get('auto_generated')))]

        base_query = _account_transaction_history(
            account_id=(account_id or None),
            account_group=(account_group or None),
            date_from=date_from,
            date_to=date_to,
            tx_direction=(tx_direction or None),
            project_id=(project_id or None),
            stage_id=(stage_id or None),
            tx_type=(tx_type or None),
            category=(category or None),
            group_id=(group_id or None),
            reference_id=(reference_id or None),
            party_name=(party_name or None),
            worker_id=(worker_id_f or None),
            return_query=True,
            include_void=True,
        )
        pg_total_items = base_query.count()
        pg_total_pages = max(1, (pg_total_items + per_page - 1) // per_page) if pg_total_items else 1
        page = min(page, pg_total_pages)
        history_rows = base_query.offset((page - 1) * per_page).limit(per_page).all()
        running_map = _account_running_balance_rows(history_rows, account_id=(account_id or None))
        latest_print_txn = None
        if print_txn_id:
            cand = AccountTransaction.query.get(int(print_txn_id))
            if cand and not cand.is_void:
                latest_print_txn = cand

        workers_for_filter = Worker.query.order_by(Worker.name.asc()).all()

        suppliers_list = Supplier.query.filter(Supplier.is_void == False).order_by(Supplier.name.asc()).all()
        subcontractors_list = Subcontractor.query.order_by(Subcontractor.name.asc()).all()
        office_staff_list = OfficeStaff.query.order_by(OfficeStaff.active_status.desc(), OfficeStaff.name.asc()).all()

        worker_option_rows = []
        for w in workers_for_filter:
            worker_option_rows.append({
                'id': int(w.id),
                'label': f'{w.worker_code or ("W-" + str(w.id))} | {w.name} | {w.role_type or "Worker"}' + (' [Suspended]' if not bool(getattr(w, 'active_status', True)) else '')
            })
        supplier_option_rows = []
        for s in suppliers_list:
            supplier_option_rows.append({
                'id': int(s.id),
                'label': f'{s.name}'
            })
        subcontractor_option_rows = []
        for s in subcontractors_list:
            code = (s.subcontractor_code or f'SUB-{s.id}')
            stage_name = (s.stage_rel.name if getattr(s, 'stage_rel', None) else '')
            project_name = ''
            project_id_for_option = int(getattr(s, 'project_id', 0) or 0)
            stage_id_for_option = int(getattr(s, 'stage_id', 0) or 0)
            if getattr(s, 'stage_rel', None) and getattr(s.stage_rel, 'project', None):
                project_name = s.stage_rel.project.name or ''
                if not project_id_for_option:
                    project_id_for_option = int(getattr(s.stage_rel, 'project_id', 0) or 0)
            scope = ' / '.join([x for x in [project_name, stage_name] if x]).strip()
            subcontractor_option_rows.append({
                'id': int(s.id),
                'label': f'{code} | {s.name}' + (f' | {scope}' if scope else ''),
                'project_id': (project_id_for_option or None),
                'stage_id': (stage_id_for_option or None),
            })
        office_staff_option_rows = []
        for s in office_staff_list:
            snap = _office_staff_ledger_snapshot(s.id)
            pending_amt = max(0.0, float(snap.get('balance') or 0.0))
            office_staff_option_rows.append({
                'id': int(s.id),
                'label': f'{s.staff_code or ("OFF-" + str(s.id))} | {s.name} | Pending: {pending_amt:,.0f}',
            })

        pg_query = {
            'account_id': account_id or '',
            'account_group': account_group or '',
            'project_id': project_id or '',
            'stage_id': stage_id or '',
            'transaction_type': tx_type or '',
            'direction': tx_direction or '',
            'category': category or '',
            'group_id': group_id or '',
            'reference_id': reference_id or '',
            'party_name': party_name or '',
            'worker_id': worker_id_f or '',
            'date_from': (date_from.isoformat() if date_from else ''),
            'date_to': (date_to.isoformat() if date_to else ''),
            'show_auto_person': (1 if show_auto_person else 0),
            'per_page': per_page,
        }
        pg_query = {k: v for k, v in pg_query.items() if v not in ('', None)}

        personal_expense_category_names = sorted({
            (r.name or '').strip()
            for r in PersonalExpenseCategory.query
            .filter(PersonalExpenseCategory.active_status == True)
            .all()
            if (r.name or '').strip()
        }, key=lambda x: x.lower())
        personal_expense_party_names = sorted({
            _normalize_name_ci(r.beneficiary_name or '')
            for r in PersonalExpense.query
            .filter(PersonalExpense.is_void == False)
            .all()
            if _normalize_name_ci(r.beneficiary_name or '')
        }, key=lambda x: x.lower())

        project_receivable_rows = _running_projects_receivable_rows()

        project_client_meta = {}
        name_to_client_acc = {}
        for a in (form_accounts or []):
            nm = _normalize_name_ci(a.get('name') or '')
            if not nm:
                continue
            if str(a.get('type') or '').strip().lower() == 'client':
                name_to_client_acc[nm.lower()] = a
        for p in Project.query.order_by(Project.name.asc()).all():
            cn = _normalize_name_ci(getattr(p, 'client', '') or '')
            acc = name_to_client_acc.get(cn.lower()) if cn else None
            project_client_meta[str(int(p.id))] = {
                'project_id': int(p.id),
                'project_name': (p.name or ''),
                'client_name': cn,
                'account_id': (int(acc.get('id')) if acc and acc.get('id') else None),
                'account_label': ((acc.get('name') or '') + ' (Client)' if acc else ''),
                'balance': (float(acc.get('current_balance') or 0.0) if acc else 0.0),
            }

        return render_template('accounts/accounts_entries.html',
            accounts=accounts,
            form_accounts=form_accounts,
            projects=Project.query.order_by(Project.name.asc()).all(),
            stages=Stage.query.order_by(Stage.project_id.asc(), Stage.name.asc()).all(),
            workers_for_filter=workers_for_filter,
            history_rows=history_rows,
            running_map=running_map,
            txn_types=_ACCOUNT_TXN_TYPES,
            txn_categories=_ACCOUNT_TXN_CATEGORIES,
            filter_account_id=account_id,
            filter_account_group=account_group,
            filter_project_id=project_id,
            filter_stage_id=stage_id,
            filter_tx_type=tx_type,
            filter_direction=tx_direction,
            filter_category=category,
            filter_group_id=group_id,
            filter_reference_id=reference_id,
            filter_party_name=party_name,
            filter_worker_id=worker_id_f,
            filter_date_from=(date_from.isoformat() if date_from else ''),
            filter_date_to=(date_to.isoformat() if date_to else ''),
            latest_print_txn=latest_print_txn,
            account_tx_direction_for_type=_account_tx_direction_for_type,
            show_auto_person=show_auto_person,
            pg_page=page,
            pg_total_pages=pg_total_pages,
            pg_total_items=pg_total_items,
            pg_per_page=per_page,
            pg_endpoint='hdc_accounts_entries',
            pg_url_kwargs={},
            pg_query=pg_query,
            suppliers=suppliers_list,
            subcontractors=subcontractors_list,
            office_staff=office_staff_list,
            edit_txn=edit_txn,
            txn_form_options=txn_form_options,
            worker_option_rows=worker_option_rows,
            supplier_option_rows=supplier_option_rows,
            subcontractor_option_rows=subcontractor_option_rows,
            office_staff_option_rows=office_staff_option_rows,
            intent_matrix=_account_intent_field_matrix(),
            expense_categories=ExpenseCategory.query.filter(ExpenseCategory.active_status == True).order_by(ExpenseCategory.name.asc()).all(),
            personal_expense_category_names=personal_expense_category_names,
            personal_expense_party_names=personal_expense_party_names,
            project_receivable_rows=project_receivable_rows,
            project_client_meta=project_client_meta,
            office_expense_categories=[],
            today=_pkt_today().isoformat()
        )


    @app.route('/hdc/accounts/transactions/find')
    @login_required
    def hdc_account_transaction_find():
        """Look up any transaction by its internal ID and jump to its receipt.

        Accepts ?id=<n> in either pure numeric form or pasted as 'TXN #123' /
        'RCPT-ATX-00000123' / 'account_txn#123' — anything with digits in it.
        """
        if _admin_only():
            return redirect(url_for('hdc_dashboard'))
        raw = (request.args.get('id') or request.args.get('q') or '').strip()
        if not raw:
            flash('Enter a transaction ID to look up.', 'warning')
            return redirect(request.referrer or url_for('hdc_accounts'))
        digits = ''.join(ch for ch in raw if ch.isdigit())
        if not digits:
            flash(f'Could not find a transaction ID inside "{raw}".', 'warning')
            return redirect(request.referrer or url_for('hdc_accounts'))
        try:
            tid = int(digits)
        except ValueError:
            flash(f'Invalid transaction ID "{raw}".', 'danger')
            return redirect(request.referrer or url_for('hdc_accounts'))
        row = AccountTransaction.query.get(tid)
        if not row:
            flash(f'Transaction #{tid} not found.', 'danger')
            return redirect(request.referrer or url_for('hdc_accounts'))
        return redirect(url_for('hdc_account_transaction_receipt', txn_id=tid))


    @app.route('/hdc/accounts/transactions/<int:txn_id>/receipt')
    @login_required
    def hdc_account_transaction_receipt(txn_id):
        if _admin_only():
            return redirect(url_for('hdc_dashboard'))
        txn = AccountTransaction.query.get_or_404(txn_id)
        auto_print = (str(request.args.get('auto_print') or '').strip().lower() in ('1', 'true', 'yes', 'on'))
        receipt_id = f"RCPT-ATX-{txn.id:08d}"
        account_used = (txn.executed_by_account.name if txn.executed_by_account else '')
        if not account_used:
            account_used = (txn.from_account.name if txn.from_account else '')
        if not account_used:
            account_used = '-'
        party_label = (txn.party_name or '').strip()
        if not party_label:
            et = _normalize_related_entity_type(txn.related_entity_type)
            party_label = _account_entity_label(et, txn.related_entity_id)
        if not party_label:
            party_label = (txn.to_account.name if txn.to_account else '-')

        direction = _account_tx_direction_for_type(txn.type) or 'transfer'

        # All rows that belong to the same accounting group (e.g. internal split,
        # tip / settlement breakdown) so the receipt shows the full audit trail.
        group_rows = _account_txn_group_rows(txn)
        group_total = sum(float(r.amount or 0.0) for r in (group_rows or []) if not bool(getattr(r, 'is_void', False)))
        split_rows = []
        for r in (group_rows or []):
            split_rows.append({
                'id': int(r.id),
                'date': (r.date.isoformat() if r.date else ''),
                'type_label': str(r.type or '').replace('_', ' ').title(),
                'direction': _account_tx_direction_for_type(r.type),
                'from_account': (r.from_account.name if r.from_account else '-'),
                'to_account': (r.to_account.name if r.to_account else '-'),
                'executed_by': (r.executed_by_account.name if r.executed_by_account else '-'),
                'category': (r.category or '-'),
                'amount': float(r.amount or 0.0),
                'is_void': bool(getattr(r, 'is_void', False)),
                'is_current': (int(r.id) == int(txn.id)),
                'note': (r.note or ''),
                'receipt_url': url_for('hdc_account_transaction_receipt', txn_id=r.id),
            })

        recent_entries, recent_scope_label = _account_receipt_recent_entries(txn, limit=5)
        recent_title = 'Last 5 Related Entries'
        if recent_scope_label:
            recent_title = f'Last 5 Entries For: {recent_scope_label}'

        return render_template('accounts/transaction_receipt.html',
            company_profile=_receipt_company_profile(),
            receipt_id=receipt_id,
            created_at=(txn.created_at or datetime.combine((txn.date or _pkt_today()), datetime.min.time())),
            txn_date=(txn.date.isoformat() if txn.date else '-'),
            tx_type=str(txn.type or '').replace('_', ' ').title(),
            tx_direction=direction,
            category=(txn.category or '-').replace('_', ' ').title(),
            party_name=party_label,
            related_entity_type=(_normalize_related_entity_type(txn.related_entity_type) or ''),
            related_entity_id=(int(txn.related_entity_id) if txn.related_entity_id else None),
            project_name=(txn.project.name if txn.project else '-'),
            stage_name=(txn.stage.name if txn.stage else '-'),
            account_used=account_used,
            from_account_name=(txn.from_account.name if txn.from_account else '-'),
            to_account_name=(txn.to_account.name if txn.to_account else '-'),
            executed_by_account_name=(txn.executed_by_account.name if txn.executed_by_account else '-'),
            amount=float(txn.amount or 0.0),
            amount_words=_amount_to_words(txn.amount or 0.0),
            note=(txn.note or ''),
            reference_id=(txn.reference_id or f'account_txn#{txn.id}'),
            group_id=(txn.group_id or ''),
            source_type=(txn.source_type or ''),
            source_id=(int(txn.source_id) if txn.source_id else None),
            is_void=bool(getattr(txn, 'is_void', False)),
            txn_id=int(txn.id),
            split_rows=split_rows,
            split_total=float(group_total or 0.0),
            recent_entries=recent_entries,
            recent_entries_title=recent_title,
            back_url=url_for('hdc_accounts'),
            print_label='Print / Save PDF',
            auto_print=auto_print
        )


    @app.route('/hdc/accounts/reconciliation')
    @login_required
    def hdc_accounts_reconciliation():
        if _admin_only():
            return redirect(url_for('hdc_dashboard'))
        findings = _accounts_reconciliation_findings()
        total_issues = sum(findings['totals'].values())
        return render_template('accounts/accounts_reconciliation.html',
            findings=findings,
            total_issues=total_issues,
            scanned_at=_pkt_now_naive(),
            back_url=url_for('hdc_accounts'),
        )


    @app.route('/hdc/accounts/kpi/<string:metric>')
    @login_required
    def hdc_accounts_kpi_detail(metric):
        if _admin_only():
            return redirect(url_for('hdc_dashboard'))

        date_from = _parse_date((request.args.get('date_from') or '').strip(), fallback=None)
        date_to = _parse_date((request.args.get('date_to') or '').strip(), fallback=None)
        payable_head = (request.args.get('payable_head') or '').strip()
        spent_head = (request.args.get('spent_head') or '').strip()
        ctx = _account_kpi_detail_context(metric, date_from=date_from, date_to=date_to, payable_head=payable_head, spent_head=spent_head)
        return render_template('accounts/accounts_kpi_detail.html',
            **ctx,
            filter_date_from=(date_from.isoformat() if date_from else ''),
            filter_date_to=(date_to.isoformat() if date_to else ''),
            filter_payable_head=payable_head,
            filter_spent_head=spent_head,
        )
