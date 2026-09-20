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
        if sub is not None and int(sub.category_id or 0) != int(category.id):
            sub = None
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


def _cf_resolve_counterparty_account(party_name):
    """Resolve (or lazily create) the ledger account standing behind a party.

    HDC's ledger is fully double-entry — every row needs both a ``from`` and a
    ``to`` account — so an external counterparty still needs an account to
    balance against.  Named parties get their own ``person`` account (the same
    convention the rest of Accounts uses); an unnamed receipt settles against
    the shared ``Credit/Debit Control`` account.
    """
    from hdc.services.accounts import _accounts_default_external_parties, _accounts_party_account
    nm = (party_name or '').strip()
    if nm:
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
                                source_type=SRC_MANUAL, source_id=None, commit=True):
    """Post a new register entry **and** its ledger transaction atomically.

    Returns ``(entry, created)``.  When ``idempotency_key`` matches an existing
    entry the existing row is returned with ``created=False`` — a retried or
    double-clicked form cannot double-post.
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

    cat = None
    sub = None
    if direction != CF_DIR_TRANSFER:
        cat = _cf_resolve_category(direction, category_id, category_name,
                                   required=True, create_if_missing=create_missing)
        sub = _cf_resolve_subcategory(cat, subcategory_id, subcategory_name,
                                      create_if_missing=create_missing)

    ptype = (party_type or 'other').strip().lower() or 'other'
    party = _cf_find_party(party_id=party_id, name=party_name, party_type=ptype)
    if party is None and (party_name or '').strip():
        party, _ = save_cf_party(party_name, ptype)
    if party is not None:
        party_name = party.name
        ptype = party.party_type or ptype

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
        counterparty = _cf_resolve_counterparty_account(party_name)
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
    if commit:
        db.session.commit()
    return entry, True


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


def save_cf_party(name, party_type='other', phone=None, note=None):
    """Get-or-create a party by name (case-insensitive).  Returns ``(row, created)``."""
    nm = (name or '').strip()
    if not nm:
        raise ValueError('Party name is required.')
    ptype = (party_type or 'other').strip().lower() or 'other'
    row = CashFlowParty.query.filter(func.lower(func.trim(CashFlowParty.name)) == nm.lower()).first()
    if row:
        if not row.is_active:
            row.is_active = True
        if ptype and row.party_type != ptype:
            row.party_type = ptype
        db.session.flush()
        return row, False
    row = CashFlowParty(name=nm[:160], party_type=ptype, phone=(phone or '').strip() or None,
                        note=(note or '').strip() or None, is_active=True)
    db.session.add(row)
    db.session.flush()
    return row, True


def save_cf_category(name, direction='both', notes=None, sort_order=0):
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
                           sort_order=int(sort_order or 0), notes=(notes or '').strip() or None)
    db.session.add(row)
    db.session.flush()
    return row, True


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


def lock_cash_day(day, actor=None, note=None, commit=True):
    """Verify & lock a financial day; each counted closing carries forward.

    Every money account must have a counted figure.  Where the counted figure
    differs from the ledger-computed closing, an immutable
    :class:`AccountReconciliation` snapshot is written per account so the
    discrepancy is on the record rather than silently absorbed.
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
    ('Other Income', 'in', 40),
    ('Material & Purchase', 'out', 50),
    ('Labour & Wages', 'out', 60),
    ('Subcontractor Payment', 'out', 70),
    ('Fuel & Transport', 'out', 80),
    ('Equipment & Machinery', 'out', 90),
    ('Office Expense', 'out', 100),
    ('Staff Salary', 'out', 110),
    ('Personal Expense', 'out', 120),
    ('Miscellaneous', 'out', 130),
]

_DEFAULT_SUBCATEGORIES = {
    'Material & Purchase': ['Cement', 'Steel / Saria', 'Sand / Crush', 'Bricks / Blocks',
                            'Plumbing', 'Electrical', 'Paint & Finish', 'Other Material'],
    'Labour & Wages': ['Mason', 'Labor', 'Carpenter', 'Steel Fixer', 'Electrician',
                       'Plumber', 'Painter', 'Tile Fixer'],
    'Fuel & Transport': ['Diesel / Petrol', 'Vehicle Rent', 'Freight / Cartage', 'Toll & Parking'],
    'Office Expense': ['Rent', 'Utilities', 'Stationery', 'Internet & Phone', 'Tea / Refreshment'],
    'Owner / Client Receipt': ['Project Payment', 'Advance Received', 'Final Settlement'],
}


def ensure_cashflow_seed_data(commit=True):
    """Seed the register's default category / subcategory vocabulary.

    Idempotent: existing names are never duplicated or renamed.  Only runs the
    (slightly more expensive) subcategory pass when the tables are empty, so
    startup cost stays negligible.
    """
    created = 0
    try:
        if CashFlowCategory.query.first() is not None:
            return 0
        for name, direction, order in _DEFAULT_CATEGORIES:
            row, is_new = save_cf_category(name, direction=direction, sort_order=order)
            created += 1 if is_new else 0
            for sub_name in _DEFAULT_SUBCATEGORIES.get(name, []):
                save_cf_subcategory(row.id, sub_name)
        if commit:
            db.session.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
        return 0
    return created


def _ensure_cashflow_seed_data():
    """Bootstrap alias (kept separate so the public name stays descriptive)."""
    return ensure_cashflow_seed_data()


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
