/* HDC ERP — core/responsive_tables.js
 * ---------------------------------------------------------------------------
 * Makes every ordinary data table readable on a phone without touching a
 * single template.
 *
 * THE PROBLEM
 *   A ledger with nine columns is comfortable on a laptop and unusable on a
 *   360px phone: the row runs off the right edge, so the operator scrolls
 *   sideways for every value and loses the thread of the record.
 *
 * WHAT THIS DOES
 *   1. Reads the column headings of the table (`<thead>` last row).
 *   2. Copies each heading onto every matching cell as `data-hdc-label`, and
 *      marks the cell that leads the row (date / code / name) as
 *      `data-hdc-rt-lead="1"`.
 *   3. Marks the table `hdc-rt-stack` — `css/responsive.css` then renders one
 *      row as one card, with the heading printed above each value.
 *   4. Leaves complicated tables alone.  A table whose *rows* are built out
 *      of `colspan`/`rowspan` (an audit matrix, a printable grid) is a
 *      spreadsheet, not a list: it keeps its shape and, because the responsive
 *      stylesheet drops its `min-width`, it simply fits the screen.  A single
 *      totals row or group heading does NOT disqualify a table — those rows
 *      are marked `data-hdc-rt-band` and render as a full-width strip between
 *      the cards, so the operator still gets the cards *and* the total.
 *
 *   Nothing is hidden, nothing is removed and nothing is reordered — the same
 *   DOM is just laid out for the screen it is on.  Above 768px the stylesheet
 *   switches back to the real table and this script's work is inert, so the
 *   laptop view is byte-for-byte the layout the team already knows.
 *
 * IDEMPOTENT AND LIVE
 *   `HDCResponsiveTables.refresh()` can be called at any time; it recomputes
 *   labels from the live DOM.  It is wired to DOMContentLoaded, window load,
 *   Bootstrap modal openings and a debounced MutationObserver, so rows added
 *   by page scripts (new tool lines, delivery rows, the "Entered by" column
 *   appended by core/audit.js) are labelled the moment they appear.
 */
