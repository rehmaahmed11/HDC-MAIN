/* HDC ERP — new_transaction_harness.js
 *
 * Runs the real New Transaction controller (static/hdc/js/pages/
 * new_transaction.js, on top of the real core/combo.js) against a minimal
 * hand-rolled DOM, and asserts the behaviour the redesign promises:
 *
 *   * Direction is the first decision: nothing else exists until it is made,
 *     and a transfer never shows category / party / project.
 *   * Category → Subcategory is a real dependency: switching category clears
 *     a subcategory that belonged to the old one.
 *   * "+ Add New …" creates over the same JSON contract and selects the new
 *     row *without* touching anything the user already typed.
 *   * The no-result state shows the empty text plus the add action.
 *   * Validation blocks the submit and puts the message next to the field;
 *     a valid submit happens once (the button is disabled while it does).
 *
 * Invoked by tests/test_new_transaction_form.py via `node`; exits non-zero on
 * the first failed assertion.
 *
 * Usage: node new_transaction_harness.js <repo_root>
 */

'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const repoRoot = process.argv[2];
if (!repoRoot) {
  console.error('usage: node new_transaction_harness.js <repo_root>');
  process.exit(2);
}

// ── Minimal DOM ─────────────────────────────────────────────────────────────

const observers = [];

function notifyMutation(kind, element, attributeName) {
  for (const obs of observers.slice()) {
    if (kind === 'childList' && !obs.options.childList) continue;
    if (kind === 'attributes') {
      if (!obs.options.attributes) continue;
      const filter = obs.options.attributeFilter;
      if (filter && filter.indexOf(attributeName) === -1) continue;
    }
    let node = element;
    let inScope = false;
    while (node) {
      if (node === obs.target) { inScope = true; break; }
      if (!obs.options.subtree) break;
      node = node.parentNode;
    }
    if (inScope) obs.callback([{ type: kind }], obs);
  }
}

class FakeEvent {
  constructor(type, options) {
    this.type = type;
    this.bubbles = !!(options && options.bubbles);
    this.target = null;
    this.key = (options && options.key) || '';
    this.defaultPrevented = false;
  }
  preventDefault() { this.defaultPrevented = true; }
  stopPropagation() {}
}

function makeClassList(el) {
  const set = new Set(String(el._attrs['class'] || '').split(/\s+/).filter(Boolean));
  function sync() { el._attrs['class'] = Array.from(set).join(' '); }
  return {
    add(c) { set.add(c); sync(); },
    remove(c) { set.delete(c); sync(); },
    toggle(c, force) {
      const on = force === undefined ? !set.has(c) : !!force;
      if (on) set.add(c); else set.delete(c);
      sync();
      return on;
    },
    contains(c) { return set.has(c); },
  };
}

