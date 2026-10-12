# HDC Tools & Tool-Rental Section — CRUD Availability, Health & Flow Audit

**Repo:** `rehmaahmed11/HDC-MAIN` @ `d86b1fc` (branch `arena/abe089b5-hdc-main`)
**Date:** 2026-10-12
**Scope:** everything under `/hdc/tool-rental*` and `/hdc/api/tool-rental*` — routes (`hdc/routes/tool_rental.py`, 4,365 lines), services (`tool_rental.py`, `tool_tracking.py`, `tool_audit.py`, `tool_txn_entry.py`, `tool_txn_types.py`), models (`hdc/models/tool_rental.py`, 18 tables), templates (`templates/hdc/tool_rental/`, 17 files / 7,367 lines), page JS (`static/hdc/js/pages/tool_audit.js`, `tool_txn.js`), permissions (`hdc/services/permissions.py`, `hdc/extensions.py`).
**Method:** static read of every route/service/template **plus** five live probe harnesses run against a throwaway SQLite DB with a real `app.test_client()` and real hashed-password logins (route walk, CRUD chain walk, permission matrix, role/scale matrix, archive+wipe side-effects, flow-map resolution). Findings are tagged **[LIVE]** (reproduced by a probe) or **[STATIC]** (code reading only).

---

## 0. Verdict

| Area | Grade | One-line reason |
|---|---|---|
| CRUD completeness | **B−** | Create/Read/Update are complete and well-guarded for rentals, tools, payments, discounts, audits. **Void/cancel is missing for 6 of 9 money-or-stock-moving entities**, and tool archive is one-way. |
| Flow continuity | **A−** | 17 pages render, **48/48 forms** and **256/256 internal links** resolve to real rules; txn-type registry self-check is clean. Breaks are at the *permission/nav* layer, not the link layer. |
| Permission gating | **B** | Write gate is correct and genuinely enforced (blocked roles create **zero rows**). But nav gating is role-based and does **not** match route gating → 403 dead-ends; scoped managers see silently wrong numbers. |
| Data integrity | **C** | 8 live-confirmed ways to desynchronise stock, serials, billing or the ledger (overpayment, archive, audit gain, serial manager, transfer dup, GET-writes, dead mutating APIs, edit-wipes-category). |
| Money / Accounts | **B+** | The payment → ledger path is correct and idempotent (`tool_rental_payment:direct`, one txn, right account). Overpayment is unguarded; admin wipe leaves discount concessions live in the ledger. |
| Performance / scale | **D** | **No pagination anywhere in the section**; 2,400 serials → 12.8 MB page, 3,087 queries. N+1 in `all_tool_serials_tracking`; `ensure_all_tools_serials` runs on every inventory/new/tracking GET. |
| Test coverage | **C+** | 217 tool tests, real assertions, but **13 endpoints are never hit by any test** (incl. `tool_edit`, `tool_delete`, `payment_void`, `audit_close/reopen/void`, serial-create page+API) and 9 API endpoints have no caller at all. |

**Bottom line:** the happy path is solid and unusually well documented — a full create → serial → rent → transfer → return → pay → discount → audit chain runs end-to-end and lands correctly in Accounts. The risk is concentrated in (1) **reversal**: most stock/money events cannot be undone, so operators "fix" them with edits that break invariants; (2) **read paths that write**; (3) **unbounded pages**; (4) **permission/nav asymmetry** that produces 403 dead-ends and mis-scoped dashboards that still report `reconciled: true`.

## 0.1 Fix status on this branch — everything below is implemented

All P0/P1/P2 items from §9 are code-complete on `arena/abe089b5-hdc-main`, each with a
regression test in the new `tests/test_tool_crud_gaps.py` (32 cases).

