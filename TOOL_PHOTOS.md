## Compact photo access (latest correction)

Inventory and dashboard rows no longer show thumbnails or an extra photo action.
Click the existing tool code / serial badge to open the saved photo in the shared
viewer. Codes without a photo remain plain badges; writers can add a photo from
Edit or the tool position page. The eye button still opens position and history.

Fixed malformed photo controls that printed `data-*` metadata as visible text:
all metadata now belongs inside the button opening tag, including thumbnails in
Edit and on the position page. Regression tests check both compact rows and valid
attribute placement.

---

# Tool photos — see the picture you saved

The request from the field, in its own words:

> "last pr worked very well but how to see photo after saving the tool dont show
> on site, name instead make a show photo button or make it with eye button that
> already have"

Read as a requirement: **a saved tool photo must be visible on the site, with an
obvious button to open it, and a tool that was saved without one must be able to
get one later.**

| # | Field request | What it means on screen |
|---|---------------|-------------------------|
| 1 | "how to see photo after saving" | The photo is on the row itself, and one click opens it full size in a dialog. |
| 2 | "the tool dont show on site, name instead" | Every tools screen that names a tool now also shows its picture: Inventory, HDC Tools dashboard, and the tool's Position page. |
| 3 | "make a show photo button" | A **Show photo** button sits in the row's Actions, next to the eye button. |
| 4 | "or make it with eye button that already have" | The eye button still opens *Position + history* — and that page now carries the photo at 72 px with one click to enlarge, plus **Change / remove photo**. |

## What was already true

* The add-tool form accepted a photo and stored it as a resized WebP
  (`static/hdc/tool-images/<tool id>.webp`, max 640 px, ~12 MB upload ceiling),
  and `Tool.image_path` recorded it.
* The inventory table rendered it once — a 48 px `<img>` squeezed in front of
  the tool name. Nowhere else in the app showed it.
* Nothing could enlarge it, nothing said whether the upload had worked, and
  **the edit dialog had no file field at all**: a tool created before the photo
  column existed could never be given one.

## What changed

### 1. One place that stores and one place that renders

* `hdc/services/tool_photos.py` (new) — `save_tool_photo()`, `remove_tool_photo()`
  and `tool_photo_url()`. The add-tool form now calls the same writer the new
  dialog uses, so a photo is validated, EXIF-rotated, resized and re-encoded
  identically whichever way it arrives.
* `tool_photo_url()` is a Jinja global, so a template can never hand-build a
  photo URL. It returns `None` when the file is not on disk, which is what keeps
  a checkout copied without its uploads from rendering broken images — the row
  offers **Add photo** instead.
* The URL carries `?v=<file mtime in µs>`. A replacement keeps the same file
  name, so without the stamp the browser would keep showing the old picture
  after a successful save — exactly the "it did not work" feeling reported.

### 2. A Show photo button and a full-size viewer

* `templates/hdc/tool_rental/_tool_photo.html` — `tool_photo_thumb()`,
  `tool_photo_button()` and the `photo_data()` attributes they share.
* `templates/hdc/tool_rental/_tool_photo_dialogs.html` — one viewer dialog and
  one uploader dialog for the whole page (a 200-tool list does not render 200
  modals). It is **included**, not imported: an imported macro cannot see the
  `csrf_token` context variable and renders it empty (see 4).
* `static/hdc/js/pages/tool_photos.js` — opens the viewer from any thumbnail or
  button, previews a picked file before it is uploaded, refuses an oversized
  pick on the spot, and closes the dialog that opened it (Bootstrap stacks
  badly otherwise).
* Styles live with the other tools styles in `static/hdc/css/hdc.css`
  (`.tool-photo-thumb`, `.tool-photo-large`, `.tool-photo-preview`), light and
  dark.

### 3. A photo can be added, replaced and removed later

* `POST /hdc/tool-rental/inventory/<tool_id>/photo` — `action=upload` (default)
  or `action=remove`, behind `@login_required` and `@_money_write_required()`,
  the same gate as every other tool write. It flashes the ordinary saved /
  not-saved result and returns to the page that opened the dialog (`next`,
  accepted only for `/hdc/tool-rental/…` paths after dot-segment folding, so a
  posted `next` cannot aim the redirect elsewhere).
* Readers (e.g. a storekeeper on role defaults) see every photo and get the
  viewer, but no uploader is rendered and the write route refuses them.

### 4. Fixed on the way: the shared tool dialogs answered 400

The Purchase / Scrap dialogs on the tools pages are rendered by *imported*
macros (`{% from "_stock_forms.html" import … %}`). An imported macro does not
receive the context processor's `csrf_token`, so both forms carried
`<input name="_csrf_token" value="">`; the browser-side fallback skips a form
that already has the input, and the server rejected the submit with
**400 CSRF token missing or invalid**.

* `hdc/app.py::_inject_csrf_into_forms` now fills an *empty* token input as well
  as adding a missing one (works with JavaScript off).
* `templates/hdc/shared/base.html` fills an empty token in the browser too, for
  forms built after page load.

## Where the photo appears

| Screen | Path | What you get |
|--------|------|--------------|
| Tool Inventory | `/hdc/tool-rental/inventory` | 48 px thumbnail next to the name, **Show photo** button in Actions, photo block inside the Edit dialog |
| HDC Tools dashboard | `/hdc/tool-rental/dashboard` | 40 px thumbnail next to the tool name |
| One tool (the eye button) | `/hdc/tool-rental/tool/<id>` | 72 px photo in the header, **Change / remove photo** |

## Tests

`tests/test_tool_photos.py` (18 cases) — upload through the real form, the
thumbnail and viewer on all three screens, adding a photo to a tool that never
had one, cache-stamp change on replacement, removal, non-image / oversized /
empty uploads, a missing file not rendered as a picture, a reader refused the
write, the `next` open-redirect guard, and the CSRF regression from 4.

```
HDC_BOOTSTRAP_ADMIN_PASSWORD='Admin@1234' python -m unittest tests.test_tool_photos -v
```

Uploaded photos are operator data, not code: `static/hdc/tool-images/` is in
`.gitignore`, so a photo never dirties the deploy checkout (the deploy hook
reports a dirty tree as "a pull will fail").
