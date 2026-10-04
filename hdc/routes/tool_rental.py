"""HDC Tool Rental routes - dashboard, hub, inventory, rentals, returns, transfers, tracking, reports.
Now includes Accounts integration: payment receiving in Cash/Bank accounts.
"""

from datetime import datetime
from flask import flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func

from hdc.extensions import _money_write_required, db
from hdc.models.accounts import Account
from hdc.models.projects import Project, Stage
from hdc.models.tool_rental import (
    TOOL_SCRAP_REASONS, Tool, ToolCategory, ToolMovementLog, ToolPurchase, ToolRental,
    ToolRentalItem, ToolRentalPayment, ToolRentalReturn, ToolRentalReturnItem,
    ToolRentalTransfer, ToolRentalTransferItem
)
from hdc.services.cashflow_register import ensure_party
from hdc.services.tool_rental import (
    _ensure_tool_category, _next_rental_code, _next_tool_code,
    _parse_date, create_movement_log, get_rental_tracking_chain,
    get_receiving_accounts, global_tool_locations,
    known_tool_customers, known_tool_suppliers,
    post_tool_rental_payment_to_accounts, recalc_rental_totals,
    record_tool_purchase, record_tool_scrap, search_rentals,
    tool_available_for_integrity, tool_kpis, tool_purchases, tool_scraps, tool_stock_aggregates,
    void_tool_rental_payment_in_accounts
)
from hdc.services.tool_tracking import (
    LOC_CUSTOMER, LOC_OWN_PROJECT, LOC_STORE, WAREHOUSE_LABEL,
    allocate_transfer_qty, dashboard_summary, inventory_rows, location_summary,
    record_transfer_items, tool_item_summary, tool_ledger, tool_position
)
from hdc.utils.dates import _pkt_now_naive, _pkt_today
from hdc.utils.format import _flt, _amount_to_words
from hdc.services.receipts import _receipt_company_profile
from hdc.services.record_permissions import integrity_message
from hdc.services.timekeeping import _has_recent_duplicate
from hdc.services.permissions import may_access_path


#: How many rentals the create page lists *below* the form.  The newest are
#: always on top, so a rental the operator just made is the first row and the
#: page never grows into a second hub — the full searchable list stays on the
#: Rentals page.
RECENT_RENTALS_ON_NEW = 10


