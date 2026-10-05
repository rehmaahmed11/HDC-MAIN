#!/usr/bin/env python3
"""Tests for the HDC Tools discount ("payments handling discount") feature.

A discount is the part of a rental bill the customer never pays.  These tests
pin the three promises the feature makes:

 1. **It shrinks what is owed** — cash + discount together clear the balance,
    and the rental can never read as over-settled.
 2. **It never touches cash** — only the cash leg lands in Cash/Bank; the
    concession lands in Accounts as its own ``discount_given`` entry with no
    money movement anywhere.
 3. **Voiding is symmetric** — voiding a payment takes its discount with it,
    voiding a discount puts the amount back on what is owed, and both remove
    the row from Accounts.

Run with:
    HDC_BOOTSTRAP_ADMIN_PASSWORD='Admin@1234' python -m pytest tests/test_tool_rental_discount.py -q
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

os.environ.setdefault('HDC_ENV', 'test')
os.environ.setdefault('HDC_SECRET_KEY', 'unit-test-secret')
os.environ.setdefault('HDC_BOOTSTRAP_ADMIN_PASSWORD', 'Admin@1234')

from hdc.app import create_app                                     # noqa: E402
from hdc.extensions import db                                      # noqa: E402
from hdc.models.accounts import Account, AccountTransaction        # noqa: E402
from hdc.models.projects import Project                            # noqa: E402
from hdc.models.tool_rental import (                               # noqa: E402
    Tool, ToolRental, ToolRentalDiscount, ToolRentalItem, ToolRentalPayment,
)
from hdc.services.accounts import _account_balance                 # noqa: E402
from hdc.services.tool_rental import (                             # noqa: E402
    recalc_rental_totals, record_tool_rental_discount, rental_discount_rows,
    void_discounts_for_payment, void_tool_rental_discount,
)
from hdc.utils.dates import _pkt_today                             # noqa: E402

ADMIN_PASSWORD = os.environ['HDC_BOOTSTRAP_ADMIN_PASSWORD']


class ToolRentalDiscountTestCase(unittest.TestCase):
    """One isolated app + database per test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='hdc-tool-discount-test-')
        self.db_path = os.path.join(self.tmp, 'test.db')
        self.app = create_app({'HDC_DB_PATH': self.db_path,
                               'HDC_INSTANCE_DIR': self.tmp})
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()
        r = self.client.get('/hdc/login')
        token = self._csrf(r.get_data(as_text=True))
        self.client.post('/hdc/login', data={
            'username': 'admin', 'password': ADMIN_PASSWORD, '_csrf_token': token,
        }, follow_redirects=True)
        self.site = Project(name='Site A', project_code='P-A', client='Owner A',
                            location='Karachi', contract_type='lump_sum',
                            owner_lump_sum=1_000_000)
        db.session.add(self.site)
        db.session.commit()
        self.tool = self._make_tool('TOOL-0001', 'Grinder', 10)
        self.rental = self._rent([(self.tool, 2, 5_000.0)], customer_name='Bilal')
        self.cash = self._cash_account()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _csrf(html):
        import re
        m = re.search(r'name="_csrf_token" value="([^"]+)"', html or '')
        return m.group(1) if m else ''

    def _make_tool(self, code, name, qty, rate=500.0, cost=10_000.0):
        tool = Tool(tool_code=code, name=name, unit='pcs', total_quantity=qty,
                    purchase_cost=cost, rental_rate_per_day=rate,
                    condition='good', status='active', is_void=False)
        db.session.add(tool)
        db.session.commit()
        return tool

    def _rent(self, pairs, project=None, customer_name=None):
        code = f'RENT-{(ToolRental.query.count() + 1):05d}'
        rental = ToolRental(
            rental_code=code,
            renter_type='internal' if project else 'external',
            project_id=project.id if project else None,
            customer_name=customer_name,
            rental_date=_pkt_today(),
            billing_type='fixed_fee',
            status='active',
            payment_status='unpaid',
        )
        db.session.add(rental)
        db.session.flush()
        for tool, qty, rate in pairs:
            db.session.add(ToolRentalItem(
                rental_id=rental.id, tool_id=tool.id, qty_rented=qty,
                qty_returned=0.0, qty_pending=qty, rate=rate,
                amount=qty * rate))
        db.session.commit()
        recalc_rental_totals(rental.id)
        db.session.commit()
        return rental

    def _cash_account(self):
        acc = Account.query.filter_by(type='company').first()
        if acc is None:
            acc = Account(name='Company Cash', type='company',
                          opening_balance=0.0, status='active')
            db.session.add(acc)
            db.session.commit()
        return acc

    def _post_payment(self, amount, discount=None, **extra):
        data = {'payment_date': _pkt_today().isoformat(),
                'amount': amount,
                'received_to_account_id': self.cash.id,
                'payment_mode': 'cash'}
        if discount is not None:
            data['discount'] = discount
        data.update(extra)
        data['_csrf_token'] = self._csrf_payload_token()
        return self.client.post(
            f'/hdc/tool-rental/{self.rental.id}/payment',
            data=data, follow_redirects=True)

    def _csrf_payload_token(self):
        with self.client.session_transaction() as sess:
            return sess.get('_csrf_token', '')

    def _cust_account(self):
        name = (self.rental.customer_name or '').strip()
        return Account.query.filter(
            db.func.lower(Account.name) == name.lower()).first()

    # ------------------------------------------------------------ tests
    def test_rental_starts_with_no_discount(self):
        self.assertEqual(float(self.rental.total_amount or 0), 10_000.0)
        self.assertEqual(float(self.rental.total_discount or 0), 0.0)
        self.assertEqual(float(self.rental.total_pending_amount), 10_000.0)
        self.assertEqual(rental_discount_rows(self.rental.id), [])

    def test_standalone_discount_reduces_outstanding_without_cash(self):
        cash_before = _account_balance(self.cash.id)
        row, ok, msg = record_tool_rental_discount(
            self.rental.id, 2_000.0, reason='goodwill', commit=True)
        self.assertTrue(ok, msg)
        self.assertIsNotNone(row)

        self.rental = db.session.get(ToolRental, self.rental.id)
        self.assertEqual(float(self.rental.total_discount or 0), 2_000.0)
        self.assertEqual(float(self.rental.total_paid or 0), 0.0)
        self.assertEqual(float(self.rental.total_pending_amount), 8_000.0)
        # No money moved: the cash account is untouched.
        self.assertAlmostEqual(_account_balance(self.cash.id), cash_before, places=2)

    def test_discount_posts_its_own_accounts_entry(self):
        _row, ok, msg = record_tool_rental_discount(
            self.rental.id, 1_500.0, reason='negotiated', commit=True)
        self.assertTrue(ok, msg)
        txn = (AccountTransaction.query
               .filter(AccountTransaction.source_type.like('tool_rental_discount%'),
                       AccountTransaction.category == 'discount',
                       AccountTransaction.type == 'discount_given')
               .first())
        self.assertIsNotNone(txn, 'discount must be visible in the accounts ledger')
        self.assertAlmostEqual(float(txn.amount or 0), 1_500.0, places=2)
        self.assertFalse(txn.is_void)
        # The customer's own account carries the concession, cash does not.
        cust = self._cust_account()
        self.assertIsNotNone(cust)
        self.assertEqual(int(txn.from_account_id or 0), int(cust.id))

    def test_payment_with_discount_clears_both_legs(self):
        resp = self._post_payment(7_000.0, discount=3_000.0,
                                  discount_reason='long_term')
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True)[:400])

        rental = db.session.get(ToolRental, self.rental.id)
        self.assertAlmostEqual(float(rental.total_paid or 0), 7_000.0, places=2)
        self.assertAlmostEqual(float(rental.total_discount or 0), 3_000.0, places=2)
        self.assertAlmostEqual(float(rental.total_pending_amount), 0.0, places=2)
        self.assertEqual(rental.payment_status, 'paid')

        # Only the cash leg reached the cash account.
        self.assertAlmostEqual(_account_balance(self.cash.id), 7_000.0, places=2)

        pay = (ToolRentalPayment.query
               .filter_by(rental_id=rental.id).order_by(ToolRentalPayment.id.desc())
               .first())
        self.assertAlmostEqual(float(pay.discount or 0), 3_000.0, places=2)
        self.assertEqual(len(rental_discount_rows(rental.id)), 1)
        self.assertEqual(rental_discount_rows(rental.id)[0].payment_id, pay.id)

    def test_discount_cannot_exceed_outstanding(self):
        _row, ok, msg = record_tool_rental_discount(
            self.rental.id, 50_000.0, commit=True)
        self.assertFalse(ok)
        self.assertIn('exceeds', (msg or '').lower())
        self.assertEqual(ToolRentalDiscount.query.count(), 0)

    def test_cash_plus_discount_cannot_exceed_the_bill(self):
        resp = self._post_payment(9_000.0, discount=3_000.0)
        self.assertEqual(resp.status_code, 200)
        # The whole post is rejected, so nothing was written at all.
        rental = db.session.get(ToolRental, self.rental.id)
        self.assertAlmostEqual(float(rental.total_paid or 0), 0.0, places=2)
        self.assertAlmostEqual(float(rental.total_discount or 0), 0.0, places=2)
        self.assertEqual(ToolRentalDiscount.query.count(), 0)

    def test_no_charge_rental_cannot_be_discounted(self):
        self.rental.billing_type = 'no_charge'
        db.session.commit()
        _row, ok, msg = record_tool_rental_discount(
            self.rental.id, 500.0, commit=True)
        self.assertFalse(ok)
        self.assertIn('no charge', (msg or '').lower())

    def test_void_discount_puts_the_amount_back(self):
        row, ok, _msg = record_tool_rental_discount(
            self.rental.id, 4_000.0, commit=True)
        self.assertTrue(ok)
        _row, ok2, msg2 = void_tool_rental_discount(row.id, reason='mistake', commit=True)
        self.assertTrue(ok2, msg2)

        rental = db.session.get(ToolRental, self.rental.id)
        self.assertAlmostEqual(float(rental.total_discount or 0), 0.0, places=2)
        self.assertAlmostEqual(float(rental.total_pending_amount), 10_000.0, places=2)
        txn = (AccountTransaction.query
               .filter(AccountTransaction.source_type.like('tool_rental_discount%'),
                       AccountTransaction.source_id == row.id)
               .first())
        self.assertTrue(txn.is_void)

    def test_voiding_a_payment_voids_its_discount(self):
        self._post_payment(6_000.0, discount=2_000.0)
        rental = db.session.get(ToolRental, self.rental.id)
        pay = (ToolRentalPayment.query
               .filter_by(rental_id=rental.id).order_by(ToolRentalPayment.id.desc())
               .first())
        self.assertEqual(float(pay.discount or 0), 2_000.0)

        pay.is_void = True
        pay.void_reason = 'bounced'
        pay.voided_at = _pkt_today()
        void_discounts_for_payment(pay.id, reason='bounced')
        recalc_rental_totals(rental.id)
        db.session.commit()

        rental = db.session.get(ToolRental, self.rental.id)
        self.assertAlmostEqual(float(rental.total_paid or 0), 0.0, places=2)
        self.assertAlmostEqual(float(rental.total_discount or 0), 0.0, places=2)
        self.assertAlmostEqual(float(rental.total_pending_amount), 10_000.0, places=2)
        for row in rental_discount_rows(rental.id):
            self.assertTrue(row.is_void)
            txn = (AccountTransaction.query
                   .filter(AccountTransaction.source_type.like('tool_rental_discount%'),
                           AccountTransaction.source_id == row.id)
                   .first())
            self.assertTrue(txn.is_void)

    def test_void_discount_http_route(self):
        _row, ok, _msg = record_tool_rental_discount(
            self.rental.id, 1_000.0, commit=True)
        self.assertTrue(ok)
        row_id = _row.id
        with self.client.session_transaction() as sess:
            token = sess.get('_csrf_token', '')
        resp = self.client.post(f'/hdc/tool-rental/discount/{row_id}/void',
                                data={'_csrf_token': token},
                                follow_redirects=True)
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True)[:400])
        self.assertTrue(db.session.get(ToolRentalDiscount, row_id).is_void)

    def test_discount_reason_label_is_human_readable(self):
        row, ok, _msg = record_tool_rental_discount(
            self.rental.id, 250.0, reason='long_term', commit=True)
        self.assertTrue(ok)
        self.assertEqual(row.reason_label, 'Long-term / repeat customer')
        row2, _ok2, _m2 = record_tool_rental_discount(
            self.rental.id, 250.0, reason='not-a-real-reason', commit=True)
        self.assertEqual(row2.reason_label, 'Other')

    def test_discount_code_is_sequential(self):
        r1, _ok1, _m1 = record_tool_rental_discount(self.rental.id, 100.0, commit=True)
        r2, _ok2, _m2 = record_tool_rental_discount(self.rental.id, 100.0, commit=True)
        self.assertEqual(r1.discount_code, 'DISC-00001')
        self.assertEqual(r2.discount_code, 'DISC-00002')

    def test_ledger_type_is_registered(self):
        """``discount_given`` must exist in the ledger vocabulary, or posting
        it would be refused by the payload validator."""
        from hdc.services.accounts import (
            _ACCOUNT_INTENT_DEFAULT_RULES, _ACCOUNT_TXN_CATEGORIES,
            _ACCOUNT_TXN_TYPE_DEFAULT_CATEGORY, _ACCOUNT_TXN_TYPES,
        )
        self.assertIn('discount_given', _ACCOUNT_TXN_TYPES)
        self.assertIn('discount', _ACCOUNT_TXN_CATEGORIES)
        self.assertEqual(_ACCOUNT_TXN_TYPE_DEFAULT_CATEGORY['discount_given'], 'discount')
        self.assertIn('discount_given', _ACCOUNT_INTENT_DEFAULT_RULES)


if __name__ == '__main__':
    unittest.main()
