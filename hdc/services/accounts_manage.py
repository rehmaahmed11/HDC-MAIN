"""HDC services.accounts_manage — the unified Accounts list / edit layer.

This module backs the *Manage Accounts* page (``/hdc/accounts/manage``) and the
full *Edit Account* page (``/hdc/accounts/<id>/edit``), which bring the AMS
accounts UI (``templates/accounts/manage_accounts.html``,
``edit_account.html``) into HDC.

Why a separate module instead of extending ``services/accounts.py``?
--------------------------------------------------------------------
``services/accounts.py`` is the unified ledger engine: it posts, voids,
reverses and balances transactions, and every other domain module (payroll,
purchases, subcontract, office, cash flow) depends on it.  The code below is
purely *presentation and maintenance of the account master data* — listing,
grouping, classifying and editing accounts.  Keeping it apart means the ledger
engine stays free of view-shaped helpers, and the AMS-derived classification
hierarchy has one obvious home.

Design rules followed here
--------------------------
* **Balances are never stored.**  They are derived from ``hdc_account_txn``
  exactly like the rest of HDC (see ``CASHFLOW_MODEL.md`` §2), so this module
  can never drift from the ledger.
* **Nothing is hard-deleted once it has history.**  An account with posted
  transactions is archived (``is_void``) instead, preserving auditability.
* **Classification is validated against the registry**, never trusted from the
  browser: ``services/account_classification.py`` is the single source of truth
  for the Category → Subcategory → Account Type → Channel hierarchy.
* Routes stay thin — they parse the request, call one function here and flash
  the outcome, so the rules below cannot be bypassed from a view.
"""

from __future__ import annotations

from sqlalchemy import func, or_

from hdc.extensions import db
from hdc.models.accounts import Account, AccountTransaction
from hdc.services.account_classification import (
    CHANNEL_LABELS as _CHANNEL_LABELS,
    ENTITY_LABELS as _ENTITY_LABELS,
    ENTITY_TYPES,
    categories,
    classification_tree,
    default_channel,
    is_valid,
    leaf,
    legacy_entity,
    legacy_to_classification,
    required_entity,
    subcategories,
)
from hdc.services.accounts import (
    _ACCOUNT_COMPANY_TYPES,
    _ACCOUNT_TYPES,
    _account_group_mode_for_row,
    _resolve_account_type,
)
from hdc.utils.format import _flt
from hdc.utils.money import sync_money_fields
from hdc.utils.normalize import _normalize_account_mode, _normalize_name_ci

__all__ = [
    "CHANNEL_LABELS",
    "ENTITY_LABELS",
    "SHOW_MODES",
    "account_classification_for",
    "account_groups",
    "classification_tree_json",
    "create_account",
    "delete_account",
    "list_manage_accounts",
    "manage_summary",
    "restore_account",
    "set_account_status",
    "update_account",
]

CHANNEL_LABELS = _CHANNEL_LABELS
ENTITY_LABELS = _ENTITY_LABELS

# Statuses the *Manage Accounts* filter offers.  ``archived`` covers both rows
# explicitly marked archived and voided rows (HDC archives by setting is_void).
SHOW_MODES = ("active", "inactive", "archived", "all")



# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------

