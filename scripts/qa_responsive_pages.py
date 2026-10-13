#!/usr/bin/env python3
"""Page-by-page responsive audit in a real browser (PC / tablet / phone).

Renders **every** GET page of the application in Chromium at twelve viewports —
three desktop widths, three tablets and six phones (430 down to 320 CSS px) —
and fails the run if any page loses content off the screen, crops it inside a
container, keeps a table wider than the screen, or leaves a record-card field
unlabelled.

Why this exists
---------------
``scripts/qa_responsive.py`` checks the adaptive layer statically (syntax,
breakpoints, load order) and ``scripts/qa_browser.py`` asserts that the
*document* does not scroll sideways.  That assertion is necessary but not
sufficient: ``html { overflow-x: clip }`` — the guard that stops the page from
scrolling sideways — also hides any element that escapes the viewport, so the
document reports no overflow while a customer name, an amount or a whole table
column is silently cut off.  This script measures the elements themselves:

* **content-out** — an element's box crosses the viewport edge with no
  scrollable or clipping ancestor: the operator can never reach that content.
* **clipped** — text (or an in-flow child) overflows an ``overflow: hidden``
  container that has no scrolling: the container cuts it off.
  Decorative bleed (a KPI card's ``::after`` circle, an ``opacity: .18``
  background icon) and deliberate ``text-overflow: ellipsis`` are excluded;
  so are form controls, which scroll their own value by design.
* **wide-table** — a table wider than the screen with nothing to scroll it.
* **unlabelled-cards** — a row-as-card cell with no column heading (phone).
* **small-targets** — a link or button under 32px tall on a touch screen.

Usage
-----
    pip install playwright && playwright install chromium
    python scripts/qa_responsive_pages.py --output qa/responsive_pages.json
    python scripts/qa_responsive_pages.py --executable /usr/bin/chromium
    python scripts/qa_responsive_pages.py --views mob-390,tab-768   # spot check

The script owns its server and a throwaway database (it can never touch a
deployment), seeds one record of every kind through the real HTTP forms, and
exits non-zero when any page fails at any width.
"""
from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import re
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

VIEWPORTS = {
    'pc-1920': (1920, 1080), 'pc-1440': (1440, 900), 'pc-1280': (1280, 800),
    'tab-1024': (1024, 768), 'tab-834': (834, 1112), 'tab-768': (768, 1024),
    'mob-430': (430, 932), 'mob-414': (414, 896), 'mob-390': (390, 844),
    'mob-375': (375, 812), 'mob-360': (360, 800), 'mob-320': (320, 568),
}
DEFAULT_VIEWS = ','.join(VIEWPORTS)
MOBILE = {k for k in VIEWPORTS if k.startswith('mob')}
TABLET = {k for k in VIEWPORTS if k.startswith('tab')}
ADMIN_PASSWORD = 'Responsive-Qa-Only-123'

# Pages that are a file, a print sheet or a machine-readable export rather than
# a screen the operator navigates to.
SKIP_MARKERS = ('logout', 'deploy', 'backup', '/export', '.csv', '.json', '.xlsx',
                '.pdf', 'receipt', 'download', '/api/', 'access-data', '/csv', '/xlsx',
                '/print', 'print_view')

