"""مسارات تقارير جلسات الكاشير تحت /reports/shifts."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.deps import DBSession, require_permission
from app.jinja_env import templates
from modules.authz.models import User
from modules.authz.permissions import PAYMENTS_MANAGE, REPORTS_VIEW, SALES_EDIT_INVOICE, TREASURY_HANDOFF_REVOKE
from modules.authz.service import user_has_permission
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
        return RedirectResponse("/reports/hotel-collections?period=month", status_code=302)

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
    can_admin_close = user_has_permission(user, PAYMENTS_MANAGE)
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
        user_has_permission(user, PAYMENTS_MANAGE)
        and sh.status.value == "CLOSED"
        and sh.treasury_handoff_at is None
        and next_handoff is not None
        and next_handoff.id == sh.id
    )
    can_revoke_handoff = (
        user_has_permission(user, TREASURY_HANDOFF_REVOKE)
        and can_revoke_shift_handoff(db, shift_id)
    )
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
            "can_admin_close": user_has_permission(user, PAYMENTS_MANAGE),
            "can_approve_handoff": can_approve_handoff,
            "can_revoke_handoff": can_revoke_handoff,
            "next_handoff_shift_id": next_handoff.id if next_handoff else None,
            "can_edit_invoice": user_has_permission(user, SALES_EDIT_INVOICE),
            "error": err,
            "saved": bool(saved),
        },
    )


@router.post("/{shift_id:int}/admin-close", response_class=HTMLResponse)
def reports_shift_admin_close(
    shift_id: int,
    db: DBSession,
    user: User = Depends(require_permission(PAYMENTS_MANAGE)),
    counted_cash: str = Form(""),
    counted_bank: str = Form(""),
    closing_note: str = Form(""),
    cancel_safe_drafts: str = Form("on"),
    force_ignore_pending: str = Form(""),
):
    try:
        cc = _parse_money(counted_cash)
        cb = _parse_money(counted_bank)
    except (InvalidOperation, ValueError):
        return RedirectResponse(
            "/reports/shifts/" + str(shift_id) + "?err=" + quote("مبلغ غير صالح."),
            status_code=302,
        )
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
    user: User = Depends(require_permission(PAYMENTS_MANAGE)),
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
        approve_shift_handoff(
            db,
            shift_id=shift_id,
            user_id=user.id,
            admin_username=user.username,
            handoff_cash=parse_handoff_amount(handoff_cash),
            handoff_bank=parse_handoff_amount(handoff_bank),
            handoff_note=handoff_note,
        )
        db.commit()
    except ShiftHandoffError as e:
        db.rollback()
        return RedirectResponse(
            "/reports/shifts/" + str(shift_id) + "?err=" + quote(str(e)),
            status_code=302,
        )
    return RedirectResponse(
        "/reports/shifts/" + str(shift_id) + "?saved=1&handoff=1",
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
