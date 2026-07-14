#!/usr/bin/env python3
"""نقل بيانات SQLite (pos.db) إلى MySQL/MariaDB.

المصدر: ملف SQLite (افتراضي: pos.db في جذر المشروع).
الهدف: DATABASE_URL من .env أو --target (يجب أن يكون mysql+pymysql://...).

أمثلة:
  # معاينة بدون كتابة
  python tools/migrate_sqlite_to_mysql.py --dry-run

  # تنفيذ النقل (يُنشئ الجداول على MySQL ثم ينقل البيانات)
  python tools/migrate_sqlite_to_mysql.py --force

  # مسارات صريحة
  python tools/migrate_sqlite_to_mysql.py \\
    --source ./pos.db \\
    --target "mysql+pymysql://user:pass@127.0.0.1:3306/pos_db?charset=utf8mb4" \\
    --force

ملاحظات:
  - لا يحذف ملف SQLite.
  - يحافظ على قيم id كما هي.
  - يقارن عدد السجلات بعد النقل.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@dataclass
class TableReport:
    name: str
    source_count: int
    target_count: int | None = None

    @property
    def migrated(self) -> int:
        return self.source_count if self.target_count is None else self.target_count

    @property
    def ok(self) -> bool:
        if self.target_count is None:
            return True
        return self.source_count == self.target_count


def _import_all_models() -> None:
    from infra.model_registry import import_all_models

    import_all_models()


def _default_sqlite_path() -> Path:
    return ROOT / "pos.db"


def _sqlite_url_from_path(path: Path) -> str:
    path = path.resolve()
    if not path.exists():
        raise SystemExit(f"ملف SQLite غير موجود: {path}")
    # مسار مطلق لـ SQLAlchemy على Windows
    return f"sqlite:///{path.as_posix()}"


def _table_row_count(conn, table_name: str) -> int:
    from sqlalchemy import text

    return int(conn.execute(text(f"SELECT COUNT(*) FROM {table_name}")).scalar() or 0)


def _collect_source_counts(engine, table_names: list[str]) -> dict[str, int]:
    from sqlalchemy import inspect

    insp = inspect(engine)
    existing = set(insp.get_table_names())
    counts: dict[str, int] = {}
    with engine.connect() as conn:
        for name in table_names:
            if name not in existing:
                counts[name] = 0
                continue
            counts[name] = _table_row_count(conn, name)
    return counts


def _fix_mysql_auto_increment(engine, table_names: list[str]) -> None:
    from sqlalchemy import inspect, text

    from infra.db import Base

    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in table_names or "id" not in table.c:
                continue
            if table.name not in insp.get_table_names():
                continue
            max_id = conn.execute(
                text(f"SELECT COALESCE(MAX(id), 0) FROM {table.name}")
            ).scalar()
            if int(max_id or 0) > 0:
                conn.execute(
                    text(
                        f"ALTER TABLE {table.name} AUTO_INCREMENT = {int(max_id) + 1}"
                    )
                )


def _print_report(reports: list[TableReport], *, dry_run: bool) -> None:
    title = "تقرير معاينة (dry-run)" if dry_run else "تقرير النقل"
    print()
    print(title)
    print("-" * 62)
    print(f"{'الجدول':<32} {'SQLite':>8} {'MySQL':>8} {'الحالة':>8}")
    print("-" * 62)
    src_total = 0
    dst_total = 0
    mismatches: list[str] = []
    for row in reports:
        src_total += row.source_count
        dst_col = "—" if row.target_count is None else str(row.target_count)
        if row.target_count is not None:
            dst_total += row.target_count
        if row.target_count is None:
            status = "معاينة"
        elif row.ok:
            status = "OK"
        else:
            status = "FAIL"
            mismatches.append(row.name)
        if row.source_count or row.target_count:
            print(f"{row.name:<32} {row.source_count:>8} {dst_col:>8} {status:>8}")
    print("-" * 62)
    if dry_run:
        print(f"{'الإجمالي (سيُنقل)':<32} {src_total:>8}")
    else:
        print(f"{'الإجمالي':<32} {src_total:>8} {dst_total:>8}")
    print()
    if mismatches:
        print("⚠ جداول بعدد سجلات مختلف:", ", ".join(mismatches))
        raise SystemExit(1)
    if dry_run:
        print("✓ المعاينة اكتملت — لا تغييرات على MySQL.")
    else:
        print("✓ اكتمل النقل والمقارنة بنجاح. ملف SQLite لم يُمس.")


def migrate(
    source_url: str,
    target_url: str,
    *,
    force: bool = False,
    dry_run: bool = False,
) -> list[TableReport]:
    from sqlalchemy import inspect, text

    from infra.database import is_mysql_url, is_sqlite_url
    from infra.db import Base, make_engine
    from infra.schema_bootstrap import bootstrap_schema
    from infra.sqlite_patch import patch_sqlite_schema

    if not is_sqlite_url(source_url):
        raise SystemExit("المصدر يجب أن يكون SQLite (sqlite:///...)")
    if not is_mysql_url(target_url):
        raise SystemExit(
            "الهدف يجب أن يكون MySQL/MariaDB "
            "(mysql+pymysql://user:pass@host/db?charset=utf8mb4)"
        )

    _import_all_models()

    src_engine = make_engine(source_url)
    patch_sqlite_schema(src_engine)

    ordered_tables = [t.name for t in Base.metadata.sorted_tables]
    source_counts = _collect_source_counts(src_engine, ordered_tables)

    reports = [
        TableReport(name=name, source_count=source_counts.get(name, 0))
        for name in ordered_tables
    ]

    if dry_run:
        dst_engine = make_engine(target_url)
        dst_insp = inspect(dst_engine)
        dst_existing = set(dst_insp.get_table_names())
        if dst_existing:
            with dst_engine.connect() as conn:
                for row in reports:
                    if row.name in dst_existing:
                        row.target_count = _table_row_count(conn, row.name)
                    else:
                        row.target_count = 0
        print("وضع dry-run: لن تُجرى أي كتابة على MySQL.")
        _print_report(reports, dry_run=True)
        return reports

    dst_engine = make_engine(target_url)
    dst_insp = inspect(dst_engine)
    existing = set(dst_insp.get_table_names())
    if existing:
        with dst_engine.connect() as conn:
            non_empty = [
                t
                for t in sorted(existing)
                if _table_row_count(conn, t) > 0
            ]
        if non_empty and not force:
            raise SystemExit(
                "قاعدة MySQL الهدف تحتوي بيانات في: "
                + ", ".join(non_empty[:8])
                + (" …" if len(non_empty) > 8 else "")
                + ". استخدم --force للاستبدال."
            )

    print("إنشاء مخطط MySQL (migrations / create_all)…")
    if force:
        Base.metadata.drop_all(bind=dst_engine)
    bootstrap_schema(dst_engine)

    src_insp = inspect(src_engine)
    src_tables = set(src_insp.get_table_names())

    print("نسخ البيانات (مع الحفاظ على id)…")
    with dst_engine.begin() as dst_conn:
        dst_conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        for table in Base.metadata.sorted_tables:
            row = next(r for r in reports if r.name == table.name)
            if table.name not in src_tables:
                row.target_count = 0
                continue
            with src_engine.connect() as src_conn:
                rows = src_conn.execute(table.select()).mappings().all()
            if not rows:
                row.target_count = 0
                continue
            dst_conn.execute(table.insert(), [dict(r) for r in rows])
            row.target_count = len(rows)
            print(f"  {table.name}: {len(rows)}")
        dst_conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))

    print("ضبط AUTO_INCREMENT…")
    _fix_mysql_auto_increment(dst_engine, ordered_tables)

    # مقارنة نهائية
    with dst_engine.connect() as conn:
        for row in reports:
            row.target_count = _table_row_count(conn, row.name)

    _print_report(reports, dry_run=False)
    return reports


def main() -> None:
    parser = argparse.ArgumentParser(
        description="نقل SQLite (pos.db) → MySQL/MariaDB مع مقارنة السجلات"
    )
    parser.add_argument(
        "--source",
        default=None,
        help="مسار ملف SQLite أو sqlite:/// URL (افتراضي: ./pos.db)",
    )
    parser.add_argument(
        "--target",
        default=None,
        help="عنوان MySQL الهدف (افتراضي: DATABASE_URL من .env)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="السماح بالكتابة فوق بيانات MySQL موجودة (يُعاد إنشاء الجداول)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="معاينة فقط: عدّ السجلات بدون نقل أو تعديل MySQL",
    )
    args = parser.parse_args()

    if args.source:
        src = args.source.strip()
        if src.startswith("sqlite:"):
            source_url = src
        else:
            source_url = _sqlite_url_from_path(Path(src))
    else:
        source_url = _sqlite_url_from_path(_default_sqlite_path())

    target = args.target
    if not target:
        from infra.config import get_settings

        target = get_settings().database_url

    if args.dry_run and args.force:
        print("تحذير: --dry-run يتجاهل --force (لا كتابة على MySQL).")

    migrate(source_url, target, force=args.force, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
