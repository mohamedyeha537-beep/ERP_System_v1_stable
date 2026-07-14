#!/usr/bin/env python3
"""إصلاح أعمدة الكatalog الناقصة على MySQL/PostgreSQL (شغّله على VPS).

Usage:
  cd /var/www/pos && source .venv/bin/activate
  python tools/fix_mysql_catalog_schema.py
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

import modules.authz.models  # noqa: F401
import modules.catalog.models  # noqa: F401
import modules.settings.models  # noqa: F401

from infra.db import get_engine
from infra.schema_bootstrap import bootstrap_schema
from infra.server_schema_patch import patch_server_schema


def main() -> int:
    engine = get_engine()
    url = str(engine.url)
    print(f"Database: {url.split('@')[-1] if '@' in url else url}")

    from sqlalchemy import inspect

    def missing(table: str, expected: set[str]) -> set[str]:
        insp = inspect(engine)
        if table not in insp.get_table_names():
            return expected
        have = {c["name"] for c in insp.get_columns(table)}
        return expected - have

    before_cat = missing(
        "product_categories",
        {"show_in_pos", "kitchen_section_id", "delete_protected", "routing_mode", "routing_target"},
    )
    before_prod = missing(
        "products",
        {
            "show_in_pos",
            "kitchen_department_id",
            "image_filename",
            "line_modifier_presets",
            "expiry_tracked",
            "expiry_production_date",
            "expiry_date",
            "expiry_warn_days",
            "price_linked_to_bom",
            "bom_markup_pct",
            "reference_unit_cost",
        },
    )
    if before_cat or before_prod:
        print("Missing BEFORE patch:")
        if before_cat:
            print(f"  product_categories: {sorted(before_cat)}")
        if before_prod:
            print(f"  products: {sorted(before_prod)}")
    else:
        print("All expected catalog columns present (before patch).")

    bootstrap_schema(engine)
    patch_server_schema(engine)

    after_cat = missing("product_categories", {"show_in_pos"})
    after_prod = missing("products", {"show_in_pos", "expiry_tracked", "price_linked_to_bom"})
    if after_cat or after_prod:
        print("ERROR — still missing AFTER patch:")
        if after_cat:
            print(f"  product_categories: {sorted(after_cat)}")
        if after_prod:
            print(f"  products: {sorted(after_prod)}")
        print("Try manually: mysql ... < deploy/mysql-catalog-columns.sql")
        return 1

    print("OK — catalog schema complete. Restart: sudo systemctl restart pos")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
