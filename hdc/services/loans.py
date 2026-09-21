"""HDC services.loans — the loan ledger: who owes whom, and how much is left.

Every loan here is a *contract* with a counterparty plus the money movements
that belong to it.  The money itself is posted by the register engine
(``hdc.services.cashflow_register.save_manual_cash_flow_entry``) — one entry,
one double-entry ledger posting, full audit and day-lock protection — and this
module records the *meaning* of that posting (principal, interest, which loan,
which kind of movement).  Nothing posts money twice, and no figure is stored:
outstanding is always derived from the movements (see ``hdc.models.loans``).

The four loan movements and where they come from:

===============  ==================  ==========================================
category effect  money direction     meaning
===============  ==================  ==========================================
``take``         in                  we took a loan — the lender gave us money
``give``         out                 we gave a loan — the borrower took money
``repay``        out                 we pay a lender back (principal/interest)
``recover``      in                  a borrower pays us back (principal/interest)
===============  ==================  ==========================================

Both entry surfaces feed this module:

* the Cash Flow register / New Transaction form, because its category carries
  ``loan_effect`` (the engine calls :func:`apply_cash_flow_entry`), and
* Accounts → All Entries, where the four loan transaction types post an ordinary
  ledger row and then call :func:`attach_ledger_transaction`.

Layer: ``services`` — may import models, utils and other services.
"""

from __future__ import annotations

from datetime import date as _date

from sqlalchemy import func, or_

from hdc.extensions import db
from hdc.models.accounts import Account, AccountTransaction
from hdc.models.cashflow import CashFlowEntry, CashFlowParty
from hdc.models.loans import (
    LOAN_DIRECTIONS,
    LOAN_DIRECTION_LABELS,
    LOAN_MOVEMENT_LABELS,
    Loan,
    LoanMovement,
)
from hdc.services.cashflow_register import (
    LOAN_EFFECT_PARTY_TYPE,
    LOAN_EFFECTS,
    category_field_rules,
    save_cf_category,
    save_cf_party,
    save_manual_cash_flow_entry,
)
from hdc.utils.dates import _pkt_now_naive, _pkt_today
from hdc.utils.money import from_minor, to_minor

__all__ = [
    "EFFECT_LOAN_DIRECTION",
    "LOAN_SOURCE_REGISTER",
    "LOAN_SOURCE_ENTRIES",
    "add_movement",
    "apply_cash_flow_entry",
    "attach_ledger_transaction",
    "close_loan",
    "create_loan",
    "find_open_loan",
    "loan_detail",
    "loan_rows",
    "loan_summary",
    "reopen_loan",
    "restore_movement_for_entry",
    "void_movement",
    "void_movement_for_entry",
    "write_off",
]

#: ``loan_effect`` -> the direction of the loan it belongs to.
EFFECT_LOAN_DIRECTION = {
    'take': 'received',
    'give': 'given',
    'repay': 'received',
    'recover': 'given',
}

#: ``loan_effect`` -> the movement kind it produces.
EFFECT_MOVEMENT_KIND = {
    'take': 'disbursement',
    'give': 'disbursement',
    'repay': 'repayment',
    'recover': 'recovery',
}

#: The register category each effect books against (created on demand).
EFFECT_CATEGORY = {
    'take': ('Loan Received', 'in'),
    'give': ('Loan Given', 'out'),
    'repay': ('Loan Repayment', 'out'),
    'recover': ('Loan Recovery', 'in'),
}

LOAN_SOURCE_REGISTER = 'cash_flow_register'
LOAN_SOURCE_ENTRIES = 'accounts_entries'
LOAN_SOURCE_MANUAL = 'manual'


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _actor_name(actor):
    if actor is None:
        return 'system'
    if isinstance(actor, str):
        return actor.strip() or 'system'
    return (str(getattr(actor, 'username', '') or '').strip() or 'system')


def _money(value, field='Amount'):
    """Normalise a submitted money value to a float with exact paisa."""
    return float(from_minor(to_minor(value, field=field)))


