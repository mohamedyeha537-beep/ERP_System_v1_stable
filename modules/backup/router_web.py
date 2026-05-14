"""واجهة إدارة النسخ الاحتياطي والاستعادة والتصفير."""

from __future__ import annotations

import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from app.deps import DBSession, require_permission
from app.jinja_env import templates
from modules.authz.models import User
from modules.authz.permissions import BACKUP_MANAGE
from modules.backup import service as backup_service


router = APIRouter(prefix="/admin/backup", tags=["backup"])
_perm = require_permission(BACKUP_MANAGE)


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
        return RedirectResponse(
            f"/admin/backup?notice=" + f"تم إنشاء النسخة: {path.name}",
            status_code=302,
        )
    except Exception as e:  # noqa: BLE001
        return RedirectResponse(
            f"/admin/backup?error=" + str(e), status_code=302
        )


@router.get("/download/current")
def backup_download_current(_: User = Depends(_perm)):
    """ينزّل قاعدة البيانات الحالية مباشرة كملف."""
    if not backup_service.is_sqlite():
        return RedirectResponse(
            "/admin/backup?error=" + "النسخ الاحتياطي يعمل لقواعد SQLite فقط.",
            status_code=302,
        )
    # ننشئ نسخة Online Backup ثم نرسلها (أأمن من نسخ الملف مع كتابات متزامنة)
    try:
        tmp_dir = Path(tempfile.gettempdir()) / "pos-backup-tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        path = backup_service.make_backup(tmp_dir)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        return FileResponse(
            str(path),
            filename=f"pos-backup-{ts}.db",
            media_type="application/octet-stream",
        )
    except Exception as e:  # noqa: BLE001
        return RedirectResponse(
            f"/admin/backup?error=" + str(e), status_code=302
        )


@router.get("/download/{name}")
def backup_download_named(name: str, _: User = Depends(_perm)):
    """ينزّل نسخة احتياطية موجودة بالاسم."""
    p = backup_service.backup_path_for(name)
    if p is None:
        return RedirectResponse(
            "/admin/backup?error=" + "النسخة غير موجودة أو الاسم غير صالح.",
            status_code=302,
        )
    return FileResponse(
        str(p),
        filename=p.name,
        media_type="application/octet-stream",
    )


@router.post("/delete/{name}")
def backup_delete_named(name: str, _: User = Depends(_perm)):
    p = backup_service.backup_path_for(name)
    if p is None:
        return RedirectResponse(
            "/admin/backup?error=" + "النسخة غير موجودة.", status_code=302
        )
    try:
        p.unlink()
        return RedirectResponse(
            "/admin/backup?notice=" + f"تم حذف {name}", status_code=302
        )
    except Exception as e:  # noqa: BLE001
        return RedirectResponse(f"/admin/backup?error={e}", status_code=302)


@router.post("/restore-uploaded", response_class=HTMLResponse)
async def backup_restore_uploaded(
    file: UploadFile = File(...),
    _: User = Depends(_perm),
):
    """يستعيد قاعدة البيانات من ملف يرفعه المستخدم. يأخذ نسخة أمان أولاً."""
    if not backup_service.is_sqlite():
        return RedirectResponse(
            "/admin/backup?error=" + "الاستعادة تعمل لقواعد SQLite فقط.",
            status_code=302,
        )
    fd, tmp_path = tempfile.mkstemp(suffix=".db")
    try:
        with os.fdopen(fd, "wb") as out:
            shutil.copyfileobj(file.file, out)
        safety = backup_service.restore_from_file(Path(tmp_path))
        return RedirectResponse(
            "/admin/backup?notice="
            + f"تمت الاستعادة. نسخة الأمان قبل الاستعادة: {safety.name}",
            status_code=302,
        )
    except Exception as e:  # noqa: BLE001
        return RedirectResponse(f"/admin/backup?error={e}", status_code=302)
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


@router.post("/restore-existing/{name}", response_class=HTMLResponse)
def backup_restore_existing(name: str, _: User = Depends(_perm)):
    p = backup_service.backup_path_for(name)
    if p is None:
        return RedirectResponse(
            "/admin/backup?error=" + "النسخة غير موجودة.", status_code=302
        )
    try:
        safety = backup_service.restore_from_file(p)
        return RedirectResponse(
            "/admin/backup?notice="
            + f"تمت استعادة {name}. نسخة الأمان: {safety.name}",
            status_code=302,
        )
    except Exception as e:  # noqa: BLE001
        return RedirectResponse(f"/admin/backup?error={e}", status_code=302)


@router.post("/reset/transactions", response_class=HTMLResponse)
def reset_transactions(
    db: DBSession,
    _: User = Depends(_perm),
    confirm: str = Query(""),
):
    """تصفير الحركات فقط — الاحتفاظ بالكتالوج والمستخدمين والإعدادات والأصول."""
    if confirm != "محو الحركات":
        return RedirectResponse(
            "/admin/backup?error="
            + "يجب كتابة عبارة التأكيد بالضبط: محو الحركات",
            status_code=302,
        )
    # نسخة احتياطية أمان أولاً
    try:
        backup_service.make_backup()
    except Exception:
        pass
    counts = backup_service.clear_transactions(db, keep_fixed_assets=True)
    return RedirectResponse(
        "/admin/backup?notice=" + f"تم تصفير الحركات. صفوف محذوفة: {sum(counts.values())}",
        status_code=302,
    )


@router.post("/reset/all-except-users", response_class=HTMLResponse)
def reset_all_except_users(
    db: DBSession,
    _: User = Depends(_perm),
    confirm: str = Query(""),
):
    """تصفير كل البيانات عدا المستخدمين والصلاحيات."""
    if confirm != "محو كل البيانات":
        return RedirectResponse(
            "/admin/backup?error="
            + "يجب كتابة عبارة التأكيد بالضبط: محو كل البيانات",
            status_code=302,
        )
    try:
        backup_service.make_backup()
    except Exception:
        pass
    counts = backup_service.clear_all_except_users(db)
    return RedirectResponse(
        "/admin/backup?notice="
        + f"تم تصفير كل البيانات عدا المستخدمين. صفوف محذوفة: {sum(counts.values())}",
        status_code=302,
    )


@router.post("/reset/factory", response_class=HTMLResponse)
def reset_factory(
    _: User = Depends(_perm),
    confirm: str = Query(""),
):
    """التصفير الكامل (مصنع) — يحذف ملف القاعدة. يحتاج إعادة تشغيل الخادم."""
    if confirm != "تصفير المصنع الكامل":
        return RedirectResponse(
            "/admin/backup?error="
            + "يجب كتابة عبارة التأكيد بالضبط: تصفير المصنع الكامل",
            status_code=302,
        )
    try:
        result = backup_service.factory_reset()
        msg = (
            f"تم التصفير الكامل. أُرشفت القاعدة القديمة في: {Path(result['archived_to']).name}. "
            "يُستحسن إعادة تشغيل الخادم لإعادة بذر بيانات افتراضية."
        )
        return RedirectResponse("/admin/backup?notice=" + msg, status_code=302)
    except Exception as e:  # noqa: BLE001
        return RedirectResponse(f"/admin/backup?error={e}", status_code=302)
