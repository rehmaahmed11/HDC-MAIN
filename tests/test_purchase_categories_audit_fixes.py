"""Audit of the Purchase item-type (category) + supplier ⇄ material/site work.

Every case here pins a defect that the first pass of the item-type feature
shipped with.  They are written as **behaviour**, not as "the function returns
X", because the whole point of the feature is that the registers must agree
with each other:

* the categories page must report the real site / supplier counts for every
  item type — not "0" because one item somewhere is uncategorised;
* the "Uncategorized" bucket must actually be reachable on every register that
  offers it (Suppliers, Suppliers-by-site), and must show *only* those rows;
* the Stock page's "Sent of Purchased" must be measured over the items the
  page is showing, not over the whole book;
* "In store" is a balance, so it must read the full history on the item-wise
  register exactly as it does on the item ledger;
* a search box must narrow the page it belongs to — including the rows that
  are only *assigned*;
* the same purchase row must show the same item type on every page (the
  shared rule: the line's own type, else its item's);
* an item type that was retired (set inactive because records still use it)
  must survive an unrelated edit instead of being silently re-filed;
* re-using the name of a deleted item type must not crash the page.

Only disposable databases are used.  Run with:
    python -m unittest discover -s tests -p 'test_purchase_categories_audit*.py'
"""
import datetime
import re
import unittest

from qa_support import IsolatedAppTest

from hdc.extensions import db
from hdc.models.materials import (Delivery, MaterialCategory, MaterialV2,
                                  PurchaseV2, Supplier, SupplierCategory,
                                  SupplierSite, UsageLogV2)
from hdc.models.projects import Project, Stage
from hdc.services.material_categories import (_material_category_by_name,
                                              _material_category_summary_rows)
try:
    from hdc.services.material_categories import _material_category_for_write
except ImportError:      # the rule helper is part of the fix
    _material_category_for_write = None
from hdc.utils.dates import _pkt_now_naive, _pkt_today

CATEGORIES_URL = '/hdc/purchase-v2/categories'
ITEMS_URL = '/hdc/purchase-v2/items'
BY_SITE_URL = '/hdc/purchase-v2/suppliers-by-site'
SUPPLIERS_URL = '/hdc/purchase-v2/suppliers'
STOCK_URL = '/hdc/purchase-v2/stock'
PURCHASES_URL = '/hdc/purchase-v2/purchases'
DELIVERED_URL = '/hdc/purchase-v2/delivered'

CATEGORIES_TABLE = 'Categories — Purchased / Sent / Used'
ITEMS_TABLE = 'Items — Purchase / Delivery / Usage'
SUPPLIERS_TABLE = '<!-- Suppliers Table -->'
SUPPLIERS_TABLE_END = '<!-- Edit Supplier Modal -->'
SITE_TABLE = '<!-- Site → Suppliers -->'
SITE_TABLE_END = '<!-- Supplier → Sites -->'
SUPPLIER_SITES_TABLE = '<!-- Supplier → Sites -->'


def table_region(html, marker, end_marker='</table>'):
    """One table on a page — no filter bar, no modal, no other table."""
    start = html.index(marker)
    return html[start:html.index(end_marker, start)]


def _clean(cell):
    """Cell text without the markup around it."""
    text = re.sub(r'<[^>]+>', ' ', cell)
    return ' '.join(text.split())


def rows_of(html, marker, end_marker='</table>'):
    """The data rows of one table, as lists of plain cell texts."""
    region = table_region(html, marker, end_marker)
    out = []
    for match in re.finditer(r'<tr[^>]*>(.*?)</tr>', region, re.S):
        cells = re.findall(r'<td[^>]*>(.*?)</td>', match.group(1), re.S)
        if cells:
            out.append([_clean(c) for c in cells])
    return out


def row_with(table, needle, column=0):
    """The first row whose given cell contains ``needle``."""
    for row in table:
        if len(row) > column and needle in row[column]:
            return row
    return None


def has_text(table, needle):
    """True when any cell of the table contains ``needle``."""
    return any(needle in cell for row in table for cell in row)


