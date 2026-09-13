# Labour audit — wages, payments, tips and advances

**Scope:** every calculation and money movement touching a labourer —
attendance → wage, advances, salary payments, tips, settlement write-offs,
payroll runs, and their mirror in unified accounts.

**Method:** static trace of every code path that writes or reads labour money
(`hdc/models/workforce.py`, `hdc/services/timekeeping.py`,
`hdc/services/ledger.py`, `hdc/routes/workers.py`, `hdc/routes/timekeeping.py`,
`hdc/routes/payroll.py`, `hdc/services/accounts.py`), cross-checked against the
templates that display the numbers.

**This repository contains no database** (`*.db` is git-ignored and no
`hdc_instance/` is checked in), so nothing here is a statement about your live
figures — it is a statement about what the code *will* do to them. To check the
actual data, run the audit script shipped with this report:

```bash
python3 scripts/labour_audit.py --db hdc_instance/hdc_erp.db --json audit.json
```

It opens the database **read-only** (`file:...?mode=ro`), never writes, and
exits 1 when anything CRITICAL/HIGH is found. Every finding below has a matching
check in that script; `scripts/make_labour_audit_fixture.py` builds a throwaway
database seeded with each bug so the checks can be verified end to end.

---

## Verdict

There **was** a real inconsistency, and it was not cosmetic: the app computed a
worker's "balance due" with **two different formulas**, and the two disagreed by
exactly the amount of tips paid. Beyond that were nine further defects, four of
which could silently move real money (duplicate tips, resurrected voided tips,
orphaned cash after deleting a payroll run, and per-sqft workers who work for
free).

**All CRITICAL and HIGH findings are now fixed**, plus #12, #13 and #15. Every
fix is pinned by a regression test in `tests/test_labour_audit_fixes.py`
(31 tests). #11 and #14 are documented as deliberate/deferred.

| # | Severity | Area | Finding | Status |
|---|----------|------|---------|--------|
| 1 | 🔴 CRITICAL | Balances | Workers list and Advance screen subtract tips; ledger and payment screen do not | ✅ Fixed |
| 2 | 🔴 CRITICAL | Tips | Worker-ledger tips are not tagged with `TIP_EXPENSE_ID` — reconciler can duplicate them | ✅ Fixed |
| 3 | 🔴 CRITICAL | Tips | Voiding a tip does not void its expense, so the tip silently comes back | ✅ Fixed |
| 4 | 🔴 CRITICAL | Payroll | Deleting a payroll run deletes the worker payments but leaves the cash in accounts | ✅ Fixed |
| 5 | 🔴 CRITICAL | Wages | Per-sqft workers are paid `rate × 0` — the attendance screens never set `qty_sqft` | ✅ Fixed |
| 6 | 🔴 CRITICAL | Legacy data | `TimeEntry.attendance_id` means two different things; legacy wages get dropped | ✅ Fixed |
| 7 | 🟠 HIGH | Payroll | Overlapping payroll runs let the same days be paid twice | ✅ Fixed |
| 8 | 🟠 HIGH | Payroll | Advances above period gross are silently clamped to zero | ✅ Fixed |
| 9 | 🟠 HIGH | Wages | Hourly workers earn nothing for overtime | ✅ Fixed (straight time) |
| 10 | 🟠 HIGH | Ledger | Running balance uses `work` ledger rows while payable uses time entries | ✅ Fixed |
| 11 | 🟡 MEDIUM | Timekeeping | Day recalculation overwrites real clock-in/out times | ⚠️ Deferred |
| 12 | 🟡 MEDIUM | Wages | Voided migrated attendance makes the wage vanish from both totals | ✅ Fixed |
| 13 | 🟡 MEDIUM | Tips | Editing a tip ledger row can desynchronise it from its expense | ✅ Fixed |
| 14 | 🟡 MEDIUM | Safety | The duplicate guard is a 12-second window, not a real idempotency key | ⚠️ Deferred |
| 15 | 🔵 LOW | Robustness | `Worker.total_*` sums crash on `NULL` amounts | ✅ Fixed |
| 16 | 🟠 HIGH | Wages | Historical wages aren't frozen — a rate-card gap lets one edit rewrite them | ⚠️ Needs a decision |

