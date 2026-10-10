"""HDC Tool Rental routes - dashboard, hub, inventory, rentals, returns, transfers, tracking, reports.
Now includes Accounts integration: payment receiving in Cash/Bank accounts.
"""

from datetime import datetime
from urllib.parse import quote, unquote
from flask import flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func
from sqlalchemy.orm import joinedload

from hdc.extensions import _money_write_required, db
from hdc.models.accounts import Account
from hdc.models.projects import Project, Stage
from hdc.models.tool_rental import (
    TOOL_SCRAP_REASONS, Tool, ToolAudit, ToolCategory, ToolMovementLog, ToolPurchase,
    ToolRental, ToolRentalDiscount, ToolRentalItem, ToolRentalPayment, ToolRentalReturn,
    ToolRentalReturnItem, ToolRentalTransfer, ToolRentalTransferItem,
    ToolSerial, ToolSerialMovement
)
from hdc.services.cashflow_register import ensure_party
from hdc.services.tool_rental import (
    _ensure_tool_category, _next_rental_code, _next_tool_code,
    _parse_date, create_movement_log, discount_reason_options, get_rental_tracking_chain,
    get_receiving_accounts, global_tool_locations,
    known_tool_customers, known_tool_suppliers,
    post_tool_rental_payment_to_accounts, recalc_rental_totals,
    record_tool_purchase, record_tool_scrap,
    record_tool_rental_discount, rental_discount_rows, rental_transfer_chain,
    search_rentals, tool_available_for_integrity, tool_kpis, tool_purchases,
    tool_scraps, tool_stock_aggregates, void_discounts_for_payment,
    void_tool_rental_discount, void_tool_rental_payment_in_accounts,
    # Serial management functions
    assign_serials_to_rental, create_tool_serials, create_serial_movement,
    ensure_all_tools_serials, ensure_tool_serials,
    get_all_tool_serials_summary, get_available_serials, get_out_of_store_serials,
    get_serial_status, get_serials_by_tool_for_rental, get_serials_for_rental,
    get_serials_in_store_summary, get_serials_out_of_store_summary,
    parse_custom_serial_list, rename_tool_serial,
    return_serials_from_rental, transfer_serials, update_serial_status
)
from hdc.services.tool_audit import (
    AUDIT_LOSS_REASONS, audit_history, audit_locations, audit_matrix, audit_movement,
    audit_rows_for_json, audit_sheet, audit_summary, close_audit, open_audit_count,
    parse_location_key, pending_audit_for, plan_adjustments, post_adjustments,
    reopen_audit, save_counts, start_audit, void_audit,
)
from hdc.services.tool_tracking import (
    LOC_CUSTOMER, LOC_OWN_PROJECT, LOC_STORE, WAREHOUSE_LABEL,
    allocate_transfer_qty, dashboard_summary, inventory_rows, location_summary,
    record_transfer_items, tool_item_summary, tool_ledger, tool_position,
    tracking_location_groups,
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

#: How many still-open rentals the "Transfer Rental" picker offers as a source.
#: A picker, not a list — the full searchable Rentals page is elsewhere.
TRANSFER_SOURCE_LIMIT = 60


def _transfer_location_key(loc_type, project_id=None, stage_id=None, customer_name=None):
    """Encode one actual holding location (shared by several rentals)."""
    return '|'.join((
        str(loc_type or ''),
        str(int(project_id or 0)),
        str(int(stage_id or 0)),
        quote((customer_name or '').strip(), safe=''),
    ))


def _transfer_source_key(rental_id, loc_type, project_id=None, stage_id=None, customer_name=None):
    """Encode one rental + one actual holding location for the From picker."""
    return f"{int(rental_id)}|{_transfer_location_key(loc_type, project_id, stage_id, customer_name)}"


def _tool_project_option_label(project):
    """Show enough context to find a site by either its name or its client."""
    if not project:
        return ''
    label = (project.name or '').strip()
    code = (project.project_code or '').strip()
    client = (project.client or '').strip()
    if code:
        label += f' ({code})'
    if client:
        label += f' — Client: {client}'
    return label


def _tool_project_combo_options(projects):
    return [(project.id, _tool_project_option_label(project))
            for project in projects]


def _tool_stage_combo_options(stages):
    return [
        (stage.id,
         f"{stage.name} — {_tool_project_option_label(stage.project)}",
         {'project-id': stage.project_id})
        for stage in stages
    ]


def _tool_picker_options(tools):
    return [
        (tool.id, f'{tool.name} ({tool.tool_code})' if tool.tool_code else tool.name)
        for tool in tools
    ]


def _parse_transfer_source_key(value):
    """Return ``(rental_id, location)``; numeric keys are legacy rental-only posts."""
    raw = (value or '').strip()
    if not raw:
        return None, None
    parts = raw.split('|', 4)
    try:
        rental_id = int(parts[0])
    except (TypeError, ValueError):
        return None, None
    if len(parts) == 1:
        return rental_id, None
    if len(parts) != 5:
        return None, None
    loc_type = parts[1].strip().lower()
    if loc_type not in (LOC_OWN_PROJECT, LOC_CUSTOMER, LOC_STORE):
        return None, None
    try:
        project_id = int(parts[2] or 0)
        stage_id = int(parts[3] or 0)
    except (TypeError, ValueError):
        return None, None
    return rental_id, {
        'loc_type': loc_type,
        'project_id': project_id,
        'stage_id': stage_id,
        'customer_name': unquote(parts[4]).strip(),
    }


def _holding_is_at_transfer_source(holding, source_location):
    if not source_location:
        return True
    return (
        holding.get('loc_type') == source_location.get('loc_type')
        and int(holding.get('project_id') or 0) == int(source_location.get('project_id') or 0)
        and int(holding.get('stage_id') or 0) == int(source_location.get('stage_id') or 0)
        and (holding.get('customer_name') or '').strip().lower()
            == (source_location.get('customer_name') or '').strip().lower()
    )


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
        q = (request.args.get('q') or '').strip()
        category_id = request.args.get('category_id', type=int)
        summary = tool_item_summary(term=q or None, category_id=category_id)
        categories = ToolCategory.query.order_by(ToolCategory.name.asc()).all()

        return render_template('tool_rental/tool_dashboard.html',
            summary=summary,
            totals=summary['totals'],
            categories=categories,
            q=q,
            selected_category=category_id,
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
        ensure_tool_serials(tool=tool, commit=True)
        return render_template('tool_rental/tool_position.html',
            row=row,
            movements=position['movements'],
            serials=tool.active_serials,
            in_store_serials=tool.in_store_serials,
            out_serials=tool.out_serials,
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
            known_customers=known_tool_customers(),
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
                serial_start_no = request.form.get('serial_start_no', type=int)
                custom_serials = (request.form.get('custom_serials') or '').strip() or None
                if custom_serials and total_qty <= 0:
                    total_qty = float(len(parse_custom_serial_list(custom_serials)))

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
                        serial_numbers=custom_serials,
                        start_no=serial_start_no,
                    )
                    if not ok_pur:
                        db.session.rollback()
                        flash(msg_pur or 'Could not record opening stock.', 'danger')
                        return redirect(url_for('hdc_tool_rental_inventory'))

                db.session.commit()
                if purchase is not None:
                    serial_list = tool.active_serials
                    serial_hint = ''
                    if serial_list:
                        if len(serial_list) <= 4:
                            serial_hint = f" [{', '.join(s.serial_number for s in serial_list)}]"
                        else:
                            serial_hint = f" [{serial_list[0].serial_number} .. {serial_list[-1].serial_number}]"
                    flash(f'Tool {tool.name} ({code}) added with {total_qty:g} {unit} '
                          f'opening stock ({purchase.purchase_code}){serial_hint}.', 'success')
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

        # Sync serial markings with ledger quantities & get serial summary for inventory
        ensure_all_tools_serials(
            created_by=getattr(current_user, 'id', None),
            commit=True,
        )
        serial_summary = get_all_tool_serials_summary()
        serial_lookup = {s['tool_id']: s for s in serial_summary}
        in_store_serials_by_tool = {}
        for s in ToolSerial.query.filter_by(is_in_store=True).all():
            if getattr(s, 'is_scrapped', False) or s.status == 'scrapped':
                continue
            in_store_serials_by_tool.setdefault(int(s.tool_id), []).append(s)
        for tid_k in in_store_serials_by_tool:
            in_store_serials_by_tool[tid_k].sort(key=lambda s: s.sort_key)

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
            serial_lookup=serial_lookup,
            in_store_serials_by_tool=in_store_serials_by_tool,
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
        ensure_tool_serials(tool=tool, created_by=getattr(current_user, 'id', None), commit=False)
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
        custom_serials = (request.form.get('custom_serials') or '').strip() or None
        serial_start_no = request.form.get('serial_start_no', type=int)
        qty = max(0.0, _flt(request.form.get('qty')))
        if qty <= 0 and custom_serials:
            qty = float(len(parse_custom_serial_list(custom_serials)))
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
            serial_numbers=custom_serials,
            start_no=serial_start_no,
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
        serial_ids = (
            request.form.getlist('scrap_serial_ids[]')
            or request.form.getlist('scrap_serial_ids')
            or (request.form.get('scrap_serial_ids_csv') or '').strip()
            or None
        )
        qty = max(0.0, _flt(request.form.get('qty')))
        if qty <= 0 and serial_ids:
            if isinstance(serial_ids, str):
                qty = float(len([x for x in serial_ids.split(',') if x.strip()]))
            else:
                qty = float(len([x for x in serial_ids if str(x).strip()]))
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
            serial_ids=serial_ids,
        )
        if not ok:
            flash(msg or 'Could not record scrap.', 'danger')
            return redirect(url_for('hdc_tool_rental_inventory', q=tool.tool_code))
        flash(f'Scrapped {scrap.qty:g} {tool.unit} of {tool.name} ({scrap.scrap_code}, '
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

        # Ensure all tools have their individual serial markings synced
        ensure_all_tools_serials(commit=True)

        # For "Transfer Rental": offer each actual holder/location within the
        # latest active rentals, not just a rental-wide "current location". A
        # rental may have been split by earlier site transfers, so the list of
        # tools must reflect the qty really sitting at the chosen From location.
        active_rentals = (ToolRental.query
                          .filter(ToolRental.is_void == False,
                                  ToolRental.status.in_(['active', 'partially_returned', 'overdue']))
                          .order_by(ToolRental.rental_date.desc(), ToolRental.id.desc())
                          .limit(TRANSFER_SOURCE_LIMIT).all())
        active_rental_ids = {int(r.id) for r in active_rentals}
        active_rental_order = {int(r.id): index for index, r in enumerate(active_rentals)}
        source_groups = {}
        positions = tool_ledger()
        for tool_row in positions['tools']:
            for holding in tool_row['holdings']:
                rental = holding['rental']
                rental_id = int(rental.id)
                if rental_id not in active_rental_ids:
                    continue
                loc_type = holding['loc_type']
                project_id = int(holding.get('project_id') or 0)
                stage_id = int(holding.get('stage_id') or 0)
                customer_name = (holding.get('customer_name') or '').strip()
                source_key = _transfer_source_key(
                    rental_id, loc_type, project_id, stage_id, customer_name)
                source = source_groups.setdefault(source_key, {
                    'key': source_key,
                    'id': rental_id,
                    'code': rental.rental_code,
                    'holder': holding['label'],
                    'loc_type': loc_type,
                    'project_id': project_id,
                    'stage_id': stage_id,
                    'customer_name': customer_name,
                    'location_key': _transfer_location_key(
                        loc_type, project_id, stage_id, customer_name),
                    'pending': 0.0,
                    'billing': rental.billing_type,
                    'lines_by_id': {},
                })
                item = holding['rental_item']
                tool = holding['tool']
                item_id = int(item.id)
                source_rate = float(item.rate or 0)
                daily_rate = float(tool.rental_rate_per_day or 0) if tool else 0.0
                if source_rate <= 0:
                    source_rate = daily_rate
                cat_name = (tool.category.name if tool and tool.category else (tool.name if tool else f'Tool#{item.tool_id}'))
                line = source['lines_by_id'].setdefault(item_id, {
                    'item_id': item_id,
                    'tool_id': int(item.tool_id),
                    'category_id': int(tool.category_id) if tool and tool.category_id else None,
                    'category_name': cat_name,
                    'name': tool.name if tool else f'Tool#{item.tool_id}',
                    'code': tool.tool_code if tool else '',
                    'qty': 0.0,
                    'rate': source_rate,
                    'daily_rate': daily_rate or source_rate,
                    'unit': (tool.unit if tool else '') or '',
                    'serials': [],
                })
                available_here = float(holding['qty'] or 0)
                line['qty'] += available_here
                source['pending'] += available_here

        # Attach out-of-store serial markings to each transfer source line
        out_serials_by_rental_tool = {}
        for s in ToolSerial.query.filter(
            ToolSerial.is_in_store == False,  # noqa: E712
            ToolSerial.current_rental_id.in_(active_rental_ids) if active_rental_ids else False,
        ).all():
            if getattr(s, 'is_scrapped', False) or s.status == 'scrapped':
                continue
            out_serials_by_rental_tool.setdefault((int(s.current_rental_id), int(s.tool_id)), []).append(s)
        for key_rt in out_serials_by_rental_tool:
            out_serials_by_rental_tool[key_rt].sort(key=lambda s: s.sort_key)

        used_serial_ids_in_sources = set()
        for source in source_groups.values():
            holder_norm = (source['holder'] or '').strip().lower()
            for line in source['lines_by_id'].values():
                need_cnt = max(0, int(round(float(line['qty'] or 0))))
                candidates = out_serials_by_rental_tool.get((int(source['id']), int(line['tool_id'])), [])
                # Prefer serials whose current_location_label matches this holder
                exact = [
                    s for s in candidates
                    if s.id not in used_serial_ids_in_sources
                    and (s.current_location_label or '').strip().lower() == holder_norm
                ]
                fallback = [
                    s for s in candidates
                    if s.id not in used_serial_ids_in_sources and s not in exact
                ]
                chosen = (exact + fallback)[:need_cnt]
                for s in chosen:
                    used_serial_ids_in_sources.add(s.id)
                    line['serials'].append({
                        'id': s.id,
                        'serial_number': s.serial_number,
                        'condition': s.condition or 'good',
                        'location': source['holder'],
                    })

        transfer_sources = []
        for source in source_groups.values():
            source['lines'] = sorted(source.pop('lines_by_id').values(),
                                     key=lambda line: (line['name'].lower(), line['code']))
            if source['pending'] > 0.001:
                transfer_sources.append(source)
        transfer_sources.sort(key=lambda source: (
            active_rental_order.get(source['id'], TRANSFER_SOURCE_LIMIT),
            source['holder'].lower(),
        ))
        # The transfer form can now be searched from either direction: start
        # with a current site/location and then choose its holders/tools, or
        # start with a tool and narrow the location list to places that
        # actually hold it. The exact rental+location source keys above stay on
        # the page (as ``TRANSFER_SOURCES``) so the holder checkboxes can post
        # the precise source of every moved tool; the grouped lists below are
        # only a convenient way to find those sources.
        transfer_location_groups = {}
        transfer_tool_groups = {}
        for source in transfer_sources:
            location = transfer_location_groups.setdefault(source['location_key'], {
                'key': source['location_key'],
                'label': source['holder'],
                'pending': 0.0,
                'tool_ids': set(),
                'tool_qtys': {},
            })
            location['pending'] += float(source['pending'] or 0)
            for line in source['lines']:
                tool_id = int(line['tool_id'])
                location['tool_ids'].add(tool_id)
                location['tool_qtys'][str(tool_id)] = (
                    location['tool_qtys'].get(str(tool_id), 0.0) + float(line['qty'] or 0)
                )
                tool_group = transfer_tool_groups.setdefault(tool_id, {
                    'id': tool_id,
                    'name': line['name'],
                    'code': line['code'],
                    'category_id': line.get('category_id'),
                    'category_name': line.get('category_name') or line['name'],
                    'qty': 0.0,
                    'location_keys': set(),
                    'serials': [],
                })
                tool_group['qty'] += float(line['qty'] or 0)
                tool_group['location_keys'].add(source['location_key'])
                for s_info in line.get('serials', []):
                    tool_group['serials'].append({
                        **s_info,
                        'source_key': source['key'],
                        'rental_id': source['id'],
                        'rental_code': source['code'],
                        'item_id': line['item_id'],
                        'holder': source['holder'],
                    })

        transfer_locations = sorted(
            ({**location, 'tool_ids': sorted(location['tool_ids'])}
             for location in transfer_location_groups.values()),
            key=lambda location: location['label'].lower(),
        )
        transfer_location_options = [
            (location['key'], f"{location['label']} ({location['pending']:g} available)")
            for location in transfer_locations
        ]
        transfer_tools = sorted(
            ({**tool, 'location_keys': sorted(tool['location_keys'])}
             for tool in transfer_tool_groups.values()),
            key=lambda tool: (tool['name'].lower(), tool['code']),
        )
        transfer_tool_options = [
            (tool['id'], f"{tool['name']}" +
             (f" ({tool['code']})" if tool['code'] else '') +
             f" — {tool['qty']:g} out at {len(tool['location_keys'])} location" +
             ('' if len(tool['location_keys']) == 1 else 's'))
            for tool in transfer_tools
        ]
        receiving_accounts = get_receiving_accounts()
        receiving_account_options = [
            (a.id, f"{a.name} ({a.type})") for a in receiving_accounts
        ]
        projects = Project.query.order_by(Project.name.asc()).all()
        stages = Stage.query.options(joinedload(Stage.project)).order_by(Stage.name.asc()).all()
        all_tools = Tool.query.filter(Tool.is_void == False).order_by(Tool.name.asc()).all()
        # For New Rental: only show tools that are currently IN STORE (available > 0)
        tools = [t for t in all_tools if t.available_qty > 0.001 or len(t.in_store_serials) > 0]
        categories = ToolCategory.query.order_by(ToolCategory.name.asc()).all()

        in_store_tools_json = []
        for t in tools:
            cat_name = t.category.name if t.category else t.name
            in_store_list = t.in_store_serials
            in_store_tools_json.append({
                'id': t.id,
                'name': t.name,
                'code': t.tool_code,
                'category_id': t.category_id,
                'category_name': cat_name,
                'rate': float(t.rental_rate_per_day or 0.0),
                'available_qty': float(t.available_qty),
                'unit': t.unit or 'pcs',
                'serials': [
                    {
                        'id': s.id,
                        'serial_number': s.serial_number,
                        'condition': s.condition or 'good',
                    }
                    for s in in_store_list
                ],
            })

        return render_template('tool_rental/tool_new_rental.html',
            projects=projects,
            project_options=_tool_project_combo_options(projects),
            stages=stages,
            stage_options=_tool_stage_combo_options(stages),
            tools=tools,
            categories=categories,
            in_store_tools_json=in_store_tools_json,
            known_customers=known_tool_customers(),
            recent_rentals=recent_rentals,
            created_rental=created_rental,
            transfer_sources=transfer_sources,
            transfer_locations=transfer_locations,
            transfer_location_options=transfer_location_options,
            transfer_tools=transfer_tools,
            transfer_tool_options=transfer_tool_options,
            receiving_account_options=receiving_account_options,
            today=_pkt_today().isoformat(),
        )

    # ------------------ TRANSFER RENTAL (settle old holder(s), start new cycle) ------------------
    def _transfer_from_parts(rental, location):
        """Where one source rental's tools are physically leaving from.

        Returns ``(from_type, project_id, stage_id, customer_name, label)``.
        """
        if location:
            if location['loc_type'] == LOC_OWN_PROJECT:
                project = db.session.get(Project, location['project_id'])
                stage = (db.session.get(Stage, location['stage_id'])
                         if location['stage_id'] else None)
                label = project.name if project else f"Project #{location['project_id']}"
                if stage:
                    label += f' > {stage.name}'
                return ('site', location['project_id'] or None,
                        location['stage_id'] or None, None, label)
            if location['loc_type'] == LOC_CUSTOMER:
                label = location['customer_name'] or 'External Customer'
                return ('customer', None, None,
                        location['customer_name'] or None, label)
            return 'warehouse', None, None, None, WAREHOUSE_LABEL

        # No exact location posted (legacy forms): fall back to the rental's own
        # last hand-over target, else where the rental was originally opened.
        last_transfer = (ToolRentalTransfer.query
                         .filter_by(rental_id=rental.id)
                         .order_by(ToolRentalTransfer.transfer_date.desc(),
                                   ToolRentalTransfer.id.desc())
                         .first())
        label = rental.current_location_label or 'Unknown'
        if last_transfer:
            return (last_transfer.to_type or 'site', last_transfer.to_project_id,
                    last_transfer.to_stage_id, last_transfer.to_customer_name,
                    label)
        return ('site' if rental.renter_type == 'internal' else 'customer',
                rental.project_id, rental.stage_id,
                rental.customer_name if rental.renter_type == 'external' else None,
                label)

    def _create_transfer_rental():
        """Transfer selected tools from one or more holders into a new rental.

        A site can hold tools under more than one active rental (holder), so the
        source is one *or several* rentals at once: the operator ticks the
        holders — all ticked by default — and then the tool lines to move.
        Unselected lines stay on their source rental; selected lines are
        recorded as per-tool transfers and receive the destination rate entered
        on the form. Every contributing rental is settled, and ONE new rental
        is opened for the destination with all the moved lines.
        """
        ensure_all_tools_serials(commit=False)

        # Parse any global out-of-store serial markings chosen from the
        # Category -> Out-of-Store Tool No selector
        global_transfer_serial_ids = []
        raw_global_serials = (
            request.form.getlist('transfer_selected_serials[]')
            or request.form.getlist('transfer_selected_serials')
            or [(request.form.get('transfer_selected_serials_csv') or '').strip()]
        )
        for chunk in raw_global_serials:
            for tok in str(chunk or '').split(','):
                tok = tok.strip()
                if tok.isdigit():
                    sid = int(tok)
                    if sid not in global_transfer_serial_ids:
                        global_transfer_serial_ids.append(sid)

        # ---- the "from" side: a single holder, or every ticked holder ----
        raw_sources = [raw for raw in
                       (request.form.getlist('source_rental_id[]')
                        or request.form.getlist('source_rental_id')
                        or request.form.getlist('from_source_key[]')
                        or request.form.getlist('from_source_key'))
                       if (raw or '').strip()]

        # If the user selected out-of-store serials directly from the cascading
        # Category -> Tool No picker without ticking source_rental_id, derive
        # the source rentals automatically from those serials.
        if not raw_sources and global_transfer_serial_ids:
            for sid in global_transfer_serial_ids:
                s_obj = db.session.get(ToolSerial, sid)
                if s_obj and not s_obj.is_in_store and s_obj.current_rental_id:
                    raw_sources.append(str(int(s_obj.current_rental_id)))

        sources = []                       # [(rental, location)] in posted order
        seen_rental_ids = set()
        for raw in raw_sources:
            rental_id, location = _parse_transfer_source_key(raw)
            if not rental_id or rental_id in seen_rental_ids:
                continue
            rental = db.session.get(ToolRental, rental_id)
            if not rental or rental.is_void:
                continue
            seen_rental_ids.add(rental_id)
            sources.append((rental, location))
        if not sources:
            flash('Select the rental you are transferring from.', 'danger')
            return redirect(url_for('hdc_tool_rental_new'))
        closed = [rental.rental_code for rental, _ in sources
                  if rental.status in ('returned', 'closed')]
        if closed:
            if len(closed) == 1:
                flash(f'{closed[0]} is already closed — nothing left to transfer.',
                      'warning')
            else:
                flash(f"{', '.join(closed)} are already closed — nothing left "
                      f'to transfer.', 'warning')
            return redirect(url_for('hdc_tool_rental_new'))

        # One ledger pass powers the availability check for every source: a
        # rental split across sites by an earlier move may only offer what
        # really sits at the chosen From location.
        ledger_rows = (tool_ledger()['tools']
                       if any(location for _, location in sources) else [])

        pending_by_id = {}
        available_by_item = {}
        source_by_item = {}
        location_by_item = {}
        for rental, location in sources:
            pending_items = [item for item in
                             ToolRentalItem.query.filter_by(rental_id=rental.id).all()
                             if float(item.qty_pending or 0) > 0.001]
            availability = {int(item.id): max(0.0, float(item.qty_pending or 0))
                            for item in pending_items}
            if location:
                availability = {}
                for tool_row in ledger_rows:
                    for holding in tool_row['holdings']:
                        if (int(holding['rental'].id) == int(rental.id)
                                and _holding_is_at_transfer_source(holding, location)):
                            item_id = int(holding['rental_item'].id)
                            availability[item_id] = (
                                availability.get(item_id, 0.0)
                                + float(holding['qty'] or 0))
            for item in pending_items:
                item_id = int(item.id)
                pending_by_id[item_id] = item
                source_by_item[item_id] = rental
                location_by_item[item_id] = location
                available_by_item[item_id] = availability.get(item_id, 0.0)

        if not pending_by_id:
            if len(sources) == 1:
                flash(f'{sources[0][0].rental_code} has no tools still out to '
                      f'transfer.', 'warning')
            else:
                flash('The selected holders have no tools still out to transfer.',
                      'warning')
            return redirect(url_for('hdc_tool_rental_new'))

        # The new form posts an explicit marker, so an empty checkbox selection
        # can never silently move every tool. An unmarked legacy post keeps its
        # former behaviour and transfers every pending line of the source(s).
        raw_item_ids = (request.form.getlist('transfer_item_id[]')
                        or request.form.getlist('transfer_item_id'))
        if not raw_item_ids and global_transfer_serial_ids:
            derived_item_ids = []
            for sid in global_transfer_serial_ids:
                s_obj = db.session.get(ToolSerial, sid)
                if not s_obj or s_obj.is_in_store:
                    continue
                if s_obj.current_rental_item_id and int(s_obj.current_rental_item_id) in pending_by_id:
                    iid = str(int(s_obj.current_rental_item_id))
                    if iid not in derived_item_ids:
                        derived_item_ids.append(iid)
                else:
                    for iid_cand, it_cand in pending_by_id.items():
                        if it_cand.rental_id == s_obj.current_rental_id and it_cand.tool_id == s_obj.tool_id:
                            if str(iid_cand) not in derived_item_ids:
                                derived_item_ids.append(str(iid_cand))
                            break
            raw_item_ids = derived_item_ids

        explicit_selection = (request.form.get('transfer_selection_enabled') == '1'
                              or bool(raw_item_ids))
        if explicit_selection:
            if not raw_item_ids:
                flash('Select at least one tool to transfer.', 'danger')
                return redirect(url_for('hdc_tool_rental_new'))
            try:
                selected_ids = [int(raw_id) for raw_id in raw_item_ids]
            except (TypeError, ValueError):
                flash('The selected tools are invalid. Please choose them again.', 'danger')
                return redirect(url_for('hdc_tool_rental_new'))
            if len(selected_ids) != len(set(selected_ids)):
                flash('A tool line was selected more than once. Please choose the tools again.', 'danger')
                return redirect(url_for('hdc_tool_rental_new'))
            if any(item_id not in pending_by_id or available_by_item.get(item_id, 0) <= 0.001
                   for item_id in selected_ids):
                flash('One of the selected tools is no longer available at this holder. Please refresh and try again.', 'danger')
                return redirect(url_for('hdc_tool_rental_new'))
            selected_items = [(pending_by_id[item_id], available_by_item[item_id])
                              for item_id in selected_ids]
        else:
            selected_items = [
                (item, available_by_item.get(item_id, 0.0))
                for item_id, item in pending_by_id.items()
                if available_by_item.get(item_id, 0.0) > 0.001
            ]

        # ---- rent type for the new holder ----
        billing_type = (request.form.get('billing_type') or 'fixed_fee').strip().lower()
        if billing_type not in ('no_charge', 'fixed_fee', 'per_day', 'per_hour'):
            billing_type = 'fixed_fee'

        # ---- destination (the "to" holder) ----
        to_type = (request.form.get('renter_type') or 'internal').strip().lower()
        if to_type not in ('internal', 'external'):
            to_type = 'internal'
        to_project_id = request.form.get('project_id', type=int) if to_type == 'internal' else None
        to_stage_id = request.form.get('stage_id', type=int) if to_type == 'internal' else None
        to_customer_name = (request.form.get('customer_name') or '').strip() if to_type == 'external' else None
        to_customer_phone = (request.form.get('customer_phone') or '').strip() if to_type == 'external' else None
        to_customer_address = (request.form.get('customer_address') or '').strip() if to_type == 'external' else None

        if to_type == 'internal' and not to_project_id:
            flash('Select the destination site/project for the transfer.', 'danger')
            return redirect(url_for('hdc_tool_rental_new'))
        if to_type == 'internal' and not db.session.get(Project, to_project_id):
            flash('The destination site/project could not be found. Please choose it again.', 'danger')
            return redirect(url_for('hdc_tool_rental_new'))
        if to_type == 'external' and not to_customer_name:
            flash('Enter the destination customer name for the transfer.', 'danger')
            return redirect(url_for('hdc_tool_rental_new'))
        if to_type == 'internal' and to_stage_id:
            st = db.session.get(Stage, to_stage_id)
            if not st or st.project_id != to_project_id:
                flash('Selected stage does not belong to the destination site.', 'danger')
                return redirect(url_for('hdc_tool_rental_new'))

        date_raw = (request.form.get('rental_date') or '').strip()
        try:
            txn_date = datetime.strptime(date_raw, '%Y-%m-%d').date() if date_raw else _pkt_today()
        except Exception:
            txn_date = _pkt_today()
        exp_raw = (request.form.get('expected_return_date') or '').strip()
        try:
            exp_date = datetime.strptime(exp_raw, '%Y-%m-%d').date() if exp_raw else None
        except Exception:
            exp_date = None
        operator_notes = (request.form.get('notes') or '').strip()

        # Snapshot the chosen lines, out-of-store serial markings, and destination
        # rates before touching the source rentals.
        moved = []
        claimed_serial_ids = set()
        for item, available_qty in selected_items:
            max_avail = min(max(0.0, float(available_qty or 0)),
                            max(0.0, float(item.qty_pending or 0)))
            if max_avail <= 0.001:
                continue

            # Check if explicit out-of-store serial IDs were selected for this item
            raw_item_serials = (
                request.form.getlist(f'transfer_serial_ids_{item.id}[]')
                or request.form.getlist(f'transfer_serial_ids_{item.id}')
            )
            item_serial_ids = []
            for chunk in raw_item_serials:
                for tok in str(chunk or '').split(','):
                    tok = tok.strip()
                    if tok.isdigit():
                        sid = int(tok)
                        if sid not in item_serial_ids:
                            item_serial_ids.append(sid)

            if not item_serial_ids and global_transfer_serial_ids:
                for sid in global_transfer_serial_ids:
                    s_obj = db.session.get(ToolSerial, sid)
                    if (s_obj and not s_obj.is_in_store
                            and s_obj.current_rental_id == item.rental_id
                            and s_obj.tool_id == item.tool_id
                            and sid not in claimed_serial_ids):
                        item_serial_ids.append(sid)

            chosen_serials = []
            if item_serial_ids:
                for sid in item_serial_ids:
                    s_obj = db.session.get(ToolSerial, sid)
                    if not s_obj or s_obj.tool_id != item.tool_id or getattr(s_obj, 'is_scrapped', False):
                        flash(f'Invalid tool serial selected for {item.tool.name if item.tool else "tool"}.', 'danger')
                        return redirect(url_for('hdc_tool_rental_new'))
                    if s_obj.is_in_store:
                        flash(f'{s_obj.serial_number} is currently in store — only tools out at a site/customer can be transferred.', 'danger')
                        return redirect(url_for('hdc_tool_rental_new'))
                    if s_obj.current_rental_id and int(s_obj.current_rental_id) != int(item.rental_id):
                        flash(f'{s_obj.serial_number} belongs to a different rental.', 'danger')
                        return redirect(url_for('hdc_tool_rental_new'))
                    if sid not in claimed_serial_ids:
                        chosen_serials.append(s_obj)
                        claimed_serial_ids.add(sid)
                qty = min(max_avail, float(len(chosen_serials)))
                chosen_serials = chosen_serials[:int(round(qty))]
            else:
                qty_raw = request.form.get(f'transfer_qty_{item.id}')
                if qty_raw is not None and str(qty_raw).strip():
                    qty = min(max_avail, max(0.0, _flt(qty_raw, max_avail)))
                else:
                    qty = max_avail
                if qty > 0.001:
                    # Auto-pick matching out-of-store serials for this source rental/tool
                    loc_info = location_by_item.get(int(item.id))
                    _, _, _, _, src_loc_label = _transfer_from_parts(source_by_item[int(item.id)], loc_info)
                    candidates = get_out_of_store_serials(
                        tool_id=item.tool_id,
                        rental_id=item.rental_id,
                        location_label=src_loc_label,
                    )
                    for s_obj in candidates:
                        if s_obj.id not in claimed_serial_ids and len(chosen_serials) < int(round(qty)):
                            chosen_serials.append(s_obj)
                            claimed_serial_ids.add(s_obj.id)

            if qty <= 0.001:
                continue
            source_rate = float(item.rate or 0)
            daily_rate = float(item.tool.rental_rate_per_day or 0) if item.tool else 0.0
            if billing_type in ('per_day', 'per_hour'):
                default_rate = daily_rate or source_rate
            else:
                default_rate = source_rate or daily_rate
            rate_raw = request.form.get(f'transfer_rate_{item.id}')
            if rate_raw is None or not str(rate_raw).strip():
                rate = default_rate
            else:
                rate = max(0.0, _flt(rate_raw, default_rate))
            if billing_type == 'no_charge':
                rate = 0.0
            moved.append({
                'item': item,
                'source': source_by_item[int(item.id)],
                'tool_id': int(item.tool_id),
                'name': item.tool.name if item.tool else f'Tool#{item.tool_id}',
                'qty': qty,
                'rate': rate,
                'amount': qty * rate if billing_type != 'no_charge' else 0.0,
                'serials': chosen_serials,
            })
        if not moved:
            flash('Select at least one tool with a quantity still out to transfer.', 'danger')
            return redirect(url_for('hdc_tool_rental_new'))
        moved_qty = sum(line['qty'] for line in moved)
        new_amount = sum(line['amount'] for line in moved)

        # Everything leaving one rental travels as that rental's own hand-over,
        # so each contributing holder keeps its own row in its own history.
        moved_by_source = {}
        for line in moved:
            moved_by_source.setdefault(int(line['source'].id), []).append(line)
        from_parts = {}
        for rental, location in sources:
            if int(rental.id) in moved_by_source:
                from_parts[int(rental.id)] = _transfer_from_parts(rental, location)

        if to_type == 'internal':
            project = db.session.get(Project, to_project_id)
            to_label = project.name
            if to_stage_id:
                stage = db.session.get(Stage, to_stage_id)
                if stage:
                    to_label += f' > {stage.name}'
        else:
            to_label = to_customer_name or 'External Customer'

        # ---- settle the previous holder(s)' outstanding rent ----
        settle_choice = (request.form.get('settle_choice') or 'pay_now').strip().lower()
        if settle_choice not in ('pay_now', 'add_credit'):
            settle_choice = 'pay_now'
        finalized_amount = 0.0
        credit_amount = 0.0
        recv_acc = None
        for rental, _location in sources:
            if int(rental.id) not in moved_by_source:
                continue
            recalc_rental_totals(rental.id)
            outstanding = (0.0 if (rental.billing_type or '').lower() == 'no_charge'
                           else float(rental.total_pending_amount or 0))
            if outstanding <= 0.001:
                continue
            if settle_choice == 'add_credit':
                # Nothing is collected now: the previous holder keeps the full
                # balance owed, even when some tools stay with them.
                rental.payment_status = 'credit'
                credit_amount += outstanding
                continue
            if recv_acc is None:
                from hdc.services.accounts import _accounts_default_company_cash
                acc_id = request.form.get('received_to_account_id', type=int)
                candidate = db.session.get(Account, int(acc_id)) if acc_id else None
                if (not candidate) or candidate.is_void \
                        or str(candidate.status or 'active').lower() != 'active' \
                        or str(candidate.type or '').lower() not in ('company', 'cash', 'bank'):
                    candidate = _accounts_default_company_cash()
                if not candidate:
                    db.session.rollback()
                    flash('Choose a Cash/Bank account to receive the finalised rent before transferring.', 'danger')
                    return redirect(url_for('hdc_tool_rental_new'))
                recv_acc = candidate
            payment = ToolRentalPayment(
                rental_id=rental.id, return_id=None, payment_date=txn_date,
                amount=outstanding,
                payment_mode=(request.form.get('payment_mode') or 'cash').strip().lower(),
                received_to_account_id=int(recv_acc.id),
                reference=(request.form.get('payment_reference') or '').strip(),
                notes=f'Rent finalised on transfer to {to_label}',
                created_by=current_user.id if hasattr(current_user, 'id') else None,
            )
            db.session.add(payment)
            db.session.flush()
            ok_acc, msg_acc, _ = post_tool_rental_payment_to_accounts(
                payment, rental=rental, commit=False)
            if not ok_acc:
                db.session.rollback()
                flash(msg_acc or 'Unable to post the finalised rent to the party ledger.', 'danger')
                return redirect(url_for('hdc_tool_rental_new'))
            finalized_amount += outstanding

        # ---- record the hand-overs, including their per-tool splits ----
        handovers = {}
        for rental, location in sources:
            source_id = int(rental.id)
            lines = moved_by_source.get(source_id)
            if not lines:
                continue
            from_type, from_project_id, from_stage_id, from_customer_name, from_label = \
                from_parts[source_id]
            transfer = ToolRentalTransfer(
                rental_id=rental.id,
                from_type=from_type,
                from_project_id=from_project_id,
                from_stage_id=from_stage_id,
                from_customer_name=from_customer_name,
                from_location_label=from_label,
                to_type='site' if to_type == 'internal' else 'customer',
                to_project_id=to_project_id,
                to_stage_id=to_stage_id,
                to_customer_name=to_customer_name,
                to_location_label=to_label,
                qty_transferred=sum(line['qty'] for line in lines),
                transfer_date=txn_date,
                notes=(operator_notes or 'Selected tools transferred to a new rental cycle'),
                created_by=current_user.id if hasattr(current_user, 'id') else None,
            )
            db.session.add(transfer)
            db.session.flush()
            record_transfer_items(transfer, [(line['item'], line['qty']) for line in lines])
            handovers[source_id] = (transfer, from_label)

        # Selected lines leave their holder; unselected lines remain pending
        # there and can be transferred or returned later.
        for line in moved:
            item = line['item']
            rental = line['source']
            transfer, from_label = handovers[int(rental.id)]
            take = min(line['qty'], max(0.0, float(item.qty_pending or 0)))
            item.qty_returned = float(item.qty_returned or 0) + take
            item.qty_pending = max(0.0, float(item.qty_rented or 0) - item.qty_returned)
            movement_type = 'external_transfer' if to_type == 'external' else 'site_transfer'
            create_movement_log(
                tool_id=line['tool_id'], rental_id=rental.id,
                movement_type=movement_type,
                from_label=from_label, to_label=to_label, qty=take,
                transfer_id=transfer.id,
                notes=f'Transferred out of {rental.rental_code} to {to_label}',
            )

        # ---- start the destination's new rental cycle with every moved line ----
        source_codes = ' + '.join(lines[0]['source'].rental_code
                                  for lines in moved_by_source.values())
        new_code = _next_rental_code()
        new_rental = ToolRental(
            rental_code=new_code, renter_type=to_type,
            project_id=to_project_id, stage_id=to_stage_id,
            customer_name=to_customer_name, customer_phone=to_customer_phone,
            customer_address=to_customer_address,
            rental_date=txn_date, expected_return_date=exp_date,
            billing_type=billing_type,
            billing_notes=(request.form.get('billing_notes') or '').strip(),
            total_rented_qty=moved_qty,
            total_amount=new_amount,
            total_paid=0.0, total_returned_qty=0.0,
            status='active',
            payment_status='no_charge' if billing_type == 'no_charge' else 'unpaid',
            notes=(f'{operator_notes} | Transferred from {source_codes}'
                   if operator_notes else f'Transferred from {source_codes}'),
            created_by=current_user.id if hasattr(current_user, 'id') else None,
        )
        db.session.add(new_rental)
        db.session.flush()
        for line in moved:
            rental = line['source']
            transfer, from_label = handovers[int(rental.id)]
            sn_list = [s.serial_number for s in line.get('serials', [])]
            item_note = f'From transfer of {rental.rental_code}'
            if sn_list:
                item_note += f" [{', '.join(sn_list)}]"
            new_item = ToolRentalItem(
                rental_id=new_rental.id,
                tool_id=line['tool_id'],
                qty_rented=line['qty'], qty_returned=0.0, qty_pending=line['qty'],
                rate=line['rate'], amount=line['amount'],
                notes=item_note[:300],
            )
            db.session.add(new_item)
            db.session.flush()
            movement_type = 'external_transfer' if to_type == 'external' else 'site_transfer'
            if line.get('serials'):
                transfer_serials(
                    serial_ids=[s.id for s in line['serials']],
                    from_label=from_label,
                    to_label=to_label,
                    transfer_id=transfer.id,
                    rental_id=new_rental.id,
                    rental_item_id=new_item.id,
                    movement_type=movement_type,
                    notes=f'Transferred from {rental.rental_code} ({from_label}) to {new_code} ({to_label})',
                    created_by=current_user.id if hasattr(current_user, 'id') else None,
                    commit=False,
                )
            create_movement_log(
                tool_id=line['tool_id'], rental_id=new_rental.id,
                movement_type='rental_out',
                from_label=from_label, to_label=to_label, qty=line['qty'],
                transfer_id=transfer.id,
                notes=f'New rental {new_code} from transfer of {rental.rental_code}',
            )

        for lines in moved_by_source.values():
            rental = lines[0]['source']
            transfer = handovers[int(rental.id)][0]
            transfer.to_rental_id = new_rental.id
            transfer.notes = (f'{operator_notes} | Transferred selected tools to new rental {new_code}'
                              if operator_notes else
                              f'Transferred selected tools to new rental {new_code}')

        if to_type == 'external' and to_customer_name:
            ensure_party(to_customer_name, party_type='rental', phone=to_customer_phone)

        for lines in moved_by_source.values():
            rental = lines[0]['source']
            recalc_rental_totals(rental.id)
            if credit_amount > 0 and float(rental.total_pending_amount or 0) > 0.001:
                rental.payment_status = 'credit'
            line_qty = sum(line['qty'] for line in lines)
            add = f'Transferred {line_qty:g} tools to {to_label} (new rental {new_code}).'
            src_note = (rental.notes or '').strip()
            rental.notes = (src_note + ' ' + add).strip() if src_note else add

        marked = [lines[0]['source'] for lines in moved_by_source.values()]
        moved_desc_parts = []
        for line in moved:
            sn_list = [s.serial_number for s in line.get('serials', [])]
            if sn_list and len(sn_list) <= 5:
                moved_desc_parts.append(f"{line['name']} x{line['qty']:g} ({', '.join(sn_list)})")
            else:
                moved_desc_parts.append(f"{line['name']} x{line['qty']:g}")
        moved_desc = ', '.join(moved_desc_parts)
        db.session.commit()

        chain_str = ' > '.join(rental_transfer_chain(new_rental.id))
        holder_word = 'holder' if len(marked) == 1 else 'holders'
        if credit_amount > 0:
            settle_txt = f' {credit_amount:,.0f} PKR moved to credit for the previous {holder_word}.'
        elif finalized_amount > 0:
            settle_txt = f' Finalised {finalized_amount:,.0f} PKR rent from the previous {holder_word}.'
        else:
            settle_txt = ''
        flash(f'Transfer complete: {source_codes} → {new_code} for {to_label} '
              f'({moved_qty:g} tools: {moved_desc}).{settle_txt} '
              f'Chain: {chain_str}. New rental shown below.', 'success')
        return redirect(url_for('hdc_tool_rental_new', created=new_rental.id))


    # ------------------ CREATE RENTAL ------------------
    @app.route('/hdc/tool-rental/create', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_tool_rental_create():
        # One form, two jobs: a brand-new rental, or a transfer that settles the
        # previous holder and starts a fresh cycle for the destination.
        txn_type = (request.form.get('txn_type') or 'new_rental').strip().lower()
        if txn_type == 'transfer':
            return _create_transfer_rental()

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
        row_serials_list = request.form.getlist('row_serial_ids[]') or request.form.getlist('row_serial_ids')
        selected_serials_raw = (request.form.get('selected_serials') or '').strip()

        # Parse global selected_serials (if submitted from Individual Serials picker)
        global_serial_ids = []
        if selected_serials_raw:
            for tok in selected_serials_raw.split(','):
                tok = tok.strip()
                if tok.isdigit():
                    sid = int(tok)
                    if sid not in global_serial_ids:
                        global_serial_ids.append(sid)

        # If the user submitted via the Individual Serials picker and left tool_id[] empty,
        # build rows grouped by tool_id from the selected serials.
        non_empty_tool_ids = [t for t in tool_ids if str(t or '').strip()]
        if not non_empty_tool_ids and global_serial_ids:
            grouped_by_tid = {}
            for sid in global_serial_ids:
                s_obj = db.session.get(ToolSerial, sid)
                if not s_obj or getattr(s_obj, 'is_scrapped', False):
                    flash('One of the selected tool serials is invalid.', 'danger')
                    return redirect(url_for('hdc_tool_rental_new'))
                if not s_obj.is_in_store:
                    flash(f'{s_obj.serial_number} is not available in store (currently at {s_obj.current_location_label}).', 'danger')
                    return redirect(url_for('hdc_tool_rental_new'))
                grouped_by_tid.setdefault(int(s_obj.tool_id), []).append(sid)
            tool_ids = [str(tid) for tid in grouped_by_tid.keys()]
            qtys = [str(len(sids)) for sids in grouped_by_tid.values()]
            rates = []
            notes_list = []
            row_serials_list = [','.join(str(sid) for sid in sids) for sids in grouped_by_tid.values()]

        if not tool_ids or len(tool_ids)!=len(qtys):
            flash('Add at least one tool item.', 'danger')
            return redirect(url_for('hdc_tool_rental_new'))

        parsed_items = []
        requested_by_tool = {}
        claimed_serial_ids = set()
        total_rented_qty = 0.0
        total_amount = 0.0
        for idx, (tid_raw, qty_raw) in enumerate(zip(tool_ids, qtys)):
            # Skip completely blank extra rows if at least one row is valid
            if not str(tid_raw or '').strip() and len(tool_ids) > 1:
                continue
            try:
                tid = int(tid_raw)
            except:
                flash(f'Invalid tool at row {idx+1}.', 'danger')
                return redirect(url_for('hdc_tool_rental_new'))
            tool = Tool.query.get(tid)
            if not tool or tool.is_void:
                flash(f'Tool not found at row {idx+1}.', 'danger')
                return redirect(url_for('hdc_tool_rental_new'))

            ensure_tool_serials(tool=tool, commit=False)

            # Parse explicit serial markings selected for this row (e.g. Shovel No 5, Shovel No 6)
            raw_row_sids = row_serials_list[idx] if idx < len(row_serials_list) else ''
            explicit_sids = []
            for tok in str(raw_row_sids or '').split(','):
                tok = tok.strip()
                if tok.isdigit():
                    sid = int(tok)
                    if sid not in explicit_sids:
                        explicit_sids.append(sid)

            row_serials = []
            if explicit_sids:
                for sid in explicit_sids:
                    s_obj = db.session.get(ToolSerial, sid)
                    if not s_obj or s_obj.tool_id != tool.id or getattr(s_obj, 'is_scrapped', False):
                        flash(f'Invalid serial number selected for {tool.name} at row {idx+1}.', 'danger')
                        return redirect(url_for('hdc_tool_rental_new'))
                    if not s_obj.is_in_store:
                        flash(f'{s_obj.serial_number} is not available in store (currently out at {s_obj.current_location_label}).', 'danger')
                        return redirect(url_for('hdc_tool_rental_new'))
                    if sid in claimed_serial_ids:
                        flash(f'{s_obj.serial_number} was selected more than once.', 'danger')
                        return redirect(url_for('hdc_tool_rental_new'))
                    claimed_serial_ids.add(sid)
                    row_serials.append(s_obj)
                qty = float(len(row_serials))
            else:
                qty = max(0.0, _flt(qty_raw))
                if qty <= 0:
                    flash(f'Quantity must be >0 at row {idx+1}.', 'danger')
                    return redirect(url_for('hdc_tool_rental_new'))
                # Auto-assign the first `qty` available in-store serial markings
                in_store_candidates = [s for s in tool.in_store_serials if s.id not in claimed_serial_ids]
                need_cnt = max(0, int(round(qty)))
                for s_obj in in_store_candidates[:need_cnt]:
                    claimed_serial_ids.add(s_obj.id)
                    row_serials.append(s_obj)

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
            if row_serials:
                sn_str = ', '.join(s.serial_number for s in row_serials)
                if sn_str not in note:
                    note = f"{note} [Serials: {sn_str}]".strip() if note else f"Serials: {sn_str}"
            parsed_items.append((tool, qty, rate, amount, note[:300], row_serials))
            total_rented_qty += qty
            total_amount += amount

        if not parsed_items:
            flash('Add at least one tool item.', 'danger')
            return redirect(url_for('hdc_tool_rental_new'))

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

        for tool, qty, rate, amount, note, row_serials in parsed_items:
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
            if row_serials:
                assign_serials_to_rental(
                    rental_id=rental.id,
                    serial_ids=[s.id for s in row_serials],
                    rental_item_id=item.id,
                    location_label=to_label,
                    notes=f'Rental {rental_code} out to {to_label}',
                    created_by=current_user.id if hasattr(current_user, 'id') else None,
                    commit=False,
                )
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
        stages = Stage.query.options(joinedload(Stage.project)).order_by(Stage.name.asc()).all()
        tools = Tool.query.filter(Tool.is_void==False).order_by(Tool.name.asc()).all()
        receiving_accounts = get_receiving_accounts()
        # (id, label) pairs for the searchable account combos on this page.
        receiving_account_options = [
            (acc.id, '%s (%s) - Bal: %s' % (acc.name, acc.type,
                                             '{:,.0f}'.format(acc.opening_balance or 0)))
            for acc in receiving_accounts
        ]

        pending_tools = float(rental.total_rented_qty or 0) - float(rental.total_returned_qty or 0)
        # Outstanding = earned - cash received - discounts granted, so the
        # figure on this page can never drift from the rental's own totals.
        pending_amount = float(rental.total_pending_amount or 0.0)

        # Discounts granted on this rental (cash never moved for them).
        discounts = rental_discount_rows(rental.id)
        discount_total = sum(float(d.amount or 0.0) for d in discounts if not d.is_void)

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

        # Serial markings currently out on this rental, grouped by rental_item_id
        rental_serials = get_serials_for_rental(rental.id)
        serials_by_item = {}
        unassigned_by_tool = {}
        for s_info in rental_serials:
            ri_id = s_info.get('rental_item_id')
            if ri_id:
                serials_by_item.setdefault(int(ri_id), []).append(s_info)
            else:
                unassigned_by_tool.setdefault(int(s_info['tool_id']), []).append(s_info)
        for it in items:
            slot = serials_by_item.setdefault(int(it.id), [])
            need = max(0, int(round(float(it.qty_pending or 0))) - len(slot))
            pool = unassigned_by_tool.get(int(it.tool_id), [])
            while need > 0 and pool:
                slot.append(pool.pop(0))
                need -= 1

        return render_template('tool_rental/tool_rental_detail.html',
            rental=rental,
            items=items,
            rental_serials=rental_serials,
            serials_by_item=serials_by_item,
            returns=returns,
            payments=payments,
            transfers=transfers,
            tracking_chain=tracking_chain,
            transfer_chain=rental_transfer_chain(rental.id),
            movement_logs=movement_logs,
            projects=projects,
            project_options=_tool_project_combo_options(projects),
            stages=stages,
            stage_options=_tool_stage_combo_options(stages),
            tools=tools,
            receiving_accounts=receiving_accounts,
            receiving_account_options=receiving_account_options,
            known_customers=known_tool_customers(),
            pending_tools=pending_tools,
            pending_amount=pending_amount,
            acct_links=acct_links,
            discounts=discounts,
            discount_total=discount_total,
            discount_reason_options=discount_reason_options(),
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

        for it in rental.items:
            if it.tool:
                ensure_tool_serials(tool=it.tool, commit=False)

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
        claimed_return_serial_ids = set()
        for idx, (ri_id_raw, qty_raw) in enumerate(zip(rental_item_ids, qty_returned_list)):
            try:
                ri_id = int(ri_id_raw)
            except:
                continue
            r_item = ToolRentalItem.query.get(ri_id)
            if not r_item or int(r_item.rental_id)!=int(rental.id):
                flash(f'Invalid rental item {ri_id_raw}.', 'danger')
                return redirect(url_for('hdc_tool_rental_detail', rental_id=rental.id))

            # Check if specific out-of-store serial markings were selected for return
            raw_ret_serials = (
                request.form.getlist(f'return_serial_ids_{r_item.id}[]')
                or request.form.getlist(f'return_serial_ids_{r_item.id}')
            )
            explicit_ret_sids = []
            for chunk in raw_ret_serials:
                for tok in str(chunk or '').split(','):
                    tok = tok.strip()
                    if tok.isdigit():
                        sid = int(tok)
                        if sid not in explicit_ret_sids:
                            explicit_ret_sids.append(sid)

            item_return_serials = []
            if return_type != 'full' and explicit_ret_sids:
                for sid in explicit_ret_sids:
                    s_obj = db.session.get(ToolSerial, sid)
                    if s_obj and not s_obj.is_in_store and s_obj.tool_id == r_item.tool_id and sid not in claimed_return_serial_ids:
                        claimed_return_serial_ids.add(sid)
                        item_return_serials.append(s_obj)
                qty_ret = float(len(item_return_serials))
            else:
                qty_ret = max(0.0, _flt(qty_raw))
                if qty_ret > 0:
                    candidates = get_out_of_store_serials(
                        tool_id=r_item.tool_id,
                        rental_id=rental.id,
                        rental_item_id=r_item.id,
                    )
                    for s_obj in candidates:
                        if s_obj.id not in claimed_return_serial_ids and len(item_return_serials) < int(round(qty_ret)):
                            claimed_return_serial_ids.add(s_obj.id)
                            item_return_serials.append(s_obj)

            if qty_ret <= 0:
                continue
            if qty_ret > float(r_item.qty_pending or 0) + 0.001:
                flash(f'Return qty {qty_ret} exceeds pending {r_item.qty_pending} for {r_item.tool.name}.', 'danger')
                return redirect(url_for('hdc_tool_rental_detail', rental_id=rental.id))
            cond_note = (condition_notes_list[idx] if idx < len(condition_notes_list) else '').strip()
            if item_return_serials:
                sn_str = ', '.join(s.serial_number for s in item_return_serials)
                if sn_str not in cond_note:
                    cond_note = f"{cond_note} [Serials: {sn_str}]".strip() if cond_note else f"Serials: {sn_str}"
            parsed_returns.append((r_item, qty_ret, cond_note[:300], item_return_serials))
            total_tools_returned += qty_ret

        if total_tools_returned <= 0:
            flash('No tools marked for return.', 'warning')
            return redirect(url_for('hdc_tool_rental_detail', rental_id=rental.id))

        amount_paid_raw = (request.form.get('amount_paid') or '0').strip()
        amount_paid = max(0.0, _flt(amount_paid_raw))

        if payment_type == 'full':
            # Discount-aware: "pay everything still owed" means the outstanding
            # balance, which discounts have already reduced.
            pending_amt = float(rental.total_pending_amount or 0.0)
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

        for r_item, qty_ret, cond_note, item_return_serials in parsed_returns:
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
            if item_return_serials:
                return_serials_from_rental(
                    rental_id=rental.id,
                    serial_ids=[s.id for s in item_return_serials],
                    return_id=ret_rec.id,
                    created_by=current_user.id if hasattr(current_user, 'id') else None,
                    commit=False,
                )
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

        # A return can settle the bill partly with cash and partly with a
        # discount ("forget the last 2,000"), so read it before the payment.
        discount_amt = max(0.0, _flt(request.form.get('discount_amount')))
        discount_reason = (request.form.get('discount_reason') or 'goodwill').strip().lower()
        discount_notes = (request.form.get('discount_notes') or '').strip()

        pay_record = None
        if (amount_paid > 0 or discount_amt > 0) and rental.billing_type!='no_charge':
            pay_record = ToolRentalPayment(
                rental_id=rental.id,
                return_id=ret_rec.id,
                payment_date=return_date,
                amount=amount_paid,
                discount=discount_amt,
                payment_mode=(request.form.get('payment_mode') or 'cash').strip().lower(),
                received_to_account_id=int(recv_acc.id) if recv_acc else None,
                reference=(request.form.get('payment_reference') or '').strip(),
                notes=f'Payment on return {ret_rec.id}',
                created_by=current_user.id if hasattr(current_user,'id') else None
            )
            db.session.add(pay_record)
            db.session.flush()
            # post the cash leg to accounts
            if amount_paid > 0:
                ok_acc, msg_acc, _ = post_tool_rental_payment_to_accounts(pay_record, rental=rental, commit=False)
                if not ok_acc:
                    db.session.rollback()
                    flash(msg_acc or 'Unable to post rental payment in accounts.', 'danger')
                    return redirect(url_for('hdc_tool_rental_detail', rental_id=rental.id))
            # ...and the discount leg, so the receivable shrinks by both
            if discount_amt > 0:
                _drow, ok_disc, msg_disc = record_tool_rental_discount(
                    rental.id, discount_amt, discount_date=return_date,
                    reason=discount_reason, notes=discount_notes,
                    payment_id=pay_record.id, return_id=ret_rec.id,
                    created_by=current_user.id if hasattr(current_user,'id') else None,
                    commit=False)
                if not ok_disc:
                    db.session.rollback()
                    flash(msg_disc or 'Unable to post rental discount.', 'danger')
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

        # Optional discount granted in the same settlement: cash + discount
        # together clear the balance, but only the cash reaches an account.
        discount_amt = max(0.0, _flt(request.form.get('discount')))
        discount_reason = (request.form.get('discount_reason') or 'goodwill').strip().lower()
        discount_notes = (request.form.get('discount_notes') or '').strip()
        if discount_amt > 0:
            # Cash + concession together may not exceed the bill.
            outstanding = float(rental.total_pending_amount or 0.0)
            if discount_amt > max(0.0, outstanding - amount) + 0.001:
                flash(f'Discount {discount_amt:,.2f} plus this {amount:,.2f} payment exceeds '
                      f'the outstanding {outstanding:,.2f} PKR.', 'danger')
                return redirect(url_for('hdc_tool_rental_detail', rental_id=rental.id))

        pay = ToolRentalPayment(
            rental_id=rental.id,
            payment_date=pay_date,
            amount=amount,
            discount=discount_amt,
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

        disc_row = None
        if discount_amt > 0:
            disc_row, ok_disc, msg_disc = record_tool_rental_discount(
                rental.id, discount_amt, discount_date=pay_date,
                reason=discount_reason, notes=discount_notes,
                payment_id=pay.id,
                created_by=current_user.id if hasattr(current_user,'id') else None,
                commit=False)
            if not ok_disc:
                db.session.rollback()
                flash(msg_disc or 'Unable to post discount in accounts.', 'danger')
                return redirect(url_for('hdc_tool_rental_detail', rental_id=rental.id))

        recalc_rental_totals(rental.id)
        db.session.commit()
        msg = f'Payment {amount:.2f} PKR received in {recv_acc.name} (Cash/Bank).'
        if disc_row is not None:
            msg += f' Discount {disc_row.amount:,.2f} PKR granted ({disc_row.reason_label}).'
        msg += f' Pending: {rental.total_pending_amount:.2f}'
        flash(msg, 'success')
        return redirect(url_for('hdc_tool_rental_detail', rental_id=rental.id))

    @app.route('/hdc/tool-rental/payment/<int:payment_id>/void', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_tool_rental_payment_void(payment_id):
        pay = ToolRentalPayment.query.get_or_404(payment_id)
        if pay.is_void:
            flash('Payment already voided.', 'info')
            return redirect(url_for('hdc_tool_rental_detail', rental_id=pay.rental_id))
        reason = (request.form.get('void_reason') or '').strip() or 'Voided by user'
        pay.is_void = True
        pay.void_reason = reason
        pay.voided_at = _pkt_now_naive()
        void_tool_rental_payment_in_accounts(pay.id)
        # A discount granted alongside this payment goes with it: leaving it
        # alive would keep the bill settled for money the customer never paid.
        voided_discounts = void_discounts_for_payment(pay.id, reason=reason)
        recalc_rental_totals(pay.rental_id)
        db.session.commit()
        msg = 'Payment voided and removed from accounts.'
        if voided_discounts:
            msg += f' {voided_discounts} linked discount(s) voided with it.'
        flash(msg, 'warning')
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
            note=((pay.notes or f'Rental {rental.rental_code}')
                  + (f' | Discount granted: {float(pay.discount or 0.0):,.2f} PKR (no cash)'
                     if float(pay.discount or 0.0) > 0 else '')),
            reference_id=f'tool_rental_payment#{pay.id}',
            recent_entries=[],
            recent_entries_title='',
            back_url=url_for('hdc_tool_rental_detail', rental_id=rental.id),
            print_label='Print / Save PDF'
        )

    # ------------------ DISCOUNT / WAIVE OFF ------------------
    @app.route('/hdc/tool-rental/<int:rental_id>/discount', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_tool_rental_add_discount(rental_id):
        """Grant a standalone discount (no cash involved) on a rental.

        This is the "waive off the rest" action: the bill shrinks, nothing is
        received into Cash/Bank, and the concession is posted to Accounts on
        its own so the ledger can always explain the missing money.
        """
        rental = ToolRental.query.get_or_404(rental_id)
        if rental.billing_type == 'no_charge':
            flash('This rental is No Charge (contract included) - there is nothing to discount.', 'info')
            return redirect(url_for('hdc_tool_rental_detail', rental_id=rental.id))
        if rental.is_void:
            flash('Rental is voided.', 'danger')
            return redirect(url_for('hdc_tool_rental_detail', rental_id=rental.id))

        amount = max(0.0, _flt(request.form.get('amount')))
        if amount <= 0:
            flash('Discount amount must be greater than zero.', 'danger')
            return redirect(url_for('hdc_tool_rental_detail', rental_id=rental.id))

        disc_date_raw = (request.form.get('discount_date') or '').strip()
        try:
            disc_date = datetime.strptime(disc_date_raw, '%Y-%m-%d').date() if disc_date_raw else _pkt_today()
        except Exception:
            disc_date = _pkt_today()

        reason = (request.form.get('reason') or 'goodwill').strip().lower()
        notes = (request.form.get('notes') or '').strip()

        row, ok_disc, msg_disc = record_tool_rental_discount(
            rental.id, amount, discount_date=disc_date, reason=reason, notes=notes,
            created_by=current_user.id if hasattr(current_user, 'id') else None,
            commit=False)
        if not ok_disc:
            db.session.rollback()
            flash(msg_disc or 'Unable to record discount.', 'danger')
            return redirect(url_for('hdc_tool_rental_detail', rental_id=rental.id))

        recalc_rental_totals(rental.id)
        db.session.commit()
        flash(f'Discount {row.amount:,.2f} PKR granted ({row.reason_label}). '
              f'Outstanding is now {rental.total_pending_amount:,.2f} PKR.', 'success')
        return redirect(url_for('hdc_tool_rental_detail', rental_id=rental.id))

    @app.route('/hdc/tool-rental/discount/<int:discount_id>/void', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_tool_rental_discount_void(discount_id):
        """Take a granted discount back — the amount is owed again."""
        row = db.session.get(ToolRentalDiscount, discount_id)
        if row is None:
            flash('Discount not found.', 'danger')
            return redirect(url_for('hdc_tool_rental'))
        rental_id = int(row.rental_id)
        if row.is_void:
            flash('Discount is already voided.', 'info')
            return redirect(url_for('hdc_tool_rental_detail', rental_id=rental_id))
        _row, ok_disc, msg_disc = void_tool_rental_discount(
            discount_id,
            reason=(request.form.get('void_reason') or '').strip(),
            commit=False)
        if not ok_disc:
            db.session.rollback()
            flash(msg_disc or 'Unable to void discount.', 'danger')
            return redirect(url_for('hdc_tool_rental_detail', rental_id=rental_id))
        recalc_rental_totals(rental_id)
        db.session.commit()
        flash(f'Discount of {row.amount:,.2f} PKR voided — it is owed again and '
              f'removed from accounts.', 'warning')
        return redirect(url_for('hdc_tool_rental_detail', rental_id=rental_id))

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
        for ri in rental_items:
            if ri.tool:
                ensure_tool_serials(tool=ri.tool, commit=False)

        # Split the transferred quantity across tools so the dashboard can say
        # *which* tool moved where (not just "20 pcs left Site1").  Per-tool
        # qty_transfer[] or site_transfer_serial_ids_<id>[] fields win; otherwise
        # allocate_transfer_qty fills the pending lines up to the requested total.
        per_item_raw = request.form.getlist('qty_transfer[]') or request.form.getlist('qty_transfer')
        item_id_raw = request.form.getlist('rental_item_id[]') or request.form.getlist('rental_item_id')
        allocations = []
        serials_for_item_transfer = {}
        claimed_site_serials = set()

        by_id = {int(ri.id): ri for ri in rental_items}
        # First check if explicit out-of-store serial markings were selected per item
        for ri in rental_items:
            raw_sids = (
                request.form.getlist(f'transfer_serial_ids_{ri.id}[]')
                or request.form.getlist(f'transfer_serial_ids_{ri.id}')
                or request.form.getlist(f'site_transfer_serial_ids_{ri.id}[]')
                or request.form.getlist(f'site_transfer_serial_ids_{ri.id}')
            )
            chosen_sids = []
            for chunk in raw_sids:
                for tok in str(chunk or '').split(','):
                    tok = tok.strip()
                    if tok.isdigit():
                        sid = int(tok)
                        s_obj = db.session.get(ToolSerial, sid)
                        if s_obj and not s_obj.is_in_store and s_obj.tool_id == ri.tool_id and sid not in claimed_site_serials:
                            claimed_site_serials.add(sid)
                            chosen_sids.append(s_obj)
            if chosen_sids:
                serials_for_item_transfer[int(ri.id)] = chosen_sids

        if per_item_raw and len(per_item_raw) == len(item_id_raw):
            for ri_id_raw, qty_raw in zip(item_id_raw, per_item_raw):
                try:
                    ri = by_id.get(int(ri_id_raw))
                except (TypeError, ValueError):
                    ri = None
                if not ri:
                    continue
                chosen_s = serials_for_item_transfer.get(int(ri.id), [])
                if chosen_s:
                    take = min(float(len(chosen_s)), float(ri.qty_pending or 0))
                else:
                    take = max(0.0, _flt(qty_raw))
                    take = min(take, float(ri.qty_pending or 0))
                if take > 0:
                    allocations.append((ri, take))
        elif serials_for_item_transfer:
            for ri_id, chosen_s in serials_for_item_transfer.items():
                ri = by_id.get(int(ri_id))
                if ri:
                    take = min(float(len(chosen_s)), float(ri.qty_pending or 0))
                    if take > 0:
                        allocations.append((ri, take))
        if not allocations:
            allocations = allocate_transfer_qty(
                [(ri, float(ri.qty_pending or 0)) for ri in rental_items], qty_transferred)

        moved_qty = 0.0
        moved_tools = []
        movement_type = 'site_transfer' if to_type=='site' else 'external_transfer'
        for ri, take in allocations:
            moved_qty += float(take)
            chosen_s = serials_for_item_transfer.get(int(ri.id), [])
            if not chosen_s:
                candidates = get_out_of_store_serials(
                    tool_id=ri.tool_id,
                    rental_id=rental.id,
                    rental_item_id=ri.id,
                )
                for s_obj in candidates:
                    if s_obj.id not in claimed_site_serials and len(chosen_s) < int(round(float(take))):
                        claimed_site_serials.add(s_obj.id)
                        chosen_s.append(s_obj)
            if chosen_s:
                transfer_serials(
                    serial_ids=[s.id for s in chosen_s[:int(round(float(take)))]],
                    from_label=from_label,
                    to_label=to_label,
                    transfer_id=transfer.id,
                    rental_id=rental.id,
                    rental_item_id=ri.id,
                    movement_type=movement_type,
                    notes=f'Transfer {from_label} > {to_label}',
                    created_by=current_user.id if hasattr(current_user, 'id') else None,
                    commit=False,
                )
                sn_txt = ', '.join(s.serial_number for s in chosen_s[:int(round(float(take)))])
                moved_tools.append(f'{ri.tool.name if ri.tool else ri.tool_id} x{take:g} ({sn_txt})')
            else:
                moved_tools.append(f'{ri.tool.name if ri.tool else ri.tool_id} x{take:g}')
            create_movement_log(
                tool_id=ri.tool_id,
                rental_id=rental.id,
                movement_type=movement_type,
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
        tool_locations = global_tool_locations(
            search_tool_id=tool_id, search_project_id=project_id, search_text=q)
        locations = tracking_location_groups(tool_locations, project_id=project_id)
        projects = Project.query.order_by(Project.name.asc()).all()
        tools = Tool.query.filter(Tool.is_void==False).order_by(Tool.name.asc()).all()
        kpis = tool_kpis()
        return render_template('tool_rental/tool_tracking.html',
            locations=locations,
            projects=projects,
            project_options=_tool_project_combo_options(projects),
            tools=tools,
            tool_options=_tool_picker_options(tools),
            selected_tool=tool_id,
            selected_project=project_id,
            q=q,
            kpis=kpis
        )

    # ------------------ PHYSICAL AUDIT: what the sites actually counted ------------------
    def _audit_counts_from_form(form):
        """Turn ``counted[<tool_id>]`` boxes into the service payload.

        A blank box and a typed ``0`` mean different things: blank is "nobody
        counted this yet", zero is "we counted the shelf and it is empty".  Only
        the second one produces a shortage, so the two are kept apart all the way
        into ``hdc_tool_audit_line.counted_qty``.
        """
        counts = {}
        for name, value in (form.items() if hasattr(form, 'items') else []):
            name = name or ''
            if '[' not in name:
                continue
            field, _, raw_id = name.partition('[')
            raw_id = raw_id.rstrip(']')
            if not raw_id.isdigit():
                continue
            text = (value or '').strip()
            slot = counts.setdefault(int(raw_id), {})
            if field == 'counted':
                slot['counted'] = text or None
            elif field == 'damaged':
                slot['damaged'] = text or 0
            elif field == 'notes':
                slot['notes'] = text
        return counts

    def _audit_write_allowed():
        """Who may post an adjustment: finance roles, or an explicit grant."""
        role = (getattr(current_user, 'role', '') or '').strip().lower()
        granted = may_access_path(current_user, request.path, 'write')
        if granted is None:
            return role in ('admin', 'accountant')
        return bool(granted)

    @app.route('/hdc/tool-rental/audit')
    @login_required
    def hdc_tool_rental_audit():
        """All tools: total owned vs where they are vs what each site counted.

        One row per tool item.  ``Where the book says it is`` is the tracker's
        own position (so this page can never disagree with the dashboard);
        ``Counted`` is what the physical sheets say, and only for the places a
        sheet actually covers.  Anything that does not match is listed first.
        """
        ledger = tool_ledger()
        cards = audit_locations(ledger)
        selected = (request.args.get('location') or '').strip() or None
        if selected and not any(card['key'] == selected for card in cards):
            selected = None
        visible = [card for card in cards if card['key'] == selected] if selected else cards
        matrix = audit_matrix(
            ledger=ledger, locations=visible,
            term=(request.args.get('q') or '').strip() or None,
            category_id=request.args.get('category_id', type=int),
            only_discrepancies=(request.args.get('only') or '').strip() == 'variance')

        return render_template('tool_rental/tool_audit.html',
            ledger=ledger, totals=ledger['totals'],
            cards=cards, selected_location=selected,
            rows=matrix['rows'], matrix_totals=matrix['totals'],
            summary=audit_summary(ledger=ledger, locations=cards),
            history=audit_history(limit=25, location_key_filter=selected),
            categories=ToolCategory.query.order_by(ToolCategory.name.asc()).all(),
            location_options=[(card['key'],
                                f"{card['label']} ({card['expected_qty']:g} "
                                f"{('expected' if card['expected_qty'] else 'nothing')})")
                               for card in cards],
            selected_location_label=(visible[0]['label'] if selected and visible else ''),
            q=(request.args.get('q') or '').strip(),
            selected_category=request.args.get('category_id', type=int),
            only=(request.args.get('only') or '').strip(),
            can_adjust=_audit_write_allowed(),
            audit_open_count=open_audit_count(),
            scrap_reasons=AUDIT_LOSS_REASONS,
            today=_pkt_today().isoformat(),
        )

    @app.route('/hdc/tool-rental/audit/count')
    @login_required
    def hdc_tool_rental_audit_new():
        """A count sheet for a place nobody has counted yet.

        Opening it does not create anything: a site that was only *looked at*
        must not leave a half-empty audit record behind.  The first saved number
        (or *Start empty*) creates the sheet.
        """
        key = (request.args.get('location') or LOC_STORE).strip()
        spec = parse_location_key(key)
        existing = pending_audit_for(spec)
        if existing is not None:
            return redirect(url_for('hdc_tool_rental_audit_sheet', audit_id=int(existing.id)))
        sheet = audit_sheet(spec=spec)
        return render_template('tool_rental/tool_audit_sheet.html',
            tools_active='audit',
            sheet=sheet, audit=None, spec=sheet['spec'], plan=[],
            movements=[], history=audit_history(limit=10, location_key_filter=spec['key']),
            can_adjust=_audit_write_allowed(),
            audit_open_count=open_audit_count(),
            scrap_reasons=AUDIT_LOSS_REASONS,
            today=_pkt_today().isoformat(),
        )

    @app.route('/hdc/tool-rental/audit/<int:audit_id>')
    @login_required
    def hdc_tool_rental_audit_sheet(audit_id):
        """One physical count: entry sheet, discrepancy report, adjust action."""
        audit = db.session.get(ToolAudit, int(audit_id))
        if audit is None or audit.is_void:
            flash('Audit not found.', 'danger')
            return redirect(url_for('hdc_tool_rental_audit'))
        sheet = audit_sheet(audit=audit)
        plan = plan_adjustments(audit) if audit.is_open else []
        return render_template('tool_rental/tool_audit_sheet.html',
            tools_active='audit',
            sheet=sheet, audit=audit, spec=sheet['spec'], plan=plan,
            movements=audit_movement(audit),
            history=audit_history(limit=10, location_key_filter=sheet['spec']['key']),
            can_adjust=_audit_write_allowed(),
            audit_open_count=open_audit_count(),
            scrap_reasons=AUDIT_LOSS_REASONS,
            today=_pkt_today().isoformat(),
        )

    def _audit_redirect(audit_id):
        return redirect(url_for('hdc_tool_rental_audit_sheet', audit_id=int(audit_id)))

    @app.route('/hdc/tool-rental/audit/start', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_tool_rental_audit_start():
        """Create the sheet and save whatever was typed on the preview."""
        spec = parse_location_key((request.form.get('location') or LOC_STORE).strip())
        counts = _audit_counts_from_form(request.form)
        created, audit = start_audit(
            spec,
            counter_name=(request.form.get('counter_name') or '').strip(),
            audit_date=(request.form.get('audit_date') or '').strip(),
            reference=(request.form.get('reference') or '').strip(),
            notes=(request.form.get('notes') or '').strip(),
            counts=counts or None,
            user_id=getattr(current_user, 'id', None))
        if created:
            flash(f'Count sheet {audit.audit_code} started for {audit.location_label}.',
                  'success')
        else:
            flash(f'{audit.audit_code} is already open for this place — your numbers '
                  f'were saved onto that sheet instead of starting a second count.',
                  'info')
        if counts and (request.form.get('save_and_adjust') or '').strip():
            ok, message, summary = post_adjustments(
                audit, mode=(request.form.get('mode') or 'losses'),
                reason=(request.form.get('reason') or 'lost'),
                audit_date=(request.form.get('audit_date') or '').strip(),
                notes=(request.form.get('adjust_notes') or '').strip(),
                user_id=getattr(current_user, 'id', None))
            _audit_adjust_flash(ok, message, summary)
        return _audit_redirect(audit.id)

    def _audit_adjust_flash(ok, message, summary):
        """One wording for every adjust action, so the flash never undersells a loss."""
        summary = summary or {}
        if ok and not summary.get('done'):
            flash(summary.get('message') or message or
                  'No discrepancy to adjust — the count is closed as verified.', 'info')
            return True
        if not ok:
            flash(message or 'Nothing could be adjusted on this count.', 'danger')
            return False
        parts = []
        if summary.get('losses'):
            parts.append(f"{summary.get('lost_qty', 0):g} piece(s) written off "
                         f"({summary.get('write_off_value', 0):,.0f} PKR)")
        if summary.get('gains'):
            parts.append(f"+{summary.get('found_qty', 0):g} pcs added to stock")
        tail = (f"The sheet stays open — {summary.get('remaining')} finding(s) left unposted."
                if summary.get('remaining') else 'Owned stock now matches the count.')
        flash(f"Adjustments posted: {', '.join(parts) or 'stock corrected'}. {tail}", 'success')
        for remark in summary.get('remarks') or []:
            flash(remark, 'warning')
        return True

    @app.route('/hdc/tool-rental/audit/<int:audit_id>/save', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_tool_rental_audit_save(audit_id):
        audit = db.session.get(ToolAudit, int(audit_id))
        if audit is None or not audit.is_open:
            flash('This count can no longer be edited.', 'danger')
            return _audit_redirect(audit_id)
        ok, message, _ = save_counts(
            audit, _audit_counts_from_form(request.form),
            meta={
                'counter_name': (request.form.get('counter_name') or '').strip()[:150] or None,
                'reference': (request.form.get('reference') or '').strip()[:120] or None,
                'notes': (request.form.get('notes') or '').strip()[:500] or None,
                'audit_date': _parse_date(request.form.get('audit_date')) or _pkt_today(),
            },
            user_id=getattr(current_user, 'id', None))
        if not ok:
            flash(message, 'danger')
            return _audit_redirect(audit_id)
        flash(f'Count saved on {audit.audit_code}: {audit.counted_lines:g} of '
              f'{audit.total_lines:g} line(s) entered, {audit.discrepancy_lines:g} '
              f'discrepancie(s).', 'success')
        return _audit_redirect(audit_id)

    @app.route('/hdc/tool-rental/audit/<int:audit_id>/adjust', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_tool_rental_audit_adjust(audit_id):
        """The button: turn the counted discrepancies into real stock numbers."""
        audit = db.session.get(ToolAudit, int(audit_id))
        if audit is None or not audit.is_open:
            flash('This count is closed, so it cannot be adjusted.', 'danger')
            return _audit_redirect(audit_id)
        if (request.form.get('confirm') or '').strip() != 'ADJUST':
            flash('Type ADJUST in the confirm box to change stock — a write-off '
                  'cannot be undone from this page.', 'warning')
            return _audit_redirect(audit_id)
        ok, message, summary = post_adjustments(
            audit,
            mode=(request.form.get('mode') or 'losses'),
            reason=(request.form.get('reason') or 'lost'),
            audit_date=(request.form.get('adjust_date') or request.form.get('audit_date') or '').strip(),
            notes=(request.form.get('adjust_notes') or '').strip(),
            user_id=getattr(current_user, 'id', None))
        _audit_adjust_flash(ok, message, summary)
        return _audit_redirect(audit_id)

    @app.route('/hdc/tool-rental/audit/<int:audit_id>/close', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_tool_rental_audit_close(audit_id):
        """File a clean count (nothing changes in stock)."""
        audit = db.session.get(ToolAudit, int(audit_id))
        ok, message = close_audit(audit, note=(request.form.get('close_note') or '').strip(),
                                  user_id=getattr(current_user, 'id', None))
        flash(message, 'success' if ok else 'warning')
        return _audit_redirect(audit_id)

    @app.route('/hdc/tool-rental/audit/<int:audit_id>/reopen', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_tool_rental_audit_reopen(audit_id):
        audit = db.session.get(ToolAudit, int(audit_id))
        ok, message = reopen_audit(audit)
        flash(message, 'success' if ok else 'warning')
        return _audit_redirect(audit_id)

    @app.route('/hdc/tool-rental/audit/<int:audit_id>/void', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_tool_rental_audit_void(audit_id):
        audit = db.session.get(ToolAudit, int(audit_id))
        if audit is None:
            flash('Audit not found.', 'danger')
            return redirect(url_for('hdc_tool_rental_audit'))
        ok, message = void_audit(audit, reason=(request.form.get('void_reason') or '').strip())
        flash(message, 'success' if ok else 'warning')
        return redirect(url_for('hdc_tool_rental_audit'))

    @app.route('/hdc/api/tool-rental/audit')
    @login_required
    def hdc_api_tool_audit():
        """Machine-readable audit: book position, counts and variances per place."""
        return jsonify(audit_rows_for_json())

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
        site_breakdown = {}
        report_rentals = []
        for r in rentals:
            if r.renter_type == 'internal':
                project_id = int(r.project_id or 0)
                key = ('site', project_id)
                if r.project:
                    label = r.project.name or f'Project #{project_id}'
                    client = (r.project.client or '').strip()
                else:
                    label = 'Internal — No Site' if not project_id else f'Project #{project_id}'
                    client = ''
                location_type = 'site'
            else:
                customer = (r.customer_name or 'External Customer').strip()
                key = ('customer', customer.casefold())
                label = customer
                client = ''
                location_type = 'customer'

            site = site_breakdown.setdefault(key, {
                'label': label,
                'client': client,
                'location_type': location_type,
                'rented': 0.0,
                'pending_tools': 0.0,
                'amount': 0.0,
                'paid': 0.0,
                'pending_amount': 0.0,
                'count': 0,
                'tool_map': {},
                'rentals': [],
            })
            rental_items = list(r.items)
            rental_detail = {
                'rental': r,
                'items': rental_items,
                'tool_count': len({int(item.tool_id) for item in rental_items}),
                'tracking_chain': list(r.tracking_chain),
            }
            report_rentals.append(rental_detail)
            site['rentals'].append(rental_detail)

            site['rented'] += float(r.total_rented_qty or 0)
            site['pending_tools'] += float(r.total_pending_tools or 0)
            site['amount'] += float(r.total_amount or 0) if r.billing_type != 'no_charge' else 0
            site['paid'] += float(r.total_paid or 0)
            site['pending_amount'] += float(r.total_pending_amount or 0)
            site['count'] += 1

            for item in rental_detail['items']:
                tid = int(item.tool_id)
                tool_row = tool_breakdown.setdefault(tid, {
                    'tool': item.tool, 'rented': 0.0, 'returned': 0.0,
                    'pending': 0.0, 'amount': 0.0,
                })
                tool_row['rented'] += float(item.qty_rented or 0)
                tool_row['returned'] += float(item.qty_returned or 0)
                tool_row['pending'] += float(item.qty_pending or 0)
                tool_row['amount'] += float(item.amount or 0)

                site_tool = site['tool_map'].setdefault(tid, {
                    'tool': item.tool, 'rented': 0.0, 'returned': 0.0,
                    'pending': 0.0, 'amount': 0.0, 'rental_ids': set(),
                })
                site_tool['rented'] += float(item.qty_rented or 0)
                site_tool['returned'] += float(item.qty_returned or 0)
                site_tool['pending'] += float(item.qty_pending or 0)
                site_tool['amount'] += float(item.amount or 0)
                site_tool['rental_ids'].add(int(r.id))

        for site in site_breakdown.values():
            site['tools'] = list(site.pop('tool_map').values())
            for row in site['tools']:
                row['rental_count'] = len(row.pop('rental_ids'))
            site['tools'].sort(
                key=lambda row: ((getattr(row['tool'], 'name', '') or '').casefold(),
                                 (getattr(row['tool'], 'tool_code', '') or '').casefold()),
            )
            site['tool_count'] = len(site['tools'])

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
            report_rentals=report_rentals,
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
            project_options=_tool_project_combo_options(projects),
            tools=tools,
            tool_options=_tool_picker_options(tools),
            known_customers=known_tool_customers(),
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

    # ------------------ SERIAL NUMBER API ROUTES ------------------

    @app.route('/hdc/api/tool-rental/serials/in-store')
    @login_required
    def hdc_api_serials_in_store():
        """Get all serials currently IN STORE for the New Rental data-driven picker.

        Optional query params:
            category_id: Filter by ToolCategory ID
            tool_id: Filter by Tool ID
        """
        category_id = request.args.get('category_id', type=int)
        tool_id = request.args.get('tool_id', type=int)
        serials = get_serials_in_store_summary(category_id=category_id, tool_id=tool_id)
        by_tool = {}
        categories_map = {}
        for s in serials:
            tid = s['tool_id']
            cid = s.get('category_id') or 0
            cname = s.get('category_name') or s['tool_name']
            categories_map[cid] = {'category_id': cid, 'category_name': cname}
            if tid not in by_tool:
                by_tool[tid] = {
                    'tool_id': tid,
                    'tool_name': s['tool_name'],
                    'tool_code': s['tool_code'],
                    'category_id': s.get('category_id'),
                    'category_name': cname,
                    'rate': s.get('rate', 0.0),
                    'serials': [],
                    'in_store_count': 0,
                }
            by_tool[tid]['serials'].append({
                'serial_id': s['serial_id'],
                'serial_number': s['serial_number'],
                'label': s['label'],
                'condition': s['condition'],
            })
            by_tool[tid]['in_store_count'] += 1

        return jsonify({
            'tools': sorted(by_tool.values(), key=lambda t: (t['tool_name'].lower(), t['tool_code'])),
            'categories': sorted(categories_map.values(), key=lambda c: c['category_name'].lower()),
            'total_serials': len(serials),
        })

    @app.route('/hdc/api/tool-rental/serials/out-of-store')
    @login_required
    def hdc_api_serials_out_of_store():
        """Get all serials currently OUT OF STORE (at a site or customer) for
        the Transfer Tools data-driven picker.

        Optional query params:
            category_id: Filter by ToolCategory ID
            tool_id: Filter by Tool ID
            rental_id: Filter by active Rental ID
            holder: Filter by current location / holder name
        """
        category_id = request.args.get('category_id', type=int)
        tool_id = request.args.get('tool_id', type=int)
        rental_id = request.args.get('rental_id', type=int)
        holder = (request.args.get('holder') or '').strip() or None
        serials = get_serials_out_of_store_summary(
            category_id=category_id, tool_id=tool_id, rental_id=rental_id, holder=holder
        )
        by_tool = {}
        for s in serials:
            tid = s['tool_id']
            cname = s.get('category_name') or s['tool_name']
            if tid not in by_tool:
                by_tool[tid] = {
                    'tool_id': tid,
                    'tool_name': s['tool_name'],
                    'tool_code': s['tool_code'],
                    'category_id': s.get('category_id'),
                    'category_name': cname,
                    'rate': s.get('rate', 0.0),
                    'serials': [],
                    'out_count': 0,
                }
            by_tool[tid]['serials'].append({
                'serial_id': s['serial_id'],
                'serial_number': s['serial_number'],
                'holder': s['holder'],
                'rental_id': s['rental_id'],
                'rental_item_id': s['rental_item_id'],
                'rental_code': s['rental_code'],
                'condition': s['condition'],
            })
            by_tool[tid]['out_count'] += 1

        return jsonify({
            'tools': sorted(by_tool.values(), key=lambda t: (t['tool_name'].lower(), t['tool_code'])),
            'total_serials': len(serials),
        })

    @app.route('/hdc/api/tool-rental/serials/<int:serial_id>/status')
    @login_required
    def hdc_api_serial_status(serial_id):
        """Get detailed status of a single serial-numbered piece.

        Used by the tool-status dialog when clicking on a serial in the picker.
        """
        status = get_serial_status(serial_id)
        if not status:
            return jsonify({'error': 'Serial not found'}), 404

        return jsonify({
            'serial_id': status['serial'].id,
            'serial_number': status['serial'].serial_number,
            'tool_id': status['serial'].tool_id,
            'tool_name': status['tool'].name if status['tool'] else '-',
            'tool_code': status['tool'].tool_code if status['tool'] else status['serial'].serial_number,
            'status': status['status'],
            'status_label': status['status_label'],
            'is_in_store': status['is_in_store'],
            'condition': status['condition'],
            'notes': status['notes'],
            'current_location': status['serial'].current_location_label,
            'current_rental_code': status['current_rental'].rental_code if status['current_rental'] else None,
            'current_rental_id': status['current_rental'].id if status['current_rental'] else None,
            'movements': [{
                'id': m.id,
                'type': m.movement_type,
                'from_label': m.from_location_label,
                'to_label': m.to_location_label,
                'notes': m.notes,
                'timestamp': m.timestamp.isoformat() if m.timestamp else None,
            } for m in status['movements']],
        })

    @app.route('/hdc/api/tool-rental/serials/<int:serial_id>/rename', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_api_serial_rename(serial_id):
        """Rename or update an individual tool serial marking (e.g. 'Shovel No 5')."""
        new_sn = (request.form.get('serial_number') or '').strip()
        condition = (request.form.get('condition') or '').strip() or None
        notes = request.form.get('notes')
        ok, msg, s_obj = rename_tool_serial(
            serial_id=serial_id,
            new_serial_number=new_sn,
            condition=condition,
            notes=notes,
            updated_by=getattr(current_user, 'id', None),
            commit=True,
        )
        if not ok:
            return jsonify({'success': False, 'message': msg}), 400
        return jsonify({
            'success': True,
            'message': msg,
            'serial_id': s_obj.id,
            'serial_number': s_obj.serial_number,
            'condition': s_obj.condition,
            'notes': s_obj.notes,
        })

    @app.route('/hdc/api/tool-rental/serials/tool/<int:tool_id>/available')
    @login_required
    def hdc_api_serials_for_tool(tool_id):
        """Get available (in-store) serials for a specific tool.

        Used when a user selects a tool in the rental form to show its available serials.
        """
        tool = Tool.query.get_or_404(tool_id)
        serials = get_serials_by_tool_for_rental(tool_id)

        return jsonify({
            'tool_id': tool.id,
            'tool_name': tool.name,
            'tool_code': tool.tool_code,
            'category_id': tool.category_id,
            'category_name': tool.category.name if tool.category else tool.name,
            'total_qty': float(tool.total_quantity or 0),
            'available_serials': len(serials),
            'serials': [{
                'serial_id': s.id,
                'serial_number': s.serial_number,
                'label': s.serial_label,
                'condition': s.condition,
                'status': s.status,
                'status_label': s.status_label,
            } for s in serials],
        })

    @app.route('/hdc/api/tool-rental/serials/assign', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_api_serials_assign():
        """Assign serials to a rental."""
        rental_id = request.form.get('rental_id', type=int)
        serial_ids_raw = request.form.get('serial_ids', '').strip()
        notes = (request.form.get('notes') or '').strip()
        created_by = getattr(current_user, 'id', None)

        if not rental_id:
            return jsonify({'success': False, 'message': 'Rental ID required'}), 400

        if not serial_ids_raw:
            return jsonify({'success': False, 'message': 'No serials selected'}), 400

        try:
            serial_ids = [int(x.strip()) for x in serial_ids_raw.split(',') if x.strip()]
        except ValueError:
            return jsonify({'success': False, 'message': 'Invalid serial IDs'}), 400

        success, message, assigned = assign_serials_to_rental(
            rental_id, serial_ids, notes=notes, created_by=created_by
        )

        if not success:
            return jsonify({'success': False, 'message': message}), 400

        return jsonify({
            'success': True,
            'message': message,
            'assigned': assigned,
            'rental_id': rental_id,
        })

    @app.route('/hdc/api/tool-rental/serials/return', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_api_serials_return():
        """Return serials from a rental."""
        rental_id = request.form.get('rental_id', type=int)
        serial_ids_raw = request.form.get('serial_ids', '').strip()
        return_id = request.form.get('return_id', type=int)
        notes = (request.form.get('notes') or '').strip()
        created_by = getattr(current_user, 'id', None)

        if not rental_id:
            return jsonify({'success': False, 'message': 'Rental ID required'}), 400

        if not serial_ids_raw:
            return jsonify({'success': False, 'message': 'No serials selected'}), 400

        try:
            serial_ids = [int(x.strip()) for x in serial_ids_raw.split(',') if x.strip()]
        except ValueError:
            return jsonify({'success': False, 'message': 'Invalid serial IDs'}), 400

        success, message, returned = return_serials_from_rental(
            rental_id, serial_ids, return_id=return_id, notes=notes, created_by=created_by
        )

        if not success:
            return jsonify({'success': False, 'message': message}), 400

        return jsonify({
            'success': True,
            'message': message,
            'returned': returned,
            'rental_id': rental_id,
        })

    @app.route('/hdc/api/tool-rental/serials/create', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_api_serials_create():
        """Create serial-numbered pieces for a tool."""
        tool_id = request.form.get('tool_id', type=int)
        qty = request.form.get('qty', type=float)
        start_no = request.form.get('start_no', type=int)
        serial_numbers_raw = (request.form.get('serial_numbers') or '').strip()
        created_by = getattr(current_user, 'id', None)

        if not tool_id:
            return jsonify({'success': False, 'message': 'Tool ID required'}), 400

        serial_numbers = parse_custom_serial_list(serial_numbers_raw) if serial_numbers_raw else None
        if (qty is None or qty <= 0) and serial_numbers:
            qty = float(len(serial_numbers))
        if qty is None or qty <= 0:
            return jsonify({'success': False, 'message': 'Quantity must be > 0'}), 400

        success, message, serial_ids = create_tool_serials(
            tool_id, qty, serial_numbers=serial_numbers, start_no=start_no,
            created_by=created_by, commit=True
        )

        if not success:
            return jsonify({'success': False, 'message': message}), 400

        return jsonify({
            'success': True,
            'message': message,
            'serial_ids': serial_ids,
            'tool_id': tool_id,
        })

    @app.route('/hdc/api/tool-rental/tools/<int:tool_id>/summary')
    @login_required
    def hdc_api_tool_summary(tool_id):
        """Get summary of a tool including full serial breakdown (in store vs out at site/customer)."""
        tool = Tool.query.get_or_404(tool_id)
        ensure_tool_serials(tool=tool, commit=True)
        serials = tool.active_serials

        serial_data = []
        for s in serials:
            rental = s.current_rental
            serial_data.append({
                'serial_id': s.id,
                'serial_number': s.serial_number,
                'status': s.status,
                'status_label': s.status_label,
                'is_in_store': s.is_in_store,
                'condition': s.condition,
                'notes': s.notes or '',
                'current_location': s.current_location_label or ('Warehouse / Store' if s.is_in_store else (rental.current_location_label if rental else 'Out of Store')),
                'current_rental_id': s.current_rental_id,
                'current_rental_code': rental.rental_code if rental else None,
            })

        return jsonify({
            'tool_id': tool.id,
            'tool_name': tool.name,
            'tool_code': tool.tool_code,
            'category_name': tool.category.name if tool.category else tool.name,
            'owned': float(tool.total_quantity or 0),
            'in_store': float(tool.available_qty),
            'rented': float(tool.rented_out_qty),
            'utilization': round(float(tool.utilization_pct), 1),
            'serials': serial_data,
            'serial_count': len(serials),
        })

    @app.route('/hdc/api/tool-rental/serials/inventory-summary')
    @login_required
    def hdc_api_serials_inventory_summary():
        """Get serial summary for tools (for inventory page)."""
        tool_id = request.args.get('tool_id', type=int)
        summary = get_all_tool_serials_summary(tool_id=tool_id)
        return jsonify([
            {
                'tool_id': s['tool_id'],
                'tool_name': s['tool_name'],
                'tool_code': s['tool_code'],
                'total_serials': s['total_serials'],
                'in_store': s['in_store'],
                'rented': s['rented'],
                'maintenance': s['maintenance'],
                'damaged': s['damaged'],
                'lost': s['lost'],
                'serials': [
                    {
                        'serial_id': sr.id,
                        'serial_number': sr.serial_number,
                        'is_in_store': sr.is_in_store,
                        'status': sr.status,
                        'status_label': sr.status_label,
                        'condition': sr.condition,
                        'location': sr.current_location_label or ('Warehouse / Store' if sr.is_in_store else 'Out of Store'),
                        'rental_id': sr.current_rental_id,
                        'rental_code': sr.current_rental.rental_code if sr.current_rental else None,
                    }
                    for sr in s['serials']
                ],
            }
            for s in summary
        ])

    @app.route('/hdc/tool-rental/inventory/<int:tool_id>/serials/create', methods=['GET', 'POST'])
    @login_required
    @_money_write_required()
    def hdc_tool_rental_serials_create(tool_id):
        """Create or manage serial-numbered pieces for a tool (e.g. 'Shovel No 5')."""
        tool = Tool.query.get_or_404(tool_id)

        if request.method == 'POST':
            action = (request.form.get('action') or 'create').strip()
            if action == 'rename':
                serial_id = request.form.get('serial_id', type=int)
                new_sn = (request.form.get('serial_number') or '').strip()
                condition = (request.form.get('condition') or '').strip() or None
                notes = request.form.get('notes')
                ok, msg, _ = rename_tool_serial(
                    serial_id=serial_id,
                    new_serial_number=new_sn,
                    condition=condition,
                    notes=notes,
                    updated_by=getattr(current_user, 'id', None),
                    commit=True,
                )
                flash(msg, 'success' if ok else 'danger')
                return redirect(url_for('hdc_tool_rental_serials_create', tool_id=tool_id))

            serial_numbers_raw = (request.form.get('serial_numbers') or '').strip()
            start_no = request.form.get('start_no', type=int)
            serial_numbers = parse_custom_serial_list(serial_numbers_raw) if serial_numbers_raw else None
            qty = max(0, _flt(request.form.get('qty'), 0))
            if qty <= 0 and serial_numbers:
                qty = float(len(serial_numbers))
            if qty <= 0:
                flash('Quantity must be greater than 0.', 'danger')
                return redirect(url_for('hdc_tool_rental_serials_create', tool_id=tool_id))

            success, message, serial_ids = create_tool_serials(
                tool_id=tool_id,
                qty=qty,
                serial_numbers=serial_numbers,
                start_no=start_no,
                created_by=getattr(current_user, 'id', None),
                commit=True,
            )

            if success:
                flash(message, 'success')
            else:
                flash(message or 'Could not create serials.', 'danger')
            return redirect(url_for('hdc_tool_rental_serials_create', tool_id=tool_id))

        # GET: ensure serials are synced and show management page
        ensure_tool_serials(tool=tool, commit=True)
        serials = tool.active_serials
        return render_template('tool_rental/tool_serials_create.html',
            tool=tool,
            serials=serials,
            existing_serials=len(serials),
            today=_pkt_today().isoformat(),
        )
