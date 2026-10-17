"""HDC services.material_categories — item types for the Purchase section.

The field asked to see Purchase / Delivery / Usage *on item basis* and to have
a category on every item and every purchase — Electric Materials, Plumbing
Materials, Cement, Steel …  This module owns that vocabulary and the one rule
the whole section shares:

    a purchase line's category is the line's own category if it was set,
    otherwise the category of its material.

Every register, filter and total in the Purchase section reads a category
through the condition helpers below (``_purchase_category_condition``,
``_delivery_category_condition``, ``_usage_category_condition``) so the pages
can never disagree about what belongs to a category.

Two conveniences are built in on purpose:

* a material (or purchase) with **no** category can be filtered for by passing
  category id ``0`` — "Uncategorized" — so legacy rows stay reachable and can
  be assigned in one click;
* the default vocabulary is seeded once (``_ensure_material_category_seed``)
  with the names the field listed, and never overwrites a rename.
"""

from sqlalchemy import and_, false, func, or_

from hdc.extensions import db
from hdc.models.materials import Delivery, MaterialCategory, MaterialV2, PurchaseV2, UsageLogV2
from hdc.utils.dates import _pkt_now_naive

#: The category names the field listed, plus the ones a construction store
#: cannot be run without.  Seeded only while the table is still empty.
DEFAULT_MATERIAL_CATEGORIES = (
    'Cement',
    'Steel',
    'Electric Materials',
    'Plumbing Materials',
    'Bricks & Blocks',
    'Sand & Aggregate',
    'Tiles & Marble',
    'Paint & Finishing',
    'Wood & Timber',
    'Waterproofing & Chemicals',
    'Hardware & Fasteners',
    'Glass & Aluminium',
    'Fuel & Consumables',
    'Miscellaneous',
)

#: Filter value that means "no category yet" — see the module docstring.
UNCATEGORIZED_ID = 0
UNCATEGORIZED_LABEL = 'Uncategorized'


# ── the vocabulary ──────────────────────────────────────────────────────────

def _material_category_query(include_void=False):
    query = MaterialCategory.query
    if not include_void:
        query = query.filter(MaterialCategory.is_void == False)
    return query


def _material_category_rows(include_inactive=False, include_void=False):
    query = _material_category_query(include_void=include_void)
    if not include_inactive:
        query = query.filter(func.lower(func.coalesce(MaterialCategory.status, 'active')) == 'active')
    return query.order_by(MaterialCategory.name.asc()).all()


def _material_category_options(include_inactive=False):
    """Every category, for the ``<select>`` filters and forms."""
    return _material_category_rows(include_inactive=include_inactive)


def _material_category_map(include_void=True):
    return {int(c.id): c for c in _material_category_query(include_void=include_void).all()}


def _material_category_by_id(category_id, active_only=False):
    """The category row for an id.

    ``active_only`` is what the write paths use: a category that was set
    inactive (because records still point at it) stays readable and stays on
    those records, but nothing new may be filed under it.
    """
    if category_id in (None, ''):
        return None
    try:
        cid = int(category_id)
    except (TypeError, ValueError):
        return None
    if cid <= 0:
        return None
    row = _material_category_query(include_void=True).filter(MaterialCategory.id == cid).first()
    if row and active_only and ((row.status or 'active').strip().lower() != 'active' or row.is_void):
        return None
    return row


def _material_category_by_name(name):
    """The category row carrying this name, case- and space-insensitively.

    A **live** row always wins over a voided one with the same name.  The
    unique index only covers live rows (``WHERE is_void = 0``), so a voided
    "Tile" and a live "tile" can legitimately sit side by side; without the
    ordering below the voided row could be picked, revived, and then collide
    with the live one on commit.
    """
    text = (name or '').strip()
    if not text:
        return None
    return (_material_category_query(include_void=True)
            .filter(func.lower(func.trim(MaterialCategory.name)) == text.lower())
            .order_by(MaterialCategory.is_void.asc(), MaterialCategory.id.asc())
            .first())


