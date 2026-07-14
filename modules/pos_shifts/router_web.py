from __future__ import annotations

import json
from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from app.deps import DBSession, require_any_permission, require_permission
from app.datetime_local import format_local_dt
from app.jinja_env import templates
from modules.authz.models import User
from modules.authz.permissions import HR_MANAGE, PAYMENTS_MANAGE, PURCHASES_MANAGE, REPORTS_VIEW, SALES_CREATE
from modules.sales.models import SaleStatus
from modules.payments.models import PaymentMethodKind
from modules.payments.service import (
    PaymentsError,
    list_payment_methods_transfer_sources,
    list_payment_methods_transfer_targets,
    payment_method_balances_map,
    record_manual_transfer,
)
from modules.payments.shift_handoff_service import issue_opening_float_transfer
from modules.payments.treasury_service import (
    daily_balance_rows,
    list_ledger_entries,
    treasury_summaries,
    treasury_summary_for_kind,
    treasury_summary_for_method,
)
from modules.authz.kiosk import is_cashier_kiosk_user, requires_pos_pin
from modules.authz.service import user_has_permission
from modules.hr.service import get_employee
from modules.pos_shifts.models import PosShift, PosShiftStatus
from modules.pos_shifts.service import (
    PosShiftError,
    build_shift_sale_rows,
    clear_pos_operator_session,
    close_shift,
    compute_expected_cash,
    compute_expected_bank,
    compute_shift_financial_summary,
    get_shift,
    get_open_shift_for_user,
    list_completed_sales_for_shift,
    open_shift,
    pin_authenticate_for_shift,
    session_pos_employee_id,
    sync_session_pos_shift,
)
from modules.pos_shifts.shortages import (
    backfill_shortages_from_closed_shifts,
    list_shift_shortages,
    shortage_kind_is_cash,
    shortage_kind_label_ar,
    shortages_summary,
)
from modules.reporting.exports import csv_response
from modules.settings.service import get_setting

router = APIRouter(prefix="/pos", tags=["pos-shift"])
_pos_perm = require_permission(SALES_CREATE)
_treasury_manage = require_permission(PAYMENTS_MANAGE)
_treasury_view = require_any_permission(PAYMENTS_MANAGE, REPORTS_VIEW)
_shortage_page_perm = require_any_permission(
    PAYMENTS_MANAGE, REPORTS_VIEW, HR_MANAGE
)
_shortage_deduct_perm = require_any_permission(HR_MANAGE, PAYMENTS_MANAGE)
_shortage_forgive_perm = require_any_permission(PURCHASES_MANAGE, PAYMENTS_MANAGE)


def _can_view_shift_shortages(user: User) -> bool:
    return user_has_permission(user, PAYMENTS_MANAGE) or user_has_permission(
        user, REPORTS_VIEW
    ) or user_has_permission(user, HR_MANAGE)


def _ensure_shortage_db_schema(db: DBSession) -> None:
    """ترقية SQLite إن لزم (جداول خصم الراتب وأعمدة معالجة العجز)."""
    from infra.sqlite_patch import patch_sqlite_schema

    patch_sqlite_schema(db.get_bind())


def _parse_money(raw: str) -> Decimal:
    s = (raw or "").strip().replace(",", ".")
    if not s:
        raise ValueError("empty")
    return Decimal(s).quantize(Decimal("0.001"))


def _owns_shift(user: User, sh: PosShift) -> bool:
    return sh.user_id == user.id


def _can_view_shift_detail(user: User, sh: PosShift) -> bool:
    """صاحب الجلسة أو مدير مالي/موارد بشرية/تقارير."""
    if _owns_shift(user, sh):
        return True
    return user_has_permission(user, PAYMENTS_MANAGE) or user_has_permission(
        user, REPORTS_VIEW
    ) or user_has_permission(user, HR_MANAGE)


def _render_shift_page(
    request: Request,
    db: DBSession,
    user: User,
    *,
    error: str | None,
    need_open: bool,
    closed_shift_id: int | None,
    err_param: str | None,
    status_code: int = 200,
):
    from infra.sqlite_patch import patch_sqlite_schema

    patch_sqlite_schema(db.get_bind())
    sync_session_pos_shift(request=request, db=db, user_id=user.id)
    open_s = get_open_shift_for_user(db, user.id)
    op_name = None
    if open_s is not None and open_s.employee is not None:
        op_name = open_s.employee.full_name_ar
    recent = list(
        db.scalars(
            select(PosShift)
            .where(
                PosShift.user_id == user.id,
                PosShift.status == PosShiftStatus.CLOSED,
            )
            .order_by(PosShift.id.desc())
            .limit(12)
        ).all()
    )
    store_name = get_setting(db, "store_name", "نقطة البيع")
    msg = err_param or error
    preview_expected_cash = None
    preview_expected_bank = None
    shift_financial = None
    if open_s is not None:
        shift_financial = compute_shift_financial_summary(db, open_s.id)
        preview_expected_cash = shift_financial.expected_cash_drawer
        preview_expected_bank = shift_financial.expected_bank
    try:
        sh_sum = shortages_summary(db) if _can_view_shift_shortages(user) else None
    except Exception:  # noqa: BLE001
        from modules.pos_shifts.shortages import ShortagesSummary

        sh_sum = (
            ShortagesSummary(
                total_shortage=Decimal("0"),
                event_count=0,
                cash_total=Decimal("0"),
                bank_total=Decimal("0"),
                cash_count=0,
                bank_count=0,
            )
            if _can_view_shift_shortages(user)
            else None
        )
    closed_shift = get_shift(db, closed_shift_id) if closed_shift_id else None
    pending_shift_orders: list[dict] = []
    if open_s is not None:
        from modules.sales.service import list_pending_orders_blocking_shift_close

        pending_shift_orders = list_pending_orders_blocking_shift_close(
            db, user_id=user.id, pos_shift_id=open_s.id
        )
    try:
        treasuries = treasury_summaries(db)
    except Exception:  # noqa: BLE001
        treasuries = {}
    close_rows: list = []
    if open_s is not None and shift_financial is not None:
        from modules.pos_shifts.close_wallets import build_shift_close_rows

        close_rows = build_shift_close_rows(db, open_s.id)

    from modules.pos_shifts.shift_expenses import shift_expense_ui_context

    expense_ctx = shift_expense_ui_context(db, open_s.id if open_s else None)

    open_shift_stale = False
    if open_s is not None:
        from modules.pos_shifts.service import shift_open_age_hours

        open_shift_stale = shift_open_age_hours(open_s) >= 24.0

    return templates.TemplateResponse(
        "pos_shift.html",
        {
            "request": request,
            "user": user,
            "open_shift": open_s,
            "open_pos_shift": open_s,
            "pos_operator_name": op_name,
            "recent_closed": recent,
            "need_open": need_open,
            "closed_shift_id": closed_shift_id,
            "error": msg,
            "store_name": store_name,
            "preview_expected_cash": preview_expected_cash,
            "preview_expected_bank": preview_expected_bank,
            "shift_financial": shift_financial,
            "shortages_summary": sh_sum,
            "closed_shift": closed_shift,
            "pending_shift_orders": pending_shift_orders,
            "treasuries": treasuries,
            "close_rows": close_rows,
            "open_shift_stale": open_shift_stale,
            **expense_ctx,
        },
        status_code=status_code,
    )


