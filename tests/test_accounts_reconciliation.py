"""Regression coverage for audit Step 5: office expense mirror reconciliation."""

import os
import tempfile
import unittest

os.environ.setdefault('HDC_ENV', 'test')
os.environ.setdefault('HDC_SECRET_KEY', 'unit-test-secret')
os.environ.setdefault('HDC_BOOTSTRAP_ADMIN_PASSWORD', 'Admin@1234')

from hdc.app import create_app
from hdc.extensions import db
from hdc.models.accounts import Account, AccountTransaction
from hdc.models.office import OfficeExpense, OfficeStaff, OfficeStaffLedger
from hdc.services.accounts import (
    _accounts_reconciliation_findings,
    _accounts_upsert_office_staff_ledger_txn,
)
from hdc.services.ledger import _sync_office_staff_expense_from_ledger


class AccountsReconciliationTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='hdc-reconciliation-test-')
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
        cash = Account.query.filter_by(name='Company Cash').one()
        cash.opening_balance = 500000.0
        self.staff = OfficeStaff(staff_code='RECON-001', name='Reconciliation Staff')
        db.session.add(self.staff)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        self.tmp.cleanup()

    def _staff_posting(self, entry_type='payment', amount=40000.0):
        ledger = OfficeStaffLedger(
            staff_id=self.staff.id, entry_type=entry_type, amount=amount,
        )
        db.session.add(ledger)
        db.session.flush()
        mirror = _sync_office_staff_expense_from_ledger(self.staff, ledger)
        ok, message, txns = _accounts_upsert_office_staff_ledger_txn(self.staff, ledger)
        self.assertTrue(ok, message)
        self.assertEqual(len(txns), 1)
        db.session.commit()
        self.assertEqual(mirror.office_staff_ledger_id, ledger.id)
        return ledger, mirror, txns[0]

    def _findings(self):
        with self.app.test_request_context('/'):
            return _accounts_reconciliation_findings()

    def test_staff_payment_advance_and_tip_mirrors_are_not_orphans(self):
        for entry_type, amount in (('payment', 40000), ('advance', 3000), ('tip', 500)):
            with self.subTest(entry_type=entry_type):
                self._staff_posting(entry_type, amount)
                findings = self._findings()
                self.assertEqual(findings['orphan_sources'], [])
                self.assertTrue(all(count == 0 for count in findings['totals'].values()), findings)
        self.assertEqual(OfficeExpense.query.count(), 3)
        self.assertEqual(AccountTransaction.query.count(), 3)
        response = self.client.get('/hdc/accounts/reconciliation')
        self.assertEqual(response.status_code, 200)
        self.assertIn('0 issues found', response.get_data(as_text=True))

    def test_unlinked_office_expense_still_reports_missing_posting(self):
        self._staff_posting()
        # Even a salary-labelled expense needs a posting unless it has a ledger link.
        expense = OfficeExpense(
            office_staff_id=self.staff.id, category='Office Salary Payment', amount=7000,
        )
        db.session.add(expense)
        db.session.commit()
        findings = self._findings()
        self.assertEqual(findings['totals']['orphan_sources'], 1)
        orphan = findings['orphan_sources'][0]
        self.assertEqual((orphan['source_type'], orphan['source_id']), ('office_expense', expense.id))

    def test_missing_staff_posting_is_reported_once_not_again_for_mirror(self):
        ledger, mirror, txn = self._staff_posting()
        db.session.delete(txn)
        db.session.commit()
        findings = self._findings()
        self.assertEqual(findings['totals']['orphan_sources'], 1)
        orphan = findings['orphan_sources'][0]
        self.assertEqual((orphan['source_type'], orphan['source_id']),
                         ('office_staff_ledger_payment', ledger.id))

    def test_staff_posting_void_mismatch_is_still_detected(self):
        ledger, mirror, txn = self._staff_posting()
        txn.is_void = True
        db.session.commit()
        findings = self._findings()
        self.assertEqual(findings['orphan_sources'], [])
        self.assertEqual(findings['totals']['void_mismatch'], 1)
        self.assertEqual(findings['void_mismatch'][0]['id'], txn.id)


if __name__ == '__main__':
    unittest.main()