def _parse_day(value, fallback=None):
    if value is None or value == '':
        return fallback
    if isinstance(value, _date):
        return value
    from hdc.utils.format import _parse_date
    parsed = _parse_date(str(value).strip(), fallback=None)
    return parsed or fallback


def _next_loan_code():
    """``LN-0001``-style code, unique without a sequence table."""
    rows = db.session.query(Loan.loan_code).filter(Loan.loan_code.isnot(None)).all()
    highest = 0
    for (code,) in rows:
        digits = ''.join(ch for ch in str(code or '') if ch.isdigit())
        if digits:
            highest = max(highest, int(digits))
    return 'LN-%04d' % (highest + 1)


def ensure_loan_categories(commit=False):
    """Get-or-create the four loan categories and return ``{effect: category}``.

    The categories are what give the register form its loan vocabulary, so they
    are created with the rules that make the form behave: a party is mandatory
    (a loan to nobody is not a loan), restricted to loan parties, and no project
    is asked for — loans are not project costs.
    """
    out = {}
    for effect in LOAN_EFFECTS:
        name, direction = EFFECT_CATEGORY[effect]
        party_type = LOAN_EFFECT_PARTY_TYPE[effect]
        row, _created = save_cf_category(
            name,
            direction=direction,
            party_mode='required',
            project_mode='none',
            party_types=party_type,
            loan_effect=effect,
        )
        # An existing category (seeded, or created by hand) gets the loan rules
        # filled in where it has none — never overwritten where it has some.
        if not (row.loan_effect or '').strip():
            row.loan_effect = effect
        if not (row.party_types or '').strip():
            row.party_types = party_type
        if row.party_mode_value != 'required':
            row.party_mode = 'required'
        if row.project_mode_value == 'optional':
            row.project_mode = 'none'
        out[effect] = row
    db.session.flush()
    if commit:
        db.session.commit()
    return out


def _ensure_loan_party(name, wanted_type):
    """Resolve a party for a loan, classifying it as a loan party when unset."""
    nm = (name or '').strip()
    if not nm:
        raise ValueError('Party name is required.')
    row = (CashFlowParty.query
           .filter(func.lower(func.trim(CashFlowParty.name)) == nm.lower())
           .first())
    if row is None:
        row, _created = save_cf_party(nm, party_type=wanted_type)
        return row
    if not row.is_active:
        row.is_active = True
    current = (row.party_type or 'other').strip().lower() or 'other'
    if current == 'other' and wanted_type != 'other':
        row.party_type = wanted_type
    db.session.flush()
    return row


def find_open_loan(party_name, direction):
    """The open loan for ``party_name`` in ``direction``, newest first."""
    nm = (party_name or '').strip()
    if not nm:
        return None
    direction = (direction or '').strip().lower()
    q = (Loan.query
         .filter(Loan.status == 'open',
                 func.lower(func.trim(Loan.party_name)) == nm.lower()))
    if direction in LOAN_DIRECTIONS:
        q = q.filter(func.lower(func.trim(Loan.direction)) == direction)
    return q.order_by(Loan.id.desc()).first()


def _movement_for_entry(entry):
    if entry is None or not getattr(entry, 'id', None):
        return None
    return (LoanMovement.query
            .filter(LoanMovement.cash_flow_entry_id == int(entry.id))
            .order_by(LoanMovement.id.desc())
            .first())


def _movement_for_txn(txn):
    if txn is None or not getattr(txn, 'id', None):
        return None
    return (LoanMovement.query
            .filter(LoanMovement.account_txn_id == int(txn.id))
            .order_by(LoanMovement.id.desc())
            .first())


# ---------------------------------------------------------------------------
# creating and feeding loans
# ---------------------------------------------------------------------------

