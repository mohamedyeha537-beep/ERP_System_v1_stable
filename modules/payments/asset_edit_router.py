from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.datastructures import UploadFile

from app.deps import DBSession, require_permission
from app.jinja_env import templates
from modules.authz.models import User
from modules.authz.permissions import ASSETS_EDIT_INVOICE
from modules.catalog.uploads import save_purchase_invoice_image
from modules.payments.asset_edit import AssetEditError, edit_asset_purchase, get_editable_asset_purchase
from modules.payments.models import Purchase
from modules.payments.service import (
    list_payment_methods_main_treasury_for_pay,
    sum_purchase_payments,
)

router = APIRouter(prefix="/admin/assets", tags=["assets-edit"])
_edit_perm = require_permission(ASSETS_EDIT_INVOICE)
_STATIC = Path(__file__).resolve().parents[2] / "app" / "static"


def _unlink_static_relative(static_root: Path, relative: str | None) -> None:
    if not relative:
        return
    try:
        fp = (static_root / relative).resolve()
        root = static_root.resolve()
        if root in fp.parents or fp == root:
            if fp.is_file():
                fp.unlink()
    except OSError:
        pass


def _parse_datetime_local(raw: str) -> datetime | None:
    if not raw.strip():
        return None
    try:
        dt = datetime.fromisoformat(raw.strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _parse_asset_lines(form) -> list[tuple[str, str | None, Decimal, Decimal, int, Decimal]]:
    names = form.getlist("item_name")
    units = form.getlist("unit")
    qtys = form.getlist("quantity")
    costs = form.getlist("unit_cost")
    lives = form.getlist("useful_life_months")
    salvages = form.getlist("salvage_value")
    lines: list[tuple[str, str | None, Decimal, Decimal, int, Decimal]] = []
    for nm, un, raw_qty, raw_cost, raw_life, raw_salvage in zip(
        names, units, qtys, costs, lives, salvages
    ):
        if not (nm or "").strip():
            continue
        qty = Decimal((raw_qty or "0").strip() or "0")
        cost = Decimal((raw_cost or "0").strip() or "0")
        salvage = Decimal((raw_salvage or "0").strip() or "0")
        try:
            life = int((raw_life or "0").strip() or "0")
        except ValueError:
            life = 0
        if life < 0:
            life = 0
        if qty > 0:
            lines.append(
                (nm.strip(), (un or "").strip() or None, qty, cost, life, salvage)
            )
    return lines


@router.get("/{pid}/edit", response_class=HTMLResponse)
def asset_edit_page(
    request: Request,
    pid: int,
    db: DBSession,
    user: User = Depends(_edit_perm),
):
    try:
        purchase = get_editable_asset_purchase(db, pid)
    except AssetEditError as exc:
        return RedirectResponse(
            f"/admin/assets?error={quote(str(exc))}",
            status_code=302,
        )
    methods = list_payment_methods_main_treasury_for_pay(db, only_active=True)
    paid_total = sum_purchase_payments(db, purchase.id)
    return templates.TemplateResponse(
        "admin_asset_edit.html",
        {
            "request": request,
            "purchase": purchase,
            "methods": methods,
            "paid_total": paid_total,
            "payment_count": len(purchase.payments or []),
            "user": user,
            "error": request.query_params.get("error"),
            "saved": request.query_params.get("saved"),
            "life_presets": [0, 12, 24, 36, 60, 84, 120],
        },
    )


@router.post("/{pid}/edit", response_class=HTMLResponse)
async def asset_edit_save(
    request: Request,
    pid: int,
    db: DBSession,
    user: User = Depends(_edit_perm),
):
    form = await request.form()
    reason = str(form.get("reason") or "").strip()
    if not reason:
        return RedirectResponse(
            f"/admin/assets/{pid}/edit?error={quote('اذكر سبب التعديل.')}",
            status_code=302,
        )

    pm_raw = (form.get("payment_method_id") or "").strip()
    supplier = (form.get("supplier") or "").strip()
    note = (form.get("note") or "").strip()
    purchase_date_raw = (form.get("purchase_date") or "").strip()
    supplier_invoice_ref = (form.get("supplier_invoice_ref") or "").strip()
    remove_image = str(form.get("remove_invoice_image") or "") == "on"

    invoice_image_filename: str | None = None
    inv_upload = form.get("invoice_image")
    if isinstance(inv_upload, UploadFile) and inv_upload.filename:
        try:
            invoice_image_filename = save_purchase_invoice_image(inv_upload, _STATIC)
        except ValueError as exc:
            return RedirectResponse(
                f"/admin/assets/{pid}/edit?error={quote(str(exc))}",
                status_code=302,
            )

    try:
        pm_id = int(pm_raw)
    except (TypeError, ValueError):
        if invoice_image_filename:
            _unlink_static_relative(_STATIC, invoice_image_filename)
        return RedirectResponse(
            f"/admin/assets/{pid}/edit?error={quote('أسلوب الدفع غير صالح.')}",
            status_code=302,
        )

    try:
        lines = _parse_asset_lines(form)
    except (InvalidOperation, ValueError):
        if invoice_image_filename:
            _unlink_static_relative(_STATIC, invoice_image_filename)
        return RedirectResponse(
            f"/admin/assets/{pid}/edit?error={quote('بيانات بند غير صالحة.')}",
            status_code=302,
        )

    if not lines:
        if invoice_image_filename:
            _unlink_static_relative(_STATIC, invoice_image_filename)
        return RedirectResponse(
            f"/admin/assets/{pid}/edit?error={quote('أضف بنداً واحداً على الأقل.')}",
            status_code=302,
        )

    purchase = db.execute(
        select(Purchase).where(Purchase.id == pid)
    ).scalar_one_or_none()
    old_image = purchase.invoice_image_filename if purchase else None

    created_at = _parse_datetime_local(purchase_date_raw)
    try:
        edit_asset_purchase(
            db,
            purchase_id=pid,
            payment_method_id=pm_id,
            supplier=supplier,
            note=note,
            lines=lines,
            user_id=user.id,
            created_at=created_at,
            supplier_invoice_ref=supplier_invoice_ref or None,
            invoice_image_filename=invoice_image_filename,
            remove_invoice_image=remove_image,
            reason=reason,
        )
        db.commit()
        if invoice_image_filename and old_image and old_image != invoice_image_filename:
            _unlink_static_relative(_STATIC, old_image)
        if remove_image and old_image:
            _unlink_static_relative(_STATIC, old_image)
    except AssetEditError as exc:
        db.rollback()
        if invoice_image_filename:
            _unlink_static_relative(_STATIC, invoice_image_filename)
        return RedirectResponse(
            f"/admin/assets/{pid}/edit?error={quote(str(exc))}",
            status_code=302,
        )

    return RedirectResponse(
        f"/admin/assets/{pid}/edit?saved=1",
        status_code=302,
    )
