"""Business logic for HDC Tool Rental."""

from datetime import date, datetime
from sqlalchemy import func, or_

from hdc.extensions import db
from hdc.models.accounts import Account
from hdc.models.tool_rental import (
    MOVEMENT_PURCHASE_IN, MOVEMENT_SCRAP_OUT, TOOL_DISCOUNT_REASONS, TOOL_DISCOUNT_REASON_LABELS,
    TOOL_SCRAP_REASONS, Tool, ToolCategory,
    ToolMovementLog, ToolPurchase, ToolRental, ToolRentalAccountTxn, ToolRentalDiscount,
    ToolRentalItem, ToolRentalPayment, ToolRentalReturn, ToolRentalReturnItem,
    ToolRentalTransfer, ToolScrap, ToolSerial, ToolSerialMovement
)
from hdc.services.record_permissions import integrity_message, integrity_query, record_code_query
from hdc.utils.dates import _pkt_now_naive, _pkt_today
from hdc.utils.normalize import _normalize_name_ci

_ACCOUNT_COMPANY_TYPES = ('company', 'cash', 'bank')
EPS = 0.001

# Location labels used by the stock movement log.  Kept here so the inventory,
# purchase and scrap flows can never drift apart on wording.
SUPPLIER_LABEL = 'Supplier / Purchase'
STORE_LABEL = 'Warehouse / Store'
SCRAP_LABEL = 'Scrap / Discard'


def _next_tool_code():
    last_id = record_code_query(db.session.query(func.max(Tool.id)), Tool.__tablename__).scalar()
    nxt = int(last_id or 0) + 1
    return f"TOOL-{nxt:04d}"

def _next_rental_code():
    last_id = record_code_query(db.session.query(func.max(ToolRental.id)), ToolRental.__tablename__).scalar()
    nxt = int(last_id or 0) + 1
    return f"RENT-{nxt:05d}"

def _next_purchase_code():
    last_id = record_code_query(db.session.query(func.max(ToolPurchase.id)), ToolPurchase.__tablename__).scalar()
    nxt = int(last_id or 0) + 1
    return f"PUR-TOOL-{nxt:05d}"

def _next_scrap_code():
    last_id = record_code_query(db.session.query(func.max(ToolScrap.id)), ToolScrap.__tablename__).scalar()
    nxt = int(last_id or 0) + 1
    return f"SCRAP-{nxt:05d}"

def _next_discount_code():
    last_id = record_code_query(db.session.query(func.max(ToolRentalDiscount.id)), ToolRentalDiscount.__tablename__).scalar()
    nxt = int(last_id or 0) + 1
    return f"DISC-{nxt:05d}"

def _ensure_tool_category(name):
    name = (name or '').strip()
    if not name:
        return None
    existing = ToolCategory.query.filter(func.lower(ToolCategory.name) == name.lower()).first()
    if existing:
        return existing
    cat = ToolCategory(name=name, active_status=True)
    db.session.add(cat)
    db.session.flush()
    return cat

# --------------------------------------------------------------------------- #
# stock life-cycle: buy more of a tool / throw the rest away
# --------------------------------------------------------------------------- #
def _parse_date(raw, fallback=None):
    # A real date/datetime is what the models store, so service callers may
    # hand one straight over instead of re-serialising it first.
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    raw = (raw or '').strip()
    if not raw:
        return fallback or _pkt_today()
    try:
        return datetime.strptime(raw, '%Y-%m-%d').date()
    except Exception:
        return fallback or _pkt_today()


def _round2(value):
    return round(float(value or 0.0) + 0.0, 2)


def _empty_stock_stats():
    return {'purchased_qty': 0.0, 'purchase_value': 0.0, 'scrapped_qty': 0.0,
            'scrapped_value': 0.0, 'purchases': 0, 'scraps': 0}


def tool_stock_aggregates(tool_ids=None):
    """Purchased / scrapped totals per tool in two grouped queries.

    Returns ``{tool_id: {'purchased_qty', 'purchase_value', 'scrapped_qty',
    'scrapped_value', 'purchases', 'scraps'}}`` so list pages can show the full
    stock life-cycle without an N+1 query storm.
    """
    ids = [int(i) for i in (tool_ids or []) if i]
    stats = {}

    q = (db.session.query(ToolPurchase.tool_id,
                          func.coalesce(func.sum(ToolPurchase.qty), 0.0),
                          func.coalesce(func.sum(ToolPurchase.total_cost), 0.0),
                          func.count(ToolPurchase.id))
         .group_by(ToolPurchase.tool_id))
    if ids:
        q = q.filter(ToolPurchase.tool_id.in_(ids))
    for tool_id, qty, value, count in q.all():
        slot = stats.setdefault(int(tool_id), _empty_stock_stats())
        slot['purchased_qty'] = _round2(qty)
        slot['purchase_value'] = _round2(value)
        slot['purchases'] = int(count or 0)

    sq = (db.session.query(ToolScrap.tool_id,
                           func.coalesce(func.sum(ToolScrap.qty), 0.0),
                           func.coalesce(func.sum(ToolScrap.value_written_off), 0.0),
                           func.count(ToolScrap.id))
          .group_by(ToolScrap.tool_id))
    if ids:
        sq = sq.filter(ToolScrap.tool_id.in_(ids))
    for tool_id, qty, value, count in sq.all():
        slot = stats.setdefault(int(tool_id), _empty_stock_stats())
        slot['scrapped_qty'] = _round2(qty)
        slot['scrapped_value'] = _round2(value)
        slot['scraps'] = int(count or 0)
    return stats


def stock_stats_for(stats, tool_id):
    """Never-None lookup used by templates."""
    return (stats or {}).get(int(tool_id or 0)) or _empty_stock_stats()


def record_tool_purchase(tool_id, qty, unit_cost=None, supplier='', purchase_date=None,
                         reference='', notes='', update_cost=True, is_opening_stock=False,
                         created_by=None, commit=True, serial_numbers=None, start_no=None):
    """Buy more of a tool we already own: +qty on total_quantity + audit row.

    ``unit_cost`` falls back to the tool's current cost and only overwrites it
    when ``update_cost`` is true (a fresh market price is usually the better
    number to value the remaining stock with).
    """
    tool = db.session.get(Tool, int(tool_id or 0))
    if not tool:
        return False, 'Tool not found.', None
    if tool.is_void:
        return False, f'{tool.name} is archived. Un-archive it before purchasing stock.', None

    qty = _round2(qty)
    if qty <= EPS:
        return False, 'Purchase quantity must be greater than 0.', None

    # Ensure any pre-existing owned quantity already has serial markings before
    # we add the newly purchased pieces.
    old_qty = _round2(tool.total_quantity)
    if old_qty > EPS:
        ensure_tool_serials(tool=tool, created_by=created_by, commit=False)

    cost = _round2(tool.purchase_cost if unit_cost is None else unit_cost)
    if cost < 0:
        cost = 0.0
    total_cost = _round2(qty * cost)
    when = _parse_date(purchase_date)

    purchase = ToolPurchase(
        purchase_code=_next_purchase_code(),
        tool_id=tool.id,
        purchase_date=when,
        qty=qty,
        unit_cost=cost,
        total_cost=total_cost,
        supplier=(supplier or '').strip()[:150],
        reference=(reference or '').strip()[:120],
        notes=(notes or '').strip()[:300],
        is_opening_stock=bool(is_opening_stock),
        created_by=created_by,
    )
    db.session.add(purchase)
    db.session.flush()

    tool.total_quantity = _round2(old_qty + qty)
    if update_cost and cost > 0:
        tool.purchase_cost = cost
    tool.updated_at = _pkt_now_naive()

    create_movement_log(
        tool_id=tool.id, rental_id=None, movement_type=MOVEMENT_PURCHASE_IN,
        from_label=(purchase.supplier or SUPPLIER_LABEL), to_label=STORE_LABEL,
        qty=qty,
        notes=(f'Purchase {purchase.purchase_code}: +{qty:g} {tool.unit} '
               f'@ {cost:,.0f} = {total_cost:,.0f}'),
    )

    # Auto-create individual serial markings (e.g. "Shovel No 1", "Shovel No 2")
    int_qty = int(round(qty))
    if int_qty > 0:
        create_tool_serials(
            tool_id=tool.id,
            qty=int_qty,
            serial_numbers=serial_numbers,
            start_no=start_no,
            created_by=created_by,
            from_label=(purchase.supplier or SUPPLIER_LABEL),
            notes_prefix=f'Purchase {purchase.purchase_code}',
            commit=False,
        )

    if commit:
        db.session.commit()
    return True, '', purchase


def tool_available_for_integrity(tool):
    query = db.session.query(func.coalesce(func.sum(ToolRentalItem.qty_pending), 0.0)).filter(
        ToolRentalItem.tool_id == tool.id)
    return max(0.0, float(tool.total_quantity or 0) - float(integrity_query(query).scalar() or 0))


