"""خدمة النسخ الاحتياطي / الاستعادة / تصفير قاعدة البيانات.

مستويات التصفير:
1) `clear_transactions` — يحذف الحركات التشغيلية (مبيعات، جلسات، خزينة، مشتريات بضائع،
   مصروفات، حركات مخزون، مرتجعات، توصيل، …). يُبقي الإعدادات والكتالوج والمستخدمين
   وسجل الأصول الثابتة (للإهلاك) مع تصفير أرصدة الخزينة والمخزون.
2) `clear_all_except_users` — يحذف كل البيانات التشغيلية مع الكتالوج والإعدادات،
   ويُبقي فقط: المستخدمون، الأدوار، الصلاحيات (ضرورية للدخول).
3) `factory_reset` — يحذف ملف قاعدة البيانات بالكامل (يتم على القرص).
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from infra.config import get_settings
from infra.database import (
    database_kind,
    database_label_ar,
    is_mysql_url,
    is_postgresql_url,
    is_server_database_url,
    is_sqlite_url,
    server_backup_suffix as backup_suffix_for_url,
)
from infra.db import get_engine


# ===== مساعدات الموقع =====


def _sqlite_path() -> Path | None:
    """يستخرج مسار ملف SQLite من DATABASE_URL إن كان sqlite، وإلا None."""
    url = get_settings().database_url or ""
    if not is_sqlite_url(url):
        return None
    # sqlite:///./pos.db  أو  sqlite:////absolute/path/db
    raw = url.split("sqlite:///", 1)[-1]
    if not raw or raw == ":memory:":
        return None
    p = Path(raw).resolve()
    return p


def is_sqlite() -> bool:
    return _sqlite_path() is not None


def is_postgresql() -> bool:
    return is_postgresql_url(get_settings().database_url or "")


def is_mysql() -> bool:
    return is_mysql_url(get_settings().database_url or "")


def is_server_database() -> bool:
    return is_server_database_url(get_settings().database_url or "")


def backup_file_suffix() -> str:
    return backup_suffix_for_url(get_settings().database_url or "")


def db_kind() -> str:
    return database_kind()


def db_kind_label_ar() -> str:
    return database_label_ar()


def db_path_str() -> str:
    if is_sqlite():
        p = _sqlite_path()
        return str(p) if p else "—"
    url = get_settings().database_url or ""
    # إخفاء كلمة المرور في العرض
    if "@" in url:
        head, tail = url.rsplit("@", 1)
        if ":" in head:
            scheme_user = head.rsplit(":", 1)[0]
            return f"{scheme_user}:****@{tail}"
    return url or "—"


def db_size_human() -> str:
    if is_sqlite():
        p = _sqlite_path()
        if p is None or not p.exists():
            return "—"
        sz = p.stat().st_size
    elif is_postgresql():
        try:
            eng = get_engine()
            with eng.connect() as conn:
                raw = conn.execute(
                    text("SELECT pg_database_size(current_database())")
                ).scalar()
            sz = int(raw or 0)
        except Exception:
            return "—"
    elif is_mysql():
        try:
            eng = get_engine()
            with eng.connect() as conn:
                raw = conn.execute(
                    text(
                        "SELECT COALESCE(SUM(data_length + index_length), 0) "
                        "FROM information_schema.tables "
                        "WHERE table_schema = DATABASE()"
                    )
                ).scalar()
            sz = int(raw or 0)
        except Exception:
            return "—"
    else:
        return "—"
    for unit in ("B", "KB", "MB", "GB"):
        if sz < 1024:
            return f"{sz:.1f} {unit}"
        sz /= 1024
    return f"{sz:.1f} TB"


def _backup_dir() -> Path:
    if is_sqlite():
        src = _sqlite_path()
        assert src is not None
        return src.parent / "backups"
    return Path.cwd() / "backups"


def _sqlalchemy_url():
    from sqlalchemy.engine import make_url

    return make_url(get_settings().database_url)


# ===== النسخ الاحتياطي =====


def _make_backup_sqlite(target_dir: Path) -> Path:
    src = _sqlite_path()
    if src is None:
        raise RuntimeError("ملف SQLite غير متاح.")
    if not src.exists():
        raise RuntimeError("ملف قاعدة البيانات غير موجود.")
    target_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dst = target_dir / f"pos-backup-{ts}.db"

    eng = get_engine()
    try:
        with eng.connect() as conn:
            conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.commit()
    except Exception:
        pass

    src_conn = sqlite3.connect(str(src))
    try:
        dst_conn = sqlite3.connect(str(dst))
        try:
            with dst_conn:
                src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()
    return dst


def _make_backup_postgresql(target_dir: Path) -> Path:
    import subprocess

    url = _sqlalchemy_url()
    if not url.drivername.startswith("postgres"):
        raise RuntimeError("DATABASE_URL ليس PostgreSQL.")

    target_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dst = target_dir / f"pos-backup-{ts}.dump"

    env = os.environ.copy()
    if url.password:
        env["PGPASSWORD"] = url.password

    cmd = [
        "pg_dump",
        "-h",
        url.host or "127.0.0.1",
        "-p",
        str(url.port or 5432),
        "-U",
        url.username or "postgres",
        "-Fc",
        "-f",
        str(dst),
        url.database or "postgres",
    ]
    try:
        subprocess.run(cmd, env=env, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError(
            "أداة pg_dump غير موجودة. ثبّت PostgreSQL client على السيرفر."
        ) from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        raise RuntimeError(f"فشل pg_dump: {detail}") from exc
    return dst


def _make_backup_mysql(target_dir: Path) -> Path:
    import subprocess

    url = _sqlalchemy_url()
    if not url.drivername.startswith("mysql"):
        raise RuntimeError("DATABASE_URL ليس MySQL.")

    target_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dst = target_dir / f"pos-backup-{ts}.sql"

    cmd = ["mysqldump"]
    if url.password:
        cmd.append(f"--password={url.password}")
    cmd.extend(
        [
            "-h",
            url.host or "127.0.0.1",
            "-P",
            str(url.port or 3306),
            "-u",
            url.username or "root",
            "--single-transaction",
            "--routines",
            "--triggers",
            "--result-file",
            str(dst),
            url.database or "pos_db",
        ]
    )

    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError(
            "أداة mysqldump غير موجودة. ثبّت MySQL client على السيرفر."
        ) from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        raise RuntimeError(f"فشل mysqldump: {detail}") from exc
    return dst


def make_backup(target_dir: Path | None = None) -> Path:
    """ينشئ نسخة احتياطية ويعيد مسار الملف."""
    if target_dir is None:
        target_dir = _backup_dir()
    target_dir = Path(target_dir)
    if is_postgresql():
        return _make_backup_postgresql(target_dir)
    if is_mysql():
        return _make_backup_mysql(target_dir)
    if is_sqlite():
        return _make_backup_sqlite(target_dir)
    raise RuntimeError("نوع قاعدة البيانات غير مدعوم للنسخ الاحتياطي.")


def list_backups(target_dir: Path | None = None) -> list[tuple[str, int, datetime]]:
    """قائمة بالنسخ الاحتياطية: (اسم الملف، الحجم، تاريخ التعديل)."""
    if target_dir is None:
        target_dir = _backup_dir()
    if not target_dir.exists():
        return []
    rows = []
    patterns = ("pos-backup-*.db", "pos-backup-*.dump", "pos-backup-*.sql")
    seen: set[Path] = set()
    for pattern in patterns:
        for p in sorted(target_dir.glob(pattern), reverse=True):
            if p in seen:
                continue
            seen.add(p)
            st = p.stat()
            rows.append(
                (p.name, st.st_size, datetime.fromtimestamp(st.st_mtime, tz=timezone.utc))
            )
    rows.sort(key=lambda r: r[2], reverse=True)
    return rows


def backup_path_for(name: str) -> Path | None:
    """يضمن أن الاسم آمن ويعود بالمسار الكامل لنسخة احتياطية."""
    target_dir = _backup_dir()
    candidate = (target_dir / name).resolve()
    if not str(candidate).startswith(str(target_dir.resolve())):
        return None
    if not candidate.exists():
        return None
    return candidate


# ===== الاستعادة =====


def _verify_sqlite_file(path: Path) -> tuple[bool, str]:
    """يتحقق أن الملف هو قاعدة SQLite صحيحة."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            cur = conn.cursor()
            cur.execute("PRAGMA integrity_check")
            result = cur.fetchone()
            if not result or result[0] != "ok":
                return False, f"اختبار سلامة فشل: {result}"
            # تأكّد من وجود جداول معروفة
            cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('users','sales','products')"
            )
            tables = {r[0] for r in cur.fetchall()}
            if not {"users", "products"}.intersection(tables):
                return (
                    False,
                    "هذا الملف لا يبدو وكأنه نسخة احتياطية صالحة لنظام نقطة البيع.",
                )
            return True, "OK"
        finally:
            conn.close()
    except sqlite3.DatabaseError as e:
        return False, f"الملف ليس قاعدة SQLite صحيحة: {e}"
    except Exception as e:  # noqa: BLE001
        return False, f"خطأ غير متوقع: {e}"


