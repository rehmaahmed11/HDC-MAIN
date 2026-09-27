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
