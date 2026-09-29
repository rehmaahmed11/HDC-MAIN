# HDC Tools — Position Dashboard & Complete Tool Tracking

Answers one question for every tool HDC owns: **where is it right now, and do the
numbers add up?**

```
Total Owned (Inventory)  =  In Store  +  Sent to Own Projects  +  Sent to Other Customers
```

Everything in the Tools section is built around that single identity. If it ever
fails to hold, the dashboard says so loudly instead of quietly showing a
plausible-looking wrong number.

- Dashboard: `/hdc/tool-rental/dashboard` (sidebar → **HDC Tools**)
- One tool: `/hdc/tool-rental/tool/<tool_id>`
- JSON feed: `/hdc/api/tool-rental/dashboard`
- Inventory (add tools, buy stock, scrap, categories): `/hdc/tool-rental/inventory`
- Logic: `hdc/services/tool_tracking.py`, `hdc/services/tool_rental.py`
- Pages: `templates/hdc/tool_rental/tool_dashboard.html`, `tool_position.html`,
  `tool_inventory.html`, `_stock_forms.html`
- Tests: `tests/test_tool_tracking.py` (19 cases),
  `tests/test_tool_stock_lifecycle.py` (22 cases)

---

## 1. What the dashboard shows

| Block | What it answers |
| --- | --- |
| KPI strip | Total owned · in store · **sent to own projects** · **sent to other customers** · total sent · rent pending |
| Reconciliation strip | `store + own sites + customers = accounted`, compared with `owned`, with a **Balanced / N tool(s) off** badge |
| Split bar | One glance at the proportion in store vs own sites vs customers |
| Needs Attention | Overdue rentals, tools out > 30 days, damaged/lost stock, tools that do not reconcile, idle tools with a rental rate, rent pending |
| Universal search | One box across tools, rental codes, customers, sites and movement notes — every hit says where the pieces are |
| Tool-by-tool table | Per tool: owned / store / own sites / customers, a mini split bar, the actual locations holding it, and a status badge |
| By location | Who is holding what: qty, tool types, rentals, overdue qty, oldest days, rent pending |

Filters: `view` (all / out / store / own / customer / attention),
`location_type`, `tool_id`, `category_id`, `project_id`, `issues=1`, `q`.
Every filter narrows the *tool list*; the reconciliation strip always reports the
true company-wide position so a filtered view can never be mistaken for the
whole picture.

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
data breaks it, the row is flagged `unaccounted` with the signed variance and
pushed into *Needs Attention*.

---

## 3. Quantity-accurate positions (event replay)

A rental line is not "at one place". It is replayed into **buckets**:

```
start     → bucket[origin] += qty_rented
transfer T → lift T pieces from the most recently touched bucket
             open bucket[destination] += T   (chain inherits + destination)
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
same `tool_ledger()` the dashboard uses — the two pages can never disagree.

## 4. Schema change

One new table, created idempotently by `_ensure_tool_rental_schema()` in
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
```

No existing column changed type or meaning. Nothing is deleted.

---

## 5. Service API (`hdc/services/tool_tracking.py`)

| Function | Returns |
| --- | --- |
| `tool_ledger()` | `{'tools': [...], 'locations': [...], 'totals': {...}, 'today': date}` — the single source of truth |
| `tools_reconciliation(ledger)` | the balance identity + `balanced` / `variance` / `unaccounted_rows` |
| `dashboard_summary(ledger)` | KPI totals, split bar segments, top locations, attention list |
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

Location types: `store` / `own_project` / `customer` (`LOC_STORE`,
`LOC_OWN_PROJECT`, `LOC_CUSTOMER`). All read-only except the two transfer
helpers.

The JSON feed at `/hdc/api/tool-rental/dashboard` returns the same numbers the
HTML shows — reconciliation, split, **all** locations (not a truncated top-N)
and a per-tool array — so external analysis cannot drift from the UI.

---

## 6. Navigation

`templates/hdc/tool_rental/_tools_nav.html` gives the section one sub-nav
(Dashboard · Rentals · Inventory · Tracking · Reports) included by every tool
page. The sidebar entry **HDC Tools** now opens the dashboard; the rentals hub
stays at `/hdc/tool-rental` and carries the same reconciliation numbers.

---

## 7. Demo data

```bash
python3 scripts/seed_tools_demo.py     # idempotent
```

Seeds 11 tools (542 pieces) across 3 categories, 3 sites, 7 rentals: internal
no-charge site rentals, a site-to-site chain, external fee rentals, partial
returns, one overdue rental, one long-out rental, one damaged tool, one top-up
purchase and one scrap — enough for every dashboard panel to show something
real:

```
owned 542 = in store 160 + own projects 270 + customers 112   (balanced: True)
purchased 543 pcs (6,364,000 PKR)   scrapped 1 pcs (55,000 PKR)
```

---

## 8. Tests

```bash
HDC_BOOTSTRAP_ADMIN_PASSWORD='Admin@1234' python -m unittest tests.test_tool_stock_lifecycle -v
```

Covered: all-in-store balance · own-project vs customer split · transfer moves
the current location and keeps the `Site A > Site B` chain · partial transfer
splits a line across two sites · `allocate_transfer_qty` never exceeds the
request · returns put stock back · stock edited below out-qty is flagged not
hidden · overdue and >30-day flags · location roll-up · universal search by
code / name / rental / customer · dashboard + position pages render · filters
narrow the list · JSON matches the service · void rentals hold nothing ·
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

---

## 9. Settings → Wipe now includes the Tools section

The Tools section used to survive a granular wipe, because `_WIPE_TARGETS` in
`hdc/core/admin.py` had no `tools` group. It now does, covering
`hdc_tool`, `hdc_tool_category`, `hdc_tool_purchase`, `hdc_tool_scrap`,
`hdc_tool_rental`, `hdc_tool_rental_item`, `hdc_tool_rental_return`,
`hdc_tool_rental_return_item`, `hdc_tool_rental_payment`,
`hdc_tool_rental_transfer`, `hdc_tool_rental_transfer_item`,
`hdc_tool_rental_account_txn` and `hdc_tool_movement_log`, in dependency-safe
order.
