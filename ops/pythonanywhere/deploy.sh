#!/usr/bin/env bash
# Safe PythonAnywhere deployment for HDC ERP.
#
# Run on the PythonAnywhere Bash console:
#   bash /home/yourname/deploy_hdc.sh
#
# Configuration is loaded from HDC_DEPLOY_ENV_FILE (or the default path
# documented in production.env.example).  That file must never be committed.
set -Eeuo pipefail

ENV_FILE="${HDC_DEPLOY_ENV_FILE:-$HOME/.config/hdc/production.env}"
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

BRANCH="${HDC_DEPLOY_BRANCH:-main}"
BACKUP_DIR="${HDC_BACKUP_DIR:-$HDC_INSTANCE_DIR/backups/deployments}"
LOCK_DIR="${HDC_DEPLOY_LOCK_DIR:-/tmp/hdc-erp-deploy.lock}"
PYTHON="$HDC_VENV_PATH/bin/python"
PIP="$HDC_VENV_PATH/bin/pip"

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
cleanup() { rmdir "$LOCK_DIR" 2>/dev/null || true; }
trap cleanup EXIT

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$BACKUP_DIR"

# SQLite's backup API creates a consistent snapshot even when the web worker
# has the database open.  This is safer than copying a live WAL database file.
if [[ -f "$HDC_DB_PATH" ]]; then
    DB_BACKUP="$BACKUP_DIR/hdc_erp-$STAMP.db"
    echo "Backing up database to $DB_BACKUP"
    "$PYTHON" - "$HDC_DB_PATH" "$DB_BACKUP" <<'PY'
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
PY
else
    if [[ "${HDC_ALLOW_NEW_DB:-0}" != "1" ]]; then
        echo "Database does not exist: $HDC_DB_PATH" >&2
        echo "Place the old DB there first, or explicitly set HDC_ALLOW_NEW_DB=1 for a new installation." >&2
        exit 1
    fi
    echo "No existing DB found; an empty database will be created by the app."
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

cd "$HDC_APP_DIR"
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
    echo "Tracked local changes found in the application checkout. Aborting." >&2
    git status --short
    exit 1
fi

echo "Fetching origin/$BRANCH"
git fetch --prune origin "$BRANCH"
if git show-ref --verify --quiet "refs/heads/$BRANCH"; then
    git checkout "$BRANCH"
else
    git checkout -b "$BRANCH" --track "origin/$BRANCH"
fi
git pull --ff-only origin "$BRANCH"

if [[ "${HDC_SKIP_PIP:-0}" != "1" ]]; then
    echo "Installing Python requirements"
    "$PIP" install -r requirements.txt
fi

echo "Running syntax and layer checks"
"$PYTHON" -m compileall -q hdc hdc_erp.py wsgi.py
"$PYTHON" scripts/check_layers.py
"$PYTHON" scripts/reorganize_frontend.py --check
if command -v node >/dev/null 2>&1; then
    find static -type f -name '*.js' -print0 | xargs -0 -r -n1 node --check
else
    echo "Node is not installed; JavaScript syntax check is covered by GitHub CI."
fi

echo "Starting the app once to apply schema heals and migrations"
"$PYTHON" - <<'PY'
from hdc.app import create_app

app = create_app()
with app.test_client() as client:
    response = client.get('/hdc/login')
    if response.status_code != 200:
        raise SystemExit(f'Login health check failed: HTTP {response.status_code}')
print('Application startup, migrations, and login health check completed')
PY

# PythonAnywhere can reload when its WSGI file is touched.  Leave this empty
# if you prefer to click Reload manually in the Web tab.
if [[ -n "${HDC_WSGI_FILE:-}" ]]; then
    if [[ ! -f "$HDC_WSGI_FILE" ]]; then
        echo "Configured HDC_WSGI_FILE does not exist: $HDC_WSGI_FILE" >&2
        exit 1
    fi
    touch "$HDC_WSGI_FILE"
    echo "PythonAnywhere reload requested by touching $HDC_WSGI_FILE"
else
    echo "Deployment completed. Click Reload in the PythonAnywhere Web tab."
fi
