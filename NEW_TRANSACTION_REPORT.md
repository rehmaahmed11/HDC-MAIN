# New Transaction Form — Redesign Report

Scope: the **AccountsHub "New Transaction" form** and its form-filling behaviour only.
Everything else in the application (dashboard, reports, transaction history, navigation,
account/project pages, authentication, unrelated styling) is untouched.

The form is a front end to the Cash Flow register engine that already owns money posting:
one shared partial (`templates/hdc/accounts/_new_transaction_form.html`) rendered by two
surfaces — the focused page `/hdc/accounts/new-transaction` and the entry card on the
existing register `/hdc/accounts/cashflow/register` — driven by one controller
(`static/hdc/js/pages/new_transaction.js`) posting to
`hdc.services.cashflow_register.save_manual_cash_flow_entry`.

---

## 1. Files changed

### Added

| File | Lines | What it is |
|---|---|---|
| `hdc/services/transaction_entry.py` | 398 | Form context + the one mapping from form fields to the register engine: `entry_form_context`, `create_entry_from_form`, draft `stash_entry_form`/`pop_entry_form`/`clear_entry_form`, `create_money_account`, `create_cashflow_party`, `create_project_quick` |
| `hdc/routes/new_transaction.py` | 211 | The page (GET/POST) plus the Account / Party / Project create endpoints and the subcategory lookup |
| `templates/hdc/accounts/new_transaction.html` | 44 | Focused single-transaction page (extends `shared/base.html`) |
| `templates/hdc/accounts/_new_transaction_form.html` | 311 | The shared, direction-first form partial (the redesign itself) |
| `templates/hdc/accounts/_new_transaction_modals.html` | 157 | The three compact "+ Add New …" modals |
| `static/hdc/js/pages/new_transaction.js` | 739 | The shared controller: direction gating, category → subcategory, pickers, add-new modals, validation, submit guards |
| `static/hdc/css/new_transaction.css` | 364 | Token-based styling (1/2/3 columns at <576 / ≥576 / ≥992 px), 16 px / 44 px touch-sized controls |
| `tests/test_new_transaction_form.py` | 646 | 29 tests for the form, its endpoints and the engine's validation |
| `tests/new_transaction_harness.js` | 802 | Node harness that runs the real controller against a DOM stub (11 checks) |

### Modified

| File | Δ | Why |
|---|---|---|
| `hdc/routes/__init__.py` | +2 | Register the new route module |
| `hdc/routes/cashflow_register.py` | +23 / −46 | The register's entry card now renders the shared partial; the old inline direction/category wiring and duplicate context keys were removed |
| `hdc/services/cashflow_register.py` | +60 / −5 | Scope validation for project/stage, hard error on a stale category+subcategory pair, party type no longer downgraded (details in §4) |
| `static/hdc/js/core/combo.js` | +61 / −7 | Opt-in no-result and "+ Add New …" rows, escaped item text, `role=listbox`, stable menu ids — existing callers behave exactly as before |
| `templates/hdc/accounts/cashflow_register.html` | +24 / −150 | The flat 12-column entry form was replaced by the shared partial; the register table, amend/void modals and pagination are untouched |
| `scripts/reorganize_frontend.py` | +5 | The three new templates mapped to the accounts domain (frontend-organisation check) |
| `tests/test_combo_widget.py` | +19 | The new page and the register added to the combo-contract table so their picker wiring is pinned too |

## 2. What changed

**Direction is the first decision.** *Money In / Money Out / Internal Transfer* is the only
thing on screen until it is chosen (segmented buttons driving the native `<select>`, so
keyboard and no-JS both still work). It then decides what else exists:

* Money Out — Date, Amount (PKR), Account, Category, Subcategory, Party/Person, Project,
  Reference, Description, Note.
