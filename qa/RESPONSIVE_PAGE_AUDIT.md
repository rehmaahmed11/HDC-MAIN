# Responsive audit — every page at PC, tablet and phone widths

Two browser gates drive the real application in headless Chromium and report
what an operator can actually reach:

* `scripts/qa_responsive_pages.py` renders **every GET page** at **every**
  viewport below and fails on content that escapes the screen, is cropped,
  keeps a table wider than the screen, loses a record-card label, or leaves a
  thumb target under 32px.
* `scripts/qa_responsive_functional.py` exercises the shell itself at
  320 / 390 / 768 / 1440px: drawer, Logout, sticky top bar, modal fit and
  pinned footer, iOS-zoom input sizes, stacked record cards, sideways scroll.

| viewport | device | width |
| --- | --- | --- |
| `mob-320` | phone | 320 px |
| `mob-360` | phone | 360 px |
| `mob-375` | phone | 375 px |
| `mob-390` | phone | 390 px |
| `mob-414` | phone | 414 px |
| `mob-430` | phone | 430 px |
| `tab-768` | tablet | 768 px |
| `tab-834` | tablet | 834 px |
| `tab-1024` | tablet | 1024 px |
| `pc-1280` | desktop | 1280 px |
| `pc-1440` | desktop | 1440 px |
| `pc-1920` | desktop | 1920 px |

## Result

| run | renders | pages | failing renders | pages with a defect |
| --- | --- | --- | --- | --- |
| before the fixes | 1488 | 124 | 245 | 87 |
| **after the fixes** | **1488** | **124** | **0** | **0** |

* **37 of 124 pages were clean at all twelve widths before any change.**
* 87 pages had at least one defect; **all of them are fixed** (245 failing renders → 0).
* Interaction pass: **42/42 checks pass** — no failures.

Breakdown of the 245 failing renders: **185** content-out (past the screen
edge), **81** small-targets (a thumb target under 32px), **2** clipped.  Two
routes are documents rather than screens and are excluded from layout
measurement: `/hdc/stage/drawing/1/view` (a streamed PDF) and
`/hdc/accounts/new-transaction/subcategories` (a JSON feed).

## What was changed

| change | why |
| --- | --- |
| `static/hdc/css/responsive.css` — `.row` gutter + `#flashMessages` padding reset | Bootstrap pulls every `.row` 12px sideways while the phone layer shrinks the page padding to 8–10px, so **80 of 124 pages** hung 2–6px off the right edge at 320px (21 at 390px). Removing the negative margin keeps every column inside the screen. |
| `responsive.css` — drawer: `#sidebar` pinned, `#sidebarGroups` scrolls | On a 320×568 phone the rail (brand 62 + list 529 + footer 93 = 684px) pushed **Logout** ~120px below the fold. The brand and footer are now pinned and the group list scrolls; Logout sits at y≈533 of 568. |
| `responsive.css` — shared-expense split grid (≤575px / ≤419px) | The party + amount + percent grid overflowed its card at 320px. The column floors now shrink with the screen and the party name wraps. |
| `responsive.css` — text-link thumb targets | Inline links in helper text, alerts, card footers and `btn-link` controls measured 19–31px tall on touch screens; a padded tap area brings them to ≥32px without moving the copy. |
| `responsive.css` — headers, badges, toolbars, KPI links | The remaining `content-out` / `small-targets` finds at 320–430px. |
| `templates/hdc/payroll/payroll_salary_cards_print.html` | The standalone print sheet had **no viewport meta** (phones rendered it at 980px and scaled it down) and its toolbar wrapped off-screen. Adds the meta plus a `screen and (max-width: 820px)` reflow; `@media print` output is untouched. |
| `templates/hdc/projects/project_report_print.html` | Same treatment: viewport meta + screen-only reflow (stacked header/meta/footer, two-column info boxes, wrapping toolbar). Printing is unchanged. |
| `hdc/services/cashflow_register.py` — day-close row insert is race-safe | The concurrent gate exposed a real 500: two operators opening the same financial day both inserted `hdc_cash_day_position` and the loser died on the UNIQUE index. The insert now runs in a savepoint and adopts the winner’s row (2 of 120 concurrent loads failed before, 0 of 120 after). |

