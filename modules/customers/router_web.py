"""Customers admin pages + loyalty management."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.requests import Request

from app.deps import DBSession, require_permission
from app.jinja_env import templates
from modules.authz.models import User
from modules.authz.permissions import CUSTOMERS_MANAGE, CUSTOMERS_VIEW
from modules.customers.service import (
    CustomersError,
    adjust_points,
    create_customer,
    delete_customer,
    get_customer,
    list_customers,
    list_transactions,
    loyalty_settings,
    total_points_grand,
    update_customer,
)
from modules.settings.service import set_setting

router = APIRouter(prefix="/admin/customers", tags=["customers"])
_view = require_permission(CUSTOMERS_VIEW)
_manage = require_permission(CUSTOMERS_MANAGE)


@router.get("", response_class=HTMLResponse)
def customers_page(
    request: Request,
    db: DBSession,
    _: User = Depends(_view),
):
    import logging

    log = logging.getLogger("pos.customers")
    search = (request.query_params.get("q") or "").strip()
    try:
        customers = list_customers(db, search=search or None)
        settings = loyalty_settings(db)
        grand = total_points_grand(db)
    except Exception as exc:
        log.exception("customers page failed: %s", exc)
        return templates.TemplateResponse(
            "admin_customers.html",
            {
                "request": request,
                "customers": [],
                "search": search,
                "loyalty": {
                    "enabled": True,
                    "earn_per_dinar": Decimal("1"),
                    "redeem_value_per_point": Decimal("0.1"),
                    "min_points_to_redeem": Decimal("50"),
                },
                "grand_points": Decimal("0"),
                "saved": request.query_params.get("saved"),
                "error": (
                    "تعذّر تحميل العملاء. أعد تشغيل التطبيق لترقية قاعدة البيانات "
                    "أو راجع سجل السيرفر."
                ),
            },
        )
    return templates.TemplateResponse(
        "admin_customers.html",
        {
            "request": request,
            "customers": customers,
            "search": search,
            "loyalty": settings,
            "grand_points": grand,
            "saved": request.query_params.get("saved"),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/add", response_class=HTMLResponse)
def customers_add(
    db: DBSession,
    _: User = Depends(_manage),
    phone: str = Form(...),
    name: str = Form(""),
    email: str = Form(""),
    notes: str = Form(""),
):
    try:
        create_customer(db, phone=phone, name=name, email=email, notes=notes)
        db.commit()
    except CustomersError as e:
        db.rollback()
        return RedirectResponse(
            f"/admin/customers?error={e}", status_code=302
        )
    return RedirectResponse("/admin/customers?saved=1", status_code=302)


@router.post("/{cid}/edit", response_class=HTMLResponse)
def customers_edit(
    cid: int,
    db: DBSession,
    _: User = Depends(_manage),
    phone: str = Form(...),
    name: str = Form(""),
    email: str = Form(""),
    notes: str = Form(""),
    is_active: str = Form(""),
):
    try:
        update_customer(
            db,
            cid,
            phone=phone,
            name=name,
            email=email,
            notes=notes,
            is_active=(is_active == "on"),
        )
        db.commit()
    except CustomersError as e:
        db.rollback()
        return RedirectResponse(
            f"/admin/customers?error={e}", status_code=302
        )
    return RedirectResponse("/admin/customers?saved=1", status_code=302)


@router.post("/{cid}/delete", response_class=HTMLResponse)
def customers_delete(
    cid: int,
    db: DBSession,
    _: User = Depends(_manage),
):
    try:
        delete_customer(db, cid)
        db.commit()
    except CustomersError as e:
        db.rollback()
        return RedirectResponse(
            f"/admin/customers?error={e}", status_code=302
        )
    return RedirectResponse("/admin/customers?saved=1", status_code=302)


@router.get("/{cid}", response_class=HTMLResponse)
def customer_detail(
    request: Request,
    cid: int,
    db: DBSession,
    user: User = Depends(_view),
):
    c = get_customer(db, cid)
    if c is None:
        return RedirectResponse(
            "/admin/customers?error=العميل غير موجود.", status_code=302
        )
    txns = list_transactions(db, cid, limit=200)
    settings = loyalty_settings(db)
    return templates.TemplateResponse(
        "admin_customer_detail.html",
        {
            "request": request,
            "customer": c,
            "transactions": txns,
            "loyalty": settings,
            "saved": request.query_params.get("saved"),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/{cid}/adjust-points", response_class=HTMLResponse)
def customer_adjust_points(
    cid: int,
    db: DBSession,
    user: User = Depends(_manage),
    delta: str = Form(...),
    note: str = Form(""),
):
    try:
        d = Decimal(delta.strip())
    except (InvalidOperation, ValueError):
        return RedirectResponse(
            f"/admin/customers/{cid}?error=قيمة غير صحيحة.", status_code=302
        )
    try:
        adjust_points(db, customer_id=cid, delta_points=d, note=note, user_id=user.id)
        db.commit()
    except CustomersError as e:
        db.rollback()
        return RedirectResponse(
            f"/admin/customers/{cid}?error={e}", status_code=302
        )
    return RedirectResponse(f"/admin/customers/{cid}?saved=1", status_code=302)


# ============================================================
# Loyalty settings page
# ============================================================
loyalty_router = APIRouter(prefix="/admin/loyalty", tags=["loyalty"])
_loyalty_admin = require_permission(CUSTOMERS_MANAGE)


@loyalty_router.get("", response_class=HTMLResponse)
def loyalty_settings_page(
    request: Request,
    db: DBSession,
    _: User = Depends(_loyalty_admin),
):
    s = loyalty_settings(db)
    return templates.TemplateResponse(
        "admin_loyalty_settings.html",
        {
            "request": request,
            "loyalty": s,
            "saved": request.query_params.get("saved"),
            "error": request.query_params.get("error"),
        },
    )


@loyalty_router.post("/save", response_class=HTMLResponse)
def loyalty_settings_save(
    db: DBSession,
    _: User = Depends(_loyalty_admin),
    enabled: str = Form(""),
    earn_per_dinar: str = Form("1"),
    redeem_value_per_point: str = Form("0.1"),
    min_points_to_redeem: str = Form("50"),
):
    set_setting(db, "loyalty_enabled", "1" if enabled == "on" else "0")
    set_setting(db, "loyalty_earn_per_dinar", earn_per_dinar.strip() or "1")
    set_setting(
        db, "loyalty_redeem_value_per_point", redeem_value_per_point.strip() or "0.1"
    )
    set_setting(
        db, "loyalty_min_points_to_redeem", min_points_to_redeem.strip() or "50"
    )
    db.commit()
    return RedirectResponse("/admin/loyalty?saved=1", status_code=302)
