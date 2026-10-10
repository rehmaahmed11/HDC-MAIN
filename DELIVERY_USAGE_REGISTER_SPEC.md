# Delivery / Material Usage register — operator requests (rewritten & implemented)

The six bullet points that came in from the field were short-hand. This file is
the plain-English version of each one, what was already true in the code, and
what was changed. Every claim below points at the route or template that
carries it, so it can be checked without re-reading the whole app.

| # | Field request (as received) | Rewritten requirement |
|---|------------------------------|------------------------|
| 1 | "All New Entries either in accounts supplier deliver new entries shows at top" | Every register the operator reads must list the **newest entry first**: Accounts Entries, the Supplier page (purchases + ledger) and the Delivery register. |
| 2 | "In Delivery A butten where i see all pendings purchases that are filter by sites show in pop-up" | The Delivery page gets a **Pending Purchases** button that opens a **pop-up** listing every purchase order that still owes stock, with a **site filter** (plus material / supplier / search) and a one-click *Use PO* that pre-fills the delivery form. |
| 3 | "During enter delivery there is extra butten of 1. all sites 2. all stages" | The Delivery screen gets **All Sites** and **All Stages** buttons that jump the register back to the unfiltered view in one click (the site and stage dropdowns default to those same "All …" options). |
| 4 | "Need filters 1. deliver 2. material usages TO FILTER ENTRIES IN THESE SECTIONS" | Both the **Delivery** register and the **Material Usage** log get a real filter bar: PO #, material, supplier, site, stage, date range. |
| 5 | "Filter for arrangement of PO# number in delivery and material usage" | Both sections get an **Arrange (sort)** control so the list can be ordered by PO # ascending, PO # descending, newest entry, or oldest entry. |

## What was already true before this change

* `/hdc/accounts/entries` was **already newest-first** —
  `hdc/services/accounts.py::_account_transaction_history` orders by
  `AccountTransaction.date.desc(), AccountTransaction.id.desc()`. Left as it is
  and now pinned by a test so it cannot silently regress.
* `/hdc/purchase-v2/purchases` and `/hdc/purchase-v2/usage` were already
  newest-first and already had PO # / material / date filters
  (`tests/test_purchase_usage_filters.py`).

## What changed

### 1. Newest entry on top

* **Delivery register** (`/hdc/purchase-v2/delivered`) — the list was
  `created_at.asc()` (oldest first); it is now newest-first by default and also
  shows a *Recorded* column with the entry date + time.
* **Supplier page** (`/hdc/purchase-v2/suppliers/<id>`) — purchase history and
  supplier ledger are now newest-first. The ledger's running balance is still
  computed in chronological order (so each row keeps the correct balance at its
  own point in time) and only the *display* order is reversed; the row number
  column now shows the ledger row id instead of a sequence that would read
  backwards.
* **Stock page** (`/hdc/purchase-v2/stock`) — its delivery list follows the same
  newest-first rule for consistency.

### 2. Pending Purchases pop-up (Delivery page)

* New button in the Delivery top bar: **Pending Purchases** (shows how many POs
  still owe stock) opening the `#pendingPOModal` pop-up.
* Rows come from `hdc/services/purchase.py::_purchase_v2_pending_rows()` —
  a PO is *pending* while `quantity − delivered > 0`. Columns: PO #, date,
  supplier, material, ordered / delivered / **pending** qty, rate, pending
  value, and the sites that already took stock from that PO.
* Filters inside the pop-up: **Site** ("All Sites" by default), Material,
  Supplier and a free-text search across PO #, supplier and material. Filtering
  is instant and client-side (the rows are already in the page), so the pop-up
  never reloads out from under the operator.
* **Site semantics**, stated plainly because a purchase order is bought at store
  level and only becomes site-specific when it is delivered: picking a site
  narrows the list to the POs that have **already delivered to that site** and
  still owe stock, with that site's delivered quantity shown on the row.
* Each row carries a **Use PO** button: it closes the pop-up and pre-fills the
  Record Delivery form with that material and PO.

### 3. All Sites / All Stages buttons

The Delivery register's filter bar has two buttons — **All Sites** and
**All Stages** — that clear the matching filter and re-run the search. The
site and stage dropdowns themselves start on the "All Sites" / "All Stages"
option, so the unfiltered register is what an operator sees by default.

### 4. Filters in both sections

* **Delivery** (`/hdc/purchase-v2/delivered`): `po_number`, `material_id`,
  `supplier_id`, `project_id` (site), `stage_id`, `date_from`, `date_to`.
* **Material Usage** (`/hdc/purchase-v2/usage`): adds `project_id` (site) and
  `stage_id` to the existing `po_number`, `material_id`, `date_from`,
  `date_to`.

Totals at the top of each register follow the filtered rows, and a
**Clear** link resets everything.

### 5. Arrange by PO #

Both registers take `sort=`:

| value | Delivery | Material Usage |
|-------|----------|----------------|
| `newest` (default) | newest entry first | newest entry first |
| `oldest` | oldest entry first | oldest entry first |
| `po_asc` | PO # low → high | PO # low → high |
| `po_desc` | PO # high → low | PO # high → low |

An unknown value falls back to `newest`. An unknown `po_number` is ignored with
the same "PO number must be numeric." warning the Purchases page already uses.

## Where the behaviour lives

| Piece | File |
|-------|------|
| Pending-purchase rows (PO owes stock + the sites it fed) | `hdc/services/purchase.py::_purchase_v2_pending_rows` |
| Register filters, sort modes, newest-first ordering | `hdc/routes/purchase_v2.py` (`hdc_purchase_v2_delivered`, `hdc_purchase_v2_usage_page`, `hdc_purchase_v2_supplier_detail`, `hdc_purchase_v2_stock`) |
| Delivery screen: filter bar, All Sites / All Stages, pop-up | `templates/hdc/purchase/purchase_v2_delivered.html` |
| Usage screen: filter bar, All Sites / All Stages, sort | `templates/hdc/purchase/purchase_v2_usage.html` |
| Supplier page: newest-first labels, ledger row ids | `templates/hdc/purchase/purchase_v2_supplier_detail.html` |
| Pop-up filtering + All Sites / All Stages buttons (shared by both registers) | `static/hdc/js/pages/purchase_delivery.js` |

## Tests

* `tests/test_delivery_filters_and_newest_first.py` — server side, against a
  disposable SQLite database: newest-first ordering on the delivery register and
  the supplier page, every new filter on both registers, the sort modes, the
  pending-purchase pop-up rows and their site data, the totals following the
  filtered rows, and the Accounts Entries ordering that was already correct.
* `tests/test_purchase_delivery_js.py` + `tests/purchase_delivery_harness.js` —
  the real `purchase_delivery.js` run under Node against a minimal DOM: site /
  material / supplier / text filtering, the summary line, the "nothing matches"
  row, Reset filters, and the All Sites / All Stages buttons clearing only their
  own filter. The same harness approach `test_sidebar_groups_js.py` uses.

## Newest entry on top, by time and date stamp (Purchase section)

Rule applied to every Purchase-section list that shows delivery, PO or usage
entries: rows are ordered by the **entry's recorded time stamp**
(`created_at`, newest first, `id` breaks ties) and every such row shows the
date **and** the time it was recorded.

* Pending Purchases pop-up (`_purchase_v2_pending_rows`) — now newest first by
  time stamp (was PO # descending), with the recorded time under the date.
* Stock page delivery list — recorded time shown under the date.
* Supplier page purchase rows and ledger rows — recorded time shown under the date.
* Purchases, Delivery and Material Usage registers were already newest first
  and already show a *Recorded* date-time column.
