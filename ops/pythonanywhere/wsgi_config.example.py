"""PythonAnywhere WSGI editor example.

Copy the logic into PythonAnywhere's WSGI configuration file and replace
``yourname``. Do not commit the real production.env file.
"""

import os
import sys

ENV_FILE = "/home/yourname/.config/hdc/production.env"


def load_env_file(path):
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key.strip(), value)


load_env_file(ENV_FILE)
project_dir = os.environ["HDC_APP_DIR"]
if project_dir not in sys.path:
    sys.path.insert(0, project_dir)

from wsgi import app as application  # noqa: E402,F401
