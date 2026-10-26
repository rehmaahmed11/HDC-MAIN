"""HDC services.labour_card — the per-worker "Labour Card" aggregation.

One card for one worker over one date range:

* a row per **site** (project): days worked there, overtime hours and the
  wage earned there — plus the site's own Fuel / Food expenses in the same
  dates, shown for information only;
* the period money summary: grand total earned, advances taken, amount
  paid, settlements, the opening balance brought forward and the closing
  balance — ending in the single **Payable Now** figure;
* the **last 4 weeks** (Monday–Sunday) ending with the week of ``date_to``:
  days worked, earned, advance taken, paid, settled and the pending
  (cumulative still-owed) balance at each week end.

Definitions deliberately match the rest of the app so the card can never
disagree with the Worker Ledger / Statement screens:

* **Earned** comes from ``hdc_time_entry.wage_calculated`` (non-void) plus
  legacy ``hdc_attendance.total_wage`` rows that were never migrated —
  the exact source of truth used by
  :func:`hdc.services.ledger._worker_payable_snapshots`, including its
  rule that a legacy row counts unless an *active* (non-void) migrated
  time entry points at it (LABOUR_AUDIT #1/#12).
* **Balance** = earned − advances − payments − settlements.  **Tips are
  never deducted** (gratis cash on top of the wage).
* **Payable Now** is taken straight from the canonical payable snapshot
  (whole ledger, ``max(0, balance)``), not recomputed here.
* **Days** = regular hours ÷ 8 (regular = ``hours − overtime`` on a time
  entry; overtime is the portion beyond the day's first 8 hours — see
  ``_recalculate_attendance_day``).  A day split across two sites therefore
  shows as fractions that still add up to 1 (4h + 4h = 0.5 + 0.5), and the
  days column always sums to the total.  Legacy attendance rows use their
  stored ``full_day_equivalent``.
* **Fuel / Food** are *site* expenses (``hdc_expense``) on that project in
  the selected dates whose Expense Category name contains
  fuel/petrol/diesel resp. food/refreshment.  They belong to the whole
  site — every worker's card for that site shows the same figure — so they
  are **never added** to the worker's grand total, balance or payable.

Weeks run Monday–Sunday (``date.weekday()``).  A week's **Pending** is the
cumulative owed balance at that Sunday (everything earned up to then minus
every advance/payment/settlement up to then), because a payment made in a
later week usually settles an earlier week's wage — a per-week remainder
would wrongly show the worker as overpaid.
"""

from datetime import timedelta

from hdc.extensions import db
from hdc.models.accounts import Expense, ExpenseCategory
from hdc.models.projects import Project
from hdc.models.workforce import Attendance, LabourLedger, TimeEntry, Worker
from hdc.services.ledger import _worker_payable_snapshots
from hdc.utils.dates import _pkt_today

#: Expense-category name fragments that count as a site's Fuel / Food spend.
#: Categories are operator-managed (Expense Categories screen), so the card
#: matches by name fragment and reports which categories it actually counted.
FUEL_CATEGORY_KEYWORDS = ('fuel', 'petrol', 'diesel')
FOOD_CATEGORY_KEYWORDS = ('food', 'refreshment')

#: Ledger kinds that move a worker's balance, plus tips (tracked, neutral).
_MONEY_TYPES = ('advance', 'payment', 'settlement', 'tip')

#: Cap on the Fuel/Food detail lines returned with a card (totals are never
#: capped — only the printable line-by-line list is, with a flag when cut).
SITE_EXPENSE_DETAIL_LIMIT = 500


def classify_site_expense(category_name):
    """Return ``'fuel'`` / ``'food'`` / ``None`` for an expense category name."""
    name = (category_name or '').strip().lower()
    if not name:
        return None
    if any(word in name for word in FUEL_CATEGORY_KEYWORDS):
        return 'fuel'
    if any(word in name for word in FOOD_CATEGORY_KEYWORDS):
        return 'food'
    return None


