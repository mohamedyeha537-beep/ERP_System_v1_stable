#!/usr/bin/env python3
"""إعداد MySQL عبر XAMPP (بدون Docker) + نقل SQLite → MySQL.

قبل التشغيل:
  1. افتح XAMPP Control Panel
  2. اضغط Start بجانب MySQL (يجب أن يصبح أخضر)

أمثلة:
  python tools/setup_mysql_xampp.py --all
  python tools/setup_mysql_xampp.py --all --root-password ""
  python tools/setup_mysql_xampp.py --all --root-password "secret"
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from urllib.parse import quote_plus

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# مطابق لإعداد VPS — يُنشأ تلقائياً في XAMPP
APP_DB = "pos_db"
APP_USER = "pos_user"
APP_PASS = "pos_local_dev"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 3306


def _sqlalchemy_url(user: str, password: str, host: str, port: int, db: str) -> str:
    pw = quote_plus(password) if password else ""
    auth = f"{quote_plus(user)}:{pw}@" if password else f"{quote_plus(user)}@"
    return f"mysql+pymysql://{auth}{host}:{port}/{db}?charset=utf8mb4"


def _connect_root(
    *,
    host: str,
    port: int,
    root_user: str,
    root_password: str,
    database: str | None = None,
):
    import pymysql

    return pymysql.connect(
        host=host,
        port=port,
        user=root_user,
        password=root_password,
        database=database,
        charset="utf8mb4",
        connect_timeout=5,
    )


def wait_xampp_mysql(
    *,
    host: str,
    port: int,
    root_user: str,
    root_password: str,
    timeout: int = 30,
) -> None:
    print(f"انتظار MySQL على {host}:{port} (XAMPP)…")
    deadline = time.time() + timeout
    last_err = ""
    while time.time() < deadline:
        try:
            conn = _connect_root(
                host=host,
                port=port,
                root_user=root_user,
                root_password=root_password,
            )
            conn.close()
            print("✓ MySQL يعمل (XAMPP)")
            return
        except Exception as exc:
            last_err = str(exc)
            time.sleep(2)
    raise SystemExit(
        "لم يتصل MySQL.\n"
        "• افتح XAMPP Control Panel → Start بجانب MySQL.\n"
        "• تأكد أن المنفذ 3306 غير مستخدم من برنامج آخر.\n"
        "• إذا غيّرت كلمة root في phpMyAdmin استخدم: --root-password \"...\"\n"
        f"آخر خطأ: {last_err}\n"
        "راجع: deploy/mysql-xampp-windows.md"
    )


def bootstrap_xampp_database(
    *,
    host: str,
    port: int,
    root_user: str,
    root_password: str,
    use_app_user: bool,
) -> str:
    """ينشئ pos_db (+ pos_user اختياري) ويرجع DATABASE_URL."""
    conn = _connect_root(
        host=host,
        port=port,
        root_user=root_user,
        root_password=root_password,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{APP_DB}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
            if use_app_user:
                for app_host in ("localhost", "127.0.0.1"):
                    cur.execute(
                        f"CREATE USER IF NOT EXISTS '{APP_USER}'@'{app_host}' "
                        f"IDENTIFIED BY '{APP_PASS}'"
                    )
                    cur.execute(
                        f"ALTER USER '{APP_USER}'@'{app_host}' "
                        f"IDENTIFIED BY '{APP_PASS}'"
                    )
                    cur.execute(
                        f"GRANT ALL PRIVILEGES ON `{APP_DB}`.* TO "
                        f"'{APP_USER}'@'{app_host}'"
                    )
                cur.execute("FLUSH PRIVILEGES")
                url = _sqlalchemy_url(APP_USER, APP_PASS, host, port, APP_DB)
                print(f"✓ قاعدة {APP_DB} + مستخدم {APP_USER}")
            else:
                url = _sqlalchemy_url(root_user, root_password, host, port, APP_DB)
                print(f"✓ قاعدة {APP_DB} (اتصال root)")
        conn.commit()
    finally:
        conn.close()
    return url


def main() -> None:
    from tools.setup_mysql_local import migrate_sqlite, verify_app, write_env_mysql_url

    parser = argparse.ArgumentParser(description="إعداد MySQL عبر XAMPP + نقل SQLite")
    parser.add_argument("--all", action="store_true", help="إعداد + .env + نقل + تحقق")
    parser.add_argument("--bootstrap", action="store_true", help="إنشاء pos_db فقط")
    parser.add_argument("--write-env", action="store_true", help="تحديث DATABASE_URL")
    parser.add_argument("--migrate", action="store_true", help="نقل pos.db")
    parser.add_argument("--verify", action="store_true", help="اختبار التطبيق")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--root-user", default="root")
    parser.add_argument(
        "--root-password",
        default="",
        help="كلمة root في XAMPP (افتراضي: فارغة)",
    )
    parser.add_argument(
        "--use-root",
        action="store_true",
        help="استخدم root مباشرة في DATABASE_URL (بدون pos_user)",
    )
    parser.add_argument(
        "--no-force",
        action="store_true",
        help="لا تستبدل بيانات MySQL عند النقل",
    )
    args = parser.parse_args()

    if not any([args.all, args.bootstrap, args.write_env, args.migrate, args.verify]):
        parser.print_help()
        return

    wait_xampp_mysql(
        host=args.host,
        port=args.port,
        root_user=args.root_user,
        root_password=args.root_password,
    )

    url = bootstrap_xampp_database(
        host=args.host,
        port=args.port,
        root_user=args.root_user,
        root_password=args.root_password,
        use_app_user=not args.use_root,
    )

    if args.all or args.write_env or args.bootstrap:
        write_env_mysql_url(url)

    if args.all or args.migrate:
        migrate_sqlite(force=not args.no_force)

    if args.all or args.verify:
        verify_app()

    print()
    print("✓ اكتمل إعداد XAMPP + MySQL")
    print("DATABASE_URL =", url)
    print("الخطوة التالية: start-server.bat")


if __name__ == "__main__":
    main()