---

## What changed

| Fix | Files |
|---|---|
| #1 One balance formula — `total_paid` excludes tips, new `total_tips` for display | `hdc/models/workforce.py`, `templates/hdc/workers/workers.html`, `worker_advance.html` |
| #1/#12 Snapshot counts unmigrated legacy wages, voided migration rows no longer hide them | `hdc/services/ledger.py` (`_worker_legacy_earned`), `hdc/models/workforce.py` |
| #2 Tips from the Workers screen get `TIP_EXPENSE_ID` in both the expense and the ledger row | `hdc/routes/workers.py` |
| #3 Voiding/restoring a tip or settlement moves its mirrored expense; voided expenses are never resurrected | `hdc/routes/workers.py`, `hdc/services/ledger.py` (`_linked_expense_for_labour_ledger`, `_worker_tip_expenses`) |
| #4 Deleting a payroll run voids the payments **and** their account transactions instead of hard-deleting | `hdc/routes/payroll.py` |
| #5 Per-sqft quantity is captured on the bulk sheet, the allocation modal and the edit form, and preserved on edit | `hdc/routes/timekeeping.py`, `hdc/services/timekeeping.py`, `templates/hdc/timekeeping/*.html` |
| #6 New `attendance_day_id` column; `attendance_id` stays exclusively the legacy link | `hdc/models/workforce.py`, `hdc/services/timekeeping.py`, `hdc/core/schema.py`, `hdc/core/bootstrap.py` |
| #7 Overlapping payroll runs are refused at generation | `hdc/routes/payroll.py` |
| #8 Unrecovered advances are reported as "carried fwd" on the salary card and run summary | `hdc/routes/payroll.py`, `templates/hdc/payroll/*.html` |
| #9 Hourly workers are paid overtime at straight time | `hdc/services/timekeeping.py` |
| #10 Ledger running balance reads wages from the time entry, not the `work` row | `hdc/routes/workers.py` |
| #13 Editing a tip/settlement syncs its expense and cannot drop the identity tag | `hdc/routes/workers.py` |
| #15 `NULL`-safe sums in the worker model properties | `hdc/models/workforce.py` |

**Schema change:** `hdc_time_entry.attendance_day_id` is added automatically by
`_ensure_timeentry_attendance_day_schema()` on boot (same idempotent
`ALTER TABLE … ADD COLUMN` pattern the rest of the app uses). No manual
migration step is needed, and existing rows keep working — the column simply
starts out `NULL` and is populated the next time a day is recalculated.

**Deliberately not changed:** overtime stays at **straight time** for both daily
and hourly workers (confirmed as correct practice) — the fix was only to stop
hourly workers losing their overtime entirely.

---

## 1. 🔴 Two different "balance due" figures for the same worker

The single most visible inconsistency.

**Ledger / payment path — tips are *not* deducted** (`hdc/services/ledger.py:274-281`):

```python
# Bug fix (2026-04-25): tip is gratis cash given on top of what the worker
# earned, so it MUST NOT subtract from the worker's owed balance. ...
paid    = salary_paid
balance = earned - advanced - salary_paid - settled
```

**Workers list / advance path — tips *are* deducted** (`hdc/models/workforce.py:56-71`):

```python
@property
def total_paid(self):
    cash_paid = sum(e.amount for e in self.ledger
                    if e.entry_type in ('payment', 'tip') and not e.is_void)   # ← tip
    return cash_paid

@property
def balance_due(self):
    return self.total_earned - self.total_advanced - self.total_paid - self.total_settled
```

The running-balance column on the ledger page agrees with the *service*
(`hdc/routes/workers.py:200-207`: work `+`, advance/payment/settlement `−`,
**tip stays 0**). So the split is:

| Screen | Source | Tips deducted? |
|---|---|---|
| Workers list → Balance Due (`workers.html:199-200`) | `Worker.balance_due` | **Yes** |
| Give Advance → Current Balance Due (`worker_advance.html:14`) | `Worker.balance_due` | **Yes** |
| Worker ledger KPIs + running balance (`worker_ledger.html:10-31, 167-171`) | `_worker_payable_snapshot` | No |
| Record Payment → Payable (`worker_payment.html:15-25`) | `_worker_payable_snapshot` | No |
| Accounts module → worker payable | `_worker_payable_snapshot` | No |

