#!/usr/bin/env python3
"""The Tools New Rental page as one data-driven transaction form.

``/hdc/tool-rental/new`` offers New Rental, Transfer Rental, Return Tools and
Rent Payment from one dropdown.  The dropdown, headings, submit labels, the
endpoint each type posts to and the rentals each type may pick are all read
from ``hdc/services/tool_txn_types.py``.  These cases cover:

  1. the registry itself (pure data, no database);
  2. the rendered form: every type in the dropdown, the registry handed to the
     page script, the rental pickers listing only qualifying rentals;
  3. the endpoints the form posts to, driven with the same field names the
     page sends, so a return or payment from the one form moves the same money
     and tools as the rental page does.

Run with:
    HDC_BOOTSTRAP_ADMIN_PASSWORD='Admin@1234' \\
        python -m unittest tests.test_tool_txn_form -v
"""

from __future__ import annotations

import json
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

from hdc.app import create_app                                    # noqa: E402
from hdc.extensions import db                                     # noqa: E402
from hdc.models.accounts import Account                           # noqa: E402
from hdc.models.projects import Project, Stage                    # noqa: E402
from hdc.models.tool_rental import (                              # noqa: E402
    Tool, ToolCategory, ToolRental, ToolRentalItem,
)
from hdc.services.tool_txn_types import (                         # noqa: E402
    ELIGIBLE_PENDING_AMOUNT, ELIGIBLE_PENDING_TOOLS, TOOL_TXN_TYPES,
    rental_eligible, txn_type,
)
from hdc.utils.dates import _pkt_today                            # noqa: E402

ADMIN_PASSWORD = os.environ['HDC_BOOTSTRAP_ADMIN_PASSWORD']


class TxnRegistryTests(unittest.TestCase):
    """Pure data: no app, no database."""

    def test_every_transaction_is_registered_once(self):
        keys = [t['key'] for t in TOOL_TXN_TYPES]
        self.assertEqual(keys, ['new_rental', 'transfer', 'return', 'payment'])
        self.assertEqual(len(keys), len(set(keys)))

    def test_only_create_and_transfer_post_to_the_create_endpoint(self):
        create = {t['key'] for t in TOOL_TXN_TYPES
                  if t['endpoint'] == 'hdc_tool_rental_create'}
        self.assertEqual(create, {'new_rental', 'transfer'})

    def test_rental_bound_types_declare_their_picker_rule(self):
        for entry in TOOL_TXN_TYPES:
            if entry['needs_rental']:
                self.assertIn(entry['eligible_for'],
                              (ELIGIBLE_PENDING_TOOLS, ELIGIBLE_PENDING_AMOUNT))
            else:
                self.assertIsNone(entry['eligible_for'])

    def test_lookup_returns_entry_or_none(self):
        self.assertEqual(txn_type('return')['endpoint'], 'hdc_tool_rental_return')
        self.assertIsNone(txn_type('scrap'))

    def test_eligibility_rules(self):
        self.assertTrue(rental_eligible(ELIGIBLE_PENDING_TOOLS,
                                        pending_tools=2, pending_amount=0))
        self.assertFalse(rental_eligible(ELIGIBLE_PENDING_TOOLS,
                                         pending_tools=0, pending_amount=500))
        self.assertTrue(rental_eligible(ELIGIBLE_PENDING_AMOUNT,
                                        pending_tools=0, pending_amount=120))
        self.assertFalse(rental_eligible(ELIGIBLE_PENDING_AMOUNT,
                                         pending_tools=3, pending_amount=0))
        # A no-charge rental owes nothing, so it is never a payment target.
        self.assertFalse(rental_eligible(ELIGIBLE_PENDING_AMOUNT,
                                         pending_tools=3, pending_amount=900,
                                         billing_type='no_charge'))
        self.assertFalse(rental_eligible(None, pending_tools=3, pending_amount=3))


