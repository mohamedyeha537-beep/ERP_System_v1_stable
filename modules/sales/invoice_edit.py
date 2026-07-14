"""تعديل فواتير مكتملة — الكمية والسعر مع انعكاس على المخزون والسجلات."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from modules.catalog.bom_explosion import merge_line_requirements
from modules.catalog.models import Product
from modules.customers.models import Customer, LoyaltyTransaction, LoyaltyTxnKind
from modules.customers.service import loyalty_settings
from modules.inventory.models import StockMovementType
from modules.inventory.service import apply_movement, get_product_sales_warehouse_id
from modules.hotel.models import RoomCharge
from modules.payments.models import PaymentMethod, SalePayment
from modules.payments.service import (
    is_pos_sale_payment_method,
    list_sale_payments,
    record_sale_payment,
    sum_sale_payments,
)
from modules.refunds.service import (
    returned_qty_by_sale_line,
    sale_returned_total,
)
from modules.sales.models import Sale, SaleContext, SaleLine, SaleStatus


class InvoiceEditError(Exception):
    pass


@dataclass
class EditableInvoiceLine:
    sale_line: SaleLine
    returned_qty: Decimal
    min_qty: Decimal


@dataclass
class EditableSaleModal:
    sale: Sale
    lines: list[EditableInvoiceLine]
    returned_total: Decimal
    paid_total: Decimal
    payment_count: int
    settlement_type: str
    primary_payment_method_id: int | None
    room_charge: RoomCharge | None


def _load_completed_sale(db: Session, sale_id: int) -> Sale | None:
    stmt = (
        select(Sale)
        .where(Sale.id == sale_id, Sale.status == SaleStatus.COMPLETED)
        .options(
            selectinload(Sale.lines)
            .selectinload(SaleLine.product)
            .selectinload(Product.bom_lines_as_parent),
            selectinload(Sale.lines).selectinload(SaleLine.product),
            selectinload(Sale.customer),
        )
    )
    return db.execute(stmt).scalar_one_or_none()


def get_editable_sale(db: Session, sale_id: int) -> EditableSaleModal:
    sale = _load_completed_sale(db, sale_id)
    if sale is None:
        raise InvoiceEditError("الفاتورة غير موجودة أو غير مكتملة.")
    returned_map = returned_qty_by_sale_line(db, sale_id)
    lines: list[EditableInvoiceLine] = []
    for line in sale.lines:
        returned = returned_map.get(line.id, Decimal("0")).quantize(Decimal("0.0001"))
        min_qty = returned
        lines.append(
            EditableInvoiceLine(
                sale_line=line,
                returned_qty=returned,
                min_qty=min_qty,
            )
        )
    payments = list_sale_payments(db, sale_id)
    room_charge = db.scalar(select(RoomCharge).where(RoomCharge.sale_id == sale_id))
    settlement_type = "room" if room_charge is not None else "payment" if payments else "none"
    return EditableSaleModal(
        sale=sale,
        lines=lines,
        returned_total=sale_returned_total(db, sale_id),
        paid_total=sum_sale_payments(db, sale_id),
        payment_count=len(payments),
        settlement_type=settlement_type,
        primary_payment_method_id=payments[0].payment_method_id if payments else None,
        room_charge=room_charge,
    )


def _aggregate_component_needs(db: Session, sale: Sale) -> dict[int, Decimal]:
    pairs = [(line.product_id, line.quantity) for line in sale.lines if line.product_id]
    return merge_line_requirements(db, pairs)


def _apply_inventory_delta(
    db: Session,
    *,
    sale_id: int,
    old_needs: dict[int, Decimal],
    new_needs: dict[int, Decimal],
    user_id: int | None,
    reason: str | None = None,
) -> None:
    all_pids = set(old_needs) | set(new_needs)
    from modules.sales.models import Sale

    sale = db.get(Sale, sale_id)
    pos_shift_id = sale.pos_shift_id if sale else None
    note_base = f"تعديل فاتورة #{sale_id}"
    if reason:
        note_base = f"{note_base} — {reason.strip()}"
    for pid in all_pids:
        old = Decimal(str(old_needs.get(pid, 0)))
        new = Decimal(str(new_needs.get(pid, 0)))
        movement_delta = old - new
        if movement_delta == 0:
            continue
        sales_wh = get_product_sales_warehouse_id(db, pid, pos_shift_id=pos_shift_id)
        apply_movement(
            db,
            product_id=pid,
            quantity_delta=movement_delta,
            movement_type=StockMovementType.SALE,
            user_id=user_id,
            sale_id=sale_id,
            warehouse_id=sales_wh,
            note=note_base,
        )


def _adjust_loyalty_for_total_change(
    db: Session,
    *,
    sale: Sale,
    old_total: Decimal,
    new_total: Decimal,
    user_id: int | None,
) -> None:
    if not sale.customer_id or old_total == new_total:
        return
    customer = db.get(Customer, sale.customer_id)
    if customer is None:
        return

    delta_total = (new_total - old_total).quantize(Decimal("0.001"))
    customer.total_spent = max(
        Decimal("0"),
        Decimal(str(customer.total_spent or 0)) + delta_total,
    ).quantize(Decimal("0.001"))

    s = loyalty_settings(db)
    if not s["enabled"]:
        db.flush()
        return

    earn_rate = Decimal(str(s["earn_per_dinar"]))
    earned_before = db.execute(
        select(func.coalesce(func.sum(LoyaltyTransaction.points), 0)).where(
            LoyaltyTransaction.customer_id == customer.id,
            LoyaltyTransaction.sale_id == sale.id,
            LoyaltyTransaction.kind == LoyaltyTxnKind.EARN,
        )
    ).scalar_one()
    earned_before = Decimal(str(earned_before or 0)).quantize(Decimal("0.001"))
    if earned_before <= 0 and old_total <= 0:
        db.flush()
        return

    if old_total > 0 and earned_before > 0:
        new_earned = (new_total * earn_rate).quantize(Decimal("0.001"))
        delta_points = (new_earned - earned_before).quantize(Decimal("0.001"))
    else:
        delta_points = (delta_total * earn_rate).quantize(Decimal("0.001"))

    if delta_points == 0:
        db.flush()
        return

    new_balance = Decimal(str(customer.points_balance or 0)) + delta_points
    if new_balance < 0:
        raise InvoiceEditError(
            "تعديل الفاتورة سيُنقص نقاط الولاء تحت الصفر. "
            "عدّل الكميات/الأسعار بقيمة أصغر أو راجع نقاط العميل."
        )
    customer.points_balance = new_balance
    db.add(
        LoyaltyTransaction(
            customer_id=customer.id,
            sale_id=sale.id,
            kind=LoyaltyTxnKind.ADJUST,
            points=delta_points,
            note=f"تعديل فاتورة #{sale.id} (إجمالي {old_total} → {new_total})",
            created_by_id=user_id,
        )
    )
    db.flush()


def _sync_payments_after_edit(
    db: Session,
    *,
    sale: Sale,
    old_total: Decimal,
    new_total: Decimal,
    returned_total: Decimal,
) -> None:
    payments = list_sale_payments(db, sale.id)
    if not payments:
        return
    if len(payments) > 1:
        raise InvoiceEditError(
            "الفاتورة لها أكثر من دفعة واحدة — لا يمكن تعديلها تلقائياً. "
            "استخدم المرتجعات أو راجع المحاسبة."
        )
    if returned_total > 0:
        return
    paid = Decimal(str(payments[0].amount or 0))
    if paid != old_total.quantize(Decimal("0.001")):
        raise InvoiceEditError(
            "مبلغ الدفع لا يطابق إجمالي الفاتورة — لا يمكن تعديلها تلقائياً."
        )
    payments[0].amount = new_total.quantize(Decimal("0.001"))
    db.flush()


def _remove_sale_payments(db: Session, payments: list[SalePayment]) -> None:
    for pay in payments:
        db.delete(pay)
    db.flush()


def _apply_settlement_correction(
    db: Session,
    *,
    sale: Sale,
    settlement_type: str,
    payment_method_id: int | None,
    room_id: int | None,
    guest_name: str | None,
    note: str | None,
    reason: str | None,
    user_id: int | None,
    returned_total: Decimal,
) -> None:
    target = (settlement_type or "keep").strip().lower()
    if target in ("", "keep"):
        return
    if target not in ("payment", "room"):
        raise InvoiceEditError("نوع التسوية غير صالح.")
    if returned_total > 0:
        raise InvoiceEditError("لا يمكن تغيير طريقة التسوية لفاتورة عليها مرتجعات.")
    reason_txt = (reason or "").strip()
    if not reason_txt:
        raise InvoiceEditError("أدخل سبب التصحيح عند تغيير طريقة التسوية.")

    payments = list_sale_payments(db, sale.id)
    if len(payments) > 1:
        raise InvoiceEditError("لا يمكن تغيير التسوية لفاتورة لها أكثر من دفعة.")
    room_charge = db.scalar(select(RoomCharge).where(RoomCharge.sale_id == sale.id))
    if room_charge is not None and room_charge.is_settled:
        raise InvoiceEditError("لا يمكن تعديل فاتورة حساب شقة تمت تسويتها بالفعل.")

    amount = Decimal(str(sale.total or 0)).quantize(Decimal("0.001"))
    if amount <= 0:
        raise InvoiceEditError("إجمالي الفاتورة غير صالح للتسوية.")

    if target == "payment":
        if payment_method_id is None:
            raise InvoiceEditError("اختر وسيلة الدفع.")
        pm = db.get(PaymentMethod, int(payment_method_id))
        if not is_pos_sale_payment_method(pm):
            raise InvoiceEditError("وسيلة الدفع غير صالحة للتحصيل.")
        if room_charge is not None:
            db.delete(room_charge)
            db.flush()
        sale.context_type = SaleContext.TABLE
        sale.booking_id = None
        if payments:
            payments[0].payment_method_id = int(payment_method_id)
            payments[0].amount = amount
            db.flush()
        else:
            record_sale_payment(db, sale.id, int(payment_method_id), amount)
        return

    if room_id is None:
        raise InvoiceEditError("اختر الشقة.")
    if payments:
        _remove_sale_payments(db, payments)
    sale.context_type = SaleContext.ROOM
    sale.table_id = None
    if room_charge is not None:
        room_charge.room_id = int(room_id)
        room_charge.guest_name_snapshot = (guest_name or "").strip() or None
        room_charge.note = (note or reason_txt).strip() or None
        room_charge.is_settled = False
        room_charge.settled_at = None
        room_charge.settled_by_id = None
        room_charge.settlement_payment_method_id = None
        db.flush()
    else:
        from modules.hotel.service import HotelError, open_room_charge

        try:
            open_room_charge(
                db,
                sale_id=sale.id,
                room_id=int(room_id),
                guest_name=guest_name,
                note=(note or reason_txt),
                user_id=user_id,
            )
        except HotelError as exc:
            raise InvoiceEditError(str(exc)) from exc


def edit_completed_invoice(
    db: Session,
    *,
    sale_id: int,
    line_updates: dict[int, tuple[Decimal, Decimal]],
    user_id: int | None,
    reason: str | None = None,
    settlement_type: str = "keep",
    payment_method_id: int | None = None,
    room_id: int | None = None,
    guest_name: str | None = None,
    settlement_note: str | None = None,
) -> Sale:
    """تعديل كمية وسعر بنود فاتورة مكتملة.

    line_updates: {sale_line_id: (quantity, unit_price)}
    """
    modal = get_editable_sale(db, sale_id)
    sale = modal.sale
    if not line_updates:
        raise InvoiceEditError("لم يُرسل أي بند للتعديل.")

    old_total = Decimal(str(sale.total or 0)).quantize(Decimal("0.001"))
    returned_map = returned_qty_by_sale_line(db, sale_id)
    sale_line_map = {ln.id: ln for ln in sale.lines}

    for line_id, (qty_raw, price_raw) in line_updates.items():
        line = sale_line_map.get(int(line_id))
        if line is None:
            raise InvoiceEditError("أحد البنود لا يتبع هذه الفاتورة.")
        qty = Decimal(str(qty_raw)).quantize(Decimal("0.0001"))
        price = Decimal(str(price_raw)).quantize(Decimal("0.001"))
        if qty <= 0:
            raise InvoiceEditError(
                f"الكمية يجب أن تكون أكبر من صفر للصنف «{line.product.name_ar}»."
            )
        if price < 0:
            raise InvoiceEditError("سعر الوحدة لا يمكن أن يكون سالباً.")
        returned = returned_map.get(line.id, Decimal("0")).quantize(Decimal("0.0001"))
        if qty < returned:
            raise InvoiceEditError(
                f"الكمية الجديدة للصنف «{line.product.name_ar}» "
                f"أقل من الكمية المرتجعة سابقاً ({returned})."
            )

    old_needs = _aggregate_component_needs(db, sale)

    changed = False
    for line_id, (qty_raw, price_raw) in line_updates.items():
        line = sale_line_map[int(line_id)]
        qty = Decimal(str(qty_raw)).quantize(Decimal("0.0001"))
        price = Decimal(str(price_raw)).quantize(Decimal("0.001"))
        new_total = (qty * price).quantize(Decimal("0.001"))
        if line.quantity != qty or line.unit_price != price:
            changed = True
        line.quantity = qty
        line.unit_price = price
        line.line_total = new_total

    settlement_change = (settlement_type or "keep").strip().lower() not in ("", "keep")
    if not changed and not settlement_change:
        raise InvoiceEditError("لم يتغيّر أي بند — لا حاجة للحفظ.")

    new_sale_total = sum(
        (Decimal(str(ln.line_total or 0)) for ln in sale.lines), Decimal("0")
    ).quantize(Decimal("0.001"))
    if new_sale_total < modal.returned_total:
        raise InvoiceEditError(
            f"إجمالي الفاتورة الجديد ({new_sale_total}) "
            f"أقل من مجموع المرتجعات ({modal.returned_total})."
        )

    new_needs = _aggregate_component_needs(db, sale)
    _apply_inventory_delta(
        db,
        sale_id=sale.id,
        old_needs=old_needs,
        new_needs=new_needs,
        user_id=user_id,
        reason=reason,
    )

    sale.total = new_sale_total
    if settlement_change:
        _apply_settlement_correction(
            db,
            sale=sale,
            settlement_type=settlement_type,
            payment_method_id=payment_method_id,
            room_id=room_id,
            guest_name=guest_name,
            note=settlement_note,
            reason=reason,
            user_id=user_id,
            returned_total=modal.returned_total,
        )
    else:
        _sync_payments_after_edit(
            db,
            sale=sale,
            old_total=old_total,
            new_total=new_sale_total,
            returned_total=modal.returned_total,
        )
    _adjust_loyalty_for_total_change(
        db,
        sale=sale,
        old_total=old_total,
        new_total=new_sale_total,
        user_id=user_id,
    )
    db.flush()
    from modules.dashboard_notify.constants import INVOICE_EDIT
    from modules.dashboard_notify.service import record_activity

    record_activity(
        db,
        INVOICE_EDIT,
        event_type="invoice_edit",
        ref_id=sale.id,
        note=reason or f"تعديل فاتورة #{sale.id}",
    )
    return sale
