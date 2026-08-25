"""إدارة تصنيفات مصروفات الجلسة — للمدير."""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.deps import DBSession, require_any_permission
from app.jinja_env import templates
from modules.authz.models import User
from modules.authz.permissions import PAYMENTS_MANAGE, PURCHASES_MANAGE
from modules.gl.models import GlAccount
from modules.pos_shifts.expense_categories import (
    KIND_ADVANCE,
    add_category,
    delete_category,
    ensure_category_gl_maps,
    list_expense_gl_accounts,
    load_shift_expense_categories,
    update_category,
)

router = APIRouter(prefix="/admin/shift-expense-categories", tags=["shift-expense-categories"])
_perm = require_any_permission(PAYMENTS_MANAGE, PURCHASES_MANAGE)


def _categories_for_admin(db) -> list[dict]:
    ensure_category_gl_maps(db)
    out: list[dict] = []
    for c in load_shift_expense_categories(db):
        row = dict(c)
        acc = db.get(GlAccount, int(row["gl_account_id"])) if row.get("gl_account_id") else None
        row["gl_label"] = f"{acc.code} — {acc.name_ar}" if acc is not None else "—"
        row["is_advance"] = row.get("kind") == KIND_ADVANCE
        out.append(row)
    return out


@router.get("", response_class=HTMLResponse)
def categories_page(
    request: Request,
    db: DBSession,
    _: User = Depends(_perm),
):
    cats = _categories_for_admin(db)
    from modules.pos_shifts.expense_categories import save_shift_expense_categories

    save_shift_expense_categories(db, cats)
    db.commit()
    return templates.TemplateResponse(
        "admin_shift_expense_categories.html",
        {
            "request": request,
            "categories": cats,
            "gl_accounts": list_expense_gl_accounts(db),
            "saved": request.query_params.get("saved"),
            "err": request.query_params.get("err"),
        },
    )


@router.post("/add", response_class=HTMLResponse)
def categories_add(
    db: DBSession,
    _: User = Depends(_perm),
    label: str = Form(...),
    kind: str = Form("expense"),
    gl_account_id: str = Form(""),
):
    gl_id = int(gl_account_id) if (gl_account_id or "").strip().isdigit() else None
    try:
        add_category(db, label=label, kind=kind, gl_account_id=gl_id)
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
    kind: str = Form("expense"),
    gl_account_id: str = Form(""),
):
    gl_id = int(gl_account_id) if (gl_account_id or "").strip().isdigit() else None
    try:
        update_category(
            db,
            key=key,
            label=label,
            active=(active == "on"),
            kind=kind,
            gl_account_id=gl_id,
        )
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