def create_loan(*, direction, party_name, principal, account_id, start_date=None,
                due_date=None, party_type=None, phone=None, interest_rate=None,
                purpose=None, note=None, reference=None, actor=None, commit=True):
    """Open a loan and post its opening movement.

    Returns ``(loan, entry)``.  ``direction`` is ``received`` (we took the money)
    or ``given`` (we handed it out).  The money is posted by the register engine
    using the matching loan category, so this is the *only* place a loan starts
    — the entry and the loan ledger can never disagree.
    """
    direction = (direction or '').strip().lower()
    if direction not in LOAN_DIRECTIONS:
        raise ValueError('Choose whether this is a loan taken or a loan given.')
    nm = (party_name or '').strip()
    if not nm:
        raise ValueError('Party name is required.')
    amount = _money(principal, 'Principal')
    if amount <= 0:
        raise ValueError('Principal must be greater than zero.')
    account = db.session.get(Account, int(account_id)) if account_id else None
    if account is None:
        raise ValueError('Choose the cash or bank account the money moved through.')

    day = _parse_day(start_date, fallback=_pkt_today())
    due = _parse_day(due_date, fallback=None)
    if due is not None and due < day:
        raise ValueError('The due date cannot be before the loan date.')
    rate = 0.0
    if interest_rate not in (None, ''):
        try:
            rate = float(str(interest_rate).replace('%', '').strip() or 0)
        except (TypeError, ValueError):
            raise ValueError('Interest rate must be a number (percent per year).')
        if rate < 0 or rate > 100:
            raise ValueError('Interest rate must be between 0 and 100 percent.')

    wanted_type = party_type or LOAN_EFFECT_PARTY_TYPE['take' if direction == 'received' else 'give']
    party = _ensure_loan_party(nm, wanted_type)

    loan = Loan(
        loan_code=_next_loan_code(),
        direction=direction,
        party_id=int(party.id),
        party_name=party.name,
        party_type=(party.party_type or wanted_type),
        phone=(phone or party.phone or '').strip() or None,
        principal=amount,
        interest_rate=rate,
        purpose=(purpose or '').strip()[:200] or None,
        start_date=day,
        due_date=due,
        account_id=int(account.id),
        status='open',
        note=(note or '').strip()[:500] or None,
        created_by=_actor_name(actor),
    )
    from hdc.utils.money import sync_money_fields
    sync_money_fields(loan, 'principal', 'principal_minor')
    db.session.add(loan)
    db.session.flush()

    effect = 'take' if direction == 'received' else 'give'
    entry = _post_loan_entry(
        loan, effect, amount,
        principal_amount=amount, interest_amount=0.0,
        account_id=int(account.id), day=day, reference=reference, note=note, actor=actor,
    )
    if commit:
        db.session.commit()
    return loan, entry


def add_movement(loan, *, kind, amount, principal_amount=None, interest_amount=None,
                 account_id=None, day=None, reference=None, note=None, actor=None,
                 commit=True):
    """Record one money movement against ``loan`` and post it.

    ``kind`` is ``disbursement`` (more principal handed over), ``repayment``
    (we pay a loan we took) or ``recovery`` (a borrower pays us).  For the last
    two, ``principal_amount`` / ``interest_amount`` split the payment; when they
    are omitted the whole amount counts against principal.
    """
    if loan is None:
        raise ValueError('Loan not found.')
    if loan.status == 'void':
        raise ValueError('This loan is voided — restore it before movements.')
    kind = (kind or '').strip().lower()
    if kind not in ('disbursement', 'repayment', 'recovery'):
        raise ValueError('Choose a valid loan movement.')
    total = _money(amount, 'Amount')
    if total <= 0:
        raise ValueError('Amount must be greater than zero.')

    if kind == 'disbursement':
        principal_part, interest_part = total, 0.0
    else:
        interest_part = _money(interest_amount or 0, 'Interest')
        if interest_part < 0:
            raise ValueError('Interest cannot be negative.')
        if principal_amount in (None, ''):
            principal_part = total - interest_part
        else:
            principal_part = _money(principal_amount, 'Principal')
        if principal_part < 0:
            raise ValueError('Principal cannot be negative.')
        if abs((principal_part + interest_part) - total) > 0.005 + 1e-9:
            raise ValueError('Principal + interest must add up to the amount paid.')

    if kind in ('repayment', 'recovery') and principal_part > 0:
        if principal_part > float(from_minor(loan.outstanding_minor)) + 1e-9:
            raise ValueError(
                'That is more than the outstanding principal '
                f'({float(from_minor(loan.outstanding_minor)):,.2f} PKR). '
                'Enter the principal and interest separately.')

    effect = {'disbursement': 'take' if loan.is_received else 'give',
              'repayment': 'repay',
              'recovery': 'recover'}[kind]
    day = _parse_day(day, fallback=_pkt_today())
    account_id = account_id or loan.account_id
    entry = _post_loan_entry(
        loan, effect, total,
        principal_amount=principal_part, interest_amount=interest_part,
        account_id=account_id, day=day, reference=reference, note=note, actor=actor,
    )
    loan.updated_at = _pkt_now_naive()
    if commit:
        db.session.commit()
    return entry


