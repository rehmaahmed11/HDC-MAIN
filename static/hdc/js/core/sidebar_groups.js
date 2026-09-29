/* HDC ERP — core/sidebar_groups.js
   Collapsible groups in the main navigation (loaded by shared/base.html).

   The markup renders every group with its own heading button and a
   Bootstrap-collapse list underneath; this script decides which groups are
   open when the page loads and remembers the choice:

     • the group holding the page being viewed is always opened — the drawer
       must never hide where the user is;
     • every other group starts from the browser's saved choice
       (localStorage "hdc_sidebar_sections"), falling back to "open" on a wide
       screen and "closed" on a phone/tablet, where the drawer is small;
     • every later change — a heading click, "Expand / Collapse all sections",
       or the group closing on its own — is written straight back, so the
       drawer reopens the way the user left it.

   Only the `show` class on the group's <ul> is touched here: Bootstrap runs
   the animation, and core/collapse_groups.js keeps the arrow, aria-expanded
   and the footer button label in sync with it.
*/
(function () {
    'use strict';

    var STORAGE_KEY = 'hdc_sidebar_sections';
    var sidebar = document.getElementById('sidebar');
    if (!sidebar) return;

    var groups = Array.prototype.slice.call(
        sidebar.querySelectorAll('[data-hdc-nav-section]'));
    if (!groups.length) return;

    var narrow = window.matchMedia
        ? window.matchMedia('(max-width: 992px)')
        : null;

    function readPrefs() {
        try {
            var parsed = JSON.parse(window.localStorage.getItem(STORAGE_KEY) || 'null');
            return (parsed && typeof parsed === 'object') ? parsed : {};
        } catch (error) {
            return {};                 // private mode / corrupt entry — start clean
        }
    }

    function writePrefs() {
        try {
            window.localStorage.setItem(STORAGE_KEY, JSON.stringify(prefs));
        } catch (error) {
            /* storage unavailable: the state simply does not outlive the page */
        }
    }

    function idOf(group) {
        return group.getAttribute('data-hdc-nav-section') || '';
    }

    function listOf(group) {
        return group.querySelector('.sidebar-subnav');
    }

    function isOpen(group) {
        var list = listOf(group);
        return !!(list && list.classList.contains('show'));
    }

    function setOpen(group, open, animate) {
        var list = listOf(group);
        if (!list) return;
        var toggle = group.querySelector('.nav-group-toggle');

        if (animate && window.bootstrap && window.bootstrap.Collapse) {
            window.bootstrap.Collapse.getOrCreateInstance(list, { toggle: false })[
                open ? 'show' : 'hide']();
        } else {
            // No animation on the first paint — the drawer is off-canvas anyway,
            // and a snap avoids a visible slide when it opens.
            list.classList.toggle('show', open);
            list.classList.remove('collapsing');
            list.style.height = '';
        }
        if (toggle) toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
        group.classList.toggle('is-section-closed', !open);
    }

    var prefs = readPrefs();
    var compactScreen = !!(narrow && narrow.matches);

    // ── initial state ────────────────────────────────────────────────────
    groups.forEach(function (group) {
        var id = idOf(group);
        var open;
        if (group.classList.contains('has-current')) {
            open = true;                                // never hide the current page
        } else if (Object.prototype.hasOwnProperty.call(prefs, id)) {
            open = !!prefs[id];
        } else {
            open = !compactScreen;
        }
        setOpen(group, open, false);
    });

    // ── remember every later change ──────────────────────────────────────
    groups.forEach(function (group) {
        var list = listOf(group);
        var id = idOf(group);
        if (!list || !id || typeof window.MutationObserver === 'undefined') return;

        new window.MutationObserver(function () {
            if (list.classList.contains('collapsing')) return;   // mid-animation
            var open = list.classList.contains('show');
            group.classList.toggle('is-section-closed', !open);
            if (prefs[id] !== open) {
                prefs[id] = open;
                writePrefs();
            }
        }).observe(list, { attributes: true, attributeFilter: ['class'] });
    });

    // The footer button labels are rendered from the DOM state by
    // core/collapse_groups.js; let it re-read the state we just applied.
    window.setTimeout(function () {
        document.dispatchEvent(new Event('shown.bs.collapse'));
    }, 0);

    // Expanding a group low in a long drawer would otherwise reveal its links
    // below the fold — nudge the last one into view once the slide is done.
    sidebar.addEventListener('click', function (event) {
        var target = event.target;
        if (!target || !target.closest) return;
        var toggle = target.closest('.nav-group-toggle');
        if (!toggle) return;
        var group = toggle.closest('[data-hdc-nav-section]');
        if (!group || isOpen(group)) return;
        window.setTimeout(function () {
            var list = listOf(group);
            var last = list && list.lastElementChild;
            if (last && last.scrollIntoView) last.scrollIntoView({ block: 'nearest' });
        }, 320);
    });

    // Expose the state for page scripts and the console (mirrors HDCSidebar).
    window.HDCSidebarSections = {
        isOpen: function (id) {
            for (var i = 0; i < groups.length; i += 1) {
                if (idOf(groups[i]) === id) return isOpen(groups[i]);
            }
            return null;
        },
        openAll: function () { groups.forEach(function (g) { setOpen(g, true, true); }); },
        closeAll: function () { groups.forEach(function (g) { setOpen(g, false, true); }); },
        clearSaved: function () {
            prefs = {};
            writePrefs();
        }
    };
})();
