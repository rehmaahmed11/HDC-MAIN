/* HDC ERP — combo_widget_harness.js
 *
 * Runs static/hdc/js/core/combo.js against a minimal hand-rolled DOM and
 * asserts the searchable-combo behaviours the Accounts Hub transaction forms
 * rely on.  Invoked by tests/test_combo_widget.py via `node`; exits non-zero
 * on the first failed assertion.
 *
 * Usage: node combo_widget_harness.js <repo_root>
 */

'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const repoRoot = process.argv[2];
if (!repoRoot) {
  console.error('usage: node combo_widget_harness.js <repo_root>');
  process.exit(2);
}

// ── Minimal DOM ─────────────────────────────────────────────────────────────

const observers = []; // { target, options, callback }

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
    if (inScope) obs.callback([]);
  }
}

class FakeEvent {
  constructor(type, opts) {
    this.type = type;
    this.bubbles = !!(opts && opts.bubbles);
    this.target = null;
    this.key = (opts && opts.key) || '';
    this.defaultPrevented = false;
  }
  preventDefault() { this.defaultPrevented = true; }
}

function makeClassList(el) {
  const set = new Set(
    String(el._attrs['class'] || '').split(/\s+/).filter(Boolean));
  return {
    add(c) { set.add(c); syncClassAttr(); },
    remove(c) { set.delete(c); syncClassAttr(); },
    toggle(c, force) {
      const on = force === undefined ? !set.has(c) : !!force;
      if (on) set.add(c); else set.delete(c);
      syncClassAttr();
      return on;
    },
    contains(c) { return set.has(c); },
  };
  function syncClassAttr() { el._attrs['class'] = Array.from(set).join(' '); }
}

function makeElement(tag, id) {
  const el = {
    tagName: String(tag).toUpperCase(),
    id: id || '',
    children: [],
    parentNode: null,
    _attrs: {},
    _text: '',
    _listeners: {},
    style: {},
    value: '',
    required: false,
    selectedIndex: -1,
    get options() { return this._options || (this._options = []); },
    get classList() { return this._classList || (this._classList = makeClassList(this)); },
    get className() { return this._attrs['class'] || ''; },
    set className(v) { this._attrs['class'] = String(v); this._classList = null; },
    get text() { return this._text; },
    set text(v) {
      this._text = String(v);
      if (this.tagName === 'OPTION') notifyMutation('childList', this);
    },
    get textContent() { return this._text; },
    set textContent(v) {
      this._text = String(v);
      notifyMutation('childList', this);
    },
    get innerHTML() { return this._innerHTML || ''; },
    set innerHTML(v) {
      this._innerHTML = String(v);
      this.children = [];
      if (this._options) this._options = [];
      this.selectedIndex = -1;
      notifyMutation('childList', this);
    },
    getAttribute(name) { return Object.prototype.hasOwnProperty.call(this._attrs, name) ? this._attrs[name] : null; },
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
      ev.target = this;
      const list = (this._listeners[ev.type] || []).slice();
      for (const fn of list) fn(ev);
      return true;
    },
    getBoundingClientRect() { return { left: 10, right: 110, top: 100, bottom: 120, width: 100, height: 20 }; },
    querySelectorAll(selector) {
      if (selector === '.hdc-combo-item') {
        const html = this._innerHTML || '';
        const count = (html.match(/hdc-combo-item/g) || []).length;
        const stubs = [];
        for (let i = 0; i < count; i++) {
          stubs.push({ classList: { toggle() {} } });
        }
        return stubs;
      }
      return [];
    },
    closest() { return null; },
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
    Object.defineProperty(el, 'required', {
      get() { return Object.prototype.hasOwnProperty.call(this._attrs, 'required'); },
      set(v) {
        if (v) this.setAttribute('required', ''); else this.removeAttribute('required');
      },
      configurable: true,
    });
  }
  return el;
}

function mkopt(value, text, selected) {
  const o = makeElement('option');
  o.value = String(value);
  o.text = String(text);
  o.selected = !!selected;
  return o;
}

const registry = new Map();
function register(el) { registry.set(el.id, el); return el; }

