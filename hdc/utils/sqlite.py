"""SQLite transaction primitives; no application/model imports."""


def begin_sqlite_write(connection):
    """Reserve the writer before check-then-write validation, not at first INSERT.

    Python sqlite3 legacy transaction mode does not BEGIN on SELECT. Two workers
    can therefore both approve the same balance/key before either starts writing.
    BEGIN IMMEDIATE makes the second worker wait and read committed state.
    Call before business reads. An existing write transaction remains owned by
    its caller; this helper never commits or discards pending work.
    """
    if connection.dialect.name == 'sqlite':
        driver = connection.connection.driver_connection
        if not driver.in_transaction:
            connection.exec_driver_sql('BEGIN IMMEDIATE')


def incoming_foreign_key_reference(connection, target_table, target_id=None, excluded_tables=()):
    """Return one surviving SQLite FK reference into a table, bypassing ORM scopes.

    Schema identifiers come from SQLite itself and are quoted. This checks the
    actual on-disk schema, including tables unknown to the ORM. Composite foreign
    keys are checked column-by-column conservatively (possible false positives).
    """
    if connection.dialect.name != 'sqlite':
        raise RuntimeError('Dependency checks require SQLite.')
    quote = lambda name: '"' + str(name).replace('"', '""') + '"'
    tables = [r[0] for r in connection.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    excluded = set(excluded_tables)
    for table in tables:
        if table in excluded:
            continue
        for fk in connection.exec_driver_sql('PRAGMA foreign_key_list(' + quote(table) + ')'):
            if fk[2] != target_table:
                continue
            column, parent = fk[3], fk[4] or 'id'
            sql = (f'SELECT 1 FROM {quote(table)} AS child JOIN {quote(target_table)} AS parent '
                   f'ON child.{quote(column)} = parent.{quote(parent)}')
            params = ()
            if target_id is not None:
                sql += ' WHERE parent."id" = ?'
                params = (target_id,)
            if connection.exec_driver_sql(sql + ' LIMIT 1', params).first():
                return table, column
    return None
