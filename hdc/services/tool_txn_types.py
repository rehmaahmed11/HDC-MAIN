"""HDC services.tool_txn_types — the data tables behind the Tools forms.

Two forms read from here, and nothing else:

* **The one transaction page** (``/hdc/tool-rental/transaction``) — the front
  door for tools.  It offers exactly three groups, and every transaction lives
  under one of them::

      Rent Out      rent out to a customer · issue to an own site ·
                    transfer to another site / customer ·
                    receive rent payment · credit (waive-off) adjustment
      Return        partial return · full return · return to store ·
                    damage or loss
      Maintenance   send for maintenance · return from maintenance

  ``TXN_GROUPS`` below *is* that tree — the group labels and icons, and for
  every option: the endpoint it posts to, the panels the page shows, the
  checks the page script runs, which rentals it may pick from and how its
  submit button reads.  ``templates/hdc/tool_rental/tool_txn.html`` and
  ``static/hdc/js/pages/tool_txn.js`` render from this table only, so adding a
  transaction means adding one entry here (plus its panel markup) — never a
  new page, a new dropdown or another ``if`` in the page script.

* **The older New Rental page** (``/hdc/tool-rental/new``), which keeps its own
  four modes in ``NEW_RENTAL_TXN_TYPES``; ``TOOL_TXN_*`` names are gone so the
  two tables can no longer be mixed up.

Layer: ``services`` — pure data and small pure functions.
"""

from __future__ import annotations

from typing import Optional

# --------------------------------------------------------------------------- #
# The New Rental page's four modes (legacy ``/hdc/tool-rental/new``)
# --------------------------------------------------------------------------- #
#: How a rental qualifies for the picker of a transaction that works on an
#: existing rental (shared by both tables).
ELIGIBLE_PENDING_TOOLS = 'pending_tools'    # still has tools out
ELIGIBLE_PENDING_AMOUNT = 'pending_amount'  # still owes rent

