#!/usr/bin/env python3
"""Labour Card: site-wise days/overtime/amount, payable, Fuel & Food, weeks.

The fixture is built around Monday 2026-09-07 (``M``) so every figure can
be checked by hand:

* before the period: one day earned (2,000) and an advance (500)
  → opening balance 1,500;
* week of M: Site Alpha 3.5 days / 2h OT / 7,500 (including a 4h split day
  and a migrated legacy row whose 9,999 legacy wage must *not* double up);
  Site Beta 2.5 days / 1h OT / 5,000 (split day + a never-migrated legacy
  attendance row); advance 1,000; tip 300 (neutral);
* week of M+7: payment 3,000; settlement 200.

→ Grand total 12,500; period balance 8,300; closing = payable = 9,800.
Fuel/Food are *site* expenses: Alpha fuel 5,000 + food 1,200, Beta food
800 — with a Material expense, a voided fuel expense and an out-of-range
fuel expense all correctly excluded, and none of it in the worker totals.
"""

import os
import sys
import unittest
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from qa_support import IsolatedAppTest  # noqa: E402

from sqlalchemy import func  # noqa: E402

from hdc.extensions import db  # noqa: E402
from hdc.models.accounts import Expense, ExpenseCategory  # noqa: E402
from hdc.models.projects import Project  # noqa: E402
from hdc.models.workforce import (  # noqa: E402
    Attendance, LabourLedger, TimeEntry, Worker)
from hdc.services.labour_card import classify_site_expense, labour_card  # noqa: E402

M = date(2026, 9, 7)  # a Monday


def _at(day, hour=9):
    return datetime.combine(day, datetime.min.time()) + timedelta(hours=hour)


