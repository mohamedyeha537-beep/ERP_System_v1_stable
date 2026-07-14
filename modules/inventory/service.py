from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select
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
    main = Warehouse(
        name_ar="المخزن الرئيسي",
        is_main=True,
        is_active=True,
        deduct_sales_enabled=True,
        sort_order=0,
    )
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
    deduct_sales_enabled: bool | None = None,
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
    if deduct_sales_enabled is not None:
        w.deduct_sales_enabled = deduct_sales_enabled
    if notes is not None:
        w.notes = notes.strip() or None
    db.flush()
    return w


def get_sales_warehouse_id(db: Session, pos_shift_id: int | None = None) -> int:
    """مخزن خصم المبيعات — من جلسة البيع إن وُجد، وإلا الإعداد العام."""
    if pos_shift_id is not None:
        from modules.pos_shifts.models import PosShift

        sh = db.get(PosShift, int(pos_shift_id))
        if sh is not None and sh.warehouse_id is not None:
            w = db.get(Warehouse, sh.warehouse_id)
            if w is not None and w.is_active:
                return w.id
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


def list_sales_deduction_warehouses(db: Session) -> list[Warehouse]:
    """المخازن المسموح استخدامها كمصدر خصم عند البيع."""
    return list(
        db.scalars(
            select(Warehouse)
            .where(
                Warehouse.is_active.is_(True),
                Warehouse.deduct_sales_enabled.is_(True),
            )
            .order_by(Warehouse.is_main.desc(), Warehouse.sort_order, Warehouse.name_ar)
        ).all()
    )


def get_product_sales_warehouse_id(
    db: Session,
    product_id: int,
    *,
    pos_shift_id: int | None = None,
) -> int:
    """مخزن خصم صنف محدد: ربط الصنف ثم مخزن الجلسة ثم الإعداد العام."""
    from modules.catalog.models import Product

    product = db.get(Product, int(product_id))
    if product is not None and product.sales_warehouse_id is not None:
        wh = db.get(Warehouse, int(product.sales_warehouse_id))
        if wh is not None and wh.is_active and wh.deduct_sales_enabled:
            return wh.id
    return get_sales_warehouse_id(db, pos_shift_id=pos_shift_id)


def default_inventory_warehouse_id(db: Session) -> int:
    """مخزن العرض الافتراضي — نفس مخزن خصم المبيعات."""
    return get_sales_warehouse_id(db)


def resolve_warehouse_id(db: Session, warehouse_id: int | None) -> int:
    if warehouse_id is not None:
        w = db.get(Warehouse, warehouse_id)
        if w is None or not w.is_active:
            raise WarehouseError("المخزن غير صالح أو غير مفعّل.")
        return warehouse_id
    return get_sales_warehouse_id(db)


def balance_from_movements(
    db: Session, product_id: int, warehouse_id: int | None = None
) -> Decimal:
    """مجموع حركات المخزون — المصدر الموثوق للرصيد."""
    wid = resolve_warehouse_id(db, warehouse_id)
    total = db.scalar(
        select(func.coalesce(func.sum(StockMovement.quantity), 0)).where(
            StockMovement.product_id == product_id,
            StockMovement.warehouse_id == wid,
        )
    )
    return Decimal(str(total or 0))


def reconcile_stock_balance(
    db: Session, product_id: int, warehouse_id: int
) -> tuple[Decimal, bool]:
    correct = balance_from_movements(db, product_id, warehouse_id)
    row = ensure_balance_row(db, product_id, warehouse_id)
    changed = row.quantity != correct
    if changed:
        row.quantity = correct
    return correct, changed


def reconcile_all_stock_balances(db: Session) -> int:
    """مزامنة جدول الأرصدة مع مجموع الحركات (يُصلح بيانات قديمة أو مستوردة)."""
    rows = db.execute(
        select(
            StockMovement.warehouse_id,
            StockMovement.product_id,
            func.coalesce(func.sum(StockMovement.quantity), 0),
        ).group_by(StockMovement.warehouse_id, StockMovement.product_id)
    ).all()
    movement_totals: dict[tuple[int, int], Decimal] = {}
    for wid, pid, total in rows:
        if wid is None or pid is None:
            continue
        movement_totals[(int(wid), int(pid))] = Decimal(str(total or 0))

    changed = 0
    for (wid, pid), total in movement_totals.items():
        row = ensure_balance_row(db, pid, wid)
        if row.quantity != total:
            row.quantity = total
            changed += 1

    for row in db.scalars(select(StockBalance)).all():
        key = (row.warehouse_id, row.product_id)
        if key not in movement_totals and row.quantity != 0:
            row.quantity = Decimal("0")
            changed += 1

    db.flush()
    return changed


def get_balance(db: Session, product_id: int, warehouse_id: int | None = None) -> Decimal:
    """الرصيد الحالي — مجموع حركات المخزون (المصدر الموثوق)."""
    return balance_from_movements(db, product_id, warehouse_id)


