#!/usr/bin/env python3
"""Static DOM audit of every rendered HDC page (CSS/UI contract checks).

Fetches every GET page as an authenticated admin and reports:
  * tables not wrapped in .table-responsive
  * form controls with no accessible label
  * icon-only buttons with no accessible name
  * inline styles carrying hardcoded (theme-breaking) colours
  * long <select> lists with no searchable combo pair
  * free-text NAME fields with no searchable combo pair  (the "one rule")
"""
import json
import os
import re
import sys
import tempfile
from html.parser import HTMLParser

os.environ.setdefault('HDC_ENV', 'test')
os.environ.setdefault('HDC_SECRET_KEY', 'audit-only')
os.environ.setdefault('HDC_BOOTSTRAP_ADMIN_PASSWORD', 'Admin@1234')
# Defaults point at a throwaway database so the audit is safe to run anywhere;
# set HDC_DB_PATH to audit a real (seeded) database instead.
os.environ.setdefault('HDC_INSTANCE_DIR', tempfile.gettempdir() + '/hdc_css_audit')
os.environ.setdefault('HDC_DB_PATH', tempfile.gettempdir() + '/hdc_css_audit/audit.db')

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from hdc.app import create_app  # noqa: E402

NAME_RE = re.compile(
    r'(customer|supplier|vendor|party|worker|subcontractor|staff|employee|person|'
    r'contact|renter|contractor|beneficiary|client|owner|name)', re.I)
COMBO_INPUT_RE = re.compile(r'id="([A-Za-z0-9_\-]*[Ii]nput)"')
HARD_HEX_RE = re.compile(r'#[0-9a-fA-F]{3,6}\b')


