"""HDC services.tool_txn_entry — the Tool Transaction page's server side.

The central **HDC Tool Transaction** page (``/hdc/tool-rental/transaction``) is a
front end over the business logic the Tools section already owns: three groups
— **Rent Out**, **Return**, **Maintenance** — whose transactions are declared in
``hdc/services/tool_txn_types.py``.  This module adds *no* parallel
bookkeeping: it only

* builds the data the form renders (in-store tools with their serial markings,
  current rental holders for transfers, rentals that still have tools out or
  rent due, pieces under maintenance, accounts, projects and customers);
* mints and detects the one transaction reference the form carries, so a
  double-click or a retried post cannot post twice;
* implements the physical movements that previously had no route:

  - **a return from a holder** — the sheet lists everything a site or customer
    still holds, so one submission can close several of its open issues at
    once.  Each issue keeps its own return record, written together in ONE
    database transaction; the optional rent collection settles exactly one of
    them;
  - **damage / loss reported on a holder** — the piece is returned off the
    rental line and written off through the existing scrap register, in ONE
    database transaction, so ``owned = in_store + out`` still holds;
  - **send for maintenance** — pieces leave the available pool but stay owned
    and stay traceable (serial + movement history);
  - **return from maintenance** — pieces come back to the store, or are
    written off if the workshop could not repair them.

Everything else (rent out, issue to an own site, transfer between sites or
customers, rent payments and credit adjustments) posts to the existing
endpoints — ``hdc_tool_rental_create``, ``hdc_tool_rental_payment`` and
``hdc_tool_rental_add_discount`` — with the same field names those routes
already accept, so the money and tool rules live in one place.  The
per-rental ``hdc_tool_rental_return`` route still serves the rental page and
the New Rental form unchanged.

Layer: ``services`` — may import models, utils and other services.
"""

from __future__ import annotations

import secrets

from sqlalchemy import func, or_
from sqlalchemy.orm import joinedload

from hdc.extensions import db
from hdc.models.tool_rental import (
    TOOL_DISCOUNT_REASONS, TOOL_SCRAP_REASONS, Tool, ToolCategory, ToolMovementLog,
    ToolPurchase, ToolRental, ToolRentalDiscount, ToolRentalItem,
    ToolRentalPayment, ToolRentalReturn, ToolRentalReturnItem, ToolRentalTransfer,
    ToolScrap, ToolSerial,
)
from hdc.services.record_permissions import integrity_message
from hdc.services.tool_rental import (
    EPS, SCRAP_LABEL, STORE_LABEL, create_movement_log, create_serial_movement,
    ensure_all_tools_serials, ensure_tool_serials, get_receiving_accounts,
    known_tool_customers, recalc_rental_totals, record_tool_rental_discount,
    record_tool_scrap, post_tool_rental_payment_to_accounts,
    return_serials_from_rental, tool_available_for_integrity, get_out_of_store_serials,
)
from hdc.services.tool_tracking import (
    LOC_CUSTOMER, LOC_OWN_PROJECT, holding_key, holdings_by_item,
    serials_at_holding, tool_ledger,
)
from hdc.utils.dates import _pkt_now_naive, _pkt_today
from hdc.utils.format import _flt

#: Where pieces wait while the workshop has them.  Deliberately NOT the store
#: label: the piece is still owned and still on the store side of the balance
#: (see hdc/services/tool_tracking.py rule 4), but "available to rent" is a lie.
MAINTENANCE_LABEL = 'Maintenance / Workshop'

#: Movement-log vocabulary for the two maintenance legs.
MOVEMENT_MAINTENANCE_OUT = 'maintenance_out'
MOVEMENT_MAINTENANCE_IN = 'maintenance_in'

#: Reason -> label lookup for scrap reasons (damage / loss).
SCRAP_REASON_LABELS = {key: label for key, label in TOOL_SCRAP_REASONS}
DISCOUNT_REASON_LABELS = {key: label for key, label in TOOL_DISCOUNT_REASONS}

#: How many still-open rentals a picker offers before the searchable combo
#: simply shows the newest ones (same cap the New Rental page uses).
RENTAL_PICKER_LIMIT = 300
TRANSFER_SOURCE_LIMIT = 60

#: Transaction reference shape: HDC-TX-YYYYMMDD-XXXXXX (Crockford-ish alphabet,
#: no 0/O/1/I so it can be read aloud over the phone).
_REF_ALPHABET = '23456789ABCDEFGHJKMNPQRSTUVWXYZ'


# --------------------------------------------------------------------------- #
# Transaction reference — the duplicate-submission guard
# --------------------------------------------------------------------------- #
def new_txn_reference(when=None, *, prefix='HDC-TX') -> str:
    """Mint a fresh transaction reference: ``HDC-TX-20261011-4F7A2C``.

    Uniqueness is checked against every record a Tools transaction can leave a
    reference on, so the number on the printed form always resolves to exactly
    one real transaction.
    """
    day = (when or _pkt_today())
    day_str = day.strftime('%Y%m%d') if hasattr(day, 'strftime') else str(day)
    for _attempt in range(25):
        token = ''.join(secrets.choice(_REF_ALPHABET) for _ in range(6))
        reference = f'{prefix}-{day_str}-{token}'
        if not txn_reference_seen(reference):
            return reference
    # Astronomically unlikely; fall back to a longer random body.
    return f'{prefix}-{day_str}-{"".join(secrets.choice(_REF_ALPHABET) for _ in range(12))}'


def _like(model_column, reference):
    return model_column.ilike(f'%{reference}%')


def txn_reference_seen(reference: str) -> bool:
    """Has this transaction reference already been recorded anywhere?

    A Tools transaction can leave its reference on a rental, a return, a
    payment, a concession or a scrap row (there is no new "transaction" table —
    the reference rides on the record it produced, exactly like the operator
    reference the cash-flow register carries).  The moment one of those rows
    carries it, the reference is spent: a re-posted form is a duplicate.
    """
    ref = (reference or '').strip()
    if not ref:
        return False
    checks = (
        (ToolRental, ToolRental.notes),
        (ToolRentalReturn, ToolRentalReturn.notes),
        (ToolRentalPayment, ToolRentalPayment.reference),
        (ToolRentalPayment, ToolRentalPayment.notes),
        (ToolRentalDiscount, ToolRentalDiscount.notes),
        (ToolScrap, ToolScrap.reference),
        (ToolScrap, ToolScrap.notes),
        # A maintenance move and a site transfer leave their reference on the
        # transfer row and on the movement log; without these two a re-posted
        # form looked brand new and moved the same pieces twice.
        (ToolRentalTransfer, ToolRentalTransfer.notes),
        (ToolPurchase, ToolPurchase.reference),
        (ToolPurchase, ToolPurchase.notes),
        (ToolMovementLog, ToolMovementLog.notes),
    )
    for model, column in checks:
        found = (db.session.query(model.id)
                 .filter(_like(column, ref))
                 .limit(1)
                 .first())
        if found is not None:
            return True
    return False


