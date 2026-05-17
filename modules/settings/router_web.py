from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.deps import DBSession, require_permission
from app.jinja_env import templates
from modules.authz.models import User
from modules.authz.permissions import ADMIN_SETTINGS
from modules.inventory.service import get_main_warehouse, list_warehouses
from modules.settings.service import (
    PAPER_SIZES,
    get_setting,
    normalize_paper,
    set_setting,
)

router = APIRouter(prefix="/admin/settings", tags=["settings"])
_admin = require_permission(ADMIN_SETTINGS)


@router.get("", response_class=HTMLResponse)
def settings_page(
    request: Request,
    db: DBSession,
    _: User = Depends(_admin),
):
    main_wh = get_main_warehouse(db)
    wh_raw = get_setting(db, "default_sales_warehouse_id", "")
    try:
        sales_wh_id = int(wh_raw) if wh_raw else main_wh.id
    except ValueError:
        sales_wh_id = main_wh.id
    return templates.TemplateResponse(
        "admin_settings.html",
        {
            "request": request,
            "paper_sizes": PAPER_SIZES,
            "current_paper": get_setting(db, "print_paper_size", "A5"),
            "store_name": get_setting(db, "store_name", "نقطة البيع"),
            "warehouses": list_warehouses(db),
            "sales_warehouse_id": sales_wh_id,
            "saved": request.query_params.get("saved") == "1",
        },
    )


@router.post("", response_class=HTMLResponse)
def settings_save(
    request: Request,
    db: DBSession,
    _: User = Depends(_admin),
    print_paper_size: str = Form("A5"),
    store_name: str = Form(""),
    default_sales_warehouse_id: str = Form(""),
):
    set_setting(db, "print_paper_size", normalize_paper(print_paper_size, "A5"))
    set_setting(db, "store_name", store_name.strip() or "نقطة البيع")
    wh_raw = (default_sales_warehouse_id or "").strip()
    if wh_raw:
        from modules.inventory.service import resolve_warehouse_id

        try:
            wid = resolve_warehouse_id(db, int(wh_raw))
            set_setting(db, "default_sales_warehouse_id", str(wid))
        except Exception:
            set_setting(db, "default_sales_warehouse_id", "")
    else:
        set_setting(db, "default_sales_warehouse_id", "")
    db.commit()
    return RedirectResponse("/admin/settings?saved=1", status_code=302)
