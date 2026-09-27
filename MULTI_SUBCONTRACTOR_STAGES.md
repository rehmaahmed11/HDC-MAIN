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
On project detail, expand the stage assignment form and add each subcontractor
with their own terms. Existing assignments remain. Re-selecting a member updates
that member's terms. Expand completion controls to update or remove an individual
member. Returning to company execution removes all members after confirmation.
Removing an assignment retains financial history and existing project scope.
An already-assigned record cannot silently move to another stage. Completed stages
must be reopened before adding members. Completion requires every member at 100%,
or the existing explicit force-completion action updates and audits every member.

## Deployment
Back up the database and deploy all changed modules/templates together. Do not run
old and new application versions concurrently: the old reconciliation logic can
clear secondary assignments. Restart all application workers. A rollback to old
code requires reviewing stages with multiple members first.

## Verification
- `python -m unittest discover -s tests -p test_multi_subcontractors.py`
- `python -m unittest discover -s tests -p test_subcontract_team_attendance.py`

These are targeted regression tests, not a full production acceptance audit.
