# HDC Tool Transaction — Uniform Form Completion Report

Ahmed's request: one uniform transaction form in the **HDC Tools** section for every
equipment transaction, built on the existing routes/models/services — no new
inventory system, no new financial architecture, no duplicate workflows, no data
rewrites.

The form lives at **`/hdc/tool-rental/transaction`** ("HDC Tool Transaction",
sub-nav tab **New Transaction**). The existing *New Rental* page and every current
URL keep working; where a tab already had a transaction action, the uniform form
posts to the same endpoint that implements it.

The change is committed on the session branch `arena/30197002-hdc-main`
("HDC Tools: one uniform transaction form for every equipment transaction");
the changed files are listed in §2 and the test evidence in §6.

---

## 1. Files inspected (audit pass)

**Routes**
- `hdc/routes/tool_rental.py` — every HDC Tools route in one `register(app)`:
  `hdc_tool_rental_new` (`/hdc/tool-rental/new`), `hdc_tool_rental_create`
  (`/create`), `/<id>/return`, `/<id>/payment` (+void/receipt),
  `/<id>/discount` (+void), `/<id>/transfer`, `/inventory/scrap`, `/<id>/scrap`,
  the serial picker APIs, and the dashboard/inventory/tracking/report pages.
- `hdc/routes/__init__.py` — the module registration order.

**Models**
- `hdc/models/tool_rental.py` — `Tool`, `ToolCategory`, `ToolPurchase`,
  `ToolScrap`, `ToolRental`, `ToolRentalItem`, `ToolRentalReturn(Item)`,
  `ToolRentalPayment`, `ToolRentalDiscount`, `ToolRentalAccountTxn`,
  `ToolRentalTransfer(Item)`, `ToolSerial`, `ToolSerialMovement`,
  `ToolMovementLog`, `ToolAudit(.Line)`.
- `hdc/models/projects.py` — `Project`, `Stage` (sites the tools are issued to).
- `hdc/models/accounts.py` — `Account`, `AccountTransaction` (cash/bank posting).

**Services**
- `hdc/services/tool_rental.py` — `record_tool_purchase`, `record_tool_scrap`,
  `recalc_rental_totals`, `create_movement_log`, payment/discount posting,
  serial helpers (`ensure_tool_serials`, `get_available_serials`,
  `return_serials_from_rental`, `update_serial_status`, `create_serial_movement`,
  `rename_tool_serial`), `known_tool_customers`, store/scrap location labels.
- `hdc/services/tool_tracking.py` — `tool_ledger`, `record_transfer_items`,
  `allocate_transfer_qty`, `LOC_*`, `WAREHOUSE_LABEL`.
- `hdc/services/tool_txn_types.py` — the pre-existing transaction registry
  (`TOOL_TXN_TYPES`, `txn_type()`, `rental_eligible()`) that the New Rental page
  drives itself from.
- `hdc/services/tool_audit.py`, `hdc/services/timekeeping.py`
  (`_has_recent_duplicate`), `hdc/services/cashflow_register.py` (`ensure_party`),
  `hdc/services/permissions.py` / `record_permissions.py`,
  `hdc/services/transaction_entry.py`.

**Templates / front-end**
- `templates/hdc/shared/base.html`, `templates/hdc/shared/_combo_field.html`
  (combo/select macros the pickers reuse).
- `templates/hdc/tool_rental/tool_new_rental.html` (the existing multi-mode form
  and its inline controller script), `tool_rental.html`, `_tools_nav.html`,
  `_stock_forms.html`, and the dashboard/inventory/detail/tracking templates.
- `static/hdc/js/core/*` (combo, dialog, audit, forms, layout,
  responsive_tables) and the page-script convention
  `/hdc_static/js/pages/<name>.js`.

**Tests / scripts / docs**
- `tests/test_tool_txn_form.py`, `test_tool_new_rental_smoke.py`,
  `test_template_hygiene.py`, `test_frontend_config_contract.py`,
  `test_tool_tracking.py`, `test_tool_stock_lifecycle.py`,
  `test_tool_serial_*.py`, `test_tool_audit.py`, `test_name_combo_fields.py`,
  `test_user_permissions.py`, `tests/qa_support.py`.
- `scripts/check_layers.py`, `scripts/reorganize_frontend.py`,
  `scripts/check_db_safety.py`.