def _balances_map():
    """Derive every account's balance from the ledger (opening + in − out).

    Voided transactions are ignored, matching ``services/accounts.py``.  Done
    as two grouped sub-queries so a 50k-row ledger costs two passes, not one
    query per account.
    """
    outgoing_sq = (db.session.query(
        AccountTransaction.from_account_id.label('account_id'),
        func.coalesce(func.sum(AccountTransaction.amount), 0.0).label('total')
    ).filter(AccountTransaction.is_void == False)  # noqa: E712
     .group_by(AccountTransaction.from_account_id).subquery())

    incoming_sq = (db.session.query(
        AccountTransaction.to_account_id.label('account_id'),
        func.coalesce(func.sum(AccountTransaction.amount), 0.0).label('total')
    ).filter(AccountTransaction.is_void == False,  # noqa: E712
             AccountTransaction.to_account_id.isnot(None))
     .group_by(AccountTransaction.to_account_id).subquery())

    rows = (db.session.query(
                Account.id,
                func.coalesce(incoming_sq.c.total, 0.0),
                func.coalesce(outgoing_sq.c.total, 0.0))
            .outerjoin(incoming_sq, incoming_sq.c.account_id == Account.id)
            .outerjoin(outgoing_sq, outgoing_sq.c.account_id == Account.id)
            .all())

    opening = {int(a.id): float(a.opening_balance or 0.0) for a in Account.query.all()}
    out = {}
    for acc_id, incoming, outgoing in rows:
        acc_id = int(acc_id)
        out[acc_id] = float(opening.get(acc_id, 0.0)
                            + float(incoming or 0.0)
                            - float(outgoing or 0.0))
    return out


def _txn_counts_map():
    """Posted-transaction count per account (either side), for archive safety.

    Shown on the list so the user can see *why* an account cannot be deleted,
    and used by :func:`delete_account` to decide archive-vs-delete.
    """
    from_side = (db.session.query(
        AccountTransaction.from_account_id.label('account_id'),
        func.count(AccountTransaction.id).label('n'))
        .filter(AccountTransaction.is_void == False)  # noqa: E712
        .group_by(AccountTransaction.from_account_id).all())
    to_side = (db.session.query(
        AccountTransaction.to_account_id.label('account_id'),
        func.count(AccountTransaction.id).label('n'))
        .filter(AccountTransaction.is_void == False,  # noqa: E712
                AccountTransaction.to_account_id.isnot(None))
        .group_by(AccountTransaction.to_account_id).all())

    counts: dict[int, int] = {}
    for account_id, n in list(from_side) + list(to_side):
        if account_id is None:
            continue
        counts[int(account_id)] = counts.get(int(account_id), 0) + int(n or 0)
    return counts


def _row_status(acc):
    """Collapse HDC's two archive signals into one display status.

    ``status`` holds active/inactive/archived and ``is_void`` is the archive
    flag set by the delete flow.  A voided row is *archived* regardless of the
    string in ``status``, so the list never shows a deleted account as active.
    """
    if bool(getattr(acc, 'is_void', False)):
        return 'archived'
    return (getattr(acc, 'status', 'active') or 'active').strip().lower() or 'active'


def account_classification_for(acc):
    """The classification triple for an account, filling gaps from legacy type.

    Accounts created before the AMS hierarchy was ported have NULL ``class_*``
    columns; the bootstrap backfill normally fills them, but a row can still be
    bare (e.g. created by an older import).  Deriving the answer on read keeps
    the page correct without ever writing behind the user's back.
    """
    category = (getattr(acc, 'class_category', '') or '').strip()
    subcategory = (getattr(acc, 'class_subcategory', '') or '').strip()
    account_type = (getattr(acc, 'class_account_type', '') or '').strip()
    channel = (getattr(acc, 'channel', '') or '').strip()
    entity = (getattr(acc, 'linked_entity_type', '') or '').strip()

    if not (category and subcategory and account_type):
        _group, mode = _account_group_mode_for_row(acc)
        category, subcategory, account_type = legacy_to_classification(
            (acc.type or '').strip().lower(), is_bank=(mode == 'bank'))
        if not entity:
            entity = legacy_entity((acc.type or '').strip().lower())
    if not channel:
        channel = default_channel(category, subcategory, account_type)
    if not entity:
        entity = 'none'
    return {
        'category': category,
        'subcategory': subcategory,
        'account_type': account_type,
        'channel': channel,
        'entity': entity,
    }


