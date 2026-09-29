/* Shared Expenses — page behaviour (progressive enhancement only).
 *
 * Nothing here is required for the module to work: the form is complete and
 * server-rendered, the split is computed and validated in integer paisa on the
 * server, and every action is a normal form POST.  This script only makes the
 * page nicer to use:
 *
 *   1. shows the money-side panel that matches the chosen option;
 *   2. keeps the split table live, both ways — type the total and every ticked
 *      head's amount appears at once (equal split), type a percentage and that
 *      head's rupees appear at once, or type an amount and its percentage is
 *      worked out for you.  Whatever is typed, the row says whether the
 *      figures add up before the server has to;
 *   3. filters the (long) list of linkable Accounts entries;
 *   4. pre-fills the two sides of a settlement transfer from the parties'
 *      linked accounts.
 *
 * The arithmetic here is a mirror of ``hdc.services.shared_expenses``
 * (integer paisa, odd paisa handed out one by one, rounding drift absorbed by
 * the largest share) so what the operator sees is exactly what gets saved.
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

    function parseMoney(raw) {
        if (!raw) { return 0; }
        var clean = String(raw).replace(/,/g, '').replace(/\s/g, '').trim();
        if (!clean) { return 0; }
        var value = parseFloat(clean);
        return isFinite(value) ? value : NaN;
    }

    function toMinor(value) {
        // Mirrors hdc.utils.money: round half-up to 2dp, then integer paisa.
        return Math.round((value + Number.EPSILON) * 100);
    }

    function fmt(minor) {
        return (minor / 100).toLocaleString('en-US', {
            minimumFractionDigits: 2, maximumFractionDigits: 2
        });
    }

    function sum(list) {
        return list.reduce(function (a, b) { return a + b; }, 0);
    }

    /* ── 1. money-side panels ───────────────────────────────────────────── */
    function initMoneyPanels() {
        var radios = document.querySelectorAll('input[name="money_source"]');
        if (!radios.length) { return; }
        var panels = document.querySelectorAll('[data-money-panel]');
        var choices = document.querySelectorAll('[data-money-choice]');

        function render() {
            var chosen = '';
            radios.forEach(function (r) { if (r.checked) { chosen = r.value; } });
            panels.forEach(function (p) {
                p.classList.toggle('se-hidden', p.getAttribute('data-money-panel') !== chosen);
            });
            choices.forEach(function (c) {
                c.classList.toggle('active', c.getAttribute('data-money-choice') === chosen);
            });
        }
        radios.forEach(function (r) { r.addEventListener('change', render); });
        render();
    }

    /* ── 2. the split table — live, both ways ───────────────────────────── */
    function initSplitTable() {
        var form = document.getElementById('seExpenseForm');
        if (!form) { return; }
        var modeSel = document.getElementById('split_mode');
        var totalInput = document.getElementById('total_amount');
        var box = document.getElementById('seSplitCheck');
        if (!modeSel || !totalInput || !box) { return; }

        var rows = Array.prototype.slice.call(form.querySelectorAll('.se-split-row'))
            .map(function (el) {
                return {
                    el: el,
                    tick: el.querySelector('.se-party-tick'),
                    amount: el.querySelector('.se-amount'),
                    percent: el.querySelector('.se-percent')
                };
            })
            .filter(function (row) { return !!row.tick; });
        if (!rows.length) { return; }

        /* Money is paisa; a percentage is basis points (33.33% → 3333 bp). */
        function minorOf(input) {
            if (!input || !String(input.value || '').trim()) { return NaN; }
            var value = parseMoney(input.value);
            return isNaN(value) ? NaN : toMinor(value);
        }

        function valueOf(input) {
            var minor = minorOf(input);
            return isNaN(minor) ? 0 : minor;
        }

        function isBlank(input) {
            return !input || !String(input.value || '').trim();
        }

        /* 'auto'  = worked out by the page, shown tinted and read-only
         * 'live'  = worked out by the page, but typing in it still works
         * 'plain' = the operator's own figure                              */
        function putStyle(input, style) {
            if (!input) { return; }
            input.classList.toggle('se-auto', style === 'auto' || style === 'live');
            var readOnly = style === 'auto';
            if (input.readOnly !== readOnly) { input.readOnly = readOnly; }
        }

        function put(input, text, style) {
            if (!input) { return; }
            if (input.value !== text) { input.value = text; }
            putStyle(input, style);
        }

        function fmtPct(bp) {
            if (bp === null || bp === undefined || isNaN(bp)) { return ''; }
            return (bp / 100).toFixed(2);
        }

        function ticked() {
            return rows.filter(function (r) { return !!r.tick.checked; });
        }

        function equalShares(totalMinor, count) {
            // Odd paisa handed out one per head from the top, as the server does.
            var base = Math.floor(totalMinor / count);
            var odd = totalMinor - (base * count);
            var out = [];
            for (var i = 0; i < count; i++) { out.push(base + (i < odd ? 1 : 0)); }
            return out;
        }

        function largestIndex(values) {
            var index = 0;
            for (var i = 1; i < values.length; i++) {
                if (values[i] > values[index]) { index = i; }
            }
            return index;
        }

        function amountsFromBps(bps, totalMinor, absorb) {
            // Mirrors compute_split('percent'): half-up, drift into the largest
            // share.  The drift is only absorbed once the percentages really do
            // add up to 100% — while they do not, each head's rupees stay the
            // honest share of what has been typed so far (50% of 9,000 is
            // 4,500.00, not the whole bill minus everybody else).
            var out = bps.map(function (bp) {
                if (bp === null || isNaN(bp)) { return 0; }
                return Math.floor((totalMinor * bp + 5000) / 10000);
            });
            if (!absorb) { return out; }
            var diff = totalMinor - sum(out);
            if (diff) { out[largestIndex(out)] += diff; }
            return out;
        }

        function bpsFromAmounts(amounts, totalMinor) {
            if (!totalMinor) { return amounts.map(function () { return 0; }); }
            var out = amounts.map(function (amount) {
                return Math.round(amount * 10000 / totalMinor);
            });
            var diff = 10000 - sum(out);
            if (diff) { out[largestIndex(out)] += diff; }
            return out;
        }

        function hint(html) { box.innerHTML = html; }

        function render(opts) {
            opts = opts || {};
            var skip = opts.skip || null;          // the field being typed in
            var mode = modeSel.value || 'equal';
            var totalMinor = toMinor(parseMoney(totalInput.value) || 0);
            var on = ticked();
            // Which column the operator drives, and which one follows.
            var dress = {
                equal:   {amount: 'auto',  percent: 'auto'},
                percent: {amount: 'live',  percent: 'plain'},
                custom:  {amount: 'plain', percent: 'auto'}
            }[mode] || {amount: 'plain', percent: 'plain'};

            rows.forEach(function (r) {
                r.el.classList.toggle('se-split-off', !r.tick.checked);
                putStyle(r.amount, dress.amount);
                putStyle(r.percent, dress.percent);
            });
            box.classList.remove('ok', 'bad');

            if (!on.length) {
                hint('<span class="text-muted">Tick the heads that share this expense.</span>');
                return;
            }

            function clear(column, style) {
                on.forEach(function (r) { put(r[column], '', style); });
            }

            if (!totalMinor) {
                // Nothing to divide yet — drop whatever the page had derived.
                if (mode === 'equal') { clear('amount', 'auto'); clear('percent', 'auto'); }
                else if (mode === 'percent') { clear('amount', 'live'); }
                else { clear('percent', 'auto'); }
                hint('<span class="text-muted">Enter the total amount to split it ' +
                     on.length + ' way(s).</span>');
                return;
            }

            /* ── equal: one answer, worked out for every ticked head ────── */
            if (mode === 'equal') {
                var shares = equalShares(totalMinor, on.length);
                var base = Math.floor(totalMinor / on.length);
                var odd = totalMinor - (base * on.length);
                // Percentages are two decimals, so their rounding drift is
                // handed to the biggest share — they always add up to 100.00%.
                var equalBps = bpsFromAmounts(shares, totalMinor);
                on.forEach(function (r, i) {
                    if (r.amount !== skip) { put(r.amount, fmt(shares[i]), 'auto'); }
                    if (r.percent !== skip) { put(r.percent, fmtPct(equalBps[i]), 'auto'); }
                });
                hint('<strong>' + on.length + '-way equal split:</strong> ' + fmt(base) + ' each' +
                     (odd ? ' <span class="text-muted">· the odd ' + odd +
                            ' paisa handed out one by one</span>' : '') +
                     ' <span class="text-success">— adds up to ' + fmt(totalMinor) + '</span>');
                box.classList.add('ok');
                return;
            }

            /* ── percent: percentages drive it, rupees follow ───────────── */
            if (mode === 'percent') {
                if (opts.from === 'amount' && opts.row) {
                    // An amount typed straight in works out that head's percentage.
                    var typed = minorOf(opts.row.amount);
                    if (!isNaN(typed) && opts.row.percent !== skip) {
                        put(opts.row.percent,
                            fmtPct(Math.round(typed * 10000 / totalMinor)), 'plain');
                    }
                }
                var bps = [], missing = 0, nonsense = false;
                on.forEach(function (r) {
                    var bp = minorOf(r.percent);      // basis points, straight from the box
                    if (isNaN(bp)) { missing += 1; bps.push(null); return; }
                    if (bp < 0) { nonsense = true; }
                    bps.push(bp);
                });
                var totalBp = sum(bps.map(function (bp) { return bp || 0; }));
                var balanced = !missing && !nonsense && totalBp === 10000;
                var amounts = amountsFromBps(bps, totalMinor, balanced);
                var shown = [];
                on.forEach(function (r, i) {
                    if (r.amount !== skip || opts.from !== 'amount') {
                        put(r.amount, fmt(amounts[i]), 'live');
                    }
                    shown.push(fmt(amounts[i]));
                });
                hint('<strong>Percentages:</strong> ' + fmtPct(totalBp) + '% of 100% ' +
                     (balanced ? '<span class="text-success">— adds up</span>'
                               : '<span class="text-danger">— ' +
                                 (missing ? 'give every head a percentage'
                                          : nonsense ? 'percentages cannot be negative'
                                                     : 'must add up to 100%') + '</span>') +
                     '<br><span class="text-muted">' + shown.join(' · ') + '</span>');
                box.classList.add(balanced ? 'ok' : 'bad');
                return;
            }

            /* ── custom: amounts drive it, percentages follow ───────────── */
            var minors = [], assigned = 0, gaps = 0, bad = false;
            on.forEach(function (r) {
                var minor = minorOf(r.amount);
                if (isNaN(minor)) {
                    if (!isBlank(r.amount)) { bad = true; } else { gaps += 1; }
                    minors.push(null);
                    return;
                }
                if (minor < 0) { bad = true; }
                minors.push(minor);
                assigned += minor;
            });
            var diff = totalMinor - assigned;
            var adds = !gaps && !bad && diff === 0;
            var customBps = minors.map(function (minor) {
                return minor === null ? null : Math.round(minor * 10000 / totalMinor);
            });
            if (adds) {
                // The split is whole, so its percentages are too: the odd
                // hundredth of a percent goes to the biggest share.
                var drift = 10000 - sum(customBps);
                if (drift) { customBps[largestIndex(minors)] += drift; }
            }
            on.forEach(function (r, i) {
                if (r.percent !== skip) { put(r.percent, fmtPct(customBps[i]), 'auto'); }
            });
            hint('<strong>Assigned:</strong> ' + fmt(assigned) + ' of ' + fmt(totalMinor) +
                 (adds ? ' <span class="text-success">— adds up</span>'
                       : ' <span class="text-danger">— ' +
                         (bad ? 'check the amounts entered'
                              : diff > 0 ? fmt(diff) + ' still to assign'
                                         : fmt(-diff) + ' too much') + '</span>'));
            box.classList.add(adds ? 'ok' : 'bad');
        }

        /* Switching mode carries the figures across, so nothing is retyped. */
        function seedFigures(mode, on, totalMinor) {
            if (!on.length || !totalMinor) { return; }
            if (mode === 'percent') {
                if (!on.every(function (r) { return isBlank(r.percent); })) { return; }
                var amounts = on.map(function (r) { return valueOf(r.amount); });
                if (sum(amounts) !== totalMinor) { amounts = equalShares(totalMinor, on.length); }
                var bps = bpsFromAmounts(amounts, totalMinor);
                on.forEach(function (r, i) { put(r.percent, fmtPct(bps[i]), 'plain'); });
                return;
            }
            if (mode === 'custom') {
                if (!on.every(function (r) { return isBlank(r.amount); })) { return; }
                var bps2 = on.map(function (r) { return minorOf(r.percent); });
                var usable = bps2.every(function (bp) { return !isNaN(bp); });
                var amounts2 = (usable && sum(bps2.map(function (bp) { return bp || 0; })) === 10000)
                    ? amountsFromBps(bps2, totalMinor, true)
                    : equalShares(totalMinor, on.length);
                on.forEach(function (r, i) { put(r.amount, fmt(amounts2[i]), 'plain'); });
            }
        }

        function onType(row, column) {
            return function () {
                render({from: column, row: row, skip: row[column]});
            };
        }

        function onCommit(row, column) {
            return function () {
                // Leaving the field lets the page settle the figure (percentages
                // are two decimals, so a typed amount can shift by a paisa).
                render({from: column, row: row});
            };
        }

        rows.forEach(function (row) {
            row.tick.addEventListener('change', function () { render({}); });
            ['amount', 'percent'].forEach(function (column) {
                var input = row[column];
                if (!input) { return; }
                input.addEventListener('input', onType(row, column));
                input.addEventListener('change', onCommit(row, column));
            });
        });

        totalInput.addEventListener('input', function () { render({}); });
        totalInput.addEventListener('change', function () { render({}); });

        modeSel.addEventListener('change', function () {
            var totalMinor = toMinor(parseMoney(totalInput.value) || 0);
            seedFigures(modeSel.value, ticked(), totalMinor);
            render({});
        });

        render({});
    }

    /* ── 3. filter the linkable Accounts entries ────────────────────────── */
    function initEntrySearch() {
        var box = document.getElementById('seEntrySearch');
        if (!box) { return; }
        var selects = document.querySelectorAll('select[name="cf_entry_id"]');
        selects.forEach(function (sel) {
            box.addEventListener('input', function () {
                var needle = box.value.toLowerCase().trim();
                Array.prototype.forEach.call(sel.options, function (opt) {
                    var hay = (opt.getAttribute('data-search') || opt.text || '').toLowerCase();
                    opt.hidden = needle !== '' && hay.indexOf(needle) === -1;
                });
            });
        });
    }

    /* ── 4. settlement transfer pre-fill ────────────────────────────────── */
    function initSettlement() {
        var post = document.getElementById('postTransfer');
        if (!post) { return; }
        var fields = document.getElementById('seTransferFields');
        var from = document.getElementById('from_party_id');
        var to = document.getElementById('to_party_id');
        var toAccount = document.getElementById('to_account_id');
        var fromAccount = document.getElementById('from_account_id');
        var destAccount = document.getElementById('settlement_to_account_id');

        function accountOf(select) {
            if (!select || !select.selectedOptions || !select.selectedOptions.length) { return ''; }
            return select.selectedOptions[0].getAttribute('data-account-id') || '';
        }

        function sync() {
            if (fields) { fields.style.opacity = post.checked ? '1' : '.55'; }
            var src = accountOf(from);
            if (src && fromAccount && !fromAccount.value) { fromAccount.value = src; }
            var dst = accountOf(to);
            if (toAccount && toAccount.value) { dst = toAccount.value; }
            if (dst && destAccount && !destAccount.value) { destAccount.value = dst; }
        }
        [post, from, to, toAccount].forEach(function (el) {
            if (el) { el.addEventListener('change', sync); }
        });
        sync();
    }

    onReady(function () {
        initMoneyPanels();
        initSplitTable();
        initEntrySearch();
        initSettlement();
    });
})();
