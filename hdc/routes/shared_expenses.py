"""HDC routes: Shared Expenses (part of the Accounts section).

    /hdc/accounts/shared                          the ledgers (landing page)
    /hdc/accounts/shared/expenses                 every shared expense + its split
    /hdc/accounts/shared/expenses/new             record a shared expense
    /hdc/accounts/shared/expenses/<id>            one expense, its split and links
    /hdc/accounts/shared/expenses/<id>/edit       edit it (splits, amounts, links)
    /hdc/accounts/shared/settlements              who squared up with whom
    /hdc/accounts/shared/parties                  the sharing heads (FBM, HDC, Home, …)
    /hdc/accounts/shared/report                   the clean "who shares what" report
    /hdc/accounts/shared/expenses.csv             filtered expense list as CSV
    /hdc/accounts/shared/report.csv               the report as CSV

One cost — car fuel, a utility, a workshop bill — is entered once and divided
between however many heads it belongs to (three today, four or five tomorrow).
The ledgers on the landing page are *derived* from those splits, so they can
never disagree with the expenses behind them.

Money is not created here.  Paying a shared expense calls the Cash Flow register
engine (``save_manual_cash_flow_entry``) and links the document back to this
module, or links an entry already recorded in Accounts; either way the payment
lives once, in the accounts ledger, and this module only records who it was for.

Every mutating request goes through ``hdc/services/shared_expenses.py``; these
handlers parse the request, call one service function and flash the outcome.
"""

import csv
import io
import os
import secrets

from flask import (Response, current_app, flash, redirect, render_template,
                   request, session, url_for)
from flask_login import current_user, login_required

from hdc.config import BASE_DIR
from hdc.extensions import _admin_only, db
from hdc.models.shared_expenses import SharedExpense, SharedParty, SharedSettlement
from hdc.services.shared_expenses import (
    CATEGORY_PRESETS,
    MONEY_SOURCE_LABELS,
    account_options,
    accounts_category_options,
    all_accounts,
    balance_state,
    categories_in_use,
    category_totals,
    delete_party,
    delete_shared_expense,
    default_party_ids,
    expense_filters,
    expense_query,
    filter_query_string,
    link_accounts_entry,
    linkable_entries,
    ledger_lines,
    module_summary,
    money_text,
    money_value,
    month_totals,
    party_balances,
    party_kind_label,
    party_options,
    party_statement,
    party_usage,
    post_expense_to_accounts,
    report_matrix,
    restore_shared_expense,
    restore_settlement,
    save_party,
    save_settlement,
    save_shared_expense,
    settlement_query,
    split_mode_label,
    update_party,
    void_settlement,
    void_shared_expense,
)
from hdc.utils.dates import _pkt_today

DRAFT_KEY = 'hdc_shared_expense_draft'
DRAFT_ERROR_KEY = 'hdc_shared_expense_draft_error'


def _asset_stamp():
    """A ?v= stamp for this module's own CSS / JS.

    Static files are served straight off disk (PythonAnywhere maps
    ``/hdc_static`` itself), so a browser that fetched the script last week
    keeps using it and the operator sees the old page — a split table that
    never updates, however correct the new script is.  Stamping the two files
    with their modified time makes every deploy land without asking anybody to
    hard-reload.
    """
    stamp = 0
    for rel in ('js/pages/shared_expenses.js', 'css/shared_expenses.css'):
        try:
            stamp = max(stamp, int(os.path.getmtime(os.path.join(BASE_DIR, 'static', 'hdc', rel))))
        except OSError:
            continue
    return str(stamp or 1)


# ---------------------------------------------------------------------------
# form drafts — never lose what was typed
# ---------------------------------------------------------------------------

def _stash_draft(form, *, error=None, extra_party_ids=()):
    """Remember a submitted form (values + message) for the next render."""
    data = {key: form.get(key) for key in form.keys()}
    data['party_ids'] = list(form.getlist('party_ids')) + [str(p) for p in extra_party_ids]
    session[DRAFT_KEY] = data
    if error:
        session[DRAFT_ERROR_KEY] = error


def _pop_draft():
    data = session.pop(DRAFT_KEY, None) or {}
    error = session.pop(DRAFT_ERROR_KEY, None)
    return data, error


