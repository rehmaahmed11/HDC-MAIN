"""HDC services.purchase — moved verbatim from hdc_erp.py.

See MODULARIZATION_PLAN.md for the module map.
"""

from sqlalchemy import and_, func

from hdc.extensions import db
from hdc.models.materials import Delivery, Material, MaterialUsage, MaterialV2, Purchase, PurchaseV2, Supplier, SupplierLedger, UsageLogV2
from hdc.models.projects import Project
from hdc.services.record_permissions import integrity_query
from hdc.utils.dates import _pkt_now_naive, _pkt_today
from hdc.utils.normalize import _normalize_name_ci


#: How the Delivery / Material Usage registers may be arranged.  Each entry is
#: the ``order_by`` pair for that mode; an unknown mode falls back to 'newest'.
REGISTER_SORT_MODES = ('newest', 'oldest', 'po_asc', 'po_desc')


def _material_stock_map(project_id=None, stage_id=None):
    """
    Return stock summary per material id:
    { material_id: {'purchased': x, 'used': y, 'remaining': z} }
    """
    pur_q = db.session.query(
        Purchase.material_id,
        func.coalesce(func.sum(Purchase.qty), 0.0)
    )
    use_q = db.session.query(
        MaterialUsage.material_id,
        func.coalesce(func.sum(MaterialUsage.qty), 0.0)
    )
    if stage_id:
        pur_q = pur_q.filter(Purchase.stage_id == stage_id)
        use_q = use_q.filter(MaterialUsage.stage_id == stage_id)
    elif project_id:
        pur_q = pur_q.filter(Purchase.project_id == project_id)
        use_q = use_q.filter(MaterialUsage.project_id == project_id)

    purchases = dict(
        pur_q.group_by(Purchase.material_id).all()
    )
    usages = dict(
        use_q.group_by(MaterialUsage.material_id).all()
    )
    out = {}
    for m in Material.query.all():
        purchased = float(purchases.get(m.id, 0.0) or 0.0)
        used = float(usages.get(m.id, 0.0) or 0.0)
        out[m.id] = {
            'purchased': purchased,
            'used': used,
            'remaining': purchased - used
        }
    return out


def _material_stock_for_scope(material_id, project_id=None, stage_id=None, *, integrity=False):
    if integrity:
        totals = []
        for model in (Purchase, MaterialUsage):
            query = db.session.query(func.coalesce(func.sum(model.qty), 0.0)).filter(model.material_id == material_id)
            if stage_id:
                query = query.filter(model.stage_id == stage_id)
            elif project_id:
                query = query.filter(model.project_id == project_id)
            totals.append(float(integrity_query(query).scalar() or 0.0))
        return totals[0] - totals[1]
    stock_map = _material_stock_map(project_id=project_id, stage_id=stage_id)
    row = stock_map.get(int(material_id or 0), {}) if material_id else {}
    return float(row.get('remaining', 0.0) or 0.0)


_MATERIAL_V2_UNITS = (
    'KG', 'G', 'TON', 'LTS', 'ML',
    'METER', 'CM', 'MM', 'FT', 'INCH',
    'SQ FT', 'SQ M', 'CFT',
    'PCS', 'NOS', 'BAG', 'BOX', 'ROLL', 'SHEET',
    'SET', 'PAIR', 'UNIT'
)


