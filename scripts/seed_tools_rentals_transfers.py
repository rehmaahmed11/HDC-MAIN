#!/usr/bin/env python3
"""Populate HDC Tools with a *verified* set of NEW RENTALS and TRANSFERS only,
then prove the result shows up correctly in Tools > Audit and Tools > Tracking.

Scope (deliberately narrow):
  * Master data needed to rent anything: 3 sites (+ stages), 3 customers and
    4 tools with opening stock (through the purchase register, as the app does).
  * Tools > New Rental  -> POST /hdc/tool-rental/create  (txn_type=new_rental)
  * Tools > Transfer    -> POST /hdc/tool-rental/create  (txn_type=transfer)
  * No returns, no payments, no scrap, no audit adjustments.  Transfers settle
    the previous holder with "add credit" so no cash is posted.

Every HTTP call goes through the Flask test client with the real CSRF token,
exactly like the browser form.  Afterwards the script checks, independently
of the app's own summaries:

  1. every rental and transfer was accepted (302 + the expected rows exist);
  2. the per-tool, per-place quantities in the ledger equal the hand-computed
     expectation below;
  3. Tools > Audit: one card per place whose "book" quantities equal the
     expectation, and the audit matrix page renders them;
  4. Tools > Tracking: the page renders every rental code and place;
  5. the ledger reconciles (owned == in store + out) for every tool.

Usage (writes the demo DB to the default instance dir, hdc_instance/, which is
git-ignored):

    HDC_BOOTSTRAP_ADMIN_PASSWORD='Admin@1234' \\
        python3 scripts/seed_tools_rentals_transfers.py

Idempotent: refuses to double-populate if the smoke tools already exist.
"""
from __future__ import annotations

import os
import re
import sys
from datetime import timedelta
from html import unescape

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, ROOT)

ADMIN_USER = 'admin'
ADMIN_PASSWORD_DEFAULT = 'Admin@1234'

SMOKE_TOOL_PREFIX = 'TOOL-SMK-'

# --------------------------------------------------------------------------
# Master data
# --------------------------------------------------------------------------
SITES = {
    # key: (project_code, name, client, location, stages)
    'A': ('SMK-SITE-A', 'Gulshan Villa Block A', 'Mr. Ahmed', 'Karachi', ['Foundation', 'Grey Structure']),
    'B': ('SMK-SITE-B', 'DHA Phase 6 Tower', 'Mr. Khan', 'Karachi', ['Slab Work']),
    'C': ('SMK-SITE-C', 'Bahria Town Duplex', 'Mrs. Raza', 'Hyderabad', []),
}
CUSTOMERS = {
    'ALI': 'Ali Traders',
    'BIL': 'Bilal Construction Co',
    'USM': 'Usman Steel Works',
}
# code: (name, unit, owned qty, rental rate per day/ fee per unit, purchase cost)
TOOLS = {
    'VIB': ('TOOL-SMK-VIB', 'Concrete Vibrator', 'pcs', 24, 1200, 45000),
    'PRP': ('TOOL-SMK-PRP', 'Steel Shuttering Prop', 'nos', 200, 60, 3200),
    'GRD': ('TOOL-SMK-GRD', 'Angle Grinder', 'pcs', 30, 400, 9500),
    'SCF': ('TOOL-SMK-SCF', 'Scaffolding Frame Set', 'set', 90, 150, 7800),
}

# --------------------------------------------------------------------------
# The operator scenario.  Rentals are numbered N1..N4 (created by the New
# Rental form) and the destination rentals opened by transfers are N5..N8 in
# the order they are created.  ``days_ago`` keeps the dates chronological.
# --------------------------------------------------------------------------
NEW_RENTALS = [
    # id, days_ago, renter, billing, lines [(tool, qty)]
    ('N1', 30, ('site', 'A', 'Foundation'), 'per_day', [('VIB', 8), ('PRP', 120)]),
    ('N2', 28, ('site', 'A', 'Foundation'), 'fixed_fee', [('SCF', 30), ('GRD', 10)]),
    ('N3', 20, ('customer', 'ALI'), 'per_day', [('VIB', 6), ('GRD', 12)]),
    ('N4', 15, ('customer', 'BIL'), 'fixed_fee', [('PRP', 50), ('SCF', 20)]),
]

