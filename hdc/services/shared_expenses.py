"""HDC services.shared_expenses — the Shared Expenses brain.

Where the module's pages call one function each, and every rule that matters
lives here:

**Money stays in Accounts.**  This service never writes a ledger row of its own.
When the operator asks the module to pay for a shared expense it calls
``save_manual_cash_flow_entry`` — the very same engine the Cash Flow register and
the New Transaction form use — and keeps the resulting document id.  When the
expense was already paid in Accounts, it only *links* it.  Either way the whole
payment exists exactly once, in ``hdc_account_txn``.

**Splits are exact.**  All arithmetic runs on integer paisa (``*_minor``).  An
equal split hands the odd paisa out one by one instead of rounding every slice,
so 5,000 across 3 gives 1,666.67 + 1,666.67 + 1,666.66 — the slices always re-add
to the total, and the module can prove it.

**Balances are derived, never stored.**  ``party_balances`` / ``ledger_lines``
read the shares, the payments and the settlements every time, so a cleared
expense, a voided expense and a settled party can never disagree with the pages.

Sign convention used everywhere in this module::

    balance = shares - paid - settled_out + settled_in

    positive  ->  the party still owes that much
    negative  ->  the party has paid more than its share and is owed that much
"""

from __future__ import annotations

from datetime import date as _date

from sqlalchemy import func, or_

from hdc.extensions import db
from hdc.models.accounts import Account, AccountTransaction
from hdc.models.cashflow import CashFlowCategory, CashFlowEntry
from hdc.models.shared_expenses import (SHARED_PARTY_KINDS, SHARED_SPLIT_MODES,
                                        SharedExpense, SharedExpenseShare,
                                        SharedParty, SharedSettlement)
from hdc.utils.dates import _pkt_now_naive, _pkt_today
from hdc.utils.format import _parse_date, _payload_int
from hdc.utils.money import from_minor, sync_money_fields, to_minor

#: what the module calls a shared expense when nothing was typed
DEFAULT_ACCOUNTS_CATEGORY = 'Shared Expenses'

#: the heads the business named, seeded once so the module is usable on day one
DEFAULT_PARTIES = (
    ('FBM', 'FBM', 'business'),
    ('HDC', 'HDC', 'business'),
    ('Home', 'HOME', 'home'),
)

#: quick-pick heads for the free-text "what was it for" box
CATEGORY_PRESETS = (
    'Car Fuel', 'Vehicle Maintenance', 'Vehicle Repairs', 'Toll & Parking',
    'Utilities', 'Electricity Bill', 'Gas Bill', 'Water Bill', 'Internet',
    'Groceries', 'Home Rent', 'Office Rent', 'Office Supplies', 'Stationery',
    'Staff Salary', 'Food & Refreshment', 'Medical', 'Repairs & Maintenance',
    'Security', 'Misc',
)

MONEY_SOURCES = ('post', 'link', 'none')
MONEY_SOURCE_LABELS = {
    'post': 'Pay from an account now (posts to Accounts)',
    'link': 'Already paid — link the Accounts entry',
    'none': 'Allocation only — no Accounts entry yet',
}


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _actor_name(actor) -> str:
    if actor is None:
        return ''
    getter = getattr(actor, 'username', None)
    if getter:
        return str(getter).strip()[:80]
    return str(actor).strip()[:80]


def _norm(value) -> str:
    return (value or '').strip()


def _money_minor(value, field='Amount'):
    """Parse operator money input into integer paisa (raises ``ValueError``)."""
    from hdc.utils.money import MoneyValueError
    try:
        return to_minor(value, field=field)
    except MoneyValueError as exc:
        raise ValueError(str(exc)) from exc


def money_value(minor) -> float:
    """Integer paisa -> float for the templates/CSV (``1666.67``)."""
    return float(from_minor(int(minor or 0)))


def money_text(minor) -> str:
    return f'{from_minor(int(minor or 0)):,.2f}'


def split_mode_label(mode) -> str:
    return {
        'equal': 'Equal split',
        'custom': 'Custom amounts',
        'percent': 'Percentages',
    }.get((mode or '').strip().lower(), 'Equal split')


def party_kind_label(kind) -> str:
    return {
        'business': 'Business',
        'home': 'Home',
        'person': 'Person',
        'other': 'Other',
    }.get((kind or '').strip().lower(), 'Business')


def balance_state(balance_minor) -> dict:
    """Read a derived balance: who owes whom, in words, with a colour."""
    minor = int(balance_minor or 0)
    if abs(minor) < 1:
        return {'state': 'settled', 'label': 'Settled', 'tone': 'slate',
                'text': 'Settled', 'amount': 0.0}
    if minor > 0:
        return {'state': 'owes', 'label': 'Owes', 'tone': 'rose',
                'text': f'Owes {money_text(minor)}', 'amount': money_value(minor)}
    return {'state': 'owed', 'label': 'To receive', 'tone': 'emerald',
            'text': f'Gets back {money_text(-minor)}', 'amount': money_value(-minor)}


# ---------------------------------------------------------------------------
# seed + parties
# ---------------------------------------------------------------------------

def ensure_shared_expense_seed_data():
    """Create the FBM / HDC / Home heads once, on an empty module.

    Called from bootstrap (never from a page render): the module has to be
    usable the first time it is opened, and the three heads the business always
    splits between should not have to be typed in by hand.  Runs only when the
    party list is completely empty, so it can never resurrect a head the
    operator deleted.
    """
    try:
        if db.session.query(SharedParty.id).first() is not None:
            return 0
        created = 0
        for index, (name, code, kind) in enumerate(DEFAULT_PARTIES):
            db.session.add(SharedParty(
                name=name, short_code=code, kind=kind, is_default=True,
                sort_order=index * 10, status='active',
                note='Seeded sharing head — edit or deactivate any time.',
                created_by='system',
            ))
            created += 1
        db.session.commit()
        return created
    except Exception:
        db.session.rollback()
        return 0


def party_options(*, active_only=False, include_inactive=None):
    """All sharing heads, defaults first, then by sort order and name."""
    if include_inactive is not None:          # back-compat spelling
        active_only = not include_inactive
    q = SharedParty.query
    if active_only:
        q = q.filter(func.lower(func.coalesce(SharedParty.status, 'active')) == 'active')
    return q.order_by(SharedParty.is_default.desc(),
                      SharedParty.sort_order.asc(),
                      SharedParty.name.asc()).all()


def parties_by_id(*, active_only=False):
    return {int(p.id): p for p in party_options(active_only=active_only)}


def default_party_ids():
    return [int(p.id) for p in party_options(active_only=True) if p.is_default]


def find_party_by_name(name):
    nm = _norm(name)
    if not nm:
        return None
    return (SharedParty.query
            .filter(func.lower(SharedParty.name) == nm.lower())
            .first())