def record_tool_scrap(tool_id, qty, reason='damaged', scrap_date=None, reference='',
                      notes='', created_by=None, commit=True, serial_ids=None):
    """Throw away / lose / sell as scrap: -qty on total_quantity + audit row.

    Only what is sitting in the store can be scrapped - rented-out pieces have
    to come back first, otherwise the owned-vs-located balance would break.
    """
    tool = db.session.get(Tool, int(tool_id or 0))
    if not tool:
        return False, 'Tool not found.', None
    if tool.is_void:
        return False, f'{tool.name} is archived. Un-archive it before recording scrap.', None

    ensure_tool_serials(tool=tool, created_by=created_by, commit=False)

    parsed_serial_ids = []
    if serial_ids:
        if isinstance(serial_ids, str):
            parsed_serial_ids = [int(x.strip()) for x in serial_ids.split(',') if x.strip().isdigit()]
        else:
            for x in serial_ids:
                if str(x).strip().isdigit():
                    parsed_serial_ids.append(int(str(x).strip()))
        # Deduplicate preserving order
        seen_sids = set()
        parsed_serial_ids = [sid for sid in parsed_serial_ids if not (sid in seen_sids or seen_sids.add(sid))]

    if parsed_serial_ids and (qty is None or float(qty or 0) <= 0):
        qty = float(len(parsed_serial_ids))

    qty = _round2(qty)
    if qty <= EPS:
        return False, 'Scrap quantity must be greater than 0.', None

    available = _round2(tool_available_for_integrity(tool))
    if qty > available + EPS:
        return False, integrity_message('Insufficient available tool stock. Return rented tools first.',
                       f'Only {available:g} {tool.unit} of {tool.name} is in the store '
                       f'({tool.rented_out_qty:g} is rented out) - return it first.'), None

    # Validate explicit serial_ids if supplied
    serials_to_scrap = []
    if parsed_serial_ids:
        for sid in parsed_serial_ids:
            s_obj = db.session.get(ToolSerial, sid)
            if not s_obj or s_obj.tool_id != tool.id or getattr(s_obj, 'is_scrapped', False):
                return False, f'Invalid serial selection for {tool.name}.', None
            if not s_obj.is_in_store:
                return False, f'{s_obj.serial_number} is currently out ({s_obj.current_location_label}) — return it to store before scrapping.', None
            serials_to_scrap.append(s_obj)
        qty = _round2(float(len(serials_to_scrap)))
    else:
        int_scrap = int(round(qty))
        in_store_list = tool.in_store_serials
        if int_scrap > 0 and in_store_list:
            serials_to_scrap = in_store_list[:int_scrap]

    reason_key = (reason or 'damaged').strip().lower()
    if reason_key not in {k for k, _ in TOOL_SCRAP_REASONS}:
        reason_key = 'other'

    unit_cost = _round2(tool.purchase_cost)
    value = _round2(qty * unit_cost)
    when = _parse_date(scrap_date)

    scrap_notes = (notes or '').strip()

    scrap = ToolScrap(
        scrap_code=_next_scrap_code(),
        tool_id=tool.id,
        scrap_date=when,
        qty=qty,
        reason=reason_key,
        unit_cost=unit_cost,
        value_written_off=value,
        reference=(reference or '').strip()[:120],
        notes=scrap_notes[:300],
        created_by=created_by,
    )
    db.session.add(scrap)
    db.session.flush()

    old_qty = _round2(tool.total_quantity)
    tool.total_quantity = max(0.0, _round2(old_qty - qty))
    tool.updated_at = _pkt_now_naive()

    for s_obj in serials_to_scrap:
        prev_loc = s_obj.current_location_label or STORE_LABEL
        s_obj.is_in_store = False
        s_obj.is_scrapped = True
        s_obj.current_rental_id = None
        s_obj.current_rental_item_id = None
        s_obj.status = 'scrapped'
        s_obj.condition = 'lost' if reason_key == 'lost' else 'damaged'
        s_obj.current_location_label = SCRAP_LABEL
        s_obj.updated_at = _pkt_now_naive()
        s_obj.updated_by = created_by
        create_serial_movement(
            serial_id=s_obj.id,
            movement_type=MOVEMENT_SCRAP_OUT,
            from_label=prev_loc,
            to_label=SCRAP_LABEL,
            notes=f'Scrap {scrap.scrap_code} ({scrap.reason_label}): {s_obj.serial_number}',
            created_by=created_by,
        )

    create_movement_log(
        tool_id=tool.id, rental_id=None, movement_type=MOVEMENT_SCRAP_OUT,
        from_label=STORE_LABEL, to_label=SCRAP_LABEL, qty=qty,
        notes=(f'Scrap {scrap.scrap_code}: -{qty:g} {tool.unit} '
               f'({scrap.reason_label}), written off {value:,.0f}'),
    )
    if commit:
        db.session.commit()
    return True, '', scrap


def tool_purchases(tool_id=None, date_from=None, date_to=None, search_text=None, limit=200):
    """Purchase (stock-in) register, newest first."""
    q = ToolPurchase.query.join(Tool, Tool.id == ToolPurchase.tool_id).filter(
        Tool.is_void == False)  # noqa: E712
    if tool_id:
        q = q.filter(ToolPurchase.tool_id == int(tool_id))
    if date_from:
        q = q.filter(ToolPurchase.purchase_date >= date_from)
    if date_to:
        q = q.filter(ToolPurchase.purchase_date <= date_to)
    if search_text:
        like = f"%{search_text.strip().lower()}%"
        q = q.filter(or_(
            func.lower(func.coalesce(Tool.name, '')).like(like),
            func.lower(func.coalesce(Tool.tool_code, '')).like(like),
            func.lower(func.coalesce(ToolPurchase.supplier, '')).like(like),
            func.lower(func.coalesce(ToolPurchase.reference, '')).like(like),
            func.lower(func.coalesce(ToolPurchase.purchase_code, '')).like(like),
        ))
    return q.order_by(ToolPurchase.purchase_date.desc(), ToolPurchase.id.desc()).limit(limit).all()


def tool_scraps(tool_id=None, date_from=None, date_to=None, search_text=None, limit=200):
    """Scrap (write-off) register, newest first."""
    q = ToolScrap.query.join(Tool, Tool.id == ToolScrap.tool_id).filter(
        Tool.is_void == False)  # noqa: E712
    if tool_id:
        q = q.filter(ToolScrap.tool_id == int(tool_id))
    if date_from:
        q = q.filter(ToolScrap.scrap_date >= date_from)
    if date_to:
        q = q.filter(ToolScrap.scrap_date <= date_to)
    if search_text:
        like = f"%{search_text.strip().lower()}%"
        q = q.filter(or_(
            func.lower(func.coalesce(Tool.name, '')).like(like),
            func.lower(func.coalesce(Tool.tool_code, '')).like(like),
            func.lower(func.coalesce(ToolScrap.reference, '')).like(like),
            func.lower(func.coalesce(ToolScrap.notes, '')).like(like),
            func.lower(func.coalesce(ToolScrap.scrap_code, '')).like(like),
        ))
    return q.order_by(ToolScrap.scrap_date.desc(), ToolScrap.id.desc()).limit(limit).all()

def recalc_rental_totals(rental_id):
    rental = db.session.get(ToolRental, rental_id)
    if not rental:
        return
    items = ToolRentalItem.query.filter_by(rental_id=rental.id).all()
    total_rented = sum(float(i.qty_rented or 0) for i in items)
    total_returned = sum(float(i.qty_returned or 0) for i in items)
    total_pending = max(0.0, total_rented - total_returned)
    total_amount = sum(float(i.amount or 0) for i in items)

    total_paid = db.session.query(func.coalesce(func.sum(ToolRentalPayment.amount), 0.0)).filter(
        ToolRentalPayment.rental_id == rental.id,
        ToolRentalPayment.is_void == False
    ).scalar() or 0.0

    # A discount settles part of the receivable without cash ever moving, so
    # it is summed from its own register (hdc_tool_rental_discount) exactly
    # like payments are -- one row per reason, voidable on its own.
    total_discount = db.session.query(func.coalesce(func.sum(ToolRentalDiscount.amount), 0.0)).filter(
        ToolRentalDiscount.rental_id == rental.id,
        ToolRentalDiscount.is_void == False
    ).scalar() or 0.0

    rental.total_rented_qty = total_rented
    rental.total_returned_qty = total_returned
    rental.total_amount = total_amount if total_amount>0 else float(rental.total_amount or 0)
    rental.total_paid = float(total_paid)
    rental.total_discount = min(float(total_discount), max(0.0, float(rental.total_amount or 0.0)))

    if rental.is_void:
        rental.status = 'closed'
    elif total_pending <= 0.001:
        rental.status = 'returned'
    elif total_returned > 0:
        rental.status = 'partially_returned'
    else:
        if rental.expected_return_date and rental.expected_return_date < _pkt_today():
            rental.status = 'overdue'
        else:
            rental.status = 'active'

    if (rental.billing_type or '').lower() == 'no_charge':
        rental.payment_status = 'no_charge'
    else:
        pending_amt = max(0.0, float(rental.total_amount or 0)
                          - float(total_paid)
                          - float(rental.total_discount or 0.0))
        if pending_amt <= 0.001 and float(rental.total_amount or 0) > 0:
            rental.payment_status = 'paid'
        elif float(total_paid) > 0 and pending_amt > 0:
            rental.payment_status = 'partial'
        elif (rental.payment_status or '') == 'credit':
            if pending_amt <= 0.001:
                rental.payment_status = 'paid'
            else:
                rental.payment_status = 'credit'
        else:
            rental.payment_status = 'unpaid' if float(rental.total_amount or 0) > 0 else 'unpaid'

    if total_pending <= 0.001 and rental.payment_status in ('paid','no_charge'):
        rental.status = 'closed'

    db.session.flush()

