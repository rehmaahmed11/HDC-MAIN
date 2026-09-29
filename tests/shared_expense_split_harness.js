/* HDC ERP — shared_expense_split_harness.js
 *
 * Runs the real Shared Expenses page script (static/hdc/js/pages/
 * shared_expenses.js) against a minimal hand-rolled DOM shaped exactly like
 * the split table the expense form renders, and asserts the behaviour the
 * form promises:
 *
 *   * type the total and every ticked head's amount appears at once (equal);
 *   * a percentage typed in works out that head's rupees at once;
 *   * an amount typed in works out that head's percentage;
 *   * the check line says whether the figures add up, before the server has to;
 *   * a head that is not ticked is left out of the split;
 *   * switching mode carries the figures across instead of wiping them.
 *
 * The figures the page ends up with are written to a JSON file so the Python
 * test (tests/test_shared_expenses.py) can run them through the real split
 * engine — what the operator sees must be what gets saved.
 *
 * Usage: node shared_expense_split_harness.js <repo_root> [out_json]
 */

'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const repoRoot = process.argv[2];
const outPath = process.argv[3] || null;
if (!repoRoot) {
  console.error('usage: node shared_expense_split_harness.js <repo_root> [out_json]');
  process.exit(2);
}

// ── Minimal DOM ─────────────────────────────────────────────────────────────

function makeClassList(el) {
  const set = new Set(String(el.className || '').split(/\s+/).filter(Boolean));
  const sync = () => { el.className = Array.from(set).join(' '); };
  return {
    add(c) { set.add(c); sync(); },
    remove(c) { set.delete(c); sync(); },
    toggle(c, force) {
      const on = force === undefined ? !set.has(c) : !!force;
      if (on) { set.add(c); } else { set.delete(c); }
      sync();
      return on;
    },
    contains(c) { return set.has(c); },
  };
}

function matches(el, selector) {
  const sel = String(selector).trim();
  if (!sel) { return false; }
  if (sel.charAt(0) === '#') { return el.id === sel.slice(1); }
  if (sel.charAt(0) === '.') { return el.classList.contains(sel.slice(1)); }
  return el.tagName === sel.toUpperCase();
}

function makeElement(tag, id) {
  const el = {
    tagName: String(tag).toUpperCase(),
    id: id || '',
    className: '',
    children: [],
    parentNode: null,
    value: '',
    checked: false,
    readOnly: false,
    hidden: false,
    options: [],
    style: {},
    _text: '',
    _html: '',
    _listeners: {},
    get classList() { return this._classList || (this._classList = makeClassList(this)); },
    get textContent() { return this._text; },
    set textContent(v) { this._text = String(v); },
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = String(v); this._text = String(v).replace(/<[^>]*>/g, ' '); },
    appendChild(child) { child.parentNode = this; this.children.push(child); return child; },
    addEventListener(type, fn) { (this._listeners[type] || (this._listeners[type] = [])).push(fn); },
    dispatchEvent(ev) {
      if (!ev.target) { ev.target = this; }
      for (const fn of (this._listeners[ev.type] || []).slice()) { fn(ev); }
      return true;
    },
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; },
    querySelectorAll(selector) {
      const parts = String(selector).split(',').map((s) => s.trim()).filter(Boolean);
      const found = [];
      const walk = (node) => {
        for (const child of node.children) {
          if (parts.some((sel) => matches(child, sel))) { found.push(child); }
          walk(child);
        }
      };
      walk(this);
      return found;
    },
    closest(selector) {
      let node = this;
      while (node) {
        if (matches(node, selector)) { return node; }
        node = node.parentNode;
      }
      return null;
    },
  };
  return el;
}

