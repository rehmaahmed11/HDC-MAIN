"""Money Center dropdown regression coverage (audit Step 6)."""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

os.environ.setdefault('HDC_ENV', 'test')
os.environ.setdefault('HDC_SECRET_KEY', 'unit-test-secret')
os.environ.setdefault('HDC_BOOTSTRAP_ADMIN_PASSWORD', 'Admin@1234')

from hdc.app import create_app
from hdc.extensions import db
from hdc.models.accounts import Account, AccountTransaction
from hdc.models.materials import Supplier
from hdc.models.office import OfficeStaff
from hdc.models.subcontract import Subcontractor
from hdc.models.workforce import Worker


class MoneyCenterOptionsTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='hdc-money-center-test-')
        self.app = create_app({
            'HDC_DB_PATH': os.path.join(self.tmp.name, 'test.db'),
            'HDC_INSTANCE_DIR': self.tmp.name,
            'TESTING': True,
        })
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.client = self.app.test_client()
        self.client.get('/hdc/login')
        with self.client.session_transaction() as session:
            token = session['_csrf_token']
        response = self.client.post('/hdc/login', data={
            'username': 'admin',
            'password': os.environ['HDC_BOOTSTRAP_ADMIN_PASSWORD'],
            '_csrf_token': token,
        })
        self.assertEqual(response.status_code, 302)
        self.rows = {
            'workers': Worker(worker_code='MC-W1', name='Options Worker'),
            'suppliers': Supplier(name='Options Supplier'),
            'subcontractors': Subcontractor(subcontractor_code='MC-S1', name='Options Contractor'),
            'office_staff': OfficeStaff(staff_code='MC-O1', name='Options Staff'),
        }
        db.session.add_all(self.rows.values())
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        self.tmp.cleanup()

    def _feed(self, family):
        response = self.client.get('/hdc/api/' + family)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.is_json)
        payload = response.get_json()
        self.assertTrue(payload['ok'], payload)
        self.assertIsInstance(payload['items'], list)
        return payload

    def _page(self):
        response = self.client.get('/hdc/accounts/money-center')
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def _page_code(self):
        """The page's Money Center logic, wherever it now lives.

        Audit 7.3 / Step 12 moved it out of the template into a cacheable
        static file, so the behaviour tests read the script the page loads
        instead of assuming it is inline.
        """
        page = self._page()
        code = page
        for src in re.findall(r'<script[^>]+src="([^"]+)"', page):
            if 'money_center' not in src:
                continue
            path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                'static', 'hdc', src.split('/hdc_static/', 1)[-1])
            with open(path, encoding='utf-8') as fh:
                code += '\n' + fh.read()
        return code

    def test_all_four_feeds_return_seeded_options(self):
        for family, row in self.rows.items():
            with self.subTest(family=family):
                items = self._feed(family)['items']
                item = next(item for item in items if item['id'] == row.id)
                self.assertIn(row.name, item['label'])

    def test_inactive_workers_staff_and_void_suppliers_are_excluded(self):
        self.rows['workers'].active_status = False
        self.rows['office_staff'].active_status = False
        self.rows['suppliers'].is_void = True
        db.session.commit()
        for family in ('workers', 'office_staff', 'suppliers'):
            with self.subTest(family=family):
                self.assertEqual(self._feed(family)['items'], [])

    def test_money_center_uses_correct_dropdown_urls(self):
        page = self._page()
        code = self._page_code()
        for family in self.rows:
            with self.subTest(family=family):
                self.assertNotIn('/hdc/api/' + family + '/options', page)
                self.assertNotIn('/hdc/api/' + family + '/options', code)
                self.assertIn("fetch('/hdc/api/" + family + "')", code)

    @unittest.skipUnless(shutil.which('node'), 'Node.js required for dropdown JS execution')
    def test_initialization_loads_all_four_selectors_and_refreshes_early_selection(self):
        page = self._page_code()
        # Execute the actual rendered loader/selector functions with real API
        # responses and a minimal DOM; no browser/network dependencies required.
        names = ('loadWorkerOptions', 'loadSupplierOptions', 'loadSubcontractorOptions',
                 'loadOfficeStaffOptions', 'loadAllOptions', 'loadRelatedOptions')
        functions = []
        for name in names:
            match = re.search(r'(?:async )?function ' + name + r'\([^)]*\) \{.*?^\}',
                              page, re.S | re.M)
            self.assertIsNotNone(match, name)
            functions.append(match.group())
        self.assertRegex(page, r"document.addEventListener\('DOMContentLoaded',.*?\n\s*loadAllOptions\(\)")
        feeds = {'/hdc/api/' + family: self._feed(family) for family in self.rows}
        script = r'''
const assert = require('node:assert/strict');
let workerOptions = [], supplierOptions = [], subcontractorOptions = [], officeStaffOptions = [];
const calls = [];
const select = {
  options: [], value: '',
  set innerHTML(value) { this.options = []; this.value = ''; },
  appendChild(option) { this.options.push(option); }
};
const relatedType = {value: 'office_staff'};
const document = {
  getElementById(id) {
    if (id === 'mc_related_id') return select;
    if (id === 'mc_related_type') return relatedType;
    throw new Error('Unexpected DOM lookup: ' + id);
  },
  createElement(tag) { assert.equal(tag, 'option'); return {}; }
};
async function fetch(url) {
  calls.push(url);
  assert.ok(feeds[url], 'Unexpected URL: ' + url);
  return {ok: true, json: async () => feeds[url]};
}
'''
        script += '\nconst feeds = ' + json.dumps(feeds) + ';\n' + '\n'.join(functions)
        script += r'''
(async () => {
  loadRelatedOptions('office_staff'); // User selects a type before fetch finishes.
  assert.equal(select.options.length, 0);
  await loadAllOptions();
  assert.deepEqual(calls.sort(), Object.keys(feeds).sort());
  assert.equal(select.options.length, feeds['/hdc/api/office_staff'].items.length);
  for (const [type, family] of Object.entries({worker: 'workers', supplier: 'suppliers',
       subcontractor: 'subcontractors', office_staff: 'office_staff'})) {
    loadRelatedOptions(type);
    assert.deepEqual(select.options.map(o => ({id: o.value, label: o.textContent})),
      feeds['/hdc/api/' + family].items.map(o => ({id: o.id, label: o.label})));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
        result = subprocess.run(['node', '-e', script], text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()


class MoneyCenterRegressionTestCase(unittest.TestCase):
    """The regression tests the audit asked for in Step 10.

    Each one reproduces a shipped bug so it can never come back:
      10.2 every MONEY_FLOWS route builds (Step 1 — the 500 page)
      10.3 a supplier payment reduces the payable (Step 2)
      10.4 voiding from All Entries voids the CF document (Step 3)
      10.5 that void records reason/user/time (Step 4)
      10.6 quick-post cannot silently skip a supplier payable (Step 15.3)
      10.7/10.8 the Money Center renders and its feeds answer
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='hdc-money-regress-')
        self.app = create_app({
            'HDC_DB_PATH': os.path.join(self.tmp.name, 'test.db'),
            'HDC_INSTANCE_DIR': self.tmp.name,
            'TESTING': True,
        })
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.client = self.app.test_client()
        self.client.get('/hdc/login')
        with self.client.session_transaction() as session:
            self.token = session['_csrf_token']
        self.client.post('/hdc/login', data={
            'username': 'admin',
            'password': os.environ['HDC_BOOTSTRAP_ADMIN_PASSWORD'],
            '_csrf_token': self.token,
        })
        cash = Account.query.filter(Account.name == 'Company Cash').first()
        self.assertIsNotNone(cash, 'bootstrap must seed Company Cash')
        cash.opening_balance = 1000000.0
        cash.opening_balance_minor = 100000000
        counterparty = Account(name='Regression Party', type='credit_debit')
        self.supplier = Supplier(name='Regression Supplier')
        db.session.add_all([counterparty, self.supplier])
        db.session.commit()
        self.cash = cash
        self.counterparty = counterparty
        self.cash_id = int(cash.id)
        self.counterparty_id = int(counterparty.id)

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        self.tmp.cleanup()

    # ---- 10.2 -----------------------------------------------------------
    def test_every_money_flow_route_builds(self):
        """A typo in a flow's route used to 500 the whole page (audit 4.1)."""
        from flask import url_for
        from hdc.services.money_hub import get_all_money_flows
        flows = get_all_money_flows()
        self.assertTrue(flows, 'the money flow inventory must not be empty')
        with self.app.test_request_context('/'):
            for flow in flows:
                route = (flow.get('route') or '')
                if not route.startswith('hdc_'):
                    continue
                with self.subTest(flow=flow.get('id'), route=route):
                    url_for(route)      # raises BuildError on regression
        # and the page itself must render for a real user
        self.assertEqual(self.client.get('/hdc/accounts/money-center').status_code, 200)

    # ---- 10.3 -----------------------------------------------------------
    def test_supplier_payment_posts_and_reduces_the_payable(self):
        """Step 2: 'Record payment' used to crash (Order_date) and lose money."""
        from hdc.models.materials import SupplierLedger
        from hdc.services.accounts import _account_pending_snapshot

        db.session.add(SupplierLedger(
            supplier_id=self.supplier.id, entry_type='debit', amount=120000.0,
            reference_type='purchase', note='regression purchase'))
        db.session.commit()
        self.assertAlmostEqual(
            float(_account_pending_snapshot('purchase', 'supplier', self.supplier.id)['pending']),
            120000.0, places=2)

        response = self.client.post(
            f'/hdc/purchase-v2/suppliers/{self.supplier.id}/payment',
            data={'_csrf_token': self.token, 'amount': '30000', 'entry_kind': 'payment',
                  'note': 'regression payment'})
        self.assertEqual(response.status_code, 302)

        credits = SupplierLedger.query.filter_by(
            supplier_id=self.supplier.id, entry_type='credit').all()
        self.assertEqual(len(credits), 1, 'the payment must be written to the supplier ledger')
        self.assertAlmostEqual(float(credits[0].amount), 30000.0, places=2)

        # ...and the payable is now smaller, so the supplier cannot be paid twice.
        self.assertAlmostEqual(
            float(_account_pending_snapshot('purchase', 'supplier', self.supplier.id)['pending']),
            90000.0, places=2)

    # ---- 10.4 + 10.5 ----------------------------------------------------
    def test_void_from_all_entries_voids_the_cash_flow_entry(self):
        """Step 3: the register stayed active while the ledger row was voided."""
        from hdc.models.cashflow import CashFlowEntry
        from hdc.services.cashflow_register import (
            category_options, save_manual_cash_flow_entry)

        categories = category_options('out')
        self.assertTrue(categories, 'the register needs at least one category')
        entry, _ = save_manual_cash_flow_entry(
            direction='out', amount=5000.0,
            account_id=self.cash.id, destination_account_id=self.counterparty.id,
            category_id=int(categories[0].id), description='regression out')
        db.session.commit()
        txn_id = int(entry.account_tx_id)
        self.assertFalse(entry.is_void)

        # The Void button lives on the All Entries page but posts to
        # /hdc/accounts, which is where the void handler is registered.
        response = self.client.post('/hdc/accounts', data={
            '_csrf_token': self.token, 'action': 'void_transaction',
            'transaction_id': txn_id, 'void_reason': 'regression void'})
        self.assertEqual(response.status_code, 302)

        db.session.expire_all()
        entry = db.session.get(CashFlowEntry, entry.id)
        txn = db.session.get(AccountTransaction, txn_id)
        self.assertTrue(txn.is_void, 'the ledger row must be voided')
        self.assertTrue(entry.is_void,
                        'the Cash Flow document must follow the ledger row (audit 5.2)')
        # 10.5 — the audit trail is persisted, not just asked for.
        self.assertEqual(txn.void_reason, 'regression void')
        self.assertTrue((txn.voided_by or '').strip())
        self.assertIsNotNone(txn.voided_at)
        self.assertEqual(entry.void_reason, 'regression void')

    # ---- 10.6 -----------------------------------------------------------
    def test_quick_post_party_payment_with_supplier_is_refused(self):
        """Decision 15.3: never record one-sided money that skips the payable."""
        response = self.client.post(
            '/hdc/accounts/money-center/api/quick-post',
            json={
                'date': '2026-02-03', 'type': 'party_payment', 'amount': 7000,
                'from_account_id': self.cash.id, 'to_account_id': self.counterparty.id,
                'related_entity_type': 'supplier', 'related_entity_id': self.supplier.id,
            },
            headers={'X-CSRFToken': self.token})
        self.assertEqual(response.status_code, 400, response.get_data(as_text=True))
        payload = response.get_json()
        self.assertFalse(payload['ok'])
        self.assertIn('payable', payload['message'].lower())
        self.assertEqual(AccountTransaction.query.count(), 0,
                         'a refused quick-post must not write anything')

    # ---- 10.7 -----------------------------------------------------------
    def test_staff_cannot_post_money_from_the_money_center(self):
        from hdc.models.auth import HDCUser
        staff = HDCUser(username='regress-staff', role='staff', password_hash='x')
        db.session.add(staff)
        db.session.commit()

        # A *fresh* client, and a fresh app context for the request:
        # Flask-Login caches the resolved user on ``g``, which lives on the
        # app context this test case keeps pushed, so reusing either would
        # silently keep authenticating as the admin who logged in above.
        token = 'money-center-staff-csrf'
        staff_id = int(staff.id)
        self.ctx.pop()
        try:
            staff_client = self.app.test_client()
            with staff_client.session_transaction() as session:
                session.clear()
                session['_csrf_token'] = token
                session['_user_id'] = str(staff_id)
                session['_fresh'] = True
            response = staff_client.post(
                '/hdc/accounts/money-center/api/quick-post',
                json={'date': '2026-02-04', 'type': 'party_payment', 'amount': 100,
                      'from_account_id': self.cash_id,
                      'to_account_id': self.counterparty_id},
                headers={'X-CSRFToken': token})
        finally:
            self.ctx.push()
        self.assertEqual(response.status_code, 403, response.get_data(as_text=True))
        self.assertEqual(AccountTransaction.query.count(), 0,
                         'a denied staff post must not write anything')

    # ---- 10.8 -----------------------------------------------------------
    def test_money_center_feeds_and_assets_are_served(self):
        """The page's dropdown feeds and its extracted JS/CSS must all answer 200."""
        for url in ('/hdc/api/workers', '/hdc/api/suppliers',
                    '/hdc/api/subcontractors', '/hdc/api/office_staff',
                    '/hdc_static/js/pages/money_center.js',
                    '/hdc_static/css/money_center.css'):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)
