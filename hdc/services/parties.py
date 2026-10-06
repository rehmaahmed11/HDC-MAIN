"""Parties directory + per-party financial ledger.

The sidebar Parties module is the counterparty directory
(``hdc_cash_flow_party``).  Money itself lives in the unified ledger
(``hdc_account_txn``) — a payment tagged with a party name is one row, so
the party page and Accounts → All Entries cannot disagree.

This module:

* groups parties into filterable categories (loan / rental / worker / client /
  supplier / other);
* batches In / Out / balance per name from the ledger (no N+1);
* builds the chronological party statement (date+time, running balance).
"""

from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import joinedload

from hdc.extensions import db
from hdc.models.accounts import AccountTransaction
from hdc.models.cashflow import CashFlowParty
from hdc.models.loans import Loan
from hdc.models.tool_rental import ToolRental
from hdc.models.workforce import LabourLedger, Worker
from hdc.services.cashflow_register import (
    BUCKETED_PARTY_TYPES, CLIENT_PARTY_TYPES, LOAN_PARTY_TYPES, PARTY_TYPES,
    RENTAL_PARTY_TYPES, SUPPLIER_PARTY_TYPES, WORKER_PARTY_TYPES,
    party_type_label,
)
from hdc.services.ledger import _worker_payable_snapshots
from hdc.utils.dates import _pkt_today
from hdc.utils.normalize import _normalize_name_ci

#: ``(key, label, icon, kpi colour, hint)`` — KPI cards and the category filter.
PARTY_CATEGORIES = (
    ('loan', 'Loan Parties', 'fa-money-bill-transfer', 'kpi-blue',
     'Loan Money In / Out in the CF Register offer only these people.'),
    ('rental', 'External Customers', 'fa-screwdriver-wrench', 'kpi-orange',
     'HDC Tools rental customers — an external rental syncs its name here.'),
    ('worker', 'Workers', 'fa-hard-hat', 'kpi-green',
     'Workers synced from the Workers module (advances, payments, tips).'),
    ('client', 'Clients / Owners', 'fa-building', 'kpi-indigo',
     'Project owners and clients — the people whose projects we build.'),
    ('supplier', 'Suppliers / Vendors', 'fa-truck-field', 'kpi-slate',
     'Material, tool and service suppliers we buy from.'),
    ('other', 'Other Parties', 'fa-users', 'kpi-purple',
     'Office staff, subcontractors and everyone else.'),
)

PARTY_CATEGORY_KEYS = tuple(item[0] for item in PARTY_CATEGORIES)


def party_name_key(name):
    """Case-insensitive identity used to match ledger rows to a party."""
    return (_normalize_name_ci(name) or '').lower()


def party_category(party_type):
    """Which filter bucket a ``party_type`` belongs to.

    Clients (project owners) and suppliers used to fall through to ``other``,
    which made their ledger read "Other Parties" even though the badge next to
    it said *Client / Owner* or *Supplier / Vendor*; they now have buckets of
    their own.  Only office staff, subcontractors and untyped names remain in
    the catch-all.
    """
    ptype = (party_type or 'other').strip().lower() or 'other'
    if ptype in LOAN_PARTY_TYPES:
        return 'loan'
    if ptype in RENTAL_PARTY_TYPES:
        return 'rental'
    if ptype in WORKER_PARTY_TYPES:
        return 'worker'
    if ptype in CLIENT_PARTY_TYPES:
        return 'client'
    if ptype in SUPPLIER_PARTY_TYPES:
        return 'supplier'
    return 'other'


def category_label(key):
    for item in PARTY_CATEGORIES:
        if item[0] == key:
            return item[1]
    return 'All Parties'


def _txn_direction(tx_type):
    from hdc.services.accounts import _account_tx_direction_for_type
    return _account_tx_direction_for_type(tx_type)


