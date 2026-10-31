#!/usr/bin/env python3
"""Regression tests: tools must stay where the book says they are.

Covers the three ways tools were seen to "disappear" back into the store:

  - a return did not say which site / customer the pieces came from, so the
    ledger took them from whichever holding was added last (Site B's pieces
    went to the store when Site A's were returned);
  - a transfer back to the warehouse was counted as "out", so the store figure
    (owned - out) lost those pieces and the rental still showed them as out;
  - a store count of pieces that really were back in the store produced a false
    "extra" and Adjust then *created* stock.

Plus the audit side: a sheet whose lines all match the book files itself as
verified, and a shortage that really came back to store is posted as a return
(not a write-off) and can be reversed cleanly.

Run with:
    HDC_BOOTSTRAP_ADMIN_PASSWORD='Admin@1234' python -m unittest tests.test_tool_location_integrity -v
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
    Tool, ToolAudit, ToolRental, ToolRentalItem, ToolRentalReturn, ToolRentalReturnItem,
    ToolRentalTransfer,
)
from hdc.services import tool_audit as audit                            # noqa: E402
from hdc.services.tool_tracking import tool_ledger                      # noqa: E402
from hdc.utils.dates import _pkt_today                                   # noqa: E402

ADMIN_PASSWORD = os.environ['HDC_BOOTSTRAP_ADMIN_PASSWORD']
EPS = 0.001


class LocationIntegrityTestCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='hdc-location-test-')
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

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ fixtures
    def _login(self):
        r = self.client.get('/hdc/login')
        m = re.search(r'name="_csrf_token" value="([^"]+)"', r.get_data(as_text=True) or '')
        self.client.post('/hdc/login', data={
            'username': 'admin', 'password': ADMIN_PASSWORD,
            '_csrf_token': m.group(1) if m else '',
        }, follow_redirects=True)

    def _post(self, path, data):
        with self.client.session_transaction() as sess:
            data = dict(data, _csrf_token=sess.get('_csrf_token', ''))
        return self.client.post(path, data=data)

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

    def _rent(self, project, qty, tool=None, customer_name=None):
        tool = tool or self.grinder
        rental = ToolRental(
            rental_code=f'RENT-{ToolRental.query.count() + 1:05d}',
            renter_type='internal' if project else 'external',
            project_id=project.id if project else None,
            customer_name=customer_name, rental_date=_pkt_today(),
            billing_type='per_day', status='active', payment_status='unpaid',
            is_void=False)
        db.session.add(rental)
        db.session.flush()
        item = ToolRentalItem(rental_id=rental.id, tool_id=tool.id, qty_rented=qty,
                              qty_returned=0.0, qty_pending=qty,
                              rate=float(tool.rental_rate_per_day or 0),
                              amount=qty * float(tool.rental_rate_per_day or 0))
        db.session.add(item)
        rental.total_rented_qty = qty
        rental.total_amount = qty * float(tool.rental_rate_per_day or 0)
        db.session.commit()
        return rental, item

    def _row(self, tool=None):
        led = tool_ledger()
        return next(r for r in led['tools'] if r['tool_id'] == (tool or self.grinder).id)

    def _balance(self):
        totals = tool_ledger()['totals']
        return abs(totals['owned_qty'] - (totals['in_store_qty'] + totals['out_qty'])) <= EPS

    def _holding(self, row, label):
        return sum(h['qty'] for h in row['holdings'] if h['label'] == label)

    def _transfer(self, rental, **form):
        data = {'transfer_date': _pkt_today().isoformat(), **form}
        return self._post(f'/hdc/tool-rental/{rental.id}/transfer', data)

    def _return(self, rental, item, qty, from_key=None):
        data = {'return_condition': 'partial', 'payment_condition': 'no_payment',
                'return_date': _pkt_today().isoformat(),
                'rental_item_id[]': [str(item.id)], 'qty_returned[]': [str(qty)],
                'from_location_key[]': [from_key or '']}
        return self._post(f'/hdc/tool-rental/{rental.id}/return', data)

    # ------------------------------------------------ 1. returns know the place
    def test_return_takes_pieces_from_the_site_they_came_from(self):
        rental, item = self._rent(self.site_a, 10)
        self._transfer(rental, to_type='site', to_project_id=self.site_b.id, qty_transferred='4')
        row = self._row()
        self.assertEqual(self._holding(row, 'Site A'), 6.0)
        self.assertEqual(self._holding(row, 'Site B'), 4.0)

        # the form asks which site the pieces came from; say Site A
        key_a = f'site:{self.site_a.id}'
        self._return(rental, item, 4, from_key=key_a)

        row = self._row()
        self.assertEqual(self._holding(row, 'Site A'), 2.0)      # A gave 4 back
        self.assertEqual(self._holding(row, 'Site B'), 4.0)      # B keeps its 4
        self.assertEqual(row['in_store_qty'], 4.0)
        self.assertTrue(self._balance())

    def test_return_from_a_split_line_needs_a_place_and_refuses_too_many(self):
        rental, item = self._rent(self.site_a, 10)
        self._transfer(rental, to_type='site', to_project_id=self.site_b.id, qty_transferred='4')
        before = (self._row()['in_store_qty'], self._holding(self._row(), 'Site B'))

        self._return(rental, item, 4)                              # no place given
        self.assertEqual((self._row()['in_store_qty'],
                          self._holding(self._row(), 'Site B')), before)

        self._return(rental, item, 5, from_key=f'site:{self.site_b.id}')   # B holds 4
        self.assertEqual((self._row()['in_store_qty'],
                          self._holding(self._row(), 'Site B')), before)
        self.assertEqual(ToolRentalReturnItem.query.count(), 0)

    # ------------------------------------------- 2. a warehouse move is a return
    def test_transfer_to_warehouse_puts_pieces_in_store_and_closes_them_on_the_rental(self):
        rental, item = self._rent(self.site_a, 10)
        self._transfer(rental, to_type='warehouse', qty_transferred='3')

        row = self._row()
        self.assertEqual(row['in_store_qty'], 3.0)               # visible in store now
        self.assertEqual(self._holding(row, 'Site A'), 7.0)
        self.assertEqual(row['out_qty'], 7.0)
        self.assertTrue(self._balance())
        db.session.expire_all()
        item = db.session.get(ToolRentalItem, item.id)
        self.assertEqual(item.qty_pending, 7.0)                  # rental stops holding 3
        self.assertEqual(rental.status, 'partially_returned')
        self.assertTrue(ToolRentalTransfer.query.filter_by(rental_id=rental.id)
                        .first().closes_rental)

    def test_store_count_after_warehouse_move_has_no_false_extra_and_no_phantom_stock(self):
        rental, _ = self._rent(self.site_a, 10)
        self._transfer(rental, to_type='warehouse', qty_transferred='3')

        _, record = audit.start_audit(audit.parse_location_key('store'),
                                      counter_name='Storekeeper', user_id=1,
                                      counts={self.grinder.id: {'counted': 3}},
                                      ledger=tool_ledger())
        db.session.expire_all()
        self.assertEqual(record.discrepancy_lines, 0)            # the count is right
        self.assertEqual(record.status, 'adjusted')              # so it files itself

        owned_before = db.session.get(Tool, self.grinder.id).total_quantity
        ok, message, summary = audit.post_adjustments(record, mode='both', user_id=1)
        self.assertEqual(summary.get('gains', 0), 0)
        db.session.expire_all()
        self.assertEqual(db.session.get(Tool, self.grinder.id).total_quantity, owned_before)

    # --------------------------------------------- 3. audit "came back to store"
    def test_site_shortage_that_came_back_is_a_return_and_reverses_cleanly(self):
        rental, _ = self._rent(self.site_a, 6)
        _, record = audit.start_audit(audit.parse_location_key(f'site:{self.site_a.id}'),
                                      counter_name='Site', user_id=1,
                                      counts={self.grinder.id: {'counted': 4}},
                                      ledger=tool_ledger())
        self.assertEqual(record.discrepancy_lines, 1)            # 2 missing at the site
        owned_before = db.session.get(Tool, self.grinder.id).total_quantity

        ok, message, summary = audit.post_adjustments(
            record, mode='losses', reason=audit.AUDIT_RETURNED_REASON, user_id=1)
        self.assertTrue(ok, message)
        self.assertEqual(summary['returned_qty'], 2.0)
        db.session.expire_all()
        row = self._row()
        self.assertEqual(db.session.get(Tool, self.grinder.id).total_quantity, owned_before)
        self.assertEqual(row['in_store_qty'], 6.0)               # the 2 are in store
        self.assertEqual(self._holding(row, 'Site A'), 4.0)
        self.assertEqual(db.session.get(ToolRentalItem, rental.items[0].id).qty_pending, 4.0)
        self.assertTrue(ToolRentalReturnItem.query.filter_by(
            from_location_key=f'site:{self.site_a.id}').count() == 1)
        self.assertTrue(self._balance())

        # cancelling the sheet puts the pieces back out on the rental
        ok, message = audit.void_audit(record, reason='wrong count', reverse=True, user_id=1)
        self.assertTrue(ok, message)
        db.session.expire_all()
        row = self._row()
        self.assertEqual(self._holding(row, 'Site A'), 6.0)
        self.assertEqual(row['in_store_qty'], 4.0)
        self.assertTrue(self._balance())

    def test_came_back_is_refused_for_a_store_shortage(self):
        # a store shortage has no rental line to give pieces back: it must be a
        # loss, never a "came back" that changes nothing and says it did
        self._rent(self.site_a, 4)
        _, record = audit.start_audit(audit.parse_location_key('store'),
                                      counter_name='Storekeeper', user_id=1,
                                      counts={self.grinder.id: {'counted': 5}},
                                      ledger=tool_ledger())
        ok, message, summary = audit.post_adjustments(
            record, mode='losses', reason=audit.AUDIT_RETURNED_REASON, user_id=1)
        self.assertFalse(ok)
        self.assertIn('store shortage', message)
        self.assertEqual(ToolRentalReturnItem.query.count(), 0)
        self.assertTrue(self._balance())

    # ------------------------------------------------ 4. exact match is verified
    def test_exact_match_sheet_is_verified_and_leaves_the_waiting_list(self):
        self._rent(self.site_a, 6)
        _, record = audit.start_audit(audit.parse_location_key(f'site:{self.site_a.id}'),
                                      counter_name='Site', user_id=1,
                                      counts={self.grinder.id: {'counted': 6}},
                                      ledger=tool_ledger())
        db.session.expire_all()
        self.assertEqual(record.status, 'adjusted')
        self.assertFalse(record.is_open)
        self.assertEqual(audit.open_audit_count(), 0)

    # ------------------------------------------- 5. report-only check of old data
    def test_drift_report_flags_old_warehouse_moves_and_changes_nothing(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            'report_tool_location_drift',
            os.path.join(os.path.dirname(__file__), '..', 'scripts',
                         'report_tool_location_drift.py'))
        report = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(report)

        rental, item = self._rent(self.site_a, 10)
        self._transfer(rental, to_type='warehouse', qty_transferred='3')
        # make it look like data written before the fix: the move did not close
        # the rental, so the rental still shows all 10 as pending
        ToolRentalTransfer.query.filter_by(rental_id=rental.id).update({'closes_rental': False})
        db.session.get(ToolRentalItem, item.id).qty_pending = 10.0
        db.session.commit()
        before = (self._row()['in_store_qty'], self._holding(self._row(), 'Site A'))

        rows = list(report._rental_rows())
        self.assertEqual([r['rental_code'] for r in rows], [rental.rental_code])
        self.assertIn('warehouse move', rows[0]['why_check'])
        # nothing was changed by looking
        self.assertEqual((self._row()['in_store_qty'], self._holding(self._row(), 'Site A')), before)
        db.session.expire_all()
        self.assertEqual(db.session.get(ToolRentalItem, item.id).qty_pending, 10.0)


if __name__ == '__main__':
    unittest.main()