- `TOOLS_SECTION_DEEP_AUDIT.md`, `TOOLS_TRACKING_SYSTEM.md`,
  `TRANSACTION_ARCHITECTURE_AUDIT.md`, `NEW_TRANSACTION_REPORT.md` (report style).

---

## 2. Files changed

| File | Status | Size |
|------|--------|------|
| `hdc/routes/tool_rental.py` | modified | +303 / −15 |
| `hdc/services/tool_txn_types.py` | modified | +272 / −2 |
| `templates/hdc/tool_rental/_tools_nav.html` | modified | +6 |
| `scripts/reorganize_frontend.py` | modified | +1 / −1 (maps `tool_txn.html`) |
| `hdc/services/tool_txn_entry.py` | **new** | 786 lines |
| `templates/hdc/tool_rental/tool_txn.html` | **new** | 485 lines |
| `static/hdc/js/pages/tool_txn.js` | **new** | 879 lines |
| `tests/test_tool_txn_uniform.py` | **new** | 586 lines |

What the changes do:

- **`hdc/services/tool_txn_types.py`** — extended with the uniform registry:
  `UNIFORM_TXN_TYPES` (12 entries), `uniform_txn_type()`, the `KIND_*`/`PANEL_*`
  constants, and the shared reason lists. Pure data; the page script and the
  routes both read it, so the form and the server cannot drift apart.
- **`hdc/routes/tool_rental.py`** — the uniform page and its thin handlers:
  - `GET /hdc/tool-rental/transaction` (`hdc_tool_txn`) — builds the form
    context and the JSON config block; the same write gate the New Rental page
    uses (`may_access_path(...)` falling back to the `admin`/`accountant` roles).
  - `POST /hdc/tool-rental/transaction/damage-loss`,
    `POST /hdc/tool-rental/transaction/maintenance`,
    `POST /hdc/tool-rental/transaction/maintenance-return` — thin wrappers
    around the new service functions, with `@login_required`,
    `@_money_write_required()` and flash + redirect.
  - `_uniform_txn_type_context()` — resolves each registry entry to the URL it
    posts to (rental-bound entries carry `__RENTAL_ID__` for the page to fill).
  - `_uniform_note_text()` / `_uniform_billing_notes()` — stamp the form's
    reference + responsible person into the record the transaction creates, so
    the audit trail exists even if the page script never runs.
  - `_txn_reference_blocked()` — server-side duplicate-submission guard, wired
    into `/create`, `/<id>/return`, `/<id>/payment`, `/<id>/discount` and the
    scrap handlers. Posts from the older pages carry no reference and are
    unaffected.
  - `_uniform_return_url()` — wraps the redirect of those handlers, so a post
    that came from the uniform form always lands back on the uniform page with
    its success/error flash instead of being thrown onto the legacy page.
    Posts from the older pages (no `txn_source`) keep their existing
    destinations.
  - `/create` learned the uniform keys: `rent_out_customer` forces
    `renter_type=external`, `issue_own_site` forces `internal`, and
    `transfer_site`/`transfer_customer` route into the existing
    `_create_transfer_rental(force_renter_type=...)` so the destination type is
    unambiguous.
- **`templates/hdc/tool_rental/tool_txn.html`** — the page: one type selector,
  panels keyed by `data-txn-panels`, a server-rendered `<script id="toolTxnConfig"
  type="application/json">` data block, and `extra_js` wiring to the page script.
- **`static/hdc/js/pages/tool_txn.js`** — the controller: switches panels by the
  selected type, builds/removes tool lines, fills serial pickers from the config,
  loads outstanding return rows, inline-validates each line, confirms before
  submit, and locks the button while a post is in flight.
- **`templates/hdc/tool_rental/_tools_nav.html`** — the "New Transaction" tab
  pointing at the uniform page, without changing any existing tab or URL.
- **`scripts/reorganize_frontend.py`** — maps the new template into
  `DOMAIN_OF['tool_rental']` so the front-end layout check accepts it.

---

## 3. Per-type support (all 12 requested types)

Every type is on the one selector; the page shows only the panels that apply and
posts to the endpoint that already implements the transaction (or to the three
new thin handlers for the movements that had no route before).