def _sync_supplier_po_payment_status(supplier_id):
    """FIFO auto-update PurchaseV2.payment_status after a supplier payment."""
    pos = (PurchaseV2.query
           .filter(PurchaseV2.supplier_id == supplier_id, PurchaseV2.is_void == False)
           .order_by(PurchaseV2.date, PurchaseV2.id)
           .all())
    from flask_login import current_user
    from hdc.services.record_permissions import exact_access_enabled, require_complete_grant
    if exact_access_enabled(current_user):
        require_complete_grant(current_user, PurchaseV2, db.session.query(PurchaseV2.id).filter(
            PurchaseV2.supplier_id == supplier_id, PurchaseV2.is_void == False))
        require_complete_grant(current_user, SupplierLedger, db.session.query(SupplierLedger.id).filter(
            SupplierLedger.supplier_id == supplier_id, SupplierLedger.is_void == False))
    total_credits = float(db.session.query(func.coalesce(func.sum(SupplierLedger.amount), 0.0))
                          .filter(
                              SupplierLedger.supplier_id == supplier_id,
                              SupplierLedger.entry_type == 'credit',
                              SupplierLedger.is_void == False
                          )
                          .scalar() or 0.0)
    remaining = total_credits
    for po in pos:
        po_total = float(po.total_amount or 0.0)
        if po_total <= 0:
            continue
        if remaining >= po_total - 0.01:
            po.payment_status = 'paid'
            remaining -= po_total
        else:
            po.payment_status = 'unpaid'


def _ensure_supplier_quick(name, phone=''):
    sname = _normalize_name_ci(name)
    if not sname:
        return None
    existing = Supplier.query.filter(func.lower(Supplier.name) == sname.lower(), Supplier.is_void == False).first()
    if existing:
        if phone and not (existing.phone or '').strip():
            existing.phone = phone.strip()
        existing.updated_at = _pkt_now_naive()
        return existing
    row = Supplier(name=sname, phone=(phone or '').strip(), status='active', is_void=False, created_at=_pkt_now_naive(), updated_at=_pkt_now_naive())
    db.session.add(row)
    db.session.flush()
    return row


def _ensure_material_v2(name, unit='KG'):
    mname = _normalize_name_ci(name)
    if not mname:
        return None
    existing = MaterialV2.query.filter(func.lower(MaterialV2.name) == mname.lower(), MaterialV2.is_void == False).first()
    if existing:
        return existing
    row = MaterialV2(name=mname, unit=(unit or 'KG').upper(), status='active', is_void=False, created_at=_pkt_now_naive(), updated_at=_pkt_now_naive())
    db.session.add(row)
    db.session.flush()
    return row


def _supplier_balance(supplier_id):
    debit = float(db.session.query(func.coalesce(func.sum(SupplierLedger.amount), 0.0))
                  .filter(SupplierLedger.supplier_id == supplier_id,
                          SupplierLedger.is_void == False,
                          SupplierLedger.entry_type == 'debit').scalar() or 0.0)
    credit = float(db.session.query(func.coalesce(func.sum(SupplierLedger.amount), 0.0))
                   .filter(SupplierLedger.supplier_id == supplier_id,
                           SupplierLedger.is_void == False,
                           SupplierLedger.entry_type == 'credit').scalar() or 0.0)
    return max(0.0, debit - credit)


def _material_v2_delivered(material_id, project_id=None, stage_id=None, *, integrity=False):
    q = db.session.query(func.coalesce(func.sum(Delivery.quantity), 0.0)).filter(
        Delivery.material_id == material_id, Delivery.is_void == False
    )
    if project_id:
        q = q.filter(Delivery.project_id == project_id)
    if stage_id:
        q = q.filter(Delivery.stage_id == stage_id)
    return float((integrity_query(q) if integrity else q).scalar() or 0.0)


def _material_v2_used(material_id, project_id=None, stage_id=None, *, integrity=False):
    q = db.session.query(func.coalesce(func.sum(UsageLogV2.quantity), 0.0)).filter(
        UsageLogV2.material_id == material_id, UsageLogV2.is_void == False
    )
    if project_id:
        q = q.filter(UsageLogV2.project_id == project_id)
    if stage_id:
        q = q.filter(UsageLogV2.stage_id == stage_id)
    return float((integrity_query(q) if integrity else q).scalar() or 0.0)


def _material_v2_available(material_id, project_id=None, stage_id=None, *, integrity=False):
    return max(0.0, _material_v2_delivered(material_id, project_id, stage_id, integrity=integrity) -
               _material_v2_used(material_id, project_id, stage_id, integrity=integrity))


