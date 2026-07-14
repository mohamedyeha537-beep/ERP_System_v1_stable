"""Hotel module web routes:



- /admin/hotel/rooms          (HOTEL_ROOMS_MANAGE)  إعداد الشقق

- /hotel/settle               (HOTEL_SETTLE)        تسوية حسابات الغرف

"""



from __future__ import annotations



from decimal import Decimal, InvalidOperation

from pathlib import Path

from urllib.parse import quote



from fastapi import APIRouter, Depends, File, Form, UploadFile

from fastapi.responses import HTMLResponse, RedirectResponse

from starlette.requests import Request

from sqlalchemy import func, select



from app.deps import DBSession, require_any_permission, require_permission

from app.jinja_env import templates

from modules.authz.models import User

from modules.authz.permissions import (

    HOTEL_ROOMS_MANAGE,

    HOTEL_SETTLE,

    HOTEL_BOOKING_MANAGE,

    HOTEL_HOUSEKEEPING,

)

from modules.hotel.maintenance import MAINTENANCE_ISSUE_TYPES
from modules.settings.service import get_setting
from modules.hotel.booking_service import (
    BookingError,
    ensure_default_property,
    ensure_default_room_types,
    list_room_types,
    mark_maintenance_complete,
    set_room_maintenance_status,
)

from modules.hotel.models import HotelRoom, RoomCharge

from modules.hotel.service import (

    HotelError,

    create_room,

    delete_room,

    ensure_room_charge_for_sale,

    get_room,

    grand_open_total,

    list_charges,

    list_rooms,

    list_unlinked_room_receivables,

    open_charges_for_room,

    open_guests_for_room,

    room_open_total,

    rooms_with_open_balance,

    settle_charges,

    unlinked_room_receivables_total,

    update_room,

)

from modules.hotel.uploads import save_room_image, save_room_media

from modules.payments.service import list_hotel_settle_payment_methods



STATIC_ROOT = Path(__file__).resolve().parents[2] / "app" / "static"





# ============================================================

# Admin: Rooms management

# ============================================================

rooms_router = APIRouter(prefix="/admin/hotel/rooms", tags=["hotel-rooms"])

_admin = require_permission(HOTEL_ROOMS_MANAGE)

_maint = require_any_permission(
    HOTEL_ROOMS_MANAGE,
    HOTEL_BOOKING_MANAGE,
    HOTEL_HOUSEKEEPING,
)





def _parse_price(raw: str | None) -> Decimal | None:

    s = (raw or "").strip()

    if not s:

        return None

    try:

        val = Decimal(s)

        return val if val > 0 else None

    except InvalidOperation:

        return None





def _parse_room_type_id(raw: str | None) -> int | None:

    s = (raw or "").strip()

    if not s or not s.isdigit():

        return None

    n = int(s)

    return n if n > 0 else None





@rooms_router.get("", response_class=HTMLResponse)

def admin_rooms_page(

    request: Request,

    db: DBSession,

    _: User = Depends(_admin),

):

    ensure_default_property(db)

    ensure_default_room_types(db)

    db.commit()

    rooms = list_rooms(db)
    open_totals: dict[int, "tuple"] = {}
    for r in rooms:
        open_totals[r.id] = (
            room_open_total(db, r.id),
            len(open_charges_for_room(db, r.id)),
        )
    from modules.hotel.dashboard import build_room_dashboard

    cards, _counts = build_room_dashboard(db)
    setup_rooms = {r.id: r for r in rooms}
    from modules.hotel.booking_models import HotelBooking

    room_booking_counts = {
        int(room_id): int(count or 0)
        for room_id, count in db.execute(
            select(HotelBooking.room_id, func.count(HotelBooking.id))
            .where(HotelBooking.room_id.isnot(None))
            .group_by(HotelBooking.room_id)
        ).all()
    }
    return templates.TemplateResponse(
        "admin_hotel_rooms.html",
        {
            "request": request,
            "rooms": rooms,
            "cards": cards,
            "setup_rooms": setup_rooms,
            "room_booking_counts": room_booking_counts,
            "room_types": list_room_types(db, only_active=True),
            "open_totals": open_totals,
            "saved": request.query_params.get("saved"),
            "error": request.query_params.get("error"),
            "issue_types": MAINTENANCE_ISSUE_TYPES,
            "can_maintain": True,
            "default_maintenance_phone": get_setting(db, "hotel_maintenance_phone") or "",
            "default_maintenance_name": get_setting(db, "hotel_maintenance_name") or "",
            "maint_redirect_to": "/admin/hotel/rooms",
        },
    )





