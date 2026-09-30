"""Business logic for HDC Tool Rental."""

from datetime import date, datetime
from sqlalchemy import func, or_

from hdc.extensions import db
from hdc.models.accounts import Account
from hdc.models.tool_rental import (
    MOVEMENT_PURCHASE_IN, MOVEMENT_SCRAP_OUT, TOOL_SCRAP_REASONS, Tool, ToolCategory,
    ToolMovementLog, ToolPurchase, ToolRental, ToolRentalAccountTxn, ToolRentalItem,
    ToolRentalPayment, ToolRentalReturn, ToolRentalReturnItem, ToolRentalTransfer, ToolScrap
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
                         created_by=None, commit=True):
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

    old_qty = _round2(tool.total_quantity)
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
    if commit:
        db.session.commit()
    return True, '', purchase


def tool_available_for_integrity(tool):
    query = db.session.query(func.coalesce(func.sum(ToolRentalItem.qty_pending), 0.0)).filter(
        ToolRentalItem.tool_id == tool.id)
    return max(0.0, float(tool.total_quantity or 0) - float(integrity_query(query).scalar() or 0))


def record_tool_scrap(tool_id, qty, reason='damaged', scrap_date=None, reference='',
                      notes='', created_by=None, commit=True):
    """Throw away / lose / sell as scrap: -qty on total_quantity + audit row.

    Only what is sitting in the store can be scrapped - rented-out pieces have
    to come back first, otherwise the owned-vs-located balance would break.
    """
    tool = db.session.get(Tool, int(tool_id or 0))
    if not tool:
        return False, 'Tool not found.', None
    if tool.is_void:
        return False, f'{tool.name} is archived. Un-archive it before recording scrap.', None

    qty = _round2(qty)
    if qty <= EPS:
        return False, 'Scrap quantity must be greater than 0.', None

    available = _round2(tool_available_for_integrity(tool))
    if qty > available + EPS:
        return False, integrity_message('Insufficient available tool stock. Return rented tools first.',
                       f'Only {available:g} {tool.unit} of {tool.name} is in the store '
                       f'({tool.rented_out_qty:g} is rented out) - return it first.'), None

    reason_key = (reason or 'damaged').strip().lower()
    if reason_key not in {k for k, _ in TOOL_SCRAP_REASONS}:
        reason_key = 'other'

    unit_cost = _round2(tool.purchase_cost)
    value = _round2(qty * unit_cost)
    when = _parse_date(scrap_date)

    scrap = ToolScrap(
        scrap_code=_next_scrap_code(),
        tool_id=tool.id,
        scrap_date=when,
        qty=qty,
        reason=reason_key,
        unit_cost=unit_cost,
        value_written_off=value,
        reference=(reference or '').strip()[:120],
        notes=(notes or '').strip()[:300],
        created_by=created_by,
    )
    db.session.add(scrap)
    db.session.flush()

    old_qty = _round2(tool.total_quantity)
    tool.total_quantity = max(0.0, _round2(old_qty - qty))
    tool.updated_at = _pkt_now_naive()

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

    rental.total_rented_qty = total_rented
    rental.total_returned_qty = total_returned
    rental.total_amount = total_amount if total_amount>0 else float(rental.total_amount or 0)
    rental.total_paid = float(total_paid)

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
        pending_amt = max(0.0, float(rental.total_amount or 0) - float(total_paid))
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
            has_match = False
            if last_log and last_log.rental_id:
                rental = db.session.get(ToolRental, last_log.rental_id)
                if rental and int(rental.project_id or 0) == int(search_project_id):
                    has_match = True
            if not has_match:
                exists = (db.session.query(ToolRentalItem.id)
                          .join(ToolRental, ToolRental.id == ToolRentalItem.rental_id)
                          .filter(ToolRentalItem.tool_id == tool.id,
                                  ToolRental.project_id == search_project_id,
                                  ToolRentalItem.qty_pending > 0)
                          .first())
                if exists:
                    has_match = True
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
