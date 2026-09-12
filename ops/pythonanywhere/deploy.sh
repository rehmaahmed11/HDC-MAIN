#!/usr/bin/env bash
# Safe PythonAnywhere deployment for HDC ERP.
#
# Updates *all tracked code* in the checkout and nothing else: the SQLite
# database, the estimation store, stage drawings and backups are never pulled
# from Git, never cleaned and never replaced.  That is what makes this script
# usable as the payload of a GitHub webhook (see ../../deploy_receiver.py) as
# well as by hand.
#
# Run on the PythonAnywhere Bash console:
#   bash /home/yourname/deploy_hdc.sh
#
# The webhook receiver runs it with HDC_DEPLOY_SHA, HDC_DEPLOY_STATE_FILE and
# HDC_PIP_AUTOSKIP set.
#
# Configuration is loaded from HDC_DEPLOY_ENV_FILE (or the default path
# documented in production.env.example).  That file must never be committed.
#
# Optional overrides honoured by this script:
#   HDC_DEPLOY_REF           ref to fetch (default: HDC_DEPLOY_BRANCH)
#   HDC_DEPLOY_REVISION      exact commit id to check out (rollback)
#   HDC_DEPLOY_FORCE         1 = redeploy even when already at that commit
#   HDC_DEPLOY_SOURCE        label recorded in the state file (console|webhook)
#   HDC_DEPLOY_STATE_FILE    JSON file updated with the result
#   HDC_PIP_AUTOSKIP         1 = skip pip install when requirements.txt is unchanged
#   HDC_DEPLOY_BOOT_CHECK    0 = skip the create_app()/login health check
#   HDC_DEPLOY_SYNTAX_CHECK  0 = skip compileall/layer/frontend checks
#   HDC_SKIP_PIP             1 = never run pip install
#   HDC_GIT_BIN              git binary to use
set -Eeuo pipefail

# Outcome reporting is wired up before anything can fail, so the webhook
# receiver is never left sitting on a "running" deploy that died on a missing
# config file.  PYBIN starts as any system python3 and is upgraded to the
# project virtualenv once that is known.
STATE_FILE="${HDC_DEPLOY_STATE_FILE:-}"
SOURCE="${HDC_DEPLOY_SOURCE:-console}"
PYBIN="$(command -v python3 || command -v python || true)"
LOCK_DIR="${HDC_DEPLOY_LOCK_DIR:-/tmp/hdc-erp-deploy.lock}"
GIT_BIN="${HDC_GIT_BIN:-git}"
LOCK_HELD=0
STATE_TMP=""
DEPLOYED_SHA=""
HEAD_SHA=""
PYTHON=""
PIP=""

log() { printf '%s %s\n' "[hdc-deploy]" "$*"; }

# Record the outcome for the webhook receiver.  A state problem must never
# change the deploy result, and nothing here prints the environment.
write_state() {
    local status="$1" message="$2" commit="${3:-}"
    [[ -n "$STATE_FILE" && -n "$PYBIN" ]] || return 0
    if ! STATE_TMP="$(mktemp "${TMPDIR:-/tmp}/hdc-deploy-state.XXXXXX" 2>/dev/null)"; then
        return 0
    fi
    if "$PYBIN" - "$STATE_FILE" "$STATE_TMP" "$status" "$message" "$commit" "$SOURCE" <<'PYTHON_STATE'
import json
import os
import sys
import time

path, tmp, status, message, commit, source = sys.argv[1:7]
state = {}
try:
    with open(path, encoding='utf-8') as handle:
        loaded = json.load(handle)
    if isinstance(loaded, dict):
        state = loaded
except (OSError, ValueError):
    state = {}
state.update({
    'status': status,
    'message': message,
    'sha': commit or state.get('sha', ''),
    'source': source,
    'finished_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
})
os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
with open(tmp, 'w', encoding='utf-8') as handle:
    json.dump(state, handle, indent=2, sort_keys=True)
    handle.write('\n')
os.replace(tmp, path)
PYTHON_STATE
    then
        log "state file updated ($status)"
    else
        log "WARNING: could not write the state file"
    fi
    if [[ -n "$STATE_TMP" ]]; then
        rm -f "$STATE_TMP"
        STATE_TMP=""
    fi
    return 0
}

