# Bulk E2E — final summary

Run: 2026-11-27 09:34:32 → 2026-11-27 09:35:31
Sections: 15 · records per section: 100

| Phase | HTTP OK | Attempted |
|---|---|---|
| Create | 1500 | 1500 |
| Edit | 1500 | 1500 |
| Delete | 1500 | 1500 |

## Per-section result

| Section | Created | Edited | Deleted | Active rows after delete | Final report reads |
|---|---|---|---|---|---|
| Parties directory | 100/100 | 100/100 | 100/100 | 0 | 2/2 |
| Projects | 100/100 | 100/100 | 100/100 | 0 | 2/2 |
| Stages | 100/100 | 100/100 | 100/100 | 0 | 1/1 |
| Workers | 100/100 | 100/100 | 100/100 | 0 | 2/2 |
| Timekeeping (attendance) | 100/100 | 100/100 | 100/100 | 0 | 1/1 |
| Project expenses | 100/100 | 100/100 | 100/100 | 0 | 2/2 |
| Office staff | 100/100 | 100/100 | 100/100 | 0 | 1/1 |
| Office expenses | 100/100 | 100/100 | 100/100 | 0 | 1/1 |
| Suppliers | 100/100 | 100/100 | 100/100 | 0 | 2/2 |
| Materials | 100/100 | 100/100 | 100/100 | 0 | 2/2 |
| Purchases (V2) | 100/100 | 100/100 | 100/100 | 0 | 2/2 |
| Tools inventory | 100/100 | 100/100 | 100/100 | 0 | 2/2 |
| Accounts (money pots) | 100/100 | 100/100 | 100/100 | 0 | 2/2 |
| Cash-flow register | 100/100 | 100/100 | 100/100 | 0 | 2/2 |
| Shared expenses | 100/100 | 100/100 | 100/100 | 0 | 2/2 |

## Net effect on the ledger after voiding/deleting every row

- Row-count deltas: `{'account_txns': 400, 'cash_flow_entries': 200, 'labour_ledger': 100, 'expenses': 100, 'office_expenses': 100}`
- Account balances differing from baseline: `none`
