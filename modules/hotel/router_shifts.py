"""ورديات الفندق — فتح وإقفال."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.deps import DBSession, require_any_permission, require_permission
from app.jinja_env import templates
from modules.authz.kiosk import requires_hotel_shift_pin
from modules.authz.models import User
from modules.authz.permissions import (
    HOTEL_BOOKING_CHECKIN,
    HOTEL_BOOKING_CREATE,
    HOTEL_BOOKING_MANAGE,
    HOTEL_BOOKING_VIEW,
    HOTEL_FINANCE_CLOSE,
)
from modules.hotel.shift_activity import compute_hotel_shift_activity
from modules.hotel.shift_close_rows import build_hotel_shift_close_rows
from modules.hotel.shift_expenses import (
    list_hotel_expense_methods,
    list_shift_expenses,
    record_hotel_shift_expense,
)
from modules.hotel.shift_schedules import (
    DEFAULT_SCHEDULES,
    OVERDUE_ENABLED_KEY,
    OVERDUE_MINUTES_KEY,
    SUPERVISOR_PHONE_KEY,
    active_shift_slot,
    load_shift_schedules,
    overdue_alerts_enabled,
    overdue_grace_minutes,
    save_shift_schedules,
    supervisor_phone,
)
from modules.hotel.shift_models import HotelShiftError
from modules.hotel.shift_service import (
    close_shift,
    compute_shift_summary,
    format_shift_window,
    get_open_shift,
    get_shift,
    list_recent_shifts,
    open_shift,
    shift_is_overdue,
    shift_overdue_minutes,
)
from modules.hotel.shift_session import (
    HotelShiftSessionError,
    clear_hotel_operator_session,
    guard_hotel_shift_start_page,
    open_hotel_shift_after_pin,
    pin_authenticate_for_hotel_shift,
    require_hotel_shift_session,
    session_hotel_employee_id,
    sync_session_hotel_shift,
)
from app.datetime_local import ensure_utc, format_local_dt
from modules.settings.service import invalidate_settings_cache, set_setting

shifts_router = APIRouter(prefix="/admin/hotel", tags=["hotel-shifts"])

# موظف الحجوزات يفتح/يقفل الجلسة مثل كاشير المطعم
_shift_view = require_any_permission(
    HOTEL_BOOKING_VIEW,
    HOTEL_BOOKING_CREATE,
    HOTEL_BOOKING_MANAGE,
    HOTEL_FINANCE_CLOSE,
)
_shift_manage = require_any_permission(
    HOTEL_BOOKING_CREATE,
    HOTEL_BOOKING_CHECKIN,
    HOTEL_BOOKING_MANAGE,
    HOTEL_FINANCE_CLOSE,
)
_shift_settings = require_permission(HOTEL_FINANCE_CLOSE)


@shifts_router.get("/pin", response_class=HTMLResponse)
def hotel_pin_page(request: Request, db: DBSession, user: User = Depends(_shift_view)):
    if not requires_hotel_shift_pin(user):
        return RedirectResponse("/admin/hotel/shift", status_code=302)
    open_s = get_open_shift(db)
    if open_s and session_hotel_employee_id(request):
        return RedirectResponse("/admin/hotel/dashboard", status_code=302)
    return templates.TemplateResponse(
        "hotel/pin.html",
        {
            "request": request,
            "error": request.query_params.get("error"),
        },
    )


@shifts_router.post("/pin/verify")
def hotel_pin_verify(
    request: Request,
    db: DBSession,
    user: User = Depends(_shift_view),
    pin: str = Form(...),
):
    if not requires_hotel_shift_pin(user):
        return RedirectResponse("/admin/hotel/shift", status_code=302)
    try:
        existing, need_start = pin_authenticate_for_hotel_shift(db, request, user, pin)
        db.commit()
    except HotelShiftSessionError as e:
        db.rollback()
        return RedirectResponse(f"/admin/hotel/pin?error={quote(str(e))}", status_code=302)
    if need_start:
        return RedirectResponse("/admin/hotel/shift/start", status_code=302)
    return RedirectResponse("/admin/hotel/dashboard", status_code=302)


@shifts_router.post("/pin/logout")
def hotel_pin_logout(request: Request, user: User = Depends(_shift_view)):
    clear_hotel_operator_session(request)
    if requires_hotel_shift_pin(user):
        return RedirectResponse("/admin/hotel/pin", status_code=302)
    return RedirectResponse("/admin/hotel/shift", status_code=302)


@shifts_router.get("/shift/start", response_class=HTMLResponse)
def hotel_shift_start_page(request: Request, db: DBSession, user: User = Depends(_shift_view)):
    blocked = guard_hotel_shift_start_page(request, db, user)
    if blocked is not None:
        return blocked
    slot = active_shift_slot(db)
    return templates.TemplateResponse(
        "hotel/shift_start.html",
        {
            "request": request,
            "current_slot": slot,
            "shift_window": (
                f"{format_local_dt(slot.scheduled_start, '%H:%M')} — "
                f"{format_local_dt(slot.scheduled_end, '%H:%M')}"
            ),
            "error": request.query_params.get("error"),
        },
    )


@shifts_router.post("/shift/start")
def hotel_shift_start_submit(
    request: Request,
    db: DBSession,
    user: User = Depends(_shift_manage),
    shift_number: str = Form(""),
    opening_note: str = Form(""),
    opening_cash: str = Form("0"),
    no_opening_float: str = Form(""),
):
    blocked = guard_hotel_shift_start_page(request, db, user)
    if blocked is not None:
        return blocked
    num = int(shift_number) if (shift_number or "").strip().isdigit() else None
    cash = "0" if no_opening_float == "on" else opening_cash
    try:
        if requires_hotel_shift_pin(user):
            open_hotel_shift_after_pin(
                db,
                request,
                user,
                shift_number=num,
                opening_note=opening_note,
                opening_cash=cash,
            )
        else:
            shift = open_shift(
                db,
                user_id=user.id,
                opening_note=opening_note,
                shift_number=num,
                opening_cash=cash,
            )
            request.session["hotel_shift_id"] = shift.id
        db.commit()
    except (HotelShiftError, HotelShiftSessionError) as e:
        db.rollback()
        return RedirectResponse(
            f"/admin/hotel/shift/start?error={quote(str(e))}", status_code=302
        )
    return RedirectResponse("/admin/hotel/dashboard?saved=session_opened", status_code=302)


@shifts_router.get("/shift", response_class=HTMLResponse)
def hotel_shift_page(request: Request, db: DBSession, user: User = Depends(_shift_view)):
    guard = require_hotel_shift_session(request, db, user)
    if isinstance(guard, RedirectResponse):
        return guard

    open_s = get_open_shift(db) or guard
    slot = active_shift_slot(db)
    summary = compute_shift_summary(db, open_s) if open_s else None
    activity = None
    close_rows = []
    if open_s:
        from datetime import datetime, timezone

        from modules.hotel.shift_close_rows import hotel_shift_expected_drawers

        end_at = (
            ensure_utc(open_s.closed_at)
            if open_s.closed_at
            else datetime.now(timezone.utc)
        )
        activity = compute_hotel_shift_activity(db, ensure_utc(open_s.opened_at), end_at)
        exp_cash, exp_bank, cash_exp, bank_exp = hotel_shift_expected_drawers(
            db, open_s, activity
        )
        close_rows = build_hotel_shift_close_rows(
            activity,
            expected_cash=exp_cash,
            expected_bank=exp_bank,
            opening_cash=open_s.opening_cash,
            cash_expenses=cash_exp,
            bank_expenses=bank_exp,
        )
    expenses = list_shift_expenses(db, open_s.id) if open_s else []
    methods = list_hotel_expense_methods(db) if open_s else []
    overdue = False
    overdue_mins = 0
    if open_s is not None:
        grace = overdue_grace_minutes(db)
        overdue = shift_is_overdue(open_s, grace_minutes=grace)
        overdue_mins = shift_overdue_minutes(open_s) if overdue else 0
    window_label = (
        format_shift_window(open_s)
        if open_s
        else f"{format_local_dt(slot.scheduled_start, '%H:%M')} — {format_local_dt(slot.scheduled_end, '%H:%M')}"
    )
    closed_id = request.query_params.get("closed")
    closed_shift = None
    if closed_id and str(closed_id).isdigit():
        closed_shift = get_shift(db, int(closed_id))
    debt_due = []
    debt_due_count = 0
    try:
        from modules.hotel.booking_debts import list_debts_due_for_shift_followup

        debt_due = list_debts_due_for_shift_followup(db)
        debt_due_count = len(debt_due)
    except Exception:
        debt_due = []
        debt_due_count = 0
    return templates.TemplateResponse(
        "hotel/shift.html",
        {
            "request": request,
            "open_shift": open_s,
            "current_slot": slot,
            "summary": summary,
            "activity": activity,
            "close_rows": close_rows,
            "expenses": expenses,
            "methods": methods,
            "shift_window": window_label,
            "overdue": overdue,
            "overdue_minutes": overdue_mins,
            "debt_due": debt_due,
            "debt_due_count": debt_due_count,
            "recent": list_recent_shifts(db, limit=10),
            "saved": request.query_params.get("saved"),
            "error": request.query_params.get("error"),
            "closed_shift_id": closed_id,
            "closed_shift": closed_shift,
            "requires_pin": requires_hotel_shift_pin(user),
        },
    )


@shifts_router.post("/shift/open")
def hotel_shift_open(
    request: Request,
    db: DBSession,
    user: User = Depends(_shift_manage),
    shift_number: str = Form(""),
    opening_note: str = Form(""),
):
    if requires_hotel_shift_pin(user):
        return RedirectResponse("/admin/hotel/shift/start", status_code=302)
    num = int(shift_number) if (shift_number or "").strip().isdigit() else None
    try:
        shift = open_shift(
            db,
            user_id=user.id,
            opening_note=opening_note,
            shift_number=num,
        )
        request.session["hotel_shift_id"] = shift.id
        db.commit()
    except HotelShiftError as e:
        db.rollback()
        return RedirectResponse(f"/admin/hotel/shift?error={quote(str(e))}", status_code=302)
    return RedirectResponse("/admin/hotel/shift?saved=opened", status_code=302)


@shifts_router.post("/shift/close")
def hotel_shift_close(
    request: Request,
    db: DBSession,
    user: User = Depends(_shift_manage),
    closing_note: str = Form(""),
    counted_cash: str = Form(""),
    counted_bank: str = Form(""),
    counted_bookings: str = Form(""),
    counted_meals: str = Form(""),
    counted_laundry: str = Form(""),
    counted_services: str = Form(""),
):
    open_s = get_open_shift(db)
    if open_s is None:
        return RedirectResponse("/admin/hotel/shift?error=لا+جلسة+مفتوحة", status_code=302)

    cash_raw = (counted_cash or "").strip()
    bank_raw = (counted_bank or "").strip()
    if not cash_raw:
        return RedirectResponse(
            "/admin/hotel/shift?error="
            + quote("أدخل المبلغ المعدود للكاش بعد عدّ الدرج — لا يمكن الإقفال بدون ذلك."),
            status_code=302,
        )
    if not bank_raw:
        return RedirectResponse(
            "/admin/hotel/shift?error="
            + quote("أدخل المبلغ المعدود للمصرف/التحويل بعد مراجعة الإيصالات."),
            status_code=302,
        )
    for label, raw in (
        ("عمليات الحجز", counted_bookings),
        ("الوجبات المسوّاة", counted_meals),
        ("المغسلة", counted_laundry),
        ("الخدمات", counted_services),
    ):
        if not (raw or "").strip():
            return RedirectResponse(
                "/admin/hotel/shift?error="
                + quote(f"أدخل العدد المعدود لبند «{label}»."),
                status_code=302,
            )
    try:
        close_shift(
            db,
            open_s.id,
            user_id=user.id,
            closing_note=closing_note,
            counted_cash=Decimal(cash_raw),
            counted_bank=Decimal(bank_raw),
            counted_bookings=counted_bookings,
            counted_meals=counted_meals,
            counted_laundry=counted_laundry,
            counted_services=counted_services,
            require_counts=True,
        )
        db.commit()
        shift_id = open_s.id
        clear_hotel_operator_session(request)
    except (HotelShiftError, InvalidOperation) as e:
        db.rollback()
        return RedirectResponse(f"/admin/hotel/shift?error={quote(str(e))}", status_code=302)
    # نفس نمط المطعم: بعد الإقفال شاشة الجلسة مع ملخص، أو شاشة الرقم السري للكاشير
    if requires_hotel_shift_pin(user):
        return RedirectResponse(
            f"/admin/hotel/pin?closed={shift_id}", status_code=302
        )
    return RedirectResponse(
        f"/admin/hotel/shift?closed={shift_id}&saved=closed", status_code=302
    )


@shifts_router.post("/shift/expense")
def hotel_shift_expense(
    db: DBSession,
    user: User = Depends(_shift_manage),
    amount: str = Form(...),
    payment_method_id: str = Form(...),
    category: str = Form("مصروف جلسة"),
    note: str = Form(""),
):
    open_s = get_open_shift(db)
    if open_s is None:
        return RedirectResponse("/admin/hotel/shift?error=لا+جلسة+مفتوحة", status_code=302)
    if not (payment_method_id or "").strip().isdigit():
        return RedirectResponse("/admin/hotel/shift?error=اختر+وسيلة+الدفع", status_code=302)
    try:
        record_hotel_shift_expense(
            db,
            shift_id=open_s.id,
            amount=Decimal(amount or "0"),
            payment_method_id=int(payment_method_id),
            category=category,
            note=note,
            user_id=user.id,
        )
        db.commit()
    except (HotelShiftError, InvalidOperation) as e:
        db.rollback()
        return RedirectResponse(f"/admin/hotel/shift?error={quote(str(e))}", status_code=302)
    return RedirectResponse("/admin/hotel/shift?saved=expense", status_code=302)


@shifts_router.get("/shift/settings", response_class=HTMLResponse)
def hotel_shift_settings_page(request: Request, db: DBSession, _: User = Depends(_shift_settings)):
    schedules = load_shift_schedules(db)
    if len(schedules) < 3:
        schedules = [dict(x) for x in DEFAULT_SCHEDULES]
    return templates.TemplateResponse(
        "hotel/shift_settings.html",
        {
            "request": request,
            "schedules": schedules,
            "overdue_minutes": overdue_grace_minutes(db),
            "supervisor_phone": supervisor_phone(db),
            "overdue_enabled": overdue_alerts_enabled(db),
            "saved": request.query_params.get("saved"),
        },
    )


@shifts_router.post("/shift/settings")
def hotel_shift_settings_save(
    db: DBSession,
    _: User = Depends(_shift_settings),
    s1_name: str = Form("الوردية الأولى"),
    s1_start: str = Form("08:00"),
    s1_end: str = Form("16:00"),
    s2_name: str = Form("الوردية الثانية"),
    s2_start: str = Form("16:00"),
    s2_end: str = Form("00:00"),
    s3_name: str = Form("الوردية الثالثة"),
    s3_start: str = Form("00:00"),
    s3_end: str = Form("08:00"),
    overdue_minutes: str = Form("30"),
    supervisor_phone_val: str = Form(""),
    overdue_enabled: str = Form(""),
):
    save_shift_schedules(
        db,
        [
            {"number": 1, "name_ar": s1_name, "start": s1_start, "end": s1_end},
            {"number": 2, "name_ar": s2_name, "start": s2_start, "end": s2_end},
            {"number": 3, "name_ar": s3_name, "start": s3_start, "end": s3_end},
        ],
    )
    set_setting(db, OVERDUE_MINUTES_KEY, str(max(5, int(overdue_minutes or "30"))))
    set_setting(db, SUPERVISOR_PHONE_KEY, (supervisor_phone_val or "").strip())
    set_setting(db, OVERDUE_ENABLED_KEY, "1" if overdue_enabled == "on" else "0")
    invalidate_settings_cache()
    db.commit()
    return RedirectResponse("/admin/hotel/shift/settings?saved=1", status_code=302)


@shifts_router.get("/shift/{shift_id}/report", response_class=HTMLResponse)
def hotel_shift_report(shift_id: int, request: Request, db: DBSession, _: User = Depends(_shift_view)):
    import json
    from datetime import datetime, timezone
    from decimal import Decimal

    from modules.hotel.revenue_stats import hotel_collections_detail
    from modules.hotel.shift_activity import (
        list_shift_laundry_orders,
        list_shift_restaurant_orders,
    )

    def _pm_kind(name: str | None) -> str:
        text = (name or "").strip()
        low = text.lower()
        if "كاش" in text or "cash" in low:
            return "cash"
        if "مصرف" in text or "bank" in low:
            return "bank"
        return "other"

    shift = get_shift(db, shift_id)
    if shift is None:
        return RedirectResponse("/admin/hotel/shift?error=الجلسة+غير+موجودة", status_code=302)
    summary = compute_shift_summary(db, shift)
    expenses = list_shift_expenses(db, shift.id)
    start = ensure_utc(shift.opened_at)
    end = ensure_utc(shift.closed_at) if shift.closed_at else datetime.now(timezone.utc)
    collections, collections_summary = hotel_collections_detail(
        db, start, end, hotel_shift_id=shift.id
    )
    activity = None
    if shift.close_snapshot_json:
        try:
            activity = json.loads(shift.close_snapshot_json)
        except json.JSONDecodeError:
            activity = None
    if activity is None:
        activity = compute_hotel_shift_activity(db, start, end).to_dict()

    view = (request.query_params.get("view") or "").strip().lower()
    if view not in ("cash", "bank", "restaurant", "laundry"):
        view = ""

    detail_collections = []
    restaurant_orders = []
    laundry_orders = []
    if view == "cash":
        detail_collections = [
            c for c in collections if _pm_kind(c.payment_method) == "cash"
        ]
    elif view == "bank":
        detail_collections = [
            c for c in collections if _pm_kind(c.payment_method) == "bank"
        ]
    elif view == "restaurant":
        restaurant_orders = list_shift_restaurant_orders(db, start, end)
    elif view == "laundry":
        laundry_orders = list_shift_laundry_orders(db, start, end)

    def _act_dec(key: str) -> Decimal:
        try:
            return Decimal(str((activity or {}).get(key) or 0))
        except (InvalidOperation, ValueError, TypeError):
            return Decimal("0")

    cash_net = (
        Decimal(str(collections_summary.cash_in or 0))
        - Decimal(str(collections_summary.cash_out or 0))
    ).quantize(Decimal("0.001"))
    bank_net = (
        Decimal(str(collections_summary.bank_in or 0))
        - Decimal(str(collections_summary.bank_out or 0))
    ).quantize(Decimal("0.001"))

    return templates.TemplateResponse(
        "hotel/shift_report.html",
        {
            "request": request,
            "shift": shift,
            "summary": summary,
            "expenses": expenses,
            "collections": collections,
            "collections_summary": collections_summary,
            "activity": activity,
            "shift_window": format_shift_window(shift),
            "saved": request.query_params.get("saved"),
            "detail_view": view or None,
            "detail_collections": detail_collections,
            "restaurant_orders": restaurant_orders,
            "laundry_orders": laundry_orders,
            "card_cash_total": cash_net,
            "card_bank_total": bank_net,
            "card_restaurant_count": int((activity or {}).get("meals_settled_count") or 0),
            "card_restaurant_total": _act_dec("meals_settled_total"),
            "card_laundry_count": int((activity or {}).get("laundry_count") or 0),
            "card_laundry_total": _act_dec("laundry_total"),
        },
    )