def _post_loan_entry(loan, effect, amount, *, principal_amount, interest_amount,
                     account_id, day, reference=None, note=None, actor=None):
    """Post the register entry for a loan movement and mirror it into the loan."""
    categories = ensure_loan_categories()
    category = categories.get(effect)
    direction = 'in' if effect in ('take', 'recover') else 'out'
    party_type = LOAN_EFFECT_PARTY_TYPE.get(effect) or loan.party_type or 'other'
    description = {
        'take': f'Loan taken — {loan.party_name}',
        'give': f'Loan given — {loan.party_name}',
        'repay': f'Loan repayment — {loan.party_name}',
        'recover': f'Loan recovery — {loan.party_name}',
    }[effect]

    entry, created = save_manual_cash_flow_entry(
        direction=direction,
        amount=float(from_minor(to_minor(amount))),
        account_id=int(account_id),
        category_id=(int(category.id) if category is not None else None),
        party_name=loan.party_name,
        party_type=party_type,
        description=description,
        note=note or None,
        reference=reference or None,
        date_posted=_pkt_now_naive().replace(year=day.year, month=day.month, day=day.day),
        actor=actor,
        source_type='LOAN',
        source_id=int(loan.id),
        commit=False,
    )
    movement = _movement_for_entry(entry)
    if movement is None:  # defensive: the engine always mirrors a loan category
        movement = LoanMovement(loan_id=int(loan.id), kind=EFFECT_MOVEMENT_KIND[effect],
                                cash_flow_entry_id=int(entry.id))
        db.session.add(movement)
        movement.date = day
    movement.principal_amount = float(principal_amount or 0.0)
    movement.interest_amount = float(interest_amount or 0.0)
    from hdc.utils.money import sync_money_fields
    sync_money_fields(movement, 'amount', 'amount_minor')
    sync_money_fields(movement, 'principal_amount', 'principal_amount_minor')
    sync_money_fields(movement, 'interest_amount', 'interest_amount_minor')
    db.session.flush()
    _refresh_loan_status(loan)
    return entry


