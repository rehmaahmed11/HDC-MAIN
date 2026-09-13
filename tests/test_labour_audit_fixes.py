#!/usr/bin/env python3
"""Regression tests for the labour wage / payment / tip / advance audit fixes.

Each test pins one finding from ``LABOUR_AUDIT.md`` so it cannot silently come
back. They run against a real (temporary) Flask app and database, exercising the
actual routes rather than mocks.

Run with:
    HDC_BOOTSTRAP_ADMIN_PASSWORD='Admin@1234' \
        python -m unittest tests.test_labour_audit_fixes -v
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

os.environ.setdefault('HDC_ENV', 'test')
os.environ.setdefault('HDC_SECRET_KEY', 'unit-test-secret')
os.environ.setdefault('HDC_BOOTSTRAP_ADMIN_PASSWORD', 'Admin@1234')

from sqlalchemy import func                                      # noqa: E402

from hdc.app import create_app                                   # noqa: E402
from hdc.extensions import db                                    # noqa: E402
from hdc.models.accounts import AccountTransaction, Expense      # noqa: E402
from hdc.models.projects import Project, Stage                   # noqa: E402
from hdc.models.workforce import (                               # noqa: E402
    Attendance, AttendanceDay, LabourLedger, PayrollRun, TimeEntry,
    Worker, WorkerTrade,
)
from hdc.services.ledger import (                                # noqa: E402
    _linked_expense_for_labour_ledger, _worker_payable_snapshot,
)
from hdc.services.timekeeping import (                           # noqa: E402
    _calc_time_wage, _recalculate_attendance_day,
    _reconcile_worker_tip_ledger,
)

ADMIN_PASSWORD = os.environ['HDC_BOOTSTRAP_ADMIN_PASSWORD']
D0 = date(2026, 8, 1)


class LabourTestCase(unittest.TestCase):
    """One isolated app + database per test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='hdc-labour-test-')
        self.db_path = os.path.join(self.tmp, 'test.db')
        self.app = create_app({'HDC_DB_PATH': self.db_path,
                               'HDC_INSTANCE_DIR': self.tmp})
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()
        self._login()
        self._fund_treasury()
        self.project, self.stage = self._make_project()

    def _fund_treasury(self, amount=10_000_000.0):
        """Give the company cash accounts an opening balance.

        The unified-accounts overdraft guard refuses any payout from a
        company/cash/bank account that would go negative, so an unfunded test
        database blocks every worker payment before the labour logic runs.
        """
        from hdc.models.accounts import Account
        for acc in Account.query.filter(
                Account.is_void == False,
                func.lower(Account.type).in_(('company', 'cash', 'bank'))).all():
            acc.opening_balance = amount
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------------------------------------------------------------- helpers
    def _login(self):
        r = self.client.get('/hdc/login')
        token = self._csrf(r.get_data(as_text=True))
        self.client.post('/hdc/login', data={
            'username': 'admin', 'password': ADMIN_PASSWORD, '_csrf_token': token,
        }, follow_redirects=True)

    @staticmethod
    def _csrf(html):
        m = re.search(r'name="_csrf_token" value="([^"]+)"', html or '')
        return m.group(1) if m else ''

    def _token(self):
        with self.client.session_transaction() as sess:
            return sess.get('_csrf_token', '')

    def _make_project(self):
        if not WorkerTrade.query.filter_by(name='Mason').first():
            db.session.add(WorkerTrade(name='Mason', active_status=True))
        p = Project(name='Test Site', project_code='T-01', client='Owner',
                    location='Nowhere', contract_type='lump_sum',
                    owner_lump_sum=1_000_000)
        db.session.add(p)
        db.session.flush()
        s = Stage(project_id=p.id, name='Foundation', contract_basis='Lump Sum',
                  lump_sum_value=100_000)
        db.session.add(s)
        db.session.commit()
        return p, s

    def _make_worker(self, code='W-001', name='Ali Raza', wage_type='daily',
                     daily=1000.0, hourly=125.0, sqft=25.0):
        w = Worker(worker_code=code, name=name, role_type='Mason',
                   wage_type=wage_type, base_daily_wage=daily,
                   hourly_rate=hourly, rate_per_sqft=sqft, active_status=True)
        db.session.add(w)
        db.session.commit()
        return w

    def _time_entry(self, w, day_offset=0, hours=8.0, ot=0.0, wage=1000.0,
                    qty=0.0, start_hour=9, legacy=False, att_id=None, void=False):
        ci = (datetime.combine(D0 + timedelta(days=day_offset), datetime.min.time())
              + timedelta(hours=start_hour))
        te = TimeEntry(worker_id=w.id, project_id=self.project.id,
                       stage_id=self.stage.id, check_in=ci,
                       check_out=ci + timedelta(hours=hours), hours=hours,
                       overtime=ot, qty_sqft=qty, wage_calculated=wage,
                       legacy_calc=legacy, attendance_id=att_id, is_void=void,
                       activity_at=ci)
        db.session.add(te)
        db.session.commit()
        return te

    def _ledger(self, w, etype, amount, day_offset=0, notes='', void=False,
                project=None):
        row = LabourLedger(worker_id=w.id, entry_type=etype, amount=amount,
                           date=D0 + timedelta(days=day_offset), notes=notes,
                           project_id=(self.project.id if project else None),
                           stage_id=(self.stage.id if project else None),
                           is_void=void,
                           activity_at=datetime.combine(
                               D0 + timedelta(days=day_offset), datetime.min.time()))
        db.session.add(row)
        db.session.commit()
        return row


