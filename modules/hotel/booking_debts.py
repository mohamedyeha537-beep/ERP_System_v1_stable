"""ديون حجوزات الفندق — ترحيل عند المغادرة، تحصيل، وشطب كخسارة."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.hotel.booking_models import (
    HotelBooking,
    HotelBookingDebt,
    HotelBookingDebtStatus,
)
from modules.hotel.booking_service import BookingError, log_audit, record_payment


def list_open_debts(db: Session, *, booking_id: int | None = None) -> list[HotelBookingDebt]:
    stmt = select(HotelBookingDebt).where(
        HotelBookingDebt.status == HotelBookingDebtStatus.OPEN
    )
    if booking_id is not None:
        stmt = stmt.where(HotelBookingDebt.booking_id == booking_id)
    return list(db.scalars(stmt.order_by(HotelBookingDebt.id.desc())).all())


def list_booking_debts(db: Session, booking_id: int) -> list[HotelBookingDebt]:
    return list(
        db.scalars(
            select(HotelBookingDebt)
            .where(HotelBookingDebt.booking_id == booking_id)
            .order_by(HotelBookingDebt.id.desc())
        ).all()
    )


def create_checkout_debt(
    db: Session,
    booking: HotelBooking,
    amount: Decimal,
    *,
    user_id: int | None = None,
    note: str | None = None,
    settlement_json: str | None = None,
) -> HotelBookingDebt:
    amt = Decimal(str(amount)).quantize(Decimal("0.001"))
    if amt <= 0:
        raise BookingError("مبلغ الدين غير صالح.")
    existing = db.scalar(
        select(HotelBookingDebt).where(
            HotelBookingDebt.booking_id == booking.id,
            HotelBookingDebt.status == HotelBookingDebtStatus.OPEN,
        )
    )
    if existing is not None:
        raise BookingError("يوجد دين مفتوح على هذا الحجز بالفعل.")
    debt = HotelBookingDebt(
        booking_id=booking.id,
        amount=amt,
        status=HotelBookingDebtStatus.OPEN,
        reason=(note or "").strip() or "دين عند المغادرة — لم يتم التسديد",
        settlement_json=settlement_json,
        created_by_id=user_id,
    )
    db.add(debt)
    db.flush()
    log_audit(
        db,
        entity_type="booking_debt",
        entity_id=debt.id,
        action="create",
        new_value=str(amt),
        reason=debt.reason,
        user_id=user_id,
    )
    return debt


def collect_booking_debt(
    db: Session,
    debt_id: int,
    *,
    payment_method_id: int,
    user_id: int | None = None,
    note: str | None = None,
) -> HotelBookingDebt:
    debt = db.get(HotelBookingDebt, debt_id)
    if debt is None:
        raise BookingError("سجل الدين غير موجود.")
    if debt.status != HotelBookingDebtStatus.OPEN:
        raise BookingError("هذا الدين ليس مفتوحاً.")
    pay = record_payment(
        db,
        debt.booking_id,
        amount=debt.amount,
        payment_method_id=payment_method_id,
        is_deposit=False,
        user_id=user_id,
        note=(note or "").strip() or f"تحصيل دين حجز #{debt.booking_id}",
    )
    debt.status = HotelBookingDebtStatus.COLLECTED
    debt.collected_at = datetime.now(timezone.utc)
    debt.collected_by_id = user_id
    debt.collection_payment_id = pay.id
    log_audit(
        db,
        entity_type="booking_debt",
        entity_id=debt.id,
        action="collect",
        new_value=str(debt.amount),
        user_id=user_id,
    )
    db.flush()
    return debt


def write_off_booking_debt(
    db: Session,
    debt_id: int,
    *,
    reason: str | None,
    user_id: int | None = None,
) -> HotelBookingDebt:
    debt = db.get(HotelBookingDebt, debt_id)
    if debt is None:
        raise BookingError("سجل الدين غير موجود.")
    if debt.status != HotelBookingDebtStatus.OPEN:
        raise BookingError("يمكن شطب الديون المفتوحة فقط.")
    why = (reason or "").strip() or "شطب دين — عدم التسديد (خسارة)"
    debt.status = HotelBookingDebtStatus.WRITTEN_OFF
    debt.written_off_at = datetime.now(timezone.utc)
    debt.written_off_by_id = user_id
    debt.write_off_reason = why
    log_audit(
        db,
        entity_type="booking_debt",
        entity_id=debt.id,
        action="write_off",
        new_value=str(debt.amount),
        reason=why,
        user_id=user_id,
    )
    db.flush()
    return debt


def open_debts_summary(db: Session) -> tuple[list[HotelBookingDebt], Decimal]:
    rows = list_open_debts(db)
    total = sum((Decimal(str(r.amount or 0)) for r in rows), Decimal("0")).quantize(
        Decimal("0.001")
    )
    return rows, total
