"""Who did it — actor lookup for every list screen.

Every insert / update / delete the app performs is captured by the SQLAlchemy
``after_flush`` listener in :mod:`hdc.services.audit` into
``hdc_user_activity`` (``entity_type`` = table name, ``entity_id`` = row id,
``username`` = the logged-in user).  This service turns that raw log into a
compact, **batched** "who touched this row" lookup — one query per entity type
per page instead of one query per row — so any list template can show:

    created by  •  last updated by  •  who voided it (and why)

Two tables are deliberately skipped by the audit listener
(``hdc_labour_ledger`` mirrors and ``hdc_attendance_day`` summaries).  Labour
ledger rows are resolved through the row they mirror instead: a cash row
(advance / payment / tip / settlement) from its ``hdc_account_txn`` posting, a
``work`` row from its time entry.  Rows older than the audit trail simply come
back empty and are rendered as "—".

Typical template use::

    {% set actors = actor_map(ledger_entries) %}
    ...
    <td>{{ actors.get(row.id, {}).get('last_by') or '—' }}</td>

The page-wide JS column (``static/hdc/js/core/audit.js``) uses the same data
through ``/hdc/api/row_actors``.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict

from flask import g, has_request_context
from markupsafe import Markup, escape

from hdc.extensions import db
from hdc.models.accounts import AccountTransaction
from hdc.models.auth import UserActivity
from hdc.models.workforce import LabourLedger, TimeEntry
from hdc.utils.dates import _as_pkt

#: Status values that mean "this row was voided".
VOID_STATUSES = frozenset({'void', 'voided', 'cancelled', 'canceled'})

#: Show at most this many ids per entity type in one lookup/API call.
MAX_IDS_PER_ENTITY = 1000

_CHUNK = 400

#: entity types whose actor has to be derived from a mirrored row.
_DERIVED_ENTITY_TYPES = frozenset({'hdc_labour_ledger'})

#: tags the labour ledger writes into ``notes`` to point at its expense mirror
_EXPENSE_TAG_RE = re.compile(r'(?:TIP|SETTLE|SETTLEMENT)_EXPENSE_ID:(\d+)')


#: dict keys that hold the ORM row a view-model row stands for, in priority order
_VIEW_ROW_KEYS = (
    '_hdc_row', 'row', 'record', 'item', 'obj', 'object', 'entity', 'source',
    'entry', 'txn', 'transaction', 'expense', 'payment', 'ledger', 'settlement',
    'material', 'supplier', 'purchase', 'delivery', 'usage', 'account', 'staff',
    'mark', 'attendance', 'run', 'document', 'doc', 'invoice', 'voucher',
    'report',
)


def model_of(obj, _depth=0):
    """Best-effort: the ORM row a template loop variable stands for.

    Templates loop over all sorts of things: model instances, ``(row, project,
    stage)`` tuples, and hand-built view-model dicts.  Resolving the underlying
    row here means a single ``{{ row|hdc_row_attrs }}`` works for all of them.
    """
    if obj is None or _depth > 2:
        return None
    try:
        if getattr(obj, '__tablename__', None) and getattr(obj, 'id', None) is not None:
            return obj
    except Exception:
        return None
    if isinstance(obj, (tuple, list)) or hasattr(obj, '_mapping') or hasattr(obj, '_fields'):
        # plain tuples and SQLAlchemy Row objects, e.g. a query of
        # ``(Expense, Project, Stage)`` -- the first model in the row wins.
        try:
            parts = list(obj)
        except TypeError:
            parts = []
        for part in parts:
            found = model_of(part, _depth + 1)
            if found is not None:
                return found
        return None
    if isinstance(obj, dict):
        for key in _VIEW_ROW_KEYS:
            if key in obj:
                found = model_of(obj.get(key), _depth + 1)
                if found is not None:
                    return found
        return None
    return None


def entity_type_of(obj):
    """Return the audit ``entity_type`` for a model instance ('' if unknown)."""
    if obj is None:
        return ''
    try:
        if isinstance(obj, dict):
            marker = str(obj.get('_hdc_entity') or '').strip()
            if marker:
                return marker
        model = model_of(obj)
        if model is not None:
            return str(getattr(model, '__tablename__', '') or '')
    except Exception:
        # Jinja's Undefined (e.g. the {% else %} branch of a for loop) raises
        # on attribute access -- treat anything unreadable as "not a row".
        return ''
    return ''


def row_id_of(obj, id_attr='id'):
    """Return the integer id of a model instance, or None."""
    try:
        if isinstance(obj, dict) and obj.get('_hdc_id') is not None:
            return int(obj['_hdc_id'])
        model = model_of(obj)
        if model is not None:
            val = getattr(model, id_attr, None)
            return None if val is None else int(val)
        if isinstance(obj, dict) and obj.get('id') is not None:
            return int(obj['id'])
    except Exception:
        return None
    return None


def row_is_void(obj):
    """True when a row is voided / cancelled (grey out the whole row)."""
    if obj is None:
        return False
    try:
        if isinstance(obj, dict):
            for key in ('is_void', 'voided_at', 'void'):
                if bool(obj.get(key)):
                    return True
            if str(obj.get('status') or '').strip().lower() in VOID_STATUSES:
                return True
        model = model_of(obj) or obj
        if bool(getattr(model, 'is_void', False)):
            return True
        if getattr(model, 'voided_at', None):
            return True
        if str(getattr(model, 'status', '') or '').strip().lower() in VOID_STATUSES:
            return True
    except Exception:
        return False
    return False


def void_reason_of(obj):
    try:
        if isinstance(obj, dict):
            for key in ('void_reason', 'void_notes', 'status_reason'):
                val = obj.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
        model = model_of(obj) or obj
        for attr in ('void_reason', 'void_notes', 'status_reason'):
            val = getattr(model, attr, None)
            if isinstance(val, str) and val.strip():
                return val.strip()
        status = str(getattr(model, 'status', '') or '').strip()
        if status.lower() in VOID_STATUSES:
            return status.title()
    except Exception:
        return ''
    return ''


def row_label(obj):
    """A short human label used for the row tooltip."""
    if obj is None:
        return ''
    try:
        if isinstance(obj, dict):
            for key in ('name', 'label', 'title', 'worker_name', 'supplier_name',
                        'party_name', 'reference_id', 'source', 'ref', 'remarks',
                        'note', 'notes', 'description', 'summary', 'category'):
                val = obj.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()[:90]
        model = model_of(obj) or obj
        for attr in ('name', 'worker_code', 'supplier_name', 'party_name', 'label',
                     'reference_id', 'title', 'remarks', 'note', 'notes',
                     'description', 'summary', 'category'):
            val = getattr(model, attr, None)
            if isinstance(val, str) and val.strip():
                return val.strip()[:90]
    except Exception:
        return ''
    return ''


def _fmt_dt(value):
    pkt = _as_pkt(value)
    return pkt.strftime('%Y-%m-%d %H:%M') if pkt else ''


def empty_info(entity_type='', row_id=None):
    return {
        'entity_type': entity_type,
        'id': ('' if row_id is None else str(row_id)),
        'created_by': '',
        'created_at': '',
        'updated_by': '',
        'updated_at': '',
        'last_by': '',
        'last_at': '',
        'voided_by': '',
        'voided_at': '',
        'void_reason': '',
        'events': 0,
        'derived': False,
    }


# --------------------------------------------------------------------------
# raw audit rows
# --------------------------------------------------------------------------
def _chunks(seq, size=_CHUNK):
    seq = list(seq)
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _audit_rows(entity_type, ids):
    """All audit rows for one entity type + id set, oldest first."""
    out = []
    str_ids = [str(int(i)) for i in ids]
    for chunk in _chunks(str_ids):
        rows = (UserActivity.query
                .filter(UserActivity.entity_type == entity_type,
                        UserActivity.entity_id.in_(chunk))
                .order_by(UserActivity.created_at.asc(), UserActivity.id.asc())
                .all())
        out.extend(rows)
    return out


def _changed_fields(row):
    raw = getattr(row, 'changed_fields', None)
    if not raw:
        return {}
    try:
        val = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return val if isinstance(val, dict) else {}


def _truthy(val):
    return str(val).strip().lower() in ('1', 'true', 'yes', 'on')


def _void_transition(changed):
    """Return (is_void_event, reason) for an update event's changed fields."""
    if not changed:
        return False, ''
    for key in ('is_void', 'voided_at', 'void'):
        if key in changed:
            entry = changed.get(key) or {}
            new_val = entry.get('new') if isinstance(entry, dict) else entry
            if key == 'voided_at':
                if str(new_val or '').strip():
                    return True, ''
            elif _truthy(new_val):
                return True, ''
    status_entry = changed.get('status')
    if isinstance(status_entry, dict):
        new_status = str(status_entry.get('new') or '').strip().lower()
        if new_status in VOID_STATUSES:
            return True, str(status_entry.get('new') or '').strip().title()
    return False, ''


