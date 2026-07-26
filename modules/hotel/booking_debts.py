"""ديون حجوزات الفندق — ترحيل عند المغادرة، تحصيل (كامل/جزئي)، ملاحظات وتذكير."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from modules.hotel.booking_models import (
    HotelBooking,
    HotelBookingDebt,
    HotelBookingDebtStatus,
)
from modules.hotel.booking_service import BookingError, log_audit, record_payment


def debt_remaining(debt: HotelBookingDebt) -> Decimal:
    if debt.amount_remaining is not None:
        return Decimal(str(debt.amount_remaining)).quantize(Decimal("0.001"))
    return Decimal(str(debt.amount or 0)).quantize(Decimal("0.001"))


def list_open_debts(db: Session, *, booking_id: int | None = None) -> list[HotelBookingDebt]:
    stmt = (
        select(HotelBookingDebt)
        .options(joinedload(HotelBookingDebt.booking))
        .where(HotelBookingDebt.status == HotelBookingDebtStatus.OPEN)
    )
    if booking_id is not None:
        stmt = stmt.where(HotelBookingDebt.booking_id == booking_id)
    return list(db.scalars(stmt.order_by(HotelBookingDebt.id.desc())).unique().all())


def list_debts_due_for_shift_followup(db: Session, *, as_of: date | None = None) -> list[HotelBookingDebt]:
    """ديون مفتوحة حان موعد تذكيرها، أو بلا موعد (تذكير كل وردية)."""
    today = as_of or date.today()
    rows = list_open_debts(db)
    out: list[HotelBookingDebt] = []
    for d in rows:
        if debt_remaining(d) <= Decimal("0.0005"):
            continue
        if d.reminder_at is None or d.reminder_at <= today:
            out.append(d)
    return out


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
    reminder_at: date | None = None,
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
        amount_remaining=amt,
        status=HotelBookingDebtStatus.OPEN,
        reason=(note or "").strip() or "دين عند المغادرة — لم يتم التسديد",
        reminder_at=reminder_at,
        settlement_json=settlement_json,
        created_by_id=user_id,
    )
    db.add(debt)
    db.flush()
    try:
        from modules.hotel.nav_badges import invalidate_hotel_nav_badges

        invalidate_hotel_nav_badges()
    except Exception:
        pass
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
    amount: Decimal | None = None,
) -> HotelBookingDebt:
    debt = db.get(HotelBookingDebt, debt_id)
    if debt is None:
        raise BookingError("سجل الدين غير موجود.")
    if debt.status != HotelBookingDebtStatus.OPEN:
        raise BookingError("هذا الدين ليس مفتوحاً.")
    remaining = debt_remaining(debt)
    if remaining <= Decimal("0.0005"):
        raise BookingError("لا يتبقى مبلغ على هذا الدين.")
    try:
        take = (
            Decimal(str(amount)).quantize(Decimal("0.001"))
            if amount is not None
            else remaining
        )
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise BookingError("مبلغ التحصيل غير صالح.") from exc
    if take <= Decimal("0.0005"):
        raise BookingError("أدخل مبلغ تحصيل أكبر من صفر.")
    if take > remaining + Decimal("0.0005"):
        raise BookingError(f"المبلغ أكبر من المتبقي ({remaining} د.ل).")

    pay = record_payment(
        db,
        debt.booking_id,
        amount=take,
        payment_method_id=payment_method_id,
        is_deposit=False,
        user_id=user_id,
        note=(note or "").strip()
        or f"تحصيل دين حجز #{debt.booking_id} ({take} من {remaining})",
    )
    new_rem = (remaining - take).quantize(Decimal("0.001"))
    if new_rem < 0:
        new_rem = Decimal("0")
    debt.amount_remaining = new_rem
    if new_rem <= Decimal("0.0005"):
        debt.amount_remaining = Decimal("0")
        debt.status = HotelBookingDebtStatus.COLLECTED
        debt.collected_at = datetime.now(timezone.utc)
        debt.collected_by_id = user_id
        debt.collection_payment_id = pay.id
    log_audit(
        db,
        entity_type="booking_debt",
        entity_id=debt.id,
        action="collect",
        new_value=str(take),
        reason=f"متبقي بعد التحصيل: {debt.amount_remaining}",
        user_id=user_id,
    )
    db.flush()
    try:
        from modules.hotel.nav_badges import invalidate_hotel_nav_badges

        invalidate_hotel_nav_badges()
    except Exception:
        pass
    return debt


def update_debt_followup(
    db: Session,
    debt_id: int,
    *,
    note: str | None = None,
    reminder_at: date | None = None,
    clear_reminder: bool = False,
    user_id: int | None = None,
) -> HotelBookingDebt:
    debt = db.get(HotelBookingDebt, debt_id)
    if debt is None:
        raise BookingError("سجل الدين غير موجود.")
    if debt.status != HotelBookingDebtStatus.OPEN:
        raise BookingError("يمكن تحديث الديون المفتوحة فقط.")
    note_s = (note or "").strip()
    if note_s:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        line = f"[{stamp}] {note_s}"
        prev = (debt.follow_up_notes or "").strip()
        debt.follow_up_notes = f"{prev}\n{line}".strip() if prev else line
    if clear_reminder:
        debt.reminder_at = None
    elif reminder_at is not None:
        debt.reminder_at = reminder_at
    log_audit(
        db,
        entity_type="booking_debt",
        entity_id=debt.id,
        action="followup",
        new_value=str(debt.reminder_at or ""),
        reason=note_s or None,
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
    debt.amount_remaining = Decimal("0")
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
    total = sum((debt_remaining(r) for r in rows), Decimal("0")).quantize(
        Decimal("0.001")
    )
    return rows, total


def debts_followup_counts(db: Session) -> dict[str, int]:
    due = list_debts_due_for_shift_followup(db)
    open_n = len(list_open_debts(db))
    return {
        "open": open_n,
        "due_today": len(due),
        "no_reminder": sum(1 for d in due if d.reminder_at is None),
    }


def debts_nav_badge_count(db: Session) -> int:
    """
    عدد الحجوزات/الغرف التي تستحق تنبيه الذمم:
    دين رسمي مفتوح، تتبّع أثناء الإقامة، حجز نشط غير مسدّد، أو رسوم غرفة غير مسوّاة.
    """
    from modules.hotel.booking_models import BookingPaymentStatus, BookingStatus
    from modules.hotel.models import RoomCharge

    ids: set[int] = set()
    for d in list_open_debts(db):
        if debt_remaining(d) > Decimal("0.0005"):
            ids.add(int(d.booking_id))

    unpaid_ids = db.scalars(
        select(HotelBooking.id).where(
            HotelBooking.booking_status.in_(
                (
                    BookingStatus.CHECKED_IN,
                    BookingStatus.CONFIRMED,
                    BookingStatus.PENDING,
                )
            ),
            HotelBooking.payment_status.in_(
                (
                    BookingPaymentStatus.UNPAID,
                    BookingPaymentStatus.PARTIALLY_PAID,
                )
            ),
        )
    ).all()
    ids.update(int(i) for i in unpaid_ids)

    watched = db.scalars(
        select(HotelBooking.id).where(
            HotelBooking.claim_wa_until_paid.is_(True),
            HotelBooking.booking_status.in_(
                (
                    BookingStatus.CHECKED_IN,
                    BookingStatus.CONFIRMED,
                    BookingStatus.PENDING,
                    BookingStatus.CHECKED_OUT,
                )
            ),
        )
    ).all()
    ids.update(int(i) for i in watched)

    charge_booking_ids = db.scalars(
        select(RoomCharge.booking_id).where(
            RoomCharge.is_settled.is_(False),
            RoomCharge.booking_id.isnot(None),
        )
    ).all()
    ids.update(int(i) for i in charge_booking_ids if i)

    return len(ids)


def bookings_with_debt_amounts(
    db: Session, booking_ids: list[int]
) -> dict[int, Decimal]:
    """معرّف الحجز → المتبقي (>0) لعرض شارة الدين في قائمة الحجوزات."""
    if not booking_ids:
        return {}
    from modules.hotel.booking_models import BookingStatus
    from modules.hotel.folio import booking_balance_due
    from modules.hotel.models import RoomCharge
    from modules.hotel.service import room_open_total

    out: dict[int, Decimal] = {}
    id_set = {int(i) for i in booking_ids}

    for d in list_open_debts(db):
        bid = int(d.booking_id)
        if bid not in id_set:
            continue
        rem = debt_remaining(d)
        if rem > Decimal("0.0005"):
            out[bid] = (out.get(bid, Decimal("0")) + rem).quantize(Decimal("0.001"))

    for bid in id_set:
        if bid in out:
            continue
        booking = db.get(HotelBooking, bid)
        if booking is None:
            continue
        if booking.booking_status in (BookingStatus.CANCELLED, BookingStatus.NO_SHOW):
            continue
        bal = Decimal("0")
        try:
            bal = booking_balance_due(db, bid)
        except Exception:
            bal = Decimal("0")
        if bal <= Decimal("0.0005") and booking.room_id:
            try:
                bal = max(bal, room_open_total(db, int(booking.room_id)))
            except Exception:
                pass
        if bal <= Decimal("0.0005"):
            has_charge = db.scalar(
                select(RoomCharge.id)
                .where(
                    RoomCharge.booking_id == bid,
                    RoomCharge.is_settled.is_(False),
                )
                .limit(1)
            )
            if has_charge:
                bal = Decimal("0.001")
        if bal > Decimal("0.0005"):
            out[bid] = bal.quantize(Decimal("0.001"))
    return out