def stamp_reference(text: str, reference: str, *, responsible: str = '') -> str:
    """Prefix the transaction reference (and responsible person) onto free text.

    The reference has to survive on the record the transaction creates, because
    that record *is* the audit trail — nothing else is written.
    """
    parts = []
    ref = (reference or '').strip()
    if ref:
        parts.append(f'[Ref {ref}]')
    person = (responsible or '').strip()
    if person:
        parts.append(f'[Responsible: {person}]')
    body = (text or '').strip()
    if body:
        parts.append(body)
    return ' '.join(parts)


# --------------------------------------------------------------------------- #
# Form context
# --------------------------------------------------------------------------- #
def in_store_tool_rows():
    """Every active tool that has something to issue, with its serial markings.

    The same shape the New Rental page renders (category, rate, available qty,
    in-store serials) so the searchable tool combo and its serial picker behave
    identically on both pages.
    """
    tools = (Tool.query
             .filter(Tool.is_void == False)  # noqa: E712
             .order_by(Tool.name.asc())
             .all())
    rows = []
    for tool in tools:
        available = tool_available_for_integrity(tool)
        serials = [s for s in tool.issuable_serials
                   if (s.status or '') != 'maintenance']
        if available <= 0.001 and not serials:
            continue
        category_name = tool.category.name if tool.category else tool.name
        rows.append({
            'id': int(tool.id),
            'name': tool.name,
            'code': tool.tool_code or '',
            'category_id': int(tool.category_id) if tool.category_id else None,
            'category_name': category_name,
            'unit': tool.unit or 'pcs',
            'rate': _flt(tool.rental_rate_per_day),
            'available_qty': available,
            'owned_qty': _flt(tool.total_quantity),
            'condition': tool.condition or 'good',
            'serials': [
                {
                    'id': int(s.id),
                    'serial_number': s.serial_number,
                    'condition': s.condition or 'good',
                }
                for s in serials
            ],
        })
    return rows


def maintenance_pieces():
    """Pieces currently in the workshop, ready to come back."""
    rows = (ToolSerial.query
            .join(Tool, Tool.id == ToolSerial.tool_id)
            .filter(Tool.is_void == False,  # noqa: E712
                    ToolSerial.status == 'maintenance')
            .order_by(ToolSerial.tool_id.asc())
            .all())
    out = []
    for serial in rows:
        tool = serial.tool
        out.append({
            'serial_id': int(serial.id),
            'serial_number': serial.serial_number,
            'tool_id': int(serial.tool_id),
            'tool_name': tool.name if tool else f'Tool #{serial.tool_id}',
            'tool_code': (tool.tool_code if tool else '') or '',
            'unit': (tool.unit if tool else 'pcs') or 'pcs',
            'condition': serial.condition or 'good',
            'notes': serial.notes or '',
            'since': serial.updated_at.strftime('%Y-%m-%d') if serial.updated_at else '',
        })
    out.sort(key=lambda row: (row['tool_name'].lower(), row['serial_number'].lower()))
    return out


def transfer_source_rows(limit=TRANSFER_SOURCE_LIMIT):
    """Rented tools grouped by the holder that is actually holding them.

    One row per rental + location, because a site can hold tools under more
    than one active rental.  Built from :func:`tool_ledger`, the same source of
    truth the Tracking page and the transfer route trust, so what the form
    offers is exactly what the server will accept.
    """
    active_rentals = (ToolRental.query
                      .filter(ToolRental.is_void == False,  # noqa: E712
                              ToolRental.status.in_(['active', 'partially_returned', 'overdue']))
                      .order_by(ToolRental.rental_date.desc(), ToolRental.id.desc())
                      .limit(limit)
                      .all())
    if not active_rentals:
        return []
    order = {int(r.id): index for index, r in enumerate(active_rentals)}
    active_ids = set(order)

    sources = {}
    positions = tool_ledger()
    for tool_row in positions['tools']:
        for holding in tool_row['holdings']:
            rental = holding['rental']
            rental_id = int(rental.id)
            if rental_id not in active_ids:
                continue
            loc_type = holding['loc_type']
            project_id = int(holding.get('project_id') or 0)
            stage_id = int(holding.get('stage_id') or 0)
            customer_name = (holding.get('customer_name') or '').strip()
            location_key = location_key_for(loc_type, project_id, stage_id, customer_name)
            source_key = f'{rental_id}|{location_key}'
            source = sources.setdefault(source_key, {
                'key': source_key,
                'rental_id': rental_id,
                'rental_code': rental.rental_code,
                'location_key': location_key,
                'holder': holding['label'],
                'loc_type': loc_type,
                'project_id': project_id,
                'stage_id': stage_id,
                'customer_name': customer_name,
                'billing_type': rental.billing_type,
                'amount_due': 0.0 if rental.billing_type == 'no_charge' else _flt(rental.total_pending_amount),
                'pending': 0.0,
                'lines': [],
            })
            item = holding['rental_item']
            tool = holding['tool']
            source_rate = _flt(item.rate)
            if source_rate <= 0:
                source_rate = _flt(tool.rental_rate_per_day if tool else 0.0)
            qty = _flt(holding.get('qty'))
            source['pending'] += qty
            line = None
            for existing in source['lines']:
                if existing['item_id'] == int(item.id):
                    line = existing
                    break
            if line is None:
                line = {
                    'item_id': int(item.id),
                    'tool_id': int(item.tool_id),
                    'name': tool.name if tool else f'Tool #{item.tool_id}',
                    'code': (tool.tool_code if tool else '') or '',
                    'unit': (tool.unit if tool else 'pcs') or 'pcs',
                    'category_name': (tool.category.name if tool and tool.category
                                      else (tool.name if tool else '')),
                    'qty': 0.0,
                    'rate': source_rate,
                    'daily_rate': _flt(tool.rental_rate_per_day if tool else source_rate) or source_rate,
                    'serials': [],
                }
                source['lines'].append(line)
            line['qty'] += qty
            for serial in serials_at_holding(
                    item, location_key=holding_key(loc_type, project_id, stage_id, customer_name),
                    ledger=positions):
                if any(s['id'] == int(serial.id) for s in line['serials']):
                    continue
                line['serials'].append({
                    'id': int(serial.id),
                    'serial_number': serial.serial_number,
                    'condition': serial.condition or 'good',
                    'location': serial.current_location_label or source['holder'],
                })
    rows = []
    for source in sources.values():
        if source['pending'] <= EPS:
            continue
        source['lines'] = sorted(source['lines'], key=lambda line: (line['name'].lower(), line['code']))
        rows.append(source)
    rows.sort(key=lambda source: (order.get(source['rental_id'], TRANSFER_SOURCE_LIMIT),
                                  (source['holder'] or '').lower()))
    return rows


def location_key_for(loc_type, project_id=None, stage_id=None, customer_name=None) -> str:
    """Encode one actual holding location.

    Byte-for-byte the key the transfer route parses, so a form built here can
    post to the existing endpoint unchanged.
    """
    from urllib.parse import quote
    return '|'.join((
        str(loc_type or ''),
        str(int(project_id or 0)),
        str(int(stage_id or 0)),
        quote((customer_name or '').strip(), safe=''),
    ))


