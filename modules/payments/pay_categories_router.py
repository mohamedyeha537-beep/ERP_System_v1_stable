"""إدارة بنود سندات الصرف (مصروف تشغيلي + خدمات) — للأدمن."""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.deps import DBSession, require_login
from app.jinja_env import templates
from modules.authz.models import User
from modules.platform.business_domain import is_system_admin
from modules.gl.models import GlAccount
from modules.payments.pay_categories import (
    KIND_OPERATING,
    KIND_UTILITY,
    add_pay_category,
    delete_pay_category,
    ensure_pay_category_gl_maps,
    load_pay_categories,
    save_pay_categories,
    update_pay_category,
)
from modules.pos_shifts.expense_categories import list_expense_gl_accounts

router = APIRouter(prefix="/admin/treasury-pay-categories", tags=["treasury-pay-categories"])


def _require_admin(user: User = Depends(require_login)) -> User:
    if not is_system_admin(user):
        raise HTTPException(status_code=403, detail="إدارة البنود متاحة لمدير النظام فقط.")
    return user


_perm = _require_admin


def _categories_for_admin(db) -> list[dict]:
    ensure_pay_category_gl_maps(db)
    out: list[dict] = []
    for c in load_pay_categories(db):
        row = dict(c)
        acc = db.get(GlAccount, int(row["gl_account_id"])) if row.get("gl_account_id") else None
        row["gl_label"] = f"{acc.code} — {acc.name_ar}" if acc is not None else "—"
        row["is_utility"] = row.get("kind") == KIND_UTILITY
        out.append(row)
    return out


@router.get("", response_class=HTMLResponse)
def categories_page(
    request: Request,
    db: DBSession,
    _: User = Depends(_perm),
):
    cats = _categories_for_admin(db)
    save_pay_categories(db, cats)
    db.commit()
    return templates.TemplateResponse(
        "admin_treasury_pay_categories.html",
        {
            "request": request,
            "categories": cats,
            "operating": [c for c in cats if c.get("kind") == KIND_OPERATING],
            "utilities": [c for c in cats if c.get("kind") == KIND_UTILITY],
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
    kind: str = Form(KIND_OPERATING),
    gl_account_id: str = Form(""),
):
    gl_id = int(gl_account_id) if (gl_account_id or "").strip().isdigit() else None
    try:
        add_pay_category(db, label=label, kind=kind, gl_account_id=gl_id)
        db.commit()
    except ValueError as exc:
        db.rollback()
        return RedirectResponse(
            "/admin/treasury-pay-categories?err=" + quote(str(exc)),
            status_code=302,
        )
    return RedirectResponse("/admin/treasury-pay-categories?saved=1", status_code=302)


@router.post("/{key}/update", response_class=HTMLResponse)
def categories_update(
    key: str,
    db: DBSession,
    _: User = Depends(_perm),
    label: str = Form(...),
    active: str = Form(""),
    kind: str = Form(KIND_OPERATING),
    gl_account_id: str = Form(""),
):
    gl_id = int(gl_account_id) if (gl_account_id or "").strip().isdigit() else None
    try:
        update_pay_category(
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
            "/admin/treasury-pay-categories?err=" + quote(str(exc)),
            status_code=302,
        )
    return RedirectResponse("/admin/treasury-pay-categories?saved=1", status_code=302)


@router.post("/{key}/delete", response_class=HTMLResponse)
def categories_delete(
    key: str,
    db: DBSession,
    _: User = Depends(_perm),
):
    delete_pay_category(db, key=key)
    db.commit()
    return RedirectResponse("/admin/treasury-pay-categories?saved=1", status_code=302)