| Finding | Status | What changed | Proven by |
|---|---|---|---|
| **T-06** overpayment | **fixed** | `payment` and `return` refuse any amount above `earned − paid − discount`; nothing reaches the ledger | `test_payment_above_outstanding_is_refused`, `test_return_payment_above_outstanding_is_refused` |
| **T-03** edit wipes category / impossible qty | **fixed** | absent `category_id` key = keep, explicit empty = "No Category"; owned qty may not drop below pieces out (rented **or** marked out) | `test_tool_edit_without_category_field_keeps_category`, `test_tool_edit_refuses_qty_below_pieces_out` |
| **T-11** incomplete wipe | **fixed** | `hdc_tool_rental_discount` added to the wipe target; discount ledger rows voided with it | `test_wipe_clears_discount_rows_and_voids_their_ledger_entries` |
| **T-05** transfer dup / no-op | **fixed** | same-destination transfer refused; an identical date+destination+qty transfer is treated as a double-submit | `test_transfer_to_the_same_place_is_refused`, `test_duplicate_transfer_submit_is_refused` |
| **T-15** dead mutating APIs | **fixed** | `serials/assign` and `serials/return` removed (404, no rule); `serials/create` now reconciles owned qty | `test_unused_mutating_serial_apis_are_gone`, `test_serial_api_reconciles_owned_qty` |
| **T-01** no rental void | **fixed** | `void_tool_rental` / `restore_tool_rental` + Cancel/Reinstate card on the rental page; refused while a payment is live | `test_rental_void_refuses_while_money_is_live`, `test_rental_void_returns_stock_and_restore_puts_it_back`, `test_void_requires_a_reason_through_the_form` |
| **T-02** archive breaks tie-out | **fixed** | one archive rule for KPIs *and* registers, archived panel + un-archive on Reports, `include_archived` on the registers, archive blocked while pieces are out | `test_archived_tool_leaves_kpis_and_registers_together`, `test_archived_tool_is_listed_on_reports_and_can_be_restored`, `test_archive_is_blocked_while_pieces_are_out` |
| **T-04** serials without a purchase | **fixed** | `reconcile_tool_serial_stock` writes a real stock-in row (never an `adjusted_qty`), so `owned == purchased − scrapped` holds | `test_serial_manager_reconciles_owned_qty_with_markings`, `test_serial_api_reconciles_owned_qty` |
| **T-07** audit gain breaks the invariant | **fixed** | an overage posts a `ToolPurchase` referencing the count and generates its markings | `test_audit_gain_keeps_owned_equal_purchased_minus_scrapped` |
| **T-08** close claims a clean count | **fixed** | `close_audit` names the unposted variance (short/extra) and writes it to the sheet notes | `test_close_audit_reports_the_discrepancy_it_leaves_unposted`, `test_close_audit_still_says_the_count_matched_when_it_did` |
| **T-09** reopen/void leave stock moved | **fixed** | `reverse_audit_adjustments` unwinds write-offs and overages; `void_audit(reverse=)` / `reopen_audit(reverse=)` expose it in the UI | `test_void_audit_with_reverse_puts_stock_back`, `test_reopen_audit_with_reverse_undoes_an_overage`, `test_void_without_reverse_says_the_stock_stays_moved` |
| **T-14** GETs write | **fixed** | `ensure_all_tools_serials` / `recalc_rental_totals` removed from every read path; the summary API no longer commits | `test_reading_tools_pages_writes_nothing` (13 pages, zero INSERT/UPDATE/DELETE) |
| **T-12** nav 403 dead-ends | **fixed** | `_tools_nav.html` is driven by `hdc_page_allowed(page_id, 'read')` — no role checks left | `test_inventory_only_user_gets_no_dead_nav_links` |
| **T-13** scoped dashboards claim balance | **fixed** | `scope_limited` on ledger totals, reconciliation and the JSON API; `reconciled`/`balanced` suppressed; a banner explains the slice on 8 pages | `test_scoped_user_totals_are_flagged_not_reconciled` |
| **T-16** no pagination / N+1 | **fixed** | one grouped movements query, eager loads, SQL-side filtering, pagination on `/serials`, `/glance` and the hub (see §6.1) | `test_serial_page_is_paginated` |
| **T-10 a,b,d,e,f,g,h** | **fixed** | totals follow the lines and a zero-pending rental becomes `paid`; posted `transaction_date` stamps maintenance/damage movements; duplicate references blocked across transfers, purchases and movement notes; cancelled source rentals refused by name; a substituted tool code warns; `current_location_label` indexed | `test_no_charge_edit_zeroes_the_bill`, `test_maintenance_records_the_posted_date`, `test_duplicate_reference_is_blocked_on_a_second_transaction`, `test_duplicate_tool_code_is_reported_not_swallowed` |
| **T-10 c** audit adjustments do not re-bill open `per_day` rentals | **by design — not changed** | `post_adjustments` documents this: rent already billed is never touched, and money owed for a lost tool belongs on the rental's own page | — |

### Two further defects the new tests found (both fixed here)

1. **Editing a tool with no description 500'd.** `(request.form.get('description') or tool.description).strip()`
   raises `AttributeError` when both sides are empty — which is every tool saved without a description, from the
   real form as well as a script. Replaced by `_posted_text()` for description/unit/condition/status/name/code
   (`test_tool_edit_survives_a_tool_without_a_description`).
2. **Reversing an audit write-off rolled itself back.** The audit line still pointed at the scrap row
   (`hdc_tool_audit_line.scrap_id`), so the flush inside `create_movement_log` hit a foreign-key error, the
   `except` did a whole-session `rollback()`, and the count reported "cancelled" while the stock stayed written
   off. The reference is now cleared before the delete, each line reverses inside its own savepoint, and
   `void_audit` / `reopen_audit` say loudly when a reversal failed
   (`test_void_audit_with_reverse_puts_stock_back`).

### Three tools-section tests that were already failing on `d86b1fc`

Verified in a clean worktree of the base commit, then fixed:

* `test_tool_tracking.test_transfer_rental_form_follows_from_tools_to_terms_order` — the "what is happening"
  step moved off the New Rental page when creation got its own page; the ordering assertions now use the five
  steps the page actually renders.
* `test_name_combo_fields.test_fixed_enumerations_remain_native_selects` — `txn_type` is asserted on
  `/hdc/tool-rental/transaction` (where it always renders) instead of `/new`.
* `test_user_permissions.test_tool_rental_hub_stays_simple_and_creation_has_its_own_page` — the hub's action
  card had gone missing entirely while its subtitle still said "New rentals are created on the New Rental
  tab"; the card is back, gated by the same write check the other tool pages use.


---

## 1. CRUD availability matrix

Legend — ✅ full path exists · ⚠️ exists but flawed · ❌ absent · 🔒 admin/accountant only (`@_money_write_required`).

### 1.1 Master data

| Entity | Create | Read | Update | Delete / Void | UI entry point | Notes |
|---|---|---|---|---|---|---|
| **Tool Category** (`hdc_tool_category`) | ✅🔒 `POST /inventory` `action=add_category`; also inline `new_category` on add-tool | ✅ inventory filters, modals | ✅🔒 `POST /category/<id>/edit` | ✅🔒 `POST /category/<id>/delete` — **hard delete**, guarded: refuses if any tool (incl. archived) references it (`tool_rental.py:1037`) | `tool_inventory.html:331–361` | Only true hard delete in the section. Duplicate name → `_ensure_tool_category` reuses + flashes "already exists". |
| **Tool** (`hdc_tool`) | ✅🔒 `POST /inventory` `action=add_tool` (+ optional opening purchase) | ✅ `/inventory`, `/tool/<id>`, `/glance`, `/reports`, `/serials` | ⚠️🔒 `POST /inventory/<id>/edit` — see **T-03** (wipes `category_id`, accepts qty below qty-out) | ⚠️🔒 `POST /inventory/<id>/delete` = **soft archive** (`is_void=True`, `status='retired'`, `:888`); blocked while `qty_pending > 0`. **No un-archive route, no `is_void=False` write anywhere** | `tool_inventory.html` add form 60–75, per-row edit modal 380–398 | Archived tools vanish from inventory, serial tracking and Reports registers but **stay in `tool_kpis()` purchase totals** → see **T-02**. |
| **Tool Purchase / stock-in** (`hdc_tool_purchase`) | ✅🔒 `POST /inventory/<id>/purchase` and generic `POST /inventory/purchase` | ✅ Reports "Purchase register", `/tool/<id>` | ❌ no edit route | ❌ **no void/cancel** — a wrong purchase can only be countered by hand-editing `total_quantity` (logged as `movement_type='adjustment'`) or scrapping | Purchase modal + per-tool modal | Purchase rows are filtered out of registers once the tool is archived (**T-02**). |
| **Tool Scrap / stock-out** (`hdc_tool_scrap`) | ✅🔒 `POST /inventory/<id>/scrap`, `POST /inventory/scrap` | ✅ Reports "Scrap register" | ❌ | ❌ **no void** | Scrap modal | Same archive blind spot as purchases. |
| **Tool Serial** (`hdc_tool_serial`) | ✅🔒 `GET+POST /inventory/<id>/serials/create` page, `POST /api/.../serials/create`, auto-backfill `ensure_tool_serials` | ✅ `/serials`, `/tracking`, `/glance`, APIs | ⚠️ `POST /api/.../serials/<id>/rename` (**no caller**), `POST /api/.../serials/<id>/status` (**no caller**) | ❌ no delete/void/merge; serials of an archived tool simply disappear from views | Serial manager page; JS-built link from inventory rows | Manager page/API create serials with **no purchase row and no qty bump** → **T-04**. |
| **Serial movement** (`hdc_tool_serial_movement`) | ✅ implicit (every move) | ✅ serial history, tracking | ❌ (append-only by design — correct) | ❌ | — | Append-only ledger: good design. |
| **Movement log** (`hdc_tool_movement_log`) | ✅ implicit | ✅ `/tool/<id>`, audit movement pane | ❌ | ❌ | — | `adjustment` rows are the only place hand-edited qty is recorded. |

