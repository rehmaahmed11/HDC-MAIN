/* HDC ERP — core/combo.js: HDCComboList searchable combo widget (split from hdc.js; loaded by base.html) */
(function () {
    'use strict';

    // Shared searchable combo-list for input + select pairs
    if (!window.HDCComboList) {
        var styleId = 'hdc-combo-list-style';
        if (!document.getElementById(styleId)) {
            var style = document.createElement('style');
            style.id = styleId;
            // NOTE: Menu is rendered with position:fixed and appended to <body>
            // so it can never be clipped by a parent's overflow:hidden (cards,
            // table-responsive, modals, etc).
            //
            // Every colour comes from the design tokens in hdc.css so the menu
            // follows the light/dark theme like every other surface — no
            // hardcoded hex that would stay cream in dark mode.
            style.textContent = [
                '.hdc-combo-wrap{position:relative;overflow:visible;}',
                '.hdc-combo-menu{position:fixed;z-index:10500;min-width:160px;max-width:min(640px, calc(100vw - 24px));max-height:320px;overflow:auto;',
                'border:1px solid var(--card-border);border-radius:10px;background:var(--card-bg);color:var(--text);',
                'box-shadow:var(--shadow-card);-webkit-backdrop-filter:blur(8px);backdrop-filter:blur(8px);}',
                '.hdc-combo-item{padding:8px 11px;cursor:pointer;font-size:.86rem;border-bottom:1px solid var(--card-border);',
                'white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}',
                '.hdc-combo-item:last-child{border-bottom:0;}',
                '.hdc-combo-item:hover,.hdc-combo-item.active{background:var(--accent);color:#fff;}',
                // Shown only when the caller passes emptyText / onAdd, so pages
                // that do not opt in render exactly what they always did.
                '.hdc-combo-empty{padding:8px 11px;font-size:.82rem;color:var(--text-muted);}',
                '.hdc-combo-add{padding:8px 11px;cursor:pointer;font-size:.82rem;font-weight:700;',
                'color:var(--accent-strong);border-top:1px dashed var(--card-border);white-space:nowrap;}',
                '.hdc-combo-add:hover,.hdc-combo-add.active{background:var(--accent);color:#fff;}',
                // Keyboard focus ring for the visible input, so a combo is as
                // reachable as the select it replaced.
                'input[data-hdc-combo]:focus-visible{outline:2px solid var(--accent);outline-offset:1px;}'
            ].join('');
            document.head.appendChild(style);
        }

        window.HDCComboList = {
            attach: function (inputRef, selectRef, options) {
                var opts = options || {};
                var input = typeof inputRef === 'string' ? document.getElementById(inputRef) : inputRef;
                var select = typeof selectRef === 'string' ? document.getElementById(selectRef) : selectRef;
                if (!input || !select) return null;

                var wrap = input.parentElement;
                if (!wrap || !wrap.classList.contains('hdc-combo-wrap')) {
                    wrap = document.createElement('div');
                    wrap.className = 'hdc-combo-wrap';
                    input.parentNode.insertBefore(wrap, input);
                    wrap.appendChild(input);
                }
                // Append menu to <body> so no parent overflow:hidden can clip it.
                var menu = document.createElement('div');
                menu.className = 'hdc-combo-menu d-none';
                menu.setAttribute('role', 'listbox');
                if (!menu.id) menu.id = select.id ? select.id + '_menu' : '';
                if (menu.id) input.setAttribute('aria-controls', menu.id);
                document.body.appendChild(menu);

                if (opts.hideSelect !== false) select.classList.add('d-none');
                if (opts.syncInputWithSelected !== false) {
                    var sel = select.options[select.selectedIndex];
                    if (sel && sel.value) input.value = sel.text;
                }

                var state = { items: [], idx: -1 };
                var requiredEchoGuard = false;
                function selectedOption() {
                    return select.options[select.selectedIndex] || null;
                }
                function syncFromSelect(force) {
                    // While the user is typing in the input they own the text;
                    // it is reconciled on blur (strict) or by exact match.
                    if (!force && document.activeElement === input) return;
                    var opt = selectedOption();
                    input.value = (opt && opt.value) ? (opt.text || '') : '';
                }
                function syncRequired() {
                    if (opts.mirrorRequired === false) return;
                    var need = !!select.required;
                    input.required = need;
                    if (need) {
                        // The hidden select must never be the control the
                        // browser tries to focus for "required" validation -
                        // the visible input owns that contract now.
                        requiredEchoGuard = true;
                        select.required = false;
                    }
                }
                function placeMenu() {
                    if (menu.classList.contains('d-none')) return;
                    var rect = input.getBoundingClientRect();
                    var viewportH = window.innerHeight || document.documentElement.clientHeight || 0;
                    var viewportW = window.innerWidth || document.documentElement.clientWidth || 0;
                    var spaceBelow = Math.max(0, viewportH - rect.bottom - 10);
                    var spaceAbove = Math.max(0, rect.top - 10);
                    var openUp = spaceBelow < 200 && spaceAbove > spaceBelow;
                    var available = openUp ? spaceAbove : spaceBelow;
                    var safeHeight = Math.max(160, Math.min(360, available));

                    // Position with viewport-fixed coords.
                    var leftPx = Math.max(8, Math.min(rect.left, viewportW - 180));
                    var minWidth = Math.max(rect.width, 160);
                    menu.style.left = leftPx + 'px';
                    menu.style.minWidth = minWidth + 'px';
                    menu.style.maxWidth = Math.max(220, viewportW - leftPx - 12) + 'px';
                    if (openUp) {
                        menu.style.top = 'auto';
                        menu.style.bottom = (viewportH - rect.top + 4) + 'px';
                    } else {
                        menu.style.bottom = 'auto';
                        menu.style.top = (rect.bottom + 4) + 'px';
                    }
                    menu.style.maxHeight = safeHeight + 'px';
                }
                function getOptions() {
                    var all = Array.from(select.options).filter(function (o) {
                        if (!opts.includeEmpty && !o.value) return false;
                        if (o.hidden || (o.style && o.style.display === 'none')) return false;
                        return true;
                    });
                    var q = (input.value || '').toLowerCase().trim();
                    if (q) {
                        all = all.filter(function (o) {
                            return (o.text || '').toLowerCase().indexOf(q) !== -1;
                        });
                    }
                    return all.slice(0, opts.maxItems || 25);
                }
                // The "+ Add New …" affordance is opt-in: a combo only shows it
                // when the caller passes onAdd, so every existing caller keeps
                // exactly the list it had before.
                function hasAddRow() {
                    return typeof opts.onAdd === 'function';
                }
                function escapeHtml(text) {
                    return String(text == null ? '' : text)
                        .replace(/&/g, '&amp;').replace(/</g, '&lt;')
                        .replace(/>/g, '&gt;').replace(/\"/g, '&quot;');
                }
                function render() {
                    state.items = getOptions();
                    state.idx = state.items.length ? 0 : -1;
                    var rows = state.items.map(function (o, i) {
                        return '<div class=\"hdc-combo-item' + (i === 0 ? ' active' : '') +
                            '\" role=\"option\" data-idx=\"' + i + '\">' + escapeHtml(o.text) + '</div>';
                    });
                    if (!state.items.length) {
                        // No matches.  Rather than an empty box, point at the way
                        // out when the caller offered one.
                        if (opts.emptyText || hasAddRow()) {
                            var empty = '<div class=\"hdc-combo-empty\">' +
                                escapeHtml(opts.emptyText || 'No matches found.') + '</div>';
                            var addOnly = hasAddRow()
                                ? '<div class=\"hdc-combo-add\" role=\"option\" data-idx=\"' + state.items.length +
                                  '\">' + escapeHtml(opts.addLabel || '+ Add new') + '</div>'
                                : '';
                            menu.innerHTML = empty + addOnly;
                            state.idx = -1;
                            menu.classList.remove('d-none');
                            placeMenu();
                            return;
                        }
                        menu.innerHTML = '';
                        menu.classList.add('d-none');
                        return;
                    }
                    if (hasAddRow()) {
                        rows.push('<div class=\"hdc-combo-add\" role=\"option\" data-idx=\"' +
                            state.items.length + '\">' +
                            escapeHtml(opts.addLabel || '+ Add new') + '</div>');
                    }
                    menu.innerHTML = rows.join('');
                    menu.classList.remove('d-none');
                    placeMenu();
                }
                function highlight() {
                    Array.from(menu.querySelectorAll('.hdc-combo-item, .hdc-combo-add')).forEach(function (el) {
                        el.classList.toggle('active', parseInt(el.getAttribute('data-idx') || '-1', 10) === state.idx);
                    });
                }
                function maxIdx() {
                    return hasAddRow() ? state.items.length : state.items.length - 1;
                }
                function pick(i) {
                    if (hasAddRow() && i === state.items.length) {
                        // The action row is not a value: it never touches the
                        // select, so a cancelled modal leaves the field as-is.
                        menu.classList.add('d-none');
                        opts.onAdd(input, select);
                        return;
                    }
                    var opt = state.items[i];
                    if (!opt) return;
                    select.value = opt.value;
                    input.value = (opt.value || !opts.strict) ? opt.text : '';
                    select.dispatchEvent(new Event('change', { bubbles: true }));
                    menu.classList.add('d-none');
                }
                function syncByExact() {
                    var q = (input.value || '').trim().toLowerCase();
                    if (!q) {
                        if (opts.clearOnEmpty !== false) {
                            select.value = '';
                            if (opts.strict) select.dispatchEvent(new Event('change', { bubbles: true }));
                        }
                        return;
                    }
                    var exact = Array.from(select.options).find(function (o) {
                        return (o.text || '').trim().toLowerCase() === q;
                    });
                    if (exact) {
                        select.value = exact.value;
                        if (opts.strict) input.value = exact.text;
                        select.dispatchEvent(new Event('change', { bubbles: true }));
                    } else if (opts.strict) {
                        // Strict combos must always mirror the select: typed
                        // text that matches no option reverts to the current
                        // selection so the input can never claim a value the
                        // select does not hold.
                        syncFromSelect(true);
                    }
                }

                input.addEventListener('focus', render);
                input.addEventListener('input', function () {
                    if (opts.clearOnInput !== false) select.value = '';
                    render();
                });
                // Keep the visible label in sync when another page control
                // changes the hidden select (dependent pickers, modal
                // preselection, or a server-driven option refresh).
                select.addEventListener('change', function () { syncFromSelect(true); });
                window.addEventListener('resize', placeMenu);
                window.addEventListener('scroll', placeMenu, true);
                input.addEventListener('keydown', function (e) {
                    if (menu.classList.contains('d-none')) return;
                    if (e.key === 'ArrowDown') {
                        e.preventDefault();
                        state.idx = Math.min(state.idx + 1, maxIdx());
                        highlight();
                    } else if (e.key === 'ArrowUp') {
                        e.preventDefault();
                        state.idx = Math.max(state.idx - 1, 0);
                        highlight();
                    } else if (e.key === 'Enter') {
                        e.preventDefault();
                        if (state.idx >= 0) pick(state.idx);
                    } else if (e.key === 'Escape') {
                        menu.classList.add('d-none');
                    }
                });
                input.addEventListener('blur', function () {
                    setTimeout(function () {
                        syncByExact();
                        menu.classList.add('d-none');
                    }, 120);
                });
                menu.addEventListener('mousemove', function (e) {
                    var row = e.target.closest('.hdc-combo-item');
                    if (!row) return;
                    state.idx = parseInt(row.getAttribute('data-idx') || '-1', 10);
                    highlight();
                });
                menu.addEventListener('mousedown', function (e) {
                    var row = e.target.closest('.hdc-combo-item');
                    if (!row) return;
                    e.preventDefault();
                    pick(parseInt(row.getAttribute('data-idx') || '-1', 10));
                });

                // Page code rebuilds these selects as direction / type /
                // project change and rewrites option labels when balances
                // refresh; mirror every such change into the input text.
                if (window.MutationObserver) {
                    new MutationObserver(function () { syncFromSelect(false); })
                        .observe(select, { childList: true, subtree: true });
                    new MutationObserver(function () {
                        if (requiredEchoGuard) {
                            requiredEchoGuard = false;
                            return;
                        }
                        syncRequired();
                    }).observe(select, { attributes: true, attributeFilter: ['required'] });
                }
                syncRequired();

                return {
                    refresh: render,
                    syncFromSelect: function () { syncFromSelect(true); }
                };
            }
        };
    }

    // ── Auto-wire ────────────────────────────────────────────────────────
    // Any input carrying data-hdc-combo="<select id>" is wired on
    // DOMContentLoaded, so a template only has to render the pair — no
    // per-page script, and no way for the markup and the wiring to drift
    // apart. data-hdc-combo-strict selects closed-set records/keys;
    // name-valued inputs remain non-strict so a new name can be typed. Repeated fields
    // may set data-hdc-combo-manual and call attach() with page-specific opts.
    function wireCombos(root) {
        var nodes = (root || document).querySelectorAll('input[data-hdc-combo]');
        for (var i = 0; i < nodes.length; i++) {
            var input = nodes[i];
            // A page may need a manual strict attachment (for example a
            // repeated tool picker); it still uses the data attribute for the
            // no-JS fallback but owns its own attach options.
            if (input.__hdcCombo || input.getAttribute('data-hdc-combo-manual') !== null) continue;
            var selectId = input.getAttribute('data-hdc-combo');
            var select = selectId ? document.getElementById(selectId) : null;
            if (!select) continue;
            var opts = {
                // Name-valued combos accept a new typed name; combo_select()
                // inputs set data-hdc-combo-strict so only a real listed
                // option can supply the selected value.
                strict: input.getAttribute('data-hdc-combo-strict') === 'true',
                maxItems: parseInt(input.getAttribute('data-hdc-combo-max') || '50', 10),
                emptyText: input.getAttribute('data-hdc-combo-empty') ||
                           'No match — keep typing to use a new name.'
            };
            var handle = window.HDCComboList.attach(input, select, opts);
            if (handle) input.__hdcCombo = handle;
        }
    }

    if (typeof document !== 'undefined' && typeof document.querySelectorAll === 'function') {
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', function () { wireCombos(document); });
        } else {
            wireCombos(document);
        }
    }
})();
