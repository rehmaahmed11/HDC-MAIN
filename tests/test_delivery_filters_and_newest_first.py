"""Delivery / Material Usage registers: newest entry first, filters, pop-up.

Pins the six field requests written up in ``DELIVERY_USAGE_REGISTER_SPEC.md``:

1. newest entry on top of the Delivery register, the Supplier page and the
   Accounts Entries register (the last was already correct — this keeps it so);
2. the Pending Purchases pop-up on the Delivery page, including its site
   filter data;
3. the All Sites / All Stages quick buttons;
4. filters on both the Delivery register and the Material Usage log;
5. arranging either register by PO number.

Only disposable databases are used.  Run with:
    python -m unittest tests.test_delivery_filters_and_newest_first -v
"""
import re
import unittest
from datetime import timedelta

from qa_support import IsolatedAppTest

from hdc.extensions import db
from hdc.models.accounts import Account, AccountTransaction
from hdc.models.materials import (Delivery, MaterialV2, PurchaseV2, Supplier,
                                  SupplierLedger, UsageLogV2)
from hdc.models.projects import Project, Stage
from hdc.utils.dates import _pkt_now_naive

DELIVERY_URL = '/hdc/purchase-v2/delivered'
USAGE_URL = '/hdc/purchase-v2/usage'

# The delivery register slice of the page: the records table only, so the PO
# numbers rendered inside the pending-purchase pop-up cannot be mistaken for
# delivery rows.
REGISTER_START = '<!-- Delivery Records Table -->'
REGISTER_END = '<!-- Pending Purchases pop-up'


