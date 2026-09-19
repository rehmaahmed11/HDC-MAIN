#!/usr/bin/env python3
"""Reorganize frontend assets by domain (one folder per feature).

- templates/hdc/*.html -> templates/hdc/<domain>/*.html
- render_template('x.html') calls updated to the new relative paths
- {% extends "base.html" %} -> {% extends "shared/base.html" %}
- static/hdc/js/hdc.js split into core/{layout,theme,forms,combo}.js
- static/hdc/js/project_estimation.js -> static/hdc/js/pages/

Idempotent: safe to re-run (skips work that is already done).
Usage: python3 scripts/reorganize_frontend.py [--check]
"""
import os
import re
import subprocess
import sys

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TPL = os.path.join(BASE, "templates", "hdc")
ROUTES = os.path.join(BASE, "hdc", "routes")
STATIC_JS = os.path.join(BASE, "static", "hdc", "js")

DOMAIN_OF = {
    "shared": ["base.html", "_pagination.html"],
    "auth": ["login.html"],
    "dashboard": ["dashboard.html", "kpi_detail.html"],
    "projects": ["projects.html", "add_project.html", "edit_project.html",
                 "project_detail.html", "project_report_print.html",
                 "stage_form.html", "stage_ledger.html",
                 "stage_library.html", "stage_overview.html"],
    "subcontractors": ["subcontractors.html", "subcontractor_ledger.html",
                       "subcontractor_payment.html",
                       "subcontractor_attendance.html",
                       "subcontractor_worker_ledger.html"],
    "workers": ["workers.html", "trades.html", "worker_ledger.html",
                "worker_advance.html", "worker_payment.html",
                "worker_ledger_entry_edit.html", "worker_rate.html"],
    "timekeeping": ["timekeeping.html", "timekeeping_edit.html",
                    "timekeeping_status.html", "attendance.html"],
    "payroll": ["payroll.html", "payroll_generate.html",
                "payroll_history.html", "payroll_salary_cards.html",
                "payroll_salary_cards_print.html"],
    "expenses": ["expenses.html", "expense_edit.html",
                 "expense_categories.html", "alerts.html"],
    "office": ["office_management.html", "office_staff_home.html",
               "office_staff_ledger.html", "office_staff_ledger_list.html",
               "office_staff_ledger_entry_edit.html",
               "office_staff_payment.html", "office_staff_attendance.html",
               "office_expenses.html", "office_expense_edit.html",
               "allowance_categories.html", "allowance_category_edit.html",
               "office_staff_allowances.html",
               "office_staff_allowance_edit.html"],
    "materials": ["materials.html", "material_usage.html",
                  "purchases.html"],
    "purchase": ["purchase_v2.html", "purchase_v2_materials.html",
                 "purchase_v2_purchases.html", "purchase_v2_suppliers.html",
                 "purchase_v2_supplier_detail.html",
                 "purchase_v2_delivered.html", "purchase_v2_usage.html",
                 "purchase_v2_stock.html"],
    "estimation": ["estimation.html", "project_estimation.html",
                   "formulas.html"],
    "reports": ["reports.html", "reports_glance.html"],
    "users": ["users.html", "event_recorder.html"],
    "accounts": ["accounts.html", "account_ledger.html",
                 "accounts_entries.html", "accounts_transaction_edit.html",
                 "accounts_reconciliation.html",
                 "accounts_kpi_detail.html", "transaction_receipt.html",
                 "cashflow.html", "cashflow_report.html",
                 "personal_management.html", "personal_expenses.html",
                 "personal_expense_categories.html",
                 "personal_expense_void.html"],
    "settings": ["settings.html"],
}

FILE_TO_DOMAIN = {f: d for d, fs in DOMAIN_OF.items() for f in fs}


