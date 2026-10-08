"""Tools > Audit — count what is physically at each place, then prove the book.

:mod:`hdc.services.tool_tracking` answers "where does the system *think* every
tool is".  An audit answers the only question that can prove it: send a sheet to
the site, count the pieces, type the numbers in, look at what does not match and
put it right.

This service

* lists the places worth counting (the store, every own site, every outside
  customer holding something — plus live sites that hold *nothing*, because
  "you should have no tools here" is exactly what a physical check proves);
* builds one count sheet per place, including tools that are **not** expected
  there, since "we found two grinders nobody booked" is the other half of a
  verification;
* stores each counted number beside a snapshot of the expected number, so a
  sheet reopened a month later still shows the discrepancy as it was found
  instead of silently re-deriving it from today's book;
* posts what the count found back into the stock registers on demand — a
  shortage is written off through the scrap register *and* taken off the rental
  line that was holding it (otherwise the tools would stay "out" forever), an
  overage is added to owned stock.

Everything reads the same :func:`hdc.services.tool_tracking.tool_ledger` the
dashboard / tracking / inventory pages use, so the audit can never disagree with
the rest of the Tools section — and every write keeps that section's one
identity true: ``owned == in store + out at sites + out with customers``.
"""

from datetime import datetime

from sqlalchemy import func

from hdc.extensions import db
from hdc.models.projects import Project, Stage
from hdc.models.tool_rental import (
    MOVEMENT_AUDIT_ADJUST, TOOL_AUDIT_LOSS_REASONS, TOOL_SCRAP_REASONS, Tool,
    ToolAudit, ToolAuditLine, ToolMovementLog, ToolRental, ToolRentalItem, ToolScrap,
)
from hdc.services.record_permissions import record_code_query
from hdc.services.tool_rental import (
    STORE_LABEL, create_movement_log, record_tool_scrap, recalc_rental_totals,
    tool_available_for_integrity,
)
from hdc.services.tool_tracking import (
    EPS, LOC_CUSTOMER, LOC_OWN_PROJECT, LOC_STORE, WAREHOUSE_LABEL, tool_ledger,
)
from hdc.utils.dates import _pkt_now_naive, _pkt_today
from hdc.utils.format import _flt

#: A site that can no longer hold tools is not worth an empty count card.  The
#: same list :mod:`hdc.services.money_hub` uses, so the app never disagrees
#: about which projects are still live.
_CLOSED_PROJECT_STATUSES = ('completed', 'closed', 'cancelled', 'canceled', 'inactive')

#: Movement-log wording for a fix that came from a count, so Tracking can say
#: "an audit did this" instead of looking like a normal move.
AUDIT_LOSS_LABEL = 'Written off — audit shortage'
AUDIT_GAIN_LABEL = 'Found at audit — stock corrected'

#: Meta fields the count form is allowed to write.
_AUDIT_META_FIELDS = ('counter_name', 'reference', 'notes', 'audit_date')

#: Reason picker for a write-off.  Deliberately a subset of the *scrap* reasons
#: so an audit loss lands in the same register (and the same report) as a scrap
#: typed in by hand — one place explains every piece that is gone.
AUDIT_LOSS_REASONS = tuple((key, label) for key, label in TOOL_SCRAP_REASONS
                           if key in TOOL_AUDIT_LOSS_REASONS)
AUDIT_MODE_LABELS = {
    'losses': 'Write off the losses only',
    'both': 'Write off losses and add the overages',
}


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _q2(value):
    """Pieces / metres — two decimals and no float dust (mirrors the tracker)."""
    return round(float(value or 0.0), 2)


def _parse_day(raw):
    raw = str(raw or '').strip()
    if not raw:
        return _pkt_today()
    try:
        return datetime.strptime(raw[:10], '%Y-%m-%d').date()
    except ValueError:
        return _pkt_today()


def _next_audit_code():
    last_id = record_code_query(db.session.query(func.max(ToolAudit.id)),
                                ToolAudit.__tablename__).scalar()
    return f"AUDIT-{int(last_id or 0) + 1:05d}"


def location_key(loc_type, project_id=None, stage_id=None, customer_name=None):
    """Stable text key for one auditable place (used in URLs and on forms).

    A site is keyed by the **project**, not by stage: a storekeeper counts
    "what is at Gulshan Villa Block A", which is often spread over several
    stages.  A stage-level key is still honoured for a stage-specific count.
    """
    if loc_type == LOC_OWN_PROJECT:
        key = f'site:{int(project_id or 0)}'
        if stage_id:
            key += f'#{int(stage_id)}'
        return key
    if loc_type == LOC_CUSTOMER:
        return f'cust:{(customer_name or "").strip()}'
    return LOC_STORE


def parse_location_key(key):
    """Inverse of :func:`location_key`.  Never raises — junk keys mean the store."""
    raw = (key or '').strip()
    if raw.startswith('site:'):
        pid, _, sid = raw[5:].partition('#')
        return {'loc_type': LOC_OWN_PROJECT,
                'project_id': int(pid) if pid.isdigit() else None,
                'stage_id': int(sid) if sid.isdigit() else None,
                'customer_name': None}
    if raw.startswith('cust:'):
        name = raw[5:].strip()
        return {'loc_type': LOC_CUSTOMER, 'project_id': None, 'stage_id': None,
                'customer_name': name or None}
    return {'loc_type': LOC_STORE, 'project_id': None, 'stage_id': None,
            'customer_name': None}


def spec_label(spec):
    """Human label for a location spec ('Gulshan Villa Block A > Grey Structure')."""
    loc_type = spec.get('loc_type') or LOC_STORE
    if loc_type == LOC_OWN_PROJECT:
        project = (db.session.get(Project, int(spec['project_id']))
                   if spec.get('project_id') else None)
        label = (project.name if project else
                 f"Site #{spec.get('project_id') or '?'}")
        stage = (db.session.get(Stage, int(spec['stage_id']))
                 if spec.get('stage_id') else None)
        if stage:
            label = f'{label} > {stage.name}'
        return label
    if loc_type == LOC_CUSTOMER:
        return (spec.get('customer_name') or '').strip() or 'External Customer'
    return WAREHOUSE_LABEL


def location_type_label(loc_type):
    if loc_type == LOC_OWN_PROJECT:
        return 'HDC site'
    if loc_type == LOC_CUSTOMER:
        return 'Outside customer'
    return 'Warehouse / Store'


def _holding_matches_location(holding, spec):
    """Does one ledger holding physically sit at the audited place?"""
    if (holding.get('loc_type') or LOC_STORE) != spec['loc_type']:
        return False
    if spec['loc_type'] == LOC_STORE:
        return True
    if spec['loc_type'] == LOC_OWN_PROJECT:
        if int(holding.get('project_id') or 0) != int(spec.get('project_id') or 0):
            return False
        stage_id = int(spec.get('stage_id') or 0)
        return not stage_id or int(holding.get('stage_id') or 0) == stage_id
    return ((holding.get('customer_name') or '').strip().casefold()
            == (spec.get('customer_name') or '').strip().casefold())


