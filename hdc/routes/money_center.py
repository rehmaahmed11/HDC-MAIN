"""HDC routes: Record Money — the Accounts section's one entry page.

``/hdc/accounts/money-center`` is where the Accounts section records money, and
it renders the *same* New Transaction form as ``/hdc/accounts/new-transaction``
and the entry card on the Cash Flow register
(``templates/hdc/accounts/_new_transaction_form.html``).  Posting goes through
the same engine as everywhere else —
``hdc.services.transaction_entry.create_entry_from_form`` →
``hdc.services.cashflow_register.save_manual_cash_flow_entry`` — so there is one
form and one posting path for every kind of transaction (money in, money out,
internal transfer), with the register's validation, balance guard, day lock,
exactly-once posting and audit trail unchanged.

Around the form the page shows the numbers an operator needs: today's movement,
company balances, and what is still to be paid or received.  Every pending row
links to the surface that *owns* that balance — a worker payable is settled on
the worker's payment page (which writes the labour ledger), a supplier's on the
supplier's page, and a project receipt comes back here with the form already
pointed at that project.  That is deliberate: a cash entry that does not settle
the payable behind it would leave the same person payable twice.

Routes:
  GET/POST /hdc/accounts/money-center          the page + the shared form's post
  GET      /hdc/accounts/money-center/api/...  JSON feeds (flows, pending, KPIs)
  GET      /hdc/api/...                        the picker feeds the APIS expose
"""

from flask import flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from hdc.extensions import _admin_only, db
from hdc.models.accounts import AccountTransaction
from hdc.services.accounts import (
    _account_intent_field_matrix,
    _create_accounts_transaction_with_sync,
)
from hdc.services.cashflow_register import (
    CF_DIRECTION_LABELS,
    category_options,
    party_options,
    register_rows,
    register_summary,
)
from hdc.services.money_hub import (
    get_all_money_flows,
    get_money_accounts,
    get_money_flow_diagram,
    get_money_flows_grouped,
    get_money_kpis,
    get_pending_payables_detailed,
    get_smooth_entry_config,
)
from hdc.services.transaction_entry import (
    clear_entry_form,
    create_entry_from_form,
    entry_form_context,
    pop_entry_form,
    stash_entry_form,
)
from hdc.utils.dates import _pkt_today
from hdc.utils.format import _parse_date, _payload_int

#: Directions the shared entry form understands (the form's own vocabulary).
ENTRY_DIRECTIONS = ('in', 'out', 'transfer')

#: Prefill numbers are shown in the form as typed, so they are only accepted
#: when they look like an amount at all (the engine does the real parsing).
_MAX_PREFILL_AMOUNT = 1_000_000_000_000


def _account_balance(account):
    """The live balance of an account row, whichever shape it arrives in.

    ``get_money_accounts()`` returns plain dicts while other callers pass model
    rows; the page treats both the same.
    """
    if isinstance(account, dict):
        return float(account.get('current_balance') or 0)
    return float(getattr(account, 'current_balance', 0) or 0)


def _money_center_context():
    """The numbers around the entry form — nothing the form itself supplies."""
    today = _pkt_today()
    accounts_info = get_money_accounts()
    pending = get_pending_payables_detailed()

    today_summary = {'total_in': 0.0, 'total_out': 0.0,
                     'total_transfer': 0.0, 'net': 0.0, 'count': 0}
    try:
        today_summary = register_summary(
            register_rows(date_from=today, date_to=today, limit=None))
    except Exception:
        pass

    recent_txns = []
    try:
        recent_txns = (AccountTransaction.query
                       .filter(AccountTransaction.is_void == False)  # noqa: E712
                       .order_by(AccountTransaction.id.desc())
                       .limit(10).all())
    except Exception:
        pass

    company_accounts = accounts_info.get('company_accounts', [])
    overdrawn_accounts = [a for a in company_accounts if _account_balance(a) < -0.01]

    return {
        'today': today.isoformat(),
        'kpis': get_money_kpis(),
        'company_accounts': company_accounts,
        'overdrawn_accounts': overdrawn_accounts,
        'pending': pending,
        'recent_txns': recent_txns,
        'today_summary': today_summary,
    }


# ---------------------------------------------------------------------------
# deep links: a pending row -> the form (or the page that settles it)
# ---------------------------------------------------------------------------

def _project_receipt_category():
    """The category that books money received for a project, if configured.

    The name is read from the database rather than hard-coded, so a renamed or
    operator-added receipt category is what the pending rows point at.
    """
    for category in category_options('in'):
        if getattr(category, 'is_project_receipt', False):
            return category
    return None


def _prefill_category_id(raw_id, raw_name, direction):
    """Resolve a deep-link's category, by id or by (case-insensitive) name."""
    wanted_id = _payload_int({'category_id': raw_id}, 'category_id')
    wanted_name = (raw_name or '').strip().casefold()
    if not wanted_id and not wanted_name:
        return 0
    for category in category_options(direction or None):
        if wanted_id and int(category.id) == wanted_id:
            return int(category.id)
        if wanted_name and (category.name or '').strip().casefold() == wanted_name:
            return int(category.id)
    return 0


def _prefill_party(raw_name):
    """The party a deep-link named, matched case-insensitively.

    Only a name the picker actually offers is returned: pre-filling a name the
    form cannot select would render a select with nothing chosen, which reads
    as "the party was forgotten" rather than "the party is not in the list yet".
    """
    wanted = (raw_name or '').strip().casefold()
    if not wanted:
        return None
    for party in party_options():
        if (party.name or '').strip().casefold() == wanted:
            return party
    return None


