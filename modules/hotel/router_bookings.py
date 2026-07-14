"""إدارة الحجوزات — استقبال."""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select

from app.deps import DBSession, require_module, require_permission
from app.jinja_env import templates
from modules.authz.models import User
from modules.authz.permissions import (
    HOTEL_BOOKING_CHECKIN,
    HOTEL_BOOKING_CHECKOUT,
    HOTEL_BOOKING_CHECKOUT_BALANCE,
    HOTEL_BOOKING_CREATE,
    HOTEL_BOOKING_MANAGE,
    HOTEL_BOOKING_VIEW,
    HOTEL_FINANCE_CLOSE,
    HOTEL_HOUSEKEEPING,
    HOTEL_ROOMS_MANAGE,
)
from modules.hotel.availability import calendar_bookings

from modules.hotel.booking_models import (
    BookingStatus,
    GuestType,
    HotelAuditLog,
    HotelBookingStatusLog,
    QuotationStatus,
    RecordKind,
    RoomPhysicalStatus,
)
from modules.hotel.booking_labels import (
    QUOTATION_NEXT_STATUSES,
    QUOTATION_STATUS_COLORS,
    QUOTATION_STATUS_LABELS,
    guest_type_label,
    quotation_status_label,
)
from modules.hotel.booking_service import (
    BookingError,
    add_booking_service,
    adjust_stay_dates,
    available_rooms_for_change,
    cancel_booking,
    change_room,
    check_in_booking,
    check_out_booking,
    confirm_booking,
    convert_quotation_to_booking,
    parse_staying_guests,
    create_booking,
    validate_booking_prepayment,
    ensure_default_cancellation_policy,
    ensure_default_property,
    ensure_default_room_types,
    extend_stay,
    get_booking,
    get_booking_prepayment_percent,
    list_bookings,
    list_quotations,
    list_room_types,
    mark_no_show,
    mark_room_clean,
    mark_room_maintenance,
    mark_maintenance_complete,
    parse_booking_prepayment_percent,
    prepayment_percent_choices,
    prepayment_percent_label,
    record_payment,
    refund_payment,
    update_quotation_status,
    _recalc_payment_status,
    room_type_can_delete,
    room_type_usage,
    set_room_type_active,
    delete_room_type,
)
from modules.hotel.dashboard import build_front_desk_summary, build_room_dashboard
from modules.hotel.finance_service import close_daily, finance_enabled, issue_checkout_invoice, occupancy_stats
from modules.hotel.extra_services import (
    delete_service_catalog_item,
    get_service_catalog_item,
    list_service_catalog,
    save_service_catalog_item,
    set_service_catalog_active,
)
from modules.hotel.folio import build_folio, build_guest_account
from modules.hotel.models import HotelRoom, RoomCharge
from modules.hotel.service import list_rooms
from modules.hotel.guest_documents import attach_staying_guest_documents, validate_staying_guests_form
from modules.hotel.guest_form_options import (
    guest_form_context,
    get_guest_id_types,
    save_guest_id_types,
)
from modules.payments.service import list_hotel_settle_payment_methods
from modules.platform.module_registry import HOTEL_BOOKING, HOTEL_FINANCE

bookings_router = APIRouter(prefix="/admin/hotel", tags=["hotel-bookings"])

_view = require_permission(HOTEL_BOOKING_VIEW)
_create = require_permission(HOTEL_BOOKING_CREATE)
_manage = require_permission(HOTEL_BOOKING_MANAGE)
_checkin = require_permission(HOTEL_BOOKING_CHECKIN)
_checkout = require_permission(HOTEL_BOOKING_CHECKOUT)
_checkout_bal = require_permission(HOTEL_BOOKING_CHECKOUT_BALANCE)
_hk = require_permission(HOTEL_HOUSEKEEPING)
_fin = require_permission(HOTEL_FINANCE_CLOSE)
_rooms = require_permission(HOTEL_ROOMS_MANAGE)
_mod_booking = require_module(HOTEL_BOOKING)


def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        return None


def _boot(db: DBSession) -> None:
    ensure_default_property(db)
    ensure_default_room_types(db)
    ensure_default_cancellation_policy(db)


def _parse_guest_type(raw: str | None) -> GuestType:
    try:
        return GuestType((raw or GuestType.INDIVIDUAL.value).strip().upper())
    except ValueError:
        return GuestType.INDIVIDUAL


def _staying_guests_from_form(form) -> list:
    return parse_staying_guests(
        names=form.getlist("staying_guest_name"),
        id_numbers=form.getlist("staying_guest_id_number"),
        id_types=form.getlist("staying_guest_id_type"),
        nationalities=form.getlist("staying_guest_nationality"),
        addresses=form.getlist("staying_guest_address"),
        phones=form.getlist("staying_guest_phone"),
    )


_BOOKING_DRAFT_KEY = "hotel_booking_form_draft"
_QUOTATION_DRAFT_KEY = "hotel_quotation_form_draft"


def _form_str(form, key: str, default: str = "") -> str:
    val = form.get(key, default)
    if val is None:
        return default
    return str(val)


def _staying_guests_draft_from_form(form) -> list[dict]:
    names = form.getlist("staying_guest_name")
    id_nums = form.getlist("staying_guest_id_number")
    id_types = form.getlist("staying_guest_id_type")
    nationalities = form.getlist("staying_guest_nationality")
    addresses = form.getlist("staying_guest_address")
    phones = form.getlist("staying_guest_phone")
    guests: list[dict] = []
    for i, name in enumerate(names):
        guests.append(
            {
                "name": str(name or ""),
                "id_number": str(id_nums[i] if i < len(id_nums) else ""),
                "id_type": str(id_types[i] if i < len(id_types) else ""),
                "nationality": str(nationalities[i] if i < len(nationalities) else ""),
                "address": str(addresses[i] if i < len(addresses) else ""),
                "phone": str(phones[i] if i < len(phones) else ""),
            }
        )
    return guests


def _booking_form_draft_from_form(form, *, is_quotation: bool = False) -> dict:
    draft = {
        "guest_type": _form_str(form, "guest_type", "COMPANY" if is_quotation else "INDIVIDUAL"),
        "guest_name": _form_str(form, "guest_name"),
        "guest_phone": _form_str(form, "guest_phone"),
        "guest_email": _form_str(form, "guest_email"),
        "company_name": _form_str(form, "company_name"),
        "company_tax_id": _form_str(form, "company_tax_id"),
        "company_address": _form_str(form, "company_address"),
        "company_contact_name": _form_str(form, "company_contact_name"),
        "company_contact_phone": _form_str(form, "company_contact_phone"),
        "company_contact_email": _form_str(form, "company_contact_email"),
        "check_in": _form_str(form, "check_in"),
        "check_out": _form_str(form, "check_out"),
        "room_id": _form_str(form, "room_id"),
        "adults": _form_str(form, "adults", "1"),
        "children": _form_str(form, "children", "0"),
        "discount": _form_str(form, "discount", "0"),
        "internal_notes": _form_str(form, "internal_notes"),
        "staying_guests": _staying_guests_draft_from_form(form),
    }
    if is_quotation:
        draft["quotation_valid_until"] = _form_str(form, "quotation_valid_until")
        draft["quotation_notes"] = _form_str(form, "quotation_notes")
    else:
        draft["deposit"] = _form_str(form, "deposit", "0")
        draft["pay_method_id"] = _form_str(form, "pay_method_id")
        draft["auto_confirm"] = _form_str(form, "auto_confirm") == "on"
    return draft


def _stash_booking_draft(request: Request, form, *, is_quotation: bool = False) -> None:
    key = _QUOTATION_DRAFT_KEY if is_quotation else _BOOKING_DRAFT_KEY
    request.session[key] = _booking_form_draft_from_form(form, is_quotation=is_quotation)


def _clear_booking_draft(request: Request, *, is_quotation: bool = False) -> None:
    request.session.pop(_QUOTATION_DRAFT_KEY if is_quotation else _BOOKING_DRAFT_KEY, None)


def _room_rate_for(room: HotelRoom) -> Decimal:
    override = getattr(room, "nightly_price", None)
    if override is not None and Decimal(str(override or 0)) > 0:
        return Decimal(str(override)).quantize(Decimal("0.001"))
    if room.room_type is not None:
        return Decimal(str(room.room_type.base_price or 0)).quantize(Decimal("0.001"))
    return Decimal("0")


def _rooms_form_context(db: DBSession, *, pre_room: HotelRoom | None = None) -> dict:
    active_rooms = list_rooms(db, only_active=True)
    pct = get_booking_prepayment_percent(db)
    return {
        **guest_form_context(db),
        "rooms": active_rooms,
        "rooms_type_map": {
            str(r.id): {
                "type_id": r.room_type_id,
                "type_name": r.room_type.name_ar if r.room_type else None,
                "price": f"{_room_rate_for(r):.3f}" if r.room_type else None,
                "type_price": f"{r.room_type.base_price:.3f}" if r.room_type else None,
                "has_room_price": bool(
                    getattr(r, "nightly_price", None) is not None
                    and Decimal(str(r.nightly_price or 0)) > 0
                ),
            }
            for r in active_rooms
        },
        "methods": list_hotel_settle_payment_methods(db, only_active=True),
        "pre_room": pre_room,
        "today": date.today(),
        "prepayment_percent": pct,
        "prepayment_percent_label": prepayment_percent_label(pct),
    }


