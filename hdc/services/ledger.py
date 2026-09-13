"""HDC services.ledger — moved verbatim from hdc_erp.py.

See MODULARIZATION_PLAN.md for the module map.
"""

import calendar as pycal
import re
from datetime import date

from sqlalchemy import and_, func, or_
from sqlalchemy.exc import OperationalError

from hdc.extensions import db
from hdc.models.accounts import Expense, ExpenseCategory, PersonalExpense
from hdc.models.office import OfficeExpense, OfficeExpenseCategory, OfficeStaff, OfficeStaffAttendance, OfficeStaffLedger, StaffAllowance
from hdc.models.workforce import Attendance, LabourLedger, TimeEntry
from hdc.utils.dates import _pkt_now_naive, _pkt_today
from hdc.utils.format import _activity_at_for

def _is_linked_system_expense(exp):
    if not exp:
        return False
    if getattr(exp, 'tip_worker_id', None):
        return True
    remarks_u = ((getattr(exp, 'remarks', '') or '')).upper()
    return (
        'TIP_WORKER_ID:' in remarks_u
        or 'TIP_SUBCONTRACTOR_ID:' in remarks_u
        or 'SETTLE_WORKER_ID:' in remarks_u
        or 'SETTLE_SUBCONTRACTOR_ID:' in remarks_u
    )


def _office_expense_total():
    return float(
        db.session.query(func.coalesce(func.sum(OfficeExpense.amount), 0.0))
        .filter(OfficeExpense.is_void == False)
        .scalar() or 0.0
    )


def _personal_expense_total():
    return float(
        db.session.query(func.coalesce(func.sum(PersonalExpense.amount), 0.0))
        .filter(PersonalExpense.is_void == False)
        .scalar() or 0.0
    )


def _personal_expense_month():
    return float(
        db.session.query(func.coalesce(func.sum(PersonalExpense.amount), 0.0))
        .filter(
            PersonalExpense.is_void == False,
            func.strftime('%Y-%m', PersonalExpense.date) == _pkt_today().strftime('%Y-%m')
        )
        .scalar() or 0.0
    )


