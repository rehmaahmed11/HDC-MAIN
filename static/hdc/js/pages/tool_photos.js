/* HDC ERP — pages/tool_photos.js: show, add and replace a tool's photo.
 *
 * Why this exists
 * ───────────────
 * A photo could be uploaded with a tool, and then there was no way to look at
 * it: it appeared once, 48 px wide, inside a crowded inventory cell, and a
 * tool created before the photo field existed could never be given one. The
 * operator could not tell a saved photo from a failed upload.
 *
 * This script turns every rendered thumbnail / camera button into a dialog:
 *
 *   data-tool-photo="view"  →  #toolPhotoViewerModal   (full-size picture,
 *                               plus the "Position + history" link the eye
 *                               button in the same row already used)
 *   data-tool-photo="edit"  →  #toolPhotoUploadModal   (choose a file, see a
 *                               preview, save, or remove the photo)
 *
 * One pair of dialogs serves the whole page: the buttons carry the tool's id,
 * photo URL and upload URL as data attributes (see the macros in
 * templates/hdc/tool_rental/_tool_photo.html), so a list of 200 tools does not
 * render 200 modals.
 *
 * Saving is a normal multipart form POST — the server re-encodes the image,
 * flashes the result and redirects back to this page (`next`), which is why
 * nothing here talks to a JSON API. The 12 MB check below is only a courtesy:
 * hdc.services.tool_photos enforces it again on the server.
 *
 * Without JavaScript the thumbnails still show the saved photo inline, and the
 * Add-tool form still uploads one; only the enlarge / replace dialogs need it.
 */
