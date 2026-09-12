#!/usr/bin/env python3
"""Guard: code goes through Git, data does not.

HDC's deploy path is ``git pull`` on PythonAnywhere, and the production
database plus the instance files (``hdc_erp.db``, ``project_estimations.json``,
``stage_drawings/``, backups) live beside or inside that checkout.  This script
makes "update all code except the databases" a checked property instead of a
hope:

* no database/backup/archive file may be **tracked** in Git -- if one were, a
  deploy would overwrite live data with the copy committed to the repo;
* every runtime path handed to ``--protect`` must be untracked (and ideally
  git-ignored) so ``git checkout``/``git pull`` cannot touch it.

Modes:
    --worktree          inspect the index (default)
    --ref <rev>         inspect a committed tree instead, e.g. origin/main, so
                        a deploy can reject an unsafe *incoming* commit

Used by ``ops/pythonanywhere/deploy.sh`` before anything is checked out, and by
GitHub CI so the repo itself can never regress.

Usage:
    python3 scripts/check_db_safety.py [--worktree] [--ref REV]
                                       [--protect PATH ...] [--root DIR]
"""

import argparse
import os
import subprocess
import sys

# Files that must never live in the repository: data, deltas, backups, blobs.
HARD_FAIL_SUFFIXES = (
    '.db', '.db-wal', '.db-shm', '.db-journal', '.sqlite', '.sqlite3',
    '.zip', '.tar', '.tar.gz', '.tgz', '.bak', '.dump',
)
# Path fragments that mark a runtime/data location rather than source.
HARD_FAIL_FRAGMENTS = ('/hdc_instance/', '/instance/', '/backups/')
# Suspicious enough to report, but not blocking (e.g. a fixtures .sql file).
WARN_SUFFIXES = ('.sql', '.env', '.pem', '.key', '.p12', '.crt')
WARN_FRAGMENTS = ('/stage_drawings/',)


def run_git(root, args):
    """Run git in ``root``; return (returncode, stdout) without raising."""
    try:
        proc = subprocess.run(
            ['git', '-C', root] + args,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    except OSError as exc:
        return 127, f'{exc}'.encode('utf-8')
    return proc.returncode, proc.stdout.decode('utf-8', 'replace')


def list_tracked(root, ref=None):
    """Tracked paths for the index or for a committed tree."""
    if ref:
        code, out = run_git(root, ['ls-tree', '-r', '--name-only', ref])
        if code != 0:
            return None, out.strip()
        return [line.strip() for line in out.splitlines() if line.strip()], ''
    code, out = run_git(root, ['ls-files', '-z'])
    if code != 0:
        return None, out.strip()
    return [p for p in out.split('\0') if p], ''


def classify(paths):
    """Split tracked paths into hard failures and warnings."""
    hard, warn = [], []
    for rel in paths:
        lowered = '/' + rel.replace(os.sep, '/').lower()
        if not lowered.startswith('/'):
            lowered = '/' + lowered
        ends_hard = lowered.endswith(HARD_FAIL_SUFFIXES)
        has_hard_fragment = any(fragment in lowered for fragment in HARD_FAIL_FRAGMENTS)
        if ends_hard or has_hard_fragment:
            hard.append(rel)
        elif lowered.endswith(WARN_SUFFIXES) or any(
                fragment in lowered for fragment in WARN_FRAGMENTS):
            warn.append(rel)
    return hard, warn


def check_protected(root, targets):
    """Verify runtime paths cannot be rewritten by a Git operation."""
    problems, notes = [], []
    code, top_out = run_git(root, ['rev-parse', '--show-toplevel'])
    top = os.path.realpath(top_out.strip()) if code == 0 else os.path.realpath(root)
    for raw in targets:
        if not raw:
            continue
        path = os.path.realpath(os.path.expanduser(raw))
        try:
            relative = os.path.relpath(path, top)
        except ValueError:  # pragma: no cover - different drive on Windows
            relative = ''
        if not relative or relative.startswith('..'):
            notes.append(f'{path}: outside the repository -- Git can never touch it')
            continue
        code, _ = run_git(root, ['ls-files', '--error-unmatch', '--', relative])
        if code == 0:
            problems.append(
                f'{path}: TRACKED at {relative} -- a deploy would overwrite live '
                f'data; run `git rm --cached {relative}` and move it out of the checkout')
            continue
        ignored_code, _ = run_git(root, ['check-ignore', '-q', '--', relative])
        if ignored_code == 0:
            notes.append(f'{path}: inside the checkout at {relative}, untracked and '
                         f'git-ignored -- safe from pull/checkout')
        else:
            notes.append(f'{path}: inside the checkout at {relative}, untracked but NOT '
                         f'git-ignored -- add it to .gitignore so it is never committed '
                         f'and never removed by a clean')
    return problems, notes


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--root', default=os.path.abspath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')))
    parser.add_argument('--worktree', action='store_true',
                        help='inspect the index (default)')
    parser.add_argument('--ref', default='',
                        help='inspect this committed tree instead of the index')
    parser.add_argument('--protect', action='append', default=[],
                        metavar='PATH', help='runtime path that Git must not own')
    parser.add_argument('--quiet', action='store_true')
    parser.add_argument('--no-fail', action='store_true',
                        help='report only; always exit 0')
    args = parser.parse_args(argv)

    root = os.path.abspath(args.root)
    if not os.path.isdir(os.path.join(root, '.git')):
        print(f'DB safety: {root} is not a Git repository', file=sys.stderr)
        return 0 if args.no_fail else 1

    paths, error = list_tracked(root, args.ref or None)
    if paths is None:
        print(f'DB safety: git could not list {args.ref or "the index"}: {error}',
              file=sys.stderr)
        return 0 if args.no_fail else 1

    hard, warn = classify(paths)
    problems, notes = check_protected(root, args.protect)
    hard.extend(problems)

    target = f'tree {args.ref}' if args.ref else 'index'
    if not args.quiet:
        print(f'DB safety check ({target}, {len(paths)} tracked files)')
        for note in notes:
            print(f'  ok:      {note}')
        for rel in warn:
            print(f'  warning: {rel} looks like data or a secret in the repository')
    if hard:
        print(f'DB safety FAILED -- {len(hard)} item(s) would let Git own production '
              f'data:', file=sys.stderr)
        for rel in hard:
            print(f'  tracked: {rel}' if not rel.startswith('/') else f'  {rel}',
                  file=sys.stderr)
        print('Databases, WAL files and backups must live outside Git (see '
              '.gitignore and ops/pythonanywhere/README.md).', file=sys.stderr)
        return 0 if args.no_fail else 1
    if not args.quiet:
        print('DB safety: OK -- no database, backup or archive files in Git')
    return 0


if __name__ == '__main__':
    sys.exit(main())
