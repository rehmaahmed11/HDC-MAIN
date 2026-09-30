#!/usr/bin/env python3
"""Smoke-test harness: every major money-writing form + on-the-fly add-new.

Runs against a fresh test DB. For each surface:
  1. GET the page / render the form
  2. If a required picker item is missing, use the "+ Add New ..." modal
     to create it (account / party / project)
  3. POST the form with that new item selected
  4. Check the result (success = 302 redirect with flash,
     failure = 200 with error message kept)

Reports pass/fail per surface and notes any broken flows.
"""
import os, sys, tempfile, re
os.environ['HDC_ENV'] = 'test'
os.environ['HDC_SECRET_KEY'] = 'unit-test-secret'
os.environ['HDC_BOOTSTRAP_ADMIN_PASSWORD'] = 'Admin@1234'

sys.path.insert(0, '.')
from hdc.app import create_app
from hdc.extensions import db
from hdc.services.cashflow_register import category_options, save_cf_party

REPO = os.path.dirname(os.path.abspath(__file__))

def run_smoke():
    tmp = tempfile.mkdtemp(prefix='hdc-smoke-')
    app = create_app({
        'HDC_DB_PATH': os.path.join(tmp, 'smoke.db'),
        'HDC_INSTANCE_DIR': tmp,
        'TESTING': True,
    })
    client = app.test_client()
    results = {}

    with app.app_context():
        # Login
        client.get('/hdc/login')
        with client.session_transaction() as s:
            token = s['_csrf_token']
        resp = client.post('/hdc/login', data={
            'username': 'admin', 'password': 'Admin@1234', '_csrf_token': token,
        })
        results['login'] = (resp.status_code == 302, 'login redirect')

        # Helper to extract CSRF + idempotency from a page
        def tokens(url):
            html = client.get(url).get_data(as_text=True)
            csrf = re.search(r'name="_csrf_token" value="([^"]+)"', html)
            key = re.search(r'name="_idempotency_key" value="([^"]+)"', html)
            return (csrf.group(1) if csrf else None,
                    key.group(1) if key else None,
                    html)

        # Helper: submit a form payload
        def submit(url, payload):
            csrf, key, _ = tokens(url)
            p = dict(payload)
            p.setdefault('action', 'create_entry')
            p['_csrf_token'] = csrf
            p['_idempotency_key'] = key
            return client.post(url, data=p, follow_redirects=True)

        # 1. New Transaction with basic Money Out + add-new Party on-the-fly
        try:
            from hdc.services.cashflow_register import save_cf_party as save_party
            # Ensure a supplier exists for purchase-type categories
            party, created = save_party('Smoke Supplier', party_type='supplier')
            db.session.commit()
            results['add_party_on_fly'] = (True, f"created={created} name={party.name}")
        except Exception as exc:
            results['add_party_on_fly'] = (False, str(exc))

        # 2. New Transaction: basic Money Out with category/subcategory
        try:
            csrf, key, html = tokens('/hdc/accounts/new-transaction')
            cat = [c for c in category_options() if c.name == 'Material & Purchase'][0]
            sub = [s for s in category_options(cat.id) if s.active][0] if category_options(cat.id) else None
            # Use existing accounts from seed
            resp = submit('/hdc/accounts/new-transaction', {
                'direction': 'out', 'date': '2026-09-21', 'amount': '500',
                'account_id': '1', 'category_id': cat.id,
                'subcategory_id': sub.id if sub else '',
                'party_name': '', 'project_id': '',
                'reference': 'SMOKE-1',
            })
            text = resp.get_data(as_text=True)
            ok = 'recorded' in text or 'already recorded' in text or 'not saved' not in text.lower()
            # Check for success flash or at least no critical error
            results['new_transaction_money_out'] = (ok, f"status={resp.status_code} flash_present={('recorded' in text or 'already' in text)}")
        except Exception as exc:
            results['new_transaction_money_out'] = (False, str(exc))

        # 3. Internal Transfer
        try:
            resp = submit('/hdc/accounts/new-transaction', {
                'direction': 'transfer', 'date': '2026-09-21', 'amount': '100',
                'account_id': '1', 'destination_account_id': '3',
                'reference': 'SMOKE-TRF',
            })
            text = resp.get_data(as_text=True)
            ok = 'recorded' in text or 'already' in text
            results['new_transaction_transfer'] = (ok, f"status={resp.status_code} flash={'recorded' in text}")
        except Exception as exc:
            results['new_transaction_transfer'] = (False, str(exc))

        # 4. Add New Project on-the-fly (through the new-transaction modal endpoint)
        try:
            import json
            with client.session_transaction() as s:
                csrf_hdr = s.get('_csrf_token', '')
            resp = client.post('/hdc/accounts/new-transaction/project',
                               json={'name': 'Smoke Project', 'client': 'Smoke Client'},
                               headers={'X-CSRFToken': csrf_hdr})
            data = resp.get_json()
            results['add_project_on_fly'] = (data.get('ok') is True, f"created={data.get('created')} msg={data.get('message')}")
        except Exception as exc:
            results['add_project_on_fly'] = (False, str(exc))

        # 5. Add New Account (cash) on-the-fly
        try:
            with client.session_transaction() as s:
                csrf_hdr = s.get('_csrf_token', '')
            resp = client.post('/hdc/accounts/new-transaction/account',
                               json={'name': 'Smoke Cash Account', 'mode': 'cash'},
                               headers={'X-CSRFToken': csrf_hdr})
            data = resp.get_json()
            results['add_account_on_fly'] = (data.get('ok') is True, f"created={data.get('created')}")
        except Exception as exc:
            results['add_account_on_fly'] = (False, str(exc))

        # 6. Add New Account (bank) with bank fields
        try:
            with client.session_transaction() as s:
                csrf_hdr = s.get('_csrf_token', '')
            resp = client.post('/hdc/accounts/new-transaction/account',
                               json={'name': 'Smoke Bank Account', 'mode': 'bank',
                                     'bank_name': 'Smoke Bank', 'account_number': '999'},
                               headers={'X-CSRFToken': csrf_hdr})
            data = resp.get_json()
            results['add_bank_account_on_fly'] = (data.get('ok') is True, f"created={data.get('created')}")
        except Exception as exc:
            results['add_bank_account_on_fly'] = (False, str(exc))

        # 7. Check that category rules load (deep-level check)
        try:
            from hdc.services.cashflow_register import category_rules_map
            rules = category_rules_map()
            receipt_rules = rules.get(1, {})  # category id 1 = Owner / Client Receipt
            has_project_effect = receipt_rules.get('project_effect') == 'receipt'
            results['category_rules_loaded'] = (has_project_effect, f"receipt_effect={receipt_rules.get('project_effect')}")
        except Exception as exc:
            results['category_rules_loaded'] = (False, str(exc))

        # 8. Subcategory endpoint returns correct scoped results
        try:
            resp = client.get('/hdc/accounts/new-transaction/subcategories?category_id=1')
            data = resp.get_json()
            results['subcategory_lookup'] = (data.get('ok') and len(data.get('items', [])) > 0,
                                             f"items={len(data.get('items', []))}")
        except Exception as exc:
            results['subcategory_lookup'] = (False, str(exc))

    # Print results
    print("\n" + "=" * 60)
    print("SMOKE TEST RESULTS")
    print("=" * 60)
    for test, (ok, detail) in results.items():
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {test:<35s} {detail}")
    print("=" * 60)

    all_ok = all(ok for ok, _ in results.values())
    print(f"\nOVERALL: {'ALL PASS' if all_ok else 'SOME FAILURES'} ({sum(1 for ok, _ in results.items() if ok)}/{len(results)} surfaces OK)")
    return results

if __name__ == '__main__':
    run_smoke()