def book_position(ledger, spec):
    """What the book says is at one place, tool by tool.

    ``{tool_id: {'qty': .., 'holdings': [holding, ..]}}``.  For the store the
    expected number is whatever is owned and not out — the tracker's own figure,
    so the two pages can never disagree about what should be on the shelf.
    """
    is_store = spec['loc_type'] == LOC_STORE
    position = {}
    for row in ledger['tools']:
        if is_store:
            qty = _q2(row['in_store_qty'])
            if qty > EPS:
                position[row['tool_id']] = {'qty': qty, 'holdings': []}
            continue
        holdings = [h for h in row['holdings'] if _holding_matches_location(h, spec)]
        qty = _q2(sum(_q2(h['qty']) for h in holdings))
        if holdings and qty > EPS:
            position[row['tool_id']] = {'qty': qty, 'holdings': holdings}
    return position


def _countable_specs(ledger):
    """Every place a count sheet can be opened for (store last of the list)."""
    specs = {}
    for location in ledger['locations']:
        loc_type = location['loc_type']
        if loc_type == LOC_STORE or _q2(location['qty']) <= EPS:
            continue
        project_id = location['project_id'] if loc_type == LOC_OWN_PROJECT else None
        stage_id = None if loc_type == LOC_OWN_PROJECT else location['stage_id']
        key = location_key(loc_type, project_id, stage_id, location['customer_name'])
        slot = specs.get(key)
        if slot is None:
            label = location['label']
            if loc_type == LOC_OWN_PROJECT and project_id:
                project = db.session.get(Project, int(project_id))
                if project and project.name:
                    label = project.name
            slot = specs[key] = {
                'loc_type': loc_type, 'project_id': project_id, 'stage_id': stage_id,
                'customer_name': location['customer_name'], 'key': key, 'label': label,
                'qty': 0.0, 'tool_types': 0, 'rental_count': 0, 'overdue_qty': 0.0,
            }
        slot['qty'] = _q2(slot['qty'] + _q2(location['qty']))
        slot['tool_types'] += int(location['tool_types'] or 0)
        slot['rental_count'] += int(location['rentals'] or 0)
        slot['overdue_qty'] = _q2(slot['overdue_qty'] + _q2(location['overdue_qty']))

    live_projects = (Project.query
                     .filter(~func.lower(func.coalesce(Project.status, '')).in_(_CLOSED_PROJECT_STATUSES))
                     .order_by(Project.name.asc()).all())
    for project in live_projects:
        key = location_key(LOC_OWN_PROJECT, project.id)
        if key in specs:
            continue
        specs[key] = {'loc_type': LOC_OWN_PROJECT, 'project_id': int(project.id),
                      'stage_id': None, 'customer_name': None, 'key': key,
                      'label': project.name, 'qty': 0.0, 'tool_types': 0,
                      'rental_count': 0, 'overdue_qty': 0.0}

    store_key = location_key(LOC_STORE)
    specs[store_key] = {
        'loc_type': LOC_STORE, 'project_id': None, 'stage_id': None,
        'customer_name': None, 'key': store_key, 'label': WAREHOUSE_LABEL,
        'qty': _q2(ledger['totals']['in_store_qty']),
        'tool_types': len([t for t in ledger['tools'] if t['in_store_qty'] > EPS]),
        'rental_count': 0, 'overdue_qty': 0.0,
    }
    order = {LOC_OWN_PROJECT: 0, LOC_CUSTOMER: 1, LOC_STORE: 2}
    return sorted(specs.values(), key=lambda s: (order.get(s['loc_type'], 9),
                                                 -s['qty'], (s['label'] or '').casefold()))


# --------------------------------------------------------------------------- #
# audits: index them once, reuse everywhere
# --------------------------------------------------------------------------- #
def _line_view(line):
    return {
        'id': int(line.id),
        'tool_id': int(line.tool_id),
        'book_qty': _q2(line.book_qty),
        'counted_qty': None if line.counted_qty is None else _q2(line.counted_qty),
        'damaged_qty': _q2(line.damaged_qty),
        'variance': _q2(line.variance),
        'status': line.status or 'pending',
        'notes': line.notes or '',
        'adjusted_qty': _q2(line.adjusted_qty),
        'adjust_reason': line.adjust_reason or '',
        'adjusted_at': line.adjusted_at,
        'has_scrap': bool(line.scrap_id),
    }


def lines_of(audit):
    """The sheet's lines, straight from the session — never a stale cache.

    Lines are created inside the same flush that saves them, so ``audit.lines``
    (loaded before the insert) can still be the old collection.  Every count and
    adjustment path re-reads them here instead of trusting the relationship.
    """
    if audit is None:
        return []
    return (ToolAuditLine.query.filter_by(audit_id=int(audit.id))
            .order_by(ToolAuditLine.tool_name.asc(), ToolAuditLine.id.asc()).all())


def _audits_lines_view(audits):
    """``{audit_id: {tool_id: line view}}`` for any set of audits — one query."""
    ids = [int(audit.id) for audit in (audits or ())]
    out = {audit_id: {} for audit_id in ids}
    if not ids:
        return out
    lines = ToolAuditLine.query.filter(ToolAuditLine.audit_id.in_(ids)).all()
    for line in lines:
        out.setdefault(int(line.audit_id), {})[int(line.tool_id)] = _line_view(line)
    return out


def _audits_index():
    """``{location_key: {'open': audit, 'last': audit, 'count': n, 'lines': {}, 'history': []}}``.

    ``open`` is the sheet somebody can still type into (draft / counted);
    ``last`` is the newest non-void sheet for the place, open or closed — that
    is what the comparison column shows once nobody is counting any more.
    """
    audits = (ToolAudit.query
              .filter(ToolAudit.is_void == False)  # noqa: E712
              .order_by(ToolAudit.audit_date.desc(), ToolAudit.id.desc()).all())
    index = {}
    if not audits:
        return index
    lines_by_audit = _audits_lines_view(audits)
    for audit in audits:
        key = location_key(audit.loc_type, audit.project_id, audit.stage_id,
                           audit.customer_name)
        slot = index.setdefault(key, {'open': None, 'last': None, 'count': 0,
                                       'history': [], 'lines': {}})
        slot['count'] += 1
        slot['lines'][int(audit.id)] = lines_by_audit.get(int(audit.id), {})
        if slot['last'] is None:
            slot['last'] = audit
        if audit.is_open and slot['open'] is None:
            slot['open'] = audit
        slot['history'].append(audit)
    return index


def _audit_brief(audit, lines=None):
    """Compact audit summary for cards, history rows and the sheet header."""
    if audit is None:
        return None
    lines = lines or {}
    counted = [line for line in lines.values() if line['counted_qty'] is not None]
    open_variance = [line for line in counted
                     if abs(line['variance']) > EPS and line['status'] != 'adjusted']
    return {
        'id': int(audit.id),
        'code': audit.audit_code,
        'date': audit.audit_date,
        'status': audit.status or 'draft',
        'status_label': audit.status_label,
        'is_open': bool(audit.is_open),
        'is_void': bool(audit.is_void),
        'counter_name': audit.counter_name or '',
        'reference': audit.reference or '',
        'notes': audit.notes or '',
        'lines': len(lines),
        'counted_lines': len(counted),
        'expected_lines': sum(1 for line in lines.values() if line['book_qty'] > EPS),
        'shortage_qty': _q2(sum(-line['variance'] for line in counted if line['variance'] < 0)),
        'overage_qty': _q2(sum(line['variance'] for line in counted if line['variance'] > 0)),
        'damaged_qty': _q2(sum(line['damaged_qty'] for line in counted)),
        'discrepancy_lines': len(open_variance),
        'write_off_value': _q2(audit.write_off_value),
        'adjusted_lines': int(audit.adjusted_lines or 0),
        'adjusted_at': audit.adjusted_at,
        'void_reason': audit.void_reason or '',
    }


