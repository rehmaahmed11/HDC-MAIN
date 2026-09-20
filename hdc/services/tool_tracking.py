"""Where is every HDC tool right now? — one source of truth for the Tools dashboard.

The tool rental module already stores *events* (rental out, return in, site
transfer).  This service turns those events into a **position**: for every tool
type, how many pieces are in the store, how many are on HDC's own projects, how
many are with outside customers, and whether the numbers still add up to what we
own.

Rules (deliberately boring, so the numbers always reconcile):

1. A tool is ``out`` while its rental line still has ``qty_pending > 0``.
2. The current site of a pending line is the destination of its latest
   transfer; if it was never transferred it is the rental's original
   site/customer.  Transfers recorded before per-tool split existed have no
   ``ToolRentalTransferItem`` rows — they are treated as "the whole pending
   line moved", which is what the old movement log already assumed.
3. Everything owned but not out is ``in_store`` (Warehouse / Store).
4. Pieces marked damaged / lost / under maintenance are counted separately but
   still belong to the store side of the balance, so
   ``owned == in_store + own_projects + customers`` always holds.  When it does
   not (bad legacy data, manual stock edit while tools were out) the row is
   flagged ``unaccounted`` instead of silently lying.
5. A transfer whose destination is the store is really a return, so the route
   records it through :func:`apply_transfer` + the normal return path; that
   keeps rule 3 true.

Everything here is read-only except :func:`allocate_transfer_qty` /
:func:`record_transfer_items`, which the transfer route uses to persist the
per-tool split.
"""

from datetime import date

from sqlalchemy import func, or_

from hdc.extensions import db
from hdc.models.projects import Project, Stage
from hdc.models.tool_rental import (
    Tool, ToolMovementLog, ToolRental, ToolRentalItem, ToolRentalReturn,
    ToolRentalReturnItem, ToolRentalTransfer, ToolRentalTransferItem
)
from hdc.utils.dates import _pkt_now_naive, _pkt_today
from hdc.utils.format import _flt

WAREHOUSE_LABEL = 'Warehouse / Store'
EPS = 0.001

LOC_STORE = 'store'
LOC_OWN_PROJECT = 'own_project'
LOC_CUSTOMER = 'customer'

# Out-of-stock / unusable conditions reported separately from "available".
_BAD_CONDITIONS = ('damaged', 'lost')

# A tool out longer than this without a return is worth a look.
LONG_OUT_DAYS = 30


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _round_qty(value):
    """Quantities are pieces/metres — 2 decimals is plenty and kills float dust."""
    return round(float(value or 0.0) + 0.0, 2)


def _days_between(start, end=None):
    if not start:
        return 0
    end = end or _pkt_today()
    try:
        return max(0, (end - start).days)
    except Exception:
        return 0


def _location_key(loc_type, project_id=None, stage_id=None, customer_name=None):
    return (
        loc_type,
        int(project_id or 0),
        int(stage_id or 0),
        (customer_name or '').strip().lower(),
    )


def _load_position_events(rental_ids, item_ids, names=None):
    """Chronological movement events per rental line.

    Returns ``{rental_item_id: [event, ...]}`` where an event is
    ``{'kind': 'transfer'|'return', 'date', 'qty', 'dest', 'transfer'}``.
    ``qty is None`` means "everything that was there" — that is how transfers
    recorded before the per-tool split existed behave (the legacy movement log
    made the same assumption).

    Ordering is ``(date, kind, id)``: on the same day a transfer is applied
    before a return, which keeps the result deterministic.
    """
    events_by_item = {}
    rental_ids = [int(r) for r in (rental_ids or ())]
    item_ids = {int(i) for i in (item_ids or ())}
    if not rental_ids or not item_ids:
        return events_by_item

    kind_rank = {'transfer': 1, 'return': 2}

    transfers = (ToolRentalTransfer.query
                 .filter(ToolRentalTransfer.rental_id.in_(rental_ids))
                 .all())
    splits_by_transfer = {}
    if transfers:
        splits = (ToolRentalTransferItem.query
                  .filter(ToolRentalTransferItem.transfer_id.in_([int(t.id) for t in transfers]))
                  .all())
        for split in splits:
            splits_by_transfer.setdefault(int(split.transfer_id), []).append(split)

        items_by_rental = {}
        for item in ToolRentalItem.query.filter(
                ToolRentalItem.rental_id.in_(rental_ids)).all():
            items_by_rental.setdefault(int(item.rental_id), []).append(int(item.id))

        for transfer in transfers:
            dest = _transfer_destination(transfer, names)
            base = {'kind': 'transfer', 'date': transfer.transfer_date,
                    'dest': dest, 'transfer': transfer,
                    'seq': (kind_rank['transfer'], int(transfer.id or 0))}
            splits = splits_by_transfer.get(int(transfer.id))
            if splits:
                for split in splits:
                    if int(split.rental_item_id) not in item_ids:
                        continue
                    event = dict(base)
                    event['qty'] = _round_qty(split.qty_transferred)
                    events_by_item.setdefault(int(split.rental_item_id), []).append(event)
            else:
                # legacy: the whole rental moved, so every line moved in full
                for item_id in items_by_rental.get(int(transfer.rental_id), []):
                    if item_id not in item_ids:
                        continue
                    event = dict(base)
                    event['qty'] = None
                    events_by_item.setdefault(item_id, []).append(event)

    returns = (ToolRentalReturn.query
               .filter(ToolRentalReturn.rental_id.in_(rental_ids))
               .all())
    if returns:
        return_items = (ToolRentalReturnItem.query
                        .filter(ToolRentalReturnItem.return_id.in_([int(r.id) for r in returns]))
                        .all())
        by_id = {int(r.id): r for r in returns}
        for ret_item in return_items:
            if int(ret_item.rental_item_id) not in item_ids:
                continue
            ret = by_id.get(int(ret_item.return_id))
            if not ret:
                continue
            events_by_item.setdefault(int(ret_item.rental_item_id), []).append({
                'kind': 'return', 'date': ret.return_date, 'transfer': None,
                'qty': _round_qty(ret_item.qty_returned), 'dest': None,
                'seq': (kind_rank['return'], int(ret.id or 0)),
            })

    for events in events_by_item.values():
        events.sort(key=lambda e: ((e['date'] or date.min), e['seq']))
    return events_by_item