def party_money_by_name():
    """``{name_key: {in, out, count, balance}}`` from non-void ledger rows."""
    rows = (db.session.query(
                func.lower(func.trim(func.coalesce(AccountTransaction.party_name, ''))),
                AccountTransaction.type,
                func.coalesce(func.sum(AccountTransaction.amount), 0.0),
                func.count(AccountTransaction.id))
            .filter(AccountTransaction.is_void == False,  # noqa: E712
                    AccountTransaction.party_name.isnot(None),
                    func.trim(func.coalesce(AccountTransaction.party_name, '')) != '')
            .group_by(
                func.lower(func.trim(func.coalesce(AccountTransaction.party_name, ''))),
                AccountTransaction.type)
            .all())
    out = {}
    for key, tx_type, amount, count in rows:
        if not key:
            continue
        bucket = out.setdefault(key, {'in': 0.0, 'out': 0.0, 'count': 0})
        amt = float(amount or 0.0)
        direction = _txn_direction(tx_type)
        if direction == 'receive':
            bucket['in'] += amt
        elif direction == 'pay':
            bucket['out'] += amt
        bucket['count'] += int(count or 0)
    for bucket in out.values():
        bucket['balance'] = float(bucket['in'] - bucket['out'])
    return out


def _loan_counts():
    rows = (db.session.query(func.lower(func.trim(Loan.party_name)),
                             func.count(Loan.id))
            .filter(Loan.status != 'void')
            .group_by(func.lower(func.trim(Loan.party_name)))
            .all())
    return {key: int(count) for key, count in rows if key}


def _rental_stats():
    rows = (db.session.query(func.lower(func.trim(ToolRental.customer_name)),
                             func.count(ToolRental.id),
                             func.coalesce(func.sum(ToolRental.total_amount), 0.0),
                             func.coalesce(func.sum(ToolRental.total_paid), 0.0),
                             func.coalesce(func.sum(ToolRental.total_discount), 0.0))
            .filter(ToolRental.renter_type == 'external',
                    ToolRental.is_void == False,  # noqa: E712
                    ToolRental.customer_name.isnot(None),
                    func.trim(func.coalesce(ToolRental.customer_name, '')) != '')
            .group_by(func.lower(func.trim(ToolRental.customer_name)))
            .all())
    stats = {}
    for key, count, amount, paid, discount in rows:
        if not key:
            continue
        due = max(0.0, float(amount or 0.0) - float(paid or 0.0) - float(discount or 0.0))
        stats[key] = {'count': int(count), 'paid': float(paid or 0.0), 'due': due}
    return stats


def _worker_stats():
    workers = Worker.query.all()
    if not workers:
        return {}
    by_name = {}
    for worker in workers:
        key = party_name_key(worker.name)
        if key:
            by_name[key] = worker
    snaps = _worker_payable_snapshots([w.id for w in workers])
    rows = (db.session.query(LabourLedger.worker_id,
                             LabourLedger.entry_type,
                             func.coalesce(func.sum(LabourLedger.amount), 0.0))
            .filter(LabourLedger.is_void == False)  # noqa: E712
            .group_by(LabourLedger.worker_id, LabourLedger.entry_type)
            .all())
    counts = {}
    for wid, etype, amount in rows:
        bucket = counts.setdefault(int(wid), {
            'advance': 0.0, 'payment': 0.0, 'tip': 0.0, 'settlement': 0.0})
        key = (etype or '').strip().lower()
        if key in bucket:
            bucket[key] = float(amount or 0.0)
    out = {}
    for key, worker in by_name.items():
        snap = snaps.get(int(worker.id), {})
        money = counts.get(int(worker.id), {})
        out[key] = {
            'worker': worker,
            'advance': float(money.get('advance', 0.0)),
            'payment': float(money.get('payment', 0.0)),
            'tip': float(money.get('tip', 0.0)),
            'settlement': float(money.get('settlement', 0.0)),
            'balance': float(snap.get('balance', 0.0) or 0.0),
        }
    return out


def known_party_names():
    """Distinct party names for combo suggestions (add form / entries filter)."""
    names = []
    seen = set()
    for party in CashFlowParty.query.order_by(CashFlowParty.name.asc()).all():
        name = _normalize_name_ci(party.name)
        key = name.lower()
        if name and key not in seen:
            seen.add(key)
            names.append(name)
    return names


