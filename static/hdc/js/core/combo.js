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
            style.textContent = [
                '.hdc-combo-wrap{position:relative;overflow:visible;}',
                '.hdc-combo-menu{position:fixed;z-index:10500;min-width:160px;max-width:min(640px, calc(100vw - 24px));max-height:320px;overflow:auto;border:1px solid #f3b66d;border-radius:8px;background:#fff3e0;box-shadow:0 12px 28px rgba(0,0,0,.22);}',
                '[data-theme="dark"] .hdc-combo-menu{background:#2b2b2b;border-color:#5a5a5a;color:#f1f1f1;}',
                '.hdc-combo-item{padding:8px 10px;cursor:pointer;font-size:.9rem;border-bottom:1px solid #f7d2a5;white-space:nowrap;}',
                '[data-theme="dark"] .hdc-combo-item{border-bottom-color:#444;}',
                '.hdc-combo-item:last-child{border-bottom:0;}',
                '.hdc-combo-item:hover,.hdc-combo-item.active{background:#87af32;color:#fff;}'
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
                document.body.appendChild(menu);

                if (opts.hideSelect !== false) select.classList.add('d-none');
                if (opts.syncInputWithSelected !== false) {
                    var sel = select.options[select.selectedIndex];
                    if (sel && sel.value) input.value = sel.text;
                }

                var state = { items: [], idx: -1 };
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
                        if (o.style && o.style.display === 'none') return false;
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
                function render() {
                    state.items = getOptions();
                    state.idx = state.items.length ? 0 : -1;
                    if (!state.items.length) {
                        menu.innerHTML = '';
                        menu.classList.add('d-none');
                        return;
                    }
                    menu.innerHTML = state.items.map(function (o, i) {
                        return '<div class=\"hdc-combo-item' + (i === state.idx ? ' active' : '') + '\" data-idx=\"' + i + '\">' + o.text + '</div>';
                    }).join('');
                    menu.classList.remove('d-none');
                    placeMenu();
                }
                function highlight() {
                    Array.from(menu.querySelectorAll('.hdc-combo-item')).forEach(function (el, i) {
                        el.classList.toggle('active', i === state.idx);
                    });
                }
                function pick(i) {
                    var opt = state.items[i];
                    if (!opt) return;
                    select.value = opt.value;
                    input.value = opt.text;
                    select.dispatchEvent(new Event('change', { bubbles: true }));
                    menu.classList.add('d-none');
                }
                function syncByExact() {
                    var q = (input.value || '').trim().toLowerCase();
                    if (!q) {
                        if (opts.clearOnEmpty !== false) select.value = '';
                        return;
                    }
                    var exact = Array.from(select.options).find(function (o) {
                        return (o.text || '').trim().toLowerCase() === q;
                    });
                    if (exact) {
                        select.value = exact.value;
                        select.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                }

                input.addEventListener('focus', render);
                input.addEventListener('input', function () {
                    if (opts.clearOnInput !== false) select.value = '';
                    render();
                });
                window.addEventListener('resize', placeMenu);
                window.addEventListener('scroll', placeMenu, true);
                input.addEventListener('keydown', function (e) {
                    if (menu.classList.contains('d-none')) return;
                    if (e.key === 'ArrowDown') {
                        e.preventDefault();
                        state.idx = Math.min(state.idx + 1, state.items.length - 1);
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

                return {
                    refresh: render
                };
            }
        };
    }
})();
