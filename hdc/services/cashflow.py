"""HDC services.cashflow — daily money in / money out ledger and reporting.

Every flow of money in the system (owner receipts, expenses, wages,
purchases, cash<->bank transfers, personal payouts) is already posted to the
unified ``AccountTransaction`` ledger by its source module.  This service is
a pure read / aggregation layer over that ledger, viewed from the company's
(treasury) perspective.  Each active transaction touching at least one
company account (company / cash / bank) is classified:

  ``in``        money came INTO a company account
  ``out``       money left a company account
  ``internal``  moved between two company accounts (e.g. cash -> bank)

Rows that touch no company account are excluded from cash flow.  There is
deliberately no parallel table: the cash-flow records ARE the
AccountTransaction rows, so nothing can drift out of sync and every entry
keeps its receipt, traceability and void/reversal behaviour.
"""

from sqlalchemy import case, func, or_

from hdc.extensions import db
from hdc.models.accounts import Account, AccountTransaction
from hdc.services.accounts import _ACCOUNT_COMPANY_TYPES, _account_group_mode_for_row
from hdc.utils.dates import _pkt_today


CASHFLOW_COMPANY_TYPES = tuple(_ACCOUNT_COMPANY_TYPES)  # company / cash / bank


CASHFLOW_DIRECTIONS = (
    ('in', 'Money In'),
    ('out', 'Money Out'),
    ('internal', 'Internal Transfer'),
)
CASHFLOW_DIRECTION_LABELS = dict(CASHFLOW_DIRECTIONS)


def _cashflow_account_maps():
    """Return (type_map, mode_map): id -> account type / cash-or-bank mode.

    Includes voided accounts so historical rows keep classifying correctly.
    """
    type_map = {}
    mode_map = {}
    for a in Account.query.all():
        aid = int(a.id)
        type_map[aid] = (a.type or '').strip().lower()
        _group, mode = _account_group_mode_for_row(a)
        mode_map[aid] = mode
    return type_map, mode_map


def _cashflow_classify(txn, type_map):
    """Classify one AccountTransaction row from the company treasury view."""
    frm_co = type_map.get(int(txn.from_account_id or 0), '') in CASHFLOW_COMPANY_TYPES
    to_id = int(txn.to_account_id or 0) if txn.to_account_id else 0
    to_co = bool(to_id) and (type_map.get(to_id, '') in CASHFLOW_COMPANY_TYPES)
    if frm_co and to_co:
        return 'internal'
    if frm_co:
        return 'out'
    if to_co:
        return 'in'
    return ''


def _cashflow_matches_mode(txn, cls, mode, mode_map):
    """Does the company side of this row involve a cash / bank account of ``mode``?"""
    if not mode:
        return True
    if cls == 'in':
        ids = [int(txn.to_account_id or 0)]
    elif cls == 'out':
        ids = [int(txn.from_account_id or 0)]
    else:
        ids = [int(txn.from_account_id or 0), int(txn.to_account_id or 0)]
    for aid in ids:
        if aid and mode_map.get(aid) == mode:
            return True
    return False


def _cashflow_row_dict(txn, cls):
    to_name = (txn.to_account.name if txn.to_account else '') or (txn.party_name or '')
    return {
        # lets the list row carry "entered by" audit attributes
        '_hdc_entity': 'hdc_account_txn',
        '_hdc_id': int(txn.id),
        'id': int(txn.id),
        'date': (txn.date or _pkt_today()),
        'direction': cls,
        'type': (txn.type or ''),
        'type_label': str(txn.type or '').replace('_', ' ').title(),
        'category': (txn.category or 'other').strip().lower() or 'other',
        'from_account': (txn.from_account.name if txn.from_account else '-'),
        'to_account': (txn.to_account.name if txn.to_account else ''),
        'to_party': (to_name or '-').strip(),
        'party_name': (txn.party_name or '').strip(),
        'project': (txn.project.name if txn.project else ''),
        'stage': (txn.stage.name if txn.stage else ''),
        'amount': float(txn.amount or 0.0),
        'note': (txn.note or '').strip(),
        'reference_id': (txn.reference_id or '').strip(),
    }


