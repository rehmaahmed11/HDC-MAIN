/* HDC ERP — purchase_delivery.js
 *
 * Shared behaviour for the two stock registers of the Purchase V2 section:
 * the Delivery register (templates/hdc/purchase/purchase_v2_delivered.html)
 * and the Material Usage log (templates/hdc/purchase/purchase_v2_usage.html).
 *
 * Three things live here, all of them requested from the field:
 *
 *   1. the Pending Purchases pop-up: every purchase order that still owes
 *      stock, filterable by site / material / supplier / free text.  The rows
 *      are rendered server-side with the data the filter needs, so filtering
 *      is local and the pop-up never reloads out from under the operator;
 *   2. the "All Sites" / "All Stages" buttons that put either register back to
 *      its unfiltered view in one click;
 *   3. "Use PO": pre-fill the Record Delivery form straight from a pending row.
 *
 * The decision logic is split out as pure functions (``pendingRowFromElement``,
 * ``pendingMatches``, ``summarisePending``) so tests/purchase_delivery_harness.js
 * can exercise it under Node without a browser.
 */
(function (root) {
    'use strict';

    // ── pending purchase filtering (pure) ──────────────────────────────────

    /**
     * Read one pending-purchase row's filter data out of the rendered <tr>.
     * ``data-site-ids`` is the comma-joined list of sites this PO has already
     * delivered to — a PO is bought at store level and only becomes
     * site-specific once stock moves, which is what the site filter means.
     */
    function pendingRowFromElement(el) {
        var rawSites = (el.getAttribute('data-site-ids') || '');
        return {
            element: el,
            poId: el.getAttribute('data-po-id') || '',
            siteIds: rawSites.split(',').map(function (s) { return s.trim(); })
                .filter(function (s) { return s !== ''; }),
            materialId: el.getAttribute('data-material-id') || '',
            supplierId: el.getAttribute('data-supplier-id') || '',
            search: (el.getAttribute('data-search') || '').toLowerCase(),
            pendingQty: parseFloat(el.getAttribute('data-pending-qty') || '0') || 0
        };
    }

    /** True when a pending row survives the current filter criteria. */
    function pendingMatches(row, criteria) {
        criteria = criteria || {};
        if (criteria.siteId && row.siteIds.indexOf(String(criteria.siteId)) === -1) {
            return false;
        }
        if (criteria.materialId && String(row.materialId) !== String(criteria.materialId)) {
            return false;
        }
        if (criteria.supplierId && String(row.supplierId) !== String(criteria.supplierId)) {
            return false;
        }
        var term = String(criteria.term || '').trim().toLowerCase();
        if (term && row.search.indexOf(term) === -1) {
            return false;
        }
        return true;
    }

    /** ``{shown, pendingQty, visible}`` for a set of pending rows. */
    function summarisePending(rows, criteria) {
        var shown = 0;
        var qty = 0;
        var visible = [];
        (rows || []).forEach(function (row) {
            if (pendingMatches(row, criteria)) {
                shown += 1;
                qty += row.pendingQty || 0;
                visible.push(row);
            }
        });
        return { shown: shown, pendingQty: qty, visible: visible };
    }

    function formatQty(value) {
        var n = parseFloat(value || 0) || 0;
        if (typeof n.toLocaleString === 'function') {
            return n.toLocaleString('en-PK', { maximumFractionDigits: 2 });
        }
        return String(n);
    }

    // ── pending purchase filtering (DOM glue) ──────────────────────────────

    /**
     * Wire the pop-up's four controls to its table.  Returns an object with
     * ``apply()`` so a test can drive it, or null when the pop-up is absent.
     */
    function bindPendingPopup(doc) {
        var body = doc.getElementById('pendingPOBody');
        if (!body) return null;
        var siteSel = doc.getElementById('pendingSiteFilter');
        var materialSel = doc.getElementById('pendingMaterialFilter');
        var supplierSel = doc.getElementById('pendingSupplierFilter');
        var searchBox = doc.getElementById('pendingSearch');
        var noMatch = doc.getElementById('pendingPONoMatch');
        var summary = doc.getElementById('pendingPOSummary');
        var resetBtn = doc.getElementById('pendingResetFilters');

        function elements() {
            return body.querySelectorAll('tr[data-po-id]');
        }
        function criteria() {
            return {
                siteId: siteSel ? siteSel.value : '',
                materialId: materialSel ? materialSel.value : '',
                supplierId: supplierSel ? supplierSel.value : '',
                term: searchBox ? searchBox.value : ''
            };
        }
        function apply() {
            var els = elements();
            var rows = [];
            Array.prototype.forEach.call(els, function (el) {
                rows.push(pendingRowFromElement(el));
            });
            var result = summarisePending(rows, criteria());
            result.visible.forEach(function (row) { row.element.style.display = ''; });
            rows.forEach(function (row) {
                if (result.visible.indexOf(row) === -1) row.element.style.display = 'none';
            });
            if (noMatch) {
                noMatch.style.display = (result.shown || !rows.length) ? 'none' : '';
            }
            if (summary) {
                summary.textContent = result.shown + ' pending purchase order(s) \u2022 '
                    + formatQty(result.pendingQty) + ' qty still to deliver'
                    + (criteria().siteId ? ' \u2022 filtered to one site' : ' \u2022 all sites');
            }
            return result;
        }
        [siteSel, materialSel, supplierSel].forEach(function (el) {
            if (el) el.addEventListener('change', apply);
        });
        if (searchBox) searchBox.addEventListener('input', apply);
        if (resetBtn) {
            resetBtn.addEventListener('click', function () {
                [siteSel, materialSel, supplierSel].forEach(function (el) {
                    if (el) el.value = '';
                });
                if (searchBox) searchBox.value = '';
                apply();
            });
        }
        apply();
        return { apply: apply, criteria: criteria };
    }

    // ── All Sites / All Stages quick buttons ───────────────────────────────

    /**
     * Bind the two "All …" buttons of one filter bar.  Each clears its own
     * select and re-runs the search, leaving every other filter alone.
     */
    function bindAllScopeButtons(doc, ids) {
        var form = doc.getElementById(ids.form);
        var projectSel = doc.getElementById(ids.project);
        var stageSel = doc.getElementById(ids.stage);
        if (!form) return null;

        function narrow() {
            if (!stageSel) return;
            var pid = projectSel ? projectSel.value : '';
            Array.prototype.forEach.call(stageSel.options, function (opt) {
                if (!opt.value) return;
                var ok = !pid || opt.getAttribute('data-project') === pid;
                opt.style.display = ok ? '' : 'none';
                if (!ok && opt.selected) stageSel.value = '';
            });
        }
        function submitForm() {
            if (typeof form.requestSubmit === 'function') form.requestSubmit();
            else form.submit();
        }
        function clear(selectEl) {
            if (selectEl) selectEl.value = '';
            narrow();
            submitForm();
        }
        var allSites = doc.getElementById(ids.allSites);
        var allStages = doc.getElementById(ids.allStages);
        if (allSites) allSites.addEventListener('click', function () { clear(projectSel); });
        if (allStages) allStages.addEventListener('click', function () { clear(stageSel); });
        if (projectSel) projectSel.addEventListener('change', narrow);
        narrow();
        return { clearAllSites: function () { clear(projectSel); },
                 clearAllStages: function () { clear(stageSel); } };
    }

    var api = {
        pendingRowFromElement: pendingRowFromElement,
        pendingMatches: pendingMatches,
        summarisePending: summarisePending,
        bindPendingPopup: bindPendingPopup,
        bindAllScopeButtons: bindAllScopeButtons,
        formatQty: formatQty
    };
    root.hdcPurchaseDelivery = api;
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : this);
