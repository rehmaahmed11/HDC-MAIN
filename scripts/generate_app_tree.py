#!/usr/bin/env python3
"""Generate APP_TREE.md: the full HDC ERP screen tree, from Dashboard to the last layer.

Read-only documentation tool. It builds a throw-away database in a temp
directory, seeds a little demo data, signs in as the bootstrap admin, renders
every screen and reads the real HTML:

    Module (sidebar group)            level 1
      Permission page                 level 2   (current permission key)
        Screen (URL)                  level 2b
          Section: card / tab / popup level 3
            Fields, buttons, tables   level 4 (last layer)

Screens that need a record the demo data does not have are read from their
template source instead (marked "from template").

Needs BeautifulSoup (docs tool only, not an app dependency):
    pip install beautifulsoup4

Usage:
    python3 scripts/generate_app_tree.py            # writes APP_TREE.md
    python3 scripts/generate_app_tree.py out.md
"""
import os
import re
import sys
import tempfile
from collections import OrderedDict

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, ROOT)
_TMP = tempfile.mkdtemp(prefix='hdc-app-tree-')
os.environ.update({
    'HDC_ENV': 'test', 'HDC_SECRET_KEY': 'app-tree-only',
    'HDC_BOOTSTRAP_ADMIN_PASSWORD': 'App-Tree-Only-123',
    'HDC_INSTANCE_DIR': _TMP, 'HDC_DB_PATH': os.path.join(_TMP, 'tree.db'),
})

from bs4 import BeautifulSoup, NavigableString  # noqa: E402

from hdc.app import create_app  # noqa: E402
from hdc.extensions import db  # noqa: E402
from hdc.services.permissions import PAGE_TREE, page_id_for_path  # noqa: E402

ADMIN_PASSWORD = os.environ['HDC_BOOTSTRAP_ADMIN_PASSWORD']
SKIP_BUTTONS = {'', '×', 'close', 'cancel', 'toggle navigation'}


# --------------------------------------------------------------------------- seed
def seed(app):
    """Small, best-effort demo data so detail screens render with real content."""
    import importlib
    with app.app_context():
        for name in ('seed_tools_demo', 'seed_cashflow_demo'):
            try:
                module = importlib.import_module(f'scripts.{name}')
            except Exception:
                sys.path.insert(0, os.path.join(ROOT, 'scripts'))
                try:
                    module = importlib.import_module(name)
                except Exception:
                    continue
            try:
                module.main()
            except Exception:
                db.session.rollback()
        from hdc.models.materials import MaterialV2, Supplier
        from hdc.models.office import OfficeStaff
        from hdc.models.projects import Project, Stage
        from hdc.models.subcontract import Subcontractor
        from hdc.models.workforce import Worker
        if not Project.query.first():
            db.session.add(Project(name='Demo House', project_code='DEMO-1'))
            db.session.commit()
        project = Project.query.order_by(Project.id).first()
        if not Stage.query.filter_by(project_id=project.id).first():
            db.session.add(Stage(project_id=project.id, name='Foundation'))
            db.session.commit()
        stage = Stage.query.filter_by(project_id=project.id).order_by(Stage.id).first()
        for row in (Worker(worker_code='W-DEMO-1', name='Ali Mason'), Supplier(name='Demo Supplier'),
                    MaterialV2(name='Cement'), OfficeStaff(staff_code='OS-DEMO-1', name='Office Clerk'),
                    Subcontractor(name='Demo Subcontractor', project_id=project.id, stage_id=stage.id)):
            try:
                db.session.add(row)
                db.session.commit()
            except Exception:
                db.session.rollback()


def login(client):
    client.get('/hdc/login')
    with client.session_transaction() as session:
        token = session['_csrf_token']
    client.post('/hdc/login', data={'username': 'admin', 'password': ADMIN_PASSWORD,
                                    '_csrf_token': token})


