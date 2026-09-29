# Shared Expenses — one bill, several heads, one ledger

FBM, HDC and Home (and whoever else is sharing at the time) regularly pay for
something together: car fuel, a workshop bill, a utility. The money leaves **one**
account, the cost belongs to **several** heads, and "who has borne how much, and
who owes whom" has to be answerable without a second set of books.

`/hdc/accounts/shared` answers that. It is an **allocation ledger on top of
Accounts**, not a second place where money moves.

```
Accounts section                    Shared Expenses module
--------------------------------    ------------------------------------------
hdc_account_txn  ←── link / post ──  hdc_shared_expense   (one bill)
  ↑ the only money row               hdc_shared_share     (one head's slice)
  ↑ full amount, once                hdc_shared_settlement (head → head / account)
                                     hdc_shared_party     (the heads themselves)
```

## 1. The one rule: money exists once

Every rupee that moves is a row in `hdc_account_txn`, created by the accounts
engine. A shared expense carries **either**

* `cf_entry_id` / `txn_id` — the bill is tied to a real accounts entry, because
  the operator **linked** one already recorded in Accounts, or **posted** a new
  one from the shared-expense form through
  `save_manual_cash_flow_entry()` (`source_type='shared_expense'`); or
* neither — an allocation recorded ahead of the accounts entry. It is shown
  everywhere with a *Not in Accounts* warning and never treated as paid.

Posting therefore inherits every accounts guarantee for free — day lock,
overdraft block, duplicate-source detection, idempotency key, audit trail — and
the register, All Entries, the Cash Flow report and the Money Center all see the
payment as an ordinary entry. There is no second money table to reconcile.

The full payment is counted exactly once, in Accounts. The shares here explain
*who that payment was for*; they are never added to the ledger again.

## 2. What is stored

| Table | Holds |
|---|---|
| `hdc_shared_party` | A sharing head / ledger: FBM, HDC, Home, a person, a site. Name is the identity; `kind` is presentation (`business` / `home` / `person` / `other`); optional `account_id` pre-fills the two sides of a settlement transfer; `is_default` pre-ticks it on a new bill; heads with history are deactivated, never deleted. |
| `hdc_shared_expense` | One bill: date, title (`Car Fuel`), category, total, who paid (`payer_party_id`), which account it left, split mode (`equal` / `custom` / `percent`), the accounts link, reference, note, `idempotency_key`, void fields. |
| `hdc_shared_share` | One head's slice of one bill. `amount` (float, UI) + `amount_minor` (integer paisa, authoritative) + `percent_bp` for percent splits. `UNIQUE(expense_id, party_id)`. |
| `hdc_shared_settlement` | One head squaring up with another head *or* with an account: date, amount, note, and (optionally) the accounts transfer that moved the money. |

Amounts are exact: integer paisa in the `*_minor` columns, kept in step with the
float columns by the same `before_insert`/`before_update` listeners the rest of
the app uses.

## 3. The split engine (`compute_split`)

* `equal` — total in paisa divided by the number of selected heads; the
  remainder is handed out **one paisa at a time** to the first heads in order, so
  the slices always re-add to the total (5,000.00 ÷ 3 → 1,666.67 / 1,666.67 /
  1,666.66).
* `custom` — a rupee figure per head; the engine **refuses** the save unless the
  figures add up to the total exactly.
* `percent` — basis points per head (`3333` = 33.33%), must total 10,000; the
  rupee slices are derived from the total, with the same odd-paisa rule.

Either column may stand in for the other: a head with no percentage is read from
its rupees, and a head with no amount is read from its percentage.  The form
works both columns out live, so refusing the one the operator did not touch
would lose work for nothing — what is typed is what is saved.  A head with
neither figure is still refused.

Duplicated or empty participants are refused, so one head cannot appear twice on
one bill, and a bill always names at least one head.

### The split on screen