@router.get("/shift/shortages", response_class=HTMLResponse)
def pos_shift_shortages_page(
    request: Request,
    db: DBSession,
    user: User = Depends(_shortage_page_perm),
    kind: str = Query("all"),
):
    import logging

    try:
        _ensure_shortage_db_schema(db)
        backfill_shortages_from_closed_shifts(db)
        from modules.pos_shifts.loyalty_settlement import reconcile_loyalty_false_shortages

        reconcile_loyalty_false_shortages(db)
        db.commit()
        kind_f = (kind or "all").lower()
        if kind_f not in ("all", "cash", "bank"):
            kind_f = "all"
        kfilter = None if kind_f == "all" else kind_f.upper()
        summary = shortages_summary(db)
        rows = list_shift_shortages(db, kind_filter=kfilter)
        from modules.payments.service import list_wallet_methods_for_shortage_forgive

        cash_pms = list_wallet_methods_for_shortage_forgive(db, kind=PaymentMethodKind.CASH)
        bank_pms = list_wallet_methods_for_shortage_forgive(db, kind=PaymentMethodKind.BANK)
        wallets_for_forgive = {
            "cash": [{"id": m.id, "name": m.name_ar} for m in cash_pms],
            "bank": [{"id": m.id, "name": m.name_ar} for m in bank_pms],
        }
        from modules.hr.service import list_employees

        employees = list_employees(db, only_active=True)
        if not employees:
            employees = list_employees(db, only_active=False)
        return templates.TemplateResponse(
            "pos_shift_shortages.html",
            {
                "request": request,
                "summary": summary,
                "rows": rows,
                "kind_filter": kind_f,
                "cash_payment_methods": cash_pms,
                "bank_payment_methods": bank_pms,
                "employees": employees,
                "wallets_for_forgive_json": json.dumps(
                    wallets_for_forgive, ensure_ascii=False
                ),
                "can_deduct_shortage": user_has_permission(user, HR_MANAGE)
                or user_has_permission(user, PAYMENTS_MANAGE),
                "can_forgive_shortage": user_has_permission(user, PURCHASES_MANAGE)
                or user_has_permission(user, PAYMENTS_MANAGE),
            },
        )
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logging.getLogger("pos_shifts").exception("shortages page failed")
        msg = str(exc)
        if "no such column" in msg.lower() or "no such table" in msg.lower():
            msg = (
                "قاعدة البيانات تحتاج تحديثاً. أوقف الخادم ثم شغّله من جديد "
                "(restart-server.bat) وحاول مرة أخرى."
            )
        return RedirectResponse(
            "/pos/shift?err=" + quote("تعذر فتح سجل العجز: " + msg),
            status_code=302,
        )


