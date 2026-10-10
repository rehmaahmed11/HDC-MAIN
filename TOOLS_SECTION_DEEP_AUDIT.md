# HDC Tools — Deep Section Audit & Serial Integration

**Scope:** the entire Tools (Tool Rental) module after the serial-tracking
work, including the new **Track by Serial** tab requested in this pass.
**Goal:** confirm the module reconciles, document where serial-level and
type-level views live, and record risks found while reading the code.

---

## 1. Module map

| Layer | Files |
| --- | --- |
| Routes | `hdc/routes/tool_rental.py` (~3,450 lines, all under `register(app)`) |
| Services | `hdc/services/tool_rental.py`, `tool_tracking.py`, `tool_audit.py` |
| Models | `hdc/models/tool_rental.py` (`Tool`, `ToolRental` + items/returns/payments/
  transfers, `ToolSerial`, `ToolSerialMovement`, `ToolPurchase`, `ToolScrap`,
  `ToolCategory`, `ToolAudit`/`ToolAuditLine`, `ToolMovementLog`) |
| Templates | `templates/hdc/tool_rental/*.html` (dashboard, inventory, rentals,
  new-rental, position, tracking, **serial_tracking**, reports, audit, …) |
| Tests | `tests/test_tool_tracking.py`, `test_tool_stock_lifecycle.py`,
  `test_tool_audit.py`, `test_tool_serial_markings.py`,
  `test_tool_new_rental_smoke.py`, `test_tool_rental_discount.py`,
  **`test_tool_serial_tracking.py`** (new) |
| Docs | `TOOLS_TRACKING_SYSTEM.md` (updated), this file |

The module is registered as one big `register(app)` block. Every mutating
route is wrapped in `@login_required` **and** `@_money_write_required()` (a
finance-role gate + a serialised SQLite write lock), so two people cannot
interleave money writes.

---

## 2. Core invariant

```
Total Owned (Tool.total_quantity)
    = In Warehouse  (owned − rented out)
    + Sent to own projects
    + Sent to other customers
```

This is enforced per tool and surfaced as `balanced: True/False` on Tracking
and the position page. Bad legacy data is flagged `unaccounted` with a signed
variance rather than silently averaged.

**Serial markings** carry a *second*, finer invariant:

```
for every open rental item:  #serials attached to that rental
    == item.qty_pending
```

It is maintained by `ensure_tool_serials()` in `hdc/services/tool_rental.py`
(lines ~1301–1400). That function is the single source of truth that keeps
`hdc_tool_serial.is_in_store` / `current_rental_id` / `current_location_label`
reconciled with the live rental holdings. **This is the most important thing to
understand about the section:** serial state is *derived* from quantities, not
stored independently.

Consequence (verified by test): calling `return_serials_from_rental()` on a
serial *without* also reducing the rental's `qty_pending` will have that piece
re-attached to the rental on the next sync — because the books still say it is
out. The canonical "return" path is therefore the real return route
(`/hdc/tool-rental/<id>/return`), which records a `ToolRentalReturn`, drops
`qty_pending`, and *then* returns the serials. Treat the bare service as an
internal helper, not an entry point.

---

## 3. Section-by-section review

### 3.1 Dashboard — `/hdc/tool-rental/dashboard`
One simple page: per-tool owned / in-store / rented-out split, plus **rent not
paid by customers** (unpaid balance of outside-customer rentals, apportioned
across a rental's customer buckets by qty; own-site no-charge rentals are
excluded). Deliberately does **not** repeat tracking/audit detail. A JSON feed
(`/hdc/api/tool-rental/dashboard`) exposes the same numbers so external
analysis cannot drift. **Healthy.**

### 3.2 Inventory — `/hdc/tool-rental/inventory`
Add tool, purchase (stock-in), scrap (write-off), categories. Adding a tool or
purchasing auto-creates human-readable serial markings (`Shovel No 5 … No N`)
via `create_tool_serials` (`serial_start_no` / `custom_serials` supported).
Scrap refuses to throw away a rented-out piece (`record_tool_scrap` checks
`tool_available_for_integrity`) and can scrap *specific* serials by id
(`scrap_serial_ids[]`). KPIs carry `purchased_qty/value` and
`scrapped_qty/value`. **Healthy; serial-aware.**