def _purchase_v2_integrity_report():
    eps = 1e-6
    issues = []

    total_purchased_qty = float(db.session.query(func.coalesce(func.sum(PurchaseV2.quantity), 0.0))
                                .filter(PurchaseV2.is_void == False).scalar() or 0.0)
    total_sent_qty = float(db.session.query(func.coalesce(func.sum(Delivery.quantity), 0.0))
                           .filter(Delivery.is_void == False).scalar() or 0.0)
    total_used_qty = float(db.session.query(func.coalesce(func.sum(UsageLogV2.quantity), 0.0))
                           .filter(UsageLogV2.is_void == False).scalar() or 0.0)

    if total_sent_qty > total_purchased_qty + eps:
        issues.append(
            f'Global sent ({total_sent_qty:,.2f}) is greater than global purchased ({total_purchased_qty:,.2f}).'
        )
    if total_used_qty > total_sent_qty + eps:
        issues.append(
            f'Global used ({total_used_qty:,.2f}) is greater than global sent ({total_sent_qty:,.2f}).'
        )

    purchase_rows = (db.session.query(
        PurchaseV2.id,
        PurchaseV2.quantity,
        func.coalesce(func.sum(Delivery.quantity), 0.0).label('delivered_qty')
    ).outerjoin(
        Delivery, and_(Delivery.purchase_id == PurchaseV2.id, Delivery.is_void == False)
    ).filter(
        PurchaseV2.is_void == False
    ).group_by(PurchaseV2.id, PurchaseV2.quantity).all())
    for pr in purchase_rows:
        ordered = float(pr.quantity or 0.0)
        delivered = float(pr.delivered_qty or 0.0)
        if delivered > ordered + eps:
            issues.append(f'PO #{pr.id} delivered ({delivered:,.2f}) exceeds ordered ({ordered:,.2f}).')

    delivered_by_material = dict(
        db.session.query(
            Delivery.material_id,
            func.coalesce(func.sum(Delivery.quantity), 0.0)
        ).filter(
            Delivery.is_void == False
        ).group_by(Delivery.material_id).all()
    )
    used_by_material = dict(
        db.session.query(
            UsageLogV2.material_id,
            func.coalesce(func.sum(UsageLogV2.quantity), 0.0)
        ).filter(
            UsageLogV2.is_void == False
        ).group_by(UsageLogV2.material_id).all()
    )
    material_rows = MaterialV2.query.filter(MaterialV2.is_void == False).order_by(MaterialV2.id.asc()).all()
    for mr in material_rows:
        delivered = float(delivered_by_material.get(mr.id, 0.0) or 0.0)
        used = float(used_by_material.get(mr.id, 0.0) or 0.0)
        if used > delivered + eps:
            mlabel = f'{mr.name} ({mr.unit or ""})'.strip()
            issues.append(f'Material {mlabel} used ({used:,.2f}) exceeds sent ({delivered:,.2f}).')

    return {
        'ok': len(issues) == 0,
        'issues': issues[:20],
        'issue_count': len(issues),
        'total_purchased_qty': total_purchased_qty,
        'total_sent_qty': total_sent_qty,
        'total_used_qty': total_used_qty
    }


def _material_v2_scope_stock_rows(project_id, stage_id=None):
    materials = MaterialV2.query.filter(MaterialV2.is_void == False).order_by(MaterialV2.name.asc(), MaterialV2.id.asc()).all()
    rows = []
    for m in materials:
        available = _material_v2_available(m.id, project_id, stage_id)
        if available <= 1e-9:
            continue
        rows.append({
            'id': int(m.id),
            'name': m.name,
            'unit': (m.unit or ''),
            'available_qty': float(available)
        })
    return rows


