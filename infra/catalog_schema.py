"""فحص وإصلاح أعمدة الكatalog الناقصة (MySQL/SQLite)."""

from __future__ import annotations

from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from infra.database import is_server_database_url, is_sqlite_url
from infra.db import get_engine

_EXPECTED: dict[str, set[str]] = {
    "product_categories": {
        "color_hex",
        "show_in_pos",
        "show_in_shop",
        "kitchen_section_id",
        "delete_protected",
        "routing_mode",
        "routing_target",
    },
    "products": {
        "show_in_pos",
        "show_in_shop",
        "kitchen_department_id",
        "kitchen_section_id",
        "sales_warehouse_id",
        "image_filename",
        "line_modifier_presets",
        "expiry_tracked",
        "expiry_production_date",
        "expiry_date",
        "expiry_warn_days",
        "price_linked_to_bom",
        "bom_markup_pct",
        "reference_unit_cost",
        "direct_purchase_enabled",
    },
    "bom_lines": {
        "packaging_only",
    },
    "hr_employees": {
        "department_id",
        "work_shift_id",
        "zk_emp_code",
        "is_pos_cashier",
        "is_pos_supervisor",
        "pos_pin_hash",
    },
    "hr_attendance": {
        "work_shift_id",
        "late_minutes",
        "early_leave_minutes",
        "overtime_minutes",
        "overtime_approval_status",
        "approved_overtime_minutes",
        "overtime_approved_by_id",
        "overtime_approved_at",
        "expected_work_hours",
    },
}

_EXPECTED_TABLES: set[str] = {
    "hr_departments",
    "hr_work_shifts",
    "hr_zk_processed_punches",
}


def _table_columns(engine: Engine, table: str) -> set[str]:
    insp = inspect(engine)
    if table not in insp.get_table_names():
        return set()
    return {c["name"] for c in insp.get_columns(table)}


def catalog_missing_columns(engine: Engine | None = None) -> dict[str, set[str]]:
    """الأعمدة المتوقعة وغير الموجودة — للعرض في الأدمن."""
    eng = engine or get_engine()
    missing: dict[str, set[str]] = {}
    insp = inspect(eng)
    tables = set(insp.get_table_names())
    for table in sorted(_EXPECTED_TABLES - tables):
        missing[table] = {"(table missing)"}
    for table, expected in _EXPECTED.items():
        have = _table_columns(eng, table)
        if not have:
            continue
        gap = expected - have
        if gap:
            missing[table] = gap
    return missing


def repair_catalog_schema(engine: Engine | None = None) -> list[str]:
    """يُصلح المخطط ويُرجع قائمة الأعمدة التي أُضيفت."""
    eng = engine or get_engine()
    url = str(eng.url)
    if is_sqlite_url(url):
        from infra.sqlite_patch import patch_sqlite_schema

        patch_sqlite_schema(eng)
    elif is_server_database_url(url):
        from infra.server_schema_patch import patch_server_schema

        patch_server_schema(eng)
    else:
        return []
    before = catalog_missing_columns(eng)
    after = catalog_missing_columns(eng)
    added: list[str] = []
    for table, cols in before.items():
        for col in cols:
            if col not in after.get(table, set()):
                added.append(f"{table}.{col}")
    return added