def _out_of_store_serials_by_item(rental_ids):
    """Every piece still out on these rentals, keyed by its rental line.

    One query for the whole sheet instead of one per row: the return sheet can
    list a site's whole holding (several issues) at once.
    """
    if not rental_ids:
        return {}
    rows = (ToolSerial.query
            .filter(ToolSerial.current_rental_id.in_([int(rid) for rid in rental_ids]),
                    ToolSerial.is_in_store == False,  # noqa: E712
                    or_(ToolSerial.is_scrapped == False,  # noqa: E712
                        ToolSerial.is_scrapped.is_(None)),
                    ToolSerial.status != 'scrapped')
            .all())
    by_item = {}
    for serial in sorted(rows, key=lambda s: (int(s.tool_id or 0), s.sort_key)):
        if serial.current_rental_item_id is None:
            continue
        by_item.setdefault(int(serial.current_rental_item_id), []).append(serial)
    return by_item


def return_sheet(limit=RENTAL_PICKER_LIMIT):
    """One return row per tool line at its ACTUAL current holder.

    A split issue can have pieces at A, B and a customer.  The original rental
    header cannot answer where they are.  Use the same location buckets as
    Tracking, and offer only the serial identities in each bucket.
    """
    rentals = (ToolRental.query
               .filter(ToolRental.is_void == False,  # noqa: E712
                       ToolRental.items.any(func.coalesce(ToolRentalItem.qty_pending, 0) > EPS))
               .order_by(ToolRental.rental_date.desc(), ToolRental.id.desc())
               .limit(limit).all())
    if not rentals:
        return {'holders': [], 'rows': []}
    by_rental = {int(r.id): r for r in rentals}
    order = {int(r.id): index for index, r in enumerate(rentals)}
    positions = tool_ledger()
    buckets = {}
    for row in positions['tools']:
        for holding in row['holdings']:
            item = holding['rental_item']
            if int(item.rental_id) not in by_rental:
                continue
            key = holding_key(holding['loc_type'], holding.get('project_id'),
                              holding.get('stage_id'), holding.get('customer_name'))
            slot_key = (int(item.id), key.casefold())
            if slot_key not in buckets:
                buckets[slot_key] = dict(holding, qty=0.0, location_key=key)
            buckets[slot_key]['qty'] += _flt(holding['qty'])

    rows = []
    for holding in buckets.values():
        item = holding['rental_item']
        rental = by_rental[int(item.rental_id)]
        tool = item.tool
        if holding['loc_type'] == LOC_OWN_PROJECT:
            holder_key = f"project:{int(holding.get('project_id') or 0)}"
        else:
            holder_key = 'customer:' + (holding.get('customer_name') or '').strip().casefold()
        rows.append({
            'holderKey': holder_key, 'holder': holding['label'],
            'locType': holding['loc_type'], 'locationKey': holding['location_key'],
            'rentalId': int(rental.id), 'rentalCode': rental.rental_code or f'Rental #{rental.id}',
            'itemId': int(item.id), 'toolId': int(item.tool_id),
            'toolName': tool.name if tool else f'Tool #{item.tool_id}',
            'toolCode': (tool.tool_code if tool else '') or '',
            'unit': (tool.unit if tool else 'pcs') or 'pcs',
            'issued': _flt(item.qty_rented), 'returned': _flt(item.qty_returned),
            'pending': _flt(holding['qty']), 'billingType': rental.billing_type or '',
            'amountDue': 0.0 if rental.billing_type == 'no_charge' else _flt(rental.total_pending_amount),
            'serials': [{'id': int(s.id), 'serialNumber': s.serial_number,
                         'condition': s.condition or 'good'}
                        for s in serials_at_holding(item, location_key=holding['location_key'],
                                                    ledger=positions)],
        })
    rows.sort(key=lambda r: (order.get(r['rentalId'], 0), r['toolName'].lower(),
                             r['toolCode'], r['locationKey']))
    holders = {}
    for row in rows:
        holder = holders.setdefault(row['holderKey'], {
            'key': row['holderKey'], 'label': row['holder'], 'locType': row['locType'],
            'pending': 0.0, 'tools': 0, '_rentals': set(),
        })
        holder['pending'] += row['pending']
        holder['tools'] += 1
        holder['_rentals'].add(row['rentalId'])
    holder_rows = sorted(holders.values(), key=lambda h: (h['label'].lower(), h['key']))
    for holder in holder_rows:
        holder['rentals'] = len(holder.pop('_rentals'))
    return {'holders': holder_rows, 'rows': rows}


def _rental_holder(rental):
    """``(key, label, loc_type)`` — *who* a rental's tools are out with.

    One site / customer, however many issues it holds: a site with three open
    rentals is still one place to send the truck to, so its key is the project
    (or the customer name), never the rental.
    """
    if rental.renter_type == 'internal' and rental.project_id:
        label = rental.project.name if rental.project else f'Project #{rental.project_id}'
        return f'project:{int(rental.project_id)}', label, LOC_OWN_PROJECT
    label = (rental.customer_name or 'Outside customer').strip()
    return 'customer:' + label.casefold(), label, LOC_CUSTOMER


def rental_picker_rows(eligible_for, limit=RENTAL_PICKER_LIMIT):
    """Rentals a transaction may pick from, as ``(id, label, data)`` options."""
    from hdc.services.tool_txn_types import rental_eligible

    rentals = (ToolRental.query
               .filter(ToolRental.is_void == False)  # noqa: E712
               .order_by(ToolRental.rental_date.desc(), ToolRental.id.desc())
               .limit(limit)
               .all())
    options = []
    for rental in rentals:
        tools_out = float(rental.total_pending_tools or 0)
        amount_due = float(rental.total_pending_amount or 0)
        if not rental_eligible(eligible_for, pending_tools=tools_out,
                               pending_amount=amount_due,
                               billing_type=rental.billing_type or ''):
            continue
        holder_key, holder, _loc_type = _rental_holder(rental)
        status = ('No charge · no rent due' if rental.billing_type == 'no_charge'
                  else f'{amount_due:,.0f} PKR due')
        label = f'{rental.rental_code} · {holder} · {tools_out:,.0f} out · {status}'
        options.append((rental.id, label, {
            'amount': f'{amount_due:.2f}',
            'tools': f'{tools_out:.2f}',
            'holder': holder,
            'holder-key': holder_key,
            'renter_type': rental.renter_type or '',
            'billing-type': rental.billing_type or '',
        }))
    return options


def account_options():
    """Existing cash / bank / company accounts that can receive money."""
    return [(acc.id, f'{acc.name} ({acc.type})') for acc in get_receiving_accounts()]


def damage_reason_options():
    return [(key, label) for key, label in TOOL_SCRAP_REASONS]


def adjustment_reason_options():
    return [(key, label) for key, label in TOOL_DISCOUNT_REASONS]