def apply_cash_flow_entry(entry, effect, actor=None):
    """Mirror a register entry with a loan category into the loan ledger.

    Called by the register engine right after a loan-category entry is posted.
    Idempotent: a movement already pointing at the entry is returned untouched.
    For ``repay`` / ``recover`` an open loan must exist — posting a repayment
    against nobody would only move money without settling anything.
    """
    if entry is None:
        return None
    effect = (effect or '').strip().lower()
    if effect not in LOAN_EFFECTS:
        return None
    existing = _movement_for_entry(entry)
    if existing is not None:
        return existing

    direction = EFFECT_LOAN_DIRECTION[effect]
    kind = EFFECT_MOVEMENT_KIND[effect]
    amount = float(from_minor(entry.amount_minor
                              if entry.amount_minor is not None
                              else to_minor(entry.amount or 0)))
    day = entry.entry_date if hasattr(entry, 'entry_date') else None
    actor_name = _actor_name(actor) if actor is not None else (entry.created_by or 'system')

    loan = None
    if (entry.source_type or '').strip().upper() == 'LOAN' and entry.source_id:
        loan = db.session.get(Loan, int(entry.source_id))
        if loan is not None and loan.status == 'void':
            raise ValueError('This loan is voided — restore it before posting more money.')
    if loan is None:
        loan = find_open_loan(entry.party_name, direction)
    if loan is None:
        if effect not in ('take', 'give'):
            label = 'Loan Taken' if effect == 'repay' else 'Loan Given'
            raise ValueError(
                f'No open loan for "{(entry.party_name or "").strip() or "this party"}" — '
                f'record it as a {label} first (Accounts → Loans), then book the '
                f'{"repayment" if effect == "repay" else "recovery"}.')
        loan = Loan(
            loan_code=_next_loan_code(),
            direction=direction,
            party_id=(int(entry.party_id) if entry.party_id else None),
            party_name=(entry.party_name or '').strip() or 'Unnamed party',
            party_type=(entry.party_type or LOAN_EFFECT_PARTY_TYPE.get(effect) or 'other'),
            principal=amount,
            account_id=(int(entry.account_id) if entry.account_id else None),
            start_date=day,
            status='open',
            note='Created from the Cash Flow register.',
            created_by=actor_name,
        )
        from hdc.utils.money import sync_money_fields
        sync_money_fields(loan, 'principal', 'principal_minor')
        db.session.add(loan)
        db.session.flush()

    movement = LoanMovement(
        loan_id=int(loan.id),
        kind=kind,
        date=day,
        account_id=(int(entry.account_id) if entry.account_id else None),
        cash_flow_entry_id=int(entry.id),
        account_txn_id=(int(entry.account_tx_id) if entry.account_tx_id else None),
        source_type=LOAN_SOURCE_REGISTER,
        reference=(entry.reference or '').strip() or None,
        note=(entry.note or '').strip() or None,
        is_void=False,
        created_by=actor_name,
    )
    movement.amount = amount
    movement.principal_amount = amount
    movement.interest_amount = 0.0
    from hdc.utils.money import sync_money_fields
    sync_money_fields(movement, 'amount', 'amount_minor')
    sync_money_fields(movement, 'principal_amount', 'principal_amount_minor')
    sync_money_fields(movement, 'interest_amount', 'interest_amount_minor')
    db.session.add(movement)
    db.session.flush()
    _refresh_loan_status(loan)
    return movement


def attach_ledger_transaction(txn, effect, *, principal_amount=None, interest_amount=None,
                              loan=None, actor=None, commit=True):
    """Attach an *already posted* ledger row (Accounts → All Entries) to a loan.

    The entries form books one ordinary ``hdc_account_txn`` for loan types; this
    records its meaning.  Idempotent per ledger row.
    """
    if txn is None:
        raise ValueError('Transaction not found.')
    effect = (effect or '').strip().lower()
    if effect not in LOAN_EFFECTS:
        raise ValueError('Unknown loan type.')
    existing = _movement_for_txn(txn)
    if existing is not None:
        return existing

    direction = EFFECT_LOAN_DIRECTION[effect]
    party_name = (txn.party_name or '').strip()
    if not party_name:
        raise ValueError('A loan needs the person’s name — fill the Party field.')
    day = txn.date or _pkt_today()
    total = float(from_minor(txn.amount_minor if txn.amount_minor is not None
                             else to_minor(txn.amount or 0)))
    actor_name = _actor_name(actor)

    if loan is None:
        loan = find_open_loan(party_name, direction)
    if loan is None:
        if effect in ('repay', 'recover'):
            label = 'Loan Taken' if effect == 'repay' else 'Loan Given'
            raise ValueError(
                f'No open loan for "{party_name}" — record it as a {label} first, '
                f'then book the {"repayment" if effect == "repay" else "recovery"}.')
        party = _ensure_loan_party(party_name, LOAN_EFFECT_PARTY_TYPE.get(effect) or 'other')
        loan = Loan(
            loan_code=_next_loan_code(),
            direction=direction,
            party_id=int(party.id),
            party_name=party.name,
            party_type=(party.party_type or LOAN_EFFECT_PARTY_TYPE.get(effect) or 'other'),
            principal=total,
            account_id=(int(txn.from_account_id or txn.to_account_id or 0) or None),
            start_date=day,
            status='open',
            note='Created from Accounts → All Entries.',
            created_by=actor_name,
        )
        from hdc.utils.money import sync_money_fields
        sync_money_fields(loan, 'principal', 'principal_minor')
        db.session.add(loan)
        db.session.flush()

    kind = EFFECT_MOVEMENT_KIND[effect]
    principal_part = total
    interest_part = 0.0
    if kind in ('repayment', 'recovery'):
        interest_part = _money(interest_amount or 0, 'Interest')
        principal_part = total - interest_part if principal_amount in (None, '') \
            else _money(principal_amount, 'Principal')
        if principal_part < 0 or interest_part < 0 or abs((principal_part + interest_part) - total) > 0.005 + 1e-9:
            raise ValueError('Principal + interest must add up to the amount paid.')

    movement = LoanMovement(
        loan_id=int(loan.id),
        kind=kind,
        date=day,
        account_id=(int(txn.from_account_id or txn.to_account_id or 0) or None),
        account_txn_id=int(txn.id),
        source_type=LOAN_SOURCE_ENTRIES,
        reference=(txn.reference_id or '').strip() or None,
        note=(txn.note or '').strip() or None,
        is_void=bool(txn.is_void),
        created_by=actor_name,
    )
    movement.amount = total
    movement.principal_amount = float(principal_part or 0.0)
    movement.interest_amount = float(interest_part or 0.0)
    from hdc.utils.money import sync_money_fields
    sync_money_fields(movement, 'amount', 'amount_minor')
    sync_money_fields(movement, 'principal_amount', 'principal_amount_minor')
    sync_money_fields(movement, 'interest_amount', 'interest_amount_minor')
    db.session.add(movement)
    db.session.flush()
    _refresh_loan_status(loan)
    if commit:
        db.session.commit()
    return movement