# --------------------------------------------------------------------------- #
# the two read views: places (cards) and tools (the matrix)
# --------------------------------------------------------------------------- #
def audit_locations(ledger=None, term=None, index=None):
    """One card per place: what the book expects, what was counted, the gap."""
    ledger = ledger or tool_ledger()
    index = _audits_index() if index is None else index
    needle = (term or '').strip().casefold()

    cards = []
    for spec in _countable_specs(ledger):
        slot = index.get(spec['key']) or {'open': None, 'last': None, 'count': 0,
                                          'lines': {}, 'history': []}
        audit = slot['open'] or slot['last']
        lines = slot['lines'].get(int(audit.id), {}) if audit else {}
        book = book_position(ledger, spec)

        counted_lines = [line for line in lines.values() if line['counted_qty'] is not None]
        counted_qty = _q2(sum(line['counted_qty'] for line in counted_lines))
        # Compared against what the book says *now*, not the sheet's own
        # snapshot: once a shortage has been written off the place really is
        # reconciled, so the card must stop shouting about a 2-piece gap. The
        # sheet itself keeps showing the as-found snapshot on every line.
        counted_book = _q2(sum((book.get(int(line['tool_id'])) or {}).get('qty', 0.0)
                               for line in counted_lines))
        card = {
            **{name: spec[name] for name in ('loc_type', 'project_id', 'stage_id',
                                              'customer_name', 'key', 'label')},
            'type_label': location_type_label(spec['loc_type']),
            'expected_qty': _q2(sum(entry['qty'] for entry in book.values())),
            'expected_types': len(book),
            'ledger_qty': _q2(spec['qty']),
            'rental_count': spec['rental_count'],
            'overdue_qty': spec['overdue_qty'],
            'counted_qty': counted_qty,
            'variance': _q2(counted_qty - counted_book),
            'discrepancy_count': len([line for line in counted_lines
                                       if abs(line['variance']) > EPS
                                       and line['status'] != 'adjusted']),
            'uncounted_types': sum(1 for tool_id, entry in book.items()
                                   if entry['qty'] > EPS
                                   and (lines.get(tool_id) or {}).get('counted_qty') is None),
            'has_count': bool(lines),
            'audit': _audit_brief(audit, lines),
            'audit_id': int(audit.id) if audit else None,
            'audit_is_open': bool(audit and audit.is_open),
            'audit_count': slot['count'],
            'book': {tool_id: _q2(entry['qty']) for tool_id, entry in book.items()},
        }
        if needle and needle not in (card['label'] or '').casefold():
            continue
        cards.append(card)
    return cards


def audit_matrix(ledger=None, locations=None, term=None, category_id=None,
                 only_discrepancies=False, index=None):
    """Every tool: total owned vs where it is vs what the places actually counted.

    ``Where the book says it is`` is the tracker's own position, so this table
    can never drift from the Tools dashboard.  ``Counted`` carries a number only
    for the places a sheet actually covers — an uncounted site is reported as
    *not verified*, never as a match.
    """
    ledger = ledger or tool_ledger()
    index = _audits_index() if index is None else index
    locations = audit_locations(ledger, index=index) if locations is None else locations
    needle = (term or '').strip().casefold()
    category_id = int(category_id) if category_id else None

    rows = []
    totals = {'tool_types': 0, 'owned_qty': 0.0, 'expected_qty': 0.0,
              'counted_qty': 0.0, 'variance_qty': 0.0, 'unverified_qty': 0.0,
              'shortage_types': 0, 'overage_types': 0, 'unaudited_types': 0,
              'damaged_qty': 0.0, 'write_off_value': 0.0}
    for row in ledger['tools']:
        tool = row['tool']
        haystack = ' '.join([row['name'] or '', row['code'] or '', row['category'] or '',
                             tool.description or '']).casefold()
        if needle and needle not in haystack:
            continue
        if category_id and int(tool.category_id or 0) != category_id:
            continue

        places = []
        counted_total = 0.0
        counted_book_total = 0.0
        damaged_total = 0.0
        shortage_value = 0.0
        statuses = set()
        for card in locations:
            tool_id = row['tool_id']
            expected = _q2((card.get('book') or {}).get(tool_id, 0.0))
            audit = card['audit']
            line = None
            if audit:
                line = (index.get(card['key'], {}).get('lines', {})
                        .get(int(audit['id']), {}).get(tool_id))
            if expected <= EPS and not line:
                continue
            counted = line['counted_qty'] if line else None
            variance = _q2(line['variance']) if line else 0.0
            if counted is not None:
                counted_total += counted
                counted_book_total += expected          # today's book, not the snapshot
                damaged_total += _q2(line['damaged_qty'])
                if variance < -EPS:
                    shortage_value += -variance * _flt(tool.purchase_cost)
                statuses.add(line['status'])
            places.append({
                'label': card['label'],
                'loc_type': card['loc_type'],
                'key': card['key'],
                'audit_id': audit['id'] if audit else None,
                'audit_code': audit['code'] if audit else '',
                'audit_is_open': bool(card['audit_is_open']),
                'expected_qty': expected,
                'counted_qty': counted,
                'variance': variance if counted is not None else None,
                'damaged_qty': _q2(line['damaged_qty']) if line else 0.0,
                'notes': (line or {}).get('notes') or '',
                'status': (line or {}).get('status') or 'pending',
                'adjusted_qty': _q2((line or {}).get('adjusted_qty')),
            })

        expected_total = _q2(sum(p['expected_qty'] for p in places))
        variance_total = _q2(counted_total - counted_book_total)
        unverified = _q2(row['owned_qty'] - counted_book_total)
        if any(p['counted_qty'] is not None for p in places):
            if 'short' in statuses:
                status = 'short'
            elif 'extra' in statuses:
                status = 'extra'
            elif 'adjusted' in statuses and abs(variance_total) <= EPS:
                status = 'adjusted'
            else:
                status = 'match'
        else:
            status = 'not_counted'

        if only_discrepancies and status in ('not_counted', 'match', 'adjusted'):
            continue

        totals['tool_types'] += 1
        totals['owned_qty'] += _q2(row['owned_qty'])
        totals['expected_qty'] += expected_total
        totals['counted_qty'] += _q2(counted_total)
        totals['variance_qty'] += variance_total
        totals['unverified_qty'] += max(0.0, unverified)
        totals['damaged_qty'] += _q2(damaged_total)
        totals['write_off_value'] += _q2(shortage_value)
        if status == 'short':
            totals['shortage_types'] += 1
        elif status == 'extra':
            totals['overage_types'] += 1
        elif status == 'not_counted':
            totals['unaudited_types'] += 1

        rows.append({
            'tool_id': row['tool_id'],
            'tool': tool,
            'name': row['name'],
            'code': row['code'],
            'unit': row['unit'],
            'category': row['category'],
            'owned_qty': _q2(row['owned_qty']),
            'in_store_qty': _q2(row['in_store_qty']),
            'own_project_qty': _q2(row['own_project_qty']),
            'customer_qty': _q2(row['customer_qty']),
            'current_label': row['current_label'],
            'condition': row['condition'],
            'unaccounted': row['unaccounted'],
            'variance_book': _q2(row['variance']),
            'unit_cost': _q2(_flt(tool.purchase_cost)),
            'places': sorted(places, key=lambda p: (-p['expected_qty'],
                                                     (p['label'] or '').casefold())),
            'expected_qty': expected_total,
            'counted_qty': _q2(counted_total),
            'variance_qty': variance_total,
            'damaged_qty': _q2(damaged_total),
            'unverified_qty': max(0.0, unverified),
            'status': status,
            'value_variance': _q2(abs(variance_total) * _flt(tool.purchase_cost)),
            'shortage_value': _q2(shortage_value),
        })

    tone_order = {'short': 0, 'extra': 1, 'adjusted': 2, 'not_counted': 3, 'match': 4}
    rows.sort(key=lambda r: (tone_order.get(r['status'], 9), -abs(r['variance_qty']),
                             r['name'].casefold()))
    for key in ('owned_qty', 'expected_qty', 'counted_qty', 'variance_qty',
                'unverified_qty', 'damaged_qty', 'write_off_value'):
        totals[key] = _q2(totals[key])
    totals['locations_total'] = len(locations)
    totals['locations_counted'] = len([c for c in locations if c['has_count']])
    return {'rows': rows, 'totals': totals}