def save_party(name, *, kind='business', short_code=None, account_id=None,
               is_default=True, sort_order=None, note=None, actor=None, commit=True,
               create_account=False):
    """Create one sharing head.

    With ``create_account`` the head also gets its own ``person`` account in the
    accounts ledger (found or created), which is what makes settling up a real
    transfer instead of a note.  Head names are unique case-insensitively: two
    spellings of "Home" would silently split one ledger in two.
    """
    nm = _norm(name)
    if not nm:
        raise ValueError('Enter the sharing party name.')
    if len(nm) > 80:
        raise ValueError('Party name is too long (80 characters max).')
    if find_party_by_name(nm) is not None:
        raise ValueError(f'A sharing party named "{nm}" already exists.')
    knd = _norm(kind).lower() or 'business'
    if knd not in SHARED_PARTY_KINDS:
        raise ValueError('Choose a valid party type.')

    account_id = int(account_id) if account_id else None
    if create_account and not account_id:
        acc = _accounts_account_for_party(nm)
        account_id = int(acc.id) if acc is not None else None
    if account_id:
        account = db.session.get(Account, int(account_id))
        if account is None:
            raise ValueError('That linked account no longer exists.')
        account_id = int(account.id)

    if sort_order in (None, ''):
        current_max = db.session.query(func.max(SharedParty.sort_order)).scalar() or 0
        sort_order = int(current_max) + 10

    party = SharedParty(
        name=nm,
        short_code=_norm(short_code)[:20] or None,
        kind=knd,
        account_id=account_id,
        is_default=bool(is_default),
        sort_order=int(sort_order or 0),
        status='active',
        note=_norm(note)[:250] or None,
        created_by=_actor_name(actor) or None,
    )
    db.session.add(party)
    if commit:
        db.session.commit()
    else:
        db.session.flush()
    return party


def update_party(party, *, name=None, kind=None, short_code=None, account_id='__keep__',
                 is_default=None, sort_order=None, note=None, status=None):
    """Edit one sharing head (rename, re-type, link an account, re-order)."""
    if name is not None:
        nm = _norm(name)
        if not nm:
            raise ValueError('Enter the sharing party name.')
        clash = find_party_by_name(nm)
        if clash is not None and int(clash.id) != int(party.id):
            raise ValueError(f'A sharing party named "{nm}" already exists.')
        party.name = nm[:80]
    if kind is not None:
        knd = _norm(kind).lower() or party.kind or 'business'
        if knd not in SHARED_PARTY_KINDS:
            raise ValueError('Choose a valid party type.')
        party.kind = knd
    if short_code is not None:
        party.short_code = _norm(short_code)[:20] or None
    if account_id != '__keep__':
        if account_id in (None, '', 0):
            party.account_id = None
        else:
            account = db.session.get(Account, int(account_id))
            if account is None:
                raise ValueError('That linked account no longer exists.')
            party.account_id = int(account.id)
    if is_default is not None:
        party.is_default = bool(is_default)
    if sort_order not in (None, ''):
        party.sort_order = int(sort_order)
    if note is not None:
        party.note = _norm(note)[:250] or None
    if status is not None:
        st = _norm(status).lower()
        if st not in ('active', 'inactive'):
            raise ValueError('Party status must be active or inactive.')
        party.status = st
    return party


def _accounts_account_for_party(name):
    """Find or create the accounts-ledger account standing behind a head."""
    try:
        from hdc.services.accounts import _accounts_party_account
        return _accounts_party_account(name, 'person')
    except Exception:
        return None


def party_usage(party_id):
    """Counts that decide whether a head may be deleted or only deactivated."""
    pid = int(party_id)
    return {
        'shares': int(db.session.query(func.count(SharedExpenseShare.id))
                      .filter(SharedExpenseShare.party_id == pid).scalar() or 0),
        'as_payer': int(db.session.query(func.count(SharedExpense.id))
                        .filter(SharedExpense.payer_party_id == pid).scalar() or 0),
        'settlements': int(db.session.query(func.count(SharedSettlement.id))
                           .filter(or_(SharedSettlement.from_party_id == pid,
                                       SharedSettlement.to_party_id == pid)).scalar() or 0),
    }


def party_has_history(party_id) -> bool:
    return any(party_usage(party_id).values())


def delete_party(party):
    """Delete a head that has never been used.  History is never deleted."""
    usage = party_usage(party.id)
    if any(usage.values()):
        raise ValueError(
            f'"{party.name}" already appears on {usage["shares"]} share(s) and '
            f'{usage["settlements"]} settlement(s) — deactivate it instead of '
            'deleting, so the old ledgers keep their name.')
    db.session.delete(party)


# ---------------------------------------------------------------------------
# the split engine (integer paisa, always re-adds to the total)
# ---------------------------------------------------------------------------

def compute_split(mode, total_minor, participants):
    """Divide ``total_minor`` between ``participants``.  Returns a list of dicts.

    ``participants`` is an ordered list of ``{'party_id': int, 'amount': ...,
    'percent': ...}``.  Every answer is integer paisa and the list always sums to
    ``total_minor`` exactly:

    * ``equal``   — base share each, then the odd paisa handed out one per party
      from the top, in the order the operator ticked them;
    * ``percent`` — each party's percentage of the total (half-up on paisa), with
      the rounding drift given to / taken from the largest share so 33.33 + 33.33
      + 33.34 still lands on the rupee;
    * ``custom``  — the figures as typed, refused when they do not add up.

    Raises ``ValueError`` with a message written for the operator.
    """
    rows = list(participants or [])
    total_minor = int(total_minor or 0)
    mode = _norm(mode).lower() or 'equal'
    if mode not in SHARED_SPLIT_MODES:
        raise ValueError('Choose Equal, Custom amounts or Percentages.')
    if not rows:
        raise ValueError('Tick at least one sharing party.')
    if total_minor < 0:
        raise ValueError('Total amount cannot be negative.')
    seen = set()
    for row in rows:
        pid = int(row.get('party_id') or 0)
        if not pid:
            raise ValueError('A split row is missing its party.')
        if pid in seen:
            raise ValueError('The same party is listed twice in the split.')
        seen.add(pid)

    if mode == 'equal':
        count = len(rows)
        base, remainder = divmod(total_minor, count)
        out = []
        for index, row in enumerate(rows):
            share = base + (1 if index < remainder else 0)
            out.append({'party_id': int(row['party_id']), 'amount_minor': share,
                        'percent_bp': _bp_of(share, total_minor)})
        return out

    if mode == 'percent':
        bp_values = []
        for row in rows:
            raw = row.get('percent')
            if raw in (None, ''):
                raise ValueError('Give every party a percentage.')
            try:
                # basis points keep "33.33%" exact; 2 decimals of a percent
                bp = to_minor(raw, field='Percentage')
            except ValueError as exc:
                raise ValueError(f'Percentage is not a number ({raw}).') from exc
            if bp < 0:
                raise ValueError('Percentages cannot be negative.')
            bp_values.append(bp)
        if sum(bp_values) != 10000:
            got = from_minor(sum(bp_values))
            raise ValueError(f'Percentages must add up to 100% (they add up to {got}%).')
        out = []
        for row, bp in zip(rows, bp_values):
            amount = (total_minor * bp + 5000) // 10000   # round half up, in paisa
            out.append({'party_id': int(row['party_id']), 'amount_minor': int(amount),
                        'percent_bp': int(bp)})
        _absorb_rounding(out, total_minor)
        return out

    # custom
    out = []
    for row in rows:
        raw = row.get('amount')
        if raw in (None, ''):
            raise ValueError('Give every party an amount.')
        try:
            amount = to_minor(raw, field='Share amount')
        except ValueError as exc:
            raise ValueError(f'Share amount is not a number ({raw}).') from exc
        if amount < 0:
            raise ValueError('Share amounts cannot be negative.')
        out.append({'party_id': int(row['party_id']), 'amount_minor': int(amount),
                    'percent_bp': _bp_of(amount, total_minor)})
    diff = total_minor - sum(r['amount_minor'] for r in out)
    if diff:
        raise ValueError(
            f'The shares add up to {money_text(sum(r["amount_minor"] for r in out))} '
            f'but the total is {money_text(total_minor)} — '
            f'{"add" if diff > 0 else "remove"} {money_text(abs(diff))}.')
    return out


