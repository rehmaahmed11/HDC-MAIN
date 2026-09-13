"""
Paste this into your PythonAnywhere WSGI file (Web tab -> the
.../var/www/yourusername_pythonanywhere_com_wsgi.py link).

It sends /deploy requests to deploy_hook.py and everything else to
your real app (hdc.app.create_app()). Edit the sys.path line to match
where you cloned the repo.
"""
import sys

path = "/home/YOURUSERNAME/HDC-MAIN"
if path not in sys.path:
    sys.path.insert(0, path)

from hdc.app import create_app
import deploy_hook

real_app = create_app()


def application(environ, start_response):
    if environ.get("PATH_INFO", "").rstrip("/") == "/deploy":
        return deploy_hook.application(environ, start_response)
    return real_app(environ, start_response)