# --------------------------------------------------------------------------
# LABOUR_AUDIT #1 -- one balance formula, tips never deducted
# --------------------------------------------------------------------------
class TestBalanceFormula(LabourTestCase):
    def test_tips_do_not_reduce_balance_due(self):
        w = self._make_worker()
        self._time_entry(w, 0, wage=1000.0)
        self._ledger(w, 'payment', 400.0, 1)
        self._ledger(w, 'tip', 100.0, 1)

        # 1000 earned - 400 paid = 600 owed; the 100 tip is gratis.
        self.assertAlmostEqual(w.balance_due, 600.0, places=2)
        self.assertAlmostEqual(w.total_tips, 100.0, places=2)
        self.assertAlmostEqual(w.total_paid, 400.0, places=2)

    def test_model_and_snapshot_agree(self):
        """Workers list and the ledger page must show the same number."""
        w = self._make_worker()
        self._time_entry(w, 0, wage=1000.0)
        self._ledger(w, 'advance', 200.0, 1)
        self._ledger(w, 'payment', 300.0, 2)
        self._ledger(w, 'tip', 50.0, 2)

        snap = _worker_payable_snapshot(w.id)
        self.assertAlmostEqual(w.balance_due, snap['balance'], places=2)
        self.assertAlmostEqual(snap['balance'], 500.0, places=2)

    def test_workers_page_shows_tips_as_informational(self):
        w = self._make_worker()
        self._time_entry(w, 0, wage=1000.0)
        self._ledger(w, 'tip', 100.0, 1)
        html = self.client.get('/hdc/workers').get_data(as_text=True)
        self.assertIn('Tips: 100', html)
        self.assertIn('1,000 PKR', html)   # balance due, tip not subtracted


# --------------------------------------------------------------------------
# LABOUR_AUDIT #1/#12 -- legacy attendance wages counted identically
# --------------------------------------------------------------------------
class TestLegacyEarnings(LabourTestCase):
    def test_snapshot_includes_unmigrated_legacy_attendance(self):
        w = self._make_worker()
        db.session.add(Attendance(project_id=self.project.id, worker_id=w.id,
                                  stage_id=self.stage.id, date=D0,
                                  hours_worked=8.0, overtime_hours=0.0,
                                  total_wage=900.0))
        db.session.commit()

        snap = _worker_payable_snapshot(w.id)
        self.assertAlmostEqual(snap['earned'], 900.0, places=2)
        self.assertAlmostEqual(w.total_earned, snap['earned'], places=2)

    def test_voided_migration_row_does_not_erase_legacy_wage(self):
        w = self._make_worker()
        att = Attendance(project_id=self.project.id, worker_id=w.id,
                         stage_id=self.stage.id, date=D0, hours_worked=8.0,
                         overtime_hours=0.0, total_wage=900.0)
        db.session.add(att)
        db.session.commit()
        # A voided migration row must not claim the legacy wage as migrated.
        self._time_entry(w, 0, wage=0.0, legacy=True, att_id=att.id, void=True)

        self.assertAlmostEqual(w.total_earned, 900.0, places=2)
        self.assertAlmostEqual(_worker_payable_snapshot(w.id)['earned'], 900.0,
                               places=2)


