/* HDC ERP — coordinated expand/collapse controls for repeated Bootstrap panels.

   Add data-hdc-collapse-group=".some-collapse-target" to a button to make it
   expand all matching panels, or collapse them once they are all open. The
   button label and icon update as panels are opened individually as well.
*/
(function () {
    'use strict';

    function targetsFor(button) {
        var selector = button.getAttribute('data-hdc-collapse-group');
        if (!selector) return [];
        try {
            return Array.prototype.slice.call(document.querySelectorAll(selector));
        } catch (error) {
            return [];
        }
    }

    function syncButton(button) {
        var targets = targetsFor(button);
        var openCount = targets.filter(function (target) {
            return target.classList.contains('show');
        }).length;
        var allOpen = targets.length > 0 && openCount === targets.length;
        var label = button.querySelector('[data-hdc-collapse-label]');
        var icon = button.querySelector('[data-hdc-collapse-icon]');

        button.disabled = targets.length === 0;
        button.setAttribute('aria-expanded', allOpen ? 'true' : 'false');
        button.setAttribute('aria-label', allOpen ? 'Collapse all sections' : 'Expand all sections');
        button.title = allOpen ? 'Close every section' : 'Open every section';
        if (label) label.textContent = allOpen
            ? (button.getAttribute('data-collapse-label') || 'Collapse all')
            : (button.getAttribute('data-expand-label') || 'Expand all');
        if (icon) {
            icon.classList.toggle('fa-angles-up', allOpen);
            icon.classList.toggle('fa-angles-down', !allOpen);
        }
    }

    function syncCollapseTriggers() {
        document.querySelectorAll('[data-bs-toggle="collapse"]').forEach(function (trigger) {
            var selector = trigger.getAttribute('data-bs-target') || trigger.getAttribute('href') || '';
            if (selector.charAt(0) !== '#') return;
            var target = document.getElementById(selector.slice(1));
            if (!target) return;
            var expanded = target.classList.contains('show');
            trigger.setAttribute('aria-expanded', expanded ? 'true' : 'false');
            trigger.classList.toggle('collapsed', !expanded);
        });
    }

    function syncAllButtons() {
        document.querySelectorAll('[data-hdc-collapse-group]').forEach(syncButton);
        syncCollapseTriggers();
    }

    document.addEventListener('DOMContentLoaded', function () {
        document.querySelectorAll('[data-hdc-collapse-group]').forEach(function (button) {
            button.addEventListener('click', function () {
                var targets = targetsFor(button);
                if (!targets.length) return;
                var shouldExpand = targets.some(function (target) {
                    return !target.classList.contains('show');
                });

                targets.forEach(function (target) {
                    if (window.bootstrap && window.bootstrap.Collapse) {
                        window.bootstrap.Collapse.getOrCreateInstance(target, { toggle: false })[
                            shouldExpand ? 'show' : 'hide'
                        ]();
                    } else {
                        target.classList.toggle('show', shouldExpand);
                    }
                });
                // Bootstrap transition events will sync again on completion.
                syncAllButtons();
            });
        });

        document.addEventListener('shown.bs.collapse', syncAllButtons);
        document.addEventListener('hidden.bs.collapse', syncAllButtons);
        syncAllButtons();
    });
})();
