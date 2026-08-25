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



from app.deps import DBSession, LoggedInUser, require_any_permission, require_permission

from app.jinja_env import templates

from modules.authz.capability import (
    can_hotel_settle_transfer_now,
    can_settle_from_main_treasury,
    can_settle_without_hotel_shift,
    is_restaurant_finance_view,
)

from modules.authz.models import User

from modules.authz.permissions import (

    HOTEL_ROOMS_MANAGE,

    HOTEL_SETTLE,

    HOTEL_BOOKING_MANAGE,

    HOTEL_HOUSEKEEPING,

)

from modules.hotel.shift_session import enforce_hotel_shift_session

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

    convert_open_charges_to_restaurant_pickup,

    link_open_charges_to_booking,

    list_charges,

    list_linkable_bookings_for_room,

    list_rooms,

    list_unlinked_room_receivables,

    open_charges_for_room,

    open_guests_for_room,

    room_open_total,

    rooms_with_open_balance,

    settle_charges,

    settle_index_charges_by_room,

    unlinked_room_receivables_total,

    update_room,

)

from modules.hotel.uploads import save_room_image, save_room_media

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
    from modules.hotel.apartment_icons import (
        AMENITY_EMOJI_CHOICES,
        amenity_icons_admin_labels,
        amenity_icons_context,
        apartment_icons_admin_labels,
        apartment_icons_context,
    )

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
            "apt_icons": apartment_icons_context(db),
            "apt_icon_labels": apartment_icons_admin_labels(),
            "amenity_icons": amenity_icons_context(db),
            "amenity_icon_labels": amenity_icons_admin_labels(),
            "amenity_emoji_choices": AMENITY_EMOJI_CHOICES,
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

    lock_no: str = Form(""),

    notes: str = Form(""),

    show_online: str = Form(""),

    online_description: str = Form(""),

    rooms_count: str = Form("1"),

    double_beds_count: str = Form("1"),

    single_beds_count: str = Form("0"),

    allows_infant: str = Form(""),

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

            lock_no=lock_no or None,

            notes=notes,

            image_filename=img_name,

            show_online=(show_online == "on"),

            online_description=online_description or None,

            rooms_count=rooms_count,

            double_beds_count=double_beds_count,

            single_beds_count=single_beds_count,

            allows_infant=(allows_infant == "on"),

        )

        db.commit()

    except HotelError as e:

        db.rollback()

        return RedirectResponse(

            f"/admin/hotel/rooms?error={quote(str(e))}", status_code=302

        )

    return RedirectResponse("/admin/hotel/rooms?saved=1", status_code=302)