# --------------------------------------------------------------------------
# LABOUR_AUDIT #6 -- attendance_day_id is its own column
# --------------------------------------------------------------------------
class TestAttendanceDayLink(LabourTestCase):
    def test_recalculate_uses_dedicated_column(self):
        w = self._make_worker()
        te = self._time_entry(w, 0, hours=8.0, wage=1000.0)
        _recalculate_attendance_day(w.id, D0)
        db.session.commit()

        day = AttendanceDay.query.filter_by(worker_id=w.id, date=D0).first()
        self.assertIsNotNone(day)
        self.assertEqual(te.attendance_day_id, day.id)
        self.assertIsNone(te.attendance_id)   # legacy column untouched

    def test_legacy_wage_survives_day_recalculation(self):
        w = self._make_worker()
        att = Attendance(project_id=self.project.id, worker_id=w.id,
                         stage_id=self.stage.id, date=D0, hours_worked=8.0,
                         overtime_hours=0.0, total_wage=900.0)
        db.session.add(att)
        db.session.commit()
        self._time_entry(w, 1, hours=8.0, wage=1000.0)
        _recalculate_attendance_day(w.id, D0 + timedelta(days=1))
        db.session.commit()

        # 900 legacy + 1000 time entry -- nothing dropped by an id collision.
        self.assertAlmostEqual(w.total_earned, 1900.0, places=2)
        self.assertAlmostEqual(self.project.total_labour_cost, 1900.0, places=2)


# --------------------------------------------------------------------------
# LABOUR_AUDIT #9 -- overtime is paid for hourly workers too
# --------------------------------------------------------------------------
class TestWageEngine(LabourTestCase):
    def test_hourly_worker_is_paid_for_overtime(self):
        w = self._make_worker(wage_type='hourly', hourly=125.0)
        self.assertAlmostEqual(_calc_time_wage(w, 8.0, 2.0, 0.0, work_date=D0),
                               1250.0, places=2)   # 10h x 125, straight time

    def test_hourly_worker_without_overtime_unchanged(self):
        w = self._make_worker(wage_type='hourly', hourly=125.0)
        self.assertAlmostEqual(_calc_time_wage(w, 8.0, 0.0, 0.0, work_date=D0),
                               1000.0, places=2)

    def test_daily_worker_overtime_is_straight_time(self):
        w = self._make_worker(wage_type='daily', daily=1000.0)
        # full day 1000 + 2h OT at 1000/8 = 250
        self.assertAlmostEqual(_calc_time_wage(w, 8.0, 2.0, 0.0, work_date=D0),
                               1250.0, places=2)

    def test_per_sqft_worker_is_paid_rate_times_quantity(self):
        w = self._make_worker(wage_type='per_sqft', sqft=25.0)
        self.assertAlmostEqual(_calc_time_wage(w, 8.0, 0.0, 100.0, work_date=D0),
                               2500.0, places=2)


# --------------------------------------------------------------------------
# LABOUR_AUDIT #5 -- per-sqft quantity reaches the wage
# --------------------------------------------------------------------------
class TestPerSqftQuantity(LabourTestCase):
    def _bulk_post(self, w, payload_extra):
        data = {'bulk_mode': '1', 'date': D0.isoformat(),
                f'status_{w.id}': 'present', '_csrf_token': self._token()}
        data.update(payload_extra)
        return self.client.post('/hdc/attendance', data=data,
                                follow_redirects=True)

    def test_bulk_sheet_records_quantity_and_pays_it(self):
        w = self._make_worker(wage_type='per_sqft', sqft=25.0)
        import json
        alloc = json.dumps([{'project_id': self.project.id,
                             'stage_id': self.stage.id,
                             'hours': 8.0, 'qty_sqft': 100.0}])
        self._bulk_post(w, {f'allocations_{w.id}': alloc})

        te = TimeEntry.query.filter_by(worker_id=w.id, is_void=False).first()
        self.assertIsNotNone(te, 'bulk sheet should have created a time entry')
        self.assertAlmostEqual(te.qty_sqft, 100.0, places=2)
        self.assertAlmostEqual(te.wage_calculated, 2500.0, places=2)

    def test_edit_form_preserves_quantity(self):
        w = self._make_worker(wage_type='per_sqft', sqft=25.0)
        te = self._time_entry(w, 0, hours=8.0, wage=2500.0, qty=100.0)
        self.client.post(f'/hdc/timekeeping/{te.id}/edit', data={
            'project_id': self.project.id, 'stage_id': self.stage.id,
            'hours': 8.0, '_csrf_token': self._token(),
        }, follow_redirects=True)
        db.session.refresh(te)
        # Editing hours used to wipe qty_sqft to 0 and zero the wage.
        self.assertAlmostEqual(te.qty_sqft, 100.0, places=2)
        self.assertAlmostEqual(te.wage_calculated, 2500.0, places=2)

    def test_edit_form_can_update_quantity(self):
        w = self._make_worker(wage_type='per_sqft', sqft=25.0)
        te = self._time_entry(w, 0, hours=8.0, wage=2500.0, qty=100.0)
        self.client.post(f'/hdc/timekeeping/{te.id}/edit', data={
            'project_id': self.project.id, 'stage_id': self.stage.id,
            'hours': 8.0, 'qty_sqft': 120.0, '_csrf_token': self._token(),
        }, follow_redirects=True)
        db.session.refresh(te)
        self.assertAlmostEqual(te.qty_sqft, 120.0, places=2)
        self.assertAlmostEqual(te.wage_calculated, 3000.0, places=2)


