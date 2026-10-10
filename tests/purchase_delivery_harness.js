/* HDC ERP — purchase_delivery_harness.js
 *
 * Runs static/hdc/js/pages/purchase_delivery.js against a minimal hand-rolled
 * DOM and asserts the Delivery / Material Usage register behaviours the field
 * asked for (see DELIVERY_USAGE_REGISTER_SPEC.md):
 *
 *   • the Pending Purchases pop-up filters by site, material, supplier and
 *     free text, and the site filter means "POs that already delivered there";
 *   • the summary line and the "nothing matches" row follow the filter;
 *   • Reset filters puts the pop-up back to every pending order;
 *   • the All Sites / All Stages buttons clear their own select, keep every
 *     other filter and re-run the search;
 *   • the stage list narrows to the selected site.
 *
 * Invoked by tests/test_purchase_delivery_js.py via `node`; exits non-zero on
 * the first failed assertion.
 *
 * Usage: node purchase_delivery_harness.js <repo_root>
 */

'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const repoRoot = process.argv[2];
if (!repoRoot) {
  console.error('usage: node purchase_delivery_harness.js <repo_root>');
  process.exit(2);
}

const SCRIPT = fs.readFileSync(
  path.join(repoRoot, 'static/hdc/js/pages/purchase_delivery.js'), 'utf8');

// ── Minimal DOM ─────────────────────────────────────────────────────────────

function makeEl(tag, attrs) {
  const el = {
    tagName: (tag || 'div').toUpperCase(),
    _attrs: attrs || {},
    style: { display: '' },
    options: [],
    value: '',
    _listeners: {},
    getAttribute(name) {
      return Object.prototype.hasOwnProperty.call(this._attrs, name) ? this._attrs[name] : null;
    },
    setAttribute(name, value) { this._attrs[name] = String(value); },
    addEventListener(type, fn) {
      (this._listeners[type] = this._listeners[type] || []).push(fn);
    },
    dispatch(type) {
      (this._listeners[type] || []).forEach((fn) => fn({ type }));
    },
    querySelectorAll() { return this._children || []; },
  };
  return el;
}

function makeDoc(elements) {
  return {
    getElementById(id) {
      return Object.prototype.hasOwnProperty.call(elements, id) ? elements[id] : null;
    },
  };
}

function loadModule() {
  const module_ = { exports: {} };
  // The page script attaches itself to `this` outside a browser; run it with a
  // throwaway sandbox object so nothing leaks into the harness globals.
  const sandbox = {};
  new Function('module', 'window', SCRIPT + '\n//# sourceURL=purchase_delivery.js')
    .call(sandbox, module_, undefined);
  return module_.exports;
}

const api = loadModule();
assert.ok(api.pendingMatches, 'module exports pendingMatches');
assert.ok(api.bindPendingPopup, 'module exports bindPendingPopup');
assert.ok(api.bindAllScopeButtons, 'module exports bindAllScopeButtons');

// ── pure filtering logic ────────────────────────────────────────────────────

const rows = [
  api.pendingRowFromElement(makeEl('tr', {
    'data-po-id': '1', 'data-site-ids': '1,2', 'data-material-id': '10',
    'data-supplier-id': '7', 'data-pending-qty': '70',
    'data-search': 'po 1 faisal cement depot cement ch-100',
  })),
  api.pendingRowFromElement(makeEl('tr', {
    'data-po-id': '2', 'data-site-ids': '2', 'data-material-id': '11',
    'data-supplier-id': '7', 'data-pending-qty': '25.5',
    'data-search': 'po 2 faisal cement depot sand ch-101',
  })),
  api.pendingRowFromElement(makeEl('tr', {
    'data-po-id': '3', 'data-site-ids': '', 'data-material-id': '10',
    'data-supplier-id': '8', 'data-pending-qty': '1000',
    'data-search': 'po 3 al-madina steel traders steel 12mm ch-102',
  })),
];

assert.deepEqual(
  api.summarisePending(rows, {}).visible.map((r) => r.poId), ['1', '2', '3'],
  'no filters shows every pending order');
assert.equal(api.summarisePending(rows, {}).pendingQty, 1095.5, 'pending qty totals');

// The site filter means "this PO has already delivered to that site".
assert.deepEqual(
  api.summarisePending(rows, { siteId: '1' }).visible.map((r) => r.poId), ['1'],
  'site 1 sees only the PO that delivered there');
assert.deepEqual(
  api.summarisePending(rows, { siteId: '2' }).visible.map((r) => r.poId), ['1', '2'],
  'site 2 sees both POs it received from');
assert.equal(api.summarisePending(rows, { siteId: '99' }).shown, 0,
  'a site nobody delivered to matches nothing');
assert.equal(rows[2].siteIds.length, 0, 'a PO with no deliveries has no sites');

assert.deepEqual(
  api.summarisePending(rows, { materialId: '10' }).visible.map((r) => r.poId), ['1', '3'],
  'material filter');
assert.deepEqual(
  api.summarisePending(rows, { supplierId: '7' }).visible.map((r) => r.poId), ['1', '2'],
  'supplier filter');
assert.deepEqual(
  api.summarisePending(rows, { term: 'SAND' }).visible.map((r) => r.poId), ['2'],
  'search is case-insensitive');
assert.deepEqual(
  api.summarisePending(rows, { term: 'ch-10' }).visible.map((r) => r.poId), ['1', '2', '3'],
  'search reaches the challan number');
assert.deepEqual(
  api.summarisePending(rows, { siteId: '2', materialId: '11' }).visible.map((r) => r.poId), ['2'],
  'filters combine');
assert.equal(api.summarisePending(rows, { siteId: '1', materialId: '11' }).shown, 0,
  'contradictory filters match nothing');