@bookings_router.get("", response_class=HTMLResponse, dependencies=[Depends(_mod_booking)])
def hotel_root_redirect():
    return RedirectResponse("/admin/hotel/dashboard", status_code=302)


@bookings_router.get("/dashboard", response_class=HTMLResponse, dependencies=[Depends(_mod_booking)])
def hotel_dashboard(
    request: Request,
    db: DBSession,
    user: User = Depends(_view),
):
    from modules.authz.kiosk import requires_hotel_shift_pin
    from modules.authz.service import user_has_permission
    from modules.hotel.shift_session import session_hotel_employee_id

    if requires_hotel_shift_pin(user) and session_hotel_employee_id(request) is None:
        return RedirectResponse("/admin/hotel/pin", status_code=302)

    from modules.hotel.maintenance import MAINTENANCE_ISSUE_TYPES
    from modules.settings.service import get_setting

    _boot(db)
    db.commit()
    today = date.today()
    cards, counts = build_room_dashboard(db, day=today)
    summary = build_front_desk_summary(db, cards=cards, counts=counts, day=today)
    pct = get_booking_prepayment_percent(db)
    can_maintain = any(
        user_has_permission(user, p)
        for p in (HOTEL_ROOMS_MANAGE, HOTEL_BOOKING_MANAGE, HOTEL_HOUSEKEEPING)
    )
    return templates.TemplateResponse(
        "hotel/dashboard.html",
        {
            "request": request,
            "today": today,
            "cards": cards,
            "counts": counts,
            "summary": summary,
            "prepayment_percent": pct,
            "prepayment_percent_label": prepayment_percent_label(pct),
            "can_maintain": can_maintain,
            "issue_types": MAINTENANCE_ISSUE_TYPES,
            "default_maintenance_phone": get_setting(db, "hotel_maintenance_phone") or "",
            "default_maintenance_name": get_setting(db, "hotel_maintenance_name") or "",
            "maint_redirect_to": "/admin/hotel/dashboard",
            "saved": request.query_params.get("saved"),
            "error": request.query_params.get("error"),
        },
    )


@bookings_router.get("/bookings", response_class=HTMLResponse, dependencies=[Depends(_mod_booking)])
def bookings_list(
    request: Request,
    db: DBSession,
    _: User = Depends(_view),
    status: str | None = Query(None),
    room_id: int | None = Query(None),
):
    _boot(db)
    db.commit()
    st = None
    if status:
        try:
            st = BookingStatus(status)
        except ValueError:
            st = None
    items = list_bookings(db, status=st, room_id=room_id)
    today = date.today()
    arrivals = [
        b for b in items
        if b.check_in == today
        and b.booking_status in (BookingStatus.CONFIRMED, BookingStatus.PENDING)
    ]
    departures = [b for b in items if b.check_out == today and b.booking_status == BookingStatus.CHECKED_IN]
    filter_room = db.get(HotelRoom, room_id) if room_id else None
    return templates.TemplateResponse(
        "hotel/bookings_list.html",
        {
            "request": request,
            "items": items,
            "arrivals": arrivals,
            "departures": departures,
            "status_filter": status,
            "room_filter": filter_room,
            "saved": request.query_params.get("saved"),
            "error": request.query_params.get("error"),
        },
    )


@bookings_router.get("/bookings/calendar", response_class=HTMLResponse, dependencies=[Depends(_mod_booking)])
def bookings_calendar(
    request: Request,
    db: DBSession,
    _: User = Depends(_view),
    start: str | None = Query(None),
    days: int = Query(14, ge=7, le=31),
):
    _boot(db)
    start_d = _parse_date(start) or date.today()
    from datetime import timedelta

    end_d = start_d + timedelta(days=days)
    date_cols = [start_d + timedelta(days=i) for i in range(days)]
    bookings = calendar_bookings(db, start=start_d, end=end_d)
    rooms = list_rooms(db, only_active=True)
    return templates.TemplateResponse(
        "hotel/bookings_calendar.html",
        {
            "request": request,
            "start": start_d,
            "end": end_d,
            "days": days,
            "date_cols": date_cols,
            "bookings": bookings,
            "rooms": rooms,
        },
    )


@bookings_router.get("/bookings/new", response_class=HTMLResponse, dependencies=[Depends(_mod_booking)])
def booking_new_page(
    request: Request,
    db: DBSession,
    _: User = Depends(_create),
    room_id: int | None = Query(None),
):
    _boot(db)
    pre_room = db.get(HotelRoom, room_id) if room_id else None
    ctx = _rooms_form_context(db, pre_room=pre_room)
    if request.query_params.get("error"):
        form_draft = request.session.get(_BOOKING_DRAFT_KEY)
    else:
        _clear_booking_draft(request, is_quotation=False)
        form_draft = None
    return templates.TemplateResponse(
        "hotel/booking_form.html",
        {
            "request": request,
            "booking": None,
            "form_mode": "booking",
            "error": request.query_params.get("error"),
            "form_draft": form_draft,
            "today": date.today(),
            **ctx,
        },
    )


@bookings_router.get("/quotations", response_class=HTMLResponse, dependencies=[Depends(_mod_booking)])
def quotations_list(
    request: Request,
    db: DBSession,
    _: User = Depends(_view),
    status: str | None = Query(None),
):
    _boot(db)
    db.commit()
    qst = None
    if status:
        try:
            qst = QuotationStatus(status)
        except ValueError:
            qst = None
    items = list_quotations(db, quotation_status=qst)
    counts = {s.value: 0 for s in QuotationStatus}
    all_q = list_quotations(db, limit=500)
    for q in all_q:
        if q.quotation_status:
            counts[q.quotation_status.value] = counts.get(q.quotation_status.value, 0) + 1
    return templates.TemplateResponse(
        "hotel/quotations_list.html",
        {
            "request": request,
            "items": items,
            "status_filter": status,
            "counts": counts,
            "quotation_labels": QUOTATION_STATUS_LABELS,
            "saved": request.query_params.get("saved"),
            "error": request.query_params.get("error"),
        },
    )


@bookings_router.get("/quotations/new", response_class=HTMLResponse, dependencies=[Depends(_mod_booking)])
def quotation_new_page(
    request: Request,
    db: DBSession,
    _: User = Depends(_create),
    room_id: int | None = Query(None),
):
    _boot(db)
    pre_room = db.get(HotelRoom, room_id) if room_id else None
    ctx = _rooms_form_context(db, pre_room=pre_room)
    if request.query_params.get("error"):
        form_draft = request.session.get(_QUOTATION_DRAFT_KEY)
    else:
        _clear_booking_draft(request, is_quotation=True)
        form_draft = None
    return templates.TemplateResponse(
        "hotel/booking_form.html",
        {
            "request": request,
            "booking": None,
            "form_mode": "quotation",
            "error": request.query_params.get("error"),
            "form_draft": form_draft,
            "today": date.today(),
            **ctx,
        },
    )


@bookings_router.post("/bookings/save", dependencies=[Depends(_mod_booking)])
async def booking_save(
    request: Request,
    db: DBSession,
    user: User = Depends(_create),
):
    form = await request.form()
    guest_type = _form_str(form, "guest_type", "INDIVIDUAL")
    guest_name = _form_str(form, "guest_name")
    guest_phone = _form_str(form, "guest_phone")
    guest_email = _form_str(form, "guest_email")
    company_name = _form_str(form, "company_name")
    company_tax_id = _form_str(form, "company_tax_id")
    company_address = _form_str(form, "company_address")
    company_contact_name = _form_str(form, "company_contact_name")
    company_contact_phone = _form_str(form, "company_contact_phone")
    company_contact_email = _form_str(form, "company_contact_email")
    guest_id_number = _form_str(form, "guest_id_number")
    guest_id_type = _form_str(form, "guest_id_type")
    guest_address = _form_str(form, "guest_address")
    guest_nationality = _form_str(form, "guest_nationality")
    check_in = _form_str(form, "check_in")
    check_out = _form_str(form, "check_out")
    room_id = _form_str(form, "room_id")
    adults = _form_str(form, "adults", "1")
    children = _form_str(form, "children", "0")
    discount = _form_str(form, "discount", "0")
    deposit = _form_str(form, "deposit", "0")
    pay_method_id = _form_str(form, "pay_method_id")
    internal_notes = _form_str(form, "internal_notes")
    should_confirm = _form_str(form, "auto_confirm") == "on"

    def _fail(msg: str) -> RedirectResponse:
        _stash_booking_draft(request, form, is_quotation=False)
        return RedirectResponse(f"/admin/hotel/bookings/new?error={quote(msg)}", status_code=302)

    ci = _parse_date(check_in)
    co = _parse_date(check_out)
    if not ci or not co:
        return _fail("تواريخ غير صالحة")
    if not room_id.strip().isdigit():
        return _fail("اختر الشقة")
    rid = int(room_id)
    room = db.get(HotelRoom, rid)
    if room is None or not room.is_active:
        return _fail("الشقة غير صالحة")
    if not room.room_type_id:
        return _fail("الشقة بدون نوع غرفة — عيّنه من إعداد الشقق")
    nightly = (
        Decimal(str(room.nightly_price)).quantize(Decimal("0.001"))
        if room.nightly_price is not None and Decimal(str(room.nightly_price or 0)) > 0
        else None
    )
    try:
        validate_staying_guests_form(form, require_documents=True)
        validate_booking_prepayment(
            db,
            amount=deposit,
            payment_method_id=pay_method_id,
            room_type_id=room.room_type_id,
            check_in=ci,
            check_out=co,
            discount_amount=Decimal(discount or "0"),
            nightly_rate=nightly,
        )
        booking = create_booking(
            db,
            guest_name=guest_name,
            guest_phone=guest_phone or None,
            guest_email=guest_email or None,
            guest_type=_parse_guest_type(guest_type),
            company_name=company_name or None,
            company_tax_id=company_tax_id or None,
            company_address=company_address or None,
            company_contact_name=company_contact_name or None,
            company_contact_phone=company_contact_phone or None,
            company_contact_email=company_contact_email or None,
            guest_id_number=guest_id_number or None,
            guest_id_type=guest_id_type or None,
            guest_address=guest_address or None,
            guest_nationality=guest_nationality or None,
            check_in=ci,
            check_out=co,
            room_type_id=room.room_type_id,
            room_id=rid,
            adults=int(adults or 1),
            children=int(children or 0),
            nightly_rate=nightly,
            discount_amount=Decimal(discount or "0"),
            internal_notes=internal_notes,
            user_id=user.id,
            auto_confirm=False,
            record_kind=RecordKind.BOOKING,
            staying_guests=_staying_guests_from_form(form),
        )
        attach_staying_guest_documents(db, booking, form, require_documents=True)
        dep = Decimal(deposit or "0").quantize(Decimal("0.001"))
        if dep > 0:
            record_payment(
                db,
                booking.id,
                amount=dep,
                payment_method_id=int(pay_method_id.strip()),
                is_deposit=True,
                user_id=user.id,
                note="دفع عند إنشاء الحجز",
            )
        _recalc_payment_status(booking)
        if should_confirm:
            confirm_booking(db, booking.id, user_id=user.id)
        db.commit()
        _clear_booking_draft(request, is_quotation=False)
    except (BookingError, InvalidOperation, ValueError) as e:
        db.rollback()
        return _fail(str(e))
    return RedirectResponse(f"/admin/hotel/bookings/{booking.id}?saved=1", status_code=302)


