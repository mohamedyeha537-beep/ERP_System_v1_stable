"""تكلفة المخزون FIFO — استهلاك الدفعات وتقارير COGS."""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from modules.inventory.models import InventoryLot, InventoryLotConsumption, StockMovement, StockMovementType


def movement_consumption_cost(db: Session, movement_id: int) -> Decimal:
    total = db.scalar(
        select(func.coalesce(func.sum(InventoryLotConsumption.line_total), 0)).where(
            InventoryLotConsumption.stock_movement_id == movement_id
        )
    )
    return Decimal(str(total or 0)).quantize(Decimal("0.001"))


def sale_fifo_cogs(db: Session, sale_id: int) -> Decimal | None:
    """مجموع تكلفة FIFO الفعلية لحركات بيع مرتبطة بالفاتورة."""
    movement_ids = list(
        db.scalars(
            select(StockMovement.id).where(
                StockMovement.sale_id == sale_id,
                StockMovement.quantity < 0,
            )
        ).all()
    )
    if not movement_ids:
        return None
    total = db.scalar(
        select(func.coalesce(func.sum(InventoryLotConsumption.line_total), 0)).where(
            InventoryLotConsumption.stock_movement_id.in_(movement_ids)
        )
    )
    cost = Decimal(str(total or 0)).quantize(Decimal("0.001"))
    return cost if cost > 0 else None


def fifo_unit_cost_map(db: Session) -> dict[int, Decimal]:
    """تكلفة الوحدة التالية لكل صنف — من أقدم دفعة (FIFO)."""
    from modules.inventory.lots import fifo_next_unit_cost

    product_ids = list(db.scalars(select(InventoryLot.product_id).distinct()).all())
    out: dict[int, Decimal] = {}
    for pid in product_ids:
        if pid is None:
            continue
        cost = fifo_next_unit_cost(db, int(pid))
        if cost is not None:
            out[int(pid)] = cost
    return out
