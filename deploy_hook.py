"""
Minimal auto-deploy webhook for PythonAnywhere free accounts.

What this does, in plain terms:
  You push to GitHub -> GitHub calls this file -> this file runs
  `git pull` in your repo -> touches your WSGI file so PythonAnywhere
  reloads the app. That's the whole system. No PythonAnywhere API,
  no account token, nothing else running.

ONE-TIME SETUP on PythonAnywhere (after you `git clone` this repo there):

  1. Create a file called deploy_secret.txt in the SAME folder as this
     file. Put ONE line in it: any random string you make up yourself
     (e.g. mash the keyboard for 20 characters). This is your "key" --
     it is just a shared password between GitHub and this file, not a
     PythonAnywhere account token. Do NOT commit this file to git --
     it's already in .gitignore.

  2. Edit the two paths below (REPO_DIR, WSGI_FILE) to match your
     PythonAnywhere username.

  3. Open your PythonAnywhere WSGI file (Web tab -> the
     ...wsgi.py link) and replace its content with what's in
     wsgi_dispatch_snippet.py (also in this repo) -- it routes
     /deploy to this file and everything else to your real app.

  4. Click Reload once by hand in the Web tab.

  5. On GitHub: repo -> Settings -> Webhooks -> Add webhook:
       Payload URL : https://<you>.pythonanywhere.com/deploy
       Content type: application/json
       Secret      : the exact same string you put in deploy_secret.txt
       Events      : "Just the push event"

  Done. Every future push to main now pulls + reloads automatically.

HOW YOU KNOW IT WORKED:
  Open https://<you>.pythonanywhere.com/deploy in a browser (GET
  request, no secret needed to just read the log) -- it shows the
  last few deploy attempts with timestamps and commit hashes, e.g.:
      2026-09-13 14:02:11  OK deployed a1b2c3d: Fast-forward
  If a push doesn't show up there within a few seconds, check
  GitHub -> Settings -> Webhooks -> your webhook -> "Recent Deliveries"
  to see the exact error GitHub got back.
"""

import hashlib
import hmac
import json
import os
import subprocess
import time

# ---- EDIT THESE TWO LINES FOR YOUR ACCOUNT ----
REPO_DIR = "/home/YOURUSERNAME/HDC-MAIN"
WSGI_FILE = "/var/www/yourusername_pythonanywhere_com_wsgi.py"
# ------------------------------------------------

BRANCH = "main"
LOG_FILE = os.path.join(REPO_DIR, "deploy.log")
SECRET_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "deploy_secret.txt")


def _secret():
    with open(SECRET_FILE) as f:
        return f.read().strip()


def _log(line):
    with open(LOG_FILE, "a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {line}\n")


def _verify(body, signature_header):
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    mac = hmac.new(_secret().encode(), msg=body, digestmod=hashlib.sha256)
    expected = "sha256=" + mac.hexdigest()
    return hmac.compare_digest(expected, signature_header)


def application(environ, start_response):
    """WSGI app. Mount at /deploy -- see wsgi_dispatch_snippet.py."""

    if environ["REQUEST_METHOD"] == "GET":
        # Health check / "did it work" check -- open this URL in a browser.
        try:
            with open(LOG_FILE) as f:
                last_lines = f.readlines()[-8:]
        except FileNotFoundError:
            last_lines = ["no deploys yet\n"]
        start_response("200 OK", [("Content-Type", "text/plain")])
        return ["".join(last_lines).encode()]

    length = int(environ.get("CONTENT_LENGTH", 0) or 0)
    body = environ["wsgi.input"].read(length)
    signature = environ.get("HTTP_X_HUB_SIGNATURE_256")

    if not _verify(body, signature):
        _log("REJECTED bad signature")
        start_response("401 Unauthorized", [("Content-Type", "text/plain")])
        return [b"bad signature"]

    try:
        payload = json.loads(body)
        if payload.get("ref") != f"refs/heads/{BRANCH}":
            start_response("200 OK", [("Content-Type", "text/plain")])
            return [b"ignored (not target branch)"]
    except Exception:
        pass

    try:
        pull_out = subprocess.check_output(
            ["git", "pull", "origin", BRANCH], cwd=REPO_DIR, stderr=subprocess.STDOUT
        ).decode()
        commit = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=REPO_DIR
        ).decode().strip()
        os.utime(WSGI_FILE, None)  # "touching" this file is what makes PythonAnywhere reload
        summary = pull_out.strip().splitlines()[-1] if pull_out.strip() else "up to date"
        _log(f"OK deployed {commit}: {summary}")
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [f"deployed {commit}".encode()]
    except subprocess.CalledProcessError as e:
        _log(f"FAILED: {e.output.decode()}")
        start_response("500 Internal Server Error", [("Content-Type", "text/plain")])
        return [b"deploy failed, check deploy.log"]