@bookings_router.post("/quotations/save", dependencies=[Depends(_mod_booking)])
async def quotation_save(
    request: Request,
    db: DBSession,
    user: User = Depends(_create),
):
    form = await request.form()

    def _fail(msg: str) -> RedirectResponse:
        _stash_booking_draft(request, form, is_quotation=True)
        return RedirectResponse(f"/admin/hotel/quotations/new?error={quote(msg)}", status_code=302)

    guest_type = _form_str(form, "guest_type", "COMPANY")
    guest_name = _form_str(form, "guest_name")
    guest_phone = _form_str(form, "guest_phone")
    guest_email = _form_str(form, "guest_email")
    company_name = _form_str(form, "company_name")
    company_tax_id = _form_str(form, "company_tax_id")
    company_address = _form_str(form, "company_address")
    company_contact_name = _form_str(form, "company_contact_name")
    company_contact_phone = _form_str(form, "company_contact_phone")
    company_contact_email = _form_str(form, "company_contact_email")
    check_in = _form_str(form, "check_in")
    check_out = _form_str(form, "check_out")
    room_id = _form_str(form, "room_id")
    adults = _form_str(form, "adults", "1")
    children = _form_str(form, "children", "0")
    discount = _form_str(form, "discount", "0")
    quotation_valid_until = _form_str(form, "quotation_valid_until")
    quotation_notes = _form_str(form, "quotation_notes")
    internal_notes = _form_str(form, "internal_notes")

    ci = _parse_date(check_in)
    co = _parse_date(check_out)
    if not ci or not co:
        return _fail("تواريخ غير صالحة")
    if not room_id.strip().isdigit():
        return _fail("اختر الشقة")
    rid = int(room_id)
    room = db.get(HotelRoom, rid)
    if room is None or not room.is_active:
        return _fail("الشقة غير صالحة")
    if not room.room_type_id:
        return _fail("الشقة بدون نوع غرفة — عيّنه من إعداد الشقق")
    nightly = (
        Decimal(str(room.nightly_price)).quantize(Decimal("0.001"))
        if room.nightly_price is not None and Decimal(str(room.nightly_price or 0)) > 0
        else None
    )
    valid_until = _parse_date(quotation_valid_until)
    try:
        booking = create_booking(
            db,
            guest_name=guest_name,
            guest_phone=guest_phone or None,
            guest_email=guest_email or None,
            guest_type=_parse_guest_type(guest_type),
            record_kind=RecordKind.QUOTATION,
            company_name=company_name or None,
            company_tax_id=company_tax_id or None,
            company_address=company_address or None,
            company_contact_name=company_contact_name or None,
            company_contact_phone=company_contact_phone or None,
            company_contact_email=company_contact_email or None,
            check_in=ci,
            check_out=co,
            room_type_id=room.room_type_id,
            room_id=rid,
            adults=int(adults or 1),
            children=int(children or 0),
            nightly_rate=nightly,
            discount_amount=Decimal(discount or "0"),
            quotation_valid_until=valid_until,
            quotation_notes=quotation_notes or None,
            internal_notes=internal_notes,
            user_id=user.id,
            auto_confirm=False,
            staying_guests=_staying_guests_from_form(form),
        )
        attach_staying_guest_documents(db, booking, form, require_documents=False)
        db.commit()
        _clear_booking_draft(request, is_quotation=True)
    except (BookingError, InvalidOperation, ValueError) as e:
        db.rollback()
        return _fail(str(e))
    return RedirectResponse(f"/admin/hotel/bookings/{booking.id}?saved=1", status_code=302)


@bookings_router.post("/bookings/{booking_id}/quotation-status", dependencies=[Depends(_mod_booking)])
def booking_quotation_status(
    booking_id: int,
    db: DBSession,
    user: User = Depends(_manage),
    new_status: str = Form(...),
    note: str = Form(""),
):
    try:
        st = QuotationStatus(new_status.strip().upper())
        update_quotation_status(db, booking_id, new_status=st, user_id=user.id, note=note or None)
        db.commit()
    except (BookingError, ValueError) as e:
        db.rollback()
        return RedirectResponse(f"/admin/hotel/bookings/{booking_id}?error={quote(str(e))}", status_code=302)
    return RedirectResponse(f"/admin/hotel/bookings/{booking_id}?saved=1", status_code=302)


@bookings_router.post("/bookings/{booking_id}/convert-quotation", dependencies=[Depends(_mod_booking)])
def booking_convert_quotation(
    booking_id: int,
    db: DBSession,
    user: User = Depends(_manage),
    deposit: str = Form("0"),
    pay_method_id: str = Form(""),
    auto_confirm: str = Form("on"),
):
    try:
        convert_quotation_to_booking(
            db,
            booking_id,
            user_id=user.id,
            auto_confirm=auto_confirm == "on",
            require_payment=True,
            payment_amount=Decimal(deposit or "0"),
            payment_method_id=int(pay_method_id.strip()) if pay_method_id.strip().isdigit() else None,
        )
        db.commit()
    except (BookingError, InvalidOperation, ValueError) as e:
        db.rollback()
        return RedirectResponse(f"/admin/hotel/bookings/{booking_id}?error={quote(str(e))}", status_code=302)
    return RedirectResponse(f"/admin/hotel/bookings/{booking_id}?saved=1&converted=1", status_code=302)


