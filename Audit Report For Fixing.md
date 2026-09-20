# Audit Report For Fixing

**Application:** HDC ERP — Hadi Design & Construction (`rehmaahmed11/HDC-MAIN`)
**Audited revision:** `main` @ `068d124` (merge of PR #25 "Money Center")
**Audit date:** 2026-09-20
**Audit type:** full pass — frontend, backend, database, API, money flows, and development phases
**Status of this report:** findings only. **No application file was modified by this audit** — the repo only gained this report. Follow the numbered plan in [Section 11](#11-step-by-step-fix-plan--follow-this-in-order).

---

## 0. How to use this report

| Section | What it is | Who reads it |
|---|---|---|
| 0.1 | **Coverage map** — your named areas (what we offer, money received/spent, pendings, tools, payments, UI) | Owner |
| 1–2 | Scope, method, verdict summary | Owner / lead |
| 3 | Things that are **verified working** — do not "fix" them | Everyone |
| 4–8 | Findings by severity, each with file, line and proof | Developer |
| 9–10 | Database + CI/dev-phase status | Developer |
| **11** | **The ordered fix plan — steps 1→15** | Developer (do in order) |
| 12 | Definition of done — the tick-list to close the audit | Owner / developer |
| 13 | What this audit did **not** cover | Owner |
| Step 10 (in §11) | The regression tests to add so these bugs never return | Developer |
| A–B | Raw evidence + exact commands to reproduce every claim | Anyone |

Every claim below was produced by running the app in this sandbox, not by reading code alone. Commands are in [Appendix B](#appendix-b--reproduce-every-claim).

---

## 0.1 Coverage map — every area you asked about

| Your area | App sections covered | How it was checked | Verdict |
|---|---|---|---|
| **What we offer** (estimation / quoting) | Estimation (`/hdc/estimation`), Project Estimation, Trades, Stages, Projects | GET crawl (all 200), text scan, `render_template` targets | ✅ pages render; only drifts are the doc counts (§8.6) |
| **Money received** | Owner payments (`/hdc/projects/<pid>/owner_payment`), party/client receipts (`/hdc/accounts`), tool-rental income, CF register "in" | E2E step-by-step (500,000 owner receipt → `project_income`; 75,000 CF in → amend 76,000 → void → restore), ledger + SQL verification | ✅ posts correctly; ⚠️ see 5.2 (CF void sync) |
| **Money spent** | Expenses, Expense Categories, Purchases V2 (purchase/delivery/usage/transfer), Payroll, Office Management, Personal Management, Workers, Subcontractors, HDC Tools, CF register "out"/transfer | E2E (advance, wage, payroll, supplier purchase, subcontract payment, office salary + expense, rental payment, personal expense, CF out/transfer), ledger verification | ✅ posts correctly; ⚠️ 4.2 (supplier payment 500), 5.1 (MC `party_payment` doesn't reduce the payable), 5.5 (usage cost missing from the KPI) |
| **Pendings** | Money Center pending API, supplier payables, payroll payable, worker/subcontractor balances, receivables | API probe (200), `_supplier_balance` cross-check (120,000 → 115,000), labour audit drift report | ✅ numbers reconcile; ⚠️ 5.1/5.4 |
| **Tools** | HDC Tools dashboard, inventory, create, rental, payment, return, voids | E2E full rental cycle (create → rent → pay 6,000 → return), ledger posting `party_receipt` verified | ✅ works |
| **Payments** | Worker payment/advance/tip, payroll pay, subcontractor + labour pay, supplier payment, office-staff payment/advance, personal expense, tips/settlements | E2E + guard battery + quick-post API (16 ledger rows) | ✅ all paths post; ⚠️ 4.2 supplier-payment 500; ⚠️ 6.1 role hole |
| **Every finance area** | Accounts Hub, Manage Accounts, All Entries, CF Register, Cash Flow, Cash-Flow Reconciliation (Day Close), Reconciliation, forensic/KPI APIs, Reports, Exports | Full crawl of all 166 routes, 241-step E2E, invariant SQL, guard battery, exports opened | ⚠️ 4.1 (Money Center 500), 5.2, 5.3, 5.4, 5.6, 7.1, 7.2, 7.5 |
| **The UI overall** | 98 templates (all pages) | 114-route text/garbage scan + byte/inline-script census + DOM-id audit on 18 pages | ✅ no garbage on page bodies; ⚠️ 7.3 bloat, 7.4, 7.5, and the money-center 500 |

---

## 1. Scope and method

**Environment used**

* Python 3.11.2, fresh `.venv`, `Flask 3.1.3`, `SQLAlchemy 2.0.54`, `openpyxl 3.1.5` (`requirements.txt` as-is).
* Four separate throwaway SQLite databases (`/tmp/audit/*.db`) — production data was never touched.
* Admin session via the real login form; CSRF tokens read from the real session.

**What was executed**

| # | Test | Result |
|---|---|---|
| 1 | Full GET crawl of **every** registered route (166 GET routes, 242 endpoints in the URL map) | 109 × 200, 10 × 302, 3 × 400 (by design), 43 × 404 (fabricated ids), **1 × 500** |
| 1b | Second crawl of **166 workflow URLs using the real ids created by the E2E run** | 141 × 200, 11 × 302, 3 × 400 (by design), 8 × 404 (records absent in the test DB), 1 × 500, 1 harness exception |
| 2 | **241-step money E2E**: project → stage → owner receipt → worker → timekeeping → advance/payment → payroll → supplier → material → purchase → delivery → usage → supplier payment → subcontractor (worker + payment) → office staff (ledger + expense) → tool (create + rental + payment + return) → expense → personal expense → CF register (in/out/transfer/amend/void/restore) → day close (count + lock) → Money Center APIs | 211 × 200, 12 × 302, **18 non-success**: 4 × 500 over 2 distinct URLs, 5 × 400 (all deliberate validation), 8 × 404 (missing records), 1 harness exception |
| 3 | Static analysis: every `url_for(...)` literal, every dynamic `route="…"` string, every URL literal in JS/templates, every `<form>` in 98 templates, every `render_template()`/`extends`/`include` target | 0 dangling `url_for` literals, but **14 of 22 Money-Center flow routes unbuildable** (4 non-existent endpoints, 9 needing args, 1 composite string) and 4 broken client-side URLs; 0 missing templates |
| 4 | Money invariants read directly from SQLite (ledger sums, derived balances, orphan ledger rows, minor-unit drift, CF entry ↔ ledger linkage) | orphan_txns 0, minor-unit drift 0 — but 1 real linkage gap (finding 5.2) |
| 5 | Guard tests: overdraft block, idempotency guard, day lock, back-dated entry rejection | all pass |
| 6 | Access-control test with `staff` and `accountant` roles | **fails** (finding 6.1) |
| 7 | `python -m unittest discover -s tests` (257 tests), `scripts/check_layers.py`, `scripts/check_db_safety.py`, `scripts/reorganize_frontend.py --check`, `tests/smoke_worker.py` | 1 test failure, 1 checker failure (both = red CI) |
| 8 | GitHub Actions status + `gh api .../jobs` step inspection of the last 4 runs | **main is red** |

---

## 2. Verdict at a glance

| # | Severity | Area | Issue | Where |
|---|---|---|---|---|
| 4.1 | **BLOCKER** | Money Center (newest feature) | Page returns **HTTP 500** for every user, always | `hdc/services/money_hub.py`, `templates/hdc/accounts/money_center.html:590` |
| 4.2 | **BLOCKER** | Pay supplier (money out) | "Record payment" on the supplier page returns **HTTP 500**; nothing is saved | `hdc/services/purchase.py:70`, `hdc/routes/purchase_v2.py:647` |
| 5.1 | **HIGH** | Money correctness | Paying a supplier from the Money Center records the money but **never reduces the payable** → supplier can be paid twice | `hdc/services/accounts.py:1936` |
| 5.2 | **HIGH** | Money correctness | Voiding a CF-register ledger row from *All Entries* leaves the CF document **active** → register, cash-flow report and ledger disagree with no warning | `hdc/services/cashflow_register.py:418-530` |
| 5.3 | **MEDIUM** | Audit trail | Void reason / user / timestamp are asked for but **never saved** | `hdc/services/accounts.py:1294` (+ `:1316-1317`) |
| 5.4 | **MEDIUM** | Reconciliation page | The app's own forensic check shows a **permanent false "orphan" finding** for every office salary payment | `hdc/services/accounts.py:30-192` + `hdc/services/ledger.py:180` |
| 5.5 | **MEDIUM** | KPIs | Material **usage** cost (real project spend) never appears in the Accounts spend KPIs | `hdc/services/accounts.py:2532` |
| 5.6 | **MEDIUM** | Day Close policy | A day can be locked with any difference (audited case: **‑492,000 PKR** locked silently) | `hdc/services/cashflow_register.py:1067` |
| 6.1 | **HIGH** | Security | A `staff`/`accountant` user can **post money** outside Accounts (worker pay, advance, expense, payroll, tool, office, subcontractor) | `hdc/routes/workers.py`, `payroll.py`, `expenses.py`, … |
| 6.2 | **MEDIUM** | Security / robustness | CSRF token is injected by JavaScript only — with JS off, **all 147 forms return HTTP 400** | `templates/hdc/shared/base.html:215-252` |
| 7.1 | **MEDIUM** | UI | 4 dropdown feeds on the Money Center hit **non-existent URLs** (404) | `templates/hdc/accounts/money_center.html:1201,1207,1213,1219` |
| 7.2 | **MEDIUM** | UI / exports | Real garbage in downloads: Excel `A1` = `HDC ERP â€” Project Report`, CSV title row likewise | `hdc/routes/reports.py:691`, `:577` |
| 7.3 | **MEDIUM** | UI | Accounts pages are 48–122 KB single files; `accounts.html` = 2,258 lines, **27 modals**, 77 KB inline JS → the "garbage on pages" feeling | `templates/hdc/accounts/*.html` |
| 7.5 | **MEDIUM** | UI / exports | Accounts CSV export writes the literal text `None` into the *Linked Entity* column | `hdc/routes/accounts_manage.py:309-329` |
| 8.1 | **MEDIUM** | CI / dev phase | **CI is red on main**: 1 failing test + 1 failing checker; the newest feature has **zero tests** | `tests/test_accounts_manage.py:515`, `scripts/reorganize_frontend.py` |
| 8.2 | LOW | Backend | `from hdc.models import *` raises `AttributeError` | `hdc/models/__init__.py:67` |
| 8.3 | LOW | Housekeeping | Empty junk files, dead JS, trailing-slash 404s, doc counts drift | see 8.3–8.6 |

**One-line verdict:** the ledger engine underneath is genuinely solid (overdraft block, idempotency, day lock, derived balances, minor-unit exactness all verified working) — but **the newest Money Center layer is dead on arrival**, **the supplier-payment path crashes**, and **two "single source of truth" promises are not actually kept** (5.1, 5.2). Fix steps 1–6 and the money side is trustworthy again.

---

## 3. Verified working — do not "fix" these

These were tested and behave correctly. If a fix step touches them, re-run the listed check.

| Area | Verified |
|---|---|
| Unified ledger | 16 auto-created ledger rows from every module; balances derived as `opening + in − out`; no stored balances (`/hdc/accounts`, `/hdc/accounts/manage`) |
| Minor units | `amount_minor == round(amount×100)` for **all** ledger rows and CF entries (drift = 0) |
| Orphans | No ledger row points at a missing account (`orphan_txns = 0`); no CF entry points at a missing txn (`cf_orphan_links = 0`) |
| **Overdraft block** | Paying out more than the account holds is refused: *"Insufficient balance in Company Cash."* / *"…Would be -500.00 PKR after this transaction. Overdraft blocked for treasury accounts."* |
| **Duplicate guard** | Same idempotency key posted twice → *"That entry was already recorded (duplicate submission ignored)."* (one row only) |
| **Day lock** | `lock_cash_day` requires a counted figure per account; afterwards a back-dated entry is rejected: *"Financial day 2026-09-20 is locked by system. Unlock it before this entry can be posted."* |
| CF register lifecycle | create in / out / transfer, amend (void + replace, both rows kept), void, restore — all correct, with an audit row each time |
| Owner receipt | `/hdc/projects/<pid>/owner_payment` → *"Payment recorded."* + `project_income` ledger row |
| Worker money | advance, payment (with shortfall/tip/excess guards), ledger void/restore, rate change |
| Payroll | run generation + per-worker manual pay with a real payable-balance check |
| Purchase V2 | supplier, material, purchase, delivery, usage, transfer, stock scoping — all posted correctly |
| Subcontractor | create, labour worker, crew payment (payable guard: *"Payment exceeds worker payable balance."*) |
| Office | staff create, ledger entry, office expense, allowance categories |
| Tools | tool create, rental, rental payment (posts `party_receipt` to the ledger), return, void |
| Exports | project report **xlsx / pdf / csv all return 200** (xlsx 9,565 B) — only the *title string* is garbage (7.2) |
| Templates | All 93 `render_template()` targets, all `extends`/`include` targets exist — no missing-template 500 hiding anywhere |
| Performance | Every audited page rendered in **< 150 ms** (worst: `/hdc/accounts` 139 ms) |
| Garbage-marker scan | All **114** no-argument GET pages rendered and their visible text stripped of markup: 103 × 200, 7 × intentional redirects (legacy aliases `/hdc/materials`, `/hdc/purchases`, `/hdc/stage-library`, `/hdc/accounts/transactions/find`, `/`, `/hdc/login`), 3 × 400 (by-design param validation), **1 × 500** (money center, 4.1). No `undefined`, `NaN`, `None`, raw `{{ }}`/`{% %}`, mojibake, `Traceback` or template leftovers in any page body — **except** the CSV export's `None` cells (7.5) |
| Dropdowns | The word `None` appears on `/hdc/accounts`, `/hdc/accounts/new`, `/hdc/accounts/manage` only as a legitimate `<option>None</option>` / `All entities` filter label — not a bug |
| Defensive inline JS | `/hdc/accounts/entries` references 37 element ids that exist only when the optional edit panel renders (`{% if edit_txn %}`). Static analysis of all 37 found **zero unguarded dereferences** (`if (el)`, `el?.`, `el && …`) — the page cannot throw on the default render. **Not a bug; no fix needed.** (Contrast: the tool-rental reference in §7.3 *is* dead.) |

**These HTTP 400/404s are correct — do not chase them:**

* `/api/v2/purchase/material-available`, `/api/v2/purchase/material-stock-scope`, `/api/v2/purchase/usage-po-options` → 400 without query params. That is deliberate validation (`hdc/routes/api_purchase.py:373,396,667`).
* `/hdc/accounts/money-center/api/quick-post` with a missing project → 400 *"Project is mandatory for outgoing payments."* Deliberate.
* The 43 crawl 404s are parameterised routes hit with fabricated ids (`/hdc/workers/1/ledger`, `/hdc/tool-rental/1`, …). The second crawl (Appendix A.1b) re-ran 166 of them with the **real ids created by the E2E run** — 141 returned 200/302, leaving only 8 × 404, all of them simply *missing records* in the test database: `/hdc/api/formula_vars/1`, `/hdc/expenses/1/edit`, `/hdc/office-management/allowance-categories/1/edit`, `/hdc/office-management/staff/1/allowances/1/edit`, `/hdc/personal-management/expenses/1/void`, `/hdc/settings/backup/x/download`, `/hdc/stage/drawing/1/view`, `/hdc_static/x`. All 7 app routes exist in the URL map (checked); they 404 only because there is no formula-var #1 / expense #1 / allowance #1 / personal-expense #1 / drawing #1 / backup file `x`. **Correct behaviour, not broken routes.**

---

## 4. BLOCKERS — fix these first

### 4.1 The Money Center page is dead (HTTP 500, always)

**Proof (server log from the crawl):**

```
500 /hdc/accounts/money-center
werkzeug.routing.exceptions.BuildError: Could not build url for endpoint 'hdc_owner_payment_new'.
  Did you mean 'hdc_owner_payment_receipt' instead?
  File ".../templates/hdc/accounts/money_center.html", line 590, in block 'content'
    {% if flow.route %}<a href="{{ url_for(flow.route) if flow.route.startswith('hdc_') else '#' }}" …>
```

**Root cause.** `hdc/services/money_hub.py` lists 22 money flows (`MONEY_FLOWS`, lines 55–520). The page renders an "Open Module" link for every one of them, calling `url_for(flow.route)` **with no arguments**. I validated all 22 flows against the live URL map with `url_for()` — **8 build, 14 raise `BuildError`**:

| money_hub.py line | flow id | route value in source | why `url_for()` fails |
|---|---|---|---|
| 61 | `owner_receipt` | `hdc_owner_payment_new` | endpoint does **not exist** (real one is `hdc_add_owner_payment`, needs `pid`) |
| 124 | `tool_rental_income` | `hdc_tool_rental_detail` | exists, needs `rental_id` |
| 166 | `supplier_refund` | `hdc_purchase_v2_supplier_detail` | exists, needs `supplier_id` |
| 189 | `worker_payment` | `hdc_worker_ledger` | exists, needs `wid` |
| 211 | `worker_advance` | `hdc_worker_ledger` | exists, needs `wid` |
| 232 | `worker_tip` | `hdc_worker_ledger` | exists, needs `wid` |
| 253 | `supplier_payment` | `hdc_purchase_v2_supplier_detail` | exists, needs `supplier_id` |
| 275 | `subcontractor_payment` | `hdc_subcontractor_ledger` | exists, needs `sid` |
| 297 | `subcontractor_labour_payment` | `hdc_subcontractor_labour_worker_ledger` | endpoint does **not exist** (real one is `hdc_subcontractor_worker_ledger`, needs `sid`+`wid`) |
| 318 | `office_staff_payment` | `hdc_office_staff_ledger` | exists, needs `sid` |
| 339 | `office_staff_advance` | `hdc_office_staff_ledger` | exists, needs `sid` |
| 423 | `purchase_paid` | `hdc_purchase_v2_detail` | endpoint does **not exist** (real one is `hdc_purchase_v2_page`) |
| 444 | `tool_purchase` | `hdc_tool_inventory` | endpoint does **not exist** (real one is `hdc_tool_rental_inventory`) |
| 467 | `transfer_company` | `hdc_accounts / hdc_cashflow_register` | not an endpoint at all (two URLs glued into one string) |

The other 8 build cleanly: `client_payment`, `party_receipt`, `cashflow_in`, `office_expense`, `material_expense`, `personal_expense`, `transfer_split`, `advance_person`.

Because Jinja renders **all** flows, the page raises on the very first one — `/hdc/accounts/money-center` and every POST to it return **HTTP 500** (`GET`, `POST`, both verified). The Money Center is reachable from the sidebar → *Accounts Hub* → "Open Money Center" (5 links), so it is the advertised front door for all money entry, and it is broken.

**Also dead while the page is down:** its whole "Direction → Type → Details" stepper, the pending-payable panel, treasury chips, timeline and the flow inventory — the deliverable of PR #25.

> Note: the page's *APIs* work fine (verified 200: `api/flows`, `api/pending`, `api/kpis`, `api/accounts`, `api/entry-config`, `api/diagram`) and the quick-post API posts correct ledger rows. Only the **page** is dead — which is why nobody noticed. See fix **Step 1**.

### 4.2 "Record supplier payment" always crashes and loses the payment

**Proof (server log):**

```
500 /hdc/purchase-v2/suppliers/1/payment [POST]
  File ".../hdc/routes/purchase_v2.py", line 647, in hdc_purchase_v2_supplier_payment
    _sync_supplier_po_payment_status(supplier.id)
  File ".../hdc/services/purchase.py", line 70, in _sync_supplier_po_payment_status
    .order_by(PurchaseV2.order_date, PurchaseV2.id)
AttributeError: type object 'PurchaseV2' has no attribute 'order_date'
```

**Root cause.** `PurchaseV2` has a column named **`date`**, not `order_date` (`hdc/models/materials.py`, table `hdc_purchase_v2` → `…, date, notes, challan_no, …`). The call happens **after** the `SupplierLedger` row is flushed and after the ledger transaction is posted, but **before** `db.session.commit()` — so the request raises, the session is rolled back, and **the payment is silently discarded** (verified: after the attempt, `hdc_supplier_ledger` still contained only the purchase debit row, no credit). Every user who clicks that button sees a raw 500 page and loses the entry.

Fix = one word (**Step 2**). This is the *only* place in the codebase referencing `order_date` (grep verified).

---

## 5. Money-correctness findings

### 5.1 (HIGH) Paying a supplier from the Money Center leaves the payable untouched

The Money Center quick-post accepts several transaction types. A supplier payment recorded as **`party_payment`** (the type the UI's "Pay to credit/debit party" / `pay_to_credit_debit` intent maps to) writes a ledger row but **creates no `SupplierLedger` credit row**, because the supplier-sync branch only runs for `expense_material` / `purchase` (`hdc/services/accounts.py:1936` onwards).

Measured on the audit database (supplier had one 120,000 PKR purchase):

| Action | Ledger row | `_supplier_balance(id)` | Payables report |
|---|---|---|---|
| quick-post `type=party_payment`, supplier selected, 5,000 PKR | ✔ created (`party_payment`) | **120,000 (unchanged)** | still shows 120,000 owed |
| quick-post `type=expense_material`, supplier selected, 5,000 PKR | ✔ created | **115,000** ✔ | 115,000 ✔ |

So the same business action ("pay this supplier 5,000") has two outcomes depending on which type the user picks, and the wrong choice leaves the supplier looking unpaid — inviting a **double payment**. Combined with 4.2 (the *correct* path crashes), the supplier-payment screen is unusable in both directions.

### 5.2 (HIGH) Void in All Entries does not void the Cash Flow document

The CF register is documented as "immutable: void + replace" and the ledger as "the single source of truth with source linkage". But the register's ledger row is created **without** `source_type`/`source_id` — `hdc/services/cashflow_register.py:498-530` calls `_create_account_transaction(payload)` with no source keys, and only links forward via `CashFlowEntry.account_tx_id`. Verified in SQLite:

```
txn 7  source_type=None  ← CF entry #1      txn 8  source_type=None  ← CF entry #2
```

Consequence — reproduced end-to-end:

1. Void ledger row `8` from **All Entries** (`_accounts_toggle_transaction_void_state`, `hdc/services/accounts.py:1294`).
2. Result: `hdc_account_txn.is_void = 1`, but the linked `hdc_cash_flow_entry` **stays active** — the CF Register, the Cash Flow report and the day-close totals keep showing a money document whose posting does not exist.
3. The app's own forensic scan cannot see it either: `SOURCE_MAP` (`hdc/services/accounts.py:38-53`) has no `cash_flow_entry` family, so `void_mismatch` stays `0`.

The reverse direction (void in the register → ledger row voided) works, which is why this stayed hidden. Fix ⇒ Step 3.

### 5.3 (MEDIUM) Void reason / user / timestamp asked for but never stored

* All-Entries void form (`templates/hdc/accounts/accounts_entries.html:224-228`) posts **only** `action` + `transaction_id` — there is no reason field.
* The route defaults the reason to `'Voided from Accounts'` and passes it in (`hdc/routes/accounts.py:156-165`), but `_accounts_toggle_transaction_void_state` (`hdc/services/accounts.py:1316-1317`) sets **only** `r.is_void` — `void_reason`, `voided_by`, `voided_at` are never written.

Verified: after voiding with a reason string, `void_reason` was `None`. The `AccountTransaction` model has all three columns (`hdc/models/accounts.py`), and `ROW_TRACEABILITY.md` promises the audit trail — so the report/audit columns on the receipts and All Entries are permanently blank for voids. The CF register path *does* write its own audit rows, so this is specific to the Accounts/All-Entries void path. Fix ⇒ Step 4.

### 5.4 (MEDIUM) The Reconciliation Check page always shows one false "orphan"

`_accounts_reconciliation_findings()` (`hdc/services/accounts.py:30-192`) treats every `OfficeExpense` row as needing its own ledger posting. But an office **salary payment** intentionally creates *two* source rows for *one* posting: the `OfficeStaffLedger` row **and** a mirror `OfficeExpense` row (`_sync_office_staff_expense_from_ledger`, `hdc/services/ledger.py:180-222`). The ledger row is sourced to the staff ledger, so the mirror is reported forever:

```
orphan_sources -> 1
   {'family': 'Office expense', 'source_type': 'office_expense', 'source_id': '1', 'amount': '40000.0'}
```

The helper that recognises these mirrors (`_is_linked_office_salary_expense`, `hdc/services/ledger.py:237`) exists but is **not used by the finding logic** (it is only used by the office routes to block editing). Result: a correct dataset shows a permanent red finding — users learn to ignore the health page, which defeats findings 5.2 and 5.4 both. Fix ⇒ Step 5.

### 5.5 (MEDIUM) Material usage never reaches the Accounts spend KPIs

`material_expense_total` counts ledger rows of type `expense_material` only (`hdc/services/accounts.py:2515-2535`). Purchase-V2 **usage** posts no ledger row at all — it stores `hdc_usage_log_v2.cost`. In the audit dataset, 40 bags × 1,200 = **48,000 PKR of real site material consumption** appeared in `UsageLogV2.cost` and in the project-cost aggregation, but the Accounts KPI tile reported `material_expense_total = 0.0` and `spent_total` ignored it.

This is a *design* split (stock movement vs. cash movement) rather than a crash — but the KPI is labelled "money spent", so the number a cost controller reads is incomplete. Decide and document (**Step 15.1**): either add usage cost to the material KPI as a separate memo line, or relabel the tile "cash-paid material".

### 5.6 (MEDIUM) Day Close will happily lock a half-million-rupee difference

`lock_cash_day` (`hdc/services/cashflow_register.py:1067-1153`) requires a counted figure per money account and *does* write an `AccountReconciliation` row for the difference ("Loss"/"Excess") — good. But it never asks for a reason or confirms a large gap. Audit run:

```
dayclose.lock → "Day 2026-09-18 verified and locked (difference -492,000.00 PKR)."
```

A 492,000 PKR loss was recorded with one click, no confirmation, no reason field. Recommend a threshold confirmation + mandatory reason above it (**Step 15.2**).

---

## 6. Security / access control

### 6.1 (HIGH) Non-admin users can post money outside the Accounts section

The Accounts section correctly enforces `_admin_only()` (verified: `staff`/`accountant` get redirected). **The rest of the money-writing app does not.** Logged in as `staff`, all of these returned 302 (success) and changed the database:

| Endpoint | Money effect | Rows before → after |
|---|---|---|
| `POST /hdc/workers/<id>/payment` | money OUT (wage payment) | `hdc_labour_ledger` 2 → 3 |
| `POST /hdc/workers/<id>/advance` | money OUT (advance) | `hdc_labour_ledger` 2 → 3 |
| `POST /hdc/expenses` | money OUT (site expense) | `hdc_expense` 0 → 1 |
| `POST /hdc/payroll/generate` (`action=pay_worker`) | money OUT (payroll) | (guard rejected only because balance was 0) |
| `POST /hdc/tool-rental/inventory` | creates assets/pricing | `hdc_tool` 1 → 2 |
| `POST /hdc/office-management/staff/ledger` | creates staff + salary | `hdc_office_staff` 1 → 2 |
| `POST /hdc/subcontractors` | creates contractors with contract values | `hdc_subcontractor` 1 → 2 |

Read access for non-admins (200 OK) also includes `/hdc/payroll`, `/hdc/workers`, `/hdc/expenses`, `/hdc/reports`, `/hdc/purchase-v2`, `/hdc/tool-rental/inventory`, `/hdc/office-management/staff/ledger`. Fix ⇒ Step 7 (define the role matrix, then enforce it; the codebase already has `_admin_only()` to copy the pattern from).

### 6.2 (MEDIUM) CSRF protection depends on JavaScript being enabled

`_csrf_protect` (`hdc/extensions.py:72-88`) rejects any POST without a matching token — good. But **147 of 163 POST forms** contain no `_csrf_token` field; the token is injected at runtime by an inline script in `base.html:239-252` (plus a `fetch` wrapper). Verified directly:

```
POST /hdc/workers  (no token) → 400 Bad Request "CSRF token missing or invalid."
POST /hdc/workers  (token)    → 302 OK
GET  /hdc/workers  → the rendered "Add Worker" form has no _csrf_token input
```

Impact: any environment where the inline script does not run (JS disabled/hardened browser, CSP added later, script error earlier on the page, a proxy stripping inline scripts, `curl`/integration clients) turns the entire app into HTTP 400 on every submit. Fix serverseitig — **Step 8** (inject the token into HTML responses, or add the hidden input via a template macro; keep the JS as belt-and-braces).

---

## 7. Frontend / UI

### 7.1 (MEDIUM) Dead dropdown feeds on the Money Center — 4 × 404

`templates/hdc/accounts/money_center.html` calls URLs that do not exist:

| Line | Called | Correct endpoint (verified) |
|---|---|---|
| 1201 | `/hdc/api/workers/options` | `/hdc/api/workers` |
| 1207 | `/hdc/api/suppliers/options` | `/hdc/api/suppliers` |
| 1213 | `/hdc/api/subcontractors/options` | `/hdc/api/subcontractors` |
| 1219 | `/hdc/api/office_staff/options` | `/hdc/api/office_staff` |

All four return 404 (HTML error page, not JSON), and the `.catch(()=>{})` in the page hides it — so the party selectors would be empty even after 4.1 is fixed. Fix ⇒ Step 6.

### 7.2 (MEDIUM) Garbage inside exported money reports

`hdc/routes/reports.py` has mojibake in **user-visible strings** (not comments):

* line 691 → Excel Summary sheet `A1` = `HDC ERP â€” Project Report` (confirmed by opening the generated `.xlsx` with openpyxl)
* line 577 → CSV header row = `HDC ERP â€“ Project Report`

The same files contain 23 comment-only mojibake dividers (harmless, but they are the source of the corruption). 17 files carry mojibake; only these two lines reach the user. Fix ⇒ Step 13.2.

### 7.3 (MEDIUM) Page structure — why it feels like "garbage on pages"

Measured per template (bytes / lines / inline `<style>` / inline `<script>` / inline `style=` / modals):

| Template | Bytes | Lines | Inline CSS | Inline JS | Modals |
|---|---|---|---|---|---|
| `accounts/accounts.html` | **122,086** | 2,258 | 348 B | **77,523 B** | **27** |
| `accounts/money_center.html` | 75,523 | 1,289 | 8,989 B | 21,588 B | 0 |
| `accounts/accounts_entries.html` | 47,937 | 920 | 727 B | 21,892 B | 0 |
| `projects/project_detail.html` | 42,515 | 668 | 0 | 277 B | 0 |
| `subcontractors/subcontractor_attendance.html` | 41,119 | 722 | 0 | 6,839 B | 0 |

The rendered Account workspace is **141,991 bytes of HTML for 4,822 characters of text** — i.e. ~97 % markup/script for one screen: KPI tiles + receivables + a double-entry form + an intent matrix + 27 modal dialogs. This is the concrete cause of "not simple cards and buttons", and it is also why the repo's own frontend-organisation check refuses the file (8.1). Fix ⇒ Step 12.

Also found: dead JS on `templates/hdc/tool_rental/tool_rental.html:378,388` referencing `#rentalProjectSelect` / `#rentalStageSelect` — a repo-wide grep finds **no element with that id anywhere**, so the project→stage filter sync never runs (harmless only because the reference uses optional chaining). A trap for whoever next edits the rental form.

### 7.4 (LOW) Trailing-slash URLs 404 instead of redirecting

`GET /hdc/workers/` → **404** (no redirect to `/hdc/workers`); same for `/hdc/projects/`, `/hdc/purchase-v2/…/`. With `url_map.strict_slashes = True`, any hand-typed/bookmarked/concatenated URL with a trailing slash dead-ends. Optional fix ⇒ Step 13.4.

### 7.5 (MEDIUM) The accounts CSV export shows `None` in a column

`/hdc/accounts/manage/export` (`hdc/routes/accounts_manage.py:309-329`) writes dict values straight into the CSV; for company/wallet accounts the "Linked Entity" value is the Python object `None`, which `csv.writer` stringifies literally:

```
ID,Account,Status,Category,Subcategory,Account Type,Channel,Legacy Type,Bank,Account #,IBAN,Opening Balance,Current Balance,Posted Txns,Linked Entity,Party,Created
9,Audit Wallet,active,Assets,Cash,Main Cash,Cash,cash,,,,0.00,1000.00,1,None,,2026-09-20
1,Company Cash,active,Assets,Cash,Main Cash,Cash,company,,,,0.00,494300.00,14,None,,2026-09-20
```

Two of the rows contain a `None` cell — exactly the sort of thing that gets read as "garbage" by the person opening the file. Fix ⇒ Step 13.6 (one-line per field: `or ''`).

---

## 8. Backend, models, housekeeping, CI

### 8.1 (MEDIUM) CI is red on `main`, and the newest feature has no tests

GitHub Actions, last 4 runs (all `failure`; last green was PR #24 `35511892558`):

```
35521481259  main  push          → job "verify" FAILED at step "Deploy automation tests"
                                    (later steps skipped: frontend organisation, node JS check, fresh-DB smoke)
35521397130  PR#25 pull_request  → failure
35521260794  PR#25 push          → failure
35520903551  PR#25 push          → failure
```

Local reproduction of the two failures:

1. `python -m unittest discover -s tests -p 'test_*.py'` → **`Ran 257 tests … FAILED (failures=1)`**
   `tests/test_accounts_manage.py:515 test_hub_explains_what_each_page_is_for` asserts `'What Each Page Is For'` **and** `'Cash Flow vs CF Register'` on `/hdc/accounts/hub`. The current `templates/hdc/accounts/accounts_hub.html` (rewritten by PR #25) contains neither phrase — grep finds them **only** in the test. The section the test guards was deleted, not replaced.
2. `python scripts/reorganize_frontend.py --check` → **AssertionError: `unmapped=['money_center.html']`** — the new template was never added to `FILE_TO_DOMAIN` (`scripts/reorganize_frontend.py:120`), so step "Check frontend organization" would fail as soon as the first step passes.

Additionally: **no test anywhere mentions `money_center`** (`grep -rn "money.center\|money_center" tests/` → 0 hits) although PR #25 added a 1,003-line service, a 378-line route module and a 75 KB template. `MONEY_CENTER_REPORT.md` marks all six acceptance criteria ✅ — including "Smooth UX from Accounts section" and "Sidebar primary entry with New badge" — while the page 500s and the sidebar has no Money Center entry. Two claims in a phase report that no test could ever have caught. Fix ⇒ Step 9 + Step 10.

### 8.2 (LOW) `from hdc.models import *` crashes

`hdc/models/__init__.py:67` lists `"SubcontractTeamAttendance"` in `__all__`, but the import line above never imports it (`hdc/models/subcontract.py:194` defines it):

```
>>> from hdc.models import *
AttributeError: module 'hdc.models' has no attribute 'SubcontractTeamAttendance'
```

Any star-import (or tooling that respects `__all__`) breaks. Fix ⇒ Step 13.1.
*(Repository-wide counts confirmed: 98 templates, 73 ORM classes, 74 tables, 242 endpoints, 70 modules scanned by `check_layers.py`, 25 route modules.)*

### 8.3 (LOW) Empty stray files

`Ngunga.txt` (root) and `hdc_erp/abc.txt` are **empty 1-byte files** committed to the repo (`hdc_erp/` contains nothing else). Remove both (**Step 13.3**).

### 8.4 (LOW) Timestamp convention — one raw-SQL exception

All Python code correctly uses the PKT helpers (`_pkt_now_naive`, `_pkt_today`); a grep for `datetime.now()`/`date.today()` in `hdc/` returns **zero** hits. The only deviation is 5 backfill statements in `hdc/core/schema.py` using SQLite `CURRENT_TIMESTAMP` (UTC). Cosmetic today, but it writes a UTC instant into `created_at` on migrated rows (**Step 13.4**).

### 8.5 (LOW) Dead/duplicated links & naming collisions (from the literal scan)

* `/hdc/office-management/staff/ledger` is the **staff-create** endpoint (`hdc/routes/office.py:67`) — creating staff from a page titled "ledger list" is confusing, and `POST /hdc/office-management/staff` (the obvious URL) returns **405**.
* Two live URLs look like duplicates to a user: `/hdc/accounts/cashflow` (report) vs `/hdc/accounts/cashflow/register` vs `/hdc/accounts/cashflow/report` (three cash-flow screens, two of which are described identically in the Hub).
* 4 client URLs and 5 endpoint names were dead (fix steps 1 & 6 cover them).

### 8.6 Docs drift (counts cannot be trusted as audit input)

| Claim in repo | Reality |
|---|---|
| README: "89 Jinja pages" | **98** templates |
| README: "61 ORM models" | **73** model classes |
| README: "check_layers enforces … 64 modules" | checker prints **70 modules** |
| README: "Cash Flow … `/hdc/accounts/cashflow/report`" etc. | 3 overlapping cash-flow pages exist |
| `MONEY_CENTER_REPORT.md`: "Acceptance Criteria Met ✅ … Smooth UX" | page returns 500 (4.1) |
| `MONEY_CENTER_REPORT.md`: "Sidebar primary entry with New badge" | sidebar has **no** Money Center entry (PR #25 removed it) |

---

## 9. Database audit

| Check | Result |
|---|---|
| Tables / models | 74 tables, 73 model classes; **only** `hdc_runtime_flag` has no model (created by `hdc/core/flags.py` — intentional) |
| Schema parity | No model without a table, no unexpected table |
| Migrations | `_bootstrap_hdc()` runs `db.create_all()` + 9 schema healers on every start; executed twice per boot in this audit with no error and no duplicate-column failure (idempotent) |
| Foreign keys | `PRAGMA foreign_keys=ON` per connection (`hdc/extensions.py:24`) |
| Journal/WAL | `journal_mode=WAL`, `synchronous=NORMAL` confirmed applied |
| Ledger integrity | `orphan_txns = 0`, `cf_orphan_links = 0`, `minor_unit_drift = 0`, `cf_minor_drift = 0` |
| Money sums | Cash account derived 492,000 = 500,000 receipt + 6,000 + 76,000 − 3,000 − 25,000 − 40,000 − 7,000 − 15,000 (matches the 9 ledger rows exactly) |
| Backups | `/hdc/settings/backup/<file>/download` is admin-only, uses `os.path.basename`, requires `.zip` — no traversal |
| Uploads | Stage drawings use `secure_filename` + instance-directory storage |
| Raw SQL | 5 `text(f"…")` sites (`core/admin.py:215`, `core/schema.py:672,761,1019,1028`) — table/column names come from internal lists, not user input (no injection path found) |
| Not verified | No load/concurrency test, no test against the real production DB file, no restore-from-backup drill |

---

## 10. Development phases — claim vs. reality

| Phase / doc | Claim | Reality |
|---|---|---|
| MODULARIZATION_PLAN | layer rule `routes → services/core → models → utils`, enforced by CI | ✅ `scripts/check_layers.py`: 70 modules, acyclic, OK |
| PRODUCTION_HARDENING | prod refuses to boot without secret/admin password; cookies HttpOnly/SameSite/Secure; security headers | ✅ verified in `hdc/config.py` + `hdc/app.py` (`X-Frame-Options`, `nosniff`, HSTS when secure) |
| CASHFLOW_MODEL | balances derived, never stored; void+replace; period locks | ✅ derived balances confirmed; ⚠️ void-sync gap (5.2) |
| ROW_TRACEABILITY | every list row shows who entered it; grey voids | ⚠️ implemented, but void reason/user/time are not persisted on the Accounts path (5.3) |
| LABOUR_AUDIT | `scripts/labour_audit.py` finds wage/payment/tip/advance drift | ✅ runs read-only; on the audit DB it correctly flagged HIGH `BALANCE_OVERPAID` (500 PKR) caused by my test paying more than earned — the tool works |
| TOOLS_TRACKING_SYSTEM | tools dashboard, positions, payments | ✅ tool flows verified end-to-end (payment posted to the ledger) |
| SUBCONTRACT_SIMPLE_ATTENDANCE_PLAN | crew-level attendance; per-person attendance intentionally dropped | ✅ implemented as documented |
| **MONEY_CENTER_REPORT (PR #25)** | unified money handling, all criteria met | ❌ **page 500s**, 14/22 flow links unbuildable, 4/5 option APIs 404, zero tests, CI red |

**CI/dev-process summary:** the pipeline is well designed (compile → layers → DB-safety → tests → frontend check → `node --check` → fresh-DB smoke) but it is **currently red**, and the failing test is exactly the guard for the Accounts Hub explainer section that PR #25 removed. Nothing in CI exercises the Money Center, which is how a 100 %-broken headline feature merged with a ✅ report.

---

## 11. Step-by-step fix plan — follow this in order

> Do one step at a time and run the "Verify" command. Steps 1–6 restore correct money behaviour; 7–8 are security; 9–10 restore CI/tests; 11–15 are hygiene and product decisions. After each step, `git commit -m "fix: …"` so a step can be reverted alone.

### Step 1 — Make the Money Center render (fix 4.1) — ✅ COMPLETED (2026-09-20)

> **Done.** All 14 unbuildable `MONEY_FLOWS` routes replaced with argument-free list endpoints, `safe_url_for` Jinja global added in `hdc/app.py`, and the `money_center.html` flow cards now use it. Verified: `GET /hdc/accounts/money-center` → **200**, all 22 flow routes build, 19 "Open Module" links resolve, `check_layers.py` OK.

**Files:** `hdc/services/money_hub.py`, `templates/hdc/accounts/money_center.html`, and a small helper in `hdc/app.py`.

1. In `MONEY_FLOWS`, replace every unbuildable `route` with the module's **argument-free** list page (verified endpoints):

| line | flow | new `route` | resolves to |
|---|---|---|---|
| 61 | owner_receipt | `hdc_projects` | `/hdc/projects` |
| 124 | tool_rental_income | `hdc_tool_rental` | `/hdc/tool-rental` |
| 166 | supplier_refund | `hdc_purchase_v2_suppliers` | `/hdc/purchase-v2/suppliers` |
| 189 | worker_payment | `hdc_workers` | `/hdc/workers` |
| 211 | worker_advance | `hdc_workers` | `/hdc/workers` |
| 232 | worker_tip | `hdc_workers` | `/hdc/workers` |
| 253 | supplier_payment | `hdc_purchase_v2_suppliers` | `/hdc/purchase-v2/suppliers` |
| 275 | subcontractor_payment | `hdc_subcontractors` | `/hdc/subcontractors` |
| 297 | subcontractor_labour_payment | `hdc_subcontractors` | `/hdc/subcontractors` |
| 318 | office_staff_payment | `hdc_office_staff_ledger_list` | `/hdc/office-management/staff/ledger` |
| 339 | office_staff_advance | `hdc_office_staff_ledger_list` | `/hdc/office-management/staff/ledger` *(same page)* |
| 423 | purchase_paid | `hdc_purchase_v2_purchases` | `/hdc/purchase-v2/purchases` |
| 444 | tool_purchase | `hdc_tool_rental_inventory` | `/hdc/tool-rental/inventory` |
| 467 | transfer_company | `hdc_accounts` | `/hdc/accounts` |

2. Add a defensive helper so a future typo can never 500 a page again — in `create_app()` next to the other Jinja globals (`hdc/app.py`):

```python
from flask import url_for
from werkzeug.routing import BuildError

@app.jinja_env.globals["safe_url_for"] = _safe_url_for   # module-level function

def _safe_url_for(endpoint, **values):
    """url_for that degrades to '#' instead of raising BuildError."""
    try:
        return url_for(endpoint, **values)
    except BuildError:
        return "#"
```

3. In `templates/hdc/accounts/money_center.html` replace the three occurrences of

```jinja
<a href="{{ url_for(flow.route) if flow.route.startswith('hdc_') else '#' }}" …>
```
with

```jinja
<a href="{{ safe_url_for(flow.route) if flow.route.startswith('hdc_') else '#' }}" …>
```

**Verify**

```bash
.venv/bin/python - <<'PY'
import os,sys; sys.path.insert(0,'.')
os.environ.update(HDC_DB_PATH='/tmp/v1.db', HDC_INSTANCE_DIR='/tmp/v1_inst', HDC_ENV='test',
                  HDC_SECRET_KEY='x', HDC_BOOTSTRAP_ADMIN_PASSWORD='Admin@1234')
from hdc.app import create_app
app=create_app(); c=app.test_client(); c.get('/hdc/login')
with c.session_transaction() as s: t=s['_csrf_token']
c.post('/hdc/login',data={'username':'admin','password':'Admin@1234','_csrf_token':t})
print(c.get('/hdc/accounts/money-center').status_code)   # must print 200
PY
```

### Step 2 — Fix the supplier payment crash (fix 4.2) — ✅ COMPLETED (2026-09-20)

> **Done.** One-word fix applied: `PurchaseV2.order_date` → `PurchaseV2.date` in `_sync_supplier_po_payment_status` (`hdc/services/purchase.py`). Verified end-to-end: supplier payment now returns *"Payment posted successfully."*, the `SupplierLedger` credit row persists (debit 120,000 → credit 30,000 → balance 90,000 → 0 after full pay), `supplier_credit_*` txn posted to the unified ledger, FIFO sync flips the PO to `paid`, and `order_date` no longer appears anywhere in `hdc/`. `check_layers.py` OK.

**File:** `hdc/services/purchase.py:70`

```diff
     pos = (PurchaseV2.query
            .filter(PurchaseV2.supplier_id == supplier_id, PurchaseV2.is_void == False)
-           .order_by(PurchaseV2.order_date, PurchaseV2.id)
+           .order_by(PurchaseV2.date, PurchaseV2.id)
            .all())
```

**Verify:** open a supplier with an unpaid purchase → *Record payment* → expect *"Payment posted successfully."* and:

```sql
select entry_type, amount from hdc_supplier_ledger where supplier_id = 1;   -- debit 120000, credit <paid>
select count(*) from hdc_account_txn where source_type like 'supplier_credit_%' and is_void = 0;  -- ≥ 1
```

### Step 3 — Keep the Cash Flow register in sync with the ledger (fix 5.2) — ✅ COMPLETED (2026-09-20)

> **Done.** `save_manual_cash_flow_entry` now back-links the ledger row (`tx.source_type = 'cash_flow_entry_<direction>'`, `tx.source_id = entry.id`); `_sync_source_row_void_state` gained a `cash_flow_entry_in|out|transfer` branch that mirrors `is_void`/`void_reason`/`voided_by`/`voided_at` onto the `CashFlowEntry` (keeping `account_tx_id` intact); `SOURCE_MAP` now covers all three CF families. Verified E2E: voiding/restoring the ledger row from *All Entries* now voids/restores the CF document (reason/user/timestamp persisted), a forced mismatch is caught by the forensic scan as `void_mismatch` ("Cash Flow entry (out)"), the register→ledger direction still works, all four cash-flow/entries/reconciliation pages render 200, and the audit SQL (`t.is_void == e.is_void`) holds. Full suite: 257 tests, only the pre-existing 8.1 failure remains.

**File:** `hdc/services/cashflow_register.py` (`save_manual_cash_flow_entry`, after `entry.account_tx_id = int(tx.id)`):

```python
    entry.account_tx_id = int(tx.id)
    # Back-link the ledger row to this document so Accounts-side voids and the
    # forensic scan can find each other (was missing → register/ledger drift).
    tx.source_type = f'cash_flow_entry_{entry.direction}'
    tx.source_id = int(entry.id)
    db.session.flush()
```

Then teach the void-sync and the scan about the new family:

1. `_sync_source_row_void_state` (`hdc/services/accounts.py:1123`) — add a branch for `cash_flow_entry_in|out|transfer`: load `CashFlowEntry`, set `is_void`, `void_reason`, `voided_by`, `voided_at`, and leave `account_tx_id` intact (mirror what it already does for `owner_payment`).
2. `_accounts_reconciliation_findings().SOURCE_MAP` (`hdc/services/accounts.py:36-52`) — add

```python
'cash_flow_entry_in':      (CashFlowEntry, 'Cash Flow entry (in)'),
'cash_flow_entry_out':     (CashFlowEntry, 'Cash Flow entry (out)'),
'cash_flow_entry_transfer':(CashFlowEntry, 'Cash Flow transfer'),
```

so `void_mismatch` / `orphan_txns` actually cover the register.

**Verify:** post a CF entry, void its ledger row from *All Entries*, then:

```sql
select t.is_void txn_void, e.is_void entry_void
from hdc_account_txn t join hdc_cash_flow_entry e on e.account_tx_id = t.id;
-- txn_void and entry_void must now match
```

### Step 4 — Persist the void audit trail (fix 5.3) — ✅ COMPLETED (2026-09-20)

> **Done.** `_accounts_toggle_transaction_void_state` now accepts `actor` and writes `void_reason`/`voided_by`/`voided_at` on void (clears them on restore); both Accounts route handlers pass `actor=current_user`. The All-Entries void form now has a required *Reason* input, voided rows stay visible on `/hdc/accounts/entries` (grey `table-secondary` row with the trail "Voided: reason · user · time" in the Meta column) via a new opt-in `include_void=True` on `_account_transaction_history` (dashboard/API behaviour unchanged), and a Restore button replaces Void on voided rows. Verified E2E: reason/actor/timestamp persisted and shown after void, cleared after restore, CF-register sync from Step 3 still holds, default-reason fallback works. Suite: 257 tests, only the pre-existing 8.1 failure.

1. `hdc/services/accounts.py:1294` — accept and store the reason/actor:

```python
def _accounts_toggle_transaction_void_state(txn_id, make_void=True, reason='', actor=None):
    ...
    for r in rows:
        r.is_void = bool(make_void)
        if make_void:
            r.void_reason = (reason or 'Voided from Accounts')[:300]
            r.voided_by   = (getattr(actor, 'username', None) or 'system')[:80]
            r.voided_at   = _pkt_now_naive()
        else:
            r.void_reason = None; r.voided_by = None; r.voided_at = None
```

(import `_pkt_now_naive` at the top of the module), and pass `actor=current_user` from `hdc/routes/accounts.py:159` and `:168`.

2. `templates/hdc/accounts/accounts_entries.html:224` — add the reason input the UI already implies:

```html
<input type="text" name="void_reason" class="form-control form-control-sm d-inline-block w-auto me-1"
       placeholder="Reason (required)" required>
```

**Verify:** void a row, reload `/hdc/accounts/entries` → the reason/user/time appear; restore → they clear.

### Step 5 — Stop the false "orphan" on the Reconciliation page (fix 5.4)

**File:** `hdc/services/accounts.py` (inside `_accounts_reconciliation_findings`, in the loop over `SOURCE_MAP` entries, before appending to `orphan_sources`):

```python
        # A salary payment legitimately creates TWO source rows (the staff
        # ledger row + its OfficeExpense mirror) for ONE posting.  Only the
        # ledger row owns the txn, so the mirror must not be reported.
        if base == 'office_expense' and getattr(src, 'office_staff_ledger_id', None):
            continue
```

**Verify:** with the audit dataset, `_accounts_reconciliation_findings()['orphan_sources'] == []`, and `/hdc/accounts/reconciliation` shows 0 findings.

### Step 6 — Repair the Money Center dropdown URLs (fix 7.1)

**File:** `templates/hdc/accounts/money_center.html:1201,1207,1213,1219` — drop the `/options` suffix (`/hdc/api/workers`, `/hdc/api/suppliers`, `/hdc/api/subcontractors`, `/hdc/api/office_staff`). The endpoints already return `{ok, items:[{id,label}]}` which is what the JS reads.

**Verify:** in the browser console on `/hdc/accounts/money-center` — no 404s; worker/supplier/staff selectors list rows. Or: `curl -b cookie 'http://host/hdc/api/workers' | head`.

### Step 7 — Close the money-write authorization hole (fix 6.1)

1. Add one helper in `hdc/extensions.py` beside `_admin_only()`:

```python
_MONEY_ROLES = {'admin', 'accountant'}

def _money_only():
    """Guard money-moving endpoints outside the Accounts section."""
    if (current_user.role or '').strip().lower() not in _MONEY_ROLES:
        flash('Admin/Accountant access required.', 'danger')
        return True
    return False
```

2. Apply `if _money_only(): return redirect(url_for('hdc_dashboard'))` at the top of every money **write** (POST) handler outside Accounts. Minimum list (verified reachable by `staff`):

* `hdc/routes/workers.py` — `hdc_worker_payment`, `hdc_worker_advance`, `hdc_worker_ledger_edit`, `hdc_worker_ledger_void`, `hdc_worker_ledger_restore`, `hdc_worker_rate`, `hdc_toggle_worker`
* `hdc/routes/payroll.py` — `hdc_payroll_generate` (POST), `hdc_payroll_delete`
* `hdc/routes/expenses.py` — expense create/edit/delete, alerts resolve
* `hdc/routes/subcontractors.py` — `hdc_pay_subcontractor`, `hdc_subcontractor_worker_pay`, team/labour attendance writes
* `hdc/routes/office.py` — staff create (`staff/ledger` POST), `hdc_office_staff_ledger`, `hdc_office_staff_payment`, `hdc_office_expenses` POST, allowance writes
* `hdc/routes/purchase_v2.py` — supplier purchase/payment/edit, delivery, usage, transfer
* `hdc/routes/tool_rental.py` — rental create/payment/return/transfer, inventory create/edit/delete

3. Write the decision down (README §Accounts & Cash): *"Money writes require role admin or accountant; staff can view operational pages that the admin decides to expose."*

**Verify:** repeat the E2E as `staff` — every money POST must return 302 to `/hdc/` **without** changing row counts (the test in Step 10 encodes this).

### Step 8 — Make CSRF work without JavaScript (fix 6.2)

Pick one; option (a) is the single-file fix and covers every current and future form.

**(a) Server-side injection** in `hdc/app.py` (inside `create_app`, after `register_all`):

```python
    import re as _re
    from hdc.extensions import _csrf_token

    _FORM_RE = _re.compile(r'<form\b[^>]*method\s*=\s*["\']post["\'][^>]*>', _re.I)

    @app.after_request
    def _inject_csrf_into_forms(response):
        if response.mimetype != 'text/html' or not response.is_streamed:
            return response
        if not session.get('_csrf_token'):
            return response
        body = response.get_data(as_text=True)
        if '_csrf_token' not in body:
            token = _csrf_token()
            def _add(m):
                tag = m.group(0)
                if '_csrf_token' in tag:
                    return tag
                return tag + f'<input type="hidden" name="_csrf_token" value="{token}">'
            response.set_data(_FORM_RE.sub(_add, body))
        return response
```

(keep the existing JS injection as a second layer).

**(b) Mechanical alternative** — add `{{ csrf_token }}` hidden inputs to the 147 forms (scriptable; more churn, same result).

**Verify:** with JS disabled (or `curl`), `GET /hdc/workers` must contain `name="_csrf_token"`, and a token-less POST must no longer be the only option; re-run the E2E (it posts forms without JS).

### Step 9 — Get CI green (fix 8.1)

1. **Restore the Hub explainer** in `templates/hdc/accounts/accounts_hub.html` — a section literally titled **"What Each Page Is For"** with rows for *Manage Accounts, CF Register, Cash Flow, Day Close, All Entries* and an explicit **"Cash Flow vs CF Register"** paragraph (register = where you record; cash flow = where you read). This satisfies `tests/test_accounts_manage.py:515-519` and is genuinely useful: PR #25 replaced it with sidebar-cleanup badges.
2. **Register the new template** so the frontend checker passes: add `money_center.html` to `FILE_TO_DOMAIN` (`scripts/reorganize_frontend.py:120` asserts `set(FILE_TO_DOMAIN) == all_files` and `sum(...) == 97` — bump that 97 to the new total, or drop the magic number in favour of `len(all_files)`).

**Verify**

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'     # Ran N tests ... OK
.venv/bin/python scripts/reorganize_frontend.py --check           # exits 0
gh run watch   # or re-push and let HDC CI go green
```

### Step 10 — Add the regression tests that would have caught all of this

New file `tests/test_money_center.py` (mirror the fixture style of `tests/test_cashflow_register.py`):

1. `GET /hdc/accounts/money-center` → **200** (catches Step 1 forever).
2. Every `route` in `MONEY_FLOWS` builds in a request context:

```python
def test_every_money_flow_route_builds(self):
    from hdc.services.money_hub import get_all_money_flows
    from flask import url_for
    with self.app.test_request_context('/'):
        for flow in get_all_money_flows():
            if (flow.get('route') or '').startswith('hdc_'):
                url_for(flow['route'])      # raises BuildError on regression
```

3. Supplier payment posts and reduces the payable (Step 2).
4. Void from All Entries voids the CF entry (Step 3).
5. `void_reason/voided_by/voided_at` are written (Step 4).
6. Quick-post with `party_payment` + supplier does **not** silently skip the payable — either it syncs or it returns 400 (see Step 15.3).
7. A `staff` client gets 302/403 on each money endpoint (Step 7).
8. Money Center JS URLs resolve → assert `/hdc/api/workers` etc. are 200 and the four `/options` strings are absent from the template.

**Verify:** `.venv/bin/python -m unittest tests.test_money_center -v`

### Step 11 — Prove the whole money model again (acceptance run)

Re-run the audit harness (Appendix B, item 2) and require:

* crawl: **0** responses ≥ 500 (currently 1);
* money pages/APIs: every one 200 (currently money-center 500);
* ledger invariants: `orphan_txns = 0`, `minor_unit_drift = 0`, `cf_orphan_links = 0`;
* `void` from either side keeps `hdc_account_txn.is_void == hdc_cash_flow_entry.is_void`;
* `_accounts_reconciliation_findings()` → all families 0 on a clean dataset;
* `tests/smoke_worker.py` → 87 reads / 0 errors.

### Step 12 — UI cleanup: make pages simple cards + buttons (fix 7.3)

Work page by page, biggest first; after each, re-measure bytes (target: ≤ ~30 KB rendered HTML per screen, no inline `<script>` beyond a few lines).

1. **`accounts/accounts.html` (122 KB, 27 modals, 77 KB JS)** — split into:
   * `static/hdc/js/pages/accounts_workspace.js` — KPI + intent matrix + form logic (move the inline block verbatim first, then trim);
   * replace the 27 modal dialogs with **one** reusable modal driven by `data-*` attributes (`data-modal-title`, `data-fields`) — the modals each re-declare mostly identical markup;
   * keep the visible layout as: 4 KPI cards → receivables card → **one** entry card (Direction → Type → Details) → recent rows table.
2. **`accounts/money_center.html` (75 KB, 9 KB CSS, 21 KB JS)** — move CSS to `static/hdc/css/money_center.css`, JS to `static/hdc/js/pages/money_center.js` (this also makes the flow inventory testable in Step 10.2).
3. **`accounts/accounts_entries.html` (48 KB, 22 KB JS)** — same split; leave the table + filters + void/restore buttons visible, move edit panel JS out.
4. Standardise on the existing Bootstrap classes: `.card` + `.btn btn-sm` + `.badge` (the Accounts Hub already does this well — copy its card markup). Remove the leftover per-page `<style>` blocks where `accounts.css` already has the class.
5. Delete the dead JS in `tool_rental.html:378,388` (or move it into the create page where the elements exist).

**Verify:** `.venv/bin/python scripts/reorganize_frontend.py --check`, byte counts re-measured, and each page still renders the same numbers (spot-check against the SQL in Appendix B.4).

### Step 13 — Housekeeping (LOW severity, all quick)

1. `hdc/models/__init__.py` — import `SubcontractTeamAttendance` from `hdc.models.subcontract` (it is already in `__all__`), then check: `python -c "from hdc.models import *"` must exit 0.
2. `hdc/routes/reports.py` — replace mojibake:
   * line 691 `'HDC ERP â€” Project Report'` → `'HDC ERP - Project Report'`
   * line 577 `'HDC ERP â€“ Project Report'` → `'HDC ERP - Project Report'`
   (Use plain ASCII — the same files carry `encoding='utf-8'` but the literal bytes are already mangled. Re-generate the xlsx/csv and confirm with openpyxl that `A1` is clean.)
   Optionally normalise the 23 decorative comment dividers in `routes/reports.py` plus the 16 other files listed in 7.2 to plain `# ----` ASCII, which removes the corruption source permanently.
3. `git rm Ngunga.txt hdc_erp/abc.txt` (both empty) and remove the now-empty `hdc_erp/` directory.
4. Optional: `app.url_map.strict_slashes = False` (or add `@app.route(..., strict_slashes=False)`) so `/hdc/workers/` redirects instead of 404-ing; and change the 5 `CURRENT_TIMESTAMP` backfills in `hdc/core/schema.py` to a Python PKT value.
5. Optional: rename the confusing staff-create URL — move it to `POST /hdc/office-management/staff` (the URL the UI already looks like it uses) and keep `/staff/ledger` as a redirect.
6. `hdc/routes/accounts_manage.py:309-329` — never let a `None` reach a CSV cell:

```diff
-                        a['txn_count'], a['linked_entity_label'], a['linked_party_name'],
+                        a['txn_count'], a['linked_entity_label'] or '', a['linked_party_name'] or '',
                         a['created_at']])
```
Apply `or ''` to `bank_name`, `account_number`, `iban` as well, then re-download the CSV and confirm no cell reads `None` (Step 13.2 style check).

### Step 14 — Truth-up the documentation (so the next audit isn't misled)

1. README: 98 templates (was 89), 73 models (was 61), 70 modules (was 64); add a "Money Center" row to the Accounts table and a line stating that money writes require admin/accountant (Step 7).
2. `MONEY_CENTER_REPORT.md`: add a *Corrections* section recording that the page returned 500 (BuildError on 14/22 flow routes) and that the four `/options` feeds 404'd — with the commit that fixed them. Keep the ✅ table but mark it "verified by `tests/test_money_center.py`".
3. Add to `PRODUCTION_HARDENING.md`: CSRF injection is now server-side (Step 8) and roles are enforced on money writes (Step 7).

### Step 15 — Product decisions to record (choose, then implement)

1. **Material usage in the spend KPI (5.5).** Either add `UsageLogV2.cost` as a *separate* memo tile ("material consumed, not yet paid") or relabel the existing tile. Whichever you choose, write it in `CASHFLOW_MODEL.md` so the tile's meaning is not ambiguous.
2. **Day-close difference policy (5.6).** Add a confirmation threshold + mandatory reason when `|difference| > threshold` (e.g. 5,000 PKR), and surface the value on the Hub card.
3. **Supplier payment — one path only (5.1).** Decide the canonical path:
   * keep `party_payment` and make it create the `SupplierLedger` credit (mirror the `expense_material` branch), **or**
   * block supplier selection for `party_payment` with a clear message ("use *Pay supplier/materials*").
   Either way, add the test from Step 10.6.
4. **Non-admin read matrix.** Decide which operational pages staff may *view* (today: payroll, workers, expenses, reports, purchase-v2, tools, office) and encode it in the same helper as Step 7.

---

## 12. Definition of done (tick these before closing the audit)

- [x] `GET /hdc/accounts/money-center` returns 200; all 22 flow links resolve (Step 1) ✅ 2026-09-20
- [x] Recording a supplier payment succeeds and reduces the payable (Step 2) ✅ 2026-09-20
- [x] Voiding from either side keeps ledger + CF document in the same void state (Step 3) ✅ 2026-09-20
- [x] Void reason/user/time persisted and visible (Step 4) ✅ 2026-09-20
- [ ] `/hdc/accounts/reconciliation` shows 0 findings on a clean dataset (Step 5)
- [ ] No 404s in the browser console on any Accounts page (Step 6)
- [ ] `staff`/`accountant` cannot post money outside Accounts; decision written down (Step 7)
- [ ] A form POST works with JavaScript disabled (Step 8)
- [ ] `unittest discover` → OK; `reorganize_frontend.py --check` → exit 0; **HDC CI green on main** (Step 9)
- [ ] `tests/test_money_center.py` exists and passes (Step 10)
- [ ] Full re-crawl shows **zero** responses ≥ 500 (Step 11)
- [ ] Excel/CSV report titles contain no mojibake (Step 13.2) and no `None` cells (Step 13.6)
- [ ] Every page renders with no 500 — verified by the 114-page text scan (Appendix B, step 3b)
- [ ] README counts and `MONEY_CENTER_REPORT.md` corrected (Step 14)

---

## 13. What this audit did not cover

* **No production data.** Everything ran against throwaway SQLite databases. Findings about *data* (e.g. how many supplier rows are affected by 4.2/5.1) need a read-only run of `scripts/labour_audit.py` and the SQL in Appendix B.4 on the live file — the script opens `mode=ro` so it is safe.
* **No browser/visual testing.** Layout, dark mode, mobile breakpoints, keyboard/ARIA and print fidelity were assessed from markup, byte counts and static JS analysis (which is how the `/hdc/accounts/entries` null-guard question was settled), not by looking at pixels. Step 12 should include a visual pass; the `#rentalProjectSelect` dead reference is the one JS item worth confirming in a console.
* **No load, concurrency or multi-worker testing.** `HDC_LOGIN_MAX_ATTEMPTS` throttling is per-worker (documented); SQLite + gunicorn multi-worker behaviour under concurrent posting is untested.
* **No penetration testing.** Only the specific checks listed in §6 and §9 were performed.
* **Backup/restore, deploy hook and PythonAnywhere setup** were read, not exercised end-to-end (`tests/test_deploy_hook.py`, `tests/test_pa_setup.py` pass).
* **Login throttling** was not brute-force tested.

---

## Appendix A — raw evidence

### A.1 Route crawl (166 GET routes, fresh DB, admin session)

```
200 → 109     302 → 10     400 → 3 (by design)     404 → 43 (parametrised/fabricated)     500 → 1
500  /hdc/accounts/money-center        ← BLOCKER 4.1
```

Full machine-readable dump: `/tmp/audit/crawl_result.json` (status + traceback per route; `total: 166`, `bad: 1`).

### A.1b Second crawl — 166 workflow URLs with real ids

```
200 → 141     302 → 11     400 → 3 (by design)     404 → 8 (missing records)     500 → 1     harness EXC → 1
500  /hdc/accounts/money-center   (the same blocker, reached from the crawl list)
```

### A.2 Money E2E — 241 steps, 18 non-success

```
steps 241 → 200 × 211 | 302 × 12 | 400 × 5 | 404 × 8 | 500 × 4 | harness EXC × 1   (18 non-success)

500  GET  /hdc/accounts/money-center                  ← BLOCKER 4.1
500  POST /hdc/accounts/money-center                  ← BLOCKER 4.1 (form post)
500  *    /hdc/accounts/money-center (crawl entry)     ← BLOCKER 4.1 (same URL counted in the crawl pass)
500  POST /hdc/purchase-v2/suppliers/1/payment        ← BLOCKER 4.2
400  POST /hdc/accounts/money-center/api/quick-post   (×2, "Project is mandatory for outgoing payments" — by design)
400  *    /api/v2/purchase/material-available | material-stock-scope | usage-po-options (missing params — by design)
404  *    missing records only — see the list in §3 (all 7 app routes verified present in the URL map)
EXC  *    /hdc/reports/project/1/xlsx  ← my harness decoded a binary xlsx as text; the route itself returns 200
```

Workflow steps that produced correct ledger effects (spot list): owner receipt 500,000 → `project_income`; worker advance 3,000 → `advance_to_person`; subcontractor payment 25,000 → `expense_subcontractor`; office salary 40,000 + office expense 7,000 → `office_management_payment` ×2; tool rental payment 6,000 → `party_receipt`; CF in 75,000 → amend 76,000 → void → restore.

### A.3 Money invariants (audit DB)

```
ledger by type: party_receipt ×3 = 82,000 | office_management_payment ×2 = 47,000
                project_income 1 = 500,000 | party_payment 1 = 15,000
                expense_subcontractor 1 = 25,000 | advance_to_person 1 = 3,000
Company Cash derived = 492,000  (matches the rows exactly)
orphan_txns = 0 | cf_orphan_links = 0 | minor_unit_drift = 0 | cf_minor_drift = 0
recon findings: orphan_sources = 1 (false positive, see 5.4); everything else 0
```

### A.4 CI evidence

```
35521481259  main  Merge PR #25   failure   job "verify" → step "Deploy automation tests" FAILED
                                            (frontend organisation, JS check, fresh-DB smoke skipped)
35511892558  main  Merge PR #24   success   ← last green on main
```

### A.5 Endpoint/URL inventory

```
endpoints in URL map          242
url_for literals in templates 170   (0 dangling)
dynamic route strings          14 unbuildable: 4 non-existent endpoints + 1 composite string
                               (money_hub.py 61/297/423/444/467) + 9 endpoints needing args
JS/template URL literals       37   (5 broken: 4 × /hdc/api/*/options + 1 my-parser artefact)
POST forms                    163   (147 without a server-rendered CSRF token)
render_template targets        93   (0 missing)      extends/include: 0 missing
```

---

## Appendix B — reproduce every claim

```bash
# 0. environment
cd /home/user/HDC-MAIN && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 1. crawl every GET route (prints the 500 and writes /tmp/audit/crawl_result.json)
.venv/bin/python /tmp/audit/crawl.py

# 2. full money E2E + money pages + invariants (writes /tmp/audit/e2e_result.json)
.venv/bin/python /tmp/audit/e2e.py

# 3. guard tests: overdraft, idempotency, day lock, back-dating
.venv/bin/python /tmp/audit/guards.py

# 3b. every no-arg page: status + visible-text garbage scan (writes /tmp/audit/textscan_all.json)
.venv/bin/python /tmp/audit/text_scan_all.py

# 4. money invariants by hand (against any DB, read-only)
sqlite3 -readonly /tmp/audit/e2e.db "
 select type, count(*), sum(case when is_void then 0 else amount end) from hdc_account_txn group by type;
 select count(*) from hdc_account_txn where amount_minor <> cast(round(amount*100) as integer);
 select t.id, t.is_void txn_void, e.id, e.is_void entry_void
   from hdc_account_txn t left join hdc_cash_flow_entry e on e.account_tx_id = t.id;"

# 5. project status checks the repo already ships
.venv/bin/python scripts/check_layers.py                     # OK (70 modules)
.venv/bin/python scripts/check_db_safety.py                  # OK
.venv/bin/python scripts/reorganize_frontend.py --check      # FAILS: money_center.html unmapped
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'   # 257 tests, 1 failure
.venv/bin/python tests/smoke_worker.py . /tmp/s/hdc.db /tmp/s/inst /tmp/s/out.json
.venv/bin/python scripts/labour_audit.py --db /tmp/audit/e2e.db   # read-only labour drift audit

# 6. CI status
gh run list --limit 10
gh api repos/rehmaahmed11/HDC-MAIN/actions/runs/<id>/jobs   # step-level pass/fail
```

*Audit harnesses live under `/tmp/audit/` (`crawl.py`, `crawl_result.json`, `e2e.py`, `e2e_result.json`, `endpoint_scan.py`, `forms.py`, `csrf_scan.py`, `bloat.py`, `guards.py`, `text_scan_all.py`, `textscan_all.json`) and are not part of the repo; Step 10 describes which of them should become permanent tests.*