`static/hdc/js/pages/shared_expenses.js` keeps the split table live, and mirrors
the engine's arithmetic exactly (integer paisa, odd paisa handed out one by one,
rounding drift given to the largest share) so **what is on screen is what gets
saved** — `tests/shared_expense_split_harness.js` drives the real script in a DOM
stub and `tests/test_shared_expenses.py` pushes the figures it ends up with
through `compute_split` to prove it.

* Equal — every ticked head's rupees appear the moment the total is typed, and
  re-appear whenever the total or the ticks change.
* Percentages — a percentage typed in works out that head's rupees at once; an
  amount typed in works out its percentage.  Figures the page works out are
  tinted.
* Custom amounts — the rupees are the operator's, and each head's percentage
  follows along.

The line under the table says whether the figures add up — and, for a
percentage split, that percentages carry two decimals, so a three-way even
divide is what **Equal** is for (`33.33 × 3` is 99.99%, not 100%).

## 4. Who owes whom

`balance = shares − paid − settled_out + settled_in`, per head, derived on every
read (`party_balances`). Positive means the head **owes** (it has taken more
cost than it has funded); negative means the head **gets back**.

* `payer_party_id` is who fronted the money. Leave it empty and the module says
  so: the amount appears in the `outside` bucket (*paid from an account, not yet
  credited to a head*) rather than being silently attributed.
* A **settlement** is one head paying another: it moves the payer's balance up
  and the payee's down. It can post a real accounts transfer (`direction =
  'transfer'`, `source_type='shared_settlement'`) or be recorded alone, and it is
  flagged when no accounts entry backs it.
* `totals['balanced']` is the check that the module cannot lie: the balances of
  all heads always sum to zero (plus the outside bucket when the payer is
  unnamed).

## 5. Pages

| Page | URL | Job |
|---|---|---|
| Ledger | `/hdc/accounts/shared` | Every bill, filtered by date / head / category / status, with per-head balances, the *outside* bucket and the module summary |
| Head statement | `/hdc/accounts/shared?party_id=<id>` | One head's running balance — bills, its slice, what it paid, settlements |
| All expenses | `/hdc/accounts/shared/expenses` (+ `.csv`) | Flat list: shares per head per bill, paid-by, accounts link status |
| New / edit | `/hdc/accounts/shared/expenses/new`, `/expenses/<id>/edit` | The bill: heads and their split, payer, money source (**post** a new accounts entry / **link** an existing one / none) |
| Detail | `/hdc/accounts/shared/expenses/<id>` | One bill: shares, the linked accounts entry, void / restore / delete, re-link |
| Heads | `/hdc/accounts/shared/parties` | Add / edit / deactivate heads and their usage |
| Settlements | `/hdc/accounts/shared/settlements` | Head-to-head settlement register with balances and void/restore |
| Report | `/hdc/accounts/shared/report` (+ `.csv`) | The clean share report: share × head matrix, by category, by month, print-friendly |

Void / restore never deletes: voiding a bill keeps its shares for the record and
offers to void the accounts entry it posted; the entry is voided through the
accounts engine (`void_manual_cash_flow_entry`) so Accounts stays consistent.

## 6. Permissions

The module lives under `/hdc/accounts`, which `ACCESS_MATRIX` already restricts
to `admin` / `accountant` for reads, and every handler additionally checks
`_admin_only()` at the top — the same rule as the rest of the Accounts section.
`tests/test_money_permissions.py` pins the matrix, so a new route cannot land in
no bucket.

## 7. Tests

`tests/test_shared_expenses.py` (32 tests) covers the arithmetic (odd-paisa
distribution, percent basis points, custom sums, refusals) and the app flows:
posting a bill creates exactly one ledger row, double-submit cannot create two,
linking refuses an entry another bill already claimed, voiding a bill voids its
accounts entry, and staff cannot reach the module.

```
.venv/bin/python -m unittest tests.test_shared_expenses -v
.venv/bin/python -m unittest discover -s tests -p 'test_money_permissions.py' -v
```