# ---------------------------------------------------------------------------
# void / restore / write-off
# ---------------------------------------------------------------------------

def void_movement(movement, *, reason=None, actor=None, commit=True, void_posting=True):
    """Void a loan movement (and, by default, the posting that carried it)."""
    if movement is None:
        raise ValueError('Movement not found.')
    if movement.is_void:
        raise ValueError('This movement is already voided.')
    actor_name = _actor_name(actor)
    loan = movement.loan

    if void_posting and movement.cash_flow_entry_id:
        entry = db.session.get(CashFlowEntry, int(movement.cash_flow_entry_id))
        if entry is not None and not entry.is_void:
            from hdc.services.cashflow_register import void_manual_cash_flow_entry
            void_manual_cash_flow_entry(entry, reason=reason or 'Loan movement voided',
                                        actor=actor_name, commit=False)
            return movement
    if void_posting and movement.account_txn_id:
        txn = db.session.get(AccountTransaction, int(movement.account_txn_id))
        if txn is not None and not txn.is_void:
            txn.is_void = True
            txn.voided_at = _pkt_now_naive()
            txn.voided_by = actor_name
            txn.void_reason = (reason or 'Loan movement voided')[:300]

    movement.is_void = True
    movement.voided_at = _pkt_now_naive()
    movement.voided_by = actor_name
    movement.void_reason = (reason or 'Voided')[:300]
    db.session.flush()
    _refresh_loan_status(loan)
    if commit:
        db.session.commit()
    return movement


def void_movement_for_entry(entry, *, reason=None, actor=None, commit=False):
    """Void the loan movement behind a register entry (called by the engine)."""
    movement = _movement_for_entry(entry)
    if movement is None or movement.is_void:
        return movement
    movement.is_void = True
    movement.voided_at = _pkt_now_naive()
    movement.voided_by = _actor_name(actor)
    movement.void_reason = (reason or 'Entry voided')[:300]
    db.session.flush()
    _refresh_loan_status(movement.loan)
    if commit:
        db.session.commit()
    return movement


def restore_movement_for_entry(entry, *, commit=False):
    """Un-void the loan movement behind a restored register entry."""
    movement = _movement_for_entry(entry)
    if movement is None or not movement.is_void:
        return movement
    movement.is_void = False
    movement.voided_at = None
    movement.voided_by = None
    movement.void_reason = None
    db.session.flush()
    _refresh_loan_status(movement.loan)
    if commit:
        db.session.commit()
    return movement