def location_source_options(sources):
    """One picker entry per real location, not one per rental at that location.

    The value stays a real source key; the browser expands it to the other
    rental keys at that same location and posts exactly the holders ticked.
    """
    locations = {}
    for source in sources:
        row = locations.setdefault(source['location_key'], {
            'key': source['key'], 'holder': source['holder'], 'qty': 0.0,
            'rentals': set(),
        })
        row['qty'] += source['pending']
        row['rentals'].add(source['rental_id'])
    return [(row['key'], f"{row['holder']} · {row['qty']:g} out · {len(row['rentals'])} rental(s)")
            for row in locations.values()]


def txn_form_context():
    """Everything the transaction page renders, in one pass.

    The rental pickers are built from the registry itself: every distinct
    eligibility rule gets its own option list, so a new transaction inherits a
    picker instead of adding a context key.
    """
    # GET renders the last committed write; it never repairs stock silently.
    # The explicit Rebuild action and each transaction own synchronization.
    from hdc.models.projects import Project, Stage
    from hdc.services.tool_txn_types import (
        ELIGIBLE_PENDING_AMOUNT, ELIGIBLE_PENDING_TOOLS, TXN_OPTIONS,
    )

    projects = Project.query.order_by(Project.name.asc()).all()
    from sqlalchemy.orm import joinedload
    stages = (Stage.query.options(joinedload(Stage.project))
              .order_by(Stage.name.asc()).all())

    # One set of rental choices per eligibility rule, so a new transaction
    # inherits the picker list instead of inventing a new one.  Both rules are
    # built: the damage / loss panel picks the issue a write-off happened on
    # (tools still out) even though that is not a rental-bound transaction.
    rental_picker_options = {}
    for rule in (ELIGIBLE_PENDING_TOOLS, ELIGIBLE_PENDING_AMOUNT):
        rental_picker_options[rule] = rental_picker_rows(rule)
    for option in TXN_OPTIONS:
        rule = option['eligible_for']
        if rule and rule not in rental_picker_options:
            rental_picker_options[rule] = rental_picker_rows(rule)

    in_store = in_store_tool_rows()
    transfer_sources = transfer_source_rows()
    # Who still holds what — the return sheet's holder list and its rows.
    returns = return_sheet()
    return {
        'today': _pkt_today().isoformat(),
        'txn_reference': new_txn_reference(),
        'in_store_tools': in_store,
        'return_holders': returns['holders'],
        'return_rows': returns['rows'],
        'tool_options': [
            (tool['id'], f"{tool['name']} ({tool['code']}) — {tool['available_qty']:g} available"
             if tool['code'] else f"{tool['name']} — {tool['available_qty']:g} available")
            for tool in in_store
        ],
        'transfer_sources': transfer_sources,
        'transfer_source_options': location_source_options(transfer_sources),
        'rental_picker_options': rental_picker_options,
        'maintenance_pieces': maintenance_pieces(),
        'receiving_account_options': account_options(),
        'project_options': [(p.id, f'{p.name} ({p.project_code})' if p.project_code else p.name)
                            for p in projects],
        'stage_options': [(s.id, f'{s.name} — {s.project.name if s.project else ""}',
                           {'project-id': s.project_id}) for s in stages],
        'customer_names': known_tool_customers(),
        'damage_reasons': damage_reason_options(),
        'adjustment_reasons': adjustment_reason_options(),
        'store_label': STORE_LABEL,
        'maintenance_label': MAINTENANCE_LABEL,
        'scrap_label': SCRAP_LABEL,
        # The rent-out setup question ("From where?") filters the transfer
        # locations by the holding type the ledger itself uses — a site is an
        # own-project holding, a customer an outside holding.
        'rentout_site_loc': LOC_OWN_PROJECT,
        'rentout_customer_loc': LOC_CUSTOMER,
    }


# --------------------------------------------------------------------------- #
# Returns — one load back from a holder, whatever issues it came off
# --------------------------------------------------------------------------- #
def _return_line_serials(item, raw_serial_ids, claimed, allowed_ids=None):
    """The piece markings a return line posts, as real out-of-store serials.

    ``claimed`` carries the ids an earlier line already took, so one piece can
    never be returned twice inside the same submission.
    """
    wanted = _parse_serial_ids(raw_serial_ids)
    if not wanted:
        return True, '', []
    chosen = []
    for serial_id in wanted:
        if serial_id in claimed:
            return False, 'One of the selected pieces is ticked twice on this return.', []
        serial = db.session.get(ToolSerial, serial_id)
        if (not serial or int(serial.tool_id) != int(item.tool_id)
                or getattr(serial, 'is_scrapped', False) or serial.status == 'scrapped'):
            return False, 'One of the selected piece markings does not belong to this tool.', []
        if serial.is_in_store:
            return False, (f'{serial.serial_number} is already in the store — '
                           f'reload the page and retick the pieces still out.'), []
        if serial.current_rental_id != int(item.rental_id):
            return False, (f'{serial.serial_number} is out on another issue — reload '
                           f'the page and retick the pieces still out.'), []
        if serial.current_rental_item_id is not None and int(serial.current_rental_item_id) != int(item.id):
            return False, f'{serial.serial_number} belongs to another tool line on this issue.', []
        if allowed_ids is not None and int(serial.id) not in allowed_ids:
            return False, f'{serial.serial_number} is not at the selected return location.', []
        claimed.add(int(serial_id))
        chosen.append(serial)
    return True, '', chosen


