from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.deps import DBSession, require_permission
from app.jinja_env import templates
from modules.authz.models import User
from modules.authz.permissions import INVENTORY_RECEIVE, INVENTORY_VIEW, WAREHOUSES_MANAGE
from modules.catalog.service import list_stockable_products
from modules.inventory.transfer_service import (
    TransferError,
    approve_transfer_line,
    get_transfer,
    line_status_label,
    list_all_transfers,
    list_transfers_for_warehouse,
    reject_transfer_line,
    request_transfer_line_modify,
    transfer_status_label,
)
from modules.inventory.service import (
    InsufficientStock,
    WarehouseError,
    create_branch_warehouse,
    get_balance,
    get_main_warehouse,
    get_warehouse,
    list_branch_warehouses,
    list_warehouses,
    low_stock_products,
    transfer_to_branch,
    update_warehouse,
)

router = APIRouter(tags=["warehouses"])
_wh_manage = require_permission(WAREHOUSES_MANAGE)
_wh_view = require_permission(INVENTORY_VIEW)
_wh_receive = require_permission(INVENTORY_RECEIVE)


def _user_warehouse_id(user: User) -> int | None:
    return getattr(user, "warehouse_id", None)


def _resolve_recipient_warehouse(db, user: User, warehouse_id: int | None) -> int | None:
    if warehouse_id is not None:
        wh = get_warehouse(db, warehouse_id)
        if wh is not None and wh.is_active and not wh.is_main:
            return wh.id
    uid_wh = _user_warehouse_id(user)
    if uid_wh:
        return uid_wh
    return None


@router.get("/admin/warehouses", response_class=HTMLResponse)
def warehouses_admin(
    request: Request,
    db: DBSession,
    _: User = Depends(_wh_manage),
    saved: int = Query(0),
    error: str | None = Query(None),
):
    main = get_main_warehouse(db)
    branches = list_branch_warehouses(db)
    all_wh = list_warehouses(db, active_only=False)
    return templates.TemplateResponse(
        "admin_warehouses.html",
        {
            "request": request,
            "main_warehouse": main,
            "branches": branches,
            "all_warehouses": all_wh,
            "saved": bool(saved),
            "error": error,
        },
    )


@router.post("/admin/warehouses/add", response_class=HTMLResponse)
def warehouses_add(
    request: Request,
    db: DBSession,
    _: User = Depends(_wh_manage),
    name_ar: str = Form(...),
    notes: str = Form(""),
):
    try:
        create_branch_warehouse(db, name_ar, notes=notes)
        db.commit()
    except WarehouseError as e:
        db.rollback()
        return RedirectResponse(
            "/admin/warehouses?error=" + quote(str(e)),
            status_code=302,
        )
    return RedirectResponse("/admin/warehouses?saved=1", status_code=302)


@router.post("/admin/warehouses/{wid}/edit", response_class=HTMLResponse)
def warehouses_edit(
    wid: int,
    db: DBSession,
    _: User = Depends(_wh_manage),
    name_ar: str = Form(...),
    is_active: str = Form("1"),
    deduct_sales_enabled: str = Form(""),
    notes: str = Form(""),
):
    try:
        update_warehouse(
            db,
            wid,
            name_ar=name_ar,
            is_active=is_active == "1",
            deduct_sales_enabled=deduct_sales_enabled == "1",
            notes=notes,
        )
        db.commit()
    except WarehouseError as e:
        db.rollback()
        return RedirectResponse(
            "/admin/warehouses?error=" + quote(str(e)),
            status_code=302,
        )
    return RedirectResponse("/admin/warehouses?saved=1", status_code=302)


@router.get("/admin/warehouses/transfer", response_class=HTMLResponse)
def warehouses_transfer_page(
    request: Request,
    db: DBSession,
    _: User = Depends(_wh_manage),
    error: str | None = Query(None),
    saved: int = Query(0),
):
    main = get_main_warehouse(db)
    branches = list_branch_warehouses(db)
    products = list_stockable_products(db)
    balances = {p.id: get_balance(db, p.id, main.id) for p in products}
    return templates.TemplateResponse(
        "admin_warehouse_transfer.html",
        {
            "request": request,
            "main_warehouse": main,
            "branches": branches,
            "products": products,
            "balances": balances,
            "error": error,
            "saved": bool(saved),
        },
    )


@router.post("/admin/warehouses/transfer", response_class=HTMLResponse)
async def warehouses_transfer_submit(
    request: Request,
    db: DBSession,
    user: User = Depends(_wh_manage),
):
    form = await request.form()
    try:
        to_id = int((form.get("to_warehouse_id") or "").strip())
    except (TypeError, ValueError):
        return RedirectResponse(
            "/admin/warehouses/transfer?error=" + quote("اختر المخزن الفرعي."),
            status_code=302,
        )
    note = (form.get("note") or "").strip()
    product_ids = form.getlist("product_id")
    qtys = form.getlist("quantity")
    lines: list[tuple[int, Decimal]] = []
    for raw_pid, raw_qty in zip(product_ids, qtys):
        if not (raw_pid or "").strip():
            continue
        try:
            pid = int(raw_pid)
            qty = Decimal((raw_qty or "0").strip() or "0")
        except (InvalidOperation, ValueError):
            return RedirectResponse(
                "/admin/warehouses/transfer?error=" + quote("بيانات بند غير صالحة."),
                status_code=302,
            )
        if qty > 0:
            lines.append((pid, qty))
    if not lines:
        return RedirectResponse(
            "/admin/warehouses/transfer?error=" + quote("أضف صنفاً واحداً على الأقل."),
            status_code=302,
        )
    try:
        transfer_to_branch(
            db,
            to_warehouse_id=to_id,
            lines=lines,
            user_id=user.id,
            note=note or None,
        )
        db.commit()
    except (WarehouseError, InsufficientStock) as e:
        db.rollback()
        return RedirectResponse(
            "/admin/warehouses/transfer?error=" + quote(str(e)),
            status_code=302,
        )
    return RedirectResponse("/admin/warehouses/transfer?saved=1", status_code=302)


