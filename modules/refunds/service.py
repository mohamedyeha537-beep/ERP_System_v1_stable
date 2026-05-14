from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from modules.catalog.models import Product
from modules.customers.models import Customer, LoyaltyTransaction, LoyaltyTxnKind
from modules.inventory.models import StockMovementType
from modules.inventory.service import apply_movement
from modules.payments.models import RefundPayment, SalePayment
from modules.payments.service import (
    PaymentsError,
    list_sale_payments,
    record_payment_transfer,
    record_refund_payment,
    sum_sale_payments,
)
from modules.refunds.models import (
    SaleReturn,
    SaleReturnLine,
    SaleReturnMode,
    SaleReturnStatus,
    SaleReturnType,
)
from modules.sales.models import Sale, SaleLine, SaleStatus


class RefundsError(Exception):
    pass


RETURN_LOYALTY_NOTE_PREFIX = "عكس نقاط مرتجع"


@dataclass
class RefundableSaleLine:
    sale_line: SaleLine
    returned_qty: Decimal
    remaining_qty: Decimal


@dataclass
class RefundableSaleSummary:
    sale: Sale
    lines: list[RefundableSaleLine]
    returns: list[SaleReturn]
    payments: list[SalePayment]
    room_charge: object | None
    returned_total: Decimal
    net_total: Decimal
    paid_total: Decimal
    refunded_paid_total: Decimal
    outstanding_total: Decimal


def _load_completed_sale(db: Session, sale_id: int) -> Sale | None:
    stmt = (
        select(Sale)
        .where(Sale.id == sale_id, Sale.status == SaleStatus.COMPLETED)
        .options(
            selectinload(Sale.lines)
            .selectinload(SaleLine.product)
            .selectinload(Product.bom_lines_as_parent),
            selectinload(Sale.lines)
            .selectinload(SaleLine.product)
            .selectinload(Product.category),
        )
    )
    return db.execute(stmt).scalar_one_or_none()


def _get_sale_returns_query(sale_id: int):
    return (
        select(SaleReturn)
        .where(SaleReturn.original_sale_id == sale_id)
        .order_by(SaleReturn.created_at.desc(), SaleReturn.id.desc())
    )


def list_sale_returns(db: Session, sale_id: int) -> list[SaleReturn]:
    stmt = _get_sale_returns_query(sale_id).options(
        selectinload(SaleReturn.lines).selectinload(SaleReturnLine.product),
        selectinload(SaleReturn.original_payment_method),
        selectinload(SaleReturn.refund_payment_method),
    )
    return list(db.scalars(stmt).all())


def get_sale_return(db: Session, sale_return_id: int) -> SaleReturn | None:
    stmt = (
        select(SaleReturn)
        .where(SaleReturn.id == sale_return_id)
        .options(
            selectinload(SaleReturn.lines).selectinload(SaleReturnLine.product),
            selectinload(SaleReturn.sale).selectinload(Sale.lines).selectinload(
                SaleLine.product
            ),
            selectinload(SaleReturn.original_payment_method),
            selectinload(SaleReturn.refund_payment_method),
        )
    )
    return db.execute(stmt).scalar_one_or_none()


def sale_returned_total(db: Session, sale_id: int) -> Decimal:
    total = db.execute(
        select(func.coalesce(func.sum(SaleReturn.total), 0)).where(
            SaleReturn.original_sale_id == sale_id,
            SaleReturn.status == SaleReturnStatus.POSTED,
        )
    ).scalar_one()
    return Decimal(str(total or 0)).quantize(Decimal("0.001"))


def sale_refunded_paid_total(db: Session, sale_id: int) -> Decimal:
    total = db.execute(
        select(func.coalesce(func.sum(RefundPayment.amount), 0))
        .join(SaleReturn, SaleReturn.id == RefundPayment.sale_return_id)
        .where(SaleReturn.original_sale_id == sale_id)
    ).scalar_one()
    return Decimal(str(total or 0)).quantize(Decimal("0.001"))


def returned_qty_by_sale_line(db: Session, sale_id: int) -> dict[int, Decimal]:
    rows = db.execute(
        select(
            SaleReturnLine.sale_line_id,
            func.coalesce(func.sum(SaleReturnLine.quantity), 0),
        )
        .join(SaleReturn, SaleReturn.id == SaleReturnLine.sale_return_id)
        .where(
            SaleReturn.original_sale_id == sale_id,
            SaleReturn.status == SaleReturnStatus.POSTED,
        )
        .group_by(SaleReturnLine.sale_line_id)
    ).all()
    return {int(line_id): Decimal(str(qty or 0)) for line_id, qty in rows}


def sale_remaining_total(db: Session, sale_id: int) -> Decimal:
    sale = db.get(Sale, sale_id)
    if sale is None:
        return Decimal("0")
    remaining = Decimal(str(sale.total or 0)) - sale_returned_total(db, sale_id)
    if remaining < 0:
        return Decimal("0")
    return remaining.quantize(Decimal("0.001"))