# --------------------------------------------------------------------------
# LABOUR_AUDIT #2 -- tips written from the Workers screen carry TIP_EXPENSE_ID
# --------------------------------------------------------------------------
class TestTipTagging(LabourTestCase):
    def _pay_with_tip(self, w, amount):
        return self.client.post(f'/hdc/workers/{w.id}/payment', data={
            'date': (D0 + timedelta(days=5)).isoformat(),
            'amount': amount, 'overpay_as_tip': '1',
            'project_id': self.project.id, 'stage_id': self.stage.id,
            'notes': 'salary + thanks', '_csrf_token': self._token(),
        }, follow_redirects=True)

    def test_tip_rows_are_tagged_with_expense_id(self):
        w = self._make_worker()
        self._time_entry(w, 0, wage=1000.0)
        self._pay_with_tip(w, 1200.0)

        tip_row = LabourLedger.query.filter_by(worker_id=w.id,
                                               entry_type='tip').first()
        self.assertIsNotNone(tip_row, 'overpayment should create a tip row')
        self.assertAlmostEqual(tip_row.amount, 200.0, places=2)
        m = re.search(r'TIP_EXPENSE_ID:(\d+)', tip_row.notes or '')
        self.assertIsNotNone(m, 'tip ledger row must carry TIP_EXPENSE_ID')

        exp = Expense.query.get(int(m.group(1)))
        self.assertIsNotNone(exp)
        self.assertAlmostEqual(exp.amount, 200.0, places=2)
        self.assertEqual(exp.tip_worker_id, w.id)
        self.assertIn(f'TIP_EXPENSE_ID:{exp.id}', exp.remarks or '')

    def test_reconciler_does_not_duplicate_a_tagged_tip(self):
        w = self._make_worker()
        self._time_entry(w, 0, wage=1000.0)
        self._pay_with_tip(w, 1200.0)
        before = LabourLedger.query.filter_by(worker_id=w.id,
                                              entry_type='tip').count()
        _reconcile_worker_tip_ledger(w)
        db.session.commit()
        after = LabourLedger.query.filter_by(worker_id=w.id,
                                             entry_type='tip').count()
        self.assertEqual(before, after, 'reconciler duplicated a tagged tip')


# --------------------------------------------------------------------------
# LABOUR_AUDIT #3 -- voiding a tip voids its expense and stays voided
# --------------------------------------------------------------------------
class TestTipVoidCascade(LabourTestCase):
    def test_void_tip_voids_expense_and_does_not_resurrect(self):
        w = self._make_worker()
        self._time_entry(w, 0, wage=1000.0)
        self.client.post(f'/hdc/workers/{w.id}/payment', data={
            'date': (D0 + timedelta(days=5)).isoformat(), 'amount': 1200.0,
            'overpay_as_tip': '1', 'project_id': self.project.id,
            'stage_id': self.stage.id, '_csrf_token': self._token(),
        }, follow_redirects=True)

        tip_row = LabourLedger.query.filter_by(worker_id=w.id,
                                               entry_type='tip').first()
        self.client.post(f'/hdc/workers/{w.id}/ledger/{tip_row.id}/void', data={
            'void_reason': 'entered by mistake', '_csrf_token': self._token(),
        }, follow_redirects=True)
        db.session.expire_all()

        tip_row = LabourLedger.query.get(tip_row.id)
        self.assertTrue(tip_row.is_void)
        exp = _linked_expense_for_labour_ledger(tip_row)
        self.assertIsNotNone(exp, 'voided tip should still resolve its expense')
        self.assertTrue(exp.is_void, 'linked tip expense must be voided too')

        # Opening the ledger runs the reconciler -- the tip must not come back.
        self.client.get(f'/hdc/workers/{w.id}/ledger')
        db.session.expire_all()
        live_tips = LabourLedger.query.filter_by(
            worker_id=w.id, entry_type='tip', is_void=False).count()
        self.assertEqual(live_tips, 0, 'voided tip was resurrected')

    def test_restore_tip_restores_expense(self):
        w = self._make_worker()
        self._time_entry(w, 0, wage=1000.0)
        self.client.post(f'/hdc/workers/{w.id}/payment', data={
            'date': (D0 + timedelta(days=5)).isoformat(), 'amount': 1200.0,
            'overpay_as_tip': '1', 'project_id': self.project.id,
            'stage_id': self.stage.id, '_csrf_token': self._token(),
        }, follow_redirects=True)
        tip_row = LabourLedger.query.filter_by(worker_id=w.id,
                                               entry_type='tip').first()
        self.client.post(f'/hdc/workers/{w.id}/ledger/{tip_row.id}/void', data={
            'void_reason': 'oops', '_csrf_token': self._token()},
            follow_redirects=True)
        self.client.post(f'/hdc/workers/{w.id}/ledger/{tip_row.id}/restore',
                         data={'_csrf_token': self._token()},
                         follow_redirects=True)
        db.session.expire_all()
        tip_row = LabourLedger.query.get(tip_row.id)
        self.assertFalse(tip_row.is_void)
        exp = _linked_expense_for_labour_ledger(tip_row)
        self.assertIsNotNone(exp)
        self.assertFalse(exp.is_void, 'restored tip must un-void its expense')


