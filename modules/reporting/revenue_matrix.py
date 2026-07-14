"""تقرير إيرادات محوري — مطعم / فندق حسب يوم / أسبوع / شهر."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.datetime_local import local_day_start_utc, to_local
from modules.hotel.booking_models import HotelBookingPayment, HotelBookingPaymentRefund
from modules.platform.business_domain import BusinessDomain


_AR_MONTHS = (
    "",
    "يناير",
    "فبراير",
    "مارس",
    "أبريل",
    "مايو",
    "يونيو",
    "يوليو",
    "أغسطس",
    "سبتمبر",
    "أكتوبر",
    "نوفمبر",
    "ديسمبر",
)

GRANULARITY_LABELS = {
    "day": "يومي",
    "week": "أسبوعي",
    "month": "شهري",
}

MEASURE_LABELS = {
    "net": "صافي التحصيل",
    "gross": "إجمالي الإيراد",
    "count": "عدد العمليات",
}


@dataclass(frozen=True)
class TimeBucket:
    key: str
    label: str
    start: date
    end_exclusive: date


@dataclass
class RevenueMatrixColumn:
    key: str
    label: str
    start: date
    end_exclusive: date


@dataclass
class RevenueMatrixRow:
    segment: str
    label_ar: str
    values: dict[str, Decimal | int]
    total: Decimal | int


@dataclass
class RevenueMatrixReport:
    columns: list[RevenueMatrixColumn]
    rows: list[RevenueMatrixRow]
    granularity: str
    granularity_label: str
    measure: str
    measure_label: str
    from_date: date
    to_date: date
    show_restaurant: bool
    show_hotel: bool
    column_totals: dict[str, Decimal | int]
    grand_total: Decimal | int


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


def _month_label(d: date) -> str:
    if 1 <= d.month <= 12:
        return f"{_AR_MONTHS[d.month]} {d.year}"
    return d.strftime("%Y-%m")


def _week_label(week_start: date) -> str:
    week_end = week_start + timedelta(days=6)
    return f"أسبوع {week_start.isoformat()} → {week_end.isoformat()}"


def iter_time_buckets(start: datetime, end: datetime, granularity: str) -> list[TimeBucket]:
    days = _iter_period_days(start, end)
    if not days:
        return []

    if granularity == "day":
        out: list[TimeBucket] = []
        for d in days:
            out.append(
                TimeBucket(
                    key=d.isoformat(),
                    label=d.isoformat(),
                    start=d,
                    end_exclusive=d + timedelta(days=1),
                )
            )
        return out

    grouped: dict[date, list[date]] = {}
    if granularity == "week":
        for d in days:
            week_start = d - timedelta(days=d.weekday())
            grouped.setdefault(week_start, []).append(d)
        out = []
        for week_start in sorted(grouped.keys()):
            bucket_days = grouped[week_start]
            end_exclusive = max(bucket_days) + timedelta(days=1)
            out.append(
                TimeBucket(
                    key=week_start.isoformat(),
                    label=_week_label(week_start),
                    start=week_start,
                    end_exclusive=end_exclusive,
                )
            )
        return out

    if granularity == "month":
        month_groups: dict[tuple[int, int], list[date]] = {}
        for d in days:
            month_groups.setdefault((d.year, d.month), []).append(d)
        out = []
        for year, month in sorted(month_groups.keys()):
            bucket_days = month_groups[(year, month)]
            month_start = date(year, month, 1)
            if month == 12:
                month_end = date(year + 1, 1, 1)
            else:
                month_end = date(year, month + 1, 1)
            out.append(
                TimeBucket(
                    key=f"{year:04d}-{month:02d}",
                    label=_month_label(month_start),
                    start=month_start,
                    end_exclusive=month_end,
                )
            )
        return out

    return iter_time_buckets(start, end, "day")


def _bucket_bounds(bucket: TimeBucket) -> tuple[datetime, datetime]:
    return local_day_start_utc(bucket.start), local_day_start_utc(bucket.end_exclusive)


def _restaurant_metric(
    db: Session, start: datetime, end: datetime, measure: str
) -> Decimal | int:
    from modules.reporting.queries import payment_flow_totals, sales_by_payment_method, sales_summary

    if measure == "count":
        return int(sales_summary(db, start, end).invoice_count)
    if measure == "gross":
        return sales_summary(db, start, end).gross_revenue
    _, _, net = payment_flow_totals(sales_by_payment_method(db, start, end))
    return net


def _hotel_metric(db: Session, start: datetime, end: datetime, measure: str) -> Decimal | int:
    from modules.hotel.revenue_stats import hotel_cash_collected

    if measure == "count":
        cnt = db.scalar(
            select(func.count(HotelBookingPayment.id)).where(
                HotelBookingPayment.created_at >= start,
                HotelBookingPayment.created_at < end,
            )
        )
        return int(cnt or 0)
    if measure == "gross":
        paid = db.scalar(
            select(func.coalesce(func.sum(HotelBookingPayment.amount), 0)).where(
                HotelBookingPayment.created_at >= start,
                HotelBookingPayment.created_at < end,
            )
        )
        return Decimal(str(paid or 0)).quantize(Decimal("0.001"))
    return hotel_cash_collected(db, start, end)


def _add_values(a: Decimal | int, b: Decimal | int) -> Decimal | int:
    if isinstance(a, int) and isinstance(b, int):
        return a + b
    return (Decimal(str(a)) + Decimal(str(b))).quantize(Decimal("0.001"))


def _zero_for_measure(measure: str) -> Decimal | int:
    return 0 if measure == "count" else Decimal("0")


def resolve_segment_filter(
    segment: str | None,
    domain: BusinessDomain | None,
) -> tuple[bool, bool]:
    seg = (segment or "all").lower()
    if domain == BusinessDomain.RESTAURANT:
        return True, False
    if domain == BusinessDomain.HOTEL:
        return False, True
    if seg == "restaurant":
        return True, False
    if seg == "hotel":
        return False, True
    return True, True


def build_revenue_matrix_report(
    db: Session,
    start: datetime,
    end: datetime,
    *,
    granularity: str = "month",
    measure: str = "net",
    domain: BusinessDomain | None = None,
    segment: str | None = None,
) -> RevenueMatrixReport:
    gran = granularity if granularity in GRANULARITY_LABELS else "month"
    meas = measure if measure in MEASURE_LABELS else "net"
    show_restaurant, show_hotel = resolve_segment_filter(segment, domain)

    buckets = iter_time_buckets(start, end, gran)
    columns = [
        RevenueMatrixColumn(
            key=b.key,
            label=b.label,
            start=b.start,
            end_exclusive=b.end_exclusive,
        )
        for b in buckets
    ]

    row_defs: list[tuple[str, str]] = []
    if show_restaurant:
        row_defs.append(("restaurant", "المطعم"))
    if show_hotel:
        row_defs.append(("hotel", "الفندق"))
    if show_restaurant and show_hotel:
        row_defs.append(("total", "الإجمالي"))

    rows: list[RevenueMatrixRow] = []
    column_totals: dict[str, Decimal | int] = {c.key: _zero_for_measure(meas) for c in columns}
    grand_total = _zero_for_measure(meas)

    seg_values: dict[str, dict[str, Decimal | int]] = {
        seg: {c.key: _zero_for_measure(meas) for c in columns} for seg, _ in row_defs
    }

    for bucket in buckets:
        b_start, b_end = _bucket_bounds(bucket)
        # قصّ حدود الدلو ضمن الفترة المطلوبة (أول/آخر شهر جزئي)
        if b_start < start:
            b_start = start
        if b_end > end:
            b_end = end
        if b_start >= b_end:
            continue
        rest_val = (
            _restaurant_metric(db, b_start, b_end, meas) if show_restaurant else _zero_for_measure(meas)
        )
        hotel_val = _hotel_metric(db, b_start, b_end, meas) if show_hotel else _zero_for_measure(meas)

        if show_restaurant:
            seg_values["restaurant"][bucket.key] = rest_val
            column_totals[bucket.key] = _add_values(column_totals[bucket.key], rest_val)
        if show_hotel:
            seg_values["hotel"][bucket.key] = hotel_val
            column_totals[bucket.key] = _add_values(column_totals[bucket.key], hotel_val)
        if show_restaurant and show_hotel:
            seg_values["total"][bucket.key] = _add_values(rest_val, hotel_val)

    for seg, label in row_defs:
        row_total = _zero_for_measure(meas)
        for col in columns:
            row_total = _add_values(row_total, seg_values[seg][col.key])
        rows.append(
            RevenueMatrixRow(
                segment=seg,
                label_ar=label,
                values=seg_values[seg],
                total=row_total,
            )
        )
        if seg == "total" or (not show_restaurant and seg == "hotel") or (not show_hotel and seg == "restaurant"):
            grand_total = row_total

    if show_restaurant and show_hotel:
        pass
    elif show_restaurant and rows:
        grand_total = rows[0].total
    elif show_hotel and rows:
        grand_total = rows[0].total

    days = _iter_period_days(start, end)
    from_d = days[0] if days else to_local(start).date() if to_local(start) else date.today()
    to_d = days[-1] if days else from_d

    return RevenueMatrixReport(
        columns=columns,
        rows=rows,
        granularity=gran,
        granularity_label=GRANULARITY_LABELS[gran],
        measure=meas,
        measure_label=MEASURE_LABELS[meas],
        from_date=from_d,
        to_date=to_d,
        show_restaurant=show_restaurant,
        show_hotel=show_hotel,
        column_totals=column_totals,
        grand_total=grand_total,
    )


def revenue_matrix_csv_rows(report: RevenueMatrixReport) -> tuple[list[str], list[list]]:
    headers = ["البند", * [c.label for c in report.columns], "المجموع"]
    rows: list[list] = []
    for row in report.rows:
        rows.append(
            [row.label_ar, *[row.values.get(c.key, 0) for c in report.columns], row.total]
        )
    rows.append(
        ["مجموع الأعمدة", *[report.column_totals.get(c.key, 0) for c in report.columns], report.grand_total]
    )
    return headers, rows
