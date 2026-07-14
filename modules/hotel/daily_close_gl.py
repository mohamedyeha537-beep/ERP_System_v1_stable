"""ترحيل GL عند إقفال يوم الفندق — backfill + لقطة مطابقة 4150."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.datetime_local import local_day_start_utc
from modules.hotel.booking_models import HotelBookingPayment, HotelBookingPaymentRefund
from modules.hotel.revenue_stats import hotel_cash_collected


@dataclass
class HotelDailyCloseGlResult:
    gl_enabled: bool
    backfilled_payments: int
    backfilled_refunds: int
    operational_net: Decimal
    gl_revenue_net: Decimal | None
    gl_gap: Decimal | None


def _day_bounds(closing_date: date) -> tuple:
    start = local_day_start_utc(closing_date)
    end = local_day_start_utc(closing_date + timedelta(days=1))
    return start, end


def backfill_hotel_day_gl(db: Session, closing_date: date) -> tuple[int, int]:
    """يرحّل مدفوعات/مرتجعات اليوم الناقصة في GL (idempotent)."""
    from modules.gl.posting import (
        post_hotel_booking_payment_refund_shadow,
        post_hotel_booking_payment_shadow,
    )
    from modules.gl.service import is_gl_enabled

    if not is_gl_enabled(db):
        return 0, 0

    day_start, day_end = _day_bounds(closing_date)
    payments = list(
        db.scalars(
            select(HotelBookingPayment).where(
                HotelBookingPayment.created_at >= day_start,
                HotelBookingPayment.created_at < day_end,
            )
        ).all()
    )
    refunds = list(
        db.scalars(
            select(HotelBookingPaymentRefund).where(
                HotelBookingPaymentRefund.created_at >= day_start,
                HotelBookingPaymentRefund.created_at < day_end,
            )
        ).all()
    )

    pay_count = 0
    for hp in payments:
        if post_hotel_booking_payment_shadow(db, hp):
            pay_count += 1
    refund_count = 0
    for ref in refunds:
        if post_hotel_booking_payment_refund_shadow(db, ref):
            refund_count += 1
    return pay_count, refund_count


def snapshot_hotel_day_gl(
    db: Session, closing_date: date, *, operational_net: Decimal | None = None
) -> HotelDailyCloseGlResult:
    from modules.gl.models import GlAccount
    from modules.gl.posting import CODE_HOTEL_REVENUE
    from modules.gl.service import is_gl_enabled
    from modules.reporting.comprehensive_helpers import hotel_gl_net_for_day

    day_start, day_end = _day_bounds(closing_date)
    op = (
        operational_net
        if operational_net is not None
        else hotel_cash_collected(db, day_start, day_end)
    ).quantize(Decimal("0.001"))

    if not is_gl_enabled(db):
        return HotelDailyCloseGlResult(
            gl_enabled=False,
            backfilled_payments=0,
            backfilled_refunds=0,
            operational_net=op,
            gl_revenue_net=None,
            gl_gap=None,
        )

    acc = db.scalar(select(GlAccount).where(GlAccount.code == CODE_HOTEL_REVENUE))
    if acc is None:
        return HotelDailyCloseGlResult(
            gl_enabled=True,
            backfilled_payments=0,
            backfilled_refunds=0,
            operational_net=op,
            gl_revenue_net=Decimal("0"),
            gl_gap=op,
        )

    gl_net = hotel_gl_net_for_day(db, closing_date, account_id=int(acc.id))
    gap = (op - gl_net).quantize(Decimal("0.001"))
    return HotelDailyCloseGlResult(
        gl_enabled=True,
        backfilled_payments=0,
        backfilled_refunds=0,
        operational_net=op,
        gl_revenue_net=gl_net,
        gl_gap=gap,
    )


def run_hotel_daily_close_gl(
    db: Session, closing_date: date, *, operational_net: Decimal | None = None
) -> HotelDailyCloseGlResult:
    pay_n, ref_n = backfill_hotel_day_gl(db, closing_date)
    snap = snapshot_hotel_day_gl(db, closing_date, operational_net=operational_net)
    return HotelDailyCloseGlResult(
        gl_enabled=snap.gl_enabled,
        backfilled_payments=pay_n,
        backfilled_refunds=ref_n,
        operational_net=snap.operational_net,
        gl_revenue_net=snap.gl_revenue_net,
        gl_gap=snap.gl_gap,
    )
