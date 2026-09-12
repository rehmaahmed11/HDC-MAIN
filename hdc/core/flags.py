"""HDC core.flags — moved verbatim from hdc_erp.py.

See MODULARIZATION_PLAN.md for the module map.
"""

from sqlalchemy import text

from hdc.extensions import db
from hdc.utils.dates import _pkt_now_naive

def _runtime_flag_get(flag_key):
    row = db.session.execute(
        text("SELECT value FROM hdc_runtime_flag WHERE key = :k"),
        {"k": flag_key}
    ).fetchone()
    return (row[0] if row else None)


def _runtime_flag_set(flag_key, value='1'):
    db.session.execute(text("""
        INSERT INTO hdc_runtime_flag(key, value, updated_at)
        VALUES(:k, :v, :ts)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at
    """), {"k": flag_key, "v": value, "ts": _pkt_now_naive()})
    db.session.commit()
