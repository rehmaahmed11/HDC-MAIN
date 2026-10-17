# Projects ↔ Parties Integration — Unified Owner Ledger

## Problem
Previously:
- **Projects** stored `client` as free-text string, isolated from rest of ERP.
- **Parties** (`hdc_cash_flow_party`) was a separate directory for counterparties (lenders, suppliers, workers, clients) used by Cash Flow Register, New Transaction, Loans, HDC Tools, etc.
- No link: creating a project didn't create a party, and party ledger didn't show projects. Same person could exist as "Ahmed Khan" in projects and "ahmed khan" in parties with different balances.
- Owner's other activity (tool rentals as external customer, loans) was invisible from project view.

## Solution — Name-keyed integration with unified owner ledger

Integration is done **by case-insensitive name matching**, same pattern used for Workers → Parties sync. This keeps it idempotent and backward compatible, and now gives each owner a single ledger that answers "does this owner have any rent payment, loan, etc.?" — plus a nullable `Project.client_party_id` direct link for projects created from a party (see *1b* below).

### 1. Auto-sync on Project create / edit
`hdc/routes/projects.py`:
- After `Project` flush, `ensure_party(client_name, party_type='client', phone=...)` is called.
- Creates a `CashFlowParty` of type `client` if missing, reactivates if hidden, never downgrades a specific type to `other`.
- Edit: if client name changes, new party is ensured; old party keeps history (immutable ledger rule).

### 1b. Reverse flow — create a Project **from** a Party

The integration now runs in both directions, using the same Add Project form:

* **Parties directory → row action.** Every active row has a green
  `fa-folder-plus` button — *New Project* for that party. It opens
  `hdc_add_project?party_id=<id>`.
* **Party ledger → New Project.** The ledger header (and the Projects section)
  opens the same URL, so a client with no project shows a *Create the first
  project* card instead of an empty page.
* **Prefilled, not retyped.** GET resolves the party and prefills the client
  name, phone and a hidden `party_id`; a banner confirms which party ledger the
  project will land in.
* **Exact link on save.** `_link_client_party()` honours the posted
  `party_id` while the typed name still matches that party, then writes
  `Project.client_party_id`. A changed name re-resolves (and creates the new
  party), so a project can never point at the wrong owner. The party's own
  spelling becomes `Project.client`.
* **Same automatic filing.** A typed new client still goes through
  `ensure_party(..., party_type='client')`, so it appears under
  *Clients / Owners* exactly like the project-first flow.
* **Links back.** The Projects list renders the client as a link to the party
  ledger (direct link first, name fallback); project detail already did.
* **Legacy backfill.** `sync_project_clients_as_parties()` now also fills
  `client_party_id` for every project whose client matches an existing party,
  so old rows join the exact-link model the next time the directory syncs or a
  project is edited.

| Situation | Resolution |
|---|---|
| Project created from a party | `client_party_id` → that row; `client` = party name |
| Project created the old way | no id; matches by lower-cased trimmed `client` |
| Party renamed, project not edited | FK still resolves the ledger (name-keyed receipts unchanged) |
| Project edit with a new client | FK re-pointed to the new/ensured party |

The FK is nullable, so an existing database is widened in place by
`_ensure_project_party_schema()` at bootstrap; nothing is rewritten and the
name fallback keeps old projects working.

### 2. Searchable combo in Project forms
- `templates/hdc/projects/add_project.html` and `edit_project.html` now use `combo_field` macro with `client_options = known_client_party_names()`.
- `known_client_party_names()` merges active client parties + distinct `Project.client` values not yet synced.
- User can pick existing client or type new one; new name auto-creates party on POST.
- Add route accepts `?client=Name` for prefill from Party ledger → "New Project for this Client".

### 3. Sync helpers on Parties page
`hdc/services/parties.py`:
- `sync_project_clients_as_parties()` groups projects by lower(client) and ensures each distinct client exists as party.
- Called automatically on `/hdc/parties` GET plus manual button "Sync Project Clients".

`hdc/routes/parties.py`:
- POST action `sync_clients` added.
- Directory counts include `client_total`, `client_auto_added`.

### 4. Directory shows linked projects
- `_project_stats_by_name()` aggregates projects per name key: count, contract value, received, receivable.
- `build_party_directory()` rows include `projects_count`, `projects_contract`, `projects_receivable`, etc.
- `parties_directory.html`: new Projects column, badge `X project(s) · Y contract`.

### 5. Party ledger — UNIFIED OWNER LEDGER (answers rent/loans question)
`party_ledger(party)` now returns:

**Financial core:**
- All `AccountTransaction` where `party_name` == party name (case-insensitive) — this already includes owner receipts, external rental receipts, loan movements, etc., with running balance.

**Projects owned:**
- `projects = Project.query.filter(lower(client) == key)` — all projects where client matches.
- Totals: contract, received, receivable.
- Shown in KPI cards "Projects Owned" + "Receivable" and table `#linked-projects`.

**Tool Rentals — does this owner also rent tools?**
- `rentals_external`: `ToolRental` where `customer_name` == party name (external rentals). These are customers renting HDC tools.
- `rentals_internal`: `ToolRental` where `project_id` in owned projects (company using its own tools on their sites).
- `rentals_all`: unique combined list, sorted.
- Existing `_rental_stats()` gives due amount.
- Template section `#linked-rentals` table shows rental_code, type (external/internal), project/customer, dates, total/paid/due, status, link to rental detail.
- So if "Ahmed Khan" owns a villa project AND also rents a mixer as external customer "Ahmed Khan", both appear: project in Projects section, rental in Rentals section, and rental receipt in Financials ledger (because `post_tool_rental_payment_to_accounts` creates AccountTransaction with `party_name = customer_name`).

