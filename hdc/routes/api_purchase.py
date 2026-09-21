"""HDC routes: JSON API: /api/v2/purchase/*.

Moved verbatim from hdc_erp.py; each handler keeps its
original @app.route decorator and endpoint name.
"""

from flask import jsonify, request
from flask_login import current_user, login_required
from sqlalchemy import func

from hdc.extensions import _money_write_required, db
from hdc.models.materials import Delivery, MaterialV2, PurchaseV2, Supplier, SupplierLedger, UsageLogV2
from hdc.models.projects import Project, Stage
from hdc.services.accounts import _accounts_post_supplier_credit_row, _accounts_set_void_by_source, _accounts_upsert_purchase_paid_txn
from hdc.services.audit import log_action
from hdc.services.purchase import _MATERIAL_V2_UNITS, _ensure_material_v2, _ensure_supplier_quick, _material_v2_available, _material_v2_delivered, _material_v2_scope_stock_rows, _material_v2_used, _purchase_v2_available_in_scope_qty, _purchase_v2_delivered_qty, _purchase_v2_delivered_to_scope_qty, _purchase_v2_scope_remaining_map, _purchase_v2_used_in_scope_qty, _supplier_balance, _sync_purchase_v2_ledger
from hdc.utils.dates import _pkt_now_naive
from hdc.utils.format import _flt, _payload_int
from hdc.utils.normalize import _normalize_name_ci

