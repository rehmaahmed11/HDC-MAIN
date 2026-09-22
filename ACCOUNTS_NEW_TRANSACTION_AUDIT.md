# Accounts → New Transaction — Audit

**Scope:** the Accounts section's New Transaction form
(`/hdc/accounts/new-transaction` and the identical entry card on
`/hdc/accounts/cashflow/register`), with a focused answer to the question
**"why do we need the Party field for a project payment received?"**

**Method:** every finding below was reproduced by running the real application
against a fresh SQLite database (admin session, real HTTP POSTs through the
actual routes), not by reading code alone. The existing suite was run first as a
baseline: `tests.test_new_transaction_form` → **29 tests OK**, and
`node tests/new_transaction_harness.js .` → **11 checks passed**. So nothing here
is a "the tests are broken" finding — the form does exactly what it was built to
do. The problem is what it was built to do.

**Verdict in one line:** the form is well-built as a *cash-flow register* entry
screen, but it is **not** wired into the project-receivable system, and the Party
field is the load-bearing reason why — it is the only thing the engine uses to
decide which ledger account the money comes from, while the Project field it sits
next to is decorative.

---

## 1. The direct answer: why the Party field exists for a project payment

The Party field is **not** there for reporting or for tidiness. It is there
because HDC's ledger is double-entry, and on a *Money In* entry the engine has no
other way to find the account the money came **from**.

`hdc/services/cashflow_register.py:623-626` — on every Money In / Money Out entry:

```python
if direction in (CF_DIR_IN, CF_DIR_OUT):
    counterparty = _cf_resolve_counterparty_account(party_name)
    if counterparty is None:
        raise ValueError('Unable to resolve the counterparty account for this entry.')
```

and `_cf_resolve_counterparty_account` (`:440-455`) resolves it **from the party
name string and nothing else**:

```python
nm = (party_name or '').strip()
if nm:
    acc = _accounts_party_account(nm, 'person')   # get-or-create a person account
    if acc is not None:
        return acc
return _accounts_default_external_parties()        # else: "Credit/Debit Control"
```

So on a project payment received:

| Party field | What the money is booked as coming from |
|---|---|
| filled in | a ledger account auto-created under that **name** |
| left blank | the shared **`Credit/Debit Control`** bucket |

The `project_id` the user carefully picked is written onto the `CashFlowEntry`
and onto the ledger row as a *tag*, but it is **never** used to resolve the
counterparty. The Party field is doing the job the Project field should be doing.

**Confirmed by running it.** A 500,000 receipt on category *Owner / Client
Receipt* → subcategory *Project Payment*, project *JPS*, Party left blank (the
form allows this — `party_mode` for this category is `optional`):

```
entry#1 in 500,000 party=None/other project=1
    tx#1 type=party_receipt cat=income
         from=Credit/Debit Control[person] -> to=Main Cash[cash]
         project=1 related=None:None
```

Money arrived. Nobody paid it.

### Why that is the wrong design for this specific transaction

For a **project payment** the counterparty is not an open question — it is
`Project.client`, which the system already stores. Compare the other route that
records exactly the same real-world event, the Projects page owner-payment form
(`hdc/services/accounts.py:3367-3392`):

```python
client_name = _normalize_name_ci((project.client if project else '') or 'Client')
client_acc  = _accounts_party_account(client_name, 'client')
```

It derives the counterparty **from the project**. No party field, no chance of a
blank, no chance of a typo. That is the correct shape, and the New Transaction
form does not use it.

**So: the Party field is needed on this form only because the form asks the
generic register question ("who is the counterparty?") instead of the specific
one the category already answers ("which project, and therefore which client?").
Fix the project→client derivation and the Party field stops being required
information for this category — it becomes what it should be, an optional
override.**

---

## 2. The bigger finding this exposes: a project payment recorded here never
   reaches the project

This is the serious one, and it is a direct consequence of the same wiring.

`Project.total_received` / `remaining_receivable` are computed **only** from
`OwnerPayment` rows (`hdc/models/projects.py:83-89, 176-180`). The New
Transaction form never creates one. Nothing anywhere reads
`CashFlowEntry.project_id` for project receivable maths — verified by grepping
`aggregation.py`, `reporting.py`, `models/projects.py`, `routes/projects.py` and
`routes/reports.py`: **zero references**.

### Measured, same 200,000 receipt, two routes

