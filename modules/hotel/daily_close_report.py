"""تقرير الإقفال اليومي للفندق — إشغال، حركات، وتحصيلات لكل يوم."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.datetime_local import local_day_start_utc, to_local
from modules.authz.models import User
from modules.hotel.booking_models import (
    BookingStatus,
    HotelBooking,
    HotelBookingPayment,
    HotelDailyClosing,
)
from modules.hotel.finance_service import occupancy_stats
from modules.hotel.revenue_stats import hotel_cash_collected


@dataclass
class HotelDailyCloseDayRow:
    day: date
    is_closed: bool
    closing_id: int | None
    bookings_new: int
    check_ins: int
    check_outs: int
    cancellations: int
    no_shows: int
    revenue_net: Decimal
    deposits_total: Decimal
    occupancy_rate: float
    occupied: int
    total_rooms: int
    closed_by: str | None
    closed_at: datetime | None
    notes: str | None
    gl_revenue_net: Decimal | None = None
    gl_gap: Decimal | None = None


@dataclass
class HotelDailyClosePeriodSummary:
    day_count: int
    closed_days: int
    open_days: int
    total_revenue: Decimal
    total_deposits: Decimal
    total_check_ins: int
    total_check_outs: int
    avg_occupancy: float
    gl_gap_total: Decimal = Decimal("0")
    days_with_gl_mismatch: int = 0


def _day_counts(db: Session, closing_date: date) -> tuple[int, int, int, int, int]:
    bookings_new = int(
        db.scalar(
            select(func.count())
            .select_from(HotelBooking)
            .where(func.date(HotelBooking.created_at) == closing_date)
        )
        or 0
    )
    check_ins = int(
        db.scalar(
            select(func.count())
            .select_from(HotelBooking)
            .where(
                HotelBooking.booking_status == BookingStatus.CHECKED_IN,
                func.date(HotelBooking.checked_in_at) == closing_date,
            )
        )
        or 0
    )
    check_outs = int(
        db.scalar(
            select(func.count())
            .select_from(HotelBooking)
            .where(
                HotelBooking.booking_status == BookingStatus.CHECKED_OUT,
                func.date(HotelBooking.checked_out_at) == closing_date,
            )
        )
        or 0
    )
    cancellations = int(
        db.scalar(
            select(func.count())
            .select_from(HotelBooking)
            .where(
                HotelBooking.booking_status == BookingStatus.CANCELLED,
                func.date(HotelBooking.updated_at) == closing_date,
            )
        )
        or 0
    )
    no_shows = int(
        db.scalar(
            select(func.count())
            .select_from(HotelBooking)
            .where(
                HotelBooking.booking_status == BookingStatus.NO_SHOW,
                func.date(HotelBooking.updated_at) == closing_date,
            )
        )
        or 0
    )
    return bookings_new, check_ins, check_outs, cancellations, no_shows


def _day_deposits(db: Session, day_start: datetime, day_end: datetime) -> Decimal:
    total = db.scalar(
        select(func.coalesce(func.sum(HotelBookingPayment.amount), 0)).where(
            HotelBookingPayment.created_at >= day_start,
            HotelBookingPayment.created_at < day_end,
            HotelBookingPayment.is_deposit.is_(True),
        )
    )
    return Decimal(str(total or 0)).quantize(Decimal("0.001"))


def _closed_by_label(db: Session, user_id: int | None) -> str | None:
    if user_id is None:
        return None
    user = db.get(User, user_id)
    if user is None:
        return None
    return user.username or f"#{user.id}"


def _gl_account_id_for_hotel_revenue(db: Session) -> int | None:
    from modules.gl.models import GlAccount
    from modules.gl.posting import CODE_HOTEL_REVENUE
    from modules.gl.service import is_gl_enabled

    if not is_gl_enabled(db):
        return None
    acc = db.scalar(select(GlAccount).where(GlAccount.code == CODE_HOTEL_REVENUE))
    return int(acc.id) if acc is not None else None


def _day_gl_fields(
    db: Session, day: date, revenue_net: Decimal, *, gl_account_id: int | None
) -> tuple[Decimal | None, Decimal | None]:
    if gl_account_id is None:
        return None, None
    from modules.reporting.comprehensive_helpers import hotel_gl_net_for_day

    gl_net = hotel_gl_net_for_day(db, day, account_id=gl_account_id)
    gap = (revenue_net - gl_net).quantize(Decimal("0.001"))
    return gl_net, gap


def _iter_days(start: datetime, end: datetime) -> list[date]:
    start_local = to_local(start)
    end_local = to_local(end)
    if not isinstance(start_local, datetime) or not isinstance(end_local, datetime):
        return []
    cur = start_local.date()
    end_exclusive = end_local.date()
    days: list[date] = []
    while cur < end_exclusive:
        days.append(cur)
        cur += timedelta(days=1)
    days.reverse()
    return days


def hotel_daily_close_report(
    db: Session, start: datetime, end: datetime
) -> tuple[list[HotelDailyCloseDayRow], HotelDailyClosePeriodSummary]:
    days = _iter_days(start, end)
    if not days:
        empty = HotelDailyClosePeriodSummary(
            day_count=0,
            closed_days=0,
            open_days=0,
            total_revenue=Decimal("0"),
            total_deposits=Decimal("0"),
            total_check_ins=0,
            total_check_outs=0,
            avg_occupancy=0.0,
            gl_gap_total=Decimal("0"),
            days_with_gl_mismatch=0,
        )
        return [], empty

    gl_account_id = _gl_account_id_for_hotel_revenue(db)
    closings = {
        c.closing_date: c
        for c in db.scalars(
            select(HotelDailyClosing).where(
                HotelDailyClosing.closing_date >= days[-1],
                HotelDailyClosing.closing_date <= days[0],
            )
        ).all()
    }

    rows: list[HotelDailyCloseDayRow] = []
    closed_days = 0
    total_revenue = Decimal("0")
    total_deposits = Decimal("0")
    total_check_ins = 0
    total_check_outs = 0
    occ_sum = 0.0
    gl_gap_total = Decimal("0")
    days_with_gl_mismatch = 0

    for day in days:
        closing = closings.get(day)
        occ = occupancy_stats(db, on_date=day)
        if closing is not None:
            closed_days += 1
            rev = Decimal(str(closing.revenue_total or 0)).quantize(Decimal("0.001"))
            if closing.gl_revenue_net is not None and closing.gl_gap is not None:
                gl_net = Decimal(str(closing.gl_revenue_net)).quantize(Decimal("0.001"))
                gl_gap = Decimal(str(closing.gl_gap)).quantize(Decimal("0.001"))
            else:
                gl_net, gl_gap = _day_gl_fields(db, day, rev, gl_account_id=gl_account_id)
            if gl_gap is not None and gl_gap != 0:
                days_with_gl_mismatch += 1
                gl_gap_total += gl_gap
            rows.append(
                HotelDailyCloseDayRow(
                    day=day,
                    is_closed=True,
                    closing_id=int(closing.id),
                    bookings_new=int(closing.bookings_new),
                    check_ins=int(closing.check_ins),
                    check_outs=int(closing.check_outs),
                    cancellations=int(closing.cancellations),
                    no_shows=int(closing.no_shows),
                    revenue_net=rev,
                    deposits_total=Decimal(str(closing.deposits_total or 0)).quantize(
                        Decimal("0.001")
                    ),
                    occupancy_rate=float(occ["occupancy_rate"]),
                    occupied=int(occ["occupied"]),
                    total_rooms=int(occ["total_rooms"]),
                    closed_by=_closed_by_label(db, closing.closed_by_id),
                    closed_at=closing.closed_at,
                    notes=closing.notes,
                    gl_revenue_net=gl_net,
                    gl_gap=gl_gap,
                )
            )
            total_revenue += rev
            total_deposits += Decimal(str(closing.deposits_total or 0))
            total_check_ins += int(closing.check_ins)
            total_check_outs += int(closing.check_outs)
        else:
            day_start = local_day_start_utc(day)
            day_end = local_day_start_utc(day + timedelta(days=1))
            bn, ci, co, ca, ns = _day_counts(db, day)
            rev = hotel_cash_collected(db, day_start, day_end)
            dep = _day_deposits(db, day_start, day_end)
            gl_net, gl_gap = _day_gl_fields(db, day, rev, gl_account_id=gl_account_id)
            if gl_gap is not None and gl_gap != 0:
                days_with_gl_mismatch += 1
                gl_gap_total += gl_gap
            rows.append(
                HotelDailyCloseDayRow(
                    day=day,
                    is_closed=False,
                    closing_id=None,
                    bookings_new=bn,
                    check_ins=ci,
                    check_outs=co,
                    cancellations=ca,
                    no_shows=ns,
                    revenue_net=rev,
                    deposits_total=dep,
                    occupancy_rate=float(occ["occupancy_rate"]),
                    occupied=int(occ["occupied"]),
                    total_rooms=int(occ["total_rooms"]),
                    closed_by=None,
                    closed_at=None,
                    notes=None,
                    gl_revenue_net=gl_net,
                    gl_gap=gl_gap,
                )
            )
            total_revenue += rev
            total_deposits += dep
            total_check_ins += ci
            total_check_outs += co
        occ_sum += float(occ["occupancy_rate"])

    summary = HotelDailyClosePeriodSummary(
        day_count=len(days),
        closed_days=closed_days,
        open_days=len(days) - closed_days,
        total_revenue=total_revenue.quantize(Decimal("0.001")),
        total_deposits=total_deposits.quantize(Decimal("0.001")),
        total_check_ins=total_check_ins,
        total_check_outs=total_check_outs,
        avg_occupancy=round(occ_sum / len(days), 1) if days else 0.0,
        gl_gap_total=gl_gap_total.quantize(Decimal("0.001")),
        days_with_gl_mismatch=days_with_gl_mismatch,
    )
    return rows, summary
