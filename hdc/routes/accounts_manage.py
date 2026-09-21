"""HDC routes: Accounts hub, Manage Accounts list and the Edit Account page.

These three pages bring the AMS accounts UI
(``templates/accounts/manage_accounts.html`` / ``edit_account.html`` /
``dashboard.html``) into HDC and unify the Accounts section, which had grown
four separate sidebar entries with no landing page explaining how they relate:

  /hdc/accounts/hub          Accounts hub — what each page is for + live totals
  /hdc/accounts/manage       Manage Accounts — every account in one list
  /hdc/accounts/<id>/edit    Edit Account — details + classification hierarchy

The pre-existing pages are untouched and stay exactly where they were:

  /hdc/accounts                       transaction workspace + KPI drilldown
  /hdc/accounts/entries               all ledger entries
  /hdc/accounts/cashflow              daily money in/out report
  /hdc/accounts/cashflow/register     the Cash Flow register (primary entry)
  /hdc/accounts/cashflow/reconciliation  day close

All rules live in ``hdc/services/accounts_manage.py``; the handlers below only
parse the request, call one service function and flash the result, so the
classification / archive rules cannot be bypassed from a view.
"""

import csv
import io
from datetime import timedelta

from flask import (Response, abort, current_app, flash, redirect, render_template,
                   request, url_for)
from flask_login import current_user, login_required
from sqlalchemy import func, or_

from hdc.extensions import _admin_only, db
from hdc.models.accounts import Account, AccountTransaction
from hdc.models.cashflow import CashDayLock, CashFlowEntry
from hdc.services.accounts import _account_dashboard_kpis
from hdc.services.accounts_manage import (
    CHANNEL_LABELS,
    ENTITY_LABELS,
    SHOW_MODES,
    account_groups,
    classification_tree_json,
    create_account,
    delete_account,
    list_manage_accounts,
    manage_summary,
    restore_account,
    set_account_status,
    update_account,
)
from hdc.services.cashflow_register import (
    day_lock_state,
    day_positions,
    day_totals,
    register_rows,
    register_summary,
)
from hdc.services.money_hub import get_money_flows_grouped, get_pending_payables_detailed
from hdc.utils.dates import _pkt_today
from hdc.utils.money import from_minor

# How many recent day-locks the hub lists.
HUB_LOCK_HISTORY = 5


def _posted_txn_count(account_id):
    """Posted (non-void) transactions touching one account, on either side.

    Shown on the edit page so the user can see how much history an account
    carries before renaming or reclassifying it.
    """
    return db.session.query(func.count(AccountTransaction.id)).filter(
        AccountTransaction.is_void == False,  # noqa: E712
        or_(AccountTransaction.from_account_id == int(account_id),
            AccountTransaction.to_account_id == int(account_id))
    ).scalar() or 0


def _show_mode():
    """Sanitise the ``show`` filter (active / inactive / archived / all)."""
    mode = (request.args.get('show') or 'active').strip().lower()
    return mode if mode in SHOW_MODES else 'active'


def _include_auto_person():
    """``show_auto_person=1`` reveals the auto-created worker/party accounts."""
    return (request.args.get('show_auto_person') or '1').strip() not in ('0', 'false', 'no')


