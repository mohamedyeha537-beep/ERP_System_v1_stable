from __future__ import annotations

from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from app.deps import DBSession, require_permission
from app.jinja_env import templates
from modules.authz.models import User
from modules.authz.permissions import SALES_EDIT_INVOICE
from modules.hotel.models import HotelRoom
from modules.inventory.service import InsufficientStock
from modules.payments.service import list_pos_sale_payment_methods
from modules.refunds.service import search_completed_sales
from modules.sales.invoice_edit import InvoiceEditError, edit_completed_invoice, get_editable_sale

router = APIRouter(prefix="/sales/invoices", tags=["sales-invoices"])
_edit_perm = require_permission(SALES_EDIT_INVOICE)


def _parse_decimal(raw: str | None) -> Decimal | None:
    try:
        text = str(raw or "").strip()
        if not text:
            return None
        return Decimal(text)
    except (InvalidOperation, TypeError, ValueError):
        return None


def _parse_int(raw: str | None) -> int | None:
    text = str(raw or "").strip()
    if not text or not text.isdigit():
        return None
    val = int(text)
    return val if val > 0 else None


@router.get("", response_class=HTMLResponse)
def invoice_edit_index(
    request: Request,
    db: DBSession,
    user: User = Depends(_edit_perm),
    q: str = Query(""),
):
    sales = search_completed_sales(db, q=q, limit=100) if q.strip() else []
    return templates.TemplateResponse(
        "sales_invoice_edit_index.html",
        {
            "request": request,
            "sales": sales,
            "q": q,
            "user": user,
            "error": request.query_params.get("error"),
            "saved": request.query_params.get("saved"),
        },
    )


@router.get("/{sale_id}/edit", response_class=HTMLResponse)
def invoice_edit_form(
    request: Request,
    sale_id: int,
    db: DBSession,
    user: User = Depends(_edit_perm),
):
    try:
        modal = get_editable_sale(db, sale_id)
    except InvoiceEditError as exc:
        return RedirectResponse(f"/sales/invoices?error={exc}", status_code=302)
    return templates.TemplateResponse(
        "sales_invoice_edit.html",
        {
            "request": request,
            "modal": modal,
            "user": user,
            "payment_methods": list_pos_sale_payment_methods(
                db, only_active=True, user=user
            ),
            "hotel_rooms": list(
                db.scalars(
                    select(HotelRoom)
                    .where(HotelRoom.is_active.is_(True))
                    .order_by(HotelRoom.number, HotelRoom.name_ar)
                ).all()
            ),
            "error": request.query_params.get("error"),
            "saved": request.query_params.get("saved"),
        },
    )


@router.post("/{sale_id}/edit", response_class=HTMLResponse)
async def invoice_edit_save(
    request: Request,
    sale_id: int,
    db: DBSession,
    user: User = Depends(_edit_perm),
):
    form = await request.form()
    updates: dict[int, tuple[Decimal, Decimal]] = {}
    for key in form.keys():
        sk = str(key)
        if not sk.startswith("qty_"):
            continue
        line_id_raw = sk.replace("qty_", "", 1)
        if not line_id_raw.isdigit():
            continue
        line_id = int(line_id_raw)
        qty = _parse_decimal(str(form.get(sk)))
        price = _parse_decimal(str(form.get(f"price_{line_id}")))
        if qty is None or price is None:
            return RedirectResponse(
                f"/sales/invoices/{sale_id}/edit?error=كمية أو سعر غير صالح.",
                status_code=302,
            )
        updates[line_id] = (qty, price)

    settlement_type = str(form.get("settlement_type") or "keep").strip() or "keep"
    payment_method_id = _parse_int(str(form.get("payment_method_id") or ""))
    room_id = _parse_int(str(form.get("room_id") or ""))
    if settlement_type == "keep" and room_id is not None:
        settlement_type = "room"

    try:
        edit_completed_invoice(
            db,
            sale_id=sale_id,
            line_updates=updates,
            user_id=user.id,
            reason=str(form.get("reason") or "").strip() or None,
            settlement_type=settlement_type,
            payment_method_id=payment_method_id,
            room_id=room_id,
            guest_name=str(form.get("guest_name") or "").strip() or None,
            settlement_note=str(form.get("settlement_note") or "").strip() or None,
        )
        db.commit()
    except InsufficientStock as exc:
        db.rollback()
        return RedirectResponse(
            f"/sales/invoices/{sale_id}/edit?error={exc}",
            status_code=302,
        )
    except InvoiceEditError as exc:
        db.rollback()
        return RedirectResponse(
            f"/sales/invoices/{sale_id}/edit?error={exc}",
            status_code=302,
        )

    return RedirectResponse(
        f"/sales/invoices/{sale_id}/edit?saved=1",
        status_code=302,
    )