* Money In — same shape, but only income categories are selectable.
* Internal Transfer — Date, Amount, From Account, To Account, Reference, Description, Note.
  Category, Subcategory, Party and Project are hidden **and disabled**, so a transfer cannot
  post them; the same account can never be both From and To (blocked on the control, before
  submit, and again on the server).

**Category → Subcategory is a real dependency**, read from the database: each subcategory
carries its parent id, switching category disables every subcategory that does not belong to
it and clears the selection instead of leaving a stale pair behind.

**Account, Party/Person and Project are searchable comboboxes** built on the existing
`HDCComboList` widget: substring, case-insensitive matching (`mcb` → `MCB 1109 (Bank)`,
`cash` → the cash accounts), arrow keys to move, Enter to select, Escape to close, blur
reverting to the current selection. They are the only three searchable fields — Category and
Subcategory are ordinary selects, Date is a native date picker, Amount is a text field with a
decimal keypad hint that accepts `1,25,000.50`.

**"+ Add New …" stays inside the transaction.** Typing a name that does not exist shows
`No accounts found.` / `No parties found.` / `No projects found.` plus the matching
`+ Add New Account` / `+ Add New Party` / `+ Add New Project` row. That opens a compact modal
(prefilled with what was typed), saves over JSON, appends and selects the new row in the
picker and returns focus to the form — **no page load and no field is touched**. The rest of
the transaction (date, amount, category, subcategory, project, reference, description, note,
and an already-chosen account) is still there afterwards; a refused create (for example a
bank account with no bank name) keeps the modal open with the reason and leaves the
transaction alone.

**Validation sits next to the field**, before submit and again on the server: the message is
written into the field's own error line, the control is marked `aria-invalid`, and the first
offending control is focused and scrolled into view. A rejected POST re-renders the same
page with everything the user typed (including a refused raw amount such as `not-money`),
so nothing is ever silently erased.

**Save behaviour**: the button is locked and shows a spinner while the (single) submit is in
flight, the success flash names the direction, amount and entry number, and the hidden
idempotency key means a retried or double-clicked post can only ever create one entry.

**Reuse, not a second system**: the form posts through the existing register engine (which
already does posting, the 1:1 ledger transaction, balance/overdraft checks, day locks,
immutability and the audit trail). No new model, no second category system, no new table.

## 3. Database / API changes

**Database: none.** No schema change, no migration, no new table or column. The form writes
the same `hdc_cash_flow_entry` / ledger rows the register has always written. Existing rows
and reports are unaffected.

**New HTTP endpoints** (all `login_required`, all admin/accountant-only; non-money roles get a
JSON `403 {ok: false, message: "Admin/Accountant access required."}`):

| Method | Path | Purpose |
|---|---|---|
| GET/POST | `/hdc/accounts/new-transaction` | The focused page; POST posts one entry |
| POST | `/hdc/accounts/new-transaction/account` | Create/reuse a cash or bank account |
| POST | `/hdc/accounts/new-transaction/party` | Create/reuse a register party |
| POST | `/hdc/accounts/new-transaction/project` | Create/reuse a project (code auto-generated) |
| GET | `/hdc/accounts/new-transaction/subcategories?category_id=` | Authoritative subcategory lookup |

The create endpoints are get-or-create: an existing row (case-insensitive name) is reused and
returned with `created: false` — "That account already existed — selected it." — so the add
flow cannot create accidental duplicates.

## 4. Validation added

Client-side (immediate feedback) and server-side (authoritative) apply the same rules:

* direction required; date required; amount required, numeric and **> 0**;
* account required, and it must be an active cash/bank account; transfers need a distinct,
  valid destination account;
* category required for money in/out, and the category's direction must match the entry;
* a subcategory must belong to the chosen category — a stale pair is now **rejected**
  (`"Mason" is not a subcategory of "Material & Purchase". …`) instead of being silently
  dropped, which is the one behaviour change to the existing engine;
