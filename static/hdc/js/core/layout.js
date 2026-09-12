/* HDC ERP — core/layout.js: Sidebar collapse + persistence (split from hdc.js; loaded by base.html) */
(function () {
    'use strict';

    // ── SIDEBAR TOGGLE ──
    var sidebar = document.getElementById('sidebar');
    var toggleBtn = document.getElementById('sidebarToggle');
    if (toggleBtn && sidebar) {
        toggleBtn.addEventListener('click', function () {
            sidebar.classList.toggle('collapsed');
            localStorage.setItem('hdc_sidebar', sidebar.classList.contains('collapsed') ? '1' : '0');
        });
        // Restore saved state (only on wider screens)
        if (window.innerWidth > 768) {
            var saved = localStorage.getItem('hdc_sidebar');
            if (saved === '1') sidebar.classList.add('collapsed');
        } else {
            sidebar.classList.add('collapsed');
        }
    }
})();
