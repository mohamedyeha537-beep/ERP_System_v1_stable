from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select, update
from sqlalchemy.orm import selectinload
from fastapi.responses import HTMLResponse, RedirectResponse

from app.deps import DBSession, require_permission
from app.jinja_env import templates
from modules.authz.models import User
from modules.authz.permissions import ADMIN_ROLES, CATALOG_WRITE
from modules.authz.service import user_has_permission
from modules.catalog.models import CategoryRouting, Product, ProductCategory

router = APIRouter(prefix="/categories", tags=["catalog-categories"])
_perm = require_permission(CATALOG_WRITE)
_lock_perm = require_permission(ADMIN_ROLES)


def _subtree_ids(db, root_id: int) -> list[int]:
    out: list[int] = []
    stack = [root_id]
    while stack:
        pid = stack.pop()
        out.append(pid)
        for cid in db.scalars(select(ProductCategory.id).where(ProductCategory.parent_id == pid)):
            stack.append(cid)
    return out


def _cannot_delete_ids(db, roots: list[ProductCategory]) -> set[int]:
    all_ids: set[int] = set()
    for r in roots:
        all_ids.update(_subtree_ids(db, r.id))
    cannot: set[int] = set()
    for cid in all_ids:
        for tid in _subtree_ids(db, cid):
            c = db.get(ProductCategory, tid)
            if c is not None and c.delete_protected:
                cannot.add(cid)
                break
    return cannot


def _subtree_has_protection(db, category_id: int) -> bool:
    for tid in _subtree_ids(db, category_id):
        c = db.get(ProductCategory, tid)
        if c is not None and c.delete_protected:
            return True
    return False


def _parse_sort(s: str) -> int:
    try:
        return int(s.strip() or "0")
    except ValueError:
        return 0


def _normalize_color(v: str) -> str:
    t = (v or "").strip()[:7]
    return t if t.startswith("#") and len(t) == 7 else "#3b82f6"


@router.get("", response_class=HTMLResponse)
def categories_list(request: Request, db: DBSession, user: User = Depends(_perm)):
    roots = list(
        db.scalars(
            select(ProductCategory)
            .where(ProductCategory.parent_id.is_(None))
            .options(selectinload(ProductCategory.children))
            .order_by(ProductCategory.sort_order, ProductCategory.id)
        ).all()
    )
    cannot_delete = _cannot_delete_ids(db, roots)
    err_code = request.query_params.get("error")
    error_message = None
    if err_code == "protected":
        error_message = (
            "لا يمكن حذف الفئة لوجود قفل حماية من الحذف عليها أو على إحدى الفئات الفرعية تحتها."
        )
    return templates.TemplateResponse(
        "catalog_categories.html",
        {
            "request": request,
            "roots": roots,
            "cannot_delete": cannot_delete,
            "can_lock_delete": user_has_permission(user, ADMIN_ROLES),
            "error_message": error_message,
            "routing_modes": [
                (CategoryRouting.NONE.value, "بدون توجيه"),
                (CategoryRouting.SCREEN.value, "شاشة المطبخ KDS"),
                (CategoryRouting.WHATSAPP.value, "رسالة واتساب (Webhook)"),
                (CategoryRouting.PRINT.value, "طباعة تذكرة منفصلة"),
            ],
        },
    )


@router.post("/add", response_class=HTMLResponse)
def add_category(
    db: DBSession,
    _: User = Depends(_perm),
    name_ar: str = Form(...),
    color_hex: str = Form("#3b82f6"),
    sort_order: str = Form("0"),
    parent_id: str = Form(""),
):
    """فئة جديدة: بدون أب = رئيسية؛ أو تندرج تحت فئة رئيسية تختارها من القائمة."""
    name = name_ar.strip()
    if not name:
        return RedirectResponse("/catalog/categories", status_code=302)
    parent_key = (parent_id or "").strip()
    pid: int | None = None
    if parent_key:
        try:
            p = db.get(ProductCategory, int(parent_key))
        except ValueError:
            p = None
        if p is None or p.parent_id is not None:
            return RedirectResponse("/catalog/categories", status_code=302)
        pid = p.id
    c = ProductCategory(
        name_ar=name,
        parent_id=pid,
        sort_order=_parse_sort(sort_order),
        color_hex=_normalize_color(color_hex),
    )
    db.add(c)
    db.commit()
    return RedirectResponse("/catalog/categories", status_code=302)


@router.post("/{category_id}/update", response_class=HTMLResponse)
def update_category(
    category_id: int,
    db: DBSession,
    _: User = Depends(_perm),
    name_ar: str = Form(...),
    color_hex: str = Form("#3b82f6"),
    sort_order: str = Form("0"),
    parent_root_id: str = Form(""),
    routing_mode: str = Form("NONE"),
    routing_target: str = Form(""),
):
    cat = db.get(ProductCategory, category_id)
    if cat is None:
        return RedirectResponse("/catalog/categories", status_code=302)
    name = name_ar.strip()
    if not name:
        return RedirectResponse("/catalog/categories", status_code=302)
    cat.name_ar = name
    cat.color_hex = _normalize_color(color_hex)
    cat.sort_order = _parse_sort(sort_order)
    if cat.parent_id is not None:
        raw = (parent_root_id or "").strip()
        if raw:
            try:
                new_parent_id = int(raw)
            except ValueError:
                new_parent_id = None
            else:
                p = db.get(ProductCategory, new_parent_id)
                if p is not None and p.parent_id is None:
                    cat.parent_id = new_parent_id
    # التوجيه يُطبَّق فعلياً على الفئة الجذر فقط (مطعم/مقهى)
    if cat.parent_id is None:
        try:
            cat.routing_mode = CategoryRouting(routing_mode)
        except ValueError:
            cat.routing_mode = CategoryRouting.NONE
        cat.routing_target = (routing_target or "").strip() or None
    db.commit()
    return RedirectResponse("/catalog/categories", status_code=302)


@router.post("/{category_id}/toggle-delete-protect", response_class=HTMLResponse)
def toggle_delete_protect(
    category_id: int,
    db: DBSession,
    _: User = Depends(_lock_perm),
):
    cat = db.get(ProductCategory, category_id)
    if cat is not None:
        cat.delete_protected = not cat.delete_protected
        db.commit()
    return RedirectResponse("/catalog/categories", status_code=302)


@router.post("/{category_id}/delete", response_class=HTMLResponse)
def delete_category(
    category_id: int,
    db: DBSession,
    _: User = Depends(_perm),
):
    cat = db.get(ProductCategory, category_id)
    if cat is None:
        return RedirectResponse("/catalog/categories", status_code=302)
    if _subtree_has_protection(db, category_id):
        return RedirectResponse("/catalog/categories?error=protected", status_code=302)
    ids = _subtree_ids(db, category_id)
    db.execute(update(Product).where(Product.category_id.in_(ids)).values(category_id=None))
    db.delete(cat)
    db.commit()
    return RedirectResponse("/catalog/categories", status_code=302)