def _info_from_audit_rows(entity_type, row_id, rows):
    info = empty_info(entity_type, row_id)
    for row in rows:
        username = str(getattr(row, 'username', '') or '').strip()
        event_type = str(getattr(row, 'event_type', '') or '').strip().lower()
        created_at = _fmt_dt(getattr(row, 'created_at', None))
        info['events'] += 1
        # rows arrive oldest-first, so the latest assignment wins
        info['last_by'], info['last_at'] = username or info['last_by'], created_at or info['last_at']
        if event_type == 'create' and not info['created_by']:
            info['created_by'], info['created_at'] = username, created_at
        if event_type == 'update':
            info['updated_by'], info['updated_at'] = username, created_at
            is_void_event, reason = _void_transition(_changed_fields(row))
            if is_void_event:
                info['voided_by'], info['voided_at'] = username, created_at
                if reason:
                    info['void_reason'] = reason
    if not info['created_by']:
        info['created_by'], info['created_at'] = info['last_by'], info['last_at']
    return info


# --------------------------------------------------------------------------
# derived actors (tables the audit listener deliberately skips)
# --------------------------------------------------------------------------
def _derive_labour_ledger(ids):
    """Resolve labour-ledger actors through their mirrored rows.

    Cash rows mirror into ``hdc_account_txn`` (source_type
    ``labour_ledger_<entry_type>[:direct]``); ``work`` rows mirror a time
    entry.  Both mirrors are audited, so the actor of the mirror is the actor
    of the ledger row.
    """
    ids = [int(i) for i in ids]
    if not ids:
        return {}
    ledger_rows = LabourLedger.query.filter(LabourLedger.id.in_(ids)).all()
    by_id = {int(r.id): r for r in ledger_rows}
    txn_by_ledger = {}
    te_by_ledger = {}
    for row in ledger_rows:
        if row.entry_type == 'work' and row.time_entry_id:
            te_by_ledger[int(row.id)] = int(row.time_entry_id)

    cash_ids = [int(r.id) for r in ledger_rows if (r.entry_type or '') != 'work']
    if cash_ids:
        txns = (AccountTransaction.query
                .filter(AccountTransaction.source_id.in_(cash_ids),
                        AccountTransaction.source_type.ilike('labour_ledger_%'))
                .order_by(AccountTransaction.id.asc())
                .all())
        for txn in txns:
            source = (txn.source_type or '').split(':')[0].strip().lower()
            ledger_id = int(txn.source_id or 0)
            row = by_id.get(ledger_id)
            if not row:
                continue
            if source != f'labour_ledger_{(row.entry_type or "").strip().lower()}':
                continue
            txn_by_ledger.setdefault(ledger_id, int(txn.id))

    out = {}
    if txn_by_ledger:
        for row in _audit_rows('hdc_account_txn', set(txn_by_ledger.values())):
            ledger_id = None
            row_id = str(getattr(row, 'entity_id', '') or '')
            for lid, txn_id in txn_by_ledger.items():
                if str(txn_id) == row_id:
                    ledger_id = lid
                    break
            if ledger_id is None:
                continue
            out.setdefault(ledger_id, []).append(row)
    if te_by_ledger:
        for row in _audit_rows('hdc_time_entry', set(te_by_ledger.values())):
            row_id = str(getattr(row, 'entity_id', '') or '')
            for lid, te_id in te_by_ledger.items():
                if str(te_id) == row_id:
                    out.setdefault(lid, []).append(row)
                    break

    # Tips / settlements also mirror into hdc_expense, tagged in the row notes.
    expense_by_ledger = {}
    for row in ledger_rows:
        if int(row.id) in out:
            continue
        match = _EXPENSE_TAG_RE.search(row.notes or '')
        if match:
            expense_by_ledger[int(row.id)] = int(match.group(1))
    if expense_by_ledger:
        for audit_row in _audit_rows('hdc_expense', set(expense_by_ledger.values())):
            entity_id = str(getattr(audit_row, 'entity_id', '') or '')
            for ledger_id, expense_id in expense_by_ledger.items():
                if str(expense_id) == entity_id:
                    out.setdefault(ledger_id, []).append(audit_row)

    result = {}
    for ledger_id, rows in out.items():
        info = _info_from_audit_rows('hdc_labour_ledger', ledger_id, rows)
        info['derived'] = True
        result[ledger_id] = info
    return result


