"""شاشة العجوزات والزيادات — قرار إداري فقط."""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.deps import DBSession, require_any_permission
from app.jinja_env import templates
from modules.authz.models import User
from modules.authz.permissions import PAYMENTS_MANAGE, PURCHASES_MANAGE, TREASURY_HANDOFF_APPROVE
from modules.payments.shift_variances import (
    ShiftVarianceError,
    cancel_variance_after_investigation,
    charge_variance_to_employee,
    count_pending_variances,
    employee_shortage_recurrence,
    list_shift_variances,
    settle_variance_admin,
)

router = APIRouter(prefix="/admin/shift-variances", tags=["shift-variances"])
_perm = require_any_permission(PAYMENTS_MANAGE, PURCHASES_MANAGE, TREASURY_HANDOFF_APPROVE)


@router.get("", response_class=HTMLResponse)
def variances_page(
    request: Request,
    db: DBSession,
    _: User = Depends(_perm),
    status: str = "",
):
    st = (status or "").strip() or None
    return templates.TemplateResponse(
        "admin_shift_variances.html",
        {
            "request": request,
            "rows": list_shift_variances(db, status=st),
            "status_filter": st or "",
            "pending_count": count_pending_variances(db),
            "recurrence": employee_shortage_recurrence(db),
            "saved": request.query_params.get("saved"),
            "err": request.query_params.get("err"),
        },
    )


@router.post("/{variance_id}/charge")
def variance_charge(
    variance_id: int,
    db: DBSession,
    user: User = Depends(_perm),
    note: str = Form(""),
    employee_id: str = Form(""),
):
    emp = int(employee_id) if (employee_id or "").strip().isdigit() else None
    try:
        charge_variance_to_employee(
            db,
            variance_id=variance_id,
            resolved_by_id=user.id,
            note=note,
            employee_id=emp,
        )
        db.commit()
    except ShiftVarianceError as exc:
        db.rollback()
        return RedirectResponse(
            "/admin/shift-variances?err=" + quote(str(exc)), status_code=302
        )
    return RedirectResponse("/admin/shift-variances?saved=charged", status_code=302)


@router.post("/{variance_id}/settle")
def variance_settle(
    variance_id: int,
    db: DBSession,
    user: User = Depends(_perm),
    note: str = Form(...),
):
    try:
        settle_variance_admin(
            db, variance_id=variance_id, resolved_by_id=user.id, note=note
        )
        db.commit()
    except ShiftVarianceError as exc:
        db.rollback()
        return RedirectResponse(
            "/admin/shift-variances?err=" + quote(str(exc)), status_code=302
        )
    return RedirectResponse("/admin/shift-variances?saved=settled", status_code=302)


@router.post("/{variance_id}/cancel")
def variance_cancel(
    variance_id: int,
    db: DBSession,
    user: User = Depends(_perm),
    note: str = Form(...),
):
    try:
        cancel_variance_after_investigation(
            db, variance_id=variance_id, resolved_by_id=user.id, note=note
        )
        db.commit()
    except ShiftVarianceError as exc:
        db.rollback()
        return RedirectResponse(
            "/admin/shift-variances?err=" + quote(str(exc)), status_code=302
        )
    return RedirectResponse("/admin/shift-variances?saved=cancelled", status_code=302)
