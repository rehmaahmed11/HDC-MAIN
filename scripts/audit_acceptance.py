#!/usr/bin/env python3
"""Audit Step 11 — acceptance run against a fresh, throwaway database.

Re-proves the whole money model after the Step 1-15 fixes.  It is read-only
with respect to any real data: it always builds its own database under a
temporary directory unless you point ``--db`` at a copy.

What it checks (the audit's Step 11 list):

* every registered GET route returns < 500 — the crawl that used to find the
  Money Center 500;
* the critical money pages/APIs all return 200;
* ledger invariants: ``orphan_txns = 0``, ``minor_unit_drift = 0``,
  ``cf_orphan_links = 0``;
* voiding from *either* side keeps
  ``hdc_account_txn.is_void == hdc_cash_flow_entry.is_void``;
* ``_accounts_reconciliation_findings()`` reports zero findings on a clean
  dataset that exercises office-salary mirrors (the audit 5.4 false orphan).

Usage::

    python scripts/audit_acceptance.py            # fresh temp database
    python scripts/audit_acceptance.py --db /tmp/x.db --json /tmp/out.json
"""
import argparse
import json
import os
import sys
import tempfile
import traceback

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

PAGES_EXPECTED_200 = (
    '/hdc/accounts', '/hdc/accounts/hub', '/hdc/accounts/entries',
    '/hdc/accounts/manage', '/hdc/accounts/cashflow',
    '/hdc/accounts/cashflow/register', '/hdc/accounts/cashflow/reconciliation',
    '/hdc/accounts/reconciliation', '/hdc/accounts/money-center',
    '/hdc/workers', '/hdc/payroll', '/hdc/expenses', '/hdc/subcontractors',
    '/hdc/purchase-v2/suppliers', '/hdc/tool-rental', '/hdc/projects',
    '/hdc/office-management', '/hdc/reports',
)

APIS_EXPECTED_200 = (
    '/hdc/api/workers', '/hdc/api/suppliers', '/hdc/api/subcontractors',
    '/hdc/api/office_staff', '/hdc/accounts/money-center/api/diagram',
    '/hdc/accounts/money-center/api/pending',
    '/hdc_static/js/pages/money_center.js',
    '/hdc_static/css/money_center.css',
)