def _bp_of(amount_minor, total_minor):
    """A share's percentage of the total, in basis points (NULL when no total)."""
    if not total_minor:
        return None
    return int(round(int(amount_minor) * 10000 / int(total_minor)))


def _absorb_rounding(rows, total_minor):
    """Push a percent split's rounding drift into its largest share."""
    if not rows:
        return
    diff = int(total_minor) - sum(r['amount_minor'] for r in rows)
    if not diff:
        return
    target = max(rows, key=lambda r: (r['amount_minor'], -r['party_id']))
    target['amount_minor'] = int(target['amount_minor']) + diff


def equal_split_preview(total_minor, party_ids):
    """What an equal split would look like right now (used by the page script)."""
    return compute_split('equal', total_minor,
                         [{'party_id': pid} for pid in (party_ids or [])])


# ---------------------------------------------------------------------------
# shared expenses
# ---------------------------------------------------------------------------

def expense_filters(args):
    """Read the shared GET filter set (date range, party, category, search)."""
    date_from = _parse_date(_norm(args.get('date_from')), fallback=None)
    date_to = _parse_date(_norm(args.get('date_to')), fallback=None)
    party_id = args.get('party_id', type=int)
    category = _norm(args.get('category'))
    status = _norm(args.get('status')).lower()
    if status not in ('active', 'void', 'all'):
        status = 'active'
    return {
        'date_from': date_from,
        'date_to': date_to,
        'party_id': (int(party_id) if party_id else None),
        'category': category,
        'status': status,
        'search': _norm(args.get('q')),
        'money': _norm(args.get('money')).lower(),      # linked | unlinked | ''
    }


def filter_query_string(flt, *, exclude=()):
    """The live filters as a query string (for ``url_for(..., **filter_qs)``).

    ``exclude`` drops keys another link overrides (the party chips set their own
    ``party_id``), so a template never has to merge dictionaries.
    """
    q = {
        'date_from': (flt['date_from'].isoformat() if flt.get('date_from') else ''),
        'date_to': (flt['date_to'].isoformat() if flt.get('date_to') else ''),
        'party_id': (str(flt['party_id']) if flt.get('party_id') else ''),
        'category': flt.get('category') or '',
        'status': flt.get('status') or '',
        'q': flt.get('search') or '',
        'money': flt.get('money') or '',
    }
    return {k: v for k, v in q.items()
            if v not in ('', None) and k not in set(exclude or ())}


def expense_query(flt=None):
    """The shared-expense documents matching a filter set (newest first)."""
    flt = flt or {}
    q = SharedExpense.query
    if flt.get('date_from'):
        q = q.filter(SharedExpense.date >= flt['date_from'])
    if flt.get('date_to'):
        q = q.filter(SharedExpense.date <= flt['date_to'])
    status = (flt.get('status') or 'active')
    if status == 'active':
        q = q.filter(SharedExpense.is_void.is_(False))
    elif status == 'void':
        q = q.filter(SharedExpense.is_void.is_(True))
    if flt.get('category'):
        q = q.filter(func.lower(func.coalesce(SharedExpense.category, '')) ==
                     flt['category'].strip().lower())
    if flt.get('search'):
        like = f"%{flt['search'].strip().lower()}%"
        q = q.filter(or_(
            func.lower(func.coalesce(SharedExpense.title, '')).like(like),
            func.lower(func.coalesce(SharedExpense.category, '')).like(like),
            func.lower(func.coalesce(SharedExpense.note, '')).like(like),
            func.lower(func.coalesce(SharedExpense.reference, '')).like(like),
        ))
    if flt.get('party_id'):
        pid = int(flt['party_id'])
        sub = db.session.query(SharedExpenseShare.expense_id).filter(
            SharedExpenseShare.party_id == pid)
        q = q.filter(or_(SharedExpense.payer_party_id == pid,
                         SharedExpense.id.in_(sub)))
    if flt.get('money') == 'linked':
        q = q.filter(or_(SharedExpense.txn_id.isnot(None),
                         SharedExpense.cf_entry_id.isnot(None)))
    elif flt.get('money') == 'unlinked':
        q = q.filter(SharedExpense.txn_id.is_(None),
                     SharedExpense.cf_entry_id.is_(None))
    return q.order_by(SharedExpense.date.desc(), SharedExpense.id.desc())


def categories_in_use(*, include_void=False):
    """Distinct category names, for the filter bar and the form's quick picks."""
    q = db.session.query(SharedExpense.category, func.count(SharedExpense.id))
    if not include_void:
        q = q.filter(SharedExpense.is_void.is_(False))
    rows = (q.group_by(SharedExpense.category)
            .order_by(func.count(SharedExpense.id).desc())
            .all())
    names = [(_norm(name) or 'Uncategorised') for name, _count in rows]
    for preset in CATEGORY_PRESETS:
        if preset not in names:
            names.append(preset)
    return names


def read_split_payload(form, *, total_minor, mode):
    """Turn the posted party rows into the list :func:`compute_split` wants.

    Each ticked party posts ``party_ids`` (repeated) plus ``amount_party_<id>`` /
    ``percent_party_<id>`` figures, so a row can never be read against the wrong
    party the way positional arrays can.
    """
    ids = []
    for raw in form.getlist('party_ids'):
        try:
            ids.append(int(raw))
        except (TypeError, ValueError):
            continue
    participants = []
    for pid in ids:
        participants.append({
            'party_id': pid,
            'amount': _norm(form.get(f'amount_party_{pid}')),
            'percent': _norm(form.get(f'percent_party_{pid}')),
        })
    return compute_split(mode, total_minor, participants)


def _resolve_parties(party_ids):
    """Validate a list of ids against the party master (returns models in order)."""
    out = []
    for pid in party_ids:
        party = db.session.get(SharedParty, int(pid))
        if party is None:
            raise ValueError('One of the selected sharing parties no longer exists.')
        out.append(party)
    return out


def _resolve_accounts_category(category_name, category_id=None):
    """The Accounts cash-flow head a shared expense is filed under.

    The module's own category ("Car Fuel") *is* the Accounts category name, so
    one vocabulary serves both screens: an unknown name is created in Accounts
    rather than forcing the operator to pre-create it in Settings.
    """
    from hdc.services.cashflow_register import _cf_resolve_category
    name = _norm(category_name)
    if category_id:
        return _cf_resolve_category('out', int(category_id), None,
                                    required=True, create_if_missing=True)
    return _cf_resolve_category('out', None, name or DEFAULT_ACCOUNTS_CATEGORY,
                                required=True, create_if_missing=True)


