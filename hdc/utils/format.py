"""HDC utils.format — moved verbatim from hdc_erp.py.

See MODULARIZATION_PLAN.md for the module map.
"""

import ast
import operator as op_module
from datetime import date, datetime

from hdc.utils.dates import _pkt_now, _pkt_today

def _num_to_words_en(n):
    n = int(max(0, n or 0))
    under_20 = [
        'zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine',
        'ten', 'eleven', 'twelve', 'thirteen', 'fourteen', 'fifteen', 'sixteen',
        'seventeen', 'eighteen', 'nineteen'
    ]
    tens = ['', '', 'twenty', 'thirty', 'forty', 'fifty', 'sixty', 'seventy', 'eighty', 'ninety']
    scales = [(10**9, 'billion'), (10**6, 'million'), (10**3, 'thousand'), (100, 'hundred')]

    if n < 20:
        return under_20[n]
    if n < 100:
        return tens[n // 10] + ('' if (n % 10 == 0) else f"-{under_20[n % 10]}")
    for scale_val, scale_name in scales:
        if n >= scale_val:
            left = n // scale_val
            rem = n % scale_val
            return _num_to_words_en(left) + f" {scale_name}" + ('' if rem == 0 else f" {_num_to_words_en(rem)}")
    return str(n)


def _amount_to_words(amount):
    val = float(amount or 0.0)
    whole = int(abs(val))
    frac = int(round((abs(val) - whole) * 100))
    words = _num_to_words_en(whole) + ' rupees'
    if frac > 0:
        words += f' and {_num_to_words_en(frac)} paisa'
    if val < 0:
        words = 'minus ' + words
    return words + ' only'


def _activity_at_for(event_date):
    base_date = event_date if isinstance(event_date, date) else _pkt_today()
    now = _pkt_now()
    return datetime.combine(base_date, now.time().replace(microsecond=0))


def _is_strong_password(pwd):
    pwd = str(pwd or '')
    if len(pwd) < 8:
        return False, 'Password must be at least 8 characters.'
    if not any(ch.islower() for ch in pwd):
        return False, 'Password must include a lowercase letter.'
    if not any(ch.isupper() for ch in pwd):
        return False, 'Password must include an uppercase letter.'
    if not any(ch.isdigit() for ch in pwd):
        return False, 'Password must include a number.'
    if not any((not ch.isalnum()) for ch in pwd):
        return False, 'Password must include a special character.'
    return True, ''


# --- Safe Math Evaluator ---------------------------------------------------
_OPS = {
    ast.Add: op_module.add, ast.Sub: op_module.sub,
    ast.Mult: op_module.mul, ast.Div: op_module.truediv,
    ast.Pow: op_module.pow, ast.USub: op_module.neg
}


def _safe_eval(expr, variables=None):
    variables = variables or {}
    tree = ast.parse(expr, mode='eval')
    def _ev(n):
        if isinstance(n, ast.Constant): return n.value
        if isinstance(n, ast.Name):
            if n.id not in variables: raise ValueError(f"Unknown variable: {n.id}")
            return float(variables[n.id])
        if isinstance(n, ast.BinOp):
            op = _OPS.get(type(n.op))
            if not op: raise ValueError("Unsupported operator")
            return op(_ev(n.left), _ev(n.right))
        if isinstance(n, ast.UnaryOp):
            op = _OPS.get(type(n.op))
            if not op: raise ValueError("Unsupported unary operator")
            return op(_ev(n.operand))
        if isinstance(n, ast.Expression): return _ev(n.body)
        raise ValueError(f"Unsupported: {type(n).__name__}")
    return _ev(tree)


# --- Helpers ---------------------------------------------------------------
_DATE_FALLBACK_DEFAULT = object()


def _parse_date(s, fallback=_DATE_FALLBACK_DEFAULT):
    fallback_val = _pkt_today() if fallback is _DATE_FALLBACK_DEFAULT else fallback
    if not s:
        return fallback_val
    try:
        return datetime.strptime(s.strip(), '%Y-%m-%d').date()
    except Exception:
        return fallback_val


def _flt(v, default=0.0):
    try:
        if v is None:
            return float(default)
        if isinstance(v, (int, float)):
            return float(v)
        raw = str(v).strip()
        if not raw:
            return float(default)
        # Accept common human input like "2,500" for numeric fields.
        raw = raw.replace(',', '')
        return float(raw)
    except Exception:
        return default


def _is_pdf_upload(file_obj):
    if not file_obj:
        return False
    filename = (file_obj.filename or '').strip().lower()
    return filename.endswith('.pdf')


def _quote_ident(name):
    return '"' + str(name or '').replace('"', '""') + '"'


def _safe_sheet_name(name, fallback='Sheet'):
    raw = str(name or '').strip() or fallback
    cleaned = ''.join(ch for ch in raw if ch not in r'[]:*?/\\')
    cleaned = cleaned[:31].strip() or fallback
    return cleaned


_UNIT_TO_M = {
    'mm': 0.001,
    'cm': 0.01,
    'm': 1.0,
    'ft': 0.3048,
    'in': 0.0254,
}


_UNIT_TO_MM = {
    'mm': 1.0,
    'cm': 10.0,
    'm': 1000.0,
    'ft': 304.8,
    'in': 25.4,
}


def _to_meters(value, unit):
    return float(value or 0) * _UNIT_TO_M.get(unit or 'm', 1.0)


def _to_mm(value, unit):
    return float(value or 0) * _UNIT_TO_MM.get(unit or 'mm', 1.0)


def _payload_int(payload, key):
    try:
        return int((payload.get(key) if payload is not None else None) or 0)
    except Exception:
        return 0