## Page by page

“ok” = clean at every width of that class.  A PC / tablet / phone cell lists
the widths that failed *before* the fix; all of them pass now.

### Login & access

| page | PC | tablet | phone | verdict |
| --- | --- | --- | --- | --- |
| `/hdc/access` | ok | ok | ok | already fine |
| `/hdc/login` | ok | ok | FAIL 320 | **fixed** (1 width) |

### Legacy aliases

| page | PC | tablet | phone | verdict |
| --- | --- | --- | --- | --- |
| `/hdc/purchases` | ok | ok | ok | already fine |
| `/hdc/stages` | ok | ok | ok | already fine |
| `/hdc/subcontractors` | ok | ok | ok | already fine |
| `/project-estimation` | ok | ok | ok | already fine |

### Shared expenses

| page | PC | tablet | phone | verdict |
| --- | --- | --- | --- | --- |
| `/hdc/accounts/shared` | ok | ok | FAIL 320 | **fixed** (1 width) |
| `/hdc/accounts/shared/expenses` | ok | ok | ok | already fine |
| `/hdc/accounts/shared/expenses/1` | ok | ok | FAIL 320 | **fixed** (1 width) |
| `/hdc/accounts/shared/expenses/1/edit` | ok | ok | FAIL 320 | **fixed** (1 width) |
| `/hdc/accounts/shared/expenses/new` | ok | ok | FAIL 320 | **fixed** (1 width) |
| `/hdc/accounts/shared/parties` | ok | ok | FAIL 320 | **fixed** (1 width) |
| `/hdc/accounts/shared/report` | ok | ok | FAIL 320 | **fixed** (1 width) |
| `/hdc/accounts/shared/settlements` | ok | ok | FAIL 320 | **fixed** (1 width) |

### Accounts & cash flow

| page | PC | tablet | phone | verdict |
| --- | --- | --- | --- | --- |
| `/hdc/accounts` | ok | ok | FAIL 320, 360, 375, 390, 430 | **fixed** (5 widths) |
| `/hdc/accounts/1/edit` | ok | ok | FAIL 320 | **fixed** (1 width) |
| `/hdc/accounts/1/ledger` | ok | ok | ok | already fine |
| `/hdc/accounts/cashflow` | ok | ok | ok | already fine |
| `/hdc/accounts/cashflow/reconciliation` | ok | ok | ok | already fine |
| `/hdc/accounts/cashflow/register` | ok | ok | ok | already fine |
| `/hdc/accounts/cashflow/report` | ok | ok | FAIL 320 | **fixed** (1 width) |
| `/hdc/accounts/entries` | ok | ok | ok | already fine |
| `/hdc/accounts/hub` | ok | ok | FAIL 320, 360, 375, 390, 414, 430 | **fixed** (6 widths) |
| `/hdc/accounts/kpi/balances` | ok | ok | FAIL 320 | **fixed** (1 width) |
| `/hdc/accounts/manage` | ok | ok | FAIL 320 | **fixed** (1 width) |
| `/hdc/accounts/money-center` | ok | ok | FAIL 320 | **fixed** (1 width) |
| `/hdc/accounts/new` | ok | ok | ok | already fine |
| `/hdc/accounts/new-transaction` | ok | ok | ok | already fine |
| `/hdc/accounts/new-transaction/subcategories` | ok | ok | ok | already fine |
| `/hdc/accounts/reconciliation` | ok | ok | FAIL 320 | **fixed** (1 width) |
| `/hdc/accounts/transactions/1/edit` | ok | ok | ok | already fine |
| `/hdc/accounts/transactions/find` | ok | ok | FAIL 320, 360, 375, 390, 430 | **fixed** (5 widths) |
| `/hdc/kpi/balances` | ok | ok | FAIL 320 | **fixed** (1 width) |

### Personal management

