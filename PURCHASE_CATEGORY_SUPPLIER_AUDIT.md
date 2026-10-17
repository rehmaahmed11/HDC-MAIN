# Purchase item types + supplier ⇄ material / site — audit and fixes

Audit of the work merged in **PR #127** ("Purchase: item types (categories) +
supplier ⇄ material/site tracking"), which is the change that put the
**categories** (item types) and the **supplier ⇄ material / site tracking**
into the Purchase section.

The feature itself is sound: one vocabulary, one shared rule, the same
filter everywhere. This audit read every route, service, template and the
JSON API behind it, and found **eleven defects** — seven of them visible to
the operator as a wrong number or a page that lies about what it is showing.
All eleven are fixed and pinned by tests.

| # | Severity | Where | What was wrong |
|---|----------|-------|----------------|
| 1 | High | `hdc/services/material_categories.py` | The categories page reported **0 sites and 0 suppliers for every item type** as soon as one item anywhere was uncategorised. |
| 2 | High | `hdc/routes/purchase_v2.py` | The **"Uncategorized" filter on the Suppliers page was dead** — it always answered "no supplier". |
| 3 | High | `hdc/services/purchase.py` | The **"Uncategorized" filter on Suppliers-by-site showed every row** while the dropdown claimed it was filtered. |
| 4 | High | `hdc/services/material_categories.py` | Typing the name of a **deleted** item type crashed the categories page with a database error (HTTP 500). |
| 5 | Medium | `hdc/routes/purchase_v2.py` | The Stock page's **"Sent of Purchased" ignored the category filter** — the numerator was narrowed, the denominator was not. |
| 6 | Medium | `hdc/routes/purchase_v2.py` | The item-wise register's **"In Store" collapsed to zero under a date filter**, and disagreed with the item ledger for the same item. |
| 7 | Medium | `hdc/routes/purchase_v2.py` | The **search box on Suppliers-by-site did not narrow the assigned-only rows** — a search that matched nothing still listed every assigned supplier. |
| 8 | Medium | `hdc/routes/purchase_v2.py` | The **Supplier page showed the item's item type**, not the purchase line's — the same purchase row read as two different item types on two pages. |
| 9 | Medium | `templates/…/purchase_v2_delivered.html` | The **Delivery register had no Item Category column**, although the spec says it has one. |
| 10 | Medium | `hdc/routes/purchase_v2.py`, `hdc/routes/api_purchase.py` | **Editing a record whose item type had been retired silently re-filed it** under a different type; the JSON API also wiped the type on any update that omitted it. |
| 11 | Low | `hdc/routes/purchase_v2.py` | The categories page **counted the "Uncategorized" bucket as an item type** in its "N categories" figure. |

## The rule the whole section runs on

> A purchase line's item type is the line's own if it was set, otherwise the
> item's. Delivery and usage resolve through their purchase order, falling
> back to the item when no order is linked.

This is implemented once, in `hdc/services/material_categories.py`
(`_purchase_category_condition`, `_delivery_category_condition`,
`_usage_category_condition`), and every register filters through those
helpers. Findings 2, 3, 8 and 10 were all the same class of defect: a page
that read the category by a **different** path than the shared rule.

A second rule now sits next to it, in the same module:

> A **retired** item type (set inactive because records still point at it)
> may be **kept**, never newly chosen. A **deleted** one is never allowed.

`_material_category_for_write` states it once; the purchase form, the item
form and the JSON API all call it.

## What each fix changed

### 1 — the categories page reported no sites and no suppliers

`_material_category_summary_rows` wrote `site_count` / `supplier_count` in a
loop that sat **after** an early `return` for the Uncategorized bucket. The
loop therefore only ran when nothing at all was uncategorised. With one
uncategorised item in the shop, every real item type on
`/hdc/purchase-v2/categories` read "0 sites · 0 suppliers" — the control page
the operator uses to see where a type went, answering nothing.

The counts are now written for every row before the bucket is appended.

### 2 — the Suppliers page could not find the uncategorised rows

The filter asked whether the bucket id `0` was in the supplier's list of
item types. That list only ever holds *real* categories, so the answer was
always "no" and the bucket was empty. `_supplier_supplied_profile` now
carries an `uncategorized_count`, and the filter matches on it. The supplier
row also says "Uncategorized" in its **Supplied Categories** column, so a
supplier that only ever supplied legacy rows no longer reads as having
supplied nothing.

### 3 — the Suppliers-by-site page ignored the bucket