def party_combo_options(parties=None, category=''):
    """``(id, label)`` pairs for the directory name combo."""
    rows = parties if parties is not None else (
        CashFlowParty.query.order_by(CashFlowParty.name.asc()).all())
    cat = (category or '').strip().lower()
    options = []
    for party in rows:
        if cat and cat in PARTY_CATEGORY_KEYS and party_category(party.party_type) != cat:
            continue
        label = f'{party.name} ({party_type_label(party.party_type)})'
        if not party.is_active:
            label += ' — hidden'
        options.append((party.id, label))
    return options


def party_id_by_name():
    """``{name_key: party_id}`` — first active match wins."""
    mapping = {}
    parties = (CashFlowParty.query
               .order_by(CashFlowParty.is_active.desc(), CashFlowParty.id.asc())
               .all())
    for party in parties:
        key = party_name_key(party.name)
        if key and key not in mapping:
            mapping[key] = int(party.id)
    return mapping


def build_party_directory(*, search='', category='', party_id=None, status=''):
    """Assemble the ledger-style directory rows + KPI counts."""
    search = (search or '').strip()
    needle = search.lower()
    category = (category or '').strip().lower()
    if category not in PARTY_CATEGORY_KEYS:
        category = ''
    status = (status or '').strip().lower()
    try:
        party_id = int(party_id or 0) or None
    except (TypeError, ValueError):
        party_id = None

    parties = CashFlowParty.query.order_by(CashFlowParty.name.asc()).all()
    money = party_money_by_name()
    loan_counts = _loan_counts()
    rental_stats = _rental_stats()
    worker_stats = _worker_stats()

    def _count(bucket):
        return sum(1 for p in parties
                   if p.is_active and party_category(p.party_type) == bucket)

    counts = {
        'loan': _count('loan'),
        'rental': _count('rental'),
        'worker': _count('worker'),
        'client': _count('client'),
        'supplier': _count('supplier'),
        'other': _count('other'),
        'active_total': sum(1 for p in parties if p.is_active),
        'all': len(parties),
        'hidden': sum(1 for p in parties if not p.is_active),
    }

    rows = []
    for party in parties:
        cat = party_category(party.party_type)
        if category and cat != category:
            continue
        if party_id and int(party.id) != int(party_id):
            continue
        if status == 'active' and not party.is_active:
            continue
        if status == 'hidden' and party.is_active:
            continue
        if needle:
            hay = ' '.join([
                party.name or '', party.phone or '', party.note or '',
                party_type_label(party.party_type), cat,
            ]).lower()
            if needle not in hay:
                continue
        key = party_name_key(party.name)
        cash = money.get(key, {'in': 0.0, 'out': 0.0, 'count': 0, 'balance': 0.0})
        rent = rental_stats.get(key, {'count': 0, 'paid': 0.0, 'due': 0.0})
        rows.append({
            'party': party,
            'category': cat,
            'category_label': category_label(cat),
            'type_label': party_type_label(party.party_type),
            'money_in': float(cash.get('in') or 0.0),
            'money_out': float(cash.get('out') or 0.0),
            'balance': float(cash.get('balance') or 0.0),
            'entries': int(cash.get('count') or 0),
            'loans': loan_counts.get(key, 0),
            'rentals': rent.get('count') or 0,
            'rental_due': float(rent.get('due') or 0.0),
            'worker': worker_stats.get(key),
        })

    return {
        'rows': rows,
        'counts': counts,
        'parties': parties,
        'party_options': party_combo_options(parties, category=category),
        'name_options': known_party_names(),
        'worker_total': Worker.query.count(),
    }


