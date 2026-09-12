/* HDC ERP — core/theme.js: Dark/light theme toggle (split from hdc.js; loaded by base.html) */
(function () {
    'use strict';

    // ── THEME TOGGLE ──
    var themeBtn = document.getElementById('themeToggle');
    var themeIcon = document.getElementById('themeIcon');
    function applyTheme(t) {
        document.documentElement.setAttribute('data-theme', t);
        localStorage.setItem('hdc_theme', t);
        if (themeIcon) {
            themeIcon.className = t === 'dark' ? 'fas fa-sun' : 'fas fa-moon';
        }
    }
    var currentTheme = localStorage.getItem('hdc_theme') || 'light';
    applyTheme(currentTheme);
    if (themeBtn) {
        themeBtn.addEventListener('click', function () {
            var t = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
            applyTheme(t);
        });
    }
})();