# --------------------------------------------------------------------------
# The measurement probe.  Runs in the page and returns one record per render.
# --------------------------------------------------------------------------
PROBE_JS = r'''
() => {
  const vw = window.innerWidth;
  const doc = document.documentElement;
  const shown = (el) => {
    try { return el.checkVisibility({ checkVisibilityCSS: true, checkOpacity: true, contentVisibilityAuto: true }); }
    catch (e) { const cs = getComputedStyle(el); return cs.display !== 'none' && cs.visibility !== 'hidden'; }
  };
  const ignorable = (el) => el.classList.contains('visually-hidden') || el.classList.contains('sr-only')
        || el.getAttribute('aria-hidden') === 'true' || !!el.closest('.visually-hidden, .sr-only');
  const scrollableAncestor = (el) => {
    let p = el.parentElement;
    while (p && p !== doc) {
      const cs = getComputedStyle(p);
      if ((cs.overflowX === 'auto' || cs.overflowX === 'scroll') && p.scrollWidth > p.clientWidth + 1) return p;
      p = p.parentElement;
    }
    return null;
  };
  /* Anything lost to a card's own overflow: hidden is reported by the
     "clipped" check; anything lost to the page-level html/body clip is real. */
  const clippedByAncestor = (el) => {
    let p = el.parentElement;
    while (p && p !== doc && p !== document.body) {
      const ox = getComputedStyle(p).overflowX;
      if (ox === 'hidden' || ox === 'clip') return p;
      p = p.parentElement;
    }
    return null;
  };
  const cssPath = (el) => {
    const bits = [];
    let n = el;
    while (n && n.nodeType === 1 && bits.length < 6) {
      let s = n.tagName.toLowerCase();
      if (n.id) { s += '#' + n.id; bits.unshift(s); break; }
      const cls = (n.className && typeof n.className === 'string')
        ? n.className.trim().split(/\s+/)
            .filter(c => !/^(mt|mb|ms|me|py|px|pt|pb|g|col|text)-/.test(c)).slice(0, 3).join('.') : '';
      if (cls) s += '.' + cls;
      bits.unshift(s);
      n = n.parentElement;
    }
    return bits.join('>');
  };
  const label = (el) => (el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 70);
  const FORMS = new Set(['INPUT', 'TEXTAREA', 'SELECT', 'BUTTON', 'OPTION']);
  const inFlow = (k) => {
    const cs = getComputedStyle(k);
    return (cs.position === 'static' || cs.position === 'relative') && parseFloat(cs.opacity) >= 0.5
        && cs.display !== 'none' && cs.visibility !== 'hidden';
  };
  const textOverflows = (el) => {
    const er = el.getBoundingClientRect();
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) {
      if (!node.nodeValue || !node.nodeValue.trim()) continue;
      const par = node.parentElement;
      if (!par || !inFlow(par)) continue;
      if (par.closest('.visually-hidden, .sr-only')) continue;
      const range = document.createRange();
      range.selectNodeContents(node);
      for (const rect of Array.from(range.getClientRects())) {
        if (rect.width > 0 && (rect.right > er.right + 1 || rect.left < er.left - 1)) return true;
      }
    }
    return false;
  };
  const inFlowChildOverflows = (el) => {
    const er = el.getBoundingClientRect();
    return Array.from(el.children).some(k => {
      if (!inFlow(k)) return false;
      const kr = k.getBoundingClientRect();
      return kr.width > 0 && (kr.right > er.right + 1 || kr.left < er.left - 1);
    });
  };
  const overflowers = [], clipped = [], wideTables = [], small = [];
  document.querySelectorAll('body *').forEach((el) => {
    if (!shown(el) || ignorable(el)) return;
    if (el.closest('[data-hdc-responsive-ignore]')) return;
    const r = el.getBoundingClientRect();
    if ((r.right > vw + 1 || r.left < -1) && el.clientWidth > 0
        && !scrollableAncestor(el) && !clippedByAncestor(el)) {
      overflowers.push({sel: cssPath(el), left: Math.round(r.left), right: Math.round(r.right),
                        w: Math.round(r.width), text: label(el), tag: el.tagName.toLowerCase(),
                        parent: el.parentElement ? cssPath(el.parentElement) : ''});
    }
    const cs = getComputedStyle(el);
    if ((cs.overflowX === 'hidden' || cs.overflowX === 'clip') && el.clientWidth > 0
        && !FORMS.has(el.tagName) && !scrollableAncestor(el) && !cs.textOverflow.includes('ellipsis')
        && el.scrollWidth > el.clientWidth + 2 && (textOverflows(el) || inFlowChildOverflows(el))) {
      clipped.push({sel: cssPath(el), need: el.scrollWidth, have: el.clientWidth, text: label(el)});
    }
  });
  const tops = new Set(overflowers.map(o => o.sel));
  const topOut = overflowers.filter(o => !(o.parent && tops.has(o.parent)));
  document.querySelectorAll('table').forEach((t) => {
    if (!shown(t) || t.closest('[data-hdc-responsive-ignore]')) return;
    if ((t.scrollWidth > t.clientWidth + 2 || t.getBoundingClientRect().right > vw + 1) && !scrollableAncestor(t))
      wideTables.push({sel: cssPath(t), sw: t.scrollWidth, cw: t.clientWidth,
                       cols: t.querySelectorAll('thead th').length,
                       rows: t.querySelectorAll('tbody tr').length,
                       stacked: t.classList.contains('hdc-rt-stack')});
  });
  document.querySelectorAll('a, button, [role=button], input[type=submit]').forEach((el) => {
    if (!shown(el) || ignorable(el) || el.closest('table') || el.closest('.modal')) return;
    const r = el.getBoundingClientRect();
    if (r.height < 32 && r.width > 0)
      small.push({sel: cssPath(el), h: Math.round(r.height), w: Math.round(r.width), text: label(el).slice(0, 45)});
  });
  const unlabelled = document.querySelectorAll(
    '[data-hdc-rt-stack] td:not([data-hdc-label]):not([colspan]):not([rowspan])').length;
  const contentOverflow = topOut.reduce((m, o) => Math.max(m, o.right - vw, -o.left), 0);
  return {vw,
    overflow: contentOverflow,
    docScrollDelta: doc.scrollWidth - vw,
    overflowers: topOut.slice(0, 10), overflowers_total: topOut.length,
    clipped: clipped.slice(0, 8), clipped_total: clipped.length,
    wideTables: wideTables.slice(0, 6), wideTables_total: wideTables.length,
    smallTargets: small.slice(0, 8), smallTargets_total: small.length,
    unlabelledCells: unlabelled,
    stackedTables: document.querySelectorAll('table.hdc-rt-stack').length,
    mainTables: document.querySelectorAll('main table').length};
}
'''