def party_ledger(party, *, include_void=False):
    """Chronological statement for one party, sourced from ``hdc_account_txn``."""
    from hdc.services.accounts import _account_reference_links

    key = party_name_key(party.name)
    q = (AccountTransaction.query
         .options(joinedload(AccountTransaction.from_account),
                  joinedload(AccountTransaction.to_account),
                  joinedload(AccountTransaction.project),
                  joinedload(AccountTransaction.stage))
         .filter(func.lower(func.trim(func.coalesce(AccountTransaction.party_name, ''))) == key))
    if not include_void:
        q = q.filter(AccountTransaction.is_void == False)  # noqa: E712
    txns = q.all()
    txns.sort(key=lambda row: (
        row.date or _pkt_today(),
        row.created_at or datetime.min,
        int(row.id or 0),
    ))

    running = 0.0
    total_in = 0.0
    total_out = 0.0
    rows = []
    for txn in txns:
        amount = float(txn.amount or 0.0)
        direction = _txn_direction(txn.type)
        is_void = bool(txn.is_void)
        money_in = amount if direction == 'receive' and not is_void else 0.0
        money_out = amount if direction == 'pay' and not is_void else 0.0
        if not is_void:
            running += money_in - money_out
            total_in += money_in
            total_out += money_out
        ts = (txn.created_at
              or datetime.combine((txn.date or _pkt_today()), datetime.min.time()))
        from_name = (txn.from_account.name if getattr(txn, 'from_account', None)
                     else (f'Account #{txn.from_account_id}' if txn.from_account_id else ''))
        to_name = (txn.to_account.name if getattr(txn, 'to_account', None)
                   else (txn.party_name or '').strip() or 'Off-ledger')
        rows.append({
            'id': int(txn.id),
            'txn': txn,
            'timestamp': ts,
            'tx_type': str(txn.type or '').replace('_', ' ').title(),
            'type_raw': txn.type or '',
            'direction': direction,
            'flow': f'{from_name} → {to_name}'.strip(' →'),
            'note': txn.note or '',
            'reference_id': txn.reference_id or '',
            'amount': amount,
            'money_in': money_in,
            'money_out': money_out,
            'running_balance': float(running),
            'is_void': is_void,
            'references': _account_reference_links(txn),
        })

    key = party_name_key(party.name)
    worker = _worker_stats().get(key)
    rent = _rental_stats().get(key, {'count': 0, 'due': 0.0, 'paid': 0.0})
    open_loans = (Loan.query
                  .filter(func.lower(func.trim(Loan.party_name)) == key,
                          Loan.status == 'open')
                  .all())
    loan_outstanding = sum(float(loan.outstanding_minor or 0) / 100.0
                           for loan in open_loans)

    return {
        'rows': rows,
        'total_in': float(total_in),
        'total_out': float(total_out),
        'balance': float(total_in - total_out),
        'entry_count': len([row for row in rows if not row['is_void']]),
        'worker': worker,
        'rentals': rent.get('count') or 0,
        'rental_due': float(rent.get('due') or 0.0),
        'loan_outstanding': float(loan_outstanding),
        'open_loans': len(open_loans),
    }


def type_choice_groups():
    """Add-party <optgroup> structure used by the directory form.

    The optgroups mirror the KPI/filter buckets: every type with a bucket of
    its own is offered under that bucket's name, so choosing "Client / Owner"
    here is what files the party under *Clients / Owners* on the directory.
    """
    return [
        ('loan', 'Loan Parties',
         [(value, label) for value, label in PARTY_TYPES if value in LOAN_PARTY_TYPES]),
        ('rental', 'External Customers (HDC Tools)',
         [(value, label) for value, label in PARTY_TYPES if value in RENTAL_PARTY_TYPES]),
        ('worker', 'Workers',
         [(value, label) for value, label in PARTY_TYPES if value in WORKER_PARTY_TYPES]),
        ('client', 'Clients / Owners',
         [(value, label) for value, label in PARTY_TYPES if value in CLIENT_PARTY_TYPES]),
        ('supplier', 'Suppliers / Vendors',
         [(value, label) for value, label in PARTY_TYPES if value in SUPPLIER_PARTY_TYPES]),
        ('other', 'Other Parties',
         [(value, label) for value, label in PARTY_TYPES
          if value not in BUCKETED_PARTY_TYPES]),
    ]