const documentStub = {
  activeElement: null,
  head: makeElement('head'),
  body: makeElement('body'),
  getElementById(id) {
    if (!registry.has(id)) return null; // e.g. the one-time <style> probe
    return registry.get(id);
  },
  createElement(tag) { return makeElement(tag); },
};

const windowStub = {
  innerWidth: 1200,
  innerHeight: 800,
  MutationObserver: class {
    constructor(callback) { this._cb = callback; this._target = null; this._options = null; }
    observe(target, options) {
      this._target = target;
      this._options = options;
      observers.push({ target, options, callback: this._cb });
    }
    disconnect() {}
  },
  addEventListener() {},
  Event: FakeEvent,
};

// A container so the combo can wrap the input.
function buildField(inputId, selectId, opts) {
  const container = makeElement('div');
  const input = register(makeElement('input', inputId));
  const select = register(makeElement('select', selectId));
  container.appendChild(input);
  container.appendChild(select);
  (opts || []).forEach(o => select.appendChild(o));
  if (select.options.some(o => o.selected)) {
    select.selectedIndex = select.options.findIndex(o => o.selected);
  } else {
    select.selectedIndex = select.options.length ? 0 : -1;
  }
  return { container, input, select };
}

function flushMicrotasks() {
  return new Promise(resolve => setImmediate(resolve));
}
function wait(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

// ── Load the widget ─────────────────────────────────────────────────────────

global.window = windowStub;
global.document = documentStub;
global.MutationObserver = windowStub.MutationObserver;
global.Event = FakeEvent;

const comboSource = fs.readFileSync(
  path.join(repoRoot, 'static', 'hdc', 'js', 'core', 'combo.js'), 'utf-8');
// eslint-disable-next-line no-eval
eval(comboSource);

const HDCComboList = windowStub.HDCComboList;
assert.ok(HDCComboList, 'combo.js must define window.HDCComboList');

function menuVisible(m) { return m && !m.classList.contains('d-none'); }
function menuHtml(m) { return m._innerHTML || ''; }
function comboMenus() {
  return documentStub.body.children.filter(c => c.classList.contains('hdc-combo-menu'));
}

// ── Behaviour assertions ────────────────────────────────────────────────────

(async function main() {
  // 1. attach hides the select and leaves the input visible.
  const f1 = buildField('inp1', 'sel1', [
    mkopt('', 'Select'),
    mkopt('1', 'Alice (W-1)'),
    mkopt('2', 'Bob (W-2)'),
  ]);
  const combo1 = HDCComboList.attach('inp1', 'sel1', { strict: true });
  assert.ok(combo1, 'attach returns the widget handle');
  const menu = comboMenus()[0];
  assert.ok(menu, 'attach appends the menu to <body>');
  assert.ok(f1.select.classList.contains('d-none'), 'select is hidden');
  assert.ok(!f1.input.classList.contains('d-none'), 'input stays visible');
  await flushMicrotasks();

  // 2. focus opens a menu without the empty placeholder (includeEmpty off).
  documentStub.activeElement = f1.input;
  f1.input.dispatchEvent(new FakeEvent('focus'));
  assert.ok(menuVisible(menu), 'menu opens on focus');
  assert.equal((menuHtml(menu).match(/hdc-combo-item/g) || []).length, 2,
    'placeholder excluded by default');

  // 3. typing filters and clears the select value.
  f1.input.value = 'bob';
  f1.input.dispatchEvent(new FakeEvent('input'));
  assert.equal(f1.select.value, '', 'typing clears the select value');
  assert.equal((menuHtml(menu).match(/hdc-combo-item/g) || []).length, 1,
    'filter narrows to Bob only');
  assert.ok(/Bob \(W-2\)/.test(menuHtml(menu)), 'menu shows the match');

  // 4. Enter picks: select value, input text and change event all line up.
  let changeCount = 0;
  f1.select.addEventListener('change', () => { changeCount += 1; });
  f1.input.dispatchEvent(new FakeEvent('keydown', { key: 'Enter' }));
  assert.equal(f1.select.value, '2', 'Enter picks the highlighted match');
  assert.equal(f1.input.value, 'Bob (W-2)', 'input mirrors the picked label');
  assert.ok(changeCount >= 1, 'pick fires change on the select');
  assert.ok(!menuVisible(menu), 'menu closes after pick');

  // 5. strict blur with unmatched text reverts to the selection.
  f1.input.value = 'zzz-no-such-party';
  f1.input.dispatchEvent(new FakeEvent('input'));
  f1.input.dispatchEvent(new FakeEvent('blur'));
  documentStub.activeElement = null;
  await wait(200);
  assert.equal(f1.select.value, '', 'unmatched text clears the select');
  assert.equal(f1.input.value, '', 'strict blur reverts the input text');

  // 6. strict blur with an exact (case-insensitive) match selects it.
  documentStub.activeElement = f1.input;
  f1.input.dispatchEvent(new FakeEvent('focus'));
  f1.input.value = 'alice (w-1)';
  f1.input.dispatchEvent(new FakeEvent('input'));
  f1.input.dispatchEvent(new FakeEvent('blur'));
  documentStub.activeElement = null;
  await wait(200);
  assert.equal(f1.select.value, '1', 'exact text selects on blur');
  assert.equal(f1.input.value, 'Alice (W-1)', 'input is normalised to the option label');

  // 7. option rebuilds by page code mirror into the input.
  f1.select.innerHTML = '';
  f1.select.appendChild(mkopt('1', 'Alice (W-1)'));
  f1.select.appendChild(mkopt('2', 'Bob (W-2)', true));
  await flushMicrotasks();
  assert.equal(f1.input.value, 'Bob (W-2)', 'rebuild with kept value syncs the input');

  // 8. public syncFromSelect mirrors a direct value assignment.
  f1.select.value = '1';
  combo1.syncFromSelect();
  assert.equal(f1.input.value, 'Alice (W-1)', 'syncFromSelect mirrors the select');

  // 9. required ownership moves to the visible input.
  const f2 = buildField('inp2', 'sel2', [
    mkopt('', 'Select'),
    mkopt('10', 'Cash Drawer'),
  ]);
  f2.select.required = true;
  observers.length = 0; // attach-time echo already consumed
  const combo2 = HDCComboList.attach('inp2', 'sel2', { strict: true });
  await flushMicrotasks();
  assert.equal(f2.input.required, true, 'input inherits required');
  assert.equal(f2.select.required, false, 'hidden select no longer owns required');
  // Page code (updateTxnUI) re-sets select.required; mirror it again.
  f2.select.required = true;
  await flushMicrotasks();
  await wait(10);
  assert.equal(f2.input.required, true, 'later required=true is mirrored');
  assert.equal(f2.select.required, false, 'and transferred off the select again');
  assert.ok(combo2.syncFromSelect, 'handle exposes syncFromSelect');

  // 10. includeEmpty keeps the placeholder pickable (e.g. "Off-ledger Party").
  const f3 = buildField('inp3', 'sel3', [
    mkopt('', 'Off-ledger Party'),
    mkopt('21', 'Supplier X'),
  ]);
  HDCComboList.attach('inp3', 'sel3', { strict: true, includeEmpty: true });
  const menu3 = comboMenus()[comboMenus().length - 1];
  await flushMicrotasks();
  documentStub.activeElement = f3.input;
  f3.input.dispatchEvent(new FakeEvent('focus'));
  assert.equal((menuHtml(menu3).match(/hdc-combo-item/g) || []).length, 2,
    'includeEmpty shows the placeholder row');
  // picking the placeholder clears the selection
  f3.input.value = 'off-ledger';
  f3.input.dispatchEvent(new FakeEvent('input'));
  f3.input.dispatchEvent(new FakeEvent('blur'));
  documentStub.activeElement = null;
  await wait(200);
  assert.equal(f3.select.value, '', 'picking/typing the placeholder clears the select');

  console.log('combo widget harness: all assertions passed');
})().catch(err => {
  console.error(err && err.stack ? err.stack : err);
  process.exit(1);
});