| | **New Transaction form** | **Projects page → owner payment** |
|---|---|---|
| `OwnerPayment` row | ✗ none | ✓ created |
| `CashFlowEntry` (register) | ✓ created | ✗ none |
| ledger txn type | `party_receipt` | `project_income` |
| counterparty account | `Abdul Rehman` **type=person**, `auto_source='worker'` | `Bilal Khan` **type=client**, `auto_source='client'` |
| `related_entity_type` | `None` | `project:1` |
| `project.total_received` | **0** | **200,000** |
| `remaining_receivable` (contract 1,000,000) | **1,000,000 — unchanged** | 800,000 |
| shows on the project detail page | **✗ no** | ✓ yes |
| counted in the Receivable KPI | **✗ no** (person → `credit_debit` group) | ✓ yes (client → `project_in_flow`) |
| over-receipt guard | **✗ none** | ✓ *"Amount exceeds project receivable pending"* |
| duplicate guard | **✗ none** (only same-key idempotency) | ✓ `_has_recent_duplicate` |

The account-type divergence is not cosmetic. `_account_group_mode_for_row`
(`accounts.py:448-453`) routes `client` → `project_in_flow` and `person` →
`credit_debit`, and the Receivable KPI sums only `project_in_flow`
(`accounts.py:2813-2815`). Proven directly: two otherwise identical 300,000
receivable positions, one on a `client` account and one on a `person` account →
`Receivable KPI total = 300,000`, not 600,000. **The form's half is invisible to
the KPI.**

And there is a **third** surface. `POST /hdc/accounts` with
`type=project_income` *does* create the `OwnerPayment`, *does* post
`from=Abdul Rehman[client]`, and *does* move `total_received` to 250,000 — but
writes no `CashFlowEntry`, so it is invisible to the Cash Flow register. Three
ways to record one event, three different outcomes, no two agreeing.

### What an operator actually sees

12 project receipts booked through the form with the Party field left blank
(3 clients, 3 projects, 100,000 each):

```
Credit/Debit Control balance = -1,200,000   (one anonymous bucket)
  Project Alpha (Abdul Rehman): total_received = 0
  Project Beta  (Bilal Khan):   total_received = 0
  Project Gamma (Chaudhry Sons):total_received = 0
```

Every project still shows the full contract outstanding. The cash is in the bank
and the books say no client has ever paid.

---

## 3. Other defects found in the form

Ordered by how much money they can move.

**3.1 — No over-receipt guard.** Contract 1,000,000; posted 900,000 then
500,000. Both **saved**. Total received 1,400,000 against a 1,000,000 contract,
with no warning. The Projects route refuses this (`accounts.py:2752-2755`).

**3.2 — No duplicate guard.** The same receipt (same project, account, amount,
date, reference `CHQ-77`) submitted from two page loads → **two entries**. The
`_idempotency_key` only stops a double-click on *one* rendered form; it cannot
stop the same cheque being entered twice. The Projects route blocks this.

**3.3 — Party ≠ the project's client is accepted silently.** Project *Alpha*
(client `Abdul Rehman`), receipt booked against party `Bilal Khan` → **SAVED, no
complaint**. The client sub-ledger now shows Bilal Khan paying for Abdul Rehman's
project. Nothing cross-checks `party_name` against `Project.client`.

**3.4 — Free-text party names fork the ledger.** Four spellings of one client
(`Abdul Rehman`, `abdul rehman`, `Abdul Rehman Sb`, `A. Rehman`) → **3 new
`person` accounts and 3 new `CashFlowParty` rows** (the case-variant correctly
matched). One client, four ledger identities, no merge tool.

**3.5 — The client's ledger account is created with the wrong type.**
`_cf_resolve_counterparty_account` hardcodes `'person'` (`:452`), and
`_accounts_party_account` then stamps `auto_source='worker'`
(`accounts.py:3363`). A **client** is filed in the ledger as a **worker**. This
is what causes the KPI blindness in §2.

**3.6 — The party-type guard is bypassable exactly when it matters.** The
category restricts parties to `client`. A *known* supplier is correctly refused
("*Owner / Client Receipt is for Client / Owner…*") — but a **brand-new** name
defaults to `other`, and `party_type_allowed` always lets `other` through
(`:1018-1022`). So the guard stops you misfiling an existing supplier and waves
through an unclassified stranger. Combined with 3.4, the easy path (type a new
name) is the unguarded one.

