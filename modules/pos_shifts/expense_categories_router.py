"""إدارة تصنيفات مصروفات الجلسة — للمدير."""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.deps import DBSession, require_permission
from app.jinja_env import templates
from modules.authz.models import User
from modules.authz.permissions import PAYMENTS_MANAGE
from modules.pos_shifts.expense_categories import (
    add_category,
    delete_category,
    load_shift_expense_categories,
    update_category,
)

router = APIRouter(prefix="/admin/shift-expense-categories", tags=["shift-expense-categories"])
_perm = require_permission(PAYMENTS_MANAGE)


@router.get("", response_class=HTMLResponse)
def categories_page(
    request: Request,
    db: DBSession,
    _: User = Depends(_perm),
):
    return templates.TemplateResponse(
        "admin_shift_expense_categories.html",
        {
            "request": request,
            "categories": load_shift_expense_categories(db),
            "saved": request.query_params.get("saved"),
            "err": request.query_params.get("err"),
        },
    )


@router.post("/add", response_class=HTMLResponse)
def categories_add(
    db: DBSession,
    _: User = Depends(_perm),
    label: str = Form(...),
):
    try:
        add_category(db, label=label)
        db.commit()
    except ValueError as exc:
        db.rollback()
        return RedirectResponse(
            "/admin/shift-expense-categories?err=" + quote(str(exc)),
            status_code=302,
        )
    return RedirectResponse("/admin/shift-expense-categories?saved=1", status_code=302)


@router.post("/{key}/update", response_class=HTMLResponse)
def categories_update(
    key: str,
    db: DBSession,
    _: User = Depends(_perm),
    label: str = Form(...),
    active: str = Form(""),
):
    try:
        update_category(db, key=key, label=label, active=(active == "on"))
        db.commit()
    except ValueError as exc:
        db.rollback()
        return RedirectResponse(
            "/admin/shift-expense-categories?err=" + quote(str(exc)),
            status_code=302,
        )
    return RedirectResponse("/admin/shift-expense-categories?saved=1", status_code=302)


@router.post("/{key}/delete", response_class=HTMLResponse)
def categories_delete(
    key: str,
    db: DBSession,
    _: User = Depends(_perm),
):
    delete_category(db, key=key)
    db.commit()
    return RedirectResponse("/admin/shift-expense-categories?saved=1", status_code=302)
