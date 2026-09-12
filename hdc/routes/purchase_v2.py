"""HDC routes: Purchase V2 pages: materials, orders, suppliers, stock.

Moved verbatim from hdc_erp.py; each handler keeps its
original @app.route decorator and endpoint name.
"""

from datetime import datetime

from flask import flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func, or_

from hdc.extensions import db
from hdc.models.materials import Delivery, MaterialV2, PurchaseV2, Supplier, SupplierLedger, UsageLogV2
from hdc.models.projects import Project, Stage
from hdc.services.accounts import _accounts_post_supplier_credit_row, _accounts_set_void_by_source, _accounts_upsert_purchase_paid_txn
from hdc.services.audit import log_action
from hdc.services.purchase import _MATERIAL_V2_UNITS, _ensure_material_v2, _ensure_supplier_quick, _material_v2_available, _material_v2_delivered, _material_v2_used, _material_v2_weighted_cost, _purchase_v2_available_in_scope_qty, _purchase_v2_delivered_qty, _purchase_v2_integrity_report, _repair_supplier_purchase_v2_ledger, _sync_purchase_v2_ledger, _sync_supplier_po_payment_status, _transfer_v2_material_between_scopes
from hdc.services.timekeeping import _has_recent_duplicate
from hdc.utils.dates import _pkt_now_naive, _pkt_today
from hdc.utils.format import _flt
from hdc.utils.normalize import _normalize_name_ci

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
        return render_template('purchase/purchase_v2.html',
            material_count=material_count,
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
    def hdc_purchase_v2_materials():
        if request.method == 'POST':
            name = _normalize_name_ci(request.form.get('name'))
            unit = (request.form.get('unit') or 'KG').strip().upper()
            if unit not in _MATERIAL_V2_UNITS:
                flash(f'Unit must be one of: {", ".join(_MATERIAL_V2_UNITS)}.', 'danger')
                return redirect(url_for('hdc_purchase_v2_materials'))
            row = _ensure_material_v2(name, unit)
            if not row:
                flash('Material name is required.', 'danger')
                return redirect(url_for('hdc_purchase_v2_materials'))
            row.unit = unit
            row.updated_at = _pkt_now_naive()
            log_action(current_user, 'create', f'{current_user.username.title()} added material {row.name} ({unit})', 'material_v2', row.id)
            db.session.commit()
            flash('Material saved.', 'success')
            return redirect(url_for('hdc_purchase_v2_materials'))
        rows = MaterialV2.query.filter_by(is_void=False).order_by(MaterialV2.name.asc()).all()
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
        return render_template('purchase/purchase_v2_materials.html', materials=materials, material_units=_MATERIAL_V2_UNITS, totals=totals)


    @app.route('/hdc/purchase-v2/materials/<int:material_id>/edit', methods=['POST'])
    @login_required
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
        row.name = name
        row.unit = unit
        row.status = status
        row.updated_at = _pkt_now_naive()
        log_action(current_user, 'update', f'{current_user.username.title()} updated material #{row.id}: {row.name} ({row.unit})', 'material_v2', row.id)
        db.session.commit()
        flash('Material updated.', 'success')
        return redirect(url_for('hdc_purchase_v2_materials'))


    @app.route('/hdc/purchase-v2/materials/<int:material_id>/delete', methods=['POST'])
    @login_required
    def hdc_purchase_v2_material_delete(material_id):
        row = MaterialV2.query.get_or_404(material_id)
        has_purchase = PurchaseV2.query.filter_by(material_id=row.id, is_void=False).first() is not None
        has_delivery = Delivery.query.filter_by(material_id=row.id, is_void=False).first() is not None
        has_usage = UsageLogV2.query.filter_by(material_id=row.id, is_void=False).first() is not None
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
    def hdc_purchase_v2_purchases():
        if request.method == 'POST':
            supplier_id = request.form.get('supplier_id', type=int)
            material_id = request.form.get('material_id', type=int)
            unit_price = max(0.0, _flt(request.form.get('unit_price'), 0.0))
            quantity = max(0.0, _flt(request.form.get('quantity'), 0.0))
            payment_status = (request.form.get('payment_status') or 'unpaid').strip().lower()
            if payment_status not in ('paid', 'unpaid'):
                payment_status = 'unpaid'
            _date_raw = (request.form.get('date') or '').strip()
            try:
                _date = datetime.strptime(_date_raw, '%Y-%m-%d').date() if _date_raw else _pkt_today()
            except ValueError:
                _date = _pkt_today()
            _notes     = (request.form.get('notes') or '').strip() or None
            _challan   = (request.form.get('challan_no') or '').strip() or None
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
            total_amount = float(unit_price * quantity)
            if _has_recent_duplicate(
                PurchaseV2,
                supplier_id=supplier.id,
                material_id=material.id,
                unit_price=unit_price,
                quantity=quantity,
                total_amount=total_amount,
                payment_status=payment_status,
                date=_date,
                notes=_notes,
                challan_no=_challan
            ):
                flash('Duplicate purchase prevented (same values submitted too quickly).', 'warning')
                return redirect(url_for('hdc_purchase_v2_purchases'))
            row = PurchaseV2(
                supplier_id=supplier.id,
                material_id=material.id,
                unit_price=unit_price,
                quantity=quantity,
                total_amount=total_amount,
                payment_status=payment_status,
                date=_date,
                notes=_notes,
                challan_no=_challan,
                is_void=False,
                created_at=_pkt_now_naive(),
                updated_at=_pkt_now_naive()
            )
            db.session.add(row)
            db.session.flush()
            _sync_purchase_v2_ledger(row)
            if payment_status == 'paid':
                ok_txn, msg_txn, _ = _accounts_upsert_purchase_paid_txn(row, supplier_name=supplier.name, commit=False)
                if not ok_txn:
                    db.session.rollback()
                    return jsonify(ok=False, message=(msg_txn or 'Unable to post paid purchase in unified accounts.')), 400
            log_action(
                current_user,
                'create',
                f'{current_user.username.title()} created purchase #{row.id}: {supplier.name}, {material.name}, {quantity:.2f} x {unit_price:.2f} = {total_amount:.2f} ({payment_status})',
                'purchase_v2',
                row.id
            )
            db.session.commit()
            flash(f'Purchase #{row.id} recorded.', 'success')
            return redirect(url_for('hdc_purchase_v2_purchases'))
        supplier_id = request.args.get('supplier_id', type=int)
        q = PurchaseV2.query.filter(PurchaseV2.is_void == False)
        if supplier_id:
            q = q.filter(PurchaseV2.supplier_id == supplier_id)
        rows = q.order_by(PurchaseV2.created_at.asc(), PurchaseV2.id.asc()).all()
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
        total = float(sum(float(r.total_amount or 0.0) for r in rows))
        return render_template('purchase/purchase_v2_purchases.html',
            rows=rows,
            suppliers=suppliers,
            materials=materials,
            delivered_map=delivered_map,
            total=total,
            selected_supplier_id=supplier_id,
            today=_pkt_today().isoformat()
        )


    @app.route('/hdc/purchase-v2/purchases/<int:purchase_id>/edit', methods=['POST'])
    @login_required
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
        delivered = _purchase_v2_delivered_qty(row.id)
        if quantity + 1e-9 < delivered:
            flash(f'Cannot set quantity below delivered quantity ({delivered:,.2f}).', 'danger')
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
        old_payment_status = (row.payment_status or 'unpaid').strip().lower()
        row.supplier_id = supplier.id
        row.material_id = material.id
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
    def hdc_purchase_v2_purchase_delete(purchase_id):
        row = PurchaseV2.query.get_or_404(purchase_id)
        if row.is_void:
            flash('Purchase is already deleted.', 'warning')
            return redirect(url_for('hdc_purchase_v2_purchases'))
        delivered = _purchase_v2_delivered_qty(row.id)
        if delivered > 0:
            flash(f'Cannot delete purchase #{row.id}; delivery exists ({delivered:,.2f}).', 'danger')
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
        query = Supplier.query.filter(Supplier.is_void == False)
        if q:
            ql = f'%{q.lower()}%'
            query = query.filter(or_(func.lower(Supplier.name).like(ql), func.lower(func.coalesce(Supplier.phone, '')).like(ql)))
        rows = query.order_by(Supplier.name.asc()).all()

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
            suppliers.append({
                'id': s.id,
                'name': s.name,
                'phone': s.phone or '',
                'status': s.status or 'active',
                'balance': max(0.0, debit - credit),
                'purchase_total': float(purchase_total_rows.get(s.id, 0.0) or 0.0),
                'payment_total': float(credit_kind_map.get((s.id, 'payment'), 0.0) or 0.0),
                'tip_total': float(credit_kind_map.get((s.id, 'tip'), 0.0) or 0.0),
                'settlement_total': float(credit_kind_map.get((s.id, 'settlement'), 0.0) or 0.0),
            })

        return render_template('purchase/purchase_v2_suppliers.html', suppliers=suppliers, q=q)


    @app.route('/hdc/purchase-v2/suppliers/<int:supplier_id>')
    @login_required
    def hdc_purchase_v2_supplier_detail(supplier_id):
        supplier = Supplier.query.get_or_404(supplier_id)
        _repair_supplier_purchase_v2_ledger(supplier.id)
        if db.session.new or db.session.dirty:
            db.session.commit()
        purchases = (PurchaseV2.query
                     .filter(PurchaseV2.supplier_id == supplier.id, PurchaseV2.is_void == False)
                     .order_by(PurchaseV2.created_at.asc(), PurchaseV2.id.asc())
                     .all())
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
            settlement_total=settlement_total
        )


    @app.route('/hdc/purchase-v2/suppliers/<int:supplier_id>/purchase', methods=['POST'])
    @login_required
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
        row = PurchaseV2(
            supplier_id=supplier.id,
            material_id=material.id,
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
            delivered_so_far = float(db.session.query(func.coalesce(func.sum(Delivery.quantity), 0.0))
                                     .filter(Delivery.purchase_id == purchase.id, Delivery.is_void == False).scalar() or 0.0)
            remaining_purchase_qty = max(0.0, float(purchase.quantity or 0.0) - delivered_so_far)
            if quantity > remaining_purchase_qty + 1e-9:
                flash(f'Cannot deliver more than remaining purchase quantity ({remaining_purchase_qty:,.2f}).', 'danger')
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
        rows = (Delivery.query
                .filter(Delivery.is_void == False)
                .order_by(Delivery.created_at.asc(), Delivery.id.asc())
                .all())
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
        projects = Project.query.order_by(Project.name.asc()).all()
        stages = Stage.query.order_by(Stage.name.asc()).all()
        purchases = (PurchaseV2.query
                     .filter(PurchaseV2.is_void == False)
                     .order_by(PurchaseV2.created_at.asc(), PurchaseV2.id.asc())
                     .all())
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
        purchase_rows = []
        for p in purchases:
            delivered_qty = float(delivered_map.get(p.id, 0.0) or 0.0)
            remaining_qty = max(0.0, float(p.quantity or 0.0) - delivered_qty)
            if remaining_qty <= 1e-9:
                continue
            purchase_rows.append({
                'id': p.id,
                'supplier_name': (p.supplier.name if p.supplier else '-'),
                'material_name': (p.material.name if p.material else '-'),
                'material_id': int(p.material_id or 0),
                'unit': (p.material.unit if p.material else ''),
                'total_qty': float(p.quantity or 0.0),
                'delivered_qty': delivered_qty,
                'remaining_qty': remaining_qty
            })
        all_materials = MaterialV2.query.filter_by(is_void=False).order_by(MaterialV2.name.asc()).all()
        material_ids_with_stock = {int(pr['material_id']) for pr in purchase_rows}
        return render_template('purchase/purchase_v2_delivered.html',
            rows=rows,
            total_qty=total_qty,
            projects=projects,
            stages=stages,
            purchase_rows=purchase_rows,
            all_materials=all_materials,
            material_ids_with_stock=material_ids_with_stock,
            today=_pkt_today().isoformat(),
            prefill_shift_material_id=prefill_shift_material_id,
            prefill_from_project_id=prefill_from_project_id,
            prefill_from_stage_id=prefill_from_stage_id,
            prefill_to_project_id=prefill_to_project_id,
            prefill_to_stage_id=prefill_to_stage_id,
            prefill_edit_row=prefill_edit_row
        )


    @app.route('/hdc/purchase-v2/delivered/transfer', methods=['POST'])
    @login_required
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

        available = _material_v2_available(material.id, from_project.id, from_stage.id if from_stage else None)
        if qty > available + 1e-9:
            flash(f'Shift exceeds source available stock ({available:,.2f}).', 'danger')
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
            available = _purchase_v2_available_in_scope_qty(purchase.id, project.id, stage.id if stage else None)
            if quantity > available + 1e-9:
                flash(f'Usage exceeds available stock for selected purchase order in this stage ({available:,.2f}).', 'danger')
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
        rows = (UsageLogV2.query
                .filter(UsageLogV2.is_void == False)
                .order_by(UsageLogV2.created_at.asc(), UsageLogV2.id.asc())
                .all())
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
            today=_pkt_today().isoformat()
        )


    @app.route('/hdc/purchase-v2/stock')
    @login_required
    def hdc_purchase_v2_stock():
        project_id = request.args.get('project_id', type=int)
        stage_id = request.args.get('stage_id', type=int)
        material_id = request.args.get('material_id', type=int)

        project = Project.query.get(project_id) if project_id else None
        stage = Stage.query.get(stage_id) if stage_id else None
        if stage_id and ((not stage) or (project and int(stage.project_id or 0) != int(project.id))):
            flash('Selected stage does not belong to selected project.', 'danger')
            return redirect(url_for('hdc_purchase_v2_stock', project_id=project_id, material_id=material_id))

        projects = Project.query.order_by(Project.name.asc()).all()
        stages = Stage.query.order_by(Stage.name.asc()).all()
        materials = MaterialV2.query.filter(MaterialV2.is_void == False).order_by(MaterialV2.name.asc()).all()
        if material_id:
            materials = [m for m in materials if int(m.id) == int(material_id)]

        scope_project_id = project.id if project else None
        scope_stage_id = stage.id if stage else None

        summary_rows = []
        for m in materials:
            sent_qty = _material_v2_delivered(m.id, scope_project_id, scope_stage_id)
            used_qty = _material_v2_used(m.id, scope_project_id, scope_stage_id)
            pending_qty = max(0.0, sent_qty - used_qty)
            if sent_qty <= 1e-9 and used_qty <= 1e-9 and material_id:
                summary_rows.append({
                    'material': m,
                    'sent_qty': 0.0,
                    'used_qty': 0.0,
                    'pending_qty': 0.0
                })
                continue
            if sent_qty <= 1e-9 and used_qty <= 1e-9:
                continue
            summary_rows.append({
                'material': m,
                'sent_qty': sent_qty,
                'used_qty': used_qty,
                'pending_qty': pending_qty
            })

        dq = Delivery.query.filter(Delivery.is_void == False)
        if project:
            dq = dq.filter(Delivery.project_id == project.id)
        if stage:
            dq = dq.filter(Delivery.stage_id == stage.id)
        if material_id:
            dq = dq.filter(Delivery.material_id == material_id)
        delivery_rows = dq.order_by(Delivery.created_at.asc(), Delivery.id.asc()).all()

        total_sent = float(sum(float(r.get('sent_qty') or 0.0) for r in summary_rows))
        total_used = float(sum(float(r.get('used_qty') or 0.0) for r in summary_rows))
        total_pending = float(sum(float(r.get('pending_qty') or 0.0) for r in summary_rows))
        pq = PurchaseV2.query.filter(PurchaseV2.is_void == False)
        if material_id:
            pq = pq.filter(PurchaseV2.material_id == material_id)
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
            used_of_sent_pct=used_of_sent_pct
        )


    @app.route('/hdc/purchase-v2/delivered/<int:delivery_id>/void', methods=['POST'])
    @login_required
    def hdc_purchase_v2_delivery_void(delivery_id):
        row = Delivery.query.get_or_404(delivery_id)
        if row.is_void:
            flash('Delivery already voided.', 'warning')
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
            other_delivered = float(db.session.query(func.coalesce(func.sum(Delivery.quantity), 0.0))
                .filter(Delivery.purchase_id == purchase.id, Delivery.is_void == False, Delivery.id != row.id).scalar() or 0.0)
            remaining = max(0.0, float(purchase.quantity or 0.0) - other_delivered)
            if qty > remaining + 1e-9:
                flash(f'Quantity exceeds remaining purchase qty ({remaining:,.2f}).', 'danger')
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
                exclude_usage_id=row.id
            )
            if qty > available_for_edit + 1e-9:
                flash(f'Edited usage exceeds selected purchase order stock in this stage ({available_for_edit:,.2f}).', 'danger')
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
            available_for_edit = max(0.0, float(del_q.scalar() or 0.0) - float(use_q.scalar() or 0.0))
            if qty > available_for_edit + 1e-9:
                flash(f'Edited usage exceeds available delivered stock ({available_for_edit:,.2f}).', 'danger')
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