def create_movement_log(tool_id, rental_id, movement_type, from_label, to_label, qty, transfer_id=None, return_id=None, notes=""):
    log = ToolMovementLog(
        tool_id=tool_id,
        rental_id=rental_id,
        transfer_id=transfer_id,
        return_id=return_id,
        movement_type=movement_type,
        from_location_label=from_label,
        to_location_label=to_label,
        qty=float(qty or 0),
        timestamp=_pkt_now_naive(),
        notes=notes
    )
    db.session.add(log)
    db.session.flush()
    return log

def get_tool_tracking_chain(tool_id):
    logs = (ToolMovementLog.query
            .filter_by(tool_id=tool_id)
            .order_by(ToolMovementLog.timestamp.asc(), ToolMovementLog.id.asc())
            .all())
    return logs

def _rental_holder_label(rental):
    """Short 'who holds this rental' label used in the a > b > c chain."""
    if rental.renter_type == 'internal' and rental.project:
        lbl = rental.project.name
        if rental.stage:
            lbl += f" > {rental.stage.name}"
        return lbl
    return rental.customer_name or 'External'


def rental_transfer_chain(rental_id):
    """The full hand-over chain ``a > b > c`` across transfer-linked rentals.

    A "Transfer Rental" closes one rental and opens another for the new holder,
    linking them through ``ToolRentalTransfer.to_rental_id``.  Starting from any
    rental in that lineage, walk backwards to the first holder(s) and forwards
    to the last, and return the ordered holder labels so the UI can render e.g.
    ``Ali Traders > Site B > Site C``.  When one transfer merged several holders
    into the new rental, every one of those holders is listed, so the chain
    reads ``Holder A > Holder B > Site C`` instead of silently dropping one.
    A rental with no transfer links returns a single-element list (just itself).
    """
    rental = db.session.get(ToolRental, rental_id)
    if not rental:
        return []
    seen = {int(rental.id)}
    ids = [int(rental.id)]

    # backwards: everyone who handed tools over to this rental. A single-holder
    # transfer has one parent; a merged multi-holder transfer has several, all
    # listed oldest hand-over first.
    frontier = [int(rental.id)]
    while frontier:
        cur = frontier.pop(0)
        parents = (ToolRentalTransfer.query
                   .filter(ToolRentalTransfer.to_rental_id == cur)
                   .order_by(ToolRentalTransfer.transfer_date.asc(), ToolRentalTransfer.id.asc())
                   .all())
        fresh = [int(parent.rental_id) for parent in parents
                 if int(parent.rental_id) not in seen]
        if fresh:
            at = ids.index(cur) if cur in ids else 0
            ids[at:at] = fresh
            seen.update(fresh)
            frontier.extend(fresh)

    # forwards: who did we hand it over to?
    cur = int(rental.id)
    while True:
        nxt = (ToolRentalTransfer.query
               .filter(ToolRentalTransfer.rental_id == cur,
                       ToolRentalTransfer.to_rental_id.isnot(None))
               .order_by(ToolRentalTransfer.transfer_date.desc(), ToolRentalTransfer.id.desc())
               .first())
        if not nxt or int(nxt.to_rental_id) in seen:
            break
        ids.append(int(nxt.to_rental_id))
        seen.add(int(nxt.to_rental_id))
        cur = int(nxt.to_rental_id)

    labels = []
    for rid in ids:
        r = db.session.get(ToolRental, rid)
        if not r:
            continue
        label = _rental_holder_label(r)
        # Two holders of one merge can carry the same label (e.g. the same site
        # under two rentals) — a chain is the path, so collapse repeats.
        if labels and labels[-1] == label:
            continue
        labels.append(label)
    return labels


def get_rental_tracking_chain(rental_id):
    rental = db.session.get(ToolRental, rental_id)
    if not rental:
        return []
    chain = []
    if rental.renter_type == 'internal' and rental.project:
        initial = rental.project.name
        if rental.stage:
            initial += f" / {rental.stage.name}"
    else:
        initial = rental.customer_name or "External"
    chain.append({'label': initial, 'type': 'rental_start', 'date': rental.rental_date, 'is_current': False})

    transfers = (ToolRentalTransfer.query
                 .filter_by(rental_id=rental.id)
                 .order_by(ToolRentalTransfer.transfer_date.asc(), ToolRentalTransfer.id.asc())
                 .all())
    for t in transfers:
        chain.append({
            'label': t.to_location_label,
            'type': 'transfer',
            'date': t.transfer_date,
            'is_current': False,
            'transfer': t
        })
    if chain:
        chain[-1]['is_current'] = True
    return chain

def tool_kpis():
    total_tools = db.session.query(func.coalesce(func.sum(Tool.total_quantity), 0.0)).filter(Tool.is_void==False).scalar() or 0.0
    total_tool_types = db.session.query(func.count(Tool.id)).filter(Tool.is_void==False).scalar() or 0
    rented_out = db.session.query(func.coalesce(func.sum(ToolRentalItem.qty_pending), 0.0)).scalar() or 0.0
    available = max(0.0, float(total_tools) - float(rented_out))
    active_rentals = db.session.query(func.count(ToolRental.id)).filter(ToolRental.status.in_(['active','partially_returned','overdue']), ToolRental.is_void==False).scalar() or 0
    overdue_rentals = db.session.query(func.count(ToolRental.id)).filter(ToolRental.status=='overdue', ToolRental.is_void==False).scalar() or 0
    total_amount = db.session.query(func.coalesce(func.sum(ToolRental.total_amount), 0.0)).filter(ToolRental.is_void==False, ToolRental.billing_type!='no_charge').scalar() or 0.0
    total_paid = db.session.query(func.coalesce(func.sum(ToolRental.total_paid), 0.0)).filter(ToolRental.is_void==False).scalar() or 0.0
    pending_amount = max(0.0, float(total_amount) - float(total_paid))
    pending_tools = db.session.query(func.coalesce(func.sum(ToolRentalItem.qty_pending), 0.0)).scalar() or 0.0
    internal_rentals = db.session.query(func.count(ToolRental.id)).filter(ToolRental.renter_type=='internal', ToolRental.is_void==False).scalar() or 0
    external_rentals = db.session.query(func.count(ToolRental.id)).filter(ToolRental.renter_type=='external', ToolRental.is_void==False).scalar() or 0
    credit_rentals = db.session.query(func.count(ToolRental.id)).filter(ToolRental.payment_status=='credit', ToolRental.is_void==False).scalar() or 0
    credit_amount = db.session.query(func.coalesce(func.sum(ToolRental.total_amount - ToolRental.total_paid), 0.0)).filter(ToolRental.payment_status=='credit', ToolRental.is_void==False).scalar() or 0.0
    purchased_qty = db.session.query(func.coalesce(func.sum(ToolPurchase.qty), 0.0)).scalar() or 0.0
    purchase_value = db.session.query(func.coalesce(func.sum(ToolPurchase.total_cost), 0.0)).scalar() or 0.0
    scrapped_qty = db.session.query(func.coalesce(func.sum(ToolScrap.qty), 0.0)).scalar() or 0.0
    scrapped_value = db.session.query(func.coalesce(func.sum(ToolScrap.value_written_off), 0.0)).scalar() or 0.0
    return {
        'total_tools': float(total_tools),
        'total_tool_types': int(total_tool_types),
        'rented_out': float(rented_out),
        'available': float(available),
        'active_rentals': int(active_rentals),
        'overdue_rentals': int(overdue_rentals),
        'total_amount': float(total_amount),
        'total_paid': float(total_paid),
        'pending_amount': float(pending_amount),
        'pending_tools': float(pending_tools),
        'internal_rentals': int(internal_rentals),
        'external_rentals': int(external_rentals),
        'credit_rentals': int(credit_rentals),
        'credit_amount': float(credit_amount),
        'purchased_qty': float(purchased_qty),
        'purchase_value': float(purchase_value),
        'scrapped_qty': float(scrapped_qty),
        'scrapped_value': float(scrapped_value),
    }

def search_rentals(filters):
    q = ToolRental.query.filter(ToolRental.is_void==False)
    if filters.get('project_id'):
        q = q.filter(ToolRental.project_id == filters['project_id'])
    if filters.get('renter_type'):
        q = q.filter(ToolRental.renter_type == filters['renter_type'])
    if filters.get('status'):
        q = q.filter(ToolRental.status == filters['status'])
    if filters.get('payment_status'):
        q = q.filter(ToolRental.payment_status == filters['payment_status'])
    if filters.get('billing_type'):
        q = q.filter(ToolRental.billing_type == filters['billing_type'])
    if filters.get('date_from'):
        try:
            df = datetime.strptime(filters['date_from'], '%Y-%m-%d').date()
            q = q.filter(ToolRental.rental_date >= df)
        except:
            pass
    if filters.get('date_to'):
        try:
            dt = datetime.strptime(filters['date_to'], '%Y-%m-%d').date()
            q = q.filter(ToolRental.rental_date <= dt)
        except:
            pass
    if filters.get('search_text'):
        txt = f"%{filters['search_text'].lower()}%"
        q = q.filter(or_(
            func.lower(ToolRental.rental_code).like(txt),
            func.lower(func.coalesce(ToolRental.customer_name,'')).like(txt),
            func.lower(func.coalesce(ToolRental.customer_phone,'')).like(txt),
        ))
    if filters.get('tool_id'):
        q = q.join(ToolRentalItem, ToolRentalItem.rental_id == ToolRental.id).filter(ToolRentalItem.tool_id == filters['tool_id'])
    q = q.order_by(ToolRental.rental_date.desc(), ToolRental.id.desc())
    return q.all()

