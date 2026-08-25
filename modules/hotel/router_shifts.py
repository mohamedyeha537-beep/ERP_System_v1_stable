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
    PAYMENTS_MANAGE,
    POS_SHIFT_REOPEN,
    TREASURY_HANDOFF_APPROVE,
    TREASURY_HANDOFF_REVOKE,
)
from modules.hotel.shift_activity import compute_hotel_shift_activity
from modules.hotel.shift_close_rows import build_hotel_shift_close_rows
from modules.hotel.shift_expenses import (
    hotel_shift_expense_ui_context,
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
    list_active_reception_employees,
    list_recent_shifts,
    open_shift,
    peek_pending_hotel_carry_offer,
    shift_is_overdue,
    shift_overdue_minutes,
)
from modules.hr.service import get_employee_by_user_id
from modules.payments.shift_handovers import (
    ShiftHandoverError,
    confirm_cash_carry,
    declare_bank_transfer,
    get_bank_declaration,
    hotel_handover_progress,
    last_cash_handover_recipient,
    list_incoming_cash_handovers,
    parse_bank_transferred_at,
    propose_cash_carry,
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


def _hotel_handoff_defaults(db, shift) -> tuple:
    from modules.payments.shift_carry import money3
    from modules.payments.shift_handovers import handed_claimed_totals

    handed_c, handed_b = handed_claimed_totals(db, hotel_shift_id=shift.id)
    raw_c = shift.counted_cash if shift.counted_cash is not None else shift.expected_cash
    raw_b = shift.counted_bank if shift.counted_bank is not None else shift.expected_bank
    return max(money3(raw_c) - handed_c, money3(0)), max(money3(raw_b) - handed_b, money3(0))


def _actor_hotel_employee_id(request: Request, db, user: User) -> int | None:
    sid = session_hotel_employee_id(request)
    if sid:
        return int(sid)
    emp = get_employee_by_user_id(db, user.id)
    return int(emp.id) if emp is not None else None


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
    emp_id = session_hotel_employee_id(request)
    open_s = get_open_shift(db)
    if (
        open_s is not None
        and emp_id
        and open_s.employee_id
        and int(open_s.employee_id) != int(emp_id)
    ):
        incoming = list_incoming_cash_handovers(db, int(emp_id))
        if incoming:
            return RedirectResponse("/admin/hotel/shift/incoming", status_code=302)
        return RedirectResponse(
            "/admin/hotel/dashboard?error="
            + quote("الجلسة الحالية لموظف آخر. أكد استلام العهدة أو انتظر إقفالها."),
            status_code=302,
        )
    return RedirectResponse("/admin/hotel/dashboard", status_code=302)


@shifts_router.post("/pin/logout")
def hotel_pin_logout(request: Request, user: User = Depends(_shift_view)):
    clear_hotel_operator_session(request)
    if requires_hotel_shift_pin(user):
        return RedirectResponse("/admin/hotel/pin", status_code=302)
    return RedirectResponse("/admin/hotel/shift", status_code=302)


@shifts_router.get("/shift/start", response_class=HTMLResponse)
def hotel_shift_start_page(request: Request, db: DBSession, user: User = Depends(_shift_view)):
    from modules.authz.capability import treasury_clerk_desk_redirect

    blocked_clerk = treasury_clerk_desk_redirect(user)
    if blocked_clerk is not None:
        return blocked_clerk
    blocked = guard_hotel_shift_start_page(request, db, user)
    if blocked is not None:
        return blocked
    slot = active_shift_slot(db)
    carry = peek_pending_hotel_carry_offer(db)
    emp_id = _actor_hotel_employee_id(request, db, user)
    incoming = list_incoming_cash_handovers(db, emp_id) if emp_id else []
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
            "carry_offer": carry,
            "incoming_handovers": incoming,
            "incoming_confirm_action": "/admin/hotel/shift/handover/confirm",
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
    received_cash: str = Form(""),
    received_bank: str = Form(""),
):
    blocked = guard_hotel_shift_start_page(request, db, user)
    if blocked is not None:
        return blocked
    num = int(shift_number) if (shift_number or "").strip().isdigit() else None
    cash = "0" if no_opening_float == "on" else opening_cash
    rec_cash = received_cash.strip() if (received_cash or "").strip() else None
    rec_bank = received_bank.strip() if (received_bank or "").strip() else None
    try:
        if requires_hotel_shift_pin(user):
            open_hotel_shift_after_pin(
                db,
                request,
                user,
                shift_number=num,
                opening_note=opening_note,
                opening_cash=cash,
                received_cash=rec_cash,
                received_bank=rec_bank,
            )
        else:
            shift = open_shift(
                db,
                user_id=user.id,
                opening_note=opening_note,
                shift_number=num,
                opening_cash=cash,
                received_cash=rec_cash,
                received_bank=rec_bank,
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
    from modules.authz.capability import treasury_clerk_desk_redirect

    blocked_clerk = treasury_clerk_desk_redirect(user)
    if blocked_clerk is not None:
        return blocked_clerk
    guard = require_hotel_shift_session(request, db, user)
    if isinstance(guard, RedirectResponse):
        return guard

    open_s = get_open_shift(db) or guard
    slot = active_shift_slot(db)
    summary = compute_shift_summary(db, open_s) if open_s else None
    activity = None
    close_rows = []
    close_rows_error = None
    handover_progress = None
    if open_s:
        from datetime import datetime, timezone

        from modules.hotel.shift_close_rows import hotel_shift_expected_drawers

        try:
            end_at = (
                ensure_utc(open_s.closed_at)
                if open_s.closed_at
                else datetime.now(timezone.utc)
            )
            activity = compute_hotel_shift_activity(
                db, ensure_utc(open_s.opened_at), end_at
            )
            exp_cash, exp_bank, cash_exp, bank_exp = hotel_shift_expected_drawers(
                db, open_s, activity
            )
            handover_progress = hotel_handover_progress(
                db, open_s, expected_cash=exp_cash, expected_bank=exp_bank
            )
            count_cash, count_bank = exp_cash, exp_bank
            if handover_progress.rows:
                count_cash, count_bank = (
                    handover_progress.remainder_cash,
                    handover_progress.remainder_bank,
                )
            close_rows = build_hotel_shift_close_rows(
                activity,
                expected_cash=count_cash,
                expected_bank=count_bank,
                opening_cash=open_s.opening_cash,
                opening_bank=getattr(open_s, "opening_bank", None),
                cash_expenses=cash_exp,
                bank_expenses=bank_exp,
            )
            if not close_rows:
                close_rows_error = "تعذّر بناء بنود عدّ الكاش والمصرف."
        except Exception as exc:  # noqa: BLE001
            close_rows = []
            close_rows_error = f"تعذّر تحميل بنود العدّ: {exc}"
    expenses = list_shift_expenses(db, open_s.id) if open_s else []
    methods = list_hotel_expense_methods(db) if open_s else []
    expense_ui = hotel_shift_expense_ui_context(db) if open_s else {
        "shift_expense_categories": [],
        "shift_expense_advance_keys": [],
        "shift_expense_employees": [],
    }
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
    from modules.payments.shift_carry import load_hotel_shift_close_policy

    close_policy = load_hotel_shift_close_policy(db)
    carry_recipients = (
        list_active_reception_employees(
            db, exclude_employee_id=getattr(open_s, "employee_id", None)
        )
        if open_s
        else []
    )
    return templates.TemplateResponse(
        "hotel/shift.html",
        {
            "request": request,
            "open_shift": open_s,
            "current_slot": slot,
            "summary": summary,
            "activity": activity,
            "close_rows": close_rows,
            "close_rows_error": close_rows_error,
            "expenses": expenses,
            "methods": methods,
            **expense_ui,
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
            "close_policy": close_policy,
            "carry_recipients": carry_recipients,
            "handover_progress": handover_progress,
            "handover_action": "/admin/hotel/shift/handover",
            "default_carry_employee_id": (
                last_cash_handover_recipient(db, hotel_shift_id=open_s.id)
                if open_s
                else None
            ),
            "bank_declaration": (
                get_bank_declaration(db, hotel_shift_id=open_s.id) if open_s else None
            ),
            "bank_declare_action": (
                f"/admin/hotel/shift/{open_s.id}/bank-transfer" if open_s else ""
            ),
            "bank_default": (
                handover_progress.remainder_bank
                if handover_progress
                else getattr(open_s, "expected_bank", 0)
            )
            if open_s
            else 0,
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
    from modules.hotel.shift_service import get_pending_hotel_carry

    if get_pending_hotel_carry(db) is not None:
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
    close_destination: str = Form(""),
    carried_to_employee_id: str = Form(""),
):
    open_s = get_open_shift(db)
    if open_s is None:
        return RedirectResponse("/admin/hotel/shift?error=لا+جلسة+مفتوحة", status_code=302)

    cash_raw = (counted_cash or "").strip()
    bank_raw = (counted_bank or "").strip()
    if not cash_raw:
        return RedirectResponse(
            "/admin/hotel/shift?error="
            + quote("أدخل رصيد الكاش المعدود بعد عدّ الدرج — لا يمكن الإقفال بدون ذلك."),
            status_code=302,
        )
    if not bank_raw:
        return RedirectResponse(
            "/admin/hotel/shift?error="
            + quote(
                "أدخل رصيد المصرف المعدود بعد مراجعة الإيصالات — لا يمكن الإقفال بدون ذلك."
            ),
            status_code=302,
        )
    if len((closing_note or "").strip()) < 2:
        return RedirectResponse(
            "/admin/hotel/shift?error="
            + quote("أدخل بيان الإغلاق — الوصف إلزامي قبل إقفال الجلسة."),
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
            require_counts=True,
            close_destination=close_destination,
            carried_to_employee_id=(
                int(carried_to_employee_id)
                if (carried_to_employee_id or "").strip().isdigit()
                else None
            ),
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


@shifts_router.get("/shift/incoming", response_class=HTMLResponse)
def hotel_shift_incoming_page(
    request: Request, db: DBSession, user: User = Depends(_shift_view)
):
    if requires_hotel_shift_pin(user) and session_hotel_employee_id(request) is None:
        return RedirectResponse("/admin/hotel/pin", status_code=302)
    emp_id = _actor_hotel_employee_id(request, db, user)
    incoming = list_incoming_cash_handovers(db, emp_id) if emp_id else []
    open_s = get_open_shift(db)
    other_open = bool(
        open_s
        and emp_id
        and open_s.employee_id
        and int(open_s.employee_id) != int(emp_id)
    )
    return templates.TemplateResponse(
        "hotel/shift_incoming.html",
        {
            "request": request,
            "incoming_handovers": incoming,
            "incoming_confirm_action": "/admin/hotel/shift/handover/confirm",
            "other_shift_open": other_open,
            "error": request.query_params.get("error"),
            "saved": request.query_params.get("saved"),
        },
    )


@shifts_router.post("/shift/handover")
def hotel_shift_handover_propose(
    request: Request,
    db: DBSession,
    user: User = Depends(_shift_manage),
    claimed_cash: str = Form(""),
    claimed_bank: str = Form(""),
    to_employee_id: str = Form(""),
    note: str = Form(""),
):
    open_s = get_open_shift(db)
    if open_s is None:
        return RedirectResponse("/admin/hotel/shift?error=" + quote("لا جلسة مفتوحة"), status_code=302)
    try:
        to_id = int(to_employee_id) if (to_employee_id or "").strip().isdigit() else 0
        propose_cash_carry(
            db,
            domain="hotel",
            hotel_shift_id=open_s.id,
            from_employee_id=open_s.employee_id,
            to_employee_id=to_id,
            claimed_cash=Decimal(str(claimed_cash or "0")),
            claimed_bank=Decimal(str(claimed_bank or "0")),
            user_id=user.id,
            note=note,
        )
        db.commit()
    except (ShiftHandoverError, InvalidOperation, HotelShiftError) as e:
        db.rollback()
        return RedirectResponse("/admin/hotel/shift?error=" + quote(str(e)), status_code=302)
    return RedirectResponse("/admin/hotel/shift?saved=handover", status_code=302)


@shifts_router.post("/shift/handover/confirm")
def hotel_shift_handover_confirm(
    request: Request,
    db: DBSession,
    user: User = Depends(_shift_manage),
    handover_id: str = Form(""),
    received_cash: str = Form(""),
    received_bank: str = Form(""),
):
    emp_id = _actor_hotel_employee_id(request, db, user)
    try:
        hid = int(handover_id) if (handover_id or "").strip().isdigit() else 0
        confirm_cash_carry(
            db,
            handover_id=hid,
            received_cash=Decimal(str(received_cash or "0")),
            received_bank=Decimal(str(received_bank or "0")),
            user_id=user.id,
            confirmer_employee_id=emp_id,
        )
        db.commit()
    except (ShiftHandoverError, InvalidOperation) as e:
        db.rollback()
        nxt = request.headers.get("referer") or "/admin/hotel/shift/incoming"
        if "/shift/start" in nxt:
            return RedirectResponse("/admin/hotel/shift/start?error=" + quote(str(e)), status_code=302)
        return RedirectResponse("/admin/hotel/shift/incoming?error=" + quote(str(e)), status_code=302)
    nxt = request.headers.get("referer") or ""
    if "/shift/start" in nxt:
        return RedirectResponse("/admin/hotel/shift/start", status_code=302)
    return RedirectResponse("/admin/hotel/shift/incoming?saved=1", status_code=302)


@shifts_router.post("/shift/{shift_id}/bank-transfer")
def hotel_shift_bank_declare(
    shift_id: int,
    request: Request,
    db: DBSession,
    user: User = Depends(_shift_manage),
    bank_amount: str = Form(""),
    bank_name: str = Form(""),
    bank_ref: str = Form(""),
    bank_transferred_at: str = Form(""),
):
    sh = get_shift(db, shift_id) or get_open_shift(db)
    if sh is None:
        return RedirectResponse("/admin/hotel/shift?error=" + quote("الجلسة غير موجودة"), status_code=302)
    try:
        declare_bank_transfer(
            db,
            domain="hotel",
            hotel_shift_id=sh.id,
            from_employee_id=sh.employee_id,
            amount=Decimal(str(bank_amount or "0")),
            bank_name=bank_name,
            bank_ref=bank_ref,
            transferred_at=parse_bank_transferred_at(bank_transferred_at),
            user_id=user.id,
        )
        db.commit()
    except (ShiftHandoverError, InvalidOperation) as e:
        db.rollback()
        dest = f"/admin/hotel/shift/{sh.id}/report" if sh.status and str(sh.status) != "OPEN" else "/admin/hotel/shift"
        return RedirectResponse(f"{dest}?error=" + quote(str(e)), status_code=302)
    from modules.hotel.shift_models import HotelShiftStatus

    if getattr(sh, "status", None) == HotelShiftStatus.CLOSED:
        return RedirectResponse(f"/admin/hotel/shift/{sh.id}/report?saved=1", status_code=302)
    return RedirectResponse("/admin/hotel/shift?saved=bank", status_code=302)


@shifts_router.get("/shift/expense")
def hotel_shift_expense_get(_: User = Depends(_shift_view)):
    return RedirectResponse("/admin/hotel/shift#hotel-shift-expense", status_code=302)


@shifts_router.post("/shift/expense")
def hotel_shift_expense(
    db: DBSession,
    user: User = Depends(_shift_manage),
    amount: str = Form(...),
    payment_method_id: str = Form(...),
    category: str = Form(""),
    note: str = Form(""),
    employee_id: str = Form(""),
):
    open_s = get_open_shift(db)
    if open_s is None:
        return RedirectResponse("/admin/hotel/shift?error=لا+جلسة+مفتوحة", status_code=302)
    if not (payment_method_id or "").strip().isdigit():
        return RedirectResponse("/admin/hotel/shift?error=اختر+وسيلة+الدفع", status_code=302)
    emp_id = int(employee_id) if (employee_id or "").strip().isdigit() else None
    try:
        record_hotel_shift_expense(
            db,
            shift_id=open_s.id,
            amount=Decimal(amount or "0"),
            payment_method_id=int(payment_method_id),
            category=category,
            note=note,
            user_id=user.id,
            employee_id=emp_id,
        )
        db.commit()
    except (HotelShiftError, InvalidOperation) as e:
        db.rollback()
        return RedirectResponse(f"/admin/hotel/shift?error={quote(str(e))}", status_code=302)
    except Exception as e:
        db.rollback()
        return RedirectResponse(
            f"/admin/hotel/shift?error={quote('تعذّر تسجيل المصروف: ' + str(e))}",
            status_code=302,
        )
    return RedirectResponse(
        "/admin/hotel/shift?saved=expense#hotel-shift-expense",
        status_code=302,
    )


@shifts_router.get("/shift/settings", response_class=HTMLResponse)
def hotel_shift_settings_page(request: Request, db: DBSession, _: User = Depends(_shift_settings)):
    from modules.payments.shift_carry import load_hotel_shift_close_policy

    schedules = load_shift_schedules(db)
    if len(schedules) < 3:
        schedules = [dict(x) for x in DEFAULT_SCHEDULES]
    close_policy = load_hotel_shift_close_policy(db)
    return templates.TemplateResponse(
        "hotel/shift_settings.html",
        {
            "request": request,
            "schedules": schedules,
            "overdue_minutes": overdue_grace_minutes(db),
            "supervisor_phone": supervisor_phone(db),
            "overdue_enabled": overdue_alerts_enabled(db),
            "hotel_shift_allow_next_shift_carry": close_policy.allow_carry,
            "hotel_shift_allow_treasury_close": close_policy.allow_treasury,
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
    hotel_shift_allow_next_shift_carry: str = Form(""),
    hotel_shift_allow_treasury_close: str = Form(""),
):
    from modules.payments.shift_carry import save_hotel_shift_close_policy

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
    save_hotel_shift_close_policy(
        db,
        allow_carry=hotel_shift_allow_next_shift_carry == "1",
        allow_treasury=hotel_shift_allow_treasury_close == "1",
    )
    invalidate_settings_cache()
    db.commit()
    return RedirectResponse("/admin/hotel/shift/settings?saved=1", status_code=302)


@shifts_router.get("/shift/{shift_id}/report", response_class=HTMLResponse)
def hotel_shift_report(
    shift_id: int,
    request: Request,
    db: DBSession,
    user: User = Depends(_shift_view),
):
    import json
    from datetime import datetime, timezone
    from decimal import Decimal

    from modules.authz.capability import (
        can_approve_treasury_handoff,
        can_reopen_closed_shift,
        can_revoke_treasury_handoff,
    )
    from modules.hotel.revenue_stats import hotel_collections_detail
    from modules.hotel.shift_handoff import get_next_hotel_shift_pending_handoff
    from modules.hotel.shift_models import HotelShiftStatus

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
    if view not in ("cash", "bank", "expenses"):
        view = ""

    detail_collections = []
    if view == "cash":
        detail_collections = [
            c for c in collections if _pm_kind(c.payment_method) == "cash"
        ]
    elif view == "bank":
        detail_collections = [
            c for c in collections if _pm_kind(c.payment_method) == "bank"
        ]

    cash_net = (
        Decimal(str(collections_summary.cash_in or 0))
        - Decimal(str(collections_summary.cash_out or 0))
    ).quantize(Decimal("0.001"))
    bank_net = (
        Decimal(str(collections_summary.bank_in or 0))
        - Decimal(str(collections_summary.bank_out or 0))
    ).quantize(Decimal("0.001"))

    next_hof = get_next_hotel_shift_pending_handoff(db)
    can_approve_handoff = (
        can_approve_treasury_handoff(user)
        and shift.status == HotelShiftStatus.CLOSED
        and getattr(shift, "treasury_handoff_at", None) is None
        and next_hof is not None
        and next_hof.id == shift.id
    )
    can_revoke_handoff = (
        can_revoke_treasury_handoff(user)
        and getattr(shift, "treasury_handoff_at", None) is not None
    )

    reception_cash_balance = Decimal("0")
    reception_bank_balance = Decimal("0")
    try:
        from modules.payments.service import (
            ensure_hotel_reception_payment_methods,
            method_current_balance,
        )

        recv_w = ensure_hotel_reception_payment_methods(db)
        reception_cash_balance = method_current_balance(db, recv_w["CASH"].id)
        reception_bank_balance = method_current_balance(db, recv_w["BANK"].id)
    except Exception:  # noqa: BLE001
        pass

    hof_cash_def, hof_bank_def = _hotel_handoff_defaults(db, shift)

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
            "error": request.query_params.get("error"),
            "handoff": request.query_params.get("handoff"),
            "detail_view": view or None,
            "detail_collections": detail_collections,
            "card_cash_total": cash_net,
            "card_bank_total": bank_net,
            "can_approve_handoff": can_approve_handoff,
            "can_revoke_handoff": can_revoke_handoff,
            "can_reopen_shift": can_reopen_closed_shift(user)
            and shift.status == HotelShiftStatus.CLOSED,
            "next_handoff_shift_id": next_hof.id if next_hof else None,
            "reception_cash_balance": reception_cash_balance,
            "reception_bank_balance": reception_bank_balance,
            "bank_declaration": get_bank_declaration(db, hotel_shift_id=shift.id),
            "bank_declare_action": f"/admin/hotel/shift/{shift.id}/bank-transfer",
            "bank_default": shift.counted_bank or shift.expected_bank or 0,
            "handoff_cash_default": hof_cash_def,
            "handoff_bank_default": hof_bank_def,
        },
    )


@shifts_router.post("/shift/{shift_id}/handoff", response_class=HTMLResponse)
def hotel_shift_handoff_approve(
    shift_id: int,
    db: DBSession,
    user: User = Depends(
        require_any_permission(TREASURY_HANDOFF_APPROVE, PAYMENTS_MANAGE)
    ),
    handoff_cash: str = Form(""),
    handoff_bank: str = Form(""),
    handoff_note: str = Form(""),
):
    from modules.hotel.shift_handoff import (
        HotelShiftHandoffError,
        approve_hotel_shift_handoff,
        parse_handoff_amount,
    )

    try:
        approve_hotel_shift_handoff(
            db,
            shift_id=shift_id,
            user_id=user.id,
            admin_username=user.username or "",
            handoff_cash=parse_handoff_amount(handoff_cash),
            handoff_bank=parse_handoff_amount(handoff_bank),
            handoff_note=handoff_note,
        )
        db.commit()
    except HotelShiftHandoffError as e:
        db.rollback()
        return RedirectResponse(
            f"/admin/hotel/shift/{shift_id}/report?error={quote(str(e))}",
            status_code=302,
        )
    return RedirectResponse(
        f"/admin/hotel/shift/{shift_id}/report?saved=1&handoff=1",
        status_code=302,
    )


@shifts_router.post("/shift/{shift_id}/handoff-revoke", response_class=HTMLResponse)
def hotel_shift_handoff_revoke(
    shift_id: int,
    db: DBSession,
    user: User = Depends(require_permission(TREASURY_HANDOFF_REVOKE)),
    revoke_reason: str = Form(""),
):
    from modules.hotel.shift_handoff import HotelShiftHandoffError, revoke_hotel_shift_handoff

    try:
        revoke_hotel_shift_handoff(
            db,
            shift_id=shift_id,
            user_id=user.id,
            reason=revoke_reason,
        )
        db.commit()
    except HotelShiftHandoffError as e:
        db.rollback()
        return RedirectResponse(
            f"/admin/hotel/shift/{shift_id}/report?error={quote(str(e))}",
            status_code=302,
        )
    return RedirectResponse(
        f"/admin/hotel/shift/{shift_id}/report?saved=1&handoff=revoked",
        status_code=302,
    )


@shifts_router.post("/shift/{shift_id}/reopen-draft", response_class=HTMLResponse)
def hotel_shift_reopen_draft(
    request: Request,
    shift_id: int,
    db: DBSession,
    user: User = Depends(require_permission(POS_SHIFT_REOPEN)),
    reason: str = Form(""),
):
    from modules.hotel.shift_service import reopen_hotel_shift_to_draft

    try:
        reopen_hotel_shift_to_draft(
            db,
            shift_id=shift_id,
            user_id=user.id,
            admin_username=user.username or "",
            reason=reason,
        )
        request.session["hotel_shift_id"] = shift_id
        db.commit()
    except HotelShiftError as e:
        db.rollback()
        return RedirectResponse(
            f"/admin/hotel/shift/{shift_id}/report?error={quote(str(e))}",
            status_code=302,
        )
    return RedirectResponse(
        "/admin/hotel/shift?saved=reopened",
        status_code=302,
    )