def _hub_context():
    """Numbers and state for the Accounts hub landing page.

    Deliberately cheap: balances come from the same derived query the list uses
    and the register totals cover *today* only, so the hub stays fast even on a
    large ledger.  Every block is defensive — a missing/failed sub-query must
    never take the landing page down with it.
    """
    today = _pkt_today()
    accounts = list_manage_accounts(show='active', include_auto_person=False)
    summary = manage_summary(accounts)

    # All active accounts (including auto person ledgers) drive the KPI strip,
    # so the hub's money totals agree with the Accounts workspace.
    kpis = _account_dashboard_kpis()

    # Today's register movement — the authoritative in/out for the day.
    today_summary = {'total_in': 0.0, 'total_out': 0.0, 'total_transfer': 0.0,
                     'net': 0.0, 'count': 0, 'void_count': 0}
    try:
        today_rows = register_rows(date_from=today, date_to=today, limit=None)
        today_summary = register_summary(today_rows)
    except Exception:
        pass

    # Day-close state: is today locked, and what did the last few days close at?
    lock_today = None
    lock_history = []
    try:
        lock_today = day_lock_state(today)
        lock_history = (CashDayLock.query
                        .order_by(CashDayLock.lock_date.desc())
                        .limit(HUB_LOCK_HISTORY).all())
    except Exception:
        pass

    # Yesterday's close is the one users usually still need to act on.
    yesterday = today - timedelta(days=1)
    lock_yesterday = None
    yesterday_expected = None
    try:
        lock_yesterday = day_lock_state(yesterday)
        if not lock_yesterday:
            totals = day_totals(day_positions(yesterday))
            yesterday_expected = float(from_minor(totals.get('expected_closing_minor') or 0))
    except Exception:
        pass

    try:
        entry_total = CashFlowEntry.query.count()
        txn_total = AccountTransaction.query.filter(
            AccountTransaction.is_void == False).count()  # noqa: E712
    except Exception:
        entry_total = txn_total = 0

    # Money flows for hub
    money_grouped = {}
    money_pending = {}
    try:
        money_grouped = get_money_flows_grouped()
    except Exception:
        money_grouped = {'in': [], 'out': [], 'transfer': []}
    try:
        money_pending = get_pending_payables_detailed().get('totals', {})
    except Exception:
        money_pending = {}

    # Day-close difference policy (audit 5.6 / decision 15.2): show the
    # threshold on the Hub, and flag any already-locked day whose variance
    # exceeds it so a large difference is visible, never buried.
    try:
        day_close_threshold = float(
            current_app.config.get('HDC_DAY_CLOSE_DIFFERENCE_THRESHOLD', 5000) or 0)
    except Exception:
        day_close_threshold = 5000.0
    large_difference_locks = [
        l for l in lock_history
        if day_close_threshold > 0 and abs(float(l.difference or 0)) > day_close_threshold
    ]

    return {
        'today': today.isoformat(),
        'yesterday': yesterday.isoformat(),
        'summary': summary,
        'kpis': kpis,
        'groups': account_groups(accounts),
        'today_summary': today_summary,
        'lock_today': lock_today,
        'lock_yesterday': lock_yesterday,
        'yesterday_expected': yesterday_expected,
        'lock_history': lock_history,
        'day_close_threshold': day_close_threshold,
        'large_difference_locks': large_difference_locks,
        'entry_total': entry_total,
        'txn_total': txn_total,
        'accounts': accounts,
        'money_grouped': money_grouped,
        'money_pending': money_pending,
        'money_flows_count': len(money_grouped.get('in', [])) + len(money_grouped.get('out', [])) + len(money_grouped.get('transfer', [])),
    }