**3.7 — A "Project Payment" can be saved with no project.** `project_mode` is
`optional`, so subcategory *Project Payment* with `project_id=''` **saves**. The
subcategory names a project; the form does not require one.

**3.8 — Completed projects are offered.** `entry_form_context` loads
`Project.query.order_by(...)` with **no status filter**
(`transaction_entry.py:274`). A project marked `Completed` appears in the picker
and accepts money.

**3.9 — Money Out with a project never reaches project cost.** Same root cause,
other direction: a 50,000 *Material & Purchase* entry tagged to a project leaves
`Expense` rows at 0 and `project.total_cost` at 0.0. The project P&L does not see
it.

**3.10 — Dead element id.** `new_transaction.js:134` reads
`byId('txnAccountField')`; the template renders no such id (confirmed absent
from the served HTML). `accountField` is therefore always `null` and the
`accountField.hidden = false` guard at `:214` is a no-op. Harmless today only
because the field is never hidden — but `tests/new_transaction_harness.js:369`
*creates* that node, so the harness passes a case the browser cannot reach. The
harness is greener than reality.

**3.11 — The hidden `party_type` input is never synced by the picker.** It
renders as `other` and the only two assignments (`:373`, `:688`) fire on *clear*
and on *add-new*, never on selecting an existing party. Every normal submission
posts `party_type=other`. The server repairs it from the party master, so it is
currently harmless — but the client-side type filter is reasoning about a value
the form never actually sends.

**3.12 — Documented rules have no UI.** Five code comments and
`NEW_TRANSACTION_REPORT.md` say an operator can tighten these rules in
"Settings → Cash Flow" without a deploy. `update_cf_category` (which sets
`party_mode` / `project_mode` / `party_types`) has **zero callers**, and no route
or template exposes those fields. The rules are editable in the schema only.

---

## 4. Root cause

One sentence: **the form treats a project payment as a generic party receipt.**

The register engine was designed for "someone gave us money" — party-first,
project optional, `project_id` a tag. A project payment is a different event:
project-first, client derived, and it must debit the receivable. The form maps
the second onto the first, which is why the Party field is carrying weight it
should not carry and the Project field is carrying none.

The category system already knows the difference — *Owner / Client Receipt*
carries `party_types=('client',)` and subcategory *Project Payment* — but that
knowledge stops at the UI. Nothing downstream acts on it.

---

## 5. Recommendations

**Fix 1 — derive the counterparty from the project (removes the need for Party).**
In `_cf_resolve_counterparty_account`, when the entry has a `project_id` and the
category is a client-receipt category, resolve
`_accounts_party_account(project.client, 'client')` — the same call the Projects
route makes. Falls back to the typed party, then to the control account. This
alone fixes 3.3, 3.4 and 3.5 and demotes Party to a genuine optional override.

**Fix 2 — make the category's `loan_effect` pattern general, and add a
`project_effect='receipt'`.** The engine already mirrors loan categories into the
loan ledger via `_cf_apply_loan_effect` (`:667-675`). The identical hook should
create the `OwnerPayment` for a client-receipt category, inside the same
transaction, so an entry can never exist without its receivable movement. That
closes §2 and 3.9 without a second posting path.

**Fix 3 — make Project required, and Party optional, for this category.** Change
the seeded rule from `('optional', 'optional', 'client', '')` to
`('optional', 'required', 'client', '')`. One line at
`cashflow_register.py:1646`. Fixes 3.7.

**Fix 4 — reuse the existing guards.** Call the same pending-snapshot check and
`_has_recent_duplicate` the Projects route uses (3.1, 3.2), and filter the
project picker on active status (3.8).

**Fix 5 — the small ones.** Delete or render `txnAccountField` (3.10), sync
`party_type` on combo selection (3.11), and either build the Settings UI or
correct the docs (3.12).

**Sequencing:** Fixes 1–3 are the money-correctness set and belong together, with
a backfill decision for entries already posted. Note that any backfill must not
double-count against the third surface (`POST /hdc/accounts` with
`project_income`), which already writes `OwnerPayment` rows.

**Prerequisite — decide the model first.** Three surfaces currently record this
event three ways. The real fix is choosing one system of record (I would make the
register's `CashFlowEntry` the document and have it emit the `OwnerPayment`, per
Fix 2) and routing the other two through it. Patching the form alone would make
four.

---

## 6. What I verified vs. what I did not