def _delivery_scope_qty_by_purchase(purchase_id, project_id, stage_id=None):
    q = db.session.query(func.coalesce(func.sum(Delivery.quantity), 0.0)).filter(
        Delivery.purchase_id == purchase_id,
        Delivery.is_void == False,
        Delivery.project_id == project_id
    )
    if stage_id:
        q = q.filter(Delivery.stage_id == stage_id)
    return float(q.scalar() or 0.0)


def _transfer_v2_material_between_scopes(material_id, from_project_id, from_stage_id, to_project_id, to_stage_id, quantity, date_value=None, note=''):
    qty_left = float(quantity or 0.0)
    if qty_left <= 0:
        return []
    purchases = (PurchaseV2.query
                 .filter(PurchaseV2.material_id == material_id, PurchaseV2.is_void == False)
                 .order_by(PurchaseV2.created_at.asc(), PurchaseV2.id.asc())
                 .all())
    transfer_ref = f"TRF-{_pkt_now_naive().strftime('%Y%m%d%H%M%S%f')}"
    moved_rows = []
    for p in purchases:
        if qty_left <= 1e-9:
            break
        scope_qty = _purchase_v2_available_in_scope_qty(p.id, from_project_id, from_stage_id, integrity=True)
        if scope_qty <= 1e-9:
            continue
        take = min(scope_qty, qty_left)
        from_scope = f'P{from_project_id}' + (f'-S{from_stage_id}' if from_stage_id else '')
        to_scope = f'P{to_project_id}' + (f'-S{to_stage_id}' if to_stage_id else '')
        note_out = f'Transfer out {transfer_ref}: {from_scope} -> {to_scope}'
        note_in = f'Transfer in {transfer_ref}: {from_scope} -> {to_scope}'
        if note:
            note_out = f'{note_out} | {note}'
            note_in = f'{note_in} | {note}'
        out_row = Delivery(
            purchase_id=p.id,
            material_id=material_id,
            project_id=from_project_id,
            stage_id=from_stage_id,
            quantity=-float(take),
            date=(date_value or _pkt_today()),
            notes=note_out,
            delivery_person='Stock Transfer',
            is_void=False,
            created_at=_pkt_now_naive()
        )
        in_row = Delivery(
            purchase_id=p.id,
            material_id=material_id,
            project_id=to_project_id,
            stage_id=to_stage_id,
            quantity=float(take),
            date=(date_value or _pkt_today()),
            notes=note_in,
            delivery_person='Stock Transfer',
            is_void=False,
            created_at=_pkt_now_naive()
        )
        db.session.add(out_row)
        db.session.add(in_row)
        moved_rows.append((p.id, float(take)))
        qty_left -= float(take)
    return moved_rows


def _material_v2_weighted_cost(material_id):
    rows = (db.session.query(PurchaseV2)
            .filter(PurchaseV2.material_id == material_id, PurchaseV2.is_void == False)
            .order_by(PurchaseV2.created_at.asc(), PurchaseV2.id.asc())
            .all())
    total_qty = sum(float(r.quantity or 0.0) for r in rows)
    total_cost = sum(float(r.total_amount or 0.0) for r in rows)
    if total_qty > 0:
        return float(total_cost / total_qty)
    last = (db.session.query(PurchaseV2)
            .filter(PurchaseV2.material_id == material_id, PurchaseV2.is_void == False)
            .order_by(PurchaseV2.created_at.desc(), PurchaseV2.id.desc())
            .first())
    return float(last.unit_price if last else 0.0)


def _purchase_v2_delivered_qty(purchase_id, *, exclude_delivery_id=None, integrity=False):
    query = db.session.query(func.coalesce(func.sum(Delivery.quantity), 0.0)).filter(
        Delivery.purchase_id == purchase_id, Delivery.is_void == False)
    if exclude_delivery_id:
        query = query.filter(Delivery.id != exclude_delivery_id)
    return float((integrity_query(query) if integrity else query).scalar() or 0.0)