def list_manage_accounts(*, show='active', include_auto_person=True):
    """Every account as a flat dict, ready for the AMS-style list.

    Unlike ``_list_accounts_with_balances`` this includes archived (voided)
    rows when asked, and carries the classification hierarchy, the posted
    transaction count and the display labels the list renders.
    """
    show = (show or 'active').strip().lower()
    if show not in SHOW_MODES:
        show = 'active'

    q = Account.query
    if show == 'active':
        q = q.filter(Account.is_void == False,  # noqa: E712
                     func.lower(func.coalesce(Account.status, 'active')) == 'active')
    elif show == 'inactive':
        q = q.filter(Account.is_void == False,  # noqa: E712
                     func.lower(func.coalesce(Account.status, 'active')) == 'inactive')
    elif show == 'archived':
        q = q.filter(or_(Account.is_void == True,  # noqa: E712
                         func.lower(func.coalesce(Account.status, 'active')) == 'archived'))
    # ``all`` intentionally applies no status filter.

    rows = q.order_by(Account.name.asc(), Account.id.asc()).all()
    balances = _balances_map()
    txn_counts = _txn_counts_map()

    items = []
    for acc in rows:
        group, mode = _account_group_mode_for_row(acc)
        classification = account_classification_for(acc)
        is_auto_person = (acc.type == 'person' and bool(acc.auto_generated))
        if is_auto_person and not include_auto_person:
            continue
        status = _row_status(acc)
        balance = float(balances.get(int(acc.id), 0.0))
        items.append({
            # lets the list template tag the row for the "entered by" lookup
            '_hdc_entity': 'hdc_account',
            'id': int(acc.id),
            'name': acc.name or '',
            'type': (acc.type or '').strip().lower(),
            'account_group': group,
            'account_mode': mode,
            'status': status,
            'opening_balance': float(acc.opening_balance or 0.0),
            'current_balance': balance,
            'bank_name': (acc.bank_name or ''),
            'account_number': (acc.account_number or ''),
            'iban': (acc.iban or ''),
            'auto_generated': bool(acc.auto_generated),
            'auto_source': (acc.auto_source or ''),
            'note': (getattr(acc, 'note', '') or ''),
            'created_at': (acc.created_at.date().isoformat() if acc.created_at else ''),
            'txn_count': int(txn_counts.get(int(acc.id), 0)),
            # classification hierarchy + human labels
            'class_category': classification['category'],
            'class_subcategory': classification['subcategory'],
            'class_account_type': classification['account_type'],
            'channel': classification['channel'],
            'channel_label': CHANNEL_LABELS.get(classification['channel'], classification['channel']),
            'linked_entity_type': classification['entity'],
            'linked_entity_label': ENTITY_LABELS.get(classification['entity'], classification['entity']),
            'linked_party_name': (getattr(acc, 'linked_party_name', '') or ''),
            'cash_location': (getattr(acc, 'cash_location', '') or ''),
            'cash_responsible': (getattr(acc, 'cash_responsible', '') or ''),
            'wallet_provider': (getattr(acc, 'wallet_provider', '') or ''),
            'wallet_number': (getattr(acc, 'wallet_number', '') or ''),
            'wallet_holder': (getattr(acc, 'wallet_holder', '') or ''),
            'is_auto_person': is_auto_person,
        })
    return items


def account_groups(accounts):
    """Chips for the *Transaction Groups* card, grouped by classification.

    AMS groups accounts by a free-text ``source_category``; HDC has the
    controlled Category → Subcategory hierarchy instead, so the chips are built
    from that (in registry order) and only non-empty groups are shown.  Each
    chip carries the running balance of its members, which is what makes the
    card useful rather than decorative.
    """
    buckets: dict[tuple[str, str], dict] = {}
    for a in accounts or []:
        key = (a.get('class_category') or 'Uncategorised',
               a.get('class_subcategory') or 'Other')
        b = buckets.setdefault(key, {
            'category': key[0],
            'subcategory': key[1],
            'count': 0,
            'balance': 0.0,
            'accounts': [],
        })
        b['count'] += 1
        b['balance'] += float(a.get('current_balance') or 0.0)
        b['accounts'].append(a.get('name') or '')

    # Keep registry order so the chips are stable between page loads.
    order = {}
    for cat in categories():
        for i, sub in enumerate(subcategories(cat)):
            order[(cat, sub)] = (list(categories()).index(cat), i)

    def sort_key(item):
        return order.get((item[0], item[1]), (999, 999))

    return [buckets[k] for k in sorted(buckets.keys(), key=sort_key)]


