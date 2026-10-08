#!/usr/bin/env python3
"""Tests for the HDC Tools **Audit** tab — physical count vs book position.

Covers the whole promise of the tab:

  - the sheet's "book says" column is the tracker's own position (never a
    second, independently-computed truth);
  - every place that can hold tools can be counted, including a live site the
    book says is empty, and a tool found where it should not be;
  - typed numbers become variances — a blank box ("not counted") is *not* a
    typed zero ("nothing here");
  - the Adjust button turns a shortage into a real stock event: the piece is
    written off the owned quantity **and** taken off the rental line that was
    holding it, so ``owned == store + sites + customers`` survives the fix;
  - an overage is added back to owned stock and parked where it was found;
  - a closed sheet cannot be typed into or adjusted twice, a cancelled sheet
    disappears, and a wipe clears the audit registers;
  - the pages render, the tab shows for the right roles, the JSON feed agrees
    with the service, and a non-finance user cannot post an adjustment.

Run with:
    HDC_BOOTSTRAP_ADMIN_PASSWORD='Admin@1234' python -m unittest tests.test_tool_audit -v
"""

from __future__ import annotations

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

from hdc.app import create_app                                          # noqa: E402
from hdc.extensions import db                                           # noqa: E402
from hdc.models.projects import Project                                 # noqa: E402
from hdc.models.tool_rental import (                                    # noqa: E402
    Tool, ToolAudit, ToolAuditLine, ToolMovementLog, ToolRental, ToolRentalItem, ToolScrap,
)
from hdc.services import tool_audit as audit                            # noqa: E402
from hdc.services.tool_tracking import tool_ledger                      # noqa: E402
from hdc.utils.dates import _pkt_today                                   # noqa: E402

ADMIN_PASSWORD = os.environ['HDC_BOOTSTRAP_ADMIN_PASSWORD']
EPS = 0.001