# --------------------------------------------------------------------------
# Fixture: one of everything, written through the real forms.
# --------------------------------------------------------------------------
def seed_demo(client, app):
    """Create a small but complete business dataset (returns an id map)."""
    from datetime import timedelta

    from hdc.extensions import db
    from hdc.models.accounts import Account
    from hdc.models.materials import Delivery, PurchaseV2, Supplier
    from hdc.models.office import OfficeStaff
    from hdc.models.projects import Project, Stage
    from hdc.models.subcontract import Subcontractor
    from hdc.models.tool_rental import Tool, ToolRental
    from hdc.models.workforce import LabourLedger, TimeEntry, Worker
    from hdc.utils.dates import _pkt_today

    def token():
        client.get('/hdc/login')
        with client.session_transaction() as session:
            return session['_csrf_token']

    # Sign in, or every write below silently redirects back to the login page.
    csrf = token()
    client.post('/hdc/login', data={'username': 'admin', 'password': ADMIN_PASSWORD,
                                    '_csrf_token': csrf})
    csrf = token()
    with client.session_transaction() as session:
        assert session.get('_user_id'), 'audit client could not sign in as admin'
    today = _pkt_today()
    d0, d1, d2 = today.isoformat(), (today - timedelta(days=1)).isoformat(), (today - timedelta(days=2)).isoformat()

    def post(url, data, expect=(200, 302)):
        response = client.post(url, data=dict(data, _csrf_token=csrf))
        assert response.status_code in expect, (url, response.status_code,
                                                response.get_data(as_text=True)[:200])
        assert '/hdc/login' not in (response.headers.get('Location') or ''), \
            ('%s bounced to the login page' % url)
        return response

    def api(url, payload, method='POST'):
        response = client.open(url, method=method, json=payload, headers={'X-CSRFToken': csrf})
        assert response.status_code == 200, (url, response.status_code, response.get_data(as_text=True)[:200])
        return response.get_json() or {}

    with app.app_context():
        cash = Account.query.filter_by(name='Company Cash').one().id
    api(f'/api/accounts/account/{cash}', {'opening_balance': 1000000}, 'PUT')
    api('/api/accounts/create_account', {'name': 'Qa Bank', 'account_group': 'company',
                                         'account_mode': 'bank', 'bank_name': 'HBL',
                                         'account_number': '0001'})
    with app.app_context():
        bank = Account.query.filter_by(name='Qa Bank').one().id
        category = None
        from hdc.models.accounts import ExpenseCategory
        cat = ExpenseCategory.query.filter_by(active_status=True).first()
        category = cat.id if cat else None

    post('/hdc/projects/add', dict(project_code='QA-RWD', name='Riverside Villas', client='Mr Qa',
                                   client_phone='0300-0000000', location='Lahore', total_sqft=2000,
                                   owner_rate=3000, start_date=d2, status='active'))
    post('/hdc/projects/add', dict(project_code='QA-LMP', name='Gulberg Shop', client='Ms Qa',
                                   lump_sum=900000, start_date=d2, status='active'))
    with app.app_context():
        pa = Project.query.filter_by(project_code='QA-RWD').one().id
        pb = Project.query.filter_by(project_code='QA-LMP').one().id
    post(f'/hdc/projects/{pa}/stage/add', dict(name='Grey Structure', contract_basis='Per Sq Ft',
                                               rate_per_sqft=1500, qty_sqft=2000, status='Active'))
    post(f'/hdc/projects/{pa}/stage/add', dict(name='Finishing', contract_basis='Lump Sum',
                                               lump_sum_value=1500000, status='Active'))
    post(f'/hdc/projects/{pb}/stage/add', dict(name='Renovation', contract_basis='Lump Sum',
                                               lump_sum_value=500000, status='Active'))
    with app.app_context():
        sa1 = Stage.query.filter_by(project_id=pa, name='Grey Structure').one().id
        sb1 = Stage.query.filter_by(project_id=pb).one().id
    post(f'/hdc/projects/{pa}/owner_payment', dict(date=d1, amount=500000,
                                                   received_to_account_id=cash, remarks='1st'))
    post(f'/hdc/projects/{pb}/owner_payment', dict(date=d1, amount=100000,
                                                   received_to_account_id=bank, remarks='advance'))
    post('/hdc/trades', dict(action='add', trade_name='Steel Fixer'))
    post('/hdc/workers', dict(worker_code='QA-W1', name='Aslam Mason', role_type='Mason',
                              wage_type='daily', daily_wage=1500))
    post('/hdc/workers', dict(worker_code='QA-W2', name='Bilal Labour', role_type='Labor',
                              wage_type='daily', daily_wage=1000))
    with app.app_context():
        w1 = Worker.query.filter_by(worker_code='QA-W1').one().id
        w2 = Worker.query.filter_by(worker_code='QA-W2').one().id
    post(f'/hdc/workers/{w1}/rate', dict(wage_type='daily', new_rate=1600,
                                         effective_from=d1, reason='increment'))
    for day, rows in ((d2, {w1: ('present', 8, 0), w2: ('present', 8, 0)}),
                      (d1, {w1: ('present', 8, 2), w2: ('absent', 0, 0)}),
                      (d0, {w1: ('present', 8, 0), w2: ('present', 8, 1)})):
        data = {'bulk_mode': '1', 'date': day}
        for wid, (status, hours, ot) in rows.items():
            data.update({f'status_{wid}': status, f'project_id_{wid}': pa, f'stage_id_{wid}': sa1,
                         f'working_hours_{wid}': hours, f'overtime_hours_{wid}': ot})
        post('/hdc/timekeeping', data)
    post(f'/hdc/workers/{w1}/advance', dict(date=d0, amount=1000, project_id=pa,
                                            stage_id=sa1, notes='advance'))
    post(f'/hdc/workers/{w1}/payment', dict(date=d0, amount=2000, project_id=pa,
                                            stage_id=sa1, notes='wages'))
    post(f'/hdc/workers/{w2}/payment', dict(date=d0, amount=1000, project_id=pa,
                                            stage_id=sa1, notes='wages'))
    with app.app_context():
        ledger_id = LabourLedger.query.filter_by(worker_id=w1, entry_type='payment').first().id
        time_entry = TimeEntry.query.filter_by(worker_id=w2).first().id
    post('/hdc/payroll/generate', dict(date_from=d2, date_to=d0))
    post(f'/hdc/projects/{pa}/add_subcontractor', dict(stage_id=sa1, name='Qa Builders',
                                                       phone='0301-0000000'))
    with app.app_context():
        sub = Subcontractor.query.filter_by(name='Qa Builders').one().id
    post(f'/hdc/stage/{sa1}/shift/subcontractor', dict(subcontractor_id=sub, contract_type='sqft',
                                                       rate_per_sqft=400, total_sqft=2000,
                                                       retention_pct=5))
    post(f'/hdc/subcontractor/{sub}/team_attendance', dict(project_id=pa, stage_id=sa1, date=d0,
                                                           worker_type='Mason', period_from=d2,
                                                           period_to=d0, days_count=2,
                                                           workers_count=3, wage_rate=1200))
    post(f'/hdc/subcontractor/{sub}/progress', dict(work_done_percentage=25))
    post(f'/hdc/subcontractor/{sub}/pay', dict(date=d0, amount=150000, project_id=pa,
                                               stage_id=sa1, notes='running bill'))
    post('/hdc/purchase-v2/suppliers', dict(name='Qa Cement Co', phone='0302-0000000',
                                            opening_balance=10000, address='Raiwind'))
    with app.app_context():
        supplier = Supplier.query.filter_by(name='Qa Cement Co').one().id
    supplier2 = api('/api/v2/purchase/suppliers', {'name': 'Qa Steel Traders'})['id']
    material = api('/api/v2/purchase/materials', {'name': 'Qa Cement', 'unit': 'Bag'})['id']
    material2 = api('/api/v2/purchase/materials', {'name': 'Qa Steel', 'unit': 'KG'})['id']
    purchase = api('/api/v2/purchase/purchases', dict(supplier_id=supplier, material_id=material,
                                                      unit_price=1300, quantity=200, date=d1,
                                                      purchase_date=d1, invoice_no='QA-1'))['id']
    api('/api/v2/purchase/purchases', dict(supplier_id=supplier2, material_id=material2,
                                           unit_price=280, quantity=1000, date=d1,
                                           purchase_date=d1))
    api('/api/v2/purchase/deliveries', dict(project_id=pa, stage_id=sa1, purchase_id=purchase,
                                            quantity=120, date=d1, delivery_person='Driver'))
    api('/api/v2/purchase/deliveries', dict(project_id=pb, stage_id=sb1, purchase_id=purchase,
                                            quantity=40, date=d1))
    api('/api/v2/purchase/usage', dict(project_id=pa, stage_id=sa1, purchase_id=purchase,
                                       material_id=material, quantity=30, date=d0))
    api('/api/v2/purchase/payments', dict(supplier_id=supplier, amount=50000, note='part payment'))
    if category:
        post('/hdc/expenses', dict(date=d0, category_id=category, amount=5000, remarks='QA expense',
                                   project_id=pa, stage_id=sa1))
    post('/hdc/office-management/staff', dict(staff_code='QA-S1', name='Qa Clerk',
                                              designation='Clerk', monthly_salary=45000))
    with app.app_context():
        staff = OfficeStaff.query.first().id if OfficeStaff.query.first() else None
    if staff:
        post(f'/hdc/office-management/staff/{staff}/ledger', dict(date=d0, entry_type='salary',
                                                                  amount=20000, notes='QA ledger'))
    post('/hdc/office-management/expenses', dict(date=d0, amount=1200, category='Utilities',
                                                 description='QA office expense'))
    post('/hdc/tool-rental/inventory', dict(action='add_tool', name='Qa Mixer',
                                            total_quantity=2, purchase_cost=90000,
                                            rental_rate_per_day=1500, unit='pcs',
                                            condition='good', description='QA mixer'))
    with app.app_context():
        tool_row = Tool.query.filter_by(name='Qa Mixer').first()
        tool_id = tool_row.id if tool_row else None
    if tool_id:
        post('/hdc/tool-rental/create', dict(txn_type='new_rental', renter_type='external',
                                             customer_name='Mr Qa Customer',
                                             customer_phone='0303-0000000',
                                             billing_type='fixed_fee', rental_date=d1,
                                             expected_return_date=d0, notes='QA rental',
                                             **{'tool_id[]': [str(tool_id)],
                                                'qty[]': ['1'], 'rate[]': ['1500']}))
    with app.app_context():
        rental_row = ToolRental.query.first()
        rental = rental_row.id if rental_row else None
    post('/hdc/accounts/cashflow/register', dict(action='create_entry', direction='in', amount=25000,
                                                 account_id=cash, date=d0, description='QA receipt'))
    post('/hdc/accounts/cashflow/register', dict(action='create_entry', direction='out', amount=4000,
                                                 account_id=cash, date=d0, description='QA payment'))
    # ---- rows whose only path is a second-level page -----------------------
    from hdc.models.shared_expenses import SharedParty
    with app.app_context():
        party = SharedParty.query.first()
    post('/hdc/accounts/shared/expenses/new',
         dict(date=d0, title='QA shared fuel', category='Fuel', total_amount=10000,
              payer_party_id=party.id if party else '', split_mode='equal',
              money_source='none', note='QA split'))
    post(f'/hdc/subcontractor/{sub}/workers', dict(name='Qa Helper', phone='0304-0000000',
                                                   trade='Mason', daily_wage=1300))
    post('/hdc/office-management/allowance-categories',
         dict(name='QA Transport', description='QA allowance head'))
    with app.app_context():
        from hdc.models.office import AllowanceCategory
        office_cat = AllowanceCategory.query.first()
    if staff and office_cat:
        post(f'/hdc/office-management/staff/{staff}/allowances',
             dict(category_id=office_cat.id, amount=3000, effective_date=d0))
    from hdc.models.accounts import AccountTransaction, Expense, PersonalExpense
    from hdc.models.cashflow import CashFlowEntry
    from hdc.models.office import OfficeStaffLedger
    from hdc.models.projects import StageDrawing
    from hdc.models.shared_expenses import SharedExpense, SharedExpenseShare, SharedParty
    from hdc.utils.dates import _pkt_today

    with app.app_context():
        # ---- last-resort rows for the pages whose only entry point is a
        #      second-level screen (a detail page for a document this seed
        #      could not create through a form).  Without them the audit would
        #      silently skip a page; the layout is what is under test here, so
        #      the row is written directly and the skip is never silent.
        if SharedExpense.query.count() == 0:
            party_a = SharedParty.query.first()
            if party_a is None:
                party_a = SharedParty(name='QA Head A', status='active')
                db.session.add(party_a)
                db.session.flush()
            party_b = SharedParty(name='QA Head B', status='active')
            db.session.add(party_b)
            db.session.flush()
            expense = SharedExpense(date=_pkt_today(), title='QA shared fuel', category='Fuel',
                                    total_amount=10000.0, total_amount_minor=1000000,
                                    payer_party_id=party_a.id, split_mode='equal')
            db.session.add(expense)
            db.session.flush()
            db.session.add_all([
                SharedExpenseShare(expense_id=expense.id, party_id=party_a.id, amount=5000.0,
                                   amount_minor=500000, percent_bp=5000, position=0),
                SharedExpenseShare(expense_id=expense.id, party_id=party_b.id, amount=5000.0,
                                   amount_minor=500000, percent_bp=5000, position=1),
            ])
        if staff and OfficeStaffLedger.query.count() == 0:
            db.session.add(OfficeStaffLedger(staff_id=staff, date=_pkt_today(),
                                             entry_type='payment', amount=20000.0,
                                             notes='QA ledger'))
        if PersonalExpense.query.count() == 0:
            db.session.add(PersonalExpense(beneficiary_name='Qa Vendor', beneficiary_type='other',
                                           date=_pkt_today(), category='Misc',
                                           amount=2500.0, remarks='QA personal'))
        if StageDrawing.query.count() == 0:
            from hdc.config import get_runtime_settings
            folder = get_runtime_settings().stage_drawings_dir
            os.makedirs(folder, exist_ok=True)
            stored = 'qa-drawing.pdf'
            with open(os.path.join(folder, stored), 'wb') as handle:
                handle.write(b'%PDF-1.4\n% QA placeholder\n')
            db.session.add(StageDrawing(stage_id=sa1, original_name='QA drawing.pdf',
                                        stored_name=stored))
        db.session.commit()

        ids = dict(
            pid=pa, sid=sa1, wid=w1, tid=time_entry,
            eid=Expense.query.first().id if Expense.query.first() else 1,
            expense_id=Expense.query.first().id if Expense.query.first() else 1,
            supplier_id=supplier, material_id=material, purchase_id=purchase, account_id=cash,
            txn_id=AccountTransaction.query.first().id if AccountTransaction.query.first() else 1,
            rental_id=rental or 1, tool_id=tool_id or 1, staff_id=staff or 1,
            lid=ledger_id, aid=1, cid=1,
            rid=CashFlowEntry.query.first().id if CashFlowEntry.query.first() else 1,
            party_id=1, oid=1, run_id=1, did=1,
            delivery_id=Delivery.query.first().id if Delivery.query.first() else 1,
            usage_id=PurchaseV2.query.first().id if PurchaseV2.query.first() else 1,
            id=1, metric='balances')
        ids['expense_pk'] = SharedExpense.query.first().id if SharedExpense.query.first() else 1
        personal = PersonalExpense.query.first()
        ids['personal_expense_id'] = personal.id if personal else 1
        staff_ledger = OfficeStaffLedger.query.first()
        ids['staff_ledger_id'] = staff_ledger.id if staff_ledger else 1
        drawing = StageDrawing.query.first()
        ids['drawing_id'] = drawing.id if drawing else 1
    return ids


