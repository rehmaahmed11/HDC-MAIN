# Cash Flow & Accounts model — ported from AMS

This document records what was taken from the **AMS** accounts/cash-flow model
(`rizwanahmedsora9-pixel/AMS`, `main` branch — `models/cash.py`,
`blueprints/accounts/*`, `app/services/accounting.py`, `app/services/cash_flow_svc.py`,
`utils/money.py`), how it was adapted to HDC, and — importantly — **which parts
were deliberately not ported and why**.

---

## 1. What was extracted

| AMS original | HDC port | Notes |
|---|---|---|
| `Account` + classification columns | `hdc_account` (extended) | additive `ALTER TABLE`, legacy `type` untouched |
| `Account.class_category / subcategory / account_type / channel` | same names | kept 1:1 so knowledge transfers |
| `blueprints/accounts/classification.py` registry | `hdc/services/account_classification.py` | re-voiced for construction (site cash, subcontractor payables, project income…) |
| `AccountTransaction` (from/to, void, idempotency, amount_minor) | `hdc_account_txn` (extended) | plus `reversal_of_txn_id` / `reversed_by_txn_id` |
| `CashFlowEntry` + category / subcategory / party | `hdc_cash_flow_entry`, `hdc_cash_flow_category`, `hdc_cash_flow_subcategory`, `hdc_cash_flow_party` | **plus HDC's `project_id` / `stage_id`**, which AMS has no equivalent for |
| `CashFlowEntryAudit` (before/after JSON) | `hdc_cash_flow_entry_audit` | same shape |
| `AccountReconciliation` (full carry chain) | `hdc_account_reconciliation` | same shape |
| `CashDayLock` / `CashDayAccountPosition` | `hdc_cash_day_lock` / `hdc_cash_day_position` | same shape |
| `utils/money.py` (minor units) | `hdc/utils/money.py` | extended to parse `1,25,000.50`, `Rs …`, `(250)` |
| `_assert_period_open` | `assert_period_open()` | same intent |

**New tables** (all `CREATE TABLE IF NOT EXISTS`, all additive):

```
hdc_cash_flow_category        user-managed heads (Material, Wages, Fuel …)
hdc_cash_flow_subcategory     optional second level (Cement, Steel / Saria …)
hdc_cash_flow_party           reusable counterparty names
hdc_cash_flow_entry           the register document  ← the main one
hdc_cash_flow_entry_audit     append-only before/after history
hdc_account_reconciliation    immutable per-account closing snapshot
hdc_cash_day_lock             one row per verified & locked financial day
hdc_cash_day_position         per-account daily position (opening → counted)
```

**New columns on existing tables:**

* `hdc_account` — `opening_balance_minor`, `class_category`, `class_subcategory`,
  `class_account_type`, `channel`, `cash_location`, `cash_responsible`,
  `wallet_*`, `linked_entity_type`, `linked_party_name`, `note`, `updated_by`, `updated_at`
* `hdc_account_txn` — `amount_minor`, `reversal_of_txn_id`, `reversed_by_txn_id`,
  `reconciliation_id`, `void_reason`, `voided_by`, `voided_at`, `reason`,
  `idempotency_key`, `updated_at`

Nothing was renamed, dropped or rewritten, so every existing report, import and
API keeps working. Verified by running the whole suite plus an in-place upgrade
of a synthetic pre-upgrade database.

---

## 2. Transaction style — what is actually better

This was the real question, so here is the reasoning rather than just the result.

### 2.1 Exact money: integer paisa, not float — **taken**

SQLite has no fixed-point type and HDC stores amounts in `FLOAT`. Floats
cannot hold paisa exactly, so `0.1 + 0.2 != 0.3` and a long ledger drifts.

AMS's answer — and it is the right one — is a **minor-unit mirror**: every money
column keeps an integer `*_minor` twin holding paisa, and the integer is
authoritative for arithmetic while the float remains the legacy/UI surface.
Rounding happens exactly once, at the boundary, half-up:

```python
to_minor('1,25,000.555')   ->  12500056      # not 12500055.999…
from_minor(12500056)       ->  Decimal('125000.56')
```

In HDC this is enforced by a `before_insert` / `before_update` listener on
`AccountTransaction`, so **every** module that posts to the unified ledger —
payroll, expenses, purchases, subcontract, office — gets the exact mirror for
free without knowing the cash flow layer exists. A one-off bootstrap backfill
(gated by the `cashflow_minor_backfill_done` runtime flag) fills historical rows.