class TxnFormTestCase(unittest.TestCase):
    """One isolated app + database for the rendered form and its endpoints."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='hdc-tool-txn-form-')
        self.app = create_app({
            'HDC_DB_PATH': os.path.join(self.tmp, 'test.db'),
            'HDC_INSTANCE_DIR': self.tmp,
            'TESTING': True,
        })
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()
        self._login()
        self.site = Project(name='Site A', project_code='P-A', client='Owner A',
                            location='Karachi', contract_type='lump_sum',
                            owner_lump_sum=1_000_000)
        db.session.add(self.site)
        db.session.flush()
        self.stage = Stage(project_id=self.site.id, name='Foundation',
                           contract_basis='Lump Sum', lump_sum_value=100_000)
        db.session.add(self.stage)
        self.cat = ToolCategory(name='Power Tools', active_status=True)
        db.session.add(self.cat)
        db.session.commit()
        self.bank = Account(name='Smoke Bank', type='bank', status='active')
        db.session.add(self.bank)
        self.vibrator = self._make_tool('TOOL-0001', 'Vibrator', 20)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ helpers
    def _login(self):
        page = self.client.get('/hdc/login')
        token = self._csrf(page.get_data(as_text=True))
        self.client.post('/hdc/login', data={
            'username': 'admin', 'password': ADMIN_PASSWORD, '_csrf_token': token,
        }, follow_redirects=True)

    @staticmethod
    def _csrf(html):
        match = re.search(r'name="_csrf_token" value="([^"]+)"', html or '')
        return match.group(1) if match else ''

    def _token(self):
        with self.client.session_transaction() as sess:
            return sess.get('_csrf_token', '')

    def _make_tool(self, code, name, qty, rate=500.0):
        tool = Tool(tool_code=code, name=name, unit='pcs',
                    category_id=self.cat.id, total_quantity=qty,
                    purchase_cost=10_000.0, rental_rate_per_day=rate,
                    condition='good', status='active', is_void=False)
        db.session.add(tool)
        db.session.commit()
        return tool

    def _create_rental(self, qty=4, billing='per_day'):
        resp = self.client.post('/hdc/tool-rental/create', data={
            '_csrf_token': self._token(),
            'txn_type': 'new_rental',
            'renter_type': 'internal',
            'project_id': str(self.site.id),
            'stage_id': str(self.stage.id),
            'billing_type': billing,
            'rental_date': _pkt_today().isoformat(),
            'tool_id[]': [str(self.vibrator.id)],
            'qty[]': [str(qty)],
            'rate[]': ['500'],
        }, follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        match = re.search(r'created=(\d+)', resp.headers.get('Location', ''))
        self.assertIsNotNone(match)
        return ToolRental.query.get(int(match.group(1)))

    def _form_html(self):
        resp = self.client.get('/hdc/tool-rental/new')
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    @staticmethod
    def _registry_from_page(html):
        match = re.search(r'const TOOL_TXN_TYPES = (\[.*?\]);\n', html, re.S)
        if match is None:
            raise AssertionError('TOOL_TXN_TYPES not rendered on the page')
        return json.loads(match.group(1))

    @staticmethod
    def _picker_option_values(html, select_id):
        start = html.index(f'<select id="{select_id}"')
        end = html.index('</select>', start)
        return re.findall(r'<option value="(\d+)"', html[start:end])

    # ------------------------------------------------------------ the form
    def test_dropdown_offers_every_transaction_from_the_registry(self):
        html = self._form_html()
        for entry in TOOL_TXN_TYPES:
            self.assertIn(f'<option value="{entry["key"]}"', html)
            self.assertIn(f'>{entry["label"]}</option>', html)

    def test_page_script_gets_the_registry_with_the_right_actions(self):
        html = self._form_html()
        registry = {t['key']: t for t in self._registry_from_page(html)}
        self.assertEqual(set(registry), {'new_rental', 'transfer', 'return', 'payment'})
        self.assertEqual(registry['new_rental']['action'], '/hdc/tool-rental/create')
        self.assertEqual(registry['transfer']['action'], '/hdc/tool-rental/create')
        self.assertEqual(registry['return']['action'],
                         '/hdc/tool-rental/__RENTAL_ID__/return')
        self.assertEqual(registry['payment']['action'],
                         '/hdc/tool-rental/__RENTAL_ID__/payment')
        self.assertEqual(registry['return']['submit_label'], 'Record Return')

    def test_each_transaction_has_its_own_panel_and_mode_tags(self):
        html = self._form_html()
        self.assertIn('id="rentalTxnPanel" data-txn-modes="return payment"', html)
        self.assertIn('data-txn-modes="return"', html)
        self.assertIn('data-txn-modes="payment"', html)
        self.assertIn('id="transferSourceField" data-txn-modes="transfer"', html)
        self.assertIn('id="newRentalTools" data-txn-modes="new_rental"', html)
        self.assertIn('id="rentalSubmitRow" data-txn-modes="new_rental transfer return payment"', html)

    def test_return_picker_lists_only_rentals_with_tools_out(self):
        rental = self._create_rental(qty=4)
        other = self._create_rental(qty=2)
        html = self._form_html()
        picks = self._picker_option_values(html, 'returnRentalSelect')
        self.assertIn(str(rental.id), picks)
        self.assertIn(str(other.id), picks)

        # Take everything back from one rental: it must leave the return picker.
        self._post_return(rental, qty=4, payment='no_payment')
        html = self._form_html()
        picks = self._picker_option_values(html, 'returnRentalSelect')
        self.assertNotIn(str(rental.id), picks)
        self.assertIn(str(other.id), picks)

    def test_payment_picker_lists_only_rentals_that_owe_rent(self):
        billed = self._create_rental(qty=2, billing='per_day')
        html = self._form_html()
        self.assertIn(str(billed.id), self._picker_option_values(html, 'paymentRentalSelect'))
        # Pending amount is carried on each option for the page's balance line.
        self.assertRegex(html, rf'<option value="{billed.id}"[^>]*data-amount="[0-9.]+"')

        # A no-charge rental never appears in the payment picker.
        free = self._create_rental(qty=1, billing='no_charge')
        html = self._form_html()
        self.assertNotIn(str(free.id), self._picker_option_values(html, 'paymentRentalSelect'))

    def test_new_rental_form_still_renders_its_controls(self):
        html = self._form_html()
        self.assertIn('id="createRentalForm"', html)
        self.assertIn('name="tool_id[]"', html)
        self.assertIn('name="rental_date"', html)

    # ---------------------------------------- endpoints the one form posts to
    def _post_return(self, rental, qty, payment='no_payment', amount_paid=None):
        item = ToolRentalItem.query.filter_by(rental_id=rental.id).first()
        data = {
            '_csrf_token': self._token(),
            'return_date': _pkt_today().isoformat(),
            'return_condition': 'partial',
            'payment_condition': payment,
            'payment_mode': 'cash',
            'rental_item_id[]': [str(item.id)],
            'qty_returned[]': [str(qty)],
            'condition_notes[]': [''],
        }
        if amount_paid is not None:
            data['amount_paid'] = str(amount_paid)
            data['received_to_account_id'] = str(self.bank.id)
        resp = self.client.post(f'/hdc/tool-rental/{rental.id}/return', data=data,
                                follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        db.session.expire_all()
        return ToolRental.query.get(rental.id)

    def test_return_from_the_form_moves_the_tools_back(self):
        rental = self._create_rental(qty=4)
        rental = self._post_return(rental, qty=3, payment='no_payment')
        self.assertAlmostEqual(float(rental.total_pending_tools), 1.0)

    def test_payment_from_the_form_records_cash_into_the_chosen_account(self):
        rental = self._create_rental(qty=2)
        due = float(rental.total_pending_amount)
        self.assertGreater(due, 0)
        resp = self.client.post(f'/hdc/tool-rental/{rental.id}/payment', data={
            '_csrf_token': self._token(),
            'payment_date': _pkt_today().isoformat(),
            'amount': f'{due:.2f}',
            'received_to_account_id': str(self.bank.id),
            'payment_mode': 'bank',
            'reference': 'TXN-1',
        }, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        db.session.expire_all()
        rental = ToolRental.query.get(rental.id)
        self.assertAlmostEqual(float(rental.total_pending_amount), 0.0, places=2)


if __name__ == '__main__':
    unittest.main()