@bookings_router.get("/bookings/{booking_id}", response_class=HTMLResponse, dependencies=[Depends(_mod_booking)])
def booking_detail(
    request: Request,
    booking_id: int,
    db: DBSession,
    user: User = Depends(_view),
):
    booking = get_booking(db, booking_id)
    if booking is None:
        return RedirectResponse("/admin/hotel/bookings?error=الحجز غير موجود", status_code=302)
    folio = build_folio(db, booking_id)
    guest_account = build_guest_account(db, booking_id)
    change_rooms = available_rooms_for_change(db, booking)
    room_assignments = sorted(
        booking.room_assignments,
        key=lambda a: (a.created_at or a.id, a.id),
        reverse=True,
    )
    room_charges = list(
        db.scalars(
            select(RoomCharge)
            .where(RoomCharge.booking_id == booking_id)
            .order_by(RoomCharge.id.desc())
        ).all()
    )
    from modules.payments.service import sum_sale_payments
    from modules.refunds.service import sale_outstanding_total

    charge_totals = {}
    charge_paid = {}
    charge_due = {}
    for charge in room_charges:
        total = Decimal(str(charge.sale.total or 0)) if charge.sale else Decimal("0")
        charge_totals[charge.id] = total
        charge_paid[charge.id] = sum_sale_payments(db, charge.sale_id)
        charge_due[charge.id] = sale_outstanding_total(db, charge.sale_id)
    logs = list(
        db.scalars(
            select(HotelBookingStatusLog)
            .where(HotelBookingStatusLog.booking_id == booking_id)
            .order_by(HotelBookingStatusLog.id.desc())
        ).all()
    )
    audit = list(
        db.scalars(
            select(HotelAuditLog)
            .where(
                HotelAuditLog.entity_type == "booking",
                HotelAuditLog.entity_id == booking_id,
            )
            .order_by(HotelAuditLog.id.desc())
            .limit(30)
        ).all()
    )
    from modules.authz.service import user_has_permission
    from modules.hotel.booking_debts import list_booking_debts

    booking_debts = list_booking_debts(db, booking_id)
    return templates.TemplateResponse(
        "hotel/booking_detail.html",
        {
            "request": request,
            "booking": booking,
            "folio": folio,
            "guest_account": guest_account,
            "booking_debts": booking_debts,
            "can_checkout_with_balance": user_has_permission(
                user, HOTEL_BOOKING_CHECKOUT_BALANCE
            ),
            "change_rooms": change_rooms,
            "room_assignments": room_assignments,
            "room_charges": room_charges,
            "charge_totals": charge_totals,
            "charge_paid": charge_paid,
            "charge_due": charge_due,
            "logs": logs,
            "audit": audit,
            "rooms": list_rooms(db, only_active=True),
            "methods": list_hotel_settle_payment_methods(db, only_active=True),
            "saved": request.query_params.get("saved"),
            "converted": request.query_params.get("converted"),
            "error": request.query_params.get("error"),
            "quotation_labels": QUOTATION_STATUS_LABELS,
            "quotation_colors": QUOTATION_STATUS_COLORS,
            "quotation_next": QUOTATION_NEXT_STATUSES,
            "guest_type_label": guest_type_label,
            "quotation_status_label": quotation_status_label,
            "prepayment_percent": get_booking_prepayment_percent(db),
            "prepayment_percent_label": prepayment_percent_label(
                get_booking_prepayment_percent(db)
            ),
            "today": date.today(),
            "service_catalog": list_service_catalog(db, only_active=True),
            **__import__(
                "modules.receipt_whatsapp.service",
                fromlist=["hotel_whatsapp_detail_ctx"],
            ).hotel_whatsapp_detail_ctx(db, booking),
        },
    )


@bookings_router.get("/settings", response_class=HTMLResponse, dependencies=[Depends(_mod_booking)])
def hotel_booking_settings_page(
    request: Request,
    db: DBSession,
    _: User = Depends(_rooms),
):
    from modules.customers.referral_service import referral_settings
    from modules.platform.business_domain import BusinessDomain
    from modules.printing.models import Printer
    from modules.settings.service import (
        PAPER_SIZES,
        get_receipt_paper_size,
        get_receipt_printer_id,
        get_setting,
    )

    pct = get_booking_prepayment_percent(db)
    id_types = get_guest_id_types(db)
    printers = list(db.scalars(select(Printer).where(Printer.is_active.is_(True)).order_by(Printer.name)).all())
    return templates.TemplateResponse(
        "hotel/settings.html",
        {
            "request": request,
            "prepayment_percent": pct,
            "prepayment_choices": prepayment_percent_choices(),
            "hotel_online_staff_phone": get_setting(db, "hotel_online_staff_phone", "") or "",
            "guest_id_types_text": "\n".join(id_types),
            "loyalty": referral_settings(db, BusinessDomain.HOTEL),
            "domain_label": "الفندق",
            "paper_sizes": PAPER_SIZES,
            "hotel_print_paper": get_receipt_paper_size(db, BusinessDomain.HOTEL),
            "hotel_receipt_printer_id": get_receipt_printer_id(db, BusinessDomain.HOTEL),
            "receipt_printers": printers,
            "service_catalog": list_service_catalog(db, only_active=False),
            **guest_form_context(db),
            "saved": request.query_params.get("saved"),
            "error": request.query_params.get("error"),
        },
    )


@bookings_router.post("/settings", dependencies=[Depends(_mod_booking)])
def hotel_booking_settings_save(
    db: DBSession,
    _: User = Depends(_rooms),
    section: str = Form("prepay"),
    prepayment_percent: str = Form("100"),
    hotel_online_staff_phone: str = Form(""),
    hotel_guest_id_types: str = Form(""),
    enabled: str = Form(""),
    earn_per_dinar: str = Form("1"),
    redeem_value_per_point: str = Form("0.1"),
    min_points_to_redeem: str = Form("50"),
    loyalty_max_redeem_percent: str = Form(""),
    referral_enabled: str = Form(""),
    referral_referrer_points: str = Form("50"),
    referral_buyer_points: str = Form("25"),
    referral_min_sale_total: str = Form("0"),
    referral_max_uses_per_code: str = Form("1"),
    hotel_print_paper_size: str = Form("A5"),
    hotel_receipt_printer_id: str = Form(""),
):
    from modules.platform.business_domain import BusinessDomain
    from modules.platform.domain_loyalty import (
        save_loyalty_settings_for_domain,
        save_referral_settings_for_domain,
    )
    from modules.settings.service import invalidate_settings_cache, normalize_paper, set_setting

    if section == "store":
        set_setting(db, "hotel_online_staff_phone", (hotel_online_staff_phone or "").strip()[:40])
        invalidate_settings_cache()
        db.commit()
        return RedirectResponse("/admin/hotel/settings?saved=store", status_code=302)
    if section == "id_types":
        save_guest_id_types(db, hotel_guest_id_types)
        invalidate_settings_cache()
        db.commit()
        return RedirectResponse("/admin/hotel/settings?saved=id_types", status_code=302)
    if section == "loyalty":
        save_loyalty_settings_for_domain(
            db,
            BusinessDomain.HOTEL,
            enabled=enabled == "on",
            earn_per_dinar=earn_per_dinar,
            redeem_value_per_point=redeem_value_per_point,
            min_points_to_redeem=min_points_to_redeem,
            max_redeem_percent=loyalty_max_redeem_percent,
        )
        save_referral_settings_for_domain(
            db,
            BusinessDomain.HOTEL,
            enabled=referral_enabled == "on",
            referrer_points=referral_referrer_points,
            buyer_points=referral_buyer_points,
            min_sale_total=referral_min_sale_total,
            max_uses_per_code=referral_max_uses_per_code,
        )
        invalidate_settings_cache()
        db.commit()
        return RedirectResponse("/admin/hotel/settings?saved=loyalty", status_code=302)
    if section == "print":
        set_setting(
            db,
            "hotel_print_paper_size",
            normalize_paper(hotel_print_paper_size, "A5"),
        )
        rp = (hotel_receipt_printer_id or "").strip()
        saved_rp = ""
        if rp.isdigit():
            from modules.printing.models import Printer

            pid = int(rp)
            if db.get(Printer, pid) is not None:
                saved_rp = str(pid)
        set_setting(db, "hotel_receipt_printer_id", saved_rp)
        invalidate_settings_cache()
        db.commit()
        return RedirectResponse("/admin/hotel/settings?saved=print", status_code=302)
    from modules.hotel.booking_service import PREPAYMENT_SETTING_KEY

    pct = parse_booking_prepayment_percent(prepayment_percent)
    set_setting(db, PREPAYMENT_SETTING_KEY, str(pct))
    invalidate_settings_cache()
    db.commit()
    return RedirectResponse("/admin/hotel/settings?saved=1", status_code=302)


@bookings_router.post("/bookings/{booking_id}/confirm", dependencies=[Depends(_mod_booking)])
def booking_confirm(booking_id: int, db: DBSession, user: User = Depends(_manage)):
    try:
        confirm_booking(db, booking_id, user_id=user.id)
        db.commit()
    except BookingError as e:
        db.rollback()
        return RedirectResponse(f"/admin/hotel/bookings/{booking_id}?error={e}", status_code=302)
    return RedirectResponse(f"/admin/hotel/bookings/{booking_id}?saved=1", status_code=302)


@bookings_router.post("/bookings/{booking_id}/check-in", dependencies=[Depends(_mod_booking)])
def booking_check_in(
    booking_id: int,
    db: DBSession,
    user: User = Depends(_checkin),
    room_id: int = Form(...),
    actual_arrival: str = Form(""),
):
    arrival = _parse_date(actual_arrival) if actual_arrival.strip() else None
    try:
        check_in_booking(
            db,
            booking_id,
            room_id=room_id,
            user_id=user.id,
            actual_arrival=arrival,
        )
        db.commit()
    except BookingError as e:
        db.rollback()
        return RedirectResponse(f"/admin/hotel/bookings/{booking_id}?error={e}", status_code=302)
    return RedirectResponse(f"/admin/hotel/bookings/{booking_id}?saved=1", status_code=302)


@bookings_router.post("/bookings/{booking_id}/check-out", dependencies=[Depends(_mod_booking)])
def booking_check_out(
    booking_id: int,
    db: DBSession,
    user: User = Depends(_checkout),
    allow_balance: str = Form(""),
    post_as_debt: str = Form(""),
    debt_note: str = Form(""),
    actual_departure: str = Form(""),
):
    allow = allow_balance == "on"
    as_debt = post_as_debt == "on"
    departure = _parse_date(actual_departure) if actual_departure.strip() else None
    try:
        from modules.authz.service import user_has_permission

        if (allow or as_debt) and not user_has_permission(
            user, HOTEL_BOOKING_CHECKOUT_BALANCE
        ):
            raise BookingError("لا تملك صلاحية المغادرة مع متبقٍ أو ترحيل دين.")
        check_out_booking(
            db,
            booking_id,
            user_id=user.id,
            allow_balance=allow and not as_debt,
            post_as_debt=as_debt,
            debt_note=debt_note.strip() or None,
            actual_departure=departure,
        )
        if finance_enabled(db):
            issue_checkout_invoice(db, booking_id, user_id=user.id)
            from modules.hotel.finance_service import try_post_booking_gl

            try_post_booking_gl(db, booking_id)
        db.commit()
    except BookingError as e:
        db.rollback()
        return RedirectResponse(f"/admin/hotel/bookings/{booking_id}?error={e}", status_code=302)
    suffix = "&settlement=1" if departure else ""
    return RedirectResponse(
        f"/admin/hotel/bookings/{booking_id}?saved=checkout{suffix}",
        status_code=302,
    )


