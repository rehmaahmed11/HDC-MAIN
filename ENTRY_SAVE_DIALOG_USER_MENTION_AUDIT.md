# Audit — entry-save dialogue box & user-name mention, in every app

**Scope:** every module of the ERP. Two features were audited end to end and
the gaps found in any module were filled:

1. **The entry-result dialogue box** — the centred modal that reports
   *Entry Saved / Entry Not Saved / Saved With a Warning* on every entry save
   (`static/hdc/js/core/dialog.js` + `templates/hdc/shared/_result_dialog.html`).
2. **The user-name mention on every entry** — the "Entered by" column / inline
   badge filled from the audit trail (`hdc_row_attrs` + `static/hdc/js/core/audit.js`,
   see [`ROW_TRACEABILITY.md`](ROW_TRACEABILITY.md)).

## 1. Entry-save dialogue box

### What the audit found

| Module | Dialog on entry save before |
|---|---|
| Purchase V2 (Purchases / Delivery / Usage) | ✅ mounted per page + `data-hdc-dialog` on the entry forms |
| Tool Txn | ✅ result payload mounted (form reports through `hdcDialog` from its own script) |
| **Everything else** (Accounts, Cash Flow, Money Center, Shared Expenses, Expenses, Materials, Office, Parties, Payroll, Projects, Subcontractors, Timekeeping, Workers, Users, Estimation, Settings, Tools) | ❌ only the flash banner at the top of the page |

The engine (`dialog.js`) was already loaded globally by `base.html`, but the
flash queue was only written into the JSON payload on the pages that mounted
`shared/_result_dialog.html` — everywhere else the operator got the easy-to-miss
banner strip and nothing else.

### What was added

* **Global mount.** `shared/base.html` now includes `shared/_result_dialog.html`
  once, right **after** the flash queue is popped (`get_flashed_messages()` can
  only be called once per render), so **every save in every module** reports its
  verdict in the dialogue box. The four per-page includes (3 purchase screens +
  tool txn) were removed to keep a single payload block.
* **`data-hdc-dialog` on the entry forms** — 150 forms across 69 templates in
  every module now opt in, so an incomplete entry is refused *before* the POST
  with the same box ("Entry Not Saved — *Supplier is required.*") and the
  offending field is focused.
  * Deliberately **not** marked: the login/logout forms, destructive
    `onsubmit="return confirm(...)"` action forms (their own confirmation is
    the report), `data-own-submit` / `data-hdc-txn-form` screens (tool txn and
    the new-transaction workspace post in the background and report through
    `hdcDialog` themselves), and the handful of forms with their own submit-time
    validation that can cancel the post (account edit, expense batch, quick
    material, new rental transfer mode, worker payment, stage-subcontractor
    picker) — marking those would leave a stuck "Saving…" label when their rule
    cancels a submit the guard already let through.

### Tests

`tests/test_purchase_entry_dialog.py` now pins the new invariant: the mount
lives in `base.html` after the flash pop, the three purchase entry forms keep
their `data-hdc-dialog` opt-in and page rules, **and a page outside purchase
(Materials) reports its save through the dialogue box too** — the old
"untouched pages" test was rewritten into that proof. The JS behaviour is still
checked by `tests/dialog_harness.js` under Node.

## 2. User-name mention ("Entered by") on every entry

### What the audit found

`hdc_row_attrs` was on 66 templates (verified previously at 2,778+ tagged
rows), but whole entry lists were untagged:

| Module | Untagged entry lists found |
|---|---|
| **Tools / Rentals (whole module)** | inventory tools, rentals, glance pieces, serial tracking, rental detail items / movements / returns, tracking movement logs, reports purchases / scraps / archived tools / rentals, tool position purchases / scraps, count-sheet history + count lines, serial-create rows, dashboard rows |
| **Shared Expenses** | ledger lines (expense + settlement), report rows, expense-detail share rows |
| **Parties** | directory rows |
| **Workers** | consolidated statement rows |
| **Accounts** | Money Center recent transactions, cash-flow reconciliation positions + day entries |
| Accounts hubs / reports / KPI pages | aggregate rows only (not entries) — left untagged on purpose |

### What was added

* **Service stamps** (so every consumer of the rows inherits the audit tags):
  * `hdc/services/shared_expenses.py` — every ledger line now carries
    `_hdc_entity: hdc_shared_expense` / `hdc_shared_settlement` + its id.
  * `hdc/services/tool_rental.py` — glance rows carry
    `_hdc_entity: hdc_tool_serial` + the piece id.
* **`hdc/services/actors.py`** — `_VIEW_ROW_KEYS` learned the directory/tool
  view-model keys (`party`, `serial`, `rental`, `holding`, `tool`, `audit`,
  `share`, `line`, `movement`, `log`, `return`, `transfer`), so
  `{{ row|hdc_row_attrs }}` resolves `row.tool`, `row.serial`, `row.party`,
  `entry.audit`, … automatically. A serial row prefers the piece over its tool.
* **Template tagging** — 20 templates piped their entry rows through
  `hdc_row_attrs` (86 templates tagged now): the whole Tools module list set,
  Shared Expenses ledger/report/detail, Parties directory, Worker statement,
  Money Center recent transactions, cash-flow reconciliation rows, and the
  saved count-sheet lines (tagged with the parent `hdc_tool_audit` — "who
  recorded the count"). Aggregate/KPI rows are still untagged on purpose: the
  filter renders nothing for them and the tooltip would be misleading.

Labour ledger rows on the statement resolve through their mirrors (time entry /
account posting / expense) exactly like the older worker ledger pages.

### Tests

`tests/test_row_traceability.py` grew five regression tests: worker statement
rows, tool inventory/glance rows, parties-directory rows, the shared-expense
line stamps (service + rendered ledger/report pages), and the view-model key
resolution order (serial beats tool, rental holdings, party, audit).

## Result

* Entry-save verdict: **every module** — one mount in `base.html`, 150 guarded
  entry forms.
* User-name mention: **every entry list** found in the audit now carries the
  "Entered by" column/inline badge with the full created/updated/voided trail in
  its tooltip.
* Full suite green: 988 passed, 5 skipped (1,096 subtests; see
  `qa/QA_HARDENING_REPORT.md` for how the suite is run).