_DERIVERS = {
    'hdc_labour_ledger': _derive_labour_ledger,
}


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------
def _request_cache():
    if not has_request_context():
        return None
    cache = getattr(g, '_hdc_actor_cache', None)
    if cache is None:
        cache = {}
        setattr(g, '_hdc_actor_cache', cache)
    return cache


def _load_missing(entity_type, ids):
    """Load actor info for ids not already cached (one query per entity type).

    Results are memoised on ``flask.g`` for the current request, so a page that
    renders the same table twice (page + modal) still issues a single query.
    """
    ids = [int(i) for i in dict.fromkeys(ids)]
    cache = _request_cache()
    missing = ids if cache is None else [i for i in ids if (entity_type, i) not in cache]

    if missing:
        rows_by_id = defaultdict(list)
        for row in _audit_rows(entity_type, missing):
            try:
                rows_by_id[int(row.entity_id)].append(row)
            except (TypeError, ValueError):
                continue
        resolved = {}
        for row_id in missing:
            rows = rows_by_id.get(int(row_id), [])
            if rows:
                resolved[int(row_id)] = _info_from_audit_rows(entity_type, row_id, rows)
        still_missing = [i for i in missing if int(i) not in resolved]
        if still_missing and entity_type in _DERIVED_ENTITY_TYPES:
            try:
                resolved.update(_DERIVERS[entity_type](still_missing) or {})
            except Exception:
                # Never let traceability break a page: fall back to "no trail".
                resolved = resolved
        if cache is None:
            return {i: (resolved.get(int(i)) or empty_info(entity_type, i)) for i in ids}
        for row_id in missing:
            cache[(entity_type, int(row_id))] = resolved.get(int(row_id))

    return {i: (cache.get((entity_type, i)) or empty_info(entity_type, i)) for i in ids}


