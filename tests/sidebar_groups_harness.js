/* HDC ERP — sidebar_groups_harness.js
 *
 * Runs static/hdc/js/core/sidebar_groups.js against a minimal hand-rolled DOM
 * and asserts the collapsible-sidebar behaviours base.html relies on:
 *
 *   • the group holding the current page is always open;
 *   • other groups use the saved choice, else default to open (wide screen) or
 *     closed (phone/tablet);
 *   • any later class change — Bootstrap's `show`, the footer expand-all, a
 *     heading click — is written back to localStorage;
 *   • the mid-animation `collapsing` state is not persisted;
 *   • a broken/privating localStorage cannot break the drawer.
 *
 * Invoked by tests/test_sidebar_groups_js.py via `node`; exits non-zero on the
 * first failed assertion.
 *
 * Usage: node sidebar_groups_harness.js <repo_root>
 */

'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const repoRoot = process.argv[2];
if (!repoRoot) {
  console.error('usage: node sidebar_groups_harness.js <repo_root>');
  process.exit(2);
}

const SCRIPT = fs.readFileSync(
  path.join(repoRoot, 'static/hdc/js/core/sidebar_groups.js'), 'utf8');

// ── Minimal DOM ─────────────────────────────────────────────────────────────

const observers = [];

function matches(el, selector) {
  if (selector.startsWith('.')) return el.classList.contains(selector.slice(1));
  if (selector.startsWith('[') && selector.endsWith(']')) {
    const name = selector.slice(1, -1).split('=')[0];
    return el.getAttribute(name) !== null;
  }
  throw new Error('unsupported selector ' + selector);
}

function notifyClassChange(el) {
  for (const obs of observers.slice()) {
    if (!obs.options.attributes) continue;
    const filter = obs.options.attributeFilter;
    if (filter && filter.indexOf('class') === -1) continue;
    if (el === obs.target) obs.callback([]);
  }
}

class FakeClassList {
  constructor(el) {
    this.el = el;
    this.tokens = new Set(
      String(el._attrs['class'] || '').split(/\s+/).filter(Boolean));
  }
  contains(name) { return this.tokens.has(name); }
  add(...names) { names.forEach((n) => this.tokens.add(n)); this._sync(); }
  remove(...names) { names.forEach((n) => this.tokens.delete(n)); this._sync(); }
  toggle(name, force) {
    const has = this.tokens.has(name);
    const next = (force === undefined) ? !has : !!force;
    if (next) this.tokens.add(name); else this.tokens.delete(name);
    this._sync();
    return next;
  }
  _sync() {
    this.el._attrs['class'] = Array.from(this.tokens).join(' ');
    notifyClassChange(this.el);
  }
  toString() { return Array.from(this.tokens).join(' '); }
}

class FakeElement {
  constructor(tag, attrs, children) {
    this.tagName = String(tag).toUpperCase();
    this._attrs = Object.assign({}, attrs || {});
    this.classList = new FakeClassList(this);
    this.children = [];
    this.parentNode = null;
    this.style = {};
    this._listeners = {};
    (children || []).forEach((child) => this.appendChild(child));
  }
  appendChild(child) { child.parentNode = this; this.children.push(child); return child; }
  getAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this._attrs, name)
      ? this._attrs[name] : null;
  }
  setAttribute(name, value) {
    this._attrs[name] = String(value);
    if (name === 'class') {
      this.classList.tokens = new Set(String(value).split(/\s+/).filter(Boolean));
    }
  }
  removeAttribute(name) { delete this._attrs[name]; }
  get lastElementChild() { return this.children[this.children.length - 1] || null; }
  scrollIntoView() {}
  addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); }
  dispatchEvent(event) {
    if (!event.target) event.target = this;
    (this._listeners[event.type] || []).forEach((fn) => fn(event));
    return true;
  }
  _descendants(out) {
    for (const child of this.children) { out.push(child); child._descendants(out); }
    return out;
  }
  _findAll(selector) { return this._descendants([]).filter((el) => matches(el, selector)); }
  querySelector(selector) { return this._findAll(selector)[0] || null; }
  querySelectorAll(selector) { return this._findAll(selector); }
  closest(selector) {
    let node = this;
    while (node) {
      if (matches(node, selector)) return node;
      node = node.parentNode;
    }
    return null;
  }
}