class LabourCardTestCase(IsolatedAppTest):
    def setUp(self):
        super().setUp()
        assert M.weekday() == 0, 'fixture anchor must stay a Monday'
        with self.app.app_context():
            alpha = Project(name='Site Alpha', project_code='LC-A', client='Owner')
            beta = Project(name='Site Beta', project_code='LC-B', client='Owner')
            worker = Worker(name='Card Worker', worker_code='LC-W',
                            role_type='Mason', base_daily_wage=2000,
                            wage_type='daily')
            db.session.add_all([alpha, beta, worker])
            db.session.flush()

            def te(project, day, hours, overtime, wage, hour=9, **kw):
                start = _at(day, hour)
                row = TimeEntry(worker_id=worker.id, project_id=project.id,
                                check_in=start,
                                check_out=start + timedelta(hours=hours),
                                hours=hours, overtime=overtime,
                                wage_calculated=wage, is_void=False, **kw)
                db.session.add(row)
                return row

            def ledger(kind, day, amount, project=None, notes=''):
                db.session.add(LabourLedger(
                    worker_id=worker.id, entry_type=kind, amount=amount,
                    date=day, project_id=(project.id if project else None),
                    notes=notes, is_void=False))

            # --- before the period (opening balance) ---------------------
            te(alpha, M - timedelta(days=20), 8, 0, 2000)
            ledger('advance', M - timedelta(days=20), 500, alpha, 'old advance')

            # --- week of M ------------------------------------------------
            te(alpha, M, 8, 0, 2000)
            te(alpha, M + timedelta(days=1), 10, 2, 2500)
            te(alpha, M + timedelta(days=2), 4, 0, 1000)            # split day …
            te(beta, M + timedelta(days=2), 4, 0, 1000, hour=13)    # … second half
            te(beta, M + timedelta(days=3), 8, 0, 2000)
            legacy = Attendance(worker_id=worker.id, project_id=beta.id,
                                date=M + timedelta(days=4), hours_worked=8,
                                overtime_hours=1, full_day_equivalent=1,
                                total_wage=2000)
            migrated = Attendance(worker_id=worker.id, project_id=alpha.id,
                                  date=M + timedelta(days=5), hours_worked=8,
                                  overtime_hours=0, full_day_equivalent=1,
                                  total_wage=9999)
            db.session.add_all([legacy, migrated])
            db.session.flush()
            te(alpha, M + timedelta(days=5), 8, 0, 2000,
               attendance_id=migrated.id)
            ledger('advance', M + timedelta(days=1), 1000, alpha, 'weekly advance')
            ledger('tip', M + timedelta(days=2), 300, alpha, 'tea money')

            # --- week of M+7 ----------------------------------------------
            ledger('payment', M + timedelta(days=7), 3000, None, 'week payment')
            ledger('settlement', M + timedelta(days=8), 200, alpha, 'shortfall')

            # --- site Fuel / Food expenses --------------------------------
            def category(name):
                row = (ExpenseCategory.query
                       .filter(func.lower(ExpenseCategory.name) == name.lower())
                       .first())
                if row is None:
                    row = ExpenseCategory(name=name, active_status=True)
                    db.session.add(row)
                    db.session.flush()
                return row

            fuel, food, material = category('Fuel'), category('Food'), category('Material')

            def expense(project, cat, day, amount, remarks='', void=False):
                db.session.add(Expense(project_id=project.id, category_id=cat.id,
                                       amount=amount, date=day, remarks=remarks,
                                       is_void=void))

            expense(alpha, fuel, M + timedelta(days=1), 5000, 'Petrol for generator')
            expense(alpha, food, M + timedelta(days=2), 1200, 'Labour lunch')
            expense(alpha, material, M + timedelta(days=1), 9999, 'Cement (not fuel/food)')
            expense(alpha, fuel, M + timedelta(days=3), 7000, 'Voided fuel', void=True)
            expense(alpha, fuel, M + timedelta(days=20), 4000, 'Outside the period')
            expense(beta, food, M + timedelta(days=3), 800, 'Tea & food')

            db.session.commit()
            self.ids = {'worker': worker.id, 'alpha': alpha.id, 'beta': beta.id}

    # ── service figures ──────────────────────────────────────────────────
    def _card(self, date_from=M, date_to=M + timedelta(days=13)):
        with self.app.app_context():
            return labour_card(self.ids['worker'], date_from=date_from,
                               date_to=date_to)

    def test_site_rows_days_overtime_amount(self):
        card = self._card()
        sites = {s['name']: s for s in card['sites']}
        alpha, beta = sites['Site Alpha'], sites['Site Beta']
        # Alpha: 8h + (10−2)h + 4h split + 8h migrated = 28 regular h = 3.5 days
        self.assertAlmostEqual(alpha['days'], 3.5)
        self.assertAlmostEqual(alpha['overtime'], 2.0)
        self.assertAlmostEqual(alpha['amount'], 7500.0)
        self.assertEqual(alpha['dates_worked'], 4)
        # Beta: 4h split + 8h = 1.5 days, plus 1 legacy day; legacy OT 1h
        self.assertAlmostEqual(beta['days'], 2.5)
        self.assertAlmostEqual(beta['overtime'], 1.0)
        self.assertAlmostEqual(beta['amount'], 5000.0)

        self.assertAlmostEqual(card['total_days'], 6.0)
        self.assertAlmostEqual(card['total_overtime'], 3.0)
        self.assertAlmostEqual(card['grand_total'], 12500.0)
        # The migrated legacy wage (9,999) must never double-count.
        self.assertNotAlmostEqual(card['grand_total'], 12500.0 + 9999.0)

    def test_period_summary_and_payable(self):
        card = self._card()
        period = card['period']
        self.assertAlmostEqual(period['opening_balance'], 1500.0)   # 2000 − 500
        self.assertAlmostEqual(period['advance'], 1000.0)
        self.assertAlmostEqual(period['paid'], 3000.0)
        self.assertAlmostEqual(period['settled'], 200.0)
        self.assertAlmostEqual(period['tip'], 300.0)
        # Tip is tracked but never deducted: 12500 − 1000 − 3000 − 200
        self.assertAlmostEqual(period['period_balance'], 8300.0)
        self.assertAlmostEqual(period['closing_balance'], 9800.0)
        # Whole-ledger payable comes from the canonical snapshot.
        self.assertAlmostEqual(card['payable_now'], 9800.0)
        self.assertAlmostEqual(card['balance_now'], 9800.0)
        self.assertAlmostEqual(card['snapshot']['earned'], 14500.0)
        self.assertAlmostEqual(card['balance_after_to'], 0.0)

    def test_fuel_food_are_site_info_only(self):
        card = self._card()
        sites = {s['name']: s for s in card['sites']}
        self.assertAlmostEqual(sites['Site Alpha']['fuel'], 5000.0)
        self.assertAlmostEqual(sites['Site Alpha']['food'], 1200.0)
        self.assertAlmostEqual(sites['Site Beta']['fuel'], 0.0)
        self.assertAlmostEqual(sites['Site Beta']['food'], 800.0)
        self.assertAlmostEqual(card['total_fuel'], 5000.0)
        self.assertAlmostEqual(card['total_food'], 2000.0)
        # Material, the voided fuel row and the out-of-period fuel are out;
        # exactly the three real lines remain.
        self.assertEqual(card['site_expenses']['detail_total_count'], 3)
        self.assertEqual(card['site_expenses']['counted_categories'],
                         ['Food', 'Fuel'])
        # And none of it leaked into the worker's own money.
        self.assertAlmostEqual(card['grand_total'], 12500.0)
        self.assertAlmostEqual(card['payable_now'], 9800.0)

    def test_last_four_weeks_pending_is_cumulative(self):
        card = self._card()
        pack = card['last_4_weeks']
        weeks = pack['weeks']
        self.assertEqual([w['start'] for w in weeks],
                         [M - timedelta(weeks=2), M - timedelta(weeks=1),
                          M, M + timedelta(weeks=1)])
        self.assertAlmostEqual(pack['opening_before'], 1500.0)
        self.assertAlmostEqual(weeks[0]['pending'], 1500.0)
        self.assertAlmostEqual(weeks[1]['pending'], 1500.0)

        busy = weeks[2]
        self.assertAlmostEqual(busy['days'], 6.0)
        self.assertAlmostEqual(busy['overtime'], 3.0)
        self.assertAlmostEqual(busy['earned'], 12500.0)
        self.assertAlmostEqual(busy['advance'], 1000.0)
        self.assertAlmostEqual(busy['paid'], 0.0)
        self.assertAlmostEqual(busy['week_balance'], 11500.0)
        self.assertAlmostEqual(busy['pending'], 13000.0)

        payday = weeks[3]
        self.assertTrue(payday['is_current'])
        self.assertAlmostEqual(payday['paid'], 3000.0)
        self.assertAlmostEqual(payday['settled'], 200.0)
        self.assertAlmostEqual(payday['week_balance'], -3200.0)
        # Paid in week 4 against week 3's wage: pending settles at 9,800,
        # which is also the card's closing balance.
        self.assertAlmostEqual(payday['pending'], 9800.0)
        self.assertAlmostEqual(pack['totals']['pending'], 9800.0)
        self.assertAlmostEqual(pack['totals']['earned'], 12500.0)

    def test_all_time_bounds_and_unknown_worker(self):
        card = self._card(date_from=None, date_to=None)
        self.assertAlmostEqual(card['grand_total'], 14500.0)
        self.assertAlmostEqual(card['period']['opening_balance'], 0.0)
        self.assertAlmostEqual(card['period']['closing_balance'], 9800.0)
        # The out-of-period fuel expense joins in when the range is open.
        self.assertAlmostEqual(card['total_fuel'], 9000.0)

        with self.app.app_context():
            self.assertIsNone(labour_card(999999))
            # Reversed bounds are swapped, not treated as an empty range.
            swapped = labour_card(self.ids['worker'],
                                  date_from=M + timedelta(days=13), date_to=M)
        self.assertAlmostEqual(swapped['grand_total'], 12500.0)

    def test_classify_site_expense(self):
        self.assertEqual(classify_site_expense('Fuel'), 'fuel')
        self.assertEqual(classify_site_expense('Petrol / Diesel'), 'fuel')
        self.assertEqual(classify_site_expense('Food'), 'food')
        self.assertEqual(classify_site_expense('Food & Refreshment'), 'food')
        self.assertIsNone(classify_site_expense('Transport'))
        self.assertIsNone(classify_site_expense('Material'))
        self.assertIsNone(classify_site_expense(''))
        self.assertIsNone(classify_site_expense(None))

    # ── page + wiring ────────────────────────────────────────────────────
    def test_page_renders_picker_then_card(self):
        landing = self.client.get('/hdc/labour-card')
        self.assertEqual(landing.status_code, 200)
        body = landing.get_data(as_text=True)
        self.assertIn('Labour Card', body)
        # Searchable combo-box list of worker names (strict select combo).
        self.assertIn('data-hdc-combo="labourWorker"', body)
        self.assertIn('Card Worker (LC-W)', body)
        self.assertIn('Select a worker', body)

        url = (f"/hdc/labour-card?worker_id={self.ids['worker']}"
               f"&date_from={M.isoformat()}"
               f"&date_to={(M + timedelta(days=13)).isoformat()}")
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        body = page.get_data(as_text=True)
        for needle in ('Site Alpha', 'Site Beta', 'Site-wise Work',
                       'Days Worked', 'Overtime (hrs)', 'Grand Total',
                       '12,500', '7,500', '5,000',
                       'Payable Now', '9,800',
                       'Fuel (site)*', 'Food (site)*',
                       'NOT added to this worker',
                       'Advances &amp; Payments in This Period',
                       'weekly advance', 'week payment',
                       'Fuel &amp; Food Detail', 'Petrol for generator',
                       'Last 4 Weeks', 'Pending (owed at week end)',
                       'This week'):
            self.assertIn(needle, body, needle)
        # Sidebar entry is present and highlighted as the current page.
        self.assertIn('href="/hdc/labour-card"', body)

    def test_unknown_worker_shows_picker_with_warning(self):
        page = self.client.get('/hdc/labour-card?worker_id=999999')
        self.assertEqual(page.status_code, 200)
        body = page.get_data(as_text=True)
        self.assertIn('Selected worker was not found', body)
        self.assertIn('Select a worker', body)

    def test_permission_catalog_knows_the_page(self):
        from hdc.services.permissions import PAGE_BY_ID, page_id_for_path
        self.assertEqual(page_id_for_path('/hdc/labour-card'), 'labour_card')
        self.assertIn('labour_card', PAGE_BY_ID)


if __name__ == '__main__':
    unittest.main()
