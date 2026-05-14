"""Hotel module web routes:

- /admin/hotel/rooms          (HOTEL_ROOMS_MANAGE)  إدارة الغرف
- /hotel/settle               (HOTEL_SETTLE)        تسوية حسابات الغرف
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from starlette.requests import Request

from app.deps import DBSession, require_permission
from app.jinja_env import templates
from modules.authz.models import User
from modules.authz.permissions import (
    HOTEL_ROOMS_MANAGE,
    HOTEL_SETTLE,
)
from modules.hotel.models import HotelRoom, RoomCharge
from modules.hotel.service import (
    HotelError,
    create_room,
    delete_room,
    get_room,
    grand_open_total,
    list_charges,
    list_rooms,
    open_charges_for_room,
    open_guests_for_room,
    room_open_total,
    rooms_with_open_balance,
    settle_charges,
    update_room,
)
from modules.payments.service import list_payment_methods


# ============================================================
# Admin: Rooms management
# ============================================================
rooms_router = APIRouter(prefix="/admin/hotel/rooms", tags=["hotel-rooms"])
_admin = require_permission(HOTEL_ROOMS_MANAGE)


@rooms_router.get("", response_class=HTMLResponse)
def admin_rooms_page(
    request: Request,
    db: DBSession,
    _: User = Depends(_admin),
):
    rooms = list_rooms(db)
    open_totals: dict[int, "tuple"] = {}
    for r in rooms:
        open_totals[r.id] = (
            room_open_total(db, r.id),
            len(open_charges_for_room(db, r.id)),
        )
    return templates.TemplateResponse(
        "admin_hotel_rooms.html",
        {
            "request": request,
            "rooms": rooms,
            "open_totals": open_totals,
            "saved": request.query_params.get("saved"),
            "error": request.query_params.get("error"),
        },
    )


@rooms_router.post("/add", response_class=HTMLResponse)
def admin_rooms_add(
    db: DBSession,
    _: User = Depends(_admin),
    number: str = Form(...),
    notes: str = Form(""),
):
    try:
        create_room(db, number=number, notes=notes)
        db.commit()
    except HotelError as e:
        db.rollback()
        return RedirectResponse(
            f"/admin/hotel/rooms?error={e}", status_code=302
        )
    return RedirectResponse("/admin/hotel/rooms?saved=1", status_code=302)


@rooms_router.post("/{room_id}/edit", response_class=HTMLResponse)
def admin_rooms_edit(
    room_id: int,
    db: DBSession,
    _: User = Depends(_admin),
    number: str = Form(...),
    notes: str = Form(""),
    is_active: str = Form(""),
):
    try:
        update_room(
            db,
            room_id,
            number=number,
            notes=notes,
            is_active=(is_active == "on"),
        )
        db.commit()
    except HotelError as e:
        db.rollback()
        return RedirectResponse(
            f"/admin/hotel/rooms?error={e}", status_code=302
        )
    return RedirectResponse("/admin/hotel/rooms?saved=1", status_code=302)


@rooms_router.post("/{room_id}/delete", response_class=HTMLResponse)
def admin_rooms_delete(
    room_id: int,
    db: DBSession,
    _: User = Depends(_admin),
):
    try:
        delete_room(db, room_id)
        db.commit()
    except HotelError as e:
        db.rollback()
        return RedirectResponse(
            f"/admin/hotel/rooms?error={e}", status_code=302
        )
    return RedirectResponse("/admin/hotel/rooms?saved=1", status_code=302)


# ============================================================
# Reception: Settle room charges
# ============================================================
settle_router = APIRouter(prefix="/hotel", tags=["hotel-settle"])
_settle = require_permission(HOTEL_SETTLE)


@settle_router.get("/settle", response_class=HTMLResponse)
def hotel_settle_index(
    request: Request,
    db: DBSession,
    _: User = Depends(_settle),
):
    """قائمة الغرف التي عليها حسابات مفتوحة + أسماء النزلاء عليها."""
    open_rooms = rooms_with_open_balance(db)
    guests_by_room = {
        r.id: open_guests_for_room(db, r.id) for (r, _t, _c) in open_rooms
    }
    return templates.TemplateResponse(
        "hotel_settle_index.html",
        {
            "request": request,
            "open_rooms": open_rooms,
            "guests_by_room": guests_by_room,
            "grand_total": grand_open_total(db),
            "saved": request.query_params.get("saved"),
            "error": request.query_params.get("error"),
        },
    )


@settle_router.get("/settle/room/{room_id}", response_class=HTMLResponse)
def hotel_settle_room_page(
    request: Request,
    room_id: int,
    db: DBSession,
    _: User = Depends(_settle),
):
    room = get_room(db, room_id)
    if room is None:
        return RedirectResponse(
            "/hotel/settle?error=الغرفة غير موجودة.", status_code=302
        )
    open_list = open_charges_for_room(db, room_id)
    from modules.refunds.service import sale_outstanding_total

    charge_totals = {c.id: sale_outstanding_total(db, c.sale_id) for c in open_list}
    history = list_charges(db, room_id=room_id, settled=True, limit=50)
    methods = list_payment_methods(db, only_active=True)
    return templates.TemplateResponse(
        "hotel_settle_room.html",
        {
            "request": request,
            "room": room,
            "open_charges": open_list,
            "charge_totals": charge_totals,
            "open_total": room_open_total(db, room_id),
            "history": history,
            "methods": methods,
            "error": request.query_params.get("error"),
        },
    )


@settle_router.post("/settle/room/{room_id}/pay", response_class=HTMLResponse)
async def hotel_settle_room_pay(
    request: Request,
    room_id: int,
    db: DBSession,
    user: User = Depends(_settle),
    payment_method_id: str = Form(...),
):
    form = await request.form()
    selected = form.getlist("charge_ids")
    if not selected:
        return RedirectResponse(
            f"/hotel/settle/room/{room_id}?error=لم تختر أي فواتير.",
            status_code=302,
        )
    try:
        pm_id = int(payment_method_id)
    except ValueError:
        return RedirectResponse(
            f"/hotel/settle/room/{room_id}?error=أسلوب الدفع غير صالح.",
            status_code=302,
        )
    try:
        settle_charges(
            db,
            charge_ids=selected,
            payment_method_id=pm_id,
            user_id=user.id,
        )
        db.commit()
    except HotelError as e:
        db.rollback()
        return RedirectResponse(
            f"/hotel/settle/room/{room_id}?error={e}", status_code=302
        )
    return RedirectResponse(
        f"/hotel/settle/room/{room_id}?", status_code=302
    )
