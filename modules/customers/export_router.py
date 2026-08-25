"""تصدير واستيراد العملاء — مسارات مستقلة لا تتعارض مع /admin/customers/{id}."""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import RedirectResponse
from starlette.requests import Request

from app.deps import DBSession, require_permission
from modules.authz.models import User
from modules.authz.permissions import CUSTOMERS_MANAGE, CUSTOMERS_VIEW
from modules.customers.import_handlers import handle_customers_import

router = APIRouter(prefix="/admin/export", tags=["export"])
_view = require_permission(CUSTOMERS_VIEW)
_manage = require_permission(CUSTOMERS_MANAGE)


@router.get("/customers")
def export_customers(request: Request, db: DBSession, user: User = Depends(_view)):
    from modules.customers.export import export_customers_for_request

    return export_customers_for_request(request, db, user)


@router.get("/customers/import")
def import_customers_get_hint():
    return RedirectResponse("/admin/customers", status_code=302)


@router.post("/customers/import")
async def import_customers(
    db: DBSession,
    user: User = Depends(_manage),
    file: UploadFile = File(...),
    update_existing: str = Form("on"),
    create_missing: str = Form("on"),
    restore_points: str = Form("on"),
    restore_wallet: str = Form("on"),
    restore_referral: str = Form("on"),
    return_to: str = Form("/admin/customers"),
):
    return await handle_customers_import(
        db,
        user,
        file=file,
        update_existing=update_existing,
        create_missing=create_missing,
        restore_points=restore_points,
        restore_wallet=restore_wallet,
        restore_referral=restore_referral,
        return_to=return_to,
    )
