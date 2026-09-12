"""HDC utils.dates — moved verbatim from hdc_erp.py.

See MODULARIZATION_PLAN.md for the module map.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

PKT_ZONE = ZoneInfo('Asia/Karachi')


def _pkt_now():
    return datetime.now(PKT_ZONE)


def _pkt_now_naive():
    # Persist PKT wall-clock in DB (SQLite stores naive datetimes).
    return _pkt_now().replace(tzinfo=None)


def _pkt_today():
    return _pkt_now().date()


def _as_pkt(dt_value):
    if not dt_value:
        return None
    if isinstance(dt_value, datetime):
        if dt_value.tzinfo is None:
            return dt_value.replace(tzinfo=PKT_ZONE)
        return dt_value.astimezone(PKT_ZONE)
    return None


def _fmt_pkt(dt_value, fmt='%Y-%m-%d %H:%M:%S PKT'):
    pkt = _as_pkt(dt_value)
    return pkt.strftime(fmt) if pkt else ''