def audit_summary(ledger=None, locations=None, index=None):
    """The KPI strip on the Audit tab."""
    ledger = ledger or tool_ledger()
    index = _audits_index() if index is None else index
    locations = audit_locations(ledger, index=index) if locations is None else locations
    audited = [card for card in locations if card['audit']]
    return {
        'owned_qty': _q2(ledger['totals']['owned_qty']),
        'placed_qty': _q2(sum(card['expected_qty'] for card in locations)),
        'counted_qty': _q2(sum(card['counted_qty'] for card in audited)),
        'variance_qty': _q2(sum(card['variance'] for card in audited)),
        'shortage_qty': _q2(sum(card['audit']['shortage_qty'] for card in audited)),
        'overage_qty': _q2(sum(card['audit']['overage_qty'] for card in audited)),
        'write_off_value': _q2(sum(card['audit']['write_off_value'] for card in audited)),
        'open_variances': _q2(sum(card['discrepancy_count'] for card in locations)),
        'locations_total': len(locations),
        'locations_counted': len([card for card in locations if card['has_count']]),
        'locations_open': len([card for card in locations if card['audit_is_open']]),
        'audits_total': sum(slot['count'] for slot in index.values()),
        'unbalanced_qty': _q2(abs(ledger['totals']['owned_qty']
                                 - ledger['totals']['in_store_qty']
                                 - ledger['totals']['out_qty'])),
    }


def audit_history(limit=40, location_key_filter=None):
    """Recent count sheets, newest first (the audit trail of the section)."""
    query = ToolAudit.query
    spec = parse_location_key(location_key_filter) if location_key_filter else None
    if spec:
        query = query.filter(ToolAudit.loc_type == spec['loc_type'])
        if spec['loc_type'] == LOC_OWN_PROJECT:
            query = query.filter(ToolAudit.project_id == spec['project_id'])
        elif spec['loc_type'] == LOC_CUSTOMER:
            query = query.filter(func.lower(ToolAudit.customer_name)
                                 == (spec['customer_name'] or '').lower())
        else:
            query = query.filter(ToolAudit.project_id.is_(None))
    audits = (query.order_by(ToolAudit.audit_date.desc(), ToolAudit.id.desc())
              .limit(int(limit or 40)).all())
    lines_by_audit = _audits_lines_view(audits)
    rows = []
    for audit in audits:
        rows.append({
            'audit': audit,
            'brief': _audit_brief(audit, lines_by_audit.get(int(audit.id), {})),
            'key': location_key(audit.loc_type, audit.project_id, audit.stage_id,
                                audit.customer_name),
            'label': audit.location_label or spec_label({
                'loc_type': audit.loc_type, 'project_id': audit.project_id,
                'stage_id': audit.stage_id, 'customer_name': audit.customer_name}),
            'type_label': location_type_label(audit.loc_type),
            'is_void': bool(audit.is_void),
        })
    return rows


# --------------------------------------------------------------------------- #
# the count sheet itself
# --------------------------------------------------------------------------- #
def _sheet_row(ledger_row, entry, line):
    """One tool on the sheet: book, saved count, variance and the paper trail."""
    entry = entry or {'qty': 0.0, 'holdings': []}
    line = line or {}
    tool = ledger_row['tool'] if ledger_row else None
    counted = line.get('counted_qty')
    expected = _q2(entry['qty'])

    rentals = []
    seen = set()
    for holding in entry.get('holdings') or ():
        rental = holding.get('rental')
        if rental is None or int(rental.id) in seen:
            continue
        seen.add(int(rental.id))
        rentals.append({
            'id': int(rental.id),
            'code': rental.rental_code,
            'label': holding.get('label') or WAREHOUSE_LABEL,
            'qty': _q2(holding.get('qty')),
            'days_out': int(holding.get('days_out') or 0),
            'overdue': bool(holding.get('overdue')),
            'pending_amount': _q2(holding.get('pending_amount')),
            'chain': ' › '.join(step['label'] for step in (holding.get('chain') or [])),
        })

    return {
        'tool_id': int((ledger_row or {}).get('tool_id') or (tool.id if tool else 0)),
        'tool': tool,
        'name': (ledger_row or {}).get('name') or (tool.name if tool else '-'),
        'code': (ledger_row or {}).get('code') or (tool.tool_code if tool else ''),
        'unit': (ledger_row or {}).get('unit') or (tool.unit if tool else 'pcs') or 'pcs',
        'category': (ledger_row or {}).get('category') or '',
        'condition': (ledger_row or {}).get('condition') or 'good',
        'owned_qty': _q2((ledger_row or {}).get('owned_qty')),
        'unit_cost': _q2(_flt(tool.purchase_cost) if tool else 0.0),
        'expected_qty': expected,
        'counted_qty': None if counted is None else _q2(counted),
        'damaged_qty': _q2(line.get('damaged_qty')),
        'variance': _q2(line.get('variance')),
        'status': line.get('status') or 'pending',
        'notes': line.get('notes') or '',
        'adjusted_qty': _q2(line.get('adjusted_qty')),
        'adjust_reason': line.get('adjust_reason') or '',
        'adjusted_at': line.get('adjusted_at'),
        'has_scrap': bool(line.get('has_scrap')),
        'rentals': rentals,
        'rental_codes': ' · '.join(r['code'] for r in rentals),
        'line_id': line.get('id'),
    }


