# Multiple subcontractors per stage

## Supported scope
A stage can have several subcontractor records. Each record still has one current
stage and its own existing pricing, retention, progress, attendance and payments.
This is intentionally not a many-to-many contract redesign: sharing one record
across simultaneous stages would mix its existing record-level money totals.

## Compatibility
No schema migration is required. `Subcontractor.stage_id` is membership;
`Stage.assigned_subcontractor_id` is retained as a legacy primary pointer.
`Stage.assigned_subcontractors` provides the full active membership, including a
legacy pointer pending reconciliation. Admin reconciliation preserves secondary
members. No financial rows or historical events are migrated or deleted.

## Workflow
**Project page:** every stage row has a **Subcontractors (N)** button in the
*Exec* column. It opens the stage's panel, which shows:
- a table of all assigned subcontractors with their type, rate, sqft, lump sum,
  retention, contract value, amount paid and completion %. From here you can save
  completion %, edit that member's terms (pencil) or remove just that member;
- **Add subcontractors to this stage**: a searchable checklist. Tick one or more,
  optionally fill in terms, and click *Add selected subcontractors*. Terms apply to
  each ticked subcontractor, and blank fields keep that subcontractor's own values.
  Existing members are never replaced.

**Add Stage / Edit Stage form:** the *Subcontractors* card has the same checklist,
so you can assign several subcontractors when creating a stage. On edit, you can add
more. Its terms fields are named `sub_*` so they don't clash with the owner's
`rate_per_sqft`.

Rules:
- A subcontractor already on another stage is shown greyed out ("On: <stage>")
  and can't be ticked. If one is posted anyway, it is skipped with a warning while
  the others are still added.
- Invalid terms (negative, NaN, retention > 100) reject the whole batch before
  anything changes.
- Completed stages must be reopened before members are added or terms changed.
- Completion needs every member at 100%, or the existing force-completion action
  (bolt icon) updates and audits every member.
- "Shift to Company" (building icon, shown only when members exist) removes all
  members after confirmation. Removing a member keeps its financial history.
- All assignment paths use `hdc.services.subcontract.assign_subcontractors_from_form`.

## Remaining sqft logic (new)
If a stage has `qty_sqft` > 0 (e.g. 2000 sqft), the system now tracks how much
sqft is already taken by sqft-type subcontractors.

- `Stage.subcontracted_sqft` = sum of `total_sqft` for all assigned subcontractors
  where `contract_type == 'sqft'`.
- `Stage.remaining_sqft` = `qty_sqft - subcontracted_sqft` (floored at 0, None if no cap).
- `Stage.remaining_sqft_excluding(sub_id)` = remaining if that member were excluded,
  used when editing a member's own terms.

Assignment guard:
- When assigning subcontractors, if `contract_type` is `sqft` and the requested
  `total_sqft` exceeds remaining, that pick is skipped with a clear message:
  `"Sub X needs 1000 sqft but only 900 sqft remains in stage Y (total 2000, allocated 1100). Only 900 sqft is shown as available to others."`
- If `total_sqft` is left blank for a sqft-type sub, the system auto-assigns the
  remaining sqft (e.g. stage 2000, 1st took 100, 2nd blank → gets 1900). If no
  remaining, the pick is blocked.
- Batch assignment is sequential: remaining is recalculated after each successful
  pick in the same request, so 2nd/3rd/4th only see what's left.
- Editing a member's sqft checks against remaining excluding itself. Example:
  stage 2000, allocated 1100 (100+1000), remaining 900. Editing the 100-sqft member
  to 1500 fails because max allowed is 1000 (remaining 900 + its own 100).
- Reducing a stage's `qty_sqft` below already allocated sqft is blocked in the
  edit form with an error message.
- Lump-sum subcontractors do not consume sqft and are not limited by this cap.

UI:
- Project page stage panel shows one compact allocation strip:
  `STAGE 2,000 sqft · ALLOCATED 1,100 sqft · REMAINING 900 sqft` plus a fill bar
  and a badge (`900 sqft free` / `No sqft left`). The same strip is repeated next
  to the add-subcontractor terms so the free sqft is visible where it is used.
- The Subcontractors and Drawings panels are rendered **under** the stages table
  (`.stage-panel-wrap`), not inside it. An 18-column table is horizontally
  scrollable, so rows inside it used to stretch past the card edge and clip the
  terms fields; the panels now share the card width.
- Terms fields (contract type, rate, sqft, lump sum, retention) use one
  `row g-2` grid with equal `col-lg` widths and a trailing button, so everything
  in a row lines up on its own label + input pair and never wraps mid-field.
  The sqft input keeps `max` = remaining and the placeholder shows the remaining
  sqft; client-side JS blocks submitting more than remaining.
- Member terms editor (the pen button) uses the same grid and states the maximum
  that member can be set to (`remaining excluding member + its own sqft`).
- Add/Edit Stage form: "currently assigned" subcontractors render as chips and
  the allocation strip summarises total/allocated/remaining.
- On narrow screens (< lg) the terms grid becomes two columns and the member
  table scrolls inside its own `.table-responsive` instead of squashing
  headers letter by letter.

This fixes the missed approach from the last PR where 1st contractor could take
100 sqft but 2nd/3rd/4th still saw full 2000 sqft as available.

## Deployment
Back up the database and deploy all changed modules/templates together. Do not run
old and new application versions concurrently: the old reconciliation logic can
clear secondary assignments. Restart all application workers. A rollback to old
code requires reviewing stages with multiple members first.

## Verification
- `python -m unittest discover -s tests -p test_multi_subcontractors.py`
- `python -m unittest discover -s tests -p test_subcontract_team_attendance.py`
- Manual: create stage 2000 sqft, assign 1st sub 100 sqft, verify 2nd sees 1900 remaining,
  try assign 2000 to 2nd (should fail), assign 1900 (should succeed), try 3rd (should fail),
  blank assign (should auto-fill remaining), edit 1st to 1500 (should fail if would exceed).
- `python scripts/qa_browser.py --output qa/browser.json` in a real browser asserts
  the stage panel is not inside the scrolling stages table, its five term fields
  share one row, the fields never cross the card edge and the page has no
  horizontal overflow.

These are targeted regression tests, not a full production acceptance audit.
