"""تعديل فواتير شراء المخزون — بنود ورأس الفاتورة ومزامنة المخزون والدفع."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from modules.inventory.models import StockMovementType
from modules.inventory.service import InsufficientStock, apply_movement, get_balance
from modules.payments.cost_reference import is_cost_reference_purchase
from modules.payments.models import PaymentMethod, Purchase, PurchaseKind, PurchaseLine
from modules.payments.service import (
    InventoryPurchaseLineIn,
    PaymentsError,
    assert_main_treasury_or_supplier_credit,
    is_supplier_credit_payment_method,
    sum_purchase_payments,
)


class PurchaseEditError(Exception):
    pass


def _load_inventory_purchase(db: Session, purchase_id: int) -> Purchase | None:
    return db.execute(
        select(Purchase)
        .where(Purchase.id == purchase_id, Purchase.kind == PurchaseKind.INVENTORY)
        .options(
            selectinload(Purchase.lines),
            selectinload(Purchase.payments),
            selectinload(Purchase.method),
            selectinload(Purchase.warehouse),
        )
    ).scalar_one_or_none()


def get_editable_inventory_purchase(db: Session, purchase_id: int) -> Purchase:
    purchase = _load_inventory_purchase(db, purchase_id)
    if purchase is None:
        raise PurchaseEditError("فاتورة الشراء غير موجودة.")
    if is_cost_reference_purchase(purchase):
        raise PurchaseEditError(
            "فواتير «مرجع تكلفة» تُعدَّل من صفحة عرض الفاتورة (تكلفة البنود فقط)."
        )
    return purchase


def _validate_inventory_lines(
    db: Session,
    lines: list[InventoryPurchaseLineIn],
) -> tuple[Decimal, list[InventoryPurchaseLineIn]]:
    from modules.catalog.models import Product, ProductKind
    from modules.catalog.service import CatalogError
    from modules.inventory.lots import validate_lot_expiry

    if not lines:
        raise PurchaseEditError("أضف بنداً واحداً على الأقل.")

    pids = [ln.product_id for ln in lines]
    products_by_id = {
        p.id: p
        for p in db.scalars(select(Product).where(Product.id.in_(pids))).all()
    }
    bad_products = [
        products_by_id[pid]
        for pid in pids
        if pid in products_by_id
        and products_by_id[pid].kind == ProductKind.FINAL_SELLABLE
        and not products_by_id[pid].direct_purchase_enabled
    ]
    if bad_products:
        names = [bp.name_ar for bp in bad_products]
        raise PurchaseEditError(
            "لا يمكن شراء منتجات نهائية غير مفعّل لها خيار الشراء المباشر: "
            + "، ".join(names)
            + ". فعّل «قابل للشراء والتوريد» من كرت الصنف إذا كان يُشترى جاهزاً مثل الكولا."
        )

    total = Decimal("0")
    cleaned: list[InventoryPurchaseLineIn] = []
    for ln in lines:
        product = products_by_id.get(ln.product_id)
        if product is None:
            raise PurchaseEditError("صنف غير موجود في أحد بنود الفاتورة.")
        if ln.quantity <= 0:
            raise PurchaseEditError("الكمية يجب أن تكون أكبر من صفر.")
        if ln.unit_cost < 0:
            raise PurchaseEditError("سعر الوحدة لا يمكن أن يكون سالباً.")
        try:
            dates = validate_lot_expiry(
                product,
                production_date=ln.production_date,
                expiry_date=ln.expiry_date,
            )
        except CatalogError as exc:
            raise PurchaseEditError(str(exc)) from exc
        if product.expiry_tracked and dates.expiry_date is None:
            raise PurchaseEditError(
                f"تاريخ الانتهاء مطلوب للصنف «{product.name_ar}» — فعّل الصلاحية في كرت الصنف."
            )
        qty = ln.quantity.quantize(Decimal("0.0001"))
        cost = ln.unit_cost.quantize(Decimal("0.001"))
        total += qty * cost
        cleaned.append(
            InventoryPurchaseLineIn(
                product_id=ln.product_id,
                quantity=qty,
                unit_cost=cost,
                production_date=dates.production_date,
                expiry_date=dates.expiry_date,
            )
        )
    total = total.quantize(Decimal("0.001"))
    if total <= 0:
        raise PurchaseEditError("إجمالي الفاتورة يجب أن يكون أكبر من صفر.")
    return total, cleaned


def _assert_can_reverse_stock(
    db: Session,
    *,
    lines: list[PurchaseLine],
    warehouse_id: int,
) -> None:
    from modules.catalog.models import Product

    for ln in lines:
        if ln.product_id is None:
            continue
        qty = Decimal(str(ln.quantity or 0))
        if qty <= 0:
            continue
        bal = get_balance(db, int(ln.product_id), warehouse_id)
        if bal < qty:
            product = db.get(Product, ln.product_id)
            name = product.name_ar if product else str(ln.product_id)
            raise PurchaseEditError(
                f"لا يمكن التعديل: رصيد «{name}» ({bal}) أقل من كمية الفاتورة ({qty}) — "
                "يُرجَّح أن جزءاً من الكمية بُيع أو صُرف."
            )


def _reverse_inventory_lines(
    db: Session,
    purchase: Purchase,
    *,
    warehouse_id: int,
    user_id: int | None,
) -> None:
    from modules.inventory.lots import void_lots_for_purchase

    void_lots_for_purchase(db, purchase.id)
    for ln in list(purchase.lines):
        if ln.product_id is None:
            continue
        try:
            apply_movement(
                db,
                product_id=int(ln.product_id),
                quantity_delta=-Decimal(str(ln.quantity or 0)),
                movement_type=StockMovementType.ADJUSTMENT,
                user_id=user_id,
                warehouse_id=warehouse_id,
                purchase_id=purchase.id,
                note=f"تعديل شراء فاتورة #{purchase.id}",
            )
        except InsufficientStock as exc:
            raise PurchaseEditError(str(exc)) from exc
    for ln in list(purchase.lines):
        db.delete(ln)
    db.flush()


def _apply_inventory_lines(
    db: Session,
    purchase: Purchase,
    *,
    lines: list[InventoryPurchaseLineIn],
    warehouse_id: int,
    user_id: int | None,
) -> None:
    from modules.catalog.models import Product
    from modules.inventory.lots import (
        create_lot_for_purchase_line,
        validate_lot_expiry,
    )

    products_by_id = {
        p.id: p
        for p in db.scalars(
            select(Product).where(Product.id.in_([ln.product_id for ln in lines]))
        ).all()
    }
    if not purchase.receipt_batch_no:
        from modules.inventory.lots import receipt_batch_no_for_purchase

        purchase.receipt_batch_no = receipt_batch_no_for_purchase(purchase.id)
        db.flush()

    line_no = 0
    for ln in lines:
        line_no += 1
        product = products_by_id[ln.product_id]
        line_total = (ln.quantity * ln.unit_cost).quantize(Decimal("0.001"))
        pl = PurchaseLine(
            purchase_id=purchase.id,
            product_id=ln.product_id,
            quantity=ln.quantity,
            unit_cost=ln.unit_cost,
            line_total=line_total,
        )
        db.add(pl)
        db.flush()
        dates = validate_lot_expiry(
            product,
            production_date=ln.production_date,
            expiry_date=ln.expiry_date,
        )
        create_lot_for_purchase_line(
            db,
            purchase=purchase,
            line=pl,
            product=product,
            warehouse_id=warehouse_id,
            line_index=line_no,
            production_date=dates.production_date,
            expiry_date=dates.expiry_date,
        )
        apply_movement(
            db,
            product_id=ln.product_id,
            quantity_delta=ln.quantity,
            movement_type=StockMovementType.PURCHASE,
            user_id=user_id,
            warehouse_id=warehouse_id,
            purchase_id=purchase.id,
            note=f"شراء {purchase.receipt_batch_no} — {pl.lot_code or f'#{purchase.id}'}",
        )
    db.flush()


def _sync_payment_after_edit(
    db: Session,
    purchase: Purchase,
    *,
    old_total: Decimal,
    new_total: Decimal,
    payment_method_id: int,
    pm: PaymentMethod,
) -> None:
    payments = list(purchase.payments or [])
    purchase.payment_method_id = payment_method_id
    purchase.amount = new_total

    paid = sum_purchase_payments(db, purchase.id)
    if paid > new_total:
        raise PurchaseEditError("مجموع الدفعات يتجاوز الإجمالي الجديد.")

    if is_supplier_credit_payment_method(pm):
        return

    if not payments:
        return
    if len(payments) > 1:
        raise PurchaseEditError(
            "الفاتورة لها أكثر من دفعة — لا يمكن تعديل طريقة الدفع تلقائياً. "
            "عدّل الإجمالي فقط أو راجع المحاسبة."
        )
    paid_single = Decimal(str(payments[0].amount or 0)).quantize(Decimal("0.001"))
    if paid_single != old_total.quantize(Decimal("0.001")):
        raise PurchaseEditError(
            "مبلغ الدفع لا يطابق إجمالي الفاتورة — لا يمكن تعديلها تلقائياً."
        )
    payments[0].amount = new_total
    payments[0].payment_method_id = payment_method_id
    db.flush()


def edit_inventory_purchase(
    db: Session,
    *,
    purchase_id: int,
    payment_method_id: int,
    supplier: str | None,
    supplier_phone: str | None,
    note: str | None,
    lines: list[InventoryPurchaseLineIn],
    warehouse_id: int,
    user_id: int | None,
    created_at: datetime | None = None,
    supplier_invoice_ref: str | None = None,
    invoice_image_filename: str | None = None,
    remove_invoice_image: bool = False,
) -> Purchase:
    from modules.inventory.service import WarehouseError, resolve_warehouse_id

    purchase = get_editable_inventory_purchase(db, purchase_id)
    old_total = Decimal(str(purchase.amount or 0)).quantize(Decimal("0.001"))
    old_wid = purchase.warehouse_id

    try:
        pm = assert_main_treasury_or_supplier_credit(db, payment_method_id)
    except PaymentsError as exc:
        raise PurchaseEditError(str(exc)) from exc

    try:
        new_wid = resolve_warehouse_id(db, warehouse_id)
    except WarehouseError as exc:
        raise PurchaseEditError(str(exc)) from exc

    new_total, cleaned = _validate_inventory_lines(db, lines)

    from modules.inventory.service import get_main_warehouse

    reverse_wid = int(old_wid) if old_wid is not None else get_main_warehouse(db).id
    _assert_can_reverse_stock(db, lines=list(purchase.lines), warehouse_id=reverse_wid)

    purchase.supplier = (supplier or "").strip() or None
    purchase.supplier_phone = (supplier_phone or "").strip() or None
    purchase.note = (note or "").strip() or None
    purchase.supplier_invoice_ref = (supplier_invoice_ref or "").strip() or None
    purchase.warehouse_id = new_wid
    if created_at is not None:
        purchase.created_at = created_at
    if remove_invoice_image:
        purchase.invoice_image_filename = None
    elif invoice_image_filename:
        purchase.invoice_image_filename = invoice_image_filename

    reverse_wid = int(old_wid) if old_wid is not None else new_wid
    _reverse_inventory_lines(
        db, purchase, warehouse_id=reverse_wid, user_id=user_id
    )
    _apply_inventory_lines(
        db,
        purchase,
        lines=cleaned,
        warehouse_id=new_wid,
        user_id=user_id,
    )

    _sync_payment_after_edit(
        db,
        purchase,
        old_total=old_total,
        new_total=new_total,
        payment_method_id=payment_method_id,
        pm=pm,
    )

    from modules.gl.posting import post_inventory_purchase_shadow_safe

    post_inventory_purchase_shadow_safe(db, purchase)
    return purchase