def restore_from_file(uploaded_path: Path) -> Path:
    """يستبدل قاعدة البيانات الحالية بنسخة احتياطية مرفوعة.

    يعمل: حفظ نسخة من الحالية كاحتياط، ثمّ نسخ المرفوع مكانها، ثمّ تحرير الاتصالات.
    """
    target = _sqlite_path()
    if target is None:
        raise RuntimeError("الاستعادة تعمل لقواعد SQLite فقط.")
    if not Path(uploaded_path).exists():
        raise RuntimeError("الملف المرفوع غير موجود.")

    ok, msg = _verify_sqlite_file(Path(uploaded_path))
    if not ok:
        raise RuntimeError(msg)

    # نسخة احتياطية أمان قبل الاستبدال
    safety_dir = target.parent / "backups"
    safety_dir.mkdir(parents=True, exist_ok=True)
    safety_name = (
        f"pre-restore-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.db"
    )
    safety_path = safety_dir / safety_name
    if target.exists():
        shutil.copy2(target, safety_path)

    # تحرير اتصالات SQLAlchemy
    try:
        eng = get_engine()
        eng.dispose()
    except Exception:
        pass

    # استبدال الملف
    shutil.copy2(uploaded_path, target)

    # حذف ملفات WAL/SHM المتبقّية لضمان قراءة قاعدة جديدة نظيفة
    for ext in ("-wal", "-shm"):
        side = target.with_name(target.name + ext)
        try:
            if side.exists():
                side.unlink()
        except Exception:
            pass

    # إعادة إنشاء engine عبر first call
    from infra.db import get_session_factory

    get_session_factory()
    return safety_path


