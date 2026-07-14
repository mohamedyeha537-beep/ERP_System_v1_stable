#!/usr/bin/env python3
"""تشخيص أعمدة الكatalog الناقصة على MySQL — شغّله على VPS وانسخ المخرجات.

Usage:
  cd /home/posbaytak/pos_app && source .venv/bin/activate
  python tools/diagnose_mysql_catalog.py
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

from sqlalchemy import inspect

from infra.db import get_engine

# أعمدة يتوقعها الكود على origin/main (السيرفر الحي)
REQUIRED: dict[str, set[str]] = {
    "kitchen_departments": {
        "id",
        "name_ar",
        "venue",
        "sort_order",
        "is_active",
        "routing_mode",
        "routing_target",
    },
    "product_categories": {
        "color_hex",
        "delete_protected",
        "routing_mode",
        "routing_target",
    },
    "products": {
        "kitchen_department_id",
        "image_filename",
        "reference_unit_cost",
    },
}

OPTIONAL_NEW: dict[str, set[str]] = {
    "product_categories": {"show_in_pos"},
    "products": {
        "show_in_pos",
        "line_modifier_presets",
        "expiry_tracked",
        "price_linked_to_bom",
        "bom_markup_pct",
    },
}


def main() -> int:
    engine = get_engine()
    url = str(engine.url)
    if "mysql" not in url and "mariadb" not in url:
        print("WARN: DATABASE_URL is not MySQL — results are for local dev only.")
    print("DB:", url.split("@")[-1] if "@" in url else url)

    insp = inspect(engine)
    tables = set(insp.get_table_names())
    missing_required: list[str] = []
    missing_optional: list[str] = []

    for table, cols in REQUIRED.items():
        if table not in tables:
            missing_required.append(f"TABLE {table}")
            continue
        have = {c["name"] for c in insp.get_columns(table)}
        for col in sorted(cols - have):
            missing_required.append(f"{table}.{col}")

    for table, cols in OPTIONAL_NEW.items():
        if table not in tables:
            continue
        have = {c["name"] for c in insp.get_columns(table)}
        for col in sorted(cols - have):
            missing_optional.append(f"{table}.{col}")

    if missing_required:
        print("\nMISSING (causes 500 on /catalog/products/new):")
        for m in missing_required:
            print(" ", m)
    else:
        print("\nOK — all required columns/tables present for current live code.")

    if missing_optional:
        print("\nOPTIONAL (needed after git pull / new features):")
        for m in missing_optional:
            print(" ", m)

    if missing_required:
        print("\nFix: run deploy/mysql-catalog-columns.sql in phpMyAdmin")
        print("  or: python tools/fix_live_mysql_now.py")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