def _cashflow_base_query(date_from=None, date_to=None, account_id=None,
                         category=None, project_id=None, party_name=None):
    q = AccountTransaction.query.filter(AccountTransaction.is_void == False)
    if date_from:
        q = q.filter(AccountTransaction.date >= date_from)
    if date_to:
        q = q.filter(AccountTransaction.date <= date_to)
    if account_id:
        q = q.filter(or_(AccountTransaction.from_account_id == int(account_id),
                         AccountTransaction.to_account_id == int(account_id)))
    if category:
        q = q.filter(func.lower(func.coalesce(AccountTransaction.category, '')) == str(category).strip().lower())
    if project_id:
        q = q.filter(AccountTransaction.project_id == int(project_id))
    if party_name:
        pat = f"%{str(party_name).strip().lower()}%"
        q = q.filter(or_(
            func.lower(func.coalesce(AccountTransaction.party_name, '')).like(pat),
            func.lower(func.coalesce(AccountTransaction.note, '')).like(pat),
        ))
    return q


def _cashflow_load_rows(date_from=None, date_to=None, account_id=None, account_mode=None,
                        category=None, direction=None, project_id=None, party_name=None,
                        limit=None):
    """Classified cash-flow rows, newest first.

    ``account_mode`` filters on the company side (cash or bank) and
    ``direction`` on the in/out/internal classification; the rest run in SQL.
    """
    q = _cashflow_base_query(
        date_from=date_from, date_to=date_to, account_id=account_id,
        category=category, project_id=project_id, party_name=party_name,
    )
    q = q.order_by(AccountTransaction.date.desc(), AccountTransaction.id.desc())
    if limit:
        q = q.limit(int(limit))
    type_map, mode_map = _cashflow_account_maps()
    mode = (account_mode or '').strip().lower()
    direction = (direction or '').strip().lower()
    out = []
    for t in q.all():
        cls = _cashflow_classify(t, type_map)
        if not cls:
            continue
        if direction and cls != direction:
            continue
        if mode and not _cashflow_matches_mode(t, cls, mode, mode_map):
            continue
        out.append(_cashflow_row_dict(t, cls))
    return out


def _cashflow_summary(rows):
    in_total = sum(r['amount'] for r in rows if r['direction'] == 'in')
    out_total = sum(r['amount'] for r in rows if r['direction'] == 'out')
    internal_total = sum(r['amount'] for r in rows if r['direction'] == 'internal')
    return {
        'in_total': float(in_total),
        'out_total': float(out_total),
        'internal_total': float(internal_total),
        'net_total': float(in_total - out_total),
        'in_count': sum(1 for r in rows if r['direction'] == 'in'),
        'out_count': sum(1 for r in rows if r['direction'] == 'out'),
        'internal_count': sum(1 for r in rows if r['direction'] == 'internal'),
        'row_count': len(rows),
        'day_count': len({r['date'] for r in rows}),
    }


def _cashflow_group_by_day(rows):
    """Group rows into complete days (newest first) with day totals."""
    groups = {}
    for r in rows:
        g = groups.setdefault(r['date'], {
            'date': r['date'], 'in_total': 0.0, 'out_total': 0.0,
            'internal_total': 0.0, 'count': 0, 'rows': [],
        })
        if r['direction'] == 'in':
            g['in_total'] += r['amount']
        elif r['direction'] == 'out':
            g['out_total'] += r['amount']
        else:
            g['internal_total'] += r['amount']
        g['count'] += 1
        g['rows'].append(r)
    out = sorted(groups.values(), key=lambda g: g['date'], reverse=True)
    for g in out:
        g['in_total'] = float(g['in_total'])
        g['out_total'] = float(g['out_total'])
        g['internal_total'] = float(g['internal_total'])
        g['net'] = float(g['in_total'] - g['out_total'])
    return out


def _cashflow_daily_report_rows(rows, opening_funds=0.0):
    """Ascending daily table with a running company-funds closing balance.

    Internal transfers never change company funds, so the running balance
    only moves with money in minus money out.
    """
    groups = list(reversed(_cashflow_group_by_day(rows)))
    running = float(opening_funds or 0.0)
    out = []
    for g in groups:
        running += g['in_total'] - g['out_total']
        out.append({
            'date': g['date'],
            'in_total': float(g['in_total']),
            'out_total': float(g['out_total']),
            'internal_total': float(g['internal_total']),
            'net': float(g['net']),
            'count': g['count'],
            'closing': float(running),
        })
    return out