@rooms_router.post("/add", response_class=HTMLResponse)

def admin_rooms_add(

    db: DBSession,

    _: User = Depends(_admin),

    name_ar: str = Form(...),

    number: str = Form(...),

    nightly_price: str = Form(""),

    room_type_id: str = Form(""),

    floor: str = Form(""),

    notes: str = Form(""),

    show_online: str = Form(""),

    online_description: str = Form(""),

    image: UploadFile | None = File(None),

):

    img_name = None

    if image and image.filename:

        try:

            img_name = save_room_image(image, STATIC_ROOT)

        except ValueError as e:

            return RedirectResponse(

                f"/admin/hotel/rooms?error={quote(str(e))}", status_code=302

            )

    try:

        create_room(

            db,

            number=number,

            name_ar=name_ar,

            nightly_price=_parse_price(nightly_price),

            room_type_id=_parse_room_type_id(room_type_id),

            floor=floor or None,

            notes=notes,

            image_filename=img_name,

            show_online=(show_online == "on"),

            online_description=online_description or None,

        )

        db.commit()

    except HotelError as e:

        db.rollback()

        return RedirectResponse(

            f"/admin/hotel/rooms?error={quote(str(e))}", status_code=302

        )

    return RedirectResponse("/admin/hotel/rooms?saved=1", status_code=302)





@rooms_router.post("/{room_id}/edit", response_class=HTMLResponse)

def admin_rooms_edit(

    room_id: int,

    db: DBSession,

    _: User = Depends(_admin),

    name_ar: str = Form(""),

    number: str = Form(...),

    nightly_price: str = Form(""),

    room_type_id: str = Form(""),

    floor: str = Form(""),

    notes: str = Form(""),

    is_active: str = Form(""),

    show_online: str = Form(""),

    online_description: str = Form(""),

    image: UploadFile | None = File(None),

):

    img_name = None

    if image and image.filename:

        try:

            img_name = save_room_image(image, STATIC_ROOT)

        except ValueError as e:

            return RedirectResponse(

                f"/admin/hotel/rooms?error={quote(str(e))}", status_code=302

            )

    try:
        edit_kwargs = dict(
            number=number,
            name_ar=name_ar,
            nightly_price=_parse_price(nightly_price),
            clear_nightly_price=not (nightly_price or "").strip(),
            room_type_id=_parse_room_type_id(room_type_id) or 0,
            floor=floor or None,
            notes=notes,
            is_active=(is_active == "on"),
            show_online=(show_online == "on"),
            online_description=online_description or None,
        )
        if img_name:
            edit_kwargs["image_filename"] = img_name
        update_room(db, room_id, **edit_kwargs)
        db.commit()

    except HotelError as e:

        db.rollback()

        return RedirectResponse(

            f"/admin/hotel/rooms?error={quote(str(e))}", status_code=302

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

            f"/admin/hotel/rooms?error={quote(str(e))}", status_code=302

        )

    return RedirectResponse("/admin/hotel/rooms?saved=1", status_code=302)