@bookings_router.get("/bookings/{booking_id}/departure-preview", dependencies=[Depends(_mod_booking)])
def booking_departure_preview(
    booking_id: int,
    db: DBSession,
    _: User = Depends(_view),
    actual_departure: str = Query(""),
):
    from modules.hotel.departure_settlement import preview_departure_settlement

    booking = get_booking(db, booking_id)
    if booking is None:
        return JSONResponse({"ok": False, "error": "الحجز غير موجود."}, status_code=404)
    dep = _parse_date(actual_departure) if actual_departure.strip() else None
    if dep is None:
        return JSONResponse({"ok": False, "error": "تاريخ المغادرة مطلوب."}, status_code=400)
    try:
        preview = preview_departure_settlement(db, booking, actual_departure=dep)
        return JSONResponse({"ok": True, "preview": preview.to_dict()})
    except BookingError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@bookings_router.get("/bookings/{booking_id}/departure-settlement", response_class=HTMLResponse, dependencies=[Depends(_mod_booking)])
def booking_departure_settlement_page(
    request: Request,
    booking_id: int,
    db: DBSession,
    _: User = Depends(_view),
    actual_departure: str = Query(""),
):
    from modules.branding.service import get_hotel_branding, hotel_display_name
    from modules.hotel.departure_settlement import preview_departure_settlement

    booking = get_booking(db, booking_id)
    if booking is None:
        return RedirectResponse("/admin/hotel/bookings?error=الحجز غير موجود", status_code=302)
    dep = _parse_date(actual_departure) if actual_departure.strip() else booking.check_out
    try:
        preview = preview_departure_settlement(db, booking, actual_departure=dep)
    except BookingError as exc:
        return RedirectResponse(
            f"/admin/hotel/bookings/{booking_id}?error={quote(str(exc))}",
            status_code=302,
        )
    folio = build_folio(db, booking_id)
    guest_account = build_guest_account(db, booking_id)
    return templates.TemplateResponse(
        "hotel/departure_settlement.html",
        {
            "request": request,
            "booking": booking,
            "preview": preview,
            "folio": folio,
            "guest_account": guest_account,
            "hotel_brand": get_hotel_branding(db),
            "store_name": hotel_display_name(db),
        },
    )


@bookings_router.post("/bookings/{booking_id}/adjust-stay", dependencies=[Depends(_mod_booking)])
def booking_adjust_stay(
    booking_id: int,
    db: DBSession,
    user: User = Depends(_manage),
    new_check_in: str = Form(""),
    new_check_out: str = Form(""),
):
    ci = _parse_date(new_check_in) if new_check_in.strip() else None
    co = _parse_date(new_check_out) if new_check_out.strip() else None
    try:
        adjust_stay_dates(
            db,
            booking_id,
            new_check_in=ci,
            new_check_out=co,
            user_id=user.id,
        )
        db.commit()
    except BookingError as e:
        db.rollback()
        return RedirectResponse(f"/admin/hotel/bookings/{booking_id}?error={e}", status_code=302)
    return RedirectResponse(f"/admin/hotel/bookings/{booking_id}?saved=1", status_code=302)


@bookings_router.post("/bookings/{booking_id}/cancel", dependencies=[Depends(_mod_booking)])
def booking_cancel(
    booking_id: int,
    db: DBSession,
    user: User = Depends(_manage),
    reason: str = Form(""),
):
    try:
        cancel_booking(db, booking_id, user_id=user.id, reason=reason or None)
        db.commit()
    except BookingError as e:
        db.rollback()
        return RedirectResponse(f"/admin/hotel/bookings/{booking_id}?error={e}", status_code=302)
    return RedirectResponse(f"/admin/hotel/bookings/{booking_id}?saved=1", status_code=302)


@bookings_router.post("/bookings/{booking_id}/no-show", dependencies=[Depends(_mod_booking)])
def booking_no_show(booking_id: int, db: DBSession, user: User = Depends(_manage)):
    try:
        mark_no_show(db, booking_id, user_id=user.id)
        db.commit()
    except BookingError as e:
        db.rollback()
        return RedirectResponse(f"/admin/hotel/bookings/{booking_id}?error={e}", status_code=302)
    return RedirectResponse(f"/admin/hotel/bookings/{booking_id}?saved=1", status_code=302)


@bookings_router.post("/bookings/{booking_id}/extend", dependencies=[Depends(_mod_booking)])
def booking_extend(
    booking_id: int,
    db: DBSession,
    user: User = Depends(_manage),
    new_check_out: str = Form(...),
):
    co = _parse_date(new_check_out)
    if not co:
        return RedirectResponse(f"/admin/hotel/bookings/{booking_id}?error=تاريخ غير صالح", status_code=302)
    try:
        extend_stay(db, booking_id, co, user_id=user.id)
        db.commit()
    except BookingError as e:
        db.rollback()
        return RedirectResponse(f"/admin/hotel/bookings/{booking_id}?error={e}", status_code=302)
    return RedirectResponse(f"/admin/hotel/bookings/{booking_id}?saved=1", status_code=302)


@bookings_router.post("/bookings/{booking_id}/change-room", dependencies=[Depends(_mod_booking)])
def booking_change_room(
    booking_id: int,
    db: DBSession,
    user: User = Depends(_manage),
    new_room_id: int = Form(...),
    reason: str = Form(""),
    transfer_date: str = Form(""),
):
    td = _parse_date(transfer_date) if transfer_date.strip() else None
    try:
        change_room(
            db,
            booking_id,
            new_room_id,
            reason=reason or None,
            transfer_date=td,
            user_id=user.id,
        )
        db.commit()
    except BookingError as e:
        db.rollback()
        return RedirectResponse(f"/admin/hotel/bookings/{booking_id}?error={e}", status_code=302)
    return RedirectResponse(f"/admin/hotel/bookings/{booking_id}?saved=1", status_code=302)


@bookings_router.post("/bookings/{booking_id}/payment", dependencies=[Depends(_mod_booking)])
def booking_payment(
    booking_id: int,
    db: DBSession,
    user: User = Depends(_create),
    amount: str = Form(...),
    payment_method_id: int = Form(...),
    is_deposit: str = Form(""),
):
    try:
        record_payment(
            db,
            booking_id,
            amount=Decimal(amount),
            payment_method_id=payment_method_id,
            is_deposit=is_deposit == "on",
            user_id=user.id,
        )
        db.commit()
    except (BookingError, InvalidOperation) as e:
        db.rollback()
        return RedirectResponse(f"/admin/hotel/bookings/{booking_id}?error={e}", status_code=302)
    return RedirectResponse(f"/admin/hotel/bookings/{booking_id}?saved=1", status_code=302)


@bookings_router.post("/bookings/{booking_id}/debts/{debt_id}/collect", dependencies=[Depends(_mod_booking)])
def booking_debt_collect(
    booking_id: int,
    debt_id: int,
    db: DBSession,
    user: User = Depends(_create),
    payment_method_id: int = Form(...),
    note: str = Form(""),
):
    from modules.hotel.booking_debts import collect_booking_debt

    try:
        debt = collect_booking_debt(
            db,
            debt_id,
            payment_method_id=payment_method_id,
            user_id=user.id,
            note=note.strip() or None,
        )
        if debt.booking_id != booking_id:
            raise BookingError("الدين لا يخص هذا الحجز.")
        db.commit()
    except (BookingError, InvalidOperation) as e:
        db.rollback()
        return RedirectResponse(f"/admin/hotel/bookings/{booking_id}?error={e}", status_code=302)
    return RedirectResponse(f"/admin/hotel/bookings/{booking_id}?saved=debt_collected", status_code=302)


@bookings_router.post("/bookings/{booking_id}/debts/{debt_id}/write-off", dependencies=[Depends(_mod_booking)])
def booking_debt_write_off(
    booking_id: int,
    debt_id: int,
    db: DBSession,
    user: User = Depends(_manage),
    reason: str = Form(""),
):
    from modules.hotel.booking_debts import write_off_booking_debt

    try:
        debt = write_off_booking_debt(
            db,
            debt_id,
            reason=reason.strip() or None,
            user_id=user.id,
        )
        if debt.booking_id != booking_id:
            raise BookingError("الدين لا يخص هذا الحجز.")
        db.commit()
    except BookingError as e:
        db.rollback()
        return RedirectResponse(f"/admin/hotel/bookings/{booking_id}?error={e}", status_code=302)
    return RedirectResponse(f"/admin/hotel/bookings/{booking_id}?saved=debt_written_off", status_code=302)


