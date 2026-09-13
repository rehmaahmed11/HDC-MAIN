# Row traceability — "who entered this?" on every list, and grey voided rows

Every list-style section of the app shows **who created / last touched / voided
the entry**, and every voided row is highlighted **grey**. Both work on top of
the audit trail the app already writes (`hdc_user_activity`, filled by the
SQLAlchemy `after_flush` listener in `hdc/services/audit.py`).

## How it fits together

| Piece | File | Role |
|---|---|---|
| Tag filter | `hdc/services/actors.py` → `row_attrs_html` (Jinja filter `hdc_row_attrs`) | Renders `data-hdc-ent`, `data-hdc-id`, `data-hdc-label`, `data-hdc-void`, `data-hdc-void-reason` on a list row |
| Template globals | `hdc/app.py` (`actor_map`, `actor_for`, `hdc_row_attrs`) | Lets templates query the trail directly if they want an inline column |
| Batched lookup | `hdc/services/actors.py` → `actor_map` / `actor_payload` | One query per entity type per page (`flask.g`-cached), never one per row |
| JSON endpoint | `hdc/routes/api_actors.py` → `GET /hdc/api/row_actors` | What the page script calls: `?e=<entity>&ids=<csv>` (repeatable) |
| Browser script | `static/hdc/js/core/audit.js` (loaded from `shared/base.html`) | Collects every tagged row, fetches the trail once, appends the **Entered by** column / inline badge with the full trail in the tooltip |
| Styling | `static/hdc/css/hdc.css` (traceability block) | Grey highlighter `tr[data-hdc-void="1"] > td` (light `#d8d8d8`, dark `rgba(148,163,184,.28)`) plus the badge styles |

The void grey is **pure CSS on a server-rendered attribute**, so it works even
if the script never runs. The actor column is progressive enhancement: without
JavaScript the marks are still in the DOM for print/export and for any template
that wants to render `actor_map(...)` inline.

## Adding a new list

Tag each list row with its model:

```jinja
{% for row in rows %}
<tr {{ row|hdc_row_attrs }}>
```

The filter understands all the shapes the templates already loop over:

* a model instance → its `__tablename__` + `id`;
* a SQLAlchemy query row or tuple such as `(Expense, Project, Stage)` → the first
  model in the row;
* a view-model dict → `_hdc_entity` / `_hdc_id` keys, or the first model found
  under `row`/`record`/`item`/`entry`/`txn`/`expense`/`material`/… ;
* a dict with only scalars → pass the entity explicitly:
  `{{ s|hdc_row_attrs(entity_type='hdc_supplier', row_id=s.id) }}`.

Anything that is not a database row (aggregate dicts, `{% else %}` branches,
numbers, strings) renders nothing, so piping a loop variable through the filter
is always safe. A dict row is greyed when `is_void`/`voided_at` is set or its
`status` is `void`/`voided` — that is how the timekeeping day rows and the stage
event ledger grey themselves.

The labour ledger is resolved through its mirrors, because the ledger rows
themselves are internal: `actors.py` follows a cash row to its account
transaction (`source_type = labour_ledger_<entry_type>`), its time entry (work
rows) or the expense named in its notes (`TIP_EXPENSE_ID:` / `SETTLE_EXPENSE_ID:`
/ `SETTLEMENT_EXPENSE_ID:`), and marks the answer `"derived": true` so the
tooltip can say where the name came from. Settlement rows have no account
posting by design; a legacy settlement row with no linked record shows `—`.
Backfilled matches on the live database: 24 of 26 tip-style ledger rows resolve
an author (the 2 blanks are settlements). New cash rows are recorded directly —
`hdc/services/audit.py` now excludes only `hdc_labour_ledger` rows whose
`entry_type` is `work`, so advances, payments, tips and settlements land in
`hdc_user_activity` with their author as they are entered.

## Coverage verified against `hdc_erp/hdc_erp (1).db`

Rendering every static GET page plus the detail pages against a copy of the live
database: 0 errors, and 2,778+ tagged rows. Examples — reports 273, event
recorder 600, workers 77, worker ledger 118 (7 grey), account ledger 427 (42
grey), stage ledger 765 (481 grey), expenses 261, office expenses 125,
timekeeping 169, purchase-v2 stock 244, accounts 28, subcontractor ledgers and
payments, payroll, office staff, personal expenses.

`GET /hdc/api/row_actors` answers a 600-row page in ~0.01 s, and
`tests/test_row_traceability.py` (14 tests) pins the filter shapes, the void
flag, the endpoint and the ledger derivation; the whole suite is 149 tests.

## Which user is shown

The actor is the `username` column of `hdc_user_activity` (the audit listener
records the logged-in user for create/update/delete; rows written by background
reconciliations are recorded as `system`). Rows older than the audit trail show
`—` with the tooltip "No activity trail recorded for this entry".
