"""فواتير مرجع التكلفة (من التركيبة) — بدون استلام مخزون."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from modules.inventory.models import InventoryLot
from modules.payments.models import Purchase, PurchaseKind, PurchaseLine

COST_REFERENCE_SUPPLIER = "مرجع تكلفة"


class CostReferenceError(Exception):
    pass


def is_cost_reference_purchase(purchase: Purchase | None) -> bool:
    if purchase is None:
        return False
    return (purchase.supplier or "").strip() == COST_REFERENCE_SUPPLIER


def purchase_lines_total(purchase: Purchase) -> Decimal:
    total = sum(
        (Decimal(str(ln.line_total or 0)) for ln in (purchase.lines or [])),
        Decimal("0"),
    )
    return total.quantize(Decimal("0.001"))


def effective_purchase_amount(purchase: Purchase) -> Decimal:
    """إجمالي العرض — يُصحَّح من البنود إن كان amount صفراً."""
    amt = Decimal(str(purchase.amount or 0)).quantize(Decimal("0.001"))
    if amt <= 0 and purchase.lines:
        return purchase_lines_total(purchase)
    return amt


def sync_purchase_amount_from_lines(purchase: Purchase) -> None:
    purchase.amount = purchase_lines_total(purchase)


def get_editable_cost_reference(db: Session, purchase_id: int) -> Purchase:
    p = db.execute(
        select(Purchase)
        .where(Purchase.id == purchase_id, Purchase.kind == PurchaseKind.INVENTORY)
        .options(selectinload(Purchase.lines))
    ).scalar_one_or_none()
    if p is None:
        raise CostReferenceError("فاتورة الشراء غير موجودة.")
    if not is_cost_reference_purchase(p):
        raise CostReferenceError("التعديل متاح لفواتير «مرجع تكلفة» فقط.")
    return p


def edit_cost_reference_lines(
    db: Session,
    purchase_id: int,
    *,
    line_costs: dict[int, Decimal],
) -> Purchase:
    purchase = get_editable_cost_reference(db, purchase_id)
    if not line_costs:
        raise CostReferenceError("لا توجد بنود للتعديل.")

    line_ids = {int(ln.id) for ln in purchase.lines}
    for line_id, raw_cost in line_costs.items():
        if int(line_id) not in line_ids:
            raise CostReferenceError(f"بند #{line_id} غير تابع لهذه الفاتورة.")
        cost = Decimal(str(raw_cost)).quantize(Decimal("0.001"))
        if cost <= 0:
            raise CostReferenceError("تكلفة الوحدة يجب أن تكون أكبر من صفر.")

    lots_by_line = {
        int(lot.purchase_line_id): lot
        for lot in db.scalars(
            select(InventoryLot).where(InventoryLot.purchase_id == purchase_id)
        ).all()
    }

    for ln in purchase.lines:
        if ln.id not in line_costs:
            continue
        cost = Decimal(str(line_costs[int(ln.id)])).quantize(Decimal("0.001"))
        ln.unit_cost = cost
        ln.line_total = (Decimal(str(ln.quantity or 1)) * cost).quantize(Decimal("0.001"))
        lot = lots_by_line.get(int(ln.id))
        if lot is not None:
            lot.unit_cost = cost

    sync_purchase_amount_from_lines(purchase)
    db.flush()
    return purchase


def parse_line_costs_from_form(form_items: list[tuple[str, str]]) -> dict[int, Decimal]:
    out: dict[int, Decimal] = {}
    prefix = "unit_cost_"
    for key, val in form_items:
        if not key.startswith(prefix):
            continue
        try:
            line_id = int(key[len(prefix) :])
        except ValueError:
            continue
        s = (val or "").strip().replace(",", ".")
        if not s:
            continue
        try:
            out[line_id] = Decimal(s)
        except InvalidOperation as exc:
            raise CostReferenceError(f"تكلفة البند #{line_id} غير صالحة.") from exc
    return out