def _resolve_position_buckets(rental, item, events, names=None):
    """Replay a rental line's events into ``(location, qty)`` buckets.

    A transfer lifts pieces out of the most recently touched bucket and opens a
    new one at the destination; a return takes pieces off the most recent
    bucket first.  Because a partial transfer only moves the qty it names, a
    line can legitimately sit in two places at once (80 props on Site A, 40 on
    Site B) — which is the whole point of the position dashboard.
    """
    origin = _rental_origin(rental, names)
    buckets = []

    def open_bucket(loc, qty, chain, since):
        buckets.append({
            'loc_type': loc['loc_type'], 'label': loc['label'],
            'project_id': loc['project_id'], 'stage_id': loc['stage_id'],
            'customer_name': loc['customer_name'],
            'qty': _round_qty(qty), 'chain': list(chain), 'since': since,
        })

    origin_step = {'label': origin['label'], 'date': rental.rental_date}
    open_bucket(origin, _round_qty(item.qty_rented), [origin_step], rental.rental_date)

    for event in events:
        if event['kind'] == 'transfer':
            dest = event['dest']
            want = None if event['qty'] is None else _round_qty(event['qty'])
            remaining = want
            source_chain = None
            for bucket in reversed(buckets):
                if bucket['qty'] <= EPS:
                    continue
                if remaining is None:
                    take = bucket['qty']
                else:
                    if remaining <= EPS:
                        break
                    take = min(bucket['qty'], remaining)
                    remaining = _round_qty(remaining - take)
                bucket['qty'] = _round_qty(bucket['qty'] - take)
                source_chain = bucket['chain']
            moved = _round_qty(item.qty_rented) if want is None else _round_qty((want or 0) - (remaining or 0))
            if moved > EPS:
                chain = list(source_chain or [origin_step]) + [
                    {'label': dest['label'], 'date': event['date'] or rental.rental_date}]
                open_bucket(dest, moved, chain, event['date'] or rental.rental_date)
        else:  # return
            remaining = _round_qty(event['qty'])
            for bucket in reversed(buckets):
                if remaining <= EPS:
                    break
                if bucket['qty'] <= EPS:
                    continue
                take = min(bucket['qty'], remaining)
                bucket['qty'] = _round_qty(bucket['qty'] - take)
                remaining = _round_qty(remaining - take)

    return [b for b in buckets if b['qty'] > EPS]


class _NameLookup:
    """Project / stage names in one query each.

    Resolving labels through relationships costs two lazy loads per rental and
    per transfer — thousands of queries on a busy dashboard.  The tables are
    small next to the rental volume, so load them once per ledger build.
    """

    def __init__(self):
        self.projects = {int(p.id): (p.name or '') for p in Project.query.all()}
        self.stages = {int(s.id): (s.name or '') for s in Stage.query.all()}

    def project(self, project_id):
        return self.projects.get(int(project_id or 0))

    def stage(self, stage_id):
        return self.stages.get(int(stage_id or 0))


def _rental_origin(rental, names=None):
    """Where the tools went when the rental was created."""
    if (rental.renter_type or 'internal') == 'internal':
        if rental.project_id:
            pname = names.project(rental.project_id) if names else (
                rental.project.name if rental.project else None)
            label = pname or f'Project #{rental.project_id}'
            if rental.stage_id:
                sname = names.stage(rental.stage_id) if names else (
                    rental.stage.name if rental.stage else None)
                stage_name = sname or f'Stage #{rental.stage_id}'
                label = f'{label} > {stage_name}'
            return {
                'loc_type': LOC_OWN_PROJECT,
                'label': label,
                'project_id': int(rental.project_id or 0),
                'stage_id': int(rental.stage_id or 0),
                'customer_name': None,
            }
        # internal rental without a project behaves like a store issue
        return {'loc_type': LOC_STORE, 'label': WAREHOUSE_LABEL,
                'project_id': 0, 'stage_id': 0, 'customer_name': None}
    return {
        'loc_type': LOC_CUSTOMER,
        'label': (rental.customer_name or 'External Customer'),
        'project_id': 0,
        'stage_id': 0,
        'customer_name': (rental.customer_name or '').strip() or None,
    }


