"""HDC routes: Cash Flow register + daily reconciliation (Accounts section).

Part of the Accounts section — admin only, like the rest of Accounts, and
sitting next to the existing ``/hdc/accounts/cashflow`` page rather than
replacing it:

  /hdc/accounts/cashflow/register        the Cash Flow register (documents)
  /hdc/accounts/cashflow/reconciliation  Daily Cash & Bank Reconciliation
  /hdc/accounts/cashflow/register/export CSV download of the filtered register

Everything mutating goes through ``hdc/services/cashflow_register.py``.  Pages
only parse the request, call one service function and flash the outcome, so
the immutability / audit / day-lock rules cannot be bypassed from a view.

The register is *immutable*: there is no edit-in-place and no delete.  A
mistake is voided (with a reason) and, if it needs fixing, replaced by a new
entry that points back at the one it amended.
"""

import csv
import io
import secrets
from datetime import timedelta

from flask import Response, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from hdc.extensions import _admin_only, db
from hdc.models.accounts import Account
from hdc.models.cashflow import CashFlowEntry
from hdc.models.projects import Project
from hdc.services.cashflow_register import (
    CF_DIRECTIONS,
    CF_DIRECTION_LABELS,
    amend_manual_cash_flow_entry,
    category_options,
    day_lock_state,
    day_positions,
    day_totals,
    lock_cash_day,
    party_options,
    register_row_dicts,
    register_rows,
    register_summary,
    restore_manual_cash_flow_entry,
    save_cf_category,
    save_cf_party,
    save_cf_subcategory,
    save_counted_position,
    save_manual_cash_flow_entry,
    unlock_cash_day,
    void_manual_cash_flow_entry,
)
from hdc.utils.dates import _pkt_now_naive, _pkt_today
from hdc.utils.format import _parse_date, _payload_int

PER_PAGE = 50


def _register_filters():
    """Read and sanitise the shared GET filter set for the register."""
    date_from = _parse_date((request.args.get('date_from') or '').strip(), fallback=None)
    date_to = _parse_date((request.args.get('date_to') or '').strip(), fallback=None)
    account_id = request.args.get('account_id', type=int)
    direction = (request.args.get('direction') or '').strip().lower()
    if direction not in CF_DIRECTIONS:
        direction = ''
    category_id = request.args.get('category_id', type=int)
    project_id = request.args.get('project_id', type=int)
    search = (request.args.get('q') or '').strip()
    show_void = (request.args.get('show_void') or '').strip().lower() in ('1', 'yes', 'true')
    return {
        'date_from': date_from,
        'date_to': date_to,
        'account_id': (int(account_id) if account_id else None),
        'direction': direction,
        'category_id': (int(category_id) if category_id else None),
        'project_id': (int(project_id) if project_id else None),
        'search': search,
        'show_void': show_void,
    }


def _register_filter_query(flt):
    q = {
        'date_from': (flt['date_from'].isoformat() if flt['date_from'] else ''),
        'date_to': (flt['date_to'].isoformat() if flt['date_to'] else ''),
        'account_id': (str(flt['account_id']) if flt['account_id'] else ''),
        'direction': flt['direction'],
        'category_id': (str(flt['category_id']) if flt['category_id'] else ''),
        'project_id': (str(flt['project_id']) if flt['project_id'] else ''),
        'q': flt['search'],
        'show_void': ('yes' if flt['show_void'] else ''),
    }
    return {k: v for k, v in q.items() if v not in ('', None)}


def _money_accounts():
    """Active treasury accounts (cash / bank / company)."""
    rows = Account.query.filter(Account.is_void == False).all()  # noqa: E712
    return [a for a in rows
            if not a.is_void
            and str(a.status or 'active').strip().lower() == 'active'
            and str(a.type or '').strip().lower() in ('company', 'cash', 'bank')]