# --------------------------------------------------------------------------
# LABOUR_AUDIT #4 -- deleting a payroll run reverses the cash
# --------------------------------------------------------------------------
class TestPayrollDelete(LabourTestCase):
    def _run_with_payment(self, w):
        self._time_entry(w, 0, wage=1000.0)
        self.client.post('/hdc/payroll/generate', data={
            'date_from': D0.isoformat(),
            'date_to': (D0 + timedelta(days=5)).isoformat(),
            '_csrf_token': self._token(),
        }, follow_redirects=True)
        run = PayrollRun.query.order_by(PayrollRun.id.desc()).first()
        self.assertIsNotNone(run)
        self.client.post('/hdc/payroll/generate', data={
            'action': 'pay_all', 'run_id': run.id, '_csrf_token': self._token(),
        }, follow_redirects=True)
        return run

    def test_delete_voids_payments_and_their_account_transactions(self):
        w = self._make_worker()
        run = self._run_with_payment(w)
        pay_rows = LabourLedger.query.filter_by(worker_id=w.id,
                                                entry_type='payment').all()
        self.assertTrue(pay_rows)
        txns = AccountTransaction.query.filter(
            AccountTransaction.reference_id.in_(
                [f'labour_ledger#{r.id}' for r in pay_rows])).all()
        self.assertTrue(txns, 'payment should have posted to unified accounts')

        self.client.post(f'/hdc/payroll/{run.id}/delete',
                         data={'_csrf_token': self._token()},
                         follow_redirects=True)
        db.session.expire_all()

        for r in pay_rows:
            row = LabourLedger.query.get(r.id)
            self.assertIsNotNone(row, 'payment row must be voided, not deleted')
            self.assertTrue(row.is_void)
        for t in txns:
            self.assertTrue(AccountTransaction.query.get(t.id).is_void,
                            'account transaction must be voided with the payment')

        # Cash is back: the worker is owed the wage again.
        snap = _worker_payable_snapshot(w.id)
        self.assertAlmostEqual(snap['payable'], 1000.0, places=2)

    def test_orphan_account_transaction_is_not_left_behind(self):
        w = self._make_worker()
        run = self._run_with_payment(w)
        self.client.post(f'/hdc/payroll/{run.id}/delete',
                         data={'_csrf_token': self._token()},
                         follow_redirects=True)
        db.session.expire_all()
        live = AccountTransaction.query.filter(
            AccountTransaction.is_void == False,
            AccountTransaction.source_type == 'labour_ledger_payment').all()
        self.assertEqual(live, [], 'live cash posting survived the run deletion')


