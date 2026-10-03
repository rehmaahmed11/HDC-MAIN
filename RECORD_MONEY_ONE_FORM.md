# Record Money — one form for every transaction

Follow-up to PR #69 (*"Simplify Accounts and HDC Tools sections for everyday
users"*). That PR simplified the Accounts landing page but left the section with
**two parallel ways to record the same money**, and hid the better one.

## What was wrong

1. **The uniform form had disappeared from the UI.** The shared, direction-first
   New Transaction form (`templates/hdc/accounts/_new_transaction_form.html`,
   driven by `new_transaction.js`, posting to the cash-flow register engine) was
   only linked from the Cash Flow Register — which PR #69 had moved into the
   collapsed *Advanced (admin only)* block. The sidebar's *Record Money* and the
   hub's big green card both pointed at the Money Center instead.
2. **Record Money was a menu of little forms.** Its Step 2 was a grid with one
   tile per transaction type, each opening its own mini-form, and every tile
   printed its internals on screen (`project_income · project_receivable ·
   receive_from_project`). An admin-only *How It Works* tab rendered the posting
   paths, table names and reconciliation internals — developer documentation
   inside an operator screen.
3. **Two posting engines.** The Money Center posted through
   `_create_accounts_transaction_with_sync`; the New Transaction form posts
   through `save_manual_cash_flow_entry`. Same money, two code paths, two sets
   of rules.

## What changed

**`/hdc/accounts/money-center` (Record Money) now renders the shared form.**
The same partial, the same controller, the same POST as
`/hdc/accounts/new-transaction` and the CF register. The stepper, the type
tiles, the per-type mini-forms, `static/hdc/js/pages/money_center.js` and the
*How It Works* tab are gone. Around the form the page keeps what an operator
actually needs, in plain language:

* today's numbers (company total, money in today, money out today, net today);
* **Still to pay** — workers, suppliers, subcontractors and office staff, each
  row linking to the page that settles it (see below);
* **Still to receive** — projects (a "Receive" link opens *this* form
  pre-filled) and tool rentals;
* account balances and the last few entries.

**Posting goes through one engine.** The page accepts only
`action=create_entry` and calls `create_entry_from_form` →
`save_manual_cash_flow_entry`, with the same draft replay, idempotency key and
flash messages as the other two surfaces. The `quick-post` JSON endpoint stays
for API compatibility (and its guard test).

**Deep links pre-fill the form.** `?direction=…&category=…&project_id=…&amount=…
&party=…` resolves each id/name against the database first (a stale bookmark
opens a clean form), and a submitted-but-rejected draft still wins over the
query string.

**Pending payables link to the ledger that owns them.** A worker's payable is
settled on `/hdc/workers/<id>/payment`, a supplier's on the supplier page, a
subcontractor's on their payment page, office staff on theirs. This is
deliberate: a plain cash entry moves the money but leaves the payable standing,
so the same person could be paid twice — the exact failure mode the accounts
audit called out in decision 15.3. A **project** receipt is different: the
register engine mirrors it into the project's own receipts
(`project_effect='receipt'`), so those rows do come back to this form.

**Text cleanup across the section.** Operator-visible copy that talked about
ledgers, intent matrices, posting paths, "forensic" scans and double-entry was
rewritten in plain language on the hub, Record Money, Manage Accounts, All
Entries, Cash Flow, the register and the Books Check page (the last one keeps
the ids it needs to be useful, but its labels now say what they mean).

## Bug found and fixed on the way

`hdc/services/money_hub.py` filtered projects with `Project.is_void` — a column
that does not exist. The `AttributeError` was swallowed by the surrounding
`try/except`, so **the "Projects receivable" panel was always empty**, on both
the Money Center page and `/hdc/accounts/money-center/api/pending`. It now
filters on the project status the rest of the app uses, and a test pins it.

`pop_entry_form()` also returned a *blank-shaped* draft when nothing had been
stashed, so merging a pre-fill underneath it silently overwrote every field. It
now returns `{}` when there is no draft.

## Architecture follow-up

The uniform form is the one cash-movement pattern, not a replacement for every
module's source-ledger settlement form. The full post-PR #70 matrix is in
`TRANSACTION_ARCHITECTURE_AUDIT.md`. The shared engine now also carries the
operator reference into the linked Accounts row, blocks active exact-reference
duplicates while allowing amend/replace, and applies the Accounts-style cap for
a project receipt when a positive receivable is established. Generic
project-linked outflows remain cash tags only until an explicit project-cost
effect is designed.

## Tests

* `tests/test_money_center.py` — rewritten around the new contract: the page
  renders the shared form and no per-type picker; no developer text; the three
  entry surfaces render the same partial; deep links pre-fill; an unresolvable
  deep link opens a clean form; posting records exactly one entry (and a
  double-click cannot post twice); pending rows link to the pages that settle
  them.
* `tests/test_new_transaction_form.py` — the "same form partial" test now
  covers Record Money as the third surface.
* `tests/test_combo_widget.py`, `tests/test_frontend_config_contract.py`,
  `tests/test_accounts_section_smoke.py`, `scripts/audit_acceptance.py` —
  updated to the contract above (`money_center.js` no longer exists).
* Full suite: **667 tests, OK**; `compileall`, `check_layers.py`,
  `check_db_safety.py` and `reorganize_frontend.py --check` all pass.