| # | Type | Posts to | What happens |
|---|------|----------|--------------|
| 1 | Rent Out to Customer | `POST /hdc/tool-rental/create` (forced `external`) | Creates the rental + item lines through the existing handler; store → customer movement, serials marked out, billing per the terms panel. |
| 2 | Issue to Own Company Site | `POST /hdc/tool-rental/create` (forced `internal` + project/stage) | Records the actual destination site. No external revenue is raised. |
| 3 | Transfer Between Sites | `POST /hdc/tool-rental/create` (`transfer_site` → `_create_transfer_rental`) | Source holder cycles are settled and one new internal rental opens on the destination site. |
| 4 | Transfer Between Customers | same (`transfer_customer`) | Same transfer engine, destination type `customer`; only holders whose tools are actually out are offered — the existing transfer rules decide what can move. |
| 5 | Partial Return | `POST /hdc/tool-rental/<id>/return` (`partial`) | Only the posted quantities come back; the remainder stays outstanding. Optional settlement panel collects/allocates on the same post. |
| 6 | Full Return | `POST /hdc/tool-rental/<id>/return` (`full`) | Requires the whole outstanding balance of the lines; the physical issue closes. |
| 7 | Return to Store | `POST /hdc/tool-rental/<id>/return`, no settlement panel | Chosen pieces land back at `Warehouse / Store`; nothing is collected. |
| 8 | Record Rental Payment | `POST /hdc/tool-rental/<id>/payment` | Money in, posted to Accounts through the existing receipt poster. The tools do not move. |
| 9 | Record Credit / Outstanding Adjustment | `POST /hdc/tool-rental/<id>/discount` | Reduces what is owed, posts `discount_given` with no cash. Same authorization as the existing discount action. |
| 10 | Record Damage or Loss | `POST /hdc/tool-rental/transaction/damage-loss` | From the store: the existing scrap write-off. From a holder: the pieces are returned off the rental line and written off in the same database transaction, so owned and out move together. |
| 11 | Send for Maintenance | `POST /hdc/tool-rental/transaction/maintenance` | Pieces leave the available pool (serial status/condition → `maintenance`) but stay owned and store-side, with a workshop location and both serial + tool movement logs. |
| 12 | Return from Maintenance | `POST /hdc/tool-rental/transaction/maintenance-return` | Pieces come back to the store — or are written off through the scrap register when the operator marks them unrepairable. |

Common fields (transaction date, unique reference, responsible person, notes)
are on every variant; the tool-line editor (mark / inventory item, description,
quantity, rate + billing unit, condition/remarks, Add/Remove) covers the
bulk-quantity and per-serial cases; validation checks each line against the real
inventory before the post leaves the browser, and the server re-checks.

Duplicate simultaneous allocation of identified assets and over-issue of bulk
quantities are refused by the existing allocation logic (`allocate_transfer_qty`,
`ensure_tool_serials` reconciliation, availability checks in the scrap/damage
helpers) — the new page adds no parallel path that could bypass them.

---

## 4. Backend functions reused (no duplicate workflows)

| Area | Existing code the uniform form posts into |
|------|-------------------------------------------|
| Rent out / issue / transfer | The existing `/create` handler and `_create_transfer_rental()`; `ensure_all_tools_serials`, `ensure_tool_serials`, `_next_rental_code`, `create_movement_log`, `create_serial_movement`. |
| Returns | The existing `/<id>/return` handler and its `ToolRentalReturn(Item)` accounting; `return_serials_from_rental`, `update_serial_status`, `recalc_rental_totals`, the existing settlement posting. |
| Payments | `post_tool_rental_payment_to_accounts` / `void_tool_rental_payment_in_accounts`, `ensure_party`, `get_receiving_accounts` — the receipt is idempotent per payment through the existing `ToolRentalAccountTxn` link. |
| Credit / adjustment | `record_tool_rental_discount` + its Accounts posting and void path. |
| Damage / loss | `record_tool_scrap` (owned qty −, `ToolScrap` row, serials scrapped, `scrap_out` movement) — the new service only orchestrates the "from a holder" case around it. |
| Maintenance | `update_serial_status`, `create_serial_movement`, `create_movement_log`, `rename_tool_serial` (condition notes) — no new tables or parallel ledger. |
| Location truth | `tool_ledger`, `record_transfer_items`, `allocate_transfer_qty`, `LOC_*`/`WAREHOUSE_LABEL` — the page never writes locations itself; the tracking service continues to be the single source of truth. |
| Money parsing / dates | `_flt`, `_pkt_today`. |
| Access & CSRF | `@login_required`, `@_money_write_required()` (the `ACCESS_MATRIX` write gate), `may_access_path`, the app's `_csrf_token` session token. |
| Duplicate protection | Reference check built on the records' existing notes/reference fields; the same idea as `timekeeping._has_recent_duplicate` used elsewhere. |

