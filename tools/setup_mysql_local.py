#!/usr/bin/env python3
"""إعداد MySQL محلياً (Docker) + نقل SQLite → MySQL — مطابق لبيئة VPS.

أمثلة:
  python tools/setup_mysql_local.py --start          # تشغيل حاوية MySQL
  python tools/setup_mysql_local.py --write-env      # تحديث DATABASE_URL في .env
  python tools/setup_mysql_local.py --migrate        # نقل pos.db → MySQL
  python tools/setup_mysql_local.py --all            # كل الخطوات
  python tools/setup_mysql_local.py --verify         # اختبار الاتصال
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

DEFAULT_MYSQL_URL = (
    "mysql+pymysql://pos_user:pos_local_dev@127.0.0.1:3306/pos_db?charset=utf8mb4"
)
DOCKER_COMPOSE = ["docker", "compose"]


def _run(cmd: list[str], *, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    print("$", " ".join(cmd))
    return subprocess.run(cmd, cwd=cwd or ROOT, check=check)


def start_mysql_container() -> None:
    try:
        _run(DOCKER_COMPOSE + ["up", "-d", "mysql"])
    except subprocess.CalledProcessError as exc:
        raise SystemExit(
            "تعذّر تشغيل MySQL عبر Docker.\n"
            "• تأكد أن Docker Desktop يعمل (أيقونة الحوت في شريط المهام).\n"
            "• أو ثبّت MySQL على Windows وعدّل DATABASE_URL في .env.\n"
            "راجع: deploy/mysql-local-windows.md"
        ) from exc
    print("انتظار جاهزية MySQL…")
    for _ in range(90):
        proc = subprocess.run(
            DOCKER_COMPOSE + ["ps", "--format", "json", "mysql"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0 and "healthy" in (proc.stdout or "").lower():
            print("✓ MySQL جاهز (healthy)")
            return
        time.sleep(2)
    print("⚠ لم يُؤكَّد healthcheck — سنجرب الاتصال مباشرة…")
    wait_mysql_tcp()


def wait_mysql_tcp(timeout: int = 60) -> None:
    try:
        import pymysql
    except ImportError as exc:
        raise SystemExit("ثبّت الحزم: pip install -r requirements.txt") from exc

    deadline = time.time() + timeout
    last_err = ""
    while time.time() < deadline:
        try:
            conn = pymysql.connect(
                host="127.0.0.1",
                port=3306,
                user="pos_user",
                password="pos_local_dev",
                database="pos_db",
                charset="utf8mb4",
                connect_timeout=3,
            )
            conn.close()
            print("✓ اتصال MySQL ناجح")
            return
        except Exception as exc:
            last_err = str(exc)
            time.sleep(2)
    raise SystemExit(f"فشل الاتصال بـ MySQL: {last_err}")


def write_env_mysql_url(url: str = DEFAULT_MYSQL_URL) -> None:
    env_path = ROOT / ".env"
    if not env_path.is_file():
        example = ROOT / ".env.example"
        if example.is_file():
            shutil.copy(example, env_path)
            print(f"أُنشئ .env من .env.example")
        else:
            env_path.write_text(f"DATABASE_URL={url}\n", encoding="utf-8")
            print("✓ DATABASE_URL في .env")
            return

    backup = ROOT / ".env.bak-mysql"
    shutil.copy(env_path, backup)
    print(f"نسخة احتياطية: {backup.name}")

    text = env_path.read_text(encoding="utf-8")
    if re.search(r"^DATABASE_URL=", text, flags=re.MULTILINE):
        text = re.sub(
            r"^DATABASE_URL=.*$",
            f"DATABASE_URL={url}",
            text,
            count=1,
            flags=re.MULTILINE,
        )
    else:
        text = f"DATABASE_URL={url}\n" + text
    env_path.write_text(text, encoding="utf-8")
    print(f"✓ DATABASE_URL → MySQL")


def verify_app() -> None:
    from infra.config import get_settings
    from infra.database import database_kind, database_label_ar
    from infra.db import get_engine, reset_engine
    from infra.schema_bootstrap import bootstrap_schema

    get_settings.cache_clear()
    reset_engine()
    url = get_settings().database_url
    kind = database_kind(url)
    print(f"نوع القاعدة: {database_label_ar(url)} ({kind})")
    if kind != "mysql":
        raise SystemExit("DATABASE_URL لا يزال ليس MySQL — شغّل --write-env")

    import infra.model_registry

    infra.model_registry.import_all_models()
    engine = get_engine()
    bootstrap_schema(engine)
    with engine.connect() as conn:
        from sqlalchemy import text

        users = conn.execute(text("SELECT COUNT(*) FROM users")).scalar()
        products = conn.execute(text("SELECT COUNT(*) FROM products")).scalar()
    print(f"✓ users={users}  products={products}")
    print("✓ التطبيق جاهز للعمل على MySQL محلياً")


def migrate_sqlite(force: bool = True) -> None:
    sqlite_path = ROOT / "pos.db"
    if not sqlite_path.is_file():
        raise SystemExit(f"لا يوجد {sqlite_path.name} — لا شيء للنقل")

    from tools.migrate_sqlite_to_mysql import migrate

    source = f"sqlite:///{sqlite_path.resolve().as_posix()}"
    get_settings = __import__("infra.config", fromlist=["get_settings"]).get_settings
    from infra.db import reset_engine

    get_settings.cache_clear()
    reset_engine()
    target = get_settings().database_url
    migrate(source, target, force=force, dry_run=False)


def main() -> None:
    parser = argparse.ArgumentParser(description="إعداد MySQL محلي + نقل SQLite")
    parser.add_argument("--start", action="store_true", help="تشغيل docker compose mysql")
    parser.add_argument("--write-env", action="store_true", help="تحديث DATABASE_URL في .env")
    parser.add_argument("--migrate", action="store_true", help="نقل pos.db إلى MySQL")
    parser.add_argument("--verify", action="store_true", help="اختبار الاتصال والجداول")
    parser.add_argument("--all", action="store_true", help="start + write-env + migrate + verify")
    parser.add_argument(
        "--mysql-url",
        default=DEFAULT_MYSQL_URL,
        help="رابط MySQL (لـ --write-env)",
    )
    parser.add_argument(
        "--no-force",
        action="store_true",
        help="لا تستبدل بيانات MySQL الموجودة عند النقل",
    )
    args = parser.parse_args()

    if not any([args.start, args.write_env, args.migrate, args.verify, args.all]):
        parser.print_help()
        return

    if args.all or args.start:
        start_mysql_container()
    elif args.verify or args.migrate or args.write_env:
        wait_mysql_tcp()

    if args.all or args.write_env:
        write_env_mysql_url(args.mysql_url)

    if args.all or args.migrate:
        migrate_sqlite(force=not args.no_force)

    if args.all or args.verify:
        verify_app()

    print()
    print("الخطوة التالية: start-server.bat")
    print("رابط MySQL المحلي:", args.mysql_url)


if __name__ == "__main__":
    main()
