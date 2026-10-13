#!/usr/bin/env python3
"""Static CSS cascade probe — "which rule made this box this size?"

The app layers seven stylesheets (Bootstrap, hdc.css, per-section sheets,
responsive.css) over 120+ templates, so a question like "why is this field
300px here and 512px there?" is usually answered by a single winning
declaration in a specific file.  This script answers it without a browser:

    # 1. capture a rendered page (no server needed, throwaway DB)
    python3 - <<'EOF'
    import os, tempfile
    tmp = tempfile.mkdtemp()
    os.environ.update(HDC_ENV='test', HDC_SECRET_KEY='probe-only',
                      HDC_BOOTSTRAP_ADMIN_PASSWORD='Probe-Test-Only-123',
                      HDC_DB_PATH=tmp + '/p.db', HDC_INSTANCE_DIR=tmp)
    from hdc.app import create_app
    c = create_app({'TESTING': True}).test_client()
    c.get('/hdc/login')
    with c.session_transaction() as s: tok = s['_csrf_token']
    c.post('/hdc/login', data={'username': 'admin', 'password': 'Probe-Test-Only-123',
                              '_csrf_token': tok})
    open('/tmp/page.html', 'w').write(c.get('/hdc/expense_categories').get_data(as_text=True))
    EOF

    # 2. ask which declarations win for the elements you care about
    python scripts/css_cascade_probe.py --html /tmp/page.html \
        --selector ".expense-category-page" --selector ".expense-category-new-name"

Optional development dependency: pip install tinycss2 cssselect2

It is an approximation of a browser, on purpose: it resolves the cascade
(origin order, specificity, source order, width media queries) but does not run
layout.  Numbers that depend on text (button widths) or on the table/flex
algorithms still have to come from a real browser — `scripts/qa_browser.py`
takes that route when Playwright is available.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from html.parser import HTMLParser

import tinycss2
import cssselect2

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

DEFAULT_PROPS = (
    'display', 'width', 'max-width', 'min-width', 'padding', 'margin',
    'font-size', 'border-radius', 'flex', 'gap', 'grid-template-columns',
    'align-items', 'justify-content', 'text-align', 'white-space',
    'vertical-align',
)

MEDIA_RE = re.compile(r'\(\s*(min|max)-width\s*:\s*([\d.]+)px\s*\)')


# ── DOM ──────────────────────────────────────────────────────────────────────
class Node:
    def __init__(self, tag, attrs, parent):
        self.tag = tag
        self.attrs = dict(attrs)
        self.parent = parent
        self.children = []
        self.etree = None
        if parent is not None:
            parent.children.append(self)

    @property
    def cls(self):
        return (self.attrs.get('class') or '').split()

    def describe(self):
        out = self.tag
        if self.attrs.get('id'):
            out += '#' + self.attrs['id']
        for name in self.cls:
            out += '.' + name
        return out


class Dom(HTMLParser):
    VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link',
            'meta', 'param', 'source', 'track', 'wbr'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node('#document', {}, None)
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs, self.cur)
        if tag not in self.VOID:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        Node(tag, attrs, self.cur)

    def handle_endtag(self, tag):
        node = self.cur
        while node is not None and node.tag != tag:
            node = node.parent
        if node is not None and node.parent is not None:
            self.cur = node.parent


def build_etree(node, etree):
    for child in node.children:
        el = etree.makeelement(child.tag, child.attrs)
        child.etree = el
        etree.append(el)
        build_etree(child, el)


def find(node, predicate, acc=None):
    acc = [] if acc is None else acc
    for child in node.children:
        if predicate(child):
            acc.append(child)
        find(child, predicate, acc)
    return acc


# ── CSS ──────────────────────────────────────────────────────────────────────
def split_selectors(text):
    """Split a selector list on top-level commas (parenthesis aware)."""
    out, depth, cur = [], 0, ''
    for ch in text:
        if ch in '([':
            depth += 1
        elif ch in ')]':
            depth -= 1
        if ch == ',' and depth == 0:
            out.append(cur.strip())
            cur = ''
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return [s for s in out if s]


def media_matches(media, width):
    """True when a (width-only) media prelude applies at ``width``."""
    if not media:
        return True
    conds = MEDIA_RE.findall(media)
    if not conds:
        return False  # print / hover:none / prefers-* : not a width query
    for kind, value in conds:
        value = float(value)
        if kind == 'max' and width > value + 0.02:
            return False
        if kind == 'min' and width < value - 0.02:
            return False
    return True


def stylesheet_paths(html, base=BASE):
    """Map the page's <link> hrefs onto files on disk."""
    out = []
    for href in re.findall(r'<link[^>]+href="([^"]+\.css[^"]*)"', html):
        rel = href.split('?')[0].lstrip('/')
        if rel.startswith('hdc_static/'):
            rel = 'static/hdc/' + rel[len('hdc_static/'):]
        path = os.path.join(base, rel)
        if os.path.exists(path):
            out.append(path)
        else:
            print(f'  ! stylesheet not found on disk: {href}', file=sys.stderr)
    return out


