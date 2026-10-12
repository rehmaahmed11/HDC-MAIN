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


def _project_stats_by_name():
    """``{name_key: {count, projects, contract_value, received, receivable}}`` from Project.client."""
    try:
        from hdc.models.projects import Project
        from hdc.services.aggregation import _apply_aggregated_project_costs
    except Exception:
        return {}
    try:
        projects = Project.query.all()
    except Exception:
        return {}
    # Apply aggregated costs once for performance if available
    try:
        _apply_aggregated_project_costs(projects)
    except Exception:
        pass
    stats = {}
    for p in projects:
        raw_client = (getattr(p, 'client', '') or '').strip()
        if not raw_client:
            continue
        key = party_name_key(raw_client)
        if not key:
            continue
        bucket = stats.setdefault(key, {
            'count': 0,
            'projects': [],
            'contract_value': 0.0,
            'received': 0.0,
            'receivable': 0.0,
        })
        bucket['count'] += 1
        bucket['projects'].append(p)
        try:
            bucket['contract_value'] += float(getattr(p, 'owner_contract_value', 0.0) or 0.0)
        except Exception:
            pass
        try:
            bucket['received'] += float(getattr(p, 'total_received', 0.0) or 0.0)
        except Exception:
            pass
        try:
            bucket['receivable'] += float(getattr(p, 'remaining_receivable', 0.0) or 0.0)
        except Exception:
            pass
    return stats


def sync_project_clients_as_parties():
    """File every Project.client in the Parties directory as a ``client`` party.

    Mirrors ``sync_workers_as_parties``: idempotent, non-destructive, reviving
    hidden rows.  A project client is the person who owes us for a project, so
    they belong in the same directory as lenders, suppliers and workers.

    Returns ``(created, reactivated, total_clients)``.
    """
    try:
        from hdc.models.projects import Project
        from hdc.services.cashflow_register import ensure_party
    except Exception:
        return 0, 0, 0

    try:
        projects = Project.query.all()
    except Exception:
        return 0, 0, 0

    seen_keys = {}
    created = 0
    reactivated = 0
    for p in projects:
        nm = (getattr(p, 'client', '') or '').strip()
        if not nm:
            continue
        key = party_name_key(nm)
        if not key or key in seen_keys:
            continue
        seen_keys[key] = nm

    # Group projects by client key for counts and phone lookup
    grouped = {}
    for p in projects:
        raw = (getattr(p, 'client', '') or '').strip()
        if not raw:
            continue
        k = party_name_key(raw)
        if not k:
            continue
        grouped.setdefault(k, []).append(p)

    # Now ensure each distinct client name exists as a party
    for key, nm in seen_keys.items():
        existing = (CashFlowParty.query
                    .filter(func.lower(func.trim(CashFlowParty.name)) == key)
                    .first())
        was_hidden = bool(existing and not existing.is_active)
        # Find latest phone for this client
        latest_phone = None
        for pr in reversed(grouped.get(key, [])):
            ph = (getattr(pr, 'client_phone', '') or '').strip()
            if ph:
                latest_phone = ph[:40]
                break
        try:
            _party, is_new = ensure_party(
                nm,
                party_type='client',
                phone=latest_phone,
                note=f"Client / Owner — {len(grouped.get(key, []))} project(s)",
            )
            # Enrich phone if missing
            if not is_new and latest_phone and not (_party.phone or '').strip():
                _party.phone = latest_phone
            if is_new:
                created += 1
            elif was_hidden:
                reactivated += 1
        except Exception:
            continue

    return created, reactivated, len(seen_keys)


def projects_for_party(party):
    """Return list of Project rows where Project.client matches party.name (case-insensitive)."""
    if party is None:
        return []
    key = party_name_key(party.name)
    if not key:
        return []
    try:
        from hdc.models.projects import Project
        rows = (Project.query
                .filter(func.lower(func.trim(func.coalesce(Project.client, ''))) == key)
                .order_by(Project.created_at.desc(), Project.id.desc())
                .all())
        # Apply aggregated costs if possible for totals
        try:
            from hdc.services.aggregation import _apply_aggregated_project_costs
            _apply_aggregated_project_costs(rows)
        except Exception:
            pass
        return rows
    except Exception:
        return []


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


def known_client_party_names():
    """Distinct client party names for project forms — searchable combo."""
    names = []
    seen = set()
    q = CashFlowParty.query.filter(
        func.lower(CashFlowParty.party_type) == 'client'
    ).order_by(CashFlowParty.name.asc()).all()
    for party in q:
        name = _normalize_name_ci(party.name)
        key = name.lower()
        if name and key not in seen:
            seen.add(key)
            names.append(name)
    # Also include any project.client that hasn't been synced yet
    try:
        from hdc.models.projects import Project
        for proj in Project.query.all():
            raw = _normalize_name_ci(getattr(proj, 'client', '') or '')
            k = raw.lower()
            if raw and k not in seen:
                seen.add(k)
                names.append(raw)
    except Exception:
        pass
    return sorted(names, key=lambda s: s.lower())


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
    project_stats = _project_stats_by_name()

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
        proj = project_stats.get(key, {'count': 0, 'projects': [], 'contract_value': 0.0, 'received': 0.0, 'receivable': 0.0})
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
            'projects_count': int(proj.get('count') or 0),
            'projects': proj.get('projects') or [],
            'projects_contract': float(proj.get('contract_value') or 0.0),
            'projects_received': float(proj.get('received') or 0.0),
            'projects_receivable': float(proj.get('receivable') or 0.0),
        })

    return {
        'rows': rows,
        'counts': counts,
        'parties': parties,
        'party_options': party_combo_options(parties, category=category),
        'name_options': known_party_names(),
        'client_name_options': known_client_party_names(),
        'project_stats': project_stats,
        'worker_total': Worker.query.count(),
        'total_project_clients': len(project_stats),
    }


