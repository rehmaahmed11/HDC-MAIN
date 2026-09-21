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
        for family in self.rows:
            with self.subTest(family=family):
                self.assertNotIn('/hdc/api/' + family + '/options', page)
                self.assertIn("fetch('/hdc/api/" + family + "')", page)

    @unittest.skipUnless(shutil.which('node'), 'Node.js required for dropdown JS execution')
    def test_initialization_loads_all_four_selectors_and_refreshes_early_selection(self):
        page = self._page()
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
