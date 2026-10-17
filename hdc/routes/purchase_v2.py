"""HDC routes: Purchase V2 pages: materials, orders, suppliers, stock.

Moved verbatim from hdc_erp.py; each handler keeps its
original @app.route decorator and endpoint name.
"""

from datetime import datetime

from flask import flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func, or_

from hdc.extensions import _money_write_required, db
from hdc.models.materials import Delivery, MaterialCategory, MaterialV2, PurchaseV2, Supplier, SupplierCategory, SupplierLedger, SupplierSite, UsageLogV2
from hdc.models.projects import Project, Stage
from hdc.services.accounts import _accounts_post_supplier_credit_row, _accounts_set_void_by_source, _accounts_upsert_purchase_paid_txn
from hdc.services.audit import log_action
from hdc.services.material_categories import (
    UNCATEGORIZED_ID,
    UNCATEGORIZED_LABEL,
    _delivery_category_condition,
    _ensure_material_category,
    _material_category_by_id,
    _material_category_by_name,
    _material_category_choices,
    _material_category_label,
    _material_category_map,
    _material_category_options,
    _material_category_summary_rows,
    _material_category_usage_counts,
    _material_category_condition,
    _material_category_for_write,
    _purchase_category_condition,
    _usage_category_condition,
    material_category_id,
    purchase_resolved_category_id,
    purchase_resolved_category_name,
)
from hdc.services.record_permissions import integrity_message, integrity_query
from hdc.services.purchase import REGISTER_SORT_MODES, validate_delivery_reduction, _MATERIAL_V2_UNITS, _ensure_material_v2, _ensure_supplier_quick, _material_v2_available, _material_v2_delivered, _material_v2_used, _material_v2_weighted_cost, _purchase_v2_available_in_scope_qty, _purchase_v2_delivered_qty, _purchase_v2_integrity_report, _purchase_v2_pending_rows, _repair_supplier_purchase_v2_ledger, _site_supplier_supplied_rows, _supplier_category_ids, _supplier_category_ids_map, _supplier_category_supplied_rows, _supplier_site_ids, _supplier_site_ids_map, _supplier_site_supplied_rows, _supplier_supplied_profile, _sync_purchase_category, _sync_purchase_v2_ledger, _sync_supplier_po_payment_status, _transfer_v2_material_between_scopes
from hdc.services.timekeeping import _has_recent_duplicate
from hdc.utils.dates import _pkt_now_naive, _pkt_today
from hdc.utils.format import _flt, _parse_date
from hdc.utils.normalize import _normalize_name_ci

def _category_filter_arg(name='category_id'):
    """Read a category filter from the query string.

    ``None`` (no value) means **All Categories**; ``0`` is the deliberately
    reachable "Uncategorized" bucket, so legacy rows can always be listed and
    assigned; anything else must be a real, non-void category id or it is
    ignored.  Every register reads its filter through here, which is what keeps
    "All Categories" and "one category at a time" behaving identically on the
    Purchases, Delivery, Usage, Stock, Materials and Supplier pages.
    """
    raw = (request.args.get(name) or '').strip()
    if raw == '':
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    if value == UNCATEGORIZED_ID:
        return UNCATEGORIZED_ID
    return value if _material_category_by_id(value) else None


def _category_filter_context(selected_category_id):
    """Everything a template's category filter needs, in one place."""
    return {
        'category_options': _material_category_choices(include_inactive=False),
        # The **edit** dropdowns need the retired types as well: a category
        # that was set inactive still sits on the records that use it, and an
        # edit form that cannot show the record's own category would silently
        # re-file the record under a different one.
        'category_options_all': _material_category_choices(include_inactive=True),
        'selected_category_id': ('' if selected_category_id is None else int(selected_category_id)),
        'category_filter_label': ('All Categories' if selected_category_id is None
                                  else _material_category_label(selected_category_id)),
        'uncategorized_id': UNCATEGORIZED_ID,
    }


