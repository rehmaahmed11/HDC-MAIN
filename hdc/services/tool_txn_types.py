"""HDC services.tool_txn_types — the one data table behind the Tools transaction form.

The Tools *New Rental* page (``/hdc/tool-rental/new``) is a single form for the
rental life-cycle.  Which transactions it offers, what each one is called, which
endpoint it posts to, which rentals it may pick from and how its submit button
reads are all described here, as plain data.  The template renders the
dropdown and the submit label from this table, and the page script switches
panels by the ``data-txn-modes`` attribute, so adding a transaction means
adding one entry here plus its panel markup — no new page.

``UNIFORM_TXN_TYPES`` below is the same idea, widened: the central **HDC Tool
Transaction** page (``/hdc/tool-rental/transaction``) records *every*
equipment transaction from one form — renting out, issuing to an own site,
transferring between sites or customers, partial/full/store returns, rent
payments, credit adjustments, damage/loss and maintenance — and each entry
points at the endpoint that already implements it.  Nothing in this module
touches the database or Flask.  The route builds the URL of every entry and the
picker options that go with it.

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


# --------------------------------------------------------------------------- #
# The uniform HDC Tool Transaction registry
# --------------------------------------------------------------------------- #
#: What each transaction actually does on the server.  ``kind`` tells the page
#: script which controller section fills the form and which submit path it
#: takes; the endpoints themselves are the existing Tools routes, plus the
#: three thin handlers for the physical movements that had no route before
#: (damage/loss from a holder, send for maintenance, return from maintenance).
KIND_CREATE = 'create'                # opens a new rental (external or internal)
KIND_TRANSFER = 'transfer'            # moves rented tools to a new holder/cycle
KIND_RETURN = 'return'                # brings tools back to the store
KIND_PAYMENT = 'payment'              # money in, no tool movement
KIND_ADJUSTMENT = 'adjustment'        # credit / waive-off, no cash, no movement
KIND_DAMAGE_LOSS = 'damage_loss'      # write-off, from store or from a holder
KIND_MAINTENANCE_OUT = 'maintenance_out'
KIND_MAINTENANCE_IN = 'maintenance_in'

#: Which panel groups the page shows for each transaction, so the operator only
#: ever sees the fields that apply.  ``common`` (date/reference/tool/notes) and
#: the transaction-type selector are always on screen.
PANEL_DESTINATION = 'destination'          # who receives (customer / site)
PANEL_LINES = 'lines'                      # tool lines leaving the store
PANEL_TERMS = 'terms'                      # billing unit, dates, agreement
PANEL_TRANSFER_SOURCE = 'transfer_source'  # where the tools are now
PANEL_TRANSFER_DESTINATION = 'transfer_destination'
PANEL_RETURN = 'return'                    # outstanding lines coming back
PANEL_SETTLEMENT = 'settlement'            # optional collection on a return
PANEL_PAYMENT = 'payment'
PANEL_ADJUSTMENT = 'adjustment'
PANEL_DAMAGE = 'damage'
PANEL_MAINTENANCE_SEND = 'maintenance_send'
PANEL_MAINTENANCE_RETURN = 'maintenance_return'

UNIFORM_TXN_TYPES = (
    {
        'key': 'rent_out_customer',
        'label': 'Rent Out to Customer',
        'icon': 'fa-truck-fast',
        'kind': KIND_CREATE,
        'endpoint': 'hdc_tool_rental_create',
        'needs_rental': False,
        'eligible_for': None,
        'renter_type': 'external',
        'return_type': None,
        'settlement': False,
        'panels': (PANEL_DESTINATION, PANEL_LINES, PANEL_TERMS),
        'submit_label': 'Create Rental',
        'submit_class': 'btn-success',
        'help': 'Send tools from the store to an outside customer and raise the rent.',
        'confirm': 'Create this rental and send the tools to the customer?',
    },
    {
        'key': 'issue_own_site',
        'label': 'Issue to Own Company Site',
        'icon': 'fa-diagram-project',
        'kind': KIND_CREATE,
        'endpoint': 'hdc_tool_rental_create',
        'needs_rental': False,
        'eligible_for': None,
        'renter_type': 'internal',
        'return_type': None,
        'settlement': False,
        'panels': (PANEL_DESTINATION, PANEL_LINES, PANEL_TERMS),
        'submit_label': 'Issue to Site',
        'submit_class': 'btn-primary',
        'help': 'Move tools to an HDC site or project. It is not an outside sale.',
        'confirm': 'Issue these tools to the selected HDC site?',
    },
    {
        'key': 'transfer_site',
        'label': 'Transfer Between Sites',
        'icon': 'fa-right-left',
        'kind': KIND_TRANSFER,
        'endpoint': 'hdc_tool_rental_create',
        'needs_rental': False,
        'eligible_for': None,
        'renter_type': 'internal',
        'return_type': None,
        'settlement': True,
        'panels': (PANEL_TRANSFER_SOURCE, PANEL_TRANSFER_DESTINATION, PANEL_TERMS),
        'submit_label': 'Complete Transfer',
        'submit_class': 'btn-warning',
        'help': 'Move tools that are out back to another HDC site, with a fresh rate.',
        'confirm': 'Transfer the selected tools and close their current cycle?',
    },
    {
        'key': 'transfer_customer',
        'label': 'Transfer Between Customers',
        'icon': 'fa-people-arrows',
        'kind': KIND_TRANSFER,
        'endpoint': 'hdc_tool_rental_create',
        'needs_rental': False,
        'eligible_for': None,
        'renter_type': 'external',
        'return_type': None,
        'settlement': True,
        'panels': (PANEL_TRANSFER_SOURCE, PANEL_TRANSFER_DESTINATION, PANEL_TERMS),
        'submit_label': 'Transfer to Customer',
        'submit_class': 'btn-warning',
        'help': 'Hand rented tools to another customer where the current holder allows it.',
        'confirm': 'Transfer the selected tools to this customer?',
    },
    {
        'key': 'partial_return',
        'label': 'Partial Return',
        'icon': 'fa-rotate-left',
        'kind': KIND_RETURN,
        'endpoint': 'hdc_tool_rental_return',
        'needs_rental': True,
        'eligible_for': ELIGIBLE_PENDING_TOOLS,
        'renter_type': None,
        'return_type': 'partial',
        'settlement': True,
        'panels': (PANEL_RETURN, PANEL_SETTLEMENT),
        'submit_label': 'Record Partial Return',
        'submit_class': 'btn-primary',
        'help': 'Bring back only the quantities that actually came back.',
        'confirm': 'Record this partial return and settle what is being paid?',
    },
    {
        'key': 'full_return',
        'label': 'Full Return',
        'icon': 'fa-circle-check',
        'kind': KIND_RETURN,
        'endpoint': 'hdc_tool_rental_return',
        'needs_rental': True,
        'eligible_for': ELIGIBLE_PENDING_TOOLS,
        'renter_type': None,
        'return_type': 'full',
        'settlement': True,
        'panels': (PANEL_RETURN, PANEL_SETTLEMENT),
        'submit_label': 'Record Full Return',
        'submit_class': 'btn-success',
        'help': 'Close the physical issue: every piece still out comes back.',
        'confirm': 'Return everything still outstanding on this rental?',
    },
    {
        'key': 'return_to_store',
        'label': 'Return to Store',
        'icon': 'fa-warehouse',
        'kind': KIND_RETURN,
        'endpoint': 'hdc_tool_rental_return',
        'needs_rental': True,
        'eligible_for': ELIGIBLE_PENDING_TOOLS,
        'renter_type': None,
        'return_type': 'partial',
        'settlement': False,
        'panels': (PANEL_RETURN,),
        'submit_label': 'Return to Store',
        'submit_class': 'btn-secondary',
        'help': 'Put chosen pieces back in the warehouse without collecting rent now.',
        'confirm': 'Put the selected pieces back into the store?',
    },
    {
        'key': 'rental_payment',
        'label': 'Record Rental Payment',
        'icon': 'fa-money-bill-wave',
        'kind': KIND_PAYMENT,
        'endpoint': 'hdc_tool_rental_payment',
        'needs_rental': True,
        'eligible_for': ELIGIBLE_PENDING_AMOUNT,
        'renter_type': None,
        'return_type': None,
        'settlement': False,
        'panels': (PANEL_PAYMENT,),
        'submit_label': 'Receive Payment',
        'submit_class': 'btn-success',
        'help': 'Money in. The tools stay exactly where they are.',
        'confirm': 'Post this rent receipt and update the outstanding balance?',
    },
    {
        'key': 'credit_adjustment',
        'label': 'Record Credit / Outstanding Adjustment',
        'icon': 'fa-percent',
        'kind': KIND_ADJUSTMENT,
        'endpoint': 'hdc_tool_rental_add_discount',
        'needs_rental': True,
        'eligible_for': ELIGIBLE_PENDING_AMOUNT,
        'renter_type': None,
        'return_type': None,
        'settlement': False,
        'panels': (PANEL_ADJUSTMENT,),
        'submit_label': 'Post Adjustment',
        'submit_class': 'btn-warning',
        'help': 'Reduce what is owed without taking cash. Finance roles only.',
        'confirm': 'Post this credit against the rental balance?',
    },
    {
        'key': 'damage_loss',
        'label': 'Record Damage or Loss',
        'icon': 'fa-triangle-exclamation',
        'kind': KIND_DAMAGE_LOSS,
        'endpoint': 'hdc_tool_txn_damage_loss',
        'needs_rental': False,
        'eligible_for': None,
        'renter_type': None,
        'return_type': None,
        'settlement': False,
        'panels': (PANEL_DAMAGE,),
        'submit_label': 'Record Damage / Loss',
        'submit_class': 'btn-danger',
        'help': 'Write off broken or missing pieces, from the store or from a holder.',
        'confirm': 'Write off these pieces? Stock ownership drops and it cannot be undone from here.',
    },
    {
        'key': 'maintenance_out',
        'label': 'Send for Maintenance',
        'icon': 'fa-screwdriver-wrench',
        'kind': KIND_MAINTENANCE_OUT,
        'endpoint': 'hdc_tool_txn_maintenance',
        'needs_rental': False,
        'eligible_for': None,
        'renter_type': None,
        'return_type': None,
        'settlement': False,
        'panels': (PANEL_MAINTENANCE_SEND,),
        'submit_label': 'Send for Maintenance',
        'submit_class': 'btn-primary',
        'help': 'Pieces leave the available pool but stay owned and stay traceable.',
        'confirm': 'Send the selected pieces for maintenance?',
    },
    {
        'key': 'maintenance_in',
        'label': 'Return from Maintenance',
        'icon': 'fa-clipboard-check',
        'kind': KIND_MAINTENANCE_IN,
        'endpoint': 'hdc_tool_txn_maintenance_return',
        'needs_rental': False,
        'eligible_for': None,
        'renter_type': None,
        'return_type': None,
        'settlement': False,
        'panels': (PANEL_MAINTENANCE_RETURN,),
        'submit_label': 'Return from Maintenance',
        'submit_class': 'btn-success',
        'help': 'Pieces come back to the store — repaired, or written off if they are not.',
        'confirm': 'Record these pieces as back from maintenance?',
    },
)

UNIFORM_TXN_TYPE_KEYS = tuple(t['key'] for t in UNIFORM_TXN_TYPES)
DEFAULT_UNIFORM_TXN_TYPE = 'rent_out_customer'


def uniform_txn_type(key: str) -> Optional[dict]:
    """Return the uniform-registry entry for ``key`` (``None`` when unknown)."""
    for entry in UNIFORM_TXN_TYPES:
        if entry['key'] == key:
            return entry
    return None


#: Which of the 12 transactions leaves the store physically, touches money, or
#: only changes a recorded state — used by the page's help text and by tests
#: that must prove e.g. "a payment never moves a tool".
PHYSICAL_KINDS = (KIND_CREATE, KIND_TRANSFER, KIND_RETURN,
                  KIND_DAMAGE_LOSS, KIND_MAINTENANCE_OUT, KIND_MAINTENANCE_IN)
MONEY_KINDS = (KIND_PAYMENT, KIND_ADJUSTMENT)

#: Default reason choices for the damage/loss transaction (same vocabulary the
#: scrap register already records, so reports keep grouping correctly).
DAMAGE_REASON_KEYS = ('damaged', 'lost', 'worn_out', 'obsolete', 'sold_as_scrap', 'other')