def _apply_shortage_payroll_deduction(
    db: DBSession,
    *,
    shortage_id: int,
    user: User,
    note: str,
    employee_id_override: int | None = None,
) -> RedirectResponse:
    import logging
    from datetime import datetime, timezone

    from modules.hr.service import HRError, create_employee_deduction
    from modules.pos_shifts.models import PosShiftShortage

    _ensure_shortage_db_schema(db)

    s = db.get(PosShiftShortage, shortage_id)
    if s is None or (s.shortage_amount or Decimal("0")) <= 0:
        return RedirectResponse(
            "/pos/shift/shortages?kind=all&err="
            + quote("هذا العجز غير موجود أو تمت معالجته مسبقاً."),
            status_code=302,
        )
    if not s.employee_id and not employee_id_override:
        return RedirectResponse(
            "/pos/shift/shortages?kind=all&err="
            + quote(
                "اختر الموظف الذي يُخصم من راتبه، أو اربط الكاشier بموظف في الموارد البشرية."
            ),
            status_code=302,
        )
    emp_id = int(s.employee_id or employee_id_override)
    if s.payroll_deduction_id:
        return RedirectResponse(
            "/pos/shift/shortages?kind=all&err="
            + quote("تم تسجيل خصم راتب لهذا العجز مسبقاً."),
            status_code=302,
        )
    amt = Decimal(str(s.shortage_amount)).quantize(Decimal("0.001"))
    try:
        ded = create_employee_deduction(
            db,
            employee_id=emp_id,
            amount=amt,
            note=(note or f"عجز {shortage_kind_label_ar(s.kind)} جلسة #{s.shift_id}").strip(),
            source_type="POS_SHIFT_SHORTAGE",
            source_id=s.id,
            resolved_by_id=user.id,
        )
    except HRError as exc:
        return RedirectResponse(
            "/pos/shift/shortages?kind=all&err=" + quote(str(exc)),
            status_code=302,
        )
    if s.employee_id is None:
        s.employee_id = emp_id
    if s.original_shortage_amount is None:
        s.original_shortage_amount = amt
    s.resolved_action = "DEDUCT"
    s.resolved_note = (note or "").strip() or None
    s.resolved_at = datetime.now(timezone.utc)
    s.resolved_by_id = user.id
    s.payroll_deduction_id = ded.id
    s.shortage_amount = Decimal("0.000")
    try:
        db.flush()
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logging.getLogger("pos_shifts").exception("shortage payroll deduct failed")
        msg = str(exc)
        if "hr_employee_deductions" in msg or "no such table" in msg.lower():
            msg = (
                "قاعدة البيانات تحتاج تحديثاً. أوقف الخادم ثم شغّله من جديد "
                "(restart-server.bat) وحاول مرة أخرى."
            )
        return RedirectResponse(
            "/pos/shift/shortages?kind=all&err=" + quote(msg),
            status_code=302,
        )
    return RedirectResponse(
        "/pos/shift/shortages?kind=all&ok=deduct&amt=" + quote(str(amt)),
        status_code=302,
    )


@router.get("/shift/shortages/{shortage_id}/deduct", response_class=HTMLResponse)
def pos_shift_shortage_deduct_get(
    shortage_id: int,
    _: User = Depends(_shortage_deduct_perm),
):
    """تفادي 404 عند فتح الرابط مباشرة — الإجراء يتم عبر POST من الجدول."""
    return RedirectResponse(
        "/pos/shift/shortages?kind=all&err="
        + quote("استخدم زر «خصم من الراتب» في جدول العجز (وليس فتح الرابط في المتصفح)."),
        status_code=302,
    )


@router.post("/shift/shortages/{shortage_id}/deduct", response_class=HTMLResponse)
def pos_shift_shortage_deduct(
    request: Request,
    shortage_id: int,
    db: DBSession,
    user: User = Depends(_shortage_deduct_perm),
    note: str = Form(""),
    employee_id: str = Form(""),
):
    eid: int | None = None
    raw = (employee_id or "").strip()
    if raw.isdigit() and int(raw) > 0:
        eid = int(raw)
    return _apply_shortage_payroll_deduction(
        db, shortage_id=shortage_id, user=user, note=note, employee_id_override=eid
    )


@router.post("/shift/shortages/{shortage_id}/forgive", response_class=HTMLResponse)
def pos_shift_shortage_forgive(
    request: Request,
    shortage_id: int,
    db: DBSession,
    user: User = Depends(_shortage_forgive_perm),
    payment_method_id: str = Form(""),
    note: str = Form(""),
):
    from datetime import datetime, timezone
    from modules.payments.models import PaymentMethod
    from modules.payments.service import PaymentsError, record_expense
    from modules.pos_shifts.models import PosShiftShortage

    _ensure_shortage_db_schema(db)

    s = db.get(PosShiftShortage, shortage_id)
    if s is None or (s.shortage_amount or Decimal("0")) <= 0:
        return RedirectResponse("/pos/shift/shortages?kind=all", status_code=302)
    amt = Decimal(str(s.shortage_amount)).quantize(Decimal("0.001"))

    try:
        pm_id = int((payment_method_id or "").strip())
    except (TypeError, ValueError):
        pm_id = 0
    pm = db.get(PaymentMethod, pm_id) if pm_id else None
    if pm is None:
        return RedirectResponse(
            "/pos/shift/shortages?kind=all&err=" + quote("اختر محفظة خصم (كاش/مصرف) لتسجيل العفو كمصروف."),
            status_code=302,
        )
    # ضمان نوع المحفظة مطابق لنوع العجز
    from modules.payments.service import payment_method_kind_matches

    need_kind = PaymentMethodKind.CASH if shortage_kind_is_cash(s.kind) else PaymentMethodKind.BANK
    if not payment_method_kind_matches(pm, need_kind):
        return RedirectResponse(
            "/pos/shift/shortages?kind=all&err="
            + quote("المحفظة المختارة لا تطابق نوع العجز (كاش/مصرف)."),
            status_code=302,
        )
    emp = s.employee.full_name_ar if getattr(s, "employee", None) else None
    base_note = f"عفو عن عجز {shortage_kind_label_ar(s.kind)} جلسة #{s.shift_id}"
    if emp:
        base_note += f" — الموظف: {emp}"
    if note:
        base_note += f" — {note.strip()}"
    try:
        exp = record_expense(
            db,
            payment_method_id=pm.id,
            amount=amt,
            expense_category="عجز صندوق",
            supplier=emp,
            note=base_note[:240],
            user_id=user.id,
            created_at=datetime.now(timezone.utc),
        )
    except PaymentsError as e:
        db.rollback()
        return RedirectResponse(
            "/pos/shift/shortages?kind=all&err=" + quote(str(e)), status_code=302
        )
    if s.original_shortage_amount is None:
        s.original_shortage_amount = amt
    s.resolved_action = "FORGIVE"
    s.resolved_note = (note or "").strip() or None
    s.resolved_at = datetime.now(timezone.utc)
    s.resolved_by_id = user.id
    s.expense_purchase_id = exp.id
    s.shortage_amount = Decimal("0.000")
    db.flush()
    db.commit()
    return RedirectResponse("/pos/shift/shortages?kind=all", status_code=302)


