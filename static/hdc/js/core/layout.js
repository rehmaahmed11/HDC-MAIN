/* HDC ERP — core/layout.js
   Adaptive sidebar behaviour (loaded by shared/base.html):

     • on wide desktops (≥ 1200px), the sidebar supports a docked/pinned mode
       (`body.sidebar-docked`, remembered in localStorage `hdc_sidebar_pinned`
       and defaulting to pinned so cross-module navigation is 1 click);
     • clicking the pin button (`#sidebarPin`) or hamburger on wide desktop
       switches between docked and overlay/collapsed according to operator
       preference;
     • below 1200px (or when unpinned), the sidebar acts as an off-canvas
       overlay drawer: hidden by default, toggled by the hamburger, and closed
       by picking a link, clicking the backdrop, or pressing Escape.
*/
(function () {
    'use strict';

    var STORAGE_PIN_KEY = 'hdc_sidebar_pinned';
    var sidebar = document.getElementById('sidebar');
    if (!sidebar) return;

    var toggleBtn = document.getElementById('sidebarToggle');
    var toggleIcon = document.getElementById('sidebarToggleIcon');
    var closeBtn = document.getElementById('sidebarClose');
    var pinBtn = document.getElementById('sidebarPin');
    var backdrop = document.getElementById('sidebarBackdrop');

    var MOBILE_QUERY = '(max-width: 992px)';
    var DOCK_QUERY = '(min-width: 1200px)';
    var mobileMq = window.matchMedia ? window.matchMedia(MOBILE_QUERY) : null;
    var dockMq = window.matchMedia ? window.matchMedia(DOCK_QUERY) : null;

    function isMobile() {
        return mobileMq ? mobileMq.matches : window.innerWidth <= 992;
    }

    function canDock() {
        return dockMq ? dockMq.matches : window.innerWidth >= 1200;
    }

    function readPinnedPref() {
        try {
            var raw = window.localStorage.getItem(STORAGE_PIN_KEY);
            if (raw === '0' || raw === 'false') return false;
            if (raw === '1' || raw === 'true') return true;
        } catch (err) {
            /* storage unavailable */
        }
        return true; // Default to 1-click docked navigation on wide desktop
    }

    function writePinnedPref(val) {
        try {
            window.localStorage.setItem(STORAGE_PIN_KEY, val ? '1' : '0');
        } catch (err) {
            /* storage unavailable */
        }
    }

    var pinnedPref = readPinnedPref();

    function isDocked() {
        return canDock() && pinnedPref;
    }

    function isOpen() {
        return isDocked() || !sidebar.classList.contains('collapsed');
    }

    function syncPinButton() {
        if (!pinBtn) return;
        var active = isDocked();
        pinBtn.setAttribute('aria-pressed', active ? 'true' : 'false');
        pinBtn.setAttribute('title', active ? 'Unpin sidebar (auto-hide)' : 'Pin sidebar open');
        pinBtn.setAttribute('aria-label', active ? 'Unpin sidebar' : 'Pin sidebar open');
    }

    function setState(overlayOpen) {
        var docked = isDocked();
        var visible = docked || !!overlayOpen;

        document.body.classList.toggle('sidebar-docked', docked);
        sidebar.classList.toggle('collapsed', !visible);
        sidebar.setAttribute('aria-hidden', visible ? 'false' : 'true');
        document.body.classList.toggle('sidebar-open', visible && !docked);
        document.body.classList.toggle('sidebar-scroll-lock', visible && !docked && isMobile());

        if (backdrop) {
            backdrop.classList.toggle('show', visible && !docked);
            backdrop.setAttribute('aria-hidden', (visible && !docked) ? 'false' : 'true');
        }
        if (toggleBtn) {
            toggleBtn.setAttribute('aria-expanded', visible ? 'true' : 'false');
            toggleBtn.setAttribute('title', visible ? 'Hide menu' : 'Open menu');
        }
        if (toggleIcon) {
            toggleIcon.classList.toggle('fa-bars', !visible || docked);
            toggleIcon.classList.toggle('fa-xmark', visible && !docked);
        }
        syncPinButton();
    }

    function open() {
        setState(true);
    }

    function close() {
        if (isDocked()) {
            pinnedPref = false;
            writePinnedPref(false);
        }
        setState(false);
    }

    function setPinned(nextPinned) {
        pinnedPref = !!nextPinned;
        writePinnedPref(pinnedPref);
        setState(pinnedPref);
    }

    function toggle() {
        if (canDock()) {
            if (isDocked()) {
                setPinned(false);
            } else if (!sidebar.classList.contains('collapsed')) {
                setState(false);
            } else {
                setPinned(true);
            }
            return;
        }
        isOpen() ? setState(false) : setState(true);
    }

    // Apply initial state (docked on ≥1200px if pinned, collapsed overlay otherwise).
    setState(false);

    if (toggleBtn) toggleBtn.addEventListener('click', toggle);
    if (closeBtn) closeBtn.addEventListener('click', function () { setState(false); });
    if (pinBtn) {
        pinBtn.addEventListener('click', function () {
            setPinned(!pinnedPref);
        });
    }
    if (backdrop) backdrop.addEventListener('click', function () { setState(false); });

    document.addEventListener('keydown', function (e) {
        if ((e.key === 'Escape' || e.key === 'Esc') && !isDocked() && isOpen()) {
            setState(false);
            if (toggleBtn) toggleBtn.focus();
        }
    });

    /* Selecting an option: collapse the overlay drawer on narrow/unpinned views,
       or stay docked on wide desktop. */
    sidebar.addEventListener('click', function (e) {
        var target = e.target;
        if (!target || !target.closest) return;
        var item = target.closest('.sidebar-nav a, .sidebar-nav button[type="submit"]');
        if (!item) return;

        if (item.tagName === 'A') {
            var newTab = (item.getAttribute('target') || '').toLowerCase() === '_blank';
            if (newTab || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
        }
        if (e.defaultPrevented) return;

        if (!isDocked()) setState(false);
    });

    var onMediaChange = function () {
        setState(!sidebar.classList.contains('collapsed') && !isDocked());
    };
    if (mobileMq) {
        if (mobileMq.addEventListener) mobileMq.addEventListener('change', onMediaChange);
        else if (mobileMq.addListener) mobileMq.addListener(onMediaChange);
    }
    if (dockMq) {
        if (dockMq.addEventListener) dockMq.addEventListener('change', onMediaChange);
        else if (dockMq.addListener) dockMq.addListener(onMediaChange);
    }

    window.HDCSidebar = {
        open: open,
        close: close,
        toggle: toggle,
        isOpen: isOpen,
        isPinned: function () { return pinnedPref; },
        setPinned: setPinned
    };
})();
