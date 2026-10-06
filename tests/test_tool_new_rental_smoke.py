#!/usr/bin/env python3
"""Smoke the HDC Tools *New Rental* form end-to-end.

The create form now lives on ``/hdc/tool-rental/new``.  These cases walk the
operator path the hub's **New Rental** action opens:

  1. the form renders (fields, tool picker, recent-rental list);
  2. posting a new rental stays on the form, flashes success, and tints the
     fresh row so the operator can see the result without hunting;
  3. Tracking (``/hdc/tool-rental/tracking``) shows the tools at the site or
     customer the form just sent them to;
  4. Reports (``/hdc/tool-rental/reports``) groups that rental under the same
     site/customer and lists the code, qty and unpaid rent.

Run with:
    HDC_BOOTSTRAP_ADMIN_PASSWORD='Admin@1234' \\
        python -m unittest tests.test_tool_new_rental_smoke -v
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
from hdc.models.projects import Project, Stage                    # noqa: E402
from hdc.models.tool_rental import (                              # noqa: E402
    Tool, ToolCategory, ToolMovementLog, ToolRental, ToolRentalItem,
)
from hdc.services.tool_tracking import (                          # noqa: E402
    tool_ledger, tools_reconciliation,
)
from hdc.utils.dates import _pkt_today                            # noqa: E402

ADMIN_PASSWORD = os.environ['HDC_BOOTSTRAP_ADMIN_PASSWORD']


class NewRentalFormSmokeTestCase(unittest.TestCase):
    """One isolated app + database for the New Rental form smoke."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='hdc-tool-new-rental-smoke-')
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
        self.vibrator = self._make_tool('TOOL-0001', 'Vibrator', 20, rate=500)
        self.jack = self._make_tool('TOOL-0002', 'Jack Hammer', 10, rate=1200)

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

    def _make_tool(self, code, name, qty, rate=500.0):
        tool = Tool(tool_code=code, name=name, unit='pcs',
                    category_id=self.cat.id, total_quantity=qty,
                    purchase_cost=10_000.0, rental_rate_per_day=rate,
                    condition='good', status='active', is_void=False)
        db.session.add(tool)
        db.session.commit()
        return tool

    def _post_new_rental(self, fields, follow=True):
        payload = {
            '_csrf_token': self._token(),
            'txn_type': 'new_rental',
            'billing_type': 'fixed_fee',
            'renter_type': 'internal',
            'rental_date': _pkt_today().isoformat(),
        }
        payload.update(fields)
        return self.client.post('/hdc/tool-rental/create', data=payload,
                                follow_redirects=follow)

    def _created_id_from_location(self, location):
        match = re.search(r'[?&]created=(\d+)', location or '')
        self.assertIsNotNone(match, 'create did not redirect with ?created=')
        return int(match.group(1))

    @staticmethod
    def _table_slice(html, marker):
        start = html.index(marker)
        end = html.index('</table>', start)
        return html[start:end]

    # ------------------------------------------------------------ the form
    def test_new_rental_form_renders_create_controls(self):
        response = self.client.get('/hdc/tool-rental/new')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('id="createRentalForm"', html)
        self.assertIn('Who is renting?', html)
        self.assertIn('Tools to rent', html)
        self.assertIn('name="txn_type"', html)
        self.assertIn('value="new_rental"', html)
        self.assertIn('name="tool_id[]"', html)
        self.assertIn('name="qty[]"', html)
        self.assertIn('Create Rental', html)
        self.assertIn('Vibrator (TOOL-0001)', html)
        self.assertIn('Jack Hammer (TOOL-0002)', html)
        self.assertIn('Site A', html)
        self.assertIn('id="recentRentals"', html)
        self.assertIn('No rentals yet', html)
        # Tracking / Reports stay one click away on the tools sub-nav.
        self.assertIn('/hdc/tool-rental/tracking', html)
        self.assertIn('/hdc/tool-rental/reports', html)

    def test_failed_create_stays_on_the_form_with_the_error(self):
        response = self._post_new_rental({
            'renter_type': 'internal', 'billing_type': 'fixed_fee',
        })
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('Select a project/site for internal rental.', html)
        self.assertIn('id="createRentalForm"', html)
        self.assertEqual(ToolRental.query.count(), 0)

        response = self._post_new_rental({
            'renter_type': 'internal', 'project_id': str(self.site.id),
            'billing_type': 'per_day',
        })
        html = response.get_data(as_text=True)
        self.assertIn('Add at least one tool item.', html)
        self.assertIn('id="createRentalForm"', html)
        self.assertEqual(ToolRental.query.count(), 0)

    # -------------------------------------------- create + smoke result
    def test_internal_rental_create_shows_smoke_result_on_the_form(self):
        redirect = self._post_new_rental({
            'renter_type': 'internal',
            'project_id': str(self.site.id),
            'stage_id': str(self.stage.id),
            'billing_type': 'per_day',
            'tool_id[]': [str(self.vibrator.id)],
            'qty[]': ['8'],
            'rate[]': ['500'],
            'notes': 'Smoke internal rental',
        }, follow=False)
        self.assertEqual(redirect.status_code, 302)
        location = redirect.headers.get('Location', '')
        self.assertTrue(location.endswith('/hdc/tool-rental/new')
                        or '/hdc/tool-rental/new?created=' in location)
        created_id = self._created_id_from_location(location)

        page = self.client.get(location)
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        rental = db.session.get(ToolRental, created_id)
        self.assertIsNotNone(rental)
        self.assertIn(f'Rental {rental.rental_code} created', html)
        self.assertIn('8 tools shown in the list below', html)
        self.assertIn('class="alert alert-success', html)
        self.assertIn('tool-rental-row is-new', html)
        self.assertIn('badge bg-success ms-1">New</span>', html)
        self.assertIn(rental.rental_code, html)
        self.assertIn('Site A', html)
        self.assertIn('Foundation', html)
        self.assertIn('8 still out', html)
        self.assertIn('4,000 PKR', html)

        self.assertEqual(rental.renter_type, 'internal')
        self.assertEqual(rental.project_id, self.site.id)
        self.assertEqual(rental.stage_id, self.stage.id)
        self.assertEqual(rental.billing_type, 'per_day')
        self.assertEqual(rental.status, 'active')
        self.assertEqual(rental.payment_status, 'unpaid')
        self.assertEqual(float(rental.total_rented_qty), 8.0)
        self.assertEqual(float(rental.total_amount), 4000.0)
        self.assertEqual(rental.notes, 'Smoke internal rental')
        item = rental.items[0]
        self.assertEqual(item.tool_id, self.vibrator.id)
        self.assertEqual(float(item.qty_pending), 8.0)
        log = ToolMovementLog.query.filter_by(
            rental_id=rental.id, movement_type='rental_out').one()
        self.assertEqual(log.from_location_label, 'Warehouse / Store')
        self.assertIn('Site A', log.to_location_label)
        self.assertIn('Foundation', log.to_location_label)

    def test_external_rental_create_shows_smoke_result_on_the_form(self):
        page = self._post_new_rental({
            'renter_type': 'external',
            'customer_name': 'Smoke Customer Co',
            'customer_phone': '0300-1112233',
            'customer_address': 'Korangi',
            'billing_type': 'fixed_fee',
            'tool_id[]': [str(self.jack.id)],
            'qty[]': ['3'],
            'rate[]': ['1200'],
        })
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        rental = ToolRental.query.filter_by(renter_type='external').one()
        self.assertIn(f'Rental {rental.rental_code} created', html)
        self.assertIn('3 tools shown in the list below', html)
        self.assertIn('tool-rental-row is-new', html)
        self.assertIn('Smoke Customer Co', html)
        self.assertIn('3,600 PKR', html)
        self.assertEqual(rental.customer_phone, '0300-1112233')
        self.assertEqual(float(rental.total_amount), 3600.0)
        self.assertEqual(float(rental.items[0].qty_pending), 3.0)

    # -------------------------------------------- tracking + reports
    def test_new_form_rentals_appear_on_tracking_and_reports(self):
        """The smoke result is not only a flash — Tracking and Reports must
        both list the same rentals the form just created."""
        internal = self._post_new_rental({
            'renter_type': 'internal',
            'project_id': str(self.site.id),
            'stage_id': str(self.stage.id),
            'billing_type': 'per_day',
            'tool_id[]': [str(self.vibrator.id)],
            'qty[]': ['8'],
            'rate[]': ['500'],
        }, follow=False)
        internal_id = self._created_id_from_location(
            internal.headers.get('Location', ''))
        external = self._post_new_rental({
            'renter_type': 'external',
            'customer_name': 'Smoke Customer Co',
            'billing_type': 'fixed_fee',
            'tool_id[]': [str(self.jack.id)],
            'qty[]': ['3'],
            'rate[]': ['1200'],
        }, follow=False)
        external_id = self._created_id_from_location(
            external.headers.get('Location', ''))
        internal_rental = db.session.get(ToolRental, internal_id)
        external_rental = db.session.get(ToolRental, external_id)

        recon = tools_reconciliation(tool_ledger())
        self.assertTrue(recon['balanced'])
        self.assertEqual(recon['owned'], 30.0)
        self.assertEqual(recon['own_project'], 8.0)
        self.assertEqual(recon['customer'], 3.0)
        self.assertEqual(recon['in_store'], 19.0)

        tracking = self.client.get('/hdc/tool-rental/tracking')
        self.assertEqual(tracking.status_code, 200)
        tracking_html = tracking.get_data(as_text=True)
        summary = self._table_slice(
            tracking_html,
            '<table class="table hdc-table table-sm mb-0 tool-tracking-table">')
        self.assertIn('Site A', summary)
        self.assertIn('Smoke Customer Co', summary)
        self.assertIn('Warehouse / Store', summary)
        self.assertIn('Vibrator ×8 pcs', summary)
        self.assertIn('Jack Hammer ×3 pcs', summary)
        self.assertIn('1 rental', summary)
        # Full holdings (rental codes, movement chain) live in the View dialog.
        self.assertIn(internal_rental.rental_code, tracking_html)
        self.assertIn(external_rental.rental_code, tracking_html)
        self.assertIn('Warehouse / Store', tracking_html)
        site_only = self.client.get(
            f'/hdc/tool-rental/tracking?project_id={self.site.id}')
        site_html = site_only.get_data(as_text=True)
        site_summary = self._table_slice(
            site_html,
            '<table class="table hdc-table table-sm mb-0 tool-tracking-table">')
        self.assertIn('Site A', site_summary)
        self.assertNotIn('Smoke Customer Co', site_summary)
        self.assertNotIn('Warehouse / Store', site_summary)

        reports = self.client.get('/hdc/tool-rental/reports')
        self.assertEqual(reports.status_code, 200)
        reports_html = reports.get_data(as_text=True)
        self.assertIn('Detailed Rentals — 2 records', reports_html)
        rentals_table = self._table_slice(
            reports_html,
            '<table class="table hdc-table table-sm mb-0 tool-report-rentals-table">')
        self.assertIn(internal_rental.rental_code, rentals_table)
        self.assertIn(external_rental.rental_code, rentals_table)
        self.assertIn('Site A', rentals_table)
        self.assertIn('Smoke Customer Co', rentals_table)
        self.assertIn('4,000', rentals_table)
        self.assertIn('3,600', rentals_table)
        locations = self._table_slice(
            reports_html,
            '<table class="table hdc-table table-sm mb-0 tool-report-location-table">')
        self.assertIn('Site A', locations)
        self.assertIn('Smoke Customer Co', locations)
        self.assertIn('Client: Owner A', locations)
        # View dialogs keep the tool lines and the rental codes.
        self.assertIn('Vibrator', reports_html)
        self.assertIn('Jack Hammer', reports_html)
        self.assertEqual(
            reports_html.count('class="modal fade tool-report-location-modal"'), 2)

        by_customer = self.client.get(
            '/hdc/tool-rental/reports?renter_type=external')
        customer_html = by_customer.get_data(as_text=True)
        self.assertIn('Detailed Rentals — 1 records', customer_html)
        self.assertIn(external_rental.rental_code, customer_html)
        self.assertNotIn(internal_rental.rental_code, customer_html)
        self.assertIn('Smoke Customer Co', customer_html)

        by_site = self.client.get(
            f'/hdc/tool-rental/reports?project_id={self.site.id}')
        site_reports = by_site.get_data(as_text=True)
        self.assertIn(internal_rental.rental_code, site_reports)
        self.assertNotIn(external_rental.rental_code, site_reports)

    def test_two_tools_on_one_form_post_stay_together_in_tracking_and_reports(self):
        page = self._post_new_rental({
            'renter_type': 'internal',
            'project_id': str(self.site.id),
            'billing_type': 'fixed_fee',
            'tool_id[]': [str(self.vibrator.id), str(self.jack.id)],
            'qty[]': ['5', '2'],
            'rate[]': ['500', '1200'],
        })
        html = page.get_data(as_text=True)
        rental = ToolRental.query.one()
        self.assertIn(f'Rental {rental.rental_code} created', html)
        self.assertEqual(float(rental.total_rented_qty), 7.0)
        self.assertEqual(float(rental.total_amount), 5 * 500 + 2 * 1200)
        self.assertEqual(ToolRentalItem.query.filter_by(rental_id=rental.id).count(), 2)
        self.assertEqual(
            ToolMovementLog.query.filter_by(rental_id=rental.id).count(), 2)

        tracking = self.client.get('/hdc/tool-rental/tracking').get_data(as_text=True)
        summary = self._table_slice(
            tracking, '<table class="table hdc-table table-sm mb-0 tool-tracking-table">')
        self.assertIn('Site A', summary)
        self.assertIn('2 tool types', summary)
        self.assertIn('Vibrator ×5 pcs', summary)
        self.assertIn('Jack Hammer ×2 pcs', summary)

        reports = self.client.get('/hdc/tool-rental/reports').get_data(as_text=True)
        self.assertIn(rental.rental_code, reports)
        self.assertIn('2 type(s)', reports)
        locations = self._table_slice(
            reports,
            '<table class="table hdc-table table-sm mb-0 tool-report-location-table">')
        self.assertIn('Site A', locations)
        self.assertIn('>7<', locations)


if __name__ == '__main__':
    unittest.main(verbosity=2)