@rooms_router.post("/{room_id}/set-maintenance", response_class=HTMLResponse)
def admin_room_set_maintenance(
    room_id: int,
    db: DBSession,
    user: User = Depends(_maint),
    issue_type: str = Form("other"),
    note: str = Form(""),
    maintenance_phone: str = Form(""),
    maintenance_name: str = Form(""),
    send_notification: str = Form(""),
    redirect_to: str = Form("/admin/hotel/rooms"),
):
    from modules.settings.service import get_setting

    reporter = (user.username or "").strip()
    back = (redirect_to or "/admin/hotel/rooms").strip()
    if not back.startswith("/admin/hotel"):
        back = "/admin/hotel/rooms"
    try:
        phone = (maintenance_phone or "").strip() or (
            get_setting(db, "hotel_maintenance_phone") or ""
        ).strip()
        set_room_maintenance_status(
            db,
            room_id,
            user_id=user.id,
            note=note,
            issue_type=issue_type or "other",
            maintenance_phone=phone,
            maintenance_staff_name=maintenance_name,
            reported_by=reporter,
            send_notification=send_notification == "on",
        )
        db.commit()
    except BookingError as e:
        db.rollback()
        sep = "&" if "?" in back else "?"
        return RedirectResponse(f"{back}{sep}error={quote(str(e))}", status_code=302)
    sep = "&" if "?" in back else "?"
    return RedirectResponse(f"{back}{sep}saved=maintenance", status_code=302)


@rooms_router.post("/{room_id}/maintenance-done", response_class=HTMLResponse)
def admin_room_maintenance_done(
    room_id: int,
    db: DBSession,
    user: User = Depends(_maint),
    redirect_to: str = Form("/admin/hotel/rooms"),
):
    back = (redirect_to or "/admin/hotel/rooms").strip()
    if not back.startswith("/admin/hotel"):
        back = "/admin/hotel/rooms"
    try:
        mark_maintenance_complete(db, room_id, user_id=user.id)
        db.commit()
    except BookingError as e:
        db.rollback()
        sep = "&" if "?" in back else "?"
        return RedirectResponse(f"{back}{sep}error={quote(str(e))}", status_code=302)
    sep = "&" if "?" in back else "?"
    return RedirectResponse(f"{back}{sep}saved=ready", status_code=302)


@rooms_router.get("/{room_id}/media", response_class=HTMLResponse)
def admin_room_media_page(
    request: Request,
    room_id: int,
    db: DBSession,
    _: User = Depends(_admin),
):
    from modules.hotel.store_models import HotelRoomMedia
    from modules.hotel.uploads import room_media_public_url

    room = db.get(HotelRoom, room_id)
    if room is None:
        return RedirectResponse("/admin/hotel/rooms?error=الشقة غير موجودة", status_code=302)
    items = list(
        db.scalars(
            select(HotelRoomMedia)
            .where(HotelRoomMedia.room_id == room_id)
            .order_by(HotelRoomMedia.sort_order.asc(), HotelRoomMedia.id.asc())
        ).all()
    )
    media = [
        {
            "row": m,
            "url": room_media_public_url(m.filename),
        }
        for m in items
    ]
    return templates.TemplateResponse(
        "admin_hotel_room_media.html",
        {
            "request": request,
            "room": room,
            "media": media,
            "saved": request.query_params.get("saved"),
            "error": request.query_params.get("error"),
        },
    )


@rooms_router.post("/{room_id}/media/upload", response_class=HTMLResponse)
def admin_room_media_upload(
    room_id: int,
    db: DBSession,
    _: User = Depends(_admin),
    media_kind: str = Form("IMAGE"),
    caption: str = Form(""),
    sort_order: str = Form("0"),
    upload: UploadFile | None = File(None),
):
    from modules.hotel.store_models import HotelRoomMedia, RoomMediaKind

    room = db.get(HotelRoom, room_id)
    if room is None:
        return RedirectResponse("/admin/hotel/rooms?error=الشقة غير موجودة", status_code=302)
    if not upload or not upload.filename:
        return RedirectResponse(
            f"/admin/hotel/rooms/{room_id}/media?error={quote('اختر ملفاً')}",
            status_code=302,
        )
    try:
        fname = save_room_media(upload, STATIC_ROOT, kind=media_kind)
        kind = RoomMediaKind.VIDEO if media_kind.upper() == "VIDEO" else RoomMediaKind.IMAGE
        try:
            order = int((sort_order or "0").strip())
        except ValueError:
            order = 0
        db.add(
            HotelRoomMedia(
                room_id=room_id,
                kind=kind,
                filename=fname,
                caption=(caption or "").strip() or None,
                sort_order=order,
            )
        )
        db.commit()
    except ValueError as e:
        db.rollback()
        return RedirectResponse(
            f"/admin/hotel/rooms/{room_id}/media?error={quote(str(e))}",
            status_code=302,
        )
    return RedirectResponse(f"/admin/hotel/rooms/{room_id}/media?saved=1", status_code=302)