def record_return(*, actor_id, lines, return_type='partial', return_date=None,
                  payment_type='no_payment', amount_paid=0.0, discount_amount=0.0,
                  discount_reason='goodwill', discount_notes='', settle_rental_id=None,
                  received_to_account_id=None, payment_mode='cash',
                  payment_reference='', notes='', reference='', responsible='',
                  commit=True):
    """Bring tools back from one holder — off every issue they came from.

    The return sheet lists everything a site or customer still holds, so one
    submission can close several of its issues at once.  Each issue still gets
    its own return record (``ToolRentalReturn`` with one line per tool), because
    that is the audit trail the rental page, the tracking chain and the
    movement log read; the whole submission is written in **one** database
    transaction, so a refused line writes nothing at all.

    ``lines`` is a list of ``{rental_id, rental_item_id, qty, serial_ids,
    condition}``.  A line with ``serial_ids`` returns exactly those piece
    markings; otherwise ``qty`` does, and a *full* return takes whatever is
    still out on the row.

    Money is optional and always settles **one** issue — ``settle_rental_id``,
    defaulting to the only issue in the submission.  Cash, discount and the
    credit flag follow the same rules the per-rental return endpoint applies:
    never more than the issue still owes, and a no-charge issue collects
    nothing whatever the form had left on it.

    Returns ``(ok, message, summary)``.
    """
    when = _parse_when(return_date)
    return_type = 'full' if str(return_type or '').strip().lower() == 'full' else 'partial'

    wanted = []
    for raw in lines or ():
        try:
            rental_id = int(raw.get('rental_id') or 0)
            item_id = int(raw.get('rental_item_id') or 0)
        except (TypeError, ValueError):
            return False, 'The return lines could not be read — reload the page.', {}
        if rental_id and item_id:
            wanted.append((rental_id, item_id, raw))
    if not wanted:
        return False, ('Tick the tools coming back, or choose Full Return to bring '
                       'everything still out back.'), {}

    rental_ids = sorted({rental_id for rental_id, _item_id, _raw in wanted})
    rentals = {int(rental.id): rental
               for rental in ToolRental.query.filter(ToolRental.id.in_(rental_ids)).all()}
    for rental_id in rental_ids:
        rental = rentals.get(rental_id)
        if not rental:
            return False, f'Issue #{rental_id} no longer exists — reload the page.', {}
        if rental.is_void:
            return False, (f'{rental.rental_code} is voided, so its tools cannot come '
                           f'back through a return.'), {}

    ensure_all_tools_serials(created_by=actor_id, commit=False)
    positions = tool_ledger()
    location_rows = holdings_by_item(positions)
    claimed = set()
    groups = {}
    claimed_at_location = {}
    total_pieces = 0.0
    for rental_id, item_id, raw in wanted:
        rental = rentals[rental_id]
        item = db.session.get(ToolRentalItem, item_id)
        if not item or int(item.rental_id or 0) != int(rental.id):
            return False, (f'That tool line is not on {rental.rental_code} — reload the '
                           f'page and retick.'), {}
        tool = item.tool
        name = tool.name if tool else f'Tool #{item.tool_id}'
        pending = max(0.0, _flt(item.qty_pending))
        if pending <= EPS:
            return False, f'{name} is already back from {rental.rental_code}.', {}

        sources = location_rows.get(int(item.id), [])
        places = {holding_key(h['loc_type'], h.get('project_id'), h.get('stage_id'),
                              h.get('customer_name')).casefold(): h for h in sources}
        requested_key = str(raw.get('from_location_key') or raw.get('location_key') or '').strip()
        if requested_key:
            source = places.get(requested_key.casefold())
            if source is None:
                return False, f'{name} is no longer at that return location — reload the sheet.', {}
        elif len(places) == 1:
            source = next(iter(places.values()))
        else:
            return False, f'Choose the site/customer the {name} pieces are coming from.', {}
        from_key = holding_key(source['loc_type'], source.get('project_id'),
                               source.get('stage_id'), source.get('customer_name'))
        here = sum(_flt(h['qty']) for h in sources
                   if holding_key(h['loc_type'], h.get('project_id'), h.get('stage_id'),
                                  h.get('customer_name')).casefold() == from_key.casefold())
        allowed_ids = {int(s.id) for s in serials_at_holding(
            item, location_key=from_key, ledger=positions)}
        ok, message, serials = _return_line_serials(item, raw.get('serial_ids'), claimed, allowed_ids)
        if not ok:
            return False, message, {}
        if serials:
            qty = float(len(serials))
        elif return_type == 'full':
            qty = here
        else:
            qty = max(0.0, _flt(raw.get('qty')))
        if qty <= 0:
            continue
        slot = (int(item.id), from_key.casefold())
        already = claimed_at_location.get(slot, 0.0)
        if qty > pending + EPS or already + qty > here + EPS:
            return False, integrity_message(
                'A return cannot bring back more than is still out.',
                f'{qty:g} × {name} coming back, but only {here - already:g} is still out '
                f'at {source["label"]} on {rental.rental_code}.'), {}
        claimed_at_location[slot] = already + qty

        condition = str(raw.get('condition') or '').strip()
        if serials:
            numbers = ', '.join(serial.serial_number for serial in serials)
            if numbers not in condition:
                condition = (f'{condition} [Serials: {numbers}]'.strip()
                             if condition else f'Serials: {numbers}')
        groups.setdefault(rental_id, []).append((item, qty, condition[:300], serials,
                                                  from_key, source['label']))
        total_pieces += qty

    if not groups:
        return False, 'No tools were marked to come back on this return.', {}

    # ---- the optional settlement, always against ONE issue -----------------
    payment_type = str(payment_type or 'no_payment').strip().lower()
    if payment_type not in ('full', 'partial', 'credit', 'no_payment'):
        payment_type = 'no_payment'
    amount = max(0.0, _flt(amount_paid))
    discount = max(0.0, _flt(discount_amount))

    settle_rental = None
    if payment_type != 'no_payment' or amount > 0 or discount > 0:
        try:
            settle_id = int(settle_rental_id or 0)
        except (TypeError, ValueError):
            settle_id = 0
        if not settle_id and len(rental_ids) == 1:
            settle_id = rental_ids[0]
        settle_rental = rentals.get(settle_id)
        if settle_rental is None:
            return False, ('Choose the issue the rent is being collected against — '
                           'money can only settle one issue at a time.'), {}
        if (settle_rental.billing_type or '').strip().lower() == 'no_charge':
            # A free issue has no settlement, even if the form kept a stale
            # cash selection from another holder.
            payment_type, amount, discount = 'no_payment', 0.0, 0.0
        elif payment_type == 'no_payment':
            amount = 0.0

    received_to_account_id = int(received_to_account_id or 0) or None
    receiving_account = None
    if amount > 0:
        if payment_type == 'full':
            amount = max(0.0, _flt(settle_rental.total_pending_amount))
        owed = max(0.0, _flt(settle_rental.total_pending_amount))
        if amount > owed + 0.001:
            return False, (f'Cash {amount:,.2f} PKR is more than the {owed:,.2f} PKR '
                           f'{settle_rental.rental_code} still owes.'), {}
        if not received_to_account_id:
            return False, ('Choose the cash / bank account receiving the money.'), {}
        from hdc.models.accounts import Account
        receiving_account = db.session.get(Account, received_to_account_id)
        if (not receiving_account or receiving_account.is_void
                or str(receiving_account.status or 'active').lower() != 'active'
                or str(receiving_account.type or '').lower() not in ('company', 'cash', 'bank')):
            return False, 'Select a valid Cash/Bank receiving account for the payment.', {}
    if discount > 0:
        owed = max(0.0, _flt(settle_rental.total_pending_amount) - amount)
        if discount > owed + 0.001:
            return False, (f'The discount {discount:,.2f} PKR is more than the '
                           f'{owed:,.2f} PKR left on {settle_rental.rental_code} '
                           f'after this payment.'), {}

    # ---- record one return per issue, all inside this transaction ----------
    created = []
    for rental_id in sorted(groups):
        rental = rentals[rental_id]
        group = groups[rental_id]
        pieces = sum(line[1] for line in group)
        is_settled = settle_rental is not None and int(rental.id) == int(settle_rental.id)
        record = ToolRentalReturn(
            rental_id=rental.id,
            return_date=when,
            return_type=return_type,
            payment_type=payment_type if is_settled else 'no_payment',
            total_tools_returned=pieces,
            amount_paid=amount if is_settled else 0.0,
            notes=stamp_reference(
                (notes or '').strip() or f'Return from {rental.current_location_label or "holder"}',
                reference, responsible=responsible),
            created_by=actor_id,
        )
        db.session.add(record)
        db.session.flush()

        for item, qty, condition, serials, from_key, from_label in group:
            db.session.add(ToolRentalReturnItem(
                return_id=record.id,
                rental_item_id=item.id,
                tool_id=item.tool_id,
                qty_returned=qty,
                condition_notes=condition,
                from_location_key=from_key,
            ))
            item.qty_returned = _flt(item.qty_returned) + qty
            item.qty_pending = max(0.0, _flt(item.qty_rented) - item.qty_returned)
            if serials:
                return_serials_from_rental(
                    rental_id=rental.id, serial_ids=[int(s.id) for s in serials],
                    return_id=record.id, created_by=actor_id,
                    notes=f'Return {record.id} from {from_label}', when=when, commit=False)
            create_movement_log(
                tool_id=int(item.tool_id), rental_id=rental.id,
                movement_type='return_in', from_label=from_label,
                to_label=STORE_LABEL, qty=qty, return_id=record.id,
                notes=f'Return {record.id} from {from_label}: {qty:g} pcs', when=when,
            )
        created.append((rental, record, pieces))

    # ---- cash, discount and credit, on the settled issue only --------------
    settle_record = next((record for rental, record, _pieces in created
                          if settle_rental is not None
                          and int(rental.id) == int(settle_rental.id)), None)
    paid_total = 0.0
    if settle_record is not None and (amount > 0 or discount > 0):
        payment = ToolRentalPayment(
            rental_id=settle_rental.id,
            return_id=settle_record.id,
            payment_date=when,
            amount=amount,
            discount=discount,
            payment_mode=(payment_mode or 'cash').strip().lower(),
            received_to_account_id=int(receiving_account.id) if receiving_account else None,
            reference=(payment_reference or '').strip(),
            notes=f'Payment on return {settle_record.id}',
            created_by=actor_id,
        )
        db.session.add(payment)
        db.session.flush()
        if amount > 0:
            ok_acc, message, _links = post_tool_rental_payment_to_accounts(
                payment, rental=settle_rental, commit=False)
            if not ok_acc:
                db.session.rollback()
                return False, message or 'Unable to post the rent receipt in accounts.', {}
            paid_total = amount
        if discount > 0:
            _row, ok_disc, message = record_tool_rental_discount(
                settle_rental.id, discount, discount_date=when,
                reason=(discount_reason or 'goodwill').strip().lower(),
                notes=(discount_notes or '').strip(),
                payment_id=payment.id, return_id=settle_record.id,
                created_by=actor_id, commit=False)
            if not ok_disc:
                db.session.rollback()
                return False, message or 'Unable to post the discount.', {}

    for rental_id in sorted(groups):
        recalc_rental_totals(rental_id)
    if settle_rental is not None and payment_type == 'credit':
        if (float(settle_rental.total_pending_amount or 0) > 0
                and (settle_rental.billing_type or '').strip().lower() != 'no_charge'):
            settle_rental.payment_status = 'credit'

    summary = {
        'rental_ids': sorted(groups),
        'return_ids': [int(record.id) for _rental, record, _pieces in created],
        'qty': total_pieces,
        'amount_paid': paid_total,
        'discount': discount if settle_record is not None else 0.0,
        'settle_rental_id': int(settle_rental.id) if settle_rental else None,
    }
    if commit:
        db.session.commit()
    else:
        db.session.flush()

    codes = ', '.join(rental.rental_code for rental, _record, _pieces in created)
    message = (f'Return recorded: {total_pieces:g} piece(s) came back from '
               f'{created[0][0].current_location_label or "the holder"} '
               f'across {len(created)} issue(s) ({codes}).')
    if paid_total > 0:
        message += (f' {paid_total:,.2f} PKR received into '
                    f'{receiving_account.name if receiving_account else "-"}' + '.')
    if summary['discount'] > 0:
        message += f' {summary["discount"]:,.2f} PKR discounted.'
    if payment_type == 'credit':
        message += ' The balance stays on credit.'
    return True, message, summary