def arg_ids(app):
    """Map every ``<converter:argument>`` used by a rule to a real row id.

    Pages are addressed by primary key, so a made-up ``1`` produces honest 404s
    that look like failures.  Each argument name is resolved against the table
    that seeds it; anything unknown falls back to ``1``.
    """
    from hdc.models.accounts import Account, AccountTransaction, Expense, ExpenseCategory
    from hdc.models.cashflow import CashFlowEntry
    from hdc.models.materials import Delivery, MaterialV2, PurchaseV2, Supplier
    from hdc.models.office import OfficeStaff
    from hdc.models.projects import Project, Stage
    from hdc.models.subcontract import Subcontractor
    from hdc.models.tool_rental import Tool, ToolRental
    from hdc.models.workforce import LabourLedger, Worker

    def first(model, **where):
        try:
            row = model.query.filter_by(**where).first() if where else model.query.first()
        except Exception:  # noqa: BLE001
            row = None
        return row.id if row else None

    with app.app_context():
        from hdc.models.office import AllowanceCategory, StaffAllowance
        from hdc.models.projects import StageDrawing
        from hdc.models.shared_expenses import SharedExpense, SharedParty, SharedSettlement
        values = {
            'pid': first(Project), 'project_id': first(Project),
            'sid': first(Stage), 'stage_id': first(Stage),
            'wid': first(Worker), 'worker_id': first(Worker),
            'sub_id': first(Subcontractor), 'subcontractor_id': first(Subcontractor),
            'sid_or_sub': first(Subcontractor),
            'eid': first(Expense), 'expense_id': first(Expense),
            'expense_pk': first(Expense), 'shared_expense_id': first(SharedExpense),
            'category_id': first(ExpenseCategory), 'cat_id': first(ExpenseCategory),
            'supplier_id': first(Supplier), 'material_id': first(MaterialV2),
            'purchase_id': first(PurchaseV2), 'delivery_id': first(Delivery),
            'usage_id': first(PurchaseV2), 'tool_id': first(Tool),
            'rental_id': first(ToolRental), 'staff_id': first(OfficeStaff),
            'allowance_id': first(StaffAllowance), 'ledger_id': first(LabourLedger),
            'lid': first(LabourLedger), 'txn_id': first(AccountTransaction),
            'transaction_id': first(AccountTransaction), 'account_id': first(Account),
            'aid': first(Account), 'rid': first(CashFlowEntry),
            'cid': first(AllowanceCategory), 'party_id': first(SharedParty),
            'settlement_id': first(SharedSettlement), 'drawing_id': first(StageDrawing),
            'did': first(Delivery),
        }
    return {k: v for k, v in values.items() if v}