class Cascade:
    def __init__(self, paths):
        self.rules = []
        order = 0
        for path in paths:
            order = self._parse(path, order)

    def _parse(self, path, order):
        with open(path, encoding='utf-8', errors='replace') as handle:
            text = handle.read()
        # Blank comments but keep their newlines: source lines must stay real.
        text = re.sub(r'/\*.*?\*/', lambda m: '\n' * m.group(0).count('\n'),
                      text, flags=re.S)
        nodes = tinycss2.parse_stylesheet(text, skip_comments=True,
                                          skip_whitespace=True)
        return self._walk(nodes, None, order, path)

    def _walk(self, nodes, media, order, path):
        for node in nodes:
            if node.type == 'at-rule' and node.lower_at_keyword == 'media':
                inner = tinycss2.parse_rule_list(node.content, skip_whitespace=True)
                order = self._walk(inner, tinycss2.serialize(node.prelude).strip(),
                                   order, path)
            elif node.type == 'qualified-rule':
                order += 1
                prelude = tinycss2.serialize(node.prelude).strip()
                decls = [d for d in tinycss2.parse_declaration_list(
                    node.content, skip_comments=True, skip_whitespace=True)
                    if d.type == 'declaration']
                for selector in split_selectors(prelude):
                    try:
                        compiled = cssselect2.compile_selector_list(selector)
                    except Exception:
                        continue
                    for csel in compiled:
                        # ::before / ::file-selector-button match nothing we measure
                        if csel.pseudo_element:
                            continue
                        self.rules.append((csel, decls, media, order, path,
                                           node.source_line))
        return order

    def winners(self, wrapper, width, props):
        """Winning declaration for each property, with where it came from."""
        out = {}
        for csel, decls, media, order, path, line in self.rules:
            if not media_matches(media, width):
                continue
            try:
                if not csel.test(wrapper):
                    continue
            except Exception:
                continue
            key = (csel.specificity, order)
            for decl in decls:
                name = decl.lower_name
                if name not in props:
                    continue
                if name not in out or key >= out[name][0]:
                    out[name] = (key, tinycss2.serialize(decl.value).strip(),
                                 os.path.relpath(path, BASE), line,
                                 csel.specificity)
        return out


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--html', required=True,
                        help='a page already rendered by the app')
    parser.add_argument('--selector', action='append', required=True,
                        help='CSS selector to report on (repeatable)')
    parser.add_argument('--width', type=int, action='append',
                        help='viewport width in px (repeatable, default 1440)')
    parser.add_argument('--props', help='comma-separated property list')
    args = parser.parse_args()

    widths = args.width or [1440]
    props = ([p.strip() for p in args.props.split(',') if p.strip()]
             if args.props else list(DEFAULT_PROPS))

    with open(args.html, encoding='utf-8') as handle:
        html = handle.read()

    cascade = Cascade(stylesheet_paths(html))
    dom = Dom()
    dom.feed(html)
    import xml.etree.ElementTree as ET
    tree = ET.Element('html')
    build_etree(dom.root, tree)
    wrappers = {id(w.etree_element): w
                for w in cssselect2.ElementWrapper.from_html_root(tree).iter_subtree()}

    print(f'{args.html}: {len(cascade.rules)} rules from '
          f'{len(stylesheet_paths(html))} stylesheets')

    missing = False
    for selector in args.selector:
        for width in widths:
            hits = []
            for node in find(dom.root, lambda n: n.tag != '#document'):
                wrapper = wrappers.get(id(node.etree))
                if wrapper is None:
                    continue
                for csel in cssselect2.compile_selector_list(selector):
                    try:
                        if csel.test(wrapper):
                            hits.append(node)
                            break
                    except Exception:
                        continue
            print(f'\n══ {selector}  @ {width}px  ({len(hits)} match'
                  f'{"es" if len(hits) != 1 else ""})')
            if not hits:
                missing = True
                print('   (nothing matched)')
            for node in hits[:6]:
                print(f'\n   {node.describe()[:90]}')
                winners = cascade.winners(wrappers[id(node.etree)], width, props)
                if not winners:
                    print('        – no declaration wins any tracked property –')
                for name in props:
                    if name in winners:
                        _, value, rel, line, spec = winners[name]
                        print(f'        {name:>20} = {value:<34} ← {rel}:{line}'
                              f'   spec={spec}')
            if len(hits) > 6:
                print(f'   … and {len(hits) - 6} more')
    return 1 if missing else 0


if __name__ == '__main__':
    sys.exit(main())
