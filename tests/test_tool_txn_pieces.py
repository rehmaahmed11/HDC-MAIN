#!/usr/bin/env python3
"""Pieces / serial is *ticked* — one checkbox per piece marking, on every sheet.

The Tools transaction form asks which individual pieces are moving in three
places: the store sheet's **Pieces / serial** column (rent out / issue from the
store), the transfer sheet's **Exact pieces** column (moving on from the current
holder) and the return sheet's **Exact pieces** column (coming home).  All three
used to be a ``<select multiple>`` — no tick to give, and ctrl-clicks (or a
desktop) to drive.  They are now the same ticked control the rest of the page
uses: one checkbox per serial marking, a count line, a "tick all shown"
shortcut and a search box once a tool has many markings.

The field names did not change (``row_serial_ids[]``,
``transfer_serial_ids_<item>[]``, ``return_serial_ids_<item>[]``), so what is
pinned here is only *how a piece is chosen* and *that the ticked pieces are the
ones that move*:

* the page script builds the picker and posts the ticked ids — no
  ``selectedOptions`` and no ``<select multiple>`` survives anywhere;
* the rendered page, driven in a real DOM (``tests/tool_txn_pieces_harness.js``
  under jsdom, the real scripts and the real server-rendered markup), ticks
  pieces in all three sheets and hands back the exact fields the browser would
  post;
* those fields are then posted to the **real endpoints** here: the pieces ticked
  in the store are the pieces that leave the store, the piece ticked in the
  transfer moves to the new holder, and the pieces ticked in the return come
  back — while everything left unticked stays where it was.

The DOM harness needs ``node`` with ``jsdom`` (``npm ci --prefix tests``, which
CI does); without it those cases skip, and the script/template contracts below
still run everywhere.

Run with:
    HDC_BOOTSTRAP_ADMIN_PASSWORD='Admin@1234' \\
        python -m unittest tests.test_tool_txn_pieces -v
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

os.environ.setdefault('HDC_ENV', 'test')
os.environ.setdefault('HDC_SECRET_KEY', 'unit-test-secret')
os.environ.setdefault('HDC_BOOTSTRAP_ADMIN_PASSWORD', 'Admin@1234')

from hdc.app import create_app                                     # noqa: E402
from hdc.extensions import db                                      # noqa: E402
from hdc.models.accounts import Account                            # noqa: E402
from hdc.models.projects import Project                            # noqa: E402
from hdc.models.tool_rental import (                               # noqa: E402
    Tool, ToolCategory, ToolRental, ToolRentalItem, ToolSerial,
)
from hdc.services.tool_rental import (                             # noqa: E402
    assign_serials_to_rental, ensure_all_tools_serials, get_out_of_store_serials,
)
from hdc.services.tool_txn_entry import return_sheet, txn_form_context   # noqa: E402
from hdc.utils.dates import _pkt_today                             # noqa: E402

ADMIN_PASSWORD = os.environ['HDC_BOOTSTRAP_ADMIN_PASSWORD']

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
HARNESS = os.path.join(TESTS_DIR, 'tool_txn_pieces_harness.js')
SCRIPT_PATH = os.path.join(REPO_ROOT, 'static', 'hdc', 'js', 'pages', 'tool_txn.js')
TEMPLATE_PATH = os.path.join(REPO_ROOT, 'templates', 'hdc', 'tool_rental', 'tool_txn.html')

#: The shape of the ticked control every sheet must use.
TICK_CLASS = 'pieces-pick-box'
TICK_FIELDS = ('row_serial_ids[]', 'transfer_serial_ids_', 'return_serial_ids_')


def _jsdom_probe() -> tuple[bool, str]:
    """Whether the DOM harness can run here, and why not when it cannot.

    ``jsdom`` is *loaded*, not just resolved, so a jsdom whose engine range
    does not cover this Node (pinned in ``tests/package.json``, installed with
    ``npm ci --prefix tests``) skips with the real reason instead of failing
    the suite for an environment the app cannot control.
    """
    if shutil.which('node') is None:
        return False, 'node is not installed'
    try:
        probe = subprocess.run(['node', '-e', 'require("jsdom")'], cwd=TESTS_DIR,
                               capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if probe.returncode == 0:
        return True, ''
    lines = [line.strip() for line in (probe.stderr or probe.stdout or '').splitlines()
             if line.strip()]
    errors = [line for line in lines if line.startswith(('Error:', 'TypeError:'))]
    detail = errors[0] if errors else (lines[0] if lines else '')
    return False, detail or 'jsdom could not be loaded'


JSDOM_AVAILABLE, JSDOM_DETAIL = _jsdom_probe()


def _read(path: str) -> str:
    with open(path, encoding='utf-8') as handle:
        return handle.read()


class PiecesPickerTestCase(unittest.TestCase):
    """One isolated app + database, rendered once and driven in a DOM."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='hdc-tool-pieces-')
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
        db.session.add(self.site_a)
        db.session.add(Account(name='Pieces Bank', type='bank', status='active'))
        self.cat = ToolCategory(name='Power Tools', active_status=True)
        db.session.add(self.cat)
        db.session.commit()
        # A tool with many markings (the picker grows a search box) and one with
        # few, so both shapes of the control are on the page.
        self.shovel = self._make_tool('TOOL-2001', 'Shovel', 14, 60.0)
        self.vibrator = self._make_tool('TOOL-2002', 'Vibrator', 6, 500.0)
        db.session.commit()
        ensure_all_tools_serials(commit=True)

        # Two customers hold tools: one for the return flow, one for the transfer.
        self.return_rental = self._rent(self.shovel, 3, 'Ali Customer')
        self.transfer_rental = self._rent(self.vibrator, 2, 'Beta Customer')

        self.html = self.client.get('/hdc/tool-rental/transaction').get_data(as_text=True)
        self.page_file = os.path.join(self.tmp, 'page.html')
        self.context_file = os.path.join(self.tmp, 'context.json')
        with open(self.page_file, 'w', encoding='utf-8') as handle:
            handle.write(self.html)
        with open(self.context_file, 'w', encoding='utf-8') as handle:
            json.dump(self._harness_context(), handle)
        self._results = None

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ helpers
    def _login(self):
        page = self.client.get('/hdc/login')
        match = re.search(r'name="_csrf_token" value="([^"]+)"', page.get_data(as_text=True))
        self.client.post('/hdc/login', data={
            'username': 'admin', 'password': ADMIN_PASSWORD,
            '_csrf_token': match.group(1) if match else '',
        }, follow_redirects=True)

    def _make_tool(self, code, name, qty, rate):
        tool = Tool(tool_code=code, name=name, unit='pcs', category_id=self.cat.id,
                    total_quantity=qty, purchase_cost=10_000.0,
                    rental_rate_per_day=rate, condition='good', status='active',
                    is_void=False)
        db.session.add(tool)
        db.session.commit()
        return tool

    def _rent(self, tool, qty, customer):
        rental = ToolRental(
            rental_code=f'RENT-PIECE-{self.shovel.id}-{tool.id}-{qty:g}',
            renter_type='external', customer_name=customer,
            rental_date=_pkt_today(), billing_type='per_day',
            total_rented_qty=qty, total_amount=qty * tool.rental_rate_per_day,
            total_paid=0.0, total_returned_qty=0.0, status='active',
            payment_status='unpaid', notes='pieces picker fixture',
        )
        db.session.add(rental)
        db.session.flush()
        item = ToolRentalItem(rental_id=rental.id, tool_id=tool.id, qty_rented=qty,
                              qty_returned=0.0, qty_pending=qty,
                              rate=tool.rental_rate_per_day,
                              amount=qty * tool.rental_rate_per_day, notes='')
        db.session.add(item)
        db.session.flush()
        piece_ids = [s.id for s in tool.in_store_serials][:int(qty)]
        assign_serials_to_rental(rental_id=rental.id, serial_ids=piece_ids,
                                 rental_item_id=item.id, location_label=customer,
                                 notes=f'Out to {customer}', commit=False)
        db.session.commit()
        return rental

    def _harness_context(self):
        """The rows of *this* database the DOM harness needs to answer the page."""
        rental = self.return_rental
        items = ToolRentalItem.query.filter_by(rental_id=rental.id).all()
        out_serials = {}
        for item in items:
            rows = get_out_of_store_serials(tool_id=int(item.tool_id),
                                            rental_id=int(rental.id),
                                            rental_item_id=int(item.id))
            out_serials[str(item.id)] = [{
                'serial_id': int(serial.id),
                'serial_number': serial.serial_number,
                'tool_id': int(serial.tool_id),
                'rental_id': int(rental.id),
                'condition': serial.condition or 'good',
            } for serial in rows]
        # The picker's option for a location *is* a real source key, so the
        # harness drives the page with the key the server actually sent.
        source = [row for row in txn_form_context()['transfer_sources']
                  if int(row['rental_id']) == int(self.transfer_rental.id)]
        self.assertTrue(source, 'the fixture rental must be offered as a transfer source')
        # The return sheet is driven by *who is returning*: hand the harness the
        # holder key the server actually rendered, and the rows it lists.
        sheet = return_sheet()
        holder_rows = [row for row in sheet['rows']
                       if int(row['rentalId']) == int(rental.id)]
        self.assertTrue(holder_rows, 'the fixture rental must appear on the return sheet')
        holder_key = holder_rows[0]['holderKey']
        return {
            'storeTool': self.shovel.name,
            'transfer': {
                'holder': self.transfer_rental.customer_name,
                'toolName': self.vibrator.name,
                'source': {'key': source[0]['key']},
            },
            'returnHolder': holder_key,
            'returnRowCount': len([row for row in sheet['rows']
                                   if row['holderKey'] == holder_key]),
            'returnRentalCode': holder_rows[0]['rentalCode'],
            'returnTool': holder_rows[0]['toolName'],
            'returnRentalId': int(rental.id),
            'rentals': [{
                'id': int(rental.id),
                'holder': rental.customer_name,
                'items': [{
                    'rental_item_id': int(item.id),
                    'tool_id': int(item.tool_id),
                    'tool_name': item.tool.name,
                    'tool_code': item.tool.tool_code,
                    'qty_rented': float(item.qty_rented),
                    'qty_returned': float(item.qty_returned),
                    'qty_pending': float(item.qty_pending),
                } for item in items],
            }],
            'outOfStoreSerials': out_serials,
        }

    def _run_harness(self):
        """Render → drive in a DOM → the fields the browser would post."""
        if self._results is not None:
            return self._results
        if not JSDOM_AVAILABLE:
            self.skipTest('the DOM harness needs node + jsdom to drive the rendered '
                          'page (run: npm ci --prefix tests) — ' + JSDOM_DETAIL)
        result = subprocess.run(
            ['node', HARNESS, REPO_ROOT, self.page_file, self.context_file],
            capture_output=True, text=True, timeout=180, cwd=TESTS_DIR,
        )
        self.assertEqual(result.returncode, 0,
                         'pieces harness failed:\n%s%s' % (result.stdout, result.stderr))
        self.assertIn('all assertions passed', result.stdout)
        payload = None
        for line in result.stdout.splitlines():
            if line.startswith('HDC-HARNESS-RESULT '):
                payload = json.loads(line[len('HDC-HARNESS-RESULT '):])
        self.assertIsNotNone(payload, 'the harness printed no result payload')
        self._results = payload
        return payload

    def _post_fields(self, action, fields, follow=True):
        """Post exactly what the DOM harness collected — as the browser would."""
        return self.client.post(action, data={name: list(values) for name, values in
                                              fields.items()}, follow_redirects=follow)

    @staticmethod
    def _ticked_ids(fields, prefix):
        """The ticked piece ids a set of collected fields carries."""
        for name, values in fields.items():
            if name.startswith(prefix):
                for value in values:
                    ids = [int(tok) for tok in str(value).split(',') if str(tok).strip()]
                    if ids:
                        return ids
        return []

    # ------------------------------------------------- the control itself
    def test_every_sheet_ticks_pieces_in_a_real_dom(self):
        results = self._run_harness()
        self.assertEqual(results['store']['action'], '/hdc/tool-rental/create')
        self.assertTrue(results['store']['fields']['row_serial_ids[]'])
        # A rental-bound transaction posts to its own route, with the picker's
        # rental in rental_id — the action the script built from the registry.
        self.assertEqual(results['returns']['fields']['txn_type'], ['partial_return'])
        self.assertIn('/return', results['returns']['action'])
        # The harness' own assertions cover the markup; these pin the payloads
        # the three sheets produced, so a silent change here is a test failure.
        for key in ('store', 'transfer', 'transferQtyOnly', 'returns'):
            self.assertIn(key, results, key)
        self.assertEqual(len(self._ticked_ids(results['store']['fields'], 'row_serial_ids[]')), 3)
        self.assertEqual(len(self._ticked_ids(results['transfer']['fields'],
                                             'transfer_serial_ids_')), 1)
        self.assertEqual(self._ticked_ids(results['transferQtyOnly']['fields'],
                                          'transfer_serial_ids_'), [])
        self.assertEqual(len(self._ticked_ids(results['returns']['fields'],
                                             'return_serial_ids_')), 2)

    # ------------------------------------------------- what actually moves
    def test_the_ticked_pieces_are_the_ones_that_leave_the_store(self):
        results = self._run_harness()
        store = results['store']
        ticked = self._ticked_ids(store['fields'], 'row_serial_ids[]')
        self.assertEqual(len(ticked), 3)
        self._post_fields(store['action'], store['fields'])

        rental = ToolRental.query.order_by(ToolRental.id.desc()).first()
        self.assertEqual(rental.customer_name, 'Harness Customer')
        item = ToolRentalItem.query.filter_by(rental_id=rental.id).first()
        self.assertEqual(float(item.qty_rented), 3.0)
        moved = ToolSerial.query.filter_by(current_rental_item_id=int(item.id)).all()
        self.assertEqual(sorted(serial.id for serial in moved), sorted(ticked),
                         'the pieces ticked in the Pieces / serial column are the ones issued')
        self.assertTrue(all(not serial.is_in_store for serial in moved))
        self.assertTrue(all(serial.current_location_label == 'Harness Customer'
                            for serial in moved))
        stayed = ToolSerial.query.filter(ToolSerial.tool_id == self.shovel.id,
                                         ToolSerial.is_in_store == True).all()  # noqa: E712
        self.assertTrue(stayed, 'unticked pieces stay in the store')
        self.assertTrue(all(serial.id not in ticked for serial in stayed))

    def test_the_ticked_transfer_piece_moves_to_the_new_holder(self):
        results = self._run_harness()
        transfer = results['transfer']
        ticked = self._ticked_ids(transfer['fields'], 'transfer_serial_ids_')
        self.assertEqual(len(ticked), 1)
        self._post_fields(transfer['action'], transfer['fields'])

        rental = ToolRental.query.order_by(ToolRental.id.desc()).first()
        self.assertEqual(rental.customer_name, 'Harness Next Customer')
        item = ToolRentalItem.query.filter_by(rental_id=rental.id).first()
        self.assertEqual(float(item.qty_pending), 1.0)
        moved = db.session.get(ToolSerial, ticked[0])
        self.assertEqual(int(moved.current_rental_id or 0), int(rental.id),
                         'the piece ticked in the transfer sheet is the piece that moved')
        self.assertFalse(moved.is_in_store)

    def test_the_ticked_return_pieces_come_back_to_the_store(self):
        results = self._run_harness()
        returns = results['returns']
        ticked = self._ticked_ids(returns['fields'], 'return_serial_ids_')
        rental_id = int(returns['fields']['return_rental_id[]'][0])
        self.assertEqual(rental_id, int(self.return_rental.id))
        self.assertEqual(len(ticked), 2)
        self._post_fields(returns['action'], returns['fields'])

        for serial_id in ticked:
            serial = db.session.get(ToolSerial, serial_id)
            self.assertTrue(serial.is_in_store,
                            f'{serial.serial_number} was ticked and must be back in the store')
        still_out = [serial for serial in
                     ToolSerial.query.filter(ToolSerial.tool_id == self.shovel.id).all()
                     if int(serial.current_rental_id or 0) == int(self.return_rental.id)]
        self.assertEqual(len(still_out), 1, 'the unticked piece stays at the holder')
        self.assertNotIn(still_out[0].id, ticked)

    # ------------------------------------------------- contracts that always run
    def test_page_script_ticks_pieces_and_keeps_the_posted_field_names(self):
        script = _read(SCRIPT_PATH)
        self.assertIn(TICK_CLASS, script)
        self.assertIn("box.type = 'checkbox'", script)
        self.assertIn('pieces-picker-all', script)
        self.assertIn('pieces-picker-search', script)
        # A ticked piece list is read through the picker, never a multi-select.
        self.assertNotIn('selectedOptions', script)
        self.assertNotIn('multiple = true', script)
        for field in TICK_FIELDS:
            self.assertIn(field, script, field)
        # The three sheets that ask which pieces move all build the picker.
        for sheet_class in ('store-line-serials', 'transfer-serials', 'return-serials'):
            self.assertIn(f"className: '{sheet_class}'", script, sheet_class)

    def test_template_sizes_the_pieces_column_for_ticks(self):
        template = _read(TEMPLATE_PATH)
        self.assertIn('Pieces / serial', template)
        self.assertIn('.pieces-picker', template)
        self.assertIn('.pieces-picker-all', template)
        self.assertIn('.pieces-picker-list', template)
        self.assertIn('.pieces-pick ', template)
        self.assertRegex(template, r'\.tool-txn-sheet \.col-pieces \{ min-width:\d+px; \}')
        self.assertIn('Tick the exact markings', template)


if __name__ == '__main__':
    unittest.main()
