"""شاشة المطبخ KDS — عرض وتحريك التذاكر."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from starlette.requests import Request

from app.deps import DBSession, require_permission
from app.jinja_env import templates
from modules.authz.models import User
from modules.authz.permissions import KDS_VIEW
from modules.catalog.models import CategoryRouting, ProductCategory
from modules.kds.service import advance_ticket, list_tickets, reset_ticket


router = APIRouter(prefix="/admin/kds", tags=["kds"])
_perm = require_permission(KDS_VIEW)


def _routed_root_categories(db) -> list[ProductCategory]:
    return list(
        db.scalars(
            select(ProductCategory)
            .where(
                ProductCategory.parent_id.is_(None),
                ProductCategory.routing_mode != CategoryRouting.NONE,
            )
            .order_by(ProductCategory.sort_order, ProductCategory.id)
        ).all()
    )


@router.get("", response_class=HTMLResponse)
def kds_home(
    request: Request,
    db: DBSession,
    _: User = Depends(_perm),
):
    sections = _routed_root_categories(db)
    sel_raw = request.query_params.get("section")
    only_open = request.query_params.get("show", "open") != "all"
    sel_id: int | None = None
    if sel_raw:
        try:
            sel_id = int(sel_raw)
        except ValueError:
            sel_id = None
    if sel_id is None and sections:
        sel_id = sections[0].id
    selected = (
        next((s for s in sections if s.id == sel_id), None) if sel_id else None
    )
    tickets = list_tickets(
        db, root_category_id=sel_id, only_open=only_open, limit=80
    )
    return templates.TemplateResponse(
        "admin_kds.html",
        {
            "request": request,
            "sections": sections,
            "selected": selected,
            "tickets": tickets,
            "only_open": only_open,
        },
    )


@router.post("/{tid}/advance", response_class=HTMLResponse)
def advance(
    tid: int,
    db: DBSession,
    request: Request,
    _: User = Depends(_perm),
):
    advance_ticket(db, tid)
    db.commit()
    section = request.query_params.get("section") or ""
    return RedirectResponse(
        f"/admin/kds?section={section}", status_code=302
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
    section = request.query_params.get("section") or ""
    return RedirectResponse(
        f"/admin/kds?section={section}", status_code=302
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
    rows = []
    from modules.sales.receipt_layout import resolve_root_category

    for ln in t.sale.lines:
        if ln.product is None:
            continue
        root = resolve_root_category(db, ln.product.category)
        if root is None or root.id != t.root_category_id:
            continue
        rows.append(ln)
    return templates.TemplateResponse(
        "kitchen_ticket_print.html",
        {
            "request": request,
            "ticket": t,
            "sale": t.sale,
            "section": t.root_category,
            "lines": rows,
            "autoprint": int(request.query_params.get("autoprint", "1") or 0),
        },
    )
