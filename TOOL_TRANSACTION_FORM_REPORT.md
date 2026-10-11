# HDC Tool Transaction — one page, three groups

The **HDC Tools** section records every equipment transaction on one page:

**`/hdc/tool-rental/transaction`** — *HDC Tool Transaction*, sub-nav tab
**New Transaction**.

The page offers exactly three groups, and every transaction lives under one of
them:

| Group | Transactions |
| --- | --- |
| **Rent Out** | Rent Out to Customer · Issue to Own Site · Transfer to Another Site · Transfer to Another Customer · Receive Rent Payment · Credit / Waive-off |
| **Return** | Partial Return · Full Return · Return to Store (no payment) · Damage or Loss (write off) |
| **Maintenance** | Send for Maintenance · Back from Maintenance |

That is the whole menu. The older *New Rental* page (`/hdc/tool-rental/new`) and
every existing URL keep working; where a tab already had a transaction action,
this page posts to the same endpoint that implements it.

---

## 1. What each transaction does

| Group | Transaction | Posts to | What happens |
| --- | --- | --- | --- |
| Rent Out | Rent Out to Customer | `POST /hdc/tool-rental/create` (forced `external`) | Rental + item lines through the existing handler; store → customer, serials marked out, billing per the terms panel. |
| Rent Out | Issue to Own Site | `POST /hdc/tool-rental/create` (forced `internal` + project/stage) | Records the destination site. No outside rental income is raised. |
| Rent Out | Transfer to Another Site | `POST /hdc/tool-rental/create` (`transfer_site`) | Source holder cycles are settled and one new internal rental opens on the destination site. |
| Rent Out | Transfer to Another Customer | same (`transfer_customer`) | Same transfer engine, destination `customer`; only holders whose tools are actually out are offered. |
| Rent Out | Receive Rent Payment | `POST /hdc/tool-rental/<id>/payment` | Money in, posted to Accounts through the existing receipt poster. No tool moves. |
| Rent Out | Credit / Waive-off | `POST /hdc/tool-rental/<id>/discount` | Reduces what is owed, posts `discount_given` with no cash. Same authorization as the rental's own discount action. |
| Return | Partial Return | `POST /hdc/tool-rental/<id>/return` (`partial`) | Only the posted quantities come back; the rest stays outstanding. Optional collection panel on the same post. |
| Return | Full Return | `POST /hdc/tool-rental/<id>/return` (`full`) | Every outstanding line comes back; the physical issue closes. |
| Return | Return to Store (no payment) | `POST /hdc/tool-rental/<id>/return`, no collection panel | Chosen pieces land back at `Warehouse / Store`; nothing is collected. |
| Return | Damage or Loss | `POST /hdc/tool-rental/transaction/damage-loss` | From the store: the existing scrap write-off. From a holder: pieces are returned off the rental line and written off in the same database transaction, so owned and out move together. |
| Maintenance | Send for Maintenance | `POST /hdc/tool-rental/transaction/maintenance` | Pieces leave the available pool (serial status → `maintenance`) but stay owned and store-side, with a workshop location and movement history. |
| Maintenance | Back from Maintenance | `POST /hdc/tool-rental/transaction/maintenance-return` | Pieces come back to the store — or are written off through the scrap register when the operator marks them unrepairable. |

**Billing and source sheet.** Billing choices and their explicit cash status are
served from `BILLING_MODES` in `tool_txn_types.py`. Choosing **No charge** shows
"no rent due" immediately, zeros and locks new rates, and hides collection
fields when returning a no-charge rental. The saved rental and return also say
No charge / No payment; the payment endpoint still refuses a free rental.
Transfer now picks one real site/customer location, expands its live holdings
into one tickable row per rental, and shows each tool as a spreadsheet-style row
with available quantity, move quantity, new rate and exact serials. Returns
show the selected rental's tools in the same row format. Both sheets use the
server's real rental/ledger data; quantity limits are rechecked on posting.
For a transfer, the old holder's outstanding balance and free status are visible
before settlement. The new page defaults to **leave on credit**, never silently
collects cash, and requires a chosen receiving account to collect now (older
forms keep their existing behavior).

Common fields (transaction date, one unique reference, responsible person,
notes) are on every transaction. Duplicate submissions are refused twice: the
page locks its submit button and the server refuses a `txn_reference` it has
already recorded, so a double click or a browser resend cannot post twice.

---

## 2. Data-driven — the registry is the whole contract