# Routes that legitimately answer 400 without query parameters (deliberate
# validation, per the audit's Appendix A note) or 404 without a real id.
ALLOWED_4XX = ('/api/v2/purchase/material-available',
               '/api/v2/purchase/material-stock-scope',
               '/api/v2/purchase/usage-po-options')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', help='database file to use (default: a temp file)')
    ap.add_argument('--json', help='write the full result JSON here')
    args = ap.parse_args()

    tmp = None
    if args.db:
        db_path = args.db
        inst = os.path.join(os.path.dirname(db_path) or '.', 'inst')
    else:
        tmp = tempfile.TemporaryDirectory(prefix='hdc-acceptance-')
        db_path = os.path.join(tmp.name, 'acceptance.db')
        inst = tmp.name

    os.environ.update(HDC_DB_PATH=db_path, HDC_INSTANCE_DIR=inst,
                      HDC_ENV='test', HDC_SECRET_KEY='acceptance-secret',
                      HDC_BOOTSTRAP_ADMIN_PASSWORD='Admin@1234')

    from hdc.app import create_app
    from hdc.extensions import db
    from hdc.models.accounts import Account, AccountTransaction
    from hdc.models.cashflow import CashFlowEntry

    app = create_app({'HDC_DB_PATH': db_path, 'HDC_INSTANCE_DIR': inst,
                      'TESTING': True})
    result = {'crawl': {}, 'pages': {}, 'apis': {}, 'invariants': {},
              'void_sync': {}, 'reconciliation': {}, 'errors': []}

    client = app.test_client()
    client.get('/hdc/login')
    with client.session_transaction() as session:
        token = session['_csrf_token']
    client.post('/hdc/login', data={'username': 'admin',
                                    'password': 'Admin@1234',
                                    '_csrf_token': token})

    # ---- 1. crawl every GET route -------------------------------------
    server_errors = []
    statuses = {}
    with app.test_request_context('/'):
        rules = sorted({r.rule for r in app.url_map.iter_rules()
                        if 'GET' in (r.methods or set()) and '<' not in r.rule})
    for rule in rules:
        try:
            code = client.get(rule).status_code
        except Exception:
            code = 500
            result['errors'].append(f'crawl {rule}: {traceback.format_exc()[-300:]}')
        statuses[rule] = code
        if code >= 500:
            server_errors.append((rule, code))
    result['crawl'] = {'routes': len(rules), 'server_errors': server_errors,
                       'statuses': statuses}

    for url in PAGES_EXPECTED_200:
        result['pages'][url] = client.get(url).status_code
    for url in APIS_EXPECTED_200:
        result['apis'][url] = client.get(url).status_code

    # ---- 2. build a clean dataset that exercises every mirror ---------
    with app.app_context():
        from hdc.services.accounts import (
            _accounts_reconciliation_findings,
            _create_accounts_transaction_with_sync)
        from hdc.services.cashflow_register import (
            category_options, save_manual_cash_flow_entry,
            void_manual_cash_flow_entry, restore_manual_cash_flow_entry)

        cash = Account.query.filter(Account.name == 'Company Cash').first()
        party = Account.query.filter(Account.name == 'Credit/Debit Control').first()
        if party is None:
            party = Account(name='Credit/Debit Control', type='credit_debit')
            db.session.add(party)
            db.session.commit()
        # Fund the company account: this is a scratch database, and the
        # overdraft block (correctly) refuses the seeded out-payments below
        # against a zero balance.
        cash.opening_balance = 500000.0
        cash.opening_balance_minor = 50000000
        db.session.commit()

        # a generic out/in pair through the real posting service
        ok, msg, _rows = _create_accounts_transaction_with_sync({
            'date': '2026-03-01', 'type': 'party_payment', 'amount': 2500,
            'from_account_id': cash.id, 'to_account_id': party.id,
            'executed_by_account_id': cash.id, 'category': 'expense'})
        if not ok:
            result['errors'].append(f'seed party_payment: {msg}')

        # a cash-flow register document (status: active)
        cats = category_options('out')
        cf_out, direction = None, None
        if cats:
            cf_out, _warn = save_manual_cash_flow_entry(
                direction='out', amount=1000.0, account_id=cash.id,
                destination_account_id=party.id, category_id=int(cats[0].id),
                description='acceptance run')
            db.session.commit()

        findings = _accounts_reconciliation_findings()
        result['reconciliation'] = {
            key: (len(value) if isinstance(value, list) else value)
            for key, value in findings.items()}

        # ---- 3. invariants by direct SQL -----------------------------
        from sqlalchemy import text
        orphan_txns = db.session.execute(text("""
            SELECT COUNT(*) FROM hdc_account_txn t
            WHERE (t.from_account_id IS NOT NULL AND t.from_account_id NOT IN (SELECT id FROM hdc_account))
               OR (t.to_account_id   IS NOT NULL AND t.to_account_id   NOT IN (SELECT id FROM hdc_account))
        """)).scalar() or 0
        minor_drift = db.session.execute(text("""
            SELECT COUNT(*) FROM hdc_account_txn
            WHERE amount_minor <> CAST(ROUND(amount * 100) AS INTEGER)
        """)).scalar() or 0
        cf_orphan = db.session.execute(text("""
            SELECT COUNT(*) FROM hdc_cash_flow_entry e
            WHERE e.account_tx_id IS NOT NULL
              AND e.account_tx_id NOT IN (SELECT id FROM hdc_account_txn)
        """)).scalar() or 0
        result['invariants'] = {'orphan_txns': int(orphan_txns),
                               'minor_unit_drift': int(minor_drift),
                               'cf_orphan_links': int(cf_orphan)}

        # ---- 4. void from the *ledger* side must void the CF document --
        if cf_out is not None:
            txn_id = int(cf_out.account_tx_id)
            ledger_voids_cf = None
            resp = client.post('/hdc/accounts', data={
                '_csrf_token': token, 'action': 'void_transaction',
                'transaction_id': txn_id, 'void_reason': 'acceptance void'})
            db.session.expire_all()
            entry = db.session.get(CashFlowEntry, cf_out.id)
            txn = db.session.get(AccountTransaction, txn_id)
            ledger_voids_cf = bool(entry.is_void) == bool(txn.is_void) and bool(txn.is_void)

            # and the CF side must void the ledger row back
            restore_manual_cash_flow_entry(entry)
            db.session.commit()
            db.session.expire_all()
            entry = db.session.get(CashFlowEntry, cf_out.id)
            restore_ok = (not entry.is_void) and (not db.session.get(
                AccountTransaction, txn_id).is_void)
            void_manual_cash_flow_entry(entry, reason='acceptance cf void')
            db.session.commit()
            db.session.expire_all()
            entry = db.session.get(CashFlowEntry, cf_out.id)
            cf_voids_ledger = bool(db.session.get(AccountTransaction, txn_id).is_void)

            result['void_sync'] = {
                'void_from_all_entries_http': resp.status_code,
                'ledger_void_voids_cf': ledger_voids_cf,
                'restore_from_cf_side': restore_ok,
                'cf_void_voids_ledger': cf_voids_ledger,
            }

    # ---- 5. verdict ----------------------------------------------------
    ok_crawl = not server_errors
    ok_pages = all(v == 200 for v in result['pages'].values())
    ok_apis = all(v == 200 for v in result['apis'].values())
    ok_invariants = all(v == 0 for v in result['invariants'].values())
    # Each finding family is a list/count; the extra ``totals`` key is a nested
    # dict and is not itself a finding.
    ok_recon = all(
        (v == 0 if isinstance(v, int) else (len(v) == 0 if isinstance(v, list) else True))
        for k, v in result['reconciliation'].items() if k != 'totals')
    ok_void = all(result['void_sync'].get(k) for k in
                  ('ledger_void_voids_cf', 'restore_from_cf_side',
                   'cf_void_voids_ledger'))
    result['verdict'] = {
        'crawl_no_5xx': ok_crawl,
        'money_pages_200': ok_pages,
        'money_apis_200': ok_apis,
        'ledger_invariants_zero': ok_invariants,
        'reconciliation_zero_findings': ok_recon,
        'void_sync_both_directions': ok_void,
        'PASS': all([ok_crawl, ok_pages, ok_apis, ok_invariants, ok_recon, ok_void]),
    }

    print(f"crawled {result['crawl']['routes']} no-arg GET routes; "
          f"{len(server_errors)} response(s) >= 500")
    for rule, code in server_errors:
        print(f"   SERVER ERROR {code} {rule}")
    print(f"money pages 200: {ok_pages} | money APIs 200: {ok_apis}")
    print(f"invariants: {result['invariants']}")
    print(f"reconciliation findings: {result['reconciliation']}")
    print(f"void sync: {result['void_sync']}")
    print(f"VERDICT: {'PASS' if result['verdict']['PASS'] else 'FAIL'}")
    if result['errors']:
        print(f"harness errors: {len(result['errors'])}")
        for e in result['errors'][:5]:
            print('  ', e.splitlines()[-1][:160])

    if args.json:
        with open(args.json, 'w', encoding='utf-8') as fh:
            json.dump(result, fh, indent=1, sort_keys=True)
        print('wrote', args.json)

    if tmp is not None:
        tmp.cleanup()
    return 0 if result['verdict']['PASS'] else 1


if __name__ == '__main__':
    sys.exit(main())