# A rule's ``<id>`` means whatever table that page reads.  ``/…/expenses/<eid>``
# is a PersonalExpense in the personal module and a SharedExpense in the shared
# one, so the page decides, not the argument name.
RULE_MODELS = (
    ('/accounts/shared/expenses/', 'SharedExpense', 'expense_id'),
    ('/accounts/shared/settlements/', 'SharedSettlement', 'settlement_id'),
    ('/office-management/staff/', 'OfficeStaffLedger', 'lid'),
    ('/personal-management/expenses/', 'PersonalExpense', 'eid'),
    ('/subcontractor/', 'SubcontractLabourWorker', 'wid'),
    ('/stage/drawing/', 'StageDrawing', 'did'),
)


def build_pages(app, ids):
    """Every navigable GET page, with real ids substituted for the fixtures."""
    ids = dict(ids)
    ids.update(arg_ids(app))

    def first(model_name):
        import hdc.models as models
        model = getattr(models, model_name, None)
        if model is None:
            return None
        try:
            with app.app_context():
                row = model.query.first()
            return row.id if row else None
        except Exception:  # noqa: BLE001
            return None

    pages = []
    for rule in app.url_map.iter_rules():
        if 'GET' not in rule.methods or rule.endpoint == 'static':
            continue
        url = rule.rule
        if any(marker in str(rule) for marker in SKIP_MARKERS):
            continue
        for arg in rule.arguments:
            value = ids.get(arg)
            for prefix, model_name, arg_name in RULE_MODELS:
                if prefix in url and arg == arg_name:
                    value = first(model_name) or value
            url = re.sub(r'<(?:[a-z_]+:)?%s>' % arg, str(value if value else 1), url)
        if '<' in url:
            continue
        pages.append(url)
    return sorted(set(pages))


