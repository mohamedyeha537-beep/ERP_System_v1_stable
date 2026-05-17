from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.inventory.models import (
    StockBalance,
    StockMovement,
    StockMovementType,
    Warehouse,
)
from modules.settings.service import get_setting


class InsufficientStock(Exception):
    def __init__(self, product_name: str, needed: Decimal, available: Decimal):
        self.product_name = product_name
        self.needed = needed
        self.available = available
        super().__init__(
            f"رصيد غير كافٍ للصنف «{product_name}»: المطلوب {needed} والمتاح {available}"
        )


class WarehouseError(Exception):
    pass


def ensure_main_warehouse(db: Session) -> Warehouse:
    main = db.execute(
        select(Warehouse).where(Warehouse.is_main.is_(True)).limit(1)
    ).scalar_one_or_none()
    if main is not None:
        return main
    main = Warehouse(name_ar="المخزن الرئيسي", is_main=True, is_active=True, sort_order=0)
    db.add(main)
    db.flush()
    return main


def get_main_warehouse(db: Session) -> Warehouse:
    w = db.execute(
        select(Warehouse).where(Warehouse.is_main.is_(True)).limit(1)
    ).scalar_one_or_none()
    if w is None:
        return ensure_main_warehouse(db)
    return w


def get_warehouse(db: Session, warehouse_id: int) -> Warehouse | None:
    return db.get(Warehouse, warehouse_id)


def list_warehouses(db: Session, *, active_only: bool = True) -> list[Warehouse]:
    stmt = select(Warehouse).order_by(Warehouse.is_main.desc(), Warehouse.sort_order, Warehouse.name_ar)
    if active_only:
        stmt = stmt.where(Warehouse.is_active.is_(True))
    return list(db.scalars(stmt).all())


def list_branch_warehouses(db: Session) -> list[Warehouse]:
    return list(
        db.scalars(
            select(Warehouse)
            .where(Warehouse.is_main.is_(False), Warehouse.is_active.is_(True))
            .order_by(Warehouse.sort_order, Warehouse.name_ar)
        ).all()
    )


def create_branch_warehouse(db: Session, name_ar: str, *, notes: str | None = None) -> Warehouse:
    name = (name_ar or "").strip()
    if not name:
        raise WarehouseError("اسم المخزن مطلوب.")
    exists = db.execute(select(Warehouse).where(Warehouse.name_ar == name).limit(1)).scalar_one_or_none()
    if exists is not None:
        raise WarehouseError("يوجد مخزن بنفس الاسم.")
    ensure_main_warehouse(db)
    w = Warehouse(name_ar=name, is_main=False, is_active=True, notes=(notes or "").strip() or None)
    db.add(w)
    db.flush()
    return w


def update_warehouse(
    db: Session,
    warehouse_id: int,
    *,
    name_ar: str | None = None,
    is_active: bool | None = None,
    notes: str | None = None,
) -> Warehouse:
    w = db.get(Warehouse, warehouse_id)
    if w is None:
        raise WarehouseError("المخزن غير موجود.")
    if w.is_main and is_active is False:
        raise WarehouseError("لا يمكن تعطيل المخزن الرئيسي.")
    if name_ar is not None:
        name = name_ar.strip()
        if not name:
            raise WarehouseError("اسم المخزن مطلوب.")
        dup = db.execute(
            select(Warehouse).where(Warehouse.name_ar == name, Warehouse.id != w.id).limit(1)
        ).scalar_one_or_none()
        if dup is not None:
            raise WarehouseError("يوجد مخزن بنفس الاسم.")
        w.name_ar = name
    if is_active is not None:
        w.is_active = is_active
    if notes is not None:
        w.notes = notes.strip() or None
    db.flush()
    return w


def get_sales_warehouse_id(db: Session) -> int:
    raw = get_setting(db, "default_sales_warehouse_id", "")
    if raw:
        try:
            wid = int(raw)
            w = db.get(Warehouse, wid)
            if w is not None and w.is_active:
                return wid
        except (TypeError, ValueError):
            pass
    return get_main_warehouse(db).id


def resolve_warehouse_id(db: Session, warehouse_id: int | None) -> int:
    if warehouse_id is not None:
        w = db.get(Warehouse, warehouse_id)
        if w is None or not w.is_active:
            raise WarehouseError("المخزن غير صالح أو غير مفعّل.")
        return warehouse_id
    return get_sales_warehouse_id(db)