def _transfer_destination(transfer, names=None):
    to_type = (transfer.to_type or 'site').strip().lower()
    if to_type == 'warehouse':
        return {'loc_type': LOC_STORE, 'label': WAREHOUSE_LABEL,
                'project_id': 0, 'stage_id': 0, 'customer_name': None}
    if to_type == 'customer':
        name = (transfer.to_customer_name or 'External Customer').strip()
        return {'loc_type': LOC_CUSTOMER, 'label': name,
                'project_id': 0, 'stage_id': 0, 'customer_name': name}
    label = WAREHOUSE_LABEL
    if transfer.to_project_id:
        pname = names.project(transfer.to_project_id) if names else None
        if pname is None:
            proj = transfer.to_project or db.session.get(Project, int(transfer.to_project_id))
            pname = proj.name if proj else None
        label = pname or f'Project #{transfer.to_project_id}'
        if transfer.to_stage_id:
            sname = names.stage(transfer.to_stage_id) if names else None
            if sname is None:
                stage = transfer.to_stage or db.session.get(Stage, int(transfer.to_stage_id))
                sname = stage.name if stage else None
            if sname:
                label = f'{label} > {sname}'
        return {'loc_type': LOC_OWN_PROJECT, 'label': label,
                'project_id': int(transfer.to_project_id), 'stage_id': int(transfer.to_stage_id or 0),
                'customer_name': None}
    if transfer.to_customer_name:
        name = transfer.to_customer_name.strip()
        return {'loc_type': LOC_CUSTOMER, 'label': name,
                'project_id': 0, 'stage_id': 0, 'customer_name': name}
    return {'loc_type': LOC_STORE, 'label': WAREHOUSE_LABEL,
            'project_id': 0, 'stage_id': 0, 'customer_name': None}


def _is_overdue(rental, today=None):
    today = today or _pkt_today()
    if (rental.status or '') == 'overdue':
        return True
    exp = rental.expected_return_date
    return bool(exp and exp < today and (rental.status or '') in ('active', 'partially_returned'))