def manage_summary(accounts):
    """Stat-card numbers for the Manage Accounts header.

    Money totals are split by *channel* (the classification field) with a
    fallback to the legacy ``type``, because accounts created before the
    hierarchy was ported may not have a channel set.

    ``negative_count`` deliberately counts **only accounts that hold money**
    (cash / bank / wallet).  A client or supplier ledger running negative is
    ordinary double-entry — an advance received or a credit — so counting it as
    an "overdrawn account" would raise a false alarm on healthy data.  Those
    ledgers are reported separately as ``party_credit_count`` /
    ``party_credit_total``.
    """
    accounts = accounts or []
    total = len(accounts)
    total_balance = sum(float(a.get('current_balance') or 0.0) for a in accounts)

    def _is_money(a):
        return (a.get('type') in _ACCOUNT_COMPANY_TYPES
                or a.get('channel') in ('cash', 'bank', 'digital_wallet'))

    cash_accounts = [a for a in accounts if _is_money(a) and a.get('channel') == 'cash']
    bank_accounts = [a for a in accounts if _is_money(a) and a.get('channel') == 'bank']
    wallet_accounts = [a for a in accounts if _is_money(a) and a.get('channel') == 'digital_wallet']
    # Legacy rows without a channel: fall back to type/account_mode.
    unclassified = [a for a in accounts if _is_money(a) and not a.get('channel')]
    for a in unclassified:
        (bank_accounts if a.get('account_mode') == 'bank' else cash_accounts).append(a)

    company_owned = sum(float(a.get('current_balance') or 0.0) for a in accounts
                        if a.get('type') in _ACCOUNT_COMPANY_TYPES)
    receivable = sum(float(a.get('current_balance') or 0.0) for a in accounts
                     if a.get('type') == 'client' and float(a.get('current_balance') or 0.0) > 0)

    # "Negative balance" only means something for an account that actually
    # holds money.  A client or supplier ledger running negative is normal
    # double-entry — it is an advance received / a credit note, not an
    # overdrawn cash box — so flagging those would cry wolf on every page load.
    money_pots = [a for a in accounts
                  if a.get('channel') in ('cash', 'bank', 'digital_wallet')
                  or (a.get('type') in _ACCOUNT_COMPANY_TYPES and not a.get('channel'))]
    overdrawn = [a for a in money_pots if float(a.get('current_balance') or 0.0) < 0]

    # Party ledgers in credit: money the business effectively owes back or has
    # received in advance.  Reported separately, not as an error.
    party_credit = [a for a in accounts
                    if a.get('channel') == 'ledger_only'
                    and float(a.get('current_balance') or 0.0) < 0]

    return {
        'total_accounts': total,
        'total_balance': total_balance,
        'company_owned': company_owned,
        'cash_count': len(cash_accounts),
        'cash_balance': sum(float(a.get('current_balance') or 0.0) for a in cash_accounts),
        'bank_count': len(bank_accounts),
        'bank_balance': sum(float(a.get('current_balance') or 0.0) for a in bank_accounts),
        'wallet_count': len(wallet_accounts),
        'wallet_balance': sum(float(a.get('current_balance') or 0.0) for a in wallet_accounts),
        'receivable': receivable,
        'negative_count': len(overdrawn),
        'negative_accounts': overdrawn[:8],
        'party_credit_count': len(party_credit),
        'party_credit_total': sum(abs(float(a.get('current_balance') or 0.0)) for a in party_credit),
        'active_count': sum(1 for a in accounts if a.get('status') == 'active'),
        'inactive_count': sum(1 for a in accounts if a.get('status') == 'inactive'),
        'archived_count': sum(1 for a in accounts if a.get('status') == 'archived'),
        'auto_count': sum(1 for a in accounts if a.get('auto_generated')),
        'unclassified_count': sum(1 for a in accounts
                                  if not (a.get('class_account_type') or '').strip()),
    }