def _cashflow_opening_funds(date_from=None):
    """Company money in the treasury strictly before ``date_from``.

    Sum of opening balances of all company accounts plus the net cash flow
    (in minus out, transfers excluded) of every earlier active transaction.
    With no ``date_from`` the report covers full history, so only the opening
    balances are returned.
    """
    type_map, _ = _cashflow_account_maps()
    company_ids = [aid for aid, tp in type_map.items() if tp in CASHFLOW_COMPANY_TYPES]
    if not company_ids:
        return 0.0
    opening = float(db.session.query(func.coalesce(func.sum(Account.opening_balance), 0.0))
                    .filter(Account.id.in_(company_ids)).scalar() or 0.0)
    if not date_from:
        return opening
    from_co = AccountTransaction.from_account_id.in_(company_ids)
    to_co = AccountTransaction.to_account_id.in_(company_ids)
    to_not_co = or_(AccountTransaction.to_account_id.is_(None),
                    AccountTransaction.to_account_id.notin_(company_ids))
    in_sum = case((to_co & ~from_co, AccountTransaction.amount), else_=0.0)
    out_sum = case((from_co & to_not_co, AccountTransaction.amount), else_=0.0)
    delta = float(db.session.query(
        func.coalesce(func.sum(in_sum), 0.0) - func.coalesce(func.sum(out_sum), 0.0)
    ).filter(
        AccountTransaction.is_void == False,
        AccountTransaction.date < date_from,
    ).scalar() or 0.0)
    return opening + delta


def _cashflow_category_breakdown(rows):
    by_cat = {}
    for r in rows:
        s = by_cat.setdefault(r['category'], {
            'in_total': 0.0, 'out_total': 0.0, 'internal_total': 0.0, 'count': 0,
        })
        if r['direction'] == 'in':
            s['in_total'] += r['amount']
        elif r['direction'] == 'out':
            s['out_total'] += r['amount']
        else:
            s['internal_total'] += r['amount']
        s['count'] += 1
    total = sum(s['in_total'] + s['out_total'] for s in by_cat.values())
    out = []
    for cat, s in by_cat.items():
        share = s['in_total'] + s['out_total']
        out.append({
            'category': cat,
            'label': cat.replace('_', ' ').title(),
            'in_total': float(s['in_total']),
            'out_total': float(s['out_total']),
            'internal_total': float(s['internal_total']),
            'net': float(s['in_total'] - s['out_total']),
            'count': s['count'],
            'pct': (round(100.0 * share / total, 1) if total else 0.0),
        })
    out.sort(key=lambda x: (x['in_total'] + x['out_total']), reverse=True)
    return out


def _cashflow_account_breakdown(rows):
    """Per company account: money in / out / net plus internal transfers in / out."""
    def _slot(name):
        return {'name': name, 'in_total': 0.0, 'out_total': 0.0,
                'transfer_in': 0.0, 'transfer_out': 0.0}
    acc = {}
    for r in rows:
        if r['direction'] == 'in':
            acc.setdefault(r['to_account'], _slot(r['to_account']))['in_total'] += r['amount']
        elif r['direction'] == 'out':
            acc.setdefault(r['from_account'], _slot(r['from_account']))['out_total'] += r['amount']
        else:
            frm = acc.setdefault(r['from_account'], _slot(r['from_account']))
            to = acc.setdefault(r['to_account'], _slot(r['to_account']))
            frm['transfer_out'] += r['amount']
            to['transfer_in'] += r['amount']
    _type_map, mode_map = _cashflow_account_maps()
    name_to_mode = {}
    for a in Account.query.all():
        name_to_mode[(a.name or '').strip()] = mode_map.get(int(a.id), '')
    out = []
    for nm, s in acc.items():
        out.append({
            'name': nm,
            'mode': (name_to_mode.get(nm, '') or 'cash'),
            'in_total': float(s['in_total']),
            'out_total': float(s['out_total']),
            'net': float(s['in_total'] - s['out_total']),
            'transfer_in': float(s['transfer_in']),
            'transfer_out': float(s['transfer_out']),
        })
    out.sort(key=lambda x: (x['in_total'] + x['out_total']
                            + x['transfer_in'] + x['transfer_out']), reverse=True)
    return out
