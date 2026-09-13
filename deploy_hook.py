"""
Minimal auto-deploy webhook for PythonAnywhere free accounts.

What this does, in plain terms:
  You push to GitHub -> GitHub calls this file -> this file runs
  `git pull` in your repo -> touches your WSGI file so PythonAnywhere
  reloads the app. That's the whole system. No PythonAnywhere API,
  no account token, nothing else running.

NOTHING IN THIS FILE NEEDS EDITING.  Every path is detected at import
time from where the file actually lives and who is running it:

  REPO_DIR   = the folder this file is in (it sits at the repo root)
  WSGI_FILE  = /var/www/<your-username>_pythonanywhere_com_wsgi.py,
               found by looking in /var/www for your wsgi file
  SECRET_FILE= <repo>/deploy_secret.txt

Each of those can still be overridden with an environment variable
(HDC_REPO_DIR, HDC_WSGI_FILE, HDC_DEPLOY_SECRET_FILE, HDC_PA_USERNAME,
HDC_DEPLOY_BRANCH) if your layout is unusual.

ONE-TIME SETUP on PythonAnywhere (after you `git clone` this repo there):

  1. Run the installer -- it writes the secret, writes the WSGI file for
     you, and prints the exact values to paste into GitHub:

         cd ~/HDC-MAIN
         python3 ops/pythonanywhere/install_deploy_hook.py

  2. Web tab -> big green Reload button (once).

  3. On GitHub: repo -> Settings -> Webhooks -> Add webhook, using the
     values the installer printed:
       Payload URL : https://<you>.pythonanywhere.com/deploy
       Content type: application/json
       Secret      : the string the installer printed
       Events      : "Just the push event"

  Done. Every future push to main now pulls + reloads automatically.

HOW YOU KNOW IT WORKED:
  Open https://<you>.pythonanywhere.com/deploy in a browser (GET
  request, no secret needed to just read the status) -- it prints the
  detected configuration plus the last few deploy attempts, e.g.:
      2026-09-13 14:02:11  OK deployed a1b2c3d: Fast-forward
  /deploy/health and /deploy/status do the same thing.
  If a push doesn't show up there within a few seconds, check
  GitHub -> Settings -> Webhooks -> your webhook -> "Recent Deliveries"
  to see the exact error GitHub got back.
"""

import getpass
import hashlib
import hmac
import json
import os
import subprocess
import time

VAR_WWW = "/var/www"
LOG_TAIL_LINES = 8
DISPATCH_MARKER = "HDC deploy hook dispatch"


# --------------------------------------------------------------------------
# Path detection (this is what removes the hand-editing)
# --------------------------------------------------------------------------
def detect_username():
    """Best guess at the PythonAnywhere username running this process."""
    for var in ("HDC_PA_USERNAME", "USER", "LOGNAME"):
        val = (os.environ.get(var) or "").strip()
        if val:
            return val
    try:
        return getpass.getuser()
    except Exception:
        return ""


def detect_repo_dir():
    """The repo root: the folder holding this file (it lives at the root)."""
    explicit = (os.environ.get("HDC_REPO_DIR") or "").strip()
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    return os.path.dirname(os.path.abspath(__file__))


def username_from_wsgi_name(path):
    """/var/www/bob_pythonanywhere_com_wsgi.py -> bob"""
    name = os.path.basename(path or "")
    for suffix in ("_pythonanywhere_com_wsgi.py", "_wsgi.py"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return ""


def detect_wsgi_file(var_www=VAR_WWW):
    """Find this account's WSGI file without being told the username.

    Order: explicit env var, the conventional name for the current user,
    then any single ``*_wsgi.py`` in /var/www (free accounts have one).
    Returns the conventional path even when nothing is found yet, so the
    status page can say "not installed" instead of crashing.
    """
    explicit = (os.environ.get("HDC_WSGI_FILE") or "").strip()
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))

    user = detect_username()
    conventional = os.path.join(var_www, f"{user}_pythonanywhere_com_wsgi.py")
    if os.path.exists(conventional):
        return conventional

    try:
        candidates = sorted(
            os.path.join(var_www, n)
            for n in os.listdir(var_www)
            if n.endswith("_wsgi.py")
        )
    except OSError:
        candidates = []

    if user:
        mine = [p for p in candidates if username_from_wsgi_name(p) == user]
        if len(mine) == 1:
            return mine[0]
    if len(candidates) == 1:
        return candidates[0]
    return conventional