def _office_staff_ledger_snapshot(staff_id):
    today = _pkt_today()
    month_start = date(today.year, today.month, 1)
    month_days = float(pycal.monthrange(today.year, today.month)[1] or 30)
    month_end = date(today.year, today.month, int(month_days))

    staff_row = OfficeStaff.query.get(staff_id)
    monthly_basic_pay = float(getattr(staff_row, 'monthly_salary', 0.0) or 0.0)
    allowance_rows = (StaffAllowance.query
                      .filter(
                          StaffAllowance.staff_id == staff_id,
                          StaffAllowance.is_active == True
                      )
                      .all())
    monthly_allowances = float(sum(
        float(a.amount or 0.0)
        for a in allowance_rows
        if (not getattr(a, 'effective_date', None)) or (a.effective_date <= today)
    ))
    monthly_gross_pay = monthly_basic_pay + monthly_allowances
    per_day_basic = (monthly_basic_pay / month_days) if month_days > 0 else 0.0
    per_day_allowances = (monthly_allowances / month_days) if month_days > 0 else 0.0
    per_day_salary = per_day_basic + per_day_allowances

    units_map = {
        'present': 1.0,
        'absent': 0.0,
        'weekly_leave': 1.0,
        'leave': 1.0,
    }

    # Current month attendance for display purposes
    this_month_rows = (OfficeStaffAttendance.query
                       .filter(
                           OfficeStaffAttendance.staff_id == staff_id,
                           OfficeStaffAttendance.date >= month_start,
                           OfficeStaffAttendance.date <= month_end
                       )
                       .all())
    attendance_units = float(sum(units_map.get((r.status or '').strip().lower(), 0.0) for r in this_month_rows))
    earned_basic = attendance_units * per_day_basic
    earned_allowances = attendance_units * per_day_allowances
    earned = earned_basic + earned_allowances

    # All-time total earned (all attendance records, using current salary rate per day in each month)
    all_att_rows = (OfficeStaffAttendance.query
                    .filter(OfficeStaffAttendance.staff_id == staff_id)
                    .all())
    total_earned = 0.0
    for r in all_att_rows:
        units = units_map.get((r.status or '').strip().lower(), 0.0)
        if units > 0:
            row_month_days = float(pycal.monthrange(r.date.year, r.date.month)[1] or 30)
            allowance_for_row = float(sum(
                float(a.amount or 0.0)
                for a in allowance_rows
                if (not getattr(a, 'effective_date', None)) or (a.effective_date <= r.date)
            ))
            row_basic = (monthly_basic_pay / row_month_days)
            row_allowance = (allowance_for_row / row_month_days)
            total_earned += (row_basic + row_allowance) * units

    # All-time cumulative advances, payments, and settlements (not month-locked)
    advanced = float(db.session.query(func.coalesce(func.sum(OfficeStaffLedger.amount), 0.0))
                     .filter(
                         OfficeStaffLedger.staff_id == staff_id,
                         OfficeStaffLedger.entry_type == 'advance',
                         OfficeStaffLedger.is_void == False
                     )
                     .scalar() or 0.0)
    paid = float(db.session.query(func.coalesce(func.sum(OfficeStaffLedger.amount), 0.0))
                 .filter(
                     OfficeStaffLedger.staff_id == staff_id,
                     OfficeStaffLedger.entry_type == 'payment',
                     OfficeStaffLedger.is_void == False
                 )
                 .scalar() or 0.0)
    tip = float(db.session.query(func.coalesce(func.sum(OfficeStaffLedger.amount), 0.0))
                .filter(
                    OfficeStaffLedger.staff_id == staff_id,
                    OfficeStaffLedger.entry_type == 'tip',
                    OfficeStaffLedger.is_void == False
                )
                .scalar() or 0.0)
    settled = float(db.session.query(func.coalesce(func.sum(OfficeStaffLedger.amount), 0.0))
                    .filter(
                        OfficeStaffLedger.staff_id == staff_id,
                        OfficeStaffLedger.entry_type == 'settlement',
                        OfficeStaffLedger.is_void == False
                    )
                    .scalar() or 0.0)
    # Balance based on all-time earned vs all-time advances, payments, and write-offs.
    # Tip is gratis (above what was earned) and is tracked separately as a cash
    # expense — it does NOT reduce what the staff member is owed.
    balance = total_earned - advanced - paid - settled
    return {
        'month_start': month_start,
        'month_end': month_end,
        'month_days': month_days,
        'monthly_salary': monthly_basic_pay,
        'monthly_basic_pay': monthly_basic_pay,
        'monthly_allowances': monthly_allowances,
        'monthly_gross_pay': monthly_gross_pay,
        'per_day_salary': per_day_salary,
        'per_day_basic': per_day_basic,
        'per_day_allowances': per_day_allowances,
        'attendance_units': attendance_units,
        'earned': earned,
        'earned_basic': earned_basic,
        'earned_allowances': earned_allowances,
        'total_earned': total_earned,
        'advanced': advanced,
        'paid': paid,
        'tip': tip,
        'settled': settled,
        'balance': balance
    }