### 1.2 Rentals

| Entity | Create | Read | Update | Delete / Void | UI entry point | Notes |
|---|---|---|---|---|---|---|
| **Rental** (`hdc_tool_rental`) | ✅🔒 `POST /create` (from `/new`) | ✅ hub `/`, `/dashboard`, `/<id>`, `/<id>/edit`, Reports | ✅🔒 `POST /<id>/update` — header always editable; **tool lines locked server-side** once a return/transfer exists (`:2528–2542`, compares `(tool_id, qty, rate)` tuples and flashes a warning); `billing_type` locked once money moved (`:2418`) | ❌ **`ToolRental.is_void` is never written by any route or service** — only `is_void=False` at creation (`:725`). Four routes branch on it (`:1403, :2387, :2678, :3075`) = dead code. There is **no way to cancel a wrongly-created rental** except editing it down or the admin bulk wipe | `tool_new_rental.html` | **T-01** (missing capability, not just a dead flag): a mistaken rental keeps stock out and keeps accruing `per_day` billing. |
| **Rental item / line** (`hdc_tool_rental_item`) | ✅ via create/update | ✅ detail, pending-items API | ⚠️ via `/<id>/update` (locked as above) | ⚠️ line removal only through update; `qty_pending` never goes negative (guarded) | `tool_new_rental.html` line repeater | `recalc_rental_totals` keeps a **stale `total_amount`** when lines zero out, and has a dead `payment_status` branch **[STATIC]**. |
| **Return** (`hdc_tool_rental_return` + `_item`) | ✅🔒 `POST /<id>/return` (qty or serial-level) | ✅ detail, tracking | ❌ | ❌ **no void** — a wrong return can only be undone by a new transfer/rental edit | Return modal on detail page | Blocked for `returned/closed`; **not** blocked for `is_void` (moot today, see T-01). |
| **Transfer** (`hdc_tool_rental_transfer` + `_item`) | ⚠️🔒 `POST /<id>/transfer` — **no duplicate guard, no from≠to check** → **T-05** | ✅ detail chain | ❌ | ❌ no void | Transfer modal | Double-click / retry produces two rows and a chain reading `Site B › Site B` **[LIVE]**. `_has_recent_duplicate` (12-second window) exists in `timekeeping.py` but is **not** used here. |
| **Payment** (`hdc_tool_rental_payment`) | ✅🔒 `POST /<id>/payment` — requires an active `company/cash/bank` receiving account (`:2804`); posts exactly one `AccountTransaction` `tool_rental_payment:direct` **[LIVE verified: cash +1,000, 1 ledger row]** | ✅ detail, receipt `GET /payment/<id>/receipt` | ❌ (no edit — correct for money) | ✅🔒 `POST /payment/<id>/void` (`pay.is_void=True`, `:3016`) reverses the ledger row | Detail page payment block + void modal `tool_rental_detail.html:204–217` | ⚠️ **overpayment is completely unguarded** → **T-06** (₨500,000 on a ₨3,000 rental accepted). |
| **Discount / concession** (`hdc_tool_rental_discount`) | ✅🔒 `POST /<id>/discount` | ✅ detail | ❌ | ✅🔒 `POST /discount/<id>/void` | Void modal `tool_rental_detail.html:491–508` | `tool_kpis()` **ignores `total_discount`** → KPI revenue overstates net **[STATIC]**. |
| **Rental → ledger link** (`hdc_tool_rental_account_txn`) | ✅ implicit | ✅ | ❌ | ✅ via payment/discount void | — | Correctly maintained; `admin._drop_dead_tool_rental_account_links()` cleans it after an Accounts wipe. |

### 1.3 Stock transactions & physical audit

