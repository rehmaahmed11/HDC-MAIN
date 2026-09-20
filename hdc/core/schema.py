"""HDC core.schema — moved verbatim from hdc_erp.py.

See MODULARIZATION_PLAN.md for the module map.
"""

from sqlalchemy import text

from hdc.extensions import db

def _ensure_timeentry_unique_indexes():
    with db.engine.connect() as conn:
        try:
            conn.execute(text("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_time_entry_active_worker_checkin
                ON hdc_time_entry(worker_id, check_in)
                WHERE is_void = 0
            """))
            conn.commit()
        except Exception:
            conn.rollback()


def _ensure_timeentry_attendance_day_schema():
    """Give ``hdc_time_entry`` a dedicated link to its AttendanceDay summary.

    ``attendance_id`` is owned by the one-off legacy migration and points at
    ``hdc_attendance``. Day recalculation used to overwrite it with an
    ``hdc_attendance_day`` id, which made three readers (``Worker.total_earned``,
    ``Project.total_labour_cost`` and ``Stage.stage_labour_cost``) treat a legacy
    attendance wage as already migrated and drop it (LABOUR_AUDIT #6).
    """
    _ensure_table_columns_sqlite('hdc_time_entry', {
        'attendance_day_id': 'attendance_day_id INTEGER REFERENCES hdc_attendance_day(id)',
    })


def _ensure_runtime_flags_table():
    db.session.execute(text("""
        CREATE TABLE IF NOT EXISTS hdc_runtime_flag (
            key VARCHAR(80) PRIMARY KEY,
            value TEXT,
            updated_at DATETIME
        )
    """))
    db.session.commit()


def _run_migrations():
    """Safely add new columns to existing tables (SQLite-compatible)."""
    with db.engine.connect() as conn:
        try:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS hdc_worker_trade (
                    id INTEGER PRIMARY KEY,
                    name VARCHAR(80) UNIQUE NOT NULL,
                    active_status BOOLEAN DEFAULT 1,
                    created_at DATETIME
                )
            """))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS hdc_expense_category (
                    id INTEGER PRIMARY KEY,
                    name VARCHAR(80) UNIQUE NOT NULL,
                    active_status BOOLEAN DEFAULT 1,
                    created_at DATETIME
                )
            """))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS hdc_stage_drawing (
                    id INTEGER PRIMARY KEY,
                    stage_id INTEGER NOT NULL REFERENCES hdc_stage(id),
                    original_name VARCHAR(255) NOT NULL,
                    stored_name VARCHAR(255) NOT NULL UNIQUE,
                    created_at DATETIME,
                    updated_at DATETIME
                )
            """))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS hdc_attendance_mark (
                    id INTEGER PRIMARY KEY,
                    worker_id INTEGER NOT NULL REFERENCES hdc_worker(id),
                    date DATE NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'absent',
                    notes VARCHAR(250),
                    activity_at DATETIME,
                    created_at DATETIME,
                    CONSTRAINT uq_attendance_mark_worker_date UNIQUE (worker_id, date)
                )
            """))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS hdc_attendance_day (
                    id INTEGER PRIMARY KEY,
                    worker_id INTEGER NOT NULL REFERENCES hdc_worker(id),
                    date DATE NOT NULL,
                    total_hours FLOAT DEFAULT 0,
                    day_value FLOAT DEFAULT 0,
                    overtime_hours FLOAT DEFAULT 0,
                    entry_count INTEGER DEFAULT 0,
                    is_void BOOLEAN DEFAULT 0,
                    created_at DATETIME,
                    updated_at DATETIME,
                    CONSTRAINT uq_attendance_day_worker_date UNIQUE (worker_id, date)
                )
            """))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS hdc_subcontract_attendance (
                    id INTEGER PRIMARY KEY,
                    subcontractor_id INTEGER NOT NULL REFERENCES hdc_subcontractor(id),
                    date DATE NOT NULL,
                    present_count INTEGER DEFAULT 0,
                    work_done_pct FLOAT DEFAULT 0,
                    notes VARCHAR(250),
                    activity_at DATETIME,
                    created_at DATETIME,
                    CONSTRAINT uq_subcontract_attendance_sub_date UNIQUE (subcontractor_id, date)
                )
            """))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS hdc_subcontract_labour_worker (
                    id INTEGER PRIMARY KEY,
                    subcontractor_id INTEGER NOT NULL REFERENCES hdc_subcontractor(id),
                    name VARCHAR(120) NOT NULL,
                    phone VARCHAR(30),
                    trade VARCHAR(80),
                    daily_wage FLOAT DEFAULT 0,
                    active_status BOOLEAN DEFAULT 1,
                    created_at DATETIME
                )
            """))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS hdc_subcontract_labour_payment (
                    id INTEGER PRIMARY KEY,
                    subcontractor_id INTEGER NOT NULL REFERENCES hdc_subcontractor(id),
                    worker_id INTEGER NOT NULL REFERENCES hdc_subcontract_labour_worker(id),
                    amount FLOAT DEFAULT 0,
                    date DATE,
                    notes VARCHAR(250),
                    activity_at DATETIME,
                    created_at DATETIME
                )
            """))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS hdc_subcontract_labour_attendance (
                    id INTEGER PRIMARY KEY,
                    subcontractor_id INTEGER NOT NULL REFERENCES hdc_subcontractor(id),
                    project_id INTEGER REFERENCES hdc_project(id),
                    stage_id INTEGER NOT NULL REFERENCES hdc_stage(id),
                    worker_id INTEGER REFERENCES hdc_subcontract_labour_worker(id),
                    date DATE NOT NULL,
                    labour_count INTEGER DEFAULT 0,
                    wage_rate FLOAT DEFAULT 0,
                    total_labour_paid FLOAT DEFAULT 0,
                    attendance_status VARCHAR(20) DEFAULT 'Present',
                    working_hours FLOAT DEFAULT 0,
                    overtime_hours FLOAT DEFAULT 0,
                    notes VARCHAR(250),
                    activity_at DATETIME,
                    created_at DATETIME,
                    updated_at DATETIME,
                    CONSTRAINT uq_sub_labour_sub_stage_worker_date UNIQUE (subcontractor_id, stage_id, worker_id, date)
                )
            """))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS hdc_subcontract_event (
                    id INTEGER PRIMARY KEY,
                    subcontractor_id INTEGER NOT NULL REFERENCES hdc_subcontractor(id),
                    project_id INTEGER REFERENCES hdc_project(id),
                    stage_id INTEGER REFERENCES hdc_stage(id),
                    actor_user_id INTEGER REFERENCES hdc_user(id),
                    event_type VARCHAR(40) NOT NULL,
                    from_value VARCHAR(250),
                    to_value VARCHAR(250),
                    amount FLOAT DEFAULT 0,
                    notes VARCHAR(300),
                    created_at DATETIME
                )
            """))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS hdc_office_staff (
                    id INTEGER PRIMARY KEY,
                    staff_code VARCHAR(20) UNIQUE NOT NULL,
                    name VARCHAR(100) NOT NULL,
                    role_type VARCHAR(80),
                    phone VARCHAR(30),
                    monthly_salary FLOAT DEFAULT 0,
                    active_status BOOLEAN DEFAULT 1,
                    created_at DATETIME
                )
            """))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS hdc_office_staff_attendance (
                    id INTEGER PRIMARY KEY,
                    staff_id INTEGER NOT NULL REFERENCES hdc_office_staff(id),
                    date DATE NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'present',
                    notes VARCHAR(250),
                    activity_at DATETIME,
                    created_at DATETIME,
                    updated_at DATETIME,
                    CONSTRAINT uq_office_staff_attendance_staff_date UNIQUE (staff_id, date)
                )
            """))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS hdc_office_staff_ledger (
                    id INTEGER PRIMARY KEY,
                    staff_id INTEGER NOT NULL REFERENCES hdc_office_staff(id),
                    date DATE,
                    entry_type VARCHAR(20) NOT NULL,
                    amount FLOAT DEFAULT 0,
                    notes TEXT,
                    is_void BOOLEAN DEFAULT 0,
                    void_reason VARCHAR(250),
                    voided_at DATETIME,
                    activity_at DATETIME,
                    created_at DATETIME
                )
            """))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS hdc_office_expense (
                    id INTEGER PRIMARY KEY,
                    date DATE,
                    category VARCHAR(80),
                    amount FLOAT DEFAULT 0,
                    remarks VARCHAR(250),
                    is_void BOOLEAN DEFAULT 0,
                    void_reason VARCHAR(250),
                    voided_at DATETIME,
                    activity_at DATETIME,
                    created_at DATETIME
                )
            """))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS hdc_activity_log (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER REFERENCES hdc_user(id),
                    username VARCHAR(80),
                    action_type VARCHAR(40) NOT NULL,
                    description TEXT NOT NULL,
                    entity_type VARCHAR(80) NOT NULL,
                    entity_id VARCHAR(80),
                    created_at DATETIME
                )
            """))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS hdc_supplier (
                    id INTEGER PRIMARY KEY,
                    name VARCHAR(120) NOT NULL,
                    phone VARCHAR(30),
                    status VARCHAR(20) DEFAULT 'active',
                    is_void BOOLEAN DEFAULT 0,
                    created_at DATETIME,
                    updated_at DATETIME
                )
            """))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS hdc_material_v2 (
                    id INTEGER PRIMARY KEY,
                    name VARCHAR(120) NOT NULL,
                    unit VARCHAR(20) DEFAULT 'KG',
                    status VARCHAR(20) DEFAULT 'active',
                    is_void BOOLEAN DEFAULT 0,
                    created_at DATETIME,
                    updated_at DATETIME
                )
            """))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS hdc_purchase_v2 (
                    id INTEGER PRIMARY KEY,
                    supplier_id INTEGER NOT NULL REFERENCES hdc_supplier(id),
                    material_id INTEGER NOT NULL REFERENCES hdc_material_v2(id),
                    unit_price FLOAT DEFAULT 0,
                    quantity FLOAT DEFAULT 0,
                    total_amount FLOAT DEFAULT 0,
                    payment_status VARCHAR(20) DEFAULT 'unpaid',
                    is_void BOOLEAN DEFAULT 0,
                    void_reason VARCHAR(250),
                    voided_at DATETIME,
                    created_at DATETIME,
                    updated_at DATETIME
                )
            """))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS hdc_supplier_ledger (
                    id INTEGER PRIMARY KEY,
                    supplier_id INTEGER NOT NULL REFERENCES hdc_supplier(id),
                    entry_type VARCHAR(20) NOT NULL,
                    amount FLOAT DEFAULT 0,
                    reference_type VARCHAR(50),
                    reference_id INTEGER,
                    note VARCHAR(300),
                    is_void BOOLEAN DEFAULT 0,
                    void_reason VARCHAR(250),
                    voided_at DATETIME,
                    created_at DATETIME
                )
            """))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS hdc_delivery (
                    id INTEGER PRIMARY KEY,
                    purchase_id INTEGER NOT NULL REFERENCES hdc_purchase_v2(id),
                    material_id INTEGER NOT NULL REFERENCES hdc_material_v2(id),
                    project_id INTEGER NOT NULL REFERENCES hdc_project(id),
                    stage_id INTEGER REFERENCES hdc_stage(id),
                    quantity FLOAT DEFAULT 0,
                    delivery_person VARCHAR(120),
                    is_void BOOLEAN DEFAULT 0,
                    void_reason VARCHAR(250),
                    voided_at DATETIME,
                    created_at DATETIME
                )
            """))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS hdc_usage_log_v2 (
                    id INTEGER PRIMARY KEY,
                    purchase_id INTEGER REFERENCES hdc_purchase_v2(id),
                    material_id INTEGER NOT NULL REFERENCES hdc_material_v2(id),
                    project_id INTEGER NOT NULL REFERENCES hdc_project(id),
                    stage_id INTEGER REFERENCES hdc_stage(id),
                    quantity FLOAT DEFAULT 0,
                    cost FLOAT DEFAULT 0,
                    is_void BOOLEAN DEFAULT 0,
                    void_reason VARCHAR(250),
                    voided_at DATETIME,
                    created_at DATETIME
                )
            """))
            conn.commit()
        except Exception:
            pass
        for stmt in [
            "ALTER TABLE hdc_attendance ADD COLUMN stage_id INTEGER REFERENCES hdc_stage(id)",
            "ALTER TABLE hdc_attendance ADD COLUMN activity_at DATETIME",
            "ALTER TABLE hdc_expense ADD COLUMN stage_id INTEGER REFERENCES hdc_stage(id)",
            "ALTER TABLE hdc_expense ADD COLUMN activity_at DATETIME",
            "ALTER TABLE hdc_expense ADD COLUMN tip_worker_id INTEGER REFERENCES hdc_worker(id)",
            "ALTER TABLE hdc_expense ADD COLUMN category_id INTEGER REFERENCES hdc_expense_category(id)",
            "ALTER TABLE hdc_expense ADD COLUMN is_void BOOLEAN DEFAULT 0",
            "ALTER TABLE hdc_expense ADD COLUMN void_reason VARCHAR(250)",
            "ALTER TABLE hdc_expense ADD COLUMN voided_at DATETIME",
            "ALTER TABLE hdc_subcontractor ADD COLUMN stage_id INTEGER REFERENCES hdc_stage(id)",
            "ALTER TABLE hdc_subcontractor ADD COLUMN phone VARCHAR(30)",
            "ALTER TABLE hdc_subcontractor ADD COLUMN subcontractor_code VARCHAR(20)",
            "ALTER TABLE hdc_subcontractor ADD COLUMN work_done_percentage FLOAT DEFAULT 0",
            "ALTER TABLE hdc_owner_payment ADD COLUMN activity_at DATETIME",
            "ALTER TABLE hdc_project ADD COLUMN client_phone VARCHAR(30)",
            "ALTER TABLE hdc_stage_definition ADD COLUMN project_id INTEGER REFERENCES hdc_project(id)",
            "ALTER TABLE hdc_stage_definition ADD COLUMN active_status BOOLEAN DEFAULT 1",
            "ALTER TABLE hdc_subcontract_payment ADD COLUMN activity_at DATETIME",
            "ALTER TABLE hdc_subcontract_payment ADD COLUMN project_id INTEGER REFERENCES hdc_project(id)",
            "ALTER TABLE hdc_subcontract_payment ADD COLUMN stage_id INTEGER REFERENCES hdc_stage(id)",
            "ALTER TABLE hdc_subcontract_payment ADD COLUMN entry_type VARCHAR(20)",
            "ALTER TABLE hdc_labour_ledger ADD COLUMN activity_at DATETIME",
            "ALTER TABLE hdc_time_entry ADD COLUMN activity_at DATETIME",
            "ALTER TABLE hdc_time_entry ADD COLUMN is_void BOOLEAN DEFAULT 0",
            "ALTER TABLE hdc_time_entry ADD COLUMN void_reason VARCHAR(250)",
            "ALTER TABLE hdc_time_entry ADD COLUMN voided_at DATETIME",
            "ALTER TABLE hdc_material_usage ADD COLUMN activity_at DATETIME",
            "ALTER TABLE hdc_purchase ADD COLUMN activity_at DATETIME",
            "ALTER TABLE hdc_purchase ADD COLUMN entry_type VARCHAR(20)",
            "ALTER TABLE hdc_purchase ADD COLUMN supplier_name VARCHAR(120)",
            "ALTER TABLE hdc_purchase ADD COLUMN return_ref VARCHAR(80)",
            "ALTER TABLE hdc_purchase ADD COLUMN return_reason VARCHAR(200)",
            "ALTER TABLE hdc_purchase ADD COLUMN approved_by VARCHAR(100)",
            "ALTER TABLE hdc_labour_ledger ADD COLUMN stage_id INTEGER REFERENCES hdc_stage(id)",
            "ALTER TABLE hdc_labour_ledger ADD COLUMN time_entry_id INTEGER REFERENCES hdc_time_entry(id)",
            "ALTER TABLE hdc_labour_ledger ADD COLUMN is_void BOOLEAN DEFAULT 0",
            "ALTER TABLE hdc_labour_ledger ADD COLUMN void_reason VARCHAR(250)",
            "ALTER TABLE hdc_labour_ledger ADD COLUMN voided_at DATETIME",
            "ALTER TABLE hdc_project ADD COLUMN client VARCHAR(100)",
            "ALTER TABLE hdc_project ADD COLUMN estimation_id INTEGER REFERENCES hdc_estimation(id)",
            "ALTER TABLE hdc_project ADD COLUMN budget_total FLOAT",
            "ALTER TABLE hdc_project ADD COLUMN planned_start DATE",
            "ALTER TABLE hdc_project ADD COLUMN planned_end DATE",
            "ALTER TABLE hdc_stage ADD COLUMN estimated_cost FLOAT",
            "ALTER TABLE hdc_stage ADD COLUMN progress FLOAT",
            "ALTER TABLE hdc_stage ADD COLUMN execution_mode VARCHAR(20) DEFAULT 'company'",
            "ALTER TABLE hdc_stage ADD COLUMN assigned_subcontractor_id INTEGER REFERENCES hdc_subcontractor(id)",
            "ALTER TABLE hdc_stage ADD COLUMN start_date DATE",
            "ALTER TABLE hdc_stage ADD COLUMN end_date DATE",
            "ALTER TABLE hdc_worker ADD COLUMN wage_type VARCHAR(20)",
            "ALTER TABLE hdc_worker ADD COLUMN hourly_rate FLOAT",
            "ALTER TABLE hdc_worker ADD COLUMN rate_per_sqft FLOAT",
            "ALTER TABLE hdc_subcontract_labour_attendance ADD COLUMN worker_id INTEGER REFERENCES hdc_subcontract_labour_worker(id)",
            "ALTER TABLE hdc_subcontract_labour_attendance ADD COLUMN attendance_status VARCHAR(20) DEFAULT 'Present'",
            "ALTER TABLE hdc_subcontract_labour_attendance ADD COLUMN working_hours FLOAT DEFAULT 0",
            "ALTER TABLE hdc_subcontract_labour_attendance ADD COLUMN overtime_hours FLOAT DEFAULT 0",
            "ALTER TABLE hdc_office_expense ADD COLUMN office_staff_id INTEGER REFERENCES hdc_office_staff(id)",
            "ALTER TABLE hdc_office_expense ADD COLUMN office_staff_ledger_id INTEGER REFERENCES hdc_office_staff_ledger(id)",
            "ALTER TABLE hdc_subcontract_labour_payment ADD COLUMN is_void BOOLEAN DEFAULT 0",
            "ALTER TABLE hdc_subcontract_labour_payment ADD COLUMN void_reason VARCHAR(250)",
            "ALTER TABLE hdc_subcontract_labour_payment ADD COLUMN voided_at DATETIME",
        ]:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                pass   # column already exists or table doesn't exist yet
        try:
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_stage_definition_project_name_idx ON hdc_stage_definition(project_id, name)"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_subcontractor_code_idx ON hdc_subcontractor(subcontractor_code)"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_sub_event_sub_time ON hdc_subcontract_event(subcontractor_id, created_at, id)"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_sub_labour_sub_time ON hdc_subcontract_labour_attendance(subcontractor_id, date, id)"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_sub_labour_worker_time ON hdc_subcontract_labour_attendance(worker_id, date, id)"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_sub_labour_sub_stage_worker_or_bulk_date
                ON hdc_subcontract_labour_attendance(subcontractor_id, stage_id, ifnull(worker_id, -1), date)
            """))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_sub_labour_sub_worker_daily
                ON hdc_subcontract_labour_attendance(subcontractor_id, worker_id, date)
                WHERE worker_id IS NOT NULL
            """))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_sub_lab_pay_worker_time ON hdc_subcontract_labour_payment(worker_id, date, id)"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_time_entry_active_key
                ON hdc_time_entry(worker_id, project_id, stage_id, check_in)
                WHERE is_void = 0
            """))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_office_staff_code_idx ON hdc_office_staff(staff_code)"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_office_staff_attendance_staff_date_idx ON hdc_office_staff_attendance(staff_id, date)"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_office_staff_ledger_staff_date ON hdc_office_staff_ledger(staff_id, date, id)"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_office_expense_date ON hdc_office_expense(date, id)"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_office_expense_staff_ledger ON hdc_office_expense(office_staff_ledger_id, office_staff_id, date)"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_expense_project_date ON hdc_expense(project_id, date, id)"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_expense_stage_date ON hdc_expense(stage_id, date, id)"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_owner_payment_project_date ON hdc_owner_payment(project_id, date, id)"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_time_entry_active_project_checkin
                ON hdc_time_entry(project_id, check_in, id)
                WHERE is_void = 0
            """))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_time_entry_active_worker_checkin
                ON hdc_time_entry(worker_id, check_in, id)
                WHERE is_void = 0
            """))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_sub_payment_sub_date ON hdc_subcontract_payment(subcontractor_id, date, id)"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_purchase_project_date ON hdc_purchase(project_id, date, id)"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_expense_category_name_ci ON hdc_expense_category(lower(trim(name)))"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_supplier_name_ci ON hdc_supplier(lower(trim(name))) WHERE is_void = 0"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_material_v2_name_ci ON hdc_material_v2(lower(trim(name))) WHERE is_void = 0"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_activity_log_created_at ON hdc_activity_log(created_at, id)"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_activity_log_user_created ON hdc_activity_log(user_id, created_at, id)"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_activity_log_entity ON hdc_activity_log(entity_type, entity_id, created_at, id)"))
            conn.commit()
        except Exception:
            pass
        try:
            # Row traceability: every list row is looked up by (entity_type,
            # entity_id) in hdc_user_activity to show who entered it.
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_user_activity_entity ON hdc_user_activity(entity_type, entity_id, created_at, id)"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_purchase_v2_mat_date ON hdc_purchase_v2(material_id, created_at, id)"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_delivery_mat_scope ON hdc_delivery(material_id, project_id, stage_id, created_at, id)"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_usage_v2_mat_scope ON hdc_usage_log_v2(material_id, project_id, stage_id, created_at, id)"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_usage_v2_purchase_scope ON hdc_usage_log_v2(purchase_id, project_id, created_at, id)"))
            conn.commit()
        except Exception:
            pass
        # Rebuild stage definition table if legacy schema has global UNIQUE(name).
        try:
            idx_rows = conn.execute(text("PRAGMA index_list('hdc_stage_definition')")).fetchall()
            has_global_name_unique = False
            for row in idx_rows:
                idx_name = row[1]
                is_unique = int(row[2]) == 1
                if not is_unique:
                    continue
                cols = conn.execute(text(f"PRAGMA index_info('{idx_name}')")).fetchall()
                idx_cols = [c[2] for c in cols]
                if idx_cols == ['name']:
                    has_global_name_unique = True
                    break
            if has_global_name_unique:
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS hdc_stage_definition_new (
                        id INTEGER PRIMARY KEY,
                        project_id INTEGER REFERENCES hdc_project(id),
                        name VARCHAR(120) NOT NULL,
                        default_order INTEGER DEFAULT 0,
                        active_status BOOLEAN DEFAULT 1,
                        created_at DATETIME,
                        CONSTRAINT uq_stage_definition_project_name UNIQUE (project_id, name)
                    )
                """))
                conn.execute(text("""
                    INSERT INTO hdc_stage_definition_new (id, project_id, name, default_order, active_status, created_at)
                    SELECT id, NULL, name, default_order, 1, created_at FROM hdc_stage_definition
                """))
                conn.execute(text("DROP TABLE hdc_stage_definition"))
                conn.execute(text("ALTER TABLE hdc_stage_definition_new RENAME TO hdc_stage_definition"))
                conn.commit()
        except Exception:
            conn.rollback()

        # Rebuild subcontractor table if legacy schema enforces NOT NULL project_id.
        try:
            cols = conn.execute(text("PRAGMA table_info('hdc_subcontractor')")).fetchall()
            proj_notnull = False
            for c in cols:
                # cid, name, type, notnull, dflt_value, pk
                if str(c[1]).lower() == 'project_id' and int(c[3] or 0) == 1:
                    proj_notnull = True
                    break
            if proj_notnull:
                conn.execute(text("PRAGMA foreign_keys=OFF"))
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS hdc_subcontractor_new (
                        id INTEGER PRIMARY KEY,
                        subcontractor_code VARCHAR(20),
                        project_id INTEGER REFERENCES hdc_project(id),
                        stage_id INTEGER REFERENCES hdc_stage(id),
                        name VARCHAR(100) NOT NULL,
                        phone VARCHAR(30),
                        work_type VARCHAR(100),
                        contract_type VARCHAR(20) DEFAULT 'lump_sum',
                        rate_per_sqft FLOAT DEFAULT 0,
                        total_sqft FLOAT DEFAULT 0,
                        lump_sum_amount FLOAT DEFAULT 0,
                        retention_percentage FLOAT DEFAULT 0,
                        work_done_percentage FLOAT DEFAULT 0,
                        created_at DATETIME
                    )
                """))
                conn.execute(text("""
                    INSERT INTO hdc_subcontractor_new (
                        id, subcontractor_code, project_id, stage_id, name, phone, work_type,
                        contract_type, rate_per_sqft, total_sqft, lump_sum_amount,
                        retention_percentage, work_done_percentage, created_at
                    )
                    SELECT
                        id, subcontractor_code, project_id, stage_id, name, phone, work_type,
                        contract_type, rate_per_sqft, total_sqft, lump_sum_amount,
                        retention_percentage, work_done_percentage, created_at
                    FROM hdc_subcontractor
                """))
                conn.execute(text("DROP TABLE hdc_subcontractor"))
                conn.execute(text("ALTER TABLE hdc_subcontractor_new RENAME TO hdc_subcontractor"))
                conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_subcontractor_code_idx ON hdc_subcontractor(subcontractor_code)"))
                conn.execute(text("PRAGMA foreign_keys=ON"))
                conn.commit()
        except Exception:
            try:
                conn.execute(text("PRAGMA foreign_keys=ON"))
            except Exception:
                pass
            conn.rollback()

        # Rebuild subcontract labour attendance table if legacy UNIQUE key does not include worker_id.
        try:
            idx_rows = conn.execute(text("PRAGMA index_list('hdc_subcontract_labour_attendance')")).fetchall()
            needs_rebuild = True
            for row in idx_rows:
                idx_name = row[1]
                is_unique = int(row[2]) == 1
                if not is_unique:
                    continue
                cols = conn.execute(text(f"PRAGMA index_info('{idx_name}')")).fetchall()
                idx_cols = [c[2] for c in cols]
                if idx_cols == ['subcontractor_id', 'stage_id', 'worker_id', 'date']:
                    needs_rebuild = False
                    break
            if needs_rebuild:
                conn.execute(text("PRAGMA foreign_keys=OFF"))
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS hdc_subcontract_labour_attendance_new (
                        id INTEGER PRIMARY KEY,
                        subcontractor_id INTEGER NOT NULL REFERENCES hdc_subcontractor(id),
                        project_id INTEGER REFERENCES hdc_project(id),
                        stage_id INTEGER NOT NULL REFERENCES hdc_stage(id),
                        worker_id INTEGER REFERENCES hdc_subcontract_labour_worker(id),
                        date DATE NOT NULL,
                        labour_count INTEGER DEFAULT 0,
                        wage_rate FLOAT DEFAULT 0,
                        total_labour_paid FLOAT DEFAULT 0,
                        attendance_status VARCHAR(20) DEFAULT 'Present',
                        working_hours FLOAT DEFAULT 0,
                        overtime_hours FLOAT DEFAULT 0,
                        notes VARCHAR(250),
                        activity_at DATETIME,
                        created_at DATETIME,
                        updated_at DATETIME,
                        CONSTRAINT uq_sub_labour_sub_stage_worker_date UNIQUE (subcontractor_id, stage_id, worker_id, date)
                    )
                """))
                conn.execute(text("""
                    INSERT INTO hdc_subcontract_labour_attendance_new (
                        id, subcontractor_id, project_id, stage_id, worker_id, date, labour_count,
                        wage_rate, total_labour_paid, attendance_status, working_hours, overtime_hours,
                        notes, activity_at, created_at, updated_at
                    )
                    SELECT
                        id, subcontractor_id, project_id, stage_id, worker_id, date, labour_count,
                        wage_rate, total_labour_paid, 'Present', 0, 0, notes, activity_at, created_at, updated_at
                    FROM hdc_subcontract_labour_attendance
                """))
                conn.execute(text("DROP TABLE hdc_subcontract_labour_attendance"))
                conn.execute(text("ALTER TABLE hdc_subcontract_labour_attendance_new RENAME TO hdc_subcontract_labour_attendance"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_sub_labour_sub_time ON hdc_subcontract_labour_attendance(subcontractor_id, date, id)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_sub_labour_worker_time ON hdc_subcontract_labour_attendance(worker_id, date, id)"))
                conn.execute(text("""
                    CREATE UNIQUE INDEX IF NOT EXISTS uq_sub_labour_sub_stage_worker_or_bulk_date
                    ON hdc_subcontract_labour_attendance(subcontractor_id, stage_id, ifnull(worker_id, -1), date)
                """))
                conn.execute(text("PRAGMA foreign_keys=ON"))
                conn.commit()
        except Exception:
            try:
                conn.execute(text("PRAGMA foreign_keys=ON"))
            except Exception:
                pass
            conn.rollback()

        # Expense category migration: text -> category_id, then rebuild to drop legacy text column.
        try:
            cols = conn.execute(text("PRAGMA table_info('hdc_expense')")).fetchall()
            col_names = [str(c[1]).lower() for c in cols]
            has_category = 'category' in col_names
            has_category_id = 'category_id' in col_names
            if has_category_id:
                # Ensure default fallback category exists.
                conn.execute(text("""
                    INSERT INTO hdc_expense_category(name, active_status, created_at)
                    SELECT 'Misc', 1, CURRENT_TIMESTAMP
                    WHERE NOT EXISTS (
                        SELECT 1 FROM hdc_expense_category WHERE lower(trim(name)) = 'misc'
                    )
                """))
                if has_category:
                    conn.execute(text("""
                        INSERT INTO hdc_expense_category(name, active_status, created_at)
                        SELECT DISTINCT trim(category), 1, CURRENT_TIMESTAMP
                        FROM hdc_expense
                        WHERE category IS NOT NULL AND trim(category) <> ''
                          AND lower(trim(category)) NOT IN (
                              SELECT lower(trim(name)) FROM hdc_expense_category
                          )
                    """))
                    conn.execute(text("""
                        UPDATE hdc_expense
                        SET category_id = (
                            SELECT id FROM hdc_expense_category c
                            WHERE lower(trim(c.name)) = lower(trim(hdc_expense.category))
                            LIMIT 1
                        )
                        WHERE category_id IS NULL
                          AND category IS NOT NULL
                          AND trim(category) <> ''
                    """))
                conn.execute(text("""
                    UPDATE hdc_expense
                    SET category_id = (
                        SELECT id FROM hdc_expense_category
                        WHERE lower(trim(name)) = 'misc'
                        LIMIT 1
                    )
                    WHERE category_id IS NULL
                """))
                # Rebuild only if legacy text column still exists.
                if has_category:
                    conn.execute(text("PRAGMA foreign_keys=OFF"))
                    conn.execute(text("""
                        CREATE TABLE IF NOT EXISTS hdc_expense_new (
                            id INTEGER PRIMARY KEY,
                            project_id INTEGER NOT NULL REFERENCES hdc_project(id),
                            stage_id INTEGER REFERENCES hdc_stage(id),
                            tip_worker_id INTEGER REFERENCES hdc_worker(id),
                            category_id INTEGER NOT NULL REFERENCES hdc_expense_category(id),
                            amount FLOAT DEFAULT 0,
                            date DATE,
                            remarks VARCHAR(200),
                            is_void BOOLEAN DEFAULT 0,
                            void_reason VARCHAR(250),
                            voided_at DATETIME,
                            activity_at DATETIME,
                            created_at DATETIME
                        )
                    """))
                    conn.execute(text("""
                        INSERT INTO hdc_expense_new (
                            id, project_id, stage_id, tip_worker_id, category_id,
                            amount, date, remarks, is_void, void_reason, voided_at,
                            activity_at, created_at
                        )
                        SELECT
                            id, project_id, stage_id, tip_worker_id,
                            COALESCE(
                                category_id,
                                (SELECT id FROM hdc_expense_category WHERE lower(trim(name)) = 'misc' LIMIT 1)
                            ),
                            amount, date, remarks,
                            COALESCE(is_void, 0), void_reason, voided_at,
                            activity_at, created_at
                        FROM hdc_expense
                    """))
                    conn.execute(text("DROP TABLE hdc_expense"))
                    conn.execute(text("ALTER TABLE hdc_expense_new RENAME TO hdc_expense"))
                    conn.execute(text("PRAGMA foreign_keys=ON"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_expense_project_date ON hdc_expense(project_id, date, id)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_expense_stage_date ON hdc_expense(stage_id, date, id)"))
                conn.commit()
        except Exception:
            try:
                conn.execute(text("PRAGMA foreign_keys=ON"))
            except Exception:
                pass
            conn.rollback()

        # Backfill activity timestamps for legacy rows.
        backfills = [
            "UPDATE hdc_owner_payment SET activity_at = COALESCE(activity_at, created_at, datetime(date || ' 12:00:00'))",
            "UPDATE hdc_subcontract_payment SET activity_at = COALESCE(activity_at, created_at, datetime(date || ' 12:00:00'))",
            "UPDATE hdc_subcontract_payment SET entry_type = COALESCE(NULLIF(entry_type, ''), 'payment')",
            "UPDATE hdc_labour_ledger SET activity_at = COALESCE(activity_at, created_at, datetime(date || ' 12:00:00'))",
            "UPDATE hdc_expense SET activity_at = COALESCE(activity_at, created_at, datetime(date || ' 12:00:00'))",
            "UPDATE hdc_purchase SET activity_at = COALESCE(activity_at, created_at, datetime(date || ' 12:00:00'))",
            "UPDATE hdc_purchase SET entry_type = COALESCE(NULLIF(entry_type, ''), 'purchase')",
            "UPDATE hdc_material_usage SET activity_at = COALESCE(activity_at, created_at, datetime(used_at || ' 12:00:00'))",
            "UPDATE hdc_attendance SET activity_at = COALESCE(activity_at, created_at, datetime(date || ' 12:00:00'))",
            "UPDATE hdc_time_entry SET activity_at = COALESCE(activity_at, check_in, created_at)",
            "UPDATE hdc_time_entry SET is_void = COALESCE(is_void, 0)",
            "UPDATE hdc_labour_ledger SET is_void = COALESCE(is_void, 0)",
            "UPDATE hdc_subcontractor SET work_done_percentage = COALESCE(work_done_percentage, 0)",
            "UPDATE hdc_subcontractor SET subcontractor_code = COALESCE(NULLIF(subcontractor_code, ''), ('SUB-' || printf('%04d', id)))",
            "UPDATE hdc_stage SET execution_mode = COALESCE(NULLIF(execution_mode, ''), 'company')",
            "UPDATE hdc_subcontract_attendance SET activity_at = COALESCE(activity_at, created_at, datetime(date || ' 12:00:00'))",
            "UPDATE hdc_subcontract_labour_attendance SET activity_at = COALESCE(activity_at, created_at, datetime(date || ' 12:00:00'))",
            "UPDATE hdc_subcontract_labour_attendance SET updated_at = COALESCE(updated_at, activity_at, created_at, datetime(date || ' 12:00:00'))",
            "UPDATE hdc_subcontract_labour_attendance SET total_labour_paid = COALESCE(total_labour_paid, 0)",
            "UPDATE hdc_subcontract_labour_attendance SET wage_rate = COALESCE(wage_rate, 0)",
            "UPDATE hdc_subcontract_labour_attendance SET labour_count = COALESCE(labour_count, 0)",
            "UPDATE hdc_subcontract_labour_attendance SET attendance_status = COALESCE(NULLIF(attendance_status, ''), CASE WHEN COALESCE(labour_count, 0) > 0 THEN 'Present' ELSE 'Absent' END)",
            "UPDATE hdc_subcontract_labour_attendance SET working_hours = COALESCE(working_hours, 0)",
            "UPDATE hdc_subcontract_labour_attendance SET overtime_hours = COALESCE(overtime_hours, 0)",
            "UPDATE hdc_subcontract_labour_payment SET activity_at = COALESCE(activity_at, created_at, datetime(date || ' 12:00:00'))"
        ]
        for stmt in backfills:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                pass
        try:
            conn.execute(text("""
                INSERT INTO hdc_subcontract_event
                    (subcontractor_id, project_id, stage_id, actor_user_id, event_type, from_value, to_value, amount, notes, created_at)
                SELECT
                    s.id, s.project_id, s.stage_id, NULL, 'create', '', COALESCE(s.subcontractor_code, ''), 0,
                    'Backfill: subcontractor profile', COALESCE(s.created_at, CURRENT_TIMESTAMP)
                FROM hdc_subcontractor s
                WHERE NOT EXISTS (
                    SELECT 1 FROM hdc_subcontract_event e
                    WHERE e.subcontractor_id = s.id AND e.event_type = 'create'
                )
            """))
            conn.commit()
        except Exception:
            pass

        try:
            conn.execute(text("""
                INSERT INTO hdc_subcontract_event
                    (subcontractor_id, project_id, stage_id, actor_user_id, event_type, from_value, to_value, amount, notes, created_at)
                SELECT
                    s.id, s.project_id, s.stage_id, NULL, 'shift', 'backfill',
                    COALESCE(st.name, ('STAGE#' || s.stage_id)), 0,
                    'Backfill: stage assignment',
                    COALESCE(st.created_at, s.created_at, CURRENT_TIMESTAMP)
                FROM hdc_subcontractor s
                LEFT JOIN hdc_stage st ON st.id = s.stage_id
                WHERE s.stage_id IS NOT NULL
                  AND s.project_id IS NOT NULL
                  AND NOT EXISTS (
                    SELECT 1 FROM hdc_subcontract_event e
                    WHERE e.subcontractor_id = s.id
                      AND e.stage_id = s.stage_id
                      AND e.event_type IN ('shift', 'reassign')
                )
            """))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("""
                INSERT INTO hdc_subcontract_event
                    (subcontractor_id, project_id, stage_id, actor_user_id, event_type, from_value, to_value, amount, notes, created_at)
                SELECT
                    s.id, s.project_id, s.stage_id, NULL, 'price_update', '',
                    COALESCE(s.contract_type, ''),
                    CASE
                        WHEN COALESCE(s.contract_type, '') = 'sqft' THEN COALESCE(s.rate_per_sqft, 0) * COALESCE(s.total_sqft, 0)
                        ELSE COALESCE(s.lump_sum_amount, 0)
                    END,
                    ('Backfill: Rate ' || COALESCE(s.rate_per_sqft, 0) || ' | Sqft ' || COALESCE(s.total_sqft, 0) || ' | Lump ' || COALESCE(s.lump_sum_amount, 0)),
                    COALESCE(s.created_at, CURRENT_TIMESTAMP)
                FROM hdc_subcontractor s
                WHERE s.stage_id IS NOT NULL
                  AND NOT EXISTS (
                    SELECT 1 FROM hdc_subcontract_event e
                    WHERE e.subcontractor_id = s.id
                      AND e.stage_id = s.stage_id
                      AND e.event_type = 'price_update'
                )
            """))
            conn.commit()
        except Exception:
            pass


def _ensure_table_columns_sqlite(table_name, columns):
    """Best-effort column auto-heal for SQLite tables on legacy DB files."""
    if not table_name or not columns:
        return
    with db.engine.connect() as conn:
        try:
            rows = conn.execute(text(f"PRAGMA table_info('{table_name}')")).fetchall()
            existing = {str(r[1]).lower() for r in rows}
        except Exception:
            return
        for col_name, col_ddl in columns.items():
            key = str(col_name or '').strip().lower()
            if not key or key in existing:
                continue
            try:
                conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {col_ddl}"))
                conn.commit()
                existing.add(key)
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass


def _ensure_purchase_v2_schema():
    _ensure_table_columns_sqlite('hdc_purchase_v2', {
        'date':       "date DATE",
        'notes':      "notes VARCHAR(300)",
        'challan_no': "challan_no VARCHAR(80)",
    })
    _ensure_table_columns_sqlite('hdc_delivery', {
        'date':  "date DATE",
        'notes': "notes VARCHAR(300)",
    })
    _ensure_table_columns_sqlite('hdc_usage_log_v2', {
        'purchase_id': "purchase_id INTEGER",
        'date':  "date DATE",
        'notes': "notes VARCHAR(300)",
    })
    _ensure_table_columns_sqlite('hdc_supplier', {
        'address': "address VARCHAR(250)",
        'email':   "email VARCHAR(120)",
    })
    _ensure_table_columns_sqlite('hdc_office_expense', {
        'is_void': "is_void BOOLEAN DEFAULT 0",
        'void_reason': "void_reason VARCHAR(250)",
        'voided_at': "voided_at DATETIME",
    })
    try:
        with db.engine.connect() as conn:
            conn.execute(text("UPDATE hdc_office_expense SET is_void = COALESCE(is_void, 0)"))
            conn.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass


def _ensure_owner_payment_void_schema():
    _ensure_table_columns_sqlite('hdc_owner_payment', {
        'received_to_account_id': "received_to_account_id INTEGER REFERENCES hdc_account(id)",
        'is_void': "is_void BOOLEAN DEFAULT 0",
        'void_reason': "void_reason VARCHAR(250)",
        'voided_at': "voided_at DATETIME",
    })
    try:
        with db.engine.connect() as conn:
            conn.execute(text("UPDATE hdc_owner_payment SET is_void = COALESCE(is_void, 0)"))
            conn.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass


def _ensure_accounts_schema():
    with db.engine.connect() as conn:
        try:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS hdc_account (
                    id INTEGER PRIMARY KEY,
                    name VARCHAR(120) NOT NULL,
                    type VARCHAR(20) NOT NULL,
                    opening_balance FLOAT DEFAULT 0,
                    bank_name VARCHAR(120),
                    account_number VARCHAR(80),
                    iban VARCHAR(80),
                    auto_generated BOOLEAN DEFAULT 0,
                    auto_source VARCHAR(40),
                    status VARCHAR(20) DEFAULT 'active',
                    is_void BOOLEAN DEFAULT 0,
                    created_at DATETIME
                )
            """))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS hdc_account_txn (
                    id INTEGER PRIMARY KEY,
                    date DATE NOT NULL,
                    amount FLOAT NOT NULL,
                    type VARCHAR(40),
                    from_account_id INTEGER NOT NULL REFERENCES hdc_account(id),
                    to_account_id INTEGER REFERENCES hdc_account(id),
                    executed_by_account_id INTEGER NOT NULL REFERENCES hdc_account(id),
                    project_id INTEGER REFERENCES hdc_project(id),
                    stage_id INTEGER REFERENCES hdc_stage(id),
                    related_entity_type VARCHAR(40),
                    related_entity_id INTEGER,
                    party_name VARCHAR(120),
                    category VARCHAR(20) NOT NULL,
                    note VARCHAR(400),
                    reference_id VARCHAR(120),
                    group_id VARCHAR(64),
                    source_type VARCHAR(80),
                    source_id INTEGER,
                    is_void BOOLEAN DEFAULT 0,
                    created_at DATETIME
                )
            """))
            conn.commit()
        except Exception:
            pass

        idx_sql = [
            "CREATE INDEX IF NOT EXISTS idx_hdc_account_txn_from_date_id ON hdc_account_txn(from_account_id, date, id)",
            "CREATE INDEX IF NOT EXISTS idx_hdc_account_txn_to_date_id ON hdc_account_txn(to_account_id, date, id)",
            "CREATE INDEX IF NOT EXISTS idx_hdc_account_txn_exec_date_id ON hdc_account_txn(executed_by_account_id, date, id)",
            "CREATE INDEX IF NOT EXISTS idx_hdc_account_txn_stage_date_id ON hdc_account_txn(stage_id, date, id)",
            "CREATE INDEX IF NOT EXISTS idx_hdc_account_txn_group_id ON hdc_account_txn(group_id)",
            "CREATE INDEX IF NOT EXISTS idx_hdc_account_txn_reference_id ON hdc_account_txn(reference_id)",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_hdc_account_txn_source ON hdc_account_txn(source_type, source_id) WHERE source_type IS NOT NULL AND source_id IS NOT NULL",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_hdc_account_name_active_ci ON hdc_account(lower(trim(name))) WHERE is_void = 0",
        ]
        for sql in idx_sql:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass

    _ensure_table_columns_sqlite('hdc_account', {
        'name': "name VARCHAR(120)",
        'type': "type VARCHAR(20)",
        'opening_balance': "opening_balance FLOAT DEFAULT 0",
        'bank_name': "bank_name VARCHAR(120)",
        'account_number': "account_number VARCHAR(80)",
        'iban': "iban VARCHAR(80)",
        'auto_generated': "auto_generated BOOLEAN DEFAULT 0",
        'auto_source': "auto_source VARCHAR(40)",
        'status': "status VARCHAR(20) DEFAULT 'active'",
        'is_void': "is_void BOOLEAN DEFAULT 0",
        'created_at': "created_at DATETIME",
    })
    _ensure_table_columns_sqlite('hdc_account_txn', {
        'date': "date DATE",
        'amount': "amount FLOAT DEFAULT 0",
        'type': "type VARCHAR(40)",
        'from_account_id': "from_account_id INTEGER",
        'to_account_id': "to_account_id INTEGER",
        'executed_by_account_id': "executed_by_account_id INTEGER",
        'project_id': "project_id INTEGER REFERENCES hdc_project(id)",
        'stage_id': "stage_id INTEGER REFERENCES hdc_stage(id)",
        'related_entity_type': "related_entity_type VARCHAR(40)",
        'related_entity_id': "related_entity_id INTEGER",
        'party_name': "party_name VARCHAR(120)",
        'category': "category VARCHAR(20)",
        'note': "note VARCHAR(400)",
        'reference_id': "reference_id VARCHAR(120)",
        'group_id': "group_id VARCHAR(64)",
        'source_type': "source_type VARCHAR(80)",
        'source_id': "source_id INTEGER",
        'is_void': "is_void BOOLEAN DEFAULT 0",
        'created_at': "created_at DATETIME",
    })
    # --- Cash Flow v2: additive columns on the unified accounts tables ------
    # (the new register tables themselves are created by
    #  _ensure_cashflow_schema(); these ALTERs only widen existing tables).
    _ensure_table_columns_sqlite('hdc_account', {
        'opening_balance_minor': "opening_balance_minor BIGINT",
        'class_category':       "class_category VARCHAR(50)",
        'class_subcategory':    "class_subcategory VARCHAR(80)",
        'class_account_type':   "class_account_type VARCHAR(100)",
        'channel':              "channel VARCHAR(30)",
        'cash_location':        "cash_location VARCHAR(120)",
        'cash_responsible':     "cash_responsible VARCHAR(120)",
        'wallet_provider':      "wallet_provider VARCHAR(100)",
        'wallet_number':        "wallet_number VARCHAR(80)",
        'wallet_holder':        "wallet_holder VARCHAR(120)",
        'linked_entity_type':   "linked_entity_type VARCHAR(30)",
        'linked_party_name':    "linked_party_name VARCHAR(160)",
        'note':                 "note VARCHAR(500)",
        'updated_by':           "updated_by VARCHAR(80)",
        'updated_at':           "updated_at DATETIME",
    })
    _ensure_table_columns_sqlite('hdc_account_txn', {
        'amount_minor':         "amount_minor BIGINT",
        'reversal_of_txn_id':   "reversal_of_txn_id INTEGER REFERENCES hdc_account_txn(id)",
        'reversed_by_txn_id':   "reversed_by_txn_id INTEGER REFERENCES hdc_account_txn(id)",
        'reconciliation_id':    "reconciliation_id INTEGER REFERENCES hdc_account_reconciliation(id)",
        'void_reason':          "void_reason VARCHAR(300)",
        'voided_by':            "voided_by VARCHAR(80)",
        'voided_at':            "voided_at DATETIME",
        'reason':               "reason VARCHAR(300)",
        'idempotency_key':      "idempotency_key VARCHAR(64)",
        'updated_at':           "updated_at DATETIME",
    })


def _ensure_cashflow_schema():
    """Create the Cash Flow v2 register / reconciliation tables.

    ``CREATE TABLE IF NOT EXISTS`` + best-effort ``ALTER TABLE ADD COLUMN``
    mirrors every other migration in this file, so an existing production
    database is widened in place instead of being rebuilt.
    """
    tables = [
        """
        CREATE TABLE IF NOT EXISTS hdc_cash_flow_category (
            id INTEGER PRIMARY KEY,
            name VARCHAR(120) NOT NULL,
            direction VARCHAR(10),
            is_active BOOLEAN DEFAULT 1,
            sort_order INTEGER DEFAULT 0,
            notes VARCHAR(300),
            created_at DATETIME,
            updated_at DATETIME
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS hdc_cash_flow_subcategory (
            id INTEGER PRIMARY KEY,
            category_id INTEGER NOT NULL REFERENCES hdc_cash_flow_category(id),
            name VARCHAR(120) NOT NULL,
            is_active BOOLEAN DEFAULT 1,
            notes VARCHAR(300),
            created_at DATETIME,
            updated_at DATETIME
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS hdc_cash_flow_party (
            id INTEGER PRIMARY KEY,
            name VARCHAR(160) NOT NULL,
            party_type VARCHAR(40),
            phone VARCHAR(40),
            note VARCHAR(300),
            is_active BOOLEAN DEFAULT 1,
            created_at DATETIME,
            updated_at DATETIME
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS hdc_account_reconciliation (
            id INTEGER PRIMARY KEY,
            account_id INTEGER NOT NULL REFERENCES hdc_account(id),
            previous_reconciliation_id INTEGER REFERENCES hdc_account_reconciliation(id),
            adjustment_transaction_id INTEGER,
            reconciliation_date DATE NOT NULL,
            period_start_at DATETIME,
            period_end_at DATETIME,
            previous_balance FLOAT DEFAULT 0,
            opening_balance FLOAT DEFAULT 0,
            transaction_in FLOAT DEFAULT 0,
            transaction_out FLOAT DEFAULT 0,
            transaction_net FLOAT DEFAULT 0,
            expected_balance FLOAT DEFAULT 0,
            actual_balance FLOAT DEFAULT 0,
            difference FLOAT DEFAULT 0,
            final_reconciled_balance FLOAT DEFAULT 0,
            previous_balance_minor BIGINT,
            opening_balance_minor BIGINT,
            transaction_in_minor BIGINT,
            transaction_out_minor BIGINT,
            transaction_net_minor BIGINT,
            expected_balance_minor BIGINT,
            actual_balance_minor BIGINT,
            difference_minor BIGINT,
            final_reconciled_balance_minor BIGINT,
            difference_type VARCHAR(20),
            status VARCHAR(20),
            note VARCHAR(500),
            created_by VARCHAR(80),
            created_at DATETIME
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS hdc_cash_flow_entry (
            id INTEGER PRIMARY KEY,
            direction VARCHAR(10) NOT NULL,
            amount FLOAT DEFAULT 0,
            amount_minor BIGINT,
            account_id INTEGER REFERENCES hdc_account(id),
            destination_account_id INTEGER REFERENCES hdc_account(id),
            category_id INTEGER REFERENCES hdc_cash_flow_category(id),
            subcategory_id INTEGER REFERENCES hdc_cash_flow_subcategory(id),
            party_id INTEGER REFERENCES hdc_cash_flow_party(id),
            party_name VARCHAR(160),
            party_type VARCHAR(40),
            description VARCHAR(200),
            note VARCHAR(500),
            reference VARCHAR(80),
            date_posted DATETIME,
            project_id INTEGER REFERENCES hdc_project(id),
            stage_id INTEGER REFERENCES hdc_stage(id),
            created_by VARCHAR(80),
            updated_by VARCHAR(80),
            source_type VARCHAR(50),
            source_id INTEGER,
            account_tx_id INTEGER REFERENCES hdc_account_txn(id),
            is_void BOOLEAN DEFAULT 0,
            voided_at DATETIME,
            voided_by VARCHAR(80),
            void_reason VARCHAR(300),
            amends_entry_id INTEGER REFERENCES hdc_cash_flow_entry(id),
            superseded_by_entry_id INTEGER REFERENCES hdc_cash_flow_entry(id),
            reconciliation_id INTEGER REFERENCES hdc_account_reconciliation(id),
            idempotency_key VARCHAR(64),
            revision INTEGER DEFAULT 1,
            created_at DATETIME,
            updated_at DATETIME
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS hdc_cash_flow_entry_audit (
            id INTEGER PRIMARY KEY,
            entry_id INTEGER NOT NULL REFERENCES hdc_cash_flow_entry(id),
            action VARCHAR(20) NOT NULL,
            before_json TEXT,
            after_json TEXT,
            reason VARCHAR(300),
            changed_by VARCHAR(80),
            changed_at DATETIME
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS hdc_cash_day_lock (
            id INTEGER PRIMARY KEY,
            lock_date DATE NOT NULL UNIQUE,
            total_expected FLOAT DEFAULT 0,
            total_counted FLOAT DEFAULT 0,
            difference FLOAT DEFAULT 0,
            note VARCHAR(500),
            locked_by VARCHAR(80),
            locked_at DATETIME,
            updated_at DATETIME
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS hdc_cash_day_position (
            id INTEGER PRIMARY KEY,
            position_date DATE NOT NULL,
            account_id INTEGER NOT NULL REFERENCES hdc_account(id),
            account_name VARCHAR(120),
            opening FLOAT DEFAULT 0,
            opening_minor BIGINT,
            amount_in FLOAT DEFAULT 0,
            amount_in_minor BIGINT,
            amount_out FLOAT DEFAULT 0,
            amount_out_minor BIGINT,
            transfer_in FLOAT DEFAULT 0,
            transfer_in_minor BIGINT,
            transfer_out FLOAT DEFAULT 0,
            transfer_out_minor BIGINT,
            expected_closing FLOAT DEFAULT 0,
            expected_closing_minor BIGINT,
            counted FLOAT,
            counted_minor BIGINT,
            difference FLOAT,
            difference_minor BIGINT,
            is_locked BOOLEAN DEFAULT 0,
            locked_by VARCHAR(80),
            locked_at DATETIME,
            updated_by VARCHAR(80),
            updated_at DATETIME,
            UNIQUE(position_date, account_id)
        )
        """,
    ]
    with db.engine.connect() as conn:
        for ddl in tables:
            try:
                conn.execute(text(ddl))
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
        idx_sql = [
            "CREATE INDEX IF NOT EXISTS idx_hdc_cfe_date ON hdc_cash_flow_entry(date_posted, id)",
            "CREATE INDEX IF NOT EXISTS idx_hdc_cfe_account ON hdc_cash_flow_entry(account_id, date_posted)",
            "CREATE INDEX IF NOT EXISTS idx_hdc_cfe_direction ON hdc_cash_flow_entry(direction, is_void)",
            "CREATE INDEX IF NOT EXISTS idx_hdc_cfe_category ON hdc_cash_flow_entry(category_id)",
            "CREATE INDEX IF NOT EXISTS idx_hdc_cfe_project ON hdc_cash_flow_entry(project_id)",
            "CREATE INDEX IF NOT EXISTS idx_hdc_cfe_tx ON hdc_cash_flow_entry(account_tx_id)",
            "CREATE INDEX IF NOT EXISTS idx_hdc_cfe_audit_entry ON hdc_cash_flow_entry_audit(entry_id, id)",
            "CREATE INDEX IF NOT EXISTS idx_hdc_accrec_account_date ON hdc_account_reconciliation(account_id, reconciliation_date)",
            "CREATE INDEX IF NOT EXISTS idx_hdc_cdp_account ON hdc_cash_day_position(account_id, position_date)",
        ]
        for sql in idx_sql:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass

    _ensure_table_columns_sqlite('hdc_cash_flow_entry', {
        'project_id': "project_id INTEGER REFERENCES hdc_project(id)",
        'stage_id': "stage_id INTEGER REFERENCES hdc_stage(id)",
        'amount_minor': "amount_minor BIGINT",
        'amends_entry_id': "amends_entry_id INTEGER REFERENCES hdc_cash_flow_entry(id)",
        'superseded_by_entry_id': "superseded_by_entry_id INTEGER REFERENCES hdc_cash_flow_entry(id)",
    })


def _ensure_tool_rental_schema():
    """Create HDC Tool Rental tables - flexible rental with partial returns, transfers, tracking."""
    tables = [
        """
        CREATE TABLE IF NOT EXISTS hdc_tool_category (
            id INTEGER PRIMARY KEY,
            name VARCHAR(120) NOT NULL UNIQUE,
            description VARCHAR(300),
            active_status BOOLEAN DEFAULT 1,
            created_at DATETIME
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS hdc_tool (
            id INTEGER PRIMARY KEY,
            tool_code VARCHAR(30) NOT NULL UNIQUE,
            name VARCHAR(150) NOT NULL,
            category_id INTEGER REFERENCES hdc_tool_category(id),
            description VARCHAR(500),
            unit VARCHAR(30) DEFAULT 'pcs',
            total_quantity FLOAT DEFAULT 0,
            purchase_cost FLOAT DEFAULT 0,
            rental_rate_per_day FLOAT DEFAULT 0,
            condition VARCHAR(30) DEFAULT 'good',
            status VARCHAR(30) DEFAULT 'active',
            is_void BOOLEAN DEFAULT 0,
            created_at DATETIME,
            updated_at DATETIME
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS hdc_tool_rental (
            id INTEGER PRIMARY KEY,
            rental_code VARCHAR(30) NOT NULL UNIQUE,
            renter_type VARCHAR(20) DEFAULT 'internal',
            project_id INTEGER REFERENCES hdc_project(id),
            stage_id INTEGER REFERENCES hdc_stage(id),
            customer_name VARCHAR(150),
            customer_phone VARCHAR(40),
            customer_address VARCHAR(300),
            rental_date DATE,
            expected_return_date DATE,
            billing_type VARCHAR(30) DEFAULT 'fixed_fee',
            billing_notes VARCHAR(300),
            total_rented_qty FLOAT DEFAULT 0,
            total_amount FLOAT DEFAULT 0,
            total_paid FLOAT DEFAULT 0,
            total_returned_qty FLOAT DEFAULT 0,
            status VARCHAR(30) DEFAULT 'active',
            payment_status VARCHAR(30) DEFAULT 'unpaid',
            notes TEXT,
            created_by INTEGER REFERENCES hdc_user(id),
            created_at DATETIME,
            updated_at DATETIME,
            is_void BOOLEAN DEFAULT 0
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS hdc_tool_rental_item (
            id INTEGER PRIMARY KEY,
            rental_id INTEGER NOT NULL REFERENCES hdc_tool_rental(id),
            tool_id INTEGER NOT NULL REFERENCES hdc_tool(id),
            qty_rented FLOAT DEFAULT 0,
            qty_returned FLOAT DEFAULT 0,
            qty_pending FLOAT DEFAULT 0,
            rate FLOAT DEFAULT 0,
            amount FLOAT DEFAULT 0,
            notes VARCHAR(300),
            created_at DATETIME
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS hdc_tool_rental_return (
            id INTEGER PRIMARY KEY,
            rental_id INTEGER NOT NULL REFERENCES hdc_tool_rental(id),
            return_date DATE,
            return_type VARCHAR(20) DEFAULT 'partial',
            payment_type VARCHAR(20) DEFAULT 'partial',
            total_tools_returned FLOAT DEFAULT 0,
            amount_paid FLOAT DEFAULT 0,
            notes VARCHAR(500),
            created_at DATETIME,
            created_by INTEGER REFERENCES hdc_user(id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS hdc_tool_rental_return_item (
            id INTEGER PRIMARY KEY,
            return_id INTEGER NOT NULL REFERENCES hdc_tool_rental_return(id),
            rental_item_id INTEGER NOT NULL REFERENCES hdc_tool_rental_item(id),
            tool_id INTEGER NOT NULL REFERENCES hdc_tool(id),
            qty_returned FLOAT DEFAULT 0,
            condition_notes VARCHAR(300)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS hdc_tool_rental_payment (
            id INTEGER PRIMARY KEY,
            rental_id INTEGER NOT NULL REFERENCES hdc_tool_rental(id),
            return_id INTEGER REFERENCES hdc_tool_rental_return(id),
            payment_date DATE,
            amount FLOAT DEFAULT 0,
            payment_mode VARCHAR(30) DEFAULT 'cash',
            received_to_account_id INTEGER REFERENCES hdc_account(id),
            reference VARCHAR(120),
            notes VARCHAR(300),
            is_void BOOLEAN DEFAULT 0,
            void_reason VARCHAR(250),
            voided_at DATETIME,
            created_at DATETIME,
            created_by INTEGER REFERENCES hdc_user(id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS hdc_tool_rental_account_txn (
            id INTEGER PRIMARY KEY,
            payment_id INTEGER NOT NULL REFERENCES hdc_tool_rental_payment(id),
            account_txn_id INTEGER NOT NULL REFERENCES hdc_account_txn(id),
            created_at DATETIME
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS hdc_tool_rental_transfer (
            id INTEGER PRIMARY KEY,
            rental_id INTEGER NOT NULL REFERENCES hdc_tool_rental(id),
            from_type VARCHAR(20) DEFAULT 'site',
            from_project_id INTEGER REFERENCES hdc_project(id),
            from_stage_id INTEGER REFERENCES hdc_stage(id),
            from_customer_name VARCHAR(150),
            from_location_label VARCHAR(300),
            to_type VARCHAR(20) DEFAULT 'site',
            to_project_id INTEGER REFERENCES hdc_project(id),
            to_stage_id INTEGER REFERENCES hdc_stage(id),
            to_customer_name VARCHAR(150),
            to_location_label VARCHAR(300),
            qty_transferred FLOAT DEFAULT 0,
            transfer_date DATE,
            notes VARCHAR(500),
            created_at DATETIME,
            created_by INTEGER REFERENCES hdc_user(id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS hdc_tool_rental_transfer_item (
            id INTEGER PRIMARY KEY,
            transfer_id INTEGER NOT NULL REFERENCES hdc_tool_rental_transfer(id),
            rental_item_id INTEGER NOT NULL REFERENCES hdc_tool_rental_item(id),
            tool_id INTEGER NOT NULL REFERENCES hdc_tool(id),
            qty_transferred FLOAT DEFAULT 0,
            created_at DATETIME
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS hdc_tool_movement_log (
            id INTEGER PRIMARY KEY,
            tool_id INTEGER NOT NULL REFERENCES hdc_tool(id),
            rental_id INTEGER REFERENCES hdc_tool_rental(id),
            transfer_id INTEGER REFERENCES hdc_tool_rental_transfer(id),
            return_id INTEGER REFERENCES hdc_tool_rental_return(id),
            movement_type VARCHAR(30) DEFAULT 'rental_out',
            from_location_label VARCHAR(300),
            to_location_label VARCHAR(300),
            qty FLOAT DEFAULT 0,
            timestamp DATETIME,
            notes VARCHAR(500)
        )
        """,
    ]
    with db.engine.connect() as conn:
        for ddl in tables:
            try:
                conn.execute(text(ddl))
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
        idx_sql = [
            "CREATE INDEX IF NOT EXISTS idx_tool_rental_date ON hdc_tool_rental(rental_date, id)",
            "CREATE INDEX IF NOT EXISTS idx_tool_rental_project ON hdc_tool_rental(project_id)",
            "CREATE INDEX IF NOT EXISTS idx_tool_rental_customer ON hdc_tool_rental(customer_name)",
            "CREATE INDEX IF NOT EXISTS idx_tool_rental_status ON hdc_tool_rental(status)",
            "CREATE INDEX IF NOT EXISTS idx_tool_rental_item_rental ON hdc_tool_rental_item(rental_id)",
            "CREATE INDEX IF NOT EXISTS idx_tool_rental_item_tool ON hdc_tool_rental_item(tool_id)",
            "CREATE INDEX IF NOT EXISTS idx_tool_movement_tool_time ON hdc_tool_movement_log(tool_id, timestamp, id)",
            "CREATE INDEX IF NOT EXISTS idx_tool_movement_rental ON hdc_tool_movement_log(rental_id)",
            "CREATE INDEX IF NOT EXISTS idx_tool_transfer_rental ON hdc_tool_rental_transfer(rental_id, transfer_date)",
            "CREATE INDEX IF NOT EXISTS idx_tool_transfer_item_transfer ON hdc_tool_rental_transfer_item(transfer_id)",
            "CREATE INDEX IF NOT EXISTS idx_tool_transfer_item_rental_item ON hdc_tool_rental_transfer_item(rental_item_id)",
            "CREATE INDEX IF NOT EXISTS idx_tool_transfer_item_tool ON hdc_tool_rental_transfer_item(tool_id)",
        ]
        for sql in idx_sql:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass

    # add columns if legacy
    _ensure_table_columns_sqlite('hdc_tool', {
        'tool_code': "tool_code VARCHAR(30)",
        'purchase_cost': "purchase_cost FLOAT DEFAULT 0",
        'rental_rate_per_day': "rental_rate_per_day FLOAT DEFAULT 0",
        'is_void': "is_void BOOLEAN DEFAULT 0",
    })
    _ensure_table_columns_sqlite('hdc_tool_rental', {
        'customer_phone': "customer_phone VARCHAR(40)",
        'billing_notes': "billing_notes VARCHAR(300)",
        'total_returned_qty': "total_returned_qty FLOAT DEFAULT 0",
        'payment_status': "payment_status VARCHAR(30) DEFAULT 'unpaid'",
        'is_void': "is_void BOOLEAN DEFAULT 0",
    })
    _ensure_table_columns_sqlite('hdc_tool_rental_payment', {
        'received_to_account_id': "received_to_account_id INTEGER REFERENCES hdc_account(id)",
        'is_void': "is_void BOOLEAN DEFAULT 0",
        'void_reason': "void_reason VARCHAR(250)",
        'voided_at': "voided_at DATETIME",
    })
    # Backfill is_void for legacy rows and ensure link table indexes - auto on reload
    with db.engine.connect() as conn:
        try:
            conn.execute(text("UPDATE hdc_tool_rental_payment SET is_void = COALESCE(is_void, 0)"))
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
        try:
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_tool_rental_payment_rental ON hdc_tool_rental_payment(rental_id, payment_date)"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_tool_rental_payment_recv_acc ON hdc_tool_rental_payment(received_to_account_id)"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_tool_rental_acct_txn_payment ON hdc_tool_rental_account_txn(payment_id)"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_tool_rental_acct_txn_acct ON hdc_tool_rental_account_txn(account_txn_id)"))
            conn.commit()
        except Exception:
            pass
        try:
            # Log migration success for debugging - visible in app logs
            conn.execute(text("SELECT 1 FROM hdc_tool_rental_payment LIMIT 1"))
            print("[HDC ERP] Tool Rental schema migrated: received_to_account_id + is_void + account_txn link ready")
        except Exception:
            pass
