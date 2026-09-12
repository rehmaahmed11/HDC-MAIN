"""WSGI entrypoint (gunicorn / PythonAnywhere).

Database and instance paths come from the environment (HDC_DB_PATH,
HDC_INSTANCE_DIR) with safe defaults — no machine-specific paths here.
"""
from hdc.app import create_app

app = create_app()