# --------------------------------------------------------------------------- #
# Damage / loss — from the store, or straight off a holder
# --------------------------------------------------------------------------- #
def record_damage_loss(*, actor_id, source='store', tool_id=None, qty=0.0,
                       serial_ids=None, reason='damaged', txn_date=None,
                       reference='', notes='', rental_id=None,
                       rental_item_id=None):
    """Write off damaged / lost pieces, wherever they physically are.

    * ``source='store'`` — the pieces are in the warehouse: the existing scrap
      register does the work (:func:`record_tool_scrap`).
    * ``source='rental'`` — the pieces are out with a site or customer.  They
      cannot be scrapped from there (the store has nothing to give), so the
      transaction does what a person would do in two steps, atomically in one
      database transaction: return the pieces off the rental line and then
      write them off.  Owned stock and "out" drop by the same quantity, the
      store side is untouched, and the ledger balance still holds —
      ``owned = in_store + out``.

    Returns ``(ok, message, summary)``.
    """
    when = _parse_when(txn_date)
    if source == 'rental':
        return _record_rental_damage_loss(
            actor_id=actor_id, rental_id=rental_id, rental_item_id=rental_item_id,
            qty=qty, serial_ids=serial_ids, reason=reason, when=when,
            reference=reference, notes=notes)

    if not tool_id:
        return False, 'Choose the tool that was damaged or lost.', {}
    ok, msg, scrap = record_tool_scrap(
        tool_id=tool_id, qty=qty, reason=reason, scrap_date=when,
        reference=reference, notes=stamp_reference(notes, reference),
        created_by=actor_id, commit=False, serial_ids=serial_ids)
    if not ok:
        db.session.rollback()
        return False, msg, {}
    db.session.commit()
    tool = db.session.get(Tool, int(tool_id))
    return True, (
        f'{scrap.qty:g} {tool.unit if tool else "pcs"} of '
        f'{tool.name if tool else "tool"} written off ({scrap.reason_label}). '
        f'{tool.total_quantity:g} still owned.'
    ), {'scrap_id': scrap.id, 'qty': scrap.qty, 'scrap_code': scrap.scrap_code}