| Entity | Create | Read | Update | Delete / Void | UI entry point | Notes |
|---|---|---|---|---|---|---|
| **Damage / Loss** | ✅🔒 `POST /transaction/damage-loss` (`record_damage_loss`, incl. rental-scoped `_record_rental_damage_loss`) | ✅ `/transaction`, movement logs | ❌ | ❌ **no reversal** | `tool_txn.html` via `TXN_GROUPS` | Posted date from the form is **discarded** (`_parse_when` fallback) **[STATIC]**. |
| **Send to maintenance** | ✅🔒 `POST /transaction/maintenance` | ✅ `maintenance_pieces()` | ❌ | ⚠️ only by `maintenance-return` (condition change), not a true void | same | Same posted-date issue. |
| **Return from maintenance** | ✅🔒 `POST /transaction/maintenance-return` | ✅ | ❌ | ❌ | same | `txn_reference_seen()` does **not** cover maintenance/transfer references → duplicate references possible **[STATIC]**. |
| **Physical Audit** (`hdc_tool_audit`) | ✅🔒 `POST /audit/start` (or first `save`) — `GET /audit/count` intentionally creates nothing and 302s to the open sheet for that location | ✅ `/audit`, `/audit/<id>`, `/api/.../audit` | ✅🔒 `POST /audit/<id>/save` (incremental counts) | ✅🔒 `POST /audit/<id>/void` — but **leaves posted adjustments in place** → **T-09** | `tool_audit.html`, `tool_audit_sheet.html`, `tool_audit.js` | `reopen_audit` also **does not undo** posted adjustments (**T-09**). |
| **Audit line** (`hdc_tool_audit_line`) | ✅ via start/save | ✅ sheet (`_sheet_row`, key is `expected_qty`) | ✅ via save | ❌ (voided with the parent) | sheet | `close_audit` refuses only *uncounted* lines, then sets `status='adjusted'` and flashes **"the count matched the book"** even with an open discrepancy → **T-08**. |
| **Audit adjustment** (posted gain/loss) | ✅🔒 `POST /audit/<id>/adjust` (`_grant_gain` / `_lift_pending`) | ✅ plan pane | ❌ | ❌ | sheet | Gain via `mode=both` breaks `owned == purchased − scrapped` (**14 vs 8** **[LIVE]**) → **T-07**. Adjustments mutate qty **without re-billing** open rentals **[STATIC]**. |

### 1.4 CRUD gap summary

* **Entities with a working reversal path:** payment, discount, audit (partial), category (hard delete).
* **Entities with NO reversal path:** rental, rental line removal, return, transfer, purchase, scrap, serial, damage/loss, maintenance, audit adjustment.
* **Irreversible state changes:** tool archive (no restore), audit close (message lies), audit void/reopen (stock stays moved).
* **Only bulk delete:** `admin._wipe_selected_targets(['tools'])` (Settings, admin only) — and it is **incomplete**, see **T-11**.

---

## 2. Route inventory & permission gating

**57 rules** match `/hdc/tool-rental*` + `/hdc/api/tool-rental*` — verified against the live url_map: **42 page rules** (15 GET, 2 GET+POST, 25 POST) + **15 API rules** (11 GET, **4 mutating POST**).

* Every route is `@login_required`.
* Every mutating POST is `@_money_write_required()` (`hdc/extensions.py:143`), which combines `begin_sqlite_write` (serialised write txn) with the `ACCESS_MATRIX` row `('/hdc/tool-rental', 'write', {'admin','accountant'})` (`extensions.py:174`).
* A global `before_request` (`hdc/app.py:131–160`) additionally enforces per-page permissions through `may_access_path()` (`hdc/services/permissions.py:167`) with page IDs `tools`, `tool_inventory`, `tool_tracking`, `tool_audit` (ids 69–72).

### 2.1 Live permission matrix **[LIVE]**

| Actor | Read pages (dashboard/hub/inventory/tracking/serials/glance/reports/audit) | `new`, `transaction`, `<id>/edit` | Any POST |
|---|---|---|---|
| admin | 200 | 200 | succeeds |
| accountant | 200 | 200 | succeeds |
| manager | 200 | **302** | **302, zero rows written** (payment/rental/category counts unchanged, `owned_qty` unchanged 50.0) |
| staff (no grants) | 200 | **302** | **302, zero rows written** |
| staff + `tools:read` only | inventory/tracking/reports/audit → **403** | 302 | 302 |
| staff + all four pages `rw` | 200 | 302 (write gate still blocks) | 302 |
| staff + `tools:read:false` | **403 everywhere** | — | — |
| staff + `tool_inventory:rw` only | `/inventory` 200, other pages 403 | — | — |

> **Note:** `TOOLS_SECTION_DEEP_AUDIT.md` §4 claims "Admin/accountant/manager pass" the money-write gate. That is **wrong** — manager writes are blocked, confirmed live with row-count assertions.

### 2.2 Gating findings

* **T-12 — Nav/route asymmetry produces 403 dead-ends.** `_tools_nav.html:9–12,40` gates links by *role* (`tools_is_admin`), not by `may_access_path`. A staff user granted only `tool_inventory:rw` gets an inventory page whose nav renders **7 links, 5 of which return 403** (hub, dashboard, tool position, tracking, transaction) **[LIVE]**. Conversely Glance / Tracking / Serials / Reports are hidden from a *manager* who is fully allowed to read them.
* **T-13 — Scoped manager dashboards under-report but still claim balance.** With a scope limiting the manager to one project: scoped sees `in_store 48, out 2, customer 0, open_rentals 1, pending_amount 200, utilization 4%` while admin sees `44/6/4/2/600/12%`; `owned_qty` is **identical (50)** because `Tool` is unscoped, and **both report `reconciled: true` / `balanced: true`** **[LIVE]**. The scoped manager's external rental detail returns **404** (correct), and the hub lists only `R-INT`. The KPI card therefore says "books balance" on numbers that provably don't.

---

## 3. Flow audit (end-to-end journeys)

Two harnesses:

1. **Route walk** (`/tmp/tools_flow_walk.py`): 122 steps across all 57 rules — **6 FAIL, 4 CHECK, 12 REJECTED-by-design**. Every FAIL was a probe-ordering artefact except those listed below.
2. **Flow-map resolution** (`/tmp/tools_probe5.py`): rendered all 17 tool pages as admin with seeded data, extracted every `<form>` and every internal `/hdc/tool-rental*` `href`, and matched each against `app.url_map`:
   * **17/17 pages render 200** (`/audit/count` 302s to the open sheet — by design, `:3508–3522`).
   * **48/48 forms resolve** to a real rule accepting their method. The 9 "GET form" hits are legitimate search/filter forms (inventory, hub, dashboard, glance, serials, tracking, reports, audit).
   * **256/256 internal links resolve** — no dead `url_for`, no 404 hrefs. (Earlier regex-scraping false positives came from JS string concatenation such as `'/hdc/tool-rental/inventory/' + toolId + '/serials/create'`; the target rule exists.)
   * `tool_txn_types.registry_problems() == []` and **every** `TXN_GROUPS` / `NEW_RENTAL_TXN_TYPES` endpoint exists in the url_map.
   * Every `fetch()` in `tool_new_rental.html` (lines 1780, 1984, 2156, 2252) and `tool_txn.js` targets an existing endpoint.

