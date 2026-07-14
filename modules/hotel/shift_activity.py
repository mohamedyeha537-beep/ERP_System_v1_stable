"""إحصائيات نشاط وردية الفندق خلال فترة الجلسة."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from modules.hotel.booking_models import (
    BookingStatus,
    HotelBooking,
    HotelBookingService,
    HotelBookingStatusLog,
    RecordKind,
)
from modules.hotel.folio import is_laundry_service
from modules.hotel.models import RoomCharge
from modules.hotel.revenue_stats import hotel_collections_by_payment_method
from modules.payments.models import PaymentMethod, SalePayment
from modules.sales.models import Sale, SaleStatus


@dataclass
class HotelShiftActivitySummary:
    bookings_created: int = 0
    checkins: int = 0
    checkouts: int = 0
    services_count: int = 0
    services_total: Decimal = field(default_factory=lambda: Decimal("0"))
    laundry_count: int = 0
    laundry_total: Decimal = field(default_factory=lambda: Decimal("0"))
    meals_settled_count: int = 0
    meals_settled_total: Decimal = field(default_factory=lambda: Decimal("0"))
    meals_settled_cash: Decimal = field(default_factory=lambda: Decimal("0"))
    meals_settled_bank: Decimal = field(default_factory=lambda: Decimal("0"))
    booking_payments_cash: Decimal = field(default_factory=lambda: Decimal("0"))
    booking_payments_bank: Decimal = field(default_factory=lambda: Decimal("0"))
    booking_payment_count: int = 0
    settle_payment_count: int = 0

    @property
    def expected_cash(self) -> Decimal:
        return (self.booking_payments_cash + self.meals_settled_cash).quantize(
            Decimal("0.001")
        )

    @property
    def expected_bank(self) -> Decimal:
        return (self.booking_payments_bank + self.meals_settled_bank).quantize(
            Decimal("0.001")
        )

    @property
    def expected_booking_ops(self) -> int:
        return self.bookings_created + self.checkins + self.checkouts

    def to_dict(self) -> dict:
        return {
            "bookings_created": self.bookings_created,
            "checkins": self.checkins,
            "checkouts": self.checkouts,
            "services_count": self.services_count,
            "services_total": str(self.services_total),
            "laundry_count": self.laundry_count,
            "laundry_total": str(self.laundry_total),
            "meals_settled_count": self.meals_settled_count,
            "meals_settled_total": str(self.meals_settled_total),
            "meals_settled_cash": str(self.meals_settled_cash),
            "meals_settled_bank": str(self.meals_settled_bank),
            "booking_payments_cash": str(self.booking_payments_cash),
            "booking_payments_bank": str(self.booking_payments_bank),
            "booking_payment_count": self.booking_payment_count,
            "settle_payment_count": self.settle_payment_count,
        }


def _is_cash_method(name: str | None) -> bool:
    low = (name or "").lower()
    return "كاش" in (name or "") or "cash" in low


def _is_bank_method(name: str | None) -> bool:
    low = (name or "").lower()
    return "مصرف" in (name or "") or "bank" in low


def compute_hotel_shift_activity(
    db: Session, start: datetime, end: datetime
) -> HotelShiftActivitySummary:
    summary = HotelShiftActivitySummary()

    summary.bookings_created = int(
        db.scalar(
            select(func.count(HotelBooking.id)).where(
                HotelBooking.record_kind == RecordKind.BOOKING,
                HotelBooking.created_at >= start,
                HotelBooking.created_at < end,
            )
        )
        or 0
    )

    summary.checkins = int(
        db.scalar(
            select(func.count(HotelBookingStatusLog.id)).where(
                HotelBookingStatusLog.to_status == BookingStatus.CHECKED_IN.value,
                HotelBookingStatusLog.created_at >= start,
                HotelBookingStatusLog.created_at < end,
            )
        )
        or 0
    )

    summary.checkouts = int(
        db.scalar(
            select(func.count(HotelBookingStatusLog.id)).where(
                HotelBookingStatusLog.to_status == BookingStatus.CHECKED_OUT.value,
                HotelBookingStatusLog.created_at >= start,
                HotelBookingStatusLog.created_at < end,
            )
        )
        or 0
    )

    svc_rows = list(
        db.scalars(
            select(HotelBookingService).where(
                HotelBookingService.created_at >= start,
                HotelBookingService.created_at < end,
            )
        ).all()
    )
    summary.services_count = len(svc_rows)
    laundry_n = 0
    laundry_t = Decimal("0")
    for svc in svc_rows:
        amt = Decimal(str(svc.line_total or 0))
        summary.services_total += amt
        if is_laundry_service(svc.name_ar):
            laundry_n += 1
            laundry_t += amt
    summary.laundry_count = laundry_n
    summary.laundry_total = laundry_t.quantize(Decimal("0.001"))
    summary.services_total = summary.services_total.quantize(Decimal("0.001"))

    settled_charges = list(
        db.scalars(
            select(RoomCharge).where(
                RoomCharge.is_settled.is_(True),
                RoomCharge.settled_at >= start,
                RoomCharge.settled_at < end,
            )
        ).all()
    )
    summary.meals_settled_count = len(settled_charges)
    meal_cash = Decimal("0")
    meal_bank = Decimal("0")
    settle_pay_n = 0
    for rc in settled_charges:
        sale = db.get(Sale, rc.sale_id)
        if sale is None or sale.status != SaleStatus.COMPLETED:
            continue
        sale_total = Decimal(str(sale.total or 0)).quantize(Decimal("0.001"))
        summary.meals_settled_total += sale_total
        pay_rows = db.execute(
            select(SalePayment.amount, PaymentMethod.name_ar)
            .join(PaymentMethod, PaymentMethod.id == SalePayment.payment_method_id)
            .where(
                SalePayment.sale_id == sale.id,
                SalePayment.created_at >= start,
                SalePayment.created_at < end,
            )
        ).all()
        for amt, pm_name in pay_rows:
            settle_pay_n += 1
            val = Decimal(str(amt or 0))
            if _is_bank_method(pm_name):
                meal_bank += val
            else:
                meal_cash += val
    summary.meals_settled_total = summary.meals_settled_total.quantize(Decimal("0.001"))
    summary.meals_settled_cash = meal_cash.quantize(Decimal("0.001"))
    summary.meals_settled_bank = meal_bank.quantize(Decimal("0.001"))
    summary.settle_payment_count = settle_pay_n

    pay_count = 0
    for name, cnt, _pt, _rc, _rt, net in hotel_collections_by_payment_method(
        db, start, end
    ):
        pay_count += int(cnt or 0)
        net_amt = Decimal(str(net or 0))
        if _is_bank_method(name):
            summary.booking_payments_bank += net_amt
        else:
            summary.booking_payments_cash += net_amt
    summary.booking_payment_count = pay_count
    summary.booking_payments_cash = summary.booking_payments_cash.quantize(
        Decimal("0.001")
    )
    summary.booking_payments_bank = summary.booking_payments_bank.quantize(
        Decimal("0.001")
    )

    return summary