@router.get("/pin", response_class=HTMLResponse)
def pos_pin_page(
    request: Request,
    db: DBSession,
    user: User = Depends(_pos_perm),
    err: str | None = Query(None),
):
    open_s = get_open_shift_for_user(db, user.id)
    emp_id = session_pos_employee_id(request)
    if open_s is not None and emp_id is not None:
        return RedirectResponse("/pos", status_code=302)
    if open_s is None and emp_id is not None:
        return RedirectResponse("/pos/shift/start", status_code=302)
    emp = get_employee(db, emp_id) if emp_id else None
    store_name = get_setting(db, "store_name", "نقطة البيع")
    return templates.TemplateResponse(
        "pos_pin.html",
        {
            "request": request,
            "user": user,
            "error": err,
            "store_name": store_name,
            "operator_name": emp.full_name_ar if emp else None,
        },
    )


@router.post("/pin/verify", response_class=HTMLResponse)
def pos_pin_verify(
    request: Request,
    db: DBSession,
    user: User = Depends(_pos_perm),
    pin: str = Form(...),
):
    try:
        existing, need_start = pin_authenticate_for_shift(db, request, user, pin)
        db.commit()
    except PosShiftError as e:
        db.rollback()
        return RedirectResponse("/pos/pin?err=" + quote(str(e)), status_code=302)
    if need_start:
        return RedirectResponse("/pos/shift/start", status_code=302)
    return RedirectResponse("/pos", status_code=302)


@router.post("/pin/logout", response_class=HTMLResponse)
def pos_pin_logout(
    request: Request,
    user: User = Depends(_pos_perm),
):
    """إنهاء جلسة الكاشير (بعد إغلاق الوردية) — يبقى تسجيل الدخول للنظام."""
    clear_pos_operator_session(request)
    if is_cashier_kiosk_user(user):
        return RedirectResponse("/pos/pin", status_code=302)
    return RedirectResponse("/pos/shift", status_code=302)


@router.get("/shift", response_class=HTMLResponse)
def pos_shift_page(
    request: Request,
    db: DBSession,
    user: User = Depends(_pos_perm),
    need: str | None = Query(None),
    closed: str | None = Query(None),
    err: str | None = Query(None),
):
    if is_cashier_kiosk_user(user) and get_open_shift_for_user(db, user.id) is None:
        return RedirectResponse("/pos/pin", status_code=302)
    if need and get_open_shift_for_user(db, user.id) is None:
        return RedirectResponse("/pos/shift/start", status_code=302)
    closed_id: int | None = None
    if closed:
        try:
            closed_id = int(closed)
        except ValueError:
            closed_id = None
    return _render_shift_page(
        request,
        db,
        user,
        error=None,
        need_open=bool(need),
        closed_shift_id=closed_id,
        err_param=err,
        status_code=200,
    )


@router.get("/shift/start", response_class=HTMLResponse)
def pos_shift_start_page(
    request: Request,
    db: DBSession,
    user: User = Depends(_pos_perm),
    err: str | None = Query(None),
):
    if get_open_shift_for_user(db, user.id) is not None:
        return RedirectResponse("/pos", status_code=302)
    if requires_pos_pin(user) and session_pos_employee_id(request) is None:
        return RedirectResponse("/pos/pin", status_code=302)
    op_name = None
    emp_id = session_pos_employee_id(request)
    if emp_id:
        emp = get_employee(db, emp_id)
        if emp is not None:
            op_name = emp.full_name_ar
    from modules.inventory.service import list_sales_deduction_warehouses

    branches = list_sales_deduction_warehouses(db)
    default_wh = getattr(user, "warehouse_id", None)
    return templates.TemplateResponse(
        "pos_shift_start.html",
        {
            "request": request,
            "operator_name": op_name,
            "error": err,
            "opening_cash_value": "",
            "opening_note_value": "",
            "no_float_checked": False,
            "branch_warehouses": branches,
            "default_warehouse_id": default_wh,
        },
    )


def _submit_shift_start(
    request: Request,
    db: DBSession,
    user: User,
    *,
    opening_cash: str,
    opening_note: str,
    no_opening_float: str,
    warehouse_id: str = "",
) -> RedirectResponse | HTMLResponse:
    if get_open_shift_for_user(db, user.id) is not None:
        return RedirectResponse("/pos", status_code=302)
    if requires_pos_pin(user) and session_pos_employee_id(request) is None:
        return RedirectResponse("/pos/pin", status_code=302)

    no_float = (no_opening_float or "").strip().lower() in ("1", "on", "true", "yes")
    op_name = None
    emp_id = session_pos_employee_id(request)
    if emp_id:
        emp = get_employee(db, emp_id)
        if emp is not None:
            op_name = emp.full_name_ar
            emp_id = emp.id
    else:
        from modules.hr.service import get_employee_by_user_id

        linked = get_employee_by_user_id(db, user.id)
        emp_id = linked.id if linked is not None else None

    def _render_err(msg: str) -> HTMLResponse:
        from modules.inventory.service import list_sales_deduction_warehouses

        return templates.TemplateResponse(
            "pos_shift_start.html",
            {
                "request": request,
                "operator_name": op_name,
                "error": msg,
                "opening_cash_value": opening_cash,
                "opening_note_value": opening_note,
                "no_float_checked": no_float,
                "branch_warehouses": list_sales_deduction_warehouses(db),
                "default_warehouse_id": warehouse_id.strip() or getattr(user, "warehouse_id", None),
            },
            status_code=400,
        )

    if no_float:
        oc = Decimal("0")
    else:
        raw = (opening_cash or "").strip()
        if not raw:
            return _render_err("أدخل رصيد الافتتاح في الدرج، أو فعّل «لا يوجد فكة».")
        try:
            oc = _parse_money(raw)
        except (InvalidOperation, ValueError):
            return _render_err("رصيد الافتتاح غير صالح.")
        if oc < 0:
            return _render_err("رصيد الافتتاح لا يمكن أن يكون سالباً.")

    wh_id: int | None = None
    if warehouse_id.strip().isdigit():
        wh_id = int(warehouse_id.strip())

    try:
        sh = open_shift(
            db,
            user.id,
            employee_id=emp_id,
            opening_note=opening_note,
            opening_cash=oc,
            warehouse_id=wh_id,
        )
        if oc > 0:
            issue_opening_float_transfer(
                db, shift_id=sh.id, amount=oc, user_id=user.id
            )
        db.commit()
        request.session["pos_shift_id"] = sh.id
    except (PosShiftError, PaymentsError) as e:
        db.rollback()
        return _render_err(str(e))

    return RedirectResponse("/pos", status_code=302)


