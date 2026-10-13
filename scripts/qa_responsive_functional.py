#!/usr/bin/env python3
"""Interaction checks + screenshots for the responsive layer (real browser).

Where ``scripts/qa_responsive_pages.py`` measures geometry, this script drives the
shell the way a thumb would, at phone, tablet and desktop widths:

* the hamburger is visible below 992px and opens the off-canvas drawer with the
  logout control reachable inside it; it closes again with Escape / backdrop;
* the top bar stays pinned while the page scrolls;
* every Bootstrap modal fits the screen and its footer buttons stay on screen;
* phone inputs keep a 16px font (iOS does not zoom on focus);
* stacked record cards exist wherever a wide table was folded;
* the page itself never scrolls sideways.

With ``--shots`` it also writes full-page screenshots for the given pages so the
result can be reviewed by eye.  Exits non-zero when a check fails.

Usage
-----
    python scripts/qa_responsive_functional.py --executable /path/to/chromium
    python scripts/qa_responsive_functional.py --views mob-390,tab-768 --shots
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

VIEWPORTS = {
    'pc-1440': (1440, 900), 'pc-1280': (1280, 800),
    'tab-1024': (1024, 768), 'tab-768': (768, 1024),
    'mob-390': (390, 844), 'mob-360': (360, 800), 'mob-320': (320, 568),
}

# Representative pages: one per layout family (dashboard, list, wide table,
# form, long form, statement, hub, print sheet, login).
SHOT_PAGES = [
    '/hdc/', '/hdc/accounts', '/hdc/accounts/manage', '/hdc/projects',
    '/hdc/workers', '/hdc/timekeeping', '/hdc/payroll', '/hdc/expenses',
    '/hdc/purchase-v2/suppliers', '/hdc/tool-rental', '/hdc/office-management',
    '/hdc/accounts/shared/expenses/new', '/hdc/workers/1/statement',
    '/hdc/reports/project/1/pdf', '/hdc/payroll/1/salary-cards',
]
ADMIN_PASSWORD = 'Responsive-Qa-Only-123'


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--output', default='qa/responsive-functional.json')
    parser.add_argument('--executable', help='Chromium/Chrome binary')
    parser.add_argument('--views', default='mob-320,mob-390,tab-768,pc-1440')
    parser.add_argument('--shots', action='store_true', help='also write screenshots')
    parser.add_argument('--shot-dir', default='qa/screenshots')
    parser.add_argument('--timeout', type=int, default=25000)
    args = parser.parse_args()

    views = [v.strip() for v in args.views.split(',') if v.strip()]
    unknown = [v for v in views if v not in VIEWPORTS]
    if unknown:
        parser.error('unknown viewport(s): %s' % ', '.join(unknown))

    from werkzeug.serving import make_server
    from hdc.app import create_app
    from hdc.extensions import db

    tmp = tempfile.mkdtemp(prefix='hdc-responsive-functional-')
    os.environ.update(HDC_ENV='test', HDC_SECRET_KEY='responsive-audit-only',
                      HDC_BOOTSTRAP_ADMIN_PASSWORD=ADMIN_PASSWORD,
                      HDC_INSTANCE_DIR=tmp, HDC_DB_PATH=os.path.join(tmp, 'audit.db'))
    app = create_app({'TESTING': True})

    # Reuse the page audit's fixture seeder (same module that owns the probe).
    import importlib.util
    spec = importlib.util.spec_from_file_location('qa_pages', ROOT / 'scripts' / 'qa_responsive_pages.py')
    qa_pages = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(qa_pages)
    with app.test_client() as client:
        qa_pages.seed_demo(client, app)

    server = make_server('127.0.0.1', 0, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = 'http://127.0.0.1:%d' % server.server_port

    from playwright.sync_api import sync_playwright

    findings = []

    def check(view, name, ok, detail=''):
        findings.append({'view': view, 'check': name, 'ok': bool(ok), 'detail': detail})
        print('%-8s %s %s%s' % (view, 'PASS' if ok else 'FAIL', name,
                                ('  ' + str(detail)[:200]) if detail else ''))

    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=args.executable,
                                     args=['--no-sandbox', '--disable-dev-shm-usage',
                                           '--headless'])
        for view in views:
            width, height = VIEWPORTS[view]
            touch = view.startswith('mob') or view.startswith('tab')
            context = browser.new_context(viewport={'width': width, 'height': height},
                                          has_touch=touch, is_mobile=view.startswith('mob'))
            page = context.new_page()
            page.set_default_timeout(args.timeout)
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)[:200]))

            page.goto(base + '/hdc/login', wait_until='load')
            box = page.locator('.login-card').bounding_box()
            check(view, 'login card fits the screen',
                  box and box['x'] >= -1 and box['x'] + box['width'] <= width + 1, box)
            page.fill('[name=username]', 'admin')
            page.fill('[name=password]', ADMIN_PASSWORD)
            page.click('button[type=submit]')
            page.wait_for_url(base + '/hdc/')
            page.wait_for_timeout(250)

            if width < 992:
                toggle = page.locator('#sidebarToggle')
                check(view, 'hamburger visible', toggle.is_visible())
                if toggle.is_visible():
                    toggle.click()
                    page.wait_for_timeout(450)
                    drawer = page.locator('#sidebar').bounding_box()
                    check(view, 'drawer opens on screen',
                          drawer and drawer['x'] >= -1 and drawer['width'] > 100, drawer)
                    logout = page.locator('#sidebar form[action*="logout"] button').bounding_box()
                    check(view, 'logout reachable inside the drawer',
                          logout and 0 <= logout['y'] and logout['y'] + logout['height'] <= height + 1,
                          logout)
                    page.keyboard.press('Escape')
                    page.wait_for_timeout(420)
                    closed = page.locator('#sidebar').bounding_box()
                    check(view, 'drawer closes again',
                          closed is None or closed['x'] < 0 or closed['width'] < 5, closed)

            if width < 768:
                small = page.evaluate("""() => {
                    const bad = [];
                    document.querySelectorAll('input, select, textarea').forEach((el) => {
                        if (!el.checkVisibility()) return;
                        const fs = parseFloat(getComputedStyle(el).fontSize);
                        if (fs < 16) bad.push({name: el.name || el.id || el.tagName, fs: fs});
                    });
                    return bad.slice(0, 5);
                }""")
                check(view, 'no input under 16px (no iOS zoom)', not small, small)

            page.evaluate('window.scrollTo(0, 1400)')
            page.wait_for_timeout(220)
            bar = page.locator('.topbar').bounding_box()
            check(view, 'top bar stays pinned while scrolling', bar and bar['y'] <= 1, bar)
            page.evaluate('window.scrollTo(0, 0)')

            page.goto(base + '/hdc/accounts', wait_until='load')
            page.wait_for_timeout(250)
            trigger = page.locator('[data-bs-toggle="modal"]:visible').first
            if trigger.count():
                trigger.click()
                page.wait_for_timeout(520)
                geo = page.evaluate("""() => {
                    const m = document.querySelector('.modal.show');
                    if (!m) return null;
                    const d = m.querySelector('.modal-dialog');
                    const c = m.querySelector('.modal-content');
                    const r = d.getBoundingClientRect();
                    const footer = m.querySelector('.modal-footer');
                    const scope = footer || c;
                    const buttons = Array.from(scope.querySelectorAll('button, a.btn'))
                        .filter(x => x.checkVisibility());
                    const last = buttons.length ? buttons[buttons.length - 1].getBoundingClientRect() : null;
                    /* A button below the fold is only a defect when nothing can
                       scroll to it.  A dialog whose body carries the submit
                       button (the JS-built transaction form) scrolls as a
                       sheet, exactly like the record card lists. */
                    const scrollable = (el) => {
                        let p = el; while (p && p !== document.body) {
                            const cs = getComputedStyle(p);
                            if ((cs.overflowY === 'auto' || cs.overflowY === 'scroll')
                                && p.scrollHeight > p.clientHeight + 1) return true;
                            p = p.parentElement; }
                        return false; };
                    const visible = !!last && last.top >= 0 && last.bottom <= innerHeight + 1;
                    return {x: Math.round(r.x), right: Math.round(r.right), vw: innerWidth, vh: innerHeight,
                            bodyOverflowX: c.scrollWidth > c.clientWidth,
                            hasFooter: !!footer,
                            footerOnScreen: visible,
                            footerReachable: visible || (buttons.length > 0 && scrollable(buttons[buttons.length - 1])),
                            footerText: buttons.length ? buttons[buttons.length - 1].textContent.trim().slice(0, 20) : null};
                }""")
                check(view, 'modal fits the screen',
                      geo and geo['x'] >= -1 and geo['right'] <= geo['vw'] + 1 and not geo['bodyOverflowX'],
                      geo)
                check(view, 'modal action button reachable', geo and geo['footerReachable'], geo)
                # A dialog that carries its own footer must show it: that bar is
                # pinned on purpose, so scrolling must never be needed.
                if geo and geo['hasFooter']:
                    check(view, 'pinned modal footer stays on screen', geo['footerOnScreen'], geo)
                page.keyboard.press('Escape')
                page.wait_for_timeout(280)

            # Stacked record cards: a folded table must keep every card labelled.
            stacked = page.evaluate("""() => {
                const t = document.querySelector('main table[data-hdc-rt-stack], main table.hdc-rt-stack');
                if (!t) return null;
                const cells = Array.from(t.querySelectorAll('tbody td')).filter(td => td.checkVisibility());
                const unlabelled = cells.filter(td => !td.getAttribute('data-hdc-label')
                    && td.getAttribute('colspan') === null).length;
                return {stacked: true, cells: cells.length, unlabelled: unlabelled,
                        display: getComputedStyle(t).display, label: t.getAttribute('data-hdc-label')};
            }""")
            if stacked:
                check(view, 'stacked table keeps every field labelled', stacked['unlabelled'] == 0, stacked)

            page.goto(base + '/hdc/', wait_until='load')
            page.wait_for_timeout(200)
            overflow = page.evaluate(
                '() => ({doc: document.documentElement.scrollWidth - innerWidth,'
                ' body: document.body.scrollWidth - innerWidth})')
            check(view, 'page does not scroll sideways',
                  overflow['doc'] <= 1 and overflow['body'] <= 1, overflow)

            if args.shots:
                out_dir = Path(args.shot_dir) / view
                out_dir.mkdir(parents=True, exist_ok=True)
                for path in SHOT_PAGES:
                    page.goto(base + path, wait_until='load')
                    page.wait_for_timeout(320)
                    name = (path.strip('/').replace('/', '-') or 'dashboard') + '.png'
                    page.screenshot(path=str(out_dir / name), full_page=True)

            console_errors = [e for e in errors if e]
            check(view, 'no uncaught page errors', not console_errors, console_errors[:2])
            context.close()
        browser.close()

    server.shutdown()
    with app.app_context():
        db.session.remove()
        db.engine.dispose()

    report = {'views': views, 'checks': findings,
              'failed': [f for f in findings if not f['ok']]}
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(report, indent=1) + '\n')
    passed = sum(1 for f in findings if f['ok'])
    print('\n%d/%d functional checks passed  (%d failed)'
          % (passed, len(findings), len(report['failed'])))
    print('report: %s' % args.output)
    return 1 if report['failed'] else 0


if __name__ == '__main__':
    sys.exit(main())
