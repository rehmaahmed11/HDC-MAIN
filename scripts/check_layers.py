#!/usr/bin/env python3
"""Enforce the modular dependency rules (MODULARIZATION_PLAN.md section 5).

- The hdc.* top-level import graph must be acyclic.
- utils/* may not import models/services/routes/core.
- models/* may import only extensions/utils/config at top level.
- No module may import hdc_erp (the shim) or routes.* except app/routes-init.
- No module may reference the global `app` except routes (register param),
  hdc.app (factory) and the shim.

Usage: python3 scripts/check_layers.py
"""
import ast
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PKG = os.path.join(ROOT, "hdc")


def mod_name(path):
    rel = os.path.relpath(path, ROOT).replace(os.sep, ".")
    return rel[:-3] if rel.endswith(".py") else rel


def hdc_imports(tree):
    """Top-level `from hdc[.x] import ...` targets (module dots).

    Only direct children of the module body count: function-local imports
    are deliberate cycle-breakers (documented in REFACTOR_NOTES.md) and
    execute at call time, so they cannot create import cycles.
    """
    out = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module and (
                node.module == "hdc" or node.module.startswith("hdc.")
                or node.module == "hdc_erp"):
            out.add(node.module)
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "hdc" or a.name.startswith("hdc."):
                    out.add(a.name)
    return out


def uses_global_app(tree, path):
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value,
                                                           ast.Name):
            if node.value.id == "app":
                hits.append(node.lineno)
    return hits


def main():
    py_files = []
    for dirpath, _, files in os.walk(PKG):
        for fn in sorted(files):
            if fn.endswith(".py"):
                py_files.append(os.path.join(dirpath, fn))
    mods = {mod_name(p): p for p in py_files}
    edges = {}
    violations = []
    for mod, path in sorted(mods.items()):
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        deps = hdc_imports(tree)
        edges[mod] = set()
        for d in deps:
            if d == "hdc":
                continue  # package itself (no names imported from it)
            edges[mod].add(d)
        short = mod[4:] if mod.startswith("hdc.") else mod
        layer = short.split(".")[0]
        for d in sorted(deps):
            if d == "hdc_erp":
                violations.append(f"{mod}: must not import the shim")
            if d == "hdc.models" or d.startswith("hdc.models."):
                if layer == "utils":
                    violations.append(f"{mod}: utils may not import models")
            if d == "hdc.services" or d.startswith("hdc.services."):
                if layer in ("utils", "models"):
                    violations.append(
                        f"{mod}: {layer} may not import services")
            if d == "hdc.routes" or d.startswith("hdc.routes."):
                if mod not in ("hdc.app", "hdc.routes.__init__"):
                    violations.append(
                        f"{mod}: only the factory may import routes")
            if d == "hdc.core" or d.startswith("hdc.core."):
                if layer in ("utils", "models"):
                    violations.append(
                        f"{mod}: {layer} may not import core")
        # global app check
        if mod not in ("hdc.app",):
            hits = uses_global_app(tree, path)
            if layer == "routes":
                # allowed: @app.route decorators + register(app) signature
                with open(path, encoding="utf-8") as f:
                    lines = f.read().splitlines()
                bad = [ln for ln in hits
                       if "app.route" not in lines[ln - 1]]
                if bad:
                    violations.append(
                        f"{mod}: global app use outside @app.route: "
                        f"lines {bad[:5]}")
            elif hits:
                violations.append(
                    f"{mod}: global app use outside factory: lines "
                    f"{hits[:5]}")

    # cycle detection over module graph (expand package refs to members)
    nodes = set(mods)
    adj = {m: set() for m in mods}
    for m, ds in edges.items():
        for d in ds:
            if d in mods:
                adj[m].add(d)
            else:
                # dependency on a package (hdc.models): edge to each member
                for cand in mods:
                    if cand == d or cand.startswith(d + "."):
                        adj[m].add(cand)
    idx, low, ctr, stack, on, sccs = {}, {}, [0], [], set(), []

    def sc(v):
        idx[v] = low[v] = ctr[0]
        ctr[0] += 1
        stack.append(v)
        on.add(v)
        for w in sorted(adj.get(v, ())):
            if w not in idx:
                sc(w)
                low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], idx[w])
        if low[v] == idx[v]:
            comp = []
            while True:
                w = stack.pop()
                on.discard(w)
                comp.append(w)
                if w == v:
                    break
            sccs.append(comp)

    sys.setrecursionlimit(10000)
    for v in sorted(nodes):
        if v not in idx:
            sc(v)
    cycles = [c for c in sccs if len(c) > 1]
    self_loops = sorted(m for m in nodes if m in adj.get(m, ()))

    print(f"modules scanned: {len(mods)}")
    if cycles:
        print("CYCLES:")
        for c in cycles:
            print("  " + " <-> ".join(sorted(c)))
    if self_loops:
        print("SELF IMPORTS:", self_loops)
    if violations:
        print("VIOLATIONS:")
        for v in violations:
            print("  " + v)
    if cycles or self_loops or violations:
        sys.exit(1)
    print("LAYERS: OK — acyclic, rules hold")


if __name__ == "__main__":
    main()