def _purchase_v2_delivered_to_scope_qty(purchase_id, project_id, stage_id):
    if (not purchase_id) or (not project_id):
        return 0.0
    q = db.session.query(func.coalesce(func.sum(Delivery.quantity), 0.0)).filter(
        Delivery.purchase_id == purchase_id,
        Delivery.project_id == project_id,
        Delivery.is_void == False
    )
    if stage_id:
        q = q.filter(Delivery.stage_id == stage_id)
    return float(q.scalar() or 0.0)


def _purchase_v2_used_in_scope_qty(purchase_id, project_id, stage_id, exclude_usage_id=None):
    if (not purchase_id) or (not project_id):
        return 0.0
    q = db.session.query(func.coalesce(func.sum(UsageLogV2.quantity), 0.0)).filter(
        UsageLogV2.purchase_id == purchase_id,
        UsageLogV2.project_id == project_id,
        UsageLogV2.is_void == False
    )
    if stage_id:
        q = q.filter(UsageLogV2.stage_id == stage_id)
    if exclude_usage_id:
        q = q.filter(UsageLogV2.id != exclude_usage_id)
    return float(q.scalar() or 0.0)


def _purchase_v2_scope_remaining_map(material_id, project_id, stage_id, exclude_usage_id=None, *, integrity=False):
    if (not material_id) or (not project_id):
        return {}
    purchases_query = (db.session.query(PurchaseV2.id) if integrity else PurchaseV2.query)
    purchases = (purchases_query
                 .filter(
                     PurchaseV2.material_id == material_id,
                     PurchaseV2.is_void == False
                 )
                 .order_by(PurchaseV2.created_at.asc(), PurchaseV2.id.asc()))
    purchases = (integrity_query(purchases) if integrity else purchases).all()
    purchase_ids = [int(p.id) for p in purchases]
    if not purchase_ids:
        return {}

    del_q = (db.session.query(
        Delivery.purchase_id,
        func.coalesce(func.sum(Delivery.quantity), 0.0)
    ).filter(
        Delivery.is_void == False,
        Delivery.project_id == project_id,
        Delivery.purchase_id.in_(purchase_ids)
    ))
    if stage_id:
        del_q = del_q.filter(Delivery.stage_id == stage_id)
    del_q = del_q.group_by(Delivery.purchase_id)
    delivered_map = {int(pid): float(qty or 0.0) for pid, qty in (integrity_query(del_q) if integrity else del_q).all()}

    use_q = (db.session.query(
        UsageLogV2.purchase_id,
        func.coalesce(func.sum(UsageLogV2.quantity), 0.0)
    ).filter(
        UsageLogV2.is_void == False,
        UsageLogV2.project_id == project_id,
        UsageLogV2.purchase_id.in_(purchase_ids)
    ))
    if stage_id:
        use_q = use_q.filter(UsageLogV2.stage_id == stage_id)
    if exclude_usage_id:
        use_q = use_q.filter(UsageLogV2.id != exclude_usage_id)
    linked_used_map = {int(pid): float(qty or 0.0) for pid, qty in (integrity_query(use_q) if integrity else use_q).group_by(UsageLogV2.purchase_id).all()}

    legacy_q = db.session.query(func.coalesce(func.sum(UsageLogV2.quantity), 0.0)).filter(
        UsageLogV2.is_void == False,
        UsageLogV2.project_id == project_id,
        UsageLogV2.material_id == material_id,
        UsageLogV2.purchase_id.is_(None)
    )
    if stage_id:
        legacy_q = legacy_q.filter(UsageLogV2.stage_id == stage_id)
    if exclude_usage_id:
        legacy_q = legacy_q.filter(UsageLogV2.id != exclude_usage_id)
    legacy_unlinked_qty = float((integrity_query(legacy_q) if integrity else legacy_q).scalar() or 0.0)

    remaining_map = {}
    carry = max(0.0, legacy_unlinked_qty)
    for p in purchases:
        pid = int(p.id)
        delivered = float(delivered_map.get(pid, 0.0) or 0.0)
        linked_used = float(linked_used_map.get(pid, 0.0) or 0.0)
        remaining = max(0.0, delivered - linked_used)
        if carry > 1e-9 and remaining > 1e-9:
            take = min(remaining, carry)
            remaining -= take
            carry -= take
        remaining_map[pid] = max(0.0, remaining)
    return remaining_map


