# Bulk E2E — DELETE phase

- Run at: 2026-11-27 09:34:32
- Records requested per section: 100
- Database: `/tmp/hdc-bulk-e2e/bulk.db` (disposable)
- Python 3.11.2

| Section | Rows | HTTP ok | DB verify | Report reads | Failures | Time |
|---|---|---|---|---|---|---|
| Parties directory (`parties`) | 100 | 100/100 | PASS (0) | 2/2 | 0 | 0.77s |
| Projects (`projects`) | 100 | 100/100 | PASS (0) | 2/2 | 0 | 0.51s |
| Stages (`stages`) | 100 | 100/100 | PASS (0) | 1/1 | 0 | 1.36s |
| Workers (`workers`) | 100 | 100/100 | PASS (0) | 2/2 | 0 | 0.5s |
| Timekeeping (attendance) (`timekeeping`) | 100 | 100/100 | PASS (0) | 1/1 | 0 | 0.76s |
| Project expenses (`expenses`) | 100 | 100/100 | PASS (0) | 2/2 | 0 | 0.59s |
| Office staff (`office_staff`) | 100 | 100/100 | PASS (0) | 1/1 | 0 | 0.39s |
| Office expenses (`office_expenses`) | 100 | 100/100 | PASS (0) | 1/1 | 0 | 0.47s |
| Suppliers (`suppliers`) | 100 | 100/100 | PASS (0) | 2/2 | 0 | 0.53s |
| Materials (`materials`) | 100 | 100/100 | PASS (0) | 2/2 | 0 | 0.54s |
| Purchases (V2) (`purchases`) | 100 | 100/100 | PASS (0) | 2/2 | 0 | 0.72s |
| Tools inventory (`tools`) | 100 | 100/100 | PASS (0) | 2/2 | 0 | 0.52s |
| Accounts (money pots) (`accounts`) | 100 | 100/100 | PASS (0) | 2/2 | 0 | 0.78s |
| Cash-flow register (`cashflow`) | 100 | 100/100 | PASS (0) | 2/2 | 0 | 0.71s |
| Shared expenses (`shared`) | 100 | 100/100 | PASS (0) | 2/2 | 0 | 0.84s |

**Failures: 0**
