/* HDC ERP — tool_stage_scope_harness.js
 *
 * Drives a *server-rendered* Tools page (Rent Out transaction, New Rental, or
 * the rental-detail transfer form) in jsdom with the real scripts and proves
 * the Stage picker is scoped to the chosen site / project:
 *
 *   • before a site is chosen the stage list offers nothing (and says so);
 *   • choosing site A offers exactly site A's stages — never site B's;
 *   • typing the exact label of a site-B stage does not select it;
 *   • a site-A stage is cleared when the operator switches to site B.
 *
 * Invoked by tests/test_tool_stage_scope.py; needs jsdom
 * (``npm ci --prefix tests``).
 *
 * Usage: node tool_stage_scope_harness.js <repo_root> <page.html> <spec.json>
 */

'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const [repoRoot, pageFile, specFile] = process.argv.slice(2);
if (!repoRoot || !pageFile || !specFile) {
  console.error('usage: node tool_stage_scope_harness.js <repo_root> <page.html> <spec.json>');
  process.exit(2);
}

let JSDOM;
try {
  ({ JSDOM } = require('jsdom'));
} catch (err) {
  console.error('jsdom is not installed: ' + err.message);
  process.exit(3);
}

const spec = JSON.parse(fs.readFileSync(specFile, 'utf8'));
// Inline every /hdc_static script so the page runs in its real order (inline
// page code that relies on core/combo.js still finds it).
const html = fs.readFileSync(pageFile, 'utf8').replace(
  /<script([^>]*)\ssrc="\/hdc_static\/([^"?]+)(?:\?[^"]*)?"([^>]*)><\/script>/g,
  (whole, before, rel, after) => {
    const file = path.join(repoRoot, 'static', 'hdc', rel);
    if (!fs.existsSync(file)) return '';
    const code = fs.readFileSync(file, 'utf8').replace(/<\/script/gi, '<\\/script');
    return '<script' + before + after + '>' + code + '</script>';
  });

const errors = [];
const { VirtualConsole } = require('jsdom');
const virtualConsole = new VirtualConsole();
virtualConsole.on('jsdomError', (err) => errors.push(String(err && (err.stack || err.message) || err)));

const dom = new JSDOM(html, {
  runScripts: 'dangerously',
  pretendToBeVisual: true,
  url: 'https://hdc.local' + spec.url,
  virtualConsole,
  beforeParse(window) {
    window.confirm = () => true;
    window.fetch = () => Promise.resolve({ ok: false, status: 404, json: () => Promise.resolve(null) });
  },
});
const { window } = dom;
const { document } = window;

function fire(el, type) {
  el.dispatchEvent(new window.Event(type, { bubbles: true }));
}
function offeredStageValues() {
  const stage = document.getElementById(spec.stageSelect);
  return Array.from(stage.options)
    .filter((o) => o.value && !o.hidden && !o.disabled)
    .map((o) => o.value).sort();
}
function menuFor(selectId) {
  return document.getElementById(selectId + '_menu');
}
function chooseProject(value) {
  const project = document.getElementById(spec.projectSelect);
  project.value = String(value);
  fire(project, 'change');
}

const sorted = (list) => list.map(String).sort();
const loaded = new Promise((resolve) => {
  if (document.readyState === 'complete') resolve();
  else window.addEventListener('load', () => resolve());
});
const settle = () => new Promise((resolve) => window.setTimeout(resolve, 200));
let projectSelect, stageSelect, stageInput, menu;

loaded.then(() => {
  projectSelect = document.getElementById(spec.projectSelect);
  stageSelect = document.getElementById(spec.stageSelect);
  stageInput = document.getElementById(spec.stageSelect + 'Input');
  assert.ok(projectSelect, 'project picker is rendered');
  assert.ok(stageSelect, 'stage picker is rendered');
  assert.ok(stageInput, 'stage combo input is rendered');
  assert.equal(stageSelect.getAttribute('data-hdc-depends-on'), spec.projectSelect,
    'the stage picker declares the project picker it depends on');

  // Every stage of every site is in the markup — the scoping is the contract.
  const allValues = Array.from(stageSelect.options).filter((o) => o.value).map((o) => o.value);
  assert.deepEqual(sorted(allValues), sorted([...spec.stagesA, ...spec.stagesB]));

  // 1. No site yet → no stage offered, and the combo tells the operator why.
  if (projectSelect.value) chooseProject('');
  assert.deepEqual(offeredStageValues(), [], 'no stage is offered before a site is chosen');
  fire(stageInput, 'focus');
  menu = menuFor(spec.stageSelect);
  assert.equal(menu.querySelectorAll('.hdc-combo-item').length, 0,
    'the open stage list has no rows before a site is chosen');
  assert.match(menu.textContent, /Choose the site \/ project first/);
  fire(stageInput, 'blur');

  // 2. Site A → exactly site A's stages.
  chooseProject(spec.projectA);
  assert.deepEqual(offeredStageValues(), sorted(spec.stagesA), 'site A offers only its stages');
  stageInput.value = '';
  fire(stageInput, 'focus');
  menu = menuFor(spec.stageSelect);
  const rows = Array.from(menu.querySelectorAll('.hdc-combo-item')).map((r) => r.textContent);
  assert.equal(rows.length, spec.stagesA.length, 'the open list shows only site A stages');
  rows.forEach((text) => assert.ok(!text.includes(spec.projectBName),
    'no site B stage appears in the list: ' + text));

  // 3. Typing a site-B stage's exact label cannot select it.
  const foreign = Array.from(stageSelect.options).find((o) => o.value === String(spec.stagesB[0]));
  stageInput.value = foreign.text;
  fire(stageInput, 'input');
  fire(stageInput, 'blur');

  return settle();
}).then(() => {
  assert.notEqual(stageSelect.value, String(spec.stagesB[0]),
    'a typed site-B stage label is not accepted under site A');

  // 4. A chosen site-A stage is cleared when the site changes to B.
  stageSelect.value = String(spec.stagesA[0]);
  fire(stageSelect, 'change');
  assert.equal(stageInput.value, Array.from(stageSelect.options)
    .find((o) => o.value === String(spec.stagesA[0])).text);
  chooseProject(spec.projectB);
  assert.equal(stageSelect.value, '', 'switching site clears a stage of the old site');
  assert.equal(stageInput.value, '', 'the visible stage text is cleared too');
  assert.deepEqual(offeredStageValues(), sorted(spec.stagesB), 'site B offers only its stages');

  // 5. Clearing the site offers nothing again.
  chooseProject('');
  assert.deepEqual(offeredStageValues(), []);

  const relevant = errors.filter((e) => !/Not implemented/.test(e));
  assert.deepEqual(relevant, [], 'the page scripts ran without errors');
  console.log('HDC-HARNESS-OK');
}).catch((err) => {
  console.error(err && err.stack || err);
  process.exit(1);
});
