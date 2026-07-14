"""تعديل فواتير الأصول/الأدوات — بنود ورأس الفاتورة ومزامنة الدفع."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from modules.payments.models import PaymentMethod, Purchase, PurchaseKind, PurchaseLine
from modules.payments.service import sum_purchase_payments


class AssetEditError(Exception):
    pass


AssetLineInput = tuple[str, str | None, Decimal, Decimal, int, Decimal]


def _load_asset_purchase(db: Session, purchase_id: int) -> Purchase | None:
    return db.execute(
        select(Purchase)
        .where(Purchase.id == purchase_id, Purchase.kind == PurchaseKind.ASSET)
        .options(
            selectinload(Purchase.lines),
            selectinload(Purchase.payments),
            selectinload(Purchase.method),
        )
    ).scalar_one_or_none()


def get_editable_asset_purchase(db: Session, purchase_id: int) -> Purchase:
    purchase = _load_asset_purchase(db, purchase_id)
    if purchase is None:
        raise AssetEditError("فاتورة الأصول غير موجودة.")
    return purchase


def _validate_asset_lines(
    lines: list[AssetLineInput],
) -> tuple[Decimal, list[AssetLineInput]]:
    if not lines:
        raise AssetEditError("أضف بنداً واحداً على الأقل.")
    total = Decimal("0")
    cleaned: list[AssetLineInput] = []
    for name, unit, qty, cost, life, salvage in lines:
        nm = (name or "").strip()
        if not nm:
            raise AssetEditError("اكتب اسم الأداة/الأصل لكل بند.")
        if qty <= 0:
            raise AssetEditError("الكمية يجب أن تكون أكبر من صفر.")
        if cost < 0:
            raise AssetEditError("سعر الوحدة لا يمكن أن يكون سالباً.")
        if life < 0:
            raise AssetEditError("العمر الإنتاجي لا يمكن أن يكون سالباً.")
        if salvage < 0:
            raise AssetEditError("قيمة الخردة لا يمكن أن تكون سالبة.")
        line_total = (qty * cost).quantize(Decimal("0.001"))
        if salvage > line_total:
            raise AssetEditError(
                f"قيمة الخردة لـ «{nm}» ({salvage}) لا يمكن أن تتجاوز إجمالي البند ({line_total})."
            )
        u = (unit or "").strip() or None
        total += line_total
        cleaned.append((nm, u, qty, cost, int(life), salvage))
    total = total.quantize(Decimal("0.001"))
    if total <= 0:
        raise AssetEditError("إجمالي الفاتورة يجب أن يكون أكبر من صفر.")
    return total, cleaned


def _sync_payment_after_edit(
    db: Session,
    purchase: Purchase,
    *,
    old_total: Decimal,
    new_total: Decimal,
    payment_method_id: int,
) -> None:
    payments = list(purchase.payments or [])
    purchase.payment_method_id = payment_method_id
    purchase.amount = new_total
    if not payments:
        return
    if len(payments) > 1:
        raise AssetEditError(
            "الفاتورة لها أكثر من دفعة — لا يمكن تعديلها تلقائياً. راجع المحاسبة."
        )
    paid = Decimal(str(payments[0].amount or 0)).quantize(Decimal("0.001"))
    if paid != old_total.quantize(Decimal("0.001")):
        raise AssetEditError(
            "مبلغ الدفع لا يطابق إجمالي الفاتورة — لا يمكن تعديلها تلقائياً."
        )
    payments[0].amount = new_total
    payments[0].payment_method_id = payment_method_id
    db.flush()


def edit_asset_purchase(
    db: Session,
    *,
    purchase_id: int,
    payment_method_id: int,
    supplier: str | None,
    note: str | None,
    lines: list[AssetLineInput],
    user_id: int | None,
    created_at: datetime | None = None,
    supplier_invoice_ref: str | None = None,
    invoice_image_filename: str | None = None,
    remove_invoice_image: bool = False,
    reason: str | None = None,
) -> Purchase:
    purchase = get_editable_asset_purchase(db, purchase_id)
    old_total = Decimal(str(purchase.amount or 0)).quantize(Decimal("0.001"))

    pm = db.get(PaymentMethod, payment_method_id)
    if pm is None or not pm.is_active:
        raise AssetEditError("أسلوب الدفع غير صالح.")
    if not pm.can_pay:
        raise AssetEditError(f"الحساب «{pm.name_ar}» غير مسموح بالصرف.")

    new_total, cleaned = _validate_asset_lines(lines)

    purchase.supplier = (supplier or "").strip() or None
    purchase.note = (note or "").strip() or None
    purchase.supplier_invoice_ref = (supplier_invoice_ref or "").strip() or None
    if created_at is not None:
        purchase.created_at = created_at
    if remove_invoice_image:
        purchase.invoice_image_filename = None
    elif invoice_image_filename:
        purchase.invoice_image_filename = invoice_image_filename

    for line in list(purchase.lines):
        db.delete(line)
    db.flush()

    for name, unit, qty, cost, life, salvage in cleaned:
        db.add(
            PurchaseLine(
                purchase_id=purchase.id,
                product_id=None,
                item_name=name,
                unit=unit,
                quantity=qty.quantize(Decimal("0.0001")),
                unit_cost=cost.quantize(Decimal("0.001")),
                line_total=(qty * cost).quantize(Decimal("0.001")),
                useful_life_months=life,
                salvage_value=salvage.quantize(Decimal("0.001")),
            )
        )
    db.flush()

    _sync_payment_after_edit(
        db,
        purchase,
        old_total=old_total,
        new_total=new_total,
        payment_method_id=payment_method_id,
    )

    paid = sum_purchase_payments(db, purchase.id)
    if paid > new_total:
        raise AssetEditError("مجموع الدفعات يتجاوز الإجمالي الجديد.")

    from modules.dashboard_notify.constants import ASSETS
    from modules.dashboard_notify.service import resolve_activity

    note_bits = [f"أصول #{purchase.id}"]
    if reason:
        note_bits.append(reason.strip()[:120])
    resolve_activity(db, ASSETS, ref_id=purchase.id)
    return purchase
