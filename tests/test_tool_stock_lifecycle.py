#!/usr/bin/env python3
"""Tests for the HDC Tools stock life-cycle (purchase / scrap / categories) and
for the Settings wipe actually clearing the Tools section.

Covers:
  - adding a tool creates an opening-stock purchase + movement log
  - buying more stock raises Total Qty Owned and is audited
  - scrapping lowers Total Qty Owned, writes the cost off and is audited
  - scrap of rented-out qty is refused (the balance must stay true)
  - categories can be created inline, renamed and deleted (only when unused)
  - the ledger / KPIs / JSON feed carry purchased + scrapped numbers
  - inventory search finds a tool by the site it is sitting on
  - Settings > Wipe clears every tool table (it used to leave them behind)

Run with:
    HDC_BOOTSTRAP_ADMIN_PASSWORD='Admin@1234' python -m pytest tests/test_tool_stock_lifecycle.py -q
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

from hdc.app import create_app                                    # noqa: E402
from hdc.core.admin import _WIPE_TARGETS, _wipe_selected_targets  # noqa: E402
from hdc.extensions import db                                     # noqa: E402
from hdc.models.projects import Project                           # noqa: E402
from hdc.models.tool_rental import (                              # noqa: E402
    Tool, ToolCategory, ToolMovementLog, ToolPurchase, ToolRental, ToolScrap,
)
from hdc.services.tool_rental import (                            # noqa: E402
    record_tool_purchase, record_tool_scrap, tool_kpis, tool_purchases, tool_scraps,
)
from hdc.services.tool_tracking import (                          # noqa: E402
    inventory_rows, tool_ledger, tools_reconciliation,
)
from hdc.utils.dates import _pkt_today                            # noqa: E402

ADMIN_PASSWORD = os.environ['HDC_BOOTSTRAP_ADMIN_PASSWORD']


class ToolStockLifecycleTestCase(unittest.TestCase):
    """One isolated app + database per test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='hdc-tool-stock-test-')
        self.db_path = os.path.join(self.tmp, 'test.db')
        self.app = create_app({'HDC_DB_PATH': self.db_path,
                               'HDC_INSTANCE_DIR': self.tmp})
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()
        self._login()
        self.site = Project(name='Site A', project_code='P-A', client='Owner A',
                            location='Karachi', contract_type='lump_sum',
                            owner_lump_sum=1_000_000)
        db.session.add(self.site)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ helpers
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

    def _make_tool(self, code, name, qty, rate=500.0, cost=10_000.0, category=None):
        tool = Tool(tool_code=code, name=name, unit='pcs',
                    category_id=category.id if category else None,
                    total_quantity=qty, purchase_cost=cost,
                    rental_rate_per_day=rate, condition='good',
                    status='active', is_void=False)
        db.session.add(tool)
        db.session.commit()
        return tool

    def _post_inventory(self, payload):
        data = dict(payload)
        data['_csrf_token'] = self._token()
        return self.client.post('/hdc/tool-rental/inventory', data=data,
                                follow_redirects=True)

    def _rent(self, tool_qty_pairs, project=None, customer_name=None):
        code = f'RENT-{(ToolRental.query.count() + 1):05d}'
        rental = ToolRental(rental_code=code, renter_type='internal' if project else 'external',
                            project_id=project.id if project else None,
                            customer_name=customer_name,
                            rental_date=_pkt_today(), billing_type='per_day',
                            status='active', payment_status='unpaid', is_void=False)
        db.session.add(rental)
        db.session.flush()
        for tool, qty in tool_qty_pairs:
            db.session.add(_rental_item(rental.id, tool.id, qty,
                                        rate=float(tool.rental_rate_per_day or 0)))
        rental.total_rented_qty = sum(q for _, q in tool_qty_pairs)
        db.session.commit()
        return rental

    # ------------------------------------------------------------ tests
    def test_add_tool_records_opening_purchase_and_movement(self):
        r = self._post_inventory({
            'action': 'add_tool', 'name': 'Concrete Vibrator', 'total_quantity': '12',
            'purchase_cost': '45000', 'rental_rate_per_day': '1200', 'unit': 'pcs',
            'supplier': 'Karachi Tools House', 'purchase_reference': 'BILL-77',
        })
        self.assertEqual(r.status_code, 200)
        tool = Tool.query.filter_by(name='Concrete Vibrator').first()
        self.assertIsNotNone(tool)
        self.assertEqual(float(tool.total_quantity), 12.0)

        purchase = ToolPurchase.query.filter_by(tool_id=tool.id).first()
        self.assertIsNotNone(purchase)
        self.assertTrue(purchase.is_opening_stock)
        self.assertEqual(float(purchase.qty), 12.0)
        self.assertEqual(float(purchase.total_cost), 540000.0)
        self.assertEqual(purchase.supplier, 'Karachi Tools House')

        log = ToolMovementLog.query.filter_by(tool_id=tool.id).first()
        self.assertEqual(log.movement_type, 'purchase_in')
        self.assertEqual(float(log.qty), 12.0)

    def test_add_tool_with_zero_qty_needs_no_purchase(self):
        self._post_inventory({'action': 'add_tool', 'name': 'Spare Part',
                              'total_quantity': '0'})
        tool = Tool.query.filter_by(name='Spare Part').first()
        self.assertEqual(float(tool.total_quantity), 0.0)
        self.assertEqual(ToolPurchase.query.filter_by(tool_id=tool.id).count(), 0)

    def test_purchase_adds_stock_and_audits_it(self):
        tool = self._make_tool('TOOL-0001', 'Grinder', 8, cost=9000)
        ok, msg, purchase = record_tool_purchase(tool.id, qty=5, unit_cost=9500,
                                                 supplier='Lahore Tools',
                                                 reference='INV-9')
        self.assertTrue(ok, msg)
        self.assertEqual(float(tool.total_quantity), 13.0)
        self.assertEqual(float(tool.purchase_cost), 9500.0)   # update_cost default
        self.assertEqual(float(purchase.total_cost), 47500.0)
        # purchased_qty counts *recorded* purchases, so the 8 opening pieces
        # (entered straight into the tool row) are not part of it
        self.assertEqual(float(tool.purchased_qty), 5.0)
        self.assertEqual(float(tool.purchase_value), 47500.0)

        log = (ToolMovementLog.query
               .filter_by(tool_id=tool.id, movement_type='purchase_in')
               .order_by(ToolMovementLog.id.desc()).first())
        self.assertEqual(float(log.qty), 5.0)
        self.assertEqual(log.from_location_label, 'Lahore Tools')

        # still balances: 13 owned, none out
        recon = tools_reconciliation()
        self.assertEqual(recon['owned'], 13.0)
        self.assertEqual(recon['in_store'], 13.0)
        self.assertTrue(recon['balanced'])

    def test_purchase_can_keep_the_old_unit_cost(self):
        tool = self._make_tool('TOOL-0001', 'Grinder', 8, cost=9000)
        ok, _, purchase = record_tool_purchase(tool.id, qty=2, unit_cost=11000,
                                               update_cost=False)
        self.assertTrue(ok)
        self.assertEqual(float(tool.purchase_cost), 9000.0)
        self.assertEqual(float(purchase.total_cost), 22000.0)
        self.assertEqual(float(tool.total_quantity), 10.0)

    def test_purchase_rejects_bad_input(self):
        tool = self._make_tool('TOOL-0001', 'Grinder', 8)
        for qty in (0, -3):
            ok, msg, _ = record_tool_purchase(tool.id, qty=qty)
            self.assertFalse(ok)
            self.assertIn('greater than 0', msg)
        self.assertEqual(float(tool.total_quantity), 8.0)
        ok, msg, _ = record_tool_purchase(999999, qty=1)
        self.assertFalse(ok)

    def test_scrap_reduces_stock_and_writes_off_value(self):
        tool = self._make_tool('TOOL-0001', 'Rebar Bender', 6, cost=5000)
        ok, msg, scrap = record_tool_scrap(tool.id, qty=2, reason='damaged',
                                           notes='motor burnt')
        self.assertTrue(ok, msg)
        self.assertEqual(float(tool.total_quantity), 4.0)
        self.assertEqual(float(scrap.value_written_off), 10000.0)
        self.assertEqual(scrap.reason_label, 'Damaged beyond repair')
        self.assertEqual(float(tool.scrapped_qty), 2.0)
        self.assertEqual(float(tool.scrapped_value), 10000.0)

        log = ToolMovementLog.query.filter_by(tool_id=tool.id,
                                             movement_type='scrap_out').first()
        self.assertEqual(float(log.qty), 2.0)
        self.assertEqual(log.to_location_label, 'Scrap / Discard')

        recon = tools_reconciliation()
        self.assertEqual(recon['owned'], 4.0)
        self.assertEqual(recon['in_store'], 4.0)
        self.assertTrue(recon['balanced'])

    def test_scrap_refuses_more_than_what_is_in_store(self):
        tool = self._make_tool('TOOL-0001', 'Vibrator', 10)
        self._rent([(tool, 6)], project=self.site)
        ok, msg, scrap = record_tool_scrap(tool.id, qty=8, reason='lost')
        self.assertFalse(ok)
        self.assertIn('rented out', msg)
        self.assertIsNone(scrap)
        self.assertEqual(float(tool.total_quantity), 10.0)

        # exactly what is in the store is fine
        ok, _, _ = record_tool_scrap(tool.id, qty=4, reason='worn_out')
        self.assertTrue(ok)
        self.assertEqual(float(tool.total_quantity), 6.0)
        self.assertTrue(tools_reconciliation()['balanced'])

    def test_scrap_unknown_reason_falls_back_to_other(self):
        tool = self._make_tool('TOOL-0001', 'Vibrator', 3)
        ok, _, scrap = record_tool_scrap(tool.id, qty=1, reason='banana')
        self.assertTrue(ok)
        self.assertEqual(scrap.reason, 'other')

    def test_purchase_then_rent_then_return_keeps_balance(self):
        tool = self._make_tool('TOOL-0001', 'Prop', 100)
        record_tool_purchase(tool.id, qty=50, unit_cost=3200)
        self.assertEqual(float(tool.total_quantity), 150.0)
        rental = self._rent([(tool, 120)], project=self.site)
        recon = tools_reconciliation()
        self.assertEqual(recon['owned'], 150.0)
        self.assertEqual(recon['own_project'], 120.0)
        self.assertEqual(recon['in_store'], 30.0)
        self.assertTrue(recon['balanced'])

        # return 70, scrap 5 of what is back
        item = rental.items[0]
        item.qty_returned = 70.0
        item.qty_pending = 50.0
        rental.total_returned_qty = 70.0
        db.session.commit()
        ok, _, _ = record_tool_scrap(tool.id, qty=5, reason='damaged')
        self.assertTrue(ok)
        self.assertEqual(float(tool.total_quantity), 145.0)
        recon = tools_reconciliation()
        self.assertEqual(recon['owned'], 145.0)
        self.assertEqual(recon['own_project'], 50.0)
        self.assertEqual(recon['in_store'], 95.0)
        self.assertTrue(recon['balanced'])

    def test_purchase_route_through_the_ui(self):
        tool = self._make_tool('TOOL-0001', 'Grinder', 4, cost=9000)
        r = self.client.post(f'/hdc/tool-rental/inventory/{tool.id}/purchase', data={
            '_csrf_token': self._token(), 'qty': '6', 'unit_cost': '9800',
            'update_cost': '1', 'supplier': 'Bilal Hardware',
            'purchase_date': _pkt_today().isoformat(), 'reference': 'INV-12',
        }, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(float(tool.total_quantity), 10.0)
        self.assertEqual(float(tool.purchase_cost), 9800.0)
        purchase = ToolPurchase.query.filter_by(tool_id=tool.id).first()
        self.assertEqual(purchase.supplier, 'Bilal Hardware')
        self.assertEqual(purchase.reference, 'INV-12')

    def test_generic_purchase_route_takes_tool_from_the_form(self):
        tool = self._make_tool('TOOL-0001', 'Grinder', 4)
        r = self.client.post('/hdc/tool-rental/inventory/purchase', data={
            '_csrf_token': self._token(), 'tool_id': str(tool.id), 'qty': '3',
        }, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(float(tool.total_quantity), 7.0)

    def test_scrap_route_through_the_ui(self):
        tool = self._make_tool('TOOL-0001', 'Rebar Bender', 6, cost=5000)
        r = self.client.post(f'/hdc/tool-rental/inventory/{tool.id}/scrap', data={
            '_csrf_token': self._token(), 'qty': '2', 'reason': 'damaged',
            'scrap_date': _pkt_today().isoformat(), 'notes': 'burnt motor',
        }, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(float(tool.total_quantity), 4.0)
        scrap = ToolScrap.query.filter_by(tool_id=tool.id).first()
        self.assertEqual(scrap.notes, 'burnt motor')
        self.assertEqual(float(scrap.value_written_off), 10000.0)

    def test_categories_create_rename_delete(self):
        # inline creation while adding a tool
        r = self._post_inventory({'action': 'add_tool', 'name': 'Welder',
                                  'category_id': '', 'new_category': 'Power Tools',
                                  'total_quantity': '2'})
        self.assertEqual(r.status_code, 200)
        cat = ToolCategory.query.filter_by(name='Power Tools').first()
        self.assertIsNotNone(cat)
        tool = Tool.query.filter_by(name='Welder').first()
        self.assertEqual(int(tool.category_id), int(cat.id))

        # standalone creation is idempotent and re-selects the category
        r = self.client.post('/hdc/tool-rental/inventory', data={
            '_csrf_token': self._token(), 'action': 'add_category',
            'category_name': 'Power Tools'}, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(ToolCategory.query.filter_by(name='Power Tools').count(), 1)

        # rename
        r = self.client.post(f'/hdc/tool-rental/category/{cat.id}/edit', data={
            '_csrf_token': self._token(), 'name': 'Power Tools & Machines'},
            follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(cat.name, 'Power Tools & Machines')

        # duplicate rename refused
        other = ToolCategory(name='Hand Tools', active_status=True)
        db.session.add(other)
        db.session.commit()
        r = self.client.post(f'/hdc/tool-rental/category/{cat.id}/edit', data={
            '_csrf_token': self._token(), 'name': 'hand tools'},
            follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(cat.name, 'Power Tools & Machines')

        # delete refused while a tool uses it
        r = self.client.post(f'/hdc/tool-rental/category/{cat.id}/delete', data={
            '_csrf_token': self._token()}, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertIsNotNone(db.session.get(ToolCategory, cat.id))

        # unused category can go
        r = self.client.post(f'/hdc/tool-rental/category/{other.id}/delete', data={
            '_csrf_token': self._token()}, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(db.session.get(ToolCategory, other.id))

    def test_ledger_and_kpis_carry_purchase_and_scrap_numbers(self):
        tool = self._make_tool('TOOL-0001', 'Vibrator', 10, cost=45000)
        record_tool_purchase(tool.id, qty=5, unit_cost=45000)
        record_tool_scrap(tool.id, qty=3, reason='damaged')

        row = next(r for r in tool_ledger()['tools'] if int(r['tool_id']) == int(tool.id))
        self.assertEqual(row['purchased_qty'], 5.0)
        self.assertEqual(row['purchased_value'], 225000.0)
        self.assertEqual(row['scrapped_qty'], 3.0)
        self.assertEqual(row['scrapped_value'], 135000.0)
        self.assertEqual(row['owned_qty'], 12.0)

        totals = tool_ledger()['totals']
        self.assertEqual(totals['purchased_qty'], 5.0)
        self.assertEqual(totals['scrapped_qty'], 3.0)

        kpis = tool_kpis()
        self.assertEqual(kpis['purchased_qty'], 5.0)
        self.assertEqual(kpis['purchase_value'], 225000.0)
        self.assertEqual(kpis['scrapped_qty'], 3.0)
        self.assertEqual(kpis['scrapped_value'], 135000.0)

    def test_json_feed_reports_stock_lifecycle(self):
        tool = self._make_tool('TOOL-0001', 'Vibrator', 10, cost=45000)
        record_tool_purchase(tool.id, qty=5, unit_cost=45000)
        record_tool_scrap(tool.id, qty=2, reason='lost')
        r = self.client.get('/hdc/api/tool-rental/dashboard')
        self.assertEqual(r.status_code, 200)
        payload = r.get_json()
        entry = next(t for t in payload['tools'] if t['tool_id'] == tool.id)
        self.assertEqual(entry['purchased_qty'], 5.0)
        self.assertEqual(entry['scrapped_qty'], 2.0)

    def test_inventory_search_finds_a_tool_by_the_site_it_sits_on(self):
        tool = self._make_tool('TOOL-0001', 'Vibrator', 10)
        self._rent([(tool, 4)], project=self.site)

        rows = inventory_rows(term='Site A')
        self.assertEqual([int(r['tool_id']) for r in rows], [int(tool.id)])

        rows = inventory_rows(term='vibrator')
        self.assertEqual(len(rows), 1)
        rows = inventory_rows(term='TOOL-0001')
        self.assertEqual(len(rows), 1)
        self.assertEqual(inventory_rows(term='nothing-like-this'), [])

    def test_inventory_category_search_fields_keep_named_selects(self):
        cat = ToolCategory(name='Power Tools', active_status=True)
        db.session.add(cat)
        db.session.commit()
        tool = self._make_tool('TOOL-0001', 'Vibrator', 1, category=cat)
        r = self.client.get(f'/hdc/tool-rental/inventory?category_id={cat.id}&new_category={cat.id}')
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)

        # The widget searches the categories, but the original select still posts
        # the id (or empty for No Category / All Categories) in each form.
        for input_id, select_id in (
            ('toolCategoryInput', 'toolCategorySelect'),
            ('inventoryCategoryInput', 'inventoryCategorySelect'),
            (f'editToolCategoryInput{tool.id}', f'editToolCategorySelect{tool.id}'),
        ):
            self.assertIn(f'id="{input_id}"', html)
            self.assertRegex(html, rf'<select name="category_id" id="{select_id}"')
            self.assertRegex(html, rf'id="{select_id}"[^>]*>\s*<option value=""')
        self.assertIn('value="__new">＋ Create new category…', html)
        self.assertIn('id="toolNewCategory"', html)
        self.assertIn("HDCComboList.attach('toolCategoryInput', 'toolCategorySelect'", html)
        self.assertIn("HDCComboList.attach('inventoryCategoryInput', 'inventoryCategorySelect'", html)
        self.assertIn("querySelectorAll('.js-edit-tool-category-input')", html)
        for select_id in ('toolCategorySelect', 'inventoryCategorySelect',
                          f'editToolCategorySelect{tool.id}'):
            self.assertIsNotNone(re.search(
                rf'id="{select_id}"[^>]*>.*?value="{cat.id}" selected', html, re.S
            ), select_id)

    def test_inventory_page_renders_with_stock_controls(self):
        tool = self._make_tool('TOOL-0001', 'Vibrator', 10)
        record_tool_purchase(tool.id, qty=4, unit_cost=45000)
        record_tool_scrap(tool.id, qty=1, reason='damaged')
        r = self.client.get('/hdc/tool-rental/inventory')
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn('Purchase Stock', html)
        self.assertIn('Scrap', html)
        self.assertIn('Add New Tool / Category', html)

        # per-tool position page shows both registers
        r = self.client.get(f'/hdc/tool-rental/tool/{tool.id}')
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn('Purchases', html)
        self.assertIn('Scrap', html)
        self.assertIn('PUR-TOOL-', html)
        self.assertIn('SCRAP-', html)

    def test_reports_page_lists_purchases_and_scraps(self):
        tool = self._make_tool('TOOL-0001', 'Vibrator', 10, cost=45000)
        record_tool_purchase(tool.id, qty=4, unit_cost=45000, supplier='Ali Traders')
        record_tool_scrap(tool.id, qty=1, reason='damaged')
        r = self.client.get('/hdc/tool-rental/reports')
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn('Purchases — Stock In', html)
        self.assertIn('Scrap — Discarded / Lost', html)
        self.assertIn('Ali Traders', html)

        self.assertEqual(len(tool_purchases(tool_id=tool.id)), 1)
        self.assertEqual(len(tool_scraps(tool_id=tool.id)), 1)

    # ------------------------------------------------------- wipe behaviour
    def test_wipe_has_a_tools_group_covering_every_table(self):
        self.assertIn('tools', _WIPE_TARGETS)
        tables = set(_WIPE_TARGETS['tools']['tables'])
        for expected in ('hdc_tool', 'hdc_tool_category', 'hdc_tool_purchase',
                         'hdc_tool_scrap', 'hdc_tool_rental', 'hdc_tool_rental_item',
                         'hdc_tool_rental_return', 'hdc_tool_rental_payment',
                         'hdc_tool_rental_transfer', 'hdc_tool_movement_log'):
            self.assertIn(expected, tables)

    def test_wiping_tools_clears_every_tool_table(self):
        """The reported bug: wiping data left the Tools section behind."""
        cat = ToolCategory(name='Power Tools', active_status=True)
        db.session.add(cat)
        db.session.commit()
        tool = self._make_tool('TOOL-0001', 'Vibrator', 10, category=cat)
        record_tool_purchase(tool.id, qty=5, unit_cost=45000)
        record_tool_scrap(tool.id, qty=2, reason='damaged')
        self._rent([(tool, 3)], project=self.site)
        self.assertEqual(Tool.query.count(), 1)
        self.assertEqual(ToolPurchase.query.count(), 1)
        self.assertEqual(ToolScrap.query.count(), 1)

        info = _wipe_selected_targets(['tools'])
        self.assertIn('tools', info['targets'])

        self.assertEqual(Tool.query.count(), 0)
        self.assertEqual(ToolCategory.query.count(), 0)
        self.assertEqual(ToolPurchase.query.count(), 0)
        self.assertEqual(ToolScrap.query.count(), 0)
        self.assertEqual(ToolRental.query.count(), 0)
        self.assertEqual(ToolMovementLog.query.count(), 0)

        # the section is genuinely empty afterwards, not just hidden
        self.assertEqual(tool_ledger()['totals']['owned_qty'], 0.0)
        self.assertTrue(tools_reconciliation()['balanced'])

        # and the app still serves every tools page
        for url in ('/hdc/tool-rental/dashboard', '/hdc/tool-rental/inventory',
                    '/hdc/tool-rental', '/hdc/tool-rental/reports',
                    '/hdc/tool-rental/tracking'):
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_wiping_tools_voids_the_linked_ledger_income(self):
        """Rent already posted to Accounts must not survive as orphan income."""
        from hdc.models.accounts import Account, AccountTransaction
        from hdc.services.tool_rental import post_tool_rental_payment_to_accounts

        cash = Account(name='Test Wipe Cash', type='cash', opening_balance=0,
                       status='active', is_void=False)
        db.session.add(cash)
        db.session.commit()
        tool = self._make_tool('TOOL-0001', 'Vibrator', 10, rate=1000)
        rental = self._rent([(tool, 2)], project=self.site)

        from hdc.models.tool_rental import ToolRentalPayment
        payment = ToolRentalPayment(rental_id=rental.id, amount=2000,
                                    payment_mode='cash', received_to_account_id=cash.id)
        db.session.add(payment)
        db.session.flush()
        ok, msg, txns = post_tool_rental_payment_to_accounts(payment, rental=rental)
        self.assertTrue(ok, msg)
        self.assertTrue(txns)
        db.session.commit()

        _wipe_selected_targets(['tools'])
        live = AccountTransaction.query.filter_by(is_void=False).count()
        self.assertEqual(live, 0)
        self.assertEqual(AccountTransaction.query.count(), len(txns))

    def test_wiping_accounts_drops_tool_links_but_keeps_tools(self):
        from hdc.models.accounts import Account, AccountTransaction
        from hdc.models.tool_rental import ToolRentalAccountTxn, ToolRentalPayment
        from hdc.services.tool_rental import post_tool_rental_payment_to_accounts

        cash = Account(name='Test Wipe Cash', type='cash', opening_balance=0,
                       status='active', is_void=False)
        db.session.add(cash)
        db.session.commit()
        tool = self._make_tool('TOOL-0001', 'Vibrator', 10, rate=1000)
        rental = self._rent([(tool, 2)], project=self.site)
        payment = ToolRentalPayment(rental_id=rental.id, amount=2000,
                                    payment_mode='cash', received_to_account_id=cash.id)
        db.session.add(payment)
        db.session.flush()
        ok, _, _ = post_tool_rental_payment_to_accounts(payment, rental=rental)
        self.assertTrue(ok)
        db.session.commit()

        _wipe_selected_targets(['accounts'])
        # bootstrap re-creates its default accounts, but ours is gone for good
        self.assertEqual(Account.query.filter_by(name='Test Wipe Cash').count(), 0)
        self.assertEqual(AccountTransaction.query.count(), 0)
        # the tool data survives, only the dead account link is dropped
        self.assertEqual(Tool.query.count(), 1)
        self.assertEqual(ToolRentalPayment.query.count(), 1)
        self.assertEqual(ToolRentalAccountTxn.query.count(), 0)
        self.assertIsNone(ToolRentalPayment.query.first().received_to_account_id)


def _rental_item(rental_id, tool_id, qty, rate):
    from hdc.models.tool_rental import ToolRentalItem
    return ToolRentalItem(rental_id=rental_id, tool_id=tool_id, qty_rented=qty,
                          qty_returned=0.0, qty_pending=qty, rate=rate,
                          amount=qty * rate)


if __name__ == '__main__':
    unittest.main()
