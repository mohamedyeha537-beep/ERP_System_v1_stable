"""مصروفات وردية الفندق."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, joinedload

from modules.hotel.shift_models import HotelShift, HotelShiftError, HotelShiftStatus
from modules.payments.models import PaymentMethod, PaymentMethodKind, Purchase, PurchaseKind
from modules.payments.service import PaymentsError, record_expense
from modules.platform.business_domain import BusinessDomain


def _shift_expense_scope(db: Session, shift_id: int):
    direct = Purchase.hotel_shift_id == shift_id
    sh = db.get(HotelShift, shift_id)
    if sh is None:
        return direct
    fallback = [
        Purchase.hotel_shift_id.is_(None),
        Purchase.created_by_id == sh.user_id,
        Purchase.expense_category.like("مصروف وردية%"),
    ]
    if sh.opened_at is not None:
        fallback.append(Purchase.created_at >= sh.opened_at)
    fallback.append(Purchase.created_at <= (sh.closed_at or datetime.now(timezone.utc)))
    return or_(direct, and_(*fallback))


def sum_shift_expenses(db: Session, shift_id: int) -> Decimal:
    raw = db.scalar(
        select(func.coalesce(func.sum(Purchase.amount), 0)).where(
            _shift_expense_scope(db, shift_id),
            Purchase.kind == PurchaseKind.EXPENSE,
        )
    )
    return Decimal(str(raw or 0)).quantize(Decimal("0.001"))


def list_shift_expenses(db: Session, shift_id: int, *, limit: int = 50) -> list[Purchase]:
    return list(
        db.scalars(
            select(Purchase)
            .options(joinedload(Purchase.method))
            .where(
                _shift_expense_scope(db, shift_id),
                Purchase.kind == PurchaseKind.EXPENSE,
            )
            .order_by(Purchase.id.desc())
            .limit(limit)
        )
        .unique()
        .all()
    )


def record_hotel_shift_expense(
    db: Session,
    *,
    shift_id: int,
    amount: Decimal,
    payment_method_id: int,
    category: str,
    note: str | None,
    user_id: int | None,
) -> Purchase:
    shift = db.get(HotelShift, shift_id)
    if shift is None or shift.status != HotelShiftStatus.OPEN:
        raise HotelShiftError("لا توجد وردية مفتوحة.")
    label = (category or "مصروف وردية").strip() or "مصروف وردية"
    if not label.startswith("مصروف وردية"):
        label = f"مصروف وردية — {label}"
    try:
        purchase = record_expense(
            db,
            amount=amount,
            payment_method_id=payment_method_id,
            category=label,
            note=note,
            user_id=user_id,
            business_domain=BusinessDomain.HOTEL.value,
        )
    except PaymentsError as exc:
        raise HotelShiftError(str(exc)) from exc
    purchase.hotel_shift_id = shift_id
    db.flush()
    return purchase


def list_hotel_expense_methods(db: Session) -> list[PaymentMethod]:
    from modules.payments.service import list_hotel_settle_payment_methods

    return list_hotel_settle_payment_methods(db, only_active=True)