def _sync_office_staff_expense_from_ledger(staff_row, ledger_row):
    if not staff_row or not ledger_row:
        return
    entry_type = (ledger_row.entry_type or '').strip().lower()
    if entry_type not in ('advance', 'payment', 'tip'):
        return
    if entry_type == 'advance':
        category = 'Office Staff Advance'
    elif entry_type == 'tip':
        category = 'Office Staff Tip'
    else:
        category = 'Office Salary Payment'
    linked = (OfficeExpense.query
              .filter(OfficeExpense.office_staff_ledger_id == ledger_row.id)
              .first())
    notes = (ledger_row.notes or '').strip()
    remarks = f'{category} | {staff_row.name} ({staff_row.staff_code})'
    if notes:
        remarks = remarks + f' | {notes}'
    if linked:
        linked.office_staff_id = staff_row.id
        linked.date = ledger_row.date
        linked.category = category
        linked.amount = float(ledger_row.amount or 0.0)
        linked.remarks = remarks
        linked.is_void = False
        linked.void_reason = None
        linked.voided_at = None
        linked.activity_at = _activity_at_for(ledger_row.date)
        return linked
    linked = OfficeExpense(
        office_staff_id=staff_row.id,
        office_staff_ledger_id=ledger_row.id,
        date=ledger_row.date,
        category=category,
        amount=float(ledger_row.amount or 0.0),
        remarks=remarks,
        is_void=False,
        activity_at=_activity_at_for(ledger_row.date)
    )
    db.session.add(linked)
    return linked


def _remove_office_salary_expense_for_ledger(ledger_id):
    if not ledger_id:
        return
    rows = OfficeExpense.query.filter(
        OfficeExpense.office_staff_ledger_id == ledger_id,
        OfficeExpense.is_void == False
    ).all()
    for r in rows:
        r.is_void = True
        r.void_reason = 'Voided from office staff ledger'
        r.voided_at = _pkt_now_naive()


def _is_linked_office_salary_expense(exp):
    if not exp:
        return False
    return bool(getattr(exp, 'office_staff_ledger_id', None))


def _worker_legacy_earned(worker_id):
    """Wages on legacy ``hdc_attendance`` rows that were never migrated.

    ``Worker.total_earned`` counts these; the payable snapshot used to count
    only ``hdc_time_entry`` rows, so the Workers list and the ledger page could
    disagree about what a worker earned whenever the one-off migration had not
    covered every legacy row (LABOUR_AUDIT #1/#12).
    """
    migrated = {
        int(aid) for (aid,) in db.session.query(TimeEntry.attendance_id)
        .filter(TimeEntry.worker_id == worker_id,
                TimeEntry.is_void == False,
                TimeEntry.attendance_id.isnot(None))
        .distinct().all()
    }
    q = (db.session.query(func.coalesce(func.sum(Attendance.total_wage), 0.0))
         .filter(Attendance.worker_id == worker_id))
    if migrated:
        q = q.filter(Attendance.id.notin_(migrated))
    return float(q.scalar() or 0.0)


def _worker_payable_snapshot(worker_id):
    earned = float(db.session.query(func.coalesce(func.sum(TimeEntry.wage_calculated), 0.0))
                   .filter(TimeEntry.worker_id == worker_id, TimeEntry.is_void == False)
                   .scalar() or 0.0)
    earned += _worker_legacy_earned(worker_id)
    advanced = float(db.session.query(func.coalesce(func.sum(LabourLedger.amount), 0.0))
                     .filter(
                         LabourLedger.worker_id == worker_id,
                         LabourLedger.entry_type == 'advance',
                         LabourLedger.is_void == False
                     )
                     .scalar() or 0.0)
    salary_paid = float(db.session.query(func.coalesce(func.sum(LabourLedger.amount), 0.0))
                 .filter(
                     LabourLedger.worker_id == worker_id,
                     LabourLedger.entry_type == 'payment',
                     LabourLedger.is_void == False
                 )
                 .scalar() or 0.0)
    tip_paid = float(db.session.query(func.coalesce(func.sum(LabourLedger.amount), 0.0))
                .filter(
                    LabourLedger.worker_id == worker_id,
                    LabourLedger.entry_type == 'tip',
                    LabourLedger.is_void == False
                )
                .scalar() or 0.0)
    settled = float(db.session.query(func.coalesce(func.sum(LabourLedger.amount), 0.0))
               .filter(
                   LabourLedger.worker_id == worker_id,
                   LabourLedger.entry_type == 'settlement',
                   LabourLedger.is_void == False
               )
               .scalar() or 0.0)
    # Bug fix (2026-04-25): tip is gratis cash given on top of what the worker
    # earned, so it MUST NOT subtract from the worker's owed balance. The cash
    # outflow is still tracked (Expense + AccountTransaction); it just stays
    # informational in the worker ledger. Previously a worker with due 667
    # paid 700 (= 667 payment + 33 tip) ended up at balance -33 even though
    # the user's intent was "settle 667 and give 33 as a thank-you" -> 0.
    paid = salary_paid  # what the worker was paid against earnings
    balance = earned - advanced - salary_paid - settled
    payable = balance if balance > 0 else 0.0
    return {
        'earned': earned,
        'advanced': advanced,
        'paid': paid,
        'salary_paid': salary_paid,
        'tip_paid': tip_paid,
        'settled': settled,
        'balance': balance,
        'payable': payable
    }


