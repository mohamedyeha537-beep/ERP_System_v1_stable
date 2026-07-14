#!/usr/bin/env python3
"""تهيئة واستيراد نسخة MySQL من السيرفر الفعلي إلى XAMPP محلياً.

يُصلح فروقات dump ChatGPT (physical_status بأحرف صغيرة، أعمدة booking_id الناقصة)
ثم يستورد البيانات إلى pos_db ويطبّق ترقيات التطبيق.

أمثلة:
  python tools/import_live_mysql_backup.py --prepare-only
  python tools/import_live_mysql_backup.py --import
  python tools/import_live_mysql_backup.py --all
  python tools/import_live_mysql_backup.py --all --source "C:\\path\\to\\backup.sql"
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_SOURCE = Path(r"C:\Users\PIXEL\OneDrive\Desktop\سيف\pos-backup-20260615-231016.sql")
PREPARED_REL = Path("deploy/backups/prepared-live-backup-20260615.sql")
APP_DB = "pos_db"

# مسارات mysql.exe الشائعة على Windows (XAMPP)
MYSQL_BIN_CANDIDATES = [
    Path(r"C:\xampp\mysql\bin\mysql.exe"),
    Path(r"C:\Program Files\MySQL\MySQL Server 8.0\bin\mysql.exe"),
    Path(r"C:\Program Files\MariaDB 10.11\bin\mysql.exe"),
]

PHYSICAL_STATUS_FIXES = {
    "'available'": "'AVAILABLE'",
    "'reserved'": "'RESERVED'",
    "'occupied'": "'OCCUPIED'",
    "'dirty'": "'DIRTY'",
    "'maintenance'": "'MAINTENANCE'",
    "'out_of_service'": "'OUT_OF_SERVICE'",
    "'blocked'": "'BLOCKED'",
}


def _find_mysql_bin() -> Path | None:
    found = shutil.which("mysql")
    if found:
        return Path(found)
    for candidate in MYSQL_BIN_CANDIDATES:
        if candidate.is_file():
            return candidate
    return None


def prepare_sql(source: Path, dest: Path) -> Path:
    """تنظيف dump السيرفر ليتوافق مع التطبيق الحالي."""
    if not source.is_file():
        raise SystemExit(f"ملف النسخة غير موجود: {source}")

    raw = source.read_text(encoding="utf-8")
    out = raw

    # توضيح في الترويسة
    out = out.replace(
        "Database: posbaytak_pos_system",
        "Database: pos_db (prepared for local import)",
        1,
    )
    out = out.replace(
        "Dumping routines for database 'posbaytak_pos_system'",
        "Dumping routines for database 'pos_db'",
        1,
    )

    # DEFAULT في CREATE TABLE
    out = out.replace(
        "`physical_status` varchar(50) NOT NULL DEFAULT 'available'",
        "`physical_status` varchar(50) NOT NULL DEFAULT 'AVAILABLE'",
    )

    # XAMPP/MariaDB لا يدعم utf8mb4_0900_ai_ci (MySQL 8 على السيرفر)
    out = out.replace("utf8mb4_0900_ai_ci", "utf8mb4_unicode_ci")

    # INSERT hotel_rooms — آخر عمود physical_status
    for old, new in PHYSICAL_STATUS_FIXES.items():
        out = out.replace(f",{old})", f",{new})")

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(out, encoding="utf-8")

    src_kb = source.stat().st_size // 1024
    print(f"✓ تمت التهيئة: {dest}")
    print(f"  المصدر: {source} ({src_kb} KB)")
    print(f"  الإصلاحات: physical_status → UPPERCASE، collation → utf8mb4_unicode_ci")
    return dest


def _connect_root(host: str, port: int, user: str, password: str):
    import pymysql

    return pymysql.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        charset="utf8mb4",
        connect_timeout=10,
    )


def wait_mysql(host: str, port: int, user: str, password: str, timeout: int = 30) -> None:
    deadline = time.time() + timeout
    last_err = ""
    while time.time() < deadline:
        try:
            conn = _connect_root(host, port, user, password)
            conn.close()
            print(f"✓ MySQL يعمل على {host}:{port}")
            return
        except Exception as exc:
            last_err = str(exc)
            time.sleep(2)
    raise SystemExit(
        "لم يتصل MySQL.\n"
        "• شغّل MySQL من XAMPP Control Panel.\n"
        f"آخر خطأ: {last_err}"
    )


def recreate_database(
    *,
    host: str,
    port: int,
    root_user: str,
    root_password: str,
    app_user: str,
) -> None:
    conn = _connect_root(host, port, root_user, root_password)
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS `{APP_DB}`")
            cur.execute(
                f"CREATE DATABASE `{APP_DB}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
            cur.execute(f"GRANT ALL PRIVILEGES ON `{APP_DB}`.* TO %s@'localhost'", (app_user,))
            cur.execute("FLUSH PRIVILEGES")
        conn.commit()
        print(f"✓ أُعيد إنشاء قاعدة `{APP_DB}` (فارغة)")
    finally:
        conn.close()


def import_sql_file(
    sql_path: Path,
    *,
    host: str,
    port: int,
    root_user: str,
    root_password: str,
) -> None:
    mysql_bin = _find_mysql_bin()
    if mysql_bin:
        cmd = [
            str(mysql_bin),
            f"-h{host}",
            f"-P{port}",
            f"-u{root_user}",
            APP_DB,
        ]
        if root_password:
            cmd.insert(-1, f"-p{root_password}")
        print("$", " ".join(cmd[:-1] + ["-p***", APP_DB] if root_password else cmd))
        with sql_path.open("r", encoding="utf-8") as fh:
            proc = subprocess.run(cmd, stdin=fh, capture_output=True, text=True)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            raise SystemExit(f"فشل استيراد mysql:\n{err[:2000]}")
        print("✓ اكتمل الاستيراد عبر mysql.exe")
        return

    # fallback: pymysql (بطيء لكن يعمل بدون mysql CLI)
    print("⚠ mysql.exe غير موجود — استيراد عبر pymysql…")
    import pymysql

    conn = pymysql.connect(
        host=host,
        port=port,
        user=root_user,
        password=root_password,
        database=APP_DB,
        charset="utf8mb4",
        connect_timeout=30,
    )
    sql = sql_path.read_text(encoding="utf-8")
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
        print("✓ اكتمل الاستيراد عبر pymysql")
    finally:
        conn.close()


def patch_and_verify() -> None:
    from infra.db import get_engine, reset_engine
    from infra.schema_bootstrap import bootstrap_schema, reset_schema_patch_flag

    reset_engine()
    reset_schema_patch_flag()
    engine = get_engine()
    bootstrap_schema(engine)

    from sqlalchemy import inspect, text

    insp = inspect(engine)
    tables = insp.get_table_names()
    print(f"✓ الجداول بعد الترقية: {len(tables)}")

    with engine.connect() as conn:
        counts = {}
        for table in (
            "products",
            "customers",
            "sales",
            "hotel_rooms",
            "hotel_bookings",
            "permissions",
            "users",
        ):
            if table in tables:
                counts[table] = conn.execute(text(f"SELECT COUNT(*) FROM `{table}`")).scalar()

        if "hotel_rooms" in tables:
            bad = conn.execute(
                text(
                    "SELECT COUNT(*) FROM hotel_rooms "
                    "WHERE physical_status NOT IN "
                    "('AVAILABLE','RESERVED','OCCUPIED','DIRTY','MAINTENANCE','OUT_OF_SERVICE','BLOCKED')"
                )
            ).scalar()
            if bad:
                print(f"⚠ غرف بحالة physical_status غير معروفة: {bad}")

        sales_cols = {c["name"] for c in insp.get_columns("sales")} if "sales" in tables else set()
        if "booking_id" not in sales_cols:
            print("⚠ sales.booking_id ما زال ناقصاً")
        else:
            print("✓ sales.booking_id موجود")

    print("عدد السجلات:")
    for t, n in sorted(counts.items()):
        print(f"  {t}: {n}")


def main() -> None:
    parser = argparse.ArgumentParser(description="تهيئة واستيراد نسخة السيرفر الفعلي")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="ملف .sql الأصلي")
    parser.add_argument(
        "--dest",
        type=Path,
        default=ROOT / PREPARED_REL,
        help="ملف SQL المُهيّأ",
    )
    parser.add_argument("--prepare-only", action="store_true", help="تهيئة الملف فقط")
    parser.add_argument("--import", dest="do_import", action="store_true", help="استيراد فقط")
    parser.add_argument("--all", action="store_true", help="تهيئة + استيراد + ترقية + تحقق")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3306)
    parser.add_argument("--root-user", default="root")
    parser.add_argument("--root-password", default="")
    parser.add_argument("--app-user", default="pos_user")
    parser.add_argument(
        "--keep-db",
        action="store_true",
        help="لا تحذف pos_db (استيراد فوق الجداول الموجودة — غير موصى به)",
    )
    args = parser.parse_args()

    if not any([args.prepare_only, args.do_import, args.all]):
        parser.print_help()
        return

    prepared = prepare_sql(args.source, args.dest)

    if args.prepare_only:
        print()
        print("للاستيراد محلياً:")
        print(f"  python tools/import_live_mysql_backup.py --import --dest \"{prepared}\"")
        return

    wait_mysql(args.host, args.port, args.root_user, args.root_password)

    if not args.keep_db:
        recreate_database(
            host=args.host,
            port=args.port,
            root_user=args.root_user,
            root_password=args.root_password,
            app_user=args.app_user,
        )

    import_sql_file(
        prepared,
        host=args.host,
        port=args.port,
        root_user=args.root_user,
        root_password=args.root_password,
    )

    patch_and_verify()

    print()
    print("✓ بيانات السيرفر الفعلي جاهزة محلياً في pos_db")
    print("DATABASE_URL=mysql+pymysql://pos_user:pos_local_dev@127.0.0.1:3306/pos_db?charset=utf8mb4")
    print("الخطوة التالية: start-server.bat")


if __name__ == "__main__":
    main()