def _purchase_v2_available_in_project_qty(purchase_id, project_id, exclude_usage_id=None, *, integrity=False):
    purchase = PurchaseV2.query.get(purchase_id) if purchase_id else None
    if (not purchase) or purchase.is_void:
        return 0.0
    remaining_map = _purchase_v2_scope_remaining_map(purchase.material_id, project_id, None, exclude_usage_id=exclude_usage_id, integrity=integrity)
    return float(remaining_map.get(int(purchase.id), 0.0) or 0.0)


def _purchase_v2_available_in_scope_qty(purchase_id, project_id, stage_id, exclude_usage_id=None, *, integrity=False):
    purchase = PurchaseV2.query.get(purchase_id) if purchase_id else None
    if (not purchase) or purchase.is_void:
        return 0.0
    remaining_map = _purchase_v2_scope_remaining_map(
        purchase.material_id,
        project_id,
        stage_id,
        exclude_usage_id=exclude_usage_id, integrity=integrity
    )
    return float(remaining_map.get(int(purchase.id), 0.0) or 0.0)


def _sync_purchase_v2_ledger(purchase_row):
    if not purchase_row:
        return
    rows = (SupplierLedger.query
            .filter(
                SupplierLedger.reference_type == 'purchase_v2',
                SupplierLedger.reference_id == purchase_row.id
            )
            .order_by(SupplierLedger.id.asc())
            .all())
    active_rows = [r for r in rows if not bool(r.is_void)]
    should_be_unpaid = (not bool(purchase_row.is_void)) and ((purchase_row.payment_status or 'unpaid').strip().lower() == 'unpaid')
    if should_be_unpaid:
        if active_rows:
            keep = active_rows[0]
            keep.supplier_id = purchase_row.supplier_id
            keep.entry_type = 'debit'
            keep.amount = float(purchase_row.total_amount or 0.0)
            keep.note = f'Unpaid purchase #{purchase_row.id}'
            for extra in active_rows[1:]:
                extra.is_void = True
                extra.void_reason = 'Merged duplicate purchase ledger rows'
                extra.voided_at = _pkt_now_naive()
        else:
            db.session.add(SupplierLedger(
                supplier_id=purchase_row.supplier_id,
                entry_type='debit',
                amount=float(purchase_row.total_amount or 0.0),
                reference_type='purchase_v2',
                reference_id=purchase_row.id,
                note=f'Unpaid purchase #{purchase_row.id}',
                is_void=False,
                created_at=_pkt_now_naive()
            ))
    else:
        for row in active_rows:
            row.is_void = True
            row.void_reason = 'Purchase changed to paid or void'
            row.voided_at = _pkt_now_naive()


def _repair_supplier_purchase_v2_ledger(supplier_id):
    if not supplier_id:
        return
    purchases = (PurchaseV2.query
                 .filter(PurchaseV2.supplier_id == supplier_id, PurchaseV2.is_void == False)
                 .order_by(PurchaseV2.id.asc())
                 .all())
    for p in purchases:
        _sync_purchase_v2_ledger(p)


def validate_delivery_reduction(delivery, new_quantity):
    """Keep already-consumed stock backed by its active delivery.

    Use the canonical remaining map, including legacy unlinked FIFO usage,
    instead of duplicating stock accounting in the HTML and JSON handlers.
    Raises before callers modify the delivery or its audit metadata.
    """
    reduction = float(delivery.quantity or 0.0) - float(new_quantity)
    if reduction <= 0:
        return
    available = _purchase_v2_available_in_scope_qty(
        delivery.purchase_id, delivery.project_id, delivery.stage_id, integrity=True)
    if reduction > available + 1e-9:
        raise ValueError('Cannot reduce or void delivery: stock has already been used. Void the usage first.')


