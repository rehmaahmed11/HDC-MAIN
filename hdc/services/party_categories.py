"""User-defined categories for the Parties directory.

The Parties page used to file a party by mapping its ``party_type`` to one of
six buckets compiled into ``hdc.services.parties``.  The field asked for the
opposite: **create a category, name it anything, then file parties into it** —
and be able to grow the list later without a deploy.

So the buckets are now ordinary rows in ``hdc_party_category``:

* the six shipped buckets are *seeded* once (on an empty table) so an existing
  directory does not lose its filing, but they are editable rows like any
  other;
* :func:`save_party_category` creates a new one from the Parties page;
* :func:`bulk_assign_category` moves any number of parties into a category
  (or clears the filing, which falls back to the type-derived bucket);
* the KPI cards / filter dropdown are built from
  :func:`list_party_categories`, never from a constant.

``party_type`` is left untouched: it still drives behaviour (loan pickers,
HDC Tools customers, project owners).  Category is purely the operator's
grouping and can be anything they like.

Nothing here imports ``hdc.services.parties`` at module level: the legacy
type→bucket map lives there and ``parties`` imports this module, so the one
lazy import inside :func:`default_party_category_for_type` breaks the cycle.
"""

import re

from sqlalchemy import func

from hdc.core.flags import _runtime_flag_get, _runtime_flag_set
from hdc.extensions import db
from hdc.models.cashflow import CashFlowParty, PartyCategory
from hdc.utils.normalize import _normalize_name_ci

#: A stable flag so a fresh install seeds once and an operator who deletes
#: every category is never overruled by a later restart.
SEED_FLAG = 'party_category_seed_v1'

#: ``(key, name, icon, colour, description)`` — the buckets the directory
#: shipped with, now just the initial rows of the table.  Their keys match the
#: legacy ``?category=`` values so old links and saved filters keep working.
DEFAULT_PARTY_CATEGORIES = (
    ('loan', 'Loan Parties', 'fa-money-bill-transfer', 'kpi-blue',
     'Lenders and borrowers — the people the loan Money In / Out categories offer.'),
    ('rental', 'External Customers', 'fa-screwdriver-wrench', 'kpi-orange',
     'HDC Tools rental customers — an external rental syncs its name here.'),
    ('worker', 'Workers', 'fa-hard-hat', 'kpi-green',
     'Workers synced from the Workers module (advances, payments, tips).'),
    ('client', 'Clients / Owners', 'fa-building', 'kpi-indigo',
     'Project owners and clients — the people whose projects we build.'),
    ('supplier', 'Suppliers / Vendors', 'fa-truck-field', 'kpi-slate',
     'Material, tool and service suppliers we buy from.'),
    ('other', 'Other Parties', 'fa-users', 'kpi-purple',
     'Office staff, subcontractors and everyone not filed elsewhere.'),
)

#: Icons offered by the category form.  Any Font Awesome class still renders
#: if an API caller passes one, but the UI keeps to this palette.
PARTY_CATEGORY_ICONS = (
    ('fa-tag', 'Tag'),
    ('fa-folder', 'Folder'),
    ('fa-money-bill-transfer', 'Loan / transfer'),
    ('fa-screwdriver-wrench', 'Tools / rental'),
    ('fa-hard-hat', 'Workers'),
    ('fa-building', 'Clients / owners'),
    ('fa-truck-field', 'Suppliers'),
    ('fa-users', 'People'),
    ('fa-user-tie', 'Staff'),
    ('fa-handshake', 'Partners'),
    ('fa-diagram-project', 'Projects'),
    ('fa-wallet', 'Wallet'),
    ('fa-warehouse', 'Store'),
)

#: KPI card colours already styled in ``hdc.css``.
PARTY_CATEGORY_COLOURS = (
    ('kpi-blue', 'Blue'),
    ('kpi-teal', 'Teal'),
    ('kpi-green', 'Green'),
    ('kpi-orange', 'Orange'),
    ('kpi-indigo', 'Indigo'),
    ('kpi-slate', 'Slate'),
    ('kpi-purple', 'Purple'),
    ('kpi-red', 'Red'),
)

VALID_ICONS = tuple(value for value, _label in PARTY_CATEGORY_ICONS)
VALID_COLOURS = tuple(value for value, _label in PARTY_CATEGORY_COLOURS)


# ── small helpers ────────────────────────────────────────────────────────────

def _clean_text(value, limit):
    return (_normalize_name_ci(value) or '')[:limit]


def _slugify(name):
    """URL-safe base for a new category key (ASCII; falls back to ``category``)."""
    raw = (_normalize_name_ci(name) or '').lower()
    slug = re.sub(r'[^a-z0-9]+', '-', raw).strip('-')
    if not slug:
        return 'category'
    if slug.isdigit():
        slug = f'category-{slug}'
    return slug[:40]