class FakeDocument {
  constructor(elements) {
    this._byId = elements || {};
    this._listeners = {};
  }
  getElementById(id) { return this._byId[id] || null; }
  addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); }
  dispatchEvent(event) {
    event.target = this;
    (this._listeners[event.type] || []).forEach((fn) => fn(event));
    return true;
  }
}

class FakeEvent {
  constructor(type) { this.type = type; this.target = null; }
}

function makeStorage(initial, options) {
  const data = Object.assign({}, initial || {});
  const opts = options || {};
  return {
    getItem(key) {
      return Object.prototype.hasOwnProperty.call(data, key) ? data[key] : null;
    },
    setItem(key, value) {
      if (opts.failWrites) throw new Error('quota');
      data[key] = String(value);
    },
    removeItem(key) { delete data[key]; },
    _data: data,
  };
}

// ── The sidebar under test (mirrors base.html's rendered shape) ─────────────

function buildSidebar(sectionIds, currentId) {
  const groups = sectionIds.map((id) => {
    const link = new FakeElement('li', { class: 'nav-item' });
    const subnav = new FakeElement('ul',
      { class: 'sidebar-subnav collapse' + (id === currentId ? ' show' : ''),
        id: 'sidebarSection-' + id },
      [link]);
    const label = new FakeElement('span', { class: 'nav-group-label' });
    const arrow = new FakeElement('i', { class: 'fas fa-chevron-down nav-group-arrow' });
    const toggle = new FakeElement('button',
      { type: 'button', class: 'nav-group-toggle',
        'data-bs-target': '#sidebarSection-' + id,
        'aria-expanded': id === currentId ? 'true' : 'false' },
      [label, arrow]);
    const group = new FakeElement('li',
      { class: 'nav-group' + (id === currentId ? ' has-current' : ''),
        'data-hdc-nav-section': id },
      [toggle, subnav]);
    return group;
  });
  const sidebar = new FakeElement('nav', { id: 'sidebar' }, groups);
  return { sidebar, groups };
}

function runScript(script, options) {
  const opts = options || {};
  const { sidebar, groups } = buildSidebar(
    opts.sectionIds || ['overview', 'projects', 'workforce'],
    opts.currentId || 'overview');

  observers.length = 0;
  const storage = opts.storage || makeStorage(opts.initialStorage || {});
  const document = new FakeDocument(
    opts.noSidebar ? {} : { sidebar: sidebar });

  class HarnessMutationObserver {
    constructor(callback) { this.callback = callback; }
    observe(target, options) {
      observers.push({ target, options, callback: this.callback });
    }
    disconnect() {
      const index = observers.findIndex((o) => o.callback === this.callback);
      if (index !== -1) observers.splice(index, 1);
    }
  }

  const windowObject = {
    localStorage: storage,
    setTimeout(fn) { fn(); return 1; },
    MutationObserver: HarnessMutationObserver,
    matchMedia: () => ({ matches: !!opts.narrow }),
    addEventListener() {},
  };

  const sandbox = {
    window: windowObject,
    document,
    localStorage: storage,
    MutationObserver: HarnessMutationObserver,
    Event: FakeEvent,
    console,
  };

  const names = Object.keys(sandbox);
  const fn = new Function(...names, script);
  fn(...names.map((name) => sandbox[name]));

  return { sidebar, groups, storage, document, windowObject };
}

function listOf(group) { return group.querySelector('.sidebar-subnav'); }
function toggleOf(group) { return group.querySelector('.nav-group-toggle'); }
function isOpen(group) { return listOf(group).classList.contains('show'); }

function prefsOf(storage) {
  const raw = storage.getItem('hdc_sidebar_sections');
  return raw ? JSON.parse(raw) : {};
}

// `checkAria` is only true for the states the script itself applies: when the
// class is flipped by Bootstrap or the expand-all button, core/collapse_groups.js
// and Bootstrap own aria-expanded (they sync it on the collapse events).
function assertOpen(harness, openIds, message, checkAria) {
  harness.groups.forEach((group) => {
    const id = group.getAttribute('data-hdc-nav-section');
    const expected = openIds.indexOf(id) !== -1;
    assert.equal(isOpen(group), expected,
      `${message}: group ${id} should be ${expected ? 'open' : 'closed'}`);
    if (checkAria !== false) {
      assert.equal(toggleOf(group).getAttribute('aria-expanded'),
        expected ? 'true' : 'false',
        `${message}: group ${id} aria-expanded`);
    }
    assert.equal(group.classList.contains('is-section-closed'), !expected,
      `${message}: group ${id} is-section-closed`);
  });
}

