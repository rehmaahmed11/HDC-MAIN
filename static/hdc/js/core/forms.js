/* HDC ERP — core/forms.js: Alert auto-dismiss + double-submit guard (split from hdc.js; loaded by base.html) */
(function () {
    'use strict';

    // ── AUTO-DISMISS ALERTS ──
    setTimeout(function () {
        var alerts = document.querySelectorAll('.alert.alert-success, .alert.alert-info');
        alerts.forEach(function (el) {
            var bsAlert = new bootstrap.Alert(el);
            bsAlert.close();
        });
    }, 4000);

    // Prevent double submit (double-click / slow network retries)
    document.addEventListener('submit', function (e) {
        var form = e.target;
        if (!form || form.tagName !== 'FORM') return;
        // A page that posts in the background and reports the result itself
        // (the tool transaction page) keeps its own lock and must not be
        // disabled here: a refused entry has to be correctable at once.
        if (form.hasAttribute('data-own-submit')) return;
        if (form.dataset.submitted === '1') {
            e.preventDefault();
            return;
        }
        form.dataset.submitted = '1';
        var submitters = form.querySelectorAll('button[type="submit"], input[type="submit"]');
        submitters.forEach(function (btn) { btn.disabled = true; });
        setTimeout(function () {
            form.dataset.submitted = '0';
            submitters.forEach(function (btn) { btn.disabled = false; });
        }, 8000);
    }, true);
})();