# --------------------------------------------------------------------------- parse
def clean(text):
    text = re.sub(r'\{[{%#].*?[}%#]\}', ' ', text or '', flags=re.S)
    text = re.sub(r'\s+', ' ', text).strip(' ·:—-|*')
    return text


def own_text(tag, exclude=('button', 'a', 'select', 'input', 'form', 'small', 'span.badge')):
    """Heading text without the buttons/links sitting inside the header."""
    parts = []
    for child in tag.children:
        if isinstance(child, NavigableString):
            parts.append(str(child))
        elif 'badge' in (child.get('class') or []) or child.name == 'small':
            continue
        elif child.name in ('button', 'a', 'select', 'input', 'form', 'ul', 'div') and child.name != 'div':
            continue
        elif child.name == 'div' and child.find(['button', 'a', 'select', 'input', 'form']):
            first = next((t for t in child.find_all(['strong', 'h5', 'h6'])
                          if not t.find_parent(['button', 'a'])), None)
            if first:
                parts.append(first.get_text(' '))
        else:
            copy = BeautifulSoup(str(child), 'html.parser')
            for extra in copy.find_all(lambda t: 'badge' in (t.get('class') or []) or t.name in ('small', 'button', 'a')):
                extra.decompose()
            parts.append(copy.get_text(' '))
    return clean(' '.join(parts))[:80]


KPI_TILE_CLASSES = {'kpi-card', 'mc-kpi', 'stat-card', 'summary-card'}
KPI_LABEL_CLASSES = {'kpi-label', 'mc-kpi-label', 'stat-label', 'summary-label'}


def is_kpi(tag):
    return bool(KPI_TILE_CLASSES & set(tag.get('class') or []))


def is_section(tag):
    classes = tag.get('class') or []
    if 'card' in classes and not is_kpi(tag):
        return True
    if 'modal' in classes and tag.name == 'div':
        return True
    if 'tab-pane' in classes:
        return True
    if tag.name == 'details':
        return True
    return False


def section_title(tag, soup):
    classes = tag.get('class') or []
    if 'modal' in classes:
        head = tag.find(class_='modal-title')
        return 'Popup: ' + (clean(head.get_text(' ')) if head else '(untitled)')
    if 'tab-pane' in classes:
        tid = tag.get('id')
        trigger = soup.find(attrs={'data-bs-target': f'#{tid}'}) or soup.find(href=f'#{tid}') if tid else None
        return 'Tab: ' + (clean(re.sub(r'\(\s*\d*\s*\)', '', trigger.get_text(' '))) if trigger else tid or '(tab)')
    if tag.name == 'details':
        summary = tag.find('summary')
        return 'Expandable: ' + (clean(summary.get_text(' ')) if summary else '(details)')
    head = tag.find(class_=re.compile(r'card-header|hdc-card-header'))
    if head:
        return 'Card: ' + (own_text(head) or clean(head.get_text(' '))[:80] or '(untitled)')
    heading = tag.find(re.compile('^h[1-6]$'))
    if heading:
        return 'Panel: ' + clean(heading.get_text(' '))[:80]
    return 'Panel'


def field_label(control, soup):
    cid = control.get('id')
    if cid:
        label = soup.find('label', attrs={'for': cid})
        if label and clean(label.get_text(' ')):
            return clean(label.get_text(' '))
    parent = control.find_parent('label')
    if parent and clean(parent.get_text(' ')):
        return clean(parent.get_text(' '))
    if control.get('aria-label'):
        return clean(control.get('aria-label'))
    holder = control.parent
    for _ in range(3):
        if holder is None:
            break
        label = holder.find('label')
        if label is not None and clean(label.get_text(' ')):
            return clean(label.get_text(' '))
        holder = holder.parent
    for attr in ('placeholder', 'title'):
        value = clean(control.get(attr) or '')
        if value and not re.match(r'^[\d.,\s]+$', value):
            return value
    return clean((control.get('name') or '').replace('_', ' '))


