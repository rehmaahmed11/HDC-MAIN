"""HDC services.cashflow_register — the Cash Flow v2 engine.

Ported from the AMS *accounts + cash flow* model and adapted to HDC.  It adds
the four things AMS has that HDC's accounts layer did not:

1. **A user-facing cash flow register** (``hdc_cash_flow_entry``) with its own
   category / subcategory / party vocabulary, a reference number and a
   description — i.e. the *document*, on top of the ledger *posting*.
2. **Exact money.**  Every amount is stored twice: the legacy ``FLOAT`` column
   and an integer ``*_minor`` paisa mirror that is authoritative for
   arithmetic.  See ``hdc/utils/money.py``.
3. **Immutability.**  A posted entry is never edited or deleted.  *Amend*
   voids the old row and posts a replacement; *void* keeps the row and
   reverses it; *restore* brings a voided row back.  Every state change writes
   an audit row with who / when / why.
4. **Day close / reconciliation.**  Per-account daily positions
   (opening → in → out → expected → counted → difference) with a day lock that
   carries the counted closing forward as the next day's opening.

Balances are **never stored**.  HDC derives them from ``hdc_account_txn``
(``Account.opening_balance`` + incoming − outgoing), so this module only writes
double-entry rows and reads the derived figures back.  A stored balance would
be a second source of truth that can drift away from the ledger; see
``CASHFLOW_MODEL.md``.

Layer: ``services`` — may import models, utils and core.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from flask import current_app
from sqlalchemy import func, or_

from hdc.extensions import db
from hdc.models.accounts import Account, AccountTransaction
from hdc.models.cashflow import (
    AccountReconciliation,
    CashDayAccountPosition,
    CashDayLock,
    CashFlowCategory,
    CashFlowEntry,
    CashFlowEntryAudit,
    CashFlowParty,
    CashFlowSubcategory,
)
from hdc.utils.dates import _pkt_now_naive
from hdc.utils.money import from_minor, sync_money_fields, to_minor

__all__ = [
    "CF_DIR_IN",
    "CF_DIR_OUT",
    "CF_DIR_TRANSFER",
    "CF_DIRECTIONS",
    "CF_DIRECTION_LABELS",
    "SRC_MANUAL",
    "validate_manual_cash_flow",
    "save_manual_cash_flow_entry",
    "amend_manual_cash_flow_entry",
    "void_manual_cash_flow_entry",
    "restore_manual_cash_flow_entry",
    "register_rows",
    "register_row_dicts",
    "register_summary",
    "day_totals",
    "is_day_locked",
    "ensure_cashflow_seed_data",
    "backfill_transaction_minor_units",
    "category_options",
    "subcategory_options",
    "party_options",
    "party_type_label",
    "party_type_options",
    "category_field_rules",
    "category_rules_map",
    "save_cf_party",
    "save_cf_category",
    "save_cf_subcategory",
    "day_positions",
    "day_lock_state",
    "save_counted_position",
    "lock_cash_day",
    "unlock_cash_day",
    "assert_period_open",
    "reconcile_account",
    "ensure_cashflow_seed_data",
    "PROJECT_EFFECTS",
    "backfill_project_receipt_owner_payments",
]

CF_DIR_IN = 'in'
CF_DIR_OUT = 'out'
CF_DIR_TRANSFER = 'transfer'
CF_DIRECTIONS = (CF_DIR_IN, CF_DIR_OUT, CF_DIR_TRANSFER)

CF_DIRECTION_LABELS = {
    CF_DIR_IN: 'Received',
    CF_DIR_OUT: 'Spent',
    CF_DIR_TRANSFER: 'Transfer',
}

SRC_MANUAL = 'MANUAL_CASH_FLOW'
SRC_QUICK_ENTRY = 'CASH_FLOW_QUICK_ENTRY'

# Account types that hold real money (cash / bank / company treasury).
_MONEY_ACCOUNT_TYPES = ('company', 'cash', 'bank')

# ---------------------------------------------------------------------------
# party vocabulary — the *criteria* the Party / Person picker is built from
# ---------------------------------------------------------------------------

#: Every ``CashFlowParty.party_type`` the app understands, with the label shown
#: in the pickers.  ``lender`` / ``borrower`` are the loan parties: a lender
#: gives us a loan (we owe them), a borrower takes a loan from us (they owe us).
PARTY_TYPES = (
    ('client', 'Client / Owner'),
    ('supplier', 'Supplier / Vendor'),
    ('worker', 'Worker / Labour'),
    ('staff', 'Office Staff'),
    ('subcontractor', 'Subcontractor'),
    ('lender', 'Loan Giver / Financier'),
    ('borrower', 'Loan Taker / Borrower'),
    ('other', 'Other'),
)

PARTY_TYPE_VALUES = tuple(value for value, _label in PARTY_TYPES)

#: Types that mean "this party has a loan with us".
LOAN_PARTY_TYPES = ('lender', 'borrower')

#: The four loan movements a category can be tagged with (``loan_effect``).
LOAN_EFFECTS = ('take', 'give', 'repay', 'recover')

#: Project side-effects a category can be tagged with (``project_effect``).
#: ``receipt`` = money received from a project's owner/client, which is mirrored
#: into ``hdc_owner_payment`` so the project's received / outstanding figures
#: follow the register instead of drifting away from it.
PROJECT_EFFECTS = ('receipt',)

LOAN_EFFECT_LABELS = {
    'take': 'Loan taken (money in, we owe)',
    'give': 'Loan given (money out, they owe)',
    'repay': 'Loan repayment (money out, reduces what we owe)',
    'recover': 'Loan recovery (money in, reduces what we are owed)',
}

#: Default party type used when a category is tagged with a loan effect.
LOAN_EFFECT_PARTY_TYPE = {
    'take': 'lender',
    'give': 'borrower',
    'repay': 'lender',
    'recover': 'borrower',
}


def party_type_label(value):
    """Label for a ``party_type`` value (falls back to the raw value)."""
    key = (value or '').strip().lower()
    for candidate, label in PARTY_TYPES:
        if candidate == key:
            return label
    return key or 'Other'


def party_type_options():
    """The ``(value, label)`` pairs every party picker / modal offers."""
    return PARTY_TYPES

# register ledger type used for each register direction
_CF_TX_TYPE = {
    CF_DIR_IN: 'party_receipt',
    CF_DIR_OUT: 'party_payment',
    CF_DIR_TRANSFER: 'transfer',
}


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _cf_actor_name(actor):
    """Best-effort username for ``actor`` (a user object, a string or None)."""
    if actor is None:
        try:
            from flask import has_request_context
            from flask_login import current_user
            if has_request_context() and getattr(current_user, 'is_authenticated', False):
                actor = current_user
        except Exception:
            actor = None
    if actor is None:
        return 'system'
    if isinstance(actor, str):
        return actor.strip() or 'system'
    return (str(getattr(actor, 'username', '') or '').strip() or 'system')


def _cf_normalize_direction(value):
    raw = (value or '').strip().lower()
    aliases = {
        'in': CF_DIR_IN, 'received': CF_DIR_IN, 'receive': CF_DIR_IN, 'receipt': CF_DIR_IN,
        'out': CF_DIR_OUT, 'spent': CF_DIR_OUT, 'spend': CF_DIR_OUT, 'payment': CF_DIR_OUT,
        'transfer': CF_DIR_TRANSFER, 'internal': CF_DIR_TRANSFER,
    }
    return aliases.get(raw, raw)


def _cf_is_money_account(account):
    """True when ``account`` is a live treasury account (cash / bank)."""
    if account is None:
        return False
    if bool(getattr(account, 'is_void', False)):
        return False
    if str(getattr(account, 'status', 'active') or 'active').strip().lower() != 'active':
        return False
    return str(getattr(account, 'type', '') or '').strip().lower() in _MONEY_ACCOUNT_TYPES


def _cf_type_label(direction):
    return CF_DIRECTION_LABELS.get(direction, direction or '')


def _cf_snapshot(entry):
    """JSON-serialisable snapshot of an entry for the audit trail."""
    return {
        'id': entry.id,
        'direction': entry.direction,
        'amount': entry.amount,
        'amount_minor': entry.amount_minor,
        'account_id': entry.account_id,
        'destination_account_id': entry.destination_account_id,
        'category_id': entry.category_id,
        'subcategory_id': entry.subcategory_id,
        'party_id': entry.party_id,
        'party_name': entry.party_name,
        'party_type': entry.party_type,
        'description': entry.description,
        'note': entry.note,
        'reference': entry.reference,
        'date_posted': (entry.date_posted.isoformat() if entry.date_posted else None),
        'project_id': entry.project_id,
        'stage_id': entry.stage_id,
        'is_void': entry.is_void,
        'revision': entry.revision,
    }


def _cf_write_audit(entry, action, before=None, after=None, reason=None, actor=None):
    """Append an immutable audit row for ``entry``."""
    row = CashFlowEntryAudit(
        entry_id=entry.id,
        action=str(action or '')[:20],
        before_json=(json.dumps(before, ensure_ascii=False, default=str) if before else None),
        after_json=(json.dumps(after, ensure_ascii=False, default=str) if after else None),
        reason=(reason or '').strip() or None,
        changed_by=_cf_actor_name(actor),
        changed_at=_pkt_now_naive(),
    )
    db.session.add(row)
    db.session.flush()
    return row


def _cf_find_party(party_id=None, name=None, party_type=None, active_only=True):
    if party_id:
        row = db.session.get(CashFlowParty, int(party_id))
        if row and ((not active_only) or row.is_active):
            return row
    nm = (name or '').strip()
    if not nm:
        return None
    q = CashFlowParty.query.filter(func.lower(func.trim(CashFlowParty.name)) == nm.lower())
    if active_only:
        q = q.filter(CashFlowParty.is_active == True)  # noqa: E712
    if party_type:
        q = q.filter(func.lower(CashFlowParty.party_type) == str(party_type).strip().lower())
    return q.first()


def _cf_resolve_project(project_id, stage_id=None):
    """Validate the optional project / stage a form submitted.

    A project id that does not resolve is rejected rather than dropped: quietly
    posting the money with no project is how a cost ends up attributed to
    nothing, which is worse than asking the user to pick again.  The same goes
    for a stage that is not part of the chosen project.
    """
    if not project_id:
        if stage_id:
            raise ValueError('Select a project before choosing a stage.')
        return None, None
    from hdc.models.projects import Project, Stage
    row = db.session.get(Project, int(project_id))
    if row is None:
        raise ValueError('That project no longer exists. Pick another project.')
    stage = db.session.get(Stage, int(stage_id)) if stage_id else None
    if stage_id and stage is None:
        raise ValueError('That stage no longer exists. Pick another stage.')
    if stage is not None and int(stage.project_id or 0) != int(row.id):
        raise ValueError('That stage does not belong to the selected project.')
    return row, stage


def _cf_resolve_category(direction, category_id=None, category_name=None,
                         required=True, create_if_missing=True):
    """Resolve (or create) the cash-flow category for an entry."""
    cat = None
    if category_id:
        cat = db.session.get(CashFlowCategory, int(category_id))
    if cat is None and (category_name or '').strip():
        nm = category_name.strip()
        cat = CashFlowCategory.query.filter(
            func.lower(func.trim(CashFlowCategory.name)) == nm.lower()
        ).first()
        if cat is None and create_if_missing:
            cat = save_cf_category(nm, direction=direction if direction != CF_DIR_TRANSFER else 'both')
    if cat is None and required:
        raise ValueError('Select a cash flow category.')
    if cat is not None:
        allowed = (getattr(cat, 'direction', 'both') or 'both').strip().lower()
        if direction != CF_DIR_TRANSFER and allowed not in ('both', direction):
            raise ValueError(
                f'Category "{cat.name}" is not allowed for {_cf_type_label(direction).lower()} entries.'
            )
    return cat


def _cf_resolve_subcategory(category, subcategory_id=None, subcategory_name=None,
                            create_if_missing=True):
    if category is None:
        return None
    sub = None
    if subcategory_id:
        sub = db.session.get(CashFlowSubcategory, int(subcategory_id))
        if sub is None:
            raise ValueError('That subcategory no longer exists. Pick another one.')
        if int(sub.category_id or 0) != int(category.id):
            # A stale Category + Subcategory pair.  Silently dropping it used to
            # save an entry that disagreed with what the user saw on screen, so
            # the mismatch is now rejected outright.
            raise ValueError(
                f'"{sub.name}" is not a subcategory of "{category.name}". '
                'Pick a subcategory from the selected category.'
            )
    if sub is None and (subcategory_name or '').strip():
        nm = subcategory_name.strip()
        sub = CashFlowSubcategory.query.filter(
            CashFlowSubcategory.category_id == int(category.id),
            func.lower(func.trim(CashFlowSubcategory.name)) == nm.lower(),
        ).first()
        if sub is None and create_if_missing:
            sub = save_cf_subcategory(category.id, nm)
    return sub


# ---------------------------------------------------------------------------
# day locks — the "period closed" guard
# ---------------------------------------------------------------------------

def day_lock_state(day):
    """Return the :class:`CashDayLock` for ``day`` (a date), or ``None``."""
    if day is None:
        return None
    return CashDayLock.query.filter(CashDayLock.lock_date == day).first()


def is_day_locked(day):
    return day_lock_state(day) is not None


def assert_period_open(account_id, when, operation='posted'):
    """Raise ``ValueError`` when ``when`` falls on a locked financial day.

    This is the HDC equivalent of AMS's ``_assert_period_open``: once a day has
    been verified and locked on the reconciliation page, its counted totals are
    authoritative, so no mutation may land on or before that date.
    """
    if when is None:
        return
    day = when.date() if isinstance(when, datetime) else when
    lock = day_lock_state(day)
    if lock is None:
        return
    who = (lock.locked_by or '').strip()
    raise ValueError(
        f'Financial day {day.isoformat()} is locked'
        + (f' by {who}' if who else '')
        + f'. Unlock it before this entry can be {operation}.'
    )


# ---------------------------------------------------------------------------
# validation + posting
# ---------------------------------------------------------------------------

def validate_manual_cash_flow(*, direction, amount, account_id, destination_account_id=None,
                              category_id=None, category_name=None, subcategory_id=None,
                              subcategory_name=None, require_category=True,
                              create_missing=True, check_balance=True, skip_balance_account_id=None):
    """Validate a register submission.  Returns ``(direction, amount, account, destination)``.

    Raises ``ValueError`` with a user-facing message on any problem.
    """
    direction = _cf_normalize_direction(direction)
    if direction not in CF_DIRECTIONS:
        raise ValueError('Choose Received, Spent, or Transfer.')

    try:
        amount = float(from_minor(to_minor(amount)))
    except Exception as exc:
        raise ValueError('Amount must be a valid number.') from exc
    if amount <= 0:
        raise ValueError('Amount must be greater than zero.')

    account = db.session.get(Account, int(account_id)) if account_id else None
    if not _cf_is_money_account(account):
        raise ValueError('Select a valid active cash or bank account.')

    destination = None
    if direction == CF_DIR_TRANSFER:
        destination = db.session.get(Account, int(destination_account_id)) if destination_account_id else None
        if not _cf_is_money_account(destination):
            raise ValueError('Select a valid destination cash or bank account.')
        if int(destination.id) == int(account.id):
            raise ValueError('Source and destination accounts cannot be the same.')
    else:
        if require_category:
            _cf_resolve_category(
                direction, category_id, category_name,
                required=True, create_if_missing=create_missing,
            )

    if check_balance and direction in (CF_DIR_OUT, CF_DIR_TRANSFER):
        from hdc.services.accounts import _account_balance_map
        balances = _account_balance_map()
        available = float((balances or {}).get(int(account.id), 0.0) or 0.0)
        if skip_balance_account_id and int(skip_balance_account_id) == int(account.id):
            # The entry being amended is still posted, so its own amount is
            # counted twice; add it back before comparing.
            available += float(amount)
        if available + 1e-9 < amount:
            raise ValueError(f'Insufficient balance in {account.name}.')
    return direction, amount, account, destination


def _cf_resolve_counterparty_account(party_name, *, project=None, as_client=False):
    """Resolve (or lazily create) the ledger account standing behind a party.

    HDC's ledger is fully double-entry — every row needs both a ``from`` and a
    ``to`` account — so an external counterparty still needs an account to
    balance against.  Named parties get their own ``person`` account (the same
    convention the rest of Accounts uses); an unnamed receipt settles against
    the shared ``Credit/Debit Control`` account.

    ``as_client`` (set for a project-receipt category) changes two things, and
    both matter for the money to end up in the right place:

    * the name defaults to ``project.client`` — the owner of the project being
      paid for is a fact the system already stores, so it is never retyped and
      cannot fork into four spellings of one person;
    * the account is created with type ``client`` rather than ``person``.  That
      is not cosmetic: ``_account_group_mode_for_row`` files ``client`` under
      ``project_in_flow`` (counted by the Receivable KPI) and ``person`` under
      ``credit_debit`` (not counted).  Booking an owner receipt against a
      ``person`` account is what made these receipts invisible to the KPI.

    This is the same resolution the Projects page has always used
    (``_accounts_post_owner_receipt``), so both routes now name the *same*
    ledger account for the same client.
    """
    from hdc.services.accounts import _accounts_default_external_parties, _accounts_party_account
    nm = (party_name or '').strip()
    if as_client:
        # The project's own client wins; a typed name is only a fallback for a
        # project whose client field was never filled in.
        client_name = (getattr(project, 'client', '') or '').strip()
        nm = client_name or nm
        if nm:
            acc = _accounts_party_account(nm, 'client')
            if acc is not None:
                return acc
    elif nm:
        acc = _accounts_party_account(nm, 'person')
        if acc is not None:
            return acc
    return _accounts_default_external_parties()


def _cf_build_tx_payload(direction, amount, account, destination, description, note,
                         posted, project_id=None, stage_id=None, counterparty_account=None,
                         party_name=None):
    """Build the :class:`AccountTransaction` payload for a register entry.

    ``executed_by_account_id`` is pinned to the register account so the engine
    always produces exactly **one** ledger row — the register is a 1:1 view of
    a single movement, and an executor split (step 1 / step 2) would break
    that link.

    Direction -> ledger shape::

        in        counterparty/person account  ->  register account
        out       register account             ->  destination or party (may be
                                                   left open and recorded by
                                                   ``party_name`` alone)
        transfer  register account             ->  destination account
    """
    if direction == CF_DIR_IN:
        from_id = int(counterparty_account.id) if counterparty_account is not None else int(account.id)
        to_id = int(account.id)
    elif direction == CF_DIR_TRANSFER:
        from_id = int(account.id)
        to_id = int(destination.id) if destination is not None else None
    else:  # out — the payee may be unnamed, but the ledger still needs a
        # destination row, so fall back to the counterparty/control account.
        from_id = int(account.id)
        to_id = int(destination.id) if destination is not None else (
            int(counterparty_account.id) if counterparty_account is not None else None)
    if from_id == to_id:
        # Guards against a self-referential receipt with no counterparty.
        raise ValueError('Source and destination accounts cannot be the same.')
    return {
        'date': (posted.date().isoformat() if isinstance(posted, datetime) else str(posted)),
        'amount': float(amount),
        'type': _CF_TX_TYPE[direction],
        'from_account_id': from_id,
        'to_account_id': to_id,
        'executed_by_account_id': from_id,
        'category': {
            'party_receipt': 'income',
            'party_payment': 'expense',
            'transfer': 'transfer',
        }[_CF_TX_TYPE[direction]],
        'note': (note or description or '')[:400] or None,
        'reference_id': None,
        'party_name': (party_name or '').strip() or None,
        'project_id': (int(project_id) if project_id else None),
        'stage_id': (int(stage_id) if stage_id else None),
    }


def save_manual_cash_flow_entry(*, direction, amount, account_id, destination_account_id=None,
                                category_id=None, category_name=None, subcategory_id=None,
                                subcategory_name=None, party_id=None, party_name=None,
                                party_type=None, description=None, note=None, reference=None,
                                date_posted=None, idempotency_key=None, actor=None,
                                create_missing=True, project_id=None, stage_id=None,
                                source_type=SRC_MANUAL, source_id=None, commit=True,
                                validate_scope=True, enforce_category_rules=True):
    """Post a new register entry **and** its ledger transaction atomically.

    Returns ``(entry, created)``.  When ``idempotency_key`` matches an existing
    entry the existing row is returned with ``created=False`` — a retried or
    double-clicked form cannot double-post.

    ``validate_scope`` (default on) checks that a submitted ``project_id`` /
    ``stage_id`` actually exists before money is posted.  Amend turns it off for
    the leg it carries forward from the original entry, so an entry whose project
    was removed later can still be corrected.

    ``enforce_category_rules`` (default on) applies the category's own field
    rules — a category that needs a party (or a project) cannot be posted
    without one, and a party whose known type is not allowed on that category is
    refused.  The form hides those fields for a reason; this is the same rule
    enforced where it counts.
    """
    key = (idempotency_key or '').strip() or None
    if key:
        existing = CashFlowEntry.query.filter(CashFlowEntry.idempotency_key == key).first()
        if existing:
            return existing, False

    direction, amount, account, destination = validate_manual_cash_flow(
        direction=direction, amount=amount, account_id=account_id,
        destination_account_id=destination_account_id, category_id=category_id,
        category_name=category_name, subcategory_id=subcategory_id,
        subcategory_name=subcategory_name, require_category=(direction != CF_DIR_TRANSFER),
        create_missing=create_missing,
    )
    posted = date_posted or _pkt_now_naive()
    assert_period_open(int(account.id), posted, operation='posted')
    project_row = None
    if validate_scope:
        project_row, stage = _cf_resolve_project(project_id, stage_id)
        project_id = int(project_row.id) if project_row is not None else None
        stage_id = int(stage.id) if stage is not None else None

    cat = None
    sub = None
    if direction != CF_DIR_TRANSFER:
        cat = _cf_resolve_category(direction, category_id, category_name,
                                   required=True, create_if_missing=create_missing)
        sub = _cf_resolve_subcategory(cat, subcategory_id, subcategory_name,
                                      create_if_missing=create_missing)

    # ── a project receipt is about the project, not about a typed name ───────
    # The owner/client is derived from the project, so the same client can never
    # arrive under four spellings and the entry can never name somebody who has
    # nothing to do with the project being paid for.
    is_project_receipt = bool(cat is not None and getattr(cat, 'is_project_receipt', False))
    if is_project_receipt:
        if not project_id:
            raise ValueError(
                f'"{cat.name}" is money received for a project — pick the project.')
        if project_row is None:
            from hdc.models.projects import Project as _Project
            project_row = db.session.get(_Project, int(project_id))
            if project_row is None:
                raise ValueError('That project no longer exists. Pick another project.')
        owner_name = (getattr(project_row, 'client', '') or '').strip()
        typed = (party_name or '').strip()
        if owner_name:
            # The project's own owner always wins over whatever was typed: the
            # contract says who owes this money.
            party_name = owner_name
            party_id = None
            party_type = 'client'
        elif typed:
            # Project master has no client recorded — keep what was typed, but
            # file it as the client it functionally is.
            party_type = 'client'

    ptype = (party_type or 'other').strip().lower() or 'other'
    party = _cf_find_party(party_id=party_id, name=party_name, party_type=ptype)
    if party is None and (party_name or '').strip():
        party, _ = save_cf_party(party_name, ptype)
    if party is not None:
        party_name = party.name
        ptype = party.party_type or ptype

    # ── the category's own field rules ───────────────────────────────────────
    if cat is not None and enforce_category_rules:
        rules = category_field_rules(cat)
        if rules['project_mode'] == 'required' and not project_id:
            raise ValueError(f'"{cat.name}" needs a project — pick one.')
        if rules['party_mode'] == 'required' and not (party_name or '').strip() and not party_id:
            raise ValueError(
                f'"{cat.name}" needs a party — choose who this transaction is for.')
        if not party_type_allowed(rules, ptype):
            wanted = ', '.join(party_type_label(v) for v in rules['party_types'])
            raise ValueError(
                f'"{cat.name}" is for {wanted} — pick that kind of party '
                f'(the selected party is classified as {party_type_label(ptype)}).')

    desc = (description or '').strip()
    if not desc:
        desc = (cat.name if cat is not None else _cf_type_label(direction))
        if (party_name or '').strip():
            desc = f'{desc} — {party_name.strip()}'

    actor_name = _cf_actor_name(actor)
    entry = CashFlowEntry(
        direction=direction,
        amount=float(amount),
        account_id=int(account.id),
        destination_account_id=(int(destination.id) if destination is not None else None),
        category_id=(int(cat.id) if cat is not None else None),
        subcategory_id=(int(sub.id) if sub is not None else None),
        party_id=(int(party.id) if party is not None else None),
        party_name=(party_name or '').strip() or None,
        party_type=ptype,
        description=desc[:200],
        note=(note or '').strip() or None,
        reference=(reference or '').strip() or None,
        date_posted=posted,
        project_id=(int(project_id) if project_id else None),
        stage_id=(int(stage_id) if stage_id else None),
        created_by=actor_name,
        updated_by=actor_name,
        source_type=(source_type or SRC_MANUAL),
        source_id=source_id,
        is_void=False,
        revision=1,
        idempotency_key=key,
        created_at=_pkt_now_naive(),
        updated_at=_pkt_now_naive(),
    )
    sync_money_fields(entry, 'amount', 'amount_minor')

    from hdc.services.accounts import _create_account_transaction

    counterparty = None
    if direction in (CF_DIR_IN, CF_DIR_OUT):
        counterparty = _cf_resolve_counterparty_account(
            party_name, project=project_row, as_client=is_project_receipt)
        if counterparty is None:
            raise ValueError('Unable to resolve the counterparty account for this entry.')

    payload = _cf_build_tx_payload(direction, amount, account, destination, desc, note,
                                   posted, project_id=project_id, stage_id=stage_id,
                                   counterparty_account=counterparty,
                                   party_name=(party_name or '').strip())
    ok, msg, rows = _create_account_transaction(payload, commit=False)
    if not ok:
        raise ValueError(msg or 'Unable to post the ledger transaction.')

    tx = (rows or [None])[0]
    if tx is None:
        raise ValueError('Ledger transaction was not created.')

    # Exactness + traceability on the ledger row itself.
    sync_money_fields(tx, 'amount', 'amount_minor')
    tx.idempotency_key = key
    tx.reason = (note or '').strip() or None
    db.session.add(entry)
    db.session.flush()
    entry.account_tx_id = int(tx.id)
    # Back-link the ledger row to this document so Accounts-side voids and the
    # forensic scan can find each other (was missing → register/ledger drift).
    tx.source_type = f'cash_flow_entry_{entry.direction}'
    tx.source_id = int(entry.id)
    db.session.flush()

    _cf_write_audit(entry, 'Created', after=_cf_snapshot(entry), actor=actor)
    db.session.flush()

    # A loan category (loan_effect on the CashFlowCategory) mirrors the entry
    # into the loan ledger: the four loan movements are what make "who owes
    # whom, and how much is left" answerable per person.  This runs inside the
    # same transaction, so an entry can never exist without its loan movement.
    _cf_apply_loan_effect(entry, cat, actor=actor)

    # The project twin of the above: an owner/client receipt mirrors into
    # hdc_owner_payment, which is what Project.total_received and
    # remaining_receivable are computed from.  Same transaction, so a receipt
    # can never exist without the project knowing it was paid.
    _cf_apply_project_effect(entry, cat, project=project_row)

    if commit:
        db.session.commit()
    return entry, True


def _cf_apply_loan_effect(entry, category, actor=None):
    """Mirror a register entry into the loan ledger when its category says so."""
    effect = (getattr(category, 'loan_effect', '') or '').strip().lower()
    if not effect:
        return None
    # Imported lazily: hdc.services.loans posts through this module, so a
    # module-level import would be circular.
    from hdc.services.loans import apply_cash_flow_entry
    return apply_cash_flow_entry(entry, effect, actor=actor)


def _cf_owner_payment_for_entry(entry):
    """The ``OwnerPayment`` mirroring this register entry, if any."""
    from hdc.models.accounts import OwnerPayment
    if entry is None or not getattr(entry, 'id', None):
        return None
    return (OwnerPayment.query
            .filter(OwnerPayment.source_entry_id == int(entry.id))
            .first())


def _cf_apply_project_effect(entry, category, project=None):
    """Mirror an owner/client receipt into ``hdc_owner_payment``.

    ``Project.total_received`` and ``remaining_receivable`` are derived from
    ``OwnerPayment`` rows and from nothing else, so a receipt that does not
    create one leaves the project reading as unpaid no matter how much cash
    actually arrived.  This is the single line of code that connects the two.

    Idempotent (a row already pointing at this entry is returned untouched) and
    deliberately quiet about anything it is not responsible for: the money has
    already been posted by the caller, and this mirror never re-posts it.
    """
    from hdc.models.accounts import OwnerPayment

    if entry is None or category is None:
        return None
    if not getattr(category, 'is_project_receipt', False):
        return None
    if not entry.project_id:
        return None

    existing = _cf_owner_payment_for_entry(entry)
    if existing is not None:
        return existing

    remarks = (entry.description or '').strip() or f'Cash flow entry #{entry.id}'
    row = OwnerPayment(
        project_id=int(entry.project_id),
        amount=float(entry.amount or 0.0),
        date=(entry.date_posted.date() if entry.date_posted else _pkt_today()),
        # The account the money actually landed in, so the project's receipt
        # list and the treasury agree on where it went.
        received_to_account_id=(int(entry.account_id) if entry.account_id else None),
        remarks=remarks[:200],
        activity_at=_pkt_now_naive(),
        is_void=bool(entry.is_void),
        # The back-link that makes this mirror idempotent and lets a void on
        # either side find the other.
        source_entry_id=int(entry.id),
    )
    db.session.add(row)
    db.session.flush()
    return row


def _cf_sync_project_effect_void(entry, *, make_void, reason=None):
    """Keep the mirrored ``OwnerPayment`` in step when an entry is voided.

    A voided receipt must stop counting towards "what this project has been
    paid" — otherwise voiding money in the register would silently leave the
    project's outstanding balance understated.
    """
    row = _cf_owner_payment_for_entry(entry)
    if row is None:
        return None
    row.is_void = bool(make_void)
    if make_void:
        row.void_reason = ((reason or 'Cash flow entry voided'))[:250]
        row.voided_at = _pkt_now_naive()
    else:
        row.void_reason = None
        row.voided_at = None
    db.session.flush()
    return row


def amend_manual_cash_flow_entry(entry, *, direction=None, amount=None, account_id=None,
                                 destination_account_id=None, category_id=None, category_name=None,
                                 subcategory_id=None, subcategory_name=None, party_id=None,
                                 party_name=None, party_type=None, description=None, note=None,
                                 reference=None, date_posted=None, reason=None, actor=None,
                                 project_id=None, stage_id=None, create_missing=True, commit=True):
    """Correct an entry the immutable way: void + replace.

    The original row and its ledger posting are kept (flagged void with the
    reason) and a brand-new entry is posted.  Returns ``(new_entry, old_entry)``.
    """
    if entry is None:
        raise ValueError('Entry not found.')
    if entry.is_void:
        raise ValueError('A voided entry cannot be amended. Restore it first.')

    old_snapshot = _cf_snapshot(entry)
    reason_txt = (reason or '').strip() or 'Amended'

    # 1. Reverse the old posting.
    void_manual_cash_flow_entry(
        entry,
        reason=f'Amended — {reason_txt}',
        actor=actor,
        commit=False,
        allow_locked_period=False,
    )

    # 2. Post the corrected replacement.
    new_entry, _ = save_manual_cash_flow_entry(
        direction=(direction or entry.direction),
        amount=(amount if amount is not None else entry.amount),
        account_id=(account_id or entry.account_id),
        destination_account_id=(destination_account_id if destination_account_id is not None
                                else entry.destination_account_id),
        category_id=(category_id if category_id is not None else entry.category_id),
        category_name=category_name,
        subcategory_id=(subcategory_id if subcategory_id is not None else entry.subcategory_id),
        subcategory_name=subcategory_name,
        party_id=(party_id if party_id is not None else entry.party_id),
        party_name=(party_name if party_name is not None else entry.party_name),
        party_type=(party_type or entry.party_type),
        description=(description if description is not None else entry.description),
        note=(note if note is not None else entry.note),
        reference=(reference if reference is not None else entry.reference),
        date_posted=(date_posted or entry.date_posted),
        actor=actor,
        project_id=(project_id if project_id is not None else entry.project_id),
        stage_id=(stage_id if stage_id is not None else entry.stage_id),
        source_type=entry.source_type,
        create_missing=create_missing,
        commit=False,
        # The project / stage carried over from the original row is trusted
        # as-is; a deliberately re-picked one still goes through the normal
        # form path (and therefore through scope validation).
        validate_scope=(project_id is not None),
    )
    new_entry.amends_entry_id = int(entry.id)
    db.session.flush()

    # 3. Link both ends so the paper trail reads in either direction.
    entry.superseded_by_entry_id = int(new_entry.id)
    entry.void_reason = f'Amended by entry #{new_entry.id} — {reason_txt}'[:300]
    db.session.flush()

    _cf_write_audit(new_entry, 'Amended', before=old_snapshot, after=_cf_snapshot(new_entry),
                    reason=reason_txt, actor=actor)
    db.session.flush()
    if commit:
        db.session.commit()
    return new_entry, entry


def void_manual_cash_flow_entry(entry, reason=None, actor=None, commit=True,
                                allow_locked_period=False):
    """Void an entry and its ledger posting.  The rows are kept, never deleted."""
    if entry is None:
        raise ValueError('Entry not found.')
    if entry.is_void:
        raise ValueError('This entry is already voided.')

    reason_txt = (reason or '').strip() or None
    actor_name = _cf_actor_name(actor)
    if not allow_locked_period:
        assert_period_open(entry.account_id, entry.date_posted, operation='voided')

    before = _cf_snapshot(entry)
    tx = db.session.get(AccountTransaction, entry.account_tx_id) if entry.account_tx_id else None
    if tx is not None and not tx.is_void:
        tx.is_void = True
        tx.voided_at = _pkt_now_naive()
        tx.voided_by = actor_name
        tx.void_reason = (reason_txt or 'Cash flow entry voided')[:300]

    entry.is_void = True
    entry.voided_at = _pkt_now_naive()
    entry.voided_by = actor_name
    entry.void_reason = (reason_txt or 'Voided')[:300]
    entry.updated_by = actor_name
    entry.updated_at = _pkt_now_naive()
    entry.revision = int(entry.revision or 1) + 1
    db.session.flush()

    _cf_write_audit(entry, 'Voided', before=before, after=_cf_snapshot(entry),
                    reason=reason_txt, actor=actor)
    db.session.flush()

    # Keep the loan ledger in step: a voided entry must stop counting towards
    # "how much is still owed" (and say so, rather than disappearing silently).
    try:
        from hdc.services.loans import void_movement_for_entry
        void_movement_for_entry(entry, reason=reason_txt, actor=actor_name, commit=False)
    except ImportError:  # pragma: no cover - loans module always present
        pass

    # Same for the project side: a voided owner receipt must stop counting
    # towards what the project has been paid, or voiding the money would leave
    # the client's outstanding balance understated.
    _cf_sync_project_effect_void(entry, make_void=True, reason=reason_txt)

    if commit:
        db.session.commit()
    return entry


def restore_manual_cash_flow_entry(entry, actor=None, commit=True):
    """Bring a voided entry back (only while its financial day is open)."""
    if entry is None:
        raise ValueError('Entry not found.')
    if not entry.is_void:
        raise ValueError('This entry is not voided.')

    assert_period_open(entry.account_id, entry.date_posted, operation='restored')
    actor_name = _cf_actor_name(actor)
    before = _cf_snapshot(entry)

    tx = db.session.get(AccountTransaction, entry.account_tx_id) if entry.account_tx_id else None
    if tx is not None and tx.is_void:
        tx.is_void = False
        tx.voided_at = None
        tx.voided_by = None
        tx.void_reason = None

    entry.is_void = False
    entry.voided_at = None
    entry.voided_by = None
    entry.void_reason = None
    entry.updated_by = actor_name
    entry.updated_at = _pkt_now_naive()
    entry.revision = int(entry.revision or 1) + 1
    db.session.flush()

    _cf_write_audit(entry, 'Restored', before=before, after=_cf_snapshot(entry), actor=actor)
    db.session.flush()

    try:
        from hdc.services.loans import restore_movement_for_entry
        restore_movement_for_entry(entry, commit=False)
    except ImportError:  # pragma: no cover - loans module always present
        pass

    _cf_sync_project_effect_void(entry, make_void=False)

    if commit:
        db.session.commit()
    return entry


# ---------------------------------------------------------------------------
# register reads
# ---------------------------------------------------------------------------

def register_rows(*, date_from=None, date_to=None, account_id=None, direction=None,
                  category_id=None, party_name=None, project_id=None, search=None,
                  include_void=True, limit=500):
    """Query the register with the shared filter set.

    ``date_from`` / ``date_to`` are inclusive calendar dates; ``date_to`` is
    widened to the end of that day so a same-day entry is never cut off.
    """
    q = CashFlowEntry.query
    if not include_void:
        q = q.filter(CashFlowEntry.is_void == False)  # noqa: E712
    if date_from is not None:
        q = q.filter(CashFlowEntry.date_posted >= datetime.combine(date_from, datetime.min.time()))
    if date_to is not None:
        q = q.filter(CashFlowEntry.date_posted < datetime.combine(date_to, datetime.min.time()) + timedelta(days=1))
    if account_id:
        q = q.filter(or_(CashFlowEntry.account_id == int(account_id),
                         CashFlowEntry.destination_account_id == int(account_id)))
    if direction:
        q = q.filter(CashFlowEntry.direction == _cf_normalize_direction(direction))
    if category_id:
        q = q.filter(CashFlowEntry.category_id == int(category_id))
    if project_id:
        q = q.filter(CashFlowEntry.project_id == int(project_id))
    if party_name:
        like = f'%{party_name.strip()}%'
        q = q.filter(CashFlowEntry.party_name.ilike(like))
    if search:
        like = f'%{search.strip()}%'
        q = q.filter(or_(CashFlowEntry.description.ilike(like),
                         CashFlowEntry.note.ilike(like),
                         CashFlowEntry.reference.ilike(like),
                         CashFlowEntry.party_name.ilike(like)))
    q = q.order_by(CashFlowEntry.date_posted.desc(), CashFlowEntry.id.desc())
    if limit:
        q = q.limit(int(limit))
    return q.all()


def register_summary(rows):
    """Totals for a set of register rows (exact, in minor units)."""
    total_in = total_out = total_transfer = 0
    for r in rows or []:
        if r.is_void:
            continue
        minor = int(r.amount_minor if r.amount_minor is not None else to_minor(r.amount or 0))
        if r.direction == CF_DIR_IN:
            total_in += minor
        elif r.direction == CF_DIR_OUT:
            total_out += minor
        else:
            total_transfer += minor
    return {
        'total_in_minor': total_in,
        'total_out_minor': total_out,
        'total_transfer_minor': total_transfer,
        'net_minor': total_in - total_out,
        'total_in': float(from_minor(total_in)),
        'total_out': float(from_minor(total_out)),
        'total_transfer': float(from_minor(total_transfer)),
        'net': float(from_minor(total_in - total_out)),
        'count': len(rows or []),
        'void_count': sum(1 for r in (rows or []) if r.is_void),
    }


def register_row_dicts(rows):
    """Rows shaped for templates / CSV (amounts resolved from the minor mirror)."""
    out = []
    for r in rows or []:
        amount = float(from_minor(r.amount_minor)) if r.amount_minor is not None else float(r.amount or 0.0)
        out.append({
            # lets the list template tag the row for "entered by" lookup
            '_hdc_entity': 'hdc_cash_flow_entry',
            '_hdc_id': r.id,
            'id': r.id,
            'date': (r.date_posted.date() if r.date_posted else None),
            'date_posted': r.date_posted,
            'direction': r.direction,
            'direction_label': CF_DIRECTION_LABELS.get(r.direction, r.direction),
            'amount': amount,
            'amount_minor': r.amount_minor,
            'account': (r.account.name if r.account else ''),
            'account_id': r.account_id,
            'destination_account': (r.destination_account.name if r.destination_account else ''),
            'destination_account_id': r.destination_account_id,
            'category': (r.category.name if r.category else ''),
            'category_id': r.category_id,
            'subcategory': (r.subcategory.name if r.subcategory else ''),
            'party_name': (r.party_name or ''),
            'party_type': (r.party_type or ''),
            'description': (r.description or ''),
            'note': (r.note or ''),
            'reference': (r.reference or ''),
            'project': (r.project.name if r.project else ''),
            'project_id': r.project_id,
            'stage': (r.stage.name if r.stage else ''),
            'created_by': (r.created_by or ''),
            'is_void': bool(r.is_void),
            'void_reason': (r.void_reason or ''),
            'voided_by': (r.voided_by or ''),
            'amends_entry_id': r.amends_entry_id,
            'superseded_by_entry_id': r.superseded_by_entry_id,
            'revision': r.revision,
            'account_tx_id': r.account_tx_id,
        })
    return out


# ---------------------------------------------------------------------------
# vocabulary (categories / subcategories / parties)
# ---------------------------------------------------------------------------

def category_options(direction=None, active_only=True):
    q = CashFlowCategory.query
    if active_only:
        q = q.filter(CashFlowCategory.is_active == True)  # noqa: E712
    rows = q.order_by(CashFlowCategory.sort_order.asc(), CashFlowCategory.name.asc()).all()
    if direction:
        d = _cf_normalize_direction(direction)
        rows = [r for r in rows if (r.direction or 'both').strip().lower() in ('both', d)]
    return rows


def subcategory_options(category_id, active_only=True):
    q = CashFlowSubcategory.query.filter(CashFlowSubcategory.category_id == int(category_id))
    if active_only:
        q = q.filter(CashFlowSubcategory.is_active == True)  # noqa: E712
    return q.order_by(CashFlowSubcategory.name.asc()).all()


def party_options(active_only=True):
    q = CashFlowParty.query
    if active_only:
        q = q.filter(CashFlowParty.is_active == True)  # noqa: E712
    return q.order_by(CashFlowParty.name.asc()).all()


def category_field_rules(category):
    """The field rules a category carries, as a plain dict.

    This is *the* single answer to "which fields does this kind of transaction
    need?" — the entry form renders it as data attributes, the picker filters
    its party options with it and the engine enforces it on POST, so the three
    can never disagree.
    """
    if category is None:
        return {
            'party_mode': 'optional',
            'project_mode': 'optional',
            'party_types': (),
            'loan_effect': '',
            'project_effect': '',
        }
    effect = category.project_effect_value
    return {
        'party_mode': category.party_mode_value,
        # A project-receipt category *is* the project: the entry has no meaning
        # without one, so the rule is forced here rather than relying on every
        # database having been seeded with project_mode='required'.
        'project_mode': ('required' if effect == 'receipt' else category.project_mode_value),
        'party_types': tuple(category.allowed_party_types),
        'loan_effect': (category.loan_effect or '').strip().lower(),
        'project_effect': effect,
    }


def category_rules_map(active_only=True):
    """``{category_id: rules}`` for every category (used by the entry form)."""
    return {int(row.id): category_field_rules(row)
            for row in category_options(active_only=active_only)}


def party_type_allowed(rules, party_type):
    """Is ``party_type`` acceptable for a category carrying ``rules``?

    An empty ``party_types`` means "any".  ``other`` (the type a name typed by
    hand gets) is always acceptable: the restriction is guidance about who this
    kind of transaction is for, never a trap for a name that is not classified
    yet.
    """
    allowed = tuple((rules or {}).get('party_types') or ())
    if not allowed:
        return True
    ptype = (party_type or 'other').strip().lower() or 'other'
    return ptype in allowed or ptype == 'other'


def save_cf_party(name, party_type='other', phone=None, note=None):
    """Get-or-create a party by name (case-insensitive).  Returns ``(row, created)``.

    A generic ``other`` never overwrites a specific classification.  The entry
    form posts the name the user picked and only carries a type when the picker
    could supply one, so treating that default as an instruction would quietly
    re-file a known supplier as "other" every time a payment was recorded
    against them.  An explicit type still wins (that is how a party is
    reclassified), and ``other`` still applies to a party that has none.
    """
    nm = (name or '').strip()
    if not nm:
        raise ValueError('Party name is required.')
    ptype = (party_type or 'other').strip().lower() or 'other'
    row = CashFlowParty.query.filter(func.lower(func.trim(CashFlowParty.name)) == nm.lower()).first()
    if row:
        if not row.is_active:
            row.is_active = True
        current = (row.party_type or 'other').strip().lower() or 'other'
        if ptype and ptype != current and (ptype != 'other' or current == 'other'):
            row.party_type = ptype
        db.session.flush()
        return row, False
    row = CashFlowParty(name=nm[:160], party_type=ptype, phone=(phone or '').strip() or None,
                        note=(note or '').strip() or None, is_active=True)
    db.session.add(row)
    db.session.flush()
    return row, True


def save_cf_category(name, direction='both', notes=None, sort_order=0,
                     party_mode=None, project_mode=None, party_types=None,
                     loan_effect=None, project_effect=None):
    """Get-or-create a category by name (case-insensitive).

    ``party_mode`` / ``project_mode`` / ``party_types`` / ``loan_effect`` /
    ``project_effect`` are the field rules (see
    :class:`~hdc.models.cashflow.CashFlowCategory`).  They are applied only on
    create — an existing category keeps the rules the operator set in Settings,
    exactly like ``save_cf_party`` never downgrades a known supplier to
    ``other``.
    """
    nm = (name or '').strip()
    if not nm:
        raise ValueError('Category name is required.')
    d = _cf_normalize_direction(direction)
    if d not in CF_DIRECTIONS:
        d = 'both'
    row = CashFlowCategory.query.filter(func.lower(func.trim(CashFlowCategory.name)) == nm.lower()).first()
    if row:
        if not row.is_active:
            row.is_active = True
        db.session.flush()
        return row, False
    row = CashFlowCategory(name=nm[:120], direction=d, is_active=True,
                           sort_order=int(sort_order or 0), notes=(notes or '').strip() or None,
                           party_mode=_clean_field_mode(party_mode),
                           project_mode=_clean_field_mode(project_mode),
                           party_types=_clean_party_types(party_types),
                           loan_effect=_clean_loan_effect(loan_effect),
                           project_effect=_clean_project_effect(project_effect))
    db.session.add(row)
    db.session.flush()
    return row, True


def _clean_field_mode(value, default=None):
    """Normalise a field mode; ``None`` means "leave it unset".

    Unset is not the same as ``optional``: the shipped defaults are applied once
    to a database using this, and they must never overwrite a rule an operator
    chose in Settings.  The model reads NULL back as ``optional`` for the form.
    """
    mode = (value or '').strip().lower()
    if not mode:
        return default
    return mode if mode in ('none', 'optional', 'required') else default


def _clean_party_types(value):
    if isinstance(value, (list, tuple, set)):
        items = [str(v or '').strip().lower() for v in value]
    else:
        items = [chunk.strip().lower() for chunk in str(value or '').replace(';', ',').split(',')]
    seen = []
    for item in items:
        if item and item not in seen:
            seen.append(item)
    return ','.join(seen) or None


def _clean_loan_effect(value):
    effect = (value or '').strip().lower()
    return effect if effect in LOAN_EFFECTS else None


def _clean_project_effect(value):
    effect = (value or '').strip().lower()
    return effect if effect in PROJECT_EFFECTS else None


def update_cf_category(category, *, name=None, direction=None, notes=None, sort_order=None,
                       party_mode=None, project_mode=None, party_types=None, loan_effect=None,
                       project_effect=None, is_active=None, commit=True):
    """Edit an existing category and its field rules (Settings → Cash Flow).

    Only the keyword arguments actually passed are touched, so the settings
    form can post one panel at a time without clearing the others.
    """
    if category is None:
        raise ValueError('Category not found.')
    if name is not None:
        nm = (name or '').strip()
        if not nm:
            raise ValueError('Category name is required.')
        clash = (CashFlowCategory.query
                 .filter(func.lower(func.trim(CashFlowCategory.name)) == nm.lower(),
                         CashFlowCategory.id != int(category.id))
                 .first())
        if clash is not None:
            raise ValueError(f'A category named "{nm}" already exists.')
        category.name = nm[:120]
    if direction is not None:
        d = _cf_normalize_direction(direction)
        category.direction = d if d in CF_DIRECTIONS else 'both'
    if notes is not None:
        category.notes = (notes or '').strip() or None
    if sort_order is not None:
        try:
            category.sort_order = int(sort_order or 0)
        except (TypeError, ValueError):
            raise ValueError('Sort order must be a whole number.')
    if party_mode is not None:
        category.party_mode = _clean_field_mode(party_mode)
    if project_mode is not None:
        category.project_mode = _clean_field_mode(project_mode)
    if party_types is not None:
        category.party_types = _clean_party_types(party_types)
    if loan_effect is not None:
        category.loan_effect = _clean_loan_effect(loan_effect)
    if project_effect is not None:
        category.project_effect = _clean_project_effect(project_effect)
    if is_active is not None:
        category.is_active = bool(is_active)
    db.session.flush()
    if commit:
        db.session.commit()
    return category


def update_cf_party(party, *, name=None, party_type=None, phone=None, note=None,
                    is_active=None, commit=True):
    """Edit a register party (Settings → Cash Flow).  Only passed fields change."""
    if party is None:
        raise ValueError('Party not found.')
    if name is not None:
        nm = (name or '').strip()
        if not nm:
            raise ValueError('Party name is required.')
        clash = (CashFlowParty.query
                 .filter(func.lower(func.trim(CashFlowParty.name)) == nm.lower(),
                         CashFlowParty.id != int(party.id))
                 .first())
        if clash is not None:
            raise ValueError(f'A party named "{nm}" already exists.')
        party.name = nm[:160]
    if party_type is not None:
        ptype = (party_type or 'other').strip().lower() or 'other'
        if ptype not in PARTY_TYPE_VALUES:
            raise ValueError('Choose a valid party type.')
        party.party_type = ptype
    if phone is not None:
        party.phone = (phone or '').strip()[:40] or None
    if note is not None:
        party.note = (note or '').strip()[:300] or None
    if is_active is not None:
        party.is_active = bool(is_active)
    db.session.flush()
    if commit:
        db.session.commit()
    return party


def update_cf_subcategory(subcategory, *, name=None, is_active=None, commit=True):
    """Rename or (de)activate a subcategory (Settings → Cash Flow)."""
    if subcategory is None:
        raise ValueError('Subcategory not found.')
    if name is not None:
        nm = (name or '').strip()
        if not nm:
            raise ValueError('Subcategory name is required.')
        clash = (CashFlowSubcategory.query
                 .filter(CashFlowSubcategory.category_id == int(subcategory.category_id),
                         func.lower(func.trim(CashFlowSubcategory.name)) == nm.lower(),
                         CashFlowSubcategory.id != int(subcategory.id))
                 .first())
        if clash is not None:
            raise ValueError(f'A subcategory named "{nm}" already exists here.')
        subcategory.name = nm[:120]
    if is_active is not None:
        subcategory.is_active = bool(is_active)
    db.session.flush()
    if commit:
        db.session.commit()
    return subcategory


def save_cf_subcategory(category_id, name, notes=None):
    nm = (name or '').strip()
    if not nm:
        raise ValueError('Subcategory name is required.')
    if not category_id:
        raise ValueError('Select a parent category first.')
    row = CashFlowSubcategory.query.filter(
        CashFlowSubcategory.category_id == int(category_id),
        func.lower(func.trim(CashFlowSubcategory.name)) == nm.lower(),
    ).first()
    if row:
        if not row.is_active:
            row.is_active = True
        db.session.flush()
        return row, False
    row = CashFlowSubcategory(category_id=int(category_id), name=nm[:120], is_active=True,
                              notes=(notes or '').strip() or None)
    db.session.add(row)
    db.session.flush()
    return row, True


# ---------------------------------------------------------------------------
# daily reconciliation
# ---------------------------------------------------------------------------

def _money_accounts():
    return [a for a in Account.query.filter(Account.is_void == False).all()  # noqa: E712
            if _cf_is_money_account(a)]


def _tx_minor_expr(column=None):
    """SQL expression giving a transaction's amount in exact minor units.

    Reads must tolerate a row whose ``amount_minor`` mirror is still NULL —
    a database upgraded in place has historical rows written before the
    mirror existed, and the mirror is an optimisation for exact arithmetic,
    not a required field.  When it is absent the value is derived from the
    legacy float column in SQL, so reconciliation is correct on old and new
    data alike without needing a backfill pass.
    """
    from sqlalchemy import Integer, cast

    model = AccountTransaction
    col = column or model.amount_minor
    return func.coalesce(col, cast(func.round(model.amount * 100), Integer))


def _minor_opening_for(account, day):
    """Opening minor balance for ``account`` on ``day``.

    Uses the previous locked day's counted figure when available (that is the
    whole point of the day lock); otherwise falls back to the ledger-derived
    balance at midnight before ``day``.
    """
    prev_day = day - timedelta(days=1)
    prev_pos = (CashDayAccountPosition.query
                .filter(CashDayAccountPosition.account_id == int(account.id),
                        CashDayAccountPosition.position_date <= prev_day,
                        CashDayAccountPosition.is_locked == True)  # noqa: E712
                .order_by(CashDayAccountPosition.position_date.desc())
                .first())
    if prev_pos is not None:
        if prev_pos.counted_minor is not None:
            return int(prev_pos.counted_minor)
        if prev_pos.expected_closing_minor is not None:
            return int(prev_pos.expected_closing_minor)

    opening_minor = int(getattr(account, 'opening_balance_minor', None) or to_minor(account.opening_balance or 0))
    minor_expr = _tx_minor_expr()
    in_sum = db.session.query(func.coalesce(func.sum(minor_expr), 0)).filter(
        AccountTransaction.to_account_id == int(account.id),
        AccountTransaction.is_void == False,  # noqa: E712
        AccountTransaction.date < day,
    ).scalar() or 0
    out_sum = db.session.query(func.coalesce(func.sum(minor_expr), 0)).filter(
        AccountTransaction.from_account_id == int(account.id),
        AccountTransaction.is_void == False,  # noqa: E712
        AccountTransaction.date < day,
    ).scalar() or 0
    return opening_minor + int(in_sum) - int(out_sum)


def _day_movement(account, day):
    """(in, out, transfer_in, transfer_out) in minor units for one account/day."""
    money_ids = {int(a.id) for a in _money_accounts()}

    def _sum(field, extra=None):
        q = db.session.query(func.coalesce(func.sum(_tx_minor_expr()), 0)).filter(
            field == int(account.id),
            AccountTransaction.is_void == False,  # noqa: E712
            AccountTransaction.date == day,
        )
        if extra is not None:
            q = q.filter(extra)
        return int(q.scalar() or 0)

    amount_in = _sum(AccountTransaction.to_account_id,
                     AccountTransaction.type != 'transfer')
    amount_out = _sum(AccountTransaction.from_account_id,
                      AccountTransaction.type != 'transfer')
    t_in = _sum(AccountTransaction.to_account_id,
                AccountTransaction.type == 'transfer')
    t_out = _sum(AccountTransaction.from_account_id,
                 AccountTransaction.type == 'transfer')
    # A transfer between two treasury accounts is internal; when both ends are
    # money accounts the pair nets to zero for the group but still shows per
    # account, which is what the reconciliation grid needs.
    _ = money_ids
    return amount_in, amount_out, t_in, t_out


def day_positions(day, *, refresh=False):
    """Per-account positions for ``day`` (creates the rows on first view)."""
    positions = []
    for acc in _money_accounts():
        pos = (CashDayAccountPosition.query
               .filter(CashDayAccountPosition.position_date == day,
                       CashDayAccountPosition.account_id == int(acc.id))
               .first())
        opening = _minor_opening_for(acc, day)
        amount_in, amount_out, t_in, t_out = _day_movement(acc, day)
        expected = opening + amount_in + t_in - amount_out - t_out

        if pos is None:
            pos = CashDayAccountPosition(position_date=day, account_id=int(acc.id))
            db.session.add(pos)
        pos.account_name = acc.name
        pos.opening_minor = opening
        pos.amount_in_minor = amount_in
        pos.amount_out_minor = amount_out
        pos.transfer_in_minor = t_in
        pos.transfer_out_minor = t_out
        pos.expected_closing_minor = expected
        if pos.counted_minor is not None:
            pos.difference_minor = int(pos.counted_minor) - expected
        else:
            pos.difference_minor = None
        # keep the float mirrors readable for legacy reports
        for pair in (('opening', opening), ('amount_in', amount_in), ('amount_out', amount_out),
                     ('transfer_in', t_in), ('transfer_out', t_out), ('expected_closing', expected)):
            setattr(pos, pair[0], float(from_minor(pair[1])))
        if pos.counted_minor is not None:
            pos.counted = float(from_minor(pos.counted_minor))
            pos.difference = float(from_minor(pos.difference_minor or 0))
        positions.append(pos)
    db.session.flush()
    return positions


def day_totals(positions):
    """Group totals for the reconciliation grid."""
    def s(attr):
        return sum(int(getattr(p, attr) or 0) for p in (positions or []))
    return {
        'opening_minor': s('opening_minor'),
        'amount_in_minor': s('amount_in_minor'),
        'amount_out_minor': s('amount_out_minor'),
        'transfer_in_minor': s('transfer_in_minor'),
        'transfer_out_minor': s('transfer_out_minor'),
        'expected_closing_minor': s('expected_closing_minor'),
        'counted_minor': sum(int(p.counted_minor or 0) for p in (positions or [])
                             if p.counted_minor is not None),
        'difference_minor': sum(int(p.difference_minor or 0) for p in (positions or [])
                                if p.difference_minor is not None),
        'counted_accounts': sum(1 for p in (positions or []) if p.counted_minor is not None),
    }


def save_counted_position(day, account_id, counted, actor=None, commit=True):
    """Enter (or clear) the physically counted closing for one account/day."""
    if is_day_locked(day):
        raise ValueError(f'Financial day {day.isoformat()} is locked. Unlock it first.')
    acc = db.session.get(Account, int(account_id)) if account_id else None
    if not _cf_is_money_account(acc):
        raise ValueError('Select a valid active cash or bank account.')
    positions = day_positions(day)
    pos = next((p for p in positions if int(p.account_id) == int(acc.id)), None)
    if pos is None:
        raise ValueError('Unable to resolve the account position.')
    raw = (str(counted).strip() if counted is not None else '')
    if raw == '':
        pos.counted = None
        pos.counted_minor = None
        pos.difference = None
        pos.difference_minor = None
    else:
        minor = to_minor(raw)
        if minor < 0:
            raise ValueError('Counted amount cannot be negative.')
        pos.counted = float(from_minor(minor))
        pos.counted_minor = int(minor)
        pos.difference_minor = int(minor) - int(pos.expected_closing_minor or 0)
        pos.difference = float(from_minor(pos.difference_minor))
    pos.updated_by = _cf_actor_name(actor)
    pos.updated_at = _pkt_now_naive()
    db.session.flush()
    if commit:
        db.session.commit()
    return pos


def _is_dormant(pos):
    """True when an account had nothing to count: no balance and no movement."""
    return all(int(getattr(pos, attr) or 0) == 0 for attr in (
        'opening_minor', 'amount_in_minor', 'amount_out_minor',
        'transfer_in_minor', 'transfer_out_minor', 'expected_closing_minor',
    ))


def _day_close_difference_threshold():
    """PKR threshold above which a day-close variance needs a reason."""
    try:
        return float(current_app.config.get('HDC_DAY_CLOSE_DIFFERENCE_THRESHOLD', 5000) or 0)
    except Exception:
        return 5000.0


def lock_cash_day(day, actor=None, note=None, confirm_difference=False, commit=True):
    """Verify & lock a financial day; each counted closing carries forward.

    Every money account must have a counted figure.  Where the counted figure
    differs from the ledger-computed closing, an immutable
    :class:`AccountReconciliation` snapshot is written per account so the
    discrepancy is on the record rather than silently absorbed.

    A total variance larger than ``HDC_DAY_CLOSE_DIFFERENCE_THRESHOLD`` (5,000
    PKR by default) is not allowed to be locked until the caller both confirms
    it and writes a reason — the fix for audit 5.6, where a -492,000 PKR
    difference could be locked silently.
    """
    if is_day_locked(day):
        raise ValueError(f'Financial day {day.isoformat()} is already locked.')
    positions = day_positions(day)
    if not positions:
        raise ValueError('No cash or bank accounts to reconcile.')

    # A dormant account (no opening, no movement, nothing expected) has nothing
    # to count, so it is settled at zero instead of blocking the close.  Every
    # account that actually held or moved money still must be counted.
    for pos in positions:
        if pos.counted_minor is None and _is_dormant(pos):
            pos.counted_minor = 0
            pos.counted = 0.0
            pos.difference_minor = 0
            pos.difference = 0.0
    db.session.flush()

    missing = [p.account_name or f'#{p.account_id}' for p in positions if p.counted_minor is None]
    if missing:
        raise ValueError('Enter the counted closing for: ' + ', '.join(missing))

    actor_name = _cf_actor_name(actor)
    now = _pkt_now_naive()
    totals = day_totals(positions)

    # Large-variance gate (audit 5.6 / decision 15.2).  Checked on the totals
    # before anything is written, so a refused close leaves no partial state.
    threshold = _day_close_difference_threshold()
    variance = float(from_minor(int(totals.get('difference_minor') or 0)))
    if threshold > 0 and abs(variance) > threshold:
        if not confirm_difference:
            raise ValueError(
                f'Day difference is {variance:,.2f} PKR, which is more than the '
                f'{threshold:,.0f} PKR threshold. Confirm the difference before locking.'
            )
        if not (note or '').strip():
            raise ValueError(
                f'Day difference is {variance:,.2f} PKR (above the {threshold:,.0f} PKR '
                'threshold) — a written reason is required to lock this day.'
            )

    for pos in positions:
        pos.is_locked = True
        pos.locked_by = actor_name
        pos.locked_at = now
        expected = int(pos.expected_closing_minor or 0)
        counted = int(pos.counted_minor or 0)
        diff = counted - expected
        if diff != 0:
            rec = AccountReconciliation(
                account_id=int(pos.account_id),
                reconciliation_date=day,
                period_start_at=datetime.combine(day, datetime.min.time()),
                period_end_at=datetime.combine(day, datetime.max.time()),
                previous_balance=float(from_minor(int(pos.opening_minor or 0))),
                opening_balance=float(from_minor(int(pos.opening_minor or 0))),
                transaction_in=float(from_minor(int(pos.amount_in_minor or 0) + int(pos.transfer_in_minor or 0))),
                transaction_out=float(from_minor(int(pos.amount_out_minor or 0) + int(pos.transfer_out_minor or 0))),
                expected_balance=float(from_minor(expected)),
                actual_balance=float(from_minor(counted)),
                difference=float(from_minor(diff)),
                final_reconciled_balance=float(from_minor(counted)),
                previous_balance_minor=int(pos.opening_minor or 0),
                opening_balance_minor=int(pos.opening_minor or 0),
                transaction_in_minor=int(pos.amount_in_minor or 0) + int(pos.transfer_in_minor or 0),
                transaction_out_minor=int(pos.amount_out_minor or 0) + int(pos.transfer_out_minor or 0),
                expected_balance_minor=expected,
                actual_balance_minor=counted,
                difference_minor=diff,
                final_reconciled_balance_minor=counted,
                difference_type=('Matched' if diff == 0 else ('Excess' if diff > 0 else 'Loss')),
                status='Reconciled',
                note=(note or '').strip() or f'Day close {day.isoformat()}',
                created_by=actor_name,
                created_at=now,
            )
            db.session.add(rec)
            db.session.flush()

    lock = CashDayLock(
        lock_date=day,
        total_expected=float(from_minor(totals['expected_closing_minor'])),
        total_counted=float(from_minor(totals['counted_minor'])),
        difference=float(from_minor(totals['difference_minor'])),
        note=(note or '').strip() or None,
        locked_by=actor_name,
        locked_at=now,
        updated_at=now,
    )
    db.session.add(lock)
    db.session.flush()
    if commit:
        db.session.commit()
    return lock


def unlock_cash_day(day, commit=True):
    """Re-open a locked day (the reconciliation snapshots are kept)."""
    lock = day_lock_state(day)
    if lock is None:
        raise ValueError(f'Financial day {day.isoformat()} is not locked.')
    for pos in (CashDayAccountPosition.query
                .filter(CashDayAccountPosition.position_date == day).all()):
        pos.is_locked = False
        pos.locked_by = None
        pos.locked_at = None
    db.session.delete(lock)
    db.session.flush()
    if commit:
        db.session.commit()
    return True


def reconcile_account(account_id, day, counted=None, note=None, actor=None, commit=True):
    """Reconcile a single account on ``day`` and return the snapshot row."""
    acc = db.session.get(Account, int(account_id)) if account_id else None
    if not _cf_is_money_account(acc):
        raise ValueError('Select a valid active cash or bank account.')
    if counted is not None:
        save_counted_position(day, acc.id, counted, actor=actor, commit=False)
    positions = day_positions(day)
    pos = next((p for p in positions if int(p.account_id) == int(acc.id)), None)
    if pos is None:
        raise ValueError('Unable to resolve the account position.')
    expected = int(pos.expected_closing_minor or 0)
    counted_minor = int(pos.counted_minor if pos.counted_minor is not None else expected)
    diff = counted_minor - expected
    rec = AccountReconciliation(
        account_id=int(acc.id),
        reconciliation_date=day,
        previous_balance_minor=int(pos.opening_minor or 0),
        opening_balance_minor=int(pos.opening_minor or 0),
        transaction_in_minor=int(pos.amount_in_minor or 0) + int(pos.transfer_in_minor or 0),
        transaction_out_minor=int(pos.amount_out_minor or 0) + int(pos.transfer_out_minor or 0),
        expected_balance_minor=expected,
        actual_balance_minor=counted_minor,
        difference_minor=diff,
        final_reconciled_balance_minor=counted_minor,
        previous_balance=float(from_minor(int(pos.opening_minor or 0))),
        opening_balance=float(from_minor(int(pos.opening_minor or 0))),
        transaction_in=float(from_minor(int(pos.amount_in_minor or 0) + int(pos.transfer_in_minor or 0))),
        transaction_out=float(from_minor(int(pos.amount_out_minor or 0) + int(pos.transfer_out_minor or 0))),
        expected_balance=float(from_minor(expected)),
        actual_balance=float(from_minor(counted_minor)),
        difference=float(from_minor(diff)),
        final_reconciled_balance=float(from_minor(counted_minor)),
        difference_type=('Matched' if diff == 0 else ('Excess' if diff > 0 else 'Loss')),
        status='Reconciled',
        note=(note or '').strip() or None,
        created_by=_cf_actor_name(actor),
        created_at=_pkt_now_naive(),
    )
    db.session.add(rec)
    db.session.flush()
    if commit:
        db.session.commit()
    return rec


# ---------------------------------------------------------------------------
# seed data
# ---------------------------------------------------------------------------

_DEFAULT_CATEGORIES = [
    ('Owner / Client Receipt', 'in', 10),
    ('Scrap & Salvage Sale', 'in', 20),
    ('Loan Received', 'in', 30),
    ('Loan Recovery', 'in', 40),
    ('Other Income', 'in', 50),
    ('Material & Purchase', 'out', 60),
    ('Labour & Wages', 'out', 70),
    ('Subcontractor Payment', 'out', 80),
    ('Fuel & Transport', 'out', 90),
    ('Equipment & Machinery', 'out', 100),
    ('Office Expense', 'out', 110),
    ('Staff Salary', 'out', 120),
    ('Personal Expense', 'out', 130),
    ('Loan Given', 'out', 140),
    ('Loan Repayment', 'out', 150),
    ('Miscellaneous', 'out', 160),
]

#: Field rules for the seeded categories: ``name -> (party_mode, project_mode,
#: allowed party types, loan effect, project effect)``.  Only ever applied where
#: the column is still empty — an operator's own rule always wins.
_DEFAULT_CATEGORY_RULES = {
    # The four loan movements are the only categories that *require* a party:
    # "Loan Given" to nobody is not a loan.  Everything else keeps the app's
    # existing behaviour (party/project optional) — but the two fields are
    # hidden outright where they make no sense, which is what stops the form
    # asking a question that has no answer.  An operator can tighten any of
    # these to "required" in Settings → Cash Flow without a deploy.
    # The owner receipt is the one category whose *project* is mandatory: the
    # money is a project's client paying for that project, so the project is
    # the subject and the owner name is derived from it (project_effect below).
    'Owner / Client Receipt': ('optional', 'required', 'client', '', 'receipt'),
    'Scrap & Salvage Sale': ('none', 'none', '', '', ''),
    'Loan Received': ('required', 'none', 'lender', 'take', ''),
    'Loan Recovery': ('required', 'none', 'borrower', 'recover', ''),
    'Other Income': ('optional', 'none', '', '', ''),
    'Material & Purchase': ('optional', 'optional', 'supplier', '', ''),
    'Labour & Wages': ('optional', 'optional', 'worker', '', ''),
    'Subcontractor Payment': ('optional', 'optional', 'subcontractor', '', ''),
    'Fuel & Transport': ('optional', 'optional', '', '', ''),
    'Equipment & Machinery': ('optional', 'optional', '', '', ''),
    'Office Expense': ('none', 'none', '', '', ''),
    'Staff Salary': ('optional', 'none', 'staff', '', ''),
    'Personal Expense': ('none', 'none', '', '', ''),
    'Loan Given': ('required', 'none', 'borrower', 'give', ''),
    'Loan Repayment': ('required', 'none', 'lender', 'repay', ''),
    'Miscellaneous': ('optional', 'optional', '', '', ''),
}

_DEFAULT_SUBCATEGORIES = {
    'Material & Purchase': ['Cement', 'Steel / Saria', 'Sand / Crush', 'Bricks / Blocks',
                            'Plumbing', 'Electrical', 'Paint & Finish', 'Other Material'],
    'Labour & Wages': ['Mason', 'Labor', 'Carpenter', 'Steel Fixer', 'Electrician',
                       'Plumber', 'Painter', 'Tile Fixer'],
    'Fuel & Transport': ['Diesel / Petrol', 'Vehicle Rent', 'Freight / Cartage', 'Toll & Parking'],
    'Office Expense': ['Rent', 'Utilities', 'Stationery', 'Internet & Phone', 'Tea / Refreshment'],
    'Owner / Client Receipt': ['Project Payment', 'Advance Received', 'Final Settlement'],
    'Loan Received': ['Bank Loan', 'Personal Loan', 'Director Loan'],
    'Loan Given': ['Personal Loan', 'Business Loan'],
    'Loan Repayment': ['Principal', 'Principal + Interest'],
    'Loan Recovery': ['Principal', 'Principal + Interest'],
}


def ensure_cashflow_seed_data(commit=True):
    """Seed the register's default category / subcategory vocabulary.

    Idempotent: existing names are never duplicated or renamed.  Only runs the
    (slightly more expensive) subcategory pass when the tables are empty, so
    startup cost stays negligible.  The field-rule pass is separate
    (:func:`ensure_category_field_rules`) because it also has to reach a
    database whose categories already exist.
    """
    created = 0
    try:
        if CashFlowCategory.query.first() is not None:
            ensure_category_field_rules(commit=False)
            if commit:
                db.session.commit()
            return 0
        for name, direction, order in _DEFAULT_CATEGORIES:
            row, is_new = save_cf_category(name, direction=direction, sort_order=order)
            created += 1 if is_new else 0
            for sub_name in _DEFAULT_SUBCATEGORIES.get(name, []):
                save_cf_subcategory(row.id, sub_name)
        ensure_category_field_rules(commit=False)
        if commit:
            db.session.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
        return 0
    return created


def ensure_category_field_rules(commit=True):
    """Apply the default field rules to the shipped categories, once.

    A category that already has any rule (party/project mode, allowed types or a
    loan effect) is left exactly as it is — the operator's settings are the
    source of truth and a deploy must never quietly reset them.
    """
    touched = 0
    try:
        for row in CashFlowCategory.query.all():
            (party_mode, project_mode, party_types,
             loan_effect, project_effect) = _DEFAULT_CATEGORY_RULES.get(
                (row.name or '').strip(), (None, None, None, None, None))
            if (party_mode is None and project_mode is None and party_types is None
                    and loan_effect is None and project_effect is None):
                continue
            dirty = False
            if party_mode is not None and (row.party_mode or '').strip() == '':
                row.party_mode = party_mode
                dirty = True
            if project_mode is not None and (row.project_mode or '').strip() == '':
                row.project_mode = project_mode
                dirty = True
            if party_types is not None and not (row.party_types or '').strip():
                row.party_types = party_types
                dirty = True
            if loan_effect is not None and not (row.loan_effect or '').strip() and loan_effect:
                row.loan_effect = loan_effect
                dirty = True
            # project_effect is behaviour, not preference: an existing database
            # whose owner-receipt category predates this column must still get
            # it, or its receipts keep bypassing the project ledger.
            if project_effect and not (row.project_effect or '').strip():
                row.project_effect = project_effect
                dirty = True
            if dirty:
                touched += 1
        if commit:
            db.session.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
        return 0
    return touched


def _ensure_cashflow_seed_data():
    """Bootstrap alias (kept separate so the public name stays descriptive)."""
    return ensure_cashflow_seed_data()


def backfill_project_receipt_owner_payments(commit=True, dry_run=False):
    """Create the missing ``OwnerPayment`` for owner receipts posted before the fix.

    Every register entry on a ``project_effect='receipt'`` category that carries
    a project but has no mirrored payment row gets one, so projects recorded
    through the New Transaction form stop reading as unpaid.

    Safe to re-run (each entry is matched by ``source_entry_id``) and safe
    alongside the other two entry surfaces: rows written by the Projects page /
    Accounts quick-post have ``source_entry_id IS NULL`` and their own
    ``owner_payment`` ledger source, so they are never touched and nothing is
    double-counted.  Voided entries are mirrored as voided rows, which keeps the
    two sides consistent rather than quietly resurrecting cancelled money.

    Returns ``{'scanned', 'created', 'skipped'}``; ``dry_run`` reports without
    writing.
    """
    from hdc.models.accounts import OwnerPayment

    receipt_ids = [int(c.id) for c in CashFlowCategory.query
                   .filter(func.lower(func.coalesce(CashFlowCategory.project_effect, '')) == 'receipt')
                   .all()]
    stats = {'scanned': 0, 'created': 0, 'skipped': 0}
    if not receipt_ids:
        return stats

    entries = (CashFlowEntry.query
               .filter(CashFlowEntry.category_id.in_(receipt_ids),
                       CashFlowEntry.project_id.isnot(None))
               .order_by(CashFlowEntry.id.asc())
               .all())
    linked = {int(r.source_entry_id) for r in OwnerPayment.query
              .filter(OwnerPayment.source_entry_id.isnot(None)).all()}

    for entry in entries:
        stats['scanned'] += 1
        if int(entry.id) in linked:
            stats['skipped'] += 1
            continue
        if dry_run:
            stats['created'] += 1
            continue
        row = OwnerPayment(
            project_id=int(entry.project_id),
            amount=float(entry.amount or 0.0),
            date=(entry.date_posted.date() if entry.date_posted else _pkt_today()),
            received_to_account_id=(int(entry.account_id) if entry.account_id else None),
            remarks=((entry.description or '').strip()
                     or f'Cash flow entry #{entry.id}')[:200],
            activity_at=_pkt_now_naive(),
            is_void=bool(entry.is_void),
            void_reason=((entry.void_reason or 'Cash flow entry voided')[:250]
                         if entry.is_void else None),
            voided_at=(entry.voided_at if entry.is_void else None),
            source_entry_id=int(entry.id),
        )
        db.session.add(row)
        stats['created'] += 1

    if stats['created'] and not dry_run:
        db.session.flush()
        if commit:
            db.session.commit()
    return stats


def backfill_transaction_minor_units(batch_commit=True, limit=None):
    """Fill ``AccountTransaction.amount_minor`` for rows that predate the mirror.

    Safe to re-run: rows that already carry a minor value are skipped.
    """
    q = AccountTransaction.query.filter(AccountTransaction.amount_minor.is_(None))
    if limit:
        q = q.limit(int(limit))
    rows = q.all()
    for row in rows:
        sync_money_fields(row, 'amount', 'amount_minor')
    if rows and batch_commit:
        db.session.commit()
    return len(rows)
