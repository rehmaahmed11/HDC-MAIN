/* HDC ERP — core/dialog.js: the entry-result dialogue box (loaded by base.html).
 *
 * Why this exists
 * ───────────────
 * Every data-entry screen reported its result the same way: the server
 * redirected, `flash()` put a sentence in the banner strip at the top of the
 * page, and the operator — whose eyes are still on the form they just filled —
 * never saw it. A saved purchase looked exactly like a rejected one. This
 * module turns that banner into a real dialogue box: a centred modal that
 * states the outcome (Saved / Not saved / Check this) and has to be
 * acknowledged before the form is usable again.
 *
 * Two independent feeds drive the same dialog:
 *
 *   1. The server. `shared/_result_dialog.html` writes the flash queue into a
 *      <script type="application/json"> block; `playServerResult()` reads it
 *      on load. This is the authoritative result (duplicate prevention, stock
 *      limits, ledger failures …), so it wins whenever it is present.
 *   2. The browser, before the form is submitted. `guard()` takes over the
 *      form's own validation: `noValidate` stops the native bubble, the
 *      invalid controls are collected into a message list, and the submit is
 *      blocked with an error dialog instead. Pages add their own rules with
 *      `beforeSave()` (remaining-stock checks, half-filled item rows), so a
 *      mistake is named where it was made instead of after a round-trip.
 *
 * The dialog is built once, on demand, and appended to <body> — never inside a
 * card, so no ancestor `transform`/`filter` can move the fixed overlay. The
 * inline banner stays in the markup for browsers with JavaScript disabled;
 * `hideFlashBanner()` only folds it away on pages that mount the dialog.
 */