@router.post("/shift/start", response_class=HTMLResponse)
def pos_shift_start_submit(
    request: Request,
    db: DBSession,
    user: User = Depends(_pos_perm),
    opening_cash: str = Form(""),
    opening_note: str = Form(""),
    no_opening_float: str = Form(""),
    warehouse_id: str = Form(""),
):
    return _submit_shift_start(
        request,
        db,
        user,
        opening_cash=opening_cash,
        opening_note=opening_note,
        no_opening_float=no_opening_float,
        warehouse_id=warehouse_id,
    )


@router.post("/shift/open", response_class=HTMLResponse)
def pos_shift_open_post(
    _: User = Depends(_pos_perm),
):
    return RedirectResponse("/pos/shift/start", status_code=302)


@router.get("/shift/open", response_class=HTMLResponse)
def pos_shift_open_get(
    _: User = Depends(_pos_perm),
):
    return RedirectResponse("/pos/shift/start", status_code=302)


@router.post("/shift/expense", response_class=HTMLResponse)
def pos_shift_expense_record(
    request: Request,
    db: DBSession,
    user: User = Depends(_pos_perm),
    amount: str = Form(...),
    category: str = Form(...),
    note: str = Form(...),
    payment_method_id: str = Form(""),
    next_url: str = Form("/pos/shift"),
):
    from infra.sqlite_patch import patch_sqlite_schema

    patch_sqlite_schema(db.get_bind())
    from modules.pos_shifts.expense_categories import active_categories_for_pos
    from modules.pos_shifts.shift_expenses import record_shift_expense

    open_s = get_open_shift_for_user(db, user.id)
    if open_s is None:
        return RedirectResponse(
            "/pos/shift?err=" + quote("افتح جلسة بيع أولاً."),
            status_code=302,
        )
    allowed = {k for k, _ in active_categories_for_pos(db)}
    if (category or "").strip() not in allowed:
        return RedirectResponse(
            _shift_expense_redirect(next_url, err="اختر تصنيفاً صالحاً."),
            status_code=302,
        )
    try:
        amt = _parse_money(amount)
        if amt <= 0:
            raise ValueError("amount")
    except (ValueError, InvalidOperation):
        return RedirectResponse(
            _shift_expense_redirect(next_url, err="أدخل مبلغاً صحيحاً أكبر من صفر."),
            status_code=302,
        )
    pm_id: int | None = None
    raw_pm = (payment_method_id or "").strip()
    if raw_pm.isdigit() and int(raw_pm) > 0:
        pm_id = int(raw_pm)
    try:
        record_shift_expense(
            db,
            shift_id=open_s.id,
            user_id=user.id,
            amount=amt,
            category_key=category,
            note=note,
            payment_method_id=pm_id,
        )
        db.commit()
    except PosShiftError as exc:
        db.rollback()
        return RedirectResponse(
            _shift_expense_redirect(next_url, err=str(exc)),
            status_code=302,
        )
    return RedirectResponse(_shift_expense_redirect(next_url, ok="1"), status_code=302)


def _shift_expense_redirect(next_url: str, *, err: str | None = None, ok: str | None = None) -> str:
    base = (next_url or "/pos/shift").strip() or "/pos/shift"
    if not base.startswith("/"):
        base = "/pos/shift"
    sep = "&" if "?" in base else "?"
    if err:
        return base + sep + "expense_err=" + quote(err)
    if ok:
        return base + sep + "expense_ok=1"
    return base


