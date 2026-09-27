#!/usr/bin/env python3
"""Generate a source/runtime inventory and bounded smoke evidence in isolation.

No external DB option by design: bootstrapping may migrate a database.
Usage: python scripts/qa_inventory.py --output qa/inventory.json
"""
import argparse
import ast
import inspect
import json
import os
from pathlib import Path
import re
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    result = {'scope': 'Inventory is not functional certification. Dynamic GETs are not crawled.'}
    with tempfile.TemporaryDirectory(prefix='hdc-inventory-') as tmp:
        os.environ.update(HDC_ENV='test', HDC_SECRET_KEY='inventory-only',
                          HDC_BOOTSTRAP_ADMIN_PASSWORD='Inventory-only-123!',
                          HDC_INSTANCE_DIR=tmp, HDC_DB_PATH=tmp + '/qa.db')
        from hdc.app import create_app
        from hdc.extensions import db
        from hdc.models.auth import HDCUser
        from sqlalchemy import text
        app = create_app({'TESTING': True})
        client = app.test_client()
        with app.app_context():
            uid = HDCUser.query.filter_by(username='admin').one().id
            result['models'] = []
            for mapper in sorted(db.Model.registry.mappers, key=lambda m: m.class_.__name__):
                table = mapper.local_table
                result['models'].append({
                    'model': mapper.class_.__name__, 'table': table.name,
                    'columns': [{'name': c.name, 'type': str(c.type), 'nullable': c.nullable,
                                 'primary_key': c.primary_key, 'unique': c.unique,
                                 'default': str(c.default),
                                 'foreign_keys': sorted(f.target_fullname for f in c.foreign_keys)}
                                for c in table.columns],
                    'relationships': [{'name': r.key, 'target': r.mapper.class_.__name__,
                                       'cascade': str(r.cascade)} for r in mapper.relationships],
                    'constraints': sorted(str(c) for c in table.constraints if type(c).__name__ in
                                          ('CheckConstraint', 'UniqueConstraint')),
                    'indexes': sorted(i.name for i in table.indexes)})
            result['database'] = {pragma: [list(r) for r in db.session.execute(text('PRAGMA ' + pragma))]
                                  for pragma in ('integrity_check', 'foreign_key_check', 'journal_mode', 'foreign_keys')}
        with client.session_transaction() as session:
            session['_user_id'] = str(uid)
            session['_fresh'] = True
            session['_csrf_token'] = 'inventory-csrf'
        result['routes'] = []
        result['csrf_checks'] = []
        for rule in sorted(app.url_map.iter_rules(), key=lambda r: (r.rule, r.endpoint)):
            view = inspect.unwrap(app.view_functions[rule.endpoint])
            try:
                source = inspect.getsource(view)
            except (TypeError, OSError):
                source = ''
            entry = {'endpoint': rule.endpoint, 'url': rule.rule, 'methods': sorted(rule.methods),
                     'module': view.__module__, 'arguments': sorted(rule.arguments),
                     'guards_source': [g for g in ('login_required', '_money_write_required', '_admin_only', '_money_only') if g in source],
                     'templates': re.findall(r"render_template\(['\"]([^'\"]+)", source),
                     'calls': sorted(set(re.findall(r'\b([a-zA-Z_]\w*)\(', source))),
                     'get_smoke': 'DEFERRED'}
            if 'GET' in rule.methods and not rule.arguments:
                try:
                    response = client.get(rule.rule)
                    entry['get_smoke'] = {'status': response.status_code, 'content_type': response.content_type}
                except Exception as exc:
                    entry['get_smoke'] = {'exception': type(exc).__name__ + ': ' + str(exc)}
            result['routes'].append(entry)
            # Missing/invalid CSRF must fail before record lookup, including aliases.
            url = re.sub(r'<(?:[^:>]+:)?[^>]+>', '1', rule.rule)
            for method in sorted(rule.methods & {'POST', 'PUT', 'PATCH', 'DELETE'}):
                for token in (None, 'wrong-token'):
                    response = client.open(url, method=method, json={},
                                           headers={'X-CSRFToken': token} if token else {})
                    result['csrf_checks'].append({'url': rule.rule, 'method': method,
                                                'token': 'invalid' if token else 'missing',
                                                'status': response.status_code,
                                                'rejected_for_csrf': response.status_code == 400 and b'CSRF token' in response.data})
        result['templates'] = []
        for path in sorted((ROOT / 'templates/hdc').rglob('*.html')):
            source = path.read_text()
            name = str(path.relative_to(ROOT / 'templates/hdc'))
            try:
                app.jinja_env.get_template(name)
                compiled = True
            except Exception as exc:
                compiled = str(exc)
            result['templates'].append({'path': str(path.relative_to(ROOT)), 'compiled': compiled,
                                        'forms': re.findall(r'<form\b[^>]*>', source, re.I),
                                        'url_for': re.findall(r"url_for\(['\"]([^'\"]+)", source),
                                        'interaction_counts': {term: len(re.findall(term, source, re.I)) for term in
                                                               ('<table', 'modal', 'fetch\\(', 'pagination', 'type="file"')}})
        result['assets'] = []
        for path in sorted((ROOT / 'static/hdc').rglob('*')):
            if path.is_file():
                url = '/hdc_static/' + str(path.relative_to(ROOT / 'static/hdc'))
                response = client.get(url)
                result['assets'].append({'path': str(path.relative_to(ROOT)), 'url': url,
                                         'status': response.status_code, 'content_type': response.content_type,
                                         'api_literals': re.findall(r'''["'`]([^"'`]*(?:/api/|/api/v2/)[^"'`]*)["'`]''', path.read_text()) if path.suffix == '.js' else []})
        result['tests'] = []
        for path in sorted((ROOT / 'tests').glob('test_*.py')):
            tree = ast.parse(path.read_text())
            result['tests'].append({'path': str(path.relative_to(ROOT)), 'tests': [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name.startswith('test_')]})
        result['documents'] = [str(p.relative_to(ROOT)) for p in sorted(ROOT.glob('*.md'))]
        result['environment_variables'] = sorted(set(re.findall(r'HDC_[A-Z0-9_]+', '\n'.join(p.read_text() for p in (ROOT / 'hdc').rglob('*.py')))))
        with app.app_context():
            db.session.remove()
            db.engine.dispose()
        # Second bootstrap must preserve seed identities/counts and integrity.
        app2 = create_app({'TESTING': True})
        with app2.app_context():
            result['repeat_bootstrap'] = {'admin_count': HDCUser.query.filter_by(username='admin').count(),
                                          'admin_id_preserved': HDCUser.query.filter_by(username='admin').one().id == uid,
                                          'integrity': db.session.execute(text('PRAGMA integrity_check')).scalar()}
            db.session.remove()
            db.engine.dispose()
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({'routes': len(result['routes']), 'models': len(result['models']),
                      'templates': len(result['templates']), 'assets': len(result['assets']),
                      'csrf_checks': len(result['csrf_checks']), 'database': result['database']}))
    assert all(r['rejected_for_csrf'] for r in result['csrf_checks'])
    assert all(r['compiled'] is True for r in result['templates'])
    assert all(r['status'] == 200 for r in result['assets'])


if __name__ == '__main__':
    main()