@rooms_router.post("/{room_id}/media/{media_id}/delete", response_class=HTMLResponse)
def admin_room_media_delete(
    room_id: int,
    media_id: int,
    db: DBSession,
    _: User = Depends(_admin),
):
    from modules.hotel.store_models import HotelRoomMedia

    row = db.get(HotelRoomMedia, media_id)
    if row is not None and row.room_id == room_id:
        db.delete(row)
        db.commit()
    return RedirectResponse(f"/admin/hotel/rooms/{room_id}/media?saved=deleted", status_code=302)




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

    unlinked = list_unlinked_room_receivables(db)

    linked_total = grand_open_total(db)

    unlinked_total = unlinked_room_receivables_total(db)

    return templates.TemplateResponse(

        "hotel_settle_index.html",

        {

            "request": request,

            "open_rooms": open_rooms,

            "guests_by_room": guests_by_room,

            "grand_total": linked_total,

            "unlinked_total": unlinked_total,

            "combined_total": (linked_total + unlinked_total).quantize(

                Decimal("0.001")

            ),

            "unlinked_invoices": unlinked,

            "all_rooms": list_rooms(db, only_active=True),

            "saved": request.query_params.get("saved"),

            "error": request.query_params.get("error"),

        },

    )





@settle_router.post("/settle/link-sale", response_class=HTMLResponse)