def post_expense_to_accounts(expense, *, account_id, category_name=None,
                             category_id=None, actor=None, commit=False):
    """Pay for a shared expense out of an accounts account — through Accounts.

    Uses ``save_manual_cash_flow_entry`` (the Cash Flow register engine), so the
    row is an ordinary register entry: it shows in the register, in All Entries,
    in the Cash Flow report, and it obeys the day lock, the overdraft block and
    the idempotency guard like any other payment.  The only difference is the
    ``source_type`` tag that points back at this module.
    """
    from hdc.services.cashflow_register import save_manual_cash_flow_entry

    account = db.session.get(Account, int(account_id)) if account_id else None
    if account is None:
        raise ValueError('Choose the cash / bank account the money left.')

    cat = _resolve_accounts_category(category_name or expense.category, category_id)
    posted = _pkt_now_naive()
    if expense.date is not None:
        posted = posted.replace(year=expense.date.year, month=expense.date.month,
                                day=expense.date.day)

    description = expense.title or 'Shared expense'
    if cat is not None and _norm(expense.category):
        description = f'{expense.category} — {expense.title}'

    entry, created = save_manual_cash_flow_entry(
        direction='out',
        amount=money_value(expense.total_minor),
        account_id=int(account.id),
        category_id=(int(cat.id) if cat is not None else None),
        category_name=(None if cat is not None else (category_name or expense.category)),
        party_name=None,
        description=description[:200],
        note=(expense.note or '').strip() or None,
        reference=(expense.reference or '').strip() or None,
        date_posted=posted,
        source_type='shared_expense',
        source_id=int(expense.id),
        actor=actor,
        commit=False,
        enforce_category_rules=False,
    )
    if entry is None:
        raise ValueError('Accounts refused the entry — nothing was posted.')

    expense.cf_entry_id = int(entry.id)
    expense.txn_id = int(entry.account_tx_id) if entry.account_tx_id else None
    expense.paid_from_account_id = int(account.id)
    sync_money_fields(expense, 'total_amount', 'total_amount_minor')
    db.session.flush()
    if commit:
        db.session.commit()
    return entry, created


def link_accounts_entry(expense, *, cf_entry_id=None, txn_id=None):
    """Attach an accounts entry that was already recorded in Accounts.

    Exactly one of ``cf_entry_id`` (cash-flow register document) / ``txn_id``
    (raw ledger row) is expected.  A register entry already linked to another
    shared expense is refused: two allocations claiming one payment would each
    look fully funded while only one payment exists.
    """
    entry = None
    if cf_entry_id:
        entry = db.session.get(CashFlowEntry, int(cf_entry_id))
        if entry is None:
            raise ValueError('That Accounts entry no longer exists.')
        if entry.is_void:
            raise ValueError('That Accounts entry is voided — pick a live one.')
        if _norm(entry.direction).lower() != 'out':
            raise ValueError('A shared expense has to be paid out — '
                             'that entry is money in.')
        clash = (SharedExpense.query
                 .filter(SharedExpense.cf_entry_id == int(entry.id),
                         SharedExpense.is_void.is_(False),
                         SharedExpense.id != int(expense.id or 0))
                 .first())
        if clash is not None:
            raise ValueError(
                f'That Accounts entry is already allocated on shared expense '
                f'#{clash.id} ({clash.title}).')
        expense.cf_entry_id = int(entry.id)
        expense.txn_id = int(entry.account_tx_id) if entry.account_tx_id else None
        expense.paid_from_account_id = int(entry.account_id) if entry.account_id else None
        if not expense.date and entry.date_posted:
            expense.date = entry.date_posted.date()
        db.session.flush()
        return entry

    if txn_id:
        txn = db.session.get(AccountTransaction, int(txn_id))
        if txn is None:
            raise ValueError('That ledger entry no longer exists.')
        if txn.is_void:
            raise ValueError('That ledger entry is voided — pick a live one.')
        if _norm(txn.category).lower() not in ('expense', 'personal', 'salary', 'transfer'):
            raise ValueError('That ledger entry is not a payment out.')
        clash = (SharedExpense.query
                 .filter(SharedExpense.txn_id == int(txn.id),
                         SharedExpense.is_void.is_(False),
                         SharedExpense.id != int(expense.id or 0))
                 .first())
        if clash is not None:
            raise ValueError(
                f'That ledger entry is already allocated on shared expense '
                f'#{clash.id} ({clash.title}).')
        expense.txn_id = int(txn.id)
        expense.paid_from_account_id = int(txn.from_account_id) if txn.from_account_id else None
        db.session.flush()
        return txn

    raise ValueError('Pick the Accounts entry this expense was paid with.')


def find_by_idempotency_key(key):
    key = _norm(key)
    if not key:
        return None
    return SharedExpense.query.filter(SharedExpense.idempotency_key == key).first()


def save_shared_expense(*, payload, form=None, actor=None, expense=None, commit=True):
    """Create (or update) a shared expense and its split.  Returns ``(expense, created)``.

    ``payload`` carries the plain fields; ``form`` (optional) carries the party
    rows, read by :func:`read_split_payload`.  Money that the operator asked for
    is posted **after** the document exists, through the accounts engine, so a
    posting failure leaves no half-written expense behind.

    A re-posted form (double click, browser retry) is caught by
    ``idempotency_key``: the second submission returns the first document
    instead of creating a twin that would double every share.
    """
    is_new = expense is None
    if is_new:
        existing = find_by_idempotency_key(payload.get('idempotency_key'))
        if existing is not None:
            return existing, False
    expense = expense or SharedExpense()
    if expense.id is not None and expense.is_void:
        raise ValueError('This expense is voided — restore it before editing.')

    title = _norm(payload.get('title')) or _norm(payload.get('category'))
    if not title:
        raise ValueError('Enter what the expense was for (e.g. Car Fuel).')
    category = _norm(payload.get('category')) or title
    total_minor = _money_minor(payload.get('total_amount'), field='Total amount')
    if total_minor <= 0:
        raise ValueError('Total amount must be greater than zero.')

    mode = _norm(payload.get('split_mode')).lower() or 'equal'
    if mode not in SHARED_SPLIT_MODES:
        raise ValueError('Choose Equal, Custom amounts or Percentages.')

    expenses_date = _parse_date(_norm(payload.get('date')), fallback=_pkt_today())
    if expenses_date is None:
        raise ValueError('Enter a valid date.')

    payer_party_id = int(payload.get('payer_party_id') or 0) or None
    if payer_party_id and db.session.get(SharedParty, payer_party_id) is None:
        raise ValueError('The selected "paid by" party no longer exists.')
    money_source = _norm(payload.get('money_source')).lower() or 'none'
    if money_source not in MONEY_SOURCES:
        money_source = 'none'
    account_id = int(payload.get('account_id') or 0) or None
    linked_entry_id = int(payload.get('cf_entry_id') or 0) or None

    # --- split first: a bad split must never touch the money side ----------
    shares = (read_split_payload(form, total_minor=total_minor, mode=mode)
              if form is not None
              else compute_split(mode, total_minor,
                                 [{'party_id': s.party_id, 'amount': money_value(s.minor)}
                                  for s in (expense.shares or [])]))
    _resolve_parties([s['party_id'] for s in shares])

    expense.date = expenses_date
    expense.title = title[:160]
    expense.category = category[:80]
    expense.total_amount = money_value(total_minor)
    expense.total_amount_minor = total_minor
    expense.payer_party_id = payer_party_id
    expense.split_mode = mode
    expense.reference = _norm(payload.get('reference'))[:120] or None
    expense.note = _norm(payload.get('note'))[:400] or None
    if is_new:
        expense.created_by = _actor_name(actor) or None
        expense.idempotency_key = _norm(payload.get('idempotency_key'))[:64] or None
        expense.is_void = False
    db.session.add(expense)
    db.session.flush()

    _write_shares(expense, shares)

    # --- the money side ---------------------------------------------------
    if money_source == 'post':
        if expense.cf_entry_id:
            _amend_posted_entry(expense, account_id=account_id,
                                category_name=category,
                                category_id=payload.get('cf_category_id'),
                                actor=actor)
        else:
            post_expense_to_accounts(expense, account_id=account_id,
                                     category_name=category,
                                     category_id=payload.get('cf_category_id'),
                                     actor=actor, commit=False)
    elif money_source == 'link' and not expense.cf_entry_id:
        link_accounts_entry(expense, cf_entry_id=linked_entry_id)

    db.session.flush()
    if commit:
        db.session.commit()
    return expense, True