**Full CRUD chain verified working end-to-end:** create category → create tool with opening purchase → serials auto-backfilled → create internal rental (per-day) → create external rental (fixed fee) → transfer to another site → return part → post payment (lands in Accounts) → grant discount → void discount → void payment (ledger reverses) → start physical count → save counts → plan adjustments → close audit → reopen → void.

**Journey-level dead ends found:**

| Journey | Breaks at | Symptom |
|---|---|---|
| "Cancel this rental, it was a mistake" | no route | Only options: edit lines down (blocked if a return/transfer exists) or admin bulk wipe |
| "Un-archive this tool" | no route | Tool invisible in inventory/serials/reports; purchase history unreachable; `tool_kpis` still counts its purchases |
| "Undo a transfer I posted twice" | no route | Two transfer rows, chain `Site B › Site B`, stock double-moved |
| "Correct a wrong purchase qty" | no route | Hand-edit `total_quantity` → logged as `adjustment`, invariant `owned == purchased − scrapped` still broken |
| "See archived tools / show void" | no filter | `inventory_rows(view=...)` has `all/out/store/attention` only; `tool_ledger()` filters `Tool.is_void == False` (`tool_tracking.py:385`) |
| "Scroll page 2 of serials" | no pagination | **No tool template uses `_pagination.html`** (other sections do); `inventory_rows(limit=…)` exists but no route passes it |
| staff with `tool_inventory` grant → any nav link | 403 | T-12 |

---

## 4. Data-integrity findings (ranked)

### T-06 · Overpayment is unguarded — money disappears into an invisible credit **[LIVE, HIGH]**
`POST /<id>/payment` validates amount > 0 and the receiving account, but never compares against `pending_amount`.
Probe: rental total ₨3,000, posted a ₨500,000 payment → `pending_amount 0`, `payment_status 'paid'`, **client ledger +501,000**, and the ₨497,000 credit is **not surfaced in any KPI, report or receivable view**.
Fix: reject `amount > pending_amount + EPS` (or require an explicit "advance / credit" flag that posts to a customer-advance account and shows on the Reports page). Files: `hdc/routes/tool_rental.py:2916` (payment route), account validation at `:2804` and `:2934`, `hdc/services/tool_rental.py:820–960`.

### T-03 · `POST /inventory/<id>/edit` silently wipes `category_id` and accepts impossible quantities **[LIVE, HIGH]**
The edit route reads `category_id` with `type=int` and assigns it unconditionally — unlike `name`/`unit`, which fall back to the existing value when absent. Any post that omits the field (combo widget `data-hdc-combo` not yet synced, JS off, a partial/programmatic post, or an unrecognised typed category name) **nulls the tool's category**. The edit form always posts `category_id` and includes a "No Category" option, so this is reachable from the UI.
Same route: accepts `total_quantity` **below the quantity currently out** → `balanced=False, variance=-2` **[LIVE]**, with only an `adjustment` movement log.
Fix: fall back to `tool.category_id` when the field is absent/invalid; clamp `new_qty >= qty_out + qty_pending` (or refuse with an integrity message). File: `hdc/routes/tool_rental.py:835–895` (`hdc_tool_rental_tool_edit`).

### T-02 · Archiving a tool breaks the purchase tie-out and hides history **[LIVE, MEDIUM-HIGH]**
`tool_kpis()` aggregates `ToolPurchase`/`ToolScrap` **without** filtering `Tool.is_void`, while `tool_purchases()` / `tool_scraps()` (the Reports registers) **do** filter it.
Probe: 2 tools, 2 purchases, total ₨15,000. After archiving one tool:
* KPI: `purchased_qty 15.0, purchase_value 15000.0`, `total_tools 5.0`
* Reports register: **1 row, ₨5,000**; the Reports page no longer shows 15,000 anywhere
* DB still holds **2** purchase rows; live owned qty (non-void) is **5**
So `owned == purchased − scrapped` stops tying the moment anything is archived, and the archived tool's purchase/scrap history becomes unreachable from every page while still inflating KPIs.
Fix: make `tool_kpis()` and the registers agree on one rule (recommend: exclude archived tools from both, and add a "show archived" filter on Inventory + Reports so history stays reachable). Files: `hdc/services/tool_rental.py` (`tool_kpis`, `tool_stock_aggregates`), `hdc/services/tool_tracking.py:385`, `hdc/routes/tool_rental.py:879–893`.

### T-04 · Serial manager creates serials that the books don't know about **[LIVE, MEDIUM-HIGH]**
The serial-manager page/API (`GET+POST /inventory/<id>/serials/create`, `POST /api/.../serials/create`) creates `ToolSerial` rows **without** a purchase row and **without** bumping `Tool.total_quantity`. Probe: owned 10 → **13 live serials**. `ensure_tool_serials` only *backfills* (caps at `min(500, owned)`) and never prunes extras, so the drift is permanent and `serials_out != qty_out` from then on. Combined with **T-14** (dead APIs that mutate) this is the easiest way to desync the section.
Fix: either route serial creation through the purchase flow (create the purchase row + qty bump) or add an explicit "extra serial" reason that writes an `adjustment` movement log and reconciles `total_quantity`. Files: `hdc/routes/tool_rental.py:4200+`, `hdc/services/tool_rental.py:1222–1300, 1301–1412`.

### T-05 · Transfers have no duplicate guard and no from≠to check **[LIVE, MEDIUM]**
`POST /<id>/transfer` accepts an identical body twice (2 rows) and accepts `to_project_id == current location`, producing a movement chain reading `Site B › Site B` and a double stock move.
Fix: reuse the `_has_recent_duplicate` pattern (12-second window, `hdc/services/timekeeping.py:111`) keyed on `(rental_id, to_label, qty, date)`, and refuse a no-op transfer. File: `hdc/routes/tool_rental.py:3143` (`hdc_tool_rental_transfer`).