def _pg_connection_env():
    url = _sqlalchemy_url()
    if not url.drivername.startswith("postgres"):
        raise RuntimeError("DATABASE_URL ليس PostgreSQL.")
    env = os.environ.copy()
    if url.password:
        env["PGPASSWORD"] = url.password
    return env, url


def restore_from_dump(uploaded_path: Path) -> Path:
    """يستعيد PostgreSQL من ملف pg_dump (.dump).

    يأخذ نسخة أمان أولاً، ثم يُعيد بناء القاعدة عبر pg_restore.
    """
    import subprocess

    if not is_postgresql():
        raise RuntimeError("استعادة .dump تعمل مع PostgreSQL فقط.")
    if not uploaded_path.exists():
        raise RuntimeError("ملف الاستعادة غير موجود.")
    if uploaded_path.suffix.lower() != ".dump":
        raise RuntimeError("ملف PostgreSQL يجب أن ينتهي بـ .dump")

    safety_path = make_backup()
    env, url = _pg_connection_env()

    try:
        eng = get_engine()
        eng.dispose()
    except Exception:
        pass

    cmd = [
        "pg_restore",
        "-h",
        url.host or "127.0.0.1",
        "-p",
        str(url.port or 5432),
        "-U",
        url.username or "postgres",
        "--clean",
        "--if-exists",
        "--no-owner",
        "--no-privileges",
        "-d",
        url.database or "postgres",
        str(uploaded_path),
    ]
    try:
        result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError(
            "أداة pg_restore غير موجودة. ثبّت PostgreSQL client على السيرفر."
        ) from exc

    # pg_restore قد يُرجع 1 مع تحذيرات غير حرجة
    if result.returncode not in (0, 1):
        detail = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
        raise RuntimeError(f"فشل pg_restore: {detail}")

    from infra.db import get_session_factory

    get_session_factory()
    return safety_path