**Verified by execution:** every table and code block above — counterparty
resolution, the three-route comparison, the KPI grouping, the over-receipt and
duplicate gaps, party/project mismatch, name forking, the `other` bypass,
project-less "Project Payment", completed projects, the missing `Expense` rows,
the absent DOM id, the unsynced `party_type`, and the zero-caller
`update_cf_category`.

**Not verified:** behaviour on the production dataset (all runs were fresh
SQLite); no browser/device testing; I did not assess how many historical entries
are already affected, which matters for any backfill.

**No code was changed.** This is an audit; `git status` is clean apart from this
report.

---

## 7. Fix as built

Sections 1–6 are the audit as first delivered. This section records what was
subsequently **implemented and tested** in response to it.

### The rule

A category can now be tagged `project_effect='receipt'`, exactly mirroring the
existing `loan_effect` hook. `'Owner / Client Receipt'` carries that tag, and on
it the engine enforces:

| Before | After |
| --- | --- |
| Project optional — receipt could be saved with none | **Project mandatory**; refused without one |
| Owner typed free-hand into Party | **Owner derived from `Project.client`**; typed value cannot override it |
| Counterparty account created as `type='person'` → filed under `credit_debit`, invisible to the Receivable KPI | Created as `type='client'` → filed under `project_in_flow`, counted |
| Nothing written to the project | `OwnerPayment` written **in the same transaction**, so `total_received` / `remaining_receivable` move immediately |
| Void left the project side untouched | Void/restore/amend keep the mirror in step |

This answers the original question — *why is a Party field needed for a project
payment?* — by removing the need. It is derived, not asked for: the field is
relabelled **"Owner / Client"**, filled from the project, and made read-only. It
remains a free field for every other category.

The mirror is idempotent by back-link (`OwnerPayment.source_entry_id` →
`hdc_cash_flow_entry.id`), the same way loan movements link back to their entry,
so re-saving or replaying an entry cannot double-count.

Defect 3.8 (completed projects offered) is fixed in passing: the picker now lists
open projects only.

### Historical entries

Receipts already posted the old way are **not** discarded — they are repaired:

```
python3 scripts/repair_project_receipts.py --dry-run   # lists what would change
python3 scripts/repair_project_receipts.py --apply     # writes the missing rows
```

It writes only the missing project-side row; cash and bank balances are
untouched because the ledger side was always correct. Payments entered on the
Projects page have no `source_entry_id` and are skipped, so nothing is counted
twice. Voided entries are mirrored as voided. Re-running is a no-op.

Verified on a reconstructed pre-fix database — two projects with receipts
recorded by project name only, plus one payment entered the old way on the
Projects page:

```
BEFORE  Hill View Villa        received=           0  remaining=   3,000,000
BEFORE  Mansehra Road Plaza    received=     300,000  remaining=   4,700,000
AFTER   Hill View Villa        received=   1,250,000  remaining=   1,750,000
AFTER   Mansehra Road Plaza    received=   1,500,000  remaining=   3,500,000
```

Mansehra = 300,000 (pre-existing row, untouched) + 1,200,000 (repaired). The
second `--apply` reports `none — every project receipt is already attached`.

### Upgrade safety

Two additive nullable columns (`hdc_cash_flow_category.project_effect`,
`hdc_owner_payment.source_entry_id`) are created by the existing startup schema
guard. Verified by stripping both columns from a database and booting: the app
added them and read the correct rules. `project_mode='required'` is forced at
read time for receipt categories, so databases whose stored rule already says
`optional` are corrected without a data migration.

### Tests

347 tests pass (`python -m unittest discover -s tests`), including 8 new ones in
`tests/test_cashflow_register.py::ProjectReceiptTestCase` covering: project
required, owner derived over a conflicting typed name, the `client` account type
and its KPI grouping, one-mirror-per-entry, void/restore round-trip, backfill
repair and its idempotence, and backfill leaving other surfaces alone.
`scripts/check_layers.py` → OK, and the JS harness → 11 checks passed.

Five pre-existing tests used `'Owner / Client Receipt'` as a generic stand-in for
"an income category" and were moved to `'Other Income'`; none of them tested
project behaviour.

### Still open

Section 5's remaining items are unchanged and **not** addressed here: no
over-receipt guard (3.1), no duplicate-payment warning (3.2), money-out still
does not reach project cost (3.9), and the three entry surfaces still exist
separately — this change makes the register surface correct rather than
consolidating them.
