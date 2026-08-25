"""معالج طلب استيراد العملاء — يُستخدم من أكثر من مسار."""
from __future__ import annotations

from urllib.parse import quote, urlencode

from fastapi import File, Form, UploadFile
from fastapi.responses import RedirectResponse

from app.deps import DBSession
from modules.authz.models import User
from modules.customers.import_service import import_customers_csv


async def _read_upload(file: UploadFile | None) -> str:
    if file is None or not file.filename:
        raise ValueError("لم يُرفع ملف.")
    raw = await file.read()
    for enc in ("utf-8-sig", "utf-8", "cp1256", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def import_redirect(result, *, return_to: str) -> RedirectResponse:
    base = return_to if return_to.startswith("/") else "/admin/customers"
    if not result.ok() and result.created == 0 and result.updated == 0:
        err = "؛ ".join(result.errors[:8])
        if len(result.errors) > 8:
            err += f" … (+{len(result.errors) - 8})"
        return RedirectResponse(f"{base}?error={quote(err)}", status_code=302)

    if base.startswith("/admin/backup"):
        notice = f"تم استيراد العملاء: {result.summary()}"
        if result.warnings:
            warn = "؛ ".join(result.warnings[:3])
            notice += f" — تحذيرات: {warn}"
        if result.errors:
            notice += f" — أخطاء جزئية: {'؛ '.join(result.errors[:3])}"
        return RedirectResponse(f"{base}?notice={quote(notice[:900])}", status_code=302)

    params = {
        "saved": "import",
        "created": str(result.created),
        "updated": str(result.updated),
        "skipped": str(result.skipped),
        "points": str(result.points_restored),
        "wallet": str(result.wallet_restored),
        "referral": str(result.referral_set),
    }
    if result.warnings:
        warn = "؛ ".join(result.warnings[:5])
        if len(result.warnings) > 5:
            warn += f" … (+{len(result.warnings) - 5})"
        params["warn"] = warn[:900]
    if result.errors:
        err = "؛ ".join(result.errors[:5])
        if len(result.errors) > 5:
            err += f" … (+{len(result.errors) - 5})"
        params["import_err"] = err[:900]
    return RedirectResponse(f"{base}?{urlencode(params)}", status_code=302)


async def handle_customers_import(
    db: DBSession,
    user: User,
    *,
    file: UploadFile,
    update_existing: str = "on",
    create_missing: str = "on",
    restore_points: str = "on",
    restore_wallet: str = "on",
    restore_referral: str = "on",
    return_to: str = "/admin/customers",
) -> RedirectResponse:
    try:
        text = await _read_upload(file)
    except ValueError as e:
        base = return_to if return_to.startswith("/") else "/admin/customers"
        return RedirectResponse(f"{base}?error={quote(str(e))}", status_code=302)
    result = import_customers_csv(
        db,
        text,
        user_id=user.id,
        update_existing=update_existing == "on",
        create_missing=create_missing == "on",
        restore_points=restore_points == "on",
        restore_wallet=restore_wallet == "on",
        restore_referral=restore_referral == "on",
    )
    db.commit()
    return import_redirect(result, return_to=return_to)