def _ensure_material_category(name, *, commit=False):
    """Find or create a category by name (case-insensitive), reviving voids.

    A live row with the same name is always reused; a voided row is only
    revived when no live row carries that name, so this can never create two
    live rows with the same name (the unique index would refuse the commit).
    """
    text = (name or '').strip()
    if not text:
        return None
    row = _material_category_by_name(text)
    if row:
        # A live row with this name must win: reviving a voided one while a
        # live one exists would leave two live rows with the same name, and
        # the unique index would refuse the commit.
        live = (_material_category_query(include_void=False)
                .filter(func.lower(func.trim(MaterialCategory.name)) == text.lower())
                .first())
        if live and int(live.id) != int(row.id):
            return live
        changed = False
        if row.is_void:
            row.is_void = False
            changed = True
        if (row.status or 'active').strip().lower() != 'active':
            row.status = 'active'
            changed = True
        if changed:
            row.updated_at = _pkt_now_naive()
        return row
    row = MaterialCategory(name=text, status='active', is_void=False,
                           created_at=_pkt_now_naive(), updated_at=_pkt_now_naive())
    db.session.add(row)
    db.session.flush()
    if commit:
        db.session.commit()
    return row


def _material_category_for_write(category_id, *, current_category_id=None):
    """The category a write path is allowed to file a record under.

    * an **active** category is always allowed;
    * a category that was set **inactive** (because records still point at it)
      may only be *kept* — never newly chosen, so nothing new is filed under a
      retired item type;
    * a **voided** category is never allowed.

    ``current_category_id`` is the record's own category before the edit: it
    is the one inactive category that may survive an edit, so saving an
    unrelated field never silently re-files the record.
    """
    if category_id in (None, '', 0, '0'):
        return None
    try:
        cid = int(category_id)
    except (TypeError, ValueError):
        return None
    if cid <= 0:
        return None
    row = _material_category_query(include_void=True).filter(MaterialCategory.id == cid).first()
    if not row or row.is_void:
        return None
    is_active = (row.status or 'active').strip().lower() == 'active'
    if is_active:
        return row
    if current_category_id and int(current_category_id) == cid:
        return row      # the record already sits here — keeping it is safe
    return None


def _ensure_material_category_seed():
    """Seed the default vocabulary once.  Never touches an operator's list."""
    if _material_category_query(include_void=True).count() > 0:
        return 0
    created = 0
    for name in DEFAULT_MATERIAL_CATEGORIES:
        if _ensure_material_category(name):
            created += 1
    db.session.commit()
    return created


def _material_category_label(category_id, category_map=None):
    if not category_id:
        return UNCATEGORIZED_LABEL
    row = (category_map or {}).get(int(category_id)) or _material_category_by_id(category_id)
    return (row.name if row else UNCATEGORIZED_LABEL)


def _material_category_choices(include_inactive=False):
    """Plain dicts for the templates' filter selects."""
    return [{'id': int(c.id), 'name': c.name, 'status': (c.status or 'active')}
            for c in _material_category_options(include_inactive=include_inactive)]


# ── resolving a category ────────────────────────────────────────────────────

def material_category_id(material):
    """The category id of a material row (None when it has none)."""
    category_id = getattr(material, 'category_id', None)
    return int(category_id) if category_id else None


def purchase_resolved_category_id(purchase, material=None):
    """A purchase line's category: its own choice first, else its material's."""
    if purchase is None:
        return None
    if getattr(purchase, 'category_id', None):
        return int(purchase.category_id)
    material = material if material is not None else getattr(purchase, 'material', None)
    return material_category_id(material)


def purchase_resolved_category_name(purchase, material=None, category_map=None):
    return _material_category_label(purchase_resolved_category_id(purchase, material=material),
                                    category_map=category_map)


def delivery_resolved_category_id(delivery, purchase=None, material=None):
    if delivery is None:
        return None
    purchase = purchase if purchase is not None else getattr(delivery, 'purchase', None)
    if purchase is not None:
        return purchase_resolved_category_id(purchase, material=material)
    return material_category_id(material if material is not None else getattr(delivery, 'material', None))


# ── SQL conditions every register filters through ───────────────────────────

def _material_ids_in_category(category_id):
    cid = int(category_id or 0)
    query = db.session.query(MaterialV2.id)
    if cid == UNCATEGORIZED_ID:
        query = query.filter(MaterialV2.category_id.is_(None))
    else:
        query = query.filter(MaterialV2.category_id == cid)
    return [int(r[0]) for r in query.all()]


def _purchase_category_condition(category_id):
    """SQL condition: the purchase lines that belong to a category.

    The line's own category wins; a line that never set one inherits its
    material's.  ``UNCATEGORIZED_ID`` selects the lines where neither is set.
    """
    cid = int(category_id or 0)
    if cid == UNCATEGORIZED_ID:
        return and_(PurchaseV2.category_id.is_(None),
                    PurchaseV2.material_id.in_(_material_ids_in_category(UNCATEGORIZED_ID) or [-1]))
    material_ids = _material_ids_in_category(cid)
    conditions = [PurchaseV2.category_id == cid]
    if material_ids:
        conditions.append(and_(PurchaseV2.category_id.is_(None),
                               PurchaseV2.material_id.in_(material_ids)))
    return or_(*conditions)


