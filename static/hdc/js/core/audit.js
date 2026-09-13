/* HDC row traceability — "who entered this?" on every list row.
 *
 * List rows carry data-hdc-ent / data-hdc-id (rendered by the `hdc_row_attrs`
 * Jinja filter).  This script asks /hdc/api/row_actors once per page for all
 * of them and fills in an "Entered by" column (tables) or an inline badge
 * (list items), with the full trail — created / last updated / voided — in the
 * cell tooltip.  Voided rows are also greyed out, but that is pure CSS
 * (tr[data-hdc-void="1"]) so it works even if this script never runs.
 */
(function () {
    'use strict';

    var MAX_IDS_PER_ENTITY = 1000;
    var API = '/hdc/api/row_actors';

    function el(tag, cls, text) {
        var node = document.createElement(tag);
        if (cls) node.className = cls;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }

    function isTableRow(node) {
        return !!(node && node.tagName && node.tagName.toUpperCase() === 'TR');
    }

    function groupRows() {
        var nodes = document.querySelectorAll('[data-hdc-ent][data-hdc-id]');
        var groups = {};
        Array.prototype.forEach.call(nodes, function (node) {
            var ent = node.getAttribute('data-hdc-ent');
            var id = node.getAttribute('data-hdc-id');
            if (!ent || !id) return;
            var group = groups[ent] || (groups[ent] = { ids: [], nodes: [] });
            if (group.ids.indexOf(id) === -1 && group.ids.length < MAX_IDS_PER_ENTITY) {
                group.ids.push(id);
            }
            group.nodes.push(node);
        });
        return groups;
    }

    function buildQuery(groups) {
        var parts = [];
        Object.keys(groups).forEach(function (ent) {
            var ids = groups[ent].ids;
            if (!ids.length) return;
            parts.push('e=' + encodeURIComponent(ent) + '&ids=' + encodeURIComponent(ids.join(',')));
        });
        return parts.join('&');
    }

    function trailText(info, label) {
        var lines = [];
        if (label) lines.push('Entry: ' + label);
        if (info.created_at || info.created_by) {
            lines.push('Created by ' + (info.created_by || '—') + (info.created_at ? ' · ' + info.created_at : ''));
        }
        if (info.updated_at && info.updated_at !== info.created_at) {
            lines.push('Last updated by ' + (info.updated_by || '—') + ' · ' + info.updated_at);
        }
        if (info.voided_at || info.voided_by) {
            lines.push('Voided by ' + (info.voided_by || '—') + (info.voided_at ? ' · ' + info.voided_at : ''));
        }
        if (info.void_reason) lines.push('Void reason: ' + info.void_reason);
        if (info.derived) lines.push('Traced through this entry\u2019s linked records (accounts posting / time entry / expense).');
        if (!lines.length) lines.push('No activity trail recorded for this entry.');
        return lines.join('\n');
    }

    function buildCell(info, label, voided) {
        var td = el('td', 'hdc-audit-cell');
        if (!info || !info.events) {
            var none = el('span', 'hdc-audit-none', '\u2014');
            none.title = trailText(info || {}, label);
            td.appendChild(none);
        } else {
            var who = info.last_by || info.created_by || '—';
            var badge = el('span', 'hdc-actor-badge');
            badge.appendChild(el('i', 'fas fa-user-check hdc-actor-icon'));
            badge.appendChild(document.createTextNode(who));
            td.appendChild(badge);
            if (info.created_at) {
                td.appendChild(el('span', 'hdc-actor-meta', info.created_at));
            }
        }
        if (voided || (info && (info.voided_at || info.voided_by))) {
            var voidBadge = el('span', 'hdc-void-badge', 'Void');
            td.appendChild(voidBadge);
        }
        td.title = trailText(info || {}, label);
        return td;
    }

    function buildInline(info, label, voided) {
        var wrap = el('span', 'hdc-actor-inline');
        wrap.appendChild(el('i', 'fas fa-user-check'));
        wrap.appendChild(document.createTextNode(
            (info && (info.last_by || info.created_by)) || 'no entry trail'
        ));
        if (voided) wrap.appendChild(el('span', 'hdc-void-badge', 'Void'));
        wrap.title = trailText(info || {}, label);
        return wrap;
    }

    function ensureColumn(table) {
        if (!table || table.getAttribute('data-hdc-audit-col') === '1') return;
        var headRows = table.querySelectorAll('thead tr');
        if (headRows.length) {
            headRows[headRows.length - 1].appendChild(el('th', 'hdc-audit-col', 'Entered by'));
        } else {
            // No header row to extend: give the table one so the column is labelled.
            var thead = el('thead');
            var headRow = el('tr');
            headRow.appendChild(el('th', 'hdc-audit-col', 'Entered by'));
            thead.appendChild(headRow);
            table.insertBefore(thead, table.firstChild);
        }
        // keep "no records" placeholder rows covering the extra column
        Array.prototype.forEach.call(table.querySelectorAll('tr'), function (row) {
            if (row.getAttribute('data-hdc-id')) return;
            var cells = row.children;
            if (cells.length !== 1) return;
            var cell = cells[0];
            if (!cell || cell.tagName.toUpperCase() !== 'TD') return;
            var span = parseInt(cell.getAttribute('colspan'), 10);
            if (span > 0) cell.setAttribute('colspan', String(span + 1));
        });
        table.setAttribute('data-hdc-audit-col', '1');
    }

    function decorate(node, info) {
        var label = node.getAttribute('data-hdc-label') || '';
        var voided = node.getAttribute('data-hdc-void') === '1';
        if (isTableRow(node)) {
            var table = node.closest ? node.closest('table') : null;
            if (!table) return;
            ensureColumn(table);
            node.appendChild(buildCell(info, label, voided));
            return;
        }
        node.appendChild(buildInline(info, label, voided));
    }

    function apply(actors) {
        var groups = groupRows();
        Object.keys(groups).forEach(function (ent) {
            var byId = (actors && actors[ent]) || {};
            groups[ent].nodes.forEach(function (node) {
                var id = node.getAttribute('data-hdc-id');
                decorate(node, byId[id] || null);
            });
        });
    }

    function init() {
        var groups = groupRows();
        var query = buildQuery(groups);
        if (!query) return;
        fetch(API + '?' + query, {
            credentials: 'same-origin',
            headers: { 'Accept': 'application/json' }
        }).then(function (response) {
            if (!response.ok) throw new Error('row_actors HTTP ' + response.status);
            return response.json();
        }).then(function (data) {
            if (!data || !data.ok) return;
            apply(data.actors || {});
        }).catch(function () {
            /* Traceability is best-effort: never break the page over it. */
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