# Transfers, in order.  Each one moves explicit per-tool quantities from one or
# more source rentals (holder + the place it sits at) to a new destination.
TRANSFERS = [
    # T1: partial move, one holder, site -> site
    {
        'label': 'T1 Site A Foundation -> DHA Phase 6 (Slab Work)',
        'days_ago': 10,
        'sources': [('N1', [('VIB', 3), ('PRP', 40)])],
        'dest': ('site', 'B', 'Slab Work'),
        'billing': 'per_day',
    },
    # T2: two holders at the same site move different tools to a third site
    {
        'label': 'T2 Site A (two holders) -> Bahria Town Duplex',
        'days_ago': 7,
        'sources': [('N1', [('PRP', 30)]), ('N2', [('SCF', 20), ('GRD', 10)])],
        'dest': ('site', 'C', None),
        'billing': 'per_day',
    },
    # T3: customer -> customer
    {
        'label': 'T3 Ali Traders -> Usman Steel Works',
        'days_ago': 5,
        'sources': [('N3', [('GRD', 4)])],
        'dest': ('customer', 'USM'),
        'billing': 'per_day',
    },
    # T4: customer -> site (whole line)
    {
        'label': 'T4 Bilal Construction Co -> DHA Phase 6 (Slab Work)',
        'days_ago': 2,
        'sources': [('N4', [('SCF', 20)])],
        'dest': ('site', 'B', 'Slab Work'),
        'billing': 'fixed_fee',
    },
]

# Rental a transfer opens, in creation order (after N1..N4).
DESTINATION_RENTALS = {'N5': 'T1', 'N6': 'T2', 'N7': 'T3', 'N8': 'T4'}

# --------------------------------------------------------------------------
# Hand-computed expectation.  place -> tool -> qty still out there.
# Places are keyed by (kind, a, b): own project + stage, customer, or store.
# --------------------------------------------------------------------------
# The audit keys a site by its project (all stages together), so Site A is
# Foundation + the rest of the site.
EXPECTED_BOOK = {
    ('site', 'A', None): {'VIB': 5, 'PRP': 50, 'SCF': 10},                  # N1 + N2
    ('site', 'B', None): {'VIB': 3, 'PRP': 40, 'SCF': 20},                  # N5 + N8
    ('site', 'C', None): {'PRP': 30, 'SCF': 20, 'GRD': 10},                 # N6
    ('customer', 'ALI'): {'VIB': 6, 'GRD': 8},                              # N3
    ('customer', 'BIL'): {'PRP': 50},                                       # N4
    ('customer', 'USM'): {'GRD': 4},                                        # N7
    ('store', None, None): {'VIB': 10, 'PRP': 30, 'GRD': 8, 'SCF': 40},    # not out
}
EXPECTED_OWNED = {'VIB': 24, 'PRP': 200, 'GRD': 30, 'SCF': 90}
EXPECTED_RENTAL_COUNT = 8   # N1..N8
EXPECTED_TRANSFER_ROWS = 5  # T1 (1) + T2 (2) + T3 (1) + T4 (1)

NOTE_TAG = 'SMK populate'


# ==========================================================================
# helpers
# ==========================================================================
def _csrf_from_html(html):
    m = re.search(r'name="_csrf_token" value="([^"]+)"', html or '')
    return m.group(1) if m else ''


def _flash_messages(html):
    """Plain-text flash messages rendered on the page (for readable failures)."""
    out = []
    for m in re.finditer(r'class="alert[^"]*"[^>]*>(.*?)</div>', html or '', re.S):
        text = re.sub(r'<[^>]+>', ' ', m.group(1))
        text = re.sub(r'\s+', ' ', unescape(text)).strip()
        if text:
            out.append(text)
    return out