def global_tool_locations(search_tool_id=None, search_project_id=None, search_text=None):
    from hdc.services.tool_tracking import tool_ledger
    ledger = tool_ledger()
    rows_by_tool = {int(r['tool_id']): r for r in ledger['tools']}

    tq = Tool.query.filter(Tool.is_void==False)
    if search_tool_id:
        tq = tq.filter(Tool.id == search_tool_id)
    if search_text:
        txt = f"%{search_text.lower()}%"
        tq = tq.filter(or_(
            func.lower(Tool.name).like(txt),
            func.lower(Tool.tool_code).like(txt),
        ))
    tools = tq.order_by(Tool.name.asc()).all()
    result = []
    for tool in tools:
        # The position ledger is the single source of truth for "where is it
        # now": a tool can sit on two sites at once, which no single "last
        # movement" label can express.
        ledger_row = rows_by_tool.get(int(tool.id))
        last_log = (ToolMovementLog.query
                    .filter_by(tool_id=tool.id)
                    .order_by(ToolMovementLog.timestamp.desc(), ToolMovementLog.id.desc())
                    .first())
        chain_logs = (ToolMovementLog.query
                      .filter_by(tool_id=tool.id)
                      .order_by(ToolMovementLog.timestamp.asc(), ToolMovementLog.id.asc())
                      .all())
        if search_project_id:
            # The current position ledger is authoritative. Looking only at a
            # rental's origin or the tool's last movement misses partial
            # transfers that are still held at a site after later movements.
            has_match = bool(ledger_row and any(
                holding.get('loc_type') == 'own_project'
                and int(holding.get('project_id') or 0) == int(search_project_id)
                for holding in ledger_row.get('holdings', [])
            ))
            if not has_match:
                continue
        if ledger_row:
            current_label = ledger_row['current_label']
            holdings = ledger_row['holdings']
        else:
            current_label = last_log.to_location_label if last_log else STORE_LABEL
            holdings = []
        result.append({
            'tool': tool,
            'current_label': current_label,
            'last_log': last_log,
            'chain': chain_logs,
            'rented_out_qty': tool.rented_out_qty,
            'available_qty': tool.available_qty,
            'owned_qty': ledger_row['owned_qty'] if ledger_row else float(tool.total_quantity or 0),
            'in_store_qty': ledger_row['in_store_qty'] if ledger_row else float(tool.available_qty or 0),
            'holdings': holdings,
        })
    return result

def get_receiving_accounts():
    return (Account.query
            .filter(
                Account.is_void == False,
                func.lower(func.coalesce(Account.status, 'active')) == 'active',
                func.lower(func.coalesce(Account.type, '')).in_(_ACCOUNT_COMPANY_TYPES)
            )
            .order_by(Account.name.asc(), Account.id.asc())
            .all())


#: How many distinct names a combo list will ever offer.  The widget itself
#: caps what it *renders* (``maxItems``); this only bounds the query.
MAX_COMBO_OPTIONS = 300


def known_tool_customers(limit=MAX_COMBO_OPTIONS):
    """Distinct external customer names: past rentals + the Parties module.

    Two sources are merged so the searchable list is complete:

    * names already used on an external tool rental — most-used-first, then
      alphabetical, so the customer the operator probably wants is on top;
    * every ``rental``-type party (sidebar → Parties → External Customers),
      including customers added there *before* the first rental exists.

    Feeds the searchable combo boxes on the create-rental and site-transfer
    forms — the "every name field is searchable" rule.
    """
    from hdc.models.cashflow import CashFlowParty

    rows = (db.session.query(ToolRental.customer_name, func.count(ToolRental.id))
            .filter(ToolRental.is_void == False,
                    ToolRental.customer_name.isnot(None),
                    func.trim(func.coalesce(ToolRental.customer_name, '')) != '')
            .group_by(func.lower(func.trim(ToolRental.customer_name)))
            .order_by(func.count(ToolRental.id).desc(),
                      func.lower(func.trim(ToolRental.customer_name)).asc())
            .limit(limit)
            .all())
    # The grouping is case-insensitive, so keep one spelling per group.
    seen = set()
    names = []
    for raw, _count in rows:
        name = (raw or '').strip()
        key = name.lower()
        if not name or key in seen:
            continue
        seen.add(key)
        names.append(name)
    # Union with the rental-type parties (External Customers on /hdc/parties).
    party_rows = (CashFlowParty.query
                  .filter(CashFlowParty.is_active == True,  # noqa: E712
                          func.lower(func.coalesce(CashFlowParty.party_type, 'other')) == 'rental')
                  .order_by(CashFlowParty.name.asc())
                  .limit(limit)
                  .all())
    for party in party_rows:
        name = (party.name or '').strip()
        key = name.lower()
        if not name or key in seen:
            continue
        seen.add(key)
        names.append(name)
    return names[:limit]


def known_tool_suppliers(limit=MAX_COMBO_OPTIONS):
    """Distinct supplier names tools have already been bought from.

    Union of the tool-purchase ledger's own ``supplier`` text and the materials
    supplier master, because the shop that sells cement also sells grinders.
    Both are read-only lookups: nothing is created or renamed here.
    """
    names = set()
    rows = (db.session.query(ToolPurchase.supplier)
            .filter(ToolPurchase.supplier.isnot(None),
                    func.trim(func.coalesce(ToolPurchase.supplier, '')) != '')
            .limit(limit * 2)
            .all())
    for (raw,) in rows:
        name = (raw or '').strip()
        if name:
            names.add(name)
    try:
        from hdc.models.materials import Supplier
        for (raw,) in (db.session.query(Supplier.name)
                       .filter(Supplier.name.isnot(None)).limit(limit * 2).all()):
            name = (raw or '').strip()
            if name:
                names.add(name)
    except Exception:  # pragma: no cover - supplier master is optional
        pass
    return sorted(names, key=lambda n: (n.lower(), n))[:limit]


def _get_or_create_customer_account(rental):
    from hdc.services.accounts import _account_get_or_create
    if rental.renter_type == 'internal' and rental.project:
        name = _normalize_name_ci(rental.project.client or rental.project.name or f'Project#{rental.project_id}')
        acc_type = 'client'
        auto_source = 'client'
    else:
        name = _normalize_name_ci(rental.customer_name or f'Customer Rental#{rental.id}')
        acc_type = 'client'
        auto_source = 'client'
    if not name:
        name = f'ToolRental#{rental.rental_code}'
    acc = _account_get_or_create(name, acc_type, auto_generated=True, auto_source=auto_source)
    return acc

def post_tool_rental_payment_to_accounts(payment, rental=None, commit=False):
    from hdc.services.accounts import _create_account_transaction, _accounts_default_company_cash
    if not payment or float(payment.amount or 0) <= 0:
        return False, 'Payment amount must be >0', []
    if payment.is_void:
        return False, 'Payment is voided', []
    rental = rental or db.session.get(ToolRental, int(payment.rental_id or 0))
    if not rental:
        return False, 'Rental not found', []

    recv_acc = None
    if payment.received_to_account_id:
        recv_acc = Account.query.get(int(payment.received_to_account_id))
    if not recv_acc:
        recv_acc = _accounts_default_company_cash()
    if not recv_acc or recv_acc.is_void or str(recv_acc.status or 'active').lower() != 'active' or str(recv_acc.type or '').lower() not in _ACCOUNT_COMPANY_TYPES:
        return False, 'Select valid receiving account (Cash/Bank/Company)', []

    cust_acc = _get_or_create_customer_account(rental)
    if not cust_acc:
        return False, 'Unable to resolve customer account', []

    from hdc.models.accounts import AccountTransaction
    existing = (AccountTransaction.query
                .filter(
                    func.lower(func.coalesce(AccountTransaction.source_type,'')).like('tool_rental_payment%'),
                    AccountTransaction.source_id == int(payment.id),
                    AccountTransaction.is_void == False
                )
                .first())
    if existing:
        return True, 'Already posted', [existing]

    pay_date = payment.payment_date or _pkt_today()
    date_str = pay_date.isoformat() if hasattr(pay_date, 'isoformat') else str(pay_date)

    note_raw = payment.notes or ''
    mode_raw = payment.payment_mode or ''
    note_txt = (f'Tool Rental Receipt {rental.rental_code} - {note_raw} [{mode_raw}]').strip()[:400]

    party_name = rental.customer_name if rental.renter_type == 'external' else (rental.project.name if rental.project else 'Internal Site')

    if rental.renter_type == 'external' and (rental.customer_name or '').strip():
        # Every HDC Tools transaction with an outside customer lands in the
        # Parties module (sidebar → Parties) under External Customers, so the
        # customer exists there from the first payment onwards — even for
        # rentals created before that directory existed.
        from hdc.services.cashflow_register import ensure_party
        ensure_party(rental.customer_name, party_type='rental')

    payload = {
        'date': date_str,
        'amount': float(payment.amount or 0),
        'type': 'party_receipt',
        'from_account_id': cust_acc.id,
        'to_account_id': recv_acc.id,
        'executed_by_account_id': cust_acc.id,
        'project_id': rental.project_id,
        'stage_id': rental.stage_id,
        'related_entity_type': 'tool_rental',
        'related_entity_id': rental.id,
        'party_name': party_name,
        'category': 'income',
        'note': note_txt,
        'reference_id': f'tool_rental_payment#{payment.id}',
        'source_type': 'tool_rental_payment',
        'source_id': payment.id,
        'group_id': f'tool-rent-{rental.id}-pay-{payment.id}'
    }

    ok, msg, txns = _create_account_transaction(payload, commit=False)
    if not ok:
        return False, msg, []

    for txn in txns:
        link = ToolRentalAccountTxn(payment_id=payment.id, account_txn_id=txn.id)
        db.session.add(link)
    db.session.flush()
    if commit:
        db.session.commit()
    return True, '', txns