def _entry_prefill(args):
    """Turn a pending row's deep-link into values the entry form replays.

    Only fields the shared form understands are produced, and every id is
    resolved against the database first: a stale bookmark opens a clean form
    instead of one pointing at a row that no longer exists.
    """
    values = {}
    direction = (args.get('direction') or '').strip().lower()
    if direction in ENTRY_DIRECTIONS:
        values['direction'] = direction

    category_id = _prefill_category_id(args.get('category_id'),
                                       args.get('category'), direction)
    if category_id:
        values['category_id'] = str(category_id)

    project_id = _payload_int(args, 'project_id')
    if project_id:
        values['project_id'] = str(project_id)

    party = _prefill_party(args.get('party'))
    if party is not None:
        values['party_name'] = party.name
        values['party_type'] = (party.party_type or 'other')

    amount = (args.get('amount') or '').strip().replace(',', '')
    if amount:
        try:
            parsed = float(amount)
        except ValueError:
            parsed = 0.0
        if parsed > 0 and parsed < _MAX_PREFILL_AMOUNT:
            values['amount'] = '%.2f' % parsed

    date = (args.get('date') or '').strip()
    if date and _parse_date(date, fallback=None) is not None:
        values['date'] = date

    return values


def _pending_links(pending):
    """Attach to every pending row the link that settles it.

    Services return plain facts; building URLs is the view's job, so nothing
    else has to know that (for example) a worker payable is settled on
    ``/hdc/workers/<id>/payment``.
    """
    receipt_category = _project_receipt_category()

    for row in pending.get('workers', []):
        row['action_url'] = url_for('hdc_worker_payment', wid=row['id'])
        row['action_label'] = 'Pay'
        row['action_module'] = 'Payroll'
    for row in pending.get('suppliers', []):
        row['action_url'] = url_for('hdc_purchase_v2_supplier_detail',
                                    supplier_id=row['id'])
        row['action_label'] = 'Pay'
        row['action_module'] = 'Suppliers'
    for row in pending.get('subcontractors', []):
        row['action_url'] = url_for('hdc_subcontractor_payment_page', sid=row['id'])
        row['action_label'] = 'Pay'
        row['action_module'] = 'Subcontractor'
    for row in pending.get('office_staff', []):
        row['action_url'] = url_for('hdc_office_staff_payment', sid=row['id'])
        row['action_label'] = 'Pay'
        row['action_module'] = 'Office'
    for row in pending.get('tool_rentals_receivable', []):
        row['action_url'] = url_for('hdc_tool_rental_dashboard')
        row['action_label'] = 'Open rentals'
        row['action_module'] = 'HDC Tools'
    for row in pending.get('projects_receivable', []):
        # A project receipt belongs on this page: the entry form books it and
        # the engine mirrors it into the project's own receipts, so the
        # receivable really does come down.  Open it with the project, the
        # receipt category and the outstanding amount already filled in.
        params = {'direction': 'in', 'project_id': row['id']}
        if receipt_category is not None:
            params['category_id'] = receipt_category.id
        if float(row.get('pending') or 0) > 0:
            params['amount'] = '%.2f' % float(row['pending'])
        row['action_url'] = url_for('hdc_money_center', **params) + '#record-money-form'
        row['action_label'] = 'Receive'
        row['action_module'] = 'This form'
    return pending


def register(app):
    @app.route('/hdc/accounts/money-center', methods=['GET', 'POST'])
    @login_required
    def hdc_money_center():
        if _admin_only():
            return redirect(url_for('hdc_dashboard'))

        if request.method == 'POST':
            action = (request.form.get('action') or '').strip().lower()
            if action != 'create_entry':
                flash('Unknown action — nothing was saved.', 'danger')
                return redirect(url_for('hdc_money_center'))
            try:
                entry, created = create_entry_from_form(request.form, actor=current_user)
                db.session.commit()
            except ValueError as exc:
                # Keep the submission so the re-rendered form is not empty.
                db.session.rollback()
                stash_entry_form(request.form, str(exc))
                flash(str(exc), 'danger')
                return redirect(url_for('hdc_money_center'))
            except Exception as exc:  # pragma: no cover - defensive
                db.session.rollback()
                stash_entry_form(request.form, 'Unexpected error — your entry was not saved.')
                flash(f'Unable to save the transaction: {exc}', 'danger')
                return redirect(url_for('hdc_money_center'))

            clear_entry_form()
            if not created:
                flash('That transaction was already recorded (duplicate submission ignored).',
                      'info')
            else:
                flash(
                    f"{CF_DIRECTION_LABELS.get(entry.direction, entry.direction)} recorded: "
                    f"{entry.amount:,.2f} PKR (entry #{entry.id}).",
                    'success',
                )
            return redirect(url_for('hdc_money_center'))

        values, error = pop_entry_form()
        # A pending row's deep-link is a suggestion; a rejected submission is
        # what the user actually typed, so the draft wins.
        values = {**_entry_prefill(request.args), **(values or {})}
        context = _money_center_context()
        context['pending'] = _pending_links(context['pending'])
        context.update(entry_form_context(values or None, error or None))
        return render_template('accounts/money_center.html', **context)

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

    # Entity options APIs for pickers and module screens.
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
            staff = OfficeStaff.query.filter(OfficeStaff.active_status == True).order_by(OfficeStaff.name.asc()).limit(500).all()
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
