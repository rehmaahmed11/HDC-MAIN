"""Step 7: operational money writes require admin/accountant, not just login.

Uses isolated real databases, valid CSRF tokens, and database snapshots so a
302/403 cannot disguise a write. Route-map coverage includes every unsafe
method and alias in the operational finance modules, not just sampled POSTs.
"""

import os
import sqlite3
import tempfile
import unittest
from urllib.parse import urlsplit

os.environ.setdefault('HDC_ENV', 'test')
os.environ.setdefault('HDC_SECRET_KEY', 'unit-test-secret')
os.environ.setdefault('HDC_BOOTSTRAP_ADMIN_PASSWORD', 'Admin@1234')

from hdc.app import create_app
from hdc.extensions import db
from hdc.models.accounts import Account, AccountTransaction, Expense, ExpenseCategory, OwnerPayment, PersonalExpense
from hdc.models.auth import HDCUser
from hdc.models.materials import Supplier, SupplierLedger
from hdc.models.office import OfficeExpenseCategory, OfficeStaff, OfficeStaffLedger
from hdc.models.projects import Project, Stage
from hdc.models.subcontract import Subcontractor, SubcontractPayment
from hdc.models.tool_rental import Tool
from hdc.models.workforce import LabourLedger, PayrollItem, PayrollRun, Worker
from hdc.utils.dates import _pkt_today


MONEY_MODULES = {
    'workers', 'payroll', 'expenses', 'subcontractors', 'office',
    'purchase_v2', 'tool_rental', 'api_purchase', 'timekeeping', 'materials',
}
OTHER_MONEY_ENDPOINTS = {
    'hdc_add_owner_payment', 'hdc_delete_owner_payment', 'hdc_restore_owner_payment',
    'hdc_stage_status', 'hdc_stage_sub_progress',
    'hdc_personal_expenses', 'hdc_personal_expense_void',
    'hdc_personal_expense_categories', 'hdc_personal_category_suspend',
    'hdc_personal_category_activate',
}
DENIED_MESSAGE = 'Admin/Accountant access required.'


class MoneyPermissionsTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='hdc-money-permissions-')
        self.db_path = os.path.join(self.tmp.name, 'test.db')
        self.app = create_app({
            'HDC_DB_PATH': self.db_path, 'HDC_INSTANCE_DIR': self.tmp.name,
            'TESTING': True,
        })
        self.client = self.app.test_client()
        self.token = 'money-permissions-csrf'
        with self.app.app_context():
            admin = HDCUser.query.filter_by(username='admin').one()
            self.users = {'admin': admin.id}
            for label, role in (('staff', 'staff'), ('accountant', 'accountant'),
                                ('manager', 'manager'), ('unknown', 'other'),
                                ('blank', ''), ('missing', None),
                                ('normalized', ' Accountant ')):
                user = HDCUser(username='permissions-' + label, password_hash='unused', role=role)
                db.session.add(user)
                db.session.flush()
                # Explicitly exercise NULL despite the model's insert default.
                user.role = role
                self.users[label] = user.id
            cash = Account.query.filter_by(name='Company Cash').one()
            cash.opening_balance = 1000000
            project = Project(name='Permission Site', project_code='PERM-P', client='Owner')
            worker = Worker(name='Permission Worker', worker_code='PERM-W', base_daily_wage=1000)
            staff = OfficeStaff(name='Permission Staff', staff_code='PERM-O', monthly_salary=30000)
            supplier = Supplier(name='Permission Supplier')
            category = ExpenseCategory(name='Permission Expense')
            run = PayrollRun(date_from=_pkt_today(), date_to=_pkt_today())
            personal = PersonalExpense(beneficiary_name='Owner', amount=500)
            db.session.add_all([project, worker, staff, supplier, category, run, personal])
            db.session.flush()
            stage = Stage(project_id=project.id, name='Permission Stage')
            sub = Subcontractor(name='Permission Contractor', subcontractor_code='PERM-S',
                                project_id=project.id, contract_type='lump_sum', lump_sum_amount=10000)
            db.session.add_all([stage, sub, PayrollItem(run_id=run.id, worker_id=worker.id, net_pay=10000)])
            db.session.commit()
            self.ids = {key: row.id for key, row in {
                'cash': cash, 'project': project, 'worker': worker, 'staff': staff,
                'supplier': supplier, 'category': category, 'run': run,
                'stage': stage, 'sub': sub, 'personal': personal,
            }.items()}
        with self.app.test_request_context('/'):
            from flask import url_for
            self.dashboard = url_for('hdc_dashboard')

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()
        self.tmp.cleanup()

    def _as(self, role):
        with self.client.session_transaction() as session:
            session.clear()
            session['_csrf_token'] = self.token
            if role is not None:
                session['_user_id'] = str(self.users[role])
                session['_fresh'] = True

    def _snapshot(self):
        with sqlite3.connect(self.db_path) as conn:
            return '\n'.join(conn.iterdump())

    def _count(self, model):
        with self.app.app_context():
            return model.query.count()

    def _request(self, url, method='POST', data=None, api=False):
        args = {'json' if api else 'data': data or {}}
        return self.client.open(url, method=method,
                                headers={'X-CSRFToken': self.token}, **args)

    def _assert_denied(self, response, api=False):
        if api:
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.get_json(), {'ok': False, 'message': DENIED_MESSAGE})
        else:
            self.assertEqual(response.status_code, 302)
            self.assertEqual(urlsplit(response.location).path, self.dashboard)
        with self.client.session_transaction() as session:
            self.assertIn(('danger', DENIED_MESSAGE), session.pop('_flashes', []))

    def _write_routes(self):
        adapter = self.app.url_map.bind('localhost')
        for rule in self.app.url_map.iter_rules():
            module = self.app.view_functions[rule.endpoint].__module__.rsplit('.', 1)[-1]
            if module not in MONEY_MODULES and rule.endpoint not in OTHER_MONEY_ENDPOINTS:
                continue
            for method in sorted(rule.methods - {'GET', 'HEAD', 'OPTIONS'}):
                url = adapter.build(rule.endpoint, {arg: 999999 for arg in rule.arguments}, method=method)
                # Build each alias explicitly (attendance/timekeeping share an endpoint).
                if not rule.arguments:
                    url = rule.rule
                yield rule.endpoint, method, url, rule.endpoint.startswith(('api_', 'hdc_api_'))

    def test_all_operational_write_routes_deny_non_money_roles_without_mutation(self):
        cases = list(self._write_routes())
        self.assertGreaterEqual(len(cases), 100)
        self.assertEqual({method for _, method, _, _ in cases}, {'POST', 'PUT', 'PATCH', 'DELETE'})
        for role in ('staff', 'manager', 'unknown', 'blank', 'missing'):
            self._as(role)
            before = self._snapshot()
            for endpoint, method, url, api in cases:
                with self.subTest(role=role, endpoint=endpoint, method=method, url=url):
                    response = self._request(url, method, {'name': 'Unauthorized change', 'amount': 100}, api)
                    self._assert_denied(response, api)
            self.assertEqual(self._snapshot(), before, role + ' changed database contents')

    def _valid_money_cases(self):
        i = self.ids
        scope = {'project_id': i['project'], 'stage_id': i['stage'],
                 'date': _pkt_today().isoformat(), 'amount': 100}
        return [
            (f"/hdc/projects/{i['project']}/owner_payment",
             dict(scope, received_to_account_id=i['cash']), OwnerPayment, True),
            (f"/hdc/workers/{i['worker']}/advance", scope, LabourLedger, True),
            ('/hdc/expenses', dict(scope, category_id=i['category']), Expense, True),
            (f"/hdc/purchase-v2/suppliers/{i['supplier']}/payment", scope, SupplierLedger, True),
            (f"/hdc/subcontractor/{i['sub']}/pay",
             {'project_id': i['project'], 'amount': 100}, SubcontractPayment, True),
            (f"/hdc/office-management/staff/{i['staff']}/payment", scope, OfficeStaffLedger, True),
            ('/hdc/payroll/generate', dict(scope, action='pay_worker',
                                         run_id=i['run'], worker_id=i['worker']), LabourLedger, True),
            ('/hdc/tool-rental/inventory', {'name': 'Permission Tool', 'total_quantity': 5,
                                           'purchase_cost': 100, 'rental_rate_per_day': 10}, Tool, False),
        ]

    def test_valid_money_posts_are_blocked_for_staff_not_just_bad_ids(self):
        self._as('staff')
        for url, payload, _, _ in self._valid_money_cases():
            with self.subTest(url=url):
                before = self._snapshot()
                self._assert_denied(self._request(url, data=payload))
                self.assertEqual(self._snapshot(), before)
        # A real existing row cannot be edited/deleted/voided through an alias.
        for url, method, data, api in (
            (f"/hdc/workers/{self.ids['worker']}/toggle", 'POST', {}, False),
            (f"/hdc/personal-management/expenses/{self.ids['personal']}/void", 'POST', {'void_reason': 'test'}, False),
            (f"/api/v2/purchase/suppliers/{self.ids['supplier']}", 'PUT', {'name': 'changed'}, True),
            (f"/api/v2/purchase/suppliers/{self.ids['supplier']}", 'DELETE', {}, True),
        ):
            with self.subTest(url=url, method=method):
                before = self._snapshot()
                self._assert_denied(self._request(url, method, data, api), api)
                self.assertEqual(self._snapshot(), before)

    def _exercise_allowed_posts(self, role):
        self._as(role)
        for url, payload, model, posts_cash in self._valid_money_cases():
            with self.subTest(role=role, url=url):
                before_rows, before_txns = self._count(model), self._count(AccountTransaction)
                response = self._request(url, data=payload)
                self.assertEqual(response.status_code, 302)
                self.assertNotEqual(urlsplit(response.location).path, self.dashboard)
                self.assertEqual(self._count(model), before_rows + 1)
                if posts_cash:
                    self.assertEqual(self._count(AccountTransaction), before_txns + 1)

    def test_admin_can_still_post_operational_money(self):
        self._exercise_allowed_posts('admin')

    def test_accountant_can_post_operational_money(self):
        self._exercise_allowed_posts('accountant')

    def test_normalized_accountant_role_is_allowed(self):
        self._exercise_allowed_posts('normalized')

    def test_accountant_can_write_json_api_with_post_put_delete_and_patch(self):
        self._as('accountant')
        response = self._request('/api/v2/purchase/suppliers', data={'name': 'API permission supplier'}, api=True)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['ok'])
        sid = response.get_json()['id']
        for method, data in (('PUT', {'name': 'Renamed API supplier'}), ('DELETE', {})):
            response = self._request(f'/api/v2/purchase/suppliers/{sid}', method, data, True)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.get_json()['ok'])
        with self.app.app_context():
            row = db.session.get(Supplier, sid)
            self.assertEqual(row.name, 'Renamed API supplier')
            self.assertTrue(row.is_void)
        response = self._request('/hdc/api/office_expense_categories', data={'name': 'API permission category'}, api=True)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['ok'])
        with self.app.app_context():
            cid = OfficeExpenseCategory.query.filter_by(name=response.get_json()['name']).one().id
        response = self._request(f'/hdc/api/office_expense_categories/{cid}', 'PATCH', {'action': 'suspend'}, True)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['ok'])
        with self.app.app_context():
            self.assertFalse(db.session.get(OfficeExpenseCategory, cid).active_status)

    def test_staff_and_accountant_keep_existing_operational_reads(self):
        for role in ('staff', 'accountant'):
            self._as(role)
            for url in ('/hdc/workers', '/hdc/payroll', '/hdc/expenses', '/hdc/reports',
                        '/hdc/purchase-v2', '/hdc/tool-rental/inventory',
                        '/hdc/office-management/staff/ledger', '/hdc/subcontractors',
                        '/hdc/timekeeping', '/hdc/personal-management/expenses',
                        '/api/v2/purchase/suppliers', '/hdc/api/office_expense_categories'):
                with self.subTest(role=role, url=url):
                    self.assertEqual(self.client.get(url).status_code, 200)
            for method in ('HEAD', 'OPTIONS'):
                self.assertEqual(self.client.open('/hdc/workers', method=method).status_code, 200)

    def test_accounts_and_admin_settings_remain_admin_only(self):
        for role in ('staff', 'accountant'):
            self._as(role)
            before = self._snapshot()
            for url in ('/hdc/accounts', '/hdc/accounts/entries', '/hdc/accounts/manage',
                        '/hdc/accounts/money-center', '/hdc/settings', '/hdc/users'):
                for method in ('GET', 'POST'):
                    with self.subTest(role=role, url=url, method=method):
                        response = self._request(url, method)
                        self.assertEqual(response.status_code, 302)
                        self.assertEqual(urlsplit(response.location).path, self.dashboard)
            self.assertEqual(self._snapshot(), before)

    def test_anonymous_write_still_requires_login(self):
        self._as(None)
        before = self._snapshot()
        for url in ('/hdc/workers', '/api/v2/purchase/suppliers'):
            response = self._request(url, data={'name': 'anonymous'})
            self.assertEqual(response.status_code, 302)
            self.assertEqual(urlsplit(response.location).path, '/hdc/login')
        self.assertEqual(self._snapshot(), before)

    def test_csrf_remains_required_for_allowed_and_denied_roles(self):
        for role in ('staff', 'accountant', 'admin'):
            self._as(role)
            before = self._snapshot()
            for url in ('/hdc/workers', '/api/v2/purchase/suppliers'):
                response = self.client.post(url, json={'name': 'No CSRF'})
                self.assertEqual(response.status_code, 400)
            self.assertEqual(self._snapshot(), before)


if __name__ == '__main__':
    unittest.main()
