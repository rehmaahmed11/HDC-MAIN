# Purchase by item and by item type (category) + supplier ⇄ material / site tracking

The request from the field was short, so this file states the plain-English
requirement first, then what was built and where each piece lives. Every claim
points at the route, service or template that carries it, so it can be checked
without re-reading the whole application.

| # | Field request (as received) | Rewritten requirement |
|---|------------------------------|------------------------|
| 1 | "Purchase section show purchase / delivery / usage on item base" | A register where **one line = one item (material)**, showing what was bought, what was sent to sites and what was used — plus a per-item page listing that item's purchase, delivery and usage entries one by one. |
| 2 | "…and by item type = category" | Every item **and** every purchase line carries an **item type** (category): Cement, Steel, Electric Materials, Plumbing Materials … Totals and registers can be read per category. |
| 3 | "Every purchase and every material needs a category" | A material has a category; a purchase line defaults to its material's category but can be overridden on that line. Anything without one is listed under **Uncategorized** so it can be fixed. |
| 4 | "Filter for all categories plus separate category filters — full control" | Every register offers **All Categories**, **any single category**, and **Uncategorized**. Each category row on the category page carries its own filter links into every register. |
| 5 | "Supplier lists based on material/category" | The supplier list and supplier page can be read by item type, by item, and by site — and each supplier shows what it *stated* it deals in separately from what it *actually* supplied. |
| 6 | "Supplier ↔ site/project tracking in both directions" | Both readings exist: **site → suppliers** (who supplied this site, who is only assigned) and **supplier → sites** (which sites this supplier fed). The project page lists its own suppliers. |

## 1. Item types (categories)

**Vocabulary.** `hdc/services/material_categories.py` owns the category rules.
On a fresh database it seeds 14 defaults — Cement, Steel, Electric Materials,
Plumbing Materials, Bricks & Blocks, Sand & Aggregate, Tiles & Marble, Paint &
Finishing, Wood & Timber, Waterproofing & Chemicals, Hardware & Fasteners, Glass
& Aluminium, Fuel & Consumables, Miscellaneous — **only while the table is
empty**, so a rename is never overwritten. New categories are added on
`/hdc/purchase-v2/categories`.

**Storage.** New table `hdc_material_category` (`name`, `status`, `is_void`)
with a case-insensitive unique name; `category_id` columns added to
`hdc_material_v2` and `hdc_purchase_v2`, kept in step by
`hdc/core/schema.py` (`_ensure_material_category_schema`) so existing databases
heal on boot.

**The one rule every page shares:**

> a purchase line's category is the line's own `category_id` if it was set,
> otherwise the category of its material.

That rule is implemented once, in `_purchase_category_condition`,
`_delivery_category_condition` and `_usage_category_condition`
(`hdc/services/material_categories.py`), and every register filters through
those helpers — so the pages cannot disagree about what belongs to a category.
Delivery and usage resolve through their PO, falling back to the material when
no PO is linked.

**A second rule sits next to it** (`_material_category_for_write`, same
module): a category that was set *inactive* — because records still point at
it — may be **kept** on those records but never newly chosen; a *voided*
category is never allowed. This is what stops an unrelated edit (a quantity,
a date) from silently re-filing a purchase under its item's type, and what
stops the retired type from disappearing out of the edit form.

**Deleting is safe.** A category in use is never deleted (its rows would lose
their type): `_material_category_usage_counts()` decides, and the route sets it
`inactive` instead. An unused category is voided. The name of a voided
category may be typed again — the lookup prefers the live row, so the page
never revives a deleted type into a collision.

**Fixing old data.** `/hdc/purchase-v2/categories/assign` files many materials
under one category in a single action (`material_ids[]`, value `0` clears), and
`/hdc/purchase-v2/categories/sync-purchases` re-points every purchase line onto
its item's current category. The **Uncategorized** bucket (filter value `0`) is
how those rows are found.

## 2. Reading purchase / delivery / usage on item basis

* **`/hdc/purchase-v2/items`** — one row per item: category, unit, purchase
  rows/qty/value, delivery rows/sent qty, usage rows/used qty, **in store** and
  **balance at site**. Filters: category, item, site, stage, date range, sort.
  "In store" is a **balance**, so it always reads the full history — the same
  number the item ledger shows — while the flow columns follow the filters.
* **`/hdc/purchase-v2/items/<material_id>`** — that item's own ledger: its
  purchases, its deliveries (with the site), its usage entries, and the stock
  arithmetic that ties them together.
* Both pages are linked from the Purchase hub (`Item Categories`,
  `Item-wise View`) and from every category row.

## 3. Categories on every register and form