@router.post("/shift/close", response_class=HTMLResponse)
def pos_shift_close(
    request: Request,
    db: DBSession,
    user: User = Depends(_pos_perm),
    counted_cash: str = Form(...),
    counted_bank: str = Form(""),
    counted_room: str = Form(""),
    closing_note: str = Form(""),
):
    sid = request.session.get("pos_shift_id")
    try:
        shift_id = int(sid) if sid is not None else 0
    except (TypeError, ValueError):
        shift_id = 0
    try:
        cc = _parse_money(counted_cash)
    except (InvalidOperation, ValueError):
        return RedirectResponse(
            "/pos/shift?err=" + quote("المبلغ المعدود (كاش) غير صالح."),
            status_code=302,
        )
    cb: Decimal | None = None
    bank_raw = (counted_bank or "").strip()
    if not bank_raw:
        return RedirectResponse(
            "/pos/shift?err="
            + quote("أدخل المبلغ المعدود لخزينة المصرف بعد مراجعة فواتير المصرف."),
            status_code=302,
        )
    try:
        cb = _parse_money(bank_raw)
    except (InvalidOperation, ValueError):
        return RedirectResponse(
            "/pos/shift?err=" + quote("رصيد المصرف المعدود غير صالح."),
            status_code=302,
        )
    room_raw = (counted_room or "").strip()
    if not room_raw:
        return RedirectResponse(
            "/pos/shift?err="
            + quote(
                "أدخل إجمالي فواتير الشقق (قيد على حساب الشقة) بعد مراجعتها."
            ),
            status_code=302,
        )
    try:
        cr = _parse_money(room_raw)
    except (InvalidOperation, ValueError):
        return RedirectResponse(
            "/pos/shift?err=" + quote("إجمالي فواتير الشقق غير صالح."),
            status_code=302,
        )
    try:
        close_shift(
            db,
            shift_id=shift_id,
            user_id=user.id,
            counted_cash=cc,
            counted_bank=cb,
            counted_room=cr,
            closing_note=closing_note,
        )
        db.commit()
    except PosShiftError as e:
        db.rollback()
        return RedirectResponse("/pos/shift?err=" + quote(str(e)), status_code=302)
    clear_pos_operator_session(request)
    if is_cashier_kiosk_user(user):
        return RedirectResponse(f"/pos/pin?closed={shift_id}", status_code=302)
    return RedirectResponse(f"/pos/shift?closed={shift_id}", status_code=302)


@router.get("/shift/{shift_id}/export.csv")
def pos_shift_export_csv(
    shift_id: int,
    db: DBSession,
    user: User = Depends(_pos_perm),
):
    sh = get_shift(db, shift_id)
    if sh is None or not _can_view_shift_detail(user, sh):
        return RedirectResponse(
            "/pos/shift?err="
            + quote("لا يمكنك فتح تقرير جلسة مستخدم آخر."),
            status_code=302,
        )
    rows = build_shift_sale_rows(db, shift_id, user=user)
    summary_rows: list[tuple] = [
        ("نوع السطر", "1", "2", "3", "4"),
        ("ملخص", f"جلسة #{shift_id}", "", "", ""),
        ("الحالة", sh.status.value, "", "", ""),
        ("فتح", format_local_dt(sh.opened_at, "%Y-%m-%d %H:%M") if sh.opened_at else "", "", "", ""),
        (
            "إغلاق",
            format_local_dt(sh.closed_at, "%Y-%m-%d %H:%M") if sh.closed_at else "",
            "",
            "",
            "",
        ),
        (
            "رصيد افتتاحي (فكة)",
            str(sh.opening_cash or 0),
            "",
            "",
            "",
        ),
        (
            "كاش متوقع",
            str(sh.expected_cash) if sh.expected_cash is not None else "",
            "",
            "",
            "",
        ),
        (
            "كاش معدود",
            str(sh.counted_cash) if sh.counted_cash is not None else "",
            "",
            "",
            "",
        ),
        (
            "الفرق (معدود − متوقع)",
            str(sh.cash_difference) if sh.cash_difference is not None else "",
            "",
            "",
            "",
        ),
        (
            "مصرف متوقع",
            str(sh.expected_bank) if sh.expected_bank is not None else "",
            "",
            "",
            "",
        ),
        (
            "مصرف معدود",
            str(sh.counted_bank) if sh.counted_bank is not None else "",
            "",
            "",
            "",
        ),
        (
            "فرق المصرف",
            str(sh.bank_difference) if sh.bank_difference is not None else "",
            "",
            "",
            "",
        ),
    ]
    from modules.pos_shifts.shortages import shift_reconciliation_totals

    short_total, sur_total = shift_reconciliation_totals(
        sh.cash_difference, sh.bank_difference
    )
    summary_rows.extend(
        [
            ("إجمالي العجز", str(short_total), "", "", ""),
            ("إجمالي الفائض", str(sur_total), "", "", ""),
            ("", "", "", "", ""),
            ("فاتورة", "نوع الفاتورة", "وسيلة الدفع", "الإجمالي", "الوقت والتاريخ"),
        ]
    )
    detail = [
        (r.sale_id, r.context_label, r.payment_label, r.total, format_local_dt(r.created_at, "%Y-%m-%d %H:%M") if r.created_at else "")
        for r in rows
    ]
    return csv_response(
        f"pos_shift_{shift_id}",
        ["عمود1", "عمود2", "عمود3", "عمود4", "عمود5"],
        summary_rows + detail,
    )


@router.get("/shift/{shift_id}/report", response_class=HTMLResponse)
def pos_shift_report_print(
    request: Request,
    shift_id: int,
    db: DBSession,
    user: User = Depends(_pos_perm),
):
    sh = get_shift(db, shift_id)
    if sh is None or not _can_view_shift_detail(user, sh):
        return RedirectResponse(
            "/pos/shift?err="
            + quote("لا يمكنك فتح تقرير جلسة مستخدم آخر."),
            status_code=302,
        )
    rows = build_shift_sale_rows(db, shift_id, user=user)
    sales = list_completed_sales_for_shift(db, shift_id)
    total = sum(
        (Decimal(str(s.total or 0)) for s in sales),
        Decimal("0"),
    ).quantize(Decimal("0.001"))
    store_name = get_setting(db, "store_name", "نقطة البيع")
    from modules.pos_shifts.shift_reports import compute_shift_total_costs

    shift_financial = compute_shift_financial_summary(db, shift_id)
    total_costs = compute_shift_total_costs(db, shift_id)
    from modules.payments.service import (
        is_treasury_wallet_method,
        list_payment_methods_for_pay,
    )
    from modules.sales.invoice_actions import (
        user_can_correct_payment,
        user_can_print_receipt,
        user_can_refund_sale,
    )
    from modules.settings.refund_auth import refund_auth_configured

    treasury_pay_methods = [
        m
        for m in list_payment_methods_for_pay(db, only_active=True)
        if is_treasury_wallet_method(m)
    ]
    return templates.TemplateResponse(
        "pos_shift_report.html",
        {
            "request": request,
            "shift": sh,
            "rows": rows,
            "sales_count": len(sales),
            "sales_total": total,
            "total_costs": total_costs,
            "store_name": store_name,
            "shift_financial": shift_financial,
            "show_drawer_expected": True,
            "shift_id": shift_id,
            "can_print_receipt": user_can_print_receipt(user),
            "can_correct_payment": user_can_correct_payment(user),
            "can_refund_sales": user_can_refund_sale(user),
            "refund_auth_configured": refund_auth_configured(db),
            "treasury_pay_methods": treasury_pay_methods,
        },
    )


