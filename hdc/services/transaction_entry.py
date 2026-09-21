"""HDC services.transaction_entry — the New Transaction form's server side.

The New Transaction form (``/hdc/accounts/new-transaction`` and the entry card
on the Cash Flow register) is a *thin* front end over the register engine in
``hdc/services/cashflow_register.py``.  Nothing in this module invents its own
accounting: it only

* parses the posted form into the keyword arguments
  :func:`~hdc.services.cashflow_register.save_manual_cash_flow_entry` expects,
* keeps a one-shot copy of a rejected submission in the session so a server
  validation error can never erase what the user typed (see *draft* below),
* and exposes the three get-or-create helpers behind the ``+ Add New …``
  actions in the Account / Party / Project pickers.

Source of truth, unchanged by this module: money movements are
``hdc.models.accounts.AccountTransaction`` rows posted by the cash flow
register; accounts are ``hdc.models.accounts.Account``; categories,
subcategories and parties are the ``CashFlowCategory`` / ``CashFlowSubcategory``
/ ``CashFlowParty`` vocabulary; projects are ``hdc.models.projects.Project``.

The draft
---------
The register is POST-redirect-GET (a reload must never re-post money), so a
``ValueError`` from the engine used to bounce the user back to an empty form.
:func:`stash_entry_form` now saves the submitted values *and* the error message
in the session, :func:`pop_entry_form` returns them once to the next render, and
the template redisplays them next to the field that caused the problem.  The
flash message is still raised, so the existing behaviour and its tests are
untouched.

Layer: ``services`` — may import models, utils and other services.
"""

from __future__ import annotations

import secrets

from flask import has_request_context, session
from sqlalchemy import func

from hdc.extensions import db
from hdc.models.accounts import Account
from hdc.models.cashflow import CashFlowCategory, CashFlowSubcategory
from hdc.models.projects import Project
from hdc.services.accounts import _create_account
from hdc.services.cashflow_register import (
    _cf_normalize_direction,
    category_options,
    party_options,
    save_manual_cash_flow_entry,
    save_cf_party,
)
from hdc.services.lookups import _next_project_code
from hdc.utils.dates import _pkt_now_naive, _pkt_today
from hdc.utils.format import _parse_date, _payload_int
from hdc.utils.money import to_minor

__all__ = [
    "DRAFT_ERROR_KEY",
    "DRAFT_SESSION_KEY",
    "ENTRY_FORM_FIELDS",
    "MONEY_ACCOUNT_TYPES",
    "clear_entry_form",
    "create_cashflow_party",
    "create_entry_from_form",
    "create_money_account",
    "create_project_quick",
    "entry_form_context",
    "entry_form_values",
    "money_accounts",
    "party_types",
    "pop_entry_form",
    "posted_datetime_from_form_date",
    "resolve_entry_selection",
    "stash_entry_form",
]

# Account types that can hold real money — the only ones the entry form offers.
MONEY_ACCOUNT_TYPES = ('cash', 'bank', 'company')

# Party kinds offered by the ``+ Add New Party`` modal.  Mirrors the comment on
# ``CashFlowParty.party_type`` so the register vocabulary stays the source.
PARTY_TYPES = (
    ('client', 'Client / Owner'),
    ('supplier', 'Supplier / Vendor'),
    ('worker', 'Worker / Labour'),
    ('staff', 'Office Staff'),
    ('subcontractor', 'Subcontractor'),
    ('other', 'Other'),
)

# Every field the New Transaction form posts.  The tuple doubles as the
# whitelist for the session draft, so it is what gets replayed back.
ENTRY_FORM_FIELDS = (
    'direction',
    'date',
    'amount',
    'account_id',
    'destination_account_id',
    'category_id',
    'subcategory_id',
    'party_name',
    'party_type',
    'project_id',
    'reference',
    'description',
    'note',
)

DRAFT_SESSION_KEY = 'hdc_txn_entry_draft'
DRAFT_ERROR_KEY = 'hdc_txn_entry_draft_error'


def party_types():
    """The ``(value, label)`` pairs the party modal offers."""
    return PARTY_TYPES


# ---------------------------------------------------------------------------
# form -> service arguments
# ---------------------------------------------------------------------------

def posted_datetime_from_form_date(raw_date):
    """Build a PKT datetime from a ``YYYY-MM-DD`` form field (time = now)."""
    d = _parse_date((raw_date or '').strip(), fallback=None)
    if d is None:
        return None
    now = _pkt_now_naive()
    return now.replace(year=d.year, month=d.month, day=d.day)


def entry_form_values(form):
    """Whitelist + trim the posted entry form into a plain ``dict``.

    Only :data:`ENTRY_FORM_FIELDS` survive, so nothing a caller happens to put
    in ``request.form`` can leak into the session draft.
    """
    return {name: (form.get(name) or '').strip() for name in ENTRY_FORM_FIELDS}