# --------------------------------------------------------------------------
# Browser side
# --------------------------------------------------------------------------
def _launch(pw, executable):
    return pw.chromium.launch(executable_path=executable,
                              args=['--no-sandbox', '--disable-dev-shm-usage', '--headless'])


def _context(browser, view):
    # Real phone/tablet context (touch, mobile UA hints) at the exact width
    # under test: the login form and the shell behave differently on touch.
    width, height = VIEWPORTS[view]
    return browser.new_context(viewport={'width': width, 'height': height},
                              has_touch=view in MOBILE or view in TABLET,
                              is_mobile=view in MOBILE)


def _login(page, base):
    """Sign in once per context; later pages in the same context reuse it."""
    page.goto(base + '/hdc/login', wait_until='load')
    if page.locator('[name=username]').count() == 0:
        return
    page.fill('[name=username]', 'admin')
    page.fill('[name=password]', ADMIN_PASSWORD)
    page.click('button[type=submit]')
    page.wait_for_url(base + '/hdc/')


def _audit_pages(base, view, pages, executable, timeout):
    """Render `pages` at one viewport; returns a list of records."""
    from playwright.sync_api import sync_playwright
    out = []
    with sync_playwright() as pw:
        browser = _launch(pw, executable)
        context = _context(browser, view)
        page = context.new_page()
        page.set_default_timeout(timeout)
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)[:160]))
        page.on('response', lambda r: errors.append('HTTP %s %s' % (r.status, r.url))
                if r.status >= 400 and '/static' not in r.url else None)
        _login(page, base)
        width, height = VIEWPORTS[view]
        for path in pages:
            record = {'path': path, 'view': view, 'w': width, 'h': height}
            del errors[:]
            try:
                response = page.goto(base + path, wait_until='load')
                page.wait_for_timeout(90)
                record['status'] = response.status if response else 0
                content_type = (response.headers.get('content-type') if response else '') or ''
                if record['status'] == 200 and 'html' not in content_type.lower():
                    record['status'] = 'document'
                    record['content_type'] = content_type
                    out.append(record)
                    continue
                if record['status'] != 200:
                    out.append(record)
                    continue
                record.update(page.evaluate(PROBE_JS))
                record['problems'] = []
                if record['overflowers_total'] or record['overflow'] > 1:
                    record['problems'].append('content-out')
                if record['clipped_total']:
                    record['problems'].append('clipped')
                if record['wideTables_total']:
                    record['problems'].append('wide-table')
                if view in MOBILE and record['unlabelledCells']:
                    record['problems'].append('unlabelled-cards')
                if view in MOBILE and record['smallTargets_total']:
                    record['problems'].append('small-targets')
                record['page_errors'] = [e for e in errors if not e.startswith('HTTP')][:3]
                record['http_errors'] = [e for e in errors if e.startswith('HTTP')][:3]
            except Exception as ex:  # noqa: BLE001
                if 'Download is starting' in str(ex):
                    # A PDF/image the app streams (a drawing, a receipt) is a
                    # document, not a screen: there is no layout to measure.
                    record['status'] = 'document'
                    record['problems'] = []
                else:
                    record['status'] = 'ERR'
                    record['error'] = repr(ex)[:200]
                    record['problems'] = ['exception']
            out.append(record)
        context.close()
        browser.close()
    return out


