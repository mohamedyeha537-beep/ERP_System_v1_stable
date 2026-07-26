from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.datastructures import UploadFile

from app.deps import DBSession, require_any_permission
from app.jinja_env import templates
from modules.authz.models import User
from modules.authz.permissions import PURCHASE_INVOICES_MANAGE, PURCHASES_MANAGE
from modules.catalog.models import Product
from modules.catalog.service import CatalogError, list_stockable_products
from modules.catalog.uploads import save_purchase_invoice_image
from modules.inventory.lots import parse_lot_dates_from_form
from modules.inventory.service import get_main_warehouse, list_warehouses
from modules.payments.purchase_edit import (
    PurchaseEditError,
    edit_inventory_purchase,
    get_editable_inventory_purchase,
)
from modules.payments.service import (
    InventoryPurchaseLineIn,
    PaymentsError,
    assert_purchase_custody_payment_method,
    assert_purchase_term_payment_method,
    list_payment_methods_for_purchase_term,
    list_payment_methods_for_purchase_term_custody,
    purchase_user_limited_to_custody,
    sum_purchase_payments,
)

router = APIRouter(prefix="/admin/purchases", tags=["purchases-edit"])
_edit_perm = require_any_permission(PURCHASE_INVOICES_MANAGE, PURCHASES_MANAGE)
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


def _parse_inventory_lines_with_db(db, form) -> list[InventoryPurchaseLineIn]:
    product_ids = form.getlist("product_id")
    qtys = form.getlist("quantity")
    costs = form.getlist("unit_cost")
    prod_dates = form.getlist("expiry_production_date")
    exp_dates = form.getlist("expiry_date")
    lines: list[InventoryPurchaseLineIn] = []
    for i, (raw_pid, raw_qty, raw_cost) in enumerate(zip(product_ids, qtys, costs)):
        if not (raw_pid or "").strip():
            continue
        try:
            pid = int(raw_pid)
            qty = Decimal((raw_qty or "0").strip() or "0")
            cost = Decimal((raw_cost or "0").strip() or "0")
        except (InvalidOperation, ValueError) as exc:
            raise PurchaseEditError("بيانات بند غير صالحة.") from exc
        if qty <= 0:
            continue
        product = db.get(Product, pid)
        if product is None:
            raise PurchaseEditError("صنف غير موجود.")
        prod_raw = prod_dates[i] if i < len(prod_dates) else ""
        exp_raw = exp_dates[i] if i < len(exp_dates) else ""
        try:
            lot_dates = parse_lot_dates_from_form(
                prod_raw,
                exp_raw,
                product_name=product.name_ar,
                required=bool(product.expiry_tracked),
            )
        except CatalogError as exc:
            raise PurchaseEditError(str(exc)) from exc
        lines.append(
            InventoryPurchaseLineIn(
                product_id=pid,
                quantity=qty,
                unit_cost=cost,
                production_date=lot_dates.production_date,
                expiry_date=lot_dates.expiry_date,
            )
        )
    return lines


@router.get("/{pid}/edit", response_class=HTMLResponse)
def purchase_edit_page(
    request: Request,
    pid: int,
    db: DBSession,
    user: User = Depends(_edit_perm),
):
    try:
        purchase = get_editable_inventory_purchase(db, pid)
    except PurchaseEditError as exc:
        return RedirectResponse(
            f"/admin/purchases?error={quote(str(exc))}",
            status_code=302,
        )
    # خزين/عهد المطعم والفندق معاً — موظف المشتريات قد يدفع من أيهما
    if purchase_user_limited_to_custody(user):
        methods = list_payment_methods_for_purchase_term_custody(
            db, only_active=True, domain=None
        )
    else:
        methods = list_payment_methods_for_purchase_term(
            db, only_active=True, domain=None
        )
    products = list_stockable_products(db)
    warehouses = list_warehouses(db)
    products_by_id = {p.id: p for p in products}
    paid_total = sum_purchase_payments(db, purchase.id)
    return templates.TemplateResponse(
        "admin_purchase_edit.html",
        {
            "request": request,
            "purchase": purchase,
            "methods": methods,
            "products": products,
            "products_by_id": products_by_id,
            "warehouses": warehouses,
            "default_warehouse_id": purchase.warehouse_id or get_main_warehouse(db).id,
            "paid_total": paid_total,
            "payment_count": len(purchase.payments or []),
            "user": user,
            "error": request.query_params.get("error"),
            "saved": request.query_params.get("saved"),
        },
    )


