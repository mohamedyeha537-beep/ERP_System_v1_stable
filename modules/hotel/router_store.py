"""متجر الشقق الأونلاين — /suites"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.deps import DBSession, require_module
from app.jinja_env import templates
from modules.branding.hotel_banners import get_hotel_banners_public
from modules.branding.service import get_hotel_store_branding
from modules.hotel.booking_models import GuestType
from modules.hotel.booking_service import parse_staying_guests
from modules.hotel.smart_search import (
    child_bed_age_threshold,
    default_stay_times,
    parse_child_ages,
    parse_time_str,
    smart_search_types,
)
from modules.hotel.store_service import (
    StoreError,
    create_online_booking_request,
    get_store_room,
    list_store_rooms,
    quote_stay,
    room_media_gallery,
    store_enabled,
)
from modules.hotel.guest_form_options import guest_form_context
from modules.platform.module_registry import HOTEL_BOOKING

suites_router = APIRouter(prefix="/suites", tags=["hotel-store"])
suites_api = APIRouter(prefix="/api/suites", tags=["hotel-store-api"])
_mod = require_module(HOTEL_BOOKING)


def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        return None


def _parse_guest_type(raw: str | None) -> GuestType:
    try:
        return GuestType((raw or GuestType.INDIVIDUAL.value).strip().upper())
    except ValueError:
        return GuestType.INDIVIDUAL


def _store_ctx(
    request: Request,
    db: DBSession,
    *,
    page_path: str = "/suites",
    page_title: str | None = None,
    **extra,
):
    brand = get_hotel_store_branding(db)
    banners = get_hotel_banners_public(db)
    web_mkt = None
    try:
        from modules.web_marketing.service import (
            SURFACE_HOTEL_PORTAL,
            get_public_web_config,
        )

        web_mkt = get_public_web_config(
            db,
            SURFACE_HOTEL_PORTAL,
            page_path=page_path,
            page_title=page_title,
            default_title=f"{brand['store_name']} — حجز الشقق",
        )
    except Exception:  # noqa: BLE001
        web_mkt = None
    return {
        "request": request,
        "store_name": brand["store_name"],
        "brand": brand,
        "banners": banners.get("items") or [],
        "banner_interval": banners.get("interval_seconds") or 10,
        "web_mkt": web_mkt,
        **extra,
    }


def _parse_int(raw: str | None, default: int, *, minimum: int = 0, maximum: int = 99) -> int:
    try:
        n = int(str(raw or "").strip() or default)
    except ValueError:
        n = default
    return max(minimum, min(maximum, n))


@suites_router.get("", response_class=HTMLResponse, dependencies=[Depends(_mod)])
def suites_home(
    request: Request,
    db: DBSession,
    check_in: str = Query(""),
    check_out: str = Query(""),
    check_in_time: str = Query(""),
    check_out_time: str = Query(""),
    adults: str = Query("2"),
    children: str = Query("0"),
    units: str = Query("1"),
    child_age: list[str] | None = Query(default=None),
    child_ages: str = Query(""),
):
    if not store_enabled(db):
        return templates.TemplateResponse(
            "hotel_store/disabled.html",
            _store_ctx(request, db),
            status_code=403,
        )
    ci = _parse_date(check_in)
    co = _parse_date(check_out)
    default_in, default_out = default_stay_times(db)
    cin_t = parse_time_str(check_in_time, default_in)
    cout_t = parse_time_str(check_out_time, default_out)
    n_adults = _parse_int(adults, 2, minimum=1, maximum=20)
    n_children = _parse_int(children, 0, minimum=0, maximum=15)
    n_units = _parse_int(units, 1, minimum=1, maximum=20)
    ages_raw: str | list[str] = list(child_age) if child_age else child_ages
    ages = parse_child_ages(ages_raw, children=n_children)
    bed_age = child_bed_age_threshold(db)

    # الموظف المسجّل يرى كل الشقق القابلة للإيجار؛ الزائر يرى المعروضة أونلاين فقط
    online_only = True
    user = getattr(request.state, "current_user", None)
    if user is not None:
        try:
            from modules.authz.permissions import HOTEL_BOOKING_VIEW
            from modules.authz.service import user_has_permission

            if user_has_permission(user, HOTEL_BOOKING_VIEW):
                online_only = False
        except Exception:
            pass

    searched = bool(ci and co)
    smart_results = []
    nights = 0
    search_error = None
    if searched:
        if co <= ci:
            search_error = "تاريخ المغادرة يجب أن يكون بعد الوصول."
        else:
            nights = (co - ci).days
            smart_results = smart_search_types(
                db,
                check_in=ci,
                check_out=co,
                adults=n_adults,
                children=n_children,
                child_ages=ages,
                units_needed=n_units,
                online_only=online_only,
            )

    cards = list_store_rooms(db, check_in=ci, check_out=co)
    return templates.TemplateResponse(
        "hotel_store/index.html",
        _store_ctx(
            request,
            db,
            cards=cards,
            check_in=ci,
            check_out=co,
            check_in_time=cin_t,
            check_out_time=cout_t,
            adults=n_adults,
            children=n_children,
            units=n_units,
            child_ages=ages,
            child_bed_age=bed_age,
            searched=searched,
            smart_results=smart_results,
            nights=nights,
            today=date.today(),
            error=request.query_params.get("error") or search_error,
        ),
    )


@suites_router.get("/room/{room_id}", response_class=HTMLResponse, dependencies=[Depends(_mod)])
def suites_room_detail(
    request: Request,
    room_id: int,
    db: DBSession,
    check_in: str = Query(""),
    check_out: str = Query(""),
):
    if not store_enabled(db):
        return templates.TemplateResponse(
            "hotel_store/disabled.html",
            _store_ctx(request, db),
            status_code=403,
        )
    room = get_store_room(db, room_id)
    if room is None:
        return RedirectResponse("/suites?error=الشقة غير متاحة", status_code=302)
    ci = _parse_date(check_in)
    co = _parse_date(check_out)
    quote = None
    stay_info = None
    if ci and co and co > ci:
        from modules.hotel.availability import room_stay_availability

        stay_info = room_stay_availability(
            db, room_id=room_id, check_in=ci, check_out=co
        )
        if stay_info.full_stay_ok:
            try:
                quote = quote_stay(db, room_id=room_id, check_in=ci, check_out=co)
            except StoreError:
                quote = None
    room_label = (room.name_ar or room.number or str(room_id)).strip()
    return templates.TemplateResponse(
        "hotel_store/room.html",
        _store_ctx(
            request,
            db,
            page_path=f"/suites/room/{room_id}",
            page_title=f"{room_label} — حجز شقة",
            room=room,
            gallery=room_media_gallery(room),
            check_in=ci,
            check_out=co,
            quote=quote,
            stay_info=stay_info,
            today=date.today(),
            error=request.query_params.get("error"),
            saved=request.query_params.get("saved"),
            **guest_form_context(db),
        ),
    )


@suites_api.get("/quote", dependencies=[Depends(_mod)])
def suites_quote_api(
    db: DBSession,
    room_id: int = Query(...),
    check_in: str = Query(...),
    check_out: str = Query(...),
):
    if not store_enabled(db):
        return JSONResponse({"error": "المتجر غير متاح"}, status_code=403)
    ci = _parse_date(check_in)
    co = _parse_date(check_out)
    if not ci or not co:
        return JSONResponse({"error": "تواريخ غير صالحة"}, status_code=400)
    try:
        q = quote_stay(db, room_id=room_id, check_in=ci, check_out=co)
    except StoreError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {
        "nights": q["nights"],
        "nightly_rate": str(q["nightly_rate"]),
        "total": str(q["total"]),
    }


@suites_router.post("/room/{room_id}/book", dependencies=[Depends(_mod)])
async def suites_book_submit(
    request: Request,
    room_id: int,
    db: DBSession,
    guest_type: str = Form("INDIVIDUAL"),
    guest_name: str = Form(""),
    guest_phone: str = Form(""),
    guest_email: str = Form(""),
    company_name: str = Form(""),
    company_contact_name: str = Form(""),
    company_contact_phone: str = Form(""),
    company_contact_email: str = Form(""),
    check_in: str = Form(...),
    check_out: str = Form(...),
    adults: str = Form("1"),
    children: str = Form("0"),
    notes: str = Form(""),
):
    if not store_enabled(db):
        return RedirectResponse("/suites", status_code=302)
    ci = _parse_date(check_in)
    co = _parse_date(check_out)
    if not ci or not co:
        return RedirectResponse(
            f"/suites/room/{room_id}?error={quote('تواريخ غير صالحة')}",
            status_code=302,
        )
    form = await request.form()
    staying = parse_staying_guests(
        names=form.getlist("staying_guest_name"),
        id_numbers=form.getlist("staying_guest_id_number"),
        id_types=form.getlist("staying_guest_id_type"),
        nationalities=form.getlist("staying_guest_nationality"),
        addresses=form.getlist("staying_guest_address"),
        phones=form.getlist("staying_guest_phone"),
    )
    gt = _parse_guest_type(guest_type)
    try:
        booking = create_online_booking_request(
            db,
            room_id=room_id,
            guest_name=guest_name,
            guest_phone=guest_phone,
            guest_email=guest_email or None,
            check_in=ci,
            check_out=co,
            adults=int(adults or 1),
            children=int(children or 0),
            guest_type=gt,
            company_name=company_name or None,
            company_contact_name=company_contact_name or None,
            company_contact_phone=company_contact_phone or None,
            company_contact_email=company_contact_email or None,
            internal_notes=(notes or "").strip() or "طلب حجز من المتجر الأونلاين",
            staying_guests=staying,
        )
        db.commit()
    except (StoreError, InvalidOperation) as e:
        db.rollback()
        return RedirectResponse(
            f"/suites/room/{room_id}?check_in={ci}&check_out={co}&error={quote(str(e))}",
            status_code=302,
        )
    return RedirectResponse(
        f"/suites/room/{room_id}?saved=1&booking_ref={quote(booking.reference)}",
        status_code=302,
    )
