# Parties — one form for Add and Edit; tidy row actions

**Problem.** PR #129 added editing to the Parties directory, but its window had
drifted from the form that *creates* a party: the Add card offered a searchable
name combo and the explanatory type hint, while Edit rendered a bare text input
with no hints and different spacing. The new edit control was also dropped into
the row as a loose inline button next to the existing ledger link and hide/show
button, so the action cell had uneven gaps and pushed the table wider. It read
as a page whose CSS had come apart.

**Fix — one form, both uses.** The fields now live in a single macro,
`templates/hdc/parties/_party_form_fields.html`, included by the Add Party card
and by the Edit Party window. Both render, in the same order and from the same
source:

* Name — the same searchable combo, prefilled with the saved name;
* Type — the same grouped select (Loan / External Customers / Workers /
  Clients / Suppliers / Other) and the same explanation of what the type does;
* Phone and Note — the same optional badges, limits and placeholders.

The edit window is opened from a row, its controls are filled from that row, and
**Save Changes** posts through the existing `edit_party` action. A field added to
the shared macro appears in both windows the next time either is rendered, so
the two can never drift apart again.

The edit window's footer buttons now use the same `btn-sm` scale as the rest of
the page instead of the default-size pair that sat off the form's rhythm.

**Fix — row actions are one component.** `.hdc-row-actions` (in `static/hdc/css/hdc.css`)
is a right-aligned flex cluster with one gap and one baseline, and it sizes a
POST button's `<form>` as a flex item too. The Parties row now renders all three
controls (open ledger, edit, hide/show) as equal icon buttons inside it, under
an **Actions** column heading rather than the old *Open*. The buttons no longer
each keep their own line box, and the cell is narrower than it was after #129
because the labels live in tooltips / `aria-label`s (the party name itself
already opens the ledger).

**Cache-busting.** `hdc.css` and `accounts.css` are now stamped with
`?v={{ hdc_asset_stamp }}` like `responsive.css`, so a deploy that adds the new
action component actually reaches a browser that cached the old stylesheet.

**What did not change.** The hide/show action is still a one-click row button;
status is not duplicated inside the edit window. All existing `toggle_party`
behaviour and tests remain.

## Tests

`tests/test_parties_module.py` — 18 tests, all passing. New coverage:

* the rendered Add and Edit forms expose the **same field names in the same
  order** and the same explanatory hints (a hand-edited window fails);
* every row's edit button carries the saved name, type, phone and note for its
  party, so opening the window can prefill all four.

```bash
.venv/bin/python -m unittest tests.test_parties_module
```