* project and stage are validated when submitted (`That project no longer exists. Pick
  another project.`) instead of being ignored — new `validate_scope` argument on
  `save_manual_cash_flow_entry`, default on; an amend still carries its original project
  forward (turning it off only for that leg, so an entry whose project was later removed can
  still be corrected);
* everything the register already enforced stays enforced: balance/overdraft, day locks,
  void/immutability, and the reuse of existing parties/projects;
* `save_cf_party` no longer overwrites a specific party type with the generic `other`, so
  posting a payment to a known supplier cannot quietly re-file them as "other".

## 5. Tests performed (and how)

Automated, all green:

| Command | Result |
|---|---|
| `python -m unittest tests.test_new_transaction_form` | **29 tests OK** (page + partial rendering, pickers against the real vocabulary, all three directions, stale pair, invalid ids, rejected-submission replay, add-new endpoints incl. duplicates and 400s, double submission, non-admin denial) |
| `python -m unittest tests.test_cashflow_register` | 46 OK (engine unchanged in behaviour for existing callers) |
| `python -m unittest tests.test_money_permissions` | 12 OK (every non-GET route still denies non-money roles, incl. the new ones) |
| `python -m unittest tests.test_frontend_config_contract` | 6 OK |
| `python -m unittest tests.test_combo_widget` | 3 OK (now also pins the new page's and the register's combo pairs) |
| `python -m unittest discover -s tests -p 'test_*.py'` (CI parity) | **321 tests OK** (~143 s) |
| `scripts/check_layers.py`, `scripts/check_db_safety.py`, `scripts/reorganize_frontend.py --check`, `compileall` | all OK |
| `node --check` on every `static/**/*.js` | OK |
| `node tests/combo_widget_harness.js .` | all assertions passed |
| `node tests/new_transaction_harness.js .` | **11 checks passed** — runs the real controller: direction gating, transfer hiding/To-account, category → subcategory clearing, wrong-direction category lock, inline validation, single submit + locked button, same-account transfer refusal, substring search and the no-result copy, successful add-new preserving every field, failed add-new keeping the modal |
| `tests/smoke_worker.py` (fresh DB) | 87 reads, 16 writes, 17 counts, **0 errors** |

Manual end-to-end run on a scratch SQLite database through the real app (admin session):

* Money Out `1,25,000.50` → stored as `12500050` paisa with its category/subcategory/party/
  project and a linked ledger transaction;
* Money In 50,000 booked to *Owner / Client Receipt*; Internal Transfer 50,000 with no
  category and no party; a same-account transfer and a transfer without a destination were
  refused;
* `+ Add New` account/party/project: created, then repeated case-insensitively and reused
  (no duplicate rows); invalid payloads returned 400 with the exact reason;
* 13 malformed submissions (empty/zero/negative/garbage amount, missing or bogus account,
  missing or bogus category, stale subcategory, unknown project, wrong-direction category,
  empty direction, transfer without destination) each showed their field message and wrote
  **no** row;
* the same payload posted twice with one idempotency key posted **once**;
* a rejected register submission re-rendered with every value kept (`value="not-a-number"`
  and the flash message).

Not claimed: no browser-based UI test. Behaviour is verified through the Node DOM harness and
the rendered HTML/CSS, not through a real browser or a phone.

## 6. Remaining limitations

1. **No real-device browser testing.** Mobile/desktop quality rests on the CSS (single column
   under 576 px, 16 px inputs, 44 px+ targets, viewport-safe menus/modals, reduced-motion
   support) and the DOM harness — not on a browser or a phone. Layout details such as an
   on-screen keyboard covering the Save button could not be verified here.
2. **The DOM harness is a hand-rolled stub.** It exercises logic, keyboard handling and the
   add-new flow, but it cannot catch CSS painting issues or real browser quirks.
3. **Keyboard order** matches the required sequence structurally (template order) and arrows /
   Enter / Escape are harness-tested; full Tab traversal in a browser was not re-measured.