class Dom(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.tables = []            # (in_responsive, )
        self.controls = []          # dicts
        self.icon_buttons = []
        self.inline_styles = []
        self.selects = []
        self.combo_inputs = set()
        self.text_inputs = []
        self._cur_control = None
        self._cur_select = None
        self._cur_button = None
        self._button_text = ''
        self.labels = {}            # for= -> text
        self._in_label_for = None
        self._label_text = ''
        self._labelled_parents = set()   # ids of stack frames holding a <label>

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get('class', '')
        self.stack.append([tag, cls, False])
        if tag == 'label':
            self._in_label_for = a.get('for')
            self._label_text = ''
            # A <label> that wraps or sits beside the control counts: mark the
            # element that *contains* the label (the label's own parent frame).
            if len(self.stack) >= 2:
                self.stack[-2][2] = True
        if tag in ('input', 'select', 'textarea'):
            typ = (a.get('type') or 'text').lower()
            if tag == 'select':
                typ = 'select'
            self._cur_control = {
                'tag': tag, 'type': typ, 'name': a.get('name', ''),
                'id': a.get('id', ''), 'class': cls,
                'placeholder': a.get('placeholder', ''),
                'aria': a.get('aria-label', '') or a.get('title', ''),
                'required': 'required' in a,
                'text': '',
                'labelled': len(self.stack) >= 2 and self.stack[-2][2],
            }
            if tag == 'select':
                self._cur_select = {'options': 0, 'id': a.get('id', ''),
                                    'name': a.get('name', ''), 'class': cls}
            if tag == 'input':
                # <input> is a void element: there is no end tag to close it.
                self.controls.append(self._cur_control)
                self._cur_control = None
        if tag == 'option' and self._cur_select is not None:
            self._cur_select['options'] += 1
        if tag == 'table':
            self.tables.append({'responsive': 'table-responsive' in cls or
                                any('table-responsive' in f[1] for f in self.stack[:-1])})
        if tag == 'button':
            self._cur_button = {'class': cls, 'title': a.get('title', ''),
                                'aria': a.get('aria-label', ''), 'type': a.get('type', '')}
            self._button_text = ''
        if 'style' in a:
            self.inline_styles.append({'style': a['style'], 'hex': HARD_HEX_RE.findall(a['style'])})
        if tag == 'input' and re.search(r'input$', a.get('id', ''), re.I):
            self.combo_inputs.add(a.get('id', ''))

    def handle_endtag(self, tag):
        if self.stack:
            self.stack.pop()
        if tag == 'label' and self._in_label_for:
            self.labels[self._in_label_for] = self._label_text.strip()
            self._in_label_for = None
        if tag in ('input', 'br', 'img', 'hr') and self._cur_control is not None and tag == 'input':
            self.controls.append(self._cur_control)
            self._cur_control = None
        if tag in ('select', 'textarea') and self._cur_control is not None:
            self.controls.append(self._cur_control)
            self._cur_control = None
        if tag == 'select' and self._cur_select is not None:
            self.selects.append(self._cur_select)
            self._cur_select = None
        if tag == 'button' and self._cur_button is not None:
            self._cur_button['text'] = self._button_text.strip()
            self.icon_buttons.append(self._cur_button)
            self._cur_button = None

    def handle_data(self, data):
        if self._cur_control is not None:
            self._cur_control['text'] += data
        if self._in_label_for is not None:
            self._label_text += data
        if self._cur_button is not None:
            self._button_text += data


def _param_models():
    """param name -> model class, used to turn /hdc/x/<int:id> into a real URL."""
    from hdc.models.projects import Project, Stage, StageDrawing
    from hdc.models.tool_rental import ToolRental, Tool
    from hdc.models.accounts import Account, AccountTransaction
    from hdc.models.workforce import Worker, TimeEntry, PayrollRun
    from hdc.models.subcontract import Subcontractor
    from hdc.models.office import (OfficeStaff, OfficeStaffLedger, AllowanceCategory,
                                   StaffAllowance, OfficeExpenseCategory, OfficeExpense)
    from hdc.models.materials import Supplier, Material, MaterialUsage
    from hdc.models.shared_expenses import SharedParty, SharedExpense
    from hdc.models.projects import CustomFormula
    from hdc.models.materials import PurchaseV2, Delivery, UsageLogV2, MaterialV2
    from hdc.models.accounts import Expense
    return {
        'pid': Project, 'project_id': Project,
        'stage_id': Stage, 'did': StageDrawing,
        'rental_id': ToolRental, 'tool_id': Tool, 'category_id': None,
        'account_id': Account, 'txn_id': AccountTransaction,
        'worker_id': Worker, 'tid': TimeEntry, 'wid': Worker, 'run_id': PayrollRun,
        'sub_id': Subcontractor,
        'sid': OfficeStaff, 'staff_id': OfficeStaff, 'lid': OfficeStaffLedger,
        'aid': StaffAllowance, 'cid': AllowanceCategory,
        'supplier_id': Supplier, 'mid': Material, 'material_id': MaterialV2,
        'usage_id': UsageLogV2, 'delivery_id': Delivery,
        'purchase_id': PurchaseV2,
        'expense_id': SharedExpense, 'party_id': SharedParty,
        'eid': Expense, 'fid': CustomFormula,
    }


def resolve_pages(app):
    """Every GET /hdc page we can build a concrete URL for."""
    models = None
    pages = []
    with app.app_context():
        models = _param_models()
        cache = {}

        def first_id(model):
            if model is None:
                return None
            if model not in cache:
                cache[model] = (model.query.first().id
                                if model.query.first() is not None else None)
            return cache[model]

        for rule in app.url_map.iter_rules():
            path = str(rule)
            if 'static' in path or not path.startswith('/hdc'):
                continue
            if 'GET' not in rule.methods:
                continue
            args = re.findall(r'<(?:string|int|path|float)?(?::(\w+))?>', path)
            if any(a in ('metric', 'scope', 'head', 'filename') for a in args):
                continue
            ok = True
            for arg in args:
                if arg in ('oid',):
                    ok = False
                    break
                model = models.get(arg, 'skip')
                if model == 'skip':
                    ok = False
                    break
                val = first_id(model)
                if val is None:
                    ok = False
                    break
                path = re.sub(r'<[^>]*%s>' % re.escape(arg), str(val), path, count=1)
            if ok:
                pages.append(path)
    return sorted(set(pages))


def audit(app):
    client = app.test_client()
    client.get('/hdc/login')
    with client.session_transaction() as s:
        tok = s['_csrf_token']
    r = client.post('/hdc/login', data={'username': 'admin',
                                        'password': os.environ['HDC_BOOTSTRAP_ADMIN_PASSWORD'],
                                        '_csrf_token': tok})
    assert r.status_code == 302, r.status_code
    return client, resolve_pages(app)


def main():
    app = create_app({'TESTING': True})
    client, pages = audit(app)
    report = {}
    for url in pages:
        try:
            resp = client.get(url)
        except Exception as exc:  # noqa: BLE001
            report[url] = {'error': str(exc)}
            continue
        if resp.status_code != 200:
            report[url] = {'status': resp.status_code}
            continue
        raw = resp.get_data()
        try:
            html = raw.decode('utf-8')
        except UnicodeDecodeError:
            html = raw.decode('utf-8', 'replace')
        d = Dom()
        d.feed(html)
        unlabelled = []
        for c in d.controls:
            if c['type'] in ('hidden', 'submit', 'button', 'file', 'checkbox', 'radio'):
                continue
            lbl = d.labels.get(c['id'], '') if c['id'] else ''
            if not (lbl or c['aria'] or c['text'].strip() or c['placeholder'] or c['labelled']):
                unlabelled.append(c['name'] or c['id'] or c['type'])
        icon_only = [b for b in d.icon_buttons
                     if not b['text'] and not b['title'] and not b['aria']]
        hexes = sorted({h for s in d.inline_styles for h in s['hex']})
        long_selects = [s for s in d.selects if s['options'] > 12]
        name_free = []
        for c in d.controls:
            if c['tag'] != 'input' or c['type'] in ('hidden', 'submit', 'date', 'number', 'checkbox'):
                continue
            text = ' '.join([c['name'], c['placeholder'],
                             d.labels.get(c['id'], '') if c['id'] else '', c['text']])
            if NAME_RE.search(c['name']) and NAME_RE.search(text) and c['id'] not in d.combo_inputs:
                name_free.append({'name': c['name'], 'id': c['id'],
                                  'label': (d.labels.get(c['id'], '') if c['id'] else '') or c['placeholder']})
        report[url] = {
            'tables': len(d.tables),
            'tables_unwrapped': sum(1 for t in d.tables if not t['responsive']),
            'controls': len(d.controls),
            'unlabelled_controls': sorted(set(unlabelled)),
            'icon_buttons_no_name': len(icon_only),
            'inline_styles': len(d.inline_styles),
            'hardcoded_hex': hexes,
            'long_selects': [{'id': s['id'], 'name': s['name'], 'options': s['options']}
                             for s in long_selects],
            'combo_inputs': sorted(d.combo_inputs),
            'name_fields_without_combo': name_free,
        }
    print(json.dumps(report, indent=1))
    with open('/tmp/hdc_dom_audit.json', 'w') as fh:
        json.dump(report, fh, indent=1)


if __name__ == '__main__':
    main()
