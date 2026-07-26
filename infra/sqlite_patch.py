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
                    show_in_shop BOOLEAN NOT NULL DEFAULT 1,
                    FOREIGN KEY(parent_id) REFERENCES product_categories (id) ON DELETE CASCADE
                )
                """
                )
            )

        insp = inspect(engine)
        names = set(insp.get_table_names())
        if "product_categories" in names:
            cat_cols = {c["name"] for c in insp.get_columns("product_categories")}
            if "color_hex" not in cat_cols:
                conn.execute(
                    text(
                        "ALTER TABLE product_categories ADD COLUMN color_hex VARCHAR(7) NOT NULL DEFAULT '#3b82f6'"
                    )
                )
            if "delete_protected" not in cat_cols:
                conn.execute(
                    text(
                        "ALTER TABLE product_categories ADD COLUMN delete_protected BOOLEAN NOT NULL DEFAULT 0"
                    )
                )
            if "show_in_shop" not in cat_cols:
                conn.execute(
                    text(
                        "ALTER TABLE product_categories ADD COLUMN show_in_shop BOOLEAN NOT NULL DEFAULT 1"
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
        if "customers" in names:
            ccols = {c["name"] for c in insp.get_columns("customers")}
            if "points_balance" not in ccols:
                conn.execute(
                    text(
                        "ALTER TABLE customers ADD COLUMN points_balance "
                        "NUMERIC(14, 3) NOT NULL DEFAULT 0"
                    )
                )
            if "total_spent" not in ccols:
                conn.execute(
                    text(
                        "ALTER TABLE customers ADD COLUMN total_spent "
                        "NUMERIC(14, 3) NOT NULL DEFAULT 0"
                    )
                )
            if "visits_count" not in ccols:
                conn.execute(
                    text(
                        "ALTER TABLE customers ADD COLUMN visits_count "
                        "INTEGER NOT NULL DEFAULT 0"
                    )
                )
            if "last_visit_at" not in ccols:
                conn.execute(
                    text("ALTER TABLE customers ADD COLUMN last_visit_at DATETIME")
                )
            if "is_active" not in ccols:
                conn.execute(
                    text(
                        "ALTER TABLE customers ADD COLUMN is_active "
                        "BOOLEAN NOT NULL DEFAULT 1"
                    )
                )
            if "updated_at" not in ccols:
                conn.execute(
                    text(
                        "ALTER TABLE customers ADD COLUMN updated_at DATETIME "
                        "DEFAULT CURRENT_TIMESTAMP"
                    )
                )

        insp = inspect(engine)
        names = set(insp.get_table_names())
        if "loyalty_transactions" in names:
            ltcols = {c["name"] for c in insp.get_columns("loyalty_transactions")}
            if "created_by_id" not in ltcols:
                conn.execute(
                    text(
                        "ALTER TABLE loyalty_transactions ADD COLUMN created_by_id "
                        "INTEGER REFERENCES users(id) ON DELETE SET NULL"
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
            if "served_to_customer_at" not in scols:
                conn.execute(text("ALTER TABLE sales ADD COLUMN served_to_customer_at DATETIME"))
            if "referral_code_used" not in scols:
                conn.execute(text("ALTER TABLE sales ADD COLUMN referral_code_used VARCHAR(32)"))
            if "referrer_customer_id" not in scols:
                conn.execute(
                    text(
                        "ALTER TABLE sales ADD COLUMN referrer_customer_id "
                        "INTEGER REFERENCES customers(id)"
                    )
                )

        insp = inspect(engine)
        names = set(insp.get_table_names())
        if "customers" in names:
            ccols = {c["name"] for c in insp.get_columns("customers")}
            if "referral_code" not in ccols:
                conn.execute(text("ALTER TABLE customers ADD COLUMN referral_code VARCHAR(32)"))
        if "delivery_drivers" not in names:
            conn.execute(
                text(
                    """
                CREATE TABLE delivery_drivers (
                    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                    name VARCHAR(120) NOT NULL,
                    phone VARCHAR(40) NOT NULL UNIQUE,
                    use_count INTEGER NOT NULL DEFAULT 0,
                    last_used_at DATETIME,
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
                )
            )
        if "delivery_handoffs" not in names:
            conn.execute(
                text(
                    """
                CREATE TABLE delivery_handoffs (
                    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                    sale_id INTEGER NOT NULL UNIQUE REFERENCES sales(id) ON DELETE CASCADE,
                    driver_id INTEGER REFERENCES delivery_drivers(id) ON DELETE SET NULL,
                    driver_name VARCHAR(120) NOT NULL,
                    driver_phone VARCHAR(40) NOT NULL,
                    handed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    created_by_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    note TEXT
                )
                """
                )
            )
        if "referral_events" not in names:
            conn.execute(
                text(
                    """
                CREATE TABLE referral_events (
                    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                    referrer_customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
                    buyer_customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
                    sale_id INTEGER NOT NULL UNIQUE REFERENCES sales(id) ON DELETE CASCADE,
                    referral_code VARCHAR(32) NOT NULL,
                    referrer_points NUMERIC(14, 3) NOT NULL DEFAULT 0,
                    buyer_points NUMERIC(14, 3) NOT NULL DEFAULT 0,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
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
            if "supplier_phone" not in pcols:
                conn.execute(
                    text(
                        "ALTER TABLE purchases ADD COLUMN supplier_phone VARCHAR(40)"
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
        if "recurring_costs" in names:
            rc_cols = {c["name"] for c in insp.get_columns("recurring_costs")}
            if "business_domain" not in rc_cols:
                conn.execute(
                    text(
                        "ALTER TABLE recurring_costs ADD COLUMN business_domain "
                        "VARCHAR(20) NOT NULL DEFAULT 'restaurant'"
                    )
                )
                conn.execute(
                    text(
                        "UPDATE recurring_costs SET business_domain = 'restaurant' "
                        "WHERE business_domain IS NULL OR business_domain = ''"
                    )
                )
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_recurring_costs_business_domain "
                        "ON recurring_costs (business_domain)"
                    )
                )

        insp = inspect(engine)
        names = set(insp.get_table_names())
        if "hr_employees" in names:
            he_cols = {c["name"] for c in insp.get_columns("hr_employees")}
            if "business_domain" not in he_cols:
                conn.execute(
                    text(
                        "ALTER TABLE hr_employees ADD COLUMN business_domain "
                        "VARCHAR(20) NOT NULL DEFAULT 'restaurant'"
                    )
                )
                conn.execute(
                    text(
                        "UPDATE hr_employees SET business_domain = 'restaurant' "
                        "WHERE business_domain IS NULL OR business_domain = ''"
                    )
                )
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_hr_employees_business_domain "
                        "ON hr_employees (business_domain)"
                    )
                )

        insp = inspect(engine)
        names = set(insp.get_table_names())
        if "hr_payroll_runs" in names:
            pr_cols = {c["name"] for c in insp.get_columns("hr_payroll_runs")}
            if "business_domain" not in pr_cols:
                conn.execute(
                    text(
                        "ALTER TABLE hr_payroll_runs ADD COLUMN business_domain "
                        "VARCHAR(20) NOT NULL DEFAULT 'restaurant'"
                    )
                )
                conn.execute(
                    text(
                        "UPDATE hr_payroll_runs SET business_domain = 'restaurant' "
                        "WHERE business_domain IS NULL OR business_domain = ''"
                    )
                )
            conn.execute(text("DROP INDEX IF EXISTS uq_payroll_period"))
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_payroll_period_domain "
                    "ON hr_payroll_runs (period_year, period_month, business_domain)"
                )
            )

        insp = inspect(engine)
        names = set(insp.get_table_names())
        if "purchases" in names:
            pcols = {c["name"] for c in insp.get_columns("purchases")}
            if "business_domain" not in pcols:
                conn.execute(
                    text(
                        "ALTER TABLE purchases ADD COLUMN business_domain "
                        "VARCHAR(20) NOT NULL DEFAULT 'restaurant'"
                    )
                )
                conn.execute(
                    text(
                        "UPDATE purchases SET business_domain = ("
                        "SELECT business_domain FROM payment_methods "
                        "WHERE payment_methods.id = purchases.payment_method_id"
                        ") WHERE business_domain IS NULL OR business_domain = ''"
                    )
                )
                conn.execute(
                    text(
                        "UPDATE purchases SET business_domain = 'restaurant' "
                        "WHERE business_domain IS NULL OR business_domain = ''"
                    )
                )
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_purchases_business_domain "
                        "ON purchases (business_domain)"
                    )
                )

        insp = inspect(engine)
        names = set(insp.get_table_names())
        if "gl_accounts" in names:
            ga_cols = {c["name"] for c in insp.get_columns("gl_accounts")}
            if "business_domain" not in ga_cols:
                conn.execute(
                    text(
                        "ALTER TABLE gl_accounts ADD COLUMN business_domain "
                        "VARCHAR(20) NOT NULL DEFAULT 'shared'"
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
                        "UPDATE gl_accounts SET business_domain = 'shared' "
                        "WHERE code IN ('1000','1100','2000','3000','4000','5000','3100')"
                    )
                )
                conn.execute(
                    text(
                        "UPDATE gl_accounts SET business_domain = 'restaurant' "
                        "WHERE business_domain = 'shared' "
                        "AND code NOT IN ('1000','1100','2000','3000','4000','5000','3100','1115','1125')"
                    )
                )

        insp = inspect(engine)
        names = set(insp.get_table_names())
        if "gl_journal_entries" in names:
            je_cols = {c["name"] for c in insp.get_columns("gl_journal_entries")}
            if "business_domain" not in je_cols:
                conn.execute(
                    text(
                        "ALTER TABLE gl_journal_entries ADD COLUMN business_domain "
                        "VARCHAR(20) NOT NULL DEFAULT 'restaurant'"
                    )
                )
                conn.execute(
                    text(
                        "UPDATE gl_journal_entries SET business_domain = 'hotel' "
                        "WHERE source_type LIKE 'hotel_%'"
                    )
                )
                conn.execute(
                    text(
                        "UPDATE gl_journal_entries SET business_domain = 'restaurant' "
                        "WHERE source_type LIKE 'sale%' OR source_type = 'refund_payment'"
                    )
                )
                conn.execute(
                    text(
                        "UPDATE gl_journal_entries SET business_domain = 'shared' "
                        "WHERE source_type = 'payment_transfer'"
                    )
                )
                if "purchases" in names:
                    conn.execute(
                        text(
                            "UPDATE gl_journal_entries SET business_domain = ("
                            "SELECT business_domain FROM purchases "
                            "WHERE purchases.id = gl_journal_entries.source_id"
                            ") WHERE source_type IN ("
                            "'purchase_expense','purchase_inventory','purchase_asset','salary_advance'"
                            ")"
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
            if "purchase_id" not in sm_cols:
                conn.execute(
                    text(
                        "ALTER TABLE stock_movements ADD COLUMN purchase_id INTEGER "
                        "REFERENCES purchases(id)"
                    )
                )
            if "reason_label" not in sm_cols:
                conn.execute(
                    text("ALTER TABLE stock_movements ADD COLUMN reason_label VARCHAR(120)")
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
            if "pos_shift_id" not in pcols:
                conn.execute(
                    text(
                        "ALTER TABLE purchases ADD COLUMN pos_shift_id INTEGER "
                        "REFERENCES pos_shifts(id) ON DELETE SET NULL"
                    )
                )
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_purchases_pos_shift_id "
                        "ON purchases (pos_shift_id)"
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
            if "counted_room" not in ps_cols:
                conn.execute(
                    text("ALTER TABLE pos_shifts ADD COLUMN counted_room NUMERIC(14, 3)")
                )
            if "expected_room" not in ps_cols:
                conn.execute(
                    text("ALTER TABLE pos_shifts ADD COLUMN expected_room NUMERIC(14, 3)")
                )
            if "room_difference" not in ps_cols:
                conn.execute(
                    text("ALTER TABLE pos_shifts ADD COLUMN room_difference NUMERIC(14, 3)")
                )
            if "employee_id" not in ps_cols:
                conn.execute(
                    text(
                        "ALTER TABLE pos_shifts ADD COLUMN employee_id INTEGER "
                        "REFERENCES hr_employees(id)"
                    )
                )
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_pos_shifts_employee_id "
                        "ON pos_shifts (employee_id)"
                    )
                )
            if "treasury_handoff_at" not in ps_cols:
                conn.execute(
                    text("ALTER TABLE pos_shifts ADD COLUMN treasury_handoff_at DATETIME")
                )
            if "treasury_handoff_by_id" not in ps_cols:
                conn.execute(
                    text(
                        "ALTER TABLE pos_shifts ADD COLUMN treasury_handoff_by_id INTEGER "
                        "REFERENCES users(id)"
                    )
                )
            if "opening_cash" not in ps_cols:
                conn.execute(
                    text(
                        "ALTER TABLE pos_shifts ADD COLUMN opening_cash NUMERIC(14, 3) "
                        "NOT NULL DEFAULT 0"
                    )
                )

        insp = inspect(engine)
        names = set(insp.get_table_names())
        if "pos_shift_shortages" not in names:
            conn.execute(
                text(
                    """
                    CREATE TABLE pos_shift_shortages (
                        id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                        shift_id INTEGER NOT NULL,
                        kind VARCHAR(10) NOT NULL,
                        expected_amount NUMERIC(14, 3),
                        counted_amount NUMERIC(14, 3),
                        difference NUMERIC(14, 3),
                        shortage_amount NUMERIC(14, 3) NOT NULL,
                        closed_at DATETIME,
                        user_id INTEGER NOT NULL,
                        employee_id INTEGER,
                        closing_note TEXT,
                        created_at DATETIME NOT NULL,
                        FOREIGN KEY(shift_id) REFERENCES pos_shifts(id) ON DELETE CASCADE,
                        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                        FOREIGN KEY(employee_id) REFERENCES hr_employees(id) ON DELETE SET NULL,
                        UNIQUE(shift_id, kind)
                    )
                    """
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_pos_shift_shortages_closed_at "
                    "ON pos_shift_shortages (closed_at)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_pos_shift_shortages_kind "
                    "ON pos_shift_shortages (kind)"
                )
            )
        else:
            # ترقيات لاحقة لسجل العجز (حل/خصم/عفو)
            sh_cols = {c["name"] for c in insp.get_columns("pos_shift_shortages")}
            if "original_shortage_amount" not in sh_cols:
                conn.execute(
                    text(
                        "ALTER TABLE pos_shift_shortages ADD COLUMN original_shortage_amount NUMERIC(14, 3)"
                    )
                )
            if "resolved_action" not in sh_cols:
                conn.execute(
                    text(
                        "ALTER TABLE pos_shift_shortages ADD COLUMN resolved_action VARCHAR(20)"
                    )
                )
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_pos_shift_shortages_resolved_action "
                        "ON pos_shift_shortages (resolved_action)"
                    )
                )
            if "resolved_note" not in sh_cols:
                conn.execute(
                    text("ALTER TABLE pos_shift_shortages ADD COLUMN resolved_note TEXT")
                )
            if "resolved_at" not in sh_cols:
                conn.execute(
                    text("ALTER TABLE pos_shift_shortages ADD COLUMN resolved_at DATETIME")
                )
            if "resolved_by_id" not in sh_cols:
                conn.execute(
                    text(
                        "ALTER TABLE pos_shift_shortages ADD COLUMN resolved_by_id INTEGER REFERENCES users(id)"
                    )
                )
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_pos_shift_shortages_resolved_by_id "
                        "ON pos_shift_shortages (resolved_by_id)"
                    )
                )
            if "payroll_deduction_id" not in sh_cols:
                conn.execute(
                    text(
                        "ALTER TABLE pos_shift_shortages ADD COLUMN payroll_deduction_id INTEGER REFERENCES hr_employee_deductions(id)"
                    )
                )
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_pos_shift_shortages_payroll_deduction_id "
                        "ON pos_shift_shortages (payroll_deduction_id)"
                    )
                )
            if "expense_purchase_id" not in sh_cols:
                conn.execute(
                    text(
                        "ALTER TABLE pos_shift_shortages ADD COLUMN expense_purchase_id INTEGER REFERENCES purchases(id)"
                    )
                )
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_pos_shift_shortages_expense_purchase_id "
                        "ON pos_shift_shortages (expense_purchase_id)"
                    )
                )

        if "hr_employees" in names:
            he_cols = {c["name"] for c in insp.get_columns("hr_employees")}
            if "is_pos_cashier" not in he_cols:
                conn.execute(
                    text(
                        "ALTER TABLE hr_employees ADD COLUMN is_pos_cashier BOOLEAN "
                        "NOT NULL DEFAULT 0"
                    )
                )
            if "pos_pin_hash" not in he_cols:
                conn.execute(
                    text("ALTER TABLE hr_employees ADD COLUMN pos_pin_hash VARCHAR(255)")
                )
            if "is_pos_supervisor" not in he_cols:
                conn.execute(
                    text(
                        "ALTER TABLE hr_employees ADD COLUMN is_pos_supervisor BOOLEAN "
                        "NOT NULL DEFAULT 0"
                    )
                )

        insp = inspect(engine)
        names = set(insp.get_table_names())
        if "hr_work_shifts" not in names:
            conn.execute(
                text(
                    """
                    CREATE TABLE hr_work_shifts (
                        id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                        name_ar VARCHAR(120) NOT NULL,
                        code VARCHAR(32) UNIQUE,
                        start_time VARCHAR(5) NOT NULL DEFAULT '08:00',
                        end_time VARCHAR(5) NOT NULL DEFAULT '16:00',
                        work_hours NUMERIC(4, 2) NOT NULL DEFAULT 8.00,
                        grace_minutes INTEGER NOT NULL DEFAULT 15,
                        is_active BOOLEAN NOT NULL DEFAULT 1,
                        notes TEXT,
                        created_at DATETIME NOT NULL
                    )
                    """
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_hr_work_shifts_name_ar "
                    "ON hr_work_shifts (name_ar)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_hr_work_shifts_is_active "
                    "ON hr_work_shifts (is_active)"
                )
            )

        insp = inspect(engine)
        names = set(insp.get_table_names())
        if "hr_employees" in names:
            he_cols = {c["name"] for c in insp.get_columns("hr_employees")}
            if "work_shift_id" not in he_cols:
                conn.execute(
                    text(
                        "ALTER TABLE hr_employees ADD COLUMN work_shift_id INTEGER "
                        "REFERENCES hr_work_shifts(id) ON DELETE SET NULL"
                    )
                )
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_hr_employees_work_shift_id "
                        "ON hr_employees (work_shift_id)"
                    )
                )
            if "zk_emp_code" not in he_cols:
                conn.execute(
                    text("ALTER TABLE hr_employees ADD COLUMN zk_emp_code VARCHAR(64)")
                )
                conn.execute(
                    text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS ix_hr_employees_zk_emp_code "
                        "ON hr_employees (zk_emp_code) WHERE zk_emp_code IS NOT NULL"
                    )
                )

        insp = inspect(engine)
        names = set(insp.get_table_names())
        if "hr_zk_processed_punches" not in names:
            conn.execute(
                text(
                    """
                    CREATE TABLE hr_zk_processed_punches (
                        zk_transaction_id INTEGER NOT NULL PRIMARY KEY,
                        employee_id INTEGER,
                        action VARCHAR(32) NOT NULL DEFAULT '',
                        detail VARCHAR(500),
                        processed_at DATETIME NOT NULL,
                        FOREIGN KEY(employee_id) REFERENCES hr_employees(id) ON DELETE SET NULL
                    )
                    """
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_hr_zk_processed_punches_employee_id "
                    "ON hr_zk_processed_punches (employee_id)"
                )
            )

        insp = inspect(engine)
        names = set(insp.get_table_names())
        if "hr_attendance" in names:
            att_cols = {c["name"] for c in insp.get_columns("hr_attendance")}
            if "work_shift_id" not in att_cols:
                conn.execute(
                    text(
                        "ALTER TABLE hr_attendance ADD COLUMN work_shift_id INTEGER "
                        "REFERENCES hr_work_shifts(id) ON DELETE SET NULL"
                    )
                )
            if "late_minutes" not in att_cols:
                conn.execute(
                    text(
                        "ALTER TABLE hr_attendance ADD COLUMN late_minutes INTEGER NOT NULL DEFAULT 0"
                    )
                )
            if "early_leave_minutes" not in att_cols:
                conn.execute(
                    text(
                        "ALTER TABLE hr_attendance ADD COLUMN early_leave_minutes INTEGER NOT NULL DEFAULT 0"
                    )
                )
            if "overtime_minutes" not in att_cols:
                conn.execute(
                    text(
                        "ALTER TABLE hr_attendance ADD COLUMN overtime_minutes INTEGER NOT NULL DEFAULT 0"
                    )
                )
            if "overtime_approval_status" not in att_cols:
                conn.execute(
                    text(
                        "ALTER TABLE hr_attendance ADD COLUMN overtime_approval_status "
                        "VARCHAR(16) NOT NULL DEFAULT 'NONE'"
                    )
                )
            if "approved_overtime_minutes" not in att_cols:
                conn.execute(
                    text(
                        "ALTER TABLE hr_attendance ADD COLUMN approved_overtime_minutes "
                        "INTEGER NOT NULL DEFAULT 0"
                    )
                )
            if "overtime_approved_by_id" not in att_cols:
                conn.execute(
                    text(
                        "ALTER TABLE hr_attendance ADD COLUMN overtime_approved_by_id INTEGER "
                        "REFERENCES users(id) ON DELETE SET NULL"
                    )
                )
            if "overtime_approved_at" not in att_cols:
                conn.execute(
                    text(
                        "ALTER TABLE hr_attendance ADD COLUMN overtime_approved_at DATETIME"
                    )
                )
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

        insp = inspect(engine)
        names = set(insp.get_table_names())
        if "hr_attendance" in names:
            att_cols = {c["name"] for c in insp.get_columns("hr_attendance")}
            if "expected_work_hours" not in att_cols:
                conn.execute(
                    text(
                        "ALTER TABLE hr_attendance ADD COLUMN expected_work_hours NUMERIC(4, 2)"
                    )
                )
        if "hr_work_shift_day_schedules" not in names:
            conn.execute(
                text(
                    """
                    CREATE TABLE hr_work_shift_day_schedules (
                        id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                        work_shift_id INTEGER NOT NULL,
                        day_of_week INTEGER NOT NULL,
                        is_rest_day BOOLEAN NOT NULL DEFAULT 0,
                        start_time VARCHAR(5),
                        end_time VARCHAR(5),
                        work_hours NUMERIC(4, 2),
                        grace_minutes INTEGER,
                        FOREIGN KEY(work_shift_id) REFERENCES hr_work_shifts(id) ON DELETE CASCADE,
                        UNIQUE (work_shift_id, day_of_week)
                    )
                    """
                )
            )
        if "hr_employee_day_schedules" not in names:
            conn.execute(
                text(
                    """
                    CREATE TABLE hr_employee_day_schedules (
                        id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                        employee_id INTEGER NOT NULL,
                        day_of_week INTEGER NOT NULL,
                        is_rest_day BOOLEAN NOT NULL DEFAULT 0,
                        start_time VARCHAR(5),
                        end_time VARCHAR(5),
                        work_hours NUMERIC(4, 2),
                        grace_minutes INTEGER,
                        FOREIGN KEY(employee_id) REFERENCES hr_employees(id) ON DELETE CASCADE,
                        UNIQUE (employee_id, day_of_week)
                    )
                    """
                )
            )

        insp = inspect(engine)
        names = set(insp.get_table_names())
        if "hr_departments" not in names:
            conn.execute(
                text(
                    """
                    CREATE TABLE hr_departments (
                        id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                        name_ar VARCHAR(120) NOT NULL,
                        code VARCHAR(32),
                        sort_order INTEGER NOT NULL DEFAULT 0,
                        is_active BOOLEAN NOT NULL DEFAULT 1,
                        notes TEXT,
                        created_at DATETIME NOT NULL
                    )
                    """
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_hr_departments_name_ar "
                    "ON hr_departments (name_ar)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_hr_departments_sort_order "
                    "ON hr_departments (sort_order)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_hr_departments_is_active "
                    "ON hr_departments (is_active)"
                )
            )
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ix_hr_departments_code "
                    "ON hr_departments (code) WHERE code IS NOT NULL"
                )
            )

        insp = inspect(engine)
        names = set(insp.get_table_names())
        if "hr_employees" in names:
            he_cols = {c["name"] for c in insp.get_columns("hr_employees")}
            if "department_id" not in he_cols:
                conn.execute(
                    text(
                        "ALTER TABLE hr_employees ADD COLUMN department_id INTEGER "
                        "REFERENCES hr_departments(id) ON DELETE SET NULL"
                    )
                )
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_hr_employees_department_id "
                        "ON hr_employees (department_id)"
                    )
                )

        # خصومات الموظفين (عجز/جزاءات) — تُسدد عبر الرواتب
        insp = inspect(engine)
        names = set(insp.get_table_names())
        if "hr_employee_deductions" not in names:
            conn.execute(
                text(
                    """
                    CREATE TABLE hr_employee_deductions (
                        id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                        employee_id INTEGER NOT NULL,
                        amount NUMERIC(14, 3) NOT NULL DEFAULT 0,
                        repaid_amount NUMERIC(14, 3) NOT NULL DEFAULT 0,
                        status VARCHAR(30) NOT NULL DEFAULT 'OUTSTANDING',
                        source_type VARCHAR(40),
                        source_id INTEGER,
                        note TEXT,
                        created_at DATETIME NOT NULL,
                        resolved_at DATETIME,
                        resolved_by_id INTEGER,
                        FOREIGN KEY(employee_id) REFERENCES hr_employees(id) ON DELETE RESTRICT,
                        FOREIGN KEY(resolved_by_id) REFERENCES users(id) ON DELETE SET NULL
                    )
                    """
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_hr_employee_deductions_employee_id "
                    "ON hr_employee_deductions (employee_id)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_hr_employee_deductions_status "
                    "ON hr_employee_deductions (status)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_hr_employee_deductions_source "
                    "ON hr_employee_deductions (source_type, source_id)"
                )
            )

        insp = inspect(engine)
        names = set(insp.get_table_names())
        if "hr_deduction_repayments" not in names:
            conn.execute(
                text(
                    """
                    CREATE TABLE hr_deduction_repayments (
                        id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                        deduction_id INTEGER NOT NULL,
                        amount NUMERIC(14, 3) NOT NULL DEFAULT 0,
                        repaid_at DATETIME NOT NULL,
                        payroll_entry_id INTEGER,
                        note TEXT,
                        FOREIGN KEY(deduction_id) REFERENCES hr_employee_deductions(id) ON DELETE CASCADE,
                        FOREIGN KEY(payroll_entry_id) REFERENCES hr_payroll_entries(id) ON DELETE SET NULL
                    )
                    """
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_hr_deduction_repayments_deduction_id "
                    "ON hr_deduction_repayments (deduction_id)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_hr_deduction_repayments_payroll_entry_id "
                    "ON hr_deduction_repayments (payroll_entry_id)"
                )
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
            pm_cols3 = {c["name"] for c in insp.get_columns("payment_methods")}
            if "icon_path" not in pm_cols3:
                conn.execute(
                    text(
                        "ALTER TABLE payment_methods ADD COLUMN icon_path VARCHAR(255)"
                    )
                )
            pm_cols4 = {c["name"] for c in insp.get_columns("payment_methods")}
            if "business_domain" not in pm_cols4:
                conn.execute(
                    text(
                        "ALTER TABLE payment_methods ADD COLUMN business_domain "
                        "VARCHAR(20) NOT NULL DEFAULT 'shared'"
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

        insp = inspect(engine)
        names = set(insp.get_table_names())

        if "print_agents" not in names:
            conn.execute(
                text(
                    """
                    CREATE TABLE print_agents (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name VARCHAR(120) NOT NULL,
                        branch_id INTEGER,
                        token_hash VARCHAR(128) NOT NULL UNIQUE,
                        last_seen_at DATETIME,
                        is_active BOOLEAN NOT NULL DEFAULT 1,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            )

        if "printers" not in names:
            conn.execute(
                text(
                    """
                    CREATE TABLE printers (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name VARCHAR(120) NOT NULL,
                        code VARCHAR(64) NOT NULL UNIQUE,
                        connection_type VARCHAR(32) NOT NULL DEFAULT 'local_agent',
                        agent_id INTEGER REFERENCES print_agents(id) ON DELETE SET NULL,
                        local_printer_key VARCHAR(64),
                        ip_address VARCHAR(64),
                        port INTEGER NOT NULL DEFAULT 9100,
                        paper_width INTEGER NOT NULL DEFAULT 80,
                        is_active BOOLEAN NOT NULL DEFAULT 1,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            )

        if "kitchen_sections" not in names:
            conn.execute(
                text(
                    """
                    CREATE TABLE kitchen_sections (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name VARCHAR(120) NOT NULL,
                        code VARCHAR(64) NOT NULL UNIQUE,
                        printer_id INTEGER REFERENCES printers(id) ON DELETE SET NULL,
                        is_active BOOLEAN NOT NULL DEFAULT 1,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            )
            if "kitchen_departments" in names:
                conn.execute(
                    text(
                        """
                        INSERT OR IGNORE INTO kitchen_sections (id, name, code, is_active)
                        SELECT id, name_ar, 'DEPT' || id, is_active FROM kitchen_departments
                        """
                    )
                )

        if "print_jobs" not in names:
            conn.execute(
                text(
                    """
                    CREATE TABLE print_jobs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        printer_id INTEGER NOT NULL REFERENCES printers(id) ON DELETE CASCADE,
                        kitchen_ticket_id INTEGER REFERENCES kitchen_tickets(id) ON DELETE SET NULL,
                        job_type VARCHAR(32) NOT NULL DEFAULT 'kitchen_ticket',
                        status VARCHAR(20) NOT NULL DEFAULT 'pending',
                        payload_format VARCHAR(20) NOT NULL DEFAULT 'text',
                        payload TEXT NOT NULL,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        claimed_by_agent_id INTEGER REFERENCES print_agents(id) ON DELETE SET NULL,
                        claimed_at DATETIME,
                        printed_at DATETIME,
                        error_message VARCHAR(500),
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_print_jobs_status ON print_jobs (status)"
                )
            )

        if "product_categories" in names:
            pcc = {c["name"] for c in insp.get_columns("product_categories")}
            if "kitchen_section_id" not in pcc:
                conn.execute(
                    text(
                        "ALTER TABLE product_categories ADD COLUMN kitchen_section_id INTEGER "
                        "REFERENCES kitchen_sections(id)"
                    )
                )
            if "show_in_pos" not in pcc:
                conn.execute(
                    text(
                        "ALTER TABLE product_categories ADD COLUMN show_in_pos "
                        "BOOLEAN NOT NULL DEFAULT 1"
                    )
                )
            if "show_in_shop" not in pcc:
                conn.execute(
                    text(
                        "ALTER TABLE product_categories ADD COLUMN show_in_shop "
                        "BOOLEAN NOT NULL DEFAULT 1"
                    )
                )

        if "products" in names:
            pcols = {c["name"] for c in insp.get_columns("products")}
            if "kitchen_section_id" not in pcols:
                conn.execute(
                    text(
                        "ALTER TABLE products ADD COLUMN kitchen_section_id INTEGER "
                        "REFERENCES kitchen_sections(id)"
                    )
                )
            conn.execute(
                text(
                    """
                    UPDATE products SET kitchen_section_id = kitchen_department_id
                    WHERE kitchen_section_id IS NULL AND kitchen_department_id IS NOT NULL
                    """
                )
            )
            if "line_modifier_presets" not in pcols:
                conn.execute(
                    text("ALTER TABLE products ADD COLUMN line_modifier_presets TEXT")
                )
            if "expiry_tracked" not in pcols:
                conn.execute(
                    text(
                        "ALTER TABLE products ADD COLUMN expiry_tracked "
                        "BOOLEAN NOT NULL DEFAULT 0"
                    )
                )
            if "expiry_production_date" not in pcols:
                conn.execute(
                    text("ALTER TABLE products ADD COLUMN expiry_production_date DATE")
                )
            if "expiry_date" not in pcols:
                conn.execute(
                    text("ALTER TABLE products ADD COLUMN expiry_date DATE")
                )
            if "expiry_warn_days" not in pcols:
                conn.execute(
                    text(
                        "ALTER TABLE products ADD COLUMN expiry_warn_days "
                        "INTEGER NOT NULL DEFAULT 7"
                    )
                )
            if "price_linked_to_bom" not in pcols:
                conn.execute(
                    text(
                        "ALTER TABLE products ADD COLUMN price_linked_to_bom "
                        "BOOLEAN NOT NULL DEFAULT 0"
                    )
                )
            if "bom_markup_pct" not in pcols:
                conn.execute(
                    text("ALTER TABLE products ADD COLUMN bom_markup_pct NUMERIC(8, 2)")
                )
            if "reference_unit_cost" not in pcols:
                conn.execute(
                    text("ALTER TABLE products ADD COLUMN reference_unit_cost NUMERIC(14, 3)")
                )
            if "direct_purchase_enabled" not in pcols:
                conn.execute(
                    text(
                        "ALTER TABLE products ADD COLUMN direct_purchase_enabled "
                        "BOOLEAN NOT NULL DEFAULT 0"
                    )
                )
                conn.execute(
                    text(
                        """
                        UPDATE products
                        SET direct_purchase_enabled = 1
                        WHERE kind = 'FINAL_SELLABLE'
                          AND NOT EXISTS (
                            SELECT 1 FROM bom_lines
                            WHERE bom_lines.parent_product_id = products.id
                          )
                        """
                    )
                )
            if "show_in_pos" not in pcols:
                conn.execute(
                    text(
                        "ALTER TABLE products ADD COLUMN show_in_pos "
                        "BOOLEAN NOT NULL DEFAULT 1"
                    )
                )
                conn.execute(
                    text("UPDATE products SET show_in_pos = 0 WHERE kind = 'STOCK_ONLY'")
                )
            if "show_in_shop" not in pcols:
                conn.execute(
                    text(
                        "ALTER TABLE products ADD COLUMN show_in_shop "
                        "BOOLEAN NOT NULL DEFAULT 1"
                    )
                )
                conn.execute(text("UPDATE products SET show_in_shop = show_in_pos"))
                conn.execute(
                    text("UPDATE products SET show_in_shop = 0 WHERE kind = 'STOCK_ONLY'")
                )
        if "bom_lines" in names:
            bl_cols = {c["name"] for c in insp.get_columns("bom_lines")}
            if "packaging_only" not in bl_cols:
                conn.execute(
                    text(
                        "ALTER TABLE bom_lines ADD COLUMN packaging_only "
                        "BOOLEAN NOT NULL DEFAULT 0"
                    )
                )
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_bom_lines_packaging_only "
                        "ON bom_lines (packaging_only)"
                    )
                )

        if "kitchen_tickets" in names:
            kt_cols = {c["name"] for c in insp.get_columns("kitchen_tickets")}
            if "kitchen_section_id" not in kt_cols:
                conn.execute(
                    text(
                        "ALTER TABLE kitchen_tickets ADD COLUMN kitchen_section_id INTEGER "
                        "REFERENCES kitchen_sections(id)"
                    )
                )
            if "archived_at" not in kt_cols:
                conn.execute(
                    text("ALTER TABLE kitchen_tickets ADD COLUMN archived_at DATETIME")
                )
            if "is_supplement" not in kt_cols:
                conn.execute(
                    text(
                        "ALTER TABLE kitchen_tickets ADD COLUMN is_supplement "
                        "BOOLEAN NOT NULL DEFAULT 0"
                    )
                )
            if "supplement_lines_json" not in kt_cols:
                conn.execute(
                    text(
                        "ALTER TABLE kitchen_tickets ADD COLUMN supplement_lines_json TEXT"
                    )
                )

        if "sale_lines" in names:
            sl_cols = {c["name"] for c in insp.get_columns("sale_lines")}
            if "line_note" not in sl_cols:
                conn.execute(text("ALTER TABLE sale_lines ADD COLUMN line_note TEXT"))
            if "kitchen_sent_qty" not in sl_cols:
                conn.execute(
                    text(
                        "ALTER TABLE sale_lines ADD COLUMN kitchen_sent_qty "
                        "NUMERIC(14, 4) NOT NULL DEFAULT 0"
                    )
                )
                conn.execute(
                    text(
                        "UPDATE sale_lines SET kitchen_sent_qty = quantity "
                        "WHERE sale_id IN ("
                        "SELECT id FROM sales WHERE sent_to_kitchen_at IS NOT NULL"
                        ")"
                    )
                )

        insp = inspect(engine)
        names = set(insp.get_table_names())
        if "dashboard_activities" not in names:
            conn.execute(
                text(
                    """
                CREATE TABLE dashboard_activities (
                    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                    section_key VARCHAR(64) NOT NULL,
                    event_type VARCHAR(64) NOT NULL DEFAULT 'change',
                    ref_id INTEGER,
                    note VARCHAR(255),
                    created_at DATETIME NOT NULL
                )
                """
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_dashboard_activities_section_key "
                    "ON dashboard_activities (section_key)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_dashboard_activities_created_at "
                    "ON dashboard_activities (created_at)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_dashboard_activities_section_created "
                    "ON dashboard_activities (section_key, created_at)"
                )
            )
        if "dashboard_section_seen" not in names:
            conn.execute(
                text(
                    """
                CREATE TABLE dashboard_section_seen (
                    user_id INTEGER NOT NULL,
                    section_key VARCHAR(64) NOT NULL,
                    last_seen_at DATETIME NOT NULL,
                    PRIMARY KEY (user_id, section_key),
                    FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
                )
                """
                )
            )

        insp = inspect(engine)
        names = set(insp.get_table_names())
        if "dashboard_activities" in names:
            da_cols = {c["name"] for c in insp.get_columns("dashboard_activities")}
            if "resolved_at" not in da_cols:
                conn.execute(
                    text(
                        "ALTER TABLE dashboard_activities "
                        "ADD COLUMN resolved_at DATETIME"
                    )
                )
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_dashboard_activities_resolved_at "
                        "ON dashboard_activities (resolved_at)"
                    )
                )

        if "activity_hub_item_states" not in names:
            conn.execute(
                text(
                    """
                CREATE TABLE activity_hub_item_states (
                    user_id INTEGER NOT NULL,
                    item_key VARCHAR(96) NOT NULL,
                    state VARCHAR(16) NOT NULL DEFAULT 'read',
                    updated_at DATETIME NOT NULL,
                    PRIMARY KEY (user_id, item_key),
                    FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
                )
                """
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_activity_hub_item_states_user "
                    "ON activity_hub_item_states (user_id, state)"
                )
            )
        if "activity_hub_mutes" not in names:
            conn.execute(
                text(
                    """
                CREATE TABLE activity_hub_mutes (
                    user_id INTEGER NOT NULL,
                    mute_key VARCHAR(96) NOT NULL,
                    label_ar VARCHAR(160),
                    muted_at DATETIME NOT NULL,
                    PRIMARY KEY (user_id, mute_key),
                    FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
                )
                """
                )
            )
        if "activity_hub_wa_overrides" not in names:
            conn.execute(
                text(
                    """
                CREATE TABLE activity_hub_wa_overrides (
                    event_key VARCHAR(64) NOT NULL PRIMARY KEY,
                    phones VARCHAR(255) NOT NULL DEFAULT '',
                    message_body TEXT,
                    updated_at DATETIME
                )
                """
                )
            )

        insp = inspect(engine)
        names = set(insp.get_table_names())
        if "message_templates" not in names:
            conn.execute(
                text(
                    """
                    CREATE TABLE message_templates (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        code VARCHAR(64) NOT NULL UNIQUE,
                        name VARCHAR(160) NOT NULL,
                        body_text TEXT NOT NULL,
                        body_voice_url VARCHAR(500),
                        is_active BOOLEAN NOT NULL DEFAULT 1,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            )
        if "customer_messaging_profiles" not in names:
            conn.execute(
                text(
                    """
                    CREATE TABLE customer_messaging_profiles (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        customer_id INTEGER NOT NULL UNIQUE REFERENCES customers(id) ON DELETE CASCADE,
                        opt_in BOOLEAN NOT NULL DEFAULT 0,
                        preferred_channel VARCHAR(32) NOT NULL DEFAULT 'whatsapp',
                        telegram_chat_id VARCHAR(64),
                        consent_source VARCHAR(32),
                        opt_in_at DATETIME,
                        opt_out_at DATETIME,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            )
        if "message_rules" not in names:
            conn.execute(
                text(
                    """
                    CREATE TABLE message_rules (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name VARCHAR(160) NOT NULL,
                        event_type VARCHAR(64) NOT NULL,
                        conditions_json TEXT NOT NULL DEFAULT '{}',
                        template_id INTEGER NOT NULL REFERENCES message_templates(id) ON DELETE CASCADE,
                        channels VARCHAR(120) NOT NULL DEFAULT 'whatsapp',
                        audience VARCHAR(20) NOT NULL DEFAULT 'customer',
                        priority INTEGER NOT NULL DEFAULT 100,
                        is_active BOOLEAN NOT NULL DEFAULT 1,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_message_rules_event_type ON message_rules (event_type)"
                )
            )
        if "message_event_definitions" not in names:
            conn.execute(
                text(
                    """
                    CREATE TABLE message_event_definitions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        code VARCHAR(64) NOT NULL UNIQUE,
                        name_ar VARCHAR(160) NOT NULL,
                        description TEXT,
                        category VARCHAR(20) NOT NULL DEFAULT 'automatic',
                        is_system BOOLEAN NOT NULL DEFAULT 0,
                        is_active BOOLEAN NOT NULL DEFAULT 1,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_message_event_definitions_code ON message_event_definitions (code)"
                )
            )
        if "message_event_hooks" not in names:
            conn.execute(
                text(
                    """
                    CREATE TABLE message_event_hooks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        hook_code VARCHAR(64) NOT NULL,
                        event_type VARCHAR(64) NOT NULL,
                        is_active BOOLEAN NOT NULL DEFAULT 1,
                        priority INTEGER NOT NULL DEFAULT 100,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_message_event_hooks_hook_code ON message_event_hooks (hook_code)"
                )
            )
        if "message_campaigns" not in names:
            conn.execute(
                text(
                    """
                    CREATE TABLE message_campaigns (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name VARCHAR(160) NOT NULL,
                        template_id INTEGER NOT NULL REFERENCES message_templates(id) ON DELETE CASCADE,
                        segment_json TEXT NOT NULL DEFAULT '{}',
                        starts_at DATETIME,
                        ends_at DATETIME,
                        is_active BOOLEAN NOT NULL DEFAULT 0,
                        last_sent_at DATETIME,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            )
        if "message_outbox" not in names:
            conn.execute(
                text(
                    """
                    CREATE TABLE message_outbox (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        customer_id INTEGER REFERENCES customers(id) ON DELETE SET NULL,
                        phone VARCHAR(40),
                        channel VARCHAR(32) NOT NULL DEFAULT 'whatsapp',
                        event_type VARCHAR(64),
                        body TEXT NOT NULL,
                        status VARCHAR(20) NOT NULL DEFAULT 'pending',
                        attempts INTEGER NOT NULL DEFAULT 0,
                        error_message VARCHAR(500),
                        rule_id INTEGER REFERENCES message_rules(id) ON DELETE SET NULL,
                        campaign_id INTEGER REFERENCES message_campaigns(id) ON DELETE SET NULL,
                        meta_json TEXT,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        sent_at DATETIME
                    )
                    """
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_message_outbox_status ON message_outbox (status)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_message_outbox_created_at ON message_outbox (created_at)"
                )
            )
        if "message_phone_lists" not in names:
            conn.execute(
                text(
                    """
                    CREATE TABLE message_phone_lists (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name VARCHAR(160) NOT NULL,
                        note TEXT,
                        created_by_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_message_phone_lists_created_at ON message_phone_lists (created_at)"
                )
            )
        if "message_phone_list_entries" not in names:
            conn.execute(
                text(
                    """
                    CREATE TABLE message_phone_list_entries (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        list_id INTEGER NOT NULL REFERENCES message_phone_lists(id) ON DELETE CASCADE,
                        phone VARCHAR(40) NOT NULL,
                        display_name VARCHAR(120),
                        is_valid BOOLEAN NOT NULL DEFAULT 1,
                        skip_reason VARCHAR(120),
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_message_phone_list_entries_list_id ON message_phone_list_entries (list_id)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_message_phone_list_entries_phone ON message_phone_list_entries (phone)"
                )
            )
        if "message_conversations" not in names:
            conn.execute(
                text(
                    """
                    CREATE TABLE message_conversations (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        phone VARCHAR(40) NOT NULL,
                        customer_id INTEGER REFERENCES customers(id) ON DELETE SET NULL,
                        status VARCHAR(20) NOT NULL DEFAULT 'open',
                        assigned_to_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                        last_message_at DATETIME,
                        last_preview VARCHAR(200),
                        unread_count INTEGER NOT NULL DEFAULT 0,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_message_conversations_phone ON message_conversations (phone)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_message_conversations_status ON message_conversations (status)"
                )
            )
        if "message_thread_items" not in names:
            conn.execute(
                text(
                    """
                    CREATE TABLE message_thread_items (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        conversation_id INTEGER NOT NULL REFERENCES message_conversations(id) ON DELETE CASCADE,
                        direction VARCHAR(16) NOT NULL,
                        body TEXT NOT NULL,
                        channel VARCHAR(32) NOT NULL DEFAULT 'whatsapp',
                        user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                        outbox_id INTEGER REFERENCES message_outbox(id) ON DELETE SET NULL,
                        external_id VARCHAR(120),
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_message_thread_items_conversation_id ON message_thread_items (conversation_id)"
                )
            )
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_message_thread_external_id ON message_thread_items (external_id) WHERE external_id IS NOT NULL"
                )
            )
        if "web_chat_sessions" not in names:
            conn.execute(
                text(
                    """
                    CREATE TABLE web_chat_sessions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        token VARCHAR(64) NOT NULL UNIQUE,
                        guest_name VARCHAR(120),
                        guest_phone VARCHAR(40),
                        department VARCHAR(20),
                        status VARCHAR(20) NOT NULL DEFAULT 'open',
                        inbox_conversation_id INTEGER REFERENCES message_conversations(id) ON DELETE SET NULL,
                        last_message_at DATETIME,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_web_chat_sessions_token ON web_chat_sessions (token)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_web_chat_sessions_status ON web_chat_sessions (status)"
                )
            )
        if "web_chat_messages" not in names:
            conn.execute(
                text(
                    """
                    CREATE TABLE web_chat_messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id INTEGER NOT NULL REFERENCES web_chat_sessions(id) ON DELETE CASCADE,
                        direction VARCHAR(16) NOT NULL,
                        sender_type VARCHAR(16) NOT NULL DEFAULT 'guest',
                        body TEXT NOT NULL,
                        external_id VARCHAR(120),
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_web_chat_messages_session_id ON web_chat_messages (session_id)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_web_chat_messages_created_at ON web_chat_messages (created_at)"
                )
            )
        if "web_chat_messages" in names:
            wcm_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(web_chat_messages)"))}
            if "image_url" not in wcm_cols:
                conn.execute(
                    text("ALTER TABLE web_chat_messages ADD COLUMN image_url VARCHAR(512)")
                )
        if "web_chat_sessions" in names:
            wcs_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(web_chat_sessions)"))}
            if "order_phase" not in wcs_cols:
                conn.execute(
                    text(
                        "ALTER TABLE web_chat_sessions ADD COLUMN order_phase VARCHAR(32) NOT NULL DEFAULT 'browse'"
                    )
                )
            if "order_json" not in wcs_cols:
                conn.execute(
                    text("ALTER TABLE web_chat_sessions ADD COLUMN order_json TEXT NOT NULL DEFAULT '{}'")
                )
            if "sale_id" not in wcs_cols:
                conn.execute(
                    text("ALTER TABLE web_chat_sessions ADD COLUMN sale_id INTEGER REFERENCES sales(id) ON DELETE SET NULL")
                )
                conn.execute(
                    text("CREATE INDEX IF NOT EXISTS ix_web_chat_sessions_sale_id ON web_chat_sessions (sale_id)")
                )
            if "payment_proof_filename" not in wcs_cols:
                conn.execute(
                    text("ALTER TABLE web_chat_sessions ADD COLUMN payment_proof_filename VARCHAR(255)")
                )
            if "feedback_rating" not in wcs_cols:
                conn.execute(
                    text("ALTER TABLE web_chat_sessions ADD COLUMN feedback_rating INTEGER")
                )
            if "feedback_comment" not in wcs_cols:
                conn.execute(
                    text("ALTER TABLE web_chat_sessions ADD COLUMN feedback_comment TEXT")
                )
            conn.execute(
                text("CREATE INDEX IF NOT EXISTS ix_web_chat_sessions_order_phase ON web_chat_sessions (order_phase)")
            )

        insp = inspect(engine)
        names = set(insp.get_table_names())
        if "gl_accounts" in names:
            gl_cols = {c["name"] for c in insp.get_columns("gl_accounts")}
            if "show_on_dashboard" not in gl_cols:
                conn.execute(
                    text(
                        "ALTER TABLE gl_accounts ADD COLUMN show_on_dashboard BOOLEAN NOT NULL DEFAULT 0"
                    )
                )

        insp = inspect(engine)
        names = set(insp.get_table_names())
        if "purchases" in names:
            pcols = {c["name"] for c in insp.get_columns("purchases")}
            if "receipt_batch_no" not in pcols:
                conn.execute(
                    text("ALTER TABLE purchases ADD COLUMN receipt_batch_no VARCHAR(32)")
                )
                conn.execute(
                    text(
                        "UPDATE purchases SET receipt_batch_no = "
                        "printf('GR-%06d', id) WHERE kind = 'INVENTORY' "
                        "AND receipt_batch_no IS NULL"
                    )
                )
        if "purchase_lines" in names:
            lcols = {c["name"] for c in insp.get_columns("purchase_lines")}
            if "lot_code" not in lcols:
                conn.execute(
                    text("ALTER TABLE purchase_lines ADD COLUMN lot_code VARCHAR(32)")
                )
            if "production_date" not in lcols:
                conn.execute(text("ALTER TABLE purchase_lines ADD COLUMN production_date DATE"))
            if "expiry_date" not in lcols:
                conn.execute(text("ALTER TABLE purchase_lines ADD COLUMN expiry_date DATE"))
            if "line_kind" not in lcols:
                conn.execute(
                    text("ALTER TABLE purchase_lines ADD COLUMN line_kind VARCHAR(20)")
                )
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
        if "inventory_lots" in names:
            lot_cols = {c["name"] for c in insp.get_columns("inventory_lots")}
            if "unit_cost" not in lot_cols:
                conn.execute(
                    text(
                        "ALTER TABLE inventory_lots ADD COLUMN unit_cost "
                        "NUMERIC(14, 3) NOT NULL DEFAULT 0"
                    )
                )
                conn.execute(
                    text(
                        "UPDATE inventory_lots SET unit_cost = ("
                        "SELECT pl.unit_cost FROM purchase_lines pl "
                        "WHERE pl.id = inventory_lots.purchase_line_id"
                        ") WHERE unit_cost = 0"
                    )
                )
        if "inventory_lot_consumptions" not in names:
            conn.execute(
                text(
                    """
                    CREATE TABLE inventory_lot_consumptions (
                        id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                        stock_movement_id INTEGER NOT NULL REFERENCES stock_movements(id) ON DELETE CASCADE,
                        inventory_lot_id INTEGER NOT NULL REFERENCES inventory_lots(id) ON DELETE CASCADE,
                        quantity NUMERIC(14, 4) NOT NULL,
                        unit_cost NUMERIC(14, 3) NOT NULL,
                        line_total NUMERIC(14, 3) NOT NULL,
                        created_at DATETIME NOT NULL
                    )
                    """
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_inventory_lot_consumptions_movement "
                    "ON inventory_lot_consumptions (stock_movement_id)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_inventory_lot_consumptions_lot "
                    "ON inventory_lot_consumptions (inventory_lot_id)"
                )
            )
        if "purchases" in names:
            conn.execute(
                text(
                    "UPDATE purchases SET amount = ("
                    "SELECT COALESCE(SUM(pl.line_total), 0) FROM purchase_lines pl "
                    "WHERE pl.purchase_id = purchases.id"
                    ") WHERE supplier = 'مرجع تكلفة' AND (amount IS NULL OR amount = 0)"
                )
            )

        insp = inspect(engine)
        names = set(insp.get_table_names())
        if "shop_product_ratings" not in names:
            conn.execute(
                text(
                    """
                    CREATE TABLE shop_product_ratings (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        product_id INTEGER NOT NULL,
                        guest_phone VARCHAR(40),
                        stars INTEGER NOT NULL,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_shop_product_ratings_product_id "
                    "ON shop_product_ratings (product_id)"
                )
            )

        insp = inspect(engine)
        names = set(insp.get_table_names())
        if "web_analytics_events" not in names:
            conn.execute(
                text(
                    """
                    CREATE TABLE web_analytics_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        surface VARCHAR(32) NOT NULL,
                        event_type VARCHAR(40) NOT NULL,
                        page_path VARCHAR(255) NOT NULL DEFAULT '/',
                        session_id VARCHAR(64) NOT NULL DEFAULT '',
                        referrer VARCHAR(500),
                        meta_json TEXT NOT NULL DEFAULT '{}',
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_web_analytics_events_surface "
                    "ON web_analytics_events (surface)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_web_analytics_events_event_type "
                    "ON web_analytics_events (event_type)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_web_analytics_events_session_id "
                    "ON web_analytics_events (session_id)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_web_analytics_events_created_at "
                    "ON web_analytics_events (created_at)"
                )
            )

        insp = inspect(engine)
        names = set(insp.get_table_names())
        if "notification_events" not in names:
            conn.execute(
                text(
                    """
                    CREATE TABLE notification_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_key VARCHAR(64) NOT NULL,
                        source_type VARCHAR(32) NOT NULL,
                        source_id INTEGER,
                        payload_json TEXT NOT NULL DEFAULT '{}',
                        status VARCHAR(20) NOT NULL DEFAULT 'pending',
                        error_message VARCHAR(500),
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        processed_at DATETIME
                    )
                    """
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_notification_events_event_key "
                    "ON notification_events (event_key)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_notification_events_status "
                    "ON notification_events (status)"
                )
            )
        if "notification_templates" not in names:
            conn.execute(
                text(
                    """
                    CREATE TABLE notification_templates (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name VARCHAR(160) NOT NULL,
                        event_key VARCHAR(64) NOT NULL,
                        channel VARCHAR(32) NOT NULL DEFAULT 'whatsapp',
                        recipient_type VARCHAR(32) NOT NULL,
                        message_type VARCHAR(20) NOT NULL DEFAULT 'text',
                        title VARCHAR(160),
                        body_template TEXT NOT NULL,
                        buttons_json TEXT,
                        image_url VARCHAR(500),
                        document_url VARCHAR(500),
                        language VARCHAR(8) NOT NULL DEFAULT 'ar',
                        is_active BOOLEAN NOT NULL DEFAULT 1,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            )
        if "notification_rules" not in names:
            conn.execute(
                text(
                    """
                    CREATE TABLE notification_rules (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_key VARCHAR(64) NOT NULL,
                        recipient_type VARCHAR(32) NOT NULL,
                        channel VARCHAR(32) NOT NULL DEFAULT 'whatsapp',
                        template_id INTEGER NOT NULL REFERENCES notification_templates(id) ON DELETE CASCADE,
                        is_active BOOLEAN NOT NULL DEFAULT 1,
                        priority VARCHAR(16) NOT NULL DEFAULT 'normal',
                        delay_seconds INTEGER NOT NULL DEFAULT 0,
                        condition_json TEXT NOT NULL DEFAULT '{}',
                        throttle_minutes INTEGER,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            )
        if "notification_logs" not in names:
            conn.execute(
                text(
                    """
                    CREATE TABLE notification_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_key VARCHAR(64) NOT NULL,
                        notification_event_id INTEGER REFERENCES notification_events(id) ON DELETE SET NULL,
                        source_type VARCHAR(32) NOT NULL,
                        source_id INTEGER,
                        recipient_type VARCHAR(32) NOT NULL,
                        recipient_name VARCHAR(160),
                        recipient_phone VARCHAR(40),
                        channel VARCHAR(32) NOT NULL,
                        message_type VARCHAR(20) NOT NULL,
                        template_id INTEGER REFERENCES notification_templates(id) ON DELETE SET NULL,
                        body_rendered TEXT NOT NULL,
                        provider VARCHAR(32),
                        provider_message_id VARCHAR(120),
                        outbox_id INTEGER,
                        status VARCHAR(20) NOT NULL DEFAULT 'queued',
                        error_message VARCHAR(500),
                        idempotency_key VARCHAR(200) NOT NULL UNIQUE,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        last_attempt_at DATETIME,
                        sent_at DATETIME,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            )
        if "notification_actions" not in names:
            conn.execute(
                text(
                    """
                    CREATE TABLE notification_actions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        notification_log_id INTEGER REFERENCES notification_logs(id) ON DELETE CASCADE,
                        action_key VARCHAR(64) NOT NULL,
                        action_payload_json TEXT NOT NULL DEFAULT '{}',
                        received_from_phone VARCHAR(40),
                        handled_status VARCHAR(20) NOT NULL DEFAULT 'pending',
                        handled_at DATETIME,
                        result_message VARCHAR(500),
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            )
        elif "notification_actions" in names:
            try:
                conn.execute(
                    text(
                        "INSERT INTO notification_actions "
                        "(notification_log_id, action_key, action_payload_json, handled_status) "
                        "VALUES (NULL, '__nullable_probe__', '{}', 'pending')"
                    )
                )
                conn.execute(
                    text(
                        "DELETE FROM notification_actions WHERE action_key='__nullable_probe__'"
                    )
                )
            except Exception:
                conn.execute(
                    text("ALTER TABLE notification_actions RENAME TO notification_actions_old")
                )
                conn.execute(
                    text(
                        """
                        CREATE TABLE notification_actions (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            notification_log_id INTEGER REFERENCES notification_logs(id) ON DELETE CASCADE,
                            action_key VARCHAR(64) NOT NULL,
                            action_payload_json TEXT NOT NULL DEFAULT '{}',
                            received_from_phone VARCHAR(40),
                            handled_status VARCHAR(20) NOT NULL DEFAULT 'pending',
                            handled_at DATETIME,
                            result_message VARCHAR(500),
                            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                        )
                        """
                    )
                )
                conn.execute(
                    text(
                        """
                        INSERT INTO notification_actions (
                            id, notification_log_id, action_key, action_payload_json,
                            received_from_phone, handled_status, handled_at,
                            result_message, created_at
                        )
                        SELECT
                            id, notification_log_id, action_key, action_payload_json,
                            received_from_phone, handled_status, handled_at,
                            result_message, created_at
                        FROM notification_actions_old
                        """
                    )
                )
                conn.execute(text("DROP TABLE notification_actions_old"))

        insp = inspect(engine)
        names = set(insp.get_table_names())
        if "notification_routes" not in names:
            conn.execute(
                text(
                    """
                    CREATE TABLE notification_routes (
                        id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                        name_ar VARCHAR(120) NOT NULL,
                        event_patterns_json TEXT NOT NULL DEFAULT '[]',
                        phones TEXT NOT NULL DEFAULT '',
                        recipient_scope VARCHAR(32) NOT NULL DEFAULT '*',
                        is_active BOOLEAN NOT NULL DEFAULT 1,
                        sort_order INTEGER NOT NULL DEFAULT 100,
                        notes TEXT,
                        created_at DATETIME NOT NULL,
                        updated_at DATETIME NOT NULL
                    )
                    """
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_notification_routes_scope "
                    "ON notification_routes (recipient_scope)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_notification_routes_active "
                    "ON notification_routes (is_active)"
                )
            )

        if "hr_payroll_entries" in names:
            pe_cols = {c["name"] for c in insp.get_columns("hr_payroll_entries")}
            if "salary_receipt_confirmed_at" not in pe_cols:
                conn.execute(
                    text(
                        "ALTER TABLE hr_payroll_entries ADD COLUMN salary_receipt_confirmed_at DATETIME"
                    )
                )
            if "salary_receipt_confirmed_via" not in pe_cols:
                conn.execute(
                    text(
                        "ALTER TABLE hr_payroll_entries ADD COLUMN salary_receipt_confirmed_via VARCHAR(32)"
                    )
                )

        # ── Hotel booking module (rooms extension + booking tables via create_all)
        insp = inspect(engine)
        names = set(insp.get_table_names())
        if "hotel_rooms" in names:
            hr_cols = {c["name"] for c in insp.get_columns("hotel_rooms")}
            patches = [
                ("property_id", "INTEGER NOT NULL DEFAULT 1"),
                ("room_type_id", "INTEGER REFERENCES hotel_room_types(id) ON DELETE SET NULL"),
                ("floor", "VARCHAR(20)"),
                ("physical_status", "VARCHAR(32) NOT NULL DEFAULT 'AVAILABLE'"),
                ("name_ar", "VARCHAR(120)"),
                ("nightly_price", "NUMERIC(14, 3)"),
                ("image_filename", "VARCHAR(255)"),
            ]
            for col, ddl in patches:
                if col not in hr_cols:
                    conn.execute(text(f"ALTER TABLE hotel_rooms ADD COLUMN {col} {ddl}"))
        if "hotel_bookings" in names:
            hb_cols = {c["name"] for c in insp.get_columns("hotel_bookings")}
            booking_patches = [
                ("guest_id_type", "VARCHAR(80)"),
                ("guest_address", "TEXT"),
                ("guest_nationality", "VARCHAR(80)"),
                ("guest_type", "VARCHAR(20) NOT NULL DEFAULT 'INDIVIDUAL'"),
                ("record_kind", "VARCHAR(20) NOT NULL DEFAULT 'BOOKING'"),
                ("quotation_status", "VARCHAR(20)"),
                ("company_name", "VARCHAR(200)"),
                ("company_tax_id", "VARCHAR(64)"),
                ("company_address", "TEXT"),
                ("company_contact_name", "VARCHAR(160)"),
                ("company_contact_phone", "VARCHAR(40)"),
                ("company_contact_email", "VARCHAR(120)"),
                ("quotation_valid_until", "DATE"),
                ("quotation_notes", "TEXT"),
            ]
            for col, ddl in booking_patches:
                if col not in hb_cols:
                    conn.execute(text(f"ALTER TABLE hotel_bookings ADD COLUMN {col} {ddl}"))
            if "scheduled_check_out" not in hb_cols:
                conn.execute(text("ALTER TABLE hotel_bookings ADD COLUMN scheduled_check_out DATE"))
        if "hotel_booking_debts" not in names:
            conn.execute(
                text(
                    """
                    CREATE TABLE hotel_booking_debts (
                        id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                        booking_id INTEGER NOT NULL REFERENCES hotel_bookings(id) ON DELETE CASCADE,
                        amount NUMERIC(14, 3) NOT NULL,
                        status VARCHAR(20) NOT NULL DEFAULT 'OPEN',
                        reason TEXT,
                        settlement_json TEXT,
                        created_by_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                        created_at DATETIME NOT NULL,
                        collected_at DATETIME,
                        collected_by_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                        collection_payment_id INTEGER REFERENCES hotel_booking_payments(id) ON DELETE SET NULL,
                        written_off_at DATETIME,
                        written_off_by_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                        write_off_reason TEXT
                    )
                    """
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_hotel_booking_debts_booking_id "
                    "ON hotel_booking_debts (booking_id)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_hotel_booking_debts_status "
                    "ON hotel_booking_debts (status)"
                )
            )
        if "hotel_booking_debts" in names:
            debt_cols = {c["name"] for c in insp.get_columns("hotel_booking_debts")}
            if "amount_remaining" not in debt_cols:
                conn.execute(
                    text(
                        "ALTER TABLE hotel_booking_debts "
                        "ADD COLUMN amount_remaining NUMERIC(14, 3)"
                    )
                )
                conn.execute(
                    text(
                        "UPDATE hotel_booking_debts SET amount_remaining = amount "
                        "WHERE amount_remaining IS NULL AND status = 'OPEN'"
                    )
                )
            if "follow_up_notes" not in debt_cols:
                conn.execute(
                    text("ALTER TABLE hotel_booking_debts ADD COLUMN follow_up_notes TEXT")
                )
            if "reminder_at" not in debt_cols:
                conn.execute(
                    text("ALTER TABLE hotel_booking_debts ADD COLUMN reminder_at DATE")
                )
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_hotel_booking_debts_reminder_at "
                        "ON hotel_booking_debts (reminder_at)"
                    )
                )
        if "hotel_room_charges" in names:
            rc_cols = {c["name"] for c in insp.get_columns("hotel_room_charges")}
            if "booking_id" not in rc_cols:
                conn.execute(
                    text(
                        "ALTER TABLE hotel_room_charges ADD COLUMN booking_id INTEGER "
                        "REFERENCES hotel_bookings(id) ON DELETE SET NULL"
                    )
                )
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_hotel_room_charges_booking_id "
                        "ON hotel_room_charges (booking_id)"
                    )
                )
        if "sales" in names:
            s_cols = {c["name"] for c in insp.get_columns("sales")}
            if "booking_id" not in s_cols:
                conn.execute(
                    text(
                        "ALTER TABLE sales ADD COLUMN booking_id INTEGER "
                        "REFERENCES hotel_bookings(id) ON DELETE SET NULL"
                    )
                )
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_sales_booking_id ON sales (booking_id)"
                    )
                )

        # ── Warehouse transfers + shift/user warehouse ──
        insp = inspect(engine)
        names = set(insp.get_table_names())
        if "warehouse_transfers" not in names:
            conn.execute(
                text(
                    """
                    CREATE TABLE warehouse_transfers (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        from_warehouse_id INTEGER NOT NULL REFERENCES warehouses(id),
                        to_warehouse_id INTEGER NOT NULL REFERENCES warehouses(id),
                        status VARCHAR(24) NOT NULL DEFAULT 'PENDING',
                        note TEXT,
                        created_by_id INTEGER REFERENCES users(id),
                        created_at DATETIME NOT NULL,
                        closed_at DATETIME
                    )
                    """
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_warehouse_transfers_status "
                    "ON warehouse_transfers (status)"
                )
            )
        if "warehouse_transfer_lines" not in names:
            conn.execute(
                text(
                    """
                    CREATE TABLE warehouse_transfer_lines (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        transfer_id INTEGER NOT NULL REFERENCES warehouse_transfers(id) ON DELETE CASCADE,
                        product_id INTEGER NOT NULL REFERENCES products(id),
                        qty_sent NUMERIC(14, 4) NOT NULL,
                        qty_received NUMERIC(14, 4),
                        line_status VARCHAR(24) NOT NULL DEFAULT 'PENDING',
                        recipient_note TEXT,
                        responded_by_id INTEGER REFERENCES users(id),
                        responded_at DATETIME
                    )
                    """
                )
            )
        if "stock_movements" in names:
            sm_cols = {c["name"] for c in insp.get_columns("stock_movements")}
            if "transfer_id" not in sm_cols:
                conn.execute(
                    text(
                        "ALTER TABLE stock_movements ADD COLUMN transfer_id INTEGER "
                        "REFERENCES warehouse_transfers(id) ON DELETE SET NULL"
                    )
                )
        if "pos_shifts" in names:
            ps_cols = {c["name"] for c in insp.get_columns("pos_shifts")}
            if "warehouse_id" not in ps_cols:
                conn.execute(
                    text(
                        "ALTER TABLE pos_shifts ADD COLUMN warehouse_id INTEGER "
                        "REFERENCES warehouses(id) ON DELETE SET NULL"
                    )
                )
        if "users" in names:
            u_cols = {c["name"] for c in insp.get_columns("users")}
            if "warehouse_id" not in u_cols:
                conn.execute(
                    text(
                        "ALTER TABLE users ADD COLUMN warehouse_id INTEGER "
                        "REFERENCES warehouses(id) ON DELETE SET NULL"
                    )
                )
            if "kds_scope" not in u_cols:
                conn.execute(
                    text("ALTER TABLE users ADD COLUMN kds_scope VARCHAR(20) NOT NULL DEFAULT 'ALL'")
                )
            if "view_scope" not in u_cols:
                conn.execute(
                    text(
                        "ALTER TABLE users ADD COLUMN view_scope VARCHAR(20) NOT NULL DEFAULT 'both'"
                    )
                )
            if "ui_hidden" not in u_cols:
                conn.execute(
                    text(
                        "ALTER TABLE users ADD COLUMN ui_hidden VARCHAR(4000) NOT NULL DEFAULT '[]'"
                    )
                )
        if "warehouses" in names:
            wh_cols = {c["name"] for c in insp.get_columns("warehouses")}
            if "deduct_sales_enabled" not in wh_cols:
                conn.execute(
                    text(
                        "ALTER TABLE warehouses ADD COLUMN deduct_sales_enabled "
                        "BOOLEAN NOT NULL DEFAULT 0"
                    )
                )
                conn.execute(
                    text(
                        "UPDATE warehouses SET deduct_sales_enabled = 1 "
                        "WHERE is_main = 1"
                    )
                )
        if "products" in names:
            p_cols = {c["name"] for c in insp.get_columns("products")}
            if "sales_warehouse_id" not in p_cols:
                conn.execute(
                    text(
                        "ALTER TABLE products ADD COLUMN sales_warehouse_id INTEGER "
                        "REFERENCES warehouses(id) ON DELETE SET NULL"
                    )
                )

        insp = inspect(engine)
        names = set(insp.get_table_names())
        if "hotel_shifts" not in names:
            conn.execute(
                text(
                    """
                    CREATE TABLE hotel_shifts (
                        id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                        property_id INTEGER NOT NULL DEFAULT 1,
                        shift_number INTEGER NOT NULL,
                        shift_name_ar VARCHAR(80) NOT NULL,
                        status VARCHAR(20) NOT NULL,
                        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        closed_by_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                        employee_id INTEGER REFERENCES hr_employees(id) ON DELETE SET NULL,
                        scheduled_start DATETIME NOT NULL,
                        scheduled_end DATETIME NOT NULL,
                        opened_at DATETIME NOT NULL,
                        closed_at DATETIME,
                        opening_note TEXT,
                        closing_note TEXT,
                        total_revenue NUMERIC(14, 3),
                        total_expenses NUMERIC(14, 3),
                        cash_collected NUMERIC(14, 3),
                        bank_collected NUMERIC(14, 3),
                        payment_count INTEGER NOT NULL DEFAULT 0,
                        expense_count INTEGER NOT NULL DEFAULT 0,
                        overdue_notified_at DATETIME
                    )
                    """
                )
            )
            conn.execute(
                text("CREATE INDEX IF NOT EXISTS ix_hotel_shifts_status ON hotel_shifts (status)")
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_hotel_shifts_opened_at ON hotel_shifts (opened_at)"
                )
            )
        if "purchases" in names:
            pcols = {c["name"] for c in insp.get_columns("purchases")}
            if "hotel_shift_id" not in pcols:
                conn.execute(
                    text(
                        "ALTER TABLE purchases ADD COLUMN hotel_shift_id INTEGER "
                        "REFERENCES hotel_shifts(id) ON DELETE SET NULL"
                    )
                )
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_purchases_hotel_shift_id "
                        "ON purchases (hotel_shift_id)"
                    )
                )

        if "hotel_shifts" in names:
            hcols = {c["name"] for c in insp.get_columns("hotel_shifts")}
            for col, ddl in (
                ("opening_cash", "NUMERIC(14, 3)"),
                ("counted_cash", "NUMERIC(14, 3)"),
                ("counted_bank", "NUMERIC(14, 3)"),
                ("expected_cash", "NUMERIC(14, 3)"),
                ("expected_bank", "NUMERIC(14, 3)"),
                ("cash_difference", "NUMERIC(14, 3)"),
                ("bank_difference", "NUMERIC(14, 3)"),
                ("expected_bookings_count", "INTEGER"),
                ("counted_bookings_count", "INTEGER"),
                ("expected_meals_count", "INTEGER"),
                ("counted_meals_count", "INTEGER"),
                ("expected_laundry_count", "INTEGER"),
                ("counted_laundry_count", "INTEGER"),
                ("expected_services_count", "INTEGER"),
                ("counted_services_count", "INTEGER"),
                ("close_snapshot_json", "TEXT"),
            ):
                if col not in hcols:
                    conn.execute(text(f"ALTER TABLE hotel_shifts ADD COLUMN {col} {ddl}"))
        if "hr_employees" in names:
            ecols = {c["name"] for c in insp.get_columns("hr_employees")}
            if "is_hotel_front" not in ecols:
                conn.execute(
                    text(
                        "ALTER TABLE hr_employees ADD COLUMN is_hotel_front BOOLEAN NOT NULL DEFAULT 0"
                    )
                )

        insp = inspect(engine)
        names = set(insp.get_table_names())
        if "customers" in names:
            ccols = {c["name"] for c in insp.get_columns("customers")}
            for col, ddl in (
                ("customer_type", "VARCHAR(20) NOT NULL DEFAULT 'INDIVIDUAL'"),
                ("company_name", "VARCHAR(200)"),
                ("wallet_balance", "NUMERIC(14, 3) NOT NULL DEFAULT 0"),
                ("business_domain", "VARCHAR(20) NOT NULL DEFAULT 'restaurant'"),
                ("loyalty_intro_sent_at", "DATETIME"),
            ):
                if col not in ccols:
                    conn.execute(text(f"ALTER TABLE customers ADD COLUMN {col} {ddl}"))
            # أُضيف العمود للتو أو كان موجوداً — حدّث التخزين فقط عند الإضافة الأولى
            ccols_after = {c["name"] for c in insp.get_columns("customers")}
            if "business_domain" in ccols_after and "business_domain" not in ccols:
                try:
                    conn.execute(
                        text(
                            """
                            UPDATE customers
                            SET business_domain = 'hotel'
                            WHERE id IN (
                                SELECT customer_id FROM hotel_bookings
                                WHERE customer_id IS NOT NULL
                            )
                            OR id IN (
                                SELECT customer_id FROM sales
                                WHERE customer_id IS NOT NULL
                                  AND (
                                    booking_id IS NOT NULL
                                    OR UPPER(COALESCE(context_type, '')) = 'ROOM'
                                  )
                            )
                            """
                        )
                    )
                    conn.execute(
                        text(
                            """
                            UPDATE customers
                            SET business_domain = 'shared'
                            WHERE business_domain = 'hotel'
                              AND id IN (
                                SELECT customer_id FROM sales
                                WHERE customer_id IS NOT NULL
                                  AND booking_id IS NULL
                                  AND (
                                    context_type IS NULL
                                    OR UPPER(COALESCE(context_type, '')) <> 'ROOM'
                                  )
                              )
                            """
                        )
                    )
                except Exception:
                    pass
        if "customer_wallet_transactions" not in names:
            conn.execute(
                text(
                    """
                    CREATE TABLE customer_wallet_transactions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
                        kind VARCHAR(20) NOT NULL DEFAULT 'ADJUST',
                        amount NUMERIC(14, 3) NOT NULL DEFAULT 0,
                        note VARCHAR(255),
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        created_by_id INTEGER REFERENCES users(id) ON DELETE SET NULL
                    )
                    """
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_customer_wallet_transactions_customer_id "
                    "ON customer_wallet_transactions (customer_id)"
                )
            )
        if "hotel_booking_payment_refunds" in names:
            href_cols = {
                c["name"] for c in insp.get_columns("hotel_booking_payment_refunds")
            }
            if "payment_method_id" not in href_cols:
                conn.execute(
                    text(
                        "ALTER TABLE hotel_booking_payment_refunds "
                        "ADD COLUMN payment_method_id INTEGER "
                        "REFERENCES payment_methods(id) ON DELETE SET NULL"
                    )
                )
                try:
                    conn.execute(
                        text(
                            """
                            UPDATE hotel_booking_payment_refunds
                            SET payment_method_id = (
                                SELECT payment_method_id FROM hotel_booking_payments
                                WHERE hotel_booking_payments.id =
                                      hotel_booking_payment_refunds.payment_id
                            )
                            WHERE payment_method_id IS NULL
                            """
                        )
                    )
                except Exception:
                    pass
        if "hotel_booking_guests" in names:
            bg_cols = {c["name"] for c in insp.get_columns("hotel_booking_guests")}
            for col, ddl in (
                ("id_type", "VARCHAR(80)"),
                ("nationality", "VARCHAR(80)"),
                ("address", "TEXT"),
                ("id_document_filename", "VARCHAR(255)"),
            ):
                if col not in bg_cols:
                    conn.execute(text(f"ALTER TABLE hotel_booking_guests ADD COLUMN {col} {ddl}"))
        if "hotel_rooms" in names:
            hr_cols = {c["name"] for c in insp.get_columns("hotel_rooms")}
            for col, ddl in (
                ("show_online", "BOOLEAN NOT NULL DEFAULT 0"),
                ("online_description", "TEXT"),
                ("lock_no", "VARCHAR(16)"),
                ("rooms_count", "INTEGER NOT NULL DEFAULT 1"),
                ("beds_count", "INTEGER NOT NULL DEFAULT 1"),
                ("allows_infant", "BOOLEAN NOT NULL DEFAULT 0"),
            ):
                if col not in hr_cols:
                    conn.execute(text(f"ALTER TABLE hotel_rooms ADD COLUMN {col} {ddl}"))
            hr_cols = {c["name"] for c in insp.get_columns("hotel_rooms")}
            added_bed_split = False
            if "double_beds_count" not in hr_cols:
                conn.execute(
                    text(
                        "ALTER TABLE hotel_rooms ADD COLUMN double_beds_count INTEGER NOT NULL DEFAULT 1"
                    )
                )
                added_bed_split = True
            if "single_beds_count" not in hr_cols:
                conn.execute(
                    text(
                        "ALTER TABLE hotel_rooms ADD COLUMN single_beds_count INTEGER NOT NULL DEFAULT 0"
                    )
                )
                added_bed_split = True
            if added_bed_split:
                # الأسرة القديمة → زوجية (مرة واحدة عند إضافة الأعمدة)
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
        if "hotel_room_media" not in names:
            conn.execute(
                text(
                    """
                    CREATE TABLE hotel_room_media (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        room_id INTEGER NOT NULL REFERENCES hotel_rooms(id) ON DELETE CASCADE,
                        kind VARCHAR(20) NOT NULL DEFAULT 'IMAGE',
                        filename VARCHAR(255) NOT NULL,
                        caption VARCHAR(200),
                        sort_order INTEGER NOT NULL DEFAULT 0,
                        is_active BOOLEAN NOT NULL DEFAULT 1,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            )
        if "hotel_room_types" in names:
            hrt_cols = {c["name"] for c in insp.get_columns("hotel_room_types")}
            for col, ddl in (
                ("max_occupancy", "INTEGER"),
                ("beds_description", "VARCHAR(200)"),
                ("allows_extra_bed", "BOOLEAN NOT NULL DEFAULT 0"),
            ):
                if col not in hrt_cols:
                    conn.execute(text(f"ALTER TABLE hotel_room_types ADD COLUMN {col} {ddl}"))
        if "hotel_bookings" in names:
            hb_cols = {c["name"] for c in insp.get_columns("hotel_bookings")}
            if "final_invoice_number" not in hb_cols:
                conn.execute(
                    text("ALTER TABLE hotel_bookings ADD COLUMN final_invoice_number VARCHAR(32)")
                )
            for col, ddl in (
                ("follow_up_at", "DATE"),
                ("follow_up_note", "TEXT"),
                ("claim_wa_until_paid", "BOOLEAN NOT NULL DEFAULT 0"),
            ):
                if col not in hb_cols:
                    conn.execute(text(f"ALTER TABLE hotel_bookings ADD COLUMN {col} {ddl}"))
            if "follow_up_at" not in hb_cols:
                try:
                    conn.execute(
                        text(
                            "CREATE INDEX IF NOT EXISTS ix_hotel_bookings_follow_up_at "
                            "ON hotel_bookings (follow_up_at)"
                        )
                    )
                except Exception:
                    pass
        if "hotel_booking_payments" in names:
            hbp_cols = {c["name"] for c in insp.get_columns("hotel_booking_payments")}
            if "receipt_number" not in hbp_cols:
                conn.execute(
                    text("ALTER TABLE hotel_booking_payments ADD COLUMN receipt_number VARCHAR(32)")
                )
            if "hotel_shift_id" not in hbp_cols:
                conn.execute(
                    text(
                        "ALTER TABLE hotel_booking_payments ADD COLUMN hotel_shift_id "
                        "INTEGER REFERENCES hotel_shifts(id) ON DELETE SET NULL"
                    )
                )
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_hotel_booking_payments_hotel_shift_id "
                        "ON hotel_booking_payments (hotel_shift_id)"
                    )
                )
            if "received_by_employee_id" not in hbp_cols:
                conn.execute(
                    text(
                        "ALTER TABLE hotel_booking_payments ADD COLUMN received_by_employee_id "
                        "INTEGER REFERENCES hr_employees(id) ON DELETE SET NULL"
                    )
                )
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_hotel_booking_payments_recv_emp "
                        "ON hotel_booking_payments (received_by_employee_id)"
                    )
                )
        if "hotel_booking_payment_refunds" in names:
            hbr_cols = {c["name"] for c in insp.get_columns("hotel_booking_payment_refunds")}
            if "hotel_shift_id" not in hbr_cols:
                conn.execute(
                    text(
                        "ALTER TABLE hotel_booking_payment_refunds ADD COLUMN hotel_shift_id "
                        "INTEGER REFERENCES hotel_shifts(id) ON DELETE SET NULL"
                    )
                )
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_hotel_booking_payment_refunds_shift "
                        "ON hotel_booking_payment_refunds (hotel_shift_id)"
                    )
                )
            if "approved_by_employee_id" not in hbr_cols:
                conn.execute(
                    text(
                        "ALTER TABLE hotel_booking_payment_refunds ADD COLUMN approved_by_employee_id "
                        "INTEGER REFERENCES hr_employees(id) ON DELETE SET NULL"
                    )
                )
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_hotel_booking_payment_refunds_emp "
                        "ON hotel_booking_payment_refunds (approved_by_employee_id)"
                    )
                )
        if "sales" in names:
            sale_cols = {c["name"] for c in insp.get_columns("sales")}
            for col, ddl in (
                ("receipt_number", "VARCHAR(32)"),
                ("final_invoice_number", "VARCHAR(32)"),
            ):
                if col not in sale_cols:
                    conn.execute(text(f"ALTER TABLE sales ADD COLUMN {col} {ddl}"))
        if "hotel_service_catalog" not in names:
            conn.execute(
                text(
                    """
                    CREATE TABLE hotel_service_catalog (
                        id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                        name_ar VARCHAR(160) NOT NULL,
                        default_price NUMERIC(14, 3) NOT NULL DEFAULT 0,
                        product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
                        sort_order INTEGER NOT NULL DEFAULT 0,
                        is_active BOOLEAN NOT NULL DEFAULT 1,
                        created_at DATETIME NOT NULL
                    )
                    """
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_hotel_service_catalog_name_ar "
                    "ON hotel_service_catalog (name_ar)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_hotel_service_catalog_is_active "
                    "ON hotel_service_catalog (is_active)"
                )
            )
        # إفطار مشمول / تكلفة فندق
        names = set(insp.get_table_names())
        if "hotel_booking_services" in names:
            hbs_cols = {c["name"] for c in insp.get_columns("hotel_booking_services")}
            if "charged_to_guest" not in hbs_cols:
                conn.execute(
                    text(
                        "ALTER TABLE hotel_booking_services "
                        "ADD COLUMN charged_to_guest BOOLEAN NOT NULL DEFAULT 1"
                    )
                )
        if "products" in names:
            pcols = {c["name"] for c in insp.get_columns("products")}
            if "is_hotel_breakfast" not in pcols:
                conn.execute(
                    text(
                        "ALTER TABLE products "
                        "ADD COLUMN is_hotel_breakfast BOOLEAN NOT NULL DEFAULT 0"
                    )
                )

        # SEO entity columns
        names = set(insp.get_table_names())
        seo_col_defs = (
            ("seo_title", "VARCHAR(255)"),
            ("seo_description", "VARCHAR(500)"),
            ("seo_h1", "VARCHAR(255)"),
            ("seo_slug", "VARCHAR(255)"),
            ("seo_keywords", "VARCHAR(500)"),
            ("seo_schema_json", "TEXT"),
            ("seo_og_title", "VARCHAR(255)"),
            ("seo_og_description", "VARCHAR(500)"),
            ("seo_og_image", "VARCHAR(500)"),
            ("seo_indexable", "BOOLEAN DEFAULT 1"),
            ("seo_canonical_url", "VARCHAR(500)"),
            ("seo_updated_at", "DATETIME"),
        )
        for table in (
            "products",
            "product_categories",
            "hotel_rooms",
            "hotel_service_catalog",
        ):
            if table not in names:
                continue
            tcols = {c["name"] for c in insp.get_columns(table)}
            for col, ddl in seo_col_defs:
                if col not in tcols:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))
