#!/usr/bin/env python3
"""إصلاح MySQL لصفحة «صنف جديد» — يعمل على الكود القديم بدون git pull.

Usage:
  cd /home/posbaytak/pos_app && source .venv/bin/activate
  python tools/fix_live_mysql_now.py
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

from sqlalchemy import create_engine, inspect, text

from infra.config import get_settings

ALTERS: list[tuple[str, str, str]] = [
    ("product_categories", "color_hex", "VARCHAR(7) NOT NULL DEFAULT '#3b82f6'"),
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

REQUIRED_CHECK: dict[str, tuple[str, ...]] = {
    "product_categories": ("color_hex", "delete_protected", "routing_mode", "routing_target"),
    "products": ("kitchen_department_id", "image_filename"),
}


def main() -> int:
    url = get_settings().database_url
    if not url.startswith("mysql"):
        print("DATABASE_URL is not MySQL.")
        return 1

    engine = create_engine(url)
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    added: list[str] = []

    with engine.begin() as conn:
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
                        routing_target VARCHAR(255) NULL
                    )
                    """
                )
            )
            added.append("kitchen_departments (table)")

        for table, col, ddl in ALTERS:
            if table not in tables:
                continue
            have = {c["name"] for c in insp.get_columns(table)}
            if col not in have:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))
                added.append(f"{table}.{col}")

        have_sp = "show_in_pos" in {
            c["name"] for c in insp.get_columns("products")
        } if "products" in tables else False
        if have_sp:
            conn.execute(
                text("UPDATE products SET show_in_pos = 0 WHERE kind = 'STOCK_ONLY'")
            )

    insp = inspect(engine)
    still: list[str] = []
    if "kitchen_departments" not in insp.get_table_names():
        still.append("kitchen_departments (table)")
    for table, cols in REQUIRED_CHECK.items():
        if table not in insp.get_table_names():
            still.append(f"{table} (table)")
            continue
        have = {c["name"] for c in insp.get_columns(table)}
        for col in cols:
            if col not in have:
                still.append(f"{table}.{col}")

    print("Database:", url.split("@")[-1] if "@" in url else url)
    if added:
        print("Added:", ", ".join(added))
    else:
        print("No new columns (already present).")

    if still:
        print("ERROR — still missing:", ", ".join(still))
        print("Check MySQL user has ALTER TABLE permission.")
        return 1

    print("OK — restart: sudo systemctl restart pos")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
