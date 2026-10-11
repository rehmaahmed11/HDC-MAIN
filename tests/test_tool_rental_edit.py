#!/usr/bin/env python3
"""Tests for the HDC Tools *Edit Rental* form.

The complaint these tests pin: a brand-new rental could only be changed
through forms with far fewer fields than the one that created it.  The fix
makes the edit form *be* the new-rental form:

  1. ``GET /hdc/tool-rental/<id>/edit`` renders the very template the create
     form uses — every create field is present, prefilled with the rental's
     saved values;
  2. saving applies the header details (renter, destination, rent type,
     dates, notes) and reconciles the tool lines: resized lines move their
     serial markings with them, removed lines come back to the store, added
     lines go out like a fresh create — and the stock ledger stays balanced;
  3. once a return, transfer, payment or discount exists, the dangerous parts
     lock with a clear message instead of silently breaking history.

Run with:
    HDC_BOOTSTRAP_ADMIN_PASSWORD='Admin@1234' \\
        python -m unittest tests.test_tool_rental_edit -v
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
    Tool, ToolCategory, ToolMovementLog, ToolRental, ToolRentalItem,
    ToolSerial,
)
from hdc.services.tool_tracking import (                          # noqa: E402
    tool_ledger, tools_reconciliation,
)
from hdc.utils.dates import _pkt_today                            # noqa: E402

ADMIN_PASSWORD = os.environ['HDC_BOOTSTRAP_ADMIN_PASSWORD']

#: The fields the create form posts.  The edit form must offer every one of
#: them — that parity *is* the feature under test.
CORE_FORM_FIELDS = (
    'txn_type', 'renter_type', 'project_id', 'stage_id',
    'customer_name', 'customer_phone', 'customer_address',
    'billing_type', 'rental_date', 'expected_return_date', 'notes',
    'tool_id[]', 'qty[]', 'rate[]',
)


class ToolRentalEditTestCase(unittest.TestCase):
    """One isolated app + database for the rental edit form."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='hdc-tool-rental-edit-')
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
        self.site_b = Project(name='Site B', project_code='P-B',
                              client='Owner B', location='Lahore',
                              contract_type='lump_sum', owner_lump_sum=500_000)
        db.session.add(self.site_b)
        db.session.commit()
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

    def _post_new_rental(self, fields, follow=False):
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

    def _create_rental_id(self, fields):
        redirect = self._post_new_rental(fields)
        self.assertEqual(redirect.status_code, 302)
        match = re.search(r'[?&]created=(\d+)', redirect.headers.get('Location', ''))
        self.assertIsNotNone(match, 'create did not redirect with ?created=')
        return int(match.group(1))

    def _post_edit(self, rental_id, fields, follow=True):
        payload = {
            '_csrf_token': self._token(),
            'txn_type': 'new_rental',
            'billing_type': 'fixed_fee',
            'renter_type': 'internal',
            'rental_date': _pkt_today().isoformat(),
        }
        payload.update(fields)
        return self.client.post(f'/hdc/tool-rental/{rental_id}/update',
                                data=payload, follow_redirects=follow)

    @staticmethod
    def _serial_counts(tool_id):
        rows = ToolSerial.query.filter_by(tool_id=tool_id).all()
        in_store = sum(1 for s in rows if s.is_in_store)
        out = sum(1 for s in rows if not s.is_in_store)
        return in_store, out

    # ----------------------------------------------- the edit form itself
    def test_edit_form_is_the_create_form_prefilled(self):
        rental_id = self._create_rental_id({
            'renter_type': 'external',
            'customer_name': 'Edit Customer Co',
            'customer_phone': '0300-9998877',
            'customer_address': 'Korangi Road',
            'billing_type': 'per_day',
            'expected_return_date': '2026-12-31',
            'tool_id[]': [str(self.vibrator.id)],
            'qty[]': ['8'],
            'rate[]': ['500'],
            'notes': 'original agreement terms',
        })
        rental = db.session.get(ToolRental, rental_id)

        create_html = self.client.get('/hdc/tool-rental/new').get_data(as_text=True)
        edit_response = self.client.get(f'/hdc/tool-rental/{rental_id}/edit')
        self.assertEqual(edit_response.status_code, 200)
        edit_html = edit_response.get_data(as_text=True)

        # Every field the create form posts is present on the edit form.
        for field in CORE_FORM_FIELDS:
            self.assertIn(f'name="{field}"', create_html, field)
            self.assertIn(f'name="{field}"', edit_html,
                          f'edit form is missing create field {field}')

        # …and the edit form posts to the update endpoint with saved values.
        self.assertIn(f'action="/hdc/tool-rental/{rental_id}/update"', edit_html)
        self.assertIn('Save Changes', edit_html)
        self.assertIn(rental.rental_code, edit_html)
        self.assertIn('<option value="external" selected>Outside customer</option>',
                      edit_html)
        self.assertIn('value="Edit Customer Co"', edit_html)
        self.assertIn('value="0300-9998877"', edit_html)
        self.assertIn('value="Korangi Road"', edit_html)
        self.assertIn('<option value="per_day" selected>Per day</option>', edit_html)
        self.assertIn('value="2026-12-31"', edit_html)
        self.assertIn('original agreement terms', edit_html)
        # The saved line is prefilled with its quantity, rate and identity.
        self.assertIn('value="8"', edit_html)
        self.assertIn('value="500"', edit_html)
        self.assertIn(f'value="{rental.items[0].id}"', edit_html)
        self.assertIn(f'<option value="{self.vibrator.id}"', edit_html)
        self.assertIn('selected>Vibrator (TOOL-0001)', edit_html)
        # Its out-of-store Tool No markings are listed on the line.
        out_serials = ToolSerial.query.filter_by(
            tool_id=self.vibrator.id, is_in_store=False).all()
        self.assertEqual(len(out_serials), 8)
        for serial in out_serials:
            self.assertIn(serial.serial_number, edit_html)
        # The detail page and the rentals hub both link to the edit form.
        detail_html = self.client.get(
            f'/hdc/tool-rental/{rental_id}').get_data(as_text=True)
        self.assertIn(f'/hdc/tool-rental/{rental_id}/edit', detail_html)
        hub_html = self.client.get('/hdc/tool-rental').get_data(as_text=True)
        self.assertIn(f'/hdc/tool-rental/{rental_id}/edit', hub_html)

    # ----------------------------------------------- header detail updates
    def test_edit_updates_header_details(self):
        rental_id = self._create_rental_id({
            'renter_type': 'internal',
            'project_id': str(self.site.id),
            'stage_id': str(self.stage.id),
            'billing_type': 'fixed_fee',
            'tool_id[]': [str(self.vibrator.id)],
            'qty[]': ['4'],
            'rate[]': ['500'],
        })
        item_id = db.session.get(ToolRental, rental_id).items[0].id
        response = self._post_edit(rental_id, {
            'renter_type': 'internal',
            'project_id': str(self.site_b.id),
            'billing_type': 'fixed_fee',
            'rental_date': '2026-10-01',
            'expected_return_date': '2026-11-15',
            'notes': 'corrected destination',
            'tool_id[]': [str(self.vibrator.id)],
            'qty[]': ['4'],
            'rate[]': ['500'],
            'rental_item_id[]': [str(item_id)],
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn(f'Rental {db.session.get(ToolRental, rental_id).rental_code} updated',
                      response.get_data(as_text=True))
        rental = db.session.get(ToolRental, rental_id)
        self.assertEqual(rental.project_id, self.site_b.id)
        self.assertIsNone(rental.stage_id)
        self.assertEqual(rental.rental_date.isoformat(), '2026-10-01')
        self.assertEqual(rental.expected_return_date.isoformat(), '2026-11-15')
        self.assertEqual(rental.notes, 'corrected destination')
        # The line itself is untouched.
        self.assertEqual(float(rental.items[0].qty_rented), 4.0)
        self.assertEqual(float(rental.total_amount), 2000.0)
        self.assertTrue(tools_reconciliation(tool_ledger())['balanced'])

    def test_edit_switches_internal_rental_to_external_customer(self):
        rental_id = self._create_rental_id({
            'renter_type': 'internal',
            'project_id': str(self.site.id),
            'billing_type': 'fixed_fee',
            'tool_id[]': [str(self.jack.id)],
            'qty[]': ['2'],
            'rate[]': ['1200'],
        })
        item_id = db.session.get(ToolRental, rental_id).items[0].id
        response = self._post_edit(rental_id, {
            'renter_type': 'external',
            'customer_name': 'Switched Customer',
            'customer_phone': '0311-1234567',
            'billing_type': 'fixed_fee',
            'tool_id[]': [str(self.jack.id)],
            'qty[]': ['2'],
            'rate[]': ['1200'],
            'rental_item_id[]': [str(item_id)],
        })
        self.assertEqual(response.status_code, 200)
        rental = db.session.get(ToolRental, rental_id)
        self.assertEqual(rental.renter_type, 'external')
        self.assertEqual(rental.customer_name, 'Switched Customer')
        self.assertEqual(rental.customer_phone, '0311-1234567')
        self.assertIsNone(rental.project_id)
        # The pieces now sit with the new customer, ledger still balanced.
        out_serials = ToolSerial.query.filter_by(
            tool_id=self.jack.id, is_in_store=False).all()
        self.assertEqual(len(out_serials), 2)
        for serial in out_serials:
            self.assertEqual(serial.current_location_label, 'Switched Customer')
        self.assertTrue(tools_reconciliation(tool_ledger())['balanced'])

    # ----------------------------------------------- tool line reconciliation
    def test_edit_reduces_quantity_and_returns_serials_to_store(self):
        rental_id = self._create_rental_id({
            'renter_type': 'internal',
            'project_id': str(self.site.id),
            'billing_type': 'per_day',
            'tool_id[]': [str(self.vibrator.id)],
            'qty[]': ['8'],
            'rate[]': ['500'],
        })
        rental = db.session.get(ToolRental, rental_id)
        item_id = rental.items[0].id
        self.assertEqual(self._serial_counts(self.vibrator.id), (12, 8))

        response = self._post_edit(rental_id, {
            'renter_type': 'internal',
            'project_id': str(self.site.id),
            'billing_type': 'per_day',
            'tool_id[]': [str(self.vibrator.id)],
            'qty[]': ['5'],
            'rate[]': ['600'],
            'rental_item_id[]': [str(item_id)],
        })
        self.assertEqual(response.status_code, 200)

        rental = db.session.get(ToolRental, rental_id)
        self.assertEqual(float(rental.total_rented_qty), 5.0)
        self.assertEqual(float(rental.total_pending_tools), 5.0)
        self.assertEqual(float(rental.total_amount), 5 * 600)
        self.assertEqual(float(rental.items[0].rate), 600.0)
        self.assertEqual(self._serial_counts(self.vibrator.id), (15, 5))
        # The reduction is visible in the movement log, and the ledger holds.
        correction = ToolMovementLog.query.filter_by(
            rental_id=rental_id, movement_type='return_in').one()
        self.assertEqual(float(correction.qty), 3.0)
        self.assertIn('edited', correction.notes)
        self.assertTrue(tools_reconciliation(tool_ledger())['balanced'])

    def test_edit_increases_quantity_and_takes_more_from_store(self):
        rental_id = self._create_rental_id({
            'renter_type': 'internal',
            'project_id': str(self.site.id),
            'billing_type': 'fixed_fee',
            'tool_id[]': [str(self.vibrator.id)],
            'qty[]': ['2'],
            'rate[]': ['500'],
        })
        item_id = db.session.get(ToolRental, rental_id).items[0].id
        self.assertEqual(self._serial_counts(self.vibrator.id), (18, 2))

        self._post_edit(rental_id, {
            'renter_type': 'internal',
            'project_id': str(self.site.id),
            'billing_type': 'fixed_fee',
            'tool_id[]': [str(self.vibrator.id)],
            'qty[]': ['6'],
            'rate[]': ['500'],
            'rental_item_id[]': [str(item_id)],
        })
        rental = db.session.get(ToolRental, rental_id)
        self.assertEqual(float(rental.total_rented_qty), 6.0)
        self.assertEqual(float(rental.total_amount), 3000.0)
        self.assertEqual(self._serial_counts(self.vibrator.id), (14, 6))
        increase = ToolMovementLog.query.filter_by(
            rental_id=rental_id, movement_type='rental_out').order_by(
            ToolMovementLog.id.desc()).first()
        self.assertEqual(float(increase.qty), 4.0)
        self.assertIn('edited', increase.notes)
        self.assertTrue(tools_reconciliation(tool_ledger())['balanced'])

    def test_edit_adds_and_removes_tool_lines(self):
        rental_id = self._create_rental_id({
            'renter_type': 'internal',
            'project_id': str(self.site.id),
            'billing_type': 'fixed_fee',
            'tool_id[]': [str(self.vibrator.id), str(self.jack.id)],
            'qty[]': ['5', '2'],
            'rate[]': ['500', '1200'],
        })
        rental = db.session.get(ToolRental, rental_id)
        vib_item = next(i for i in rental.items if i.tool_id == self.vibrator.id)

        # Keep the vibrator line, drop the jack line, add it back as a fresh
        # row would behave — here we simply drop it.
        self._post_edit(rental_id, {
            'renter_type': 'internal',
            'project_id': str(self.site.id),
            'billing_type': 'fixed_fee',
            'tool_id[]': [str(self.vibrator.id)],
            'qty[]': ['5'],
            'rate[]': ['500'],
            'rental_item_id[]': [str(vib_item.id)],
        })
        rental = db.session.get(ToolRental, rental_id)
        self.assertEqual(len(rental.items), 1)
        self.assertEqual(rental.items[0].tool_id, self.vibrator.id)
        self.assertEqual(float(rental.total_rented_qty), 5.0)
        self.assertEqual(float(rental.total_amount), 2500.0)
        self.assertEqual(self._serial_counts(self.jack.id), (10, 0))
        self.assertTrue(tools_reconciliation(tool_ledger())['balanced'])

        # Add a brand-new line for a tool this rental never had.
        self._post_edit(rental_id, {
            'renter_type': 'internal',
            'project_id': str(self.site.id),
            'billing_type': 'fixed_fee',
            'tool_id[]': [str(self.vibrator.id), str(self.jack.id)],
            'qty[]': ['5', '3'],
            'rate[]': ['500', '1000'],
            'rental_item_id[]': [str(rental.items[0].id), ''],
        })
        rental = db.session.get(ToolRental, rental_id)
        self.assertEqual(len(rental.items), 2)
        jack_item = next(i for i in rental.items if i.tool_id == self.jack.id)
        self.assertEqual(float(jack_item.qty_rented), 3.0)
        self.assertEqual(float(jack_item.rate), 1000.0)
        self.assertEqual(float(rental.total_amount), 5 * 500 + 3 * 1000)
        self.assertEqual(self._serial_counts(self.jack.id), (7, 3))
        self.assertTrue(tools_reconciliation(tool_ledger())['balanced'])

    def test_edit_cannot_exceed_store_stock(self):
        rental_id = self._create_rental_id({
            'renter_type': 'internal',
            'project_id': str(self.site.id),
            'billing_type': 'fixed_fee',
            'tool_id[]': [str(self.jack.id)],
            'qty[]': ['4'],
            'rate[]': ['1200'],
        })
        item_id = db.session.get(ToolRental, rental_id).items[0].id
        # 10 owned, 4 out on this rental -> 14 is more than exists anywhere.
        response = self._post_edit(rental_id, {
            'renter_type': 'internal',
            'project_id': str(self.site.id),
            'billing_type': 'fixed_fee',
            'tool_id[]': [str(self.jack.id)],
            'qty[]': ['14'],
            'rate[]': ['1200'],
            'rental_item_id[]': [str(item_id)],
        })
        html = response.get_data(as_text=True)
        self.assertIn('Not enough stock for Jack Hammer', html)
        rental = db.session.get(ToolRental, rental_id)
        self.assertEqual(float(rental.items[0].qty_rented), 4.0)
        self.assertTrue(tools_reconciliation(tool_ledger())['balanced'])

    # ----------------------------------------------- locks once history exists
    def test_tool_lines_lock_after_a_return_is_recorded(self):
        rental_id = self._create_rental_id({
            'renter_type': 'internal',
            'project_id': str(self.site.id),
            'billing_type': 'fixed_fee',
            'tool_id[]': [str(self.vibrator.id)],
            'qty[]': ['6'],
            'rate[]': ['500'],
        })
        rental = db.session.get(ToolRental, rental_id)
        item_id = rental.items[0].id
        # Return two pieces through the normal return form.
        response = self.client.post(
            f'/hdc/tool-rental/{rental_id}/return', data={
                '_csrf_token': self._token(),
                'return_date': _pkt_today().isoformat(),
                'return_condition': 'partial',
                'rental_item_id[]': [str(item_id)],
                'qty_returned[]': ['2'],
                'payment_condition': 'no_payment',
            }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._serial_counts(self.vibrator.id), (16, 4))

        # Now the edit form still shows every field, but changing a tool
        # line only warns — the details still save.
        edit_html = self.client.get(
            f'/hdc/tool-rental/{rental_id}/edit').get_data(as_text=True)
        for field in CORE_FORM_FIELDS:
            self.assertIn(f'name="{field}"', edit_html, field)
        self.assertIn('tool lines are locked', edit_html)

        response = self._post_edit(rental_id, {
            'renter_type': 'internal',
            'project_id': str(self.site.id),
            'billing_type': 'fixed_fee',
            'notes': 'details still editable',
            'tool_id[]': [str(self.vibrator.id)],
            'qty[]': ['1'],
            'rate[]': ['500'],
            'rental_item_id[]': [str(item_id)],
        })
        html = response.get_data(as_text=True)
        self.assertIn('tool lines cannot change', html)
        rental = db.session.get(ToolRental, rental_id)
        self.assertEqual(rental.notes, 'details still editable')
        self.assertEqual(float(rental.items[0].qty_rented), 6.0)
        self.assertEqual(float(rental.total_pending_tools), 4.0)
        self.assertEqual(self._serial_counts(self.vibrator.id), (16, 4))
        self.assertTrue(tools_reconciliation(tool_ledger())['balanced'])

    def test_billing_type_locks_once_money_has_moved(self):
        rental_id = self._create_rental_id({
            'renter_type': 'external',
            'customer_name': 'Paying Customer',
            'billing_type': 'fixed_fee',
            'tool_id[]': [str(self.jack.id)],
            'qty[]': ['2'],
            'rate[]': ['1200'],
        })
        rental = db.session.get(ToolRental, rental_id)
        item_id = rental.items[0].id
        cash = Account.query.filter_by(type='company').first()
        if cash is None:
            cash = Account(name='Company Cash', type='company',
                           opening_balance=0.0, status='active')
            db.session.add(cash)
            db.session.commit()
        response = self.client.post(
            f'/hdc/tool-rental/{rental_id}/payment', data={
                '_csrf_token': self._token(),
                'amount': '1000',
                'payment_date': _pkt_today().isoformat(),
                'payment_mode': 'cash',
                'received_to_account_id': str(cash.id),
            }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)

        response = self._post_edit(rental_id, {
            'renter_type': 'external',
            'customer_name': 'Paying Customer',
            'billing_type': 'per_day',
            'tool_id[]': [str(self.jack.id)],
            'qty[]': ['2'],
            'rate[]': ['1200'],
            'rental_item_id[]': [str(item_id)],
        })
        html = response.get_data(as_text=True)
        self.assertIn('rent type cannot change', html)
        rental = db.session.get(ToolRental, rental_id)
        self.assertEqual(rental.billing_type, 'fixed_fee')
        self.assertEqual(float(rental.total_paid), 1000.0)

        # A non-billing change still goes through on the same rental.
        self._post_edit(rental_id, {
            'renter_type': 'external',
            'customer_name': 'Paying Customer',
            'customer_phone': '0300-1212121',
            'billing_type': 'fixed_fee',
            'tool_id[]': [str(self.jack.id)],
            'qty[]': ['2'],
            'rate[]': ['1200'],
            'rental_item_id[]': [str(item_id)],
        })
        rental = db.session.get(ToolRental, rental_id)
        self.assertEqual(rental.customer_phone, '0300-1212121')
        self.assertEqual(float(rental.total_paid), 1000.0)

    def test_closed_rental_cannot_be_edited(self):
        rental_id = self._create_rental_id({
            'renter_type': 'internal',
            'project_id': str(self.site.id),
            'billing_type': 'fixed_fee',
            'tool_id[]': [str(self.vibrator.id)],
            'qty[]': ['2'],
            'rate[]': ['500'],
        })
        rental = db.session.get(ToolRental, rental_id)
        item_id = rental.items[0].id
        # Full return closes the rental.
        self.client.post(f'/hdc/tool-rental/{rental_id}/return', data={
            '_csrf_token': self._token(),
            'return_date': _pkt_today().isoformat(),
            'return_condition': 'full',
            'payment_condition': 'no_payment',
        }, follow_redirects=True)
        rental = db.session.get(ToolRental, rental_id)
        self.assertIn(rental.status, ('returned', 'closed'))

        open_edit = self.client.get(f'/hdc/tool-rental/{rental_id}/edit',
                                    follow_redirects=True)
        self.assertIn('can no longer be edited', open_edit.get_data(as_text=True))
        response = self._post_edit(rental_id, {
            'renter_type': 'internal',
            'project_id': str(self.site.id),
            'billing_type': 'fixed_fee',
            'notes': 'should not save',
            'tool_id[]': [str(self.vibrator.id)],
            'qty[]': ['2'],
            'rate[]': ['500'],
            'rental_item_id[]': [str(item_id)],
        })
        html = response.get_data(as_text=True)
        self.assertIn('can no longer be edited', html)
        rental = db.session.get(ToolRental, rental_id)
        self.assertNotEqual(rental.notes, 'should not save')


if __name__ == '__main__':
    unittest.main(verbosity=2)