@rooms_router.post("/{room_id}/edit", response_class=HTMLResponse)
async def admin_rooms_edit(
    room_id: int,
    db: DBSession,
    _: User = Depends(_admin),
    name_ar: str = Form(""),
    number: str = Form(...),
    nightly_price: str = Form(""),
    room_type_id: str = Form(""),
    floor: str = Form(""),
    lock_no: str = Form(""),
    notes: str = Form(""),
    is_active: str = Form(""),
    show_online: str = Form(""),
    online_description: str = Form(""),
    rooms_count: str = Form("1"),
    double_beds_count: str = Form("1"),
    single_beds_count: str = Form("0"),
    allows_infant: str = Form(""),
    amenity_icon_rooms: str = Form(""),
    amenity_icon_double: str = Form(""),
    amenity_icon_single: str = Form(""),
    amenity_icon_infant: str = Form(""),
    amenity_img_rooms: UploadFile | None = File(None),
    amenity_img_double: UploadFile | None = File(None),
    amenity_img_single: UploadFile | None = File(None),
    amenity_img_infant: UploadFile | None = File(None),
    clear_amenity_img_rooms: str = Form(""),
    clear_amenity_img_double: str = Form(""),
    clear_amenity_img_single: str = Form(""),
    clear_amenity_img_infant: str = Form(""),
    emoji_available: str = Form(""),
    emoji_occupied: str = Form(""),
    emoji_dirty: str = Form(""),
    emoji_maint: str = Form(""),
    img_available: UploadFile | None = File(None),
    img_occupied: UploadFile | None = File(None),
    img_dirty: UploadFile | None = File(None),
    img_maint: UploadFile | None = File(None),
    clear_available: str = Form(""),
    clear_occupied: str = Form(""),
    clear_dirty: str = Form(""),
    clear_maint: str = Form(""),
    image: UploadFile | None = File(None),
):
    from modules.hotel.apartment_icons import (
        ApartmentIconError,
        save_amenity_icon_emojis,
        save_amenity_icon_upload,
        save_apartment_icon_emojis,
        save_apartment_icon_upload,
    )
    from modules.settings.service import invalidate_settings_cache

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
            lock_no=lock_no if lock_no is not None else "",
            notes=notes,
            is_active=(is_active == "on"),
            show_online=(show_online == "on"),
            online_description=online_description or None,
            rooms_count=rooms_count,
            double_beds_count=double_beds_count,
            single_beds_count=single_beds_count,
            allows_infant=(allows_infant == "on"),
        )
        if img_name:
            edit_kwargs["image_filename"] = img_name
        update_room(db, room_id, **edit_kwargs)

        save_amenity_icon_emojis(
            db,
            {
                "rooms": amenity_icon_rooms,
                "double": amenity_icon_double,
                "single": amenity_icon_single,
                "infant": amenity_icon_infant,
            },
        )
        amenity_uploads = {
            "rooms": (amenity_img_rooms, clear_amenity_img_rooms == "1"),
            "double": (amenity_img_double, clear_amenity_img_double == "1"),
            "single": (amenity_img_single, clear_amenity_img_single == "1"),
            "infant": (amenity_img_infant, clear_amenity_img_infant == "1"),
        }
        for key, (up, clear) in amenity_uploads.items():
            await save_amenity_icon_upload(db, key=key, upload=up, clear=clear)

        # أيقونات حالة الكرت الكبيرة (متاحة / مشغولة / تنظيف / صيانة)
        if any(
            [
                emoji_available,
                emoji_occupied,
                emoji_dirty,
                emoji_maint,
                getattr(img_available, "filename", None),
                getattr(img_occupied, "filename", None),
                getattr(img_dirty, "filename", None),
                getattr(img_maint, "filename", None),
                clear_available,
                clear_occupied,
                clear_dirty,
                clear_maint,
            ]
        ):
            save_apartment_icon_emojis(
                db,
                {
                    "available": emoji_available,
                    "occupied": emoji_occupied,
                    "dirty": emoji_dirty,
                    "maint": emoji_maint,
                },
            )
            status_uploads = {
                "available": (img_available, clear_available == "1"),
                "occupied": (img_occupied, clear_occupied == "1"),
                "dirty": (img_dirty, clear_dirty == "1"),
                "maint": (img_maint, clear_maint == "1"),
            }
            for key, (up, clear) in status_uploads.items():
                await save_apartment_icon_upload(db, key=key, upload=up, clear=clear)

        invalidate_settings_cache()
        db.commit()
    except (HotelError, ApartmentIconError) as e:
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


def _settle_shift_dep(request: Request, db: DBSession, user: LoggedInUser) -> None:
    """تسوية الغرف تتطلب جلسة استقبال — إلا لأدمن النظام وأمين الخزينة."""
    if can_settle_without_hotel_shift(user):
        return None
    enforce_hotel_shift_session(request, db, user)
    return None


settle_router = APIRouter(
    prefix="/hotel",
    tags=["hotel-settle"],
    dependencies=[Depends(_settle_shift_dep)],
)

_settle = require_permission(HOTEL_SETTLE)


def _require_settle_transfer(user: User, request: Request | None = None) -> str | None:
    """None إن مسموح؛ وإلا رسالة خطأ عربية."""
    from modules.authz.capability import hotel_settle_transfer_blocked_reason

    session = request.session if request is not None else None
    return hotel_settle_transfer_blocked_reason(user, session)



@settle_router.get("/settle", response_class=HTMLResponse)

