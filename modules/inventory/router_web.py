from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.deps import DBSession, require_permission
from app.jinja_env import templates
from modules.authz.models import User
from modules.authz.permissions import INVENTORY_ADJUST, INVENTORY_VIEW
from modules.catalog.models import Product, ProductKind
from modules.catalog.service import list_stockable_products
from modules.inventory.models import StockMovementType
from modules.inventory.service import apply_movement, get_balance, low_stock_products

router = APIRouter(prefix="/inventory", tags=["inventory"])


@router.get("", response_class=HTMLResponse)
def inventory_home(
    request: Request,
    db: DBSession,
    _: User = Depends(require_permission(INVENTORY_VIEW)),
):
    products = list_stockable_products(db)
    balances = {p.id: get_balance(db, p.id) for p in products}
    low = low_stock_products(db)
    # تصفية تنبيهات النفاد لتشمل فقط الأصناف المخزّنة (نفس قائمة العرض)
    stockable_ids = {p.id for p in products}
    low = [(p, q) for (p, q) in low if p.id in stockable_ids]
    return templates.TemplateResponse(
        "inventory.html",
        {"request": request, "products": products, "balances": balances, "low_stock": low},
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
):
    qty = Decimal(quantity.strip())
    try:
        mt = StockMovementType(movement_type)
    except ValueError:
        return RedirectResponse("/inventory?err=type", status_code=302)
    if mt == StockMovementType.SALE:
        return RedirectResponse("/inventory?err=sale_via_pos", status_code=302)
    # المخزون للمكوّنات (STOCK_ONLY) فقط — المنتجات النهائية (FINAL_SELLABLE)
    # تُخصم مكوّناتها تلقائياً عند البيع ولا تُسجّل لها حركات يدوية
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
        apply_movement(
            db,
            product_id=product_id,
            quantity_delta=delta,
            movement_type=mt,
            user_id=user.id,
            sale_id=None,
            note=note.strip() or None,
        )
        db.commit()
    except Exception:
        db.rollback()
        return RedirectResponse("/inventory?err=stock", status_code=302)
    return RedirectResponse("/inventory", status_code=302)
