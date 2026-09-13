#!/usr/bin/env python3
"""Read-only consistency audit of labour wages, payments, tips and advances.

HDC keeps the same money in four places that are computed independently:

  * ``hdc_time_entry.wage_calculated``  -- what a worker earned
  * ``hdc_labour_ledger``               -- work / advance / payment / tip / settlement
  * ``hdc_expense`` (Tip, Settlement)   -- the cash that actually left the company
  * ``hdc_account_txn``                 -- the unified accounts mirror of the above

…and it then reports worker "balance due" in **two different ways**
(``Worker.balance_due`` in ``hdc/models/workforce.py`` subtracts tips, while
``_worker_payable_snapshot`` in ``hdc/services/ledger.py`` deliberately does
not).  Any drift between those four sources is invisible on screen but shows up
as over-paid workers, resurrected tips, or wages that silently vanish.

This script re-derives every figure from the tables and reports the drift.
It opens the database with SQLite's ``mode=ro`` URI, so it can never write to
(or lock) a production database -- it is safe to run against the live file.

Usage:
    python3 scripts/labour_audit.py --db /path/to/hdc_erp.db
    python3 scripts/labour_audit.py --db hdc_instance/hdc_erp.db --json audit.json
    python3 scripts/labour_audit.py --db ... --csv findings.csv --top 25

Exit status is 1 when any CRITICAL/HIGH finding is present (use
``--exit-zero`` to always return 0, e.g. from a report-only cron job).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict

SEVERITIES = ('CRITICAL', 'HIGH', 'MEDIUM', 'LOW', 'INFO')

MONEY_TOL = 0.51          # PKR; wages are rounded to whole rupees on screen
HOUR_TOL = 0.01
MAX_DAY_HOURS = 24.0

TIP_WORKER_RE = re.compile(r'(?:^|[^\d])TIP_WORKER_ID:(\d+)(?!\d)')
TIP_EXPENSE_RE = re.compile(r'(?:^|[^\d])TIP_EXPENSE_ID:(\d+)(?!\d)')
SETTLE_WORKER_RE = re.compile(r'(?:^|[^\d])SETTLE_WORKER_ID:(\d+)(?!\d)')
PAYROLL_RUN_RE = re.compile(r'Payroll run #(\d+)\s')


# --------------------------------------------------------------------------
# database access (read-only)
# --------------------------------------------------------------------------
def open_db(path):
    """Open ``path`` read-only; raise a friendly error if it is not a DB."""
    if not os.path.exists(path):
        raise SystemExit(f'Database not found: {path}')
    uri = f'file:{os.path.abspath(path)}?mode=ro'
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def columns(conn, table):
    try:
        return {r['name'] for r in conn.execute(f'PRAGMA table_info({table})')}
    except sqlite3.DatabaseError:
        return set()


def table_exists(conn, table):
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def rows(conn, sql, params=()):
    try:
        return [dict(r) for r in conn.execute(sql, params)]
    except sqlite3.DatabaseError:
        return []


# --------------------------------------------------------------------------
# findings
# --------------------------------------------------------------------------
def finding(check, severity, title, detail, worker=None, amount=None, refs=None):
    return {
        'check': check,
        'severity': severity,
        'title': title,
        'detail': detail,
        'worker': worker or '',
        'amount': round(float(amount or 0.0), 2),
        'refs': refs or '',
    }


def f2(v):
    try:
        return float(v or 0.0)
    except (TypeError, ValueError):
        return 0.0


def day(ts):
    return (str(ts or '')[:10] or '')


# --------------------------------------------------------------------------
# wage engine (mirrors hdc/services/timekeeping.py)
# --------------------------------------------------------------------------
def rate_on(worker, rate_rows, on_date):
    """Mirror of ``_worker_rate_on``: rate effective on ``on_date``."""
    if on_date:
        cands = [r for r in rate_rows
                 if r['worker_id'] == worker['id']
                 and r['effective_from'] and str(r['effective_from']) <= on_date]
        if cands:
            r = sorted(cands, key=lambda x: (str(x['effective_from']), x['id']),
                       reverse=True)[0]
            return (r['wage_type'] or worker['wage_type'] or 'daily'), f2(r['rate'])
    wt = (worker['wage_type'] or 'daily')
    if wt == 'hourly':
        return wt, f2(worker.get('hourly_rate'))
    if wt == 'per_sqft':
        return wt, f2(worker.get('rate_per_sqft'))
    return 'daily', f2(worker.get('base_daily_wage'))


def calc_time_wage(worker, rate_rows, hours, overtime, qty_sqft, on_date):
    """Mirror of ``_calc_time_wage`` (daily / hourly / per_sqft)."""
    wt, base = rate_on(worker, rate_rows, on_date)
    if wt == 'per_sqft':
        return max(0.0, base * f2(qty_sqft)), wt, base
    if wt == 'hourly':
        # Overtime is paid at straight time for hourly workers too.
        return max(0.0, base * (max(0.0, f2(hours)) + max(0.0, f2(overtime)))), wt, base
    hours = max(0.0, f2(hours))
    full_day = min(1.0, hours / 8.0) if hours > 0 else 0.0
    hourly = (base / 8.0) if base > 0 else 0.0
    return max(0.0, base * full_day + f2(overtime) * hourly), wt, base


# --------------------------------------------------------------------------
# loaders
# --------------------------------------------------------------------------
def load(conn):
    data = {}
    data['has_worker_rate'] = table_exists(conn, 'hdc_worker_rate')
    data['has_tip_worker_id'] = 'tip_worker_id' in columns(conn, 'hdc_expense')

    data['workers'] = rows(conn, """
        SELECT id, worker_code, name, role_type, wage_type,
               base_daily_wage, hourly_rate, rate_per_sqft, active_status
        FROM hdc_worker ORDER BY id""")
    data['worker_by_id'] = {w['id']: w for w in data['workers']}
    wname = {w['id']: f"{w['name']} ({w['worker_code']})" for w in data['workers']}
    data['wname'] = wname

    data['rates'] = (rows(conn, """
        SELECT id, worker_id, wage_type, rate, effective_from
        FROM hdc_worker_rate ORDER BY worker_id, effective_from, id""")
        if data['has_worker_rate'] else [])
    data['rates_by_worker'] = defaultdict(list)
    for r in data['rates']:
        data['rates_by_worker'][r['worker_id']].append(r)

    data['time_entries'] = rows(conn, """
        SELECT id, worker_id, project_id, stage_id, check_in, check_out,
               hours, overtime, qty_sqft, wage_calculated, legacy_calc,
               attendance_id, is_void, void_reason
        FROM hdc_time_entry ORDER BY worker_id, check_in, id""")

    data['attendance'] = rows(conn, """
        SELECT id, project_id, worker_id, stage_id, date, hours_worked,
               overtime_hours, total_wage
        FROM hdc_attendance ORDER BY id""")

    data['attendance_day'] = rows(conn, """
        SELECT id, worker_id, date, total_hours, day_value, overtime_hours,
               entry_count, is_void
        FROM hdc_attendance_day ORDER BY worker_id, date""")

    data['ledger'] = rows(conn, """
        SELECT id, worker_id, date, entry_type, amount, project_id, stage_id,
               time_entry_id, notes, is_void, void_reason, activity_at
        FROM hdc_labour_ledger ORDER BY worker_id, activity_at, id""")
    data['ledger_by_worker'] = defaultdict(list)
    for r in data['ledger']:
        data['ledger_by_worker'][r['worker_id']].append(r)

    data['expenses'] = rows(conn, """
        SELECT e.id, e.project_id, e.stage_id, e.category_id, e.amount, e.date,
               e.remarks, e.is_void,
               {tip} AS tip_worker_id,
               c.name AS category_name
        FROM hdc_expense e
        LEFT JOIN hdc_expense_category c ON c.id = e.category_id
        ORDER BY e.id""".format(
            tip='e.tip_worker_id' if data['has_tip_worker_id'] else 'NULL'))
    data['expense_by_id'] = {e['id']: e for e in data['expenses']}

    data['txns'] = rows(conn, """
        SELECT id, date, amount, type, category, source_type, source_id,
               reference_id, party_name, note, is_void
        FROM hdc_account_txn
        WHERE source_type LIKE 'labour_ledger%'
           OR source_type LIKE 'worker_payment%'
           OR reference_id LIKE 'labour_ledger#%'
        ORDER BY id""")
    data['ledger_by_id'] = {r['id']: r for r in data['ledger']}

    data['payroll_runs'] = rows(conn, """
        SELECT id, date_from, date_to, run_date, total_amount
        FROM hdc_payroll_run ORDER BY id""")
    data['payroll_items'] = rows(conn, """
        SELECT id, run_id, worker_id, total_hours, total_overtime, gross_amount,
               advance_deducted, net_pay
        FROM hdc_payroll_item ORDER BY run_id, id""")
    return data


def tip_worker_of(exp):
    if exp.get('tip_worker_id'):
        return int(exp['tip_worker_id'])
    m = TIP_WORKER_RE.search(exp.get('remarks') or '')
    return int(m.group(1)) if m else None


def tip_expense_ids(notes):
    return [int(m) for m in TIP_EXPENSE_RE.findall(notes or '')]


# --------------------------------------------------------------------------
# CHECK 1 -- wage calculation
# --------------------------------------------------------------------------
def check_wages(d):
    out = []
    per_sqft_zero = defaultdict(int)

    for te in d['time_entries']:
        if te['is_void']:
            continue
        w = d['worker_by_id'].get(te['worker_id'])
        if not w:
            out.append(finding(
                'WAGE_ORPHAN_WORKER', 'HIGH',
                'Time entry points at a missing worker',
                f"Time entry #{te['id']} has worker_id={te['worker_id']} which "
                f"does not exist in hdc_worker.",
                worker=f"worker#{te['worker_id']}",
                amount=te['wage_calculated']))
            continue

        work_date = day(te['check_in'])
        hours = f2(te['hours'])
        ot = f2(te['overtime'])
        regular = max(0.0, hours - ot)
        stored = f2(te['wage_calculated'])
        expected, wt, base = calc_time_wage(
            w, d['rates_by_worker'].get(w['id'], []),
            regular, ot, te['qty_sqft'], work_date)

        if not te['legacy_calc'] and abs(expected - stored) > MONEY_TOL:
            out.append(finding(
                'WAGE_MISMATCH', 'HIGH' if abs(expected - stored) > 5 else 'MEDIUM',
                'Stored wage does not match the wage formula',
                f"Time entry #{te['id']} on {work_date}: stored "
                f"{stored:,.2f} PKR but the rate card gives {expected:,.2f} PKR "
                f"({wt} @ {base:,.2f}, {regular:.2f}h regular + {ot:.2f}h OT). "
                f"Drift {stored - expected:+,.2f} PKR.",
                worker=d['wname'].get(w['id']),
                amount=stored - expected,
                refs=f"time_entry#{te['id']}"))

        if hours > HOUR_TOL and stored <= 0.005 and not te['legacy_calc']:
            out.append(finding(
                'WAGE_ZERO_WITH_HOURS', 'CRITICAL',
                'Worker worked hours but earned nothing',
                f"Time entry #{te['id']} on {work_date} records {hours:.2f}h "
                f"for a '{wt}' worker but wage_calculated = 0. "
                f"{'qty_sqft is 0, so rate x qty = 0' if wt == 'per_sqft' else f'rate is {base:,.2f}'}.",
                worker=d['wname'].get(w['id']),
                amount=expected,
                refs=f"time_entry#{te['id']}"))

        if wt == 'per_sqft' and f2(te['qty_sqft']) <= 0.0:
            per_sqft_zero[w['id']] += 1

    for wid, n in per_sqft_zero.items():
        out.append(finding(
            'WAGE_PERSQFT_NO_QTY', 'HIGH',
            'Per-sqft worker has entries with no quantity',
            f"{n} time entr(y/ies) for a per-sqft worker carry qty_sqft = 0, so "
            f"wage = rate x 0 = 0 and those days are unpaid. The quantity is "
            f"captured on the attendance screens now, but these historical rows "
            f"still need their quantity entered (edit the entry).",
            worker=d['wname'].get(wid), amount=0.0))
    return out


# --------------------------------------------------------------------------
# CHECK 2 -- worker balance: the two competing formulas
# --------------------------------------------------------------------------
def snapshot(ledger_rows, earned):
    """Mirror of ``_worker_payable_snapshot`` (tips are NOT deducted).

    ``earned`` is passed in because the service derives it from time entries
    (plus unmigrated legacy attendance), not from the ledger's ``work`` rows.
    """
    def total(etype, void=False):
        return sum(f2(r['amount']) for r in ledger_rows
                   if (r['entry_type'] or '') == etype and bool(r['is_void']) == void)
    advanced = total('advance')
    salary_paid = total('payment')
    tip = total('tip')
    settled = total('settlement')
    balance = earned - advanced - salary_paid - settled
    return {'earned': earned, 'advanced': advanced, 'salary_paid': salary_paid,
            'tip': tip, 'settled': settled, 'balance': balance,
            'payable': max(0.0, balance)}


def time_wage_total(d, wid):
    return sum(f2(t['wage_calculated']) for t in d['time_entries']
               if t['worker_id'] == wid and not t['is_void'])


def legacy_wage_total(d, wid):
    """Legacy ``hdc_attendance`` wages not covered by a time entry.

    Mirrors ``Worker.total_earned``: a legacy row counts as already migrated
    when an *active* time entry carries its id in ``attendance_id``.
    """
    migrated = {int(t['attendance_id']) for t in d['time_entries']
                if t['worker_id'] == wid and t.get('attendance_id') and not t['is_void']}
    return sum(f2(a['total_wage']) for a in d['attendance']
               if a['worker_id'] == wid and a['id'] not in migrated)


def model_earned_total(d, wid):
    """``Worker.total_earned``: time entries + unmigrated legacy attendance."""
    return time_wage_total(d, wid) + legacy_wage_total(d, wid)


def check_balances(d):
    out = []
    for w in d['workers']:
        wid = w['id']
        lr = d['ledger_by_worker'].get(wid, [])
        # Both readers must agree: the ledger/payment screens use
        # _worker_payable_snapshot and the Workers list / Advance screen use
        # Worker.balance_due. Tips are excluded from both and legacy attendance
        # wages are included in both (LABOUR_AUDIT #1).
        model_earned = model_earned_total(d, wid)
        snap = snapshot(lr, model_earned)
        tips = snap['tip']

        # --- the two balance formulas must produce the same number -----------
        # Mirrors Worker.balance_due after the fix: total_paid counts 'payment'
        # rows only, so tips never reduce the balance due.
        model_balance = (model_earned
                         - sum(f2(r['amount']) for r in lr
                               if r['entry_type'] == 'advance' and not r['is_void'])
                         - snap['salary_paid']
                         - snap['settled'])
        if abs(model_balance - snap['balance']) > MONEY_TOL:
            out.append(finding(
                'BALANCE_TWO_FORMULAS', 'HIGH',
                'Same worker shows two different "balance due" figures',
                f"Worker.balance_due (Workers list, Advance screen) gives "
                f"{model_balance:,.0f} PKR but _worker_payable_snapshot (worker "
                f"ledger, payment screen, accounts) gives "
                f"{snap['balance']:,.0f} PKR -- a {model_balance - snap['balance']:+,.0f} PKR "
                f"gap. Tips are gratis cash and must not be deducted from what "
                f"the worker is owed.",
                worker=d['wname'].get(wid),
                amount=model_balance - snap['balance']))

        if tips > MONEY_TOL:
            out.append(finding(
                'TIPS_INFORMATIONAL', 'INFO',
                'Tips paid to this worker (gratis cash, not deducted)',
                f"{tips:,.0f} PKR of tips recorded. These are shown for "
                f"information only and correctly do not reduce the balance due.",
                worker=d['wname'].get(wid), amount=tips))

        # --- earnings: time entries vs work ledger rows ----------------------
        work_rows = sum(f2(r['amount']) for r in lr
                        if (r['entry_type'] or '') == 'work' and not r['is_void'])
        tw = time_wage_total(d, wid)
        if abs(work_rows - tw) > MONEY_TOL:
            out.append(finding(
                'BALANCE_WORK_LEDGER_DRIFT', 'MEDIUM',
                'Work ledger total differs from time-entry wages',
                f"Sum of active 'work' ledger rows = {work_rows:,.2f} PKR but the "
                f"sum of active time entries = {tw:,.2f} PKR "
                f"(drift {work_rows - tw:+,.2f}). Orphan or duplicated work rows "
                f"inflate/deflate the ledger running balance even though the "
                f"payable snapshot does not use them.",
                worker=d['wname'].get(wid), amount=work_rows - tw))

        # --- over-payment -----------------------------------------------------
        if snap['balance'] < -MONEY_TOL:
            out.append(finding(
                'BALANCE_OVERPAID', 'HIGH',
                'Worker has been paid more than they earned',
                f"Earned {snap['earned']:,.0f} - advance {snap['advanced']:,.0f} "
                f"- paid {snap['salary_paid']:,.0f} - settled {snap['settled']:,.0f} "
                f"= {snap['balance']:,.0f} PKR. The payable screen clamps this to 0, "
                f"so the {abs(snap['balance']):,.0f} PKR over-payment is invisible "
                f"unless you read the ledger.",
                worker=d['wname'].get(wid), amount=snap['balance']))

        if snap['advanced'] - snap['earned'] > MONEY_TOL and snap['earned'] > 0:
            out.append(finding(
                'ADVANCE_EXCEEDS_EARNINGS', 'MEDIUM',
                'Advances exceed everything the worker ever earned',
                f"Advance {snap['advanced']:,.0f} PKR > earned "
                f"{snap['earned']:,.0f} PKR. Payroll clamps net pay at 0 for this "
                f"worker, so the excess advance never appears as a deduction on "
                f"any salary card -- it only survives in the all-time ledger.",
                worker=d['wname'].get(wid),
                amount=snap['advanced'] - snap['earned']))
    return out


# --------------------------------------------------------------------------
# CHECK 3 -- tips
# --------------------------------------------------------------------------
def check_tips(d):
    out = []
    tip_exps = [e for e in d['expenses']
                if (e.get('category_name') or '').strip().lower() == 'tip'
                and not e['is_void']]

    active_tip_rows = [r for r in d['ledger']
                       if (r['entry_type'] or '') == 'tip' and not r['is_void']]
    tagged = {}
    for r in active_tip_rows:
        for eid in tip_expense_ids(r['notes']):
            tagged.setdefault(eid, []).append(r)

    # C1 -- expense with no ledger row: the reconciler will create one later
    for e in tip_exps:
        wid = tip_worker_of(e)
        if not wid:
            out.append(finding(
                'TIP_NO_WORKER', 'MEDIUM',
                'Tip expense is not linked to any worker',
                f"Expense #{e['id']} ({f2(e['amount']):,.0f} PKR on {e['date']}) is "
                f"in the 'Tip' category but carries no tip_worker_id and no "
                f"TIP_WORKER_ID tag, so it never reaches a worker ledger and the "
                f"cash is invisible on the labour side.",
                amount=e['amount'], refs=f"expense#{e['id']}"))
            continue
        rows_for_exp = tagged.get(e['id'], [])
        if rows_for_exp:
            amt_ok = any(abs(f2(r['amount']) - f2(e['amount'])) <= MONEY_TOL
                         for r in rows_for_exp)
            if not amt_ok:
                out.append(finding(
                    'TIP_AMOUNT_MISMATCH', 'HIGH',
                    'Tip ledger amount differs from the tip expense',
                    f"Expense #{e['id']} is {f2(e['amount']):,.2f} PKR but the "
                    f"labour ledger row tagged TIP_EXPENSE_ID:{e['id']} is "
                    f"{f2(rows_for_exp[0]['amount']):,.2f} PKR "
                    f"(ledger #{rows_for_exp[0]['id']}).",
                    worker=d['wname'].get(wid),
                    amount=f2(rows_for_exp[0]['amount']) - f2(e['amount']),
                    refs=f"expense#{e['id']} labour_ledger#{rows_for_exp[0]['id']}"))
            continue
        # No TIP_EXPENSE_ID tag: does the date+amount fallback catch it?
        fb = [r for r in active_tip_rows
              if r['worker_id'] == wid and r['date'] == e['date']
              and abs(f2(r['amount']) - f2(e['amount'])) <= MONEY_TOL]
        if fb:
            out.append(finding(
                'TIP_UNTAGGED', 'MEDIUM',
                'Tip ledger row is not tagged with its expense id',
                f"Tip expense #{e['id']} matches labour ledger #{fb[0]['id']} only "
                f"by date+amount. The reconciler's primary key is TIP_EXPENSE_ID, "
                f"so this row survives purely on the legacy fallback. If the "
                f"amount or date is ever edited (the ledger edit form allows it) "
                f"the reconciler will write a second tip and the worker will be "
                f"over-paid by {f2(e['amount']):,.0f} PKR.",
                worker=d['wname'].get(wid), amount=e['amount'],
                refs=f"expense#{e['id']} labour_ledger#{fb[0]['id']}"))
        else:
            out.append(finding(
                'TIP_EXPENSE_WITHOUT_LEDGER', 'HIGH',
                'Tip expense has no worker ledger entry yet',
                f"Tip expense #{e['id']} ({f2(e['amount']):,.2f} PKR on {e['date']}) "
                f"has no active labour ledger row. The next time this worker's "
                f"ledger page is opened the reconciler will silently create one, "
                f"so the cash exists today as a project expense but not yet on "
                f"the worker's books.",
                worker=d['wname'].get(wid), amount=e['amount'],
                refs=f"expense#{e['id']}"))

    # C2 -- ledger tip with no expense
    for r in active_tip_rows:
        eids = tip_expense_ids(r['notes'])
        if not eids:
            wid = r['worker_id']
            fb = [e for e in tip_exps if tip_worker_of(e) == wid
                  and e['date'] == r['date']
                  and abs(f2(e['amount']) - f2(r['amount'])) <= MONEY_TOL]
            if not fb:
                out.append(finding(
                    'TIP_LEDGER_WITHOUT_EXPENSE', 'HIGH',
                    'Tip recorded on the worker but no tip expense exists',
                    f"Labour ledger #{r['id']} posts {f2(r['amount']):,.2f} PKR as a "
                    f"tip on {r['date']} but there is no matching 'Tip' expense, so "
                    f"the cash never reaches the project/stage cost or the unified "
                    f"accounts expense side.",
                    worker=d['wname'].get(wid), amount=r['amount'],
                    refs=f"labour_ledger#{r['id']}"))
            continue
        for eid in eids:
            e = d['expense_by_id'].get(eid)
            if not e:
                out.append(finding(
                    'TIP_LEDGER_ORPHAN_TAG', 'MEDIUM',
                    'Tip ledger row tags an expense that no longer exists',
                    f"Labour ledger #{r['id']} references TIP_EXPENSE_ID:{eid} but "
                    f"expense #{eid} is gone (hard-deleted). The reconciler will "
                    f"re-create a tip from nothing if the row is ever voided.",
                    worker=d['wname'].get(r['worker_id']), amount=r['amount'],
                    refs=f"labour_ledger#{r['id']}"))
            elif e['is_void']:
                out.append(finding(
                    'TIP_LEDGER_VOID_EXPENSE', 'HIGH',
                    'Tip expense was voided but the worker tip is still active',
                    f"Expense #{eid} is voided while labour ledger #{r['id']} "
                    f"({f2(r['amount']):,.2f} PKR) is still active. The worker keeps "
                    f"the tip but the company cash movement was cancelled.",
                    worker=d['wname'].get(r['worker_id']), amount=r['amount'],
                    refs=f"expense#{eid} labour_ledger#{r['id']}"))

    # C3 -- voided tip ledger whose expense is still alive -> tip resurrects
    for r in d['ledger']:
        if (r['entry_type'] or '') != 'tip' or not r['is_void']:
            continue
        for eid in tip_expense_ids(r['notes']):
            e = d['expense_by_id'].get(eid)
            if e and not e['is_void']:
                out.append(finding(
                    'TIP_RESURRECTS', 'CRITICAL',
                    'Voided tip will silently reappear',
                    f"Labour ledger #{r['id']} (tip {f2(r['amount']):,.2f} PKR) was "
                    f"voided, but its tip expense #{eid} is still active and nothing "
                    f"voids the expense when a tip ledger row is voided. The next "
                    f"visit to this worker's ledger re-creates the tip, so the void "
                    f"does not stick.",
                    worker=d['wname'].get(r['worker_id']), amount=r['amount'],
                    refs=f"labour_ledger#{r['id']} expense#{eid}"))
            break
        else:
            # untagged voided tip: fall back to the expense side
            fb = [e for e in tip_exps if tip_worker_of(e) == r['worker_id']
                  and e['date'] == r['date']
                  and abs(f2(e['amount']) - f2(r['amount'])) <= MONEY_TOL]
            if fb:
                out.append(finding(
                    'TIP_RESURRECTS', 'CRITICAL',
                    'Voided tip will silently reappear',
                    f"Labour ledger #{r['id']} (tip {f2(r['amount']):,.2f} PKR) was "
                    f"voided but tip expense #{fb[0]['id']} is still active and is "
                    f"not linked by TIP_EXPENSE_ID, so the reconciler will write a "
                    f"fresh tip ledger row for the same cash on the next page load.",
                    worker=d['wname'].get(r['worker_id']), amount=r['amount'],
                    refs=f"labour_ledger#{r['id']} expense#{fb[0]['id']}"))

    # C4 -- duplicate tips: same worker, date and amount, distinct rows
    buckets = defaultdict(list)
    for r in active_tip_rows:
        buckets[(r['worker_id'], r['date'], round(f2(r['amount']), 2))].append(r)
    for (wid, dt, amt), group in buckets.items():
        if len(group) < 2:
            continue
        ids = set()
        for r in group:
            ids.update(tip_expense_ids(r['notes']))
        if len(group) > max(1, len(ids)):
            out.append(finding(
                'TIP_DUPLICATE', 'CRITICAL',
                'Duplicate tip posted for the same cash event',
                f"{len(group)} active tip ledger rows of {amt:,.2f} PKR on {dt} for "
                f"the same worker (ids {', '.join(str(r['id']) for r in group)}) but "
                f"only {len(ids)} tip expense id(s). This is the over-payment bug the "
                f"TIP_EXPENSE_ID tagging was meant to prevent.",
                worker=d['wname'].get(wid), amount=amt * (len(group) - len(ids)),
                refs=' '.join(f"labour_ledger#{r['id']}" for r in group)))
    return out


# --------------------------------------------------------------------------
# CHECK 4 -- settlements (write-offs)
# --------------------------------------------------------------------------
def check_settlements(d):
    out = []
    settle_rows = [r for r in d['ledger']
                   if (r['entry_type'] or '') == 'settlement']
    active_settle = [r for r in settle_rows if not r['is_void']]
    settle_exps = [e for e in d['expenses']
                   if (e.get('category_name') or '').strip().lower() == 'settlement']

    for r in active_settle:
        match = [e for e in settle_exps
                 if not e['is_void']
                 and SETTLE_WORKER_RE.search(e.get('remarks') or '')
                 and int(SETTLE_WORKER_RE.search(e['remarks']).group(1)) == r['worker_id']
                 and abs(abs(f2(e['amount'])) - f2(r['amount'])) <= MONEY_TOL]
        if not match:
            out.append(finding(
                'SETTLEMENT_NO_EXPENSE', 'MEDIUM',
                'Settlement write-off has no matching expense',
                f"Labour ledger #{r['id']} writes off {f2(r['amount']):,.2f} PKR on "
                f"{r['date']} but no 'Settlement' expense (negative amount) was "
                f"booked, so the shortfall never reaches project/stage cost.",
                worker=d['wname'].get(r['worker_id']), amount=r['amount'],
                refs=f"labour_ledger#{r['id']}"))

    for r in settle_rows:
        if r['is_void']:
            live = [e for e in settle_exps
                    if not e['is_void'] and e['date'] == r['date']
                    and abs(abs(f2(e['amount'])) - f2(r['amount'])) <= MONEY_TOL
                    and SETTLE_WORKER_RE.search(e.get('remarks') or '')
                    and int(SETTLE_WORKER_RE.search(e['remarks']).group(1)) == r['worker_id']]
            if live:
                out.append(finding(
                    'SETTLEMENT_VOID_LEDGER_LIVE_EXPENSE', 'MEDIUM',
                    'Settlement voided on the ledger but the expense is live',
                    f"Labour ledger #{r['id']} is voided while settlement expense "
                    f"#{live[0]['id']} (-{abs(f2(live[0]['amount'])):,.2f} PKR) is "
                    f"still active, so project cost stays reduced while the worker "
                    f"owes the money again.",
                    worker=d['wname'].get(r['worker_id']), amount=r['amount'],
                    refs=f"labour_ledger#{r['id']} expense#{live[0]['id']}"))

    for e in settle_exps:
        if e['is_void']:
            continue
        m = SETTLE_WORKER_RE.search(e.get('remarks') or '')
        if not m:
            continue
        wid = int(m.group(1))
        if not any(r for r in active_settle if r['worker_id'] == wid
                   and abs(f2(r['amount']) - abs(f2(e['amount']))) <= MONEY_TOL
                   and r['date'] == e['date']):
            out.append(finding(
                'SETTLEMENT_EXPENSE_NO_LEDGER', 'MEDIUM',
                'Settlement expense has no worker ledger entry',
                f"Settlement expense #{e['id']} ({f2(e['amount']):,.2f} PKR on "
                f"{e['date']}) has no matching 'settlement' row in the worker's "
                f"ledger, so the worker's payable was not reduced even though the "
                f"company booked the write-off.",
                worker=d['wname'].get(wid), amount=abs(f2(e['amount'])),
                refs=f"expense#{e['id']}"))
    return out


# --------------------------------------------------------------------------
# CHECK 5 -- unified accounts mirror
# --------------------------------------------------------------------------
def txns_for(conn_txns, ledger_id):
    return [t for t in conn_txns
            if t['source_id'] == ledger_id
            or t['reference_id'] == f'labour_ledger#{ledger_id}']


def check_accounts(d):
    out = []
    txns = d['txns']
    for r in d['ledger']:
        et = (r['entry_type'] or '').strip().lower()
        if et not in ('advance', 'payment', 'tip'):
            continue
        if r['is_void']:
            live = [t for t in txns_for(txns, r['id']) if not t['is_void']]
            if live:
                out.append(finding(
                    'ACCOUNTS_VOID_NOT_PROPAGATED', 'HIGH',
                    'Voided ledger entry is still live in unified accounts',
                    f"Labour ledger #{r['id']} ({et} {f2(r['amount']):,.2f} PKR) is "
                    f"voided but account transaction(s) "
                    f"{', '.join('#' + str(t['id']) for t in live)} are still active, "
                    f"so the company cash is still shown as paid out.",
                    worker=d['wname'].get(r['worker_id']), amount=r['amount'],
                    refs=f"labour_ledger#{r['id']}"))
            continue
        live = [t for t in txns_for(txns, r['id']) if not t['is_void']]
        if not live:
            out.append(finding(
                'ACCOUNTS_MISSING', 'HIGH',
                'Labour cash entry is missing from unified accounts',
                f"Labour ledger #{r['id']} ({et} {f2(r['amount']):,.2f} PKR on "
                f"{r['date']}) has no active account transaction, so the money "
                f"left the worker ledger but never hit the company cash account.",
                worker=d['wname'].get(r['worker_id']), amount=r['amount'],
                refs=f"labour_ledger#{r['id']}"))
            continue
        total_live = sum(f2(t['amount']) for t in live)
        if len(live) > 1:
            out.append(finding(
                'ACCOUNTS_DOUBLE_POSTED', 'CRITICAL',
                'Labour entry posted to accounts more than once',
                f"Labour ledger #{r['id']} ({et} {f2(r['amount']):,.2f} PKR) has "
                f"{len(live)} active account transactions totalling "
                f"{total_live:,.2f} PKR -- the company cash is debited twice for "
                f"one payment.",
                worker=d['wname'].get(r['worker_id']),
                amount=total_live - f2(r['amount']),
                refs=f"labour_ledger#{r['id']}"))
        elif abs(total_live - f2(r['amount'])) > MONEY_TOL:
            out.append(finding(
                'ACCOUNTS_AMOUNT_MISMATCH', 'HIGH',
                'Accounts amount differs from the labour ledger amount',
                f"Labour ledger #{r['id']} is {f2(r['amount']):,.2f} PKR but account "
                f"transaction #{live[0]['id']} is {f2(live[0]['amount']):,.2f} PKR.",
                worker=d['wname'].get(r['worker_id']),
                amount=f2(live[0]['amount']) - f2(r['amount']),
                refs=f"labour_ledger#{r['id']} txn#{live[0]['id']}"))

    # orphan transactions: cash posted for a ledger row that no longer exists
    seen = set()
    for t in txns:
        if t['is_void']:
            continue
        lid = None
        if t['reference_id'] and t['reference_id'].startswith('labour_ledger#'):
            try:
                lid = int(t['reference_id'].split('#', 1)[1])
            except (IndexError, ValueError):
                lid = None
        if lid is None and (t['source_type'] or '').startswith('labour_ledger'):
            lid = t['source_id']
        if lid is None:
            continue
        seen.add(lid)
        row = d['ledger_by_id'].get(lid)
        if row is None:
            out.append(finding(
                'ACCOUNTS_ORPHAN_TXN', 'CRITICAL',
                'Cash was paid out but no worker ledger entry exists',
                f"Account transaction #{t['id']} ({f2(t['amount']):,.2f} PKR, "
                f"{t['type']}) references labour_ledger#{lid}, which no longer "
                f"exists. Deleting a payroll run hard-deletes the payment rows "
                f"without voiding their account transactions, which produces "
                f"exactly this: money still gone from the company, worker balance "
                f"restored.",
                amount=t['amount'], refs=f"txn#{t['id']}"))
        elif row['is_void']:
            out.append(finding(
                'ACCOUNTS_TXN_ON_VOIDED_ROW', 'HIGH',
                'Active account transaction on a voided ledger entry',
                f"Account transaction #{t['id']} ({f2(t['amount']):,.2f} PKR) is "
                f"active although labour ledger #{lid} is voided.",
                worker=d['wname'].get(row['worker_id']), amount=t['amount'],
                refs=f"txn#{t['id']} labour_ledger#{lid}"))

    # settlement rows must NOT have a cash transaction (they are write-offs)
    settle_with_txn = [r for r in d['ledger']
                       if (r['entry_type'] or '') == 'settlement'
                       and not r['is_void']
                       and any(not t['is_void'] for t in txns_for(txns, r['id']))]
    for r in settle_with_txn:
        out.append(finding(
            'SETTLEMENT_POSTED_AS_CASH', 'MEDIUM',
            'Settlement write-off was posted as real cash',
            f"Labour ledger #{r['id']} is a settlement (no cash should move) but it "
            f"has an active account transaction, so the write-off is being counted "
            f"as money leaving the company.",
            worker=d['wname'].get(r['worker_id']), amount=r['amount'],
            refs=f"labour_ledger#{r['id']}"))
    return out


# --------------------------------------------------------------------------
# CHECK 6 -- payroll
# --------------------------------------------------------------------------
def check_payroll(d):
    out = []
    runs = d['payroll_runs']

    for i, a in enumerate(runs):
        for b in runs[i + 1:]:
            if a['date_from'] and b['date_from'] and a['date_to'] and b['date_to']:
                if a['date_from'] <= b['date_to'] and b['date_from'] <= a['date_to']:
                    out.append(finding(
                        'PAYROLL_RUN_OVERLAP', 'HIGH',
                        'Two payroll runs cover overlapping dates',
                        f"Payroll run #{a['id']} ({a['date_from']} to {a['date_to']}) "
                        f"overlaps run #{b['id']} ({b['date_from']} to {b['date_to']}). "
                        f"Each run independently recomputes the full gross wage for "
                        f"the period and 'Pay All' pays against its own run note, so "
                        f"paying both runs pays the same days twice. Nothing in the "
                        f"generate flow blocks this.",
                        amount=0.0,
                        refs=f"payroll_run#{a['id']} payroll_run#{b['id']}"))

    items_by_run = defaultdict(list)
    for it in d['payroll_items']:
        items_by_run[it['run_id']].append(it)

    run_by_id = {r['id']: r for r in runs}
    for run_id, items in items_by_run.items():
        run = run_by_id.get(run_id)
        if not run:
            out.append(finding(
                'PAYROLL_ORPHAN_ITEM', 'LOW',
                'Payroll item belongs to a deleted run',
                f"{len(items)} payroll item(s) reference run #{run_id} which no "
                f"longer exists.", amount=0.0))
            continue
        for it in items:
            wid = it['worker_id']
            gross = f2(it['gross_amount'])
            adv = f2(it['advance_deducted'])
            net = f2(it['net_pay'])
            expected_net = max(0.0, gross - adv)
            if abs(expected_net - net) > MONEY_TOL:
                out.append(finding(
                    'PAYROLL_NET_MISMATCH', 'MEDIUM',
                    'Payroll net pay does not equal gross minus advances',
                    f"Payroll item #{it['id']} (run #{run_id}): gross "
                    f"{gross:,.2f} - advance {adv:,.2f} = {gross - adv:,.2f}, but "
                    f"net_pay is stored as {net:,.2f}.",
                    worker=d['wname'].get(wid), amount=net - expected_net,
                    refs=f"payroll_item#{it['id']}"))
            if gross - adv < -MONEY_TOL:
                out.append(finding(
                    'PAYROLL_ADVANCE_CLAMPED', 'MEDIUM',
                    'Payroll silently drops the part of the advance it cannot deduct',
                    f"Payroll item #{it['id']} (run #{run_id}): advances "
                    f"({adv:,.2f}) exceed this period's gross ({gross:,.2f}). Net pay "
                    f"is clamped to 0, so {adv - gross:,.2f} PKR of advance "
                    f"disappears from the salary card even though the worker ledger "
                    f"still carries it. Payroll figures therefore cannot be "
                    f"reconciled against the ledger.",
                    worker=d['wname'].get(wid), amount=adv - gross,
                    refs=f"payroll_item#{it['id']}"))

            # payments booked against this run
            key = f'Payroll run #{run_id} '
            paid = sum(f2(r['amount']) for r in d['ledger_by_worker'].get(wid, [])
                       if (r['entry_type'] or '') == 'payment' and not r['is_void']
                       and key in (r['notes'] or ''))
            if paid - net > MONEY_TOL:
                out.append(finding(
                    'PAYROLL_OVERPAID_RUN', 'HIGH',
                    'Payments booked against this payroll run exceed net pay',
                    f"Run #{run_id}: {paid:,.2f} PKR of payments carry the run note "
                    f"but net pay is only {net:,.2f} PKR "
                    f"(over by {paid - net:,.2f}).",
                    worker=d['wname'].get(wid), amount=paid - net,
                    refs=f"payroll_run#{run_id}"))

    # run totals vs item totals
    for run in runs:
        items = items_by_run.get(run['id'], [])
        s = sum(f2(i['net_pay']) for i in items)
        if abs(s - f2(run['total_amount'])) > MONEY_TOL:
            out.append(finding(
                'PAYROLL_TOTAL_MISMATCH', 'LOW',
                'Payroll run total does not equal the sum of its items',
                f"Run #{run['id']} stores total_amount "
                f"{f2(run['total_amount']):,.2f} but its items add up to "
                f"{s:,.2f} PKR.", amount=s - f2(run['total_amount']),
                refs=f"payroll_run#{run['id']}"))
    return out


# --------------------------------------------------------------------------
# CHECK 7 -- attendance / time-entry integrity
# --------------------------------------------------------------------------
def check_attendance(d):
    out = []
    te_by_att = defaultdict(list)
    for te in d['time_entries']:
        if te['attendance_id']:
            te_by_att[int(te['attendance_id'])].append(te)

    att_ids = {a['id']: a for a in d['attendance']}

    # legacy attendance rows whose wage is silently dropped
    for a in d['attendance']:
        linked = te_by_att.get(a['id'], [])
        if not linked:
            continue
        same = [te for te in linked
                if te['worker_id'] == a['worker_id']
                and day(te['check_in']) == str(a['date'])[:10]]
        if same and not any(not te['is_void'] for te in same):
            out.append(finding(
                'ATTENDANCE_WAGE_LOST_VOID', 'CRITICAL',
                'Legacy attendance wage is dropped by a voided time entry',
                f"Attendance #{a['id']} ({f2(a['total_wage']):,.2f} PKR on "
                f"{a['date']}) is excluded from Worker.total_earned because a time "
                f"entry references its id, but every such time entry is voided -- so "
                f"the wage is counted nowhere.",
                worker=d['wname'].get(a['worker_id']), amount=a['total_wage'],
                refs=f"attendance#{a['id']}"))
        elif not same:
            out.append(finding(
                'ATTENDANCE_ID_COLLISION', 'CRITICAL',
                'TimeEntry.attendance_id collides with a legacy attendance row',
                f"Attendance #{a['id']} (worker "
                f"{d['wname'].get(a['worker_id'], a['worker_id'])}, {a['date']}, "
                f"{f2(a['total_wage']):,.2f} PKR) is referenced by time entr(y/ies) "
                f"{', '.join('#' + str(t['id']) for t in linked)} belonging to a "
                f"different worker/date. attendance_id is meant to point only at "
                f"hdc_attendance; older builds also wrote AttendanceDay.id into "
                f"it, so Worker.total_earned and the project labour cost treat "
                f"this wage as already migrated and drop it. This is pre-existing "
                f"data damage -- clear attendance_id on the listed time entries "
                f"(the day link now lives in attendance_day_id).",
                worker=d['wname'].get(a['worker_id']), amount=a['total_wage'],
                refs=f"attendance#{a['id']}"))

    # voided time entry still carrying an active work ledger row
    work_by_te = defaultdict(list)
    for r in d['ledger']:
        if (r['entry_type'] or '') == 'work' and r['time_entry_id']:
            work_by_te[int(r['time_entry_id'])].append(r)
    for te in d['time_entries']:
        if not te['is_void']:
            continue
        live = [r for r in work_by_te.get(te['id'], []) if not r['is_void']]
        if live:
            out.append(finding(
                'TIMEENTRY_VOID_LIVE_WORK', 'HIGH',
                'Voided time entry still has an active work ledger row',
                f"Time entry #{te['id']} ({day(te['check_in'])}, "
                f"{f2(te['wage_calculated']):,.2f} PKR) is voided but labour ledger "
                f"row(s) {', '.join('#' + str(r['id']) for r in live)} are still "
                f"active, inflating the ledger running balance.",
                worker=d['wname'].get(te['worker_id']), amount=te['wage_calculated'],
                refs=f"time_entry#{te['id']}"))

    # duplicate active entries for the same worker/project/stage/day
    dup = defaultdict(list)
    for te in d['time_entries']:
        if te['is_void']:
            continue
        dup[(te['worker_id'], te['project_id'], te['stage_id'],
             day(te['check_in']))].append(te)
    for key, group in dup.items():
        if len(group) > 1:
            wid = key[0]
            hrs = sum(f2(t['hours']) for t in group)
            out.append(finding(
                'TIMEENTRY_DUPLICATE', 'MEDIUM',
                'Multiple active time entries for the same site/stage/day',
                f"{len(group)} active entries on {key[3]} for the same project/stage "
                f"(ids {', '.join(str(t['id']) for t in group)}), {hrs:.2f}h total. "
                f"These are only collapsed when the worker's ledger page is opened "
                f"(_reconcile_worker_time_entries runs on that page load).",
                worker=d['wname'].get(wid), amount=0.0,
                refs=' '.join(f"time_entry#{t['id']}" for t in group)))

    # day-level sanity
    day_totals = defaultdict(float)
    for te in d['time_entries']:
        if not te['is_void']:
            day_totals[(te['worker_id'], day(te['check_in']))] += f2(te['hours'])
    for (wid, dt), hrs in day_totals.items():
        if hrs > MAX_DAY_HOURS + HOUR_TOL:
            out.append(finding(
                'TIMEENTRY_OVER_24H', 'MEDIUM',
                'More than 24 hours booked in one day',
                f"{hrs:.2f}h recorded on {dt}. The entry forms reject >24h per "
                f"submission, but this can still accumulate across separate "
                f"entries/edits.", worker=d['wname'].get(wid), amount=0.0))

    # AttendanceDay aggregates vs the time entries they summarise
    for ad in d['attendance_day']:
        if ad['is_void']:
            continue
        grp = [t for t in d['time_entries']
               if t['worker_id'] == ad['worker_id']
               and day(t['check_in']) == str(ad['date'])[:10]
               and not t['is_void']]
        hrs = sum(f2(t['hours']) for t in grp)
        ot = sum(f2(t['overtime']) for t in grp)
        if abs(hrs - f2(ad['total_hours'])) > HOUR_TOL or abs(ot - f2(ad['overtime_hours'])) > HOUR_TOL:
            out.append(finding(
                'ATTENDANCE_DAY_DRIFT', 'LOW',
                'AttendanceDay totals do not match the time entries',
                f"AttendanceDay #{ad['id']} ({ad['date']}) says "
                f"{f2(ad['total_hours']):.2f}h / {f2(ad['overtime_hours']):.2f}h OT "
                f"but the active time entries add up to {hrs:.2f}h / {ot:.2f}h OT.",
                worker=d['wname'].get(ad['worker_id']),
                amount=hrs - f2(ad['total_hours']),
                refs=f"attendance_day#{ad['id']}"))

    # time entries whose attendance_id does not exist in either table
    ad_ids = {r['id'] for r in d['attendance_day']}
    for te in d['time_entries']:
        aid = te['attendance_id']
        if aid and aid not in att_ids and aid not in ad_ids:
            out.append(finding(
                'TIMEENTRY_DANGLING_ATTENDANCE_ID', 'LOW',
                'Time entry points at a missing attendance record',
                f"Time entry #{te['id']} has attendance_id={aid}, which exists in "
                f"neither hdc_attendance (legacy) nor hdc_attendance_day.",
                worker=d['wname'].get(te['worker_id']), amount=0.0,
                refs=f"time_entry#{te['id']}"))
    return out


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------
ALL_CHECKS = (
    check_wages,
    check_balances,
    check_tips,
    check_settlements,
    check_accounts,
    check_payroll,
    check_attendance,
)


def run_audit(db_path):
    conn = open_db(db_path)
    try:
        d = load(conn)
        findings = []
        for fn in ALL_CHECKS:
            try:
                findings.extend(fn(d))
            except Exception as exc:  # a bad row must not sink the whole audit
                findings.append(finding(
                    fn.__name__, 'MEDIUM', 'Check failed',
                    f'{fn.__name__} raised {type(exc).__name__}: {exc}'))
        return d, findings
    finally:
        conn.close()


def print_report(d, findings, top, show_info=True):
    sev_order = {s: i for i, s in enumerate(SEVERITIES)}
    findings = sorted(findings,
                      key=lambda f: (sev_order.get(f['severity'], 9),
                                     -abs(f['amount']), f['check']))
    if not show_info:
        findings = [f for f in findings if f['severity'] != 'INFO']

    counts = defaultdict(int)
    for f in findings:
        counts[f['severity']] += 1

    print('=' * 78)
    print('HDC labour audit -- wages, payments, tips, advances')
    print('=' * 78)
    print(f"workers: {len(d['workers'])}   time entries: {len(d['time_entries'])}   "
          f"ledger rows: {len(d['ledger'])}   expenses: {len(d['expenses'])}")
    print()
    if not findings:
        print('No inconsistencies found.')
        return findings

    print('Summary by severity:')
    for s in SEVERITIES:
        if counts.get(s):
            print(f'  {s:<9} {counts[s]:>4}')
    print()

    by_check = defaultdict(list)
    for f in findings:
        by_check[(f['severity'], f['check'])].append(f)
    for (sev, check) in sorted(by_check, key=lambda k: (sev_order.get(k[0], 9), k[1])):
        group = by_check[(sev, check)]
        print('-' * 78)
        print(f'[{sev}] {check}  ({len(group)} finding(s))')
        for f in group[:top]:
            who = f" {f['worker']}" if f['worker'] else ''
            amt = f" [{f['amount']:,.2f} PKR]" if f['amount'] else ''
            print(f'  -{who}{amt}')
            print(f'     {f["title"]}')
            print(f'     {f["detail"]}')
            if f['refs']:
                print(f'     refs: {f["refs"]}')
        if len(group) > top:
            print(f'  ... and {len(group) - top} more')
        print()
    return findings


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--db', default=os.environ.get('HDC_DB_PATH'),
                    help='path to the HDC SQLite database (read-only)')
    ap.add_argument('--json', dest='json_path', help='write findings as JSON')
    ap.add_argument('--csv', dest='csv_path', help='write findings as CSV')
    ap.add_argument('--top', type=int, default=10,
                    help='max examples printed per check (default 10)')
    ap.add_argument('--exit-zero', action='store_true',
                    help='always exit 0 (report-only mode)')
    args = ap.parse_args(argv)

    if not args.db:
        ap.error('--db is required (or set HDC_DB_PATH)')

    d, findings = run_audit(args.db)
    findings = print_report(d, findings, args.top)

    if args.json_path:
        with open(args.json_path, 'w', encoding='utf-8') as fh:
            json.dump({'database': os.path.abspath(args.db),
                       'findings': findings}, fh, indent=2)
        print(f'JSON written to {args.json_path}')
    if args.csv_path:
        with open(args.csv_path, 'w', encoding='utf-8', newline='') as fh:
            wr = csv.DictWriter(fh, fieldnames=['severity', 'check', 'worker',
                                                'amount', 'title', 'detail', 'refs'])
            wr.writeheader()
            for f in findings:
                wr.writerow(f)
        print(f'CSV written to {args.csv_path}')

    blocking = [f for f in findings if f['severity'] in ('CRITICAL', 'HIGH')]
    if blocking and not args.exit_zero:
        print(f'{len(blocking)} CRITICAL/HIGH finding(s): exiting 1')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
