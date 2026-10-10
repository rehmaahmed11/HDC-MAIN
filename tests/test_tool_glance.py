"""Tools Glance view: one row per tool piece, where it is, and every site and
customer it was sent to.  Runs against a real temporary database."""

import os
import re
import tempfile
import unittest
from datetime import datetime

os.environ.setdefault('HDC_ENV', 'test')
os.environ.setdefault('HDC_SECRET_KEY', 'unit-test-secret')
os.environ.setdefault('HDC_BOOTSTRAP_ADMIN_PASSWORD', 'Admin@1234')

from hdc.app import create_app
from hdc.extensions import db
from hdc.models.auth import HDCUser
from hdc.models.projects import Project
from hdc.models.tool_rental import (
    Tool, ToolRental, ToolRentalItem, ToolSerial, ToolSerialMovement,
)
from hdc.services.tool_rental import STORE_LABEL, tool_glance_rows


class ToolGlanceTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='hdc-tool-glance-')
        self.app = create_app({
            'HDC_DB_PATH': os.path.join(self.tmp.name, 'test.db'),
            'HDC_INSTANCE_DIR': self.tmp.name,
            'TESTING': True,
        })
        self.client = self.app.test_client()
        self.token = 'glance-csrf'
        with self.app.app_context():
            admin = HDCUser.query.filter_by(username='admin').one()
            self.admin_id = admin.id
            site = Project(project_code='P-GL-1', name='Site Alpha')
            db.session.add(site)
            tool = Tool(tool_code='TOOL-GL1', name='Shovel', total_quantity=3)
            db.session.add(tool)
            db.session.flush()
            rental = ToolRental(rental_code='RENT-GL1', renter_type='external',
                                customer_name='Ali Customer', rental_date=datetime(2026, 9, 1).date())
            db.session.add(rental)
            db.session.flush()
            item = ToolRentalItem(rental_id=rental.id, tool_id=tool.id, qty_rented=1,
                                  qty_pending=1, rate=0)
            db.session.add(item)
            db.session.flush()
            s1 = ToolSerial(serial_number='Shovel No 1', tool_id=tool.id, is_in_store=False,
                            status='transferred', current_location_label='Site Alpha')
            s2 = ToolSerial(serial_number='Shovel No 2', tool_id=tool.id, is_in_store=False,
                            status='rented', current_rental_id=rental.id,
                            current_rental_item_id=item.id,
                            current_location_label='Ali Customer')
            s3 = ToolSerial(serial_number='Shovel No 3', tool_id=tool.id, is_in_store=True,
                            status='in_store', current_location_label=STORE_LABEL)
            db.session.add_all([s1, s2, s3])
            db.session.flush()
            db.session.add(ToolSerialMovement(
                serial_id=s1.id, movement_type='transfer_out', from_location_label=STORE_LABEL,
                to_location_label='Site Alpha', timestamp=datetime(2026, 9, 5, 10, 0)))
            db.session.add(ToolSerialMovement(
                serial_id=s2.id, rental_id=rental.id, movement_type='rental_out',
                from_location_label=STORE_LABEL, to_location_label='Ali Customer',
                timestamp=datetime(2026, 9, 1, 9, 0)))
            db.session.commit()
            self.s1_id, self.s2_id, self.s3_id = s1.id, s2.id, s3.id

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()
        self.tmp.cleanup()

    def _login(self):
        with self.client.session_transaction() as session:
            session.clear()
            session['_csrf_token'] = self.token
            session['_user_id'] = str(self.admin_id)
            session['_fresh'] = True

    def test_service_rows_and_destinations(self):
        with self.app.app_context():
            data = tool_glance_rows()
            by_serial = {r['serial_number']: r for r in data['rows']}
            s1 = by_serial['Shovel No 1']
            self.assertEqual(s1['tool_and_serial'], 'Shovel — Shovel No 1')
            self.assertEqual(s1['current_kind'], 'site')
            self.assertEqual(s1['site_name'], 'Site Alpha')
            self.assertEqual(s1['customer_name'], '—')
            self.assertEqual(s1['date_sent'].isoformat(), '2026-09-05')
            self.assertEqual(s1['sent_to_text'], 'Site Alpha')

            s2 = by_serial['Shovel No 2']
            self.assertEqual(s2['current_kind'], 'customer')
            self.assertEqual(s2['customer_name'], 'Ali Customer')
            self.assertEqual(s2['site_name'], '—')
            self.assertEqual(s2['rental_code'], 'RENT-GL1')

            s3 = by_serial['Shovel No 3']
            self.assertEqual(s3['current_kind'], 'store')
            self.assertEqual(s3['in_store_on_site'], 'In Store')
            self.assertIsNone(s3['date_sent'])
            self.assertEqual(s3['sent_count'], 0)

            kinds = {(d['kind'], d['label']): d for d in data['destinations']}
            self.assertEqual(kinds[('site', 'Site Alpha')]['pieces_now'], 1)
            self.assertEqual(kinds[('customer', 'Ali Customer')]['pieces_now'], 1)
            self.assertEqual(kinds[('customer', 'Ali Customer')]['pieces_sent'], 1)
            self.assertEqual(data['kpis']['in_store'], 1)
            self.assertEqual(data['kpis']['sites'], 1)
            self.assertEqual(data['kpis']['customers'], 1)

    def test_page_renders_with_all_columns_and_nav_tab(self):
        self._login()
        resp = self.client.get('/hdc/tool-rental/glance')
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        for needle in ('Shovel — Shovel No 1', 'Shovel — Shovel No 2', 'Shovel — Shovel No 3',
                       'Site Alpha', 'Ali Customer', 'RENT-GL1', 'In Store',
                       'All Sites &amp; Customers Where Tools Were Sent'):
            self.assertIn(needle, html)
        self.assertIn('href="/hdc/tool-rental/glance"', html)  # Glance tab in tools nav

        filtered = self.client.get('/hdc/tool-rental/glance?only=in_store').get_data(as_text=True)
        rows = re.findall(r'<td><strong>(Shovel — [^<]+)</strong>', filtered)
        self.assertEqual(rows, ['Shovel — Shovel No 3'])

    def test_requires_login(self):
        resp = self.client.get('/hdc/tool-rental/glance')
        self.assertEqual(resp.status_code, 302)


if __name__ == '__main__':
    unittest.main()
