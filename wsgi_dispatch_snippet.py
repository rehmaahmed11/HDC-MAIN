"""Paste this into your PythonAnywhere WSGI file -- or let the installer do it.

    cd ~/HDC-MAIN
    python3 ops/pythonanywhere/install_deploy_hook.py

The installer copies this file verbatim to
/var/www/<you>_pythonanywhere_com_wsgi.py (backing up whatever was there)
and adds one generated line that pins the repo path.

It routes /deploy (and /deploy/health, /deploy/status) to deploy_hook.py and
everything else to the real HDC app.  NOTHING HERE NEEDS EDITING BY HAND:
the repo folder is detected from HDC_REPO_DIR, from the username in this
file's own name, or by looking for hdc/app.py under your home directory.

The real app is imported lazily and defensively, so a broken deploy still
leaves /deploy answering with the log -- which is the page you need most
exactly when the site is down.
"""

# HDC deploy hook dispatch -- do not edit by hand; re-run
# ops/pythonanywhere/install_deploy_hook.py to rewrite this file.

import os
import sys
import traceback

# --- repo detection -------------------------------------------------------
def _username():
    """Username from this WSGI file's own name, else from the environment."""
    name = os.path.basename(globals().get("__file__") or "")
    for suffix in ("_pythonanywhere_com_wsgi.py", "_wsgi.py"):
        if name.endswith(suffix):
            candidate = name[: -len(suffix)]
            if candidate:
                return candidate
    for var in ("HDC_PA_USERNAME", "USER", "LOGNAME"):
        value = (os.environ.get(var) or "").strip()
        if value:
            return value
    return ""


def _looks_like_repo(folder):
    return os.path.isfile(os.path.join(folder, "hdc", "app.py"))


def _detect_repo_dir():
    """Find the HDC-MAIN checkout without being told where it is.

    Priority: the HDC_REPO_DIR environment variable, then the path the
    installer pinned into the header above (HDC_REPO_DIR_DEFAULT), then a
    search of the home directory belonging to this WSGI file's username.
    """
    explicit = (os.environ.get("HDC_REPO_DIR") or "").strip()
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))

    pinned = str(globals().get("HDC_REPO_DIR_DEFAULT") or "").strip()
    if pinned:
        pinned = os.path.abspath(os.path.expanduser(pinned))
        if _looks_like_repo(pinned):
            return pinned

    user = _username()
    home = os.path.expanduser(f"~{user}") if user else os.path.expanduser("~")

    preferred = os.path.join(home, "HDC-MAIN")
    if _looks_like_repo(preferred):
        return preferred

    # Fall back to the first folder under home that holds hdc/app.py.
    try:
        names = sorted(os.listdir(home))
    except OSError:
        names = []
    for name in names:
        candidate = os.path.join(home, name)
        if os.path.isdir(candidate) and _looks_like_repo(candidate):
            return candidate

    # Best guess; the import below reports clearly if it is wrong.
    return pinned or preferred


path = _detect_repo_dir()
if path not in sys.path:
    sys.path.insert(0, path)

# --- deploy hook (mounted at /deploy) -------------------------------------
try:
    import deploy_hook
    _HOOK_ERROR = None
except Exception:  # pragma: no cover - reported through /deploy below
    deploy_hook = None
    _HOOK_ERROR = traceback.format_exc()

if deploy_hook is not None:
    # This file knows its own path, which beats any guess: touching THIS file
    # is what makes PythonAnywhere reload the site after a deploy.
    _self_path = globals().get("__file__")
    if _self_path and not (os.environ.get("HDC_WSGI_FILE") or "").strip():
        deploy_hook.WSGI_FILE = os.path.abspath(_self_path)

# --- real app (imported on first request, never at WSGI load time) --------
_real_app = None
_real_app_error = None


def _get_real_app():
    global _real_app, _real_app_error
    if _real_app is None and _real_app_error is None:
        try:
            from hdc.app import create_app
            _real_app = create_app()
        except Exception:
            _real_app_error = traceback.format_exc()
    return _real_app


def _plain(start_response, status, text):
    start_response(status, [("Content-Type", "text/plain; charset=utf-8")])
    return [text.encode("utf-8")]


def application(environ, start_response):
    """WSGI entrypoint: /deploy -> hook, everything else -> HDC app."""
    if environ.get("PATH_INFO", "").rstrip("/") in ("/deploy", "/deploy/health",
                                                     "/deploy/status"):
        if deploy_hook is not None:
            return deploy_hook.application(environ, start_response)
        return _plain(
            start_response,
            "500 Internal Server Error",
            "deploy_hook.py could not be imported.\n"
            f"repo dir used: {path}\n\n{_HOOK_ERROR}\n",
        )

    app = _get_real_app()
    if app is None:
        return _plain(
            start_response,
            "500 Internal Server Error",
            "The HDC app failed to start.\n"
            f"repo dir used: {path}\n"
            "(open /deploy for the deploy log)\n\n"
            f"{_real_app_error}\n",
        )
    return app(environ, start_response)