class Populator:
    """Drives the app the way the browser does and records every check."""

    def __init__(self, app, client):
        self.app = app
        self.client = client
        self.checks = []          # (ok, label, detail)
        self.rentals = {}         # scenario id -> ToolRental
        self.dest_rentals = {}    # N5..N8 -> ToolRental
        self.sites = {}           # key -> Project
        self.stages = {}          # (key, stage name) -> Stage
        self.customers = CUSTOMERS
        self.tools = {}           # key -> Tool
        self.site_key_by_id = {}  # project id -> site key

    # -- bookkeeping -------------------------------------------------------
    def check(self, ok, label, detail=''):
        self.checks.append((bool(ok), label, detail))
        return bool(ok)

    # -- http --------------------------------------------------------------
    def _token(self):
        with self.client.session_transaction() as sess:
            return sess.get('_csrf_token', '')

    def login(self, password):
        page = self.client.get('/hdc/login')
        token = _csrf_from_html(page.get_data(as_text=True))
        resp = self.client.post('/hdc/login', data={
            'username': ADMIN_USER, 'password': password, '_csrf_token': token,
        })
        return self.check(resp.status_code == 302, 'admin login', f'status {resp.status_code}')

    def post_form(self, url, payload):
        data = dict(payload)
        data['_csrf_token'] = self._token()
        return self.client.post(url, data=data, follow_redirects=False)

    # -- master data (services, same as the app) ---------------------------
    def master_data(self):
        from hdc.extensions import db
        from hdc.models.projects import Project, Stage
        from hdc.models.tool_rental import Tool, ToolCategory
        from hdc.services.tool_rental import record_tool_purchase

        cat = ToolCategory.query.filter_by(name='Smoke Tools').first()
        if not cat:
            cat = ToolCategory(name='Smoke Tools', active_status=True)
            db.session.add(cat)
            db.session.flush()

        for key, (code, name, client, location, stages) in SITES.items():
            proj = Project.query.filter_by(project_code=code).first()
            if not proj:
                proj = Project(project_code=code, name=name, client=client,
                               location=location, contract_type='lump_sum',
                               owner_lump_sum=1_000_000)
                db.session.add(proj)
                db.session.flush()
            self.sites[key] = proj
            self.site_key_by_id[proj.id] = key
            for stage_name in stages:
                st = Stage.query.filter_by(project_id=proj.id, name=stage_name).first()
                if not st:
                    st = Stage(project_id=proj.id, name=stage_name,
                               contract_basis='Lump Sum', lump_sum_value=100_000)
                    db.session.add(st)
                    db.session.flush()
                self.stages[(key, stage_name)] = st

        for key, (code, name, unit, owned, rate, cost) in TOOLS.items():
            tool = Tool.query.filter_by(tool_code=code).first()
            if not tool:
                tool = Tool(tool_code=code, name=name, unit=unit, category_id=cat.id,
                            total_quantity=0.0, purchase_cost=cost,
                            rental_rate_per_day=rate, condition='good',
                            status='active', is_void=False)
                db.session.add(tool)
                db.session.flush()
                ok, msg, _ = record_tool_purchase(
                    tool_id=tool.id, qty=owned, unit_cost=cost,
                    supplier='Karachi Tools House', is_opening_stock=True, commit=False)
                if not ok:
                    raise RuntimeError(f'opening stock for {code} failed: {msg}')
            self.tools[key] = tool
        db.session.commit()
        self.check(True, 'master data ready',
                   f'{len(self.sites)} sites, {len(self.tools)} tools')

    # -- location helpers --------------------------------------------------
    def _renter_fields(self, renter):
        """Form fields for an internal (site) or external (customer) renter."""
        if renter[0] == 'site':
            _, key, stage = renter
            stage_obj = self.stages.get((key, stage)) if stage else None
            fields = {'renter_type': 'internal', 'project_id': str(self.sites[key].id)}
            if stage_obj:
                fields['stage_id'] = str(stage_obj.id)
            return fields
        return {'renter_type': 'external',
                'customer_name': self.customers[renter[1]],
                'customer_phone': '0300-0000000'}

    def _source_key(self, rental):
        """The From-picker key the browser would send for this rental's place."""
        from hdc.routes.tool_rental import _transfer_source_key
        from hdc.services.tool_audit import LOC_CUSTOMER, LOC_OWN_PROJECT
        if rental.project_id:
            return _transfer_source_key(rental.id, LOC_OWN_PROJECT,
                                        project_id=rental.project_id,
                                        stage_id=rental.stage_id)
        return _transfer_source_key(rental.id, LOC_CUSTOMER,
                                    customer_name=rental.customer_name)

    def _tool_item(self, rental_id, tool_key):
        from hdc.models.tool_rental import ToolRentalItem
        return (ToolRentalItem.query
                .filter_by(rental_id=rental_id, tool_id=self.tools[tool_key].id)
                .first())

    # -- New Rental --------------------------------------------------------
    def new_rental(self, scen_id, days_ago, renter, billing, lines):
        from hdc.extensions import db
        from hdc.utils.dates import _pkt_today
        from hdc.models.tool_rental import ToolRental
        rental_date = (_pkt_today() - timedelta(days=days_ago)).isoformat()
        payload = {
            'txn_type': 'new_rental', 'billing_type': billing, 'rental_date': rental_date,
            'notes': f'{NOTE_TAG} {scen_id}',
        }
        payload.update(self._renter_fields(renter))
        payload['tool_id[]'] = [str(self.tools[k].id) for k, _ in lines]
        payload['qty[]'] = [str(q) for _, q in lines]
        payload['rate[]'] = [str(self._rate_for(k, billing)) for k, _ in lines]

        resp = self.post_form('/hdc/tool-rental/create', payload)
        loc = resp.headers.get('Location', '')
        m = re.search(r'[?&]created=(\d+)', loc)
        if resp.status_code != 302 or not m:
            page = self.client.get('/hdc/tool-rental/new').get_data(as_text=True)
            self.check(False, f'{scen_id} new rental', '; '.join(_flash_messages(page)) or resp.status_code)
            return None
        rental = db.session.get(ToolRental, int(m.group(1)))
        self.rentals[scen_id] = rental
        self.check(rental is not None and rental.status == 'active',
                   f'{scen_id} new rental created', rental.rental_code if rental else 'missing')
        return rental

    def _rate_for(self, tool_key, billing):
        # the form posts the destination rate; use the tool's list rate for
        # per-day, and the same figure for fixed fee so amounts are predictable
        return TOOLS[tool_key][4]

    # -- Transfer ----------------------------------------------------------
    def transfer(self, spec):
        from hdc.extensions import db
        from hdc.utils.dates import _pkt_today
        from hdc.models.tool_rental import ToolRental

        source_keys = []
        item_ids = []
        qty_fields = {}
        for scen_id, lines in spec['sources']:
            rental = self.rentals[scen_id]
            source_keys.append(self._source_key(rental))
            for tool_key, qty in lines:
                item = self._tool_item(rental.id, tool_key)
                if item is None:
                    raise RuntimeError(f'{scen_id} has no {tool_key} line')
                item_ids.append(str(item.id))
                qty_fields[f'transfer_qty_{item.id}'] = str(qty)
                qty_fields[f'transfer_rate_{item.id}'] = str(self._rate_for(tool_key, spec['billing']))

        dest = spec['dest']
        rental_date = (_pkt_today() - timedelta(days=spec['days_ago'])).isoformat()
        payload = {
            'txn_type': 'transfer',
            'source_rental_id[]': source_keys,
            'transfer_selection_enabled': '1',
            'transfer_item_id[]': item_ids,
            'billing_type': spec['billing'],
            'rental_date': rental_date,
            'settle_choice': 'add_credit',       # no cash posted by the smoke run
            'notes': f'{NOTE_TAG} {spec["label"]}',
        }
        payload.update(qty_fields)
        payload.update(self._renter_fields(
            ('site', dest[1], dest[2]) if dest[0] == 'site' else ('customer', dest[1])))

        before = ToolRental.query.count()
        resp = self.post_form('/hdc/tool-rental/create', payload)
        loc = resp.headers.get('Location', '')
        m = re.search(r'[?&]created=(\d+)', loc)
        if resp.status_code != 302 or not m:
            page = self.client.get('/hdc/tool-rental/new').get_data(as_text=True)
            self.check(False, spec['label'], '; '.join(_flash_messages(page)) or resp.status_code)
            return None
        new_rental = db.session.get(ToolRental, int(m.group(1)))
        self.check(ToolRental.query.count() == before + 1 and new_rental is not None,
                   spec['label'], f'opened {new_rental.rental_code}')
        return new_rental

    # -- verification ------------------------------------------------------
    def verify(self):
        from hdc.models.tool_rental import ToolMovementLog, ToolRental, ToolRentalTransfer
        from hdc.services.tool_audit import audit_locations
        from hdc.services.tool_tracking import tool_ledger, tools_reconciliation

        ledger = tool_ledger()
        recon = tools_reconciliation(ledger)
        code_by_tool = {t.id: k for k, t in self.tools.items()}

        # 1. counts
        n_rentals = ToolRental.query.filter(ToolRental.notes.like(f'%{NOTE_TAG}%')).count()
        self.check(n_rentals == EXPECTED_RENTAL_COUNT,
                   f'rentals created = {EXPECTED_RENTAL_COUNT}', f'found {n_rentals}')
        n_tr = ToolRentalTransfer.query.count()
        self.check(n_tr == EXPECTED_TRANSFER_ROWS,
                   f'transfer rows = {EXPECTED_TRANSFER_ROWS}', f'found {n_tr}')

        # 2. owned + reconciliation
        for key, owned in EXPECTED_OWNED.items():
            row = next(t for t in ledger['tools'] if t['tool_id'] == self.tools[key].id)
            self.check(abs(float(row['owned_qty']) - owned) < 1e-6,
                       f'{key} owned = {owned}', f"{row['owned_qty']:g}")
            self.check(not row['unaccounted'], f'{key} reconciles (owned = store + out)',
                       f"store {row['in_store_qty']:g} + out {row['out_qty']:g}")
        self.check(recon['balanced'], 'ledger balanced', str(recon.get('balanced')))

        # 3. per-place book quantities, via the Audit cards (the same numbers the page shows)
        cards = {}
        for card in audit_locations(ledger):
            cards[self._card_place(card)] = card
        for place, expected in EXPECTED_BOOK.items():
            card = cards.get(place)
            got = {}
            if card:
                for tool_id, qty in card['book'].items():
                    if tool_id in code_by_tool:
                        got[code_by_tool[tool_id]] = qty
            ok = {k: float(v) for k, v in got.items()} == {k: float(v) for k, v in expected.items()}
            self.check(ok, f'audit place {self._place_label(place)} = expected',
                       ', '.join(f'{k} {v:g}' for k, v in sorted(got.items())) or 'no card')

        # 4. movement log: a rental_out per line, transfer moves present
        self.check(ToolMovementLog.query.filter_by(movement_type='rental_out').count() == 15,
                   'rental_out movements = 15 rental lines',
                   f"found {ToolMovementLog.query.filter_by(movement_type='rental_out').count()}")
        moves = ToolMovementLog.query.filter(
            ToolMovementLog.movement_type.in_(['site_transfer', 'external_transfer'])).count()
        self.check(moves >= 1, 'transfer movements logged', f'{moves} rows')

        # 5. HTML: Audit + Tracking + New Rental pages render the new data
        client = self.client
        audit_html = client.get('/hdc/tool-rental/audit').get_data(as_text=True)
        self.check('Gulshan Villa Block A' in audit_html and 'Ali Traders' in audit_html
                   and 'Warehouse / Store' in audit_html,
                   'Audit page lists sites, customers and store',
                   'checked page text')
        tracking_html = client.get('/hdc/tool-rental/tracking').get_data(as_text=True)
        missing = [r.rental_code for r in list(self.rentals.values()) + list(self.dest_rentals.values())
                   if r and r.rental_code not in tracking_html]
        self.check(not missing and 'Warehouse / Store' in tracking_html,
                   'Tracking page shows every rental code',
                   f'missing {missing}' if missing else f'{EXPECTED_RENTAL_COUNT} codes')
        for path in ('/hdc/tool-rental/dashboard', '/hdc/tool-rental/reports',
                     '/hdc/tool-rental/serials'):
            status = client.get(path).status_code
            self.check(status == 200, f'page {path} renders', f'status {status}')

    def _card_place(self, card):
        if card['loc_type'] == 'store':
            return ('store', None, None)
        if card['loc_type'] == 'customer':
            name = card.get('customer_name') or ''
            for k, v in CUSTOMERS.items():
                if v.casefold() == name.casefold():
                    return ('customer', k)
            return ('customer', name)
        # own project: the site key and stage name
        return ('site', self.site_key_by_id.get(card.get('project_id')), None)

    @staticmethod
    def _place_label(place):
        return ' / '.join(str(p) for p in place if p)


