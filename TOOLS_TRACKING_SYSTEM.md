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
- Logic: `hdc/services/tool_tracking.py`
- Pages: `templates/hdc/tool_rental/tool_dashboard.html`, `tool_position.html`
- Tests: `tests/test_tool_tracking.py` (19 cases)

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

Seeds 11 tools (533 pieces) across 3 categories, 3 sites, 7 rentals: internal
no-charge site rentals, a site-to-site chain, external fee rentals, partial
returns, one overdue rental, one long-out rental and one damaged tool — enough
for every dashboard panel to show something real:

```
owned 533 = in store 151 + own projects 270 + customers 112   (balanced: True)
```

---

## 8. Tests

```bash
HDC_BOOTSTRAP_ADMIN_PASSWORD='Admin@1234' python -m unittest tests.test_tool_tracking -v
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
