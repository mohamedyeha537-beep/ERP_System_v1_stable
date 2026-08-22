"""تقرير جلسات الفندق — مثل /reports/shifts لجلسات الكاشير."""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.deps import DBSession, require_permission
from app.jinja_env import templates
from modules.authz.models import User
from modules.authz.capability import can_admin_close_hotel_shift
from modules.authz.permissions import REPORTS_VIEW
from modules.hotel.shift_models import HotelShiftError
from modules.hotel.shift_service import (
    admin_close_hotel_shift,
    list_hotel_shifts_index,
    list_stale_open_hotel_shifts,
)
from modules.reporting.exports import csv_response

router = APIRouter(prefix="/reports/hotel-shifts", tags=["hotel-shift-reports"])
_perm = require_permission(REPORTS_VIEW)


@router.get("", response_class=HTMLResponse)
def reports_hotel_shifts_index(
    request: Request,
    db: DBSession,
    user: User = Depends(_perm),
    status: str = Query("all"),
    page: int = Query(1, ge=1),
    err: str | None = Query(None),
    saved: int = Query(0),
):
    from modules.platform.business_domain import reports_show_hotel_sections

    if not reports_show_hotel_sections(user, request.session):
        return RedirectResponse("/reports/shifts", status_code=302)

    status_f = (status or "all").lower()
    if status_f not in ("all", "open", "closed"):
        status_f = "all"
    page_size = 40
    offset = (page - 1) * page_size
    rows, total = list_hotel_shifts_index(
        db, status_filter=status_f, limit=page_size, offset=offset
    )
    total_pages = max(1, (total + page_size - 1) // page_size)
    stale = list_stale_open_hotel_shifts(db, stale_hours=24.0)
    stale_ids = {s.id for s in stale}
    can_admin_close = can_admin_close_hotel_shift(user)
    return templates.TemplateResponse(
        "reports_hotel_shifts.html",
        {
            "request": request,
            "rows": rows,
            "status_filter": status_f,
            "page": page,
            "total": total,
            "total_pages": total_pages,
            "stale_ids": stale_ids,
            "stale_count": len(stale),
            "can_admin_close": can_admin_close,
            "error": err,
            "saved": bool(saved),
        },
    )


@router.post("/{shift_id:int}/admin-close")
def reports_hotel_shift_admin_close(
    shift_id: int,
    db: DBSession,
    user: User = Depends(_perm),
    note: str = Form(""),
):
    if not can_admin_close_hotel_shift(user):
        return RedirectResponse(
            "/reports/hotel-shifts?err=" + quote("لا صلاحية للإغلاق الإداري."),
            status_code=302,
        )
    try:
        admin_close_hotel_shift(db, shift_id, user_id=user.id, note=note)
        db.commit()
    except HotelShiftError as exc:
        db.rollback()
        return RedirectResponse(
            f"/reports/hotel-shifts?err={quote(str(exc))}", status_code=302
        )
    return RedirectResponse(
        f"/admin/hotel/shift/{shift_id}/report?saved=admin_closed", status_code=302
    )


@router.get("/export.csv")
def export_hotel_shifts_csv(
    request: Request,
    db: DBSession,
    user: User = Depends(_perm),
    status: str = Query("all"),
):
    from modules.platform.business_domain import reports_show_hotel_sections

    if not reports_show_hotel_sections(user, request.session):
        return RedirectResponse("/reports/shifts", status_code=302)

    status_f = (status or "all").lower()
    if status_f not in ("all", "open", "closed"):
        status_f = "all"
    rows, _total = list_hotel_shifts_index(
        db, status_filter=status_f, limit=5000, offset=0
    )
    headers = [
        "رقم الجلسة",
        "الوردية",
        "الموظف",
        "الحالة",
        "فتح",
        "إغلاق",
        "تحصيلات",
        "إيراد",
        "مصروف",
        "صافي",
        "كاش معدود",
        "مصرف معدود",
        "عجز",
        "فائض",
    ]
    data = []
    for r in rows:
        data.append(
            [
                r.shift_id,
                f"{r.shift_number} — {r.shift_name_ar}",
                r.operator_label,
                "مفتوحة" if r.status == "OPEN" else "مغلقة",
                r.opened_at,
                r.closed_at,
                r.payment_count,
                r.total_revenue,
                r.total_expenses,
                r.net_total,
                r.counted_cash if r.counted_cash is not None else "",
                r.counted_bank if r.counted_bank is not None else "",
                r.shortage_total if r.shortage_total > 0 else "",
                r.surplus_total if r.surplus_total > 0 else "",
            ]
        )
    return csv_response("hotel-shifts", headers, data)
