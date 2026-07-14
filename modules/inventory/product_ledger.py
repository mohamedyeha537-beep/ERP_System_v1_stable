"""تتبع حركة الصنف — سجل المخزون، الجرد، والهدر."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from modules.catalog.models import BillOfMaterialsLine, Product, ProductKind
from modules.inventory.models import StockMovement, StockMovementType
from modules.inventory.service import (
    InsufficientStock,
    apply_movement,
    get_balance,
    resolve_warehouse_id,
)
from modules.sales.models import Sale, SaleLine


class LedgerError(Exception):
    pass


MOVEMENT_LABELS: dict[StockMovementType, str] = {
    StockMovementType.SALE: "خصم بيع",
    StockMovementType.SALE_RETURN: "مرتجع بيع",
    StockMovementType.PURCHASE: "شراء / إدخال",
    StockMovementType.ADJUSTMENT: "تسوية",
    StockMovementType.TRANSFER: "تحويل مخزن",
    StockMovementType.ONLINE_SYNC: "مزامنة أونلاين",
    StockMovementType.WASTE: "هدر / هالك",
    StockMovementType.SHORTAGE: "عجز مخزني (جرد)",
}


@dataclass
class LedgerRow:
    id: int
    created_at: datetime
    movement_type: StockMovementType
    type_label: str
    quantity: Decimal
    balance_after: Decimal
    note: str | None
    reason_label: str | None
    detail: str | None
    sale_id: int | None
    purchase_id: int | None
    warehouse_name: str | None


def movement_type_label(mt: StockMovementType) -> str:
    return MOVEMENT_LABELS.get(mt, mt.value)


def _sale_detail_for_component(db: Session, sale_id: int, component_id: int) -> str | None:
    sale = db.execute(
        select(Sale)
        .where(Sale.id == sale_id)
        .options(selectinload(Sale.lines).selectinload(SaleLine.product))
    ).scalar_one_or_none()
    if sale is None:
        return None
    parent_ids = {ln.product_id for ln in sale.lines}
    if not parent_ids:
        return None
    bom_rows = db.execute(
        select(BillOfMaterialsLine.parent_product_id).where(
            BillOfMaterialsLine.component_product_id == component_id,
            BillOfMaterialsLine.parent_product_id.in_(parent_ids),
        )
    ).all()
    bom_parent_ids = {r[0] for r in bom_rows}
    if not bom_parent_ids:
        return f"طلب بيع #{sale_id}"
    parts: list[str] = []
    for ln in sale.lines:
        if ln.product_id in bom_parent_ids and ln.product:
            parts.append(f"{ln.product.name_ar} ×{ln.quantity}")
    if not parts:
        return f"طلب بيع #{sale_id}"
    return f"طلب #{sale_id} — " + "، ".join(parts)


def build_product_ledger(
    db: Session,
    product_id: int,
    *,
    warehouse_id: int | None = None,
    limit: int = 300,
) -> list[LedgerRow]:
    from modules.inventory.models import Warehouse

    stmt = (
        select(StockMovement)
        .where(StockMovement.product_id == product_id)
        .order_by(StockMovement.created_at.asc(), StockMovement.id.asc())
    )
    if warehouse_id is not None:
        stmt = stmt.where(StockMovement.warehouse_id == warehouse_id)
    movements = list(db.scalars(stmt).all())
    prefix = Decimal("0")
    if len(movements) > limit:
        prefix = sum((mv.quantity for mv in movements[:-limit]), Decimal("0"))
        movements = movements[-limit:]

    wh_names: dict[int, str] = {}
    running = prefix
    rows: list[LedgerRow] = []
    for mv in movements:
        running += mv.quantity
        wh_name = None
        if mv.warehouse_id:
            if mv.warehouse_id not in wh_names:
                w = db.get(Warehouse, mv.warehouse_id)
                wh_names[mv.warehouse_id] = w.name_ar if w else ""
            wh_name = wh_names.get(mv.warehouse_id)
        detail = None
        if mv.movement_type == StockMovementType.SALE and mv.sale_id:
            detail = _sale_detail_for_component(db, mv.sale_id, product_id)
        elif mv.movement_type == StockMovementType.PURCHASE and mv.purchase_id:
            detail = f"فاتورة شراء #{mv.purchase_id}"
        elif mv.note:
            detail = mv.note
        rows.append(
            LedgerRow(
                id=mv.id,
                created_at=mv.created_at,
                movement_type=mv.movement_type,
                type_label=movement_type_label(mv.movement_type),
                quantity=mv.quantity,
                balance_after=running,
                note=mv.note,
                reason_label=mv.reason_label,
                detail=detail,
                sale_id=mv.sale_id,
                purchase_id=mv.purchase_id,
                warehouse_name=wh_name,
            )
        )
    rows.reverse()
    return rows


def bom_usage_for_component(db: Session, component_id: int) -> list[tuple[Product, Decimal]]:
    """منتجات نهائية تستخدم هذا المكوّن في تركيبتها."""
    stmt = (
        select(Product, BillOfMaterialsLine.qty_per_parent)
        .join(BillOfMaterialsLine, BillOfMaterialsLine.parent_product_id == Product.id)
        .where(
            BillOfMaterialsLine.component_product_id == component_id,
            Product.kind == ProductKind.FINAL_SELLABLE,
            Product.is_active.is_(True),
        )
        .order_by(Product.name_ar)
    )
    return list(db.execute(stmt).all())


def apply_stock_count(
    db: Session,
    *,
    product_id: int,
    warehouse_id: int | None,
    counted_qty: Decimal,
    user_id: int | None,
) -> StockMovement | None:
    if counted_qty < 0:
        raise LedgerError("الكمية المعدودة لا يمكن أن تكون سالبة.")
    wid = resolve_warehouse_id(db, warehouse_id)
    current = get_balance(db, product_id, wid)
    diff = counted_qty - current
    if diff == 0:
        return None
    if diff < 0:
        return apply_movement(
            db,
            product_id=product_id,
            quantity_delta=diff,
            movement_type=StockMovementType.SHORTAGE,
            user_id=user_id,
            warehouse_id=wid,
            note=f"جرد: الدفتري {current} — المعدود {counted_qty} (عجز {abs(diff)})",
            reason_label="عجز جرد",
        )
    return apply_movement(
        db,
        product_id=product_id,
        quantity_delta=diff,
        movement_type=StockMovementType.ADJUSTMENT,
        user_id=user_id,
        warehouse_id=wid,
        note=f"جرد: الدفتري {current} — المعدود {counted_qty} (زيادة {diff})",
        reason_label="زيادة جرد",
    )


def apply_waste(
    db: Session,
    *,
    product_id: int,
    warehouse_id: int | None,
    quantity: Decimal,
    reason_label: str,
    user_id: int | None,
    extra_note: str | None = None,
) -> StockMovement:
    qty = quantity
    if qty <= 0:
        raise LedgerError("كمية الهدر يجب أن تكون أكبر من صفر.")
    reason = (reason_label or "").strip()
    if not reason:
        raise LedgerError("اختر سبب الهدر.")
    wid = resolve_warehouse_id(db, warehouse_id)
    note_parts = [f"هدر: {reason}"]
    if (extra_note or "").strip():
        note_parts.append(extra_note.strip())
    try:
        return apply_movement(
            db,
            product_id=product_id,
            quantity_delta=-qty,
            movement_type=StockMovementType.WASTE,
            user_id=user_id,
            warehouse_id=wid,
            reason_label=reason,
            note=" — ".join(note_parts),
        )
    except InsufficientStock as e:
        raise LedgerError(str(e)) from e