def write_off(loan, *, amount=None, reason=None, actor=None, commit=True):
    """Write off (part of) the outstanding principal without moving money."""
    if loan is None:
        raise ValueError('Loan not found.')
    outstanding = float(from_minor(loan.outstanding_minor))
    if outstanding <= 0:
        raise ValueError('This loan has nothing outstanding to write off.')
    value = _money(amount if amount not in (None, '') else outstanding, 'Write-off')
    if value <= 0:
        raise ValueError('Write-off amount must be greater than zero.')
    if value > outstanding + 1e-9:
        raise ValueError(f'You can write off at most {outstanding:,.2f} PKR.')

    movement = LoanMovement(
        loan_id=int(loan.id),
        kind='writeoff',
        date=_pkt_today(),
        amount=value,
        principal_amount=value,
        interest_amount=0.0,
        source_type=LOAN_SOURCE_MANUAL,
        note=(reason or 'Written off')[:500],
        is_void=False,
        created_by=_actor_name(actor),
    )
    from hdc.utils.money import sync_money_fields
    sync_money_fields(movement, 'amount', 'amount_minor')
    sync_money_fields(movement, 'principal_amount', 'principal_amount_minor')
    sync_money_fields(movement, 'interest_amount', 'interest_amount_minor')
    db.session.add(movement)
    db.session.flush()
    _refresh_loan_status(loan)
    if commit:
        db.session.commit()
    return movement


def _refresh_loan_status(loan):
    """Close a loan whose principal is fully settled; reopen one that is not."""
    if loan is None or loan.status == 'void':
        return loan
    if loan.outstanding_minor <= 0 and loan.status == 'open':
        loan.status = 'closed'
        loan.closed_at = _pkt_now_naive()
        loan.close_reason = loan.close_reason or 'Fully settled'
    elif loan.outstanding_minor > 0 and loan.status == 'closed':
        loan.status = 'open'
        loan.closed_at = None
        loan.close_reason = None
    return loan


def close_loan(loan, *, reason=None, actor=None, commit=True):
    """Close a loan by hand (e.g. settled outside the books)."""
    if loan is None:
        raise ValueError('Loan not found.')
    if loan.status == 'closed':
        raise ValueError('This loan is already closed.')
    if loan.outstanding_minor > 0:
        raise ValueError(
            'There is still principal outstanding — record the final payment or '
            'write the balance off before closing.')
    loan.status = 'closed'
    loan.closed_at = _pkt_now_naive()
    loan.closed_by = _actor_name(actor)
    loan.close_reason = (reason or '').strip()[:300] or 'Closed'
    db.session.flush()
    if commit:
        db.session.commit()
    return loan


def reopen_loan(loan, *, actor=None, commit=True):
    """Reopen a closed loan."""
    if loan is None:
        raise ValueError('Loan not found.')
    loan.status = 'open'
    loan.closed_at = None
    loan.closed_by = None
    loan.close_reason = None
    db.session.flush()
    if commit:
        db.session.commit()
    return loan


# ---------------------------------------------------------------------------
# read models for the page
# ---------------------------------------------------------------------------

def _loan_query(search=None, direction=None, status=None, party_name=None):
    q = Loan.query
    search = (search or '').strip()
    if search:
        like = f'%{search.lower()}%'
        q = q.filter(or_(
            func.lower(func.coalesce(Loan.party_name, '')).like(like),
            func.lower(func.coalesce(Loan.loan_code, '')).like(like),
            func.lower(func.coalesce(Loan.purpose, '')).like(like),
            func.lower(func.coalesce(Loan.note, '')).like(like),
        ))
    direction = (direction or '').strip().lower()
    if direction in LOAN_DIRECTIONS:
        q = q.filter(func.lower(func.coalesce(Loan.direction, '')) == direction)
    status = (status or '').strip().lower()
    if status in ('open', 'closed', 'void'):
        q = q.filter(func.lower(func.coalesce(Loan.status, 'open')) == status)
    elif status != 'all':
        q = q.filter(func.lower(func.coalesce(Loan.status, 'open')) == 'open')
    if party_name:
        q = q.filter(Loan.party_name == party_name)
    return q