# --------------------------------------------------------------------------
# LABOUR_AUDIT #7 -- overlapping payroll runs are refused
# --------------------------------------------------------------------------
class TestPayrollOverlap(LabourTestCase):
    def test_overlapping_run_is_blocked(self):
        self._make_worker()
        first = self.client.post('/hdc/payroll/generate', data={
            'date_from': D0.isoformat(),
            'date_to': (D0 + timedelta(days=9)).isoformat(),
            '_csrf_token': self._token(),
        }, follow_redirects=True)
        self.assertEqual(PayrollRun.query.count(), 1)

        second = self.client.post('/hdc/payroll/generate', data={
            'date_from': (D0 + timedelta(days=5)).isoformat(),
            'date_to': (D0 + timedelta(days=14)).isoformat(),
            '_csrf_token': self._token(),
        }, follow_redirects=True)
        self.assertEqual(PayrollRun.query.count(), 1,
                         'overlapping payroll run was created')
        self.assertIn('overlaps payroll run', second.get_data(as_text=True))

    def test_disjoint_run_is_allowed(self):
        self._make_worker()
        self.client.post('/hdc/payroll/generate', data={
            'date_from': D0.isoformat(),
            'date_to': (D0 + timedelta(days=9)).isoformat(),
            '_csrf_token': self._token(),
        }, follow_redirects=True)
        self.client.post('/hdc/payroll/generate', data={
            'date_from': (D0 + timedelta(days=10)).isoformat(),
            'date_to': (D0 + timedelta(days=19)).isoformat(),
            '_csrf_token': self._token(),
        }, follow_redirects=True)
        self.assertEqual(PayrollRun.query.count(), 2)


# --------------------------------------------------------------------------
# LABOUR_AUDIT #8 -- unrecovered advances are shown, not swallowed
# --------------------------------------------------------------------------
class TestAdvanceCarriedForward(LabourTestCase):
    def test_salary_card_reports_carried_forward_advance(self):
        w = self._make_worker()
        self._ledger(w, 'advance', 3000.0, 0)   # far above the period's wage
        self._time_entry(w, 0, wage=1000.0)
        self.client.post('/hdc/payroll/generate', data={
            'date_from': D0.isoformat(),
            'date_to': (D0 + timedelta(days=5)).isoformat(),
            '_csrf_token': self._token(),
        }, follow_redirects=True)
        run = PayrollRun.query.order_by(PayrollRun.id.desc()).first()

        html = self.client.get(f'/hdc/payroll/{run.id}/salary-cards'
                               ).get_data(as_text=True)
        self.assertIn('carried fwd', html)

        summary = self.client.get(
            f'/hdc/payroll/generate?run_id={run.id}').get_data(as_text=True)
        self.assertIn('carried fwd', summary)

    def test_ledger_still_owes_the_unrecovered_advance(self):
        w = self._make_worker()
        self._ledger(w, 'advance', 3000.0, 0)
        self._time_entry(w, 0, wage=1000.0)
        # The all-time ledger keeps the full 2000 PKR receivable from the worker.
        self.assertAlmostEqual(w.balance_due, -2000.0, places=2)


# --------------------------------------------------------------------------
# LABOUR_AUDIT #10 -- running balance is driven by the time entries
# --------------------------------------------------------------------------
class TestRunningBalance(LabourTestCase):
    def test_orphan_work_row_does_not_inflate_running_balance(self):
        w = self._make_worker()
        self._time_entry(w, 0, wage=1000.0)
        # An orphaned 'work' row with no time entry behind it.
        self._ledger(w, 'work', 5000.0, 0, notes='orphan')
        html = self.client.get(f'/hdc/workers/{w.id}/ledger'
                               ).get_data(as_text=True)
        self.assertIn('1,000', html)
        self.assertNotIn('6,000', html)

    def test_voided_time_entry_excluded_from_running_balance(self):
        w = self._make_worker()
        live = self._time_entry(w, 0, wage=1000.0)
        dead = self._time_entry(w, 1, wage=700.0, void=True, start_hour=9)
        # Work rows mirror both entries, as _sync_work_ledger_for_time_entry does.
        self._ledger(w, 'work', 1000.0, 0, notes='live')
        self._ledger(w, 'work', 700.0, 1, notes='void')
        db.session.query(LabourLedger).filter_by(
            worker_id=w.id, entry_type='work').all()
        rows = LabourLedger.query.filter_by(worker_id=w.id,
                                            entry_type='work').all()
        rows[0].time_entry_id = live.id
        rows[1].time_entry_id = dead.id
        db.session.commit()

        html = self.client.get(f'/hdc/workers/{w.id}/ledger'
                               ).get_data(as_text=True)
        self.assertIn('1,000', html)
        self.assertNotIn('1,700', html)


