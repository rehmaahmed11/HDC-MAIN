#!/usr/bin/env python3
"""Build a throwaway HDC database seeded with known labour inconsistencies.

This exists so ``scripts/labour_audit.py`` can be validated end to end without
touching production data: every row written here reproduces a real failure
mode that the audit is supposed to catch.

Usage:
    python3 scripts/make_labour_audit_fixture.py --db /tmp/hdc_fixture.db
"""

from __future__ import annotations

import argparse
import os
import shutil
from datetime import date, datetime, timedelta

os.environ.setdefault('HDC_ENV', 'dev')
os.environ.setdefault('HDC_BOOTSTRAP_ADMIN_PASSWORD', 'FixturePass123!')

from hdc.app import create_app                       # noqa: E402
from hdc.extensions import db                        # noqa: E402
from hdc.models.accounts import Account, AccountTransaction, Expense, ExpenseCategory  # noqa: E402
from hdc.models.projects import Project, Stage       # noqa: E402
from hdc.models.workforce import (                   # noqa: E402
    Attendance, AttendanceDay, LabourLedger, PayrollItem, PayrollRun,
    TimeEntry, Worker, WorkerRate, WorkerTrade,
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', required=True, help='path of the fixture DB to create')
    args = ap.parse_args()

    db_path = os.path.abspath(args.db)
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    if os.path.exists(db_path):
        os.remove(db_path)

    app = create_app({'HDC_DB_PATH': db_path,
                      'HDC_INSTANCE_DIR': os.path.dirname(db_path)})
    with app.app_context():
        db.create_all()
        _seed()
        db.session.commit()
    print(f'fixture written to {db_path}')


def _seed():
    trade = WorkerTrade.query.filter_by(name='Mason').first()
    if not trade:
        trade = WorkerTrade(name='Mason', active_status=True)
        db.session.add(trade)
        db.session.flush()
    proj = Project(name='Model Town', project_code='MT-01', client='Owner A',
                   location='Lahore', contract_type='lump_sum', owner_lump_sum=5_000_000)
    db.session.add(proj)
    db.session.flush()
    st = Stage(project_id=proj.id, name='Foundation', contract_basis='Lump Sum',
               lump_sum_value=1_000_000)
    db.session.add(st)

    tip_cat = ExpenseCategory(name='Tip', active_status=True)
    settle_cat = ExpenseCategory(name='Settlement', active_status=True)
    db.session.add_all([tip_cat, settle_cat])

    cash = (Account.query.filter(Account.type == 'company', Account.is_void == False)
            .order_by(Account.id).first())
    ext = (Account.query.filter(Account.is_void == False, Account.id != cash.id)
           .order_by(Account.id).first())
    if not cash:
        cash = Account(name='Company Cash', type='company', opening_balance=1_000_000)
        db.session.add(cash)
        db.session.flush()
    if not ext:
        ext = Account(name='External Parties', type='person', opening_balance=0)
        db.session.add(ext)
        db.session.flush()

    w_daily = Worker(worker_code='W-001', name='Ali Raza', role_type='Mason',
                     wage_type='daily', base_daily_wage=1000.0, active_status=True)
    w_sqft = Worker(worker_code='W-002', name='Bilal Khan', role_type='Mason',
                    wage_type='per_sqft', rate_per_sqft=25.0, active_status=True)
    w_hour = Worker(worker_code='W-003', name='Cash Hourly', role_type='Mason',
                    wage_type='hourly', hourly_rate=125.0, active_status=True)
    db.session.add_all([w_daily, w_sqft, w_hour])
    db.session.flush()

    d0 = date(2026, 8, 1)

    def te(worker, day_offset, hours, ot=0.0, wage=None, qty=0.0, legacy=False,
           att_id=None, void=False, start_hour=9):
        ci = (datetime.combine(d0 + timedelta(days=day_offset), datetime.min.time())
              + timedelta(hours=start_hour))
        e = TimeEntry(worker_id=worker.id, project_id=proj.id, stage_id=st.id,
                      check_in=ci, check_out=ci + timedelta(hours=hours),
                      hours=hours, overtime=ot, qty_sqft=qty,
                      wage_calculated=wage if wage is not None else 0.0,
                      legacy_calc=legacy, attendance_id=att_id, is_void=void,
                      void_reason='fixture' if void else None,
                      activity_at=ci)
        db.session.add(e)
        db.session.flush()
        return e

    # --- Ali: 10 days, but one day stored with a stale (pre-raise) wage -------
    db.session.add(WorkerRate(worker_id=w_daily.id, wage_type='daily', rate=1000.0,
                              effective_from=d0, reason='baseline'))
    db.session.add(WorkerRate(worker_id=w_daily.id, wage_type='daily', rate=1200.0,
                              effective_from=d0 + timedelta(days=5), reason='raise'))
    te(w_daily, 0, 8.0, wage=1000.0)
    te(w_daily, 1, 8.0, wage=1000.0)
    te(w_daily, 6, 8.0, wage=1000.0)          # after the raise: should be 1200
    te(w_daily, 7, 10.0, ot=2.0, wage=1250.0)  # 8h @1000 pro-rata + 2h OT

    # --- Bilal: per-sqft worker, qty never captured -> wage silently zero -----
    te(w_sqft, 0, 8.0, wage=0.0)
    te(w_sqft, 1, 8.0, wage=0.0)

    # --- Hourly worker with unpaid overtime ----------------------------------
    te(w_hour, 0, 10.0, ot=2.0, wage=1250.0)

    # --- duplicate entry on the same site/stage/day (unreconciled) -----------
    te(w_daily, 2, 8.0, wage=1000.0)
    te(w_daily, 2, 8.0, wage=1000.0, start_hour=14)

    # --- voided entry that still carries an active work ledger row -----------
    voided = te(w_daily, 3, 8.0, wage=1000.0, void=True)
    db.session.add(LabourLedger(worker_id=w_daily.id, entry_type='work',
                                amount=1000.0, date=voided.check_in.date(),
                                time_entry_id=voided.id,
                                project_id=proj.id, stage_id=st.id,
                                notes='stale work row', is_void=False,
                                activity_at=voided.check_in))

    # --- attendance_id collision (AttendanceDay id reused as Attendance id) ---
    legacy_att = Attendance(project_id=proj.id, worker_id=w_daily.id, stage_id=st.id,
                            date=d0, hours_worked=8.0, overtime_hours=0.0,
                            total_wage=900.0)
    db.session.add(legacy_att)
    db.session.flush()
    collide = te(w_hour, 4, 4.0, wage=500.0, att_id=legacy_att.id)
    db.session.add(AttendanceDay(id=legacy_att.id, worker_id=w_hour.id,
                                 date=d0 + timedelta(days=4), total_hours=4.0,
                                 day_value=0.0, overtime_hours=0.0,
                                 entry_count=1, is_void=False))

    def ledger(worker, etype, amount, day_offset, notes='', void=False, proj_id=None):
        row = LabourLedger(worker_id=worker.id, entry_type=etype, amount=amount,
                           date=d0 + timedelta(days=day_offset), notes=notes,
                           project_id=proj_id, stage_id=(st.id if proj_id else None),
                           is_void=void,
                           activity_at=datetime.combine(d0 + timedelta(days=day_offset),
                                                        datetime.min.time()))
        db.session.add(row)
        db.session.flush()
        return row

    def txn(row, amount=None, void=False):
        t = AccountTransaction(
            date=row.date, amount=(amount if amount is not None else float(row.amount)),
            type=('advance_to_person' if row.entry_type == 'advance' else 'payroll'),
            from_account_id=cash.id, to_account_id=ext.id,
            executed_by_account_id=cash.id,
            related_entity_type='worker', related_entity_id=row.worker_id,
            party_name='fixture', category=('advance' if row.entry_type == 'advance' else 'payroll'),
            note=row.notes or '', reference_id=f'labour_ledger#{row.id}',
            source_type=f'labour_ledger_{row.entry_type}', source_id=row.id,
            is_void=void)
        db.session.add(t)
        return t

    # --- advances, payments ---------------------------------------------------
    adv = ledger(w_daily, 'advance', 500.0, 4, notes='advance')
    txn(adv)
    pay = ledger(w_daily, 'payment', 2000.0, 8, notes='salary')
    txn(pay)
    orphan_adv = ledger(w_sqft, 'advance', 300.0, 1, notes='no accounts posting')
    # (deliberately no account transaction -> ACCOUNTS_MISSING)

    # --- tip posted with the amount mismatch between expense and ledger -------
    tip_exp = Expense(project_id=proj.id, stage_id=st.id, tip_worker_id=w_daily.id,
                      category_id=tip_cat.id, amount=200.0, date=d0 + timedelta(days=8),
                      remarks=f'Tip for Ali Raza | TIP_WORKER_ID:{w_daily.id}',
                      activity_at=datetime.combine(d0 + timedelta(days=8), datetime.min.time()))
    db.session.add(tip_exp)
    db.session.flush()
    tip_row = ledger(w_daily, 'tip', 200.0, 8,
                     notes=f'Tip for Ali Raza | TIP_WORKER_ID:{w_daily.id}')
    txn(tip_row)
    # a second, untagged tip on the same date/amount -> duplicate risk
    tip_row2 = ledger(w_daily, 'tip', 200.0, 8, notes='Tip for Ali Raza')

    # --- tip expense with no ledger row at all --------------------------------
    db.session.add(Expense(project_id=proj.id, stage_id=st.id, tip_worker_id=w_hour.id,
                           category_id=tip_cat.id, amount=150.0,
                           date=d0 + timedelta(days=5),
                           remarks=f'Tip for Cash Hourly | TIP_WORKER_ID:{w_hour.id}',
                           activity_at=datetime.combine(d0 + timedelta(days=5), datetime.min.time())))

    # --- voided tip whose expense is still alive -> tip resurrects ------------
    tip_exp2 = Expense(project_id=proj.id, stage_id=st.id, tip_worker_id=w_sqft.id,
                       category_id=tip_cat.id, amount=100.0,
                       date=d0 + timedelta(days=6),
                       remarks=f'Tip for Bilal Khan | TIP_WORKER_ID:{w_sqft.id}',
                       activity_at=datetime.combine(d0 + timedelta(days=6), datetime.min.time()))
    db.session.add(tip_exp2)
    db.session.flush()
    void_tip = ledger(w_sqft, 'tip', 100.0, 6,
                      notes=f'Tip for Bilal Khan | TIP_EXPENSE_ID:{tip_exp2.id}', void=True)
    txn(void_tip, void=True)

    # --- settlement with no matching expense ----------------------------------
    ledger(w_daily, 'settlement', 250.0, 9, notes='settlement shortfall', proj_id=proj.id)

    # --- orphan account transaction (as produced by deleting a payroll run) ---
    db.session.add(AccountTransaction(
        date=d0 + timedelta(days=10), amount=1500.0, type='payroll',
        from_account_id=cash.id, to_account_id=ext.id, executed_by_account_id=cash.id,
        related_entity_type='worker', related_entity_id=w_daily.id,
        party_name='Ali Raza', category='payroll', note='Payroll run #1 bulk payment',
        reference_id='labour_ledger#9999', source_type='labour_ledger_payment',
        source_id=9999, is_void=False))

    # --- payroll: two overlapping runs, clamped advance, overpaid run ---------
    run1 = PayrollRun(date_from=d0, date_to=d0 + timedelta(days=9), run_date=d0)
    run2 = PayrollRun(date_from=d0 + timedelta(days=5), date_to=d0 + timedelta(days=14),
                      run_date=d0)
    db.session.add_all([run1, run2])
    db.session.flush()
    db.session.add(PayrollItem(run_id=run1.id, worker_id=w_daily.id, total_hours=42.0,
                               total_overtime=2.0, gross_amount=4250.0,
                               advance_deducted=500.0, net_pay=3750.0))
    db.session.add(PayrollItem(run_id=run1.id, worker_id=w_sqft.id, total_hours=16.0,
                               total_overtime=0.0, gross_amount=0.0,
                               advance_deducted=300.0, net_pay=0.0))
    db.session.add(PayrollItem(run_id=run2.id, worker_id=w_daily.id, total_hours=26.0,
                               total_overtime=2.0, gross_amount=2250.0,
                               advance_deducted=0.0, net_pay=2250.0))
    ledger(w_daily, 'payment', 4000.0, 10, notes='Payroll run #1 bulk payment')


if __name__ == '__main__':
    main()
