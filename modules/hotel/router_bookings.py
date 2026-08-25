"""إدارة الحجوزات — استقبال."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select

from app.deps import DBSession, LoggedInUser, require_any_permission, require_module, require_permission
from app.jinja_env import templates
from modules.authz.models import User
from modules.authz.permissions import (
    HOTEL_BOOKING_CHECKIN,
    HOTEL_BOOKING_CHECKOUT,
    HOTEL_BOOKING_CHECKOUT_BALANCE,
    HOTEL_BOOKING_CREATE,
    HOTEL_BOOKING_MANAGE,
    HOTEL_BOOKING_VIEW,
    HOTEL_DEBTS_COLLECT,
    HOTEL_DEBTS_VIEW,
    HOTEL_FINANCE_CLOSE,
    HOTEL_HOUSEKEEPING,
    HOTEL_ROOMS_MANAGE,
)
from modules.hotel.availability import calendar_bookings
from modules.hotel.shift_session import enforce_hotel_shift_session

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
    change_departure_date,
    change_room,
    check_in_booking,
    check_out_booking,
    confirm_booking,
    convert_quotation_to_booking,
    parse_staying_guests,
    create_booking,
    validate_booking_prepayment,
    apply_customer_wallet_to_booking,
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
    assign_room_cleaning,
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
from modules.hotel.folio import (
    build_account_ledger,
    build_folio,
    build_guest_account,
    build_party_accounts,
    build_payment_ledger,
    build_payment_means_breakdown,
    checkout_service_groups_for_booking,
    folio_debt_breakdown,
    folio_domain_slices,
    folio_scope_from_domain,
    guest_account_from_slice,
    list_open_room_charges_for_booking,
    scoped_party_accounts,
)
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

# مسارات إعدادات/تقارير لا تتطلب جلسة استقبال مفتوحة
_HOTEL_SHIFT_EXEMPT_PATH_PARTS = (
    "/settings",
    "/room-types",
    "/housekeeping",
    "/reports",
    "/security-authorities",
    "/extra-services",
    "/daily-close",
    "/receipt",
    "/voucher",
)


def _hotel_wallet_label(db, method) -> str:
    if method is None:
        return "—"
    from modules.gl.wallet_labels import wallet_label_for_pm

    return wallet_label_for_pm(db, method)


def _front_desk_shift_dep(request: Request, db: DBSession, user: LoggedInUser) -> None:
    """إلزام رقم سري + جلسة مفتوحة لموظف الحجوزات (مثل كاشير المطعم)."""
    path = request.url.path or ""
    if any(part in path for part in _HOTEL_SHIFT_EXEMPT_PATH_PARTS):
        return None
    enforce_hotel_shift_session(request, db, user)
    return None


bookings_router = APIRouter(
    prefix="/admin/hotel",
    tags=["hotel-bookings"],
    dependencies=[Depends(_front_desk_shift_dep)],
)

_view = require_permission(HOTEL_BOOKING_VIEW)
_create = require_permission(HOTEL_BOOKING_CREATE)
_manage = require_permission(HOTEL_BOOKING_MANAGE)
_checkin = require_permission(HOTEL_BOOKING_CHECKIN)
_checkout = require_permission(HOTEL_BOOKING_CHECKOUT)
_checkout_bal = require_permission(HOTEL_BOOKING_CHECKOUT_BALANCE)
_debts_view = require_permission(HOTEL_DEBTS_VIEW)
_debts_collect = require_permission(HOTEL_DEBTS_COLLECT)
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


def _parse_money_form(raw: str | None) -> Decimal:
    try:
        return Decimal(str(raw or "0").strip() or "0").quantize(Decimal("0.001"))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _booking_cash_payment_legs(form) -> list[tuple[Decimal, int, str]]:
    """دفعات الكاش/المصرف عند إنشاء الحجز — واحدة أو مقسّمة."""
    split = _form_str(form, "split_payment") == "on"
    legs: list[tuple[Decimal, int, str]] = []
    if split:
        pairs = (
            (_form_str(form, "deposit_cash", "0"), _form_str(form, "pay_method_cash_id"), "كاش"),
            (_form_str(form, "deposit_bank", "0"), _form_str(form, "pay_method_bank_id"), "مصرف"),
        )
        for raw_amt, raw_pm, label in pairs:
            amt = _parse_money_form(raw_amt)
            if amt <= Decimal("0.0005"):
                continue
            if not raw_pm.isdigit():
                raise BookingError(f"اختر وسيلة الدفع لمبلغ ال{label}.")
            legs.append((amt, int(raw_pm), label))
        return legs
    amt = _parse_money_form(_form_str(form, "deposit", "0"))
    pm = _form_str(form, "pay_method_id")
    if amt > Decimal("0.0005"):
        if not pm.isdigit():
            raise BookingError("اختر وسيلة الدفع لتسجيل المبلغ المدفوع.")
        legs.append((amt, int(pm), "دفع"))
    return legs


def _parse_adults_count(raw: str | None) -> int | None:
    """عدد النزلاء البالغين — مطلوب يدوياً، بدون افتراض 1."""
    s = str(raw or "").strip()
    if not s:
        return None
    try:
        n = int(s)
    except (TypeError, ValueError):
        return None
    if n < 1:
        return None
    return n


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
        "customer_id": _form_str(form, "customer_id"),
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
        "check_in_time": _form_str(form, "check_in_time"),
        "check_out_time": _form_str(form, "check_out_time"),
        "room_id": _form_str(form, "room_id"),
        "adults": _form_str(form, "adults", ""),
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
        draft["split_payment"] = _form_str(form, "split_payment") == "on"
        draft["deposit_cash"] = _form_str(form, "deposit_cash", "")
        draft["deposit_bank"] = _form_str(form, "deposit_bank", "")
        draft["pay_method_cash_id"] = _form_str(form, "pay_method_cash_id")
        draft["pay_method_bank_id"] = _form_str(form, "pay_method_bank_id")
        draft["use_wallet_credit"] = _form_str(form, "use_wallet_credit") == "on"
        draft["wallet_amount"] = _form_str(form, "wallet_amount", "0")
        draft["booking_payer"] = _form_str(form, "booking_payer", "GUEST")
        draft["charge_to_company_account"] = (
            _form_str(form, "charge_to_company_account") == "on"
        )
        draft["notify_to"] = _form_str(form, "notify_to", "COMPANY")
        draft["stay_payer"] = _form_str(form, "stay_payer", "")
        draft["extras_payer"] = _form_str(form, "extras_payer", "")
        draft["is_tourism_agency"] = _form_str(form, "is_tourism_agency") == "on"
        draft["tourism_commission_percent"] = _form_str(
            form, "tourism_commission_percent", "0"
        )
        draft["tourism_agency_id"] = _form_str(form, "tourism_agency_id")
        draft["company_customer_id"] = _form_str(form, "company_customer_id")
        draft["company_link_label"] = _form_str(form, "company_link_search") or _form_str(
            form, "company_name"
        )
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


def _rooms_form_context(
    db: DBSession, *, pre_room: HotelRoom | None = None, user: User | None = None
) -> dict:
    from modules.authz.permissions import HOTEL_COMPANY_CREDIT
    from modules.authz.service import user_has_permission

    from app.datetime_local import now_local
    from modules.hotel.checkin_stay import checkin_stay_policy
    from modules.hotel.late_checkout import late_checkout_policy
    from modules.payments.models import PaymentMethodKind
    from modules.payments.service import payment_method_kind_matches

    active_rooms = list_rooms(db, only_active=True)
    pct = get_booking_prepayment_percent(db)
    stay_pol = checkin_stay_policy(db)
    out_pol = late_checkout_policy(db)
    now = now_local()
    cut_h, cut_m = [int(x) for x in ((stay_pol.business_day_cutoff or "12:00").split(":") + ["0", "0"])[:2]]
    default_in_time = f"{now.hour:02d}:{now.minute:02d}"
    methods = list_hotel_settle_payment_methods(db, only_active=True, user=user)
    cash_methods = [
        m for m in methods if payment_method_kind_matches(m, PaymentMethodKind.CASH)
    ]
    bank_methods = [
        m for m in methods if payment_method_kind_matches(m, PaymentMethodKind.BANK)
    ]
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
        "methods": methods,
        "cash_methods": cash_methods,
        "bank_methods": bank_methods,
        "pre_room": pre_room,
        "today": date.today(),
        "prepayment_percent": pct,
        "prepayment_percent_label": prepayment_percent_label(pct),
        "can_company_credit": bool(
            user and user_has_permission(user, HOTEL_COMPANY_CREDIT)
        ),
        "tourism_agencies": __import__(
            "modules.hotel.tourism_agency", fromlist=["list_tourism_agencies"]
        ).list_tourism_agencies(db, only_active=True, only_with_rate=True),
        "hotel_checkin_stay_policy_enabled": stay_pol.enabled,
        "hotel_business_day_cutoff": stay_pol.business_day_cutoff,
        "hotel_default_check_in_time": stay_pol.check_in_time,
        "hotel_default_check_out_time": out_pol.checkout_time,
        "hotel_full_night_hours": stay_pol.full_night_hours,
        "hotel_half_night_hours": stay_pol.half_night_hours,
        "stay_cutoff_hour": cut_h,
        "stay_cutoff_minute": cut_m,
        "stay_now_hour": now.hour,
        "stay_now_minute": now.minute,
        "stay_now_date": now.date().isoformat(),
        "stay_default_in_time": default_in_time,
        "stay_default_out_time": out_pol.checkout_time,
    }


@bookings_router.get("", response_class=HTMLResponse, dependencies=[Depends(_mod_booking)])
def hotel_root_redirect():
    return RedirectResponse("/admin/hotel/dashboard", status_code=302)


@bookings_router.get("/dashboard", response_class=HTMLResponse, dependencies=[Depends(_mod_booking)])
def hotel_dashboard(
    request: Request,
    db: DBSession,
    user: User = Depends(_view),
    view: str | None = Query(None),
):
    from modules.authz.service import user_has_permission
    from modules.hotel.dashboard import filter_dashboard_by_view, normalize_dashboard_view
    from modules.hotel.maintenance import MAINTENANCE_ISSUE_TYPES
    from modules.settings.service import get_setting

    _boot(db)
    db.commit()
    today = date.today()
    cards, counts = build_room_dashboard(db, day=today)
    # قد يخصم رصيد المحفظة أثناء بناء اللوحة — احفظ التغييرات
    db.commit()
    summary = build_front_desk_summary(db, cards=cards, counts=counts, day=today)
    active_view = normalize_dashboard_view(view)
    cards, summary, view_label = filter_dashboard_by_view(cards, summary, active_view)
    pct = get_booking_prepayment_percent(db)
    can_maintain = any(
        user_has_permission(user, p)
        for p in (HOTEL_ROOMS_MANAGE, HOTEL_BOOKING_MANAGE, HOTEL_HOUSEKEEPING)
    )
    maint_redirect = "/admin/hotel/dashboard"
    if active_view:
        maint_redirect = f"/admin/hotel/dashboard?view={active_view}"
    from modules.hotel.apartment_icons import amenity_icons_context, apartment_icons_context

    incoming_count = 0
    try:
        from modules.hotel.shift_session import session_hotel_employee_id
        from modules.hr.service import get_employee_by_user_id
        from modules.payments.shift_handovers import list_incoming_cash_handovers

        emp_id = session_hotel_employee_id(request)
        if not emp_id:
            emp = get_employee_by_user_id(db, user.id)
            emp_id = emp.id if emp is not None else None
        if emp_id:
            incoming_count = len(list_incoming_cash_handovers(db, int(emp_id)))
    except Exception:  # noqa: BLE001
        incoming_count = 0

    return templates.TemplateResponse(
        "hotel/dashboard.html",
        {
            "request": request,
            "today": today,
            "cards": cards,
            "counts": counts,
            "summary": summary,
            "dashboard_view": active_view,
            "dashboard_view_label": view_label,
            "prepayment_percent": pct,
            "prepayment_percent_label": prepayment_percent_label(pct),
            "can_maintain": can_maintain,
            "issue_types": MAINTENANCE_ISSUE_TYPES,
            "default_maintenance_phone": get_setting(db, "hotel_maintenance_phone") or "",
            "default_maintenance_name": get_setting(db, "hotel_maintenance_name") or "",
            "maint_redirect_to": maint_redirect,
            "apt_icons": apartment_icons_context(db),
            "amenity_icons": amenity_icons_context(db),
            "saved": request.query_params.get("saved"),
            "error": request.query_params.get("error"),
            "incoming_handover_count": incoming_count,
        },
    )


@bookings_router.get("/bookings", response_class=HTMLResponse, dependencies=[Depends(_mod_booking)])
def bookings_list(
    request: Request,
    db: DBSession,
    _: User = Depends(_view),
    status: str | None = Query(None),
    room_id: int | None = Query(None),
    scope: str | None = Query(None),
    q_name: str | None = Query(None),
    q_ref: str | None = Query(None),
    q_phone: str | None = Query(None),
):
    _boot(db)
    db.commit()
    st = None
    statuses = None
    scope_key = (scope or "").strip().lower()
    q_name = (q_name or "").strip()
    q_ref = (q_ref or "").strip()
    q_phone = (q_phone or "").strip()
    searching = bool(q_name or q_ref or q_phone)
    if scope_key in ("open", "active", "current"):
        # الحالية والمستقبلية: مسكّن + مؤكد + معلّق
        statuses = (
            BookingStatus.CHECKED_IN,
            BookingStatus.CONFIRMED,
            BookingStatus.PENDING,
        )
        scope_key = "open"
    elif status:
        try:
            st = BookingStatus(status)
        except ValueError:
            st = None
        scope_key = ""
    if searching:
        # البحث عن زبون يعمل على كل الحجوزات حتى لا يُخفى المغادر
        st = None
        statuses = None
    items = list_bookings(
        db,
        status=st,
        statuses=statuses,
        room_id=room_id,
        guest_name=q_name or None,
        reference=q_ref or None,
        guest_phone=q_phone or None,
    )
    today = date.today()
    arrivals = [
        b for b in items
        if b.check_in == today
        and b.booking_status in (BookingStatus.CONFIRMED, BookingStatus.PENDING)
    ]
    departures = [b for b in items if b.check_out == today and b.booking_status == BookingStatus.CHECKED_IN]
    filter_room = db.get(HotelRoom, room_id) if room_id else None
    from modules.hotel.booking_debts import bookings_list_pay_summaries

    pay_summaries = bookings_list_pay_summaries(db, [int(b.id) for b in items])
    try:
        db.commit()
    except Exception:
        db.rollback()
    booking_debts = {
        bid: row["due"]
        for bid, row in pay_summaries.items()
        if Decimal(str(row.get("due") or 0)) > Decimal("0")
    }
    return templates.TemplateResponse(
        "hotel/bookings_list.html",
        {
            "request": request,
            "items": items,
            "arrivals": arrivals,
            "departures": departures,
            "status_filter": status,
            "scope_filter": scope_key or None,
            "q_name": q_name,
            "q_ref": q_ref,
            "q_phone": q_phone,
            "searching": searching,
            "room_filter": filter_room,
            "booking_debts": booking_debts,
            "booking_pay": pay_summaries,
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


@bookings_router.get("/bookings/lookup-customer", dependencies=[Depends(_mod_booking)])
def booking_lookup_customer(
    db: DBSession,
    user: User = Depends(_create),
    q: str = Query(""),
    guest_type: str = Query(""),
):
    """بحث عميل بالاسم/الهاتف لملء نموذج الحجز وعرض الرصيد."""
    from modules.customers.account_balance import balance_to_dict, lookup_booking_customers

    rows = lookup_booking_customers(db, q=q, guest_type=guest_type, limit=8)
    return JSONResponse(
        {"ok": True, "results": [balance_to_dict(r) for r in rows]},
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        },
    )


@bookings_router.get(
    "/companies/{customer_id}/agreement-preview",
    dependencies=[Depends(_mod_booking)],
)
def company_agreement_preview(
    customer_id: int,
    db: DBSession,
    user: User = Depends(_create),
):
    """معاينة بنود عقد الشركة عند اختيارها في نموذج الحجز."""
    from modules.hotel.company_agreement_service import agreement_preview_for_company

    data = agreement_preview_for_company(db, customer_id)
    return JSONResponse(data)


@bookings_router.post(
    "/companies/{customer_id}/agreement-change-request",
    dependencies=[Depends(_mod_booking)],
)
async def company_agreement_change_request(
    customer_id: int,
    request: Request,
    db: DBSession,
    user: User = Depends(_create),
    items_json: str = Form("[]"),
    note: str = Form(""),
    booking_id: str = Form(""),
    letter: UploadFile | None = File(None),
):
    """طلب استقبال لتعديل بنود عقد الشركة — يُراجع من بطاقة الشركة."""
    from pathlib import Path

    from modules.hotel.company_agreement_service import (
        AgreementError,
        create_agreement_change_request,
    )
    from modules.hotel.uploads import save_agreement_request_file

    attach_path = None
    attach_name = None
    if letter is not None and (letter.filename or "").strip():
        try:
            static_root = Path(__file__).resolve().parents[2] / "app" / "static"
            attach_path = save_agreement_request_file(letter, static_root)
            attach_name = (letter.filename or "").strip()[:180]
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    try:
        bid = int(booking_id) if (booking_id or "").strip().isdigit() else None
        row = create_agreement_change_request(
            db,
            company_customer_id=int(customer_id),
            items=items_json,
            note=note,
            attachment_path=attach_path,
            attachment_name=attach_name,
            booking_id=bid,
            user_id=user.id,
        )
        db.commit()
    except AgreementError as exc:
        db.rollback()
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        return JSONResponse({"ok": False, "error": str(exc)[:180]}, status_code=400)
    return JSONResponse(
        {
            "ok": True,
            "request_id": row.id,
            "message": "أُرسل طلب تعديل الاتفاقية إلى الأدمن.",
        }
    )


@bookings_router.get("/bookings/new", response_class=HTMLResponse, dependencies=[Depends(_mod_booking)])
def booking_new_page(
    request: Request,
    db: DBSession,
    user: User = Depends(_create),
    room_id: int | None = Query(None),
):
    _boot(db)
    pre_room = db.get(HotelRoom, room_id) if room_id else None
    ctx = _rooms_form_context(db, pre_room=pre_room, user=user)
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
            "tomorrow": date.today() + timedelta(days=1),
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
    user: User = Depends(_create),
    room_id: int | None = Query(None),
):
    _boot(db)
    pre_room = db.get(HotelRoom, room_id) if room_id else None
    ctx = _rooms_form_context(db, pre_room=pre_room, user=user)
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
            "tomorrow": date.today() + timedelta(days=1),
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
    check_in_time = _form_str(form, "check_in_time")
    check_out_time = _form_str(form, "check_out_time")
    room_id = _form_str(form, "room_id")
    adults = _form_str(form, "adults", "")
    children = _form_str(form, "children", "0")
    discount = _form_str(form, "discount", "0")
    deposit = _form_str(form, "deposit", "0")
    pay_method_id = _form_str(form, "pay_method_id")
    try:
        pay_legs = _booking_cash_payment_legs(form)
    except BookingError as exc:
        _stash_booking_draft(request, form, is_quotation=False)
        return RedirectResponse(
            f"/admin/hotel/bookings/new?error={quote(str(exc))}", status_code=302
        )
    deposit_total = sum((amt for amt, _pm, _lb in pay_legs), Decimal("0")).quantize(
        Decimal("0.001")
    )
    if pay_legs:
        deposit = str(deposit_total)
        pay_method_id = str(pay_legs[0][1])
    use_wallet = _form_str(form, "use_wallet_credit") == "on"
    wallet_amount_raw = _form_str(form, "wallet_amount", "0")
    booking_payer = _form_str(form, "booking_payer", "").strip().upper()
    charge_to_company_account = _form_str(form, "charge_to_company_account") == "on"
    notify_to_raw = _form_str(form, "notify_to", "").strip().upper()
    stay_payer_raw = _form_str(form, "stay_payer", "").strip().upper()
    extras_payer_raw = _form_str(form, "extras_payer", "").strip().upper()
    is_tourism_agency = _form_str(form, "is_tourism_agency") == "on"
    tourism_commission_raw = _form_str(form, "tourism_commission_percent", "0")
    tourism_agency_raw = _form_str(form, "tourism_agency_id")
    tourism_agency_id = (
        int(tourism_agency_raw) if tourism_agency_raw.isdigit() else None
    )
    internal_notes = _form_str(form, "internal_notes")
    should_confirm = _form_str(form, "auto_confirm") == "on"
    customer_id_raw = _form_str(form, "customer_id")
    linked_customer_id = int(customer_id_raw) if customer_id_raw.isdigit() else None
    company_customer_raw = _form_str(form, "company_customer_id")
    company_customer_id = (
        int(company_customer_raw) if company_customer_raw.isdigit() else None
    )

    def _fail(msg: str) -> RedirectResponse:
        _stash_booking_draft(request, form, is_quotation=False)
        return RedirectResponse(f"/admin/hotel/bookings/new?error={quote(msg)}", status_code=302)

    ci = _parse_date(check_in)
    co = _parse_date(check_out)
    if not ci or not co:
        return _fail("تواريخ غير صالحة")
    if not room_id.strip().isdigit():
        return _fail("اختر الشقة")
    adults_n = _parse_adults_count(adults)
    if adults_n is None:
        return _fail("أدخل عدد النزلاء (بالغون) — الخانة مطلوبة ولا تُملأ تلقائياً.")
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
    from modules.authz.permissions import HOTEL_COMPANY_CREDIT
    from modules.authz.service import user_has_permission
    from modules.customers.models import Customer, CustomerType
    from modules.customers.service import (
        CustomersError,
        ensure_company_customer,
    )
    from modules.hotel.booking_debts import create_checkout_debt
    from modules.hotel.booking_service import booking_amount_due

    parsed_guest_type = _parse_guest_type(guest_type)
    if parsed_guest_type != GuestType.COMPANY:
        company_customer_id = None
        charge_to_company_account = False
        company_name = ""
        company_contact_name = ""
        company_contact_phone = ""
        company_contact_email = ""
        company_address = ""
        company_tax_id = ""
    # حجز شركة مباشرة: حساب الشركة = العميل المختار
    if parsed_guest_type == GuestType.COMPANY and linked_customer_id and not company_customer_id:
        company_customer_id = linked_customer_id
    # شركة: أنشئ/اربط حسابها (إلزامي عند التسجيل على الحساب)
    company_row = None
    if parsed_guest_type == GuestType.COMPANY or charge_to_company_account or company_customer_id:
        try:
            company_row = ensure_company_customer(
                db,
                company_name=company_name,
                phone=company_contact_phone or guest_phone or None,
                email=company_contact_email or None,
                company_customer_id=company_customer_id,
            )
            company_customer_id = int(company_row.id)
            if not linked_customer_id:
                linked_customer_id = company_customer_id
        except CustomersError as exc:
            if charge_to_company_account or company_customer_id:
                return _fail(str(exc))
            company_row = (
                db.get(Customer, int(company_customer_id)) if company_customer_id else None
            )
    if company_customer_id and (
        company_row is None
        or company_row.customer_type != CustomerType.COMPANY
        or not company_row.is_active
    ):
        return _fail("حساب الشركة المختار غير صالح.")
    is_company_booking = bool(company_customer_id) or parsed_guest_type == GuestType.COMPANY
    # التسجيل على حساب الشركة يُفعَّل صراحةً بالتشيك بوكس (افتراضي: يجب الدفع)
    if charge_to_company_account:
        if not is_company_booking or not company_customer_id:
            return _fail(
                "لتفعيل التسجيل على حساب الشركة: اختر نوع «شركة» وأدخل اسمها ورقم الهاتف."
            )
        booking_payer = "COMPANY"
    else:
        booking_payer = "GUEST"
    if booking_payer == "COMPANY" and not company_customer_id:
        return _fail("أدخل اسم الشركة ورقم الهاتف لإنشاء حسابها.")
    from modules.hotel.notify_routing import (
        NOTIFY_TO_COMPANY,
        NOTIFY_TO_GUEST1,
        normalize_notify_to,
        normalize_payer,
    )

    if company_row is not None:
        default_to = (
            getattr(company_row, "company_default_notify_to", None) or NOTIFY_TO_COMPANY
        )
    else:
        default_to = NOTIFY_TO_GUEST1 if not is_company_booking else NOTIFY_TO_COMPANY
    notify_to = normalize_notify_to(notify_to_raw or default_to, default=default_to)
    stay_payer = normalize_payer(
        stay_payer_raw or booking_payer, default=booking_payer or "GUEST"
    )
    extras_payer = normalize_payer(
        extras_payer_raw or booking_payer, default=booking_payer or "GUEST"
    )
    # حجز فرد بدون ربط شركة: الإقامة والخدمات على النزيل — لا تُقرأ قوائم لوحة الشركة المخفية
    if parsed_guest_type != GuestType.COMPANY:
        # فرد = حساب فرد فقط. الشركة تُختار من تاب «شركة» لا من الربط الجانبي.
        company_customer_id = None
        company_row = None
        company_name = ""
        company_contact_name = ""
        company_contact_phone = ""
        company_contact_email = ""
        company_address = ""
        company_tax_id = ""
        charge_to_company_account = False
        is_company_booking = False
        stay_payer = "GUEST"
        extras_payer = "GUEST"
        booking_payer = "GUEST"
        notify_to_raw = NOTIFY_TO_GUEST1
        notify_to = NOTIFY_TO_GUEST1
    if stay_payer == "COMPANY" and not company_customer_id:
        return _fail("لتحمّل الشركة للإقامة يجب ربط حساب شركة.")
    if extras_payer == "COMPANY" and not company_customer_id:
        return _fail("لتحمّل الشركة للخدمات/المطعم يجب ربط حساب شركة.")
    try:
        # المصدر الوحيد لنسبة العمولة: سجل وكالات السياحة (أدمن) وليس إدخال الموظف
        from modules.hotel.tourism_agency import (
            TourismAgencyError,
            resolve_tourism_for_booking,
            tourism_discount_from_gross,
        )

        if tourism_agency_id:
            is_tourism_agency, tourism_pct, company_customer_id = resolve_tourism_for_booking(
                db,
                tourism_agency_id=tourism_agency_id,
                company_customer_id=None,
            )
        elif company_customer_id:
            auto_t, auto_pct, auto_cid = resolve_tourism_for_booking(
                db,
                tourism_agency_id=None,
                company_customer_id=company_customer_id,
            )
            if auto_t:
                is_tourism_agency, tourism_pct, company_customer_id = (
                    auto_t,
                    auto_pct,
                    auto_cid,
                )
            else:
                is_tourism_agency = False
                tourism_pct = Decimal("0")
        else:
            is_tourism_agency = False
            tourism_pct = Decimal("0")
    except TourismAgencyError as exc:
        return _fail(str(exc))
    if parsed_guest_type != GuestType.COMPANY:
        company_customer_id = None
        company_row = None
    if is_tourism_agency and company_customer_id:
        if company_row is None or int(getattr(company_row, "id", 0) or 0) != int(
            company_customer_id
        ):
            company_row = db.get(Customer, int(company_customer_id))
        if company_row is not None and not company_name:
            company_name = (company_row.company_name or company_row.name or "").strip()
    # خصم شركة تلقائي إن لم يُدخل خصم يدوي
    try:
        discount_amt = Decimal(discount or "0").quantize(Decimal("0.001"))
    except (InvalidOperation, ValueError):
        return _fail("قيمة الخصم غير صالحة")
    stay_due_gross = booking_amount_due(
        db,
        room_type_id=room.room_type_id,
        check_in=ci,
        check_out=co,
        discount_amount=Decimal("0"),
        nightly_rate=nightly,
        check_in_time=check_in_time or None,
        check_out_time=check_out_time or None,
    )
    if company_row is not None and discount_amt <= 0 and not is_tourism_agency:
        pct = Decimal(str(company_row.company_discount_percent or 0))
        if pct > 0:
            discount_amt = (stay_due_gross * pct / Decimal("100")).quantize(
                Decimal("0.001")
            )
            if not company_name:
                company_name = (
                    (company_row.company_name or company_row.name or "").strip()
                )
    # عمولة السياحة تُخصم من قيمة الحجز (تلقائياً حسب نسبة الوكالة)
    if is_tourism_agency and tourism_pct > 0:
        t_disc = tourism_discount_from_gross(stay_due_gross, tourism_pct)
        if t_disc > 0:
            discount_amt = (discount_amt + t_disc).quantize(Decimal("0.001"))
    stay_due = booking_amount_due(
        db,
        room_type_id=room.room_type_id,
        check_in=ci,
        check_out=co,
        discount_amount=discount_amt,
        nightly_rate=nightly,
        check_in_time=check_in_time or None,
        check_out_time=check_out_time or None,
    )
    try:
        wallet_amt = (
            Decimal(wallet_amount_raw or "0").quantize(Decimal("0.001"))
            if use_wallet
            else Decimal("0")
        )
    except (InvalidOperation, ValueError):
        return _fail("مبلغ الخصم من الرصيد غير صالح")
    # مصدر المحفظة: حجز شركة / تحميل على الشركة → محفظة الشركة؛ وإلا محفظة النزيل
    wallet_customer_id = None
    use_company_wallet = bool(
        company_customer_id
        and company_row is not None
        and (
            booking_payer == "COMPANY"
            or charge_to_company_account
            or parsed_guest_type == GuestType.COMPANY
        )
    )
    if use_company_wallet:
        wallet_customer_id = company_customer_id
        if charge_to_company_account:
            # التسجيل على حساب الشركة = دين على الحجز/الشركة، وليس «مدفوع» عبر المحفظة/الائتمان
            # لا خصم تلقائي؛ يُخصم من المحفظة فقط إن طُلب صراحةً ومن الرصيد الموجب فقط
            if not use_wallet:
                wallet_amt = Decimal("0")
            else:
                positive = max(
                    Decimal("0"),
                    Decimal(str(company_row.wallet_balance or 0)).quantize(
                        Decimal("0.001")
                    ),
                )
                if wallet_amt <= 0:
                    wallet_amt = min(positive, stay_due).quantize(Decimal("0.001"))
                else:
                    wallet_amt = min(wallet_amt, positive, stay_due).quantize(
                        Decimal("0.001")
                    )
                if wallet_amt <= Decimal("0.0005"):
                    wallet_amt = Decimal("0")
                    use_wallet = False
        elif use_wallet:
            positive = max(
                Decimal("0"),
                Decimal(str(company_row.wallet_balance or 0)).quantize(
                    Decimal("0.001")
                ),
            )
            if wallet_amt <= 0:
                wallet_amt = min(positive, stay_due).quantize(Decimal("0.001"))
            else:
                wallet_amt = min(wallet_amt, positive, stay_due).quantize(
                    Decimal("0.001")
                )
            if wallet_amt <= Decimal("0.0005"):
                wallet_amt = Decimal("0")
                use_wallet = False
            else:
                use_wallet = True
        elif wallet_amt <= 0:
            # بدون تفعيل صريح ولا دين: خصم تلقائي اختياري من الرصيد الموجب فقط
            positive = max(
                Decimal("0"),
                Decimal(str(company_row.wallet_balance or 0)).quantize(
                    Decimal("0.001")
                ),
            )
            if positive > Decimal("0.0005"):
                wallet_amt = min(positive, stay_due).quantize(Decimal("0.001"))
                use_wallet = wallet_amt > 0
    elif use_wallet:
        wallet_customer_id = linked_customer_id
        if not wallet_customer_id:
            return _fail("اختر العميل من نتائج البحث أولاً لاستخدام رصيد النزيل.")
    # تحقق مبكر من حد دين الشركة (قبل إنشاء الحجز)
    if charge_to_company_account and company_row is not None:
        try:
            dep_est = Decimal(deposit or "0").quantize(Decimal("0.001"))
        except (InvalidOperation, ValueError):
            dep_est = Decimal("0")
        if not str(pay_method_id or "").strip().isdigit():
            dep_est = Decimal("0")
        remaining_est = (
            stay_due - min(wallet_amt, stay_due) - dep_est
        ).quantize(Decimal("0.001"))
        if remaining_est > Decimal("0.0005"):
            try:
                from modules.customers.company_credit import (
                    CompanyCreditError,
                    assert_company_can_accept_debt,
                )

                assert_company_can_accept_debt(db, company_row, remaining_est)
            except CompanyCreditError as exc:
                return _fail(str(exc))
    can_company_debt = user_has_permission(user, HOTEL_COMPANY_CREDIT)
    try:
        validate_staying_guests_form(form, require_documents=True)
        validate_booking_prepayment(
            db,
            amount=deposit,
            payment_method_id=pay_method_id,
            room_type_id=room.room_type_id,
            check_in=ci,
            check_out=co,
            discount_amount=discount_amt,
            nightly_rate=nightly,
            check_in_time=check_in_time or None,
            check_out_time=check_out_time or None,
            guest_type=parsed_guest_type,
            wallet_amount=wallet_amt,
            company_account=is_company_booking,
            payer=booking_payer,
            charge_to_company_account=charge_to_company_account,
        )
        deposit_pay = None
        wallet_applied = Decimal("0")
        # نؤجّل إشعار «إنشاء» لنرسل رسالة واتساب واحدة حسب المسار (سداد / تأكيد / إنشاء)
        booking = create_booking(
            db,
            guest_name=guest_name,
            guest_phone=guest_phone or None,
            guest_email=guest_email or None,
            guest_type=parsed_guest_type,
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
            check_in_time=check_in_time or None,
            check_out_time=check_out_time or None,
            room_type_id=room.room_type_id,
            room_id=rid,
            adults=adults_n,
            children=int(children or 0),
            nightly_rate=nightly,
            discount_amount=discount_amt,
            internal_notes=internal_notes,
            user_id=user.id,
            auto_confirm=False,
            record_kind=RecordKind.BOOKING,
            customer_id=linked_customer_id,
            company_customer_id=company_customer_id,
            booking_payer=booking_payer,
            stay_payer=stay_payer,
            extras_payer=extras_payer,
            notify_to=notify_to,
            is_tourism_agency=is_tourism_agency,
            tourism_commission_percent=tourism_pct,
            staying_guests=_staying_guests_from_form(form),
            notify_created=False,
        )
        attach_staying_guest_documents(db, booking, form, require_documents=True)
        if wallet_amt > 0 and wallet_customer_id:
            wallet_applied = apply_customer_wallet_to_booking(
                db,
                booking.id,
                wallet_amt,
                user_id=user.id,
                note=(
                    "خصم من محفظة الشركة عند إنشاء الحجز"
                    if booking_payer == "COMPANY"
                    else "خصم من رصيد النزيل عند إنشاء الحجز"
                ),
                as_deposit=True,
                wallet_customer_id=wallet_customer_id,
                # الدين عند «التسجيل على حساب الشركة» يُسجَّل على الحجز لا بإنزال المحفظة
                allow_company_debt=(
                    can_company_debt
                    and booking_payer == "COMPANY"
                    and not charge_to_company_account
                ),
            )
        # التسجيل على حساب الشركة: لا تُحتسب دفعة إلا بمبلغ صريح + وسيلة دفع
        if charge_to_company_account and not pay_legs:
            pay_legs = []
        if pay_legs:
            from modules.hotel.shift_session import (
                session_hotel_employee_id,
                session_hotel_shift_id,
            )

            _shift_id = session_hotel_shift_id(request)
            _emp_id = session_hotel_employee_id(request)
            for amt, pm_id, label in pay_legs:
                pay_row = record_payment(
                    db,
                    booking.id,
                    amount=amt,
                    payment_method_id=pm_id,
                    is_deposit=True,
                    user_id=user.id,
                    note=f"{label} عند إنشاء الحجز",
                    hotel_shift_id=_shift_id,
                    employee_id=_emp_id,
                )
                if deposit_pay is None:
                    deposit_pay = pay_row
        _recalc_payment_status(db, booking)
        paid_now = Decimal(str(booking.paid_amount or 0)).quantize(Decimal("0.001"))
        remaining_due = (stay_due - paid_now).quantize(Decimal("0.001"))
        # شركة تدفع ولم يُسدَّد كامل المبلغ → دين على الشركة/الحجز
        if (
            charge_to_company_account
            and is_company_booking
            and booking_payer == "COMPANY"
            and remaining_due > Decimal("0.0005")
        ):
            create_checkout_debt(
                db,
                booking,
                remaining_due,
                user_id=user.id,
                note="دين شركة عند إنشاء الحجز — تسجيل على حساب الشركة",
            )
        paid_via_wallet_or_cash = deposit_pay is not None or wallet_applied > 0
        if should_confirm:
            # مع السداد: رسالة الإيصال كافية — بدون رسالة تأكيد إضافية
            confirm_booking(
                db, booking.id, user_id=user.id, notify=not paid_via_wallet_or_cash
            )
        elif not paid_via_wallet_or_cash:
            from modules.notifications.hotel_hooks import emit_hotel_booking_created

            emit_hotel_booking_created(db, booking)
        db.commit()
        _clear_booking_draft(request, is_quotation=False)
    except (BookingError, InvalidOperation, ValueError) as e:
        db.rollback()
        return _fail(str(e))
    # بعد الإنشاء دائماً إلى تفاصيل الحجز — الطباعة تُطلَق من هناك
    if deposit_pay is not None:
        return RedirectResponse(
            f"/admin/hotel/bookings/{booking.id}"
            f"?saved=created&autoprint_receipt=1&payment_id={deposit_pay.id}",
            status_code=302,
        )
    return RedirectResponse(
        f"/admin/hotel/bookings/{booking.id}?saved=created", status_code=302
    )


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
    check_in_time = _form_str(form, "check_in_time")
    check_out_time = _form_str(form, "check_out_time")
    room_id = _form_str(form, "room_id")
    adults = _form_str(form, "adults", "")
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
    adults_n = _parse_adults_count(adults)
    if adults_n is None:
        return _fail("أدخل عدد النزلاء (بالغون) — الخانة مطلوبة ولا تُملأ تلقائياً.")
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
            check_in_time=check_in_time or None,
            check_out_time=check_out_time or None,
            room_type_id=room.room_type_id,
            room_id=rid,
            adults=adults_n,
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
    try:
        booking = get_booking(db, booking_id)
    except Exception as exc:  # noqa: BLE001
        import logging

        logging.getLogger("hotel.bookings").exception(
            "booking_detail load failed booking=%s", booking_id
        )
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return RedirectResponse(
            f"/admin/hotel/bookings?error="
            + quote(f"تعذّر فتح الحجز #{booking_id}: {exc}"),
            status_code=302,
        )
    if booking is None:
        return RedirectResponse("/admin/hotel/bookings?error=الحجز غير موجود", status_code=302)

    # تأكيد ربط العميل بقائمة الفندق (اسم الشركة / المجال)
    try:
        from modules.customers.service import sync_customer_from_hotel_booking

        if sync_customer_from_hotel_booking(db, booking):
            db.commit()
            db.refresh(booking)
    except Exception:  # noqa: BLE001
        db.rollback()
        booking = get_booking(db, booking_id) or booking

    # حجز مغادر: حرّر الشقة إن بقيت مشغولة — لا ترحيل فائض إلى المحفظة
    credit_moved = Decimal("0")
    try:
        from modules.hotel.booking_models import BookingStatus
        from modules.hotel.booking_service import release_room_after_departed_booking

        if booking.booking_status == BookingStatus.CHECKED_OUT:
            if release_room_after_departed_booking(
                db,
                booking,
                user_id=getattr(user, "id", None),
            ):
                db.commit()
                db.refresh(booking)
    except Exception:  # noqa: BLE001
        db.rollback()
        booking = get_booking(db, booking_id) or booking

    # تزامن إقامة مخزّنة مع التواريخ/سعر الشقة (إصلاح تمديد قديم حسب نوع الغرفة فقط)
    try:
        from modules.hotel.booking_models import BookingStatus as _BS
        from modules.hotel.booking_service import (
            _apply_stay_pricing,
            _recalc_booking_accommodation,
        )

        if booking.booking_status in (_BS.CHECKED_IN, _BS.CONFIRMED, _BS.PENDING):
            live_acc = _recalc_booking_accommodation(db, booking)
            stored_acc = Decimal(str(booking.accommodation_total or 0))
            if abs(live_acc - stored_acc) > Decimal("0.001"):
                _apply_stay_pricing(db, booking)
                db.commit()
                db.refresh(booking)
    except Exception:  # noqa: BLE001
        db.rollback()
        booking = get_booking(db, booking_id) or booking

    # ليلة تأخير فائتة + إلغاء خصم محفظة الشركة عن فاتورة النزيل
    try:
        from modules.hotel.booking_models import BookingStatus as _BS2
        from modules.hotel.booking_service import (
            repair_auto_wallet_covering_guest_folio,
            repair_silent_auto_wallet_cover,
        )
        from modules.hotel.late_checkout import ensure_overstay_nights_caught_up

        dirty = False
        if repair_auto_wallet_covering_guest_folio(
            db, booking, user_id=getattr(user, "id", None)
        ) > 0:
            dirty = True
            db.refresh(booking)
        if repair_silent_auto_wallet_cover(
            db, booking, user_id=getattr(user, "id", None)
        ) > 0:
            dirty = True
            db.refresh(booking)
        if booking.booking_status == _BS2.CHECKED_IN:
            if ensure_overstay_nights_caught_up(db, booking, notify=True):
                dirty = True
                db.refresh(booking)
        if dirty:
            db.commit()
            db.refresh(booking)
    except Exception:  # noqa: BLE001
        db.rollback()
        booking = get_booking(db, booking_id) or booking

    folio = None
    guest_account = None
    party_accounts = None
    debt_breakdown = None
    try:
        folio = build_folio(db, booking_id)
        guest_account = build_guest_account(db, booking_id)
        party_accounts = build_party_accounts(db, booking_id)
        # إصلاح بيانات قديمة: العربون المخزّن لا يتجاوز صافي المدفوع
        try:
            dep = Decimal(str(booking.deposit_amount or 0))
            paid = Decimal(str(booking.paid_amount or 0))
            if dep > paid + Decimal("0.001"):
                booking.deposit_amount = paid
                db.commit()
                db.refresh(booking)
                guest_account = build_guest_account(db, booking_id)
                party_accounts = build_party_accounts(db, booking_id)
        except Exception:  # noqa: BLE001
            db.rollback()
        debt_breakdown = folio_debt_breakdown(db, booking_id)
    except Exception as exc:  # noqa: BLE001
        import logging

        logging.getLogger("hotel.bookings").exception(
            "booking_detail folio failed booking=%s", booking_id
        )
        db.rollback()
        return RedirectResponse(
            f"/admin/hotel/bookings?error="
            + quote(f"تم إنشاء الحجز #{booking_id} لكن تعذّر فتح التفاصيل: {exc}"),
            status_code=302,
        )
    folio_scope = "all"
    folio_slices = None
    folio_display_guest = guest_account
    folio_display_party = party_accounts
    try:
        from modules.platform.business_domain import resolve_finance_domain

        folio_scope = folio_scope_from_domain(
            resolve_finance_domain(user, request.session)
        )
        if folio is not None:
            folio_slices = folio_domain_slices(folio)
            if folio_scope in folio_slices:
                folio_display_guest = guest_account_from_slice(folio_slices[folio_scope])
                folio_display_party = scoped_party_accounts(folio, folio_scope)
    except Exception:  # noqa: BLE001
        folio_scope = "all"
        folio_slices = folio_domain_slices(folio) if folio is not None else None
    try:
        payment_ledger, payments_gross, payments_refunded = build_account_ledger(
            db, booking
        )
    except Exception:  # noqa: BLE001
        payment_ledger, payments_gross, payments_refunded = build_payment_ledger(booking)
    try:
        payment_means = build_payment_means_breakdown(db, booking)
    except Exception:  # noqa: BLE001
        payment_means = None
    payments_to_wallet = Decimal("0")
    try:
        for m in payment_ledger or []:
            if getattr(m, "kind", None) == "wallet":
                payments_to_wallet += Decimal(str(getattr(m, "debit", None) or getattr(m, "amount", 0) or 0))
        payments_to_wallet = payments_to_wallet.quantize(Decimal("0.001"))
    except Exception:  # noqa: BLE001
        payments_to_wallet = Decimal("0")
    change_rooms = available_rooms_for_change(db, booking)
    room_assignments = sorted(
        booking.room_assignments,
        key=lambda a: (a.created_at or a.id, a.id),
        reverse=True,
    )
    # فواتير الغرفة: المربوطة بالحجز + المفتوحة على الشقة أثناء الإقامة
    _charges_by_id: dict[int, RoomCharge] = {}
    try:
        for rc in db.scalars(
            select(RoomCharge)
            .where(RoomCharge.booking_id == booking_id)
            .order_by(RoomCharge.id.desc())
        ).all():
            _charges_by_id[int(rc.id)] = rc
        for rc in list_open_room_charges_for_booking(db, booking_id, auto_link=True):
            _charges_by_id[int(rc.id)] = rc
    except Exception:  # noqa: BLE001
        db.rollback()
    from modules.hotel.breakfast_settle import sale_is_hotel_breakfast

    room_charges = sorted(
        (
            c
            for c in _charges_by_id.values()
            # إفطار مشمول: للتسوية فقط — لا يظهر ضمن طلبات المطعم على الحجز
            if not sale_is_hotel_breakfast(db, c.sale)
        ),
        key=lambda c: int(c.id),
        reverse=True,
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
    from modules.platform.business_domain import is_system_admin

    booking_debts = list_booking_debts(db, booking_id)
    from modules.customers.account_balance import customer_money_balance_by_id

    try:
        customer_balance = customer_money_balance_by_id(
            db,
            getattr(booking, "customer_id", None),
            exclude_booking_id=booking_id,
        )
    except Exception:  # noqa: BLE001
        customer_balance = None
    company_wallet_balance = Decimal("0")
    try:
        from modules.customers.models import Customer

        cid = getattr(booking, "company_customer_id", None)
        if cid:
            co = db.get(Customer, int(cid))
            if co is not None:
                company_wallet_balance = Decimal(
                    str(getattr(co, "wallet_balance", 0) or 0)
                ).quantize(Decimal("0.001"))
    except Exception:  # noqa: BLE001
        company_wallet_balance = Decimal("0")
    saved_q = request.query_params.get("saved")
    if credit_moved > 0 and not saved_q:
        saved_q = "credit_to_wallet"
    try:
        late_status = __import__(
            "modules.hotel.late_checkout",
            fromlist=["late_checkout_status_for_booking"],
        ).late_checkout_status_for_booking(db, booking)
    except Exception:  # noqa: BLE001
        late_status = None
    try:
        lock_ctx = __import__(
            "modules.hotel.lock_cards",
            fromlist=["lock_ui_context"],
        ).lock_ui_context(db, booking)
    except Exception:  # noqa: BLE001
        lock_ctx = {
            "lock_encoder_enabled": False,
            "lock_encoder_url": "",
            "lock_co_id": "",
            "lock_room_ready": False,
            "lock_no": "",
        }
    try:
        wa_ctx = __import__(
            "modules.receipt_whatsapp.service",
            fromlist=["hotel_whatsapp_detail_ctx"],
        ).hotel_whatsapp_detail_ctx(db, booking)
    except Exception:  # noqa: BLE001
        wa_ctx = {
            "hotel_whatsapp_receipt_show": False,
            "hotel_whatsapp_receipt_phone": "",
            "hotel_whatsapp_send_url": "",
            "hotel_whatsapp_receipt_url": "",
        }
    try:
        methods = list_hotel_settle_payment_methods(db, only_active=True, user=user)
    except Exception:  # noqa: BLE001
        methods = []
    try:
        service_catalog = list_service_catalog(db, only_active=True)
    except Exception:  # noqa: BLE001
        service_catalog = []
    try:
        checkout_service_groups = checkout_service_groups_for_booking(
            db, int(booking.id)
        )
    except Exception:  # noqa: BLE001
        checkout_service_groups = {
            "laundry": [],
            "violations": [],
            "other": [],
        }
    from app.datetime_local import now_local as _now_local
    from modules.hotel.checkin_stay import checkin_stay_policy as _checkin_stay_policy

    _now = _now_local()
    try:
        _cutoff = (_checkin_stay_policy(db).business_day_cutoff or "12:00").strip()
    except Exception:  # noqa: BLE001
        _cutoff = "12:00"
    try:
        _ch, _cm = [int(x) for x in (_cutoff.split(":") + ["0", "0"])[:2]]
    except (TypeError, ValueError):
        _ch, _cm = 6, 0
    _before_cutoff = (_now.hour, _now.minute) < (_ch, _cm)

    pending_cancel = None
    pending_cancel_label = ""
    try:
        from modules.hotel.cancel_approval import (
            KIND_LABELS,
            pending_cancel_for_booking,
        )

        pending_cancel = pending_cancel_for_booking(db, int(booking.id))
        if pending_cancel is not None:
            import json as _json

            _pl = _json.loads(pending_cancel.action_payload_json or "{}")
            pending_cancel_label = KIND_LABELS.get(str(_pl.get("kind") or ""), "إلغاء")
    except Exception:
        pending_cancel = None

    return templates.TemplateResponse(
        "hotel/booking_detail.html",
        {
            "request": request,
            "booking": booking,
            "folio": folio,
            "guest_account": guest_account,
            "party_accounts": party_accounts,
            "folio_scope": folio_scope,
            "folio_slices": folio_slices,
            "folio_display_guest": folio_display_guest,
            "folio_display_party": folio_display_party,
            "debt_breakdown": debt_breakdown,
            "customer_balance": customer_balance,
            "company_wallet_balance": company_wallet_balance,
            "credit_to_wallet_amount": credit_moved,
            "payment_ledger": payment_ledger,
            "payments_gross": payments_gross,
            "payments_refunded": payments_refunded,
            "payments_to_wallet": payments_to_wallet,
            "payment_means": payment_means,
            "booking_debts": booking_debts,
            "can_checkout_with_balance": user_has_permission(
                user, HOTEL_BOOKING_CHECKOUT_BALANCE
            ),
            "can_pick_departure_date": is_system_admin(user),
            "can_checkout_write_off": is_system_admin(user),
            "can_write_off_debt": is_system_admin(user),
            "change_rooms": change_rooms,
            "room_assignments": room_assignments,
            "room_charges": room_charges,
            "charge_totals": charge_totals,
            "charge_paid": charge_paid,
            "charge_due": charge_due,
            "logs": logs,
            "audit": audit,
            "rooms": list_rooms(db, only_active=True),
            "methods": methods,
            "saved": saved_q,
            "converted": request.query_params.get("converted"),
            "error": request.query_params.get("error"),
            "confirm_midnight": (request.query_params.get("confirm_midnight") or "").strip()
            in ("1", "true", "yes", "on"),
            "midnight_msg": request.query_params.get("midnight_msg"),
            "can_manage_booking": user_has_permission(user, HOTEL_BOOKING_MANAGE),
            "quotation_labels": QUOTATION_STATUS_LABELS,
            "quotation_colors": QUOTATION_STATUS_COLORS,
            "quotation_next": QUOTATION_NEXT_STATUSES,
            "guest_type_label": guest_type_label,
            "quotation_status_label": quotation_status_label,
            "prepayment_percent": get_booking_prepayment_percent(db),
            "prepayment_percent_label": prepayment_percent_label(
                get_booking_prepayment_percent(db)
            ),
            "today": _now.date(),
            "change_departure_min": (
                max(
                    (getattr(booking, "first_chargeable_night", None) or booking.check_in)
                    + timedelta(days=1),
                    _now.date(),
                )
                if booking.booking_status == BookingStatus.CHECKED_IN
                else (getattr(booking, "first_chargeable_night", None) or booking.check_in)
                + timedelta(days=1)
            ),
            "change_departure_max": booking.check_out - timedelta(days=1),
            "now_local_display": _now.strftime("%Y-%m-%d %H:%M"),
            "hotel_business_day_cutoff": f"{_ch:02d}:{_cm:02d}",
            "before_business_cutoff": _before_cutoff,
            "service_catalog": service_catalog,
            "checkout_service_groups": checkout_service_groups,
            "late_checkout_status": late_status,
            "user_is_system_admin": is_system_admin(user),
            "pending_cancel_approval": pending_cancel,
            "pending_cancel_kind_label": pending_cancel_label,
            **(
                __import__(
                    "modules.security.supervisor_otp",
                    fromlist=["otp_policy_context"],
                ).otp_policy_context(db)
            ),
            **lock_ctx,
            **wa_ctx,
        },
    )


@bookings_router.get(
    "/tourism-agencies",
    response_class=HTMLResponse,
    dependencies=[Depends(_mod_booking)],
)
def tourism_agencies_page(
    request: Request,
    db: DBSession,
    _: User = Depends(_rooms),
):
    from modules.hotel.tourism_agency import list_tourism_agencies

    agencies = list_tourism_agencies(db, only_active=False, only_with_rate=False)
    active = [a for a in agencies if a.is_active and a.commission_percent > 0]
    inactive = [a for a in agencies if a not in active]
    return templates.TemplateResponse(
        "hotel/tourism_agencies.html",
        {
            "request": request,
            "agencies": active,
            "inactive_agencies": inactive,
            "saved": request.query_params.get("saved"),
            "error": request.query_params.get("error"),
        },
    )


@bookings_router.post(
    "/tourism-agencies/save",
    dependencies=[Depends(_mod_booking)],
)
def tourism_agencies_save(
    db: DBSession,
    user: User = Depends(_rooms),
    customer_id: str = Form(""),
    company_name: str = Form(""),
    phone: str = Form(""),
    commission_percent: str = Form(""),
    notes: str = Form(""),
):
    from modules.hotel.tourism_agency import (
        TourismAgencyError,
        create_or_update_tourism_agency,
    )

    cid = int(customer_id) if (customer_id or "").strip().isdigit() else None
    try:
        create_or_update_tourism_agency(
            db,
            company_name=company_name,
            commission_percent=commission_percent,
            phone=phone or None,
            customer_id=cid,
            notes=notes or None,
        )
        db.commit()
    except TourismAgencyError as exc:
        db.rollback()
        return RedirectResponse(
            f"/admin/hotel/tourism-agencies?error={quote(str(exc))}",
            status_code=302,
        )
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        return RedirectResponse(
            f"/admin/hotel/tourism-agencies?error={quote(str(exc))}",
            status_code=302,
        )
    return RedirectResponse(
        f"/admin/hotel/tourism-agencies?saved={'updated' if cid else 'created'}",
        status_code=302,
    )


@bookings_router.post(
    "/tourism-agencies/{company_id}/deactivate",
    dependencies=[Depends(_mod_booking)],
)
def tourism_agency_deactivate(
    company_id: int,
    db: DBSession,
    _: User = Depends(_rooms),
):
    from modules.hotel.tourism_agency import TourismAgencyError, deactivate_tourism_agency

    try:
        deactivate_tourism_agency(db, company_id)
        db.commit()
    except TourismAgencyError as exc:
        db.rollback()
        return RedirectResponse(
            f"/admin/hotel/tourism-agencies?error={quote(str(exc))}",
            status_code=302,
        )
    return RedirectResponse(
        "/admin/hotel/tourism-agencies?saved=deactivated",
        status_code=302,
    )


@bookings_router.post(
    "/tourism-agencies/{company_id}/settle",
    dependencies=[Depends(_mod_booking)],
)
def tourism_agency_settle(
    company_id: int,
    db: DBSession,
    user: User = Depends(_rooms),
    amount: str = Form(""),
    note: str = Form(""),
):
    from modules.hotel.tourism_agency import TourismAgencyError, settle_agency_commission

    try:
        settle_agency_commission(
            db,
            company_id,
            user_id=getattr(user, "id", None),
            note=note or None,
            amount=amount.strip() if (amount or "").strip() else None,
        )
        db.commit()
    except TourismAgencyError as exc:
        db.rollback()
        return RedirectResponse(
            f"/admin/hotel/tourism-agencies?error={quote(str(exc))}",
            status_code=302,
        )
    return RedirectResponse(
        "/admin/hotel/tourism-agencies?saved=settled",
        status_code=302,
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
        PAPER_ORIENTATIONS,
        PAPER_SIZES,
        get_receipt_orientation,
        get_receipt_paper_size,
        get_receipt_printer_id,
        get_setting,
    )

    pct = get_booking_prepayment_percent(db)
    id_types = get_guest_id_types(db)
    printers = list(db.scalars(select(Printer).where(Printer.is_active.is_(True)).order_by(Printer.name)).all())
    from modules.hotel.apartment_icons import (
        AMENITY_EMOJI_CHOICES,
        amenity_icons_admin_labels,
        amenity_icons_context,
        apartment_icons_admin_labels,
        apartment_icons_context,
    )
    from modules.hotel.checkin_stay import settings_context as checkin_stay_settings_context
    from modules.hotel.checkin_welcome import welcome_settings_context
    from modules.hotel.cancellation_flow import cancellation_settings_context

    cancellation_settings_ctx = cancellation_settings_context(db)

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
            "paper_orientations": PAPER_ORIENTATIONS,
            "hotel_print_paper": get_receipt_paper_size(db, BusinessDomain.HOTEL),
            "hotel_print_orientation": get_receipt_orientation(db, BusinessDomain.HOTEL),
            "hotel_receipt_printer_id": get_receipt_printer_id(db, BusinessDomain.HOTEL),
            "receipt_printers": printers,
            "service_catalog": list_service_catalog(db, only_active=False),
            "hotel_lock_encoder_enabled": get_setting(db, "hotel_lock_encoder_enabled", "0") == "1",
            "hotel_lock_encoder_url": get_setting(db, "hotel_lock_encoder_url", "http://127.0.0.1:9199") or "",
            "hotel_lock_co_id": get_setting(db, "hotel_lock_co_id", "") or "",
            "hotel_lock_usb_flag": get_setting(db, "hotel_lock_usb_flag", "1") or "1",
            "hotel_lock_public_doors": get_setting(db, "hotel_lock_public_doors", "1") == "1",
            "hotel_lock_deadbolt": get_setting(db, "hotel_lock_deadbolt", "0") == "1",
            "hotel_breakfast_included_enabled": get_setting(
                db, "hotel_breakfast_included_enabled", "1"
            )
            == "1",
            "hotel_late_checkout_enabled": get_setting(
                db, "hotel_late_checkout_enabled", "1"
            )
            == "1",
            "hotel_default_check_out_time": (
                get_setting(db, "hotel_default_check_out_time", "12:00") or "12:00"
            ),
            "hotel_late_checkout_grace_hours": get_setting(
                db, "hotel_late_checkout_grace_hours", "3"
            )
            or "3",
            "hotel_checkout_reminder_lead_hours": get_setting(
                db, "hotel_checkout_reminder_lead_hours", "3"
            )
            or "3",
            "hotel_late_checkout_grace_fee": (
                get_setting(db, "hotel_late_checkout_grace_fee", "FREE") or "FREE"
            ).upper(),
            "hotel_late_checkout_grace_fee_fixed": get_setting(
                db, "hotel_late_checkout_grace_fee_fixed", "0"
            )
            or "0",
            **cancellation_settings_ctx,
            "hotel_guest_reminders_enabled": get_setting(
                db, "hotel_guest_reminders_enabled", "1"
            )
            == "1",
            "hotel_daily_balance_claim_enabled": get_setting(
                db, "hotel_daily_balance_claim_enabled", "1"
            )
            == "1",
            **(
                __import__(
                    "modules.security.supervisor_otp",
                    fromlist=["otp_policy_context"],
                ).otp_policy_context(db)
            ),
            "hotel_cancel_admin_phone": get_setting(db, "hotel_cancel_admin_phone", "")
            or "",
            "apt_icons": apartment_icons_context(db),
            "apt_icon_labels": apartment_icons_admin_labels(),
            "amenity_icons": amenity_icons_context(db),
            "amenity_icon_labels": amenity_icons_admin_labels(),
            "amenity_emoji_choices": AMENITY_EMOJI_CHOICES,
            **guest_form_context(db),
            **welcome_settings_context(db),
            **checkin_stay_settings_context(db),
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
    hotel_print_paper_orientation: str = Form("portrait"),
    hotel_receipt_printer_id: str = Form(""),
    hotel_lock_encoder_enabled: str = Form(""),
    hotel_lock_encoder_url: str = Form(""),
    hotel_lock_co_id: str = Form(""),
    hotel_lock_usb_flag: str = Form("1"),
    hotel_lock_public_doors: str = Form(""),
    hotel_lock_deadbolt: str = Form(""),
    hotel_breakfast_included_enabled: str = Form(""),
    hotel_late_checkout_enabled: str = Form(""),
    hotel_default_check_out_time: str = Form("12:00"),
    hotel_late_checkout_grace_hours: str = Form("3"),
    hotel_checkout_reminder_lead_hours: str = Form("3"),
    hotel_late_checkout_grace_fee: str = Form("FREE"),
    hotel_late_checkout_grace_fee_fixed: str = Form("0"),
    hotel_auto_no_show_enabled: str = Form(""),
    hotel_no_show_cutoff_time: str = Form("23:59"),
    hotel_cancel_before_arrival_fee: str = Form("FULL_REFUND"),
    hotel_cancel_after_checkin_fee: str = Form("FIRST_NIGHT"),
    hotel_no_show_fee: str = Form("FIRST_NIGHT"),
    hotel_no_show_fee_fixed: str = Form("0"),
    hotel_checkin_stay_policy_enabled: str = Form(""),
    hotel_default_check_in_time: str = Form("14:00"),
    hotel_full_night_hours: str = Form("12"),
    hotel_half_night_hours: str = Form("6"),
    hotel_business_day_cutoff: str = Form("12:00"),
    hotel_early_checkin_policy: str = Form("FREE"),
    hotel_early_checkin_fixed_amount: str = Form("0"),
    hotel_early_checkin_percent: str = Form("50"),
    hotel_guest_reminders_enabled: str = Form(""),
    hotel_daily_balance_claim_enabled: str = Form(""),
    hotel_checkin_welcome_enabled: str = Form(""),
    hotel_reception_phone: str = Form(""),
    hotel_checkin_welcome_title: str = Form(""),
    hotel_checkin_welcome_body: str = Form(""),
    otp_require_hotel_refund: str = Form(""),
    otp_require_pos_refund: str = Form(""),
    otp_require_hotel_cancel: str = Form(""),
    hotel_cancel_admin_phone: str = Form(""),
):
    from modules.platform.business_domain import BusinessDomain
    from modules.platform.domain_loyalty import (
        save_loyalty_settings_for_domain,
        save_referral_settings_for_domain,
    )
    from modules.settings.service import (
        invalidate_settings_cache,
        normalize_orientation,
        normalize_paper,
        set_setting,
    )

    if section == "supervisor_otp":
        from modules.hotel.cancel_approval import (
            CancelApprovalError,
            save_cancel_admin_phone,
        )
        from modules.security.supervisor_otp import save_otp_policy

        try:
            save_cancel_admin_phone(db, hotel_cancel_admin_phone)
        except CancelApprovalError as exc:
            db.rollback()
            return RedirectResponse(
                f"/admin/hotel/settings?error={quote(str(exc))}#hotel-supervisor-otp",
                status_code=302,
            )
        save_otp_policy(
            db,
            hotel_refund=otp_require_hotel_refund == "on",
            pos_refund=otp_require_pos_refund == "on",
            hotel_cancel=otp_require_hotel_cancel == "on",
        )
        db.commit()
        return RedirectResponse(
            "/admin/hotel/settings?saved=supervisor_otp#hotel-supervisor-otp",
            status_code=302,
        )

    if section == "welcome":
        from modules.hotel.checkin_welcome import save_check_in_welcome_settings

        save_check_in_welcome_settings(
            db,
            enabled=hotel_checkin_welcome_enabled == "on",
            reception_phone=hotel_reception_phone,
            title=hotel_checkin_welcome_title,
            body=hotel_checkin_welcome_body,
        )
        db.commit()
        return RedirectResponse(
            "/admin/hotel/settings?saved=welcome#hotel-welcome", status_code=302
        )

    if section == "debt_claim":
        set_setting(
            db,
            "hotel_guest_reminders_enabled",
            "1" if hotel_guest_reminders_enabled == "on" else "0",
        )
        set_setting(
            db,
            "hotel_daily_balance_claim_enabled",
            "1" if hotel_daily_balance_claim_enabled == "on" else "0",
        )
        invalidate_settings_cache()
        db.commit()
        return RedirectResponse(
            "/admin/hotel/settings?saved=debt_claim#hotel-debt-claim", status_code=302
        )

    if section == "checkin_stay":
        from modules.hotel.checkin_stay import save_checkin_stay_settings

        save_checkin_stay_settings(
            db,
            enabled=hotel_checkin_stay_policy_enabled == "on",
            check_in_time=hotel_default_check_in_time,
            full_night_hours=hotel_full_night_hours,
            half_night_hours=hotel_half_night_hours,
            business_day_cutoff=hotel_business_day_cutoff,
            early_checkin_policy=hotel_early_checkin_policy,
            early_checkin_fixed=hotel_early_checkin_fixed_amount,
            early_checkin_percent=hotel_early_checkin_percent,
        )
        db.commit()
        return RedirectResponse(
            "/admin/hotel/settings?saved=checkin_stay#hotel-checkin-stay",
            status_code=302,
        )

    if section == "late_checkout":
        from modules.hotel.late_checkout import (
            normalize_checkout_time,
            normalize_grace_fee_mode,
        )

        set_setting(
            db,
            "hotel_late_checkout_enabled",
            "1" if hotel_late_checkout_enabled == "on" else "0",
        )
        set_setting(
            db,
            "hotel_default_check_out_time",
            normalize_checkout_time(hotel_default_check_out_time),
        )
        try:
            grace = int((hotel_late_checkout_grace_hours or "3").strip())
        except ValueError:
            grace = 3
        try:
            lead = int((hotel_checkout_reminder_lead_hours or "3").strip())
        except ValueError:
            lead = 3
        set_setting(db, "hotel_late_checkout_grace_hours", str(max(0, min(24, grace))))
        set_setting(
            db, "hotel_checkout_reminder_lead_hours", str(max(0, min(48, lead)))
        )
        set_setting(
            db,
            "hotel_late_checkout_grace_fee",
            normalize_grace_fee_mode(hotel_late_checkout_grace_fee),
        )
        try:
            from decimal import Decimal

            fixed = Decimal(
                str(hotel_late_checkout_grace_fee_fixed or "0").replace(",", ".")
            ).quantize(Decimal("0.001"))
            if fixed < 0:
                fixed = Decimal("0")
        except Exception:
            fixed = Decimal("0")
        set_setting(db, "hotel_late_checkout_grace_fee_fixed", str(fixed))
        invalidate_settings_cache()
        db.commit()
        return RedirectResponse(
            "/admin/hotel/settings?saved=late_checkout", status_code=302
        )

    if section == "cancel_no_show":
        from modules.hotel.cancellation_flow import (
            normalize_cutoff_time,
            normalize_fee_mode,
        )

        set_setting(
            db,
            "hotel_auto_no_show_enabled",
            "1" if hotel_auto_no_show_enabled == "on" else "0",
        )
        set_setting(
            db,
            "hotel_no_show_cutoff_time",
            normalize_cutoff_time(hotel_no_show_cutoff_time),
        )
        set_setting(
            db,
            "hotel_cancel_before_arrival_fee",
            normalize_fee_mode(hotel_cancel_before_arrival_fee, default="FULL_REFUND"),
        )
        set_setting(
            db,
            "hotel_cancel_after_checkin_fee",
            normalize_fee_mode(hotel_cancel_after_checkin_fee, default="FIRST_NIGHT"),
        )
        set_setting(
            db,
            "hotel_no_show_fee",
            normalize_fee_mode(hotel_no_show_fee, default="FIRST_NIGHT"),
        )
        try:
            from decimal import Decimal

            fixed_ns = Decimal(
                str(hotel_no_show_fee_fixed or "0").replace(",", ".")
            ).quantize(Decimal("0.001"))
            if fixed_ns < 0:
                fixed_ns = Decimal("0")
        except Exception:
            fixed_ns = Decimal("0")
        set_setting(db, "hotel_no_show_fee_fixed", str(fixed_ns))
        invalidate_settings_cache()
        db.commit()
        return RedirectResponse(
            "/admin/hotel/settings?saved=cancel_no_show#hotel-cancel-no-show",
            status_code=302,
        )

    if section == "locks":
        set_setting(
            db,
            "hotel_lock_encoder_enabled",
            "1" if hotel_lock_encoder_enabled == "on" else "0",
        )
        url = (hotel_lock_encoder_url or "").strip() or "http://127.0.0.1:9199"
        set_setting(db, "hotel_lock_encoder_url", url[:240])
        set_setting(db, "hotel_lock_co_id", (hotel_lock_co_id or "").strip()[:20])
        usb = (hotel_lock_usb_flag or "1").strip()
        set_setting(db, "hotel_lock_usb_flag", usb if usb in ("0", "1") else "1")
        set_setting(
            db,
            "hotel_lock_public_doors",
            "1" if hotel_lock_public_doors == "on" else "0",
        )
        set_setting(
            db,
            "hotel_lock_deadbolt",
            "1" if hotel_lock_deadbolt == "on" else "0",
        )
        invalidate_settings_cache()
        db.commit()
        return RedirectResponse("/admin/hotel/settings?saved=locks", status_code=302)

    if section == "breakfast":
        set_setting(
            db,
            "hotel_breakfast_included_enabled",
            "1" if hotel_breakfast_included_enabled == "on" else "0",
        )
        invalidate_settings_cache()
        db.commit()
        return RedirectResponse("/admin/hotel/settings?saved=breakfast", status_code=302)

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
        set_setting(
            db,
            "hotel_print_paper_orientation",
            normalize_orientation(hotel_print_paper_orientation, "portrait"),
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


@bookings_router.post(
    "/settings/apartment-icons",
    dependencies=[Depends(_mod_booking)],
)
async def hotel_apartment_icons_save(
    request: Request,
    db: DBSession,
    _: User = Depends(_rooms),
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
):
    from modules.hotel.apartment_icons import (
        ApartmentIconError,
        save_apartment_icon_emojis,
        save_apartment_icon_upload,
    )
    from modules.settings.service import invalidate_settings_cache

    try:
        save_apartment_icon_emojis(
            db,
            {
                "available": emoji_available,
                "occupied": emoji_occupied,
                "dirty": emoji_dirty,
                "maint": emoji_maint,
            },
        )
        uploads = {
            "available": (img_available, clear_available == "1"),
            "occupied": (img_occupied, clear_occupied == "1"),
            "dirty": (img_dirty, clear_dirty == "1"),
            "maint": (img_maint, clear_maint == "1"),
        }
        for key, (up, clear) in uploads.items():
            await save_apartment_icon_upload(db, key=key, upload=up, clear=clear)
    except ApartmentIconError as e:
        return RedirectResponse(
            f"/admin/hotel/settings?error={quote(str(e))}#hotel-apt-icons",
            status_code=302,
        )
    invalidate_settings_cache()
    db.commit()
    return RedirectResponse("/admin/hotel/settings?saved=apt_icons#hotel-apt-icons", status_code=302)


@bookings_router.post(
    "/settings/amenity-icons",
    dependencies=[Depends(_mod_booking)],
)
async def hotel_amenity_icons_save(
    db: DBSession,
    _: User = Depends(_rooms),
    emoji_rooms: str = Form(""),
    emoji_double: str = Form(""),
    emoji_single: str = Form(""),
    emoji_infant: str = Form(""),
    img_rooms: UploadFile | None = File(None),
    img_double: UploadFile | None = File(None),
    img_single: UploadFile | None = File(None),
    img_infant: UploadFile | None = File(None),
    clear_rooms: str = Form(""),
    clear_double: str = Form(""),
    clear_single: str = Form(""),
    clear_infant: str = Form(""),
):
    from modules.hotel.apartment_icons import (
        ApartmentIconError,
        save_amenity_icon_emojis,
        save_amenity_icon_upload,
    )
    from modules.settings.service import invalidate_settings_cache

    try:
        save_amenity_icon_emojis(
            db,
            {
                "rooms": emoji_rooms,
                "double": emoji_double,
                "single": emoji_single,
                "infant": emoji_infant,
            },
        )
        uploads = {
            "rooms": (img_rooms, clear_rooms == "1"),
            "double": (img_double, clear_double == "1"),
            "single": (img_single, clear_single == "1"),
            "infant": (img_infant, clear_infant == "1"),
        }
        for key, (up, clear) in uploads.items():
            await save_amenity_icon_upload(db, key=key, upload=up, clear=clear)
    except ApartmentIconError as e:
        return RedirectResponse(
            f"/admin/hotel/settings?error={quote(str(e))}#hotel-amenity-icons",
            status_code=302,
        )
    invalidate_settings_cache()
    db.commit()
    return RedirectResponse(
        "/admin/hotel/settings?saved=apt_icons#hotel-amenity-icons",
        status_code=302,
    )


@bookings_router.get(
    "/bookings/{booking_id}/lock-card/prepare",
    dependencies=[Depends(_mod_booking)],
)
def booking_lock_card_prepare(
    booking_id: int,
    db: DBSession,
    _: User = Depends(_manage),
    action: str = "guest_card",
):
    """يجهّز بيانات البرمجة لوكيل الأقفال المحلي على جهاز الاستقبال."""
    from modules.hotel.lock_cards import (
        LockCardError,
        build_erase_payload,
        build_guest_card_payload,
        build_read_payload,
    )

    booking = get_booking(db, booking_id)
    if booking is None:
        return JSONResponse({"ok": False, "message": "الحجز غير موجود"}, status_code=404)
    try:
        if action == "erase":
            payload = build_erase_payload(db)
        elif action == "read":
            payload = build_read_payload(db)
        else:
            payload = build_guest_card_payload(db, booking)
        return {"ok": True, "payload": payload}
    except LockCardError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)


@bookings_router.post(
    "/bookings/{booking_id}/lock-card/run",
    dependencies=[Depends(_mod_booking)],
)
async def booking_lock_card_run(
    booking_id: int,
    request: Request,
    db: DBSession,
    _: User = Depends(_manage),
):
    """بديل: السيرفر يستدعي الوكيل إن كان على نفس الجهاز."""
    from modules.hotel.lock_cards import (
        LockCardError,
        build_erase_payload,
        build_guest_card_payload,
        build_read_payload,
        call_local_encoder,
    )

    booking = get_booking(db, booking_id)
    if booking is None:
        return JSONResponse({"ok": False, "message": "الحجز غير موجود"}, status_code=404)
    try:
        body = await request.json()
    except Exception:
        body = {}
    action = (body.get("action") or "guest_card").strip()
    try:
        if action == "erase":
            payload = build_erase_payload(db)
        elif action == "read":
            payload = build_read_payload(db)
        else:
            payload = build_guest_card_payload(db, booking)
        result = call_local_encoder(payload)
        return {"ok": True, **result}
    except LockCardError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)


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
    actual_arrival_at: str = Form(""),
    confirm_after_midnight: str = Form(""),
    waive_previous_night: str = Form(""),
    waive_reason: str = Form(""),
    override_early_block: str = Form(""),
):
    from datetime import datetime as dt_cls

    from modules.authz.service import user_has_permission

    arrival = _parse_date(actual_arrival) if actual_arrival.strip() else None
    arrival_dt = None
    raw_at = (actual_arrival_at or "").strip()
    if raw_at:
        try:
            # datetime-local: 2026-08-07T03:15
            arrival_dt = dt_cls.fromisoformat(raw_at.replace(" ", "T")[:19])
        except ValueError:
            arrival_dt = None

    want_waive = (waive_previous_night or "").strip().lower() in ("1", "on", "true", "yes")
    want_override = (override_early_block or "").strip().lower() in (
        "1",
        "on",
        "true",
        "yes",
    )
    if want_waive or want_override:
        if not user_has_permission(user, HOTEL_BOOKING_MANAGE):
            return RedirectResponse(
                f"/admin/hotel/bookings/{booking_id}?error="
                + quote("يلزم صلاحية إدارة الحجوزات لإلغاء ليلة سابقة أو تجاوز منع الدخول المبكر."),
                status_code=302,
            )

    from modules.platform.business_domain import is_system_admin

    if want_waive and not is_system_admin(user):
        from modules.hotel.cancel_approval import (
            KIND_WAIVE_PREVIOUS_NIGHT,
            request_cancel_approval,
        )

        try:
            extra = {
                "room_id": int(room_id),
                "actual_arrival": arrival.isoformat() if arrival else "",
                "actual_arrival_at": arrival_dt.isoformat(timespec="seconds")
                if arrival_dt
                else "",
                "override_early_block": want_override,
            }
            request_cancel_approval(
                db,
                booking_id=booking_id,
                kind=KIND_WAIVE_PREVIOUS_NIGHT,
                requested_by_id=user.id,
                reason=(waive_reason or "").strip() or None,
                extra=extra,
            )
            db.commit()
        except BookingError as e:
            db.rollback()
            return RedirectResponse(
                f"/admin/hotel/bookings/{booking_id}?error={quote(str(e))}",
                status_code=302,
            )
        return RedirectResponse(
            f"/admin/hotel/bookings/{booking_id}?saved=cancel_approval_sent",
            status_code=302,
        )

    try:
        check_in_booking(
            db,
            booking_id,
            room_id=room_id,
            user_id=user.id,
            actual_arrival=arrival,
            actual_arrival_at=arrival_dt,
            confirm_after_midnight=(confirm_after_midnight or "").strip().lower()
            in ("1", "on", "true", "yes"),
            waive_previous_night=want_waive,
            waive_reason=(waive_reason or "").strip() or None,
            override_early_block=want_override,
        )
        db.commit()
    except BookingError as e:
        db.rollback()
        msg = str(e)
        if msg.startswith("CONFIRM_AFTER_MIDNIGHT::"):
            body = msg.split("::", 1)[1]
            return RedirectResponse(
                f"/admin/hotel/bookings/{booking_id}"
                f"?confirm_midnight=1&midnight_msg={quote(body)}"
                f"#midnight-confirm-panel",
                status_code=302,
            )
        return RedirectResponse(
            f"/admin/hotel/bookings/{booking_id}?error={quote(msg)}",
            status_code=302,
        )
    return RedirectResponse(f"/admin/hotel/bookings/{booking_id}?saved=1", status_code=302)


@bookings_router.post("/bookings/{booking_id}/check-out", dependencies=[Depends(_mod_booking)])
def booking_check_out(
    booking_id: int,
    db: DBSession,
    user: User = Depends(_checkout),
    allow_balance: str = Form(""),
    post_as_debt: str = Form(""),
    write_off_balance: str = Form(""),
    debt_note: str = Form(""),
    debt_reminder_at: str = Form(""),
    debt_reminder_time: str = Form(""),
    actual_departure: str = Form(""),
    credit_disposition: str = Form(""),
    credit_refund_method_id: str = Form(""),
):
    from app.datetime_local import now_local
    from modules.platform.business_domain import is_system_admin

    allow = allow_balance == "on"
    as_debt = post_as_debt == "on"
    as_write_off = write_off_balance == "on"
    today = now_local().date()
    rem_at = _parse_date(debt_reminder_at) if debt_reminder_at.strip() else None
    rem_time = (debt_reminder_time or "").strip() or None
    # الاستقبال: تاريخ اليوم فقط — اختيار يدوي لمدير النظام وحده
    if is_system_admin(user):
        departure = _parse_date(actual_departure) if actual_departure.strip() else today
    else:
        departure = today
    if departure is None:
        departure = today
    invoice_warn = ""
    already_out = False
    credit_mode = (credit_disposition or "").strip().lower()
    refund_pm: int | None = None
    if (credit_refund_method_id or "").strip().isdigit():
        refund_pm = int(credit_refund_method_id.strip())
    try:
        from modules.authz.service import user_has_permission
        from modules.hotel.booking_models import HotelBooking
        from modules.hotel.finance_service import FinanceError, try_post_booking_gl

        booking = db.get(HotelBooking, booking_id)
        if booking is not None and departure is not None:
            if departure < booking.check_in:
                raise BookingError("تاريخ المغادرة لا يمكن أن يكون قبل تاريخ الوصول.")
        # «بدون سجل دين» أُلغي للاستقبال وللأدمن — للأدمن: شطب مسجّل فقط
        if allow:
            raise BookingError(
                "المغادرة مع متبقٍ بدون سجل دين غير مسموحة. "
                "استخدم ترحيل المتبقي إلى ذمم الحجوزات، "
                "أو (مدير النظام) شطبه كدين غير قابل للتحصيل."
            )
        if as_write_off and not is_system_admin(user):
            raise BookingError(
                "شطب المتبقي كدين غير قابل للتحصيل متاح لمدير النظام فقط."
            )
        if as_write_off and not user_has_permission(
            user, HOTEL_BOOKING_CHECKOUT_BALANCE
        ):
            raise BookingError("لا تملك صلاحية شطب المتبقي كدين غير قابل للتحصيل.")
        if as_write_off and len((debt_note or "").strip()) < 3:
            raise BookingError(
                "أدخل سبب الشطب (3 أحرف على الأقل) عند تسجيل دين غير قابل للتحصيل."
            )
        if rem_time and rem_at is None:
            raise BookingError("حدد تاريخ التذكير مع الوقت، أو اترك الوقت فارغاً.")
        already_out = (
            booking is not None
            and booking.booking_status == BookingStatus.CHECKED_OUT
        )
        check_out_booking(
            db,
            booking_id,
            user_id=user.id,
            allow_balance=False,
            post_as_debt=as_debt and not as_write_off,
            write_off_balance=as_write_off,
            debt_note=debt_note.strip() or None,
            debt_reminder_at=rem_at if as_debt and not as_write_off else None,
            debt_reminder_time=rem_time if as_debt and not as_write_off else None,
            actual_departure=departure,
            credit_disposition=credit_mode or None,
            credit_refund_method_id=refund_pm,
        )
        if not already_out and finance_enabled(db):
            try:
                issue_checkout_invoice(db, booking_id, user_id=user.id)
                try_post_booking_gl(db, booking_id)
            except (FinanceError, Exception) as inv_exc:  # noqa: BLE001
                # المغادرة أهم من الفاتورة المالية — لا نُفشل العملية بسببها
                invoice_warn = str(inv_exc) or "تعذّر إصدار فاتورة المغادرة"
        db.commit()
    except BookingError as e:
        db.rollback()
        return RedirectResponse(
            f"/admin/hotel/bookings/{booking_id}?error={quote(str(e))}",
            status_code=302,
        )
    except Exception as e:  # noqa: BLE001
        db.rollback()
        return RedirectResponse(
            f"/admin/hotel/bookings/{booking_id}?error={quote('فشل تسجيل المغادرة: ' + str(e))}",
            status_code=302,
        )
    if already_out:
        return RedirectResponse(
            f"/admin/hotel/bookings/{booking_id}?saved=already_out",
            status_code=302,
        )
    # بعد تسجيل المغادرة → فاتورة نهائية بكل التفاصيل (شامل طلبات المطعم)
    q = "doc=invoice&pos_details=1&autoprint=1"
    if invoice_warn:
        q += f"&warn={quote(invoice_warn)}"
    return RedirectResponse(
        f"/admin/hotel/bookings/{booking_id}/receipt?{q}",
        status_code=302,
    )


@bookings_router.post("/bookings/{booking_id}/follow-up", dependencies=[Depends(_mod_booking)])
def booking_follow_up_set(
    booking_id: int,
    db: DBSession,
    user: User = Depends(_view),
    follow_up_at: str = Form(""),
    follow_up_note: str = Form(""),
    claim_wa_until_paid: str = Form(""),
    clear: str = Form(""),
):
    from modules.hotel.follow_up import set_booking_follow_up

    try:
        set_booking_follow_up(
            db,
            booking_id,
            follow_up_at=_parse_date(follow_up_at) if follow_up_at.strip() else None,
            follow_up_note=follow_up_note.strip() or None,
            claim_wa_until_paid=claim_wa_until_paid == "on",
            clear=clear == "1" or clear == "on",
            user_id=user.id,
        )
        db.commit()
    except BookingError as e:
        db.rollback()
        return RedirectResponse(
            f"/admin/hotel/bookings/{booking_id}?error={quote(str(e))}",
            status_code=302,
        )
    return RedirectResponse(
        f"/admin/hotel/bookings/{booking_id}?saved=follow_up",
        status_code=302,
    )


@bookings_router.post("/bookings/{booking_id}/debt-watch", dependencies=[Depends(_mod_booking)])
def booking_debt_watch(
    booking_id: int,
    db: DBSession,
    user: User = Depends(_view),
    note: str = Form(""),
    clear: str = Form(""),
    next: str = Form(""),
):
    """تسجيل / إيقاف تتبّع الدين أثناء الإقامة — يظهر في الذمم والشارات حتى السداد."""
    from modules.hotel.follow_up import mark_stay_debt_watch

    back = (next or "").strip() or f"/admin/hotel/bookings/{booking_id}"
    try:
        mark_stay_debt_watch(
            db,
            booking_id,
            note=note.strip() or None,
            user_id=user.id,
            clear=clear == "1" or clear == "on",
        )
        db.commit()
    except BookingError as e:
        db.rollback()
        sep = "&" if "?" in back else "?"
        return RedirectResponse(f"{back}{sep}error={quote(str(e))}", status_code=302)
    sep = "&" if "?" in back else "?"
    saved = "debt_watch_cleared" if (clear == "1" or clear == "on") else "debt_watch"
    return RedirectResponse(f"{back}{sep}saved={saved}", status_code=302)


@bookings_router.post(
    "/bookings/{booking_id}/send-claim-whatsapp",
    dependencies=[Depends(_mod_booking)],
)
def booking_send_claim_whatsapp(
    booking_id: int,
    db: DBSession,
    user: User = Depends(_view),
    claim_note: str = Form(""),
    also_set_reminder: str = Form(""),
    follow_up_at: str = Form(""),
    claim_wa_until_paid: str = Form(""),
):
    from modules.hotel.follow_up import send_booking_claim_whatsapp, set_booking_follow_up

    try:
        if also_set_reminder == "on" or claim_wa_until_paid == "on":
            set_booking_follow_up(
                db,
                booking_id,
                follow_up_at=_parse_date(follow_up_at) if follow_up_at.strip() else None,
                follow_up_note=claim_note.strip() or None,
                claim_wa_until_paid=claim_wa_until_paid == "on" or also_set_reminder == "on",
                user_id=user.id,
            )
        send_booking_claim_whatsapp(
            db,
            booking_id,
            user_id=user.id,
            note=claim_note.strip() or None,
        )
        db.commit()
    except BookingError as e:
        db.rollback()
        return RedirectResponse(
            f"/admin/hotel/bookings/{booking_id}?error={quote(str(e))}",
            status_code=302,
        )
    except Exception as e:  # noqa: BLE001
        db.rollback()
        return RedirectResponse(
            f"/admin/hotel/bookings/{booking_id}?error={quote('فشل إرسال المطالبة: ' + str(e))}",
            status_code=302,
        )
    return RedirectResponse(
        f"/admin/hotel/bookings/{booking_id}?saved=claim_wa",
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
    request: Request,
    booking_id: int,
    db: DBSession,
    user: User = Depends(_manage),
    reason: str = Form(""),
    force_late: str = Form(""),
    supervisor_otp: str = Form(""),
):
    from modules.hotel.cancel_approval import (
        KIND_CANCEL,
        KIND_LATE_CANCEL,
        request_cancel_approval,
    )
    from modules.platform.business_domain import is_system_admin

    late = force_late in ("1", "on", "true", "yes")
    try:
        if not is_system_admin(user):
            request_cancel_approval(
                db,
                booking_id=booking_id,
                kind=KIND_LATE_CANCEL if late else KIND_CANCEL,
                requested_by_id=user.id,
                reason=reason or None,
            )
            db.commit()
            return RedirectResponse(
                f"/admin/hotel/bookings/{booking_id}?saved=cancel_approval_sent#booking-cancel-section",
                status_code=302,
            )
        cancel_booking(
            db,
            booking_id,
            user_id=user.id,
            reason=reason or None,
            force_late=late,
        )
        db.commit()
    except BookingError as e:
        db.rollback()
        return RedirectResponse(f"/admin/hotel/bookings/{booking_id}?error={e}", status_code=302)
    return RedirectResponse(f"/admin/hotel/bookings/{booking_id}?saved=cancelled", status_code=302)


@bookings_router.post("/bookings/{booking_id}/no-show", dependencies=[Depends(_mod_booking)])
def booking_no_show(
    request: Request,
    booking_id: int,
    db: DBSession,
    user: User = Depends(_manage),
    reason: str = Form(""),
    supervisor_otp: str = Form(""),
):
    from modules.hotel.cancel_approval import KIND_NO_SHOW, request_cancel_approval
    from modules.platform.business_domain import is_system_admin

    try:
        if not is_system_admin(user):
            request_cancel_approval(
                db,
                booking_id=booking_id,
                kind=KIND_NO_SHOW,
                requested_by_id=user.id,
                reason=reason or None,
            )
            db.commit()
            return RedirectResponse(
                f"/admin/hotel/bookings/{booking_id}?saved=cancel_approval_sent#booking-cancel-section",
                status_code=302,
            )
        mark_no_show(
            db, booking_id, user_id=user.id, automatic=False, reason=reason or None
        )
        db.commit()
    except BookingError as e:
        db.rollback()
        return RedirectResponse(f"/admin/hotel/bookings/{booking_id}?error={e}", status_code=302)
    return RedirectResponse(f"/admin/hotel/bookings/{booking_id}?saved=no_show", status_code=302)


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


@bookings_router.post("/bookings/{booking_id}/change-departure", dependencies=[Depends(_mod_booking)])
def booking_change_departure(
    booking_id: int,
    db: DBSession,
    user: User = Depends(_manage),
    new_check_out: str = Form(...),
    reason: str = Form(""),
):
    co = _parse_date(new_check_out)
    if not co:
        return RedirectResponse(
            f"/admin/hotel/bookings/{booking_id}?error={quote('تاريخ غير صالح')}",
            status_code=302,
        )
    try:
        _booking, cancelled = change_departure_date(
            db,
            booking_id,
            co,
            user_id=user.id,
            reason=reason or None,
        )
        db.commit()
    except BookingError as e:
        db.rollback()
        return RedirectResponse(
            f"/admin/hotel/bookings/{booking_id}?error={quote(str(e))}",
            status_code=302,
        )
    guest = build_guest_account(db, booking_id)
    return RedirectResponse(
        f"/admin/hotel/bookings/{booking_id}"
        f"?saved=departure_changed&cancelled_nights={cancelled}"
        f"&credit={guest.amount_credit}&due={guest.amount_due}"
        f"#booking-change-departure",
        status_code=302,
    )


@bookings_router.post(
    "/bookings/{booking_id}/waive-auto-late-night",
    dependencies=[Depends(_mod_booking)],
)
def booking_waive_auto_late_night(
    booking_id: int,
    db: DBSession,
    user: User = Depends(_manage),
    waive_reason: str = Form(""),
):
    """إلغاء ليلة Overstay التلقائية — الموظف يطلب موافقة واتساب؛ الأدمن ينفّذ مباشرة."""
    from modules.hotel.cancel_approval import (
        KIND_WAIVE_AUTO_NIGHT,
        request_cancel_approval,
    )
    from modules.hotel.late_checkout import LateCheckoutError, waive_auto_late_night
    from modules.platform.business_domain import is_system_admin

    try:
        if not is_system_admin(user):
            request_cancel_approval(
                db,
                booking_id=booking_id,
                kind=KIND_WAIVE_AUTO_NIGHT,
                requested_by_id=user.id,
                reason=waive_reason,
            )
            db.commit()
            return RedirectResponse(
                f"/admin/hotel/bookings/{booking_id}?saved=cancel_approval_sent",
                status_code=302,
            )
        waive_auto_late_night(
            db, booking_id, user_id=user.id, reason=waive_reason
        )
        db.commit()
    except (LateCheckoutError, BookingError) as e:
        db.rollback()
        return RedirectResponse(
            f"/admin/hotel/bookings/{booking_id}?error={quote(str(e))}",
            status_code=302,
        )
    return RedirectResponse(
        f"/admin/hotel/bookings/{booking_id}?saved=auto_night_waived",
        status_code=302,
    )


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


@bookings_router.post(
    "/bookings/{booking_id}/apply-company-wallet",
    dependencies=[Depends(_mod_booking)],
)
def booking_apply_company_wallet(
    booking_id: int,
    db: DBSession,
    user: User = Depends(require_any_permission(HOTEL_BOOKING_CREATE, HOTEL_BOOKING_CHECKOUT)),
    amount: str = Form(""),
):
    """خصم يدوي من رصيد الشركة على حساب الإقامة/بنود الشركة فقط."""
    from modules.customers.models import Customer
    from modules.hotel.folio import build_party_accounts

    try:
        booking = get_booking(db, booking_id)
        if booking is None:
            raise BookingError("الحجز غير موجود.")
        cid = getattr(booking, "company_customer_id", None)
        if not cid:
            raise BookingError("هذا الحجز غير مربوط بشركة لها محفظة.")
        company = db.get(Customer, int(cid))
        if company is None:
            raise BookingError("حساب الشركة غير موجود.")
        wallet = Decimal(str(company.wallet_balance or 0)).quantize(Decimal("0.001"))
        if wallet <= Decimal("0.0005"):
            raise BookingError("رصيد محفظة الشركة صفر — لا يوجد ما يُخصم.")
        party = build_party_accounts(db, booking_id)
        due = Decimal(str(party.company.due or 0)).quantize(Decimal("0.001"))
        if due <= Decimal("0.0005"):
            raise BookingError("حساب الشركة على هذا الحجز خالص — لا متبقي للخصم.")
        raw = (amount or "").strip()
        if raw:
            try:
                asked = Decimal(raw).quantize(Decimal("0.001"))
            except InvalidOperation as exc:
                raise BookingError("مبلغ الخصم غير صالح.") from exc
        else:
            asked = due
        take = min(asked, due, wallet).quantize(Decimal("0.001"))
        if take <= Decimal("0.0005"):
            raise BookingError("لا يوجد مبلغ قابل للخصم من رصيد الشركة.")
        apply_customer_wallet_to_booking(
            db,
            booking_id,
            take,
            user_id=user.id,
            note="خصم يدوي من رصيد الشركة على حساب الإقامة",
            as_deposit=False,
            wallet_customer_id=int(cid),
        )
        db.commit()
    except BookingError as e:
        db.rollback()
        return RedirectResponse(
            f"/admin/hotel/bookings/{booking_id}?error={quote(str(e))}#checkout-settle",
            status_code=302,
        )
    return RedirectResponse(
        f"/admin/hotel/bookings/{booking_id}?saved=company_wallet#booking-folio",
        status_code=302,
    )


@bookings_router.post("/bookings/{booking_id}/payment", dependencies=[Depends(_mod_booking)])
def booking_payment(
    request: Request,
    booking_id: int,
    db: DBSession,
    user: User = Depends(_create),
    amount: str = Form(...),
    payment_method_id: int = Form(...),
    is_deposit: str = Form(""),
    next: str = Form(""),
):
    from modules.hotel.shift_session import session_hotel_employee_id, session_hotel_shift_id

    # من صفحة الذمم: يُسمح بالقبض حتى على حجز مغادر (تحصيل متبقٍ/ذمة)
    from_debts = (next or "").strip().startswith("/admin/hotel/debts")
    try:
        pay = record_payment(
            db,
            booking_id,
            amount=Decimal(amount),
            payment_method_id=payment_method_id,
            is_deposit=is_deposit == "on",
            user_id=user.id,
            hotel_shift_id=session_hotel_shift_id(request),
            employee_id=session_hotel_employee_id(request),
            allow_closed=from_debts and is_deposit != "on",
        )
        db.commit()
    except (BookingError, InvalidOperation) as e:
        db.rollback()
        back = (next or "").strip()
        if back:
            sep = "&" if "?" in back else "?"
            return RedirectResponse(f"{back}{sep}error={quote(str(e))}", status_code=302)
        return RedirectResponse(
            f"/admin/hotel/bookings/{booking_id}?error={quote(str(e))}",
            status_code=302,
        )
    back = (next or "").strip()
    if back.startswith("/admin/hotel/debts"):
        sep = "&" if "?" in back else "?"
        return RedirectResponse(f"{back}{sep}saved=debt_collected", status_code=302)
    # إنشاء إيصال قبض — العودة لصفحة الحجز؛ الطباعة من القائمة أو الرابط المباشر
    return RedirectResponse(
        f"/admin/hotel/bookings/{booking_id}"
        f"?saved=receipt_created&payment_id={pay.id}#booking-folio",
        status_code=302,
    )


@bookings_router.get("/debts", response_class=HTMLResponse, dependencies=[Depends(_mod_booking)])
def hotel_debts_list(
    request: Request,
    db: DBSession,
    user: User = Depends(_debts_view),
):
    from modules.authz.service import user_has_permission
    from modules.hotel.booking_debts import (
        cleanup_stale_debt_alerts,
        debt_remaining,
        debt_reminder_is_due,
        debts_followup_counts,
        list_debts_due_for_shift_followup,
        open_debts_summary,
    )
    from modules.hotel.bookings_report import hotel_open_balances
    from modules.payments.service import list_hotel_settle_payment_methods
    from modules.platform.business_domain import (
        BusinessDomain,
        is_system_admin,
        resolve_finance_domain,
    )

    if resolve_finance_domain(user, request.session) == BusinessDomain.RESTAURANT:
        return RedirectResponse("/hotel/settle", status_code=302)

    err_msg = request.query_params.get("error")
    open_rows: list = []
    total = Decimal("0")
    due_rows: list = []
    counts = {"open": 0, "due_today": 0, "no_reminder": 0}
    stay_balances: list = []
    stay_balance_total = Decimal("0")
    stay_balance_count = 0
    closed_balances: list = []
    closed_balance_total = Decimal("0")
    stay_watch: dict[int, bool] = {}
    stay_notes: dict[int, str] = {}
    methods = []
    live_user = db.get(User, int(user.id)) if user is not None else None
    if live_user is not None:
        user = live_user
    can_collect = False
    can_write_off = False
    try:
        can_collect = user_has_permission(user, HOTEL_DEBTS_COLLECT)
        can_write_off = is_system_admin(user)
        if cleanup_stale_debt_alerts(db):
            db.commit()
        open_rows, total = open_debts_summary(db)
        due_rows = list_debts_due_for_shift_followup(db)
        counts = debts_followup_counts(db)
        stay_balances, stay_sum = hotel_open_balances(db)
        # استبعاد من لديهم دين مغادرة مفتوح أصلاً لتجنب التكرار البصري
        debt_booking_ids = {int(d.booking_id) for d in open_rows}
        stay_balances = [
            r
            for r in stay_balances
            if int(getattr(r, "booking_id", 0) or 0) not in debt_booking_ids
        ]
        # فصل: مسكّن/محجوز vs مغادر مع متبقٍ (كان يُعرض الكل كـ «قيد الإقامة»)
        active_stay = []
        closed_balances = []
        from modules.hotel.booking_models import HotelBooking, BookingStatus

        stay_ids_all = [int(r.booking_id) for r in stay_balances]
        status_by_id: dict[int, BookingStatus | None] = {}
        if stay_ids_all:
            for b in db.scalars(
                select(HotelBooking).where(HotelBooking.id.in_(stay_ids_all))
            ).all():
                status_by_id[int(b.id)] = b.booking_status
        for r in stay_balances:
            st = status_by_id.get(int(r.booking_id))
            if st == BookingStatus.CHECKED_OUT:
                closed_balances.append(r)
            else:
                active_stay.append(r)
        stay_balances = active_stay
        stay_ids = [int(r.booking_id) for r in stay_balances]
        if stay_ids:
            for b in db.scalars(
                select(HotelBooking).where(HotelBooking.id.in_(stay_ids))
            ).all():
                stay_watch[int(b.id)] = bool(getattr(b, "claim_wa_until_paid", False))
                note = (getattr(b, "follow_up_note", None) or "").strip()
                if note:
                    stay_notes[int(b.id)] = note
        stay_balance_count = len(stay_balances)
        stay_balance_total = sum(
            (Decimal(str(r.balance or 0)) for r in stay_balances), Decimal("0")
        ).quantize(Decimal("0.001"))
        closed_balance_total = sum(
            (Decimal(str(r.balance or 0)) for r in closed_balances), Decimal("0")
        ).quantize(Decimal("0.001"))
        try:
            methods = list_hotel_settle_payment_methods(
                db, only_active=True, user=user
            )
        except Exception as pm_exc:  # noqa: BLE001
            import logging

            logging.getLogger("pos.hotel.debts").exception(
                "فشل تحميل وسائل دفع الذمم: %s", pm_exc
            )
            methods = []
    except Exception as exc:  # noqa: BLE001
        import logging

        logging.getLogger("pos.hotel.debts").exception("فشل تحميل صفحة الذمم: %s", exc)
        try:
            db.rollback()
        except Exception:
            pass
        open_rows = []
        due_rows = []
        stay_balances = []
        closed_balances = []
        methods = []
        err_msg = err_msg or f"تعذّر تحميل بعض بيانات الذمم: {exc}"

    claim_total = (
        Decimal(str(total or 0))
        + Decimal(str(stay_balance_total or 0))
        + Decimal(str(closed_balance_total or 0))
    ).quantize(Decimal("0.001"))
    claim_count = (
        int(counts.get("open") or 0)
        + int(stay_balance_count or 0)
        + len(closed_balances or [])
    )

    context = {
        "request": request,
        "open_debts": open_rows,
        "due_debts": due_rows,
        "open_total": total,
        "counts": counts,
        "debt_remaining": debt_remaining,
        "debt_reminder_is_due": debt_reminder_is_due,
        "stay_balances": stay_balances,
        "stay_balance_total": stay_balance_total,
        "stay_balance_count": stay_balance_count,
        "closed_balances": closed_balances,
        "closed_balance_total": closed_balance_total,
        "stay_watch": stay_watch,
        "stay_notes": stay_notes,
        "methods": methods,
        "can_collect": can_collect,
        "can_write_off": can_write_off,
        "saved": request.query_params.get("saved"),
        "error": err_msg,
        "today": date.today(),
        "claim_total": claim_total,
        "claim_count": claim_count,
    }
    try:
        return templates.TemplateResponse("hotel/debts.html", context)
    except Exception as exc:  # noqa: BLE001
        import logging

        logging.getLogger("pos.hotel.debts").exception("فشل عرض قالب الذمم: %s", exc)
        return HTMLResponse(
            "<!DOCTYPE html><html lang='ar' dir='rtl'><head><meta charset='utf-8'/>"
            "<title>ذمم الحجوزات</title></head><body style='font-family:sans-serif;padding:1.5rem'>"
            "<h1>ذمم الحجوزات</h1>"
            f"<p style='color:#b91c1c'>تعذّر عرض الصفحة: {exc}</p>"
            "<p><a href='/admin/hotel/dashboard'>العودة للوحة الشقق</a></p>"
            "</body></html>",
            status_code=200,
        )


@bookings_router.post("/debts/{debt_id}/collect", dependencies=[Depends(_mod_booking)])
def hotel_debt_collect(
    debt_id: int,
    db: DBSession,
    user: User = Depends(_debts_collect),
    payment_method_id: int = Form(...),
    amount: str = Form(""),
    note: str = Form(""),
    next: str = Form(""),
):
    from modules.hotel.booking_debts import collect_booking_debt

    back = (next or "").strip() or "/admin/hotel/debts"
    try:
        amt = None
        if (amount or "").strip():
            amt = Decimal(str(amount).strip())
        collect_booking_debt(
            db,
            debt_id,
            payment_method_id=payment_method_id,
            user_id=user.id,
            note=note.strip() or None,
            amount=amt,
        )
        db.commit()
    except (BookingError, InvalidOperation) as e:
        db.rollback()
        return RedirectResponse(f"{back}?error={quote(str(e))}", status_code=302)
    return RedirectResponse(f"{back}?saved=debt_collected", status_code=302)


@bookings_router.post("/debts/{debt_id}/followup", dependencies=[Depends(_mod_booking)])
def hotel_debt_followup(
    debt_id: int,
    db: DBSession,
    user: User = Depends(_debts_collect),
    note: str = Form(""),
    reminder_at: str = Form(""),
    reminder_time: str = Form(""),
    clear_reminder: str = Form(""),
    next: str = Form(""),
):
    from modules.hotel.booking_debts import update_debt_followup

    back = (next or "").strip() or "/admin/hotel/debts"
    try:
        update_debt_followup(
            db,
            debt_id,
            note=note.strip() or None,
            reminder_at=_parse_date(reminder_at) if reminder_at.strip() else None,
            reminder_time=(reminder_time or "").strip() or None,
            clear_reminder=clear_reminder == "on",
            user_id=user.id,
        )
        db.commit()
    except BookingError as e:
        db.rollback()
        return RedirectResponse(f"{back}?error={quote(str(e))}", status_code=302)
    return RedirectResponse(f"{back}?saved=followup", status_code=302)


@bookings_router.post("/bookings/{booking_id}/debts/{debt_id}/collect", dependencies=[Depends(_mod_booking)])
def booking_debt_collect(
    booking_id: int,
    debt_id: int,
    db: DBSession,
    user: User = Depends(_debts_collect),
    payment_method_id: int = Form(...),
    amount: str = Form(""),
    note: str = Form(""),
):
    from modules.hotel.booking_debts import collect_booking_debt

    try:
        amt = None
        if (amount or "").strip():
            amt = Decimal(str(amount).strip())
        debt = collect_booking_debt(
            db,
            debt_id,
            payment_method_id=payment_method_id,
            user_id=user.id,
            note=note.strip() or None,
            amount=amt,
        )
        if debt.booking_id != booking_id:
            raise BookingError("الدين لا يخص هذا الحجز.")
        db.commit()
    except (BookingError, InvalidOperation) as e:
        db.rollback()
        return RedirectResponse(f"/admin/hotel/bookings/{booking_id}?error={e}", status_code=302)
    pay_id = getattr(debt, "collection_payment_id", None)
    if pay_id:
        return RedirectResponse(
            f"/admin/hotel/bookings/{booking_id}/receipt"
            f"?doc=receipt&payment_id={pay_id}&autoprint=1",
            status_code=302,
        )
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
    from modules.platform.business_domain import is_system_admin

    if not is_system_admin(user):
        return RedirectResponse(
            f"/admin/hotel/bookings/{booking_id}?error="
            + quote("شطب الدين كغير قابل للتحصيل متاح لمدير النظام فقط."),
            status_code=302,
        )
    why = (reason or "").strip()
    if len(why) < 3:
        return RedirectResponse(
            f"/admin/hotel/bookings/{booking_id}?error="
            + quote("أدخل سبب الشطب (3 أحرف على الأقل)."),
            status_code=302,
        )
    try:
        debt = write_off_booking_debt(
            db,
            debt_id,
            reason=why,
            user_id=user.id,
        )
        if debt.booking_id != booking_id:
            raise BookingError("الدين لا يخص هذا الحجز.")
        db.commit()
    except BookingError as e:
        db.rollback()
        return RedirectResponse(f"/admin/hotel/bookings/{booking_id}?error={e}", status_code=302)
    return RedirectResponse(f"/admin/hotel/bookings/{booking_id}?saved=debt_written_off", status_code=302)


@bookings_router.post(
    "/bookings/{booking_id}/refund-otp/request",
    dependencies=[Depends(_mod_booking)],
)
def booking_refund_otp_request(
    booking_id: int,
    db: DBSession,
    user: User = Depends(_manage),
    purpose: str = Form("hotel_refund"),
    focus: str = Form("create-disbursement-voucher"),
):
    from modules.platform.business_domain import BusinessDomain
    from modules.security.supervisor_otp import (
        PURPOSE_HOTEL_CANCEL,
        PURPOSE_HOTEL_REFUND,
        SupervisorOtpError,
        request_refund_otp,
    )

    booking = get_booking(db, booking_id)
    if booking is None:
        return RedirectResponse(
            f"/admin/hotel/bookings?error={quote('الحجز غير موجود')}",
            status_code=302,
        )
    purp = (purpose or PURPOSE_HOTEL_REFUND).strip()
    if purp not in (PURPOSE_HOTEL_REFUND, PURPOSE_HOTEL_CANCEL):
        purp = PURPOSE_HOTEL_REFUND
    anchor = (focus or "create-disbursement-voucher").strip()
    if not anchor.replace("-", "").replace("_", "").isalnum():
        anchor = "create-disbursement-voucher"
    if purp == PURPOSE_HOTEL_CANCEL:
        ref_label = f"إلغاء/No-Show حجز {booking.reference} (#{booking_id})"
    else:
        ref_label = f"حجز فندق {booking.reference} (#{booking_id})"
    try:
        request_refund_otp(
            db,
            purpose=purp,
            domain=BusinessDomain.HOTEL,
            ref_type="booking",
            ref_id=booking_id,
            ref_label=ref_label,
            requested_by_user_id=getattr(user, "id", None),
        )
    except SupervisorOtpError as e:
        return RedirectResponse(
            f"/admin/hotel/bookings/{booking_id}?error={quote(str(e))}#{anchor}",
            status_code=302,
        )
    return RedirectResponse(
        f"/admin/hotel/bookings/{booking_id}?saved=otp_sent#{anchor}",
        status_code=302,
    )


@bookings_router.post("/bookings/{booking_id}/refund-credit", dependencies=[Depends(_mod_booking)])
def booking_refund_credit(
    request: Request,
    booking_id: int,
    db: DBSession,
    user: User = Depends(_manage),
    payment_id: int = Form(...),
    amount: str = Form(...),
    reason: str = Form(""),
    payment_method_id: str = Form(""),
    supervisor_otp: str = Form(""),
):
    from modules.hotel.shift_session import session_hotel_employee_id, session_hotel_shift_id

    del supervisor_otp  # ترجيع مبلغ النزيل بدون رمز مشرف
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
        pm_raw = (payment_method_id or "").strip()
        if not pm_raw.isdigit():
            raise BookingError("اختر وسيلة صرف الإرجاع (كاش أو مصرف).")
        ref = refund_payment(
            db,
            payment_id,
            amount=amt,
            reason=(reason or "").strip() or "استرداد بعد مغادرة مبكرة",
            user_id=user.id,
            payment_method_id=int(pm_raw),
            hotel_shift_id=session_hotel_shift_id(request),
            employee_id=session_hotel_employee_id(request),
        )
        db.commit()
    except (BookingError, InvalidOperation) as e:
        db.rollback()
        return RedirectResponse(f"/admin/hotel/bookings/{booking_id}?error={e}", status_code=302)
    # إنشاء إيصال صرف — العودة لصفحة الحجز؛ الطباعة من القائمة أو الرابط المباشر
    return RedirectResponse(
        f"/admin/hotel/bookings/{booking_id}"
        f"?saved=disbursement_created&refund_id={ref.id}#booking-folio",
        status_code=302,
    )


@bookings_router.get(
    "/bookings/{booking_id}/refunds/{refund_id}/voucher",
    response_class=HTMLResponse,
    dependencies=[Depends(_mod_booking)],
)
def booking_refund_voucher_print(
    request: Request,
    booking_id: int,
    refund_id: int,
    db: DBSession,
    user: User = Depends(_view),
    paper: str | None = Query(None),
    orientation: str | None = Query(None),
    autoprint: int = Query(0, ge=0, le=1),
):
    """إيصال صرف عند إرجاع مبلغ للنزيل — بنفس أسلوب إيصال القبض."""
    from modules.branding.service import hotel_display_name
    from modules.hotel.booking_models import HotelBookingPayment, HotelBookingPaymentRefund
    from modules.payments.models import PaymentMethod
    from modules.platform.business_domain import BusinessDomain
    from modules.printing.doc_numbers import PrintDocKind, doc_kind_label, next_doc_number
    from modules.settings.service import (
        PAPER_ORIENTATIONS,
        PAPER_SIZES,
        get_paper_css,
        get_receipt_orientation,
        get_receipt_paper_size,
        get_setting,
        normalize_orientation,
        normalize_paper,
        set_setting,
    )

    booking = get_booking(db, booking_id)
    if booking is None:
        return RedirectResponse("/admin/hotel/bookings?error=" + quote("الحجز غير موجود"), status_code=302)
    ref = db.get(HotelBookingPaymentRefund, refund_id)
    if ref is None:
        return RedirectResponse(
            f"/admin/hotel/bookings/{booking_id}?error=" + quote("سند الاسترداد غير موجود"),
            status_code=302,
        )
    pay = db.get(HotelBookingPayment, ref.payment_id)
    if pay is None or int(pay.booking_id) != int(booking_id):
        return RedirectResponse(
            f"/admin/hotel/bookings/{booking_id}?error=" + quote("الاسترداد لا يخص هذا الحجز"),
            status_code=302,
        )

    bd = BusinessDomain.HOTEL
    chosen = normalize_paper(paper, get_receipt_paper_size(db, bd))
    orient = normalize_orientation(orientation, get_receipt_orientation(db, bd))
    meta_key = f"hotel_refund_voucher_{refund_id}"
    existing = (get_setting(db, meta_key, "") or "").strip()
    if existing:
        doc_number = existing
    else:
        doc_number = next_doc_number(db, PrintDocKind.DISBURSEMENT, domain="hotel")
        set_setting(db, meta_key, doc_number)
        db.commit()

    method = db.get(PaymentMethod, ref.payment_method_id) if ref.payment_method_id else None
    if method is None and pay.payment_method_id:
        method = db.get(PaymentMethod, pay.payment_method_id)
    emp = db.get(User, ref.approved_by_id) if ref.approved_by_id else None
    guest = (getattr(booking, "guest_name", None) or "").strip() or "نزيل"
    booking_code = getattr(booking, "booking_code", None) or getattr(booking, "code", None) or booking.id
    note_parts = []
    if (ref.reason or "").strip():
        note_parts.append(ref.reason.strip())
    note_parts.append(f"حجز #{booking_code}")
    preserve = {}
    if autoprint:
        preserve["autoprint"] = "1"

    return templates.TemplateResponse(
        "disbursement_voucher.html",
        {
            "request": request,
            "doc_title": doc_kind_label(PrintDocKind.DISBURSEMENT),
            "doc_number": doc_number,
            "amount": Decimal(str(ref.amount or 0)).quantize(Decimal("0.001")),
            "method_name": _hotel_wallet_label(db, method),
            "category": "استرداد حجز",
            "party": guest,
            "note": " — ".join(note_parts),
            "created_at": ref.created_at,
            "employee_name": emp.username if emp else "",
            "store_name": hotel_display_name(db),
            "back_url": f"/admin/hotel/bookings/{booking_id}",
            "paper": chosen,
            "orientation": orient,
            "paper_css": get_paper_css(chosen, orient),
            "paper_choices": PAPER_SIZES,
            "orientation_choices": PAPER_ORIENTATIONS,
            "can_choose_paper": True,
            "print_form_action": f"/admin/hotel/bookings/{booking_id}/refunds/{refund_id}/voucher",
            "print_preserve_params": preserve,
            "autoprint": autoprint,
        },
    )


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


@bookings_router.post(
    "/bookings/{booking_id}/violation", dependencies=[Depends(_mod_booking)]
)
def booking_add_violation(
    booking_id: int,
    db: DBSession,
    user: User = Depends(_create),
    description: str = Form(...),
    amount: str = Form(...),
    notes: str = Form(""),
):
    """تسجيل مخالفة / تلف أو فقدان مواد الشقة على حساب النزيل قبل المغادرة."""
    try:
        desc = (description or "").strip()
        if len(desc) < 2:
            raise BookingError("أدخل وصف المخالفة (اتلاف أو فقدان…).")
        amt = Decimal(str(amount).strip().replace(",", "."))
        if amt <= Decimal("0"):
            raise BookingError("مبلغ المخالفة يجب أن يكون أكبر من صفر.")
        name_ar = desc if desc.startswith("مخالفة") else f"مخالفة — {desc}"
        if len(name_ar) > 160:
            name_ar = name_ar[:157] + "…"
        add_booking_service(
            db,
            booking_id,
            name_ar=name_ar,
            quantity=Decimal("1"),
            unit_price=amt.quantize(Decimal("0.001")),
            notes=(notes or "").strip() or None,
            user_id=user.id,
            service_code="VIOLATION",
        )
        db.commit()
    except (BookingError, InvalidOperation) as e:
        db.rollback()
        return RedirectResponse(
            f"/admin/hotel/bookings/{booking_id}?error={quote(str(e))}#checkout-settle",
            status_code=302,
        )
    except Exception:  # noqa: BLE001
        db.rollback()
        return RedirectResponse(
            f"/admin/hotel/bookings/{booking_id}?error={quote('تعذّر تسجيل المخالفة. أعد المحاولة أو راجع إعدادات النظام.')}#checkout-settle",
            status_code=302,
        )
    return RedirectResponse(
        f"/admin/hotel/bookings/{booking_id}?saved=violation#checkout-settle",
        status_code=302,
    )


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
    max_occupancy: str = Form(""),
    beds_description: str = Form(""),
    allows_extra_bed: str = Form(""),
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
        mo = (max_occupancy or "").strip()
        rt.max_occupancy = int(mo) if mo.isdigit() and int(mo) > 0 else None
        rt.beds_description = (beds_description or "").strip() or None
        rt.allows_extra_bed = allows_extra_bed in ("1", "on", "true", "yes")
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
    cleaning = list(
        db.scalars(
            select(HotelRoom).where(
                HotelRoom.physical_status == RoomPhysicalStatus.CLEANING,
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
    from modules.hotel.housekeeping_links import base_url_reachable_from_phone
    from modules.messaging.service import messaging_enabled
    from modules.settings.service import get_bool

    public_base = (get_setting(db, "public_base_url", "") or "").strip()
    return templates.TemplateResponse(
        "hotel/housekeeping.html",
        {
            "request": request,
            "dirty_rooms": dirty,
            "cleaning_rooms": cleaning,
            "maintenance_rooms": maintenance,
            "issue_types": MAINTENANCE_ISSUE_TYPES,
            "default_maintenance_phone": get_setting(db, "hotel_maintenance_phone", ""),
            "default_maintenance_name": get_setting(db, "hotel_maintenance_name", ""),
            "default_cleaning_phone": get_setting(db, "hotel_cleaning_phone", ""),
            "default_cleaning_name": get_setting(db, "hotel_cleaning_name", ""),
            "maintenance_staff": maintenance_staff,
            "messaging_on": messaging_enabled(db),
            "notifications_on": get_bool(db, "notifications_enabled", True),
            "public_base_ok": base_url_reachable_from_phone(public_base),
            "saved": request.query_params.get("saved"),
            "error": request.query_params.get("error"),
            "warn": request.query_params.get("warn"),
        },
    )


@bookings_router.post("/housekeeping/settings", dependencies=[Depends(_mod_booking)])
def housekeeping_settings_save(
    db: DBSession,
    _: User = Depends(_hk),
    maintenance_phone: str = Form(""),
    maintenance_name: str = Form(""),
    cleaning_phone: str = Form(""),
    cleaning_name: str = Form(""),
):
    from modules.settings.service import set_setting

    set_setting(db, "hotel_maintenance_phone", (maintenance_phone or "").strip()[:40])
    set_setting(db, "hotel_maintenance_name", (maintenance_name or "").strip()[:80])
    set_setting(db, "hotel_cleaning_phone", (cleaning_phone or "").strip()[:40])
    set_setting(db, "hotel_cleaning_name", (cleaning_name or "").strip()[:80])
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


@bookings_router.post("/housekeeping/{room_id}/assign-cleaning", dependencies=[Depends(_mod_booking)])
def housekeeping_assign_cleaning(
    request: Request,
    room_id: int,
    db: DBSession,
    user: User = Depends(_hk),
    cleaning_phone: str = Form(""),
    cleaning_name: str = Form(""),
    employee_id: str = Form(""),
    note: str = Form(""),
):
    from modules.messaging.service import messaging_enabled
    from modules.settings.service import get_bool, get_setting

    eid = int(employee_id) if (employee_id or "").strip().isdigit() else None
    public = (get_setting(db, "public_base_url", "") or "").strip()
    base = public or str(request.base_url).rstrip("/")
    try:
        assign_room_cleaning(
            db,
            room_id,
            user_id=user.id,
            cleaning_phone=cleaning_phone,
            cleaning_staff_name=cleaning_name,
            employee_id=eid,
            note=note,
            send_notification=True,
            base_url=base,
        )
        db.commit()
    except BookingError as e:
        db.rollback()
        return RedirectResponse(
            f"/admin/hotel/housekeeping?error={quote(str(e))}", status_code=302
        )
    if not messaging_enabled(db):
        return RedirectResponse(
            "/admin/hotel/housekeeping?saved=assigned&warn=messaging_off",
            status_code=302,
        )
    if not get_bool(db, "notifications_enabled", True):
        return RedirectResponse(
            "/admin/hotel/housekeeping?saved=assigned&warn=notifications_off",
            status_code=302,
        )
    return RedirectResponse("/admin/hotel/housekeeping?saved=assigned", status_code=302)


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
                "contact_channel_label": contact_channel_label(a.contact_channel),
                "contact_value": a.contact_value,
            }
        )
    security_sent = qp.get("security_sent") == "1"
    security_sent_authority = qp.get("security_authority") or ""
    security_sent_channel = qp.get("security_channel") or ""
    selected_authority_id = None
    aid_raw = qp.get("authority_id") or ""
    if aid_raw.isdigit():
        aid = int(aid_raw)
        if any(a["id"] == aid for a in authorities):
            selected_authority_id = aid
    from modules.settings.service import get_bool, get_setting

    return templates.TemplateResponse(
        "hotel/reports.html",
        {
            "request": request,
            "occ": occ,
            "today": today.isoformat(),
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
            "selected_authority_id": selected_authority_id,
            "daily_close_auto_enabled": get_bool(
                db, "hotel_daily_close_auto_enabled", True
            ),
            "daily_close_last_date": (
                get_setting(db, "hotel_daily_close_last_date", "") or ""
            ),
        },
    )


@bookings_router.get(
    "/reports/bookings",
    response_class=HTMLResponse,
    dependencies=[Depends(_mod_booking)],
)
def hotel_bookings_period_report(request: Request, db: DBSession, _: User = Depends(_view)):
    """تقرير تأجير الشقق — عدد الشقق، القيمة، المدفوع، المتبقي."""
    from datetime import datetime, time, timedelta

    from modules.hotel.bookings_report import hotel_bookings_in_period

    qp = request.query_params
    today = date.today()
    yesterday = today - timedelta(days=1)
    month_start = today.replace(day=1)
    week_start = today - timedelta(days=today.weekday())
    raw_from = (qp.get("from") or "").strip()
    raw_to = (qp.get("to") or "").strip()
    try:
        date_from = date.fromisoformat(raw_from) if raw_from else today
    except ValueError:
        date_from = today
    try:
        date_to = date.fromisoformat(raw_to) if raw_to else today
    except ValueError:
        date_to = today
    if date_from > date_to:
        date_from, date_to = date_to, date_from

    start_dt = datetime.combine(date_from, time.min)
    # نهاية حصرية لليوم الأخير (اليوم التالي)
    end_dt = datetime.combine(date_to + timedelta(days=1), time.min)
    rows, summary = hotel_bookings_in_period(db, start_dt, end_dt)

    preset = "custom"
    if date_from == today and date_to == today:
        preset = "today"
    elif date_from == yesterday and date_to == yesterday:
        preset = "yesterday"
    elif date_from == week_start and date_to == today:
        preset = "week"
    elif date_from == month_start and date_to == today:
        preset = "month"

    return templates.TemplateResponse(
        "hotel/bookings_period_report.html",
        {
            "request": request,
            "rows": rows,
            "summary": summary,
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "today": today.isoformat(),
            "yesterday": yesterday.isoformat(),
            "week_start": week_start.isoformat(),
            "month_start": month_start.isoformat(),
            "preset": preset,
        },
    )


@bookings_router.get(
    "/reports/breakfast",
    response_class=HTMLResponse,
    dependencies=[Depends(_mod_booking)],
)
def hotel_breakfast_cost_report(request: Request, db: DBSession, _: User = Depends(_view)):
    """تقرير تكلفة وجبات الإفطار (مشمولة / معلّقة)."""
    from modules.hotel.breakfast_settle import (
        hotel_breakfast_included_enabled,
        list_breakfast_cost_rows,
    )

    qp = request.query_params
    today = date.today()
    raw_from = (qp.get("from") or "").strip()
    raw_to = (qp.get("to") or "").strip()
    try:
        date_from = date.fromisoformat(raw_from) if raw_from else today.replace(day=1)
    except ValueError:
        date_from = today.replace(day=1)
    try:
        date_to = date.fromisoformat(raw_to) if raw_to else today
    except ValueError:
        date_to = today
    if date_from > date_to:
        date_from, date_to = date_to, date_from

    rows, hotel_cost_total, pending_total = list_breakfast_cost_rows(
        db, date_from=date_from, date_to=date_to
    )
    return templates.TemplateResponse(
        "hotel/breakfast_report.html",
        {
            "request": request,
            "rows": rows,
            "hotel_cost_total": hotel_cost_total,
            "pending_total": pending_total,
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "breakfast_included_enabled": hotel_breakfast_included_enabled(db),
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
    paper: str | None = Query(None),
    orientation: str | None = Query(None),
):
    from modules.hotel.security_report_service import (
        fetch_security_report_bookings,
        filter_bookings_by_scope,
        get_security_authority,
        guest_scope_label,
    )
    from modules.platform.business_domain import BusinessDomain
    from modules.settings.service import (
        PAPER_ORIENTATIONS,
        PAPER_SIZES,
        get_paper_css,
        get_receipt_orientation,
        get_receipt_paper_size,
        normalize_orientation,
        normalize_paper,
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

    guest_rows = iter_security_guest_rows(bookings, scope=scope)
    chosen = normalize_paper(paper, get_receipt_paper_size(db, BusinessDomain.HOTEL))
    # تقارير الأمن عريضة افتراضياً
    default_orient = get_receipt_orientation(db, BusinessDomain.HOTEL)
    if paper is None and orientation is None and chosen in ("A4", "A5"):
        default_orient = "landscape"
    orient = normalize_orientation(orientation, default_orient)
    paper_css = get_paper_css(chosen, orient)
    preserve = {
        "from_date": start.isoformat() if hasattr(start, "isoformat") else str(start),
        "to_date": end.isoformat() if hasattr(end, "isoformat") else str(end),
        "print_view": str(print_view),
    }
    if authority is not None:
        preserve["authority_id"] = str(authority.id)
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
            "paper": chosen,
            "orientation": orient,
            "paper_css": paper_css,
            "paper_choices": PAPER_SIZES,
            "orientation_choices": PAPER_ORIENTATIONS,
            "can_choose_paper": True,
            "print_form_action": "/admin/hotel/reports/security",
            "print_preserve_params": preserve,
        },
    )


@bookings_router.get("/reports/security.xlsx", dependencies=[Depends(_mod_booking)])
def hotel_security_report_xlsx(
    db: DBSession,
    _: User = Depends(_view),
    from_date: str = Query(""),
    to_date: str = Query(""),
    authority_id: str = Query(""),
):
    from modules.hotel.security_report_service import (
        fetch_security_report_bookings,
        filter_bookings_by_scope,
        get_security_authority,
        security_export_table,
    )
    from modules.reporting.exports import SheetSpec, xlsx_response

    start, end = _security_report_dates(from_date, to_date)
    authority = None
    scope = None
    if authority_id and authority_id.isdigit():
        authority = get_security_authority(db, int(authority_id))
        if authority is not None:
            scope = authority.guest_scope
    bookings = filter_bookings_by_scope(
        fetch_security_report_bookings(db, start=start, end=end), scope
    )
    headers, rows = security_export_table(bookings, scope=scope)
    sheet_name = (authority.name_ar if authority else "تقرير أمني")[:31]
    fname = f"security-report-{start}-{end}"
    if authority is not None:
        fname = f"security-{authority.id}-{start}-{end}"
    return xlsx_response(fname, [SheetSpec(name=sheet_name, headers=headers, rows=rows)])


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
    from modules.hotel.booking_service import primary_staying_guest_contact

    _, staying_phone = primary_staying_guest_contact(booking)
    if staying_phone:
        return normalize_phone(staying_phone)
    if booking.guest_type == GuestType.COMPANY:
        raw = (booking.company_contact_phone or booking.guest_phone or "").strip()
    else:
        raw = (booking.guest_phone or "").strip()
    return normalize_phone(raw)


def _booking_nights(booking) -> int:
    try:
        n = int(getattr(booking, "nights", None) or 0)
        if n > 0:
            return n
        return max(0, (booking.check_out - booking.check_in).days) or (
            1 if booking.check_out == booking.check_in else 0
        )
    except Exception:  # noqa: BLE001
        return 0


def _group_folio_lines_for_invoice(folio) -> list[tuple[str, list]]:
    """تجميع بنود الفاتورة النهائية: إقامة / مطعم / مغسلة / أخرى."""
    stay: list = []
    restaurant: list = []
    laundry: list = []
    other: list = []
    for line in list(getattr(folio, "lines", None) or []):
        kind = (getattr(line, "kind", None) or "").strip().lower()
        if kind == "hotel_cost":
            continue
        amt = getattr(line, "amount", None)
        try:
            zero = float(amt or 0) == 0 and kind != "accommodation"
        except Exception:  # noqa: BLE001
            zero = False
        if zero:
            continue
        desc = (getattr(line, "description", None) or "").strip()
        desc_l = desc.lower()
        if kind == "accommodation":
            stay.append(line)
        elif kind == "laundry" or "مغسلة" in desc or "غسيل" in desc or "laundry" in desc_l:
            laundry.append(line)
        elif kind == "pos" or "مطعم" in desc or "مقهى" in desc:
            restaurant.append(line)
        else:
            other.append(line)
    groups: list[tuple[str, list]] = []
    if stay:
        groups.append(("الإقامة", stay))
    if restaurant:
        groups.append(("فواتير المطعم", restaurant))
    if laundry:
        groups.append(("فواتير المغسلة", laundry))
    if other:
        groups.append(("خدمات أخرى", other))
    if not groups:
        # fallback: كل البنود الظاهرة
        flat = [
            ln
            for ln in list(getattr(folio, "lines", None) or [])
            if (getattr(ln, "kind", None) or "") != "hotel_cost"
        ]
        if flat:
            groups.append(("تفاصيل الحساب", flat))
    return groups


@bookings_router.get("/bookings/{booking_id}/receipt", response_class=HTMLResponse)
def booking_receipt_print(
    request: Request,
    booking_id: int,
    db: DBSession,
    user: User = Depends(_view),
    autoprint: int = Query(0, ge=0, le=1),
    paper: str | None = Query(None),
    orientation: str | None = Query(None),
    doc: str | None = Query(None),
    payment_id: int | None = Query(None),
):
    import logging

    log = logging.getLogger("hotel.receipt")
    try:
        from modules.branding.service import get_hotel_branding, hotel_display_name
        from modules.hotel.booking_models import HotelBookingPayment
        from modules.platform.business_domain import BusinessDomain
        from modules.printing.doc_kind_resolve import (
            assign_hotel_doc_number,
            resolve_hotel_doc_kind,
        )
        from modules.printing.doc_numbers import DOC_KIND_LABELS, DOC_KIND_PREFIX, next_doc_number
        from modules.receipt_whatsapp.service import booking_phone_hint, whatsapp_receipt_ctx
        from modules.settings.service import (
            PAPER_ORIENTATIONS,
            PAPER_SIZES,
            get_paper_css,
            get_receipt_orientation,
            get_receipt_paper_size,
            normalize_orientation,
            normalize_paper,
        )

        booking = get_booking(db, booking_id)
        if booking is None:
            return RedirectResponse(
                f"/admin/hotel/bookings?error={quote('الحجز غير موجود')}",
                status_code=302,
            )
        try:
            # فاتورة النزيل النهائية: بنود مختصرة بدون تفاصيل أصناف المطعم/المغسلة
            folio = build_folio(db, booking_id, pos_item_details=False)
            debt_breakdown = folio_debt_breakdown(db, booking_id)
            invoice_line_groups = _group_folio_lines_for_invoice(folio)
        except Exception as exc:  # noqa: BLE001
            log.exception("build_folio failed booking=%s", booking_id)
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
            return RedirectResponse(
                f"/admin/hotel/bookings/{booking_id}?error="
                + quote(f"تعذّر بناء كشف الحساب: {exc}"),
                status_code=302,
            )
        try:
            payment_ledger, payments_gross, payments_refunded = build_account_ledger(
                db, booking
            )
        except Exception:  # noqa: BLE001
            log.exception("account_ledger failed booking=%s", booking_id)
            try:
                payment_ledger, payments_gross, payments_refunded = build_payment_ledger(
                    booking
                )
            except Exception:  # noqa: BLE001
                payment_ledger, payments_gross, payments_refunded = (
                    [],
                    Decimal("0"),
                    Decimal("0"),
                )

        paper = normalize_paper(paper, get_receipt_paper_size(db, BusinessDomain.HOTEL))
        orient = normalize_orientation(
            orientation, get_receipt_orientation(db, BusinessDomain.HOTEL)
        )
        try:
            from modules.printing.service import get_receipt_printer, thermal_paper_code

            if get_receipt_printer(db, BusinessDomain.HOTEL) is not None:
                paper = normalize_paper(thermal_paper_code(db), "80mm")
                orient = "portrait"
        except Exception:  # noqa: BLE001
            pass

        focus_payment = None
        payment_method_name = ""

        def _payment_method_label(pay) -> str:
            if pay is None or not getattr(pay, "payment_method_id", None):
                return ""
            try:
                from modules.payments.models import PaymentMethod

                pm = db.get(PaymentMethod, int(pay.payment_method_id))
                return _hotel_wallet_label(db, pm) if pm else ""
            except Exception:  # noqa: BLE001
                return ""

        if payment_id:
            focus_payment = db.get(HotelBookingPayment, payment_id)
            if focus_payment is None or int(focus_payment.booking_id) != int(booking_id):
                focus_payment = None
            else:
                payment_method_name = _payment_method_label(focus_payment)

        from modules.printing.doc_numbers import PrintDocKind

        doc_kind = resolve_hotel_doc_kind(
            booking,
            requested=doc,
            payment_id=int(focus_payment.id) if focus_payment else None,
        )
        # إيصال القبض: ركّز على آخر دفعة إن لم يُحدَّد رقم دفعة
        if focus_payment is None and doc_kind == PrintDocKind.RECEIPT:
            try:
                pays = sorted(
                    (
                        p
                        for p in (booking.payments or [])
                        if not getattr(p, "is_refunded", False)
                    ),
                    key=lambda p: (p.created_at or p.id, p.id),
                    reverse=True,
                )
                if pays:
                    focus_payment = pays[0]
                    payment_method_name = _payment_method_label(focus_payment)
            except Exception:  # noqa: BLE001
                pass

        try:
            doc_number = assign_hotel_doc_number(
                db, booking, doc_kind, payment=focus_payment
            )
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
            try:
                doc_number = next_doc_number(db, doc_kind, domain="hotel")
                db.commit()
            except Exception:  # noqa: BLE001
                db.rollback()
                prefix = DOC_KIND_PREFIX.get(doc_kind, "DOC")
                doc_number = f"{prefix}-{booking.id:06d}"

        try:
            wa_phone = booking_phone_hint(booking, db)
        except Exception:  # noqa: BLE001
            wa_phone = ""
        try:
            guest_phone = _booking_guest_phone_display(booking)
        except Exception:  # noqa: BLE001
            guest_phone = (booking.guest_phone or "")[:40]
        try:
            from modules.hotel.booking_service import build_hotel_invoice_party
            from modules.customers.models import Customer

            # حمّل بطاقة الشركة/العميل لهاتف الجهة إن لزم
            if getattr(booking, "company_customer_id", None):
                co = db.get(Customer, int(booking.company_customer_id))
                if co is not None:
                    try:
                        booking.company_customer = co  # type: ignore[attr-defined]
                    except Exception:  # noqa: BLE001
                        pass
                    if not (getattr(booking, "company_contact_phone", None) or "").strip():
                        if (co.phone or "").strip():
                            booking.company_contact_phone = co.phone
                    if not (getattr(booking, "company_name", None) or "").strip():
                        booking.company_name = (co.company_name or co.name or "").strip() or None
            if getattr(booking, "customer_id", None) and not getattr(booking, "customer", None):
                cu = db.get(Customer, int(booking.customer_id))
                if cu is not None:
                    try:
                        booking.customer = cu  # type: ignore[attr-defined]
                    except Exception:  # noqa: BLE001
                        pass

            invoice_party = build_hotel_invoice_party(booking)
        except Exception:  # noqa: BLE001
            invoice_party = {
                "is_company": False,
                "payer_label": "الجهة الدافعة",
                "payer_name": (booking.guest_name or booking.display_name or "—"),
                "payer_phone": guest_phone,
                "payer_contact_name": "",
                "resident_name": (booking.guest_name or "—"),
                "resident_phone": guest_phone,
                "room_label": (booking.room.number if booking.room else "—"),
                "room_type": "",
                "check_in": booking.check_in,
                "check_out": booking.check_out,
                "nights": _booking_nights(booking),
                "payer_is_resident": True,
                "entity_name": "",
                "entity_phone": "",
                "show_entity_block": False,
            }
        try:
            hotel_brand = get_hotel_branding(db)
            store_name = hotel_display_name(db)
        except Exception:  # noqa: BLE001
            hotel_brand = {"hotel_name": "الفندق", "hotel_logo_url": "", "hotel_print_logo_url": ""}
            store_name = "الفندق"
        try:
            silent_ctx = _hotel_receipt_silent_ctx(db)
        except Exception:  # noqa: BLE001
            silent_ctx = {
                "silent_print_enabled": False,
                "receipt_printer_name": None,
            }
        try:
            wa_ctx = whatsapp_receipt_ctx(
                db,
                domain="hotel",
                phone=wa_phone,
                send_url=(
                    f"/admin/hotel/bookings/{booking_id}/receipt/send-whatsapp"
                    f"?doc={doc_kind.value}"
                    + (f"&payment_id={focus_payment.id}" if focus_payment else "")
                ),
            )
        except Exception:  # noqa: BLE001
            wa_ctx = {
                "whatsapp_receipt_enabled": False,
                "whatsapp_receipt_phone": "",
                "whatsapp_send_url": "",
            }

        preserve = {"doc": doc_kind.value}
        if focus_payment:
            preserve["payment_id"] = str(focus_payment.id)
        if autoprint:
            preserve["autoprint"] = "1"
        if (request.query_params.get("return_booking") or "").strip() == "1":
            preserve["return_booking"] = "1"

        doc_title = DOC_KIND_LABELS.get(doc_kind, "إيصال قبض")
        return templates.TemplateResponse(
            "hotel/booking_receipt.html",
            {
                "request": request,
                "booking": booking,
                "folio": folio,
                "invoice_line_groups": invoice_line_groups,
                "debt_breakdown": debt_breakdown,
                "pos_item_details": False,
                "payment_ledger": payment_ledger,
                "payments_gross": payments_gross,
                "payments_refunded": payments_refunded,
                "focus_payment": focus_payment,
                "payment_method_name": payment_method_name,
                "doc_kind": doc_kind.value,
                "doc_title": doc_title,
                "doc_number": doc_number,
                "guest_phone": guest_phone,
                "invoice_party": invoice_party,
                "nights": invoice_party.get("nights") or _booking_nights(booking),
                "hotel_brand": hotel_brand,
                "store_name": store_name,
                "autoprint": autoprint,
                "embed": 0,
                "paper": paper,
                "orientation": orient,
                "paper_css": get_paper_css(paper, orient),
                "paper_choices": PAPER_SIZES,
                "orientation_choices": PAPER_ORIENTATIONS,
                "can_choose_paper": True,
                "print_form_action": f"/admin/hotel/bookings/{booking_id}/receipt",
                "print_preserve_params": preserve,
                "silent_print_url": f"/admin/hotel/bookings/{booking_id}/receipt/silent-print",
                **silent_ctx,
                **wa_ctx,
            },
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("booking receipt 500 booking_id=%s", booking_id)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return RedirectResponse(
            f"/admin/hotel/bookings/{booking_id}?error={quote('فشل فتح الإيصال: ' + str(exc)[:180])}",
            status_code=302,
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
    doc = (request.query_params.get("doc") or "").strip().lower()
    payment_id_raw = (request.query_params.get("payment_id") or "").strip()
    focus_amt = None
    doc_title = "فاتورة نهائية" if doc in ("invoice", "final", "فاتورة") else "إيصال قبض"
    if payment_id_raw.isdigit():
        from modules.hotel.booking_models import HotelBookingPayment

        pay = db.get(HotelBookingPayment, int(payment_id_raw))
        if pay is not None and int(pay.booking_id) == int(booking_id):
            focus_amt = pay.amount
            doc_title = "إيصال قبض"
    try:
        result = send_hotel_receipt_whatsapp(
            db,
            booking,
            folio_total=folio.total,
            folio_paid=folio.paid,
            folio_balance=folio.balance,
            focus_payment_amount=focus_amt,
            doc_title=doc_title,
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
