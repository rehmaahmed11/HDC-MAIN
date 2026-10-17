/* HDC ERP — tests/expense_categories_harness.js
 *
 * Runs the real Expense Categories page (rendered by the Flask test client,
 * handed to us as a file) inside jsdom, with the page's own script list, and
 * pins the row-rename behaviour of static/hdc/js/core/row_rename.js (the same
 * script the Trades page loads):
 *
 *   1. every row starts read-only: name visible, rename field hidden;
 *   2. the pencil opens that row: field shown, name hidden, field focused and
 *      selected (so typing replaces the old name);
 *   3. only one row is open at a time;
 *   4. Escape restores the original value and closes the row;
 *   5. the ✕ button does the same as Escape;
 *   6. each row keeps exactly one live text input, inside that row's <form>,
 *      and both row buttons live in the Actions cell.
 *
 * Usage: node tests/expense_categories_harness.js <rendered-page.html>
 */
'use strict';

const fs = require('fs');
const path = require('path');
const { JSDOM, VirtualConsole } = require('jsdom');

const page = process.argv[2];
if (!page) {
    console.error('usage: node expense_categories_harness.js <rendered-page.html>');
    process.exit(2);
}

const failures = [];
function check(label, condition, detail) {
    if (condition) return;
    failures.push(detail ? `${label} — ${detail}` : label);
}

const html = fs.readFileSync(page, 'utf8');

// The page pulls its scripts from /hdc_static/... which jsdom must not fetch;
// only the inline + file-loaded page script runs, which is what we are pinning.
const virtualConsole = new VirtualConsole();
virtualConsole.on('jsdomError', (err) => {
    failures.push('page script threw: ' + err.message);
});

const dom = new JSDOM(html, {
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    virtualConsole,
    // A real origin: the layout's inline scripts touch localStorage, which
    // jsdom refuses on an opaque origin (about:blank).
    url: 'http://localhost/hdc/expense_categories',
});

const { window } = dom;
const document = window.document;

function loadPageScript() {
    const src = fs.readFileSync(
        path.join(__dirname, '..', 'static', 'hdc', 'js', 'core',
                  'row_rename.js'),
        'utf8');
    const script = document.createElement('script');
    script.textContent = src;
    document.body.appendChild(script);
    // jsdom keeps readyState at "loading" while the page's external
    // /hdc_static/*.js tags go unfetched, so the script's own
    // DOMContentLoaded hook would never fire here.  Dispatch it ourselves —
    // the script wiring itself up is exactly what we are pinning.
    if (document.readyState !== 'complete') {
        document.dispatchEvent(new window.Event('DOMContentLoaded', { bubbles: true }));
    }
}

function rows() {
    return Array.from(document.querySelectorAll('[data-hdc-rename-table] tbody tr'))
        .filter((row) => row.querySelector('.hdc-rename-form'));
}

function state(row) {
    return {
        form: row.querySelector('.hdc-rename-form'),
        name: row.querySelector('.hdc-rename-view'),
        input: row.querySelector('input[name="category_name"]'),
        pencil: row.querySelector('.hdc-rename-open'),
        cancel: row.querySelector('.hdc-rename-form button[type="button"]'),
        save: row.querySelector('.hdc-rename-form button[type="submit"]'),
        deleteBtn: row.querySelector('.hdc-rename-actions button[type="submit"]'),
    };
}

loadPageScript();

const all = rows();
check('the seeded page renders rows with a rename form', all.length >= 3,
      `found ${all.length}`);

// 1. read-only by default
all.forEach((row, i) => {
    const s = state(row);
    check(`row ${i + 1}: field hidden until the pencil is pressed`,
          s.form.classList.contains('d-none'),
          s.form.className);
    check(`row ${i + 1}: name is readable`,
          s.name && !s.name.classList.contains('d-none'));
    check(`row ${i + 1}: exactly one text input in the row`,
          row.querySelectorAll('input[type="text"]').length === 1);
});

// structural: both row buttons in the Actions cell, save inside the form
const first = state(all[0]);
check('the Save button lives inside the row form (dialog + submit lock)',
      first.save && first.form.contains(first.save));
check('the rename pencil lives in the Actions cell',
      first.pencil && first.pencil.closest('.hdc-rename-actions') !== null);
check('the delete button lives in the Actions cell next to the pencil',
      first.deleteBtn && first.deleteBtn.closest('.hdc-rename-actions') !== null);
check('Save and Delete share one Actions cell',
      first.pencil.parentElement === first.deleteBtn.closest('td'));

// 2. the pencil opens the row
const original = first.input.value;
first.pencil.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
check('pencil opens the rename field', !first.form.classList.contains('d-none'));
check('pencil hides the read-only name', first.name.classList.contains('d-none'));
check('the field takes focus', document.activeElement === first.input);
check('the field is selected (typing replaces the old name)',
      first.input.selectionStart === 0 && first.input.selectionEnd === original.length,
      `${first.input.selectionStart}-${first.input.selectionEnd} of ${original.length}`);

// 3. one row at a time
const second = state(all[1]);
second.pencil.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
check('opening another row closes the first', first.form.classList.contains('d-none'));
check('the newly opened row shows its field', !second.form.classList.contains('d-none'));
check('the first row shows its name again', !first.name.classList.contains('d-none'));

// 4. Escape restores and closes
second.input.value = 'typed but abandoned';
second.input.dispatchEvent(new window.KeyboardEvent('keydown',
    { key: 'Escape', bubbles: true, cancelable: true }));
check('Escape closes the row', second.form.classList.contains('d-none'));
check('Escape restores the original name',
      second.input.value === second.input.getAttribute('value'),
      `${second.input.value} vs ${second.input.getAttribute('value')}`);

// 5. the ✕ button behaves like Escape
second.pencil.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
second.cancel.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
check('cancel button closes the row', second.form.classList.contains('d-none'));
check('cancel button leaves the row hidden',
      !second.form.classList.contains('d-flex'));

if (failures.length) {
    console.error('expense categories harness FAILED:');
    failures.forEach((f) => console.error('  ✗ ' + f));
    process.exit(1);
}
console.log(`expense categories harness OK (${all.length} rows checked)`);