function matches(el, selector) {
  const sel = selector.trim();
  if (!sel) return false;
  if (sel.charAt(0) === '#') return el.id === sel.slice(1);
  if (sel.charAt(0) === '.') return el.classList.contains(sel.slice(1));
  if (sel.charAt(0) === '[') {
    const body = sel.slice(1, -1);
    const eq = body.indexOf('=');
    if (eq === -1) return el.getAttribute(body) !== null;
    const name = body.slice(0, eq);
    const value = body.slice(eq + 1).replace(/^["']|["']$/g, '');
    return el.getAttribute(name) === value;
  }
  return el.tagName === sel.toUpperCase();
}

function makeElement(tag, id) {
  const el = {
    tagName: String(tag).toUpperCase(),
    id: id || '',
    name: '',
    type: '',
    placeholder: '',
    children: [],
    parentNode: null,
    dataset: {},
    style: {},
    hidden: false,
    disabled: false,
    required: false,
    selected: false,
    _attrs: {},
    _text: '',
    _listeners: {},
    _submitted: 0,
    value: '',
    selectedIndex: -1,
    get options() { return this._options || (this._options = []); },
    get classList() { return this._classList || (this._classList = makeClassList(this)); },
    get text() { return this._text; },
    set text(v) { this._text = String(v); notifyMutation('childList', this); },
    get textContent() { return this._text; },
    set textContent(v) { this._text = String(v); notifyMutation('childList', this); },
    get innerHTML() { return this._innerHTML || ''; },
    set innerHTML(v) {
      this._innerHTML = String(v);
      this.children = [];
      if (this._options) this._options = [];
      this.selectedIndex = -1;
      notifyMutation('childList', this);
    },
    get firstElementChild() { return this.children[0] || null; },
    get parentElement() { return this.parentNode; },
    getAttribute(name) {
      return Object.prototype.hasOwnProperty.call(this._attrs, name) ? this._attrs[name] : null;
    },
    setAttribute(name, value) { this._attrs[name] = String(value); notifyMutation('attributes', this, name); },
    removeAttribute(name) { delete this._attrs[name]; notifyMutation('attributes', this, name); },
    appendChild(child) {
      child.parentNode = this;
      this.children.push(child);
      if (this.tagName === 'SELECT' && child.tagName === 'OPTION') {
        this.options.push(child);
        if (child.selected) this.selectedIndex = this.options.length - 1;
      }
      notifyMutation('childList', this);
      return child;
    },
    insertBefore(node, ref) {
      node.parentNode = this;
      const idx = this.children.indexOf(ref);
      if (idx === -1) this.children.push(node); else this.children.splice(idx, 0, node);
      notifyMutation('childList', this);
      return node;
    },
    addEventListener(type, fn) {
      (this._listeners[type] || (this._listeners[type] = [])).push(fn);
    },
    dispatchEvent(ev) {
      if (!ev.target) ev.target = this;
      const list = (this._listeners[ev.type] || []).slice();
      for (const fn of list) fn(ev);
      return true;
    },
    focus() {
      documentStub.activeElement = this;
      this.dispatchEvent(new FakeEvent('focus'));
    },
    blur() {
      if (documentStub.activeElement === this) documentStub.activeElement = null;
      this.dispatchEvent(new FakeEvent('blur'));
    },
    scrollIntoView() {},
    getBoundingClientRect() {
      return { left: 10, right: 210, top: 100, bottom: 130, width: 200, height: 30 };
    },
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; },
    querySelectorAll(selector) {
      const found = [];
      const parts = String(selector).split(',').map(s => s.trim()).filter(Boolean);
      const walk = (node) => {
        for (const child of node.children) {
          if (parts.some(sel => matches(child, sel))) found.push(child);
          walk(child);
        }
      };
      walk(this);
      if (found.length) return found;
      // The combo menu is rendered as an HTML string; give the widget the
      // class-wrapped rows it expects to highlight.
      if (this._innerHTML && /hdc-combo-(item|add)/.test(String(selector))) {
        const count = (this._innerHTML.match(/hdc-combo-(item|add)/g) || []).length;
        for (let i = 0; i < count; i++) {
          found.push({
            classList: { toggle() {} },
            getAttribute(name) { return name === 'data-idx' ? String(i) : null; },
          });
        }
      }
      return found;
    },
    closest(selector) {
      let node = this;
      while (node) {
        if (matches(node, selector)) return node;
        node = node.parentNode;
      }
      return null;
    },
    removeChild(child) {
      const idx = this.children.indexOf(child);
      if (idx !== -1) this.children.splice(idx, 1);
      if (this._options) {
        const oi = this._options.indexOf(child);
        if (oi !== -1) this._options.splice(oi, 1);
      }
      child.parentNode = null;
      notifyMutation('childList', this);
      return child;
    },
    reset() {
      const walk = (node) => {
        for (const child of node.children) {
          if (['INPUT', 'TEXTAREA'].indexOf(child.tagName) !== -1) child.value = '';
          if (child.tagName === 'SELECT') {
            child.selectedIndex = child._defaultIndex == null ? (child.options.length ? 0 : -1) : child._defaultIndex;
          }
          walk(child);
        }
      };
      walk(this);
    },
    submit() { this._submitted += 1; },
  };
  if (el.tagName === 'SELECT') {
    Object.defineProperty(el, 'value', {
      get() {
        const opt = this.options[this.selectedIndex];
        return opt ? opt.value : '';
      },
      set(v) {
        const idx = this.options.findIndex(o => o.value === String(v));
        this.selectedIndex = idx;
      },
      configurable: true,
    });
  }
  return el;
}

function node(tag, id, props) {
  const el = makeElement(tag, id);
  Object.assign(el, props || {});
  registry.set(el.id, el);
  return el;
}

function option(parent, value, text, attrs) {
  const el = makeElement('option');
  el.value = String(value);
  el.text = String(text);
  Object.keys(attrs || {}).forEach((k) => el.setAttribute(k, attrs[k]));
  parent.appendChild(el);
  return el;
}

const registry = new Map();

const documentStub = {
  activeElement: null,
  readyState: 'complete',
  head: makeElement('head'),
  body: makeElement('body'),
  documentElement: { clientHeight: 800, clientWidth: 1200, setAttribute() {} },
  getElementById(id) {
    if (registry.has(id)) return registry.get(id);
    const found = documentStub.body.querySelectorAll('#' + id);
    if (found.length) { registry.set(id, found[0]); return found[0]; }
    return null;
  },
  createElement(tag) { return makeElement(tag); },
  addEventListener() {},
  querySelectorAll(selector) { return this.body.querySelectorAll(selector); },
  querySelector(selector) { return this.body.querySelector(selector); },
};

const modalLog = [];
const fetchLog = [];

const windowStub = {
  innerWidth: 1200,
  innerHeight: 800,
  document: documentStub,
  MutationObserver: class {
    constructor(callback) { this._cb = callback; }
    observe(target, options) { observers.push({ target, options, callback: this._cb }); }
    disconnect() {}
  },
  addEventListener() {},
  setTimeout(fn) { fn(); return 0; },
  Event: FakeEvent,
  fetch(url, init) {
    fetchLog.push({ url, init: init || {} });
    const queued = windowStub._nextResponse || { ok: true, json: { ok: true } };
    windowStub._nextResponse = null;
    return Promise.resolve({
      ok: queued.ok !== false,
      json: () => Promise.resolve(queued.json),
    });
  },
  bootstrap: {
    Modal: {
      getOrCreateInstance(element) {
        modalLog.push(element.id);
        return { show() {}, hide() {} };
      },
      getInstance() { return { show() {}, hide() {} }; },
    },
  },
};

// ── The form, built with the ids the shared partial renders ─────────────────

const FORM = node('form', 'hdcTxnForm');
FORM.setAttribute('data-hdc-txn-form', '');

const direction = node('select', 'txnDirection', { name: 'direction' });
option(direction, '', 'Choose…');
option(direction, 'in', 'Money In');
option(direction, 'out', 'Money Out');
option(direction, 'transfer', 'Internal Transfer');
FORM.appendChild(direction);

const directionGroup = node('div', 'txnDirectionGroup', { hidden: true });
['in', 'out', 'transfer'].forEach((value) => {
  const btn = makeElement('button');
  btn.setAttribute('data-direction', value);
  btn.classList.add('hdc-dir-btn');
  directionGroup.appendChild(btn);
});
FORM.appendChild(directionGroup);

const body = node('div', 'txnBody', { hidden: true });
FORM.appendChild(body);

const dateInput = node('input', 'txnDate');
dateInput.value = '2026-09-21';
body.appendChild(dateInput);

const amountInput = node('input', 'txnAmount');
body.appendChild(amountInput);
body.appendChild(node('span', 'txnAmountLegend'));

const amountError = node('div', 'txnAmountError');
amountError.classList.add('hdc-field-error');
amountError.setAttribute('data-error-for', 'amount');
body.appendChild(amountError);
const dateError = node('div', 'txnDateError');
dateError.classList.add('hdc-field-error');
dateError.setAttribute('data-error-for', 'date');
body.appendChild(dateError);

const accountField = node('div', 'txnAccountField');
const accountLabel = node('span', 'txnAccountLabel');
accountField.appendChild(accountLabel);
const accountInput = node('input', 'txnAccountInput');
accountField.appendChild(accountInput);
const accountSelect = node('select', 'txnAccount', { name: 'account_id' });
option(accountSelect, '', 'Choose…');
option(accountSelect, '1', 'MCB 1109 (Bank)');
option(accountSelect, '2', 'Company Cash (Cash)');
accountField.appendChild(accountSelect);
const accountError = node('div', 'txnAccountError');
accountError.classList.add('hdc-field-error');
accountError.setAttribute('data-error-for', 'account_id');
accountField.appendChild(accountError);
body.appendChild(accountField);

const toField = node('div', 'txnToField', { hidden: true });
const toInput = node('input', 'txnToAccountInput');
toField.appendChild(toInput);
const toSelect = node('select', 'txnToAccount', { name: 'destination_account_id' });
option(toSelect, '', 'Choose…');
option(toSelect, '1', 'MCB 1109 (Bank)');
option(toSelect, '2', 'Company Cash (Cash)');
toField.appendChild(toSelect);
const toError = node('div', 'txnToError');
toError.classList.add('hdc-field-error');
toError.setAttribute('data-error-for', 'destination_account_id');
toField.appendChild(toError);
body.appendChild(toField);

const categoryBlock = node('fieldset', 'txnCategoryBlock');
categoryBlock.appendChild(node('span', 'txnCategoryLegend'));
const categorySelect = node('select', 'txnCategory', { name: 'category_id' });
option(categorySelect, '', 'Choose…');
option(categorySelect, '10', 'Material & Purchase', { 'data-direction': 'out' });
option(categorySelect, '11', 'Labour & Wages', { 'data-direction': 'out' });
option(categorySelect, '20', 'Owner / Client Receipt', { 'data-direction': 'in' });
categoryBlock.appendChild(categorySelect);
const subcategorySelect = node('select', 'txnSubcategory', { name: 'subcategory_id' });
option(subcategorySelect, '', 'Choose…');
option(subcategorySelect, '101', 'Cement', { 'data-category': '10' });
option(subcategorySelect, '102', 'Steel / Saria', { 'data-category': '10' });
option(subcategorySelect, '111', 'Mason', { 'data-category': '11' });
option(subcategorySelect, '201', 'Project Payment', { 'data-category': '20' });
categoryBlock.appendChild(subcategorySelect);
const subcategoryHint = node('div', 'txnSubcategoryHint');
categoryBlock.appendChild(subcategoryHint);
const categoryError = node('div', 'txnCategoryError');
categoryError.classList.add('hdc-field-error');
categoryError.setAttribute('data-error-for', 'category_id');
categoryBlock.appendChild(categoryError);
const subcategoryError = node('div', 'txnSubcategoryError');
subcategoryError.classList.add('hdc-field-error');
subcategoryError.setAttribute('data-error-for', 'subcategory_id');
categoryBlock.appendChild(subcategoryError);
body.appendChild(categoryBlock);

const whoBlock = node('fieldset', 'txnWhoBlock');
const partyInput = node('input', 'txnPartyInput');
whoBlock.appendChild(partyInput);
const partySelect = node('select', 'txnParty', { name: 'party_name' });
option(partySelect, '', 'Choose…');
option(partySelect, 'Ahmed Cement Supplier', 'Ahmed Cement Supplier');
whoBlock.appendChild(partySelect);
const partyType = node('input', 'txnPartyType', { name: 'party_type' });
whoBlock.appendChild(partyType);
const partyError = node('div', 'txnPartyError');
partyError.classList.add('hdc-field-error');
partyError.setAttribute('data-error-for', 'party_name');
whoBlock.appendChild(partyError);
const projectInput = node('input', 'txnProjectInput');
whoBlock.appendChild(projectInput);
const projectSelect = node('select', 'txnProject', { name: 'project_id' });
option(projectSelect, '', 'Choose…');
option(projectSelect, '5', 'JPS KhanPur 5 Marla');
whoBlock.appendChild(projectSelect);
const projectError = node('div', 'txnProjectError');
projectError.classList.add('hdc-field-error');
projectError.setAttribute('data-error-for', 'project_id');
whoBlock.appendChild(projectError);
body.appendChild(whoBlock);

const referenceInput = node('input', 'txnReference', { name: 'reference' });
body.appendChild(referenceInput);
const descriptionInput = node('input', 'txnDescription', { name: 'description' });
body.appendChild(descriptionInput);
const noteInput = node('textarea', 'txnNote', { name: 'note' });
body.appendChild(noteInput);

const actionHint = node('div', 'txnActionHint');
body.appendChild(actionHint);
const resetButton = node('button', 'txnResetBtn', { hidden: true });
body.appendChild(resetButton);
const saveButton = node('button', 'txnSaveBtn');
const saveIdle = node('span', 'txnSaveIdle');
saveIdle.classList.add('hdc-save-idle');
saveButton.appendChild(saveIdle);
const saveBusy = node('span', 'txnSaveBusy', { hidden: true });
saveBusy.classList.add('hdc-save-busy');
saveButton.appendChild(saveBusy);
body.appendChild(saveButton);
const serverError = node('div', 'txnServerError', { hidden: true });
body.appendChild(serverError);

// Add-new modals: the ids the partial renders.
const newAccountModal = node('div', 'txnNewAccountModal');
const newAccountForm = node('form', 'txnNewAccountForm');
const naName = node('input', 'txnNaName');
const naMode = node('select', 'txnNaMode');
option(naMode, 'cash', 'Cash');
option(naMode, 'bank', 'Bank');
const naBankFields = node('div', 'txnNaBankFields', { hidden: true });
const naBankName = node('input', 'txnNaBankName');
const naBankNumber = node('input', 'txnNaBankNumber');
const naOpening = node('input', 'txnNaOpening');
naOpening.value = '0';
const naError = node('div', 'txnNaError');
naError.setAttribute('data-modal-error', '');
[naName, naMode, naBankFields, naOpening, naError].forEach((el) => newAccountForm.appendChild(el));
naBankFields.appendChild(naBankName);
naBankFields.appendChild(naBankNumber);
newAccountModal.appendChild(newAccountForm);
FORM.appendChild(newAccountModal);

const newPartyModal = node('div', 'txnNewPartyModal');
const newPartyForm = node('form', 'txnNewPartyForm');
const npName = node('input', 'txnNpName');
const npType = node('select', 'txnNpType');
option(npType, 'client', 'Client');
option(npType, 'supplier', 'Supplier');
const npPhone = node('input', 'txnNpPhone');
const npError = node('div', 'txnNpError');
npError.setAttribute('data-modal-error', '');
[naName && npName, npType, npPhone, npError].forEach((el) => newPartyForm.appendChild(el));
newPartyModal.appendChild(newPartyForm);
FORM.appendChild(newPartyModal);

const newProjectModal = node('div', 'txnNewProjectModal');
const newProjectForm = node('form', 'txnNewProjectForm');
const nprName = node('input', 'txnNprName');
const nprClient = node('input', 'txnNprClient');
const nprLocation = node('input', 'txnNprLocation');
const nprError = node('div', 'txnNprError');
nprError.setAttribute('data-modal-error', '');
[nprName, nprClient, nprLocation, nprError].forEach((el) => newProjectForm.appendChild(el));
newProjectModal.appendChild(newProjectForm);
FORM.appendChild(newProjectModal);

documentStub.body.appendChild(FORM);

// ── Load the real scripts ───────────────────────────────────────────────────

const sandbox = {
  window: windowStub,
  document: documentStub,
  console,
  setTimeout: windowStub.setTimeout,
  clearTimeout() {},
  Event: FakeEvent,
  MutationObserver: windowStub.MutationObserver,
  fetch: windowStub.fetch,
  bootstrap: windowStub.bootstrap,
  alert() {},
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);

['static/hdc/js/core/combo.js', 'static/hdc/js/pages/new_transaction.js'].forEach((rel) => {
  vm.runInContext(fs.readFileSync(path.join(repoRoot, rel), 'utf8'), sandbox, { filename: rel });
});
assert.ok(sandbox.window.HDCComboList, 'core/combo.js must expose HDCComboList');

const tick = () => new Promise((resolve) => setImmediate(resolve));
const menu = (id) => documentStub.getElementById(id);
const menuHtml = (id) => {
  const el = menu(id);
  return el ? el.innerHTML : '';
};
const type = (input, text) => {
  input.value = text;
  input.dispatchEvent(new FakeEvent('input'));
};
const press = (input, key) => input.dispatchEvent(new FakeEvent('keydown', { key }));
const change = (el) => el.dispatchEvent(new FakeEvent('change'));

const checks = [];
function check(name, fn) { checks.push({ name, fn }); }

// ── 1. Direction is the first decision ──────────────────────────────────────

check('nothing but the direction is shown until one is chosen', () => {
  assert.equal(body.hidden, true, 'the field body must stay hidden');
  assert.equal(actionHint.textContent, 'Choose a direction to start.');
  assert.equal(directionGroup.hidden, false, 'the segmented buttons replace the select');
});

check('Money Out shows category/party/project and no To account', () => {
  direction.value = 'out';
  change(direction);
  assert.equal(body.hidden, false);
  assert.equal(categoryBlock.hidden, false);
  assert.equal(whoBlock.hidden, false);
  assert.equal(toField.hidden, true);
  assert.equal(toSelect.disabled, true, 'an unused To account must never be postable');
  assert.equal(categorySelect.disabled, false);
  assert.match(accountLabel.innerHTML, /Account/);
  assert.equal(actionHint.textContent.indexOf('Category drives the subcategory list') !== -1, true);
});

check('a transfer hides category and party and asks for the second account', () => {
  direction.value = 'transfer';
  change(direction);
  assert.equal(categoryBlock.hidden, true);
  assert.equal(whoBlock.hidden, true);
  assert.equal(categorySelect.disabled, true);
  assert.equal(partySelect.disabled, true);
  assert.equal(projectSelect.disabled, true);
  assert.equal(toField.hidden, false);
  assert.equal(toSelect.disabled, false);
  assert.match(accountLabel.innerHTML, /From account/);
  direction.value = 'out';
  change(direction);
  assert.equal(toSelect.disabled, true);
});

// ── 2. Category → Subcategory dependency ────────────────────────────────────

check('selecting a subcategory then switching category clears it', () => {
  direction.value = 'out';
  change(direction);
  categorySelect.value = '10';
  change(categorySelect);
  const mason = subcategorySelect.options.find(o => o.value === '111');
  assert.equal(mason.hidden, true, 'another category\'s subcategory is not offered');
  assert.equal(mason.disabled, true);
  subcategorySelect.value = '101';           // Cement ∈ Material & Purchase
  change(subcategorySelect);
  assert.equal(subcategorySelect.value, '101');
  categorySelect.value = '11';               // switch to Labour & Wages
  change(categorySelect);
  assert.equal(subcategorySelect.value, '', 'a stale subcategory must not survive');
  assert.equal(subcategorySelect.options.find(o => o.value === '101').disabled, true);
  assert.equal(subcategorySelect.options.find(o => o.value === '111').disabled, false);
  categorySelect.value = '10';
  change(categorySelect);
});

check('the wrong-direction category is not offered for Money In', () => {
  direction.value = 'in';
  change(direction);
  assert.equal(categorySelect.options.find(o => o.value === '10').disabled, true,
    'an expense category must not be selectable on a receipt');
  assert.equal(categorySelect.options.find(o => o.value === '20').disabled, false);
  direction.value = 'out';
  change(direction);
  assert.equal(categorySelect.options.find(o => o.value === '20').disabled, true);
  assert.equal(categorySelect.options.find(o => o.value === '10').disabled, false);
});

// ── 3. Validation keeps the message next to the field ───────────────────────

check('an empty/invalid form cannot be submitted and the errors sit by the fields', () => {
  direction.value = 'out';
  change(direction);
  dateInput.value = '';
  amountInput.value = '0';
  accountSelect.value = '';
  categorySelect.value = '';
  FORM.dispatchEvent(new FakeEvent('submit'));
  assert.equal(FORM._submitted, 0, 'an invalid form must not submit');
  assert.equal(dateError.textContent, 'Date is required.');
  assert.equal(dateError.hidden, false);
  assert.equal(amountError.textContent, 'Amount must be greater than 0.');
  assert.equal(accountError.textContent, 'Choose the account.');
  assert.equal(categoryError.textContent, 'Choose an expense category.');
});

check('a valid Money Out posts once and locks the button while it does', () => {
  dateInput.value = '2026-09-21';
  amountInput.value = '25,000';
  accountSelect.value = '1';
  change(accountSelect);
  categorySelect.value = '10';
  change(categorySelect);
  subcategorySelect.value = '101';
  change(subcategorySelect);
  FORM.dispatchEvent(new FakeEvent('submit'));
  assert.equal(FORM._submitted, 1);
  assert.equal(saveButton.disabled, true, 'the save button is locked against a double click');
  assert.equal(saveBusy.hidden, false);
  assert.equal(saveIdle.hidden, true);
  saveButton.disabled = false;
});

check('a transfer to the same account is refused', () => {
  direction.value = 'transfer';
  change(direction);
  accountSelect.value = '2';
  change(accountSelect);
  toSelect.value = '2';
  change(toSelect);
  const before = FORM._submitted;
  FORM.dispatchEvent(new FakeEvent('submit'));
  assert.equal(FORM._submitted, before, 'same-account transfers must not submit');
  assert.equal(toError.textContent, 'From and To must be different accounts.');
  direction.value = 'out';
  change(direction);
});

// ── 4. The searchable pickers ───────────────────────────────────────────────

check('typing filters, and the no-result state offers the add action', () => {
  accountInput.focus();
  type(accountInput, 'zzz');
  const html = menuHtml('txnAccount_menu');
  assert.match(html, /No accounts found\./);
  assert.match(html, /\+ Add New Account/);
  type(accountInput, 'mcb');
  assert.match(menuHtml('txnAccount_menu'), /MCB 1109 \(Bank\)/);
  press(accountInput, 'Enter');
  assert.equal(accountSelect.value, '1', 'Enter selects the highlighted account');
  assert.equal(accountInput.value, 'MCB 1109 (Bank)');
  press(accountInput, 'Escape');
});

// ── 5. Add-on-the-fly keeps everything else ─────────────────────────────────

check('adding an account keeps every other field and selects the new row', async () => {
  direction.value = 'out';
  change(direction);
  dateInput.value = '2026-09-21';
  amountInput.value = '25,000';
  accountSelect.value = '1';
  change(accountSelect);
  accountInput.value = 'MCB 1109 (Bank)';
  categorySelect.value = '10';
  change(categorySelect);
  subcategorySelect.value = '101';
  change(subcategorySelect);
  partySelect.value = 'Ahmed Cement Supplier';
  change(partySelect);
  projectSelect.value = '5';
  change(projectSelect);
  referenceInput.value = 'SLIP-9';
  descriptionInput.value = 'Cement 50 bags';
  noteInput.value = 'keep me';

  // The user types a name that is not in the list and picks "+ Add New …".
  accountInput.focus();
  type(accountInput, 'Easypaisa Rizwan');
  press(accountInput, 'ArrowDown');   // the action row follows the single match
  press(accountInput, 'Enter');
  assert.equal(modalLog.indexOf('txnNewAccountModal') !== -1, true,
    'the add row opens the compact modal instead of leaving the page');

  // The modal prefills what was typed, then saves over JSON.
  naName.value = 'Easypaisa Rizwan';
  naMode.value = 'cash';
  windowStub._nextResponse = {
    ok: true,
    json: {
      ok: true, created: true, message: 'Account added.',
      item: { id: 77, name: 'Easypaisa Rizwan', label: 'Easypaisa Rizwan (Cash)', type: 'cash' },
    },
  };
  newAccountForm.dispatchEvent(new FakeEvent('submit'));
  await tick();
  await tick();

  assert.equal(fetchLog.length, 1, 'the create must be a single JSON call');
  assert.equal(fetchLog[0].url, '/hdc/accounts/new-transaction/account');
  const created = accountSelect.options.find(o => o.value === '77');
  assert.ok(created, 'the new account is appended to the select');
  assert.equal(accountSelect.value, '77', 'and auto-selected');
  assert.equal(accountInput.value, 'Easypaisa Rizwan (Cash)', 'the visible input mirrors it');

  // Nothing else moved.
  assert.equal(dateInput.value, '2026-09-21');
  assert.equal(amountInput.value, '25,000');
  assert.equal(categorySelect.value, '10');
  assert.equal(subcategorySelect.value, '101');
  assert.equal(partySelect.value, 'Ahmed Cement Supplier');
  assert.equal(projectSelect.value, '5');
  assert.equal(referenceInput.value, 'SLIP-9');
  assert.equal(descriptionInput.value, 'Cement 50 bags');
  assert.equal(noteInput.value, 'keep me');
  assert.match(actionHint.textContent, /Account added\./);
});

check('a failed create keeps the modal open and reports the reason', async () => {
  accountInput.focus();
  type(accountInput, 'Bad Bank');
  press(accountInput, 'ArrowDown');
  press(accountInput, 'Enter');
  naName.value = 'Bad Bank';
  naMode.value = 'bank';
  naBankName.value = '';
  windowStub._nextResponse = {
    ok: false,
    json: { ok: false, message: 'bank_name is required for bank accounts.' },
  };
  newAccountForm.dispatchEvent(new FakeEvent('submit'));
  await tick();
  await tick();
  assert.equal(naError.textContent, 'bank_name is required for bank accounts.');
  assert.equal(naError.hidden, false, 'the modal stays open with the reason');
  assert.equal(accountSelect.options.some(o => o.text === 'Bad Bank'), false,
    'a refused create never reaches the picker');
  // The transaction itself is untouched by the failed create.
  assert.equal(dateInput.value, '2026-09-21');
  assert.equal(amountInput.value, '25,000');
  assert.equal(referenceInput.value, 'SLIP-9');
  assert.equal(descriptionInput.value, 'Cement 50 bags');
  assert.equal(noteInput.value, 'keep me');
  assert.equal(categorySelect.value, '10');
  assert.equal(subcategorySelect.value, '101');
});

// ── run ─────────────────────────────────────────────────────────────────────

(async () => {
  let passed = 0;
  for (const { name, fn } of checks) {
    try {
      await fn();
      passed += 1;
    } catch (error) {
      console.error('FAIL: ' + name);
      console.error(error && error.stack ? error.stack : error);
      process.exit(1);
    }
  }
  console.log('new transaction harness: ' + passed + ' checks passed');
})();
