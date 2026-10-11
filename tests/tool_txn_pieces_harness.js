/* HDC ERP — tool_txn_pieces_harness.js
 *
 * Runs the *server-rendered* HDC Tool Transaction page with the real page
 * script and proves the one thing the field asked for: **Pieces / serial is
 * ticked with checkboxes** — on every sheet that asks which individual pieces
 * are moving — and the ticked pieces are what the form posts.
 *
 *   • the store sheet (rent out / issue from the store): the Pieces / serial
 *     cell holds one checkbox per in-store marking, with a count line, a
 *     "Tick all shown" shortcut and a search box on a tool with many pieces;
 *   • ticking pieces fills the quantity cell (and locks it), unticking them
 *     hands it back;
 *   • the transfer sheet ticks the pieces leaving the current holder — and a
 *     ticked piece can never sit on a row that would not post;
 *   • the return sheet ticks the exact pieces coming back;
 *   • on submit the form carries exactly the ticked ids, under the same field
 *     names the routes already read (``row_serial_ids[]``,
 *     ``transfer_serial_ids_<item>[]``, ``return_serial_ids_<item>[]``), and a
 *     row with no tick still posts its plain quantity;
 *   • nothing on the page posts pieces through a ``<select multiple>``.
 *
 * The page HTML and a fixture of the live database rows are rendered/written
 * by ``tests/test_tool_txn_pieces.py``; this harness only drives them.  It
 * needs jsdom (``npm ci --prefix tests``) and is
 * skipped when jsdom is not installed, so the Python suite stays runnable
 * without it.
 *
 * Usage: node tool_txn_pieces_harness.js <repo_root> <page.html> <context.json>
 * Prints one JSON result line (``HDC-HARNESS-RESULT {…}``) that the Python
 * test feeds back into the real endpoints.
 */

'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const [repoRoot, pageFile, contextFile] = process.argv.slice(2);
if (!repoRoot || !pageFile || !contextFile) {
  console.error('usage: node tool_txn_pieces_harness.js <repo_root> <page.html> <context.json>');
  process.exit(2);
}

let JSDOM;
try {
  ({ JSDOM } = require('jsdom'));
} catch (err) {
  console.error('jsdom is not installed: ' + err.message);
  process.exit(3);
}

const context = JSON.parse(fs.readFileSync(contextFile, 'utf8'));
const html = fs.readFileSync(pageFile, 'utf8');

const dom = new JSDOM(html, {
  runScripts: 'dangerously',
  pretendToBeVisual: true,
  url: 'https://hdc.local/hdc/tool-rental/transaction',
});
const { window } = dom;
const { document } = window;

// The page's external scripts are not fetched by jsdom: run the real files.
const bootstrapScript = document.createElement('script');
bootstrapScript.textContent = fs.readFileSync(
  path.join(repoRoot, 'static', 'hdc', 'vendor', 'bootstrap', 'bootstrap.bundle.min.js'), 'utf8');
document.body.appendChild(bootstrapScript);
['core/layout.js', 'core/theme.js', 'core/dialog.js', 'core/forms.js', 'core/combo.js',
 'core/audit.js', 'core/responsive_tables.js', 'core/collapse_groups.js',
 'pages/tool_txn.js'].forEach((rel) => {
  const node = document.createElement('script');
  node.textContent = fs.readFileSync(path.join(repoRoot, 'static', 'hdc', 'js', rel), 'utf8');
  document.body.appendChild(node);
});

// The page asks before it posts; a prompt must not stop the harness.
window.confirm = function () { return true; };

// The return sheet fills its pieces from the API — answer it with the rows the
// Python fixture read out of the same database the page was rendered from.
window.fetch = function (url) {
  const answer = (payload) => Promise.resolve({
    ok: true, status: 200, json: () => Promise.resolve(payload),
  });
  const pending = /\/rental\/(\d+)\/pending-items/.exec(String(url));
  if (pending) {
    const rental = (context.rentals || []).find((r) => String(r.id) === pending[1]);
    return answer(rental ? rental.items : []);
  }
  if (String(url).indexOf('serials/out-of-store') !== -1) {
    const toolId = decodeURIComponent((/[?&]tool_id=([^&]+)/.exec(String(url)) || [])[1] || '');
    const serials = [];
    Object.keys(context.outOfStoreSerials || {}).forEach((itemId) => {
      (context.outOfStoreSerials[itemId] || []).forEach((serial) => {
        if (String(serial.tool_id) !== String(toolId)) return;
        serials.push({
          serial_id: serial.serial_id,
          serial_number: serial.serial_number,
          condition: serial.condition || 'good',
          rental_item_id: Number(itemId),
        });
      });
    });
    return answer({ tools: [{ tool_id: Number(toolId), serials }] });
  }
  return Promise.resolve({ ok: false, status: 404, json: () => Promise.resolve(null) });
};