def audit_sheet(audit=None, ledger=None, spec=None, include_unexpected=True):
    """The entry sheet: one row per tool — expected, counted, variance.

    ``audit=None`` previews a place nobody has counted yet (the *New count*
    screen): the same rows, only the save target differs, so starting a count
    and filling it in is one click for the person at the site.
    """
    ledger = ledger or tool_ledger()
    if audit is not None:
        spec = {'loc_type': audit.loc_type, 'project_id': audit.project_id,
                'stage_id': audit.stage_id, 'customer_name': audit.customer_name}
        spec['key'] = location_key(audit.loc_type, audit.project_id, audit.stage_id,
                                   audit.customer_name)
        spec['label'] = audit.location_label or spec_label(spec)
    else:
        spec = spec or {'loc_type': LOC_STORE, 'project_id': None, 'stage_id': None,
                        'customer_name': None}
        spec.setdefault('key', location_key(spec['loc_type'], spec['project_id'],
                                           spec['stage_id'], spec['customer_name']))
        spec.setdefault('label', spec_label(spec))

    saved = _audits_lines_view([audit]).get(int(audit.id), {}) if audit else {}
    book = book_position(ledger, spec)
    rows_by_id = {int(row['tool_id']): row for row in ledger['tools']}

    expected_rows = []
    for tool_id, entry in sorted(book.items(), key=lambda item: (
            -item[1]['qty'], (rows_by_id.get(item[0], {}).get('name') or '').casefold())):
        expected_rows.append(_sheet_row(rows_by_id.get(tool_id), entry, saved.get(tool_id)))

    other_rows = []
    saved_only = [tool_id for tool_id in saved if tool_id not in book]
    if saved_only or include_unexpected:
        for tool in (Tool.query.filter(Tool.is_void == False)  # noqa: E712
                     .order_by(Tool.name.asc()).all()):
            tool_id = int(tool.id)
            if tool_id in book:
                continue
            if not include_unexpected and tool_id not in saved_only:
                continue
            row = rows_by_id.get(tool_id) or {
                'tool': tool, 'tool_id': tool_id, 'name': tool.name,
                'code': tool.tool_code, 'unit': tool.unit or 'pcs',
                'category': tool.category.name if tool.category else 'No Category',
                'owned_qty': _q2(tool.total_quantity), 'in_store_qty': 0.0,
                'own_project_qty': 0.0, 'customer_qty': 0.0, 'holdings': [],
                'condition': (tool.condition or 'good').strip().lower(),
                'variance': 0.0, 'unaccounted': False,
            }
            other_rows.append(_sheet_row(row, {'qty': 0.0, 'holdings': []},
                                        saved.get(tool_id)))
    rows = expected_rows + other_rows

    counted_rows = [row for row in rows if row['counted_qty'] is not None]
    summary = {
        'expected_qty': _q2(sum(row['expected_qty'] for row in rows)),
        'expected_types': len([row for row in rows if row['expected_qty'] > EPS]),
        'counted_qty': _q2(sum(row['counted_qty'] for row in counted_rows)),
        'counted_types': len(counted_rows),
        'uncounted_types': max(0, len([row for row in rows if row['expected_qty'] > EPS])
                               - len([row for row in counted_rows
                                      if row['expected_qty'] > EPS])),
        'shortage_qty': _q2(sum(-row['variance'] for row in counted_rows
                                if row['variance'] < 0)),
        'overage_qty': _q2(sum(row['variance'] for row in counted_rows
                               if row['variance'] > 0)),
        'damaged_qty': _q2(sum(row['damaged_qty'] for row in counted_rows)),
        'write_off_value': _q2(sum(max(0.0, -row['variance']) * row['unit_cost']
                                   for row in counted_rows)),
        'found_value': _q2(sum(max(0.0, row['variance']) * row['unit_cost']
                               for row in counted_rows)),
        'match_types': len([row for row in counted_rows
                            if abs(row['variance']) <= EPS]),
        'discrepancies': len([row for row in counted_rows
                              if abs(row['variance']) > EPS and row['status'] != 'adjusted']),
    }
    summary['discrepancy_rows'] = [row for row in counted_rows
                                  if abs(row['variance']) > EPS and row['status'] != 'adjusted']
    return {
        'spec': spec,
        'rows': rows,
        'expected_rows': expected_rows,
        'other_rows': other_rows,
        'summary': summary,
        'audit': audit,
        'brief': _audit_brief(audit, saved) if audit else None,
        'can_edit': True if audit is None else bool(audit.is_open),
        'ledger_today': ledger.get('today') or _pkt_today(),
    }


def audit_movement(audit, limit=60):
    """Movement-log entries this audit produced — the trail of every fix."""
    if audit is None:
        return []
    tool_ids = [int(line.tool_id) for line in lines_of(audit)]
    if not tool_ids:
        return []
    return (ToolMovementLog.query
            .filter(ToolMovementLog.tool_id.in_(tool_ids),
                    ToolMovementLog.movement_type.in_((MOVEMENT_AUDIT_ADJUST, 'scrap_out')),
                    ToolMovementLog.notes.like(f'%{audit.audit_code}%'))
            .order_by(ToolMovementLog.timestamp.desc(), ToolMovementLog.id.desc())
            .limit(int(limit or 60)).all())


# --------------------------------------------------------------------------- #
# writes
# --------------------------------------------------------------------------- #
def _state_for(expected, counted, was_adjusted):
    if counted is None:
        return 'pending'
    variance = _q2(counted) - _q2(expected)
    if abs(variance) <= EPS:
        return 'adjusted' if was_adjusted else 'match'
    return 'short' if variance < 0 else 'extra'


def recompute_audit_totals(audit, refresh_status=True):
    """Keep the header roll-up in step with its lines (lists never re-add)."""
    lines = list(lines_of(audit))
    counted = [line for line in lines if line.counted_qty is not None]
    short = [line for line in counted if _q2(line.variance) < -EPS]
    extra = [line for line in counted if _q2(line.variance) > EPS]
    audit.total_lines = len(lines)
    audit.counted_lines = len(counted)
    audit.discrepancy_lines = len([line for line in counted
                                   if abs(_q2(line.variance)) > EPS
                                   and (line.status or '') != 'adjusted'])
    audit.shortage_qty = _q2(sum(-_q2(line.variance) for line in short))
    audit.overage_qty = _q2(sum(_q2(line.variance) for line in extra))
    audit.damaged_qty = _q2(sum(_q2(line.damaged_qty) for line in counted))
    audit.adjusted_lines = len([line for line in lines
                                if abs(_q2(line.adjusted_qty)) > EPS])
    scrap_ids = [int(line.scrap_id) for line in lines if line.scrap_id]
    if scrap_ids:
        write_off = db.session.query(func.coalesce(func.sum(ToolScrap.value_written_off), 0.0)).filter(
            ToolScrap.id.in_(scrap_ids)).scalar()
    else:
        write_off = 0.0
    audit.write_off_value = _q2(write_off or 0.0)

    if refresh_status and (audit.status or 'draft') in ('draft', 'counted'):
        untouched = [line for line in lines
                     if _q2(line.book_qty) > EPS and line.counted_qty is None]
        audit.status = 'counted' if (counted and not untouched) else 'draft'
    return audit


