#!/usr/bin/env python3
"""Regression tests for the HDC Tools position dashboard.

Pins the behaviour described in TOOLS_TRACKING_SYSTEM.md:
  - owned == in store + own projects + customers (always reconciles)
  - "total sent" splits into own-project vs other-customer buckets
  - a site-to-site transfer moves the tool's current location and keeps the
    chain Site1 > Site2 > Site3 (current in green)
  - a per-tool transfer split moves only the tools that were selected
  - a return puts the qty back in store so the balance still holds
  - the universal search finds a tool by code, a rental by code and a
    customer by name, and reports where the pieces are
  - the dashboard stays a simple item-wise stock + unpaid-rent summary
    (no repeated tracking/audit blocks) and its filters narrow the list
  - a customer's unpaid rent is split per item, but own-site money is not
  - tracking and reports group tools per site/customer and expand long paths on View
  - the dashboard / position pages / JSON feed all render

Run with:
    HDC_BOOTSTRAP_ADMIN_PASSWORD='Admin@1234' \
        python -m unittest tests.test_tool_tracking -v
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

os.environ.setdefault('HDC_ENV', 'test')
os.environ.setdefault('HDC_SECRET_KEY', 'unit-test-secret')
os.environ.setdefault('HDC_BOOTSTRAP_ADMIN_PASSWORD', 'Admin@1234')

from hdc.app import create_app                                    # noqa: E402
from hdc.extensions import db                                     # noqa: E402
from hdc.models.projects import Project, Stage                    # noqa: E402
from hdc.models.tool_rental import (                              # noqa: E402
    Tool, ToolCategory, ToolRental, ToolRentalItem, ToolRentalPayment,
    ToolRentalTransfer, ToolRentalTransferItem,
)
from hdc.services.tool_rental import rental_transfer_chain        # noqa: E402
from hdc.services.tool_tracking import (                          # noqa: E402
    LOC_CUSTOMER, LOC_OWN_PROJECT, LOC_STORE, WAREHOUSE_LABEL,
    allocate_transfer_qty, dashboard_summary, location_summary,
    tool_item_summary, tool_ledger, tools_reconciliation,
    tools_universal_search,
)
from hdc.utils.dates import _pkt_today                            # noqa: E402

ADMIN_PASSWORD = os.environ['HDC_BOOTSTRAP_ADMIN_PASSWORD']


class ToolTrackingTestCase(unittest.TestCase):
    """One isolated app + database per test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='hdc-tool-track-test-')
        self.db_path = os.path.join(self.tmp, 'test.db')
        self.app = create_app({'HDC_DB_PATH': self.db_path,
                               'HDC_INSTANCE_DIR': self.tmp})
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()
        self._login()
        self.site_a, self.site_b = self._make_sites()
        self.cat = ToolCategory(name='Power Tools', active_status=True)
        db.session.add(self.cat)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ helpers
    def _login(self):
        r = self.client.get('/hdc/login')
        token = self._csrf(r.get_data(as_text=True))
        self.client.post('/hdc/login', data={
            'username': 'admin', 'password': ADMIN_PASSWORD, '_csrf_token': token,
        }, follow_redirects=True)

    @staticmethod
    def _csrf(html):
        m = re.search(r'name="_csrf_token" value="([^"]+)"', html or '')
        return m.group(1) if m else ''

    def _token(self):
        with self.client.session_transaction() as sess:
            return sess.get('_csrf_token', '')

    def _make_sites(self):
        a = Project(name='Site A', project_code='P-A', client='Owner A',
                    location='Karachi', contract_type='lump_sum',
                    owner_lump_sum=1_000_000)
        b = Project(name='Site B', project_code='P-B', client='Owner B',
                    location='Lahore', contract_type='lump_sum',
                    owner_lump_sum=2_000_000)
        db.session.add_all([a, b])
        db.session.flush()
        stage = Stage(project_id=a.id, name='Foundation',
                      contract_basis='Lump Sum', lump_sum_value=100_000)
        db.session.add(stage)
        db.session.commit()
        return a, b

    def _make_tool(self, code, name, qty, rate=500.0, cost=10_000.0,
                   condition='good', category=None):
        tool = Tool(tool_code=code, name=name, unit='pcs',
                    category_id=(category or self.cat).id,
                    total_quantity=qty, purchase_cost=cost,
                    rental_rate_per_day=rate, condition=condition,
                    status='active', is_void=False)
        db.session.add(tool)
        db.session.commit()
        return tool

    def _rent(self, tool_qty_pairs, renter_type='internal', project=None,
              stage=None, customer_name=None, customer_phone=None,
              billing='fixed_fee', rental_date=None, expected_return=None):
        """Create a rental straight through the ORM (the routes cover the UI)."""
        code = f'RENT-{(ToolRental.query.count() + 1):05d}'
        rental = ToolRental(
            rental_code=code, renter_type=renter_type,
            project_id=project.id if project else None,
            stage_id=stage.id if stage else None,
            customer_name=customer_name,
            customer_phone=customer_phone,
            rental_date=rental_date or _pkt_today(),
            expected_return_date=expected_return,
            billing_type=billing, status='active', payment_status='unpaid',
            is_void=False,
        )
        db.session.add(rental)
        db.session.flush()
        total_qty = 0.0
        total_amt = 0.0
        for tool, qty in tool_qty_pairs:
            rate = float(tool.rental_rate_per_day or 0)
            amount = qty * rate
            db.session.add(ToolRentalItem(
                rental_id=rental.id, tool_id=tool.id, qty_rented=qty,
                qty_returned=0.0, qty_pending=qty, rate=rate, amount=amount))
            total_qty += qty
            total_amt += amount
        rental.total_rented_qty = total_qty
        rental.total_amount = total_amt if billing != 'no_charge' else 0.0
        db.session.commit()
        return rental

    def _return(self, rental, qty_by_tool_id):
        for item in rental.items:
            take = float(qty_by_tool_id.get(int(item.tool_id), 0))
            if take <= 0:
                continue
            item.qty_returned = float(item.qty_returned or 0) + take
            item.qty_pending = max(0.0, float(item.qty_rented or 0) - float(item.qty_returned or 0))
        rental.total_returned_qty = sum(float(i.qty_returned or 0) for i in rental.items)
        db.session.commit()

    def _row(self, ledger, tool_id):
        return next(r for r in ledger['tools'] if int(r['tool_id']) == int(tool_id))

    # ------------------------------------------------------------ tests
    def test_all_in_store_reconciles(self):
        self._make_tool('TOOL-0001', 'Vibrator', 10)
        ledger = tool_ledger()
        recon = tools_reconciliation(ledger)
        self.assertEqual(recon['owned'], 10.0)
        self.assertEqual(recon['in_store'], 10.0)
        self.assertEqual(recon['own_project'], 0.0)
        self.assertEqual(recon['customer'], 0.0)
        self.assertEqual(recon['total_sent'], 0.0)
        self.assertTrue(recon['balanced'])
        self.assertEqual(ledger['totals']['owned_qty'],
                         ledger['totals']['in_store_qty'] + ledger['totals']['out_qty'])

    def test_own_project_vs_customer_split(self):
        """The headline ask: total sent = own sites + other customers."""
        vib = self._make_tool('TOOL-0001', 'Vibrator', 50)
        jack = self._make_tool('TOOL-0002', 'Jack Hammer', 20)

        self._rent([(vib, 30)], renter_type='internal', project=self.site_a)
        self._rent([(vib, 12), (jack, 8)], renter_type='external',
                   customer_name='Ali Traders', billing='per_day')

        ledger = tool_ledger()
        recon = tools_reconciliation(ledger)

        self.assertEqual(recon['owned'], 70.0)
        self.assertEqual(recon['own_project'], 30.0)
        self.assertEqual(recon['customer'], 20.0)
        self.assertEqual(recon['total_sent'], 50.0)
        self.assertEqual(recon['in_store'], 20.0)   # 50+20 owned - 50 out
        self.assertTrue(recon['balanced'])

        vib_row = self._row(ledger, vib.id)
        self.assertEqual(vib_row['own_project_qty'], 30.0)
        self.assertEqual(vib_row['customer_qty'], 12.0)
        self.assertEqual(vib_row['in_store_qty'], 8.0)
        self.assertEqual(vib_row['owned_qty'],
                         vib_row['in_store_qty'] + vib_row['own_project_qty'] + vib_row['customer_qty'])
        self.assertFalse(vib_row['unaccounted'])

        # utilization: 50 of 70 pieces are out
        self.assertAlmostEqual(ledger['totals']['utilization_pct'], 71.4, places=1)
        self.assertEqual(ledger['totals']['own_project_count'], 1)
        self.assertEqual(ledger['totals']['customer_count'], 1)

    def test_transfer_moves_current_location_and_keeps_chain(self):
        vib = self._make_tool('TOOL-0001', 'Vibrator', 10)
        rental = self._rent([(vib, 10)], renter_type='internal', project=self.site_a)

        token = self._token()
        r = self.client.post(f'/hdc/tool-rental/{rental.id}/transfer', data={
            '_csrf_token': token, 'to_type': 'site',
            'to_project_id': str(self.site_b.id), 'to_stage_id': '',
            'qty_transferred': '10',
            'transfer_date': _pkt_today().isoformat(), 'notes': 'moved to B',
        }, follow_redirects=True)
        self.assertEqual(r.status_code, 200)

        ledger = tool_ledger()
        row = self._row(ledger, vib.id)
        self.assertEqual(len(row['holdings']), 1)
        self.assertEqual(row['holdings'][0]['label'], 'Site B')
        self.assertEqual(row['holdings'][0]['loc_type'], LOC_OWN_PROJECT)
        chain_labels = [c['label'] for c in row['holdings'][0]['chain']]
        self.assertEqual(chain_labels, ['Site A', 'Site B'])
        self.assertTrue(row['holdings'][0]['chain'][-1]['is_current'])
        # still reconciles after the move
        self.assertTrue(tools_reconciliation(ledger)['balanced'])
        self.assertEqual(row['own_project_qty'], 10.0)

        # the split was persisted per tool
        split = ToolRentalTransferItem.query.filter_by(tool_id=vib.id).first()
        self.assertIsNotNone(split)
        self.assertEqual(float(split.qty_transferred), 10.0)

    def test_partial_transfer_moves_only_selected_qty(self):
        vib = self._make_tool('TOOL-0001', 'Vibrator', 20)
        jack = self._make_tool('TOOL-0002', 'Jack Hammer', 5)
        rental = self._rent([(vib, 20), (jack, 5)], renter_type='internal',
                            project=self.site_a)
        vib_item = next(i for i in rental.items if int(i.tool_id) == vib.id)

        token = self._token()
        # move 8 vibrators only; jack hammers stay on Site A
        r = self.client.post(f'/hdc/tool-rental/{rental.id}/transfer', data={
            '_csrf_token': token, 'to_type': 'site',
            'to_project_id': str(self.site_b.id),
            'rental_item_id[]': [str(vib_item.id)],
            'qty_transfer[]': ['8'],
            'transfer_date': _pkt_today().isoformat(),
        }, follow_redirects=True)
        self.assertEqual(r.status_code, 200)

        ledger = tool_ledger()
        vib_row = self._row(ledger, vib.id)
        jack_row = self._row(ledger, jack.id)
        # only 8 of the 20 vibrators moved: the rest are still on Site A
        by_label = {h['label']: h['qty'] for h in vib_row['holdings']}
        self.assertEqual(by_label, {'Site A': 12.0, 'Site B': 8.0})
        self.assertEqual(vib_row['own_project_qty'], 20.0)
        moved = next(h for h in vib_row['holdings'] if h['label'] == 'Site B')
        self.assertEqual([c['label'] for c in moved['chain']], ['Site A', 'Site B'])
        stayed = next(h for h in vib_row['holdings'] if h['label'] == 'Site A')
        self.assertEqual([c['label'] for c in stayed['chain']], ['Site A'])
        # jack was never transferred -> still reported on the rental's origin
        self.assertEqual(jack_row['holdings'][0]['label'], 'Site A')
        self.assertEqual(ToolRentalTransferItem.query.filter_by(tool_id=jack.id).count(), 0)
        self.assertTrue(tools_reconciliation(ledger)['balanced'])

    def test_transfer_rental_form_follows_from_rent_type_to_tools_settings_order(self):
        vib = self._make_tool('TOOL-0001', 'Vibrator', 10)
        source = self._rent([(vib, 4)], renter_type='internal', project=self.site_a)

        response = self.client.get('/hdc/tool-rental/new')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        # The transfer UI is ordered so the available-tool list sits directly
        # below the From holder picker (holder list → tools), and the
        # destination follows, with rates controlled by rent type.
        ordered_ids = [
            'id="txnType"', 'id="transferSourceField"', 'id="transferHoldersList"',
            'id="transferToolsPanel"', 'id="billingType"',
            'id="renterType"', 'id="otherSettings"',
        ]
        positions = [html.index(marker) for marker in ordered_ids]
        self.assertEqual(positions, sorted(positions))
        self.assertIn('Rent already fixed in contact / contract (no extra rent)', html)
        self.assertIn('transfer_item_id[]', html)
        self.assertIn('transfer_rate_', html)
        self.assertIn("cell.classList.toggle('d-none', noCharge)", html)
        self.assertIn(str(source.id), html)
        self.assertIn(str(source.items[0].id), html)

    def test_transfer_picker_lists_only_locations_and_tools_with_live_holdings(self):
        vib = self._make_tool('TOOL-0001', 'Vibrator', 10)
        jack = self._make_tool('TOOL-0002', 'Jack Hammer', 5)
        self._make_tool('TOOL-0003', 'Idle Saw', 3)
        empty_site = Project(name='Site C', project_code='P-C', client='Owner C')
        db.session.add(empty_site)
        db.session.commit()
        # The same site can have several active rentals; the site-first list
        # should still show that location once, while the exact source picker
        # can disambiguate the rental when needed.
        self._rent([(vib, 4)], renter_type='internal', project=self.site_a)
        self._rent([(vib, 2)], renter_type='internal', project=self.site_a)
        self._rent([(jack, 2)], renter_type='internal', project=self.site_b)

        response = self.client.get('/hdc/tool-rental/new')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)

        def picker_options(select_id):
            start = html.index(f'<select id="{select_id}"')
            end = html.index('</select>', start)
            return html[start:end]

        site_options = picker_options('transferFromSite')
        self.assertIn(f'own_project|{self.site_a.id}|0|', site_options)
        self.assertIn(f'own_project|{self.site_b.id}|0|', site_options)
        self.assertEqual(site_options.count(f'value="own_project|{self.site_a.id}|0|"'), 1)
        self.assertIn('Site A', site_options)
        self.assertIn('Site B', site_options)
        self.assertNotIn('Site C', site_options)

        tool_options = picker_options('transferFindTool')
        self.assertIn('Vibrator', tool_options)
        self.assertIn('Jack Hammer', tool_options)
        self.assertNotIn('Idle Saw', tool_options)
        self.assertIn('out at 1 location', tool_options)

        # The client narrows sites from the current position rows, not from
        # every project or from tools that merely exist in inventory.
        self.assertIn('source.location_key', html)
        self.assertIn('location.tool_ids', html)
        self.assertIn('transfer_item_id[]', html)

    def test_transfer_rental_moves_only_checked_tools_and_uses_destination_rate(self):
        vib = self._make_tool('TOOL-0001', 'Vibrator', 10, rate=500)
        jack = self._make_tool('TOOL-0002', 'Jack Hammer', 5, rate=300)
        source = self._rent([(vib, 10), (jack, 5)], renter_type='internal',
                            project=self.site_a, billing='per_day')
        vib_item = next(item for item in source.items if item.tool_id == vib.id)
        jack_item = next(item for item in source.items if item.tool_id == jack.id)

        response = self.client.post('/hdc/tool-rental/create', data={
            '_csrf_token': self._token(),
            'txn_type': 'transfer',
            'source_rental_id': str(source.id),
            'transfer_selection_enabled': '1',
            'transfer_item_id[]': [str(vib_item.id)],
            f'transfer_rate_{vib_item.id}': '875',
            'billing_type': 'per_day',
            'renter_type': 'internal',
            'project_id': str(self.site_b.id),
            'rental_date': _pkt_today().isoformat(),
            'settle_choice': 'add_credit',
            'notes': 'Transfer approved by site manager',
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('Vibrator x10', response.get_data(as_text=True))

        new_rental = ToolRental.query.filter(ToolRental.id != source.id).order_by(
            ToolRental.id.desc()).first()
        self.assertIsNotNone(new_rental)
        self.assertEqual(new_rental.renter_type, 'internal')
        self.assertEqual(new_rental.project_id, self.site_b.id)
        self.assertEqual(new_rental.billing_type, 'per_day')
        self.assertIn('Transfer approved by site manager', new_rental.notes)
        self.assertEqual(float(new_rental.total_rented_qty), 10.0)
        self.assertEqual(float(new_rental.total_amount), 8750.0)
        self.assertEqual(len(new_rental.items), 1)
        self.assertEqual(new_rental.items[0].tool_id, vib.id)
        self.assertEqual(float(new_rental.items[0].rate), 875.0)
        self.assertEqual(float(new_rental.items[0].amount), 8750.0)

        db.session.refresh(vib_item)
        db.session.refresh(jack_item)
        self.assertEqual(float(vib_item.qty_pending), 0.0)
        self.assertEqual(float(jack_item.qty_pending), 5.0)
        self.assertEqual(float(source.total_pending_tools), 5.0)
        self.assertEqual(source.payment_status, 'credit')

        transfer = ToolRentalTransfer.query.filter_by(rental_id=source.id).first()
        self.assertIsNotNone(transfer)
        self.assertEqual(transfer.to_rental_id, new_rental.id)
        self.assertEqual(float(transfer.qty_transferred), 10.0)
        split = ToolRentalTransferItem.query.filter_by(transfer_id=transfer.id).all()
        self.assertEqual([(item.tool_id, float(item.qty_transferred)) for item in split],
                         [(vib.id, 10.0)])

        ledger = tool_ledger()
        vib_row = self._row(ledger, vib.id)
        jack_row = self._row(ledger, jack.id)
        self.assertEqual([(h['label'], h['qty']) for h in vib_row['holdings']],
                         [('Site B', 10.0)])
        self.assertEqual([(h['label'], h['qty']) for h in jack_row['holdings']],
                         [('Site A', 5.0)])
        self.assertTrue(tools_reconciliation(ledger)['balanced'])

    def test_transfer_rent_already_in_contact_hides_and_ignores_rates(self):
        vib = self._make_tool('TOOL-0001', 'Vibrator', 10, rate=500)
        jack = self._make_tool('TOOL-0002', 'Jack Hammer', 5, rate=300)
        source = self._rent([(vib, 10), (jack, 5)], renter_type='internal',
                            project=self.site_a, billing='no_charge')
        vib_item = next(item for item in source.items if item.tool_id == vib.id)

        response = self.client.post('/hdc/tool-rental/create', data={
            '_csrf_token': self._token(),
            'txn_type': 'transfer',
            'source_rental_id': str(source.id),
            'transfer_selection_enabled': '1',
            'transfer_item_id[]': [str(vib_item.id)],
            # A forged/stale rate must never create rent for a no-charge transfer.
            f'transfer_rate_{vib_item.id}': '9999',
            'billing_type': 'no_charge',
            'renter_type': 'internal',
            'project_id': str(self.site_b.id),
            'rental_date': _pkt_today().isoformat(),
            'settle_choice': 'pay_now',
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)

        new_rental = ToolRental.query.filter(ToolRental.id != source.id).order_by(
            ToolRental.id.desc()).first()
        self.assertEqual(new_rental.billing_type, 'no_charge')
        self.assertEqual(float(new_rental.total_amount), 0.0)
        self.assertEqual(float(new_rental.items[0].rate), 0.0)
        self.assertEqual(float(new_rental.items[0].amount), 0.0)
        self.assertEqual(float(new_rental.total_rented_qty), 10.0)
        self.assertEqual(float(source.total_pending_tools), 5.0)

    def test_transfer_rental_lists_and_moves_only_tools_at_selected_from_location(self):
        vib = self._make_tool('TOOL-0001', 'Vibrator', 10)
        jack = self._make_tool('TOOL-0002', 'Jack Hammer', 5)
        source = self._rent([(vib, 10), (jack, 5)], renter_type='internal',
                            project=self.site_a, billing='no_charge')
        vib_item = next(item for item in source.items if item.tool_id == vib.id)

        # Split the source rental across two sites first: six vibrators and all
        # jack hammers remain on A, while four vibrators sit at B.
        self.client.post(f'/hdc/tool-rental/{source.id}/transfer', data={
            '_csrf_token': self._token(), 'to_type': 'site',
            'to_project_id': str(self.site_b.id),
            'rental_item_id[]': [str(vib_item.id)], 'qty_transfer[]': ['4'],
            'transfer_date': _pkt_today().isoformat(),
        }, follow_redirects=True)

        page = self.client.get('/hdc/tool-rental/new').get_data(as_text=True)
        from_key = f'{source.id}|own_project|{self.site_b.id}|0|'
        self.assertIn(from_key, page)
        self.assertIn('Site B (4 available)', page)
        self.assertIn('Jack Hammer', page)

        response = self.client.post('/hdc/tool-rental/create', data={
            '_csrf_token': self._token(),
            'txn_type': 'transfer',
            'source_rental_id': from_key,
            'transfer_selection_enabled': '1',
            'transfer_item_id[]': [str(vib_item.id)],
            'billing_type': 'no_charge',
            'renter_type': 'internal',
            'project_id': str(self.site_a.id),
            'rental_date': _pkt_today().isoformat(),
            'settle_choice': 'pay_now',
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)

        transfer = ToolRentalTransfer.query.filter_by(rental_id=source.id).order_by(
            ToolRentalTransfer.id.desc()).first()
        self.assertEqual(transfer.from_project_id, self.site_b.id)
        self.assertEqual(transfer.from_location_label, 'Site B')
        self.assertEqual(float(transfer.qty_transferred), 4.0)
        db.session.refresh(vib_item)
        self.assertEqual(float(vib_item.qty_pending), 6.0)

        ledger = tool_ledger()
        vib_row = self._row(ledger, vib.id)
        jack_row = self._row(ledger, jack.id)
        vib_locations = {}
        for holding in vib_row['holdings']:
            vib_locations[holding['label']] = vib_locations.get(holding['label'], 0.0) + holding['qty']
        self.assertEqual(vib_locations, {'Site A': 10.0})
        self.assertEqual([(h['label'], h['qty']) for h in jack_row['holdings']],
                         [('Site A', 5.0)])
        self.assertTrue(tools_reconciliation(ledger)['balanced'])

    def test_transfer_rental_rejects_empty_explicit_tool_selection(self):
        vib = self._make_tool('TOOL-0001', 'Vibrator', 10)
        source = self._rent([(vib, 4)], renter_type='internal', project=self.site_a)

        response = self.client.post('/hdc/tool-rental/create', data={
            '_csrf_token': self._token(),
            'txn_type': 'transfer',
            'source_rental_id': str(source.id),
            'transfer_selection_enabled': '1',
            'billing_type': 'fixed_fee',
            'renter_type': 'internal',
            'project_id': str(self.site_b.id),
            'rental_date': _pkt_today().isoformat(),
            'settle_choice': 'add_credit',
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('Select at least one tool to transfer', response.get_data(as_text=True))
        self.assertEqual(ToolRental.query.count(), 1)
        self.assertEqual(float(source.total_pending_tools), 4.0)
        self.assertEqual(ToolRentalTransfer.query.count(), 0)

    def test_transfer_form_lists_every_holder_at_a_site_with_select_all(self):
        """A site's tools can come from several rentals — all must be pickable."""
        vib = self._make_tool('TOOL-0001', 'Vibrator', 10)
        jack = self._make_tool('TOOL-0002', 'Jack Hammer', 5)
        first = self._rent([(vib, 4)], renter_type='internal', project=self.site_a)
        second = self._rent([(jack, 3)], renter_type='internal', project=self.site_a)

        html = self.client.get('/hdc/tool-rental/new').get_data(as_text=True)

        # Both holders of Site A reach the page with their exact source keys,
        # instead of the form forcing the operator to choose a single one.
        site_key = f'own_project|{self.site_a.id}|0|'
        self.assertIn(f'"{first.id}|{site_key}"', html)
        self.assertIn(f'"{second.id}|{site_key}"', html)
        # The holder picker is a checkbox list (with select-all), so tools from
        # several rentals can move together in one transfer.
        self.assertIn('id="transferHoldersList"', html)
        self.assertIn('id="selectAllTransferHolders"', html)
        self.assertIn('toggleAllTransferHolders', html)
        self.assertIn("box.name = 'source_rental_id[]'", html)
        self.assertIn('transfer-holder-checkbox', html)
        self.assertIn('selectedTransferSources', html)
        self.assertIn('Select all holders', html)
        # The old single-choice combo is gone: choosing one holder would hide
        # the other holders' tools at the same site.
        self.assertNotIn('id="sourceRental"', html)

    def test_transfer_moves_tools_from_several_holders_in_one_transfer(self):
        vib = self._make_tool('TOOL-0001', 'Vibrator', 10, rate=500)
        jack = self._make_tool('TOOL-0002', 'Jack Hammer', 5, rate=300)
        vib_holder = self._rent([(vib, 4)], renter_type='internal',
                                project=self.site_a, billing='per_day')
        jack_holder = self._rent([(jack, 3)], renter_type='internal',
                                 project=self.site_a, billing='per_day')
        vib_item = next(i for i in vib_holder.items if i.tool_id == vib.id)
        jack_item = next(i for i in jack_holder.items if i.tool_id == jack.id)
        site_key = f'own_project|{self.site_a.id}|0|'

        response = self.client.post('/hdc/tool-rental/create', data={
            '_csrf_token': self._token(),
            'txn_type': 'transfer',
            'source_rental_id[]': [f'{vib_holder.id}|{site_key}',
                                   f'{jack_holder.id}|{site_key}'],
            'transfer_selection_enabled': '1',
            'transfer_item_id[]': [str(vib_item.id), str(jack_item.id)],
            f'transfer_rate_{vib_item.id}': '650',
            f'transfer_rate_{jack_item.id}': '350',
            'billing_type': 'per_day',
            'renter_type': 'internal',
            'project_id': str(self.site_b.id),
            'rental_date': _pkt_today().isoformat(),
            'settle_choice': 'add_credit',
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('Vibrator x4', html)
        self.assertIn('Jack Hammer x3', html)

        new_rental = (ToolRental.query
                      .filter(ToolRental.id.notin_([vib_holder.id, jack_holder.id]))
                      .order_by(ToolRental.id.desc()).first())
        self.assertIsNotNone(new_rental)
        self.assertEqual(new_rental.renter_type, 'internal')
        self.assertEqual(new_rental.project_id, self.site_b.id)
        self.assertEqual(float(new_rental.total_rented_qty), 7.0)
        self.assertEqual(sorted(int(i.tool_id) for i in new_rental.items), [vib.id, jack.id])
        self.assertEqual({int(i.tool_id): float(i.rate) for i in new_rental.items},
                         {vib.id: 650.0, jack.id: 350.0})

        # Each holder keeps its own hand-over row, and every row walks forward
        # to the same new rental.
        transfers = ToolRentalTransfer.query.order_by(ToolRentalTransfer.id.asc()).all()
        self.assertEqual([t.rental_id for t in transfers],
                         [vib_holder.id, jack_holder.id])
        self.assertEqual({t.to_rental_id for t in transfers}, {new_rental.id})
        self.assertEqual(sorted(float(t.qty_transferred) for t in transfers), [3.0, 4.0])
        self.assertEqual(sorted(i.rental_item_id for i in
                                ToolRentalTransferItem.query.all()),
                         sorted([vib_item.id, jack_item.id]))
        # Both holders merged into one destination: the chain names the path
        # once, not the same site twice.
        self.assertEqual(rental_transfer_chain(new_rental.id),
                         ['Site A', 'Site B'])

        db.session.refresh(vib_item)
        db.session.refresh(jack_item)
        self.assertEqual(float(vib_item.qty_pending), 0.0)
        self.assertEqual(float(jack_item.qty_pending), 0.0)
        self.assertEqual(vib_holder.payment_status, 'credit')
        self.assertEqual(jack_holder.payment_status, 'credit')

        ledger = tool_ledger()
        self.assertEqual([(h['label'], h['qty'])
                          for h in self._row(ledger, vib.id)['holdings']],
                         [('Site B', 4.0)])
        self.assertEqual([(h['label'], h['qty'])
                          for h in self._row(ledger, jack.id)['holdings']],
                         [('Site B', 3.0)])
        self.assertTrue(tools_reconciliation(ledger)['balanced'])

    def test_transfer_from_several_holders_settles_each_holders_rent(self):
        vib = self._make_tool('TOOL-0001', 'Vibrator', 10, rate=500)
        jack = self._make_tool('TOOL-0002', 'Jack Hammer', 5, rate=300)
        vib_holder = self._rent([(vib, 2)], renter_type='internal',
                                project=self.site_a, billing='per_day')
        jack_holder = self._rent([(jack, 2)], renter_type='internal',
                                 project=self.site_a, billing='per_day')
        vib_item = next(i for i in vib_holder.items if i.tool_id == vib.id)
        jack_item = next(i for i in jack_holder.items if i.tool_id == jack.id)
        site_key = f'own_project|{self.site_a.id}|0|'

        response = self.client.post('/hdc/tool-rental/create', data={
            '_csrf_token': self._token(),
            'txn_type': 'transfer',
            'source_rental_id[]': [f'{vib_holder.id}|{site_key}',
                                   f'{jack_holder.id}|{site_key}'],
            'transfer_selection_enabled': '1',
            'transfer_item_id[]': [str(vib_item.id), str(jack_item.id)],
            'billing_type': 'fixed_fee',
            'renter_type': 'internal',
            'project_id': str(self.site_b.id),
            'rental_date': _pkt_today().isoformat(),
            'settle_choice': 'pay_now',
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('Finalised', response.get_data(as_text=True))

        # Every contributing holder's rent is collected (and posted) on its own.
        payments = ToolRentalPayment.query.order_by(ToolRentalPayment.id.asc()).all()
        self.assertEqual([p.rental_id for p in payments],
                         [vib_holder.id, jack_holder.id])
        self.assertEqual([float(p.amount) for p in payments], [1000.0, 600.0])
        db.session.refresh(vib_holder)
        db.session.refresh(jack_holder)
        self.assertEqual(float(vib_holder.total_pending_amount), 0.0)
        self.assertEqual(float(jack_holder.total_pending_amount), 0.0)

    def test_allocate_transfer_qty_never_exceeds_request(self):
        vib = self._make_tool('TOOL-0001', 'Vibrator', 30)
        jack = self._make_tool('TOOL-0002', 'Jack Hammer', 10)
        rental = self._rent([(vib, 30), (jack, 10)], renter_type='internal',
                            project=self.site_a)
        pairs = [(i, float(i.qty_pending)) for i in rental.items]

        self.assertEqual([(float(q)) for _, q in allocate_transfer_qty(pairs, None)],
                         [30.0, 10.0])
        capped = allocate_transfer_qty(pairs, 35)
        self.assertEqual(sum(q for _, q in capped), 35.0)
        self.assertEqual(allocate_transfer_qty(pairs, 999), pairs and [(i, q) for i, q in pairs])
        self.assertEqual(allocate_transfer_qty([], 5), [])

    def test_return_puts_qty_back_in_store(self):
        vib = self._make_tool('TOOL-0001', 'Vibrator', 10)
        rental = self._rent([(vib, 10)], renter_type='external',
                            customer_name='Ali Traders')
        ledger = tool_ledger()
        self.assertEqual(self._row(ledger, vib.id)['customer_qty'], 10.0)
        self.assertEqual(self._row(ledger, vib.id)['in_store_qty'], 0.0)

        self._return(rental, {vib.id: 4})
        ledger = tool_ledger()
        row = self._row(ledger, vib.id)
        self.assertEqual(row['customer_qty'], 6.0)
        self.assertEqual(row['in_store_qty'], 4.0)
        self.assertTrue(tools_reconciliation(ledger)['balanced'])

        self._return(rental, {vib.id: 6})
        ledger = tool_ledger()
        row = self._row(ledger, vib.id)
        self.assertEqual(row['in_store_qty'], 10.0)
        self.assertEqual(row['out_qty'], 0.0)
        self.assertEqual(row['holdings'], [])
        self.assertEqual(row['current_label'], WAREHOUSE_LABEL)

    def test_stock_edit_below_out_qty_is_flagged_not_hidden(self):
        vib = self._make_tool('TOOL-0001', 'Vibrator', 10)
        self._rent([(vib, 10)], renter_type='internal', project=self.site_a)
        # somebody lowers the owned qty while all 10 are out on a site
        vib.total_quantity = 4
        db.session.commit()

        ledger = tool_ledger()
        recon = tools_reconciliation(ledger)
        row = self._row(ledger, vib.id)
        self.assertTrue(row['unaccounted'])
        self.assertFalse(recon['balanced'])
        self.assertEqual(recon['variance'], -6.0)
        self.assertIn(row, recon['unaccounted_rows'])
        titles = ' '.join(i['title'] for i in dashboard_summary(ledger)['attention'])
        self.assertIn('does not reconcile', titles)

    def test_overdue_and_long_out_are_flagged(self):
        vib = self._make_tool('TOOL-0001', 'Vibrator', 5)
        jack = self._make_tool('TOOL-0002', 'Jack Hammer', 5)
        today = _pkt_today()
        self._rent([(vib, 5)], renter_type='external', customer_name='Late Co',
                   expected_return=today - timedelta(days=3))
        self._rent([(jack, 5)], renter_type='internal', project=self.site_b,
                   rental_date=today - timedelta(days=45))

        ledger = tool_ledger()
        vib_row = self._row(ledger, vib.id)
        jack_row = self._row(ledger, jack.id)
        self.assertTrue(vib_row['holdings'][0]['overdue'])
        self.assertEqual(vib_row['overdue_qty'], 5.0)
        self.assertTrue(jack_row['long_out'])
        self.assertGreaterEqual(jack_row['oldest_days_out'], 45)
        self.assertEqual(ledger['totals']['overdue_rentals'], 1)

        attention = dashboard_summary(ledger)['attention']
        blobs = ' '.join(f"{i['title']} {i['detail']}" for i in attention)
        self.assertIn('overdue', blobs)
        self.assertIn('Late Co', blobs)

    def test_location_summary_groups_by_site_and_customer(self):
        vib = self._make_tool('TOOL-0001', 'Vibrator', 30)
        jack = self._make_tool('TOOL-0002', 'Jack Hammer', 10)
        self._rent([(vib, 10), (jack, 4)], renter_type='internal', project=self.site_a)
        self._rent([(vib, 6)], renter_type='external', customer_name='Ali Traders')

        locations = {loc['label']: loc for loc in location_summary(tool_ledger())}
        self.assertIn('Site A', locations)
        self.assertEqual(locations['Site A']['qty'], 14.0)
        self.assertEqual(locations['Site A']['tool_types'], 2)
        self.assertEqual(locations['Site A']['loc_type'], LOC_OWN_PROJECT)
        self.assertIn('Ali Traders', locations)
        self.assertEqual(locations['Ali Traders']['qty'], 6.0)
        self.assertEqual(locations['Ali Traders']['loc_type'], LOC_CUSTOMER)
        self.assertIn(WAREHOUSE_LABEL, locations)
        self.assertEqual(locations[WAREHOUSE_LABEL]['qty'], 20.0)
        self.assertEqual(locations[WAREHOUSE_LABEL]['loc_type'], LOC_STORE)

    def test_universal_search_finds_tool_rental_and_customer(self):
        vib = self._make_tool('TOOL-0001', 'Vibrator', 20)
        rental = self._rent([(vib, 8)], renter_type='external',
                            customer_name='Ali Traders', customer_phone='0300-1234567')

        by_code = tools_universal_search('TOOL-0001')
        self.assertEqual(len(by_code['tools']), 1)
        self.assertEqual(by_code['tools'][0]['tool_id'], vib.id)

        by_name = tools_universal_search('vibra')
        self.assertEqual(len(by_name['tools']), 1)

        by_rental = tools_universal_search(rental.rental_code)
        self.assertEqual(len(by_rental['rentals']), 1)
        self.assertEqual(by_rental['rentals'][0]['pending_qty'], 8.0)
        self.assertEqual(by_rental['rentals'][0]['pending_tools'][0]['location'], 'Ali Traders')

        by_customer = tools_universal_search('ali traders')
        self.assertTrue(by_customer['rentals'] or by_customer['locations'])
        labels = [loc['label'] for loc in by_customer['locations']]
        self.assertIn('Ali Traders', labels)

        self.assertEqual(tools_universal_search('')['total'], 0)
        self.assertEqual(tools_universal_search('zzz-nothing')['tools'], [])

    def test_dashboard_is_a_simple_item_wise_summary(self):
        """The dashboard answers only the stock question — and repeats nothing.

        Item by item: how many we own, how many are in the store, how many are
        rented out and how much rent customers have not paid.  Movement chains,
        per-site positions and audit panels belong to the Tracking / Rentals
        pages, so the dashboard must not carry them again.
        """
        vib = self._make_tool('TOOL-0001', 'Vibrator', 50, rate=500)
        self._make_tool('TOOL-0002', 'Jack Hammer', 20, condition='damaged')
        self._rent([(vib, 30)], renter_type='internal', project=self.site_a)
        self._rent([(vib, 12)], renter_type='external', customer_name='Ali Traders')

        r = self.client.get('/hdc/tool-rental/dashboard')
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        # the four simple headline numbers
        self.assertIn('Total Tools Owned', html)
        self.assertIn('In Store (Available)', html)
        self.assertIn('Rented Out', html)
        self.assertIn('Rent Not Paid by Customers (PKR)', html)
        # one item-wise table: owned / store / rented / unpaid per item
        self.assertIn('Tools by item', html)
        self.assertIn('>Vibrator</a>', html)
        self.assertIn('TOOL-0001', html)
        self.assertIn('Totals (2 shown)', html)
        # 12 pcs out to the customer at 500/day = 6,000 not paid
        self.assertIn('6,000', html)
        # no repeated tracking / audit blocks on this page
        self.assertNotIn('tools-split-seg', html)
        self.assertNotIn('Does every tool add up', html)
        self.assertNotIn('Needs Attention', html)
        self.assertNotIn('By location', html)
        self.assertNotIn('Movement history', html)

    def test_dashboard_filters_narrow_the_item_list(self):
        vib = self._make_tool('TOOL-0001', 'Vibrator', 50)
        other_cat = ToolCategory(name='Manual Tools', active_status=True)
        db.session.add(other_cat)
        db.session.commit()
        self._make_tool('TOOL-0002', 'Jack Hammer', 20, category=other_cat)
        self._rent([(vib, 30)], renter_type='internal', project=self.site_a)

        r = self.client.get('/hdc/tool-rental/dashboard?q=vibrator')
        html = r.get_data(as_text=True)
        self.assertIn('Totals (1 shown)', html)
        self.assertIn('>Vibrator</a>', html)
        self.assertNotIn('>Jack Hammer</a>', html)

        r = self.client.get(f'/hdc/tool-rental/dashboard?category_id={self.cat.id}')
        html = r.get_data(as_text=True)
        self.assertIn('Totals (1 shown)', html)
        self.assertIn('>Vibrator</a>', html)
        self.assertNotIn('>Jack Hammer</a>', html)

        r = self.client.get('/hdc/tool-rental/dashboard?q=nothing-like-this')
        self.assertIn('No item matches this search', r.get_data(as_text=True))

    def test_item_summary_counts_only_unpaid_customer_rent(self):
        """Money: unpaid rent per item, customers only, apportioned per line."""
        vib = self._make_tool('TOOL-0001', 'Vibrator', 50, rate=500)
        jack = self._make_tool('TOOL-0002', 'Jack Hammer', 20, rate=1000)
        self._rent([(vib, 30)], renter_type='internal', project=self.site_a)
        external = self._rent([(vib, 12), (jack, 8)], renter_type='external',
                              customer_name='Ali Traders')
        # own-site money is internal, and a no-charge rental owes nothing
        self._rent([(vib, 5)], renter_type='internal', project=self.site_b)
        self._rent([(jack, 3)], renter_type='external', customer_name='Free Co',
                   billing='no_charge')

        summary = tool_item_summary()
        by_name = {r['name']: r for r in summary['rows']}
        self.assertEqual(by_name['Vibrator']['pending_amount'], 6000.0)
        self.assertEqual(by_name['Jack Hammer']['pending_amount'], 8000.0)
        self.assertEqual(summary['totals']['pending_amount'], 14000.0)
        self.assertEqual(summary['customer_count'], 1)
        self.assertEqual(summary['unpaid_rental_count'], 1)
        # own-site (internal) charges are reported separately, never as dues
        self.assertEqual(summary['internal_pending'], 30 * 500 + 5 * 500)
        # stock side of the same rows still reconciles: a no-charge customer
        # still holds the pieces, it just does not owe money for them
        self.assertEqual(by_name['Jack Hammer']['owned_qty'], 20.0)
        self.assertEqual(by_name['Jack Hammer']['customer_qty'], 11.0)
        self.assertEqual(by_name['Jack Hammer']['in_store_qty'], 9.0)

        # a part payment lowers the due and is shared pro-rata across the lines
        external.total_paid = 4000.0
        db.session.commit()
        summary = tool_item_summary()
        by_name = {r['name']: r for r in summary['rows']}
        self.assertEqual(summary['totals']['pending_amount'], 10000.0)
        self.assertAlmostEqual(by_name['Vibrator']['pending_amount'],
                               6000.0 / 14000.0 * 10000.0, places=2)
        self.assertAlmostEqual(by_name['Jack Hammer']['pending_amount'],
                               8000.0 / 14000.0 * 10000.0, places=2)

    def test_tool_position_page_renders_chain(self):
        vib = self._make_tool('TOOL-0001', 'Vibrator', 10)
        rental = self._rent([(vib, 10)], renter_type='internal', project=self.site_a)
        self.client.post(f'/hdc/tool-rental/{rental.id}/transfer', data={
            '_csrf_token': self._token(), 'to_type': 'site',
            'to_project_id': str(self.site_b.id),
            'transfer_date': _pkt_today().isoformat(),
        }, follow_redirects=True)

        r = self.client.get(f'/hdc/tool-rental/tool/{vib.id}')
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn('Where every piece is right now', html)
        self.assertIn('Site A', html)
        self.assertIn('Site B', html)
        self.assertIn('Movement history', html)
        self.assertIn('Balanced', html)

        missing = self.client.get('/hdc/tool-rental/tool/999999', follow_redirects=True)
        self.assertEqual(missing.status_code, 200)

    def test_json_feed_matches_html_numbers(self):
        vib = self._make_tool('TOOL-0001', 'Vibrator', 50)
        self._rent([(vib, 30)], renter_type='internal', project=self.site_a)
        self._rent([(vib, 5)], renter_type='external', customer_name='Ali Traders')

        r = self.client.get('/hdc/api/tool-rental/dashboard')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.get_data(as_text=True))
        self.assertEqual(data['reconciliation']['owned'], 50.0)
        self.assertEqual(data['reconciliation']['own_project'], 30.0)
        self.assertEqual(data['reconciliation']['customer'], 5.0)
        self.assertEqual(data['reconciliation']['in_store'], 15.0)
        self.assertTrue(data['reconciliation']['balanced'])
        self.assertEqual(len(data['tools']), 1)
        self.assertEqual(data['tools'][0]['utilization_pct'], 70.0)
        # same numbers as the service produces directly
        recon = tools_reconciliation(tool_ledger())
        self.assertEqual(data['reconciliation']['total_sent'], recon['total_sent'])

    def test_voided_rental_does_not_hold_tools(self):
        vib = self._make_tool('TOOL-0001', 'Vibrator', 10)
        rental = self._rent([(vib, 10)], renter_type='internal', project=self.site_a)
        rental.is_void = True
        rental.status = 'closed'
        db.session.commit()

        row = self._row(tool_ledger(), vib.id)
        self.assertEqual(row['in_store_qty'], 10.0)
        self.assertEqual(row['out_qty'], 0.0)
        self.assertEqual(row['holdings'], [])

    def test_no_charge_internal_rental_still_counts_as_sent(self):
        vib = self._make_tool('TOOL-0001', 'Vibrator', 10)
        self._rent([(vib, 10)], renter_type='internal', project=self.site_a,
                   billing='no_charge')
        ledger = tool_ledger()
        recon = tools_reconciliation(ledger)
        self.assertEqual(recon['own_project'], 10.0)
        self.assertEqual(recon['in_store'], 0.0)
        self.assertEqual(ledger['totals']['rental_value'], 0.0)

    def test_split_line_sits_in_two_places_and_return_comes_off_latest(self):
        """A partial transfer means the tool really is in two places at once."""
        prop = self._make_tool('TOOL-0001', 'Steel Prop', 100)
        rental = self._rent([(prop, 100)], renter_type='internal', project=self.site_a)
        item = rental.items[0]

        token = self._token()
        self.client.post(f'/hdc/tool-rental/{rental.id}/transfer', data={
            '_csrf_token': token, 'to_type': 'site',
            'to_project_id': str(self.site_b.id),
            'rental_item_id[]': [str(item.id)], 'qty_transfer[]': ['40'],
            'transfer_date': _pkt_today().isoformat(),
        }, follow_redirects=True)

        row = self._row(tool_ledger(), prop.id)
        self.assertEqual({h['label']: h['qty'] for h in row['holdings']},
                         {'Site A': 60.0, 'Site B': 40.0})
        self.assertEqual(row['own_project_qty'], 100.0)

        # returning 30 takes them off the most recent site (Site B) first
        self._return(rental, {prop.id: 30})
        row = self._row(tool_ledger(), prop.id)
        self.assertEqual({h['label']: h['qty'] for h in row['holdings']},
                         {'Site A': 60.0, 'Site B': 10.0})
        self.assertEqual(row['in_store_qty'], 30.0)
        self.assertTrue(tools_reconciliation(tool_ledger())['balanced'])

        # returning 70 more drains Site B then Site A, everything back in store
        self._return(rental, {prop.id: 70})
        row = self._row(tool_ledger(), prop.id)
        self.assertEqual(row['holdings'], [])
        self.assertEqual(row['in_store_qty'], 100.0)

    def test_rent_pending_is_not_double_counted_across_two_customers(self):
        vib = self._make_tool('TOOL-0001', 'Vibrator', 20)
        rental = self._rent([(vib, 20)], renter_type='external',
                            customer_name='Ali Traders', billing='per_day')
        rental.total_amount = 10_000.0
        rental.total_paid = 0.0
        db.session.commit()
        item = rental.items[0]

        self.client.post(f'/hdc/tool-rental/{rental.id}/transfer', data={
            '_csrf_token': self._token(), 'to_type': 'customer',
            'to_customer_name': 'Bilal Construction',
            'rental_item_id[]': [str(item.id)], 'qty_transfer[]': ['8'],
            'transfer_date': _pkt_today().isoformat(),
        }, follow_redirects=True)

        ledger = tool_ledger()
        row = self._row(ledger, vib.id)
        self.assertEqual({h['label']: h['qty'] for h in row['holdings']},
                         {'Ali Traders': 12.0, 'Bilal Construction': 8.0})
        shares = {h['label']: h['pending_amount'] for h in row['holdings']}
        self.assertAlmostEqual(sum(shares.values()), 10_000.0, places=1)
        self.assertAlmostEqual(shares['Ali Traders'], 6_000.0, places=1)
        self.assertAlmostEqual(shares['Bilal Construction'], 4_000.0, places=1)

        locations = {loc['label']: loc for loc in location_summary(ledger)}
        self.assertAlmostEqual(
            locations['Ali Traders']['pending_amount'] + locations['Bilal Construction']['pending_amount'],
            10_000.0, places=1)
        self.assertEqual(ledger['totals']['pending_amount'], 10_000.0)

    def test_existing_tool_pages_still_render(self):
        self._make_tool('TOOL-0001', 'Vibrator', 5)
        for url in ('/hdc/tool-rental', '/hdc/tool-rental/inventory',
                    '/hdc/tool-rental/tracking', '/hdc/tool-rental/reports'):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200, url)
            self.assertIn('tools-subnav', r.get_data(as_text=True), url)

    def test_tracking_layout_wraps_location_chains_and_keeps_modals_out_of_table(self):
        tool = self._make_tool('TOOL-0001', 'Concrete Vibrator', 3)
        self._rent([(tool, 1)], renter_type='external',
                   customer_name='Chand Contractor')

        response = self.client.get('/hdc/tool-rental/tracking')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        table_start = html.index(
            '<table class="table hdc-table table-sm mb-0 tool-tracking-table">')
        table_end = html.index('</table>', table_start)
        modal_start = html.index('<div class="modal fade tool-tracking-modal"')

        self.assertLess(table_end, modal_start)
        self.assertIn('tool-tracking-current', html)
        self.assertIn('tool-tracking-chain', html)
        self.assertIn('Chand Contractor', html)
        self.assertNotIn('Example: Site1 &gt; Site2', html)

    def test_tracking_groups_tools_by_location_and_only_expands_long_chain_on_view(self):
        vibrator = self._make_tool('TOOL-0001', 'Concrete Vibrator', 10)
        jackhammer = self._make_tool('TOOL-0002', 'Jack Hammer', 6)
        site_c = Project(name='Site C', project_code='P-C', client='Owner C',
                         location='Islamabad', contract_type='lump_sum',
                         owner_lump_sum=3_000_000)
        db.session.add(site_c)
        db.session.commit()
        rental = self._rent([(vibrator, 5), (jackhammer, 3)],
                            renter_type='internal', project=self.site_a)

        for destination in (self.site_b, site_c):
            response = self.client.post(f'/hdc/tool-rental/{rental.id}/transfer', data={
                '_csrf_token': self._token(), 'to_type': 'site',
                'to_project_id': str(destination.id), 'to_stage_id': '',
                'qty_transferred': '8',
                'transfer_date': _pkt_today().isoformat(),
            }, follow_redirects=True)
            self.assertEqual(response.status_code, 200)

        response = self.client.get('/hdc/tool-rental/tracking')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        table_start = html.index(
            '<table class="table hdc-table table-sm mb-0 tool-tracking-table">')
        table_end = html.index('</table>', table_start)
        summary_table = html[table_start:table_end]
        modal_start = html.index('<div class="modal fade tool-tracking-modal"')
        details = html[modal_start:]

        self.assertIn('Site C', summary_table)
        self.assertIn('Concrete Vibrator', summary_table)
        self.assertIn('Jack Hammer', summary_table)
        self.assertIn('2 tool types', summary_table)
        self.assertIn('data-bs-target="#detailModal', summary_table)
        # The list stays short; the complete route is only in the View dialog.
        self.assertNotIn('Site A', summary_table)
        self.assertIn('Site A', details)
        self.assertIn('Site B', details)
        self.assertIn('Site C', details)

        filtered = self.client.get(
            f'/hdc/tool-rental/tracking?project_id={site_c.id}')
        self.assertEqual(filtered.status_code, 200)
        filtered_html = filtered.get_data(as_text=True)
        filtered_start = filtered_html.index(
            '<table class="table hdc-table table-sm mb-0 tool-tracking-table">')
        filtered_end = filtered_html.index('</table>', filtered_start)
        filtered_summary = filtered_html[filtered_start:filtered_end]
        self.assertIn('Site C', filtered_summary)
        self.assertNotIn('Site B', filtered_summary)
        self.assertNotIn('Warehouse / Store', filtered_summary)

    def test_tracking_rolls_different_stages_up_to_one_site_row(self):
        site_c = Project(name='Site C', project_code='P-C', client='Owner C',
                         location='Islamabad', contract_type='lump_sum',
                         owner_lump_sum=3_000_000)
        db.session.add(site_c)
        db.session.flush()
        stage_a = Stage(project_id=site_c.id, name='Foundation',
                        contract_basis='Lump Sum', lump_sum_value=100_000)
        stage_b = Stage(project_id=site_c.id, name='Roof Work',
                        contract_basis='Lump Sum', lump_sum_value=100_000)
        db.session.add_all([stage_a, stage_b])
        db.session.commit()

        vibrator = self._make_tool('TOOL-0001', 'Concrete Vibrator', 10)
        jackhammer = self._make_tool('TOOL-0002', 'Jack Hammer', 6)
        self._rent([(vibrator, 4)], renter_type='internal',
                   project=site_c, stage=stage_a)
        self._rent([(jackhammer, 2)], renter_type='internal',
                   project=site_c, stage=stage_b)

        response = self.client.get('/hdc/tool-rental/tracking')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        table_start = html.index(
            '<table class="table hdc-table table-sm mb-0 tool-tracking-table">')
        table_end = html.index('</table>', table_start)
        summary = html[table_start:table_end]

        self.assertIn('Site C', summary)
        self.assertIn('2 tool types', summary)
        self.assertIn('2 stages', summary)
        self.assertIn('Concrete Vibrator', summary)
        self.assertIn('Jack Hammer', summary)

    def test_reports_group_each_site_or_customer_and_open_full_tool_chain_details(self):
        vibrator = self._make_tool('TOOL-0001', 'Concrete Vibrator', 10)
        jackhammer = self._make_tool('TOOL-0002', 'Jack Hammer', 6)
        drill = self._make_tool('TOOL-0003', 'Core Drill', 5)
        site_c = Project(name='Site C', project_code='P-C', client='Owner C',
                         location='Islamabad', contract_type='lump_sum',
                         owner_lump_sum=3_000_000)
        db.session.add(site_c)
        db.session.commit()

        internal = self._rent([(vibrator, 5), (jackhammer, 3)],
                              renter_type='internal', project=self.site_a)
        for destination in (self.site_b, site_c):
            response = self.client.post(f'/hdc/tool-rental/{internal.id}/transfer', data={
                '_csrf_token': self._token(), 'to_type': 'site',
                'to_project_id': str(destination.id), 'to_stage_id': '',
                'qty_transferred': '8',
                'transfer_date': _pkt_today().isoformat(),
            }, follow_redirects=True)
            self.assertEqual(response.status_code, 200)
        self._rent([(drill, 2)], renter_type='external', customer_name='Ali Traders')
        self._rent([(drill, 1)], renter_type='external', customer_name='Bilal Construction')

        response = self.client.get('/hdc/tool-rental/reports')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        summary_start = html.index(
            '<table class="table hdc-table table-sm mb-0 tool-report-location-table">')
        summary_end = html.index('</table>', summary_start)
        summary = html[summary_start:summary_end]
        modal_start = html.index('<div class="modal fade tool-report-location-modal"')
        details = html[modal_start:]

        # Internal site plus two distinct customers must remain three groups.
        self.assertEqual(html.count('class="modal fade tool-report-location-modal"'), 3)
        self.assertIn('Site A', summary)
        self.assertIn('Ali Traders', summary)
        self.assertIn('Bilal Construction', summary)
        self.assertIn('data-bs-target="#reportLocationModal', summary)
        self.assertNotIn('Site B', summary)
        self.assertIn('Concrete Vibrator', details)
        self.assertIn('Jack Hammer', details)
        self.assertIn('Site A', details)
        self.assertIn('Site B', details)
        self.assertIn('Site C', details)


if __name__ == '__main__':
    unittest.main(verbosity=2)
