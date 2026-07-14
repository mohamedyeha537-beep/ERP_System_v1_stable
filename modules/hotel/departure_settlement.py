"""تسوية المغادرة المبكرة — حساب الليالي والمبالغ المستردة."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from modules.hotel.booking_models import HotelBooking, BookingStatus
from modules.hotel.booking_service import BookingError, _recalc_booking_accommodation
from modules.hotel.folio import _booking_services_total, _pos_charges_total


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
    refund_due: Decimal
    balance_due: Decimal
    is_early_departure: bool

    def to_dict(self) -> dict:
        return {
            k: (str(v) if isinstance(v, Decimal) else v.isoformat() if isinstance(v, date) else v)
            for k, v in asdict(self).items()
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


def _acc_for_dates(
    db: Session, booking: HotelBooking, *, seg_out: date
) -> Decimal:
    return _recalc_booking_accommodation(db, booking, through_date=seg_out)


def preview_departure_settlement(
    db: Session,
    booking: HotelBooking,
    *,
    actual_departure: date | None = None,
) -> DepartureSettlementPreview:
    if booking.booking_status not in (
        BookingStatus.CHECKED_IN,
        BookingStatus.CONFIRMED,
        BookingStatus.PENDING,
    ):
        raise BookingError("معاينة التسوية متاحة قبل المغادرة فقط.")
    departure = actual_departure or booking.check_out
    if departure <= booking.check_in:
        raise BookingError("تاريخ المغادرة يجب أن يكون بعد تاريخ الوصول.")
    if departure > booking.check_out:
        raise BookingError("تاريخ المغادرة لا يمكن أن يتجاوز الموعد المجدول الحالي.")

    scheduled_out = booking.planned_check_out
    booked_nights = max(0, (scheduled_out - booking.check_in).days)
    stayed_nights = max(0, (departure - booking.check_in).days)
    cancelled_nights = max(0, booked_nights - stayed_nights)

    acc_booked = _acc_for_dates(db, booking, seg_out=scheduled_out)
    acc_actual = _acc_for_dates(db, booking, seg_out=departure)
    svc_total = _booking_services_total(db, booking.id)
    pos_total, _ = _pos_charges_total(db, booking.id)
    discount = Decimal(str(booking.discount_amount or 0)).quantize(Decimal("0.001"))
    paid = Decimal(str(booking.paid_amount or 0)).quantize(Decimal("0.001"))

    folio_booked = (acc_booked + svc_total + pos_total - discount).quantize(Decimal("0.001"))
    folio_actual = (acc_actual + svc_total + pos_total - discount).quantize(Decimal("0.001"))
    refund = max(Decimal("0"), (paid - folio_actual).quantize(Decimal("0.001")))
    balance = max(Decimal("0"), (folio_actual - paid).quantize(Decimal("0.001")))

    return DepartureSettlementPreview(
        check_in=booking.check_in,
        scheduled_check_out=scheduled_out,
        actual_departure=departure,
        booked_nights=booked_nights,
        stayed_nights=stayed_nights,
        cancelled_nights=cancelled_nights,
        accommodation_booked=acc_booked,
        accommodation_actual=acc_actual,
        accommodation_saved=(acc_booked - acc_actual).quantize(Decimal("0.001")),
        services_total=svc_total,
        pos_total=pos_total,
        discount=discount,
        folio_total_booked=folio_booked,
        folio_total_actual=folio_actual,
        paid_amount=paid,
        refund_due=refund,
        balance_due=balance,
        is_early_departure=departure < scheduled_out,
    )