def _regular_hours(entry):
    """Regular (non-overtime) hours on one time entry.

    Current timekeeping stores ``hours`` as the entry's *total* hours with
    ``overtime`` as the portion beyond the day's first 8h, so regular =
    hours − overtime.  Rows created by the one-off legacy migration
    (``legacy_calc``) instead copied ``hours_worked`` (the regular hours)
    and kept overtime as extra on top, so for those, ``hours`` already is
    the regular figure.
    """
    hours = max(0.0, float(entry.hours or 0.0))
    overtime = max(0.0, float(entry.overtime or 0.0))
    if getattr(entry, 'legacy_calc', False):
        return hours, overtime
    if overtime <= hours:
        return hours - overtime, overtime
    # Unreconciled overtime-only fragment: all of it is overtime.
    return 0.0, overtime


def _work_rows(worker_id):
    """Every wage-earning row for a worker, normalised to one shape.

    ``{date, project_id, days, overtime, regular_hours, amount, source}``
    merging live time entries with never-migrated legacy attendance.
    """
    rows = []
    entries = (TimeEntry.query
               .filter(TimeEntry.worker_id == worker_id,
                       TimeEntry.is_void == False)  # noqa: E712
               .order_by(TimeEntry.check_in.asc(), TimeEntry.id.asc())
               .all())
    migrated_attendance_ids = {
        int(e.attendance_id) for e in entries if e.attendance_id
    }
    for entry in entries:
        if entry.check_in is None:
            continue
        regular, overtime = _regular_hours(entry)
        rows.append({
            'date': entry.check_in.date(),
            'project_id': int(entry.project_id or 0),
            'days': regular / 8.0,
            'overtime': overtime,
            'regular_hours': regular,
            'amount': float(entry.wage_calculated or 0.0),
            'source': 'time',
        })

    legacy = (Attendance.query
              .filter(Attendance.worker_id == worker_id)
              .order_by(Attendance.date.asc(), Attendance.id.asc())
              .all())
    for att in legacy:
        if int(att.id) in migrated_attendance_ids or att.date is None:
            continue
        days = float(att.full_day_equivalent or 0.0)
        if days <= 0:
            days = min(1.0, max(0.0, float(att.hours_worked or 0.0)) / 8.0)
        rows.append({
            'date': att.date,
            'project_id': int(att.project_id or 0),
            'days': days,
            'overtime': max(0.0, float(att.overtime_hours or 0.0)),
            'regular_hours': days * 8.0,
            'amount': float(att.total_wage or 0.0),
            'source': 'legacy',
        })
    rows.sort(key=lambda r: r['date'])
    return rows


def _money_rows(worker_id):
    """Every non-void advance/payment/settlement/tip ledger row, dated."""
    out = []
    ledger = (LabourLedger.query
              .filter(LabourLedger.worker_id == worker_id,
                      LabourLedger.is_void == False,  # noqa: E712
                      LabourLedger.entry_type.in_(_MONEY_TYPES))
              .order_by(LabourLedger.activity_at.asc(), LabourLedger.id.asc())
              .all())
    for row in ledger:
        when = row.date or (row.activity_at.date() if row.activity_at else None)
        if when is None:
            continue
        out.append({
            '_hdc_entity': 'hdc_labour_ledger',
            'id': int(row.id),
            'row': row,
            'date': when,
            'entry_type': (row.entry_type or '').strip().lower(),
            'amount': float(row.amount or 0.0),
            'project_id': int(row.project_id or 0),
            'notes': (row.notes or '').strip(),
        })
    return out


def _sum_by_type(rows):
    totals = {kind: 0.0 for kind in _MONEY_TYPES}
    for row in rows:
        totals[row['entry_type']] = totals.get(row['entry_type'], 0.0) + row['amount']
    return totals


def _earned(rows):
    return float(sum(r['amount'] for r in rows))


def _balance(work_rows, money_totals):
    return (_earned(work_rows)
            - money_totals.get('advance', 0.0)
            - money_totals.get('payment', 0.0)
            - money_totals.get('settlement', 0.0))


def _project_names(project_ids):
    ids = [pid for pid in dict.fromkeys(int(p or 0) for p in project_ids) if pid]
    if not ids:
        return {}
    return {int(p.id): (p.name or f'Site #{p.id}')
            for p in Project.query.filter(Project.id.in_(ids)).all()}