def classification_tree_json():
    """The registry projected for the cascading selects on the edit page."""
    return classification_tree()


# ---------------------------------------------------------------------------
# Editing
# ---------------------------------------------------------------------------

def _find_duplicate_name(name, exclude_id=None):
    q = Account.query.filter(
        Account.is_void == False,  # noqa: E712
        func.lower(func.trim(Account.name)) == name.lower())
    if exclude_id:
        q = q.filter(Account.id != int(exclude_id))
    return q.first()


def _validated_account_fields(payload, *, existing=None):
    """Validate + normalise one account payload.

    Shared by :func:`create_account` and :func:`update_account` so the two
    entry points cannot drift apart.  ``existing`` is the row being edited (used
    to fall back to stored values for fields the form left blank).

    Returns ``(fields, error)`` — exactly one of the two is meaningful.
    """
    existing = existing
    name = _normalize_name_ci((payload.get('name')
                               or (existing.name if existing else '') or ''))
    if not name:
        return None, 'Account name is required.'
    if _find_duplicate_name(name, exclude_id=(existing.id if existing else None)):
        return None, 'Another account with this name already exists.'

    # --- legacy group / mode / type -------------------------------------
    # HDC screens still key off ``type`` (company / cash / bank / person /
    # vendor / client), so it is derived from the group + channel rather than
    # taken raw from the browser.
    fallback_group, fallback_mode = _account_group_mode_for_row(existing) if existing else ('company', 'cash')
    acc_group = (payload.get('account_group') or '').strip().lower() or fallback_group
    acc_mode = _normalize_account_mode(payload.get('account_mode') or fallback_mode)
    acc_type = _resolve_account_type(
        group=acc_group,
        mode=acc_mode,
        explicit_type=payload.get('type') or (existing.type if existing else None),
    )
    if acc_type not in _ACCOUNT_TYPES:
        return None, f'Account type must be one of: {", ".join(_ACCOUNT_TYPES)}.'

    # --- classification --------------------------------------------------
    current = account_classification_for(existing) if existing else {
        'category': '', 'subcategory': '', 'account_type': '', 'channel': '', 'entity': ''}
    category = (payload.get('class_category') or '').strip() or current['category']
    subcategory = (payload.get('class_subcategory') or '').strip() or current['subcategory']
    account_type = (payload.get('class_account_type') or '').strip() or current['account_type']
    channel = (payload.get('channel') or '').strip()
    entity = (payload.get('linked_entity_type') or '').strip() or current['entity'] or 'none'

    if not (category and subcategory and account_type):
        # Nothing chosen (or nothing stored): derive from the legacy type so a
        # new account is never left unclassified.
        category, subcategory, account_type = legacy_to_classification(
            acc_type, is_bank=(acc_mode == 'bank'))
    if not channel:
        channel = default_channel(category, subcategory, account_type)

    if not is_valid(category, subcategory, account_type, channel=channel):
        node = leaf(category, subcategory, account_type)
        allowed = ', '.join(node['channels']) if node else 'no such account type'
        return None, (f'Invalid classification: {category} → {subcategory} → {account_type} '
                      f'does not allow channel "{channel}" (allowed: {allowed}).')
    if entity not in ENTITY_TYPES:
        return None, f'Linked entity must be one of: {", ".join(ENTITY_TYPES)}.'

    # The registry declares which entity a classification expects; a leaf that
    # requires a specific link must not be saved as "none".
    # (Named ``expected_entity`` rather than ``required_entity`` so it does not
    # shadow the imported registry function of that name.)
    expected_entity = required_entity(category, subcategory, account_type)
    if expected_entity != 'none' and entity != expected_entity:
        entity = expected_entity

    # --- channel-specific details ---------------------------------------
    # "Key absent from the payload" means *not submitted*, so the stored value
    # is kept; "present but blank" means the user cleared it.  Without this
    # distinction a partial update (the JSON API, or a form that only exposes
    # some fields) would silently wipe a bank account's details — and then fail
    # validation for a field the caller never touched.
    def _detail(key):
        if existing is not None and key not in payload:
            return (getattr(existing, key, '') or '').strip()
        return (payload.get(key) or '').strip()

    bank_name = _normalize_name_ci(_detail('bank_name'))
    account_number = _detail('account_number')
    iban = _detail('iban')
    wallet_provider = _detail('wallet_provider')
    wallet_number = _detail('wallet_number')
    wallet_holder = _detail('wallet_holder')
    cash_location = _detail('cash_location')
    cash_responsible = _detail('cash_responsible')

    # The channel decides which detail fields are meaningful.  Bank details on a
    # cash account (or vice versa) are the classic contradictory combination the
    # registry exists to prevent, so they are required/cleared by channel.
    if channel == 'bank':
        if not bank_name:
            return None, 'Bank name is required for a bank account.'
        if not account_number:
            return None, 'Account number is required for a bank account.'
        acc_mode = 'bank'
        wallet_provider = wallet_number = wallet_holder = ''
        cash_location = cash_responsible = ''
    elif channel == 'digital_wallet':
        if not wallet_provider:
            return None, 'Wallet provider is required for a digital wallet account.'
        acc_mode = 'cash'
        bank_name = account_number = iban = ''
        cash_location = cash_responsible = ''
    elif channel == 'cash':
        acc_mode = 'cash'
        bank_name = account_number = iban = ''
        wallet_provider = wallet_number = wallet_holder = ''
    else:
        # ledger_only / other: no instrument behind the balance.
        acc_mode = 'cash'
        bank_name = account_number = iban = ''
        wallet_provider = wallet_number = wallet_holder = ''
        cash_location = cash_responsible = ''

    # Keep the legacy ``type`` consistent with the channel that was chosen, so
    # older screens (which branch on type) agree with the new hierarchy.
    if channel == 'bank' and acc_type in _ACCOUNT_COMPANY_TYPES:
        acc_type = 'bank'
    elif channel == 'cash' and acc_type == 'bank':
        acc_type = 'cash'

    linked_party_name = _detail('linked_party_name')[:160]
    if entity == 'none':
        linked_party_name = ''

    opening = _flt(payload.get('opening_balance'),
                   (existing.opening_balance if existing else 0.0))

    return {
        'name': name,
        'type': acc_type,
        'account_group': acc_group,
        'account_mode': acc_mode,
        'opening_balance': float(opening or 0.0),
        'bank_name': bank_name or None,
        'account_number': account_number or None,
        'iban': iban or None,
        'class_category': category,
        'class_subcategory': subcategory,
        'class_account_type': account_type,
        'channel': channel,
        'linked_entity_type': entity,
        'linked_party_name': linked_party_name or None,
        'cash_location': cash_location or None,
        'cash_responsible': cash_responsible or None,
        'wallet_provider': wallet_provider or None,
        'wallet_number': wallet_number or None,
        'wallet_holder': wallet_holder or None,
        'note': _detail('note')[:500] or None,
    }, None