4. **Amount parsing mirrors `to_minor` but is a separate implementation.** It accepts
   `1,25,000.50`, `Rs 250` and `(250)`, but not every locale format (e.g. `1.234,56`).
5. **Category vocabulary is read-only on this page.** Categories and subcategories come from
   the existing seeded register vocabulary; adding one is still done on the register, which
   avoids a second taxonomy. Money In/Out categories therefore show whatever the register has.
6. **Two entry surfaces exist in parallel** (the focused page and the register's entry card)
   by design; both use the same partial, controller and engine, so they cannot drift, but
   there are two URLs to know about.
7. **Search is substring-based** (fast and predictable), not fuzzy or typo-tolerant.
8. **No browser-automation suite in CI** — the repo has no jsdom/Playwright dependency, so the
   JS is covered by the `node --check` pass plus the two hand-rolled harnesses.

---

## Appendix — the numbered requirements, and where each one lives
| # | Requirement | Where it is satisfied / how it was checked |
|---|---|---|
| 1–4 | Direction first and it drives everything; the three field sets; transfer has no category/party and From ≠ To | partial `_new_transaction_form.html` + `new_transaction.js` (`applyDirection`); harness checks 1–3 (gating) and 8 (From ≠ To); server rules in `validate_manual_cash_flow_entry` |
| 5, 24 | Cascading, data-driven category → subcategory; stale pair cleared and rejected | subcategory options carry `data-category`; `filterSubcategories`; `_cf_resolve_subcategory`; tests + harness checks 4–5 |
| 6–9, 23 | Account / Party / Project searchable with `+ Add New …`, `No … found.` copy, duplicates reused | `combo.js` (opt-in rows) + `new_transaction.js` (`ensureCombos`, `wireModal`, `selectCreated`); `test_new_transaction_form.py` + harness checks 9–11 |
| 10 | Right control per field (segmented direction, native date, decimal amount, plain selects, only three searchable) | partial markup; `test_page_script_is_wired_without_inline_logic` + `test_templates_carry_no_hard_coded_category_lists` |
| 11, 22 | Progressive reveal and the requested Tab order | template control order (direction→date→amount→account→category→subcategory→party→project→reference→description→note→save); harness checks 1–3 (progressive reveal) |
| 12 | Filter / arrows / Enter / Escape / clear / no-result / add action | `combo.js` (shared, unchanged contract) + harness check 9 |
| 13, 14 | Compact modal, no page leave, nothing else in the transaction is lost | `_new_transaction_modals.html`, `payloadFor`/`selectCreated`; harness checks 10–11 assert every other field survives |
| 15 | Required fields, amount > 0, account rules, From ≠ To, category rules, subcategory belongs to category, invalid ids rejected server-side | client `validate()` + authoritative engine (`save_manual_cash_flow_entry`, `_cf_resolve_*`); tests + harness checks 6–8 |
| 16, 17 | Mobile and desktop comfort | `new_transaction.css` (single column < 576 px, 16 px inputs, 44 px+ targets, viewport-safe menus/modals, reduced motion); **layout itself not browser-tested** — see limitation 1 |
| 18, 19 | Errors next to the field, data kept on failure, blocked double submit, loading + success feedback | `setFieldError`, draft stash/replay, disabled save button + idempotency key; tests + harness checks 6–7 and 10–11 |
| 20, 21 | One source of truth, smallest clean addition, no schema change | the form posts through the existing register engine; no model/schema/migration change; endpoints listed in §3 |
| 25 | Inspect before coding | recon pass over routes/services/models/templates/tests/scripts before any edit |
| 26 | Journey test | live run on a scratch DB (three directions, add-new, validation matrix, duplicate submit) + the 321-test suite |
| 27 | Nothing unrelated redesigned | see the file tables in §1; the only shared component touched is `combo.js`, and its new behaviour is opt-in |
