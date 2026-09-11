import os

# Use the validated integrated DB by default unless explicitly overridden.
os.environ.setdefault(
    "HDC_DB_PATH",
    r"e:\WORKINGS\current working\rep hdc\hdc\hdc_instance\hdc_erp_integrated.db"
)

from hdc_erp import app