def is_low_stock(quantity: Decimal, reorder_level: Decimal) -> bool:
    """هل الرصيد منخفض؟ سالب = دائماً تنبيه؛ وإلا مقارنة بحد التنبيه من صفحة الصنف."""
    qty = Decimal(str(quantity or 0))
    rl = Decimal(str(reorder_level or 0))
    if qty < 0:
        return True
    if rl <= 0:
        return False
    return qty <= rl


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
    purchase_id: int | None = None,
    reason_label: str | None = None,
    note: str | None = None,
    counterparty_warehouse_id: int | None = None,
    transfer_id: int | None = None,
) -> StockMovement:
    """quantity_delta: positive increases stock, negative decreases."""
    wid = resolve_warehouse_id(db, warehouse_id)
    bal = ensure_balance_row(db, product_id, wid)
    old_qty = Decimal(str(bal.quantity or 0))
    new_qty = old_qty + quantity_delta
    # البيع: يُسمح بالرصيد السالب حتى يُدخل الموظف المشتريات لاحقاً.
    # الحركات الموجبة مثل مرتجع البيع يجب أن تُقبل لأنها تحسن الرصيد حتى لو بقي سالباً.
    if quantity_delta < 0 and new_qty < 0 and movement_type != StockMovementType.SALE:
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
        purchase_id=purchase_id,
        reason_label=(reason_label or "").strip() or None,
        user_id=user_id,
        note=note,
        counterparty_warehouse_id=counterparty_warehouse_id,
        transfer_id=transfer_id,
    )
    db.add(mv)
    db.flush()
    if quantity_delta < 0:
        from modules.inventory.lots import allocate_lots_fifo, record_lot_consumptions

        allocations = allocate_lots_fifo(
            db,
            product_id=product_id,
            warehouse_id=wid,
            quantity=abs(quantity_delta),
        )
        record_lot_consumptions(db, stock_movement_id=mv.id, allocations=allocations)
    elif quantity_delta > 0 and movement_type == StockMovementType.SALE_RETURN and sale_id:
        from modules.inventory.lots import restore_lots_for_sale_return

        restore_lots_for_sale_return(
            db,
            sale_id=sale_id,
            product_id=product_id,
            warehouse_id=wid,
            quantity=quantity_delta,
        )
    reconcile_stock_balance(db, product_id, wid)
    _maybe_notify_inventory_movement(
        db, movement_type, purchase_id=purchase_id, note=note, product_id=product_id
    )
    try:
        from modules.notifications.inventory_hooks import emit_inventory_after_movement

        emit_inventory_after_movement(
            db,
            product_id=product_id,
            warehouse_id=wid,
            quantity_delta=quantity_delta,
            movement_type=movement_type,
            old_qty=old_qty,
            movement_id=mv.id,
            note=note,
            reason_label=reason_label,
        )
    except Exception:  # noqa: BLE001
        pass
    return mv


def _maybe_notify_inventory_movement(
    db: Session,
    movement_type: StockMovementType,
    *,
    purchase_id: int | None,
    note: str | None,
    product_id: int,
) -> None:
    """إشعار المخزون — بدون خصم البيع العادي (كثير جداً)."""
    if movement_type == StockMovementType.SALE:
        return
    if movement_type == StockMovementType.PURCHASE and purchase_id is not None:
        return
    notify_types = {
        StockMovementType.PURCHASE,
        StockMovementType.ADJUSTMENT,
        StockMovementType.TRANSFER,
        StockMovementType.WASTE,
        StockMovementType.SHORTAGE,
        StockMovementType.SALE_RETURN,
    }
    if movement_type not in notify_types:
        return
    from modules.dashboard_notify.constants import INVENTORY
    from modules.dashboard_notify.service import record_activity

    record_activity(
        db,
        INVENTORY,
        event_type=movement_type.value.lower(),
        ref_id=product_id,
        note=(note or "").strip()[:255] or movement_type.value,
    )


def transfer_to_branch(
    db: Session,
    *,
    to_warehouse_id: int,
    lines: list[tuple[int, Decimal]],
    user_id: int | None,
    note: str | None = None,
) -> "WarehouseTransfer":
    """صرف من المخزن الرئيسي — يُنشئ مستنداً بانتظار مصادقة المستلم."""
    from modules.inventory.transfer_service import create_transfer_request

    return create_transfer_request(
        db,
        to_warehouse_id=to_warehouse_id,
        lines=lines,
        user_id=user_id,
        note=note,
    )


def low_stock_products(db: Session, warehouse_id: int | None = None):
    """تنبيهات نفاد المخزون — الأصناف ذات الرصيد المباشر في مخزن محدد."""
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
        .where(
            (Product.kind == ProductKind.STOCK_ONLY)
            | (
                (Product.kind == ProductKind.FINAL_SELLABLE)
                & Product.direct_purchase_enabled.is_(True)
            )
        )
    )
    rows = db.execute(stmt).all()
    out = []
    for p, _qty in rows:
        q = get_balance(db, p.id, wid)
        if is_low_stock(q, p.reorder_level):
            out.append((p, q))
    return out


def low_stock_for_warehouses(
    db: Session, warehouse_ids: list[int] | None = None
) -> list[tuple[Warehouse, list]]:
    """قائمة النقص لمخازن محددة (أو كل المخازن النشطة)."""
    if warehouse_ids is None:
        return low_stock_by_warehouse(db)
    out: list[tuple[Warehouse, list]] = []
    for wid in warehouse_ids:
        wh = db.get(Warehouse, wid)
        if wh is None or not wh.is_active:
            continue
        rows = low_stock_products(db, wid)
        if rows:
            out.append((wh, rows))
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
