"""PythonAnywhere WSGI file: HDC ERP + the webhook deploy receiver.

A free PythonAnywhere account gets exactly **one** web app, so the deploy
receiver cannot have its own domain.  Paste this into the single WSGI
configuration file (Web tab -> WSGI configuration file) and it keeps the two
applications separate where it matters:

    /deploy/*        -> deploy_receiver   (stdlib only; no DB, no login, no CSRF)
    everything else -> the HDC ERP application

Both are imported lazily and independently:

* if a pushed commit breaks the ERP, /deploy stays reachable so you can push a
  fix and self-heal instead of phoning the problem in;
* if the receiver module itself somehow breaks, the WSGI file falls back to the
  copy that deploy.sh keeps outside the checkout
  (``/home/yourname/hdc_deploy_receiver.py``).

On a paid plan with a second web app, skip this dispatcher and give the receiver
its own WSGI file containing just ``from deploy_receiver import application``.

Replace ``yourname`` in ENV_FILE, PROJECT_DIR_DEFAULT and FALLBACK_RECEIVER.
"""

import os
import sys

ENV_FILE = "/home/yourname/.config/hdc/production.env"
PROJECT_DIR_DEFAULT = "/home/yourname/HDC-MAIN"
FALLBACK_RECEIVER = "/home/yourname/hdc_deploy_receiver.py"
DEPLOY_PREFIX = "/deploy"

_cache = {"erp_loaded": False, "erp": None, "erp_error": "",
          "receiver_loaded": False, "receiver": None, "receiver_error": ""}


def load_env_file(path):
    """Populate os.environ from KEY=VALUE lines without overriding real env."""
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        if key.strip():
            os.environ.setdefault(key.strip(), value)


load_env_file(ENV_FILE)
project_dir = os.environ.get("HDC_APP_DIR") or PROJECT_DIR_DEFAULT
if project_dir not in sys.path:
    sys.path.insert(0, project_dir)


def _plain(start_response, status, text, retry_after=None):
    body = text.encode("utf-8")
    headers = [
        ("Content-Type", "text/plain; charset=utf-8"),
        ("Content-Length", str(len(body))),
        ("Cache-Control", "no-store"),
        ("X-Content-Type-Options", "nosniff"),
    ]
    if retry_after:
        headers.append(("Retry-After", retry_after))
    start_response(status, headers)
    return [body]


def erp_application():
    """Return (app, error); the ERP is imported once per worker process."""
    if not _cache["erp_loaded"]:
        _cache["erp_loaded"] = True
        try:
            from wsgi import app as application

            _cache["erp"] = application
        except Exception as exc:  # pragma: no cover - deployment-time guard
            _cache["erp_error"] = f"{exc.__class__.__name__}: {exc}"
    return _cache["erp"], _cache["erp_error"]


def deploy_application():
    """Return (app, error) for the receiver, falling back to the saved copy."""
    if not _cache["receiver_loaded"]:
        _cache["receiver_loaded"] = True
        try:
            from deploy_receiver import application as application_

            _cache["receiver"] = application_
        except Exception as primary_error:
            try:
                import importlib.util

                spec = importlib.util.spec_from_file_location(
                    "hdc_deploy_receiver_fallback", FALLBACK_RECEIVER)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                _cache["receiver"] = module.application
                _cache["receiver_error"] = (
                    f"repo copy unusable ({primary_error}); using {FALLBACK_RECEIVER}")
            except Exception:
                _cache["receiver_error"] = (
                    f"{primary_error.__class__.__name__}: {primary_error}")
    return _cache["receiver"], _cache["receiver_error"]


def application(environ, start_response):
    script_name = environ.get("SCRIPT_NAME", "") or ""
    path_info = environ.get("PATH_INFO", "") or ""
    path = path_info if path_info.startswith(DEPLOY_PREFIX) else script_name + path_info

    if path == DEPLOY_PREFIX or path.startswith(DEPLOY_PREFIX + "/"):
        app, _error = deploy_application()
        if app is None:
            return _plain(
                start_response, "503 Service Unavailable",
                "The HDC deploy receiver could not be imported.\n"
                "Open a PythonAnywhere Bash console and run:\n"
                f"  cd {project_dir} && bash ops/pythonanywhere/deploy.sh\n",
            )
        return app(environ, start_response)

    app, error = erp_application()
    if app is None:
        return _plain(
            start_response, "503 Service Unavailable",
            "HDC ERP failed to start, so the site is temporarily unavailable.\n"
            f"Import error: {error or 'unknown failure'}\n\n"
            "The deploy endpoint is still live.  Push a fix to the deployed\n"
            "branch, or roll back from a Bash console with:\n"
            f"  cd {project_dir} && HDC_DEPLOY_REVISION=<previous commit> "
            "bash ops/pythonanywhere/deploy.sh\n",
            retry_after="60",
        )
    return app(environ, start_response)