(function () {
    'use strict';

    var LEAD_MAX_LEN = 60;      // a "title" cell, not a paragraph
    var LEAD_MAX_LOOK = 4;      // how far into the row to look for one

    function clean(text) {
        return String(text || '')
            .replace(/\s+/g, ' ')
            .replace(/[*:]+$/g, '')
            .trim();
    }

    function spanMoreThanOne(node) {
        var raw = node.getAttribute('colspan') || node.getAttribute('rowspan');
        if (!raw) return false;
        var value = parseInt(raw, 10);
        return isNaN(value) ? false : value > 1;
    }

    function optedOut(table) {
        if (!table || table.nodeName !== 'TABLE') return true;
        if (table.classList.contains('hdc-no-stack')) return true;
        if (table.classList.contains('no-hdc-stack')) return true;
        return !!(table.closest && table.closest('[data-hdc-no-stack]'));
    }

    /* The heading row: the last row of <thead> when there are stacked header
       rows (a group title above the real column names). */
    function headingRow(table) {
        var head = table.tHead;
        if (!head || !head.rows.length) return null;
        return head.rows[head.rows.length - 1];
    }

    function headingLabels(row) {
        var labels = [];
        for (var i = 0; i < row.cells.length; i++) {
            var cell = row.cells[i];
            if (spanMoreThanOne(cell)) return null;   // merged header: keep the grid
            labels.push(clean(cell.textContent));
        }
        return labels;
    }

    function rowHasMerge(row) {
        for (var i = 0; i < row.cells.length; i++) {
            if (spanMoreThanOne(row.cells[i])) return true;
        }
        return false;
    }

    /* Placeholder text that carries no information: a card must not spend a
       line on it. */
    var PLACEHOLDERS = { '': 1, '-': 1, '--': 1, '—': 1, '–': 1, 'n/a': 1,
                         'na': 1, 'none': 1, 'null': 1, 'nil': 1, '.': 1 };

    function isEmptyText(text) {
        return !!PLACEHOLDERS[String(text).trim().toLowerCase()];
    }

    /* Which cell reads best as the card title: the first one that has text,
       is short, and is not a bank of buttons or a checkbox. */
    function markLead(row) {
        var cells = row.cells;
        var limit = Math.min(cells.length, LEAD_MAX_LOOK);
        for (var i = 0; i < limit; i++) {
            var cell = cells[i];
            if (!cell) continue;
            var text = clean(cell.textContent);
            if (!text || text.length > LEAD_MAX_LEN) continue;
            if (cell.querySelector('input[type="checkbox"], input[type="radio"], .btn, button, a.btn')) continue;
            cell.setAttribute('data-hdc-rt-lead', '1');
            return;
        }
        /* Nothing qualifies (a row of tick boxes, say): the first non-empty
           cell still anchors the card. */
        for (var j = 0; j < cells.length; j++) {
            if (clean(cells[j].textContent)) {
                cells[j].setAttribute('data-hdc-rt-lead', '1');
                return;
            }
        }
    }

    function labelRow(row, labels) {
        var cells = row.cells;
        for (var i = 0; i < cells.length; i++) {
            var cell = cells[i];
            var text = clean(cell.textContent);
            cell.setAttribute('data-hdc-label', labels[i] || '');
            cell.setAttribute('data-hdc-rt-empty', isEmptyText(text) ? '1' : '0');
            cell.removeAttribute('data-hdc-rt-lead');
        }
        markLead(row);
    }

    /* A totals row or a "Project A — 3 stages" group heading: it spans the
       table, so it cannot become a card.  It becomes a full-width strip
       instead, keeping the numbers it carries. */
    function markBand(row) {
        row.setAttribute('data-hdc-rt-band', '1');
        var cells = row.cells;
        for (var i = 0; i < cells.length; i++) {
            cells[i].removeAttribute('data-hdc-label');
            cells[i].removeAttribute('data-hdc-rt-lead');
            cells[i].setAttribute('data-hdc-rt-empty',
                isEmptyText(clean(cells[i].textContent)) ? '1' : '0');
        }
    }

    function prepare(table) {
        if (optedOut(table)) return;

        var body = table.tBodies && table.tBodies[0];
        if (!body || !body.rows.length) {
            table.setAttribute('data-hdc-rt', 'plain');
            return;
        }

        var head = headingRow(table);
        if (!head || !head.cells.length) {
            /* No <thead> at all: there is no heading text to label the cards
               with, so the table keeps its shape and fits the screen. */
            table.setAttribute('data-hdc-rt', 'scroll');
            return;
        }

        /* A single "No records yet" cell is not a data table. */
        if (body.rows.length === 1 && body.rows[0].cells.length === 1 &&
            spanMoreThanOne(body.rows[0].cells[0])) {
            table.setAttribute('data-hdc-rt', 'empty');
            return;
        }

        var labels = headingLabels(head);
        if (!labels) {
            table.setAttribute('data-hdc-rt', 'scroll');
            return;
        }

        /* Decide row by row.  Most rows are ordinary records; a handful span
           the table on purpose (a total, a group heading) and are kept as
           full-width strips.  If the spanning rows are the rule rather than
           the exception, this is a matrix and the grid is the honest layout. */
        var allRows = [];
        var bodies = table.tBodies;
        for (var b = 0; b < bodies.length; b++) {
            allRows = allRows.concat(Array.prototype.slice.call(bodies[b].rows));
        }
        if (table.tFoot) {
            allRows = allRows.concat(Array.prototype.slice.call(table.tFoot.rows));
        }

        var bands = [];
        var plain = [];
        for (var r = 0; r < allRows.length; r++) {
            var row = allRows[r];
            /* Idempotent: a row that spanned on an earlier pass may not now. */
            row.removeAttribute('data-hdc-rt-band');
            (rowHasMerge(row) ? bands : plain).push(row);
        }

        if (!plain.length) {
            table.setAttribute('data-hdc-rt', 'scroll');
            return;
        }
        /* Allow a small number of spanning rows: at most one per four records,
           and never more than three of them. */
        if (bands.length > 3 && bands.length > plain.length / 4) {
            table.setAttribute('data-hdc-rt', 'scroll');
            return;
        }

        for (var p = 0; p < plain.length; p++) labelRow(plain[p], labels);
        for (var k = 0; k < bands.length; k++) markBand(bands[k]);

        table.setAttribute('data-hdc-rt', 'stack');
        if (!table.classList.contains('hdc-rt-stack')) table.classList.add('hdc-rt-stack');
    }

    function scan() {
        var tables = document.querySelectorAll('table');
        for (var i = 0; i < tables.length; i++) prepare(tables[i]);
    }

    var pending = null;
    function refreshSoon() {
        if (pending) return;
        pending = setTimeout(function () {
            pending = null;
            scan();
        }, 120);
    }

    function observe() {
        if (!window.MutationObserver) return;
        var observer = new MutationObserver(function (records) {
            for (var i = 0; i < records.length; i++) {
                var added = records[i].addedNodes;
                for (var j = 0; j < added.length; j++) {
                    var node = added[j];
                    if (node.nodeType === 1 &&
                        (node.tagName === 'TR' || node.tagName === 'TABLE' ||
                         node.tagName === 'TD' || node.tagName === 'TH' ||
                         node.querySelector('tr, table'))) {
                        refreshSoon();
                        return;
                    }
                }
            }
        });
        observer.observe(document.body, { childList: true, subtree: true });
    }

    window.HDCResponsiveTables = { refresh: scan, prepare: prepare };

    function init() {
        scan();
        /* core/audit.js appends its "Entered by" column from an async fetch,
           so the first pass can be one column short: recompute once the page
           has settled. */
        window.addEventListener('load', scan);
        document.addEventListener('shown.bs.modal', refreshSoon);
        observe();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
