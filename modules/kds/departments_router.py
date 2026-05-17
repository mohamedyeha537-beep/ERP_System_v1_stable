"""إدارة أقسام المطبخ (منفصلة عن فئات نقطة البيع)."""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from app.deps import DBSession, require_permission
from app.jinja_env import templates
from modules.authz.models import User
from modules.authz.permissions import CATALOG_WRITE
from modules.catalog.models import CategoryRouting
from modules.kds.models import KitchenDepartment, KitchenVenue

router = APIRouter(prefix="/admin/kitchen-departments", tags=["kitchen-departments"])
_perm = require_permission(CATALOG_WRITE)


@router.get("", response_class=HTMLResponse)
def list_departments(
    request: Request,
    db: DBSession,
    _: User = Depends(_perm),
):
    venue = request.query_params.get("venue", "KITCHEN")
    try:
        venue_enum = KitchenVenue(venue.upper())
    except ValueError:
        venue_enum = KitchenVenue.KITCHEN
    rows = list(
        db.scalars(
            select(KitchenDepartment)
            .where(KitchenDepartment.venue == venue_enum)
            .order_by(KitchenDepartment.sort_order, KitchenDepartment.id)
        ).all()
    )
    return templates.TemplateResponse(
        "admin_kitchen_departments.html",
        {
            "request": request,
            "departments": rows,
            "venue": venue_enum,
            "venues": list(KitchenVenue),
        },
    )


@router.get("/new", response_class=HTMLResponse)
def new_department_form(
    request: Request,
    db: DBSession,
    _: User = Depends(_perm),
):
    venue = request.query_params.get("venue", "KITCHEN")
    try:
        venue_enum = KitchenVenue(venue.upper())
    except ValueError:
        venue_enum = KitchenVenue.KITCHEN
    return templates.TemplateResponse(
        "admin_kitchen_department_form.html",
        {
            "request": request,
            "department": None,
            "venue": venue_enum,
            "error": None,
        },
    )


@router.post("/new", response_class=HTMLResponse)
def new_department_submit(
    request: Request,
    db: DBSession,
    _: User = Depends(_perm),
    name_ar: str = Form(...),
    venue: str = Form("KITCHEN"),
    sort_order: str = Form("0"),
    routing_mode: str = Form("SCREEN"),
    routing_target: str = Form(""),
    is_active: str = Form("on"),
):
    name = (name_ar or "").strip()
    if not name:
        return _form_error(request, None, venue, "اسم القسم مطلوب.")
    try:
        venue_enum = KitchenVenue(venue.upper())
    except ValueError:
        venue_enum = KitchenVenue.KITCHEN
    try:
        mode = CategoryRouting(routing_mode)
    except ValueError:
        mode = CategoryRouting.SCREEN
    try:
        sort_val = int(sort_order.strip() or "0")
    except ValueError:
        sort_val = 0
    dept = KitchenDepartment(
        name_ar=name,
        venue=venue_enum,
        sort_order=sort_val,
        routing_mode=mode,
        routing_target=(routing_target or "").strip() or None,
        is_active=is_active == "on",
    )
    db.add(dept)
    db.commit()
    return RedirectResponse(
        f"/admin/kitchen-departments?venue={venue_enum.value}", status_code=302
    )


@router.get("/{dept_id}/edit", response_class=HTMLResponse)
def edit_department_form(
    request: Request,
    dept_id: int,
    db: DBSession,
    _: User = Depends(_perm),
):
    dept = db.get(KitchenDepartment, dept_id)
    if dept is None:
        return RedirectResponse("/admin/kitchen-departments", status_code=302)
    return templates.TemplateResponse(
        "admin_kitchen_department_form.html",
        {
            "request": request,
            "department": dept,
            "venue": dept.venue,
            "error": None,
        },
    )


@router.post("/{dept_id}/edit", response_class=HTMLResponse)
def edit_department_submit(
    request: Request,
    dept_id: int,
    db: DBSession,
    _: User = Depends(_perm),
    name_ar: str = Form(...),
    sort_order: str = Form("0"),
    routing_mode: str = Form("SCREEN"),
    routing_target: str = Form(""),
    is_active: str = Form(""),
):
    dept = db.get(KitchenDepartment, dept_id)
    if dept is None:
        return RedirectResponse("/admin/kitchen-departments", status_code=302)
    name = (name_ar or "").strip()
    if not name:
        return _form_error(request, dept, dept.venue, "اسم القسم مطلوب.")
    try:
        mode = CategoryRouting(routing_mode)
    except ValueError:
        mode = CategoryRouting.SCREEN
    try:
        sort_val = int(sort_order.strip() or "0")
    except ValueError:
        sort_val = 0
    dept.name_ar = name
    dept.sort_order = sort_val
    dept.routing_mode = mode
    dept.routing_target = (routing_target or "").strip() or None
    dept.is_active = is_active == "on"
    db.commit()
    return RedirectResponse(
        f"/admin/kitchen-departments?venue={dept.venue.value}", status_code=302
    )


@router.post("/{dept_id}/delete", response_class=HTMLResponse)
def delete_department(
    dept_id: int,
    db: DBSession,
    _: User = Depends(_perm),
):
    dept = db.get(KitchenDepartment, dept_id)
    if dept is None:
        return RedirectResponse("/admin/kitchen-departments", status_code=302)
    venue = dept.venue.value
    from modules.catalog.models import Product

    linked = db.execute(
        select(Product.id).where(Product.kitchen_department_id == dept_id).limit(1)
    ).scalar_one_or_none()
    if linked is not None:
        return RedirectResponse(
            "/admin/kitchen-departments?venue="
            + venue
            + "&err="
            + quote("لا يمكن حذف قسم مرتبط بأصناف. أزل الربط من الأصناف أولاً."),
            status_code=302,
        )
    db.delete(dept)
    db.commit()
    return RedirectResponse(f"/admin/kitchen-departments?venue={venue}", status_code=302)


def _form_error(request, department, venue, error: str):
    try:
        venue_enum = KitchenVenue(venue.upper() if isinstance(venue, str) else venue.value)
    except ValueError:
        venue_enum = KitchenVenue.KITCHEN
    return templates.TemplateResponse(
        "admin_kitchen_department_form.html",
        {
            "request": request,
            "department": department,
            "venue": venue_enum,
            "error": error,
        },
        status_code=400,
    )