@router.get("/shift/{shift_id}/invoice/{sale_id}/panel", response_class=HTMLResponse)
def pos_shift_invoice_panel(
    request: Request,
    shift_id: int,
    sale_id: int,
    db: DBSession,
    user: User = Depends(_pos_perm),
):
    sh = get_shift(db, shift_id)
    if sh is None or not _can_view_shift_detail(user, sh):
        return HTMLResponse("", status_code=404)
    from modules.payments.service import (
        is_treasury_wallet_method,
        list_payment_methods_for_pay,
    )
    from modules.sales.invoice_actions import invoice_action_flags, sale_context_label_ar
    from modules.sales.service import load_sale_with_lines

    sale = load_sale_with_lines(db, sale_id)
    if sale is None or sale.pos_shift_id != shift_id or sale.status != SaleStatus.COMPLETED:
        return HTMLResponse(
            '<p class="muted" style="margin:0">الفاتورة غير موجودة في هذه الجلسة.</p>',
            status_code=404,
        )
    flags = invoice_action_flags(db, user, sale)
    from modules.sales.invoice_actions import (
        sale_payment_method_label,
        sale_primary_payment_method_id,
    )

    return templates.TemplateResponse(
        "pos_shift_invoice_panel.html",
        {
            "request": request,
            "shift": sh,
            "sale": sale,
            "context_label": sale_context_label_ar(sale),
            "shift_id": shift_id,
            "payment_method_name": sale_payment_method_label(db, sale.id),
            "payment_method_id": sale_primary_payment_method_id(db, sale.id),
            "treasury_pay_methods": [
                m
                for m in list_payment_methods_for_pay(db, only_active=True)
                if is_treasury_wallet_method(m)
            ],
            **flags,
        },
    )


@router.post("/shift/{shift_id}/invoice/{sale_id}/change-payment", response_class=HTMLResponse)
def pos_shift_invoice_change_payment(
    request: Request,
    shift_id: int,
    sale_id: int,
    db: DBSession,
    user: User = Depends(_pos_perm),
    payment_method_id: str = Form(""),
):
    from modules.payments.service import PaymentsError, correct_sale_payment_method
    from modules.sales.invoice_actions import user_can_correct_payment
    from modules.sales.service import load_sale_with_lines

    if not user_can_correct_payment(user):
        return RedirectResponse(
            f"/pos/shift/{shift_id}/report?err="
            + quote("ليست لديك صلاحية تصحيح وسيلة الدفع."),
            status_code=302,
        )
    sh = get_shift(db, shift_id)
    if sh is None or not _can_view_shift_detail(user, sh):
        return RedirectResponse("/pos/shift", status_code=302)
    sale = load_sale_with_lines(db, sale_id)
    if sale is None or sale.pos_shift_id != shift_id:
        return RedirectResponse(
            f"/pos/shift/{shift_id}/report?err=" + quote("الفاتورة غير موجودة."),
            status_code=302,
        )
    try:
        pm_id = int(payment_method_id)
    except (TypeError, ValueError):
        return RedirectResponse(
            f"/pos/shift/{shift_id}/report?err=" + quote("اختر وسيلة دفع."),
            status_code=302,
        )
    try:
        correct_sale_payment_method(db, sale_id, pm_id, user_id=user.id)
        db.commit()
    except PaymentsError as exc:
        db.rollback()
        return RedirectResponse(
            f"/pos/shift/{shift_id}/report?err=" + quote(str(exc)),
            status_code=302,
        )
    return RedirectResponse(
        f"/pos/shift/{shift_id}/report?ok=" + quote("تم تصحيح وسيلة الدفع."),
        status_code=302,
    )