**Impact.** For any worker who has ever received a tip, the Workers list and the
Advance screen show a balance that is **lower than reality by exactly the tip
total**. A supervisor checking "how much do I still owe Ali?" on the Workers
list gets one number, opens Ali's ledger, and gets a different one. The
`_worker_payable_snapshot` behaviour is the intentional one (it is the documented
2026-04-25 fix); `Worker.balance_due` is the stale copy that was never updated.

**Fix.** Make the model match the service: exclude `'tip'` from `total_paid`,
and expose `tip_total` separately so the Workers list can show it as
informational gratis cash (exactly as the ledger page already does at
`worker_ledger.html:169`).

**Fix applied.** `Worker.total_paid` now counts `'payment'` rows only, and a new
`Worker.total_tips` exposes tips separately; the Workers list and the Advance
screen show tips as informational gratis cash instead of deducting them. The
snapshot side was also aligned on *earnings*: `_worker_payable_snapshot` now adds
unmigrated legacy `hdc_attendance` wages via `_worker_legacy_earned`, so both
formulas agree on earned **and** paid. Tests: `TestBalanceFormula`,
`TestLegacyEarnings`.

---

## 2. 🔴 Tips written from the Workers screen are not tagged with their expense id

Two screens can pay a worker. Only one of them has the fix.

**Accounts module — correct** (`hdc/services/accounts.py:2038-2064`):

```python
# Bug fix (2026-04-25): the tip ledger row used to be
# tagged only with TIP_WORKER_ID, but the periodic
# reconciler matches by TIP_EXPENSE_ID — so when the
# reconciler ran it could not see this row and wrote a
# second tip ledger entry for the same cash event,
# leaving the worker's balance over-paid by the tip.
... db.session.flush()
tip_note = tip_note_base + f' | TIP_EXPENSE_ID:{tip_exp.id}'
tip_exp.remarks = tip_note
_append_worker_cash_txn('tip', tip_part, tip_note)
```

**Workers module — still on the old tagging** (`hdc/routes/workers.py:392`):

```python
tip_remarks = (notes + ' | ' if notes else '') + \
    f'Tip for {w.name} via settlement overpayment | TIP_WORKER_ID:{wid}'
```

The reconciler's primary key is `TIP_EXPENSE_ID` (`hdc/services/timekeeping.py:113-134`);
the `TIP_WORKER_ID` tag is never read by it. These rows only survive because of a
belt-and-suspenders fallback that matches on **worker + date + amount**
(`hdc/services/timekeeping.py:137-160`).

**Impact.** Today it is masked, but the fallback is fragile in two ways:

* the ledger edit form lets you change a tip's **date, amount, project and
  stage** (`hdc/routes/workers.py:600-625`, and it does *not* update the linked
  expense) — one edit and the fallback stops matching, so the next ledger view
  writes a **second tip** for the same cash;
* two legitimate tips of the same amount on the same day are indistinguishable
  to the fallback, so the tag backfill can bind the wrong rows together.

**Fix applied.** The accounts implementation was ported into
`hdc_worker_payment`: it flushes the tip `Expense` first, then writes
`TIP_EXPENSE_ID:<id>` into **both** `Expense.remarks` and the
`LabourLedger.notes`. The duplicate guards were made remarks-independent (the tag
is per-expense, so comparing remarks could never match a repeat submission).
Tests: `TestTipTagging`.

---

## 3. 🔴 Voiding a tip does not void its expense — the tip comes back

`hdc_worker_ledger_void` (`hdc/routes/workers.py:651-672`) voids the ledger row
and the account transaction, but nothing voids the linked `Expense`:

```python
row.is_void = True
_accounts_set_void_by_source(f'labour_ledger_{row.entry_type}', row.id, True)
_accounts_set_void_by_source('worker_payment', row.id, True)
```