def register(app):
    """Register Purchase V2 pages: materials, orders, suppliers, stock."""
    @app.route('/hdc/purchase-v2')
    @login_required
    def hdc_purchase_v2_page():
        material_count = int(db.session.query(func.count(MaterialV2.id)).filter(MaterialV2.is_void == False).scalar() or 0)
        purchase_count = int(db.session.query(func.count(PurchaseV2.id)).filter(PurchaseV2.is_void == False).scalar() or 0)
        purchase_total = float(db.session.query(func.coalesce(func.sum(PurchaseV2.total_amount), 0.0))
                               .filter(PurchaseV2.is_void == False).scalar() or 0.0)
        supplier_count = int(db.session.query(func.count(Supplier.id)).filter(Supplier.is_void == False).scalar() or 0)
        delivered_count = int(db.session.query(func.count(Delivery.id)).filter(Delivery.is_void == False).scalar() or 0)
        delivered_qty = float(db.session.query(func.coalesce(func.sum(Delivery.quantity), 0.0)).filter(Delivery.is_void == False).scalar() or 0.0)
        usage_count = int(db.session.query(func.count(UsageLogV2.id)).filter(UsageLogV2.is_void == False).scalar() or 0)
        usage_qty = float(db.session.query(func.coalesce(func.sum(UsageLogV2.quantity), 0.0)).filter(UsageLogV2.is_void == False).scalar() or 0.0)
        usage_cost = float(db.session.query(func.coalesce(func.sum(UsageLogV2.cost), 0.0)).filter(UsageLogV2.is_void == False).scalar() or 0.0)
        integrity = _purchase_v2_integrity_report()
        # Category counts for the hub: how many item types exist, how many are
        # still unassigned, and the biggest categories by purchase value.
        category_summary = _material_category_summary_rows(include_inactive=False)
        category_count = len([c for c in category_summary if int(c['id']) != UNCATEGORIZED_ID])
        uncategorized_material_count = int(db.session.query(func.count(MaterialV2.id))
                                           .filter(MaterialV2.is_void == False,
                                                   MaterialV2.category_id.is_(None)).scalar() or 0)
        top_categories = sorted([c for c in category_summary if int(c['id']) != UNCATEGORIZED_ID],
                                key=lambda c: -float(c['purchased_value'] or 0.0))[:6]
        supplier_site_count = int(db.session.query(func.count(SupplierSite.id)).scalar() or 0)
        return render_template('purchase/purchase_v2.html',
            material_count=material_count,
            category_count=category_count,
            uncategorized_material_count=uncategorized_material_count,
            top_categories=top_categories,
            supplier_site_count=supplier_site_count,
            purchase_count=purchase_count,
            purchase_total=purchase_total,
            supplier_count=supplier_count,
            delivered_count=delivered_count,
            delivered_qty=delivered_qty,
            usage_count=usage_count,
            usage_qty=usage_qty,
            usage_cost=usage_cost,
            integrity=integrity
        )


    @app.route('/hdc/purchase-v2/materials', methods=['GET', 'POST'])
    @login_required
    @_money_write_required()
    def hdc_purchase_v2_materials():
        if request.method == 'POST':
            name = _normalize_name_ci(request.form.get('name'))
            unit = (request.form.get('unit') or 'KG').strip().upper()
            if unit not in _MATERIAL_V2_UNITS:
                flash(f'Unit must be one of: {", ".join(_MATERIAL_V2_UNITS)}.', 'danger')
                return redirect(url_for('hdc_purchase_v2_materials'))
            # Item type: either an existing category, or one typed on the spot.
            category = _material_category_by_id(request.form.get('category_id'), active_only=True)
            new_category_name = _normalize_name_ci(request.form.get('category_new'))
            if new_category_name:
                category = _ensure_material_category(new_category_name)
            row = _ensure_material_v2(name, unit)
            if not row:
                flash('Material name is required.', 'danger')
                return redirect(url_for('hdc_purchase_v2_materials'))
            row.unit = unit
            row.category_id = (int(category.id) if category else None)
            row.updated_at = _pkt_now_naive()
            category_note = (f' [{category.name}]' if category else '')
            log_action(current_user, 'create',
                       f'{current_user.username.title()} added material {row.name} ({unit}){category_note}',
                       'material_v2', row.id)
            db.session.commit()
            flash('Material saved.', 'success')
            return redirect(url_for('hdc_purchase_v2_materials'))
        category_id = _category_filter_arg()
        row_query = MaterialV2.query.filter_by(is_void=False)
        if category_id is not None:
            row_query = row_query.filter(_material_category_condition(category_id))
        rows = row_query.order_by(MaterialV2.name.asc()).all()
        material_ids = [int(m.id) for m in rows]
        purchased_map = {}
        if material_ids:
            purchased_map = dict(
                db.session.query(
                    PurchaseV2.material_id,
                    func.coalesce(func.sum(PurchaseV2.quantity), 0.0)
                ).filter(
                    PurchaseV2.is_void == False,
                    PurchaseV2.material_id.in_(material_ids)
                ).group_by(PurchaseV2.material_id).all()
            )
        category_map = _material_category_map()
        materials = []
        totals = {
            'purchased_qty': 0.0,
            'delivered_qty': 0.0,
            'used_qty': 0.0,
            'available_qty': 0.0,
            'in_store_qty': 0.0
        }
        for m in rows:
            purchased = float(purchased_map.get(m.id, 0.0) or 0.0)
            delivered = _material_v2_delivered(m.id)
            used = _material_v2_used(m.id)
            available = max(0.0, delivered - used)
            in_store = max(0.0, purchased - delivered)
            materials.append({
                'row': m,
                'category_id': (int(m.category_id) if m.category_id else UNCATEGORIZED_ID),
                'category_name': _material_category_label(material_category_id(m), category_map=category_map),
                'purchased_qty': purchased,
                'delivered_qty': delivered,
                'used_qty': used,
                'available_qty': available,
                'in_store_qty': in_store
            })
            totals['purchased_qty'] += purchased
            totals['delivered_qty'] += delivered
            totals['used_qty'] += used
            totals['available_qty'] += available
            totals['in_store_qty'] += in_store
        return render_template('purchase/purchase_v2_materials.html',
            materials=materials,
            material_units=_MATERIAL_V2_UNITS,
            totals=totals,
            uncategorized_materials=(MaterialV2.query
                                     .filter(MaterialV2.is_void == False,
                                             MaterialV2.category_id.is_(None))
                                     .order_by(MaterialV2.name.asc()).all()),
            **_category_filter_context(category_id))


    @app.route('/hdc/purchase-v2/materials/<int:material_id>/edit', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_purchase_v2_material_edit(material_id):
        row = MaterialV2.query.get_or_404(material_id)
        name = _normalize_name_ci(request.form.get('name')) or row.name
        unit = (request.form.get('unit') or row.unit or 'KG').strip().upper()
        status = (request.form.get('status') or row.status or 'active').strip().lower()
        if status not in ('active', 'inactive'):
            status = 'active'
        if unit not in _MATERIAL_V2_UNITS:
            flash(f'Unit must be one of: {", ".join(_MATERIAL_V2_UNITS)}.', 'danger')
            return redirect(url_for('hdc_purchase_v2_materials'))
        exists = (MaterialV2.query
                  .filter(
                      MaterialV2.id != row.id,
                      MaterialV2.is_void == False,
                      func.lower(MaterialV2.name) == name.lower()
                  )
                  .first())
        if exists:
            flash('Another material already uses this name.', 'danger')
            return redirect(url_for('hdc_purchase_v2_materials'))
        raw_category = (request.form.get('category_id') or '').strip()
        current_category_id = int(row.category_id) if row.category_id else None
        category = None
        if raw_category and raw_category != '0':
            # An item keeps the type it already has, even when that type has
            # been retired — clearing it is a separate, deliberate choice.
            category = _material_category_for_write(raw_category,
                                                   current_category_id=current_category_id)
            if not category:
                flash('Select a valid category.', 'danger')
                return redirect(url_for('hdc_purchase_v2_materials'))
        new_category_name = _normalize_name_ci(request.form.get('category_new'))
        if new_category_name:
            category = _ensure_material_category(new_category_name)
        row.name = name
        row.unit = unit
        row.status = status
        row.category_id = (int(category.id) if category else None)
        row.updated_at = _pkt_now_naive()
        log_action(current_user, 'update',
                   f'{current_user.username.title()} updated material #{row.id}: {row.name} ({row.unit})'
                   + (f' [{category.name}]' if category else ''),
                   'material_v2', row.id)
        db.session.commit()
        flash('Material updated.', 'success')
        return redirect(url_for('hdc_purchase_v2_materials'))


    @app.route('/hdc/purchase-v2/materials/<int:material_id>/delete', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_purchase_v2_material_delete(material_id):
        row = MaterialV2.query.get_or_404(material_id)
        has_purchase = integrity_query(db.session.query(PurchaseV2.id).filter_by(material_id=row.id, is_void=False)).first() is not None
        has_delivery = integrity_query(db.session.query(Delivery.id).filter_by(material_id=row.id, is_void=False)).first() is not None
        has_usage = integrity_query(db.session.query(UsageLogV2.id).filter_by(material_id=row.id, is_void=False)).first() is not None
        if has_purchase or has_delivery or has_usage:
            row.status = 'inactive'
            row.updated_at = _pkt_now_naive()
            db.session.commit()
            flash('Material has transactions, so it was set inactive instead of delete.', 'warning')
            return redirect(url_for('hdc_purchase_v2_materials'))
        row.is_void = True
        row.status = 'inactive'
        row.updated_at = _pkt_now_naive()
        log_action(current_user, 'delete', f'{current_user.username.title()} deleted material #{row.id}: {row.name}', 'material_v2', row.id)
        db.session.commit()
        flash('Material deleted.', 'success')
        return redirect(url_for('hdc_purchase_v2_materials'))


    @app.route('/hdc/purchase-v2/purchases', methods=['GET', 'POST'])
    @login_required
    @_money_write_required()
    def hdc_purchase_v2_purchases():
        if request.method == 'POST':
            supplier_id = request.form.get('supplier_id', type=int)
            # The form accepts multiple item rows. Keep a single-item fallback so
            # older clients/bookmarks posting the original field names still work.
            material_ids = request.form.getlist('material_id[]') or request.form.getlist('material_id')
            quantities = request.form.getlist('quantity[]') or request.form.getlist('quantity')
            unit_prices = request.form.getlist('unit_price[]') or request.form.getlist('unit_price')
            notes = request.form.getlist('notes[]') or request.form.getlist('notes')
            # One category per item row — the item type the field asked for.
            # Blank means "use the material's own category", which is the
            # default the item row's dropdown pre-selects.
            row_categories = request.form.getlist('category_id[]') or request.form.getlist('category_id')
            if not material_ids or len(material_ids) != len(quantities) or len(material_ids) != len(unit_prices):
                flash('Add at least one complete purchase item.', 'danger')
                return redirect(url_for('hdc_purchase_v2_purchases'))
            payment_status = (request.form.get('payment_status') or 'unpaid').strip().lower()
            if payment_status not in ('paid', 'unpaid'):
                payment_status = 'unpaid'
            date_raw = (request.form.get('date') or '').strip()
            try:
                purchase_date = datetime.strptime(date_raw, '%Y-%m-%d').date() if date_raw else _pkt_today()
            except ValueError:
                purchase_date = _pkt_today()
            challan = (request.form.get('challan_no') or '').strip() or None
            supplier = Supplier.query.get(supplier_id) if supplier_id else None
            if (not supplier) or supplier.is_void or (supplier.status or 'active').strip().lower() != 'active':
                flash('Valid supplier is required.', 'danger')
                return redirect(url_for('hdc_purchase_v2_purchases'))

            # Validate every row before creating anything, so a mistake in one
            # item cannot leave a partially saved multi-item purchase.
            items = []
            for index, (material_raw, quantity_raw, price_raw) in enumerate(zip(material_ids, quantities, unit_prices)):
                material_id = _flt(material_raw, 0)
                quantity = max(0.0, _flt(quantity_raw, 0.0))
                unit_price = max(0.0, _flt(price_raw, 0.0))
                material = MaterialV2.query.get(int(material_id)) if material_id else None
                if (not material) or material.is_void or (material.status or 'active').strip().lower() != 'active':
                    flash(f'Valid material is required for item {index + 1}.', 'danger')
                    return redirect(url_for('hdc_purchase_v2_purchases'))
                if quantity <= 0 or unit_price <= 0:
                    flash(f'Quantity and rate must be greater than 0 for item {index + 1}.', 'danger')
                    return redirect(url_for('hdc_purchase_v2_purchases'))
                item_notes = (notes[index] if index < len(notes) else '').strip() or None
                raw_category = (row_categories[index] if index < len(row_categories) else '').strip()
                category = None
                if raw_category and raw_category != '0':
                    # A retired item type may only be **kept** — the one the
                    # item itself already sits in — never newly chosen.
                    category = _material_category_for_write(
                        raw_category,
                        current_category_id=(int(material.category_id) if material.category_id else None))
                    if not category:
                        flash(f'Item {index + 1}: select a valid category.', 'danger')
                        return redirect(url_for('hdc_purchase_v2_purchases'))
                items.append((material, quantity, unit_price, item_notes, category))

            created = []
            for material, quantity, unit_price, item_notes, category in items:
                total_amount = float(unit_price * quantity)
                if _has_recent_duplicate(PurchaseV2, supplier_id=supplier.id, material_id=material.id,
                        unit_price=unit_price, quantity=quantity, total_amount=total_amount,
                        payment_status=payment_status, date=purchase_date, notes=item_notes, challan_no=challan):
                    db.session.rollback()
                    flash(f'Duplicate purchase prevented for {material.name}.', 'warning')
                    return redirect(url_for('hdc_purchase_v2_purchases'))
                row = PurchaseV2(supplier_id=supplier.id, material_id=material.id,
                    unit_price=unit_price, quantity=quantity, total_amount=total_amount,
                    payment_status=payment_status, date=purchase_date, notes=item_notes,
                    challan_no=challan, is_void=False, created_at=_pkt_now_naive(), updated_at=_pkt_now_naive())
                # Every purchase line carries a category: the one chosen on the
                # row, or the material's own.  Stored on the line so a later
                # catalog change cannot rewrite the history.
                _sync_purchase_category(row, material=material,
                                        category_id=(int(category.id) if category else None))
                db.session.add(row)
                db.session.flush()
                _sync_purchase_v2_ledger(row)
                if payment_status == 'paid':
                    ok_txn, msg_txn, _ = _accounts_upsert_purchase_paid_txn(row, supplier_name=supplier.name, commit=False)
                    if not ok_txn:
                        db.session.rollback()
                        flash(msg_txn or 'Unable to post paid purchase in unified accounts.', 'danger')
                        return redirect(url_for('hdc_purchase_v2_purchases'))
                log_action(current_user, 'create',
                    f'{current_user.username.title()} created purchase #{row.id}: {supplier.name}, {material.name}, {quantity:.2f} x {unit_price:.2f} = {total_amount:.2f} ({payment_status})',
                    'purchase_v2', row.id)
                created.append(row)
            db.session.commit()
            flash(f'{len(created)} purchase item(s) recorded successfully.', 'success')
            return redirect(url_for('hdc_purchase_v2_purchases'))
        supplier_id = request.args.get('supplier_id', type=int)
        material_id = request.args.get('material_id', type=int)
        po_raw = (request.args.get('po_number') or '').strip()
        po_number = None
        if po_raw:
            try:
                po_number = int(po_raw)
            except (TypeError, ValueError):
                flash('PO number must be numeric.', 'warning')
        date_from = _parse_date((request.args.get('date_from') or '').strip(), fallback=None)
        date_to = _parse_date((request.args.get('date_to') or '').strip(), fallback=None)
        category_id = _category_filter_arg()
        page = max(1, request.args.get('page', type=int) or 1)
        per_page = 25
        q = PurchaseV2.query.filter(PurchaseV2.is_void == False)
        if supplier_id:
            q = q.filter(PurchaseV2.supplier_id == supplier_id)
        if po_number:
            q = q.filter(PurchaseV2.id == po_number)
        if material_id:
            q = q.filter(PurchaseV2.material_id == material_id)
        if category_id is not None:
            q = q.filter(_purchase_category_condition(category_id))
        if date_from:
            q = q.filter(PurchaseV2.date >= date_from)
        if date_to:
            q = q.filter(PurchaseV2.date <= date_to)
        pg_total_items = q.count()
        pg_total_pages = max(1, (pg_total_items + per_page - 1) // per_page)
        page = min(page, pg_total_pages)
        # Newest entries first; id breaks ties for rows created in the same second.
        rows = (q.order_by(PurchaseV2.created_at.desc(), PurchaseV2.id.desc())
                 .offset((page - 1) * per_page).limit(per_page).all())
        suppliers = Supplier.query.filter(Supplier.is_void == False).order_by(Supplier.name.asc()).all()
        materials = MaterialV2.query.filter(MaterialV2.is_void == False).order_by(MaterialV2.name.asc()).all()
        purchase_ids = [int(r.id) for r in rows]
        delivered_map = {}
        if purchase_ids:
            delivered_map = dict(
                db.session.query(
                    Delivery.purchase_id,
                    func.coalesce(func.sum(Delivery.quantity), 0.0)
                ).filter(
                    Delivery.is_void == False,
                    Delivery.purchase_id.in_(purchase_ids)
                ).group_by(Delivery.purchase_id).all()
            )
        # Grand total spans every row matching the filters, not just this page.
        total = float(q.with_entities(func.coalesce(func.sum(PurchaseV2.total_amount), 0.0)).scalar() or 0.0)
        pg_query = {}
        if supplier_id:
            pg_query['supplier_id'] = supplier_id
        if po_number:
            pg_query['po_number'] = po_number
        if material_id:
            pg_query['material_id'] = material_id
        if category_id is not None:
            pg_query['category_id'] = category_id
        if date_from:
            pg_query['date_from'] = date_from.isoformat()
        if date_to:
            pg_query['date_to'] = date_to.isoformat()
        # Per-category totals for the rows behind the filters, so "All
        # Categories" also answers "how much of each" without a second visit.
        category_map = _material_category_map()
        category_totals = {}
        for row in q.all():
            key = purchase_resolved_category_id(row) or UNCATEGORIZED_ID
            bucket = category_totals.setdefault(key, {
                'category_id': key,
                'category_name': _material_category_label(key, category_map=category_map),
                'rows': 0, 'quantity': 0.0, 'value': 0.0
            })
            bucket['rows'] += 1
            bucket['quantity'] += float(row.quantity or 0.0)
            bucket['value'] += float(row.total_amount or 0.0)
        category_totals = sorted(category_totals.values(), key=lambda r: (-r['value'], r['category_name'].lower()))
        return render_template('purchase/purchase_v2_purchases.html',
            rows=rows,
            suppliers=suppliers,
            materials=materials,
            delivered_map=delivered_map,
            total=total,
            selected_supplier_id=supplier_id,
            selected_material_id=material_id,
            filter_po_number=po_raw,
            filter_date_from=(date_from.isoformat() if date_from else ''),
            filter_date_to=(date_to.isoformat() if date_to else ''),
            today=_pkt_today().isoformat(),
            pg_page=page,
            pg_total_pages=pg_total_pages,
            pg_total_items=pg_total_items,
            pg_per_page=per_page,
            pg_endpoint='hdc_purchase_v2_purchases',
            pg_url_kwargs={},
            pg_query=pg_query,
            pg_label='purchase rows',
            category_totals=category_totals,
            category_map=category_map,
            **_category_filter_context(category_id)
        )


    @app.route('/hdc/purchase-v2/purchases/<int:purchase_id>/edit', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_purchase_v2_purchase_edit(purchase_id):
        row = PurchaseV2.query.get_or_404(purchase_id)
        if row.is_void:
            flash('Purchase is already deleted.', 'danger')
            return redirect(url_for('hdc_purchase_v2_purchases'))
        supplier_id = request.form.get('supplier_id', type=int)
        material_id = request.form.get('material_id', type=int)
        unit_price = max(0.0, _flt(request.form.get('unit_price'), row.unit_price))
        quantity = max(0.0, _flt(request.form.get('quantity'), row.quantity))
        payment_status = (request.form.get('payment_status') or row.payment_status or 'unpaid').strip().lower()
        if payment_status not in ('paid', 'unpaid'):
            payment_status = 'unpaid'
        supplier = Supplier.query.get(supplier_id) if supplier_id else None
        material = MaterialV2.query.get(material_id) if material_id else None
        if (not supplier) or supplier.is_void or (supplier.status or 'active').strip().lower() != 'active':
            flash('Valid supplier is required.', 'danger')
            return redirect(url_for('hdc_purchase_v2_purchases'))
        if (not material) or material.is_void or (material.status or 'active').strip().lower() != 'active':
            flash('Valid material is required.', 'danger')
            return redirect(url_for('hdc_purchase_v2_purchases'))
        if unit_price <= 0 or quantity <= 0:
            flash('Unit price and quantity must be greater than 0.', 'danger')
            return redirect(url_for('hdc_purchase_v2_purchases'))
        delivered = _purchase_v2_delivered_qty(row.id, integrity=True)
        if quantity + 1e-9 < delivered:
            flash(integrity_message('Cannot reduce quantity: deliveries already exist.', f'Cannot set quantity below delivered quantity ({delivered:,.2f}).'), 'danger')
            return redirect(url_for('hdc_purchase_v2_purchases'))
        if row.material_id != material.id and delivered > 0:
            flash('Cannot change material after deliveries are recorded for this purchase.', 'danger')
            return redirect(url_for('hdc_purchase_v2_purchases'))
        _date_raw = (request.form.get('date') or '').strip()
        try:
            _date = datetime.strptime(_date_raw, '%Y-%m-%d').date() if _date_raw else (row.date or _pkt_today())
        except ValueError:
            _date = row.date or _pkt_today()
        _notes   = (request.form.get('notes') or '').strip() or None
        _challan = (request.form.get('challan_no') or '').strip() or None
        # Category may be re-pointed on the line without touching the catalog.
        # A type that was set inactive stays on the rows that already use it,
        # so an edit that only touches the quantity must not re-file the line.
        raw_category = (request.form.get('category_id') or '').strip()
        current_category_id = int(row.category_id) if row.category_id else None
        category = None
        if raw_category and raw_category != '0':
            category = _material_category_for_write(raw_category,
                                                   current_category_id=current_category_id)
            if not category:
                flash('Select a valid category.', 'danger')
                return redirect(url_for('hdc_purchase_v2_purchases'))
        old_payment_status = (row.payment_status or 'unpaid').strip().lower()
        row.supplier_id = supplier.id
        row.material_id = material.id
        _sync_purchase_category(row, material=material,
                                category_id=(int(category.id) if category else None))
        row.unit_price = unit_price
        row.quantity = quantity
        row.total_amount = float(unit_price * quantity)
        row.payment_status = payment_status
        row.date = _date
        row.notes = _notes
        row.challan_no = _challan
        row.updated_at = _pkt_now_naive()
        _sync_purchase_v2_ledger(row)
        if payment_status == 'paid':
            ok_txn, msg_txn, _ = _accounts_upsert_purchase_paid_txn(row, supplier_name=supplier.name, commit=False)
            if not ok_txn and 'Duplicate source transaction' not in (msg_txn or ''):
                db.session.rollback()
                flash(msg_txn or 'Unable to post paid purchase in unified accounts.', 'danger')
                return redirect(url_for('hdc_purchase_v2_purchases'))
        elif old_payment_status == 'paid':
            _accounts_set_void_by_source('purchase_v2_paid', row.id, True)
        log_action(
            current_user,
            'update',
            f'{current_user.username.title()} updated purchase #{row.id}',
            'purchase_v2',
            row.id
        )
        db.session.commit()
        flash(f'Purchase #{row.id} updated.', 'success')
        return redirect(url_for('hdc_purchase_v2_purchases'))


    @app.route('/hdc/purchase-v2/purchases/<int:purchase_id>/delete', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_purchase_v2_purchase_delete(purchase_id):
        row = PurchaseV2.query.get_or_404(purchase_id)
        if row.is_void:
            flash('Purchase is already deleted.', 'warning')
            return redirect(url_for('hdc_purchase_v2_purchases'))
        delivered = _purchase_v2_delivered_qty(row.id, integrity=True)
        if delivered > 0:
            flash(integrity_message('Cannot delete purchase: deliveries already exist.', f'Cannot delete purchase #{row.id}; delivery exists ({delivered:,.2f}).'), 'danger')
            return redirect(url_for('hdc_purchase_v2_purchases'))
        row.is_void = True
        row.void_reason = 'Deleted by user from Purchase V2'
        row.voided_at = _pkt_now_naive()
        row.updated_at = _pkt_now_naive()
        _sync_purchase_v2_ledger(row)
        _accounts_set_void_by_source('purchase_v2_paid', row.id, True)
        log_action(current_user, 'delete', f'{current_user.username.title()} deleted purchase #{row.id}', 'purchase_v2', row.id)
        db.session.commit()
        flash(f'Purchase #{row.id} deleted.', 'success')
        return redirect(url_for('hdc_purchase_v2_purchases'))


    @app.route('/hdc/purchase-v2/suppliers', methods=['GET', 'POST'])
    @login_required
    @_money_write_required()
    def hdc_purchase_v2_suppliers():
        if request.method == 'POST':
            name = _normalize_name_ci(request.form.get('name'))
            phone = (request.form.get('phone') or '').strip()
            opening_balance = max(0.0, _flt(request.form.get('opening_balance'), 0.0))
            if not name:
                flash('Supplier name is required.', 'danger')
                return redirect(url_for('hdc_purchase_v2_suppliers'))
            address = (request.form.get('address') or '').strip()
            row = _ensure_supplier_quick(name, phone)
            if not row:
                flash('Unable to save supplier.', 'danger')
                return redirect(url_for('hdc_purchase_v2_suppliers'))
            if address and hasattr(row, 'address'):
                row.address = address
            row.updated_at = _pkt_now_naive()
            # "Deals in" categories and the sites this supplier is selected
            # for, straight from the add form — the supplier list is then
            # useful by material and by site before the first purchase.
            picked_categories = request.form.getlist('category_ids[]') or request.form.getlist('category_ids')
            picked_projects = request.form.getlist('project_ids[]') or request.form.getlist('project_ids')
            for raw_category in picked_categories:
                category = _material_category_by_id(raw_category, active_only=True)
                if not category:
                    continue
                exists_link = SupplierCategory.query.filter_by(
                    supplier_id=row.id, category_id=category.id).first()
                if not exists_link:
                    db.session.add(SupplierCategory(supplier_id=row.id, category_id=category.id,
                                                    created_at=_pkt_now_naive()))
            for raw_project in picked_projects:
                project = Project.query.get(int(raw_project)) if str(raw_project).isdigit() else None
                if not project:
                    continue
                exists_link = SupplierSite.query.filter_by(
                    supplier_id=row.id, project_id=project.id).first()
                if not exists_link:
                    db.session.add(SupplierSite(supplier_id=row.id, project_id=project.id,
                                                created_at=_pkt_now_naive()))
            if opening_balance > 0:
                db.session.add(SupplierLedger(
                    supplier_id=row.id,
                    entry_type='debit',
                    amount=float(opening_balance),
                    reference_type='opening_balance',
                    reference_id=None,
                    note='Opening balance imported',
                    is_void=False,
                    created_at=_pkt_now_naive()
                ))
            log_action(current_user, 'create', f'{current_user.username.title()} added supplier {row.name}', 'supplier', row.id)
            db.session.commit()
            flash('Supplier saved.', 'success')
            return redirect(url_for('hdc_purchase_v2_suppliers'))

        q = (request.args.get('q') or '').strip()
        # The three questions the field asked this list to answer:
        #   category_id → who deals in / has supplied this item type
        #   material_id → who has supplied this exact item
        #   project_id  → who is assigned to / has delivered to this site
        filter_category_id = _category_filter_arg()
        filter_material_id = request.args.get('material_id', type=int)
        filter_project_id = request.args.get('project_id', type=int)
        sort_mode = (request.args.get('sort') or 'name').strip().lower()
        if sort_mode not in ('name', 'purchases', 'balance'):
            sort_mode = 'name'
        query = Supplier.query.filter(Supplier.is_void == False)
        if q:
            ql = f'%{q.lower()}%'
            query = query.filter(or_(func.lower(Supplier.name).like(ql), func.lower(func.coalesce(Supplier.phone, '')).like(ql)))
        rows = query.order_by(Supplier.name.asc()).all()

        profile_map = _supplier_supplied_profile()
        stated_category_map = _supplier_category_ids_map()
        stated_site_map = _supplier_site_ids_map()
        supplied_site_rows = _site_supplier_supplied_rows()
        supplied_sites_by_supplier = {}
        for srow in supplied_site_rows:
            supplied_sites_by_supplier.setdefault(srow['supplier_id'], []).append(srow)
        stated_match_ids = set()
        if filter_category_id is not None:
            # A supplier matches when it says it deals in the category (stated)
            # or when it has actually supplied it.  Both are shown apart.
            stated_match_ids = {int(sid) for sid, cids in stated_category_map.items()
                                if int(filter_category_id) in [int(c) for c in cids]}
        material_match_ids = set()
        if filter_material_id:
            material_match_ids = {int(sid) for sid, profile in profile_map.items()
                                  if int(filter_material_id) in profile['material_ids']}
        site_stated_match_ids = set()
        site_supplied_match_ids = set()
        if filter_project_id:
            site_stated_match_ids = {int(sid) for sid, pids in stated_site_map.items()
                                     if int(filter_project_id) in [int(p) for p in pids]}
            site_supplied_match_ids = {int(row['supplier_id']) for row in supplied_site_rows
                                       if int(row['project_id']) == int(filter_project_id)}
        project_names = {int(p.id): p.name for p in Project.query.all()}

        def _profile_matches(supplier_id):
            if filter_category_id is not None:
                profile = profile_map.get(int(supplier_id), {})
                supplied = profile.get('category_ids', [])
                if filter_category_id == UNCATEGORIZED_ID:
                    # "Uncategorized" is a bucket, not a vocabulary entry: a
                    # supplier can never *state* it, it can only have supplied
                    # lines that fall into it.
                    if not int(profile.get('uncategorized_count') or 0):
                        return False
                else:
                    stated_has = int(supplier_id) in stated_match_ids
                    if not stated_has and (not supplied or int(filter_category_id) not in [int(c) for c in supplied]):
                        return False
            if filter_material_id:
                if int(supplier_id) not in material_match_ids:
                    return False
            if filter_project_id:
                if (int(supplier_id) not in site_stated_match_ids
                        and int(supplier_id) not in site_supplied_match_ids):
                    return False
            return True

        rows = [s for s in rows if _profile_matches(s.id)]
        category_map = _material_category_map()

        debit_rows = dict(db.session.query(
            SupplierLedger.supplier_id,
            func.coalesce(func.sum(SupplierLedger.amount), 0.0)
        ).filter(
            SupplierLedger.is_void == False,
            SupplierLedger.entry_type == 'debit'
        ).group_by(SupplierLedger.supplier_id).all())

        credit_kind_rows = db.session.query(
            SupplierLedger.supplier_id,
            func.lower(func.coalesce(SupplierLedger.reference_type, 'payment')),
            func.coalesce(func.sum(SupplierLedger.amount), 0.0)
        ).filter(
            SupplierLedger.is_void == False,
            SupplierLedger.entry_type == 'credit',
            func.lower(func.coalesce(SupplierLedger.reference_type, 'payment')).in_(['payment', 'tip', 'settlement'])
        ).group_by(SupplierLedger.supplier_id, func.lower(func.coalesce(SupplierLedger.reference_type, 'payment'))).all()

        credit_total_rows = dict(db.session.query(
            SupplierLedger.supplier_id,
            func.coalesce(func.sum(SupplierLedger.amount), 0.0)
        ).filter(
            SupplierLedger.is_void == False,
            SupplierLedger.entry_type == 'credit'
        ).group_by(SupplierLedger.supplier_id).all())

        credit_kind_map = {}
        for sid, rtype, amt in credit_kind_rows:
            credit_kind_map[(int(sid or 0), (rtype or 'payment'))] = float(amt or 0.0)
        purchase_total_rows = dict(db.session.query(
            PurchaseV2.supplier_id,
            func.coalesce(func.sum(PurchaseV2.total_amount), 0.0)
        ).filter(
            PurchaseV2.is_void == False
        ).group_by(PurchaseV2.supplier_id).all())

        suppliers = []
        for s in rows:
            debit = float(debit_rows.get(s.id, 0.0) or 0.0)
            credit = float(credit_total_rows.get(s.id, 0.0) or 0.0)
            profile = profile_map.get(int(s.id), {})
            stated_categories = [_material_category_label(cid, category_map=category_map)
                                 for cid in stated_category_map.get(int(s.id), [])]
            supplied_categories = [_material_category_label(cid, category_map=category_map)
                                   for cid in profile.get('category_ids', [])]
            if int(profile.get('uncategorized_count') or 0):
                # Say so out loud — otherwise a supplier that only ever
                # supplied legacy rows reads as having supplied nothing.
                supplied_categories.append(UNCATEGORIZED_LABEL)
            site_rows_for_supplier = sorted(supplied_sites_by_supplier.get(int(s.id), []),
                                            key=lambda r: (-r['value'], r['project_name'].lower()))
            stated_sites = [project_names.get(pid, f'Site #{pid}')
                            for pid in stated_site_map.get(int(s.id), [])]
            suppliers.append({
                '_hdc_entity': 'hdc_supplier',
                'id': s.id,
                'name': s.name,
                'phone': s.phone or '',
                'status': s.status or 'active',
                'balance': max(0.0, debit - credit),
                'purchase_total': float(purchase_total_rows.get(s.id, 0.0) or 0.0),
                'payment_total': float(credit_kind_map.get((s.id, 'payment'), 0.0) or 0.0),
                'tip_total': float(credit_kind_map.get((s.id, 'tip'), 0.0) or 0.0),
                'settlement_total': float(credit_kind_map.get((s.id, 'settlement'), 0.0) or 0.0),
                # material basis: what this supplier deals in, and what it
                # has actually supplied.
                'stated_categories': stated_categories,
                'supplied_categories': supplied_categories,
                'material_count': len(profile.get('material_ids', [])),
                'material_names': profile.get('material_names', []),
                'purchase_rows': int(profile.get('purchase_count') or 0),
                'supplied_qty': float(profile.get('quantity') or 0.0),
                'last_supply_date': profile.get('last_purchase_date'),
                # site basis: assigned sites and sites actually supplied, the
                # two directions of the same supplier ⇄ site tracking.
                'stated_sites': stated_sites,
                'supplied_sites': [r['project_name'] for r in site_rows_for_supplier],
                'supplied_site_rows': site_rows_for_supplier,
                'site_count': len({r['project_id'] for r in site_rows_for_supplier}),
                'delivered_value': float(sum(r['value'] for r in site_rows_for_supplier)),
                'project_names': project_names,
            })

        if sort_mode == 'purchases':
            suppliers.sort(key=lambda r: (-r['purchase_rows'], r['name'].lower()))
        elif sort_mode == 'balance':
            suppliers.sort(key=lambda r: (-r['balance'], r['name'].lower()))

        return render_template('purchase/purchase_v2_suppliers.html',
            suppliers=suppliers,
            q=q,
            projects=Project.query.order_by(Project.name.asc()).all(),
            materials=(MaterialV2.query.filter(MaterialV2.is_void == False)
                       .order_by(MaterialV2.name.asc()).all()),
            selected_material_id=filter_material_id,
            selected_project_id=filter_project_id,
            sort_mode=sort_mode,
            category_map=category_map,
            **_category_filter_context(filter_category_id))


    @app.route('/hdc/purchase-v2/suppliers/<int:supplier_id>')
    @login_required
    def hdc_purchase_v2_supplier_detail(supplier_id):
        supplier = Supplier.query.get_or_404(supplier_id)
        _repair_supplier_purchase_v2_ledger(supplier.id)
        if db.session.new or db.session.dirty:
            db.session.commit()
        # Newest purchase first — the row the operator just recorded is the one
        # they are looking for when they land back on this page.
        purchases = (PurchaseV2.query
                     .filter(PurchaseV2.supplier_id == supplier.id, PurchaseV2.is_void == False)
                     .order_by(PurchaseV2.created_at.desc(), PurchaseV2.id.desc())
                     .all())
        # The ledger stays chronological here: the running balance below is
        # accumulated row by row, so it must be read oldest → newest.  Only the
        # display order is reversed afterwards, and every row keeps the balance
        # it had at its own point in time.
        ledger_rows = (SupplierLedger.query
                       .filter(SupplierLedger.supplier_id == supplier.id, SupplierLedger.is_void == False)
                       .order_by(SupplierLedger.created_at.asc(), SupplierLedger.id.asc())
                       .all())
        purchase_total = float(sum(float(p.total_amount or 0.0) for p in purchases))
        debit_total = float(sum(float(r.amount or 0.0) for r in ledger_rows if (r.entry_type or '').strip().lower() == 'debit'))
        paid_total = float(sum(float(r.amount or 0.0) for r in ledger_rows if (r.entry_type or '').strip().lower() == 'credit'))
        net_balance = float(debit_total - paid_total)
        balance = max(0.0, net_balance)
        advance_total = max(0.0, -net_balance)
        purchase_ids = [int(p.id) for p in purchases]
        delivered_map = {}
        if purchase_ids:
            delivered_map = dict(
                db.session.query(
                    Delivery.purchase_id,
                    func.coalesce(func.sum(Delivery.quantity), 0.0)
                ).filter(
                    Delivery.is_void == False,
                    Delivery.purchase_id.in_(purchase_ids)
                ).group_by(Delivery.purchase_id).all()
            )
        payment_total = float(sum(float(r.amount or 0.0) for r in ledger_rows if (r.entry_type or '').strip().lower() == 'credit' and (r.reference_type or 'payment').strip().lower() == 'payment'))
        tip_total = float(sum(float(r.amount or 0.0) for r in ledger_rows if (r.entry_type or '').strip().lower() == 'credit' and (r.reference_type or '').strip().lower() == 'tip'))
        settlement_total = float(sum(float(r.amount or 0.0) for r in ledger_rows if (r.entry_type or '').strip().lower() == 'credit' and (r.reference_type or '').strip().lower() == 'settlement'))
        materials = MaterialV2.query.filter_by(is_void=False).order_by(MaterialV2.name.asc()).all()
        purchase_map = {int(p.id): p for p in purchases}
        ledger_items = []
        running_balance = 0.0
        for r in ledger_rows:
            entry_type = (r.entry_type or '').strip().lower()
            ref_type = (r.reference_type or '').strip().lower()
            ref_id = int(r.reference_id or 0)
            linked_purchase = purchase_map.get(ref_id) if ref_type == 'purchase_v2' and ref_id else None
            material_name = (linked_purchase.material.name if linked_purchase and linked_purchase.material else '-')
            qty = float(linked_purchase.quantity or 0.0) if linked_purchase else 0.0
            unit_price = float(linked_purchase.unit_price or 0.0) if linked_purchase else 0.0
            total_price = float(linked_purchase.total_amount or 0.0) if linked_purchase else 0.0
            amount = float(r.amount or 0.0)
            paid_amount = amount if entry_type == 'credit' else 0.0
            purchase_amount = amount if entry_type == 'debit' else 0.0
            if entry_type == 'debit':
                running_balance += amount
            elif entry_type == 'credit':
                running_balance -= amount
            balance_kind = 'payable' if running_balance > 1e-9 else ('advance' if running_balance < -1e-9 else 'settled')
            if entry_type == 'debit':
                entry_label = ('Opening Balance' if ref_type == 'opening_balance' else 'Purchase')
            else:
                entry_label = (ref_type.title() if ref_type else 'Payment')
            ledger_items.append({
                '_hdc_entity': 'hdc_supplier_ledger',
                '_hdc_id': r.id,
                'id': r.id,
                'date': r.created_at,
                'supplier': supplier.name,
                'entry_label': entry_label,
                'material_name': material_name,
                'qty': qty,
                'unit_price': unit_price,
                'total_price': total_price,
                'purchase_amount': purchase_amount,
                'paid': paid_amount,
                'entry_type': entry_type,
                'balance_amount': running_balance,
                'balance_kind': balance_kind,
                'note': r.note or '',
                'reference_type': r.reference_type or '',
                'reference_id': r.reference_id
            })
        # Newest entry on top, exactly as the accounts and delivery registers
        # now read.  The balances were already computed in chronological order.
        ledger_items.reverse()
        # Supplier tracking: what it deals in vs what it has supplied, and the
        # sites it is assigned to vs the sites it has actually served.
        category_map = _material_category_map()
        stated_category_ids = _supplier_category_ids(supplier.id)
        stated_project_ids = _supplier_site_ids(supplier.id)
        supplied_category_rows = _supplier_category_supplied_rows(supplier.id)
        supplied_site_rows = _supplier_site_supplied_rows(supplier.id)
        linked_category_ids = set(int(c) for c in stated_category_ids)
        supplied_category_ids = set(int(r['category_id']) for r in supplied_category_rows)
        project_names = {int(p.id): p.name for p in Project.query.all()}
        return render_template('purchase/purchase_v2_supplier_detail.html',
            supplier=supplier,
            purchases=purchases,
            delivered_map=delivered_map,
            materials=materials,
            material_units=_MATERIAL_V2_UNITS,
            ledger_rows=ledger_rows,
            ledger_items=ledger_items,
            purchase_total=purchase_total,
            debit_total=debit_total,
            paid_total=paid_total,
            balance=balance,
            advance_total=advance_total,
            payment_total=payment_total,
            tip_total=tip_total,
            settlement_total=settlement_total,
            stated_category_ids=linked_category_ids,
            stated_project_ids=set(int(p) for p in stated_project_ids),
            supplied_category_rows=supplied_category_rows,
            supplied_category_ids=supplied_category_ids,
            supplied_site_rows=supplied_site_rows,
            supplied_site_ids={int(r['project_id']) for r in supplied_site_rows},
            project_names=project_names,
            # The **line's** item type, resolved by the one shared rule — the
            # same number the Purchases register shows for the same row.  The
            # item's own type is carried alongside so an override stays
            # visible instead of silently disagreeing with the register.
            material_category_names={
                int(p.id): _material_category_label(purchase_resolved_category_id(p), category_map=category_map)
                for p in purchases
            },
            material_own_category_names={
                int(p.id): _material_category_label(material_category_id(p.material), category_map=category_map)
                for p in purchases if p.material
            },
            category_map=category_map,
            category_options=_material_category_options(),
            projects=Project.query.order_by(Project.name.asc()).all()
        )


    @app.route('/hdc/purchase-v2/suppliers/<int:supplier_id>/purchase', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_purchase_v2_supplier_purchase(supplier_id):
        supplier = Supplier.query.get_or_404(supplier_id)
        if supplier.is_void or (supplier.status or 'active').strip().lower() != 'active':
            flash('Supplier is not active.', 'danger')
            return redirect(url_for('hdc_purchase_v2_supplier_detail', supplier_id=supplier.id))
        material_id = request.form.get('material_id', type=int)
        unit_price = max(0.0, _flt(request.form.get('unit_price'), 0.0))
        quantity = max(0.0, _flt(request.form.get('quantity'), 0.0))
        payment_status = (request.form.get('payment_status') or 'unpaid').strip().lower()
        if payment_status not in ('paid', 'unpaid'):
            payment_status = 'unpaid'
        material = MaterialV2.query.get(material_id) if material_id else None
        if (not material) or material.is_void or (material.status or 'active').strip().lower() != 'active':
            flash('Select a valid active material.', 'danger')
            return redirect(url_for('hdc_purchase_v2_supplier_detail', supplier_id=supplier.id))
        if unit_price <= 0 or quantity <= 0:
            flash('Unit price and quantity must be greater than 0.', 'danger')
            return redirect(url_for('hdc_purchase_v2_supplier_detail', supplier_id=supplier.id))
        total_amount = float(unit_price * quantity)
        category = _material_category_by_id(request.form.get('category_id'), active_only=True)
        row = PurchaseV2(
            supplier_id=supplier.id,
            material_id=material.id,
            category_id=(int(category.id) if category else (int(material.category_id) if material.category_id else None)),
            unit_price=unit_price,
            quantity=quantity,
            total_amount=total_amount,
            payment_status=payment_status,
            is_void=False,
            created_at=_pkt_now_naive(),
            updated_at=_pkt_now_naive()
        )
        db.session.add(row)
        db.session.flush()
        _sync_purchase_v2_ledger(row)
        if (payment_status or '').strip().lower() == 'paid':
            ok_txn, msg_txn, _ = _accounts_upsert_purchase_paid_txn(row, supplier_name=supplier.name, commit=False)
            if not ok_txn:
                db.session.rollback()
                flash(msg_txn or 'Unable to post paid purchase in unified accounts.', 'danger')
                return redirect(url_for('hdc_purchase_v2_supplier_detail', supplier_id=supplier.id))
        log_action(
            current_user,
            'create',
            f'{current_user.username.title()} created purchase #{row.id}: {supplier.name}, {material.name}, {quantity:.2f} x {unit_price:.2f} = {total_amount:.2f} ({payment_status})',
            'purchase_v2',
            row.id
        )
        db.session.commit()
        flash(f'Purchase #{row.id} recorded for {supplier.name}.', 'success')
        return redirect(url_for('hdc_purchase_v2_supplier_detail', supplier_id=supplier.id))


    @app.route('/hdc/purchase-v2/suppliers/<int:supplier_id>/payment', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_purchase_v2_supplier_payment(supplier_id):
        supplier = Supplier.query.get_or_404(supplier_id)
        amount = max(0.0, _flt(request.form.get('amount'), 0.0))
        entry_kind = ((request.form.get('entry_kind') or 'payment').strip().lower())
        note = (request.form.get('note') or '').strip()
        if entry_kind not in ('payment', 'tip', 'settlement'):
            entry_kind = 'payment'
        if amount <= 0:
            flash('Amount must be greater than 0.', 'danger')
            return redirect(url_for('hdc_purchase_v2_supplier_detail', supplier_id=supplier.id))

        ledger_row = SupplierLedger(
            supplier_id=supplier.id,
            entry_type='credit',
            amount=amount,
            reference_type=entry_kind,
            reference_id=None,
            note=note or f'Supplier {entry_kind}',
            is_void=False,
            created_at=_pkt_now_naive()
        )
        db.session.add(ledger_row)
        db.session.flush()
        ok_txn, msg_txn, _ = _accounts_post_supplier_credit_row(ledger_row, supplier_name=supplier.name, commit=False)
        if not ok_txn:
            db.session.rollback()
            flash(msg_txn or 'Unable to post supplier payment in unified accounts.', 'danger')
            return redirect(url_for('hdc_purchase_v2_supplier_detail', supplier_id=supplier.id))
        _sync_supplier_po_payment_status(supplier.id)
        log_action(current_user, 'payment', f'{current_user.username.title()} recorded supplier {entry_kind}: {supplier.name}, {amount:.2f} PKR', f'supplier_{entry_kind}', supplier.id)
        db.session.commit()
        flash(f'{entry_kind.title()} posted successfully.', 'success')
        return redirect(url_for('hdc_purchase_v2_supplier_detail', supplier_id=supplier.id))


    @app.route('/hdc/purchase-v2/suppliers/<int:supplier_id>/edit', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_purchase_v2_supplier_edit(supplier_id):
        supplier = Supplier.query.get_or_404(supplier_id)
        name = _normalize_name_ci(request.form.get('name')) or supplier.name
        phone = (request.form.get('phone') or '').strip()
        status = (request.form.get('status') or supplier.status or 'active').strip().lower()
        opening_balance_add = max(0.0, _flt(request.form.get('opening_balance_add'), 0.0))
        if status not in ('active', 'inactive'):
            status = 'active'

        exists = (Supplier.query
                  .filter(
                      Supplier.id != supplier.id,
                      Supplier.is_void == False,
                      func.lower(Supplier.name) == name.lower()
                  )
                  .first())
        if exists:
            flash('Another supplier already uses this name.', 'danger')
            back = (request.referrer or '').strip()
            if '/hdc/purchase-v2/suppliers' in back:
                return redirect(back)
            return redirect(url_for('hdc_purchase_v2_supplier_detail', supplier_id=supplier.id))

        supplier.name = name
        supplier.phone = phone
        supplier.status = status
        supplier.updated_at = _pkt_now_naive()
        if opening_balance_add > 0:
            db.session.add(SupplierLedger(
                supplier_id=supplier.id,
                entry_type='debit',
                amount=float(opening_balance_add),
                reference_type='opening_balance',
                reference_id=None,
                note='Opening balance adjustment',
                is_void=False,
                created_at=_pkt_now_naive()
            ))
        log_action(current_user, 'update', f'{current_user.username.title()} updated supplier #{supplier.id}: {supplier.name}', 'supplier', supplier.id)
        db.session.commit()
        flash('Supplier updated.', 'success')
        back = (request.referrer or '').strip()
        if '/hdc/purchase-v2/suppliers' in back:
            return redirect(back)
        return redirect(url_for('hdc_purchase_v2_supplier_detail', supplier_id=supplier.id))


    @app.route('/hdc/purchase-v2/suppliers/<int:supplier_id>/suspend', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_purchase_v2_supplier_suspend(supplier_id):
        supplier = Supplier.query.get_or_404(supplier_id)
        mode = (request.form.get('mode') or 'suspend').strip().lower()
        supplier.status = 'inactive' if mode == 'suspend' else 'active'
        supplier.updated_at = _pkt_now_naive()
        log_action(current_user, 'update', f'{current_user.username.title()} changed supplier #{supplier.id} status to {supplier.status}', 'supplier', supplier.id)
        db.session.commit()
        flash(f'Supplier status set to {supplier.status}.', 'success')
        back = (request.referrer or '').strip()
        if '/hdc/purchase-v2/suppliers' in back:
            return redirect(back)
        return redirect(url_for('hdc_purchase_v2_supplier_detail', supplier_id=supplier.id))


    @app.route('/hdc/purchase-v2/delivered', methods=['GET', 'POST'])
    @login_required
    @_money_write_required()
    def hdc_purchase_v2_delivered():
        if request.method == 'POST':
            purchase_id = request.form.get('purchase_id', type=int)
            project_id = request.form.get('project_id', type=int)
            stage_id = request.form.get('stage_id', type=int)
            quantity = max(0.0, _flt(request.form.get('quantity'), 0.0))
            delivery_person = (request.form.get('delivery_person') or '').strip()
            purchase = PurchaseV2.query.get(purchase_id) if purchase_id else None
            project = Project.query.get(project_id) if project_id else None
            stage = Stage.query.get(stage_id) if stage_id else None
            if (not purchase) or purchase.is_void:
                flash('Valid purchase is required.', 'danger')
                return redirect(url_for('hdc_purchase_v2_delivered'))
            if not project:
                flash('Valid project is required.', 'danger')
                return redirect(url_for('hdc_purchase_v2_delivered'))
            if stage_id and ((not stage) or int(stage.project_id or 0) != int(project.id)):
                flash('Selected stage does not belong to selected project.', 'danger')
                return redirect(url_for('hdc_purchase_v2_delivered'))
            if quantity <= 0:
                flash('Quantity must be greater than 0.', 'danger')
                return redirect(url_for('hdc_purchase_v2_delivered'))
            delivered_so_far = _purchase_v2_delivered_qty(purchase.id, integrity=True)
            remaining_purchase_qty = max(0.0, float(purchase.quantity or 0.0) - delivered_so_far)
            if quantity > remaining_purchase_qty + 1e-9:
                flash(integrity_message('Cannot deliver more than the remaining purchase quantity.', f'Cannot deliver more than remaining purchase quantity ({remaining_purchase_qty:,.2f}).'), 'danger')
                return redirect(url_for('hdc_purchase_v2_delivered'))
            _del_date_raw = (request.form.get('date') or '').strip()
            try:
                _del_date = datetime.strptime(_del_date_raw, '%Y-%m-%d').date() if _del_date_raw else _pkt_today()
            except ValueError:
                _del_date = _pkt_today()
            _del_notes = (request.form.get('notes') or '').strip() or None
            row = Delivery(
                purchase_id=purchase.id,
                material_id=purchase.material_id,
                project_id=project.id,
                stage_id=stage.id if stage else None,
                quantity=quantity,
                date=_del_date,
                notes=_del_notes,
                delivery_person=delivery_person,
                is_void=False,
                created_at=_pkt_now_naive()
            )
            db.session.add(row)
            db.session.flush()
            log_action(
                current_user,
                'delivery',
                f'{current_user.username.title()} recorded delivery #{row.id}: {purchase.material.name if purchase.material else "-"} {quantity:.2f} to {project.name} -> {stage.name if stage else "-"}',
                'delivery',
                row.id
            )
            db.session.commit()
            flash(f'Delivery #{row.id} recorded.', 'success')
            return redirect(url_for('hdc_purchase_v2_delivered'))
        # ── register filters: every one optional, all combinable ───────────
        # The operator standing at a site wants this register narrowed to what
        # they just did, so the same fields the form fills in (site, stage,
        # material, PO) are the fields the list can be filtered by.
        filter_po_raw = (request.args.get('po_number') or '').strip()
        filter_po_number = None
        if filter_po_raw:
            try:
                filter_po_number = int(filter_po_raw)
            except (TypeError, ValueError):
                flash('PO number must be numeric.', 'warning')
        filter_material_id = request.args.get('material_id', type=int)
        filter_supplier_id = request.args.get('supplier_id', type=int)
        filter_project_id = request.args.get('project_id', type=int)
        filter_stage_id = request.args.get('stage_id', type=int)
        filter_category_id = _category_filter_arg()
        filter_date_from = _parse_date((request.args.get('date_from') or '').strip(), fallback=None)
        filter_date_to = _parse_date((request.args.get('date_to') or '').strip(), fallback=None)
        sort_mode = (request.args.get('sort') or 'newest').strip().lower()
        if sort_mode not in REGISTER_SORT_MODES:
            sort_mode = 'newest'

        dq = Delivery.query.filter(Delivery.is_void == False)
        if filter_po_number:
            dq = dq.filter(Delivery.purchase_id == filter_po_number)
        if filter_material_id:
            dq = dq.filter(Delivery.material_id == filter_material_id)
        if filter_supplier_id:
            dq = (dq.join(PurchaseV2, Delivery.purchase_id == PurchaseV2.id)
                    .filter(PurchaseV2.supplier_id == filter_supplier_id))
        if filter_project_id:
            dq = dq.filter(Delivery.project_id == filter_project_id)
        if filter_stage_id:
            dq = dq.filter(Delivery.stage_id == filter_stage_id)
        if filter_category_id is not None:
            dq = dq.filter(_delivery_category_condition(filter_category_id))
        if filter_date_from:
            dq = dq.filter(Delivery.date >= filter_date_from)
        if filter_date_to:
            dq = dq.filter(Delivery.date <= filter_date_to)
        # Newest entry first unless the operator asks for another arrangement;
        # id breaks ties for rows entered in the same second.
        delivery_sort = {
            'newest': (Delivery.created_at.desc(), Delivery.id.desc()),
            'oldest': (Delivery.created_at.asc(), Delivery.id.asc()),
            'po_asc': (Delivery.purchase_id.asc(), Delivery.created_at.desc(), Delivery.id.desc()),
            'po_desc': (Delivery.purchase_id.desc(), Delivery.created_at.desc(), Delivery.id.desc()),
        }[sort_mode]
        rows = dq.order_by(*delivery_sort).all()
        prefill_shift_material_id = request.args.get('shift_material_id', type=int)
        prefill_from_project_id = request.args.get('from_project_id', type=int)
        prefill_from_stage_id = request.args.get('from_stage_id', type=int)
        prefill_to_project_id = request.args.get('to_project_id', type=int)
        prefill_to_stage_id = request.args.get('to_stage_id', type=int)
        edit_delivery_id = request.args.get('edit_delivery_id', type=int)
        prefill_edit_row = None
        if edit_delivery_id:
            prefill_edit_row = Delivery.query.filter(
                Delivery.id == edit_delivery_id,
                Delivery.is_void == False
            ).first()
        total_qty = float(sum(float(r.quantity or 0.0) for r in rows))
        delivery_total_count = int(db.session.query(func.count(Delivery.id))
                                   .filter(Delivery.is_void == False).scalar() or 0)
        projects = Project.query.order_by(Project.name.asc()).all()
        stages = Stage.query.order_by(Stage.name.asc()).all()
        suppliers = Supplier.query.filter(Supplier.is_void == False).order_by(Supplier.name.asc()).all()
        # One read serves three things on this page: the PO dropdown, the
        # Pending Purchases pop-up and the "materials that have stock" hint.
        pending_rows = _purchase_v2_pending_rows()
        purchase_rows = [{
            'id': pr['id'],
            'supplier_name': pr['supplier_name'],
            'material_name': pr['material_name'],
            'material_id': pr['material_id'],
            'unit': pr['unit'],
            'total_qty': pr['ordered_qty'],
            'delivered_qty': pr['delivered_qty'],
            'remaining_qty': pr['pending_qty']
        } for pr in pending_rows]
        pending_total_qty = float(sum(float(pr['pending_qty'] or 0.0) for pr in pending_rows))
        pending_total_value = float(sum(float(pr['pending_value'] or 0.0) for pr in pending_rows))
        all_materials = MaterialV2.query.filter_by(is_void=False).order_by(MaterialV2.name.asc()).all()
        material_ids_with_stock = {int(pr['material_id']) for pr in purchase_rows}
        return render_template('purchase/purchase_v2_delivered.html',
            rows=rows,
            total_qty=total_qty,
            delivery_total_count=delivery_total_count,
            projects=projects,
            stages=stages,
            suppliers=suppliers,
            purchase_rows=purchase_rows,
            all_materials=all_materials,
            material_ids_with_stock=material_ids_with_stock,
            pending_rows=pending_rows,
            pending_total_qty=pending_total_qty,
            pending_total_value=pending_total_value,
            filter_po_number=filter_po_raw,
            selected_material_id=filter_material_id,
            selected_supplier_id=filter_supplier_id,
            selected_project_id=filter_project_id,
            selected_stage_id=filter_stage_id,
            filter_date_from=(filter_date_from.isoformat() if filter_date_from else ''),
            filter_date_to=(filter_date_to.isoformat() if filter_date_to else ''),
            sort_mode=sort_mode,
            today=_pkt_today().isoformat(),
            category_map=_material_category_map(),
            **_category_filter_context(filter_category_id),
            prefill_shift_material_id=prefill_shift_material_id,
            prefill_from_project_id=prefill_from_project_id,
            prefill_from_stage_id=prefill_from_stage_id,
            prefill_to_project_id=prefill_to_project_id,
            prefill_to_stage_id=prefill_to_stage_id,
            prefill_edit_row=prefill_edit_row
        )


    @app.route('/hdc/purchase-v2/delivered/transfer', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_purchase_v2_delivery_transfer():
        material_id = request.form.get('material_id', type=int)
        from_project_id = request.form.get('from_project_id', type=int)
        from_stage_id = request.form.get('from_stage_id', type=int)
        to_project_id = request.form.get('to_project_id', type=int)
        to_stage_id = request.form.get('to_stage_id', type=int)
        qty = max(0.0, _flt(request.form.get('quantity'), 0.0))
        note = (request.form.get('notes') or '').strip()
        material = MaterialV2.query.get(material_id) if material_id else None
        from_project = Project.query.get(from_project_id) if from_project_id else None
        to_project = Project.query.get(to_project_id) if to_project_id else None
        from_stage = Stage.query.get(from_stage_id) if from_stage_id else None
        to_stage = Stage.query.get(to_stage_id) if to_stage_id else None

        if (not material) or material.is_void:
            flash('Valid material is required for stock shift.', 'danger')
            return redirect(url_for('hdc_purchase_v2_delivered'))
        if not from_project or not to_project:
            flash('Both source and destination projects are required.', 'danger')
            return redirect(url_for('hdc_purchase_v2_delivered'))
        if from_stage_id and ((not from_stage) or int(from_stage.project_id or 0) != int(from_project.id)):
            flash('Source stage does not belong to source project.', 'danger')
            return redirect(url_for('hdc_purchase_v2_delivered'))
        if to_stage_id and ((not to_stage) or int(to_stage.project_id or 0) != int(to_project.id)):
            flash('Destination stage does not belong to destination project.', 'danger')
            return redirect(url_for('hdc_purchase_v2_delivered'))
        if int(from_project.id) == int(to_project.id) and int(from_stage_id or 0) == int(to_stage_id or 0):
            flash('Source and destination cannot be the same scope.', 'danger')
            return redirect(url_for('hdc_purchase_v2_delivered'))
        if qty <= 0:
            flash('Shift quantity must be greater than 0.', 'danger')
            return redirect(url_for('hdc_purchase_v2_delivered'))

        available = _material_v2_available(material.id, from_project.id, from_stage.id if from_stage else None, integrity=True)
        if qty > available + 1e-9:
            flash(integrity_message('Shift exceeds available source stock.', f'Shift exceeds source available stock ({available:,.2f}).'), 'danger')
            return redirect(url_for('hdc_purchase_v2_delivered'))

        _date_raw = (request.form.get('date') or '').strip()
        try:
            transfer_date = datetime.strptime(_date_raw, '%Y-%m-%d').date() if _date_raw else _pkt_today()
        except ValueError:
            transfer_date = _pkt_today()

        moved_rows = _transfer_v2_material_between_scopes(
            material_id=material.id,
            from_project_id=from_project.id,
            from_stage_id=(from_stage.id if from_stage else None),
            to_project_id=to_project.id,
            to_stage_id=(to_stage.id if to_stage else None),
            quantity=qty,
            date_value=transfer_date,
            note=note
        )
        moved_qty = float(sum(float(q or 0.0) for _, q in moved_rows))
        if moved_qty + 1e-9 < qty:
            db.session.rollback()
            flash('Unable to allocate selected quantity from source deliveries. Try a smaller quantity.', 'danger')
            return redirect(url_for('hdc_purchase_v2_delivered'))

        log_action(
            current_user,
            'delivery',
            f'{current_user.username.title()} shifted {material.name} {moved_qty:.2f} from {from_project.name} -> {to_project.name}',
            'delivery_transfer',
            material.id
        )
        db.session.commit()
        flash(f'Stock shifted successfully: {moved_qty:,.2f} {material.unit or ""}.', 'success')
        return redirect(url_for('hdc_purchase_v2_delivered'))


    @app.route('/hdc/purchase-v2/usage', methods=['GET', 'POST'])
    @login_required
    @_money_write_required()
    def hdc_purchase_v2_usage_page():
        if request.method == 'POST':
            purchase_id = request.form.get('purchase_id', type=int)
            material_id = request.form.get('material_id', type=int)
            project_id = request.form.get('project_id', type=int)
            stage_id = request.form.get('stage_id', type=int)
            quantity = max(0.0, _flt(request.form.get('quantity'), 0.0))
            purchase = PurchaseV2.query.get(purchase_id) if purchase_id else None
            material = MaterialV2.query.get(material_id) if material_id else None
            project = Project.query.get(project_id) if project_id else None
            stage = Stage.query.get(stage_id) if stage_id else None
            if (not purchase_id) or (not purchase) or purchase.is_void:
                flash('Valid purchase order is required.', 'danger')
                return redirect(url_for('hdc_purchase_v2_usage_page'))
            if (not material) or material.is_void:
                flash('Valid material is required.', 'danger')
                return redirect(url_for('hdc_purchase_v2_usage_page'))
            if int(purchase.material_id or 0) != int(material.id):
                flash('Selected purchase order does not match selected material.', 'danger')
                return redirect(url_for('hdc_purchase_v2_usage_page'))
            if not project:
                flash('Valid project is required.', 'danger')
                return redirect(url_for('hdc_purchase_v2_usage_page'))
            if not stage_id:
                flash('Stage is required for strict stock control.', 'danger')
                return redirect(url_for('hdc_purchase_v2_usage_page'))
            if stage_id and ((not stage) or int(stage.project_id or 0) != int(project.id)):
                flash('Selected stage does not belong to selected project.', 'danger')
                return redirect(url_for('hdc_purchase_v2_usage_page'))
            if quantity <= 0:
                flash('Quantity must be greater than 0.', 'danger')
                return redirect(url_for('hdc_purchase_v2_usage_page'))
            available = _purchase_v2_available_in_scope_qty(purchase.id, project.id, stage.id if stage else None, integrity=True)
            if quantity > available + 1e-9:
                flash(integrity_message('Usage exceeds available stock for this purchase order and stage.', f'Usage exceeds available stock for selected purchase order in this stage ({available:,.2f}).'), 'danger')
                return redirect(url_for('hdc_purchase_v2_usage_page'))
            unit_price = float(purchase.unit_price or 0.0)
            cost = float(unit_price * quantity)
            _use_date_raw = (request.form.get('date') or '').strip()
            try:
                _use_date = datetime.strptime(_use_date_raw, '%Y-%m-%d').date() if _use_date_raw else _pkt_today()
            except ValueError:
                _use_date = _pkt_today()
            _use_notes = (request.form.get('notes') or '').strip() or None
            row = UsageLogV2(
                purchase_id=purchase.id,
                material_id=material.id,
                project_id=project.id,
                stage_id=stage.id if stage else None,
                quantity=quantity,
                cost=cost,
                date=_use_date,
                notes=_use_notes,
                is_void=False,
                created_at=_pkt_now_naive()
            )
            db.session.add(row)
            db.session.flush()
            log_action(
                current_user,
                'usage',
                f'{current_user.username.title()} recorded usage #{row.id}: PO#{purchase.id}, {material.name} {quantity:.2f} @ {unit_price:.2f} ({cost:.2f} PKR) on {project.name} -> {stage.name if stage else "-"}',
                'usage',
                row.id
            )
            db.session.commit()
            flash(f'Usage #{row.id} recorded.', 'success')
            return redirect(url_for('hdc_purchase_v2_usage_page'))
        po_raw = (request.args.get('po_number') or '').strip()
        po_number = None
        if po_raw:
            try:
                po_number = int(po_raw)
            except (TypeError, ValueError):
                flash('PO number must be numeric.', 'warning')
        material_id = request.args.get('material_id', type=int)
        # Site + stage: usage is always booked against a scope, so the log has
        # to be readable one site at a time.
        filter_project_id = request.args.get('project_id', type=int)
        filter_stage_id = request.args.get('stage_id', type=int)
        filter_category_id = _category_filter_arg()
        date_from = _parse_date((request.args.get('date_from') or '').strip(), fallback=None)
        date_to = _parse_date((request.args.get('date_to') or '').strip(), fallback=None)
        sort_mode = (request.args.get('sort') or 'newest').strip().lower()
        if sort_mode not in REGISTER_SORT_MODES:
            sort_mode = 'newest'
        q = UsageLogV2.query.filter(UsageLogV2.is_void == False)
        if po_number:
            q = q.filter(UsageLogV2.purchase_id == po_number)
        if material_id:
            q = q.filter(UsageLogV2.material_id == material_id)
        if filter_project_id:
            q = q.filter(UsageLogV2.project_id == filter_project_id)
        if filter_stage_id:
            q = q.filter(UsageLogV2.stage_id == filter_stage_id)
        if filter_category_id is not None:
            q = q.filter(_usage_category_condition(filter_category_id))
        if date_from:
            q = q.filter(UsageLogV2.date >= date_from)
        if date_to:
            q = q.filter(UsageLogV2.date <= date_to)
        # Newest entries first; id breaks ties for rows created in the same second.
        usage_sort = {
            'newest': (UsageLogV2.created_at.desc(), UsageLogV2.id.desc()),
            'oldest': (UsageLogV2.created_at.asc(), UsageLogV2.id.asc()),
            'po_asc': (UsageLogV2.purchase_id.asc(), UsageLogV2.created_at.desc(), UsageLogV2.id.desc()),
            'po_desc': (UsageLogV2.purchase_id.desc(), UsageLogV2.created_at.desc(), UsageLogV2.id.desc()),
        }[sort_mode]
        rows = q.order_by(*usage_sort).all()
        total_qty = float(sum(float(r.quantity or 0.0) for r in rows))
        total_cost = float(sum(float(r.cost or 0.0) for r in rows))
        projects = Project.query.order_by(Project.name.asc()).all()
        stages = Stage.query.order_by(Stage.name.asc()).all()
        materials = MaterialV2.query.filter_by(is_void=False).order_by(MaterialV2.name.asc()).all()
        stock_rows = []
        material_available_map = {}
        for m in materials:
            delivered = _material_v2_delivered(m.id)
            used = _material_v2_used(m.id)
            available = max(0.0, delivered - used)
            stock_rows.append({
                'id': m.id,
                'name': m.name,
                'unit': m.unit,
                'delivered_qty': delivered,
                'used_qty': used,
                'available_qty': available
            })
            material_available_map[m.id] = available
        return render_template('purchase/purchase_v2_usage.html',
            rows=rows,
            total_qty=total_qty,
            total_cost=total_cost,
            projects=projects,
            stages=stages,
            materials=materials,
            stock_rows=stock_rows,
            material_available_map=material_available_map,
            selected_material_id=material_id,
            selected_project_id=filter_project_id,
            selected_stage_id=filter_stage_id,
            filter_po_number=po_raw,
            filter_date_from=(date_from.isoformat() if date_from else ''),
            filter_date_to=(date_to.isoformat() if date_to else ''),
            sort_mode=sort_mode,
            today=_pkt_today().isoformat(),
            category_map=_material_category_map(),
            **_category_filter_context(filter_category_id)
        )


    @app.route('/hdc/purchase-v2/stock')
    @login_required
    def hdc_purchase_v2_stock():
        project_id = request.args.get('project_id', type=int)
        stage_id = request.args.get('stage_id', type=int)
        material_id = request.args.get('material_id', type=int)
        category_id = _category_filter_arg()

        project = Project.query.get(project_id) if project_id else None
        stage = Stage.query.get(stage_id) if stage_id else None
        if stage_id and ((not stage) or (project and int(stage.project_id or 0) != int(project.id))):
            flash('Selected stage does not belong to selected project.', 'danger')
            return redirect(url_for('hdc_purchase_v2_stock', project_id=project_id, material_id=material_id))

        projects = Project.query.order_by(Project.name.asc()).all()
        stages = Stage.query.order_by(Stage.name.asc()).all()
        materials = MaterialV2.query.filter(MaterialV2.is_void == False).order_by(MaterialV2.name.asc()).all()
        if category_id is not None:
            materials = [m for m in materials
                         if (int(m.category_id) if m.category_id else UNCATEGORIZED_ID) == int(category_id)]
        if material_id:
            materials = [m for m in materials if int(m.id) == int(material_id)]

        scope_project_id = project.id if project else None
        scope_stage_id = stage.id if stage else None

        category_map = _material_category_map()
        summary_rows = []
        for m in materials:
            sent_qty = _material_v2_delivered(m.id, scope_project_id, scope_stage_id)
            used_qty = _material_v2_used(m.id, scope_project_id, scope_stage_id)
            pending_qty = max(0.0, sent_qty - used_qty)
            row = {
                'material': m,
                'category_id': (int(m.category_id) if m.category_id else UNCATEGORIZED_ID),
                'category_name': _material_category_label(material_category_id(m), category_map=category_map),
                'sent_qty': sent_qty,
                'used_qty': used_qty,
                'pending_qty': pending_qty
            }
            if sent_qty <= 1e-9 and used_qty <= 1e-9 and not material_id:
                continue
            summary_rows.append(row)

        dq = Delivery.query.filter(Delivery.is_void == False)
        if project:
            dq = dq.filter(Delivery.project_id == project.id)
        if stage:
            dq = dq.filter(Delivery.stage_id == stage.id)
        if material_id:
            dq = dq.filter(Delivery.material_id == material_id)
        if category_id is not None:
            dq = dq.filter(_delivery_category_condition(category_id))
        # Newest delivery first, same as the Delivery register.
        delivery_rows = dq.order_by(Delivery.created_at.desc(), Delivery.id.desc()).all()

        total_sent = float(sum(float(r.get('sent_qty') or 0.0) for r in summary_rows))
        total_used = float(sum(float(r.get('used_qty') or 0.0) for r in summary_rows))
        total_pending = float(sum(float(r.get('pending_qty') or 0.0) for r in summary_rows))
        # The denominator must cover the **same items** as the numerator, or
        # "Sent of Purchased" answers a question nobody asked: with a category
        # filter on, the sent figure was narrowed to that item type while the
        # purchased figure still counted every purchase in the book.
        scope_material_ids = [int(m.id) for m in materials]
        pq = PurchaseV2.query.filter(PurchaseV2.is_void == False)
        if scope_material_ids:
            pq = pq.filter(PurchaseV2.material_id.in_(scope_material_ids))
        else:
            pq = pq.filter(PurchaseV2.material_id == -1)
        total_purchased_qty = float(pq.with_entities(func.coalesce(func.sum(PurchaseV2.quantity), 0.0)).scalar() or 0.0)
        total_purchased_amount = float(pq.with_entities(func.coalesce(func.sum(PurchaseV2.total_amount), 0.0)).scalar() or 0.0)
        sent_of_purchased_pct = float((total_sent / total_purchased_qty) * 100.0) if total_purchased_qty > 1e-9 else 0.0
        used_of_sent_pct = float((total_used / total_sent) * 100.0) if total_sent > 1e-9 else 0.0

        return render_template('purchase/purchase_v2_stock.html',
            projects=projects,
            stages=stages,
            materials=(MaterialV2.query.filter(MaterialV2.is_void == False).order_by(MaterialV2.name.asc()).all()),
            summary_rows=summary_rows,
            delivery_rows=delivery_rows,
            selected_project_id=project_id,
            selected_stage_id=stage_id,
            selected_material_id=material_id,
            total_sent=total_sent,
            total_used=total_used,
            total_pending=total_pending,
            total_purchased_qty=total_purchased_qty,
            total_purchased_amount=total_purchased_amount,
            sent_of_purchased_pct=sent_of_purchased_pct,
            used_of_sent_pct=used_of_sent_pct,
            category_map=category_map,
            **_category_filter_context(category_id)
        )


    @app.route('/hdc/purchase-v2/delivered/<int:delivery_id>/void', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_purchase_v2_delivery_void(delivery_id):
        row = Delivery.query.get_or_404(delivery_id)
        if row.is_void:
            flash('Delivery already voided.', 'warning')
            return redirect(url_for('hdc_purchase_v2_delivered'))
        try:
            validate_delivery_reduction(row, 0)
        except ValueError as exc:
            flash(str(exc), 'danger')
            return redirect(url_for('hdc_purchase_v2_delivered'))
        reason = (request.form.get('void_reason') or '').strip() or 'Voided by user'
        row.is_void = True
        row.void_reason = reason
        row.voided_at = _pkt_now_naive()
        log_action(current_user, 'void', f'{current_user.username.title()} voided delivery #{row.id}: {reason}', 'delivery', row.id)
        db.session.commit()
        flash(f'Delivery #{delivery_id} voided.', 'success')
        return redirect(url_for('hdc_purchase_v2_delivered'))


    @app.route('/hdc/purchase-v2/delivered/<int:delivery_id>/edit', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_purchase_v2_delivery_edit(delivery_id):
        row = Delivery.query.get_or_404(delivery_id)
        if row.is_void:
            flash('Cannot edit a voided delivery.', 'danger')
            return redirect(url_for('hdc_purchase_v2_delivered'))
        _date_raw = (request.form.get('date') or '').strip()
        try:
            _date = datetime.strptime(_date_raw, '%Y-%m-%d').date() if _date_raw else (row.date or _pkt_today())
        except ValueError:
            _date = row.date or _pkt_today()
        qty = max(0.0, _flt(request.form.get('quantity'), row.quantity))
        if qty <= 0:
            flash('Quantity must be > 0.', 'danger')
            return redirect(url_for('hdc_purchase_v2_delivered'))
        purchase = PurchaseV2.query.get(row.purchase_id)
        if purchase:
            other_delivered = _purchase_v2_delivered_qty(purchase.id, exclude_delivery_id=row.id, integrity=True)
            remaining = max(0.0, float(purchase.quantity or 0.0) - other_delivered)
            if qty > remaining + 1e-9:
                flash(integrity_message('Quantity exceeds the remaining purchase stock.', f'Quantity exceeds remaining purchase qty ({remaining:,.2f}).'), 'danger')
                return redirect(url_for('hdc_purchase_v2_delivered'))
        try:
            validate_delivery_reduction(row, qty)
        except ValueError as exc:
            flash(str(exc), 'danger')
            return redirect(url_for('hdc_purchase_v2_delivered'))
        row.date = _date
        row.quantity = qty
        row.delivery_person = (request.form.get('delivery_person') or row.delivery_person or '').strip()
        row.notes = (request.form.get('notes') or '').strip() or None
        log_action(current_user, 'update', f'{current_user.username.title()} edited delivery #{row.id}', 'delivery', row.id)
        db.session.commit()
        flash(f'Delivery #{delivery_id} updated.', 'success')
        return redirect(url_for('hdc_purchase_v2_delivered'))


    @app.route('/hdc/purchase-v2/usage/<int:usage_id>/void', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_purchase_v2_usage_void(usage_id):
        row = UsageLogV2.query.get_or_404(usage_id)
        if row.is_void:
            flash('Usage record already voided.', 'warning')
            return redirect(url_for('hdc_purchase_v2_usage_page'))
        reason = (request.form.get('void_reason') or '').strip() or 'Voided by user'
        row.is_void = True
        row.void_reason = reason
        row.voided_at = _pkt_now_naive()
        log_action(current_user, 'void', f'{current_user.username.title()} voided usage #{row.id}: {reason}', 'usage', row.id)
        db.session.commit()
        flash(f'Usage #{usage_id} voided.', 'success')
        return redirect(url_for('hdc_purchase_v2_usage_page'))


    @app.route('/hdc/purchase-v2/usage/<int:usage_id>/edit', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_purchase_v2_usage_edit(usage_id):
        row = UsageLogV2.query.get_or_404(usage_id)
        if row.is_void:
            flash('Cannot edit a voided usage record.', 'danger')
            return redirect(url_for('hdc_purchase_v2_usage_page'))
        _date_raw = (request.form.get('date') or '').strip()
        try:
            _date = datetime.strptime(_date_raw, '%Y-%m-%d').date() if _date_raw else (row.date or _pkt_today())
        except ValueError:
            _date = row.date or _pkt_today()
        qty = max(0.0, _flt(request.form.get('quantity'), row.quantity))
        if qty <= 0:
            flash('Quantity must be > 0.', 'danger')
            return redirect(url_for('hdc_purchase_v2_usage_page'))
        project_id = request.form.get('project_id', type=int) or row.project_id
        stage_id   = request.form.get('stage_id', type=int) or None
        project = Project.query.get(project_id) if project_id else None
        stage = Stage.query.get(stage_id) if stage_id else None
        if not project:
            flash('Valid project is required.', 'danger')
            return redirect(url_for('hdc_purchase_v2_usage_page'))
        if not stage_id:
            flash('Stage is required for strict stock control.', 'danger')
            return redirect(url_for('hdc_purchase_v2_usage_page'))
        if stage_id and ((not stage) or int(stage.project_id or 0) != int(project.id)):
            flash('Selected stage does not belong to selected project.', 'danger')
            return redirect(url_for('hdc_purchase_v2_usage_page'))
        if row.purchase_id:
            purchase = PurchaseV2.query.get(row.purchase_id)
            if (not purchase) or purchase.is_void:
                flash('Linked purchase order is missing/voided; cannot edit this usage row.', 'danger')
                return redirect(url_for('hdc_purchase_v2_usage_page'))
            available_for_edit = _purchase_v2_available_in_scope_qty(
                purchase.id,
                project.id,
                stage.id if stage else None,
                exclude_usage_id=row.id, integrity=True
            )
            if qty > available_for_edit + 1e-9:
                flash(integrity_message('Edited usage exceeds available purchase stock in this stage.', f'Edited usage exceeds selected purchase order stock in this stage ({available_for_edit:,.2f}).'), 'danger')
                return redirect(url_for('hdc_purchase_v2_usage_page'))
            row.cost = float(float(purchase.unit_price or 0.0) * qty)
        else:
            del_q = db.session.query(func.coalesce(func.sum(Delivery.quantity), 0.0)).filter(
                Delivery.material_id == row.material_id,
                Delivery.is_void == False,
                Delivery.project_id == project.id
            )
            use_q = db.session.query(func.coalesce(func.sum(UsageLogV2.quantity), 0.0)).filter(
                UsageLogV2.material_id == row.material_id,
                UsageLogV2.is_void == False,
                UsageLogV2.project_id == project.id,
                UsageLogV2.id != row.id
            )
            if stage:
                del_q = del_q.filter(Delivery.stage_id == stage.id)
                use_q = use_q.filter(UsageLogV2.stage_id == stage.id)
            available_for_edit = max(0.0, float(integrity_query(del_q).scalar() or 0.0) - float(integrity_query(use_q).scalar() or 0.0))
            if qty > available_for_edit + 1e-9:
                flash(integrity_message('Edited usage exceeds available delivered stock.', f'Edited usage exceeds available delivered stock ({available_for_edit:,.2f}).'), 'danger')
                return redirect(url_for('hdc_purchase_v2_usage_page'))
            avg_cost = _material_v2_weighted_cost(row.material_id)
            row.cost = float(avg_cost * qty)
        row.date = _date
        row.quantity = qty
        row.project_id = project_id
        row.stage_id = stage_id
        row.notes = (request.form.get('notes') or '').strip() or None
        log_action(current_user, 'update', f'{current_user.username.title()} edited usage #{row.id}', 'usage', row.id)
        db.session.commit()
        flash(f'Usage #{usage_id} updated.', 'success')
        return redirect(url_for('hdc_purchase_v2_usage_page'))


    # ── Item-type categories ────────────────────────────────────────────────
    # The field's own words: "every purchase and item needs a category like
    # Electric Materials, Plumbing Materials, Cement, Steel … and a filter of
    # all categories and separate separate too."  This is that control panel:
    # the vocabulary, the per-category totals, and the bulk assign that fixes
    # legacy rows which have no category yet.

    @app.route('/hdc/purchase-v2/categories', methods=['GET', 'POST'])
    @login_required
    @_money_write_required()
    def hdc_purchase_v2_categories():
        if request.method == 'POST':
            name = _normalize_name_ci(request.form.get('name'))
            if not name:
                flash('Category name is required.', 'danger')
                return redirect(url_for('hdc_purchase_v2_categories'))
            existing = _material_category_by_name(name)
            if existing and not existing.is_void and (existing.status or 'active').strip().lower() == 'active':
                flash(f'Category "{existing.name}" already exists.', 'warning')
                return redirect(url_for('hdc_purchase_v2_categories'))
            row = _ensure_material_category(name)
            row.updated_at = _pkt_now_naive()
            log_action(current_user, 'create',
                       f'{current_user.username.title()} added material category: {row.name}',
                       'material_category', row.id)
            db.session.commit()
            flash(f'Category "{row.name}" saved.', 'success')
            return redirect(url_for('hdc_purchase_v2_categories'))

        categories = _material_category_summary_rows(include_inactive=True)
        materials = (MaterialV2.query.filter(MaterialV2.is_void == False)
                     .order_by(MaterialV2.name.asc()).all())
        uncategorized_materials = [m for m in materials if not m.category_id]
        totals = {
            # The "Uncategorized" row is a bucket that points at missing data,
            # not an item type — it must not be counted as one of them.
            'categories': len([c for c in categories if int(c['id']) != UNCATEGORIZED_ID]),
            'materials': len(materials),
            'uncategorized_materials': len(uncategorized_materials),
            'purchased_value': float(sum(float(c.get('purchased_value') or 0.0) for c in categories)),
            'purchased_qty': float(sum(float(c.get('purchased_qty') or 0.0) for c in categories)),
            'delivered_qty': float(sum(float(c.get('delivered_qty') or 0.0) for c in categories)),
            'used_qty': float(sum(float(c.get('used_qty') or 0.0) for c in categories)),
        }
        return render_template('purchase/purchase_v2_categories.html',
            categories=categories,
            materials=materials,
            uncategorized_materials=uncategorized_materials,
            totals=totals,
            uncategorized_label=UNCATEGORIZED_LABEL,
            **_category_filter_context(None)
        )


    @app.route('/hdc/purchase-v2/categories/<int:category_id>/edit', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_purchase_v2_category_edit(category_id):
        row = MaterialCategory.query.get_or_404(category_id)
        name = _normalize_name_ci(request.form.get('name')) or row.name
        status = (request.form.get('status') or row.status or 'active').strip().lower()
        if status not in ('active', 'inactive'):
            status = 'active'
        clash = (MaterialCategory.query
                 .filter(MaterialCategory.id != row.id,
                         MaterialCategory.is_void == False,
                         func.lower(MaterialCategory.name) == name.lower())
                 .first())
        if clash:
            flash('Another category already uses this name.', 'danger')
            return redirect(url_for('hdc_purchase_v2_categories'))
        row.name = name
        row.status = status
        row.updated_at = _pkt_now_naive()
        log_action(current_user, 'update',
                   f'{current_user.username.title()} updated material category #{row.id}: {row.name} ({status})',
                   'material_category', row.id)
        db.session.commit()
        flash('Category updated.', 'success')
        return redirect(url_for('hdc_purchase_v2_categories'))


    @app.route('/hdc/purchase-v2/categories/<int:category_id>/delete', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_purchase_v2_category_delete(category_id):
        row = MaterialCategory.query.get_or_404(category_id)
        counts = _material_category_usage_counts().get(int(row.id), {})
        in_use = int(counts.get('materials') or 0) + int(counts.get('purchases') or 0)
        if in_use:
            # Deleting would silently strip the item type off real records, so
            # the category steps aside instead — same rule materials follow.
            row.status = 'inactive'
            row.updated_at = _pkt_now_naive()
            log_action(current_user, 'update',
                       f'{current_user.username.title()} set material category #{row.id} inactive ({in_use} record(s) use it)',
                       'material_category', row.id)
            db.session.commit()
            flash(f'"{row.name}" is used by {in_use} record(s), so it was set inactive instead of deleted.', 'warning')
            return redirect(url_for('hdc_purchase_v2_categories'))
        row.is_void = True
        row.status = 'inactive'
        row.updated_at = _pkt_now_naive()
        log_action(current_user, 'delete',
                   f'{current_user.username.title()} deleted material category #{row.id}: {row.name}',
                   'material_category', row.id)
        db.session.commit()
        flash('Category deleted.', 'success')
        return redirect(url_for('hdc_purchase_v2_categories'))


    @app.route('/hdc/purchase-v2/categories/assign', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_purchase_v2_categories_assign():
        """Put legacy / new materials into a category in one action."""
        material_ids = request.form.getlist('material_ids[]') or request.form.getlist('material_ids')
        raw_category = (request.form.get('category_id') or '').strip()
        if raw_category in ('', '0', str(UNCATEGORIZED_ID)):
            category = None
            label = UNCATEGORIZED_LABEL
        else:
            category = _material_category_by_id(raw_category, active_only=True)
            if not category:
                flash('Select an active category.', 'danger')
                return redirect(url_for('hdc_purchase_v2_categories'))
            label = category.name
        if not material_ids:
            flash('Select at least one material to assign.', 'danger')
            return redirect(url_for('hdc_purchase_v2_categories'))
        updated = 0
        for raw_id in material_ids:
            raw_id = str(raw_id or '').strip()
            if not raw_id.isdigit():
                continue
            material = MaterialV2.query.get(int(raw_id))
            if (not material) or material.is_void:
                continue
            material.category_id = (int(category.id) if category else None)
            material.updated_at = _pkt_now_naive()
            updated += 1
        if not updated:
            flash('No matching material was found.', 'warning')
            return redirect(url_for('hdc_purchase_v2_categories'))
        log_action(current_user, 'update',
                   f'{current_user.username.title()} set {updated} material(s) to category {label}',
                   'material_category', (category.id if category else 0))
        db.session.commit()
        flash(f'{updated} material(s) set to "{label}".', 'success')
        return redirect(url_for('hdc_purchase_v2_categories'))


    @app.route('/hdc/purchase-v2/categories/sync-purchases', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_purchase_v2_categories_sync_purchases():
        """Re-align purchase lines with their material's current category.

        A purchase line stores the category it was recorded under, so changing
        a material's item type never rewrites history.  When the operator does
        want the history moved (a material was filed under the wrong type, for
        example), this button does it explicitly and says how many rows moved.
        """
        moved = 0
        rows = (db.session.query(PurchaseV2)
                .filter(PurchaseV2.is_void == False)
                .all())
        for row in rows:
            material = row.material
            wanted = (int(material.category_id) if material and material.category_id else None)
            if (row.category_id or None) != wanted:
                row.category_id = wanted
                row.updated_at = _pkt_now_naive()
                moved += 1
        if not moved:
            flash('Every purchase line already matches its material category.', 'info')
            return redirect(url_for('hdc_purchase_v2_categories'))
        log_action(current_user, 'update',
                   f'{current_user.username.title()} re-applied material categories to {moved} purchase line(s)',
                   'material_category', 0)
        db.session.commit()
        flash(f'{moved} purchase line(s) re-aligned with their material category.', 'success')
        return redirect(url_for('hdc_purchase_v2_categories'))


    # ── Item-wise register: purchase / delivery / usage per item ────────────

    @app.route('/hdc/purchase-v2/items')
    @login_required
    def hdc_purchase_v2_items():
        """Every item with what was bought for it, sent to sites and used.

        This is the "on item base" view the field asked for: one line per item
        (material), with its category, and the three flows side by side.  The
        category filter takes "All Categories" or any single category — and
        each row links to that item's own ledger, where the three registers are
        listed entry by entry.
        """
        category_id = _category_filter_arg()
        material_id = request.args.get('material_id', type=int)
        project_id = request.args.get('project_id', type=int)
        stage_id = request.args.get('stage_id', type=int)
        date_from = _parse_date((request.args.get('date_from') or '').strip(), fallback=None)
        date_to = _parse_date((request.args.get('date_to') or '').strip(), fallback=None)
        sort_mode = (request.args.get('sort') or 'value').strip().lower()
        if sort_mode not in ('value', 'qty', 'name'):
            sort_mode = 'value'

        project = Project.query.get(project_id) if project_id else None
        stage = Stage.query.get(stage_id) if stage_id else None
        if stage_id and ((not stage) or (project and int(stage.project_id or 0) != int(project.id))):
            flash('Selected stage does not belong to selected project.', 'danger')
            return redirect(url_for('hdc_purchase_v2_items', project_id=project_id, material_id=material_id))

        mq = MaterialV2.query.filter(MaterialV2.is_void == False)
        if material_id:
            mq = mq.filter(MaterialV2.id == int(material_id))
        if category_id is not None:
            mq = mq.filter(_material_category_condition(category_id))
        materials = mq.order_by(MaterialV2.name.asc()).all()
        material_ids = [int(m.id) for m in materials]

        purchased_map = {}
        purchase_count_map = {}
        if material_ids:
            pq = (db.session.query(
                    PurchaseV2.material_id,
                    func.coalesce(func.sum(PurchaseV2.quantity), 0.0),
                    func.coalesce(func.sum(PurchaseV2.total_amount), 0.0),
                    func.count(PurchaseV2.id))
                  .filter(PurchaseV2.is_void == False, PurchaseV2.material_id.in_(material_ids)))
            if date_from:
                pq = pq.filter(PurchaseV2.date >= date_from)
            if date_to:
                pq = pq.filter(PurchaseV2.date <= date_to)
            for mid, qty, value, count in pq.group_by(PurchaseV2.material_id).all():
                purchased_map[int(mid)] = (float(qty or 0.0), float(value or 0.0), int(count or 0))

            dq = (db.session.query(
                    Delivery.material_id,
                    func.coalesce(func.sum(Delivery.quantity), 0.0),
                    func.count(Delivery.id))
                  .filter(Delivery.is_void == False, Delivery.material_id.in_(material_ids)))
            uq = (db.session.query(
                    UsageLogV2.material_id,
                    func.coalesce(func.sum(UsageLogV2.quantity), 0.0),
                    func.coalesce(func.sum(UsageLogV2.cost), 0.0),
                    func.count(UsageLogV2.id))
                  .filter(UsageLogV2.is_void == False, UsageLogV2.material_id.in_(material_ids)))
            if project:
                dq = dq.filter(Delivery.project_id == project.id)
                uq = uq.filter(UsageLogV2.project_id == project.id)
            if stage:
                dq = dq.filter(Delivery.stage_id == stage.id)
                uq = uq.filter(UsageLogV2.stage_id == stage.id)
            if date_from:
                dq = dq.filter(Delivery.date >= date_from)
                uq = uq.filter(UsageLogV2.date >= date_from)
            if date_to:
                dq = dq.filter(Delivery.date <= date_to)
                uq = uq.filter(UsageLogV2.date <= date_to)
            delivered_map = {int(mid): (float(qty or 0.0), int(count or 0))
                             for mid, qty, count in dq.group_by(Delivery.material_id).all()}
            used_map = {int(mid): (float(qty or 0.0), float(cost or 0.0), int(count or 0))
                        for mid, qty, cost, count in uq.group_by(UsageLogV2.material_id).all()}
        else:
            delivered_map, used_map = {}, {}
        all_delivered_map = {int(mid): float(qty or 0.0)
                             for mid, qty in db.session.query(Delivery.material_id,
                                                              func.coalesce(func.sum(Delivery.quantity), 0.0))
                             .filter(Delivery.is_void == False).group_by(Delivery.material_id).all()} \
            if material_ids else {}
        # "In store" is a **balance**, not a flow: what is physically on hand.
        # It therefore always reads the full history — the same way the item
        # ledger does — so a date or site filter (which narrows the flow
        # columns) can never make the store look empty.
        all_purchased_map = {int(mid): float(qty or 0.0)
                             for mid, qty in db.session.query(PurchaseV2.material_id,
                                                              func.coalesce(func.sum(PurchaseV2.quantity), 0.0))
                             .filter(PurchaseV2.is_void == False).group_by(PurchaseV2.material_id).all()} \
            if material_ids else {}

        category_map = _material_category_map()
        rows = []
        for m in materials:
            mid = int(m.id)
            purchased_qty, purchased_value, purchase_count = purchased_map.get(mid, (0.0, 0.0, 0))
            delivered_qty, delivery_count = delivered_map.get(mid, (0.0, 0))
            used_qty, used_value, usage_count = used_map.get(mid, (0.0, 0.0, 0))
            lifetime_delivered = float(all_delivered_map.get(mid, 0.0) or 0.0)
            rows.append({
                '_hdc_entity': 'hdc_material_v2',
                '_hdc_id': mid,
                'id': mid,
                'name': m.name,
                'unit': m.unit or '',
                'status': (m.status or 'active'),
                'category_id': (int(m.category_id) if m.category_id else UNCATEGORIZED_ID),
                'category_name': _material_category_label(material_category_id(m), category_map=category_map),
                'purchased_qty': purchased_qty,
                'purchased_value': purchased_value,
                'purchase_count': purchase_count,
                'delivered_qty': delivered_qty,
                'delivery_count': delivery_count,
                'used_qty': used_qty,
                'used_value': used_value,
                'usage_count': usage_count,
                'in_store_qty': max(0.0, all_purchased_map.get(mid, 0.0) - lifetime_delivered),
                'at_site_qty': max(0.0, delivered_qty - used_qty),
            })
        if sort_mode == 'name':
            rows.sort(key=lambda r: r['name'].lower())
        elif sort_mode == 'qty':
            rows.sort(key=lambda r: (-r['purchased_qty'], r['name'].lower()))
        else:
            rows.sort(key=lambda r: (-r['purchased_value'], r['name'].lower()))

        # Category strip: the same rows, grouped — the "all categories" answer
        # that sits above the "one category at a time" filter.
        strip = {}
        for row in rows:
            bucket = strip.setdefault(row['category_id'], {
                'category_id': row['category_id'],
                'category_name': row['category_name'],
                'items': 0, 'purchased_qty': 0.0, 'purchased_value': 0.0,
                'delivered_qty': 0.0, 'used_qty': 0.0
            })
            bucket['items'] += 1
            bucket['purchased_qty'] += row['purchased_qty']
            bucket['purchased_value'] += row['purchased_value']
            bucket['delivered_qty'] += row['delivered_qty']
            bucket['used_qty'] += row['used_qty']
        category_strip = sorted(strip.values(), key=lambda r: (-r['purchased_value'], r['category_name'].lower()))

        totals = {
            'items': len(rows),
            'purchased_qty': float(sum(r['purchased_qty'] for r in rows)),
            'purchased_value': float(sum(r['purchased_value'] for r in rows)),
            'delivered_qty': float(sum(r['delivered_qty'] for r in rows)),
            'used_qty': float(sum(r['used_qty'] for r in rows)),
            'in_store_qty': float(sum(r['in_store_qty'] for r in rows)),
            'at_site_qty': float(sum(r['at_site_qty'] for r in rows)),
        }
        return render_template('purchase/purchase_v2_items.html',
            rows=rows,
            totals=totals,
            category_strip=category_strip,
            projects=Project.query.order_by(Project.name.asc()).all(),
            stages=Stage.query.order_by(Stage.name.asc()).all(),
            materials=(MaterialV2.query.filter(MaterialV2.is_void == False)
                       .order_by(MaterialV2.name.asc()).all()),
            selected_material_id=material_id,
            selected_project_id=project_id,
            selected_stage_id=stage_id,
            filter_date_from=(date_from.isoformat() if date_from else ''),
            filter_date_to=(date_to.isoformat() if date_to else ''),
            sort_mode=sort_mode,
            scope_label=(project.name if project else 'All Sites'),
            **_category_filter_context(category_id)
        )


    @app.route('/hdc/purchase-v2/items/<int:material_id>')
    @login_required
    def hdc_purchase_v2_item_detail(material_id):
        """One item's ledger: its purchases, deliveries and usage entries."""
        material = MaterialV2.query.get_or_404(material_id)
        project_id = request.args.get('project_id', type=int)
        stage_id = request.args.get('stage_id', type=int)
        date_from = _parse_date((request.args.get('date_from') or '').strip(), fallback=None)
        date_to = _parse_date((request.args.get('date_to') or '').strip(), fallback=None)
        project = Project.query.get(project_id) if project_id else None
        stage = Stage.query.get(stage_id) if stage_id else None
        if stage_id and ((not stage) or (project and int(stage.project_id or 0) != int(project.id))):
            flash('Selected stage does not belong to selected project.', 'danger')
            return redirect(url_for('hdc_purchase_v2_item_detail', material_id=material.id))

        purchases = (PurchaseV2.query
                     .filter(PurchaseV2.material_id == material.id, PurchaseV2.is_void == False)
                     .order_by(PurchaseV2.created_at.desc(), PurchaseV2.id.desc()).all())
        delivered_map = {}
        if purchases:
            delivered_map = dict(db.session.query(
                Delivery.purchase_id,
                func.coalesce(func.sum(Delivery.quantity), 0.0)
            ).filter(Delivery.is_void == False,
                     Delivery.purchase_id.in_([int(p.id) for p in purchases])
                     ).group_by(Delivery.purchase_id).all())
        if date_from:
            purchases = [p for p in purchases if p.date and p.date >= date_from]
        if date_to:
            purchases = [p for p in purchases if p.date and p.date <= date_to]
        purchase_total_qty = float(sum(float(p.quantity or 0.0) for p in purchases))
        purchase_total_value = float(sum(float(p.total_amount or 0.0) for p in purchases))

        dq = Delivery.query.filter(Delivery.material_id == material.id, Delivery.is_void == False)
        uq = UsageLogV2.query.filter(UsageLogV2.material_id == material.id, UsageLogV2.is_void == False)
        if project:
            dq = dq.filter(Delivery.project_id == project.id)
            uq = uq.filter(UsageLogV2.project_id == project.id)
        if stage:
            dq = dq.filter(Delivery.stage_id == stage.id)
            uq = uq.filter(UsageLogV2.stage_id == stage.id)
        if date_from:
            dq = dq.filter(Delivery.date >= date_from)
            uq = uq.filter(UsageLogV2.date >= date_from)
        if date_to:
            dq = dq.filter(Delivery.date <= date_to)
            uq = uq.filter(UsageLogV2.date <= date_to)
        deliveries = dq.order_by(Delivery.created_at.desc(), Delivery.id.desc()).all()
        usages = uq.order_by(UsageLogV2.created_at.desc(), UsageLogV2.id.desc()).all()
        delivered_qty = float(sum(float(r.quantity or 0.0) for r in deliveries))
        used_qty = float(sum(float(r.quantity or 0.0) for r in usages))
        used_cost = float(sum(float(r.cost or 0.0) for r in usages))
        lifetime_delivered = _material_v2_delivered(material.id)
        lifetime_used = _material_v2_used(material.id)
        # "In store" is a balance, so it always reads the full history (no
        # site/date filter) — the same number the item-wise register shows.
        # "Balance at site" *is* scoped, because a balance at a site is only
        # meaningful for the site (and period) being looked at.
        lifetime_purchased = float(db.session.query(
            func.coalesce(func.sum(PurchaseV2.quantity), 0.0)
        ).filter(PurchaseV2.material_id == material.id,
                 PurchaseV2.is_void == False).scalar() or 0.0)

        category_map = _material_category_map()
        return render_template('purchase/purchase_v2_item_detail.html',
            material=material,
            category_name=_material_category_label(material_category_id(material), category_map=category_map),
            category_id=(int(material.category_id) if material.category_id else UNCATEGORIZED_ID),
            purchases=purchases,
            delivered_map=delivered_map,
            deliveries=deliveries,
            usages=usages,
            purchase_total_qty=purchase_total_qty,
            purchase_total_value=purchase_total_value,
            delivered_qty=delivered_qty,
            used_qty=used_qty,
            used_cost=used_cost,
            in_store_qty=max(0.0, lifetime_purchased - lifetime_delivered),
            at_site_qty=max(0.0, delivered_qty - used_qty),
            lifetime_delivered=lifetime_delivered,
            lifetime_used=lifetime_used,
            weighted_cost=_material_v2_weighted_cost(material.id),
            projects=Project.query.order_by(Project.name.asc()).all(),
            stages=Stage.query.order_by(Stage.name.asc()).all(),
            selected_project_id=project_id,
            selected_stage_id=stage_id,
            filter_date_from=(date_from.isoformat() if date_from else ''),
            filter_date_to=(date_to.isoformat() if date_to else ''),
            **_category_filter_context(None)
        )


    # ── Supplier ⇄ site / item-category tracking ────────────────────────────

    @app.route('/hdc/purchase-v2/suppliers-by-site')
    @login_required
    def hdc_purchase_v2_suppliers_by_site():
        """Which supplier supplied which site — and the other way round.

        Both directions of the same tracking question, on one page: each site
        with the suppliers that delivered to it (and the ones that are only
        assigned so far), and each supplier with the sites it supplied.
        """
        project_id = request.args.get('project_id', type=int)
        supplier_id = request.args.get('supplier_id', type=int)
        material_id = request.args.get('material_id', type=int)
        category_id = _category_filter_arg()
        q = (request.args.get('q') or '').strip()
        date_from = _parse_date((request.args.get('date_from') or '').strip(), fallback=None)
        date_to = _parse_date((request.args.get('date_to') or '').strip(), fallback=None)

        rows = _site_supplier_supplied_rows(
            project_id=project_id, category_id=category_id, material_id=material_id,
            supplier_id=supplier_id, date_from=date_from, date_to=date_to)
        needle = q.lower() if q else ''

        def _matches(text):
            return (not needle) or (needle in (text or '').lower())

        if needle:
            rows = [r for r in rows
                    if _matches(r['supplier_name']) or _matches(r['project_name'])]

        category_map = _material_category_map()
        projects = {int(p.id): p.name for p in Project.query.order_by(Project.name.asc()).all()}
        suppliers = {int(s.id): s for s in Supplier.query.filter(Supplier.is_void == False).all()}

        sites = {}
        for row in rows:
            site = sites.setdefault(row['project_id'], {
                'project_id': row['project_id'],
                'project_name': row['project_name'],
                'rows': [],
                'supplier_ids': set(),
                'quantity': 0.0,
                'value': 0.0,
                'assigned': [],
            })
            site['rows'].append(row)
            site['supplier_ids'].add(row['supplier_id'])
            site['quantity'] += row['quantity']
            site['value'] += row['value']
        # Stated assignments join the same view, marked as assigned-not-yet-supplied.
        link_query = SupplierSite.query
        if project_id:
            link_query = link_query.filter(SupplierSite.project_id == int(project_id))
        if supplier_id:
            link_query = link_query.filter(SupplierSite.supplier_id == int(supplier_id))
        for link in link_query.all():
            sid = int(link.supplier_id or 0)
            pid = int(link.project_id or 0)
            if not sid or not pid:
                continue
            supplier = suppliers.get(sid)
            if not supplier:
                continue
            site_name = projects.get(pid, f'Site #{pid}')
            # The search box has to narrow the assignments too, otherwise a
            # search that matches nothing still lists every assigned supplier.
            if not (_matches(supplier.name) or _matches(site_name)):
                continue
            site = sites.setdefault(pid, {
                'project_id': pid,
                'project_name': site_name,
                'rows': [], 'supplier_ids': set(), 'quantity': 0.0, 'value': 0.0, 'assigned': [],
            })
            if sid not in site['supplier_ids']:
                site['assigned'].append({
                    'supplier_id': sid,
                    'supplier_name': supplier.name,
                    'note': link.note or '',
                })
        site_rows = []
        for site in sites.values():
            site['rows'].sort(key=lambda r: (-r['value'], r['supplier_name'].lower()))
            site['assigned'].sort(key=lambda r: r['supplier_name'].lower())
            site['supplier_count'] = len(site['supplier_ids'])
            del site['supplier_ids']
            site_rows.append(site)
        site_rows.sort(key=lambda s: s['project_name'].lower())

        supplier_rows = {}
        for row in rows:
            entry = supplier_rows.setdefault(row['supplier_id'], {
                'supplier_id': row['supplier_id'],
                'supplier_name': row['supplier_name'],
                'site_ids': set(),
                'supplied_categories': set(),
                'quantity': 0.0,
                'value': 0.0,
            })
            entry['site_ids'].add(row['project_id'])
            entry['quantity'] += row['quantity']
            entry['value'] += row['value']
            for category_name in row['category_names']:
                entry['supplied_categories'].add(category_name)
        stated_sites = _supplier_site_ids_map()
        stated_categories = _supplier_category_ids_map()
        for sid, entry in supplier_rows.items():
            entry['site_names'] = sorted(projects.get(pid, f'Site #{pid}') for pid in entry['site_ids'])
            entry['site_count'] = len(entry['site_ids'])
            entry['assigned_site_ids'] = set(stated_sites.get(sid, []))
            entry['assigned_site_names'] = sorted(projects.get(pid, f'Site #{pid}')
                                                  for pid in entry['assigned_site_ids'])
            entry['supplied_category_names'] = sorted(entry['supplied_categories'])
            entry['deals_in'] = [_material_category_label(cid, category_map=category_map)
                                 for cid in stated_categories.get(sid, [])]
            del entry['supplied_categories']
        supplier_list = sorted(supplier_rows.values(), key=lambda r: (-r['value'], r['supplier_name'].lower()))
        # Assigned-only suppliers (no delivery yet) still belong on this page.
        for sid, supplier in suppliers.items():
            if sid in supplier_rows or sid not in stated_sites:
                continue
            if project_id and int(project_id) not in set(stated_sites.get(sid, [])):
                continue
            assigned_names = [projects.get(pid, f'Site #{pid}')
                              for pid in stated_sites.get(sid, [])]
            if needle and not (_matches(supplier.name)
                               or any(_matches(name) for name in assigned_names)):
                continue
            supplier_list.append({
                'supplier_id': sid,
                'supplier_name': supplier.name,
                'quantity': 0.0,
                'value': 0.0,
                'supplied_category_names': [],
                'site_names': [],
                'site_count': 0,
                'assigned_site_ids': set(stated_sites.get(sid, [])),
                'assigned_site_names': sorted(assigned_names),
                'deals_in': [_material_category_label(cid, category_map=category_map)
                             for cid in stated_categories.get(sid, [])],
            })
        totals = {
            'sites': len(site_rows),
            'suppliers': len({r['supplier_id'] for r in rows}),
            'quantity': float(sum(r['quantity'] for r in rows)),
            'value': float(sum(r['value'] for r in rows)),
        }
        return render_template('purchase/purchase_v2_suppliers_by_site.html',
            site_rows=site_rows,
            supplier_rows=supplier_list,
            totals=totals,
            projects=Project.query.order_by(Project.name.asc()).all(),
            materials=(MaterialV2.query.filter(MaterialV2.is_void == False)
                       .order_by(MaterialV2.name.asc()).all()),
            supplier_options=Supplier.query.filter(Supplier.is_void == False)
                              .order_by(Supplier.name.asc()).all(),
            selected_project_id=project_id,
            selected_supplier_id=supplier_id,
            selected_material_id=material_id,
            q=q,
            filter_date_from=(date_from.isoformat() if date_from else ''),
            filter_date_to=(date_to.isoformat() if date_to else ''),
            **_category_filter_context(category_id)
        )


    @app.route('/hdc/purchase-v2/suppliers/<int:supplier_id>/categories/add', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_purchase_v2_supplier_category_add(supplier_id):
        supplier = Supplier.query.get_or_404(supplier_id)
        category = _material_category_by_id(request.form.get('category_id'), active_only=True)
        if not category:
            flash('Select an active category.', 'danger')
            return redirect(url_for('hdc_purchase_v2_supplier_detail', supplier_id=supplier.id))
        exists = (SupplierCategory.query
                  .filter(SupplierCategory.supplier_id == supplier.id,
                          SupplierCategory.category_id == category.id).first())
        if exists:
            flash(f'{supplier.name} already deals in {category.name}.', 'warning')
            return redirect(url_for('hdc_purchase_v2_supplier_detail', supplier_id=supplier.id))
        db.session.add(SupplierCategory(supplier_id=supplier.id, category_id=category.id,
                                        created_at=_pkt_now_naive()))
        log_action(current_user, 'update',
                   f'{current_user.username.title()} set supplier #{supplier.id} to deal in {category.name}',
                   'supplier_category', supplier.id)
        db.session.commit()
        flash(f'{supplier.name} now deals in {category.name}.', 'success')
        return redirect(url_for('hdc_purchase_v2_supplier_detail', supplier_id=supplier.id))


    @app.route('/hdc/purchase-v2/suppliers/<int:supplier_id>/categories/<int:category_id>/remove', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_purchase_v2_supplier_category_remove(supplier_id, category_id):
        supplier = Supplier.query.get_or_404(supplier_id)
        link = (SupplierCategory.query
                .filter(SupplierCategory.supplier_id == supplier.id,
                        SupplierCategory.category_id == int(category_id)).first())
        if not link:
            flash('That category is not linked to this supplier.', 'warning')
            return redirect(url_for('hdc_purchase_v2_supplier_detail', supplier_id=supplier.id))
        db.session.delete(link)
        log_action(current_user, 'update',
                   f'{current_user.username.title()} removed category link #{category_id} from supplier #{supplier.id}',
                   'supplier_category', supplier.id)
        db.session.commit()
        flash('Category link removed. Purchases and deliveries stay untouched.', 'success')
        return redirect(url_for('hdc_purchase_v2_supplier_detail', supplier_id=supplier.id))


    @app.route('/hdc/purchase-v2/suppliers/<int:supplier_id>/sites/add', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_purchase_v2_supplier_site_add(supplier_id):
        supplier = Supplier.query.get_or_404(supplier_id)
        project_id = request.form.get('project_id', type=int)
        project = Project.query.get(project_id) if project_id else None
        if not project:
            flash('Select a valid site / project.', 'danger')
            return redirect(url_for('hdc_purchase_v2_supplier_detail', supplier_id=supplier.id))
        exists = (SupplierSite.query
                  .filter(SupplierSite.supplier_id == supplier.id,
                          SupplierSite.project_id == project.id).first())
        if exists:
            flash(f'{supplier.name} is already assigned to {project.name}.', 'warning')
            return redirect(url_for('hdc_purchase_v2_supplier_detail', supplier_id=supplier.id))
        db.session.add(SupplierSite(supplier_id=supplier.id, project_id=project.id,
                                    note=(request.form.get('note') or '').strip() or None,
                                    created_at=_pkt_now_naive()))
        log_action(current_user, 'update',
                   f'{current_user.username.title()} assigned supplier #{supplier.id} to site {project.name}',
                   'supplier_site', supplier.id)
        db.session.commit()
        flash(f'{supplier.name} assigned to {project.name}.', 'success')
        return redirect(url_for('hdc_purchase_v2_supplier_detail', supplier_id=supplier.id))


    @app.route('/hdc/purchase-v2/suppliers/<int:supplier_id>/sites/<int:project_id>/remove', methods=['POST'])
    @login_required
    @_money_write_required()
    def hdc_purchase_v2_supplier_site_remove(supplier_id, project_id):
        supplier = Supplier.query.get_or_404(supplier_id)
        link = (SupplierSite.query
                .filter(SupplierSite.supplier_id == supplier.id,
                        SupplierSite.project_id == int(project_id)).first())
        if not link:
            flash('That site is not assigned to this supplier.', 'warning')
            return redirect(url_for('hdc_purchase_v2_supplier_detail', supplier_id=supplier.id))
        db.session.delete(link)
        log_action(current_user, 'update',
                   f'{current_user.username.title()} removed site link #{project_id} from supplier #{supplier.id}',
                   'supplier_site', supplier.id)
        db.session.commit()
        flash('Site assignment removed. Deliveries stay untouched.', 'success')
        return redirect(url_for('hdc_purchase_v2_supplier_detail', supplier_id=supplier.id))
