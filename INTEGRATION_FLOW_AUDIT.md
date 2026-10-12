# Integration & Flow Audit — Parties · HDC Tools · Projects · Accounts

**Date:** 2026-10-12
**Branch:** `arena/bfa47028-hdc-main` (base `8f7217d`)
**Scope:** the four sections named in the request — Parties, HDC Tools (tool rental), Projects, Accounts — and every money/party flow that connects them.
**Method:** static reading of routes/services/models, the repo's own gates (`check_layers`, `check_db_safety`), the full unit suite (`unittest discover`), and three reproduction probes run against a throwaway SQLite database (outside the repo, not committed).

No application code was changed in this pass. This document records findings and the recommended order of fixes.

---

## 1. Verdict

| Area | Health | Headline |
|---|---|---|
| Accounts (unified ledger) | **Sound** | The single ledger (`hdc_account_txn`) is the cash authority. Shared-entry posting and void/restore are consistent. |
| HDC Tools | **Sound, with silent-fallback risk** | Stock/serial/rental invariants hold. Bad date input silently becomes "today". Internal rentals post money under a different party name than the owner. |
| Projects | **Sound** | Owner receipts, receivable cap and void/restore work. Party sync failures are swallowed while the UI says "linked". |
| Parties | **Two real bugs** | (1) The owner statement omits the owner's own payments. (2) Deactivating a client/worker is undone on the next visit. |

**Integration verdict:** the shared ledger is correct, but the *party view* is only a partial projection of it. Money posted with `party_name = <project name>` is invisible on the party statement. The party directory also has write-on-read side effects that fight user actions.

---

## 2. Baseline checks (all run)

| Check | Result |
|---|---|
| `scripts/check_layers.py` | OK — 87 modules, acyclic, rules hold |
| `scripts/check_db_safety.py` | OK — no DB/backup/archive files tracked |
| `unittest discover -s tests` | **973 tests OK, 5 skipped**, 0 failures |
| `scripts/parity_check.py` | Not meaningful here — needs a baseline commit (`HDC_BASELINE_COMMIT`); not run |

Passing tests do not cover the two high-severity findings below. Neither bug has a test today.

---

## 3. Flow map (how the four sections connect)

```
                ┌──────────────────────── PARTIES (hdc_cash_flow_party) ───────────────────────┐
                │  client  ◄─ ensure_party ─ Projects (add/edit, sync on /hdc/parties GET)     │
                │  worker  ◄─ sync_workers_as_parties (Workers, on /hdc/parties GET)           │
                │  rental  ◄─ ensure_party ─ HDC Tools (external customer, payment, discount) │
                │  lender/borrower ◄─ Loans / CF Register                                       │
                └───────────────┬───────────────────────────────────────▲───────────────────────┘
                 party_ledger() │ filters AccountTransaction.party_name │
                                ▼                                       │
  PROJECTS ──owner receipt──► OwnerPayment ──mirror──► ACCOUNTS (hdc_account_txn, the cash ledger)
     │  (type project_income,     ▲                    ▲   ▲
     │   party_name = PROJECT     │ CF Register        │   └── HDC TOOLS payments / discounts
     │   NAME  ⚠ see F1)          │ (project_effect=     (party_name = customer  |  PROJECT NAME ⚠ F3)
     │                            │  receipt)           
     ├── Project.client ─────────────── name-key ─────────► Party (client)
     └── internal ToolRental.project_id ─────────────────► HDC TOOLS (own-site rentals)
```

Key property that holds: **money has one home** (`hdc_account_txn`). Every posting path writes exactly one ledger row, and void/restore is propagated via `_accounts_set_void_by_source` (owner payment, tool payment, tool discount).

Key property that does not hold: **the party statement is keyed on the ledger's `party_name` text, and that text is not always the party's name** (see F1, F3).

Integration keys are **name-based** (`lower(trim(name))`), not foreign keys. Renames never propagate; old party rows remain by design (immutable ledger).

---

## 4. Findings

Severity: **High** = wrong money/party state visible to users; **Medium** = silent wrong data or misleading UI; **Low** = hygiene/performance.

### F1 — HIGH — Owner statement omits the owner's own payments (reproduced)

- **Where:** `hdc/services/accounts.py:3481` posts owner receipts with `party_name = project.name`. `hdc/services/parties.py` `party_ledger()` selects rows where `party_name` equals the party name.
- **Repro:** project `Villa Site`, client `Owner Bee`; record a 25,000 owner payment via `POST /hdc/projects/<id>/owner_payment`.
  - Ledger row created: `project_income`, amount 25,000, `party_name='Villa Site'`, from account `Owner Bee`.
  - `party_ledger(Owner Bee)` → **0 financial rows, total_in 0**.
  - Project card on the same party page shows **received 25,000**.
