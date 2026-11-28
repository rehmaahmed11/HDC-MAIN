/* HDC ERP — core/forms.js
   Form usability & speed layer (loaded by shared/base.html):

     1. Auto-dismisses non-blocking flash alerts after 4 seconds.
     2. Guards against accidental double-submits (double-click / slow network).
     3. Ctrl+Enter / Cmd+Enter submits the active form from any field.
     4. Auto-focuses the first visible input when a Bootstrap modal opens.
     5. Shows a live thousand-separated PKR readout beneath currency fields
        (`data-hdc-money` or common amount/wage/rate inputs) to prevent 10×
        zero-entry typos.
     6. Wires collapsible quick-entry cards (`[data-hdc-entry-drawer]`) and
        compact mobile filter toggles (< 768px) to keep register tables above
        the fold.
*/
(function () {
    'use strict';

    // ── 1. AUTO-DISMISS ALERTS ──
    setTimeout(function () {
        var alerts = document.querySelectorAll('.alert.alert-success, .alert.alert-info');
        Array.prototype.forEach.call(alerts, function (el) {
            if (typeof bootstrap !== 'undefined' && bootstrap && bootstrap.Alert) {
                var bsAlert = new bootstrap.Alert(el);
                bsAlert.close();
            }
        });
    }, 4000);

    // ── 2. DOUBLE-SUBMIT GUARD ──
    document.addEventListener('submit', function (e) {
        var form = e.target;
        if (!form || form.tagName !== 'FORM') return;
        if (form.hasAttribute && form.hasAttribute('data-own-submit')) return;
        if (form.dataset && form.dataset.submitted === '1') {
            e.preventDefault();
            return;
        }
        if (form.dataset) form.dataset.submitted = '1';
        var submitters = form.querySelectorAll('button[type="submit"], input[type="submit"]');
        Array.prototype.forEach.call(submitters, function (btn) { btn.disabled = true; });
        setTimeout(function () {
            if (form.dataset) form.dataset.submitted = '0';
            Array.prototype.forEach.call(submitters, function (btn) { btn.disabled = false; });
        }, 8000);
    }, true);

    // ── 3. CTRL+ENTER / CMD+ENTER FORM SUBMIT ──
    document.addEventListener('keydown', function (e) {
        if (!e || !(e.ctrlKey || e.metaKey) || e.key !== 'Enter') return;
        var target = e.target;
        if (!target || !target.closest) return;
        var form = target.closest('form');
        if (!form) return;
        e.preventDefault();
        if (typeof form.requestSubmit === 'function') {
            form.requestSubmit();
        } else if (typeof form.submit === 'function') {
            form.submit();
        }
    });

    // ── 4. MODAL AUTO-FOCUS ──
    document.addEventListener('shown.bs.modal', function (e) {
        var modal = e && e.target;
        if (!modal || !modal.querySelectorAll) return;
        if (modal.id === 'hdcResultDialog' || modal.id === 'hdcConfirmDialog' || modal.id === 'hdcQuickJumpModal') return;
        var controls = modal.querySelectorAll('input, select, textarea');
        for (var i = 0; i < controls.length; i += 1) {
            var el = controls[i];
            if (el.disabled || el.readOnly) continue;
            var type = String(el.type || '').toLowerCase();
            if (type === 'hidden' || type === 'submit' || type === 'button' || type === 'checkbox' || type === 'radio') continue;
            if (el.classList && el.classList.contains('d-none')) continue;
            try { el.focus(); break; } catch (err) { /* ignore */ }
        }
    });

    // ── 5. LIVE PKR READOUT & NUMERIC INPUTMODE ──
    var MONEY_NAME_RE = /(?:^|_)(?:amount|daily_wage|hourly_rate|rate_per_sqft|unit_price|lump_sum_amount|contract_value|total_amount|paid_amount|salary|allowance)(?:$|\[\])/i;

    function formatPKR(raw) {
        var n = Number(raw);
        if (!isFinite(n) || n === 0) return '';
        var abs = Math.abs(n);
        if (abs < 1000) return '';
        var sign = n < 0 ? '-' : '';
        var formatted = abs.toLocaleString('en-US', { maximumFractionDigits: 2 });
        var scale = '';
        if (abs >= 10000000) scale = ' (' + (abs / 10000000).toFixed(2).replace(/\.?0+$/, '') + ' Cr)';
        else if (abs >= 100000) scale = ' (' + (abs / 100000).toFixed(2).replace(/\.?0+$/, '') + ' Lakh)';
        return sign + 'Rs. ' + formatted + scale;
    }

    function wireMoneyHints() {
        if (!document.querySelectorAll || !document.createElement) return;
        var inputs = document.querySelectorAll('input[type="number"]');
        Array.prototype.forEach.call(inputs, function (inp) {
            if (!inp.getAttribute('inputmode')) {
                inp.setAttribute('inputmode', 'decimal');
            }
            var explicit = inp.hasAttribute && inp.hasAttribute('data-hdc-money');
            var name = inp.getAttribute('name') || '';
            if (!explicit && !MONEY_NAME_RE.test(name)) return;
            if (inp.closest && inp.closest('table')) return;
            if (inp.dataset && inp.dataset.hdcMoneyWired === '1') return;
            if (inp.dataset) inp.dataset.hdcMoneyWired = '1';
            if (!inp.parentNode || !inp.parentNode.appendChild) return;

            var parent = inp.parentNode;
            var host = (parent.classList && parent.classList.contains('input-group') && parent.parentNode)
                ? parent.parentNode
                : parent;
            var hint = document.createElement('small');
            hint.className = 'hdc-money-hint';
            hint.setAttribute('aria-live', 'polite');
            host.appendChild(hint);

            var update = function () {
                hint.textContent = formatPKR(inp.value);
            };
            inp.addEventListener('input', update);
            inp.addEventListener('change', update);
            update();
        });
    }

    // ── 6. COLLAPSIBLE / PINNABLE QUICK-ENTRY DRAWERS & MOBILE FILTER PILL ──
    function hasQueryFilters() {
        if (typeof window === 'undefined' || !window.location || !window.location.search) return false;
        var s = window.location.search.replace(/^\?/, '');
        if (!s) return false;
        return s.split('&').some(function (pair) {
            var parts = pair.split('=');
            var key = decodeURIComponent(parts[0] || '');
            var val = decodeURIComponent(parts[1] || '').trim();
            if (!key || !val) return false;
            if (key === 'page' && val === '1') return false;
            return true;
        });
    }

    function wireEntryDrawers() {
        if (!document.querySelectorAll) return;
        var drawers = document.querySelectorAll('[data-hdc-entry-drawer]');
        Array.prototype.forEach.call(drawers, function (card) {
            var key = 'hdc_drawer_' + (card.getAttribute('data-hdc-entry-drawer') || 'default');
            var body = card.querySelector('[data-hdc-entry-body]');
            var toggleBtn = card.querySelector('[data-hdc-entry-toggle]');
            var pinBtn = card.querySelector('[data-hdc-entry-pin]');
            if (!body || !toggleBtn) return;

            var saved = null;
            try { saved = window.localStorage.getItem(key); } catch (err) { /* ignore */ }

            // Start open on fresh visit; auto-collapse when operator actively
            // filtered or paged the table unless they pinned the form open.
            var pinned = saved === 'pinned';
            var open = pinned ? true : (saved === 'closed' ? false : !hasQueryFilters());

            function applyState() {
                body.hidden = !open;
                toggleBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
                if (pinBtn) {
                    pinBtn.setAttribute('aria-pressed', pinned ? 'true' : 'false');
                    pinBtn.setAttribute('title', pinned ? 'Unpin form (auto-collapse when filtering)' : 'Keep form pinned open');
                }
            }

            toggleBtn.addEventListener('click', function () {
                open = !open;
                if (!open && pinned) pinned = false;
                try {
                    window.localStorage.setItem(key, pinned ? 'pinned' : (open ? 'open' : 'closed'));
                } catch (err) { /* ignore */ }
                applyState();
            });

            if (pinBtn) {
                pinBtn.addEventListener('click', function () {
                    pinned = !pinned;
                    if (pinned) open = true;
                    try {
                        window.localStorage.setItem(key, pinned ? 'pinned' : (open ? 'open' : 'closed'));
                    } catch (err) { /* ignore */ }
                    applyState();
                });
            }

            applyState();
        });
    }

    function wireMobileFilters() {
        if (!document.querySelectorAll || !document.createElement) return;
        var getForms = document.querySelectorAll('form[method="get"], form[method="GET"]');
        Array.prototype.forEach.call(getForms, function (form) {
            if (form.classList && form.classList.contains('hdc-jump-page')) return;
            var controls = form.querySelectorAll('input, select');
            var visibleCount = 0;
            var activeCount = 0;
            Array.prototype.forEach.call(controls, function (c) {
                var t = String(c.type || '').toLowerCase();
                if (t === 'hidden' || t === 'submit' || t === 'button') return;
                if (c.hasAttribute && c.hasAttribute('data-hdc-combo-source')) return;
                visibleCount += 1;
                if (c.value && String(c.value).trim() !== '') activeCount += 1;
            });
            if (visibleCount < 3 || !form.parentNode || !form.parentNode.insertBefore) return;

            var btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'btn btn-sm btn-outline-secondary hdc-mobile-filter-toggle';
            var startOpen = activeCount > 0;
            if (!startOpen && form.classList) form.classList.add('hdc-mobile-filter-collapsed');

            function syncLabel() {
                var collapsed = form.classList && form.classList.contains('hdc-mobile-filter-collapsed');
                btn.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
                btn.textContent = (collapsed ? 'Show Filters' : 'Hide Filters') +
                    (activeCount > 0 ? ' (' + activeCount + ' active)' : '');
            }

            btn.addEventListener('click', function () {
                if (form.classList) form.classList.toggle('hdc-mobile-filter-collapsed');
                syncLabel();
            });
            syncLabel();
            form.parentNode.insertBefore(btn, form);
        });
    }

    function onReady() {
        wireMoneyHints();
        wireEntryDrawers();
        wireMobileFilters();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', onReady, { once: true });
    } else {
        onReady();
    }
})();
