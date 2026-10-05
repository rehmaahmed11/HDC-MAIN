# HDC Tools discounts · worker tips &amp; advances · workers as parties

Three related changes that all answer the same question — *"where did the money
go, and can the ledger explain it?"*

1. [Discounts in HDC Tools payment handling](#1-discounts-in-hdc-tools-payment-handling)
2. [Worker tips, advances and one clear ledger](#2-worker-tips-advances-and-one-clear-ledger)
3. [Workers are parties too](#3-workers-are-parties-too)

---

## 1. Discounts in HDC Tools payment handling

### The problem

A rental could only be settled with cash. If the operator agreed to knock
2,000 PKR off a 10,000 PKR bill, the only options were to leave the rental
permanently "unpaid" or to book cash that never arrived. Both lied to the
accounts ledger.

### The model

A **discount is part of the bill the customer never pays**. It shrinks what is
owed exactly like cash does, but no money enters an account.

| Table / column | What it holds |
|---|---|
| `hdc_tool_rental_discount` | One row per concession — date, amount, reason, note, and where it came from |
| `ToolRental.total_discount` | Cached sum of the non-void rows for that rental |
| `ToolRentalPayment.discount` | The discount granted **in the same breath** as a payment |

Two flavours, one table, so the outstanding amount always has a documented
reason for shrinking:

* **Standalone / waive-off** (`payment_id` NULL) — "forget the last 2,000",
  granted on its own from the rental page.
* **Settlement discount** (`payment_id` set) — cash 8,000 + discount 2,000
  clears a 10,000 balance in one action.

```
outstanding = total_amount − total_paid − total_discount
```

### How it posts to Accounts

Money has one ledger (`hdc_account_txn`) and nothing is stored twice, so the
discount posts too — but as its own kind of row, because no cash moves:

| | Payment | Discount |
|---|---|---|
| Ledger type | `party_receipt` (existing) | **`discount_given`** (new) |
| Category | `income` | **`discount`** (new) |
| Money movement | customer account → Cash/Bank | customer account → *(nowhere)* |
| P&L effect | counts as income | deliberately counted in **neither** income nor expense |

`discount_given` is a first-class ledger type: it has an intent rule (required —
`tests/test_accounts_section_smoke.py` fails if a type has none), it is
filterable on **Accounts → All Entries**, and it is listed in
`_accounts_reconciliation_findings`' `SOURCE_MAP` so a voided discount whose
ledger row stayed alive is reported instead of drifting.

It is **not** offered on the New Transaction dropdown — that list is
`_ACCOUNT_TXN_FORM_OPTIONS`. Only the HDC Tools discount flow writes it.

### The rules the engine enforces

* A discount may not exceed what is still outstanding (checked *after* the
  cached totals are refreshed, so cash + concession together cannot exceed the
  bill).
* A `no_charge` rental cannot be discounted — there is nothing to discount.
* Voiding a payment voids the discount granted with it, and both are removed
  from Accounts together.
* Voiding a discount puts the amount back on what the customer owes.

### Where the discount had to be subtracted

Adding a discount is only half the job — every screen that answered "how much
is still owed?" had to learn about it, or the app would advertise a debt the
rental had already written off:

* `ToolRental.total_pending_amount` (rental detail, reports)
* `services/tool_rental.recalc_rental_totals` (payment status)
* `services/money_hub.get_pending_payables_detailed` (Money Center pendings)
* `services/tool_tracking.customer_dues_by_tool` (per-tool dues, dashboard)
* `services/tool_tracking.inventory_rows` totals
* the "Full Payment (All Pending)" toggle on the return form

---

## 2. Worker tips, advances and one clear ledger

### Tips vs advances vs payments vs settlements

A worker's money is five things at once, and the old screens showed them in
five different places. The definitions — which the code and the UI now both
state — are:

| Kind | Effect on what is owed | What it is |
|---|---|---|
| **Work earned** | `+` | Wage earned for attendance / time entries |
| **Advance** | `−` | Cash given **before** the wage was due |
| **Payment** | `−` | Cash paid **against** the wage owed |
| **Settlement** | `−` | Shortfall written off when paying less than owed |
| **Tip** | **0** | Gratis cash **on top** of the wage |

> **A tip never reduces what a worker is owed.** It is cash given as a gift, so
> counting it as payment would silently reduce the balance due. This rule was
> already enforced in `_worker_payable_snapshot`; it is now *explained* on both
> worker screens instead of being a footnote in the code.

### The new statement — `/hdc/workers/<id>/statement`

One chronological list with a running balance, answering "what happened to this
worker?" without visiting four tabs.

* every kind of entry in one table, colour-coded, with earned / advance /
  paid / tip / settled columns;
* **filters**: date range, entry types, show-voided;
* the running balance is computed over the **whole** ledger, so narrowing a
  filter never changes what a row says the worker was owed on that day;
* **printable** (Print / Save PDF hides the chrome and prints a header);
* a plain-language legend on the page — what each kind of row means and why
  tips are neutral.

Reachable from the Workers list, the classic ledger page, and the Parties
directory.

### The classic ledger — `/hdc/workers/<id>/ledger` — kept and improved

Nothing was removed. The page gained:

* six KPI cards instead of four, so **Tips** and **Settled** are no longer a
  footnote under "Total Paid";
* an **entry-type filter** ("show me only the advances" is the question an
  operator actually asks when a worker disputes a figure);
* a one-line readable legend for the money columns;
* a link to the new statement.

---

## 3. Workers are parties too

### Why

Every advance, payment, tip and settlement is money moving between the company
and a named person. Workers were the one group of counterparties missing from
the **Parties** directory, so a worker could not be picked on a Party / Person
field and the directory never answered "who did we pay this month?".

### How

`hdc.services.cashflow_register.sync_workers_as_parties()` files every
`Worker` in `hdc_cash_flow_party` with `party_type='worker'` (label
*Worker / Labour*). It is deliberately conservative:

* **idempotent** — safe on every page load; only creates what is missing;
* **non-destructive** — a name already filed as (say) a lender stays a lender;
  `ensure_party` never reclassifies a specific type;
* **reviving** — a deactivated row is reactivated, because the worker is still
  on the books;
* **renames follow** — the new name is added and the old party row is left
  alone, because the ledger is immutable and a rename must not rewrite history.

It runs automatically when the Parties directory is opened, when a worker is
created or renamed, and when a worker is paid. **Sync Workers** re-runs it on
demand.

### Where workers appear

Per your instruction, workers are filed under the existing **Other Parties**
bucket — no fourth group was added. They are identified there by:

* the *Worker / Labour* type badge;
* a `W-001`-style worker-code chip;
* **Advances / Paid / Tips / Owed** money columns;
* a direct link to **Statement** and **Ledger**.

A **Workers** KPI card on the directory shows how many of the workers on the
books are present (`n / total on the books`).

### The previous worker settings are untouched

The **Workers** module is unchanged: trades, codes, rates, attendance, payroll
and the classic ledger all behave exactly as before. Workers appearing in
Parties is additive — it gives the Party / Person pickers a fuller vocabulary,
it does not move any worker functionality.

---

## Files touched

| Area | Files |
|---|---|
| Schema | `hdc/core/schema.py` (`_ensure_tool_rental_schema`) |
| Models | `hdc/models/tool_rental.py`, `hdc/models/__init__.py` |
| Services | `hdc/services/tool_rental.py`, `hdc/services/accounts.py`, `hdc/services/ledger.py`, `hdc/services/cashflow_register.py`, `hdc/services/money_hub.py`, `hdc/services/tool_tracking.py` |
| Routes | `hdc/routes/tool_rental.py`, `hdc/routes/workers.py`, `hdc/routes/parties.py` |
| Templates | `templates/hdc/tool_rental/tool_rental_detail.html`, `templates/hdc/tool_rental/tool_dashboard.html`, `templates/hdc/workers/worker_statement.html` (new), `templates/hdc/workers/worker_ledger.html`, `templates/hdc/workers/workers.html`, `templates/hdc/parties/parties_directory.html` |
| Tests | `tests/test_tool_rental_discount.py` (new, 13 tests), `tests/test_workers_as_parties.py` (new, 21 tests) |

Run them with:

```bash
python -m unittest tests.test_tool_rental_discount tests.test_workers_as_parties
```