- **Conflict with docs:** `PROJECTS_PARTIES_INTEGRATION.md` §5 says the owner ledger shows "owner receipts + rental receipts + loan movements". The `parties.py` docstring says the party page and Accounts → All Entries "cannot disagree". Both are false for owner receipts.
- **Not verified:** whether a receipt posted through the CF Register/New Transaction form (`project_effect='receipt'`) has the same `party_name`. It routes through the same shared engine, so likely yes. Needs a probe.
- **Fix direction:** make the owner statement match on the client identity. Either (a) post `party_name = client` for `project_income` and keep project in `project_id`, or (b) have `party_ledger` also include rows where `project_id` ∈ owned projects and `type = 'project_income'`. Option (a) is simpler and matches the other party-keyed rows. Add a regression test: owner payment → statement shows the row and money-in.

### F2 — HIGH — Deactivated client/worker is silently reactivated (reproduced)

- **Where:** `hdc/routes/parties.py:102-103` runs `sync_workers_as_parties()` and `sync_project_clients_as_parties()` on **every GET** of `/hdc/parties`. Both call `ensure_party()` (`hdc/services/cashflow_register.py:1455`), which sets `is_active = True` on any hidden row.
- **Repro:** create project for `Owner Bee` → `GET /hdc/parties` (party created) → `POST` action `toggle_party` (deactivate) → `GET /hdc/parties` again → **`is_active = True`**.
- **Impact:** the Deactivate control appears to work and then reverts. Users cannot hide a client or worker from the pickers while they still have a project or worker record. Same mechanism for workers (code-read; not separately probed).
- **Side effect:** the directory GET commits writes. A read-only role that opens the page triggers DB writes. `_money_write_required()` does not block GET.
- **Fix direction:** run the syncs only when a project/worker is created or edited (the creation paths already call `ensure_party`), or run them once per process/startup. Do not reactivate on read. Make `ensure_party` accept `reactivate=False` for sync callers. Add a regression test: deactivate → reload → still inactive.

### F3 — MEDIUM — Own-site tool rental money is filed under the project name (code-read)

- **Where:** `hdc/services/tool_rental.py:1034` (receipt) and `:1221` (discount): for internal rentals, `party_name = rental.project.name`. The customer account is keyed on `project.client` (`:982`).
- **Effect:** the owner's *account balance* includes own-site rental receipts, but the owner's *party statement* does not. Those rentals do appear in the statement's `rentals_internal` list, so the page shows a rental with money the money rows do not reflect.
- **Fix direction:** use the project's client name as `party_name` for internal rentals, consistent with `_get_or_create_customer_account`. Keep the project name in `project_id`.

### F4 — MEDIUM — Tool dates silently fall back to today (code-read)

- **Where:** `hdc/routes/tool_rental.py` lines 2296-2298 (rental date), 2846-2848 (return date), 3114-3116 (payment date), 3264-3266 (discount date). Each has a bare `except:` that replaces an unparseable date with `_pkt_today()` and gives no warning.
- **Effect:** a mistyped or malformed date posts a payment, return or discount on today's date. For cash and receivable records that is silent back-dating/forward-dating.
- **Fix direction:** on parse failure, flash an error and re-render. Do not default. Replace the bare `except:` with `except ValueError:`.

### F5 — MEDIUM — Party-sync failure is hidden but the UI claims success (code-read)

- **Where:** `hdc/routes/projects.py:137-145` (add) and `:302-310` (edit): `ensure_party(...)` is wrapped in `try: ... except Exception: pass`. The add flash still says `Client "…" linked to Parties.`
- **Effect:** a failed party link is invisible. The project then does not appear in the owner's statement (F1 compounds this).
- **Fix direction:** log the exception and flash a warning on failure, or let it fail the request. Do not show "linked" unless it is.

### F6 — LOW — Edit-project permission check is applied only to financial fields

- **Where:** `hdc/routes/projects.py:277-300`. A user without `project_financials` write can still change `client`, `client_phone` and `name`. Those fields drive party links and owner statements (F1, F5).
- **Fix direction:** decide whether client identity is a financial field. If yes, add it to `financial_fields`.

### F7 — LOW — Sync work on every directory load

- **Where:** `sync_project_clients_as_parties()` loads all projects (`hdc/services/parties.py:249`), walks the list in two separate loops (the first only builds the distinct-client map), and calls `ensure_party` per distinct client. `known_client_party_names()` (`:355`) loads all projects on each project form render. `sync_workers_as_parties()` scans all workers on every directory load.
- **Effect:** cost grows with data size on a read path. The double query and the duplicated loop are also redundant.
- **Fix direction:** move the sync out of GET (see F2). Collapse the two loops into one.

### F8 — LOW — Name-keyed integration: renames do not follow

- **Where:** by design (`PROJECTS_PARTIES_INTEGRATION.md`, "Why name-keyed"). A renamed client produces a new party; the old party keeps history.
- **Effect:** the same person can appear under two names; the owner statement for the new name starts empty (F1 aside).
- **Fix direction (already listed as future work):** add `Project.client_party_id` and link tool/loan/owner rows by id.

### F9 — INFO — Access control is layered, not per-route

