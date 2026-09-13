# Auto-deploy on PythonAnywhere (free account) — one command, no hand-editing

`install_deploy_hook.py` wires up the push-to-deploy webhook on the server.
It exists because the two files it installs (`deploy_hook.py`,
`wsgi_dispatch_snippet.py`) used to need three paths edited by hand and a
copy-paste into `/var/www` — the step everyone missed.

```bash
cd ~/HDC-MAIN
python3 ops/pythonanywhere/install_deploy_hook.py     # then click Reload once
```

That's the whole server side. It prints the exact GitHub webhook form values
at the end, including your real Payload URL and the secret to paste.

## What it does

| Step | Detail |
| --- | --- |
| Detects your username | from `$USER`, or from the WSGI filename (`bob_pythonanywhere_com_wsgi.py` → `bob`) |
| Detects the repo | the folder the code actually lives in — `deploy_hook.py` sits at the repo root, so nothing is typed in |
| Detects the WSGI file | `/var/www/<you>_pythonanywhere_com_wsgi.py`, falling back to the single `*_wsgi.py` in `/var/www` |
| Creates `deploy_secret.txt` | random 32-char key, `chmod 600`, gitignored. An existing one is kept, never regenerated |
| Writes the WSGI file | copies `wsgi_dispatch_snippet.py` verbatim under a marker-delimited header that pins the repo path |
| Keeps your settings | any `os.environ[...] = ...` / virtualenv lines in the old WSGI file are carried over; a stale `HDC_REPO_DIR` line is replaced by the pin |
| Backs up first | `<wsgi>.bak-YYYYmmdd-HHMMSS`, restorable with `--restore` |
| Syntax-checks | the file it just wrote is compiled before it reports success |
| Idempotent | re-running reports `unchanged` and writes nothing |

## GitHub side (values the installer prints)

```
Payload URL  : https://<you>.pythonanywhere.com/deploy
Content type : application/json
Secret       : <printed by the installer>
SSL          : Enable SSL verification
Events       : Just the push event
Active       : ticked
```

Test it:

```bash
git commit --allow-empty -m "test deploy hook" && git push origin main
```

Then open `https://<you>.pythonanywhere.com/deploy` — expect
`OK deployed <hash>`. `/deploy/health` and `/deploy/status` are the same page.

## Modes

| Flag | Effect |
| --- | --- |
| `--check` | diagnose only, write nothing. Paste this output when asking for help |
| `--dry-run` | show the exact WSGI file it would write, write nothing |
| `--force` | rewrite the WSGI file even when it is already installed |
| `--restore` | put the newest `.bak-*` back (then Reload) |
| `--print-secret` | print only the secret line |
| `--username`, `--repo`, `--wsgi-file`, `--var-www`, `--domain`, `--branch` | override a detection guess |

Overrides are also available as environment variables to the hook itself:
`HDC_PA_USERNAME`, `HDC_REPO_DIR`, `HDC_WSGI_FILE`, `HDC_DEPLOY_SECRET_FILE`,
`HDC_DEPLOY_BRANCH`.

## What `/deploy` shows

A GET needs no secret and prints the live configuration, so a broken deploy
says what is broken instead of just failing:

```
HDC deploy hook

repo      : /home/bob/HDC-MAIN
git       : ok, branch main, HEAD a1b2c3d, clean
wsgi file : /var/www/bob_pythonanywhere_com_wsgi.py (found)
secret    : deploy_secret.txt present (32 chars)
branch    : main
log file  : /home/bob/HDC-MAIN/deploy.log

--- last deploys ---
2026-09-13 14:02:11  OK deployed a1b2c3d: Fast-forward
```

## Failure cookbook

| Symptom | Cause → fix |
| --- | --- |
| `/deploy` → 404 | WSGI file not written or not reloaded → run the installer, then Reload |
| `/deploy` says `wsgi file ... MISSING` | the hook is running but the dispatch file moved → re-run the installer |
| `/deploy` says `secret ... MISSING` | `deploy_secret.txt` absent → run the installer (or `--print-secret`) |
| `401 bad signature` | GitHub webhook Secret ≠ `deploy_secret.txt` → copy the printed line exactly |
| `503` + "deploy_secret.txt is missing" | same mismatch, caught before any git runs |
| `500` + `git stash` in the reply | someone edited code on the server → the reply names the exact command; the edits stay on disk |
| `500` + `reset --hard` in the reply | server history diverged from GitHub → the reply names the exact command |
| `deployed <hash> (WSGI file not found ...)` | code pulled, app not reloaded → re-run the installer, then Reload |
| Green/red in GitHub → Settings → Webhooks → Recent Deliveries | the response body above is shown there verbatim |

A failed pull never half-deploys: `git pull --ff-only` is used, so the
checkout either fast-forwards cleanly or stays exactly as it was, and the
reason lands in `deploy.log` and in the HTTP reply.

## Safety

- The secret is a shared password between GitHub and this file, not a
  PythonAnywhere API token; it is gitignored and mode `0600`.
- The hook never runs `git reset`, `git clean` or any destructive command.
- Databases and `hdc_instance/` are outside Git, so a deploy cannot touch them
  (`scripts/check_db_safety.py` in CI fails any commit that tries).
- The dispatch imports the real app **lazily and defensively**: if the app
  fails to start, `/deploy` still answers with the log and the traceback —
  which is the page you need most when the site is down.

## Tests

```bash
python3 tests/test_deploy_hook.py    # hook gating, hints, real-git end-to-end
python3 tests/test_pa_setup.py       # installer CLI + the WSGI file it writes
```

`tests/test_pa_setup.py` runs the real CLI against a throwaway repo and a
throwaway `/var/www`, then *executes* the generated WSGI file to prove
`/deploy` reaches the hook, everything else reaches the app, and a push →
signed webhook → fast-forward → reload cycle works end to end.
