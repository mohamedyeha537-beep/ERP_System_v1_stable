from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.deps import DBSession, require_permission
from app.jinja_env import templates
from modules.authz.models import User
from modules.authz.permissions import INVENTORY_ADJUST, INVENTORY_VIEW
from modules.catalog.models import Product, ProductKind
from modules.catalog.service import list_stockable_products
from modules.inventory.models import StockMovementType
from modules.inventory.service import (
    apply_movement,
    get_balance,
    get_main_warehouse,
    list_warehouses,
    low_stock_by_warehouse,
    low_stock_products,
    resolve_warehouse_id,
)

router = APIRouter(prefix="/inventory", tags=["inventory"])


def _parse_warehouse_id(db, raw: str | None) -> int:
    if raw:
        try:
            return resolve_warehouse_id(db, int(raw))
        except Exception:
            pass
    return get_main_warehouse(db).id


@router.get("", response_class=HTMLResponse)
def inventory_home(
    request: Request,
    db: DBSession,
    _: User = Depends(require_permission(INVENTORY_VIEW)),
    w: str | None = Query(None),
):
    warehouses = list_warehouses(db)
    warehouse_id = _parse_warehouse_id(db, w)
    current_wh = next((x for x in warehouses if x.id == warehouse_id), get_main_warehouse(db))
    products = list_stockable_products(db)
    balances = {p.id: get_balance(db, p.id, warehouse_id) for p in products}
    low = low_stock_products(db, warehouse_id)
    stockable_ids = {p.id for p in products}
    low = [(p, q) for (p, q) in low if p.id in stockable_ids]
    low_all = low_stock_by_warehouse(db)
    return templates.TemplateResponse(
        "inventory.html",
        {
            "request": request,
            "products": products,
            "balances": balances,
            "low_stock": low,
            "low_stock_all": low_all,
            "warehouses": warehouses,
            "current_warehouse": current_wh,
            "warehouse_id": warehouse_id,
        },
    )


@router.post("/movement", response_class=HTMLResponse)
def inventory_movement(
    request: Request,
    db: DBSession,
    user: User = Depends(require_permission(INVENTORY_ADJUST)),
    product_id: int = Form(...),
    quantity: str = Form(...),
    movement_type: str = Form(...),
    note: str = Form(""),
    warehouse_id: int = Form(...),
):
    qty = Decimal(quantity.strip())
    try:
        mt = StockMovementType(movement_type)
    except ValueError:
        return RedirectResponse("/inventory?err=type", status_code=302)
    if mt == StockMovementType.SALE:
        return RedirectResponse("/inventory?err=sale_via_pos", status_code=302)
    target = db.get(Product, product_id)
    if target is None or target.kind != ProductKind.STOCK_ONLY:
        return RedirectResponse("/inventory?err=composite", status_code=302)
    delta = qty
    if mt in (StockMovementType.PURCHASE, StockMovementType.ONLINE_SYNC):
        if delta < 0:
            delta = -delta
    elif mt == StockMovementType.ADJUSTMENT:
        pass
    else:
        delta = qty
    try:
        wid = resolve_warehouse_id(db, warehouse_id)
        apply_movement(
            db,
            product_id=product_id,
            quantity_delta=delta,
            movement_type=mt,
            user_id=user.id,
            warehouse_id=wid,
            sale_id=None,
            note=note.strip() or None,
        )
        db.commit()
    except Exception:
        db.rollback()
        return RedirectResponse(f"/inventory?w={warehouse_id}&err=stock", status_code=302)
    return RedirectResponse(f"/inventory?w={warehouse_id}", status_code=302)