### T-07 · Audit gain breaks the owned/purchased invariant **[LIVE, MEDIUM]**
`_grant_gain(mode='both')` raises stock without a purchase row. Probe: `owned 14` vs `purchased − scrapped = 8`.
Fix: post gain as a zero-cost (or valued) purchase row / dedicated `gain` movement type that `tool_stock_aggregates()` counts, so the invariant is preserved by construction. File: `hdc/services/tool_audit.py` (`_grant_gain`, `_lift_pending`).

### T-14 · GET requests write to the database **[LIVE, MEDIUM]**
`GET /hdc/tool-rental/<id>` executed **12 write statements** on first visit (`get_serials_for_rental` → `ensure_tool_serials`, `hdc/services/tool_rental.py:1934–1975` → `:1301–1412`). `ensure_all_tools_serials` also runs on **every** `/inventory`, `/new` and `/tracking` GET. Consequences: read-only roles mutate data; writes happen outside `begin_sqlite_write` (lock contention risk); a page refresh can change serial counts.
Fix: move backfill to an explicit maintenance action (or to the write paths that need it) and make GETs read-only. This also removes a large slice of the T-16 query cost.

### T-15 · Dead APIs still mutate state **[LIVE, MEDIUM]**
`POST /api/.../serials/assign` moved **2 serials out** while `qty_pending` stayed at **4** (serials out 8 vs books 4); `/serials/return` reversed it. Neither endpoint has any caller in templates, static JS, tests or docs — but both are live, `login_required`-only… *(they are gated by the money-write decorator; the defect is that they bypass the rental-integrity checks the UI paths perform).*
Fix: delete the unused mutating endpoints, or make them call the same service functions the UI uses (with the pending-qty bookkeeping). File: `hdc/routes/tool_rental.py:4129–4200`.

### T-08 / T-09 · Audit close / reopen / void misreport and leave stock moved **[LIVE + STATIC, MEDIUM]**
* `close_audit` (`tool_audit.py:1244`) refuses only when lines are **uncounted**; it then sets `status='adjusted'` and flashes *"the count matched the book"* even with an open discrepancy (**T-08**).
* `reopen_audit` does **not** undo adjustments already posted.
* `void_audit` (`:1284–1286`) sets `is_void=True` but **leaves the stock adjustments applied** (**T-09**) — voiding an audit silently keeps its effect on inventory.
Fix: close should report the residual variance in the flash; void should reverse posted adjustments (or refuse to void an audit that has posted any, pointing at a correction flow instead).

### T-11 · Admin "wipe Tools" leaves discount rows and live ledger concessions **[LIVE, MEDIUM]**
`_WIPE_TARGETS['tools']` (`hdc/core/admin.py:230–250`) lists 17 tables and **omits `hdc_tool_rental_discount`**; `_void_tool_rental_ledger_income()` only voids the `tool_rental_payment` prefix.
Probe after wipe: `hdc_tool_rental_discount` still **1 row**; Accounts still holds a live `tool_rental_discount:direct` txn of **100.0** for a rental that no longer exists (all other tool tables → 0).
Fix: add `hdc_tool_rental_discount` to the wipe list and extend the void call to the discount prefix (`_accounts_void_by_source_prefix('tool_rental_discount', …)`).

### T-10 · Static-only integrity risks (not yet reproduced)