def actor_map(items, entity_type=None, id_attr='id'):
    """Batched ``{row_id: actor_info}`` for a list of model rows.

    ``items`` may be model instances (entity type is taken from
    ``__tablename__``) or bare ids when ``entity_type`` is given.  Rows that are
    not ORM objects (dicts, strings, aggregate rows) are ignored, so templates
    can safely pipe any loop variable through it.
    """
    grouped = defaultdict(set)
    for item in items or []:
        if item is None:
            continue
        if isinstance(item, int) or (isinstance(item, str) and item.strip().isdigit()):
            if entity_type:
                grouped[entity_type].add(int(item))
            continue
        et = entity_type or entity_type_of(item)
        row_id = row_id_of(item, id_attr)
        if et and row_id is not None:
            grouped[et].add(row_id)

    out = {}
    for et, ids in grouped.items():
        ids = sorted(ids)[:MAX_IDS_PER_ENTITY]
        for row_id, info in _load_missing(et, ids).items():
            out[row_id] = info
    return out


def actor_for(obj, entity_type=None):
    """Actor info for a single model instance ({} when unknown)."""
    et = entity_type or entity_type_of(obj)
    row_id = row_id_of(obj)
    if not et or row_id is None:
        return empty_info(et, row_id)
    return _load_missing(et, [row_id]).get(row_id) or empty_info(et, row_id)


def row_attrs_html(obj=None, source=None, label=None, entity_type=None,
                   row_id=None, void=None, reason=None):
    """Render the ``data-hdc-*`` attributes for one list row (Jinja filter).

    Used as ``<tr {{ row|hdc_row_attrs }}>``.  ``row`` may be a model instance,
    a ``(row, project, stage)`` tuple, or a view-model dict -- the underlying
    row is resolved automatically.  A dict can also declare itself explicitly
    with ``_hdc_entity`` / ``_hdc_id`` keys, and callers can pass
    ``entity_type=`` / ``row_id=`` for anything else.

    Returns an empty string for anything that is not a database row, so piping
    an aggregate/dict loop variable through the filter is always safe.
    """
    try:
        target = obj
        if entity_type is None:
            entity_type = entity_type_of(obj) or entity_type_of(source)
            if not entity_type and source is not None:
                target = source
        if row_id is None:
            row_id = row_id_of(obj)
            if row_id is None:
                row_id = row_id_of(source)
    except Exception:
        return Markup('')
    if not entity_type or row_id is None:
        return Markup('')

    try:
        parts = [f'data-hdc-ent="{escape(entity_type)}"', f'data-hdc-id="{row_id}"']
        is_void = row_is_void(target if target is not None else obj)
        if void is not None:
            is_void = bool(void)
        if is_void:
            parts.append('data-hdc-void="1"')
            reason_txt = reason if reason is not None else void_reason_of(obj)
            if reason_txt:
                parts.append(f'data-hdc-void-reason="{escape(reason_txt)}"')
        lbl = label or row_label(obj) or row_label(source)
        if lbl:
            parts.append(f'data-hdc-label="{escape(lbl)}"')
    except Exception:
        return Markup('')
    return Markup(' ' + ' '.join(parts))


def actor_payload(mapping):
    """``{entity_type: {str(id): info}}`` for the JSON endpoint.

    ``mapping`` is ``{entity_type: iterable_of_ids}``.
    """
    payload = {}
    for entity_type, ids in (mapping or {}).items():
        clean = []
        for raw in ids or []:
            try:
                clean.append(int(raw))
            except (TypeError, ValueError):
                continue
        if not clean:
            continue
        clean = sorted(set(clean))[:MAX_IDS_PER_ENTITY]
        payload[entity_type] = {
            str(row_id): info
            for row_id, info in _load_missing(entity_type, clean).items()
        }
    return payload
