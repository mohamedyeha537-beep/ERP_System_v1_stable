"""إيراد وتكلفة متغيّرة حسب مجال العمل — للتقارير المالية."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from modules.customers.loyalty_shift_reports import loyalty_redeem_cost_in_period
from modules.payments.daily_burden import (
    recurring_costs_breakdown_in_period,
    recurring_costs_in_period,
)
from modules.payments.depreciation import (
    consumable_assets_total_in_period,
    total_depreciation_in_period,
)
from modules.payments.models import PurchaseKind
from modules.platform.business_domain import BusinessDomain
from modules.reporting.profit_calc import calc_net_profit
from modules.reporting.queries import (
    SalesSummary,
    assets_summary,
    cogs_summary,
    expenses_summary,
    expenses_total_for_profit,
    inventory_purchases_summary,
    packaging_cost_breakdown,
    packaging_cogs_summary,
    sales_by_payment_method,
    sales_summary,
    payment_flow_totals,
)


@dataclass
class DomainPeriodFinancials:
    sales_sum: SalesSummary
    cogs: Decimal
    revenue_source: str
    net_collected: Decimal
    sales_cash_in: Decimal
    refunds_cash_out: Decimal
    delivery_cash_out: Decimal
    show_product_profit: bool


def _hotel_sales_summary(db: Session, start: datetime, end: datetime) -> SalesSummary:
    """تحصيلات الفندق: قبض − استرداد (ليس إيراد إقامة مستحق)."""
    from modules.hotel.booking_models import (
        HotelBookingPayment,
        HotelBookingPaymentRefund,
    )
    from modules.hotel.revenue_stats import hotel_cash_collected

    collected = Decimal(
        str(
            db.scalar(
                select(func.coalesce(func.sum(HotelBookingPayment.amount), 0)).where(
                    HotelBookingPayment.created_at >= start,
                    HotelBookingPayment.created_at < end,
                )
            )
            or 0
        )
    ).quantize(Decimal("0.001"))
    refunded = Decimal(
        str(
            db.scalar(
                select(func.coalesce(func.sum(HotelBookingPaymentRefund.amount), 0)).where(
                    HotelBookingPaymentRefund.created_at >= start,
                    HotelBookingPaymentRefund.created_at < end,
                )
            )
            or 0
        )
    ).quantize(Decimal("0.001"))
    net = hotel_cash_collected(db, start, end)
    pay_cnt = int(
        db.scalar(
            select(func.count())
            .select_from(HotelBookingPayment)
            .where(
                HotelBookingPayment.created_at >= start,
                HotelBookingPayment.created_at < end,
            )
        )
        or 0
    )
    ref_cnt = int(
        db.scalar(
            select(func.count())
            .select_from(HotelBookingPaymentRefund)
            .where(
                HotelBookingPaymentRefund.created_at >= start,
                HotelBookingPaymentRefund.created_at < end,
            )
        )
        or 0
    )
    avg = (collected / pay_cnt).quantize(Decimal("0.001")) if pay_cnt else Decimal("0")
    return SalesSummary(
        invoice_count=pay_cnt,
        return_count=ref_cnt,
        gross_revenue=collected,
        returns_total=refunded,
        net_revenue=net,
        revenue=net,
        avg_basket=avg,
    )


def _hotel_accommodation_revenue(db: Session, start: datetime, end: datetime) -> Decimal:
    """إيراد الإقامة المستحق (مجموع accommodation_total للحجوزات في الفترة)."""
    from modules.hotel.booking_models import HotelBooking, RecordKind

    total = db.scalar(
        select(func.coalesce(func.sum(HotelBooking.accommodation_total), 0)).where(
            HotelBooking.record_kind == RecordKind.BOOKING,
            HotelBooking.created_at >= start,
            HotelBooking.created_at < end,
        )
    )
    return Decimal(str(total or 0)).quantize(Decimal("0.001"))


def _variable_cost(db: Session, start: datetime, end: datetime, domain) -> Decimal:
    if domain == BusinessDomain.HOTEL:
        from modules.hotel.revenue_stats import hotel_variable_cost

        return hotel_variable_cost(db, start, end)
    if domain == BusinessDomain.RESTAURANT:
        return cogs_summary(db, start, end)
    cogs = cogs_summary(db, start, end)
    try:
        from modules.hotel.revenue_stats import hotel_variable_cost

        cogs += hotel_variable_cost(db, start, end)
    except Exception:  # noqa: BLE001
        pass
    return cogs.quantize(Decimal("0.001"))


def build_domain_period_financials(
    db: Session, start: datetime, end: datetime, domain=None
) -> DomainPeriodFinancials:
    from modules.delivery.service import delivery_fee_cash_out_total

    if domain == BusinessDomain.HOTEL:
        sales_sum = _hotel_sales_summary(db, start, end)
        rev = sales_sum.net_revenue
        return DomainPeriodFinancials(
            sales_sum=sales_sum,
            cogs=_variable_cost(db, start, end, domain),
            revenue_source="hotel",
            net_collected=rev,
            sales_cash_in=sales_sum.gross_revenue,
            refunds_cash_out=sales_sum.returns_total,
            delivery_cash_out=Decimal("0"),
            show_product_profit=False,
        )

    pos_sum = sales_summary(db, start, end)
    sales_by_pm = sales_by_payment_method(db, start, end)
    sales_cash_in, refunds_cash_out, net_collected = payment_flow_totals(sales_by_pm)
    delivery_cash_out = delivery_fee_cash_out_total(db, start=start, end=end)

    if domain == BusinessDomain.RESTAURANT:
        return DomainPeriodFinancials(
            sales_sum=pos_sum,
            cogs=_variable_cost(db, start, end, domain),
            revenue_source="pos",
            net_collected=net_collected,
            sales_cash_in=sales_cash_in,
            refunds_cash_out=refunds_cash_out,
            delivery_cash_out=delivery_cash_out,
            show_product_profit=True,
        )

    hotel_sum = _hotel_sales_summary(db, start, end)
    combined_rev = (pos_sum.net_revenue + hotel_sum.net_revenue).quantize(Decimal("0.001"))
    combined_cnt = pos_sum.invoice_count + hotel_sum.invoice_count
    combined = SalesSummary(
        invoice_count=combined_cnt,
        return_count=pos_sum.return_count + hotel_sum.return_count,
        gross_revenue=(pos_sum.gross_revenue + hotel_sum.gross_revenue).quantize(
            Decimal("0.001")
        ),
        returns_total=(pos_sum.returns_total + hotel_sum.returns_total).quantize(
            Decimal("0.001")
        ),
        net_revenue=combined_rev,
        revenue=combined_rev,
        avg_basket=(combined_rev / combined_cnt).quantize(Decimal("0.001"))
        if combined_cnt
        else Decimal("0"),
    )
    return DomainPeriodFinancials(
        sales_sum=combined,
        cogs=_variable_cost(db, start, end, domain),
        revenue_source="combined",
        net_collected=(net_collected + hotel_sum.net_revenue).quantize(Decimal("0.001")),
        sales_cash_in=sales_cash_in + hotel_sum.gross_revenue,
        refunds_cash_out=refunds_cash_out + hotel_sum.returns_total,
        delivery_cash_out=delivery_cash_out,
        show_product_profit=True,
    )


def build_profit_report_bundle(
    db: Session, start: datetime, end: datetime, domain=None
) -> dict:
    """حزمة أرقام الربح/التدفق — تُستخدم في hub و profit و comprehensive."""
    fin = build_domain_period_financials(db, start, end, domain=domain)
    hotel_accommodation_total = Decimal("0")
    if domain in (BusinessDomain.HOTEL, None):
        try:
            hotel_accommodation_total = _hotel_accommodation_revenue(db, start, end)
        except Exception:  # noqa: BLE001
            hotel_accommodation_total = Decimal("0")
    inv_purch = inventory_purchases_summary(db, start, end, domain=domain)
    expenses = expenses_summary(db, start, end, domain=domain)
    expenses_for_profit = expenses_total_for_profit(db, start, end, domain=domain)
    assets = assets_summary(db, start, end, domain=domain)
    if domain == BusinessDomain.HOTEL:
        consumables_period = Decimal("0")
        depreciation_period = Decimal("0")
        loyalty = type("L", (), {"dinar_cost": Decimal("0"), "redeem_count": 0})()
    else:
        consumables_period = consumable_assets_total_in_period(db, start, end)
        depreciation_period = total_depreciation_in_period(db, start, end)
        loyalty = loyalty_redeem_cost_in_period(db, start, end)

    operating_assets_expense = (consumables_period + depreciation_period).quantize(
        Decimal("0.001")
    )
    rec_monthly_total, rec_period_share, period_days = recurring_costs_in_period(
        db, start, end, domain=domain
    )
    rec_breakdown = recurring_costs_breakdown_in_period(db, start, end, domain=domain)
    gross_profit = (fin.sales_sum.revenue - fin.cogs).quantize(Decimal("0.001"))
    net_profit = calc_net_profit(
        gross_profit,
        expenses_total=expenses_for_profit,
        rec_period_share=rec_period_share,
        operating_assets_expense=operating_assets_expense,
        loyalty_dinar_cost=loyalty.dinar_cost,
    )
    cashflow = (
        fin.net_collected
        - fin.delivery_cash_out
        - inv_purch.total
        - expenses_for_profit
        - assets.total
    ).quantize(Decimal("0.001"))

    return {
        "sales_sum": fin.sales_sum,
        "hotel_accommodation_total": hotel_accommodation_total,
        "cogs": fin.cogs,
        "packaging_cogs": packaging_cogs_summary(db, start, end),
        "packaging_breakdown": packaging_cost_breakdown(db, start, end),
        "revenue_source": fin.revenue_source,
        "net_collected": fin.net_collected,
        "sales_cash_in": fin.sales_cash_in,
        "refunds_cash_out": fin.refunds_cash_out,
        "delivery_cash_out": fin.delivery_cash_out,
        "show_product_profit": fin.show_product_profit,
        "inv_purch": inv_purch,
        "expenses": expenses,
        "expenses_for_profit": expenses_for_profit,
        "assets": assets,
        "consumables_period": consumables_period,
        "depreciation_period": depreciation_period,
        "operating_assets_expense": operating_assets_expense,
        "rec_monthly_total": rec_monthly_total,
        "rec_period_share": rec_period_share,
        "rec_breakdown": rec_breakdown,
        "period_days": period_days,
        "loyalty_redeem": loyalty,
        "gross_profit": gross_profit,
        "net_profit": net_profit,
        "cashflow": cashflow,
    }


def domain_sales_by_payment_method(
    db: Session, start: datetime, end: datetime, domain=None
) -> list[tuple[str, int, Decimal, int, Decimal, Decimal]]:
    """تحصيل/رد حسب طريقة الدفع — POS أو فندق أو مجمّع."""
    if domain == BusinessDomain.HOTEL:
        from modules.hotel.revenue_stats import hotel_collections_by_payment_method

        return hotel_collections_by_payment_method(db, start, end)

    pos_rows = sales_by_payment_method(db, start, end)
    if domain == BusinessDomain.RESTAURANT:
        return pos_rows

    try:
        from modules.hotel.revenue_stats import hotel_collections_by_payment_method

        hotel_rows = hotel_collections_by_payment_method(db, start, end)
    except Exception:  # noqa: BLE001
        return pos_rows
    if not hotel_rows:
        return pos_rows
    return _merge_payment_method_rows_from_hotel(pos_rows, hotel_rows)


def _merge_payment_method_rows_from_hotel(
    pos_rows: list[tuple[str, int, Decimal, int, Decimal, Decimal]],
    hotel_rows: list[tuple[str, int, Decimal, int, Decimal, Decimal]],
) -> list[tuple[str, int, Decimal, int, Decimal, Decimal]]:
    from modules.hotel.revenue_stats import _merge_payment_method_rows

    return _merge_payment_method_rows(pos_rows, hotel_rows)