def _apply_account_fields(row, fields):
    """Write validated fields onto an ``Account`` row (no commit)."""
    for key, value in fields.items():
        if key in ('account_group', 'account_mode'):
            continue  # derived, not stored
        setattr(row, key, value)
    sync_money_fields(row, 'opening_balance', 'opening_balance_minor')
    return row


def create_account(payload):
    """Create an account from the full Add Account form.

    Returns ``(row, message)``.  Uses the same validator as :func:`update_account`
    so a created account is always validly classified.
    """
    fields, error = _validated_account_fields(payload, existing=None)
    if error:
        return None, error
    row = Account(
        status='active',
        auto_generated=False,
        is_void=False,
    )
    _apply_account_fields(row, fields)
    db.session.add(row)
    db.session.commit()
    return row, f'Account created: {row.name}.'


def update_account(account_id, payload):
    """Save the account master row (details + classification).

    Returns ``(row, message)``; ``row`` is ``None`` on failure.  Validation
    mirrors the create flow so the two entry points cannot disagree, and
    validates the classification against the registry — an illegal
    Category/Subcategory/Type/Channel combination is rejected rather than
    silently stored.
    """
    row = db.session.get(Account, int(account_id)) if account_id else None
    if not row:
        return None, 'Account not found.'

    fields, error = _validated_account_fields(payload, existing=row)
    if error:
        return None, error

    _apply_account_fields(row, fields)
    db.session.commit()
    return row, f'Account updated: {row.name}.'


