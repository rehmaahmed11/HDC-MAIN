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

## Deployment
Back up the database and deploy all changed modules/templates together. Do not run
old and new application versions concurrently: the old reconciliation logic can
clear secondary assignments. Restart all application workers. A rollback to old
code requires reviewing stages with multiple members first.

## Verification
- `python -m unittest discover -s tests -p test_multi_subcontractors.py`
- `python -m unittest discover -s tests -p test_subcontract_team_attendance.py`

These are targeted regression tests, not a full production acceptance audit.