def restore_from_sql(uploaded_path: Path) -> Path:
    """يستعيد MySQL من ملف mysqldump (.sql)."""
    import subprocess

    if not is_mysql():
        raise RuntimeError("استعادة .sql تعمل مع MySQL فقط.")
    if not uploaded_path.exists():
        raise RuntimeError("ملف الاستعادة غير موجود.")
    if uploaded_path.suffix.lower() != ".sql":
        raise RuntimeError("ملف MySQL يجب أن ينتهي بـ .sql")

    safety_path = make_backup()
    url = _sqlalchemy_url()

    try:
        eng = get_engine()
        eng.dispose()
    except Exception:
        pass

    cmd = ["mysql"]
    if url.password:
        cmd.append(f"--password={url.password}")
    cmd.extend(
        [
            "-h",
            url.host or "127.0.0.1",
            "-P",
            str(url.port or 3306),
            "-u",
            url.username or "root",
            url.database or "pos_db",
        ]
    )

    try:
        with uploaded_path.open("r", encoding="utf-8", errors="replace") as sql_in:
            result = subprocess.run(
                cmd, stdin=sql_in, capture_output=True, text=True
            )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "أداة mysql غير موجودة. ثبّت MySQL client على السيرفر."
        ) from exc

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
        raise RuntimeError(f"فشل mysql restore: {detail}")

    from infra.db import get_session_factory

    get_session_factory()
    return safety_path


def restore_backup(uploaded_path: Path) -> Path:
    """استعادة من نسخة احتياطية (.db / .dump / .sql)."""
    suffix = uploaded_path.suffix.lower()
    if is_sqlite():
        if suffix != ".db":
            raise RuntimeError("على SQLite استخدم ملف نسخة ينتهي بـ .db")
        return restore_from_file(uploaded_path)
    if is_postgresql():
        if suffix != ".dump":
            raise RuntimeError("على PostgreSQL استخدم ملف نسخة ينتهي بـ .dump")
        return restore_from_dump(uploaded_path)
    if is_mysql():
        if suffix != ".sql":
            raise RuntimeError("على MySQL استخدم ملف نسخة ينتهي بـ .sql")
        return restore_from_sql(uploaded_path)
    raise RuntimeError("نوع قاعدة البيانات غير مدعوم للاستعادة.")


# جداول الحركات (transactions) — تُحذف عند مستوى 1
_TRANSACTION_TABLES = [
    "kitchen_tickets",
    "sale_payments",
    "sale_lines",
    "sales",
    "stock_movements",
    "inventory_lots",
    "purchase_lines",  # سيُحذف هنا فقط للبضائع والمصروفات
    "purchases",  # سيُحذف هنا فقط للبضائع والمصروفات
    "alerts_log",
]

# جداول الإعدادات/الكتالوج — تُحذف عند مستوى 2 (مع الحفاظ على المستخدمين)
_CONFIG_TABLES = [
    "bom_lines",
    "products",
    "product_categories",
    "product_units",
    "dining_tables",
    "recurring_costs",
    "alert_subscriptions",
    "low_stock_alerts",
    "app_settings",
    "payment_methods",
]


