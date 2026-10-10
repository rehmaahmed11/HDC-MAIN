/* HDC ERP — dialog_harness.js
 *
 * Runs static/hdc/js/core/dialog.js — with core/forms.js loaded beside it,
 * because the two interact on every submit — against a minimal hand-rolled DOM
 * and asserts the entry-result dialogue box behaviours the Purchases, Delivery
 * and Usage screens depend on.  Invoked by tests/test_purchase_entry_dialog.py
 * via `node`; exits non-zero on the first failed assertion.
 *
 * Only the DOM surface those two files touch is implemented: element and text
 * nodes, class/attribute/dataset handling, a small selector matcher, and event
 * dispatch in capture then bubble order.  Bootstrap's modal is stubbed, and —
 * like the real widget — it reports `shown.bs.modal` only after the harness
 * flushes it, so code that waits for the fade-in is exercised properly.
 *
 * Usage: node dialog_harness.js <repo_root>
 */

'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const repoRoot = process.argv[2];
if (!repoRoot) {
  console.error('usage: node dialog_harness.js <repo_root>');
  process.exit(2);
}

const SOURCES = {
  dialog: fs.readFileSync(path.join(repoRoot, 'static/hdc/js/core/dialog.js'), 'utf8'),
  forms: fs.readFileSync(path.join(repoRoot, 'static/hdc/js/core/forms.js'), 'utf8'),
};

// ── minimal DOM ─────────────────────────────────────────────────────────────

function text(content) {
  return { nodeType: 3, textContent: String(content), parentNode: null };
}

class El {
  constructor(tag) {
    this.nodeType = 1;
    this.tagName = String(tag).toUpperCase();
    this.childNodes = [];
    this.parentNode = null;
    this.attrs = Object.create(null);
    this.dataset = Object.create(null);
    this.listeners = Object.create(null);
    this._text = '';
    this.classList = makeClassList(this);
  }

  get className() { return this.attrs.class || ''; }
  set className(v) { this.attrs.class = String(v); }

  get id() { return this.attrs.id || ''; }
  set id(v) { this.attrs.id = String(v); }

  get firstChild() { return this.childNodes[0] || null; }
  get hidden() { return this._hidden === true; }
  set hidden(v) { this._hidden = !!v; }

  get textContent() {
    if (!this.childNodes.length) return this._text;
    return this.childNodes.map((c) => (c && c.textContent !== undefined ? c.textContent : String(c))).join(' ');
  }
  set textContent(v) {
    this.childNodes = [];
    this._text = String(v);
  }

  setAttribute(name, value) { this.attrs[name] = String(value); }
  getAttribute(name) { return name in this.attrs ? this.attrs[name] : null; }
  hasAttribute(name) { return name in this.attrs; }
  removeAttribute(name) { delete this.attrs[name]; }

  appendChild(child) {
    if (child && child.parentNode) child.parentNode.removeChild(child);
    if (child) child.parentNode = this;
    this.childNodes.push(child);
    return child;
  }

  removeChild(child) {
    const at = this.childNodes.indexOf(child);
    if (at !== -1) this.childNodes.splice(at, 1);
    if (child) child.parentNode = null;
    return child;
  }

  addEventListener(type, fn, capture) {
    const key = (capture ? 'capture:' : 'bubble:') + type;
    (this.listeners[key] = this.listeners[key] || []).push(fn);
  }

  removeEventListener(type, fn, capture) {
    const key = (capture ? 'capture:' : 'bubble:') + type;
    const list = this.listeners[key] || [];
    const at = list.indexOf(fn);
    if (at !== -1) list.splice(at, 1);
  }

  fire(type, event) {
    const listeners = (this.listeners['capture:' + type] || []).concat(this.listeners['bubble:' + type] || []);
    for (const fn of listeners.slice()) {
      fn.call(this, event);
      if (event._stopped) return;
    }
  }

  matches(selector) { return matchesSelector(this, selector); }

  contains(node) {
    let found = false;
    this.walk((child) => { if (child === node) found = true; });
    return found || node === this;
  }

  closest(selector) {
    let node = this;
    while (node) {
      if (node.matches && node.matches(selector)) return node;
      node = node.parentNode;
    }
    return null;
  }

  walk(visit) {
    for (const child of this.childNodes) {
      if (!child || !(child instanceof El)) continue;
      visit(child);
      child.walk(visit);
    }
  }