@bookings_router.post("/bookings/{booking_id}/refund-credit", dependencies=[Depends(_mod_booking)])
def booking_refund_credit(
    booking_id: int,
    db: DBSession,
    user: User = Depends(_manage),
    payment_id: int = Form(...),
    amount: str = Form(...),
    reason: str = Form(""),
):
    try:
        booking = get_booking(db, booking_id)
        if booking is None:
            raise BookingError("الحجز غير موجود.")
        guest_account = build_guest_account(db, booking_id)
        amt = Decimal(amount or "0").quantize(Decimal("0.001"))
        if amt <= 0:
            raise BookingError("مبلغ الاسترداد غير صالح.")
        if amt > guest_account.amount_credit + Decimal("0.001"):
            raise BookingError("المبلغ يتجاوز رصيد الزبون (ما له).")
        refund_payment(
            db,
            payment_id,
            amount=amt,
            reason=(reason or "").strip() or "استرداد بعد مغادرة مبكرة",
            user_id=user.id,
        )
        db.commit()
    except (BookingError, InvalidOperation) as e:
        db.rollback()
        return RedirectResponse(f"/admin/hotel/bookings/{booking_id}?error={e}", status_code=302)
    return RedirectResponse(f"/admin/hotel/bookings/{booking_id}?saved=refunded", status_code=302)


@bookings_router.post("/bookings/{booking_id}/service", dependencies=[Depends(_mod_booking)])
def booking_add_service(
    booking_id: int,
    db: DBSession,
    user: User = Depends(_create),
    catalog_service_id: str = Form(""),
    quantity: str = Form("1"),
    unit_price: str = Form(...),
):
    try:
        if not catalog_service_id.strip().isdigit():
            raise BookingError("اختر الخدمة من القائمة.")
        cat = get_service_catalog_item(db, int(catalog_service_id))
        if cat is None or not cat.is_active:
            raise BookingError("الخدمة غير متاحة.")
        add_booking_service(
            db,
            booking_id,
            name_ar=cat.name_ar,
            quantity=Decimal(quantity),
            unit_price=Decimal(unit_price),
            product_id=cat.product_id,
            user_id=user.id,
        )
        db.commit()
    except (BookingError, InvalidOperation) as e:
        db.rollback()
        return RedirectResponse(f"/admin/hotel/bookings/{booking_id}?error={e}", status_code=302)
    return RedirectResponse(f"/admin/hotel/bookings/{booking_id}?saved=1", status_code=302)


@bookings_router.get("/extra-services")
def extra_services_redirect(request: Request):
    qp = request.query_params
    saved = qp.get("saved")
    error = qp.get("error")
    if saved in ("1", "services", "true", "yes"):
        url = "/admin/hotel/settings?saved=services#hotel-extra-services"
    elif error:
        url = f"/admin/hotel/settings?error={quote(error)}#hotel-extra-services"
    else:
        url = "/admin/hotel/settings#hotel-extra-services"
    return RedirectResponse(url, status_code=302)


@bookings_router.post("/extra-services/save", dependencies=[Depends(_mod_booking)])
def extra_service_save(
    db: DBSession,
    _: User = Depends(_rooms),
    item_id: str = Form(""),
    name_ar: str = Form(...),
    default_price: str = Form("0"),
    sort_order: str = Form("0"),
    is_active: str = Form(""),
):
    try:
        editing = item_id.strip().isdigit()
        save_service_catalog_item(
            db,
            name_ar=name_ar,
            default_price=Decimal(default_price or "0"),
            item_id=int(item_id) if editing else None,
            sort_order=int(sort_order or 0),
            is_active=is_active in ("1", "on", "true", "yes") if editing else True,
        )
        db.commit()
    except (BookingError, InvalidOperation) as e:
        db.rollback()
        return RedirectResponse(f"/admin/hotel/settings?error={quote(str(e))}", status_code=302)
    return RedirectResponse("/admin/hotel/settings?saved=services", status_code=302)


@bookings_router.post("/extra-services/{item_id}/toggle", dependencies=[Depends(_mod_booking)])
def extra_service_toggle(
    item_id: int,
    db: DBSession,
    _: User = Depends(_rooms),
    active: str = Form(""),
):
    try:
        set_service_catalog_active(db, item_id, active=active in ("1", "on", "true", "yes"))
        db.commit()
    except BookingError as e:
        db.rollback()
        return RedirectResponse(f"/admin/hotel/settings?error={quote(str(e))}", status_code=302)
    return RedirectResponse("/admin/hotel/settings?saved=services", status_code=302)


@bookings_router.post("/extra-services/{item_id}/delete", dependencies=[Depends(_mod_booking)])
def extra_service_delete(item_id: int, db: DBSession, _: User = Depends(_rooms)):
    try:
        delete_service_catalog_item(db, item_id)
        db.commit()
    except BookingError as e:
        db.rollback()
        return RedirectResponse(f"/admin/hotel/settings?error={quote(str(e))}", status_code=302)
    return RedirectResponse("/admin/hotel/settings?saved=services", status_code=302)


@bookings_router.get("/room-types", response_class=HTMLResponse, dependencies=[Depends(_mod_booking)])
def room_types_page(request: Request, db: DBSession, _: User = Depends(_rooms)):
    _boot(db)
    items = []
    for rt in list_room_types(db, only_active=False):
        usage = room_type_usage(db, rt.id)
        items.append(
            {
                "room_type": rt,
                "usage": usage,
                "can_delete": room_type_can_delete(db, rt.id),
            }
        )
    return templates.TemplateResponse(
        "hotel/room_types.html",
        {
            "request": request,
            "items": items,
            "saved": request.query_params.get("saved"),
            "error": request.query_params.get("error"),
        },
    )


@bookings_router.post("/room-types/save", dependencies=[Depends(_mod_booking)])
def room_type_save(
    db: DBSession,
    _: User = Depends(_rooms),
    type_id: str = Form(""),
    name_ar: str = Form(...),
    code: str = Form(""),
    capacity_adults: str = Form("2"),
    capacity_children: str = Form("0"),
    base_price: str = Form("0"),
    is_active: str = Form(""),
):
    from modules.hotel.booking_models import HotelRoomType

    try:
        editing = type_id.strip().isdigit()
        if editing:
            rt = db.get(HotelRoomType, int(type_id))
            if rt is None:
                raise BookingError("النوع غير موجود.")
        else:
            rt = HotelRoomType(property_id=1)
            db.add(rt)
        rt.name_ar = name_ar.strip()
        rt.code = code.strip() or None
        rt.capacity_adults = int(capacity_adults or 2)
        rt.capacity_children = int(capacity_children or 0)
        rt.base_price = Decimal(base_price or "0")
        if editing:
            rt.is_active = is_active in ("1", "on", "true", "yes")
        else:
            rt.is_active = True
        db.commit()
    except (BookingError, InvalidOperation) as e:
        db.rollback()
        return RedirectResponse(f"/admin/hotel/room-types?error={e}", status_code=302)
    return RedirectResponse("/admin/hotel/room-types?saved=1", status_code=302)


@bookings_router.post("/room-types/{type_id}/toggle", dependencies=[Depends(_mod_booking)])
def room_type_toggle(
    type_id: int,
    db: DBSession,
    _: User = Depends(_rooms),
    active: str = Form(""),
):
    try:
        set_room_type_active(db, type_id, active=active in ("1", "on", "true", "yes"))
        db.commit()
    except BookingError as e:
        db.rollback()
        return RedirectResponse(f"/admin/hotel/room-types?error={e}", status_code=302)
    return RedirectResponse("/admin/hotel/room-types?saved=1", status_code=302)


@bookings_router.post("/room-types/{type_id}/delete", dependencies=[Depends(_mod_booking)])
def room_type_delete(type_id: int, db: DBSession, _: User = Depends(_rooms)):
    try:
        delete_room_type(db, type_id)
        db.commit()
    except BookingError as e:
        db.rollback()
        return RedirectResponse(f"/admin/hotel/room-types?error={e}", status_code=302)
    return RedirectResponse("/admin/hotel/room-types?saved=1", status_code=302)


@bookings_router.get("/housekeeping", response_class=HTMLResponse, dependencies=[Depends(_mod_booking)])
def housekeeping_page(request: Request, db: DBSession, _: User = Depends(_hk)):
    from modules.hotel.maintenance import MAINTENANCE_ISSUE_TYPES
    from modules.settings.service import get_setting

    dirty = list(
        db.scalars(
            select(HotelRoom).where(
                HotelRoom.physical_status == RoomPhysicalStatus.DIRTY,
                HotelRoom.is_active.is_(True),
            ).order_by(HotelRoom.number)
        ).all()
    )
    maintenance = list(
        db.scalars(
            select(HotelRoom).where(
                HotelRoom.physical_status == RoomPhysicalStatus.MAINTENANCE,
                HotelRoom.is_active.is_(True),
            ).order_by(HotelRoom.number)
        ).all()
    )
    maintenance_staff: list[dict] = []
    try:
        from modules.hr.models import Employee, EmployeeStatus

        for emp in db.scalars(
            select(Employee)
            .where(Employee.status == EmployeeStatus.ACTIVE)
            .order_by(Employee.full_name_ar)
        ).all():
            if (emp.phone or "").strip():
                maintenance_staff.append(
                    {
                        "id": emp.id,
                        "name": emp.full_name_ar or "",
                        "phone": emp.phone or "",
                    }
                )
    except Exception:  # noqa: BLE001
        pass
    return templates.TemplateResponse(
        "hotel/housekeeping.html",
        {
            "request": request,
            "dirty_rooms": dirty,
            "maintenance_rooms": maintenance,
            "issue_types": MAINTENANCE_ISSUE_TYPES,
            "default_maintenance_phone": get_setting(db, "hotel_maintenance_phone", ""),
            "default_maintenance_name": get_setting(db, "hotel_maintenance_name", ""),
            "maintenance_staff": maintenance_staff,
            "saved": request.query_params.get("saved"),
            "error": request.query_params.get("error"),
        },
    )