def _site_fuel_food(project_ids, date_from, date_to):
    """Fuel/Food site expenses per project within the bounds (info only)."""
    per_site = {}
    detail = []
    counted = set()
    total_count = 0
    if project_ids:
        query = (db.session.query(Expense, ExpenseCategory.name)
                 .outerjoin(ExpenseCategory,
                            Expense.category_id == ExpenseCategory.id)
                 .filter(Expense.project_id.in_(project_ids),
                         Expense.is_void == False))  # noqa: E712
        if date_from is not None:
            query = query.filter(Expense.date >= date_from)
        if date_to is not None:
            query = query.filter(Expense.date <= date_to)
        for expense, category_name in (query.order_by(Expense.date.asc(),
                                                      Expense.id.asc()).all()):
            kind = classify_site_expense(category_name)
            if kind is None:
                continue
            pid = int(expense.project_id or 0)
            amount = float(expense.amount or 0.0)
            bucket = per_site.setdefault(pid, {'fuel': 0.0, 'food': 0.0})
            bucket[kind] += amount
            counted.add((category_name or '').strip())
            total_count += 1
            if len(detail) < SITE_EXPENSE_DETAIL_LIMIT:
                detail.append({
                    '_hdc_entity': 'hdc_expense',
                    'id': int(expense.id),
                    'date': expense.date,
                    'project_id': pid,
                    'category': (category_name or '').strip(),
                    'kind': kind,
                    'amount': amount,
                    'remarks': (expense.remarks or '').strip(),
                })
    return {
        'per_site': per_site,
        'detail': detail,
        'detail_total_count': total_count,
        'detail_truncated': total_count > len(detail),
        'counted_categories': sorted(c for c in counted if c),
    }


def _last_four_weeks(work_rows, money_rows, anchor):
    """The 4 Monday–Sunday weeks ending with the week holding ``anchor``."""
    this_monday = anchor - timedelta(days=anchor.weekday())
    starts = [this_monday - timedelta(weeks=offset) for offset in (3, 2, 1, 0)]

    opening = _balance(
        [r for r in work_rows if r['date'] < starts[0]],
        _sum_by_type([r for r in money_rows if r['date'] < starts[0]]),
    )

    weeks = []
    pending = opening
    for start in starts:
        end = start + timedelta(days=6)
        in_week_work = [r for r in work_rows if start <= r['date'] <= end]
        in_week_money = [r for r in money_rows if start <= r['date'] <= end]
        money = _sum_by_type(in_week_money)
        earned = _earned(in_week_work)
        week_balance = (earned - money['advance'] - money['payment']
                        - money['settlement'])
        pending += week_balance
        weeks.append({
            'start': start,
            'end': end,
            'is_current': start == this_monday,
            'label': f"{start.strftime('%d %b')} – {end.strftime('%d %b %Y')}",
            'days': float(sum(r['days'] for r in in_week_work)),
            'overtime': float(sum(r['overtime'] for r in in_week_work)),
            'earned': earned,
            'advance': money['advance'],
            'paid': money['payment'],
            'settled': money['settlement'],
            'tip': money['tip'],
            'week_balance': week_balance,
            'pending': pending,
        })

    totals = {
        'days': sum(w['days'] for w in weeks),
        'overtime': sum(w['overtime'] for w in weeks),
        'earned': sum(w['earned'] for w in weeks),
        'advance': sum(w['advance'] for w in weeks),
        'paid': sum(w['paid'] for w in weeks),
        'settled': sum(w['settled'] for w in weeks),
        'tip': sum(w['tip'] for w in weeks),
        'week_balance': sum(w['week_balance'] for w in weeks),
        'pending': pending,
    }
    return {'weeks': weeks, 'totals': totals, 'opening_before': opening}