def _unique_key(name):
    base = _slugify(name)
    key = base
    n = 2
    while (PartyCategory.query
           .filter(func.lower(PartyCategory.key) == key.lower()).first()):
        key = f'{base}-{n}'
        n += 1
    return key


def _normalise_icon(icon):
    value = (icon or '').strip()
    return value if value.startswith('fa-') and len(value) <= 60 else 'fa-tag'


def _normalise_colour(colour):
    value = (colour or '').strip().lower()
    return value if value in VALID_COLOURS else 'kpi-slate'


def _name_taken(name, exclude_id=None):
    q = PartyCategory.query.filter(
        func.lower(func.trim(PartyCategory.name)) == name.lower())
    if exclude_id:
        q = q.filter(PartyCategory.id != int(exclude_id))
    return q.first() is not None


# ── reads ────────────────────────────────────────────────────────────────────

def list_party_categories(*, active_only=False):
    """Every category in display order (KPI cards, filters and forms)."""
    q = PartyCategory.query
    if active_only:
        q = q.filter(PartyCategory.is_active == True)  # noqa: E712
    return (q.order_by(PartyCategory.sort_order.asc(),
                       func.lower(PartyCategory.name).asc())
            .all())


def category_by_id(category_id):
    try:
        cid = int(category_id or 0)
    except (TypeError, ValueError):
        return None
    return db.session.get(PartyCategory, cid) if cid else None


def category_by_key(key):
    raw = (key or '').strip().lower()
    if not raw:
        return None
    return (PartyCategory.query
            .filter(func.lower(func.trim(PartyCategory.key)) == raw)
            .first())


def default_party_category_for_type(party_type):
    """The seeded category whose key matches a party type's legacy bucket.

    Returns ``None`` when that bucket has been deleted, which simply leaves
    the party unfiled instead of resurrecting a category the operator removed.
    """
    from hdc.services.parties import party_category  # cycle-breaker
    return category_by_key(party_category(party_type))


def effective_party_category(party, *, by_id=None, by_key=None):
    """The category a party is *filed under*, explicit assignment first.

    ``by_id`` / ``by_key`` let the directory resolve hundreds of rows without
    a query per party.
    """
    if party is None:
        return None
    cat = None
    if party.category_id:
        if by_id is not None:
            cat = by_id.get(int(party.category_id))
        else:
            cat = category_by_id(party.category_id)
    if cat is not None:
        return cat
    fallback = default_party_category_for_type(getattr(party, 'party_type', 'other'))
    if fallback is None:
        return None
    if by_key is not None:
        return by_key.get(fallback.key)
    return fallback


def category_maps():
    """``(by_id, by_key)`` for the bulk reads in the directory."""
    rows = list_party_categories()
    return ({int(c.id): c for c in rows},
            {(c.key or '').strip().lower(): c for c in rows})


def category_usage_counts():
    """``{category_id: active_party_count}`` — used by the manager card."""
    rows = (db.session.query(CashFlowParty.category_id,
                             func.count(CashFlowParty.id))
            .filter(CashFlowParty.category_id.isnot(None))
            .group_by(CashFlowParty.category_id)
            .all())
    return {int(cid): int(count) for cid, count in rows if cid}


# ── seed + backfill ──────────────────────────────────────────────────────────

def ensure_party_category_seed():
    """Create the six shipped buckets the first time this database boots.

    Never touches an existing category: a rename, recolour or deletion is an
    operator decision.  The runtime flag makes sure an operator who deletes
    every row does not get the defaults back on the next restart.
    """
    try:
        if _runtime_flag_get(SEED_FLAG) == '1':
            return 0
    except Exception:
        pass
    created = 0
    if PartyCategory.query.first() is None:
        for order, (key, name, icon, colour, description) in enumerate(
                DEFAULT_PARTY_CATEGORIES):
            db.session.add(PartyCategory(
                key=key, name=name, icon=icon, colour=colour,
                description=description[:300], sort_order=order,
                is_active=True,
            ))
            created += 1
        db.session.commit()
    try:
        _runtime_flag_set(SEED_FLAG, '1')
    except Exception:
        pass
    return created


def backfill_party_categories(*, commit=True):
    """File every party that has no category yet under its type's bucket.

    Runs at boot (and is safe to re-run): only ``category_id IS NULL`` rows are
    touched, so an explicit assignment is never overwritten.  This is what
    makes an upgraded database open with the same grouping it had before.
    """
    by_key = {(c.key or '').strip().lower(): c for c in list_party_categories()}
    if not by_key:
        return 0
    from hdc.services.parties import party_category  # cycle-breaker
    updated = 0
    for party in CashFlowParty.query.filter(CashFlowParty.category_id.is_(None)).all():
        cat = by_key.get(party_category(party.party_type))
        if cat is not None:
            party.category_id = int(cat.id)
            party.category_auto = True
            updated += 1
    if updated:
        db.session.flush()
        if commit:
            db.session.commit()
    return updated


