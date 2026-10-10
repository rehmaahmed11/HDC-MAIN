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
from hdc.models.projects import Project                           # noqa: E402
from hdc.models.tool_rental import (                              # noqa: E402
    Tool,
    ToolCategory,
    ToolRental,
    ToolSerial,
    ToolSerialMovement,
)
from hdc.services.tool_rental import (                            # noqa: E402
    get_serials_in_store_summary,
    get_serials_out_of_store_summary,
    record_tool_purchase,
    record_tool_scrap,
)

ADMIN_PASSWORD = os.environ['HDC_BOOTSTRAP_ADMIN_PASSWORD']


class ToolSerialMarkingsTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='hdc-tool-serial-markings-')
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

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()
        shutil.rmtree(self.tmp, ignore_errors=True)

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

    def test_add_tool_generates_human_readable_serial_markings(self):
        """Adding a tool creates human-readable markings like 'Shovel No 5', 'Shovel No 6'."""
        resp = self.client.post('/hdc/tool-rental/inventory', data={
            '_csrf_token': self._token(),
            'action': 'add_tool',
            'name': 'Shovel',
            'tool_code': 'TOOL-SHV',
            'category_id': '__new',
            'new_category_name': 'Earthwork Tools',
            'unit': 'pcs',
            'condition': 'good',
            'total_quantity': '3',
            'purchase_cost': '1200',
            'rental_rate_per_day': '100',
            'serial_start_no': '5',
        }, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)

        shovel = Tool.query.filter_by(tool_code='TOOL-SHV').first()
        self.assertIsNotNone(shovel)
        serials = shovel.in_store_serials
        self.assertEqual([s.serial_number for s in serials], [
            'Shovel No 5',
            'Shovel No 6',
            'Shovel No 7',
        ])
        for s in serials:
            self.assertTrue(s.is_in_store)
            self.assertFalse(s.is_scrapped)
            self.assertEqual(s.current_location_label, 'Warehouse / Store')

    def test_purchase_and_scrap_update_serial_markings_lifecycle(self):
        """Purchasing adds new numbered serials; scrapping retires specific in-store serials."""
        cat = ToolCategory(name='Scaffolding', active_status=True)
        db.session.add(cat)
        db.session.flush()
        plank = Tool(
            tool_code='TOOL-PLK',
            name='Wooden Plank',
            category_id=cat.id,
            unit='pcs',
            total_quantity=0,
            purchase_cost=800,
            rental_rate_per_day=50,
        )
        db.session.add(plank)
        db.session.commit()

        ok, msg, _ = record_tool_purchase(
            tool_id=plank.id,
            qty=3,
            unit_cost=800,
            is_opening_stock=True,
        )
        self.assertTrue(ok, msg)
        self.assertEqual(
            [s.serial_number for s in plank.in_store_serials],
            ['Wooden Plank No 1', 'Wooden Plank No 2', 'Wooden Plank No 3'],
        )

        # Purchase 2 more -> continues numbering at No 4, No 5
        ok, msg, _ = record_tool_purchase(
            tool_id=plank.id,
            qty=2,
            unit_cost=850,
        )
        self.assertTrue(ok, msg)
        self.assertEqual(
            [s.serial_number for s in plank.in_store_serials],
            [
                'Wooden Plank No 1',
                'Wooden Plank No 2',
                'Wooden Plank No 3',
                'Wooden Plank No 4',
                'Wooden Plank No 5',
            ],
        )

        # Scrap Wooden Plank No 2 specifically
        plank_no_2 = ToolSerial.query.filter_by(
            tool_id=plank.id, serial_number='Wooden Plank No 2'
        ).first()
        ok, msg, scrap = record_tool_scrap(
            tool_id=plank.id,
            qty=1,
            reason='damaged',
            notes=' cracked in middle ',
            serial_ids=[plank_no_2.id],
        )
        self.assertTrue(ok, msg)
        self.assertEqual(scrap.notes, 'cracked in middle')
        db.session.refresh(plank_no_2)
        self.assertTrue(plank_no_2.is_scrapped)
        self.assertFalse(plank_no_2.is_in_store)
        self.assertEqual(
            [s.serial_number for s in plank.in_store_serials],
            [
                'Wooden Plank No 1',
                'Wooden Plank No 3',
                'Wooden Plank No 4',
                'Wooden Plank No 5',
            ],
        )

    def test_new_rental_only_shows_in_store_tools_and_rents_selected_serials(self):
        """New Rental filters to in-store tools/serials and rents exact chosen markings."""
        cat = ToolCategory(name='Hand Tools', active_status=True)
        db.session.add(cat)
        db.session.flush()

        shovel = Tool(
            tool_code='TOOL-001',
            name='Shovel',
            category_id=cat.id,
            unit='pcs',
            total_quantity=0,
            rental_rate_per_day=100,
        )
        wheelbarrow = Tool(
            tool_code='TOOL-002',
            name='Wheel Barrow',
            category_id=cat.id,
            unit='pcs',
            total_quantity=0,
            rental_rate_per_day=250,
        )
        db.session.add_all([shovel, wheelbarrow])
        db.session.commit()

        record_tool_purchase(shovel.id, 6, unit_cost=500, is_opening_stock=True)
        record_tool_purchase(wheelbarrow.id, 1, unit_cost=4000, is_opening_stock=True)

        # Rent the only Wheel Barrow so it has 0 in store
        wb_serial = wheelbarrow.in_store_serials[0]
        resp = self.client.post('/hdc/tool-rental/create', data={
            '_csrf_token': self._token(),
            'txn_type': 'new',
            'renter_type': 'external',
            'customer_name': 'Site Alpha Contractor',
            'billing_type': 'daily',
            'rental_date': '2026-10-01',
            'tool_id[]': [str(wheelbarrow.id)],
            'qty[]': ['1'],
            'rate[]': ['250'],
            'row_serial_ids[]': [str(wb_serial.id)],
        }, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        db.session.refresh(wb_serial)
        self.assertFalse(wb_serial.is_in_store)

        # GET /hdc/tool-rental/new: Wheel Barrow has 0 in store so it must not be in IN_STORE_TOOLS
        form_resp = self.client.get('/hdc/tool-rental/new')
        self.assertEqual(form_resp.status_code, 200)
        html = form_resp.get_data(as_text=True)
        self.assertIn('Shovel No 5', html)
        self.assertIn('Shovel No 6', html)
        # In-store API also excludes the rented-out Wheel Barrow
        api_in_store = self.client.get('/hdc/api/tool-rental/serials/in-store').get_json()
        in_store_tool_names = [t['tool_name'] for t in api_in_store['tools']]
        self.assertIn('Shovel', in_store_tool_names)
        self.assertNotIn('Wheel Barrow', in_store_tool_names)

        # Now rent specifically Shovel No 5 and Shovel No 6 via row_serial_ids[]
        s5 = ToolSerial.query.filter_by(tool_id=shovel.id, serial_number='Shovel No 5').first()
        s6 = ToolSerial.query.filter_by(tool_id=shovel.id, serial_number='Shovel No 6').first()
        resp2 = self.client.post('/hdc/tool-rental/create', data={
            '_csrf_token': self._token(),
            'txn_type': 'new',
            'renter_type': 'external',
            'customer_name': 'Bahria Site B',
            'billing_type': 'daily',
            'rental_date': '2026-10-02',
            'tool_id[]': [str(shovel.id)],
            'qty[]': ['2'],
            'rate[]': ['100'],
            'row_serial_ids[]': [f'{s5.id},{s6.id}'],
        }, follow_redirects=True)
        self.assertEqual(resp2.status_code, 200)

        db.session.refresh(s5)
        db.session.refresh(s6)
        self.assertFalse(s5.is_in_store)
        self.assertFalse(s6.is_in_store)
        self.assertEqual(s5.current_location_label, 'Bahria Site B')
        self.assertEqual(s6.current_location_label, 'Bahria Site B')

        # Remaining in-store serials for Shovel are No 1, No 2, No 3, No 4
        self.assertEqual(
            [s.serial_number for s in shovel.in_store_serials],
            ['Shovel No 1', 'Shovel No 2', 'Shovel No 3', 'Shovel No 4'],
        )
        # Out-of-store serials for Shovel are No 5, No 6
        self.assertEqual(
            [s.serial_number for s in shovel.out_serials],
            ['Shovel No 5', 'Shovel No 6'],
        )

    def test_transfer_only_shows_out_of_store_serials_and_moves_selected_serials(self):
        """Transfer Tools only offers out-of-store serials and transfers the exact selected markings."""
        p1 = Project(name='Site 1 Plaza', project_code='PRJ-1', client='HDC', status='ongoing')
        p2 = Project(name='Site 2 Tower', project_code='PRJ-2', client='HDC', status='ongoing')
        db.session.add_all([p1, p2])
        shovel = Tool(
            tool_code='TOOL-SHV2',
            name='Shovel',
            unit='pcs',
            total_quantity=0,
            rental_rate_per_day=100,
        )
        db.session.add(shovel)
        db.session.commit()

        record_tool_purchase(shovel.id, 6, unit_cost=500, is_opening_stock=True)
        s5 = ToolSerial.query.filter_by(tool_id=shovel.id, serial_number='Shovel No 5').first()
        s6 = ToolSerial.query.filter_by(tool_id=shovel.id, serial_number='Shovel No 6').first()

        # Rent Shovel No 5 and Shovel No 6 to Site 1 Plaza
        self.client.post('/hdc/tool-rental/create', data={
            '_csrf_token': self._token(),
            'txn_type': 'new',
            'renter_type': 'internal',
            'project_id': str(p1.id),
            'billing_type': 'no_charge',
            'rental_date': '2026-10-01',
            'tool_id[]': [str(shovel.id)],
            'qty[]': ['2'],
            'rate[]': ['0'],
            'row_serial_ids[]': [f'{s5.id},{s6.id}'],
        }, follow_redirects=True)

        source_rental = ToolRental.query.filter_by(project_id=p1.id).first()
        self.assertIsNotNone(source_rental)
        source_item = source_rental.items[0]

        # Out-of-store API must ONLY list Shovel No 5 and Shovel No 6 (never No 1..No 4)
        out_summary = self.client.get('/hdc/api/tool-rental/serials/out-of-store').get_json()
        self.assertEqual(out_summary['total_serials'], 2)
        out_numbers = [s['serial_number'] for s in out_summary['tools'][0]['serials']]
        self.assertEqual(out_numbers, ['Shovel No 5', 'Shovel No 6'])

        # Transfer ONLY Shovel No 5 from Site 1 Plaza to Site 2 Tower via Transfer Rental
        source_key = f'{source_rental.id}|own_project|{p1.id}|0|'
        resp = self.client.post('/hdc/tool-rental/create', data={
            '_csrf_token': self._token(),
            'txn_type': 'transfer',
            'from_source_key[]': [source_key],
            'transfer_item_id[]': [str(source_item.id)],
            f'transfer_serial_ids_{source_item.id}[]': [str(s5.id)],
            f'transfer_qty_{source_item.id}': '1',
            'renter_type': 'internal',
            'project_id': str(p2.id),
            'billing_type': 'no_charge',
            'rental_date': '2026-10-03',
        }, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)

        db.session.refresh(s5)
        db.session.refresh(s6)
        dest_rental = ToolRental.query.filter_by(project_id=p2.id).first()
        self.assertIsNotNone(dest_rental)

        # Shovel No 5 is now at Site 2 Tower under dest_rental; Shovel No 6 is still at Site 1 Plaza!
        self.assertEqual(s5.current_rental_id, dest_rental.id)
        self.assertIn('Site 2 Tower', s5.current_location_label)
        self.assertEqual(s6.current_rental_id, source_rental.id)
        self.assertIn('Site 1 Plaza', s6.current_location_label)

        # Now return Shovel No 5 from Site 2 Tower back to store
        dest_item = dest_rental.items[0]
        ret_resp = self.client.post(f'/hdc/tool-rental/{dest_rental.id}/return', data={
            '_csrf_token': self._token(),
            'return_date': '2026-10-05',
            'return_condition': 'partial',
            'rental_item_id[]': [str(dest_item.id)],
            'qty_returned[]': ['1'],
            f'return_serial_ids_{dest_item.id}[]': [str(s5.id)],
            'condition_notes[]': ['Clean'],
            'payment_condition': 'no_payment',
        }, follow_redirects=True)
        self.assertEqual(ret_resp.status_code, 200)

        db.session.refresh(s5)
        db.session.refresh(s6)
        self.assertTrue(s5.is_in_store)
        self.assertEqual(s5.current_location_label, 'Warehouse / Store')
        self.assertIsNone(s5.current_rental_id)
        self.assertFalse(s6.is_in_store)

    def test_selected_serials_mode_and_detail_page_transfer(self):
        """Posting selected_serials creates a rental; detail-page transfer moves chosen serials."""
        p1 = Project(name='Site Alpha', project_code='PRJ-A', client='HDC', status='ongoing')
        p2 = Project(name='Site Beta', project_code='PRJ-B', client='HDC', status='ongoing')
        db.session.add_all([p1, p2])
        plank = Tool(
            tool_code='TOOL-WP',
            name='Wooden Plank',
            unit='pcs',
            total_quantity=0,
            rental_rate_per_day=80,
        )
        db.session.add(plank)
        db.session.commit()

        record_tool_purchase(
            plank.id,
            3,
            unit_cost=600,
            is_opening_stock=True,
            serial_numbers='Wooden Plank No 10, Wooden Plank No 11, Wooden Plank No 12',
        )
        p10 = ToolSerial.query.filter_by(tool_id=plank.id, serial_number='Wooden Plank No 10').first()
        p11 = ToolSerial.query.filter_by(tool_id=plank.id, serial_number='Wooden Plank No 11').first()
        p12 = ToolSerial.query.filter_by(tool_id=plank.id, serial_number='Wooden Plank No 12').first()
        self.assertIsNotNone(p10)
        self.assertIsNotNone(p11)
        self.assertIsNotNone(p12)

        # Create rental using selected_serials (Individual Serials picker mode)
        resp = self.client.post('/hdc/tool-rental/create', data={
            '_csrf_token': self._token(),
            'txn_type': 'new',
            'renter_type': 'internal',
            'project_id': str(p1.id),
            'billing_type': 'no_charge',
            'rental_date': '2026-10-01',
            'selected_serials': f'{p10.id},{p12.id}',
        }, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)

        db.session.refresh(p10)
        db.session.refresh(p11)
        db.session.refresh(p12)
        self.assertFalse(p10.is_in_store)
        self.assertTrue(p11.is_in_store)
        self.assertFalse(p12.is_in_store)

        rental = ToolRental.query.filter_by(project_id=p1.id).first()
        self.assertIsNotNone(rental)
        item = rental.items[0]
        self.assertEqual(float(item.qty_rented), 2.0)

        # Attempting to rent an out-of-store serial (p10) in a new rental is rejected
        bad_resp = self.client.post('/hdc/tool-rental/create', data={
            '_csrf_token': self._token(),
            'txn_type': 'new',
            'renter_type': 'external',
            'customer_name': 'Someone Else',
            'billing_type': 'daily',
            'rental_date': '2026-10-02',
            'selected_serials': str(p10.id),
        }, follow_redirects=True)
        self.assertEqual(bad_resp.status_code, 200)
        self.assertIn('is not available in store', bad_resp.get_data(as_text=True))

        # Detail page site-to-site transfer: move only Wooden Plank No 12 to Site Beta
        tr_resp = self.client.post(f'/hdc/tool-rental/{rental.id}/transfer', data={
            '_csrf_token': self._token(),
            'to_type': 'site',
            'to_project_id': str(p2.id),
            'transfer_date': '2026-10-03',
            'rental_item_id[]': [str(item.id)],
            'qty_transfer[]': ['1'],
            f'transfer_serial_ids_{item.id}[]': [str(p12.id)],
            'notes': 'Move No 12 to Site Beta',
        }, follow_redirects=True)
        self.assertEqual(tr_resp.status_code, 200)

        db.session.refresh(p10)
        db.session.refresh(p12)
        self.assertIn('Site Alpha', p10.current_location_label)
        self.assertIn('Site Beta', p12.current_location_label)

        # Verify serial status API shows movement history
        status_json = self.client.get(f'/hdc/api/tool-rental/serials/{p12.id}/status').get_json()
        self.assertEqual(status_json['serial_number'], 'Wooden Plank No 12')
        self.assertFalse(status_json['is_in_store'])
        self.assertTrue(len(status_json['movements']) >= 2)


if __name__ == '__main__':
    unittest.main()