class DeliveryRegisterFilterTest(IsolatedAppTest):
    def setUp(self):
        super().setUp()
        with self.app.app_context():
            base = _pkt_now_naive()
            supplier = Supplier(name='QA Supplier')
            cement = MaterialV2(name='QA Cement', unit='BAG')
            steel = MaterialV2(name='QA Steel', unit='KG')
            db.session.add_all([supplier, cement, steel])
            db.session.flush()
            site_a = Project(project_code='QA-D1', name='QA Site A')
            site_b = Project(project_code='QA-D2', name='QA Site B')
            db.session.add_all([site_a, site_b])
            db.session.flush()
            stage_a = Stage(name='QA Slab A', project_id=site_a.id)
            stage_b = Stage(name='QA Slab B', project_id=site_b.id)
            db.session.add_all([stage_a, stage_b])
            db.session.flush()

            po_cement = PurchaseV2(
                supplier_id=supplier.id, material_id=cement.id, unit_price=100,
                quantity=100, total_amount=10000, payment_status='unpaid',
                date=(base - timedelta(days=10)).date(), is_void=False,
                created_at=base - timedelta(days=10),
                updated_at=base - timedelta(days=10))
            po_steel = PurchaseV2(
                supplier_id=supplier.id, material_id=steel.id, unit_price=200,
                quantity=50, total_amount=10000, payment_status='unpaid',
                date=(base - timedelta(days=5)).date(), is_void=False,
                created_at=base - timedelta(days=5),
                updated_at=base - timedelta(days=5))
            # Fully delivered — must not appear in the pending pop-up.
            po_done = PurchaseV2(
                supplier_id=supplier.id, material_id=cement.id, unit_price=90,
                quantity=10, total_amount=900, payment_status='paid',
                date=(base - timedelta(days=4)).date(), is_void=False,
                created_at=base - timedelta(days=4),
                updated_at=base - timedelta(days=4))
            db.session.add_all([po_cement, po_steel, po_done])
            db.session.flush()

            d_old = Delivery(purchase_id=po_cement.id, material_id=cement.id,
                             project_id=site_a.id, stage_id=stage_a.id, quantity=10,
                             date=(base - timedelta(days=9)).date(), is_void=False,
                             created_at=base - timedelta(days=9))
            d_mid = Delivery(purchase_id=po_cement.id, material_id=cement.id,
                             project_id=site_b.id, stage_id=stage_b.id, quantity=20,
                             date=(base - timedelta(days=1)).date(), is_void=False,
                             created_at=base - timedelta(days=1))
            d_new = Delivery(purchase_id=po_steel.id, material_id=steel.id,
                             project_id=site_a.id, stage_id=stage_a.id, quantity=5,
                             date=base.date(), is_void=False, created_at=base)
            d_full = Delivery(purchase_id=po_done.id, material_id=cement.id,
                              project_id=site_a.id, stage_id=stage_a.id, quantity=10,
                              date=(base - timedelta(days=3)).date(), is_void=False,
                              created_at=base - timedelta(days=3))
            db.session.add_all([d_old, d_mid, d_new, d_full])
            db.session.flush()

            u_old = UsageLogV2(purchase_id=po_cement.id, material_id=cement.id,
                               project_id=site_a.id, stage_id=stage_a.id, quantity=2,
                               cost=200, date=(base - timedelta(days=8)).date(),
                               is_void=False, created_at=base - timedelta(days=8))
            u_new = UsageLogV2(purchase_id=po_steel.id, material_id=steel.id,
                               project_id=site_a.id, stage_id=stage_a.id, quantity=1,
                               cost=200, date=base.date(), is_void=False,
                               created_at=base)
            u_site_b = UsageLogV2(purchase_id=po_cement.id, material_id=cement.id,
                                  project_id=site_b.id, stage_id=stage_b.id, quantity=3,
                                  cost=300, date=base.date(), is_void=False,
                                  created_at=base - timedelta(hours=1))
            db.session.add_all([u_old, u_new, u_site_b])
            db.session.commit()

            self.ids = {
                'supplier': supplier.id, 'cement': cement.id, 'steel': steel.id,
                'site_a': site_a.id, 'site_b': site_b.id,
                'stage_a': stage_a.id, 'stage_b': stage_b.id,
                'po_cement': po_cement.id, 'po_steel': po_steel.id,
                'po_done': po_done.id,
                'd_old': d_old.id, 'd_mid': d_mid.id, 'd_new': d_new.id,
                'd_full': d_full.id,
                'u_old': u_old.id, 'u_new': u_new.id, 'u_site_b': u_site_b.id,
            }

    # -- helpers -----------------------------------------------------------
    def get(self, path, **params):
        response = self.client.get(path, query_string=params or None)
        self.assertEqual(response.status_code, 200,
                         response.get_data(as_text=True)[:2000])
        return response.get_data(as_text=True)

    @staticmethod
    def register_ids(html):
        start = html.find(REGISTER_START)
        end = html.find(REGISTER_END, start)
        assert start != -1 and end != -1, 'delivery records table not found'
        return [int(m) for m in
                re.findall(r'<td class="text-muted small">#(\d+)</td>',
                           html[start:end])]

    @staticmethod
    def usage_ids(html):
        return [int(m) for m in
                re.findall(r'<td class="text-muted small">#(\d+)</td>', html)]

    # -- 1. newest entry first ---------------------------------------------
    def test_delivery_register_lists_newest_entry_first(self):
        html = self.get(DELIVERY_URL)
        ids = self.register_ids(html)
        self.assertEqual(ids, [self.ids['d_new'], self.ids['d_mid'],
                               self.ids['d_full'], self.ids['d_old']])
        self.assertIn('Newest entry first', html)
        # The register also states when each entry was keyed in.
        self.assertIn('<th>Recorded</th>', html)
        self.assertRegex(html, r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}')

    def test_supplier_page_lists_newest_purchase_and_ledger_entry_first(self):
        html = self.get(f"/hdc/purchase-v2/suppliers/{self.ids['supplier']}")
        purchase_block = html.split('Purchase History', 1)[1].split('Supplier Ledger', 1)[0]
        self.assertEqual(
            [int(m) for m in re.findall(r'#(\d+)</td>', purchase_block)],
            [self.ids['po_done'], self.ids['po_steel'], self.ids['po_cement']])
        ledger_block = html.split('Supplier Ledger', 1)[1]
        ledger_ids = [int(m) for m in re.findall(r'<td class="text-muted small">#(\d+)</td>',
                                                 ledger_block)]
        with self.app.app_context():
            chronological = [int(r.id) for r in
                             SupplierLedger.query
                             .filter_by(supplier_id=self.ids['supplier'], is_void=False)
                             .order_by(SupplierLedger.created_at.asc(), SupplierLedger.id.asc())
                             .all()]
        self.assertEqual(ledger_ids, list(reversed(chronological)))

    def test_accounts_entries_register_is_newest_first(self):
        # Already true before this change (services/accounts.py orders the
        # history by date desc, id desc) — pinned so it cannot regress.
        with self.app.app_context():
            base = _pkt_now_naive()
            account = Account(name='QA Ordering Cash', type='cash',
                              opening_balance=100000)
            db.session.add(account)
            db.session.flush()
            older = AccountTransaction(
                date=(base - timedelta(days=2)).date(), amount=100,
                type='expense_general', from_account_id=account.id,
                executed_by_account_id=account.id, category='expense',
                note='older entry', is_void=False, created_at=base - timedelta(days=2))
            newer = AccountTransaction(
                date=base.date(), amount=200, type='expense_general',
                from_account_id=account.id, executed_by_account_id=account.id,
                category='expense', note='newer entry', is_void=False,
                created_at=base)
            db.session.add_all([older, newer])
            db.session.commit()
            older_id, newer_id = older.id, newer.id
        html = self.get('/hdc/accounts/entries')
        rows = [int(m) for m in re.findall(r'<tr id="txn-(\d+)"', html)]
        self.assertIn(older_id, rows)
        self.assertIn(newer_id, rows)
        self.assertLess(rows.index(newer_id), rows.index(older_id))

    # -- 2. pending purchases pop-up ---------------------------------------
    def test_pending_purchase_popup_lists_owing_orders_with_site_data(self):
        html = self.get(DELIVERY_URL)
        self.assertIn('id="pendingPOModal"', html)
        self.assertIn('id="pendingPOBtn"', html)
        self.assertIn('Pending Purchases', html)

        body = html.split('id="pendingPOBody"', 1)[1]
        listed = [int(m) for m in re.findall(r'data-po-id="(\d+)"', body)]
        self.assertEqual(sorted(listed), sorted([self.ids['po_cement'], self.ids['po_steel']]))
        self.assertNotIn(self.ids['po_done'], listed)  # fully delivered

        cement_row = body.split(f'data-po-id="{self.ids["po_cement"]}"', 1)[1].split('</tr>', 1)[0]
        # 100 ordered − 30 delivered = 70 still pending, and both sites supplied.
        self.assertIn('data-pending-qty="70.0"', cement_row)
        self.assertEqual(sorted(cement_row.split('data-site-ids="', 1)[1].split('"', 1)[0].split(',')),
                         sorted([str(self.ids['site_a']), str(self.ids['site_b'])]))
        self.assertIn('QA Site A: 10.00', cement_row)
        self.assertIn('QA Site B: 20.00', cement_row)
        # One click fills the delivery form with that PO.
        self.assertIn(f'usePendingPO({self.ids["po_cement"]}, {self.ids["cement"]})', cement_row)

    def test_pending_purchase_popup_offers_the_site_filter(self):
        html = self.get(DELIVERY_URL)
        popup = html.split('id="pendingPOModal"', 1)[1].split('id="pendingPOBody"', 1)[0]
        self.assertIn('id="pendingSiteFilter"', popup)
        self.assertIn('>All Sites</option>', popup)
        self.assertIn(f'<option value="{self.ids["site_a"]}">QA Site A</option>', popup)
        self.assertIn(f'<option value="{self.ids["site_b"]}">QA Site B</option>', popup)
        self.assertIn('id="pendingMaterialFilter"', popup)
        self.assertIn('id="pendingSupplierFilter"', popup)
        self.assertIn('id="pendingSearch"', popup)
        self.assertIn('id="pendingPONoMatch"', html)

    # -- 3. All Sites / All Stages buttons ---------------------------------
    def test_all_sites_and_all_stages_controls_are_on_the_delivery_screen(self):
        html = self.get(DELIVERY_URL)
        self.assertIn('id="filterAllSitesBtn"', html)
        self.assertIn('id="filterAllStagesBtn"', html)
        self.assertIn('id="usageFilterAllSitesBtn"', self.get(USAGE_URL))
        self.assertIn('id="usageFilterAllStagesBtn"', self.get(USAGE_URL))
        for select_id in ('filterProjectSelect', 'filterStageSelect'):
            block = html.split(f'id="{select_id}"', 1)[1].split('</select>', 1)[0]
            self.assertIn('<option value="">All ', block)

    # -- 4. filters on both registers --------------------------------------
    def test_delivery_register_filters(self):
        ids = self.ids
        cases = [
            ({'po_number': ids['po_cement']}, [ids['d_mid'], ids['d_old']]),
            ({'material_id': ids['steel']}, [ids['d_new']]),
            ({'supplier_id': ids['supplier']},
             [ids['d_new'], ids['d_mid'], ids['d_full'], ids['d_old']]),
            ({'project_id': ids['site_a']}, [ids['d_new'], ids['d_full'], ids['d_old']]),
            ({'stage_id': ids['stage_b']}, [ids['d_mid']]),
            ({'project_id': ids['site_a'], 'material_id': ids['cement']},
             [ids['d_full'], ids['d_old']]),
        ]
        for params, expected in cases:
            with self.subTest(**params):
                self.assertEqual(self.register_ids(self.get(DELIVERY_URL, **params)), expected)

    def test_delivery_register_date_range_filter(self):
        base = _pkt_now_naive()
        day = lambda n: (base - timedelta(days=n)).date().isoformat()
        # Deliveries sit at base-9d, base-3d, base-1d and today.
        self.assertEqual(self.register_ids(self.get(DELIVERY_URL, date_from=day(4), date_to=day(2))),
                         [self.ids['d_full']])
        self.assertEqual(self.register_ids(self.get(DELIVERY_URL, date_from=day(2))),
                         [self.ids['d_new'], self.ids['d_mid']])
        self.assertEqual(self.register_ids(self.get(DELIVERY_URL, date_to=day(4))),
                         [self.ids['d_old']])
        self.assertEqual(self.register_ids(self.get(DELIVERY_URL, date_from=day(2), date_to=day(2))),
                         [])

    def test_delivery_register_rejects_a_non_numeric_po_and_says_why(self):
        response = self.client.get(DELIVERY_URL, query_string={'po_number': 'abc'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('PO number must be numeric', response.get_data(as_text=True))

    def test_delivery_register_reports_when_nothing_matches(self):
        html = self.get(DELIVERY_URL, project_id=self.ids['site_b'], material_id=self.ids['steel'])
        self.assertEqual(self.register_ids(html), [])
        self.assertIn('No deliveries match these filters.', html)

    def test_usage_log_filters_by_site_and_stage(self):
        ids = self.ids
        self.assertEqual(self.usage_ids(self.get(USAGE_URL)),
                         [ids['u_new'], ids['u_site_b'], ids['u_old']])
        self.assertEqual(self.usage_ids(self.get(USAGE_URL, project_id=ids['site_b'])),
                         [ids['u_site_b']])
        self.assertEqual(self.usage_ids(self.get(USAGE_URL, stage_id=ids['stage_b'])),
                         [ids['u_site_b']])
        self.assertEqual(self.usage_ids(self.get(USAGE_URL, project_id=ids['site_a'],
                                                 stage_id=ids['stage_a'])),
                         [ids['u_new'], ids['u_old']])
        self.assertEqual(self.usage_ids(self.get(USAGE_URL, material_id=ids['steel'])),
                         [ids['u_new']])
        self.assertEqual(self.usage_ids(self.get(USAGE_URL, po_number=ids['po_cement'])),
                         [ids['u_site_b'], ids['u_old']])

    def test_filter_fields_render_on_both_registers(self):
        delivery_html = self.get(DELIVERY_URL)
        for field in ('po_number', 'material_id', 'supplier_id', 'project_id',
                      'stage_id', 'date_from', 'date_to', 'sort'):
            self.assertIn(f'name="{field}"', delivery_html)
        usage_html = self.get(USAGE_URL)
        for field in ('po_number', 'material_id', 'project_id', 'stage_id',
                      'date_from', 'date_to', 'sort'):
            self.assertIn(f'name="{field}"', usage_html)

    # -- 5. arrange by PO number -------------------------------------------
    def test_delivery_register_can_be_arranged_by_po_number(self):
        ids = self.ids
        self.assertEqual(self.register_ids(self.get(DELIVERY_URL, sort='po_asc')),
                         [ids['d_mid'], ids['d_old'], ids['d_new'], ids['d_full']])
        self.assertEqual(self.register_ids(self.get(DELIVERY_URL, sort='po_desc')),
                         [ids['d_full'], ids['d_new'], ids['d_mid'], ids['d_old']])
        self.assertEqual(self.register_ids(self.get(DELIVERY_URL, sort='oldest')),
                         [ids['d_old'], ids['d_full'], ids['d_mid'], ids['d_new']])
        # An unknown arrangement falls back to newest first.
        self.assertEqual(self.register_ids(self.get(DELIVERY_URL, sort='sideways')),
                         self.register_ids(self.get(DELIVERY_URL)))
        self.assertIn('<option value="po_asc" selected>PO # (low &rarr; high)</option>',
                      self.get(DELIVERY_URL, sort='po_asc'))

    def test_usage_log_can_be_arranged_by_po_number(self):
        ids = self.ids
        self.assertEqual(self.usage_ids(self.get(USAGE_URL, sort='po_asc')),
                         [ids['u_site_b'], ids['u_old'], ids['u_new']])
        self.assertEqual(self.usage_ids(self.get(USAGE_URL, sort='po_desc')),
                         [ids['u_new'], ids['u_site_b'], ids['u_old']])
        self.assertEqual(self.usage_ids(self.get(USAGE_URL, sort='oldest')),
                         [ids['u_old'], ids['u_site_b'], ids['u_new']])
        self.assertIn('<option value="po_desc" selected>PO # (high &rarr; low)</option>',
                      self.get(USAGE_URL, sort='po_desc'))

    def test_totals_follow_the_filtered_rows(self):
        unfiltered = self.get(DELIVERY_URL)
        self.assertIn('4 of 4 records', unfiltered)
        filtered = self.get(DELIVERY_URL, project_id=self.ids['site_b'])
        self.assertIn('1 of 4 records', filtered)
        self.assertIn('Total Qty: 20.00', filtered)


if __name__ == '__main__':
    unittest.main()