def ensure_party_has_category(party):
    """Keep an **automatic** filing in step with the party's type.

    Called after a party is created or its type changes:

    * unfiled → file under the type's bucket and mark it automatic;
    * auto-filed → follow the new type's bucket (``other`` → ``supplier``
      must move *Other Parties* → *Suppliers / Vendors*);
    * filed by hand (``category_auto`` false) → never touch it.
    """
    if party is None:
        return party
    cat = default_party_category_for_type(getattr(party, 'party_type', 'other'))
    if cat is None:
        return party
    if party.category_id is None:
        party.category_id = int(cat.id)
        party.category_auto = True
    elif party.category_auto is not False:
        party.category_id = int(cat.id)
    return party


# ── writes ───────────────────────────────────────────────────────────────────

def save_party_category(name, *, description=None, icon=None, colour=None,
                        sort_order=None, commit=True):
    """Create a category named anything the operator likes."""
    nm = _clean_text(name, 120)
    if not nm:
        raise ValueError('Category name is required.')
    if _name_taken(nm):
        raise ValueError(f'A category named "{nm}" already exists.')
    if sort_order is None:
        last = (PartyCategory.query
                .order_by(PartyCategory.sort_order.desc()).first())
        sort_order = int(getattr(last, 'sort_order', 0) or 0) + 1
    row = PartyCategory(
        key=_unique_key(nm),
        name=nm,
        description=_clean_text(description, 300) or None,
        icon=_normalise_icon(icon),
        colour=_normalise_colour(colour),
        sort_order=int(sort_order or 0),
        is_active=True,
    )
    db.session.add(row)
    db.session.flush()
    if commit:
        db.session.commit()
    return row


def update_party_category(category, *, name=None, description=None, icon=None,
                          colour=None, is_active=None, sort_order=None,
                          commit=True):
    """Rename / recolour / retire a category.  The key never changes.

    Keeping the key stable is what lets a rename keep every
    ``/hdc/parties?category=...`` link and saved filter working.
    """
    if category is None:
        raise ValueError('Category not found.')
    if name is not None:
        nm = _clean_text(name, 120)
        if not nm:
            raise ValueError('Category name is required.')
        if _name_taken(nm, exclude_id=category.id):
            raise ValueError(f'A category named "{nm}" already exists.')
        category.name = nm
    if description is not None:
        category.description = _clean_text(description, 300) or None
    if icon is not None:
        category.icon = _normalise_icon(icon)
    if colour is not None:
        category.colour = _normalise_colour(colour)
    if is_active is not None:
        category.is_active = bool(is_active)
    if sort_order is not None:
        category.sort_order = int(sort_order)
    db.session.flush()
    if commit:
        db.session.commit()
    return category


def set_party_category_active(category, active, *, commit=True):
    return update_party_category(category, is_active=bool(active), commit=commit)


def delete_party_category(category, *, commit=True):
    """Remove an unused category.

    A category with parties in it is refused — hiding it keeps those parties'
    filing visible and is one click away, so nothing is silently orphaned.
    """
    if category is None:
        raise ValueError('Category not found.')
    in_use = (CashFlowParty.query
              .filter(CashFlowParty.category_id == int(category.id))
              .count())
    if in_use:
        raise ValueError(
            f'"{category.name}" still has {in_use} party(ies) filed under it. '
            'Move them to another category first, or use Hide to retire it.')
    db.session.delete(category)
    db.session.flush()
    if commit:
        db.session.commit()
    return True


def assign_party_category(party, category, *, commit=True):
    """File one party under *category*; ``None`` returns it to Automatic.

    Either way this is a human decision, so ``category_auto`` follows it: a
    later type change must not undo what the operator chose.
    """
    if party is None:
        raise ValueError('Party not found.')
    party.category_id = int(category.id) if category is not None else None
    party.category_auto = category is None
    db.session.flush()
    if commit:
        db.session.commit()
    return party


def bulk_assign_category(party_ids, category, *, commit=True):
    """Move every selected party into *category* (or clear with ``None``).

    Returns ``(updated, missing)``.  Unknown ids are counted, not fatal: the
    directory may have been refreshed in another tab between the tick and the
    submit, and losing the whole assignment because one row vanished is worse
    than skipping it.
    """
    wanted = []
    for raw in party_ids or []:
        try:
            pid = int(raw)
        except (TypeError, ValueError):
            continue
        if pid and pid not in wanted:
            wanted.append(pid)
    if not wanted:
        raise ValueError('Select at least one party to assign.')
    parties = CashFlowParty.query.filter(CashFlowParty.id.in_(wanted)).all()
    found = {int(p.id) for p in parties}
    category_id = int(category.id) if category is not None else None
    updated = 0
    for party in parties:
        was_auto = party.category_auto is not False
        if party.category_id != category_id or was_auto != (category is None):
            party.category_id = category_id
            party.category_auto = category is None
            updated += 1
    db.session.flush()
    if commit:
        db.session.commit()
    return updated, len([pid for pid in wanted if pid not in found])