def party_ledger(party, *, include_void=False):
    """Chronological statement for one party, sourced from ``hdc_account_txn``.

    For a project owner, this is now the *unified* owner ledger:
    - All ledger postings where party_name matches (owner receipts, rent receipts, loan movements, etc.)
    - Projects owned (where Project.client == party name)
    - External tool rentals where customer_name == party name
    - Internal tool rentals for projects owned by this party (company using tools on their sites)
    - Loans where party_name == party name (open + closed)
    - Supplier / purchase history if party is supplier (optional)
    So you can answer "does this owner have any rent payment?" in one page.
    """
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
    proj_stats = _project_stats_by_name().get(key, {'count': 0, 'projects': [], 'contract_value': 0.0, 'received': 0.0, 'receivable': 0.0})
    try:
        party_projects = projects_for_party(party)
    except Exception:
        party_projects = proj_stats.get('projects') or []

    # ── Detailed rentals for this party ──────────────────────────────────
    rentals_external = []
    rentals_internal = []
    rentals_all = []
    try:
        # External: customer_name matches
        rentals_external = (ToolRental.query
                            .filter(func.lower(func.trim(func.coalesce(ToolRental.customer_name, ''))) == key)
                            .order_by(ToolRental.rental_date.desc(), ToolRental.id.desc())
                            .all())
        # Internal: rentals for projects owned by this party
        project_ids = [int(p.id) for p in (party_projects or []) if getattr(p, 'id', None)]
        if project_ids:
            rentals_internal = (ToolRental.query
                                .filter(ToolRental.project_id.in_(project_ids))
                                .order_by(ToolRental.rental_date.desc(), ToolRental.id.desc())
                                .all())
        # Combine unique
        seen_rental_ids = set()
        for r in (rentals_external + rentals_internal):
            if int(r.id) not in seen_rental_ids:
                rentals_all.append(r)
                seen_rental_ids.add(int(r.id))
        rentals_all.sort(key=lambda r: (r.rental_date or _pkt_today(), r.id), reverse=True)
    except Exception:
        rentals_external = []
        rentals_internal = []
        rentals_all = []

    # ── Detailed loans for this party ────────────────────────────────────
    loans_all = []
    loans_open = []
    loans_closed = []
    try:
        loans_all = (Loan.query
                     .filter(func.lower(func.trim(Loan.party_name)) == key)
                     .order_by(Loan.created_at.desc(), Loan.id.desc())
                     .all())
        loans_open = [ln for ln in loans_all if (ln.status or '').lower() == 'open']
        loans_closed = [ln for ln in loans_all if (ln.status or '').lower() != 'open']
    except Exception:
        loans_all = []
        loans_open = []
        loans_closed = []

    open_loans = loans_open
    loan_outstanding = sum(float(loan.outstanding_minor or 0) / 100.0
                           for loan in open_loans)

    # ── Supplier stats if party is supplier (optional) ───────────────────
    supplier_stats = None
    try:
        from hdc.models.materials import Supplier
        sup = (Supplier.query
               .filter(func.lower(func.trim(Supplier.name)) == key)
               .first())
        if sup:
            supplier_stats = sup
    except Exception:
        supplier_stats = None

    return {
        'rows': rows,
        'total_in': float(total_in),
        'total_out': float(total_out),
        'balance': float(total_in - total_out),
        'entry_count': len([row for row in rows if not row['is_void']]),
        'worker': worker,
        'rentals': rent.get('count') or len(rentals_external) or 0,
        'rental_due': float(rent.get('due') or 0.0),
        'rentals_external': rentals_external,
        'rentals_internal': rentals_internal,
        'rentals_all': rentals_all,
        'rentals_count': len(rentals_all),
        'loan_outstanding': float(loan_outstanding),
        'open_loans': len(open_loans),
        'loans_all': loans_all,
        'loans_open': loans_open,
        'loans_closed': loans_closed,
        'loans_count': len(loans_all),
        'projects': party_projects,
        'projects_count': int(proj_stats.get('count') or len(party_projects) or 0),
        'projects_contract': float(proj_stats.get('contract_value') or 0.0),
        'projects_received': float(proj_stats.get('received') or 0.0),
        'projects_receivable': float(proj_stats.get('receivable') or 0.0),
        'supplier': supplier_stats,
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