def sale_outstanding_total(db: Session, sale_id: int) -> Decimal:
    remaining = sale_remaining_total(db, sale_id)
    paid = sum_sale_payments(db, sale_id)
    outstanding = remaining - paid
    if outstanding < 0:
        return Decimal("0")
    return outstanding.quantize(Decimal("0.001"))


def get_open_room_charge(db: Session, sale_id: int):
    from modules.hotel.models import RoomCharge

    return db.execute(
        select(RoomCharge)
        .where(RoomCharge.sale_id == sale_id, RoomCharge.is_settled.is_(False))
        .options(selectinload(RoomCharge.room))
    ).scalar_one_or_none()


def get_refundable_sale_summary(db: Session, sale_id: int) -> RefundableSaleSummary:
    sale = _load_completed_sale(db, sale_id)
    if sale is None:
        raise RefundsError("الفاتورة غير موجودة أو غير مكتملة.")
    returned_qty_map = returned_qty_by_sale_line(db, sale_id)
    line_rows: list[RefundableSaleLine] = []
    for line in sale.lines:
        returned = returned_qty_map.get(line.id, Decimal("0")).quantize(
            Decimal("0.0001")
        )
        remaining = (Decimal(str(line.quantity or 0)) - returned).quantize(
            Decimal("0.0001")
        )
        if remaining < 0:
            remaining = Decimal("0")
        line_rows.append(
            RefundableSaleLine(
                sale_line=line,
                returned_qty=returned,
                remaining_qty=remaining,
            )
        )
    returns = list_sale_returns(db, sale_id)
    payments = list_sale_payments(db, sale_id)
    returned_total = sale_returned_total(db, sale_id)
    net_total = sale_remaining_total(db, sale_id)
    paid_total = sum_sale_payments(db, sale_id)
    refunded_paid_total = sale_refunded_paid_total(db, sale_id)
    outstanding_total = sale_outstanding_total(db, sale_id)
    room_charge = get_open_room_charge(db, sale_id)
    return RefundableSaleSummary(
        sale=sale,
        lines=line_rows,
        returns=returns,
        payments=payments,
        room_charge=room_charge,
        returned_total=returned_total,
        net_total=net_total,
        paid_total=paid_total,
        refunded_paid_total=refunded_paid_total,
        outstanding_total=outstanding_total,
    )


def search_completed_sales(
    db: Session,
    *,
    q: str | None = None,
    limit: int = 100,
) -> list[Sale]:
    stmt = select(Sale).where(Sale.status == SaleStatus.COMPLETED)
    term = (q or "").strip()
    if term:
        if term.isdigit():
            stmt = stmt.where(Sale.id == int(term))
        else:
            stmt = stmt.where(Sale.external_order_id.like(f"%{term}%"))
    stmt = stmt.order_by(Sale.created_at.desc(), Sale.id.desc()).limit(limit)
    return list(db.scalars(stmt).all())


def _normalize_return_lines(
    sale: Sale,
    returned_qty_map: dict[int, Decimal],
    lines: list[tuple[int, Decimal]],
) -> tuple[list[tuple[SaleLine, Decimal, Decimal]], Decimal]:
    grouped: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
    for sale_line_id, qty in lines:
        grouped[int(sale_line_id)] += Decimal(str(qty or 0))
    if not grouped:
        raise RefundsError("اختر بنداً واحداً على الأقل للترجيع.")

    sale_line_map = {line.id: line for line in sale.lines}
    normalized: list[tuple[SaleLine, Decimal, Decimal]] = []
    total = Decimal("0")
    for sale_line_id, qty_raw in grouped.items():
        line = sale_line_map.get(sale_line_id)
        if line is None:
            raise RefundsError("أحد البنود المحددة لا يتبع هذه الفاتورة.")
        qty = Decimal(str(qty_raw)).quantize(Decimal("0.0001"))
        if qty <= 0:
            continue
        already = returned_qty_map.get(line.id, Decimal("0"))
        remaining = (Decimal(str(line.quantity or 0)) - already).quantize(
            Decimal("0.0001")
        )
        if qty > remaining:
            raise RefundsError(
                f"الكمية المرتجعة للصنف «{line.product.name_ar}» تتجاوز المتاح للترجيع."
            )
        line_total = (qty * Decimal(str(line.unit_price or 0))).quantize(
            Decimal("0.001")
        )
        normalized.append((line, qty, line_total))
        total += line_total

    total = total.quantize(Decimal("0.001"))
    if not normalized or total <= 0:
        raise RefundsError("قيمة المرتجع يجب أن تكون أكبر من صفر.")
    return normalized, total