# --------------------------------------------------------------------------- #
# the ledger: where every piece of every tool is
# --------------------------------------------------------------------------- #
def tool_ledger():
    """Build the complete position for every (non-void) tool.

    Returns a dict with ``tools`` (per-tool rows), ``locations`` (roll-up per
    site/customer/store) and ``totals`` (dashboard numbers).
    """
    today = _pkt_today()
    tools = (Tool.query.filter(Tool.is_void == False)  # noqa: E712
             .order_by(Tool.name.asc(), Tool.tool_code.asc()).all())

    # Pending rental lines of live rentals, in one pass.  Void rentals hold
    # nothing, so they are excluded in SQL rather than filtered afterwards.
    pending_items = (ToolRentalItem.query
                     .join(ToolRental, ToolRental.id == ToolRentalItem.rental_id)
                     .filter(ToolRentalItem.qty_pending > EPS,
                             ToolRental.is_void == False)  # noqa: E712
                     .all())

    rental_ids = {int(it.rental_id) for it in pending_items}
    rentals = {}
    if rental_ids:
        for r in ToolRental.query.filter(ToolRental.id.in_(list(rental_ids))).all():
            rentals[int(r.id)] = r

    names = _NameLookup()
    events_by_item = _load_position_events(
        rental_ids, {int(it.id) for it in pending_items}, names)
    pending_by_tool = {}
    for item in pending_items:
        pending_by_tool.setdefault(int(item.tool_id), []).append(item)

    tool_rows = []
    locations = {}
    totals = {
        'owned_qty': 0.0, 'tool_types': 0, 'in_store_qty': 0.0,
        'own_project_qty': 0.0, 'customer_qty': 0.0, 'out_qty': 0.0,
        'damaged_lost_qty': 0.0, 'maintenance_qty': 0.0, 'unaccounted_qty': 0.0,
        'purchase_value': 0.0, 'out_value': 0.0, 'rental_value': 0.0,
        'paid_value': 0.0, 'pending_amount': 0.0,
        'open_rentals': 0, 'overdue_rentals': 0, 'overdue_qty': 0.0,
        'own_project_count': 0, 'customer_count': 0,
        'unaccounted_rows': 0, 'long_out_rows': 0, 'idle_rows': 0,
        'utilization_pct': 0.0, 'reconciled': True,
    }
    seen_rentals = set()
    all_holdings = []

    for tool in tools:
        owned = _round_qty(tool.total_quantity)
        unit_cost = _flt(tool.purchase_cost)
        condition = (tool.condition or 'good').strip().lower()

        holdings = []
        out_qty = 0.0
        out_value = 0.0
        overdue_qty = 0.0
        oldest_days = 0
        long_out = False
        rental_ids_for_tool = set()

        for item in pending_by_tool.get(int(tool.id), []):
            rental = rentals.get(int(item.rental_id))
            if not rental:
                continue
            pending_qty = _round_qty(item.qty_pending)
            if pending_qty <= EPS:
                continue

            days_out = _days_between(rental.rental_date, today)
            overdue = _is_overdue(rental, today)

            rental_ids_for_tool.add(int(rental.id))
            if int(rental.id) not in seen_rentals:
                seen_rentals.add(int(rental.id))
                totals['open_rentals'] += 1
                totals['rental_value'] += (_flt(rental.total_amount)
                                           if (rental.billing_type or '') != 'no_charge' else 0.0)
                totals['paid_value'] += _flt(rental.total_paid)
                if overdue:
                    totals['overdue_rentals'] += 1

            buckets = _resolve_position_buckets(
                rental, item, events_by_item.get(int(item.id), []), names)
            # Legacy rows can disagree with qty_pending (returns logged before
            # the split existed).  Force the buckets to add up to what is really
            # still out, so the dashboard can never show phantom stock.
            bucket_total = _round_qty(sum(b['qty'] for b in buckets))
            drift = _round_qty(pending_qty - bucket_total)
            if abs(drift) > EPS:
                if buckets:
                    buckets[-1]['qty'] = _round_qty(buckets[-1]['qty'] + drift)
                    buckets = [b for b in buckets if b['qty'] > EPS]
                else:
                    origin = _rental_origin(rental, names)
                    buckets = [{
                        'loc_type': origin['loc_type'], 'label': origin['label'],
                        'project_id': origin['project_id'], 'stage_id': origin['stage_id'],
                        'customer_name': origin['customer_name'], 'qty': pending_qty,
                        'chain': [{'label': origin['label'], 'date': rental.rental_date}],
                        'since': rental.rental_date,
                    }]

            for bucket in buckets:
                qty = _round_qty(bucket['qty'])
                if qty <= EPS:
                    continue
                chain = [dict(step, is_current=False) for step in bucket['chain']]
                if chain:
                    chain[-1]['is_current'] = True
                days_here = _days_between(bucket['since'], today)

                if overdue:
                    overdue_qty += qty
                if days_out > oldest_days:
                    oldest_days = days_out
                if days_out >= LONG_OUT_DAYS:
                    long_out = True

                out_qty += qty
                out_value += qty * unit_cost

                holding = {
                    'qty': qty,
                    'loc_type': bucket['loc_type'],
                    'label': bucket['label'],
                    'project_id': bucket['project_id'],
                    'stage_id': bucket['stage_id'],
                    'customer_name': bucket['customer_name'],
                    'rental': rental,
                    'rental_item': item,
                    'tool': tool,
                    'unit_cost': unit_cost,
                    'chain': chain,
                    'days_out': days_out,
                    'days_here': days_here,
                    'overdue': overdue,
                    'billing_type': rental.billing_type,
                    # apportioned below — a rental can sit at two customers
                    'pending_amount': 0.0,
                }
                holdings.append(holding)
                all_holdings.append(holding)

        # Clamp the store side at zero: if more is out than we own (bad legacy
        # stock edit) the store shows 0 and the gap surfaces as `variance`
        # instead of silently cancelling itself out.
        in_store = max(0.0, _round_qty(owned - out_qty))
        variance = _round_qty(owned - (in_store + out_qty))
        unaccounted = abs(variance) > EPS
        damaged_lost = _round_qty(owned if condition in _BAD_CONDITIONS else 0.0)
        maintenance = _round_qty(owned if condition == 'maintenance' else 0.0)
        idle = in_store >= owned - EPS and owned > 0

        totals['owned_qty'] += owned
        totals['tool_types'] += 1
        totals['in_store_qty'] += in_store
        totals['out_qty'] += out_qty
        totals['out_value'] += out_value
        totals['purchase_value'] += owned * unit_cost
        totals['damaged_lost_qty'] += damaged_lost
        totals['maintenance_qty'] += maintenance
        if unaccounted:
            totals['unaccounted_qty'] += abs(variance)
            totals['unaccounted_rows'] += 1
            totals['reconciled'] = False
        if long_out:
            totals['long_out_rows'] += 1
        if idle:
            totals['idle_rows'] += 1
        totals['overdue_qty'] += overdue_qty

        tool_rows.append({
            'tool': tool,
            'tool_id': int(tool.id),
            'name': tool.name,
            'code': tool.tool_code,
            'unit': tool.unit or 'pcs',
            'category': tool.category.name if tool.category else 'No Category',
            'owned_qty': owned,
            'in_store_qty': in_store,
            'own_project_qty': _round_qty(sum(h['qty'] for h in holdings if h['loc_type'] == LOC_OWN_PROJECT)),
            'customer_qty': _round_qty(sum(h['qty'] for h in holdings if h['loc_type'] == LOC_CUSTOMER)),
            'out_qty': out_qty,
            'out_value': _round_qty(out_value),
            'purchase_value': _round_qty(owned * unit_cost),
            'rate_per_day': _flt(tool.rental_rate_per_day),
            'condition': condition,
            'status': (tool.status or 'active'),
            'damaged_lost_qty': damaged_lost,
            'maintenance_qty': maintenance,
            'utilization_pct': round((out_qty / owned * 100.0), 1) if owned > 0 else 0.0,
            'variance': variance,
            'unaccounted': unaccounted,
            'overdue_qty': _round_qty(overdue_qty),
            'oldest_days_out': oldest_days,
            'long_out': long_out,
            'idle': idle,
            'holdings': sorted(holdings, key=lambda h: (h['loc_type'], h['label'], -h['qty'])),
            'current_label': _primary_location_label(holdings, in_store),
            'rental_ids': sorted(rental_ids_for_tool),
        })

    # Rent pending is a rental-level number; a rental can now hold pieces at
    # more than one customer, so share it out by qty instead of double counting.
    customer_holdings = {}
    for holding in all_holdings:
        if holding['loc_type'] == LOC_CUSTOMER:
            customer_holdings.setdefault(int(holding['rental'].id), []).append(holding)
    for cust_holdings in customer_holdings.values():
        rental = cust_holdings[0]['rental']
        pending_amt = _round_qty(rental.total_pending_amount)
        total_q = sum(h['qty'] for h in cust_holdings)
        for holding in cust_holdings:
            share = (holding['qty'] / total_q) if total_q > 0 else 0.0
            holding['pending_amount'] = _round_qty(pending_amt * share)

    for holding in all_holdings:
        key = _location_key(holding['loc_type'], holding['project_id'],
                            holding['stage_id'], holding['customer_name'])
        slot = locations.setdefault(key, {
            'loc_type': holding['loc_type'],
            'label': holding['label'],
            'project_id': holding['project_id'],
            'stage_id': holding['stage_id'],
            'customer_name': holding['customer_name'],
            'qty': 0.0, 'value': 0.0, 'tool_types': set(), 'rentals': set(),
            'overdue_qty': 0.0, 'pending_amount': 0.0, 'oldest_days': 0,
        })
        slot['qty'] = _round_qty(slot['qty'] + holding['qty'])
        slot['value'] += holding['qty'] * _flt(holding['unit_cost'])
        slot['tool_types'].add(int(holding['tool'].id))
        slot['rentals'].add(int(holding['rental'].id))
        if holding['overdue']:
            slot['overdue_qty'] = _round_qty(slot['overdue_qty'] + holding['qty'])
        slot['pending_amount'] = _round_qty(slot['pending_amount'] + holding['pending_amount'])
        slot['oldest_days'] = max(slot['oldest_days'], holding['days_out'])

    store_key = _location_key(LOC_STORE)
    store_slot = locations.setdefault(store_key, {
        'loc_type': LOC_STORE, 'label': WAREHOUSE_LABEL, 'project_id': 0,
        'stage_id': 0, 'customer_name': None, 'qty': 0.0, 'value': 0.0,
        'tool_types': set(), 'rentals': set(), 'overdue_qty': 0.0,
        'pending_amount': 0.0, 'oldest_days': 0,
    })
    store_slot['qty'] = _round_qty(totals['in_store_qty'])
    for row in tool_rows:
        if row['in_store_qty'] > EPS:
            store_slot['tool_types'].add(row['tool_id'])
            store_slot['value'] += row['in_store_qty'] * _flt(row['tool'].purchase_cost)

    location_rows = []
    for slot in locations.values():
        location_rows.append({
            'loc_type': slot['loc_type'],
            'label': slot['label'],
            'project_id': slot['project_id'],
            'stage_id': slot['stage_id'],
            'customer_name': slot['customer_name'],
            'qty': _round_qty(slot['qty']),
            'value': _round_qty(slot['value']),
            'tool_types': len(slot['tool_types']),
            'rentals': len(slot['rentals']),
            'overdue_qty': _round_qty(slot['overdue_qty']),
            'pending_amount': _round_qty(slot['pending_amount']),
            'oldest_days': slot['oldest_days'],
        })
    location_rows.sort(key=lambda r: (r['loc_type'] != LOC_OWN_PROJECT,
                                      r['loc_type'] != LOC_CUSTOMER,
                                      -r['qty'], r['label'].lower()))

    totals['own_project_qty'] = _round_qty(sum(r['qty'] for r in location_rows if r['loc_type'] == LOC_OWN_PROJECT))
    totals['customer_qty'] = _round_qty(sum(r['qty'] for r in location_rows if r['loc_type'] == LOC_CUSTOMER))
    totals['in_store_qty'] = _round_qty(totals['in_store_qty'])
    totals['out_qty'] = _round_qty(totals['out_qty'])
    totals['owned_qty'] = _round_qty(totals['owned_qty'])
    totals['own_project_count'] = len([r for r in location_rows if r['loc_type'] == LOC_OWN_PROJECT and r['qty'] > EPS])
    totals['customer_count'] = len([r for r in location_rows if r['loc_type'] == LOC_CUSTOMER and r['qty'] > EPS])
    totals['pending_amount'] = _round_qty(totals['rental_value'] - totals['paid_value'])
    totals['utilization_pct'] = round((totals['out_qty'] / totals['owned_qty'] * 100.0), 1) if totals['owned_qty'] > 0 else 0.0
    for key in ('purchase_value', 'out_value', 'rental_value', 'paid_value'):
        totals[key] = _round_qty(totals[key])

    return {'tools': tool_rows, 'locations': location_rows, 'totals': totals, 'today': today}