`hdc/services/tool_txn_types.py` holds one table, `TXN_GROUPS`: the three groups
(labels, icons, help) and, for every transaction, the endpoint it posts to, the
panels the page shows, the checks the page script runs, which rentals it may
pick from, and how its submit button reads.

The template (`templates/hdc/tool_rental/tool_txn.html`) loops over that table
to render the three tabs, the dropdown (one `<optgroup>` per group) and the
submit label. The page script (`static/hdc/js/pages/tool_txn.js`) reads the same
table from the page config and:

* shows the panels the chosen transaction declares, and **disables** everything
  else, so a hidden panel can never leak into a submit;
* runs exactly the checks the transaction names (`checks: ('rental',
  'payment')` → the `checkRental` + `checkPayment` functions), and collects
  exactly the fields those checks imply — there is no per-transaction branch in
  the script;
* finds its rental picker by the `data-rental-picker` name the registry gave it,
  never by a hard-coded DOM id.

Adding a transaction is therefore adding one data entry (plus its panel markup)
— no new page, no new dropdown, no new `if` in the script. A test asserts the
script contains none of the transaction keys, so this cannot rot.

The registry checks itself: `registry_problems()` returns every inconsistency
(unknown kind, unknown panel, missing label, money/physical mismatch, …) as a
sentence, and a test asserts the list is empty.

---

## 3. Clean-up in this pass

* Twelve flat dropdown entries became **three groups with their transactions
  nested** — Rent Out, Return, Maintenance.
* `UNIFORM_TXN_TYPES` / `uniform_txn_type()` are gone; the transaction page's
  table is `TXN_GROUPS` / `TXN_OPTIONS` / `txn_option()`, and the older New
  Rental page's four modes are clearly named `NEW_RENTAL_TXN_TYPES`.
* The page script's per-transaction `if` chain is gone (checks and collectors
  are looked up by name), as is the special-casing that showed the destination
  panels: a block now says whose it is (`data-renter-type`).
* Removed: the unused `PANEL_TRANSFER_DESTINATION` (no template block ever
  declared it), the unused `settlement` flag on options, the unused
  `DAMAGE_REASON_KEYS`, the unused `pending_field` argument of
  `rental_picker_rows()`, the separate `return_rental_options` /
  `payment_rental_options` / `adjustment_rental_options` context keys (the
  pickers are now built from the registry), and the helper/`txn_source` names
  that still said "uniform".
* `scripts/reorganize_frontend.py` — the three template files that were missing
  from its domain map (`_result_dialog.html`, `tool_serial_tracking.html`,
  `tool_serials_create.html`) are mapped, so its CI `--check` passes again.
* The four JSON serial endpoints (`/hdc/api/tool-rental/serials/…`) were guarded
  with `@_money_write_required()` while `tests/test_money_permissions.py`
  requires the decorator's documented JSON contract for an API route
  (`api=True` → `403 {"ok": false}`, not an HTML redirect).  They now pass
  `api=True`, which clears the twenty pre-existing failures in that test.

---

## 4. Files

| File | Change |
| --- | --- |
| `hdc/services/tool_txn_types.py` | The grouped, self-checking registry; `NEW_RENTAL_TXN_TYPES` for the old page. |
| `hdc/services/tool_txn_entry.py` | Form context builds one picker list per eligibility rule from the registry. |
| `hdc/routes/tool_rental.py` | `_txn_group_context()` / `_txn_page_config()`; the three thin POST handlers unchanged. |
| `templates/hdc/tool_rental/tool_txn.html` | Three group tabs + `<optgroup>` dropdown from data; legacy panels deleted. |
| `static/hdc/js/pages/tool_txn.js` | Group tabs; checks/collectors by name; picker by data attribute. |
| `templates/hdc/tool_rental/tool_new_rental.html` | Its registry const renamed to match `NEW_RENTAL_TXN_TYPES`. |
| `tests/test_tool_txn_page.py` | Registry, page and per-transaction cases for the grouped page, plus the data-driven guard. |
| `tests/test_tool_txn_form.py` | Imports the renamed New Rental registry. |
| `scripts/reorganize_frontend.py` | The three unmapped templates. |
| `hdc/routes/tool_rental.py` (serial API guards) | `@_money_write_required(api=True)` on the four JSON serial endpoints. |

Tests: `python -m unittest tests.test_tool_txn_page tests.test_tool_txn_form -v`
(the full suite is run by CI: `python -m unittest discover -s tests -p 'test_*.py'`).