### 3.3 Rentals — `/hdc/tool-rental` (+ `/new`, `/<id>`)
Create (own-site vs external, per-day vs fixed-fee vs no-charge), return
(full/partial with optional per-serial selection), payment (cash into a
Cash/Bank/Company account, optional discount), discount (voidable), transfer
(per-tool split, "All" + "Move everything pending", chain named explicitly).
Money posts to the unified ledger; a voided payment/rental reverses the link
(`hdc_tool_rental_account_txn`). **Healthy.**

### 3.4 Tracking — `/hdc/tool-rental/tracking` (type-level)
Grouped by *current location* (warehouse / own project / customer), one compact
row per place; **View** opens the tool types, rental holdings, movement chains
and movement log. **Unchanged by this pass — the new tab is additive.**

### 3.5 **Track by Serial** — `/hdc/tool-rental/serials` (NEW)
The individual-piece complement to Tracking/Reports. One row per
`hdc_tool_serial` with current location, status, holder rental, and the **full
movement chain** (`Warehouse / Store › Site A › Site B`), plus a per-piece
modal showing the complete `hdc_tool_serial_movement` log. Filters: tool,
status, free-text, *Out only* / *In store only*. Backed by
`all_tool_serials_tracking()` (re-syncs markings, orders naturally, builds the
chain via `_serial_chain_from_movements`) and `get_tool_serials_tracking_kpis`.
**Verified by `test_tool_serial_tracking.py`.**

### 3.6 Reports — `/hdc/tool-rental/reports`
One summary row per site/customer with purchases, scraps, rentals, pending
amount, and a detail modal (tool totals + movement paths). **Unchanged — left
type-level on purpose.**

### 3.7 Audit — `/hdc/tool-rental/audit`
Physical count: book position per place, the count sheet, live discrepancies,
and an **Adjust** block that writes shortages through `record_tool_scrap` and
adds overages back to owned stock — re-deriving the balance after every
posting. Access is its own page permission (`tool_audit`). **Healthy.**

---

## 4. Permissions & money gate

- `_money_write_required()` blocks writes for non-finance roles and serialises
  SQLite writes. Admin/accountant/manager pass.
- Section nav: Dashboard/Rentals/Inventory for everyone; **Tracking / Track by
  Serial / Reports** are admin-only in the sub-nav; **Audit** shows for admin,
  accountant, or any role explicitly granted `tool_audit` read.
- Observation: the *route* for Track by Serial is `@login_required` only, so a
  non-admin who knows the URL can technically view it (no page-level deny,
  because the path has no explicit access rule → `may_access_path` returns
  `None` → allowed). This matches Tracking/Reports' existing pattern, but if
  serial visibility should be tighter, add a `tool_serials` rule. Low risk.

---

## 5. Risks / gaps found

1. **Serial state is derived, not authoritative.** `ensure_tool_serials` can
   *rewrite* `is_in_store`/`current_rental_id` on every sync. Any code that
   mutates a serial's location outside the rental lifecycle must also adjust the
   underlying quantity, or the next sync will undo it. The bare
   `return_serials_from_rental` / `transfer_serials` helpers are safe *only*
   when paired with the matching quantity change. Mitigation already in place:
   the canonical routes do both together. (Documented in §2.)
2. **GET-time sync.** `all_tool_serials_tracking()` calls
   `ensure_all_tools_serials(tools=None, commit=False)`, which `flush()`es on a
   read GET. Harmless (rolled back at request end) but means a very large tool
   table recomputes the whole serial sync on every page load. For a few hundred
   tools this is fine; at warehouse scale consider syncing only the filtered
   tool set.
3. **Nav/route permission asymmetry** (§4) — cosmetic, noted.
4. **No index on `hdc_tool_serial.current_location_label`** — the serial page
   never queries by that column, so fine; but any future "all pieces at place X"
   query should add one.

---

## 6. Test coverage (this pass)

New `tests/test_tool_serial_tracking.py` (8 cases) boots an isolated app,
creates brand-new entries in **every** tool section — Inventory add, Purchase,
Rental, Transfer, Return, Payment, Scrap, Audit — and asserts the Track-by-
Serial page reflects each at the individual-piece level (current location,
status, movement chain). It also covers filters/search and the empty-state.

Full tool suite after this pass: **115 existing** + **8 new** tests, all green.

---

## 7. How to run

```bash
# from a venv with requirements installed
HDC_BOOTSTRAP_ADMIN_PASSWORD='Admin@1234' \
    python -m unittest tests.test_tool_serial_tracking -v
```