def _primary_location_label(holdings, in_store_qty):
    """One-line answer to "where is this tool?" for the list view."""
    if not holdings:
        return WAREHOUSE_LABEL
    top = max(holdings, key=lambda h: h['qty'])
    label = top['label']
    if len(holdings) > 1:
        label = f'{label} (+{len(holdings) - 1} more)'
    if in_store_qty > EPS:
        label = f'{WAREHOUSE_LABEL} + {label}'
    return label


def tools_reconciliation(ledger=None):
    """The balance sheet of tools: owned vs store vs own projects vs customers."""
    ledger = ledger or tool_ledger()
    totals = ledger['totals']
    rows = [
        {
            'key': 'owned', 'label': 'Total Tools Owned (Inventory)',
            'qty': totals['owned_qty'], 'types': totals['tool_types'],
            'value': totals['purchase_value'], 'icon': 'fa-tools', 'tone': 'blue',
            'hint': 'Sum of total_quantity on every active tool',
        },
        {
            'key': 'in_store', 'label': 'In Store / Warehouse (Available)',
            'qty': totals['in_store_qty'],
            'types': len([t for t in ledger['tools'] if t['in_store_qty'] > EPS]),
            'value': _round_qty(totals['purchase_value'] - totals['out_value']),
            'icon': 'fa-warehouse', 'tone': 'green',
            'hint': 'Owned minus everything currently out',
        },
        {
            'key': 'own_project', 'label': 'Sent to Own Projects / Sites',
            'qty': totals['own_project_qty'],
            'types': len([t for t in ledger['tools'] if t['own_project_qty'] > EPS]),
            'value': _round_qty(sum(h['qty'] * _flt(t['tool'].purchase_cost)
                                    for t in ledger['tools'] for h in t['holdings']
                                    if h['loc_type'] == LOC_OWN_PROJECT)),
            'icon': 'fa-helmet-safety', 'tone': 'orange',
            'hint': f"Holding on {totals['own_project_count']} own site(s)",
        },
        {
            'key': 'customer', 'label': 'Sent to Other Customers (Rental)',
            'qty': totals['customer_qty'],
            'types': len([t for t in ledger['tools'] if t['customer_qty'] > EPS]),
            'value': _round_qty(sum(h['qty'] * _flt(t['tool'].purchase_cost)
                                    for t in ledger['tools'] for h in t['holdings']
                                    if h['loc_type'] == LOC_CUSTOMER)),
            'icon': 'fa-user-tag', 'tone': 'purple',
            'hint': f"{totals['customer_count']} customer(s) | pending rent {totals['pending_amount']:,.0f}",
        },
    ]
    total_sent = _round_qty(totals['own_project_qty'] + totals['customer_qty'])
    return {
        'rows': rows,
        'owned': totals['owned_qty'],
        'in_store': totals['in_store_qty'],
        'own_project': totals['own_project_qty'],
        'customer': totals['customer_qty'],
        'total_sent': total_sent,
        'accounted': _round_qty(totals['in_store_qty'] + total_sent),
        'variance': _round_qty(totals['owned_qty'] - (totals['in_store_qty'] + total_sent)),
        'balanced': abs(totals['owned_qty'] - (totals['in_store_qty'] + total_sent)) <= EPS,
        'unaccounted_rows': [t for t in ledger['tools'] if t['unaccounted']],
    }


