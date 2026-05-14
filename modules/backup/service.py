"""خدمة النسخ الاحتياطي / الاستعادة / تصفير قاعدة البيانات.

مستويات التصفير:
1) `clear_transactions` — يحذف الحركات اليومية فقط (مبيعات، مشتريات بضائع، مصروفات،
   حركات مخزون، تذاكر مطبخ). يُبقي الإعدادات والكتالوج والمستخدمين والأصول الثابتة
   المُسجَّلة (لأنها مازالت موجودة فعلياً) وأساليب الدفع والتكاليف الشهرية.
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
from infra.db import get_engine


# ===== مساعدات الموقع =====


def _sqlite_path() -> Path | None:
    """يستخرج مسار ملف SQLite من DATABASE_URL إن كان sqlite، وإلا None."""
    url = get_settings().database_url or ""
    if not url.startswith("sqlite"):
        return None
    # sqlite:///./pos.db  أو  sqlite:////absolute/path/db
    raw = url.split("sqlite:///", 1)[-1]
    if not raw or raw == ":memory:":
        return None
    p = Path(raw).resolve()
    return p


def is_sqlite() -> bool:
    return _sqlite_path() is not None


def db_path_str() -> str:
    p = _sqlite_path()
    return str(p) if p else "(غير SQLite)"


def db_size_human() -> str:
    p = _sqlite_path()
    if p is None or not p.exists():
        return "—"
    sz = p.stat().st_size
    for unit in ("B", "KB", "MB", "GB"):
        if sz < 1024:
            return f"{sz:.1f} {unit}"
        sz /= 1024
    return f"{sz:.1f} TB"


# ===== النسخ الاحتياطي =====


def make_backup(target_dir: Path | None = None) -> Path:
    """ينشئ نسخة احتياطية مستقرّة (Online Backup API) ويعيد المسار."""
    src = _sqlite_path()
    if src is None:
        raise RuntimeError("النسخ الاحتياطي يعمل حالياً لقواعد SQLite فقط.")
    if not src.exists():
        raise RuntimeError("ملف قاعدة البيانات غير موجود.")
    if target_dir is None:
        target_dir = src.parent / "backups"
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dst = target_dir / f"pos-backup-{ts}.db"

    # تنفيذ checkpoint أولاً (إن كان WAL مفعَّلاً)
    eng = get_engine()
    try:
        with eng.connect() as conn:
            conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.commit()
    except Exception:
        pass

    # استخدم Online Backup API لاستكمال النسخ بأمان حتى مع كتابات متزامنة
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


def list_backups(target_dir: Path | None = None) -> list[tuple[str, int, datetime]]:
    """قائمة بالنسخ الاحتياطية الموجودة: (اسم الملف، الحجم بالبايت، تاريخ التعديل)."""
    src = _sqlite_path()
    if src is None:
        return []
    if target_dir is None:
        target_dir = src.parent / "backups"
    if not target_dir.exists():
        return []
    rows = []
    for p in sorted(target_dir.glob("pos-backup-*.db"), reverse=True):
        st = p.stat()
        rows.append(
            (p.name, st.st_size, datetime.fromtimestamp(st.st_mtime, tz=timezone.utc))
        )
    return rows


def backup_path_for(name: str) -> Path | None:
    """يضمن أن الاسم آمن ويعود بالمسار الكامل لنسخة احتياطية."""
    src = _sqlite_path()
    if src is None:
        return None
    target_dir = src.parent / "backups"
    candidate = (target_dir / name).resolve()
    if not str(candidate).startswith(str(target_dir.resolve())):
        return None  # محاولة Path Traversal
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


# ===== التصفير =====


# جداول الحركات (transactions) — تُحذف عند مستوى 1
_TRANSACTION_TABLES = [
    "kitchen_tickets",
    "sale_payments",
    "sale_lines",
    "sales",
    "stock_movements",
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


def clear_transactions(db: Session, *, keep_fixed_assets: bool = True) -> dict[str, int]:
    """مستوى 1: محو الحركات (مبيعات، مشتريات، مصروفات، حركات مخزون، تذاكر مطبخ).
    إن كان keep_fixed_assets=True فلن يُحذف Purchase(kind='ASSET') لأن الأصول الثابتة
    لا تزال موجودة فعلياً ويجب إبقاء سجلّها.
    إعادة رصيد المخزون إلى صفر لأن الحركات أُلغيت.
    """
    counts: dict[str, int] = {}
    counts["kitchen_tickets"] = _safe_delete(db, "DELETE FROM kitchen_tickets")
    counts["sale_payments"] = _safe_delete(db, "DELETE FROM sale_payments")
    counts["sale_lines"] = _safe_delete(db, "DELETE FROM sale_lines")
    counts["sales"] = _safe_delete(db, "DELETE FROM sales")
    counts["stock_movements"] = _safe_delete(db, "DELETE FROM stock_movements")

    if keep_fixed_assets:
        # احذف بنود وفواتير الـ INVENTORY و EXPENSE فقط
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

    # سجلات التنبيهات
    counts["alerts_log"] = _safe_delete(db, "DELETE FROM alerts_log")

    # تصفير المخزون
    if _table_exists(db, "products"):
        try:
            db.execute(text("UPDATE products SET stock_quantity = 0"))
            counts["products_stock_zeroed"] = 1
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
