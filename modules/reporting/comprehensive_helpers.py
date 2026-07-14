"""مساعدات التقرير الشامل — CSV وت reconciliaton GL للفندق."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.datetime_local import format_local_dt, to_local
from modules.platform.business_domain import BusinessDomain


@dataclass
class HotelGlReconciliation:
    gl_enabled: bool
    operational_net: Decimal
    gl_revenue_net: Decimal
    gap: Decimal
    account_code: str
    account_name: str
    account_id: int | None
    from_date: date
    to_date: date


def period_local_dates(start: datetime, end: datetime) -> tuple[date, date]:
    sl = to_local(start)
    el = to_local(end)
    if not isinstance(sl, datetime) or not isinstance(el, datetime):
        today = date.today()
        return today, today
    from_d = sl.date()
    to_d = (el - timedelta(microseconds=1)).date()
    if to_d < from_d:
        to_d = from_d
    return from_d, to_d


def hotel_gl_net_for_day(db: Session, day: date, *, account_id: int) -> Decimal:
    from modules.gl.reports import account_period_movement

    deb, cred = account_period_movement(
        db, account_id, day, day, domain=BusinessDomain.HOTEL
    )
    return (cred - deb).quantize(Decimal("0.001"))


def hotel_gl_reconciliation_for_period(
    db: Session, start: datetime, end: datetime, *, operational_net: Decimal
) -> HotelGlReconciliation | None:
    from modules.gl.models import GlAccount
    from modules.gl.posting import CODE_HOTEL_REVENUE
    from modules.gl.reports import account_period_movement
    from modules.gl.service import is_gl_enabled

    if not is_gl_enabled(db):
        return None
    acc = db.scalar(select(GlAccount).where(GlAccount.code == CODE_HOTEL_REVENUE))
    from_d, to_d = period_local_dates(start, end)
    gl_net = Decimal("0")
    acc_name = "إيرادات الإقامة (فندق)"
    acc_id: int | None = None
    if acc is not None:
        acc_id = int(acc.id)
        acc_name = str(acc.name_ar or acc_name)
        deb, cred = account_period_movement(
            db, acc_id, from_d, to_d, domain=BusinessDomain.HOTEL
        )
        gl_net = (cred - deb).quantize(Decimal("0.001"))
    gap = (operational_net - gl_net).quantize(Decimal("0.001"))
    return HotelGlReconciliation(
        gl_enabled=True,
        operational_net=operational_net.quantize(Decimal("0.001")),
        gl_revenue_net=gl_net,
        gap=gap,
        account_code=CODE_HOTEL_REVENUE,
        account_name=acc_name,
        account_id=acc_id,
        from_date=from_d,
        to_date=to_d,
    )


@dataclass
class RestaurantGlReconciliation:
    gl_enabled: bool
    operational_net: Decimal
    gl_revenue_net: Decimal
    gap: Decimal
    account_code: str
    account_name: str
    account_id: int | None
    from_date: date
    to_date: date


def restaurant_gl_reconciliation_for_period(
    db: Session, start: datetime, end: datetime, *, operational_net: Decimal
) -> RestaurantGlReconciliation | None:
    from modules.gl.models import GlAccount
    from modules.gl.posting import CODE_REVENUE
    from modules.gl.reports import account_period_movement
    from modules.gl.service import is_gl_enabled

    if not is_gl_enabled(db):
        return None
    acc = db.scalar(select(GlAccount).where(GlAccount.code == CODE_REVENUE))
    from_d, to_d = period_local_dates(start, end)
    gl_net = Decimal("0")
    acc_name = "إيرادات المبيعات"
    acc_id: int | None = None
    if acc is not None:
        acc_id = int(acc.id)
        acc_name = str(acc.name_ar or acc_name)
        deb, cred = account_period_movement(
            db, acc_id, from_d, to_d, domain=BusinessDomain.RESTAURANT
        )
        gl_net = (cred - deb).quantize(Decimal("0.001"))
    gap = (operational_net - gl_net).quantize(Decimal("0.001"))
    return RestaurantGlReconciliation(
        gl_enabled=True,
        operational_net=operational_net.quantize(Decimal("0.001")),
        gl_revenue_net=gl_net,
        gap=gap,
        account_code=CODE_REVENUE,
        account_name=acc_name,
        account_id=acc_id,
        from_date=from_d,
        to_date=to_d,
    )


def hotel_comprehensive_supplements(
    db: Session, start: datetime, end: datetime, *, operational_net: Decimal
) -> dict:
    from modules.hotel.bookings_report import hotel_open_balances
    from modules.hotel.daily_close_report import hotel_daily_close_report

    _, open_summary = hotel_open_balances(db)
    _, close_summary = hotel_daily_close_report(db, start, end)
    gl_recon = hotel_gl_reconciliation_for_period(
        db, start, end, operational_net=operational_net
    )
    return {
        "hotel_open_summary": open_summary,
        "hotel_close_summary": close_summary,
        "hotel_gl_recon": gl_recon,
    }


def _include_hotel_in_comprehensive(domain) -> bool:
    return domain in (None, BusinessDomain.HOTEL)


def _include_restaurant_gl(domain) -> bool:
    return domain in (None, BusinessDomain.RESTAURANT)


def build_comprehensive_csv_rows(
    db: Session,
    start: datetime,
    end: datetime,
    domain,
    bundle: dict,
) -> list[list]:
    from modules.platform.business_domain import domain_label

    sales_sum = bundle["sales_sum"]
    dom_tag = domain_label(domain) if domain else "الكل"
    rev_src = bundle.get("revenue_source", "pos")
    is_hotel = domain == BusinessDomain.HOTEL or rev_src == "hotel"
    is_pos = domain == BusinessDomain.RESTAURANT or (
        domain is None and rev_src in ("pos", "combined")
    )

    sales_cash_in = bundle["sales_cash_in"]
    refunds_cash_out = bundle["refunds_cash_out"]
    net_collected = bundle["net_collected"]
    delivery_cash_out = bundle["delivery_cash_out"]

    rev_label = "التحصيل الصافي (فندق)" if is_hotel else "إجمالي المبيعات"
    rows: list[list] = [
        ["الفترة", f"{format_local_dt(start, '%Y-%m-%d')} → {format_local_dt(end, '%Y-%m-%d')}"],
        ["مجال التقرير", dom_tag],
        ["مصدر الإيراد", rev_src],
        ["عدد أيام الفترة", f"{float(bundle['period_days']):.1f}"],
        [],
        ["━━━━━━ الحسبة (1): الفرق المباشر ━━━━━━", ""],
        [f"+ {rev_label}", sales_sum.gross_revenue],
    ]
    if not is_hotel:
        rows.extend(
            [
                ["− المرتجعات", sales_sum.returns_total],
                ["= صافي المبيعات", sales_sum.net_revenue],
            ]
        )
    else:
        rows.append(["= صافي التحصيل", sales_sum.net_revenue])
    rows.extend(
        [
            ["− تكلفة متغيّرة / COGS", bundle["cogs"]],
            ["= إجمالي الربح / هامش المساهمة", bundle["gross_profit"]],
            [],
            ["━━━━━━ الحسبة (2): صافي الربح بعد كل المصاريف ━━━━━━", ""],
            ["+ إجمالي الربح من (1)", bundle["gross_profit"]],
            ["− المصاريف التشغيلية", bundle["expenses"].total],
            [
                f"− رواتب وإيجار وثابتة (حصة الفترة من {bundle['rec_monthly_total']} شهرياً)",
                bundle["rec_period_share"],
            ],
        ]
    )
    if not is_hotel:
        rows.extend(
            [
                ["− مستلزمات استهلاكية", bundle["consumables_period"]],
                ["− إهلاك الأصول الثابتة (IAS 16)", bundle["depreciation_period"]],
                [
                    f"− نقاط ولاء مُصرفة ({bundle['loyalty_redeem'].redeem_count} عملية)",
                    bundle["loyalty_redeem"].dinar_cost,
                ],
            ]
        )
    rows.extend(
        [
            ["= صافي الربح/الخسارة", bundle["net_profit"]],
            [],
            ["━━━━━━ التدفق النقدي ━━━━━━", ""],
            ["التحصيل الداخل", sales_cash_in],
            ["رد المبالغ", refunds_cash_out],
            ["صافي التحصيل", net_collected],
        ]
    )
    if is_pos and not is_hotel:
        rows.append(["أجرة التوصيل الخارجة", delivery_cash_out])
    rows.extend(
        [
            ["شراء بضاعة (مخزون)", bundle["inv_purch"].total],
            ["مصاريف", bundle["expenses"].total],
            ["شراء أصول/أدوات (نقد)", bundle["assets"].total],
            ["= صافي التدفق النقدي", bundle["cashflow"]],
        ]
    )

    if _include_hotel_in_comprehensive(domain):
        from modules.hotel.revenue_stats import hotel_cash_collected

        hotel_op = (
            net_collected
            if is_hotel
            else hotel_cash_collected(db, start, end)
        )
        sup = hotel_comprehensive_supplements(
            db, start, end, operational_net=hotel_op
        )
        open_s = sup["hotel_open_summary"]
        close_s = sup["hotel_close_summary"]
        gl = sup["hotel_gl_recon"]
        rows.extend(
            [
                [],
                ["━━━━━━ الفندق — ذمم وإقفال ━━━━━━", ""],
                ["ذمم الحجز (عدد)", open_s.booking_count],
                ["ذمم الحجز (رصيد)", open_s.balance_total],
                ["أيام مُقفلة في الفترة", close_s.closed_days],
                ["تحصيل الفترة (إقفال يومي)", close_s.total_revenue],
                ["متوسط إشغال %", close_s.avg_occupancy],
            ]
        )
        if gl is not None:
            rows.extend(
                [
                    [],
                    ["━━━━━━ مطابقة GL — حساب 4150 ━━━━━━", ""],
                    ["تحصيل تشغيلي (صافي)", gl.operational_net],
                    ["صافي دائن 4150 في GL", gl.gl_revenue_net],
                    ["الفارق (تشغيل − GL)", gl.gap],
                ]
            )

    if _include_restaurant_gl(domain) and not is_hotel:
        pos_net = net_collected
        if domain is None:
            from modules.reporting.domain_financials import build_domain_period_financials

            pos_fin = build_domain_period_financials(
                db, start, end, domain=BusinessDomain.RESTAURANT
            )
            pos_net = pos_fin.net_collected
        pos_gl = restaurant_gl_reconciliation_for_period(
            db, start, end, operational_net=pos_net
        )
        if pos_gl is not None:
            rows.extend(
                [
                    [],
                    ["━━━━━━ مطابقة GL — حساب 4100 (مطعم) ━━━━━━", ""],
                    ["تحصيل تشغيلي (صافي)", pos_gl.operational_net],
                    ["صافي دائن 4100 في GL", pos_gl.gl_revenue_net],
                    ["الفارق (تشغيل − GL)", pos_gl.gap],
                ]
            )

    if is_pos and not is_hotel:
        from modules.reporting import queries as report_queries

        inv_value = report_queries.inventory_value(db)
        rows.extend(
            [
                [],
                ["━━━━━━ المخزون ━━━━━━", ""],
                ["قيمة المخزون الحالية", inv_value],
                [
                    "أصناف منخفضة المخزون",
                    sum(
                        1
                        for _r in report_queries.inventory_snapshot(db, only_low=True)
                    ),
                ],
            ]
        )
    return rows
