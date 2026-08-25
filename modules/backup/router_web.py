"""واجهة إدارة النسخ الاحتياطي والاستعادة والتصفير."""

from __future__ import annotations

import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from app.deps import DBSession, require_any_permission, require_permission
from app.jinja_env import templates
from modules.authz.models import User
from modules.authz.permissions import BACKUP_MANAGE, CUSTOMERS_MANAGE, CUSTOMERS_VIEW
from modules.backup import service as backup_service


router = APIRouter(prefix="/admin/backup", tags=["backup"])
_perm = require_permission(BACKUP_MANAGE)
_export_perm = require_any_permission(BACKUP_MANAGE, CUSTOMERS_VIEW)
_import_perm = require_any_permission(BACKUP_MANAGE, CUSTOMERS_MANAGE)


def _backup_redirect(*, notice: str | None = None, error: str | None = None) -> RedirectResponse:
    q: dict[str, str] = {}
    if notice:
        q["notice"] = notice[:500]
    if error:
        q["error"] = str(error)[:800]
    return RedirectResponse("/admin/backup?" + urlencode(q), status_code=302)


@router.get("", response_class=HTMLResponse)
def backup_home(
    request: Request,
    db: DBSession,
    _: User = Depends(_perm),
    error: str | None = Query(None),
    notice: str | None = Query(None),
):
    backups = backup_service.list_backups()
    return templates.TemplateResponse(
        "admin_backup.html",
        {
            "request": request,
            "is_sqlite": backup_service.is_sqlite(),
            "is_postgresql": backup_service.is_postgresql(),
            "is_mysql": backup_service.is_mysql(),
            "is_server_db": backup_service.is_server_database(),
            "db_kind": backup_service.db_kind_label_ar(),
            "db_path": backup_service.db_path_str(),
            "db_size": backup_service.db_size_human(),
            "backups": backups,
            "error": error,
            "notice": notice,
        },
    )


@router.post("/create", response_class=HTMLResponse)
def backup_create(
    db: DBSession,
    _: User = Depends(_perm),
):
    """ينشئ نسخة احتياطية جديدة على القرص في مجلد backups/."""
    try:
        path = backup_service.make_backup()
        return _backup_redirect(notice=f"تم إنشاء النسخة: {path.name}")
    except Exception as e:  # noqa: BLE001
        return _backup_redirect(error=str(e))


@router.get("/download/current")
def backup_download_current(_: User = Depends(_perm)):
    """ينزّل نسخة من القاعدة الحالية مباشرة."""
    try:
        tmp_dir = Path(tempfile.gettempdir()) / "pos-backup-tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        path = backup_service.make_backup(tmp_dir)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        ext = path.suffix or backup_service.backup_file_suffix()
        return FileResponse(
            str(path),
            filename=f"pos-backup-{ts}{ext}",
            media_type="application/octet-stream",
        )
    except Exception as e:  # noqa: BLE001
        return _backup_redirect(error=str(e))


@router.get("/download/{name}")
def backup_download_named(name: str, _: User = Depends(_perm)):
    """ينزّل نسخة احتياطية موجودة بالاسم."""
    p = backup_service.backup_path_for(name)
    if p is None:
        return _backup_redirect(error="النسخة غير موجودة أو الاسم غير صالح.")
    return FileResponse(
        str(p),
        filename=p.name,
        media_type="application/octet-stream",
    )


@router.post("/delete/{name}")
def backup_delete_named(name: str, _: User = Depends(_perm)):
    p = backup_service.backup_path_for(name)
    if p is None:
        return _backup_redirect(error="النسخة غير موجودة.")
    try:
        p.unlink()
        return _backup_redirect(notice=f"تم حذف {name}")
    except Exception as e:  # noqa: BLE001
        return _backup_redirect(error=str(e))


@router.post("/restore-uploaded", response_class=HTMLResponse)
async def backup_restore_uploaded(
    file: UploadFile = File(...),
    _: User = Depends(_perm),
):
    """يستعيد قاعدة البيانات من ملف يرفعه المستخدم. يأخذ نسخة أمان أولاً."""
    name = Path(file.filename or "").name.lower()
    expected = backup_service.backup_file_suffix()
    allowed = name.endswith(expected) or (
        expected == ".sql" and (name.endswith(".sql.gz") or name.endswith(".gz"))
    )
    if not allowed:
        return _backup_redirect(error=f"ارفع ملف {expected} أو {expected}.gz فقط.")
    tmp_suffix = ".sql.gz" if name.endswith(".gz") else (expected or ".bak")
    fd, tmp_path = tempfile.mkstemp(suffix=tmp_suffix)
    try:
        with os.fdopen(fd, "wb") as out:
            shutil.copyfileobj(file.file, out)
        tmp = Path(tmp_path)
        if tmp.stat().st_size < 32:
            return _backup_redirect(
                error=(
                    "الملف فارغ أو قُطع أثناء الرفع. على السيرفر زد "
                    "client_max_body_size في nginx إلى 256M (الملفات الحالية ~90MB)."
                )
            )
        safety = backup_service.restore_backup(tmp)
        return _backup_redirect(
            notice=f"تمت الاستعادة. نسخة الأمان قبل الاستعادة: {safety.name}"
        )
    except Exception as e:  # noqa: BLE001
        return _backup_redirect(error=str(e))
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


