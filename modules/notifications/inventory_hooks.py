"""إشعارات مخزون — المرحلة 3."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from modules.notifications.events import (
    INVENTORY_BOM_MISSING_COST,
    INVENTORY_EXPIRED_ITEM,
    INVENTORY_EXPIRY_WARNING,
    INVENTORY_LOW_STOCK,
    INVENTORY_NEGATIVE_STOCK,
    INVENTORY_OUT_OF_STOCK,
    INVENTORY_PURCHASE_NEEDED,
    INVENTORY_PURCHASE_RECEIVED,
    INVENTORY_RECIPE_COST_CHANGED,
    INVENTORY_STOCK_ADJUSTMENT_CREATED,
    INVENTORY_STOCK_REPLENISHED,
    INVENTORY_TRANSFER_RECEIVED,
    INVENTORY_UNUSUAL_STOCK_MOVEMENT,
    INVENTORY_WASTE_RECORDED,
)
from modules.notifications.service import emit_event_safe
from modules.settings.service import get_int, get_setting

__all__ = [
    "emit_bom_missing_cost_if_needed",
    "emit_expiry_scan",
    "emit_inventory_after_movement",
    "emit_inventory_balance_after_movement",
    "emit_inventory_low_stock_if_needed",
    "emit_purchase_needed_digest",
    "emit_purchase_received",
    "emit_recipe_cost_changed",
]


def _unit_name(db: Session, product) -> str:
    if not product.unit_id:
        return ""
    from modules.catalog.models import Unit

    u = db.get(Unit, product.unit_id)
    return u.name_ar if u else ""


def _inventory_payload(
    db: Session,
    *,
    product,
    warehouse_id: int,
    qty: Decimal,
    **extra: Any,
) -> dict[str, Any]:
    reorder = Decimal(str(product.reorder_level or 0))
    unit = _unit_name(db, product)
    payload: dict[str, Any] = {
        "product_id": product.id,
        "warehouse_id": warehouse_id,
        "product_name": product.name_ar or str(product.id),
        "balance": str(qty.quantize(Decimal("0.001"))),
        "balance_qty": str(qty.quantize(Decimal("0.001"))),
        "reorder_level": str(reorder.quantize(Decimal("0.001"))),
        "unit": unit,
    }
    payload.update(extra)
    return payload


def emit_inventory_balance_after_movement(
    db: Session,
    *,
    product_id: int,
    warehouse_id: int,
    old_qty: Decimal | None = None,
) -> None:
    from modules.catalog.models import Product
    from modules.inventory.service import ensure_balance_row, is_low_stock

    p = db.get(Product, product_id)
    if p is None:
        return
    bal = ensure_balance_row(db, product_id, warehouse_id)
    qty = Decimal(str(bal.quantity or 0))
    reorder = Decimal(str(p.reorder_level or 0))
    payload = _inventory_payload(db, product=p, warehouse_id=warehouse_id, qty=qty)

    if qty < 0:
        emit_event_safe(
            db,
            event_key=INVENTORY_NEGATIVE_STOCK,
            source_type="inventory",
            source_id=product_id,
            payload=payload,
        )
        return
    if qty == 0:
        emit_event_safe(
            db,
            event_key=INVENTORY_OUT_OF_STOCK,
            source_type="inventory",
            source_id=product_id,
            payload=payload,
        )
        return
    if is_low_stock(qty, reorder):
        emit_event_safe(
            db,
            event_key=INVENTORY_LOW_STOCK,
            source_type="inventory",
            source_id=product_id,
            payload=payload,
        )
        return
    if old_qty is not None and is_low_stock(old_qty, reorder):
        emit_event_safe(
            db,
            event_key=INVENTORY_STOCK_REPLENISHED,
            source_type="inventory",
            source_id=product_id,
            payload=payload,
        )


def emit_inventory_low_stock_if_needed(
    db: Session,
    *,
    product_id: int,
    warehouse_id: int,
) -> None:
    emit_inventory_balance_after_movement(
        db, product_id=product_id, warehouse_id=warehouse_id
    )


def _unusual_threshold(db: Session, product) -> Decimal:
    custom = get_int(db, "notification_inventory_unusual_qty", 0)
    if custom > 0:
        return Decimal(str(custom))
    reorder = Decimal(str(product.reorder_level or 0))
    if reorder > 0:
        return max(reorder * Decimal("3"), Decimal("10"))
    return Decimal("50")


def emit_inventory_after_movement(
    db: Session,
    *,
    product_id: int,
    warehouse_id: int,
    quantity_delta: Decimal,
    movement_type,
    old_qty: Decimal,
    movement_id: int | None = None,
    note: str | None = None,
    reason_label: str | None = None,
) -> None:
    from modules.catalog.models import Product
    from modules.inventory.models import StockMovementType

    emit_inventory_balance_after_movement(
        db,
        product_id=product_id,
        warehouse_id=warehouse_id,
        old_qty=old_qty,
    )

    p = db.get(Product, product_id)
    if p is None:
        return
    bal_qty = old_qty + quantity_delta
    base = _inventory_payload(
        db,
        product=p,
        warehouse_id=warehouse_id,
        qty=bal_qty,
        movement_qty=str(abs(quantity_delta).quantize(Decimal("0.001"))),
        note=(note or "").strip(),
        reason=(reason_label or note or "").strip(),
    )

    mt = movement_type
    if mt in (
        StockMovementType.SALE,
        StockMovementType.SALE_RETURN,
        StockMovementType.PURCHASE,
        StockMovementType.ONLINE_SYNC,
    ):
        return
    if mt == StockMovementType.ADJUSTMENT or mt == StockMovementType.SHORTAGE:
        emit_event_safe(
            db,
            event_key=INVENTORY_STOCK_ADJUSTMENT_CREATED,
            source_type="inventory",
            source_id=movement_id or product_id,
            payload=base,
        )
    elif mt == StockMovementType.WASTE:
        emit_event_safe(
            db,
            event_key=INVENTORY_WASTE_RECORDED,
            source_type="inventory",
            source_id=movement_id or product_id,
            payload=base,
        )
    elif mt == StockMovementType.TRANSFER and quantity_delta > 0:
        emit_event_safe(
            db,
            event_key=INVENTORY_TRANSFER_RECEIVED,
            source_type="inventory",
            source_id=movement_id or product_id,
            payload=base,
        )

    threshold = _unusual_threshold(db, p)
    unusual = abs(quantity_delta) >= threshold or bal_qty < 0
    if mt == StockMovementType.SALE and bal_qty < 0:
        unusual = True
    if unusual and mt != StockMovementType.SALE:
        emit_event_safe(
            db,
            event_key=INVENTORY_UNUSUAL_STOCK_MOVEMENT,
            source_type="inventory",
            source_id=movement_id or product_id,
            payload=base,
        )


def emit_purchase_received(db: Session, purchase) -> None:
    from modules.payments.models import PurchaseKind

    if purchase.kind != PurchaseKind.INVENTORY:
        return
    supplier = (purchase.supplier or "مورّد").strip()
    amount = Decimal(str(purchase.amount or 0)).quantize(Decimal("0.001"))
    emit_event_safe(
        db,
        event_key=INVENTORY_PURCHASE_RECEIVED,
        source_type="purchase",
        source_id=purchase.id,
        payload={
            "purchase_id": purchase.id,
            "supplier": supplier,
            "amount": str(amount),
            "message": (
                f"تم استلام فاتورة شراء #{purchase.id} من {supplier} "
                f"بقيمة {amount} د.ل وتم تحديث المخزون."
            ),
        },
    )


def emit_purchase_needed_digest(db: Session) -> bool:
    """ملخص أصناف تحت حد إعادة الطلب — مرة واحدة يومياً."""
    from modules.inventory.service import get_main_warehouse, low_stock_products

    main = get_main_warehouse(db)
    rows = low_stock_products(db, warehouse_id=main.id)
    if not rows:
        return False
    lines: list[str] = []
    for p, qty in rows[:20]:
        rl = Decimal(str(p.reorder_level or 0)).quantize(Decimal("0.001"))
        unit = _unit_name(db, p)
        u = f" {unit}" if unit else ""
        lines.append(
            f"• {p.name_ar}: الرصيد {qty.quantize(Decimal('0.001'))}{u} / الحد {rl}{u}"
        )
    extra = ""
    if len(rows) > 20:
        extra = f"\n… و{len(rows) - 20} صنفاً آخر."
    body = (
        f"توجد {len(rows)} أصناف تحتاج شراء:\n"
        + "\n".join(lines)
        + extra
    )
    emit_event_safe(
        db,
        event_key=INVENTORY_PURCHASE_NEEDED,
        source_type="inventory_digest",
        source_id=None,
        payload={
            "message": body,
            "item_count": str(len(rows)),
            "digest_date": date.today().isoformat(),
        },
    )
    return True


def list_expired_lots(db: Session):
    from sqlalchemy import select

    from modules.catalog.models import Product
    from modules.inventory.models import InventoryLot

    today = date.today()
    lots = list(
        db.scalars(
            select(InventoryLot)
            .where(
                InventoryLot.qty_remaining > 0,
                InventoryLot.expiry_date.isnot(None),
                InventoryLot.expiry_date < today,
            )
            .order_by(InventoryLot.expiry_date.asc(), InventoryLot.id.asc())
        ).all()
    )
    if not lots:
        return []
    product_ids = {lot.product_id for lot in lots}
    products = {
        p.id: p
        for p in db.scalars(
            select(Product).where(
                Product.id.in_(product_ids),
                Product.is_active.is_(True),
                Product.expiry_tracked.is_(True),
            )
        ).all()
    }
    return [(products[lot.product_id], lot) for lot in lots if lot.product_id in products]


def emit_expiry_scan(db: Session) -> int:
    from modules.catalog.expiry import days_until_expiry, list_expiry_warning_lots

    n = 0
    for row in list_expiry_warning_lots(db):
        lot = row.lot
        product = row.product
        days = days_until_expiry(lot.expiry_date)
        if days is None or days < 0:
            continue
        emit_event_safe(
            db,
            event_key=INVENTORY_EXPIRY_WARNING,
            source_type="inventory_lot",
            source_id=lot.id,
            payload={
                "product_id": product.id,
                "product_name": product.name_ar or str(product.id),
                "lot_code": lot.lot_code or str(lot.id),
                "days_left": str(days),
                "qty": str(lot.qty_remaining.quantize(Decimal("0.001"))),
                "expiry_date": lot.expiry_date.isoformat() if lot.expiry_date else "",
                "unit": _unit_name(db, product),
            },
        )
        n += 1
    for product, lot in list_expired_lots(db):
        days = days_until_expiry(lot.expiry_date)
        emit_event_safe(
            db,
            event_key=INVENTORY_EXPIRED_ITEM,
            source_type="inventory_lot",
            source_id=lot.id,
            payload={
                "product_id": product.id,
                "product_name": product.name_ar or str(product.id),
                "lot_code": lot.lot_code or str(lot.id),
                "days_left": str(days if days is not None else 0),
                "qty": str(lot.qty_remaining.quantize(Decimal("0.001"))),
                "expiry_date": lot.expiry_date.isoformat() if lot.expiry_date else "",
                "unit": _unit_name(db, product),
            },
        )
        n += 1
    return n


def emit_recipe_cost_changed(
    db: Session,
    *,
    product_id: int,
    product_name: str,
    old_cost: Decimal,
    new_cost: Decimal,
) -> None:
    if old_cost == new_cost:
        return
    emit_event_safe(
        db,
        event_key=INVENTORY_RECIPE_COST_CHANGED,
        source_type="product",
        source_id=product_id,
        payload={
            "product_id": product_id,
            "product_name": product_name,
            "old_cost": str(old_cost.quantize(Decimal("0.001"))),
            "new_cost": str(new_cost.quantize(Decimal("0.001"))),
        },
    )


def emit_bom_missing_cost_if_needed(db: Session, product_id: int) -> None:
    from modules.catalog.bom_pricing import bom_line_costs

    rows = bom_line_costs(db, product_id)
    if not rows:
        return
    missing = [r for r in rows if r.cost_from_purchase and r.reference_unit_cost is None]
    if not missing:
        return
    from modules.catalog.models import Product

    p = db.get(Product, product_id)
    if p is None:
        return
    names = ", ".join(r.name_ar for r in missing[:5])
    extra = f" (+{len(missing) - 5})" if len(missing) > 5 else ""
    emit_event_safe(
        db,
        event_key=INVENTORY_BOM_MISSING_COST,
        source_type="product",
        source_id=product_id,
        payload={
            "product_id": product_id,
            "product_name": p.name_ar or str(product_id),
            "message": f"مكوّنات بدون تكلفة: {names}{extra}",
        },
    )