class ToolAuditTestCase(unittest.TestCase):
    """One isolated app + database per test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='hdc-tool-audit-test-')
        self.app = create_app({'HDC_DB_PATH': os.path.join(self.tmp, 'test.db'),
                               'HDC_INSTANCE_DIR': self.tmp})
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()
        self._login()

        self.site_a = self._project('Site A', 'P-A')
        self.site_b = self._project('Site B', 'P-B')
        self.grinder = self._tool('TOOL-0001', 'Angle Grinder', 10, cost=15_000)
        self.prop = self._tool('TOOL-0002', 'Shuttering Prop', 20, cost=2_500)
        # 6 grinders + 12 props at Site A (own site), 4 props with a customer.
        self.rental_a = self._rent(self.site_a, [(self.grinder, 6), (self.prop, 12)])
        self.rental_b = self._rent(None, [(self.prop, 4)], customer_name='Ali Traders')
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ fixtures
    def _login(self, username='admin', password=None):
        r = self.client.get('/hdc/login')
        m = re.search(r'name="_csrf_token" value="([^"]+)"', r.get_data(as_text=True) or '')
        self.client.post('/hdc/login', data={
            'username': username, 'password': password or ADMIN_PASSWORD,
            '_csrf_token': m.group(1) if m else '',
        }, follow_redirects=True)

    def _token(self):
        with self.client.session_transaction() as sess:
            return sess.get('_csrf_token', '')

    def _project(self, name, code):
        project = Project(name=name, project_code=code, client=f'Owner {code}',
                          location='Karachi', contract_type='lump_sum',
                          owner_lump_sum=1_000_000, status='Active')
        db.session.add(project)
        db.session.commit()
        return project

    def _tool(self, code, name, qty, cost=1000.0, rate=100.0):
        tool = Tool(tool_code=code, name=name, unit='pcs', total_quantity=qty,
                    purchase_cost=cost, rental_rate_per_day=rate, condition='good',
                    status='active', is_void=False)
        db.session.add(tool)
        db.session.commit()
        return tool

    def _rent(self, project, pairs, customer_name=None):
        rental = ToolRental(
            rental_code=f'RENT-{ToolRental.query.count() + 1:05d}',
            renter_type='internal' if project else 'external',
            project_id=project.id if project else None,
            customer_name=customer_name, rental_date=_pkt_today(),
            billing_type='per_day', status='active', payment_status='unpaid',
            is_void=False)
        db.session.add(rental)
        db.session.flush()
        for tool, qty in pairs:
            db.session.add(ToolRentalItem(
                rental_id=rental.id, tool_id=tool.id, qty_rented=qty,
                qty_returned=0.0, qty_pending=qty,
                rate=float(tool.rental_rate_per_day or 0),
                amount=qty * float(tool.rental_rate_per_day or 0)))
        rental.total_rented_qty = sum(q for _, q in pairs)
        rental.total_amount = sum(q * float(tool.rental_rate_per_day or 0) for tool, q in pairs)
        db.session.commit()
        return rental

    def _site_key(self, project):
        return f'site:{project.id}'

    def _start(self, project=None, key=None):
        spec = audit.parse_location_key(key or self._site_key(project))
        created, record = audit.start_audit(spec, counter_name='Site storekeeper',
                                           user_id=1, ledger=tool_ledger())
        return record, created

    def _balance(self):
        totals = tool_ledger()['totals']
        return (abs(totals['owned_qty']
                    - (totals['in_store_qty'] + totals['out_qty'])) <= EPS, totals)

    # --------------------------------------------------- 1. the book column
    def test_book_position_is_the_trackers_own_position(self):
        """The sheet never re-derives stock: it reads the ledger it shares."""
        ledger = tool_ledger()
        spec = audit.parse_location_key(self._site_key(self.site_a))
        book = audit.book_position(ledger, spec)
        self.assertEqual(book[self.grinder.id]['qty'], 6.0)
        self.assertEqual(book[self.prop.id]['qty'], 12.0)
        store = audit.book_position(ledger, audit.parse_location_key('store'))
        self.assertEqual(store[self.grinder.id]['qty'], 4.0)   # 10 owned - 6 out
        self.assertEqual(store[self.prop.id]['qty'], 4.0)      # 20 owned - 16 out

    def test_every_place_that_can_hold_tools_is_countable(self):
        cards = {card['key']: card for card in audit.audit_locations()}
        self.assertIn('store', cards)
        self.assertIn(self._site_key(self.site_a), cards)
        self.assertIn(self._site_key(self.site_b), cards)   # holds nothing, still countable
        self.assertIn('cust:Ali Traders', cards)
        self.assertEqual(cards[self._site_key(self.site_a)]['expected_qty'], 18.0)
        self.assertEqual(cards['store']['expected_qty'], 8.0)
        self.assertEqual(cards[self._site_key(self.site_b)]['expected_qty'], 0.0)
        self.assertEqual(cards['store']['label'], 'Warehouse / Store')

    def test_matrix_reports_an_uncounted_place_as_not_verified(self):
        matrix = audit.audit_matrix()
        self.assertEqual(matrix['totals']['locations_counted'], 0)
        for row in matrix['rows']:
            self.assertEqual(row['status'], 'not_counted')
            self.assertIsNone(row['places'][0]['counted_qty'])
        # nothing was counted, so nothing may be claimed as a match
        self.assertNotIn('match', [row['status'] for row in matrix['rows']])

    # --------------------------------------------------- 2. typing numbers
    def test_save_counts_stores_variances_and_keeps_blank_apart_from_zero(self):
        record, _ = self._start(self.site_a)
        ok, message, record = audit.save_counts(record, {
            self.grinder.id: {'counted': 4, 'damaged': 1, 'notes': 'two missing since Eid'},
            self.prop.id: {'counted': 0},          # "we looked, the shelf is empty"
        }, ledger=tool_ledger())
        self.assertTrue(ok, message)
        db.session.expire_all()

        lines = {line.tool_id: line for line in audit.lines_of(record)}
        self.assertEqual(lines[self.grinder.id].variance, -2.0)
        self.assertEqual(lines[self.grinder.id].status, 'short')
        self.assertEqual(lines[self.grinder.id].damaged_qty, 1.0)
        self.assertEqual(lines[self.prop.id].variance, -12.0)
        self.assertEqual(lines[self.prop.id].status, 'short')

        self.assertEqual(record.shortage_qty, 14.0)
        self.assertEqual(record.counted_lines, 2)
        self.assertEqual(record.discrepancy_lines, 2)
        self.assertEqual(record.status, 'counted')

    def test_a_tool_that_is_not_counted_stays_open_instead_of_being_zeroed(self):
        record, _ = self._start(self.site_a)
        self._tool('TOOL-0003', 'Plate Compactor', 3, cost=50_000)
        audit.save_counts(record, {self.grinder.id: {'counted': 6}}, ledger=tool_ledger())
        db.session.expire_all()
        lines = {line.tool_id: line for line in audit.lines_of(record)}
        self.assertEqual(lines[self.grinder.id].status, 'match')
        # the prop was expected but left blank -> still open, counted as nothing
        self.assertIsNone(lines[self.prop.id].counted_qty)
        self.assertEqual(lines[self.prop.id].status, 'pending')
        self.assertEqual(record.status, 'draft')
        self.assertEqual(record.counted_lines, 1)
        # the brand-new tool is not on this sheet until someone counts it
        self.assertNotIn('TOOL-0003', [line.tool_code for line in audit.lines_of(record)])
        # ...but it is offered in the "anything else found here?" half of the sheet
        sheet = audit.audit_sheet(audit=record)
        self.assertIn('Plate Compactor', [row['name'] for row in sheet['other_rows']])

    def test_extra_tool_found_at_a_site_is_recorded_as_an_overage(self):
        record, _ = self._start(self.site_a)
        # the prop is expected here; the grinder's neighbour is not
        audit.save_counts(record, {
            self.grinder.id: {'counted': 6},
            self.prop.id: {'counted': 15},        # three more than the book
        }, ledger=tool_ledger())
        db.session.expire_all()
        self.assertEqual(record.overage_qty, 3.0)
        self.assertEqual(record.shortage_qty, 0.0)
        line = audit.lines_of(record)[1]
        self.assertEqual(line.status, 'extra')
        self.assertEqual(audit.audit_summary()['locations_counted'], 1)

    # ------------------------------------------------------- 3. adjusting
    def test_adjust_losses_writes_off_and_frees_the_rental_line(self):
        record, _ = self._start(self.site_a)
        audit.save_counts(record, {
            self.grinder.id: {'counted': 4},
            self.prop.id: {'counted': 12},
        }, ledger=tool_ledger())
        db.session.expire_all()

        plan = audit.plan_adjustments(record, ledger=tool_ledger())
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]['kind'], 'loss')
        self.assertEqual(plan[0]['writable_qty'], 2.0)
        self.assertEqual(plan[0]['liftable_qty'], 2.0)      # the 2 lost pieces are 'out' here
        self.assertEqual(plan[0]['value'], 30_000.0)

        before = db.session.get(Tool, self.grinder.id).total_quantity
        ok, message, summary = audit.post_adjustments(record, mode='losses', reason='lost',
                                                      ledger=tool_ledger())
        self.assertTrue(ok, message)
        db.session.expire_all()

        self.assertEqual(summary['losses'], 1)
        self.assertEqual(summary['lost_qty'], 2.0)
        self.assertEqual(summary['write_off_value'], 30_000.0)
        # owned came down...
        self.assertEqual(db.session.get(Tool, self.grinder.id).total_quantity, before - 2.0)
        # ...the rental stopped carrying the lost pieces...
        item = (ToolRentalItem.query.filter_by(rental_id=self.rental_a.id,
                                                tool_id=self.grinder.id).first())
        self.assertEqual(item.qty_rented, 4.0)
        self.assertEqual(item.qty_pending, 4.0)
        # ...and the balance identity still holds
        balanced, totals = self._balance()
        self.assertTrue(balanced, totals)
        self.assertEqual(totals['owned_qty'], 28.0)
        self.assertEqual(totals['out_qty'], 20.0)

        # the write-off is a real scrap row, traceable to the audit
        scrap = ToolScrap.query.filter_by(tool_id=self.grinder.id).first()
        self.assertIsNotNone(scrap)
        self.assertEqual(scrap.reference, record.audit_code)
        self.assertEqual(scrap.reason, 'lost')
        self.assertEqual(scrap.value_written_off, 30_000.0)
        logs = ToolMovementLog.query.filter_by(tool_id=self.grinder.id).all()
        self.assertTrue(any(log.movement_type == 'adjustment' and record.audit_code in (log.notes or '')
                            for log in logs))
        # and the place now matches the count
        cards = {card['key']: card for card in audit.audit_locations()}
        self.assertEqual(cards[self._site_key(self.site_a)]['expected_qty'], 16.0)
        self.assertEqual(cards[self._site_key(self.site_a)]['counted_qty'], 16.0)
        self.assertEqual(cards[self._site_key(self.site_a)]['variance'], 0.0)

    def test_adjust_parks_an_overage_on_the_rental_that_holds_the_tool(self):
        record, _ = self._start(self.site_a)
        audit.save_counts(record, {
            self.grinder.id: {'counted': 9},     # 9 grinders physically at Site A
            self.prop.id: {'counted': 12},
        }, ledger=tool_ledger())
        db.session.expire_all()
        ok, message, summary = audit.post_adjustments(record, mode='both', ledger=tool_ledger())
        self.assertTrue(ok, message)
        db.session.expire_all()
        self.assertEqual(summary['gains'], 1)
        self.assertEqual(db.session.get(Tool, self.grinder.id).total_quantity, 13.0)
        item = (ToolRentalItem.query.filter_by(rental_id=self.rental_a.id,
                                               tool_id=self.grinder.id).first())
        self.assertEqual(item.qty_pending, 9.0)
        balanced, totals = self._balance()
        self.assertTrue(balanced, totals)
        cards = {card['key']: card for card in audit.audit_locations()}
        self.assertEqual(cards[self._site_key(self.site_a)]['expected_qty'], 21.0)

    def test_adjust_defaults_to_losses_only(self):
        record, _ = self._start(self.site_a)
        audit.save_counts(record, {self.grinder.id: {'counted': 5},
                                   self.prop.id: {'counted': 14}}, ledger=tool_ledger())
        db.session.expire_all()
        ok, message, summary = audit.post_adjustments(record, ledger=tool_ledger())
        self.assertTrue(ok, message)
        db.session.expire_all()
        self.assertEqual(summary['losses'], 1)
        self.assertEqual(summary['gains'], 0)
        # the overage stays open on the sheet instead of being invented into stock
        self.assertEqual(db.session.get(Tool, self.prop.id).total_quantity, 20.0)
        line = [line for line in audit.lines_of(record) if line.tool_id == self.prop.id][0]
        self.assertEqual(line.status, 'extra')

    def test_a_closed_sheet_cannot_be_adjusted_twice(self):
        record, _ = self._start(self.site_a)
        audit.save_counts(record, {self.grinder.id: {'counted': 4},
                                   self.prop.id: {'counted': 12}}, ledger=tool_ledger())
        db.session.expire_all()
        audit.post_adjustments(record, ledger=tool_ledger())
        db.session.expire_all()

        self.assertEqual(record.status, 'adjusted')
        ok, message, _ = audit.post_adjustments(record, ledger=tool_ledger())
        self.assertFalse(ok)
        self.assertIn('closed', message)
        ok, message, _ = audit.save_counts(record, {self.grinder.id: {'counted': 1}},
                                           ledger=tool_ledger())
        self.assertFalse(ok)
        self.assertEqual(ToolScrap.query.count(), 1)

        ok, message = audit.reopen_audit(record)
        self.assertTrue(ok, message)
        db.session.expire_all()
        self.assertEqual(record.status, 'counted')
        # the count now agrees with the corrected book, so nothing is pending
        self.assertEqual(audit.plan_adjustments(record, ledger=tool_ledger()), [])

    def test_a_store_shortage_needs_no_rental_change(self):
        record, _ = self._start(key='store')
        audit.save_counts(record, {self.grinder.id: {'counted': 1},
                                   self.prop.id: {'counted': 4}}, ledger=tool_ledger())
        db.session.expire_all()
        self.assertEqual(record.shortage_qty, 3.0)          # 4 grinders - 1 counted
        ok, message, summary = audit.post_adjustments(record, reason='damaged',
                                                      ledger=tool_ledger())
        self.assertTrue(ok, message)
        db.session.expire_all()
        self.assertEqual(summary['lost_qty'], 3.0)
        self.assertEqual(db.session.get(Tool, self.grinder.id).total_quantity, 7.0)
        self.assertEqual(self.prop.rented_out_qty, 16.0)     # rentals untouched
        balanced, totals = self._balance()
        self.assertTrue(balanced, totals)

    def test_close_verified_refuses_a_half_filled_sheet(self):
        record, _ = self._start(self.site_a)
        audit.save_counts(record, {self.grinder.id: {'counted': 6}}, ledger=tool_ledger())
        db.session.expire_all()
        ok, message = audit.close_audit(record)
        self.assertFalse(ok)
        self.assertIn('no counted number', message)
        audit.save_counts(record, {self.grinder.id: {'counted': 6},
                                   self.prop.id: {'counted': 12}}, ledger=tool_ledger())
        db.session.expire_all()
        ok, message = audit.close_audit(record)
        self.assertTrue(ok, message)
        db.session.expire_all()
        self.assertEqual(record.status, 'adjusted')
        self.assertEqual(record.write_off_value, 0.0)
        self.assertEqual(ToolScrap.query.count(), 0)

    def test_one_open_sheet_per_place_and_voiding_it(self):
        record, created = self._start(self.site_a)
        self.assertTrue(created)
        again, created_again = self._start(self.site_a)
        self.assertFalse(created_again)
        self.assertEqual(again.id, record.id)

        ok, message = audit.void_audit(record, reason='counted the wrong site')
        self.assertTrue(ok, message)
        db.session.expire_all()
        self.assertTrue(record.is_void)
        cards = {card['key']: card for card in audit.audit_locations()}
        self.assertIsNone(cards[self._site_key(self.site_a)]['audit'])

    def test_adjustment_is_capped_when_the_book_cannot_absorb_it(self):
        """Bad legacy data must not create a negative stock row."""
        self.grinder.total_quantity = 5.0          # less than the 6 out on rent
        db.session.commit()
        record, _ = self._start(self.site_a)
        audit.save_counts(record, {self.grinder.id: {'counted': 0},
                                   self.prop.id: {'counted': 12}}, ledger=tool_ledger())
        db.session.expire_all()
        plan = audit.plan_adjustments(record, ledger=tool_ledger())
        entry = [item for item in plan if item['name'] == 'Angle Grinder'][0]
        self.assertLessEqual(entry['writable_qty'], entry['qty'])
        ok, message, summary = audit.post_adjustments(record, ledger=tool_ledger())
        self.assertTrue(ok, message)
        db.session.expire_all()
        self.assertGreaterEqual(db.session.get(Tool, self.grinder.id).total_quantity, 0.0)


class ToolAuditRoutesTestCase(ToolAuditTestCase):
    """The same numbers through the browser: tab, sheet, buttons, permissions."""

    def test_audit_tab_and_pages_render(self):
        body = self.client.get('/hdc/tool-rental/dashboard').get_data(as_text=True)
        self.assertIn('/hdc/tool-rental/audit', body)          # the new tab
        overview = self.client.get('/hdc/tool-rental/audit')
        self.assertEqual(overview.status_code, 200)
        html = overview.get_data(as_text=True)
        self.assertIn('Physical check report', html)
        self.assertIn('Angle Grinder', html)
        self.assertIn('Warehouse / Store', html)
        self.assertIn('New physical count', html)

        record, _ = self._start(self.site_a)
        sheet = self.client.get(f'/hdc/tool-rental/audit/{record.id}')
        self.assertEqual(sheet.status_code, 200)
        html = sheet.get_data(as_text=True)
        self.assertIn('Enter the count by hand', html)
        self.assertIn(f'name="counted[{self.grinder.id}]"', html)
        self.assertIn(f'name="counted[{self.prop.id}]"', html)

        from urllib.parse import quote
        preview = self.client.get('/hdc/tool-rental/audit/count?location=' +
                                  quote(audit.location_key('customer', customer_name='Ali Traders')))
        self.assertEqual(preview.status_code, 200)
        self.assertIn('Ali Traders', preview.get_data(as_text=True))

    def test_count_and_adjust_through_the_ui(self):
        record, _ = self._start(self.site_a)
        saved = self.client.post(f'/hdc/tool-rental/audit/{record.id}/save', data={
            '_csrf_token': self._token(),
            'counter_name': 'Site A storekeeper',
            f'counted[{self.grinder.id}]': '4',
            f'damaged[{self.grinder.id}]': '2',
            f'notes[{self.grinder.id}]': 'missing since Eid',
            f'counted[{self.prop.id}]': '12',
        }, follow_redirects=True)
        self.assertEqual(saved.status_code, 200)
        db.session.expire_all()
        self.assertIn('discrepancie', saved.get_data(as_text=True))

        refused = self.client.post(f'/hdc/tool-rental/audit/{record.id}/adjust', data={
            '_csrf_token': self._token(), 'mode': 'losses', 'reason': 'lost',
        }, follow_redirects=True)
        self.assertIn('Type ADJUST', refused.get_data(as_text=True))
        self.assertEqual(ToolScrap.query.count(), 0)

        adjusted = self.client.post(f'/hdc/tool-rental/audit/{record.id}/adjust', data={
            '_csrf_token': self._token(), 'mode': 'losses', 'reason': 'lost',
            'confirm': 'ADJUST', 'adjust_notes': 'owner approved memo 12',
        }, follow_redirects=True)
        self.assertEqual(adjusted.status_code, 200)
        db.session.expire_all()
        self.assertEqual(ToolScrap.query.count(), 1)
        self.assertEqual(float(db.session.get(Tool, self.grinder.id).total_quantity), 8.0)
        html = adjusted.get_data(as_text=True)
        self.assertIn('Adjustments posted', html)
        self.assertIn('2 piece(s) written off', html)
        self.assertIn('30,000 PKR', html)

        filtered = self.client.get(f'/hdc/tool-rental/audit?location={self._site_key(self.site_a)}&only=variance')
        self.assertEqual(filtered.status_code, 200)

    def test_start_from_the_preview_creates_the_sheet_and_keeps_the_numbers(self):
        key = 'cust:Ali Traders'
        started = self.client.post('/hdc/tool-rental/audit/start', data={
            '_csrf_token': self._token(), 'location': key, 'counter_name': 'Ali',
            f'counted[{self.prop.id}]': '3',
        }, follow_redirects=True)
        self.assertEqual(started.status_code, 200)
        record = ToolAudit.query.order_by(ToolAudit.id.desc()).first()
        self.assertIsNotNone(record)
        self.assertEqual(record.customer_name, 'Ali Traders')
        self.assertEqual(record.location_label, 'Ali Traders')
        line = audit.lines_of(record)[0]
        self.assertEqual(line.variance, -1.0)
        self.assertEqual(line.status, 'short')
        # the second "new count" for the same place resumes, never duplicates
        self.client.get('/hdc/tool-rental/audit/count?location=' + key)
        self.assertEqual(ToolAudit.query.count(), 1)

    def test_json_feed_matches_the_service(self):
        record, _ = self._start(self.site_a)
        audit.save_counts(record, {self.grinder.id: {'counted': 4},
                                   self.prop.id: {'counted': 12}}, ledger=tool_ledger())
        db.session.expire_all()
        payload = self.client.get('/hdc/api/tool-rental/audit').get_json()
        service = audit.audit_rows_for_json()
        self.assertEqual(payload['totals']['counted_qty'], service['totals']['counted_qty'])
        self.assertEqual(payload['totals']['variance_qty'], -2.0)
        self.assertEqual({c['key'] for c in payload['locations']},
                         {c['key'] for c in service['locations']})
        store = [c for c in payload['locations'] if c['key'] == 'store'][0]
        self.assertEqual(store['expected_qty'], 8.0)

    def test_read_only_user_cannot_post_an_adjustment(self):
        from werkzeug.security import generate_password_hash

        from hdc.models.auth import HDCUser
        user = HDCUser(username='viewer', role='staff',
                      password_hash=generate_password_hash('Viewer@1234'))
        user.permissions_json = '{"tool_audit": {"read": true, "write": false}}'
        db.session.add(user)
        db.session.commit()

        record, _ = self._start(self.site_a)
        audit.save_counts(record, {self.grinder.id: {'counted': 0},
                                   self.prop.id: {'counted': 12}}, ledger=tool_ledger())
        db.session.expire_all()

        # the logout form is CSRF-protected too, so post the token with it
        self.client.post('/hdc/logout', data={'_csrf_token': self._token()},
                         follow_redirects=True)
        self._login('viewer', 'Viewer@1234')
        body = self.client.get('/hdc/tool-rental/audit').get_data(as_text=True)
        self.assertIn('Read only', body)
        self.assertNotIn('auditAdjustButton', body)
        blocked = self.client.post(f'/hdc/tool-rental/audit/{record.id}/adjust', data={
            '_csrf_token': self._token(), 'mode': 'losses', 'reason': 'lost',
            'confirm': 'ADJUST',
        })
        # the permission layer answers before the route is ever reached
        self.assertEqual(blocked.status_code, 403)
        self.assertEqual(ToolScrap.query.count(), 0)
        self.assertEqual(float(db.session.get(Tool, self.grinder.id).total_quantity), 10.0)

    def test_wipe_targets_and_boot_schema_cover_the_audit_tables(self):
        from sqlalchemy import text
        from hdc.core.admin import _WIPE_TARGETS
        tables = _WIPE_TARGETS['tools']['tables']
        self.assertIn('hdc_tool_audit_line', tables)
        self.assertLess(tables.index('hdc_tool_audit_line'), tables.index('hdc_tool_audit'))
        self.assertLess(tables.index('hdc_tool_audit'), tables.index('hdc_tool'))

        # that order is the only safe one: a sheet deleted before its lines
        # would trip the line's foreign key
        record, _ = self._start(self.site_a)
        audit.save_counts(record, {self.grinder.id: {'counted': 2},
                                   self.prop.id: {'counted': 12}},
                          ledger=tool_ledger())
        db.session.expire_all()
        self.assertEqual(ToolAuditLine.query.count(), 2)
        with db.engine.connect() as conn:
            for table in ('hdc_tool_audit_line', 'hdc_tool_audit'):
                conn.execute(text(f'DELETE FROM {table}'))
            conn.commit()
        db.session.expire_all()
        self.assertEqual(ToolAudit.query.count(), 0)
        self.assertEqual(ToolAuditLine.query.count(), 0)

        # boot must heal an old database: the DDL lives in _ensure_tool_rental_schema
        from sqlalchemy import inspect as sa_inspect, text
        with db.engine.connect() as conn:
            conn.execute(text('DROP TABLE IF EXISTS hdc_tool_audit_line'))
            conn.execute(text('DROP TABLE IF EXISTS hdc_tool_audit'))
            conn.commit()
        self.assertFalse(sa_inspect(db.engine).has_table('hdc_tool_audit'))
        from hdc.core.schema import _ensure_tool_rental_schema
        _ensure_tool_rental_schema()
        self.assertTrue(sa_inspect(db.engine).has_table('hdc_tool_audit'))
        self.assertTrue(sa_inspect(db.engine).has_table('hdc_tool_audit_line'))


class ToolAuditPermissionPageTestCase(unittest.TestCase):
    """The tab is a real page in the access map, not a URL anyone can reach."""

    def test_audit_paths_map_to_their_own_page(self):
        from hdc.services.permissions import PAGE_BY_ID, page_id_for_path
        self.assertEqual(page_id_for_path('/hdc/tool-rental/audit'), 'tool_audit')
        self.assertEqual(page_id_for_path('/hdc/tool-rental/audit/7/adjust'), 'tool_audit')
        self.assertEqual(page_id_for_path('/hdc/api/tool-rental/audit'), 'tool_audit')
        self.assertEqual(PAGE_BY_ID['tool_audit']['label'], 'Tool physical audit & adjustments')
        # the neighbours keep their own pages
        self.assertEqual(page_id_for_path('/hdc/tool-rental/inventory'), 'tool_inventory')
        self.assertEqual(page_id_for_path('/hdc/tool-rental/tracking'), 'tool_tracking')


if __name__ == '__main__':
    unittest.main(verbosity=2)
