"""مسارات تقارير جلسات الكاشير تحت /reports/shifts."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.deps import DBSession, require_any_permission, require_permission
from app.jinja_env import templates
from modules.authz.capability import (
    can_admin_close_pos_shift,
    can_approve_treasury_handoff,
    can_reopen_closed_shift,
    can_revoke_treasury_handoff,
)
from modules.authz.models import User
from modules.authz.permissions import (
    PAYMENTS_MANAGE,
    POS_ADMIN_CLOSE_SHIFT,
    REPORTS_VIEW,
    SALES_EDIT_INVOICE,
    TREASURY_HANDOFF_APPROVE,
    TREASURY_HANDOFF_REVOKE,
)
from modules.authz.service import user_has_permission
from modules.pos_shifts.models import PosShift
from modules.pos_shifts.service import (
    PosShiftError,
    admin_close_shift,
    compute_expected_bank,
    compute_expected_cash,
    list_stale_open_shifts,
)
from modules.pos_shifts.shift_reports import (
    build_shift_detail_report,
    get_shift_for_report,
    list_shifts_index,
)
from modules.sales.service import list_pending_orders_blocking_shift_close
from modules.payments.shift_handovers import get_bank_declaration


def _pos_bank_decl(db, shift_id: int):
    return get_bank_declaration(db, pos_shift_id=shift_id)

router = APIRouter(prefix="/reports/shifts", tags=["pos-shift-reports"])
_perm = require_permission(REPORTS_VIEW)


def _parse_money(raw: str) -> Decimal | None:
    s = (raw or "").strip().replace(",", ".")
    if not s:
        return None
    return Decimal(s).quantize(Decimal("0.001"))


@router.get("", response_class=HTMLResponse)
def reports_shifts_index(
    request: Request,
    db: DBSession,
    user: User = Depends(_perm),
    status: str = Query("all"),
    page: int = Query(1, ge=1),
    err: str | None = Query(None),
    saved: int = Query(0),
):
    from modules.platform.business_domain import reports_show_pos_sections

    if not reports_show_pos_sections(user, request.session):
        return RedirectResponse("/reports/hotel-shifts", status_code=302)

    status_f = (status or "all").lower()
    if status_f not in ("all", "open", "closed"):
        status_f = "all"
    page_size = 40
    offset = (page - 1) * page_size
    rows, total = list_shifts_index(
        db, status_filter=status_f, limit=page_size, offset=offset
    )
    total_pages = max(1, (total + page_size - 1) // page_size)
    stale = list_stale_open_shifts(db, stale_hours=24.0)
    stale_ids = {s.id for s in stale}
    from modules.payments.shift_handoff_service import count_shifts_pending_handoff

    pending_handoff_count = count_shifts_pending_handoff(db)
    can_admin_close = can_admin_close_pos_shift(user)
    return templates.TemplateResponse(
        "reports_shifts.html",
        {
            "request": request,
            "rows": rows,
            "status_filter": status_f,
            "page": page,
            "total": total,
            "total_pages": total_pages,
            "stale_ids": stale_ids,
            "stale_count": len(stale),
            "pending_handoff_count": pending_handoff_count,
            "can_admin_close": can_admin_close,
            "error": err,
            "saved": bool(saved),
        },
    )


@router.get("/{shift_id:int}/loyalty", response_class=HTMLResponse)
def reports_shift_loyalty(
    request: Request,
    shift_id: int,
    db: DBSession,
    user: User = Depends(_perm),
):
    from modules.customers.loyalty_shift_reports import build_shift_loyalty_report
    from modules.customers.service import loyalty_settings

    sh = get_shift_for_report(db, shift_id)
    if sh is None:
        return RedirectResponse(
            "/reports/shifts?err=" + quote("الجلسة غير موجودة."),
            status_code=302,
        )
    loyalty = build_shift_loyalty_report(db, shift_id)
    return templates.TemplateResponse(
        "reports_shift_loyalty.html",
        {
            "request": request,
            "shift": sh,
            "loyalty": loyalty,
            "loyalty_settings": loyalty_settings(db),
        },
    )


@router.get("/{shift_id:int}", response_class=HTMLResponse)
def reports_shift_detail(
    request: Request,
    shift_id: int,
    db: DBSession,
    user: User = Depends(_perm),
    err: str | None = Query(None),
    saved: int = Query(0),
):
    report = build_shift_detail_report(db, shift_id)
    if report is None:
        return RedirectResponse(
            "/reports/shifts?err=" + quote("الجلسة غير موجودة."),
            status_code=302,
        )
    sh = report.shift
    pending_orders: list[dict] = []
    expected_cash = None
    expected_bank = None
    if sh.status.value == "OPEN":
        pending_orders = list_pending_orders_blocking_shift_close(
            db, user_id=sh.user_id, pos_shift_id=shift_id
        )
        expected_cash = compute_expected_cash(db, shift_id)
        expected_bank = compute_expected_bank(db, shift_id)
    from modules.payments.shift_handoff_service import (
        can_revoke_shift_handoff,
        get_next_shift_pending_handoff,
    )

    next_handoff = get_next_shift_pending_handoff(db)
    can_approve_handoff = (
        can_approve_treasury_handoff(user)
        and sh.status.value == "CLOSED"
        and sh.treasury_handoff_at is None
        and next_handoff is not None
        and next_handoff.id == sh.id
    )
    can_revoke_handoff = (
        can_revoke_treasury_handoff(user)
        and can_revoke_shift_handoff(db, shift_id)
    )
    close_employees: list = []
    if sh.status.value == "OPEN" and not sh.employee_id:
        try:
            from modules.hr.service import list_employees

            close_employees = list_employees(db, only_active=True)
        except Exception:  # noqa: BLE001
            close_employees = []
    return templates.TemplateResponse(
        "reports_shift_detail.html",
        {
            "request": request,
            "report": report,
            "shift": sh,
            "shift_financial": report.financial,
            "pending_orders": pending_orders,
            "expected_cash": expected_cash,
            "expected_bank": expected_bank,
            "can_admin_close": can_admin_close_pos_shift(user),
            "can_reopen_shift": can_reopen_closed_shift(user)
            and sh.status.value == "CLOSED",
            "can_approve_handoff": can_approve_handoff,
            "can_revoke_handoff": can_revoke_handoff,
            "next_handoff_shift_id": next_handoff.id if next_handoff else None,
            "can_edit_invoice": user_has_permission(user, SALES_EDIT_INVOICE),
            "error": err,
            "close_employees": close_employees,
            "saved": bool(saved),
            "bank_declaration": _pos_bank_decl(db, sh.id),
            "bank_declare_action": f"/pos/shift/{sh.id}/bank-transfer",
            "bank_default": sh.counted_bank or sh.expected_bank or 0,
        },
    )


@router.post("/{shift_id:int}/admin-close", response_class=HTMLResponse)
def reports_shift_admin_close(
    shift_id: int,
    db: DBSession,
    user: User = Depends(
        require_any_permission(POS_ADMIN_CLOSE_SHIFT, PAYMENTS_MANAGE)
    ),
    counted_cash: str = Form(""),
    counted_bank: str = Form(""),
    closing_note: str = Form(""),
    cancel_safe_drafts: str = Form("on"),
    force_ignore_pending: str = Form(""),
    responsible_employee_id: str = Form(""),
):
    if not (counted_cash or "").strip() or not (counted_bank or "").strip():
        return RedirectResponse(
            "/reports/shifts/"
            + str(shift_id)
            + "?err="
            + quote("أدخل المعدود للكاش والمصرف — لا يُقفَل بالمتوقع تلقائياً."),
            status_code=302,
        )
    try:
        cc = _parse_money(counted_cash)
        cb = _parse_money(counted_bank)
    except (InvalidOperation, ValueError):
        return RedirectResponse(
            "/reports/shifts/" + str(shift_id) + "?err=" + quote("مبلغ غير صالح."),
            status_code=302,
        )
    if cc is None or cb is None:
        return RedirectResponse(
            "/reports/shifts/"
            + str(shift_id)
            + "?err="
            + quote("أدخل المعدود للكاش والمصرف."),
            status_code=302,
        )
    if len((closing_note or "").strip()) < 2:
        return RedirectResponse(
            "/reports/shifts/"
            + str(shift_id)
            + "?err="
            + quote("أدخل بيان الإغلاق — الوصف إلزامي."),
            status_code=302,
        )
    emp_raw = (responsible_employee_id or "").strip()
    emp_id: int | None = None
    if emp_raw.isdigit() and int(emp_raw) > 0:
        emp_id = int(emp_raw)
    try:
        admin_close_shift(
            db,
            shift_id=shift_id,
            admin_user_id=user.id,
            admin_username=user.username,
            counted_cash=cc,
            counted_bank=cb,
            closing_note=closing_note,
            cancel_safe_drafts=cancel_safe_drafts == "on",
            force_ignore_pending=force_ignore_pending == "on",
            responsible_employee_id=emp_id,
        )
        db.commit()
    except PosShiftError as e:
        db.rollback()
        return RedirectResponse(
            "/reports/shifts/" + str(shift_id) + "?err=" + quote(str(e)),
            status_code=302,
        )
    return RedirectResponse(
        "/reports/shifts/" + str(shift_id) + "?saved=1",
        status_code=302,
    )


@router.post("/{shift_id:int}/handoff", response_class=HTMLResponse)
def reports_shift_handoff(
    shift_id: int,
    db: DBSession,
    user: User = Depends(
        require_any_permission(TREASURY_HANDOFF_APPROVE, PAYMENTS_MANAGE)
    ),
    handoff_cash: str = Form(""),
    handoff_bank: str = Form(""),
    handoff_note: str = Form(""),
):
    from modules.payments.shift_handoff_service import (
        ShiftHandoffError,
        approve_shift_handoff,
        parse_handoff_amount,
    )

    try:
        sh = db.get(PosShift, shift_id)
        claimed_c = (
            sh.counted_cash if sh is not None and sh.counted_cash is not None else None
        )
        claimed_b = (
            sh.counted_bank if sh is not None and sh.counted_bank is not None else None
        )
        rec_c = parse_handoff_amount(handoff_cash)
        rec_b = parse_handoff_amount(handoff_bank)
        approve_shift_handoff(
            db,
            shift_id=shift_id,
            user_id=user.id,
            admin_username=user.username,
            handoff_cash=rec_c,
            handoff_bank=rec_b,
            handoff_note=handoff_note,
        )
        db.commit()
    except ShiftHandoffError as e:
        db.rollback()
        return RedirectResponse(
            "/reports/shifts/" + str(shift_id) + "?err=" + quote(str(e)),
            status_code=302,
        )
    extra = ""
    if sh is not None and rec_c is not None and claimed_c is not None:
        from decimal import Decimal

        from modules.payments.shift_variances import money3

        dc = money3(rec_c) - money3(claimed_c)
        dbk = money3(rec_b if rec_b is not None else claimed_b) - money3(
            claimed_b if claimed_b is not None else 0
        )
        total = (dc + dbk).quantize(Decimal("0.001"))
        if total < 0:
            extra = "&shortage=" + quote(f"{abs(total):.3f}")
        elif total > 0:
            extra = "&overage=" + quote(f"{total:.3f}")
    return RedirectResponse(
        "/reports/shifts/" + str(shift_id) + "?saved=1&handoff=1" + extra,
        status_code=302,
    )


@router.post("/{shift_id:int}/handoff-revoke", response_class=HTMLResponse)
def reports_shift_handoff_revoke(
    shift_id: int,
    db: DBSession,
    user: User = Depends(require_permission(TREASURY_HANDOFF_REVOKE)),
    revoke_reason: str = Form(""),
):
    from modules.payments.shift_handoff_service import (
        ShiftHandoffError,
        revoke_shift_handoff,
    )

    try:
        revoke_shift_handoff(
            db,
            shift_id=shift_id,
            user_id=user.id,
            admin_username=user.username,
            reason=revoke_reason,
        )
        db.commit()
    except ShiftHandoffError as e:
        db.rollback()
        return RedirectResponse(
            "/reports/shifts/" + str(shift_id) + "?err=" + quote(str(e)),
            status_code=302,
        )
    return RedirectResponse(
        "/reports/shifts/" + str(shift_id) + "?saved=1&handoff_revoked=1",
        status_code=302,
    )