@router.get("/admin/warehouses/transfers", response_class=HTMLResponse)
def warehouses_transfers_list(
    request: Request,
    db: DBSession,
    _: User = Depends(_wh_manage),
):
    transfers = list_all_transfers(db)
    return templates.TemplateResponse(
        "admin_warehouse_transfers.html",
        {
            "request": request,
            "transfers": transfers,
            "line_status_label": line_status_label,
            "transfer_status_label": transfer_status_label,
        },
    )


@router.get("/inventory/transfers/inbox", response_class=HTMLResponse)
def transfer_inbox(
    request: Request,
    db: DBSession,
    user: User = Depends(_wh_receive),
    warehouse_id: int | None = Query(None),
    msg: str | None = Query(None),
    err: str | None = Query(None),
):
    branches = list_branch_warehouses(db)
    wid = _resolve_recipient_warehouse(db, user, warehouse_id)
    transfers = list_transfers_for_warehouse(db, wid, pending_only=True) if wid else []
    return templates.TemplateResponse(
        "inventory_transfer_inbox.html",
        {
            "request": request,
            "transfers": transfers,
            "branches": branches,
            "warehouse_id": wid,
            "flash_msg": msg,
            "flash_err": err,
            "line_status_label": line_status_label,
            "transfer_status_label": transfer_status_label,
        },
    )


@router.post("/inventory/transfers/lines/{line_id}/approve", response_class=HTMLResponse)
def transfer_line_approve(
    line_id: int,
    db: DBSession,
    user: User = Depends(_wh_receive),
    qty_received: str = Form(""),
    warehouse_id: str = Form(""),
):
    back = "/inventory/transfers/inbox"
    if warehouse_id.strip().isdigit():
        back += f"?warehouse_id={warehouse_id.strip()}"
    try:
        qty = Decimal(qty_received.strip()) if qty_received.strip() else None
        approve_transfer_line(db, line_id, user.id, qty_received=qty)
        db.commit()
        return RedirectResponse(back + ("&" if "?" in back else "?") + "msg=" + quote("تمت مصادقة الاستلام."), status_code=302)
    except (TransferError, InvalidOperation, InsufficientStock, WarehouseError) as e:
        db.rollback()
        return RedirectResponse(back + ("&" if "?" in back else "?") + "err=" + quote(str(e)), status_code=302)


@router.post("/inventory/transfers/lines/{line_id}/reject", response_class=HTMLResponse)
def transfer_line_reject(
    line_id: int,
    db: DBSession,
    user: User = Depends(_wh_receive),
    note: str = Form(""),
    warehouse_id: str = Form(""),
):
    back = "/inventory/transfers/inbox"
    if warehouse_id.strip().isdigit():
        back += f"?warehouse_id={warehouse_id.strip()}"
    try:
        reject_transfer_line(db, line_id, user.id, note=note)
        db.commit()
        return RedirectResponse(back + ("&" if "?" in back else "?") + "msg=" + quote("تم رفض البند وإرجاع الكمية."), status_code=302)
    except (TransferError, WarehouseError) as e:
        db.rollback()
        return RedirectResponse(back + ("&" if "?" in back else "?") + "err=" + quote(str(e)), status_code=302)


@router.post("/inventory/transfers/lines/{line_id}/modify", response_class=HTMLResponse)
def transfer_line_modify(
    line_id: int,
    db: DBSession,
    user: User = Depends(_wh_receive),
    note: str = Form(...),
    suggested_qty: str = Form(""),
    warehouse_id: str = Form(""),
):
    back = "/inventory/transfers/inbox"
    if warehouse_id.strip().isdigit():
        back += f"?warehouse_id={warehouse_id.strip()}"
    try:
        sq = Decimal(suggested_qty.strip()) if suggested_qty.strip() else None
        request_transfer_line_modify(db, line_id, user.id, note=note, suggested_qty=sq)
        db.commit()
        return RedirectResponse(back + ("&" if "?" in back else "?") + "msg=" + quote("تم إرسال طلب التعديل."), status_code=302)
    except (TransferError, InvalidOperation) as e:
        db.rollback()
        return RedirectResponse(back + ("&" if "?" in back else "?") + "err=" + quote(str(e)), status_code=302)


@router.get("/inventory/low-stock/print", response_class=HTMLResponse)
def low_stock_print(
    request: Request,
    db: DBSession,
    _: User = Depends(_wh_view),
    warehouse_id: int = Query(...),
):
    wh = get_warehouse(db, warehouse_id)
    if wh is None:
        return RedirectResponse("/inventory?err=warehouse", status_code=302)
    rows = low_stock_products(db, warehouse_id)
    return templates.TemplateResponse(
        "inventory_low_stock_print.html",
        {
            "request": request,
            "warehouse": wh,
            "low_stock": rows,
            "printed_at": datetime.now(timezone.utc),
        },
    )