def _write_shares(expense, shares):
    """Replace the expense's share rows with the computed split."""
    existing = {int(s.party_id): s for s in (expense.shares or [])}
    keep = set()
    for index, row in enumerate(shares):
        pid = int(row['party_id'])
        share = existing.get(pid)
        if share is None:
            share = SharedExpenseShare(expense_id=int(expense.id), party_id=pid)
            db.session.add(share)
        share.amount = money_value(row['amount_minor'])
        share.amount_minor = int(row['amount_minor'])
        share.percent_bp = row.get('percent_bp')
        share.position = index
        keep.add(pid)
    for pid, share in existing.items():
        if pid not in keep:
            db.session.delete(share)
    db.session.flush()


def _amend_posted_entry(expense, *, account_id=None, category_name=None,
                        category_id=None, actor=None):
    """Keep a module-posted register entry in step with its expense.

    Only entries this module posted are amended; an entry that came from
    Accounts is left exactly as Accounts recorded it (unlink it and post a fresh
    one if the amount really changed).
    """
    from hdc.services.cashflow_register import amend_manual_cash_flow_entry
    entry = db.session.get(CashFlowEntry, int(expense.cf_entry_id)) if expense.cf_entry_id else None
    if entry is None:
        raise ValueError('The linked Accounts entry disappeared — re-link it.')
    if _norm(entry.source_type).lower() != 'shared_expense':
        if (int(expense.total_minor) != int(entry.amount_minor or to_minor(entry.amount or 0))
                or account_id not in (None, entry.account_id)):
            raise ValueError(
                'This expense is linked to an Accounts entry recorded in the '
                'Accounts section. Unlink it first if the amount or account '
                'has to change.')
        return entry

    cat = _resolve_accounts_category(category_name or expense.category, category_id)
    posted = _pkt_now_naive()
    if expense.date is not None:
        posted = posted.replace(year=expense.date.year, month=expense.date.month,
                                day=expense.date.day)
    # The register corrects a posting the immutable way (void + replace), so the
    # expense follows the *replacement* entry, not the one it replaced.
    new_entry, _old_entry = amend_manual_cash_flow_entry(
        entry,
        amount=money_value(expense.total_minor),
        account_id=(int(account_id) if account_id else None),
        category_id=(int(cat.id) if cat is not None else None),
        description=(f'{expense.category} — {expense.title}')[:200],
        note=(expense.note or '').strip() or None,
        date_posted=posted,
        actor=actor,
        reason=f'Shared expense #{expense.id} edited',
        commit=False,
    )
    if new_entry is not None:
        expense.cf_entry_id = int(new_entry.id)
        expense.txn_id = int(new_entry.account_tx_id) if new_entry.account_tx_id else None
        expense.paid_from_account_id = (int(new_entry.account_id)
                                        if new_entry.account_id else None)
    db.session.flush()
    return new_entry


def void_shared_expense(expense, *, reason=None, actor=None, void_linked_entry=True,
                        commit=True):
    """Void an allocation (and, unless told otherwise, its Accounts entry).

    The linked entry is voided through the accounts engine, so the ledger, the
    register and the day-close rules all stay authoritative — if the day is
    locked the void is refused and nothing changes.
    """
    if expense.is_void:
        raise ValueError('That shared expense is already voided.')
    reason = _norm(reason)
    if not reason:
        raise ValueError('Give a reason for voiding this expense.')

    if void_linked_entry and expense.cf_entry_id:
        from hdc.services.cashflow_register import void_manual_cash_flow_entry
        entry = db.session.get(CashFlowEntry, int(expense.cf_entry_id))
        if entry is not None and not entry.is_void:
            void_manual_cash_flow_entry(
                entry, reason=f'Shared expense #{expense.id} voided: {reason}',
                actor=actor, commit=False)

    expense.is_void = True
    expense.void_reason = reason[:300]
    expense.voided_by = _actor_name(actor) or None
    expense.voided_at = _pkt_now_naive()
    db.session.flush()
    if commit:
        db.session.commit()
    return expense


def restore_shared_expense(expense, *, actor=None, commit=True, restore_linked_entry=True):
    """Undo a void — the allocation comes back, and its Accounts entry too."""
    if not expense.is_void:
        raise ValueError('That shared expense is not voided.')
    if restore_linked_entry and expense.cf_entry_id:
        from hdc.services.cashflow_register import restore_manual_cash_flow_entry
        entry = db.session.get(CashFlowEntry, int(expense.cf_entry_id))
        if entry is not None and entry.is_void:
            restore_manual_cash_flow_entry(entry, actor=actor, commit=False)
    expense.is_void = False
    expense.void_reason = None
    expense.voided_by = None
    expense.voided_at = None
    db.session.flush()
    if commit:
        db.session.commit()
    return expense


def delete_shared_expense(expense, *, commit=True):
    """Remove an allocation that never touched money (nothing linked)."""
    if expense.cf_entry_id or expense.txn_id:
        raise ValueError('This expense is linked to Accounts — void it instead '
                         'of deleting, so the ledger stays explained.')
    db.session.delete(expense)
    if commit:
        db.session.commit()


# ---------------------------------------------------------------------------
# settlements
# ---------------------------------------------------------------------------

def settlement_query(*, status='active', date_from=None, date_to=None, party_id=None):
    q = SharedSettlement.query
    if status == 'active':
        q = q.filter(SharedSettlement.is_void.is_(False))
    elif status == 'void':
        q = q.filter(SharedSettlement.is_void.is_(True))
    if date_from:
        q = q.filter(SharedSettlement.date >= date_from)
    if date_to:
        q = q.filter(SharedSettlement.date <= date_to)
    if party_id:
        pid = int(party_id)
        q = q.filter(or_(SharedSettlement.from_party_id == pid,
                         SharedSettlement.to_party_id == pid))
    return q.order_by(SharedSettlement.date.desc(), SharedSettlement.id.desc())


def save_settlement(*, payload, actor=None, settlement=None, commit=True):
    """Record one head squaring up with another head (or into an account)."""
    settlement = settlement or SharedSettlement()
    from_party_id = int(payload.get('from_party_id') or 0) or None
    to_party_id = int(payload.get('to_party_id') or 0) or None
    to_account_id = int(payload.get('to_account_id') or 0) or None

    if not from_party_id:
        raise ValueError('Choose who paid.')
    if db.session.get(SharedParty, from_party_id) is None:
        raise ValueError('That party no longer exists.')
    if to_party_id and to_account_id:
        raise ValueError('Choose either a party or an account as the receiver, not both.')
    if not to_party_id and not to_account_id:
        raise ValueError('Choose who received the money.')
    if to_party_id and int(to_party_id) == int(from_party_id):
        raise ValueError('A party cannot settle with itself.')
    if to_party_id and db.session.get(SharedParty, to_party_id) is None:
        raise ValueError('That party no longer exists.')
    if to_account_id and db.session.get(Account, to_account_id) is None:
        raise ValueError('That account no longer exists.')

    amount_minor = _money_minor(payload.get('amount'), field='Settlement amount')
    if amount_minor <= 0:
        raise ValueError('Settlement amount must be greater than zero.')
    when = _parse_date(_norm(payload.get('date')), fallback=_pkt_today())
    if when is None:
        raise ValueError('Enter a valid date.')

    settlement.date = when
    settlement.from_party_id = from_party_id
    settlement.to_party_id = to_party_id
    settlement.to_account_id = to_account_id
    settlement.amount = money_value(amount_minor)
    settlement.amount_minor = amount_minor
    settlement.note = _norm(payload.get('note'))[:300] or None
    settlement.created_by = settlement.created_by or (_actor_name(actor) or None)
    settlement.is_void = False
    db.session.add(settlement)
    db.session.flush()

    if _norm(payload.get('money_source')).lower() == 'post':
        post_settlement_to_accounts(
            settlement,
            from_account_id=payload.get('from_account_id'),
            to_account_id=payload.get('settlement_to_account_id') or to_account_id,
            actor=actor, commit=False)
    db.session.flush()
    if commit:
        db.session.commit()
    return settlement


