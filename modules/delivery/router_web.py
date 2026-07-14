from __future__ import annotations

from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.deps import DBSession, require_permission
from app.jinja_env import templates
from modules.authz.models import User
from modules.authz.permissions import DELIVERY_MANAGE, REPORTS_VIEW
from modules.delivery.service import (
    DeliveryError,
    create_zone,
    delete_zone,
    delivery_orders_report,
    get_zone,
    list_zones,
    update_zone,
)
from modules.reporting.router_web import _common_ctx, _resolve_period

router = APIRouter(tags=["delivery"])


def _parse_decimal(raw: str, default: str = "0") -> Decimal:
    text = (raw or "").strip().replace(",", ".")
    if not text:
        return Decimal(default)
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise DeliveryError("الرقم المدخل غير صالح.") from exc


@router.get("/admin/delivery-zones", response_class=HTMLResponse)
def delivery_zones_page(
    request: Request,
    db: DBSession,
    _: User = Depends(require_permission(DELIVERY_MANAGE)),
    error: str | None = Query(None),
    ok: str | None = Query(None),
    edit: int | None = Query(None),
):
    edit_zone = get_zone(db, int(edit)) if edit else None
    return templates.TemplateResponse(
        "admin_delivery_zones.html",
        {
            "request": request,
            "zones": list_zones(db, only_active=False),
            "edit_zone": edit_zone,
            "error": error,
            "ok": ok,
        },
    )


@router.post("/admin/delivery-zones", response_class=HTMLResponse)
def delivery_zones_create(
    request: Request,
    db: DBSession,
    _: User = Depends(require_permission(DELIVERY_MANAGE)),
    name_ar: str = Form(""),
    fee: str = Form("0"),
    sort_order: str = Form("0"),
    notes: str = Form(""),
):
    try:
        create_zone(
            db,
            name_ar=name_ar,
            fee=_parse_decimal(fee),
            sort_order=int((sort_order or "0").strip() or "0"),
            notes=notes,
        )
        db.commit()
        return RedirectResponse("/admin/delivery-zones?ok=1", status_code=302)
    except (DeliveryError, ValueError) as exc:
        db.rollback()
        return RedirectResponse(f"/admin/delivery-zones?error={exc}", status_code=302)


@router.post("/admin/delivery-zones/{zone_id}/update", response_class=HTMLResponse)
def delivery_zones_update(
    zone_id: int,
    request: Request,
    db: DBSession,
    _: User = Depends(require_permission(DELIVERY_MANAGE)),
    name_ar: str = Form(""),
    fee: str = Form("0"),
    sort_order: str = Form("0"),
    notes: str = Form(""),
    is_active: str = Form("0"),
):
    try:
        update_zone(
            db,
            zone_id,
            name_ar=name_ar,
            fee=_parse_decimal(fee),
            sort_order=int((sort_order or "0").strip() or "0"),
            notes=notes,
            is_active=is_active == "1",
        )
        db.commit()
        return RedirectResponse("/admin/delivery-zones?ok=1", status_code=302)
    except (DeliveryError, ValueError) as exc:
        db.rollback()
        return RedirectResponse(
            f"/admin/delivery-zones?edit={zone_id}&error={exc}",
            status_code=302,
        )


@router.post("/admin/delivery-zones/{zone_id}/delete", response_class=HTMLResponse)
def delivery_zones_delete(
    zone_id: int,
    request: Request,
    db: DBSession,
    _: User = Depends(require_permission(DELIVERY_MANAGE)),
):
    try:
        delete_zone(db, zone_id)
        db.commit()
        return RedirectResponse("/admin/delivery-zones?ok=1", status_code=302)
    except DeliveryError as exc:
        db.rollback()
        return RedirectResponse(f"/admin/delivery-zones?error={exc}", status_code=302)


@router.get("/reports/delivery", response_class=HTMLResponse)
def reports_delivery(
    request: Request,
    db: DBSession,
    user: User = Depends(require_permission(REPORTS_VIEW)),
    period: str = Query("day"),
    start: str | None = Query(None),
    end: str | None = Query(None),
):
    from modules.platform.business_domain import reports_show_pos_sections

    if not reports_show_pos_sections(user, request.session):
        return RedirectResponse("/reports/hotel-collections?period=month", status_code=302)

    period, s, e = _resolve_period(period, start, end)
    rows = delivery_orders_report(db, start=s, end=e)
    order_total = sum((row.order_total for row in rows), Decimal("0")).quantize(Decimal("0.001"))
    delivery_total = sum((row.delivery_fee for row in rows), Decimal("0")).quantize(Decimal("0.001"))
    customer_total = sum((row.customer_total for row in rows), Decimal("0")).quantize(Decimal("0.001"))
    ctx = _common_ctx(request, period, s, e, start, end)
    ctx.update(
        {
            "rows": rows,
            "orders_count": len(rows),
            "order_total": order_total,
            "delivery_total": delivery_total,
            "customer_total": customer_total,
        }
    )
    return templates.TemplateResponse("reports_delivery.html", ctx)
