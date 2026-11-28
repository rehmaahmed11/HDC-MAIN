/* HDC ERP — core/quick_jump.js
   Global Quick-Jump command palette (Ctrl+K / Cmd+K / `/`).

   Indexes every navigation link and section tab rendered on the current page,
   so it works with zero network latency and automatically honours all role and
   page-level permission gates from the server.
*/
(function () {
    'use strict';

    var trigger = document.getElementById('quickJumpBtn');
    if (!document.body) return;

    var modalEl = null;
    var bsModal = null;
    var inputEl = null;
    var listEl = null;
    var items = [];
    var filtered = [];
    var activeIndex = 0;

    function isEditable(el) {
        if (!el || !el.tagName) return false;
        var tag = el.tagName.toUpperCase();
        if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return true;
        return !!el.isContentEditable;
    }

    function collectDestinations() {
        var seen = Object.create(null);
        var collected = [];

        function add(label, href, group, icon) {
            label = String(label || '').replace(/\s+/g, ' ').trim();
            if (!label || !href || href.charAt(0) === '#') return;
            var key = href + '::' + label.toLowerCase();
            if (seen[key]) return;
            seen[key] = true;
            collected.push({
                label: label,
                href: href,
                group: group || 'Navigation',
                icon: icon || 'fa-arrow-right',
                haystack: (label + ' ' + (group || '') + ' ' + href).toLowerCase()
            });
        }

        // 1. Current section subnav tabs (fastest sibling jumps)
        var subLinks = document.querySelectorAll('.tools-subnav-link, .se-tab');
        Array.prototype.forEach.call(subLinks, function (link) {
            if (link.hidden) return;
            var iconEl = link.querySelector('i');
            var icon = 'fa-compass';
            if (iconEl && iconEl.className) {
                var m = iconEl.className.match(/\bfa-[a-z0-9-]+/);
                if (m) icon = m[0];
            }
            add(link.textContent, link.getAttribute('href'), 'This Section', icon);
        });

        // 2. Main sidebar groups
        var groups = document.querySelectorAll('#sidebarGroups [data-hdc-nav-section]');
        Array.prototype.forEach.call(groups, function (groupEl) {
            var groupLabelEl = groupEl.querySelector('.nav-group-label');
            var groupName = groupLabelEl ? groupLabelEl.textContent.trim() : 'Menu';
            var links = groupEl.querySelectorAll('.sidebar-subnav .nav-item a');
            Array.prototype.forEach.call(links, function (link) {
                if (link.hidden) return;
                var span = link.querySelector('span');
                var label = span ? span.textContent : link.textContent;
                var iconEl = link.querySelector('i');
                var icon = 'fa-folder-open';
                if (iconEl && iconEl.className) {
                    var m = iconEl.className.match(/\bfa-[a-z0-9-]+/);
                    if (m) icon = m[0];
                }
                add(label, link.getAttribute('href'), groupName, icon);
            });
        });

        return collected;
    }

    function ensureModal() {
        if (modalEl && document.body.contains(modalEl)) return;
        modalEl = document.createElement('div');
        modalEl.className = 'modal fade hdc-qj-modal';
        modalEl.id = 'hdcQuickJumpModal';
        modalEl.setAttribute('tabindex', '-1');
        modalEl.setAttribute('aria-label', 'Quick jump to page');

        var dialog = document.createElement('div');
        dialog.className = 'modal-dialog';
        var content = document.createElement('div');
        content.className = 'modal-content hdc-modal';

        var searchWrap = document.createElement('div');
        searchWrap.className = 'hdc-qj-search-wrap';
        var searchIcon = document.createElement('i');
        searchIcon.className = 'fas fa-magnifying-glass';
        searchIcon.setAttribute('aria-hidden', 'true');
        inputEl = document.createElement('input');
        inputEl.type = 'search';
        inputEl.className = 'hdc-qj-input';
        inputEl.placeholder = 'Jump to any module, ledger, or page…';
        inputEl.setAttribute('aria-label', 'Search pages');
        inputEl.autocomplete = 'off';
        var closeBtn = document.createElement('button');
        closeBtn.type = 'button';
        closeBtn.className = 'btn-close';
        closeBtn.setAttribute('data-bs-dismiss', 'modal');
        closeBtn.setAttribute('aria-label', 'Close');
        searchWrap.appendChild(searchIcon);
        searchWrap.appendChild(inputEl);
        searchWrap.appendChild(closeBtn);

        listEl = document.createElement('ul');
        listEl.className = 'hdc-qj-list';
        listEl.setAttribute('role', 'listbox');

        var footer = document.createElement('div');
        footer.className = 'hdc-qj-footer';
        var hintNav = document.createElement('span');
        hintNav.textContent = '↑↓ Navigate · Enter Open';
        var hintEsc = document.createElement('span');
        hintEsc.textContent = 'Esc Close';
        footer.appendChild(hintNav);
        footer.appendChild(hintEsc);

        content.appendChild(searchWrap);
        content.appendChild(listEl);
        content.appendChild(footer);
        dialog.appendChild(content);
        modalEl.appendChild(dialog);
        document.body.appendChild(modalEl);

        inputEl.addEventListener('input', function () {
            activeIndex = 0;
            renderList();
        });

        inputEl.addEventListener('keydown', function (e) {
            if (e.key === 'ArrowDown') {
                e.preventDefault();
                if (filtered.length) {
                    activeIndex = (activeIndex + 1) % filtered.length;
                    syncActive();
                }
            } else if (e.key === 'ArrowUp') {
                e.preventDefault();
                if (filtered.length) {
                    activeIndex = (activeIndex - 1 + filtered.length) % filtered.length;
                    syncActive();
                }
            } else if (e.key === 'Enter') {
                var target = filtered[activeIndex];
                if (target && target.href) {
                    e.preventDefault();
                    window.location.assign(target.href);
                }
            }
        });

        modalEl.addEventListener('shown.bs.modal', function () {
            if (inputEl) {
                inputEl.focus();
                inputEl.select();
            }
        });
    }

    function syncActive() {
        if (!listEl) return;
        var rows = listEl.querySelectorAll('.hdc-qj-item');
        Array.prototype.forEach.call(rows, function (row, idx) {
            var active = idx === activeIndex;
            row.classList.toggle('is-active', active);
            if (active && row.scrollIntoView) row.scrollIntoView({ block: 'nearest' });
        });
    }

    function renderList() {
        if (!listEl) return;
        while (listEl.firstChild) listEl.removeChild(listEl.firstChild);
        var q = (inputEl && inputEl.value ? inputEl.value : '').toLowerCase().trim();
        var terms = q ? q.split(/\s+/) : [];

        filtered = items.filter(function (it) {
            return terms.every(function (t) { return it.haystack.indexOf(t) !== -1; });
        });

        if (!filtered.length) {
            var empty = document.createElement('li');
            empty.className = 'px-3 py-3 text-center text-muted small';
            empty.textContent = 'No matching pages found.';
            listEl.appendChild(empty);
            return;
        }

        if (activeIndex >= filtered.length) activeIndex = 0;

        filtered.forEach(function (it, idx) {
            var li = document.createElement('li');
            li.className = 'hdc-qj-item' + (idx === activeIndex ? ' is-active' : '');
            li.setAttribute('role', 'option');
            var a = document.createElement('a');
            a.href = it.href;
            var ic = document.createElement('i');
            ic.className = 'fas ' + it.icon;
            ic.setAttribute('aria-hidden', 'true');
            var title = document.createElement('span');
            title.textContent = it.label;
            var grp = document.createElement('span');
            grp.className = 'hdc-qj-item-group';
            grp.textContent = it.group;
            a.appendChild(ic);
            a.appendChild(title);
            a.appendChild(grp);
            li.appendChild(a);
            li.addEventListener('mouseenter', function () {
                activeIndex = idx;
                syncActive();
            });
            listEl.appendChild(li);
        });
    }

    function openPalette() {
        if (typeof window.bootstrap === 'undefined' || !window.bootstrap.Modal) return;
        ensureModal();
        items = collectDestinations();
        activeIndex = 0;
        if (inputEl) inputEl.value = '';
        renderList();
        if (!bsModal) bsModal = new window.bootstrap.Modal(modalEl);
        bsModal.show();
    }

    if (trigger) {
        trigger.addEventListener('click', openPalette);
    }

    document.addEventListener('keydown', function (e) {
        if ((e.ctrlKey || e.metaKey) && String(e.key || '').toLowerCase() === 'k') {
            e.preventDefault();
            openPalette();
            return;
        }
        if (e.key === '/' && !e.ctrlKey && !e.metaKey && !e.altKey && !isEditable(e.target)) {
            if (document.querySelector('.modal.show')) return;
            e.preventDefault();
            openPalette();
        }
    });

    window.HDCQuickJump = { open: openPalette };
})();