const documentStub = {
  readyState: 'complete',
  activeElement: null,
  _byId: new Map(),
  getElementById(id) {
    if (this._byId.has(id)) { return this._byId.get(id); }
    const found = this.body.querySelectorAll('#' + id);
    const el = found.length ? found[0] : null;
    if (el) { this._byId.set(id, el); }
    return el;
  },
  querySelectorAll(selector) { return this.body.querySelectorAll(selector); },
  querySelector(selector) { return this.body.querySelector(selector); },
  addEventListener() {},
  createElement(tag) { return makeElement(tag); },
  body: makeElement('body'),
};

// ── The split table, as expense_form.html renders it ────────────────────────

const PARTIES = [
  { id: 1, name: 'FBM' },
  { id: 2, name: 'HDC' },
  { id: 3, name: 'Home' },
];

const form = makeElement('form', 'seExpenseForm');
const modeSelect = makeElement('select', 'split_mode');
const totalInput = makeElement('input', 'total_amount');
const checkBox = makeElement('div', 'seSplitCheck');

function option(parent, value) {
  const opt = makeElement('option');
  opt.value = String(value);
  parent.appendChild(opt);
  parent.options.push(opt);
  return opt;
}
Object.defineProperty(modeSelect, 'value', {
  get() { return this._value === undefined ? 'equal' : this._value; },
  set(v) { this._value = String(v); },
  configurable: true,
});
['equal', 'custom', 'percent'].forEach((m) => option(modeSelect, m));

form.appendChild(modeSelect);
form.appendChild(totalInput);
form.appendChild(checkBox);

const rows = {};
for (const party of PARTIES) {
  const row = makeElement('div');
  row.classList.add('se-split-row');
  const tick = makeElement('input');
  tick.classList.add('se-party-tick');
  tick.name = 'party_ids';
  tick.value = String(party.id);
  tick.checked = false;
  const amount = makeElement('input');
  amount.classList.add('se-amount');
  amount.name = 'amount_party_' + party.id;
  const percent = makeElement('input');
  percent.classList.add('se-percent');
  percent.name = 'percent_party_' + party.id;
  row.appendChild(tick);
  row.appendChild(amount);
  row.appendChild(percent);
  form.appendChild(row);
  rows[party.id] = { party, row, tick, amount, percent };
}
documentStub.body.appendChild(form);

// ── Load the real page script ───────────────────────────────────────────────

const source = fs.readFileSync(
  path.join(repoRoot, 'static', 'hdc', 'js', 'pages', 'shared_expenses.js'), 'utf8');
vm.runInNewContext(source, { document: documentStub, window: documentStub, console });

// ── Helpers ─────────────────────────────────────────────────────────────────

function type(input, text) {
  input.value = String(text);
  input.dispatchEvent({ type: 'input', target: input });
}

function commit(input) {
  input.dispatchEvent({ type: 'change', target: input });
}

function setTotal(text) { type(totalInput, text); }

function setMode(mode) {
  modeSelect.value = mode;
  modeSelect.dispatchEvent({ type: 'change', target: modeSelect });
}

function tick(ids) {
  const wanted = ids.map(String);
  for (const id of Object.keys(rows)) {
    const on = wanted.indexOf(String(id)) !== -1;
    if (rows[id].tick.checked !== on) {
      rows[id].tick.checked = on;
      rows[id].tick.dispatchEvent({ type: 'change', target: rows[id].tick });
    }
  }
}

function reset() {
  for (const id of Object.keys(rows)) {
    rows[id].tick.checked = false;
    rows[id].amount.value = '';
    rows[id].percent.value = '';
    rows[id].amount.readOnly = false;
    rows[id].percent.readOnly = false;
  }
  totalInput.value = '';
  checkBox._html = '';
  checkBox.className = '';
  checkBox.classList.remove('ok');
  checkBox.classList.remove('bad');
  modeSelect.value = 'equal';
  modeSelect.dispatchEvent({ type: 'change', target: modeSelect });
}