def hotel_settle_index(

    request: Request,

    db: DBSession,

    user: User = Depends(_settle),

):

    """قائمة الغرف التي عليها حسابات مفتوحة + أسماء النزلاء عليها."""

    open_rooms = rooms_with_open_balance(db)

    guests_by_room = {

        r.id: open_guests_for_room(db, r.id) for (r, _t, _c) in open_rooms

    }

    charges_by_room = settle_index_charges_by_room(db)

    settleable_charge_count = 0
    total_open_charge_count = 0
    for _rid, rows in charges_by_room.items():
        for ch in rows:
            total_open_charge_count += 1
            if ch.can_settle:
                settleable_charge_count += 1

    unlinked = list_unlinked_room_receivables(db)

    linked_total = grand_open_total(db)

    unlinked_total = unlinked_room_receivables_total(db)

    from modules.hotel.breakfast_settle import hotel_breakfast_included_enabled
    from modules.hotel.restaurant_settle import (
        list_restaurant_settle_targets,
        list_settle_source_methods,
    )

    methods = list_settle_source_methods(db, user=user)
    to_methods = list_restaurant_settle_targets(db)
    breakfast_included_enabled = hotel_breakfast_included_enabled(db)

    return templates.TemplateResponse(

        "hotel_settle_index.html",

        {

            "request": request,

            "open_rooms": open_rooms,

            "guests_by_room": guests_by_room,

            "charges_by_room": charges_by_room,

            "settleable_charge_count": settleable_charge_count,

            "total_open_charge_count": total_open_charge_count,

            "methods": methods,

            "to_methods": to_methods,

            "breakfast_included_enabled": breakfast_included_enabled,

            "can_settle_transfer": can_hotel_settle_transfer_now(user, request.session),
            "settle_view_only": is_restaurant_finance_view(user, request.session),
            "can_pay_from_main": can_settle_from_main_treasury(user),

            "grand_total": linked_total,

            "unlinked_total": unlinked_total,

            "combined_total": (linked_total + unlinked_total).quantize(

                Decimal("0.001")

            ),

            "unlinked_invoices": unlinked,

            "all_rooms": list_rooms(db, only_active=True),

            "saved": request.query_params.get("saved"),
            "pickup": request.query_params.get("pickup"),
            "paid": request.query_params.get("paid"),

            "error": request.query_params.get("error"),

        },

    )





def _parse_treasury_transfers_from_form(form) -> tuple[list, int | None]:
    """يبني أسطر التحويل فندق→مطعم من حقول النموذج."""
    from decimal import Decimal, InvalidOperation

    from modules.hotel.service import TreasuryTransferSplit

    xfer_from = form.getlist("xfer_from_id")
    xfer_to = form.getlist("xfer_to_id")
    xfer_amt = form.getlist("xfer_amount")
    xfer_ref = form.getlist("xfer_bank_ref")
    transfers: list[TreasuryTransferSplit] = []
    for i, (mid_f, mid_t, amt_s) in enumerate(zip(xfer_from, xfer_to, xfer_amt)):
        fs = str(mid_f or "").strip()
        ts = str(mid_t or "").strip()
        if not fs.isdigit() or not ts.isdigit():
            continue
        try:
            amt = Decimal(str(amt_s or "0").strip() or "0")
        except (InvalidOperation, ValueError):
            continue
        ref = str(xfer_ref[i] if i < len(xfer_ref) else "").strip()
        if amt > 0:
            transfers.append(
                TreasuryTransferSplit(
                    from_payment_method_id=int(fs),
                    to_payment_method_id=int(ts),
                    amount=amt,
                    bank_ref=ref,
                )
            )
    method_ids = form.getlist("pay_method_id")
    from_pm_raw = str(form.get("from_payment_method_id") or "").strip()
    if not from_pm_raw and method_ids:
        from_pm_raw = str(method_ids[0] or "").strip()
    from_pm_id = int(from_pm_raw) if from_pm_raw.isdigit() else None
    return transfers, from_pm_id


def _apply_transfer_settle(
    db: DBSession,
    *,
    charge_ids: list,
    settle_mode: str,
    user_id: int,
    transfers: list,
    from_pm_id: int | None,
    room_id: int | None = None,
) -> list:
    """يشغّل تسوية التحويل (دين نزيل أو إفطار)."""
    from modules.hotel.service import (
        settle_charges_as_hotel_breakfast_cost,
        settle_charges_to_room_account,
    )

    if settle_mode in ("breakfast", "hotel_cost"):
        from modules.hotel.breakfast_settle import (
            hotel_breakfast_included_enabled,
            list_open_breakfast_charge_ids,
        )

        if not hotel_breakfast_included_enabled(db):
            raise HotelError(
                "وضع الإفطار المشمول معطّل من إعدادات الحجز — "
                "سوِّ الفاتورة كوجبة عادية (دين على النزيل)."
            )
        breakfast_ids = list_open_breakfast_charge_ids(db, room_id=room_id)
        if not breakfast_ids:
            raise HotelError("لا توجد وجبات إفطار معلّقة قابلة للتسوية.")
        return settle_charges_as_hotel_breakfast_cost(
            db,
            charge_ids=breakfast_ids,
            user_id=user_id,
            from_payment_method_id=None,
            transfers=None,
        )
    return settle_charges_to_room_account(
        db,
        charge_ids=charge_ids,
        user_id=user_id,
        from_payment_method_id=from_pm_id if not transfers else None,
        transfers=transfers or None,
    )