// ── Cases ───────────────────────────────────────────────────────────────────

// 1. wide screen, no saved choice: everything open, the current group marked.
let h = runScript(SCRIPT, {});
assertOpen(h, ['overview', 'projects', 'workforce'], 'first visit on a wide screen');
assert.equal(h.groups[0].classList.contains('has-current'), true);

// 2. phone/tablet, no saved choice: only the current group opens.
h = runScript(SCRIPT, { narrow: true });
assertOpen(h, ['overview'], 'first visit on a phone');

// 3. saved choice wins for the other groups; the current group always opens.
h = runScript(SCRIPT, {
  initialStorage: { hdc_sidebar_sections: JSON.stringify({ projects: false, overview: false }) },
});
assertOpen(h, ['overview', 'workforce'], 'saved choices (current group forced open)');

// 4. Bootstrap-style class change is remembered.
h = runScript(SCRIPT, {});
listOf(h.groups[1]).classList.remove('show');           // close "projects"
assert.deepEqual(prefsOf(h.storage), { projects: false });
listOf(h.groups[1]).classList.add('show');              // open it again
assert.deepEqual(prefsOf(h.storage), { projects: true });

// 5. mid-animation state (collapsing without show) is not persisted.
h = runScript(SCRIPT, {});
listOf(h.groups[2]).classList.add('collapsing');
assert.deepEqual(prefsOf(h.storage), {}, 'collapsing must not be persisted');

// 6. the footer "Expand / Collapse all sections" path persists every group.
h = runScript(SCRIPT, { narrow: true });
h.groups.forEach((group) => listOf(group).classList.add('show'));
assert.deepEqual(prefsOf(h.storage),
  { overview: true, projects: true, workforce: true }, 'expand all');
h.groups.forEach((group) => listOf(group).classList.remove('show'));
assert.deepEqual(prefsOf(h.storage),
  { overview: false, projects: false, workforce: false }, 'collapse all');

// 7. console API.
h = runScript(SCRIPT, { narrow: true });
assert.equal(h.windowObject.HDCSidebarSections.isOpen('projects'), false);
assert.equal(h.windowObject.HDCSidebarSections.isOpen('nope'), null);
h.windowObject.HDCSidebarSections.openAll();
assertOpen(h, ['overview', 'projects', 'workforce'], 'openAll()');
h.windowObject.HDCSidebarSections.closeAll();
assertOpen(h, [], 'closeAll()');
h.windowObject.HDCSidebarSections.clearSaved();
assert.deepEqual(prefsOf(h.storage), {}, 'clearSaved() empties storage');

// 8. a corrupt stored value is ignored instead of breaking the drawer.
h = runScript(SCRIPT, { initialStorage: { hdc_sidebar_sections: '{not json' } });
assertOpen(h, ['overview', 'projects', 'workforce'], 'corrupt storage falls back');

// 9. storage that refuses writes cannot break the drawer either.
h = runScript(SCRIPT, {
  narrow: true, storage: makeStorage({}, { failWrites: true }),
});
listOf(h.groups[1]).classList.add('show');
assertOpen(h, ['overview', 'projects'], 'read-only storage still toggles', false);

// 10. pages without the drawer (login) load the script harmlessly.
h = runScript(SCRIPT, { noSidebar: true });
assert.ok(h.document.getElementById('sidebar') === null);
assert.equal(h.windowObject.HDCSidebarSections, undefined);

// 11. clicking a heading on a closed group keeps it in view (no throw).
h = runScript(SCRIPT, { narrow: true });
const click = new FakeEvent('click');
click.target = toggleOf(h.groups[1]);
h.sidebar.dispatchEvent(click);
assert.equal(listOf(h.groups[1]).getAttribute('id'), 'sidebarSection-projects');

console.log('sidebar_groups_harness: all assertions passed');