- Across the seven route modules scanned (Parties, Projects, Accounts, Accounts manage, Money Center, New Transaction, HDC Tools), 44 of 66 POST routes carry `@_money_write_required()`. The other 22 do not, such as `hdc_new_transaction`, `hdc_account_transaction_edit`, `hdc_money_center_api_quick_post` and `hdc_add_project`.
- These are covered by `app.before_request` `_enforce_user_permissions` (path matrix in `hdc/services/permissions.py`) and inline `_admin_only()` checks. They are **not** a gap as far as reading goes. They are fragile: a new money route copied from the wrong template would skip the decorator.
- **Fix direction:** add a test that enumerates money POST routes and asserts a non-finance role gets 403.

### F10 — INFO — Bare exception use in the Tools module

- 7 bare `except:` in `hdc/routes/tool_rental.py` and 2 in `hdc/services/tool_rental.py`. Four are the date fallbacks in F4. The rest are the `Tool.query.get` / parse paths.
- `hdc/services/tool_rental.py:702` and `:708` are filter date parsers that silently ignore bad input (`except: pass`). A bad filter date returns unfiltered results with no message.

---

## 5. Recommended order

1. **F2** (deactivate reverts) — small, clear, high user impact. Remove reactivation from the GET sync path and add a test.
2. **F1** (owner statement misses payments) — decide the canonical `party_name` for project income, then add a regression test. This also fixes the doc/code conflict.
3. **F3** — same change pattern as F1 for internal rentals (`party_name = client`).
4. **F4 / F5** — remove silent fallbacks; surface failures.
5. **F9** — add a permission-matrix test for money POST routes.
6. **F6 / F7 / F8** — hygiene and performance; schedule with the next refactor.

---

## 6. What this audit did not do

- No code or data was changed. The probes lived in `/tmp` and are not committed.
- F3, F4, F5 and the worker half of F2 are based on code reading and were not executed.
- CF Register receipts (F1 "not verified") were not probed.
- No browser/UI pass was done; the UI claims above come from the rendered-statement probe and template reading.

---

## 7. Fix status (follow-up pass, same branch)

| # | Finding | Status | What changed |
|---|---|---|---|
| F1 | Owner receipts filed under project name | **Fixed (new postings) + backfill script** | `accounts.py` posts `party_name` = owner name. `scripts/backfill_owner_party_names.py` re-files existing rows; dry run by default, `--apply` to write. **Run the backfill once per database.** |
| F2 | Deactivated client/worker revived on directory load | **Fixed** | `ensure_party(..., reactivate=...)`; directory syncs never reactivate. Project and tool activity still reactivate, as before. |
| F3 | Own-site tool money under project name | **Fixed (new postings) + backfill** | `rental_party_name()` in `services/tool_rental.py`, used for payments and discounts. Covered by the same backfill script. |
| F4 | Bad tool dates silently become today | **Fixed** | `_strict_form_date()` refuses bad input with a message: rental, expected return, return, payment, discount and transfer dates. Blank still means today. |
| F5 | Failed party link swallowed; success still shown | **Fixed** | `_link_client_party()` uses a savepoint, logs the error, and shows a warning instead of "linked". |
| F6 | Client change skips permission check | **Fixed** | Changing `client` now requires `project_financials` write. |
| F7 | Sync cost on every load | **Partly fixed** | Duplicate loop removed; project-form client list is a `DISTINCT` query. The directory still *creates* missing parties on GET (idempotent); it no longer writes reactivations. |
| F8 | Renames don't propagate | **Not fixed (decision needed)** | Needs a `Project.client_party_id` foreign key and a migration. Ledger rows keep the old name as history, so the fix must choose how old statements are re-linked. |
| F9 | Money POST routes rely on layered guards | **Partly fixed** | `test_non_finance_role_cannot_post_money` checks a staff user cannot post an owner receipt or a new transaction. Other money routes (tool payments, account edits, Money Center quick-post) are not yet in the matrix. |
| F10 | Bare `except:` in tools; silent filter dates | **Fixed** | Bare excepts narrowed to `(TypeError, ValueError)`. Bad filter dates on the Reports and register pages now show a warning instead of being dropped. |

**Behaviour change to note:** owner receipts and own-site tool money now show the owner's name in the Accounts "party" column instead of the project name. Project and rental identity still live in `project_id` and `related_entity_id`.

**Tests added:** `tests/test_integration_flow_fixes.py` (6 cases covering F1–F5). Tests in `test_parties_module` still pass.

**Existing test changed:** `tests/test_workers_as_parties.py::test_sync_revives_a_hidden_party` asserted the F2 bug (a sync revives a hidden worker). It is now `test_sync_does_not_revive_a_hidden_party`, with a companion test that real worker activity still revives.

**Full suite after the fixes:** 978 tests run, 5 skipped, the one failure above (now fixed). Re-run the full suite before merging.

**Follow-up (second pass):** `tests/test_integration_flow_fixes.py` now has 8 cases, including the backfill re-filing a legacy row only, and the staff-role permission check. The earlier permission test was run on the admin session by mistake; it now uses a fresh client.
