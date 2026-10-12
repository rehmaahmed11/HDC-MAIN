#!/usr/bin/env python3
"""Regression tests for INTEGRATION_FLOW_AUDIT.md (F1-F7).

  F1  owner receipts are filed under the owner, so the owner's statement shows them
  F2  a Deactivate on a client/worker survives a visit to /hdc/parties
  F3  own-site tool rental money is filed under the owner, not the project name
  F4  an unparseable tool date is refused, not silently replaced by today
  F5  a failed party link is reported, and the project is still saved

Run with:
    HDC_BOOTSTRAP_ADMIN_PASSWORD='Admin@1234' \\
        python -m unittest tests.test_integration_flow_fixes -v
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from datetime import date
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

os.environ.setdefault('HDC_ENV', 'test')
os.environ.setdefault('HDC_SECRET_KEY', 'unit-test-secret')
os.environ.setdefault('HDC_BOOTSTRAP_ADMIN_PASSWORD', 'Admin@1234')

from hdc.app import create_app                                      # noqa: E402
from hdc.extensions import db                                       # noqa: E402
from hdc.models.accounts import AccountTransaction                  # noqa: E402
from hdc.models.cashflow import CashFlowParty                       # noqa: E402
from hdc.models.projects import Project                             # noqa: E402
from hdc.routes.tool_rental import _strict_form_date                # noqa: E402
from hdc.services.accounts import _accounts_default_company_cash    # noqa: E402
from hdc.services.parties import party_ledger                       # noqa: E402
from hdc.services.tool_rental import rental_party_name              # noqa: E402

ADMIN_PASSWORD = os.environ['HDC_BOOTSTRAP_ADMIN_PASSWORD']


class IntegrationFlowFixesTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='hdc-flow-fixes-')
        self.app = create_app({'HDC_DB_PATH': os.path.join(self.tmp, 'test.db'),
                               'HDC_INSTANCE_DIR': self.tmp,
                               'TESTING': True})
        self.client = self.app.test_client()
        self.client.get('/hdc/login')
        with self.client.session_transaction() as s:
            token = s['_csrf_token']
        resp = self.client.post('/hdc/login', data={
            'username': 'admin', 'password': ADMIN_PASSWORD, '_csrf_token': token})
        self.assertEqual(resp.status_code, 302)

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _token(self):
        with self.client.session_transaction() as s:
            return s['_csrf_token']

    def _add_project(self, code, name, client):
        with self.app.app_context():
            p = Project(project_code=code, name=name, client=client,
                        owner_lump_sum=100_000, contract_type='lump_sum')
            db.session.add(p)
            db.session.commit()
            return p.id

    # F2 ---------------------------------------------------------------------
    def test_deactivated_client_stays_hidden_after_directory_load(self):
        self._add_project('PF2', 'Site F2', 'Owner Bee')
        self.client.get('/hdc/parties')           # auto-sync creates the client
        with self.app.app_context():
            pid = CashFlowParty.query.filter_by(name='Owner Bee').one().id
        self.client.post('/hdc/parties', data={
            'action': 'toggle_party', 'party_id': pid, '_csrf_token': self._token()})
        with self.app.app_context():
            self.assertFalse(db.session.get(CashFlowParty, pid).is_active)
        self.client.get('/hdc/parties')           # the page that used to revive it
        with self.app.app_context():
            self.assertFalse(db.session.get(CashFlowParty, pid).is_active)

    # F1 ---------------------------------------------------------------------
    def test_owner_payment_appears_on_owner_statement(self):
        pid = self._add_project('PF1', 'Villa Site', 'Owner Bee')
        with self.app.app_context():
            acc_id = _accounts_default_company_cash().id
        self.client.get('/hdc/parties')
        resp = self.client.post(f'/hdc/projects/{pid}/owner_payment', data={
            'amount': '25000', 'date': date.today().isoformat(),
            'received_to_account_id': str(acc_id), 'remarks': 'part payment',
            '_csrf_token': self._token()})
        self.assertEqual(resp.status_code, 302)
        with self.app.app_context():
            txn = AccountTransaction.query.filter_by(type='project_income').one()
            self.assertEqual(txn.party_name, 'Owner Bee')
            party = CashFlowParty.query.filter_by(name='Owner Bee').one()
            with self.app.test_request_context('/'):   # ledger rows build url_for links
                statement = party_ledger(party)
            self.assertEqual(len(statement['rows']), 1)
            self.assertEqual(statement['total_in'], 25000.0)

    # F3 ---------------------------------------------------------------------
    def test_internal_rental_money_is_filed_under_the_owner(self):
        project = SimpleNamespace(name='Site A', client='Owner Bee')
        internal = SimpleNamespace(renter_type='internal', project=project, customer_name=None)
        external = SimpleNamespace(renter_type='external', project=None,
                                   customer_name='Walk-in Customer')
        self.assertEqual(rental_party_name(internal), 'Owner Bee')
        self.assertEqual(rental_party_name(external), 'Walk-in Customer')
        no_owner = SimpleNamespace(renter_type='internal',
                                   project=SimpleNamespace(name='Site B', client=''),
                                   customer_name=None)
        self.assertEqual(rental_party_name(no_owner), 'Site B')

    # F4 ---------------------------------------------------------------------
    def test_unparseable_tool_date_is_refused_not_defaulted(self):
        parsed, err = _strict_form_date('31/12/2026', 'Payment date')
        self.assertIsNone(parsed)
        self.assertIn('not a valid date', err)
        parsed, err = _strict_form_date('2026-10-01', 'Payment date')
        self.assertEqual(parsed, date(2026, 10, 1))
        self.assertIsNone(err)
        parsed, err = _strict_form_date('', 'Payment date')
        self.assertIsNone(err)
        self.assertIsNotNone(parsed)

    # F5 ---------------------------------------------------------------------
    def test_failed_party_link_is_reported_and_project_still_saved(self):
        with mock.patch('hdc.services.cashflow_register.ensure_party',
                        side_effect=RuntimeError('boom')):
            resp = self.client.post('/hdc/projects/add', data={
                'name': 'Tower F5', 'client': 'Owner Fail', 'total_sqft': '0',
                'owner_rate': '0', 'lump_sum': '5000', '_csrf_token': self._token()},
                follow_redirects=True)
        page = resp.get_data(as_text=True)
        self.assertIn('could not be linked to Parties', page)
        self.assertNotIn('linked to Parties.', page.replace('could not be linked to Parties', ''))
        with self.app.app_context():
            self.assertIsNotNone(Project.query.filter_by(name='Tower F5').first())


if __name__ == '__main__':
    unittest.main()
