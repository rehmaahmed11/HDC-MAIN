#!/usr/bin/env python3
"""The uniform HDC Tool Transaction form — one form, every transaction.

``/hdc/tool-rental/transaction`` records every equipment transaction from one
page: rent out, issue to an own site, transfer between sites or customers,
partial / full / store returns, rent payments, credit adjustments, damage &
loss, and send / return from maintenance.  These cases drive the form the way
the page posts (``txn_source=uniform`` + a generated ``txn_reference``) and
assert what actually moved:

  1. the registry itself (pure data, no database);
  2. the rendered page: every transaction type, the page config, the panels;
  3. each transaction type against an isolated app + database;
  4. the rules the task pins: no internal external-income, no payment-driven
     movement, partial vs full returns, unavailable stock refused, duplicate
     submissions handled, balance kept (owned = in_store + out).

Run with:
    HDC_BOOTSTRAP_ADMIN_PASSWORD='Admin@1234' \\
        python -m unittest tests.test_tool_txn_uniform -v
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

from flask import url_for                                          # noqa: E402

from hdc.app import create_app                                     # noqa: E402
from hdc.extensions import db                                      # noqa: E402
from hdc.models.accounts import Account, AccountTransaction        # noqa: E402
from hdc.models.projects import Project, Stage                     # noqa: E402
from hdc.models.tool_rental import (                               # noqa: E402
    Tool, ToolCategory, ToolRental, ToolRentalItem, ToolRentalPayment,
    ToolRentalReturn, ToolScrap, ToolSerial, ToolMovementLog,
)
from hdc.services.tool_rental import (                             # noqa: E402
    ensure_all_tools_serials, ensure_tool_serials,
)
from hdc.services.tool_txn_entry import (                          # noqa: E402
    MAINTENANCE_LABEL, new_txn_reference, txn_reference_seen,
)
from hdc.services.tool_txn_types import (                          # noqa: E402
    KIND_ADJUSTMENT, KIND_CREATE, KIND_DAMAGE_LOSS, KIND_MAINTENANCE_IN,
    KIND_MAINTENANCE_OUT, KIND_PAYMENT, KIND_RETURN, KIND_TRANSFER,
    MONEY_KINDS, PHYSICAL_KINDS, UNIFORM_TXN_TYPES, uniform_txn_type,
)
from hdc.services.tool_tracking import tool_ledger                 # noqa: E402
from hdc.utils.dates import _pkt_today                             # noqa: E402

ADMIN_PASSWORD = os.environ['HDC_BOOTSTRAP_ADMIN_PASSWORD']

#: Every transaction type the task requires the one form to support.
REQUIRED_KEYS = (
    'rent_out_customer', 'issue_own_site', 'transfer_site', 'transfer_customer',
    'partial_return', 'full_return', 'return_to_store', 'rental_payment',
    'credit_adjustment', 'damage_loss', 'maintenance_out', 'maintenance_in',
)


class UniformRegistryTests(unittest.TestCase):
    """Pure data: no app, no database."""

    def test_registry_covers_every_required_transaction(self):
        keys = [t['key'] for t in UNIFORM_TXN_TYPES]
        self.assertEqual(keys, list(REQUIRED_KEYS))
        self.assertEqual(len(keys), len(set(keys)))

    def test_every_type_declares_its_controller_contract(self):
        valid_kinds = (KIND_CREATE, KIND_TRANSFER, KIND_RETURN, KIND_PAYMENT,
                       KIND_ADJUSTMENT, KIND_DAMAGE_LOSS, KIND_MAINTENANCE_OUT,
                       KIND_MAINTENANCE_IN)
        for entry in UNIFORM_TXN_TYPES:
            self.assertIn(entry['kind'], valid_kinds, entry['key'])
            self.assertTrue(entry['panels'], entry['key'])
            self.assertTrue(entry['submit_label'])
            self.assertTrue(entry['help'])
            self.assertTrue(entry['confirm'])
            self.assertTrue(entry['icon'])

    def test_rental_bound_types_pick_from_rentals_and_nothing_else_does(self):
        for entry in UNIFORM_TXN_TYPES:
            if entry['needs_rental']:
                self.assertIn(entry['kind'], (KIND_RETURN, KIND_PAYMENT, KIND_ADJUSTMENT),
                              entry['key'])
            else:
                self.assertIsNone(entry['eligible_for'], entry['key'])

    def test_money_and_physical_kinds_are_disjoint(self):
        for entry in UNIFORM_TXN_TYPES:
            in_money = entry['kind'] in MONEY_KINDS
            in_physical = entry['kind'] in PHYSICAL_KINDS
            self.assertNotEqual(in_money, in_physical, entry['key'])

    def test_lookup_returns_entry_or_none(self):
        self.assertEqual(uniform_txn_type('damage_loss')['kind'], KIND_DAMAGE_LOSS)
        self.assertIsNone(uniform_txn_type('nope'))


class UniformFormTestCase(unittest.TestCase):
    """One isolated app + database for the rendered form and its endpoints."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='hdc-tool-txn-uniform-')
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

        self.site_a = Project(name='Site A', project_code='P-A', client='Owner A',
                              location='Karachi', contract_type='lump_sum',
                              owner_lump_sum=1_000_000)
        self.site_b = Project(name='Site B', project_code='P-B', client='Owner B',
                              location='Lahore', contract_type='lump_sum',
                              owner_lump_sum=1_000_000)
        db.session.add_all([self.site_a, self.site_b])
        db.session.flush()
        self.stage_a = Stage(project_id=self.site_a.id, name='Foundation',
                             contract_basis='Lump Sum', lump_sum_value=100_000)
        db.session.add(self.stage_a)
        self.cat = ToolCategory(name='Power Tools', active_status=True)
        db.session.add(self.cat)
        db.session.commit()
        self.bank = Account(name='Txn Bank', type='bank', status='active')
        db.session.add(self.bank)
        self.vibrator = self._make_tool('TOOL-1001', 'Vibrator', 6, 500.0)
        self.drill = self._make_tool('TOOL-1002', 'Drill', 4, 300.0)
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

    def _make_tool(self, code, name, qty, rate):
        tool = Tool(tool_code=code, name=name, unit='pcs', category_id=self.cat.id,
                    total_quantity=qty, purchase_cost=10_000.0,
                    rental_rate_per_day=rate, condition='good', status='active',
                    is_void=False)
        db.session.add(tool)
        db.session.commit()
        return tool

    def _post(self, url, data, follow=False):
        payload = dict(data)
        payload['_csrf_token'] = self._token()
        payload.setdefault('txn_source', 'uniform')
        return self.client.post(url, data=payload, follow_redirects=follow)

    def _uniform(self, txn_type, extra, follow=True):
        """Post the way the uniform page does: page action, source + reference.

        The endpoint is taken from the registry entry — the same contract the
        page script uses — so a rental-bound transaction lands on its own
        route and a physical/financial one on its handler.
        """
        entry = uniform_txn_type(txn_type)
        self.assertIsNotNone(entry, txn_type)
        with self.app.test_request_context():
            if entry['needs_rental']:
                rental_id = extra.get('rental_id')
                self.assertIsNotNone(rental_id, f'{txn_type} needs a rental')
                url = url_for(entry['endpoint'], rental_id=int(rental_id))
            else:
                url = url_for(entry['endpoint'])
        data = {
            'txn_type': txn_type,
            'transaction_date': _pkt_today().isoformat(),
            'txn_reference': new_txn_reference(),
            'responsible_person': 'Office Test',
        }
        data.update(extra)
        return self._post(url, data, follow=follow)

    def _ledger_row(self, tool):
        rows = {int(r['tool_id']): r for r in tool_ledger()['tools']}
        return rows[int(tool.id)]

    def _rent(self, tool, qty, renter_type='internal', billing='per_day',
              customer=None, project=None, txn_type=None, follow=True):
        data = {
            'renter_type': renter_type,
            'billing_type': billing,
            'rental_date': _pkt_today().isoformat(),
            'tool_id[]': [str(tool.id)],
            'qty[]': [str(qty)],
            'rate[]': [str(tool.rental_rate_per_day)],
        }
        if renter_type == 'internal':
            data['project_id'] = str((project or self.site_a).id)
        else:
            data['customer_name'] = customer or 'Outside Customer'
        response = self._uniform(txn_type or (
            'rent_out_customer' if renter_type == 'external' else 'issue_own_site'),
            data, follow=follow)
        return response

    def _rental_by_code(self, code_fragment):
        return (ToolRental.query
                .filter(ToolRental.rental_code.like(f'%{code_fragment}%'))
                .order_by(ToolRental.id.desc()).first())

    def _latest_rental(self):
        return ToolRental.query.order_by(ToolRental.id.desc()).first()

    def _return_qty(self, rental, item, qty, return_type='partial', reference=None,
                    payment=None):
        data = {
            'txn_type': 'partial_return',
            'transport': 'uniform',
            'txn_reference': reference or new_txn_reference(),
            'return_condition': return_type,
            'return_date': _pkt_today().isoformat(),
            'payment_condition': payment or 'no_payment',
            'rental_item_id[]': [str(item.id)],
            'qty_returned[]': [str(qty)],
            'condition_notes[]': [''],
        }
        return self._post(f'/hdc/tool-rental/{rental.id}/return', data, follow=True)

    # ------------------------------------------------------------ the page
    def test_page_offers_every_transaction_type(self):
        response = self.client.get('/hdc/tool-rental/transaction')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        for entry in UNIFORM_TXN_TYPES:
            self.assertIn(f'<option value="{entry["key"]}"', html)
            self.assertIn(entry['label'], html)

    def test_page_config_carries_types_panels_and_reference(self):
        html = self.client.get('/hdc/tool-rental/transaction').get_data(as_text=True)
        match = re.search(
            r'<script id="toolTxnConfig" type="application/json">(.*?)</script>',
            html, re.S)
        self.assertIsNotNone(match, 'page config JSON missing')
        config = json.loads(match.group(1))
        self.assertEqual([t['key'] for t in config['types']], list(REQUIRED_KEYS))
        self.assertTrue(config['txnReference'].startswith('HDC-TX-'))
        self.assertEqual(config['storeLabel'], 'Warehouse / Store')
        self.assertEqual(config['maintenanceLabel'], MAINTENANCE_LABEL)
        for entry in UNIFORM_TXN_TYPES:
            self.assertIn(entry['kind'], {t['kind'] for t in config['types']})
        self.assertIn('data-txn-panels="lines"', html)
        self.assertIn('id="toolTxnForm"', html)

    def test_page_is_linked_from_the_tools_subnav_and_dashboard(self):
        nav = self.client.get('/hdc/tool-rental/dashboard').get_data(as_text=True)
        self.assertIn('/hdc/tool-rental/transaction', nav)
        self.assertIn('New Transaction', nav)

    def test_legacy_new_rental_page_still_works(self):
        html = self.client.get('/hdc/tool-rental/new').get_data(as_text=True)
        self.assertEqual(html.count('id="txnType"'), 1)
        legacy = [t for t in re.findall(r'<option value="([a-z_]+)"', html)
                  if t in ('new_rental', 'transfer', 'return', 'payment')]
        self.assertTrue(legacy)

    def test_existing_tools_tabs_still_render(self):
        for url in ('/hdc/tool-rental', '/hdc/tool-rental/dashboard',
                    '/hdc/tool-rental/inventory', '/hdc/tool-rental/new',
                    '/hdc/tool-rental/transaction'):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)
            body = response.get_data(as_text=True)
            self.assertEqual(body.count('</html>'), 1, url)

    # ------------------------------------------------ physical transactions
    def test_rent_out_to_customer_issues_and_records_the_holder(self):
        self._rent(self.vibrator, 4, renter_type='external', customer='Sky Builders')
        rental = self._latest_rental()
        self.assertEqual(rental.renter_type, 'external')
        self.assertEqual(rental.customer_name, 'Sky Builders')
        item = ToolRentalItem.query.filter_by(rental_id=rental.id).first()
        self.assertAlmostEqual(float(item.qty_pending), 4.0)
        self.assertAlmostEqual(self._ledger_row(self.vibrator)['out_qty'], 4.0)
        log = (ToolMovementLog.query.filter_by(rental_id=rental.id)
               .order_by(ToolMovementLog.id.desc()).first())
        self.assertEqual(log.movement_type, 'rental_out')

    def test_issue_to_own_site_creates_no_external_income(self):
        ledger_before = self._ledger_row(self.vibrator)
        self._rent(self.vibrator, 3, renter_type='internal', billing='per_day')
        rental = self._latest_rental()
        self.assertEqual(rental.renter_type, 'internal')
        # The physical move happened...
        ledger_after = self._ledger_row(self.vibrator)
        self.assertAlmostEqual(ledger_after['own_project_qty'],
                               ledger_before['own_project_qty'] + 3.0)
        self.assertAlmostEqual(ledger_after['in_store_qty'],
                               ledger_before['in_store_qty'] - 3.0)
        # ...and no money was raised behind the operator's back.
        self.assertEqual(ToolRentalPayment.query.count(), 0)
        self.assertEqual(AccountTransaction.query.count(), 0)

    def test_transfer_between_sites_moves_the_recorded_location(self):
        self._rent(self.vibrator, 4, renter_type='internal', project=self.site_a)
        rental = self._latest_rental()
        item = ToolRentalItem.query.filter_by(rental_id=rental.id).first()
        source_key = f'{rental.id}|own_project|{self.site_a.id}|0|'
        response = self._uniform('transfer_site', {
            'renter_type': 'internal',
            'project_id': str(self.site_b.id),
            'billing_type': 'per_day',
            'rental_date': _pkt_today().isoformat(),
            'transfer_selection_enabled': '1',
            'source_rental_id[]': [source_key],
            'transfer_item_id[]': [str(item.id)],
            f'transfer_qty_{item.id}': '4',
            'settle_choice': 'add_credit',
        })
        self.assertEqual(response.status_code, 200)
        row = self._ledger_row(self.vibrator)
        self.assertAlmostEqual(row['own_project_qty'], 4.0)
        labels = {h['label'] for h in row['holdings']}
        self.assertTrue(any('Site B' in label for label in labels), labels)
        new_rental = self._latest_rental()
        self.assertNotEqual(new_rental.id, rental.id)
        self.assertEqual(new_rental.project_id, self.site_b.id)

    def test_transfer_between_customers_records_the_new_holder(self):
        self._rent(self.vibrator, 2, renter_type='external', customer='First Customer')
        rental = self._latest_rental()
        item = ToolRentalItem.query.filter_by(rental_id=rental.id).first()
        source_key = f'{rental.id}|customer|0|0|First%20Customer'
        self._uniform('transfer_customer', {
            'renter_type': 'external',
            'customer_name': 'Second Customer',
            'billing_type': 'per_day',
            'rental_date': _pkt_today().isoformat(),
            'transfer_selection_enabled': '1',
            'source_rental_id[]': [source_key],
            'transfer_item_id[]': [str(item.id)],
            f'transfer_qty_{item.id}': '2',
            'settle_choice': 'add_credit',
        })
        new_rental = self._latest_rental()
        self.assertEqual(new_rental.customer_name, 'Second Customer')
        self.assertAlmostEqual(self._ledger_row(self.vibrator)['customer_qty'], 2.0)

    def test_partial_return_keeps_the_remaining_outstanding(self):
        self._rent(self.vibrator, 5, renter_type='external', customer='Part Return Co')
        rental = self._latest_rental()
        item = ToolRentalItem.query.filter_by(rental_id=rental.id).first()
        self._return_qty(rental, item, 2)
        db.session.expire_all()
        item = ToolRentalItem.query.get(item.id)
        self.assertAlmostEqual(float(item.qty_pending), 3.0)
        self.assertAlmostEqual(float(item.qty_returned), 2.0)
        self.assertAlmostEqual(self._ledger_row(self.vibrator)['out_qty'], 3.0)
        self.assertAlmostEqual(self._ledger_row(self.vibrator)['in_store_qty'], 3.0)

    def test_full_return_closes_the_physical_issue(self):
        self._rent(self.vibrator, 4, renter_type='internal')
        rental = self._latest_rental()
        item = ToolRentalItem.query.filter_by(rental_id=rental.id).first()
        self._return_qty(rental, item, 0, return_type='full')
        db.session.expire_all()
        item = ToolRentalItem.query.get(item.id)
        self.assertAlmostEqual(float(item.qty_pending), 0.0)
        self.assertAlmostEqual(self._ledger_row(self.vibrator)['out_qty'], 0.0)
        self.assertAlmostEqual(self._ledger_row(self.vibrator)['in_store_qty'], 6.0)

    def test_return_to_store_posts_no_payment(self):
        self._rent(self.vibrator, 3, renter_type='external', customer='Store Return Co')
        rental = self._latest_rental()
        item = ToolRentalItem.query.filter_by(rental_id=rental.id).first()
        self._return_qty(rental, item, 3, return_type='full')
        self.assertEqual(ToolRentalPayment.query.count(), 0)
        self.assertAlmostEqual(self._ledger_row(self.vibrator)['in_store_qty'], 6.0)

    # ------------------------------------------------------ money transactions
    def test_rental_payment_updates_balance_and_moves_no_tool(self):
        self._rent(self.drill, 2, renter_type='external', customer='Paying Customer')
        rental = self._latest_rental()
        before = self._ledger_row(self.drill)
        due = float(rental.total_pending_amount)
        self.assertGreater(due, 0)
        reference = new_txn_reference()
        self._uniform('rental_payment', {
            'rental_id': str(rental.id),
            'amount': f'{due:.2f}',
            'payment_date': _pkt_today().isoformat(),
            'payment_mode': 'bank',
            'received_to_account_id': str(self.bank.id),
            'reference': 'SLIP-1',
            'txn_reference': reference,
        })
        db.session.expire_all()
        rental = ToolRental.query.get(rental.id)
        self.assertAlmostEqual(float(rental.total_pending_amount), 0.0, places=2)
        self.assertEqual(ToolRentalPayment.query.count(), 1)
        self.assertTrue(
            AccountTransaction.query.filter(
                AccountTransaction.source_type.like('tool_rental_payment%')).count() >= 1)
        after = self._ledger_row(self.drill)
        self.assertAlmostEqual(after['out_qty'], before['out_qty'])
        self.assertAlmostEqual(after['in_store_qty'], before['in_store_qty'])

    def test_credit_adjustment_reduces_outstanding_without_cash(self):
        self._rent(self.drill, 2, renter_type='external', customer='Credit Customer')
        rental = self._latest_rental()
        due = float(rental.total_pending_amount)
        self._uniform('credit_adjustment', {
            'rental_id': str(rental.id),
            'amount': f'{due:.2f}',
            'discount_date': _pkt_today().isoformat(),
            'reason': 'goodwill',
            'notes': 'Waived off the balance',
        })
        db.session.expire_all()
        rental = ToolRental.query.get(rental.id)
        self.assertAlmostEqual(float(rental.total_pending_amount), 0.0, places=2)
        self.assertEqual(ToolRentalPayment.query.count(), 0)

    # ------------------------------------------------- damage / loss / service
    def test_damage_loss_from_store_writes_off_stock(self):
        self._uniform('damage_loss', {
            'damage_source': 'store',
            'tool_id': str(self.vibrator.id),
            'qty': '2',
            'damage_reason': 'damaged',
            'notes': 'Dropped off the truck',
        })
        db.session.expire_all()
        tool = Tool.query.get(self.vibrator.id)
        self.assertAlmostEqual(float(tool.total_quantity), 4.0)
        self.assertEqual(ToolScrap.query.count(), 1)
        log = (ToolMovementLog.query.filter_by(tool_id=tool.id,
                                               movement_type='scrap_out')
               .order_by(ToolMovementLog.id.desc()).first())
        self.assertIsNotNone(log)

    def test_damage_loss_from_a_holder_keeps_the_ledger_balanced(self):
        self._rent(self.vibrator, 4, renter_type='external', customer='Rough Site Co')
        rental = self._latest_rental()
        item = ToolRentalItem.query.filter_by(rental_id=rental.id).first()
        self._uniform('damage_loss', {
            'damage_source': 'rental',
            'rental_id': str(rental.id),
            'rental_item_id': str(item.id),
            'qty': '1',
            'damage_reason': 'lost',
        })
        db.session.expire_all()
        tool = Tool.query.get(self.vibrator.id)
        item = ToolRentalItem.query.get(item.id)
        self.assertAlmostEqual(float(tool.total_quantity), 5.0)
        self.assertAlmostEqual(float(item.qty_pending), 3.0)
        row = self._ledger_row(tool)
        self.assertAlmostEqual(row['owned_qty'], 5.0)
        self.assertAlmostEqual(row['out_qty'], 3.0)
        self.assertAlmostEqual(row['in_store_qty'], 2.0)
        self.assertFalse(row['unaccounted'])
        self.assertAlmostEqual(row['variance'], 0.0)
        self.assertEqual(ToolScrap.query.count(), 1)

    def test_send_and_return_from_maintenance_keeps_ownership(self):
        ensure_tool_serials(tool=self.vibrator, commit=True)
        pieces = [s.id for s in self.vibrator.in_store_serials][:2]
        self.assertEqual(len(pieces), 2, 'fixture must have serial markings')
        self._uniform('maintenance_out', {
            'tool_id': str(self.vibrator.id),
            'maintenance_serial_ids[]': [str(p) for p in pieces],
            'maintenance_reason': 'Service',
        })
        db.session.expire_all()
        tool = Tool.query.get(self.vibrator.id)
        self.assertAlmostEqual(float(tool.total_quantity), 6.0)
        sent = ToolSerial.query.filter(ToolSerial.id.in_(pieces)).all()
        self.assertTrue(all(s.status == 'maintenance' for s in sent))
        self.assertTrue(all(s.current_location_label == MAINTENANCE_LABEL for s in sent))
        self.assertTrue(all(s.is_in_store for s in sent))

        self._uniform('maintenance_in', {
            'maintenance_serial_ids[]': [str(p) for p in pieces],
            'maintenance_condition': 'good',
        })
        db.session.expire_all()
        back = ToolSerial.query.filter(ToolSerial.id.in_(pieces)).all()
        self.assertTrue(all(s.status == 'in_store' for s in back))
        self.assertTrue(all(s.condition == 'good' for s in back))
        self.assertAlmostEqual(
            float(Tool.query.get(self.vibrator.id).total_quantity), 6.0)

    # ------------------------------------------------------------- validation
    def test_over_return_is_rejected_without_writing_anything(self):
        self._rent(self.vibrator, 3, renter_type='external', customer='Over Return Co')
        rental = self._latest_rental()
        item = ToolRentalItem.query.filter_by(rental_id=rental.id).first()
        self._return_qty(rental, item, 99)
        self.assertEqual(ToolRentalReturn.query.count(), 0)
        db.session.expire_all()
        self.assertAlmostEqual(
            float(ToolRentalItem.query.get(item.id).qty_pending), 3.0)

    def test_unavailable_stock_cannot_be_issued(self):
        self._rent(self.drill, 4, renter_type='internal')
        rentals_before = ToolRental.query.count()
        self._rent(self.drill, 1, renter_type='external', customer='Too Many Co')
        self.assertEqual(ToolRental.query.count(), rentals_before)
        self.assertAlmostEqual(self._ledger_row(self.drill)['out_qty'], 4.0)

    def test_duplicate_submission_posts_only_once(self):
        self._rent(self.drill, 2, renter_type='external', customer='Double Click Co')
        rental = self._latest_rental()
        due = float(rental.total_pending_amount)
        reference = new_txn_reference()
        data = {
            'rental_id': str(rental.id),
            'amount': f'{due:.2f}',
            'payment_date': _pkt_today().isoformat(),
            'payment_mode': 'bank',
            'received_to_account_id': str(self.bank.id),
            'reference': 'SLIP-DUP',
            'txn_reference': reference,
        }
        self._uniform('rental_payment', data)
        self._uniform('rental_payment', dict(data))
        self.assertEqual(ToolRentalPayment.query.count(), 1)
        self.assertTrue(txn_reference_seen(reference))

    def test_duplicate_return_reference_posts_only_once(self):
        self._rent(self.vibrator, 3, renter_type='external', customer='Dup Return Co')
        rental = self._latest_rental()
        item = ToolRentalItem.query.filter_by(rental_id=rental.id).first()
        reference = new_txn_reference()
        self._return_qty(rental, item, 1, reference=reference)
        self._return_qty(rental, item, 1, reference=reference)
        self.assertEqual(ToolRentalReturn.query.count(), 1)
        db.session.expire_all()
        self.assertAlmostEqual(
            float(ToolRentalItem.query.get(item.id).qty_pending), 2.0)

    def test_empty_form_posts_nothing(self):
        response = self._post('/hdc/tool-rental/create', {'txn_type': 'rent_out_customer'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ToolRental.query.count(), 0)

    def test_reference_generator_never_reuses_a_recorded_number(self):
        self._rent(self.vibrator, 1, renter_type='internal')
        rental = self._latest_rental()
        match = re.search(r'\[Ref (HDC-TX-[0-9]{8}-[A-Z0-9]+)\]', rental.notes or '')
        self.assertIsNotNone(match, rental.notes)
        self.assertTrue(txn_reference_seen(match.group(1)))
        other = new_txn_reference()
        self.assertFalse(txn_reference_seen(other))
        self.assertNotEqual(match.group(1), other)


if __name__ == '__main__':
    unittest.main()
