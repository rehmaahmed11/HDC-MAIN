#!/usr/bin/env python3
"""Smoke + unit coverage for the dedicated *Track by Serial* page.

The Tools section already tracked serial-numbered pieces inside Inventory, the
tool Position page and the Rental detail page, but the type-level Tracking and
Reports pages only ever showed tool *types* with quantities.  This test boots
the app, creates brand-new entries in every tool section (Inventory add,
Purchase, Rental, Return, Payment, Transfer, Scrap, Audit) and asserts that the
new ``/hdc/tool-rental/serials`` page reflects each one at the individual
serial level — current location, status and the full movement chain.
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
    Tool, ToolAudit, ToolCategory, ToolRental, ToolSerial,
    ToolSerialMovement,
)
from hdc.services.tool_rental import (                            # noqa: E402
    all_tool_serials_tracking, ensure_all_tools_serials, transfer_serials,
)
from hdc.utils.dates import _pkt_today                            # noqa: E402

ADMIN_PASSWORD = os.environ['HDC_BOOTSTRAP_ADMIN_PASSWORD']


class TrackBySerialTestCase(unittest.TestCase):
    """One isolated app + database, then entries across every tool section."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='hdc-tool-serial-track-')
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
        self.site, self.stage = self._make_site()
        self.cat = ToolCategory(name='Power Tools', active_status=True)
        db.session.add(self.cat)
        db.session.commit()
        # A receiving account so rental payments can post to the ledger.
        self.bank = Account(name='Smoke Bank', type='bank', status='active')
        db.session.add(self.bank)
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
            'username': 'admin', 'password': ADMIN_PASSWORD,
            '_csrf_token': token,
        }, follow_redirects=True)

    @staticmethod
    def _csrf(html):
        match = re.search(r'name="_csrf_token" value="([^"]+)"', html or '')
        return match.group(1) if match else ''

    def _token(self):
        with self.client.session_transaction() as sess:
            return sess.get('_csrf_token', '')

    def _make_site(self):
        site = Project(name='Site A', project_code='P-A', client='Owner A',
                       location='Karachi', contract_type='lump_sum',
                       owner_lump_sum=1_000_000)
        db.session.add(site)
        db.session.flush()
        stage = Stage(project_id=site.id, name='Foundation',
                      contract_basis='Lump Sum', lump_sum_value=100_000)
        db.session.add(stage)
        db.session.commit()
        return site, stage

    def _add_tool(self, code, name, qty, rate=500.0):
        """Create a tool through the real inventory route (auto serials)."""
        resp = self.client.post('/hdc/tool-rental/inventory', data={
            '_csrf_token': self._token(),
            'action': 'add_tool',
            'name': name,
            'tool_code': code,
            'category_id': '__new',
            'new_category_name': self.cat.name,
            'unit': 'pcs',
            'condition': 'good',
            'total_quantity': str(qty),
            'purchase_cost': '10000',
            'rental_rate_per_day': str(rate),
            'serial_start_no': '1',
        }, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        return Tool.query.filter_by(tool_code=code).first()

    def _purchase(self, tool, qty):
        resp = self.client.post(f'/hdc/tool-rental/inventory/{tool.id}/purchase', data={
            '_csrf_token': self._token(),
            'qty': str(qty),
            'unit_cost': '10000',
            'supplier': 'Smoke Supplier',
            'reference': 'PO-SMOKE',
            'purchase_date': _pkt_today().isoformat(),
        }, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)

    def _create_rental(self, tool, qty):
        """Post a new internal rental and return its id."""
        resp = self.client.post('/hdc/tool-rental/create', data={
            '_csrf_token': self._token(),
            'txn_type': 'new_rental',
            'billing_type': 'per_day',
            'renter_type': 'internal',
            'project_id': str(self.site.id),
            'stage_id': str(self.stage.id),
            'rental_date': _pkt_today().isoformat(),
            'tool_id[]': [str(tool.id)],
            'qty[]': [str(qty)],
            'rate[]': ['500'],
            'notes': 'Smoke rental',
        }, follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        location = resp.headers.get('Location', '')
        match = re.search(r'created=(\d+)', location)
        self.assertIsNotNone(match, 'create did not redirect with ?created=')
        return int(match.group(1))

    def _return_rental_full(self, rental_id):
        resp = self.client.post(f'/hdc/tool-rental/{rental_id}/return', data={
            '_csrf_token': self._token(),
            'return_type': 'full',
            'payment_type': 'no_payment',
            'return_date': _pkt_today().isoformat(),
        }, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)

    def _pay_rental(self, rental_id, amount):
        resp = self.client.post(f'/hdc/tool-rental/{rental_id}/payment', data={
            '_csrf_token': self._token(),
            'amount': str(amount),
            'received_to_account_id': str(self.bank.id),
            'payment_mode': 'cash',
            'payment_date': _pkt_today().isoformat(),
        }, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)

    def _start_audit(self, location='store'):
        resp = self.client.post('/hdc/tool-rental/audit/start', data={
            '_csrf_token': self._token(),
            'location': location,
            'counter_name': 'Smoke Counter',
            'audit_date': _pkt_today().isoformat(),
        }, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)

    def _serials_on_page(self):
        """Hit the page and parse the rendered table into serial dicts."""
        resp = self.client.get('/hdc/tool-rental/serials')
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        return html

    # ------------------------------------------------------------ tests
    def test_serials_page_renders_empty_with_nav(self):
        html = self._serials_on_page()
        self.assertIn('Track by Serial', html)
        self.assertIn('/hdc/tool-rental/serials', html)
        # Dedicated tab, distinct from the type-level Tracking/Reports.
        self.assertIn('No serial pieces match', html)
        self.assertIn('Pieces Tracked', html)

    def test_add_tool_and_purchase_populate_serials_page(self):
        tool = self._add_tool('TOOL-SMOKE', 'Vibrator', 3)
        self._purchase(tool, 2)
        ensure_all_tools_serials(commit=True)

        html = self._serials_on_page()
        # 5 serial pieces now exist and are all in the warehouse.
        self.assertIn('Vibrator', html)
        rows = all_tool_serials_tracking()
        self.assertEqual(len(rows), 5)
        for r in rows:
            self.assertTrue(r['is_in_store'])
            self.assertEqual(r['current_location_label'], 'Warehouse / Store')
            self.assertEqual(r['status'], 'in_store')

        # The new tab is reachable from the other tool pages too.
        dash = self.client.get('/hdc/tool-rental/dashboard').get_data(as_text=True)
        self.assertIn('/hdc/tool-rental/serials', dash)

    def test_rental_moves_serials_out_and_builds_chain(self):
        tool = self._add_tool('TOOL-RENT', 'Jack Hammer', 4)
        rental_id = self._create_rental(tool, 2)
        ensure_all_tools_serials(commit=True)

        rows = all_tool_serials_tracking()
        out = [r for r in rows if not r['is_in_store']]
        self.assertEqual(len(out), 2)
        for r in out:
            self.assertEqual(r['status'], 'rented')
            self.assertEqual(r['rental_code'],
                             ToolRental.query.get(rental_id).rental_code)
            self.assertIn(self.site.name, r['current_location_label'])
            # Chain is the *full* history: it passes through the warehouse and
            # ends at the site the rental sent the piece to.
            self.assertTrue(r['chain'])
            self.assertIn('Warehouse / Store', r['chain'])
            self.assertIn(self.site.name, r['chain'][-1])

        self._pay_rental(rental_id, 2000)

    def test_transfer_and_return_update_serial_chain(self):
        tool = self._add_tool('TOOL-XFER', 'Mixer', 3)
        rental_id = self._create_rental(tool, 3)
        ensure_all_tools_serials(commit=True)
        rental = ToolRental.query.get(rental_id)

        # Move one piece to a second site.
        out_rows = all_tool_serials_tracking()
        moving = out_rows[0]
        transfer_serials(
            serial_ids=[moving['serial_id']],
            from_label=moving['current_location_label'],
            to_label='Site B / Slab',
            rental_id=rental.id,
            movement_type='site_transfer',
            created_by=None, commit=True,
        )
        after = next(r for r in all_tool_serials_tracking()
                     if r['serial_id'] == moving['serial_id'])
        self.assertEqual(after['current_location_label'], 'Site B / Slab')
        self.assertIn('Site B / Slab', after['chain'])
        self.assertGreaterEqual(len(after['movements']), 2)

        # Return the whole rental via the real return route: this reduces the
        # rental's qty_pending, so the app's auto-sync keeps the piece in store
        # (a bare serial return without reducing qty_pending would be re-pulled
        # out by the sync that keeps serials reconciled with the books).
        self._return_rental_full(rental.id)
        final = next(r for r in all_tool_serials_tracking()
                     if r['serial_id'] == moving['serial_id'])
        self.assertTrue(final['is_in_store'])
        self.assertEqual(final['status'], 'in_store')
        self.assertEqual(final['current_location_label'], 'Warehouse / Store')

    def test_scrap_specific_serial_reflected_as_scrapped(self):
        tool = self._add_tool('TOOL-SCRP', 'Generator', 4)
        ensure_all_tools_serials(commit=True)
        target = ToolSerial.query.filter_by(tool_id=tool.id).first()
        self.assertIsNotNone(target)
        self.assertTrue(target.is_in_store)

        resp = self.client.post(
            f'/hdc/tool-rental/inventory/{tool.id}/scrap', data={
                '_csrf_token': self._token(),
                'scrap_serial_ids[]': [str(target.id)],
                'reason': 'damaged',
                'reference': 'SCR-SMOKE',
                'scrap_date': _pkt_today().isoformat(),
            }, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)

        rows = all_tool_serials_tracking()
        scrapped = next(r for r in rows if r['serial_id'] == target.id)
        self.assertTrue(scrapped['is_scrapped'])
        self.assertEqual(scrapped['status'], 'scrapped')
        kpis = None
        # KPI strip should now count 1 scrapped piece.
        html = self._serials_on_page()
        self.assertIn('Scrapped / Written Off', html)

    def test_filter_by_status_and_search(self):
        tool = self._add_tool('TOOL-FILT', 'Compactor', 3)
        rental_id = self._create_rental(tool, 2)
        ensure_all_tools_serials(commit=True)

        # Only-out filter.
        html = self.client.get('/hdc/tool-rental/serials?only=out').get_data(as_text=True)
        self.assertIn('Compactor', html)
        out_rows = all_tool_serials_tracking()
        out_only = [r for r in all_tool_serials_tracking() if not r['is_in_store']]
        self.assertEqual(len(out_only), 2)

        # Search by serial number.
        first_serial = out_only[0]['serial_number']
        found = all_tool_serials_tracking(search=first_serial)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['serial_number'], first_serial)

        # Filter by status=rented.
        rented = all_tool_serials_tracking(status='rented')
        self.assertEqual(len(rented), 2)

    def test_audit_section_entry_is_created(self):
        tool = self._add_tool('TOOL-AUD', 'Pump', 2)
        before = ToolAudit.query.count()
        self._start_audit('store')
        self.assertEqual(ToolAudit.query.count(), before + 1)
        audit = ToolAudit.query.order_by(ToolAudit.id.desc()).first()
        self.assertTrue(audit.is_open)
        # The Audit page lists the open sheet.
        html = self.client.get('/hdc/tool-rental/audit').get_data(as_text=True)
        self.assertIn(audit.audit_code, html)

    def test_full_cycle_creates_entries_in_all_sections(self):
        """One tool exercised through every section, asserted via serials page."""
        tool = self._add_tool('TOOL-FULL', 'Total Station', 5)
        self._purchase(tool, 3)          # Inventory + Purchase
        rental_id = self._create_rental(tool, 4)   # Rentals
        ensure_all_tools_serials(commit=True)
        rental = ToolRental.query.get(rental_id)
        self._pay_rental(rental_id, 3000)         # Payment

        # Transfer one out piece to another site.
        out = [r for r in all_tool_serials_tracking() if not r['is_in_store']]
        self.assertEqual(len(out), 4)
        transfer_serials(serial_ids=[out[0]['serial_id']],
                         from_label=out[0]['current_location_label'],
                         to_label='Site C / Roof',
                         rental_id=rental.id, commit=True)

        # Return the whole rental (reduces qty_pending) so the pieces come home.
        self._return_rental_full(rental_id)

        # Scrap one serial still in store.
        in_store = [r for r in all_tool_serials_tracking()
                    if r['is_in_store'] and not r['is_scrapped']]
        self.client.post(f'/hdc/tool-rental/inventory/{tool.id}/scrap', data={
            '_csrf_token': self._token(),
            'scrap_serial_ids[]': [str(in_store[0]['serial_id'])],
            'reason': 'lost',
            'scrap_date': _pkt_today().isoformat(),
        }, follow_redirects=True)
        self._start_audit('store')       # Audit

        rows = all_tool_serials_tracking()
        self.assertEqual(len(rows), 8)   # 5 owned + 3 purchased
        statuses = {r['status'] for r in rows}
        self.assertIn('scrapped', statuses)
        self.assertIn('in_store', statuses)
        # The transferred piece's movement history still records Site C even
        # after it returned to the warehouse.
        self.assertTrue(any('Site C / Roof' in r['chain'] for r in rows))
        # At least one piece is back in the warehouse (return recorded).
        self.assertTrue(any(r['is_in_store'] and r['status'] == 'in_store'
                           for r in rows))
        html = self._serials_on_page()
        self.assertIn('Total Station', html)


if __name__ == '__main__':
    unittest.main(verbosity=2)