def _purchase_ids_in_category(category_id):
    rows = (db.session.query(PurchaseV2.id)
            .filter(_purchase_category_condition(category_id))
            .all())
    return [int(r[0]) for r in rows]


def _material_category_condition(category_id, model=MaterialV2):
    cid = int(category_id or 0)
    if cid == UNCATEGORIZED_ID:
        return model.category_id.is_(None)
    return model.category_id == cid


def _no_linked_purchase(model):
    return or_(model.purchase_id.is_(None), model.purchase_id == 0)


def _delivery_category_condition(category_id):
    """SQL condition: deliveries whose stock came from a category.

    A delivery is scoped by the purchase order it drew from — that is where the
    category was decided.  A delivery with no purchase order falls back to its
    material's category.
    """
    purchase_ids = _purchase_ids_in_category(category_id)
    material_ids = _material_ids_in_category(category_id)
    conditions = []
    if purchase_ids:
        conditions.append(Delivery.purchase_id.in_(purchase_ids))
    if material_ids:
        conditions.append(and_(_no_linked_purchase(Delivery),
                               Delivery.material_id.in_(material_ids)))
    return or_(*conditions) if conditions else false()


def _usage_category_condition(category_id):
    purchase_ids = _purchase_ids_in_category(category_id)
    material_ids = _material_ids_in_category(category_id)
    conditions = []
    if purchase_ids:
        conditions.append(UsageLogV2.purchase_id.in_(purchase_ids))
    if material_ids:
        conditions.append(and_(_no_linked_purchase(UsageLogV2),
                               UsageLogV2.material_id.in_(material_ids)))
    return or_(*conditions) if conditions else false()


# ── category-wise totals (the "all categories, and each one separately" view) ─

