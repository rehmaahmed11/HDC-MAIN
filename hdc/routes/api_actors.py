"""HDC routes: JSON API for row-level audit actors.

``GET /hdc/api/row_actors`` answers "who created / last touched / voided these
rows?" for the rows currently rendered on a page.  It is what
``static/hdc/js/core/audit.js`` calls to fill the "Entered by" column on every
list table, and it is a thin wrapper over :mod:`hdc.services.actors` (which is
also available to templates as the ``hdc_row_attrs`` filter / ``actor_map``
global).

Query shape (one ``e``/``ids`` pair per entity type)::

    /hdc/api/row_actors?e=hdc_worker&ids=1,2,3&e=hdc_labour_ledger&ids=88,89
"""

from flask import jsonify, request
from flask_login import login_required

from hdc.services.actors import MAX_IDS_PER_ENTITY, actor_payload

#: Hard cap on how many entity types one request may ask about.
MAX_ENTITY_TYPES = 12


def register(app):
    """Register the row-actor JSON API."""

    @app.route('/hdc/api/row_actors', methods=['GET'])
    @login_required
    def hdc_api_row_actors():
        mapping = {}
        entity_types = request.args.getlist('e')
        id_groups = request.args.getlist('ids')
        for idx, entity_type in enumerate(entity_types[:MAX_ENTITY_TYPES]):
            clean_type = str(entity_type or '').strip()[:80]
            if not clean_type:
                continue
            raw_ids = id_groups[idx] if idx < len(id_groups) else ''
            ids = [part.strip() for part in str(raw_ids or '').split(',') if part.strip()]
            if not ids:
                continue
            bucket = mapping.setdefault(clean_type, [])
            if len(bucket) < MAX_IDS_PER_ENTITY:
                bucket.extend(ids)
        return jsonify(ok=True, actors=actor_payload(mapping))
