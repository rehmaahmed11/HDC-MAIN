# Bulk E2E — CREATE phase

- Run at: 2026-11-27 09:34:32
- Records requested per section: 100
- Database: `/tmp/hdc-bulk-e2e/bulk.db` (disposable)
- Python 3.11.2

| Section | Rows | HTTP ok | DB verify | Report reads | Failures | Time |
|---|---|---|---|---|---|---|
| Parties directory (`parties`) | 100 | 100/100 | PASS (100) | 2/2 | 0 | 0.67s |
| Projects (`projects`) | 100 | 100/100 | PASS (100) | 2/2 | 0 | 0.83s |
| Stages (`stages`) | 100 | 100/100 | PASS (100) | 1/1 | 0 | 0.54s |
| Workers (`workers`) | 100 | 100/100 | PASS (100) | 2/2 | 0 | 0.81s |
| Timekeeping (attendance) (`timekeeping`) | 100 | 100/100 | PASS (100) | 1/1 | 0 | 12.6s |
| Project expenses (`expenses`) | 100 | 100/100 | PASS (100) | 2/2 | 0 | 1.19s |
| Office staff (`office_staff`) | 100 | 100/100 | PASS (100) | 1/1 | 0 | 0.44s |
| Office expenses (`office_expenses`) | 100 | 100/100 | PASS (100) | 1/1 | 0 | 0.85s |
| Suppliers (`suppliers`) | 100 | 100/100 | PASS (100) | 2/2 | 0 | 0.63s |
| Materials (`materials`) | 100 | 100/100 | PASS (100) | 2/2 | 0 | 0.62s |
| Purchases (V2) (`purchases`) | 100 | 100/100 | PASS (100) | 2/2 | 0 | 0.72s |
| Tools inventory (`tools`) | 100 | 100/100 | PASS (100) | 2/2 | 0 | 1.94s |
| Accounts (money pots) (`accounts`) | 100 | 100/100 | PASS (100) | 2/2 | 0 | 0.73s |
| Cash-flow register (`cashflow`) | 100 | 100/100 | PASS (100) | 2/2 | 0 | 1.39s |
| Shared expenses (`shared`) | 100 | 100/100 | PASS (100) | 2/2 | 0 | 0.79s |

**Failures: 0**