def register(app):
    """Register the Accounts hub / Manage Accounts / Edit Account pages."""

    # —— Accounts hub ————————————————————————————————————————————————————————
    @app.route('/hdc/accounts/hub')
    @login_required
    def hdc_accounts_hub():
        if _admin_only():
            return redirect(url_for('hdc_dashboard'))
        return render_template('accounts/accounts_hub.html', **_hub_context())

    # —— Manage Accounts (AMS-style list of every account) ————————————————————
    @app.route('/hdc/accounts/manage', methods=['GET', 'POST'])
    @login_required
    def hdc_accounts_manage():
        if _admin_only():
            return redirect(url_for('hdc_dashboard'))

        show = _show_mode()
        include_auto = _include_auto_person()

        def _back_to_manage():
            return redirect(url_for('hdc_accounts_manage',
                                    show=show,
                                    show_auto_person=(1 if include_auto else 0)))

        if request.method == 'POST':
            action = (request.form.get('action') or '').strip().lower()
            account_id = request.form.get('account_id', type=int)
            if action == 'suspend_account':
                row, msg = set_account_status(account_id, 'inactive')
                flash(msg, 'warning' if row else 'danger')
            elif action == 'activate_account':
                row, msg = set_account_status(account_id, 'active')
                flash(msg, 'success' if row else 'danger')
            elif action == 'delete_account':
                _row, msg, archived = delete_account(account_id)
                flash(msg, 'warning' if archived else 'success')
            elif action == 'restore_account':
                row, msg = restore_account(account_id)
                flash(msg, 'success' if row else 'danger')
            elif action == 'update_account':
                row, msg = update_account(account_id, request.form.to_dict())
                flash(msg, 'success' if row else 'danger')
            else:
                flash('Unknown action.', 'danger')
            return _back_to_manage()

        # The list always shows the requested status bucket; the chips and stat
        # cards are computed over *active* accounts so the money totals are
        # never inflated by archived history.
        accounts = list_manage_accounts(show=show, include_auto_person=include_auto)
        active_accounts = accounts if show == 'active' else list_manage_accounts(
            show='active', include_auto_person=include_auto)

        return render_template('accounts/accounts_manage.html',
            accounts=accounts,
            summary=manage_summary(active_accounts),
            groups=account_groups(active_accounts),
            channel_labels=CHANNEL_LABELS,
            entity_labels=ENTITY_LABELS,
            show_mode=show,
            show_modes=SHOW_MODES,
            show_auto_person=include_auto,
            today=_pkt_today().isoformat(),
            actor=current_user.username if current_user else '')

    # —— Add Account (same form as Edit, AMS-style dedicated page) ——————————————
    @app.route('/hdc/accounts/new', methods=['GET', 'POST'])
    @login_required
    def hdc_account_new():
        if _admin_only():
            return redirect(url_for('hdc_dashboard'))

        posted = request.form.to_dict() if request.method == 'POST' else {}
        if request.method == 'POST':
            saved, msg = create_account(posted)
            flash(msg, 'success' if saved else 'danger')
            if saved:
                return redirect(request.form.get('return_to')
                                or url_for('hdc_accounts_manage'))
            # fall through and re-render with what the user typed

        return render_template('accounts/account_edit.html',
            account=None,
            form=posted,
            is_new=True,
            classification_tree=classification_tree_json(),
            channel_labels=CHANNEL_LABELS,
            entity_labels=ENTITY_LABELS,
            txn_count=0,
            is_archived=False,
            today=_pkt_today().isoformat())

    # —— Edit Account (details + classification hierarchy) ————————————————————
    @app.route('/hdc/accounts/<int:account_id>/edit', methods=['GET', 'POST'])
    @login_required
    def hdc_account_edit(account_id):
        if _admin_only():
            return redirect(url_for('hdc_dashboard'))

        row = db.session.get(Account, account_id)
        if not row:
            abort(404)

        posted = request.form.to_dict() if request.method == 'POST' else {}
        if request.method == 'POST':
            saved, msg = update_account(row.id, posted)
            flash(msg, 'success' if saved else 'danger')
            if saved:
                return redirect(request.form.get('return_to')
                                or url_for('hdc_accounts_manage'))
            # fall through and re-render with the submitted values

        # Re-read so a failed save shows what is actually stored, not stale data.
        accounts = list_manage_accounts(show='all', include_auto_person=True)
        me = next((a for a in accounts if a['id'] == int(row.id)), None)
        if me is None:
            abort(404)

        return render_template('accounts/account_edit.html',
            account=me,
            form=posted,
            is_new=False,
            classification_tree=classification_tree_json(),
            channel_labels=CHANNEL_LABELS,
            entity_labels=ENTITY_LABELS,
            txn_count=int(_posted_txn_count(row.id)),
            is_archived=(me.get('status') == 'archived'),
            today=_pkt_today().isoformat())

    # —— Manage Accounts CSV export ——————————————————————————————————————————
    @app.route('/hdc/accounts/manage/export')
    @login_required
    def hdc_accounts_manage_export():
        if _admin_only():
            return redirect(url_for('hdc_dashboard'))

        accounts = list_manage_accounts(show=_show_mode(),
                                        include_auto_person=_include_auto_person())
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(['ID', 'Account', 'Status', 'Category', 'Subcategory', 'Account Type',
                    'Channel', 'Legacy Type', 'Bank', 'Account #', 'IBAN',
                    'Opening Balance', 'Current Balance', 'Posted Txns',
                    'Linked Entity', 'Party', 'Created'])
        for a in accounts:
            # Never let the literal text "None" reach a CSV cell.  For an
            # account with no linked entity the classification label *is* the
            # string "None" (ENTITY_LABELS["none"]), which reads like a leaked
            # Python None in Excel — so write a blank instead (audit 7.5).
            linked = '' if str(a.get('linked_entity_type') or '').lower() in ('', 'none') \
                else (a.get('linked_entity_label') or '')
            w.writerow([a['id'], a['name'], a['status'], a['class_category'],
                        a['class_subcategory'], a['class_account_type'],
                        a.get('channel_label') or '',
                        a['type'], a.get('bank_name') or '', a.get('account_number') or '',
                        a.get('iban') or '',
                        f"{a['opening_balance']:.2f}", f"{a['current_balance']:.2f}",
                        a['txn_count'], linked,
                        a.get('linked_party_name') or '',
                        a['created_at'] or ''])
        fname = f"hdc-accounts-{_show_mode()}-{_pkt_today().isoformat()}.csv"
        return Response(buf.getvalue(), mimetype='text/csv',
                        headers={'Content-Disposition': f'attachment; filename="{fname}"'})
