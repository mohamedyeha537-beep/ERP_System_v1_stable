"""تقرير الإقفال اليومي للمطعم — جلسات، مبيعات، وصافي التحصيل + مطابقة GL 4100."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.datetime_local import local_day_start_utc, to_local
from modules.platform.business_domain import BusinessDomain
from modules.pos_shifts.models import PosShift, PosShiftStatus
from modules.sales.models import Sale, SaleStatus


@dataclass
class PosDailyCloseDayRow:
    day: date
    revenue_net: Decimal
    sales_count: int
    shifts_opened: int
    shifts_closed: int
    gl_revenue_net: Decimal | None = None
    gl_gap: Decimal | None = None


@dataclass
class PosDailyClosePeriodSummary:
    day_count: int
    total_revenue: Decimal
    total_sales: int
    total_shifts_opened: int
    total_shifts_closed: int
    gl_gap_total: Decimal = Decimal("0")
    days_with_gl_mismatch: int = 0


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


def _day_revenue_net(db: Session, day_start: datetime, day_end: datetime) -> Decimal:
    from modules.reporting.domain_financials import domain_sales_by_payment_method
    from modules.reporting.queries import payment_flow_totals

    rows = domain_sales_by_payment_method(
        db, day_start, day_end, domain=BusinessDomain.RESTAURANT
    )
    _, _, net = payment_flow_totals(rows)
    return net.quantize(Decimal("0.001"))


def _day_sales_count(db: Session, day_start: datetime, day_end: datetime) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(Sale)
            .where(
                Sale.status == SaleStatus.COMPLETED,
                Sale.created_at >= day_start,
                Sale.created_at < day_end,
            )
        )
        or 0
    )


def _shifts_opened(db: Session, day_start: datetime, day_end: datetime) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(PosShift)
            .where(
                PosShift.opened_at >= day_start,
                PosShift.opened_at < day_end,
            )
        )
        or 0
    )


def _shifts_closed(db: Session, day_start: datetime, day_end: datetime) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(PosShift)
            .where(
                PosShift.status == PosShiftStatus.CLOSED,
                PosShift.closed_at.isnot(None),
                PosShift.closed_at >= day_start,
                PosShift.closed_at < day_end,
            )
        )
        or 0
    )


def _gl_account_id_for_revenue(db: Session) -> int | None:
    from modules.gl.models import GlAccount
    from modules.gl.posting import CODE_REVENUE
    from modules.gl.service import is_gl_enabled

    if not is_gl_enabled(db):
        return None
    acc = db.scalar(select(GlAccount).where(GlAccount.code == CODE_REVENUE))
    return int(acc.id) if acc is not None else None


def _day_gl_fields(
    db: Session, day: date, revenue_net: Decimal, *, gl_account_id: int | None
) -> tuple[Decimal | None, Decimal | None]:
    if gl_account_id is None:
        return None, None
    from modules.reporting.comprehensive_helpers import period_local_dates
    from modules.reporting.gl_reconciliation_report import _restaurant_gl_net_for_day

    gl_net = _restaurant_gl_net_for_day(db, day, account_id=gl_account_id)
    gap = (revenue_net - gl_net).quantize(Decimal("0.001"))
    return gl_net, gap


def pos_daily_close_report(
    db: Session, start: datetime, end: datetime
) -> tuple[list[PosDailyCloseDayRow], PosDailyClosePeriodSummary]:
    days = _iter_days(start, end)
    if not days:
        empty = PosDailyClosePeriodSummary(
            day_count=0,
            total_revenue=Decimal("0"),
            total_sales=0,
            total_shifts_opened=0,
            total_shifts_closed=0,
            gl_gap_total=Decimal("0"),
            days_with_gl_mismatch=0,
        )
        return [], empty

    gl_account_id = _gl_account_id_for_revenue(db)
    rows: list[PosDailyCloseDayRow] = []
    total_revenue = Decimal("0")
    total_sales = 0
    total_opened = 0
    total_closed = 0
    gl_gap_total = Decimal("0")
    days_with_gl_mismatch = 0

    for day in days:
        day_start = local_day_start_utc(day)
        day_end = local_day_start_utc(day + timedelta(days=1))
        rev = _day_revenue_net(db, day_start, day_end)
        sales_n = _day_sales_count(db, day_start, day_end)
        opened = _shifts_opened(db, day_start, day_end)
        closed = _shifts_closed(db, day_start, day_end)
        gl_net, gl_gap = _day_gl_fields(db, day, rev, gl_account_id=gl_account_id)
        if gl_gap is not None and gl_gap != 0:
            days_with_gl_mismatch += 1
            gl_gap_total += gl_gap
        rows.append(
            PosDailyCloseDayRow(
                day=day,
                revenue_net=rev,
                sales_count=sales_n,
                shifts_opened=opened,
                shifts_closed=closed,
                gl_revenue_net=gl_net,
                gl_gap=gl_gap,
            )
        )
        total_revenue += rev
        total_sales += sales_n
        total_opened += opened
        total_closed += closed

    summary = PosDailyClosePeriodSummary(
        day_count=len(days),
        total_revenue=total_revenue.quantize(Decimal("0.001")),
        total_sales=total_sales,
        total_shifts_opened=total_opened,
        total_shifts_closed=total_closed,
        gl_gap_total=gl_gap_total.quantize(Decimal("0.001")),
        days_with_gl_mismatch=days_with_gl_mismatch,
    )
    return rows, summary