def labour_card(worker_id, date_from=None, date_to=None):
    """Build the full Labour Card for one worker; ``None`` if no such worker.

    ``date_from`` / ``date_to`` are inclusive ``date`` bounds; ``None``
    leaves that side open (all time).  Bounds are swapped if reversed.
    """
    wid = int(worker_id or 0)
    worker = Worker.query.get(wid) if wid else None
    if worker is None:
        return None
    if date_from is not None and date_to is not None and date_from > date_to:
        date_from, date_to = date_to, date_from

    work = _work_rows(wid)
    money = _money_rows(wid)

    def _in_range(day):
        return ((date_from is None or day >= date_from)
                and (date_to is None or day <= date_to))

    work_in = [r for r in work if _in_range(r['date'])]
    work_before = [r for r in work if date_from is not None and r['date'] < date_from]
    money_in = [r for r in money if _in_range(r['date'])]
    money_before = [r for r in money if date_from is not None and r['date'] < date_from]

    # --- per-site rows -------------------------------------------------------
    sites_by_id = {}
    for row in work_in:
        site = sites_by_id.setdefault(row['project_id'], {
            'project_id': row['project_id'],
            'name': '',
            'days': 0.0,
            'overtime': 0.0,
            'regular_hours': 0.0,
            'amount': 0.0,
            'fuel': 0.0,
            'food': 0.0,
            '_dates': set(),
        })
        site['days'] += row['days']
        site['overtime'] += row['overtime']
        site['regular_hours'] += row['regular_hours']
        site['amount'] += row['amount']
        site['_dates'].add(row['date'])

    project_ids = list(sites_by_id)
    names = _project_names(
        project_ids + [r['project_id'] for r in money_in])
    for site in sites_by_id.values():
        site['name'] = names.get(site['project_id'],
                                 f"Site #{site['project_id']}" if site['project_id'] else 'No site')
        site['dates_worked'] = len(site.pop('_dates'))

    fuel_food = _site_fuel_food(project_ids, date_from, date_to)
    for site in sites_by_id.values():
        amounts = fuel_food['per_site'].get(site['project_id']) or {}
        site['fuel'] = float(amounts.get('fuel', 0.0))
        site['food'] = float(amounts.get('food', 0.0))
    for line in fuel_food['detail']:
        line['site_name'] = names.get(line['project_id'],
                                      f"Site #{line['project_id']}")

    sites = sorted(sites_by_id.values(),
                   key=lambda s: (-s['amount'], (s['name'] or '').lower()))

    # --- period money summary ------------------------------------------------
    period_money = _sum_by_type(money_in)
    grand_total = _earned(work_in)
    opening_balance = _balance(work_before, _sum_by_type(money_before))
    period_balance = (grand_total - period_money['advance']
                      - period_money['payment'] - period_money['settlement'])
    closing_balance = opening_balance + period_balance

    snapshot = _worker_payable_snapshots([wid])[wid]
    balance_now = float(snapshot['balance'] or 0.0)

    for row in money_in:
        row['site_name'] = names.get(row['project_id'], '')

    anchor = date_to or _pkt_today()
    activity_dates = [r['date'] for r in work] + [r['date'] for r in money]

    return {
        'worker': worker,
        'date_from': date_from,
        'date_to': date_to,
        'sites': sites,
        'total_days': float(sum(s['days'] for s in sites)),
        'total_overtime': float(sum(s['overtime'] for s in sites)),
        'grand_total': grand_total,
        'total_fuel': float(sum(s['fuel'] for s in sites)),
        'total_food': float(sum(s['food'] for s in sites)),
        'site_expenses': fuel_food,
        'money_rows': money_in,
        'period': {
            'advance': period_money['advance'],
            'paid': period_money['payment'],
            'settled': period_money['settlement'],
            'tip': period_money['tip'],
            'opening_balance': opening_balance,
            'period_balance': period_balance,
            'closing_balance': closing_balance,
        },
        'snapshot': snapshot,
        'balance_now': balance_now,
        'payable_now': float(snapshot['payable'] or 0.0),
        'excess_advance_now': max(0.0, -balance_now),
        # Non-zero when entries exist after ``date_to`` (card is historical).
        'balance_after_to': balance_now - closing_balance,
        'last_4_weeks': _last_four_weeks(work, money, anchor),
        'first_activity': min(activity_dates) if activity_dates else None,
    }