function amountOf(id) { return rows[id].amount.value; }
function percentOf(id) { return rows[id].percent.value; }
function hint() { return checkBox.textContent.replace(/\s+/g, ' ').trim(); }
function state() {
  return checkBox.classList.contains('ok') ? 'ok'
    : checkBox.classList.contains('bad') ? 'bad' : 'idle';
}
function snapshot(name, mode, refused) {
  const picked = Object.keys(rows).filter((id) => rows[id].tick.checked);
  return {
    name,
    mode,
    total: totalInput.value,
    refused: !!refused,
    hint: hint(),
    state: state(),
    rows: picked.map((id) => ({
      party_id: Number(id),
      amount: amountOf(id),
      percent: percentOf(id),
      readOnlyAmount: rows[id].amount.readOnly,
      readOnlyPercent: rows[id].percent.readOnly,
      off: rows[id].row.classList.contains('se-split-off'),
    })),
  };
}

// ── Checks ──────────────────────────────────────────────────────────────────

const checks = [];
const cases = [];

function check(name, fn) { checks.push({ name, fn }); }
function record(entry) { cases.push(entry); return entry; }

check('an equal split fills every ticked head as soon as the total is typed', () => {
  reset();
  tick([1, 2, 3]);
  assert.equal(state(), 'idle', 'with no total there is nothing to divide yet');
  assert.match(hint(), /Enter the total amount/);

  setTotal('9000');
  assert.equal(amountOf(1), '3,000.00');
  assert.equal(amountOf(2), '3,000.00');
  assert.equal(amountOf(3), '3,000.00');
  assert.equal(percentOf(1), '33.34');
  assert.equal(percentOf(2), '33.33');
  assert.equal(percentOf(3), '33.33');
  assert.equal(state(), 'ok');
  assert.match(hint(), /adds up/);
  record(snapshot('equal-9000-three-ways', 'equal'));
});

check('changing the total re-splits on the spot', () => {
  setTotal('5,000');
  assert.equal(amountOf(1), '1,666.67');
  assert.equal(amountOf(2), '1,666.67');
  assert.equal(amountOf(3), '1,666.66', 'the odd paisa is handed out one by one');
  assert.equal(state(), 'ok');
  record(snapshot('equal-5000-odd-paisa', 'equal'));
});

check('an unticked head takes no share and is marked as out', () => {
  tick([1, 2]);
  assert.equal(amountOf(1), '2,500.00');
  assert.equal(amountOf(2), '2,500.00');
  assert.equal(rows[3].row.classList.contains('se-split-off'), true);
  assert.equal(rows[1].row.classList.contains('se-split-off'), false);
  record(snapshot('equal-5000-two-heads', 'equal'));
});

check('percentages work out each head’s rupees as they are typed', () => {
  reset();
  tick([1, 2, 3]);
  setTotal('9000');
  setMode('percent');
  type(rows[1].percent, '50');
  assert.equal(amountOf(1), '4,500.00', 'the rupees follow the percentage');
  assert.equal(state(), 'bad', 'half the bill is still unassigned');
  type(rows[2].percent, '30');
  assert.equal(amountOf(2), '2,700.00');
  type(rows[3].percent, '20');
  assert.equal(amountOf(3), '1,800.00');
  assert.equal(state(), 'ok');
  assert.match(hint(), /100\.00% of 100%/);
  const snap = snapshot('percent-50-30-20', 'percent');
  record(snap);
  assert.deepEqual(snap.rows.map((r) => r.amount), ['4,500.00', '2,700.00', '1,800.00']);
});

check('percentages that do not add up are refused on screen', () => {
  reset();
  tick([1, 2]);
  setTotal('1000');
  setMode('percent');
  type(rows[1].percent, '60');
  type(rows[2].percent, '30');
  assert.equal(state(), 'bad');
  assert.match(hint(), /add up to 100%/);
  record(snapshot('percent-short', 'percent', true));
});

