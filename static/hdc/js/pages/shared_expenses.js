/* Shared Expenses — page behaviour (progressive enhancement only).
 *
 * Nothing here is required for the module to work: the form is complete and
 * server-rendered, the split is computed and validated in integer paisa on the
 * server, and every action is a normal form POST.  This script only makes the
 * page nicer to use:
 *
 *   1. shows the money-side panel that matches the chosen option;
 *   2. previews the split while typing (equal / custom / percent) and says
 *      whether the figures add up before the server has to;
 *   3. filters the (long) list of linkable Accounts entries;
 *   4. pre-fills the two sides of a settlement transfer from the parties'
 *      linked accounts.
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

    /* ── 2. split preview ───────────────────────────────────────────────── */
    function initSplitCheck() {
        var form = document.getElementById('seExpenseForm');
        if (!form) { return; }
        var modeSel = document.getElementById('split_mode');
        var totalInput = document.getElementById('total_amount');
        var ticks = Array.prototype.slice.call(form.querySelectorAll('.se-party-tick'));
        var box = document.getElementById('seSplitCheck');
        if (!modeSel || !totalInput || !box || !ticks.length) { return; }

        function rows() {
            return ticks.map(function (tick) {
                var row = tick.closest('.se-split-row');
                return {
                    tick: tick,
                    amount: row.querySelector('.se-amount'),
                    percent: row.querySelector('.se-percent')
                };
            });
        }

        function setHint(html) { box.innerHTML = html; }

        function render(fillEmpty) {
            var mode = modeSel.value;
            var totalMinor = toMinor(parseMoney(totalInput.value) || 0);
            var picked = rows().filter(function (r) { return r.tick.checked; });
            box.classList.remove('ok', 'bad');

            if (!picked.length) {
                setHint('<span class="text-muted">Tick the heads that share this expense.</span>');
                return;
            }
            if (!totalMinor) {
                setHint('<span class="text-muted">Enter the total amount to see how it splits ' +
                        picked.length + ' way(s).</span>');
                return;
            }

            if (mode === 'equal') {
                var base = Math.floor(totalMinor / picked.length);
                var odd = totalMinor - (base * picked.length);
                var parts = picked.map(function (r, i) {
                    var minor = base + (i < odd ? 1 : 0);
                    if (fillEmpty && r.amount && !r.amount.value.trim()) { r.amount.value = fmt(minor); }
                    return fmt(minor);
                });
                setHint('<strong>' + picked.length + ' way split:</strong> ' + parts.join(' · ') +
                        ' <span class="text-muted">(each ' + fmt(base) +
                        (odd ? ', the odd ' + odd + ' paisa handed out first' : '') + ')</span>');
                box.classList.add('ok');
                return;
            }

            if (mode === 'percent') {
                var sumBp = 0, bad = false;
                picked.forEach(function (r) {
                    var pct = parseMoney(r.percent ? r.percent.value : '');
                    if (isNaN(pct)) { bad = true; pct = 0; }
                    sumBp += Math.round(pct * 100);
                });
                var ok = !bad && sumBp === 10000;
                var preview = picked.map(function (r) {
                    var pct = parseMoney(r.percent ? r.percent.value : '') || 0;
                    return fmt(Math.round(pct * 100) * Math.round(totalMinor / 1) / 10000 | 0);
                }).join(' · ');
                setHint('<strong>Percentages:</strong> ' + (sumBp / 100).toFixed(2) + '% of 100% ' +
                        (ok ? '<span class="text-success">— adds up</span>' :
                              '<span class="text-danger">— must add up to 100%</span>'));
                box.classList.add(ok ? 'ok' : 'bad');
                return;
            }

            /* custom */
            var sumMinor = 0, invalid = false;
            picked.forEach(function (r) {
                var v = parseMoney(r.amount ? r.amount.value : '');
                if (r.amount && r.amount.value.trim() === '') { invalid = true; }
                if (isNaN(v)) { invalid = true; return; }
                sumMinor += toMinor(v);
            });
            var diff = totalMinor - sumMinor;
            setHint('<strong>Typed shares:</strong> ' + fmt(sumMinor) + ' of ' + fmt(totalMinor) +
                    (diff === 0 ? ' <span class="text-success">— adds up</span>'
                                : ' <span class="text-danger">— ' +
                                  (diff > 0 ? 'still ' + fmt(diff) + ' to assign'
                                            : fmt(-diff) + ' too much') + '</span>'));
            box.classList.add(diff === 0 && !invalid ? 'ok' : 'bad');
        }

        var lastMode = modeSel.value;
        modeSel.addEventListener('change', function () {
            // Switching Equal → Custom fills the boxes with the equal split as a
            // starting point (only into empty boxes, never over typed figures).
            render(modeSel.value === 'custom' && lastMode === 'equal');
            lastMode = modeSel.value;
        });
        totalInput.addEventListener('input', function () { render(false); });
        ticks.forEach(function (t) {
            t.addEventListener('change', function () { render(modeSel.value === 'equal'); });
        });
        rows().forEach(function (r) {
            [r.amount, r.percent].forEach(function (input) {
                if (input) { input.addEventListener('input', function () { render(false); }); }
            });
        });
        render(true);
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
        initSplitCheck();
        initEntrySearch();
        initSettlement();
    });
})();
