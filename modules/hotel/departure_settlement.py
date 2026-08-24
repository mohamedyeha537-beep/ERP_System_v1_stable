"""تسوية المغادرة — الرصيد من نفس مصدر كشف الحساب (لا محفظة ثانية)."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from modules.hotel.booking_models import HotelBooking, BookingStatus
from modules.hotel.booking_service import BookingError
from modules.hotel.folio import build_folio, build_guest_account

Q = Decimal("0.001")


@dataclass
class DepartureSettlementPreview:
    check_in: date
    scheduled_check_out: date
    actual_departure: date
    booked_nights: int
    stayed_nights: int
    cancelled_nights: int
    accommodation_booked: Decimal
    accommodation_actual: Decimal
    accommodation_saved: Decimal
    services_total: Decimal
    pos_total: Decimal
    discount: Decimal
    folio_total_booked: Decimal
    folio_total_actual: Decimal
    paid_amount: Decimal
    #: رصيد النزيل = guest_account.amount_credit (نفس رقم الشريط الأخضر)
    refund_due: Decimal
    #: متبقٍ = guest_account.amount_due
    balance_due: Decimal
    is_early_departure: bool
    #: تقدير فقط عند اختيار مغادرة مبكرة — لا يُعرض كرصيد رسمي
    projected_folio_total: Decimal
    projected_refund_due: Decimal
    projected_balance_due: Decimal

    def to_dict(self) -> dict:
        return {
            k: (str(v) if isinstance(v, Decimal) else v.isoformat() if isinstance(v, date) else v)
            for k, v in asdict(self).items()
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


def preview_departure_settlement(
    db: Session,
    booking: HotelBooking,
    *,
    actual_departure: date | None = None,
) -> DepartureSettlementPreview:
    """
    الرصيد الرسمي دائماً من build_guest_account (نفس المتغير في أعلى الصفحة).
    أرقام المغادرة المبكرة تُحسب كتقدير منفصل ولا تستبدل الرصيد الرسمي.
    المغادرة بعد الموعد المجدول (Overstay) مسموحة — لا تُرفض؛ الإقامة المسجّلة تبقى كما هي.
    """
    if booking.booking_status not in (
        BookingStatus.CHECKED_IN,
        BookingStatus.CONFIRMED,
        BookingStatus.PENDING,
    ):
        raise BookingError("معاينة التسوية متاحة قبل المغادرة فقط.")
    departure = actual_departure or booking.check_out
    if departure < booking.check_in:
        raise BookingError("تاريخ المغادرة لا يمكن أن يكون قبل تاريخ الوصول.")

    try:
        from modules.hotel.late_checkout import ensure_overstay_nights_caught_up

        if booking.booking_status == BookingStatus.CHECKED_IN:
            ensure_overstay_nights_caught_up(db, booking, notify=False)
            db.refresh(booking)
    except Exception:  # noqa: BLE001
        pass

    folio = build_folio(db, booking.id)
    guest = build_guest_account(db, booking.id)

    scheduled_out = booking.planned_check_out
    booked_nights = max(0, (scheduled_out - booking.check_in).days)
    bill_nights = max(0, (booking.check_out - booking.check_in).days) or booked_nights
    raw_stayed = max(0, (departure - booking.check_in).days)
    same_day_use = departure == booking.check_in
    # ليالي الإقامة المعروضة: لا تقل عن المسجّل في الفاتورة عند التأخير
    stayed_nights = 1 if same_day_use else raw_stayed
    if bill_nights > 0 and departure <= booking.check_out:
        stayed_nights = min(stayed_nights, bill_nights)
    cancelled_nights = max(0, (booked_nights or bill_nights) - min(stayed_nights, bill_nights or stayed_nights))

    discount = Decimal(str(booking.discount_amount or 0)).quantize(Q)
    acc_booked = Decimal(str(booking.accommodation_total or 0)).quantize(Q)
    svc_total = Decimal(str(folio.services or 0)).quantize(Q)
    pos_total = Decimal(str(folio.pos_charges or 0)).quantize(Q)
    paid = Decimal(str(guest.total_paid or 0)).quantize(Q)
    folio_total = Decimal(str(guest.total_charges or 0)).quantize(Q)

    is_early = departure < booking.check_out or same_day_use
    is_late = departure > booking.check_out
    if is_late or not is_early or departure >= booking.check_out:
        # في الموعد أو بعده (أو Overstay): بدون تخفيض إقامة
        acc_actual = acc_booked
        projected_total = folio_total
        is_early = False if is_late else (is_early and departure < booking.check_out)
    else:
        denom = _accommodation_bill_nights_from_booking(
            booking, old_check_out=booking.check_out, old_acc=acc_booked
        )
        stayed_for_money = 1 if same_day_use else stayed_nights
        stayed_for_money = min(max(stayed_for_money, 0), denom)
        acc_actual = (acc_booked * Decimal(stayed_for_money) / Decimal(denom)).quantize(Q)
        projected_total = (acc_actual + svc_total + pos_total - discount).quantize(Q)

    projected_refund = max(Decimal("0"), (paid - projected_total).quantize(Q))
    projected_due = max(Decimal("0"), (projected_total - paid).quantize(Q))

    return DepartureSettlementPreview(
        check_in=booking.check_in,
        scheduled_check_out=scheduled_out,
        actual_departure=departure,
        booked_nights=booked_nights or bill_nights,
        stayed_nights=stayed_nights,
        cancelled_nights=cancelled_nights,
        accommodation_booked=acc_booked,
        accommodation_actual=acc_actual,
        accommodation_saved=(acc_booked - acc_actual).quantize(Q),
        services_total=svc_total,
        pos_total=pos_total,
        discount=discount,
        # الرسمي = نفس guest_account في الشريط / حساب الزبون / المحفظة المرتبطة بالحجز
        folio_total_booked=folio_total,
        folio_total_actual=folio_total,
        paid_amount=paid,
        refund_due=guest.amount_credit,
        balance_due=guest.amount_due,
        is_early_departure=bool(is_early and departure < booking.check_out and not is_late),
        projected_folio_total=projected_total,
        projected_refund_due=projected_refund,
        projected_balance_due=projected_due,
    )


def _accommodation_bill_nights_from_booking(
    booking: HotelBooking,
    *,
    old_check_out: date,
    old_acc: Decimal,
) -> int:
    """عدد الليالي التي يُغطّيها accommodation_total — قد يختلف عن فترة التقويم."""
    stay_start = getattr(booking, "first_chargeable_night", None) or booking.check_in
    calendar_nights = max(0, (old_check_out - stay_start).days)
    if calendar_nights <= 0:
        return 1
    nightly = Decimal(str(booking.nightly_rate or 0)).quantize(Q)
    if nightly > 0 and old_acc > 0:
        priced_nights = int((old_acc / nightly).quantize(Q))
        if priced_nights < 1:
            priced_nights = 1
        if priced_nights < calendar_nights:
            return priced_nights
    return calendar_nights


def accommodation_after_early_departure(
    booking: HotelBooking,
    *,
    actual_departure: date,
    old_check_out: date,
) -> Decimal:
    """تخفض الإقامة المخزّنة بنسبة الليالي — عند تقديم/تسجيل المغادرة."""
    old_acc = Decimal(str(booking.accommodation_total or 0)).quantize(Q)
    if old_acc <= 0:
        return old_acc
    stay_start = getattr(booking, "first_chargeable_night", None) or booking.check_in
    calendar_nights = max(0, (old_check_out - stay_start).days)
    bill_nights = _accommodation_bill_nights_from_booking(
        booking, old_check_out=old_check_out, old_acc=old_acc
    )
    if bill_nights <= 0:
        return old_acc
    if actual_departure <= stay_start:
        stayed = 1
    else:
        stayed = max(0, (actual_departure - stay_start).days)
    stayed = min(max(stayed, 0), bill_nights)
    if stayed <= 0:
        stayed = 1 if old_acc > 0 else 0
    return (old_acc * Decimal(stayed) / Decimal(bill_nights)).quantize(Q)
