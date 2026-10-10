# HDC Tools — Stock Summary & Complete Tool Tracking

Every tool HDC owns must always satisfy one identity:

```
Total Owned (Inventory)  =  In Store  +  Sent to Own Projects  +  Sent to Other Customers
```

The Tools section is built around that identity. The **dashboard** shows it the
simple way (item by item), while the views that prove it — movement chains,
per-location positions, overdue warnings, and the physical **Audit** count —
live on their own pages, so no page repeats the same numbers twice.

- Dashboard (simple stock + unpaid rent, item by item): `/hdc/tool-rental/dashboard` (sidebar → **HDC Tools**)
- Rentals (summary + search + list; return and pay from a rental's page): `/hdc/tool-rental`
- New Rental (the one transaction form, opened by the hub's **New Rental** action): `/hdc/tool-rental/new`
  — the transaction dropdown offers **New Rental, Transfer Rental, Return Tools and Rent Payment**
  (see *One transaction form* below)
- One tool (position + movement history): `/hdc/tool-rental/tool/<tool_id>`
- Tracking (chains, where each piece is right now, admin): `/hdc/tool-rental/tracking`
- **Track by Serial** (every individual serial-numbered piece, its current
  location and full movement chain, admin): `/hdc/tool-rental/serials`
- JSON feed: `/hdc/api/tool-rental/dashboard`
- Inventory (add tools, buy stock, scrap, categories): `/hdc/tool-rental/inventory`
- Audit (book position per place, physical count, discrepancies, adjust):
  `/hdc/tool-rental/audit` — one count sheet per place:
  `/hdc/tool-rental/audit/<audit_id>`
- Logic: `hdc/services/tool_tracking.py`, `hdc/services/tool_rental.py`,
  `hdc/services/tool_audit.py`
- Pages: `templates/hdc/tool_rental/tool_dashboard.html`, `tool_position.html`,
  `tool_inventory.html`, `tool_rental.html`, `tool_new_rental.html`,
  `tool_tracking.html`, `tool_serial_tracking.html`, `tool_reports.html`,
  `_stock_forms.html`, `tool_audit.html`, `tool_audit_sheet.html`,
  `_audit_line.html`
- Tests: `tests/test_tool_tracking.py` (20 cases),
  `tests/test_tool_stock_lifecycle.py` (22 cases),
  `tests/test_tool_audit.py` (35 cases),
  `tests/test_tool_new_rental_smoke.py` (New Rental form → smoke result →
  Tracking + Reports),
  `tests/test_tool_rentals_transfers_populate.py` (populate + verify smoke:
  new rentals and transfers → Audit book quantities, Tracking, reconciliation),
  `tests/test_tool_serial_tracking.py` (Track-by-Serial page + full-section
  smoke: inventory add, purchase, rental, transfer, return, scrap, payment,
  audit all asserted at the individual-serial level)

---

## 0. One transaction form (`/hdc/tool-rental/new`)

Every rental-life-cycle transaction is entered on the same page. The operator
picks the **Transaction type** first and only that type's fields appear:

| Type | Posts to | Picks |
| --- | --- | --- |
| New Rental | `POST /hdc/tool-rental/create` | tools from store, site or customer |
| Transfer Rental | `POST /hdc/tool-rental/create` (`txn_type=transfer`) | tools at a holder/site, destination, settlement |
| Return Tools | `POST /hdc/tool-rental/<id>/return` | a rental with tools still out; quantities back; optional rent collection |
| Rent Payment | `POST /hdc/tool-rental/<id>/payment` | a rental that still owes rent; amount, account, mode, waive-off |

It is data-driven: the table lives in `hdc/services/tool_txn_types.py`
(`TOOL_TXN_TYPES`: key, label, endpoint, whether it needs a rental, which
rentals qualify, submit label and help text). The route hands that table to the
page, which builds the dropdown, headings, submit button and the form's
`action` from it. Each panel is tagged with `data-txn-modes="…"`; the page shows
the panels of the chosen type, and disables the rest so hidden fields never
block or leak into a submit.

Return and payment reuse the existing endpoints, so their rules (quantities,
serials, discount limits, account posting, duplicate guard) are unchanged. The
rental pickers only list rentals that qualify: return = tools still out, payment
= rent still due (no-charge rentals are never payment targets). The return panel
returns quantities per tool; for serial-by-serial returns use the rental's page.

Tests: `tests/test_tool_txn_form.py` (registry, rendered form, pickers, and the
endpoints the form posts to).

## 1. What the dashboard shows (and what it deliberately does not)

The dashboard is one simple page: **how many tools we own item by item, how
many are in the store, how many are rented out, and how much rent customers
have still not paid.**

| Block | What it answers |
| --- | --- |
| KPI strip | Total owned · in store · rented out (own sites + customers split) · **rent not paid by customers (PKR)** |
| Tools-by-item table | Per item: total owned, in store, rented out (qty + own-site/customer split), **unpaid customer rent** |
| Totals row | The same five numbers added up for the filtered list |
| Search / category filter | `q` (name, code, category) and `category_id` only |

**Rent not paid by customers** is the unpaid balance
(`total_amount − total_paid`) of outside-customer rentals, split across the
rental's lines in proportion to the line amount (falling back to the rented
qty). Own-site (internal) rentals are HDC renting from itself, so they are
reported separately as a note under the KPI and never counted as a customer
due. A `no_charge` rental owes nothing by definition.

Deliberately **not** on the dashboard, because they already have their own home:

| Detail | Where it lives |
| --- | --- |
| Movement chains, site-to-site transfers, where each piece is now | Tracking (`/hdc/tool-rental/tracking`) and one tool's position page |
| Reconciliation strip (`store + sites + customers = owned`), Balanced/off badge | Tool position page and Tracking (`/hdc/tool-rental/tracking`) |
| Overdue / long-out / non-reconciling warnings | Rentals page (overdue rentals) + Tracking |
| Creating a rental | New Rental page (`/hdc/tool-rental/new`), opened from the hub |
| Purchase / scrap life-cycle, conditions, rates | Inventory (`/hdc/tool-rental/inventory`) |
| Rental-level money (per rental, per customer, payments) | Rentals, rental detail and Reports |

---

## 2. The rules (deliberately boring, so it always reconciles)

1. A tool is **out** while its rental line still has `qty_pending > 0`.
2. The position of a pending line is replayed from its events — see §3.
3. Everything owned but not out is **in store** (`Warehouse / Store`).
4. Damaged / lost / maintenance pieces are reported separately but stay on the
   store side of the balance, so rule 3 cannot be broken by a condition change.
5. Void rentals and void tools hold nothing.
6. A transfer whose destination is the store is a **return**, not a movement —
   otherwise the tools would leave a site without `qty_pending` falling and the
   balance would break.

Consequence: `owned == in_store + out` for every tool, always. When bad legacy
data breaks it, the row is flagged `unaccounted` with the signed variance —
surfaced in `tools_attention()` on the tracking and position views (and in
the JSON feed), not silently averaged away on the simple dashboard.

---

## 3. Quantity-accurate positions (event replay)

A rental line is not "at one place". It is replayed into **buckets**:

```
start     → bucket[origin] += qty_rented
transfer T → lift T pieces from the recorded From location (latest bucket for
             older transfers), then open bucket[destination] += T
return  R → take R pieces off the most recent bucket first
```

So a partial transfer really does leave a tool in two places at once:

```
Steel Shuttering Prop (200 owned)
  Warehouse / Store                     30
  Gulshan Villa Block A > Grey Structure 80   chain: Gulshan A > Grey Structure
  DHA Phase 6 Tower > Slab Work          40   chain: Gulshan A > Grey Structure › DHA 6 > Slab Work
  Bilal Construction Co (overdue)        50   chain: Bilal Construction Co
```

Two details that matter:

- **Legacy transfers** recorded before the per-tool split existed have no
  `hdc_tool_rental_transfer_item` rows. They are treated as "the whole pending
  line moved", which is exactly what the old movement log assumed. Old data
  keeps working; new transfers are precise.
- **Drift guard**: if replayed buckets do not sum to `qty_pending` (returns
  logged before the split existed), the difference is forced onto the last
  bucket. The dashboard can never show phantom stock.

### Per-tool transfer split

`hdc_tool_rental_transfer_item` (new table) records which tool and how many
pieces each transfer moved. The transfer form on the rental detail page has a
qty box per pending tool line, an **All** button per line and **Move everything
pending**; the total box mirrors the per-tool boxes so the two cannot disagree.
Leave every box blank and the old behaviour applies (allocate the total across
the pending lines).

`ToolRentalTransfer.qty_transferred` is rewritten to the qty that actually
moved, and the flash message names the tools:
`Tools transferred: Site A to Site B (2 qty — Angle Grinder x2). Chain: Site A > Site B`.

The **Transfer Rental** flow on `/hdc/tool-rental/new` follows the operator's
sequence: transaction type → From holder(s)/location → available tools →
rent type → To holder → other settings. From choices are grouped by each live rental's
actual location, so a rental split across sites only offers the quantities at
the chosen site. Choosing a location lists **every rental (holder) that keeps
tools there** as a checkbox — plus a *Select all holders* toggle — because one
site often receives tools through more than one rental: tick any combination of
holders and then any combination of their tools, and they all move in a single
transfer. Each contributing holder still keeps its own
`hdc_tool_rental_transfer` row (its own from-location, per-tool split and rent
settlement), while one new rental is opened for the destination; the
hand-over chain therefore names every holder it was fed from. Checked tool lines
move their displayed quantity and use their own destination rate; unchecked
lines and quantities at other locations stay with the previous rental. Choosing
rent already fixed in the contact/contract hides the rate inputs but still
allows the tools to be selected and moved.

### Hand-overs vs in-place moves (no double count)

Two different things both leave a `ToolRentalTransfer` row, and the replay
treats them differently:

- **In-place move** (`/hdc/tool-rental/<id>/transfer`): the rental itself
  changes place. `to_rental_id` is NULL, so the destination bucket opens on
  the same rental.
- **Hand-over** (Transfer Rental on `/hdc/tool-rental/new`): the moved pieces
  leave the source rental (its `qty_pending` falls) and a **new rental** is
  opened for the destination. `to_rental_id` points at that new rental, so the
  replay only *lifts* the pieces off the source bucket and does not open a
  bucket for them on the source.

Before this split, a partial hand-over from a line that was already split by an
earlier hand-over was counted twice (Steel Shuttering Prop showed 210 out
instead of 170, and the drift guard only trims the last bucket). Regression:
`tests/test_tool_rentals_transfers_populate.py`.

### Populate + verify (smoke)

`scripts/seed_tools_rentals_transfers.py` fills the demo DB (`hdc_instance/`,
git-ignored) through the real New Rental and Transfer Rental endpoints only:
8 rentals (4 new, 4 opened by transfers), 5 hand-over rows (one transfer from
two holders), no returns, no payments. It then checks the result against a
hand-computed expectation: per-tool owned, store + out, every place's book
quantity on **Tools > Audit**, and that every rental appears on **Tools >
Tracking**. Run it with `HDC_BOOTSTRAP_ADMIN_PASSWORD=... python3
scripts/seed_tools_rentals_transfers.py`; it exits non-zero if any check fails.

### Rent pending is apportioned, never duplicated

`total_pending_amount` belongs to a rental, but a rental can now sit at two
customers. The amount is shared across that rental's customer buckets **by qty**,
so the location table and the company total both stay right.

---

## 3b. Stock life-cycle: buying more and scrapping the rest

`total_quantity` is a balance, not a magic number. Two registers explain every
change to it, both on the Inventory page (and on each tool's position page):

| Event | Route | Effect | Audit |
| --- | --- | --- | --- |
| **Purchase / Add Stock** | `POST /hdc/tool-rental/inventory/<id>/purchase` (or `/purchase` with `tool_id` in the form) | `total_quantity += qty`, optionally updates the tool's unit cost | `hdc_tool_purchase` row (`PUR-TOOL-00001`, supplier, bill ref, date, cost) + a `purchase_in` movement log |
| **Scrap / Discard** | `POST /hdc/tool-rental/inventory/<id>/scrap` (or `/scrap`) | `total_quantity -= qty`, writes off `qty × unit_cost` | `hdc_tool_scrap` row (`SCRAP-00001`, reason, value) + a `scrap_out` movement log |
| Opening stock | part of **Add Tool** | same as a purchase, flagged `is_opening_stock` | so a newly added tool already explains its own qty |

Rules that keep the balance true:

- Scrapping is capped at what is **in the store** (`owned − rented out`); a
  rented-out piece must be returned first, otherwise `owned ≠ store + out`.
- Reasons are a fixed list (`damaged`, `lost`, `worn_out`, `obsolete`,
  `sold_as_scrap`, `other`) so the write-off report can be grouped and claimed.
- Hand-editing *Total Qty Owned* in the edit modal is still allowed but is
  logged as an `adjustment` movement, and the UI says so — real events belong in
  the purchase / scrap flows.
- Ledger rows and KPIs carry `purchased_qty`, `purchased_value`, `scrapped_qty`,
  `scrapped_value`; the JSON feed exposes them too.

Categories live on the same page: create one standalone (the tool form then
opens with it selected), create one inline while adding a tool
(`＋ Create new category…`), rename, or delete — deletion is refused while any
tool still uses the category.

Inventory search matches tool name, code, category, description **and the site or
customer a tool is currently sitting on**, because the list is built from the
same `tool_ledger()` the dashboard and tracking pages use — the pages can never
disagree about stock.

## 4. Schema change

New tables, each created idempotently by `_ensure_tool_rental_schema()` in
`hdc/core/schema.py` (`CREATE TABLE IF NOT EXISTS` + indexes), so existing
deployments heal on boot with no manual migration:

```sql
hdc_tool_rental_transfer_item (
    id, transfer_id -> hdc_tool_rental_transfer(id),
    rental_item_id -> hdc_tool_rental_item(id),
    tool_id -> hdc_tool(id),
    qty_transferred FLOAT, created_at DATETIME
)

hdc_tool_purchase (
    id, purchase_code UNIQUE, tool_id -> hdc_tool(id),
    purchase_date DATE, qty FLOAT, unit_cost FLOAT, total_cost FLOAT,
    supplier VARCHAR(150), reference VARCHAR(120), notes VARCHAR(300),
    is_opening_stock BOOLEAN, created_by -> hdc_user(id), created_at DATETIME
)

hdc_tool_scrap (
    id, scrap_code UNIQUE, tool_id -> hdc_tool(id),
    scrap_date DATE, qty FLOAT, reason VARCHAR(30),
    unit_cost FLOAT, value_written_off FLOAT,
    reference VARCHAR(120), notes VARCHAR(300),
    created_by -> hdc_user(id), created_at DATETIME
)

-- Tools > Audit: one count sheet per place, one line per tool
hdc_tool_audit (
    id, audit_code UNIQUE, audit_date DATE,
    loc_type VARCHAR(20), project_id -> hdc_project(id), stage_id -> hdc_stage(id),
    customer_name, location_label, counter_name, reference, notes,
    status VARCHAR(20),                        -- draft / counted / adjusted / void
    total_lines, counted_lines, discrepancy_lines, shortage_qty, overage_qty,
    damaged_qty, write_off_value, adjusted_lines, adjusted_at,
    void_reason, is_void, created_by -> hdc_user(id), created_at, updated_at
)

hdc_tool_audit_line (
    id, audit_id -> hdc_tool_audit(id), tool_id -> hdc_tool(id),
    tool_name, tool_code, unit,
    book_qty FLOAT,                            -- what the ledger said that day
    counted_qty FLOAT,                         -- NULL = not counted, 0 = counted as zero
    damaged_qty FLOAT, variance FLOAT,
    status VARCHAR(20),                        -- pending / match / short / extra / adjusted
    adjusted_qty FLOAT, adjust_reason VARCHAR(30), scrap_id -> hdc_tool_scrap(id),
    notes, created_at, updated_at, adjusted_at, adjusted_by -> hdc_user(id)
)
```

No existing column changed type or meaning. Nothing is deleted.

---

## 5. Service API (`hdc/services/tool_tracking.py`)

| Function | Returns |
| --- | --- |
| `tool_ledger()` | `{'tools': [...], 'locations': [...], 'totals': {...}, 'today': date}` — the single source of truth |
| `tools_reconciliation(ledger)` | the balance identity + `balanced` / `variance` / `unaccounted_rows` |
| `dashboard_summary(ledger)` | KPI totals, split bar segments, top locations, attention list (used by the JSON feed / audit views) |
| `tool_item_summary(term, category_id)` | the simple dashboard: item-wise owned / store / rented + unpaid customer rent |
| `customer_dues_by_tool()` | unpaid outside-customer rent per tool, split per line; internal pending reported separately |
| `tools_attention(ledger)` | ranked issues (danger → warning → info), each with a deep link |
| `tool_position(tool_id)` | one tool's row + its movement log |
| `location_summary(ledger)` | per site / customer / store holdings |
| `tools_universal_search(term)` | hits across tools, rentals, locations, movements |
| `allocate_transfer_qty(pairs, total)` | splits a transfer qty over rental lines without ever exceeding it |
| `record_transfer_items(transfer, alloc)` | persists the per-tool split |
| `inventory_rows(term, category_id, view)` | ledger rows for the Inventory page, filterable by name / code / **site** |

`hdc/services/tool_rental.py` adds the write side of the stock life-cycle:

| Function | Purpose |
| --- | --- |
| `record_tool_purchase(...)` | +qty, audit row, movement log; returns `(ok, message, purchase)` |
| `record_tool_scrap(...)` | −qty capped at in-store qty, audit row, movement log |
| `tool_stock_aggregates(tool_ids)` | purchased / scrapped totals in two grouped queries |
| `tool_purchases(...)`, `tool_scraps(...)` | the two registers, filterable by tool / date / text |

`hdc/services/tool_audit.py` is the physical-check side. Every number it shows
is read out of `tool_ledger()`, so a count can never disagree with the dashboard:

| Function | Returns |
| --- | --- |
| `location_key(...)` / `parse_location_key(key)` | a place as one comparable string (`store`, `site:<project_id>[:<stage_id>]`, `customer:<project_id>` / `customer:<name>`) and back |
| `book_position(ledger, spec)` | what the book has of *every tool* at one place, summed from the ledger holdings |
| `audit_locations(ledger, term)` | one card per countable place: book qty, pieces already counted, open sheet, variance (counted vs today's book) |
| `audit_matrix(...)` | the tool × place table — total owned, book / counted / diff per place, per-tool roll-up, `not verified` for places nobody has counted |
| `audit_summary(...)` | the KPI strip (owned, placed, counted, short, extra, written-off value, unbalanced) |
| `audit_history(limit, location_key_filter)` | recent sheets |
| `audit_sheet(audit \| spec)` | one sheet: expected lines first, then tools *not* expected at that place, plus the live movement for that place |
| `audit_movement(audit)` | the movement-log rows this sheet caused (`AUDIT-00007` in `notes`) |
| `pending_audit_for(spec)`, `start_audit(spec, ...)` | one open sheet per place — starting a second one resumes the first |
| `save_counts(audit, counts, ...)` | `{'counted': n, 'damaged': n, 'reason': ..., 'notes': ...}` per tool; blank means *not counted*, `0` means *counted as zero* |
| `plan_adjustments(audit)` | what the **Adjust** button would do, before doing it (qty writable, rent line to lift, value to write off) |
| `post_adjustments(audit, mode, reason, ...)` | applies it; `mode='losses'` (default) only removes shortages, `'both'` also adds findings back |
| `close_audit` / `reopen_audit` / `void_audit` | sign a sheet off, re-open it, or discard it (never reverses a posted adjustment) |
| `open_audit_count()`, `audit_rows_for_json(ledger)` | the nav badge and the `/hdc/api/tool-rental/audit` feed |

`lines_of(audit)` is the one helper to respect: freshly inserted lines are not
in the ORM relationship, so every total is recomputed from a query.

Location types: `store` / `own_project` / `customer` (`LOC_STORE`,
`LOC_OWN_PROJECT`, `LOC_CUSTOMER`). All read-only except the two transfer
helpers.

The JSON feed at `/hdc/api/tool-rental/dashboard` returns the audit numbers —
reconciliation, split, **all** locations (not a truncated top-N) and a per-tool
array — so external analysis cannot drift from the UI.  The simple dashboard
itself never repeats that detail; it is the one-page stock summary.
`/hdc/api/tool-rental/audit` does the same for the count: one row per place with
its book / counted / variance totals, served from the same functions the
screens use.

---

## 6. Navigation

`templates/hdc/tool_rental/_tools_nav.html` gives the section one sub-nav
(Dashboard · Rentals · Inventory · Tracking · **Track by Serial** · Reports ·
Audit) included by every tool page. The sidebar entry **HDC Tools** now opens
the dashboard; the rentals hub stays at `/hdc/tool-rental` and keeps the simple
numbers — the reconciliation identity itself is on Tracking and the one-tool
position page. Creating a rental has its own page (`/hdc/tool-rental/new`),
opened by the hub's **New Rental** action. The **Audit** tab carries a badge
with the number of count sheets still open, and is hidden entirely for a user
without permission on that page.

### Track by Serial — the individual-piece view

Tracking and Reports group by *tool type and quantity* (one compact row per
location or site). **Track by Serial** (`/hdc/tool-rental/serials`) is the
complement: it lists *every serial-numbered piece* — e.g. `Vibrator No 3` — with
its current location, status, the rental currently holding it, and the **full
movement chain** that got it there. It reads the same `hdc_tool_serial` /
`hdc_tool_serial_movement` markings the Inventory, Position and Rental-detail
pages maintain, so nothing is duplicated and the two views can never disagree.

- KPI strip: Pieces Tracked · In Warehouse · Out on Sites/Customers · Scrapped.
- Filters: tool (combo), status (in_store / rented / transferred / returned /
  maintenance / damaged / lost / scrapped), free-text search across serial,
  tool code, rental code and location, plus quick *Out only* / *In store only*
  toggles.
- Rows: serial piece, tool (link to its Position page), current location badge,
  status badge, holder rental (link to the rental), and the movement chain
  (`Warehouse / Store › Site A › Site B`).
- **View** opens a modal with the piece's full movement log (date, type, from,
  to, notes) — the same data the serial status dialog shows, centred on one
  piece.
- Service: `all_tool_serials_tracking(search, status, tool_id)` (re-syncs serial
  markings to live holdings first via `ensure_all_tools_serials`, then orders
  naturally and builds each row's chain with `_serial_chain_from_movements`);
  KPIs via `get_tool_serials_tracking_kpis`.

### Audit: what the book says, what the site says

`/hdc/tool-rental/audit` is the physical check, in four parts:

1. **Total qty vs where it is** — one card per place (warehouse, every own
   site, every outside customer holding something, plus live sites that hold
   *nothing*, because "you should have no tools here" is exactly what a count
   proves), each showing the pieces the book puts there; the tool × place matrix
   underneath repeats *Total Owned* on every row so nothing goes missing between
   columns.
2. **The manual count** — **New physical count** opens a sheet for one place
   (`/hdc/tool-rental/audit/<id>`). One row per tool the book expects there, in
   the same order, with the expected figure printed next to the box, plus rows
   for tools *not* expected at that place so a surprise can be typed in.
   `Save` is a draft: a blank box means *not counted*, a typed `0` means *none
   found*, and those two are never treated alike.
3. **Discrepancies** — `Diff = counted − book`, computed live while typing and
   again on the server; shortages in red, findings in blue, damage recorded
   separately, and *not verified* on the roll-up of any place nobody has
   counted. Filters: category, search, and **Only tools whose count disagrees
   with the book**.
4. **Adjust** — as soon as a counted sheet has anything to post, the sheet
   grows an *Adjust — what will happen* block above the form: each shortage
   printed with the pieces, the PKR it writes off, the rental line(s) they will
   be lifted off, and a warning when the book cannot absorb the whole loss; each
   overage with where it will be parked. Choose `Only the losses` (default) or
   `Losses and the overages`, pick the scrap reason and date, and the button
   stays disabled until `ADJUST` is typed.
   A loss goes through `record_tool_scrap` — reason, value, `AUDIT-00007` as the
   reference — after the pieces are taken off the rental line that was holding
   them, so the customer's line and the store stay consistent; an overage found
   at a site is added back to *Total Owned* and parked where it was found. Rent
   already billed is never touched, so recovering money from a customer still
   happens on that rental's own page. The identity `owned = store + sites +
   customers` is re-derived after every adjustment, and the sheet only closes
   once nothing is left to post — pieces still uncounted keep it open.

Access is its own page permission (`tool_audit` in the Access matrix, matching
`/hdc/tool-rental/audit*` plus its JSON feed): an administrator, manager or
accountant keeps the write side, a staff account can be given **read only**, and
a read-only user sees the numbers and the `Read only` badge but no form. Every
write route is additionally behind the Tools money-gate and a serialised SQLite
write lock, so two people counting the same site cannot interleave.


### Tracking and Reports: compact list, full details on demand

Tracking is grouped by the **current site, customer, or warehouse** rather than
repeating one row for every tool. Each row previews tool types and quantities
at that location; **View** opens a scrollable detail with every tool/rental
holding, its full current movement path, and the tool movement log. Selecting a
site filters to that current site, including partial transfers.

Reports keep one summary row per site or individual outside customer. **View**
opens the site's tool totals, rental line items, and complete movement paths.
The detailed rental register also stays compact: use its **View** action for the
full rental record. Customer groups are keyed by customer name, so unrelated
external customers are never combined into one project bucket.

---

## 7. Demo data

```bash
python3 scripts/seed_tools_demo.py     # idempotent
```

Seeds 11 tools (542 pieces) across 3 categories, 3 sites, 7 rentals: internal
no-charge site rentals, a site-to-site chain, external fee rentals, partial
returns, one overdue rental, one long-out rental, one damaged tool, one top-up
purchase and one scrap — enough for every page (dashboard, rentals, tracking,
reports) to show something real:

```
owned 542 = in store 160 + own projects 270 + customers 112   (balanced: True)
purchased 543 pcs (6,364,000 PKR)   scrapped 1 pcs (55,000 PKR)
```

---

## 8. Tests

```bash
HDC_BOOTSTRAP_ADMIN_PASSWORD='Admin@1234' python -m unittest tests.test_tool_audit -v
HDC_BOOTSTRAP_ADMIN_PASSWORD='Admin@1234' python -m unittest tests.test_tool_stock_lifecycle -v
```

Covered: all-in-store balance · own-project vs customer split · transfer moves
the current location and keeps the `Site A > Site B` chain · partial transfer
splits a line across two sites · `allocate_transfer_qty` never exceeds the
request · returns put stock back · stock edited below out-qty is flagged not
hidden · overdue and >30-day flags · location roll-up · universal search by
code / name / rental / customer · dashboard + position pages render · the
dashboard stays a simple item-wise summary (no repeated tracking/audit blocks) ·
its search/category filters narrow the item list · unpaid customer rent is split
per item while own-site money is excluded · JSON matches the service · void
rentals hold nothing ·
no-charge internal rentals still count as sent · rent pending is not double
counted across two customers · the pre-existing tool pages still render.

`tests/test_tool_stock_lifecycle.py` covers: opening-stock purchase on add ·
zero-qty tool needs no purchase · purchase raises owned qty, cost and audits it ·
purchase can keep the old unit cost · bad purchase input refused · scrap lowers
owned qty and writes the value off · scrap of rented-out qty refused · unknown
reason falls back to `other` · purchase → rent → return → scrap still balances ·
purchase / scrap through the UI routes · categories create / rename / delete ·
ledger, KPI and JSON carry the new numbers · inventory search by site ·
inventory + position + reports render the registers · and the wipe fix:

- `_WIPE_TARGETS['tools']` lists every tool table;
- wiping **Tools** clears all of them and leaves a genuinely empty section;
- wiping **Tools** voids the rent that was already posted to Accounts, so no
  orphan income survives in the ledger;
- wiping **Accounts** keeps the tool data and only drops the now-dead
  `hdc_tool_rental_account_txn` links and receiving-account references.

`tests/test_tool_audit.py` covers the count side: book position per place ·
matrix totals and `not verified` places · saving a partial sheet · a typed `0`
versus a blank box · a shortage adjusting through the scrap register and lifting
the rent line without touching billed rent · an overage added back to owned
stock · the default `losses` mode ignoring findings · `mode='both'` taking both ·
no second adjust on an adjusted sheet · a shortage in the store (no rent line to
lift) · closing a sheet while a line is uncounted · voiding · a loss the book
cannot absorb being capped · the routes rendering for an authorized user · the
UI count + adjust round trip behind the `ADJUST` confirmation · starting a sheet
from the location preview · JSON feed matching the service · a read-only user
being refused the write · and the wipe + boot-schema coverage.

`hdc_tool_audit_line` is wiped before `hdc_tool_audit` (it points at it), and
both sit ahead of `hdc_tool` / `hdc_project`, so a Tools wipe leaves no orphan
count sheet and no count sheet survives a Project wipe pointing at a deleted site.

---

## 9. Settings → Wipe now includes the Tools section

The Tools section used to survive a granular wipe, because `_WIPE_TARGETS` in
`hdc/core/admin.py` had no `tools` group. It now does, covering
`hdc_tool`, `hdc_tool_category`, `hdc_tool_purchase`, `hdc_tool_scrap`,
`hdc_tool_rental`, `hdc_tool_rental_item`, `hdc_tool_rental_return`,
`hdc_tool_rental_return_item`, `hdc_tool_rental_payment`,
`hdc_tool_rental_transfer`, `hdc_tool_rental_transfer_item`,
`hdc_tool_rental_account_txn`, `hdc_tool_audit`, `hdc_tool_audit_line`
and `hdc_tool_movement_log`, in dependency-safe
order.