def dashboard_summary(ledger=None):
    """KPI cards + the split bar for the Tools dashboard."""
    ledger = ledger or tool_ledger()
    totals = ledger['totals']
    recon = tools_reconciliation(ledger)
    owned = totals['owned_qty'] or 0.0

    def pct(value):
        return round((value / owned * 100.0), 1) if owned > 0 else 0.0

    return {
        'totals': totals,
        'recon': recon,
        'split': [
            {'key': 'in_store', 'label': 'In Store', 'qty': totals['in_store_qty'],
             'pct': pct(totals['in_store_qty']), 'color': '#16a34a', 'icon': 'fa-warehouse'},
            {'key': 'own_project', 'label': 'Own Projects', 'qty': totals['own_project_qty'],
             'pct': pct(totals['own_project_qty']), 'color': '#f59e0b', 'icon': 'fa-helmet-safety'},
            {'key': 'customer', 'label': 'Other Customers', 'qty': totals['customer_qty'],
             'pct': pct(totals['customer_qty']), 'color': '#7c3aed', 'icon': 'fa-user-tag'},
        ],
        'top_locations': [r for r in ledger['locations'] if r['qty'] > EPS][:8],
        'attention': tools_attention(ledger),
        'idle_tools': [t for t in ledger['tools'] if t['idle']][:10],
        'long_out': sorted([t for t in ledger['tools'] if t['long_out']],
                           key=lambda t: -t['oldest_days_out'])[:10],
    }


