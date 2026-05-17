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
        if "pos_shifts" not in names:
            conn.execute(
                text(
                    """
                    CREATE TABLE pos_shifts (
                        id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        status VARCHAR(20) NOT NULL DEFAULT 'OPEN',
                        opened_at DATETIME NOT NULL,
                        closed_at DATETIME,
                        opening_note TEXT,
                        closing_note TEXT,
                        counted_cash NUMERIC(14, 3),
                        expected_cash NUMERIC(14, 3),
                        cash_difference NUMERIC(14, 3),
                        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                    )
                    """
                )
            )
            conn.execute(
                text("CREATE INDEX IF NOT EXISTS ix_pos_shifts_user_id ON pos_shifts (user_id)")
            )
            conn.execute(
                text("CREATE INDEX IF NOT EXISTS ix_pos_shifts_status ON pos_shifts (status)")
            )

        insp = inspect(engine)
        names = set(insp.get_table_names())
        if "customers" not in names:
            conn.execute(
                text(
                    """
                CREATE TABLE customers (
                    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                    phone VARCHAR(40) NOT NULL,
                    name VARCHAR(160),
                    email VARCHAR(160),
                    notes TEXT,
                    points_balance NUMERIC(14, 3) NOT NULL DEFAULT 0,
                    total_spent NUMERIC(14, 3) NOT NULL DEFAULT 0,
                    visits_count INTEGER NOT NULL DEFAULT 0,
                    last_visit_at DATETIME,
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_customers_phone UNIQUE (phone)
                )
                """
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_customers_phone ON customers (phone)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_customers_is_active ON customers (is_active)"
                )
            )

        insp = inspect(engine)
        names = set(insp.get_table_names())
        if "loyalty_transactions" not in names:
            conn.execute(
                text(
                    """
                CREATE TABLE loyalty_transactions (
                    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                    customer_id INTEGER NOT NULL,
                    sale_id INTEGER,
                    kind VARCHAR(20) NOT NULL DEFAULT 'EARN',
                    points NUMERIC(14, 3) NOT NULL DEFAULT 0,
                    note VARCHAR(255),
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    created_by_id INTEGER,
                    FOREIGN KEY(customer_id) REFERENCES customers(id) ON DELETE CASCADE,
                    FOREIGN KEY(sale_id) REFERENCES sales(id) ON DELETE SET NULL,
                    FOREIGN KEY(created_by_id) REFERENCES users(id) ON DELETE SET NULL
                )
                """
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_loyalty_transactions_customer_id "
                    "ON loyalty_transactions (customer_id)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_loyalty_transactions_sale_id "
                    "ON loyalty_transactions (sale_id)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_loyalty_transactions_kind "
                    "ON loyalty_transactions (kind)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_loyalty_transactions_created_at "
                    "ON loyalty_transactions (created_at)"
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
            if "pos_shift_id" not in scols:
                conn.execute(
                    text(
                        "ALTER TABLE sales ADD COLUMN pos_shift_id INTEGER REFERENCES pos_shifts(id)"
                    )
                )
            if "sent_to_kitchen_at" not in scols:
                conn.execute(text("ALTER TABLE sales ADD COLUMN sent_to_kitchen_at DATETIME"))

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
            if "supplier_invoice_ref" not in pcols:
                conn.execute(
                    text(
                        "ALTER TABLE purchases ADD COLUMN supplier_invoice_ref VARCHAR(120)"
                    )
                )
            if "invoice_image_filename" not in pcols:
                conn.execute(
                    text(
                        "ALTER TABLE purchases ADD COLUMN invoice_image_filename VARCHAR(255)"
                    )
                )
            if "payment_proof_image_filename" not in pcols:
                conn.execute(
                    text(
                        "ALTER TABLE purchases ADD COLUMN payment_proof_image_filename VARCHAR(255)"
                    )
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

        # سطور المرتجع: تمييز عودة المخزن (وجبات مطهاة = بدون عودة للمخزن)
        insp = inspect(engine)
        names = set(insp.get_table_names())
        if "sale_return_lines" in names:
            rl_cols = {c["name"] for c in insp.get_columns("sale_return_lines")}
            if "restock" not in rl_cols:
                conn.execute(
                    text(
                        "ALTER TABLE sale_return_lines ADD COLUMN restock BOOLEAN NOT NULL DEFAULT 1"
                    )
                )

        # مخازن متعددة: رئيسي + فروع
        insp = inspect(engine)
        names = set(insp.get_table_names())
        if "warehouses" not in names:
            conn.execute(
                text(
                    """
                    CREATE TABLE warehouses (
                        id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                        name_ar VARCHAR(120) NOT NULL UNIQUE,
                        is_main BOOLEAN NOT NULL DEFAULT 0,
                        is_active BOOLEAN NOT NULL DEFAULT 1,
                        sort_order INTEGER NOT NULL DEFAULT 0,
                        notes TEXT,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            )
            conn.execute(
                text(
                    "INSERT INTO warehouses (id, name_ar, is_main, is_active, sort_order, created_at) "
                    "VALUES (1, 'المخزن الرئيسي', 1, 1, 0, CURRENT_TIMESTAMP)"
                )
            )

        insp = inspect(engine)
        names = set(insp.get_table_names())
        if "warehouses" in names:
            wh_count = conn.execute(text("SELECT COUNT(*) FROM warehouses")).scalar_one()
            if int(wh_count or 0) == 0:
                conn.execute(
                    text(
                        "INSERT INTO warehouses (id, name_ar, is_main, is_active, sort_order, created_at) "
                        "VALUES (1, 'المخزن الرئيسي', 1, 1, 0, CURRENT_TIMESTAMP)"
                    )
                )

        insp = inspect(engine)
        names = set(insp.get_table_names())
        if "stock_balances_new" in names and "stock_balances" in names:
            sb_cols = {c["name"] for c in insp.get_columns("stock_balances")}
            if "warehouse_id" not in sb_cols:
                conn.execute(text("DROP TABLE stock_balances"))
                conn.execute(text("ALTER TABLE stock_balances_new RENAME TO stock_balances"))
        elif "stock_balances" in names:
            sb_cols = {c["name"] for c in insp.get_columns("stock_balances")}
            if "warehouse_id" not in sb_cols:
                conn.execute(
                    text(
                        """
                        CREATE TABLE IF NOT EXISTS stock_balances_new (
                            warehouse_id INTEGER NOT NULL DEFAULT 1
                                REFERENCES warehouses(id) ON DELETE CASCADE,
                            product_id INTEGER NOT NULL
                                REFERENCES products(id) ON DELETE CASCADE,
                            quantity NUMERIC(14, 4) NOT NULL DEFAULT 0,
                            PRIMARY KEY (warehouse_id, product_id)
                        )
                        """
                    )
                )
                new_count = conn.execute(
                    text("SELECT COUNT(*) FROM stock_balances_new")
                ).scalar_one()
                if int(new_count or 0) == 0:
                    conn.execute(
                        text(
                            "INSERT INTO stock_balances_new (warehouse_id, product_id, quantity) "
                            "SELECT 1, product_id, quantity FROM stock_balances"
                        )
                    )
                conn.execute(text("DROP TABLE stock_balances"))
                conn.execute(text("ALTER TABLE stock_balances_new RENAME TO stock_balances"))

        insp = inspect(engine)
        if "stock_movements" in names:
            sm_cols = {c["name"] for c in insp.get_columns("stock_movements")}
            if "warehouse_id" not in sm_cols:
                conn.execute(
                    text(
                        "ALTER TABLE stock_movements ADD COLUMN warehouse_id INTEGER "
                        "REFERENCES warehouses(id)"
                    )
                )
                conn.execute(text("UPDATE stock_movements SET warehouse_id = 1 WHERE warehouse_id IS NULL"))
            if "counterparty_warehouse_id" not in sm_cols:
                conn.execute(
                    text(
                        "ALTER TABLE stock_movements ADD COLUMN counterparty_warehouse_id INTEGER "
                        "REFERENCES warehouses(id)"
                    )
                )

        insp = inspect(engine)
        if "purchases" in names:
            pcols = {c["name"] for c in insp.get_columns("purchases")}
            if "warehouse_id" not in pcols:
                conn.execute(
                    text(
                        "ALTER TABLE purchases ADD COLUMN warehouse_id INTEGER "
                        "REFERENCES warehouses(id)"
                    )
                )
                conn.execute(
                    text(
                        "UPDATE purchases SET warehouse_id = 1 "
                        "WHERE kind = 'INVENTORY' AND warehouse_id IS NULL"
                    )
                )

        insp = inspect(engine)
        names = set(insp.get_table_names())
        if "pos_shifts" in names:
            ps_cols = {c["name"] for c in insp.get_columns("pos_shifts")}
            if "counted_bank" not in ps_cols:
                conn.execute(
                    text("ALTER TABLE pos_shifts ADD COLUMN counted_bank NUMERIC(14, 3)")
                )
            if "expected_bank" not in ps_cols:
                conn.execute(
                    text("ALTER TABLE pos_shifts ADD COLUMN expected_bank NUMERIC(14, 3)")
                )
            if "bank_difference" not in ps_cols:
                conn.execute(
                    text("ALTER TABLE pos_shifts ADD COLUMN bank_difference NUMERIC(14, 3)")
                )

        insp = inspect(engine)
        names = set(insp.get_table_names())
        if "kitchen_departments" not in names:
            conn.execute(
                text(
                    """
                    CREATE TABLE kitchen_departments (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name_ar VARCHAR(120) NOT NULL,
                        venue VARCHAR(20) NOT NULL DEFAULT 'KITCHEN',
                        sort_order INTEGER NOT NULL DEFAULT 0,
                        is_active BOOLEAN NOT NULL DEFAULT 1,
                        routing_mode VARCHAR(20) NOT NULL DEFAULT 'SCREEN',
                        routing_target VARCHAR(255)
                    )
                    """
                )
            )

        if "products" in names:
            pcols = {c["name"] for c in insp.get_columns("products")}
            if "kitchen_department_id" not in pcols:
                conn.execute(
                    text(
                        "ALTER TABLE products ADD COLUMN kitchen_department_id INTEGER "
                        "REFERENCES kitchen_departments(id)"
                    )
                )

        if "kitchen_tickets" in names:
            kt_cols = {c["name"] for c in insp.get_columns("kitchen_tickets")}
            if "kitchen_department_id" not in kt_cols:
                conn.execute(
                    text(
                        "ALTER TABLE kitchen_tickets ADD COLUMN kitchen_department_id INTEGER "
                        "REFERENCES kitchen_departments(id)"
                    )
                )
            info = conn.execute(text("PRAGMA table_info(kitchen_tickets)")).fetchall()
            root_col = next((c for c in info if c[1] == "root_category_id"), None)
            if root_col is not None and root_col[3] == 1:
                pending = conn.execute(
                    text(
                        "SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name='kitchen_tickets_new'"
                    )
                ).fetchone()
                if pending is None:
                    conn.execute(
                        text(
                            """
                            CREATE TABLE kitchen_tickets_new (
                                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                                sale_id INTEGER NOT NULL,
                                root_category_id INTEGER,
                                kitchen_department_id INTEGER,
                                status VARCHAR(20) NOT NULL DEFAULT 'PENDING',
                                routing_mode VARCHAR(20) NOT NULL DEFAULT 'SCREEN',
                                delivery_status VARCHAR(40) NOT NULL DEFAULT 'PENDING',
                                delivery_info VARCHAR(500),
                                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                                served_at DATETIME,
                                FOREIGN KEY(sale_id) REFERENCES sales(id) ON DELETE CASCADE,
                                FOREIGN KEY(root_category_id) REFERENCES product_categories(id) ON DELETE CASCADE,
                                FOREIGN KEY(kitchen_department_id) REFERENCES kitchen_departments(id) ON DELETE CASCADE
                            )
                            """
                        )
                    )
                    conn.execute(
                        text(
                            """
                            INSERT INTO kitchen_tickets_new (
                                id, sale_id, root_category_id, kitchen_department_id,
                                status, routing_mode, delivery_status, delivery_info,
                                created_at, served_at
                            )
                            SELECT
                                id, sale_id, root_category_id, kitchen_department_id,
                                status, routing_mode, delivery_status, delivery_info,
                                created_at, served_at
                            FROM kitchen_tickets
                            """
                        )
                    )
                    conn.execute(text("DROP TABLE kitchen_tickets"))
                    conn.execute(
                        text("ALTER TABLE kitchen_tickets_new RENAME TO kitchen_tickets")
                    )
                    conn.execute(
                        text(
                            "CREATE INDEX IF NOT EXISTS ix_kitchen_tickets_sale_id "
                            "ON kitchen_tickets (sale_id)"
                        )
                    )
                    conn.execute(
                        text(
                            "CREATE INDEX IF NOT EXISTS ix_kitchen_tickets_root_category_id "
                            "ON kitchen_tickets (root_category_id)"
                        )
                    )
                    conn.execute(
                        text(
                            "CREATE INDEX IF NOT EXISTS ix_kitchen_tickets_kitchen_department_id "
                            "ON kitchen_tickets (kitchen_department_id)"
                        )
                    )
                    conn.execute(
                        text(
                            "CREATE INDEX IF NOT EXISTS ix_kitchen_tickets_status "
                            "ON kitchen_tickets (status)"
                        )
                    )

        insp = inspect(engine)
        names = set(insp.get_table_names())
        if "purchase_payments" not in names:
            conn.execute(
                text(
                    """
                    CREATE TABLE purchase_payments (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        purchase_id INTEGER NOT NULL,
                        payment_method_id INTEGER NOT NULL,
                        amount NUMERIC(14, 3) NOT NULL DEFAULT 0,
                        note TEXT,
                        payment_proof_image_filename VARCHAR(255),
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        created_by_id INTEGER,
                        FOREIGN KEY (purchase_id) REFERENCES purchases(id) ON DELETE CASCADE,
                        FOREIGN KEY (payment_method_id) REFERENCES payment_methods(id),
                        FOREIGN KEY (created_by_id) REFERENCES users(id)
                    )
                    """
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_purchase_payments_purchase_id "
                    "ON purchase_payments (purchase_id)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_purchase_payments_created_at "
                    "ON purchase_payments (created_at)"
                )
            )
            if "purchases" in names:
                conn.execute(
                    text(
                        """
                        INSERT INTO purchase_payments (
                            purchase_id, payment_method_id, amount, created_at, created_by_id
                        )
                        SELECT p.id, p.payment_method_id, p.amount, p.created_at, p.created_by_id
                        FROM purchases p
                        JOIN payment_methods pm ON pm.id = p.payment_method_id
                        WHERE pm.name_ar != 'ذمم دائن — مورد (آجل)'
                        AND NOT EXISTS (
                            SELECT 1 FROM purchase_payments pp WHERE pp.purchase_id = p.id
                        )
                        """
                    )
                )

        insp = inspect(engine)
        names = set(insp.get_table_names())
        if "payment_methods" in names:
            pm_cols = {c["name"] for c in insp.get_columns("payment_methods")}
            for col, default in (
                ("can_receive", "1"),
                ("can_pay", "1"),
                ("can_fund", "0"),
                ("is_system", "0"),
                ("show_on_dashboard", "1"),
            ):
                if col not in pm_cols:
                    conn.execute(
                        text(
                            f"ALTER TABLE payment_methods ADD COLUMN {col} BOOLEAN "
                            f"NOT NULL DEFAULT {default}"
                        )
                    )
            conn.execute(
                text(
                    "UPDATE payment_methods SET can_receive=0, can_pay=0, can_fund=0, is_system=1 "
                    "WHERE name_ar = 'ذمم دائن — مورد (آجل)'"
                )
            )
            # دمج حسابي المالك القديمين في حساب واحد (يُكمل عند تشغيل التطبيق)
            conn.execute(
                text(
                    "INSERT INTO payment_methods "
                    "(name_ar, kind, is_active, sort_order, can_receive, can_pay, can_fund, is_system) "
                    "SELECT 'حساب المالك — حقوق الملكية', 'OTHER', 1, 900, 0, 1, 1, 1 "
                    "WHERE NOT EXISTS ("
                    "SELECT 1 FROM payment_methods "
                    "WHERE name_ar = 'حساب المالك — حقوق الملكية'"
                    ")"
                )
            )
            conn.execute(
                text(
                    "UPDATE payment_methods SET can_fund=1 "
                    "WHERE is_system=0 AND kind IN ('CASH', 'BANK') "
                    "AND (can_fund=0 OR can_fund IS NULL)"
                )
            )
            pm_cols2 = {c["name"] for c in insp.get_columns("payment_methods")}
            if "show_on_dashboard" in pm_cols2:
                conn.execute(
                    text(
                        "UPDATE payment_methods SET show_on_dashboard=0 "
                        "WHERE is_system=1 OR kind='OTHER'"
                    )
                )

        if "sale_payments" in names:
            sp_cols = {c["name"] for c in insp.get_columns("sale_payments")}
            if "payment_proof_image_filename" not in sp_cols:
                conn.execute(
                    text(
                        "ALTER TABLE sale_payments ADD COLUMN "
                        "payment_proof_image_filename VARCHAR(255)"
                    )
                )

        if "payment_transfers" in names:
            pt_cols = {c["name"] for c in insp.get_columns("payment_transfers")}
            if "transfer_type" not in pt_cols:
                conn.execute(
                    text(
                        "ALTER TABLE payment_transfers ADD COLUMN transfer_type VARCHAR(32) "
                        "NOT NULL DEFAULT 'REFUND_SETTLEMENT'"
                    )
                )
                conn.execute(
                    text(
                        "UPDATE payment_transfers SET transfer_type='REFUND_SETTLEMENT' "
                        "WHERE sale_return_id IS NOT NULL"
                    )
                )
            info = conn.execute(text("PRAGMA table_info(payment_transfers)")).fetchall()
            sale_nullable = any(
                r[1] == "sale_return_id" and r[3] == 0 for r in info
            )
            if not sale_nullable:
                conn.execute(
                    text(
                        """
                        CREATE TABLE payment_transfers_new (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            transfer_type VARCHAR(32) NOT NULL DEFAULT 'MANUAL',
                            sale_return_id INTEGER,
                            from_payment_method_id INTEGER NOT NULL,
                            to_payment_method_id INTEGER NOT NULL,
                            amount NUMERIC(14, 3) NOT NULL DEFAULT 0,
                            note TEXT,
                            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            created_by_id INTEGER,
                            FOREIGN KEY (sale_return_id) REFERENCES sale_returns(id) ON DELETE CASCADE,
                            FOREIGN KEY (from_payment_method_id) REFERENCES payment_methods(id),
                            FOREIGN KEY (to_payment_method_id) REFERENCES payment_methods(id),
                            FOREIGN KEY (created_by_id) REFERENCES users(id)
                        )
                        """
                    )
                )
                conn.execute(
                    text(
                        """
                        INSERT INTO payment_transfers_new (
                            id, transfer_type, sale_return_id, from_payment_method_id,
                            to_payment_method_id, amount, note, created_at, created_by_id
                        )
                        SELECT
                            id, COALESCE(transfer_type, 'REFUND_SETTLEMENT'), sale_return_id,
                            from_payment_method_id, to_payment_method_id, amount, note,
                            created_at, created_by_id
                        FROM payment_transfers
                        """
                    )
                )
                conn.execute(text("DROP TABLE payment_transfers"))
                conn.execute(
                    text("ALTER TABLE payment_transfers_new RENAME TO payment_transfers")
                )