// ── helpers ─────────────────────────────────────────────────────────────────

let assertions = 0;
function ok(condition, message) {
  assertions++;
  assert.ok(condition, message);
}
function equal(actual, expected, message) {
  assertions++;
  assert.deepEqual(actual, expected, message + ' — got ' + JSON.stringify(actual));
}
function flush() {
  // Let the promise chains of the picker fetches settle.
  return new Promise((resolve) => setTimeout(resolve, 0));
}
function tick(element, on) {
  if (typeof on === 'boolean') element.checked = on;
  else element.checked = !element.checked;
  element.dispatchEvent(new window.Event('change', { bubbles: true }));
}
function press(element) {
  element.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
}
function pieces(scope) {
  return Array.from(scope.querySelectorAll('.pieces-pick-box'));
}
function tickedPieces(scope) {
  return pieces(scope).filter((box) => box.checked).map((box) => box.value);
}
function selectValue(select, value) {
  select.value = value;
  select.dispatchEvent(new window.Event('change', { bubbles: true }));
}
/* The fields the browser would post for this form, exactly as it would post
   them (nameless controls, disabled controls and unticked boxes dropped). */
function collectFields() {
  const fields = {};
  const form = document.getElementById('toolTxnForm');
  Array.from(form.elements).forEach((element) => {
    if (!element.name || element.disabled) return;
    if (element.type === 'checkbox' || element.type === 'radio') {
      if (!element.checked) return;
      (fields[element.name] = fields[element.name] || []).push(element.value);
      return;
    }
    if (element.type === 'submit' || element.type === 'button' || element.type === 'reset') return;
    (fields[element.name] = fields[element.name] || []).push(element.value);
  });
  return fields;
}
function submit() {
  const form = document.getElementById('toolTxnForm');
  form.dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
  return { action: form.getAttribute('action'), fields: collectFields() };
}
/* The page locks its submit button on the first submit (a double click must
   never post twice); Clear is what hands it back — and it re-renders the
   script-built pickers, exactly as it does for the operator. */
async function clearForm() {
  document.getElementById('toolTxnForm')
    .dispatchEvent(new window.Event('reset', { bubbles: true, cancelable: true }));
  await flush();
}

const results = {};