The reconciler only skips expenses that already have a **live** ledger row
(`hdc/services/timekeeping.py:136`: `LabourLedger.is_void == False`). So the next
time anyone opens that worker's ledger, `hdc_worker_ledger` runs
`_reconcile_worker_tip_ledger` (`hdc/routes/workers.py:172`) and **re-creates the
tip**. The void does not stick. `hdc_worker_ledger_restore` also refuses to
restore tips (`:677`), so there is no way back.

**Fix.** When a tip ledger row is voided, void the expense it points at (parse
`TIP_EXPENSE_ID` from the notes, fall back to the date/amount match), and make
the reconciler skip expenses whose matching ledger row was voided deliberately.

**Fix applied.** Three changes together make the void stick:

* `_worker_tip_expenses` filters out voided expenses, so a cancelled tip is never
  reconciled back into a ledger;
* voiding a tip/settlement row now voids its mirrored `Expense` (resolved by
  `_linked_expense_for_labour_ledger`, which prefers the `TIP_EXPENSE_ID` tag and
  falls back to delimited `TIP_WORKER_ID`/`SETTLE_WORKER_ID` matching so id `1`
  cannot match id `10`);
* restoring is now allowed for tips and settlements, and un-voids the expense
  with them.

Tests: `TestTipVoidCascade`, `TestSettlementCascade`.

---

## 4. 🔴 Deleting a payroll run leaves the cash posted in accounts

`hdc_payroll_delete` (`hdc/routes/payroll.py:407-436`) hard-deletes the payment
rows:

```python
for l in ledgers:
    db.session.delete(l)
    deleted_ledgers += 1
db.session.delete(run)
db.session.commit()
```

No `_accounts_set_void_by_source(...)` call, and `db.session.delete()` on the
ledger row does not cascade to `hdc_account_txn` (it is not a foreign key — it
is the `source_type`/`source_id` pair written at
`hdc/services/accounts.py:3018-3044`).

**Impact.** Deleting a payroll run restores every worker's payable to what it
was before, **while the money stays debited from the company cash account**. The
accounts ledger and the labour ledger permanently disagree, and the reconciliation
screen never notices (its check only runs one way: ledger → accounts,
`hdc/services/accounts.py:1310-1337`).

**Fix.** Void rather than delete: call
`_accounts_set_void_by_source('labour_ledger_payment', l.id, True)` before
deleting, or flip the rows to `is_void` and leave them.

**Fix applied.** `hdc_payroll_delete` voids the payment rows instead of
hard-deleting them, and calls `_accounts_set_void_by_source` for both
`labour_ledger_payment` and `worker_payment` so the unified-accounts mirror is
reversed in the same transaction. The audit trail survives and the cash returns
to the company account. Tests: `TestPayrollDelete`.

---

## 5. 🔴 Per-sqft workers are paid `rate × 0`

`_calc_time_wage` (`hdc/services/timekeeping.py:202-215`):

```python
if wtype == 'per_sqft':
    return max(0.0, base_rate * qty_sqft)
```

…and every code path that creates a time entry hard-codes `qty_sqft` to zero:

* bulk attendance sheet — `hdc/routes/timekeeping.py:116`
* single attendance form — `hdc/routes/timekeeping.py:299`
* editing an entry **clears** it — `hdc/routes/timekeeping.py:570` (`t.qty_sqft = 0.0`)

There is no input for quantity anywhere in the attendance UI.

**Impact.** A worker on `per_sqft` accrues a full attendance history, shows as
Present, contributes to project hours — and earns **0.00 PKR**. Nothing warns
you. Their balance correctly reads 0, so it looks like they were settled, not
like they were never paid.

**Fix.** Add a quantity field to the attendance screens (or, if per-sqft work is
actually measured elsewhere, e.g. stage progress, either wire it up or block
`per_sqft` workers from the daily attendance flow so the gap is visible).

**Fix applied.** Quantity is now captured end to end:

* `_parse_attendance_entries_payload` accepts `qty_sqft` per allocation row;
* the bulk sheet and the single-entry form pass it to `_calc_time_wage` and store
  it on the `TimeEntry`;
* the allocation modal shows a **Quantity (sq ft)** field for per-sqft workers
  only, with the worker's rate in the hint, and refuses to save `0`;
