/* Tool Audit — live variance while a site counts (progressive enhancement only).
 *
 * The sheet works with no JavaScript at all: every box posts a normal form
 * field, the server computes book − counted, stores the variance and re-renders
 * it.  This script only saves the counter from doing arithmetic in their head
 * while typing:
 *
 *   1. each row shows its difference the moment a number is typed (and says
 *      "not counted" again if the box is cleared, which is deliberately
 *      different from a typed 0);
 *   2. the sheet header totals (counted, short, extra, discrepancies, blank
 *      lines) follow the form as it is filled in;
 *   3. the Adjust button is disabled until the confirm box says ADJUST, so a
 *      write-off cannot be posted by a stray Enter on the last input.
 *
 * The numbers here are a mirror of ``hdc.services.tool_audit`` — nothing is
 * trusted from here, the server recomputes all of it.
 */
(function () {
    'use strict';

    function onReady(fn) {
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', fn);
        } else {
            fn();
        }
    }

    function number(raw) {
        if (raw === null || raw === undefined || String(raw).trim() === '') { return null; }
        var value = parseFloat(String(raw).replace(/,/g, '').trim());
        return isFinite(value) ? value : null;
    }

    function qty(value) {
        var rounded = Math.round(value * 100) / 100;
        return String(rounded);
    }

    function signed(value) {
        return (value > 0 ? '+' : '') + qty(value);
    }

    function money(value) {
        return Math.round(value).toLocaleString('en-US');
    }

    function refreshRow(row) {
        var input = row.querySelector('.audit-count');
        var diff = row.querySelector('.audit-diff');
        if (!input || !diff) { return; }
        var book = number(row.getAttribute('data-book')) || 0;
        var counted = number(input.value);
        var extra = number((row.querySelector('.audit-damaged') || {}).value);
        var variance = counted === null ? null : counted - book;
        var unit = row.getAttribute('data-unit') || '';
        var cost = number(row.getAttribute('data-unit-cost'));

        diff.setAttribute('data-variance', variance === null ? '' : String(variance));
        row.classList.remove('is-off', 'is-dry');
        if (variance === null) {
            diff.innerHTML = '—';
            diff.classList.remove('text-danger', 'text-info');
            row.classList.add('is-dry');
        } else {
            var value = Math.round(variance * 100) / 100;
            var amount = (cost === null || value === 0) ? '' :
                ' <small class="d-block text-muted">' + money(value * cost) + ' PKR</small>';
            diff.innerHTML = (value === 0 ? '0' : signed(value)) + ' ' +
                (unit ? '<small class="d-block text-muted">' + unit + '</small>' : '') + amount;
            diff.classList.toggle('text-danger', value < 0);
            diff.classList.toggle('text-info', value > 0);
            if (value !== 0) { row.classList.add('is-off'); }
        }
        if (extra) { row.classList.add('has-damage'); }
    }

    function refreshTotals(form) {
        var countedTotal = 0;
        var bookTotal = 0;
        var shortage = 0;
        var overage = 0;
        var damaged = 0;
        var discrepancies = 0;
        var blanks = 0;
        var entered = 0;

        form.querySelectorAll('.tool-audit-line').forEach(function (row) {
            var book = number(row.getAttribute('data-book')) || 0;
            var input = row.querySelector('.audit-count');
            var counted = input ? number(input.value) : null;
            var extra = number((row.querySelector('.audit-damaged') || {}).value) || 0;
            if (counted === null) {
                if (book > 0) { blanks += 1; }
                return;
            }
            entered += 1;
            countedTotal += counted;
            bookTotal += book;
            var variance = Math.round((counted - book) * 100) / 100;
            if (variance < 0) { shortage += -variance; discrepancies += 1; }
            else if (variance > 0) { overage += variance; discrepancies += 1; }
            damaged += extra;
        });

        function setText(name, value) {
            var target = document.querySelector('[data-audit-live="' + name + '"]');
            if (target) { target.textContent = value; }
        }
        setText('counted', qty(countedTotal));
        setText('shortage', qty(shortage));
        setText('overage', qty(overage));
        setText('damaged', qty(damaged));
        setText('discrepancies', String(discrepancies));
        setText('blanks', String(blanks));
        setText('entered', String(entered));
        setText('net', signed(Math.round((countedTotal - bookTotal) * 100) / 100));

        var panel = document.querySelector('[data-audit-live="summary"]');
        if (panel) { panel.classList.toggle('has-variance', discrepancies > 0); }
    }

    function guardAdjustButton(form) {
        var confirmBox = form.querySelector('input[name="confirm"]');
        var button = document.getElementById('auditAdjustButton');
        if (!confirmBox || !button) { return; }
        var sync = function () {
            var ok = confirmBox.value.trim().toUpperCase() === 'ADJUST';
            button.disabled = !ok;
            button.classList.toggle('btn-danger', ok);
            button.classList.toggle('btn-secondary', !ok);
            button.title = ok ? '' : 'Type ADJUST to confirm — this writes stock off';
        };
        confirmBox.addEventListener('input', sync);
        sync();
    }

    onReady(function () {
        var sheet = document.getElementById('auditSheetForm');
        if (sheet) {
            sheet.querySelectorAll('.audit-count, .audit-damaged').forEach(function (input) {
                var row = input.closest('.tool-audit-line');
                if (!row) { return; }
                input.addEventListener('input', function () {
                    refreshRow(row);
                    refreshTotals(sheet);
                });
            });
            refreshTotals(sheet);
        }
        document.querySelectorAll('.tool-audit-line').forEach(refreshRow);
        document.querySelectorAll('form').forEach(guardAdjustButton);
    });
}());
