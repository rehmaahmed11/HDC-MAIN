"""HDC routes: Cash Flow — daily money in / money out records and reporting.

Part of the Accounts section (admin only, like the rest of Accounts).
Built on the unified AccountTransaction ledger via hdc/services/cashflow.py:

  /hdc/accounts/cashflow            daily records + quick flow entry
  /hdc/accounts/cashflow/report     clear, straight cash flow report
  /hdc/accounts/cashflow/export     CSV download of the filtered records

Quick entry maps to the standard transaction engine so every recorded flow
gets the same validation, overdraft protection, duplicate guard, receipt and
traceability as flows created in other modules:

  Money In        -> party_receipt   (source party/client -> company account)
  Money Out       -> party_payment   (company account -> destination/party)
  Internal Transfer -> transfer      (company cash account -> company bank)
"""

import csv
import io

from flask import Response, flash, redirect, render_template, request, url_for
from flask_login import login_required

from hdc.extensions import _admin_only, db
from hdc.models.accounts import Account
from hdc.models.projects import Project
from hdc.services.accounts import (
    _ACCOUNT_COMPANY_TYPES,
    _ACCOUNT_TXN_CATEGORIES,
    _accounts_default_external_parties,
    _accounts_party_account,
    _create_accounts_transaction_with_sync,
    _list_accounts_with_balances,
)
from hdc.services.cashflow import (
    CASHFLOW_DIRECTION_LABELS,
    _cashflow_account_breakdown,
    _cashflow_category_breakdown,
    _cashflow_daily_report_rows,
    _cashflow_group_by_day,
    _cashflow_load_rows,
    _cashflow_opening_funds,
    _cashflow_summary,
)
from hdc.utils.dates import _pkt_today
from hdc.utils.format import _flt, _parse_date, _payload_int
from hdc.utils.normalize import _normalize_name_ci


def _cashflow_filters():
    """Read and sanitise the shared GET filter set."""
    date_from = _parse_date((request.args.get('date_from') or '').strip(), fallback=None)
    date_to = _parse_date((request.args.get('date_to') or '').strip(), fallback=None)
    account_id = request.args.get('account_id', type=int)
    account_mode = (request.args.get('account_mode') or '').strip().lower()
    if account_mode not in ('cash', 'bank'):
        account_mode = ''
    category = (request.args.get('category') or '').strip().lower()
    if category not in _ACCOUNT_TXN_CATEGORIES:
        category = ''
    direction = (request.args.get('direction') or '').strip().lower()
    if direction not in ('in', 'out', 'internal'):
        direction = ''
    project_id = request.args.get('project_id', type=int)
    party_name = (request.args.get('party_name') or '').strip()
    return {
        'date_from': date_from,
        'date_to': date_to,
        'account_id': (int(account_id) if account_id else None),
        'account_mode': account_mode,
        'category': category,
        'direction': direction,
        'project_id': (int(project_id) if project_id else None),
        'party_name': party_name,
    }


def _cashflow_filter_query(flt):
    """Rebuild the GET query string (without page/per_day) for links + CSV."""
    q = {
        'date_from': (flt['date_from'].isoformat() if flt['date_from'] else ''),
        'date_to': (flt['date_to'].isoformat() if flt['date_to'] else ''),
        'account_id': (str(flt['account_id']) if flt['account_id'] else ''),
        'account_mode': flt['account_mode'],
        'category': flt['category'],
        'direction': flt['direction'],
        'project_id': (str(flt['project_id']) if flt['project_id'] else ''),
        'party_name': flt['party_name'],
    }
    return {k: v for k, v in q.items() if v not in ('', None)}


def _cashflow_is_company_account(account_id):
    if not account_id:
        return False
    a = Account.query.get(int(account_id))
    return bool(a) and not a.is_void and (a.type or '').strip().lower() in _ACCOUNT_COMPANY_TYPES


