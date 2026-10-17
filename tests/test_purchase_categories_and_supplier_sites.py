"""Purchase item categories + supplier ⇄ material / site tracking.

Pins the field requests written up in ``PURCHASE_CATEGORY_SUPPLIER_SITE_SPEC.md``:

1. every item carries an item type (category), and every purchase line carries
   one too — the line's own choice, falling back to its material's;
2. the registers can be filtered by "All Categories", by one category, and by
   "Uncategorized" (legacy rows stay reachable);
3. the item-wise register shows purchase / delivery / usage on item basis, and
   each item opens its own ledger;
4. the supplier list is readable by item type, by item and by site — stated
   ("deals in" / assigned) kept apart from actual (supplied / delivered);
5. supplier ⇄ site tracking works in both directions, and a site shows its
   suppliers on the project page.

Only disposable databases are used.  Run with:
    python -m unittest discover -s tests -p 'test_purchase_categories*.py'
"""
import unittest

from qa_support import IsolatedAppTest

from hdc.extensions import db
from hdc.models.materials import (Delivery, MaterialCategory, MaterialV2,
                                  PurchaseV2, Supplier, SupplierCategory,
                                  SupplierSite, UsageLogV2)
from hdc.models.projects import Project, Stage
from hdc.services.material_categories import (DEFAULT_MATERIAL_CATEGORIES,
                                              _material_category_by_name,
                                              _material_category_label,
                                              _ensure_material_category_seed,
                                              _purchase_category_condition,
                                              UNCATEGORIZED_ID)
from hdc.services.purchase import (_site_supplier_supplied_rows,
                                   _supplier_category_supplied_rows,
                                   _supplier_site_supplied_rows,
                                   _supplier_supplied_profile)
from hdc.utils.dates import _pkt_now_naive, _pkt_today

CATEGORIES_URL = '/hdc/purchase-v2/categories'
ITEMS_URL = '/hdc/purchase-v2/items'
BY_SITE_URL = '/hdc/purchase-v2/suppliers-by-site'

# Page text is never judged as a whole: every form carries a dropdown with the
# full item list, so a material name proves nothing outside the register itself.
# Each slice is taken between the template's own region comments.
# Each slice starts at the template's own region comment for that register and
# stops at the end of its table, so the filter bar and the modals — which list
# every item, category and supplier by design — can never satisfy an assertion.
# Each slice starts at the template's own region comment for that register and
# stops where the next card begins, so the filter bars and the modals — which
# list every item, category and supplier by design — can never satisfy an
# assertion about what a register holds.
REGIONS = {
    'purchases': ('<!-- Purchases Table -->', '<!-- Edit Purchase Modal -->'),
    'delivered': ('<!-- Delivery Records Table -->', '<!-- Pending Purchases pop-up'),
    'usage': ('<i class="fas fa-list me-2"></i>Usage Log', '<!-- Edit Usage Modal -->'),
    'materials': ('<!-- Materials Table -->', '<!-- Edit Material Modal -->'),
    'items': ('Items — Purchase / Delivery / Usage', None),
    'suppliers': ('<!-- Suppliers Table -->', '<!-- Edit Supplier Modal -->'),
    'site_suppliers': ('<!-- Site → Suppliers -->', '<!-- Supplier → Sites -->'),
    'supplier_sites': ('<!-- Supplier → Sites -->', None),
}


def region(html, name):
    """One register table: no filter bar, no modal, no other register."""
    start_marker, end_marker = REGIONS[name]
    start = html.index(start_marker)
    end_marker = end_marker or '</table>'
    return html[start:html.index(end_marker, start)]


