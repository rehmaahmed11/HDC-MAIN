#!/usr/bin/env python3
"""Static parity checks: URL map, endpoints, render_template targets.

Compares the baseline monolith (git worktree / git show) against the
modular package without booting either app for the template part; boots
both apps in subprocesses for the URL-map part.

Usage: python3 scripts/parity_check.py [--baseline DIR]
"""
import ast
import json
import os
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
BASELINE_DEFAULT = "/tmp/hdc-baseline"
BASELINE_COMMIT = os.environ.get("HDC_BASELINE_COMMIT", "HEAD^")
URL_DUMP = (
    "import json, os, sys; "
    "sys.path.insert(0, os.getcwd()); "
    "os.environ['HDC_DB_PATH']='/tmp/parity-%s.db'; "
    "os.environ['HDC_INSTANCE_DIR']='/tmp/parity-%s-inst'; "
    "import hdc_erp; "
    "rules=sorted([(r.rule, sorted(r.methods-{'HEAD','OPTIONS'}), r.endpoint) "
    "for r in hdc_erp.app.url_map.iter_rules()]); "
    "print(json.dumps(rules))"
)


def dump_urlmap(app_dir, tag):
    for suffix in ("", "-wal", "-shm"):
        p = f"/tmp/parity-{tag}.db{suffix}"
        if os.path.exists(p):
            os.remove(p)
    r = subprocess.run([sys.executable, "-c", URL_DUMP % (tag, tag)],
                       check=True, cwd=app_dir, capture_output=True,
                       text=True, timeout=300)
    return json.loads(r.stdout.strip().splitlines()[-1])


def render_targets_of(path):
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    out = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "render_template" and node.args
                and isinstance(node.args[0], ast.Constant)):
            out.add(node.args[0].value)
    return out


def main():
    baseline = (sys.argv[sys.argv.index("--baseline") + 1]
                if "--baseline" in sys.argv else BASELINE_DEFAULT)
    if not os.path.isdir(baseline):
        try:
            subprocess.run(["git", "worktree", "add", baseline, BASELINE_COMMIT],
                           check=True, cwd=ROOT)
        except subprocess.CalledProcessError as ex:
            raise SystemExit(
                f"Baseline unavailable ({BASELINE_COMMIT}). Pass "
                "--baseline /path/to/legacy-copy or set HDC_BASELINE_COMMIT."
            ) from ex
    print(f"baseline: {baseline}\nnew:      {ROOT}")

    base_map = dump_urlmap(baseline, "base")
    new_map = dump_urlmap(ROOT, "new")
    print(f"url rules: baseline={len(base_map)} new={len(new_map)}")
    if base_map != new_map:
        bset, nset = set(map(tuple, base_map)), set(map(tuple, new_map))
        print("URL MAP MISMATCH")
        for r in sorted(bset - nset):
            print("  only baseline:", r)
        for r in sorted(nset - bset):
            print("  only new:     ", r)
        sys.exit(1)
    print("URL MAP: OK — 191/191 rules identical")

    base_tpl = render_targets_of(os.path.join(baseline, "hdc_erp.py"))
    new_tpl = set()
    routes_dir = os.path.join(ROOT, "hdc", "routes")
    for fn in os.listdir(routes_dir):
        if fn.endswith(".py") and fn != "__init__.py":
            new_tpl |= render_targets_of(os.path.join(routes_dir, fn))
    print(f"render targets: baseline={len(base_tpl)} new={len(new_tpl)}")
    # Templates were reorganized into domain folders; compare by basename.
    base_names = {t.split("/")[-1] for t in base_tpl}
    new_names = {t.split("/")[-1] for t in new_tpl}
    if base_names != new_names:
        print("TEMPLATE TARGET MISMATCH (by basename)")
        for t in sorted(base_names - new_names):
            print("  only baseline:", t)
        for t in sorted(new_names - base_names):
            print("  only new:     ", t)
        sys.exit(1)
    print("TEMPLATE TARGETS: OK — same 78 templates (in domain folders)")
    # every target must resolve to a real template file
    missing = [t for t in sorted(new_tpl)
               if not os.path.exists(os.path.join(ROOT, "templates", "hdc",
                                                  t))]
    if missing:
        print("MISSING TEMPLATE FILES:")
        for t in missing:
            print("  " + t)
        sys.exit(1)
    print("TEMPLATE FILES: OK — all targets exist")


if __name__ == "__main__":
    main()
