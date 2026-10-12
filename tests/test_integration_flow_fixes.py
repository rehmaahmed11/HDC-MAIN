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


    # F9 ---------------------------------------------------------------------
    def _login_as(self, username, role):
        from werkzeug.security import generate_password_hash
        from hdc.models.auth import HDCUser
        with self.app.app_context():
            if not HDCUser.query.filter_by(username=username).first():
                db.session.add(HDCUser(username=username, role=role,
                                       password_hash=generate_password_hash('Staff@1234')))
                db.session.commit()
        # Fresh client: a GET to /hdc/logout does not end the session, so
        # reusing self.client would keep the admin session (a false pass).
        self.client = self.app.test_client()
        self.client.get('/hdc/login')
        resp = self.client.post('/hdc/login', data={
            'username': username, 'password': 'Staff@1234', '_csrf_token': self._token()})
        self.assertEqual(resp.status_code, 302)

    def test_non_finance_role_cannot_post_money(self):
        pid = self._add_project('PF9', 'Site F9', 'Owner Bee')
        with self.app.app_context():
            acc_id = _accounts_default_company_cash().id
            before_txn = AccountTransaction.query.count()
            from hdc.models.accounts import OwnerPayment
            before_op = OwnerPayment.query.count()
        self._login_as('f9-staff', 'staff')
        resp = self.client.post(f'/hdc/projects/{pid}/owner_payment', data={
            'amount': '999', 'date': date.today().isoformat(),
            'received_to_account_id': str(acc_id), '_csrf_token': self._token()})
        self.assertIn(resp.status_code, (302, 403))
        resp = self.client.post('/hdc/accounts/new-transaction', data={
            'amount': '500', 'category': 'income', '_csrf_token': self._token()})
        self.assertIn(resp.status_code, (302, 403))
        with self.app.app_context():
            from hdc.models.accounts import OwnerPayment
            self.assertEqual(OwnerPayment.query.count(), before_op)
            self.assertEqual(AccountTransaction.query.count(), before_txn)

    def test_money_write_matrix_refuses_non_finance_role(self):
        """Every money POST route refuses a staff user and writes nothing (F9).

        Ids are nonexistent on purpose: the permission check runs before the
        view, so a refusal must not depend on the record existing.
        """
        from hdc.models.accounts import OwnerPayment
        from hdc.models.tool_rental import ToolRental, ToolRentalPayment
        routes = [
            ('POST', '/hdc/accounts/new-transaction'),
            ('POST', '/hdc/accounts/money-center/api/quick-post'),
            ('POST', '/hdc/accounts/transactions/999999/edit'),
            ('POST', '/hdc/accounts/new'),
            ('POST', '/hdc/accounts/999999/edit'),
            ('POST', '/hdc/personal-management/expenses'),
            ('POST', '/hdc/projects/999999/owner_payment'),
            ('POST', '/hdc/projects/999999/owner_payment/999999/delete'),
            ('POST', '/hdc/tool-rental/create'),
            ('POST', '/hdc/tool-rental/999999/payment'),
            ('POST', '/hdc/tool-rental/999999/discount'),
            ('POST', '/hdc/tool-rental/999999/return'),
            ('POST', '/hdc/tool-rental/999999/transfer'),
            ('POST', '/hdc/tool-rental/payment/999999/void'),
            ('POST', '/hdc/tool-rental/999999/void'),
            ('POST', '/hdc/tool-rental/transaction/damage-loss'),
        ]

        def counts():
            with self.app.app_context():
                return (AccountTransaction.query.count(), OwnerPayment.query.count(),
                        ToolRentalPayment.query.count(), ToolRental.query.count())

        self._login_as('f9-matrix', 'staff')
        before = counts()
        for method, path in routes:
            with self.subTest(path=path):
                resp = self.client.open(path, method=method, data={
                    'amount': '100', 'date': date.today().isoformat(),
                    'category': 'income', '_csrf_token': self._token()})
                self.assertIn(resp.status_code, (302, 403),
                              f'{path} answered {resp.status_code} to a staff user')
        self.assertEqual(counts(), before)

    # Backfill script (F1/F3 legacy rows) ------------------------------------
    def test_backfill_refiles_legacy_owner_rows_only(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            'backfill_owner_party_names',
            os.path.join(os.path.dirname(__file__), '..', 'scripts', 'backfill_owner_party_names.py'))
        backfill = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(backfill)

        pid = self._add_project('PB1', 'Villa Legacy', 'Owner Bee')
        with self.app.app_context():
            acc_id = _accounts_default_company_cash().id
        self.client.get('/hdc/parties')
        self.client.post(f'/hdc/projects/{pid}/owner_payment', data={
            'amount': '10000', 'date': date.today().isoformat(),
            'received_to_account_id': str(acc_id), '_csrf_token': self._token()})
        with self.app.app_context():
            txn = AccountTransaction.query.filter_by(type='project_income').one()
            txn.party_name = 'Villa Legacy'          # simulate a pre-fix row
            db.session.commit()
            txn_id = txn.id
            changes = list(backfill._planned_changes())
            self.assertEqual([(t.id, new) for t, new in changes], [(txn_id, 'Owner Bee')])
            for t, new in changes:
                t.party_name = new
            db.session.commit()
            self.assertEqual(list(backfill._planned_changes()), [])
            self.assertEqual(db.session.get(AccountTransaction, txn_id).party_name, 'Owner Bee')

    # Filter dates (F10) ------------------------------------------------------
    def test_bad_filter_date_warns_instead_of_silently_dropping(self):
        resp = self.client.get('/hdc/tool-rental/reports?date_from=31-12-2026', follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('Ignored date from', resp.get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
