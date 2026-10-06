#!/usr/bin/env python3
"""The "every name field is searchable" rule, enforced on the HDC Tools forms.

Rule: any field that asks the operator to supply a *name* — a customer, a
supplier, an account, a party — must render as a type-to-search combo box
(input + hidden source select wired by ``static/hdc/js/core/combo.js``), never
a bare ``<input type="text">`` and never a bare ``<select>``.

The tools return / transfer / purchase forms were the worst offenders: the
customer name on a new rental, the "To Customer Name" on a site transfer and
the supplier on a purchase were all free-text boxes with no way to see who had
been rented to before.  This module pins the fix and keeps it from regressing:

  * every name field on those pages renders the combo pair (visible input with
    ``data-hdc-combo`` + hidden source ``<select>``);
  * the widget is auto-wired by core/combo.js, so no per-page script can drift;
  * the field still posts: picking from the list, typing a brand-new name, and
    posting an id-valued list (the receiving account) all reach the server;
  * the lookup services return the distinct names the combos offer.

Run with:
    HDC_BOOTSTRAP_ADMIN_PASSWORD='Admin@1234' \\
        python -m unittest tests.test_name_combo_fields -v
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
from hdc.extensions import db                                     # noqa: E402
from hdc.models.accounts import Account                           # noqa: E402
from hdc.models.projects import Project, Stage                    # noqa: E402
from hdc.models.tool_rental import (                              # noqa: E402
    Tool, ToolCategory, ToolRental,
)
from hdc.services.tool_rental import (                            # noqa: E402
    known_tool_customers, known_tool_suppliers,
)
from hdc.utils.dates import _pkt_today                            # noqa: E402

ADMIN_PASSWORD = os.environ['HDC_BOOTSTRAP_ADMIN_PASSWORD']
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

#: page -> {field the form posts: combo input id that must sit next to it}
NAME_FIELDS = {
    '/hdc/tool-rental': {'q': 'rentalSearch'},
    '/hdc/tool-rental/new': {'customer_name': 'rentalCustomerName'},
    '/hdc/tool-rental/<id>': {'to_customer_name': 'transferCustomerName'},
    '/hdc/tool-rental/inventory': {'supplier': 'toolSupplier'},
    '/hdc/tool-rental/tool/<id>': {'supplier': 'purchaseModalSupplier'},
    '/hdc/tool-rental/reports': {'q': 'toolReportSearch'},
}
#: closed-set lists: the select keeps name= and posts the chosen value, so
#: the visible input carries no name of its own.
#:
#: ``source_rental_id`` is deliberately absent for /hdc/tool-rental/new: a site
#: can hold tools under several rentals, so the transfer form now lists every
#: holder as a ``source_rental_id[]`` checkbox with a "Select all holders"
#: toggle (pinned by tests/test_tool_tracking.py) instead of forcing one choice.
ID_FIELDS = {
    '/hdc/tool-rental/new': {
        'project_id': ('rentalProjectSelect',),
        'stage_id': ('rentalStageSelect',),
        'tool_id[]': ('rentalToolSelect0',),
        'received_to_account_id': ('recvAccount',),
    },
    '/hdc/tool-rental/<id>': {
        'to_project_id': ('transferProjectSelect',),
        'to_stage_id': ('transferStageSelect',),
        'received_to_account_id': ('recvAccReturn', 'recvAccPayment'),
    },
    '/hdc/tool-rental/inventory': {
        'tool_id': ('purchaseModalTool', 'scrapModalTool'),
    },
    '/hdc/tool-rental/tracking': {
        'tool_id': ('trackingTool',),
        'project_id': ('trackingProject',),
    },
    '/hdc/tool-rental/reports': {
        'tool_id': ('reportTool',),
        'project_id': ('reportProject',),
    },
}


class NameComboFieldTestCase(unittest.TestCase):
    """One isolated app + database per test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='hdc-name-combo-')
        self.db_path = os.path.join(self.tmp, 'test.db')
        self.app = create_app({'HDC_DB_PATH': self.db_path,
                               'HDC_INSTANCE_DIR': self.tmp})
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()
        self._login()
        self.site = Project(name='Site A', project_code='P-A', client='Owner A')
        db.session.add(self.site)
        self.cat = ToolCategory(name='Power Tools', active_status=True)
        db.session.add(self.cat)
        # bootstrap already seeds a company cash account
        self.cash = Account.query.filter_by(type='cash').first()
        if self.cash is None:
            self.cash = Account(name='Audit Cash Box', type='cash', status='active')
            db.session.add(self.cash)
            db.session.commit()
        self.tool = Tool(tool_code='TOOL-0001', name='Vibrator', unit='pcs',
                         category_id=self.cat.id, total_quantity=10,
                         purchase_cost=10_000, rental_rate_per_day=500,
                         status='active', is_void=False)
        db.session.add(self.tool)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ helpers
    def _login(self):
        r = self.client.get('/hdc/login')
        token = re.search(r'name="_csrf_token" value="([^"]+)"',
                          r.get_data(as_text=True)).group(1)
        self.client.post('/hdc/login', data={
            'username': 'admin', 'password': ADMIN_PASSWORD, '_csrf_token': token,
        }, follow_redirects=True)

    def _token(self):
        with self.client.session_transaction() as sess:
            return sess.get('_csrf_token', '')

    def _rental(self, customer='Bilal Traders'):
        rental = ToolRental(
            rental_code='RENT-%05d' % (ToolRental.query.count() + 1),
            renter_type='external',
            customer_name=customer, customer_phone='0300-1234567',
            rental_date=_pkt_today(), billing_type='fixed_fee',
            status='active', payment_status='unpaid', is_void=False)
        db.session.add(rental)
        db.session.flush()
        from hdc.models.tool_rental import ToolRentalItem
        db.session.add(ToolRentalItem(rental_id=rental.id, tool_id=self.tool.id,
                                      qty_rented=4, qty_returned=0, qty_pending=4,
                                      rate=500, amount=2000))
        rental.total_rented_qty = 4
        rental.total_amount = 2000
        db.session.commit()
        return rental

    # ------------------------------------------------- the markup contract
    def test_every_tools_page_renders_a_combo_for_every_name_field(self):
        rental = self._rental()
        urls = {
            '/hdc/tool-rental': '/hdc/tool-rental',
            '/hdc/tool-rental/new': '/hdc/tool-rental/new',
            '/hdc/tool-rental/<id>': '/hdc/tool-rental/%d' % rental.id,
            '/hdc/tool-rental/inventory': '/hdc/tool-rental/inventory',
            '/hdc/tool-rental/tool/<id>': '/hdc/tool-rental/tool/%d' % self.tool.id,
            '/hdc/tool-rental/tracking': '/hdc/tool-rental/tracking',
            '/hdc/tool-rental/reports': '/hdc/tool-rental/reports',
        }
        for key, url in urls.items():
            html = self.client.get(url).get_data(as_text=True)
            for field_name, input_id in NAME_FIELDS.get(key, {}).items():
                self._assert_combo_pair(html, field_name, input_id)
            for field_name, input_ids in ID_FIELDS.get(key, {}).items():
                for input_id in input_ids:
                    self._assert_combo_pair(html, field_name, input_id,
                                            input_carries_name=False)
            if key == '/hdc/tool-rental/inventory':
                self.assertIn('data-hdc-combo="toolCategorySelect"', html)
                self.assertIn('data-hdc-combo="inventoryCategorySelect"', html)
                self.assertIn('data-hdc-combo-source', html,
                              'manual category combos retain the no-script select fallback')

    def _assert_combo_pair(self, html, field_name, input_id,
                           input_carries_name=True):
        """The visible input, its source select, and the auto-wire hook."""
        input_tag = re.search(r'<input[^>]*id="%sInput"[^>]*>' % re.escape(input_id), html)
        self.assertIsNotNone(input_tag, '%s: no combo input rendered' % input_id)
        if input_carries_name:
            self.assertIn('name="%s"' % field_name, input_tag.group(),
                          '%s: the combo input must carry name=%s so the form '
                          'posts what the operator typed' % (input_id, field_name))
        self.assertIn('data-hdc-combo="%s"' % input_id, input_tag.group(),
                      '%s: no data-hdc-combo hook — core/combo.js will never '
                      'wire this pair' % input_id)
        select_tag = re.search(r'<select[^>]*id="%s"[^>]*>' % re.escape(input_id), html)
        self.assertIsNotNone(select_tag, '%s: no source select to mirror' % input_id)
        if input_carries_name:
            self.assertNotIn('name="%s"' % field_name, select_tag.group(),
                             '%s: the hidden select must not also carry name=%s '
                             '(the form would post the field twice)'
                             % (input_id, field_name))
        else:
            self.assertNotIn('name="%s"' % field_name, input_tag.group(),
                             '%s: the visible id-picker input must not post an id' % input_id)
            self.assertIn('name="%s"' % field_name, select_tag.group(),
                          '%s: the source select must post the chosen record id' % input_id)
        self.assertIn('data-hdc-combo-source', select_tag.group(),
                      '%s: the no-JS fallback in base.html keys off '
                      'data-hdc-combo-source' % input_id)

    def test_fixed_enumerations_remain_native_selects(self):
        rental = self._rental()
        pages = {
            '/hdc/tool-rental': ('status',),
            '/hdc/tool-rental/new': ('txn_type', 'billing_type', 'renter_type', 'payment_mode'),
            '/hdc/tool-rental/%d' % rental.id: ('payment_mode',),
            '/hdc/tool-rental/reports': ('renter_type', 'status', 'billing_type'),
        }
        for url, field_names in pages.items():
            html = self.client.get(url).get_data(as_text=True)
            for field_name in field_names:
                self.assertRegex(
                    html,
                    r'<select[^>]*name="%s"' % re.escape(field_name),
                    '%s on %s should remain a fixed-choice native select' % (field_name, url),
                )

    def test_site_client_stage_and_tool_choices_are_all_searchable(self):
        stage = Stage(project_id=self.site.id, name='Foundation')
        db.session.add(stage)
        db.session.commit()

        html = self.client.get('/hdc/tool-rental/new').get_data(as_text=True)
        self.assertIn('Client: Owner A', html,
                      'searching a client name should find its site/project option')
        self.assertIn('Foundation — Site A (P-A) — Client: Owner A', html)
        self.assertIn('data-project-id="%s"' % self.site.id, html,
                      'stage options must retain their parent-site filter metadata')
        self.assertIn('Vibrator (TOOL-0001)', html,
                      'the repeated tool picker must include searchable tool labels')
        self.assertIn('data-hdc-combo-manual="true"', html,
                      'dynamic tool rows must attach the strict combo themselves')
        self.assertIn('data-hdc-combo="transferToolSearchSelect"', html,
                      'the transfer multi-select also exposes its available tool names to the shared combo')
        transfer_picker_select = re.search(
            r'<select[^>]*id="transferToolSearchSelect"[^>]*>', html)
        self.assertIsNotNone(transfer_picker_select)
        self.assertNotIn('name=', transfer_picker_select.group(),
                         'transfer tool checkboxes, not the search helper, submit selected rows')
        self.assertIn('checkbox.checked = true', html,
                      'choosing a transfer tool suggestion should select its checkbox row')
        self.assertIn('strict: true', html)

    def test_no_bare_text_box_is_left_for_a_name_field(self):
        """A name field must not regress to a plain <input type="text">."""
        for url in ('/hdc/tool-rental/new',
                    '/hdc/tool-rental/%d' % self._rental().id):
            html = self.client.get(url).get_data(as_text=True)
            for field_name in ('customer_name', 'to_customer_name'):
                for tag in re.findall(r'<input[^>]*name="%s"[^>]*>' % field_name, html):
                    self.assertIn('data-hdc-combo', tag,
                                  '%s is a plain text box again on %s'
                                  % (field_name, url))

    def test_combo_js_autowires_data_hdc_combo_inputs(self):
        """The widget wires the pair itself; no page script is required."""
        with open(os.path.join(REPO_ROOT, 'static/hdc/js/core/combo.js'),
                  encoding='utf-8') as fh:
            source = fh.read()
        self.assertIn("querySelectorAll('input[data-hdc-combo]')", source)
        self.assertIn('DOMContentLoaded', source)
        self.assertIn("data-hdc-combo-strict", source)
        self.assertIn("data-hdc-combo-manual", source)
        self.assertIn('o.hidden', source)
        self.assertIn("select.addEventListener('change'", source)

    def test_no_script_fallback_preserves_the_named_combo_control(self):
        with open(os.path.join(REPO_ROOT, 'templates/hdc/shared/base.html'),
                  encoding='utf-8') as fh:
            source = fh.read()
        self.assertIn('select[data-hdc-combo-source][name]', source)
        self.assertIn('input[data-hdc-combo][data-hdc-combo-strict="true"]', source)
        # Name-valued combos own name= on the input, so they must not be hidden
        # when JavaScript is unavailable.
        self.assertNotIn('input[data-hdc-combo] { display: none !important; }', source)

    # ------------------------------------------------------ the post paths
    def test_new_customer_name_still_posts(self):
        token = self._token()
        r = self.client.post('/hdc/tool-rental/create', data={
            '_csrf_token': token,
            'renter_type': 'external',
            'billing_type': 'fixed_fee',
            'customer_name': 'Zeesab Hardware',   # typed, not in the list
            'customer_phone': '0321-9999999',
            'rental_date': _pkt_today().isoformat(),
            'tool_id[]': [str(self.tool.id)],
            'qty[]': ['2'],
            'rate[]': ['500'],
        }, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        rental = ToolRental.query.filter_by(renter_type='external').first()
        self.assertIsNotNone(rental, 'the external rental was not created')
        self.assertEqual(rental.customer_name, 'Zeesab Hardware')

    def test_known_customer_name_posts_from_the_combo(self):
        """Picking an existing name posts the label the operator chose."""
        self._rental(customer='Bilal Traders')
        token = self._token()
        self.client.post('/hdc/tool-rental/create', data={
            '_csrf_token': token,
            'renter_type': 'external',
            'billing_type': 'fixed_fee',
            'customer_name': 'Bilal Traders',
            'rental_date': _pkt_today().isoformat(),
            'tool_id[]': [str(self.tool.id)],
            'qty[]': ['1'],
            'rate[]': ['500'],
        }, follow_redirects=True)
        names = [r.customer_name for r in ToolRental.query.filter_by(
            renter_type='external').all()]
        self.assertEqual(names.count('Bilal Traders'), 2)

    def test_transfer_to_a_customer_posts_the_name(self):
        rental = self._rental()
        token = self._token()
        r = self.client.post('/hdc/tool-rental/%d/transfer' % rental.id, data={
            '_csrf_token': token,
            'to_type': 'customer',
            'to_customer_name': 'Chishti Builders',
            'transfer_date': _pkt_today().isoformat(),
        }, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        transfer = rental.transfers[0]
        self.assertEqual(transfer.to_customer_name, 'Chishti Builders')
        self.assertEqual(transfer.to_location_label, 'Chishti Builders')

    def test_return_still_posts_the_receiving_account_id(self):
        rental = self._rental()
        token = self._token()
        r = self.client.post('/hdc/tool-rental/%d/return' % rental.id, data={
            '_csrf_token': token,
            'return_date': _pkt_today().isoformat(),
            'return_condition': 'full',
            'payment_condition': 'full',
            'amount_paid': '2000',
            'payment_mode': 'cash',
            'received_to_account_id': str(self.cash.id),
        }, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        db.session.refresh(rental)
        self.assertGreater(float(rental.total_returned_qty or 0), 0)
        self.assertEqual(float(rental.total_paid or 0), 2000.0)

    # ------------------------------------------------------ the lookups
    def test_known_customers_lists_every_renter_once(self):
        self._rental(customer='Bilal Traders')
        self._rental(customer='bilal traders')     # same shop, typed sloppily
        self._rental(customer='Zeesab Hardware')
        names = known_tool_customers()
        self.assertEqual(len(names), 2, names)
        self.assertIn('Zeesab Hardware', names)
        self.assertIn('Bilal Traders', names)

    def test_known_customers_skips_blank_and_voided(self):
        self._rental(customer='   ')
        self.assertEqual(known_tool_customers(), [])

    def test_known_suppliers_merges_purchases_and_supplier_master(self):
        from hdc.services.tool_rental import record_tool_purchase
        record_tool_purchase(self.tool.id, 2, unit_cost=100, supplier='Karachi Tools House',
                             purchase_date=_pkt_today().isoformat())
        from hdc.models.materials import Supplier
        db.session.add(Supplier(name='Ali Steel Mart', phone='0300'))
        db.session.commit()
        names = known_tool_suppliers()
        self.assertIn('Karachi Tools House', names)
        self.assertIn('Ali Steel Mart', names)
        self.assertEqual(names, sorted(names, key=lambda n: n.lower()))


if __name__ == '__main__':
    unittest.main()
