#!/usr/bin/env python3
"""نقل بيانات SQLite إلى PostgreSQL (للانتقال إلى VPS).

الاستخدام المحلي قبل الرفع:
  1) أنشئ قاعدة PostgreSQL فارغة (محلياً أو على VPS).
  2) ضع DATABASE_URL في `.env` يشير إلى PostgreSQL.
  3) نفّذ:

     python tools/migrate_sqlite_to_postgres.py --source sqlite:///./pos.db

  أو مع عنوان Postgres صريح:

     python tools/migrate_sqlite_to_postgres.py \\
       --source sqlite:///./pos.db \\
       --target postgresql+psycopg2://pos_user:pass@127.0.0.1:5432/pos_db

  --force  يسمح بالكتابة فوق جداول غير فارغة في الهدف (يحذف البيانات أولاً).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _import_all_models() -> None:
    import modules.authz.models  # noqa: F401
    import modules.catalog.models  # noqa: F401
    import modules.customers.models  # noqa: F401
    import modules.dashboard_notify.models  # noqa: F401
    import modules.delivery.models  # noqa: F401
    import modules.hotel.models  # noqa: F401
    import modules.hr.models  # noqa: F401
    import modules.inventory.models  # noqa: F401
    import modules.kds.models  # noqa: F401
    import modules.messaging.models  # noqa: F401
    import modules.payments.models  # noqa: F401
    import modules.pos_shifts.models  # noqa: F401
    import modules.printing.models  # noqa: F401
    import modules.refunds.models  # noqa: F401
    import modules.sales.models  # noqa: F401
    import modules.settings.models  # noqa: F401


def _table_row_count(conn, table_name: str) -> int:
    from sqlalchemy import text

    return int(conn.execute(text(f"SELECT COUNT(*) FROM {table_name}")).scalar() or 0)


def _fix_postgres_sequences(engine) -> None:
    from sqlalchemy import inspect, text

    from infra.db import Base

    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if "id" not in table.c:
                continue
            if table.name not in insp.get_table_names():
                continue
            conn.execute(
                text(
                    f"SELECT setval("
                    f"pg_get_serial_sequence('{table.name}', 'id'), "
                    f"COALESCE((SELECT MAX(id) FROM {table.name}), 1), "
                    f"true)"
                )
            )


def migrate(source_url: str, target_url: str, *, force: bool = False) -> dict[str, int]:
    from sqlalchemy import create_engine, inspect, text

    from infra.database import is_postgresql_url, is_sqlite_url
    from infra.db import Base, make_engine
    from infra.sqlite_patch import patch_sqlite_schema

    if not is_sqlite_url(source_url):
        raise SystemExit("المصدر يجب أن يكون SQLite (sqlite:///...)")
    if not is_postgresql_url(target_url):
        raise SystemExit("الهدف يجب أن يكون PostgreSQL (postgresql+psycopg2://...)")

    _import_all_models()

    src_engine = make_engine(source_url)
    dst_engine = make_engine(target_url)

    dst_insp = inspect(dst_engine)
    existing = set(dst_insp.get_table_names())
    if existing:
        with dst_engine.connect() as conn:
            non_empty = [
                t for t in sorted(existing) if _table_row_count(conn, t) > 0
            ]
        if non_empty and not force:
            raise SystemExit(
                "قاعدة PostgreSQL الهدف تحتوي بيانات في: "
                + ", ".join(non_empty[:8])
                + (" …" if len(non_empty) > 8 else "")
                + ". استخدم --force للاستبدال."
            )

    print("إنشاء/تحديث مخطط PostgreSQL…")
    Base.metadata.drop_all(bind=dst_engine)
    Base.metadata.create_all(bind=dst_engine)

    # ترقيات SQLite القديمة على المصدر قبل النسخ
    patch_sqlite_schema(src_engine)

    counts: dict[str, int] = {}
    src_insp = inspect(src_engine)
    src_tables = set(src_insp.get_table_names())

    print("نسخ البيانات…")
    with dst_engine.begin() as dst_conn:
        # تعطيل قيود FK مؤقتاً أثناء النسخ
        dst_conn.execute(text("SET session_replication_role = replica"))

        for table in Base.metadata.sorted_tables:
            if table.name not in src_tables:
                counts[table.name] = 0
                continue
            with src_engine.connect() as src_conn:
                rows = src_conn.execute(table.select()).mappings().all()
            if not rows:
                counts[table.name] = 0
                continue
            dst_conn.execute(table.insert(), [dict(r) for r in rows])
            counts[table.name] = len(rows)
            print(f"  {table.name}: {len(rows)}")

        dst_conn.execute(text("SET session_replication_role = DEFAULT"))

    print("ضبط تسلسلات PostgreSQL…")
    _fix_postgres_sequences(dst_engine)

    total = sum(counts.values())
    print(f"تم. {total} صفاً في {len([c for c in counts.values() if c])} جدول.")
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="نقل SQLite → PostgreSQL")
    parser.add_argument(
        "--source",
        default="sqlite:///./pos.db",
        help="عنوان SQLite المصدر (افتراضي: sqlite:///./pos.db)",
    )
    parser.add_argument(
        "--target",
        default=None,
        help="عنوان PostgreSQL الهدف (افتراضي: DATABASE_URL من .env)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="السماح بالكتابة فوق بيانات موجودة في الهدف",
    )
    args = parser.parse_args()

    target = args.target
    if not target:
        from infra.config import get_settings

        target = get_settings().database_url

    migrate(args.source, target, force=args.force)


if __name__ == "__main__":
    main()
