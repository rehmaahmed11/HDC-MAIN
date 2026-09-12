#!/usr/bin/env python3
"""Differential smoke test: baseline monolith vs modular package.

Runs tests/smoke_worker.py against both app copies with fresh scratch DBs
and diffs every status code, write result and table count.

Usage: python3 tests/smoke_test.py [--baseline DIR] [--new DIR]
"""
import json
import os
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
BASELINE_DEFAULT = "/tmp/hdc-baseline"


def run_worker(app_dir, tag):
    db_path = f"/tmp/hdc-smoke-{tag}.db"
    inst = f"/tmp/hdc-smoke-{tag}-inst"
    out = f"/tmp/hdc-smoke-{tag}.json"
    for p in (db_path, db_path + "-wal", db_path + "-shm", out):
        if os.path.exists(p):
            os.remove(p)
    subprocess.run([sys.executable, os.path.join(ROOT, "tests",
                                                 "smoke_worker.py"),
                    app_dir, db_path, inst, out],
                   check=True, cwd=app_dir,
                   capture_output=True, text=True, timeout=600)
    with open(out, encoding="utf-8") as f:
        return json.load(f)


def main():
    baseline = BASELINE_DEFAULT
    new = ROOT
    args = sys.argv[1:]
    if "--baseline" in args:
        baseline = args[args.index("--baseline") + 1]
    if "--new" in args:
        new = args[args.index("--new") + 1]
    if not os.path.isdir(baseline):
        print(f"baseline dir {baseline} missing; creating worktree...")
        subprocess.run(["git", "worktree", "add", baseline, "fab3e8c"],
                       check=True, cwd=ROOT)
    print(f"baseline: {baseline}")
    print(f"new:      {new}")
    base = run_worker(baseline, "base")
    newr = run_worker(new, "new")
    mismatches = []
    for bucket in ("reads", "writes", "counts"):
        keys = sorted(set(base[bucket]) | set(newr[bucket]))
        for k in keys:
            b, n = base[bucket].get(k, "<missing>"), newr[bucket].get(k,
                                                                     "<missing>")
            if b != n:
                mismatches.append((bucket, k, b, n))
    print(f"compared {sum(len(base[b]) for b in ('reads', 'writes', 'counts'))}"
          " values")
    print(f"baseline errors: {len(base['errors'])}, "
          f"new errors: {len(newr['errors'])}")
    # Sanity: the flow must genuinely succeed, not fail identically twice.
    sanity = {"writes:login": 302, "writes:POST project": 302,
              "writes:POST stage": 302, "writes:POST worker": 302,
              "writes:POST expense": 302, "writes:POST supplier": 302,
              "writes:POST material": 302, "writes:POST account(json)": 200,
              "writes:PUT fund Company Cash": 200,
              "writes:POST timekeeping": 302, "counts:Project": 1,
              "counts:Stage": 1, "counts:Worker": 1, "counts:Expense": 1,
              "counts:TimeEntry": 1}
    for key, want in sanity.items():
        bucket, k = key.split(":", 1)
        for tag, dataset in (("baseline", base), ("new", newr)):
            got = dataset[bucket].get(k, "<missing>")
            if got != want:
                print(f"SANITY FAIL [{tag}] {key}: want {want!r} "
                      f"got {got!r}")
                sys.exit(1)
    print("SANITY: OK — write flow genuinely persists on both apps")
    for e in newr["errors"][:5]:
        print("NEW-ERROR:", e[:300])
    if mismatches:
        print(f"MISMATCHES: {len(mismatches)}")
        for bucket, k, b, n in mismatches:
            print(f"  [{bucket}] {k}: baseline={b!r} new={n!r}")
        sys.exit(1)
    print("SMOKE PARITY: OK — all statuses, writes and counts identical")


if __name__ == "__main__":
    main()