# --------------------------------------------------------------------------
# LABOUR_AUDIT #13 -- editing a tip keeps its expense in step
# --------------------------------------------------------------------------
class TestTipEditSync(LabourTestCase):
    def _make_tip(self, w, amount=1200.0):
        self.client.post(f'/hdc/workers/{w.id}/payment', data={
            'date': (D0 + timedelta(days=5)).isoformat(), 'amount': amount,
            'overpay_as_tip': '1', 'project_id': self.project.id,
            'stage_id': self.stage.id, '_csrf_token': self._token(),
        }, follow_redirects=True)
        return LabourLedger.query.filter_by(worker_id=w.id,
                                            entry_type='tip').first()

    def test_editing_tip_amount_updates_the_expense(self):
        w = self._make_worker()
        self._time_entry(w, 0, wage=1000.0)
        tip_row = self._make_tip(w, 1200.0)
        exp = _linked_expense_for_labour_ledger(tip_row)
        self.assertAlmostEqual(exp.amount, 200.0, places=2)

        self.client.post(f'/hdc/workers/{w.id}/ledger/{tip_row.id}/edit', data={
            'date': (D0 + timedelta(days=6)).isoformat(), 'amount': 350.0,
            'project_id': self.project.id, 'stage_id': self.stage.id,
            'notes': 'corrected tip', '_csrf_token': self._token(),
        }, follow_redirects=True)
        db.session.expire_all()

        exp = Expense.query.get(exp.id)
        self.assertAlmostEqual(exp.amount, 350.0, places=2,
                               msg='expense amount did not follow the ledger edit')
        self.assertEqual(exp.date, D0 + timedelta(days=6))

    def test_edit_cannot_drop_the_expense_id_tag(self):
        w = self._make_worker()
        self._time_entry(w, 0, wage=1000.0)
        tip_row = self._make_tip(w, 1200.0)
        exp_id = _linked_expense_for_labour_ledger(tip_row).id

        # The user types fresh notes with no tag in them at all.
        self.client.post(f'/hdc/workers/{w.id}/ledger/{tip_row.id}/edit', data={
            'date': (D0 + timedelta(days=5)).isoformat(), 'amount': 200.0,
            'project_id': self.project.id, 'stage_id': self.stage.id,
            'notes': 'thanks for the extra day', '_csrf_token': self._token(),
        }, follow_redirects=True)
        db.session.expire_all()

        tip_row = LabourLedger.query.get(tip_row.id)
        self.assertIn(f'TIP_EXPENSE_ID:{exp_id}', tip_row.notes or '',
                      'edit dropped the tag that identifies the tip expense')
        self.assertIn('thanks for the extra day', tip_row.notes)

        # And the reconciler still will not duplicate it.
        before = LabourLedger.query.filter_by(worker_id=w.id,
                                              entry_type='tip').count()
        _reconcile_worker_tip_ledger(w)
        db.session.commit()
        self.assertEqual(before, LabourLedger.query.filter_by(
            worker_id=w.id, entry_type='tip').count())


# --------------------------------------------------------------------------
# LABOUR_AUDIT #3 (settlements) -- write-offs move with their ledger row
# --------------------------------------------------------------------------
class TestSettlementCascade(LabourTestCase):
    def test_void_settlement_voids_its_expense(self):
        w = self._make_worker()
        self._time_entry(w, 0, wage=1000.0)
        self.client.post(f'/hdc/workers/{w.id}/payment', data={
            'date': (D0 + timedelta(days=5)).isoformat(), 'amount': 600.0,
            'settle_shortfall': '1', 'project_id': self.project.id,
            'stage_id': self.stage.id, '_csrf_token': self._token(),
        }, follow_redirects=True)

        settle = LabourLedger.query.filter_by(worker_id=w.id,
                                              entry_type='settlement').first()
        self.assertIsNotNone(settle, 'shortfall should be booked as settlement')
        exp = _linked_expense_for_labour_ledger(settle)
        self.assertIsNotNone(exp, 'settlement must have a matching expense')
        self.assertFalse(exp.is_void)

        self.client.post(f'/hdc/workers/{w.id}/ledger/{settle.id}/void', data={
            'void_reason': 'not agreed', '_csrf_token': self._token()},
            follow_redirects=True)
        db.session.expire_all()
        exp = Expense.query.get(exp.id)
        self.assertTrue(exp.is_void,
                        'settlement expense must be voided with the ledger row')


    def test_worker_id_prefix_cannot_cross_link_expenses(self):
        """``SETTLE_WORKER_ID:1`` must not match worker 10's expense."""
        w1 = self._make_worker(code='W-001', name='Ali Raza')
        w10 = self._make_worker(code='W-010', name='Bilal Khan')
        for w in (w1, w10):
            self._time_entry(w, 0, wage=1000.0)

        # Both workers get a 400 PKR shortfall settled on the same date, so the
        # only thing distinguishing their expenses is the worker id in remarks.
        for w in (w1, w10):
            self.client.post(f'/hdc/workers/{w.id}/payment', data={
                'date': (D0 + timedelta(days=5)).isoformat(), 'amount': 600.0,
                'settle_shortfall': '1', 'project_id': self.project.id,
                'stage_id': self.stage.id, '_csrf_token': self._token(),
            }, follow_redirects=True)

        s1 = LabourLedger.query.filter_by(worker_id=w1.id,
                                          entry_type='settlement').first()
        s10 = LabourLedger.query.filter_by(worker_id=w10.id,
                                           entry_type='settlement').first()
        self.assertIsNotNone(s1)
        self.assertIsNotNone(s10)

        e1 = _linked_expense_for_labour_ledger(s1)
        e10 = _linked_expense_for_labour_ledger(s10)
        self.assertIsNotNone(e1)
        self.assertIsNotNone(e10)
        self.assertNotEqual(e1.id, e10.id, 'worker 1 resolved worker 10 expense')
        self.assertIn(f'SETTLE_WORKER_ID:{w1.id}', e1.remarks or '')
        self.assertIn(f'SETTLE_WORKER_ID:{w10.id}', e10.remarks or '')


