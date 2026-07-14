# إصلاح فوري لخطأ 500 في «صنف جديد» على pos.baytak.ly

## ⚡ أسرع حل — phpMyAdmin

1. Hostinger → **phpMyAdmin** → قاعدة POS
2. تبويب **SQL**
3. افتح الملف **`deploy/fix-product-new-500.sql`** من المشروع والصق **كل** محتواه
4. **Go** — تجاهل «Duplicate column»
5. VPS: `sudo systemctl restart pos`

---

## السبب (مؤكّد باختبار runtime)
أعمدة أو جداول ناقصة في MySQL. **الكود الحالي على السيرفر (`origin/main`)** يستعلم عن:
- `product_categories.delete_protected`, `routing_mode`, `routing_target`
- جدول `kitchen_departments`
- `products.kitchen_department_id`, `image_filename`

> تحقق: `/admin/schema-fix` يعطي **404** على السيرفر الحي → الكود الجديد غير منشور بعد.

---

## الحل الأسرع — SSH (بدون git pull)

**الخطوة 1 — تشخيص** (انسخ المخرجات إن فشل الإصلاح):

```bash
cd /home/posbaytak/pos_app && source .venv/bin/activate && python tools/diagnose_mysql_catalog.py
```

**الخطوة 2 — إصلاح** (انسخ الملف `tools/fix_live_mysql_now.py` للسيرفر أو الصق):

```bash
cd /home/posbaytak/pos_app && source .venv/bin/activate && python tools/fix_live_mysql_now.py && sudo systemctl restart pos
```

**بديل — لصق Python مباشرة** (لا يحتاج ملفات جديدة):

```bash
su -s /bin/bash -c 'cd /home/posbaytak/pos_app && source .venv/bin/activate && python - <<PY
from sqlalchemy import create_engine, inspect, text
from infra.config import get_settings

url = get_settings().database_url
if not url.startswith("mysql"):
    raise SystemExit("DATABASE_URL is not MySQL")

engine = create_engine(url)
insp = inspect(engine)
tables = set(insp.get_table_names())

def has_col(t, c):
    return c in {x["name"] for x in insp.get_columns(t)}

with engine.begin() as conn:
    if "kitchen_departments" not in tables:
        conn.execute(text("""
            CREATE TABLE kitchen_departments (
                id INT AUTO_INCREMENT PRIMARY KEY,
                name_ar VARCHAR(120) NOT NULL,
                venue VARCHAR(20) NOT NULL DEFAULT \"KITCHEN\",
                sort_order INT NOT NULL DEFAULT 0,
                is_active TINYINT(1) NOT NULL DEFAULT 1,
                routing_mode VARCHAR(20) NOT NULL DEFAULT \"SCREEN\",
                routing_target VARCHAR(255) NULL
            )
        """))
        print("Created kitchen_departments")

    alters = [
        ("product_categories", "delete_protected", "TINYINT(1) NOT NULL DEFAULT 0"),
        ("product_categories", "routing_mode", "VARCHAR(20) NOT NULL DEFAULT 'NONE'"),
        ("product_categories", "routing_target", "VARCHAR(255) NULL"),
        ("product_categories", "show_in_pos", "TINYINT(1) NOT NULL DEFAULT 1"),
        ("products", "show_in_pos", "TINYINT(1) NOT NULL DEFAULT 1"),
        ("products", "kitchen_department_id", "INT NULL"),
        ("products", "image_filename", "VARCHAR(255) NULL"),
        ("products", "line_modifier_presets", "TEXT NULL"),
        ("products", "expiry_tracked", "TINYINT(1) NOT NULL DEFAULT 0"),
        ("products", "expiry_production_date", "DATE NULL"),
        ("products", "expiry_date", "DATE NULL"),
        ("products", "expiry_warn_days", "INT NOT NULL DEFAULT 7"),
        ("products", "price_linked_to_bom", "TINYINT(1) NOT NULL DEFAULT 0"),
        ("products", "bom_markup_pct", "DECIMAL(8,2) NULL"),
        ("products", "reference_unit_cost", "DECIMAL(14,3) NULL"),
    ]
    for table, col, ddl in alters:
        if table not in tables and table != "kitchen_departments":
            continue
        if not has_col(table, col):
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))
            print(f"Added {table}.{col}")

    if has_col("products", "show_in_pos"):
        conn.execute(text("UPDATE products SET show_in_pos = 0 WHERE kind = \"STOCK_ONLY\""))

print("Done. Restart pos service.")
PY
sudo systemctl restart pos'
```

> إذا المسار `/var/www/pos` بدّل `cd /home/posbaytak/pos_app`.

---

## phpMyAdmin (بدون SSH)

نفّذ من `deploy/mysql-catalog-columns.sql` — تجاهل «Duplicate column»:

```sql
CREATE TABLE IF NOT EXISTS kitchen_departments (
  id INT AUTO_INCREMENT PRIMARY KEY,
  name_ar VARCHAR(120) NOT NULL,
  venue VARCHAR(20) NOT NULL DEFAULT 'KITCHEN',
  sort_order INT NOT NULL DEFAULT 0,
  is_active TINYINT(1) NOT NULL DEFAULT 1,
  routing_mode VARCHAR(20) NOT NULL DEFAULT 'SCREEN',
  routing_target VARCHAR(255) NULL
);

ALTER TABLE product_categories ADD COLUMN delete_protected TINYINT(1) NOT NULL DEFAULT 0;
ALTER TABLE product_categories ADD COLUMN routing_mode VARCHAR(20) NOT NULL DEFAULT 'NONE';
ALTER TABLE product_categories ADD COLUMN routing_target VARCHAR(255) NULL;
ALTER TABLE product_categories ADD COLUMN show_in_pos TINYINT(1) NOT NULL DEFAULT 1;
ALTER TABLE products ADD COLUMN show_in_pos TINYINT(1) NOT NULL DEFAULT 1;
ALTER TABLE products ADD COLUMN kitchen_department_id INT NULL;
ALTER TABLE products ADD COLUMN image_filename VARCHAR(255) NULL;
ALTER TABLE products ADD COLUMN line_modifier_presets TEXT NULL;
ALTER TABLE products ADD COLUMN expiry_tracked TINYINT(1) NOT NULL DEFAULT 0;
ALTER TABLE products ADD COLUMN expiry_production_date DATE NULL;
ALTER TABLE products ADD COLUMN expiry_date DATE NULL;
ALTER TABLE products ADD COLUMN expiry_warn_days INT NOT NULL DEFAULT 7;
ALTER TABLE products ADD COLUMN price_linked_to_bom TINYINT(1) NOT NULL DEFAULT 0;
ALTER TABLE products ADD COLUMN bom_markup_pct DECIMAL(8,2) NULL;
ALTER TABLE products ADD COLUMN reference_unit_cost DECIMAL(14,3) NULL;
UPDATE products SET show_in_pos = 0 WHERE kind = 'STOCK_ONLY';
```

ثم: `sudo systemctl restart pos`

---

## بعد git pull (كود جديد)

1. `git pull && sudo systemctl restart pos`
2. افتح `/admin/schema-fix` → **«إصلاح الآن»**
3. جرّب `/catalog/products/new`

---

## التحقق
- `/catalog/products/new` يفتح بدون Internal Server Error
- يمكن حفظ صنف مخزني (STOCK_ONLY)