The filter was `if category_id and …`. The bucket id is `0`, and `0` is
falsy, so "Uncategorized" was read as "no filter" and the page returned every
row under a dropdown that said it was filtered. The test is now
`is not None`, and `0` is matched against the resolved category properly.

### 4 — a deleted item type's name could not be typed again

The unique index only covers live rows (`WHERE is_void = 0`), so a deleted
"Tile" and a live "tile" can sit side by side. The name lookup had no
ordering, so it could pick the deleted row, revive it, and leave two live
rows with the same name — which the index then refused, with a 500 on the
categories page. The lookup now prefers a live row, and `_ensure_material_category`
reuses it instead of reviving anything.

### 5 — the Stock page compared two different sets of items

"Sent of Purchased" took the *sent* figure from the material summary (which
the category filter narrows) and the *purchased* figure from a query that
ignored the filter. With Steel on screen the page answered "65.4%" (850 of
1300 — the whole book) instead of "60%" (600 of 1000 — Steel). The
denominator is now taken over the same items the page is showing.

### 6 — "In Store" is a balance, not a flow

The item-wise register computed `In Store` as *date-filtered purchases* minus
*all-time deliveries*, so a date filter narrowed one side of a subtraction
and the store looked empty; the item ledger for the same item showed the
real figure. The register now reads the full history on both sides, exactly
like the ledger, and the two columns say so in their tooltips.

### 7 — the search box did not narrow the whole page

On Suppliers-by-site, the search was applied to the delivered rows only; the
assigned-only suppliers were appended afterwards, so a search that matched
nothing still listed every assigned supplier. The search now covers the
assignments too, in both directions of the page.

### 8 — the same purchase showed two different item types

The Supplier page's **Item Category** column read the *item's* category,
while the Purchases register read the *line's*. A line the operator had
filed under another type read as one thing on the register and another on the
supplier page. The supplier page now uses the shared rule, and adds the
item's own type underneath when the two differ, so an override stays visible.

### 9 — the Delivery register had no Item Category column

The spec promised one. It is now there, using the same expression as the
other registers.

### 10 — a retired item type did not survive an edit

A category that is set inactive still sits on the records that use it, but
the edit dropdowns only listed **active** types. The form could not show the
record's own type, so `setSelectVal` found no match, left the first option
("Item's category") selected, and saving an unrelated field — a quantity, a
date — silently re-filed the record under its item's type. The retired types
are now listed in the edit dropdowns (marked "retired"), and the server
enforces the keep/choose rule. The JSON API had the matching bug in a
different shape: a `PUT` that omitted `category_id` wiped the item's type,
while every other omitted field kept its value. It now behaves like the rest
of the handler.

### 11 — the "N categories" figure counted the bucket

"Uncategorized" points at missing data; it is not an item type, and the
categories page's own figure now leaves it out.

## Also checked and found sound

* The **seed** runs once and never overwrites a rename.
* A category in use is **deactivated, never deleted**; an unused one is
  voided.
* The **Pending Purchases pop-up** filter (client side) handles the bucket
  correctly — it compares the id as a string, so `0` is a real value there.
* Every write path (purchase create/edit, item create/edit, assign, sync,
  supplier links) is behind the money-write guard, and the new read-only
  pages sit under the existing `/hdc/` read rule.
* The **project page** card reads the same helpers as the by-site page:
  suppliers that delivered are listed as *Supplied*, suppliers that are only
  linked as *Assigned only*.
* The **admin wipe targets** and the **record-permission labels** carry the
  three new tables.

## Tests

`tests/test_purchase_categories_audit_fixes.py` — 19 cases, one per finding
plus the controls that must keep holding. They are written as behaviour
("the page must show 2 sites for Cement", "a search that matches nothing
must show nothing"), so they cannot pass by accident.

Against the code as it was before this audit, **11 of the 19 fail**; the
other 8 are the controls that were already right and must stay right.

```
python -m unittest discover -s tests -p 'test_purchase_categories_audit*.py'
```

## Full verification

* `python -m unittest discover -s tests -p 'test_*.py'` — **1040 tests, OK**
  (6 skipped).
* `tests/smoke_worker.py` — 98 reads, 16 writes, **0 errors**.
* `scripts/audit_acceptance.py` — **VERDICT: PASS**.
* `scripts/qa_inventory.py` — database integrity / foreign-key checks clean.
* `scripts/check_layers.py`, `scripts/check_db_safety.py`,
  `scripts/reorganize_frontend.py --check`, `scripts/qa_responsive.py`
  (0 findings), `node --check` over every static JS file — all clean.
