"""إضافة أعمدة/جداول ناقصة لقواعد SQLite القديمة بعد تطوير النماذج."""

from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine


def patch_sqlite_schema(engine: Engine) -> None:
    url = str(engine.url)
    if not url.startswith("sqlite"):
        return

    insp = inspect(engine)
    names = set(insp.get_table_names())

    with engine.begin() as conn:
        if "product_categories" not in names:
            conn.execute(
                text(
                    """
                CREATE TABLE product_categories (
                    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                    name_ar VARCHAR(120) NOT NULL,
                    parent_id INTEGER,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    color_hex VARCHAR(7) NOT NULL DEFAULT '#3b82f6',
                    delete_protected BOOLEAN NOT NULL DEFAULT 0,
                    FOREIGN KEY(parent_id) REFERENCES product_categories (id) ON DELETE CASCADE
                )
                """
                )
            )

        insp = inspect(engine)
        names = set(insp.get_table_names())
        if "product_categories" in names:
            cat_cols = {c["name"] for c in insp.get_columns("product_categories")}
            if "delete_protected" not in cat_cols:
                conn.execute(
                    text(
                        "ALTER TABLE product_categories ADD COLUMN delete_protected BOOLEAN NOT NULL DEFAULT 0"
                    )
                )
            if "routing_mode" not in cat_cols:
                conn.execute(
                    text(
                        "ALTER TABLE product_categories ADD COLUMN routing_mode VARCHAR(20) NOT NULL DEFAULT 'NONE'"
                    )
                )
            if "routing_target" not in cat_cols:
                conn.execute(
                    text(
                        "ALTER TABLE product_categories ADD COLUMN routing_target VARCHAR(255)"
                    )
                )

        # جدول الطاولات
        insp = inspect(engine)
        names = set(insp.get_table_names())
        if "dining_tables" not in names:
            conn.execute(
                text(
                    """
                CREATE TABLE dining_tables (
                    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                    name_ar VARCHAR(80) NOT NULL UNIQUE,
                    section_category_id INTEGER,
                    capacity INTEGER NOT NULL DEFAULT 4,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    notes TEXT,
                    FOREIGN KEY(section_category_id) REFERENCES product_categories(id) ON DELETE SET NULL
                )
                """
                )
            )

        # جدول تذاكر المطبخ
        insp = inspect(engine)
        names = set(insp.get_table_names())
        if "kitchen_tickets" not in names:
            conn.execute(
                text(
                    """
                CREATE TABLE kitchen_tickets (
                    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                    sale_id INTEGER NOT NULL,
                    root_category_id INTEGER NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'PENDING',
                    routing_mode VARCHAR(20) NOT NULL DEFAULT 'SCREEN',
                    delivery_status VARCHAR(40) NOT NULL DEFAULT 'PENDING',
                    delivery_info VARCHAR(500),
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    served_at DATETIME,
                    FOREIGN KEY(sale_id) REFERENCES sales(id) ON DELETE CASCADE,
                    FOREIGN KEY(root_category_id) REFERENCES product_categories(id) ON DELETE CASCADE
                )
                """
                )
            )
            conn.execute(
                text("CREATE INDEX IF NOT EXISTS ix_kitchen_tickets_sale_id ON kitchen_tickets (sale_id)")
            )
            conn.execute(
                text("CREATE INDEX IF NOT EXISTS ix_kitchen_tickets_root_category_id ON kitchen_tickets (root_category_id)")
            )
            conn.execute(
                text("CREATE INDEX IF NOT EXISTS ix_kitchen_tickets_status ON kitchen_tickets (status)")
            )

        insp = inspect(engine)
        names = set(insp.get_table_names())
        if "delivery_zones" not in names:
            conn.execute(
                text(
                    """
                CREATE TABLE delivery_zones (
                    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                    name_ar VARCHAR(120) NOT NULL UNIQUE,
                    fee NUMERIC(14, 3) NOT NULL DEFAULT 0,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    notes TEXT,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
                )
            )
            conn.execute(
                text("CREATE INDEX IF NOT EXISTS ix_delivery_zones_is_active ON delivery_zones (is_active)")
            )

        insp = inspect(engine)
        names = set(insp.get_table_names())
        if "delivery_cash_settlements" not in names:
            conn.execute(
                text(
                    """
                CREATE TABLE delivery_cash_settlements (
                    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                    sale_id INTEGER NOT NULL UNIQUE,
                    cash_method_id INTEGER NOT NULL,
                    zone_id INTEGER,
                    amount NUMERIC(14, 3) NOT NULL DEFAULT 0,
                    note TEXT,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    created_by_id INTEGER,
                    FOREIGN KEY(sale_id) REFERENCES sales(id) ON DELETE CASCADE,
                    FOREIGN KEY(cash_method_id) REFERENCES payment_methods(id) ON DELETE RESTRICT,
                    FOREIGN KEY(zone_id) REFERENCES delivery_zones(id) ON DELETE SET NULL,
                    FOREIGN KEY(created_by_id) REFERENCES users(id) ON DELETE SET NULL
                )
                """
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_delivery_cash_settlements_cash_method_id ON delivery_cash_settlements (cash_method_id)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_delivery_cash_settlements_zone_id ON delivery_cash_settlements (zone_id)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_delivery_cash_settlements_created_at ON delivery_cash_settlements (created_at)"
                )
            )

        insp = inspect(engine)
        names = set(insp.get_table_names())
        if "sales" in names:
            scols = {c["name"] for c in insp.get_columns("sales")}
            if "table_id" not in scols:
                conn.execute(text("ALTER TABLE sales ADD COLUMN table_id INTEGER REFERENCES dining_tables(id)"))
            if "context_type" not in scols:
                conn.execute(
                    text(
                        "ALTER TABLE sales ADD COLUMN context_type VARCHAR(20) NOT NULL DEFAULT 'TABLE'"
                    )
                )
            if "customer_id" not in scols:
                conn.execute(
                    text(
                        "ALTER TABLE sales ADD COLUMN customer_id INTEGER REFERENCES customers(id)"
                    )
                )
            if "external_order_type" not in scols:
                conn.execute(
                    text(
                        "ALTER TABLE sales ADD COLUMN external_order_type VARCHAR(20) NOT NULL DEFAULT 'PICKUP'"
                    )
                )
            if "delivery_zone_id" not in scols:
                conn.execute(
                    text(
                        "ALTER TABLE sales ADD COLUMN delivery_zone_id INTEGER REFERENCES delivery_zones(id)"
                    )
                )
            if "delivery_zone_name" not in scols:
                conn.execute(
                    text("ALTER TABLE sales ADD COLUMN delivery_zone_name VARCHAR(120)")
                )
            if "delivery_fee" not in scols:
                conn.execute(
                    text(
                        "ALTER TABLE sales ADD COLUMN delivery_fee NUMERIC(14, 3) NOT NULL DEFAULT 0"
                    )
                )

        insp = inspect(engine)
        names = set(insp.get_table_names())
        if "products" not in names:
            return

        cols = {c["name"] for c in insp.get_columns("products")}
        if "category_id" not in cols:
            conn.execute(text("ALTER TABLE products ADD COLUMN category_id INTEGER"))
        if "barcode" not in cols:
            conn.execute(text("ALTER TABLE products ADD COLUMN barcode VARCHAR(64)"))
        if "image_filename" not in cols:
            conn.execute(text("ALTER TABLE products ADD COLUMN image_filename VARCHAR(255)"))

        # ترقية جدول المشتريات لإضافة kind / expense_category إن كانت قاعدة قديمة
        insp = inspect(engine)
        names = set(insp.get_table_names())
        if "purchases" in names:
            pcols = {c["name"] for c in insp.get_columns("purchases")}
            if "kind" not in pcols:
                conn.execute(
                    text(
                        "ALTER TABLE purchases ADD COLUMN kind VARCHAR(20) NOT NULL DEFAULT 'EXPENSE'"
                    )
                )
            if "expense_category" not in pcols:
                conn.execute(
                    text("ALTER TABLE purchases ADD COLUMN expense_category VARCHAR(80)")
                )

        # ترقية جدول purchase_lines لجعل product_id قابلاً للقيمة NULL
        # وإضافة الأعمدة item_name / unit (لدعم فواتير الأصول/الأدوات)
        insp = inspect(engine)
        if "purchase_lines" in set(insp.get_table_names()):
            lcols = {c["name"]: c for c in insp.get_columns("purchase_lines")}
            needs_rebuild = ("item_name" not in lcols) or ("unit" not in lcols) or (
                lcols.get("product_id") is not None
                and lcols["product_id"].get("nullable") is False
            )
            if needs_rebuild:
                conn.execute(
                    text(
                        """
                        CREATE TABLE purchase_lines_new (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            purchase_id INTEGER NOT NULL,
                            product_id INTEGER,
                            item_name VARCHAR(255),
                            unit VARCHAR(32),
                            quantity NUMERIC(14, 4) NOT NULL,
                            unit_cost NUMERIC(14, 3) NOT NULL,
                            line_total NUMERIC(14, 3) NOT NULL,
                            FOREIGN KEY (purchase_id) REFERENCES purchases(id) ON DELETE CASCADE,
                            FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE RESTRICT
                        )
                        """
                    )
                )
                # نسخ الأعمدة القديمة الموجودة فقط (item_name/unit ستبقى NULL)
                old_extra = []
                if "item_name" in lcols:
                    old_extra.append("item_name")
                else:
                    old_extra.append("NULL")
                if "unit" in lcols:
                    old_extra.append("unit")
                else:
                    old_extra.append("NULL")
                conn.execute(
                    text(
                        f"""
                        INSERT INTO purchase_lines_new (id, purchase_id, product_id, item_name, unit, quantity, unit_cost, line_total)
                        SELECT id, purchase_id, product_id, {old_extra[0]}, {old_extra[1]}, quantity, unit_cost, line_total
                        FROM purchase_lines
                        """
                    )
                )
                conn.execute(text("DROP TABLE purchase_lines"))
                conn.execute(
                    text("ALTER TABLE purchase_lines_new RENAME TO purchase_lines")
                )
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_purchase_lines_purchase_id ON purchase_lines (purchase_id)"
                    )
                )

            # إضافة حقول الإهلاك (للأصول الثابتة) — تُضاف دائماً إن لم تكن موجودة
            insp = inspect(engine)
            lcols = {c["name"]: c for c in insp.get_columns("purchase_lines")}
            if "useful_life_months" not in lcols:
                conn.execute(
                    text(
                        "ALTER TABLE purchase_lines ADD COLUMN useful_life_months INTEGER NOT NULL DEFAULT 0"
                    )
                )
            if "salvage_value" not in lcols:
                conn.execute(
                    text(
                        "ALTER TABLE purchase_lines ADD COLUMN salvage_value NUMERIC(14, 3) NOT NULL DEFAULT 0"
                    )
                )
            if "disposal_date" not in lcols:
                conn.execute(
                    text("ALTER TABLE purchase_lines ADD COLUMN disposal_date DATETIME")
                )

        # جدول التكاليف الشهرية الثابتة (لتحليل التعادل CVP)
        insp = inspect(engine)
        names = set(insp.get_table_names())
        if "recurring_costs" not in names:
            conn.execute(
                text(
                    """
                    CREATE TABLE recurring_costs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name_ar VARCHAR(160) NOT NULL,
                        category VARCHAR(32) NOT NULL DEFAULT 'OTHER',
                        monthly_amount NUMERIC(14, 3) NOT NULL DEFAULT 0,
                        is_active BOOLEAN NOT NULL DEFAULT 1,
                        notes TEXT,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_recurring_costs_category ON recurring_costs (category)"
                )
            )
