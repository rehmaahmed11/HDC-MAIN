"""Pure request-value validation (no Flask or database dependencies)."""
import math
from collections.abc import Mapping


def validate_purchase_payload(payload):
    """Reject malformed values before Purchase V2 handlers perform any writes.

    Optional null/blank values retain the handlers' existing default semantics.
    Comma-separated numeric strings remain supported, like the HTML forms.
    """
    if not isinstance(payload, Mapping):
        raise ValueError('Request body must be a JSON object.')
    text_fields = ('name', 'phone', 'status', 'unit', 'supplier_name',
                   'supplier_phone', 'payment_status', 'note', 'entry_kind',
                   'delivery_person')
    for field in text_fields:
        value = payload.get(field)
        if value is not None and not isinstance(value, str):
            raise ValueError(f'{field} must be text.')
    for field in ('quantity', 'unit_price', 'amount'):
        value = payload.get(field)
        if value is None or value == '':
            continue
        try:
            if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                raise ValueError
            number = float(str(value).replace(',', ''))
            if not math.isfinite(number):
                raise ValueError
        except (ValueError, OverflowError):
            raise ValueError(f'{field} must be a finite number.') from None
    for field in ('supplier_id', 'material_id', 'project_id', 'stage_id', 'purchase_id'):
        value = payload.get(field)
        if value is None or value == '':
            continue
        try:
            if isinstance(value, bool) or not isinstance(value, (str, int)):
                raise ValueError
            number = int(value)
            if not 0 <= number <= 9223372036854775807:
                raise ValueError
        except (ValueError, OverflowError):
            raise ValueError(f'{field} must be a valid integer ID.') from None
    return payload