No new inventory model, no parallel ledger, no second customer/tool/site/account
creation path: the form can only pick parties, tools and accounts that already
exist in the database.

---

## 5. Database changes

**None.** No new table, no new column, no index, no migration.

- The unique transaction reference is written into the notes/reference fields
  of the records the transaction already creates (`ToolRental.notes`,
  `ToolRentalReturn.notes`, `ToolRentalPayment.reference`/`notes`,
  `ToolRentalDiscount.notes`, `ToolScrap.reference`/`notes`), so the printed
  reference is recoverable and searchable without schema churn.
- Damage/loss and maintenance reuse `ToolScrap`, `ToolSerial`,
  `ToolSerialMovement` and `ToolMovementLog`. The two new movement markers
  (`maintenance_out` / `maintenance_in`) are string values written into the
  existing `ToolMovementLog.movement_type` (`db.String(30)`) and `ToolSerial`
  status/condition columns — data, not schema.
- Existing records are untouched; nothing is wiped, recreated or rewritten.

`scripts/check_db_safety.py` was run and reports **DB safety: OK — no database,
backup or archive files in Git** (427 tracked files).

---

## 6. Tests actually executed, and their results

All commands run from `/home/user/HDC-MAIN` in the project virtualenv.

| # | Command | Result |
|---|---------|--------|
| 1 | `HDC_BOOTSTRAP_ADMIN_PASSWORD='Admin@1234' .venv/bin/python -m unittest tests.test_tool_txn_uniform` | **Ran 28 tests in 43.4 s — OK** |
| 2 | `PYTHONPATH=tests HDC_BOOTSTRAP_ADMIN_PASSWORD='Admin@1234' .venv/bin/python -m unittest tests.test_tool_txn_form tests.test_tool_new_rental_smoke tests.test_template_hygiene` | **Ran 21 tests in 26.9 s — OK** |
| 3 | `PYTHONPATH=tests ... -m unittest tests.test_tool_rental_discount tests.test_tool_rentals_transfers_populate tests.test_tool_serial_markings tests.test_tool_serial_tracking tests.test_tool_stock_lifecycle tests.test_tool_tracking tests.test_tool_glance tests.test_tool_audit tests.test_frontend_config_contract tests.test_name_combo_fields` | **Ran 141 tests in 253.3 s — OK** |
| 4 | `.venv/bin/python -m compileall -q hdc` | clean |
| 5 | `.venv/bin/python scripts/check_layers.py` | 87 modules scanned — **LAYERS: OK** |
| 6 | `.venv/bin/python scripts/check_db_safety.py` | **DB safety: OK** |
| 7 | `node --check static/hdc/js/pages/tool_txn.js` | clean |
| 8 | `.venv/bin/python scripts/reorganize_frontend.py --check` | exits 1 on three templates that were **already unmapped before this change** (`_result_dialog.html`, `tool_serial_tracking.html`, `tool_serials_create.html` — verified absent from the mapping at HEAD); `tool_txn.html` is mapped now. |

The 28-test uniform suite (`tests/test_tool_txn_uniform.py`) covers:

- **Registry** — every required transaction is declared; every entry carries a
  complete controller contract; rental-bound types can only pick rentals (and
  nothing else does); money and physical kinds are disjoint; lookup by key.
- **Page** — all 12 types offered; the JSON config carries the panels, tools,
  serials and reference; the sub-nav/dashboard link exists; the legacy New
  Rental page and every existing Tools tab still render.
- **Behaviour** — rent out to customer; issue to own site (no external income);
  transfer between sites; transfer between customers; partial return (remainder
  stays outstanding); full return; return to store (no payment); rental payment
  (balance cleared, no tool movement); credit adjustment (balance reduced, no
  cash, no movement); damage/loss from store; damage/loss from a holder (ledger
  stays balanced); send + return from maintenance (ownership kept); over-return
  refused with nothing written; unavailable stock cannot be issued; duplicate
  submission posts once; duplicate return reference posts once; an empty form
  posts nothing; the reference generator never reuses a recorded number.

**Live end-to-end pass over real HTTP** (a real Werkzeug server on an isolated
temp database, driven with a cookie jar and CSRF tokens scraped from the pages —
not the Flask test client):

