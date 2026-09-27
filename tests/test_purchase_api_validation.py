"""Purchase API boundary regressions; only disposable databases are used."""
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from hdc.app import create_app
from hdc.extensions import db
from hdc.models.auth import HDCUser
from hdc.models.materials import MaterialV2, Supplier, PurchaseV2, SupplierLedger


class PurchaseValidationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='hdc-purchase-validation-')
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, 'qa.db')
        with patch.dict(os.environ, {'HDC_ENV': 'test', 'HDC_SECRET_KEY': 'qa-only',
                                    'HDC_BOOTSTRAP_ADMIN_PASSWORD': 'QA-only-1234!'}):
            self.app = create_app({'TESTING': True, 'HDC_DB_PATH': self.path,
                                   'HDC_INSTANCE_DIR': self.tmp.name})
        self.addCleanup(self.dispose)
        self.client = self.app.test_client()
        with self.app.app_context():
            supplier, material = Supplier(name='QA supplier'), MaterialV2(name='QA material')
            db.session.add_all([supplier, material])
            db.session.commit()
            self.payload = dict(supplier_id=supplier.id, material_id=material.id,
                                unit_price=125.5, quantity=4)
            uid = HDCUser.query.filter_by(username='admin').one().id
        with self.client.session_transaction() as session:
            session['_user_id'] = str(uid)
            session['_fresh'] = True
            session['_csrf_token'] = 'qa-csrf'

    def dispose(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()

    def snapshot(self):
        # Exclude activity telemetry; assert all business rows are unchanged.
        with sqlite3.connect(self.path) as con:
            return {table: con.execute('SELECT * FROM ' + table).fetchall()
                    for table in ('hdc_supplier', 'hdc_material_v2', 'hdc_purchase_v2',
                                  'hdc_supplier_ledger', 'hdc_account_txn')}

    def post(self, payload, path='/api/v2/purchase/purchases'):
        return self.client.post(path, json=payload, headers={'X-CSRFToken': 'qa-csrf'})

    def test_nonfinite_and_overflow_rejected_without_writes(self):
        for changes in ({'quantity': 'Infinity'}, {'unit_price': 'Infinity'},
                        {'quantity': 'NaN'}, {'unit_price': 1e308, 'quantity': 1e308}):
            with self.subTest(changes=changes):
                before = self.snapshot()
                response = self.post(dict(self.payload, **changes))
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.json['ok'])
                self.assertEqual(self.snapshot(), before)

    def test_wrong_json_shapes_and_types_rejected_without_writes(self):
        for payload in ([1], 'text', 123, {'phone': ['bad'], 'name': 'QA'},
                        {'name': {'bad': 'type'}}, {'name': True}):
            with self.subTest(payload=payload):
                before = self.snapshot()
                response = self.post(payload, '/api/v2/purchase/suppliers')
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.json['ok'])
                self.assertEqual(self.snapshot(), before)

    def test_valid_purchase_posts_exact_supplier_debit(self):
        response = self.post(self.payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['total_amount'], 502)
        with self.app.app_context():
            row = db.session.get(PurchaseV2, response.json['id'])
            self.assertEqual(row.total_amount, 502)
            ledger = SupplierLedger.query.filter_by(reference_id=row.id, entry_type='debit').one()
            self.assertEqual(ledger.amount, 502)

    def test_paid_purchase_posting_failure_rolls_back(self):
        before = self.snapshot()
        with patch('hdc.routes.api_purchase._accounts_upsert_purchase_paid_txn',
                   return_value=(False, 'Injected posting failure', None)):
            response = self.post(dict(self.payload, payment_status='paid'))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.snapshot(), before)

    def test_used_delivery_cannot_be_voided_or_reduced(self):
        from hdc.models.projects import Project, Stage
        from hdc.models.materials import Delivery, UsageLogV2
        with self.app.app_context():
            project = Project(name='QA site', project_code='QA-P', client='QA client')
            db.session.add(project)
            db.session.flush()
            stage = Stage(name='QA stage', project_id=project.id)
            db.session.add(stage)
            db.session.commit()
            scope = dict(project_id=project.id, stage_id=stage.id)
        purchase = self.post(self.payload).json['id']
        delivery = self.post(dict(scope, purchase_id=purchase, quantity=4),
                             '/api/v2/purchase/deliveries').json['id']
        usage = self.post(dict(scope, purchase_id=purchase, material_id=self.payload['material_id'], quantity=3),
                          '/api/v2/purchase/usage')
        self.assertEqual(usage.status_code, 200)
        self.assertEqual(usage.json['cost'], 376.5)
        response = self.client.delete(f'/api/v2/purchase/deliveries/{delivery}',
                                      headers={'X-CSRFToken': 'qa-csrf'})
        self.assertEqual(response.status_code, 400)
        for action, data in (('void', {}), ('edit', {'quantity': 2})):
            self.client.post(f'/hdc/purchase-v2/delivered/{delivery}/{action}',
                             data=data, headers={'X-CSRFToken': 'qa-csrf'})
            with self.app.app_context():
                row = db.session.get(Delivery, delivery)
                self.assertFalse(row.is_void)
                self.assertEqual(row.quantity, 4)
        # Reducing only unused stock remains legitimate.
        self.client.post(f'/hdc/purchase-v2/delivered/{delivery}/edit',
                         data={'quantity': 3}, headers={'X-CSRFToken': 'qa-csrf'})
        with self.app.app_context():
            self.assertEqual(db.session.get(Delivery, delivery).quantity, 3)
        self.client.delete(f'/api/v2/purchase/usage/{usage.json["id"]}',
                           headers={'X-CSRFToken': 'qa-csrf'})
        response = self.client.delete(f'/api/v2/purchase/deliveries/{delivery}',
                                      headers={'X-CSRFToken': 'qa-csrf'})
        self.assertEqual(response.status_code, 200)
        with self.app.app_context():
            self.assertTrue(db.session.get(Delivery, delivery).is_void)
            self.assertTrue(db.session.get(UsageLogV2, usage.json['id']).is_void)

    def test_update_rejects_overflow_and_invalid_ids(self):
        purchase_id = self.post(self.payload).json['id']
        for changes in ({'unit_price': 1e308, 'quantity': 1e308},
                        {'supplier_id': 10**100}, {'supplier_id': True},
                        {'supplier_id': []}, {'quantity': False}):
            with self.subTest(changes=changes):
                before = self.snapshot()
                response = self.client.put(f'/api/v2/purchase/purchases/{purchase_id}',
                                           json=dict(self.payload, **changes),
                                           headers={'X-CSRFToken': 'qa-csrf'})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.snapshot(), before)

    def test_malformed_json_and_nonfinite_payment_rejected(self):
        before = self.snapshot()
        response = self.client.post('/api/v2/purchase/suppliers', data='{',
                                    content_type='application/json', headers={'X-CSRFToken': 'qa-csrf'})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json['ok'])
        response = self.post({'supplier_id': self.payload['supplier_id'], 'amount': 'Infinity'},
                             '/api/v2/purchase/payments')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.snapshot(), before)

    def test_form_numeric_strings_remain_supported(self):
        payload = dict(self.payload, unit_price='1,250.50', quantity='2')
        response = self.client.post('/api/v2/purchase/purchases', data=payload,
                                    headers={'X-CSRFToken': 'qa-csrf'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['total_amount'], 2501)
