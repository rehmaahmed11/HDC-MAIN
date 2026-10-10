"""HDC services.tool_txn_types — the one data table behind the Tools transaction form.

The Tools *New Rental* page (``/hdc/tool-rental/new``) is a single form for the
rental life-cycle.  Which transactions it offers, what each one is called, which
endpoint it posts to, which rentals it may pick from and how its submit button
reads are all described here, as plain data.  The template renders the
dropdown and the submit label from this table, and the page script switches
panels by the ``data-txn-modes`` attribute, so adding a transaction means
adding one entry here plus its panel markup — no new page.

Nothing in this module touches the database or Flask.  The route builds the
URL templates and the rental picker options from it.

Layer: ``services`` — pure data and small pure functions.
"""

from __future__ import annotations

from typing import Optional

# How a rental qualifies for the picker of a transaction that works on an
# existing rental.
ELIGIBLE_PENDING_TOOLS = 'pending_tools'    # still has tools out
ELIGIBLE_PENDING_AMOUNT = 'pending_amount'  # still owes rent

TOOL_TXN_TYPES = (
    {
        'key': 'new_rental',
        'label': 'New Rental',
        'icon': 'fa-plus',
        'endpoint': 'hdc_tool_rental_create',
        'needs_rental': False,
        'eligible_for': None,
        'submit_label': 'Create Rental',
        'submit_class': 'btn-success',
        'help': 'Send tools from store to a site or an outside customer.',
    },
    {
        'key': 'transfer',
        'label': 'Transfer Rental',
        'icon': 'fa-right-left',
        'endpoint': 'hdc_tool_rental_create',
        'needs_rental': False,
        'eligible_for': None,
        'submit_label': 'Complete Transfer',
        'submit_class': 'btn-warning',
        'help': 'Move rented tools from one site or holder to another.',
    },
    {
        'key': 'return',
        'label': 'Return Tools',
        'icon': 'fa-rotate-left',
        'endpoint': 'hdc_tool_rental_return',
        'needs_rental': True,
        'eligible_for': ELIGIBLE_PENDING_TOOLS,
        'submit_label': 'Record Return',
        'submit_class': 'btn-primary',
        'help': 'Take back tools from a rental, with optional rent collection.',
    },
    {
        'key': 'payment',
        'label': 'Rent Payment',
        'icon': 'fa-money-bill',
        'endpoint': 'hdc_tool_rental_payment',
        'needs_rental': True,
        'eligible_for': ELIGIBLE_PENDING_AMOUNT,
        'submit_label': 'Receive Payment',
        'submit_class': 'btn-success',
        'help': 'Collect rent that is still owed on a rental.',
    },
)

TXN_TYPE_KEYS = tuple(t['key'] for t in TOOL_TXN_TYPES)
DEFAULT_TXN_TYPE = 'new_rental'


def txn_type(key: str) -> Optional[dict]:
    """Return the registry entry for ``key`` or ``None`` when unknown."""
    for entry in TOOL_TXN_TYPES:
        if entry['key'] == key:
            return entry
    return None


def rental_eligible(eligible_for: Optional[str], *, pending_tools: float,
                    pending_amount: float, billing_type: str = '') -> bool:
    """Whether a rental belongs in the rental picker of a transaction.

    ``pending_tools`` is the quantity still out; ``pending_amount`` the rent
    still owed.  A no-charge rental never owes rent, so it is not offered for
    a payment, but it can still have tools to take back.
    """
    if eligible_for == ELIGIBLE_PENDING_TOOLS:
        return float(pending_tools or 0) > 0.001
    if eligible_for == ELIGIBLE_PENDING_AMOUNT:
        return (str(billing_type or '').lower() != 'no_charge'
                and float(pending_amount or 0) > 0.009)
    return False