def save_counts(audit, counts, meta=None, user_id=None, ledger=None, commit=True):
    """Persist a typed-in sheet.

    ``counts`` is ``{tool_id: {'counted': float|None, 'damaged': float,
    'notes': str}}``.  A blank box is stored as ``None`` ("not counted"), which
    is deliberately different from a typed ``0`` ("counted — nothing here").
    The expected quantity is re-snapshotted from the live ledger on every save,
    so re-saving after a transfer compares against today's book, while what the
    count already posted stays on the line as ``adjusted_qty``.
    """
    if audit is None:
        return False, 'Audit not found.', audit
    if not audit.is_open:
        return False, 'This count is closed, so it can no longer be edited.', audit

    ledger = ledger or tool_ledger()
    spec = {'loc_type': audit.loc_type, 'project_id': audit.project_id,
            'stage_id': audit.stage_id, 'customer_name': audit.customer_name}
    book = book_position(ledger, spec)

    counts = counts or {}
    wanted = {int(tool_id) for tool_id in counts.keys()} | set(book.keys())
    tools = {}
    if wanted:
        for tool in Tool.query.filter(Tool.id.in_(sorted(wanted))).all():
            tools[int(tool.id)] = tool

    existing = {int(line.tool_id): line for line in lines_of(audit)}
    for tool_id in sorted(wanted):
        tool = tools.get(tool_id)
        if tool is None:
            continue
        payload = counts.get(tool_id) if tool_id in counts else counts.get(str(tool_id))
        payload = payload or {}
        raw = payload.get('counted', None)
        counted = None if raw is None or str(raw).strip() == '' else max(0.0, _q2(raw))
        entry = book.get(tool_id) or {'qty': 0.0, 'holdings': []}
        line = existing.get(tool_id)
        if line is None:
            line = ToolAuditLine(audit_id=audit.id, tool_id=tool_id)
            db.session.add(line)
            db.session.flush()
        line.tool_name = tool.name
        line.tool_code = tool.tool_code
        line.unit = tool.unit or 'pcs'
        line.book_qty = _q2(entry['qty'])
        line.counted_qty = counted
        line.damaged_qty = max(0.0, _q2(payload.get('damaged')))
        line.notes = (payload.get('notes') or '').strip()[:300]
        line.variance = 0.0 if counted is None else _q2(counted - line.book_qty)
        line.status = _state_for(line.book_qty, counted,
                                 abs(_q2(line.adjusted_qty)) > EPS or line.status == 'adjusted')
        line.updated_at = _pkt_now_naive()

    for name, value in (meta or {}).items():
        if name in _AUDIT_META_FIELDS:
            setattr(audit, name, value)
    recompute_audit_totals(audit)
    audit.updated_at = _pkt_now_naive()
    if commit:
        db.session.commit()
    return True, '', audit


def pending_audit_for(spec):
    """The one open sheet a place already has (never start a second story)."""
    key = location_key(spec['loc_type'], spec.get('project_id'),
                       spec.get('stage_id'), spec.get('customer_name'))
    audits = (ToolAudit.query
              .filter(ToolAudit.is_void == False,  # noqa: E712
                      ToolAudit.status.in_(('draft', 'counted')))
              .all())
    for audit in audits:
        if location_key(audit.loc_type, audit.project_id, audit.stage_id,
                        audit.customer_name) == key:
            return audit
    return None


def start_audit(spec, counter_name='', audit_date=None, reference='', notes='',
                 counts=None, user_id=None, ledger=None):
    """Open (or resume) the count sheet for one place.

    Exactly one open sheet per place, on purpose: two people counting the same
    store at the same time would produce two stories about the same stock, so a
    second *New count* resumes the sheet that is already open — and keeps the
    numbers typed on the preview by saving them with it.
    """
    existing = pending_audit_for(spec)
    meta = {}
    if (counter_name or '').strip():
        meta['counter_name'] = (counter_name or '').strip()[:150]
    if (reference or '').strip():
        meta['reference'] = (reference or '').strip()[:120]
    if (notes or '').strip():
        meta['notes'] = (notes or '').strip()[:500]
    if existing is not None:
        if meta or counts:
            save_counts(existing, counts or {}, meta=meta, user_id=user_id,
                        ledger=ledger)
        return False, existing

    ledger = ledger or tool_ledger()
    full_spec = {**spec, 'label': None}
    audit = ToolAudit(
        audit_code=_next_audit_code(),
        audit_date=_parse_day(audit_date),
        loc_type=spec['loc_type'],
        project_id=spec.get('project_id'),
        stage_id=spec.get('stage_id'),
        customer_name=(spec.get('customer_name') or '').strip() or None,
        location_label=spec_label(full_spec)[:300],
        counter_name=(counter_name or '').strip()[:150] or None,
        reference=(reference or '').strip()[:120] or None,
        notes=(notes or '').strip()[:500] or None,
        status='draft',
        created_by=user_id,
    )
    db.session.add(audit)
    db.session.flush()
    if counts:
        save_counts(audit, counts, user_id=user_id, ledger=ledger, commit=False)
    else:
        recompute_audit_totals(audit)
    db.session.commit()
    return True, audit


def _pending_at_location(ledger, spec, tool_id):
    """Rental lines that hold this tool at this place, biggest holding first."""
    holders = []
    for row in ledger['tools']:
        if int(row['tool_id']) != int(tool_id):
            continue
        for holding in row['holdings']:
            if not _holding_matches_location(holding, spec):
                continue
            item = holding.get('rental_item')
            if item is None:
                continue
            holders.append({'item': item, 'qty': _q2(holding.get('qty')),
                            'label': holding.get('label') or '',
                            'rental': holding.get('rental')})
    holders.sort(key=lambda entry: -entry['qty'])
    return holders


def _rental_line_for_gain(spec, tool_id):
    """Where an unexpectedly found tool can be parked so it keeps showing here."""
    pending_lines = (ToolRentalItem.query
                     .join(ToolRental, ToolRental.id == ToolRentalItem.rental_id)
                     .filter(ToolRentalItem.tool_id == int(tool_id),
                             ToolRental.is_void == False,  # noqa: E712
                             ToolRentalItem.qty_pending > EPS)
                     .order_by(ToolRentalItem.id.desc()).all())
    candidates = []
    for line in pending_lines:
        rental = line.rental
        if rental is None:
            continue
        if spec['loc_type'] == LOC_OWN_PROJECT:
            if int(rental.project_id or 0) == int(spec.get('project_id') or 0):
                candidates.append((line, _q2(line.qty_pending)))
        elif spec['loc_type'] == LOC_CUSTOMER:
            if ((rental.customer_name or '').strip().casefold()
                    == (spec.get('customer_name') or '').strip().casefold()):
                candidates.append((line, _q2(line.qty_pending)))
    if candidates:
        return max(candidates, key=lambda pair: pair[1])[0]
    # Nothing pending for this tool here, but the same customer / site may hold
    # it on another (fully returned) rental — that rental is its paperwork home.
    others = (ToolRentalItem.query
              .join(ToolRental, ToolRental.id == ToolRentalItem.rental_id)
              .filter(ToolRentalItem.tool_id == int(tool_id),
                      ToolRental.is_void == False).all())  # noqa: E712
    for line in others:
        rental = line.rental
        if rental is None:
            continue
        if spec['loc_type'] == LOC_OWN_PROJECT:
            if int(rental.project_id or 0) == int(spec.get('project_id') or 0):
                return line
        elif spec['loc_type'] == LOC_CUSTOMER:
            if ((rental.customer_name or '').strip().casefold()
                    == (spec.get('customer_name') or '').strip().casefold()):
                return line
    return None