class PurchaseCategoryAuditCase(IsolatedAppTest):
    """Two item types, three items (one uncategorised), two sites, two suppliers."""

    def setUp(self):
        super().setUp()
        with self.app.app_context():
            base = _pkt_now_naive()
            cement = _material_category_by_name('Cement')
            steel = _material_category_by_name('Steel')
            self.cement_id = cement.id
            self.steel_id = steel.id

            trader = Supplier(name='Cement Co')
            steel_trader = Supplier(name='Steel Traders')
            db.session.add_all([trader, steel_trader])
            db.session.flush()
            self.supplier_ids = {'cement': trader.id, 'steel': steel_trader.id}

            cement_item = MaterialV2(name='Cement OPC', unit='BAG', category_id=cement.id)
            steel_item = MaterialV2(name='Steel Bar 12mm', unit='KG', category_id=steel.id)
            loose = MaterialV2(name='Loose Wire', unit='METER')   # uncategorised on purpose
            db.session.add_all([cement_item, steel_item, loose])
            db.session.flush()
            self.material_ids = {'cement': cement_item.id, 'steel': steel_item.id,
                                 'loose': loose.id}

            site_a = Project(project_code='AUD-A', name='Audit Site A')
            site_b = Project(project_code='AUD-B', name='Audit Site B')
            db.session.add_all([site_a, site_b])
            db.session.flush()
            stage_a = Stage(name='Slab A', project_id=site_a.id)
            db.session.add(stage_a)
            db.session.flush()
            self.site_ids = {'a': site_a.id, 'b': site_b.id}
            self.stage_ids = {'a': stage_a.id}

            # Cement: 200 bought, 200 sent (150 + 50), 100 used.
            po_cement = PurchaseV2(
                supplier_id=trader.id, material_id=cement_item.id, category_id=cement.id,
                unit_price=1300, quantity=200, total_amount=260000,
                payment_status='unpaid', date=_pkt_today(), is_void=False, created_at=base)
            # Steel: 1000 bought, 600 sent, no category of its own (inherits Steel).
            po_steel = PurchaseV2(
                supplier_id=steel_trader.id, material_id=steel_item.id, category_id=None,
                unit_price=280, quantity=1000, total_amount=280000,
                payment_status='unpaid', date=_pkt_today(), is_void=False, created_at=base)
            # Loose Wire: 100 bought, 50 sent — no item type anywhere.
            po_loose = PurchaseV2(
                supplier_id=trader.id, material_id=loose.id, category_id=None,
                unit_price=40, quantity=100, total_amount=4000,
                payment_status='unpaid', date=_pkt_today(), is_void=False, created_at=base)
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
                Delivery(purchase_id=po_loose.id, material_id=loose.id,
                         project_id=site_a.id, quantity=50, date=_pkt_today(),
                         is_void=False, created_at=base),
                UsageLogV2(purchase_id=po_cement.id, material_id=cement_item.id,
                           project_id=site_a.id, stage_id=stage_a.id, quantity=100,
                           cost=130000, date=_pkt_today(), is_void=False, created_at=base),
            ])
            db.session.commit()

    # ── 1. the categories page must report real counts ─────────────────────

    def test_categories_page_reports_real_site_and_supplier_counts(self):
        """Every named item type carries its own site / supplier counts.

        They used to be written only when the Uncategorized bucket happened
        to be empty, so one uncategorised item made the whole page read
        "0 sites, 0 suppliers" for every real item type.
        """
        with self.app.app_context():
            rows = _material_category_summary_rows(include_inactive=True)
            cement = next(r for r in rows if r['name'] == 'Cement')
            self.assertEqual(cement['site_count'], 2,
                             'Cement was delivered to two sites')
            self.assertEqual(cement['supplier_count'], 1,
                             'Cement was bought from one supplier')
            for row in rows:
                self.assertIsNotNone(row.get('site_count'),
                                     f"{row['name']} lost its site count")
                self.assertIsNotNone(row.get('supplier_count'),
                                     f"{row['name']} lost its supplier count")

        html = self.client.get(CATEGORIES_URL).get_data(as_text=True)
        table = rows_of(html, CATEGORIES_TABLE)
        cement_row = row_with(table, 'Cement', 0)
        self.assertIsNotNone(cement_row, 'the Cement row must be on the page')
        # Category | Status | Items | Purchase Rows | Purchased Qty | Value |
        # Sent Qty | Used Qty | Sites | Suppliers | Actions
        self.assertEqual(cement_row[8], '2', 'Cement must report its 2 sites')
        self.assertEqual(cement_row[9], '1', 'Cement must report its 1 supplier')

    def test_categories_page_kpi_does_not_count_the_uncategorized_bucket(self):
        """The Uncategorized row points at missing data; it is not a type."""
        with self.app.app_context():
            real = MaterialCategory.query.filter_by(is_void=False).count()
        html = self.client.get(CATEGORIES_URL).get_data(as_text=True)
        # The page states the count as "<n> categories".
        match = re.search(r'(\d+)\s*categories', html)
        self.assertIsNotNone(match, 'the categories KPI must be on the page')
        self.assertEqual(int(match.group(1)), real,
                         'the KPI must count real item types only')

    # ── 2. the Uncategorized bucket must be reachable everywhere ───────────

    def test_suppliers_page_uncategorized_filter_finds_the_supplier(self):
        """A supplier that supplied legacy rows must be findable.

        The filter compared the bucket id (0) with the supplier's list of
        item types — which never holds 0 — so the bucket was dead: it always
        answered "no supplier".
        """
        html = self.client.get(f'{SUPPLIERS_URL}?category_id=0').get_data(as_text=True)
        table = rows_of(html, SUPPLIERS_TABLE, SUPPLIERS_TABLE_END)
        self.assertTrue(has_text(table, 'Cement Co'),
                        'the supplier that supplied an uncategorised item must show up')
        self.assertFalse(has_text(table, 'Steel Traders'),
                         'a supplier that only supplied typed items must not '
                         'appear under the Uncategorized bucket')
        row = row_with(table, 'Cement Co', 0)
        # Supplier | Phone | Status | Deals In | Supplied Categories | ...
        self.assertIn('Uncategorized', row[4],
                      'the row must say that its supply is uncategorised')

    def test_suppliers_page_bucket_is_not_a_catch_all(self):
        """The bucket shows the uncategorised rows only — never every row."""
        bucket = self.client.get(f'{SUPPLIERS_URL}?category_id=0').get_data(as_text=True)
        every = self.client.get(SUPPLIERS_URL).get_data(as_text=True)
        bucket_rows = rows_of(bucket, SUPPLIERS_TABLE, SUPPLIERS_TABLE_END)
        every_rows = rows_of(every, SUPPLIERS_TABLE, SUPPLIERS_TABLE_END)
        self.assertTrue(bucket_rows, 'the bucket must find its supplier')
        self.assertTrue(any('Cement Co' in row[0] for row in bucket_rows),
                        'the bucket must find the supplier that supplied an '
                        'uncategorised item')
        self.assertLess(len(bucket_rows), len(every_rows),
                        'the Uncategorized bucket must not list every supplier')

    def test_suppliers_by_site_uncategorized_filter_shows_only_those_rows(self):
        """The by-site page must honour the bucket.

        The filter was a truth test, and 0 is falsy — so "Uncategorized" was
        read as "no filter" and the page handed back every row while the
        dropdown claimed otherwise.
        """
        html = self.client.get(f'{BY_SITE_URL}?category_id=0').get_data(as_text=True)
        table = rows_of(html, SITE_TABLE, SITE_TABLE_END)
        # Only the Loose Wire delivery belongs to no item type.
        self.assertTrue(table, 'the uncategorised delivery must be listed')
        self.assertTrue(has_text(table, 'Audit Site A'))
        self.assertFalse(has_text(table, 'Audit Site B'),
                         'the bucket must not drag in the other site')
        self.assertFalse(has_text(table, 'Steel Traders'),
                         'a typed supplier must not appear under the bucket')
        every = self.client.get(BY_SITE_URL).get_data(as_text=True)
        every_table = rows_of(every, SITE_TABLE, SITE_TABLE_END)
        self.assertLess(len(table), len(every_table),
                        'the bucket must not answer exactly like "all"')

    # ── 3. the Stock page must measure what it shows ───────────────────────

    def test_stock_sent_of_purchased_respects_the_category_filter(self):
        """The percentage must compare the sent figure with the same items.

        The sent figure was narrowed to the item type on screen while the
        purchased figure still counted the whole book, so the page answered
        a question nobody asked.
        """
        html = self.client.get(f'{STOCK_URL}?category_id={self.steel_id}').get_data(as_text=True)
        match = re.search(r'Sent of Purchased:.*?fw-semibold">([\d.,]+)%', html, re.S)
        self.assertIsNotNone(match, 'the "Sent of Purchased" KPI must be on the page')
        # Steel: 600 sent of 1000 purchased.
        self.assertEqual(match.group(1), '60.0',
                         'Steel: 600 sent of 1000 purchased is 60% — not a '
                         'percentage of every purchase in the book')

    def test_stock_unfiltered_percentage_still_reads_the_whole_book(self):
        """The unfiltered page keeps its meaning: sent vs everything bought."""
        html = self.client.get(STOCK_URL).get_data(as_text=True)
        match = re.search(r'Sent of Purchased:.*?fw-semibold">([\d.,]+)%', html, re.S)
        self.assertIsNotNone(match)
        # 850 sent (200 + 600 + 50) of 1300 purchased (200 + 1000 + 100)
        self.assertEqual(match.group(1), '65.4')

    def test_stock_filtered_and_unfiltered_percentages_differ(self):
        """The filter must actually move the number."""
        filtered = self.client.get(f'{STOCK_URL}?category_id={self.cement_id}').get_data(as_text=True)
        every = self.client.get(STOCK_URL).get_data(as_text=True)
        f = re.search(r'Sent of Purchased:.*?fw-semibold">([\d.,]+)%', filtered, re.S)
        e = re.search(r'Sent of Purchased:.*?fw-semibold">([\d.,]+)%', every, re.S)
        self.assertNotEqual(f.group(1), e.group(1),
                            'a category filter must change the stock reading')

    # ── 4. "in store" is a balance, not a flow ─────────────────────────────

    def test_item_register_in_store_is_the_store_balance_under_a_filter(self):
        """A date filter must not make the store look empty.

        The register mixed a date-filtered purchase figure with an all-time
        delivery figure, so the balance collapsed; the item ledger for the
        same item showed the real one.
        """
        tomorrow = (_pkt_today() + datetime.timedelta(days=1)).isoformat()
        html = self.client.get(f'{ITEMS_URL}?date_from={tomorrow}').get_data(as_text=True)
        table = rows_of(html, ITEMS_TABLE)
        loose_row = row_with(table, 'Loose Wire', 0)
        self.assertIsNotNone(loose_row)
        # Item | Category | Unit | Purchase Rows | Purchased Qty | Value |
        # Delivery Rows | Sent Qty | Usage Rows | Used Qty | In Store | At Site
        self.assertEqual(loose_row[4], '0.00',
                         'the purchase is outside the date range, so it is out')
        self.assertEqual(loose_row[10], '50.00',
                         'but the store still holds 100 - 50 = 50, whatever '
                         'the date filter says')
        detail = self.client.get(
            f'{ITEMS_URL}/{self.material_ids["loose"]}?date_from={tomorrow}').get_data(as_text=True)
        self.assertIn('50', detail, 'the item ledger must agree with the register')

    def test_item_register_and_ledger_agree_on_in_store(self):
        """The two pages must never disagree about the store balance."""
        html = self.client.get(ITEMS_URL).get_data(as_text=True)
        table = rows_of(html, ITEMS_TABLE)
        for name, expected in (('Cement OPC', '0.00'),
                               ('Loose Wire', '50.00'),
                               ('Steel Bar 12mm', '400.00')):
            row = row_with(table, name, 0)
            self.assertIsNotNone(row, f'{name} must be on the register')
            self.assertEqual(row[10], expected, f'{name}: wrong store balance')

    # ── 5. the search box must narrow the page it belongs to ───────────────

    def test_suppliers_by_site_search_narrows_assigned_only_suppliers(self):
        """A search that matches nothing must show nothing.

        The assignment rows were added after the search had been applied, so
        a page of "no results" still listed every assigned supplier.
        """
        with self.app.app_context():
            db.session.add(SupplierSite(supplier_id=self.supplier_ids['cement'],
                                        project_id=self.site_ids['a'],
                                        note='preferred', created_at=_pkt_now_naive()))
            db.session.commit()
        html = self.client.get(f'{BY_SITE_URL}?q=ZZZ-NO-SUCH-SUPPLIER').get_data(as_text=True)
        site_table = rows_of(html, SITE_TABLE, SITE_TABLE_END)
        supplier_table = rows_of(html, SUPPLIER_SITES_TABLE)
        self.assertFalse(has_text(site_table, 'Cement Co'),
                         'the site table must not list a supplier that does not match')
        self.assertFalse(has_text(supplier_table, 'Cement Co'),
                         'the supplier table must not list a supplier that does not match')
        # ...and a search that does match still finds it.
        html2 = self.client.get(f'{BY_SITE_URL}?q=Cement').get_data(as_text=True)
        site_table2 = rows_of(html2, SITE_TABLE, SITE_TABLE_END)
        self.assertTrue(has_text(site_table2, 'Cement Co'),
                        'a matching search must still find the supplier')

    # ── 6. one purchase row, one item type, on every page ──────────────────

    def test_supplier_page_shows_the_line_item_type_not_the_items(self):
        """A line filed under its own type must read the same everywhere."""
        with self.app.app_context():
            po = db.session.get(PurchaseV2, self.purchase_ids['cement'])
            po.category_id = self.steel_id      # the operator overrode it
            db.session.commit()
        html = self.client.get(f'{SUPPLIERS_URL}/{self.supplier_ids["cement"]}').get_data(as_text=True)
        table = rows_of(html, '<!-- Purchases -->')
        line = row_with(table, '#%d' % self.purchase_ids['cement'], 0)
        self.assertIsNotNone(line, 'the purchase must be listed on the supplier page')
        self.assertIn('Steel', ' '.join(line),
                      "the supplier page must show the line's own item type")
        # The Purchases register must agree.
        purchases = self.client.get(
            f'{PURCHASES_URL}?po_number={self.purchase_ids["cement"]}').get_data(as_text=True)
        purchase_table = rows_of(purchases, '<!-- Purchases Table -->', '<!-- Edit Purchase Modal -->')
        register_line = row_with(purchase_table, '#%d' % self.purchase_ids['cement'], 0)
        self.assertIsNotNone(register_line)
        self.assertIn('Steel', ' '.join(register_line),
                      'the Purchases register must show the same type')

    def test_delivery_register_has_an_item_category_column(self):
        """The Delivery register must name the item type of every row."""
        html = self.client.get(DELIVERED_URL).get_data(as_text=True)
        header = table_region(html, '<!-- Delivery Records Table -->',
                               '<!-- Pending Purchases pop-up')
        self.assertIn('Item Category', header, 'the register needs the column')
        table = rows_of(html, '<!-- Delivery Records Table -->',
                        '<!-- Pending Purchases pop-up')
        self.assertTrue(table, 'the delivery register must list the deliveries')
        self.assertTrue(has_text(table, 'Cement'),
                        'the delivery row must carry its item type')
        self.assertTrue(has_text(table, 'Uncategorized'),
                        'a delivery of an untyped item must say so')

    # ── 7. a retired item type must survive an unrelated edit ─────────────

    def test_editing_a_purchase_keeps_a_retired_item_type(self):
        """Changing the quantity must not re-file the purchase.

        A type that was set inactive still sits on the records that use it.
        The edit form could not show it, so saving anything at all silently
        moved the purchase back to its item's type.
        """
        with self.app.app_context():
            cat = db.session.get(MaterialCategory, self.cement_id)
            cat.status = 'inactive'
            db.session.commit()
        html = self.client.get(PURCHASES_URL).get_data(as_text=True)
        modal = table_region(html, '<!-- Edit Purchase Modal -->', '</form>')
        self.assertIn('Cement', modal,
                      "the edit form must be able to show the record's own type")
        self.form(f'{PURCHASES_URL}/{self.purchase_ids["cement"]}/edit',
                  supplier_id=self.supplier_ids['cement'],
                  material_id=self.material_ids['cement'],
                  unit_price='1300', quantity='200',
                  payment_status='unpaid', date=_pkt_today().isoformat(),
                  category_id=str(self.cement_id))
        with self.app.app_context():
            row = db.session.get(PurchaseV2, self.purchase_ids['cement'])
            self.assertEqual(int(row.category_id), self.cement_id,
                             'the retired item type must stay on the record')

    def test_a_new_purchase_cannot_be_filed_under_a_retired_type(self):
        """Nothing *new* may be filed under a retired item type."""
        with self.app.app_context():
            cat = db.session.get(MaterialCategory, self.steel_id)
            cat.status = 'inactive'
            db.session.commit()
        self.form(PURCHASES_URL,
                  supplier_id=self.supplier_ids['cement'],
                  material_id=str(self.material_ids['cement']),
                  quantity='10', unit_price='100',
                  payment_status='unpaid', date=_pkt_today().isoformat(),
                  **{'material_id[]': str(self.material_ids['cement']),
                     'quantity[]': '10', 'unit_price[]': '100',
                     'category_id[]': str(self.steel_id)})
        with self.app.app_context():
            row = (PurchaseV2.query
                   .filter(PurchaseV2.material_id == self.material_ids['cement'],
                           PurchaseV2.quantity == 10)
                   .order_by(PurchaseV2.id.desc()).first())
            self.assertIsNone(row, 'the retired type must have been refused')

    def test_material_edit_keeps_a_retired_item_type(self):
        """Same rule for the item itself."""
        with self.app.app_context():
            cat = db.session.get(MaterialCategory, self.cement_id)
            cat.status = 'inactive'
            db.session.commit()
        html = self.client.get('/hdc/purchase-v2/materials').get_data(as_text=True)
        modal = table_region(html, '<!-- Edit Material Modal -->', '</form>')
        self.assertIn('Cement', modal,
                      "the edit form must be able to show the item's own type")
        self.form(f'/hdc/purchase-v2/materials/{self.material_ids["cement"]}/edit',
                  name='Cement OPC', unit='BAG', status='active',
                  category_id=str(self.cement_id))
        with self.app.app_context():
            row = db.session.get(MaterialV2, self.material_ids['cement'])
            self.assertEqual(int(row.category_id), self.cement_id)

    def test_write_rule_helper_allows_keeping_but_not_choosing(self):
        """The rule itself, stated once: keep is allowed, choose is not."""
        self.assertIsNotNone(_material_category_for_write,
                             'the write rule must exist in one place')
        with self.app.app_context():
            cat = db.session.get(MaterialCategory, self.steel_id)
            cat.status = 'inactive'
            db.session.commit()
            self.assertIsNotNone(
                _material_category_for_write(self.steel_id, current_category_id=self.steel_id),
                'a record that already sits in the retired type may keep it')
            self.assertIsNone(
                _material_category_for_write(self.steel_id, current_category_id=None),
                'nothing new may be filed under a retired type')
            self.assertIsNotNone(
                _material_category_for_write(self.cement_id, current_category_id=None),
                'an active type is always allowed')

    # ── 8. a deleted type's name must be safe to type again ────────────────

    def test_retyping_a_deleted_category_name_does_not_crash_the_page(self):
        """The name of a deleted type may be typed again — without a 500.

        The lookup matched deleted rows as well, so a deleted "Tile" and a
        live "tile" made the page revive the deleted row and collide with
        the live one on the unique index.
        """
        with self.app.app_context():
            db.session.add(MaterialCategory(name='Tile', status='inactive', is_void=True,
                                            created_at=_pkt_now_naive(),
                                            updated_at=_pkt_now_naive()))
            db.session.commit()
        response = self.client.post(CATEGORIES_URL,
                                    data={'name': 'Tile', '_csrf_token': self.token})
        self.assertEqual(response.status_code, 302,
                         'the categories page must answer, not crash')
        with self.app.app_context():
            live = MaterialCategory.query.filter_by(name='Tile', is_void=False).all()
            self.assertLessEqual(len(live), 1,
                                 'there must never be two live types with one name')
            for row in live:
                self.assertEqual(row.status, 'active')

    def test_typing_a_live_name_again_does_not_create_a_second_row(self):
        with self.app.app_context():
            before = MaterialCategory.query.count()
        self.form(CATEGORIES_URL, name='cement')     # different case
        with self.app.app_context():
            self.assertEqual(MaterialCategory.query.count(), before,
                             'the name already exists (case-insensitively)')


if __name__ == '__main__':
    unittest.main()