@settle_router.post("/settle/bulk-pay", response_class=HTMLResponse)
async def hotel_settle_bulk_pay(
    request: Request,
    db: DBSession,
    user: User = Depends(_settle),
):
    """تسوية جماعية لعدة فواتير/غرف — نفس تحويل فندق→مطعم."""
    from urllib.parse import quote

    form = await request.form()
    settle_mode = str(form.get("settle_mode") or "transfer").strip().lower()
    if settle_mode not in ("transfer", "settle", "room", "breakfast", "hotel_cost"):
        settle_mode = "transfer"
    selected = form.getlist("charge_ids")
    if not selected and settle_mode not in ("breakfast", "hotel_cost"):
        return RedirectResponse(
            "/hotel/settle?error=" + quote("لم تختر أي فواتير."),
            status_code=302,
        )
    denied = _require_settle_transfer(user, request)
    if denied:
        return RedirectResponse(
            "/hotel/settle?error=" + quote(denied),
            status_code=302,
        )
    transfers, from_pm_id = _parse_treasury_transfers_from_form(form)
    try:
        settled = _apply_transfer_settle(
            db,
            charge_ids=selected,
            settle_mode=settle_mode,
            user_id=user.id,
            transfers=transfers,
            from_pm_id=from_pm_id,
        )
        db.commit()
    except HotelError as e:
        db.rollback()
        return RedirectResponse(
            "/hotel/settle?error=" + quote(str(e)),
            status_code=302,
        )
    except Exception:
        import logging

        logging.getLogger("pos.hotel.settle").exception(
            "Unexpected error while bulk settling charges=%s",
            selected,
        )
        db.rollback()
        return RedirectResponse(
            "/hotel/settle?error="
            + quote("حدث خطأ غير متوقع أثناء التسوية الجماعية."),
            status_code=302,
        )
    paid = (
        "bulk_breakfast"
        if settle_mode in ("breakfast", "hotel_cost")
        else "bulk"
    )
    n = len(settled or [])
    return RedirectResponse(
        f"/hotel/settle?paid={paid}&n={n}",
        status_code=302,
    )


@settle_router.post("/settle/link-sale", response_class=HTMLResponse)

