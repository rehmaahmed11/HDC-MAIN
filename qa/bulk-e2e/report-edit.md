# Bulk E2E — EDIT phase

- Run at: 2026-11-27 09:34:32
- Records requested per section: 100
- Database: `/tmp/hdc-bulk-e2e/bulk.db` (disposable)
- Python 3.11.2

| Section | Rows | HTTP ok | DB verify | Report reads | Failures | Time |
|---|---|---|---|---|---|---|
| Parties directory (`parties`) | 100 | 100/100 | PASS (100) | 2/2 | 0 | 0.79s |
| Projects (`projects`) | 100 | 100/100 | PASS (100) | 2/2 | 0 | 0.42s |
| Stages (`stages`) | 100 | 100/100 | PASS (100) | 1/1 | 0 | 0.53s |
| Workers (`workers`) | 100 | 100/100 | PASS (100) | 2/2 | 0 | 0.82s |
| Timekeeping (attendance) (`timekeeping`) | 100 | 100/100 | PASS (100) | 1/1 | 0 | 1.26s |
| Project expenses (`expenses`) | 100 | 100/100 | PASS (100) | 2/2 | 0 | 0.87s |
| Office staff (`office_staff`) | 100 | 100/100 | PASS (100) | 1/1 | 0 | 0.46s |
| Office expenses (`office_expenses`) | 100 | 100/100 | PASS (100) | 1/1 | 0 | 0.62s |
| Suppliers (`suppliers`) | 100 | 100/100 | PASS (100) | 2/2 | 0 | 0.6s |
| Materials (`materials`) | 100 | 100/100 | PASS (100) | 2/2 | 0 | 0.56s |
| Purchases (V2) (`purchases`) | 100 | 100/100 | PASS (100) | 2/2 | 0 | 0.68s |
| Tools inventory (`tools`) | 100 | 100/100 | PASS (100) | 2/2 | 0 | 11.34s |
| Accounts (money pots) (`accounts`) | 100 | 100/100 | PASS (100) | 2/2 | 0 | 0.77s |
| Cash-flow register (`cashflow`) | 100 | 100/100 | PASS (100) | 2/2 | 0 | 1.93s |
| Shared expenses (`shared`) | 100 | 100/100 | PASS (100) | 2/2 | 0 | 0.73s |

**Failures: 0**
