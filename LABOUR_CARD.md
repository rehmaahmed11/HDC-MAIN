# Labour Card

Sidebar → **Workforce → Labour Card** (`/hdc/labour-card`). One page that
answers *"what does this worker's card look like, and what is payable?"*

## How to use it

1. Pick the labour from the **searchable list-style combo box**: either
   click the arrow at the right of the *Worker Name* field to browse the
   full worker list (scrollable), or type a few letters of his name to
   filter it, then pick him — the card opens below as soon as he is
   selected. (The arrow is added on top of the same shared searchable
   combo used across the app; typing, arrow keys, Enter and Escape all
   work, and without JavaScript the field still posts as a plain select.)
2. Choose the **From / To** dates, or use the quick buttons:
   This Week · Last Week · This Month · Last 30 Days · All Time.
   The page opens on **This Week (Monday → today)**. Leaving *From*
   blank means all time.

## What the card shows

**Site-wise Work** — one row per site (project) the worker worked on in
the selected dates:

| Site | Days Worked | Overtime (hrs) | Amount (PKR) | Fuel (site)* | Food (site)* |
|---|---|---|---|---|---|

with a **Grand Total** row under it.

**Summary & Payable** — the money side of the same period, in order:
Opening balance brought forward (owed before the From date) + Grand Total
earned − Advances taken − Amount paid − Settlements = **Balance**, and
under it the one big figure: **Payable Now** — what is still owed to the
worker on the whole ledger today. If advances/payments are ahead of
earnings, the box says so instead of showing a negative payable.

**Advances & Payments in This Period** — every advance he took and every
amount paid to him in the selected dates, line by line.

**Fuel & Food Detail** — the lines behind the * columns (see below).

**Last 4 Weeks** — week by week (Monday–Sunday, ending with the week of
the To date): days worked, overtime, earned/payable amount, advance
took, got paid, settled, that week's balance, and **Pending** — what was
still owed at that Sunday.

## Definitions (same as Ledger / Statement, so screens never disagree)

* **Earned** comes from Timekeeping (`wage_calculated` on live time
  entries) plus legacy attendance wages that were never migrated —
  exactly the figures the Worker Ledger and Statement use.
* **Days** = regular hours ÷ 8. Overtime is the portion beyond a day's
  first 8 hours. A day split over two sites shows as fractions that add
  up (4h + 4h = 0.5 + 0.5), so the days column always sums to the total.
* **Balance** = earned − advances − payments − settlements.
  **Tips are never deducted** — a tip is extra cash on top of the wage;
  the card mentions tips but they do not change the balance or payable.
* **Payable Now** is the canonical whole-ledger payable
  (`_worker_payable_snapshot`): pay against it only through the worker's
  own Advance / Payment screens (buttons are on the card).
* A week's **Pending** is cumulative (everything owed up to that
  Sunday), because a payment made this week usually settles last week's
  wage. Per-week remainders would make a worker look overpaid.

## Fuel & Food columns — information only, never in the total

The *Fuel (site)* / *Food (site)* columns are that **site's own
expenses** (Expenses section) in the selected dates whose expense
category name contains **Fuel / Petrol / Diesel** resp. **Food /
Refreshment**. The card lists which categories it counted, and the
detail table shows every line.

Two rules to remember:

1. **They are NOT added** to the worker's Grand Total, Balance or
   Payable — they are a site cost mentioned next to his work.
2. They are the **whole site's** spend in those dates, so every worker
   who worked that site sees the same figure. Never add these columns
   across workers.

If your fuel is recorded under a different category name (e.g.
"Transport"), rename/adjust it in **Expense Categories**, or record fuel
lines under a "Fuel"/"Petrol" category, so the card can recognise them.

## Code map

* `hdc/services/labour_card.py` — all aggregation (`labour_card()`,
  `_last_four_weeks()`, `classify_site_expense()`); no recomputed payable,
  it reuses `services.ledger._worker_payable_snapshots`.
* `hdc/routes/labour_card.py` — thin GET handler; parses worker + dates,
  runs the same time-entry/tip reconciliation the Ledger page runs.
* `templates/hdc/workers/labour_card.html` — picker + card + print CSS
  (Print / Save PDF gives a clean hand-out sheet).
* Sidebar entry in `templates/hdc/shared/base.html`; permission page id
  `labour_card` in `hdc/services/permissions.py` (assign it per user in
  Users → access editor; admins see it automatically, operational roles
  read it like the other workforce pages).
* `tests/test_labour_card.py` — hand-checked fixture: split days,
  migrated vs un-migrated legacy wages, voided/out-of-range fuel,
  cumulative weekly pending.