@bookings_router.post("/housekeeping/settings", dependencies=[Depends(_mod_booking)])
def housekeeping_settings_save(
    db: DBSession,
    _: User = Depends(_hk),
    maintenance_phone: str = Form(""),
    maintenance_name: str = Form(""),
):
    from modules.settings.service import set_setting

    set_setting(db, "hotel_maintenance_phone", (maintenance_phone or "").strip()[:40])
    set_setting(db, "hotel_maintenance_name", (maintenance_name or "").strip()[:80])
    db.commit()
    return RedirectResponse("/admin/hotel/housekeeping?saved=settings", status_code=302)


@bookings_router.post("/housekeeping/{room_id}/clean", dependencies=[Depends(_mod_booking)])
def housekeeping_clean(room_id: int, db: DBSession, user: User = Depends(_hk)):
    try:
        mark_room_clean(db, room_id, user_id=user.id)
        db.commit()
    except BookingError as e:
        db.rollback()
        return RedirectResponse(f"/admin/hotel/housekeeping?error={e}", status_code=302)
    return RedirectResponse("/admin/hotel/housekeeping?saved=clean", status_code=302)


@bookings_router.post("/housekeeping/{room_id}/maintenance", dependencies=[Depends(_mod_booking)])
def housekeeping_maintenance(
    room_id: int,
    db: DBSession,
    user: User = Depends(_hk),
    issue_type: str = Form(""),
    note: str = Form(""),
    maintenance_phone: str = Form(""),
    maintenance_name: str = Form(""),
    employee_id: str = Form(""),
):
    reporter = (user.username or "").strip()
    eid = int(employee_id) if (employee_id or "").strip().isdigit() else None
    try:
        mark_room_maintenance(
            db,
            room_id,
            user_id=user.id,
            note=note,
            issue_type=issue_type,
            maintenance_phone=maintenance_phone,
            maintenance_staff_name=maintenance_name,
            employee_id=eid,
            reported_by=reporter,
            send_notification=True,
        )
        db.commit()
    except BookingError as e:
        db.rollback()
        return RedirectResponse(f"/admin/hotel/housekeeping?error={e}", status_code=302)
    return RedirectResponse("/admin/hotel/housekeeping?saved=maintenance", status_code=302)


@bookings_router.post("/housekeeping/{room_id}/maintenance-done", dependencies=[Depends(_mod_booking)])
def housekeeping_maintenance_done(room_id: int, db: DBSession, user: User = Depends(_hk)):
    try:
        mark_maintenance_complete(db, room_id, user_id=user.id)
        db.commit()
    except BookingError as e:
        db.rollback()
        return RedirectResponse(f"/admin/hotel/housekeeping?error={e}", status_code=302)
    return RedirectResponse("/admin/hotel/housekeeping?saved=ready", status_code=302)


@bookings_router.get("/reports", response_class=HTMLResponse, dependencies=[Depends(_mod_booking)])
def hotel_reports(request: Request, db: DBSession, _: User = Depends(_view)):
    from modules.gl.service import is_gl_enabled
    from modules.hotel.security_report_service import (
        contact_channel_label,
        guest_scope_label,
        list_security_authorities,
    )

    today = date.today()
    occ = occupancy_stats(db, on_date=today)
    qp = request.query_params
    gl_gap_raw = qp.get("gl_gap")
    gl_gap = None
    if gl_gap_raw not in (None, ""):
        try:
            gl_gap = Decimal(str(gl_gap_raw))
        except Exception:
            gl_gap = None
    backfill_raw = qp.get("gl_backfill")
    gl_backfill = int(backfill_raw) if backfill_raw and backfill_raw.isdigit() else 0
    closing_date_raw = qp.get("closing_date") or ""
    authorities = []
    for a in list_security_authorities(db, only_active=True):
        authorities.append(
            {
                "id": a.id,
                "name_ar": a.name_ar,
                "guest_scope_label": guest_scope_label(a.guest_scope),
            }
        )
    security_sent = qp.get("security_sent") == "1"
    security_sent_authority = qp.get("security_authority") or ""
    security_sent_channel = qp.get("security_channel") or ""
    return templates.TemplateResponse(
        "hotel/reports.html",
        {
            "request": request,
            "occ": occ,
            "today": today,
            "finance_on": finance_enabled(db),
            "gl_enabled": is_gl_enabled(db),
            "error": qp.get("error"),
            "saved": qp.get("saved"),
            "closing_date": closing_date_raw or today.isoformat(),
            "gl_gap": gl_gap,
            "gl_backfill": gl_backfill,
            "security_authorities": authorities,
            "security_sent": security_sent,
            "security_sent_authority": security_sent_authority,
            "security_sent_channel": security_sent_channel,
        },
    )


def _security_report_dates(from_date: str, to_date: str) -> tuple[date, date]:
    today = date.today()
    start = _parse_date(from_date) or today
    end = _parse_date(to_date) or start
    if end < start:
        start, end = end, start
    return start, end


@bookings_router.get("/security-authorities", response_class=HTMLResponse, dependencies=[Depends(_mod_booking)])
def hotel_security_authorities_page(
    request: Request,
    db: DBSession,
    _: User = Depends(_manage),
):
    from modules.hotel.booking_models import SecurityContactChannel, SecurityGuestScope
    from modules.hotel.security_report_service import (
        contact_channel_label,
        guest_scope_label,
        list_security_authorities,
    )

    items = []
    for a in list_security_authorities(db, only_active=False):
        items.append(
            {
                "id": a.id,
                "name_ar": a.name_ar,
                "contact_channel": a.contact_channel,
                "contact_channel_label": contact_channel_label(a.contact_channel),
                "contact_value": a.contact_value,
                "guest_scope": a.guest_scope,
                "guest_scope_label": guest_scope_label(a.guest_scope),
                "notes": a.notes,
                "is_active": a.is_active,
                "sort_order": a.sort_order,
            }
        )
    return templates.TemplateResponse(
        "hotel/security_authorities.html",
        {
            "request": request,
            "items": items,
            "error": request.query_params.get("error"),
            "saved": request.query_params.get("saved") == "1",
            "contact_channels": [
                {"value": SecurityContactChannel.WHATSAPP.value, "label": contact_channel_label(SecurityContactChannel.WHATSAPP)},
                {"value": SecurityContactChannel.EMAIL.value, "label": contact_channel_label(SecurityContactChannel.EMAIL)},
            ],
            "guest_scopes": [
                {"value": SecurityGuestScope.FOREIGN.value, "label": guest_scope_label(SecurityGuestScope.FOREIGN)},
                {"value": SecurityGuestScope.LIBYAN.value, "label": guest_scope_label(SecurityGuestScope.LIBYAN)},
                {"value": SecurityGuestScope.BOTH.value, "label": guest_scope_label(SecurityGuestScope.BOTH)},
            ],
        },
    )


@bookings_router.post("/security-authorities/save", dependencies=[Depends(_mod_booking)])
def hotel_security_authorities_save(
    db: DBSession,
    _: User = Depends(_manage),
    authority_id: str = Form(""),
    name_ar: str = Form(""),
    contact_channel: str = Form(""),
    contact_value: str = Form(""),
    guest_scope: str = Form(""),
    notes: str = Form(""),
    sort_order: str = Form("0"),
    is_active: str = Form(""),
):
    from modules.hotel.security_report_service import SecurityReportError, save_security_authority

    aid = int(authority_id) if authority_id and authority_id.isdigit() else None
    try:
        save_security_authority(
            db,
            authority_id=aid,
            name_ar=name_ar,
            contact_channel=contact_channel,
            contact_value=contact_value,
            guest_scope=guest_scope,
            notes=notes,
            is_active=is_active == "on",
            sort_order=int(sort_order) if sort_order.isdigit() else 0,
        )
        db.commit()
    except (SecurityReportError, ValueError) as e:
        db.rollback()
        return RedirectResponse(
            f"/admin/hotel/security-authorities?error={quote(str(e))}",
            status_code=302,
        )
    return RedirectResponse("/admin/hotel/security-authorities?saved=1", status_code=302)


@bookings_router.post("/security-authorities/{authority_id}/delete", dependencies=[Depends(_mod_booking)])
def hotel_security_authorities_delete(
    authority_id: int,
    db: DBSession,
    _: User = Depends(_manage),
):
    from modules.hotel.security_report_service import SecurityReportError, delete_security_authority

    try:
        delete_security_authority(db, authority_id)
        db.commit()
    except SecurityReportError as e:
        db.rollback()
        return RedirectResponse(
            f"/admin/hotel/security-authorities?error={quote(str(e))}",
            status_code=302,
        )
    return RedirectResponse("/admin/hotel/security-authorities?saved=1", status_code=302)