def set_account_status(account_id, status):
    """Suspend (inactive) or reactivate (active) an account.

    Archived/voided rows are left alone: reactivating an archived account would
    quietly un-delete history, which must be a deliberate admin action
    (:func:`restore_account`) rather than a side effect of a status toggle.
    """
    row = db.session.get(Account, int(account_id)) if account_id else None
    if not row:
        return None, 'Account not found.'
    if row.is_void:
        return None, 'This account is archived. Restore it before changing its status.'
    target = (status or '').strip().lower()
    if target not in ('active', 'inactive'):
        return None, 'Status must be "active" or "inactive".'
    row.status = target
    db.session.commit()
    verb = 'activated' if target == 'active' else 'suspended'
    return row, f'Account {verb}: {row.name}.'


def restore_account(account_id):
    """Un-archive an account (the inverse of :func:`delete_account`).

    Archiving is reversible on purpose: an account archived by mistake, or one
    whose project resumes, should not have to be recreated — recreating it
    would start a *second* ledger with a fresh opening balance and silently
    split its history.  Restoring clears ``is_void`` and returns it to active.

    The name uniqueness check is re-run first, because another account may have
    taken the name while this one was archived; restoring then would create two
    live accounts with the same name.
    """
    row = db.session.get(Account, int(account_id)) if account_id else None
    if not row:
        return None, 'Account not found.'
    if not row.is_void:
        return row, 'This account is not archived.'
    if _find_duplicate_name(row.name or '', exclude_id=row.id):
        return None, (f'Cannot restore: another active account is already named '
                      f'"{row.name}". Rename one of them first.')
    row.is_void = False
    row.status = 'active'
    db.session.commit()
    return row, f'Account restored: {row.name}. It is active again and its ledger history is intact.'


def delete_account(account_id):
    """Delete an unused account, or archive it when it carries history.

    Mirrors the AMS rule (``accounts_crud.delete_account``): an account with
    posted transactions is never removed, because that would orphan ledger rows
    and break every report that reads them.  Returns ``(row, message, archived)``.
    """
    row = db.session.get(Account, int(account_id)) if account_id else None
    if not row:
        return None, 'Account not found.', False
    if row.is_void:
        return None, 'This account is already archived.', True

    has_txn = db.session.query(AccountTransaction.id).filter(
        AccountTransaction.is_void == False,  # noqa: E712
        or_(AccountTransaction.from_account_id == row.id,
            AccountTransaction.to_account_id == row.id)
    ).first() is not None

    if has_txn:
        row.status = 'archived'
        row.is_void = True
        db.session.commit()
        return row, ('Account has posted transactions, so it was archived instead of '
                     'deleted. Its ledger history is preserved.'), True

    name = row.name
    db.session.delete(row)
    db.session.commit()
    return None, f'Account deleted: {name}.', False
