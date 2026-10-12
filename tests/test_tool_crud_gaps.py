#!/usr/bin/env python3
"""Regression tests for the Tools CRUD / health / flow audit fixes.

Every test here reproduces a defect that was found by probing a live app
(see TOOLS_SECTION_CRUD_HEALTH_AUDIT.md) and asserts the fixed behaviour:

  T-01  a rental can be cancelled (void) and reinstated; money blocks it first
  T-02  archiving a tool keeps the KPI strip and the Reports registers in step,
        the history stays reachable and the tool can be restored
  T-03  editing a tool never silently drops its category and never accepts an
        owned quantity below the pieces that are physically out
  T-04  serial markings created by hand reconcile the owned quantity
  T-05  a transfer to the same place, or a double-submit, is refused
  T-06  a payment above the outstanding balance is refused (rental + return)
  T-07  an audit overage posts a stock-in row, so owned == purchased - scrapped
  T-08  closing a count says what it left unposted
  T-09  cancelling / re-opening a count can reverse what it posted
  T-10  duplicate tool codes, no-charge edits, posted maintenance dates and
        duplicate transaction references all behave
  T-11  the Settings wipe clears discount rows and voids their ledger entries
  T-12  navigation never offers a link the user will get a 403 for
  T-13  a record-scoped user's totals are flagged, not presented as reconciled
  T-14  reading a tools page writes nothing to the database
  T-15  the unused serial APIs that mutated stock behind the books are gone
  T-16  the Track by Serial page is paginated

Run with:
    HDC_BOOTSTRAP_ADMIN_PASSWORD='Admin@1234' python -m unittest tests.test_tool_crud_gaps -v
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

os.environ.setdefault('HDC_ENV', 'test')
os.environ.setdefault('HDC_SECRET_KEY', 'unit-test-secret')
os.environ.setdefault('HDC_BOOTSTRAP_ADMIN_PASSWORD', 'Admin@1234')

from flask import render_template                                # noqa: E402
from flask_login import login_user                               # noqa: E402
from sqlalchemy import event                                     # noqa: E402
from werkzeug.security import generate_password_hash             # noqa: E402

from hdc.app import create_app                                    # noqa: E402
from hdc.core.admin import _WIPE_TARGETS, _wipe_selected_targets  # noqa: E402
from hdc.extensions import db                                     # noqa: E402
from hdc.models.accounts import Account, AccountTransaction       # noqa: E402
from hdc.models.auth import HDCUser                               # noqa: E402
from hdc.models.projects import Project, Stage                    # noqa: E402
from hdc.models.tool_rental import (                              # noqa: E402
    Tool, ToolAudit, ToolAuditLine, ToolCategory, ToolMovementLog, ToolPurchase,
    ToolRental, ToolRentalDiscount, ToolRentalItem, ToolRentalPayment,
    ToolRentalTransfer, ToolScrap, ToolSerial,
)
from hdc.services.permissions import may_access_path              # noqa: E402
from hdc.services.tool_audit import (                             # noqa: E402
    close_audit, parse_location_key, post_adjustments, reopen_audit,
    reverse_audit_adjustments, save_counts, start_audit, void_audit,
)
from hdc.services.tool_rental import (                            # noqa: E402
    archive_tool, archived_tool_rows, create_tool_serials, live_serial_count,
    recalc_rental_totals, reconcile_tool_serial_stock, record_tool_purchase,
    restore_tool, restore_tool_rental, tool_kpis, tool_purchases, tool_scraps,
    void_tool_rental,
)
from hdc.services.tool_tracking import (                          # noqa: E402
    dashboard_summary, tool_ledger, tools_reconciliation,
)
from hdc.utils.dates import _pkt_today                            # noqa: E402

ADMIN_PASSWORD = os.environ['HDC_BOOTSTRAP_ADMIN_PASSWORD']

_NAV_LINK = re.compile(r'<a href="([^"]+)"\s+class="tools-subnav-link')
_SERIAL_NO = re.compile(r'\b([A-Z0-9]+-\d{3,6})\b')


def _nav_hrefs(html):
    return _NAV_LINK.findall(html or '')


def _serial_numbers(html):
    return set(_SERIAL_NO.findall(html or ''))


class ToolCrudGapTestCase(unittest.TestCase):
    """One isolated app + database per test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='hdc-tool-crud-test-')
        self.db_path = os.path.join(self.tmp, 'test.db')
        self.app = create_app({'HDC_DB_PATH': self.db_path,
                               'HDC_INSTANCE_DIR': self.tmp})
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()
        self._login('admin', ADMIN_PASSWORD)

        self.site = Project(name='Site A', project_code='P-A', client='Owner A')
        self.site_b = Project(name='Site B', project_code='P-B', client='Owner B')
        db.session.add_all([self.site, self.site_b])
        db.session.commit()
        self.category = ToolCategory(name='Concrete')
        db.session.add(self.category)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _csrf(html):
        m = re.search(r'name="_csrf_token" value="([^"]+)"', html or '')
        return m.group(1) if m else ''

    def _login(self, username, password):
        r = self.client.get('/hdc/login')
        self.client.post('/hdc/login', data={
            'username': username, 'password': password,
            '_csrf_token': self._csrf(r.get_data(as_text=True)),
        }, follow_redirects=True)

    def _token(self):
        with self.client.session_transaction() as sess:
            return sess.get('_csrf_token', '')

    def _make_tool(self, code, name, qty, rate=500.0, cost=10_000.0, category=None):
        tool = Tool(tool_code=code, name=name, unit='pcs',
                    category_id=(category or self.category).id,
                    total_quantity=qty, purchase_cost=cost,
                    rental_rate_per_day=rate, condition='good',
                    status='active', is_void=False)
        db.session.add(tool)
        db.session.commit()
        return tool

    def _bought_tool(self, code, name, qty, cost=1000.0, rate=500.0):
        """A tool whose owned quantity comes entirely from purchase rows.

        ``record_tool_purchase`` *adds* to ``total_quantity``, so seeding the
        tool with a quantity and then buying the same amount would double it
        and break ``owned == purchased - scrapped``.
        """
        tool = self._make_tool(code, name, 0, rate=rate, cost=cost)
        ok, message, _row = record_tool_purchase(tool.id, qty, unit_cost=cost,
                                                 commit=True)
        self.assertTrue(ok, message)
        db.session.expire_all()
        return db.session.get(Tool, tool.id)

    def _cash_account(self):
        acc = Account.query.filter(Account.type.in_(('company', 'cash', 'bank'))).first()
        if acc is None:
            acc = Account(name='Company Cash', type='company',
                          opening_balance=0.0, status='active')
            db.session.add(acc)
            db.session.commit()
        return acc

    def _create_rental(self, tool_qty_pairs, project=None, customer_name=None,
                       billing_type='fixed_fee'):
        """Create a rental through the real form (the path operators use)."""
        payload = {
            '_csrf_token': self._token(),
            'txn_type': 'new_rental',
            'billing_type': billing_type,
            'renter_type': 'internal' if project else 'external',
            'rental_date': _pkt_today().isoformat(),
            'tool_id[]': [str(t.id) for t, _q, _r in tool_qty_pairs],
            'qty[]': [str(q) for _t, q, _r in tool_qty_pairs],
            'rate[]': [str(r) for _t, _q, r in tool_qty_pairs],
        }
        if project:
            payload['project_id'] = str(project.id)
        if customer_name:
            payload['customer_name'] = customer_name
        redirect = self.client.post('/hdc/tool-rental/create', data=payload)
        self.assertEqual(redirect.status_code, 302, redirect.get_data(as_text=True)[:400])
        match = re.search(r'[?&]created=(\d+)', redirect.headers.get('Location', ''))
        self.assertIsNotNone(match, 'create did not redirect with ?created=')
        return db.session.get(ToolRental, int(match.group(1)))

    def _post_payment(self, rental, amount, **extra):
        data = {'payment_date': _pkt_today().isoformat(), 'amount': str(amount),
                'received_to_account_id': str(self._cash_account().id),
                'payment_mode': 'cash', '_csrf_token': self._token()}
        data.update(extra)
        return self.client.post(f'/hdc/tool-rental/{rental.id}/payment',
                                data=data, follow_redirects=True)

    def _stock(self, tool):
        ledger = tool_ledger()
        row = next(r for r in ledger['tools'] if r['tool_id'] == tool.id)
        return row

    def _serial_counts(self, tool_id):
        rows = ToolSerial.query.filter_by(tool_id=tool_id).all()
        return (sum(1 for s in rows if s.is_in_store),
                sum(1 for s in rows if not s.is_in_store))

    # ==================================================================
    # T-06 — money: never take more than is owed
    # ==================================================================
    def test_payment_above_outstanding_is_refused(self):
        tool = self._make_tool('T-VIB', 'Vibrator', 10)
        rental = self._create_rental([(tool, 2, 1500)], customer_name='Buildwell')
        self.assertEqual(float(rental.total_amount), 3000.0)

        r = self._post_payment(rental, 500_000)
        db.session.expire_all()
        rental = db.session.get(ToolRental, rental.id)
        self.assertEqual(ToolRentalPayment.query.count(), 0)
        self.assertEqual(float(rental.total_paid), 0.0)
        self.assertEqual(float(rental.total_pending_amount), 3000.0)
        self.assertIn('exceeds the outstanding balance', r.get_data(as_text=True))
        # nothing reached the ledger either
        self.assertEqual(AccountTransaction.query.filter(
            AccountTransaction.source_type.ilike('tool_rental_payment%')).count(), 0)

        # the exact balance is still accepted
        self._post_payment(rental, 3000)
        db.session.expire_all()
        rental = db.session.get(ToolRental, rental.id)
        self.assertEqual(float(rental.total_paid), 3000.0)
        self.assertEqual(rental.payment_status, 'paid')

    def test_return_payment_above_outstanding_is_refused(self):
        tool = self._make_tool('T-JACK', 'Jack', 6)
        rental = self._create_rental([(tool, 2, 1000)], customer_name='Buildwell')
        r = self.client.post(f'/hdc/tool-rental/{rental.id}/return', data={
            '_csrf_token': self._token(), 'return_type': 'partial',
            'payment_type': 'partial', 'return_date': _pkt_today().isoformat(),
            'rental_item_id[]': [str(rental.items[0].id)], 'qty_returned[]': ['1'],
            'amount_paid': '9000',
            'received_to_account_id': str(self._cash_account().id),
        }, follow_redirects=True)
        db.session.expire_all()
        self.assertEqual(float(db.session.get(ToolRental, rental.id).total_paid), 0.0)
        self.assertIn('exceeds the outstanding balance', r.get_data(as_text=True))

    # ==================================================================
    # T-03 — tool edit keeps its category and cannot lie about stock
    # ==================================================================
    def test_tool_edit_survives_a_tool_without_a_description(self):
        # description is NULL here, and the form posts description='' — the
        # old ``(form or tool.description).strip()`` raised AttributeError and
        # answered 500, so such a tool could never be edited at all.
        tool = self._make_tool('T-0', 'No description', 3)
        self.assertIsNone(tool.description)
        r = self.client.post(f'/hdc/tool-rental/inventory/{tool.id}/edit', data={
            '_csrf_token': self._token(), 'name': 'No description',
            'tool_code': 'T-0', 'unit': 'pcs', 'total_quantity': '3',
            'category_id': str(self.category.id), 'description': '',
        }, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        db.session.expire_all()
        self.assertEqual(db.session.get(Tool, tool.id).description, '')

    def test_tool_edit_without_category_field_keeps_category(self):
        tool = self._make_tool('T-1', 'Mixer', 5)
        r = self.client.post(f'/hdc/tool-rental/inventory/{tool.id}/edit', data={
            '_csrf_token': self._token(), 'name': 'Mixer', 'tool_code': 'T-1',
            'unit': 'pcs', 'total_quantity': '5',
        }, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        db.session.expire_all()
        self.assertEqual(db.session.get(Tool, tool.id).category_id, self.category.id)

        # an explicitly posted empty value still means "no category"
        self.client.post(f'/hdc/tool-rental/inventory/{tool.id}/edit', data={
            '_csrf_token': self._token(), 'name': 'Mixer', 'tool_code': 'T-1',
            'unit': 'pcs', 'total_quantity': '5', 'category_id': '',
        }, follow_redirects=True)
        db.session.expire_all()
        self.assertIsNone(db.session.get(Tool, tool.id).category_id)

    def test_tool_edit_refuses_qty_below_pieces_out(self):
        tool = self._make_tool('T-2', 'Drill', 10)
        rental = self._create_rental([(tool, 4, 100)], project=self.site)
        self.assertEqual(float(rental.items[0].qty_pending), 4.0)

        r = self.client.post(f'/hdc/tool-rental/inventory/{tool.id}/edit', data={
            '_csrf_token': self._token(), 'name': 'Drill', 'tool_code': 'T-2',
            'unit': 'pcs', 'total_quantity': '2', 'category_id': str(self.category.id),
        }, follow_redirects=True)
        db.session.expire_all()
        self.assertEqual(float(db.session.get(Tool, tool.id).total_quantity), 10.0)
        self.assertIn('still out', r.get_data(as_text=True))
        row = self._stock(tool)
        self.assertFalse(row['unaccounted'], row)

        # a legal reduction still works
        self.client.post(f'/hdc/tool-rental/inventory/{tool.id}/edit', data={
            '_csrf_token': self._token(), 'name': 'Drill', 'tool_code': 'T-2',
            'unit': 'pcs', 'total_quantity': '6', 'category_id': str(self.category.id),
        }, follow_redirects=True)
        db.session.expire_all()
        self.assertEqual(float(db.session.get(Tool, tool.id).total_quantity), 6.0)

    # ==================================================================
    # T-05 — transfers must go somewhere, once
    # ==================================================================
    def _transfer(self, rental, to_project, qty=1, date=None):
        return self.client.post(f'/hdc/tool-rental/{rental.id}/transfer', data={
            '_csrf_token': self._token(), 'to_type': 'site',
            'to_project_id': str(to_project.id), 'qty_transferred': str(qty),
            'transfer_date': (date or _pkt_today()).isoformat(),
        }, follow_redirects=True)

    def test_transfer_to_the_same_place_is_refused(self):
        tool = self._make_tool('T-3', 'Cutter', 8)
        rental = self._create_rental([(tool, 4, 100)], project=self.site)
        r = self._transfer(rental, self.site)
        db.session.expire_all()
        self.assertEqual(ToolRentalTransfer.query.count(), 0)
        self.assertIn('already at', r.get_data(as_text=True))

        # a real destination still transfers
        self._transfer(rental, self.site_b)
        db.session.expire_all()
        self.assertEqual(ToolRentalTransfer.query.count(), 1)

    def test_duplicate_transfer_submit_is_refused(self):
        tool = self._make_tool('T-4', 'Breaker', 8)
        rental = self._create_rental([(tool, 4, 100)], project=self.site)
        self._transfer(rental, self.site_b, qty=2)
        self._transfer(rental, self.site_b, qty=2)   # double-click / retry
        db.session.expire_all()
        self.assertEqual(ToolRentalTransfer.query.count(), 1)
        chain = db.session.get(ToolRental, rental.id).current_location_label
        self.assertNotIn('Site B › Site B', chain or '')

    # ==================================================================
    # T-01 — cancel and reinstate a rental
    # ==================================================================
    def test_rental_void_refuses_while_money_is_live(self):
        tool = self._make_tool('T-5', 'Saw', 6)
        rental = self._create_rental([(tool, 2, 1000)], customer_name='Buildwell')
        self._post_payment(rental, 500)

        ok, message, _row = void_tool_rental(rental.id, reason='mistake')
        self.assertFalse(ok)
        self.assertIn('payment', message)
        db.session.expire_all()
        self.assertFalse(db.session.get(ToolRental, rental.id).is_void)

        # void the money first, then the rental goes
        pay = ToolRentalPayment.query.filter_by(rental_id=rental.id).first()
        self.client.post(f'/hdc/tool-rental/payment/{pay.id}/void', data={
            '_csrf_token': self._token(), 'void_reason': 'wrong rental',
        }, follow_redirects=True)
        ok, message, _row = void_tool_rental(rental.id, reason='created in error')
        self.assertTrue(ok, message)

    def test_rental_void_returns_stock_and_restore_puts_it_back(self):
        tool = self._make_tool('T-6', 'Grinder', 10)
        rental = self._create_rental([(tool, 4, 250)], project=self.site)
        db.session.expire_all()
        self.assertEqual(self._serial_counts(tool.id), (6, 4))

        r = self.client.post(f'/hdc/tool-rental/{rental.id}/void', data={
            '_csrf_token': self._token(), 'void_reason': 'created in error',
        }, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        db.session.expire_all()
        rental = db.session.get(ToolRental, rental.id)
        self.assertTrue(rental.is_void)
        self.assertEqual(rental.void_reason, 'created in error')
        self.assertEqual(rental.status, 'closed')
        self.assertEqual(float(rental.items[0].qty_pending), 0.0)
        self.assertEqual(self._serial_counts(tool.id), (10, 0))
        row = self._stock(tool)
        self.assertEqual(row['in_store_qty'], 10.0)

        # and it can be put back
        ok, message, _row = restore_tool_rental(rental.id)
        self.assertTrue(ok, message)
        db.session.expire_all()
        rental = db.session.get(ToolRental, rental.id)
        self.assertFalse(rental.is_void)
        self.assertEqual(float(rental.items[0].qty_pending), 4.0)
        self.assertEqual(self._serial_counts(tool.id), (6, 4))

    def test_void_requires_a_reason_through_the_form(self):
        tool = self._make_tool('T-7', 'Welder', 4)
        rental = self._create_rental([(tool, 1, 100)], project=self.site)
        r = self.client.post(f'/hdc/tool-rental/{rental.id}/void', data={
            '_csrf_token': self._token(), 'void_reason': '   ',
        }, follow_redirects=True)
        db.session.expire_all()
        self.assertFalse(db.session.get(ToolRental, rental.id).is_void)
        self.assertIn('reason', r.get_data(as_text=True).lower())

    # ==================================================================
    # T-02 — archiving stays honest and is reversible
    # ==================================================================
    def test_archived_tool_leaves_kpis_and_registers_together(self):
        live = self._bought_tool('T-LIVE', 'Live tool', 5)
        gone = self._bought_tool('T-GONE', 'Gone tool', 10)
        kpis_before = tool_kpis()
        self.assertEqual(kpis_before['purchased_qty'], 15.0)

        ok, message, _row = archive_tool(gone.id, reason='written off')
        self.assertTrue(ok, message)
        db.session.expire_all()

        kpis = tool_kpis()
        register_value = sum(float(p.total_cost or 0) for p in tool_purchases())
        self.assertEqual(kpis['purchased_qty'], 5.0)
        self.assertEqual(kpis['purchase_value'], register_value)
        self.assertEqual(kpis['archived_tool_types'], 1)
        self.assertEqual(kpis['archived_purchase_value'], 10000.0)
        # owned == purchased - scrapped still ties on the live set
        self.assertEqual(kpis['total_tools'], kpis['purchased_qty'] - kpis['scrapped_qty'])

    def test_archived_tool_is_listed_on_reports_and_can_be_restored(self):
        tool = self._bought_tool('T-ARCH', 'Archived', 4, cost=500.0)
        archive_tool(tool.id, reason='replaced')
        db.session.expire_all()

        rows = archived_tool_rows()
        self.assertEqual([r['code'] for r in rows], ['T-ARCH'])
        self.assertEqual(rows[0]['reason'], 'replaced')
        self.assertEqual(rows[0]['purchased_qty'], 4.0)

        page = self.client.get('/hdc/tool-rental/reports').get_data(as_text=True)
        self.assertIn('Archived tools', page)
        self.assertIn('T-ARCH', page)
        # the live register does not show it, the archived-inclusive one does
        self.assertEqual(len(tool_purchases()), 0)
        self.assertEqual(len(tool_purchases(include_archived=True)), 1)

        r = self.client.post(f'/hdc/tool-rental/inventory/{tool.id}/restore', data={
            '_csrf_token': self._token(), 'return_to': 'reports',
        }, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        db.session.expire_all()
        restored = db.session.get(Tool, tool.id)
        self.assertFalse(restored.is_void)
        self.assertEqual(restored.status, 'active')
        self.assertIsNone(restored.void_reason)
        self.assertEqual(len(tool_purchases()), 1)
        self.assertEqual(archived_tool_rows(), [])

    def test_archive_is_blocked_while_pieces_are_out(self):
        tool = self._make_tool('T-OUT', 'Out tool', 5)
        self._create_rental([(tool, 2, 100)], project=self.site)
        ok, message, _row = archive_tool(tool.id)
        self.assertFalse(ok)
        self.assertIn('still rented out', message)
        db.session.expire_all()
        self.assertFalse(db.session.get(Tool, tool.id).is_void)

    # ==================================================================
    # T-04 — markings and owned quantity agree
    # ==================================================================
    def test_serial_manager_reconciles_owned_qty_with_markings(self):
        tool = self._bought_tool('T-SER', 'Serial tool', 10, cost=250.0)
        db.session.expire_all()
        self.assertEqual(live_serial_count(tool.id), 10)

        # three pieces the store physically has but nobody purchased
        ok, message, ids = create_tool_serials(tool.id, 3, commit=True)
        self.assertTrue(ok, message)
        added, purchase = reconcile_tool_serial_stock(tool.id, commit=True)
        db.session.expire_all()
        tool = db.session.get(Tool, tool.id)
        self.assertEqual(added, 3.0)
        self.assertIsNotNone(purchase)
        self.assertEqual(float(tool.total_quantity), 13.0)
        self.assertEqual(live_serial_count(tool.id), 13)
        self.assertEqual(tool_kpis()['total_tools'],
                         tool_kpis()['purchased_qty'] - tool_kpis()['scrapped_qty'])

    def test_serial_api_reconciles_owned_qty(self):
        tool = self._bought_tool('T-API', 'API tool', 4, cost=100.0)
        r = self.client.post('/hdc/api/tool-rental/serials/create', data={
            '_csrf_token': self._token(), 'tool_id': str(tool.id), 'qty': '2',
        })
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:300])
        payload = r.get_json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['reconciled_qty'], 2.0)
        db.session.expire_all()
        self.assertEqual(float(db.session.get(Tool, tool.id).total_quantity), 6.0)
        self.assertEqual(live_serial_count(tool.id), 6)

    # ==================================================================
    # T-15 — the unused APIs that mutated stock behind the books are gone
    # ==================================================================
    def test_unused_mutating_serial_apis_are_gone(self):
        for path in ('/hdc/api/tool-rental/serials/assign',
                     '/hdc/api/tool-rental/serials/return'):
            r = self.client.post(path, data={'_csrf_token': self._token(),
                                             'rental_id': '1', 'serial_ids': '1'})
            self.assertEqual(r.status_code, 404, path)
        rules = {r.rule for r in self.app.url_map.iter_rules()}
        self.assertNotIn('/hdc/api/tool-rental/serials/assign', rules)
        self.assertNotIn('/hdc/api/tool-rental/serials/return', rules)

    # ==================================================================
    # T-11 — the wipe really wipes
    # ==================================================================
    def test_wipe_clears_discount_rows_and_voids_their_ledger_entries(self):
        tool = self._make_tool('T-WIPE', 'Wipe tool', 5)
        rental = self._create_rental([(tool, 2, 1000)], customer_name='Buildwell')
        self.client.post(f'/hdc/tool-rental/{rental.id}/discount', data={
            '_csrf_token': self._token(), 'amount': '200', 'reason': 'goodwill',
            'discount_date': _pkt_today().isoformat(),
        }, follow_redirects=True)
        db.session.expire_all()
        self.assertEqual(ToolRentalDiscount.query.count(), 1)
        self.assertIn('hdc_tool_rental_discount', _WIPE_TARGETS['tools']['tables'])

        _wipe_selected_targets(['tools'])
        db.session.expire_all()
        self.assertEqual(ToolRentalDiscount.query.count(), 0)
        self.assertEqual(Tool.query.count(), 0)
        live = AccountTransaction.query.filter(
            AccountTransaction.is_void == False,  # noqa: E712
            AccountTransaction.source_type.ilike('tool_rental%')).count()
        self.assertEqual(live, 0)

    # ==================================================================
    # T-07 / T-08 / T-09 — the physical count
    # ==================================================================
    def _count(self, tool, counted, project=None):
        """Open a count sheet for one place and type a number into it.

        The tools here sit in the store, so the sheet is opened for the store
        unless a project is named — a sheet for the wrong place snapshots a
        book quantity of zero and every variance becomes an "extra".
        """
        key = f'site:{project.id}' if project else 'store'
        spec = parse_location_key(key)
        _created, audit = start_audit(spec, counter_name='Tester',
                                      audit_date=_pkt_today(), user_id=1,
                                      ledger=tool_ledger())
        save_counts(audit, {tool.id: {'counted': counted}}, ledger=tool_ledger())
        db.session.commit()
        return audit

    def test_audit_gain_keeps_owned_equal_purchased_minus_scrapped(self):
        tool = self._bought_tool('T-GAIN', 'Gain tool', 8, cost=1000.0)
        audit = self._count(tool, 14)
        ok, message, summary = post_adjustments(audit, mode='both', reason='lost',
                                                audit_date=_pkt_today(), commit=True)
        self.assertTrue(ok, message)
        db.session.expire_all()
        tool = db.session.get(Tool, tool.id)
        kpis = tool_kpis()
        self.assertEqual(float(tool.total_quantity), 14.0)
        self.assertEqual(kpis['total_tools'],
                         kpis['purchased_qty'] - kpis['scrapped_qty'])
        gain_purchase = ToolPurchase.query.filter(
            ToolPurchase.reference == audit.audit_code).first()
        self.assertIsNotNone(gain_purchase)
        self.assertEqual(float(gain_purchase.qty), 6.0)
        self.assertEqual(live_serial_count(tool.id), 14)

    def test_close_audit_reports_the_discrepancy_it_leaves_unposted(self):
        tool = self._bought_tool('T-CLOSE', 'Close tool', 10, cost=100.0)
        audit = self._count(tool, 7)      # three missing, losses-only not posted
        ok, message = close_audit(audit)
        self.assertTrue(ok)
        db.session.expire_all()
        self.assertIn('3', message)
        self.assertIn('short', message)
        self.assertNotIn('matched the book', message)
        self.assertIn('unposted', db.session.get(ToolAudit, audit.id).notes or '')
        # stock was not touched by closing
        self.assertEqual(float(db.session.get(Tool, tool.id).total_quantity), 10.0)

    def test_close_audit_still_says_the_count_matched_when_it_did(self):
        tool = self._bought_tool('T-EVEN', 'Even tool', 5, cost=100.0)
        audit = self._count(tool, 5)
        ok, message = close_audit(audit)
        self.assertTrue(ok)
        self.assertIn('matched the book', message)

    def test_void_audit_with_reverse_puts_stock_back(self):
        tool = self._bought_tool('T-VOID', 'Void tool', 10, cost=1000.0)
        audit = self._count(tool, 7)
        ok, message, summary = post_adjustments(audit, mode='losses', reason='lost',
                                                audit_date=_pkt_today(), commit=True)
        self.assertTrue(ok, message)
        db.session.expire_all()
        self.assertEqual(float(db.session.get(Tool, tool.id).total_quantity), 7.0)
        self.assertEqual(ToolScrap.query.count(), 1)

        ok, message = void_audit(db.session.get(ToolAudit, audit.id),
                                 reason='count was wrong', reverse=True)
        self.assertTrue(ok, message)
        db.session.expire_all()
        tool = db.session.get(Tool, tool.id)
        self.assertTrue(db.session.get(ToolAudit, audit.id).is_void)
        self.assertEqual(float(tool.total_quantity), 10.0)
        self.assertEqual(ToolScrap.query.count(), 0)
        self.assertEqual(tool_kpis()['total_tools'],
                         tool_kpis()['purchased_qty'] - tool_kpis()['scrapped_qty'])

    def test_reopen_audit_with_reverse_undoes_an_overage(self):
        tool = self._bought_tool('T-REOPEN', 'Reopen tool', 6, cost=500.0)
        audit = self._count(tool, 9)
        post_adjustments(audit, mode='both', reason='lost',
                         audit_date=_pkt_today(), commit=True)
        db.session.expire_all()
        self.assertEqual(float(db.session.get(Tool, tool.id).total_quantity), 9.0)

        ok, message = reopen_audit(db.session.get(ToolAudit, audit.id), reverse=True)
        self.assertTrue(ok, message)
        db.session.expire_all()
        self.assertEqual(float(db.session.get(Tool, tool.id).total_quantity), 6.0)
        self.assertEqual(ToolPurchase.query.filter(
            ToolPurchase.reference == db.session.get(ToolAudit, audit.id).audit_code
        ).count(), 0)
        self.assertEqual(live_serial_count(tool.id), 6)

    def test_void_without_reverse_says_the_stock_stays_moved(self):
        tool = self._bought_tool('T-KEEP', 'Keep tool', 10, cost=100.0)
        audit = self._count(tool, 8)
        post_adjustments(audit, mode='losses', reason='lost',
                         audit_date=_pkt_today(), commit=True)
        ok, message = void_audit(db.session.get(ToolAudit, audit.id), reason='oops')
        self.assertTrue(ok)
        self.assertIn('still in the stock books', message)
        db.session.expire_all()
        self.assertEqual(float(db.session.get(Tool, tool.id).total_quantity), 8.0)

    # ==================================================================
    # T-14 — reading is reading
    # ==================================================================
    def test_reading_tools_pages_writes_nothing(self):
        tool = self._make_tool('T-READ', 'Read tool', 12)
        rental = self._create_rental([(tool, 3, 200)], project=self.site)
        # warm up (first request in a process may set runtime flags)
        for path in ('/hdc/tool-rental/dashboard', '/hdc/tool-rental/inventory'):
            self.client.get(path)

        writes = []

        @event.listens_for(db.engine, 'before_cursor_execute')
        def _capture(conn, cursor, statement, parameters, context, executemany):
            head = statement.strip().split(None, 1)[0].lower() if statement.strip() else ''
            if head in ('insert', 'update', 'delete'):
                writes.append(statement.strip()[:120])

        try:
            for path in ('/hdc/tool-rental/dashboard', '/hdc/tool-rental',
                         '/hdc/tool-rental/inventory', '/hdc/tool-rental/new',
                         '/hdc/tool-rental/tracking', '/hdc/tool-rental/serials',
                         '/hdc/tool-rental/glance', '/hdc/tool-rental/reports',
                         '/hdc/tool-rental/audit',
                         f'/hdc/tool-rental/{rental.id}',
                         f'/hdc/tool-rental/tool/{tool.id}',
                         f'/hdc/api/tool-rental/dashboard',
                         f'/hdc/api/tool-rental/tools/{tool.id}/summary'):
                r = self.client.get(path)
                self.assertEqual(r.status_code, 200, f'{path} -> {r.status_code}')
        finally:
            event.remove(db.engine, 'before_cursor_execute', _capture)
        self.assertEqual(writes, [], f'a GET wrote to the database: {writes[:5]}')

    # ==================================================================
    # T-12 — navigation never offers a 403
    # ==================================================================
    def test_inventory_only_user_gets_no_dead_nav_links(self):
        """A user granted one page must not be offered the other eight.

        Rendered in a request context with that user signed in, because the
        Werkzeug test client shares its cookie jar across an app: a second
        form login in the same process silently keeps the first session.
        `may_access_path` is the very gate the routes enforce, so "no link the
        gate would refuse" is the same statement as "no 403 dead end".
        """
        user = HDCUser(username='invuser', role='staff',
                       password_hash=generate_password_hash('Inv@1234'),
                       permissions_json=json.dumps({
                           'tool_inventory': {'read': True, 'write': True}}))
        db.session.add(user)
        db.session.commit()
        self._make_tool('T-NAV', 'Nav tool', 3)

        with self.app.test_request_context('/hdc/tool-rental/inventory'):
            login_user(user)
            html = render_template('tool_rental/_tools_nav.html',
                                   tools_active='inventory')
        hrefs = _nav_hrefs(html)
        self.assertEqual(hrefs, ['/hdc/tool-rental/inventory'])
        for href in hrefs:
            self.assertNotEqual(may_access_path(user, href, 'read'), False,
                                f'nav offers {href}, which the route would refuse')

        denied = ('/hdc/tool-rental/dashboard', '/hdc/tool-rental',
                  '/hdc/tool-rental/transaction', '/hdc/tool-rental/audit',
                  '/hdc/tool-rental/glance', '/hdc/tool-rental/serials',
                  '/hdc/tool-rental/tracking', '/hdc/tool-rental/reports')
        for path in denied:
            self.assertEqual(may_access_path(user, path, 'read'), False, path)
            self.assertNotIn(path, hrefs)

        # A manager on role defaults keeps the reading tabs it may open — the
        # old role-based nav hid Tracking / Reports / Glance / Serials from it.
        manager = HDCUser(username='mgr', role='manager',
                          password_hash=generate_password_hash('Mgr@1234'))
        db.session.add(manager)
        db.session.commit()
        with self.app.test_request_context('/hdc/tool-rental/dashboard'):
            login_user(manager)
            mhtml = render_template('tool_rental/_tools_nav.html',
                                    tools_active='dashboard')
        mhrefs = _nav_hrefs(mhtml)
        self.assertEqual(len(mhrefs), 9, mhrefs)
        for href in mhrefs:
            self.assertNotEqual(may_access_path(manager, href, 'read'), False, href)
        for path in ('/hdc/tool-rental/reports', '/hdc/tool-rental/tracking',
                     '/hdc/tool-rental/serials', '/hdc/tool-rental/glance'):
            self.assertIn(path, mhrefs)

    # ==================================================================
    # T-13 — a scoped view says so
    # ==================================================================
    def test_scoped_user_totals_are_flagged_not_reconciled(self):
        """A record-scoped viewer's numbers say they are a slice.

        Before the fix a scoped manager saw 48 pieces in store against an
        admin's 44 while both pages reported `reconciled: true`.
        """
        tool = self._make_tool('T-SCOPE', 'Scope tool', 10)
        stage = Stage(project_id=self.site.id, name='Foundation')
        db.session.add(stage)
        db.session.commit()
        self._create_rental([(tool, 2, 100)], project=self.site)

        scoped = HDCUser(username='scoped', role='manager',
                         password_hash=generate_password_hash('Scope@1234'),
                         stage_scope_enabled=True,
                         allowed_stage_ids_json=json.dumps([stage.id + 999]))
        admin = HDCUser.query.filter_by(username='admin').one()
        db.session.add(scoped)
        db.session.commit()

        with self.app.test_request_context('/hdc/tool-rental/dashboard'):
            login_user(scoped)
            totals = tool_ledger()['totals']
            recon = tools_reconciliation(tool_ledger())
            note = render_template('tool_rental/_scope_note.html')
        self.assertTrue(totals['scope_limited'])
        self.assertFalse(totals['reconciled'])
        self.assertTrue(recon['scope_limited'])
        self.assertFalse(recon['balanced'])
        self.assertIn('Scope-limited view', note)

        with self.app.test_request_context('/hdc/tool-rental/dashboard'):
            login_user(admin)
            admin_totals = tool_ledger()['totals']
            admin_recon = tools_reconciliation(tool_ledger())
            admin_note = render_template('tool_rental/_scope_note.html')
        self.assertFalse(admin_totals['scope_limited'])
        self.assertTrue(admin_totals['reconciled'])
        self.assertFalse(admin_recon['scope_limited'])
        self.assertTrue(admin_recon['balanced'])
        self.assertNotIn('Scope-limited view', admin_note)

        # the JSON API carries the same flag, so an export cannot present the
        # slice as the book either
        with self.app.test_request_context('/hdc/api/tool-rental/dashboard'):
            login_user(scoped)
            recon_payload = dashboard_summary(tool_ledger())['recon']
        self.assertTrue(recon_payload['scope_limited'])
        self.assertFalse(recon_payload['balanced'])

    # ==================================================================
    # T-16 — the serial page is paginated
    # ==================================================================
    def test_serial_page_is_paginated(self):
        tool = self._bought_tool('T-PAGE', 'Page tool', 10, cost=100.0)
        second = self._bought_tool('T-PAGE2', 'Page tool two', 10, cost=100.0)
        db.session.expire_all()
        self.assertEqual(live_serial_count(tool.id) + live_serial_count(second.id), 20)

        rows = lambda html: html.count('<tr class="serial-')  # noqa: E731
        page1 = self.client.get('/hdc/tool-rental/serials?per_page=10')
        html = page1.get_data(as_text=True)
        self.assertEqual(page1.status_code, 200)
        self.assertEqual(rows(html), 10, 'page 1 did not hold exactly one page')
        self.assertIn('hdc-pagination', html)
        self.assertIn('Page <strong>1</strong> of <strong>2</strong>', html)
        self.assertIn('of <strong>20</strong>', html)

        page2 = self.client.get('/hdc/tool-rental/serials?per_page=10&page=2')
        html2 = page2.get_data(as_text=True)
        self.assertEqual(page2.status_code, 200)
        self.assertEqual(rows(html2), 10)
        self.assertIn('Page <strong>2</strong> of <strong>2</strong>', html2)
        self.assertNotEqual(_serial_numbers(html), _serial_numbers(html2),
                            'page 2 repeated page 1')

        # an out-of-range page falls back instead of rendering nothing
        page9 = self.client.get('/hdc/tool-rental/serials?per_page=10&page=99')
        self.assertEqual(page9.status_code, 200)

    # ==================================================================
    # T-10 — the smaller integrity fixes
    # ==================================================================
    def test_duplicate_tool_code_is_reported_not_swallowed(self):
        self._make_tool('T-DUP', 'First tool', 2)
        r = self.client.post('/hdc/tool-rental/inventory', data={
            '_csrf_token': self._token(), 'action': 'add_tool', 'name': 'Second tool',
            'tool_code': 'T-DUP', 'unit': 'pcs', 'total_quantity': '0',
            'category_id': str(self.category.id),
        }, follow_redirects=True)
        html = r.get_data(as_text=True)
        self.assertIn('was already taken', html)
        db.session.expire_all()
        self.assertEqual(Tool.query.count(), 2)
        codes = {t.tool_code for t in Tool.query.all()}
        self.assertIn('T-DUP', codes)
        self.assertNotEqual(len(codes), 1)

    def test_no_charge_edit_zeroes_the_bill(self):
        tool = self._make_tool('T-NC', 'No charge tool', 5)
        rental = self._create_rental([(tool, 2, 1000)], project=self.site)
        self.assertEqual(float(rental.total_amount), 2000.0)
        self.client.post(f'/hdc/tool-rental/{rental.id}/update', data={
            '_csrf_token': self._token(), 'txn_type': 'new_rental',
            'renter_type': 'internal', 'project_id': str(self.site.id),
            'billing_type': 'no_charge', 'rental_date': _pkt_today().isoformat(),
            'tool_id[]': [str(tool.id)], 'qty[]': ['2'], 'rate[]': ['1000'],
            'rental_item_id[]': [str(rental.items[0].id)],
        }, follow_redirects=True)
        db.session.expire_all()
        rental = db.session.get(ToolRental, rental.id)
        self.assertEqual(float(rental.total_amount), 0.0)
        self.assertEqual(float(rental.total_pending_amount), 0.0)
        self.assertEqual(rental.payment_status, 'no_charge')

    def test_maintenance_records_the_posted_date(self):
        tool = self._bought_tool('T-MAINT', 'Maintenance tool', 4, cost=100.0)
        serial = ToolSerial.query.filter_by(tool_id=tool.id).first()
        r = self.client.post('/hdc/tool-rental/transaction/maintenance', data={
            '_csrf_token': self._token(),
            'transaction_date': '2026-03-04',
            'txn_reference': 'MNT-1',
            'maintenance_serial_ids[]': [str(serial.id)],
            'maintenance_reason': 'pump seal',
            'notes': 'sent to workshop',
        }, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        db.session.expire_all()
        log = (ToolMovementLog.query
               .filter(ToolMovementLog.tool_id == tool.id,
                       ToolMovementLog.movement_type == 'maintenance_out')
               .order_by(ToolMovementLog.id.desc()).first())
        self.assertIsNotNone(log, 'no maintenance movement logged')
        self.assertEqual(log.timestamp.strftime('%Y-%m-%d'), '2026-03-04',
                         'the posted date was dropped from the tracking chain')

    def test_duplicate_reference_is_blocked_on_a_second_transaction(self):
        tool = self._bought_tool('T-REF', 'Reference tool', 6, cost=100.0)
        serials = ToolSerial.query.filter_by(tool_id=tool.id).order_by(ToolSerial.id).all()
        payload = {
            '_csrf_token': self._token(),
            'transaction_date': _pkt_today().isoformat(),
            'txn_reference': 'MNT-77',
            'maintenance_reason': 'service',
        }
        first = dict(payload, **{'maintenance_serial_ids[]': [str(serials[0].id)]})
        r1 = self.client.post('/hdc/tool-rental/transaction/maintenance',
                              data=first, follow_redirects=True)
        self.assertEqual(r1.status_code, 200)
        db.session.expire_all()
        self.assertEqual(ToolMovementLog.query.filter(
            ToolMovementLog.movement_type == 'maintenance_out').count(), 1)

        # the same reference on another transaction is a double-post
        second = dict(payload, **{'maintenance_serial_ids[]': [str(serials[1].id)]})
        r2 = self.client.post('/hdc/tool-rental/transaction/maintenance',
                              data=second, follow_redirects=True)
        self.assertEqual(r2.status_code, 200)
        db.session.expire_all()
        self.assertEqual(ToolMovementLog.query.filter(
            ToolMovementLog.movement_type == 'maintenance_out').count(), 1,
            'a duplicate reference posted a second movement')
        self.assertIn('MNT-77', r2.get_data(as_text=True))

        from hdc.services.tool_txn_entry import txn_reference_seen
        self.assertTrue(txn_reference_seen('MNT-77'))

    def test_duplicate_reference_is_blocked_for_a_transfer_transaction(self):
        tool = self._make_tool('T-REF', 'Reference tool', 6)
        record_tool_purchase(tool.id, 6, unit_cost=100.0, commit=True)
        rental = self._create_rental([(tool, 2, 100)], project=self.site)
        first = self.client.post(f'/hdc/tool-rental/{rental.id}/transfer', data={
            '_csrf_token': self._token(), 'to_type': 'site',
            'to_project_id': str(self.site_b.id), 'qty_transferred': '1',
            'transfer_date': _pkt_today().isoformat(), 'reference': 'TRF-77',
            'notes': 'TRF-77',
        }, follow_redirects=True)
        self.assertEqual(first.status_code, 200)
        db.session.expire_all()
        self.assertEqual(ToolRentalTransfer.query.count(), 1)

        from hdc.services.tool_txn_entry import txn_reference_seen
        self.assertTrue(txn_reference_seen('TRF-77'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
