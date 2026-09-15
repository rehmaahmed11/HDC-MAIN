# Subcontractor Labour Attendance — Simplification Plan

**Status:** IMPLEMENTED (2026-09-14) — attendance-only change, zero payment
changes, same page style ("same vibe"), confirmed decisions:
- `2000`/`1200` = per-worker-per-day rate → Mason 17×10×2,000 = 340,000,
  Labour 30×10×1,200 = 360,000, combined 700,000.
- Informational only: feeds Labour Cost / margin / project P&L; nothing
  auto-posts to accounts, no payment route touched (pinned by
  `tests/test_subcontract_team_attendance.py::test_no_payment_side_effect`).
- Named workers + daily register kept as a collapsed **Advanced (optional)**
  panel — still works, no longer the default path.
- Free "number of days" entry with an anchor date + optional from/to reference.

## Implemented changes (map)

| What | Where |
|---|---|
| New model `SubcontractTeamAttendance` (crew summary row, `man_days` property) | `hdc/models/subcontract.py` (+ export in `hdc/models/__init__.py`) |
| `sub_labour_rollup()` + `subcontract_scope_ids()` — the single shared labour view | `hdc/services/subcontract.py` |
| `POST /hdc/subcontractor/<sid>/team_attendance` (add/edit, duplicate guard, scope check, audit event) and `POST .../team_attendance/<rid>/delete` | `hdc/routes/subcontractors.py` |
| Attendance page: new "Crew Attendance (Simple)" card + note strip + Crew Summary table; named-worker/daily-register moved into collapsed Advanced panel; legacy rows kept as read-only history | `templates/hdc/subcontractors/subcontractor_attendance.html` |
| Ledger KPIs (Labour Cost / Labour Usage / margin) read the roll-up; new event types in the ledger filter | `hdc/routes/subcontractors.py` + `templates/hdc/subcontractors/subcontractor_ledger.html` |
| Reports glance: sub labour cost map includes crew rows (project P&L matches the page) | `hdc/routes/reports.py` |
| 8 regression tests (totals, manual total, no double count, no payment side effect, duplicate, scope, delete, ledger+reports) | `tests/test_subcontract_team_attendance.py` |

No endpoint renamed, no table dropped, nothing on the payment side modified.

---

## 1. What you are asking for (my reading)

Right now the subcontractor page behaves like our *own* staff attendance: every
labour worker is registered by name, and for every single day he must be marked
**Present / Absent / Not Assigned**, with working hours, OT auto-calc, project
and stage chosen row by row in the "Daily Register Sheet".

You do **not** want that for subcontractors. You only want three facts per crew:

| Crew | Days | Workers per day | Rate per worker per day |
|---|---|---|---|
| Mason | 17 | 10 | 2,000 |
| Labour | 30 | 10 | 1,200 |

…so that the system can answer, at any time:

```
man-days  = days x workers-per-day
labour cost = man-days x daily rate
Mason : 17 x 10 x 2,000 = 340,000
Labour: 30 x 10 x 1,200 = 360,000
                            ---------
                total sub labour = 700,000
```

That number is what we need for **cost / margin** (contract value − sub labour
cost) and for knowing **how much the subcontractor is paying his own men**. We
are deliberately *not* keeping a day-wise, name-wise proof of attendance for
other people's labour — it is their headache, not ours.