def create_entry_from_form(form, *, actor=None, commit=True):
    """Post a register entry from a posted New Transaction form.

    Returns ``(entry, created)`` exactly like
    :func:`~hdc.services.cashflow_register.save_manual_cash_flow_entry`.  All
    validation — direction, amount, account, transfer legs, category /
    subcategory pairing, project, day lock, overdraft — lives in the engine and
    stays authoritative; this function only marshals the request.
    """
    return save_manual_cash_flow_entry(
        direction=form.get('direction'),
        amount=form.get('amount'),
        account_id=_payload_int(form, 'account_id'),
        destination_account_id=(_payload_int(form, 'destination_account_id') or None),
        category_id=(_payload_int(form, 'category_id') or None),
        category_name=(form.get('category_name') or '').strip() or None,
        subcategory_id=(_payload_int(form, 'subcategory_id') or None),
        subcategory_name=(form.get('subcategory_name') or '').strip() or None,
        party_id=(_payload_int(form, 'party_id') or None),
        party_name=(form.get('party_name') or '').strip() or None,
        party_type=(form.get('party_type') or '').strip() or 'other',
        description=(form.get('description') or '').strip() or None,
        note=(form.get('note') or '').strip() or None,
        reference=(form.get('reference') or '').strip() or None,
        date_posted=posted_datetime_from_form_date(form.get('date')),
        project_id=(_payload_int(form, 'project_id') or None),
        stage_id=(_payload_int(form, 'stage_id') or None),
        idempotency_key=(form.get('_idempotency_key') or '').strip() or None,
        actor=actor,
        commit=commit,
    )


# ---------------------------------------------------------------------------
# the "never lose what was typed" draft
# ---------------------------------------------------------------------------

def stash_entry_form(form, error=None):
    """Remember a rejected submission (values + message) for the next render."""
    if not has_request_context():
        return
    try:
        session[DRAFT_SESSION_KEY] = entry_form_values(form)
        session[DRAFT_ERROR_KEY] = (str(error) if error else '')
        session.modified = True
    except Exception:  # pragma: no cover - a broken session must not 500 a POST
        pass


def pop_entry_form():
    """Return ``(values, error)`` stashed by :func:`stash_entry_form`, once.

    Both keys are removed as they are read, so the values are replayed into a
    single render and a later refresh shows a clean form again.
    """
    if not has_request_context():
        return {}, ''
    try:
        values = session.pop(DRAFT_SESSION_KEY, None) or {}
        error = session.pop(DRAFT_ERROR_KEY, '') or ''
    except Exception:  # pragma: no cover - defensive, mirrors stash
        return {}, ''
    if not isinstance(values, dict):
        return {}, ''
    clean = {name: values.get(name, '') for name in ENTRY_FORM_FIELDS}
    clean = {k: (v if isinstance(v, str) else str(v or '')) for k, v in clean.items()}
    return clean, str(error)


def clear_entry_form():
    """Drop any stashed draft (called after a successful post)."""
    if not has_request_context():
        return
    try:
        session.pop(DRAFT_SESSION_KEY, None)
        session.pop(DRAFT_ERROR_KEY, None)
        session.modified = True
    except Exception:  # pragma: no cover - defensive, mirrors stash
        pass


# ---------------------------------------------------------------------------
# pickers
# ---------------------------------------------------------------------------

def money_accounts(active_only=True):
    """Active treasury accounts (cash / bank / company) for the pickers."""
    q = Account.query.filter(
        Account.is_void == False,  # noqa: E712
        func.lower(func.coalesce(Account.type, '')).in_(MONEY_ACCOUNT_TYPES),
    )
    if active_only:
        q = q.filter(func.lower(func.coalesce(Account.status, 'active')) == 'active')
    return q.order_by(Account.name.asc(), Account.id.asc()).all()


def entry_form_context(form_values=None, error=None):
    """The template data the New Transaction form renders from.

    Both entry surfaces (``/hdc/accounts/new-transaction`` and the register's
    entry card) build their context here so the two can never drift into
    showing different accounts, categories or options.  It lives in this module
    rather than in a route because a route module may not import another route
    module (see ``scripts/check_layers.py``).

    ``form_values`` is a submitted-but-rejected form being replayed, so the user
    gets their numbers back next to the error instead of an empty form.
    """
    values = dict(form_values or {})
    today = _pkt_today().isoformat()
    return {
        # A fresh key per render: the engine turns the second post of the same
        # key into a no-op, so a double-click can never post the money twice.
        'form_token': secrets.token_hex(16),
        'txn_values': values,
        'txn_error': error or '',
        'txn_selection': resolve_entry_selection(
            direction=values.get('direction'),
            account_id=_payload_int(values, 'account_id'),
            destination_account_id=_payload_int(values, 'destination_account_id'),
            category_id=_payload_int(values, 'category_id'),
            subcategory_id=_payload_int(values, 'subcategory_id'),
            project_id=_payload_int(values, 'project_id'),
        ),
        'txn_date_value': values.get('date') or today,
        'txn_today': today,
        # The pickers' vocabulary.  Everything comes from the register tables —
        # the form never hard-codes a category, subcategory or party type.
        'accounts': money_accounts(),
        'categories': category_options(),
        'parties': party_options(),
        'projects': Project.query.order_by(Project.name.asc(), Project.id.asc()).all(),
        'party_type_options': PARTY_TYPES,
    }