def loan_rows(search=None, direction=None, status='open', today=None):
    """Loan rows (dicts) for the loans page, each with its derived figures."""
    today = today or _pkt_today()
    rows = []
    for loan in _loan_query(search=search, direction=direction, status=status) \
            .order_by(Loan.status.asc(), Loan.due_date.is_(None), Loan.due_date.asc(),
                      Loan.id.desc()).all():
        rows.append(loan_row(loan, today=today))
    return rows


def loan_row(loan, today=None):
    today = today or _pkt_today()
    days = loan.days_to_due(today)
    return {
        'id': int(loan.id),
        'code': loan.loan_code or f'LN-{loan.id:04d}',
        'direction': loan.direction,
        'direction_label': loan.direction_label,
        'party_name': loan.party_name,
        'party_type': loan.party_type,
        'party_account_id': loan.party_account_id,
        'principal': float(from_minor(loan._principal_minor())),
        'disbursed': float(from_minor(loan.disbursed_minor)),
        'settled': float(from_minor(loan.settled_minor)),
        'interest_paid': float(from_minor(loan.interest_minor)),
        'outstanding': float(from_minor(loan.outstanding_minor)),
        'settled_percent': loan.settled_percent,
        'start_date': loan.start_date,
        'due_date': loan.due_date,
        'days_to_due': days,
        'overdue': loan.is_overdue(today),
        'status': loan.status,
        'interest_rate': float(loan.interest_rate or 0.0),
        'purpose': loan.purpose,
        'note': loan.note,
        'movements': [movement_row(m) for m in loan.movements],
    }


def movement_row(movement):
    return {
        'id': int(movement.id),
        'kind': movement.kind,
        'kind_label': movement.kind_label,
        'date': movement.date,
        'amount': float(from_minor(movement.amount_minor_value)),
        'principal': float(from_minor(movement.principal_minor_value)),
        'interest': float(from_minor(movement.interest_minor_value)),
        'cash_direction': movement.cash_direction,
        'account_txn_id': movement.account_txn_id,
        'cash_flow_entry_id': movement.cash_flow_entry_id,
        'reference': movement.reference,
        'note': movement.note,
        'is_void': bool(movement.is_void),
        'void_reason': movement.void_reason,
        'created_by': movement.created_by,
    }


def loan_detail(loan, today=None):
    """Everything the loan page needs for one loan."""
    if loan is None:
        return None
    data = loan_row(loan, today=today)
    data['created_by'] = loan.created_by
    data['created_at'] = loan.created_at
    data['phone'] = loan.phone
    data['account_id'] = loan.account_id
    data['close_reason'] = loan.close_reason
    data['closed_at'] = loan.closed_at
    data['can_close'] = bool(loan.outstanding_minor <= 0 and loan.status == 'open')
    return data


def loan_summary(today=None):
    """Totals for the loans page header (open loans only)."""
    today = today or _pkt_today()
    rows = loan_rows(status='open', today=today)
    given = [r for r in rows if r['direction'] == 'given']
    received = [r for r in rows if r['direction'] == 'received']
    overdue = [r for r in rows if r['overdue']]
    due_soon = [r for r in rows if (r['days_to_due'] is not None and 0 <= r['days_to_due'] <= 7)]
    return {
        'receivable': sum(r['outstanding'] for r in given),
        'payable': sum(r['outstanding'] for r in received),
        'net': sum(r['outstanding'] for r in given) - sum(r['outstanding'] for r in received),
        'open_count': len(rows),
        'given_count': len(given),
        'received_count': len(received),
        'overdue_count': len(overdue),
        'overdue_amount': sum(r['outstanding'] for r in overdue),
        'due_soon_count': len(due_soon),
        'closed_count': len(loan_rows(status='closed', today=today)),
    }