@router.post("/{pid}/edit", response_class=HTMLResponse)
async def purchase_edit_save(
    request: Request,
    pid: int,
    db: DBSession,
    user: User = Depends(_edit_perm),
):
    form = await request.form()
    pm_raw = (form.get("payment_method_id") or "").strip()
    supplier = (form.get("supplier") or "").strip()
    supplier_phone = (form.get("supplier_phone") or "").strip()
    note = (form.get("note") or "").strip()
    purchase_date_raw = (form.get("purchase_date") or "").strip()
    supplier_invoice_ref = (form.get("supplier_invoice_ref") or "").strip()
    remove_invoice_image = (form.get("remove_invoice_image") or "").strip() == "on"

    try:
        pm_id = int(pm_raw)
    except (TypeError, ValueError):
        return RedirectResponse(
            f"/admin/purchases/{pid}/edit?error={quote('أسلوب الدفع غير صالح.')}",
            status_code=302,
        )

    try:
        if purchase_user_limited_to_custody(user):
            assert_purchase_custody_payment_method(db, pm_id)
        else:
            assert_purchase_term_payment_method(db, pm_id)
    except PaymentsError as exc:
        return RedirectResponse(
            f"/admin/purchases/{pid}/edit?error={quote(str(exc))}",
            status_code=302,
        )

    try:
        old_purchase = get_editable_inventory_purchase(db, pid)
    except PurchaseEditError as exc:
        return RedirectResponse(
            f"/admin/purchases?error={quote(str(exc))}",
            status_code=302,
        )
    old_invoice_image = old_purchase.invoice_image_filename

    invoice_image_filename: str | None = None
    inv_upload = form.get("invoice_image")
    if isinstance(inv_upload, UploadFile) and inv_upload.filename:
        try:
            invoice_image_filename = save_purchase_invoice_image(inv_upload, _STATIC)
        except ValueError as exc:
            return RedirectResponse(
                f"/admin/purchases/{pid}/edit?error={quote(str(exc))}",
                status_code=302,
            )

    try:
        lines = _parse_inventory_lines_with_db(db, form)
    except PurchaseEditError as exc:
        if invoice_image_filename:
            _unlink_static_relative(_STATIC, invoice_image_filename)
        return RedirectResponse(
            f"/admin/purchases/{pid}/edit?error={quote(str(exc))}",
            status_code=302,
        )

    if not lines:
        if invoice_image_filename:
            _unlink_static_relative(_STATIC, invoice_image_filename)
        return RedirectResponse(
            f"/admin/purchases/{pid}/edit?error={quote('أضف بنداً واحداً على الأقل.')}",
            status_code=302,
        )

    wh_raw = (form.get("warehouse_id") or "").strip()
    try:
        warehouse_id = int(wh_raw)
    except (TypeError, ValueError):
        if invoice_image_filename:
            _unlink_static_relative(_STATIC, invoice_image_filename)
        return RedirectResponse(
            f"/admin/purchases/{pid}/edit?error={quote('اختر المخزن.')}",
            status_code=302,
        )

    created_at = _parse_datetime_local(purchase_date_raw)
    try:
        edit_inventory_purchase(
            db,
            purchase_id=pid,
            payment_method_id=pm_id,
            supplier=supplier,
            supplier_phone=supplier_phone or None,
            note=note,
            lines=lines,
            warehouse_id=warehouse_id,
            user_id=user.id,
            created_at=created_at,
            supplier_invoice_ref=supplier_invoice_ref or None,
            invoice_image_filename=invoice_image_filename,
            remove_invoice_image=remove_invoice_image,
        )
        db.commit()
    except PurchaseEditError as exc:
        db.rollback()
        if invoice_image_filename:
            _unlink_static_relative(_STATIC, invoice_image_filename)
        return RedirectResponse(
            f"/admin/purchases/{pid}/edit?error={quote(str(exc))}",
            status_code=302,
        )

    if remove_invoice_image and old_invoice_image:
        _unlink_static_relative(_STATIC, old_invoice_image)
    elif invoice_image_filename and old_invoice_image:
        _unlink_static_relative(_STATIC, old_invoice_image)

    return RedirectResponse(
        f"/admin/purchases/{pid}?saved_edit=1",
        status_code=302,
    )