on_exit() {
    local code=$?
    trap - EXIT INT TERM
    if [[ "$LOCK_HELD" == "1" ]]; then
        rmdir "$LOCK_DIR" 2>/dev/null || true
    fi
    if [[ -n "$STATE_TMP" ]]; then
        rm -f "$STATE_TMP"
        STATE_TMP=""
    fi
    if [[ $code -eq 0 ]]; then
        write_state ok "deployment completed" "$DEPLOYED_SHA"
    else
        write_state failed "deployment aborted (exit $code) - see the deploy log" \
            "$DEPLOYED_SHA"
    fi
    return "$code"
}
trap on_exit EXIT

ENV_FILE="${HDC_DEPLOY_ENV_FILE:-${HOME:-/root}/.config/hdc/production.env}"
if [[ ! -f "$ENV_FILE" ]]; then
    echo "Missing deployment environment file: $ENV_FILE" >&2
    echo "Copy ops/pythonanywhere/production.env.example outside the repo and edit it." >&2
    exit 1
fi

# Export all HDC_* values for the migration/startup process.
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

: "${HDC_APP_DIR:?HDC_APP_DIR is required}"
: "${HDC_INSTANCE_DIR:?HDC_INSTANCE_DIR is required}"
: "${HDC_DB_PATH:?HDC_DB_PATH is required}"
: "${HDC_VENV_PATH:?HDC_VENV_PATH is required}"

# The values captured before the env file was sourced are only good enough to
# report a failure; the file wins from here on.
BRANCH="${HDC_DEPLOY_BRANCH:-main}"
REF="${HDC_DEPLOY_REF:-$BRANCH}"
REVISION="${HDC_DEPLOY_REVISION:-}"
FORCE="${HDC_DEPLOY_FORCE:-0}"
SOURCE="${HDC_DEPLOY_SOURCE:-$SOURCE}"
STATE_FILE="${HDC_DEPLOY_STATE_FILE:-$STATE_FILE}"
LOCK_DIR="${HDC_DEPLOY_LOCK_DIR:-$LOCK_DIR}"
GIT_BIN="${HDC_GIT_BIN:-$GIT_BIN}"
BACKUP_DIR="${HDC_BACKUP_DIR:-$HDC_INSTANCE_DIR/backups/deployments}"
PYTHON="$HDC_VENV_PATH/bin/python"
PIP="$HDC_VENV_PATH/bin/pip"
if [[ -x "$PYTHON" ]]; then
    PYBIN="$PYTHON"
fi

if [[ ! -d "$HDC_INSTANCE_DIR" ]]; then
    if [[ "${HDC_ALLOW_NEW_DB:-0}" == "1" ]]; then
        mkdir -p "$HDC_INSTANCE_DIR"
    else
        echo "Required instance directory does not exist: $HDC_INSTANCE_DIR" >&2
        exit 1
    fi
fi
for path in "$HDC_APP_DIR" "$HDC_VENV_PATH"; do
    if [[ ! -d "$path" ]]; then
        echo "Required directory does not exist: $path" >&2
        exit 1
    fi
done
if [[ ! -x "$PYTHON" || ! -x "$PIP" ]]; then
    echo "Virtualenv is missing Python or pip: $HDC_VENV_PATH" >&2
    exit 1
fi
if ! "$GIT_BIN" --version >/dev/null 2>&1; then
    echo "Git is not available: $GIT_BIN" >&2
    exit 1