def _worker_tip_expenses(worker):
    """Return tip expenses authoritatively belonging to a worker.

    Bug fix: Previously used ``LIKE '%TIP_WORKER_ID:N%'`` which matched any
    longer ID with ``N`` as a prefix (e.g. worker_id=1 matched
    ``TIP_WORKER_ID:10``). That caused tips for one worker to silently
    duplicate into other workers' ledgers. We now trust the ``tip_worker_id``
    foreign key as the single source of truth, and only fall back to
    well-delimited remark matching for legacy rows where the FK is NULL.
    """
    wid = int(worker.id)
    name_pat = f'%Tip for {worker.name}%'

    def _delim_patterns(tag):
        # Match the tag only when it ends the remarks or is followed by a
        # non-digit delimiter (space, pipe, comma, slash, hyphen, semicolon
        # or end-of-line). This prevents 'TIP_WORKER_ID:1' from matching
        # 'TIP_WORKER_ID:10', 'TIP_WORKER_ID:11', etc.
        return [
            Expense.remarks.ilike(f'%{tag}'),
            Expense.remarks.ilike(f'%{tag} %'),
            Expense.remarks.ilike(f'%{tag}|%'),
            Expense.remarks.ilike(f'%{tag},%'),
            Expense.remarks.ilike(f'%{tag};%'),
            Expense.remarks.ilike(f'%{tag}/%'),
            Expense.remarks.ilike(f'%{tag}-%'),
            Expense.remarks.ilike(f'%{tag}\n%'),
            Expense.remarks.ilike(f'%{tag}\r%'),
            Expense.remarks.ilike(f'%{tag}\t%'),
        ]

    tag = f'TIP_WORKER_ID:{wid}'
    legacy_match = or_(
        # Authoritative FK match
        Expense.tip_worker_id == wid,
        # Legacy fallback only when FK is missing
        and_(
            Expense.tip_worker_id.is_(None),
            or_(
                or_(*_delim_patterns(tag)),
                Expense.remarks.ilike(name_pat),
            )
        )
    )

    # A voided tip expense is a cancelled cash event and must never be
    # resurrected into a worker's ledger. Without this filter, voiding a tip
    # ledger row (which never voided the linked expense) made the reconciler
    # write a fresh tip row on the very next page load, so the void did not
    # stick (LABOUR_AUDIT #3).
    not_void = (Expense.is_void == False)

    try:
        return (Expense.query
                .join(ExpenseCategory, Expense.category_id == ExpenseCategory.id)
                .filter(
                    func.lower(ExpenseCategory.name) == 'tip',
                    not_void,
                    legacy_match
                )
                .order_by(Expense.activity_at.asc(), Expense.id.asc())
                .all())
    except OperationalError:
        db.session.rollback()
        # Schema lacks tip_worker_id column entirely (very old DBs). Fall back
        # to remark-only matching but still using delimited patterns so the
        # prefix-substring leak cannot recur.
        return (Expense.query
                .join(ExpenseCategory, Expense.category_id == ExpenseCategory.id)
                .filter(
                    func.lower(ExpenseCategory.name) == 'tip',
                    not_void,
                    or_(
                        or_(*_delim_patterns(tag)),
                        Expense.remarks.ilike(name_pat),
                    )
                )
                .order_by(Expense.activity_at.asc(), Expense.id.asc())
                .all())