| # | Finding | Location |
|---|---|---|
| a | `recalc_rental_totals` keeps a stale `total_amount` when all lines zero out; contains a dead `payment_status` branch | `hdc/services/tool_rental.py` |
| b | `tool_kpis()` ignores `total_discount` → revenue KPI overstates net | same |
| c | `_lift_pending` / `_grant_gain` change qty without re-billing open `per_day` rentals | `hdc/services/tool_audit.py` |
| d | Maintenance / damage-loss routes discard the posted date from the form | `hdc/services/tool_txn_entry.py:_parse_when` |
| e | `txn_reference_seen()` does not cover maintenance/transfer references → duplicate references possible | `hdc/services/tool_txn_entry.py:105` |
| f | Transfer-rental blocks `returned/closed` but not `is_void` | `hdc/routes/tool_rental.py:3075` |
| g | Add-tool silently substitutes a new `tool_code` when the typed one already exists (operator's code discarded, no warning) | `hdc/routes/tool_rental.py:704–706` |
| h | No index on `Tool.current_location_label` despite it being a search haystack field (already noted in `TOOLS_SECTION_DEEP_AUDIT.md` §5) | `hdc/core/schema.py:1942–1961` |

---

## 5. Money / Accounts integration

**Correct and verified [LIVE]:** a ₨1,000 payment on an external rental produced exactly **one** `AccountTransaction` with `source_type='tool_rental_payment:direct'`, cash account **+1,000**, one `hdc_tool_rental_account_txn` link row, `total_paid` and `payment_status` updated. Voiding reverses it. Payment requires an active `company/cash/bank` account (`:2804`, `:2934`) and rejects silently-invalid ids (probe: payment with no account → **0 rows**, `total_paid 0.0`).

Caveats:
* Source types carry `:direct` / `:step1` suffixes — any query using `== 'tool_rental_payment'` returns **0 rows** (a false-negative trap for future reports/QA).
* Overpayment → **T-06**; wipe → **T-11**; KPI ignores discounts → **T-10b**.
* `accounts.py` guards (overdraft block, duplicate-source guard in `_build_account_txn_rows:927` / `_create_account_transaction:1019`) all behave as documented for tool sources.

---

## 6. Performance & scale **[LIVE]**

Seeded **60 tools / 2,400 serials / 250 rentals** (`/tmp/tools_scale.py`, 19.6 s run):

| Page | Time | Payload | Rows rendered | SQL queries |
|---|---|---|---|---|
| `/serials` | **1.58 s** | **12.8 MB** | 7,201 `<tr>` | **3,087** |
| `/glance` | **1.35 s** | 3.3 MB | 2,403 | **3,086** |
| `/inventory` | 0.59 s | 648 KB | 60 | 830 |
| `/transaction` | 0.56 s | — | — | 882 |
| `/new` | 0.46 s | — | — | 1,047 |
| `/reports` | 0.25 s | 1.1 MB | 1,010 | — |
| `/tracking` | 0.19 s | — | — | 453 |
| `/` (hub) | 0.13 s | 459 KB | **251 (all 250 rentals)** | — |
| `/audit` | 0.11 s | — | — | 201 |
| `/dashboard` | 0.10 s | — | — | 197 |
| `/api/.../dashboard` | 0.09 s | — | — | 192 |

**T-16 — Root causes (all confirmed by reading the code paths):**
1. `all_tool_serials_tracking()` (`tool_tracking.py:1533–1630`) issues **one `ToolSerialMovement` query per serial** plus lazy loads of `current_rental` and `tool.category` → the ~3,086-query floor on `/serials` and `/glance`.
2. `ensure_all_tools_serials()` runs on every `/inventory`, `/new`, `/tracking` GET (also **T-14**).
3. Search/filter happens **in Python after all rows are built** (`inventory_rows`, `tool_tracking.py:1188–1227`) — a `limit` parameter exists but **no route passes it**.
4. **No pagination in any tool template** — `templates/hdc/tool_rental/*.html` never includes `shared/_pagination.html`, although the rest of the app does. The hub renders all 250 rentals; `/serials` renders all 2,400 serials into a 12.8 MB document.

At ~10× this volume (a realistic fleet) `/serials` is a ~130 MB / 30,000-query page. This is the single highest-impact fix in the section: eager-load movements in one grouped query, add `limit/offset` + `_pagination.html`, and drop the GET-time backfill.

### 6.1 The same seed, measured again after the fixes **[LIVE]**

Same 60 tools / 2,400 serials / 250 rentals / 500 rental lines / 60 purchase rows, same login, one warm request
then one measured request per page:

| Page | Before | After | Queries before → after |
|---|---|---|---|
| `/serials` | 1.58 s · 12.8 MB · 7,201 `<tr>` | **0.08 s · 263 KB · 151 `<tr>`** (50 rows + history modals) | 3,087 → **5** |
| `/glance` | 1.35 s · 3.3 MB · 2,403 `<tr>` | **0.18 s · 108 KB · 53 `<tr>`** | 3,086 → **9** |
| `/` (hub) | 0.13 s · 459 KB · 251 `<tr>` | **0.04 s · 142 KB · 51 `<tr>`** (50 rentals/page) | — → 74 |
| `/inventory` | 0.59 s · 648 KB | **0.16 s · 692 KB** | 830 → **279** |
| `/new` | 0.46 s | **0.26 s** | 1,047 → **623** |
| `/tracking` | 0.19 s | 0.22 s | 453 → 455 |
| `/reports` | 0.25 s · 1.1 MB | 0.23 s · 1.7 MB | — → 532 |
| `/dashboard` | 0.10 s | 0.10 s · 90 KB | 197 → 195 |

The two pages that scaled worst are now flat in the number of serials: `/serials` and `/glance` render one page
of rows from a handful of grouped queries, so 24,000 markings cost the same as 2,400. `/tracking` and `/reports`
still render every row they hold (they are grouped by location / register, not by serial) and are the next
candidates if the fleet grows; neither is a per-row query any more.

---

## 7. Dead / orphaned surface

**9 API endpoints with no caller anywhere** (templates, static JS, tests, scripts, docs) — 4 of them mutate:

| Endpoint | Method | Mutates | Note |
|---|---|---|---|
| `/api/tool-rental/serials/assign` | POST | **yes** | T-15: desyncs serials vs `qty_pending` |
| `/api/tool-rental/serials/return` | POST | **yes** | reverses the above |
| `/api/tool-rental/serials/create` | POST | **yes** | T-04: serials without purchase/qty |
| `/api/tool-rental/serials/<id>/rename` | POST | **yes** | |
| `/api/tool-rental/serials/<id>/status` | GET | no | |
| `/api/tool-rental/serials/tool/<id>/available` | GET | no | |
| `/api/tool-rental/serials/inventory-summary` | GET | no | |
| `/api/tool-rental/tools-available` | GET | no | |
| `/api/tool-rental/receiving-accounts` | GET | no | the payment modal renders accounts server-side instead |

Additionally `/api/tool-rental/audit` and `/api/tool-rental/dashboard` are **test-only** (no UI caller).

**Dead code:** 4 `rental.is_void` branches (T-01), `recalc_rental_totals`' `payment_status` branch, `inventory_rows(limit=…)`, `_tools_nav.html` links hidden from roles that may use them (T-12).

---

## 8. Test & CI coverage

* Suite: `python -m unittest discover -s tests -p 'test_tool*.py'` → **217 tests, 1 pre-existing ERROR, 5 skipped** in this environment. The error is `test_tool_tracking.test_transfer_rental_form_follows_from_tools_to_terms_order` (`tests/test_tool_tracking.py:306`, `ValueError`) and is unrelated to this audit; **the repo was not modified by any probe**.
* Endpoint hit counts (`/tmp/tools_endpoint_coverage.json`): create 94, transaction 69, detail 38, new 37, inventory GET 28 / POST 12, return 15, update 12, transfer 11, payment 10.
* **Never hit by any test:** `hdc_tool_rental_tool_edit`, `hdc_tool_rental_tool_delete`, `hdc_tool_rental_scrap_create` (generic modal), `hdc_tool_rental_payment_void`, `hdc_tool_rental_payment_receipt`, `hdc_tool_rental_audit_close`, `…_audit_reopen`, `…_audit_void`, `hdc_tool_rental_serials_create` (page **and** API) — i.e. exactly the endpoints where this audit found defects T-02, T-03, T-08, T-09.
* CI (`.github/workflows/ci.yml`, py3.11, `HDC_ENV=test`): compileall → `check_layers.py` → `check_db_safety.py` → unittest discover → `reorganize_frontend.py --check` → `qa_responsive.py` → `node --check` → jsdom `tests.test_tool_txn_pieces` → `tests/smoke_worker.py` → runtime inventory & financial acceptance. **No tool-specific smoke step.**

**Status on this branch:** `tests/test_tool_crud_gaps.py` (32 cases) now covers `tool_edit`, `tool_delete` /
`tool_restore`, `payment_void`, `audit_close` / `reopen` / `void`, serial create (page + API), rental void /
restore, transfer guards, the wipe and the nav/permission matrix. CI gained a named **"Tools section regression
smoke"** step that runs those plus the stock-lifecycle, rental-edit, discount, audit, serial-tracking and
serial-marking modules before the whole-suite step. Full suite on this branch: **971 tests, 0 failures,
0 errors, 5 skipped** (the three tools-section failures that were red on `d86b1fc` are listed in §0.1 and fixed).

---

## 9. Prioritised fix plan

> **All items below are implemented on this branch** — see §0.1 for the per-finding status, the code that
> changed and the test that proves it.

**P0 — money/stock correctness (small diffs, high value)**
1. **T-06** cap payment at `pending_amount` (or add an explicit advance path). `routes/tool_rental.py:2790+`
2. **T-03** stop wiping `category_id`; clamp `total_quantity` to `qty_out + qty_pending`. `routes/tool_rental.py:835–895`
3. **T-11** add `hdc_tool_rental_discount` to the wipe list + void the discount ledger prefix. `core/admin.py:230–250, 261–270`
4. **T-05** duplicate + no-op guard on transfer. transfer route
5. **T-15** remove or fix the 4 callerless mutating serial APIs (`rename :4074`, `assign :4129`, `return :4164`, `create :4200`).

**P1 — integrity & capability**
6. **T-01** add a rental **void/cancel** action (reverse pending qty, void linked ledger rows, block if payments are live) — and keep the existing `is_void` branches meaningful.
7. **T-02** one archive rule for KPIs *and* registers + an "include archived" filter on Inventory/Reports + an **un-archive** action.
8. **T-04** make serial creation write a purchase/adjustment so `serials == owned`.
9. **T-07 / T-08 / T-09** audit gain posts a stock-in row; close reports residual variance; void/reopen reverse posted adjustments.
10. **T-14** make GETs read-only (move `ensure_*_serials` off the read path).
11. **T-12 / T-13** drive `_tools_nav.html` from `may_access_path` instead of role; stop reporting `reconciled/balanced: true` on scope-filtered numbers (or label them "in your scope").

**P2 — scale & hygiene**
12. **T-16** one grouped movements query in `all_tool_serials_tracking`; add pagination (`_pagination.html`, `limit/offset`) to `/serials`, `/glance`, hub, `/reports`; push search filters into SQL.
13. Tests for the 13 never-hit endpoints, starting with `tool_edit`, `tool_delete`, `payment_void`, `audit_close/reopen/void`; add a tools smoke step to CI.
14. T-10 a–h cleanups; add the `current_location_label` index; correct `TOOLS_SECTION_DEEP_AUDIT.md` §4 (manager does **not** pass the write gate).

---

## Appendix A — Reproducing the key defects

All probes ran against a throwaway DB (`HDC_DB_PATH`/`HDC_INSTANCE_DIR` in a temp dir), logging in through `/hdc/login` with a real `generate_password_hash` password and taking `_csrf_token` from the login page. Gotchas worth keeping:

* Faking login via `session_transaction()['_user_id']` → HTTP 400 on POSTs. Always log in for real.
* Pushing one `app.app_context()` for a whole script leaks `g._login_user` into every later `test_client()` (all clients look authenticated). Use one client per user, and wrap DB work in `with app.app_context():`.
* `GET /hdc/logout` does nothing (POST-only + CSRF); `FlaskClient.clear_cookies()` doesn't exist in Flask 3.1.3.
* Reusing ORM instances across contexts → `DetachedInstanceError`; re-query ids inside each context.
* Query ledger rows with `.ilike('tool_rental_payment%')`, never `== 'tool_rental_payment'`.
* `audit_sheet()` row key is `expected_qty`, not `book_qty`.
* SQLAlchemy `Date` columns reject `db.func.current_date`; pass `datetime.date`.

Minimal repros:

```python
# T-06 overpayment (rental total 3000)
c.post(f'/hdc/tool-rental/{rid}/payment',
       data={'amount': '500000', 'payment_date': '2026-06-01',
             'received_to_account_id': acc_id, '_csrf_token': tok()})
# -> pending_amount 0, payment_status 'paid', ledger +501000, credit invisible

# T-03 category wipe
c.post(f'/hdc/tool-rental/inventory/{tid}/edit',
       data={'name': 'Vibrator', 'unit': 'pcs', 'total_quantity': '1',  # no category_id
             '_csrf_token': tok()})
# -> tool.category_id is None; total_quantity 1 < qty_out 3 -> balanced False, variance -2

# T-05 duplicate transfer
body = {'to_type': 'site', 'to_project_id': siteb, 'qty_transferred': '1',
        'transfer_date': '2026-06-01', '_csrf_token': tok()}
c.post(f'/hdc/tool-rental/{rid}/transfer', data=body)
c.post(f'/hdc/tool-rental/{rid}/transfer', data=body)   # 2 rows, chain 'Site B › Site B'

# T-11 incomplete wipe
from hdc.core.admin import _wipe_selected_targets
_wipe_selected_targets(['tools'])
# SELECT COUNT(*) FROM hdc_tool_rental_discount  -> still 1
# live ledger: ('tool_rental_discount:direct', 1, 100.0)

# T-15 dead API mutates
c.post('/hdc/api/tool-rental/serials/assign', data={'rental_id': rid, 'serial_ids': '1,2', ...})
# -> 2 serials out, qty_pending unchanged at 4
```

## Appendix B — Evidence artefacts

Probe harnesses and logs were run from `/tmp` (not committed): `tools_flow_walk.py` → `flow_walk.log`, `tools_flow_report.json`; `tools_probe2.py` → `probe2.log`; `tools_probe_perm.py` → `tools_perm_report.json`; `tools_probe3.py` → `probe3.log`; `tools_scale.py`; `tools_probe4.py` (archive/wipe); `tools_probe5.py` → `probe5.log` (flow map); `coverage_run.py` → `tools_endpoint_coverage.json`, `tool_tests.log`. Say the word and I'll commit them under `qa/` as a repeatable harness.

**No repository file was modified during this audit** — every probe used its own temporary database and instance directory.
