#!/usr/bin/env python3
"""The HDC Tool Transaction form — one page: three groups, every transaction.

``/hdc/tool-rental/transaction`` records every equipment transaction from one
page, and every transaction sits under one of three groups — **Rent Out**
(rent out to a customer / issue to an own site / transfer to another site or
customer / receive rent / credit), **Return** (partial / full / to store /
damage-loss) and **Maintenance** (send / back).  These cases drive the form the
way the page posts (``txn_source=transaction`` + a generated
``txn_reference``) and assert what actually moved:

  1. the registry itself (pure data, no database) and its self-check;
  2. the rendered page: the three groups, every transaction, the page config,
     the panels;
  3. each transaction against an isolated app + database;
  4. the rules the task pins: no internal external-income, no payment-driven
     movement, partial vs full returns, unavailable stock refused, duplicate
     submissions handled, balance kept (owned = in_store + out);
  5. that the page script is data-driven — it names no transaction of its own.

Run with:
    HDC_BOOTSTRAP_ADMIN_PASSWORD='Admin@1234' \\
        python -m unittest tests.test_tool_txn_page -v
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

from flask import g, url_for                                      # noqa: E402

from hdc.app import create_app                                     # noqa: E402
from hdc.extensions import db                                      # noqa: E402
from hdc.models.accounts import Account, AccountTransaction        # noqa: E402
from hdc.models.projects import Project, Stage                     # noqa: E402
from hdc.models.tool_rental import (                               # noqa: E402
    Tool, ToolCategory, ToolRental, ToolRentalItem, ToolRentalPayment,
    ToolRentalReturn, ToolScrap, ToolSerial, ToolMovementLog,
)
from hdc.services.tool_rental import (                             # noqa: E402
    ensure_all_tools_serials, ensure_tool_serials,
)
from hdc.services.tool_txn_entry import (                          # noqa: E402
    MAINTENANCE_LABEL, new_txn_reference, txn_reference_seen,
)
from hdc.services.tool_txn_types import (                          # noqa: E402
    BILLING_MODES, DEFAULT_TXN_OPTION, KINDS, KIND_ADJUSTMENT, KIND_CREATE, KIND_DAMAGE_LOSS,
    KIND_MAINTENANCE_IN, KIND_MAINTENANCE_OUT, KIND_PAYMENT, KIND_RETURN,
    KIND_TRANSFER, MONEY_KINDS, PANEL_RENTOUT_SOURCE, PHYSICAL_KINDS, TXN_GROUPS, TXN_OPTIONS,
    registry_problems, txn_group, txn_option,
)
from hdc.services.tool_tracking import tool_ledger                 # noqa: E402
from hdc.utils.dates import _pkt_today                             # noqa: E402

ADMIN_PASSWORD = os.environ['HDC_BOOTSTRAP_ADMIN_PASSWORD']

#: The three groups the page offers, in tab order, with the transactions the
#: task requires under each of them.
REQUIRED_GROUPS = (
    ('rent_out', ('rent_out_customer', 'issue_own_site', 'transfer_site',
                  'transfer_customer', 'rental_payment', 'credit_adjustment')),
    ('return', ('partial_return', 'full_return', 'return_to_store',
                'damage_loss')),
    ('maintenance', ('maintenance_out', 'maintenance_in')),
)
REQUIRED_KEYS = tuple(key for _group, keys in REQUIRED_GROUPS for key in keys)

SCRIPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..',
                           'static', 'hdc', 'js', 'pages', 'tool_txn.js')


def _txn_script():
    with open(SCRIPT_PATH, encoding='utf-8') as handle:
        return handle.read()


def _js_object(script, name):
    """The body of one ``var NAME = { … };`` table in the page script."""
    match = re.search(r'var ' + name + r' = \{(.*?)\n    \};', script, re.S)
    if not match:
        raise AssertionError(f'{name} table missing from the page script')
    return match.group(1)


class TxnRegistryTests(unittest.TestCase):
    """Pure data: no app, no database."""

    def test_registry_is_exactly_three_groups_in_order(self):
        self.assertEqual([g['key'] for g in TXN_GROUPS],
                         [key for key, _keys in REQUIRED_GROUPS])
        for group, (key, keys) in zip(TXN_GROUPS, REQUIRED_GROUPS):
            self.assertEqual(key, group['key'])
            self.assertEqual([o['key'] for o in group['options']], list(keys))
            self.assertIs(txn_group(key), group)
            self.assertTrue(group['label'])
            self.assertTrue(group['icon'])
            self.assertTrue(group['help'])

    def test_every_transaction_sits_under_one_group(self):
        keys = [t['key'] for t in TXN_OPTIONS]
        self.assertEqual(keys, list(REQUIRED_KEYS))
        self.assertEqual(len(keys), len(set(keys)))
        for group in TXN_GROUPS:
            for entry in group['options']:
                self.assertEqual(entry['group'], group['key'], entry['key'])
                self.assertIs(txn_option(entry['key']), entry)

    def test_registry_checks_itself(self):
        self.assertEqual(registry_problems(), [])

    def test_every_type_declares_its_controller_contract(self):
        for entry in TXN_OPTIONS:
            self.assertIn(entry['kind'], KINDS, entry['key'])
            self.assertTrue(entry['panels'], entry['key'])
            self.assertTrue(entry['checks'], entry['key'])
            self.assertTrue(entry['submit_label'])
            self.assertTrue(entry['help'])
            self.assertTrue(entry['confirm'])
            self.assertTrue(entry['icon'])

    def test_rental_bound_types_pick_from_rentals_and_nothing_else_does(self):
        # A return no longer picks an issue first — it opens the holder's
        # sheet — so only the money entries still carry a rental picker.
        picks = [entry['key'] for entry in TXN_OPTIONS if entry['needs_rental']]
        self.assertEqual(sorted(picks), ['credit_adjustment', 'rental_payment'])
        for entry in TXN_OPTIONS:
            if entry['needs_rental']:
                self.assertIn(entry['kind'], (KIND_PAYMENT, KIND_ADJUSTMENT), entry['key'])
                self.assertTrue(entry['picker'], entry['key'])
                self.assertTrue(entry['eligible_for'], entry['key'])
            else:
                self.assertIsNone(entry['eligible_for'], entry['key'])
                self.assertIsNone(entry['picker'], entry['key'])

    def test_every_return_posts_the_rows_the_sheet_ticked(self):
        """A return's tools come from the sheet, never from the endpoint.

        The sheet spans every issue the holder still has, so "everything still
        out" cannot be inferred from one rental: each return option has to run
        the sheet collector, or a full return would post nothing at all.
        """
        for entry in TXN_OPTIONS:
            if entry['kind'] == KIND_RETURN:
                self.assertIn('return_holder', entry['checks'], entry['key'])
                self.assertIn('return_items', entry['checks'], entry['key'])

    def test_money_and_physical_kinds_are_disjoint(self):
        for entry in TXN_OPTIONS:
            in_money = entry['kind'] in MONEY_KINDS
            in_physical = entry['kind'] in PHYSICAL_KINDS
            self.assertNotEqual(in_money, in_physical, entry['key'])

    def test_lookup_returns_entry_or_none(self):
        self.assertEqual(txn_option('damage_loss')['kind'], KIND_DAMAGE_LOSS)
        self.assertIsNone(txn_option('nope'))

    def test_rentout_setup_panel_declared_on_movement_options_only(self):
        for entry in TXN_OPTIONS:
            if entry['kind'] in (KIND_CREATE, KIND_TRANSFER):
                self.assertIn(PANEL_RENTOUT_SOURCE, entry['panels'], entry['key'])
            else:
                self.assertNotIn(PANEL_RENTOUT_SOURCE, entry['panels'], entry['key'])


class TxnFormTestCase(unittest.TestCase):
    """One isolated app + database for the rendered page and its endpoints."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='hdc-tool-txn-')
        self.app = create_app({
            'HDC_DB_PATH': os.path.join(self.tmp, 'test.db'),
            'HDC_INSTANCE_DIR': self.tmp,
            'TESTING': True,
        })
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()
        self._login()

        self.site_a = Project(name='Site A', project_code='P-A', client='Owner A',
                              location='Karachi', contract_type='lump_sum',
                              owner_lump_sum=1_000_000)
        self.site_b = Project(name='Site B', project_code='P-B', client='Owner B',
                              location='Lahore', contract_type='lump_sum',
                              owner_lump_sum=1_000_000)
        db.session.add_all([self.site_a, self.site_b])
        db.session.flush()
        self.stage_a = Stage(project_id=self.site_a.id, name='Foundation',
                             contract_basis='Lump Sum', lump_sum_value=100_000)
        db.session.add(self.stage_a)
        self.cat = ToolCategory(name='Power Tools', active_status=True)
        db.session.add(self.cat)
        db.session.commit()
        self.bank = Account(name='Txn Bank', type='bank', status='active')
        db.session.add(self.bank)
        self.vibrator = self._make_tool('TOOL-1001', 'Vibrator', 6, 500.0)
        self.drill = self._make_tool('TOOL-1002', 'Drill', 4, 300.0)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ helpers
    def _login(self):
        page = self.client.get('/hdc/login')
        token = self._csrf(page.get_data(as_text=True))
        self.client.post('/hdc/login', data={
            'username': 'admin', 'password': ADMIN_PASSWORD, '_csrf_token': token,
        }, follow_redirects=True)

    @staticmethod
    def _csrf(html):
        match = re.search(r'name="_csrf_token" value="([^"]+)"', html or '')
        return match.group(1) if match else ''

    def _token(self):
        with self.client.session_transaction() as sess:
            return sess.get('_csrf_token', '')

    def _make_tool(self, code, name, qty, rate):
        tool = Tool(tool_code=code, name=name, unit='pcs', category_id=self.cat.id,
                    total_quantity=qty, purchase_cost=10_000.0,
                    rental_rate_per_day=rate, condition='good', status='active',
                    is_void=False)
        db.session.add(tool)
        db.session.commit()
        return tool

    def _post(self, url, data, follow=False):
        payload = dict(data)
        payload['_csrf_token'] = self._token()
        payload.setdefault('txn_source', 'transaction')
        return self.client.post(url, data=payload, follow_redirects=follow)

    def _txn(self, txn_type, extra, follow=True):
        """Post the way the transaction page does: page action, source + reference.

        The endpoint is taken from the registry entry — the same contract the
        page script uses — so a rental-bound transaction lands on its own
        route and a physical/financial one on its handler.
        """
        entry = txn_option(txn_type)
        self.assertIsNotNone(entry, txn_type)
        with self.app.test_request_context():
            if entry['needs_rental']:
                rental_id = extra.get('rental_id')
                self.assertIsNotNone(rental_id, f'{txn_type} needs a rental')
                url = url_for(entry['endpoint'], rental_id=int(rental_id))
            else:
                url = url_for(entry['endpoint'])
        data = {
            'txn_type': txn_type,
            'transaction_date': _pkt_today().isoformat(),
            'txn_reference': new_txn_reference(),
            'responsible_person': 'Office Test',
        }
        data.update(extra)
        return self._post(url, data, follow=follow)

    def _ledger_row(self, tool):
        rows = {int(r['tool_id']): r for r in tool_ledger()['tools']}
        return rows[int(tool.id)]

    def _rent(self, tool, qty, renter_type='internal', billing='per_day',
              customer=None, project=None, txn_type=None, follow=True):
        data = {
            'renter_type': renter_type,
            'billing_type': billing,
            'rental_date': _pkt_today().isoformat(),
            'tool_id[]': [str(tool.id)],
            'qty[]': [str(qty)],
            'rate[]': [str(tool.rental_rate_per_day)],
        }
        if renter_type == 'internal':
            data['project_id'] = str((project or self.site_a).id)
        else:
            data['customer_name'] = customer or 'Outside Customer'
        response = self._txn(txn_type or (
            'rent_out_customer' if renter_type == 'external' else 'issue_own_site'),
            data, follow=follow)
        return response

    def _rental_by_code(self, code_fragment):
        return (ToolRental.query
                .filter(ToolRental.rental_code.like(f'%{code_fragment}%'))
                .order_by(ToolRental.id.desc()).first())

    def _latest_rental(self):
        return ToolRental.query.order_by(ToolRental.id.desc()).first()

    def _return_qty(self, rental, item, qty, return_type='partial', reference=None,
                    payment=None):
        data = {
            'txn_type': 'partial_return',
            'txn_reference': reference or new_txn_reference(),
            'return_condition': return_type,
            'return_date': _pkt_today().isoformat(),
            'payment_condition': payment or 'no_payment',
            'rental_item_id[]': [str(item.id)],
            'qty_returned[]': [str(qty)],
            'condition_notes[]': [''],
        }
        return self._post(f'/hdc/tool-rental/{rental.id}/return', data, follow=True)

    # ------------------------------------------------------------ the page
    def test_return_starts_with_the_holder_and_lists_every_tool_it_holds(self):
        """No issue picker sits between the holder and their tools.

        The return sheet asks *who is returning* and then lists **everything**
        that site or customer still holds — across all of its open issues, with
        the issue on each row — so the tools come first and the operator never
        has to guess which issue a piece belongs to.
        """
        self._rent(self.vibrator, 2, billing='no_charge', project=self.site_a)
        rental = self._latest_rental()
        response = self.client.get('/hdc/tool-rental/transaction')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('Return from — client or project / site', html)
        self.assertIn('id="returnHolderSelect"', html)
        self.assertNotIn('returnRentalSelect', html)
        self.assertNotIn('Original issue / rental reference', html)
        self.assertLess(html.index('id="returnHolderSelect"'), html.index('id="returnItemRows"'))
        # The holder list comes from what is actually out, not from a hard-coded
        # pair of ids: the site this rental belongs to is offered by its key.
        self.assertIn(f'<option value="project:{self.site_a.id}">{self.site_a.name}</option>', html)
        config = json.loads(re.search(
            r'<script id="toolTxnConfig" type="application/json">(.*?)</script>', html, re.S).group(1))
        rows = [row for row in config['returnRows'] if row['rentalId'] == rental.id]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['holderKey'], f'project:{self.site_a.id}')
        self.assertEqual(rows[0]['toolName'], self.vibrator.name)
        self.assertEqual(rows[0]['issued'], 2.0)
        self.assertEqual(rows[0]['pending'], 2.0)
        self.assertEqual(len(rows[0]['serials']), 2)
        script = _txn_script()
        self.assertIn('return-tool-check', script)
        self.assertIn('return-rental-id', script)
        self.assertIn('return_rental_id[]', script)
        self.assertIn('Tools still at the selected site / customer', script)
        self.assertIn("toolCheck.checked ? String(pendingQty) : ''", script)

    def test_page_offers_three_groups_and_every_transaction(self):
        response = self.client.get('/hdc/tool-rental/transaction')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        for group in TXN_GROUPS:
            self.assertIn(f'data-txn-group="{group["key"]}"', html)
            self.assertIn(f'<optgroup label="{group["label"]}"', html)
            self.assertIn(group['label'], html)
        for entry in TXN_OPTIONS:
            self.assertIn(f'<option value="{entry["key"]}"', html)
            self.assertIn(entry['label'], html)
        # The page opens on the first group's first transaction.
        self.assertEqual(DEFAULT_TXN_OPTION, TXN_GROUPS[0]['options'][0]['key'])
        self.assertEqual(re.findall(r'<option value="([a-z_]+)"', html)[0],
                         DEFAULT_TXN_OPTION)

    def test_group_cards_open_a_shared_modal_and_rent_out_hides_its_selector(self):
        html = self.client.get('/hdc/tool-rental/transaction').get_data(as_text=True)
        for group in TXN_GROUPS:
            self.assertIn(f'data-txn-group="{group["key"]}"', html)
            self.assertIn('data-bs-target="#toolTxnModal"', html)
        self.assertIn('id="toolTxnModal"', html)
        self.assertIn('class="col-md-6 d-none" id="txnTypeField"', html)
        self.assertIn('id="txnType"', html)
        self.assertIn('data-bs-toggle="modal"', html)
        script = _txn_script()
        self.assertIn("entry.group !== 'rent_out'", script)

    def test_every_option_sits_in_its_group_optgroup(self):
        html = self.client.get('/hdc/tool-rental/transaction').get_data(as_text=True)
        for group in TXN_GROUPS:
            block = re.search(
                r'<optgroup label="' + re.escape(group['label']) + r'".*?</optgroup>',
                html, re.S)
            self.assertIsNotNone(block, group['key'])
            keys = re.findall(r'<option value="([a-z_]+)"', block.group(0))
            self.assertEqual(keys, [o['key'] for o in group['options']])

    def test_page_config_carries_groups_panels_and_reference(self):
        html = self.client.get('/hdc/tool-rental/transaction').get_data(as_text=True)
        match = re.search(
            r'<script id="toolTxnConfig" type="application/json">(.*?)</script>',
            html, re.S)
        self.assertIsNotNone(match, 'page config JSON missing')
        config = json.loads(match.group(1))
        self.assertEqual([g['key'] for g in config['groups']],
                         [g['key'] for g in TXN_GROUPS])
        self.assertEqual(
            [o['key'] for g in config['groups'] for o in g['options']],
            list(REQUIRED_KEYS))
        self.assertTrue(config['txnReference'].startswith('HDC-TX-'))
        self.assertEqual(config['storeLabel'], 'Warehouse / Store')
        self.assertEqual(config['maintenanceLabel'], MAINTENANCE_LABEL)
        # Every option reaches the page with the contract the script reads.
        for group in config['groups']:
            for option in group['options']:
                self.assertTrue(option['panels'], option['key'])
                self.assertTrue(option['checks'], option['key'])
                self.assertEqual(option['group'], group['key'])
                if option['needs_rental']:
                    self.assertIn('__RENTAL_ID__', option['action'], option['key'])
                else:
                    self.assertNotIn('__RENTAL_ID__', option['action'], option['key'])
        self.assertIn('data-txn-panels="lines"', html)
        self.assertIn('data-rental-picker="' + txn_option('rental_payment')['picker'] + '"', html)
        self.assertIn('id="toolTxnForm"', html)

    def test_billing_registry_renders_status_and_cash_controls(self):
        html = self.client.get('/hdc/tool-rental/transaction').get_data(as_text=True)
        config = json.loads(re.search(r'<script id="toolTxnConfig" type="application/json">(.*?)</script>',
                                      html, re.S).group(1))
        self.assertEqual(config['billingModes'], list(BILLING_MODES))
        for mode in BILLING_MODES:
            self.assertIn(f'<option value="{mode["key"]}">{mode["label"]}</option>', html)
        self.assertIn('id="billingStatus"', html)
        self.assertIn('id="returnSettlementStatus"', html)
        self.assertIn('id="returnCashFields"', html)
        self.assertIn('id="transferSettlementStatus"', html)
        self.assertIn('tool-txn-sheet', _txn_script())

    def test_selected_site_groups_holders_with_sheet_lines_and_status(self):
        from hdc.services.tool_txn_entry import txn_form_context
        self._rent(self.vibrator, 2, billing='no_charge', project=self.site_a)
        free = self._latest_rental()
        self._rent(self.drill, 1, billing='per_day', project=self.site_a)
        paid = self._latest_rental()
        context = txn_form_context()
        site = [s for s in context['transfer_sources'] if s['rental_id'] in (free.id, paid.id)]
        self.assertEqual(len(site), 2)
        self.assertEqual(site[0]['location_key'], site[1]['location_key'])
        options = [o for o in context['transfer_source_options'] if o[0] in {s['key'] for s in site}]
        self.assertEqual(len(options), 1)
        self.assertIn('2 rental(s)', options[0][1])
        html = self.client.get('/hdc/tool-rental/transaction').get_data(as_text=True)
        config = json.loads(re.search(r'<script id="toolTxnConfig" type="application/json">(.*?)</script>',
                                      html, re.S).group(1))
        source_rows = [s for s in config['transferSources'] if s['rentalId'] in (free.id, paid.id)]
        self.assertEqual(len(source_rows), 2)
        self.assertEqual({s['billingType'] for s in source_rows}, {'no_charge', 'per_day'})
        self.assertEqual([s['amountDue'] for s in source_rows if s['rentalId'] == free.id], [0.0])
        self.assertTrue(all(s['lines'] for s in source_rows))
        self.assertIn('No charge · no rent due', html)

    def test_free_return_records_no_payment_even_with_stale_cash_form(self):
        self._rent(self.vibrator, 1, renter_type='external', billing='no_charge')
        rental = self._latest_rental()
        item = ToolRentalItem.query.filter_by(rental_id=rental.id).first()
        self._return_qty(rental, item, 1, payment='full')
        ret = ToolRentalReturn.query.filter_by(rental_id=rental.id).first()
        self.assertEqual(ret.payment_type, 'no_payment')
        self.assertEqual(float(ret.amount_paid), 0)
        self.assertEqual(ToolRentalPayment.query.count(), 0)
        self.assertEqual(AccountTransaction.query.count(), 0)

    def test_no_charge_transfer_has_no_cash_settlement_or_receivable(self):
        self._rent(self.vibrator, 1, billing='no_charge', project=self.site_a)
        rental = self._latest_rental()
        item = ToolRentalItem.query.filter_by(rental_id=rental.id).first()
        source_key = f'{rental.id}|own_project|{self.site_a.id}|0|'
        self._txn('transfer_site', {
            'project_id': str(self.site_b.id), 'billing_type': 'no_charge',
            'source_rental_id[]': [source_key],
            'transfer_selection_enabled': '1',
            'transfer_item_id[]': [str(item.id)],
            f'transfer_qty_{item.id}': '1',
            'settle_choice': 'pay_now',  # stale cash selection cannot charge a free rental
        })
        moved = self._latest_rental()
        self.assertNotEqual(moved.id, rental.id)
        self.assertEqual(moved.payment_status, 'no_charge')
        self.assertEqual(float(moved.total_pending_amount), 0)
        self.assertEqual(ToolRentalPayment.query.count(), 0)
        self.assertEqual(AccountTransaction.query.count(), 0)

    def test_transfer_collect_now_requires_explicit_account(self):
        self._rent(self.vibrator, 2, renter_type='external', customer='Paying customer')
        rental = self._latest_rental()
        item = ToolRentalItem.query.filter_by(rental_id=rental.id).first()
        source_key = f'{rental.id}|customer|0|0|Paying%20customer'
        response = self._txn('transfer_site', {
            'project_id': str(self.site_b.id), 'billing_type': 'per_day',
            'source_rental_id[]': [source_key],
            'transfer_selection_enabled': '1',
            'transfer_item_id[]': [str(item.id)],
            f'transfer_qty_{item.id}': '2',
            'settle_choice': 'pay_now',
        })
        self.assertIn('Choose a Cash/Bank account', response.get_data(as_text=True))
        self.assertEqual(ToolRental.query.count(), 1)
        self.assertEqual(ToolRentalPayment.query.count(), 0)

    def test_every_declared_panel_has_markup(self):
        html = self.client.get('/hdc/tool-rental/transaction').get_data(as_text=True)
        panels = {panel for entry in TXN_OPTIONS for panel in entry['panels']}
        for panel in sorted(panels):
            self.assertIn(f'data-txn-panels="{panel}"', html, panel)

    def test_rent_out_setup_renders_from_and_to_choices(self):
        html = self.client.get('/hdc/tool-rental/transaction').get_data(as_text=True)
        self.assertIn('data-txn-panels="rentout_source"', html)
        self.assertIn('name="rentout_from" value="store"', html)
        self.assertIn('name="rentout_from" value="site"', html)
        self.assertIn('name="rentout_from" value="customer"', html)
        self.assertIn('name="rentout_to" value="internal"', html)
        self.assertIn('name="rentout_to" value="external"', html)
        self.assertIn('id="storeToolSearch"', html)
        self.assertIn('id="storeToolList"', html)
        self.assertIn('id="storeSelectedRows"', html)
        self.assertIn('id="transferLinesSearch"', html)

    def test_rent_out_panels_follow_the_setup_order(self):
        html = self.client.get('/hdc/tool-rental/transaction').get_data(as_text=True)
        order = ['data-txn-panels="rentout_source"', 'data-txn-panels="lines"',
                 'data-txn-panels="transfer_source"', 'data-txn-panels="destination"',
                 'data-txn-panels="terms"']
        positions = [html.index(marker) for marker in order]
        self.assertEqual(positions, sorted(positions))

    def test_rent_type_offers_full_rent_and_no_rent(self):
        html = self.client.get('/hdc/tool-rental/transaction').get_data(as_text=True)
        self.assertIn('<option value="per_day">Full rent — per day</option>', html)
        self.assertIn('<option value="no_charge">No rent (adjusted in contract)</option>', html)

    def test_store_tools_config_carries_rates_and_availability(self):
        html = self.client.get('/hdc/tool-rental/transaction').get_data(as_text=True)
        config = json.loads(re.search(
            r'<script id="toolTxnConfig" type="application/json">(.*?)</script>',
            html, re.S).group(1))
        tools = {t['name']: t for t in config['tools']}
        self.assertIn('Vibrator', tools)
        vibrator = tools['Vibrator']
        self.assertEqual(vibrator['code'], 'TOOL-1001')
        self.assertEqual(vibrator['availableQty'], 6)
        self.assertEqual(vibrator['rate'], 500.0)
        self.assertIn('serials', vibrator)

    def test_transfer_sources_carry_location_types_for_the_from_filter(self):
        self._rent(self.vibrator, 2, renter_type='internal', project=self.site_a)
        self._rent(self.drill, 1, renter_type='external', customer='Filter Customer')
        html = self.client.get('/hdc/tool-rental/transaction').get_data(as_text=True)
        config = json.loads(re.search(
            r'<script id="toolTxnConfig" type="application/json">(.*?)</script>',
            html, re.S).group(1))
        sources = config['transferSources']
        self.assertTrue(sources)
        for source in sources:
            self.assertTrue(source['key'])
            self.assertTrue(source['locationKey'])
            self.assertTrue(source['locType'])
            self.assertTrue(source['lines'])
        loc_types = {s['locType'] for s in sources}
        self.assertIn('own_project', loc_types)
        self.assertIn('customer', loc_types)

    def test_page_script_implements_every_declared_check(self):
        """The registry may only ask for checks the script actually has."""
        script = _txn_script()
        checks = set(re.findall(r'^\s+(\w+):', _js_object(script, 'CHECKS'), re.M))
        collectors = set(re.findall(r'^\s+(\w+):', _js_object(script, 'COLLECTORS'), re.M))
        declared = {check for entry in TXN_OPTIONS for check in entry['checks']}
        self.assertTrue(declared, 'no transaction declares a check')
        self.assertEqual(declared - checks, set(), 'declared but unimplemented checks')
        self.assertEqual(collectors - checks, set(), 'collectors without a check')

    def test_page_script_names_no_transaction_of_its_own(self):
        """Data-driven only: the script reads the config, it does not know
        which transactions exist."""
        script = _txn_script()
        for entry in TXN_OPTIONS:
            self.assertNotIn(entry['key'], script, entry['key'])
        for panel in {panel for entry in TXN_OPTIONS for panel in entry['panels']}:
            self.assertNotIn(f"'{panel}'", script, panel)

    def test_page_is_linked_from_the_tools_subnav_and_dashboard(self):
        nav = self.client.get('/hdc/tool-rental/dashboard').get_data(as_text=True)
        self.assertIn('/hdc/tool-rental/transaction', nav)
        self.assertIn('New Transaction', nav)

    def test_legacy_new_rental_page_still_works(self):
        html = self.client.get('/hdc/tool-rental/new').get_data(as_text=True)
        self.assertEqual(html.count('id="txnType"'), 1)
        self.assertIn('type="hidden" name="txn_type" id="txnType" value="new_rental"', html)
        self.assertIn('id="createRentalForm"', html)

    def test_existing_tools_tabs_still_render(self):
        for url in ('/hdc/tool-rental', '/hdc/tool-rental/dashboard',
                    '/hdc/tool-rental/inventory', '/hdc/tool-rental/new',
                    '/hdc/tool-rental/transaction'):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)
            body = response.get_data(as_text=True)
            self.assertEqual(body.count('</html>'), 1, url)

    # ------------------------------------------------ physical transactions
    def test_rent_out_to_customer_issues_and_records_the_holder(self):
        self._rent(self.vibrator, 4, renter_type='external', customer='Sky Builders')
        rental = self._latest_rental()
        self.assertEqual(rental.renter_type, 'external')
        self.assertEqual(rental.customer_name, 'Sky Builders')
        item = ToolRentalItem.query.filter_by(rental_id=rental.id).first()
        self.assertAlmostEqual(float(item.qty_pending), 4.0)
        self.assertAlmostEqual(self._ledger_row(self.vibrator)['out_qty'], 4.0)
        log = (ToolMovementLog.query.filter_by(rental_id=rental.id)
               .order_by(ToolMovementLog.id.desc()).first())
        self.assertEqual(log.movement_type, 'rental_out')

    def test_issue_to_own_site_creates_no_external_income(self):
        ledger_before = self._ledger_row(self.vibrator)
        self._rent(self.vibrator, 3, renter_type='internal', billing='per_day')
        rental = self._latest_rental()
        self.assertEqual(rental.renter_type, 'internal')
        # The physical move happened...
        ledger_after = self._ledger_row(self.vibrator)
        self.assertAlmostEqual(ledger_after['own_project_qty'],
                               ledger_before['own_project_qty'] + 3.0)
        self.assertAlmostEqual(ledger_after['in_store_qty'],
                               ledger_before['in_store_qty'] - 3.0)
        # ...and no money was raised behind the operator's back.
        self.assertEqual(ToolRentalPayment.query.count(), 0)
        self.assertEqual(AccountTransaction.query.count(), 0)

    def test_transfer_between_sites_moves_the_recorded_location(self):
        self._rent(self.vibrator, 4, renter_type='internal', project=self.site_a)
        rental = self._latest_rental()
        item = ToolRentalItem.query.filter_by(rental_id=rental.id).first()
        source_key = f'{rental.id}|own_project|{self.site_a.id}|0|'
        response = self._txn('transfer_site', {
            'renter_type': 'internal',
            'project_id': str(self.site_b.id),
            'billing_type': 'per_day',
            'rental_date': _pkt_today().isoformat(),
            'transfer_selection_enabled': '1',
            'source_rental_id[]': [source_key],
            'transfer_item_id[]': [str(item.id)],
            f'transfer_qty_{item.id}': '4',
            'settle_choice': 'add_credit',
        })
        self.assertEqual(response.status_code, 200)
        row = self._ledger_row(self.vibrator)
        self.assertAlmostEqual(row['own_project_qty'], 4.0)
        labels = {h['label'] for h in row['holdings']}
        self.assertTrue(any('Site B' in label for label in labels), labels)
        new_rental = self._latest_rental()
        self.assertNotEqual(new_rental.id, rental.id)
        self.assertEqual(new_rental.project_id, self.site_b.id)

    def test_transfer_between_customers_records_the_new_holder(self):
        self._rent(self.vibrator, 2, renter_type='external', customer='First Customer')
        rental = self._latest_rental()
        item = ToolRentalItem.query.filter_by(rental_id=rental.id).first()
        source_key = f'{rental.id}|customer|0|0|First%20Customer'
        self._txn('transfer_customer', {
            'renter_type': 'external',
            'customer_name': 'Second Customer',
            'billing_type': 'per_day',
            'rental_date': _pkt_today().isoformat(),
            'transfer_selection_enabled': '1',
            'source_rental_id[]': [source_key],
            'transfer_item_id[]': [str(item.id)],
            f'transfer_qty_{item.id}': '2',
            'settle_choice': 'add_credit',
        })
        new_rental = self._latest_rental()
        self.assertEqual(new_rental.customer_name, 'Second Customer')
        self.assertAlmostEqual(self._ledger_row(self.vibrator)['customer_qty'], 2.0)

    def test_transfer_from_site_to_customer_crosses_holder_types(self):
        self._rent(self.vibrator, 2, renter_type='internal', project=self.site_a)
        rental = self._latest_rental()
        item = ToolRentalItem.query.filter_by(rental_id=rental.id).first()
        source_key = f'{rental.id}|own_project|{self.site_a.id}|0|'
        self._txn('transfer_customer', {
            'customer_name': 'Second Customer',
            'billing_type': 'per_day',
            'rental_date': _pkt_today().isoformat(),
            'transfer_selection_enabled': '1',
            'source_rental_id[]': [source_key],
            'transfer_item_id[]': [str(item.id)],
            f'transfer_qty_{item.id}': '2',
            'settle_choice': 'add_credit',
        })
        new_rental = self._latest_rental()
        self.assertNotEqual(new_rental.id, rental.id)
        self.assertEqual(new_rental.renter_type, 'external')
        self.assertEqual(new_rental.customer_name, 'Second Customer')
        self.assertAlmostEqual(self._ledger_row(self.vibrator)['customer_qty'], 2.0)

    def test_transfer_from_customer_to_site_crosses_holder_types(self):
        self._rent(self.vibrator, 2, renter_type='external', customer='First Customer')
        rental = self._latest_rental()
        item = ToolRentalItem.query.filter_by(rental_id=rental.id).first()
        source_key = f'{rental.id}|customer|0|0|First%20Customer'
        self._txn('transfer_site', {
            'project_id': str(self.site_b.id),
            'billing_type': 'per_day',
            'rental_date': _pkt_today().isoformat(),
            'transfer_selection_enabled': '1',
            'source_rental_id[]': [source_key],
            'transfer_item_id[]': [str(item.id)],
            f'transfer_qty_{item.id}': '2',
            'settle_choice': 'add_credit',
        })
        new_rental = self._latest_rental()
        self.assertNotEqual(new_rental.id, rental.id)
        self.assertEqual(new_rental.renter_type, 'internal')
        self.assertEqual(new_rental.project_id, self.site_b.id)
        self.assertAlmostEqual(self._ledger_row(self.vibrator)['customer_qty'], 0.0)
        self.assertAlmostEqual(self._ledger_row(self.vibrator)['own_project_qty'], 2.0)

    def test_partial_return_keeps_the_remaining_outstanding(self):
        self._rent(self.vibrator, 5, renter_type='external', customer='Part Return Co')
        rental = self._latest_rental()
        item = ToolRentalItem.query.filter_by(rental_id=rental.id).first()
        self._return_qty(rental, item, 2)
        db.session.expire_all()
        item = ToolRentalItem.query.get(item.id)
        self.assertAlmostEqual(float(item.qty_pending), 3.0)
        self.assertAlmostEqual(float(item.qty_returned), 2.0)
        self.assertAlmostEqual(self._ledger_row(self.vibrator)['out_qty'], 3.0)
        self.assertAlmostEqual(self._ledger_row(self.vibrator)['in_store_qty'], 3.0)

    def test_full_return_closes_the_physical_issue(self):
        self._rent(self.vibrator, 4, renter_type='internal')
        rental = self._latest_rental()
        item = ToolRentalItem.query.filter_by(rental_id=rental.id).first()
        self._return_qty(rental, item, 0, return_type='full')
        db.session.expire_all()
        item = ToolRentalItem.query.get(item.id)
        self.assertAlmostEqual(float(item.qty_pending), 0.0)
        self.assertAlmostEqual(self._ledger_row(self.vibrator)['out_qty'], 0.0)
        self.assertAlmostEqual(self._ledger_row(self.vibrator)['in_store_qty'], 6.0)

    def test_return_to_store_posts_no_payment(self):
        self._rent(self.vibrator, 3, renter_type='external', customer='Store Return Co')
        rental = self._latest_rental()
        item = ToolRentalItem.query.filter_by(rental_id=rental.id).first()
        self._return_qty(rental, item, 3, return_type='full')
        self.assertEqual(ToolRentalPayment.query.count(), 0)
        self.assertAlmostEqual(self._ledger_row(self.vibrator)['in_store_qty'], 6.0)

    # ---------------------------------------------- the holder return sheet
    def _return_sheet_post(self, lines, **extra):
        """Post the return sheet the way the page posts it: one row per tool.

        ``lines`` are ``(rental_id, item, qty)`` triples — the same three
        values each sheet row carries (its issue, its tool line, the quantity
        coming back).
        """
        data = {
            'return_condition': 'partial',
            'payment_condition': 'no_payment',
            'return_date': _pkt_today().isoformat(),
            'return_rental_id[]': [str(rental_id) for rental_id, _item, _qty in lines],
            'rental_item_id[]': [str(item.id) for _rental_id, item, _qty in lines],
            'qty_returned[]': [str(qty) for _rental_id, _item, qty in lines],
            'condition_notes[]': [''] * len(lines),
        }
        data.update(extra)
        return self._txn('partial_return', data)

    def _rent_for_site(self, tool, qty, billing='no_charge'):
        """One more issue for the same site, created the way the page does."""
        self._rent(tool, qty, renter_type='internal', billing=billing, project=self.site_a)
        rental = self._latest_rental()
        return rental, ToolRentalItem.query.filter_by(rental_id=rental.id).first()

    def test_holder_return_closes_several_issues_in_one_submission(self):
        """A site holding three issues is one place — and one submission.

        The sheet lists every tool the holder still has out, each row carrying
        its issue, so the truck is loaded once and each issue still gets its
        own return record (the audit trail the rental page reads).
        """
        first, item_a = self._rent_for_site(self.vibrator, 2)
        second, item_b = self._rent_for_site(self.drill, 1)
        third, item_c = self._rent_for_site(self.vibrator, 1)
        response = self._return_sheet_post([
            (first.id, item_a, 2), (second.id, item_b, 1), (third.id, item_c, 1),
        ])
        self.assertEqual(response.status_code, 200)
        db.session.expire_all()
        for item in (item_a, item_b, item_c):
            self.assertAlmostEqual(float(db.session.get(ToolRentalItem, item.id).qty_pending), 0.0)
        self.assertEqual(ToolRentalReturn.query.count(), 3)
        self.assertEqual({ret.rental_id for ret in ToolRentalReturn.query.all()},
                         {first.id, second.id, third.id})
        self.assertAlmostEqual(self._ledger_row(self.vibrator)['in_store_qty'], 6.0)
        self.assertAlmostEqual(self._ledger_row(self.vibrator)['out_qty'], 0.0)

    def test_holder_return_full_takes_everything_still_out(self):
        # Full Return means the whole load, even when only one row was left
        # ticked — the sheet posts the rows it lists, the flag decides the rest.
        first, item_a = self._rent_for_site(self.vibrator, 2)
        second, item_b = self._rent_for_site(self.vibrator, 1)
        self._return_sheet_post([(first.id, item_a, 0), (second.id, item_b, 0)],
                                return_condition='full')
        db.session.expire_all()
        self.assertAlmostEqual(float(db.session.get(ToolRentalItem, item_a.id).qty_pending), 0.0)
        self.assertAlmostEqual(float(db.session.get(ToolRentalItem, item_b.id).qty_pending), 0.0)
        self.assertAlmostEqual(self._ledger_row(self.vibrator)['in_store_qty'], 6.0)
        self.assertEqual(ToolRentalReturn.query.count(), 2)
        self.assertTrue(all(ret.return_type == 'full' for ret in ToolRentalReturn.query.all()))

    def test_holder_return_refuses_more_than_is_still_out_and_writes_nothing(self):
        first, item_a = self._rent_for_site(self.vibrator, 2)
        second, item_b = self._rent_for_site(self.drill, 1)
        response = self._return_sheet_post([(first.id, item_a, 1), (second.id, item_b, 9)])
        self.assertIn('only 1 is still out', response.get_data(as_text=True))
        self.assertEqual(ToolRentalReturn.query.count(), 0,
                         'a refused line writes nothing at all')
        db.session.expire_all()
        self.assertAlmostEqual(float(db.session.get(ToolRentalItem, item_a.id).qty_pending), 2.0)
        self.assertAlmostEqual(float(db.session.get(ToolRentalItem, item_b.id).qty_pending), 1.0)

    def test_holder_return_collects_rent_against_one_issue_only(self):
        """The sheet has one money answer for one issue.

        Rent is collected against the issue the operator names, while the tools
        come back from every issue ticked — cash and discount land on that
        issue's record and nowhere else.
        """
        self._rent(self.vibrator, 2, renter_type='external', customer='Mixed Load Co')
        first = self._latest_rental()
        item_a = ToolRentalItem.query.filter_by(rental_id=first.id).first()
        self._rent(self.drill, 1, renter_type='external', customer='Mixed Load Co')
        second = self._latest_rental()
        item_b = ToolRentalItem.query.filter_by(rental_id=second.id).first()
        due_a = float(first.total_pending_amount)
        due_b = float(second.total_pending_amount)
        self.assertGreater(due_a, 0)
        self._return_sheet_post(
            [(first.id, item_a, 2), (second.id, item_b, 1)],
            payment_condition='partial', amount_paid='100',
            settle_rental_id=str(first.id),
            received_to_account_id=str(self.bank.id),
            payment_mode='bank', payment_reference='SHEET-1',
            discount_amount='0')
        payments = ToolRentalPayment.query.all()
        self.assertEqual(len(payments), 1)
        self.assertEqual(payments[0].rental_id, first.id)
        self.assertAlmostEqual(float(payments[0].amount), 100.0)
        self.assertEqual(payments[0].received_to_account_id, self.bank.id)
        self.assertEqual(AccountTransaction.query.count(), 1)
        db.session.expire_all()
        self.assertAlmostEqual(float(db.session.get(ToolRental, first.id).total_pending_amount),
                               due_a - 100.0)
        self.assertAlmostEqual(float(db.session.get(ToolRental, second.id).total_pending_amount), due_b)

    def test_holder_return_never_charges_a_no_charge_issue(self):
        first, item_a = self._rent_for_site(self.vibrator, 2, billing='no_charge')
        second, item_b = self._rent_for_site(self.drill, 1, billing='per_day')
        self._return_sheet_post(
            [(first.id, item_a, 2), (second.id, item_b, 1)],
            payment_condition='full', amount_paid='500',
            settle_rental_id=str(first.id), received_to_account_id=str(self.bank.id))
        self.assertEqual(ToolRentalPayment.query.count(), 0)
        self.assertEqual(AccountTransaction.query.count(), 0)
        db.session.expire_all()
        self.assertAlmostEqual(float(db.session.get(ToolRental, second.id).total_pending_amount),
                               float(second.total_pending_amount))

    # ------------------------------------------------------ money transactions
    def test_rental_payment_updates_balance_and_moves_no_tool(self):
        self._rent(self.drill, 2, renter_type='external', customer='Paying Customer')
        rental = self._latest_rental()
        before = self._ledger_row(self.drill)
        due = float(rental.total_pending_amount)
        self.assertGreater(due, 0)
        reference = new_txn_reference()
        self._txn('rental_payment', {
            'rental_id': str(rental.id),
            'amount': f'{due:.2f}',
            'payment_date': _pkt_today().isoformat(),
            'payment_mode': 'bank',
            'received_to_account_id': str(self.bank.id),
            'reference': 'SLIP-1',
            'txn_reference': reference,
        })
        db.session.expire_all()
        rental = ToolRental.query.get(rental.id)
        self.assertAlmostEqual(float(rental.total_pending_amount), 0.0, places=2)
        self.assertEqual(ToolRentalPayment.query.count(), 1)
        self.assertTrue(
            AccountTransaction.query.filter(
                AccountTransaction.source_type.like('tool_rental_payment%')).count() >= 1)
        after = self._ledger_row(self.drill)
        self.assertAlmostEqual(after['out_qty'], before['out_qty'])
        self.assertAlmostEqual(after['in_store_qty'], before['in_store_qty'])

    def test_credit_adjustment_reduces_outstanding_without_cash(self):
        self._rent(self.drill, 2, renter_type='external', customer='Credit Customer')
        rental = self._latest_rental()
        due = float(rental.total_pending_amount)
        self._txn('credit_adjustment', {
            'rental_id': str(rental.id),
            'amount': f'{due:.2f}',
            'discount_date': _pkt_today().isoformat(),
            'reason': 'goodwill',
            'notes': 'Waived off the balance',
        })
        db.session.expire_all()
        rental = ToolRental.query.get(rental.id)
        self.assertAlmostEqual(float(rental.total_pending_amount), 0.0, places=2)
        self.assertEqual(ToolRentalPayment.query.count(), 0)

    # ------------------------------------------------- damage / loss / service
    def test_damage_loss_from_store_writes_off_stock(self):
        self._txn('damage_loss', {
            'damage_source': 'store',
            'tool_id': str(self.vibrator.id),
            'qty': '2',
            'damage_reason': 'damaged',
            'notes': 'Dropped off the truck',
        })
        db.session.expire_all()
        tool = Tool.query.get(self.vibrator.id)
        self.assertAlmostEqual(float(tool.total_quantity), 4.0)
        self.assertEqual(ToolScrap.query.count(), 1)
        log = (ToolMovementLog.query.filter_by(tool_id=tool.id,
                                               movement_type='scrap_out')
               .order_by(ToolMovementLog.id.desc()).first())
        self.assertIsNotNone(log)

    def test_damage_loss_from_a_holder_keeps_the_ledger_balanced(self):
        self._rent(self.vibrator, 4, renter_type='external', customer='Rough Site Co')
        rental = self._latest_rental()
        item = ToolRentalItem.query.filter_by(rental_id=rental.id).first()
        self._txn('damage_loss', {
            'damage_source': 'rental',
            'rental_id': str(rental.id),
            'rental_item_id': str(item.id),
            'qty': '1',
            'damage_reason': 'lost',
        })
        db.session.expire_all()
        tool = Tool.query.get(self.vibrator.id)
        item = ToolRentalItem.query.get(item.id)
        self.assertAlmostEqual(float(tool.total_quantity), 5.0)
        self.assertAlmostEqual(float(item.qty_pending), 3.0)
        row = self._ledger_row(tool)
        self.assertAlmostEqual(row['owned_qty'], 5.0)
        self.assertAlmostEqual(row['out_qty'], 3.0)
        self.assertAlmostEqual(row['in_store_qty'], 2.0)
        self.assertFalse(row['unaccounted'])
        self.assertAlmostEqual(row['variance'], 0.0)
        self.assertEqual(ToolScrap.query.count(), 1)

    def test_send_and_return_from_maintenance_keeps_ownership(self):
        ensure_tool_serials(tool=self.vibrator, commit=True)
        pieces = [s.id for s in self.vibrator.in_store_serials][:2]
        self.assertEqual(len(pieces), 2, 'fixture must have serial markings')
        self._txn('maintenance_out', {
            'tool_id': str(self.vibrator.id),
            'maintenance_serial_ids[]': [str(p) for p in pieces],
            'maintenance_reason': 'Service',
        })
        db.session.expire_all()
        tool = Tool.query.get(self.vibrator.id)
        self.assertAlmostEqual(float(tool.total_quantity), 6.0)
        sent = ToolSerial.query.filter(ToolSerial.id.in_(pieces)).all()
        self.assertTrue(all(s.status == 'maintenance' for s in sent))
        self.assertTrue(all(s.current_location_label == MAINTENANCE_LABEL for s in sent))
        self.assertTrue(all(s.is_in_store for s in sent))

        self._txn('maintenance_in', {
            'maintenance_serial_ids[]': [str(p) for p in pieces],
            'maintenance_condition': 'good',
        })
        db.session.expire_all()
        back = ToolSerial.query.filter(ToolSerial.id.in_(pieces)).all()
        self.assertTrue(all(s.status == 'in_store' for s in back))
        self.assertTrue(all(s.condition == 'good' for s in back))
        self.assertAlmostEqual(
            float(Tool.query.get(self.vibrator.id).total_quantity), 6.0)

    # ------------------------------------------------------------- validation
    def test_over_return_is_rejected_without_writing_anything(self):
        self._rent(self.vibrator, 3, renter_type='external', customer='Over Return Co')
        rental = self._latest_rental()
        item = ToolRentalItem.query.filter_by(rental_id=rental.id).first()
        self._return_qty(rental, item, 99)
        self.assertEqual(ToolRentalReturn.query.count(), 0)
        db.session.expire_all()
        self.assertAlmostEqual(
            float(ToolRentalItem.query.get(item.id).qty_pending), 3.0)

    def test_unavailable_stock_cannot_be_issued(self):
        self._rent(self.drill, 4, renter_type='internal')
        rentals_before = ToolRental.query.count()
        self._rent(self.drill, 1, renter_type='external', customer='Too Many Co')
        self.assertEqual(ToolRental.query.count(), rentals_before)
        self.assertAlmostEqual(self._ledger_row(self.drill)['out_qty'], 4.0)

    def test_duplicate_submission_posts_only_once(self):
        self._rent(self.drill, 2, renter_type='external', customer='Double Click Co')
        rental = self._latest_rental()
        due = float(rental.total_pending_amount)
        reference = new_txn_reference()
        data = {
            'rental_id': str(rental.id),
            'amount': f'{due:.2f}',
            'payment_date': _pkt_today().isoformat(),
            'payment_mode': 'bank',
            'received_to_account_id': str(self.bank.id),
            'reference': 'SLIP-DUP',
            'txn_reference': reference,
        }
        self._txn('rental_payment', data)
        self._txn('rental_payment', dict(data))
        self.assertEqual(ToolRentalPayment.query.count(), 1)
        self.assertTrue(txn_reference_seen(reference))

    def test_duplicate_return_reference_posts_only_once(self):
        self._rent(self.vibrator, 3, renter_type='external', customer='Dup Return Co')
        rental = self._latest_rental()
        item = ToolRentalItem.query.filter_by(rental_id=rental.id).first()
        reference = new_txn_reference()
        self._return_qty(rental, item, 1, reference=reference)
        self._return_qty(rental, item, 1, reference=reference)
        self.assertEqual(ToolRentalReturn.query.count(), 1)
        db.session.expire_all()
        self.assertAlmostEqual(
            float(ToolRentalItem.query.get(item.id).qty_pending), 2.0)

    def test_empty_form_posts_nothing(self):
        response = self._post('/hdc/tool-rental/create', {'txn_type': 'rent_out_customer'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ToolRental.query.count(), 0)

    def test_reference_generator_never_reuses_a_recorded_number(self):
        self._rent(self.vibrator, 1, renter_type='internal')
        rental = self._latest_rental()
        match = re.search(r'\[Ref (HDC-TX-[0-9]{8}-[A-Z0-9]+)\]', rental.notes or '')
        self.assertIsNotNone(match, rental.notes)
        self.assertTrue(txn_reference_seen(match.group(1)))
        other = new_txn_reference()
        self.assertFalse(txn_reference_seen(other))
        self.assertNotEqual(match.group(1), other)


    def test_page_asks_the_steps_in_one_numbered_order(self):
        """The rent-out flow reads 1 → 6, in the same order as the New Rental form.
        What is happening → from where → which tools → to whom → rent & terms →
        settle the previous holder.  The order is the contract an operator
        learns once, so it is pinned here rather than left to markup taste.
        """
        html = self.client.get('/hdc/tool-rental/transaction').get_data(as_text=True)
        steps = ['1 · What is happening?', '2 · From where are the tools coming?',
                 '3 · Tools from the store', '3 · Tools at the current holder',
                 '4 · To — who receives the tools?', '5 · Rent type &amp; terms',
                 '6 · Settle the previous holder']
        for step in steps:
            self.assertIn(step, html)
        positions = [html.index(step) for step in steps]
        self.assertEqual(positions, sorted(positions))
        # The controls follow the same order: from → tools → to → terms → settle.
        controls = ['name="rentout_from"', 'id="storeToolSearch"',
                    'id="transferLinesSearch"', 'name="rentout_to"',
                    'id="txnCustomerName"', 'id="billing_type"', 'id="settle_choice"']
        control_positions = [html.index(control) for control in controls]
        self.assertEqual(control_positions, sorted(control_positions))
        # The To step and the destination details it opens are one block of the
        # page, so the operator never answers "to whom" twice.
        self.assertLess(html.index('id="rentToPanel"'), html.index('id="destinationCustomerPanel"'))
        self.assertLess(html.index('id="rentToPanel"'), html.index('id="destinationSitePanel"'))

    # ------------------------------------------- the entry-result dialogue box
    # The page posts in the background with this header; the server then
    # answers with the outcome and its flash messages instead of a redirect, so
    # a refused entry leaves the form alone (see _txn_post_answers_as_data).

    def _create_url(self):
        with self.app.test_request_context():
            return url_for('hdc_tool_rental_create')

    def _rent_out_data(self, qty, reference=None):
        return {
            'txn_type': 'rent_out_customer',
            'transaction_date': _pkt_today().isoformat(),
            'txn_reference': reference or new_txn_reference(),
            'responsible_person': 'Office Test',
            'renter_type': 'external',
            'customer_name': 'Dialog Customer',
            'billing_type': 'per_day',
            'rental_date': _pkt_today().isoformat(),
            'tool_id[]': [str(self.vibrator.id)],
            'qty[]': [str(qty)],
            'rate[]': ['500'],
        }

    def _post_from_page(self, url, data, dialog=True):
        payload = dict(data)
        payload['_csrf_token'] = self._token()
        payload.setdefault('txn_source', 'transaction')
        headers = {'X-HDC-Entry-Dialog': '1'} if dialog else {}
        return self.client.post(url, data=payload, headers=headers)

    def test_a_refused_entry_is_answered_as_data_and_nothing_is_recorded(self):
        before = ToolRental.query.count()
        response = self._post_from_page(self._create_url(), self._rent_out_data(999))
        self.assertEqual(response.status_code, 200)
        answer = response.get_json()
        self.assertEqual(answer['outcome'], 'failed')
        self.assertTrue(any(item['kind'] == 'danger' and item['text'] for item in answer['items']),
                        answer)
        self.assertEqual(ToolRental.query.count(), before)

    def test_a_saved_entry_is_answered_as_saved_and_its_message_is_taken_back(self):
        before = ToolRental.query.count()
        answer = self._post_from_page(self._create_url(), self._rent_out_data(2)).get_json()
        self.assertEqual(answer['outcome'], 'saved')
        self.assertTrue(any(item['kind'] == 'success' for item in answer['items']), answer)
        self.assertEqual(ToolRental.query.count(), before + 1)
        with self.client.session_transaction() as sess:
            self.assertNotIn('_flashes', sess)

    def test_a_repeated_reference_is_answered_as_already_recorded(self):
        data = self._rent_out_data(1)
        self.assertEqual(self._post_from_page(self._create_url(), data).get_json()['outcome'],
                         'saved')
        again = self._post_from_page(self._create_url(), data).get_json()
        self.assertEqual(again['outcome'], 'duplicate')
        self.assertEqual(ToolRental.query.filter(ToolRental.rental_code.isnot(None)).count(), 1)

    def test_a_refused_return_and_maintenance_post_are_answered_as_data(self):
        """The same answer for every group: the return and the workshop too."""
        rent = self._post_from_page(self._create_url(), self._rent_out_data(2)).get_json()
        self.assertEqual(rent['outcome'], 'saved')
        rental = self._latest_rental()
        item = rental.items[0]
        too_many = self._post_from_page(
            f'/hdc/tool-rental/{rental.id}/return', {
                'txn_type': 'partial_return',
                'txn_reference': new_txn_reference(),
                'return_condition': 'partial',
                'return_date': _pkt_today().isoformat(),
                'payment_condition': 'no_payment',
                'rental_item_id[]': [str(item.id)],
                'qty_returned[]': ['50'],
                'condition_notes[]': [''],
            }).get_json()
        self.assertEqual(too_many['outcome'], 'failed')
        self.assertTrue(any('exceeds' in i['text'] for i in too_many['items']), too_many)
        nothing_ticked = self._post_from_page(
            '/hdc/tool-rental/transaction/maintenance', {
                'txn_type': 'maintenance_out',
                'txn_reference': new_txn_reference(),
            }).get_json()
        self.assertEqual(nothing_ticked['outcome'], 'failed')
        self.assertTrue(any('at least one piece' in i['text'] for i in nothing_ticked['items']),
                        nothing_ticked)

    def test_a_post_without_the_dialogue_header_still_redirects_as_before(self):
        response = self._post_from_page(self._create_url(), self._rent_out_data(999), dialog=False)
        self.assertEqual(response.status_code, 302)
        self.assertIn('/hdc/tool-rental/transaction', response.headers['Location'])

    def test_a_session_that_has_ended_is_answered_as_signed_out(self):
        # TESTING switches Flask-Login's checks off, and this test's own app
        # context keeps the signed-in user cached on g: both must be undone so
        # the anonymous client is really anonymous.
        self.app.config['LOGIN_DISABLED'] = False
        g.pop('_login_user', None)
        anonymous = self.app.test_client()
        with anonymous.session_transaction() as sess:
            sess['_csrf_token'] = token = 'signed-out-token'
        response = anonymous.post('/hdc/tool-rental/transaction/maintenance', data={
            '_csrf_token': token, 'txn_source': 'transaction',
            'txn_reference': new_txn_reference(), 'tool_id': str(self.vibrator.id), 'qty': '1',
        }, headers={'X-HDC-Entry-Dialog': '1'})
        self.assertEqual(response.get_json()['outcome'], 'signed_out')

    def test_the_page_posts_in_the_background_and_reports_through_the_dialogue_box(self):
        html = self.client.get('/hdc/tool-rental/transaction').get_data(as_text=True)
        self.assertIn('id=\"toolTxnForm\"', html)
        self.assertIn('data-own-submit', html)
        script = _txn_script()
        self.assertIn("'X-HDC-Entry-Dialog': '1'", script)
        self.assertIn('window.location.reload()', script)
        self.assertIn('hdcDialog.show(', script)
        with open(os.path.join(os.path.dirname(SCRIPT_PATH), '..', 'core', 'forms.js'),
                  encoding='utf-8') as handle:
            self.assertIn("form.hasAttribute('data-own-submit')", handle.read())


if __name__ == '__main__':
    unittest.main()