### 2.2 Balance: derived, not stored — **AMS's approach NOT taken**

AMS keeps `Account.balance` / `balance_minor` on the account row and mutates it
with `_apply_account_tx_effect()` / `_reverse_account_tx_effect()` on every post,
edit and void.

HDC already **derives** balances by summing the ledger
(`opening_balance + incoming − outgoing`). We kept that. Reasons:

* **One source of truth.** A stored balance is a cache of the ledger. Every
  writer must remember to update it — a raw SQL backfill, a bulk import, a new
  module, or one missed `reverse()` on an error path silently corrupts it, and
  nothing detects it until the numbers stop adding up.
* **Self-healing.** A derived balance is correct by construction; fix the row
  and every balance in the system is fixed with it.
* **AMS's own history shows the cost.** That repo carries
  `tools/read_only/check_cash_flow_gaps.py`, `investigate_cash_flow_sources.py`,
  `remediation_dryrun_script.py` and a 22 KB `cash_flow_reconciliation_helpers.py`
  — a substantial amount of that tooling exists to hunt down drift between the
  stored balance and the ledger.

The cost is a `SUM()` per page instead of a column read. On SQLite with the
existing `(account_id, date, id)` indexes that is not measurable at this scale,
and the sums are now exact because they add integers, not floats.

> If list pages ever get slow, the right fix is a **cached snapshot recomputed
> on write plus a drift detector** — still derived, never authoritative.

### 2.3 Immutability: void + replace, never edit — **taken**

There were three options:

| Style | Behaviour | Verdict |
|---|---|---|
| Edit in place (HDC's old `AccountTransaction`) | row is overwritten | ✗ destroys evidence |
| Void only | row is flagged, money reversed | ✓ good, but no correction path |
| **Void + replace** | old row voided with a reason, new row posted, both linked | ✓ **chosen** |

In a cash business the audit question is never "what is the right number?" but
"who entered this, when, and who changed it and why?". Editing in place cannot
answer that. So:

* **Amend** = void the original (with the reason) **and** post a replacement.
  The pair is linked both ways (`amends_entry_id` ↔ `superseded_by_entry_id`),
  so the ledger reads as a chain, and an audit row stores the full before/after
  JSON.
* **Void** = flag + reverse. The row is never deleted.
* **Restore** = un-void (allowed while the day is open).

The `revision` counter on each entry counts state changes, and
`hdc_cash_flow_entry_audit` keeps every one of them with who/when/why.

### 2.4 Document vs posting — the biggest structural win

The most transferable idea in AMS is the split between the **document** and the
**posting**:

```
CashFlowEntry        "why"  — category, party, reference, description, who, why
      │  1 : 1
      ▼
AccountTransaction   "what" — money moved from account A to account B
```

A document can be superseded (that is normal paperwork); a financial posting is
append-only (that is what makes a ledger trustworthy). Keeping them separate is
what lets the app offer a friendly "Amend" button without ever rewriting
history.

### 2.5 Idempotency keys — **taken**

`CashFlowEntry.idempotency_key` (unique, indexed). A retried request or a
double-clicked form returns the existing entry instead of posting twice. Cheap,
and it removes a whole class of duplicate-money bugs that AMS guards against
with a much more elaborate duplicate-detection service.

### 2.6 Period locks — **taken**

Once a financial day is verified and locked, its counted figures are
authoritative. `assert_period_open()` blocks any post, void, restore or count on
that date; the day must be unlocked first. This is what makes the day-close
meaningful rather than decorative.

### 2.7 Reconciliation with a carry-forward chain — **taken**

Per day, per account:

```
opening → + money in → − money out → ± transfers → expected closing
                                                  → counted (physical)
                                                  → difference → next day's opening
```

When a day is locked, the **counted** figure — not the ledger figure — becomes
the next day's opening, which is the whole point of counting cash: the book
says 8,000, the drawer holds 7,900, and from tomorrow onwards the business
operates on 7,900. The 100-rupee gap is not silently absorbed; it is recorded
as an immutable `AccountReconciliation` snapshot typed `Loss` (or `Excess`).

Dormant accounts (no balance, no movement) are settled at zero instead of
blocking the close — a small pragmatic deviation so a day with nine accounts
does not need nine confirmations.

---

## 3. What was deliberately **not** ported

| AMS piece | Why not |
|---|---|
| `Account.balance` / `balance_minor` + `_apply/_reverse_account_tx_effect` | see §2.2 — makes the ledger a second source of truth |
| `__mapper_args__ = {'version_id_col': revision}` on `Account` | raises `StaleDataError` on any direct update; HDC updates accounts from several places, so it would surface as a 500. `revision` is kept on entries as an audit counter only |
| `AccountingAuditLog` (a parallel audit table) | HDC already records who/when for **every** table through the `after_flush` listener into `hdc_user_activity` (`ROW_TRACEABILITY.md`). A second audit log would duplicate it. We add `hdc_cash_flow_entry_audit` only for the part HDC's generic audit does **not** capture: before/after JSON and the free-text reason |
| `FbmCashDrawerEntry`, `FbmCashDrawerCategory`, `CashFlowDifferenceAdjustment` | HDC has no cash-drawer module, and `CashDayAccountPosition` + `CashDayLock` supersede the old "enter the difference" workflow (AMS itself has a legacy/new split here that its own docstrings call out) |
| `CashFlowReconciliationAudit` | folded into `hdc_cash_flow_entry_audit` + the existing `hdc_user_activity` trail |
| AMS's `utils/reconciliation.py` auto-repair daemon | HDC's reconciliation is an explicit human action on the Day Close page, not a background process |
| `Payment` / `SupplierPayment` / `BillCounter` / `SB-{NS}-{seq}` bill numbering | HDC has its own project/stage-scoped document model; porting AMS's numbering would fork it |

---

## 4. Guarantees the register now provides

1. **Exact to the paisa.** All arithmetic is integer paisa. Ten `0.10`
   postings total exactly `1.00`.
2. **Nothing is ever erased.** No delete, no edit-in-place. Every correction is
   a void plus a replacement, and both stay linked.
3. **Every change is attributed.** who / when / why, in
   `hdc_cash_flow_entry_audit`, plus HDC's generic `hdc_user_activity` trail for
   the "entered by" column on the page.
4. **No double posting.** Idempotency key + the existing recent-duplicate guard.
5. **No overdraft.** Treasury accounts (company/cash/bank) cannot go negative;
   the check runs against the derived balance before the row is written.
6. **No post-dated tampering.** A locked financial day rejects every mutation.
7. **Reconciliation is reproducible.** The full carry chain
   (previous → opening → movement → expected → actual → difference → final) is
   stored, so any closing can be re-derived without replaying history.
8. **Nothing else changed.** Existing screens, reports, imports and APIs are
   untouched; the new columns are additive and optional.

---

## 5. Layout

```
hdc/utils/money.py                       exact money primitives
hdc/models/cashflow.py                   8 new ORM models
hdc/models/accounts.py                   + additive columns, + minor-unit listener
hdc/services/account_classification.py   Category → Subcategory → Type registry
hdc/services/cashflow_register.py        the engine (post / amend / void /
                                         restore / reconcile / lock)
hdc/routes/cashflow_register.py          register, day close, CSV export
hdc/core/schema.py                       _ensure_cashflow_schema() migration
hdc/core/bootstrap.py                    wire-up + one-off backfill + seed
templates/hdc/accounts/
    cashflow_register.html               register + amend/void modals
    cashflow_reconciliation.html         daily cash & bank reconciliation
tests/test_cashflow_register.py          45 tests
scripts/seed_cashflow_register_demo.py            throwaway demo data
```

## 6. Using it

```
Accounts → CF Register     record / amend / void / restore, filter, export CSV
Accounts → Day Close       count each account, lock the day, carry forward
```

Amounts accept `4,500.25`, `1,25,000.50`, `Rs 12,000` and `(250)`.

To look at it on realistic data:

```bash
HDC_DB_PATH=/tmp/cf_demo.db HDC_INSTANCE_DIR=/tmp/cf_demo_inst \
    .venv/bin/python scripts/seed_cashflow_register_demo.py
HDC_DB_PATH=/tmp/cf_demo.db HDC_INSTANCE_DIR=/tmp/cf_demo_inst \
    .venv/bin/python hdc_erp.py
```

## 7. Migration notes

* Purely additive: `CREATE TABLE IF NOT EXISTS` + `ALTER TABLE ADD COLUMN`,
  driven by the existing `_ensure_table_columns_sqlite()` helper. No data
  rewrite, no downtime, no rebuild.
* On first boot after the upgrade: legacy accounts are auto-classified from
  their existing `type` (idempotent), historical ledger rows get their paisa
  mirror (one-off, flag-gated), and the category vocabulary is seeded.
* Verified against a synthetic pre-upgrade database: tables recreated, columns
  added, balances and history preserved.