fi
if [[ "$HDC_DB_PATH" != /* || "$HDC_INSTANCE_DIR" != /* || "$HDC_APP_DIR" != /* ]]; then
    echo "HDC_APP_DIR, HDC_INSTANCE_DIR, and HDC_DB_PATH must be absolute paths." >&2
    exit 1
fi
if [[ ! -d "$HDC_APP_DIR/.git" ]]; then
    echo "Not a Git checkout: $HDC_APP_DIR" >&2
    exit 1
fi

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    echo "Another HDC deployment is already running: $LOCK_DIR" >&2
    exit 1
fi
LOCK_HELD=1

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$BACKUP_DIR"
MARKER="$BACKUP_DIR/last-successful-deploy"

# --------------------------------------------------------------------------
# 1. Fetch, then decide what would be deployed.
# --------------------------------------------------------------------------
cd "$HDC_APP_DIR"

log "fetching $REF from origin"
if ! "$GIT_BIN" fetch --prune origin "$REF" >/dev/null 2>&1; then
    echo "git fetch origin $REF failed.  On a free PythonAnywhere account the" >&2
    echo "remote must be an https:// URL (github.com is allowlisted; ssh:// is not)." >&2
    exit 1
fi

if [[ -n "$REVISION" ]]; then
    if ! "$GIT_BIN" cat-file -e "${REVISION}^{commit}" 2>/dev/null; then
        echo "Revision $REVISION is not available in this clone, so it cannot be" >&2
        echo "rolled back to.  Deploy a commit that this checkout has already seen." >&2
        exit 1
    fi
    TARGET_SHA="$("$GIT_BIN" rev-parse --verify "${REVISION}^{commit}")"
else
    TARGET_SHA="$("$GIT_BIN" rev-parse --verify "FETCH_HEAD^{commit}")"
fi
HEAD_SHA="$("$GIT_BIN" rev-parse --verify HEAD 2>/dev/null || echo none)"

log "target=$TARGET_SHA"
log "current=$HEAD_SHA branch=$BRANCH source=$SOURCE"

# Nothing to do when the checkout already sits on the pushed commit *and* the
# previous deploy of that commit succeeded.  This keeps GitHub redeliveries and
# duplicate pushes off the free tier's daily CPU budget.
if [[ "$HEAD_SHA" == "$TARGET_SHA" && "$FORCE" != "1" && -f "$MARKER" \
      && "$(cat "$MARKER" 2>/dev/null || true)" == "$TARGET_SHA" ]]; then
    log "already deployed at $TARGET_SHA - nothing to do (set HDC_DEPLOY_FORCE=1 to redo)"
    DEPLOYED_SHA="$TARGET_SHA"
    exit 0
fi

# --------------------------------------------------------------------------
# 2. Database safety: refuse to let Git own live data in either direction.
#    This is the "except databases" guarantee, checked rather than assumed.
# --------------------------------------------------------------------------
if [[ -f "$HDC_APP_DIR/scripts/check_db_safety.py" ]]; then
    log "checking the incoming commit for database/backup files"
    "$PYTHON" "$HDC_APP_DIR/scripts/check_db_safety.py" \
        --root "$HDC_APP_DIR" --ref "$TARGET_SHA" \
        --protect "$HDC_DB_PATH" --protect "$HDC_INSTANCE_DIR"
    log "checking the current index for database/backup files"
    "$PYTHON" "$HDC_APP_DIR/scripts/check_db_safety.py" \
        --root "$HDC_APP_DIR" --worktree --quiet \
        --protect "$HDC_DB_PATH" --protect "$HDC_INSTANCE_DIR"
fi

if [[ -n "$("$GIT_BIN" status --porcelain --untracked-files=no)" ]]; then
    echo "Tracked local changes found in the application checkout. Aborting." >&2
    "$GIT_BIN" status --short
    exit 1
fi

# --------------------------------------------------------------------------
# 3. Back up data before any code moves.  SQLite's backup API creates a
#    consistent snapshot even when the web worker has the database open.
# --------------------------------------------------------------------------
if [[ -f "$HDC_DB_PATH" ]]; then
    DB_BACKUP="$BACKUP_DIR/hdc_erp-$STAMP.db"
    log "backing up database to $DB_BACKUP"
    "$PYTHON" - "$HDC_DB_PATH" "$DB_BACKUP" <<'PYTHON_BACKUP'
import sqlite3
import sys

source = sqlite3.connect(sys.argv[1])
destination = sqlite3.connect(sys.argv[2])
try:
    source.backup(destination)
    destination.commit()
    check = destination.execute("PRAGMA integrity_check").fetchone()[0]
    if str(check).lower() != "ok":
        raise SystemExit(f"SQLite integrity check failed: {check}")
finally:
    destination.close()
    source.close()
print("Database backup and integrity check completed")
PYTHON_BACKUP
    DB_SNAPSHOT="$("$PYTHON" -c 'import os,sys; s=os.stat(sys.argv[1]); print(f"{s.st_size}:{int(s.st_mtime)}")' "$HDC_DB_PATH" 2>/dev/null || echo unknown)"
else
    if [[ "${HDC_ALLOW_NEW_DB:-0}" != "1" ]]; then
        echo "Database does not exist: $HDC_DB_PATH" >&2
        echo "Place the old DB there first, or explicitly set HDC_ALLOW_NEW_DB=1 for a new installation." >&2
        exit 1
    fi
    DB_SNAPSHOT="absent"
    log "no existing DB found; an empty database will be created by the app"
fi

# Preserve non-database instance data needed by the application.
if [[ -f "$HDC_INSTANCE_DIR/project_estimations.json" ]]; then
    cp "$HDC_INSTANCE_DIR/project_estimations.json" \
       "$BACKUP_DIR/project_estimations-$STAMP.json"
fi
if [[ -d "$HDC_INSTANCE_DIR/stage_drawings" ]]; then
    tar -czf "$BACKUP_DIR/stage_drawings-$STAMP.tar.gz" \
        -C "$HDC_INSTANCE_DIR" stage_drawings
fi

# --------------------------------------------------------------------------
# 4. Move the code.  Deliberately no `git clean` and no whole-worktree reset:
#    untracked files such as hdc_instance/ must survive every deploy.
# --------------------------------------------------------------------------
if [[ -n "$REVISION" ]]; then
    log "rolling the working tree back to $TARGET_SHA (detached HEAD)"
    "$GIT_BIN" checkout -q --detach "$TARGET_SHA"
    DEPLOYED_SHA="$TARGET_SHA"
else
    if "$GIT_BIN" show-ref --verify --quiet "refs/heads/$BRANCH"; then
        "$GIT_BIN" checkout -q "$BRANCH"
    else
        "$GIT_BIN" checkout -q -b "$BRANCH" FETCH_HEAD
    fi
    if ! "$GIT_BIN" merge -q --ff-only "$TARGET_SHA"; then
        echo "Local $BRANCH has diverged from $TARGET_SHA, so a fast-forward is" >&2
        echo "impossible.  Land the change on GitHub (the PythonAnywhere checkout" >&2
        echo "must not carry local commits), then re-run the deploy." >&2
        exit 1
    fi
    DEPLOYED_SHA="$TARGET_SHA"
fi
log "checked out $DEPLOYED_SHA"

# --------------------------------------------------------------------------
# 5. Dependencies, checks, migrations.
# --------------------------------------------------------------------------
if [[ "${HDC_SKIP_PIP:-0}" == "1" ]]; then
    log "skipping pip install (HDC_SKIP_PIP=1)"
elif [[ "${HDC_PIP_AUTOSKIP:-0}" == "1" && "$HEAD_SHA" != "none" ]] \
     && "$GIT_BIN" diff --quiet "$HEAD_SHA" "$DEPLOYED_SHA" -- requirements.txt; then
    log "requirements.txt unchanged since $HEAD_SHA - skipping pip install"
else
    log "installing Python requirements"
    "$PIP" install -r requirements.txt
fi

if [[ "${HDC_DEPLOY_SYNTAX_CHECK:-1}" == "1" ]]; then
    log "running syntax and layer checks"
    "$PYTHON" -m compileall -q hdc hdc_erp.py wsgi.py deploy_receiver.py
    "$PYTHON" scripts/check_layers.py
    "$PYTHON" scripts/reorganize_frontend.py --check
    if command -v node >/dev/null 2>&1; then
        find static -type f -name '*.js' -print0 | xargs -0 -r -n1 node --check
    else
        log "node is not installed; the JavaScript syntax check is covered by GitHub CI"
    fi
else
    log "skipping syntax and layer checks (HDC_DEPLOY_SYNTAX_CHECK=0)"
fi

if [[ "${HDC_DEPLOY_BOOT_CHECK:-1}" == "1" ]]; then
    log "starting the app once to apply schema heals and migrations"
    "$PYTHON" - <<'PYTHON_BOOT'
from hdc.app import create_app

app = create_app()
with app.test_client() as client:
    response = client.get('/hdc/login')
    if response.status_code != 200:
        raise SystemExit(f'Login health check failed: HTTP {response.status_code}')
print('Application startup, migrations, and login health check completed')
PYTHON_BOOT
else
    log "skipping the application boot check (HDC_DEPLOY_BOOT_CHECK=0)"
fi

# The webhook receiver lives inside this repository, so it must be proven
# importable before a reload, and a copy of the working one is kept outside the
# checkout.  If a later commit breaks deploy_receiver.py, /deploy still answers
# from that copy (see wsgi_deploy_with_receiver.example.py).
if [[ -f "$HDC_APP_DIR/deploy_receiver.py" ]]; then
    if ! "$PYTHON" -c "import deploy_receiver" >/dev/null 2>&1; then
        echo "deploy_receiver.py is not importable; refusing to reload." >&2
        exit 1
    fi
    if [[ "${HDC_DEPLOY_RECEIVER_BACKUP:-1}" == "1" && -n "${HOME:-}" ]]; then
        cp -f "$HDC_APP_DIR/deploy_receiver.py" "$HOME/hdc_deploy_receiver.py" \
            || log "WARNING: could not save the receiver copy to \$HOME"
    fi
fi

# --------------------------------------------------------------------------
# 6. Confirm the database is still ours, then reload the site.
# --------------------------------------------------------------------------
if [[ -f "$HDC_DB_PATH" ]]; then
    DB_NOW="$("$PYTHON" -c 'import os,sys; s=os.stat(sys.argv[1]); print(f"{s.st_size}:{int(s.st_mtime)}")' "$HDC_DB_PATH" 2>/dev/null || echo unknown)"
    # The live site keeps writing to the DB, so a change here is normal; it is
    # logged because Git never owns this file and the deploy must not either.
    log "database snapshot $DB_SNAPSHOT -> $DB_NOW (path $HDC_DB_PATH)"
fi

printf '%s\n' "$DEPLOYED_SHA" > "$MARKER"

# PythonAnywhere reloads when its WSGI file is touched.  Leave this empty if
# you prefer to click Reload manually in the Web tab.
if [[ -n "${HDC_WSGI_FILE:-}" ]]; then
    if [[ ! -f "$HDC_WSGI_FILE" ]]; then
        echo "Configured HDC_WSGI_FILE does not exist: $HDC_WSGI_FILE" >&2
        exit 1
    fi
    touch "$HDC_WSGI_FILE"
    log "PythonAnywhere reload requested by touching $HDC_WSGI_FILE"
else
    log "deployment completed.  Click Reload in the PythonAnywhere Web tab."
fi

log "done: $DEPLOYED_SHA"
exit 0