def plan_adjustments(audit, ledger=None):
    """What the *Adjust* button would do — computed, nothing touched.

    Every shortage is checked against the stock that can honestly be written
    off: a piece can only leave the books if the balance ``owned == store + out``
    survives it.  Where a place holds fewer pieces on paper than the audit wants
    written off (bad legacy data), the plan says so instead of writing a
    negative stock row.
    """
    ledger = ledger or tool_ledger()
    spec = {'loc_type': audit.loc_type, 'project_id': audit.project_id,
            'stage_id': audit.stage_id, 'customer_name': audit.customer_name}
    plan = []
    for line in sorted(lines_of(audit), key=lambda row: (row.tool_name or '').casefold()):
        variance = _q2(line.variance)
        if abs(variance) <= EPS or (line.status or '') not in ('short', 'extra'):
            continue
        tool = db.session.get(Tool, int(line.tool_id))
        if tool is None or tool.is_void:
            plan.append({'kind': 'error', 'name': line.tool_name or 'Deleted tool',
                         'message': 'This tool no longer exists — remove the line '
                                    'or restore the tool before adjusting.'})
            continue
        holders = _pending_at_location(ledger, spec, int(tool.id))
        pending_here = _q2(sum(holder['qty'] for holder in holders))
        if variance < 0:
            loss = _q2(-variance)
            liftable = min(loss, pending_here) if spec['loc_type'] != LOC_STORE else 0.0
            # ``tool_available_for_integrity`` floors at zero, which would hide a
            # book that is already over-issued (more out than owned). The honest
            # cap for a write-off is owned − pending + whatever this count can
            # lift off the rental lines.
            available = _q2(_q2(tool.total_quantity) - _q2(tool.rented_out_qty))
            writable = _q2(max(0.0, min(loss, available + liftable)))
            unit_cost = _q2(_flt(tool.purchase_cost))
            plan.append({
                'kind': 'loss', 'line': line, 'tool': tool, 'name': tool.name,
                'code': tool.tool_code, 'unit': tool.unit or 'pcs', 'qty': loss,
                'liftable_qty': _q2(liftable), 'writable_qty': writable,
                'unit_cost': unit_cost, 'value': _q2(writable * unit_cost),
                'holders': holders, 'partial': writable < loss - EPS,
                'blocked': writable <= EPS,
                'message': (f'{tool.name}: only {writable:g} of {loss:g} '
                            f'{tool.unit or "pcs"} can be written off from this book'
                            if writable < loss - EPS else ''),
            })
            continue
        gain = _q2(variance)
        target = None if spec['loc_type'] == LOC_STORE else _rental_line_for_gain(
            spec, int(tool.id))
        unit_cost = _q2(_flt(tool.purchase_cost))
        plan.append({
            'kind': 'gain', 'line': line, 'tool': tool, 'name': tool.name,
            'code': tool.tool_code, 'unit': tool.unit or 'pcs', 'qty': gain,
            'target': target, 'holders': holders, 'unit_cost': unit_cost,
            'value': _q2(gain * unit_cost), 'liftable_qty': 0.0,
            'writable_qty': gain, 'partial': False, 'blocked': False,
            'message': ('' if target is not None or spec['loc_type'] == LOC_STORE else
                        f'{tool.name}: {gain:g} {tool.unit or "pcs"} added to store '
                        f'stock — transfer them to {audit.location_label} if they '
                        f'must show there'),
        })
    return plan


def _lift_pending(audit, entry, user_id):
    """Take lost pieces off the rental line still holding them.

    ``qty_rented`` shrinks rather than only the ``qty_pending`` cache:
    :func:`hdc.services.tool_rental.recalc_rental_totals` re-derives pending from
    rented − returned, so editing the cache alone would be undone by the next
    return or payment on that rental and the tools would come back to life.
    """
    remaining = _q2(min(entry['qty'], entry['liftable_qty']))
    if remaining <= EPS:
        return 0.0
    lifted = 0.0
    touched = []
    for holder in entry['holders']:
        if remaining <= EPS:
            break
        item = holder['item']
        take = _q2(min(remaining, _q2(item.qty_pending)))
        if take <= EPS:
            continue
        item.qty_rented = max(0.0, _q2(_q2(item.qty_rented) - take))
        item.qty_returned = min(_q2(item.qty_returned), item.qty_rented)
        item.qty_pending = max(0.0, _q2(item.qty_rented - item.qty_returned))
        item.notes = (f'{item.notes or ""} | {audit.audit_code}: -{take:g} lost '
                      f'(audit)').strip(' |')[:300]
        remaining = _q2(remaining - take)
        lifted = _q2(lifted + take)
        touched.append(int(item.rental_id))
        create_movement_log(
            tool_id=int(item.tool_id), rental_id=int(item.rental_id),
            movement_type=MOVEMENT_AUDIT_ADJUST,
            from_label=audit.location_label or holder['label'],
            to_label=AUDIT_LOSS_LABEL, qty=take,
            notes=f'{audit.audit_code} audit at {audit.location_label}: {take:g} '
                  f'{item.tool.unit if item.tool else ""} written off — not coming back'[:500])
    for rental_id in dict.fromkeys(touched):
        recalc_rental_totals(rental_id)
    return lifted


def _grant_gain(audit, entry, user_id, note_tail):
    """Add pieces the book did not know about: found tools are HDC's tools."""
    tool = db.session.get(Tool, int(entry['tool'].id))
    if tool is None:
        return
    qty = _q2(entry['qty'])
    tool.total_quantity = _q2(_q2(tool.total_quantity) + qty)
    tool.updated_at = _pkt_now_naive()
    parked = entry.get('target')
    if parked is not None:
        parked.qty_rented = _q2(_q2(parked.qty_rented) + qty)
        parked.qty_pending = _q2(_q2(parked.qty_pending) + qty)
        parked.notes = (f'{parked.notes or ""} | {audit.audit_code}: +{qty:g} found '
                        f'(audit)').strip(' |')[:300]
        recalc_rental_totals(int(parked.rental_id))
    create_movement_log(
        tool_id=int(tool.id),
        rental_id=int(parked.rental_id) if parked is not None else None,
        movement_type=MOVEMENT_AUDIT_ADJUST,
        from_label=AUDIT_GAIN_LABEL,
        to_label=(audit.location_label if parked is not None else STORE_LABEL),
        qty=qty,
        notes=f'{audit.audit_code} audit at {audit.location_label}: {qty:g} '
              f'{tool.unit or "pcs"} of {tool.name} found, added to stock'
              f'{f" under {parked.rental.rental_code}" if parked is not None and parked.rental else ""}. '
              f'{note_tail}'[:500])


