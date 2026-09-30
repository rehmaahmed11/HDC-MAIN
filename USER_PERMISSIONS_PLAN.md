# User Role Permissions: Full Checkup & ERP-Level Redesign Plan

> Status: **checkup + plan only. No feature code changed yet.**
> Date: 2026-09-30 · Base: `main` @ `edba01e` (after PR #62, #63, #64)
> Companion: **[`APP_TREE.md`](APP_TREE.md)**, the full app tree from Dashboard to the last layer
> (module → permission page → screen → card/tab/popup → fields, buttons, table columns).

## 0. Goal (what you asked for)

- **Full control:** a user sees only what the administrator ticks, with nothing extra.
- **Every level:** Module → Page → Section (tab / card / panel) → Action (button) → sensitive Field.
- **The "2 things on 1 page" rule:** if a page has A and B and you allow only B, then **A disappears and B keeps working fully**. Today that often fails: hiding A also breaks B, because B secretly depends on A's permission.
- Professional ERP style: named roles, per-user overrides, a preview of what the user will see, and an audit trail.

---

## 1. How the checkup was done

| Check | Method | Size |
|---|---|---|
| Route inventory | Loaded the real Flask app; for every URL: methods, permission bucket, template, `action=` branches | **273 routes** |
| Template inventory | Every template/partial: tabs, cards, POST forms, hidden `action` values, links, background calls (`fetch`), included JS | **117 templates, 222 cards, 14 JS files** |
| Cross-permission dependency scan | Page owned by permission X that **submits to / loads data from** permission Y | **41 dependencies on 15 pages** |
| Live single-permission test | 112 users: each of the 56 non-admin page permissions given alone, once as read and once as read+write; every page they own was opened and scanned | **240 page loads** |
| Live dependency test | Users with one permission called the helper URLs their own page uses | **7 real breakages confirmed** |
| Existing tests | `test_user_permissions`, `test_exact_data_permissions`, `test_project_access_cascade`, `test_money_permissions`, `test_sidebar_navigation` | 108 tests, all pass |

---

## 2. Findings

### F1. "Hide one, lose the other": confirmed breakages (highest priority)

A page works only if the user **also** has a different, unrelated permission. The admin can't see this in the UI.
Confirmed live (HTTP 403 returned to the page's own form or dropdown):

| User has only… | Page part that breaks | Hidden dependency (permission it actually needs) |
|---|---|---|
| Accounts | Project → **Stage dropdown** in transaction form | `/hdc/api/project_stages` → *Stages* |
| Accounts | **Office expense category** dropdown | `/hdc/api/office_expense_categories` → *Office expenses* |
| Money Center | Stage dropdown, office categories, **Pending context** | *Stages*, *Office expenses*, *Accounts* (`/api/accounts/pending_context`) |
| Cash flow | Embedded **New Transaction form** can't be submitted; add-on-the-fly Party / Account / Project fail | posts to `hdc_new_transaction` & `/hdc/accounts/new-transaction/*` → *Accounts* |
| Deliveries & transfers | "Available stock" check in delivery form | `/api/v2/purchase/material-available`, `material-stock-scope` → *Purchase materials* |
| Stage material usage | PO picker in usage form | `/api/v2/purchase/usage-po-options` → *Purchase orders* |
| Stage library | Page won't open without a project and redirects to Projects (→ 403) | *Projects* list |
| **Every custom user** | "Created by / edited by" row traceability on **every page** (`core/audit.js`) | `/hdc/api/row_actors` → *System lookup APIs* (a catch-all bucket for `/hdc/api/`) |

The static scan found more of the same pattern (form posts across permissions). These buttons show up but fail:

| Page (owner permission) | Buttons/forms that post to another permission |
|---|---|
| Project detail | owner payments (*receipts*), add/pay subcontractor, stage status/progress/delete (*stage edit*), drawings upload/replace/delete, shift-to-company/sub, sub attendance/progress: **15 forms, 6 permissions** |
| Subcontractor attendance | add/edit/toggle crew worker → *Subcontractor workers* |
| Timekeeping | delete/reactivate attendance → *Timekeeping corrections* |
| Payroll salary cards | delete run → *Payroll generation* |
| Purchase stock | shift stock → *Deliveries* |
| Supplier detail | "Add new material" → *Purchase materials* |
| Office staff ledger list | add staff / toggle staff / next code → *Office staff* |
| Office management | allowance categories → *Office allowances* |
| Account edit | delete → *Manage accounts* |
| All entries | edit/void transaction → *Accounts* |

**Root cause:** permissions are tied to **URLs**, but one screen uses URLs from several areas. Helper/lookup URLs (dropdown data) are treated as if they were separate pages.

### F2. One URL = many actions, so you can't allow one and block another

**30 routes** handle several different operations through a single URL (`action=` in the form). The permission system sees only "Write", so it's all or nothing:

| URL | Operations bundled under one "Write" |
|---|---|
| `/hdc/accounts` | create/update/suspend/activate/delete **account**, create/update/void/reverse/restore **transaction** (10) |
| `/hdc/settings` | backup, restore, maintenance, backfill, **wipe data** (8) |
| `/hdc/accounts/cashflow/register` | create/amend/void/restore entry, add category/subcategory/party |
| `/hdc/accounts/cashflow/reconciliation` | save counted, reconcile, **lock/unlock day** |
| `/hdc/payroll/generate` | generate, **pay worker, pay all** |
| `/hdc/stage-library`, `/hdc/trades`, `/hdc/expense_categories`, `/hdc/parties`, `/hdc/accounts/manage`, shared parties/settlements, tool inventory, estimation formulas, … | add / edit / delete / toggle |
| `/hdc/tool-rental/<id>/return`, `/transfer`, `/create` | full/partial/credit/no-charge, site vs customer, internal vs external |

So today you can't say "may record transactions but may not void them", or "may generate payroll but not pay".

### F3. Pages that bundle many independent things under one permission (need "layer 3")

Only **7 of 117 templates** check permissions inside the page; the other 110 are all-or-nothing. The biggest mixed pages:

| Page | Independent parts (should each be tickable) |
|---|---|
| Dashboard | Money KPIs, operational KPIs, *Contract vs Expense* chart, *Expense distribution*, Quick access, Recent projects |
| Reports | Glance, Project, Worker, Material, Time tracking, Stage cost, Expense breakdown, Profitability + **each Export** |
| Glance report | 30-day cash pulse, Subcontractor snapshot/watchlist, Project risk, Delayed stages, Material stock watch, Budget overruns, Alerts |
| Money Center | Tabs **Money IN / OUT / Transfers / Pending**, Treasury accounts, Recent ledger, Quick transaction |
| Accounts workspace | Accounts with balances, Suspended accounts, Transaction form, Ledger workspace, Quick actions |
| Accounts Hub | Links to 6 other areas (31 dead links for a hub-only user) |
| Subcontractor attendance | Crew attendance, Crew summary (**labour cost**), Named workers, Worker earned/paid, Daily register, Legacy register |
| Worker ledger | Info, Ledger (**money**), Attendance, Tips, Settlement |
| Workers list | List, **wage rate** column, ledger links, add/edit/toggle |
| Tool rental detail | Returns, Payments (**money**), Site transfers, Move tools, Rental info + accounts |
| Supplier detail | Record purchase, Record payment, Add material, Purchase history, Supplier ledger |
| Payroll generate | Generate, Items, Day-wise status, Calendar matrix, **Pay worker / Pay all** |
| Purchase stock | Totals, Material summary, Delivery log, Edit delivery, Shift stock |
| Deliveries | Record delivery, Shift stock, Records, Edit |
| Office expenses / Office management / Personal management | form, categories, records/totals |
| Settings | Backup, Restore, Maintenance, Backfill, **Wipe** (stays admin-only, but should split) |

### F4. Buttons and links shown even though they lead to "Access denied"

In the single-permission test, **86 of 240 page loads** showed links, buttons or background calls to areas the user doesn't have. Worst offenders:

| Page | Dead links/calls | Leading to |
|---|---|---|
| Money Center | 39 | Accounts, Cash flow, Manage, Workers, Suppliers, Subcontractors, Office ledger… |
| Accounts Hub | 31 | Cash flow (15), Money Center, Manage, Entries, Shared, Personal |
| Manage accounts | 16 | Accounts |
| Purchases overview | 11 | every purchase subpage |
| Dashboard | 9 | 9 different modules |
| Tools pages (5) | 4–6 each | inventory / tracking / rentals nav |

### F5. "Read only" is done with CSS, not real hiding

For read-only users, `base.html` hides **all POST forms with CSS** (`display:none`). **44 pages** still send the full forms in the HTML. Also:
- Buttons that save in the background (`fetch`, modals) stay visible and only fail on click.
- It's page-wide, so you can't have "read the list + write only the comment box".
- Links such as "Edit", "Delete" and "Void" (`<a>` / JS) aren't covered at all.

### F6. Five overlapping permission systems

| # | System | Where |
|---|---|---|
| 1 | Base role (admin/manager/accountant/staff) + hard-coded `ACCESS_MATRIX` | `hdc/extensions.py` |
| 2 | `_admin_only()` / `_money_only()` / `_money_write_required` route guards | `hdc/extensions.py`, many routes |
| 3 | Sidebar `roles` list, maintained separately from the permission tree | `templates/hdc/shared/base.html` |
| 4 | Custom page map (Read/Write) + legacy stage limit | `hdc/services/permissions.py` |
| 5 | Strict record access + report-section cascade | `hdc/services/record_permissions.py` |

The sidebar is a **second, hand-written copy** of the menu: only 4 of 27 links declare their permission, and the rest guess it from the URL. Adding a page means editing 3 places.

### F7. Admin experience

- Everything happens inside one table row on `/hdc/users`: role, custom-map switch, stage limit, strict mode and cascade.
- No named roles, no "copy from user", no templates. Every user is set up by hand.
- No preview of what the user will see, and no "view as user".
- Strict mode: new records aren't assigned automatically, access doesn't flow from parent to child, and there's no "all including future" option.
- Can't deactivate a user (only delete).

### F8. Not bugs, just data (for completeness)

Most other non-200 responses in the live test (404s and redirects on `/…/1/edit` and similar) happen because the test database had no such record. They aren't permission defects.

---

## 3. Target design

### 3.1 One permission tree (single source of truth)

```
Level 1  Module        accounts
Level 2  Page          accounts.money_center
Level 3  Section       accounts.money_center.tab_in        (tab / card / panel / report / widget)
Level 4  Action        accounts.money_center.tab_in:create (view · create · edit · delete · void ·
                                                            restore · approve · pay · export · print)
Level 5  Field         accounts.money_center.*#amount      (sensitive: rates, amounts, profit, balances)
```

- Stored in **one Python registry** (`hdc/services/permission_registry.py`). Each node has: key, label, parent, icon, **routes** (endpoint + method + `action` value / JSON op), **UI elements**, **dependencies**, **sensitive fields**, plus any fixed ceiling (such as admin-only).
- **The sidebar, the admin tree, route enforcement and template checks are all generated from this registry.** No second copy.
- Ticking a parent ticks all its children (tri-state). Unticking a child makes the parent "partial".

### 3.2 Fixing "hide one, lose the other": the dependency rule

- **Helper URLs are not user-facing permissions.** Dropdown lookups (`project_stages`, `office_expense_categories`, `material-available`, `usage-po-options`, `pending_context`, `row_actors`, next-code APIs, …) are registered as **helpers**.
- A helper is allowed **when the user has any node that declares it needs that helper**. For example, *Deliveries → Record delivery* declares `material-available`, so a delivery user can check stock without being given "Purchase materials".
- Helper responses still pass through **data scope**: a stage dropdown returns only stages the user may see.
- Cross-area forms get a proper owner. For example, the Cash flow register's embedded transaction form belongs to `cashflow.register:create`, not to Accounts.
- **CI check:** every `fetch`/form/link target in a page must be either in the same node, a declared helper, or wrapped in a permission check. Otherwise the build fails. That makes F1 and F4 impossible to reintroduce.

### 3.3 Action-level enforcement (fixes F2)

- The server resolves each request to an exact action key: `(endpoint, method, form action / JSON op) → node:action`. For example, `POST /hdc/accounts action=void_transaction` → `accounts.transactions:void`.
- **Fail closed:** an unmapped route or `action` value is denied for restricted users, and a CI test lists every unmapped one.
- The existing strict record check (which rows) runs **after** the action check (which operation). Both must pass.

### 3.4 Hiding in pages: remove, don't CSS-hide (fixes F3, F4, F5)

- Jinja: `{% if can('accounts.money_center.tab_in') %}…{% endif %}` and a macro `{% call perm('…') %}…{% endcall %}` around every tab, card, button and sensitive column.
- `perm_link(endpoint, …)` renders a link **only if the target is allowed**. It replaces the plain `url_for` links that caused 86 dead-link pages.
- Tabs: hidden tabs and their panes aren't rendered at all, and the first allowed tab opens automatically.
- Sensitive fields: the value is replaced by "—" **on the server** (never sent to the browser), including in exports and totals.
- If every section on a page is hidden, the page itself is hidden from the sidebar.
- The CSS "hide all forms" rule is removed once all pages are converted.

### 3.5 Roles + overrides (ERP standard)

- New tables: `hdc_role` (name, description, built-in flag), `hdc_role_grant` (role, node key, action), `hdc_user_role` (user ↔ role, several allowed), `hdc_user_override` (user, node, action, **grant or deny**).
- **Effective access = union of the user's roles + user grants − user denies** (deny wins). Admin stays unrestricted.
- Built-in templates (editable copies): *Accountant, Site Supervisor, Data-Entry Operator, Store Keeper, Viewer (read-only)*.
- "Copy role", "Copy access from user", activate/deactivate user, force sign-out.

### 3.6 Data scope (keeps PR #63/#64, easier to use)

For each data type: **All** · **Selected** · **Selected + everything under it (incl. future)** · **Only records they created**.
Project → Stage → children inherit when "+ everything under it" is chosen, so new stages, expenses and attendance under a granted project are included automatically. The report-section cascade becomes normal Level-3 nodes under Reports.

### 3.7 New admin screens

| Screen | Contents |
|---|---|
| **Roles** `/hdc/roles` | list, create/copy, the permission tree editor |
| **User access** `/hdc/users/<id>/access` | roles, overrides, data scope, status |
| **Tree editor** | expand/collapse levels 1–5, tri-state boxes, columns *View · Create · Edit · Delete/Void · Export · Print*, search, "all/none" per branch, dependency notices ("Record delivery also allows the stock check. Included automatically") |
| **Effective access preview** | the exact sidebar and page sections the user will get |
| **View as user** | read-only, admin-only preview session (logged in the Event Recorder) |
| **Change review** | before/after diff on save; every change audited |

---

## 4. Page redesign list (what changes on each page)

"Split" means the page gets independent sections and action buttons that can each be hidden. The page layout mostly stays the same.

| Page | Split into (Level 3) | Separate actions (Level 4) | Sensitive fields (Level 5) |
|---|---|---|---|
| Dashboard | money KPIs · operational KPIs · each chart · quick access · recent projects | — | contract value, expense totals, profit |
| Reports | each report card + its own export | export, print | profit, cost |
| Glance | each of the 8 widgets | — | cash pulse amounts |
| Money Center | IN · OUT · Transfers · Pending tabs · treasury · recent ledger | create per tab, approve pending | balances |
| Accounts workspace / Hub | balances list · suspended · transaction form · ledger · quick actions | create/update/suspend/activate/delete account; create/update/void/reverse/restore txn | balances |
| Cash flow register / reconciliation / report | register · new txn form · reconciliation · report | create, amend, void, restore, add category/party, save counted, lock/unlock day | — |
| Project detail | stages · financials · owner payments · subcontractors · time · purchases · drawings (already partly done; convert to keys) | stage add/edit/delete/status, pay sub, receipt add/void, drawing upload/delete | contract value, margins |
| Subcontractor attendance | crew attendance · crew summary · named workers · daily register · legacy | mark, edit, add/toggle worker | labour cost |
| Subcontractor ledger/payments | ledger · payment history · pay form | pay, void | amounts |
| Workers / Worker ledger | list · ledger · attendance · tips · settlement | add/edit/toggle, advance, pay, rate change | **wage rate**, balances |
| Timekeeping | daily sheet · filtered records · status | mark, edit, delete, reactivate | — |
| Payroll | generate · items · day status · calendar · history · salary cards | generate, pay worker, pay all, delete run, print cards | salary amounts |
| Purchases (6 pages) | form · list · edit panel · stock summary · delivery log · supplier ledger | create, edit, void, pay supplier, shift stock | unit price, supplier balance |
| Tools (6 pages) | dashboard widgets · rentals · returns · payments · transfers · inventory · purchase/scrap · tracking · reports | create rental, return, pay, transfer, purchase, scrap | rental charges |
| Office / Personal management | staff · attendance · ledger · allowances · expenses · categories | add, edit, pay, void | salary, allowances |
| Shared expenses | bills · parties · settlements · ledger · report | create, edit, void, settle | — |
| Estimation | calculators · formulas · project estimation | save, delete formula | rates |
| Parties, Trades, Expense categories, Stage library | list · form | add, edit, delete, toggle | — |
| Settings (admin only) | backup · restore · maintenance · backfill · wipe | each separately confirmable | — |

---

## 5. Implementation phases (each phase = 1 PR, fully tested, nothing breaks for current users)

| Phase | What | Result for you |
|---|---|---|
| **0 Quick fixes** | Register the 8 helper URLs from F1 as dependencies of the pages that use them; let the Cash flow form submit on its own permission; Stage library opens with a project picker | Today's single-page users stop hitting hidden 403s |
| **1 Registry & engine** | `permission_registry.py` with all modules/pages/sections/actions; action resolver for all 273 routes and 30 multi-action URLs; **automatic conversion** of existing `permissions_json` (page Read/Write → all its children) | Same behaviour as now, but ready for depth. CI fails on any unmapped route/action |
| **2 In-page hiding** | `can()`, `perm()`, `perm_link()`; convert all 117 templates page by page (table in §4); server-side field masking; remove CSS form-hiding | Allow part B without part A on every page; no dead buttons |
| **3 Roles & overrides** | Role tables, built-in role templates, multi-role, grant/deny overrides; old base roles become built-in roles | Set up once, assign to many users |
| **4 Admin UI** | Roles page, user access page, tree editor, effective-access preview, view-as-user, change diff + audit | Easy, visual, safe to configure |
| **5 Data scope upgrade** | All / Selected / Selected+children (incl. future) / Own records; cascade folded into tree | Much less clicking for strict users |
| **6 Cleanup** | Retire `ACCESS_MATRIX`, `_money_only`, sidebar role lists and legacy stage limit after parity tests; update README & operator guide | One clear permission system |

### Testing approach (applies to every phase)

- **Auto-generated matrix test:** for **every node**, create a user with only that node and check that:
  - its pages return 200,
  - its sections render,
  - every other section is absent from the HTML,
  - there are zero links/calls to forbidden areas,
  - its helpers return 200,
  - forbidden actions return 403.
- Parity test during migration: an old-style user gets exactly the same access before and after conversion.
- The existing 620+ tests keep passing, and the current permission test files are extended, not replaced.

---

## 6. Decisions needed before building

1. **Several roles per user**, or one role + personal overrides only?
2. **Deny overrides:** should a user-level "deny" beat a role grant? (Recommended: yes.)
3. **Field masking:** hide sensitive values (rates, amounts, profit) completely, or show them as "—"?
4. **View as user:** allowed for admins? (Recommended: yes, read-only, audited.)
5. **Sub-admin:** should any non-admin ever be allowed to manage users/roles (for example, only below their own level)?
6. **Default for new pages/sections added later:** hidden for everyone except admin until granted? (Recommended: yes, fail closed.)
7. Start with **Phase 0 + 1 together**, or Phase 0 alone first?