def _record_rental_damage_loss(*, actor_id, rental_id, rental_item_id, qty,
                               serial_ids, reason, when, reference, notes):
    rental = db.session.get(ToolRental, int(rental_id or 0))
    if not rental or rental.is_void:
        return False, 'Choose the rental / holder the damage happened on.', {}
    item = db.session.get(ToolRentalItem, int(rental_item_id or 0))
    if not item or int(item.rental_id) != int(rental.id):
        return False, 'Choose the tool line the damage happened on.', {}
    tool = db.session.get(Tool, int(item.tool_id))
    if not tool:
        return False, 'Tool not found.', {}

    ensure_tool_serials(tool=tool, created_by=actor_id, commit=False)
    pending = max(0.0, _flt(item.qty_pending))
    if pending <= EPS:
        return False, f'{tool.name} has nothing still out on {rental.rental_code}.', {}
    positions = tool_ledger()
    sources = [h for h in holdings_by_item(positions).get(int(item.id), [])
               if _flt(h.get('qty')) > EPS]
    if not sources:
        return False, f'{tool.name} is not recorded as still out on {rental.rental_code}.', {}

    chosen = []
    if serial_ids:
        parsed = _parse_serial_ids(serial_ids)
        if not parsed:
            return False, 'The selected serial markings are invalid.', {}
        for sid in parsed:
            serial = db.session.get(ToolSerial, sid)
            if (not serial or int(serial.tool_id) != int(tool.id)
                    or getattr(serial, 'is_scrapped', False)):
                return False, f'Invalid serial selection for {tool.name}.', {}
            if serial.is_in_store:
                return False, (f'{serial.serial_number} is in the store — record it '
                               f'through the store write-off instead.'), {}
            if serial.current_rental_id and int(serial.current_rental_id) != int(rental.id):
                return False, (f'{serial.serial_number} belongs to another rental '
                               f'({serial.current_rental_id}).'), {}
            if serial.current_rental_item_id not in (None, int(item.id)):
                return False, f'{serial.serial_number} belongs to another line of this rental.', {}
            if sid not in [s.id for s in chosen]:
                chosen.append(serial)
        labels = {(s.current_location_label or '').strip() for s in chosen}
        if len(labels) != 1 or not next(iter(labels)):
            return False, 'Choose the damaged/lost pieces from one recorded holder.', {}
        from_label = next(iter(labels))
        qty = float(len(chosen))
    else:
        qty = _flt(qty)
        if qty <= EPS:
            return False, 'Quantity must be greater than 0.', {}
        labels = {(h.get('label') or '').strip() for h in sources}
        preferred = (rental.current_location_label or '').strip()
        if preferred in labels:
            from_label = preferred
        elif len(labels) == 1:
            from_label = next(iter(labels))
        else:
            return False, ('This line is split between holders. Select the exact damaged/lost '
                           'pieces so the write-off comes from the correct site.'), {}
        chosen = get_out_of_store_serials(
            tool_id=int(tool.id), rental_id=int(rental.id), rental_item_id=int(item.id),
            location_label=from_label)[:int(round(qty))]
        if len(chosen) + EPS < qty:
            return False, f'Only {len(chosen)} marked piece(s) of {tool.name} are at {from_label}.', {}
    here = sum(_flt(h.get('qty')) for h in sources
               if (h.get('label') or '').strip() == from_label)
    if qty > pending + EPS or qty > here + EPS:
        return False, integrity_message(
            'Damaged / lost quantity is more than this holder has out.',
            f'Only {here:g} {tool.unit} of {tool.name} is still out at {from_label} on '
            f'{rental.rental_code}.'), {}
    source = next((h for h in sources if (h.get('label') or '').strip() == from_label), sources[0])
    from_key = holding_key(source['loc_type'], source.get('project_id'),
                           source.get('stage_id'), source.get('customer_name'))

    reason_key = (reason or 'damaged').strip().lower()
    if reason_key not in SCRAP_REASON_LABELS:
        reason_key = 'damaged'
    reason_label = SCRAP_REASON_LABELS.get(reason_key, reason_key)

    # Lift the quantity off the rental, but do NOT send the exact pieces through
    # a store return first: they were broken/lost at the holder.  The scrap write
    # below moves those same identities straight from that holder to write-off.
    ret = ToolRentalReturn(
        rental_id=rental.id, return_date=when, return_type='partial',
        payment_type='no_payment', total_tools_returned=qty, amount_paid=0.0,
        notes=stamp_reference(
            f'{reason_label} reported at {from_label}; written off, not returned to store. '
            f'{notes}'.strip(), reference),
        created_by=actor_id)
    db.session.add(ret)
    db.session.flush()
    db.session.add(ToolRentalReturnItem(
        return_id=ret.id, rental_item_id=item.id, tool_id=int(tool.id), qty_returned=qty,
        from_location_key=from_key,
        condition_notes=f'{reason_label} — write-off via transaction {reference or ret.id}'))
    item.qty_returned = _flt(item.qty_returned) + qty
    item.qty_pending = max(0.0, _flt(item.qty_rented) - item.qty_returned)
    create_movement_log(
        tool_id=int(tool.id), rental_id=rental.id, movement_type='scrap_out',
        from_label=from_label, to_label=SCRAP_LABEL, qty=qty, return_id=ret.id,
        notes=stamp_reference(f'{reason_label} at {from_label} — written off', reference),
        when=when)

    ok, msg, scrap = record_tool_scrap(
        tool_id=int(tool.id), qty=qty, reason=reason_key, scrap_date=when,
        reference=reference,
        notes=stamp_reference(
            f'{reason_label} at {from_label} (rental {rental.rental_code}); '
            f'reported via {reference or "tool transaction"}. {notes}'.strip(), ''),
        created_by=actor_id, commit=False,
        serial_ids=[s.id for s in chosen] or None, from_holder=bool(chosen),
        from_label=from_label if chosen else None)
    if not ok:
        db.session.rollback()
        return False, msg, {}
    recalc_rental_totals(rental.id)
    db.session.commit()
    return True, (
        f'{qty:g} {tool.unit} of {tool.name} reported {reason_label.lower()} on '
        f'{rental.rental_code} and written off. {scrap.scrap_code} recorded; '
        f'owned stock is now {tool.total_quantity:g}.'
    ), {'scrap_id': scrap.id, 'return_id': ret.id, 'qty': qty}