def _return_components(
    db: Session,
    *,
    sale_return_id: int,
    lines: list[tuple[SaleLine, Decimal, Decimal]],
    user_id: int | None,
) -> None:
    component_qty: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
    for sale_line, qty, _line_total in lines:
        for bom in sale_line.product.bom_lines_as_parent:
            component_qty[bom.component_product_id] += bom.qty_per_parent * qty
    for product_id, qty in component_qty.items():
        if qty <= 0:
            continue
        apply_movement(
            db,
            product_id=product_id,
            quantity_delta=qty.quantize(Decimal("0.0001")),
            movement_type=StockMovementType.SALE_RETURN,
            user_id=user_id,
            sale_id=None,
            note=f"مرتجع بيع #{sale_return_id}",
        )


def _earned_points_for_sale(db: Session, customer_id: int, sale_id: int) -> Decimal:
    total = db.execute(
        select(func.coalesce(func.sum(LoyaltyTransaction.points), 0)).where(
            LoyaltyTransaction.customer_id == customer_id,
            LoyaltyTransaction.sale_id == sale_id,
            LoyaltyTransaction.kind == LoyaltyTxnKind.EARN,
        )
    ).scalar_one()
    return Decimal(str(total or 0)).quantize(Decimal("0.001"))


def _reversed_points_for_sale(db: Session, customer_id: int, sale_id: int) -> Decimal:
    total = db.execute(
        select(func.coalesce(func.sum(-LoyaltyTransaction.points), 0)).where(
            LoyaltyTransaction.customer_id == customer_id,
            LoyaltyTransaction.sale_id == sale_id,
            LoyaltyTransaction.kind == LoyaltyTxnKind.ADJUST,
            LoyaltyTransaction.points < 0,
            LoyaltyTransaction.note.like(f"{RETURN_LOYALTY_NOTE_PREFIX}%"),
        )
    ).scalar_one()
    return Decimal(str(total or 0)).quantize(Decimal("0.001"))


def _reverse_loyalty_for_return(
    db: Session,
    *,
    sale: Sale,
    sale_return: SaleReturn,
    previous_returned_total: Decimal,
    user_id: int | None,
) -> Decimal:
    if not sale.customer_id:
        return Decimal("0")
    customer = db.get(Customer, sale.customer_id)
    if customer is None:
        return Decimal("0")
    earned = _earned_points_for_sale(db, customer.id, sale.id)
    if earned <= 0 or sale.total <= 0:
        customer.total_spent = max(
            Decimal("0"),
            Decimal(str(customer.total_spent or 0)) - sale_return.total,
        ).quantize(Decimal("0.001"))
        return Decimal("0")

    reversed_before = _reversed_points_for_sale(db, customer.id, sale.id)
    remaining_points = (earned - reversed_before).quantize(Decimal("0.001"))
    if remaining_points < 0:
        remaining_points = Decimal("0")

    after_returned_total = (previous_returned_total + sale_return.total).quantize(
        Decimal("0.001")
    )
    if after_returned_total >= Decimal(str(sale.total or 0)):
        points = remaining_points
    else:
        points = (
            (earned * sale_return.total) / Decimal(str(sale.total or 0))
        ).quantize(Decimal("0.001"))
        if points > remaining_points:
            points = remaining_points
    if points < 0:
        points = Decimal("0")

    customer.total_spent = max(
        Decimal("0"),
        Decimal(str(customer.total_spent or 0)) - sale_return.total,
    ).quantize(Decimal("0.001"))
    if (
        previous_returned_total < Decimal(str(sale.total or 0))
        and after_returned_total >= Decimal(str(sale.total or 0))
        and int(customer.visits_count or 0) > 0
    ):
        customer.visits_count = int(customer.visits_count or 0) - 1

    if points > 0:
        customer.points_balance = max(
            Decimal("0"),
            Decimal(str(customer.points_balance or 0)) - points,
        ).quantize(Decimal("0.001"))
        db.add(
            LoyaltyTransaction(
                customer_id=customer.id,
                sale_id=sale.id,
                kind=LoyaltyTxnKind.ADJUST,
                points=-points,
                note=f"{RETURN_LOYALTY_NOTE_PREFIX} #{sale_return.id} من فاتورة #{sale.id}",
                created_by_id=user_id,
            )
        )
    db.flush()
    return points


