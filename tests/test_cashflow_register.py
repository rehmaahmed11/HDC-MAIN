#!/usr/bin/env python3
"""Regression tests for the Cash Flow v2 register (Accounts section).

Covers the four things the register adds over the plain ledger:

  * exact money        — hdc/utils/money.py + the ``*_minor`` mirrors
  * account classification — hdc/services/account_classification.py
  * immutability       — amend = void + replace, void, restore, period locks
  * day close          — counted vs expected, day lock, carry-forward

Run with:
    HDC_BOOTSTRAP_ADMIN_PASSWORD='Admin@1234' \
        python -m unittest tests.test_cashflow_register -v
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

os.environ.setdefault('HDC_ENV', 'test')
os.environ.setdefault('HDC_SECRET_KEY', 'unit-test-secret')
os.environ.setdefault('HDC_BOOTSTRAP_ADMIN_PASSWORD', 'Admin@1234')

from hdc.app import create_app                                      # noqa: E402
from hdc.extensions import db                                      # noqa: E402
from hdc.models.accounts import Account, AccountTransaction         # noqa: E402
from hdc.models.cashflow import (                                   # noqa: E402
    AccountReconciliation, CashDayLock, CashFlowEntry, CashFlowEntryAudit,
    CashFlowParty,
)
from hdc.models.projects import Project                             # noqa: E402
from sqlalchemy import text                                          # noqa: E402
from hdc.utils.dates import _pkt_now_naive                          # noqa: E402
from hdc.services.cashflow_register import (                        # noqa: E402
    amend_manual_cash_flow_entry, category_options, day_lock_state,
    day_positions, day_totals, is_day_locked, lock_cash_day,
    register_rows, register_row_dicts, register_summary,
    restore_manual_cash_flow_entry, save_cf_party,
    save_counted_position, save_manual_cash_flow_entry,
    unlock_cash_day, void_manual_cash_flow_entry,
)

ADMIN_PASSWORD = os.environ['HDC_BOOTSTRAP_ADMIN_PASSWORD']


def _csrf(client, url):
    page = client.get(url).get_data(as_text=True)
    token = re.search(r'name="_csrf_token" value="([^"]+)"', page)
    return token.group(1) if token else ''


class MoneyTestCase(unittest.TestCase):
    """Exact money primitives — no app context needed."""

    def test_to_minor_rounds_half_up(self):
        from hdc.utils.money import from_minor, to_minor
        self.assertEqual(to_minor('1234.565'), 123457)
        self.assertEqual(to_minor(0.125), 13)
        self.assertEqual(to_minor('0.00'), 0)
        self.assertEqual(to_minor(None), 0)
        self.assertEqual(float(from_minor(123457)), 1234.57)

    def test_accepts_typed_money(self):
        from hdc.utils.money import to_minor
        self.assertEqual(to_minor('1,25,000.50'), 12500050)   # lakh grouping
        self.assertEqual(to_minor('Rs 4,500.25'), 450025)     # currency prefix
        self.assertEqual(to_minor('12 000'), 1200000)         # space grouping
        self.assertEqual(to_minor('(250)'), -25000)           # accounting negative
        self.assertEqual(to_minor('-0.00'), 0)

    def test_rejects_garbage(self):
        from hdc.utils.money import MoneyValueError, to_minor
        for bad in ('abc', '1.2.3', 'twelve'):
            with self.assertRaises(MoneyValueError):
                to_minor(bad)

    def test_float_error_does_not_accumulate(self):
        """Ten 0.10 postings must total exactly 1.00 in paisa."""
        from hdc.utils.money import from_minor, to_minor
        total = sum(to_minor(0.10) for _ in range(10))
        self.assertEqual(total, 100)
        self.assertEqual(str(from_minor(total)), '1.00')


class ClassificationTestCase(unittest.TestCase):
    """The controlled Category → Subcategory → Account Type registry."""

    def test_valid_and_invalid_combinations(self):
        from hdc.services.account_classification import is_valid, leaf
        self.assertTrue(is_valid('Assets', 'Cash', 'Main Cash'))
        self.assertTrue(is_valid('Assets', 'Cash', 'Main Cash', channel='cash'))
        self.assertFalse(is_valid('Assets', 'Cash', 'Main Cash', channel='bank'))
        self.assertFalse(is_valid('Assets', 'Cash', 'Not A Type'))
        self.assertFalse(is_valid('Nonsense', 'Cash', 'Main Cash'))
        self.assertIsNone(leaf('Assets', 'Cash', 'Nope'))

    def test_tree_is_json_serialisable_and_complete(self):
        import json
        from hdc.services.account_classification import (
            account_types, categories, classification_tree, subcategories,
        )
        tree = classification_tree()
        json.dumps(tree)                       # must not raise
        self.assertIn('Assets', tree)
        for cat in categories():
            for sub in subcategories(cat):
                self.assertTrue(account_types(cat, sub),
                                f'{cat}/{sub} has no account types')

    def test_legacy_mapping_covers_every_hdc_account_type(self):
        from hdc.services.account_classification import (
            is_valid, legacy_to_classification,
        )
        for legacy in ('company', 'cash', 'bank', 'person', 'vendor', 'client'):
            triple = legacy_to_classification(legacy)
            self.assertTrue(is_valid(*triple), f'{legacy} -> {triple}')

    def test_legacy_bank_account_is_classified_as_bank(self):
        from hdc.services.account_classification import legacy_to_classification
        self.assertEqual(legacy_to_classification('company', is_bank=True),
                         ('Assets', 'Bank', 'Operating Bank'))
        self.assertEqual(legacy_to_classification('company'),
                         ('Assets', 'Cash', 'Main Cash'))


class CashFlowRegisterTestCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='hdc-cfreg-test-')
        self.app = create_app({'HDC_DB_PATH': os.path.join(self.tmp, 'test.db'),
                               'HDC_INSTANCE_DIR': self.tmp})
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()
        self._login()

        self.cash = self._mk_account('Test Cash', 'cash', opening=100000.0)
        self.bank = self._mk_account('Test Bank', 'bank', opening=50000.0,
                                     bank_name='Meezan', account_number='1234')
        self.actor = 'admin'

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── helpers ──────────────────────────────────────────────────────────

    def _login(self):
        token = _csrf(self.client, '/hdc/login')
        res = self.client.post('/hdc/login', data={
            'username': 'admin', 'password': ADMIN_PASSWORD, '_csrf_token': token,
        })
        self.assertIn(res.status_code, (200, 302), 'login failed')

    def _mk_account(self, name, acc_type, opening=0.0, bank_name='', account_number=''):
        row = Account(name=name, type=acc_type, opening_balance=float(opening),
                      bank_name=bank_name or None, account_number=account_number or None,
                      status='active', is_void=False, created_at=_pkt_now_naive())
        db.session.add(row)
        db.session.commit()
        return Account.query.filter_by(name=name).first()

    def _post(self, url, data, referrer=None):
        payload = dict(data)
        payload['_csrf_token'] = _csrf(self.client, referrer or url)
        return self.client.post(url, data=payload)

    def _entry(self, direction='out', amount=1000.0, account=None, **kw):
        entry, _ = save_manual_cash_flow_entry(
            direction=direction, amount=amount,
            account_id=(account or self.cash).id, actor=self.actor, **kw)
        db.session.commit()
        return entry

    def _categories(self):
        return {c.name: c for c in category_options()}

    # ── posting + exact money ────────────────────────────────────────────

    def test_seed_categories_exist(self):
        names = set(self._categories())
        self.assertIn('Material & Purchase', names)
        self.assertIn('Owner / Client Receipt', names)

    def test_money_in_out_and_transfer_post_one_ledger_row_each(self):
        cats = self._categories()
        # A plain income category: 'Owner / Client Receipt' is the project
        # receipt head (project_effect='receipt') and now requires a project,
        # which this test is not about.  See test_project_receipt_* below.
        e_in = self._entry('in', '1,25,000.50', account=self.cash,
                           category_id=cats['Other Income'].id,
                           party_name='Mr. Akram')
        e_out = self._entry('out', '4,500.25', account=self.cash,
                            category_id=cats['Material & Purchase'].id,
                            party_name='Al-Kabir Store')
        e_tr = self._entry('transfer', 10000, account=self.cash,
                           destination_account_id=self.bank.id)

        self.assertEqual(e_in.amount_minor, 12500050)
        self.assertEqual(e_out.amount_minor, 450025)
        self.assertEqual(e_tr.amount_minor, 1000000)

        for e in (e_in, e_out, e_tr):
            self.assertIsNotNone(e.account_tx_id, 'entry must post to the ledger')
            tx = db.session.get(AccountTransaction, e.account_tx_id)
            self.assertEqual(tx.amount_minor, e.amount_minor)
            self.assertFalse(tx.is_void)

        from hdc.services.accounts import _account_balance_map
        bal = _account_balance_map()
        self.assertAlmostEqual(bal[self.cash.id],
                               100000 + 125000.50 - 4500.25 - 10000, places=2)
        self.assertAlmostEqual(bal[self.bank.id], 50000 + 10000, places=2)

    def test_ledger_rows_get_minor_units_even_without_the_register(self):
        """The model listener must cover rows posted by other modules too."""
        row = AccountTransaction(
            date=date(2026, 9, 1), amount=1234.56, type='party_payment',
            from_account_id=self.cash.id, to_account_id=None,
            executed_by_account_id=self.cash.id, category='expense',
            party_name='Direct Row', is_void=False, created_at=_pkt_now_naive())
        db.session.add(row)
        db.session.commit()
        self.assertEqual(row.amount_minor, 123456)

    def test_rejects_non_money_account(self):
        person = self._mk_account('Some Worker', 'person')
        with self.assertRaises(ValueError):
            self._entry('out', 10, account=person,
                        category_id=self._categories()['Miscellaneous'].id)

    def test_rejects_zero_and_negative_amounts(self):
        cats = self._categories()
        for bad in (0, -5):
            with self.assertRaises(ValueError):
                save_manual_cash_flow_entry(direction='out', amount=bad,
                                            account_id=self.cash.id,
                                            category_id=cats['Miscellaneous'].id,
                                            actor=self.actor)

    def test_blocks_overdraft(self):
        cats = self._categories()
        with self.assertRaises(ValueError) as ctx:
            self._entry('out', 100000000, category_id=cats['Miscellaneous'].id)
        self.assertIn('Insufficient balance', str(ctx.exception))

    def test_transfer_needs_two_different_money_accounts(self):
        with self.assertRaises(ValueError):
            save_manual_cash_flow_entry(direction='transfer', amount=100,
                                        account_id=self.cash.id,
                                        destination_account_id=self.cash.id,
                                        actor=self.actor)
        person = self._mk_account('Not Money', 'person')
        with self.assertRaises(ValueError):
            save_manual_cash_flow_entry(direction='transfer', amount=100,
                                        account_id=self.cash.id,
                                        destination_account_id=person.id,
                                        actor=self.actor)

    def test_category_direction_is_enforced(self):
        in_cat = self._categories()['Owner / Client Receipt']   # direction = in
        with self.assertRaises(ValueError):
            save_manual_cash_flow_entry(direction='out', amount=100,
                                        account_id=self.cash.id,
                                        category_id=in_cat.id, actor=self.actor)

    def test_idempotency_key_prevents_double_posting(self):
        cats = self._categories()
        first, created1 = save_manual_cash_flow_entry(
            direction='out', amount=250, account_id=self.cash.id,
            category_id=cats['Miscellaneous'].id, idempotency_key='RETRY-1',
            actor=self.actor)
        second, created2 = save_manual_cash_flow_entry(
            direction='out', amount=250, account_id=self.cash.id,
            category_id=cats['Miscellaneous'].id, idempotency_key='RETRY-1',
            actor=self.actor)
        db.session.commit()
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(first.id, second.id)
        self.assertEqual(CashFlowEntry.query.filter_by(idempotency_key='RETRY-1').count(), 1)

    # ── immutability ─────────────────────────────────────────────────────

    def test_amend_voids_and_replaces_without_editing_the_original(self):
        cats = self._categories()
        original = self._entry('out', 4500.25,
                               category_id=cats['Material & Purchase'].id,
                               party_name='Al-Kabir Store')
        original_amount = float(original.amount)
        original_tx_id = original.account_tx_id

        new_entry, old_entry = amend_manual_cash_flow_entry(
            original, amount=5000.0, reason='Wrong amount on slip', actor=self.actor)
        db.session.commit()

        # the original is untouched apart from being voided
        self.assertTrue(old_entry.is_void)
        self.assertEqual(float(old_entry.amount), original_amount)
        self.assertEqual(old_entry.account_tx_id, original_tx_id)
        self.assertTrue(db.session.get(AccountTransaction, original_tx_id).is_void)
        # ...and the replacement is a brand new row
        self.assertNotEqual(new_entry.id, old_entry.id)
        self.assertEqual(float(new_entry.amount), 5000.0)
        self.assertFalse(new_entry.is_void)
        # both ends are linked
        self.assertEqual(new_entry.amends_entry_id, old_entry.id)
        self.assertEqual(old_entry.superseded_by_entry_id, new_entry.id)

        # the ledger nets to the corrected figure
        from hdc.services.accounts import _account_balance_map
        self.assertAlmostEqual(_account_balance_map()[self.cash.id],
                               100000 - 5000, places=2)

    def test_every_state_change_writes_an_audit_row(self):
        cats = self._categories()
        entry = self._entry('out', 100, category_id=cats['Miscellaneous'].id)
        new_entry, old_entry = amend_manual_cash_flow_entry(
            entry, amount=200, reason='correction', actor=self.actor)
        db.session.commit()
        void_manual_cash_flow_entry(new_entry, reason='made in error', actor=self.actor)
        db.session.commit()

        actions = [a.action for a in (CashFlowEntryAudit.query
                                      .filter_by(entry_id=new_entry.id)
                                      .order_by(CashFlowEntryAudit.id).all())]
        self.assertIn('Created', actions)
        self.assertIn('Amended', actions)
        self.assertIn('Voided', actions)
        # the reason survives on the void
        void_row = (CashFlowEntryAudit.query
                    .filter_by(entry_id=new_entry.id, action='Voided').first())
        self.assertEqual(void_row.reason, 'made in error')
        self.assertEqual(void_row.changed_by, self.actor)

    def test_void_keeps_the_row_and_restores_the_balance(self):
        cats = self._categories()
        entry = self._entry('out', 4000, category_id=cats['Miscellaneous'].id)
        before = float(entry.amount)
        void_manual_cash_flow_entry(entry, reason='duplicate entry', actor=self.actor)
        db.session.commit()

        self.assertTrue(entry.is_void)
        self.assertEqual(entry.void_reason, 'duplicate entry')
        self.assertEqual(entry.voided_by, self.actor)
        self.assertIsNotNone(entry.voided_at)
        self.assertEqual(CashFlowEntry.query.count(), 1, 'row must not be deleted')
        self.assertTrue(db.session.get(AccountTransaction, entry.account_tx_id).is_void)

        from hdc.services.accounts import _account_balance_map
        self.assertAlmostEqual(_account_balance_map()[self.cash.id], 100000, places=2)
        self.assertEqual(float(entry.amount), before)

    def test_void_twice_is_rejected_but_restore_works(self):
        cats = self._categories()
        entry = self._entry('out', 100, category_id=cats['Miscellaneous'].id)
        void_manual_cash_flow_entry(entry, reason='x', actor=self.actor)
        db.session.commit()
        with self.assertRaises(ValueError):
            void_manual_cash_flow_entry(entry, reason='again', actor=self.actor)

        restore_manual_cash_flow_entry(entry, actor=self.actor)
        db.session.commit()
        self.assertFalse(entry.is_void)
        self.assertIsNone(entry.void_reason)
        self.assertFalse(db.session.get(AccountTransaction, entry.account_tx_id).is_void)

    def test_amending_a_voided_entry_is_rejected(self):
        cats = self._categories()
        entry = self._entry('out', 100, category_id=cats['Miscellaneous'].id)
        void_manual_cash_flow_entry(entry, reason='x', actor=self.actor)
        db.session.commit()
        with self.assertRaises(ValueError):
            amend_manual_cash_flow_entry(entry, amount=200, reason='y', actor=self.actor)

    # ── reconciliation + day lock ────────────────────────────────────────

    def _position(self, day, account):
        return next(p for p in day_positions(day) if p.account_id == account.id)

    def test_day_position_matches_the_ledger(self):
        d = date(2026, 9, 1)
        cats = self._categories()
        save_manual_cash_flow_entry(direction='in', amount=2500, account_id=self.cash.id,
                                    category_id=cats['Other Income'].id,
                                    date_posted=datetime.combine(d, datetime.min.time()),
                                    actor=self.actor)
        save_manual_cash_flow_entry(direction='out', amount=750.50, account_id=self.cash.id,
                                    category_id=cats['Material & Purchase'].id,
                                    date_posted=datetime.combine(d, datetime.min.time()),
                                    actor=self.actor)
        db.session.commit()
        pos = self._position(d, self.cash)
        self.assertEqual(pos.opening_minor, 10000000)
        self.assertEqual(pos.amount_in_minor, 250000)
        self.assertEqual(pos.amount_out_minor, 75050)
        self.assertEqual(pos.expected_closing_minor, 10000000 + 250000 - 75050)

    def test_reconciliation_is_correct_on_legacy_rows_without_the_mirror(self):
        """A database upgraded in place has rows with a NULL amount_minor.

        Reconciliation must still be exact for them, deriving paisa from the
        legacy float column — no backfill pass required.
        """
        from sqlalchemy import text
        d = date(2026, 9, 1)
        cats = self._categories()
        save_manual_cash_flow_entry(
            direction='out', amount=1234.56, account_id=self.cash.id,
            category_id=cats['Miscellaneous'].id,
            date_posted=datetime.combine(d, datetime.min.time()), actor=self.actor)
        db.session.commit()

        # Strip the mirror with raw SQL: assigning None through the ORM would
        # simply be re-synced by the model's before_update listener.
        db.session.execute(text('UPDATE hdc_account_txn SET amount_minor = NULL'))
        db.session.commit()
        self.assertIsNone(AccountTransaction.query.first().amount_minor)

        pos = self._position(d, self.cash)
        self.assertEqual(pos.amount_out_minor, 123456,
                         'paisa must be derived from the float column')
        self.assertEqual(pos.expected_closing_minor, 10000000 - 123456)

        # and the same day still reconciles + locks end to end
        save_counted_position(d, self.cash.id, float(pos.expected_closing),
                              actor=self.actor)
        db.session.commit()
        for acc in (self.bank,):
            p = self._position(d, acc)
            save_counted_position(d, acc.id, float(p.expected_closing), actor=self.actor)
        db.session.commit()
        lock_cash_day(d, actor=self.actor)
        db.session.commit()
        self.assertTrue(is_day_locked(d))

    def test_counted_difference_and_lock(self):
        d = date(2026, 9, 1)
        pos = self._position(d, self.cash)
        db.session.commit()
        save_counted_position(d, self.cash.id, pos.expected_closing - 100, actor=self.actor)
        db.session.commit()
        pos = self._position(d, self.cash)
        self.assertEqual(pos.difference_minor, -10000)
        self.assertAlmostEqual(float(pos.difference), -100.0, places=2)

    def test_lock_requires_every_active_account_to_be_counted(self):
        d = date(2026, 9, 1)
        day_positions(d)
        db.session.commit()
        with self.assertRaises(ValueError) as ctx:
            lock_cash_day(d, actor=self.actor)
        self.assertIn('Enter the counted closing', str(ctx.exception))
        self.assertFalse(is_day_locked(d))

    def test_dormant_accounts_do_not_block_the_close(self):
        """An account with no balance and no movement has nothing to count."""
        d = date(2026, 9, 1)
        empty = self._mk_account('Idle Bank', 'bank', bank_name='X', account_number='1')
        day_positions(d)
        db.session.commit()
        for acc in (self.cash, self.bank, empty):
            p = self._position(d, acc)
            save_counted_position(d, acc.id, float(p.expected_closing), actor=self.actor)
        db.session.commit()
        lock = lock_cash_day(d, actor=self.actor, note='eod')
        db.session.commit()
        self.assertIsNotNone(day_lock_state(d))
        self.assertEqual(lock.locked_by, self.actor)

    def test_locked_day_carries_counted_forward_as_next_opening(self):
        d1 = date(2026, 9, 1)
        d2 = d1 + timedelta(days=1)
        day_positions(d1)
        db.session.commit()
        for acc in (self.cash, self.bank):
            p = self._position(d1, acc)
            save_counted_position(d1, acc.id, float(p.expected_closing), actor=self.actor)
        db.session.commit()
        # count the cash 100 short on purpose
        p = self._position(d1, self.cash)
        save_counted_position(d1, self.cash.id, float(p.expected_closing) - 100,
                              actor=self.actor)
        db.session.commit()
        counted = float(self._position(d1, self.cash).counted)

        lock_cash_day(d1, actor=self.actor, note='eod')
        db.session.commit()

        next_pos = self._position(d2, self.cash)
        self.assertAlmostEqual(float(next_pos.opening), counted, places=2,
                               msg='counted closing must become next day opening')

        # the shortage is on the record as a reconciliation snapshot
        rec = AccountReconciliation.query.filter_by(account_id=self.cash.id).first()
        self.assertIsNotNone(rec)
        self.assertEqual(rec.difference_minor, -10000)
        self.assertEqual(rec.difference_type, 'Loss')
        self.assertEqual(rec.status, 'Reconciled')

    def test_locked_day_blocks_posting_voiding_and_counting(self):
        d = date(2026, 9, 1)
        cats = self._categories()
        entry, _ = save_manual_cash_flow_entry(
            direction='out', amount=500, account_id=self.cash.id,
            category_id=cats['Miscellaneous'].id,
            date_posted=datetime.combine(d, datetime.min.time()), actor=self.actor)
        db.session.commit()
        day_positions(d)
        db.session.commit()
        for acc in (self.cash, self.bank):
            p = self._position(d, acc)
            save_counted_position(d, acc.id, float(p.expected_closing), actor=self.actor)
        db.session.commit()
        lock_cash_day(d, actor=self.actor)
        db.session.commit()

        with self.assertRaises(ValueError) as ctx:
            save_manual_cash_flow_entry(direction='out', amount=1, account_id=self.cash.id,
                                        category_id=cats['Miscellaneous'].id,
                                        date_posted=datetime.combine(d, datetime.min.time()),
                                        actor=self.actor)
        self.assertIn('is locked', str(ctx.exception))

        with self.assertRaises(ValueError):
            void_manual_cash_flow_entry(entry, reason='x', actor=self.actor)
        with self.assertRaises(ValueError):
            save_counted_position(d, self.cash.id, 1, actor=self.actor)

    def test_unlock_reopens_the_day_and_keeps_snapshots(self):
        d = date(2026, 9, 1)
        day_positions(d)
        db.session.commit()
        for acc in (self.cash, self.bank):
            p = self._position(d, acc)
            save_counted_position(d, acc.id, float(p.expected_closing), actor=self.actor)
        db.session.commit()
        lock_cash_day(d, actor=self.actor)
        db.session.commit()
        self.assertTrue(is_day_locked(d))

        unlock_cash_day(d)
        db.session.commit()
        self.assertFalse(is_day_locked(d))
        self.assertIsNone(day_lock_state(d))
        self.assertFalse(self._position(d, self.cash).is_locked)

    def test_day_totals(self):
        d = date(2026, 9, 1)
        cats = self._categories()
        save_manual_cash_flow_entry(direction='in', amount=1000, account_id=self.cash.id,
                                    category_id=cats['Other Income'].id,
                                    date_posted=datetime.combine(d, datetime.min.time()),
                                    actor=self.actor)
        db.session.commit()
        totals = day_totals(day_positions(d))
        self.assertEqual(totals['amount_in_minor'], 100000)
        self.assertEqual(totals['opening_minor'], 15000000)   # 100000 + 50000

    # ── reads ────────────────────────────────────────────────────────────

    def test_register_filters_and_summary(self):
        d1 = date(2026, 9, 1)
        d2 = date(2026, 9, 5)
        cats = self._categories()
        save_manual_cash_flow_entry(direction='in', amount=1000, account_id=self.cash.id,
                                    category_id=cats['Other Income'].id,
                                    date_posted=datetime.combine(d1, datetime.min.time()),
                                    actor=self.actor)
        save_manual_cash_flow_entry(direction='out', amount=250, account_id=self.bank.id,
                                    category_id=cats['Miscellaneous'].id,
                                    date_posted=datetime.combine(d2, datetime.min.time()),
                                    actor=self.actor)
        db.session.commit()

        self.assertEqual(len(register_rows()), 2)
        self.assertEqual(len(register_rows(date_from=d1, date_to=d1)), 1)
        self.assertEqual(len(register_rows(direction='in')), 1)
        self.assertEqual(len(register_rows(account_id=self.bank.id)), 1)

        summary = register_summary(register_rows())
        self.assertEqual(summary['total_in_minor'], 100000)
        self.assertEqual(summary['total_out_minor'], 25000)
        self.assertEqual(summary['net_minor'], 75000)

    def test_voided_rows_are_hidden_by_default_and_counted_in_summary(self):
        cats = self._categories()
        entry = self._entry('out', 100, category_id=cats['Miscellaneous'].id)
        void_manual_cash_flow_entry(entry, reason='x', actor=self.actor)
        db.session.commit()
        self.assertEqual(len(register_rows()), 1)
        self.assertEqual(len(register_rows(include_void=False)), 0)
        s = register_summary(register_rows())
        self.assertEqual(s['void_count'], 1)
        self.assertEqual(s['total_out_minor'], 0, 'void money must not be counted')

    def test_row_dicts_carry_party_and_category_labels(self):
        cats = self._categories()
        self._entry('out', 100, category_id=cats['Material & Purchase'].id,
                    party_name='Al-Kabir Store')
        row = register_row_dicts(register_rows())[0]
        self.assertEqual(row['party_name'], 'Al-Kabir Store')
        self.assertEqual(row['category'], 'Material & Purchase')
        self.assertEqual(row['account'], 'Test Cash')
        self.assertEqual(row['direction_label'], 'Spent')
        self.assertEqual(row['amount'], 100.0)

    # ── vocabulary ───────────────────────────────────────────────────────

    def test_party_is_created_once_and_reused(self):
        p1, created1 = save_cf_party('Al-Kabir Store', 'supplier')
        p2, created2 = save_cf_party('al-kabir store', 'supplier')
        db.session.commit()
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(p1.id, p2.id, 'party match must be case-insensitive')
        self.assertEqual(CashFlowParty.query.count(), 1)

    def test_entry_links_to_resolved_party(self):
        cats = self._categories()
        entry = self._entry('out', 100, category_id=cats['Miscellaneous'].id,
                            party_name='Al-Kabir Store', party_type='supplier')
        self.assertIsNotNone(entry.party_id)
        self.assertEqual(entry.party.name, 'Al-Kabir Store')
        self.assertEqual(entry.party_type, 'supplier')

    # ── routes ───────────────────────────────────────────────────────────

    def test_register_and_reconciliation_pages_render(self):
        for url in ('/hdc/accounts/cashflow/register',
                    '/hdc/accounts/cashflow/reconciliation'):
            res = self.client.get(url)
            self.assertEqual(res.status_code, 200, url)

    def test_route_creates_entry_from_the_form(self):
        cats = self._categories()
        res = self._post('/hdc/accounts/cashflow/register', {
            'action': 'create_entry',
            'direction': 'out',
            'amount': '1,250.75',
            'account_id': self.cash.id,
            'category_id': cats['Material & Purchase'].id,
            'party_name': 'Route Party',
            'description': 'Cement',
        })
        self.assertEqual(res.status_code, 302)
        entry = CashFlowEntry.query.filter_by(party_name='Route Party').first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.amount_minor, 125075)
        self.assertEqual(entry.created_by, 'admin')

    def test_route_rejects_bad_amount_with_a_flash(self):
        cats = self._categories()
        self._post('/hdc/accounts/cashflow/register', {
            'action': 'create_entry', 'direction': 'out', 'amount': 'not-a-number',
            'account_id': self.cash.id, 'category_id': cats['Miscellaneous'].id,
        })
        html = self.client.get('/hdc/accounts/cashflow/register').get_data(as_text=True)
        self.assertIn('must be a valid number', html)
        self.assertEqual(CashFlowEntry.query.count(), 0)

    def test_route_requires_a_reason_to_void(self):
        cats = self._categories()
        entry = self._entry('out', 100, category_id=cats['Miscellaneous'].id)
        self._post('/hdc/accounts/cashflow/register', {
            'action': 'void_entry', 'entry_id': entry.id, 'reason': ''})
        html = self.client.get('/hdc/accounts/cashflow/register').get_data(as_text=True)
        self.assertIn('A reason is required', html)
        self.assertFalse(entry.is_void)

    def test_route_voids_and_restores(self):
        cats = self._categories()
        entry = self._entry('out', 100, category_id=cats['Miscellaneous'].id)
        self._post('/hdc/accounts/cashflow/register', {
            'action': 'void_entry', 'entry_id': entry.id, 'reason': 'entered twice'})
        self.assertTrue(CashFlowEntry.query.get(entry.id).is_void)
        self._post('/hdc/accounts/cashflow/register', {
            'action': 'restore_entry', 'entry_id': entry.id})
        self.assertFalse(CashFlowEntry.query.get(entry.id).is_void)

    def test_route_amends_through_the_form(self):
        cats = self._categories()
        entry = self._entry('out', 1000, category_id=cats['Miscellaneous'].id)
        self._post('/hdc/accounts/cashflow/register', {
            'action': 'amend_entry', 'entry_id': entry.id, 'amount': '1,500',
            'reason': 'wrong amount'})
        old = CashFlowEntry.query.get(entry.id)
        new = CashFlowEntry.query.filter_by(amends_entry_id=entry.id).first()
        self.assertTrue(old.is_void)
        self.assertIsNotNone(new)
        self.assertEqual(new.amount_minor, 150000)

    def test_reconciliation_route_saves_counted_and_locks(self):
        d = date(2026, 9, 1)
        positions = day_positions(d)
        db.session.commit()
        data = {'action': 'save_counted'}
        for p in positions:
            data.setdefault('account_id', []).append(str(p.account_id))
            data['counted_%s' % p.account_id] = str(float(p.expected_closing))
        self._post('/hdc/accounts/cashflow/reconciliation?day=%s' % d.isoformat(), data)
        self.assertFalse(is_day_locked(d))

        res = self._post('/hdc/accounts/cashflow/reconciliation?day=%s' % d.isoformat(),
                         {'action': 'lock_day', 'note': 'eod'})
        self.assertEqual(res.status_code, 302)
        self.assertTrue(is_day_locked(d))
        self.assertEqual(CashDayLock.query.filter_by(lock_date=d).count(), 1)

        self._post('/hdc/accounts/cashflow/reconciliation?day=%s' % d.isoformat(),
                   {'action': 'unlock_day'})
        self.assertFalse(is_day_locked(d))

    def test_csv_export(self):
        cats = self._categories()
        self._entry('in', 5000, account=self.cash,
                    category_id=cats['Other Income'].id,
                    party_name='CSV Client', reference='CHQ-1')
        res = self.client.get('/hdc/accounts/cashflow/register/export')
        self.assertEqual(res.status_code, 200)
        self.assertIn('text/csv', res.content_type)
        text = res.get_data(as_text=True)
        self.assertIn('cashflow_register_', res.headers.get('Content-Disposition', ''))
        self.assertIn('CSV Client', text)
        # CSV amounts must NOT be comma-grouped or they break the columns.
        self.assertIn('5000.00', text)
        self.assertIn('Other Income', text)
        self.assertIn('Amount (PKR)', text)

    def test_pages_require_login(self):
        self.ctx.pop()
        try:
            anon = self.app.test_client()
            for url in ('/hdc/accounts/cashflow/register',
                        '/hdc/accounts/cashflow/reconciliation'):
                res = anon.get(url)
                self.assertEqual(res.status_code, 302, url)
                self.assertIn('/hdc/login', res.headers.get('Location', ''))
        finally:
            self.ctx = self.app.app_context()
            self.ctx.push()

    def test_backfill_mirrors_historical_rows(self):
        from hdc.services.cashflow_register import backfill_transaction_minor_units
        row = AccountTransaction(
            date=date(2026, 1, 1), amount=99.99, type='party_payment',
            from_account_id=self.cash.id, to_account_id=None,
            executed_by_account_id=self.cash.id, category='expense',
            is_void=False, created_at=_pkt_now_naive())
        db.session.add(row)
        db.session.commit()
        # Simulate a pre-migration row with raw SQL: assigning None through the
        # ORM would just be re-synced by the model's before_update listener.
        db.session.execute(
            text('UPDATE hdc_account_txn SET amount_minor = NULL WHERE id = :id'),
            {'id': row.id})
        db.session.commit()
        self.assertEqual(backfill_transaction_minor_units(), 1)
        self.assertEqual(row.amount_minor, 9999)
        self.assertEqual(backfill_transaction_minor_units(), 0, 'must be idempotent')


class ProjectReceiptTestCase(unittest.TestCase):
    """A project receipt posted in the register must reach the project.

    Before this behaviour existed, a payment recorded here named the project
    but credited a ``person`` account, so the project still read as unpaid and
    the Receivable KPI ignored it.  These tests pin the whole chain: the owner
    comes from the project, the project is mandatory, the money lands on
    ``Project.total_received``, and voiding takes it back off.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='hdc-cfproj-test-')
        self.app = create_app({'HDC_DB_PATH': os.path.join(self.tmp, 'test.db'),
                               'HDC_INSTANCE_DIR': self.tmp})
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.actor = 'admin'
        self.cash = Account(name='Proj Cash', type='cash', opening_balance=0.0,
                            status='active', is_void=False, created_at=_pkt_now_naive())
        db.session.add(self.cash)
        self.project = Project(project_code='PRJ-1', name='Hill View',
                               client='Abdul Rehman', contract_type='lump_sum',
                               owner_lump_sum=1000000.0, status='Active')
        db.session.add(self.project)
        db.session.commit()
        self.receipt_cat = {c.name: c for c in category_options()}['Owner / Client Receipt']

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _receipt(self, amount=250000, project=True, **kw):
        entry, _ = save_manual_cash_flow_entry(
            direction='in', amount=amount, account_id=self.cash.id,
            category_id=self.receipt_cat.id, actor=self.actor,
            project_id=(self.project.id if project else None), **kw)
        db.session.commit()
        return entry

    def test_receipt_category_requires_a_project(self):
        """Without a project the money has nowhere to land — refuse it."""
        with self.assertRaises(ValueError) as caught:
            self._receipt(project=False)
        self.assertIn('project', str(caught.exception).lower())

    def test_rules_force_project_required_even_on_seeded_rows(self):
        from hdc.services.cashflow_register import category_field_rules
        rules = category_field_rules(self.receipt_cat)
        self.assertEqual(rules['project_effect'], 'receipt')
        self.assertEqual(rules['project_mode'], 'required')

    def test_owner_is_taken_from_the_project_not_the_typed_name(self):
        """The project already knows its owner: a typed name cannot override it."""
        entry = self._receipt(party_name='Someone Else', party_type='person')
        self.assertEqual(entry.party_name, 'Abdul Rehman')
        self.assertEqual(entry.party_type, 'client')

    def test_receipt_lands_on_the_project_and_uses_a_client_account(self):
        from hdc.models.accounts import OwnerPayment
        from hdc.services.accounts import _account_group_mode_for_row
        entry = self._receipt(250000)

        payments = OwnerPayment.query.filter_by(source_entry_id=entry.id).all()
        self.assertEqual(len(payments), 1, 'exactly one mirrored payment')
        self.assertAlmostEqual(payments[0].amount, 250000.0, places=2)

        db.session.expire_all()
        project = db.session.get(Project, self.project.id)
        self.assertAlmostEqual(project.total_received, 250000.0, places=2)
        self.assertAlmostEqual(project.remaining_receivable, 750000.0, places=2)

        # The counterparty must be a *client* account: a 'person' account is
        # filed under credit_debit and never reaches the Receivable KPI.
        tx = db.session.get(AccountTransaction, entry.account_tx_id)
        counterparty = db.session.get(Account, tx.from_account_id)
        self.assertEqual(counterparty.name, 'Abdul Rehman')
        self.assertEqual(counterparty.type, 'client')
        self.assertEqual(_account_group_mode_for_row(counterparty)[0], 'project_in_flow')

    def test_void_and_restore_keep_the_project_in_step(self):
        from hdc.models.accounts import OwnerPayment
        entry = self._receipt(250000)
        payment = OwnerPayment.query.filter_by(source_entry_id=entry.id).one()

        void_manual_cash_flow_entry(entry, reason='wrong project', actor=self.actor)
        db.session.commit()
        db.session.expire_all()
        self.assertTrue(db.session.get(OwnerPayment, payment.id).is_void)
        self.assertAlmostEqual(db.session.get(Project, self.project.id).total_received,
                               0.0, places=2)

        restore_manual_cash_flow_entry(entry, actor=self.actor)
        db.session.commit()
        db.session.expire_all()
        self.assertFalse(db.session.get(OwnerPayment, payment.id).is_void)
        self.assertAlmostEqual(db.session.get(Project, self.project.id).total_received,
                               250000.0, places=2)

    def test_mirror_is_not_duplicated_when_the_entry_is_re_saved(self):
        from hdc.models.accounts import OwnerPayment
        entry = self._receipt(100000, idempotency_key='DUP-1')
        again, created = save_manual_cash_flow_entry(
            direction='in', amount=100000, account_id=self.cash.id,
            category_id=self.receipt_cat.id, project_id=self.project.id,
            idempotency_key='DUP-1', actor=self.actor)
        db.session.commit()
        self.assertFalse(created)
        self.assertEqual(again.id, entry.id)
        self.assertEqual(OwnerPayment.query.filter_by(source_entry_id=entry.id).count(), 1)

    def test_backfill_repairs_receipts_posted_before_the_mirror_existed(self):
        from hdc.models.accounts import OwnerPayment
        from hdc.services.cashflow_register import (
            backfill_project_receipt_owner_payments as backfill,
        )
        entry = self._receipt(400000)
        # Simulate the historical state: the entry exists, the mirror does not.
        OwnerPayment.query.filter_by(source_entry_id=entry.id).delete()
        db.session.commit()
        db.session.expire_all()
        self.assertAlmostEqual(db.session.get(Project, self.project.id).total_received,
                               0.0, places=2)

        stats = backfill()
        self.assertEqual(stats['created'], 1)
        db.session.expire_all()
        self.assertAlmostEqual(db.session.get(Project, self.project.id).total_received,
                               400000.0, places=2)

        # Re-running must not double-count.
        self.assertEqual(backfill()['created'], 0)
        db.session.expire_all()
        self.assertAlmostEqual(db.session.get(Project, self.project.id).total_received,
                               400000.0, places=2)

    def test_backfill_leaves_payments_from_other_surfaces_alone(self):
        """Rows posted by the Projects page have no entry link — never touch them."""
        from hdc.models.accounts import OwnerPayment
        from hdc.services.cashflow_register import (
            backfill_project_receipt_owner_payments as backfill,
        )
        db.session.add(OwnerPayment(project_id=self.project.id, amount=50000.0,
                                    date=date(2026, 3, 1), remarks='entered on the project',
                                    activity_at=_pkt_now_naive(), is_void=False))
        db.session.commit()
        before = OwnerPayment.query.count()
        stats = backfill()
        self.assertEqual(stats['created'], 0)
        self.assertEqual(OwnerPayment.query.count(), before)
        db.session.expire_all()
        self.assertAlmostEqual(db.session.get(Project, self.project.id).total_received,
                               50000.0, places=2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