def resolve_entry_selection(*, direction, account_id, destination_account_id,
                            category_id, subcategory_id, project_id):
    """Re-resolve posted ids against the database when re-rendering the form.

    Used by the draft replay so the ``<select>`` the browser is handed already
    highlights what the user picked, even when the pick is what got the post
    rejected (an id that no longer resolves simply renders unselected).
    """
    cat = db.session.get(CashFlowCategory, int(category_id)) if category_id else None
    sub = db.session.get(CashFlowSubcategory, int(subcategory_id)) if subcategory_id else None
    if cat is not None and sub is not None and int(sub.category_id or 0) != int(cat.id):
        sub = None       # stale pairing: never hand it back to the form
    return {
        'direction': _cf_normalize_direction(direction) if direction else '',
        'account_id': int(account_id) if account_id else 0,
        'destination_account_id': int(destination_account_id) if destination_account_id else 0,
        'category_id': int(cat.id) if cat is not None else 0,
        'subcategory_id': int(sub.id) if sub is not None else 0,
        'project_id': int(project_id) if project_id else 0,
    }


# ---------------------------------------------------------------------------
# "+ Add New …" helpers
# ---------------------------------------------------------------------------

def create_money_account(name, *, mode='cash', bank_name=None, account_number=None,
                         opening_balance=0.0):
    """Create a cash / bank account for the ``+ Add New Account`` action.

    Returns ``(row, created)``.  When the name already exists the existing row
    is returned with ``created=False`` — a typo in a modal must never fork a
    treasury account in two, and the caller can simply select what is there.
    """
    nm = (name or '').strip()
    if not nm:
        raise ValueError('Account name is required.')
    if len(nm) > 120:
        raise ValueError('Account name must be 120 characters or fewer.')

    existing = (Account.query
                .filter(Account.is_void == False,  # noqa: E712
                        func.lower(func.trim(Account.name)) == nm.lower())
                .first())
    if existing is not None:
        return existing, False

    raw_mode = (mode or 'cash').strip().lower()
    if raw_mode == 'bank':
        acc_type = 'bank'
    elif raw_mode == 'cash':
        acc_type = 'cash'
    else:
        raise ValueError('Choose whether this is a Cash account or a Bank account.')

    try:
        opening = float(to_minor(opening_balance)) / 100.0
    except Exception as exc:
        raise ValueError('Opening balance must be a valid number.') from exc
    if abs(opening) > 10 ** 12:
        raise ValueError('Opening balance is unrealistically large.')

    row, message = _create_account(
        nm, acc_type,
        opening_balance=opening,
        bank_name=(bank_name or '').strip(),
        account_number=(account_number or '').strip(),
    )
    if row is None:
        raise ValueError(message or 'Unable to create the account.')
    return row, True


def create_cashflow_party(name, *, party_type='other', phone=None):
    """Create a register party for ``+ Add New Party``.  Returns ``(row, created)``."""
    nm = (name or '').strip()
    if not nm:
        raise ValueError('Party name is required.')
    if len(nm) > 160:
        raise ValueError('Party name must be 160 characters or fewer.')
    ptype = (party_type or 'other').strip().lower() or 'other'
    if ptype not in {value for value, _ in PARTY_TYPES}:
        raise ValueError('Choose a valid party type.')
    return save_cf_party(nm, party_type=ptype, phone=(phone or '').strip() or None)


def create_project_quick(name, *, client=None, location=None):
    """Create a project for ``+ Add New Project``.  Returns ``(row, created)``.

    Only the name is required: a project has to exist before money can be booked
    against it, and forcing the full project master (contract type, rates,
    sq-ft) through a transaction modal would defeat the point of a quick add.
    The contract defaults match the rest of the app (``sqft`` / ``Active``) and
    the code comes from the shared ``_next_project_code`` helper, so nothing
    here can collide with the Projects page.
    """
    nm = (name or '').strip()
    if not nm:
        raise ValueError('Project name is required.')
    if len(nm) > 100:
        raise ValueError('Project name must be 100 characters or fewer.')

    existing = (Project.query
                .filter(func.lower(func.trim(Project.name)) == nm.lower())
                .first())
    if existing is not None:
        return existing, False

    row = Project(
        project_code=_next_project_code(),
        name=nm,
        client=(client or '').strip(),
        location=(location or '').strip(),
        total_constructed_sqft=0.0,
        owner_rate_per_sqft=0.0,
        owner_lump_sum=0.0,
        contract_type='sqft',
        status='Active',
    )
    db.session.add(row)
    db.session.flush()
    return row, True