| Page | What it gained |
|------|----------------|
| Purchases (`/hdc/purchase-v2/purchases`) | Category filter, a **per-line category select** on the add/edit form (defaults to the item's category, can be overridden for that line only), an Item Category column, and per-category totals across the top. |
| Materials (`/hdc/purchase-v2/materials`) | Category select on add/edit, Item Category column, category filter. |
| Delivery (`/hdc/purchase-v2/delivered`) | Category filter on the register **and** inside the Pending Purchases pop-up; Item Category column. |
| Material Usage (`/hdc/purchase-v2/usage`) | Category filter, Item Category column. |
| Stock (`/hdc/purchase-v2/stock`) | Category filter, category column on both the summary and the delivery log. |
| Suppliers (`/hdc/purchase-v2/suppliers`) | Category filter alongside item and site filters; stated vs supplied category columns. |
| Categories (`/hdc/purchase-v2/categories`) | The vocabulary page: per-category purchase / sent / used totals, item counts, site and supplier counts, one-click filter links into every register, bulk assign, sync. |
| Suppliers by site (`/hdc/purchase-v2/suppliers-by-site`) | The two-direction view described below. |

The **quick-add material** dialogs (supplier page) also take a category, and the
JSON API behind them accepts `category_id` or a `category_name` that is created
on the spot (`POST`/`PUT /api/v2/purchase/materials`), so a new item can be
filed correctly without leaving the form.

## 4. Supplier ⇄ material / category

A supplier carries two different truths, and the UI keeps them apart on purpose:

* **Stated** ("Deals In") — the categories chosen on the supplier page
  (`hdc_supplier_category`), i.e. what this supplier is *expected* to supply.
* **Actual** ("Supplied") — computed from its purchase lines, resolved through
  the same category rule as everywhere else.

The same split exists for sites:

* **Assigned sites** — an explicit `hdc_supplier_site` link with an optional
  note.
* **Sites supplied** — actual deliveries, with quantity, value and last delivery
  date.

Both are shown on the supplier page (`Deals In (Item Categories)` and
`Sites — Assigned & Supplied` cards, each with add/remove forms) and on the
supplier list, and both are filterable: `?category_id=`, `?material_id=`,
`?project_id=`, `?supplier_id=`, `?q=`, plus sorting modes.

## 5. Both directions of supplier ⇄ site

* **`/hdc/purchase-v2/suppliers-by-site`** — two tables on one page:
  * **Site → Suppliers**: per site/supplier pair, categories and items supplied,
    delivery count, qty sent, value sent, last delivery; suppliers that are only
    *assigned* to a site appear as a warning row marked **Assigned only**.
  * **Supplier → Sites**: per supplier, what it deals in (stated), what it
    supplied, which sites it supplied (names + count), which sites it is
    assigned to, qty and value sent.
* **Project page** (`/hdc/projects/<id>`) — a **Site Suppliers (Purchase
  Tracking)** card listing the suppliers that delivered to that site and the
  ones only assigned, each linking to the supplier page and to the filtered
  by-site view.
* **Supplier page** — the same facts read from the supplier side.

Semantics stated plainly: **Supplied** means real delivery rows exist (value is
qty × the PO's unit price); **Assigned only** means a link exists but nothing
has moved yet. Removing a link never touches purchases, deliveries or usage.

## Where the behaviour lives

| Piece | File |
|-------|------|
| Category vocabulary, seed, the shared category conditions, the keep/choose write rule, per-category totals | `hdc/services/material_categories.py` |
| Supplier ⇄ category / site helpers, supplied profiles, site/supplier roll-ups, `_sync_purchase_category` | `hdc/services/purchase.py` |
| Category models and `category_id` columns | `hdc/models/materials.py` |
| New tables, `category_id` columns, healing on boot | `hdc/core/schema.py`, `hdc/core/bootstrap.py` |
| Category filters, category CRUD/assign/sync, item register, item ledger, by-site view, supplier link routes | `hdc/routes/purchase_v2.py` |
| Project page supplier card | `hdc/routes/projects.py`, `templates/hdc/projects/project_detail.html` |
| Material JSON API category support | `hdc/routes/api_purchase.py` |
| Category page | `templates/hdc/purchase/purchase_v2_categories.html` |
| Item register / item ledger | `templates/hdc/purchase/purchase_v2_items.html`, `purchase_v2_item_detail.html` |
| By-site view | `templates/hdc/purchase/purchase_v2_suppliers_by_site.html` |
| Category UI on the existing registers | `templates/hdc/purchase/purchase_v2_{purchases,materials,delivered,usage,stock,suppliers,supplier_detail}.html` |
| Pending-PO pop-up category filter | `static/hdc/js/pages/purchase_delivery.js` |
| Wipe targets / record-permission labels | `hdc/core/admin.py`, `hdc/services/record_permissions.py` |

## Tests

* `tests/test_purchase_categories_and_supplier_sites.py` — server side against a
  disposable SQLite database: the seed runs once, a category in use is
  deactivated rather than deleted, a line takes its chosen category or falls
  back to its item's, the sync button re-points lines, every register filters by
  one category and by Uncategorized and ignores an unknown id, the item-wise
  register sums purchase/delivery/usage per item, the item ledger lists the
  three registers, supplier stated-vs-supplied reads from links vs purchases,
  site ⇄ supplier rows read from deliveries, the by-site view shows both
  directions and "Assigned only", the project page lists its suppliers, and the
  material JSON API accepts `category_id` / `category_name`.
* `tests/test_purchase_categories_audit_fixes.py` — the audit of the above
  feature: the categories page must report real site / supplier counts, the
  Uncategorized bucket must be reachable and must be narrow on the Suppliers
  and Suppliers-by-site pages, the Stock page's "Sent of Purchased" must be
  measured over the items on screen, "In store" must be the store balance on
  both item pages, the by-site search must narrow the assigned rows too, one
  purchase row must show one item type everywhere, a retired type must survive
  an unrelated edit (and must not be newly chosen), and re-using a deleted
  type's name must not crash the page. See `PURCHASE_CATEGORY_SUPPLIER_AUDIT.md`.
* Existing suites that were extended or kept green: `tests/smoke_worker.py`
  (three new pages in the route sweep), `tests/test_purchase_entry_dialog.py`
  (the per-line category select is not a `required` cell, so a deliberately
  blank row is still judged by the page rule, not the browser), and
  `tests/test_delivery_filters_and_newest_first.py` (unchanged behaviour).
