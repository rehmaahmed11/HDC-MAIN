"""Exact money helpers for the Accounts / Cash Flow layer.

SQLite has no native fixed-point decimal type, and HDC stores amounts in
``FLOAT`` columns (herited from the original monolith).  Binary floats cannot
represent paisa exactly, so a long ledger accumulates rounding error and two
reports that sum the same rows can disagree by a rupee.

The fix used here (ported from the AMS accounts model) is a **minor-unit
mirror**: every money column gets an integer ``*_minor`` twin holding paisa,
and every write goes through these helpers so both representations are always
in step.  The float column stays the legacy/UI surface; the integer column is
authoritative for arithmetic, reconciliation and balancing.

Ordering rule: round half-up to two decimals *once*, at the boundary, then
multiply by 100.  Never round a float sum that has already been rounded.

    >>> to_minor('1234.565')
    123457
    >>> from_minor(123457)
    Decimal('1234.57')

All money is PKR (rupees), two minor digits (paisa).
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

MONEY_QUANTUM = Decimal("0.01")
MINOR_FACTOR = Decimal("100")

__all__ = [
    "MONEY_QUANTUM",
    "MINOR_FACTOR",
    "MoneyValueError",
    "decimal_money",
    "to_minor",
    "from_minor",
    "money_float",
    "fmt_money",
    "sync_money_fields",
    "sum_minor",
]


class MoneyValueError(ValueError):
    """Raised when a submitted money value is missing, invalid or non-finite."""


def _clean_money_text(value):
    """Normalise human money input into something ``Decimal`` can parse.

    Operators in this app type amounts straight from paper slips, so thousands
    separators and a currency prefix are normal rather than exceptional::

        'Rs 12,34,567.89'  ->  '1234567.89'
        '4,500'            ->  '4500'
        '(250)'            ->  '-250'      (accounting negative)

    Only separators are touched.  The decimal point is never guessed, so a
    value with both separators and a point ('1,234.56') stays correct.
    """
    txt = str(value).strip()
    if not txt:
        return "0"
    upper = txt.upper().replace("\u00a0", " ")
    for prefix in ("PKR", "RS.", "RS", "₨"):
        if upper.startswith(prefix):
            txt = txt[len(prefix):].strip()
            break
    negative = txt.startswith("(") and txt.endswith(")")
    if negative:
        txt = txt[1:-1].strip()
    txt = txt.replace(",", "").replace(" ", "")
    if not txt:
        return "0"
    return ("-" + txt) if negative else txt


def decimal_money(value, *, field: str = "Amount") -> Decimal:
    """Return a finite two-decimal ``Decimal`` using commercial half-up rounding."""
    if value is None or (isinstance(value, str) and not value.strip()):
        value = "0"
    try:
        result = Decimal(_clean_money_text(value)).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError, AttributeError) as exc:
        raise MoneyValueError(f"{field} must be a valid number.") from exc
    if not result.is_finite():
        raise MoneyValueError(f"{field} must be a finite number.")
    if result == Decimal("-0.00"):
        return Decimal("0.00")
    return result


def to_minor(value, *, field: str = "Amount") -> int:
    """Convert a currency value to exact integer minor units (paisa)."""
    return int(decimal_money(value, field=field) * MINOR_FACTOR)


def from_minor(value) -> Decimal:
    """Convert integer minor units to a two-decimal ``Decimal``."""
    try:
        minor = int(value or 0)
    except (TypeError, ValueError) as exc:
        raise MoneyValueError("Stored minor-unit amount is invalid.") from exc
    return (Decimal(minor) / MINOR_FACTOR).quantize(MONEY_QUANTUM)


def money_float(value) -> float:
    """Legacy/UI representation after exact decimal normalisation."""
    return float(decimal_money(value))


def fmt_money(value, *, comma: bool = True) -> str:
    """Render any money value (float, minor int, Decimal, str) as ``1,234.57``."""
    if isinstance(value, int) and not isinstance(value, bool):
        dec = from_minor(value)
    else:
        dec = decimal_money(value)
    return f"{dec:,.2f}" if comma else f"{dec:.2f}"


def sync_money_fields(obj, value_attr: str, minor_attr: str) -> None:
    """Keep a model's legacy float column and its integer minor twin in step.

    Normal writes set the float attribute; the integer mirror is then derived
    from it.  If only the minor value is present (imported or backfilled rows)
    the float is derived from the minor value instead, so a row can never end
    up with one side missing.
    """
    if not hasattr(obj, value_attr) or not hasattr(obj, minor_attr):
        return
    value = getattr(obj, value_attr, None)
    minor = getattr(obj, minor_attr, None)
    if value is None and minor is not None:
        exact = from_minor(minor)
        setattr(obj, value_attr, float(exact))
        setattr(obj, minor_attr, int(minor))
        return
    exact_minor = to_minor(value or 0, field=value_attr.replace("_", " ").title())
    setattr(obj, minor_attr, exact_minor)
    setattr(obj, value_attr, float(from_minor(exact_minor)))


def sum_minor(values) -> int:
    """Sum an iterable of minor-unit ints safely (NULL/None treated as zero)."""
    total = 0
    for v in values or ():
        try:
            total += int(v or 0)
        except (TypeError, ValueError):
            continue
    return total