* the edit form exposes the field for per-sqft workers and — importantly — no
  longer wipes an existing quantity when only the hours change.

Historical rows that were paid 0 still need their quantity entered (the audit
reports them as `WAGE_PERSQFT_NO_QTY`). Tests: `TestPerSqftQuantity`,
`TestWageEngine`.

---

## 6. 🔴 `TimeEntry.attendance_id` means two different things

The legacy migration stores the **`Attendance`** id (`hdc/services/timekeeping.py:514`):

```python
te = TimeEntry(..., legacy_calc=True, attendance_id=a.id, ...)
```

`_recalculate_attendance_day` overwrites it with the **`AttendanceDay`** id
(`hdc/services/timekeeping.py:81`):

```python
te.attendance_id = day_row.id
```

Three consumers then assume it is always an `Attendance` id:

* `Worker.total_earned` (`hdc/models/workforce.py:44-53`)
* `Project.total_labour_cost` (`hdc/models/projects.py:118-129`)
* `Stage.stage_labour_cost` (`hdc/models/projects.py:250-258`)

```python
legacy = db.session.query(func.sum(Attendance.total_wage)) \
    .outerjoin(TimeEntry, TimeEntry.attendance_id == Attendance.id) \
    .filter(Attendance.project_id == self.id, TimeEntry.id.is_(None))
```

**Impact.** As soon as an `AttendanceDay.id` collides with any `Attendance.id`,
that legacy attendance row is treated as "already migrated" and its wage is
**dropped** from the worker's earnings and from project/stage labour cost — even
though no time entry exists for it. The wage disappears from every total
silently. The audit script reports this as `ATTENDANCE_ID_COLLISION`.

**Fix.** Give `AttendanceDay` its own foreign key column on `TimeEntry` (e.g.
`attendance_day_id`) and stop reusing `attendance_id`.