def void_tool_rental_payment_in_accounts(payment_id):
    from hdc.services.accounts import _accounts_set_void_by_source
    _accounts_set_void_by_source('tool_rental_payment', int(payment_id), True)
    return True


# --------------------------------------------------------------------------- #
# discounts — settling a rental partly (or fully) by concession, not by cash
# --------------------------------------------------------------------------- #

def rental_discount_rows(rental_id, include_void=True):
    """Discount rows for one rental, oldest first (``[]`` when none)."""
    q = ToolRentalDiscount.query.filter(ToolRentalDiscount.rental_id == int(rental_id or 0))
    if not include_void:
        q = q.filter(ToolRentalDiscount.is_void == False)  # noqa: E712
    return q.order_by(ToolRentalDiscount.discount_date.asc(),
                      ToolRentalDiscount.id.asc()).all()


def rental_discount_total(rental_id):
    """Total non-void discount granted on a rental."""
    val = (db.session.query(func.coalesce(func.sum(ToolRentalDiscount.amount), 0.0))
           .filter(ToolRentalDiscount.rental_id == int(rental_id or 0),
                   ToolRentalDiscount.is_void == False)  # noqa: E712
           .scalar() or 0.0)
    return float(val)


def discount_reason_options():
    """``(value, label)`` pairs for the discount reason combo."""
    return tuple(TOOL_DISCOUNT_REASONS)


def discount_reason_label(value):
    return TOOL_DISCOUNT_REASON_LABELS.get(
        (value or '').strip().lower(),
        (value or '').replace('_', ' ').title() or 'Other')


def record_tool_rental_discount(rental_id, amount, discount_date=None, reason='goodwill',
                                notes='', payment_id=None, return_id=None,
                                created_by=None, commit=False):
    """Grant a discount on a rental.  Returns ``(row, ok, message)``.

    A discount is the part of the bill the customer never pays: it shrinks the
    outstanding amount exactly like cash does, but no money enters an account.
    It is refused when it would exceed what is still owed, so the rental can
    never read as over-settled.

    ``payment_id`` set means "granted while taking this payment" — voiding that
    payment voids the discount with it.
    """
    rental = db.session.get(ToolRental, int(rental_id or 0))
    if not rental:
        return None, False, 'Rental not found.'
    if rental.is_void:
        return None, False, 'Rental is voided.'
    if (rental.billing_type or '').strip().lower() == 'no_charge':
        return None, False, 'This rental is No Charge — there is nothing to discount.'
    amt = round(max(0.0, float(amount or 0.0)), 2)
    if amt <= 0:
        return None, False, 'Discount amount must be greater than zero.'

    # Refresh the cached totals first: a discount is often granted in the same
    # breath as a payment, and ``rental.total_paid`` would still be stale --
    # which is exactly how cash + concession could together exceed the bill.
    recalc_rental_totals(rental.id)
    db.session.flush()
    already = rental_discount_total(rental.id)
    outstanding = max(0.0, float(rental.total_amount or 0.0)
                      - float(rental.total_paid or 0.0) - already)
    if outstanding <= 0.001:
        return None, False, 'This rental is already fully settled — nothing left to discount.'
    if amt > outstanding + 0.001:
        return None, False, (f'Discount {amt:,.2f} exceeds the outstanding '
                             f'{outstanding:,.2f} PKR on this rental.')

    rsn = (reason or '').strip().lower()
    if rsn not in dict(TOOL_DISCOUNT_REASONS):
        rsn = 'other'

    row = ToolRentalDiscount(
        discount_code=_next_discount_code(),
        rental_id=rental.id,
        payment_id=(int(payment_id) if payment_id else None),
        return_id=(int(return_id) if return_id else None),
        discount_date=(discount_date or _pkt_today()),
        amount=amt,
        reason=rsn,
        notes=(notes or '').strip() or None,
        created_by=created_by,
    )
    db.session.add(row)
    db.session.flush()

    ok_acc, msg_acc, _txns = post_tool_rental_discount_to_accounts(row, rental=rental, commit=False)
    if not ok_acc:
        # Never leave a discount that the ledger does not know about: the
        # rental would read as settled while Accounts still shows the debt.
        db.session.rollback()
        return None, False, (msg_acc or 'Unable to post discount in accounts.')

    recalc_rental_totals(rental.id)
    if commit:
        db.session.commit()
    return row, True, ''


def post_tool_rental_discount_to_accounts(discount, rental=None, commit=False):
    """Mirror a granted discount into the unified ledger.

    The customer's own account gives up the amount (``from_account_id``) and
    nothing is received anywhere, which is why the row carries its own
    ``discount_given`` type / ``discount`` category: it moves the receivable
    without ever touching a cash or bank balance.
    """
    from hdc.services.accounts import _create_account_transaction
    if not discount or float(discount.amount or 0) <= 0:
        return False, 'Discount amount must be >0', []
    if discount.is_void:
        return False, 'Discount is voided', []

    rental = rental or db.session.get(ToolRental, int(discount.rental_id or 0))
    if not rental:
        return False, 'Rental not found', []

    from hdc.models.accounts import AccountTransaction
    existing = (AccountTransaction.query
                .filter(
                    func.lower(func.coalesce(AccountTransaction.source_type, '')).like('tool_rental_discount%'),
                    AccountTransaction.source_id == int(discount.id),
                    AccountTransaction.is_void == False  # noqa: E712
                )
                .first())
    if existing:
        return True, 'Already posted', [existing]

    cust_acc = _get_or_create_customer_account(rental)
    if not cust_acc:
        return False, 'Unable to resolve customer account', []

    disc_date = discount.discount_date or _pkt_today()
    date_str = disc_date.isoformat() if hasattr(disc_date, 'isoformat') else str(disc_date)

    party_name = (rental.customer_name if rental.renter_type == 'external'
                  else (rental.project.name if rental.project else 'Internal Site'))

    if rental.renter_type == 'external' and (rental.customer_name or '').strip():
        from hdc.services.cashflow_register import ensure_party
        ensure_party(rental.customer_name, party_type='rental')

    reason_txt = discount_reason_label(discount.reason)
    note_txt = (f'Tool Rental Discount {rental.rental_code} - {reason_txt} '
                f'({discount.discount_code})').strip()[:400]
    if discount.notes:
        note_txt = (note_txt + ' - ' + str(discount.notes))[:400]

    payload = {
        'date': date_str,
        'amount': float(discount.amount or 0),
        'type': 'discount_given',
        'from_account_id': cust_acc.id,
        'to_account_id': None,
        'executed_by_account_id': cust_acc.id,
        'project_id': rental.project_id,
        'stage_id': rental.stage_id,
        'related_entity_type': 'tool_rental',
        'related_entity_id': rental.id,
        'party_name': party_name,
        'category': 'discount',
        'note': note_txt,
        'reference_id': f'tool_rental_discount#{discount.id}',
        'source_type': 'tool_rental_discount',
        'source_id': discount.id,
        'group_id': f'tool-rent-{rental.id}-disc-{discount.id}',
    }

    ok, msg, txns = _create_account_transaction(payload, commit=False)
    if not ok:
        return False, msg, []
    db.session.flush()
    if commit:
        db.session.commit()
    return True, '', txns


def void_tool_rental_discount(discount_id, reason='', commit=False):
    """Void a discount and put the amount back on what the customer owes."""
    row = db.session.get(ToolRentalDiscount, int(discount_id or 0))
    if not row:
        return None, False, 'Discount not found.'
    if row.is_void:
        return None, False, 'Discount is already voided.'
    row.is_void = True
    row.void_reason = (reason or '').strip() or 'Voided by user'
    row.voided_at = _pkt_now_naive()
    void_tool_rental_discount_in_accounts(row.id)
    recalc_rental_totals(row.rental_id)
    if commit:
        db.session.commit()
    return row, True, ''


def void_tool_rental_discount_in_accounts(discount_id):
    from hdc.services.accounts import _accounts_set_void_by_source
    _accounts_set_void_by_source('tool_rental_discount', int(discount_id), True)
    return True