def register(app):
    """Register JSON API: /api/v2/purchase/*."""
    @app.route('/api/v2/purchase/suppliers', methods=['GET', 'POST'])
    @login_required
    @_money_write_required(api=True)
    def api_v2_suppliers():
        if request.method == 'POST':
            payload = request.get_json(silent=True) or request.form
            name = _normalize_name_ci(payload.get('name'))
            phone = (payload.get('phone') or '').strip()
            if not name:
                return jsonify(ok=False, message='Supplier name is required.'), 400
            row = _ensure_supplier_quick(name, phone)
            if not row:
                return jsonify(ok=False, message='Invalid supplier data.'), 400
            row.updated_at = _pkt_now_naive()
            log_action(current_user, 'create', f'{current_user.username.title()} added supplier {row.name}', 'supplier', row.id)
            db.session.commit()
            return jsonify(ok=True, id=row.id, name=row.name)
        rows = Supplier.query.filter_by(is_void=False).order_by(Supplier.name.asc()).all()
        credit_rows = (db.session.query(
                SupplierLedger.supplier_id,
                func.lower(func.coalesce(SupplierLedger.reference_type, 'payment')),
                func.coalesce(func.sum(SupplierLedger.amount), 0.0)
            )
            .filter(
                SupplierLedger.is_void == False,
                SupplierLedger.entry_type == 'credit',
                func.lower(func.coalesce(SupplierLedger.reference_type, 'payment')).in_(['payment', 'tip', 'settlement'])
            )
            .group_by(SupplierLedger.supplier_id, func.lower(func.coalesce(SupplierLedger.reference_type, 'payment')))
            .all())
        credit_map = {}
        for sid, rtype, amount in credit_rows:
            credit_map[(int(sid or 0), (rtype or 'payment'))] = float(amount or 0.0)
        return jsonify(ok=True, items=[{
            'id': r.id,
            'name': r.name,
            'phone': r.phone or '',
            'status': r.status or 'active',
            'balance': _supplier_balance(r.id),
            'payment_total': float(credit_map.get((r.id, 'payment'), 0.0) or 0.0),
            'tip_total': float(credit_map.get((r.id, 'tip'), 0.0) or 0.0),
            'settlement_total': float(credit_map.get((r.id, 'settlement'), 0.0) or 0.0)
        } for r in rows])


    @app.route('/api/v2/purchase/suppliers/<int:supplier_id>', methods=['PUT', 'DELETE'])
    @login_required
    @_money_write_required(api=True)
    def api_v2_supplier_item(supplier_id):
        row = Supplier.query.get_or_404(supplier_id)
        if request.method == 'PUT':
            payload = request.get_json(silent=True) or request.form
            name = _normalize_name_ci(payload.get('name') if payload else row.name) or row.name
            phone = (payload.get('phone') or '').strip() if payload else (row.phone or '')
            status = ((payload.get('status') if payload else row.status) or 'active').strip().lower()
            if status not in ('active', 'inactive'):
                status = 'active'
            exists = (Supplier.query
                      .filter(
                          Supplier.id != row.id,
                          Supplier.is_void == False,
                          func.lower(Supplier.name) == name.lower()
                      )
                      .first())
            if exists:
                return jsonify(ok=False, message='Another supplier already uses this name.'), 400
            row.name = name
            row.phone = phone
            row.status = status
            row.updated_at = _pkt_now_naive()
            log_action(current_user, 'update', f'{current_user.username.title()} updated supplier #{row.id}: {row.name}', 'supplier', row.id)
            db.session.commit()
            return jsonify(ok=True, id=row.id)
        has_purchase = PurchaseV2.query.filter_by(supplier_id=row.id, is_void=False).first() is not None
        has_ledger = SupplierLedger.query.filter_by(supplier_id=row.id, is_void=False).first() is not None
        if has_purchase or has_ledger:
            return jsonify(ok=False, message='Supplier has transactions; set status inactive instead of delete.'), 400
        row.is_void = True
        row.status = 'inactive'
        row.updated_at = _pkt_now_naive()
        log_action(current_user, 'delete', f'{current_user.username.title()} deleted supplier #{row.id}: {row.name}', 'supplier', row.id)
        db.session.commit()
        return jsonify(ok=True, id=row.id)


    @app.route('/api/v2/purchase/materials', methods=['GET', 'POST'])
    @login_required
    @_money_write_required(api=True)
    def api_v2_materials():
        if request.method == 'POST':
            payload = request.get_json(silent=True) or request.form
            name = _normalize_name_ci(payload.get('name'))
            unit = (payload.get('unit') or 'KG').strip().upper()
            if unit not in _MATERIAL_V2_UNITS:
                return jsonify(ok=False, message=f'Unit must be one of: {", ".join(_MATERIAL_V2_UNITS)}.'), 400
            row = _ensure_material_v2(name, unit)
            if not row:
                return jsonify(ok=False, message='Material name is required.'), 400
            row.unit = unit
            row.updated_at = _pkt_now_naive()
            log_action(current_user, 'create', f'{current_user.username.title()} added material {row.name} ({unit})', 'material_v2', row.id)
            db.session.commit()
            return jsonify(ok=True, id=row.id, name=row.name, unit=row.unit)
        rows = MaterialV2.query.filter_by(is_void=False).order_by(MaterialV2.name.asc()).all()
        items = []
        for r in rows:
            delivered = _material_v2_delivered(r.id)
            used = _material_v2_used(r.id)
            items.append({
                'id': r.id,
                'name': r.name,
                'unit': r.unit,
                'status': r.status or 'active',
                'delivered_qty': delivered,
                'used_qty': used,
                'available_qty': max(0.0, delivered - used)
            })
        return jsonify(ok=True, units=list(_MATERIAL_V2_UNITS), items=items)


    @app.route('/api/v2/purchase/materials/<int:material_id>', methods=['PUT', 'DELETE'])
    @login_required
    @_money_write_required(api=True)
    def api_v2_material_item(material_id):
        row = MaterialV2.query.get_or_404(material_id)
        if request.method == 'PUT':
            payload = request.get_json(silent=True) or request.form
            name = _normalize_name_ci(payload.get('name') if payload else row.name) or row.name
            unit = ((payload.get('unit') if payload else row.unit) or 'KG').strip().upper()
            status = ((payload.get('status') if payload else row.status) or 'active').strip().lower()
            if status not in ('active', 'inactive'):
                status = 'active'
            if unit not in _MATERIAL_V2_UNITS:
                return jsonify(ok=False, message=f'Unit must be one of: {", ".join(_MATERIAL_V2_UNITS)}.'), 400
            exists = (MaterialV2.query
                      .filter(
                          MaterialV2.id != row.id,
                          MaterialV2.is_void == False,
                          func.lower(MaterialV2.name) == name.lower()
                      )
                      .first())
            if exists:
                return jsonify(ok=False, message='Another material already uses this name.'), 400
            row.name = name
            row.unit = unit
            row.status = status
            row.updated_at = _pkt_now_naive()
            log_action(current_user, 'update', f'{current_user.username.title()} updated material #{row.id}: {row.name} ({row.unit})', 'material_v2', row.id)
            db.session.commit()
            return jsonify(ok=True, id=row.id)
        has_purchase = PurchaseV2.query.filter_by(material_id=row.id, is_void=False).first() is not None
        has_delivery = Delivery.query.filter_by(material_id=row.id, is_void=False).first() is not None
        has_usage = UsageLogV2.query.filter_by(material_id=row.id, is_void=False).first() is not None
        if has_purchase or has_delivery or has_usage:
            return jsonify(ok=False, message='Material has transactions; set status inactive instead of delete.'), 400
        row.is_void = True
        row.status = 'inactive'
        row.updated_at = _pkt_now_naive()
        log_action(current_user, 'delete', f'{current_user.username.title()} deleted material #{row.id}: {row.name}', 'material_v2', row.id)
        db.session.commit()
        return jsonify(ok=True, id=row.id)


    @app.route('/api/v2/purchase/purchases', methods=['GET', 'POST'])
    @login_required
    @_money_write_required(api=True)
    def api_v2_purchases():
        if request.method == 'POST':
            payload = request.get_json(silent=True) or request.form
            supplier_id = _payload_int(payload, 'supplier_id')
            material_id = _payload_int(payload, 'material_id')
            quick_supplier = _normalize_name_ci(payload.get('supplier_name'))
            unit_price = max(0.0, _flt(payload.get('unit_price'), 0.0))
            quantity = max(0.0, _flt(payload.get('quantity'), 0.0))
            payment_status = (payload.get('payment_status') or 'unpaid').strip().lower()
            if payment_status not in ('paid', 'unpaid'):
                payment_status = 'unpaid'
            supplier = Supplier.query.get(supplier_id) if supplier_id else None
            if not supplier and quick_supplier:
                supplier = _ensure_supplier_quick(quick_supplier, payload.get('supplier_phone'))
            if (not supplier) or supplier.is_void or (supplier.status or 'active').strip().lower() != 'active':
                return jsonify(ok=False, message='Valid supplier is required.'), 400
            material = MaterialV2.query.get(material_id) if material_id else None
            if (not material) or material.is_void or (material.status or 'active').strip().lower() != 'active':
                return jsonify(ok=False, message='Valid material is required.'), 400
            if unit_price <= 0 or quantity <= 0:
                return jsonify(ok=False, message='Unit price and quantity must be greater than 0.'), 400
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
            if payment_status == 'paid':
                ok_txn, msg_txn, _ = _accounts_upsert_purchase_paid_txn(row, supplier_name=supplier.name, commit=False)
                if not ok_txn and 'Duplicate source transaction' not in (msg_txn or ''):
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
            return jsonify(ok=True, id=row.id, total_amount=total_amount)
        rows = (PurchaseV2.query
                .filter_by(is_void=False)
                .order_by(PurchaseV2.created_at.asc(), PurchaseV2.id.asc())
                .limit(400)
                .all())
        return jsonify(ok=True, items=[{
            'id': r.id,
            'supplier_id': r.supplier_id,
            'material_id': r.material_id,
            'supplier': r.supplier.name if r.supplier else '-',
            'material': r.material.name if r.material else '-',
            'unit': (r.material.unit if r.material else ''),
            'unit_price': float(r.unit_price or 0.0),
            'quantity': float(r.quantity or 0.0),
            'total_amount': float(r.total_amount or 0.0),
            'payment_status': r.payment_status,
            'delivered_qty': _purchase_v2_delivered_qty(r.id),
            'created_at': (r.created_at.isoformat(sep=' ') if r.created_at else '')
        } for r in rows])


    @app.route('/api/v2/purchase/purchases/<int:purchase_id>', methods=['PUT', 'DELETE'])
    @login_required
    @_money_write_required(api=True)
    def api_v2_purchase_item(purchase_id):
        row = PurchaseV2.query.get_or_404(purchase_id)
        if row.is_void:
            return jsonify(ok=False, message='Purchase is already deleted.'), 400
        if request.method == 'PUT':
            payload = request.get_json(silent=True) or request.form
            supplier_id = _payload_int(payload, 'supplier_id')
            material_id = _payload_int(payload, 'material_id')
            unit_price = max(0.0, _flt(payload.get('unit_price'), row.unit_price))
            quantity = max(0.0, _flt(payload.get('quantity'), row.quantity))
            payment_status = (payload.get('payment_status') or row.payment_status or 'unpaid').strip().lower()
            if payment_status not in ('paid', 'unpaid'):
                payment_status = 'unpaid'
            supplier = Supplier.query.get(supplier_id) if supplier_id else None
            material = MaterialV2.query.get(material_id) if material_id else None
            if (not supplier) or supplier.is_void or (supplier.status or 'active').strip().lower() != 'active':
                return jsonify(ok=False, message='Valid supplier is required.'), 400
            if (not material) or material.is_void or (material.status or 'active').strip().lower() != 'active':
                return jsonify(ok=False, message='Valid material is required.'), 400
            if unit_price <= 0 or quantity <= 0:
                return jsonify(ok=False, message='Unit price and quantity must be greater than 0.'), 400
            delivered = _purchase_v2_delivered_qty(row.id)
            if quantity + 1e-9 < delivered:
                return jsonify(ok=False, message=f'Cannot set quantity below delivered quantity ({delivered:.2f}).'), 400
            if row.material_id != material.id and delivered > 0:
                return jsonify(ok=False, message='Cannot change material after deliveries are recorded for this purchase.'), 400
            old_payment_status = (row.payment_status or 'unpaid').strip().lower()
            row.supplier_id = supplier.id
            row.material_id = material.id
            row.unit_price = unit_price
            row.quantity = quantity
            row.total_amount = float(unit_price * quantity)
            row.payment_status = payment_status
            row.updated_at = _pkt_now_naive()
            _sync_purchase_v2_ledger(row)
            if payment_status == 'paid':
                ok_txn, msg_txn, _ = _accounts_upsert_purchase_paid_txn(row, supplier_name=supplier.name, commit=False)
                if not ok_txn and 'Duplicate source transaction' not in (msg_txn or ''):
                    db.session.rollback()
                    return jsonify(ok=False, message=(msg_txn or 'Unable to post paid purchase in unified accounts.')), 400
            elif old_payment_status == 'paid':
                _accounts_set_void_by_source('purchase_v2_paid', row.id, True)
            log_action(
                current_user,
                'update',
                f'{current_user.username.title()} updated purchase #{row.id}: {supplier.name}, {material.name}, {quantity:.2f} x {unit_price:.2f} ({payment_status})',
                'purchase_v2',
                row.id
            )
            db.session.commit()
            return jsonify(ok=True, id=row.id, total_amount=float(row.total_amount or 0.0))
        delivered = _purchase_v2_delivered_qty(row.id)
        if delivered > 0:
            return jsonify(ok=False, message=f'Cannot delete purchase #{row.id}; delivery exists ({delivered:.2f}).'), 400
        row.is_void = True
        row.void_reason = 'Deleted by user from Purchase V2'
        row.voided_at = _pkt_now_naive()
        row.updated_at = _pkt_now_naive()
        _sync_purchase_v2_ledger(row)
        _accounts_set_void_by_source('purchase_v2_paid', row.id, True)
        log_action(current_user, 'delete', f'{current_user.username.title()} deleted purchase #{row.id}', 'purchase_v2', row.id)
        db.session.commit()
        return jsonify(ok=True, id=row.id)


    @app.route('/api/v2/purchase/payments', methods=['POST'])
    @login_required
    @_money_write_required(api=True)
    def api_v2_payments():
        payload = request.get_json(silent=True) or request.form
        supplier_id = _payload_int(payload, 'supplier_id')
        amount = max(0.0, _flt(payload.get('amount'), 0.0))
        note = (payload.get('note') or '').strip()
        entry_kind = ((payload.get('entry_kind') if payload else 'payment') or 'payment').strip().lower()
        if entry_kind not in ('payment', 'tip', 'settlement'):
            entry_kind = 'payment'
        supplier = Supplier.query.get(supplier_id) if supplier_id else None
        if (not supplier) or supplier.is_void:
            return jsonify(ok=False, message='Valid supplier is required.'), 400
        if amount <= 0:
            return jsonify(ok=False, message='Amount must be greater than 0.'), 400
        title_map = {'payment': 'payment', 'tip': 'tip', 'settlement': 'settlement'}
        pretty = title_map.get(entry_kind, 'payment')
        ledger_row = SupplierLedger(
            supplier_id=supplier.id,
            entry_type='credit',
            amount=amount,
            reference_type=entry_kind,
            reference_id=None,
            note=note or f'Supplier {pretty}',
            is_void=False,
            created_at=_pkt_now_naive()
        )
        db.session.add(ledger_row)
        db.session.flush()
        ok_txn, msg_txn, _ = _accounts_post_supplier_credit_row(ledger_row, supplier_name=supplier.name, commit=False)
        if not ok_txn:
            db.session.rollback()
            return jsonify(ok=False, message=(msg_txn or 'Unable to post supplier payment in unified accounts.')), 400
        log_action(
            current_user,
            'payment',
            f'{current_user.username.title()} recorded supplier {pretty}: {supplier.name}, {amount:.2f} PKR',
            f'supplier_{entry_kind}',
            supplier.id
        )
        db.session.commit()
        return jsonify(ok=True, supplier_id=supplier.id, balance=_supplier_balance(supplier.id), entry_kind=entry_kind)


    @app.route('/api/v2/purchase/suppliers/<int:supplier_id>/balance', methods=['GET'])
    @login_required
    def api_v2_supplier_balance(supplier_id):
        supplier = Supplier.query.get_or_404(supplier_id)
        return jsonify(ok=True, supplier_id=supplier.id, supplier=supplier.name, balance=_supplier_balance(supplier.id))


    @app.route('/api/v2/purchase/material-available', methods=['GET'])
    @login_required
    def api_v2_material_available():
        material_id = request.args.get('material_id', type=int)
        project_id = request.args.get('project_id', type=int)
        stage_id = request.args.get('stage_id', type=int)
        if not material_id:
            return jsonify(ok=False, message='material_id is required.'), 400
        material = MaterialV2.query.get(material_id)
        if (not material) or material.is_void:
            return jsonify(ok=False, message='Valid material is required.'), 400
        if stage_id and (not project_id):
            return jsonify(ok=False, message='project_id is required when stage_id is provided.'), 400
        project = Project.query.get(project_id) if project_id else None
        stage = Stage.query.get(stage_id) if stage_id else None
        if project_id and not project:
            return jsonify(ok=False, message='Valid project is required.'), 400
        if stage_id and ((not stage) or int(stage.project_id or 0) != int(project.id)):
            return jsonify(ok=False, message='Selected stage does not belong to selected project.'), 400
        available = _material_v2_available(material.id, project.id if project else None, stage.id if stage else None)
        return jsonify(ok=True, material_id=material.id, available_qty=available)


    @app.route('/api/v2/purchase/usage-po-options', methods=['GET'])
    @login_required
    def api_v2_usage_po_options():
        material_id = request.args.get('material_id', type=int)
        project_id = request.args.get('project_id', type=int)
        stage_id = request.args.get('stage_id', type=int)
        if not material_id:
            return jsonify(ok=False, message='material_id is required.'), 400
        if not project_id:
            return jsonify(ok=False, message='project_id is required.'), 400
        if not stage_id:
            return jsonify(ok=False, message='stage_id is required.'), 400
        material = MaterialV2.query.get(material_id)
        project = Project.query.get(project_id)
        stage = Stage.query.get(stage_id)
        if (not material) or material.is_void:
            return jsonify(ok=False, message='Valid material is required.'), 400
        if not project:
            return jsonify(ok=False, message='Valid project is required.'), 400
        if (not stage) or int(stage.project_id or 0) != int(project.id):
            return jsonify(ok=False, message='Selected stage does not belong to selected project.'), 400

        rows = (PurchaseV2.query
                .filter(
                    PurchaseV2.material_id == material.id,
                    PurchaseV2.is_void == False
                )
                .order_by(PurchaseV2.created_at.asc(), PurchaseV2.id.asc())
                .all())
        remaining_map = _purchase_v2_scope_remaining_map(material.id, project.id, stage.id)
        items = []
        for p in rows:
            delivered_qty = _purchase_v2_delivered_to_scope_qty(p.id, project.id, stage.id)
            used_qty = _purchase_v2_used_in_scope_qty(p.id, project.id, stage.id)
            remaining_qty = float(remaining_map.get(int(p.id), 0.0) or 0.0)
            if remaining_qty <= 1e-9:
                continue
            items.append({
                'id': int(p.id),
                'supplier': (p.supplier.name if p.supplier else '-'),
                'unit_price': float(p.unit_price or 0.0),
                'remaining_qty': float(remaining_qty),
                'delivered_qty': float(delivered_qty),
                'used_qty': float(used_qty),
                'unit': (p.material.unit if p.material else material.unit or ''),
                'challan_no': (p.challan_no or ''),
                'date': (p.date.isoformat() if p.date else ''),
            })
        return jsonify(ok=True, material_id=material.id, project_id=project.id, stage_id=stage.id, items=items)


    @app.route('/api/v2/purchase/deliveries', methods=['GET', 'POST'])
    @login_required
    @_money_write_required(api=True)
    def api_v2_deliveries():
        if request.method == 'POST':
            payload = request.get_json(silent=True) or request.form
            purchase_id = _payload_int(payload, 'purchase_id')
            project_id = _payload_int(payload, 'project_id')
            stage_id = _payload_int(payload, 'stage_id')
            quantity = max(0.0, _flt(payload.get('quantity'), 0.0))
            delivery_person = (payload.get('delivery_person') or '').strip()
            purchase = PurchaseV2.query.get(purchase_id) if purchase_id else None
            project = Project.query.get(project_id) if project_id else None
            stage = Stage.query.get(stage_id) if stage_id else None
            if (not purchase) or purchase.is_void:
                return jsonify(ok=False, message='Valid purchase is required.'), 400
            if not project:
                return jsonify(ok=False, message='Valid project is required.'), 400
            if stage_id and ((not stage) or int(stage.project_id or 0) != int(project.id)):
                return jsonify(ok=False, message='Selected stage does not belong to selected project.'), 400
            if quantity <= 0:
                return jsonify(ok=False, message='Quantity must be greater than 0.'), 400
            delivered_so_far = float(db.session.query(func.coalesce(func.sum(Delivery.quantity), 0.0))
                                     .filter(Delivery.purchase_id == purchase.id, Delivery.is_void == False).scalar() or 0.0)
            remaining_purchase_qty = max(0.0, float(purchase.quantity or 0.0) - delivered_so_far)
            if quantity > remaining_purchase_qty + 1e-9:
                return jsonify(ok=False, message=f'Cannot deliver more than remaining purchase quantity ({remaining_purchase_qty:.2f}).'), 400
            row = Delivery(
                purchase_id=purchase.id,
                material_id=purchase.material_id,
                project_id=project.id,
                stage_id=stage.id if stage else None,
                quantity=quantity,
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
            return jsonify(ok=True, id=row.id)
        rows = (Delivery.query
                .filter_by(is_void=False)
                .order_by(Delivery.created_at.asc(), Delivery.id.asc())
                .limit(400)
                .all())
        return jsonify(ok=True, items=[{
            'id': r.id,
            'purchase_id': r.purchase_id,
            'material_id': r.material_id,
            'project_id': r.project_id,
            'stage_id': r.stage_id,
            'material': r.material.name if r.material else '-',
            'project': r.project.name if r.project else '-',
            'stage': r.stage.name if r.stage else '-',
            'quantity': float(r.quantity or 0.0),
            'delivery_person': r.delivery_person or '',
            'created_at': (r.created_at.isoformat(sep=' ') if r.created_at else '')
        } for r in rows])


    @app.route('/api/v2/purchase/deliveries/<int:delivery_id>', methods=['DELETE'])
    @login_required
    @_money_write_required(api=True)
    def api_v2_delivery_item(delivery_id):
        row = Delivery.query.get_or_404(delivery_id)
        if row.is_void:
            return jsonify(ok=False, message='Delivery is already deleted.'), 400
        row.is_void = True
        row.void_reason = 'Deleted by user from Purchase V2'
        row.voided_at = _pkt_now_naive()
        log_action(current_user, 'delete', f'{current_user.username.title()} deleted delivery #{row.id}', 'delivery', row.id)
        db.session.commit()
        return jsonify(ok=True, id=row.id)


    @app.route('/api/v2/purchase/usage', methods=['GET', 'POST'])
    @login_required
    @_money_write_required(api=True)
    def api_v2_usage():
        if request.method == 'POST':
            payload = request.get_json(silent=True) or request.form
            purchase_id = _payload_int(payload, 'purchase_id')
            material_id = _payload_int(payload, 'material_id')
            project_id = _payload_int(payload, 'project_id')
            stage_id = _payload_int(payload, 'stage_id')
            quantity = max(0.0, _flt(payload.get('quantity'), 0.0))
            purchase = PurchaseV2.query.get(purchase_id) if purchase_id else None
            material = MaterialV2.query.get(material_id) if material_id else None
            project = Project.query.get(project_id) if project_id else None
            stage = Stage.query.get(stage_id) if stage_id else None
            if (not purchase_id) or (not purchase) or purchase.is_void:
                return jsonify(ok=False, message='Valid purchase order is required.'), 400
            if (not material) or material.is_void or (material.status or 'active').strip().lower() != 'active':
                return jsonify(ok=False, message='Valid material is required.'), 400
            if int(purchase.material_id or 0) != int(material.id):
                return jsonify(ok=False, message='Selected purchase order does not match selected material.'), 400
            if not project:
                return jsonify(ok=False, message='Valid project is required.'), 400
            if not stage_id:
                return jsonify(ok=False, message='stage_id is required for strict stock control.'), 400
            if stage_id and ((not stage) or int(stage.project_id or 0) != int(project.id)):
                return jsonify(ok=False, message='Selected stage does not belong to selected project.'), 400
            if quantity <= 0:
                return jsonify(ok=False, message='Quantity must be greater than 0.'), 400
            available = _purchase_v2_available_in_scope_qty(purchase.id, project.id, stage.id if stage else None)
            if quantity > available + 1e-9:
                return jsonify(ok=False, message=f'Usage exceeds available stock for selected purchase order in this stage ({available:.2f}).'), 400
            unit_price = float(purchase.unit_price or 0.0)
            cost = float(unit_price * quantity)
            row = UsageLogV2(
                purchase_id=purchase.id,
                material_id=material.id,
                project_id=project.id,
                stage_id=stage.id if stage else None,
                quantity=quantity,
                cost=cost,
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
            return jsonify(ok=True, id=row.id, cost=cost)
        rows = (UsageLogV2.query
                .filter_by(is_void=False)
                .order_by(UsageLogV2.created_at.asc(), UsageLogV2.id.asc())
                .limit(400)
                .all())
        return jsonify(ok=True, items=[{
            'id': r.id,
            'material_id': r.material_id,
            'project_id': r.project_id,
            'stage_id': r.stage_id,
            'material': r.material.name if r.material else '-',
            'project': r.project.name if r.project else '-',
            'stage': r.stage.name if r.stage else '-',
            'purchase_id': r.purchase_id,
            'unit_price': float((r.purchase.unit_price if r.purchase else 0.0) or 0.0),
            'challan_no': (r.purchase.challan_no if r.purchase else ''),
            'quantity': float(r.quantity or 0.0),
            'cost': float(r.cost or 0.0),
            'created_at': (r.created_at.isoformat(sep=' ') if r.created_at else '')
        } for r in rows])


    @app.route('/api/v2/purchase/usage/<int:usage_id>', methods=['DELETE'])
    @login_required
    @_money_write_required(api=True)
    def api_v2_usage_item(usage_id):
        row = UsageLogV2.query.get_or_404(usage_id)
        if row.is_void:
            return jsonify(ok=False, message='Usage row is already deleted.'), 400
        row.is_void = True
        row.void_reason = 'Deleted by user from Purchase V2'
        row.voided_at = _pkt_now_naive()
        log_action(current_user, 'delete', f'{current_user.username.title()} deleted usage #{row.id}', 'usage', row.id)
        db.session.commit()
        return jsonify(ok=True, id=row.id)


    @app.route('/api/v2/purchase/supplier-ledger', methods=['GET'])
    @login_required
    def api_v2_supplier_ledger():
        supplier_id = request.args.get('supplier_id', type=int)
        q = SupplierLedger.query.filter(SupplierLedger.is_void == False)
        if supplier_id:
            q = q.filter(SupplierLedger.supplier_id == supplier_id)
        rows = (q.order_by(SupplierLedger.created_at.asc(), SupplierLedger.id.asc())
                .limit(500)
                .all())
        items = []
        for r in rows:
            signed = float(r.amount or 0.0)
            if (r.entry_type or '').strip().lower() == 'credit':
                signed = -signed
            items.append({
                'id': r.id,
                'supplier_id': r.supplier_id,
                'supplier': (r.supplier.name if r.supplier else '-'),
                'entry_type': (r.entry_type or '').strip().lower(),
                'amount': float(r.amount or 0.0),
                'signed_amount': signed,
                'reference_type': r.reference_type or '',
                'reference_id': r.reference_id,
                'note': r.note or '',
                'created_at': (r.created_at.isoformat(sep=' ') if r.created_at else '')
            })
        return jsonify(ok=True, items=items)


    @app.route('/api/v2/purchase/material-stock', methods=['GET'])
    @login_required
    def api_v2_material_stock_list():
        rows = MaterialV2.query.filter_by(is_void=False).order_by(MaterialV2.name.asc()).all()
        items = []
        for m in rows:
            delivered = _material_v2_delivered(m.id)
            used = _material_v2_used(m.id)
            items.append({
                'id': m.id,
                'name': m.name,
                'unit': m.unit,
                'delivered_qty': delivered,
                'used_qty': used,
                'available_qty': max(0.0, delivered - used)
            })
        return jsonify(ok=True, items=items)


    @app.route('/api/v2/purchase/material-stock-scope', methods=['GET'])
    @login_required
    def api_v2_material_stock_scope():
        project_id = request.args.get('project_id', type=int)
        stage_id = request.args.get('stage_id', type=int)
        if not project_id:
            return jsonify(ok=False, message='project_id is required.'), 400
        project = Project.query.get(project_id)
        if not project:
            return jsonify(ok=False, message='Valid project is required.'), 400
        stage = Stage.query.get(stage_id) if stage_id else None
        if stage_id and ((not stage) or int(stage.project_id or 0) != int(project.id)):
            return jsonify(ok=False, message='Selected stage does not belong to selected project.'), 400
        return jsonify(ok=True, items=_material_v2_scope_stock_rows(project.id, stage.id if stage else None))


    @app.route('/api/v2/purchase/kpis', methods=['GET'])
    @login_required
    def api_v2_kpis():
        total_purchase = float(db.session.query(func.coalesce(func.sum(PurchaseV2.total_amount), 0.0))
                               .filter(PurchaseV2.is_void == False).scalar() or 0.0)
        unpaid = float(db.session.query(func.coalesce(func.sum(PurchaseV2.total_amount), 0.0))
                       .filter(PurchaseV2.is_void == False, PurchaseV2.payment_status == 'unpaid').scalar() or 0.0)
        delivered_qty = float(db.session.query(func.coalesce(func.sum(Delivery.quantity), 0.0))
                              .filter(Delivery.is_void == False).scalar() or 0.0)
        used_qty = float(db.session.query(func.coalesce(func.sum(UsageLogV2.quantity), 0.0))
                         .filter(UsageLogV2.is_void == False).scalar() or 0.0)
        used_cost = float(db.session.query(func.coalesce(func.sum(UsageLogV2.cost), 0.0))
                          .filter(UsageLogV2.is_void == False).scalar() or 0.0)
        return jsonify(ok=True, purchase_total=total_purchase, unpaid_total=unpaid, delivered_qty=delivered_qty, used_qty=used_qty, used_cost=used_cost)


    @app.route('/api/v2/purchase/recalculate-stock', methods=['POST'])
    @login_required
    @_money_write_required(api=True)
    def api_v2_recalculate_stock():
        materials = MaterialV2.query.filter_by(is_void=False).all()
        rows = []
        for m in materials:
            delivered = _material_v2_delivered(m.id)
            used = _material_v2_used(m.id)
            rows.append({'material_id': m.id, 'material': m.name, 'delivered': delivered, 'used': used, 'available': max(0.0, delivered - used)})
        log_action(current_user, 'update', f'{current_user.username.title()} recalculated stock ledger for {len(rows)} material(s).', 'stock', len(rows))
        db.session.commit()
        return jsonify(ok=True, items=rows)


    @app.route('/api/v2/purchase', methods=['GET'])
    @login_required
    def api_v2_purchase_root():
        return jsonify(
            ok=True,
            endpoints=[
                '/api/v2/purchase/suppliers',
                '/api/v2/purchase/suppliers/<id>',
                '/api/v2/purchase/supplier-ledger',
                '/api/v2/purchase/materials',
                '/api/v2/purchase/materials/<id>',
                '/api/v2/purchase/material-stock',
                '/api/v2/purchase/material-stock-scope',
                '/api/v2/purchase/purchases',
                '/api/v2/purchase/purchases/<id>',
                '/api/v2/purchase/payments',
                '/api/v2/purchase/suppliers/<id>/balance',
                '/api/v2/purchase/usage-po-options',
                '/api/v2/purchase/deliveries',
                '/api/v2/purchase/deliveries/<id>',
                '/api/v2/purchase/usage',
                '/api/v2/purchase/usage/<id>',
                '/api/v2/purchase/kpis',
                '/api/v2/purchase/recalculate-stock'
            ]
        )