| page | PC | tablet | phone | verdict |
| --- | --- | --- | --- | --- |
| `/hdc/personal-management` | ok | ok | FAIL 320 | **fixed** (1 width) |
| `/hdc/personal-management/categories` | ok | ok | ok | already fine |
| `/hdc/personal-management/expenses` | ok | ok | ok | already fine |
| `/hdc/personal-management/expenses/1/void` | ok | ok | FAIL 320, 360, 375, 390, 414, 430 | **fixed** (6 widths) |

### Projects, stages & estimation

| page | PC | tablet | phone | verdict |
| --- | --- | --- | --- | --- |
| `/hdc/estimation` | ok | ok | FAIL 320 | **fixed** (1 width) |
| `/hdc/estimation/formulas` | ok | ok | FAIL 320 | **fixed** (1 width) |
| `/hdc/projects` | ok | ok | FAIL 320, 375, 390, 414, 430 | **fixed** (5 widths) |
| `/hdc/projects/1` | ok | ok | FAIL 320, 360, 375, 390, 414, 430 | **fixed** (6 widths) |
| `/hdc/projects/1/edit` | ok | ok | FAIL 320, 360, 375, 390, 414, 430 | **fixed** (6 widths) |
| `/hdc/projects/1/stage/add` | ok | ok | FAIL 320, 360, 375, 390, 414, 430 | **fixed** (6 widths) |
| `/hdc/projects/add` | ok | ok | FAIL 320, 360, 375, 390, 414, 430 | **fixed** (6 widths) |
| `/hdc/stage-library` | ok | ok | FAIL 320, 375, 390, 414, 430 | **fixed** (5 widths) |
| `/hdc/stage/1/edit` | ok | ok | FAIL 320, 360, 375, 390, 414, 430 | **fixed** (6 widths) |
| `/hdc/stage/1/ledger` | ok | ok | FAIL 320 | **fixed** (1 width) |
| `/hdc/stage/drawing/1/view` | ok | ok | ok | already fine |

### Workers & trades

| page | PC | tablet | phone | verdict |
| --- | --- | --- | --- | --- |
| `/hdc/trades` | ok | ok | ok | already fine |
| `/hdc/workers` | ok | ok | ok | already fine |
| `/hdc/workers/1/advance` | ok | ok | FAIL 320, 360, 375, 390, 414, 430 | **fixed** (6 widths) |
| `/hdc/workers/1/ledger` | ok | ok | FAIL 320 | **fixed** (1 width) |
| `/hdc/workers/1/ledger/1/edit` | ok | ok | FAIL 320 | **fixed** (1 width) |
| `/hdc/workers/1/payment` | ok | ok | FAIL 320, 360, 375, 390, 414, 430 | **fixed** (6 widths) |
| `/hdc/workers/1/rate` | ok | ok | FAIL 320, 360, 375, 390, 414, 430 | **fixed** (6 widths) |
| `/hdc/workers/1/statement` | ok | ok | FAIL 320 | **fixed** (1 width) |

### Timekeeping & attendance

| page | PC | tablet | phone | verdict |
| --- | --- | --- | --- | --- |
| `/hdc/attendance` | ok | ok | FAIL 320 | **fixed** (1 width) |
| `/hdc/timekeeping` | ok | ok | FAIL 320 | **fixed** (1 width) |
| `/hdc/timekeeping/2/edit` | ok | ok | FAIL 320, 360, 375, 390, 414, 430 | **fixed** (6 widths) |
| `/hdc/timekeeping/status` | ok | ok | ok | already fine |

### Payroll

| page | PC | tablet | phone | verdict |
| --- | --- | --- | --- | --- |
| `/hdc/payroll` | ok | ok | FAIL 320, 360, 375, 390, 414, 430 | **fixed** (6 widths) |
| `/hdc/payroll/1/salary-cards` | ok | FAIL 768 | FAIL 320, 360, 375, 390, 414, 430 | **fixed** (7 widths) |
| `/hdc/payroll/generate` | ok | ok | FAIL 320 | **fixed** (1 width) |
| `/hdc/payroll/history` | ok | ok | ok | already fine |
| `/hdc/payroll/salary-cards` | ok | ok | FAIL 320 | **fixed** (1 width) |