def _draft_party_ids(values):
    ids = values.get('party_ids') or []
    if isinstance(ids, str):
        ids = [ids]
    out = []
    for raw in ids:
        try:
            out.append(int(raw))
        except (TypeError, ValueError):
            continue
    return out


# ---------------------------------------------------------------------------
# shared form context
# ---------------------------------------------------------------------------

def _party_rows(values, *, expense=None, fresh=False):
    """The split table: one row per sharing head, with what is already typed.

    Inactive heads are shown only when they already carry a share of the
    expense being edited — a head that was retired this morning must not
    silently lose a slice of last month's bill.

    ``fresh`` marks a form nobody has typed into yet (a new expense, not a
    draft being restored after a refusal).  Those start with the default heads
    already ticked, so the operator sees the split the moment the total is
    typed instead of hunting for a tick box first.
    """
    ids = _draft_party_ids(values)
    if not ids and expense is not None:
        ids = [int(s.party_id) for s in (expense.shares or [])]
    if not ids and fresh and expense is None:
        ids = default_party_ids()
    existing = {int(s.party_id): s for s in (expense.shares or [])} if expense else {}

    rows = []
    for party in party_options(active_only=False):
        pid = int(party.id)
        is_checked = pid in ids
        if not party.is_active and pid not in existing:
            continue
        amount_raw = values.get(f'amount_party_{pid}')
        percent_raw = values.get(f'percent_party_{pid}')
        share = existing.get(pid)
        rows.append({
            'party': party,
            'party_id': pid,
            'checked': is_checked,
            'amount_raw': (amount_raw if amount_raw is not None
                           else (f'{money_value(share.minor):.2f}' if share else '')),
            'percent_raw': (percent_raw if percent_raw is not None
                            else (_bp_text(share.percent_bp) if share else '')),
            'existing_minor': int(share.minor) if share else 0,
        })
    return rows


def _bp_text(bp):
    if bp in (None, ''):
        return ''
    return f'{int(bp) / 100:.2f}'


def _expense_values(expense):
    """A saved expense as form values, so edit renders from the same shape."""
    return {
        'date': (expense.date.isoformat() if expense.date else _pkt_today().isoformat()),
        'title': expense.title or '',
        'category': expense.category or '',
        'total_amount': f'{money_value(expense.total_minor):.2f}',
        'payer_party_id': (str(expense.payer_party_id) if expense.payer_party_id else ''),
        'split_mode': expense.split_mode or 'equal',
        'reference': expense.reference or '',
        'note': expense.note or '',
        'money_source': ('link' if expense.cf_entry_id else 'none'),
        'cf_entry_id': (str(expense.cf_entry_id) if expense.cf_entry_id else ''),
        'account_id': (str(expense.paid_from_account_id) if expense.paid_from_account_id else ''),
        'party_ids': [str(s.party_id) for s in (expense.shares or [])],
    }


def _form_context(values, *, expense=None, error=None, new_party_id=None, fresh=False):
    linkable = linkable_entries(limit=300)
    ctx = {
        'values': values,
        'form_error': error or '',
        'expense': expense,
        'is_edit': expense is not None,
        'party_rows': _party_rows(values, expense=expense, fresh=fresh),
        'new_party_id': new_party_id,
        'money_sources': MONEY_SOURCE_LABELS,
        'accounts': account_options(),
        'all_accounts': all_accounts(),
        'accounts_categories': accounts_category_options(),
        'linkable': linkable,
        'categories': CATEGORY_PRESETS,
        'categories_in_use': categories_in_use(include_void=True),
        'today': _pkt_today().isoformat(),
        'form_token': (values.get('idempotency_key') or secrets.token_hex(16)),
        'kind_labels': {p.id: party_kind_label(p.kind) for p in party_options(active_only=False)},
    }
    return ctx


def _read_expense_payload(form):
    return {
        'date': (form.get('date') or '').strip(),
        'title': (form.get('title') or '').strip(),
        'category': (form.get('category') or '').strip(),
        'total_amount': (form.get('total_amount') or '').strip(),
        'payer_party_id': (form.get('payer_party_id') or '').strip(),
        'split_mode': (form.get('split_mode') or '').strip(),
        'reference': (form.get('reference') or '').strip(),
        'note': (form.get('note') or '').strip(),
        'money_source': (form.get('money_source') or '').strip(),
        'account_id': (form.get('account_id') or '').strip(),
        'cf_entry_id': (form.get('cf_entry_id') or '').strip(),
        'cf_category_id': (form.get('cf_category_id') or '').strip(),
        'idempotency_key': (form.get('idempotency_key') or '').strip(),
    }


