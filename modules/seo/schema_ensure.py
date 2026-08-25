"""إضافة أعمدة SEO للكيانات الموجودة (SQLite / MySQL / Postgres)."""
from __future__ import annotations

import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

log = logging.getLogger(__name__)

_SEO_COLUMNS: list[tuple[str, str, str]] = [
    # (column, sqlite_ddl, server_ddl roughly same)
    ("seo_title", "VARCHAR(255)", "VARCHAR(255) NULL"),
    ("seo_description", "VARCHAR(500)", "VARCHAR(500) NULL"),
    ("seo_h1", "VARCHAR(255)", "VARCHAR(255) NULL"),
    ("seo_slug", "VARCHAR(255)", "VARCHAR(255) NULL"),
    ("seo_keywords", "VARCHAR(500)", "VARCHAR(500) NULL"),
    ("seo_schema_json", "TEXT", "TEXT NULL"),
    ("seo_og_title", "VARCHAR(255)", "VARCHAR(255) NULL"),
    ("seo_og_description", "VARCHAR(500)", "VARCHAR(500) NULL"),
    ("seo_og_image", "VARCHAR(500)", "VARCHAR(500) NULL"),
    ("seo_indexable", "BOOLEAN DEFAULT 1", "BOOLEAN NULL"),
    ("seo_canonical_url", "VARCHAR(500)", "VARCHAR(500) NULL"),
    ("seo_updated_at", "DATETIME", "DATETIME NULL"),
]

_TABLES = (
    "products",
    "product_categories",
    "hotel_rooms",
    "hotel_service_catalog",
)


def ensure_seo_entity_columns(engine: Engine) -> list[str]:
    added: list[str] = []
    dialect = engine.dialect.name
    insp = inspect(engine)
    names = set(insp.get_table_names())
    with engine.begin() as conn:
        for table in _TABLES:
            if table not in names:
                continue
            cols = {c["name"] for c in insp.get_columns(table)}
            for col, sqlite_ddl, server_ddl in _SEO_COLUMNS:
                if col in cols:
                    continue
                ddl = sqlite_ddl if dialect == "sqlite" else server_ddl
                if col == "seo_indexable" and dialect != "sqlite":
                    if dialect == "mysql":
                        ddl = "TINYINT(1) NOT NULL DEFAULT 1"
                    else:
                        ddl = "BOOLEAN NOT NULL DEFAULT TRUE"
                if col == "seo_updated_at" and dialect == "postgresql":
                    ddl = "TIMESTAMPTZ NULL"
                try:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))
                    added.append(f"{table}.{col}")
                    cols.add(col)
                except Exception as exc:  # noqa: BLE001
                    log.warning("seo column %s.%s: %s", table, col, exc)
    if added:
        log.info("seo entity columns added: %s", ", ".join(added))
    return added