def create_sale_return(
    db: Session,
    *,
    sale_id: int,
    lines: list[tuple[int, Decimal]],
    created_by_id: int | None,
    reason: str | None = None,
    note: str | None = None,
    refund_payment_method_id: int | None = None,
    allow_payment_override: bool = False,
    approved_by_id: int | None = None,
) -> SaleReturn:
    sale = _load_completed_sale(db, sale_id)
    if sale is None:
        raise RefundsError("الفاتورة غير موجودة أو غير مكتملة.")

    previous_returned_total = sale_returned_total(db, sale_id)
    returned_qty_map = returned_qty_by_sale_line(db, sale_id)
    normalized_lines, total = _normalize_return_lines(sale, returned_qty_map, lines)
    remaining_before = (
        Decimal(str(sale.total or 0)) - previous_returned_total
    ).quantize(Decimal("0.001"))
    if total > remaining_before:
        raise RefundsError("إجمالي المرتجع يتجاوز صافي الفاتورة القابل للترجيع.")

    payments = list_sale_payments(db, sale.id)
    paid_total = sum_sale_payments(db, sale.id)
    refunded_paid_before = sale_refunded_paid_total(db, sale.id)
    room_charge = get_open_room_charge(db, sale.id)

    original_payment_method_id = payments[0].payment_method_id if payments else None
    resolved_refund_method_id: int | None = None
    mode = SaleReturnMode.NO_PAYMENT
    resolved_approved_by_id = None

    if room_charge is not None and paid_total <= 0:
        mode = SaleReturnMode.REDUCE_RECEIVABLE
    elif paid_total > 0:
        refundable_cash_left = (paid_total - refunded_paid_before).quantize(
            Decimal("0.001")
        )
        if total > refundable_cash_left:
            raise RefundsError(
                "قيمة المرتجع النقدي تتجاوز ما تم تحصيله فعلياً من هذه الفاتورة."
            )
        resolved_refund_method_id = refund_payment_method_id or original_payment_method_id
        if resolved_refund_method_id is None:
            raise RefundsError("تعذر تحديد وسيلة رد المبلغ لهذه الفاتورة.")
        if resolved_refund_method_id != original_payment_method_id:
            if not allow_payment_override:
                raise RefundsError(
                    "لا يمكن رد المبلغ بوسيلة مختلفة إلا بصلاحية مدير/محاسب."
                )
            mode = SaleReturnMode.OVERRIDE_TRANSFER
            resolved_approved_by_id = approved_by_id or created_by_id
        else:
            mode = SaleReturnMode.SAME_METHOD
    else:
        mode = SaleReturnMode.NO_PAYMENT

    sale_return = SaleReturn(
        original_sale_id=sale.id,
        return_type=SaleReturnType.PARTIAL,
        status=SaleReturnStatus.POSTED,
        mode=mode,
        total=total,
        reason=(reason or "").strip() or None,
        note=(note or "").strip() or None,
        created_by_id=created_by_id,
        approved_by_id=resolved_approved_by_id,
        original_payment_method_id=original_payment_method_id,
        refund_payment_method_id=resolved_refund_method_id,
    )
    db.add(sale_return)
    db.flush()

    for sale_line, qty, line_total in normalized_lines:
        db.add(
            SaleReturnLine(
                sale_return_id=sale_return.id,
                sale_line_id=sale_line.id,
                product_id=sale_line.product_id,
                quantity=qty,
                unit_price=sale_line.unit_price,
                line_total=line_total,
            )
        )
    db.flush()

    remaining_after = (
        remaining_before - sale_return.total
    ).quantize(Decimal("0.001"))
    if remaining_after <= 0:
        sale_return.return_type = SaleReturnType.FULL

    _return_components(
        db,
        sale_return_id=sale_return.id,
        lines=normalized_lines,
        user_id=created_by_id,
    )

    if mode in (SaleReturnMode.SAME_METHOD, SaleReturnMode.OVERRIDE_TRANSFER):
        try:
            record_refund_payment(
                db,
                sale_return_id=sale_return.id,
                payment_method_id=int(resolved_refund_method_id),
                amount=sale_return.total,
                user_id=created_by_id,
                note=f"مرتجع فاتورة #{sale.id}",
            )
            if (
                mode == SaleReturnMode.OVERRIDE_TRANSFER
                and original_payment_method_id is not None
                and resolved_refund_method_id is not None
                and original_payment_method_id != resolved_refund_method_id
            ):
                record_payment_transfer(
                    db,
                    sale_return_id=sale_return.id,
                    from_payment_method_id=original_payment_method_id,
                    to_payment_method_id=resolved_refund_method_id,
                    amount=sale_return.total,
                    user_id=created_by_id,
                    note=f"تسوية فرق وسيلة مرتجع فاتورة #{sale.id}",
                )
        except PaymentsError as exc:
            raise RefundsError(str(exc)) from exc

    _reverse_loyalty_for_return(
        db,
        sale=sale,
        sale_return=sale_return,
        previous_returned_total=previous_returned_total,
        user_id=created_by_id,
    )

    if room_charge is not None and sale_outstanding_total(db, sale.id) <= 0:
        room_charge.is_settled = True
        room_charge.settled_at = datetime.now(timezone.utc)
        room_charge.settled_by_id = created_by_id
        room_charge.settlement_payment_method_id = None

    db.flush()
    return sale_return
