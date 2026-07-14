"""مركز إدارة الورديات — فلترة حسب اليوم والموظف."""
from __future__ import annotations

from datetime import date, timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.datetime_local import local_day_start_utc, now_local, parse_local_date_range
from app.deps import DBSession, require_any_permission
from app.jinja_env import templates
from modules.authz.models import User
from modules.authz.permissions import (
    HOTEL_BOOKING_MANAGE,
    HOTEL_FINANCE_CLOSE,
    REPORTS_VIEW,
    SALES_EDIT_INVOICE,
)
from modules.authz.service import user_has_permission
from modules.hotel.shift_service import list_hotel_shifts_index
from modules.hr.service import list_employees
from modules.platform.business_domain import reports_show_pos_sections
from modules.platform.module_registry import HOTEL_BOOKING
from modules.pos_shifts.service import list_stale_open_shifts
from modules.pos_shifts.shift_reports import list_shifts_index

router = APIRouter(prefix="/admin/shifts", tags=["admin-shifts"])

_view = require_any_permission(REPORTS_VIEW, HOTEL_BOOKING_MANAGE, HOTEL_FINANCE_CLOSE)


def _default_date_range() -> tuple[str, str]:
    today = now_local().date()
    start = today - timedelta(days=30)
    return start.isoformat(), today.isoformat()


def _parse_filters(
    *,
    date_from: str | None,
    date_to: str | None,
    employee_id: str | None,
    user_id: str | None,
) -> tuple[date, date, int | None, int | None, object | None]:
    d_from_s, d_to_s = _default_date_range()
    if date_from:
        d_from_s = date_from[:10]
    if date_to:
        d_to_s = date_to[:10]
    bounds = parse_local_date_range(d_from_s, d_to_s)
    emp_id: int | None = None
    uid: int | None = None
    if (employee_id or "").strip().isdigit():
        emp_id = int(employee_id.strip())
    if (user_id or "").strip().isdigit():
        uid = int(user_id.strip())
    return (
        date.fromisoformat(d_from_s),
        date.fromisoformat(d_to_s),
        emp_id,
        uid,
        bounds,
    )


def _filter_query(
    *,
    kind: str,
    status: str,
    date_from: date,
    date_to: date,
    employee_id: int | None,
    user_id: int | None,
    page: int,
) -> str:
    q = {
        "kind": kind,
        "status": status,
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "page": str(page),
    }
    if employee_id is not None:
        q["employee_id"] = str(employee_id)
    if user_id is not None:
        q["user_id"] = str(user_id)
    return urlencode(q)


@router.get("", response_class=HTMLResponse)
def admin_shifts_hub(
    request: Request,
    db: DBSession,
    user: User = Depends(_view),
    kind: str = Query("auto"),
    status: str = Query("all"),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    employee_id: str | None = Query(None),
    user_id: str | None = Query(None),
    page: int = Query(1, ge=1),
):
    from modules.platform.module_registry import HOTEL_BOOKING, module_enabled_map

    mods = module_enabled_map(db)
    show_pos = reports_show_pos_sections(user, request.session)
    show_hotel = bool(mods.get(HOTEL_BOOKING)) and (
        user_has_permission(user, HOTEL_BOOKING_MANAGE)
        or user_has_permission(user, HOTEL_FINANCE_CLOSE)
    )

    kind_f = (kind or "auto").lower()
    if kind_f == "auto":
        if show_pos and not show_hotel:
            kind_f = "pos"
        elif show_hotel and not show_pos:
            kind_f = "hotel"
        elif show_hotel:
            kind_f = "hotel"
        else:
            kind_f = "pos"
    if kind_f not in ("pos", "hotel"):
        kind_f = "hotel" if show_hotel else "pos"

    if kind_f == "pos" and not show_pos and user_has_permission(user, REPORTS_VIEW):
        return RedirectResponse("/reports/hotel-collections?period=month", status_code=302)

    d_from, d_to, emp_id, uid, bounds = _parse_filters(
        date_from=date_from,
        date_to=date_to,
        employee_id=employee_id,
        user_id=user_id,
    )
    status_f = (status or "all").lower()
    if status_f not in ("all", "open", "closed"):
        status_f = "all"

    page_size = 40
    offset = (page - 1) * page_size
    dt_from = bounds[0] if bounds else local_day_start_utc(d_from)
    dt_to = bounds[1] if bounds else local_day_start_utc(d_to + timedelta(days=1))

    pos_rows: list = []
    hotel_rows: list = []
    total = 0
    stale_ids: set[int] = set()
    stale_count = 0

    if kind_f == "pos" and show_pos:
        pos_rows, total = list_shifts_index(
            db,
            status_filter=status_f,
            date_from=dt_from,
            date_to=dt_to,
            employee_id=emp_id,
            user_id=uid,
            limit=page_size,
            offset=offset,
        )
        stale = list_stale_open_shifts(db, stale_hours=24.0)
        stale_ids = {s.id for s in stale}
        stale_count = len(stale)
    elif kind_f == "hotel" and show_hotel:
        hotel_rows, total = list_hotel_shifts_index(
            db,
            status_filter=status_f,
            date_from=dt_from,
            date_to=dt_to,
            employee_id=emp_id,
            user_id=uid,
            limit=page_size,
            offset=offset,
        )

    total_pages = max(1, (total + page_size - 1) // page_size)
    employees = list_employees(db, only_active=True)

    return templates.TemplateResponse(
        "admin_shifts.html",
        {
            "request": request,
            "kind": kind_f,
            "show_pos": show_pos,
            "show_hotel": show_hotel,
            "status_filter": status_f,
            "date_from": d_from.isoformat(),
            "date_to": d_to.isoformat(),
            "employee_id": emp_id,
            "user_id": uid,
            "page": page,
            "total": total,
            "total_pages": total_pages,
            "pos_rows": pos_rows,
            "hotel_rows": hotel_rows,
            "employees": employees,
            "stale_ids": stale_ids,
            "stale_count": stale_count,
            "page_prev_qs": _filter_query(
                kind=kind_f,
                status=status_f,
                date_from=d_from,
                date_to=d_to,
                employee_id=emp_id,
                user_id=uid,
                page=max(1, page - 1),
            ),
            "page_next_qs": _filter_query(
                kind=kind_f,
                status=status_f,
                date_from=d_from,
                date_to=d_to,
                employee_id=emp_id,
                user_id=uid,
                page=page + 1,
            ),
            "can_reports": user_has_permission(user, REPORTS_VIEW),
            "can_edit_invoice": user_has_permission(user, SALES_EDIT_INVOICE),
        },
    )