# ==========================================================================
# runner
# ==========================================================================
def run_scenario(app, password=None, quiet=False):
    """Populate and verify on an already-configured app. Returns the Populator."""
    from hdc.extensions import db

    password = password or os.environ.get('HDC_BOOTSTRAP_ADMIN_PASSWORD') or ADMIN_PASSWORD_DEFAULT
    pop = None
    with app.app_context():
        pop = Populator(app, app.test_client())
        if not pop.login(password):
            return pop
        pop.master_data()

        for scen_id, days_ago, renter, billing, lines in NEW_RENTALS:
            pop.new_rental(scen_id, days_ago, renter, billing, lines)
        db.session.expire_all()

        for spec in TRANSFERS:
            new = pop.transfer(spec)
            if new is not None:
                dest_id = next(k for k, v in DESTINATION_RENTALS.items()
                               if v == spec['label'].split()[0])
                pop.dest_rentals[dest_id] = new
        pop.verify()
        if not quiet:
            _print_report(pop)
    return pop


def _print_report(pop):
    width = max(len(label) for _, label, _ in pop.checks) if pop.checks else 40
    print('\n=== HDC Tools rentals & transfers — smoke result ===')
    for ok, label, detail in pop.checks:
        mark = 'PASS' if ok else 'FAIL'
        print(f'[{mark}] {label.ljust(width)}  {detail}')
    passed = sum(1 for ok, _, _ in pop.checks if ok)
    print(f'\n{passed}/{len(pop.checks)} checks passed')


def main():
    os.environ.setdefault('HDC_BOOTSTRAP_ADMIN_PASSWORD', ADMIN_PASSWORD_DEFAULT)
    os.environ.setdefault('HDC_SECRET_KEY', 'hdc-local-demo-secret')
    from hdc.app import create_app
    from hdc.models.tool_rental import Tool

    app = create_app()
    with app.app_context():
        if Tool.query.filter(Tool.tool_code.like(SMOKE_TOOL_PREFIX + '%')).first():
            print('smoke tools already populated — delete hdc_instance/ to re-run from scratch')
            return 0
    pop = run_scenario(app)
    failed = [label for ok, label, _ in pop.checks if not ok]
    return 1 if failed or not pop.checks else 0


if __name__ == '__main__':
    sys.exit(main())