def _worker(slot, base, views, pages, executable, timeout, queue):
    out = []
    for view in views:
        try:
            out.extend(_audit_pages(base, view, pages, executable, timeout))
        except Exception as ex:  # noqa: BLE001
            out.append({'path': '<worker>', 'view': view, 'status': 'ERR',
                        'error': repr(ex)[:300], 'problems': ['worker-error']})
    queue.put(out)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--output', required=True, help='JSON report path')
    parser.add_argument('--executable', help='path to a Chromium/Chrome binary')
    parser.add_argument('--views', default=DEFAULT_VIEWS, help='comma separated viewport names')
    parser.add_argument('--workers', type=int, default=2, help='parallel browsers (default 2)')
    parser.add_argument('--seed-only', action='store_true', help='seed a DB and print the page list')
    parser.add_argument('--timeout', type=int, default=25000, help='per-page timeout (ms)')
    args = parser.parse_args()

    views = [v.strip() for v in args.views.split(',') if v.strip()]
    unknown = [v for v in views if v not in VIEWPORTS]
    if unknown:
        parser.error('unknown viewport(s): %s (known: %s)' % (', '.join(unknown), ', '.join(VIEWPORTS)))

    from werkzeug.serving import make_server
    from hdc.app import create_app
    from hdc.extensions import db

    tmp = tempfile.mkdtemp(prefix='hdc-responsive-audit-')
    os.environ.update(HDC_ENV='test', HDC_SECRET_KEY='responsive-audit-only',
                      HDC_BOOTSTRAP_ADMIN_PASSWORD=ADMIN_PASSWORD,
                      HDC_INSTANCE_DIR=tmp, HDC_DB_PATH=os.path.join(tmp, 'audit.db'))
    app = create_app({'TESTING': True})
    started = time.time()
    with app.test_client() as client:
        ids = seed_demo(client, app)
    pages = build_pages(app, ids)
    print('seeded %s pages in %.1fs (%s)' % (len(pages), time.time() - started, tmp))

    server = make_server('127.0.0.1', 0, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = 'http://127.0.0.1:%d' % server.server_port

    if args.seed_only:
        for path in pages:
            print(path)
        server.shutdown()
        return 0

    # Fail early — and loudly — when the browser layer is missing, instead of
    # discovering it inside a forked worker.
    import playwright.sync_api  # noqa: F401
    assert hasattr(playwright.sync_api, 'sync_playwright')

    try:
        mp.set_start_method('spawn', force=True)
    except RuntimeError:
        pass
    # Every page is rendered at every viewport: the views are shared out over
    # the workers, never the pages, so no page can go unaudited at a width.
    queue = mp.Queue()
    processes = []
    for index in range(max(1, args.workers)):
        assigned = views[index::max(1, args.workers)]
        if not assigned:
            continue
        process = mp.Process(target=_worker,
                             args=(index, base, assigned, pages, args.executable,
                                   args.timeout, queue))
        process.start()
        processes.append(process)

    results = []
    for _ in processes:
        results.extend(queue.get())
    for process in processes:
        process.join()
    server.shutdown()
    with app.app_context():
        db.session.remove()
        db.engine.dispose()

    results.sort(key=lambda r: (r['path'], r['view']))
    audited = [r for r in results if r.get('status') == 200]
    bad = [r for r in audited if r.get('problems')]
    documents = [r for r in results if r.get('status') == 'document']
    unreachable = [r for r in results if r.get('status') not in (200, 'document')]
    report = {
        'scope': ('Real-browser render of every GET page at %d viewports; a page fails when '
                  'content escapes the screen, is cropped, keeps an over-wide table, loses a '
                  'record-card label or presents an undersized thumb target.' % len(views)),
        'pages': pages, 'views': views, 'renders': len(results), 'audited': len(audited),
        'documents': sorted({r['path'] for r in documents}),
        'failed': len(bad), 'unreachable': unreachable, 'results': results,
    }
    Path(args.output).write_text(json.dumps(report, indent=1) + '\n')

    # ---------------------------------------------------------------- summary
    by_page = {}
    for record in bad:
        by_page.setdefault(record['path'], {})[record['view']] = record
    print('\n%d renders / %d pages / %d viewports: %d failed, %d not reachable'
          % (len(results), len(pages), len(views), len(bad), len(unreachable)))
    if documents:
        print('  (documents, not screens: %s)' % ', '.join(sorted({r['path'] for r in documents})))
    for path, records in by_page.items():
        views_hit = ', '.join(sorted(records))
        first = records[sorted(records)[0]]
        print('  FAIL %s  [%s]  %s' % (path, views_hit, ','.join(first['problems'])))
        for offender in first.get('overflowers', [])[:2]:
            print('       out : %s  right=%s w=%s "%s"' % (offender['sel'][:100], offender['right'],
                                                           offender['w'], offender['text'][:40]))
        for crop in first.get('clipped', [])[:2]:
            print('       clip: %s  %s<-%s' % (crop['sel'][:100], crop['have'], crop['need']))
        for target in first.get('smallTargets', [])[:2]:
            print('       tap : %s  h=%s "%s"' % (target['sel'][:90], target['h'], target['text'][:30]))
    if unreachable:
        print('  (not reachable: %s)' % ', '.join(sorted({r['path'] for r in unreachable})))
    print('\nreport: %s' % args.output)
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