```
login -> HTTP 200

GET /hdc/tool-rental/transaction -> HTTP 200, 84472 bytes
  types: rent_out_customer, issue_own_site, transfer_site, transfer_customer,
         partial_return, full_return, return_to_store, rental_payment,
         credit_adjustment, damage_loss, maintenance_out, maintenance_in
  receiving accounts on the payment panel: ['1', '3']

POST /create  (Rent Out to Customer) -> HTTP 200, landed on /hdc/tool-rental/transaction
    success: Rental RENT-00001 created — 2 tools shown in the list below.

POST /<id>/payment  (Record Rental Payment) -> HTTP 200, landed on /hdc/tool-rental/transaction
    success: Payment 3000.00 PKR received in Company Cash (Cash/Bank). Pending: 0.00
POST the SAME reference again (duplicate guard) -> HTTP 200
    warning: Transaction HDC-TX-LIVE-0002 was already recorded. This submission
             was ignored, so nothing was posted twice.
POST /transaction/damage-loss  (Record Damage or Loss) -> HTTP 200
    success: 1 pcs of Live Vibrator written off (Damaged beyond repair). 5 still owned.
```

The database after that run: tool owned quantity 6 → **5**; rental line still
`rented 2 / pending 2` (the payment moved no tool); **1** payment row (the
duplicate posted nothing); 1 scrap row; movements `rental_out 2`, `scrap_out 1`;
serial markings `rented, rented, scrapped, in_store, in_store, in_store`.

An earlier GET smoke pass also fetched `/hdc/tool-rental/dashboard`,
`/hdc/tool-rental` and `/hdc/tool-rental/new` — all HTTP 200; the embedded
config parses with the 12 type keys in the required order; nothing renders after
`</html>`.

**Honest history:** the first run of the new suite was 21/28. The seven failures
were three test-harness bugs (a zero rate on the fixture rentals, and posting
non-create types to `/create` instead of their own endpoints) plus two real
route gaps (the uniform `transfer_*` keys were not recognised, and the reference
was not stamped server-side). The routes were fixed and the suite has been green
at 28/28 on every run since.

All commands listed above are the ones that were actually executed; results are
quoted verbatim from the runs.

---

## 7. Unsupported types and why

**None.** All twelve requested transaction types are supported through the one
form, and all of them were exercised by the acceptance suite (§6).

Deliberate limitations, so nothing is a surprise later:

- **The reference is not a new database column** — it rides on existing note /
  reference fields (§5). That was chosen to honour the "no unnecessary
  migrations" constraint; a future dedicated `txn_reference` column would be a
  pure migration with no behaviour change.
- **Transfer Between Customers inherits the existing transfer rules.** Only
  holders whose tools are actually out are offered, exactly as the existing
  transfer page behaves; the uniform form does not widen or override those
  rules.
- **Damage/loss write-off cannot be undone from the uniform page** — the same
  as the existing scrap register. Corrections go through the existing tools.
- **Maintenance has no new table.** It is recorded with the existing serial
  status/condition plus movement logs; a piece under repair stays owned and
  shows in the ledger flagged with its condition.
- **Everything stays behind the existing access rules.** The page and every post
  require login and write access to the Tools section (admin/accountant by
  default, or whatever the permission matrix grants); no credential is hardcoded
  anywhere in the feature.

---

## Appendix — request contract the page script relies on

- The page posts `txn_source=uniform`, one generated `txn_reference`, the
  selected `txn_type`, and the panel fields described in §3.
- Rental-bound types fill `__RENTAL_ID__` inside the action URL at submit time
  (return / payment / discount, and the damage-from-holder case).
- Field names used by the script: lines `tool_id[]`, `qty[]`, `rate[]`,
  `row_serial_ids[]`, `item_notes[]`; transfer `source_rental_id[]`,
  `transfer_item_id[]`, `transfer_qty_<itemId>`, `transfer_rate_<itemId>`,
  `transfer_serial_ids_<itemId>[]`; return `rental_item_id[]`,
  `qty_returned[]`, `condition_notes[]`, `return_serial_ids_<itemId>[]`;
  damage/maintenance `damage_serial_ids[]` / `maintenance_serial_ids[]`.
- Return rows are loaded from `GET /hdc/api/tool-rental/rental/<id>/pending-items`.
- Every post is confirmed in the browser, the submit button is locked while the
  request is in flight, and the server refuses any reference it has already
  recorded — so a double click, a resend, or a back-and-resubmit cannot move
  tools or money twice.