NEW_RENTAL_TXN_TYPES = (
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

NEW_RENTAL_TXN_TYPE_KEYS = tuple(t['key'] for t in NEW_RENTAL_TXN_TYPES)
NEW_RENTAL_DEFAULT_TXN_TYPE = 'new_rental'


def new_rental_txn_type(key: str) -> Optional[dict]:
    """Return the New Rental page registry entry for ``key`` (``None`` if none)."""
    for entry in NEW_RENTAL_TXN_TYPES:
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


# Billing is a contract, not a display-only dropdown.  ``charges_rent`` drives
# the page's rate/cash controls and the explicit status shown to the operator.
BILLING_MODES = (
    {'key': 'per_day', 'label': 'Full rent — per day', 'charges_rent': True,
     'status': 'Rent charged — payment may be due.'},
    {'key': 'per_hour', 'label': 'Full rent — per hour', 'charges_rent': True,
     'status': 'Rent charged — payment may be due.'},
    {'key': 'fixed_fee', 'label': 'One rental fee', 'charges_rent': True,
     'status': 'Rent charged — payment may be due.'},
    {'key': 'no_charge', 'label': 'No rent (adjusted in contract)', 'charges_rent': False,
     'status': 'No rent — adjusted in contract. No rent due, no cash receipt or payment required.'},
)


# --------------------------------------------------------------------------- #
# The transaction page — what each transaction does on the server
# --------------------------------------------------------------------------- #
KIND_CREATE = 'create'                # opens a new rental (external or internal)
KIND_TRANSFER = 'transfer'            # moves rented tools to a new holder/cycle
KIND_RETURN = 'return'                # brings tools back to the store
KIND_PAYMENT = 'payment'              # money in, no tool movement
KIND_ADJUSTMENT = 'adjustment'        # credit / waive-off, no cash, no movement
KIND_DAMAGE_LOSS = 'damage_loss'      # write-off, from store or from a holder
KIND_MAINTENANCE_OUT = 'maintenance_out'
KIND_MAINTENANCE_IN = 'maintenance_in'

KINDS = (KIND_CREATE, KIND_TRANSFER, KIND_RETURN, KIND_PAYMENT,
         KIND_ADJUSTMENT, KIND_DAMAGE_LOSS, KIND_MAINTENANCE_OUT,
         KIND_MAINTENANCE_IN)

#: Which of the transactions leaves the store physically, touches money, or
#: only changes a recorded state — used by help text and by tests that must
#: prove e.g. "a payment never moves a tool".
PHYSICAL_KINDS = (KIND_CREATE, KIND_TRANSFER, KIND_RETURN,
                  KIND_DAMAGE_LOSS, KIND_MAINTENANCE_OUT, KIND_MAINTENANCE_IN)
MONEY_KINDS = (KIND_PAYMENT, KIND_ADJUSTMENT)

#: Which panel groups the page shows, so the operator only ever sees the
#: fields that apply.  The transaction selector and the date / reference /
#: responsible fields carry ``common`` and are always on screen.
PANEL_DESTINATION = 'destination'          # who receives (customer / site)
PANEL_LINES = 'lines'                      # tool lines leaving the store
PANEL_TERMS = 'terms'                      # billing unit, dates, agreement
PANEL_RENTOUT_SOURCE = 'rentout_source'    # rent-out setup: where the tools
                                           # come from (store / site /
                                           # customer) and who they go to
PANEL_TRANSFER_SOURCE = 'transfer_source'  # where the tools are now, and the
                                           # settlement of the old holder
PANEL_RETURN = 'return'                    # outstanding lines coming back
PANEL_SETTLEMENT = 'settlement'            # optional collection on a return
PANEL_PAYMENT = 'payment'
PANEL_ADJUSTMENT = 'adjustment'
PANEL_DAMAGE = 'damage'
PANEL_MAINTENANCE_SEND = 'maintenance_send'
PANEL_MAINTENANCE_RETURN = 'maintenance_return'

PANELS = (PANEL_DESTINATION, PANEL_LINES, PANEL_TERMS, PANEL_RENTOUT_SOURCE,
          PANEL_TRANSFER_SOURCE, PANEL_RETURN, PANEL_SETTLEMENT, PANEL_PAYMENT,
          PANEL_ADJUSTMENT, PANEL_DAMAGE, PANEL_MAINTENANCE_SEND,
          PANEL_MAINTENANCE_RETURN)

#: Which rental combo a transaction picks from.  The page finds the picker by
#: its ``data-rental-picker`` attribute, so the script never names a DOM id.
PICKER_RETURN = 'return'
PICKER_PAYMENT = 'payment'
PICKER_ADJUSTMENT = 'adjustment'
PICKERS = (PICKER_RETURN, PICKER_PAYMENT, PICKER_ADJUSTMENT)

#: The field checks the page script runs before it lets a transaction post.
#: Each name is one function in the script's ``CHECKS`` table; a transaction
#: lists the checks it needs, so adding a rule means adding a name here and a
#: function there — not another branch in the submit handler.
CHECK_CUSTOMER = 'customer'                    # customer name chosen / typed
CHECK_PROJECT = 'project'                      # own site / project chosen
CHECK_RENTAL = 'rental'                        # the rental picker is filled
CHECK_LINES = 'lines'                          # store lines added and valid
CHECK_TRANSFER = 'transfer'                    # holders ticked, qty to move
CHECK_RETURN_ITEMS = 'return_items'            # qty coming back on a return
CHECK_PAYMENT = 'payment'                      # amount + receiving account
CHECK_ADJUSTMENT = 'adjustment'                # amount to write off
CHECK_DAMAGE = 'damage'                        # piece(s) to write off
CHECK_MAINTENANCE_SEND = 'maintenance_send'    # piece(s) to send to the shop
CHECK_MAINTENANCE_RETURN = 'maintenance_return'  # piece(s) coming back
CHECKS = (CHECK_CUSTOMER, CHECK_PROJECT, CHECK_RENTAL, CHECK_LINES,
          CHECK_TRANSFER, CHECK_RETURN_ITEMS, CHECK_PAYMENT,
          CHECK_ADJUSTMENT, CHECK_DAMAGE, CHECK_MAINTENANCE_SEND,
          CHECK_MAINTENANCE_RETURN)


def _option(raw: dict, group_key: str) -> dict:
    """One option, with its defaults filled and its group stamped on.

    ``needs_rental`` is derived from ``picker`` so the two can never disagree.
    """
    option = {
        'icon': 'fa-circle-dot',
        'kind': KIND_CREATE,
        'panels': (),
        'checks': (),
        'picker': None,
        'eligible_for': None,
        'renter_type': None,
        'return_type': None,
        'submit_class': 'btn-primary',
        'help': '',
        'confirm': '',
    }
    option.update(raw)
    option['group'] = group_key
    option['needs_rental'] = bool(option['picker'])
    return option


#: The three groups of the transaction page.  Order matters: it is the order of
#: the tabs, of the dropdown and of the button on every row.
TXN_GROUPS = (
    {
        'key': 'rent_out',
        'label': 'Rent Out',
        'icon': 'fa-truck-fast',
        'help': 'Out to a customer or an HDC site, or on to the next holder.',
        'options': (
            _option({
                'key': 'rent_out_customer',
                'label': 'Rent Out to Customer',
                'icon': 'fa-user-tie',
                'kind': KIND_CREATE,
                'endpoint': 'hdc_tool_rental_create',
                'renter_type': 'external',
                'panels': (PANEL_RENTOUT_SOURCE, PANEL_LINES, PANEL_DESTINATION,
                           PANEL_TERMS),
                'checks': (CHECK_CUSTOMER, CHECK_LINES),
                'submit_label': 'Create Rental',
                'submit_class': 'btn-success',
                'help': 'Tools leave the store and rent starts for an outside customer.',
                'confirm': 'Create this rental and send the tools to the customer?',
            }, 'rent_out'),
            _option({
                'key': 'issue_own_site',
                'label': 'Issue to Own Site',
                'icon': 'fa-diagram-project',
                'kind': KIND_CREATE,
                'endpoint': 'hdc_tool_rental_create',
                'renter_type': 'internal',
                'panels': (PANEL_RENTOUT_SOURCE, PANEL_LINES, PANEL_DESTINATION,
                           PANEL_TERMS),
                'checks': (CHECK_PROJECT, CHECK_LINES),
                'submit_label': 'Issue to Site',
                'help': 'Move tools to an HDC site or project. It is not an outside rental.',
                'confirm': 'Issue these tools to the selected HDC site?',
            }, 'rent_out'),
            _option({
                'key': 'transfer_site',
                'label': 'Transfer to Another Site',
                'icon': 'fa-right-left',
                'kind': KIND_TRANSFER,
                'endpoint': 'hdc_tool_rental_create',
                'renter_type': 'internal',
                'panels': (PANEL_RENTOUT_SOURCE, PANEL_TRANSFER_SOURCE,
                           PANEL_DESTINATION, PANEL_TERMS),
                'checks': (CHECK_TRANSFER, CHECK_PROJECT),
                'submit_label': 'Complete Transfer',
                'submit_class': 'btn-warning',
                'help': 'Move tools that are out on to another HDC site, with a fresh rate.',
                'confirm': 'Transfer the selected tools and close their current cycle?',
            }, 'rent_out'),
            _option({
                'key': 'transfer_customer',
                'label': 'Transfer to Another Customer',
                'icon': 'fa-people-arrows',
                'kind': KIND_TRANSFER,
                'endpoint': 'hdc_tool_rental_create',
                'renter_type': 'external',
                'panels': (PANEL_RENTOUT_SOURCE, PANEL_TRANSFER_SOURCE,
                           PANEL_DESTINATION, PANEL_TERMS),
                'checks': (CHECK_TRANSFER, CHECK_CUSTOMER),
                'submit_label': 'Transfer to Customer',
                'submit_class': 'btn-warning',
                'help': 'Hand rented tools to another customer where the holder allows it.',
                'confirm': 'Transfer the selected tools to this customer?',
            }, 'rent_out'),
            _option({
                'key': 'rental_payment',
                'label': 'Receive Rent Payment',
                'icon': 'fa-money-bill-wave',
                'kind': KIND_PAYMENT,
                'endpoint': 'hdc_tool_rental_payment',
                'picker': PICKER_PAYMENT,
                'eligible_for': ELIGIBLE_PENDING_AMOUNT,
                'panels': (PANEL_PAYMENT,),
                'checks': (CHECK_RENTAL, CHECK_PAYMENT),
                'submit_label': 'Receive Payment',
                'submit_class': 'btn-success',
                'help': 'Money in. The tools stay exactly where they are.',
                'confirm': 'Post this rent receipt and update the outstanding balance?',
            }, 'rent_out'),
            _option({
                'key': 'credit_adjustment',
                'label': 'Credit / Waive-off',
                'icon': 'fa-percent',
                'kind': KIND_ADJUSTMENT,
                'endpoint': 'hdc_tool_rental_add_discount',
                'picker': PICKER_ADJUSTMENT,
                'eligible_for': ELIGIBLE_PENDING_AMOUNT,
                'panels': (PANEL_ADJUSTMENT,),
                'checks': (CHECK_RENTAL, CHECK_ADJUSTMENT),
                'submit_label': 'Post Adjustment',
                'submit_class': 'btn-warning',
                'help': 'Reduce what is owed without taking cash. Finance roles only.',
                'confirm': 'Post this credit against the rental balance?',
            }, 'rent_out'),
        ),
    },
    {
        'key': 'return',
        'label': 'Return',
        'icon': 'fa-rotate-left',
        'help': 'Coming back partly or fully, or written off as lost.',
        'options': (
            _option({
                'key': 'partial_return',
                'label': 'Partial Return',
                'icon': 'fa-rotate-left',
                'kind': KIND_RETURN,
                'endpoint': 'hdc_tool_rental_return',
                'picker': PICKER_RETURN,
                'eligible_for': ELIGIBLE_PENDING_TOOLS,
                'return_type': 'partial',
                'panels': (PANEL_RETURN, PANEL_SETTLEMENT),
                'checks': (CHECK_RENTAL, CHECK_RETURN_ITEMS),
                'submit_label': 'Record Partial Return',
                'help': 'Bring back only the quantities that actually came back.',
                'confirm': 'Record this partial return and settle what is being paid?',
            }, 'return'),
            _option({
                'key': 'full_return',
                'label': 'Full Return',
                'icon': 'fa-circle-check',
                'kind': KIND_RETURN,
                'endpoint': 'hdc_tool_rental_return',
                'picker': PICKER_RETURN,
                'eligible_for': ELIGIBLE_PENDING_TOOLS,
                'return_type': 'full',
                'panels': (PANEL_RETURN, PANEL_SETTLEMENT),
                'checks': (CHECK_RENTAL,),
                'submit_label': 'Record Full Return',
                'submit_class': 'btn-success',
                'help': 'Close the physical issue: every piece still out comes back.',
                'confirm': 'Return everything still outstanding on this rental?',
            }, 'return'),
            _option({
                'key': 'return_to_store',
                'label': 'Return to Store (no payment)',
                'icon': 'fa-warehouse',
                'kind': KIND_RETURN,
                'endpoint': 'hdc_tool_rental_return',
                'picker': PICKER_RETURN,
                'eligible_for': ELIGIBLE_PENDING_TOOLS,
                'return_type': 'partial',
                'panels': (PANEL_RETURN,),
                'checks': (CHECK_RENTAL, CHECK_RETURN_ITEMS),
                'submit_label': 'Return to Store',
                'submit_class': 'btn-secondary',
                'help': 'Put chosen pieces back in the warehouse without collecting rent now.',
                'confirm': 'Put the selected pieces back into the store?',
            }, 'return'),
            _option({
                'key': 'damage_loss',
                'label': 'Damage or Loss (write off)',
                'icon': 'fa-triangle-exclamation',
                'kind': KIND_DAMAGE_LOSS,
                'endpoint': 'hdc_tool_txn_damage_loss',
                'panels': (PANEL_DAMAGE,),
                'checks': (CHECK_DAMAGE,),
                'submit_label': 'Record Damage / Loss',
                'submit_class': 'btn-danger',
                'help': 'Write off broken or missing pieces, from the store or from a holder.',
                'confirm': 'Write off these pieces? Stock drops and it cannot be undone from here.',
            }, 'return'),
        ),
    },
    {
        'key': 'maintenance',
        'label': 'Maintenance',
        'icon': 'fa-screwdriver-wrench',
        'help': 'Leaving the rentable pool for the workshop, and coming back.',
        'options': (
            _option({
                'key': 'maintenance_out',
                'label': 'Send for Maintenance',
                'icon': 'fa-screwdriver-wrench',
                'kind': KIND_MAINTENANCE_OUT,
                'endpoint': 'hdc_tool_txn_maintenance',
                'panels': (PANEL_MAINTENANCE_SEND,),
                'checks': (CHECK_MAINTENANCE_SEND,),
                'submit_label': 'Send for Maintenance',
                'help': 'Pieces leave the available pool but stay owned and stay traceable.',
                'confirm': 'Send the selected pieces for maintenance?',
            }, 'maintenance'),
            _option({
                'key': 'maintenance_in',
                'label': 'Back from Maintenance',
                'icon': 'fa-clipboard-check',
                'kind': KIND_MAINTENANCE_IN,
                'endpoint': 'hdc_tool_txn_maintenance_return',
                'panels': (PANEL_MAINTENANCE_RETURN,),
                'checks': (CHECK_MAINTENANCE_RETURN,),
                'submit_label': 'Return from Maintenance',
                'submit_class': 'btn-success',
                'help': 'Pieces come back to the store — repaired, or written off if they are not.',
                'confirm': 'Record these pieces as back from maintenance?',
            }, 'maintenance'),
        ),
    },
)

#: The same tree, flattened: every option once, in tab order.  This is what the
#: routes and tests iterate over.
TXN_OPTIONS = tuple(option for group in TXN_GROUPS for option in group['options'])
TXN_OPTION_KEYS = tuple(option['key'] for option in TXN_OPTIONS)
DEFAULT_TXN_OPTION = TXN_OPTION_KEYS[0]


def txn_group(key: str) -> Optional[dict]:
    """Return the group entry for ``key`` (``None`` when unknown)."""
    for group in TXN_GROUPS:
        if group['key'] == key:
            return group
    return None


def txn_option(key: str) -> Optional[dict]:
    """Return the transaction-page option for ``key`` (``None`` when unknown)."""
    for option in TXN_OPTIONS:
        if option['key'] == key:
            return option
    return None


def registry_problems() -> list:
    """Everything wrong with the table above, as plain sentences.

    The registry is data the whole page trusts, so it checks itself: one test
    asserts this list is empty instead of re-stating every rule by hand.
    """
    problems = []
    if not TXN_GROUPS:
        return ['The transaction registry is empty.']
    for group in TXN_GROUPS:
        where = f"group {group.get('key')!r}"
        for field in ('key', 'label', 'icon', 'help'):
            if not group.get(field):
                problems.append(f'{where} has no {field}.')
        options = group.get('options') or ()
        if not options:
            problems.append(f'{where} offers no transaction.')
        for option in options:
            key = option.get('key')
            option_where = f'option {key!r}'
            for field in ('label', 'icon', 'help', 'submit_label', 'confirm'):
                if not option.get(field):
                    problems.append(f'{option_where} has no {field}.')
            if option.get('kind') not in KINDS:
                problems.append(f'{option_where} has an unknown kind.')
            panels = option.get('panels') or ()
            if not panels:
                problems.append(f'{option_where} shows no panel.')
            for panel in panels:
                if panel not in PANELS:
                    problems.append(f'{option_where} names an unknown panel {panel!r}.')
            picker = option.get('picker')
            if picker is not None and picker not in PICKERS:
                problems.append(f'{option_where} names an unknown picker {picker!r}.')
            if bool(picker) != bool(option.get('needs_rental')):
                problems.append(f'{option_where} disagrees about needing a rental.')
            if bool(picker) != bool(option.get('eligible_for')):
                problems.append(f'{option_where} disagrees about its rental picker rule.')
            for check in option.get('checks') or ():
                if check not in CHECKS:
                    problems.append(f'{option_where} asks for an unknown check {check!r}.')
            in_money = option.get('kind') in MONEY_KINDS
            in_physical = option.get('kind') in PHYSICAL_KINDS
            if in_money == in_physical:
                problems.append(f'{option_where} is neither money nor movement.')
    keys = list(TXN_OPTION_KEYS)
    if len(keys) != len(set(keys)):
        problems.append('Two transactions share one key.')
    group_keys = [group['key'] for group in TXN_GROUPS]
    if len(group_keys) != len(set(group_keys)):
        problems.append('Two groups share one key.')
    return problems
