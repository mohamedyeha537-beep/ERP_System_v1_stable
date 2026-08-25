"""تكلفة المخزون FIFO — استهلاك الدفعات وتقارير COGS."""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from modules.inventory.models import InventoryLot, InventoryLotConsumption, StockMovement


def movement_consumption_cost(db: Session, movement_id: int) -> Decimal:
    total = db.scalar(
        select(func.coalesce(func.sum(InventoryLotConsumption.line_total), 0)).where(
            InventoryLotConsumption.stock_movement_id == movement_id
        )
    )
    return Decimal(str(total or 0)).quantize(Decimal("0.001"))


def _movement_allocated_qty(db: Session, movement_id: int) -> Decimal:
    total = db.scalar(
        select(func.coalesce(func.sum(InventoryLotConsumption.quantity), 0)).where(
            InventoryLotConsumption.stock_movement_id == movement_id
        )
    )
    return Decimal(str(total or 0)).quantize(Decimal("0.0001"))


def fallback_unit_cost(
    db: Session,
    product_id: int,
    *,
    warehouse_id: int | None = None,
    avg_costs: dict[int, Decimal] | None = None,
) -> Decimal:
    """تكلفة احتياطية عند بيع بلا دفعات — reference → متوسط → FIFO."""
    from modules.catalog.models import Product

    prod = db.get(Product, int(product_id))
    if prod is not None and prod.reference_unit_cost:
        ref = Decimal(str(prod.reference_unit_cost)).quantize(Decimal("0.001"))
        if ref > Decimal("0"):
            return ref
    if avg_costs is None:
        from modules.reporting.queries import avg_unit_cost_per_product

        avg_costs = avg_unit_cost_per_product(db)
    ac = avg_costs.get(int(product_id), Decimal("0"))
    if ac > Decimal("0"):
        return ac
    from modules.inventory.lots import fifo_next_unit_cost

    nxt = fifo_next_unit_cost(db, int(product_id), warehouse_id=warehouse_id)
    return nxt if nxt is not None else Decimal("0")


def sale_fifo_cogs(db: Session, sale_id: int) -> Decimal | None:
    """تكلفة FIFO + تكلفة احتياطية للكمية غير المغطاة بدفعات."""
    movements = list(
        db.scalars(
            select(StockMovement).where(
                StockMovement.sale_id == sale_id,
                StockMovement.quantity < 0,
            )
        ).all()
    )
    if not movements:
        return None
    from modules.reporting.queries import avg_unit_cost_per_product

    avg_costs = avg_unit_cost_per_product(db)
    total = Decimal("0")
    for mv in movements:
        total += movement_consumption_cost(db, mv.id)
        sold_qty = abs(Decimal(str(mv.quantity or 0))).quantize(Decimal("0.0001"))
        allocated = _movement_allocated_qty(db, mv.id)
        uncovered = (sold_qty - allocated).quantize(Decimal("0.0001"))
        if uncovered > Decimal("0"):
            unit = fallback_unit_cost(
                db,
                int(mv.product_id),
                warehouse_id=int(mv.warehouse_id) if mv.warehouse_id else None,
                avg_costs=avg_costs,
            )
            total += (uncovered * unit).quantize(Decimal("0.001"))
    return total.quantize(Decimal("0.001"))


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