def post_settlement_to_accounts(settlement, *, from_account_id=None, to_account_id=None,
                                actor=None, commit=False):
    """Move the settlement money between accounts — through the register engine."""
    from hdc.services.cashflow_register import save_manual_cash_flow_entry

    src = db.session.get(Account, int(from_account_id)) if from_account_id else None
    dst = db.session.get(Account, int(to_account_id)) if to_account_id else None
    if src is None or dst is None:
        raise ValueError('A settlement transfer needs both accounts (from and to).')
    if int(src.id) == int(dst.id):
        raise ValueError('The settlement accounts cannot be the same.')
    posted = _pkt_now_naive()
    if settlement.date is not None:
        posted = posted.replace(year=settlement.date.year,
                                month=settlement.date.month, day=settlement.date.day)
    description = f'Settlement — {settlement.from_party.name}'
    if settlement.to_party is not None:
        description = f'{description} to {settlement.to_party.name}'
    elif settlement.to_account is not None:
        description = f'{description} to {settlement.to_account.name}'
    entry, _created = save_manual_cash_flow_entry(
        direction='transfer',
        amount=money_value(settlement.minor),
        account_id=int(src.id),
        destination_account_id=int(dst.id),
        description=description[:200],
        note=(settlement.note or '').strip() or None,
        date_posted=posted,
        source_type='shared_settlement',
        source_id=int(settlement.id),
        actor=actor,
        commit=False,
    )
    settlement.cf_entry_id = int(entry.id)
    settlement.txn_id = int(entry.account_tx_id) if entry.account_tx_id else None
    db.session.flush()
    if commit:
        db.session.commit()
    return entry


def void_settlement(settlement, *, reason=None, actor=None, void_linked_entry=True,
                    commit=True):
    if settlement.is_void:
        raise ValueError('That settlement is already voided.')
    _void_settlement_row(settlement, reason=reason, actor=actor,
                         void_linked_entry=void_linked_entry)
    db.session.flush()
    if commit:
        db.session.commit()
    return settlement


def _void_settlement_row(settlement, *, reason=None, actor=None,
                         void_linked_entry=True):
    if settlement.is_void:
        return settlement
    if void_linked_entry and settlement.cf_entry_id:
        from hdc.services.cashflow_register import void_manual_cash_flow_entry
        entry = db.session.get(CashFlowEntry, int(settlement.cf_entry_id))
        if entry is not None and not entry.is_void:
            void_manual_cash_flow_entry(
                entry, reason=f'Settlement #{settlement.id} voided',
                actor=actor, commit=False)
    settlement.is_void = True
    settlement.void_reason = (_norm(reason) or 'Auto-voided with its expense')[:300]
    settlement.voided_by = _actor_name(actor) or None
    settlement.voided_at = _pkt_now_naive()
    return settlement


def restore_settlement(settlement, *, actor=None, commit=True, restore_linked_entry=True):
    if not settlement.is_void:
        raise ValueError('That settlement is not voided.')
    if restore_linked_entry and settlement.cf_entry_id:
        from hdc.services.cashflow_register import restore_manual_cash_flow_entry
        entry = db.session.get(CashFlowEntry, int(settlement.cf_entry_id))
        if entry is not None and entry.is_void:
            restore_manual_cash_flow_entry(entry, actor=actor, commit=False)
    settlement.is_void = False
    settlement.void_reason = None
    settlement.voided_by = None
    settlement.voided_at = None
    db.session.flush()
    if commit:
        db.session.commit()
    return settlement


# ---------------------------------------------------------------------------
# the ledgers (derived on every read)
# ---------------------------------------------------------------------------

def _expense_total_minor(expense):
    return int(expense.total_minor)


def _lines_for_expense(expense, party_names):
    """One line per party on an expense: their share, and the payment if they bore it."""
    payer_id = int(expense.payer_party_id) if expense.payer_party_id else None
    lines = []
    seen = set()
    for share in (expense.shares or []):
        pid = int(share.party_id)
        seen.add(pid)
        lines.append({
            'date': expense.date,
            'kind': 'expense',
            'expense_id': int(expense.id),
            'settlement_id': None,
            'party_id': pid,
            'party_name': party_names.get(pid, f'Party #{pid}'),
            'title': expense.title,
            'category': expense.category or '',
            'reference': expense.reference or '',
            'total_minor': _expense_total_minor(expense),
            'share_minor': int(share.minor),
            'paid_minor': (_expense_total_minor(expense) if payer_id == pid else 0),
            'settled_out_minor': 0,
            'settled_in_minor': 0,
            'is_void': bool(expense.is_void),
            'linked': bool(expense.txn_id or expense.cf_entry_id),
            'split_mode': expense.split_mode,
            'note': expense.note or '',
        })
    if payer_id and payer_id not in seen:
        # Paid the bill but carries no share of it (a head can front a cost that
        # belongs entirely to the others).
        lines.append({
            'date': expense.date,
            'kind': 'expense',
            'expense_id': int(expense.id),
            'settlement_id': None,
            'party_id': payer_id,
            'party_name': party_names.get(payer_id, f'Party #{payer_id}'),
            'title': expense.title,
            'category': expense.category or '',
            'reference': expense.reference or '',
            'total_minor': _expense_total_minor(expense),
            'share_minor': 0,
            'paid_minor': _expense_total_minor(expense),
            'settled_out_minor': 0,
            'settled_in_minor': 0,
            'is_void': bool(expense.is_void),
            'linked': bool(expense.txn_id or expense.cf_entry_id),
            'split_mode': expense.split_mode,
            'note': expense.note or '',
        })
    return lines


def _lines_for_settlement(settlement, party_names):
    amount = int(settlement.minor)
    out = [{
        'date': settlement.date,
        'kind': 'settlement',
        'expense_id': None,
        'settlement_id': int(settlement.id),
        'party_id': int(settlement.from_party_id),
        'party_name': party_names.get(int(settlement.from_party_id), ''),
        'title': 'Paid ' + (settlement.to_party.name if settlement.to_party
                            else (settlement.to_account.name if settlement.to_account
                                  else 'outside')),
        'category': 'Settlement',
        'reference': '',
        'total_minor': amount,
        'share_minor': 0,
        'paid_minor': 0,
        'settled_out_minor': amount,
        'settled_in_minor': 0,
        'is_void': bool(settlement.is_void),
        'linked': bool(settlement.is_linked),
        'split_mode': '',
        'note': settlement.note or '',
    }]
    if settlement.to_party_id:
        out.append({
            'date': settlement.date,
            'kind': 'settlement',
            'expense_id': None,
            'settlement_id': int(settlement.id),
            'party_id': int(settlement.to_party_id),
            'party_name': party_names.get(int(settlement.to_party_id), ''),
            'title': f'Received from {settlement.from_party.name if settlement.from_party else "party"}',
            'category': 'Settlement',
            'reference': '',
            'total_minor': amount,
            'share_minor': 0,
            'paid_minor': 0,
            'settled_out_minor': 0,
            'settled_in_minor': amount,
            'is_void': bool(settlement.is_void),
            'linked': bool(settlement.is_linked),
            'split_mode': '',
            'note': settlement.note or '',
        })
    return out


