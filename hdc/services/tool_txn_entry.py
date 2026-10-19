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
* implements the three physical movements that previously had no route:

  - **damage / loss reported on a holder** — the piece is returned off the
    rental line and written off through the existing scrap register, in ONE
    database transaction, so ``owned = in_store + out`` still holds;
  - **send for maintenance** — pieces leave the available pool but stay owned
    and stay traceable (serial + movement history);
  - **return from maintenance** — pieces come back to the store, or are
    written off if the workshop could not repair them.

Everything else (rent out, issue to an own site, transfer between sites or
customers, partial/full/store returns, rent payments and credit adjustments)
posts to the existing endpoints — ``hdc_tool_rental_create``,
``hdc_tool_rental_return``, ``hdc_tool_rental_payment`` and
``hdc_tool_rental_add_discount`` — with the same field names those routes
already accept, so the money and tool rules live in one place.

Layer: ``services`` — may import models, utils and other services.
"""

from __future__ import annotations

import secrets

from sqlalchemy import func, or_

from hdc.extensions import db
from hdc.models.tool_rental import (
    TOOL_DISCOUNT_REASONS, TOOL_SCRAP_REASONS, Tool, ToolCategory, ToolMovementLog,
    ToolMovementLog, ToolPurchase, ToolRental, ToolRentalDiscount, ToolRentalItem,
    ToolRentalPayment, ToolRentalReturn, ToolRentalReturnItem, ToolRentalTransfer,
    ToolScrap, ToolSerial,
)
from hdc.services.record_permissions import integrity_message
from hdc.services.tool_rental import (
    EPS, SCRAP_LABEL, STORE_LABEL, create_movement_log, create_serial_movement,
    ensure_all_tools_serials, ensure_tool_serials, get_receiving_accounts,
    known_tool_customers, recalc_rental_totals, record_tool_scrap,
    return_serials_from_rental, tool_available_for_integrity, get_out_of_store_serials,
)
from hdc.services.tool_tracking import LOC_CUSTOMER, LOC_OWN_PROJECT, tool_ledger
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
        serials = [s for s in tool.in_store_serials
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
    for tool_row in tool_ledger()['tools']:
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
            for serial in get_out_of_store_serials(tool_id=int(item.tool_id),
                                                   rental_id=rental_id,
                                                   rental_item_id=int(item.id)):
                if any(s['id'] == int(serial.id) for s in line['serials']):
                    continue
                if len(line['serials']) >= int(round(line['qty'] or 0)):
                    break
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
        holder = (rental.project.name if rental.renter_type == 'internal' and rental.project
                  else (rental.customer_name or 'Outside customer'))
        status = ('No charge · no rent due' if rental.billing_type == 'no_charge'
                  else f'{amount_due:,.0f} PKR due')
        label = f'{rental.rental_code} · {holder} · {tools_out:,.0f} out · {status}'
        options.append((rental.id, label, {
            'amount': f'{amount_due:.2f}',
            'tools': f'{tools_out:.2f}',
            'holder': holder,
            'holder-key': (f'project:{rental.project_id}'
                           if rental.renter_type == 'internal' and rental.project_id
                           else 'customer:' + holder.strip().casefold()),
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
    ensure_all_tools_serials(commit=False)
    from hdc.models.projects import Project, Stage
    from hdc.services.tool_txn_types import TXN_OPTIONS

    projects = Project.query.order_by(Project.name.asc()).all()
    from sqlalchemy.orm import joinedload
    stages = (Stage.query.options(joinedload(Stage.project))
              .order_by(Stage.name.asc()).all())

    # One set of rental choices per eligibility rule the registry uses, so a
    # new transaction inherits the picker list instead of inventing a new one.
    rental_picker_options = {}
    for option in TXN_OPTIONS:
        rule = option['eligible_for']
        if rule and rule not in rental_picker_options:
            rental_picker_options[rule] = rental_picker_rows(rule)

    in_store = in_store_tool_rows()
    transfer_sources = transfer_source_rows()
    return {
        'today': _pkt_today().isoformat(),
        'txn_reference': new_txn_reference(),
        'in_store_tools': in_store,
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

    # Which exact pieces were lost / broken?
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
            if sid not in [s.id for s in chosen]:
                chosen.append(serial)
        qty = float(len(chosen))
    else:
        qty = _flt(qty)
        if qty <= EPS:
            return False, 'Quantity must be greater than 0.', {}
        if qty > pending + EPS:
            return False, integrity_message(
                'Damaged / lost quantity is more than this rental has out.',
                f'Only {pending:g} {tool.unit} of {tool.name} is out on '
                f'{rental.rental_code}.'), {}
        chosen = get_out_of_store_serials(tool_id=int(tool.id), rental_id=int(rental.id),
                                          rental_item_id=int(item.id))[:int(round(qty))]
        qty = float(len(chosen)) if chosen else _flt(qty)

    reason_key = (reason or 'damaged').strip().lower()
    if reason_key not in SCRAP_REASON_LABELS:
        reason_key = 'damaged'
    reason_label = SCRAP_REASON_LABELS.get(reason_key, reason_key)
    from_label = rental.current_location_label or 'Site'

    # 1. Off the rental line — the piece is no longer "out".
    ret = ToolRentalReturn(
        rental_id=rental.id,
        return_date=when,
        return_type='partial',
        payment_type='no_payment',
        total_tools_returned=qty,
        amount_paid=0.0,
        notes=stamp_reference(
            f'{reason_label} reported at {from_label}; written off, not returned to store. '
            f'{notes}'.strip(), reference),
        created_by=actor_id,
    )
    db.session.add(ret)
    db.session.flush()
    db.session.add(ToolRentalReturnItem(
        return_id=ret.id,
        rental_item_id=item.id,
        tool_id=int(tool.id),
        qty_returned=qty,
        condition_notes=f'{reason_label} — write-off via transaction {reference or ret.id}',
    ))
    item.qty_returned = _flt(item.qty_returned) + qty
    item.qty_pending = max(0.0, _flt(item.qty_rented) - item.qty_returned)
    if chosen:
        return_serials_from_rental(
            rental_id=rental.id, serial_ids=[s.id for s in chosen], return_id=ret.id,
            created_by=actor_id, commit=False)
    create_movement_log(
        tool_id=int(tool.id), rental_id=rental.id, movement_type='return_in',
        from_label=from_label, to_label=STORE_LABEL, qty=qty, return_id=ret.id,
        notes=stamp_reference(f'{reason_label} at {from_label} — written off', reference),
        when=when,
    )

    # 2. Write the pieces off the stock register (store side, unchanged by step 1).
    ok, msg, scrap = record_tool_scrap(
        tool_id=int(tool.id), qty=qty, reason=reason_key, scrap_date=when,
        reference=reference,
        notes=stamp_reference(
            f'{reason_label} at {from_label} (rental {rental.rental_code}); '
            f'reported via {reference or "tool transaction"}. {notes}'.strip(), ''),
        created_by=actor_id, commit=False,
        serial_ids=[s.id for s in chosen] or None)
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
            pool = [s for s in tool.in_store_serials
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