def hotel_link_orphan_sale(

    request: Request,

    db: DBSession,

    user: User = Depends(_settle),

    sale_id: int = Form(...),

    room_id: int = Form(...),

    guest_name: str = Form(""),

):

    """ربط فاتورة شقة «غير مربوطة» برقم غرفة لتظهر في تسوية الغرف."""

    denied = _require_settle_transfer(user, request)
    if denied:
        from urllib.parse import quote

        return RedirectResponse(
            "/hotel/settle?error=" + quote(denied),
            status_code=302,
        )

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

    user: User = Depends(_settle),

):

    room = get_room(db, room_id)

    if room is None:

        return RedirectResponse(

            "/hotel/settle?error=الغرفة غير موجودة.", status_code=302

        )

    open_list = open_charges_for_room(db, room_id)

    from modules.refunds.service import sale_outstanding_total



    from modules.payments.service import sum_sale_payments



    from modules.hotel.breakfast_settle import (
        hotel_breakfast_included_enabled,
        sale_is_hotel_breakfast,
    )

    charge_totals = {}

    charge_paid = {}

    charge_is_breakfast = {}
    breakfast_included_enabled = hotel_breakfast_included_enabled(db)

    for c in open_list:

        charge_totals[c.id] = sale_outstanding_total(db, c.sale_id)

        charge_paid[c.id] = sum_sale_payments(db, c.sale_id)

        charge_is_breakfast[c.id] = sale_is_hotel_breakfast(db, c.sale)

    history = list_charges(db, room_id=room_id, settled=True, limit=50)

    from modules.hotel.restaurant_settle import list_settle_source_methods

    methods = list_settle_source_methods(db, user=user)
    from modules.hotel.restaurant_settle import list_restaurant_settle_targets
    from modules.payments.service import list_pos_sale_payment_methods
    from modules.platform.business_domain import BusinessDomain

    to_methods = list_restaurant_settle_targets(db)
    pickup_payment_methods = list_pos_sale_payment_methods(
        db, only_active=True, domain=BusinessDomain.RESTAURANT, user=user
    )
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

    linkable_bookings = (
        []
        if linked_booking is not None
        else list_linkable_bookings_for_room(db, room_id)
    )

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

            "charge_is_breakfast": charge_is_breakfast,
            "breakfast_included_enabled": breakfast_included_enabled,

            "open_total": room_open_total(db, room_id),

            "paid_msg": request.query_params.get("paid"),

            "history": history,

            "methods": methods,
            "to_methods": to_methods,
            "linked_booking": linked_booking,
            "linkable_bookings": linkable_bookings,
            "pickup_payment_methods": pickup_payment_methods,
            "booking_credit": booking_credit,
            "linked_ok": request.query_params.get("linked"),
            "pickup_ok": request.query_params.get("pickup"),

            "can_settle_transfer": can_hotel_settle_transfer_now(user, request.session),
            "settle_view_only": is_restaurant_finance_view(user, request.session),
            "can_pay_from_main": can_settle_from_main_treasury(user),

            "error": request.query_params.get("error"),

            "customer": customer,

            "customer_points_value": customer_points_value,

            "loyalty": loyalty,

            "loyalty_quote": loyalty_quote,

            "guest_phone_prefill": guest_phone_prefill,

            "guest_name_prefill": guest_name_prefill,

        },

    )





@settle_router.post("/settle/room/{room_id}/link-booking", response_class=HTMLResponse)
async def hotel_settle_link_booking(
    request: Request,
    room_id: int,
    db: DBSession,
    user: User = Depends(_settle),
):
    """يربط الفواتير المفتوحة على الشقة بحجز مؤكد/مقيم ثم يعيد لصفحة التسوية."""
    denied = _require_settle_transfer(user, request)
    if denied:
        return RedirectResponse(
            f"/hotel/settle/room/{room_id}?error={quote(denied)}",
            status_code=302,
        )
    form = await request.form()
    raw_bid = str(form.get("booking_id") or "").strip()
    if not raw_bid.isdigit():
        return RedirectResponse(
            f"/hotel/settle/room/{room_id}?error=اختر حجزاً للربط.",
            status_code=302,
        )
    selected = form.getlist("charge_ids")
    charge_ids = None
    if selected:
        try:
            charge_ids = [int(x) for x in selected if str(x).strip()]
        except (TypeError, ValueError):
            charge_ids = None
    try:
        link_open_charges_to_booking(
            db,
            room_id=room_id,
            booking_id=int(raw_bid),
            charge_ids=charge_ids,
        )
        db.commit()
    except HotelError as e:
        db.rollback()
        return RedirectResponse(
            f"/hotel/settle/room/{room_id}?error={e}",
            status_code=302,
        )
    return RedirectResponse(
        f"/hotel/settle/room/{room_id}?linked=1",
        status_code=302,
    )


@settle_router.post("/settle/room/{room_id}/to-pickup", response_class=HTMLResponse)
async def hotel_settle_to_pickup(
    request: Request,
    room_id: int,
    db: DBSession,
    user: User = Depends(_settle),
):
    """يحوّل فواتير الشقة العالقة إلى استلام من المطعم مع تحصيل فوري."""
    denied = _require_settle_transfer(user, request)
    if denied:
        return RedirectResponse(
            f"/hotel/settle/room/{room_id}?error={quote(denied)}",
            status_code=302,
        )
    form = await request.form()
    raw_pm = str(form.get("payment_method_id") or "").strip()
    if not raw_pm.isdigit():
        return RedirectResponse(
            f"/hotel/settle/room/{room_id}?error=اختر وسيلة دفع المطعم.",
            status_code=302,
        )
    selected = form.getlist("charge_ids")
    charge_ids = None
    if selected:
        try:
            charge_ids = [int(x) for x in selected if str(x).strip()]
        except (TypeError, ValueError):
            charge_ids = None
    try:
        n = convert_open_charges_to_restaurant_pickup(
            db,
            room_id=room_id,
            payment_method_id=int(raw_pm),
            charge_ids=charge_ids,
            user_id=user.id,
        )
        db.commit()
    except HotelError as e:
        db.rollback()
        return RedirectResponse(
            f"/hotel/settle/room/{room_id}?error={e}",
            status_code=302,
        )
    if n <= 0:
        return RedirectResponse(
            f"/hotel/settle/room/{room_id}?error=لم يُحوَّل أي طلب.",
            status_code=302,
        )
    still = open_charges_for_room(db, room_id)
    if still:
        return RedirectResponse(
            f"/hotel/settle/room/{room_id}?pickup=1",
            status_code=302,
        )
    return RedirectResponse("/hotel/settle?pickup=1", status_code=302)


