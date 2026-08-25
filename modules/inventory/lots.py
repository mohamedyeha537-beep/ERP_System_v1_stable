"""دفعات المخزون (Lot/Batch) — FIFO: أول وارد أول صادر للتكلفة."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from modules.catalog.expiry import parse_optional_date
from modules.catalog.models import Product
from modules.catalog.service import CatalogError
from modules.inventory.models import InventoryLot, InventoryLotConsumption, StockMovement
from modules.payments.models import Purchase, PurchaseLine


def receipt_batch_no_for_purchase(purchase_id: int) -> str:
    return f"GR-{purchase_id:06d}"


def lot_code_for_line(receipt_batch_no: str, line_index: int) -> str:
    return f"{receipt_batch_no}-L{line_index:02d}"


@dataclass(frozen=True)
class LotExpiryInput:
    production_date: date | None
    expiry_date: date | None


@dataclass(frozen=True)
class LotAllocation:
    lot_id: int
    quantity: Decimal
    unit_cost: Decimal


def validate_lot_expiry(
    product: Product,
    *,
    production_date: date | None,
    expiry_date: date | None,
) -> LotExpiryInput:
    if not product.expiry_tracked:
        return LotExpiryInput(None, None)
    if expiry_date is None:
        raise CatalogError(
            f"تاريخ الانتهاء مطلوب للصنف «{product.name_ar}» — الصنف مفعّل لتتبع الصلاحية."
        )
    if production_date is not None and expiry_date < production_date:
        raise CatalogError(
            f"تاريخ الانتهاء للصنف «{product.name_ar}» يجب أن يكون بعد تاريخ الإنتاج."
        )
    return LotExpiryInput(production_date, expiry_date)


def parse_lot_dates_from_form(
    production_raw: str | None,
    expiry_raw: str | None,
    *,
    product_name: str,
    required: bool,
) -> LotExpiryInput:
    prod = parse_optional_date(production_raw, field_label=f"تاريخ الإنتاج ({product_name})")
    exp = parse_optional_date(expiry_raw, field_label=f"تاريخ الانتهاء ({product_name})")
    if required and exp is None:
        raise CatalogError(f"تاريخ الانتهاء مطلوب للصنف «{product_name}».")
    if prod is not None and exp is not None and exp < prod:
        raise CatalogError(f"تاريخ الانتهاء للصنف «{product_name}» يجب أن يكون بعد الإنتاج.")
    return LotExpiryInput(prod, exp)


def create_lot_for_purchase_line(
    db: Session,
    *,
    purchase: Purchase,
    line: PurchaseLine,
    product: Product,
    warehouse_id: int,
    line_index: int,
    production_date: date | None,
    expiry_date: date | None,
    receive_stock: bool = True,
) -> InventoryLot:
    """إنشاء دفعة مخزون لكل بند شراء — للصلاحية ولتتبع التكلفة برقم BAT."""
    batch = purchase.receipt_batch_no or receipt_batch_no_for_purchase(purchase.id)
    code = lot_code_for_line(batch, line_index)
    line.lot_code = code
    if product.expiry_tracked:
        line.production_date = production_date
        line.expiry_date = expiry_date
    stock_qty = line.quantity if receive_stock else Decimal("0")
    lot = InventoryLot(
        lot_code=code,
        purchase_id=purchase.id,
        purchase_line_id=line.id,
        product_id=product.id,
        warehouse_id=warehouse_id,
        production_date=production_date if product.expiry_tracked else None,
        expiry_date=expiry_date if product.expiry_tracked else None,
        qty_received=stock_qty,
        qty_remaining=stock_qty,
        unit_cost=line.unit_cost,
    )
    db.add(lot)
    db.flush()
    return lot


def list_fifo_lots(
    db: Session,
    *,
    product_id: int,
    warehouse_id: int | None = None,
) -> list[InventoryLot]:
    """دفعات بكمية متبقية — FEFO: الأقرب صلاحيةً ثم الأقدم استلاماً."""
    today = date.today()
    stmt = (
        select(InventoryLot)
        .where(
            InventoryLot.product_id == product_id,
            InventoryLot.qty_remaining > 0,
            InventoryLot.expiry_date.is_(None) | (InventoryLot.expiry_date >= today),
        )
        .order_by(
            func.coalesce(InventoryLot.expiry_date, date(9999, 12, 31)).asc(),
            InventoryLot.id.asc(),
        )
    )
    if warehouse_id is not None:
        stmt = stmt.where(InventoryLot.warehouse_id == warehouse_id)
    return list(db.scalars(stmt).all())


def list_active_lots(
    db: Session,
    *,
    product_id: int,
    warehouse_id: int | None = None,
) -> list[InventoryLot]:
    return list_fifo_lots(db, product_id=product_id, warehouse_id=warehouse_id)


def allocate_lots_fifo(
    db: Session,
    *,
    product_id: int,
    warehouse_id: int,
    quantity: Decimal,
) -> list[LotAllocation]:
    """خصم كمية من الدفعات بالترتيب FIFO — يُرجع تفاصيل الاستهلاك."""
    if quantity <= 0:
        return []
    remaining = quantity
    allocations: list[LotAllocation] = []
    for lot in list_fifo_lots(db, product_id=product_id, warehouse_id=warehouse_id):
        if remaining <= 0:
            break
        take = min(lot.qty_remaining, remaining)
        lot.qty_remaining = (lot.qty_remaining - take).quantize(Decimal("0.0001"))
        cost = Decimal(str(lot.unit_cost or 0)).quantize(Decimal("0.001"))
        allocations.append(LotAllocation(lot_id=lot.id, quantity=take, unit_cost=cost))
        remaining -= take
    db.flush()
    return allocations


def record_lot_consumptions(
    db: Session,
    *,
    stock_movement_id: int,
    allocations: list[LotAllocation],
) -> list[InventoryLotConsumption]:
    rows: list[InventoryLotConsumption] = []
    for alloc in allocations:
        if alloc.quantity <= 0:
            continue
        line_total = (alloc.quantity * alloc.unit_cost).quantize(Decimal("0.001"))
        row = InventoryLotConsumption(
            stock_movement_id=stock_movement_id,
            inventory_lot_id=alloc.lot_id,
            quantity=alloc.quantity,
            unit_cost=alloc.unit_cost,
            line_total=line_total,
        )
        db.add(row)
        rows.append(row)
    db.flush()
    return rows


def restore_lots_for_sale_return(
    db: Session,
    *,
    sale_id: int,
    product_id: int,
    warehouse_id: int,
    quantity: Decimal,
) -> None:
    """إعادة كمية مرتجعة إلى الدفعات — عكس FIFO."""
    if quantity <= 0 or sale_id is None:
        return
    remaining = quantity
    consumptions = list(
        db.scalars(
            select(InventoryLotConsumption)
            .join(StockMovement, StockMovement.id == InventoryLotConsumption.stock_movement_id)
            .where(
                StockMovement.sale_id == sale_id,
                StockMovement.product_id == product_id,
                StockMovement.warehouse_id == warehouse_id,
                StockMovement.quantity < 0,
            )
            .order_by(InventoryLotConsumption.id.desc())
        ).all()
    )
    for cons in consumptions:
        if remaining <= 0:
            break
        lot = db.get(InventoryLot, cons.inventory_lot_id)
        if lot is None:
            continue
        max_restore = min(remaining, cons.quantity)
        lot.qty_remaining = (lot.qty_remaining + max_restore).quantize(Decimal("0.0001"))
        remaining -= max_restore
    if remaining > 0:
        lots = list_fifo_lots(db, product_id=product_id, warehouse_id=warehouse_id)
        if lots:
            lots[-1].qty_remaining = (lots[-1].qty_remaining + remaining).quantize(
                Decimal("0.0001")
            )
    db.flush()


def deduct_lots_fefo(
    db: Session,
    *,
    product_id: int,
    warehouse_id: int,
    quantity: Decimal,
) -> None:
    allocate_lots_fifo(db, product_id=product_id, warehouse_id=warehouse_id, quantity=quantity)


def void_lots_for_purchase(db: Session, purchase_id: int) -> None:
    for lot in db.scalars(
        select(InventoryLot).where(InventoryLot.purchase_id == purchase_id)
    ).all():
        lot.qty_remaining = Decimal("0")
    db.flush()


def fifo_next_unit_cost(
    db: Session,
    product_id: int,
    *,
    warehouse_id: int | None = None,
) -> Decimal | None:
    lots = list_fifo_lots(db, product_id=product_id, warehouse_id=warehouse_id)
    if not lots:
        return None
    return Decimal(str(lots[0].unit_cost or 0)).quantize(Decimal("0.001"))