_TIP_EXPENSE_ID_RE = re.compile(r'(?:^|[^\d])TIP_EXPENSE_ID:(\d+)(?!\d)')


def _linked_expense_for_labour_ledger(row):
    """Return the ``Expense`` that carries the same cash event as ``row``.

    Tips and shortfall settlements are mirrored into ``hdc_expense``: a tip as a
    positive 'Tip' expense, a settlement shortfall as a negative 'Settlement'
    one. Voiding or restoring the ledger row has to move the expense with it,
    otherwise the tip reconciler resurrects a voided tip (LABOUR_AUDIT #3) or a
    project stays written off for a shortfall the worker now owes again.
    """
    if not row:
        return None
    et = (row.entry_type or '').strip().lower()
    if et not in ('tip', 'settlement'):
        return None

    if et == 'tip':
        # Preferred link: the expense id stamped into the ledger row's notes.
        for eid in _TIP_EXPENSE_ID_RE.findall(row.notes or ''):
            exp = Expense.query.get(int(eid))
            if exp is not None:
                return exp

    cat = 'tip' if et == 'tip' else 'settlement'
    tag = ('TIP_WORKER_ID:' if et == 'tip' else 'SETTLE_WORKER_ID:') + str(int(row.worker_id))

    # Match the tag only when it ends the remarks or is followed by a non-digit
    # delimiter, so ``..._ID:1`` cannot match ``..._ID:10`` -- the same
    # prefix-substring leak that _worker_tip_expenses was hardened against.
    delim_clauses = [
        Expense.remarks.ilike(f'%{tag}'),
        Expense.remarks.ilike(f'%{tag} %'),
        Expense.remarks.ilike(f'%{tag}|%'),
        Expense.remarks.ilike(f'%{tag},%'),
        Expense.remarks.ilike(f'%{tag};%'),
        Expense.remarks.ilike(f'%{tag}/%'),
        Expense.remarks.ilike(f'%{tag}-%'),
    ]
    try:
        cands = (Expense.query
                 .join(ExpenseCategory, Expense.category_id == ExpenseCategory.id)
                 .filter(
                     func.lower(ExpenseCategory.name) == cat,
                     or_(
                         Expense.tip_worker_id == row.worker_id,
                         or_(*delim_clauses),
                     )
                 )
                 .order_by(Expense.id.asc())
                 .all())
    except OperationalError:
        db.session.rollback()
        cands = (Expense.query
                 .join(ExpenseCategory, Expense.category_id == ExpenseCategory.id)
                 .filter(func.lower(ExpenseCategory.name) == cat,
                         or_(*delim_clauses))
                 .order_by(Expense.id.asc())
                 .all())

    amount = float(row.amount or 0.0)
    for exp in cands:
        if exp.date == row.date and abs(abs(float(exp.amount or 0.0)) - amount) <= 0.01:
            return exp
    # No exact date/amount match: only trust a single unambiguous candidate.
    return cands[0] if len(cands) == 1 else None


def _get_all_office_expense_categories():
    from_expenses = {
        (r.category or '').strip()
        for r in OfficeExpense.query.filter(OfficeExpense.is_void == False).all()
        if (r.category or '').strip()
    }
    from_saved = {
        r.name.strip()
        for r in OfficeExpenseCategory.query.filter(OfficeExpenseCategory.active_status == True).all()
        if (r.name or '').strip()
    }
    return sorted(from_expenses | from_saved, key=lambda x: x.lower())


def _get_all_office_expense_category_rows():
    return [
        {
            'id': int(r.id),
            'name': (r.name or '').strip(),
            'active_status': bool(r.active_status)
        }
        for r in OfficeExpenseCategory.query.order_by(OfficeExpenseCategory.name.asc()).all()
    ]