  querySelectorAll(selector) {
    const found = [];
    this.walk((node) => { if (node.matches(selector)) found.push(node); });
    return found;
  }

  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }

  focus() { if (this._doc) this._doc.activeElement = this; }
  blur() { if (this._doc && this._doc.activeElement === this) this._doc.activeElement = null; }
  scrollIntoView() { this._scrolled = true; }
}

function makeClassList(owner) {
  return {
    _tokens() { return (owner.attrs.class || '').split(/\s+/).filter(Boolean); },
    _write(tokens) { owner.attrs.class = tokens.join(' '); },
    add(...names) {
      const tokens = this._tokens();
      names.forEach((n) => { if (tokens.indexOf(n) === -1) tokens.push(n); });
      this._write(tokens);
    },
    remove(...names) { this._write(this._tokens().filter((t) => names.indexOf(t) === -1)); },
    contains(name) { return this._tokens().indexOf(name) !== -1; },
    toggle(name, force) {
      const on = force === undefined ? !this.contains(name) : !!force;
      if (on) this.add(name); else this.remove(name);
      return on;
    },
  };
}

function matchesSelector(node, selector) {
  return String(selector).split(',').map((s) => s.trim()).some((part) => {
    const m = /^([a-zA-Z]*)((?:\[[^\]]+\]|(?:\.[A-Za-z0-9_-]+)|(?:#[A-Za-z0-9_-]+))*)$/.exec(part);
    if (!m) return false;
    if (m[1] && node.tagName !== m[1].toUpperCase()) return false;
    const chunks = m[2].match(/\[[^\]]+\]|\.[A-Za-z0-9_-]+|#[A-Za-z0-9_-]+/g) || [];
    return chunks.every((chunk) => {
      if (chunk[0] === '#') return node.id === chunk.slice(1);
      if (chunk[0] === '.') return node.classList.contains(chunk.slice(1));
      const attr = /^\[([^\]=]+)(?:=["']?([^"']*)["']?)?\]$/.exec(chunk);
      if (!attr) return false;
      if (attr[2] === undefined) return node.hasAttribute(attr[1]);
      return node.getAttribute(attr[1]) === attr[2];
    });
  });
}

function makeDocument() {
  const doc = {
    readyState: 'complete',
    activeElement: null,
    listeners: Object.create(null),
    createElement(tag) { const el = new El(tag); el._doc = doc; return el; },
    createTextNode(content) { return text(content); },
    _all() {
      const out = [doc.body];
      doc.body.walk((node) => out.push(node));
      return out;
    },
    getElementById(id) { return doc._all().find((node) => node.id === id) || null; },
    contains(node) { return doc._all().indexOf(node) !== -1; },
    addEventListener(type, fn, capture) {
      const key = (capture ? 'capture:' : 'bubble:') + type;
      (doc.listeners[key] = doc.listeners[key] || []).push(fn);
    },
    removeEventListener(type, fn, capture) {
      const key = (capture ? 'capture:' : 'bubble:') + type;
      const list = doc.listeners[key] || [];
      const at = list.indexOf(fn);
      if (at !== -1) list.splice(at, 1);
    },
    _fire(type, event) {
      ['capture', 'bubble'].forEach((phase) => {
        (doc.listeners[phase + ':' + type] || []).slice().forEach((fn) => {
          if (event._stopped) return;
          fn.call(doc, event);
        });
      });
    },
    querySelectorAll(selector) { return doc._all().filter((node) => node.matches(selector)); },
    querySelector(selector) { return doc.querySelectorAll(selector)[0] || null; },
  };
  doc.body = new El('body');
  doc.body._doc = doc;
  return doc;
}

/** A field, with the constraint-validation state a browser would report. */
function field(props) {
  const el = new El(props.tag || 'input');
  el.name = props.name || '';
  el.type = props.type || 'text';
  el.value = props.value === undefined ? '' : props.value;
  el.disabled = !!props.disabled;
  el.willValidate = props.willValidate !== false;
  el.validity = props.validity || { valueMissing: false, rangeUnderflow: false };
  el.validationMessage = props.validationMessage || 'please check this value.';
  el._valid = props.valid !== false;
  el.checkValidity = () => el._valid;
  if (props.id) el.setAttribute('id', props.id);
  if (props.label) {
    const lab = new El('label');
    lab.textContent = props.label;
    el.labels = [lab];
  }
  return el;
}

function setValid(el, valid) {
  el._valid = valid;
  if (valid) el.value = el.value || '1';
}

/** A <form> whose elements/submit look like the real thing. */
function form(props) {
  const el = new El('form');
  el.method = 'post';
  el.noValidate = false;
  el.fields = props.fields || [];
  el.elements = el.fields;
  el.fields.forEach((f) => el.appendChild(f));
  if (props.guarded) el.setAttribute('data-hdc-dialog', '');
  el.submit = submitButton();
  el.appendChild(el.submit);
  el.querySelectorAll = function (selector) {
    if (/type="submit"/.test(selector)) return [el.submit];
    const hits = [];
    el.fields.forEach((f) => {
      String(selector).split(',').forEach((part) => {
        const cls = part.trim().replace(/^\./, '');
        if (cls && (f.attrs.class || '').split(/\s+/).indexOf(cls) !== -1) hits.push(f);
      });
    });
    return hits;
  };
  el.querySelector = function (selector) { return el.querySelectorAll(selector)[0] || null; };
  return el;
}

function submitButton() {
  const btn = new El('button');
  btn.setAttribute('type', 'submit');
  btn.appendChild(new El('i'));
  btn.appendChild(text('Save purchase'));
  return btn;
}

function mount(doc, node) {
  doc.body.appendChild(node);
  node._doc = doc;
  node.walk((child) => { child._doc = doc; });
  return node;
}

/** Submit the way a browser does: document capture listeners, then the form. */
function dispatchSubmit(formEl, doc) {
  const event = {
    type: 'submit', target: formEl, bubbles: true,
    defaultPrevented: false, _stopped: false,
    preventDefault() { this.defaultPrevented = true; },
    stopPropagation() { this._stopped = true; },
  };
  (doc.listeners['capture:submit'] || []).slice().forEach((fn) => {
    if (event._stopped || event._cancelled) return;
    fn.call(doc, event);
  });
  if (!event._stopped) formEl.fire('submit', event);
  if (!event._stopped) {
    (doc.listeners['bubble:submit'] || []).slice().forEach((fn) => fn.call(doc, event));
  }
  return event;
}

// ── sandbox ─────────────────────────────────────────────────────────────────

function load(sourceNames, doc, options) {
  options = options || {};
  const timers = [];
  const modals = [];
  const alerts = [];
  const errors = [];
  const winListeners = Object.create(null);
  const win = {
    setTimeout(fn) { timers.push(fn); return timers.length; },
    clearTimeout() {},
    alert(line) { alerts.push(String(line)); },
    console: { error(...args) { errors.push(args.map(String).join(' ')); } },
    addEventListener(type, fn) { (winListeners[type] = winListeners[type] || []).push(fn); },
    removeEventListener() {},
    /** Fire a window-level event (bfcache `pageshow`, mostly). */
    _fire(type, event) {
      (winListeners[type] || []).slice().forEach((fn) => fn.call(win, event || { type }));
    },
  };
  if (!options.noBootstrap) {
    win.bootstrap = {
      Modal: class {
        constructor(element, config) {
          this._element = element;
          this._config = config;
          this.shown = 0;
          this.hideCount = 0;
          this.disposed = false;
          this._awaitingShown = false;
          modals.push(this);
        }
        show() { this.shown += 1; this._awaitingShown = true; }
        hide() {
          this.hideCount += 1;
          this._awaitingShown = false;
          this._element.fire('hidden.bs.modal', { type: 'hidden.bs.modal' });
        }
        dispose() { this.disposed = true; }
      },
      Alert: class { constructor() { } close() { } },
    };
  }
  const sandbox = {
    window: win, document: doc, JSON, Object, Array, String, Number, Math, RegExp,
    isNaN, parseFloat, parseInt, TypeError, Error,
    // core/forms.js calls these bare; core/dialog.js through `window`.
    setTimeout: win.setTimeout, clearTimeout: win.clearTimeout,
  };
  if (win.bootstrap) sandbox.bootstrap = win.bootstrap;
  sandbox.globalThis = sandbox;
  win.document = win.document || doc;
  const context = vm.createContext(sandbox);
  sourceNames.forEach((name) => vm.runInContext(SOURCES[name], context, { filename: 'core/' + name + '.js' }));
  return {
    sandbox, win, doc, modals, alerts, timers, errors,
    /** Simulate the end of the fade-in, when `shown.bs.modal` really fires. */
    flushShown() {
      modals.forEach((modal) => {
        if (!modal._awaitingShown) return;
        modal._awaitingShown = false;
        modal._element.fire('shown.bs.modal', { type: 'shown.bs.modal' });
      });
    },
    runTimers() { timers.splice(0).forEach((fn) => fn()); },
  };
}

/** Page shell: the flash payload the server wrote, plus the banner to fold. */
function page(items, opts) {
  opts = opts || {};
  const doc = makeDocument();
  if (items) {
    const script = doc.createElement('script');
    script.setAttribute('id', 'hdcResultDialogData');
    script._text = opts.raw || JSON.stringify({ items });
    doc.body.appendChild(script);
  }
  const banner = doc.createElement('div');
  banner.setAttribute('id', 'flashMessages');
  banner.appendChild(doc.createElement('div'));
  doc.body.appendChild(banner);
  return doc;
}

function dialogOf(env) { return env.doc.getElementById('hdcResultDialog'); }
function lines(env) { const d = dialogOf(env); return d ? d.textContent : ''; }

// ── scenarios ───────────────────────────────────────────────────────────────

let passed = 0;
function test(name, fn) {
  try {
    fn();
    passed += 1;
  } catch (err) {
    console.error('\nFAIL: ' + name);
    console.error('  ' + (err && err.message ? err.message : err));
    process.exit(1);
  }
}

test('a flashed error opens an "Entry Not Saved" dialog the operator must acknowledge', () => {
  const env = load(['dialog'], page([{ kind: 'danger', text: 'Valid supplier is required.' }]));
  const modal = dialogOf(env);
  assert.ok(modal, 'dialog built on demand');
  assert.equal(modal.parentNode, env.doc.body, 'mounted on <body>, so no card transform can move the overlay');
  assert.ok(lines(env).indexOf('Valid supplier is required.') > -1, 'the server sentence is shown verbatim');
  assert.ok(lines(env).indexOf('Entry Not Saved') > -1, 'titled as a refusal, not a notice');
  assert.ok(lines(env).indexOf('OK') > -1, 'a server verdict is acknowledged, not re-submitted from');
  assert.ok(modal.classList.contains('hdc-result-bad'), 'danger tone');
  assert.equal(modal.getAttribute('role'), 'alertdialog');
  assert.equal(env.modals.length, 1);
  assert.equal(env.modals[0].shown, 1, 'opened without a click');
  assert.equal(env.modals[0]._config.backdrop, 'static', 'cannot be clicked away');
  assert.equal(env.modals[0]._config.keyboard, false, 'and not Esc-ed away');
  assert.equal(env.doc.getElementById('flashMessages').hidden, true, 'banner folded away so it is not read twice');
  env.flushShown();
  assert.equal(env.doc.activeElement, env.doc.getElementById('hdcResultDialogOk'), 'Enter acknowledges');
});

test('a flashed success is titled as a save and may be dismissed', () => {
  const env = load(['dialog'], page([{ kind: 'success', text: '1 purchase item(s) recorded successfully.' }]));
  assert.ok(lines(env).indexOf('Entry Saved') > -1, 'success wording');
  assert.ok(dialogOf(env).classList.contains('hdc-result-ok'), 'success tone');
  assert.equal(env.modals[0]._config.backdrop, true, 'a save can be clicked away');
  assert.equal(env.modals[0]._config.keyboard, true);
});

test('the worst verdict leads, and repeated sentences collapse', () => {
  const env = load(['dialog'], page([
    { kind: 'success', text: '1 purchase item(s) recorded successfully.' },
    { kind: 'warning', text: 'Duplicate purchase prevented for Cement.' },
    { kind: 'warning', text: 'Duplicate purchase prevented for Cement.' },
  ]));
  assert.ok(dialogOf(env).classList.contains('hdc-result-warn'), 'a warning outranks a success');
  assert.ok(lines(env).indexOf('Saved With a Warning') > -1);
  assert.ok(lines(env).indexOf('1 purchase item(s) recorded successfully.') > -1, 'the save is still reported');
  const repeats = lines(env).split('Duplicate purchase prevented for Cement.').length - 1;
  assert.equal(repeats, 1, 'the same sentence is not shown twice');
});

test('unknown flash categories fold into info; a page with no flash stays quiet', () => {
  const odd = load(['dialog'], page([{ kind: 'not_a_tone', text: 'FYI.' }]));
  assert.equal(odd.modals.length, 1);
  assert.ok(dialogOf(odd).classList.contains('hdc-result-info'), 'falls back to the info tone');
  const quiet = load(['dialog'], page(null));
  assert.equal(quiet.modals.length, 0, 'no flash, no dialog');
  assert.equal(dialogOf(quiet), null, 'and no dialog element is built at all');
  assert.equal(quiet.doc.getElementById('flashMessages').hidden, false, 'banner left alone for no-JS parity');
});

test('an incomplete entry is refused before the post, by name', () => {
  const supplier = field({ name: 'supplier_id', tag: 'select', label: 'Supplier *', valid: false, validity: { valueMissing: true } });
  const qty = field({ name: 'quantity', value: '5' });
  const f = form({ guarded: true, fields: [supplier, qty] });
  const doc = page(null);
  mount(doc, f);
  const env = load(['dialog'], doc);
  assert.equal(f.noValidate, true, 'our bubble replaces the native one');
  assert.equal(f.getAttribute('novalidate'), 'novalidate');
  const event = dispatchSubmit(f, doc);
  assert.equal(event.defaultPrevented, true, 'submit blocked');
  assert.ok(lines(env).indexOf('Supplier is required.') > -1, 'names the field, stripped of its asterisk: ' + lines(env));
  assert.ok(supplier.classList.contains('is-invalid'), 'offending control highlighted');
  assert.equal(qty.classList.contains('is-invalid'), false, 'the good field is not');
  env.flushShown();
  assert.equal(doc.activeElement, supplier, 'focus lands on the field to fix');
  assert.equal(supplier._scrolled, true, 'and it is scrolled into view');
  assert.ok(lines(env).indexOf('1 things need attention') === -1, 'one problem is not counted');
});

test('three problems are listed and counted', () => {
  const a = field({ name: 'supplier_id', tag: 'select', label: 'Supplier', valid: false, validity: { valueMissing: true } });
  const b = field({ name: 'date', label: 'Date', value: '13/13/2026', valid: false, validationMessage: 'Please enter a date.' });
  const c = field({ name: 'notes', label: 'Notes', valid: false, validity: { valueMissing: true } });
  const f = form({ guarded: true, fields: [a, b, c] });
  const doc = page(null);
  mount(doc, f);
  const env = load(['dialog'], doc);
  dispatchSubmit(f, doc);
  assert.ok(lines(env).indexOf('3 things need attention before saving') > -1, lines(env));
  assert.ok(lines(env).indexOf('Date: Please enter a date.') > -1, 'a filled-but-wrong field quotes the reason');
});

test('blocking a submit releases the double-click lock from core/forms.js', () => {
  const broken = field({ name: 'material_id', tag: 'select', label: 'Material', valid: false, validity: { valueMissing: true } });
  const f = form({ guarded: true, fields: [broken] });
  const doc = page(null);
  mount(doc, f);
  load(['dialog', 'forms'], doc);
  assert.equal(dispatchSubmit(f, doc).defaultPrevented, true);
  assert.equal(f.dataset.submitted, '0', 'lock released so the corrected attempt can post');
  assert.equal(f.submit.disabled, false, 'submit re-enabled');
  setValid(broken, true);
  assert.equal(dispatchSubmit(f, doc).defaultPrevented, false, 'the fixed entry posts');
  assert.equal(f.submit.disabled, true, 'and is then locked by forms.js');
});

test('a valid save puts the button in a Saving state, with a timeout back', () => {
  const good = field({ name: 'quantity', value: '12' });
  const f = form({ guarded: true, fields: [good] });
  const doc = page(null);
  mount(doc, f);
  const env = load(['dialog'], doc);
  assert.equal(dispatchSubmit(f, doc).defaultPrevented, false, 'nothing to complain about');
  assert.ok(f.submit.textContent.indexOf('Saving') > -1, 'button reports the save in flight');
  assert.equal(f.submit.getAttribute('aria-busy'), 'true');
  assert.equal(env.modals.length, 0, 'a save that is merely in flight does not interrupt');
  env.runTimers();
  assert.ok(f.submit.textContent.indexOf('Saving') === -1, 'label restored if the save stalls');
  assert.ok(f.submit.textContent.indexOf('Save purchase') > -1, 'original children come back');
});

test('Back into a cached page clears a stuck Saving label', () => {
  const good = field({ name: 'quantity', value: '12' });
  const f = form({ guarded: true, fields: [good] });
  const doc = page(null);
  mount(doc, f);
  const env = load(['dialog'], doc);
  dispatchSubmit(f, doc);
  assert.ok(f.submit.textContent.indexOf('Saving') > -1, 'the save is in flight');
  // No timer has run yet: this is the bfcache path, where the page comes back
  // exactly as it was left.
  env.win._fire('pageshow', { type: 'pageshow', persisted: true });
  assert.ok(f.submit.textContent.indexOf('Saving') === -1, 'label cleared on re-show');
  assert.ok(f.submit.textContent.indexOf('Save purchase') > -1, 'the original label is back');
  // a plain (non-cached) load must not touch anything
  dispatchSubmit(f, doc);
  env.win._fire('pageshow', { type: 'pageshow', persisted: false });
  assert.ok(f.submit.textContent.indexOf('Saving') > -1, 'still busy after a fresh navigation');
});

test('page rules block with their own wording (stock limits)', () => {
  const qty = field({ name: 'quantity', value: '40' });
  const f = form({ guarded: true, fields: [qty] });
  const doc = page(null);
  mount(doc, f);
  const env = load(['dialog'], doc);
  env.sandbox.window.hdcDialog.beforeSave(f, function () {
    return [{ message: 'Only 12.00 BAG is still pending on PO #4; 40.00 was entered.', focus: qty }];
  });
  assert.equal(dispatchSubmit(f, doc).defaultPrevented, true);
  assert.ok(lines(env).indexOf('Only 12.00 BAG is still pending on PO #4') > -1, lines(env));
  assert.ok(lines(env).indexOf('Entry Not Saved') > -1);
  assert.equal(env.modals[0]._config.backdrop, 'static', 'a refusal must be acknowledged');
});

test('a rule may report a plain string for a form whose fields are all valid', () => {
  const qty = field({ name: 'quantity', value: '1' });
  const f = form({ guarded: true, fields: [qty] });
  const doc = page(null);
  mount(doc, f);
  const env = load(['dialog'], doc);
  env.sandbox.window.hdcDialog.beforeSave(f, () => 'Add at least one item with a material, a quantity and a rate.');
  assert.equal(dispatchSubmit(f, doc).defaultPrevented, true);
  assert.ok(lines(env).indexOf('Add at least one item') > -1);
});

test('a throwing rule cannot eat the entry', () => {
  const good = field({ name: 'quantity', value: '1' });
  const f = form({ guarded: true, fields: [good] });
  const doc = page(null);
  mount(doc, f);
  const env = load(['dialog'], doc);
  env.sandbox.window.hdcDialog.beforeSave(f, function () { throw new Error('boom'); });
  assert.equal(dispatchSubmit(f, doc).defaultPrevented, false, 'submit still happens');
  assert.ok(env.errors.join(' ').indexOf('boom') > -1, 'but the bug is logged');
});

test('only forms that opted in are guarded', () => {
  const broken = field({ name: 'q', valid: false, validity: { valueMissing: true } });
  const f = form({ guarded: false, fields: [broken] });
  const doc = page(null);
  mount(doc, f);
  const env = load(['dialog'], doc);
  assert.equal(dispatchSubmit(f, doc).defaultPrevented, false, 'not our business');
  assert.equal(f.noValidate, false, 'native validation untouched');
  assert.equal(dialogOf(env), null, 'no dialog built');
  // a global rule (no form given) must not reach an unguarded form either
  env.sandbox.window.hdcDialog.beforeSave(null, () => 'should not run');
  assert.equal(dispatchSubmit(f, doc).defaultPrevented, false);
});

test('skip-marked fields are left to the page rule that asked for them', () => {
  const blankRow = field({ name: 'quantity[]', valid: false, validity: { valueMissing: true } });
  blankRow.setAttribute('data-hdc-dialog-skip', 'true');
  const hiddenToken = field({ name: '_csrf_token', type: 'hidden', valid: false });
  const f = form({ guarded: true, fields: [blankRow, hiddenToken] });
  const doc = page(null);
  mount(doc, f);
  const env = load(['dialog'], doc);
  assert.equal(dispatchSubmit(f, doc).defaultPrevented, false, 'a blank extra row does not block the save');
  assert.equal(env.modals.length, 0);
});

test('disabled and non-validating fields are ignored', () => {
  const disabled = field({ name: 'x', valid: false, disabled: true });
  const readOnly = field({ name: 'y', valid: false, willValidate: false });
  const f = form({ guarded: true, fields: [disabled, readOnly] });
  const doc = page(null);
  mount(doc, f);
  const env = load(['dialog'], doc);
  assert.equal(dispatchSubmit(f, doc).defaultPrevented, false);
});

test('without bootstrap the verdict still reaches the operator', () => {
  const env = load(['dialog'], page([{ kind: 'danger', text: 'Cannot deliver more than the remaining purchase quantity.' }]), { noBootstrap: true });
  assert.equal(env.modals.length, 0);
  assert.equal(env.alerts.length, 1, 'native box instead of silence');
  assert.ok(env.alerts[0].indexOf('Cannot deliver more than') > -1, env.alerts[0]);
  assert.ok(env.alerts[0].indexOf('Entry Not Saved') > -1);
  assert.equal(env.doc.getElementById('flashMessages').hidden, true, 'banner still folded, not doubled');
});

test('malformed flash JSON is ignored, not thrown', () => {
  const env = load(['dialog'], page([{}], { raw: '{"items": [ {oops' }));
  assert.equal(env.modals.length, 0);
  assert.equal(env.alerts.length, 0);
  assert.equal(env.doc.getElementById('flashMessages').hidden, false);
});

test('an empty payload object renders a readable empty dialog', () => {
  const env = load(['dialog'], page([{ kind: 'danger', text: '   ' }]));
  assert.equal(env.modals.length, 0, 'whitespace is not a message, so nothing pops');
});

test('hasPendingResult keeps auto-opening page modals out of the way', () => {
  const loud = load(['dialog'], page([{ kind: 'danger', text: 'Nope.' }]));
  assert.equal(loud.sandbox.window.hdcDialog.hasPendingResult(), true, 'the delivery page must not stack its edit modal on top');
  const quiet = load(['dialog'], page(null));
  assert.equal(quiet.sandbox.window.hdcDialog.hasPendingResult(), false);
  assert.equal(quiet.sandbox.window.hdcDialog.serverResult(), null);
});

test('show() can be called directly, and hides back to the control in focus', () => {
  const doc = page(null);
  const env = load(['dialog'], doc);
  const api = env.sandbox.window.hdcDialog;
  const trigger = field({ name: 'z' });
  mount(doc, trigger);
  doc.activeElement = trigger;
  api.success('Delivery #7 recorded.', { title: 'Delivery Recorded' });
  assert.ok(lines(env).indexOf('Delivery Recorded') > -1, 'page wording wins over the default title');
  assert.ok(lines(env).indexOf('Delivery #7 recorded.') > -1);
  assert.equal(env.modals[0].shown, 1);
  env.modals[0].hide();
  assert.equal(doc.activeElement, trigger, 'focus returns to where it was');
});

test('re-showing for a different tone rebuilds the modal instance', () => {
  const doc = page(null);
  const env = load(['dialog'], doc);
  const api = env.sandbox.window.hdcDialog;
  api.success('Saved.');
  const first = env.modals[0];
  api.error('Cannot delete purchase #3; delivery exists (10.00).');
  assert.equal(env.modals.length, 2, 'a new instance for the new backdrop policy');
  assert.equal(first.disposed, true, 'the old one is disposed, not leaked');
  assert.equal(env.modals[1]._config.backdrop, 'static');
  // same tone twice keeps the instance
  api.error('Second failure.');
  assert.equal(env.modals.length, 2);
  assert.ok(lines(env).indexOf('Second failure.') > -1, 'content is repainted');
});

test('the dialog element is reused across calls, never duplicated', () => {
  const doc = page(null);
  const env = load(['dialog'], doc);
  env.sandbox.window.hdcDialog.error('one');
  env.sandbox.window.hdcDialog.error('two');
  const built = doc.querySelectorAll('#hdcResultDialog').length;
  assert.equal(built, 1, 'one modal in the DOM');
});

test('flash text is inserted as text, never parsed as markup', () => {
  const env = load(['dialog'], page([{ kind: 'danger', text: '<img src=x onerror=alert(1)>' }]));
  assert.ok(lines(env).indexOf('<img') > -1, 'shown verbatim');
  assert.equal(dialogOf(env).querySelector('img'), null, 'no element parsed from it');
  assert.equal(env.alerts.length, 0, 'nothing executed');
});

console.log('\ndialog_harness: all ' + passed + ' assertions groups passed');