def _cashflow_record_flow(form):
    """Handle the quick-entry POST; returns the recorded date, or None on failure."""
    kind = (form.get('flow_kind') or '').strip().lower()
    tx_date = _parse_date((form.get('date') or '').strip())
    amount = max(0.0, _flt(form.get('amount'), 0.0))
    note = (form.get('note') or '').strip()
    reference_id = (form.get('reference_id') or '').strip()
    project_id = _payload_int(form, 'project_id') or None
    party_name = _normalize_name_ci(form.get('party_name') or '')
    from_id = _payload_int(form, 'from_account_id') or None
    to_id = _payload_int(form, 'to_account_id') or None

    if kind not in ('in', 'out', 'transfer'):
        flash('Choose Money In, Money Out or Internal Transfer first.', 'danger')
        return False
    if not tx_date:
        flash('A valid date is required (YYYY-MM-DD).', 'danger')
        return False
    if amount <= 0:
        flash('Amount must be greater than 0.', 'danger')
        return False

    base = {
        'date': tx_date.isoformat(),
        'amount': amount,
        'note': note,
        'reference_id': reference_id,
        'project_id': project_id,
    }

    if kind == 'in':
        if not _cashflow_is_company_account(to_id):
            flash('Money In: select the company (cash/bank) account the money is received into.', 'danger')
            return False
        if not from_id:
            src = _accounts_party_account(party_name, 'person') if party_name else _accounts_default_external_parties()
            from_id = (int(src.id) if src else None)
        if not from_id:
            flash('Money In: could not resolve the source account. Enter the party name or pick an account.', 'danger')
            return False
        if from_id == to_id:
            flash('Money In: source and receiving account must be different.', 'danger')
            return False
        base.update({
            'type': 'party_receipt',
            'from_account_id': from_id,
            'to_account_id': to_id,
            'executed_by_account_id': from_id,
            'party_name': party_name,
            'category': ((form.get('category') or '').strip().lower() or 'income'),
        })
    elif kind == 'out':
        if not _cashflow_is_company_account(from_id):
            flash('Money Out: select the company (cash/bank) account paying out.', 'danger')
            return False
        if to_id and _cashflow_is_company_account(to_id):
            flash('Money Out: the destination is another company account — use Internal Transfer instead.', 'danger')
            return False
        if (not to_id) and (not party_name):
            flash('Money Out: choose a destination account or enter the party name.', 'danger')
            return False
        base.update({
            'type': 'party_payment',
            'from_account_id': from_id,
            'executed_by_account_id': from_id,
            'to_account_id': to_id,
            'party_name': party_name,
            'category': ((form.get('category') or '').strip().lower() or 'expense'),
        })
    else:  # transfer
        if not _cashflow_is_company_account(from_id) or not _cashflow_is_company_account(to_id):
            flash('Internal Transfer: both accounts must be company (cash/bank) accounts.', 'danger')
            return False
        if from_id == to_id:
            flash('Internal Transfer: source and destination must be different accounts.', 'danger')
            return False
        base.update({
            'type': 'transfer',
            'from_account_id': from_id,
            'to_account_id': to_id,
            'executed_by_account_id': from_id,
            'category': 'transfer',
        })

    ok, msg, rows = _create_accounts_transaction_with_sync(base)
    if not ok:
        flash(msg or 'Unable to record the cash flow.', 'danger')
        return None
    label = {'in': 'Money In', 'out': 'Money Out', 'transfer': 'Internal Transfer'}.get(kind, 'Flow')
    flash(f'{label} recorded: {amount:,.2f} PKR ({len(rows)} row{"s" if len(rows) != 1 else ""}).', 'success')
    return tx_date