def tools_attention(ledger=None, limit=None):
    """Everything that needs a human decision, newest problem first."""
    ledger = ledger or tool_ledger()
    totals = ledger['totals']
    today = ledger.get('today') or _pkt_today()
    issues = []

    for row in ledger['tools']:
        if row['unaccounted']:
            issues.append({
                'severity': 'danger', 'icon': 'fa-scale-unbalanced',
                'title': f"{row['name']} does not reconcile",
                'detail': (f"Owned {row['owned_qty']:g} but store {row['in_store_qty']:g} + "
                           f"out {row['out_qty']:g} = {row['in_store_qty'] + row['out_qty']:g} "
                           f"({row['variance']:+g}). Fix the stock qty in Inventory."),
                'url_endpoint': 'hdc_tool_rental_inventory', 'url_args': {},
            })
        for holding in row['holdings']:
            if holding['overdue']:
                issues.append({
                    'severity': 'danger', 'icon': 'fa-clock',
                    'title': f"{row['name']} overdue at {holding['label']}",
                    'detail': (f"{holding['qty']:g} {row['unit']} out since {holding['rental'].rental_date} "
                               f"(expected {holding['rental'].expected_return_date or 'no date'}). "
                               f"Rental {holding['rental'].rental_code}."),
                    'url_endpoint': 'hdc_tool_rental_detail',
                    'url_args': {'rental_id': holding['rental'].id},
                })
            elif holding['days_out'] >= LONG_OUT_DAYS:
                issues.append({
                    'severity': 'warning', 'icon': 'fa-hourglass-half',
                    'title': f"{row['name']} out {holding['days_out']} days at {holding['label']}",
                    'detail': f"Rental {holding['rental'].rental_code} — confirm it is still needed there.",
                    'url_endpoint': 'hdc_tool_rental_detail',
                    'url_args': {'rental_id': holding['rental'].id},
                })
        if row['condition'] in ('damaged', 'lost') and row['owned_qty'] > EPS:
            issues.append({
                'severity': 'warning', 'icon': 'fa-triangle-exclamation',
                'title': f"{row['name']} marked {row['condition']}",
                'detail': f"{row['owned_qty']:g} {row['unit']} counted as {row['condition']} — write off or repair.",
                'url_endpoint': 'hdc_tool_rental_inventory', 'url_args': {},
            })
        if row['idle'] and row['rate_per_day'] > 0:
            issues.append({
                'severity': 'info', 'icon': 'fa-couch',
                'title': f"{row['name']} is idle in store",
                'detail': (f"{row['in_store_qty']:g} {row['unit']} never rented out "
                           f"(rate {row['rate_per_day']:,.0f}/day) — rent it or retire it."),
                'url_endpoint': 'hdc_tool_rental', 'url_args': {},
            })

    if totals['pending_amount'] > EPS:
        issues.append({
            'severity': 'warning', 'icon': 'fa-hand-holding-dollar',
            'title': f"Rent pending: {totals['pending_amount']:,.0f}",
            'detail': f"{totals['open_rentals']} open rental(s), {totals['paid_value']:,.0f} collected so far.",
            'url_endpoint': 'hdc_tool_rental_reports', 'url_args': {},
        })

    order = {'danger': 0, 'warning': 1, 'info': 2}
    issues.sort(key=lambda i: (order.get(i['severity'], 9), i['title']))
    return issues[:limit] if limit else issues


def tool_position(tool_id, ledger=None):
    """Deep view for one tool: balance + every place it currently is."""
    ledger = ledger or tool_ledger()
    row = next((t for t in ledger['tools'] if int(t['tool_id']) == int(tool_id)), None)
    if not row:
        return None
    movements = (ToolMovementLog.query
                 .filter(ToolMovementLog.tool_id == int(tool_id))
                 .order_by(ToolMovementLog.timestamp.desc(), ToolMovementLog.id.desc())
                 .limit(120).all())
    return {'row': row, 'movements': movements}


def location_summary(ledger=None):
    """Per-site / per-customer holdings, ready for the dashboard table."""
    ledger = ledger or tool_ledger()
    rows = []
    for loc in ledger['locations']:
        if loc['qty'] <= EPS and loc['loc_type'] != LOC_STORE:
            continue
        rows.append(loc)
    return rows