(function () {
    'use strict';

    var VIEWER_ID = 'toolPhotoViewerModal';
    var UPLOAD_ID = 'toolPhotoUploadModal';
    var MAX_BYTES = 12 * 1024 * 1024;      // mirrors MAX_TOOL_PHOTO_BYTES
    var localObjectUrl = null;             // blob URL of the file being previewed

    function releaseLocalPreview() {
        if (localObjectUrl) {
            window.URL.revokeObjectURL(localObjectUrl);
            localObjectUrl = null;
        }
    }

    function element(id) { return document.getElementById(id); }

    function hasBootstrap() {
        return !!(window.bootstrap && window.bootstrap.Modal);
    }

    function openModal(node) {
        if (!node || !hasBootstrap()) { return; }
        window.bootstrap.Modal.getOrCreateInstance(node).show();
    }

    /* Bootstrap stacks badly: a second dialog opened while the first is still
     * fading ends up behind its own backdrop. Hide the others and wait for the
     * transition to finish before showing the one the operator asked for. */
    function closeOtherModals(keepId, then) {
        var open = document.querySelectorAll('.modal.show');
        var waiting = 0;
        var done = false;

        function finish() {
            if (done) { return; }
            if (waiting > 0) { return; }
            done = true;
            then();
        }

        Array.prototype.forEach.call(open, function (modal) {
            if (modal.id === keepId) { return; }
            waiting += 1;
            modal.addEventListener('hidden.bs.modal', function handler() {
                modal.removeEventListener('hidden.bs.modal', handler);
                waiting -= 1;
                finish();
            });
            window.bootstrap.Modal.getOrCreateInstance(modal).hide();
        });
        finish();
    }

    /* ---------------------------------------------------------------- viewer */
    function showViewer(button) {
        var modal = element(VIEWER_ID);
        var image = element('toolPhotoViewerImage');
        var title = element('toolPhotoViewerTitle');
        var meta = element('toolPhotoViewerMeta');
        var position = element('toolPhotoViewerPosition');
        var change = element('toolPhotoViewerEdit');
        var src = button.getAttribute('data-photo-src') || '';
        var name = button.getAttribute('data-photo-title') || 'Tool photo';
        if (!modal || !image || !src) { return; }

        image.src = src;
        image.alt = 'Photo of ' + name;
        if (title) { title.textContent = name; }
        if (meta) {
            meta.textContent = button.getAttribute('data-photo-meta') || '';
            meta.classList.toggle('d-none', !meta.textContent);
        }
        if (position) {
            position.href = button.getAttribute('data-photo-position-url') || '#';
        }
        if (change) {
            change.onclick = function () {
                closeOtherModals(UPLOAD_ID, function () { showUploader(button); });
            };
        }
        closeOtherModals(VIEWER_ID, function () { openModal(modal); });
    }

    /* -------------------------------------------------------------- uploader */
    function showUploader(button) {
        var modal = element(UPLOAD_ID);
        var form = element('toolPhotoUploadForm');
        if (!modal || !form) { return; }       // read-only page: no dialog rendered

        var input = element('toolPhotoUploadInput');
        var preview = element('toolPhotoUploadPreview');
        var empty = element('toolPhotoUploadEmpty');
        var title = element('toolPhotoUploadTitle');
        var hint = element('toolPhotoUploadHint');
        var action = element('toolPhotoUploadAction');
        var next = element('toolPhotoUploadNext');
        var remove = element('toolPhotoRemoveBtn');
        var src = button.getAttribute('data-photo-src') || '';
        var name = button.getAttribute('data-photo-title') || 'Tool photo';

        form.action = button.getAttribute('data-photo-upload-url') || form.action;
        if (action) { action.value = 'upload'; }
        if (next) { next.value = window.location.pathname + window.location.search; }
        if (title) { title.textContent = 'Photo — ' + name; }
        if (hint) { hint.textContent = ''; hint.className = 'small mt-2'; }
        if (input) { input.value = ''; }
        if (preview) {
            releaseLocalPreview();
            preview.classList.toggle('d-none', !src);
            if (src) { preview.src = src; preview.alt = 'Current photo of ' + name; }
            else { preview.removeAttribute('src'); }
        }
        if (empty) { empty.classList.toggle('d-none', !!src); }
        if (remove) { remove.classList.toggle('d-none', !src); }

        modal._hdcPhotoTarget = button;
        closeOtherModals(UPLOAD_ID, function () { openModal(modal); });
    }

    /* A file chosen in the dialog is previewed locally before the POST, so a
     * wrong pick is caught without a round-trip (and without losing the rest of
     * the form). The server validates and re-encodes it again. */
    function previewPickedFile() {
        var input = element('toolPhotoUploadInput');
        var preview = element('toolPhotoUploadPreview');
        var empty = element('toolPhotoUploadEmpty');
        var hint = element('toolPhotoUploadHint');
        var button = (element(UPLOAD_ID) || {})._hdcPhotoTarget;
        var current = button ? (button.getAttribute('data-photo-src') || '') : '';
        var file = input && input.files && input.files[0];

        if (hint) { hint.textContent = ''; hint.className = 'small mt-2'; }
        if (!file) {
            releaseLocalPreview();
            if (preview) {
                preview.classList.toggle('d-none', !current);
                preview.src = current || '';
            }
            if (empty) { empty.classList.toggle('d-none', !!current); }
            return;
        }
        if (file.size > MAX_BYTES) {
            if (hint) {
                hint.textContent = 'That file is ' + (file.size / (1024 * 1024)).toFixed(1) +
                    ' MB — the limit is 12 MB. Pick a smaller photo.';
                hint.className = 'small mt-2 text-danger fw-semibold';
            }
            input.value = '';
            return;
        }
        if (preview) {
            releaseLocalPreview();
            localObjectUrl = window.URL.createObjectURL(file);
            preview.src = localObjectUrl;
            preview.classList.remove('d-none');
            preview.alt = 'Selected photo for ' + (file.name || 'this tool');
        }
        if (empty) { empty.classList.add('d-none'); }
        if (hint) {
            hint.textContent = 'Ready to save: ' + file.name + ' (' +
                (file.size / 1024).toFixed(0) + ' KB).';
            hint.className = 'small mt-2 text-success';
        }
    }

    /* Removing is the same form with a different action, so the reader gets the
     * ordinary saved / not-saved dialog for it too. requestSubmit (not submit)
     * keeps the shared double-submit guard and validation in the loop. */
    function removePhoto() {
        var form = element('toolPhotoUploadForm');
        var action = element('toolPhotoUploadAction');
        var modal = element(UPLOAD_ID);
        var button = modal ? modal._hdcPhotoTarget : null;
        if (!form || !action || !button) { return; }
        if (!(button.getAttribute('data-photo-src') || '')) { return; }
        if (!window.confirm('Remove the photo of ' +
                (button.getAttribute('data-photo-title') || 'this tool') + '?')) { return; }
        action.value = 'remove';
        var input = element('toolPhotoUploadInput');
        if (input) { input.value = ''; }
        if (form.requestSubmit) { form.requestSubmit(); } else { form.submit(); }
    }

    /* ---------------------------------------------------------------- wiring */
    /* A stored file can go missing (a checkout copied without its uploads).
     * Say so on the row instead of leaving a broken-image icon. */
    function markMissingPhoto(image) {
        var button = image.closest('[data-tool-photo]');
        if (!button) { return; }
        var canEdit = button.getAttribute('data-photo-can-edit') === '1';
        button.setAttribute('data-photo-src', '');
        button.className = canEdit ? 'tool-photo-thumb tool-photo-thumb-empty'
                                   : 'tool-photo-thumb tool-photo-thumb-missing';
        button.setAttribute('data-tool-photo', canEdit ? 'edit' : 'view');
        button.title = canEdit ? 'Photo file is missing — add it again'
                               : 'Photo file is missing on the server';
        button.textContent = '';
        var icon = document.createElement('i');
        icon.className = canEdit ? 'fas fa-camera' : 'fas fa-image';
        icon.setAttribute('aria-hidden', 'true');
        button.appendChild(icon);
    }

    function onReady(fn) {
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', fn);
        } else {
            fn();
        }
    }

    onReady(function () {
        document.addEventListener('click', function (event) {
            var button = event.target.closest ? event.target.closest('[data-tool-photo]') : null;
            if (!button) { return; }
            event.preventDefault();
            if (!hasBootstrap()) {
                // No dialog engine: fall back to opening the picture itself.
                var src = button.getAttribute('data-photo-src');
                if (src) { window.location.href = src; }
                return;
            }
            if (button.getAttribute('data-tool-photo') === 'edit') { showUploader(button); }
            else { showViewer(button); }
        });

        // Capture-phase listener: 'error' does not bubble, only capture reaches here.
        document.addEventListener('error', function (event) {
            var image = event.target;
            if (!image || image.tagName !== 'IMG') { return; }
            if (image.hasAttribute('data-tool-photo-img')) { markMissingPhoto(image); }
            else if (image.id === 'toolPhotoViewerImage') {
                var meta = element('toolPhotoViewerMeta');
                if (meta) {
                    meta.textContent = 'This photo file could not be loaded from the server.';
                    meta.className = 'small mt-2 text-danger';
                }
            }
        }, true);

        var input = element('toolPhotoUploadInput');
        if (input) { input.addEventListener('change', previewPickedFile); }
        var remove = element('toolPhotoRemoveBtn');
        if (remove) { remove.addEventListener('click', removePhoto); }
        var uploadModal = element(UPLOAD_ID);
        if (uploadModal) {
            uploadModal.addEventListener('hidden.bs.modal', releaseLocalPreview);
        }

        // Let a page open a photo dialog itself (e.g. a shortcut or a test).
        window.HDCToolPhoto = {
            view: function (button) { showViewer(button); },
            edit: function (button) { showUploader(button); }
        };
    });
})();