// ── the pop-up wiring ───────────────────────────────────────────────────────

function buildPopupDom() {
  const rowEls = rows.map((r) => r.element);
  const body = makeEl('tbody');
  body._children = rowEls;
  const elements = {
    pendingPOBody: body,
    pendingSiteFilter: makeEl('select'),
    pendingMaterialFilter: makeEl('select'),
    pendingSupplierFilter: makeEl('select'),
    pendingSearch: makeEl('input'),
    pendingPONoMatch: makeEl('tr'),
    pendingPOSummary: makeEl('div'),
    pendingResetFilters: makeEl('button'),
  };
  return { doc: makeDoc(elements), elements, rowEls };
}

{
  const { doc, elements, rowEls } = buildPopupDom();
  const popup = api.bindPendingPopup(doc);
  assert.ok(popup, 'the pop-up binds when its table is on the page');
  assert.equal(elements.pendingPOSummary.textContent.indexOf('3 pending purchase order(s)'), 0,
    'summary counts every pending order before filtering: ' + elements.pendingPOSummary.textContent);
  assert.equal(elements.pendingPONoMatch.style.display, 'none', 'no-match row hidden at first');

  elements.pendingSiteFilter.value = '2';
  elements.pendingSiteFilter.dispatch('change');
  assert.deepEqual(rowEls.map((el) => el.style.display), ['', '', 'none'],
    'site 2 hides the PO that never delivered there');
  assert.ok(elements.pendingPOSummary.textContent.indexOf('2 pending purchase order(s)') === 0,
    'summary follows the site filter: ' + elements.pendingPOSummary.textContent);
  assert.ok(elements.pendingPOSummary.textContent.indexOf('filtered to one site') !== -1,
    'summary says a site filter is active');

  elements.pendingSearch.value = 'steel';
  elements.pendingSearch.dispatch('input');
  assert.deepEqual(rowEls.map((el) => el.style.display), ['none', 'none', 'none'],
    'site + search that contradicts hides everything');
  assert.equal(elements.pendingPONoMatch.style.display, '', 'no-match row shows');

  elements.pendingResetFilters.dispatch('click');
  assert.deepEqual(rowEls.map((el) => el.style.display), ['', '', ''],
    'Reset filters brings every pending order back');
  assert.equal(elements.pendingSiteFilter.value, '', 'Reset clears the site filter');
  assert.equal(elements.pendingSearch.value, '', 'Reset clears the search box');
  assert.equal(elements.pendingPONoMatch.style.display, 'none', 'no-match row hidden again');
}

// A page with no pending orders at all must not show the no-match row.
{
  const body = makeEl('tbody');
  body._children = [];
  const doc = makeDoc({
    pendingPOBody: body,
    pendingPONoMatch: makeEl('tr'),
    pendingPOSummary: makeEl('div'),
  });
  api.bindPendingPopup(doc);
  assert.equal(doc.getElementById('pendingPONoMatch').style.display, 'none',
    'an empty pending list keeps the no-match row hidden');
}

// ── All Sites / All Stages buttons ──────────────────────────────────────────

{
  let submitted = 0;
  const form = makeEl('form');
  form.requestSubmit = () => { submitted += 1; };
  const project = makeEl('select');
  project.value = '2';
  const stage = makeEl('select');
  stage.value = '4';
  stage.options = [
    makeEl('option'),                                        // the "All Stages" option
    Object.assign(makeEl('option', { 'data-project': '1' }), { value: '3' }),
    Object.assign(makeEl('option', { 'data-project': '2' }), { value: '4' }),
  ];
  // A real <option> reports `selected` from its parent select's value; the
  // page script relies on that to drop a stage outside the picked site.
  stage.options.forEach((opt) => {
    Object.defineProperty(opt, 'selected', { get: () => stage.value === opt.value });
  });
  const allSites = makeEl('button');
  const allStages = makeEl('button');
  const doc = makeDoc({
    deliveryFilterForm: form,
    filterProjectSelect: project,
    filterStageSelect: stage,
    filterAllSitesBtn: allSites,
    filterAllStagesBtn: allStages,
  });

  const bound = api.bindAllScopeButtons(doc, {
    form: 'deliveryFilterForm', project: 'filterProjectSelect',
    stage: 'filterStageSelect', allSites: 'filterAllSitesBtn',
    allStages: 'filterAllStagesBtn',
  });
  assert.ok(bound, 'the filter bar binds');
  assert.deepEqual(stage.options.map((o) => o.style.display), ['', 'none', ''],
    'stages of other sites are hidden while a site is selected');

  allSites.dispatch('click');
  assert.equal(project.value, '', 'All Sites clears the site filter');
  assert.equal(submitted, 1, 'All Sites re-runs the search');
  assert.deepEqual(stage.options.map((o) => o.style.display), ['', '', ''],
    'every stage is offered again once all sites are shown');

  project.value = '1';
  project.dispatch('change');
  assert.equal(stage.value, '', 'a stage outside the newly picked site is dropped');
  assert.deepEqual(stage.options.map((o) => o.style.display), ['', '', 'none'],
    'the stage list follows the site');

  allStages.dispatch('click');
  assert.equal(stage.value, '', 'All Stages clears the stage filter');
  assert.equal(submitted, 2, 'All Stages re-runs the search');
  assert.equal(project.value, '1', 'All Stages leaves the site filter alone');
}

// A page without the filter bar binds nothing and must not throw.
assert.equal(api.bindAllScopeButtons(makeDoc({}), { form: 'nope' }), null,
  'absent filter bar is a no-op');
assert.equal(api.bindPendingPopup(makeDoc({})), null,
  'absent pop-up is a no-op');

console.log('purchase_delivery.js harness: all assertions passed');