def git_mv(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.exists(dst):
        return
    r = subprocess.run(["git", "mv", src, dst], cwd=BASE,
                       capture_output=True, text=True)
    if r.returncode != 0:  # not tracked yet or git unavailable: plain move
        os.rename(src, dst)


def read_keep(path):
    with open(path, "r", encoding="utf-8", newline="") as f:
        return f.read()


def write_keep(path, text):
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def main():
    check_only = "--check" in sys.argv
    # 0. sanity: every template file has exactly one domain
    flat = [f for f in os.listdir(TPL)
            if os.path.isfile(os.path.join(TPL, f))]
    nested = []
    for d in os.listdir(TPL):
        dd = os.path.join(TPL, d)
        if os.path.isdir(dd):
            nested += [f for f in os.listdir(dd)
                       if os.path.isfile(os.path.join(dd, f))]
    all_files = set(flat) | set(nested)
    assert set(FILE_TO_DOMAIN) == all_files, (
        "mapping mismatch: "
        f"unmapped={sorted(all_files - set(FILE_TO_DOMAIN))} "
        f"missing={sorted(set(FILE_TO_DOMAIN) - all_files)}")
    assert sum(len(v) for v in DOMAIN_OF.values()) == 84

    # 1. move templates
    for fname in flat:
        if fname in FILE_TO_DOMAIN:
            dst = os.path.join(TPL, FILE_TO_DOMAIN[fname], fname)
            if not check_only:
                git_mv(os.path.join(TPL, fname), dst)

    # 2. rewrite render_template targets in route modules
    # (regex spans newlines: some calls put the template name on the next line)
    for fn in sorted(os.listdir(ROUTES)):
        if not fn.endswith(".py") or fn == "__init__.py":
            continue
        path = os.path.join(ROUTES, fn)
        text = read_keep(path)
        new = text
        for fname, domain in sorted(FILE_TO_DOMAIN.items()):
            new = re.sub(
                r"render_template\(\s*(['\"])" + re.escape(fname) + r"\1",
                lambda m: f"render_template({m.group(1)}{domain}/{fname}"
                          f"{m.group(1)}",
                new)
        if new != text and not check_only:
            write_keep(path, new)

    # 3. rewrite extends/include inside templates
    for domain, files in DOMAIN_OF.items():
        for fname in files:
            path = os.path.join(TPL, domain, fname)
            if not os.path.exists(path):
                continue  # not moved yet (check mode pre-move)
            text = read_keep(path)
            new = text
            for q in ("'", '"'):
                new = new.replace(f"extends {q}base.html{q}",
                                  f"extends {q}shared/base.html{q}")
                new = new.replace(f"include {q}_pagination.html{q}",
                                  f"include {q}shared/_pagination.html{q}")
            if new != text and not check_only:
                write_keep(path, new)

    # 4. split static JS core bundle (mechanical, section-preserving)
    old_js = os.path.join(STATIC_JS, "hdc.js")
    core_dir = os.path.join(STATIC_JS, "core")
    pages_dir = os.path.join(STATIC_JS, "pages")
    sections = [("layout.js", 5, 20, "Sidebar collapse + persistence"),
                ("theme.js", 22, 39, "Dark/light theme toggle"),
                ("forms.js", 41, 65,
                 "Alert auto-dismiss + double-submit guard"),
                ("combo.js", 67, 244,
                 "HDCComboList searchable combo widget")]
    if os.path.exists(old_js):
        with open(old_js, encoding="utf-8", newline="") as f:
            js_lines = f.read().splitlines(keepends=True)
        assert len(js_lines) == 245, f"hdc.js changed ({len(js_lines)} lines)"
        if not check_only:
            os.makedirs(core_dir, exist_ok=True)
            os.makedirs(pages_dir, exist_ok=True)
            for fname, start, end, desc in sections:
                body = "".join(js_lines[start - 1:end])
                content = (
                    f"/* HDC ERP — core/{fname}: {desc} "
                    f"(split from hdc.js; loaded by base.html) */\n"
                    "(function () {\n    'use strict';\n\n"
                    f"{body}"
                    "})();\n")
                with open(os.path.join(core_dir, fname), "w",
                          encoding="utf-8", newline="") as f:
                    f.write(content)
            # move page-specific bundle
            old_page = os.path.join(STATIC_JS, "project_estimation.js")
            if os.path.exists(old_page):
                git_mv(old_page, os.path.join(pages_dir,
                                              "project_estimation.js"))
            # retire the monolith bundle (git rm keeps history)
            r = subprocess.run(["git", "rm", "-q", old_js], cwd=BASE,
                               capture_output=True, text=True)
            if r.returncode != 0:
                os.remove(old_js)

    # 5. update script references in templates
    base_html = os.path.join(TPL, "shared", "base.html")
    if os.path.exists(base_html):
        text = read_keep(base_html)
        new = text.replace(
            '<script src="/hdc_static/js/hdc.js"></script>',
            '<script src="/hdc_static/js/core/layout.js"></script>\n'
            '<script src="/hdc_static/js/core/theme.js"></script>\n'
            '<script src="/hdc_static/js/core/forms.js"></script>\n'
            '<script src="/hdc_static/js/core/combo.js"></script>')
        if new != text and not check_only:
            write_keep(base_html, new)
    est_html = os.path.join(TPL, "estimation", "project_estimation.html")
    if os.path.exists(est_html):
        text = read_keep(est_html)
        new = text.replace("/hdc_static/js/project_estimation.js",
                           "/hdc_static/js/pages/project_estimation.js")
        if new != text and not check_only:
            write_keep(est_html, new)

    print("frontend reorganize: done" if not check_only else "check: OK")


if __name__ == "__main__":
    main()