REPO_DIR = detect_repo_dir()
WSGI_FILE = detect_wsgi_file()
BRANCH = (os.environ.get("HDC_DEPLOY_BRANCH") or "main").strip() or "main"
LOG_FILE = os.path.join(REPO_DIR, "deploy.log")
SECRET_FILE = (os.environ.get("HDC_DEPLOY_SECRET_FILE") or "").strip() or os.path.join(
    REPO_DIR, "deploy_secret.txt"
)


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def _secret():
    """Return the shared secret, or None when the file is missing/empty."""
    try:
        with open(SECRET_FILE) as f:
            val = f.read().strip()
    except OSError:
        return None
    return val or None


def _log(line):
    try:
        with open(LOG_FILE, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {line}\n")
    except OSError as ex:  # never let a logging problem break a deploy
        print(f"[deploy_hook] could not write {LOG_FILE}: {ex}")


def _is_git_checkout():
    return os.path.isdir(os.path.join(REPO_DIR, ".git"))


def _run_git(*args):
    return subprocess.check_output(
        ["git"] + list(args), cwd=REPO_DIR, stderr=subprocess.STDOUT
    ).decode(errors="replace")


def _verify(body, signature_header):
    secret = _secret()
    if not secret:
        return False
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    mac = hmac.new(secret.encode(), msg=body, digestmod=hashlib.sha256)
    expected = "sha256=" + mac.hexdigest()
    return hmac.compare_digest(expected, signature_header)


def _respond(start_response, status, text):
    start_response(status, [("Content-Type", "text/plain; charset=utf-8")])
    if isinstance(text, bytes):
        return [text]
    return [text.encode("utf-8")]


def _pull_hint(output):
    """Turn a git failure into the one command that fixes it."""
    low = output.lower()
    if "local changes" in low or "would be overwritten" in low:
        return (
            f"fix: cd {REPO_DIR} && git stash && git pull --ff-only origin {BRANCH}"
            "  (server-side edits are blocking the pull; they live in git stash"
            " afterwards if you need them)"
        )
    if "divergent" in low or "not possible to fast-forward" in low:
        return (
            f"fix: cd {REPO_DIR} && git reset --hard origin/{BRANCH}"
            "  (server checkout has drifted from GitHub)"
        )
    if "could not resolve host" in low or "unable to access" in low:
        return "fix: network/auth problem reaching github.com from this account"
    return f"fix: cd {REPO_DIR} && git pull --ff-only origin {BRANCH}  (see output above)"


# --------------------------------------------------------------------------
# Status page (GET) -- open https://<you>.pythonanywhere.com/deploy
# --------------------------------------------------------------------------
def _status_text():
    lines = ["HDC deploy hook", ""]

    git_state = "not a git checkout"
    if _is_git_checkout():
        try:
            head = _run_git("rev-parse", "--short", "HEAD").strip()
            branch = _run_git("rev-parse", "--abbrev-ref", "HEAD").strip()
            dirty = _run_git("status", "--porcelain").strip()
            git_state = (
                f"ok, branch {branch}, HEAD {head}, "
                + ("working tree DIRTY (a pull will fail)" if dirty else "clean")
            )
        except subprocess.CalledProcessError as e:
            detail = (e.output.decode(errors="replace") if e.output else "").strip()
            first = detail.splitlines()[0] if detail else "git exited non-zero"
            git_state = f"present but unusable: {first}"
        except Exception as ex:
            git_state = f"present but git failed: {ex}"

    secret = _secret()
    lines += [
        f"repo      : {REPO_DIR}",
        f"git       : {git_state}",
        f"wsgi file : {WSGI_FILE} "
        + ("(found)" if os.path.exists(WSGI_FILE) else "(MISSING - hook is not installed)"),
        f"secret    : "
        + (
            f"{os.path.basename(SECRET_FILE)} present ({len(secret)} chars)"
            if secret
            else f"{SECRET_FILE} MISSING - run ops/pythonanywhere/install_deploy_hook.py"
        ),
        f"branch    : {BRANCH}",
        f"log file  : {LOG_FILE}",
        "",
        "--- last deploys ---",
    ]

    try:
        with open(LOG_FILE) as f:
            tail = f.readlines()[-LOG_TAIL_LINES:]
    except OSError:
        tail = ["no deploys yet\n"]
    lines += [line.rstrip("\n") for line in tail] or ["no deploys yet"]
    return "\n".join(lines) + "\n"


def _handle_get(start_response):
    return _respond(start_response, "200 OK", _status_text())


# --------------------------------------------------------------------------
# Deploy (POST from GitHub)
# --------------------------------------------------------------------------
def _deploy():
    """Run the pull + reload. Returns (http_status, body_text)."""
    if not os.path.isdir(REPO_DIR):
        return "500 Internal Server Error", f"repo dir missing: {REPO_DIR}"
    if not _is_git_checkout():
        return "500 Internal Server Error", f"{REPO_DIR} is not a git checkout"

    try:
        _run_git("fetch", "--quiet", "origin", BRANCH)
    except Exception:
        pass  # fetch is best-effort; the pull below reports the real problem

    try:
        pull_out = _run_git("pull", "--ff-only", "origin", BRANCH)
    except subprocess.CalledProcessError as e:
        output = e.output.decode(errors="replace") if e.output else str(e)
        detail = output.strip() or "git pull failed"
        hint = _pull_hint(output)
        _log(f"FAILED: {detail} | {hint}")
        return "500 Internal Server Error", f"deploy failed: {detail}\n{hint}\n"
    except OSError as e:
        _log(f"FAILED: cannot run git: {e}")
        return "500 Internal Server Error", f"cannot run git in {REPO_DIR}: {e}"

    try:
        commit = _run_git("rev-parse", "--short", "HEAD").strip()
    except Exception:
        commit = "unknown"

    summary = pull_out.strip().splitlines()[-1] if pull_out.strip() else "up to date"

    # Touching the WSGI file is what makes PythonAnywhere reload the app.
    reload_note = ""
    if os.path.exists(WSGI_FILE):
        try:
            os.utime(WSGI_FILE, None)
        except OSError as ex:
            reload_note = f" (reload FAILED: {ex})"
            _log(f"WARN: could not touch {WSGI_FILE}: {ex}")
    else:
        reload_note = (
            " (WSGI file not found - pulled but NOT reloaded; run"
            " ops/pythonanywhere/install_deploy_hook.py)"
        )
        _log(f"WARN: {WSGI_FILE} missing, code pulled but app not reloaded")

    _log(f"OK deployed {commit}: {summary}")
    return "200 OK", f"deployed {commit}{reload_note}"


def application(environ, start_response):
    """WSGI app. Mounted at /deploy by wsgi_dispatch_snippet.py."""
    if environ.get("REQUEST_METHOD", "GET").upper() != "POST":
        # Status / "did it work" page -- open this URL in a browser.
        return _handle_get(start_response)

    if not _secret():
        _log("REJECTED: deploy_secret.txt missing or empty")
        return _respond(
            start_response,
            "503 Service Unavailable",
            "deploy_secret.txt is missing or empty on the server.\n"
            f"expected at: {SECRET_FILE}\n"
            "fix: cd " + REPO_DIR + " && python3 ops/pythonanywhere/install_deploy_hook.py\n",
        )

    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except (TypeError, ValueError):
        length = 0
    body = environ["wsgi.input"].read(length) if length else b""
    signature = environ.get("HTTP_X_HUB_SIGNATURE_256")

    if not _verify(body, signature):
        _log("REJECTED bad signature")
        return _respond(
            start_response,
            "401 Unauthorized",
            "bad signature\n"
            "(the GitHub webhook Secret must be the exact line in "
            f"{os.path.basename(SECRET_FILE)})\n",
        )

    try:
        payload = json.loads(body)
        if payload.get("ref") != f"refs/heads/{BRANCH}":
            return _respond(
                start_response, "200 OK", f"ignored (not {BRANCH}): {payload.get('ref')}"
            )
    except Exception:
        pass  # undecodable payload: still deploy, the pull is idempotent

    status, text = _deploy()
    return _respond(start_response, status, text)