def _table_exists(db: Session, name: str) -> bool:
    insp = inspect(db.get_bind())
    return name in set(insp.get_table_names())


def _safe_delete(db: Session, sql: str) -> int:
    if "FROM " in sql:
        # استخراج اسم الجدول بشكل بسيط
        tbl = sql.split("FROM ", 1)[1].split()[0].strip().strip(";")
        if not _table_exists(db, tbl):
            return 0
    try:
        result = db.execute(text(sql))
        return int(result.rowcount or 0)
    except Exception:
        return 0


def _run_deletes(db: Session, counts: dict[str, int], steps: list[tuple[str, str]]) -> None:
    """ينفّذ DELETE بالترتيب ويسجّل عدد الصفوف المحذوفة."""
    for key, sql in steps:
        counts[key] = _safe_delete(db, sql)


def clear_transactions(db: Session, *, keep_fixed_assets: bool = True) -> dict[str, int]:
    """مستوى 1: محو الحركات التشغيلية (مبيعات، جلسات، خزينة، مشتريات، مخزون، …).

    إن كان keep_fixed_assets=True فلن تُحذف فواتير Purchase(kind='ASSET') ولا بنودها
    (سجل الأصول الثابتة للإهلاك)، لكن تُحذف **كل** مدفوعات الشراء/التحويلات حتى
    تُصفَّر أرصدة الخزينة الرئيسية.

    ما يُبقى عمداً: الكتالوج، المستخدمون، الإعدادات، أساليب الدفع، الأصول الثابتة
    المسجَّلة (مع إهلاكها الشهري في التقارير)، وحدود إعادة الطلب في الكتالوج.
    """
    counts: dict[str, int] = {}

    # —— جلسات POS وعجز الكاشير ——
    _run_deletes(
        db,
        counts,
        [
            (
                "hr_deduction_repayments_shift",
                "DELETE FROM hr_deduction_repayments WHERE deduction_id IN "
                "(SELECT id FROM hr_employee_deductions WHERE source_type = 'POS_SHIFT_SHORTAGE')",
            ),
            (
                "hr_employee_deductions_shift",
                "DELETE FROM hr_employee_deductions WHERE source_type = 'POS_SHIFT_SHORTAGE'",
            ),
            ("pos_shift_shortages", "DELETE FROM pos_shift_shortages"),
            ("pos_shifts", "DELETE FROM pos_shifts"),
        ],
    )

    # —— مرتجعات ومدفوعات مرتبطة بالمبيعات ——
    _run_deletes(
        db,
        counts,
        [
            ("refund_payments", "DELETE FROM refund_payments"),
            ("payment_transfers", "DELETE FROM payment_transfers"),
            ("sale_return_lines", "DELETE FROM sale_return_lines"),
            ("sale_returns", "DELETE FROM sale_returns"),
            ("hotel_room_charges", "DELETE FROM hotel_room_charges"),
            ("delivery_cash_settlements", "DELETE FROM delivery_cash_settlements"),
            ("delivery_handoffs", "DELETE FROM delivery_handoffs"),
            ("kitchen_tickets", "DELETE FROM kitchen_tickets"),
            ("loyalty_transactions", "DELETE FROM loyalty_transactions"),
            ("referral_events", "DELETE FROM referral_events"),
            ("sale_payments", "DELETE FROM sale_payments"),
            ("sale_lines", "DELETE FROM sale_lines"),
            ("sales", "DELETE FROM sales"),
        ],
    )

    # —— مشتريات/مصروفات/مدفوعات (تصفير الخزينة) ——
    counts["purchase_payments"] = _safe_delete(db, "DELETE FROM purchase_payments")

    if keep_fixed_assets:
        counts["purchase_lines_inv_exp"] = _safe_delete(
            db,
            "DELETE FROM purchase_lines WHERE purchase_id IN "
            "(SELECT id FROM purchases WHERE kind IN ('INVENTORY','EXPENSE'))",
        )
        counts["purchases_inv_exp"] = _safe_delete(
            db, "DELETE FROM purchases WHERE kind IN ('INVENTORY','EXPENSE')"
        )
    else:
        counts["purchase_lines"] = _safe_delete(db, "DELETE FROM purchase_lines")
        counts["purchases"] = _safe_delete(db, "DELETE FROM purchases")

    counts["stock_movements"] = _safe_delete(db, "DELETE FROM stock_movements")

    # —— مخزون ——
    if _table_exists(db, "stock_balances"):
        try:
            db.execute(text("UPDATE stock_balances SET quantity = 0"))
            counts["stock_balances_zeroed"] = 1
        except Exception:
            pass
    if _table_exists(db, "products"):
        try:
            db.execute(text("UPDATE products SET stock_quantity = 0"))
            counts["products_stock_zeroed"] = 1
        except Exception:
            pass

    # —— ولاء العملاء (إحصائيات متراكمة من المبيعات) ——
    if _table_exists(db, "customers"):
        try:
            db.execute(
                text(
                    "UPDATE customers SET points_balance = 0, total_spent = 0, "
                    "visits_count = 0, last_visit_at = NULL"
                )
            )
            counts["customers_stats_reset"] = 1
        except Exception:
            pass

    # —— سجلات تشغيلية ثانوية ——
    _run_deletes(
        db,
        counts,
        [
            ("print_jobs", "DELETE FROM print_jobs"),
            ("dashboard_activities", "DELETE FROM dashboard_activities"),
            ("dashboard_section_seen", "DELETE FROM dashboard_section_seen"),
            ("message_outbox", "DELETE FROM message_outbox"),
            ("alerts_log", "DELETE FROM alerts_log"),
        ],
    )

    if _table_exists(db, "app_settings"):
        try:
            db.execute(
                text(
                    "DELETE FROM app_settings WHERE key IN "
                    "('alerts_low_stock_fingerprint',)"
                )
            )
            counts["alert_fingerprints_cleared"] = 1
        except Exception:
            pass

    db.commit()
    return counts


