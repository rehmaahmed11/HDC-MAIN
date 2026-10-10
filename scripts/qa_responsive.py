#!/usr/bin/env python3
"""Static gate for the adaptive/responsive layer.

The layer is one stylesheet (`static/hdc/css/responsive.css`) and one script
(`static/hdc/js/core/responsive_tables.js`) that re-flow the whole app for
phones without touching the 110+ templates.  Because everything depends on
them being present, ordered and syntactically sound, this script checks exactly
that — with the standard library only, so it runs anywhere CI runs.

    python scripts/qa_responsive.py            # human readable
    python scripts/qa_responsive.py --json     # machine readable

Checks
------
1.  responsive.css exists and is syntactically balanced (braces, at-rules,
    every declaration carrying a value, every @media prelude non-empty).
2.  Every breakpoint the design claims to support is actually present.
3.  base.html loads the stylesheet AFTER every other stylesheet, and loads the
    script AFTER core/audit.js (whose async "Entered by" column must be
    labelled too).
4.  The viewport tag asks for the device width and allows the page to paint
    under a notch (`viewport-fit=cover`); it never blocks pinch-zoom.
5.  Both assets are stamped so a phone cannot keep last week's copy.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
CSS = os.path.join(BASE, 'static', 'hdc', 'css', 'responsive.css')
JS = os.path.join(BASE, 'static', 'hdc', 'js', 'core', 'responsive_tables.js')
BASE_HTML = os.path.join(BASE, 'templates', 'hdc', 'shared', 'base.html')

# The widths the design is required to serve, in CSS pixels.
REQUIRED_BREAKPOINTS = (
    ('991.98', 'tablet portrait and below'),
    ('767.98', 'phones and small tablets'),
    ('575.98', 'small phones'),
    ('419.98', 'tiny phones (<=400px)'),
    ('359.98', 'very small phones (320px)'),
)
REQUIRED_FEATURES = (
    ('max-height: 560px', 'landscape phones'),
    ('hover: none', 'touch target sizing'),
    ('100dvh', 'mobile browser chrome'),
)


def _read(path):
    with open(path, encoding='utf-8') as handle:
        return handle.read()


def _strip_comments(text):
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.S)
    # Remove comment-like content inside strings (rare in this stylesheet).
    return text


def check_css_syntax(source):
    """Brace/at-rule balance plus a declaration sanity pass."""
    problems = []
    text = _strip_comments(source)
    depth = 0
    line = 1
    for index, char in enumerate(text):
        if char == '\n':
            line += 1
        elif char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
            if depth < 0:
                problems.append('unbalanced "}" on line %d' % line)
                depth = 0
    if depth:
        problems.append('%d unclosed "{" block(s)' % depth)

    for match in re.finditer(r'@media([^{]*)\{', text):
        prelude = match.group(1).strip()
        if not prelude:
            problems.append('@media with no condition')
        elif prelude.count('(') != prelude.count(')'):
            problems.append('@media prelude has unbalanced parentheses: %r' % prelude)
    if text.count('@supports') != len(re.findall(r'@supports[^{]*\{', text)):
        problems.append('malformed @supports rule')

    # Every declaration inside a block must be `property: value`.
    for block in re.findall(r'\{([^{}]*)\}', text):
        for decl in [d.strip() for d in block.split(';') if d.strip()]:
            if ':' not in decl:
                problems.append('declaration without a value: %r' % decl[:60])
            elif decl.split(':', 1)[0].strip().endswith('{'):
                problems.append('nested brace inside declaration: %r' % decl[:60])
    return sorted(set(problems))


def check_breakpoints(source):
    missing = []
    text = _strip_comments(source)
    for token, label in REQUIRED_BREAKPOINTS:
        if token not in text:
            missing.append('%s (%s)' % (token, label))
    for token, label in REQUIRED_FEATURES:
        if token not in text:
            missing.append('%s (%s)' % (token, label))
    return missing


def check_base_html(source):
    problems = []
    css_links = re.findall(r'<link[^>]+href="([^"]+\.css[^"]*)"', source)
    if not css_links:
        problems.append('base.html loads no stylesheet')
    responsive = [c for c in css_links if 'responsive.css' in c]
    if not responsive:
        problems.append('base.html does not load responsive.css')
    elif css_links[-1] != responsive[-1]:
        problems.append('responsive.css must be loaded LAST (found %s after it)'
                        % css_links[css_links.index(responsive[-1]) + 1])

    scripts = re.findall(r'<script[^>]+src="([^"]+)"', source)
    if 'js/core/responsive_tables.js' not in ' '.join(scripts):
        problems.append('base.html does not load core/responsive_tables.js')
    else:
        audit = [i for i, s in enumerate(scripts) if 'core/audit.js' in s]
        rt = [i for i, s in enumerate(scripts) if 'core/responsive_tables.js' in s]
        if audit and rt and rt[0] < audit[0]:
            problems.append('responsive_tables.js must load after core/audit.js')

    viewport = re.search(r'<meta name="viewport" content="([^"]*)"', source)
    if not viewport:
        problems.append('base.html has no viewport meta tag')
    else:
        content = viewport.group(1)
        if 'width=device-width' not in content:
            problems.append('viewport must set width=device-width')
        if 'viewport-fit=cover' not in content:
            problems.append('viewport must set viewport-fit=cover for notched phones')
        if 'maximum-scale' in content or 'user-scalable=no' in content:
            problems.append('viewport must not block pinch-zoom')

    if 'hdc_asset_stamp' not in source:
        problems.append('layout assets are not cache-busted (hdc_asset_stamp)')
    for asset in ('responsive.css', 'responsive_tables.js'):
        if not re.search(re.escape(asset) + r'\?v=', source):
            problems.append('%s is not stamped with ?v=' % asset)
    return problems


def check_js(source):
    problems = []
    for name in ('HDCResponsiveTables', 'hdc-rt-stack', 'data-hdc-label',
                 'MutationObserver'):
        if name not in source:
            problems.append('responsive_tables.js is missing %r' % name)
    # node --check is the real syntax gate (CI runs it over every static JS).
    return problems


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()

    css = _read(CSS)
    js = _read(JS)
    base = _read(BASE_HTML)

    result = {
        'css_syntax': check_css_syntax(css),
        'missing_breakpoints': check_breakpoints(css),
        'base_html': check_base_html(base),
        'js_contract': check_js(js),
    }
    result['ok'] = not any(result.values())

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        checks = {k: v for k, v in result.items() if k != 'ok'}
        total = sum(len(v) for v in checks.values())
        for key, value in checks.items():
            if value:
                print('[FAIL] %s' % key)
                for item in value:
                    print('       - %s' % item)
            else:
                print('[ ok ] %s' % key)
        print('Responsive layer: %s (%d finding%s)'
              % ('PASS' if result['ok'] else 'FAIL', total, '' if total == 1 else 's'))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