(async function main() {
  const config = JSON.parse(document.getElementById('toolTxnConfig').textContent);
  const tools = {};
  config.tools.forEach((tool) => { tools[tool.name] = tool; });

  // The three group cards open the shared entry dialog. Rent Out infers its
  // issue/transfer type from the existing From / To controls, while Return and
  // Maintenance keep their transaction selector visible.
  const cards = Array.from(document.querySelectorAll('#txnGroupCards [data-txn-group]'));
  equal(cards.map((card) => card.getAttribute('data-txn-group')),
    ['rent_out', 'return', 'maintenance'], 'the page offers three transaction cards');
  const transactionField = document.getElementById('txnTypeField');
  ok(transactionField.classList.contains('d-none'),
    'Rent Out hides the transaction selector');
  press(cards.find((card) => card.getAttribute('data-txn-group') === 'return'));
  await new Promise((resolve) => setTimeout(resolve, 20));
  ok(document.getElementById('toolTxnModal').classList.contains('show'),
    'clicking a group card opens the shared entry dialog');
  ok(!transactionField.classList.contains('d-none'),
    'Return shows the transaction selector');
  equal(document.getElementById('txnType').value, 'partial_return',
    'Return opens on its first transaction');
  press(cards.find((card) => card.getAttribute('data-txn-group') === 'maintenance'));
  ok(!transactionField.classList.contains('d-none'),
    'Maintenance keeps the transaction selector visible');
  equal(document.getElementById('txnType').value, 'maintenance_out',
    'Maintenance opens on its first transaction');
  press(cards.find((card) => card.getAttribute('data-txn-group') === 'rent_out'));
  ok(transactionField.classList.contains('d-none'),
    'switching back to Rent Out hides the transaction selector again');
  equal(document.getElementById('txnType').value, 'rent_out_customer',
    'Rent Out opens on its default issue action');

  // ── the store sheet: one checkbox per piece, no multi-select ─────────────
  const shovelTick = Array.from(document.querySelectorAll('#storeToolList .store-tool-check'))
    .find((box) => /Shovel/.test(box.parentNode.textContent));
  ok(shovelTick, 'the store tick list offers the shovel');
  tick(shovelTick);

  const storeRow = document.querySelector('#storeSelectedRows .store-line-row');
  ok(storeRow, 'ticking a tool adds its excel-style row');
  const storeCell = storeRow.querySelector('.store-line-pieces');
  const shovel = tools.Shovel;
  equal(pieces(storeCell).map((box) => box.value),
    shovel.serials.map((serial) => String(serial.id)),
    'the Pieces / serial cell holds one checkbox per in-store marking, in order');
  equal(pieces(storeCell).map((box) => box.parentNode.querySelector('.pieces-pick-name').textContent),
    shovel.serials.map((serial) => serial.serial_number),
    'every checkbox is named by its own serial marking');
  equal(storeCell.querySelectorAll('select').length, 0,
    'the Pieces / serial cell posts nothing through a multi-select');
  equal(storeCell.querySelectorAll('input[type="checkbox"]').length, shovel.serials.length,
    'each piece is a real checkbox, not an option');
  equal(storeCell.querySelector('.pieces-picker-count').textContent,
    '0 of ' + shovel.serials.length + ' ticked', 'the tick count starts at zero');
  ok(storeCell.querySelector('.pieces-picker-search'),
    'a tool with many markings offers a search box');
  equal(storeCell.querySelector('.pieces-picker-all').textContent.trim(), 'Tick all shown',
    'the bulk shortcut offers to tick everything shown');

  const storeQty = storeRow.querySelector('.store-line-qty');
  const firstThree = pieces(storeCell).slice(0, 3);
  firstThree.forEach((box) => tick(box));
  equal(tickedPieces(storeCell), firstThree.map((box) => box.value), 'three pieces are ticked');
  equal(storeQty.value, '3', 'the quantity follows the ticked pieces');
  ok(storeQty.readOnly, 'a ticked row cannot contradict its ticks with a typed quantity');
  equal(storeCell.querySelector('.pieces-picker-count').textContent,
    '3 of ' + shovel.serials.length + ' ticked', 'the count line follows the ticks');

  const storeSearch = storeCell.querySelector('.pieces-picker-search');
  const wantedLabel = shovel.serials[shovel.serials.length - 1].serial_number;
  storeSearch.value = wantedLabel;
  storeSearch.dispatchEvent(new window.Event('input', { bubbles: true }));
  const shown = Array.from(storeCell.querySelectorAll('.pieces-pick'))
    .filter((row) => !row.classList.contains('pieces-pick-off'));
  ok(shown.length && shown.length < pieces(storeCell).length,
    'the picker search hides the markings that do not match');
  ok(shown.every((row) => row.querySelector('.pieces-pick-name').textContent.indexOf(wantedLabel) !== -1),
    'every marking left on screen matches the search');
  storeSearch.value = '';
  storeSearch.dispatchEvent(new window.Event('input', { bubbles: true }));

  // The ticked pieces are what the form posts, under the existing field name.
  selectValue(document.getElementById('txnType'), 'rent_out_customer');
  document.getElementById('txnCustomerNameInput').value = 'Harness Customer';
  results.store = submit();
  equal(results.store.fields['tool_id[]'], [String(shovel.id)], 'the row posts its tool');
  equal(results.store.fields['qty[]'], ['3'], 'the row posts the count of ticked pieces');
  equal(results.store.fields['row_serial_ids[]'], [tickedPieces(storeCell).join(',')],
    'the ticked pieces post as row_serial_ids[] — the field the create route reads');
  equal(results.store.fields['row_serial_ids[]'][0].split(','), firstThree.map((box) => box.value),
    'the posted ids are exactly the ticked pieces, in sheet order');

  // Unticking every piece hands the quantity cell back; the shortcut fills it.
  firstThree.forEach((box) => tick(box, false));
  equal(tickedPieces(storeCell), [], 'clearing the ticks clears the selection');
  ok(!storeQty.readOnly, 'an unticked row types its own quantity again');
  press(storeCell.querySelector('.pieces-picker-all'));
  equal(storeQty.value, String(shovel.serials.length),
    'the shortcut ticks every shown piece and fills the quantity');
  ok(storeQty.readOnly, 'a shortcut-ticked row locks its quantity too');
  equal(storeCell.querySelector('.pieces-picker-all').textContent.trim(), 'Clear all shown',
    'the shortcut then offers to clear them again');
  press(storeCell.querySelector('.pieces-picker-all'));
  equal(tickedPieces(storeCell), [], 'the shortcut clears every shown piece again');

  // ── the transfer sheet ──────────────────────────────────────────────────
  await clearForm();
  const fromCustomer = document.querySelector('input[name="rentout_from"][value="customer"]');
  document.querySelector('input[name="rentout_to"][value="external"]').checked = true;
  tick(fromCustomer, true);                 // retargets to a customer → customer hand-over
  equal(document.getElementById('txnType').value, 'transfer_customer',
    'From customer + To customer picks the customer transfer');

  const source = context.transfer.source;
  selectValue(document.getElementById('transferSourceSelect'), source.key);
  const holderBoxes = Array.from(document.querySelectorAll('#transferHoldersList .transfer-holder-checkbox'));
  ok(holderBoxes.length, 'the holder list offers the rentals at ' + context.transfer.holder);
  holderBoxes.forEach((box) => tick(box, true));

  const transferRow = Array.from(document.querySelectorAll('#transferLinesContainer .transfer-line-row'))
    .find((row) => row.querySelector('strong').textContent === context.transfer.toolName);
  ok(transferRow, 'the ticked holder shows its tools in a sheet');
  document.getElementById('txnCustomerNameInput').value = 'Harness Next Customer';
  const transferCell = transferRow.querySelector('.transfer-pieces');
  const transferPieces = pieces(transferCell);
  ok(transferPieces.length, 'the transfer sheet offers one checkbox per piece at the holder');
  equal(tickedPieces(transferCell), [], 'a transfer starts with no piece ticked');
  equal(transferCell.querySelectorAll('select').length, 0,
    'the transfer sheet posts nothing through a multi-select');
  const transferPick = transferRow.querySelector('.transfer-pick');
  tick(transferPick, false);
  tick(transferPieces[0]);
  equal(transferRow.querySelector('.transfer-qty').value, '1', 'ticking one piece sets the move quantity');
  ok(transferPick.checked, 'ticking a piece ticks its row — a tick can never be dropped unnoticed');
  results.transfer = submit();
  const transferItemId = transferRow.querySelector('.transfer-item-id').value;
  equal(results.transfer.fields['transfer_serial_ids_' + transferItemId + '[]'],
    [tickedPieces(transferCell).join(',')],
    'the ticked pieces post under transfer_serial_ids_<item>[]');
  equal(results.transfer.fields['transfer_item_id[]'], [transferItemId],
    'the row itself still posts');

  // A row without a tick keeps posting its plain quantity — the old path.
  await clearForm();
  const plainRow = Array.from(document.querySelectorAll('#transferLinesContainer .transfer-line-row'))
    .find((row) => row.querySelector('strong').textContent === context.transfer.toolName);
  ok(plainRow, 'the transfer sheet is back after Clear');
  equal(tickedPieces(plainRow.querySelector('.transfer-pieces')), [],
    'Clear leaves no piece ticked');
  tick(plainRow.querySelector('.transfer-pick'), true);
  plainRow.querySelector('.transfer-qty').value = '1';
  const plainItemId = plainRow.querySelector('.transfer-item-id').value;
  results.transferQtyOnly = submit();
  equal(results.transferQtyOnly.fields['transfer_qty_' + plainItemId], ['1'],
    'a quantity-only transfer posts its quantity');
  ok(!results.transferQtyOnly.fields['transfer_serial_ids_' + plainItemId + '[]'],
    'a quantity-only transfer posts no piece list at all');

  // ── the return sheet ────────────────────────────────────────────────────
  await clearForm();
  selectValue(document.getElementById('txnType'), 'partial_return');
  const rental = (context.rentals || [])
    .find((row) => String(row.id) === String(context.returnRentalId)) || context.rentals[0];
  selectValue(document.getElementById('returnRentalSelect'), String(rental.id));
  await flush(); await flush(); await flush();

  const returnRow = document.querySelector('#returnItemRows .tool-txn-line');
  ok(returnRow, 'the return sheet lists the tools still out');
  const returnCell = returnRow.querySelector('.return-pieces');
  const returnPieces = pieces(returnCell);
  ok(returnPieces.length, 'the return sheet offers one checkbox per piece still out');
  equal(returnCell.querySelectorAll('select').length, 0,
    'the return sheet posts nothing through a multi-select');
  tick(returnPieces[0]);
  tick(returnPieces[1]);
  const returnQty = returnRow.querySelector('.return-qty-input');
  equal(returnQty.value, '2', 'the returning quantity follows the ticked pieces');
  ok(returnQty.readOnly, 'ticked pieces decide the returning quantity');
  results.returns = submit();
  const returnItemId = returnRow.querySelector('.return-item-id').value;
  equal(results.returns.fields['return_serial_ids_' + returnItemId + '[]'],
    [tickedPieces(returnCell).join(',')],
    'the ticked pieces post under return_serial_ids_<item>[]');
  equal(results.returns.fields['qty_returned[]'], ['2'], 'the returning quantity posts with them');

  // ── nothing left to ctrl-click ──────────────────────────────────────────
  equal(document.querySelectorAll('#toolTxnForm select[multiple]').length, 0,
    'no sheet posts pieces through a multi-select any more');
  equal(document.querySelectorAll('#toolTxnForm .pieces-picker').length,
    document.querySelectorAll('#toolTxnForm .pieces-picker[data-piece-picker]').length,
    'every piece picker is the ticked control the script builds');

  console.log('HDC-HARNESS-RESULT ' + JSON.stringify(results));
  console.log('all assertions passed (' + assertions + ')');
  window.close();
})().catch((err) => {
  console.error(err && err.stack ? err.stack : String(err));
  process.exit(1);
});