**Fix applied.** `hdc_time_entry` gained a dedicated `attendance_day_id` foreign
key; `_recalculate_attendance_day` writes the day link there and leaves
`attendance_id` alone, so the column once again means only "legacy
`hdc_attendance` row". `Worker.total_earned` also only treats **active** time
entries as migrated (see #12). The new column is added on boot by
`_ensure_timeentry_attendance_day_schema()`. Tests: `TestAttendanceDayLink`.

---

## 7. 🟠 Overlapping payroll runs let the same days be paid twice

Generating a run (`hdc/routes/payroll.py:141-183`) does not check whether a run
already covers those dates. Each run independently sums the gross wage for its
own window and 'Pay All' pays against a note string unique to *that run*
(`hdc/routes/payroll.py:99, 415`: `f'Payroll run #{run.id} '`).

**Impact.** Two runs covering 1–10 Aug and 6–15 Aug each show the full wage for
6–10 Aug as payable. Paying both pays those five days twice. The worker's ledger
happily accepts it; `payable` clamps to 0, so the over-payment is invisible
until you read the ledger (see #1).

**Fix.** Block generation when the range overlaps an existing run, and compute
`paid` per run as *period* payments rather than by note-string matching.

**Fix applied.** Generation now normalises a reversed range and refuses any range
that overlaps an existing run, redirecting to the clashing run with an
explanation. Disjoint ranges still generate normally. Tests:
`TestPayrollOverlap`.

---

## 8. 🟠 Payroll silently discards advances it cannot deduct

`hdc/routes/payroll.py:173, 330, 520`:

```python
net     = max(0.0, info['gross'] - advances)
payable = max(0.0, gross - advances)
```

If a worker's advances in the period exceed that period's gross wage, the excess
is clamped away.

**Impact.** The salary card shows "Advance deducted: 300, Net pay: 0" and the
remaining 300 PKR of advance is simply gone from the payroll view, while the
worker ledger still carries it. **Payroll totals can therefore never be
reconciled against worker balances** for anyone who was advanced ahead of their
earnings — which is exactly the common case at the start of a project. Payroll
also only ever looks at advances *inside* the run window, whereas the worker
balance is all-time (`hdc/services/ledger.py:242-281`), so the two views differ
by design.

**Fix.** Carry the unrecovered advance forward explicitly (show it as "advance
carried forward") instead of clamping, and state on the card that payroll is
period-scoped while the ledger is all-time.

**Fix applied.** The clamp is still there (net pay cannot go negative) but the
unrecovered amount is now computed as `advance_carried` and surfaced on both the
run summary and the printed salary card as "+N carried fwd", with the totals row
summing it. The card makes it explicit that payroll is period-scoped while the
worker ledger is all-time. Tests: `TestAdvanceCarriedForward`.

---

## 9. 🟠 Overtime is unpaid for hourly workers; there is no OT premium anywhere

`hdc/services/timekeeping.py:202-215`:

```python
if wtype == 'hourly':
    return max(0.0, base_rate * hours)      # `overtime` is ignored entirely
...
return max(0.0, base_rate * full_day + (overtime or 0) * hourly)   # hourly = rate/8
```

**Impact.** Hourly workers who work overtime are paid for the regular part only.
Daily workers *are* paid for overtime, but at `rate ÷ 8` — i.e. **straight
time**, with no premium. Both may well be deliberate local practice; flagging it
because nothing in the UI says so, and a 1.0× "overtime" rate is unusual enough
to be worth confirming.

**Fix applied — and confirmed as intended practice.** Overtime is paid at
**straight time** for both wage types; the bug was that hourly workers lost their
overtime entirely. `_calc_time_wage` now returns `rate × (hours + overtime)` for
hourly workers. No OT premium was introduced, per confirmation that straight time
is correct. Tests: `TestWageEngine`.

---

## 10. 🟠 The ledger running balance and the payable figure use different sources of earnings

* Running balance column (`hdc/routes/workers.py:196-207`) sums `entry_type='work'`
  rows from `hdc_labour_ledger`.
* Payable / KPIs (`hdc/services/ledger.py:242-281`) sum `TimeEntry.wage_calculated`.

The `work` rows are maintained separately by `_sync_work_ledger_for_time_entry`
and a repair pass (`hdc/services/timekeeping.py:219-291`) that only runs when a
worker's ledger page is opened.

**Impact.** Any orphaned or duplicated `work` row makes the running-balance
column wrong while the headline "Balance" KPI stays right (or vice versa), and
the drift is invisible until someone adds the column up. The audit script
reports this as `BALANCE_WORK_LEDGER_DRIFT`.

**Fix applied.** The ledger's running balance now reads each `work` row's wage
from the `TimeEntry` it mirrors (and contributes 0 when that entry is missing or
voided), so the column is driven by the same source of truth as the payable KPI.
Orphaned and duplicated `work` rows can no longer move the balance. Test:
`TestRunningBalance`.

---

## 11. 🟡 Day recalculation overwrites real clock-in/out times

`hdc/services/timekeeping.py:80-87`:

```python
te.check_in  = base_dt + timedelta(hours=total_hours)
te.check_out = te.check_in + timedelta(hours=hours)
```

Every entry is rewritten onto a synthetic midnight-based timeline. Called from
bulk save, single save, edit, void and reactivate
(`hdc/routes/timekeeping.py:170, 311, 571, 612, 650`).

**Impact.** Actual check-in/check-out times are not preserved — the first entry
of a day is always stamped 00:00. This also means the unique index on
`(worker_id, check_in)` is defending synthetic values, and the OT boundary
(regular vs overtime) is decided by row order rather than by the clock.

**Deferred — deliberate behaviour.** The synthetic timeline is how the app keeps
multi-site allocations on one day from colliding on the
`(worker_id, check_in)` unique index, and the regular/overtime split is defined
by cumulative hours rather than by the clock. Changing it would alter the
meaning of existing data, so it is left as-is and documented here. If real
clock-in times are ever captured (e.g. from a biometric device), they should go
in new columns rather than replacing these.

---

## 12. 🟡 A voided migrated entry makes a legacy wage vanish from both totals

`Worker.total_earned` (`hdc/models/workforce.py:44-53`) builds
`migrated_attendance_ids` from **all** time entries, including voided ones:

```python
migrated_attendance_ids = {
    int(t.attendance_id) for t in self.time_entries
    if getattr(t, 'attendance_id', None)      # ← no `if not t.is_void`
}
legacy_total = sum(a.total_wage for a in self.attendance
                   if a.id not in migrated_attendance_ids)
```

If the time entry created from an old attendance row is later voided (the bulk
sheet voids everything first, `hdc/routes/timekeeping.py:88-95`), the attendance
row is excluded from `legacy_total` **and** the voided entry is excluded from
`time_total`. The wage is counted nowhere. Reported as
`ATTENDANCE_WAGE_LOST_VOID`.

**Fix applied.** `Worker.total_earned` builds `migrated_attendance_ids` from
**active** time entries only, so a voided migration row no longer hides the
legacy wage; `_worker_payable_snapshot` counts the same legacy wages through
`_worker_legacy_earned`. Both totals now agree, and a voided migration cannot
make a wage vanish. Tests: `TestLegacyEarnings`.

---

## 13. 🟡 Editing a tip ledger row can desynchronise it from its expense

`hdc_worker_ledger_edit` (`hdc/routes/workers.py:600-625`) rewrites amount, date,
project and stage and calls `_accounts_upsert_labour_ledger_txn` — but never
touches the linked `Expense`. Combined with the date/amount-based fallback in
#2, an edit is enough to make the reconciler write a duplicate tip.

**Fix applied.** The edit handler resolves the mirrored expense *before* mutating
the row, then syncs the expense's amount (negative for settlements), date,
project and stage. It also refuses to let an edit drop the `TIP_EXPENSE_ID` tag —
user notes are preserved and the tag is re-appended. Tests: `TestTipEditSync`.

---

## 14. 🟡 The duplicate guard is a 12-second window, not an idempotency key

`_has_recent_duplicate` (`hdc/services/timekeeping.py:97-108`) blocks a repeat
submission only if the identical row was created within **12 seconds**.

**Impact.** A deliberate second identical payment a minute later is allowed
through; a slow form (or a slow phone on site, which is the normal case) fails
to be protected. Tips, advances and payments all rely on it.

**Deferred.** A real idempotency key needs a client-supplied token persisted per
submission, which is a change to every money form in the app rather than to the
labour path alone. The labour flows are now much less exposed to it: tips are
identified by expense id rather than by date+amount, so a repeat submission
creates a distinguishable event instead of an invisible duplicate. Worth doing
app-wide as its own piece of work.

---

## 15. 🔵 `Worker.total_*` can raise `TypeError` on `NULL` amounts

`hdc/models/workforce.py:57, 61, 66` do `sum(e.amount for e in ...)` without the
`or 0.0` guard used elsewhere, and `Attendance.total_wage` is summed bare at
`:51`. Any row with a `NULL` amount (possible for rows written before the
column default existed, or by direct SQL) takes down the Workers list page.

**Fix applied.** All of the worker model's aggregate properties now sum
`(x or 0.0)`, so a `NULL` amount can no longer raise `TypeError` and take down
the Workers list.

---

## 16. 🟠 Historical wages are not frozen — a rate gap lets one edit rewrite them

Found while running the audit against live data, not in the original code trace.

`_worker_rate_on` (`hdc/services/timekeeping.py:163-181`) resolves the rate for a
work date by taking the latest `WorkerRate` row with
`effective_from <= work_date`, and **falls back to the worker's current profile**
when none matches:

```python
if on_date:
    wr = (WorkerRate.query
          .filter(WorkerRate.worker_id == worker.id,
                  WorkerRate.effective_from <= on_date)   # ← nothing matches
          ...
# Fallback to current profile values
return 'daily', float(worker.base_daily_wage or 0.0)
```

`hdc_worker_rate` sets `effective_from` to the day the change is *entered*
(`hdc/routes/workers.py`, `_parse_date(request.form.get('effective_from'))`
defaulting to today), so any day worked **before the first rate change** has no
covering row.

**Why it matters.** Those historical wages are correct for their date but cannot
be reproduced from the rate card. Every path that recalculates a day —
`hdc_edit_attendance`, `hdc_delete_attendance`, `hdc_reactivate_attendance` and
the bulk sheet — calls `_recalculate_attendance_day`, which recomputes
`wage_calculated` from scratch. Touching one of those days **silently rewrites
the wage at today's rate**.

In the live database this affects **17 time entries worth 886,952 PKR**, the
largest being a single 454,190 PKR entry that would recalculate to 65,000 PKR.

**Not fixed here** — it needs a product decision, because there are two
defensible answers:

1. **Freeze the wage**: store the rate and wage type used on the `TimeEntry` at
   save time and never recompute historical days (only recompute when the user
   explicitly asks). Safest, and it makes the ledger reproducible.
2. **Fix the rate card**: backdate `WorkerRate` rows so every period is covered,
   and make `effective_from` a required, validated field on the rate screen
   rather than defaulting to today.

Until one is chosen, the audit reports these as `WAGE_RATE_CARD_GAP` (kept
separate from `WAGE_MISMATCH`, which means a rate row *does* cover the date and
the stored wage is genuinely wrong).

---

## Running the audit on your live database

```bash
# report to the terminal
python3 scripts/labour_audit.py --db hdc_instance/hdc_erp.db

# machine-readable, for working through the findings
python3 scripts/labour_audit.py --db hdc_instance/hdc_erp.db \
        --json audit.json --csv findings.csv

# validate the checks themselves (builds a DB seeded with every bug)
python3 scripts/make_labour_audit_fixture.py --db /tmp/hdc_fixture.db
python3 scripts/labour_audit.py --db /tmp/hdc_fixture.db

# the regression tests that pin every fix in this report
python -m unittest tests.test_labour_audit_fixes -v
```

Checks performed (`--json` includes the check id on every finding):

| Area | Check ids |
|---|---|
| Wages | `WAGE_MISMATCH`, `WAGE_RATE_CARD_GAP`, `WAGE_ZERO_WITH_HOURS`, `WAGE_PERSQFT_NO_QTY`, `WAGE_ORPHAN_WORKER` |
| Balances | `BALANCE_TWO_FORMULAS`, `BALANCE_OVERPAID`, `BALANCE_WORK_LEDGER_DRIFT`, `ADVANCE_EXCEEDS_EARNINGS` |
| Tips | `TIP_DUPLICATE`, `TIP_RESURRECTS`, `TIP_EXPENSE_WITHOUT_LEDGER`, `TIP_LEDGER_WITHOUT_EXPENSE`, `TIP_UNTAGGED`, `TIP_AMOUNT_MISMATCH`, `TIP_LEDGER_VOID_EXPENSE`, `TIP_NO_WORKER`, `TIPS_INFORMATIONAL` |
| Settlements | `SETTLEMENT_NO_EXPENSE`, `SETTLEMENT_EXPENSE_NO_LEDGER`, `SETTLEMENT_VOID_LEDGER_LIVE_EXPENSE`, `SETTLEMENT_POSTED_AS_CASH` |
| Accounts | `ACCOUNTS_MISSING`, `ACCOUNTS_DOUBLE_POSTED`, `ACCOUNTS_AMOUNT_MISMATCH`, `ACCOUNTS_ORPHAN_TXN`, `ACCOUNTS_VOID_NOT_PROPAGATED`, `ACCOUNTS_TXN_ON_VOIDED_ROW` |
| Payroll | `PAYROLL_RUN_OVERLAP`, `PAYROLL_OVERPAID_RUN`, `PAYROLL_ADVANCE_CLAMPED`, `PAYROLL_NET_MISMATCH`, `PAYROLL_TOTAL_MISMATCH` |
| Timekeeping | `ATTENDANCE_ID_COLLISION`, `ATTENDANCE_WAGE_LOST_VOID`, `TIMEENTRY_VOID_LIVE_WORK`, `TIMEENTRY_DUPLICATE`, `TIMEENTRY_OVER_24H`, `TIMEENTRY_DANGLING_ATTENDANCE_ID`, `ATTENDANCE_DAY_DRIFT` |

The script re-implements `_calc_time_wage` and `_worker_rate_on` exactly as the
app does, so `WAGE_MISMATCH` is a true recomputation rather than a guess.
