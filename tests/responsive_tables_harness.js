/* HDC ERP — tests/responsive_tables_harness.js
 *
 * Runs the real phone-table script (`static/hdc/js/core/responsive_tables.js`)
 * against a page rendered by the Flask test client, in jsdom, and pins the
 * card-title decision that the row-rename lists depend on:
 *
 *   - the table becomes a stack of record cards (`hdc-rt-stack`);
 *   - the FIRST cell (the name, `.hdc-rename-view`) is the card title
 *     (`data-hdc-rt-lead="1"`);
 *   - the Created date is a labelled field, never the title;
 *   - the Actions cell — which holds only icon buttons — stays visible
 *     (`data-hdc-rt-empty="0"`), so a phone never loses Rename / Delete.
 *
 * Why this exists: the rename controls live inside a `d-none` form.  A
 * visibility-blind "has controls" test treated those hidden buttons as
 * content, skipped the name cell when choosing the title, and promoted the
 * date — the cards read "CATEGORY NAME / Cement" above a bold "2026-10-17".
 *
 * Usage: node tests/responsive_tables_harness.js <rendered-page.html>
 */
'use strict';

const fs = require('fs');
const path = require('path');
const { JSDOM, VirtualConsole } = require('jsdom');

const page = process.argv[2];
if (!page) {
    console.error('usage: node responsive_tables_harness.js <rendered-page.html>');
    process.exit(2);
}

const failures = [];
function check(label, condition, detail) {
    if (condition) return;
    failures.push(detail ? `${label} — ${detail}` : label);
}

const html = fs.readFileSync(page, 'utf8');

const virtualConsole = new VirtualConsole();
virtualConsole.on('jsdomError', (err) => {
    failures.push('page script threw: ' + err.message);
});

const dom = new JSDOM(html, {
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    virtualConsole,
    url: 'http://localhost/hdc/expense_categories',
});

const { window } = dom;
const document = window.document;

const table = document.querySelector('[data-hdc-rename-table]');
check('rendered page has a rename table', !!table);
if (table) {
    const script = document.createElement('script');
    script.textContent = fs.readFileSync(
        path.join(__dirname, '..', 'static', 'hdc', 'js', 'core',
                  'responsive_tables.js'),
        'utf8');
    document.body.appendChild(script);
    // jsdom keeps readyState at "loading" while the page's external
    // /hdc_static/*.js tags go unfetched, so the script's own DOMContentLoaded
    // hook would never fire.  Dispatch it ourselves.
    if (document.readyState !== 'complete') {
        document.dispatchEvent(new window.Event('DOMContentLoaded', { bubbles: true }));
    }
    if (window.HDCResponsiveTables && window.HDCResponsiveTables.refresh) {
        window.HDCResponsiveTables.refresh();
    }

    check('table is marked as a card stack',
          table.classList.contains('hdc-rt-stack'),
          'class=' + table.className);
    check('table records its stack mode',
          table.getAttribute('data-hdc-rt') === 'stack',
          'data-hdc-rt=' + table.getAttribute('data-hdc-rt'));

    const rows = Array.from(table.tBodies[0].rows)
        .filter((row) => row.querySelector('.hdc-rename-view'));
    check('table has data rows', rows.length > 0);

    rows.forEach((row, index) => {
        const leads = Array.from(row.cells)
            .filter((cell) => cell.getAttribute('data-hdc-rt-lead') === '1');
        check(`row ${index + 1} has exactly one card title`, leads.length === 1,
              `${leads.length} lead cells`);

        const nameCell = Array.from(row.cells)
            .find((cell) => cell.querySelector('.hdc-rename-view'));
        check(`row ${index + 1} title is the name cell`,
              leads[0] === nameCell,
              leads[0] ? 'title cell class=' + leads[0].className : 'no title cell');
        check(`row ${index + 1} name cell carries the name`,
              nameCell && nameCell.textContent.trim().length > 0);

        const created = Array.from(row.cells)
            .find((cell) => cell.getAttribute('data-hdc-label') === 'Created');
        check(`row ${index + 1} does not title the created date`,
              created && created.getAttribute('data-hdc-rt-lead') !== '1');

        const actions = row.querySelector('.hdc-rename-actions');
        check(`row ${index + 1} keeps its actions`,
              actions && actions.getAttribute('data-hdc-rt-empty') === '0',
              'actions cell hidden from the record card');
        check(`row ${index + 1} actions cell holds both buttons`,
              actions && actions.querySelectorAll('.hdc-rename-icon-btn').length >= 2);
    });
}

if (failures.length) {
    console.error(failures.join('\n'));
    process.exit(1);
}
console.log('responsive_tables harness: OK');