### Expenses

| page | PC | tablet | phone | verdict |
| --- | --- | --- | --- | --- |
| `/hdc/expense_categories` | ok | ok | ok | already fine |
| `/hdc/expenses` | ok | ok | FAIL 320, 360, 375, 390, 414, 430 | **fixed** (6 widths) |
| `/hdc/expenses/1/edit` | ok | ok | FAIL 320, 360, 375, 390, 414, 430 | **fixed** (6 widths) |

### Subcontractors

| page | PC | tablet | phone | verdict |
| --- | --- | --- | --- | --- |
| `/hdc/subcontractor/1/attendance_page` | ok | ok | ok | already fine |
| `/hdc/subcontractor/1/ledger` | ok | ok | FAIL 320 | **fixed** (1 width) |
| `/hdc/subcontractor/1/payments` | ok | ok | FAIL 320 | **fixed** (1 width) |
| `/hdc/subcontractor/1/workers/1/ledger` | ok | ok | FAIL 320 | **fixed** (1 width) |

### Purchase v2 & materials

| page | PC | tablet | phone | verdict |
| --- | --- | --- | --- | --- |
| `/hdc/materials` | ok | ok | ok | already fine |
| `/hdc/materials/usage` | ok | ok | FAIL 320, 360, 375, 390, 414, 430 | **fixed** (6 widths) |
| `/hdc/purchase-v2` | ok | ok | FAIL 320 | **fixed** (1 width) |
| `/hdc/purchase-v2/delivered` | ok | ok | ok | already fine |
| `/hdc/purchase-v2/materials` | ok | ok | ok | already fine |
| `/hdc/purchase-v2/purchases` | ok | ok | ok | already fine |
| `/hdc/purchase-v2/stock` | ok | ok | ok | already fine |
| `/hdc/purchase-v2/suppliers` | ok | ok | ok | already fine |
| `/hdc/purchase-v2/suppliers/1` | ok | ok | FAIL 320, 360, 375, 390, 414, 430 | **fixed** (6 widths) |
| `/hdc/purchase-v2/usage` | ok | ok | FAIL 320, 360, 375, 390, 414, 430 | **fixed** (6 widths) |

### Tool rental

| page | PC | tablet | phone | verdict |
| --- | --- | --- | --- | --- |
| `/hdc/tool-rental` | ok | ok | FAIL 320 | **fixed** (1 width) |
| `/hdc/tool-rental/1` | ok | ok | FAIL 320, 360, 375, 390, 414, 430 | **fixed** (6 widths) |
| `/hdc/tool-rental/1/edit` | ok | ok | ok | already fine |
| `/hdc/tool-rental/audit` | ok | ok | FAIL 320 | **fixed** (1 width) |
| `/hdc/tool-rental/audit/1` | ok | ok | FAIL 320 | **fixed** (1 width) |
| `/hdc/tool-rental/audit/count` | ok | ok | ok | already fine |
| `/hdc/tool-rental/dashboard` | ok | ok | FAIL 320 | **fixed** (1 width) |
| `/hdc/tool-rental/glance` | ok | ok | FAIL 320 | **fixed** (1 width) |
| `/hdc/tool-rental/inventory` | ok | ok | FAIL 320 | **fixed** (1 width) |
| `/hdc/tool-rental/inventory/1/serials/create` | ok | ok | FAIL 320 | **fixed** (1 width) |
| `/hdc/tool-rental/new` | ok | ok | FAIL 320, 360, 375, 390, 414, 430 | **fixed** (6 widths) |
| `/hdc/tool-rental/reports` | ok | ok | FAIL 320 | **fixed** (1 width) |
| `/hdc/tool-rental/serials` | ok | ok | FAIL 320 | **fixed** (1 width) |
| `/hdc/tool-rental/tool/1` | ok | ok | FAIL 320 | **fixed** (1 width) |
| `/hdc/tool-rental/tracking` | ok | ok | FAIL 320 | **fixed** (1 width) |
| `/hdc/tool-rental/transaction` | ok | ok | ok | already fine |

### Office management

