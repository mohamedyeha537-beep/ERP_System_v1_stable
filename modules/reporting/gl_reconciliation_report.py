"""تقرير مطابقة GL — إيراد تشغيلي مقابل حسابات 4100 / 4150."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.datetime_local import local_day_start_utc, to_local
from modules.platform.business_domain import BusinessDomain


@dataclass
class GlReconSegmentSummary:
    segment: str
    segment_label: str
    account_code: str
    account_name: str
    account_id: int | None
    operational_net: Decimal
    gl_revenue_net: Decimal
    gap: Decimal
    operational_label: str


@dataclass
class GlReconDayRow:
    day: date
    restaurant_operational: Decimal | None = None
    restaurant_gl: Decimal | None = None
    restaurant_gap: Decimal | None = None
    hotel_operational: Decimal | None = None
    hotel_gl: Decimal | None = None
    hotel_gap: Decimal | None = None


@dataclass
class GlReconciliationReport:
    gl_enabled: bool
    from_date: date
    to_date: date
    segments: list[GlReconSegmentSummary]
    daily_rows: list[GlReconDayRow]
    total_gap: Decimal
    mismatch_days: int
    show_restaurant: bool
    show_hotel: bool


def _iter_period_days(start: datetime, end: datetime) -> list[date]:
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
    return days


def _restaurant_daily_net(db: Session, day_start: datetime, day_end: datetime) -> Decimal:
    from modules.reporting.queries import payment_flow_totals, sales_by_payment_method

    rows = sales_by_payment_method(db, day_start, day_end)
    _, _, net = payment_flow_totals(rows)
    return net.quantize(Decimal("0.001"))


def _restaurant_gl_net_for_day(db: Session, day: date, *, account_id: int) -> Decimal:
    from modules.gl.reports import account_period_movement

    deb, cred = account_period_movement(
        db, account_id, day, day, domain=BusinessDomain.RESTAURANT
    )
    return (cred - deb).quantize(Decimal("0.001"))


def _load_gl_account(db: Session, code: str) -> tuple[int | None, str]:
    from modules.gl.models import GlAccount

    acc = db.scalar(select(GlAccount).where(GlAccount.code == code))
    if acc is None:
        return None, ""
    return int(acc.id), str(acc.name_ar or "")


def build_gl_reconciliation_report(
    db: Session, start: datetime, end: datetime, domain=None
) -> GlReconciliationReport:
    from modules.gl.posting import CODE_HOTEL_REVENUE, CODE_REVENUE
    from modules.gl.service import is_gl_enabled
    from modules.hotel.revenue_stats import hotel_cash_collected
    from modules.reporting.comprehensive_helpers import (
        hotel_gl_net_for_day,
        hotel_gl_reconciliation_for_period,
        period_local_dates,
        restaurant_gl_reconciliation_for_period,
    )
    from modules.reporting.domain_financials import build_domain_period_financials

    from_d, to_d = period_local_dates(start, end)
    show_restaurant = domain in (None, BusinessDomain.RESTAURANT)
    show_hotel = domain in (None, BusinessDomain.HOTEL)
    gl_on = is_gl_enabled(db)

    segments: list[GlReconSegmentSummary] = []
    if show_restaurant:
        pos_fin = build_domain_period_financials(
            db, start, end, domain=BusinessDomain.RESTAURANT
        )
        pos_gl = restaurant_gl_reconciliation_for_period(
            db, start, end, operational_net=pos_fin.net_collected
        )
        if pos_gl is not None:
            segments.append(
                GlReconSegmentSummary(
                    segment="restaurant",
                    segment_label="مطعم",
                    account_code=pos_gl.account_code,
                    account_name=pos_gl.account_name,
                    account_id=pos_gl.account_id,
                    operational_net=pos_gl.operational_net,
                    gl_revenue_net=pos_gl.gl_revenue_net,
                    gap=pos_gl.gap,
                    operational_label="صافي تحصيل POS",
                )
            )
        elif not gl_on:
            segments.append(
                GlReconSegmentSummary(
                    segment="restaurant",
                    segment_label="مطعم",
                    account_code=CODE_REVENUE,
                    account_name="إيرادات المبيعات",
                    account_id=None,
                    operational_net=pos_fin.net_collected,
                    gl_revenue_net=Decimal("0"),
                    gap=pos_fin.net_collected,
                    operational_label="صافي تحصيل POS",
                )
            )

    if show_hotel:
        hotel_fin = build_domain_period_financials(
            db, start, end, domain=BusinessDomain.HOTEL
        )
        hotel_gl = hotel_gl_reconciliation_for_period(
            db, start, end, operational_net=hotel_fin.net_collected
        )
        if hotel_gl is not None:
            segments.append(
                GlReconSegmentSummary(
                    segment="hotel",
                    segment_label="فندق",
                    account_code=hotel_gl.account_code,
                    account_name=hotel_gl.account_name,
                    account_id=hotel_gl.account_id,
                    operational_net=hotel_gl.operational_net,
                    gl_revenue_net=hotel_gl.gl_revenue_net,
                    gap=hotel_gl.gap,
                    operational_label="صافي تحصيل الحجز",
                )
            )
        elif not gl_on:
            segments.append(
                GlReconSegmentSummary(
                    segment="hotel",
                    segment_label="فندق",
                    account_code=CODE_HOTEL_REVENUE,
                    account_name="إيرادات الإقامة (فندq)",
                    account_id=None,
                    operational_net=hotel_fin.net_collected,
                    gl_revenue_net=Decimal("0"),
                    gap=hotel_fin.net_collected,
                    operational_label="صافي تحصيل الحجز",
                )
            )

    rest_acc_id: int | None = None
    hotel_acc_id: int | None = None
    if gl_on:
        rest_acc_id, _ = _load_gl_account(db, CODE_REVENUE)
        hotel_acc_id, _ = _load_gl_account(db, CODE_HOTEL_REVENUE)

    daily_rows: list[GlReconDayRow] = []
    mismatch_days = 0
    total_gap = Decimal("0")

    for day in reversed(_iter_period_days(start, end)):
        row = GlReconDayRow(day=day)
        day_has_mismatch = False
        if show_restaurant:
            ds = local_day_start_utc(day)
            de = local_day_start_utc(day + timedelta(days=1))
            op = _restaurant_daily_net(db, ds, de)
            gl_net: Decimal | None = None
            gap: Decimal | None = None
            if rest_acc_id is not None:
                gl_net = _restaurant_gl_net_for_day(db, day, account_id=rest_acc_id)
                gap = (op - gl_net).quantize(Decimal("0.001"))
                if gap != 0:
                    day_has_mismatch = True
                total_gap += abs(gap)
            row.restaurant_operational = op
            row.restaurant_gl = gl_net
            row.restaurant_gap = gap
        if show_hotel:
            ds = local_day_start_utc(day)
            de = local_day_start_utc(day + timedelta(days=1))
            hop = hotel_cash_collected(db, ds, de)
            hgl: Decimal | None = None
            hgap: Decimal | None = None
            if hotel_acc_id is not None:
                hgl = hotel_gl_net_for_day(db, day, account_id=hotel_acc_id)
                hgap = (hop - hgl).quantize(Decimal("0.001"))
                if hgap != 0:
                    day_has_mismatch = True
                total_gap += abs(hgap)
            row.hotel_operational = hop
            row.hotel_gl = hgl
            row.hotel_gap = hgap
        if day_has_mismatch:
            mismatch_days += 1
        daily_rows.append(row)

    if not gl_on:
        total_gap = sum((abs(s.gap) for s in segments), Decimal("0"))

    return GlReconciliationReport(
        gl_enabled=gl_on,
        from_date=from_d,
        to_date=to_d,
        segments=segments,
        daily_rows=daily_rows,
        total_gap=total_gap.quantize(Decimal("0.001")),
        mismatch_days=mismatch_days,
        show_restaurant=show_restaurant,
        show_hotel=show_hotel,
    )


def gl_reconciliation_csv_rows(
    report: GlReconciliationReport, domain_label: str, start: datetime, end: datetime
) -> list[list]:
    from app.datetime_local import format_local_dt

    rows: list[list] = [
        ["الفترة", f"{format_local_dt(start, '%Y-%m-%d')} → {format_local_dt(end, '%Y-%m-%d')}"],
        ["مجال التقرير", domain_label],
        ["GL مفعّل", "نعم" if report.gl_enabled else "لا"],
        ["أيام بفارق", report.mismatch_days],
        ["مجموع الفوارق (مطلق)", report.total_gap],
        [],
    ]
    for seg in report.segments:
        rows.extend(
            [
                [f"—— {seg.segment_label} — حساب {seg.account_code} —", ""],
                [seg.operational_label, seg.operational_net],
                ["صافي دائن GL", seg.gl_revenue_net],
                ["الفارق (تشغيل − GL)", seg.gap],
                [],
            ]
        )
    rows.append(["—— تفصيل يومي ——", ""])
    header = ["التاريخ"]
    if report.show_restaurant:
        header.extend(["POS تشغيلي", "4100 GL", "فارق مطعم"])
    if report.show_hotel:
        header.extend(["فندق تشغيلي", "4150 GL", "فارق فندق"])
    rows.append(header)
    for r in report.daily_rows:
        line: list = [r.day]
        if report.show_restaurant:
            line.extend(
                [
                    r.restaurant_operational if r.restaurant_operational is not None else "",
                    r.restaurant_gl if r.restaurant_gl is not None else "",
                    r.restaurant_gap if r.restaurant_gap is not None else "",
                ]
            )
        if report.show_hotel:
            line.extend(
                [
                    r.hotel_operational if r.hotel_operational is not None else "",
                    r.hotel_gl if r.hotel_gl is not None else "",
                    r.hotel_gap if r.hotel_gap is not None else "",
                ]
            )
        rows.append(line)
    return rows