@router.post("/restore-existing/{name}", response_class=HTMLResponse)
def backup_restore_existing(name: str, _: User = Depends(_perm)):
    p = backup_service.backup_path_for(name)
    if p is None:
        return _backup_redirect(error="النسخة غير موجودة.")
    try:
        safety = backup_service.restore_backup(p)
        return _backup_redirect(notice=f"تمت استعادة {name}. نسخة الأمان: {safety.name}")
    except Exception as e:  # noqa: BLE001
        return _backup_redirect(error=str(e))


@router.post("/reset/transactions", response_class=HTMLResponse)
def reset_transactions(
    db: DBSession,
    _: User = Depends(_perm),
    confirm: str = Form(""),
):
    """تصفير الحركات فقط — الاحتفاظ بالكتالوج والمستخدمين والإعدادات والأصول."""
    if confirm.strip() != "محو الحركات":
        return _backup_redirect(error="يجب كتابة عبارة التأكيد بالضبط: محو الحركات")
    # نسخة احتياطية أمان أولاً
    try:
        backup_service.make_backup()
    except Exception:
        pass
    counts = backup_service.clear_transactions(db, keep_fixed_assets=True)
    return _backup_redirect(notice=f"تم تصفير الحركات. صفوف محذوفة: {sum(counts.values())}")


@router.post("/reset/all-except-users", response_class=HTMLResponse)
def reset_all_except_users(
    db: DBSession,
    _: User = Depends(_perm),
    confirm: str = Form(""),
):
    """تصفير كل البيانات عدا المستخدمين والصلاحيات."""
    if confirm.strip() != "محو كل البيانات":
        return _backup_redirect(error="يجب كتابة عبارة التأكيد بالضبط: محو كل البيانات")
    try:
        backup_service.make_backup()
    except Exception:
        pass
    counts = backup_service.clear_all_except_users(db)
    return _backup_redirect(
        notice=f"تم تصفير كل البيانات عدا المستخدمين. صفوف محذوفة: {sum(counts.values())}"
    )


@router.get("/export-customers")
def backup_export_customers(
    request: Request,
    db: DBSession,
    user: User = Depends(_export_perm),
):
    """تصدير العملاء (نقاط + إحالة) — مسار مستقل لا يتعارض مع /admin/customers/{id}."""
    from modules.customers.export import export_customers_for_request

    return export_customers_for_request(request, db, user)


@router.post("/import-customers")
async def backup_import_customers(
    db: DBSession,
    user: User = Depends(_import_perm),
    file: UploadFile = File(...),
    update_existing: str = Form("on"),
    create_missing: str = Form("on"),
    restore_points: str = Form("on"),
    restore_wallet: str = Form("on"),
    restore_referral: str = Form("on"),
):
    from modules.customers.import_handlers import handle_customers_import

    return await handle_customers_import(
        db,
        user,
        file=file,
        update_existing=update_existing,
        create_missing=create_missing,
        restore_points=restore_points,
        restore_wallet=restore_wallet,
        restore_referral=restore_referral,
        return_to="/admin/backup",
    )


@router.post("/clear-notifications", response_class=HTMLResponse)
def clear_notifications_route(
    db: DBSession,
    _: User = Depends(_perm),
    confirm: str = Form(""),
):
    """تصفير عداد الجرس ومركز الإشعارات دون مسح العملاء أو الحركات."""
    if confirm.strip() != "تصفير الإشعارات":
        return _backup_redirect(
            error="يجب كتابة عبارة التأكيد بالضبط: تصفير الإشعارات"
        )
    counts = backup_service.clear_notifications(db)
    return _backup_redirect(
        notice=f"تم تصفير الإشعارات. صفوف محذوفة: {sum(counts.values())}"
    )


@router.post("/reset/factory", response_class=HTMLResponse)
def reset_factory(
    _: User = Depends(_perm),
    confirm: str = Form(""),
):
    """التصفير الكامل (مصنع) — SQLite: حذف الملف. PostgreSQL: غير متاح."""
    if not backup_service.is_sqlite():
        return _backup_redirect(
            error="التصفير الكامل (مصنع) متاح لـ SQLite فقط. على MySQL/PostgreSQL أنشئ قاعدة جديدة يدوياً."
        )
    if confirm.strip() != "تصفير المصنع الكامل":
        return _backup_redirect(error="يجب كتابة عبارة التأكيد بالضبط: تصفير المصنع الكامل")
    try:
        result = backup_service.factory_reset()
        msg = (
            f"تم التصفير الكامل. أُرشفت القاعدة القديمة في: {Path(result['archived_to']).name}. "
            "يُستحسن إعادة تشغيل الخادم لإعادة بذر بيانات افتراضية."
        )
        return _backup_redirect(notice=msg)
    except Exception as e:  # noqa: BLE001
        return _backup_redirect(error=str(e))