| page | PC | tablet | phone | verdict |
| --- | --- | --- | --- | --- |
| `/hdc/office-management` | ok | ok | FAIL 320, 360, 375, 390, 414, 430 | **fixed** (6 widths) |
| `/hdc/office-management/allowance-categories` | ok | ok | ok | already fine |
| `/hdc/office-management/allowance-categories/1/edit` | ok | ok | FAIL 320, 360, 375, 390, 414, 430 | **fixed** (6 widths) |
| `/hdc/office-management/expenses` | ok | ok | FAIL 320 | **fixed** (1 width) |
| `/hdc/office-management/expenses/1/edit` | ok | ok | FAIL 320, 360, 375, 390, 414, 430 | **fixed** (6 widths) |
| `/hdc/office-management/staff` | ok | ok | FAIL 320 | **fixed** (1 width) |
| `/hdc/office-management/staff/1/allowances` | ok | ok | FAIL 320 | **fixed** (1 width) |
| `/hdc/office-management/staff/1/allowances/1/edit` | ok | ok | FAIL 320, 360, 375, 390, 414, 430 | **fixed** (6 widths) |
| `/hdc/office-management/staff/1/ledger` | ok | ok | FAIL 320 | **fixed** (1 width) |
| `/hdc/office-management/staff/1/ledger/1/edit` | ok | ok | FAIL 320, 360, 375, 390, 414, 430 | **fixed** (6 widths) |
| `/hdc/office-management/staff/1/payment` | ok | ok | FAIL 320, 360, 375, 390, 414, 430 | **fixed** (6 widths) |
| `/hdc/office-management/staff/attendance` | ok | ok | FAIL 320 | **fixed** (1 width) |
| `/hdc/office-management/staff/ledger` | ok | ok | ok | already fine |

### Reports & cost entries

| page | PC | tablet | phone | verdict |
| --- | --- | --- | --- | --- |
| `/hdc/cost-entries/1/1/1` | ok | ok | FAIL 320 | **fixed** (1 width) |
| `/hdc/reports` | ok | ok | FAIL 320 | **fixed** (1 width) |
| `/hdc/reports/glance` | ok | ok | FAIL 320 | **fixed** (1 width) |
| `/hdc/reports/project/1/pdf` | ok | FAIL 768 | FAIL 320, 360, 375, 390, 414, 430 | **fixed** (7 widths) |

### Parties

| page | PC | tablet | phone | verdict |
| --- | --- | --- | --- | --- |
| `/hdc/parties` | ok | ok | FAIL 320 | **fixed** (1 width) |
| `/hdc/parties/1` | ok | ok | FAIL 320, 360, 375, 390, 414, 430 | **fixed** (6 widths) |

### Settings, users & audit

| page | PC | tablet | phone | verdict |
| --- | --- | --- | --- | --- |
| `/hdc/alerts` | ok | ok | ok | already fine |
| `/hdc/event-recorder` | ok | ok | FAIL 320 | **fixed** (1 width) |
| `/hdc/settings` | ok | ok | FAIL 320 | **fixed** (1 width) |
| `/hdc/users` | ok | ok | FAIL 320 | **fixed** (1 width) |

### Dashboard

| page | PC | tablet | phone | verdict |
| --- | --- | --- | --- | --- |
| `/` | ok | ok | FAIL 320 | **fixed** (1 width) |
| `/hdc/` | ok | ok | FAIL 320 | **fixed** (1 width) |
| `/hdc/project-estimation` | ok | ok | ok | already fine |

## How to reproduce

```bash
pip install -r requirements.txt playwright && playwright install chromium
python scripts/qa_responsive_pages.py --output qa/responsive-pages.json
python scripts/qa_responsive_functional.py --views mob-320,mob-390,tab-768,pc-1440 --shots
```

The page gate owns its own server and a throwaway database, seeds one record
of every kind through the real forms/API, and exits non-zero when a page
fails.  Raw evidence: `qa/responsive-pages-before.json` (baseline),
`qa/responsive-pages.json` (after), `qa/responsive-functional.json` and
`qa/screenshots/<viewport>/*.png`.