def field_kind(control):
    if control.name == 'select':
        return 'dropdown'
    if control.name == 'textarea':
        return 'text box'
    kind = (control.get('type') or 'text').lower()
    return {'checkbox': 'tick', 'radio': 'choice', 'number': 'number', 'date': 'date',
            'file': 'file upload', 'search': 'search', 'password': 'password',
            'month': 'month', 'time': 'time', 'datetime-local': 'date-time'}.get(kind, 'text')


def button_text(button):
    text = clean(button.get('value') if button.name == 'input' else button.get_text(' '))
    if not text:
        text = clean(button.get('title') or button.get('aria-label') or '')
    return text[:50]


def collect(scope, soup, stop_at_sections=True):
    """Return (fields, buttons, tables, kpis, links) directly inside scope."""
    fields, buttons, tables, kpis = OrderedDict(), OrderedDict(), [], OrderedDict()

    def inside_nested(tag):
        parent = tag.parent
        while parent is not None and parent is not scope:
            if stop_at_sections and is_section(parent):
                return True
            parent = parent.parent
        return False

    for kpi in scope.find_all(is_kpi):
        if inside_nested(kpi):
            continue
        label = kpi.find(lambda t: bool(KPI_LABEL_CLASSES & set(t.get('class') or [])))
        if label is None:
            label = kpi.find(['small', 'span', 'div', 'h6'])
        text = clean(label.get_text(' ')) if label else clean(kpi.get_text(' '))[:40]
        if re.match(r'^(rs\.?|pkr)?\s*[\d,.\-]+', text.lower()):
            continue
        if text:
            kpis[text] = True
    for control in scope.find_all(['input', 'select', 'textarea']):
        if inside_nested(control):
            continue
        kind = (control.get('type') or '').lower()
        if kind in ('hidden', 'submit', 'button', 'reset', 'image'):
            continue
        if control.name == 'input' and control.get('name') in ('_csrf_token', 'csrf_token'):
            continue
        if (control.get('placeholder') or '').lower().startswith('type to search') and not control.get('aria-label'):
            continue  # combo search box: its dropdown is listed as the field
        label = field_label(control, soup)
        if not label or len(label) > 70:
            continue
        fields.setdefault(label, field_kind(control))
    for button in scope.find_all(['button', 'input', 'a']):
        if inside_nested(button):
            continue
        if button.name == 'input' and (button.get('type') or '').lower() not in ('submit', 'button'):
            continue
        if button.name == 'a' and 'btn' not in (button.get('class') or []):
            continue
        if button.get('data-bs-toggle') in ('tab', 'pill', 'collapse') and 'nav-link' in (button.get('class') or []):
            continue
        if 'btn-close' in (button.get('class') or []) or 'nav-group-toggle' in (button.get('class') or []):
            continue
        text = button_text(button)
        if text.lower() in SKIP_BUTTONS or len(text) < 2:
            continue
        buttons[text] = True
    for table in scope.find_all('table'):
        if inside_nested(table):
            continue
        cols = [clean(th.get_text(' ')) for th in table.find_all('th')]
        cols = [c for c in OrderedDict.fromkeys(cols) if c and len(c) < 40]
        if cols:
            tables.append(cols[:18])
    return fields, buttons, tables, kpis


def build_sections(scope, soup, depth=0):
    """Nearest nested sections of scope, each with its own content and children."""
    result = []

    def walk(node):
        for child in node.children:
            if getattr(child, 'name', None) is None:
                continue
            if is_section(child):
                fields, buttons, tables, kpis = collect(child, soup)
                result.append({
                    'title': section_title(child, soup), 'fields': fields, 'buttons': buttons,
                    'tables': tables, 'kpis': kpis,
                    'children': build_sections(child, soup, depth + 1) if depth < 3 else []})
            else:
                walk(child)
    walk(scope)
    return result