def register(app):
    """Register Cash Flow pages (Accounts section)."""

    @app.route('/hdc/accounts/cashflow', methods=['GET', 'POST'])
    @login_required
    def hdc_cashflow():
        if _admin_only():
            return redirect(url_for('hdc_dashboard'))

        today = _pkt_today()

        if request.method == 'POST':
            action = (request.form.get('action') or '').strip().lower()
            if action == 'record_flow':
                recorded_on = _cashflow_record_flow(request.form)
                if recorded_on:
                    # Land on the recorded date so the new entry is visible.
                    return redirect(url_for('hdc_cashflow',
                                            date_from=recorded_on.isoformat(),
                                            date_to=recorded_on.isoformat()))
            return redirect(url_for('hdc_cashflow'))

        flt = _cashflow_filters()
        rows = _cashflow_load_rows(**flt)
        summary = _cashflow_summary(rows)
        day_groups = _cashflow_group_by_day(rows)

        # Pagination by day so each page always shows complete days.
        try:
            per_day = int(request.args.get('per_day', type=int) or 7)
        except Exception:
            per_day = 7
        per_day = max(1, min(per_day, 31))
        total_days = len(day_groups)
        total_pages = max(1, (total_days + per_day - 1) // per_day) if total_days else 1
        page = max(1, min(request.args.get('page', type=int) or 1, total_pages))
        page_day_groups = day_groups[(page - 1) * per_day: page * per_day]

        accounts = _list_accounts_with_balances()
        company_accounts = [a for a in accounts if a['type'] in _ACCOUNT_COMPANY_TYPES]
        non_company_accounts = [a for a in accounts if a['type'] not in _ACCOUNT_COMPANY_TYPES]
        projects_list = Project.query.order_by(Project.name.asc()).all()

        pg_query = _cashflow_filter_query(flt)
        pg_query['per_day'] = per_day

        return render_template('accounts/cashflow.html',
            today=today.isoformat(),
            summary=summary,
            page_day_groups=page_day_groups,
            accounts=accounts,
            company_accounts=company_accounts,
            non_company_accounts=non_company_accounts,
            projects=projects_list,
            txn_categories=list(_ACCOUNT_TXN_CATEGORIES),
            direction_filters=CASHFLOW_DIRECTION_LABELS,
            filter_date_from=(flt['date_from'].isoformat() if flt['date_from'] else ''),
            filter_date_to=(flt['date_to'].isoformat() if flt['date_to'] else ''),
            filter_account_id=flt['account_id'] or '',
            filter_account_mode=flt['account_mode'],
            filter_category=flt['category'],
            filter_direction=flt['direction'],
            filter_project_id=flt['project_id'] or '',
            filter_party_name=flt['party_name'],
            pg_page=page,
            pg_total_pages=total_pages,
            pg_total_items=total_days,
            pg_per_page=per_day,
            pg_endpoint='hdc_cashflow',
            pg_url_kwargs={},
            pg_query=pg_query,
            pg_label='days'
        )

    @app.route('/hdc/accounts/cashflow/report')
    @login_required
    def hdc_cashflow_report():
        if _admin_only():
            return redirect(url_for('hdc_dashboard'))

        flt = _cashflow_filters()
        rows = _cashflow_load_rows(**flt)
        summary = _cashflow_summary(rows)
        opening = _cashflow_opening_funds(flt['date_from'])
        daily = _cashflow_daily_report_rows(rows, opening_funds=opening)
        DAILY_CAP = 366
        daily_shown = daily[-DAILY_CAP:]
        daily_truncated = (len(daily) > DAILY_CAP)
        categories = _cashflow_category_breakdown(rows)
        account_rows = _cashflow_account_breakdown(rows)
        q = _cashflow_filter_query(flt)
        records_url = url_for('hdc_cashflow', **q)
        export_url = url_for('hdc_cashflow_export', **q)

        return render_template('accounts/cashflow_report.html',
            today=_pkt_today().isoformat(),
            summary=summary,
            opening=opening,
            daily=daily_shown,
            daily_truncated=daily_truncated,
            daily_cap=DAILY_CAP,
            categories=categories,
            account_rows=account_rows,
            accounts=_list_accounts_with_balances(),
            projects=Project.query.order_by(Project.name.asc()).all(),
            txn_categories=list(_ACCOUNT_TXN_CATEGORIES),
            direction_filters=CASHFLOW_DIRECTION_LABELS,
            filter_date_from=(flt['date_from'].isoformat() if flt['date_from'] else ''),
            filter_date_to=(flt['date_to'].isoformat() if flt['date_to'] else ''),
            filter_account_id=flt['account_id'] or '',
            filter_account_mode=flt['account_mode'],
            filter_category=flt['category'],
            filter_direction=flt['direction'],
            filter_project_id=flt['project_id'] or '',
            filter_party_name=flt['party_name'],
            records_url=records_url,
            export_url=export_url
        )

    @app.route('/hdc/accounts/cashflow/export')
    @login_required
    def hdc_cashflow_export():
        if _admin_only():
            return redirect(url_for('hdc_dashboard'))
        flt = _cashflow_filters()
        rows = _cashflow_load_rows(**flt, limit=20000)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([
            'Date', 'Direction', 'Type', 'Category', 'From Account', 'To Account',
            'Party', 'Project', 'Stage', 'Money In (PKR)', 'Money Out (PKR)',
            'Internal Transfer (PKR)', 'Note', 'Reference', 'Txn ID',
        ])
        for r in rows:
            w.writerow([
                r['date'].isoformat(),
                CASHFLOW_DIRECTION_LABELS.get(r['direction'], r['direction']),
                r['type_label'],
                r['category'],
                r['from_account'],
                r['to_account'],
                r['party_name'],
                r['project'],
                r['stage'],
                (f"{r['amount']:.2f}" if r['direction'] == 'in' else ''),
                (f"{r['amount']:.2f}" if r['direction'] == 'out' else ''),
                (f"{r['amount']:.2f}" if r['direction'] == 'internal' else ''),
                r['note'],
                r['reference_id'],
                r['id'],
            ])
        fname = f"cashflow_{_pkt_today().isoformat()}.csv"
        return Response(
            buf.getvalue(),
            mimetype='text/csv',
            headers={'Content-Disposition': f'attachment; filename={fname}'},
        )