(function () {
    'use strict';

    var DIALOG_ID = 'hdcResultDialog';
    var DATA_ID = 'hdcResultDialogData';
    var BANNER_ID = 'flashMessages';
    var GUARD_ATTR = 'data-hdc-dialog';     // <form data-hdc-dialog>
    var DOUBLE_SUBMIT_MS = 8000;            // must match core/forms.js

    // Tone → wording. The verbs matter as much as the colour: "recorded" for a
    // purchase and "not recorded" for a validation failure, so the operator
    // never has to read the fine print to know whether to type again.
    var PRESETS = {
        success: { title: 'Entry Saved', icon: 'fa-circle-check', dismissible: true, tone: 'hdc-result-ok' },
        danger: { title: 'Entry Not Saved', icon: 'fa-circle-exclamation', dismissible: false, tone: 'hdc-result-bad' },
        warning: { title: 'Saved With a Warning', icon: 'fa-triangle-exclamation', dismissible: false, tone: 'hdc-result-warn' },
        info: { title: 'Notice', icon: 'fa-circle-info', dismissible: true, tone: 'hdc-result-info' }
    };
    // The app flashes a few other categories in odd corners; fold them into
    // the nearest tone rather than inventing an unstyled fourth look.
    var KIND_ALIAS = {
        error: 'danger', failed: 'danger', fail: 'danger',
        saved: 'success', ok: 'success', message: 'info',
        secondary: 'info', light: 'info', dark: 'info', primary: 'info'
    };
    var TONES = ['hdc-result-ok', 'hdc-result-bad', 'hdc-result-warn', 'hdc-result-info'];

    var node = null;
    var bsModal = null;
    var lastFocus = null;
    var played = false;      // server result already shown for this page view
    var rules = [];          // [{form: Element|null, fn: Function}]

    function toneKey(kind) {
        var key = String(kind || 'info').toLowerCase();
        if (PRESETS[key]) return key;
        return PRESETS[KIND_ALIAS[key]] ? KIND_ALIAS[key] : 'info';
    }

    function presetFor(kind) { return PRESETS[toneKey(kind)]; }

    function make(tag, cls, text) {
        var el = document.createElement(tag);
        if (cls) el.className = cls;
        if (text !== undefined && text !== null) el.textContent = String(text);
        return el;
    }

    // ── the dialog itself ───────────────────────────────────────────────────
    function build() {
        if (node && document.body.contains(node)) return node;
        node = make('div');
        node.className = 'modal fade hdc-result-dialog';
        node.id = DIALOG_ID;
        node.setAttribute('tabindex', '-1');
        node.setAttribute('role', 'alertdialog');
        node.setAttribute('aria-modal', 'true');
        node.setAttribute('aria-labelledby', DIALOG_ID + 'Title');

        var dialog = make('div', 'modal-dialog modal-dialog-centered');
        var content = make('div', 'modal-content hdc-modal');

        // Built with element nodes rather than a markup string: nothing here
        // ever interpolates operator text into HTML, and the parts can be
        // cached instead of re-queried on every show().
        var body = make('div', 'modal-body text-center');
        var iconDisc = make('div', 'hdc-result-icon');
        var icon = make('i', 'fas');
        icon.setAttribute('aria-hidden', 'true');
        iconDisc.appendChild(icon);
        var title = make('h5', 'hdc-result-title');
        title.id = DIALOG_ID + 'Title';
        var lines = make('div', 'hdc-result-lines');
        body.appendChild(iconDisc);
        body.appendChild(title);
        body.appendChild(lines);

        var footer = make('div', 'modal-footer justify-content-center border-0 pb-3');
        var ok = make('button', 'btn btn-mint px-4');
        ok.type = 'button';
        ok.id = DIALOG_ID + 'Ok';
        ok.setAttribute('data-bs-dismiss', 'modal');
        var okIcon = make('i', 'fas fa-check me-1');
        okIcon.setAttribute('aria-hidden', 'true');
        var okText = make('span', null, 'OK');
        ok.appendChild(okIcon);
        ok.appendChild(okText);
        footer.appendChild(ok);

        content.appendChild(body);
        content.appendChild(footer);
        dialog.appendChild(content);
        node.appendChild(dialog);
        document.body.appendChild(node);

        node._hdcParts = { icon: icon, title: title, lines: lines, cta: okText };

        node.addEventListener('hidden.bs.modal', function () {
            if (lastFocus && document.contains(lastFocus)) {
                try { lastFocus.focus(); } catch (err) { /* control replaced mid-save */ }
            }
        });
        // Enter acknowledges — the operator's hands are already on the keyboard.
        node.addEventListener('keydown', function (e) {
            if (e.key === 'Enter') { e.preventDefault(); hide(); }
        });
        return node;
    }

    function paint(payload) {
        var preset = presetFor(payload.kind);
        var parts = node._hdcParts;
        TONES.forEach(function (tone) { node.classList.remove(tone); });
        node.classList.add(preset.tone);
        parts.icon.className = 'fas ' + preset.icon;
        parts.title.textContent = payload.title || preset.title;
        parts.cta.textContent = payload.cta || 'OK';

        var box = parts.lines;
        while (box.firstChild) box.removeChild(box.firstChild);
        var lines = (payload.lines || []).filter(function (line) { return line && String(line).trim(); });
        if (!lines.length) {
            box.appendChild(make('div', 'text-muted small', 'No message was returned.'));
        } else if (lines.length === 1) {
            box.appendChild(make('div', 'hdc-result-line', lines[0]));
        } else {
            box.appendChild(make('div', 'hdc-result-line fw-semibold', lines[0]));
            var list = make('ul', 'hdc-result-list');
            lines.slice(1).forEach(function (line) { list.appendChild(make('li', null, line)); });
            box.appendChild(list);
        }
        if (payload.note) box.appendChild(make('div', 'hdc-result-note', payload.note));
    }

    /**
     * Show a result. `opts` = {kind, title, message|lines, note, cta}.
     * Returns the tone it rendered as, so callers can branch if they care.
     */
    function show(opts) {
        opts = opts || {};
        var payload = {
            kind: toneKey(opts.kind),
            title: opts.title,
            cta: opts.cta,
            note: opts.note,
            lines: opts.lines || (opts.message ? [opts.message] : [])
        };
        if (!document.body || typeof window.bootstrap === 'undefined') {
            // Bootstrap is bundled locally, so this only fires on a page that
            // shipped without it. A native box still beats staying silent, and
            // it has to carry the verdict too — not just the server sentence.
            var fallback = payload.lines.slice();
            fallback.unshift(payload.title || presetFor(payload.kind).title);
            window.alert(fallback.filter(Boolean).join('\n'));
            return payload.kind;
        }
        build();
        paint(payload);
        lastFocus = document.activeElement;
        if (bsModal && bsModal._hdcKind !== payload.kind) {
            // Tone decides whether the backdrop may dismiss it, so the instance
            // is rebuilt when the kind flips between an acknowledgement and a
            // notice instead of quietly keeping the old behaviour.
            try { bsModal.dispose(); } catch (err) { /* already gone */ }
            bsModal = null;
        }
        if (!bsModal) {
            bsModal = new window.bootstrap.Modal(node, {
                backdrop: presetFor(payload.kind).dismissible ? true : 'static',
                keyboard: presetFor(payload.kind).dismissible
            });
            bsModal._hdcKind = payload.kind;
        }
        bsModal.show();
        whenOpen(function () {
            var cta = document.getElementById(DIALOG_ID + 'Ok');
            if (cta) cta.focus();
        });
        if (payload.kind !== 'success') hideFlashBanner();
        return payload.kind;
    }

    function hide() {
        if (bsModal) {
            try { bsModal.hide(); } catch (err) { if (node) node.classList.remove('show'); }
        }
    }

    function whenOpen(fn) {
        if (!node) { fn(); return; }
        node.addEventListener('shown.bs.modal', function once() {
            node.removeEventListener('shown.bs.modal', once);
            fn();
        });
    }

    // ── result the server flashed ────────────────────────────────────────────
    function readServerPayload() {
        var el = document.getElementById(DATA_ID);
        if (!el) return null;
        try {
            var data = JSON.parse(el.textContent || '{}');
            var items = Array.isArray(data) ? data : (data && Array.isArray(data.items) ? data.items : []);
            return items.length ? items : null;
        } catch (err) {
            return null;   // malformed JSON must not take the page down
        }
    }

    function hideFlashBanner() {
        var banner = document.getElementById(BANNER_ID);
        if (banner) banner.hidden = true;
    }

    function playServerResult() {
        if (played) return false;
        var items = readServerPayload();
        if (!items) return false;
        // Worst tone leads: an entry that saved but tripped a warning must not
        // be filed under "Entry Saved".
        var rank = { info: 0, success: 1, warning: 2, danger: 3 };
        var worst = items.slice().sort(function (a, b) {
            return rank[toneKey(b.kind)] - rank[toneKey(a.kind)];
        })[0];
        var seen = [];
        var lines = items.map(function (item) { return String(item.text || '').trim(); })
            .filter(function (line) {
                if (!line || seen.indexOf(line) !== -1) return false;
                seen.push(line);
                return true;
            });
        // An all-blank payload must not open a dialog that says nothing; the
        // banner is left in place instead, as if we were never mounted.
        if (!lines.length) return false;
        played = true;
        hideFlashBanner();
        show({ kind: worst.kind, lines: lines });
        return true;
    }

    // ── before-save interception ────────────────────────────────────────────
    function resolveForm(formOrId) {
        return typeof formOrId === 'string' ? document.getElementById(formOrId) : formOrId;
    }

    /**
     * Register a rule for a form. The rule returns nothing (fine), a message
     * or array of messages, or {message, focus}. A message blocks the submit
     * and opens the dialog; `focus` is scrolled to and focused after it opens.
     */
    function beforeSave(formOrId, fn) {
        var form = resolveForm(formOrId);
        if (typeof fn !== 'function') return;
        rules.push({ form: form, fn: fn });
        if (form) guard(form);
    }

    function labelFor(field) {
        var text = '';
        if (field.id) {
            var lab = document.querySelector('label[for="' + field.id + '"]');
            if (lab) text = lab.textContent;
        }
        if (!String(text).trim() && field.labels && field.labels.length) text = field.labels[0].textContent;
        if (!String(text).trim() && field.closest) {
            // The item rows of the purchase form label their inputs without a
            // `for`, so the nearest label inside the same column is the name.
            var wrap = field.closest('.col-md-4, .col-5, .col-6, .col-8, .col-md-2, .col-md-3, .col-md-5');
            var inner = wrap && wrap.querySelector ? wrap.querySelector('label') : null;
            if (inner && inner !== field) text = inner.textContent;
        }
        text = String(text || '').replace(/[\s:*]+$/g, '').replace(/\s+/g, ' ').trim();
        if (text) return text;
        var name = String(field.name || field.getAttribute('placeholder') || '').replace(/\[\]$/, '');
        if (!name) return 'Field';
        return name.replace(/_/g, ' ').replace(/\b\w/g, function (c) { return c.toUpperCase(); });
    }

    function describe(field) {
        var label = labelFor(field);
        var value = (field.value === undefined || field.value === null) ? '' : String(field.value).trim();
        if (field.validity && field.validity.valueMissing) return label + ' is required.';
        if (value) return label + ': ' + (field.validationMessage || 'please check this value.');
        return label + ' needs a value.';
    }

    function invalidFields(form) {
        if (!form.elements) return [];
        return Array.prototype.filter.call(form.elements, function (field) {
            if (!field || field.disabled || field.type === 'hidden' || field.getAttribute('data-hdc-dialog-skip') === 'true') return false;
            if (!field.willValidate) return false;
            return typeof field.checkValidity === 'function' && !field.checkValidity();
        });
    }

    function normalize(result) {
        if (!result) return [];
        var list = Array.isArray(result) ? result : [result];
        return list.filter(Boolean).map(function (item) {
            if (typeof item === 'string') return { message: item, focus: null };
            return { message: item.message || String(item), focus: item.focus || null };
        }).filter(function (item) { return String(item.message).trim(); });
    }

    function runRules(form) {
        var found = [];
        rules.forEach(function (rule) {
            if (rule.form && rule.form !== form) return;
            if (!rule.form && !isGuarded(form)) return;
            var out;
            try {
                out = rule.fn(form);
            } catch (err) {
                // A broken page rule must never eat the operator's entry.
                if (window.console) window.console.error('[hdcDialog] rule failed', err);
                return;
            }
            found = found.concat(normalize(out));
        });
        return found;
    }

    function isGuarded(form) { return !!(form.hasAttribute && form.hasAttribute(GUARD_ATTR)); }

    function releaseDoubleSubmitLock(form) {
        // core/forms.js locks a form for a few seconds on submit so a slow
        // network cannot be clicked twice into two rows. A submit we blocked
        // never reached the network, so the lock has to go — otherwise the
        // operator's corrected attempt is silently swallowed.
        form.dataset.submitted = '0';
        Array.prototype.forEach.call(form.querySelectorAll('button[type="submit"], input[type="submit"]'), function (btn) {
            btn.disabled = false;
            resetSaving(btn);
        });
    }

    // The submit button carries the progress: its label is swapped for a
    // spinner rather than covered by an overlay, so it keeps its width in the
    // row and is announced as busy.  An <input type=submit> has no children,
    // so its value is swapped instead.
    var busyButtons = [];   // for the Back-button reset below

    function markSaving(form) {
        var btn = form.querySelector('button[type="submit"], input[type="submit"]');
        if (!btn || btn.dataset.hdcSavingShown === '1') return;
        btn.dataset.hdcSavingShown = '1';
        busyButtons.push(btn);
        btn.setAttribute('aria-busy', 'true');
        if (btn.tagName === 'INPUT') {
            btn._hdcPrevValue = btn.value;
            btn.value = 'Saving\u2026';
        } else {
            btn._hdcHeld = [];
            while (btn.firstChild) btn._hdcHeld.push(btn.removeChild(btn.firstChild));
            var spin = make('span', 'spinner-border spinner-border-sm me-1');
            spin.setAttribute('aria-hidden', 'true');
            btn.appendChild(spin);
            btn.appendChild(make('span', null, 'Saving\u2026'));
        }
        // Same window core/forms.js gives a stuck submit; a normal redirect
        // tears the page down long before it fires.
        window.setTimeout(function () { resetSaving(btn); }, DOUBLE_SUBMIT_MS);
    }

    function resetSaving(btn) {
        if (!btn || btn.dataset.hdcSavingShown !== '1') return;
        btn.dataset.hdcSavingShown = '0';
        btn.removeAttribute('aria-busy');
        if (btn.tagName === 'INPUT') {
            if (btn._hdcPrevValue) btn.value = btn._hdcPrevValue;
            return;
        }
        while (btn.firstChild) btn.removeChild(btn.firstChild);
        (btn._hdcHeld || []).forEach(function (child) { btn.appendChild(child); });
        btn._hdcHeld = null;
    }

    function guard(form) {
        if (!form || form.dataset.hdcDialogGuarded === '1') return form;
        form.dataset.hdcDialogGuarded = '1';
        // Our bubble replaces the native one, so the browser must not preempt it.
        form.noValidate = true;
        form.setAttribute('novalidate', 'novalidate');
        form.addEventListener('submit', function (e) {
            var problems = runRules(form);
            if (!problems.length) {
                problems = invalidFields(form).map(function (field) {
                    return { message: describe(field), focus: field };
                });
            }
            if (problems.length) {
                e.preventDefault();
                e.stopPropagation();
                releaseDoubleSubmitLock(form);
                var first = null;
                problems.forEach(function (problem) {
                    if (problem.focus && problem.focus.classList) {
                        problem.focus.classList.add('is-invalid');
                        if (!first) first = problem.focus;
                    }
                });
                show({
                    kind: 'danger',
                    title: 'Entry Not Saved',
                    lines: [problems.length === 1
                        ? 'Fix this before saving:'
                        : problems.length + ' things need attention before saving:']
                        .concat(problems.map(function (p) { return p.message; })),
                    cta: 'Fix Entry'
                });
                if (first) {
                    whenOpen(function () {
                        try {
                            first.scrollIntoView({ block: 'center' });
                            first.focus({ preventScroll: true });
                        } catch (err) { /* detached control */ }
                    });
                }
                return false;
            }
            markSaving(form);
            return true;
        }, true);
        clearHighlightOnEdit(form);
        return form;
    }

    function clearHighlightOnEdit(form) {
        // The red outline is a one-shot: drop it as soon as the value changes
        // so a corrected row does not stay marked while the dialog is open.
        if (!form || form.dataset.hdcDialogClearBound === '1') return;
        form.dataset.hdcDialogClearBound = '1';
        var handler = function (e) {
            if (e.target && e.target.classList) e.target.classList.remove('is-invalid');
        };
        form.addEventListener('input', handler, true);
        form.addEventListener('change', handler, true);
    }

    function installGuards() {
        Array.prototype.forEach.call(document.querySelectorAll('form[' + GUARD_ATTR + ']'), function (form) {
            guard(form);
        });
    }

    function whenReady(fn) {
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', fn, { once: true });
        } else {
            fn();
        }
    }

    // Coming Back into a saved page restores it from the browser cache exactly
    // as it stood — with the button still reading "Saving…" — so the label is
    // reset when the page is re-shown.  (The result dialog is not replayed:
    // the operator already read it on the way out.)
    window.addEventListener('pageshow', function (e) {
        if (!e || !e.persisted) return;
        busyButtons.splice(0).forEach(resetSaving);
    });

    // The server result is played before the guards are wired so a failed save
    // cannot be re-blocked by a rule that is only right for a fresh entry.
    whenReady(function () {
        playServerResult();
        installGuards();
    });

    window.hdcDialog = {
        /** Open a dialog. `{kind, title, message|lines, note, cta}` */
        show: show,
        hide: hide,
        success: function (message, opts) {
            return show(Object.assign({ kind: 'success', message: message }, opts || {}));
        },
        error: function (message, opts) {
            return show(Object.assign({ kind: 'danger', message: message }, opts || {}));
        },
        warning: function (message, opts) {
            return show(Object.assign({ kind: 'warning', message: message }, opts || {}));
        },
        /** Add a client-side save rule. See `beforeSave`. */
        beforeSave: beforeSave,
        /** Wire `data-hdc-dialog` behaviour onto a form built after page load. */
        guard: guard,
        /** The flash payload this page was rendered with, if any. */
        serverResult: readServerPayload,
        /**
         * True when the server already reported on this page view. Pages that
         * auto-open an edit modal on load check this so the two dialogs do not
         * stack on top of each other.
         */
        hasPendingResult: function () { return played || !!readServerPayload(); }
    };
})();
