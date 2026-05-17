"""شاشة المطبخ KDS — عرض وتحريك التذاكر."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.requests import Request

from app.deps import DBSession, require_permission
from app.jinja_env import templates
from modules.authz.models import User
from modules.authz.permissions import KDS_VIEW
from modules.kds.service import (
    advance_ticket,
    build_master_ticket_sections,
    list_active_departments,
    list_legacy_routed_roots,
    list_tickets,
    reset_ticket,
)

router = APIRouter(prefix="/admin/kds", tags=["kds"])
_perm = require_permission(KDS_VIEW)


def _parse_section(request: Request) -> tuple[str | None, int | None]:
    """section=dept:3 أو section=cat:5"""
    raw = request.query_params.get("section", "")
    if raw.startswith("dept:"):
        try:
            return "dept", int(raw.split(":", 1)[1])
        except ValueError:
            return None, None
    if raw.startswith("cat:"):
        try:
            return "cat", int(raw.split(":", 1)[1])
        except ValueError:
            return None, None
    if raw.isdigit():
        return "cat", int(raw)
    return None, None


@router.get("", response_class=HTMLResponse)
def kds_home(
    request: Request,
    db: DBSession,
    _: User = Depends(_perm),
):
    departments = list_active_departments(db)
    legacy_roots = list_legacy_routed_roots(db)
    only_open = request.query_params.get("show", "open") != "all"
    kind, sel_id = _parse_section(request)
    if kind is None and departments:
        kind, sel_id = "dept", departments[0].id
    elif kind is None and legacy_roots:
        kind, sel_id = "cat", legacy_roots[0].id

    dept_id = sel_id if kind == "dept" else None
    cat_id = sel_id if kind == "cat" else None
    tickets = list_tickets(
        db,
        kitchen_department_id=dept_id,
        root_category_id=cat_id,
        only_open=only_open,
        limit=80,
    )
    selected_dept = next((d for d in departments if d.id == dept_id), None) if dept_id else None
    selected_cat = next((c for c in legacy_roots if c.id == cat_id), None) if cat_id else None

    return templates.TemplateResponse(
        "admin_kds.html",
        {
            "request": request,
            "departments": departments,
            "legacy_roots": legacy_roots,
            "selected_dept": selected_dept,
            "selected_cat": selected_cat,
            "section_kind": kind,
            "section_id": sel_id,
            "tickets": tickets,
            "only_open": only_open,
        },
    )


def _section_query(kind: str | None, sid: int | None) -> str:
    if kind == "dept" and sid:
        return f"dept:{sid}"
    if kind == "cat" and sid:
        return f"cat:{sid}"
    return ""


@router.post("/{tid}/advance", response_class=HTMLResponse)
def advance(
    tid: int,
    db: DBSession,
    request: Request,
    _: User = Depends(_perm),
):
    advance_ticket(db, tid)
    db.commit()
    kind, sid = _parse_section(request)
    return RedirectResponse(
        f"/admin/kds?section={_section_query(kind, sid)}", status_code=302
    )


@router.post("/{tid}/reset", response_class=HTMLResponse)
def reset(
    tid: int,
    db: DBSession,
    request: Request,
    _: User = Depends(_perm),
):
    reset_ticket(db, tid)
    db.commit()
    kind, sid = _parse_section(request)
    return RedirectResponse(
        f"/admin/kds?section={_section_query(kind, sid)}", status_code=302
    )


@router.get("/print/{tid}", response_class=HTMLResponse)
def print_ticket(
    tid: int,
    db: DBSession,
    request: Request,
    _: User = Depends(_perm),
):
    from modules.sales.models import KitchenTicket

    t = db.get(KitchenTicket, tid)
    if t is None or t.sale is None:
        return RedirectResponse("/admin/kds", status_code=302)
    from modules.kds.service import _lines_for_ticket, _section_label

    sale = t.sale
    section_name = _section_label(t)
    section_obj = t.kitchen_department or t.root_category
    sale_lines = []
    for ln in sale.lines:
        if ln.product is None:
            continue
        if t.kitchen_department_id:
            if ln.product.kitchen_department_id == t.kitchen_department_id:
                sale_lines.append(ln)
        elif t.root_category_id:
            from modules.sales.receipt_layout import resolve_root_category

            root = resolve_root_category(db, ln.product.category)
            if root and root.id == t.root_category_id:
                sale_lines.append(ln)
    return templates.TemplateResponse(
        "kitchen_ticket_print.html",
        {
            "request": request,
            "ticket": t,
            "sale": sale,
            "section": section_obj,
            "section_name": section_name,
            "lines": sale_lines,
            "autoprint": int(request.query_params.get("autoprint", "0") or 0),
        },
    )


@router.get("/print-sale/{sale_id}", response_class=HTMLResponse)
def print_sale_master(
    sale_id: int,
    db: DBSession,
    request: Request,
    _: User = Depends(_perm),
):
    from modules.sales.models import Sale

    sale = db.get(Sale, sale_id)
    if sale is None:
        return RedirectResponse("/admin/kds", status_code=302)
    sections = build_master_ticket_sections(db, sale)
    return templates.TemplateResponse(
        "kitchen_master_print.html",
        {
            "request": request,
            "sale": sale,
            "sections": sections,
            "autoprint": int(request.query_params.get("autoprint", "1") or 0),
        },
    )