def screen_tree(html):
    soup = BeautifulSoup(html, 'html.parser')
    main = soup.find('main') or soup.body or soup
    heading = soup.find(class_='topbar-title')
    title = clean(heading.get_text(' ')) if heading else ''
    tabs = [clean(re.sub(r'\(\s*\d*\s*\)', '', t.get_text(' ')))
            for t in main.find_all(attrs={'data-bs-toggle': re.compile('^(tab|pill)$')})]
    fields, buttons, tables, kpis = collect(main, soup)
    return {'title': title, 'tabs': [t for t in tabs if t], 'fields': fields, 'buttons': buttons,
            'tables': tables, 'kpis': kpis, 'sections': build_sections(main, soup)}


def template_html(template):
    path = os.path.join(ROOT, 'templates', 'hdc', template)
    with open(path, encoding='utf-8') as handle:
        source = handle.read()
    source = re.sub(r'\{#.*?#\}', '', source, flags=re.S)
    source = re.sub(r'\{%.*?%\}', '', source, flags=re.S)
    source = re.sub(r'\{\{.*?\}\}', '', source, flags=re.S)
    return f'<main>{source}</main>'


# --------------------------------------------------------------------------- render tree
def render_items(node, prefix, lines):
    items = []
    if node.get('kpis'):
        items.append(('KPI tiles: ' + ' · '.join(node['kpis']), []))
    for cols in node.get('tables', []):
        items.append(('Table columns: ' + ' | '.join(cols), []))
    if node.get('fields'):
        items.append(('Fields: ' + ' · '.join(f'{k} ({v})' for k, v in node['fields'].items()), []))
    if node.get('buttons'):
        items.append(('Buttons: ' + ' · '.join(f'[{b}]' for b in node['buttons']), []))
    for section in node.get('sections', node.get('children', [])):
        title = section['title']
        has = {k: bool(section.get(k)) for k in ('fields', 'buttons', 'tables', 'kpis', 'children')}
        if not any(has.values()):
            continue
        if title == 'Panel':
            names = ' '.join(section.get('buttons', {})).lower()
            if has['fields'] and ('apply' in names or 'filter' in names or 'search' in names):
                title = 'Filter bar'
            elif has['fields']:
                title = 'Form panel'
            elif has['tables'] and not has['buttons']:
                title = 'List'
            elif has['buttons'] and not has['tables']:
                title = 'Action bar'
            elif has['kpis']:
                title = 'Summary tiles'
            else:
                title = 'Panel'
        items.append((title, section))
    for index, (label, child) in enumerate(items):
        last = index == len(items) - 1
        lines.append(f'{prefix}{"└── " if last else "├── "}{label}')
        if child:
            render_items(child, prefix + ('    ' if last else '│   '), lines)