def _purchase_v2_pending_rows(material_id=None, supplier_id=None, project_id=None):
    """Purchase orders that still owe stock, highest PO # first.

    A purchase order is *pending* while its ordered quantity is larger than
    everything that has been delivered against it.  Every row also carries the
    sites that already took stock from that PO, because that is what lets the
    Delivery pop-up filter the list by site — a PO is bought at store level and
    only becomes site-specific once it is delivered.

    ``project_id`` narrows the list to the POs that have already delivered to
    that site; leaving it out returns every pending PO (all sites).
    """
    pq = PurchaseV2.query.filter(PurchaseV2.is_void == False)
    if material_id:
        pq = pq.filter(PurchaseV2.material_id == int(material_id))
    if supplier_id:
        pq = pq.filter(PurchaseV2.supplier_id == int(supplier_id))
    purchases = pq.order_by(PurchaseV2.id.desc()).all()
    if not purchases:
        return []
    purchase_ids = [int(p.id) for p in purchases]

    delivered_map = dict(
        db.session.query(
            Delivery.purchase_id,
            func.coalesce(func.sum(Delivery.quantity), 0.0)
        ).filter(
            Delivery.is_void == False,
            Delivery.purchase_id.in_(purchase_ids)
        ).group_by(Delivery.purchase_id).all()
    )
    # One grouped read for the site split, then one read for the site names —
    # the pop-up must not turn into a query per row.
    site_rows = (db.session.query(
        Delivery.purchase_id,
        Delivery.project_id,
        func.coalesce(func.sum(Delivery.quantity), 0.0)
    ).filter(
        Delivery.is_void == False,
        Delivery.purchase_id.in_(purchase_ids)
    ).group_by(Delivery.purchase_id, Delivery.project_id).all())
    site_ids = sorted({int(pid) for _, pid, _ in site_rows if pid})
    site_names = (dict(db.session.query(Project.id, Project.name)
                       .filter(Project.id.in_(site_ids)).all())
                  if site_ids else {})
    sites_by_purchase = {}
    for purchase_id, project_id_row, qty in site_rows:
        qty = float(qty or 0.0)
        if abs(qty) <= 1e-9:
            continue
        pid = int(project_id_row or 0)
        sites_by_purchase.setdefault(int(purchase_id), []).append({
            'id': pid,
            'name': site_names.get(pid, f'Site #{pid}'),
            'qty': qty
        })

    out = []
    for p in purchases:
        ordered = float(p.quantity or 0.0)
        delivered = float(delivered_map.get(p.id, 0.0) or 0.0)
        pending = max(0.0, ordered - delivered)
        if pending <= 1e-9:
            continue
        sites = sorted(sites_by_purchase.get(int(p.id), []),
                       key=lambda s: (-s['qty'], s['name']))
        if project_id and not any(int(s['id']) == int(project_id) for s in sites):
            continue
        out.append({
            '_hdc_entity': 'hdc_purchase_v2',
            '_hdc_id': int(p.id),
            'id': int(p.id),
            'date': p.date,
            'supplier_id': int(p.supplier_id or 0),
            'supplier_name': (p.supplier.name if p.supplier else '-'),
            'material_id': int(p.material_id or 0),
            'material_name': (p.material.name if p.material else '-'),
            'unit': (p.material.unit if p.material else ''),
            'ordered_qty': ordered,
            'delivered_qty': delivered,
            'pending_qty': pending,
            'unit_price': float(p.unit_price or 0.0),
            'pending_value': float(p.unit_price or 0.0) * pending,
            'payment_status': (p.payment_status or 'unpaid'),
            'challan_no': (p.challan_no or ''),
            'sites': sites,
            'site_ids': ','.join(str(int(s['id'])) for s in sites if s['id'])
        })
    return out