@router.get("/treasury", response_class=HTMLResponse)
def pos_treasury_page(
    request: Request,
    db: DBSession,
    user: User = Depends(_treasury_view),
    kind: str = Query("cash"),
    pm: int | None = Query(None),
    dir: str = Query("all"),
    day: str | None = Query(None),
):
    import logging

    from modules.payments.models import PaymentMethod
    from modules.payments.service import list_payment_methods_for_dashboard

    log = logging.getLogger("pos.treasury")
    try:
        pm_kind = PaymentMethodKind(kind.upper())
    except ValueError:
        pm_kind = PaymentMethodKind.CASH
    if pm_kind not in (PaymentMethodKind.CASH, PaymentMethodKind.BANK):
        pm_kind = PaymentMethodKind.CASH

    filter_day: date | None = None
    if day:
        try:
            filter_day = date.fromisoformat(day.strip())
        except ValueError:
            filter_day = None

    direction = (dir or "all").lower()
    if direction not in ("all", "in", "out"):
        direction = "all"

    treasury_accounts = list_payment_methods_for_dashboard(db, only_active=True)
    selected_pm_id = pm
    if selected_pm_id is not None:
        acc = db.get(PaymentMethod, selected_pm_id)
        if acc is None or not acc.show_on_dashboard:
            selected_pm_id = None

    try:
        if selected_pm_id is not None:
            summary = treasury_summary_for_method(db, selected_pm_id)
            pm_kind = summary.kind
            all_entries = list_ledger_entries(
                db, pm_kind, payment_method_id=selected_pm_id
            )
        else:
            summary = treasury_summary_for_kind(db, pm_kind)
            all_entries = list_ledger_entries(db, pm_kind)
        daily = daily_balance_rows(db, pm_kind, entries=all_entries)
        entries = all_entries
        if filter_day is not None:
            entries = [e for e in entries if e.at.date() == filter_day]
        if direction == "in":
            entries = [e for e in entries if e.direction == "IN"]
        elif direction == "out":
            entries = [e for e in entries if e.direction == "OUT"]
    except Exception as exc:
        log.exception("treasury page failed: %s", exc)
        label = "خزينة الكاش" if pm_kind == PaymentMethodKind.CASH else "خزينة المصرف"
        return templates.TemplateResponse(
            "pos_treasury.html",
            {
                "request": request,
                "user": user,
                "kind": pm_kind.value.lower(),
                "kind_label": label,
                "selected_pm_id": selected_pm_id,
                "treasury_accounts": treasury_accounts,
                "current_balance": Decimal("0"),
                "summary": None,
                "entries": [],
                "daily_rows": [],
                "filter_dir": direction,
                "filter_day": filter_day.isoformat() if filter_day else "",
                "store_name": get_setting(db, "store_name", "نقطة البيع"),
                "page_error": "تعذّر تحميل حركة الخزينة. راجع سجل السيرفر أو أعد التشغيل.",
            },
        )

    return templates.TemplateResponse(
        "pos_treasury.html",
        {
            "request": request,
            "user": user,
            "kind": pm_kind.value.lower(),
            "kind_label": summary.label_ar,
            "selected_pm_id": selected_pm_id,
            "treasury_accounts": treasury_accounts,
            "current_balance": summary.current_balance,
            "summary": summary,
            "entries": entries,
            "daily_rows": daily,
            "filter_dir": direction,
            "filter_day": filter_day.isoformat() if filter_day else "",
            "store_name": get_setting(db, "store_name", "نقطة البيع"),
            "page_error": None,
        },
    )


@router.get("/treasury/transfer", response_class=HTMLResponse)
def pos_treasury_transfer_page(
    request: Request,
    db: DBSession,
    _: User = Depends(_treasury_view),
    kind: str = Query("cash"),
    error: str | None = Query(None),
    ok: str | None = Query(None),
):
    import logging

    log = logging.getLogger("pos.treasury")
    try:
        pm_kind = PaymentMethodKind(kind.upper())
    except ValueError:
        pm_kind = PaymentMethodKind.CASH
    try:
        sources = list_payment_methods_transfer_sources(db, only_active=True)
        targets = list_payment_methods_transfer_targets(db, only_active=True)
        all_balances = payment_method_balances_map(db)
        balances = {
            m.id: all_balances.get(m.id, Decimal("0"))
            for m in {*sources, *targets}
        }
    except Exception as exc:
        log.exception("treasury transfer page failed: %s", exc)
        return templates.TemplateResponse(
            "pos_treasury_transfer.html",
            {
                "request": request,
                "kind": pm_kind.value.lower(),
                "sources": [],
                "targets": [],
                "balances": {},
                "error": error,
                "ok": ok,
                "page_error": "تعذّر تحميل صفحة التحويل. راجع سجل السيرفر أو أعد تشغيل التطبيق.",
            },
        )
    return templates.TemplateResponse(
        "pos_treasury_transfer.html",
        {
            "request": request,
            "kind": pm_kind.value.lower(),
            "sources": sources,
            "targets": targets,
            "balances": balances,
            "error": error,
            "ok": ok,
            "page_error": None,
        },
    )


@router.post("/treasury/transfer", response_class=HTMLResponse)
def pos_treasury_transfer_submit(
    request: Request,
    db: DBSession,
    user: User = Depends(_treasury_manage),
    from_payment_method_id: str = Form(...),
    to_payment_method_id: str = Form(...),
    amount: str = Form(...),
    note: str = Form(""),
    kind: str = Form("cash"),
):
    try:
        from_id = int(from_payment_method_id)
        to_id = int(to_payment_method_id)
        amt = _parse_money(amount)
    except (ValueError, InvalidOperation):
        return RedirectResponse(
            "/pos/treasury/transfer?kind="
            + quote(kind)
            + "&error="
            + quote("بيانات التحويل غير صالحة."),
            status_code=302,
        )
    try:
        record_manual_transfer(
            db,
            from_payment_method_id=from_id,
            to_payment_method_id=to_id,
            amount=amt,
            user_id=user.id,
            note=note,
        )
        db.commit()
    except PaymentsError as e:
        db.rollback()
        return RedirectResponse(
            "/pos/treasury/transfer?kind="
            + quote(kind)
            + "&error="
            + quote(str(e)),
            status_code=302,
        )
    except Exception as e:
        db.rollback()
        import logging

        logging.getLogger("pos.treasury").exception("treasury transfer submit failed: %s", e)
        return RedirectResponse(
            "/pos/treasury/transfer?kind="
            + quote(kind)
            + "&error="
            + quote("تعذّر تنفيذ التحويل. راجع سجل السيرفر."),
            status_code=302,
        )
    return RedirectResponse(
        "/pos/treasury/transfer?kind=" + quote(kind) + "&ok=1",
        status_code=302,
    )