def gather():
    app = create_app({'TESTING': True, 'HDC_DB_PATH': os.environ['HDC_DB_PATH'],
                      'HDC_INSTANCE_DIR': _TMP})
    seed(app)
    client = app.test_client()
    login(client)
    import inspect
    screens = {}
    for rule in app.url_map.iter_rules():
        if 'GET' not in rule.methods or rule.endpoint == 'static':
            continue
        view = inspect.unwrap(app.view_functions[rule.endpoint])
        try:
            source = inspect.getsource(view)
        except (OSError, TypeError):
            continue
        templates = re.findall(r"render_template\(\s*['\"]([^'\"]+)", source)
        if not templates:
            continue
        sample = re.sub(r'<int:[^>]+>', '1', rule.rule)
        bucket = page_id_for_path(re.sub(r'<[^>]+>', 'x', sample))
        if not bucket:
            continue
        screens.setdefault(bucket, []).append((rule.rule, sample, templates[0], rule.endpoint))
    data = {}
    for bucket, entries in screens.items():
        seen = set()
        for rule, sample, template, endpoint in sorted(entries, key=lambda e: (e[0].count('<'), e[0])):
            if template in seen:
                continue
            seen.add(template)
            source = 'live'
            html = None
            if '<' not in re.sub(r'<int:[^>]+>', '', rule):
                response = client.get(sample)
                if response.status_code == 200:
                    html = response.get_data().decode('utf-8', 'replace')
            if html is None:
                html = template_html(template)
                source = 'from template'
            tree = screen_tree(html)
            data.setdefault(bucket, []).append({'rule': rule, 'template': template,
                                                'source': source, 'tree': tree})
    return data


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, 'APP_TREE.md')
    data = gather()
    lines = [
        '# HDC ERP: Full App Tree (Dashboard → last layer)',
        '',
        '> Generated by `scripts/generate_app_tree.py` from the real screens (rendered as admin).',
        '> Regenerate after UI changes: `python3 scripts/generate_app_tree.py`.',
        '',
        '## How to read this',
        '',
        '| Level | Meaning | Example |',
        '|---|---|---|',
        '| **L1 Module** | Sidebar group | Accounts & Cash |',
        '| **L2 Page** | One permission page today (`key`) → its screens (URLs) | Money Center `money_center` |',
        '| **L3 Section** | Card, Tab, Popup, Expandable panel on that screen | Tab: Money IN |',
        '| **L4 Contents** | KPI tiles · Table columns · Fields (type) · Buttons | [Save] |',
        '',
        'Screens marked *from template* need a record that the demo data does not have. Their',
        'structure was read from the template, so labels that come from data may be missing.',
        'Today you can only tick L2 (Read / Write). The plan in `USER_PERMISSIONS_PLAN.md`',
        'makes every L3 section and L4 button/field individually controllable.',
        '',
        '## Overview (L1 → L2)',
        '',
        '```',
        'HDC ERP',
    ]
    for si, section in enumerate(PAGE_TREE):
        last_s = si == len(PAGE_TREE) - 1
        lines.append(f'{"└── " if last_s else "├── "}{section["label"]}')
        for pi, page in enumerate(section['children']):
            last_p = pi == len(section['children']) - 1
            count = len(data.get(page['id'], []))
            lines.append(f'{"    " if last_s else "│   "}{"└── " if last_p else "├── "}'
                         f'{page["label"]}  [{page["id"]}]' + (f'  ({count} screen{"s" if count != 1 else ""})' if count else '  (no own screen)'))
    lines += ['```', '']
    total_screens = sum(len(v) for v in data.values())
    lines += [f'**Totals:** {len(PAGE_TREE)} modules · {sum(len(s["children"]) for s in PAGE_TREE)} permission pages · '
              f'{total_screens} screens.', '', '---', '', '## Full tree (L1 → L4)', '']
    for section in PAGE_TREE:
        lines += [f'### {section["label"]}', '']
        for page in section['children']:
            screens = data.get(page['id'], [])
            lines.append(f'<details><summary><b>{page["label"]}</b> <code>{page["id"]}</code> '
                         f'· {len(screens)} screen(s)</summary>')
            lines += ['', '```']
            if not screens:
                lines.append(f'{page["label"]}')
                lines.append('└── (no screen of its own: controls data/actions shown on other pages, or JSON APIs)')
            for index, screen in enumerate(screens):
                tree = screen['tree']
                title = tree['title'] or screen['template']
                tag = '' if screen['source'] == 'live' else '  (from template)'
                lines.append(f'{title}  →  {screen["rule"]}{tag}')
                render_items(tree, '', lines)
                if index != len(screens) - 1:
                    lines.append('')
            lines += ['```', '', '</details>', '']
        lines.append('')
    with open(out, 'w', encoding='utf-8') as handle:
        handle.write('\n'.join(lines) + '\n')
    print(f'wrote {out}: {total_screens} screens')


if __name__ == '__main__':
    main()