class PurchaseCategoryCase(IsolatedAppTest):
    """Shared fixture: two categories, three items, two sites, two suppliers."""

    def setUp(self):
        super().setUp()
        with self.app.app_context():
            base = _pkt_now_naive()
            # The vocabulary is seeded at bootstrap — reuse it rather than
            # creating a second "Cement", which the unique index would refuse.
            cement = _material_category_by_name('Cement')
            steel = _material_category_by_name('Steel')
            self.assertIsNotNone(cement, 'Cement is one of the seeded item types')
            self.assertIsNotNone(steel, 'Steel is one of the seeded item types')
            self.category_ids = {'cement': cement.id, 'steel': steel.id}
            self.category_names = {'cement': cement.name, 'steel': steel.name}

            trader = Supplier(name='Cement Co')
            steel_trader = Supplier(name='Steel Traders')
            db.session.add_all([trader, steel_trader])
            db.session.flush()
            self.supplier_ids = {'cement': trader.id, 'steel': steel_trader.id}

            cement_item = MaterialV2(name='Cement OPC', unit='BAG', category_id=cement.id)
            steel_item = MaterialV2(name='Steel Bar 12mm', unit='KG', category_id=steel.id)
            # Deliberately uncategorized: the "Uncategorized" bucket must find it.
            loose = MaterialV2(name='Loose Wire', unit='METER')
            db.session.add_all([cement_item, steel_item, loose])
            db.session.flush()
            self.material_ids = {'cement': cement_item.id, 'steel': steel_item.id,
                                 'loose': loose.id}

            site_a = Project(project_code='CAT-A', name='Category Site A')
            site_b = Project(project_code='CAT-B', name='Category Site B')
            db.session.add_all([site_a, site_b])
            db.session.flush()
            stage_a = Stage(name='Slab A', project_id=site_a.id)
            db.session.add(stage_a)
            db.session.flush()
            self.site_ids = {'a': site_a.id, 'b': site_b.id}
            self.stage_ids = {'a': stage_a.id}

            po_cement = PurchaseV2(
                supplier_id=trader.id, material_id=cement_item.id, category_id=cement.id,
                unit_price=1300, quantity=200, total_amount=260000,
                payment_status='unpaid', date=_pkt_today(), is_void=False,
                created_at=base)
            po_steel = PurchaseV2(
                supplier_id=steel_trader.id, material_id=steel_item.id,
                # No category of its own: it must inherit Steel from its item.
                category_id=None, unit_price=280, quantity=1000, total_amount=280000,
                payment_status='unpaid', date=_pkt_today(), is_void=False,
                created_at=base)
            po_loose = PurchaseV2(
                supplier_id=trader.id, material_id=loose.id, category_id=None,
                unit_price=40, quantity=100, total_amount=4000,
                payment_status='unpaid', date=_pkt_today(), is_void=False,
                created_at=base)
            db.session.add_all([po_cement, po_steel, po_loose])
            db.session.flush()
            self.purchase_ids = {'cement': po_cement.id, 'steel': po_steel.id,
                                 'loose': po_loose.id}

            db.session.add_all([
                Delivery(purchase_id=po_cement.id, material_id=cement_item.id,
                         project_id=site_a.id, stage_id=stage_a.id, quantity=150,
                         date=_pkt_today(), is_void=False, created_at=base),
                Delivery(purchase_id=po_cement.id, material_id=cement_item.id,
                         project_id=site_b.id, quantity=50, date=_pkt_today(),
                         is_void=False, created_at=base),
                Delivery(purchase_id=po_steel.id, material_id=steel_item.id,
                         project_id=site_a.id, stage_id=stage_a.id, quantity=600,
                         date=_pkt_today(), is_void=False, created_at=base),
                UsageLogV2(purchase_id=po_cement.id, material_id=cement_item.id,
                           project_id=site_a.id, stage_id=stage_a.id, quantity=100,
                           cost=130000, date=_pkt_today(), is_void=False, created_at=base),
                UsageLogV2(purchase_id=po_steel.id, material_id=steel_item.id,
                           project_id=site_a.id, stage_id=stage_a.id, quantity=400,
                           cost=112000, date=_pkt_today(), is_void=False, created_at=base),
            ])
            db.session.commit()

    # ── 1. the vocabulary ───────────────────────────────────────────────────

    def test_default_categories_are_seeded_once(self):
        with self.app.app_context():
            names = {c.name for c in MaterialCategory.query.all()}
            self.assertTrue({'Cement', 'Steel', 'Electric Materials',
                             'Plumbing Materials'}.issubset(names))
            self.assertTrue(set(DEFAULT_MATERIAL_CATEGORIES).issubset(names))
            before = MaterialCategory.query.count()
            self.assertEqual(_ensure_material_category_seed(), 0,
                             'a second boot must not duplicate the vocabulary')
            self.assertEqual(MaterialCategory.query.count(), before)

    def test_category_can_be_added_renamed_and_is_not_deleted_when_in_use(self):
        self.form(CATEGORIES_URL, name='Tile Adhesive')
        with self.app.app_context():
            row = MaterialCategory.query.filter_by(name='Tile Adhesive').first()
            self.assertIsNotNone(row)
            added_id = row.id
        self.form(f'{CATEGORIES_URL}/{added_id}/edit', name='Tile Adhesives', status='active')
        with self.app.app_context():
            self.assertEqual(MaterialCategory.query.get(added_id).name, 'Tile Adhesives')

        # Cement is used by an item, so "delete" must deactivate, not vanish —
        # the item would otherwise silently lose its type.
        cement_id = self.category_ids['cement']
        self.form(f'{CATEGORIES_URL}/{cement_id}/delete')
        with self.app.app_context():
            row = MaterialCategory.query.get(cement_id)
            self.assertIsNotNone(row, 'a category in use is never deleted')
            self.assertFalse(row.is_void)
            self.assertEqual(row.status, 'inactive')

    # ── 2. every purchase line carries a category ───────────────────────────

    def test_new_purchase_line_takes_the_chosen_category(self):
        with self.app.app_context():
            supplier_id = self.supplier_ids['cement']
            material_id = self.material_ids['loose']
            steel_id = self.category_ids['steel']
        self.form('/hdc/purchase-v2/purchases', supplier_id=supplier_id,
                  material_id=[material_id], quantity=[5], unit_price=[10],
                  category_id=[steel_id], payment_status='unpaid')
        with self.app.app_context():
            row = (PurchaseV2.query.filter_by(material_id=material_id)
                   .order_by(PurchaseV2.id.desc()).first())
            self.assertEqual(row.category_id, steel_id,
                             'the explicitly chosen line category wins')

    def test_new_purchase_line_falls_back_to_the_item_category(self):
        with self.app.app_context():
            supplier_id = self.supplier_ids['cement']
            material_id = self.material_ids['cement']
            cement_id = self.category_ids['cement']
        self.form('/hdc/purchase-v2/purchases', supplier_id=supplier_id,
                  material_id=[material_id], quantity=[7], unit_price=[100],
                  category_id=[''], payment_status='unpaid')
        with self.app.app_context():
            row = (PurchaseV2.query.filter_by(material_id=material_id)
                   .order_by(PurchaseV2.id.desc()).first())
            self.assertEqual(row.category_id, cement_id,
                             'a blank line category copies the item category')

    def test_a_purchase_without_its_own_category_reads_through_its_item(self):
        with self.app.app_context():
            steel_category = self.category_ids['steel']
            rows = (db.session.query(PurchaseV2.id)
                    .filter(_purchase_category_condition(steel_category)).all())
            ids = {int(r[0]) for r in rows}
            self.assertIn(self.purchase_ids['steel'], ids,
                          'a NULL line category resolves through the material')
            self.assertNotIn(self.purchase_ids['cement'], ids)

    def test_sync_button_rewrites_purchase_lines_to_the_item_category(self):
        with self.app.app_context():
            steel_id = self.category_ids['steel']
            cement_po = PurchaseV2.query.get(self.purchase_ids['cement'])
            self.assertEqual(cement_po.category_id, self.category_ids['cement'])
            # Move the item to another type, then re-apply.
            MaterialV2.query.get(self.material_ids['cement']).category_id = steel_id
            db.session.commit()
        self.form(f'{CATEGORIES_URL}/sync-purchases')
        with self.app.app_context():
            cement_po = PurchaseV2.query.get(self.purchase_ids['cement'])
            self.assertEqual(cement_po.category_id, self.category_ids['steel'])

    # ── 3. filters: all categories, one category, uncategorized ─────────────

    def test_every_register_takes_the_category_filter(self):
        cement_id = self.category_ids['cement']
        steel_id = self.category_ids['steel']

        purchases = region(self.client.get(
            f'/hdc/purchase-v2/purchases?category_id={cement_id}').get_data(as_text=True),
            'purchases')
        self.assertIn('Cement OPC', purchases)
        self.assertNotIn('Steel Bar 12mm', purchases)

        deliveries = region(self.client.get(
            f'/hdc/purchase-v2/delivered?category_id={steel_id}').get_data(as_text=True),
            'delivered')
        self.assertIn('Steel Bar 12mm', deliveries)
        self.assertNotIn('Cement OPC', deliveries)

        usage = region(self.client.get(
            f'/hdc/purchase-v2/usage?category_id={cement_id}').get_data(as_text=True), 'usage')
        self.assertIn('Cement OPC', usage)
        self.assertNotIn('Steel Bar 12mm', usage)

        materials = region(self.client.get(
            f'/hdc/purchase-v2/materials?category_id={steel_id}').get_data(as_text=True),
            'materials')
        self.assertIn('Steel Bar 12mm', materials)
        self.assertNotIn('Cement OPC', materials)

        # Uncategorized (id 0) must reach the legacy rows in every register.
        loose_materials = region(self.client.get(
            f'/hdc/purchase-v2/materials?category_id={UNCATEGORIZED_ID}').get_data(as_text=True),
            'materials')
        self.assertIn('Loose Wire', loose_materials)
        self.assertNotIn('Cement OPC', loose_materials)
        loose_rows = region(self.client.get(
            f'/hdc/purchase-v2/purchases?category_id={UNCATEGORIZED_ID}').get_data(as_text=True),
            'purchases')
        self.assertIn('Loose Wire', loose_rows)
        self.assertNotIn('Cement OPC', loose_rows)
        self.assertIn('#%d' % self.purchase_ids['loose'], loose_rows,
                      'the loose-wire purchase line is listed')

        # "All Categories" is the register unfiltered.
        all_rows = region(self.client.get('/hdc/purchase-v2/purchases').get_data(as_text=True),
                          'purchases')
        self.assertIn('Cement OPC', all_rows)
        self.assertIn('Steel Bar 12mm', all_rows)

        # An unknown category is ignored, exactly like an unknown PO number —
        # the page still renders, showing everything.
        unknown = self.client.get('/hdc/purchase-v2/purchases?category_id=99999')
        self.assertEqual(unknown.status_code, 200)
        self.assertIn('Cement OPC',
                      region(unknown.get_data(as_text=True), 'purchases'))

    def test_categories_page_shows_totals_and_both_filters(self):
        page = self.client.get(CATEGORIES_URL)
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn('Cement', html)
        self.assertIn('Steel', html)
        self.assertIn('All Categories', html,
                      'the unfiltered choice sits beside the per-category ones')
        self.assertIn(f'category_id={self.category_ids["cement"]}', html,
                      'each row links to its own filtered register')

    # ── 4. item-wise register ───────────────────────────────────────────────

    def test_item_wise_register_shows_purchase_delivery_and_usage(self):
        page = self.client.get(ITEMS_URL)
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn('Items — Purchase / Delivery / Usage', html)
        rows = region(html, 'items')
        self.assertIn('Cement OPC', rows)
        self.assertIn('Steel Bar 12mm', rows)
        for header in ('Purchase Rows', 'Purchased Qty', 'Sent Qty', 'Used Qty',
                       'In Store', 'Balance at Site', 'Category'):
            self.assertIn(header, rows, header)
        # 150 + 50 bags of cement sent to the two sites, 600 kg of steel.
        self.assertIn('200.00', rows, 'sent quantities are summed per item')
        self.assertIn('600.00', rows)

    def test_item_register_respects_the_category_filter(self):
        steel_id = self.category_ids['steel']
        rows = region(self.client.get(
            f'{ITEMS_URL}?category_id={steel_id}').get_data(as_text=True), 'items')
        self.assertIn('Steel Bar 12mm', rows)
        self.assertNotIn('Cement OPC', rows)
        loose = region(self.client.get(
            f'{ITEMS_URL}?category_id={UNCATEGORIZED_ID}').get_data(as_text=True), 'items')
        self.assertIn('Loose Wire', loose)
        self.assertNotIn('Steel Bar 12mm', loose)

    def test_item_ledger_lists_the_three_registers_for_one_item(self):
        material_id = self.material_ids['cement']
        page = self.client.get(f'{ITEMS_URL}/{material_id}')
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn('Cement OPC', html)
        self.assertIn('Purchases (', html)
        self.assertIn('Deliveries / Stock Movement (', html)
        self.assertIn('Material Usage (', html)
        self.assertIn('Category Site A', html, 'deliveries name the site')

    # ── 5. supplier list by material / category / site ───────────────────────

    def test_supplier_can_be_marked_as_dealing_in_categories_and_sites(self):
        supplier_id = self.supplier_ids['cement']
        cement_id = self.category_ids['cement']
        site_id = self.site_ids['b']
        self.form(f'/hdc/purchase-v2/suppliers/{supplier_id}/categories/add',
                  category_id=cement_id)
        self.form(f'/hdc/purchase-v2/suppliers/{supplier_id}/sites/add',
                  project_id=site_id, note='second site')
        with self.app.app_context():
            self.assertEqual(SupplierCategory.query.filter_by(supplier_id=supplier_id).count(), 1)
            link = SupplierSite.query.filter_by(supplier_id=supplier_id).first()
            self.assertEqual(link.project_id, site_id)
            self.assertEqual(link.note, 'second site')

        detail = self.client.get(f'/hdc/purchase-v2/suppliers/{supplier_id}').get_data(as_text=True)
        self.assertIn('Deals In (Item Categories)', detail)
        self.assertIn('Sites — Assigned &amp; Supplied', detail)

        # Removing the links leaves the history alone.
        self.form(f'/hdc/purchase-v2/suppliers/{supplier_id}/sites/{site_id}/remove')
        self.form(f'/hdc/purchase-v2/suppliers/{supplier_id}/categories/{cement_id}/remove')
        with self.app.app_context():
            self.assertEqual(SupplierSite.query.filter_by(supplier_id=supplier_id).count(), 0)
            self.assertEqual(SupplierCategory.query.filter_by(supplier_id=supplier_id).count(), 0)
            self.assertGreater(PurchaseV2.query.filter_by(supplier_id=supplier_id).count(), 0,
                               'purchases survive an un-linked supplier')

    def test_supplier_list_filters_by_category_item_and_site(self):
        cement_id = self.category_ids['cement']

        by_category = region(self.client.get(
            f'/hdc/purchase-v2/suppliers?category_id={cement_id}').get_data(as_text=True),
            'suppliers')
        self.assertIn('Cement Co', by_category)
        self.assertNotIn('Steel Traders', by_category)

        by_item = region(self.client.get(
            f'/hdc/purchase-v2/suppliers?material_id={self.material_ids["steel"]}'
        ).get_data(as_text=True), 'suppliers')
        self.assertIn('Steel Traders', by_item)
        self.assertNotIn('Cement Co', by_item)

        by_site = region(self.client.get(
            f'/hdc/purchase-v2/suppliers?project_id={self.site_ids["b"]}'
        ).get_data(as_text=True), 'suppliers')
        self.assertIn('Cement Co', by_site, 'cement was delivered to site B')
        self.assertNotIn('Steel Traders', by_site, 'steel never went to site B')

        # The list carries the tracking columns themselves.
        everything = self.client.get('/hdc/purchase-v2/suppliers').get_data(as_text=True)
        for header in ('Deals In (stated)', 'Supplied Categories', 'Sites Supplied',
                       'Assigned Sites'):
            self.assertIn(header, everything)
        self.assertIn('Cement OPC', region(everything, 'suppliers'),
                      'the materials behind each supplier are named')

    def test_supplied_profiles_read_from_purchases_not_from_links(self):
        with self.app.app_context():
            profiles = _supplier_supplied_profile()
            cement_supplier = profiles[self.supplier_ids['cement']]
            self.assertEqual(cement_supplier['category_ids'], [self.category_ids['cement']])
            self.assertEqual(cement_supplier['material_ids'],
                             [self.material_ids['cement'], self.material_ids['loose']])
            steel_supplier = profiles[self.supplier_ids['steel']]
            self.assertEqual(steel_supplier['category_ids'], [self.category_ids['steel']],
                             'an un-categorized line still counts under its item type')

    # ── 6. supplier ⇄ site tracking, both directions ────────────────────────

    def test_supplier_site_rows_read_from_deliveries(self):
        with self.app.app_context():
            rows = _supplier_site_supplied_rows(self.supplier_ids['cement'])
            by_site = {r['project_id']: r for r in rows}
            self.assertEqual(set(by_site), {self.site_ids['a'], self.site_ids['b']})
            self.assertAlmostEqual(by_site[self.site_ids['a']]['quantity'], 150.0)
            self.assertAlmostEqual(by_site[self.site_ids['b']]['quantity'], 50.0)
            self.assertAlmostEqual(by_site[self.site_ids['a']]['value'], 150 * 1300.0)

            categories = _supplier_category_supplied_rows(self.supplier_ids['cement'])
            names = {r['category_name']: r for r in categories}
            self.assertIn('Cement', names)
            self.assertIn('Uncategorized', names,
                          'the loose-wire line is its own bucket until it is filed')

    def test_site_supplier_view_lists_both_directions(self):
        page = self.client.get(BY_SITE_URL)
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn('Site → Suppliers', html)
        self.assertIn('Supplier → Sites', html)
        sites = region(html, 'site_suppliers')
        self.assertIn('Cement Co', sites)
        self.assertIn('Category Site A', sites)
        self.assertIn('Steel Traders', sites, 'steel went to site A')
        suppliers = region(html, 'supplier_sites')
        self.assertIn('Cement Co', suppliers)
        self.assertIn('Steel Traders', suppliers)
        self.assertIn('Category Site A', suppliers)
        self.assertIn('Category Site B', suppliers,
                      'Cement Co delivered to both sites')

        site_filtered = region(self.client.get(
            f'{BY_SITE_URL}?project_id={self.site_ids["b"]}').get_data(as_text=True),
            'site_suppliers')
        self.assertIn('Cement Co', site_filtered)
        self.assertNotIn('Steel Traders', site_filtered)

    def test_assigned_only_supplier_shows_up_as_assigned(self):
        supplier_id = self.supplier_ids['steel']
        site_id = self.site_ids['b']
        self.form(f'/hdc/purchase-v2/suppliers/{supplier_id}/sites/add', project_id=site_id)
        sites = region(self.client.get(
            f'{BY_SITE_URL}?project_id={site_id}').get_data(as_text=True), 'site_suppliers')
        self.assertIn('Assigned only', sites)
        self.assertIn('Steel Traders', sites)

    def test_project_page_lists_the_sites_suppliers(self):
        page = self.client.get(f'/hdc/projects/{self.site_ids["a"]}')
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn('Site Suppliers (Purchase Tracking)', html)
        self.assertIn('Cement Co', html)
        self.assertIn('Steel Traders', html)
        self.assertIn('suppliers-by-site?project_id=%d' % self.site_ids['a'], html)

    def test_category_label_helper_covers_the_uncategorized_bucket(self):
        with self.app.app_context():
            self.assertEqual(_material_category_label(None), 'Uncategorized')
            self.assertEqual(_material_category_label(0), 'Uncategorized')
            self.assertEqual(_material_category_label(self.category_ids['cement']), 'Cement')

    def test_stock_page_and_hub_name_the_category(self):
        hub = self.client.get('/hdc/purchase-v2').get_data(as_text=True)
        self.assertIn('Item Categories', hub)
        self.assertIn('Item-wise View', hub)
        stock = self.client.get(
            f'/hdc/purchase-v2/stock?category_id={self.category_ids["steel"]}').get_data(as_text=True)
        self.assertIn('Item Category', stock)


    # ── 7. the JSON material API speaks the same vocabulary ─────────────────

    def test_api_material_create_and_update_accept_a_category(self):
        with self.app.app_context():
            category_id = self.category_ids['steel']
            category_name = self.category_names['steel']
        created = self.api('/api/v2/purchase/materials',
                           {'name': 'Binding Wire', 'unit': 'KG',
                            'category_id': category_id})
        self.assertTrue(created['ok'])
        self.assertEqual(created['category_id'], category_id)
        self.assertEqual(created['category_name'], category_name)

        listed = self.api('/api/v2/purchase/materials', {}, method='GET')
        self.assertTrue(any(c['name'] == category_name for c in listed['categories']))
        item = [i for i in listed['items'] if i['id'] == created['id']][0]
        self.assertEqual(item['category_id'], category_id)
        self.assertEqual(item['category_name'], category_name)

        # Re-pointing by name: an unknown name is created on the spot, which is
        # what the quick-add in the material pickers relies on.
        updated = self.api(f'/api/v2/purchase/materials/{created["id"]}',
                           {'category_name': 'Plumbing Materials'}, method='PUT')
        self.assertEqual(updated['category_name'], 'Plumbing Materials')
        with self.app.app_context():
            row = MaterialV2.query.get(created['id'])
            self.assertEqual(row.category.name, 'Plumbing Materials')


if __name__ == '__main__':
    unittest.main()