def void_discounts_for_payment(payment_id, reason=''):
    """Void every discount granted alongside a payment (payment voided)."""
    rows = (ToolRentalDiscount.query
            .filter(ToolRentalDiscount.payment_id == int(payment_id or 0),
                    ToolRentalDiscount.is_void == False)  # noqa: E712
            .all())
    for row in rows:
        row.is_void = True
        row.void_reason = (reason or '').strip() or 'Voided with its payment'
        row.voided_at = _pkt_now_naive()
        void_tool_rental_discount_in_accounts(row.id)
    return len(rows)


# --------------------------------------------------------------------------- #
# Tool Serial Number Management — individual piece tracking
# --------------------------------------------------------------------------- #

def _serial_base_name(tool, prefix=None):
    """Human-readable prefix for serial markings, e.g. 'Shovel' -> 'Shovel No 1'."""
    if prefix and str(prefix).strip():
        return str(prefix).strip()
    name = (tool.name if tool else '') or ''
    name = name.strip()
    if name:
        return name
    code = (tool.tool_code if tool else '') or ''
    return code.strip() or 'Tool'


def _extract_serial_seq_number(serial_number, base_name=''):
    """Extract the trailing sequence integer from a serial marking like
    'Shovel No 5', 'Shovel No. 6', 'TOOL-0001-005', etc."""
    import re
    s = str(serial_number or '').strip()
    if not s:
        return 0
    if base_name:
        m = re.match(rf'^{re.escape(base_name)}\s*(?:No\.?|#|-)?\s*(\d+)\s*$', s, flags=re.IGNORECASE)
        if m:
            return int(m.group(1))
    m = re.search(r'(?:No\.?\s*|#\s*|-\s*0*)(\d+)\s*$', s, flags=re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r'(\d+)\s*$', s)
    if m:
        return int(m.group(1))
    return 0


def _generate_serial_numbers(tool, count, start_no=None, prefix=None):
    """Generate ``count`` unique human-readable serial markings for ``tool``
    such as 'Shovel No 1', 'Shovel No 2', ..., 'Shovel No 5', 'Shovel No 6'."""
    import re
    count = max(0, int(count or 0))
    if count <= 0:
        return []
    base = _serial_base_name(tool, prefix=prefix)
    existing_rows = ToolSerial.query.filter_by(tool_id=tool.id).all()
    existing_names_ci = {str(r.serial_number or '').strip().lower() for r in existing_rows}

    # Special case: if the tool name itself is already a single numbered marking
    # like "Shovel No 5" with count == 1 and no serials yet, use "Shovel No 5".
    if (count == 1 and not existing_rows and not start_no and not prefix
            and re.search(r'\bNo\.?\s*\d+\s*$', base, flags=re.IGNORECASE)):
        return [base]

    if start_no is not None and str(start_no).strip().isdigit() and int(start_no) > 0:
        next_num = int(start_no)
    else:
        max_num = 0
        for r in existing_rows:
            n = _extract_serial_seq_number(r.serial_number, base_name=base)
            if n > max_num:
                max_num = n
        next_num = max(max_num + 1, len(existing_rows) + 1)

    generated = []
    while len(generated) < count:
        candidate = f"{base} No {next_num}"
        if candidate.lower() not in existing_names_ci:
            generated.append(candidate)
            existing_names_ci.add(candidate.lower())
        next_num += 1
    return generated


def _next_serial_number(tool, start_no=None, prefix=None):
    """Generate the next single serial number for a tool (e.g. 'Shovel No 5')."""
    nums = _generate_serial_numbers(tool, 1, start_no=start_no, prefix=prefix)
    return nums[0] if nums else f"{_serial_base_name(tool, prefix=prefix)} No 1"


def parse_custom_serial_list(raw):
    """Parse comma- or newline-separated serial markings into a clean list."""
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        items = [str(x).strip() for x in raw if str(x).strip()]
    else:
        import re
        items = [x.strip() for x in re.split(r'[\n,;]+', str(raw)) if x.strip()]
    seen = set()
    out = []
    for item in items:
        key = item.lower()
        if key not in seen:
            seen.add(key)
            out.append(item[:80])
    return out


def create_tool_serials(tool_id, qty=None, serial_numbers=None, created_by=None,
                        start_no=None, prefix=None, from_label=None,
                        notes_prefix=None, commit=False):
    """Create individual serial-numbered pieces for a tool (e.g. 'Shovel No 5').

    Args:
        tool_id: The tool ID.
        qty: Number of serials to create.
        serial_numbers: Optional list (or comma/newline string) of pre-defined
            serial markings. If None, auto-generates markings like
            'Shovel No 1', 'Shovel No 2', etc.
        created_by: User ID who created the serials.
        start_no: Optional starting number (e.g. 5 -> 'Shovel No 5', 'Shovel No 6').
        prefix: Optional custom prefix instead of tool.name.

    Returns:
        (success, message, [serial_ids])
    """
    tool = db.session.get(Tool, int(tool_id or 0))
    if not tool:
        return False, 'Tool not found.', []

    custom_list = parse_custom_serial_list(serial_numbers)
    if custom_list:
        if qty is not None and int(round(float(qty or 0))) > 0 and len(custom_list) < int(round(float(qty))):
            # Fill any remaining count using auto-generated markings
            extra_needed = int(round(float(qty))) - len(custom_list)
            auto_extra = _generate_serial_numbers(tool, extra_needed, start_no=start_no, prefix=prefix)
            for cand in auto_extra:
                if cand.lower() not in {c.lower() for c in custom_list}:
                    custom_list.append(cand)
        target_numbers = custom_list
    else:
        count = max(0, int(round(float(qty or 0))))
        if count <= 0:
            return False, 'Quantity must be at least 1.', []
        target_numbers = _generate_serial_numbers(tool, count, start_no=start_no, prefix=prefix)

    created_ids = []
    for sn in target_numbers:
        existing = ToolSerial.query.filter(
            ToolSerial.tool_id == tool.id,
            func.lower(ToolSerial.serial_number) == sn.lower()
        ).first()
        if existing:
            if getattr(existing, 'is_scrapped', False):
                continue
            continue  # Skip duplicates silently
        serial = ToolSerial(
            serial_number=sn,
            tool_id=tool.id,
            is_in_store=True,
            is_scrapped=False,
            current_location_label=STORE_LABEL,
            status='in_store',
            condition=tool.condition if tool.condition in ('good', 'maintenance', 'damaged') else 'good',
            created_by=created_by,
        )
        db.session.add(serial)
        db.session.flush()
        created_ids.append(serial.id)

        note_text = f'Serial {sn} added to inventory'
        if notes_prefix:
            note_text = f'{notes_prefix}: {sn}'
        create_serial_movement(
            serial_id=serial.id,
            movement_type='purchase_in',
            from_label=from_label or SUPPLIER_LABEL,
            to_label=STORE_LABEL,
            notes=note_text,
            created_by=created_by,
        )

    if commit:
        db.session.commit()
    return True, f'Created {len(created_ids)} serial(s) for {tool.name}.', created_ids


def ensure_tool_serials(tool=None, tool_id=None, created_by=None, commit=False):
    """Ensure a tool's active ToolSerial markings match its owned quantity and
    live rental holdings.

    This guarantees that even tools created before serial tracking (or directly
    via ORM in tests/imports) always have complete serial markings (e.g.
    'Shovel No 1' .. 'Shovel No N') with accurate in-store vs out-of-store
    status.
    """
    if tool is None and tool_id:
        tool = db.session.get(Tool, int(tool_id or 0))
    if not tool or tool.is_void:
        return []

    owned_int = max(0, min(500, int(round(float(tool.total_quantity or 0.0)))))
    all_serials = ToolSerial.query.filter_by(tool_id=tool.id).all()
    active_serials = [s for s in all_serials if not getattr(s, 'is_scrapped', False) and s.status != 'scrapped']

    # 1. Backfill missing serial rows up to owned_int
    if len(active_serials) < owned_int:
        missing = owned_int - len(active_serials)
        create_tool_serials(
            tool_id=tool.id,
            qty=missing,
            created_by=created_by,
            notes_prefix='Auto-synced inventory serial',
            commit=False,
        )
        all_serials = ToolSerial.query.filter_by(tool_id=tool.id).all()
        active_serials = [s for s in all_serials if not getattr(s, 'is_scrapped', False) and s.status != 'scrapped']

    active_serials.sort(key=lambda s: s.sort_key)

    # 2. Reconcile serials against active (non-void) rental items for this tool
    open_items = (
        ToolRentalItem.query
        .join(ToolRental, ToolRental.id == ToolRentalItem.rental_id)
        .filter(
            ToolRentalItem.tool_id == tool.id,
            ToolRental.is_void == False,  # noqa: E712
            ToolRentalItem.qty_pending > EPS,
        )
        .order_by(ToolRental.rental_date.asc(), ToolRental.id.asc(), ToolRentalItem.id.asc())
        .all()
    )
    open_rental_ids = {item.rental_id for item in open_items}

    # Any serial marked out on a rental that is now closed/voided or has 0 pending
    # returns to store automatically.
    for s in active_serials:
        if not s.is_in_store and s.current_rental_id and s.current_rental_id not in open_rental_ids:
            s.is_in_store = True
            s.current_rental_id = None
            s.current_rental_item_id = None
            s.status = 'in_store'
            s.current_location_label = STORE_LABEL

    # For each open rental item, make sure the number of serials attached to
    # that rental matches qty_pending.
    in_store_pool = [s for s in active_serials if s.is_in_store]
    for item in open_items:
        rental = item.rental
        needed = max(0, int(round(float(item.qty_pending or 0.0))))
        attached = [
            s for s in active_serials
            if not s.is_in_store and s.current_rental_id == rental.id
            and (s.current_rental_item_id in (None, item.id))
        ]
        # Attach item_id if unset
        for s in attached[:needed]:
            if s.current_rental_item_id is None:
                s.current_rental_item_id = item.id
            if not s.current_location_label or s.current_location_label == STORE_LABEL:
                s.current_location_label = rental.current_location_label or 'Rented Out'

        if len(attached) > needed:
            # Return excess serials to store
            for s in attached[needed:]:
                s.is_in_store = True
                s.current_rental_id = None
                s.current_rental_item_id = None
                s.status = 'in_store'
                s.current_location_label = STORE_LABEL
                in_store_pool.append(s)
        elif len(attached) < needed:
            short = needed - len(attached)
            while short > 0 and in_store_pool:
                s = in_store_pool.pop(0)
                s.is_in_store = False
                s.current_rental_id = rental.id
                s.current_rental_item_id = item.id
                s.status = 'rented'
                s.current_location_label = rental.current_location_label or 'Rented Out'
                short -= 1

    db.session.flush()
    if commit:
        db.session.commit()
    return sorted(active_serials, key=lambda s: s.sort_key)