# --------------------------------------------------------------------------
# The audit CLI itself
# --------------------------------------------------------------------------
class TestAuditScript(LabourTestCase):
    def _audit(self, extra_args=('--exit-zero',)):
        import subprocess
        script = os.path.abspath(os.path.join(os.path.dirname(__file__), '..',
                                              'scripts', 'labour_audit.py'))
        return subprocess.run(
            [sys.executable, script, '--db', self.db_path, *extra_args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False
        ).stdout.decode('utf-8', 'replace')

    def test_clean_worker_produces_no_blocking_findings(self):
        w = self._make_worker()
        self._time_entry(w, 0, wage=1000.0)
        self._ledger(w, 'advance', 200.0, 1)
        self._ledger(w, 'payment', 800.0, 2)
        db.session.commit()

        out = self._audit()
        self.assertNotIn('BALANCE_TWO_FORMULAS', out)
        self.assertNotIn('WAGE_MISMATCH', out)
        self.assertNotIn('BALANCE_OVERPAID', out)

    def test_hourly_rate_falls_back_to_daily_field(self):
        """An hourly worker whose rate lives in base_daily_wage is paid, not 0.

        The app resolves ``hourly_rate or hourly_wage`` (daily / 8). An audit
        mirror that reads only ``hourly_rate`` reports every such worker as
        unpaid -- which is what happened against the live database.
        """
        from hdc.models.workforce import WorkerRate
        w = self._make_worker(wage_type='hourly', daily=2000.0, hourly=0.0)
        db.session.add(WorkerRate(worker_id=w.id, wage_type='hourly', rate=250.0,
                                  effective_from=D0, reason='derived'))
        db.session.commit()
        # 8h x 250 = 2000, which is what the app stores.
        self._time_entry(w, 0, hours=8.0, wage=2000.0)

        out = self._audit()
        self.assertNotIn('WAGE_ZERO_WITH_HOURS', out)
        self.assertNotIn('WAGE_MISMATCH', out)

    def test_rate_card_gap_is_reported_separately_from_real_drift(self):
        """Work dated before the first rate row is a rate-card gap, not a bug."""
        from hdc.models.workforce import WorkerRate
        w = self._make_worker(wage_type='daily', daily=2200.0)
        # Rate history starts AFTER the work date, so the app cannot reproduce it.
        db.session.add(WorkerRate(worker_id=w.id, wage_type='daily', rate=2200.0,
                                  effective_from=D0 + timedelta(days=30),
                                  reason='late'))
        db.session.commit()
        self._time_entry(w, 0, wage=2100.0)   # correct for its date

        out = self._audit()
        self.assertIn('WAGE_RATE_CARD_GAP', out)
        self.assertNotIn('WAGE_MISMATCH', out)

    def test_genuine_drift_is_still_reported(self):
        from hdc.models.workforce import WorkerRate
        w = self._make_worker(wage_type='daily', daily=2200.0)
        db.session.add(WorkerRate(worker_id=w.id, wage_type='daily', rate=2200.0,
                                  effective_from=D0, reason='covers the day'))
        db.session.commit()
        self._time_entry(w, 0, wage=5000.0)   # rate covers the date: real drift

        out = self._audit()
        self.assertIn('WAGE_MISMATCH', out)
        self.assertNotIn('WAGE_RATE_CARD_GAP', out)


if __name__ == '__main__':
    unittest.main(verbosity=2)