def hotel_link_orphan_sale(

    db: DBSession,

    user: User = Depends(_settle),

    sale_id: int = Form(...),

    room_id: int = Form(...),

    guest_name: str = Form(""),

):

    """ربط فاتورة شقة «غير مربوطة» برقم غرفة لتظهر في تسوية الغرف."""

    try:

        ensure_room_charge_for_sale(

            db,

            sale_id=sale_id,

            room_id=room_id,

            guest_name=guest_name or None,

            user_id=user.id,

        )

        db.commit()

    except HotelError as e:

        db.rollback()

        return RedirectResponse(

            f"/hotel/settle?error={quote(str(e))}",

            status_code=302,

        )

    room = get_room(db, room_id)

    num = room.number if room else str(room_id)

    return RedirectResponse(

        f"/hotel/settle?saved=1&linked={sale_id}&room={num}",

        status_code=302,

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



    from modules.payments.service import sum_sale_payments



    charge_totals = {}

    charge_paid = {}

    for c in open_list:

        charge_totals[c.id] = sale_outstanding_total(db, c.sale_id)

        charge_paid[c.id] = sum_sale_payments(db, c.sale_id)

    history = list_charges(db, room_id=room_id, settled=True, limit=50)

    methods = list_hotel_settle_payment_methods(db, only_active=True)
    linked_booking = None
    for c in open_list:
        if c.booking_id:
            from modules.hotel.booking_service import get_booking

            linked_booking = get_booking(db, int(c.booking_id))
            if linked_booking is not None:
                break
    if linked_booking is None:
        from modules.hotel.booking_service import active_booking_for_room

        linked_booking = active_booking_for_room(db, room_id)

    from modules.customers.service import (

        get_customer,

        loyalty_redeem_quote,

        loyalty_settings,

        points_to_dinars,

    )



    customer = None

    customer_points_value = None

    loyalty_quote = None

    from modules.platform.business_domain import BusinessDomain

    loyalty = loyalty_settings(db, BusinessDomain.HOTEL)

    guest_phone_prefill = ""

    guest_name_prefill = ""

    for c in open_list:

        sale = c.sale

        if sale is None:

            continue

        if not guest_name_prefill and c.guest_name_snapshot:

            guest_name_prefill = (c.guest_name_snapshot or "").strip()

        if sale.customer_id and customer is None:

            customer = get_customer(db, int(sale.customer_id))

            if customer:

                guest_phone_prefill = customer.phone or ""

                if customer.name:

                    guest_name_prefill = customer.name

    if customer and Decimal(str(customer.points_balance or 0)) > 0:

        customer_points_value = points_to_dinars(db, customer.points_balance, BusinessDomain.HOTEL)

    if customer and loyalty["enabled"]:

        loyalty_quote = loyalty_redeem_quote(

            db, customer=customer, sale_total=room_open_total(db, room_id), domain=BusinessDomain.HOTEL

        )

    booking_credit = Decimal("0")
    if linked_booking is not None:
        from modules.hotel.folio import build_guest_account

        booking_credit = build_guest_account(db, int(linked_booking.id)).amount_credit

    return templates.TemplateResponse(

        "hotel_settle_room.html",

        {

            "request": request,

            "room": room,

            "open_charges": open_list,

            "charge_totals": charge_totals,

            "charge_paid": charge_paid,

            "open_total": room_open_total(db, room_id),

            "paid_msg": request.query_params.get("paid"),

            "history": history,

            "methods": methods,
            "linked_booking": linked_booking,
            "booking_credit": booking_credit,

            "error": request.query_params.get("error"),

            "customer": customer,

            "customer_points_value": customer_points_value,

            "loyalty": loyalty,

            "loyalty_quote": loyalty_quote,

            "guest_phone_prefill": guest_phone_prefill,

            "guest_name_prefill": guest_name_prefill,

        },

    )





@settle_router.post("/settle/room/{room_id}/pay", response_class=HTMLResponse)

async def hotel_settle_room_pay(

    request: Request,

    room_id: int,

    db: DBSession,

    user: User = Depends(_settle),

):

    from decimal import Decimal, InvalidOperation



    from modules.hotel.service import PaymentSplit, settle_charges_with_splits



    form = await request.form()

    selected = form.getlist("charge_ids")

    if not selected:

        return RedirectResponse(

            f"/hotel/settle/room/{room_id}?error=لم تختر أي فواتير.",

            status_code=302,

        )

    guest_phone = str(form.get("guest_phone") or "").strip()

    guest_name = str(form.get("guest_name") or "").strip()

    use_loyalty_redeem = str(form.get("use_loyalty_redeem") or "") in (

        "on",

        "1",

        "true",

        "yes",

    )

    method_ids = form.getlist("pay_method_id")

    amounts = form.getlist("pay_amount")

    payments: list[PaymentSplit] = []

    for mid, amt_s in zip(method_ids, amounts):

        mid_s = str(mid or "").strip()

        if not mid_s:

            continue

        try:

            pm_id = int(mid_s)

            amt = Decimal(str(amt_s or "0").strip() or "0")

        except (ValueError, InvalidOperation):

            continue

        if amt > 0:

            payments.append(PaymentSplit(pm_id, amt))

    try:

        settled = settle_charges_with_splits(

            db,

            charge_ids=selected,

            payments=payments,

            user_id=user.id,

            guest_phone=guest_phone or None,

            guest_name=guest_name or None,

            use_loyalty_redeem=use_loyalty_redeem,

        )

        db.commit()

    except HotelError as e:

        db.rollback()

        return RedirectResponse(

            f"/hotel/settle/room/{room_id}?error={e}", status_code=302

        )

    except Exception:

        import logging



        logging.getLogger("pos.hotel.settle").exception(

            "Unexpected error while settling room payment room_id=%s charges=%s",

            room_id,

            selected,

        )

        db.rollback()

        return RedirectResponse(

            f"/hotel/settle/room/{room_id}?error=حدث خطأ غير متوقع أثناء تسجيل الدفعة. حاول مرة أخرى أو راجع سجل الخادم.",

            status_code=302,

        )

    from modules.refunds.service import sale_outstanding_total



    still_open = any(

        sale_outstanding_total(db, c.sale_id) > 0

        for c in open_charges_for_room(db, room_id)

    )

    if still_open and len(settled) < len(selected):

        paid_q = "partial"

    elif settled:

        paid_q = "full"

    else:

        paid_q = "recorded"

    return RedirectResponse(

        f"/hotel/settle/room/{room_id}?paid={paid_q}", status_code=302

    )

