/* HDC ERP — core/layout.js
   Off-canvas sidebar behaviour (loaded by shared/base.html):

     • the sidebar starts collapsed on every page load;
     • the hamburger button (or the ✕ inside the drawer) expands/collapses it;
     • picking any option in the sidebar collapses it again and opens that page;
     • clicking the veil behind the drawer or pressing Escape also collapses it.
*/
(function () {
    'use strict';

    var sidebar = document.getElementById('sidebar');
    if (!sidebar) return;

    var toggleBtn = document.getElementById('sidebarToggle');
    var toggleIcon = document.getElementById('sidebarToggleIcon');
    var closeBtn = document.getElementById('sidebarClose');
    var backdrop = document.getElementById('sidebarBackdrop');

    // Below this breakpoint the page behind the drawer is scroll-locked.
    var MOBILE_QUERY = '(max-width: 992px)';
    var mobileMq = window.matchMedia ? window.matchMedia(MOBILE_QUERY) : null;

    function isMobile() {
        return mobileMq ? mobileMq.matches : window.innerWidth <= 992;
    }

    function isOpen() {
        return !sidebar.classList.contains('collapsed');
    }

    function setState(open) {
        sidebar.classList.toggle('collapsed', !open);
        sidebar.setAttribute('aria-hidden', open ? 'false' : 'true');
        document.body.classList.toggle('sidebar-open', open);
        document.body.classList.toggle('sidebar-scroll-lock', open && isMobile());

        if (backdrop) {
            backdrop.classList.toggle('show', open);
            backdrop.setAttribute('aria-hidden', open ? 'false' : 'true');
        }
        if (toggleBtn) {
            toggleBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
            toggleBtn.setAttribute('title', open ? 'Close menu' : 'Open menu');
        }
        if (toggleIcon) {
            toggleIcon.classList.toggle('fa-bars', !open);
            toggleIcon.classList.toggle('fa-xmark', open);
        }
    }

    function open() { setState(true); }
    function close() { setState(false); }
    function toggle() { isOpen() ? close() : open(); }

    // Always start collapsed — the drawer only opens on demand.
    close();

    if (toggleBtn) toggleBtn.addEventListener('click', toggle);
    if (closeBtn) closeBtn.addEventListener('click', close);
    if (backdrop) backdrop.addEventListener('click', close);

    document.addEventListener('keydown', function (e) {
        if ((e.key === 'Escape' || e.key === 'Esc') && isOpen()) {
            close();
            if (toggleBtn) toggleBtn.focus();
        }
    });

    /* Selecting an option: collapse the sidebar, then let the browser navigate. */
    sidebar.addEventListener('click', function (e) {
        var target = e.target;
        if (!target || !target.closest) return;
        var item = target.closest('.sidebar-nav a, .sidebar-nav button[type="submit"]');
        if (!item) return;

        // Leave new-tab / modified clicks alone — the user stays on this page.
        if (item.tagName === 'A') {
            var newTab = (item.getAttribute('target') || '').toLowerCase() === '_blank';
            if (newTab || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
        }
        if (e.defaultPrevented) return;

        close();
    });

    // Crossing the mobile breakpoint only changes the scroll lock, not the state.
    if (mobileMq) {
        var onBreakpoint = function () { setState(isOpen()); };
        if (mobileMq.addEventListener) mobileMq.addEventListener('change', onBreakpoint);
        else if (mobileMq.addListener) mobileMq.addListener(onBreakpoint);
    }

    // Handy hook for page scripts and the browser console.
    window.HDCSidebar = { open: open, close: close, toggle: toggle, isOpen: isOpen };
})();