def ledger_lines(*, flt=None, party_id=None, expenses=None, settlements=None,
                 include_void=None):
    """Every line of every (or one) party ledger, oldest first.

    Each line carries its own delta, where ``delta = share - paid - settled_out
    + settled_in`` and a positive delta means the party's debt grew.  The running
    balance is added by :func:`party_statement`, which needs one party's lines.
    """
    flt = flt or {}
    include_void = bool(include_void) if include_void is not None else (flt.get('status') == 'all')
    party_names = {int(p.id): p.name for p in party_options()}

    if expenses is None:
        expenses = expense_query(flt).all()
    if settlements is None:
        same_party = party_id or flt.get('party_id')
        settlements = settlement_query(
            status=('all' if include_void else 'active'),
            date_from=flt.get('date_from'), date_to=flt.get('date_to'),
            party_id=same_party).all()

    lines = []
    for expense in expenses:
        if expense.is_void and not include_void:
            continue
        lines.extend(_lines_for_expense(expense, party_names))
    for settlement in settlements:
        if settlement.is_void and not include_void:
            continue
        lines.extend(_lines_for_settlement(settlement, party_names))

    if party_id:
        lines = [ln for ln in lines if ln['party_id'] == int(party_id)]

    for line in lines:
        line['delta_minor'] = (int(line['share_minor']) - int(line['paid_minor'])
                               - int(line['settled_out_minor']) + int(line['settled_in_minor']))
    lines.sort(key=lambda ln: (ln['date'] or _date.min, ln['kind'] != 'expense',
                               ln['expense_id'] or 0, ln['settlement_id'] or 0,
                               ln['party_id'] or 0))
    return lines


def party_statement(party_id, *, flt=None, include_void=False):
    """One party's running ledger: lines oldest first, each with a running balance."""
    lines = ledger_lines(flt=flt, party_id=int(party_id), include_void=include_void)
    running = 0
    for line in lines:
        running += int(line['delta_minor'])
        line['running_minor'] = running
    return lines


def party_balances(*, flt=None, include_void=False):
    """Per-party share / paid / settled totals and the balance each one carries.

    Returns ``(rows, outside, totals)``:

    * ``rows``     — one dict per sharing head, in party order;
    * ``outside``  — the not-a-party side: expenses paid straight from an
      accounts account with no head named, and settlements paid into an account.
      It is what keeps the whole module balancing to zero, and it is reported
      rather than hidden: nothing is "lost", it is simply not attributed yet;
    * ``totals``   — column totals + the balance check.
    """
    flt = flt or {}
    lines = ledger_lines(flt=flt, include_void=include_void)

    rows = []
    by_party = {}
    for party in party_options():
        row = {
            'party': party,
            'party_id': int(party.id),
            'party_name': party.name,
            'share_minor': 0, 'paid_minor': 0,
            'settled_out_minor': 0, 'settled_in_minor': 0,
            'line_count': 0,
        }
        by_party[int(party.id)] = row
        rows.append(row)
    for line in lines:
        row = by_party.get(int(line['party_id']))
        if row is None:
            continue
        row['share_minor'] += int(line['share_minor'])
        row['paid_minor'] += int(line['paid_minor'])
        row['settled_out_minor'] += int(line['settled_out_minor'])
        row['settled_in_minor'] += int(line['settled_in_minor'])
        row['line_count'] += 1

    for row in rows:
        row['total_minor'] = (row['share_minor'] + row['paid_minor']
                              + row['settled_out_minor'] + row['settled_in_minor'])
        row['balance_minor'] = (row['share_minor'] - row['paid_minor']
                                - row['settled_out_minor'] + row['settled_in_minor'])
        row['balance'] = balance_state(row['balance_minor'])
        row['share_amount'] = money_value(row['share_minor'])
        row['paid_amount'] = money_value(row['paid_minor'])
        row['settled_out_amount'] = money_value(row['settled_out_minor'])
        row['settled_in_amount'] = money_value(row['settled_in_minor'])
        row['balance_amount'] = money_value(abs(row['balance_minor']))

    # --- the not-a-party side ---------------------------------------------
    expenses = expense_query({**flt, 'status': ('all' if include_void else 'active')}).all()
    unattributed_minor = sum(_expense_total_minor(e) for e in expenses
                             if not e.payer_party_id and not e.is_void)
    to_account_minor = sum(int(s.minor) for s in
                           settlement_query(status='active',
                                            date_from=flt.get('date_from'),
                                            date_to=flt.get('date_to')).all()
                           if s.to_account_id)
    outside = {
        'party_id': None,
        'party_name': 'Not attributed to a head',
        'unattributed_minor': unattributed_minor,
        'settled_in_minor': to_account_minor,
        'balance_minor': -(unattributed_minor - to_account_minor),
        'unattributed_amount': money_value(unattributed_minor),
        'settled_in_amount': money_value(to_account_minor),
        'balance_amount': money_value(abs(unattributed_minor - to_account_minor)),
    }
    outside['balance'] = balance_state(outside['balance_minor'])
    outside['label'] = 'Paid from accounts — no head named' if unattributed_minor \
        else 'Settlements into accounts'

    totals = {
        'share_minor': sum(r['share_minor'] for r in rows),
        'paid_minor': sum(r['paid_minor'] for r in rows),
        'settled_out_minor': sum(r['settled_out_minor'] for r in rows),
        'settled_in_minor': sum(r['settled_in_minor'] for r in rows),
        'balance_minor': sum(r['balance_minor'] for r in rows),
        'outside_balance_minor': outside['balance_minor'],
    }
    totals['share_amount'] = money_value(totals['share_minor'])
    totals['paid_amount'] = money_value(totals['paid_minor'])
    totals['settled_out_amount'] = money_value(totals['settled_out_minor'])
    totals['settled_in_amount'] = money_value(totals['settled_in_minor'])
    totals['balance_amount'] = money_value(abs(totals['balance_minor']))
    totals['balances'] = totals['balance_minor'] + totals['outside_balance_minor']
    totals['balanced'] = abs(totals['balances']) < 1
    return rows, outside, totals


def module_summary(*, flt=None):
    """KPI tiles for the ledger page (all figures from the same filtered read)."""
    flt = flt or {}
    expenses = expense_query(flt).all()
    active = [e for e in expenses if not e.is_void]
    total_minor = sum(_expense_total_minor(e) for e in active)
    linked = [e for e in active if e.cf_entry_id or e.txn_id]
    unlinked = [e for e in active if not (e.cf_entry_id or e.txn_id)]
    rows, outside, totals = party_balances(flt=flt)
    owed_in = sum(r['balance_minor'] for r in rows if r['balance_minor'] > 0)
    owed_out = sum(-r['balance_minor'] for r in rows if r['balance_minor'] < 0)
    return {
        'expense_count': len(active),
        'void_count': len([e for e in expenses if e.is_void]),
        'total_minor': total_minor,
        'total_amount': money_value(total_minor),
        'linked_count': len(linked),
        'unlinked_count': len(unlinked),
        'linked_minor': sum(_expense_total_minor(e) for e in linked),
        'unlinked_minor': sum(_expense_total_minor(e) for e in unlinked),
        'linked_amount': money_value(sum(_expense_total_minor(e) for e in linked)),
        'unlinked_amount': money_value(sum(_expense_total_minor(e) for e in unlinked)),
        'party_count': len(rows),
        'active_party_count': len([r for r in rows if r['party'].is_active]),
        'owed_in_minor': owed_in,
        'owed_out_minor': owed_out,
        'owed_in_amount': money_value(owed_in),
        'owed_out_amount': money_value(owed_out),
        'outside_minor': outside['balance_minor'],
        'outside_amount': money_value(abs(outside['balance_minor'])),
        'balances': totals,
        'outside': outside,
    }