def post_adjustments(audit, mode='losses', reason='lost', audit_date=None,
                     notes='', user_id=None, ledger=None, commit=True):
    """Write the count back into the books — the *Adjust* button.

    ``mode='losses'`` only writes off what is missing (the safe default: extra
    pieces may simply be paperwork nobody has filed yet).  ``mode='both'`` also
    adds an overage to owned stock, and parks it on the rental that holds the
    rest of that tool at the place so the position stays where the count found it.

    A shortage removes the pieces from the rental line **and** from
    ``total_quantity`` through the scrap register, so the identity
    ``owned == store + out`` is still true the moment the write-off posts, and
    the write-off is searchable in the same register as a hand-typed scrap.
    Rent already billed is never touched — money owed by a customer for a lost
    tool belongs on the rental's own page (a discount/settlement there), not in
    a stock sheet.
    """
    if audit is None:
        return False, 'Audit not found.', {}
    if not audit.is_open:
        return False, 'This count is already closed.', {'done': 0}
    reason_key = (reason or 'lost').strip().lower()
    if reason_key not in {key for key, _ in TOOL_SCRAP_REASONS}:
        reason_key = 'lost'
    mode = 'both' if str(mode or '').strip().lower() == 'both' else 'losses'
    when = _parse_day(audit_date)
    ledger = ledger or tool_ledger()
    note_tail = (notes or '').strip()

    plan = [entry for entry in plan_adjustments(audit, ledger=ledger)
            if entry.get('kind') == 'loss'
            or (entry.get('kind') == 'gain' and mode == 'both')]
    if not plan:
        audit.status = 'adjusted'
        audit.adjusted_at = _pkt_now_naive()
        if note_tail:
            audit.notes = f'{audit.notes or ""} | Verified: {note_tail[:200]}'.strip(' |')
        recompute_audit_totals(audit, refresh_status=False)
        audit.status = 'adjusted'
        if commit:
            db.session.commit()
        return True, '', {'done': 0, 'losses': 0, 'gains': 0, 'lost_qty': 0.0,
                          'found_qty': 0.0, 'write_off_value': 0.0, 'remarks': [],
                          'message': 'No discrepancy to adjust — the count is '
                                     'closed as verified.'}

    done_loss = done_gain = 0
    lost_qty = found_qty = write_off = 0.0
    remarks = []
    for entry in plan:
        line = entry['line']
        if entry.get('kind') == 'error' or entry.get('blocked'):
            remarks.append(entry.get('message') or
                           f'{entry.get("name", "Tool")}: nothing available to write off.')
            continue
        if entry['kind'] == 'loss':
            _lift_pending(audit, entry, user_id)
            ok, message, scrap = record_tool_scrap(
                tool_id=int(entry['tool'].id), qty=entry['writable_qty'],
                reason=reason_key, scrap_date=when, reference=audit.audit_code or '',
                notes=(f'{audit.audit_code} count at {audit.location_label}: '
                       f'{entry["name"]} short by {_q2(entry["qty"]):g}. '
                       f'{note_tail}'.strip())[:300],
                created_by=user_id, commit=False)
            if not ok:
                remarks.append(f'{entry["name"]}: {message}')
                continue
            line.adjusted_qty = _q2(-entry['writable_qty'])
            line.scrap_id = int(scrap.id) if scrap else None
            line.adjust_reason = reason_key
            lost_qty += entry['writable_qty']
            write_off += _q2(scrap.value_written_off) if scrap else 0.0
            done_loss += 1
            if entry.get('partial'):
                remarks.append(entry.get('message') or '')
        else:
            _grant_gain(audit, entry, user_id, note_tail)
            line.adjusted_qty = _q2(entry['qty'])
            line.adjust_reason = 'found_extra'
            found_qty += entry['qty']
            done_gain += 1
            if entry.get('message'):
                remarks.append(entry['message'])
        line.status = 'adjusted'
        line.adjusted_at = _pkt_now_naive()
        line.updated_at = _pkt_now_naive()

    recompute_audit_totals(audit, refresh_status=False)
    # A sheet only closes when nothing is left open on it: choosing "losses
    # only" deliberately leaves an overage unposted, and pretending it was
    # settled would hide it from the next person who opens the tab.
    remaining = [line for line in lines_of(audit) if (line.status or '') in ('short', 'extra')]
    audit.status = 'counted' if remaining else 'adjusted'
    if audit.status == 'adjusted':
        audit.adjusted_at = _pkt_now_naive()
    audit.updated_at = _pkt_now_naive()
    if commit:
        db.session.commit()

    summary = {'done': done_loss + done_gain, 'losses': done_loss, 'gains': done_gain,
               'lost_qty': _q2(lost_qty), 'found_qty': _q2(found_qty),
               'write_off_value': _q2(write_off), 'remaining': len(remaining),
               'status': audit.status,
               'remarks': [text for text in remarks if text]}
    if summary['done'] == 0:
        return False, summary['remarks'][0] if summary['remarks'] else 'Nothing to adjust.', summary
    return True, '', summary


def close_audit(audit, note='', user_id=None):
    """Close a sheet without touching stock — a clean count still has to be filed."""
    if audit is None:
        return False, 'Audit not found.'
    if not audit.is_open:
        return False, 'This count is already closed.'
    pending = [line for line in lines_of(audit)
               if line.counted_qty is None and _q2(line.book_qty) > EPS]
    if pending:
        return False, (f'{len(pending)} tool line(s) have no counted number yet. '
                       f'Type 0 for "nothing here", or leave the sheet open.')
    if (note or '').strip():
        audit.notes = f'{audit.notes or ""} | Closed: {note.strip()[:200]}'.strip(' |')
    recompute_audit_totals(audit, refresh_status=False)
    audit.status = 'adjusted'
    audit.adjusted_at = _pkt_now_naive()
    audit.updated_at = _pkt_now_naive()
    db.session.commit()
    return True, 'Audit closed — the count matched the book.'


def reopen_audit(audit):
    """Re-open a closed sheet when someone notices the count itself was wrong."""
    if audit is None:
        return False, 'Audit not found.'
    if audit.is_void:
        return False, 'A cancelled count cannot be re-opened.'
    audit.status = 'counted'
    recompute_audit_totals(audit, refresh_status=False)
    audit.status = 'counted'
    audit.updated_at = _pkt_now_naive()
    db.session.commit()
    return True, 'Audit re-opened. Adjustments already posted are not undone — ' \
                 'void them from the tool page if the count was wrong.'


def void_audit(audit, reason=''):
    """Cancel a sheet.  Stock already adjusted is deliberately *not* reversed."""
    if audit is None:
        return False, 'Audit not found.'
    if audit.is_void:
        return False, 'This count is already cancelled.'
    audit.is_void = True
    audit.status = 'void'
    audit.void_reason = (reason or 'Cancelled')[:250]
    audit.updated_at = _pkt_now_naive()
    db.session.commit()
    return True, ('Count cancelled. Anything already adjusted stays adjusted — '
                  'void that write-off from the tool page if it was a mistake.')


def open_audit_count():
    """Sheets still waiting to be finished or adjusted (the nav badge)."""
    return int(ToolAudit.query.filter(ToolAudit.is_void == False,  # noqa: E712
                                      ToolAudit.status.in_(('draft', 'counted'))).count() or 0)


def audit_rows_for_json(ledger=None):
    """Machine-readable audit comparison (JSON feed) — rows without ORM objects."""
    matrix = audit_matrix(ledger=ledger)
    locations = audit_locations(ledger or tool_ledger())
    return {
        'as_of': (ledger or {}).get('today', _pkt_today()),
        'totals': matrix['totals'],
        'locations': [{key: card[key] for key in
                       ('key', 'label', 'type_label', 'loc_type', 'expected_qty',
                        'expected_types', 'counted_qty', 'variance', 'discrepancy_count',
                        'uncounted_types', 'audit_id', 'audit_is_open', 'audit_count')}
                      for card in locations],
        'tools': [{key: row[key] for key in
                   ('tool_id', 'code', 'name', 'unit', 'category', 'owned_qty',
                    'in_store_qty', 'own_project_qty', 'customer_qty', 'expected_qty',
                    'counted_qty', 'variance_qty', 'damaged_qty', 'unverified_qty',
                    'status', 'unit_cost', 'value_variance')
                   if key in row}
                  for row in matrix['rows']],
    }