@settle_router.post("/settle/room/{room_id}/pay", response_class=HTMLResponse)

async def hotel_settle_room_pay(

    request: Request,

    room_id: int,

    db: DBSession,

    user: User = Depends(_settle),

):

    from decimal import Decimal, InvalidOperation



    from modules.hotel.service import (
        PaymentSplit,
        settle_charges_with_splits,
    )



    form = await request.form()

    selected = form.getlist("charge_ids")
    settle_mode = str(form.get("settle_mode") or "transfer").strip().lower()
    if not selected and settle_mode not in ("breakfast", "hotel_cost"):
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

        if settle_mode in ("transfer", "settle", "room", "breakfast", "hotel_cost"):
            denied = _require_settle_transfer(user, request)
            if denied:
                return RedirectResponse(
                    f"/hotel/settle/room/{room_id}?error={quote(denied)}",
                    status_code=302,
                )
            transfers, from_pm_id = _parse_treasury_transfers_from_form(form)
            settled = _apply_transfer_settle(
                db,
                charge_ids=selected,
                settle_mode=settle_mode,
                user_id=user.id,
                transfers=transfers,
                from_pm_id=from_pm_id,
                room_id=room_id,
            )
        else:
            denied = _require_settle_transfer(user, request)
            if denied:
                return RedirectResponse(
                    f"/hotel/settle/room/{room_id}?error={quote(denied)}",
                    status_code=302,
                )
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

    if settle_mode in ("transfer", "settle", "room", "breakfast", "hotel_cost"):
        if settled:
            booking_id = getattr(settled[0], "booking_id", None)
            paid_flag = (
                "breakfast_settled"
                if settle_mode in ("breakfast", "hotel_cost")
                else "settled"
            )
            if booking_id:
                saved = (
                    "breakfast_settled"
                    if settle_mode in ("breakfast", "hotel_cost")
                    else "room_settled"
                )
                return RedirectResponse(
                    f"/admin/hotel/bookings/{int(booking_id)}"
                    f"?saved={saved}",
                    status_code=302,
                )
            return RedirectResponse(
                f"/hotel/settle/room/{room_id}?paid={paid_flag}",
                status_code=302,
            )
        return RedirectResponse(
            f"/hotel/settle/room/{room_id}?paid=recorded",
            status_code=302,
        )

    if still_open and len(settled) < len(selected):

        paid_q = "partial"

    elif settled:

        paid_q = "full"

    else:

        paid_q = "recorded"

    # مسار القبض القديم: طباعة إيصال بعد تحصيل من النزيل
    if settled:
        sale_id = settled[0].sale_id
        doc = "receipt"
        try:
            from modules.hotel.booking_models import BookingStatus, HotelBooking

            charge = settled[0]
            booking = None
            if getattr(charge, "booking_id", None):
                booking = db.get(HotelBooking, int(charge.booking_id))
            if booking is None and getattr(charge, "sale", None) is not None:
                bid = getattr(charge.sale, "booking_id", None)
                if bid:
                    booking = db.get(HotelBooking, int(bid))
            if booking is not None and booking.booking_status == BookingStatus.CHECKED_OUT:
                if sale_outstanding_total(db, sale_id) <= 0:
                    doc = "invoice"
        except Exception:  # noqa: BLE001
            doc = "receipt"
        back = quote(f"/hotel/settle/room/{int(room_id)}", safe="")
        return RedirectResponse(
            f"/pos/receipt/{sale_id}?doc={doc}&autoprint=1&back={back}",
            status_code=302,
        )

    return RedirectResponse(

        f"/hotel/settle/room/{room_id}?paid={paid_q}", status_code=302

    )

