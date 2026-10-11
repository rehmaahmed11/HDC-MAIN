# Rental Edit Form = New Rental Form

**Problem.** When a new rental was made in HDC Tools and the operator went back
to change it, the editing surface lacked most of the fields the *New
Transaction* (New Rental) form had: destination details, rent type, dates,
notes and the tool lines themselves could not be corrected in one place.

**Fix.** The edit form now *is* the new-rental form. `GET
/hdc/tool-rental/<id>/edit` renders the same template that creates rentals
(`tool_rental/tool_new_rental.html`) in **edit mode** — every field the
operator filled in to make the rental is there again, prefilled with the saved
values — and `POST /hdc/tool-rental/<id>/update` saves it. One template, one
set of fields: the two forms can never drift apart again.

## What the edit form offers

* Renter type (our site ↔ outside customer), site/project + stage or customer
  name/phone/address — all searchable combos, prefilled.
* Tool lines: one prefilled row per saved line (tool, quantity, rent rate) with
  its out-of-store Tool No / serial markings listed on the row, plus "Add
  another tool" for fresh lines.
* Rent type (one fee / per day / per hour / no charge), rental date, expected
  return date, notes.
* Submit = **Save Changes**; cancel returns to the rental detail page.

Entry points: an **Edit** button on the rental detail page, an edit icon on
each open rental in the Rentals hub, and on the recent-rentals rows below the
New Rental form. Closed/returned and voided rentals are not editable.

## Ledger safety rules

Saving reconciles the tool lines while keeping `owned = in store + out` true:

* **Resized line** — pieces move with it. Reducing a line brings the freed
  serial markings back to the store; increasing takes more from the store
  (never beyond what is in store plus what this rental already has out).
* **Removed line** — its pieces come back to the store.
* **Added line** — goes out exactly like a fresh create.
* **Destination changed** — the pieces still out are relabelled to the new
  site/customer.
* Every change leaves a movement log (`Rental … edited: …`) and the serial
  chain, so Tracking and the Audit pages can still account for every piece.

History locks the dangerous parts, with a visible notice on the form:

* Once a **return or transfer** is recorded, the tool lines are locked — the
  header details still save, and the message points to the Return / Transfer
  forms for moving tools.
* Once **money has moved** (a payment or a discount), the rent type can no
  longer change.

## Tests

`tests/test_tool_rental_edit.py` pins the whole contract: field parity between
the create and edit forms, prefilling, header updates, quantity
increase/decrease with serial reconciliation, line add/remove, stock-integrity
refusal, the return/billing locks, and the closed-rental guard — each save
asserted against a balanced tools ledger.
