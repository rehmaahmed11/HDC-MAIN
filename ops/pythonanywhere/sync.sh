#!/usr/bin/env bash
# Fast PythonAnywhere sync for HDC ERP.
#
# deploy.sh is the careful path: it backs up the database, reinstalls
# requirements, runs every check, applies migrations, then reloads.
# This is the fast path for iterating on code during a working session:
#
#   bash /home/yourname/sync_hdc.sh
#
# It pulls the branch, reinstalls requirements ONLY if requirements.txt
# actually changed, runs the cheap syntax check, and requests a reload.
# No database backup and no migrations are triggered on purpose -- use
# deploy.sh for anything that touches the schema or before you trust a
# release.
#
# Options:
#   --branch NAME   sync a branch other than $HDC_DEPLOY_BRANCH (default: main)
#   --no-reload     pull and check, but do not request a PythonAnywhere reload
#   --pip           force `pip install -r requirements.txt`
#   --dry-run       show what would happen, change nothing
set -Eeuo pipefail

ENV_FILE="${HDC_DEPLOY_ENV_FILE:-$HOME/.config/hdc/production.env}"
BRANCH="${HDC_DEPLOY_BRANCH:-main}"
DO_RELOAD=1
FORCE_PIP=0
DRY_RUN=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --branch)   BRANCH="${2:?--branch needs a value}"; shift 2 ;;
        --no-reload) DO_RELOAD=0; shift ;;
        --pip)      FORCE_PIP=1; shift ;;
        --dry-run)  DRY_RUN=1; shift ;;
        -h|--help)  sed -n '2,/^set /p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done

# The env file is optional here: HDC_APP_DIR alone is enough to sync code.
if [[ -f "$ENV_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
else
    echo "note: no env file at $ENV_FILE; using HDC_APP_DIR/HDC_VENV_PATH only"
fi

: "${HDC_APP_DIR:?HDC_APP_DIR is required (set it or create $ENV_FILE)}"
PYTHON="${HDC_VENV_PATH:-}/bin/python"
[[ -x "$PYTHON" ]] || PYTHON="$(command -v python3)"

if [[ ! -d "$HDC_APP_DIR/.git" ]]; then
    echo "Not a Git checkout: $HDC_APP_DIR" >&2
    exit 1
fi

cd "$HDC_APP_DIR"

# A dirty tree would be silently clobbered by the pull, so refuse instead.
DIRTY="$(git status --porcelain --untracked-files=no)"
if [[ -n "$DIRTY" ]]; then
    echo "Uncommitted tracked changes in $HDC_APP_DIR. Commit or stash them first:" >&2
    git status --short --untracked-files=no >&2
    exit 1
fi

BEFORE="$(git rev-parse --short HEAD)"
BEFORE_REQ_HASH="$(git hash-object requirements.txt 2>/dev/null || echo none)"

if [[ "$DRY_RUN" == "1" ]]; then
    git fetch --prune origin "$BRANCH"
    echo "dry-run: $BEFORE -> $(git rev-parse --short "origin/$BRANCH") on $BRANCH"
    exit 0
fi

echo "Fetching origin/$BRANCH"
git fetch --prune origin "$BRANCH"

if git show-ref --verify --quiet "refs/heads/$BRANCH"; then
    git checkout --quiet "$BRANCH"
else
    git checkout --quiet -b "$BRANCH" --track "origin/$BRANCH"
fi
git pull --ff-only origin "$BRANCH"

AFTER="$(git rev-parse --short HEAD)"
AFTER_REQ_HASH="$(git hash-object requirements.txt 2>/dev/null || echo none)"

if [[ "$BEFORE" == "$AFTER" ]]; then
    echo "Already up to date at $AFTER."
else
    echo "Synced $BEFORE -> $AFTER on $BRANCH"
    git --no-pager log --oneline "$BEFORE..$AFTER" | sed 's/^/  /'
fi

if [[ "$FORCE_PIP" == "1" || "$BEFORE_REQ_HASH" != "$AFTER_REQ_HASH" ]]; then
    echo "requirements.txt changed (or --pip): installing"
    "$PYTHON" -m pip install -q -r requirements.txt
else
    echo "requirements.txt unchanged: skipping pip install"
fi

echo "Syntax check"
CHECK_TARGETS=()
for rel in hdc hdc_erp.py wsgi.py deploy_receiver.py scripts; do
    if [[ ! -e "$rel" ]]; then
        echo "Expected path is missing from $HDC_APP_DIR: $rel" >&2
        echo "Refusing to report a clean syntax check that never ran." >&2
        exit 1
    fi
    CHECK_TARGETS+=("$rel")
done
"$PYTHON" -m compileall -q "${CHECK_TARGETS[@]}"

if [[ "$DO_RELOAD" == "1" && -n "${HDC_WSGI_FILE:-}" ]]; then
    if [[ -f "$HDC_WSGI_FILE" ]]; then
        touch "$HDC_WSGI_FILE"
        echo "Reload requested: touched $HDC_WSGI_FILE"
    else
        echo "HDC_WSGI_FILE does not exist: $HDC_WSGI_FILE" >&2
        echo "Click Reload in the PythonAnywhere Web tab instead." >&2
    fi
elif [[ "$DO_RELOAD" == "1" ]]; then
    echo "No HDC_WSGI_FILE configured. Click Reload in the PythonAnywhere Web tab."
else
    echo "--no-reload: not touching the WSGI file."
fi

echo "Done. Running $AFTER"
