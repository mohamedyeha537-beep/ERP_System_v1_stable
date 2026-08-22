"""ترقيات مخطط MySQL/PostgreSQL — أعمدة أُضيفت في النماذج دون Alembic على السيرفر."""

from __future__ import annotations

import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from infra.database import is_mysql_url, is_postgresql_url

log = logging.getLogger("infra.server_schema_patch")


def _safe_exec(conn, sql: str, added: list[str], label: str) -> None:
    try:
        conn.execute(text(sql))
        added.append(label)
    except Exception as exc:  # noqa: BLE001
        log.warning("schema patch skipped %s: %s", label, exc)


def _table_columns(conn, table: str) -> set[str]:
    insp = inspect(conn)
    if table not in insp.get_table_names():
        return set()
    return {c["name"] for c in insp.get_columns(table)}


def _bool_ddl(dialect: str, *, default: bool = True) -> str:
    if dialect == "mysql":
        return f"TINYINT(1) NOT NULL DEFAULT {1 if default else 0}"
    return f"BOOLEAN NOT NULL DEFAULT {'TRUE' if default else 'FALSE'}"


def _add_column(conn, table: str, name: str, ddl: str) -> bool:
    cols = _table_columns(conn, table)
    if not cols or name in cols:
        return False
    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
    return True


