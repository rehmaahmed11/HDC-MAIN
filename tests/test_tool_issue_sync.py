#!/usr/bin/env python3
"""Cross-section regressions for the exact pieces issued from the store.

Quantity balance alone is not enough: serial tracking, both transfer forms,
holder returns, maintenance and physical audit must agree about the SAME piece.
These tests use the real write endpoints, then inspect every read surface.
"""
from __future__ import annotations

import unittest

from hdc.extensions import db
from hdc.models.projects import Project
from hdc.models.tool_rental import (
    Tool, ToolMovementLog, ToolRental, ToolRentalItem, ToolRentalReturn,
    ToolRentalReturnItem, ToolRentalTransfer, ToolScrap, ToolSerial,
    ToolSerialMovement,
)
from hdc.services import tool_audit as audit
from hdc.services.tool_rental import (
    ensure_all_tools_serials, ensure_tool_serials, record_tool_purchase,
)
from hdc.services.tool_tracking import tool_ledger
from hdc.services.tool_txn_entry import (
    in_store_tool_rows, record_damage_loss, record_return, return_sheet,
    transfer_source_rows,
)
from tests.qa_support import IsolatedAppTest


class ToolIssueSyncTestCase(IsolatedAppTest):
    def setUp(self):
        super().setUp()
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.addCleanup(self.ctx.pop)
        self.site_a = self._project('Site A', 'SYNC-A')
        self.site_b = self._project('Site B', 'SYNC-B')
        self.tool = Tool(tool_code='SYNC-001', name='Grinder', unit='pcs',
                         total_quantity=0, purchase_cost=1000,
                         rental_rate_per_day=100, status='active')
        db.session.add(self.tool)
        db.session.commit()
        record_tool_purchase(self.tool.id, 8, unit_cost=1000,
                             is_opening_stock=True)
        self.tool_id = self.tool.id
        self.a_id, self.b_id = self.site_a.id, self.site_b.id

    def _project(self, name, code):
        row = Project(name=name, project_code=code, client='HDC',
                      location='Karachi', contract_type='lump_sum',
                      owner_lump_sum=1_000_000, status='Active')
        db.session.add(row)
        db.session.commit()
        return row

    def _pieces(self):
        return ToolSerial.query.filter_by(tool_id=self.tool_id).order_by(ToolSerial.id).all()

    def _issue(self, numbers, project=None, customer=None):
        serials = [self._pieces()[n - 1] for n in numbers]
        data = {
            'txn_type': 'issue_own_site' if project is not None else 'rent_out_customer',
            'renter_type': 'internal' if project is not None else 'external',
            'billing_type': 'no_charge',
            'project_id': str(project.id) if project is not None else '',
            'customer_name': customer or '',
            'tool_id[]': [str(self.tool_id)], 'qty[]': [str(len(numbers))],
            'rate[]': ['0'], 'row_serial_ids[]': [','.join(str(s.id) for s in serials)],
        }
        before = ToolRental.query.count()
        self.form('/hdc/tool-rental/create', **data)
        self.assertEqual(ToolRental.query.count(), before + 1)
        rental = ToolRental.query.order_by(ToolRental.id.desc()).first()
        return rental, rental.items[0], serials

    def _move(self, rental, serials, project=None):
        item = rental.items[0]
        data = {'transfer_date': '2026-10-31',
                'to_type': 'site' if project is not None else 'customer',
                'rental_item_id[]': [str(item.id)],
                'qty_transfer[]': [str(len(serials))],
                f'transfer_serial_ids_{item.id}[]': [str(s.id) for s in serials]}
        if project is not None:
            data['to_project_id'] = str(project.id)
        else:
            data['to_customer_name'] = 'Outside Customer'
        before = ToolRentalTransfer.query.count()
        self.form(f'/hdc/tool-rental/{rental.id}/transfer', **data)
        self.assertEqual(ToolRentalTransfer.query.count(), before + 1)

    def _row(self):
        return next(r for r in tool_ledger()['tools'] if r['tool_id'] == self.tool_id)

    def _assert_serial_balance(self):
        row = self._row()
        serials = [s for s in self._pieces() if not s.is_scrapped]
        self.assertEqual(sum(not s.is_in_store for s in serials), row['out_qty'])
        located = {}
        for holding in row['holdings']:
            key = (holding['rental_item'].id, holding['label'])
            located[key] = located.get(key, 0) + holding['qty']
        for (item_id, label), qty in located.items():
            pieces = [s for s in serials if not s.is_in_store
                      and s.current_rental_item_id == item_id
                      and s.current_location_label == label]
            self.assertEqual(len(pieces), qty, label)
        for serial in serials:
            if serial.is_in_store:
                self.assertIsNone(serial.current_rental_id)
                self.assertIsNone(serial.current_rental_item_id)
            else:
                self.assertIsNotNone(serial.current_rental_id)
                self.assertIsNotNone(serial.current_rental_item_id)

    def _count(self, key, counted):
        _, sheet = audit.start_audit(audit.parse_location_key(key), user_id=1,
                                     counts={self.tool_id: {'counted': counted}},
                                     ledger=tool_ledger())
        return sheet

    def test_split_transfer_keeps_each_serial_at_its_holding(self):
        rental, item, issued = self._issue([1, 2, 3, 4], self.site_a)
        self._move(rental, issued[2:], self.site_b)
        ensure_all_tools_serials(commit=True)
        self.assertEqual([s.current_location_label for s in issued],
                         ['Site A', 'Site A', 'Site B', 'Site B'])
        self._assert_serial_balance()
        sources = {s['holder']: s['lines'][0]['serials'] for s in transfer_source_rows()}
        self.assertEqual([s['serial_number'] for s in sources['Site A']],
                         ['Grinder No 1', 'Grinder No 2'])
        self.assertEqual([s['serial_number'] for s in sources['Site B']],
                         ['Grinder No 3', 'Grinder No 4'])
        rows = return_sheet()['rows']
        self.assertEqual({r['holder']: [s['serialNumber'] for s in r['serials']] for r in rows},
                         {'Site A': ['Grinder No 1', 'Grinder No 2'],
                          'Site B': ['Grinder No 3', 'Grinder No 4']})
        for path in ('/hdc/tool-rental/tracking', '/hdc/tool-rental/serials',
                     '/hdc/tool-rental/glance', '/hdc/tool-rental/inventory',
                     '/hdc/tool-rental/audit'):
            self.assertEqual(self.client.get(path).status_code, 200, path)

    def test_quantity_transfer_uses_pieces_at_the_recorded_source(self):
        rental, item, issued = self._issue([1, 2, 3, 4, 5, 6], self.site_a)
        self._move(rental, issued[:2], self.site_b)
        # The detail form defaults to the latest destination (B), not the
        # rental's original project (A). No explicit piece was ticked.
        self.form(f'/hdc/tool-rental/{rental.id}/transfer',
                  to_type='site', to_project_id=str(self.a_id), qty_transferred='1')
        self.assertEqual(issued[0].current_location_label, 'Site A')
        self.assertEqual(issued[1].current_location_label, 'Site B')
        self.assertEqual(issued[2].current_location_label, 'Site A')
        self._assert_serial_balance()

    def test_detail_transfer_can_choose_the_older_holder_after_a_split(self):
        rental, item, issued = self._issue([1, 2, 3, 4], self.site_a)
        self._move(rental, issued[2:], self.site_b)
        self.form(f'/hdc/tool-rental/{rental.id}/transfer', **{
            'from_location_key': f'site:{self.a_id}',
            'to_type': 'site', 'to_project_id': str(self.b_id),
            'rental_item_id[]': [str(item.id)], 'qty_transfer[]': ['1'],
            f'transfer_serial_ids_{item.id}[]': [str(issued[0].id)],
        })
        self.assertEqual([s.current_location_label for s in issued],
                         ['Site B', 'Site A', 'Site B', 'Site B'])
        self._assert_serial_balance()

    def test_transaction_return_lists_actual_holder_and_records_source(self):
        rental, item, issued = self._issue([1, 2, 3, 4], self.site_a)
        self._move(rental, issued[2:], self.site_b)
        rows = return_sheet()['rows']
        a_row = next(r for r in rows if r['holder'] == 'Site A')
        self.assertEqual(a_row['pending'], 2)
        self.form('/hdc/tool-rental/transaction/return', **{
            'return_rental_id[]': [str(rental.id)],
            'rental_item_id[]': [str(item.id)], 'qty_returned[]': ['2'],
            'condition_notes[]': [''], 'from_location_key[]': [a_row['locationKey']],
            f'return_serial_ids_{item.id}[]': [str(s.id) for s in issued[:2]],
        })
        self.assertTrue(issued[0].is_in_store)
        self.assertTrue(issued[1].is_in_store)
        self.assertEqual(issued[2].current_location_label, 'Site B')
        returned = ToolRentalReturnItem.query.order_by(ToolRentalReturnItem.id.desc()).first()
        self.assertEqual(returned.from_location_key, f'site:{self.a_id}')
        log = ToolMovementLog.query.filter_by(return_id=returned.return_id).first()
        self.assertEqual(log.from_location_label, 'Site A')
        self.assertEqual(log.timestamp.date().isoformat(), '2026-10-31')
        self._assert_serial_balance()

    def test_return_refuses_a_piece_from_another_issue(self):
        rental_a, item_a, pieces_a = self._issue([1, 2], self.site_a)
        rental_b, item_b, pieces_b = self._issue([3, 4], self.site_b)
        ok, message, _ = record_return(actor_id=1, lines=[{
            'rental_id': rental_a.id, 'rental_item_id': item_a.id,
            'qty': 1, 'serial_ids': [pieces_b[0].id],
        }])
        self.assertFalse(ok)
        self.assertEqual(ToolRentalReturn.query.count(), 0)
        self.assertEqual(pieces_b[0].current_rental_id, rental_b.id)
        self._assert_serial_balance()

    def test_rebuild_repairs_orphans_without_moving_valid_pieces(self):
        rental, item, issued = self._issue([1, 2, 3, 4], self.site_a)
        self._move(rental, issued[2:], self.site_b)
        orphan = self._pieces()[4]  # No 5 was never issued
        orphan.is_in_store = False
        orphan.current_rental_id = None
        orphan.current_rental_item_id = None
        orphan.current_location_label = 'Old / unlinked site'
        orphan.status = 'rented'
        issued[0].current_rental_item_id = None
        db.session.commit()
        ensure_tool_serials(tool_id=self.tool_id, created_by=1, commit=True)
        self.assertTrue(orphan.is_in_store)
        self.assertIsNone(orphan.current_rental_id)
        self.assertEqual(issued[0].current_rental_item_id, item.id)
        self.assertEqual(issued[0].current_location_label, 'Site A')
        self.assertTrue(ToolSerialMovement.query.filter_by(serial_id=orphan.id,
                        movement_type='return_in').count())
        self._assert_serial_balance()

    def test_audit_return_updates_and_reverses_the_same_pieces(self):
        rental, item, issued = self._issue([1, 2, 3, 4], self.site_a)
        self._move(rental, issued[2:], self.site_b)
        sheet = self._count(f'site:{self.a_id}', 0)
        ok, message, summary = audit.post_adjustments(
            sheet, reason=audit.AUDIT_RETURNED_REASON, user_id=1)
        self.assertTrue(ok, message)
        self.assertEqual(summary['returned_qty'], 2)
        self.assertTrue(issued[0].is_in_store)
        self.assertTrue(issued[1].is_in_store)
        self.assertEqual(issued[2].current_location_label, 'Site B')
        self._assert_serial_balance()
        ok, message = audit.void_audit(sheet, reason='Wrong count', reverse=True, user_id=1)
        self.assertTrue(ok, message)
        self.assertEqual([s.current_location_label for s in issued],
                         ['Site A', 'Site A', 'Site B', 'Site B'])
        self._assert_serial_balance()

    def test_audit_loss_scraps_the_missing_site_pieces_not_store_pieces(self):
        rental, item, issued = self._issue([1, 2, 3, 4], self.site_a)
        sheet = self._count(f'site:{self.a_id}', 2)
        ok, message, _ = audit.post_adjustments(sheet, reason='lost', user_id=1)
        self.assertTrue(ok, message)
        scrapped = {s.serial_number for s in self._pieces() if s.is_scrapped}
        self.assertEqual(scrapped, {'Grinder No 1', 'Grinder No 2'})
        self.assertFalse(issued[2].is_scrapped)
        self.assertEqual(ToolScrap.query.first().qty, 2)
        self._assert_serial_balance()

    def test_audit_gain_at_a_transferred_site_stays_there(self):
        rental, item, issued = self._issue([1, 2, 3, 4], self.site_a)
        self._move(rental, issued[2:], self.site_b)
        sheet = self._count(f'site:{self.b_id}', 4)
        ok, message, _ = audit.post_adjustments(sheet, mode='both', user_id=1)
        self.assertTrue(ok, message)
        ensure_all_tools_serials(commit=True)
        row = self._row()
        self.assertEqual(sum(h['qty'] for h in row['holdings'] if h['label'] == 'Site B'), 4)
        self.assertEqual(sum(h['qty'] for h in row['holdings'] if h['label'] == 'Site A'), 2)
        self.assertEqual(row['in_store_qty'], 4)
        self._assert_serial_balance()
        ok, message = audit.void_audit(sheet, reason='Wrong count', reverse=True, user_id=1)
        self.assertTrue(ok, message)
        self.assertEqual(len(self._pieces()), 8)
        self.assertEqual(db.session.get(Tool, self.tool_id).total_quantity, 8)
        self.assertEqual([s.current_location_label for s in issued],
                         ['Site A', 'Site A', 'Site B', 'Site B'])
        self._assert_serial_balance()

    def test_audit_loss_reversal_restores_the_same_pieces_to_the_holder(self):
        rental, item, issued = self._issue([1, 2, 3, 4], self.site_a)
        sheet = self._count(f'site:{self.a_id}', 2)
        ok, message, _ = audit.post_adjustments(sheet, reason='lost', user_id=1)
        self.assertTrue(ok, message)
        ok, message = audit.void_audit(sheet, reason='Found later', reverse=True, user_id=1)
        self.assertTrue(ok, message)
        self.assertEqual([s.is_scrapped for s in issued], [False, False, False, False])
        self.assertEqual([s.current_location_label for s in issued],
                         ['Site A', 'Site A', 'Site A', 'Site A'])
        self.assertEqual(db.session.get(Tool, self.tool_id).total_quantity, 8)
        self._assert_serial_balance()

    def test_damage_writeoff_takes_the_selected_holder_piece_not_store_stock(self):
        rental, item, issued = self._issue([1, 2, 3, 4], self.site_a)
        self._move(rental, issued[2:], self.site_b)
        store_piece = self._pieces()[4]
        ok, message, summary = record_damage_loss(
            actor_id=1, source='rental', rental_id=rental.id, rental_item_id=item.id,
            qty=1, serial_ids=[issued[0].id], reason='damaged', reference='SYNC-DMG')
        self.assertTrue(ok, message)
        self.assertTrue(issued[0].is_scrapped)
        self.assertFalse(store_piece.is_scrapped)
        self.assertTrue(store_piece.is_in_store)
        self.assertEqual(issued[2].current_location_label, 'Site B')
        returned = db.session.get(ToolRentalReturnItem,
                                  ToolRentalReturnItem.query.order_by(ToolRentalReturnItem.id.desc()).first().id)
        self.assertEqual(returned.from_location_key, f'site:{self.a_id}')
        self._assert_serial_balance()

    def test_maintenance_is_not_available_to_either_issue_form(self):
        ensure_all_tools_serials(commit=True)
        piece = self._pieces()[0]
        self.form('/hdc/tool-rental/transaction/maintenance',
                  maintenance_serial_ids_csv=str(piece.id), reason='Service')
        available = next(r['available_qty'] for r in in_store_tool_rows()
                         if r['id'] == self.tool_id)
        self.assertEqual(available, 7)
        self.assertEqual(db.session.get(Tool, self.tool_id).available_qty, 7)
        page = self.client.get('/hdc/tool-rental/new').get_data(as_text=True)
        self.assertNotIn(f'"id": {piece.id}, "serial_number"', page)
        self.form('/hdc/tool-rental/transaction/maintenance-return',
                  maintenance_serial_ids_csv=str(piece.id), condition='good')
        self.assertEqual(db.session.get(Tool, self.tool_id).available_qty, 8)
        self._assert_serial_balance()


if __name__ == '__main__':
    unittest.main()
