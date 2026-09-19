#!/usr/bin/env python3
"""Regression tests for the Cash Flow feature (Accounts section).

Covers the classification / aggregation service (hdc/services/cashflow.py)
and the three routes (records page, report page, CSV export) including the
quick-entry form (Money In / Money Out / Internal Transfer).

Run with:
    HDC_BOOTSTRAP_ADMIN_PASSWORD='Admin@1234' \
        python -m unittest tests.test_cashflow -v
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

os.environ.setdefault('HDC_ENV', 'test')
os.environ.setdefault('HDC_SECRET_KEY', 'unit-test-secret')
os.environ.setdefault('HDC_BOOTSTRAP_ADMIN_PASSWORD', 'Admin@1234')

from datetime import date, timedelta                                # noqa: E402

from hdc.app import create_app                                     # noqa: E402
from hdc.extensions import db                                      # noqa: E402
from hdc.models.accounts import Account, AccountTransaction        # noqa: E402
from hdc.utils.dates import _pkt_now_naive                         # noqa: E402
from hdc.services.cashflow import (                                # noqa: E402
    _cashflow_account_breakdown,
    _cashflow_category_breakdown,
    _cashflow_classify,
    _cashflow_daily_report_rows,
    _cashflow_group_by_day,
    _cashflow_load_rows,
    _cashflow_opening_funds,
    _cashflow_summary,
)

ADMIN_PASSWORD = os.environ['HDC_BOOTSTRAP_ADMIN_PASSWORD']


def _csrf(client, url):
    page = client.get(url).get_data(as_text=True)
    token = re.search(r'name="_csrf_token" value="([^"]+)"', page)
    return token.group(1) if token else ''


class CashFlowTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='hdc-cashflow-test-')
        self.app = create_app({'HDC_DB_PATH': os.path.join(self.tmp, 'test.db'),
                               'HDC_INSTANCE_DIR': self.tmp})
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()
        self._login()

        # Treasury accounts with known opening balances.
        self.cash = self._mk_account('Test Cash', 'cash', opening=10000.0)
        self.bank = self._mk_account('Test Bank', 'bank', opening=5000.0,
                                     bank_name='Meezan', account_number='1234')
        # Counterparty accounts.
        self.client_acc = self._mk_account('Acme Client', 'client')
        self.person = self._mk_account('Worker One', 'person')

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── helpers ──────────────────────────────────────────────────────────

    def _login(self):
        token = _csrf(self.client, '/hdc/login')
        res = self.client.post('/hdc/login', data={
            'username': 'admin', 'password': ADMIN_PASSWORD,
            '_csrf_token': token,
        })
        self.assertIn(res.status_code, (200, 302), 'login failed')

    def _mk_account(self, name, acc_type, opening=0.0, bank_name='', account_number=''):
        row = Account(
            name=name, type=acc_type, opening_balance=float(opening),
            bank_name=bank_name or None, account_number=account_number or None,
            status='active', is_void=False, created_at=_pkt_now_naive(),
        )
        db.session.add(row)
        db.session.commit()
        return Account.query.filter_by(name=name).first()

    def _txn(self, frm, to, amount, tx_date, category, tx_type=None,
             party='', note='', ref=''):
        co_types = ('company', 'cash', 'bank')
        if tx_type is None:
            frm_co = frm.type in co_types
            to_co = bool(to) and to.type in co_types
            tx_type = ('transfer' if (frm_co and to_co)
                       else ('party_receipt' if to_co else 'party_payment'))
        row = AccountTransaction(
            date=tx_date,
            amount=float(amount),
            type=tx_type,
            from_account_id=frm.id,
            to_account_id=(to.id if to else None),
            executed_by_account_id=frm.id,
            party_name=(party or None),
            category=category,
            note=(note or None),
            reference_id=(ref or None),
            is_void=False,
            created_at=_pkt_now_naive(),
        )
        db.session.add(row)
        db.session.commit()
        return row

    # ── classification ───────────────────────────────────────────────────

    def test_classify_in_out_internal(self):
        from hdc.services.cashflow import _cashflow_account_maps
        type_map, _ = _cashflow_account_maps()
        in_row = self._txn(self.client_acc, self.cash, 1000, date(2026, 9, 1), 'income', 'project_income')
        out_row = self._txn(self.cash, self.person, 800, date(2026, 9, 1), 'payroll', 'payroll')
        xfer_row = self._txn(self.cash, self.bank, 1500, date(2026, 9, 2), 'transfer', 'transfer')
        self.assertEqual(_cashflow_classify(in_row, type_map), 'in')
        self.assertEqual(_cashflow_classify(out_row, type_map), 'out')
        self.assertEqual(_cashflow_classify(xfer_row, type_map), 'internal')

    def test_classify_excludes_non_company_rows(self):
        from hdc.services.cashflow import _cashflow_account_maps
        type_map, _ = _cashflow_account_maps()
        row = self._txn(self.person, self.client_acc, 500, date(2026, 9, 3), 'expense', 'party_payment')
        self.assertEqual(_cashflow_classify(row, type_map), '')
        rows = _cashflow_load_rows()
        self.assertNotIn(500, [r['amount'] for r in rows if r['date'] == date(2026, 9, 3)])

    # ── aggregation ──────────────────────────────────────────────────────

    def test_summary_and_daily_grouping(self):
        d1 = date(2026, 9, 1)
        d2 = date(2026, 9, 2)
        self._txn(self.client_acc, self.cash, 1000, d1, 'income', 'project_income')
        self._txn(self.cash, self.person, 300, d1, 'payroll', 'payroll')
        self._txn(self.cash, self.bank, 500, d2, 'transfer', 'transfer')
        self._txn(self.cash, self.person, 200, d2, 'expense', 'party_payment')

        rows = _cashflow_load_rows()
        s = _cashflow_summary(rows)
        self.assertEqual(s['in_total'], 1000.0)
        self.assertEqual(s['out_total'], 500.0)
        self.assertEqual(s['internal_total'], 500.0)
        self.assertEqual(s['net_total'], 500.0)
        self.assertEqual(s['day_count'], 2)

        groups = _cashflow_group_by_day(rows)
        self.assertEqual(groups[0]['date'], d2)
        self.assertEqual(groups[0]['in_total'], 0.0)
        self.assertEqual(groups[0]['out_total'], 200.0)
        self.assertEqual(groups[0]['net'], -200.0)
        self.assertEqual(groups[0]['count'], 2)  # out + transfer both on d2

        daily = _cashflow_daily_report_rows(rows, opening_funds=0.0)
        self.assertEqual([d['date'] for d in daily], [d1, d2])
        self.assertEqual(daily[0]['closing'], 700.0)   # 1000 in - 300 out
        self.assertEqual(daily[1]['closing'], 500.0)   # -200 out, transfer neutral

    def test_opening_funds_before_range(self):
        self._txn(self.client_acc, self.cash, 2000, date(2026, 8, 1), 'income', 'project_income')
        self._txn(self.cash, self.person, 700, date(2026, 8, 5), 'expense', 'party_payment')
        self._txn(self.client_acc, self.cash, 100, date(2026, 9, 10), 'income', 'project_income')

        # Openings: cash 10000 + bank 5000 = 15000, plus net flow before Sep 10 (+1300)
        self.assertAlmostEqual(_cashflow_opening_funds(date(2026, 9, 10)), 16300.0)
        # No date_from -> openings only (full-history report).
        self.assertAlmostEqual(_cashflow_opening_funds(None), 15000.0)

    def test_filters_category_mode_direction(self):
        d = date(2026, 9, 1)
        self._txn(self.client_acc, self.cash, 1000, d, 'income', 'project_income')
        self._txn(self.cash, self.person, 300, d, 'payroll', 'payroll')
        self._txn(self.cash, self.bank, 500, d, 'transfer', 'transfer')

        rows = _cashflow_load_rows(direction='in')
        self.assertEqual([r['amount'] for r in rows], [1000.0])

        rows = _cashflow_load_rows(direction='out')
        self.assertEqual([r['amount'] for r in rows], [300.0])

        rows = _cashflow_load_rows(direction='internal')
        self.assertEqual([r['amount'] for r in rows], [500.0])

        rows = _cashflow_load_rows(category='payroll')
        self.assertEqual([r['amount'] for r in rows], [300.0])

        rows = _cashflow_load_rows(account_mode='bank')
        # only the cash->bank transfer touches a bank account
        self.assertEqual([r['direction'] for r in rows], ['internal'])

        rows = _cashflow_load_rows(account_mode='cash', direction='out')
        self.assertEqual([r['amount'] for r in rows], [300.0])

        rows = _cashflow_load_rows(account_id=self.cash.id)
        self.assertEqual(len(rows), 3)
        rows = _cashflow_load_rows(account_id=self.person.id)
        self.assertEqual([r['direction'] for r in rows], ['out'])

        rows = _cashflow_load_rows(date_from=d + timedelta(days=1))
        self.assertEqual(rows, [])

    def test_category_and_account_breakdown(self):
        d = date(2026, 9, 1)
        self._txn(self.client_acc, self.cash, 1000, d, 'income', 'project_income')
        self._txn(self.cash, self.person, 300, d, 'payroll', 'payroll')
        self._txn(self.cash, self.bank, 500, d, 'transfer', 'transfer')

        cats = _cashflow_category_breakdown(_cashflow_load_rows())
        by_name = {c['category']: c for c in cats}
        self.assertEqual(by_name['income']['in_total'], 1000.0)
        self.assertEqual(by_name['payroll']['out_total'], 300.0)
        self.assertEqual(by_name['transfer']['internal_total'], 500.0)
        # income + payroll = 1300 total; payroll share = 300/1300
        self.assertAlmostEqual(by_name['payroll']['pct'], round(100.0 * 300.0 / 1300.0, 1))

        accs = _cashflow_account_breakdown(_cashflow_load_rows())
        by_acc = {a['name']: a for a in accs}
        self.assertEqual(by_acc['Test Cash']['in_total'], 1000.0)
        self.assertEqual(by_acc['Test Cash']['out_total'], 300.0)
        self.assertEqual(by_acc['Test Bank']['transfer_in'], 500.0)
        self.assertEqual(by_acc['Test Cash']['transfer_out'], 500.0)
        self.assertEqual(by_acc['Test Bank']['mode'], 'bank')
        self.assertEqual(by_acc['Test Bank']['net'], 0.0)

    # ── routes ───────────────────────────────────────────────────────────

    def test_records_page_renders(self):
        d = date(2026, 9, 1)
        self._txn(self.client_acc, self.cash, 1000, d, 'income', 'project_income')
        self._txn(self.cash, self.person, 300, d, 'payroll', 'payroll')
        res = self.client.get('/hdc/accounts/cashflow')
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        self.assertIn('Daily Records', html)
        self.assertIn('Money In', html)
        self.assertIn('#%d' % (AccountTransaction.query.order_by(AccountTransaction.id.desc()).first().id), html)

    def test_quick_entry_money_in(self):
        token = _csrf(self.client, '/hdc/accounts/cashflow')
        res = self.client.post('/hdc/accounts/cashflow', data={
            'action': 'record_flow',
            'flow_kind': 'in',
            'date': date(2026, 9, 18).isoformat(),
            'amount': '2500',
            'party_name': 'Cash Receipt',
            'to_account_id': self.bank.id,
            'category': 'income',
            '_csrf_token': token,
        }, follow_redirects=True)
        self.assertEqual(res.status_code, 200)
        self.assertIn('Money In recorded: 2,500.00 PKR', res.get_data(as_text=True))
        rows = _cashflow_load_rows(direction='in')
        self.assertEqual(sum(r['amount'] for r in rows), 2500.0)
        self.assertEqual(rows[0]['to_account'], 'Test Bank')

    def test_quick_entry_money_out_with_party(self):
        token = _csrf(self.client, '/hdc/accounts/cashflow')
        res = self.client.post('/hdc/accounts/cashflow', data={
            'action': 'record_flow',
            'flow_kind': 'out',
            'date': date(2026, 9, 18).isoformat(),
            'amount': '1200',
            'from_account_id': self.cash.id,
            'party_name': 'Tea Shop',
            'category': 'expense',
            'note': 'daily tea',
            '_csrf_token': token,
        }, follow_redirects=True)
        self.assertEqual(res.status_code, 200)
        self.assertIn('Money Out recorded: 1,200.00 PKR', res.get_data(as_text=True))
        rows = _cashflow_load_rows(direction='out')
        self.assertEqual(sum(r['amount'] for r in rows), 1200.0)

    def test_quick_entry_transfer(self):
        token = _csrf(self.client, '/hdc/accounts/cashflow')
        res = self.client.post('/hdc/accounts/cashflow', data={
            'action': 'record_flow',
            'flow_kind': 'transfer',
            'date': date(2026, 9, 18).isoformat(),
            'amount': '3000',
            'from_account_id': self.cash.id,
            'to_account_id': self.bank.id,
            '_csrf_token': token,
        }, follow_redirects=True)
        self.assertEqual(res.status_code, 200)
        self.assertIn('Internal Transfer recorded: 3,000.00 PKR', res.get_data(as_text=True))
        rows = _cashflow_load_rows(direction='internal')
        self.assertEqual(sum(r['amount'] for r in rows), 3000.0)

    def test_quick_entry_rejects_company_to_company_as_out(self):
        token = _csrf(self.client, '/hdc/accounts/cashflow')
        res = self.client.post('/hdc/accounts/cashflow', data={
            'action': 'record_flow',
            'flow_kind': 'out',
            'date': date(2026, 9, 18).isoformat(),
            'amount': '100',
            'from_account_id': self.cash.id,
            'to_account_id': self.bank.id,
            '_csrf_token': token,
        }, follow_redirects=True)
        html = res.get_data(as_text=True)
        self.assertIn('Internal Transfer instead', html)
        self.assertEqual(len(_cashflow_load_rows(direction='out')), 0)

    def test_quick_entry_blocks_overdraft(self):
        token = _csrf(self.client, '/hdc/accounts/cashflow')
        res = self.client.post('/hdc/accounts/cashflow', data={
            'action': 'record_flow',
            'flow_kind': 'out',
            'date': date(2026, 9, 18).isoformat(),
            'amount': '999999',
            'from_account_id': self.cash.id,
            'party_name': 'Big Supplier',
            '_csrf_token': token,
        }, follow_redirects=True)
        self.assertIn('Insufficient balance', res.get_data(as_text=True))
        self.assertEqual(len(_cashflow_load_rows(direction='out')), 0)

    def test_report_page_renders(self):
        d = date(2026, 9, 1)
        self._txn(self.client_acc, self.cash, 1000, d, 'income', 'project_income')
        self._txn(self.cash, self.person, 300, d, 'payroll', 'payroll')
        self._txn(self.cash, self.bank, 500, d, 'transfer', 'transfer')
        res = self.client.get('/hdc/accounts/cashflow/report')
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        for needle in ('Total Money In', 'Total Money Out', 'Net Cash Flow',
                       'Internal Transfers', 'Daily Cash Flow', 'By Category',
                       'By Account (Cash / Bank)', 'Opening funds', 'Export CSV'):
            self.assertIn(needle, html)
        # opening 15000 + net 700 = closing 15700 shown in the daily table
        self.assertIn('15,700', html)

    def test_report_filters_apply(self):
        d = date(2026, 9, 1)
        self._txn(self.client_acc, self.cash, 1000, d, 'income', 'project_income')
        self._txn(self.cash, self.person, 300, d, 'payroll', 'payroll')
        res = self.client.get('/hdc/accounts/cashflow/report?category=payroll')
        html = res.get_data(as_text=True)
        # With category=payroll the "Money In" KPI must be zero…
        self.assertRegex(html, r'Total Money In</div>\s*<div class="kpi-value">0</div>')
        # …and "Money Out" must show the payroll payment.
        self.assertRegex(html, r'Total Money Out</div>\s*<div class="kpi-value">300</div>')

    def test_csv_export(self):
        d = date(2026, 9, 1)
        self._txn(self.client_acc, self.cash, 1000, d, 'income', 'project_income', party='Acme Client', ref='cheque-1')
        self._txn(self.cash, self.person, 300, d, 'payroll', 'payroll')
        res = self.client.get('/hdc/accounts/cashflow/export')
        self.assertEqual(res.status_code, 200)
        self.assertIn('text/csv', res.content_type)
        text = res.get_data(as_text=True)
        lines = [ln for ln in text.strip().splitlines() if ln]
        self.assertEqual(lines[0].split(',')[0], 'Date')
        self.assertIn('Money In (PKR)', lines[0])
        self.assertEqual(len(lines), 3)  # header + 2 rows
        self.assertIn('1000.00', text)
        self.assertIn('300.00', text)

    def test_requires_login(self):
        # Drop the app context pushed in setUp: Flask reuses it for the test
        # client's requests, which would carry the logged-in user with it.
        self.ctx.pop()
        try:
            res = self.app.test_client().get('/hdc/accounts/cashflow')
        finally:
            self.ctx = self.app.app_context()
            self.ctx.push()
        self.assertEqual(res.status_code, 302)
        self.assertIn('/hdc/login', res.headers.get('Location', ''))


if __name__ == '__main__':
    unittest.main(verbosity=2)
