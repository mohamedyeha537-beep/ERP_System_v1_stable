from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from app.deps import DBSession, require_permission
from app.jinja_env import templates
from modules.authz.models import User
from modules.authz.permissions import HR_MANAGE, HR_VIEW
from modules.hr.meal_allowance import (
    EmployeeMealError,
    checkout_employee_options,
    current_period_label,
    ensure_meal_wallet,
    parse_money,
)
from modules.hr.models import EmployeeMealRedemption

router = APIRouter(prefix="/admin/employee-meals", tags=["hr-meals"])
_view_perm = require_permission(HR_VIEW)
_manage_perm = require_permission(HR_MANAGE)


@router.get("", response_class=HTMLResponse)
def employee_meals_page(
    request: Request,
    db: DBSession,
    _: User = Depends(_view_perm),
    period: str = Query(""),
    saved: str = Query(""),
    error: str = Query(""),
):
    period_label = (period or "").strip() or current_period_label()
    options = checkout_employee_options(db, period_label)
    recent = list(
        db.scalars(
            select(EmployeeMealRedemption)
            .order_by(EmployeeMealRedemption.id.desc())
            .limit(50)
        ).all()
    )
    return templates.TemplateResponse(
        "admin_employee_meals.html",
        {
            "request": request,
            "period_label": period_label,
            "options": options,
            "recent": recent,
            "saved": saved,
            "error": error,
        },
    )


@router.post("/save", response_class=HTMLResponse)
def employee_meals_save(
    db: DBSession,
    _: User = Depends(_manage_perm),
    employee_id: int = Form(...),
    period_label: str = Form(""),
    allowance_amount: str = Form("0"),
    notes: str = Form(""),
    is_active: str = Form(""),
):
    period = (period_label or "").strip() or current_period_label()
    try:
        ensure_meal_wallet(
            db,
            employee_id=employee_id,
            period_label=period,
            allowance_amount=parse_money(allowance_amount),
            notes=notes,
            is_active=is_active == "on",
        )
        db.commit()
    except EmployeeMealError as exc:
        db.rollback()
        return RedirectResponse(
            f"/admin/employee-meals?period={quote(period)}&error={quote(str(exc))}",
            status_code=302,
        )
    return RedirectResponse(
        f"/admin/employee-meals?period={quote(period)}&saved=1",
        status_code=302,
    )