def report_matrix(*, flt=None, include_void=False):
    """The clean "who shares what" report: one row per bill, one column per head.

    Returns ``(parties, rows, totals)`` where every ``rows[i]['cells']`` entry is
    the party's exact share of that bill (paisa), and ``totals`` holds the column
    totals, the grand total and the per-party balance — so the same page answers
    "what did this bill cost and how was it cut" and "where does each head stand".
    """
    flt = flt or {}
    expenses = expense_query({**flt, 'status': 'all' if include_void else 'active'}).all()
    parties = party_options()
    party_ids = [int(p.id) for p in parties]

    rows = []
    column_totals = {pid: 0 for pid in party_ids}
    grand_total = 0
    for expense in expenses:
        if expense.is_void and not include_void:
            continue
        cells = {pid: 0 for pid in party_ids}
        for share in (expense.shares or []):
            if int(share.party_id) in cells:
                cells[int(share.party_id)] = int(share.minor)
        total = _expense_total_minor(expense)
        grand_total += total
        for pid, minor in cells.items():
            column_totals[pid] += minor
        rows.append({
            'expense': expense,
            'id': int(expense.id),
            'date': expense.date,
            'title': expense.title,
            'category': expense.category or '',
            'reference': expense.reference or '',
            'total_minor': total,
            'total_amount': money_value(total),
            'cells': cells,
            'cell_amounts': {pid: money_value(minor) for pid, minor in cells.items()},
            'cells_minor': cells,
            'payer_name': (expense.payer_party.name if expense.payer_party
                           else (expense.paid_from_account.name
                                 if expense.paid_from_account else '—')),
            'payer_party_id': (int(expense.payer_party_id) if expense.payer_party_id else None),
            'is_void': bool(expense.is_void),
            'linked': bool(expense.cf_entry_id or expense.txn_id),
            'split_mode': expense.split_mode,
        })
    rows.sort(key=lambda r: (r['date'] or _date.min, r['id']), reverse=True)

    balances = {r['party_id']: r for r in party_balances(flt=flt)[0]}
    totals = {
        'total_minor': grand_total,
        'total_amount': money_value(grand_total),
        'cells': column_totals,
        'cell_amounts': {pid: money_value(minor) for pid, minor in column_totals.items()},
        'balances': balances,
        'row_count': len(rows),
    }
    return parties, rows, totals


def category_totals(*, flt=None, include_void=False):
    """Share totals per category (the "what are we actually spending on" cut)."""
    flt = flt or {}
    expenses = expense_query({**flt, 'status': 'all' if include_void else 'active'}).all()
    buckets = {}
    for expense in expenses:
        if expense.is_void and not include_void:
            continue
        name = _norm(expense.category) or 'Uncategorised'
        bucket = buckets.setdefault(name, {'category': name, 'count': 0, 'total_minor': 0})
        bucket['count'] += 1
        bucket['total_minor'] += _expense_total_minor(expense)
    out = sorted(buckets.values(), key=lambda b: (-b['total_minor'], b['category'].lower()))
    for bucket in out:
        bucket['total_amount'] = money_value(bucket['total_minor'])
    return out


def month_totals(*, flt=None, months=12, include_void=False):
    """Share totals per calendar month, newest first."""
    flt = dict(flt or {})
    flt.pop('date_from', None)
    flt.pop('date_to', None)
    expenses = expense_query({**flt, 'status': 'all' if include_void else 'active'}).all()
    buckets = {}
    for expense in expenses:
        if expense.is_void and not include_void:
            continue
        when = expense.date or _pkt_today()
        key = f'{when.year:04d}-{when.month:02d}'
        bucket = buckets.setdefault(key, {'month': key, 'label': when.strftime('%b %Y'),
                                          'count': 0, 'total_minor': 0})
        bucket['count'] += 1
        bucket['total_minor'] += _expense_total_minor(expense)
    out = sorted(buckets.values(), key=lambda b: b['month'], reverse=True)[:int(months or 12)]
    for bucket in out:
        bucket['total_amount'] = money_value(bucket['total_minor'])
    return out


# ---------------------------------------------------------------------------
# pickers
# ---------------------------------------------------------------------------

def account_options():
    """Active cash / bank / company accounts, for "paid from" and settlements."""
    from hdc.services.transaction_entry import money_accounts
    return money_accounts()


def all_accounts():
    """Every live account (parties can be linked to a non-treasury account)."""
    return (Account.query
            .filter(Account.is_void.is_(False))
            .order_by(Account.name.asc())
            .all())


def accounts_category_options(direction='out'):
    """Cash-flow categories usable for a shared expense (out or both)."""
    return (CashFlowCategory.query
            .filter(CashFlowCategory.is_active.is_(True),
                    func.lower(func.coalesce(CashFlowCategory.direction, 'both'))
                    .in_(('out', 'both')))
            .order_by(CashFlowCategory.name.asc())
            .all())


def linkable_entries(*, limit=200, include_linked=False):
    """Recent "money out" entries the module can be pointed at.

    These are the Accounts entries an operator would link a split to: register
    documents, newest first, marked when another shared expense already claims
    them so the same payment cannot be allocated twice.
    """
    claimed = {int(row[0]) for row in
               db.session.query(SharedExpense.cf_entry_id)
               .filter(SharedExpense.cf_entry_id.isnot(None),
                       SharedExpense.is_void.is_(False)).all()}
    q = (CashFlowEntry.query
         .filter(CashFlowEntry.is_void.is_(False),
                 func.lower(func.coalesce(CashFlowEntry.direction, '')) == 'out')
         .order_by(CashFlowEntry.date_posted.desc(), CashFlowEntry.id.desc())
         .limit(int(limit)))
    out = []
    for entry in q.all():
        already = int(entry.id) in claimed
        if already and not include_linked:
            continue
        out.append({
            'entry': entry,
            'id': int(entry.id),
            'label': (f'{entry.date_posted.strftime("%d %b %Y") if entry.date_posted else ""}'
                      f' · {entry.description or "Payment"} · {money_text(entry.amount_minor)}'
                      f' · {(entry.account.name if entry.account else "")}'),
            'already_linked': already,
        })
    return out


def unlinked_expense_documents(*, limit=20):
    """Shared expenses with no accounts entry yet (the "needs paying" list)."""
    return (SharedExpense.query
            .filter(SharedExpense.is_void.is_(False),
                    SharedExpense.cf_entry_id.is_(None),
                    SharedExpense.txn_id.is_(None))
            .order_by(SharedExpense.date.desc(), SharedExpense.id.desc())
            .limit(int(limit))
            .all())


def account_balances():
    """Live balances for the accounts named on the module's pages."""
    from hdc.services.accounts import _account_balance_map
    return _account_balance_map() or {}