@bookings_router.get("/reports/security", response_class=HTMLResponse, dependencies=[Depends(_mod_booking)])
def hotel_security_report(
    request: Request,
    db: DBSession,
    _: User = Depends(_view),
    from_date: str = Query(""),
    to_date: str = Query(""),
    authority_id: str = Query(""),
    print_view: int = Query(0, ge=0, le=1),
):
    from modules.hotel.security_report_service import (
        fetch_security_report_bookings,
        filter_bookings_by_scope,
        get_security_authority,
        guest_scope_label,
    )

    start, end = _security_report_dates(from_date, to_date)
    authority = None
    scope = None
    if authority_id and authority_id.isdigit():
        authority = get_security_authority(db, int(authority_id))
        if authority is not None:
            scope = authority.guest_scope
    bookings = fetch_security_report_bookings(db, start=start, end=end)
    bookings = filter_bookings_by_scope(bookings, scope)
    from modules.hotel.security_report_service import iter_security_guest_rows

    guest_rows = iter_security_guest_rows(bookings)
    return templates.TemplateResponse(
        "hotel/security_report.html",
        {
            "request": request,
            "bookings": bookings,
            "guest_rows": guest_rows,
            "from_date": start,
            "to_date": end,
            "print_view": bool(print_view),
            "authority": authority,
            "guest_scope_label": guest_scope_label(scope) if scope else None,
        },
    )


@bookings_router.post("/reports/security/send", dependencies=[Depends(_mod_booking)])
def hotel_security_report_send(
    db: DBSession,
    _: User = Depends(_manage),
    from_date: str = Form(""),
    to_date: str = Form(""),
    authority_id: str = Form(""),
):
    from modules.hotel.security_report_service import (
        SecurityReportError,
        contact_channel_label,
        fetch_security_report_bookings,
        filter_bookings_by_scope,
        get_security_authority,
        send_security_report,
    )

    if not authority_id or not authority_id.isdigit():
        return RedirectResponse(
            f"/admin/hotel/reports?error={quote('اختر الجهة الأمنية')}",
            status_code=302,
        )
    authority = get_security_authority(db, int(authority_id))
    if authority is None or not authority.is_active:
        return RedirectResponse(
            f"/admin/hotel/reports?error={quote('الجهة الأمنية غير موجودة أو غير نشطة')}",
            status_code=302,
        )
    start, end = _security_report_dates(from_date, to_date)
    bookings = fetch_security_report_bookings(db, start=start, end=end)
    bookings = filter_bookings_by_scope(bookings, authority.guest_scope)
    try:
        send_security_report(
            db,
            authority=authority,
            bookings=bookings,
            from_date=start,
            to_date=end,
        )
        db.commit()
    except SecurityReportError as e:
        db.rollback()
        return RedirectResponse(
            f"/admin/hotel/reports?error={quote(str(e))}",
            status_code=302,
        )
    except Exception as e:
        db.rollback()
        return RedirectResponse(
            f"/admin/hotel/reports?error={quote(str(e))}",
            status_code=302,
        )
    params = (
        f"security_sent=1"
        f"&security_authority={quote(authority.name_ar)}"
        f"&security_channel={quote(contact_channel_label(authority.contact_channel))}"
    )
    return RedirectResponse(f"/admin/hotel/reports?{params}", status_code=302)


@bookings_router.post("/daily-close", dependencies=[Depends(_mod_booking)])
def daily_close(
    db: DBSession,
    user: User = Depends(_fin),
    closing_date: str = Form(""),
    notes: str = Form(""),
):
    d = _parse_date(closing_date) or date.today()
    try:
        _, gl_result = close_daily(db, d, user_id=user.id, notes=notes or None)
        db.commit()
    except Exception as e:
        db.rollback()
        return RedirectResponse(f"/admin/hotel/reports?error={e}", status_code=302)
    params = f"saved=1&closing_date={d.isoformat()}"
    if gl_result.gl_enabled:
        params += f"&gl_backfill={gl_result.backfilled_payments + gl_result.backfilled_refunds}"
        if gl_result.gl_gap is not None:
            params += f"&gl_gap={gl_result.gl_gap}"
    return RedirectResponse(f"/admin/hotel/reports?{params}", status_code=302)


async def _read_receipt_image_json(request: Request) -> str | None:
    image, _ = await _read_whatsapp_post_body(request)
    return image


async def _read_whatsapp_post_body(request: Request) -> tuple[str | None, str | None]:
    ct = (request.headers.get("content-type") or "").lower()
    if "application/json" not in ct:
        return None, None
    try:
        body = await request.json()
    except Exception:
        return None, None
    if not isinstance(body, dict):
        return None, None
    image = body.get("image_png_b64")
    phone = body.get("phone")
    return (
        str(image).strip() if image else None,
        str(phone).strip() if phone else None,
    )


def _hotel_receipt_silent_ctx(db: DBSession) -> dict:
    from modules.platform.business_domain import BusinessDomain
    from modules.printing.service import get_receipt_printer, receipt_silent_print_available

    printer = get_receipt_printer(db, BusinessDomain.HOTEL)
    return {
        "silent_print_enabled": receipt_silent_print_available(db),
        "receipt_printer_name": printer.name if printer else None,
    }


def _booking_guest_phone_display(booking) -> str:
    from modules.customers.service import normalize_phone

    if booking.guest_type == GuestType.COMPANY:
        raw = (booking.company_contact_phone or booking.guest_phone or "").strip()
    else:
        raw = (booking.guest_phone or "").strip()
    return normalize_phone(raw)


def _booking_nights(booking) -> int:
    try:
        return max(0, (booking.check_out - booking.check_in).days)
    except Exception:  # noqa: BLE001
        return 0


@bookings_router.get("/bookings/{booking_id}/receipt", response_class=HTMLResponse)
def booking_receipt_print(
    request: Request,
    booking_id: int,
    db: DBSession,
    user: User = Depends(_view),
    autoprint: int = Query(0, ge=0, le=1),
):
    from modules.branding.service import get_hotel_branding, hotel_display_name
    from modules.platform.business_domain import BusinessDomain
    from modules.receipt_whatsapp.service import booking_phone_hint, whatsapp_receipt_ctx
    from modules.settings.service import get_paper_css, get_receipt_paper_size, normalize_paper

    booking = get_booking(db, booking_id)
    if booking is None:
        return RedirectResponse("/admin/hotel/bookings?error=الحجز غير موجود", status_code=302)
    folio = build_folio(db, booking_id)
    paper = get_receipt_paper_size(db, BusinessDomain.HOTEL)
    from modules.printing.service import get_receipt_printer, thermal_paper_code

    if get_receipt_printer(db, BusinessDomain.HOTEL) is not None:
        paper = normalize_paper(thermal_paper_code(db), "80mm")
    wa_phone = booking_phone_hint(booking)
    return templates.TemplateResponse(
        "hotel/booking_receipt.html",
        {
            "request": request,
            "booking": booking,
            "folio": folio,
            "guest_phone": _booking_guest_phone_display(booking),
            "nights": _booking_nights(booking),
            "hotel_brand": get_hotel_branding(db),
            "store_name": hotel_display_name(db),
            "autoprint": autoprint,
            "embed": 0,
            "paper": paper,
            "paper_css": get_paper_css(paper),
            "silent_print_url": f"/admin/hotel/bookings/{booking_id}/receipt/silent-print",
            **_hotel_receipt_silent_ctx(db),
            **whatsapp_receipt_ctx(
                db,
                domain="hotel",
                phone=wa_phone,
                send_url=f"/admin/hotel/bookings/{booking_id}/receipt/send-whatsapp",
            ),
        },
    )


@bookings_router.post("/bookings/{booking_id}/receipt/silent-print")
async def booking_receipt_silent_print(
    request: Request,
    booking_id: int,
    db: DBSession,
    user: User = Depends(_view),
):
    from modules.printing.service import enqueue_receipt_print

    booking = get_booking(db, booking_id)
    if booking is None:
        return JSONResponse({"ok": False, "error": "الحجز غير موجود."}, status_code=404)
    image_b64 = await _read_receipt_image_json(request)
    if not image_b64 or len(image_b64) < 100:
        return JSONResponse({"ok": False, "error": "صورة الفاتورة غير صالحة."}, status_code=400)
    try:
        job = enqueue_receipt_print(db, image_b64=image_b64)
        db.commit()
        return JSONResponse({"ok": True, "job_id": job.id, "mode": "raster"})
    except ValueError as exc:
        db.rollback()
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@bookings_router.post("/bookings/{booking_id}/receipt/send-whatsapp")
async def booking_receipt_send_whatsapp(
    request: Request,
    booking_id: int,
    db: DBSession,
    user: User = Depends(_view),
):
    from modules.receipt_whatsapp.service import ReceiptWhatsAppError, send_hotel_receipt_whatsapp

    booking = get_booking(db, booking_id)
    if booking is None:
        return JSONResponse({"ok": False, "error": "الحجز غير موجود."}, status_code=404)
    folio = build_folio(db, booking_id)
    image_b64, phone_override = await _read_whatsapp_post_body(request)
    try:
        result = send_hotel_receipt_whatsapp(
            db,
            booking,
            folio_total=folio.total,
            image_png_b64=image_b64,
            phone_override=phone_override,
        )
        db.commit()
        return JSONResponse(result)
    except ReceiptWhatsAppError as exc:
        db.rollback()
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