# --------------------------------------------------------------------------- #
# find anything — one search box for the whole tool section
# --------------------------------------------------------------------------- #
def tools_universal_search(term, limit=12):
    """Search tools, rentals, customers, sites and movements in one go.

    Returns ``{'term', 'tools', 'rentals', 'locations', 'movements', 'total'}``
    where every hit carries enough context to answer "where is it?" without
    another click.
    """
    term = (term or '').strip()
    result = {'term': term, 'tools': [], 'rentals': [], 'locations': [],
              'movements': [], 'total': 0}
    if not term:
        return result
    like = f'%{term.lower()}%'
    ledger = tool_ledger()
    rows_by_id = {int(t['tool_id']): t for t in ledger['tools']}

    for row in ledger['tools']:
        haystack = ' '.join([
            str(row['name'] or ''), str(row['code'] or ''), str(row['category'] or ''),
            str(row['tool'].description or ''), str(row['tool'].unit or ''),
        ]).lower()
        if term.lower() in haystack:
            result['tools'].append(row)
            if len(result['tools']) >= limit:
                break

    rentals = (ToolRental.query
               .filter(ToolRental.is_void == False)  # noqa: E712
               .filter(or_(
                   func.lower(func.coalesce(ToolRental.rental_code, '')).like(like),
                   func.lower(func.coalesce(ToolRental.customer_name, '')).like(like),
                   func.lower(func.coalesce(ToolRental.customer_phone, '')).like(like),
                   func.lower(func.coalesce(ToolRental.notes, '')).like(like),
               ))
               .order_by(ToolRental.rental_date.desc(), ToolRental.id.desc())
               .limit(limit).all())
    for rental in rentals:
        pending_items = [it for it in rental.items if _flt(it.qty_pending) > EPS]
        result['rentals'].append({
            'rental': rental,
            'pending_qty': _round_qty(sum(_flt(it.qty_pending) for it in pending_items)),
            'pending_tools': [
                {
                    'tool_id': int(it.tool_id),
                    'name': it.tool.name if it.tool else f"Tool #{it.tool_id}",
                    'code': it.tool.tool_code if it.tool else '',
                    'qty': _round_qty(it.qty_pending),
                    'location': _holding_label_for_item(int(it.id), ledger),
                } for it in pending_items
            ],
        })

    for loc in ledger['locations']:
        if term.lower() in (loc['label'] or '').lower() and loc['qty'] > EPS:
            result['locations'].append(loc)

    # customer / site names that hold tools right now
    for row in ledger['tools']:
        for holding in row['holdings']:
            if term.lower() in (holding['label'] or '').lower():
                result['locations'].append({
                    'loc_type': holding['loc_type'], 'label': holding['label'],
                    'project_id': holding['project_id'], 'stage_id': holding['stage_id'],
                    'customer_name': holding['customer_name'],
                    'qty': holding['qty'], 'value': 0.0, 'tool_types': 1, 'rentals': 1,
                    'overdue_qty': holding['qty'] if holding['overdue'] else 0.0,
                    'pending_amount': holding['pending_amount'], 'oldest_days': holding['days_out'],
                })
    deduped = {}
    for loc in result['locations']:
        key = _location_key(loc['loc_type'], loc.get('project_id'), loc.get('stage_id'),
                            loc.get('customer_name'))
        if key in deduped:
            deduped[key]['qty'] = _round_qty(deduped[key]['qty'] + loc['qty'])
            deduped[key]['tool_types'] += 1
        else:
            deduped[key] = dict(loc)
    result['locations'] = sorted(deduped.values(), key=lambda r: -r['qty'])[:limit]

    movements = (ToolMovementLog.query
                 .filter(or_(
                     func.lower(func.coalesce(ToolMovementLog.to_location_label, '')).like(like),
                     func.lower(func.coalesce(ToolMovementLog.from_location_label, '')).like(like),
                     func.lower(func.coalesce(ToolMovementLog.notes, '')).like(like),
                 ))
                 .order_by(ToolMovementLog.timestamp.desc(), ToolMovementLog.id.desc())
                 .limit(limit).all())
    result['movements'] = movements
    result['total'] = (len(result['tools']) + len(result['rentals'])
                       + len(result['locations']) + len(result['movements']))
    result['rows_by_id'] = rows_by_id
    return result


def _holding_label_for_item(rental_item_id, ledger):
    for row in ledger['tools']:
        for holding in row['holdings']:
            if int(holding['rental_item'].id) == int(rental_item_id):
                return holding['label']
    return WAREHOUSE_LABEL


# --------------------------------------------------------------------------- #
# per-tool transfer split (write side, used by the transfer route)
# --------------------------------------------------------------------------- #
def allocate_transfer_qty(pending_pairs, requested_total=None):
    """Split a transferred quantity over rental lines, per tool.

    ``pending_pairs`` is ``[(rental_item, qty_pending), ...]``.  When the caller
    gives no total (or a total >= everything pending) every line moves in full.
    Otherwise lines are filled in order and the requested total is never
    exceeded, so the ledger stays balanced.
    """
    lines = [(item, _round_qty(qty)) for item, qty in pending_pairs if _round_qty(qty) > EPS]
    if not lines:
        return []
    pending_total = _round_qty(sum(qty for _, qty in lines))
    total = _round_qty(requested_total) if requested_total is not None else pending_total
    if total <= EPS or total >= pending_total - EPS:
        return [(item, qty) for item, qty in lines]

    allocated = []
    remaining = total
    for item, qty in lines:
        if remaining <= EPS:
            break
        take = min(qty, remaining)
        allocated.append((item, _round_qty(take)))
        remaining = _round_qty(remaining - take)
    return allocated


def record_transfer_items(transfer, allocations):
    """Persist the per-tool split of a transfer + one movement log per tool."""
    for item, qty in allocations:
        db.session.add(ToolRentalTransferItem(
            transfer_id=int(transfer.id),
            rental_item_id=int(item.id),
            tool_id=int(item.tool_id),
            qty_transferred=_round_qty(qty),
            created_at=_pkt_now_naive(),
        ))
    db.session.flush()
    return len(allocations)
