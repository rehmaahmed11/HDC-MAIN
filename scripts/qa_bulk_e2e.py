#!/usr/bin/env python3
"""Bulk per-section end-to-end test: create N -> read reports -> edit N -> delete N -> re-read reports.

Drives the real Flask routes over the test client (same code paths as a browser
POST), then verifies each step twice:

* in the database (authoritative row state), and
* through the section's own report surface (HTML page, CSV export or JSON API),
  which is what an operator would actually look at.

Every section gets ``--count`` records (default 100). Each record carries a
unique marker (``BLKPROJ-0001``) so the run can prove its own rows were created,
edited and removed without disturbing whatever else is in the database.

Usage:
    .venv/bin/python scripts/qa_bulk_e2e.py --count 100
    .venv/bin/python scripts/qa_bulk_e2e.py --count 100 --sections projects,expenses
    .venv/bin/python scripts/qa_bulk_e2e.py --count 5 --keep-db     # quick check

Writes ``report-create.{json,md}``, ``report-edit.*``, ``report-delete.*`` and
``report-final.*`` into ``--out`` (default ``qa/bulk-e2e``).
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import shutil
import sys
import time
import traceback
from datetime import timedelta

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, ROOT)

from hdc.app import create_app  # noqa: E402
from hdc.extensions import db  # noqa: E402
from hdc.models import *  # noqa: E402,F401,F403
from hdc.models import (Account, CashFlowEntry, CashFlowParty, Expense,  # noqa: E402
                        ExpenseCategory, MaterialV2, OfficeExpense, OfficeStaff,
                        Project, PurchaseV2, SharedExpense, SharedParty, Stage,
                        Supplier, TimeEntry, Tool, ToolCategory, Worker,
                        CashFlowCategory)
from hdc.utils.dates import _pkt_today  # noqa: E402

# --------------------------------------------------------------------------- #
# runtime context
# --------------------------------------------------------------------------- #


class Ctx:
    def __init__(self, client, app, token):
        self.client = client
        self.app = app
        self.token = token
        self.warnings = []

    def flashes(self):
        with self.client.session_transaction() as s:
            fl = list(s.get('_flashes') or [])
            s.pop('_flashes', None)
        return fl

    def post(self, url, data=None, headers=None):
        payload = dict(data or {})
        payload.setdefault('_csrf_token', self.token[0])
        self.flashes()
        r = self.client.post(url, data=payload, headers=headers or {})
        fl = self.flashes()
        bad = [m for c, m in fl if c in ('danger', 'error')]
        warn = [m for c, m in fl if c == 'warning']
        body = r.get_data(as_text=True)
        if r.status_code == 200:
            m = re.search(r'alert-danger[^>]*>(.*?)<', body, re.S)
            if m and m.group(1).strip():
                bad.append(m.group(1).strip())
        ok = r.status_code in (200, 302) and not bad
        return {'ok': ok, 'status': r.status_code, 'bad': bad, 'warn': warn,
                'body': body[:400]}

    def get(self, url):
        self.flashes()
        r = self.client.get(url)
        self.flashes()
        return r

    def api(self, url, method='POST', payload=None):
        self.flashes()
        r = self.client.open(url, method=method, json=payload or {},
                             headers={'X-CSRFToken': self.token[0]})
        self.flashes()
        try:
            body = r.get_json()
        except Exception:  # noqa: BLE001
            body = None
        return {'ok': r.status_code < 300, 'status': r.status_code, 'json': body,
                'body': r.get_data(as_text=True)[:300]}


def dbq(fn):
    with APP.app_context():
        try:
            return fn()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            db.session.rollback()
            return None


# --------------------------------------------------------------------------- #
# section framework
# --------------------------------------------------------------------------- #


class Section:
    key = ''
    title = ''
    slug = ''
    #: True when the delete action is a void/archive, so the section's export
    #: still lists the row as history after the delete phase (by design).
    after_delete_visible = False
    #: column used for LIKE verification, if the default strategy fits
    marker_col = None
    #: extra page/export URLs read after every phase (formatted with self)
    pages = ()

    def __init__(self):
        self.ids = []
        self.t0 = 0.0

    # -- to be provided by subclasses ------------------------------------- #
    def create(self, i, code):
        raise NotImplementedError

    def edit(self, i, code, rid):
        raise NotImplementedError

    def delete(self, i, code, rid):
        raise NotImplementedError

    # -- default verification --------------------------------------------- #
    def _base(self):
        return db.session.query(self.model)

    def count_active(self):
        col = getattr(self.model, self.marker_col)
        return self._base().filter(col.like(f'{self.slug}-%'),
                                   *self.active_filters()).count()

    def count_edited(self):
        col = getattr(self.model, self.marker_col)
        return self._base().filter(col.like(f'{self.slug}-%-ED'),
                                   *self.active_filters()).count()

    def active_filters(self):
        return ()

    def extra_counts(self):
        return {}

    # -- reports ----------------------------------------------------------- #
    def report_urls(self):
        return list(self.pages)

    def read_reports(self, expected_created, expected_after_delete):
        out = []
        for label, url, kind in self.report_urls():
            r = CTX.get(url)
            detail = f'status={r.status_code}'
            ok = r.status_code in (200, 302)
            if ok and kind in ('csv', 'text', 'json'):
                text = r.get_data(as_text=True)
                n = text.count(self.slug)
                want = expected_created if expected_after_delete is None else expected_after_delete
                present = n > 0
                if want and want > 0:
                    ok = n >= min(want, 1)
                elif self.after_delete_visible:
                    ok = True  # voided rows stay in history/export on purpose
                else:
                    ok = n == 0
                detail += (f' slug_hits={n} expected='
                           f'{"present" if (want or self.after_delete_visible) else "absent"}')
            out.append({'label': label, 'url': url, 'ok': ok, 'detail': detail})
        return out


# --------------------------------------------------------------------------- #
# helpers shared by section implementations
# --------------------------------------------------------------------------- #


def find_one(model, **kw):
    with APP.app_context():
        return model.query.filter_by(**kw).first()


def _id(model, **kw):
    row = find_one(model, **kw)
    return row.id if row else None


def marker_text(code, suffix=''):
    return f'{code}{suffix}'


# --------------------------------------------------------------------------- #
# sections
# --------------------------------------------------------------------------- #


class Parties(Section):
    key, title, slug = 'parties', 'Parties directory', 'BLKPARTY'
    model = CashFlowParty
    marker_col = 'name'
    pages = (
        ('Parties directory', '/hdc/parties?status=all', 'page'),
        ('Parties search', '/hdc/parties?q=BLKPARTY&status=all', 'page'),
    )

    def create(self, i, code):
        r = CTX.post('/hdc/parties', {
            'action': 'add_party', 'name': code, 'party_type': 'supplier',
            'phone': '0300-0000000', 'note': 'bulk qa'})
        rid = _id(CashFlowParty, name=code)
        return r, (rid if rid else None)

    def edit(self, i, code, rid):
        r = CTX.post('/hdc/parties', {
            'action': 'edit_party', 'party_id': rid, 'name': code,
            'party_type': 'supplier', 'phone': '0301-1111111',
            'note': code + '-ED'})
        return r

    def delete(self, i, code, rid):
        return CTX.post('/hdc/parties', {'action': 'toggle_party', 'party_id': rid})

    def active_filters(self):
        return (CashFlowParty.is_active == True,)  # noqa: E712

    def count_edited(self):
        return self._base().filter(CashFlowParty.note.like(f'{self.slug}-%-ED'),
                                   *self.active_filters()).count()

    def extra_counts(self):
        return {'deactivated': db.session.query(CashFlowParty).filter(
            CashFlowParty.name.like(f'{self.slug}-%'),
            CashFlowParty.is_active == False).count()}  # noqa: E712


class Projects(Section):
    key, title, slug = 'projects', 'Projects', 'BLKPROJ'
    model = Project
    marker_col = 'project_code'
    pages = (
        ('Projects list', '/hdc/projects', 'page'),
        ('Profitability export', '/hdc/reports/export/profitability', 'csv'),
    )

    def create(self, i, code):
        r = CTX.post('/hdc/projects/add', {
            'project_code': code, 'name': f'Bulk Project {i}', 'client': 'Bulk Client',
            'client_phone': '0300-1234567', 'location': 'Bulk Site',
            'total_sqft': 1000, 'owner_rate': 1000,
            'start_date': (TODAY - timedelta(days=1)).isoformat(), 'status': 'active'})
        rid = _id(Project, project_code=code)
        return r, (rid if rid else None)

    def edit(self, i, code, rid):
        return CTX.post(f'/hdc/projects/{rid}/edit', {
            'project_code': code, 'name': f'Bulk Project {i}', 'client': 'Bulk Client',
            'location': code + '-ED', 'total_sqft': 1000, 'owner_rate': 1000,
            'start_date': (TODAY - timedelta(days=1)).isoformat(), 'status': 'active',
            'contract_type': 'per_sqft'})

    def count_edited(self):
        return self._base().filter(Project.location.like(f'{self.slug}-%-ED')).count()

    def delete(self, i, code, rid):
        # No UI/API delete route exists for projects: remove via ORM and say so.
        def _do():
            p = db.session.get(Project, rid)
            if p is None:
                return True
            db.session.delete(p)
            db.session.commit()
            return True
        try:
            return {'ok': bool(dbq(_do)), 'status': 'orm', 'bad': [], 'warn': []}
        except Exception as ex:  # noqa: BLE001
            return {'ok': False, 'status': 'orm', 'bad': [repr(ex)], 'warn': []}


class Stages(Section):
    key, title, slug = 'stages', 'Stages', 'BLKSTG'
    model = Stage
    marker_col = 'name'
    pages = (('Stage overview', '/hdc/stages', 'page'),)

    def create(self, i, code):
        r = CTX.post(f'/hdc/projects/{HOST_PID}/stage/add', {
            'name': code, 'contract_basis': 'Lump Sum', 'lump_sum_value': 100000,
            'status': 'Active'})
        rid = _id(Stage, name=code)
        return r, (rid if rid else None)

    def edit(self, i, code, rid):
        # Contract values are deliberately unchanged: any change writes a
        # StageRateHistory row, and the app then refuses to delete the stage
        # because it holds linked history. Name-only edit keeps delete testable.
        return CTX.post(f'/hdc/stage/{rid}/edit', {
            'name': code + '-ED', 'contract_basis': 'Lump Sum',
            'lump_sum_value': 100000, 'status': 'Active', 'estimated_cost': 0,
            'progress': 0})

    def delete(self, i, code, rid):
        return CTX.post(f'/hdc/stage/{rid}/delete')


class Workers(Section):
    key, title, slug = 'workers', 'Workers', 'BLKWK'
    model = Worker
    marker_col = 'worker_code'
    pages = (
        ('Workers list', '/hdc/workers', 'page'),
        ('Workers API', '/hdc/api/workers', 'json'),
    )

    def create(self, i, code):
        r = CTX.post('/hdc/workers', {
            'worker_code': code, 'name': code + '-WORKER', 'role_type': 'Mason',
            'wage_type': 'daily', 'daily_wage': 1200})
        rid = _id(Worker, worker_code=code)
        return r, (rid if rid else None)

    def edit(self, i, code, rid):
        return CTX.post(f'/hdc/workers/{rid}/edit', {
            'worker_code': code, 'name': code + '-ED', 'role_type': 'Mason',
            'wage_type': 'daily', 'daily_wage': 1300})

    def delete(self, i, code, rid):
        return CTX.post(f'/hdc/workers/{rid}/toggle')

    def active_filters(self):
        return (Worker.active_status == True,)  # noqa: E712

    def count_edited(self):
        return self._base().filter(Worker.name.like(f'{self.slug}-%-ED'),
                                   *self.active_filters()).count()

    def extra_counts(self):
        return {'deactivated': db.session.query(Worker).filter(
            Worker.worker_code.like(f'{self.slug}-%'),
            Worker.active_status == False).count()}  # noqa: E712


class Timekeeping(Section):
    key, title, slug = 'timekeeping', 'Timekeeping (attendance)', 'BLKTK'
    model = TimeEntry
    marker_col = None
    pages = (('Timekeeping status', '/hdc/timekeeping/status', 'page'),)

    def create(self, i, code):
        day = (TODAY - timedelta(days=i)).isoformat()
        r = CTX.post('/hdc/timekeeping', {
            'bulk_mode': '1', 'date': day,
            f'status_{TK_WID}': 'present', f'project_id_{TK_WID}': HOST_PID,
            f'stage_id_{TK_WID}': TK_SID, f'working_hours_{TK_WID}': 8,
            f'overtime_hours_{TK_WID}': 0})
        with APP.app_context():
            row = (TimeEntry.query.filter_by(worker_id=TK_WID, is_void=False)
                   .filter(TimeEntry.check_in >= day).first())
            rid = row.id if row else None
        return r, rid

    def edit(self, i, code, rid):
        day = (TODAY - timedelta(days=i)).isoformat()
        return CTX.post(f'/hdc/timekeeping/{rid}/edit', {
            'date': day, 'project_id': HOST_PID, 'stage_id': TK_SID,
            'hours': 9, 'overtime_hours': 1, 'status': 'present'})

    def delete(self, i, code, rid):
        return CTX.post(f'/hdc/timekeeping/{rid}/delete', {'void_reason': 'bulk qa'})

    def count_active(self):
        with APP.app_context():
            return (TimeEntry.query.join(Worker, TimeEntry.worker_id == Worker.id)
                    .filter(Worker.worker_code == TK_CODE, TimeEntry.is_void == False)
                    .count())

    def count_edited(self):
        with APP.app_context():
            return (TimeEntry.query.join(Worker, TimeEntry.worker_id == Worker.id)
                    .filter(Worker.worker_code == TK_CODE, TimeEntry.is_void == False,
                            TimeEntry.hours == 9).count())

    def extra_counts(self):
        with APP.app_context():
            return {'voided': (TimeEntry.query.join(Worker, TimeEntry.worker_id == Worker.id)
                               .filter(Worker.worker_code == TK_CODE,
                                       TimeEntry.is_void == True).count())}


class Expenses(Section):
    key, title, slug = 'expenses', 'Project expenses', 'BLKEXP'
    model = Expense
    marker_col = 'remarks'
    pages = (('Expenses list', '/hdc/expenses', 'page'),)

    def report_urls(self):
        return list(self.pages) + [
            ('Project report', f'/hdc/reports?project_id={HOST_PID}', 'page')]

    def create(self, i, code):
        r = CTX.post('/hdc/expenses', {
            'project_id': HOST_PID, 'stage_id': EXP_SID, 'category_id': EXP_CAT,
            'amount': 1000 + i, 'date': TODAY.isoformat(), 'remarks': code})
        rid = _id(Expense, remarks=code)
        return r, (rid if rid else None)

    def edit(self, i, code, rid):
        return CTX.post(f'/hdc/expenses/{rid}/edit', {
            'project_id': HOST_PID, 'stage_id': EXP_SID, 'category_id': EXP_CAT,
            'amount': 2000 + i, 'date': TODAY.isoformat(), 'remarks': code + '-ED'})

    def delete(self, i, code, rid):
        return CTX.post(f'/hdc/expenses/{rid}/delete', {'void_reason': 'bulk qa'})

    def active_filters(self):
        return (Expense.is_void == False,)  # noqa: E712

    def extra_counts(self):
        return {'voided': db.session.query(Expense).filter(
            Expense.remarks.like(f'{self.slug}-%'), Expense.is_void == True).count()}  # noqa: E712


class OfficeStaffSection(Section):
    key, title, slug = 'office_staff', 'Office staff', 'BLKSTF'
    model = OfficeStaff
    marker_col = 'staff_code'
    pages = (('Office staff list', '/hdc/office-management/staff', 'page'),)

    def create(self, i, code):
        r = CTX.post('/hdc/office-management/staff', {
            'staff_code': code, 'name': code + '-STAFF', 'role_type': 'Clerk',
            'phone': '0300-0000000', 'monthly_salary': 30000})
        rid = _id(OfficeStaff, staff_code=code)
        return r, (rid if rid else None)

    def edit(self, i, code, rid):
        return CTX.post(f'/hdc/office-management/staff/{rid}/edit', {
            'staff_code': code, 'name': code + '-ED', 'role_type': 'Clerk',
            'phone': '0301-1111111', 'monthly_salary': 31000})

    def delete(self, i, code, rid):
        return CTX.post(f'/hdc/office-management/staff/{rid}/toggle')

    def active_filters(self):
        return (OfficeStaff.active_status == True,)  # noqa: E712

    def count_edited(self):
        return self._base().filter(OfficeStaff.name.like(f'{self.slug}-%-ED'),
                                   *self.active_filters()).count()

    def extra_counts(self):
        return {'deactivated': db.session.query(OfficeStaff).filter(
            OfficeStaff.staff_code.like(f'{self.slug}-%'),
            OfficeStaff.active_status == False).count()}  # noqa: E712


class OfficeExpenses(Section):
    key, title, slug = 'office_expenses', 'Office expenses', 'BLKOFF'
    model = OfficeExpense
    marker_col = 'remarks'
    pages = (('Office expenses list', '/hdc/office-management/expenses', 'page'),)

    def create(self, i, code):
        r = CTX.post('/hdc/office-management/expenses', {
            'date': TODAY.isoformat(), 'category': 'Bulk QA',
            'amount': 500 + i, 'remarks': code})
        rid = _id(OfficeExpense, remarks=code)
        return r, (rid if rid else None)

    def edit(self, i, code, rid):
        return CTX.post(f'/hdc/office-management/expenses/{rid}/edit', {
            'date': TODAY.isoformat(), 'category': 'Bulk QA',
            'amount': 900 + i, 'remarks': code + '-ED'})

    def delete(self, i, code, rid):
        return CTX.post(f'/hdc/office-management/expenses/{rid}/delete',
                        {'void_reason': 'bulk qa'})

    def active_filters(self):
        return (OfficeExpense.is_void == False,)  # noqa: E712

    def extra_counts(self):
        return {'voided': db.session.query(OfficeExpense).filter(
            OfficeExpense.remarks.like(f'{self.slug}-%'),
            OfficeExpense.is_void == True).count()}  # noqa: E712


class Suppliers(Section):
    key, title, slug = 'suppliers', 'Suppliers', 'BLKSUP'
    model = Supplier
    marker_col = 'name'
    pages = (
        ('Suppliers page', '/hdc/purchase-v2/suppliers', 'page'),
        ('Suppliers API', '/api/v2/purchase/suppliers', 'json'),
    )

    def create(self, i, code):
        r = CTX.post('/hdc/purchase-v2/suppliers', {
            'name': code, 'phone': '0300-0000000', 'opening_balance': 0,
            'address': 'Bulk'})
        rid = _id(Supplier, name=code)
        return r, (rid if rid else None)

    def edit(self, i, code, rid):
        return CTX.post(f'/hdc/purchase-v2/suppliers/{rid}/edit', {
            'name': code + '-ED', 'phone': '0301-1111111', 'address': 'Bulk ED',
            'status': 'active'})

    def delete(self, i, code, rid):
        res = CTX.api(f'/api/v2/purchase/suppliers/{rid}', 'DELETE')
        return {'ok': res['ok'], 'status': res['status'], 'bad': [] if res['ok'] else [res['body']],
                'warn': []}

    def active_filters(self):
        return (Supplier.is_void == False,)  # noqa: E712


class Materials(Section):
    key, title, slug = 'materials', 'Materials', 'BLKMAT'
    model = MaterialV2
    marker_col = 'name'
    pages = (
        ('Materials page', '/hdc/purchase-v2/materials', 'page'),
        ('Materials API', '/api/v2/purchase/materials', 'json'),
    )

    def create(self, i, code):
        r = CTX.post('/hdc/purchase-v2/materials', {'name': code, 'unit': 'Bag'})
        rid = _id(MaterialV2, name=code)
        return r, (rid if rid else None)

    def edit(self, i, code, rid):
        return CTX.post(f'/hdc/purchase-v2/materials/{rid}/edit',
                        {'name': code + '-ED', 'unit': 'Bag'})

    def delete(self, i, code, rid):
        return CTX.post(f'/hdc/purchase-v2/materials/{rid}/delete')

    def active_filters(self):
        return (MaterialV2.is_void == False,)  # noqa: E712


class Purchases(Section):
    key, title, slug = 'purchases', 'Purchases (V2)', 'BLKPO'
    model = PurchaseV2
    marker_col = 'challan_no'
    pages = (
        ('Purchases page', '/hdc/purchase-v2/purchases', 'page'),
        ('Purchases API', '/api/v2/purchase/purchases', 'json'),
    )

    def create(self, i, code):
        r = CTX.post('/hdc/purchase-v2/purchases', {
            'supplier_id': PO_SUP, 'payment_status': 'unpaid',
            'date': TODAY.isoformat(), 'challan_no': code,
            'material_id[]': [PO_MAT], 'quantity[]': [10],
            'unit_price[]': [100], 'notes[]': ['bulk']})
        rid = _id(PurchaseV2, challan_no=code)
        return r, (rid if rid else None)

    def edit(self, i, code, rid):
        return CTX.post(f'/hdc/purchase-v2/purchases/{rid}/edit', {
            'supplier_id': PO_SUP, 'material_id': PO_MAT, 'quantity': 10,
            'unit_price': 110, 'payment_status': 'unpaid',
            'date': TODAY.isoformat(), 'challan_no': code + '-ED', 'notes': 'bulk'})

    def delete(self, i, code, rid):
        return CTX.post(f'/hdc/purchase-v2/purchases/{rid}/delete',
                        {'void_reason': 'bulk qa'})

    def active_filters(self):
        return (PurchaseV2.is_void == False,)  # noqa: E712


class Tools(Section):
    key, title, slug = 'tools', 'Tools inventory', 'BLKTOOL'
    model = Tool
    marker_col = 'tool_code'
    pages = (
        ('Tool inventory', '/hdc/tool-rental/inventory', 'page'),
        ('Tool reports', '/hdc/tool-rental/reports', 'page'),
    )

    def create(self, i, code):
        r = CTX.post('/hdc/tool-rental/inventory', {
            'action': 'add_tool', 'name': code, 'tool_code': code,
            'category_id': TOOL_CAT, 'total_quantity': 5, 'purchase_cost': 0,
            'rental_rate_per_day': 5, 'unit': 'pcs', 'condition': 'good'})
        rid = _id(Tool, tool_code=code)
        return r, (rid if rid else None)

    def edit(self, i, code, rid):
        return CTX.post(f'/hdc/tool-rental/inventory/{rid}/edit', {
            'name': code + '-ED', 'tool_code': code, 'category_id': TOOL_CAT,
            'total_quantity': 5, 'purchase_cost': 0, 'rental_rate_per_day': 6,
            'unit': 'pcs', 'condition': 'good'})

    def delete(self, i, code, rid):
        return CTX.post(f'/hdc/tool-rental/inventory/{rid}/delete',
                        {'void_reason': 'bulk qa'})

    def active_filters(self):
        return (Tool.is_void == False,)  # noqa: E712

    def count_edited(self):
        return self._base().filter(Tool.name.like(f'{self.slug}-%-ED'),
                                   *self.active_filters()).count()


class Accounts(Section):
    key, title, slug = 'accounts', 'Accounts (money pots)', 'BLKACC'
    model = Account
    marker_col = 'name'
    pages = (
        ('Accounts page', '/hdc/accounts', 'page'),
        ('Accounts balances API', '/api/accounts/list_accounts_with_balances', 'json'),
    )

    def create(self, i, code):
        res = CTX.api('/api/accounts/create_account', 'POST', {
            'name': code, 'account_group': 'company', 'account_mode': 'cash'})
        rid = None
        if isinstance(res['json'], dict):
            rid = (res['json'].get('account') or {}).get('id') or res['json'].get('id')
        rid = rid or _id(Account, name=code)
        return {'ok': res['ok'] and bool(rid), 'status': res['status'],
                'bad': [] if res['ok'] else [res['body']], 'warn': []}, rid

    def edit(self, i, code, rid):
        res = CTX.api(f'/api/accounts/account/{rid}', 'PUT', {'name': code + '-ED'})
        return {'ok': res['ok'], 'status': res['status'],
                'bad': [] if res['ok'] else [res['body']], 'warn': []}

    def delete(self, i, code, rid):
        res = CTX.api(f'/api/accounts/account/{rid}', 'DELETE')
        return {'ok': res['ok'], 'status': res['status'],
                'bad': [] if res['ok'] else [res['body']], 'warn': []}

    def active_filters(self):
        return (Account.is_void == False,)  # noqa: E712


class CashFlow(Section):
    key, title, slug = 'cashflow', 'Cash-flow register', 'BLKCF'
    after_delete_visible = True
    model = CashFlowEntry
    marker_col = 'reference'
    pages = (
        ('Register page', '/hdc/accounts/cashflow/register', 'page'),
        ('Register export', '/hdc/accounts/cashflow/register/export', 'csv'),
    )

    def create(self, i, code):
        r = CTX.post('/hdc/accounts/cashflow/register', {
            'action': 'create_entry', 'direction': 'in', 'amount': 1000 + i,
            'account_id': CF_ACC, 'date': TODAY.isoformat(), 'reference': code,
            'description': code, 'category_id': CF_CAT})
        rid = _id(CashFlowEntry, reference=code)
        return r, (rid if rid else None)

    def edit(self, i, code, rid):
        r = CTX.post('/hdc/accounts/cashflow/register', {
            'action': 'amend_entry', 'entry_id': rid, 'direction': 'in',
            'amount': 1500 + i, 'account_id': CF_ACC, 'date': TODAY.isoformat(),
            'reference': code, 'description': code + '-ED', 'category_id': CF_CAT,
            'reason': 'bulk qa correction'})
        with APP.app_context():
            row = (CashFlowEntry.query
                   .filter_by(description=code + '-ED', is_void=False)
                   .order_by(CashFlowEntry.id.desc()).first())
            new_id = row.id if row else rid
        return r, new_id

    def count_edited(self):
        return self._base().filter(
            CashFlowEntry.description.like(f'{self.slug}-%-ED'),
            *self.active_filters()).count()

    def delete(self, i, code, rid):
        return CTX.post('/hdc/accounts/cashflow/register', {
            'action': 'void_entry', 'entry_id': rid, 'reason': 'bulk qa'})

    def active_filters(self):
        return (CashFlowEntry.is_void == False,)  # noqa: E712

    def extra_counts(self):
        return {'voided': db.session.query(CashFlowEntry).filter(
            CashFlowEntry.reference.like(f'{self.slug}-%'),
            CashFlowEntry.is_void == True).count()}  # noqa: E712


class Shared(Section):
    key, title, slug = 'shared', 'Shared expenses', 'BLKSH'
    model = SharedExpense
    marker_col = 'title'
    pages = (
        ('Shared expense list', '/hdc/accounts/shared/expenses', 'page'),
        ('Shared report CSV', '/hdc/accounts/shared/report.csv', 'csv'),
    )

    def create(self, i, code):
        r = CTX.post('/hdc/accounts/shared/expenses/new', {
            'date': TODAY.isoformat(), 'title': code, 'category': 'Bulk QA',
            'total_amount': 1000, 'payer_party_id': SH_P1, 'split_mode': 'equal',
            'party_ids': [SH_P1, SH_P2], 'money_source': 'none'})
        rid = _id(SharedExpense, title=code)
        return r, (rid if rid else None)

    def edit(self, i, code, rid):
        return CTX.post(f'/hdc/accounts/shared/expenses/{rid}/edit', {
            'date': TODAY.isoformat(), 'title': code + '-ED', 'category': 'Bulk QA',
            'total_amount': 1200, 'payer_party_id': SH_P1, 'split_mode': 'equal',
            'party_ids': [SH_P1, SH_P2], 'money_source': 'none'})

    def delete(self, i, code, rid):
        r1 = CTX.post(f'/hdc/accounts/shared/expenses/{rid}/void',
                      {'void_reason': 'bulk qa'})
        r2 = CTX.post(f'/hdc/accounts/shared/expenses/{rid}/delete')
        return {'ok': r1['ok'] and r2['ok'], 'status': f'{r1["status"]}/{r2["status"]}',
                'bad': r1['bad'] + r2['bad'], 'warn': r1['warn'] + r2['warn']}

    def active_filters(self):
        return (SharedExpense.is_void == False,)  # noqa: E712

    def extra_counts(self):
        return {'voided': db.session.query(SharedExpense).filter(
            SharedExpense.title.like(f'{self.slug}-%'),
            SharedExpense.is_void == True).count()}  # noqa: E712


SECTION_CLASSES = [Parties, Projects, Stages, Workers, Timekeeping, Expenses,
                   OfficeStaffSection, OfficeExpenses, Suppliers, Materials, Purchases,
                   Tools, Accounts, CashFlow, Shared]


# --------------------------------------------------------------------------- #
# setup / fixtures
# --------------------------------------------------------------------------- #

APP = None
CTX = None
TODAY = None
HOST_PID = HOST_SID = TK_WID = TK_SID = TK_CODE = EXP_SID = EXP_CAT = None
PO_SUP = PO_MAT = TOOL_CAT = CF_ACC = CF_CAT = SH_P1 = SH_P2 = None


def bootstrap_fixtures():
    """Create the shared scaffolding the bulk rows hang off."""
    global HOST_PID, HOST_SID, TK_WID, TK_SID, TK_CODE, EXP_SID, EXP_CAT
    global PO_SUP, PO_MAT, TOOL_CAT, CF_ACC, CF_CAT, SH_P1, SH_P2

    r = CTX.post('/hdc/projects/add', {
        'project_code': 'BLKHOST', 'name': 'Bulk QA Host Project',
        'client': 'Bulk QA Client', 'location': 'QA Site', 'total_sqft': 1000,
        'owner_rate': 1000, 'start_date': (TODAY - timedelta(days=1)).isoformat(),
        'status': 'active'})
    host = find_one(Project, project_code='BLKHOST')
    if not host:
        raise SystemExit(f'host project could not be created: {r}')
    HOST_PID = host.id

    CTX.post(f'/hdc/projects/{HOST_PID}/stage/add', {
        'name': 'BLKHOST-STAGE', 'contract_basis': 'Lump Sum',
        'lump_sum_value': 100000, 'status': 'Active'})
    HOST_SID = _id(Stage, name='BLKHOST-STAGE')

    # a dedicated worker + stage for the timekeeping section
    TK_CODE = 'BLKTK-W'
    CTX.post('/hdc/workers', {'worker_code': TK_CODE, 'name': 'Bulk TK Worker',
                              'role_type': 'Mason', 'wage_type': 'daily',
                              'daily_wage': 1000})
    TK_WID = _id(Worker, worker_code=TK_CODE)
    TK_SID = HOST_SID

    CTX.post('/hdc/expense_categories', {'action': 'add_category',
                                         'category_name': 'Bulk QA'})
    EXP_SID = HOST_SID
    EXP_CAT = _id(ExpenseCategory, name='Bulk QA')
    if EXP_CAT is None:
        with APP.app_context():
            EXP_CAT = ExpenseCategory.query.first().id if ExpenseCategory.query.first() else None

    CTX.post('/hdc/purchase-v2/suppliers', {'name': 'BLKPO-SUP', 'opening_balance': 0})
    PO_SUP = _id(Supplier, name='BLKPO-SUP')
    CTX.post('/hdc/purchase-v2/materials', {'name': 'BLKPO-MAT', 'unit': 'Bag'})
    PO_MAT = _id(MaterialV2, name='BLKPO-MAT')

    CTX.post('/hdc/tool-rental/inventory', {'action': 'add_category',
                                            'category_name': 'Bulk QA Tools'})
    with APP.app_context():
        cat = ToolCategory.query.filter_by(name='Bulk QA Tools').first() or ToolCategory.query.first()
        TOOL_CAT = cat.id if cat else None

    with APP.app_context():
        cash = Account.query.filter_by(name='Company Cash').first()
        CF_ACC = cash.id if cash else None
        cat = (CashFlowCategory.query.filter_by(direction='in', name='Other Income').first()
               or CashFlowCategory.query.filter_by(direction='in').first())
        CF_CAT = cat.id if cat else None
    # The treasury account starts empty and blocks overdrafts. Fund it once so
    # 100 expenses + 100 office expenses + the register entries can be posted;
    # the baseline snapshot is taken after this, so it nets out of the report.
    if CF_ACC:
        CTX.api(f'/api/accounts/account/{CF_ACC}', 'PUT', {'opening_balance': 10000000})

    with APP.app_context():
        p1 = SharedParty.query.filter_by(name='Bulk QA Head A').first()
        if not p1:
            p1 = SharedParty(name='Bulk QA Head A', short_code='BQA')
            db.session.add(p1)
            db.session.commit()
        p2 = SharedParty.query.filter_by(name='Bulk QA Head B').first()
        if not p2:
            p2 = SharedParty(name='Bulk QA Head B', short_code='BQB')
            db.session.add(p2)
            db.session.commit()
        SH_P1, SH_P2 = p1.id, p2.id

    if not all([HOST_SID, TK_WID, EXP_CAT, PO_SUP, PO_MAT, CF_ACC, CF_CAT, SH_P1, SH_P2]):
        raise SystemExit('fixture bootstrap incomplete: ' + repr(dict(
            host_sid=HOST_SID, tk=TK_WID, cat=EXP_CAT, sup=PO_SUP,
            mat=PO_MAT, acc=CF_ACC, cfcat=CF_CAT, p1=SH_P1, p2=SH_P2)))


def ledger_snapshot():
    """Counts + balances used to prove the whole run is financially net-zero."""
    with APP.app_context():
        from hdc.services.accounts import _account_balance
        accounts = {a.id: a.name for a in Account.query.all()}
        balances = {name: round(_account_balance(aid), 2) for aid, name in accounts.items()}
        return {
            'account_txns': AccountTransaction.query.count(),
            'cash_flow_entries': CashFlowEntry.query.count(),
            'labour_ledger': LabourLedger.query.count(),
            'expenses': Expense.query.count(),
            'office_expenses': OfficeExpense.query.count(),
            'balances': balances,
        }


# --------------------------------------------------------------------------- #
# phase runners
# --------------------------------------------------------------------------- #


def run_create(section, count):
    res = {'key': section.key, 'title': section.title, 'phase': 'create',
           'attempted': count, 'succeeded': 0, 'failed': [], 'ids': [],
           'verify': {}, 'reports': [], 'seconds': 0.0}
    t0 = time.time()
    for i in range(1, count + 1):
        code = f'{section.slug}-{i:04d}'
        try:
            r, rid = section.create(i, code)
        except Exception as ex:  # noqa: BLE001
            res['failed'].append({'i': i, 'code': code, 'error': repr(ex)[:300]})
            continue
        if r['ok'] and rid:
            res['succeeded'] += 1
            res['ids'].append({'i': i, 'code': code, 'id': rid})
        else:
            res['failed'].append({'i': i, 'code': code,
                                  'status': r.get('status'),
                                  'flash': (r.get('bad') or r.get('warn') or [])[:2],
                                  'body': r.get('body', '')[:200]})
        if i % 10 == 0:
            print(f'    create {section.key}: {i}/{count} '
                  f'({res["succeeded"]} ok, {len(res["failed"])} failed)', flush=True)
    section.ids = [x['id'] for x in res['ids']]
    with APP.app_context():
        try:
            n_active = section.count_active()
            n_edit = section.count_edited()
            extra = section.extra_counts()
        except Exception as ex:  # noqa: BLE001
            n_active, n_edit, extra = -1, -1, {'error': repr(ex)}
    res['verify'] = {'expected': count, 'db_active': n_active,
                     'db_edited_markers': n_edit, 'extra': extra,
                     'ok': n_active == count}
    res['reports'] = section.read_reports(count, None)
    res['seconds'] = round(time.time() - t0, 2)
    print(f'  [{section.key}] create: {res["succeeded"]}/{count} ok, '
          f'db_active={n_active}, reports={sum(1 for x in res["reports"] if x["ok"])}/'
          f'{len(res["reports"])}, {res["seconds"]}s', flush=True)
    return res


def run_edit(section, count, ids):
    res = {'key': section.key, 'title': section.title, 'phase': 'edit',
           'attempted': len(ids), 'succeeded': 0, 'failed': [], 'verify': {},
           'reports': [], 'seconds': 0.0}
    t0 = time.time()
    for n, item in enumerate(ids, 1):
        try:
            out = section.edit(item['i'], item['code'], item['id'])
            if isinstance(out, tuple):
                r, new_id = out
                if new_id:
                    item['id'] = new_id
            else:
                r = out
        except Exception as ex:  # noqa: BLE001
            res['failed'].append({'id': item['id'], 'error': repr(ex)[:300]})
            continue
        if r['ok']:
            res['succeeded'] += 1
        else:
            res['failed'].append({'id': item['id'], 'code': item['code'],
                                  'status': r.get('status'),
                                  'flash': (r.get('bad') or r.get('warn') or [])[:2]})
        if n % 10 == 0:
            print(f'    edit {section.key}: {n}/{len(ids)} ({res["succeeded"]} ok)', flush=True)
    with APP.app_context():
        try:
            n_edit = section.count_edited()
            n_active = section.count_active()
        except Exception as ex:  # noqa: BLE001
            n_edit, n_active = -1, {'error': repr(ex)}
    res['verify'] = {'expected': len(ids), 'db_edited_markers': n_edit,
                     'db_active': n_active, 'ok': n_edit == len(ids)}
    res['reports'] = section.read_reports(len(ids), None)
    res['seconds'] = round(time.time() - t0, 2)
    print(f'  [{section.key}] edit: {res["succeeded"]}/{len(ids)} ok, '
          f'db_edited={n_edit}, {res["seconds"]}s', flush=True)
    return res


def run_delete(section, count, ids):
    res = {'key': section.key, 'title': section.title, 'phase': 'delete',
           'attempted': len(ids), 'succeeded': 0, 'failed': [], 'verify': {},
           'reports': [], 'seconds': 0.0}
    t0 = time.time()
    for n, item in enumerate(ids, 1):
        try:
            r = section.delete(item['i'], item['code'], item['id'])
        except Exception as ex:  # noqa: BLE001
            res['failed'].append({'id': item['id'], 'error': repr(ex)[:300]})
            continue
        if r['ok']:
            res['succeeded'] += 1
        else:
            res['failed'].append({'id': item['id'], 'code': item['code'],
                                  'status': r.get('status'),
                                  'flash': (r.get('bad') or r.get('warn') or [])[:2]})
        if n % 10 == 0:
            print(f'    delete {section.key}: {n}/{len(ids)} ({res["succeeded"]} ok)', flush=True)
    with APP.app_context():
        try:
            n_active = section.count_active()
            extra = section.extra_counts()
        except Exception as ex:  # noqa: BLE001
            n_active, extra = -1, {'error': repr(ex)}
    res['verify'] = {'expected': 0, 'db_active': n_active, 'extra': extra,
                     'ok': n_active == 0}
    res['reports'] = section.read_reports(len(ids), 0)
    res['seconds'] = round(time.time() - t0, 2)
    print(f'  [{section.key}] delete: {res["succeeded"]}/{len(ids)} ok, '
          f'db_active={n_active}, {res["seconds"]}s', flush=True)
    return res


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #


def write_markdown(path, phase, sections, meta):
    lines = [f'# Bulk E2E — {phase} phase', '',
             f'- Run at: {meta["started"]}',
             f'- Records requested per section: {meta["count"]}',
             f'- Database: `{meta["db_path"]}` (disposable)',
             f'- Python {meta["python"]}', '',
             '| Section | Rows | HTTP ok | DB verify | Report reads | Failures | Time |',
             '|---|---|---|---|---|---|---|']
    for r in sections:
        rep = f'{sum(1 for x in r["reports"] if x["ok"])}/{len(r["reports"])}'
        lines.append(
            f'| {r["title"]} (`{r["key"]}`) | {r["attempted"]} | '
            f'{r["succeeded"]}/{r["attempted"]} | '
            f'{"PASS" if r["verify"].get("ok") else "FAIL"} '
            f'({r["verify"].get("db_active")}) | {rep} | '
            f'{len(r["failed"])} | {r["seconds"]}s |')
    fails = [(r['key'], f) for r in sections for f in r['failed']]
    lines += ['', f'**Failures: {len(fails)}**']
    if fails:
        lines += ['', '| Section | Detail |', '|---|---|']
        for key, f in fails[:60]:
            detail = json.dumps({k: v for k, v in f.items() if k != 'body'})[:300]
            lines.append(f'| {key} | `{detail}` |')
        if len(fails) > 60:
            lines.append(f'| … | {len(fails) - 60} more in the JSON file |')
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(lines) + '\n')


def main():
    global APP, CTX, TODAY
    ap = argparse.ArgumentParser()
    ap.add_argument('--count', type=int, default=100)
    ap.add_argument('--sections', default='all')
    ap.add_argument('--out', default=os.path.join(ROOT, 'qa', 'bulk-e2e'))
    ap.add_argument('--keep-db', action='store_true')
    args = ap.parse_args()

    outdir = os.path.abspath(args.out)
    os.makedirs(outdir, exist_ok=True)
    run_dir = os.path.join('/tmp', 'hdc-bulk-e2e')
    os.makedirs(run_dir, exist_ok=True)
    db_path = os.path.join(run_dir, 'bulk.db')
    if not args.keep_db and os.path.exists(db_path):
        os.remove(db_path)
    for suffix in ('-wal', '-shm'):
        p = db_path + suffix
        if os.path.exists(p):
            os.remove(p)

    os.environ.update({'HDC_ENV': 'test', 'HDC_SECRET_KEY': 'bulk-e2e-only',
                       'HDC_BOOTSTRAP_ADMIN_PASSWORD': 'Bulk-Test-Only-123',
                       'HDC_DB_PATH': db_path, 'HDC_INSTANCE_DIR': run_dir,
                       'HDC_DEV_MODE': '0'})
    APP = create_app({'TESTING': True})
    client = APP.test_client()
    TODAY = _pkt_today()

    client.get('/hdc/login')
    with client.session_transaction() as s:
        token = [s['_csrf_token']]
    r = client.post('/hdc/login', data={'username': 'admin',
                                        'password': 'Bulk-Test-Only-123',
                                        '_csrf_token': token[0]})
    with client.session_transaction() as s:
        token[0] = s.get('_csrf_token', token[0])
    CTX = Ctx(client, APP, token)
    if r.status_code != 302:
        raise SystemExit(f'login failed: {r.status_code}')

    wanted = [c for c in SECTION_CLASSES
              if args.sections == 'all' or c.key in args.sections.split(',')]
    if not wanted:
        raise SystemExit(f'no sections matched {args.sections!r}')

    bootstrap_fixtures()
    baseline = ledger_snapshot()

    meta = {'started': time.strftime('%Y-%m-%d %H:%M:%S'),
            'count': args.count, 'db_path': db_path,
            'python': sys.version.split()[0],
            'sections': [c.key for c in wanted],
            'baseline': baseline}

    print(f'\n== create phase: {len(wanted)} sections x {args.count}', flush=True)
    created = {}
    create_rows = []
    for cls in wanted:
        sec = cls()
        created[sec.key] = run_create(sec, args.count)
        create_rows.append(created[sec.key])

    meta_after_create = dict(meta, ledger=ledger_snapshot())
    with open(os.path.join(outdir, 'report-create.json'), 'w', encoding='utf-8') as fh:
        json.dump({'meta': meta_after_create, 'sections': create_rows}, fh, indent=1)
    write_markdown(os.path.join(outdir, 'report-create.md'), 'CREATE', create_rows,
                   meta_after_create)

    print(f'\n== edit phase', flush=True)
    edit_rows = []
    for cls in wanted:
        sec = cls()
        edit_rows.append(run_edit(sec, args.count, created[sec.key]['ids']))

    meta_after_edit = dict(meta, ledger=ledger_snapshot())
    with open(os.path.join(outdir, 'report-edit.json'), 'w', encoding='utf-8') as fh:
        json.dump({'meta': meta_after_edit, 'sections': edit_rows}, fh, indent=1)
    write_markdown(os.path.join(outdir, 'report-edit.md'), 'EDIT', edit_rows,
                   meta_after_edit)

    print(f'\n== delete phase', flush=True)
    delete_rows = []
    for cls in wanted:
        sec = cls()
        delete_rows.append(run_delete(sec, args.count, created[sec.key]['ids']))

    final_ledger = ledger_snapshot()
    meta_after_delete = dict(meta, ledger=final_ledger)
    with open(os.path.join(outdir, 'report-delete.json'), 'w', encoding='utf-8') as fh:
        json.dump({'meta': meta_after_delete, 'sections': delete_rows}, fh, indent=1)
    write_markdown(os.path.join(outdir, 'report-delete.md'), 'DELETE', delete_rows,
                   meta_after_delete)

    # ---- final re-read of every report surface after the deletion pass ---- #
    print(f'\n== final report re-read (post-delete)', flush=True)
    final_rows = []
    for cls in wanted:
        sec = cls()
        final_rows.append({'key': sec.key, 'title': sec.title,
                           'reports': sec.read_reports(args.count, 0),
                           'db_active': dbq(sec.count_active)})

    balance_delta = {}
    for name, val in final_ledger['balances'].items():
        before = baseline['balances'].get(name, 0)
        if abs(val - before) > 0.009:
            balance_delta[name] = {'before': before, 'after': val,
                                   'delta': round(val - before, 2)}
    row_delta = {k: final_ledger[k] - baseline[k] for k in
                 ('account_txns', 'cash_flow_entries', 'labour_ledger',
                  'expenses', 'office_expenses')}

    summary = {
        'meta': dict(meta, finished=time.strftime('%Y-%m-%d %H:%M:%S'),
                     ledger_baseline=baseline, ledger_final=final_ledger),
        'create': {'sections': create_rows,
                   'http_ok': sum(r['succeeded'] for r in create_rows),
                   'attempted': sum(r['attempted'] for r in create_rows)},
        'edit': {'sections': edit_rows,
                 'http_ok': sum(r['succeeded'] for r in edit_rows),
                 'attempted': sum(r['attempted'] for r in edit_rows)},
        'delete': {'sections': delete_rows,
                   'http_ok': sum(r['succeeded'] for r in delete_rows),
                   'attempted': sum(r['attempted'] for r in delete_rows)},
        'final_reports': final_rows,
        'net_effect': {'balance_delta': balance_delta, 'row_delta': row_delta},
    }
    with open(os.path.join(outdir, 'report-final.json'), 'w', encoding='utf-8') as fh:
        json.dump(summary, fh, indent=1)

    lines = ['# Bulk E2E — final summary', '',
             f'Run: {meta["started"]} → {summary["meta"]["finished"]}',
             f'Sections: {len(wanted)} · records per section: {args.count}', '',
             '| Phase | HTTP OK | Attempted |', '|---|---|---|',
             f'| Create | {summary["create"]["http_ok"]} | {summary["create"]["attempted"]} |',
             f'| Edit | {summary["edit"]["http_ok"]} | {summary["edit"]["attempted"]} |',
             f'| Delete | {summary["delete"]["http_ok"]} | {summary["delete"]["attempted"]} |',
             '', '## Per-section result', '',
             '| Section | Created | Edited | Deleted | Active rows after delete | Final report reads |',
             '|---|---|---|---|---|---|']
    for c, e, d_, fr in zip(create_rows, edit_rows, delete_rows, final_rows):
        rep = f'{sum(1 for x in fr["reports"] if x["ok"])}/{len(fr["reports"])}'
        lines.append(f'| {c["title"]} | {c["succeeded"]}/{c["attempted"]} | '
                     f'{e["succeeded"]}/{e["attempted"]} | {d_["succeeded"]}/{d_["attempted"]} | '
                     f'{fr["db_active"]} | {rep} |')
    lines += ['', '## Net effect on the ledger after voiding/deleting every row', '',
              f'- Row-count deltas: `{row_delta}`',
              f'- Account balances differing from baseline: `{balance_delta or "none"}`']
    with open(os.path.join(outdir, 'report-final.md'), 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(lines) + '\n')

    total_failed = sum(len(r['failed']) for r in create_rows + edit_rows + delete_rows)
    verify_failed = [r['key'] for r in create_rows + edit_rows + delete_rows
                     if not r['verify'].get('ok')]
    print('\n==== BULK E2E SUMMARY ====')
    print(f'create {summary["create"]["http_ok"]}/{summary["create"]["attempted"]} · '
          f'edit {summary["edit"]["http_ok"]}/{summary["edit"]["attempted"]} · '
          f'delete {summary["delete"]["http_ok"]}/{summary["delete"]["attempted"]}')
    print(f'http failures: {total_failed} · verify failures: {verify_failed or "none"}')
    print(f'ledger row deltas: {row_delta}')
    print(f'balance deltas: {balance_delta or "none"}')
    print(f'reports written to {outdir}')
    return 1 if (total_failed or verify_failed) else 0


if __name__ == '__main__':
    sys.exit(main())
