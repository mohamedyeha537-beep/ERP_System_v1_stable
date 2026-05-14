"""شاشة إدارة طاولات المطعم/المقهى (CRUD)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from starlette.requests import Request

from app.deps import DBSession, require_permission
from app.jinja_env import templates
from modules.authz.models import User
from modules.authz.permissions import TABLES_MANAGE
from modules.catalog.models import DiningTable, ProductCategory


router = APIRouter(prefix="/admin/tables", tags=["tables"])
_perm = require_permission(TABLES_MANAGE)


def _parse_int(s: str | None, default: int = 0) -> int:
    if s is None or not str(s).strip():
        return default
    try:
        return int(str(s).strip())
    except ValueError:
        return default


@router.get("", response_class=HTMLResponse)
def list_tables(
    request: Request,
    db: DBSession,
    _: User = Depends(_perm),
):
    tables = list(
        db.scalars(
            select(DiningTable).order_by(
                DiningTable.sort_order, DiningTable.id
            )
        ).all()
    )
    sections = list(
        db.scalars(
            select(ProductCategory)
            .where(ProductCategory.parent_id.is_(None))
            .order_by(ProductCategory.sort_order, ProductCategory.id)
        ).all()
    )
    return templates.TemplateResponse(
        "admin_tables.html",
        {
            "request": request,
            "tables": tables,
            "sections": sections,
            "saved": request.query_params.get("saved"),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/add", response_class=HTMLResponse)
def add_table(
    db: DBSession,
    _: User = Depends(_perm),
    name_ar: str = Form(...),
    section_category_id: str = Form(""),
    capacity: str = Form("4"),
    sort_order: str = Form("0"),
    notes: str = Form(""),
):
    name = name_ar.strip()
    if not name:
        return RedirectResponse(
            "/admin/tables?error=أدخل اسماً للطاولة.", status_code=302
        )
    if db.execute(
        select(DiningTable).where(DiningTable.name_ar == name)
    ).scalar_one_or_none() is not None:
        return RedirectResponse(
            "/admin/tables?error=اسم الطاولة مستخدم بالفعل.", status_code=302
        )
    sec_id_raw = (section_category_id or "").strip()
    sec_id: int | None = None
    if sec_id_raw:
        try:
            cand = int(sec_id_raw)
        except ValueError:
            cand = None
        if cand is not None:
            cat = db.get(ProductCategory, cand)
            if cat is not None and cat.parent_id is None:
                sec_id = cand
    t = DiningTable(
        name_ar=name,
        section_category_id=sec_id,
        capacity=max(1, _parse_int(capacity, 4)),
        sort_order=_parse_int(sort_order, 0),
        notes=(notes or "").strip() or None,
        is_active=True,
    )
    db.add(t)
    db.commit()
    return RedirectResponse("/admin/tables?saved=1", status_code=302)


@router.post("/{tid}/update", response_class=HTMLResponse)
def update_table(
    tid: int,
    db: DBSession,
    _: User = Depends(_perm),
    name_ar: str = Form(...),
    section_category_id: str = Form(""),
    capacity: str = Form("4"),
    sort_order: str = Form("0"),
    notes: str = Form(""),
    is_active: str = Form("on"),
):
    t = db.get(DiningTable, tid)
    if t is None:
        return RedirectResponse("/admin/tables", status_code=302)
    name = name_ar.strip()
    if not name:
        return RedirectResponse(
            "/admin/tables?error=الاسم مطلوب.", status_code=302
        )
    other = db.execute(
        select(DiningTable).where(
            DiningTable.name_ar == name, DiningTable.id != tid
        )
    ).scalar_one_or_none()
    if other is not None:
        return RedirectResponse(
            "/admin/tables?error=اسم الطاولة مستخدم بالفعل.", status_code=302
        )
    sec_id_raw = (section_category_id or "").strip()
    sec_id: int | None = None
    if sec_id_raw:
        try:
            cand = int(sec_id_raw)
        except ValueError:
            cand = None
        if cand is not None:
            cat = db.get(ProductCategory, cand)
            if cat is not None and cat.parent_id is None:
                sec_id = cand
    t.name_ar = name
    t.section_category_id = sec_id
    t.capacity = max(1, _parse_int(capacity, 4))
    t.sort_order = _parse_int(sort_order, 0)
    t.notes = (notes or "").strip() or None
    t.is_active = bool(is_active and is_active.lower() in ("on", "1", "true", "yes"))
    db.commit()
    return RedirectResponse("/admin/tables?saved=1", status_code=302)


@router.post("/{tid}/delete", response_class=HTMLResponse)
def delete_table(
    tid: int,
    db: DBSession,
    _: User = Depends(_perm),
):
    t = db.get(DiningTable, tid)
    if t is not None:
        db.delete(t)
        db.commit()
    return RedirectResponse("/admin/tables?saved=1", status_code=302)
