"""بوابة الزبون — /stay"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from app.deps import DBSession, require_module
from app.jinja_env import templates
from modules.catalog.models import Product
from modules.catalog.service import pos_visible_product_criteria
from modules.hotel.portal_service import (
    BookingError,
    portal_can_order,
    portal_create_booking,
    portal_create_room_order,
    portal_enabled,
    portal_get_session,
    portal_record_deposit,
    portal_search,
)
from modules.payments.service import list_hotel_settle_payment_methods
from modules.platform.module_registry import HOTEL_PORTAL
from sqlalchemy import select

stay_router = APIRouter(prefix="/stay", tags=["hotel-stay"])
stay_api = APIRouter(prefix="/api/hotel/guest", tags=["hotel-guest-api"])

_mod = require_module(HOTEL_PORTAL)


def _stay_ctx(request: Request, db: DBSession, **extra):
    from modules.web_marketing.service import SURFACE_HOTEL_PORTAL, get_public_web_config

    path = (request.url.path or "/stay").split("?")[0]
    title = extra.pop("page_title", None)
    analytics_flash = extra.pop("analytics_flash", None)
    web_mkt = get_public_web_config(
        db,
        SURFACE_HOTEL_PORTAL,
        page_path=path,
        page_title=title,
        default_title="حجز إقامة — الفندق",
    )
    if analytics_flash and isinstance(analytics_flash, dict):
        init = dict(web_mkt.get("analytics_init") or {})
        init["flash_event"] = analytics_flash
        web_mkt = dict(web_mkt)
        web_mkt["analytics_init"] = init
    ctx = {
        "request": request,
        "web_mkt": web_mkt,
    }
    ctx.update(extra)
    return ctx


def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        return None


@stay_router.get("", response_class=HTMLResponse, dependencies=[Depends(_mod)])
def stay_home(request: Request, db: DBSession):
    return RedirectResponse("/suites", status_code=302)


@stay_router.post("/search", dependencies=[Depends(_mod)])
def stay_search(
    request: Request,
    db: DBSession,
    check_in: str = Form(...),
    check_out: str = Form(...),
    adults: str = Form("1"),
    children: str = Form("0"),
):
    ci = _parse_date(check_in)
    co = _parse_date(check_out)
    q = ""
    if ci:
        q += f"check_in={ci.isoformat()}"
    if co:
        q += ("&" if q else "") + f"check_out={co.isoformat()}"
    return RedirectResponse(f"/suites{'?' + q if q else ''}", status_code=302)


@stay_router.get("/book", dependencies=[Depends(_mod)])
def stay_book_form(
    request: Request,
    db: DBSession,
    room_type_id: int = Query(...),
    check_in: str = Query(...),
    check_out: str = Query(...),
    adults: int = Query(1),
):
    ci = _parse_date(check_in)
    co = _parse_date(check_out)
    q = ""
    if ci:
        q += f"check_in={ci.isoformat()}"
    if co:
        q += ("&" if q else "") + f"check_out={co.isoformat()}"
    return RedirectResponse(f"/suites{'?' + q if q else ''}", status_code=302)


@stay_router.post("/book", dependencies=[Depends(_mod)])
def stay_book_submit(
    db: DBSession,
    guest_name: str = Form(...),
    guest_phone: str = Form(...),
    guest_email: str = Form(""),
    check_in: str = Form(...),
    check_out: str = Form(...),
    room_type_id: int = Form(...),
    adults: str = Form("1"),
    children: str = Form("0"),
    deposit: str = Form("0"),
    pay_method_id: str = Form(""),
):
    ci = _parse_date(check_in)
    co = _parse_date(check_out)
    q = ""
    if ci:
        q += f"check_in={ci.isoformat()}"
    if co:
        q += ("&" if q else "") + f"check_out={co.isoformat()}"
    return RedirectResponse(f"/suites{'?' + q if q else ''}", status_code=302)


@stay_router.get("/my/{token}", response_class=HTMLResponse, dependencies=[Depends(_mod)])
def stay_my_booking(request: Request, token: str, db: DBSession):
    try:
        booking = portal_get_session(db, token)
    except BookingError as e:
        return templates.TemplateResponse(
            "stay/error.html",
            _stay_ctx(request, db, error=str(e)),
            status_code=404,
        )
    from modules.hotel.folio import build_folio

    folio = build_folio(db, booking.id) if booking.id else None
    products = []
    if portal_can_order(db, token):
        products = list(
            db.scalars(
                select(Product).where(pos_visible_product_criteria()).order_by(Product.name_ar)
            ).all()
        )[:80]
    return templates.TemplateResponse(
        "stay/my_booking.html",
        _stay_ctx(
            request,
            db,
            page_title=f"حجز #{booking.id}" if booking.id else "حجزي",
            booking=booking,
            folio=folio,
            token=token,
            can_order=portal_can_order(db, token),
            products=products,
            saved=request.query_params.get("saved"),
            order_saved=request.query_params.get("order_saved"),
            error=request.query_params.get("error"),
            analytics_flash=_booking_flash_event(request, booking),
        ),
    )


def _booking_flash_event(request: Request, booking) -> dict | None:
    if request.query_params.get("saved"):
        return {
            "type": "booking",
            "meta": {
                "booking_id": booking.id,
                "reference": getattr(booking, "reference", None),
            },
        }
    if request.query_params.get("order_saved"):
        return {"type": "room_order", "meta": {"booking_id": booking.id}}
    return None


@stay_router.post("/my/{token}/order", dependencies=[Depends(_mod)])
def stay_order(
    token: str,
    db: DBSession,
    product_id: int = Form(...),
    quantity: str = Form("1"),
):
    try:
        sale_id = portal_create_room_order(
            db, token=token, product_id=product_id, quantity=Decimal(quantity or "1")
        )
        from modules.web_marketing.analytics import record_room_order

        record_room_order(
            db,
            sale_id=sale_id,
            product_id=product_id,
            page_path=f"/stay/my/{token}",
        )
        db.commit()
    except (BookingError, InvalidOperation) as e:
        db.rollback()
        return RedirectResponse(f"/stay/my/{token}?error={e}", status_code=302)
    return RedirectResponse(f"/stay/my/{token}?order_saved=1", status_code=302)


class GuestOrderPayload(BaseModel):
    token: str
    product_id: int
    quantity: str = "1"


@stay_api.post("/order", dependencies=[Depends(_mod)])
def api_guest_order(payload: GuestOrderPayload, db: DBSession):
    try:
        sale_id = portal_create_room_order(
            db,
            token=payload.token,
            product_id=payload.product_id,
            quantity=Decimal(payload.quantity or "1"),
        )
        db.commit()
    except (BookingError, InvalidOperation) as e:
        db.rollback()
        return {"ok": False, "error": str(e)}
    return {"ok": True, "sale_id": sale_id}