**Note to be added in the UI and in the docs (your "note to explain what we
added"):**

> Subcontractor labour is recorded as a team summary only: which trade, how many
> workers, for how many days, and at what daily rate. Exact day-wise / per-worker
> attendance is intentionally NOT maintained for subcontractor labour. Individual
> wage disbursement stays with the subcontractor.

---

## 2. Where the current code stands (facts, not opinions)

| Piece | File | Current behaviour |
|---|---|---|
| Daily register sheet | `hdc/routes/subcontractors.py` → `hdc_subcontractor_labour_attendance` (`bulk_mode=1`) | One `SubcontractLabourAttendance` row per worker per day per stage; `total_labour_paid = wage_rate if present else 0`; hard duplicate block via unique constraint `uq_sub_labour_sub_stage_worker_date` |
| Workers roster | `SubcontractLabourWorker` (name, phone, trade, daily_wage) | Must be created before any attendance is possible |
| Per-worker ledger | `hdc_subcontractor_worker_ledger`, `.../workers/<wid>/pay` | earned = Σ attendance rows, paid = Σ `SubcontractLabourPayment`, posts to unified accounts |
| Subcontractor's own daily attendance | `SubcontractAttendance` (`present_count`, `work_done_pct`) | Feeds `logged_progress_percentage` → `payable_amount`; entered from project detail page |
| Ledger KPIs | `hdc/routes/subcontractors.py` (~line 1288-1306) | `labour_days`, `labour_men_total`, `labour_cost_total`, `sub_margin_*` all read `SubcontractLabourAttendance` only |
| Reports / project P&L | `hdc/routes/reports.py` (~line 384-390) | `sub_labour_cost_map` = Σ `total_labour_paid` per subcontractor |

Consequence: **whatever we add must flow into those three readers**, or the
subcontractor margin, project cost and reports silently disagree with the page.

---

## 3. Proposed design

### 3.1 New table `hdc_subcontract_team_attendance` (model `SubcontractTeamAttendance`)

| Column | Type | Meaning |
|---|---|---|
| `id` | int PK | |
| `subcontractor_id` | FK → `hdc_subcontractor.id` | required |
| `project_id` / `stage_id` | FK, nullable | required when the crew is charged to a stage; validated against the sub's existing scope helpers |
| `worker_type` | varchar(80) | `Mason`, `Labour`, `Electrician`, … free text + datalist from `WorkerTrade` and from existing `SubcontractLabourWorker.trade` |
| `date` | date | anchor date = entry/period-end date; **this is what the existing date-range filters keep using** |
| `period_from` / `period_to` | date, nullable | optional reference "1 Jan → 31 Jan", display only |
| `days_count` | int | e.g. 17 |
| `workers_count` | int | workers per day, e.g. 10 |
| `wage_rate` | float | per worker per day, e.g. 2000 |
| `total_amount` | float | 340,000 — auto = `days × workers × rate`; editable |
| `total_manual` | bool | set when the user typed the total → rate is back-derived `total / man-days` |
| `notes` | varchar(250) | e.g. "as per contractor's register" |
| `activity_at` / `created_at` / `updated_at` | datetime | existing convention (`_activity_at_for`, `_pkt_now_naive`) |

No unique constraint on dates — one row per (crew, period) is a judgement call by
the person entering it. Overlapping same-trade rows only raise a **warning**, not a block.

### 3.2 One roll-up, everyone reads it

`hdc/services/subcontract.py` gains:

```python
sub_labour_rollup(sub_id, date_from=None, date_to=None, stage_id=None)
  → {'days': .., 'man_days': .., 'crew_rows': .., 'cost': ..,
     'avg_rate': .., 'workers_peak': .., 'legacy_rows': ..}
```

…combining **new summary rows + old daily rows**, and it becomes the single source for:

- `Subcontractor.labour_days_logged` / `labour_headcount_total` / `labour_cost_total` (model properties)
- ledger KPIs (`labour_days`, `labour_men_total`, `labour_cost_total`, `sub_margin_*`)
- `reports.py` sub-labour cost map → project cost / P&L
- the attendance page totals

Old daily rows are never deleted or rewritten; they simply keep adding their
cost into the same roll-up, so live data stays consistent during the transition.

### 3.3 Routes (additive; no existing endpoint is renamed — README rule)

| Route | Purpose |
|---|---|
| `POST /hdc/subcontractor/<sid>/team_attendance` | add / edit a crew summary row; validation: sub scope, `days>0`, `workers>0`, `rate>0 or total>0`; `_has_recent_duplicate()` guard (existing helper) |
| `POST /hdc/subcontractor/<sid>/team_attendance/<rid>/delete` | delete a row (mirrors the existing daily-row delete: hard delete + `SubcontractEvent` audit) |
| `GET /hdc/subcontractor/<sid>/attendance_page` | unchanged name, new layout (below) |

Every write logs `SubcontractEvent(event_type='team_attendance')` with
`from_value` / `to_value` / `amount`, matching the existing ledger trail.

### 3.4 Screen changes on `subcontractor_attendance.html`

1. **New top card — "Crew Attendance (Simple)"**: trade · days · workers/day ·
   rate/day → **live auto-total** (small inline JS, same style as the existing OT
   auto-calc), project/stage selects, notes, `Add Entry`.
2. **Note strip** (the explanation from §1) directly under that card.
3. **Crew summary table**: rows grouped by trade, per-row delete/edit, plus a
   footer: `Days · Man-days · Total cost`, filterable by the existing date range.
4. **"Workers (named) + Daily Register"** — the whole existing block moves into a
   collapsed *Advanced / optional* panel. Still functional for anyone who wants
   per-man proof, but no longer the default path; the "Add New Worker" form stops
   being a precondition for recording anything.
5. Existing legacy "Subcontractor Attendance Sheet" table stays as read-only history.

### 3.5 Ledger page

- `Labour Cost` KPI now = summary + legacy roll-up; `Labour Usage` shows
  `days` and `Man-days` (was row count) so "17 days × 10 masons" reads correctly.
- Margin KPIs (`contract − labour cost`) automatically follow the roll-up.

### 3.6 Docs

- Update `APP_REPORT.md` §5.7 (inputs list + the roll-up formula).
- This file becomes the permanent "what we added and why" note; README's
  module line for subcontractors gets one sentence pointing at it.

### 3.7 Tests

`tests/test_subcontract_team_attendance.py` (unittest + scratch DB via
`HDC_DB_PATH`, like the existing tests) asserting:

- Mason `17 × 10 × 2000 = 340,000` and Labour `30 × 10 × 1200 = 360,000`,
  combined roll-up `700,000`.
- Total override path back-derives the rate and flags `total_manual`.
- Summary + legacy daily rows add up exactly once (no double count).
- Delete of a summary row removes exactly its cost from ledger KPIs and reports.
- Out-of-scope project/stage is rejected; duplicate POST within the guard window
  is ignored.
- `scripts/check_layers.py` and `compileall` stay green; app boots (bootstrap
  `db.create_all()` creates the new table — already the project's pattern).

---

## 4. Deliberately NOT doing

- No deletion of `SubcontractLabourWorker` / `SubcontractLabourAttendance` /
  `SubcontractLabourPayment` tables or routes (live DB depends on them; endpoint
  names are frozen).
- No per-worker payslip, advance or OT logic for the subcontractor's men — the
  company pays the subcontractor, not his labour.
- No change to the subcontractor's *contract* side (lump-sum / sqft rate,
  retention, progress %, payments, receipts).

---

## 5. Open questions before I build

1. **The money formula** — is `2000` the per-worker-per-day rate (total
   340,000) or the total for the whole 17 days (rate ≈ 11.76/worker/day)?
2. **Does this row touch money?** Informational (cost/margin only, my default),
   or should it auto-deduct / auto-post against the subcontractor's payable in
   unified accounts?
3. **Named workers + daily register** — keep as collapsed optional advanced
   panel (my default) or remove from the UI completely?
4. **Period handling** — free "number of days" only (my default, simplest), or
   pick from/to dates and let the app count them (weekend/leave adjustments then
   need an override field anyway)?