**Loans — does this owner have loans?**
- `loans_all`, `loans_open`, `loans_closed` where `Loan.party_name` == key.
- `loan_outstanding` sum.
- Template section `#linked-loans` table.

**Result:** For a project owner, Parties → Ledger is now the single place to see:
- Project contract / received / receivable
- All money movements (owner payments + rental payments + loan takes/repays)
- Detailed rentals (external + internal via owned projects) with due
- Detailed loans with outstanding
- Worker/supplier info if same name exists there

`party_ledger.html`:
- Top badges: "Project Owner — unified ledger" when category=client
- KPI row: Money In/Out/Balance/Entries + Projects Owned + Rentals count
- Info alert explains unified ledger concept
- Sections: Projects, Rentals, Loans, then chronological Financials (unified ledger)
- Quick anchor buttons to each section
- "New Project for this Client" button

### 6. Project detail links to unified ledger
- `hdc_project_detail` resolves `client_party` + `client_statement` (full party_ledger) via case-insensitive lookup.
- `project_detail.html` header:
  - Client name link to Party ledger
  - Badges: linked status, rentals count, loans count, projects count (>1)
  - Info alert: "unified ledger: rents tools (...) · has X loans — open full ledger to see all rent payments..."
  - Buttons to ledger, directory, and anchors to rentals/loans

### 7. Projects list shows client
- `projects.html` now has Client/Owner column with phone + integration info alert.

## Data flow — unified owner
```
Project.client (free text)
   --ensure_party()--> CashFlowParty(name=client, type=client)
   --auto account--> Account(name=client, type=client)
   --owner receipt (Project page or CF Register with project_effect='receipt')
        --> OwnerPayment + AccountTransaction(from client account -> company cash, party_name=project.name or client)
   --external rental as same name
        --> ToolRental(customer_name=client, renter_type=external)
        --> ToolRentalPayment + AccountTransaction(party_name=customer_name)
        --> appears in same party_ledger financial rows + rentals_all
   --loan for same name
        --> Loan(party_name=client) + AccountTransaction
        --> appears in loans_all + financial rows
   --party_ledger() aggregates all above by lower(name) key
```

## Why name-keyed plus a nullable FK?
- Existing data had only a string. The FK is **additive**: nullable, added in
  place at bootstrap, with no rewrite of existing rows.
- Name-key matching stays as the fallback, so a database whose migration has
  not run (or a project created before this change) behaves exactly as before.
- Workers sync already uses the name-keyed approach; the party → project flow
  adds the exact pointer only where the operator actually chose a party, which
  is stronger than guessing from a string without changing legacy behaviour.
- The FK enables the party-first workflow, precise project counts on the party
  ledger, and a safe place for future rename propagation.

## How to use — check if owner has rent etc.
1. Create Project with client "Mr. Ahmed Khan" → Party auto-created as Client/Owner.
2. If Ahmed Khan also rents tools externally, create HDC Tools Rental with Customer Name "Mr. Ahmed Khan" (or same spelling) → rental payment posts AccountTransaction with party_name = "Mr. Ahmed Khan".
3. Go to Parties → filter Clients/Owners → open Ahmed Khan ledger.
4. You will see:
   - Projects section: villa project(s)
   - Rentals section: mixer rental, with due
   - Loans section: if any
   - Financials table: chronological list of owner receipts + rental receipts + any other money tagged with Ahmed Khan, with running balance.
5. Same from Project detail: top card shows "1 rental(s) · 5,000 due" badge and link to "View rentals" which jumps to party ledger rentals section.
6. Old projects: Parties page → "Sync Project Clients" backfills all distinct clients.

## Files changed
- `hdc/services/parties.py` — sync, project stats, rentals/loans detailed lists, unified ledger, party/project link helpers, `client_party_id` backfill
- `hdc/routes/parties.py` — sync_clients action, auto-sync, client-added hint
- `hdc/routes/projects.py` — ensure/link the exact party on create/edit, `party_id` prefill, resolve party + statement on detail, client options, client → party links on the list
- `hdc/models/projects.py` — `Project.client_party_id` + `client_party` relationship
- `hdc/core/schema.py` / `hdc/core/bootstrap.py` — `_ensure_project_party_schema()` migration
- `templates/hdc/projects/add_project.html` — party prefill banner, hidden `party_id`, phone prefill, combo + hint
- `templates/hdc/projects/edit_project.html` — linked-party badge + shared hint
- `templates/hdc/projects/project_detail.html` — linked party, rentals/loans badges, unified ledger alert, “another project for this client”
- `templates/hdc/projects/projects.html` — client column links to the party ledger
- `templates/hdc/parties/parties_directory.html` — projects column, sync UI, per-row New Project action
- `templates/hdc/parties/party_ledger.html` — unified owner ledger: projects, rentals (external+internal), loans, financials, New Project + first-project card
- `tests/test_project_from_party.py` — reverse-flow coverage (prefill, exact link, renames, backfill, page actions)

## Future enhancements
- ~~Add `Project.client_party_id` FK for direct join~~ — done (nullable, migration + backfill)
- Filter Projects list by party_id
- On Party rename, offer to rename linked Project.client values
- Show supplier purchases for supplier parties in same unified ledger