def patch_server_schema(engine: Engine) -> None:
    """يضيف أعمدة ناقصة على MySQL/PostgreSQL (تُستدعى عند إقلاع التطبيق)."""
    url = str(engine.url)
    if not (is_mysql_url(url) or is_postgresql_url(url)):
        return

    dialect = engine.dialect.name
    added: list[str] = []

    with engine.begin() as conn:
        bool_t = _bool_ddl(dialect, default=True)
        bool_f = _bool_ddl(dialect, default=False)

        tables = set(inspect(conn).get_table_names())
        if "kitchen_departments" not in tables:
            conn.execute(
                text(
                    """
                    CREATE TABLE kitchen_departments (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        name_ar VARCHAR(120) NOT NULL,
                        venue VARCHAR(20) NOT NULL DEFAULT 'KITCHEN',
                        sort_order INT NOT NULL DEFAULT 0,
                        is_active TINYINT(1) NOT NULL DEFAULT 1,
                        routing_mode VARCHAR(20) NOT NULL DEFAULT 'SCREEN',
                        routing_target VARCHAR(255)
                    )
                    """
                    if dialect == "mysql"
                    else """
                    CREATE TABLE kitchen_departments (
                        id SERIAL PRIMARY KEY,
                        name_ar VARCHAR(120) NOT NULL,
                        venue VARCHAR(20) NOT NULL DEFAULT 'KITCHEN',
                        sort_order INTEGER NOT NULL DEFAULT 0,
                        is_active BOOLEAN NOT NULL DEFAULT TRUE,
                        routing_mode VARCHAR(20) NOT NULL DEFAULT 'SCREEN',
                        routing_target VARCHAR(255)
                    )
                    """
                )
            )
            added.append("kitchen_departments (table)")

        product_cols: list[tuple[str, str]] = [
            ("show_in_pos", bool_t),
            ("line_modifier_presets", "TEXT"),
            ("expiry_tracked", bool_f),
            ("expiry_production_date", "DATE"),
            ("expiry_date", "DATE"),
            ("expiry_warn_days", "INTEGER NOT NULL DEFAULT 7"),
            ("price_linked_to_bom", bool_f),
            ("bom_markup_pct", "NUMERIC(8, 2)"),
            ("reference_unit_cost", "NUMERIC(14, 3)"),
            ("direct_purchase_enabled", bool_f),
            ("kitchen_section_id", "INTEGER"),
            ("kitchen_department_id", "INTEGER"),
            ("image_filename", "VARCHAR(255)"),
        ]
        if _table_columns(conn, "products"):
            for col, ddl in product_cols:
                if _add_column(conn, "products", col, ddl):
                    added.append(f"products.{col}")
                    if col == "direct_purchase_enabled":
                        conn.execute(
                            text(
                                "UPDATE products SET direct_purchase_enabled = "
                                + ("1" if dialect == "mysql" else "TRUE")
                                + " WHERE kind = 'FINAL_SELLABLE' "
                                "AND NOT EXISTS ("
                                "SELECT 1 FROM bom_lines "
                                "WHERE bom_lines.parent_product_id = products.id"
                                ")"
                            )
                        )

        category_cols: list[tuple[str, str]] = [
            ("color_hex", "VARCHAR(7) NOT NULL DEFAULT '#3b82f6'"),
            ("show_in_pos", bool_t),
            ("show_in_shop", bool_t),
            ("kitchen_section_id", "INTEGER"),
            ("delete_protected", bool_f),
            ("routing_mode", "VARCHAR(20) NOT NULL DEFAULT 'NONE'"),
            ("routing_target", "VARCHAR(255)"),
        ]
        if _table_columns(conn, "product_categories"):
            for col, ddl in category_cols:
                if _add_column(conn, "product_categories", col, ddl):
                    added.append(f"product_categories.{col}")

        if _table_columns(conn, "purchases"):
            purchase_cols: list[tuple[str, str]] = [
                ("kind", "VARCHAR(20) NOT NULL DEFAULT 'EXPENSE'"),
                ("expense_category", "VARCHAR(80)"),
                ("supplier_invoice_ref", "VARCHAR(120)"),
                ("invoice_image_filename", "VARCHAR(255)"),
                ("payment_proof_image_filename", "VARCHAR(255)"),
                ("supplier_phone", "VARCHAR(40)"),
                ("receipt_batch_no", "VARCHAR(32)"),
            ]
            for col, ddl in purchase_cols:
                if _add_column(conn, "purchases", col, ddl):
                    added.append(f"purchases.{col}")

            if "receipt_batch_no" in _table_columns(conn, "purchases"):
                if dialect == "mysql":
                    conn.execute(
                        text(
                            "UPDATE purchases SET receipt_batch_no = "
                            "CONCAT('GR-', LPAD(CAST(id AS CHAR), 6, '0')) "
                            "WHERE kind = 'INVENTORY' AND receipt_batch_no IS NULL"
                        )
                    )
                elif dialect == "postgresql":
                    conn.execute(
                        text(
                            "UPDATE purchases SET receipt_batch_no = "
                            "'GR-' || LPAD(id::text, 6, '0') "
                            "WHERE kind = 'INVENTORY' AND receipt_batch_no IS NULL"
                        )
                    )

        if _table_columns(conn, "purchase_lines"):
            pl_cols: list[tuple[str, str]] = [
                ("item_name", "VARCHAR(255)"),
                ("unit", "VARCHAR(32)"),
                ("useful_life_months", "INTEGER NOT NULL DEFAULT 0"),
                ("salvage_value", "NUMERIC(14, 3) NOT NULL DEFAULT 0"),
                ("disposal_date", "DATETIME"),
                ("lot_code", "VARCHAR(32)"),
                ("production_date", "DATE"),
                ("expiry_date", "DATE"),
                ("line_kind", "VARCHAR(20)"),
            ]
            for col, ddl in pl_cols:
                if _add_column(conn, "purchase_lines", col, ddl):
                    added.append(f"purchase_lines.{col}")
            if "line_kind" in _table_columns(conn, "purchase_lines"):
                conn.execute(
                    text(
                        "UPDATE purchase_lines SET line_kind = 'PRODUCT' "
                        "WHERE product_id IS NOT NULL AND (line_kind IS NULL OR line_kind = '')"
                    )
                )
                conn.execute(
                    text(
                        "UPDATE purchase_lines SET line_kind = 'FIXED_ASSET' "
                        "WHERE product_id IS NULL AND useful_life_months > 0 "
                        "AND (line_kind IS NULL OR line_kind = '')"
                    )
                )
                conn.execute(
                    text(
                        "UPDATE purchase_lines SET line_kind = 'CONSUMABLE' "
                        "WHERE product_id IS NULL AND COALESCE(useful_life_months, 0) = 0 "
                        "AND item_name IS NOT NULL AND TRIM(item_name) != '' "
                        "AND (line_kind IS NULL OR line_kind = '')"
                    )
                )

        lot_cols = _table_columns(conn, "inventory_lots")
        if lot_cols and "unit_cost" not in lot_cols:
            conn.execute(
                text(
                    "ALTER TABLE inventory_lots ADD COLUMN unit_cost "
                    "NUMERIC(14, 3) NOT NULL DEFAULT 0"
                )
            )
            added.append("inventory_lots.unit_cost")
            conn.execute(
                text(
                    "UPDATE inventory_lots SET unit_cost = ("
                    "SELECT pl.unit_cost FROM purchase_lines pl "
                    "WHERE pl.id = inventory_lots.purchase_line_id"
                    ") WHERE unit_cost = 0"
                )
            )

        tables = set(inspect(conn).get_table_names())
        if "inventory_lot_consumptions" not in tables:
            created_at_ddl = (
                "DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP"
                if dialect == "mysql"
                else "TIMESTAMPTZ NOT NULL DEFAULT NOW()"
            )
            _safe_exec(
                conn,
                f"""
                CREATE TABLE inventory_lot_consumptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    stock_movement_id INTEGER NOT NULL REFERENCES stock_movements(id) ON DELETE CASCADE,
                    inventory_lot_id INTEGER NOT NULL REFERENCES inventory_lots(id) ON DELETE CASCADE,
                    quantity DECIMAL(14,4) NOT NULL,
                    unit_cost DECIMAL(14,3) NOT NULL,
                    line_total DECIMAL(14,3) NOT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
                if dialect == "sqlite"
                else f"""
                CREATE TABLE inventory_lot_consumptions (
                    id SERIAL PRIMARY KEY,
                    stock_movement_id INTEGER NOT NULL REFERENCES stock_movements(id) ON DELETE CASCADE,
                    inventory_lot_id INTEGER NOT NULL REFERENCES inventory_lots(id) ON DELETE CASCADE,
                    quantity DECIMAL(14,4) NOT NULL,
                    unit_cost DECIMAL(14,3) NOT NULL,
                    line_total DECIMAL(14,3) NOT NULL,
                    created_at {created_at_ddl}
                )
                """,
                added,
                "inventory_lot_consumptions",
            )
            for idx, col in (
                ("ix_inventory_lot_consumptions_movement", "stock_movement_id"),
                ("ix_inventory_lot_consumptions_lot", "inventory_lot_id"),
            ):
                _safe_exec(
                    conn,
                    f"CREATE INDEX IF NOT EXISTS {idx} ON inventory_lot_consumptions ({col})",
                    added,
                    f"inventory_lot_consumptions.{col}_idx",
                )

        if _table_columns(conn, "dashboard_activities"):
            if _add_column(conn, "dashboard_activities", "resolved_at", "DATETIME"):
                added.append("dashboard_activities.resolved_at")

        tables = set(inspect(conn).get_table_names())
        if "activity_hub_item_states" not in tables:
            _safe_exec(
                conn,
                """
                CREATE TABLE activity_hub_item_states (
                    user_id INT NOT NULL,
                    item_key VARCHAR(96) NOT NULL,
                    state VARCHAR(16) NOT NULL DEFAULT 'read',
                    updated_at DATETIME NOT NULL,
                    PRIMARY KEY (user_id, item_key),
                    INDEX ix_activity_hub_item_states_user (user_id, state),
                    CONSTRAINT fk_activity_hub_item_states_user
                        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                )
                """
                if dialect == "mysql"
                else """
                CREATE TABLE activity_hub_item_states (
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    item_key VARCHAR(96) NOT NULL,
                    state VARCHAR(16) NOT NULL DEFAULT 'read',
                    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
                    PRIMARY KEY (user_id, item_key)
                )
                """,
                added,
                "activity_hub_item_states (table)",
            )
        if "activity_hub_mutes" not in tables:
            _safe_exec(
                conn,
                """
                CREATE TABLE activity_hub_mutes (
                    user_id INT NOT NULL,
                    mute_key VARCHAR(96) NOT NULL,
                    label_ar VARCHAR(160) NULL,
                    muted_at DATETIME NOT NULL,
                    PRIMARY KEY (user_id, mute_key),
                    CONSTRAINT fk_activity_hub_mutes_user
                        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                )
                """
                if dialect == "mysql"
                else """
                CREATE TABLE activity_hub_mutes (
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    mute_key VARCHAR(96) NOT NULL,
                    label_ar VARCHAR(160),
                    muted_at TIMESTAMP WITH TIME ZONE NOT NULL,
                    PRIMARY KEY (user_id, mute_key)
                )
                """,
                added,
                "activity_hub_mutes (table)",
            )
        if "activity_hub_wa_overrides" not in tables:
            _safe_exec(
                conn,
                """
                CREATE TABLE activity_hub_wa_overrides (
                    event_key VARCHAR(64) NOT NULL PRIMARY KEY,
                    phones VARCHAR(255) NOT NULL DEFAULT '',
                    message_body TEXT NULL,
                    updated_at DATETIME(6) NULL
                )
                """
                if dialect == "mysql"
                else """
                CREATE TABLE activity_hub_wa_overrides (
                    event_key VARCHAR(64) NOT NULL PRIMARY KEY,
                    phones VARCHAR(255) NOT NULL DEFAULT '',
                    message_body TEXT,
                    updated_at TIMESTAMP WITH TIME ZONE
                )
                """,
                added,
                "activity_hub_wa_overrides (table)",
            )

        tables = set(inspect(conn).get_table_names())
        if "hr_departments" not in tables:
            _safe_exec(
                conn,
                """
                CREATE TABLE hr_departments (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    name_ar VARCHAR(120) NOT NULL,
                    code VARCHAR(32) NULL,
                    sort_order INT NOT NULL DEFAULT 0,
                    is_active TINYINT(1) NOT NULL DEFAULT 1,
                    notes TEXT,
                    created_at DATETIME NOT NULL
                )
                """
                if dialect == "mysql"
                else """
                CREATE TABLE hr_departments (
                    id SERIAL PRIMARY KEY,
                    name_ar VARCHAR(120) NOT NULL,
                    code VARCHAR(32),
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    notes TEXT,
                    created_at TIMESTAMP WITH TIME ZONE NOT NULL
                )
                """,
                added,
                "hr_departments (table)",
            )
            _safe_exec(
                conn,
                "CREATE INDEX ix_hr_departments_name_ar ON hr_departments (name_ar)",
                added,
                "ix_hr_departments_name_ar",
            )
            _safe_exec(
                conn,
                "CREATE INDEX ix_hr_departments_sort_order ON hr_departments (sort_order)",
                added,
                "ix_hr_departments_sort_order",
            )

        tables = set(inspect(conn).get_table_names())
        if "hr_work_shifts" not in tables:
            _safe_exec(
                conn,
                """
                CREATE TABLE hr_work_shifts (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    name_ar VARCHAR(120) NOT NULL,
                    code VARCHAR(32) NULL,
                    start_time VARCHAR(5) NOT NULL DEFAULT '08:00',
                    end_time VARCHAR(5) NOT NULL DEFAULT '16:00',
                    work_hours NUMERIC(4, 2) NOT NULL DEFAULT 8.00,
                    grace_minutes INT NOT NULL DEFAULT 15,
                    is_active TINYINT(1) NOT NULL DEFAULT 1,
                    notes TEXT,
                    created_at DATETIME NOT NULL
                )
                """
                if dialect == "mysql"
                else """
                CREATE TABLE hr_work_shifts (
                    id SERIAL PRIMARY KEY,
                    name_ar VARCHAR(120) NOT NULL,
                    code VARCHAR(32),
                    start_time VARCHAR(5) NOT NULL DEFAULT '08:00',
                    end_time VARCHAR(5) NOT NULL DEFAULT '16:00',
                    work_hours NUMERIC(4, 2) NOT NULL DEFAULT 8.00,
                    grace_minutes INTEGER NOT NULL DEFAULT 15,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    notes TEXT,
                    created_at TIMESTAMP WITH TIME ZONE NOT NULL
                )
                """,
                added,
                "hr_work_shifts (table)",
            )
            _safe_exec(
                conn,
                "CREATE INDEX ix_hr_work_shifts_name_ar ON hr_work_shifts (name_ar)",
                added,
                "ix_hr_work_shifts_name_ar",
            )

        tables = set(inspect(conn).get_table_names())
        if "hr_work_shift_day_schedules" not in tables and "hr_work_shifts" in tables:
            _safe_exec(
                conn,
                """
                CREATE TABLE hr_work_shift_day_schedules (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    work_shift_id INT NOT NULL,
                    day_of_week INT NOT NULL,
                    is_rest_day TINYINT(1) NOT NULL DEFAULT 0,
                    start_time VARCHAR(5) NULL,
                    end_time VARCHAR(5) NULL,
                    work_hours NUMERIC(4, 2) NULL,
                    grace_minutes INT NULL,
                    UNIQUE KEY uq_hr_shift_day (work_shift_id, day_of_week),
                    FOREIGN KEY (work_shift_id) REFERENCES hr_work_shifts(id) ON DELETE CASCADE
                )
                """
                if dialect == "mysql"
                else """
                CREATE TABLE hr_work_shift_day_schedules (
                    id SERIAL PRIMARY KEY,
                    work_shift_id INTEGER NOT NULL REFERENCES hr_work_shifts(id) ON DELETE CASCADE,
                    day_of_week INTEGER NOT NULL,
                    is_rest_day BOOLEAN NOT NULL DEFAULT FALSE,
                    start_time VARCHAR(5),
                    end_time VARCHAR(5),
                    work_hours NUMERIC(4, 2),
                    grace_minutes INTEGER,
                    UNIQUE (work_shift_id, day_of_week)
                )
                """,
                added,
                "hr_work_shift_day_schedules (table)",
            )

        tables = set(inspect(conn).get_table_names())
        if "hr_employee_day_schedules" not in tables and "hr_employees" in tables:
            _safe_exec(
                conn,
                """
                CREATE TABLE hr_employee_day_schedules (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    employee_id INT NOT NULL,
                    day_of_week INT NOT NULL,
                    is_rest_day TINYINT(1) NOT NULL DEFAULT 0,
                    start_time VARCHAR(5) NULL,
                    end_time VARCHAR(5) NULL,
                    work_hours NUMERIC(4, 2) NULL,
                    grace_minutes INT NULL,
                    UNIQUE KEY uq_hr_emp_day (employee_id, day_of_week),
                    FOREIGN KEY (employee_id) REFERENCES hr_employees(id) ON DELETE CASCADE
                )
                """
                if dialect == "mysql"
                else """
                CREATE TABLE hr_employee_day_schedules (
                    id SERIAL PRIMARY KEY,
                    employee_id INTEGER NOT NULL REFERENCES hr_employees(id) ON DELETE CASCADE,
                    day_of_week INTEGER NOT NULL,
                    is_rest_day BOOLEAN NOT NULL DEFAULT FALSE,
                    start_time VARCHAR(5),
                    end_time VARCHAR(5),
                    work_hours NUMERIC(4, 2),
                    grace_minutes INTEGER,
                    UNIQUE (employee_id, day_of_week)
                )
                """,
                added,
                "hr_employee_day_schedules (table)",
            )

        if _table_columns(conn, "hr_employees"):
            if _add_column(conn, "hr_employees", "department_id", "INT NULL"):
                added.append("hr_employees.department_id")
            if _add_column(conn, "hr_employees", "work_shift_id", "INT NULL"):
                added.append("hr_employees.work_shift_id")
            if _add_column(conn, "hr_employees", "zk_emp_code", "VARCHAR(64) NULL"):
                added.append("hr_employees.zk_emp_code")
            if _add_column(conn, "hr_employees", "is_pos_cashier", bool_f):
                added.append("hr_employees.is_pos_cashier")
            if _add_column(conn, "hr_employees", "is_pos_supervisor", bool_f):
                added.append("hr_employees.is_pos_supervisor")
            if _add_column(conn, "hr_employees", "pos_pin_hash", "VARCHAR(255) NULL"):
                added.append("hr_employees.pos_pin_hash")
            if "zk_emp_code" in _table_columns(conn, "hr_employees"):
                conn.execute(
                    text(
                        "UPDATE hr_employees SET zk_emp_code = NULL "
                        "WHERE zk_emp_code = '' OR TRIM(zk_emp_code) = ''"
                    )
                )
            he_idx = {i["name"] for i in inspect(conn).get_indexes("hr_employees")}
            if (
                "ix_hr_employees_department_id" not in he_idx
                and "department_id" in _table_columns(conn, "hr_employees")
            ):
                _safe_exec(
                    conn,
                    "CREATE INDEX ix_hr_employees_department_id ON hr_employees (department_id)",
                    added,
                    "ix_hr_employees_department_id",
                )
            if (
                "ix_hr_employees_work_shift_id" not in he_idx
                and "work_shift_id" in _table_columns(conn, "hr_employees")
            ):
                _safe_exec(
                    conn,
                    "CREATE INDEX ix_hr_employees_work_shift_id ON hr_employees (work_shift_id)",
                    added,
                    "ix_hr_employees_work_shift_id",
                )
            if (
                "ix_hr_employees_zk_emp_code" not in he_idx
                and "zk_emp_code" in _table_columns(conn, "hr_employees")
            ):
                _safe_exec(
                    conn,
                    "CREATE UNIQUE INDEX ix_hr_employees_zk_emp_code ON hr_employees (zk_emp_code)",
                    added,
                    "ix_hr_employees_zk_emp_code",
                )

        tables = set(inspect(conn).get_table_names())
        if "hr_zk_processed_punches" not in tables:
            _safe_exec(
                conn,
                """
                CREATE TABLE hr_zk_processed_punches (
                    zk_transaction_id INT NOT NULL PRIMARY KEY,
                    employee_id INT NULL,
                    action VARCHAR(32) NOT NULL DEFAULT '',
                    detail VARCHAR(500) NULL,
                    processed_at DATETIME NOT NULL,
                    FOREIGN KEY (employee_id) REFERENCES hr_employees(id) ON DELETE SET NULL
                )
                """
                if dialect == "mysql"
                else """
                CREATE TABLE hr_zk_processed_punches (
                    zk_transaction_id INTEGER NOT NULL PRIMARY KEY,
                    employee_id INTEGER NULL REFERENCES hr_employees(id) ON DELETE SET NULL,
                    action VARCHAR(32) NOT NULL DEFAULT '',
                    detail VARCHAR(500),
                    processed_at TIMESTAMP WITH TIME ZONE NOT NULL
                )
                """,
                added,
                "hr_zk_processed_punches (table)",
            )
            _safe_exec(
                conn,
                "CREATE INDEX ix_hr_zk_processed_punches_employee_id "
                "ON hr_zk_processed_punches (employee_id)",
                added,
                "ix_hr_zk_processed_punches_employee_id",
            )

        tables = set(inspect(conn).get_table_names())
        if "hr_employee_bonuses" not in tables and "hr_employees" in tables:
            _safe_exec(
                conn,
                """
                CREATE TABLE hr_employee_bonuses (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    employee_id INT NOT NULL,
                    amount DECIMAL(14,3) NOT NULL DEFAULT 0,
                    status VARCHAR(30) NOT NULL DEFAULT 'OUTSTANDING',
                    note TEXT NULL,
                    created_at DATETIME NOT NULL,
                    settled_at DATETIME NULL,
                    settled_by_id INT NULL,
                    payroll_entry_id INT NULL,
                    created_by_id INT NULL,
                    FOREIGN KEY (employee_id) REFERENCES hr_employees(id) ON DELETE RESTRICT,
                    FOREIGN KEY (settled_by_id) REFERENCES users(id) ON DELETE SET NULL,
                    FOREIGN KEY (created_by_id) REFERENCES users(id) ON DELETE SET NULL
                )
                """
                if dialect == "mysql"
                else """
                CREATE TABLE hr_employee_bonuses (
                    id SERIAL PRIMARY KEY,
                    employee_id INTEGER NOT NULL REFERENCES hr_employees(id) ON DELETE RESTRICT,
                    amount NUMERIC(14,3) NOT NULL DEFAULT 0,
                    status VARCHAR(30) NOT NULL DEFAULT 'OUTSTANDING',
                    note TEXT,
                    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
                    settled_at TIMESTAMP WITH TIME ZONE,
                    settled_by_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    payroll_entry_id INTEGER,
                    created_by_id INTEGER REFERENCES users(id) ON DELETE SET NULL
                )
                """,
                added,
                "hr_employee_bonuses (table)",
            )
            _safe_exec(
                conn,
                "CREATE INDEX ix_hr_employee_bonuses_employee_id "
                "ON hr_employee_bonuses (employee_id)",
                added,
                "ix_hr_employee_bonuses_employee_id",
            )
            _safe_exec(
                conn,
                "CREATE INDEX ix_hr_employee_bonuses_status "
                "ON hr_employee_bonuses (status)",
                added,
                "ix_hr_employee_bonuses_status",
            )

        if _table_columns(conn, "hr_attendance"):
            att_cols: list[tuple[str, str]] = [
                ("work_shift_id", "INT NULL"),
                ("late_minutes", "INT NOT NULL DEFAULT 0"),
                ("early_leave_minutes", "INT NOT NULL DEFAULT 0"),
                ("overtime_minutes", "INT NOT NULL DEFAULT 0"),
                ("overtime_approval_status", "VARCHAR(16) NOT NULL DEFAULT 'NONE'"),
                ("approved_overtime_minutes", "INT NOT NULL DEFAULT 0"),
                ("overtime_approved_by_id", "INT NULL"),
                ("overtime_approved_at", "DATETIME NULL"),
                ("expected_work_hours", "NUMERIC(4, 2) NULL"),
            ]
            for col, ddl in att_cols:
                if _add_column(conn, "hr_attendance", col, ddl):
                    added.append(f"hr_attendance.{col}")
            if "overtime_minutes" in _table_columns(conn, "hr_attendance"):
                conn.execute(
                    text(
                        """
                        UPDATE hr_attendance
                        SET overtime_approval_status = 'PENDING'
                        WHERE overtime_minutes > 0
                          AND (overtime_approval_status IS NULL
                               OR overtime_approval_status = 'NONE'
                               OR overtime_approval_status = '')
                        """
                    )
                )
                added.append("hr_attendance.overtime_approval_status_backfill")

        # ── Hotel module (نسخة live / ChatGPT dump → كود التطبيق الحالي)
        if _table_columns(conn, "hotel_rooms"):
            hr_cols = _table_columns(conn, "hotel_rooms")
            for col, ddl in [
                ("property_id", "INT NOT NULL DEFAULT 1"),
                ("room_type_id", "INT NULL"),
                ("floor", "VARCHAR(20) NULL"),
                ("physical_status", "VARCHAR(32) NOT NULL DEFAULT 'AVAILABLE'"),
                ("name_ar", "VARCHAR(120) NULL"),
                ("nightly_price", "NUMERIC(14, 3) NULL"),
                ("image_filename", "VARCHAR(255) NULL"),
            ]:
                if _add_column(conn, "hotel_rooms", col, ddl):
                    added.append(f"hotel_rooms.{col}")

            status_map = {
                "available": "AVAILABLE",
                "reserved": "RESERVED",
                "occupied": "OCCUPIED",
                "dirty": "DIRTY",
                "maintenance": "MAINTENANCE",
                "out_of_service": "OUT_OF_SERVICE",
                "blocked": "BLOCKED",
            }
            for old, new in status_map.items():
                conn.execute(
                    text(
                        "UPDATE hotel_rooms SET physical_status = :new "
                        "WHERE LOWER(physical_status) = :old"
                    ),
                    {"old": old, "new": new},
                )

        for table in ("hotel_room_status_logs",):
            cols = _table_columns(conn, table)
            if cols:
                for col in ("from_status", "to_status"):
                    if col in cols:
                        for old, new in {
                            "available": "AVAILABLE",
                            "reserved": "RESERVED",
                            "occupied": "OCCUPIED",
                            "dirty": "DIRTY",
                            "maintenance": "MAINTENANCE",
                            "out_of_service": "OUT_OF_SERVICE",
                            "blocked": "BLOCKED",
                        }.items():
                            conn.execute(
                                text(
                                    f"UPDATE {table} SET {col} = :new "
                                    f"WHERE LOWER({col}) = :old"
                                ),
                                {"old": old, "new": new},
                            )

        if _table_columns(conn, "hotel_room_charges"):
            if _add_column(conn, "hotel_room_charges", "booking_id", "INT NULL"):
                added.append("hotel_room_charges.booking_id")
            rc_idx = {i["name"] for i in inspect(conn).get_indexes("hotel_room_charges")}
            if "ix_hotel_room_charges_booking_id" not in rc_idx and "booking_id" in _table_columns(
                conn, "hotel_room_charges"
            ):
                conn.execute(
                    text(
                        "CREATE INDEX ix_hotel_room_charges_booking_id "
                        "ON hotel_room_charges (booking_id)"
                    )
                )
                added.append("ix_hotel_room_charges_booking_id")
            for col, ddl in (
                ("service_code", "VARCHAR(40) NULL"),
                ("folio_side", "VARCHAR(16) NULL"),
                ("company_amount", "DECIMAL(14,3) NULL"),
                ("guest_amount", "DECIMAL(14,3) NULL"),
            ):
                if _add_column(conn, "hotel_room_charges", col, ddl):
                    added.append(f"hotel_room_charges.{col}")

        if _table_columns(conn, "hotel_bookings"):
            hotel_booking_cols: list[tuple[str, str]] = [
                ("guest_id_type", "VARCHAR(80)"),
                ("guest_address", "TEXT"),
                ("guest_nationality", "VARCHAR(80)"),
                ("guest_type", "VARCHAR(20) NOT NULL DEFAULT 'INDIVIDUAL'"),
                ("record_kind", "VARCHAR(20) NOT NULL DEFAULT 'BOOKING'"),
                ("quotation_status", "VARCHAR(20) NULL"),
                ("company_name", "VARCHAR(200) NULL"),
                ("company_tax_id", "VARCHAR(64) NULL"),
                ("company_address", "TEXT NULL"),
                ("company_contact_name", "VARCHAR(160) NULL"),
                ("company_contact_phone", "VARCHAR(40) NULL"),
                ("company_contact_email", "VARCHAR(120) NULL"),
                ("quotation_valid_until", "DATE NULL"),
                ("quotation_notes", "TEXT NULL"),
                ("scheduled_check_out", "DATE NULL"),
            ]
            for col, ddl in hotel_booking_cols:
                if _add_column(conn, "hotel_bookings", col, ddl):
                    added.append(f"hotel_bookings.{col}")

        if _table_columns(conn, "hotel_booking_room_assignments"):
            if _add_column(
                conn,
                "hotel_booking_room_assignments",
                "effective_date",
                "DATE NULL",
            ):
                added.append("hotel_booking_room_assignments.effective_date")

        if _table_columns(conn, "payment_methods"):
            if _add_column(
                conn,
                "payment_methods",
                "business_domain",
                "VARCHAR(20) NOT NULL DEFAULT 'shared'",
            ):
                added.append("payment_methods.business_domain")
            if "business_domain" in _table_columns(conn, "payment_methods"):
                conn.execute(
                    text(
                        "ALTER TABLE payment_methods MODIFY COLUMN business_domain "
                        "VARCHAR(20) NOT NULL DEFAULT 'shared'"
                    )
                )
                conn.execute(
                    text(
                        "UPDATE payment_methods SET business_domain = 'shared' "
                        "WHERE business_domain IS NULL OR business_domain = '' "
                        "OR UPPER(business_domain) = 'SHARED'"
                    )
                )
                conn.execute(
                    text(
                        "UPDATE payment_methods SET business_domain = 'restaurant' "
                        "WHERE UPPER(business_domain) = 'RESTAURANT'"
                    )
                )
                conn.execute(
                    text(
                        "UPDATE payment_methods SET business_domain = 'hotel' "
                        "WHERE UPPER(business_domain) = 'HOTEL'"
                    )
                )
                added.append("payment_methods.business_domain_normalize")

        if _table_columns(conn, "recurring_costs"):
            if _add_column(
                conn,
                "recurring_costs",
                "business_domain",
                "VARCHAR(20) NOT NULL DEFAULT 'restaurant'",
            ):
                added.append("recurring_costs.business_domain")
            if "business_domain" in _table_columns(conn, "recurring_costs"):
                conn.execute(
                    text(
                        "UPDATE recurring_costs SET business_domain = 'restaurant' "
                        "WHERE business_domain IS NULL OR business_domain = '' "
                        "OR UPPER(business_domain) = 'RESTAURANT'"
                    )
                )
                conn.execute(
                    text(
                        "UPDATE recurring_costs SET business_domain = 'hotel' "
                        "WHERE UPPER(business_domain) = 'HOTEL'"
                    )
                )
                conn.execute(
                    text(
                        "UPDATE recurring_costs SET business_domain = 'shared' "
                        "WHERE UPPER(business_domain) = 'SHARED'"
                    )
                )
                added.append("recurring_costs.business_domain_normalize")

        if _table_columns(conn, "hr_employees"):
            if _add_column(
                conn,
                "hr_employees",
                "business_domain",
                "VARCHAR(20) NOT NULL DEFAULT 'restaurant'",
            ):
                added.append("hr_employees.business_domain")
            if "business_domain" in _table_columns(conn, "hr_employees"):
                conn.execute(
                    text(
                        "UPDATE hr_employees SET business_domain = 'restaurant' "
                        "WHERE business_domain IS NULL OR business_domain = '' "
                        "OR UPPER(business_domain) = 'RESTAURANT'"
                    )
                )
                conn.execute(
                    text(
                        "UPDATE hr_employees SET business_domain = 'hotel' "
                        "WHERE UPPER(business_domain) = 'HOTEL'"
                    )
                )
                added.append("hr_employees.business_domain_normalize")

        if _table_columns(conn, "purchases"):
            if _add_column(
                conn,
                "purchases",
                "business_domain",
                "VARCHAR(20) NOT NULL DEFAULT 'restaurant'",
            ):
                added.append("purchases.business_domain")
            if "business_domain" in _table_columns(conn, "purchases"):
                conn.execute(
                    text(
                        "UPDATE purchases p "
                        "INNER JOIN payment_methods pm ON p.payment_method_id = pm.id "
                        "SET p.business_domain = pm.business_domain "
                        "WHERE p.business_domain IS NULL OR p.business_domain = ''"
                    )
                )
                conn.execute(
                    text(
                        "UPDATE purchases SET business_domain = 'restaurant' "
                        "WHERE business_domain IS NULL OR business_domain = '' "
                        "OR UPPER(business_domain) = 'RESTAURANT'"
                    )
                )
                conn.execute(
                    text(
                        "UPDATE purchases SET business_domain = 'hotel' "
                        "WHERE UPPER(business_domain) = 'HOTEL'"
                    )
                )
                conn.execute(
                    text(
                        "UPDATE purchases SET business_domain = 'shared' "
                        "WHERE UPPER(business_domain) = 'SHARED'"
                    )
                )
                added.append("purchases.business_domain_normalize")

        if _table_columns(conn, "gl_accounts"):
            if _add_column(
                conn,
                "gl_accounts",
                "business_domain",
                "VARCHAR(20) NOT NULL DEFAULT 'shared'",
            ):
                added.append("gl_accounts.business_domain")
            if "business_domain" in _table_columns(conn, "gl_accounts"):
                conn.execute(
                    text(
                        "UPDATE gl_accounts SET business_domain = 'shared' "
                        "WHERE business_domain IS NULL OR business_domain = '' "
                        "OR code IN ('1000','1100','2000','3000','4000','5000','3100')"
                    )
                )
                conn.execute(
                    text(
                        "UPDATE gl_accounts SET business_domain = 'hotel' "
                        "WHERE code IN ('1115','1125')"
                    )
                )
                conn.execute(
                    text(
                        "UPDATE gl_accounts SET business_domain = 'restaurant' "
                        "WHERE business_domain IS NULL OR business_domain = 'shared' "
                        "AND code NOT IN ('1000','1100','2000','3000','4000','5000','3100','1115','1125')"
                    )
                )
                added.append("gl_accounts.business_domain_normalize")

        if _table_columns(conn, "gl_journal_entries"):
            if _add_column(
                conn,
                "gl_journal_entries",
                "business_domain",
                "VARCHAR(20) NOT NULL DEFAULT 'restaurant'",
            ):
                added.append("gl_journal_entries.business_domain")
            if "business_domain" in _table_columns(conn, "gl_journal_entries"):
                conn.execute(
                    text(
                        "UPDATE gl_journal_entries SET business_domain = 'hotel' "
                        "WHERE source_type LIKE 'hotel_%'"
                    )
                )
                conn.execute(
                    text(
                        "UPDATE gl_journal_entries SET business_domain = 'restaurant' "
                        "WHERE source_type LIKE 'sale%' OR source_type = 'refund_payment' "
                        "OR source_type LIKE 'payroll%'"
                    )
                )
                conn.execute(
                    text(
                        "UPDATE gl_journal_entries SET business_domain = 'shared' "
                        "WHERE source_type = 'payment_transfer'"
                    )
                )
                if _table_columns(conn, "purchases"):
                    conn.execute(
                        text(
                            "UPDATE gl_journal_entries e "
                            "INNER JOIN purchases p ON e.source_id = p.id "
                            "SET e.business_domain = p.business_domain "
                            "WHERE e.source_type IN ("
                            "'purchase_expense','purchase_inventory','purchase_asset','salary_advance'"
                            ")"
                        )
                    )
                conn.execute(
                    text(
                        "UPDATE gl_journal_entries SET business_domain = 'restaurant' "
                        "WHERE business_domain IS NULL OR business_domain = ''"
                    )
                )
                added.append("gl_journal_entries.business_domain_normalize")

        if _table_columns(conn, "sales"):
            if _add_column(conn, "sales", "booking_id", "INT NULL"):
                added.append("sales.booking_id")
            s_idx = {i["name"] for i in inspect(conn).get_indexes("sales")}
            if "ix_sales_booking_id" not in s_idx and "booking_id" in _table_columns(conn, "sales"):
                conn.execute(text("CREATE INDEX ix_sales_booking_id ON sales (booking_id)"))
                added.append("ix_sales_booking_id")

        if _table_columns(conn, "kitchen_tickets"):
            if _add_column(conn, "kitchen_tickets", "archived_at", "DATETIME NULL"):
                added.append("kitchen_tickets.archived_at")

        if _table_columns(conn, "print_jobs") and dialect == "mysql":
            conn.execute(text("ALTER TABLE print_jobs MODIFY COLUMN payload LONGTEXT NOT NULL"))
            added.append("print_jobs.payload_longtext")

        if _table_columns(conn, "stock_movements"):
            if _add_column(conn, "stock_movements", "transfer_id", "INT NULL"):
                added.append("stock_movements.transfer_id")

        if _table_columns(conn, "sale_return_lines"):
            if _add_column(conn, "sale_return_lines", "restock", bool_t):
                added.append("sale_return_lines.restock")

        if _table_columns(conn, "pos_shifts"):
            if _add_column(conn, "pos_shifts", "warehouse_id", "INT NULL"):
                added.append("pos_shifts.warehouse_id")
        if _table_columns(conn, "users"):
            if _add_column(conn, "users", "warehouse_id", "INT NULL"):
                added.append("users.warehouse_id")
            if _add_column(conn, "users", "kds_scope", "VARCHAR(20) NOT NULL DEFAULT 'ALL'"):
                added.append("users.kds_scope")
            if _add_column(conn, "users", "view_scope", "VARCHAR(20) NOT NULL DEFAULT 'both'"):
                added.append("users.view_scope")
                conn.execute(
                    text(
                        "UPDATE users SET view_scope = 'hotel' "
                        "WHERE id IN (SELECT ur.user_id FROM user_roles ur "
                        "JOIN roles r ON r.id = ur.role_id "
                        "WHERE r.name_ar IN ('موظف فندق', 'إدارة حجوزات الفندق فقط'))"
                    )
                )
                conn.execute(
                    text(
                        "UPDATE users SET view_scope = 'restaurant' "
                        "WHERE id IN (SELECT ur.user_id FROM user_roles ur "
                        "JOIN roles r ON r.id = ur.role_id "
                        "WHERE r.name_ar IN ('موظف مطعم', 'كاشير')) "
                        "AND view_scope = 'both'"
                    )
                )
            if _add_column(conn, "users", "ui_hidden", "VARCHAR(4000) NOT NULL DEFAULT '[]'"):
                added.append("users.ui_hidden")
            if _add_column(conn, "users", "pos_show_cash", bool_t):
                added.append("users.pos_show_cash")
            if _add_column(conn, "users", "pos_show_bank", bool_t):
                added.append("users.pos_show_bank")
            if "user_permission_grants" not in tables:
                conn.execute(
                    text(
                        """
                        CREATE TABLE user_permission_grants (
                            user_id INT NOT NULL,
                            permission_id INT NOT NULL,
                            PRIMARY KEY (user_id, permission_id),
                            CONSTRAINT fk_upg_user FOREIGN KEY (user_id)
                                REFERENCES users(id) ON DELETE CASCADE,
                            CONSTRAINT fk_upg_perm FOREIGN KEY (permission_id)
                                REFERENCES permissions(id) ON DELETE CASCADE
                        )
                        """
                    )
                )
                added.append("user_permission_grants")
            if "user_permission_denies" not in tables:
                conn.execute(
                    text(
                        """
                        CREATE TABLE user_permission_denies (
                            user_id INT NOT NULL,
                            permission_id INT NOT NULL,
                            PRIMARY KEY (user_id, permission_id),
                            CONSTRAINT fk_upd_user FOREIGN KEY (user_id)
                                REFERENCES users(id) ON DELETE CASCADE,
                            CONSTRAINT fk_upd_perm FOREIGN KEY (permission_id)
                                REFERENCES permissions(id) ON DELETE CASCADE
                        )
                        """
                    )
                )
                added.append("user_permission_denies")
            if _add_column(
                conn,
                "warehouses",
                "deduct_sales_enabled",
                "TINYINT(1) NOT NULL DEFAULT 0",
            ):
                added.append("warehouses.deduct_sales_enabled")
                conn.execute(
                    text(
                        "UPDATE warehouses SET deduct_sales_enabled = 1 "
                        "WHERE is_main = 1"
                    )
                )
        if _table_columns(conn, "products"):
            if _add_column(conn, "products", "sales_warehouse_id", "INT NULL"):
                added.append("products.sales_warehouse_id")
            if _add_column(conn, "products", "show_in_shop", bool_t):
                added.append("products.show_in_shop")
                # الحفاظ على السلوك السابق: المتجر كان يعتمد show_in_pos
                conn.execute(
                    text(
                        "UPDATE products SET show_in_shop = show_in_pos "
                        "WHERE show_in_pos IS NOT NULL"
                    )
                )
                conn.execute(
                    text(
                        "UPDATE products SET show_in_shop = "
                        + ("0" if dialect == "mysql" else "FALSE")
                        + " WHERE kind = 'STOCK_ONLY'"
                    )
                )

        if _table_columns(conn, "bom_lines"):
            if _add_column(conn, "bom_lines", "packaging_only", bool_f):
                added.append("bom_lines.packaging_only")
                try:
                    if dialect == "mysql":
                        conn.execute(
                            text(
                                "CREATE INDEX ix_bom_lines_packaging_only "
                                "ON bom_lines (packaging_only)"
                            )
                        )
                    else:
                        conn.execute(
                            text(
                                "CREATE INDEX IF NOT EXISTS ix_bom_lines_packaging_only "
                                "ON bom_lines (packaging_only)"
                            )
                        )
                    added.append("bom_lines.ix_packaging_only")
                except Exception as exc:  # noqa: BLE001
                    log.warning("schema patch skipped bom_lines index: %s", exc)

        if _table_columns(conn, "hr_payroll_runs"):
            if _add_column(
                conn,
                "hr_payroll_runs",
                "business_domain",
                "VARCHAR(20) NOT NULL DEFAULT 'restaurant'",
            ):
                added.append("hr_payroll_runs.business_domain")
            if "business_domain" in _table_columns(conn, "hr_payroll_runs"):
                conn.execute(
                    text(
                        "UPDATE hr_payroll_runs SET business_domain = 'restaurant' "
                        "WHERE business_domain IS NULL OR business_domain = ''"
                    )
                )
                added.append("hr_payroll_runs.business_domain_normalize")
                try:
                    conn.execute(text("ALTER TABLE hr_payroll_runs DROP INDEX uq_payroll_period"))
                    added.append("hr_payroll_runs.drop_old_uq")
                except Exception:  # noqa: BLE001
                    pass
                try:
                    conn.execute(
                        text(
                            "ALTER TABLE hr_payroll_runs ADD UNIQUE KEY "
                            "uq_payroll_period_domain (period_year, period_month, business_domain)"
                        )
                    )
                    added.append("hr_payroll_runs.uq_period_domain")
                except Exception:  # noqa: BLE001
                    pass

        if _table_columns(conn, "hr_payroll_entries"):
            if _add_column(
                conn,
                "hr_payroll_entries",
                "salary_receipt_confirmed_at",
                "DATETIME NULL",
            ):
                added.append("hr_payroll_entries.salary_receipt_confirmed_at")
            if _add_column(
                conn,
                "hr_payroll_entries",
                "salary_receipt_confirmed_via",
                "VARCHAR(32) NULL",
            ):
                added.append("hr_payroll_entries.salary_receipt_confirmed_via")

        if _table_columns(conn, "hotel_daily_closings"):
            num14 = "DECIMAL(14,3) NULL"
            hdc_cols: list[tuple[str, str]] = [
                ("gl_backfilled_payments", "INT NOT NULL DEFAULT 0"),
                ("gl_backfilled_refunds", "INT NOT NULL DEFAULT 0"),
                ("gl_operational_net", num14),
                ("gl_revenue_net", num14),
                ("gl_gap", num14),
            ]
            for col, ddl in hdc_cols:
                if _add_column(conn, "hotel_daily_closings", col, ddl):
                    added.append(f"hotel_daily_closings.{col}")

        if _table_columns(conn, "pos_shifts"):
            num14 = "DECIMAL(14,3) NULL"
            ps_cols: list[tuple[str, str]] = [
                ("gl_backfilled_entries", "INT NOT NULL DEFAULT 0"),
                ("gl_operational_net", num14),
                ("gl_revenue_net", num14),
                ("gl_gap", num14),
                ("close_destination", "VARCHAR(20) NULL"),
                ("opening_bank", num14),
                ("received_from_shift_id", "INT NULL"),
                ("carried_to_shift_id", "INT NULL"),
                ("carried_to_employee_id", "INT NULL"),
            ]
            for col, ddl in ps_cols:
                if _add_column(conn, "pos_shifts", col, ddl):
                    added.append(f"pos_shifts.{col}")

        tables = set(inspect(conn).get_table_names())
        if "web_analytics_events" not in tables:
            ts = "TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP"
            if dialect == "mysql":
                conn.execute(
                    text(
                        f"""
                        CREATE TABLE web_analytics_events (
                            id INT AUTO_INCREMENT PRIMARY KEY,
                            surface VARCHAR(32) NOT NULL,
                            event_type VARCHAR(40) NOT NULL,
                            page_path VARCHAR(255) NOT NULL DEFAULT '/',
                            session_id VARCHAR(64) NOT NULL DEFAULT '',
                            referrer VARCHAR(500) NULL,
                            meta_json TEXT NOT NULL,
                            created_at {ts.replace('TIMESTAMP', 'DATETIME')}
                        )
                        """
                    )
                )
            else:
                conn.execute(
                    text(
                        f"""
                        CREATE TABLE web_analytics_events (
                            id SERIAL PRIMARY KEY,
                            surface VARCHAR(32) NOT NULL,
                            event_type VARCHAR(40) NOT NULL,
                            page_path VARCHAR(255) NOT NULL DEFAULT '/',
                            session_id VARCHAR(64) NOT NULL DEFAULT '',
                            referrer VARCHAR(500),
                            meta_json TEXT NOT NULL DEFAULT '{{}}',
                            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        )
                        """
                    )
                )
            for idx, col in (
                ("ix_web_analytics_events_surface", "surface"),
                ("ix_web_analytics_events_event_type", "event_type"),
                ("ix_web_analytics_events_session_id", "session_id"),
                ("ix_web_analytics_events_created_at", "created_at"),
            ):
                _safe_exec(
                    conn,
                    f"CREATE INDEX IF NOT EXISTS {idx} ON web_analytics_events ({col})",
                    added,
                    f"web_analytics_events.{col}_idx",
                )
            added.append("web_analytics_events")

        tables = set(inspect(conn).get_table_names())
        ts = "TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP"
        if dialect == "postgresql":
            ts = "TIMESTAMPTZ NOT NULL DEFAULT NOW()"
        if "message_phone_lists" not in tables:
            _safe_exec(
                conn,
                f"""
                CREATE TABLE message_phone_lists (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    name VARCHAR(160) NOT NULL,
                    note TEXT NULL,
                    created_by_id INT NULL,
                    created_at {ts.replace('TIMESTAMP', 'DATETIME')}
                )
                """
                if dialect == "mysql"
                else f"""
                CREATE TABLE message_phone_lists (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(160) NOT NULL,
                    note TEXT,
                    created_by_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """,
                added,
                "message_phone_lists",
            )
            _safe_exec(
                conn,
                "CREATE INDEX IF NOT EXISTS ix_message_phone_lists_created_at ON message_phone_lists (created_at)",
                added,
                "message_phone_lists.created_at_idx",
            )
        if "message_phone_list_entries" not in tables:
            _safe_exec(
                conn,
                f"""
                CREATE TABLE message_phone_list_entries (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    list_id INT NOT NULL,
                    phone VARCHAR(40) NOT NULL,
                    display_name VARCHAR(120) NULL,
                    is_valid TINYINT(1) NOT NULL DEFAULT 1,
                    skip_reason VARCHAR(120) NULL,
                    created_at {ts.replace('TIMESTAMP', 'DATETIME')}
                )
                """
                if dialect == "mysql"
                else """
                CREATE TABLE message_phone_list_entries (
                    id SERIAL PRIMARY KEY,
                    list_id INTEGER NOT NULL REFERENCES message_phone_lists(id) ON DELETE CASCADE,
                    phone VARCHAR(40) NOT NULL,
                    display_name VARCHAR(120),
                    is_valid BOOLEAN NOT NULL DEFAULT TRUE,
                    skip_reason VARCHAR(120),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """,
                added,
                "message_phone_list_entries",
            )
            for idx, col in (
                ("ix_message_phone_list_entries_list_id", "list_id"),
                ("ix_message_phone_list_entries_phone", "phone"),
            ):
                _safe_exec(
                    conn,
                    f"CREATE INDEX IF NOT EXISTS {idx} ON message_phone_list_entries ({col})",
                    added,
                    f"message_phone_list_entries.{col}_idx",
                )

        tables = set(inspect(conn).get_table_names())
        dt = "DATETIME" if dialect == "mysql" else "TIMESTAMPTZ"
        num14 = "DECIMAL(14,3) NULL"
        if "hotel_shifts" not in tables:
            _safe_exec(
                conn,
                f"""
                CREATE TABLE hotel_shifts (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    property_id INT NOT NULL DEFAULT 1,
                    shift_number INT NOT NULL,
                    shift_name_ar VARCHAR(80) NOT NULL,
                    status VARCHAR(20) NOT NULL,
                    user_id INT NOT NULL,
                    closed_by_id INT NULL,
                    employee_id INT NULL,
                    scheduled_start {dt} NOT NULL,
                    scheduled_end {dt} NOT NULL,
                    opened_at {dt} NOT NULL,
                    closed_at {dt} NULL,
                    opening_note TEXT NULL,
                    closing_note TEXT NULL,
                    total_revenue {num14},
                    total_expenses {num14},
                    cash_collected {num14},
                    bank_collected {num14},
                    payment_count INT NOT NULL DEFAULT 0,
                    expense_count INT NOT NULL DEFAULT 0,
                    overdue_notified_at {dt} NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                    FOREIGN KEY (closed_by_id) REFERENCES users(id) ON DELETE SET NULL,
                    FOREIGN KEY (employee_id) REFERENCES hr_employees(id) ON DELETE SET NULL
                )
                """
                if dialect == "mysql"
                else f"""
                CREATE TABLE hotel_shifts (
                    id SERIAL PRIMARY KEY,
                    property_id INTEGER NOT NULL DEFAULT 1,
                    shift_number INTEGER NOT NULL,
                    shift_name_ar VARCHAR(80) NOT NULL,
                    status VARCHAR(20) NOT NULL,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    closed_by_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    employee_id INTEGER REFERENCES hr_employees(id) ON DELETE SET NULL,
                    scheduled_start TIMESTAMPTZ NOT NULL,
                    scheduled_end TIMESTAMPTZ NOT NULL,
                    opened_at TIMESTAMPTZ NOT NULL,
                    closed_at TIMESTAMPTZ,
                    opening_note TEXT,
                    closing_note TEXT,
                    total_revenue DECIMAL(14,3),
                    total_expenses DECIMAL(14,3),
                    cash_collected DECIMAL(14,3),
                    bank_collected DECIMAL(14,3),
                    payment_count INTEGER NOT NULL DEFAULT 0,
                    expense_count INTEGER NOT NULL DEFAULT 0,
                    overdue_notified_at TIMESTAMPTZ
                )
                """,
                added,
                "hotel_shifts",
            )
            for idx, col in (
                ("ix_hotel_shifts_status", "status"),
                ("ix_hotel_shifts_opened_at", "opened_at"),
            ):
                _safe_exec(
                    conn,
                    f"CREATE INDEX IF NOT EXISTS {idx} ON hotel_shifts ({col})",
                    added,
                    f"hotel_shifts.{col}_idx",
                )
        if _table_columns(conn, "hotel_shifts"):
            hotel_handoff_col_added = False
            for col, ddl in (
                ("opening_cash", num14),
                ("counted_cash", num14),
                ("counted_bank", num14),
                ("expected_cash", num14),
                ("expected_bank", num14),
                ("cash_difference", num14),
                ("bank_difference", num14),
                ("expected_bookings_count", "INT NULL"),
                ("counted_bookings_count", "INT NULL"),
                ("expected_meals_count", "INT NULL"),
                ("counted_meals_count", "INT NULL"),
                ("expected_laundry_count", "INT NULL"),
                ("counted_laundry_count", "INT NULL"),
                ("expected_services_count", "INT NULL"),
                ("counted_services_count", "INT NULL"),
                ("close_snapshot_json", "TEXT NULL"),
                ("treasury_handoff_at", "DATETIME NULL"),
                ("treasury_handoff_by_id", "INT NULL"),
                ("close_destination", "VARCHAR(20) NULL"),
                ("opening_bank", num14),
                ("received_from_shift_id", "INT NULL"),
                ("carried_to_shift_id", "INT NULL"),
                ("carried_to_employee_id", "INT NULL"),
            ):
                if _add_column(conn, "hotel_shifts", col, ddl):
                    added.append(f"hotel_shifts.{col}")
                    if col == "treasury_handoff_at":
                        hotel_handoff_col_added = True
            # مرة واحدة عند إضافة العمود — لا تُلغَى جلسات معلّقة لاحقاً عند كل ترقية
            if hotel_handoff_col_added:
                try:
                    conn.execute(
                        text(
                            "UPDATE hotel_shifts SET treasury_handoff_at = closed_at "
                            "WHERE status = 'CLOSED' AND closed_at IS NOT NULL "
                            "AND treasury_handoff_at IS NULL"
                        )
                    )
                except Exception:
                    pass
            try:
                conn.execute(
                    text(
                        "CREATE INDEX ix_hotel_shifts_treasury_handoff_at "
                        "ON hotel_shifts (treasury_handoff_at)"
                    )
                )
            except Exception:
                pass
        if _table_columns(conn, "hotel_booking_payment_refunds"):
            if _add_column(
                conn,
                "hotel_booking_payment_refunds",
                "payment_method_id",
                "INT NULL",
            ):
                added.append("hotel_booking_payment_refunds.payment_method_id")
                try:
                    # تعبئة قديمة من وسيلة الدفعة الأصلية
                    conn.execute(
                        text(
                            """
                            UPDATE hotel_booking_payment_refunds r
                            JOIN hotel_booking_payments p ON p.id = r.payment_id
                            SET r.payment_method_id = p.payment_method_id
                            WHERE r.payment_method_id IS NULL
                              AND p.payment_method_id IS NOT NULL
                            """
                        )
                    )
                except Exception:
                    pass

        tables = set(inspect(conn).get_table_names())
        if "hotel_booking_debts" not in tables:
            created_at_ddl = (
                "DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP"
                if dialect == "mysql"
                else "TIMESTAMPTZ NOT NULL DEFAULT NOW()"
            )
            pk_ddl = (
                "INT AUTO_INCREMENT PRIMARY KEY"
                if dialect == "mysql"
                else "SERIAL PRIMARY KEY"
            )
            ts_null = "DATETIME NULL" if dialect == "mysql" else "TIMESTAMPTZ NULL"
            _safe_exec(
                conn,
                f"""
                CREATE TABLE hotel_booking_debts (
                    id {pk_ddl},
                    booking_id INT NOT NULL,
                    amount DECIMAL(14,3) NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'OPEN',
                    reason TEXT NULL,
                    settlement_json TEXT NULL,
                    created_by_id INT NULL,
                    created_at {created_at_ddl},
                    collected_at {ts_null},
                    collected_by_id INT NULL,
                    collection_payment_id INT NULL,
                    written_off_at {ts_null},
                    written_off_by_id INT NULL,
                    write_off_reason TEXT NULL,
                    INDEX ix_hotel_booking_debts_booking_id (booking_id),
                    INDEX ix_hotel_booking_debts_status (status)
                )
                """
                if dialect == "mysql"
                else f"""
                CREATE TABLE hotel_booking_debts (
                    id {pk_ddl},
                    booking_id INTEGER NOT NULL REFERENCES hotel_bookings(id) ON DELETE CASCADE,
                    amount DECIMAL(14,3) NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'OPEN',
                    reason TEXT,
                    settlement_json TEXT,
                    created_by_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    created_at {created_at_ddl},
                    collected_at TIMESTAMPTZ,
                    collected_by_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    collection_payment_id INTEGER REFERENCES hotel_booking_payments(id) ON DELETE SET NULL,
                    written_off_at TIMESTAMPTZ,
                    written_off_by_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    write_off_reason TEXT
                )
                """,
                added,
                "hotel_booking_debts",
            )
            if dialect != "mysql":
                for idx, col in (
                    ("ix_hotel_booking_debts_booking_id", "booking_id"),
                    ("ix_hotel_booking_debts_status", "status"),
                ):
                    _safe_exec(
                        conn,
                        f"CREATE INDEX IF NOT EXISTS {idx} ON hotel_booking_debts ({col})",
                        added,
                        f"hotel_booking_debts.{col}_idx",
                    )

        tables = set(inspect(conn).get_table_names())
        if "hotel_booking_debts" in tables:
            debt_cols = _table_columns(conn, "hotel_booking_debts")
            if "amount_remaining" not in debt_cols:
                if _add_column(conn, "hotel_booking_debts", "amount_remaining", "DECIMAL(14,3) NULL"):
                    added.append("hotel_booking_debts.amount_remaining")
                try:
                    conn.execute(
                        text(
                            "UPDATE hotel_booking_debts SET amount_remaining = amount "
                            "WHERE amount_remaining IS NULL AND status = 'OPEN'"
                        )
                    )
                except Exception:
                    pass
            if "follow_up_notes" not in debt_cols:
                if _add_column(conn, "hotel_booking_debts", "follow_up_notes", "TEXT NULL"):
                    added.append("hotel_booking_debts.follow_up_notes")
            if "reminder_at" not in debt_cols:
                if _add_column(conn, "hotel_booking_debts", "reminder_at", "DATE NULL"):
                    added.append("hotel_booking_debts.reminder_at")
                try:
                    conn.execute(
                        text(
                            "CREATE INDEX ix_hotel_booking_debts_reminder_at "
                            "ON hotel_booking_debts (reminder_at)"
                        )
                    )
                except Exception:
                    pass
            if "reminder_time" not in debt_cols:
                if _add_column(conn, "hotel_booking_debts", "reminder_time", "VARCHAR(8) NULL"):
                    added.append("hotel_booking_debts.reminder_time")

        tables = set(inspect(conn).get_table_names())
        if "hotel_invoices" not in tables and "hotel_bookings" in tables:
            created_at_ddl = (
                "DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP"
                if dialect == "mysql"
                else "TIMESTAMPTZ NOT NULL DEFAULT NOW()"
            )
            pk_ddl = (
                "INT AUTO_INCREMENT PRIMARY KEY"
                if dialect == "mysql"
                else "SERIAL PRIMARY KEY"
            )
            ts_null = "DATETIME NULL" if dialect == "mysql" else "TIMESTAMPTZ NULL"
            _safe_exec(
                conn,
                f"""
                CREATE TABLE hotel_invoices (
                    id {pk_ddl},
                    booking_id INT NOT NULL,
                    invoice_number VARCHAR(32) NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'DRAFT',
                    subtotal DECIMAL(14,3) NOT NULL DEFAULT 0,
                    discount DECIMAL(14,3) NOT NULL DEFAULT 0,
                    tax DECIMAL(14,3) NOT NULL DEFAULT 0,
                    total DECIMAL(14,3) NOT NULL DEFAULT 0,
                    paid DECIMAL(14,3) NOT NULL DEFAULT 0,
                    issued_at {ts_null},
                    issued_by_id INT NULL,
                    created_at {created_at_ddl},
                    INDEX ix_hotel_invoices_booking_id (booking_id),
                    INDEX ix_hotel_invoices_invoice_number (invoice_number),
                    INDEX ix_hotel_invoices_status (status)
                )
                """
                if dialect == "mysql"
                else f"""
                CREATE TABLE hotel_invoices (
                    id {pk_ddl},
                    booking_id INTEGER NOT NULL REFERENCES hotel_bookings(id) ON DELETE RESTRICT,
                    invoice_number VARCHAR(32) NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'DRAFT',
                    subtotal DECIMAL(14,3) NOT NULL DEFAULT 0,
                    discount DECIMAL(14,3) NOT NULL DEFAULT 0,
                    tax DECIMAL(14,3) NOT NULL DEFAULT 0,
                    total DECIMAL(14,3) NOT NULL DEFAULT 0,
                    paid DECIMAL(14,3) NOT NULL DEFAULT 0,
                    issued_at TIMESTAMPTZ,
                    issued_by_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    created_at {created_at_ddl}
                )
                """,
                added,
                "hotel_invoices",
            )
        tables = set(inspect(conn).get_table_names())
        if "hotel_invoice_items" not in tables and "hotel_invoices" in tables:
            pk_ddl = (
                "INT AUTO_INCREMENT PRIMARY KEY"
                if dialect == "mysql"
                else "SERIAL PRIMARY KEY"
            )
            _safe_exec(
                conn,
                f"""
                CREATE TABLE hotel_invoice_items (
                    id {pk_ddl},
                    invoice_id INT NOT NULL,
                    description VARCHAR(255) NOT NULL,
                    quantity DECIMAL(14,4) NOT NULL DEFAULT 1,
                    unit_price DECIMAL(14,3) NOT NULL DEFAULT 0,
                    line_total DECIMAL(14,3) NOT NULL DEFAULT 0,
                    item_type VARCHAR(40) NOT NULL DEFAULT 'OTHER',
                    INDEX ix_hotel_invoice_items_invoice_id (invoice_id)
                )
                """
                if dialect == "mysql"
                else f"""
                CREATE TABLE hotel_invoice_items (
                    id {pk_ddl},
                    invoice_id INTEGER NOT NULL REFERENCES hotel_invoices(id) ON DELETE CASCADE,
                    description VARCHAR(255) NOT NULL,
                    quantity DECIMAL(14,4) NOT NULL DEFAULT 1,
                    unit_price DECIMAL(14,3) NOT NULL DEFAULT 0,
                    line_total DECIMAL(14,3) NOT NULL DEFAULT 0,
                    item_type VARCHAR(40) NOT NULL DEFAULT 'OTHER'
                )
                """,
                added,
                "hotel_invoice_items",
            )

        if _table_columns(conn, "hr_employees"):
            if _add_column(conn, "hr_employees", "is_hotel_front", "TINYINT(1) NOT NULL DEFAULT 0"):
                added.append("hr_employees.is_hotel_front")
        if _table_columns(conn, "purchases"):
            if _add_column(conn, "purchases", "hotel_shift_id", "INT NULL"):
                added.append("purchases.hotel_shift_id")

        if _table_columns(conn, "customers"):
            if _add_column(conn, "customers", "customer_type", "VARCHAR(20) NOT NULL DEFAULT 'INDIVIDUAL'"):
                added.append("customers.customer_type")
            if _add_column(conn, "customers", "company_name", "VARCHAR(200) NULL"):
                added.append("customers.company_name")
            if _add_column(conn, "customers", "wallet_balance", "DECIMAL(14,3) NOT NULL DEFAULT 0"):
                added.append("customers.wallet_balance")
            if _add_column(
                conn,
                "customers",
                "business_domain",
                "VARCHAR(20) NOT NULL DEFAULT 'restaurant'",
            ):
                added.append("customers.business_domain")
            if _add_column(conn, "customers", "loyalty_intro_sent_at", "DATETIME NULL"):
                added.append("customers.loyalty_intro_sent_at")
            if _add_column(conn, "customers", "parent_company_id", "INT NULL"):
                added.append("customers.parent_company_id")
            if _add_column(
                conn, "customers", "company_discount_percent", "DECIMAL(7,3) NOT NULL DEFAULT 0"
            ):
                added.append("customers.company_discount_percent")
            if _add_column(
                conn, "customers", "company_credit_limit", "DECIMAL(14,3) NOT NULL DEFAULT 0"
            ):
                added.append("customers.company_credit_limit")
            if _add_column(
                conn, "customers", "allow_company_credit", "TINYINT(1) NOT NULL DEFAULT 0"
            ):
                added.append("customers.allow_company_credit")
            if _add_column(
                conn,
                "customers",
                "company_notify_frequency",
                "VARCHAR(32) NOT NULL DEFAULT 'DAILY'",
            ):
                added.append("customers.company_notify_frequency")
            if _add_column(
                conn,
                "customers",
                "company_default_notify_to",
                "VARCHAR(16) NOT NULL DEFAULT 'COMPANY'",
            ):
                added.append("customers.company_default_notify_to")
            if _add_column(
                conn, "customers", "company_notify_last_period", "VARCHAR(32) NULL"
            ):
                added.append("customers.company_notify_last_period")
            if _add_column(
                conn, "customers", "company_notify_last_sent_at", "DATETIME NULL"
            ):
                added.append("customers.company_notify_last_sent_at")
        if _table_columns(conn, "hotel_bookings"):
            if _add_column(conn, "hotel_bookings", "company_customer_id", "INT NULL"):
                added.append("hotel_bookings.company_customer_id")
            if _add_column(conn, "hotel_bookings", "company_agreement_id", "INT NULL"):
                added.append("hotel_bookings.company_agreement_id")
            if _add_column(conn, "hotel_bookings", "booking_payer", "VARCHAR(20) NULL"):
                added.append("hotel_bookings.booking_payer")
            if _add_column(conn, "hotel_bookings", "stay_payer", "VARCHAR(20) NULL"):
                added.append("hotel_bookings.stay_payer")
            if _add_column(conn, "hotel_bookings", "extras_payer", "VARCHAR(20) NULL"):
                added.append("hotel_bookings.extras_payer")
            if _add_column(conn, "hotel_bookings", "notify_to", "VARCHAR(16) NULL"):
                added.append("hotel_bookings.notify_to")
            if _add_column(
                conn, "hotel_bookings", "notify_claim_last_period", "VARCHAR(32) NULL"
            ):
                added.append("hotel_bookings.notify_claim_last_period")
            if _add_column(
                conn, "hotel_bookings", "is_tourism_agency", "TINYINT(1) NOT NULL DEFAULT 0"
            ):
                added.append("hotel_bookings.is_tourism_agency")
            if _add_column(
                conn,
                "hotel_bookings",
                "tourism_commission_percent",
                "DECIMAL(7,3) NOT NULL DEFAULT 0",
            ):
                added.append("hotel_bookings.tourism_commission_percent")
            # تخمين/تصحيح من الحجوزات والمبيعات المرتبطة بغرف (آمن لإعادة التشغيل)
            if "business_domain" in _table_columns(conn, "customers"):
                try:
                    conn.execute(
                        text(
                            """
                            UPDATE customers c
                            SET business_domain = 'hotel'
                            WHERE business_domain = 'restaurant'
                              AND (
                                EXISTS (
                                    SELECT 1 FROM hotel_bookings hb
                                    WHERE hb.customer_id = c.id
                                )
                                OR EXISTS (
                                    SELECT 1 FROM sales s
                                    WHERE s.customer_id = c.id
                                      AND (
                                        s.booking_id IS NOT NULL
                                        OR UPPER(CAST(s.context_type AS CHAR)) = 'ROOM'
                                      )
                                )
                                OR EXISTS (
                                    SELECT 1 FROM hotel_bookings hb
                                    WHERE hb.guest_phone IS NOT NULL
                                      AND RIGHT(
                                        REPLACE(REPLACE(REPLACE(hb.guest_phone, '+', ''), ' ', ''), '-', ''),
                                        9
                                      ) = RIGHT(c.phone, 9)
                                )
                              )
                            """
                        )
                    )
                    conn.execute(
                        text(
                            """
                            UPDATE customers c
                            SET business_domain = 'shared'
                            WHERE business_domain = 'hotel'
                              AND EXISTS (
                                SELECT 1 FROM sales s
                                WHERE s.customer_id = c.id
                                  AND s.booking_id IS NULL
                                  AND (
                                    s.context_type IS NULL
                                    OR UPPER(CAST(s.context_type AS CHAR)) <> 'ROOM'
                                  )
                              )
                            """
                        )
                    )
                    added.append("customers.business_domain_backfill")
                except Exception:
                    pass

        tables = set(inspect(conn).get_table_names())
        if "customer_wallet_transactions" not in tables:
            created_at_ddl = (
                "DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP"
                if dialect == "mysql"
                else "TIMESTAMPTZ NOT NULL DEFAULT NOW()"
            )
            _safe_exec(
                conn,
                f"""
                CREATE TABLE customer_wallet_transactions (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    customer_id INT NOT NULL,
                    kind VARCHAR(20) NOT NULL DEFAULT 'ADJUST',
                    amount DECIMAL(14,3) NOT NULL DEFAULT 0,
                    note VARCHAR(255) NULL,
                    created_at {created_at_ddl},
                    created_by_id INT NULL,
                    FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE CASCADE,
                    FOREIGN KEY (created_by_id) REFERENCES users(id) ON DELETE SET NULL
                )
                """
                if dialect == "mysql"
                else f"""
                CREATE TABLE customer_wallet_transactions (
                    id SERIAL PRIMARY KEY,
                    customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
                    kind VARCHAR(20) NOT NULL DEFAULT 'ADJUST',
                    amount DECIMAL(14,3) NOT NULL DEFAULT 0,
                    note VARCHAR(255),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    created_by_id INTEGER REFERENCES users(id) ON DELETE SET NULL
                )
                """,
                added,
                "customer_wallet_transactions",
            )

        if _table_columns(conn, "hotel_booking_guests"):
            for col, ddl in (
                ("id_type", "VARCHAR(80) NULL"),
                ("nationality", "VARCHAR(80) NULL"),
                ("address", "TEXT NULL"),
                ("id_document_filename", "VARCHAR(255) NULL"),
            ):
                if _add_column(conn, "hotel_booking_guests", col, ddl):
                    added.append(f"hotel_booking_guests.{col}")

        bool_f = _bool_ddl(dialect, default=False)
        if _table_columns(conn, "hotel_rooms"):
            if _add_column(conn, "hotel_rooms", "show_online", bool_f):
                added.append("hotel_rooms.show_online")
            if _add_column(conn, "hotel_rooms", "online_description", "TEXT NULL"):
                added.append("hotel_rooms.online_description")
            if _add_column(conn, "hotel_rooms", "lock_no", "VARCHAR(16) NULL"):
                added.append("hotel_rooms.lock_no")
            if _add_column(conn, "hotel_rooms", "rooms_count", "INT NOT NULL DEFAULT 1"):
                added.append("hotel_rooms.rooms_count")
            if _add_column(conn, "hotel_rooms", "beds_count", "INT NOT NULL DEFAULT 1"):
                added.append("hotel_rooms.beds_count")
            added_double = _add_column(
                conn, "hotel_rooms", "double_beds_count", "INT NOT NULL DEFAULT 1"
            )
            if added_double:
                added.append("hotel_rooms.double_beds_count")
            added_single = _add_column(
                conn, "hotel_rooms", "single_beds_count", "INT NOT NULL DEFAULT 0"
            )
            if added_single:
                added.append("hotel_rooms.single_beds_count")
            if added_double or added_single:
                try:
                    conn.execute(
                        text(
                            """
                            UPDATE hotel_rooms
                            SET double_beds_count = COALESCE(beds_count, 1),
                                single_beds_count = 0,
                                beds_count = COALESCE(beds_count, 1)
                            """
                        )
                    )
                except Exception:
                    pass
            if _add_column(conn, "hotel_rooms", "allows_infant", bool_f):
                added.append("hotel_rooms.allows_infant")

        tables = set(inspect(conn).get_table_names())
        if "hotel_room_media" not in tables:
            _safe_exec(
                conn,
                f"""
                CREATE TABLE hotel_room_media (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    room_id INT NOT NULL,
                    kind VARCHAR(20) NOT NULL DEFAULT 'IMAGE',
                    filename VARCHAR(255) NOT NULL,
                    caption VARCHAR(200) NULL,
                    sort_order INT NOT NULL DEFAULT 0,
                    is_active {bool_t},
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (room_id) REFERENCES hotel_rooms(id) ON DELETE CASCADE
                )
                """
                if dialect == "mysql"
                else f"""
                CREATE TABLE hotel_room_media (
                    id SERIAL PRIMARY KEY,
                    room_id INTEGER NOT NULL REFERENCES hotel_rooms(id) ON DELETE CASCADE,
                    kind VARCHAR(20) NOT NULL DEFAULT 'IMAGE',
                    filename VARCHAR(255) NOT NULL,
                    caption VARCHAR(200),
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """,
                added,
                "hotel_room_media",
            )

        if _table_columns(conn, "hotel_room_types"):
            bool_extra = _bool_ddl(dialect, default=False)
            for col, ddl in (
                ("max_occupancy", "INT NULL"),
                ("beds_description", "VARCHAR(200) NULL"),
                ("allows_extra_bed", bool_extra),
            ):
                if _add_column(conn, "hotel_room_types", col, ddl):
                    added.append(f"hotel_room_types.{col}")

        if _table_columns(conn, "hotel_bookings"):
            if _add_column(conn, "hotel_bookings", "final_invoice_number", "VARCHAR(32) NULL"):
                added.append("hotel_bookings.final_invoice_number")
            bool_fu = _bool_ddl(dialect, default=False)
            for col, ddl in (
                ("follow_up_at", "DATE NULL"),
                ("follow_up_note", "TEXT NULL"),
                ("claim_wa_until_paid", bool_fu),
                ("planned_check_in", "DATE NULL"),
                ("first_chargeable_night", "DATE NULL"),
            ):
                if _add_column(conn, "hotel_bookings", col, ddl):
                    added.append(f"hotel_bookings.{col}")
            if "follow_up_at" in _table_columns(conn, "hotel_bookings"):
                try:
                    conn.execute(
                        text(
                            "CREATE INDEX ix_hotel_bookings_follow_up_at "
                            "ON hotel_bookings (follow_up_at)"
                        )
                    )
                except Exception:
                    pass
        if _table_columns(conn, "hotel_booking_payments"):
            if _add_column(conn, "hotel_booking_payments", "receipt_number", "VARCHAR(32) NULL"):
                added.append("hotel_booking_payments.receipt_number")
            if _add_column(conn, "hotel_booking_payments", "hotel_shift_id", "INT NULL"):
                added.append("hotel_booking_payments.hotel_shift_id")
            if _add_column(
                conn, "hotel_booking_payments", "received_by_employee_id", "INT NULL"
            ):
                added.append("hotel_booking_payments.received_by_employee_id")
        if _table_columns(conn, "hotel_booking_payment_refunds"):
            if _add_column(
                conn, "hotel_booking_payment_refunds", "hotel_shift_id", "INT NULL"
            ):
                added.append("hotel_booking_payment_refunds.hotel_shift_id")
            if _add_column(
                conn, "hotel_booking_payment_refunds", "approved_by_employee_id", "INT NULL"
            ):
                added.append("hotel_booking_payment_refunds.approved_by_employee_id")
        if _table_columns(conn, "sales"):
            for col, ddl in (
                ("receipt_number", "VARCHAR(32) NULL"),
                ("final_invoice_number", "VARCHAR(32) NULL"),
            ):
                if _add_column(conn, "sales", col, ddl):
                    added.append(f"sales.{col}")

        if "hotel_service_catalog" not in tables:
            bool_svc = _bool_ddl(dialect, default=True)
            _safe_exec(
                conn,
                f"""
                CREATE TABLE hotel_service_catalog (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    name_ar VARCHAR(160) NOT NULL,
                    default_price DECIMAL(14, 3) NOT NULL DEFAULT 0,
                    product_id INT NULL,
                    sort_order INT NOT NULL DEFAULT 0,
                    is_active {bool_svc},
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE SET NULL
                )
                """
                if dialect == "mysql"
                else f"""
                CREATE TABLE hotel_service_catalog (
                    id SERIAL PRIMARY KEY,
                    name_ar VARCHAR(160) NOT NULL,
                    default_price NUMERIC(14, 3) NOT NULL DEFAULT 0,
                    product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """,
                added,
                "hotel_service_catalog",
            )

        # إفطار مشمول: تكلفة فندق لا تُحمَّل على النزيل
        bool_true = _bool_ddl(dialect, default=True)
        bool_false = _bool_ddl(dialect, default=False)
        if _table_columns(conn, "hotel_booking_services"):
            if _add_column(
                conn, "hotel_booking_services", "charged_to_guest", bool_true
            ):
                added.append("hotel_booking_services.charged_to_guest")
            if _add_column(
                conn,
                "hotel_booking_services",
                "sale_id",
                "INT NULL" if dialect == "mysql" else "INTEGER",
            ):
                added.append("hotel_booking_services.sale_id")
                try:
                    conn.execute(
                        text(
                            "CREATE INDEX ix_hotel_booking_services_sale_id "
                            "ON hotel_booking_services (sale_id)"
                        )
                    )
                except Exception:
                    pass
            for col, ddl in (
                ("service_code", "VARCHAR(40) NULL"),
                ("folio_side", "VARCHAR(16) NULL"),
                ("company_amount", "DECIMAL(14,3) NULL"),
                ("guest_amount", "DECIMAL(14,3) NULL"),
            ):
                if _add_column(conn, "hotel_booking_services", col, ddl):
                    added.append(f"hotel_booking_services.{col}")
        if _table_columns(conn, "products"):
            if _add_column(conn, "products", "is_hotel_breakfast", bool_false):
                added.append("products.is_hotel_breakfast")

        # SEO entity columns (SEO Center / external agents)
        bool_idx = _bool_ddl(dialect, default=True)
        seo_cols = (
            ("seo_title", "VARCHAR(255) NULL"),
            ("seo_description", "VARCHAR(500) NULL"),
            ("seo_h1", "VARCHAR(255) NULL"),
            ("seo_slug", "VARCHAR(255) NULL"),
            ("seo_keywords", "VARCHAR(500) NULL"),
            ("seo_schema_json", "TEXT NULL"),
            ("seo_og_title", "VARCHAR(255) NULL"),
            ("seo_og_description", "VARCHAR(500) NULL"),
            ("seo_og_image", "VARCHAR(500) NULL"),
            ("seo_indexable", bool_idx),
            ("seo_canonical_url", "VARCHAR(500) NULL"),
            (
                "seo_updated_at",
                "DATETIME NULL" if dialect == "mysql" else "TIMESTAMPTZ NULL",
            ),
        )
        for table in (
            "products",
            "product_categories",
            "hotel_rooms",
            "hotel_service_catalog",
        ):
            if not _table_columns(conn, table):
                continue
            for col, ddl in seo_cols:
                if _add_column(conn, table, col, ddl):
                    added.append(f"{table}.{col}")

        # OTP اعتماد الاسترداد (مشرف واتساب)
        if not _table_columns(conn, "supervisor_otp_challenges"):
            if dialect == "mysql":
                _safe_exec(
                    conn,
                    """
                    CREATE TABLE supervisor_otp_challenges (
                      id INT AUTO_INCREMENT PRIMARY KEY,
                      purpose VARCHAR(40) NOT NULL,
                      domain VARCHAR(20) NOT NULL,
                      ref_type VARCHAR(20) NOT NULL,
                      ref_id INT NOT NULL,
                      code_hash VARCHAR(128) NOT NULL,
                      expires_at DATETIME NOT NULL,
                      consumed_at DATETIME NULL,
                      attempts INT NOT NULL DEFAULT 0,
                      requested_by_user_id INT NULL,
                      created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      INDEX ix_supervisor_otp_purpose (purpose),
                      INDEX ix_supervisor_otp_domain (domain),
                      INDEX ix_supervisor_otp_ref (ref_type, ref_id),
                      INDEX ix_supervisor_otp_expires (expires_at)
                    )
                    """,
                    added,
                    "supervisor_otp_challenges",
                )
            else:
                _safe_exec(
                    conn,
                    """
                    CREATE TABLE supervisor_otp_challenges (
                      id SERIAL PRIMARY KEY,
                      purpose VARCHAR(40) NOT NULL,
                      domain VARCHAR(20) NOT NULL,
                      ref_type VARCHAR(20) NOT NULL,
                      ref_id INTEGER NOT NULL,
                      code_hash VARCHAR(128) NOT NULL,
                      expires_at TIMESTAMPTZ NOT NULL,
                      consumed_at TIMESTAMPTZ NULL,
                      attempts INTEGER NOT NULL DEFAULT 0,
                      requested_by_user_id INTEGER NULL,
                      created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """,
                    added,
                    "supervisor_otp_challenges",
                )

        if not _table_columns(conn, "hotel_company_agreement_change_requests"):
            if dialect == "mysql":
                _safe_exec(
                    conn,
                    """
                    CREATE TABLE hotel_company_agreement_change_requests (
                      id INT AUTO_INCREMENT PRIMARY KEY,
                      company_customer_id INT NOT NULL,
                      agreement_id INT NULL,
                      booking_id INT NULL,
                      requested_by_user_id INT NULL,
                      status VARCHAR(16) NOT NULL DEFAULT 'PENDING',
                      note TEXT NULL,
                      attachment_path VARCHAR(260) NULL,
                      attachment_name VARCHAR(180) NULL,
                      items_json TEXT NOT NULL,
                      created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      reviewed_at DATETIME NULL,
                      reviewed_by_user_id INT NULL,
                      review_note TEXT NULL,
                      INDEX ix_agr_chg_req_company (company_customer_id),
                      INDEX ix_agr_chg_req_status (status),
                      INDEX ix_agr_chg_req_booking (booking_id)
                    )
                    """,
                    added,
                    "hotel_company_agreement_change_requests",
                )
            else:
                _safe_exec(
                    conn,
                    """
                    CREATE TABLE hotel_company_agreement_change_requests (
                      id SERIAL PRIMARY KEY,
                      company_customer_id INTEGER NOT NULL,
                      agreement_id INTEGER NULL,
                      booking_id INTEGER NULL,
                      requested_by_user_id INTEGER NULL,
                      status VARCHAR(16) NOT NULL DEFAULT 'PENDING',
                      note TEXT NULL,
                      attachment_path VARCHAR(260) NULL,
                      attachment_name VARCHAR(180) NULL,
                      items_json TEXT NOT NULL DEFAULT '[]',
                      created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                      reviewed_at TIMESTAMPTZ NULL,
                      reviewed_by_user_id INTEGER NULL,
                      review_note TEXT NULL
                    )
                    """,
                    added,
                    "hotel_company_agreement_change_requests",
                )

        if not _table_columns(conn, "shift_variances"):
            if dialect == "mysql":
                _safe_exec(
                    conn,
                    """
                    CREATE TABLE shift_variances (
                      id INT AUTO_INCREMENT PRIMARY KEY,
                      ref VARCHAR(20) NOT NULL,
                      source_type VARCHAR(24) NOT NULL,
                      kind VARCHAR(8) NOT NULL,
                      status VARCHAR(24) NOT NULL DEFAULT 'PENDING_REVIEW',
                      hotel_shift_id INT NULL,
                      pos_shift_id INT NULL,
                      from_employee_id INT NULL,
                      to_employee_id INT NULL,
                      claimed_amount DECIMAL(14,3) NOT NULL,
                      received_amount DECIMAL(14,3) NOT NULL,
                      difference DECIMAL(14,3) NOT NULL,
                      note TEXT NULL,
                      created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      resolved_at DATETIME NULL,
                      resolved_by_id INT NULL,
                      resolved_note TEXT NULL,
                      payroll_deduction_id INT NULL,
                      UNIQUE KEY uq_shift_variances_ref (ref),
                      INDEX ix_shift_variances_status (status),
                      INDEX ix_shift_variances_source (source_type),
                      INDEX ix_shift_variances_hotel (hotel_shift_id),
                      INDEX ix_shift_variances_pos (pos_shift_id)
                    )
                    """,
                    added,
                    "shift_variances",
                )
            else:
                _safe_exec(
                    conn,
                    """
                    CREATE TABLE shift_variances (
                      id SERIAL PRIMARY KEY,
                      ref VARCHAR(20) NOT NULL UNIQUE,
                      source_type VARCHAR(24) NOT NULL,
                      kind VARCHAR(8) NOT NULL,
                      status VARCHAR(24) NOT NULL DEFAULT 'PENDING_REVIEW',
                      hotel_shift_id INTEGER NULL,
                      pos_shift_id INTEGER NULL,
                      from_employee_id INTEGER NULL,
                      to_employee_id INTEGER NULL,
                      claimed_amount NUMERIC(14,3) NOT NULL,
                      received_amount NUMERIC(14,3) NOT NULL,
                      difference NUMERIC(14,3) NOT NULL,
                      note TEXT NULL,
                      created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                      resolved_at TIMESTAMPTZ NULL,
                      resolved_by_id INTEGER NULL,
                      resolved_note TEXT NULL,
                      payroll_deduction_id INTEGER NULL
                    )
                    """,
                    added,
                    "shift_variances",
                )

        if not _table_columns(conn, "shift_handovers"):
            if dialect == "mysql":
                _safe_exec(
                    conn,
                    """
                    CREATE TABLE shift_handovers (
                      id INT AUTO_INCREMENT PRIMARY KEY,
                      ref VARCHAR(20) NOT NULL,
                      kind VARCHAR(20) NOT NULL,
                      status VARCHAR(16) NOT NULL DEFAULT 'SENT',
                      domain VARCHAR(16) NOT NULL,
                      hotel_shift_id INT NULL,
                      pos_shift_id INT NULL,
                      from_employee_id INT NULL,
                      to_employee_id INT NULL,
                      claimed_cash DECIMAL(14,3) NOT NULL DEFAULT 0,
                      claimed_bank DECIMAL(14,3) NOT NULL DEFAULT 0,
                      received_cash DECIMAL(14,3) NULL,
                      received_bank DECIMAL(14,3) NULL,
                      handover_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      confirmed_at DATETIME NULL,
                      bank_name VARCHAR(80) NULL,
                      bank_ref VARCHAR(80) NULL,
                      bank_transferred_at DATETIME NULL,
                      note TEXT NULL,
                      created_by_id INT NULL,
                      confirmed_by_id INT NULL,
                      created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      UNIQUE KEY uq_shift_handovers_ref (ref),
                      INDEX ix_shift_handovers_hotel (hotel_shift_id),
                      INDEX ix_shift_handovers_pos (pos_shift_id),
                      INDEX ix_shift_handovers_status (status)
                    )
                    """,
                    added,
                    "shift_handovers",
                )
            else:
                _safe_exec(
                    conn,
                    """
                    CREATE TABLE shift_handovers (
                      id SERIAL PRIMARY KEY,
                      ref VARCHAR(20) NOT NULL UNIQUE,
                      kind VARCHAR(20) NOT NULL,
                      status VARCHAR(16) NOT NULL DEFAULT 'SENT',
                      domain VARCHAR(16) NOT NULL,
                      hotel_shift_id INTEGER NULL,
                      pos_shift_id INTEGER NULL,
                      from_employee_id INTEGER NULL,
                      to_employee_id INTEGER NULL,
                      claimed_cash NUMERIC(14,3) NOT NULL DEFAULT 0,
                      claimed_bank NUMERIC(14,3) NOT NULL DEFAULT 0,
                      received_cash NUMERIC(14,3) NULL,
                      received_bank NUMERIC(14,3) NULL,
                      handover_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                      confirmed_at TIMESTAMPTZ NULL,
                      bank_name VARCHAR(80) NULL,
                      bank_ref VARCHAR(80) NULL,
                      bank_transferred_at TIMESTAMPTZ NULL,
                      note TEXT NULL,
                      created_by_id INTEGER NULL,
                      confirmed_by_id INTEGER NULL,
                      created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """,
                    added,
                    "shift_handovers",
                )

        if not _table_columns(conn, "treasury_sessions"):
            if dialect == "mysql":
                _safe_exec(
                    conn,
                    """
                    CREATE TABLE treasury_sessions (
                      id INT AUTO_INCREMENT PRIMARY KEY,
                      status VARCHAR(12) NOT NULL DEFAULT 'OPEN',
                      opened_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      opened_by_id INT NULL,
                      opening_cash DECIMAL(14,3) NOT NULL DEFAULT 0,
                      opening_bank DECIMAL(14,3) NOT NULL DEFAULT 0,
                      closed_at DATETIME NULL,
                      closed_by_id INT NULL,
                      counted_cash DECIMAL(14,3) NULL,
                      counted_bank DECIMAL(14,3) NULL,
                      expected_cash DECIMAL(14,3) NULL,
                      expected_bank DECIMAL(14,3) NULL,
                      cash_in DECIMAL(14,3) NOT NULL DEFAULT 0,
                      cash_out DECIMAL(14,3) NOT NULL DEFAULT 0,
                      bank_in DECIMAL(14,3) NOT NULL DEFAULT 0,
                      bank_out DECIMAL(14,3) NOT NULL DEFAULT 0,
                      advances_out DECIMAL(14,3) NOT NULL DEFAULT 0,
                      close_note TEXT NULL,
                      INDEX ix_treasury_sessions_status (status)
                    )
                    """,
                    added,
                    "treasury_sessions",
                )
            else:
                _safe_exec(
                    conn,
                    """
                    CREATE TABLE treasury_sessions (
                      id SERIAL PRIMARY KEY,
                      status VARCHAR(12) NOT NULL DEFAULT 'OPEN',
                      opened_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                      opened_by_id INTEGER NULL,
                      opening_cash NUMERIC(14,3) NOT NULL DEFAULT 0,
                      opening_bank NUMERIC(14,3) NOT NULL DEFAULT 0,
                      closed_at TIMESTAMPTZ NULL,
                      closed_by_id INTEGER NULL,
                      counted_cash NUMERIC(14,3) NULL,
                      counted_bank NUMERIC(14,3) NULL,
                      expected_cash NUMERIC(14,3) NULL,
                      expected_bank NUMERIC(14,3) NULL,
                      cash_in NUMERIC(14,3) NOT NULL DEFAULT 0,
                      cash_out NUMERIC(14,3) NOT NULL DEFAULT 0,
                      bank_in NUMERIC(14,3) NOT NULL DEFAULT 0,
                      bank_out NUMERIC(14,3) NOT NULL DEFAULT 0,
                      advances_out NUMERIC(14,3) NOT NULL DEFAULT 0,
                      close_note TEXT NULL
                    )
                    """,
                    added,
                    "treasury_sessions",
                )

        if not _table_columns(conn, "purchase_advances"):
            if dialect == "mysql":
                _safe_exec(
                    conn,
                    """
                    CREATE TABLE purchase_advances (
                      id INT AUTO_INCREMENT PRIMARY KEY,
                      ref VARCHAR(24) NOT NULL,
                      status VARCHAR(12) NOT NULL DEFAULT 'OPEN',
                      employee_id INT NOT NULL,
                      amount DECIMAL(14,3) NOT NULL,
                      returned_amount DECIMAL(14,3) NOT NULL DEFAULT 0,
                      source_pm_id INT NOT NULL,
                      custody_pm_id INT NOT NULL,
                      purpose VARCHAR(200) NULL,
                      domain VARCHAR(16) NOT NULL DEFAULT 'restaurant',
                      transfer_id INT NULL,
                      return_transfer_id INT NULL,
                      created_by_id INT NULL,
                      created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      closed_at DATETIME NULL,
                      closed_by_id INT NULL,
                      note TEXT NULL,
                      UNIQUE KEY uq_purchase_advances_ref (ref),
                      INDEX ix_purchase_advances_status (status),
                      INDEX ix_purchase_advances_employee (employee_id)
                    )
                    """,
                    added,
                    "purchase_advances",
                )
            else:
                _safe_exec(
                    conn,
                    """
                    CREATE TABLE purchase_advances (
                      id SERIAL PRIMARY KEY,
                      ref VARCHAR(24) NOT NULL UNIQUE,
                      status VARCHAR(12) NOT NULL DEFAULT 'OPEN',
                      employee_id INTEGER NOT NULL,
                      amount NUMERIC(14,3) NOT NULL,
                      returned_amount NUMERIC(14,3) NOT NULL DEFAULT 0,
                      source_pm_id INTEGER NOT NULL,
                      custody_pm_id INTEGER NOT NULL,
                      purpose VARCHAR(200) NULL,
                      domain VARCHAR(16) NOT NULL DEFAULT 'restaurant',
                      transfer_id INTEGER NULL,
                      return_transfer_id INTEGER NULL,
                      created_by_id INTEGER NULL,
                      created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                      closed_at TIMESTAMPTZ NULL,
                      closed_by_id INTEGER NULL,
                      note TEXT NULL
                    )
                    """,
                    added,
                    "purchase_advances",
                )

        tables = set(inspect(conn).get_table_names())
        if "user_wallet_access" not in tables:
            if dialect == "mysql":
                _safe_exec(
                    conn,
                    """
                    CREATE TABLE user_wallet_access (
                      user_id INT NOT NULL,
                      payment_method_id INT NOT NULL,
                      can_send TINYINT(1) NOT NULL DEFAULT 0,
                      can_receive TINYINT(1) NOT NULL DEFAULT 0,
                      PRIMARY KEY (user_id, payment_method_id),
                      CONSTRAINT fk_uwa_user FOREIGN KEY (user_id)
                          REFERENCES users(id) ON DELETE CASCADE,
                      CONSTRAINT fk_uwa_pm FOREIGN KEY (payment_method_id)
                          REFERENCES payment_methods(id) ON DELETE CASCADE
                    )
                    """,
                    added,
                    "user_wallet_access",
                )
            else:
                _safe_exec(
                    conn,
                    """
                    CREATE TABLE user_wallet_access (
                      user_id INTEGER NOT NULL,
                      payment_method_id INTEGER NOT NULL,
                      can_send BOOLEAN NOT NULL DEFAULT FALSE,
                      can_receive BOOLEAN NOT NULL DEFAULT FALSE,
                      PRIMARY KEY (user_id, payment_method_id)
                    )
                    """,
                    added,
                    "user_wallet_access",
                )

    if added:
        log.info("server schema patch applied: %s", ", ".join(added))