def _material_category_summary_rows(include_inactive=True):
    """Per-category totals for the categories page and the item-wise register.

    Reads the same rule the filters use, in Python, from three flat queries —
    cheap enough for a store-sized ERP and impossible to disagree with the
    registers.
    """
    categories = _material_category_query(include_void=False).all()
    category_map = {int(c.id): c for c in categories}
    materials = MaterialV2.query.filter(MaterialV2.is_void == False).all()
    material_category = {int(m.id): material_category_id(m) for m in materials}

    summary = {}
    for cid, row in category_map.items():
        summary[cid] = {
            'id': cid,
            'row': row,
            'name': row.name,
            'status': (row.status or 'active'),
            'material_count': 0,
            'material_names': [],
            'purchase_count': 0,
            'purchased_qty': 0.0,
            'purchased_value': 0.0,
            'delivered_qty': 0.0,
            'delivered_value': 0.0,
            'used_qty': 0.0,
            'used_value': 0.0,
            'site_ids': set(),
            'supplier_ids': set(),
            'last_purchase_date': None,
        }
    uncategorized = {
        'id': UNCATEGORIZED_ID,
        'row': None,
        'name': UNCATEGORIZED_LABEL,
        'status': 'active',
        'material_count': 0,
        'material_names': [],
        'purchase_count': 0,
        'purchased_qty': 0.0,
        'purchased_value': 0.0,
        'delivered_qty': 0.0,
        'delivered_value': 0.0,
        'used_qty': 0.0,
        'used_value': 0.0,
        'site_ids': set(),
        'supplier_ids': set(),
        'last_purchase_date': None,
    }

    def bucket(category_id):
        if category_id and int(category_id) in summary:
            return summary[int(category_id)]
        return uncategorized

    for m in materials:
        row = bucket(material_category.get(int(m.id)))
        row['material_count'] += 1
        if len(row['material_names']) < 12:
            row['material_names'].append(m.name)

    purchase_unit_price = {}
    purchase_category = {}
    purchases = db.session.query(
        PurchaseV2.id, PurchaseV2.supplier_id, PurchaseV2.material_id,
        PurchaseV2.category_id, PurchaseV2.unit_price, PurchaseV2.quantity,
        PurchaseV2.total_amount, PurchaseV2.date
    ).filter(PurchaseV2.is_void == False).all()
    for pid, supplier_id, material_id, purchase_category_id, unit_price, quantity, total_amount, date_value in purchases:
        resolved = int(purchase_category_id) if purchase_category_id else material_category.get(int(material_id or 0))
        purchase_unit_price[int(pid)] = float(unit_price or 0.0)
        purchase_category[int(pid)] = resolved
        row = bucket(resolved)
        row['purchase_count'] += 1
        row['purchased_qty'] += float(quantity or 0.0)
        row['purchased_value'] += float(total_amount or 0.0)
        if supplier_id:
            row['supplier_ids'].add(int(supplier_id))
        if date_value and (row['last_purchase_date'] is None or date_value > row['last_purchase_date']):
            row['last_purchase_date'] = date_value

    deliveries = db.session.query(
        Delivery.purchase_id, Delivery.material_id, Delivery.project_id, Delivery.quantity
    ).filter(Delivery.is_void == False).all()
    # A delivery is attributed to the item type of the purchase it came from.
    # A purchase that was deleted still names the type the stock was bought
    # under, so its deliveries must not fall out of every filter.
    purchase_category_all = dict(purchase_category)
    for pid, material_id, purchase_category_id, unit_price in db.session.query(
        PurchaseV2.id, PurchaseV2.material_id, PurchaseV2.category_id, PurchaseV2.unit_price
    ).filter(PurchaseV2.is_void.is_(True)).all():
        purchase_category_all.setdefault(
            int(pid),
            int(purchase_category_id) if purchase_category_id
            else material_category.get(int(material_id or 0)))
        purchase_unit_price.setdefault(int(pid), float(unit_price or 0.0))
    for purchase_id, material_id, project_id, quantity in deliveries:
        resolved = (purchase_category_all.get(int(purchase_id or 0)) if purchase_id
                    else material_category.get(int(material_id or 0)))
        row = bucket(resolved)
        qty = float(quantity or 0.0)
        row['delivered_qty'] += qty
        row['delivered_value'] += qty * purchase_unit_price.get(int(purchase_id or 0), 0.0)
        if project_id:
            row['site_ids'].add(int(project_id))

    usages = db.session.query(
        UsageLogV2.purchase_id, UsageLogV2.material_id, UsageLogV2.project_id,
        UsageLogV2.quantity, UsageLogV2.cost
    ).filter(UsageLogV2.is_void == False).all()
    for purchase_id, material_id, project_id, quantity, cost in usages:
        resolved = (purchase_category_all.get(int(purchase_id or 0)) if purchase_id
                    else material_category.get(int(material_id or 0)))
        row = bucket(resolved)
        row['used_qty'] += float(quantity or 0.0)
        row['used_value'] += float(cost or 0.0)
        if project_id:
            row['site_ids'].add(int(project_id))

    rows = [summary[cid] for cid in sorted(summary, key=lambda i: summary[i]['name'].lower())]
    rows = [r for r in rows if include_inactive or r['status'] == 'active']
    # The site / supplier counts must be on **every** row: they are what the
    # categories page shows next to each item type, and they used to be
    # written only when the Uncategorized bucket happened to be empty — so a
    # single uncategorised item silently reported "0 sites, 0 suppliers" for
    # every real category.
    for r in rows:
        r['site_count'] = len(r['site_ids'])
        r['supplier_count'] = len(r['supplier_ids'])
    if uncategorized['material_count'] or uncategorized['purchase_count']:
        uncategorized['site_ids'] = sorted(uncategorized['site_ids'])
        uncategorized['supplier_ids'] = sorted(uncategorized['supplier_ids'])
        uncategorized['site_count'] = len(uncategorized['site_ids'])
        uncategorized['supplier_count'] = len(uncategorized['supplier_ids'])
        return rows + [uncategorized]
    return rows


def _material_category_usage_counts():
    """``{category_id: {'materials': n, 'purchases': n}}`` — used before delete.

    A category that is in use is never deleted (its rows would lose their item
    type); the route sets it inactive instead, exactly like a material with
    transactions.
    """
    materials = db.session.query(MaterialV2.category_id, func.count(MaterialV2.id)).filter(
        MaterialV2.is_void == False,
        MaterialV2.category_id.isnot(None)).group_by(MaterialV2.category_id).all()
    purchases = db.session.query(PurchaseV2.category_id, func.count(PurchaseV2.id)).filter(
        PurchaseV2.is_void == False,
        PurchaseV2.category_id.isnot(None)).group_by(PurchaseV2.category_id).all()
    out = {}
    for collection, key in ((materials, 'materials'), (purchases, 'purchases')):
        for category_id, count in collection:
            if not category_id:
                continue
            out.setdefault(int(category_id), {'materials': 0, 'purchases': 0})[key] = int(count or 0)
    return out
