"""تسجيل تكلفة مكوّن مخزني (مرجع BAT) — بدون استلام مخزون."""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from modules.catalog.models import Product, ProductKind
from modules.catalog.service import CatalogError
from modules.inventory.lots import (
    create_lot_for_purchase_line,
    parse_lot_dates_from_form,
    receipt_batch_no_for_purchase,
    validate_lot_expiry,
)
from modules.inventory.models import InventoryLot
from modules.inventory.service import default_inventory_warehouse_id, resolve_warehouse_id
from modules.payments.models import Purchase, PurchaseKind, PurchaseLine
from modules.payments.cost_reference import COST_REFERENCE_SUPPLIER
from modules.payments.service import ensure_supplier_credit_payment_method


def parse_unit_cost(raw_cost: str) -> Decimal:
    cost_s = (raw_cost or "").strip().replace(",", ".")
    if not cost_s:
        raise CatalogError("أدخل تكلفة الوحدة.")
    try:
        unit_cost = Decimal(cost_s)
    except InvalidOperation as exc:
        raise CatalogError("تكلفة الوحدة يجب أن تكون رقماً.") from exc
    if unit_cost < 0:
        raise CatalogError("تكلفة الوحدة لا يمكن أن تكون سالبة.")
    if unit_cost <= 0:
        raise CatalogError("تكلفة الوحدة يجب أن تكون أكبر من صفر.")
    return unit_cost.quantize(Decimal("0.001"))


def record_component_cost_entry(
    db: Session,
    *,
    product_id: int,
    unit_cost: Decimal,
    user_id: int | None,
    warehouse_id: int | None = None,
    production_date: date | None = None,
    expiry_date: date | None = None,
    bom_parent_name: str | None = None,
) -> tuple[Purchase, InventoryLot]:
    """مرجع تكلفة + رقم BAT — لا يغيّر كمية المخزون ولا يُنشئ ذمماً."""
    product = db.get(Product, product_id)
    if product is None or not product.is_active:
        raise CatalogError("المكوّن غير موجود.")
    if product.kind != ProductKind.STOCK_ONLY:
        raise CatalogError("التكلفة تُسجَّل للأصناف المخزنية فقط.")

    dates = validate_lot_expiry(
        product,
        production_date=production_date,
        expiry_date=expiry_date,
    )

    try:
        wid = resolve_warehouse_id(
            db, warehouse_id if warehouse_id is not None else default_inventory_warehouse_id(db)
        )
    except Exception as exc:
        raise CatalogError(str(exc)) from exc

    pm = ensure_supplier_credit_payment_method(db)
    parent_note = (bom_parent_name or "").strip()
    note = (
        f"مرجع تكلفة — تركيبة «{parent_note}» (بدون استلام مخزون)"
        if parent_note
        else "مرجع تكلفة مكوّن (بدون استلام مخزون)"
    )

    p = Purchase(
        payment_method_id=pm.id,
        kind=PurchaseKind.INVENTORY,
        amount=unit_cost,
        supplier=COST_REFERENCE_SUPPLIER,
        note=note,
        created_by_id=user_id,
        warehouse_id=wid,
    )
    db.add(p)
    db.flush()
    p.receipt_batch_no = receipt_batch_no_for_purchase(p.id)
    db.flush()

    pl = PurchaseLine(
        purchase_id=p.id,
        product_id=product.id,
        quantity=Decimal("1"),
        unit_cost=unit_cost,
        line_total=unit_cost,
    )
    db.add(pl)
    db.flush()

    lot = create_lot_for_purchase_line(
        db,
        purchase=p,
        line=pl,
        product=product,
        warehouse_id=wid,
        line_index=1,
        production_date=dates.production_date,
        expiry_date=dates.expiry_date,
        receive_stock=False,
    )
    db.flush()
    return p, lot


def parse_component_cost_form(
    *,
    unit_cost: str,
    product: Product,
    production_date: str = "",
    expiry_date: str = "",
) -> tuple[Decimal, date | None, date | None]:
    cost = parse_unit_cost(unit_cost)
    if product.expiry_tracked:
        dates = parse_lot_dates_from_form(
            production_date,
            expiry_date,
            product_name=product.name_ar,
            required=True,
        )
        return cost, dates.production_date, dates.expiry_date
    return cost, None, None
