"""تقرير حجوزات الفندق — استحقاق الإقامة مقابل التحصيل."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.hotel.booking_models import BookingPaymentStatus, BookingStatus, HotelBooking


BOOKING_STATUS_LABELS: dict[str, str] = {
    BookingStatus.PENDING.value: "معلّق",
    BookingStatus.CONFIRMED.value: "مؤكّد",
    BookingStatus.CHECKED_IN.value: "مسكّن",
    BookingStatus.CHECKED_OUT.value: "مغادر",
    BookingStatus.CANCELLED.value: "ملغى",
    BookingStatus.NO_SHOW.value: "لم يحضر",
}

PAYMENT_STATUS_LABELS: dict[str, str] = {
    BookingPaymentStatus.UNPAID.value: "غير مدفوع",
    BookingPaymentStatus.PARTIALLY_PAID.value: "مدفوع جزئياً",
    BookingPaymentStatus.FULLY_PAID.value: "مدفوع بالكامل",
    BookingPaymentStatus.REFUNDED.value: "مُسترد",
}


@dataclass
class HotelBookingReportRow:
    booking_id: int
    reference: str
    guest_name: str
    check_in: date
    check_out: date
    nights: int
    booking_status: str
    booking_status_label: str
    payment_status: str
    payment_status_label: str
    accommodation_total: Decimal
    paid_amount: Decimal
    balance: Decimal


@dataclass
class HotelBookingsReportSummary:
    booking_count: int
    nights_total: int
    accommodation_total: Decimal
    paid_total: Decimal
    balance_total: Decimal
    checked_in_count: int
    checked_out_count: int


def _period_dates(start: datetime, end: datetime) -> tuple[date, date]:
    s = start.date() if isinstance(start, datetime) else start
    e = end.date() if isinstance(end, datetime) else end
    return s, e


def _booking_balance(db: Session, booking: HotelBooking) -> Decimal:
    from modules.hotel.folio import build_folio

    bal = build_folio(db, booking.id).balance
    return bal if bal > 0 else Decimal("0")


def _booking_to_row(db: Session, booking: HotelBooking) -> HotelBookingReportRow:
    from modules.hotel.folio import build_folio

    folio = build_folio(db, booking.id)
    acc = folio.accommodation
    paid = folio.paid
    bal = _booking_balance(db, booking)
    nights = max((booking.check_out - booking.check_in).days, 0)
    bs = (
        booking.booking_status.value
        if hasattr(booking.booking_status, "value")
        else str(booking.booking_status)
    )
    ps = (
        booking.payment_status.value
        if hasattr(booking.payment_status, "value")
        else str(booking.payment_status)
    )
    return HotelBookingReportRow(
        booking_id=int(booking.id),
        reference=str(booking.reference or f"#{booking.id}"),
        guest_name=str(booking.guest_name or "—"),
        check_in=booking.check_in,
        check_out=booking.check_out,
        nights=nights,
        booking_status=bs,
        booking_status_label=BOOKING_STATUS_LABELS.get(bs, bs),
        payment_status=ps,
        payment_status_label=PAYMENT_STATUS_LABELS.get(ps, ps),
        accommodation_total=acc,
        paid_amount=paid,
        balance=bal,
    )


@dataclass
class HotelOpenBalancesSummary:
    booking_count: int
    balance_total: Decimal
    checked_in_count: int
    checked_in_balance: Decimal
    overdue_count: int
    overdue_balance: Decimal


def hotel_open_balances(db: Session) -> tuple[list[HotelBookingReportRow], HotelOpenBalancesSummary]:
    """حجوزات نشطة لها رصيد متبقٍ (استحقاق − مدفوع > 0) — بدون تقييد بفترة."""
    today = date.today()
    bookings = list(
        db.scalars(
            select(HotelBooking)
            .where(
                HotelBooking.booking_status.notin_(
                    (BookingStatus.CANCELLED, BookingStatus.NO_SHOW)
                ),
            )
            .order_by(HotelBooking.check_in, HotelBooking.id)
        ).all()
    )
    bookings.sort(key=lambda b: (_booking_balance(db, b), b.check_in), reverse=True)

    filtered: list[tuple[HotelBooking, HotelBookingReportRow]] = []
    for b in bookings:
        row = _booking_to_row(db, b)
        if row.balance > 0:
            filtered.append((b, row))
    bookings = [b for b, _ in filtered]
    rows = [r for _, r in filtered]
    balance_total = Decimal("0")
    checked_in_count = 0
    checked_in_balance = Decimal("0")
    overdue_count = 0
    overdue_balance = Decimal("0")

    for b, row in zip(bookings, rows):
        balance_total += row.balance
        if b.booking_status == BookingStatus.CHECKED_IN:
            checked_in_count += 1
            checked_in_balance += row.balance
        elif b.check_out < today:
            overdue_count += 1
            overdue_balance += row.balance

    summary = HotelOpenBalancesSummary(
        booking_count=len(rows),
        balance_total=balance_total.quantize(Decimal("0.001")),
        checked_in_count=checked_in_count,
        checked_in_balance=checked_in_balance.quantize(Decimal("0.001")),
        overdue_count=overdue_count,
        overdue_balance=overdue_balance.quantize(Decimal("0.001")),
    )
    return rows, summary


def hotel_bookings_in_period(
    db: Session, start: datetime, end: datetime
) -> tuple[list[HotelBookingReportRow], HotelBookingsReportSummary]:
    """حجوزات تتقاطع مع الفترة (check_in < end و check_out > start)."""
    s_date, e_date = _period_dates(start, end)
    bookings = list(
        db.scalars(
            select(HotelBooking)
            .where(
                HotelBooking.check_in < e_date,
                HotelBooking.check_out > s_date,
                HotelBooking.booking_status.notin_(
                    (BookingStatus.CANCELLED, BookingStatus.NO_SHOW)
                ),
            )
            .order_by(HotelBooking.check_in, HotelBooking.id)
        ).all()
    )

    rows: list[HotelBookingReportRow] = []
    acc_total = Decimal("0")
    paid_total = Decimal("0")
    balance_total = Decimal("0")
    nights_total = 0
    checked_in = 0
    checked_out = 0

    for b in bookings:
        row = _booking_to_row(db, b)
        if b.booking_status == BookingStatus.CHECKED_IN:
            checked_in += 1
        if b.booking_status == BookingStatus.CHECKED_OUT:
            checked_out += 1
        acc_total += row.accommodation_total
        paid_total += row.paid_amount
        balance_total += row.balance
        nights_total += row.nights
        rows.append(row)

    summary = HotelBookingsReportSummary(
        booking_count=len(rows),
        nights_total=nights_total,
        accommodation_total=acc_total.quantize(Decimal("0.001")),
        paid_total=paid_total.quantize(Decimal("0.001")),
        balance_total=balance_total.quantize(Decimal("0.001")),
        checked_in_count=checked_in,
        checked_out_count=checked_out,
    )
    return rows, summary