# --------------------------------------------------------------------------- #
# Maintenance — out of the available pool, still owned and traceable
# --------------------------------------------------------------------------- #
def send_to_maintenance(*, actor_id, serial_ids, txn_date=None, reference='',
                        notes='', reason='', tool_id=None, qty=0.0):
    """Move pieces from the store to the workshop.

    Nothing about ownership or quantity changes — that is the point.  The piece
    keeps its tool's ``total_quantity`` (it is not lost), but stops being
    offered for rent and carries a maintenance status plus a movement-history
    row, so "where is it and who had it last" always has an answer.
    """
    when = _parse_when(txn_date)
    parsed = _parse_serial_ids(serial_ids)
    if not parsed and tool_id:
        # Quantity-based send: take the first in-store pieces of that tool.
        tool = db.session.get(Tool, int(tool_id))
        if tool:
            ensure_tool_serials(tool=tool, created_by=actor_id, commit=False)
            wanted = int(round(_flt(qty)))
            pool = [s for s in tool.issuable_serials
                    if (s.status or '') != 'maintenance'
                    and (s.condition or 'good') != 'maintenance']
            parsed = [int(s.id) for s in pool[:max(0, wanted)]]
    if not parsed:
        return False, 'Select at least one piece to send for maintenance.', {}

    serials = ToolSerial.query.filter(ToolSerial.id.in_(parsed)).all()
    found = {int(s.id): s for s in serials}
    missing = [sid for sid in parsed if sid not in found]
    if missing:
        return False, 'One of the selected pieces no longer exists.', {}
    for sid in parsed:
        serial = found[sid]
        if getattr(serial, 'is_scrapped', False) or serial.status == 'scrapped':
            return False, f'{serial.serial_number} is scrapped and cannot be repaired.', {}
        if serial.status == 'maintenance':
            return False, f'{serial.serial_number} is already with the workshop.', {}
        if not serial.is_in_store:
            return False, (f'{serial.serial_number} is out at '
                           f'{serial.current_location_label or "a holder"} — bring it '
                           f'back before sending it for maintenance.'), {}

    moved_by_tool = {}
    for sid in parsed:
        serial = found[sid]
        previous = serial.current_location_label or STORE_LABEL
        serial.condition = 'maintenance'
        serial.status = 'maintenance'
        serial.is_in_store = True            # still owned, still on the store side
        serial.current_location_label = MAINTENANCE_LABEL
        serial.updated_at = _pkt_now_naive()
        serial.updated_by = actor_id
        if notes or reason:
            serial.notes = stamp_reference(
                (f'{reason} | {notes}'.strip(' |')), reference)[:300]
        create_serial_movement(
            serial_id=int(serial.id), movement_type=MOVEMENT_MAINTENANCE_OUT,
            from_label=previous, to_label=MAINTENANCE_LABEL,
            notes=stamp_reference(
                f'Sent for maintenance{f" — {reason}" if reason else ""}. {notes}'.strip(),
                reference),
            created_by=actor_id, when=when)
        moved_by_tool.setdefault(int(serial.tool_id), []).append(serial)

    for tool_id_value, pieces in moved_by_tool.items():
        tool = db.session.get(Tool, tool_id_value)
        create_movement_log(
            tool_id=tool_id_value, rental_id=None,
            movement_type=MOVEMENT_MAINTENANCE_OUT,
            from_label=STORE_LABEL, to_label=MAINTENANCE_LABEL,
            qty=float(len(pieces)),
            notes=stamp_reference(
                f'{len(pieces)} piece(s) sent for maintenance. {notes}'.strip(), reference),
            when=when)
        if tool and _tool_fully_in_maintenance(tool):
            tool.condition = 'maintenance'
            tool.updated_at = _pkt_now_naive()

    db.session.commit()
    return True, (
        f'{len(parsed)} piece(s) sent for maintenance. They stay owned and stay '
        f'traceable, but they are no longer offered for rent.'
    ), {'serial_ids': parsed, 'sent_on': when.isoformat()}


def return_from_maintenance(*, actor_id, serial_ids, condition='good',
                            txn_date=None, reference='', notes='', send_to_scrap=False):
    """Pieces come back from the workshop.

    ``condition='good'`` puts them straight back into the store.  When the
    workshop could not repair a piece, ``condition='damaged'`` (or
    ``send_to_scrap``) hands it to the existing scrap register instead, so the
    write-off is recorded in the one place write-offs live.
    """
    when = _parse_when(txn_date)
    parsed = _parse_serial_ids(serial_ids)
    if not parsed:
        return False, 'Select at least one piece to take back from maintenance.', {}
    serials = ToolSerial.query.filter(ToolSerial.id.in_(parsed)).all()
    found = {int(s.id): s for s in serials}
    if len(found) != len(parsed):
        return False, 'One of the selected pieces no longer exists.', {}

    outcome = (condition or 'good').strip().lower()
    if outcome not in ('good', 'damaged', 'lost'):
        outcome = 'good'
    write_off = bool(send_to_scrap) or outcome in ('damaged', 'lost')

    moved_by_tool = {}
    for sid in parsed:
        serial = found[sid]
        if serial.status != 'maintenance' and (serial.condition or '') != 'maintenance':
            return False, (f'{serial.serial_number} is not recorded as being with the '
                           f'workshop.'), {}
        previous = serial.current_location_label or MAINTENANCE_LABEL
        serial.condition = 'good' if not write_off else outcome
        serial.status = 'in_store'
        serial.is_in_store = True
        serial.updated_at = _pkt_now_naive()
        serial.updated_by = actor_id
        if notes:
            serial.notes = stamp_reference(
                f'Back from maintenance. {notes}'.strip(), reference)[:300]
        create_serial_movement(
            serial_id=int(serial.id),
            movement_type=(MOVEMENT_MAINTENANCE_IN if not write_off else 'maintenance_write_off'),
            from_label=previous,
            to_label=(STORE_LABEL if not write_off else SCRAP_LABEL),
            notes=stamp_reference(
                ('Repaired and back in store.' if not write_off
                 else f'Not repairable ({outcome}) — written off.') + f' {notes}'.strip(),
                reference),
            created_by=actor_id, when=when)
        moved_by_tool.setdefault(int(serial.tool_id), []).append(serial)

    # Pieces the workshop cannot fix go through the ordinary scrap register.
    scrap_details = []
    for tool_id_value, pieces in moved_by_tool.items():
        tool = db.session.get(Tool, tool_id_value)
        create_movement_log(
            tool_id=tool_id_value, rental_id=None,
            movement_type=MOVEMENT_MAINTENANCE_IN,
            from_label=MAINTENANCE_LABEL,
            to_label=(SCRAP_LABEL if write_off else STORE_LABEL),
            qty=float(len(pieces)),
            notes=stamp_reference(
                ('Back from maintenance. ' if not write_off else 'Not repairable — ')
                + f'{len(pieces)} piece(s). {notes}'.strip(), reference),
            when=when)
        if write_off:
            ok, msg, scrap = record_tool_scrap(
                tool_id=tool_id_value, qty=float(len(pieces)),
                reason=('lost' if outcome == 'lost' else 'damaged'),
                scrap_date=when, reference=reference,
                notes=stamp_reference('Not repairable by the workshop. ' + notes, ''),
                created_by=actor_id, commit=False,
                serial_ids=[int(s.id) for s in pieces])
            if not ok:
                db.session.rollback()
                return False, msg, {}
            scrap_details.append(scrap.scrap_code)
        if tool and not write_off and not _tool_fully_in_maintenance(tool):
            tool.condition = 'good'
            tool.updated_at = _pkt_now_naive()

    db.session.commit()
    if write_off:
        return True, (
            f'{len(parsed)} piece(s) came back from maintenance and were written off '
            f'({", ".join(scrap_details)}).'
        ), {'serial_ids': parsed, 'scrapped': True}
    return True, (
        f'{len(parsed)} piece(s) are repaired and back in the store.'
    ), {'serial_ids': parsed, 'scrapped': False}


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _parse_when(raw):
    if raw in (None, ''):
        return _pkt_today()
    if hasattr(raw, 'isoformat'):
        return raw
    from datetime import datetime
    try:
        return datetime.strptime(str(raw).strip(), '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return _pkt_today()


def _parse_serial_ids(raw):
    if raw in (None, '', []):
        return []
    if isinstance(raw, str):
        tokens = [t.strip() for t in raw.replace(';', ',').split(',')]
    else:
        tokens = []
        for chunk in raw:
            tokens.extend(str(chunk or '').replace(';', ',').split(','))
    out = []
    for token in tokens:
        token = token.strip()
        if token.isdigit() and int(token) not in out:
            out.append(int(token))
    return out


def _tool_fully_in_maintenance(tool) -> bool:
    """True when every piece we still own of this tool is with the workshop."""
    active = [s for s in (tool.serials or [])
              if not getattr(s, 'is_scrapped', False) and s.status != 'scrapped']
    if not active:
        return False
    return all((s.status or '') == 'maintenance' for s in active)
