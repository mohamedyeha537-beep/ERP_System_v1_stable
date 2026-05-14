from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.inventory.models import StockBalance, StockMovement, StockMovementType


class InsufficientStock(Exception):
    def __init__(self, product_name: str, needed: Decimal, available: Decimal):
        self.product_name = product_name
        self.needed = needed
        self.available = available
        super().__init__(
            f"رصيد غير كافٍ للصنف «{product_name}»: المطلوب {needed} والمتاح {available}"
        )


def get_balance(db: Session, product_id: int) -> Decimal:
    row = db.get(StockBalance, product_id)
    if row is None:
        return Decimal("0")
    return row.quantity


def ensure_balance_row(db: Session, product_id: int) -> StockBalance:
    row = db.get(StockBalance, product_id)
    if row is None:
        row = StockBalance(product_id=product_id, quantity=Decimal("0"))
        db.add(row)
        db.flush()
    return row


def apply_movement(
    db: Session,
    *,
    product_id: int,
    quantity_delta: Decimal,
    movement_type: StockMovementType,
    user_id: int | None,
    sale_id: int | None = None,
    note: str | None = None,
) -> StockMovement:
    """quantity_delta: positive increases stock, negative decreases."""
    bal = ensure_balance_row(db, product_id)
    new_qty = bal.quantity + quantity_delta
    if new_qty < 0:
        from modules.catalog.models import Product

        p = db.get(Product, product_id)
        name = p.name_ar if p else str(product_id)
        raise InsufficientStock(name, abs(quantity_delta), bal.quantity)
    bal.quantity = new_qty
    mv = StockMovement(
        product_id=product_id,
        movement_type=movement_type,
        quantity=quantity_delta,
        sale_id=sale_id,
        user_id=user_id,
        note=note,
    )
    db.add(mv)
    return mv


def low_stock_products(db: Session):
    """تنبيهات نفاد المخزون — تشمل فقط مكوّنات المخزون (STOCK_ONLY)."""
    from modules.catalog.models import Product, ProductKind

    stmt = (
        select(Product, StockBalance.quantity)
        .join(StockBalance, StockBalance.product_id == Product.id, isouter=True)
        .where(Product.is_active.is_(True))
        .where(Product.kind == ProductKind.STOCK_ONLY)
    )
    rows = db.execute(stmt).all()
    out = []
    for p, qty in rows:
        q = qty or Decimal("0")
        if p.reorder_level > 0 and q <= p.reorder_level:
            out.append((p, q))
    return out


def movements_for_product(db: Session, product_id: int, limit: int = 200):
    stmt = (
        select(StockMovement)
        .where(StockMovement.product_id == product_id)
        .order_by(StockMovement.id.desc())
        .limit(limit)
    )
    return list(db.scalars(stmt).all())