def clear_all_except_users(db: Session) -> dict[str, int]:
    """مستوى 2: يحذف كل شيء عدا المستخدمين والأدوار والصلاحيات."""
    counts = clear_transactions(db, keep_fixed_assets=False)
    for tbl in _CONFIG_TABLES:
        counts[tbl] = _safe_delete(db, f"DELETE FROM {tbl}")
    db.commit()
    return counts


def factory_reset() -> dict[str, str]:
    """مستوى 3: حذف ملف قاعدة البيانات. يجب إعادة تشغيل الخادم بعدها.
    ينقل الملف الحالي إلى مجلد النسخ كاحتياط أخير قبل الحذف.
    """
    target = _sqlite_path()
    if target is None:
        raise RuntimeError("التصفير الكامل يعمل لقواعد SQLite فقط.")
    safety_dir = target.parent / "backups"
    safety_dir.mkdir(parents=True, exist_ok=True)
    archive = safety_dir / (
        "factory-reset-archive-"
        + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        + ".db"
    )

    try:
        eng = get_engine()
        eng.dispose()
    except Exception:
        pass

    if target.exists():
        shutil.move(str(target), str(archive))
    for ext in ("-wal", "-shm"):
        side = target.with_name(target.name + ext)
        try:
            if side.exists():
                side.unlink()
        except Exception:
            pass

    # إجبار إعادة إنشاء engine + ترحيل + بذور
    from infra.db import get_session_factory  # noqa
    from infra.sqlite_patch import patch_sqlite_schema

    eng = get_engine()  # سينشئ ملف جديد فارغ
    # نُنشئ كل الجداول من الـ models قبل تطبيق الـ patch (الـ patch يُكمل ما لم يكن موجوداً)
    from infra.db import Base

    Base.metadata.create_all(eng)
    patch_sqlite_schema(eng)

    return {
        "archived_to": str(archive),
        "new_db": str(target),
    }
