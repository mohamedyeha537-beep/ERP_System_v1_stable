"""متابعة الحجوزات — تذكير داخلي ومطالبة واتساب حتى السداد."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.hotel.audit import log_audit
from modules.hotel.booking_models import BookingStatus, HotelBooking, RecordKind
from modules.hotel.booking_service import BookingError


def clear_claim_wa_if_settled(db: Session, booking: HotelBooking) -> bool:
    """يوقف مطالبات الواتساب عند تصفير رصيد الحجز. يعيد True إن تغيّر شيء."""
    from modules.hotel.folio import booking_balance_due

    if not getattr(booking, "claim_wa_until_paid", False):
        return False
    balance = booking_balance_due(db, booking.id)
    if balance > Decimal("0.001"):
        return False
    booking.claim_wa_until_paid = False
    # نبقي follow_up_at إن وُجد كتذكير داخلي فقط — أو نمسحه عند السداد الكامل
    booking.follow_up_at = None
    return True


def mark_stay_debt_watch(
    db: Session,
    booking_id: int,
    *,
    note: str | None = None,
    user_id: int | None = None,
    clear: bool = False,
) -> HotelBooking:
    """
    تتبّع دين أثناء الإقامة — يظهر دائماً في الذمم / الشارات حتى السداد.
    لا يتطلب مغادرة؛ يعتمد على رصيد الفوليو الحالي.
    """
    from modules.hotel.folio import booking_balance_due
    from modules.hotel.nav_badges import invalidate_hotel_nav_badges

    booking = db.get(HotelBooking, booking_id)
    if booking is None:
        raise BookingError("الحجز غير موجود.")
    if getattr(booking, "record_kind", None) == RecordKind.QUOTATION:
        raise BookingError("لا يُتتبّع دين على عرض سعر.")
    if booking.booking_status not in (
        BookingStatus.CHECKED_IN,
        BookingStatus.CONFIRMED,
        BookingStatus.PENDING,
    ):
        raise BookingError("تتبّع الدين أثناء الإقامة للحجوزات النشطة فقط.")

    if clear:
        booking.claim_wa_until_paid = False
        booking.follow_up_at = None
        if note is None:
            booking.follow_up_note = None
        log_audit(
            db,
            entity_type="booking",
            entity_id=booking.id,
            action="debt_watch_cleared",
            user_id=user_id,
        )
        try:
            invalidate_hotel_nav_badges()
        except Exception:
            pass
        db.flush()
        return booking

    balance = booking_balance_due(db, booking.id)
    if balance <= Decimal("0.001"):
        raise BookingError("لا يوجد متبقٍ على الحساب لتسجيله كدين.")

    today = date.today()
    try:
        from app.datetime_local import now_local

        today = now_local().date()
    except Exception:
        pass

    booking.claim_wa_until_paid = True
    booking.follow_up_at = today  # يظهر فوراً لكل وردية حتى السداد
    cleaned = (note or "").strip() or booking.follow_up_note
    if not cleaned:
        cleaned = (
            f"دين أثناء الإقامة — متبقي {balance.quantize(Decimal('0.001'))} د.ل "
            "— مطالبة حتى التحصيل"
        )
    booking.follow_up_note = cleaned
    log_audit(
        db,
        entity_type="booking",
        entity_id=booking.id,
        action="debt_watch_set",
        new_value=str(balance),
        reason=cleaned,
        user_id=user_id,
    )
    try:
        invalidate_hotel_nav_badges()
    except Exception:
        pass
    db.flush()
    return booking


def set_booking_follow_up(
    db: Session,
    booking_id: int,
    *,
    follow_up_at: date | None,
    follow_up_note: str | None = None,
    claim_wa_until_paid: bool = False,
    clear: bool = False,
    user_id: int | None = None,
) -> HotelBooking:
    booking = db.get(HotelBooking, booking_id)
    if booking is None:
        raise BookingError("الحجز غير موجود.")
    if clear:
        booking.follow_up_at = None
        booking.follow_up_note = None
        booking.claim_wa_until_paid = False
        log_audit(
            db,
            entity_type="booking",
            entity_id=booking.id,
            action="follow_up_cleared",
            user_id=user_id,
        )
        return booking

    if claim_wa_until_paid:
        from modules.hotel.folio import booking_balance_due

        if booking_balance_due(db, booking.id) <= Decimal("0.001"):
            raise BookingError("لا يوجد متبقٍ على الحساب — لا حاجة لمطالبة واتساب.")
        phone = (booking.guest_phone or "").strip()
        if not phone and booking.guest_type and booking.guest_type.value == "COMPANY":
            phone = (booking.company_contact_phone or "").strip()
        if not phone:
            raise BookingError("أضف رقم واتساب للنزيل أولاً لإرسال المطالبة.")

    booking.follow_up_at = follow_up_at
    note = (follow_up_note or "").strip() or None
    booking.follow_up_note = note
    booking.claim_wa_until_paid = bool(claim_wa_until_paid)
    log_audit(
        db,
        entity_type="booking",
        entity_id=booking.id,
        action="follow_up_set",
        new_value=str(follow_up_at or ""),
        reason=note,
        user_id=user_id,
    )
    return booking


def send_booking_claim_whatsapp(
    db: Session,
    booking_id: int,
    *,
    user_id: int | None = None,
    note: str | None = None,
) -> HotelBooking:
    """إرسال مطالبة واتساب فورية من صفحة تفاصيل الحجز."""
    from modules.notifications.hotel_hooks import emit_hotel_balance_claim

    booking = db.get(HotelBooking, booking_id)
    if booking is None:
        raise BookingError("الحجز غير موجود.")
    from modules.hotel.folio import booking_balance_due

    balance = booking_balance_due(db, booking.id)
    if balance <= Decimal("0.001"):
        clear_claim_wa_if_settled(db, booking)
        raise BookingError("الحساب مسدّد — لا متبقٍ للمطالبة.")
    phone = (booking.guest_phone or "").strip()
    if not phone and getattr(booking, "company_contact_phone", None):
        phone = (booking.company_contact_phone or "").strip()
    if not phone:
        raise BookingError("لا يوجد رقم واتساب للنزيل.")
    emit_hotel_balance_claim(
        db,
        booking,
        claim_note=(note or booking.follow_up_note or "").strip() or None,
    )
    log_audit(
        db,
        entity_type="booking",
        entity_id=booking.id,
        action="claim_whatsapp_sent",
        new_value=str(balance),
        user_id=user_id,
    )
    return booking


def list_follow_ups_due(
    db: Session,
    *,
    as_of: date | None = None,
    property_id: int | None = None,
) -> list[HotelBooking]:
    """حجوزات حان موعد متابعتها أو تغادر خلال 24 ساعة."""
    from app.datetime_local import now_local

    today = as_of or now_local().date()
    tomorrow = today + timedelta(days=1)
    stmt = select(HotelBooking).where(
        HotelBooking.record_kind == RecordKind.BOOKING,
        HotelBooking.booking_status == BookingStatus.CHECKED_IN,
    )
    if property_id is not None:
        stmt = stmt.where(HotelBooking.property_id == property_id)
    rows = list(db.scalars(stmt).all())
    out: list[HotelBooking] = []
    for b in rows:
        if b.check_out in (today, tomorrow):
            out.append(b)
            continue
        rem = getattr(b, "follow_up_at", None)
        if rem is not None and rem <= today:
            out.append(b)
            continue
        if getattr(b, "claim_wa_until_paid", False):
            out.append(b)
    return out


def follow_ups_due_count(db: Session, *, property_id: int = 1) -> int:
    return len(list_follow_ups_due(db, property_id=property_id))