check('an amount typed in percent mode works out the percentage', () => {
  reset();
  tick([1, 2]);
  setTotal('9000');
  setMode('percent');
  type(rows[1].amount, '3000');
  commit(rows[1].amount);
  assert.equal(percentOf(1), '33.33', 'the percentage is worked back from the rupees');
  type(rows[2].percent, '66.67');
  assert.equal(amountOf(1), '2,999.70', 'a percentage carries two decimals — 33.33% of 9,000');
  assert.equal(amountOf(2), '6,000.30');
  assert.equal(state(), 'ok', 'the two shares still add up to the whole bill');
  assert.match(hint(), /100\.00% of 100%/);
  record(snapshot('percent-from-amount', 'percent'));
});

check('custom amounts show each head’s percentage and what is left', () => {
  reset();
  tick([1, 2, 3]);
  setTotal('9000');
  setMode('custom');                     // the equal split comes across with it
  assert.equal(amountOf(1), '3,000.00');
  type(rows[1].amount, '4,500');
  assert.equal(percentOf(1), '50.00');
  assert.equal(state(), 'bad');
  assert.match(hint(), /1,500\.00 too much/);
  type(rows[2].amount, '1,500');
  assert.equal(percentOf(2), '16.67');
  assert.equal(amountOf(3), '3,000.00');
  assert.equal(percentOf(3), '33.33');
  assert.equal(state(), 'ok');
  assert.match(hint(), /adds up/);
  record(snapshot('custom-4500-3000-1500', 'custom'));
});

check('switching mode carries the figures across instead of wiping them', () => {
  reset();
  tick([1, 2, 3]);
  setTotal('9000');                       // equal → 3,000 each
  setMode('custom');
  assert.equal(amountOf(1), '3,000.00', 'the equal split is kept as a starting point');
  assert.equal(rows[1].amount.readOnly, false, 'custom amounts are the operator’s to type');
  setMode('percent');
  assert.equal(percentOf(1), '33.34');
  assert.equal(percentOf(2), '33.33');
  assert.equal(percentOf(3), '33.33');
  // 33.34% of 9,000 is 3,000.60 — a percentage carries two decimals, so the
  // rupees move by a few paisa and still add up to the whole bill.
  assert.equal(amountOf(1), '3,000.60');
  assert.equal(amountOf(2), '2,999.70');
  assert.equal(amountOf(3), '2,999.70');
  assert.equal(state(), 'ok');
  record(snapshot('mode-switch-equal-percent', 'percent'));
});

check('a figure that cannot be saved is named on screen', () => {
  reset();
  tick([1, 2]);
  setTotal('1000');
  setMode('percent');
  type(rows[1].percent, '150');
  type(rows[2].percent, '-50');
  assert.equal(state(), 'bad');
  assert.match(hint(), /cannot be negative/);

  setMode('custom');
  type(rows[1].amount, 'abc');
  assert.equal(state(), 'bad');
  assert.match(hint(), /check the amounts entered/);
  assert.equal(percentOf(1), '', 'a figure that is not a number shows no percentage');
});

check('a tinted box is the one the page works out, never the one being typed', () => {
  reset();
  tick([1, 2]);
  setTotal('1000');
  assert.equal(rows[1].amount.readOnly, true, 'an equal split is not for typing over');
  setMode('custom');
  assert.equal(rows[1].amount.readOnly, false);
  assert.equal(rows[1].percent.readOnly, true, 'the percentage follows the amount');
  setMode('percent');
  assert.equal(rows[1].percent.readOnly, false);
  assert.equal(rows[1].amount.readOnly, false, 'either column may be typed in');
});

// ── run ─────────────────────────────────────────────────────────────────────

let passed = 0;
for (const { name, fn } of checks) {
  try {
    fn();
    passed += 1;
  } catch (error) {
    console.error('FAIL: ' + name);
    console.error(error && error.stack ? error.stack : error);
    process.exit(1);
  }
}

if (outPath) {
  fs.writeFileSync(outPath, JSON.stringify({ cases }, null, 2));
}
console.log('shared expense split harness: ' + passed + ' checks passed');
