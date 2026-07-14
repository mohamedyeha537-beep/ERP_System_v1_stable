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
            ):
                if _add_column(conn, "hotel_shifts", col, ddl):
                    added.append(f"hotel_shifts.{col}")
        tables = set(inspect(conn).get_table_names())
        if "hotel_booking_debts" not in tables:
            created_at_ddl = (
                "DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP"
                if dialect == "mysql"
                else "TIMESTAMPTZ NOT NULL DEFAULT NOW()"
            )
            _safe_exec(
                conn,
                f"""
                CREATE TABLE hotel_booking_debts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    booking_id INTEGER NOT NULL REFERENCES hotel_bookings(id) ON DELETE CASCADE,
                    amount DECIMAL(14,3) NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'OPEN',
                    reason TEXT,
                    settlement_json TEXT,
                    created_by_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    created_at {created_at_ddl},
                    collected_at DATETIME,
                    collected_by_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    collection_payment_id INTEGER REFERENCES hotel_booking_payments(id) ON DELETE SET NULL,
                    written_off_at DATETIME,
                    written_off_by_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    write_off_reason TEXT
                )
                """
                if dialect == "sqlite"
                else f"""
                CREATE TABLE hotel_booking_debts (
                    id SERIAL PRIMARY KEY,
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

    if added:
        log.info("server schema patch applied: %s", ", ".join(added))