def get_balance(db: Session, product_id: int, warehouse_id: int | None = None) -> Decimal:
    wid = resolve_warehouse_id(db, warehouse_id)
    row = db.get(StockBalance, {"warehouse_id": wid, "product_id": product_id})
    if row is None:
        return Decimal("0")
    return row.quantity


def ensure_balance_row(db: Session, product_id: int, warehouse_id: int) -> StockBalance:
    row = db.get(StockBalance, {"warehouse_id": warehouse_id, "product_id": product_id})
    if row is None:
        row = StockBalance(
            warehouse_id=warehouse_id,
            product_id=product_id,
            quantity=Decimal("0"),
        )
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
    warehouse_id: int | None = None,
    sale_id: int | None = None,
    note: str | None = None,
    counterparty_warehouse_id: int | None = None,
) -> StockMovement:
    """quantity_delta: positive increases stock, negative decreases."""
    wid = resolve_warehouse_id(db, warehouse_id)
    bal = ensure_balance_row(db, product_id, wid)
    new_qty = bal.quantity + quantity_delta
    if new_qty < 0:
        from modules.catalog.models import Product

        p = db.get(Product, product_id)
        name = p.name_ar if p else str(product_id)
        raise InsufficientStock(name, abs(quantity_delta), bal.quantity)
    bal.quantity = new_qty
    mv = StockMovement(
        warehouse_id=wid,
        product_id=product_id,
        movement_type=movement_type,
        quantity=quantity_delta,
        sale_id=sale_id,
        user_id=user_id,
        note=note,
        counterparty_warehouse_id=counterparty_warehouse_id,
    )
    db.add(mv)
    return mv


def transfer_to_branch(
    db: Session,
    *,
    to_warehouse_id: int,
    lines: list[tuple[int, Decimal]],
    user_id: int | None,
    note: str | None = None,
) -> None:
    """صرف من المخزن الرئيسي إلى مخزن فرعي."""
    main = get_main_warehouse(db)
    dest = db.get(Warehouse, to_warehouse_id)
    if dest is None or not dest.is_active:
        raise WarehouseError("المخزن الفرعي غير صالح.")
    if dest.is_main:
        raise WarehouseError("اختر مخزناً فرعياً كوجهة للصرف.")
    if not lines:
        raise WarehouseError("أضف صنفاً واحداً على الأقل.")
    base_note = (note or "").strip()
    for pid, qty in lines:
        if qty <= 0:
            raise WarehouseError("الكمية يجب أن تكون أكبر من صفر.")
        line_note = f"صرف إلى {dest.name_ar}"
        if base_note:
            line_note = f"{line_note} — {base_note}"
        apply_movement(
            db,
            product_id=pid,
            quantity_delta=-qty,
            movement_type=StockMovementType.TRANSFER,
            user_id=user_id,
            warehouse_id=main.id,
            note=line_note,
            counterparty_warehouse_id=dest.id,
        )
        recv_note = f"استلام من {main.name_ar}"
        if base_note:
            recv_note = f"{recv_note} — {base_note}"
        apply_movement(
            db,
            product_id=pid,
            quantity_delta=qty,
            movement_type=StockMovementType.TRANSFER,
            user_id=user_id,
            warehouse_id=dest.id,
            note=recv_note,
            counterparty_warehouse_id=main.id,
        )


def low_stock_products(db: Session, warehouse_id: int | None = None):
    """تنبيهات نفاد المخزون — مكوّنات STOCK_ONLY في مخزن محدد."""
    from modules.catalog.models import Product, ProductKind

    wid = resolve_warehouse_id(db, warehouse_id)
    stmt = (
        select(Product, StockBalance.quantity)
        .join(
            StockBalance,
            (StockBalance.product_id == Product.id) & (StockBalance.warehouse_id == wid),
            isouter=True,
        )
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


def low_stock_by_warehouse(db: Session) -> list[tuple[Warehouse, list]]:
    """قائمة النقص لكل مخزن مفعّل."""
    out: list[tuple[Warehouse, list]] = []
    for wh in list_warehouses(db, active_only=True):
        rows = low_stock_products(db, wh.id)
        if rows:
            out.append((wh, rows))
    return out


def movements_for_product(
    db: Session, product_id: int, *, warehouse_id: int | None = None, limit: int = 200
):
    wid = resolve_warehouse_id(db, warehouse_id) if warehouse_id is not None else None
    stmt = select(StockMovement).where(StockMovement.product_id == product_id)
    if wid is not None:
        stmt = stmt.where(StockMovement.warehouse_id == wid)
    stmt = stmt.order_by(StockMovement.id.desc()).limit(limit)
    return list(db.scalars(stmt).all())