def ensure_all_tools_serials(tools=None, created_by=None, commit=False):
    """Ensure serial markings are synced for all active tools."""
    if tools is None:
        tools = Tool.query.filter(Tool.is_void == False).all()  # noqa: E712
    for t in tools:
        ensure_tool_serials(tool=t, created_by=created_by, commit=False)
    if commit:
        db.session.commit()
    return tools


def get_available_serials(tool_id, rental_id=None):
    """Get serials that are currently in store (available for new rental)."""
    tool = db.session.get(Tool, int(tool_id or 0))
    if tool:
        ensure_tool_serials(tool=tool, commit=False)
    query = ToolSerial.query.filter(
        ToolSerial.tool_id == int(tool_id or 0),
        ToolSerial.is_in_store == True,  # noqa: E712
        or_(ToolSerial.is_scrapped == False, ToolSerial.is_scrapped.is_(None)),  # noqa: E712
        ToolSerial.status != 'scrapped',
    )
    if rental_id:
        query = query.filter(or_(ToolSerial.current_rental_id != int(rental_id),
                                 ToolSerial.current_rental_id.is_(None)))
    rows = query.all()
    return sorted(rows, key=lambda s: s.sort_key)


def get_out_of_store_serials(tool_id=None, rental_id=None, rental_item_id=None, location_label=None):
    """Get serials that are currently OUT of store (at a site or customer),
    suitable for Transfer Tools or Return Tools."""
    if tool_id:
        tool = db.session.get(Tool, int(tool_id or 0))
        if tool:
            ensure_tool_serials(tool=tool, commit=False)
    else:
        ensure_all_tools_serials(commit=False)

    query = (
        ToolSerial.query
        .join(Tool, Tool.id == ToolSerial.tool_id)
        .filter(
            Tool.is_void == False,  # noqa: E712
            ToolSerial.is_in_store == False,  # noqa: E712
            or_(ToolSerial.is_scrapped == False, ToolSerial.is_scrapped.is_(None)),  # noqa: E712
            ToolSerial.status != 'scrapped',
        )
    )
    if tool_id:
        query = query.filter(ToolSerial.tool_id == int(tool_id))
    if rental_id:
        query = query.filter(ToolSerial.current_rental_id == int(rental_id))
    if rental_item_id:
        query = query.filter(or_(
            ToolSerial.current_rental_item_id == int(rental_item_id),
            ToolSerial.current_rental_item_id.is_(None),
        ))
    rows = query.all()
    if location_label:
        loc_norm = location_label.strip().lower()
        filtered = [r for r in rows if (r.current_location_label or '').strip().lower() == loc_norm]
        if filtered:
            rows = filtered
    return sorted(rows, key=lambda s: (s.tool_id, s.sort_key))


def get_serial_status(serial_id):
    """Get detailed status of a single serial-numbered piece.

    Returns a dict with full status info including movement history.
    """
    serial = db.session.get(ToolSerial, int(serial_id or 0))
    if not serial:
        return None

    movements = (ToolSerialMovement.query
                 .filter_by(serial_id=serial.id)
                 .order_by(ToolSerialMovement.timestamp.desc(), ToolSerialMovement.id.desc())
                 .limit(50)
                 .all())

    current_rental = None
    if serial.current_rental_id:
        current_rental = db.session.get(ToolRental, serial.current_rental_id)

    return {
        'serial': serial,
        'movements': movements,
        'current_rental': current_rental,
        'status': serial.status,
        'status_label': serial.status_label,
        'is_in_store': serial.is_in_store,
        'condition': serial.condition,
        'notes': serial.notes,
        'tool': serial.tool,
    }


def get_all_tool_serials_summary(tool_id=None):
    """Get a summary of all serials across tools for the inventory view."""
    q = Tool.query.filter(Tool.is_void == False)  # noqa: E712
    if tool_id:
        q = q.filter(Tool.id == int(tool_id))
    tools = q.order_by(Tool.name.asc()).all()
    ensure_all_tools_serials(tools=tools, commit=False)
    result = []

    for tool in tools:
        serials = tool.active_serials
        in_store = [s for s in serials if s.is_in_store]
        rented = [s for s in serials if not s.is_in_store]
        maintenance = [s for s in serials if s.condition == 'maintenance']
        damaged = [s for s in serials if s.condition == 'damaged']
        lost = [s for s in serials if s.condition == 'lost']

        result.append({
            'tool': tool,
            'tool_id': tool.id,
            'tool_name': tool.name,
            'tool_code': tool.tool_code,
            'category_id': tool.category_id,
            'category_name': tool.category.name if tool.category else tool.name,
            'total_serials': len(serials),
            'in_store': len(in_store),
            'rented': len(rented),
            'maintenance': len(maintenance),
            'damaged': len(damaged),
            'lost': len(lost),
            'serials': serials,
            'in_store_serials': in_store,
            'rented_serials': rented,
        })

    return result


def rename_tool_serial(serial_id, new_serial_number, condition=None, notes=None, updated_by=None, commit=True):
    """Rename or update an individual serial marking (e.g. 'Shovel No 5')."""
    serial = db.session.get(ToolSerial, int(serial_id or 0))
    if not serial:
        return False, 'Serial not found.', None
    new_sn = (new_serial_number or '').strip()[:80]
    if not new_sn:
        return False, 'Serial marking cannot be empty.', None
    dup = ToolSerial.query.filter(
        ToolSerial.tool_id == serial.tool_id,
        ToolSerial.id != serial.id,
        func.lower(ToolSerial.serial_number) == new_sn.lower(),
    ).first()
    if dup:
        return False, f'Serial marking "{new_sn}" already exists for this tool.', None

    old_sn = serial.serial_number
    serial.serial_number = new_sn
    if condition and condition in ('good', 'maintenance', 'damaged', 'lost'):
        serial.condition = condition
    if notes is not None:
        serial.notes = (notes or '').strip()[:300]
    serial.updated_at = _pkt_now_naive()
    serial.updated_by = updated_by
    if old_sn != new_sn:
        create_serial_movement(
            serial_id=serial.id,
            movement_type='status_change',
            from_label=serial.current_location_label or STORE_LABEL,
            to_label=serial.current_location_label or STORE_LABEL,
            notes=f'Serial marking renamed from "{old_sn}" to "{new_sn}"',
            created_by=updated_by,
        )
    if commit:
        db.session.commit()
    return True, f'Updated serial marking to "{new_sn}".', serial


def update_serial_status(serial_id, status, location_label=None, rental_id=None, created_by=None):
    """Update the status of a serial-numbered piece."""
    serial = db.session.get(ToolSerial, int(serial_id or 0))
    if not serial:
        return False, 'Serial not found.'

    old_status = serial.status
    old_location = serial.current_location_label or STORE_LABEL
    serial.status = status
    if location_label:
        serial.current_location_label = location_label
    if rental_id:
        serial.current_rental_id = rental_id
        serial.is_in_store = False
    else:
        serial.is_in_store = (status == 'in_store')
        serial.current_rental_id = None
        serial.current_rental_item_id = None
        if status == 'in_store':
            serial.current_location_label = STORE_LABEL

    serial.updated_at = _pkt_now_naive()
    serial.updated_by = created_by

    to_label = serial.current_location_label or STORE_LABEL
    if old_status != status or old_location != to_label:
        create_serial_movement(
            serial_id=serial.id,
            movement_type=f'status_change_{status}',
            from_label=old_location,
            to_label=to_label,
            notes=f'Status changed from {old_status} to {status}',
            created_by=created_by,
        )

    db.session.commit()
    return True, f'Serial status updated to {status}.'