def register(app):
    """Register the Cash Flow register + reconciliation pages."""

    @app.route('/hdc/accounts/cashflow/register', methods=['GET', 'POST'])
    @login_required
    def hdc_cashflow_register():
        if _admin_only():
            return redirect(url_for('hdc_dashboard'))

        if request.method == 'POST':
            action = (request.form.get('action') or '').strip().lower()
            try:
                if action == 'create_entry':
                    entry, created = save_manual_cash_flow_entry(
                        direction=request.form.get('direction'),
                        amount=request.form.get('amount'),
                        account_id=_payload_int(request.form, 'account_id'),
                        destination_account_id=(_payload_int(request.form, 'destination_account_id') or None),
                        category_id=(_payload_int(request.form, 'category_id') or None),
                        category_name=(request.form.get('category_name') or '').strip() or None,
                        subcategory_id=(_payload_int(request.form, 'subcategory_id') or None),
                        subcategory_name=(request.form.get('subcategory_name') or '').strip() or None,
                        party_name=(request.form.get('party_name') or '').strip() or None,
                        party_type=(request.form.get('party_type') or '').strip() or 'other',
                        description=(request.form.get('description') or '').strip() or None,
                        note=(request.form.get('note') or '').strip() or None,
                        reference=(request.form.get('reference') or '').strip() or None,
                        date_posted=_cf_posted_datetime(request.form.get('date')),
                        project_id=(_payload_int(request.form, 'project_id') or None),
                        stage_id=(_payload_int(request.form, 'stage_id') or None),
                        idempotency_key=(request.form.get('_idempotency_key') or '').strip() or None,
                        actor=current_user,
                    )
                    db.session.commit()
                    if not created:
                        flash('That entry was already recorded (duplicate submission ignored).', 'info')
                    else:
                        flash(f"{CF_DIRECTION_LABELS.get(entry.direction, entry.direction)} recorded: "
                              f"{entry.amount:,.2f} PKR (entry #{entry.id}).", 'success')

                elif action == 'amend_entry':
                    entry = CashFlowEntry.query.get(_payload_int(request.form, 'entry_id') or 0)
                    reason = (request.form.get('reason') or '').strip()
                    if not reason:
                        flash('A reason is required to amend an entry.', 'danger')
                        return redirect(url_for('hdc_cashflow_register'))
                    new_entry, old_entry = amend_manual_cash_flow_entry(
                        entry,
                        direction=(request.form.get('direction') or None),
                        amount=(request.form.get('amount') or None),
                        account_id=(_payload_int(request.form, 'account_id') or None),
                        destination_account_id=(_payload_int(request.form, 'destination_account_id') or None),
                        category_id=(_payload_int(request.form, 'category_id') or None),
                        subcategory_id=(_payload_int(request.form, 'subcategory_id') or None),
                        party_name=(request.form.get('party_name') or None),
                        description=(request.form.get('description') or None),
                        note=(request.form.get('note') or None),
                        reference=(request.form.get('reference') or None),
                        date_posted=_cf_posted_datetime(request.form.get('date')),
                        project_id=(_payload_int(request.form, 'project_id') or None),
                        stage_id=(_payload_int(request.form, 'stage_id') or None),
                        reason=reason,
                        actor=current_user,
                    )
                    db.session.commit()
                    flash(f'Entry #{old_entry.id} voided and replaced by #{new_entry.id}. '
                          f'Both records and the audit trail were kept.', 'success')

                elif action == 'void_entry':
                    entry = CashFlowEntry.query.get(_payload_int(request.form, 'entry_id') or 0)
                    reason = (request.form.get('reason') or '').strip()
                    if not reason:
                        flash('A reason is required to void an entry.', 'danger')
                        return redirect(url_for('hdc_cashflow_register'))
                    void_manual_cash_flow_entry(entry, reason=reason, actor=current_user)
                    db.session.commit()
                    flash(f'Entry #{entry.id} voided. The record and its history were retained.', 'success')

                elif action == 'restore_entry':
                    entry = CashFlowEntry.query.get(_payload_int(request.form, 'entry_id') or 0)
                    restore_manual_cash_flow_entry(entry, actor=current_user)
                    db.session.commit()
                    flash(f'Entry #{entry.id} restored.', 'success')

                elif action in ('add_category', 'add_subcategory', 'add_party'):
                    _handle_vocabulary_action(action, request.form)
                    db.session.commit()

                else:
                    flash('Unknown action.', 'danger')
            except ValueError as exc:
                db.session.rollback()
                flash(str(exc), 'danger')
            except Exception as exc:
                db.session.rollback()
                flash(f'Unable to complete the action: {exc}', 'danger')
            return redirect(url_for('hdc_cashflow_register', **_register_filter_query(_register_filters())))

        flt = _register_filters()
        all_rows = register_rows(
            date_from=flt['date_from'], date_to=flt['date_to'], account_id=flt['account_id'],
            direction=flt['direction'], category_id=flt['category_id'], project_id=flt['project_id'],
            search=flt['search'], include_void=True, limit=None,
        )
        summary = register_summary(all_rows)
        visible = all_rows if flt['show_void'] else [r for r in all_rows if not r.is_void]

        total_items = len(visible)
        total_pages = max(1, (total_items + PER_PAGE - 1) // PER_PAGE)
        page = max(1, min(request.args.get('page', type=int) or 1, total_pages))
        page_rows = visible[(page - 1) * PER_PAGE: page * PER_PAGE]

        pg_query = _register_filter_query(flt)
        return render_template(
            'accounts/cashflow_register.html',
            rows=register_row_dicts(page_rows),
            summary=summary,
            accounts=_money_accounts(),
            categories=category_options(),
            parties=party_options(),
            projects=Project.query.order_by(Project.name.asc()).all(),
            directions=[(d, CF_DIRECTION_LABELS[d]) for d in CF_DIRECTIONS],
            today=_pkt_today().isoformat(),
            filter_date_from=(flt['date_from'].isoformat() if flt['date_from'] else ''),
            filter_date_to=(flt['date_to'].isoformat() if flt['date_to'] else ''),
            filter_account_id=flt['account_id'] or '',
            filter_direction=flt['direction'],
            filter_category_id=flt['category_id'] or '',
            filter_project_id=flt['project_id'] or '',
            filter_search=flt['search'],
            show_void=flt['show_void'],
            pg_page=page,
            pg_total_pages=total_pages,
            pg_total_items=total_items,
            pg_per_page=PER_PAGE,
            pg_endpoint='hdc_cashflow_register',
            pg_url_kwargs={},
            pg_query=pg_query,
            pg_query_no_void={k: v for k, v in pg_query.items() if k != 'show_void'},
            form_token=secrets.token_hex(16),
            pg_label='entries',
        )

    @app.route('/hdc/accounts/cashflow/register/export')
    @login_required
    def hdc_cashflow_register_export():
        if _admin_only():
            return redirect(url_for('hdc_dashboard'))
        flt = _register_filters()
        rows = register_row_dicts(register_rows(
            date_from=flt['date_from'], date_to=flt['date_to'], account_id=flt['account_id'],
            direction=flt['direction'], category_id=flt['category_id'], project_id=flt['project_id'],
            search=flt['search'], include_void=True, limit=20000,
        ))
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(['Date', 'Direction', 'Category', 'Subcategory', 'Account', 'Destination',
                    'Party', 'Description', 'Reference', 'Project', 'Amount (PKR)',
                    'Status', 'Void Reason', 'Entered By', 'Entry ID', 'Ledger Txn ID'])
        for r in rows:
            w.writerow([
                (r['date'].isoformat() if r['date'] else ''),
                r['direction_label'],
                r['category'],
                r['subcategory'],
                r['account'],
                r['destination_account'],
                r['party_name'],
                r['description'],
                r['reference'],
                r['project'],
                f"{r['amount']:.2f}",
                ('Void' if r['is_void'] else 'Active'),
                r['void_reason'],
                r['created_by'],
                r['id'],
                (r['account_tx_id'] or ''),
            ])
        fname = f"cashflow_register_{_pkt_today().isoformat()}.csv"
        return Response(buf.getvalue(), mimetype='text/csv',
                        headers={'Content-Disposition': f'attachment; filename={fname}'})

    @app.route('/hdc/accounts/cashflow/reconciliation', methods=['GET', 'POST'])
    @login_required
    def hdc_cashflow_reconciliation():
        if _admin_only():
            return redirect(url_for('hdc_dashboard'))

        day = _parse_date((request.args.get('day') or request.form.get('day') or '').strip(),
                          fallback=_pkt_today())

        if request.method == 'POST':
            action = (request.form.get('action') or '').strip().lower()
            try:
                if action == 'save_counted':
                    for account_id in request.form.getlist('account_id'):
                        raw = (request.form.get(f'counted_{account_id}') or '').strip()
                        save_counted_position(day, int(account_id), raw, actor=current_user, commit=False)
                    db.session.commit()
                    flash('Counted balances saved.', 'success')

                elif action == 'lock_day':
                    lock = lock_cash_day(day, actor=current_user,
                                         note=(request.form.get('note') or '').strip() or None)
                    flash(f'Day {day.isoformat()} verified and locked '
                          f'(difference {lock.difference:,.2f} PKR). Counted balances now carry forward.',
                          'success')

                elif action == 'unlock_day':
                    unlock_cash_day(day)
                    flash(f'Day {day.isoformat()} re-opened. Reconciliation snapshots were kept.', 'warning')

                elif action == 'reconcile_account':
                    from hdc.services.cashflow_register import reconcile_account
                    raw = (request.form.get('counted') or '').strip()
                    rec = reconcile_account(
                        _payload_int(request.form, 'account_id'), day,
                        counted=(raw or None),
                        note=(request.form.get('note') or '').strip() or None,
                        actor=current_user,
                    )
                    flash(f'{rec.account.name if rec.account else "Account"} reconciled on '
                          f'{day.isoformat()} — difference {rec.difference:,.2f} PKR '
                          f'({rec.difference_type}).', 'success')

                else:
                    flash('Unknown action.', 'danger')
            except ValueError as exc:
                db.session.rollback()
                flash(str(exc), 'danger')
            except Exception as exc:
                db.session.rollback()
                flash(f'Unable to complete the action: {exc}', 'danger')
            return redirect(url_for('hdc_cashflow_reconciliation', day=day.isoformat()))

        positions = day_positions(day)
        db.session.commit()
        totals = day_totals(positions)
        lock = day_lock_state(day)

        # Day register rows so the counted figure can be checked against them.
        day_entries = register_row_dicts(register_rows(date_from=day, date_to=day, limit=None))

        return render_template(
            'accounts/cashflow_reconciliation.html',
            day=day,
            day_iso=day.isoformat(),
            positions=positions,
            totals=totals,
            lock=lock,
            locked=lock is not None,
            day_entries=day_entries,
            prev_day=(day - timedelta(days=1)).isoformat(),
            next_day=(day + timedelta(days=1)).isoformat(),
            today=_pkt_today().isoformat(),
        )


def _cf_posted_datetime(raw_date):
    """Build a PKT datetime from a ``YYYY-MM-DD`` form field (time = now)."""
    d = _parse_date((raw_date or '').strip(), fallback=None)
    if d is None:
        return None
    now = _pkt_now_naive()
    return now.replace(year=d.year, month=d.month, day=d.day)


def _handle_vocabulary_action(action, form):
    """Create a category / subcategory / party from the register form."""
    if action == 'add_category':
        row, created = save_cf_category(
            (form.get('name') or '').strip(),
            direction=(form.get('direction') or 'both').strip(),
        )
        flash(('Category added.' if created else 'That category already exists.'),
              'success' if created else 'info')
    elif action == 'add_subcategory':
        row, created = save_cf_subcategory(
            _payload_int(form, 'category_id'),
            (form.get('name') or '').strip(),
        )
        flash(('Subcategory added.' if created else 'That subcategory already exists.'),
              'success' if created else 'info')
    else:  # add_party
        row, created = save_cf_party(
            (form.get('name') or '').strip(),
            party_type=(form.get('party_type') or 'other').strip(),
            phone=(form.get('phone') or '').strip() or None,
        )
        flash(('Party added.' if created else 'That party already exists.'),
              'success' if created else 'info')
    return row