def _read_settlement_payload(form):
    return {
        'date': (form.get('date') or '').strip(),
        'from_party_id': (form.get('from_party_id') or '').strip(),
        'to_party_id': (form.get('to_party_id') or '').strip(),
        'to_account_id': (form.get('to_account_id') or '').strip(),
        'amount': (form.get('amount') or '').strip(),
        'note': (form.get('note') or '').strip(),
        'money_source': (form.get('money_source') or '').strip(),
        'from_account_id': (form.get('from_account_id') or '').strip(),
        'settlement_to_account_id': (form.get('settlement_to_account_id') or '').strip(),
    }


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------

def _csv_response(rows, header, filename):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    for row in rows:
        writer.writerow(row)
    return Response(buf.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': f'attachment; filename={filename}'})


def register(app):
    """Register the Shared Expenses pages."""

    # The ?v= stamp for this module's assets (see _asset_stamp).  Computed once
    # at start-up: a deploy restarts the process, which is exactly when the
    # stamp has to change.
    stamp = _asset_stamp()

    @app.context_processor
    def _inject_asset_stamp():
        return {'se_asset_stamp': stamp}

    # ------------------------------------------------------------------
    # the ledgers
    # ------------------------------------------------------------------
    @app.route('/hdc/accounts/shared')
    @login_required
    def hdc_shared_expenses():
        if _admin_only():
            return redirect(url_for('hdc_dashboard'))
        flt = expense_filters(request.args)
        party_id = request.args.get('party_id', type=int)
        include_void = flt.get('status') == 'all'

        balances, outside, totals = party_balances(flt=flt, include_void=include_void)
        summary = module_summary(flt=flt)
        selected_party = db.session.get(SharedParty, int(party_id)) if party_id else None
        # Links are built here, not in the template: Jinja cannot mix ``**kwargs``
        # with a plain keyword argument in one call.
        base_qs = filter_query_string(flt, exclude=('party_id',))
        party_links = {int(p.id): url_for('hdc_shared_expenses', party_id=int(p.id), **base_qs)
                       for p in party_options(active_only=False)}

        if selected_party is not None:
            statement = party_statement(int(selected_party.id), flt=flt,
                                        include_void=include_void)
            lines = []
        else:
            statement = []
            lines = ledger_lines(flt=flt, include_void=include_void)

        return render_template(
            'shared_expenses/ledger.html',
            flt=flt,
            filter_qs=filter_query_string(flt),
            base_qs=base_qs,
            party_links=party_links,
            balances=balances,
            outside=outside,
            totals=totals,
            summary=summary,
            parties=party_options(active_only=False),
            selected_party=selected_party,
            statement=statement,
            lines=lines,
            categories=categories_in_use(include_void=True),
            balance_state=balance_state,
            money_text=money_text,
        )

    # ------------------------------------------------------------------
    # expense list + CSV
    # ------------------------------------------------------------------
    @app.route('/hdc/accounts/shared/expenses')
    @login_required
    def hdc_shared_expense_list():
        if _admin_only():
            return redirect(url_for('hdc_dashboard'))
        flt = expense_filters(request.args)
        expenses = expense_query(flt).all()
        party_names = {int(p.id): p.name for p in party_options(active_only=False)}
        rows = []
        for expense in expenses:
            rows.append({
                'expense': expense,
                'shares': [{'party_id': int(s.party_id),
                            'party_name': party_names.get(int(s.party_id), '—'),
                            'amount': money_value(s.minor),
                            'percent': (int(s.percent_bp) / 100 if s.percent_bp else None)}
                           for s in (expense.shares or [])],
                'total': money_value(expense.total_minor),
            })
        summary = module_summary(flt=flt)
        return render_template(
            'shared_expenses/expenses.html',
            flt=flt,
            filter_qs=filter_query_string(flt),
            base_qs=filter_query_string(flt, exclude=('party_id',)),
            rows=rows,
            summary=summary,
            parties=party_options(active_only=False),
            categories=categories_in_use(include_void=True),
        )

    @app.route('/hdc/accounts/shared/expenses.csv')
    @login_required
    def hdc_shared_expenses_export():
        if _admin_only():
            return redirect(url_for('hdc_dashboard'))
        flt = expense_filters(request.args)
        expenses = expense_query({**flt, 'status': 'all'}).all()
        party_names = {int(p.id): p.name for p in party_options(active_only=False)}
        out = []
        for expense in expenses:
            split_txt = '; '.join(
                f'{party_names.get(int(s.party_id), "?")}: {money_value(s.minor):.2f}'
                for s in (expense.shares or []))
            out.append([
                (expense.date.isoformat() if expense.date else ''),
                expense.title or '',
                expense.category or '',
                f'{money_value(expense.total_minor):.2f}',
                (expense.payer_party.name if expense.payer_party else ''),
                split_mode_label(expense.split_mode),
                split_txt,
                (expense.paid_from_account.name if expense.paid_from_account else ''),
                ('Void' if expense.is_void else 'Active'),
                ('Linked' if expense.is_linked else 'Not in Accounts'),
                (expense.cf_entry_id or ''),
                (expense.txn_id or ''),
                expense.reference or '',
                expense.note or '',
                expense.created_by or '',
                expense.id,
            ])
        return _csv_response(
            out,
            ['Date', 'Title', 'Category', 'Total (PKR)', 'Paid By', 'Split Mode',
             'Shares', 'Paid From Account', 'Status', 'Accounts Link',
             'Cash Flow Entry ID', 'Ledger Txn ID', 'Reference', 'Note',
             'Entered By', 'Expense ID'],
            f'shared-expenses-{_pkt_today().isoformat()}.csv')

    # ------------------------------------------------------------------
    # record / edit one expense
    # ------------------------------------------------------------------
    @app.route('/hdc/accounts/shared/expenses/new', methods=['GET', 'POST'])
    @login_required
    def hdc_shared_expense_new():
        if _admin_only():
            return redirect(url_for('hdc_dashboard'))

        if request.method == 'POST':
            action = (request.form.get('action') or 'save').strip().lower()
            if action == 'add_party':
                name = (request.form.get('new_party_name') or '').strip()
                try:
                    party = save_party(name, kind='business', actor=current_user,
                                       create_account=True)
                    db.session.commit()
                    flash(f'Sharing party "{party.name}" added.', 'success')
                except ValueError as exc:
                    db.session.rollback()
                    _stash_draft(request.form, error=str(exc))
                    return redirect(url_for('hdc_shared_expense_new', restore=1))
                _stash_draft(request.form, extra_party_ids=[party.id])
                return redirect(url_for('hdc_shared_expense_new', restore=1,
                                        new_party=party.id))

            payload = _read_expense_payload(request.form)
            try:
                expense, created = save_shared_expense(
                    payload=payload, form=request.form, actor=current_user,
                    commit=True)
                if created:
                    flash(f'Shared expense #{expense.id} "{expense.title}" saved — '
                          f'{expense.party_count} party shares, '
                          f'{money_value(expense.total_minor):,.2f} PKR.', 'success')
                    if expense.cf_entry_id:
                        flash('The payment was posted to Accounts through the Cash '
                              'Flow register — it is in All Entries now.', 'info')
                    elif expense.txn_id:
                        flash('Linked to its Accounts entry.', 'info')
                    else:
                        flash('No Accounts entry yet — record the payment in Accounts '
                              'when the money leaves, then link it here.', 'warning')
                    return redirect(url_for('hdc_shared_expense_detail',
                                            expense_id=expense.id))
                flash('That form was already submitted — showing the saved expense.', 'info')
                return redirect(url_for('hdc_shared_expense_detail', expense_id=expense.id))
            except ValueError as exc:
                db.session.rollback()
                _stash_draft(request.form, error=str(exc))
                return redirect(url_for('hdc_shared_expense_new', restore=1))
            except Exception as exc:  # pragma: no cover - defensive
                db.session.rollback()
                current_app.logger.exception('shared expense create failed')
                _stash_draft(request.form, error=f'Could not save: {exc}')
                return redirect(url_for('hdc_shared_expense_new', restore=1))

        values, error = _pop_draft()
        fresh = not values
        if fresh:
            values = {'date': _pkt_today().isoformat(),
                      'split_mode': 'equal',
                      'money_source': 'post'}
        new_party_id = request.args.get('new_party', type=int)
        return render_template('shared_expenses/expense_form.html',
                               **_form_context(values, error=error,
                                               new_party_id=new_party_id,
                                               fresh=fresh))

    @app.route('/hdc/accounts/shared/expenses/<int:expense_id>')
    @login_required
    def hdc_shared_expense_detail(expense_id):
        if _admin_only():
            return redirect(url_for('hdc_dashboard'))
        expense = SharedExpense.query.get_or_404(expense_id)
        party_names = {int(p.id): p.name for p in party_options(active_only=False)}
        shares = [{'party_id': int(s.party_id),
                   'party_name': party_names.get(int(s.party_id), '—'),
                   'amount': money_value(s.minor),
                   'percent': (int(s.percent_bp) / 100 if s.percent_bp else None)}
                  for s in (expense.shares or [])]
        new_party_id = request.args.get('new_party', type=int)
        return render_template(
            'shared_expenses/expense_detail.html',
            expense=expense,
            shares=shares,
            total=money_value(expense.total_minor),
            linkable=linkable_entries(limit=300),
            accounts=account_options(),
            accounts_categories=accounts_category_options(),
            new_party_id=new_party_id,
        )

    @app.route('/hdc/accounts/shared/expenses/<int:expense_id>/edit',
               methods=['GET', 'POST'])
    @login_required
    def hdc_shared_expense_edit(expense_id):
        if _admin_only():
            return redirect(url_for('hdc_dashboard'))
        expense = SharedExpense.query.get_or_404(expense_id)
        if expense.is_void:
            flash('Voided expenses cannot be edited — restore it first.', 'warning')
            return redirect(url_for('hdc_shared_expense_detail', expense_id=expense.id))

        if request.method == 'POST':
            action = (request.form.get('action') or 'save').strip().lower()
            if action == 'add_party':
                name = (request.form.get('new_party_name') or '').strip()
                try:
                    party = save_party(name, kind='business', actor=current_user,
                                       create_account=True)
                    db.session.commit()
                    flash(f'Sharing party "{party.name}" added.', 'success')
                except ValueError as exc:
                    db.session.rollback()
                    _stash_draft(request.form, error=str(exc))
                    return redirect(url_for('hdc_shared_expense_edit',
                                            expense_id=expense.id, restore=1))
                _stash_draft(request.form, extra_party_ids=[party.id])
                return redirect(url_for('hdc_shared_expense_edit',
                                        expense_id=expense.id, restore=1,
                                        new_party=party.id))

            payload = _read_expense_payload(request.form)
            try:
                save_shared_expense(payload=payload, form=request.form,
                                    actor=current_user, expense=expense, commit=True)
                flash(f'Shared expense #{expense.id} updated.', 'success')
                return redirect(url_for('hdc_shared_expense_detail',
                                        expense_id=expense.id))
            except ValueError as exc:
                db.session.rollback()
                _stash_draft(request.form, error=str(exc))
                return redirect(url_for('hdc_shared_expense_edit',
                                        expense_id=expense.id, restore=1))
            except Exception as exc:  # pragma: no cover - defensive
                db.session.rollback()
                current_app.logger.exception('shared expense update failed')
                _stash_draft(request.form, error=f'Could not save: {exc}')
                return redirect(url_for('hdc_shared_expense_edit',
                                        expense_id=expense.id, restore=1))

        values, error = _pop_draft()
        if not values:
            values = _expense_values(expense)
        return render_template('shared_expenses/expense_form.html',
                               **_form_context(values, expense=expense, error=error,
                                               new_party_id=request.args.get('new_party',
                                                                             type=int)))

    # ------------------------------------------------------------------
    # expense actions
    # ------------------------------------------------------------------
    @app.route('/hdc/accounts/shared/expenses/<int:expense_id>/void', methods=['POST'])
    @login_required
    def hdc_shared_expense_void(expense_id):
        if _admin_only():
            return redirect(url_for('hdc_dashboard'))
        expense = SharedExpense.query.get_or_404(expense_id)
        also_entry = bool(request.form.get('void_linked_entry'))
        try:
            void_shared_expense(expense,
                                reason=(request.form.get('void_reason') or '').strip(),
                                actor=current_user, void_linked_entry=also_entry)
            flash(f'Shared expense #{expense.id} voided.', 'success')
            if also_entry and expense.cf_entry_id:
                flash('Its Accounts entry was voided too — the ledger is in step.', 'info')
            elif expense.cf_entry_id:
                flash('The Accounts entry is still posted. Void it in Accounts if the '
                      'money did not leave.', 'warning')
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
        return redirect(url_for('hdc_shared_expense_detail', expense_id=expense.id))

    @app.route('/hdc/accounts/shared/expenses/<int:expense_id>/restore', methods=['POST'])
    @login_required
    def hdc_shared_expense_restore(expense_id):
        if _admin_only():
            return redirect(url_for('hdc_dashboard'))
        expense = SharedExpense.query.get_or_404(expense_id)
        try:
            restore_shared_expense(expense, actor=current_user)
            flash(f'Shared expense #{expense.id} restored.', 'success')
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
        return redirect(url_for('hdc_shared_expense_detail', expense_id=expense.id))

    @app.route('/hdc/accounts/shared/expenses/<int:expense_id>/delete', methods=['POST'])
    @login_required
    def hdc_shared_expense_delete(expense_id):
        if _admin_only():
            return redirect(url_for('hdc_dashboard'))
        expense = SharedExpense.query.get_or_404(expense_id)
        try:
            delete_shared_expense(expense)
            flash('Shared expense deleted (it had no Accounts link).', 'success')
            return redirect(url_for('hdc_shared_expense_list'))
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
            return redirect(url_for('hdc_shared_expense_detail', expense_id=expense.id))

    @app.route('/hdc/accounts/shared/expenses/<int:expense_id>/pay', methods=['POST'])
    @login_required
    def hdc_shared_expense_pay(expense_id):
        """Pay a shared expense out of an account — posted through Accounts."""
        if _admin_only():
            return redirect(url_for('hdc_dashboard'))
        expense = SharedExpense.query.get_or_404(expense_id)
        if expense.is_void:
            flash('A voided expense cannot be paid.', 'danger')
            return redirect(url_for('hdc_shared_expense_detail', expense_id=expense.id))
        try:
            if expense.cf_entry_id:
                raise ValueError('This expense is already linked to an Accounts entry.')
            account_id = request.form.get('account_id', type=int)
            post_expense_to_accounts(
                expense, account_id=account_id,
                category_id=request.form.get('cf_category_id', type=int),
                category_name=(request.form.get('cf_category_name') or '').strip() or None,
                actor=current_user, commit=True)
            flash('Payment posted to Accounts (Cash Flow register) and linked to '
                  f'shared expense #{expense.id}.', 'success')
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
        return redirect(url_for('hdc_shared_expense_detail', expense_id=expense.id))

    @app.route('/hdc/accounts/shared/expenses/<int:expense_id>/link', methods=['POST'])
    @login_required
    def hdc_shared_expense_link(expense_id):
        """Point a shared expense at an Accounts entry recorded earlier."""
        if _admin_only():
            return redirect(url_for('hdc_dashboard'))
        expense = SharedExpense.query.get_or_404(expense_id)
        try:
            if expense.cf_entry_id:
                raise ValueError('This expense is already linked to an Accounts entry.')
            link_accounts_entry(expense,
                                cf_entry_id=request.form.get('cf_entry_id', type=int))
            db.session.commit()
            flash('Linked to its Accounts entry.', 'success')
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
        return redirect(url_for('hdc_shared_expense_detail', expense_id=expense.id))

    @app.route('/hdc/accounts/shared/expenses/<int:expense_id>/unlink', methods=['POST'])
    @login_required
    def hdc_shared_expense_unlink(expense_id):
        if _admin_only():
            return redirect(url_for('hdc_dashboard'))
        expense = SharedExpense.query.get_or_404(expense_id)
        if expense.cf_entry_id and not expense.is_void:
            entry = expense.cash_flow_entry
            if entry is not None and not entry.is_void:
                flash('Void the Accounts entry first (or void this expense with '
                      '"also void the entry"), so no payment is left unallocated.',
                      'warning')
                return redirect(url_for('hdc_shared_expense_detail',
                                        expense_id=expense.id))
        expense.cf_entry_id = None
        expense.txn_id = None
        db.session.commit()
        flash('Accounts link removed.', 'success')
        return redirect(url_for('hdc_shared_expense_detail', expense_id=expense.id))

    # ------------------------------------------------------------------
    # parties
    # ------------------------------------------------------------------
    @app.route('/hdc/accounts/shared/parties', methods=['GET', 'POST'])
    @login_required
    def hdc_shared_parties():
        if _admin_only():
            return redirect(url_for('hdc_dashboard'))

        if request.method == 'POST':
            action = (request.form.get('action') or 'create').strip().lower()
            try:
                if action == 'create':
                    party = save_party(
                        request.form.get('name'),
                        kind=request.form.get('kind') or 'business',
                        short_code=request.form.get('short_code'),
                        account_id=request.form.get('account_id', type=int),
                        is_default=bool(request.form.get('is_default')),
                        note=request.form.get('note'),
                        actor=current_user, commit=True,
                        create_account=bool(request.form.get('create_account')))
                    flash(f'Sharing party "{party.name}" added.', 'success')
                    back = (request.form.get('next') or '').strip()
                    if back.startswith('/'):
                        return redirect(back)
                elif action == 'update':
                    party = SharedParty.query.get_or_404(
                        request.form.get('party_id', type=int))
                    update_party(
                        party,
                        name=(request.form.get('name') or ''),
                        kind=request.form.get('kind'),
                        short_code=(request.form.get('short_code') or ''),
                        account_id=request.form.get('account_id', type=int),
                        is_default=bool(request.form.get('is_default')),
                        sort_order=request.form.get('sort_order'),
                        note=(request.form.get('note') or ''),
                        status=request.form.get('status'))
                    db.session.commit()
                    flash(f'"{party.name}" updated.', 'success')
                elif action == 'status':
                    party = SharedParty.query.get_or_404(
                        request.form.get('party_id', type=int))
                    update_party(party, status=request.form.get('status'))
                    db.session.commit()
                    flash(f'"{party.name}" is now {party.status}.', 'success')
                elif action == 'delete':
                    party = SharedParty.query.get_or_404(
                        request.form.get('party_id', type=int))
                    delete_party(party)
                    db.session.commit()
                    flash(f'"{party.name}" deleted.', 'success')
            except ValueError as exc:
                db.session.rollback()
                flash(str(exc), 'danger')
            return redirect(url_for('hdc_shared_parties'))

        parties = party_options(active_only=False)
        return render_template(
            'shared_expenses/parties.html',
            parties=parties,
            usage={int(p.id): party_usage(p.id) for p in parties},
            accounts=all_accounts(),
            balances={r['party_id']: r for r in party_balances()[0]},
        )

    # ------------------------------------------------------------------
    # settlements
    # ------------------------------------------------------------------
    @app.route('/hdc/accounts/shared/settlements', methods=['GET', 'POST'])
    @login_required
    def hdc_shared_settlements():
        if _admin_only():
            return redirect(url_for('hdc_dashboard'))

        if request.method == 'POST':
            action = (request.form.get('action') or 'create').strip().lower()
            try:
                if action == 'create':
                    settlement = save_settlement(
                        payload=_read_settlement_payload(request.form),
                        actor=current_user, commit=True)
                    flash(f'Settlement #{settlement.id} recorded '
                          f'({money_value(settlement.minor):,.2f} PKR).', 'success')
                elif action == 'void':
                    settlement = SharedSettlement.query.get_or_404(
                        request.form.get('settlement_id', type=int))
                    void_settlement(
                        settlement,
                        reason=(request.form.get('void_reason') or '').strip(),
                        actor=current_user,
                        void_linked_entry=bool(request.form.get('void_linked_entry')))
                    flash('Settlement voided.', 'success')
                elif action == 'restore':
                    settlement = SharedSettlement.query.get_or_404(
                        request.form.get('settlement_id', type=int))
                    restore_settlement(settlement, actor=current_user)
                    flash('Settlement restored.', 'success')
            except ValueError as exc:
                db.session.rollback()
                flash(str(exc), 'danger')
            return redirect(url_for('hdc_shared_settlements'))

        flt = expense_filters(request.args)
        show_void = (request.args.get('show_void') or '').strip().lower() in ('1', 'yes', 'true')
        settlements = settlement_query(
            status=('all' if show_void else 'active'),
            date_from=flt.get('date_from'), date_to=flt.get('date_to'),
            party_id=flt.get('party_id')).all()
        return render_template(
            'shared_expenses/settlements.html',
            settlements=settlements,
            parties=party_options(active_only=False),
            accounts=all_accounts(),
            money_accounts=account_options(),
            balances={r['party_id']: r for r in party_balances()[0]},
            flt=flt,
            filter_qs=filter_query_string(flt),
            show_void=show_void,
            today=_pkt_today().isoformat(),
        )

    # ------------------------------------------------------------------
    # the report
    # ------------------------------------------------------------------
    @app.route('/hdc/accounts/shared/report')
    @login_required
    def hdc_shared_report():
        if _admin_only():
            return redirect(url_for('hdc_dashboard'))
        flt = expense_filters(request.args)
        parties, rows, totals = report_matrix(flt=flt)
        balances, outside, balance_totals = party_balances(flt=flt)
        return render_template(
            'shared_expenses/report.html',
            flt=flt,
            filter_qs=filter_query_string(flt),
            base_qs=filter_query_string(flt, exclude=('party_id',)),
            parties=parties,
            rows=rows,
            totals=totals,
            balances=balances,
            outside=outside,
            balance_totals=balance_totals,
            category_totals=category_totals(flt=flt),
            month_totals=month_totals(flt=flt),
            summary=module_summary(flt=flt),
            categories=categories_in_use(include_void=True),
            parties_all=party_options(active_only=False),
            today=_pkt_today().isoformat(),
            balance_state=balance_state,
        )

    @app.route('/hdc/accounts/shared/report.csv')
    @login_required
    def hdc_shared_report_export():
        if _admin_only():
            return redirect(url_for('hdc_dashboard'))
        flt = expense_filters(request.args)
        parties, rows, totals = report_matrix(flt=flt)
        header = (['Date', 'Title', 'Category', 'Total (PKR)', 'Paid By', 'Accounts Link']
                  + [p.name for p in parties] + ['Split Mode', 'Reference', 'Status'])
        out = []
        for row in rows:
            out.append([
                (row['date'].isoformat() if row['date'] else ''),
                row['title'],
                row['category'],
                f"{row['total_amount']:.2f}",
                row['payer_name'],
                ('Linked' if row['linked'] else 'Not in Accounts'),
            ] + [f"{row['cell_amounts'].get(int(p.id), 0.0):.2f}" for p in parties]
              + [split_mode_label(row['split_mode']), row['reference'],
                 ('Void' if row['is_void'] else 'Active')])
        out.append(['', 'TOTAL', '', f"{totals['total_amount']:.2f}", '', '']
                   + [f"{totals['cell_amounts'].get(int(p.id), 0.0):.2f}" for p in parties]
                   + ['', '', ''])
        # The balance block: what each head still owes or is owed, same filter.
        balances, outside, _bt = party_balances(flt=flt)
        out.append([])
        out.append(['Party', 'Share (PKR)', 'Paid (PKR)', 'Settled Out (PKR)',
                    'Settled In (PKR)', 'Balance (PKR)', 'Position'])
        for row in balances:
            out.append([row['party_name'], f"{row['share_amount']:.2f}",
                        f"{row['paid_amount']:.2f}", f"{row['settled_out_amount']:.2f}",
                        f"{row['settled_in_amount']:.2f}",
                        f"{row['balance_amount']:.2f}", row['balance']['label']])
        if outside['unattributed_minor']:
            out.append([outside['party_name'], '', '', '',
                        f"{outside['unattributed_amount']:.2f}",
                        f"{outside['balance_amount']:.2f}", 'Not attributed'])
        return _csv_response(
            out, header,
            f'shared-expenses-report-{_pkt_today().isoformat()}.csv')