def create_serial_movement(serial_id, movement_type, from_label, to_label, notes='', rental_id=None, transfer_id=None, return_id=None, created_by=None):
    """Log a movement for a serial-numbered piece."""
    serial = db.session.get(ToolSerial, int(serial_id or 0))
    if not serial:
        return None

    movement = ToolSerialMovement(
        serial_id=serial.id,
        rental_id=rental_id,
        transfer_id=transfer_id,
        return_id=return_id,
        movement_type=movement_type,
        from_location_label=from_label,
        to_location_label=to_label,
        notes=notes,
        created_by=created_by,
    )
    db.session.add(movement)
    db.session.flush()
    return movement


def get_serials_for_rental(rental_id):
    """Get all serials currently assigned to a rental."""
    rental = db.session.get(ToolRental, int(rental_id or 0))
    if not rental:
        return []

    for item in rental.items:
        if item.tool:
            ensure_tool_serials(tool=item.tool, commit=False)

    serials = (
        ToolSerial.query
        .filter(
            ToolSerial.current_rental_id == rental.id,
            ToolSerial.is_in_store == False,  # noqa: E712
            or_(ToolSerial.is_scrapped == False, ToolSerial.is_scrapped.is_(None)),  # noqa: E712
        )
        .all()
    )
    serials.sort(key=lambda s: (s.tool_id, s.sort_key))
    result = []
    for serial in serials:
        result.append({
            'serial': serial,
            'serial_id': serial.id,
            'tool_id': serial.tool_id,
            'rental_item_id': serial.current_rental_item_id,
            'serial_number': serial.serial_number,
            'tool_name': serial.tool.name if serial.tool else '-',
            'tool_code': serial.tool.tool_code if serial.tool else '-',
            'category_name': (serial.tool.category.name if serial.tool and serial.tool.category else (serial.tool.name if serial.tool else '-')),
            'status': serial.status,
            'status_label': serial.status_label,
            'condition': serial.condition,
            'notes': serial.notes,
            'location': serial.current_location_label or rental.current_location_label,
        })
    return result


def get_serials_in_store_summary(category_id=None, tool_id=None):
    """Get all serials currently IN STORE for the New Rental data-driven picker."""
    q = Tool.query.filter(Tool.is_void == False)  # noqa: E712
    if category_id:
        q = q.filter(Tool.category_id == int(category_id))
    if tool_id:
        q = q.filter(Tool.id == int(tool_id))
    tools = q.order_by(Tool.name.asc()).all()
    ensure_all_tools_serials(tools=tools, commit=False)

    result = []
    for tool in tools:
        cat_name = tool.category.name if tool.category else tool.name
        for serial in tool.in_store_serials:
            result.append({
                'serial_id': serial.id,
                'tool_id': serial.tool_id,
                'tool_name': tool.name,
                'tool_code': tool.tool_code,
                'category_id': tool.category_id,
                'category_name': cat_name,
                'rate': float(tool.rental_rate_per_day or 0.0),
                'serial_number': serial.serial_number,
                'label': f"{serial.serial_number} ({tool.tool_code})",
                'status': serial.status,
                'condition': serial.condition,
                'serial': serial,
            })
    return result


def get_serials_out_of_store_summary(category_id=None, tool_id=None, rental_id=None, holder=None):
    """Get all serials currently OUT OF STORE (at a site/customer) for Transfer Tools."""
    q = Tool.query.filter(Tool.is_void == False)  # noqa: E712
    if category_id:
        q = q.filter(Tool.category_id == int(category_id))
    if tool_id:
        q = q.filter(Tool.id == int(tool_id))
    tools = q.order_by(Tool.name.asc()).all()
    ensure_all_tools_serials(tools=tools, commit=False)

    result = []
    for tool in tools:
        cat_name = tool.category.name if tool.category else tool.name
        for serial in tool.out_serials:
            if rental_id and serial.current_rental_id != int(rental_id):
                continue
            rental = serial.current_rental
            holder_label = (
                serial.current_location_label
                or (rental.current_location_label if rental else '')
                or 'Out of Store'
            )
            if holder and holder.strip().lower() not in holder_label.lower():
                continue
            result.append({
                'serial_id': serial.id,
                'tool_id': serial.tool_id,
                'tool_name': tool.name,
                'tool_code': tool.tool_code,
                'category_id': tool.category_id,
                'category_name': cat_name,
                'serial_number': serial.serial_number,
                'rental_id': serial.current_rental_id,
                'rental_item_id': serial.current_rental_item_id,
                'rental_code': rental.rental_code if rental else '',
                'holder': holder_label,
                'renter_type': rental.renter_type if rental else 'internal',
                'rate': float(tool.rental_rate_per_day or 0.0),
                'status': serial.status,
                'condition': serial.condition,
                'serial': serial,
            })
    return result


def get_serials_by_tool_for_rental(tool_id):
    """Get all in-store serials for a specific tool (for New Rental)."""
    return get_available_serials(tool_id)


def assign_serials_to_rental(rental_id, serial_ids, rental_item_id=None, location_label=None, notes='', created_by=None, commit=True):
    """Assign serial-numbered pieces from the store to a rental."""
    rental = db.session.get(ToolRental, int(rental_id or 0))
    if not rental:
        return False, 'Rental not found.', 0

    dest_label = location_label or rental.current_location_label or 'Rented Out'
    assigned = 0
    for serial_id in serial_ids:
        serial = db.session.get(ToolSerial, int(serial_id or 0))
        if not serial or getattr(serial, 'is_scrapped', False):
            continue
        if not serial.is_in_store and serial.current_rental_id != rental.id:
            continue

        serial.is_in_store = False
        serial.current_rental_id = rental.id
        if rental_item_id:
            serial.current_rental_item_id = int(rental_item_id)
        serial.status = 'rented'
        serial.current_location_label = dest_label
        serial.updated_at = _pkt_now_naive()
        serial.updated_by = created_by

        create_serial_movement(
            serial_id=serial.id,
            movement_type='rental_out',
            from_label=STORE_LABEL,
            to_label=dest_label,
            notes=notes or f'Rented out on {rental.rental_code} ({serial.serial_number})',
            rental_id=rental.id,
            created_by=created_by,
        )
        assigned += 1

    if commit:
        db.session.commit()
    return True, f'Assigned {assigned} serial(s) to rental {rental.rental_code}.', assigned


def return_serials_from_rental(rental_id, serial_ids, return_id=None, condition=None, notes='', created_by=None, commit=True):
    """Return serial-numbered pieces from a rental back to the store."""
    rental = db.session.get(ToolRental, int(rental_id or 0))
    if not rental:
        return False, 'Rental not found.', 0

    returned = 0
    for serial_id in serial_ids:
        serial = db.session.get(ToolSerial, int(serial_id or 0))
        if not serial:
            continue
        if serial.is_in_store:
            continue

        old_location = serial.current_location_label or rental.current_location_label or 'Site / Customer'
        serial.is_in_store = True
        serial.current_rental_id = None
        serial.current_rental_item_id = None
        if condition and condition in ('good', 'maintenance', 'damaged', 'lost'):
            serial.condition = condition
        serial.status = 'in_store' if serial.condition == 'good' else serial.condition
        serial.current_location_label = STORE_LABEL
        serial.updated_at = _pkt_now_naive()
        serial.updated_by = created_by

        create_serial_movement(
            serial_id=serial.id,
            movement_type='return_in',
            from_label=old_location,
            to_label=STORE_LABEL,
            notes=notes or (f'Returned from rental {rental.rental_code} ({serial.serial_number})'
                            + (f', return #{return_id}' if return_id else '')),
            rental_id=rental.id,
            return_id=return_id,
            created_by=created_by,
        )
        returned += 1

    if commit:
        db.session.commit()
    return True, f'Returned {returned} serial(s) to store.', returned


def transfer_serials(serial_ids, from_label, to_label, transfer_id=None, rental_id=None,
                     rental_item_id=None, movement_type='site_transfer', notes='',
                     created_by=None, commit=True):
    """Transfer out-of-store serial-numbered pieces between sites/customers."""
    transferred = 0
    for serial_id in serial_ids:
        serial = db.session.get(ToolSerial, int(serial_id or 0))
        if not serial or getattr(serial, 'is_scrapped', False):
            continue

        old_location = serial.current_location_label or from_label or 'Site / Customer'
        serial.is_in_store = False
        serial.current_location_label = to_label
        if rental_id is not None:
            serial.current_rental_id = int(rental_id)
        if rental_item_id is not None:
            serial.current_rental_item_id = int(rental_item_id)
        serial.status = 'rented'
        serial.updated_at = _pkt_now_naive()
        serial.updated_by = created_by

        create_serial_movement(
            serial_id=serial.id,
            movement_type=movement_type or 'site_transfer',
            from_label=old_location,
            to_label=to_label,
            notes=notes or f'Transferred {serial.serial_number} from {old_location} to {to_label}',
            transfer_id=transfer_id,
            rental_id=rental_id or serial.current_rental_id,
            created_by=created_by,
        )
        transferred += 1

    if commit:
        db.session.commit()
    return True, f'Transferred {transferred} serial(s) to {to_label}.', transferred