def register(app):

    # ------------------ TOOLS DASHBOARD (simple stock & rent summary) ------------------
    @app.route('/hdc/tool-rental/dashboard')
    @login_required
    def hdc_tool_rental_dashboard():
        """The simple Tools answers: how many tools we own **item by item**,
        how many are in the store, how many are rented out, and how much rent
        customers have still not paid.

        Movement chains, per-location positions and audit warnings are not
        repeated here — they live on the Tracking / Rentals pages (see
        `hdc.services.tool_tracking.tool_item_summary`).
        """
        summary = tool_item_summary()

        return render_template('tool_rental/tool_dashboard.html',
            summary=summary,
            totals=summary['totals'],
            today=_pkt_today().isoformat(),
        )

    # ------------------ ONE TOOL: WHERE IS EVERY PIECE ------------------
    @app.route('/hdc/tool-rental/tool/<int:tool_id>')
    @login_required
    def hdc_tool_rental_tool_position(tool_id):
        position = tool_position(tool_id)
        if not position:
            flash('Tool not found.', 'danger')
            return redirect(url_for('hdc_tool_rental_dashboard'))
        row = position['row']
        tool = row['tool']
        return render_template('tool_rental/tool_position.html',
            row=row,
            movements=position['movements'],
            recon={
                'owned': row['owned_qty'], 'in_store': row['in_store_qty'],
                'own_project': row['own_project_qty'], 'customer': row['customer_qty'],
                'total_sent': row['own_project_qty'] + row['customer_qty'],
                'accounted': row['in_store_qty'] + row['out_qty'],
                'variance': row['variance'], 'balanced': not row['unaccounted'],
            },
            purchases=tool_purchases(tool_id=tool.id, limit=100),
            scraps=tool_scraps(tool_id=tool.id, limit=100),
            scrap_reasons=TOOL_SCRAP_REASONS,
            known_suppliers=known_tool_suppliers(),
            loc_store=LOC_STORE, loc_own=LOC_OWN_PROJECT, loc_customer=LOC_CUSTOMER,
            warehouse_label=WAREHOUSE_LABEL,
            today=_pkt_today().isoformat(),
        )

    # ------------------ MAIN RENTALS HUB ------------------
    @app.route('/hdc/tool-rental')
    @login_required
    def hdc_tool_rental():
        # Old bookmarks/links that opened the create form on this page are
        # sent to the form's own page now.
        if (request.args.get('create') or '').strip() == '1':
            return redirect(url_for('hdc_tool_rental_new'))

        kpis = tool_kpis()
        # The hub is a simple list: search by code/customer and filter by
        # status.  Everything else (site, tool, payment, billing, dates) and
        # the admin reconciliation panel are deliberately not here.
        filters = {
            'status': (request.args.get('status') or '').strip() or None,
            'search_text': (request.args.get('q') or '').strip() or None,
        }
        rentals = search_rentals(filters)
        # Only users who may create rentals see the "New Rental" action.
        role = (current_user.role or '').strip().lower()
        write_access = may_access_path(current_user, '/hdc/tool-rental/new', 'write')
        can_manage_tools = (write_access if write_access is not None else
                            role in ('admin', 'accountant'))

        return render_template('tool_rental/tool_rental.html',
            kpis=kpis,
            can_manage_tools=can_manage_tools,
            rentals=rentals,
            filters=filters,
        )

    # ------------------ INVENTORY ------------------
    @app.route('/hdc/tool-rental/inventory', methods=['GET','POST'])
    @login_required
    @_money_write_required()
    def hdc_tool_rental_inventory():
        if request.method == 'POST':
            action = (request.form.get('action') or 'add').strip()

            # ---- new category (standalone) ----
            if action == 'add_category':
                name = (request.form.get('category_name') or '').strip()
                if not name:
                    flash('Category name required.', 'danger')
                    return redirect(url_for('hdc_tool_rental_inventory'))
                before = ToolCategory.query.filter(
                    func.lower(ToolCategory.name) == name.lower()).first()
                cat = _ensure_tool_category(name)
                db.session.commit()
                if before:
                    flash(f'Category "{cat.name}" already exists — selected for you.', 'info')
                else:
                    flash(f'Category "{cat.name}" created. Now add a tool to it.', 'success')
                # keep the user in the flow: land back on the add-tool form with
                # the new category already chosen
                return redirect(url_for('hdc_tool_rental_inventory',
                                        new_category=cat.id, focus='tool'))

            # ---- new tool (optionally with its opening purchase) ----
            if action in ('add_tool', 'add', ''):
                name = (request.form.get('name') or '').strip()
                if not name:
                    flash('Tool name required.', 'danger')
                    return redirect(url_for('hdc_tool_rental_inventory'))

                category_id = request.form.get('category_id', type=int)
                new_category = (request.form.get('new_category') or '').strip()
                if not category_id and new_category:
                    cat = _ensure_tool_category(new_category)
                    category_id = cat.id if cat else None

                code = (request.form.get('tool_code') or '').strip().upper() or _next_tool_code()
                if Tool.query.filter_by(tool_code=code).first():
                    code = _next_tool_code()
                total_qty = max(0.0, _flt(request.form.get('total_quantity')))
                purchase_cost = max(0.0, _flt(request.form.get('purchase_cost')))
                rate = max(0.0, _flt(request.form.get('rental_rate_per_day')))
                unit = (request.form.get('unit') or 'pcs').strip() or 'pcs'
                condition = (request.form.get('condition') or 'good').strip()
                description = (request.form.get('description') or '').strip()

                tool = Tool(
                    tool_code=code,
                    name=name,
                    category_id=category_id,
                    description=description,
                    unit=unit,
                    total_quantity=0.0,
                    purchase_cost=purchase_cost,
                    rental_rate_per_day=rate,
                    condition=condition,
                    status='active',
                    is_void=False
                )
                db.session.add(tool)
                db.session.flush()

                # opening stock is a real purchase: it keeps "why do we own 8?"
                # answerable from the stock register instead of a magic number
                purchase = None
                if total_qty > 0:
                    ok_pur, msg_pur, purchase = record_tool_purchase(
                        tool_id=tool.id,
                        qty=total_qty,
                        unit_cost=purchase_cost,
                        supplier=(request.form.get('supplier') or '').strip(),
                        purchase_date=(request.form.get('purchase_date') or '').strip() or None,
                        reference=(request.form.get('purchase_reference') or '').strip(),
                        notes=(request.form.get('purchase_notes') or '').strip() or 'Opening stock',
                        is_opening_stock=True,
                        created_by=current_user.id if hasattr(current_user, 'id') else None,
                        commit=False,
                    )
                    if not ok_pur:
                        db.session.rollback()
                        flash(msg_pur or 'Could not record opening stock.', 'danger')
                        return redirect(url_for('hdc_tool_rental_inventory'))

                db.session.commit()
                if purchase is not None:
                    flash(f'Tool {tool.name} ({code}) added with {total_qty:g} {unit} '
                          f'opening stock ({purchase.purchase_code}).', 'success')
                else:
                    flash(f'Tool {tool.name} ({code}) added with 0 qty — '
                          f'use "Add Stock" when it arrives.', 'success')
                return redirect(url_for('hdc_tool_rental_inventory',
                                        q=tool.tool_code, focus='tool'))

            flash('Unknown inventory action.', 'warning')
            return redirect(url_for('hdc_tool_rental_inventory'))

        # ------------------------------ LIST ------------------------------
        q = (request.args.get('q') or '').strip()
        category_id = request.args.get('category_id', type=int)
        view = (request.args.get('view') or 'all').strip().lower()
        if view not in ('all', 'out', 'store', 'attention'):
            view = 'all'
        new_category_id = request.args.get('new_category', type=int)

        # One ledger, one set of numbers: the inventory page can never disagree
        # with the Tools dashboard about what is in store / out / on a site.
        tools = inventory_rows(term=q, category_id=category_id, view=view)
        stock_stats = tool_stock_aggregates([int(r['tool_id']) for r in tools])
        categories = (ToolCategory.query
                      .order_by(ToolCategory.name.asc()).all())
        category_tool_counts = dict(
            (int(cid), int(cnt)) for cid, cnt in db.session.query(
                Tool.category_id, func.count(Tool.id)).group_by(Tool.category_id).all()
            if cid is not None)
        kpis = tool_kpis()
        return render_template('tool_rental/tool_inventory.html',
            tools=tools,
            stock_stats=stock_stats,
            categories=categories,
            category_tool_counts=category_tool_counts,
            kpis=kpis,
            q=q,
            selected_category=category_id,
            selected_view=view,
            new_category=new_category_id,
            scrap_reasons=TOOL_SCRAP_REASONS,
            known_suppliers=known_tool_suppliers(),
            today=_pkt_today().isoformat(),
            focus=(request.args.get('focus') or '').strip(),
        )

    # ------------------ EDIT / ARCHIVE A TOOL ------------------
    @app.route('/hdc/tool-rental/inventory/<int:tool_id>/edit', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_tool_rental_tool_edit(tool_id):
        tool = Tool.query.get_or_404(tool_id)
        name = (request.form.get('name') or tool.name).strip()
        code = (request.form.get('tool_code') or tool.tool_code).strip().upper()
        if not name:
            flash('Tool name required.', 'danger')
            return redirect(url_for('hdc_tool_rental_inventory'))
        exists = Tool.query.filter(Tool.id!=tool.id, func.lower(Tool.tool_code)==code.lower()).first()
        if exists:
            flash('Another tool already uses this code.', 'danger')
            return redirect(url_for('hdc_tool_rental_inventory'))
        old_qty = float(tool.total_quantity or 0)
        new_qty = max(0.0, _flt(request.form.get('total_quantity'), old_qty))
        diff = new_qty - old_qty

        tool.name = name
        tool.tool_code = code
        tool.category_id = request.form.get('category_id', type=int)
        tool.unit = (request.form.get('unit') or tool.unit).strip()
        tool.total_quantity = new_qty
        tool.purchase_cost = max(0.0, _flt(request.form.get('purchase_cost'), tool.purchase_cost))
        tool.rental_rate_per_day = max(0.0, _flt(request.form.get('rental_rate_per_day'), tool.rental_rate_per_day))
        tool.condition = (request.form.get('condition') or tool.condition).strip()
        tool.status = (request.form.get('status') or tool.status).strip()
        tool.description = (request.form.get('description') or tool.description).strip()
        tool.updated_at = _pkt_now_naive()

        # A hand-typed qty is a stock adjustment, not a purchase or a scrap, so
        # it is logged as one and the dashboard can still reconcile.
        if abs(diff) > 0.001:
            create_movement_log(
                tool_id=tool.id,
                rental_id=None,
                movement_type='adjustment',
                from_label='Warehouse / Store',
                to_label='Warehouse / Store',
                qty=diff,
                notes=f'Stock adjusted from {old_qty} to {new_qty}'
            )
        db.session.commit()
        flash(f'Tool {tool.name} updated.', 'success')
        return redirect(url_for('hdc_tool_rental_inventory', q=tool.tool_code))

    @app.route('/hdc/tool-rental/inventory/<int:tool_id>/delete', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_tool_rental_tool_delete(tool_id):
        tool = Tool.query.get_or_404(tool_id)
        pending = db.session.query(func.coalesce(func.sum(ToolRentalItem.qty_pending),0.0)).filter(ToolRentalItem.tool_id==tool.id).scalar() or 0.0
        if float(pending) > 0.001:
            flash(f'Cannot archive {tool.name}: {pending:g} qty still rented out.', 'danger')
            return redirect(url_for('hdc_tool_rental_inventory', q=tool.tool_code))
        tool.is_void = True
        tool.status = 'retired'
        db.session.commit()
        flash(f'Tool {tool.name} archived.', 'success')
        return redirect(url_for('hdc_tool_rental_inventory'))

    # ------------------ PURCHASE / STOCK IN ------------------
    def _do_tool_purchase(tool_id):
        """Buy more of a tool we already own: +qty, audit row, movement log.

        Shared by the per-tool route and the generic "Purchase Stock" modal so
        both behave identically.
        """
        tool = Tool.query.get_or_404(tool_id)
        qty = max(0.0, _flt(request.form.get('qty')))
        if qty <= 0:
            flash('Purchase quantity must be greater than 0.', 'danger')
            return redirect(url_for('hdc_tool_rental_inventory', q=tool.tool_code))
        purchase_date = _parse_date(request.form.get('purchase_date'))
        if _has_recent_duplicate(ToolPurchase, tool_id=tool.id, qty=qty,
                                 purchase_date=purchase_date):
            flash('Duplicate purchase prevented (same tool + qty + date submitted twice).', 'warning')
            return redirect(url_for('hdc_tool_rental_inventory', q=tool.tool_code))

        ok, msg, purchase = record_tool_purchase(
            tool_id=tool.id,
            qty=qty,
            unit_cost=_flt(request.form.get('unit_cost'), tool.purchase_cost),
            supplier=(request.form.get('supplier') or '').strip(),
            purchase_date=(request.form.get('purchase_date') or '').strip() or None,
            reference=(request.form.get('reference') or '').strip(),
            notes=(request.form.get('notes') or '').strip(),
            update_cost=(request.form.get('update_cost') == '1'),
            created_by=current_user.id if hasattr(current_user, 'id') else None,
        )
        if not ok:
            flash(msg or 'Could not record purchase.', 'danger')
            return redirect(url_for('hdc_tool_rental_inventory', q=tool.tool_code))
        flash(f'Stock in: {qty:g} {tool.unit} of {tool.name} ({purchase.purchase_code}) — '
              f'now owning {tool.total_quantity:g} {tool.unit}.', 'success')
        return redirect(url_for('hdc_tool_rental_inventory', q=tool.tool_code))

    @app.route('/hdc/tool-rental/inventory/purchase', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_tool_rental_purchase_create():
        tool_id = request.form.get('tool_id', type=int)
        if not tool_id:
            flash('Select the tool you purchased.', 'danger')
            return redirect(url_for('hdc_tool_rental_inventory'))
        return _do_tool_purchase(tool_id)

    @app.route('/hdc/tool-rental/inventory/<int:tool_id>/purchase', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_tool_rental_tool_purchase(tool_id):
        return _do_tool_purchase(tool_id)

    # ------------------ SCRAP / DISCARD ------------------
    def _do_tool_scrap(tool_id):
        """Throw away broken / lost tools: -qty, audit row, movement log."""
        tool = Tool.query.get_or_404(tool_id)
        qty = max(0.0, _flt(request.form.get('qty')))
        if qty <= 0:
            flash('Scrap quantity must be greater than 0.', 'danger')
            return redirect(url_for('hdc_tool_rental_inventory', q=tool.tool_code))

        ok, msg, scrap = record_tool_scrap(
            tool_id=tool.id,
            qty=qty,
            reason=(request.form.get('reason') or 'damaged').strip(),
            scrap_date=(request.form.get('scrap_date') or '').strip() or None,
            reference=(request.form.get('reference') or '').strip(),
            notes=(request.form.get('notes') or '').strip(),
            created_by=current_user.id if hasattr(current_user, 'id') else None,
        )
        if not ok:
            flash(msg or 'Could not record scrap.', 'danger')
            return redirect(url_for('hdc_tool_rental_inventory', q=tool.tool_code))
        flash(f'Scrapped {qty:g} {tool.unit} of {tool.name} ({scrap.scrap_code}, '
              f'{scrap.reason_label}) — written off {scrap.value_written_off:,.0f}. '
              f'{tool.total_quantity:g} {tool.unit} left.', 'warning')
        return redirect(url_for('hdc_tool_rental_inventory', q=tool.tool_code))

    @app.route('/hdc/tool-rental/inventory/scrap', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_tool_rental_scrap_create():
        tool_id = request.form.get('tool_id', type=int)
        if not tool_id:
            flash('Select the tool you are scrapping.', 'danger')
            return redirect(url_for('hdc_tool_rental_inventory'))
        return _do_tool_scrap(tool_id)

    @app.route('/hdc/tool-rental/inventory/<int:tool_id>/scrap', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_tool_rental_tool_scrap(tool_id):
        return _do_tool_scrap(tool_id)

    # ------------------ CATEGORY: RENAME / DELETE ------------------
    @app.route('/hdc/tool-rental/category/<int:category_id>/edit', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_tool_rental_category_edit(category_id):
        cat = ToolCategory.query.get_or_404(category_id)
        name = (request.form.get('name') or '').strip()
        if not name:
            flash('Category name required.', 'danger')
            return redirect(url_for('hdc_tool_rental_inventory'))
        clash = ToolCategory.query.filter(func.lower(ToolCategory.name) == name.lower(),
                                          ToolCategory.id != cat.id).first()
        if clash:
            flash(f'Another category is already called "{name}".', 'danger')
            return redirect(url_for('hdc_tool_rental_inventory'))
        cat.name = name
        cat.description = (request.form.get('description') or cat.description or '').strip()
        db.session.commit()
        flash(f'Category renamed to "{cat.name}".', 'success')
        return redirect(url_for('hdc_tool_rental_inventory', category_id=cat.id))

    @app.route('/hdc/tool-rental/category/<int:category_id>/delete', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_tool_rental_category_delete(category_id):
        cat = ToolCategory.query.get_or_404(category_id)
        used = db.session.query(func.count(Tool.id)).filter(Tool.category_id == cat.id).scalar() or 0
        if int(used) > 0:
            flash(f'Cannot delete "{cat.name}": {int(used)} tool(s) still use it. '
                  f'Move them to another category first.', 'danger')
            return redirect(url_for('hdc_tool_rental_inventory'))
        name = cat.name
        db.session.delete(cat)
        db.session.commit()
        flash(f'Category "{name}" deleted.', 'success')
        return redirect(url_for('hdc_tool_rental_inventory'))

    # ------------------ NEW RENTAL (its own page) ------------------
    @app.route('/hdc/tool-rental/new')
    @login_required
    def hdc_tool_rental_new():
        """The rental creation form lives on its own page.

        The Rentals hub keeps the summary, the search and the list; its
        "New Rental" action opens this form.  Only users who may write the
        tools page can open it — the same users who could ever see the form.
        """
        role = (current_user.role or '').strip().lower()
        write_access = may_access_path(current_user, '/hdc/tool-rental/new', 'write')
        can_manage_tools = (write_access if write_access is not None else
                            role in ('admin', 'accountant'))
        if not can_manage_tools:
            flash('You do not have permission to create rentals.', 'danger')
            return redirect(url_for('hdc_tool_rental'))

        # Landing back here after a create (or a rejected create) keeps the
        # operator in one place: the form up top, the rentals they just made
        # *below* it.  ``created`` highlights the fresh rental so "where did my
        # rental go?" is answered on the same screen instead of a jump away.
        created_rental_id = request.args.get('created', type=int)
        created_rental = db.session.get(ToolRental, created_rental_id) if created_rental_id else None
        recent_rentals = search_rentals({})[:RECENT_RENTALS_ON_NEW]

        return render_template('tool_rental/tool_new_rental.html',
            projects=Project.query.order_by(Project.name.asc()).all(),
            stages=Stage.query.order_by(Stage.name.asc()).all(),
            tools=Tool.query.filter(Tool.is_void==False).order_by(Tool.name.asc()).all(),
            known_customers=known_tool_customers(),
            recent_rentals=recent_rentals,
            created_rental=created_rental,
            today=_pkt_today().isoformat(),
        )

    # ------------------ CREATE RENTAL ------------------
    @app.route('/hdc/tool-rental/create', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_tool_rental_create():
        renter_type = (request.form.get('renter_type') or 'internal').strip().lower()
        if renter_type not in ('internal','external'):
            renter_type = 'internal'
        project_id = request.form.get('project_id', type=int) if renter_type=='internal' else None
        stage_id = request.form.get('stage_id', type=int) if renter_type=='internal' else None
        customer_name = (request.form.get('customer_name') or '').strip() if renter_type=='external' else None
        customer_phone = (request.form.get('customer_phone') or '').strip() if renter_type=='external' else None
        customer_address = (request.form.get('customer_address') or '').strip() if renter_type=='external' else None

        if renter_type=='internal' and not project_id:
            flash('Select a project/site for internal rental.', 'danger')
            return redirect(url_for('hdc_tool_rental_new'))
        if renter_type=='external' and not customer_name:
            flash('Customer name required for external rental.', 'danger')
            return redirect(url_for('hdc_tool_rental_new'))
        if renter_type == 'internal' and stage_id:
            stage = db.session.get(Stage, stage_id)
            if not stage or stage.project_id != project_id:
                flash('Selected stage does not belong to the selected project/site.', 'danger')
                return redirect(url_for('hdc_tool_rental_new'))

        billing_type = (request.form.get('billing_type') or 'fixed_fee').strip().lower()
        if billing_type not in ('no_charge','fixed_fee','per_day','per_hour'):
            billing_type = 'fixed_fee'

        rental_date_raw = (request.form.get('rental_date') or '').strip()
        try:
            rental_date = datetime.strptime(rental_date_raw, '%Y-%m-%d').date() if rental_date_raw else _pkt_today()
        except:
            rental_date = _pkt_today()
        expected_raw = (request.form.get('expected_return_date') or '').strip()
        try:
            expected_date = datetime.strptime(expected_raw, '%Y-%m-%d').date() if expected_raw else None
        except:
            expected_date = None

        tool_ids = request.form.getlist('tool_id[]') or request.form.getlist('tool_id')
        qtys = request.form.getlist('qty[]') or request.form.getlist('qty')
        rates = request.form.getlist('rate[]') or request.form.getlist('rate')
        notes_list = request.form.getlist('item_notes[]') or request.form.getlist('item_notes')

        if not tool_ids or len(tool_ids)!=len(qtys):
            flash('Add at least one tool item.', 'danger')
            return redirect(url_for('hdc_tool_rental_new'))

        parsed_items = []
        requested_by_tool = {}
        total_rented_qty = 0.0
        total_amount = 0.0
        for idx, (tid_raw, qty_raw) in enumerate(zip(tool_ids, qtys)):
            try:
                tid = int(tid_raw)
            except:
                flash(f'Invalid tool at row {idx+1}.', 'danger')
                return redirect(url_for('hdc_tool_rental_new'))
            tool = Tool.query.get(tid)
            if not tool or tool.is_void:
                flash(f'Tool not found at row {idx+1}.', 'danger')
                return redirect(url_for('hdc_tool_rental_new'))
            qty = max(0.0, _flt(qty_raw))
            if qty <= 0:
                flash(f'Quantity must be >0 at row {idx+1}.', 'danger')
                return redirect(url_for('hdc_tool_rental_new'))
            available = tool_available_for_integrity(tool)
            requested_by_tool[tid] = requested_by_tool.get(tid, 0) + qty
            if requested_by_tool[tid] > available + 0.001:
                flash(integrity_message('Not enough available tool stock.', f'Not enough stock for {tool.name}: available {available}, requested {qty}.'), 'danger')
                return redirect(url_for('hdc_tool_rental_new'))
            rate_raw = rates[idx] if idx < len(rates) else tool.rental_rate_per_day
            rate = max(0.0, _flt(rate_raw, tool.rental_rate_per_day))
            if billing_type == 'no_charge':
                rate = 0.0
            amount = qty * rate
            note = (notes_list[idx] if idx < len(notes_list) else '').strip()
            parsed_items.append((tool, qty, rate, amount, note))
            total_rented_qty += qty
            total_amount += amount

        rental_code = _next_rental_code()
        rental = ToolRental(
            rental_code=rental_code,
            renter_type=renter_type,
            project_id=project_id,
            stage_id=stage_id,
            customer_name=customer_name,
            customer_phone=customer_phone,
            customer_address=customer_address,
            rental_date=rental_date,
            expected_return_date=expected_date,
            billing_type=billing_type,
            billing_notes=(request.form.get('billing_notes') or '').strip(),
            total_rented_qty=total_rented_qty,
            total_amount=total_amount if billing_type!='no_charge' else 0.0,
            total_paid=0.0,
            total_returned_qty=0.0,
            status='active',
            payment_status='no_charge' if billing_type=='no_charge' else 'unpaid',
            notes=(request.form.get('notes') or '').strip(),
            created_by=current_user.id if hasattr(current_user,'id') else None
        )
        db.session.add(rental)
        db.session.flush()

        for tool, qty, rate, amount, note in parsed_items:
            item = ToolRentalItem(
                rental_id=rental.id,
                tool_id=tool.id,
                qty_rented=qty,
                qty_returned=0.0,
                qty_pending=qty,
                rate=rate,
                amount=amount if billing_type!='no_charge' else 0.0,
                notes=note
            )
            db.session.add(item)
            db.session.flush()
            from_label = 'Warehouse / Store'
            if renter_type == 'internal' and project_id:
                proj = db.session.get(Project, project_id)
                to_label = proj.name if proj else 'Internal Site'
                if stage_id:
                    st = db.session.get(Stage, stage_id)
                    if st:
                        to_label += f" > {st.name}"
            else:
                to_label = customer_name or 'External Customer'
            create_movement_log(
                tool_id=tool.id,
                rental_id=rental.id,
                movement_type='rental_out',
                from_label=from_label,
                to_label=to_label,
                qty=qty,
                notes=f'Rental {rental_code} out'
            )
        if renter_type == 'external' and customer_name:
            # The customer lands in the Parties module (sidebar → Parties)
            # under External Customers the moment the rental is booked, so
            # every HDC Tools transaction shows up there.
            ensure_party(customer_name, party_type='rental', phone=customer_phone)
        db.session.commit()
        # Stay on the create page: the fresh rental *shows below* the form,
        # highlighted, so the operator sees it land without navigating away.
        # Its row (and the Rentals hub) still lead to the detail view where
        # returns, payments and transfers are recorded.
        qty_word = 'tool' if abs(total_rented_qty - 1.0) < 0.001 else 'tools'
        flash(f'Rental {rental_code} created — {total_rented_qty:g} {qty_word} '
              f'shown in the list below.', 'success')
        return redirect(url_for('hdc_tool_rental_new', created=rental.id))

    # ------------------ RENTAL DETAIL ------------------
    @app.route('/hdc/tool-rental/<int:rental_id>')
    @login_required
    def hdc_tool_rental_detail(rental_id):
        rental = ToolRental.query.get_or_404(rental_id)
        recalc_rental_totals(rental.id)
        db.session.commit()

        items = ToolRentalItem.query.filter_by(rental_id=rental.id).all()
        returns = (ToolRentalReturn.query
                   .filter_by(rental_id=rental.id)
                   .order_by(ToolRentalReturn.return_date.desc(), ToolRentalReturn.id.desc())
                   .all())
        payments = (ToolRentalPayment.query
                    .filter_by(rental_id=rental.id)
                    .order_by(ToolRentalPayment.payment_date.desc(), ToolRentalPayment.id.desc())
                    .all())
        transfers = (ToolRentalTransfer.query
                     .filter_by(rental_id=rental.id)
                     .order_by(ToolRentalTransfer.transfer_date.desc(), ToolRentalTransfer.id.desc())
                     .all())
        tracking_chain = get_rental_tracking_chain(rental.id)
        movement_logs = (ToolMovementLog.query
                         .filter_by(rental_id=rental.id)
                         .order_by(ToolMovementLog.timestamp.asc(), ToolMovementLog.id.asc())
                         .all())
        projects = Project.query.order_by(Project.name.asc()).all()
        stages = Stage.query.order_by(Stage.name.asc()).all()
        tools = Tool.query.filter(Tool.is_void==False).order_by(Tool.name.asc()).all()
        receiving_accounts = get_receiving_accounts()
        # (id, label) pairs for the searchable account combos on this page.
        receiving_account_options = [
            (acc.id, '%s (%s) - Bal: %s' % (acc.name, acc.type,
                                             '{:,.0f}'.format(acc.opening_balance or 0)))
            for acc in receiving_accounts
        ]

        pending_tools = float(rental.total_rented_qty or 0) - float(rental.total_returned_qty or 0)
        pending_amount = float(rental.total_amount or 0) - float(rental.total_paid or 0) if rental.billing_type!='no_charge' else 0.0

        # account txns for this rental payments
        from hdc.models.accounts import AccountTransaction
        from hdc.models.tool_rental import ToolRentalAccountTxn
        payment_ids = [p.id for p in payments]
        acct_links = {}
        if payment_ids:
            links = (ToolRentalAccountTxn.query
                     .filter(ToolRentalAccountTxn.payment_id.in_(payment_ids))
                     .all())
            for link in links:
                acct_links.setdefault(link.payment_id, []).append(link.account_txn_id)

        return render_template('tool_rental/tool_rental_detail.html',
            rental=rental,
            items=items,
            returns=returns,
            payments=payments,
            transfers=transfers,
            tracking_chain=tracking_chain,
            movement_logs=movement_logs,
            projects=projects,
            stages=stages,
            tools=tools,
            receiving_accounts=receiving_accounts,
            receiving_account_options=receiving_account_options,
            known_customers=known_tool_customers(),
            pending_tools=pending_tools,
            pending_amount=pending_amount,
            acct_links=acct_links,
            today=_pkt_today().isoformat()
        )

    # ------------------ RETURN (FULL/PARTIAL) WITH ACCOUNTS ------------------
    @app.route('/hdc/tool-rental/<int:rental_id>/return', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_tool_rental_return(rental_id):
        rental = ToolRental.query.get_or_404(rental_id)
        if rental.is_void:
            flash('Rental is voided.', 'danger')
            return redirect(url_for('hdc_tool_rental_detail', rental_id=rental.id))

        return_date_raw = (request.form.get('return_date') or '').strip()
        try:
            return_date = datetime.strptime(return_date_raw, '%Y-%m-%d').date() if return_date_raw else _pkt_today()
        except:
            return_date = _pkt_today()

        return_type = (request.form.get('return_condition') or request.form.get('return_type') or 'partial').strip().lower()
        if return_type not in ('full','partial'):
            return_type = 'partial'

        payment_type = (request.form.get('payment_condition') or request.form.get('payment_type') or 'partial').strip().lower()
        if payment_type not in ('full','partial','credit','no_payment'):
            payment_type = 'partial'

        rental_item_ids = request.form.getlist('rental_item_id[]') or request.form.getlist('rental_item_id')
        qty_returned_list = request.form.getlist('qty_returned[]') or request.form.getlist('qty_returned')
        condition_notes_list = request.form.getlist('condition_notes[]') or request.form.getlist('condition_notes')

        if return_type == 'full':
            rental_items = ToolRentalItem.query.filter_by(rental_id=rental.id).all()
            rental_item_ids = [str(i.id) for i in rental_items if float(i.qty_pending or 0) > 0.001]
            qty_returned_list = [str(float(ToolRentalItem.query.get(int(ri)).qty_pending or 0)) for ri in rental_item_ids]
        else:
            if not rental_item_ids or len(rental_item_ids)!=len(qty_returned_list):
                flash('For partial return, specify qty for each tool.', 'danger')
                return redirect(url_for('hdc_tool_rental_detail', rental_id=rental.id))

        total_tools_returned = 0.0
        parsed_returns = []
        for idx, (ri_id_raw, qty_raw) in enumerate(zip(rental_item_ids, qty_returned_list)):
            try:
                ri_id = int(ri_id_raw)
            except:
                continue
            r_item = ToolRentalItem.query.get(ri_id)
            if not r_item or int(r_item.rental_id)!=int(rental.id):
                flash(f'Invalid rental item {ri_id_raw}.', 'danger')
                return redirect(url_for('hdc_tool_rental_detail', rental_id=rental.id))
            qty_ret = max(0.0, _flt(qty_raw))
            if qty_ret <= 0:
                continue
            if qty_ret > float(r_item.qty_pending or 0) + 0.001:
                flash(f'Return qty {qty_ret} exceeds pending {r_item.qty_pending} for {r_item.tool.name}.', 'danger')
                return redirect(url_for('hdc_tool_rental_detail', rental_id=rental.id))
            cond_note = (condition_notes_list[idx] if idx < len(condition_notes_list) else '').strip()
            parsed_returns.append((r_item, qty_ret, cond_note))
            total_tools_returned += qty_ret

        if total_tools_returned <= 0:
            flash('No tools marked for return.', 'warning')
            return redirect(url_for('hdc_tool_rental_detail', rental_id=rental.id))

        amount_paid_raw = (request.form.get('amount_paid') or '0').strip()
        amount_paid = max(0.0, _flt(amount_paid_raw))

        if payment_type == 'full':
            pending_amt = float(rental.total_amount or 0) - float(rental.total_paid or 0)
            amount_paid = max(0.0, pending_amt) if rental.billing_type!='no_charge' else 0.0
        elif payment_type == 'no_payment' or rental.billing_type=='no_charge':
            amount_paid = 0.0
        elif payment_type == 'credit':
            amount_paid = max(0.0, _flt(request.form.get('amount_paid') or 0))

        # receiving account for this return's payment
        received_to_account_id = request.form.get('received_to_account_id', type=int)
        recv_acc = None
        if amount_paid > 0:
            if received_to_account_id:
                recv_acc = Account.query.get(int(received_to_account_id))
            if (not recv_acc) or recv_acc.is_void or str(recv_acc.status or 'active').lower() != 'active' or str(recv_acc.type or '').lower() not in ('company','cash','bank'):
                flash('Select a valid Cash/Bank receiving account for payment.', 'danger')
                return redirect(url_for('hdc_tool_rental_detail', rental_id=rental.id))

        # duplicate guard
        if _has_recent_duplicate(ToolRentalReturn, rental_id=rental.id, total_tools_returned=total_tools_returned, amount_paid=amount_paid, return_date=return_date):
            flash('Duplicate return prevented (same values submitted too quickly).', 'warning')
            return redirect(url_for('hdc_tool_rental_detail', rental_id=rental.id))

        ret_rec = ToolRentalReturn(
            rental_id=rental.id,
            return_date=return_date,
            return_type=return_type,
            payment_type=payment_type,
            total_tools_returned=total_tools_returned,
            amount_paid=amount_paid if rental.billing_type!='no_charge' else 0.0,
            notes=(request.form.get('notes') or '').strip(),
            created_by=current_user.id if hasattr(current_user,'id') else None
        )
        db.session.add(ret_rec)
        db.session.flush()

        for r_item, qty_ret, cond_note in parsed_returns:
            ret_item = ToolRentalReturnItem(
                return_id=ret_rec.id,
                rental_item_id=r_item.id,
                tool_id=r_item.tool_id,
                qty_returned=qty_ret,
                condition_notes=cond_note
            )
            db.session.add(ret_item)
            r_item.qty_returned = float(r_item.qty_returned or 0) + qty_ret
            r_item.qty_pending = max(0.0, float(r_item.qty_rented or 0) - float(r_item.qty_returned or 0))
            from_label = rental.current_location_label or 'Site'
            to_label = 'Warehouse / Store'
            create_movement_log(
                tool_id=r_item.tool_id,
                rental_id=rental.id,
                movement_type='return_in',
                from_label=from_label,
                to_label=to_label,
                qty=qty_ret,
                return_id=ret_rec.id,
                notes=f'Return {ret_rec.id}: {qty_ret} pcs'
            )

        pay_record = None
        if amount_paid > 0 and rental.billing_type!='no_charge':
            pay_record = ToolRentalPayment(
                rental_id=rental.id,
                return_id=ret_rec.id,
                payment_date=return_date,
                amount=amount_paid,
                payment_mode=(request.form.get('payment_mode') or 'cash').strip().lower(),
                received_to_account_id=int(recv_acc.id) if recv_acc else None,
                reference=(request.form.get('payment_reference') or '').strip(),
                notes=f'Payment on return {ret_rec.id}',
                created_by=current_user.id if hasattr(current_user,'id') else None
            )
            db.session.add(pay_record)
            db.session.flush()
            # post to accounts
            ok_acc, msg_acc, _ = post_tool_rental_payment_to_accounts(pay_record, rental=rental, commit=False)
            if not ok_acc:
                db.session.rollback()
                flash(msg_acc or 'Unable to post rental payment in accounts.', 'danger')
                return redirect(url_for('hdc_tool_rental_detail', rental_id=rental.id))
        elif payment_type=='credit' and rental.billing_type!='no_charge':
            rental.payment_status = 'credit'

        recalc_rental_totals(rental.id)
        if payment_type=='credit' and float(rental.total_pending_amount or 0) > 0:
            rental.payment_status = 'credit'

        db.session.commit()
        recv_name = recv_acc.name if recv_acc else '-'
        flash(f'Return recorded: {total_tools_returned} tools, {amount_paid:.2f} PKR paid to {recv_name}. Pending tools: {rental.total_pending_tools:.0f}, Pending amount: {rental.total_pending_amount:.2f}', 'success')
        return redirect(url_for('hdc_tool_rental_detail', rental_id=rental.id))

    # ------------------ ADD PAYMENT WITH ACCOUNTS ------------------
    @app.route('/hdc/tool-rental/<int:rental_id>/payment', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_tool_rental_payment(rental_id):
        rental = ToolRental.query.get_or_404(rental_id)
        if rental.billing_type=='no_charge':
            flash('This rental is No Charge (contract included) - no payment needed.', 'info')
            return redirect(url_for('hdc_tool_rental_detail', rental_id=rental.id))
        amount = max(0.0, _flt(request.form.get('amount')))
        if amount <=0:
            flash('Payment amount must be >0.', 'danger')
            return redirect(url_for('hdc_tool_rental_detail', rental_id=rental.id))

        received_to_account_id = request.form.get('received_to_account_id', type=int)
        recv_acc = None
        if received_to_account_id:
            recv_acc = Account.query.get(int(received_to_account_id))
        if (not recv_acc) or recv_acc.is_void or str(recv_acc.status or 'active').lower() != 'active' or str(recv_acc.type or '').lower() not in ('company','cash','bank'):
            flash('Select a valid Cash/Bank receiving account (Company/Cash/Bank).', 'danger')
            return redirect(url_for('hdc_tool_rental_detail', rental_id=rental.id))

        pay_date_raw = (request.form.get('payment_date') or '').strip()
        try:
            pay_date = datetime.strptime(pay_date_raw, '%Y-%m-%d').date() if pay_date_raw else _pkt_today()
        except:
            pay_date = _pkt_today()

        if _has_recent_duplicate(ToolRentalPayment, rental_id=rental.id, amount=amount, payment_date=pay_date, received_to_account_id=recv_acc.id):
            flash('Duplicate payment prevented.', 'warning')
            return redirect(url_for('hdc_tool_rental_detail', rental_id=rental.id))

        payment_mode = (request.form.get('payment_mode') or 'cash').strip().lower()
        reference = (request.form.get('reference') or '').strip()
        notes = (request.form.get('notes') or '').strip()

        pay = ToolRentalPayment(
            rental_id=rental.id,
            payment_date=pay_date,
            amount=amount,
            payment_mode=payment_mode,
            received_to_account_id=int(recv_acc.id),
            reference=reference,
            notes=notes,
            created_by=current_user.id if hasattr(current_user,'id') else None
        )
        db.session.add(pay)
        db.session.flush()

        ok_acc, msg_acc, _ = post_tool_rental_payment_to_accounts(pay, rental=rental, commit=False)
        if not ok_acc:
            db.session.rollback()
            flash(msg_acc or 'Unable to post payment in accounts.', 'danger')
            return redirect(url_for('hdc_tool_rental_detail', rental_id=rental.id))

        recalc_rental_totals(rental.id)
        db.session.commit()
        flash(f'Payment {amount:.2f} PKR received in {recv_acc.name} (Cash/Bank). Pending: {rental.total_pending_amount:.2f}', 'success')
        return redirect(url_for('hdc_tool_rental_detail', rental_id=rental.id))

    @app.route('/hdc/tool-rental/payment/<int:payment_id>/void', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_tool_rental_payment_void(payment_id):
        pay = ToolRentalPayment.query.get_or_404(payment_id)
        if pay.is_void:
            flash('Payment already voided.', 'info')
            return redirect(url_for('hdc_tool_rental_detail', rental_id=pay.rental_id))
        pay.is_void = True
        pay.void_reason = (request.form.get('void_reason') or '').strip() or 'Voided by user'
        pay.voided_at = _pkt_now_naive()
        void_tool_rental_payment_in_accounts(pay.id)
        recalc_rental_totals(pay.rental_id)
        db.session.commit()
        flash('Payment voided and removed from accounts.', 'warning')
        return redirect(url_for('hdc_tool_rental_detail', rental_id=pay.rental_id))

    @app.route('/hdc/tool-rental/payment/<int:payment_id>/receipt')
    @login_required
    def hdc_tool_rental_payment_receipt(payment_id):
        pay = ToolRentalPayment.query.get_or_404(payment_id)
        rental = pay.rental
        receipt_id = f"RCPT-TR-{pay.id:08d}"
        party_name = rental.customer_name if rental.renter_type=='external' else (rental.project.name if rental.project else 'Internal Site')
        return render_template('accounts/transaction_receipt.html',
            company_profile=_receipt_company_profile(),
            receipt_id=receipt_id,
            created_at=(pay.created_at or _pkt_now_naive()),
            tx_type='Tool Rental Payment Receipt',
            party_name=party_name,
            project_name=(rental.project.name if rental.project else '-'),
            stage_name=(rental.stage.name if rental.stage else '-'),
            account_used=(pay.received_to_account.name if pay.received_to_account else 'Company Cash'),
            amount=float(pay.amount or 0.0),
            amount_words=_amount_to_words(pay.amount or 0.0),
            note=(pay.notes or f'Rental {rental.rental_code}'),
            reference_id=f'tool_rental_payment#{pay.id}',
            recent_entries=[],
            recent_entries_title='',
            back_url=url_for('hdc_tool_rental_detail', rental_id=rental.id),
            print_label='Print / Save PDF'
        )

    # ------------------ TRANSFER SITE TO SITE ------------------
    @app.route('/hdc/tool-rental/<int:rental_id>/transfer', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_tool_rental_transfer(rental_id):
        rental = ToolRental.query.get_or_404(rental_id)
        if rental.status in ('returned','closed'):
            flash('Rental already closed, cannot transfer.', 'warning')
            return redirect(url_for('hdc_tool_rental_detail', rental_id=rental.id))

        from_label = rental.current_location_label or 'Unknown'
        to_type = (request.form.get('to_type') or 'site').strip().lower()
        if to_type not in ('site','customer','warehouse'):
            to_type = 'site'

        to_project_id = request.form.get('to_project_id', type=int) if to_type=='site' else None
        to_stage_id = request.form.get('to_stage_id', type=int) if to_type=='site' else None
        to_customer_name = (request.form.get('to_customer_name') or '').strip() if to_type=='customer' else None

        if to_type=='site' and not to_project_id:
            flash('Select destination project/site.', 'danger')
            return redirect(url_for('hdc_tool_rental_detail', rental_id=rental.id))
        if to_type=='customer' and not to_customer_name:
            flash('Enter destination customer name.', 'danger')
            return redirect(url_for('hdc_tool_rental_detail', rental_id=rental.id))
        if to_type == 'site' and to_stage_id:
            stage = db.session.get(Stage, to_stage_id)
            if not stage or stage.project_id != to_project_id:
                flash('Selected stage does not belong to the destination project/site.', 'danger')
                return redirect(url_for('hdc_tool_rental_detail', rental_id=rental.id))

        if to_type=='site' and to_project_id:
            proj = db.session.get(Project, to_project_id)
            to_label = proj.name if proj else f'Project #{to_project_id}'
            if to_stage_id:
                st = db.session.get(Stage, to_stage_id)
                if st:
                    to_label += f" > {st.name}"
        elif to_type=='customer':
            to_label = to_customer_name
        else:
            to_label = 'Warehouse / Store'

        qty_transferred = max(0.0, _flt(request.form.get('qty_transferred'), rental.total_pending_tools))
        if qty_transferred <=0:
            qty_transferred = float(rental.total_pending_tools or 0)

        transfer_date_raw = (request.form.get('transfer_date') or '').strip()
        try:
            transfer_date = datetime.strptime(transfer_date_raw, '%Y-%m-%d').date() if transfer_date_raw else _pkt_today()
        except:
            transfer_date = _pkt_today()

        from_project_id = None
        from_stage_id = None
        last_transfer = (ToolRentalTransfer.query
                         .filter_by(rental_id=rental.id)
                         .order_by(ToolRentalTransfer.transfer_date.desc(), ToolRentalTransfer.id.desc())
                         .first())
        if last_transfer:
            from_project_id = last_transfer.to_project_id
            from_stage_id = last_transfer.to_stage_id
        else:
            from_project_id = rental.project_id
            from_stage_id = rental.stage_id

        transfer = ToolRentalTransfer(
            rental_id=rental.id,
            from_type='site' if from_project_id else 'customer',
            from_project_id=from_project_id,
            from_stage_id=from_stage_id,
            from_customer_name=rental.customer_name if not from_project_id else None,
            from_location_label=from_label,
            to_type=to_type,
            to_project_id=to_project_id,
            to_stage_id=to_stage_id,
            to_customer_name=to_customer_name,
            to_location_label=to_label,
            qty_transferred=qty_transferred,
            transfer_date=transfer_date,
            notes=(request.form.get('notes') or '').strip(),
            created_by=current_user.id if hasattr(current_user,'id') else None
        )
        db.session.add(transfer)
        db.session.flush()

        rental_items = ToolRentalItem.query.filter_by(rental_id=rental.id).filter(ToolRentalItem.qty_pending>0).all()
        # Split the transferred quantity across tools so the dashboard can say
        # *which* tool moved where (not just "20 pcs left Site1").  Per-tool
        # qty_transfer[] fields win; otherwise allocate_transfer_qty fills the
        # pending lines up to the requested total.
        per_item_raw = request.form.getlist('qty_transfer[]') or request.form.getlist('qty_transfer')
        item_id_raw = request.form.getlist('rental_item_id[]') or request.form.getlist('rental_item_id')
        allocations = []
        if per_item_raw and len(per_item_raw) == len(item_id_raw):
            by_id = {int(ri.id): ri for ri in rental_items}
            for ri_id_raw, qty_raw in zip(item_id_raw, per_item_raw):
                try:
                    ri = by_id.get(int(ri_id_raw))
                except (TypeError, ValueError):
                    ri = None
                take = max(0.0, _flt(qty_raw))
                if not ri or take <= 0:
                    continue
                take = min(take, float(ri.qty_pending or 0))
                if take > 0:
                    allocations.append((ri, take))
        if not allocations:
            allocations = allocate_transfer_qty(
                [(ri, float(ri.qty_pending or 0)) for ri in rental_items], qty_transferred)

        moved_qty = 0.0
        moved_tools = []
        for ri, take in allocations:
            moved_qty += float(take)
            moved_tools.append(f'{ri.tool.name if ri.tool else ri.tool_id} x{take:g}')
            create_movement_log(
                tool_id=ri.tool_id,
                rental_id=rental.id,
                movement_type='site_transfer' if to_type=='site' else 'external_transfer',
                from_label=from_label,
                to_label=to_label,
                qty=float(take),
                transfer_id=transfer.id,
                notes=f'Transfer {from_label} > {to_label}'
            )
        record_transfer_items(transfer, allocations)
        transfer.qty_transferred = moved_qty or float(qty_transferred or 0)
        db.session.commit()
        chain_str = " > ".join(rental.tracking_chain)
        moved_desc = f'{moved_qty:g} qty' if allocations else f'{qty_transferred:g} qty'
        detail = f' — {", ".join(moved_tools)}' if moved_tools else ''
        flash(f'Tools transferred: {from_label} to {to_label} ({moved_desc}{detail}). '
              f'Chain: {chain_str}', 'success')
        return redirect(url_for('hdc_tool_rental_detail', rental_id=rental.id))

    # ------------------ GLOBAL TRACKING ------------------
    @app.route('/hdc/tool-rental/tracking')
    @login_required
    def hdc_tool_rental_tracking():
        tool_id = request.args.get('tool_id', type=int)
        project_id = request.args.get('project_id', type=int)
        q = (request.args.get('q') or '').strip() or None
        locations = global_tool_locations(search_tool_id=tool_id, search_project_id=project_id, search_text=q)
        projects = Project.query.order_by(Project.name.asc()).all()
        tools = Tool.query.filter(Tool.is_void==False).order_by(Tool.name.asc()).all()
        kpis = tool_kpis()
        return render_template('tool_rental/tool_tracking.html',
            locations=locations,
            projects=projects,
            tools=tools,
            selected_tool=tool_id,
            selected_project=project_id,
            q=q,
            kpis=kpis
        )

    # ------------------ REPORTING ------------------
    @app.route('/hdc/tool-rental/reports')
    @login_required
    def hdc_tool_rental_reports():
        filters = {
            'project_id': request.args.get('project_id', type=int),
            'tool_id': request.args.get('tool_id', type=int),
            'renter_type': (request.args.get('renter_type') or '').strip() or None,
            'status': (request.args.get('status') or '').strip() or None,
            'payment_status': (request.args.get('payment_status') or '').strip() or None,
            'billing_type': (request.args.get('billing_type') or '').strip() or None,
            'date_from': (request.args.get('date_from') or '').strip() or None,
            'date_to': (request.args.get('date_to') or '').strip() or None,
            'search_text': (request.args.get('q') or '').strip() or None,
        }
        rentals = search_rentals(filters)
        total_rented = sum(float(r.total_rented_qty or 0) for r in rentals)
        total_returned = sum(float(r.total_returned_qty or 0) for r in rentals)
        total_pending_tools = sum(float(r.total_pending_tools or 0) for r in rentals)
        total_amount = sum(float(r.total_amount or 0) for r in rentals if r.billing_type!='no_charge')
        total_paid = sum(float(r.total_paid or 0) for r in rentals)
        total_pending_amount = sum(float(r.total_pending_amount or 0) for r in rentals)

        tool_breakdown = {}
        for r in rentals:
            for item in r.items:
                tid = item.tool_id
                if tid not in tool_breakdown:
                    tool_breakdown[tid] = {'tool': item.tool, 'rented':0, 'returned':0, 'pending':0, 'amount':0}
                tool_breakdown[tid]['rented'] += float(item.qty_rented or 0)
                tool_breakdown[tid]['returned'] += float(item.qty_returned or 0)
                tool_breakdown[tid]['pending'] += float(item.qty_pending or 0)
                tool_breakdown[tid]['amount'] += float(item.amount or 0)

        site_breakdown = {}
        for r in rentals:
            key = r.project_id or 0
            label = r.project.name if r.project else (r.customer_name or 'External')
            if key not in site_breakdown:
                site_breakdown[key] = {'label': label, 'rented':0, 'pending_tools':0, 'amount':0, 'paid':0, 'pending_amount':0, 'count':0}
            site_breakdown[key]['rented'] += float(r.total_rented_qty or 0)
            site_breakdown[key]['pending_tools'] += float(r.total_pending_tools or 0)
            site_breakdown[key]['amount'] += float(r.total_amount or 0) if r.billing_type!='no_charge' else 0
            site_breakdown[key]['paid'] += float(r.total_paid or 0)
            site_breakdown[key]['pending_amount'] += float(r.total_pending_amount or 0)
            site_breakdown[key]['count'] += 1

        projects = Project.query.order_by(Project.name.asc()).all()
        tools = Tool.query.filter(Tool.is_void==False).order_by(Tool.name.asc()).all()
        kpis = tool_kpis()
        receiving_accounts = get_receiving_accounts()

        # ---- stock life-cycle registers: what we bought, what we scrapped ----
        date_from = date_to = None
        for key in ('date_from', 'date_to'):
            raw = filters.get(key)
            if not raw:
                continue
            try:
                parsed = datetime.strptime(raw, '%Y-%m-%d').date()
            except Exception:
                continue
            if key == 'date_from':
                date_from = parsed
            else:
                date_to = parsed
        purchases = tool_purchases(tool_id=filters.get('tool_id'), date_from=date_from,
                                   date_to=date_to, search_text=filters.get('search_text'))
        scraps = tool_scraps(tool_id=filters.get('tool_id'), date_from=date_from,
                             date_to=date_to, search_text=filters.get('search_text'))
        purchase_totals = {
            'qty': sum(float(p.qty or 0) for p in purchases),
            'value': sum(float(p.total_cost or 0) for p in purchases),
            'count': len(purchases),
        }
        scrap_totals = {
            'qty': sum(float(s.qty or 0) for s in scraps),
            'value': sum(float(s.value_written_off or 0) for s in scraps),
            'count': len(scraps),
        }

        return render_template('tool_rental/tool_reports.html',
            rentals=rentals,
            filters=filters,
            total_rented=total_rented,
            total_returned=total_returned,
            total_pending_tools=total_pending_tools,
            total_amount=total_amount,
            total_paid=total_paid,
            total_pending_amount=total_pending_amount,
            tool_breakdown=tool_breakdown,
            site_breakdown=site_breakdown,
            purchases=purchases,
            scraps=scraps,
            purchase_totals=purchase_totals,
            scrap_totals=scrap_totals,
            projects=projects,
            tools=tools,
            receiving_accounts=receiving_accounts,
            kpis=kpis
        )

    # ------------------ API ------------------
    @app.route('/hdc/api/tool-rental/tools-available')
    @login_required
    def hdc_api_tools_available():
        tools = Tool.query.filter(Tool.is_void==False, Tool.status=='active').order_by(Tool.name.asc()).all()
        data = []
        for t in tools:
            data.append({
                'id': t.id,
                'code': t.tool_code,
                'name': t.name,
                'unit': t.unit,
                'total_qty': float(t.total_quantity or 0),
                'available_qty': float(t.available_qty),
                'rented_out': float(t.rented_out_qty),
                'rate': float(t.rental_rate_per_day or 0)
            })
        return jsonify(data)

    @app.route('/hdc/api/tool-rental/rental/<int:rental_id>/pending-items')
    @login_required
    def hdc_api_rental_pending_items(rental_id):
        rental = ToolRental.query.get_or_404(rental_id)
        items = []
        for it in rental.items:
            if float(it.qty_pending or 0) > 0.001:
                items.append({
                    'rental_item_id': it.id,
                    'tool_id': it.tool_id,
                    'tool_name': it.tool.name if it.tool else '-',
                    'tool_code': it.tool.tool_code if it.tool else '-',
                    'qty_rented': float(it.qty_rented or 0),
                    'qty_returned': float(it.qty_returned or 0),
                    'qty_pending': float(it.qty_pending or 0),
                    'rate': float(it.rate or 0)
                })
        return jsonify(items)

    @app.route('/hdc/api/tool-rental/receiving-accounts')
    @login_required
    def hdc_api_receiving_accounts():
        accs = get_receiving_accounts()
        return jsonify([{'id': a.id, 'name': a.name, 'type': a.type, 'balance': float(a.opening_balance or 0)} for a in accs])

    @app.route('/hdc/api/tool-rental/dashboard')
    @login_required
    def hdc_api_tool_dashboard():
        """Machine-readable position: totals, split, per-tool rows, locations.

        Feeds the Tools dashboard charts and any external analysis without
        re-implementing the reconciliation rules.
        """
        ledger = tool_ledger()
        summary = dashboard_summary(ledger)
        recon = summary['recon']
        return jsonify({
            'as_of': _pkt_today().isoformat(),
            'totals': ledger['totals'],
            'reconciliation': {
                'owned': recon['owned'], 'in_store': recon['in_store'],
                'own_project': recon['own_project'], 'customer': recon['customer'],
                'total_sent': recon['total_sent'], 'accounted': recon['accounted'],
                'variance': recon['variance'], 'balanced': recon['balanced'],
            },
            'split': summary['split'],
            # every location, not just the top few — an analyst reconciling
            # "where are all 533 pieces" needs the full list to add up
            'locations': list(location_summary(ledger)),
            'tools': [{
                'tool_id': r['tool_id'], 'code': r['code'], 'name': r['name'],
                'category': r['category'], 'unit': r['unit'],
                'owned': r['owned_qty'], 'in_store': r['in_store_qty'],
                'own_project': r['own_project_qty'], 'customer': r['customer_qty'],
                'out': r['out_qty'], 'utilization_pct': r['utilization_pct'],
                'overdue': r['overdue_qty'], 'days_out': r['oldest_days_out'],
                'unaccounted': r['unaccounted'], 'current_label': r['current_label'],
                'purchased_qty': r['purchased_qty'], 'purchased_value': r['purchased_value'],
                'scrapped_qty': r['scrapped_qty'], 'scrapped_value': r['scrapped_value'],
            } for r in ledger['tools']],
        })
