"""Purchase V2 list filters: PO number, date range, material type.

Covers the Purchase Orders page (/hdc/purchase-v2/purchases) and the
Material Usage page (/hdc/purchase-v2/usage): both must support filtering
by PO number, material, and date range, list newest entries first, and
show the recorded date+time for each entry. Only disposable databases
are used.
"""
import os
import re
import tempfile
import unittest
from datetime import timedelta
from unittest.mock import patch

from hdc.app import create_app
from hdc.extensions import db
from hdc.models.auth import HDCUser
from hdc.models.materials import MaterialV2, Supplier, PurchaseV2, UsageLogV2
from hdc.models.projects import Project, Stage
from hdc.utils.dates import _pkt_now_naive


class PurchaseUsageFilterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='hdc-po-filter-')
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, 'qa.db')
        with patch.dict(os.environ, {'HDC_ENV': 'test', 'HDC_SECRET_KEY': 'qa-only',
                                    'HDC_BOOTSTRAP_ADMIN_PASSWORD': 'QA-only-1234!'}):
            self.app = create_app({'TESTING': True, 'HDC_DB_PATH': self.path,
                                   'HDC_INSTANCE_DIR': self.tmp.name})
        self.addCleanup(self.dispose)
        self.client = self.app.test_client()
        with self.app.app_context():
            supplier = Supplier(name='QA Supplier')
            cement = MaterialV2(name='Cement', unit='BAG')
            steel = MaterialV2(name='Steel', unit='KG')
            db.session.add_all([supplier, cement, steel])
            db.session.flush()
            project = Project(project_code='QA-1', name='QA Project')
            db.session.add(project)
            db.session.flush()
            stage = Stage(name='QA Stage', project_id=project.id)
            db.session.add(stage)
            db.session.commit()
            self.cement_id = cement.id
            self.steel_id = steel.id
            base = _pkt_now_naive()
            # Three purchases: old cement, mid steel, new cement — distinct dates.
            po_old = PurchaseV2(supplier_id=supplier.id, material_id=cement.id,
                unit_price=100, quantity=10, total_amount=1000, payment_status='unpaid',
                date=(base - timedelta(days=10)).date(), is_void=False,
                created_at=base - timedelta(days=10), updated_at=base - timedelta(days=10))
            po_mid = PurchaseV2(supplier_id=supplier.id, material_id=steel.id,
                unit_price=200, quantity=20, total_amount=4000, payment_status='paid',
                date=(base - timedelta(days=5)).date(), is_void=False,
                created_at=base - timedelta(days=5), updated_at=base - timedelta(days=5))
            po_new = PurchaseV2(supplier_id=supplier.id, material_id=cement.id,
                unit_price=150, quantity=30, total_amount=4500, payment_status='unpaid',
                date=base.date(), is_void=False,
                created_at=base, updated_at=base)
            db.session.add_all([po_old, po_mid, po_new])
            db.session.flush()
            self.po_old_id, self.po_mid_id, self.po_new_id = po_old.id, po_mid.id, po_new.id
            # Two usage rows tied to POs (old + new).
            usage_old = UsageLogV2(purchase_id=po_old.id, material_id=cement.id,
                project_id=project.id, stage_id=stage.id, quantity=2, cost=200,
                date=(base - timedelta(days=9)).date(), is_void=False,
                created_at=base - timedelta(days=9))
            usage_new = UsageLogV2(purchase_id=po_new.id, material_id=cement.id,
                project_id=project.id, stage_id=stage.id, quantity=5, cost=750,
                date=base.date(), is_void=False, created_at=base)
            db.session.add_all([usage_old, usage_new])
            db.session.commit()
            self.usage_old_id = usage_old.id
            self.usage_new_id = usage_new.id
            uid = HDCUser.query.filter_by(username='admin').one().id
        with self.client.session_transaction() as session:
            session['_user_id'] = str(uid)
            session['_fresh'] = True
            session['_csrf_token'] = 'qa-csrf'

    def dispose(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()

    def get(self, path, query=''):
        resp = self.client.get(path + (('?' + query) if query else ''))
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True)[:2000])
        return resp.get_data(as_text=True)

    @staticmethod
    def row_ids(html):
        # Entry rows render as "#<id>" in a muted first cell.
        return [int(m) for m in re.findall(r'<td class="text-muted small">#(\d+)</td>', html)]

    # ---- Purchases page ----
    def test_purchases_newest_first_and_recorded_column(self):
        html = self.get('/hdc/purchase-v2/purchases')
        self.assertEqual(self.row_ids(html), [self.po_new_id, self.po_mid_id, self.po_old_id])
        self.assertIn('Recorded', html)
        self.assertRegex(html, r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}')

    def test_purchases_filter_by_po_number(self):
        html = self.get('/hdc/purchase-v2/purchases', f'po_number={self.po_mid_id}')
        self.assertEqual(self.row_ids(html), [self.po_mid_id])

    def test_purchases_filter_by_material(self):
        html = self.get('/hdc/purchase-v2/purchases', f'material_id={self.cement_id}')
        self.assertEqual(self.row_ids(html), [self.po_new_id, self.po_old_id])

    def test_purchases_filter_by_date_range(self):
        base = _pkt_now_naive()
        frm = (base - timedelta(days=6)).date().isoformat()
        to = base.date().isoformat()
        html = self.get('/hdc/purchase-v2/purchases', f'date_from={frm}&date_to={to}')
        self.assertEqual(self.row_ids(html), [self.po_new_id, self.po_mid_id])
        html = self.get('/hdc/purchase-v2/purchases', f'date_from={frm}')
        self.assertEqual(self.row_ids(html), [self.po_new_id, self.po_mid_id])
        html = self.get('/hdc/purchase-v2/purchases', f'date_to={frm}')
        self.assertEqual(self.row_ids(html), [self.po_old_id])

    def test_purchases_filters_combine_and_invalid_po_warns(self):
        html = self.get('/hdc/purchase-v2/purchases',
                        f'material_id={self.cement_id}&date_from=2000-01-01&date_to=2100-01-01')
        self.assertEqual(self.row_ids(html), [self.po_new_id, self.po_old_id])
        # Existing supplier filter still works alongside the new ones.
        html = self.get('/hdc/purchase-v2/purchases', f'po_number={self.po_old_id}')
        self.assertEqual(self.row_ids(html), [self.po_old_id])
        resp = self.client.get('/hdc/purchase-v2/purchases?po_number=abc')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('PO number must be numeric', resp.get_data(as_text=True))

    # ---- Usage page ----
    def test_usage_newest_first_and_recorded_column(self):
        html = self.get('/hdc/purchase-v2/usage')
        self.assertEqual(self.row_ids(html), [self.usage_new_id, self.usage_old_id])
        self.assertIn('Recorded', html)
        self.assertRegex(html, r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}')

    def test_usage_filter_by_po_number(self):
        html = self.get('/hdc/purchase-v2/usage', f'po_number={self.po_new_id}')
        self.assertEqual(self.row_ids(html), [self.usage_new_id])
        self.assertIn('PO #%d' % self.po_new_id, html)

    def test_usage_filter_by_material_and_date_range(self):
        base = _pkt_now_naive()
        html = self.get('/hdc/purchase-v2/usage', f'material_id={self.steel_id}')
        self.assertEqual(self.row_ids(html), [])
        html = self.get('/hdc/purchase-v2/usage',
                        f'date_from={(base - timedelta(days=1)).date().isoformat()}')
        self.assertEqual(self.row_ids(html), [self.usage_new_id])
        html = self.get('/hdc/purchase-v2/usage',
                        f'date_to={(base - timedelta(days=1)).date().isoformat()}')
        self.assertEqual(self.row_ids(html), [self.usage_old_id])

    def test_filter_forms_render(self):
        for path in ('/hdc/purchase-v2/purchases', '/hdc/purchase-v2/usage'):
            with self.subTest(path=path):
                html = self.get(path)
                for field in ('po_number', 'material_id', 'date_from', 'date_to'):
                    self.assertIn(f'name="{field}"', html)


if __name__ == '__main__':
    unittest.main()
