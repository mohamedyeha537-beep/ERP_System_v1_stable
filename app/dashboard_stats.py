"""إحصائيات لوحة التحكم الرئيسية.
يجمع أهم المؤشرات (المبيعات، المخزون، التنبيهات، آخر الفواتير، رصيد المحافظ)
في استدعاء واحد لتعرض بشكل احترافي على صفحة `/`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from modules.catalog.models import Product, ProductCategory
from modules.inventory.service import low_stock_by_warehouse
from modules.payments.daily_burden import (
    compute_daily_burden,
    recurring_costs_breakdown_in_period,
    recurring_costs_in_period,
)
from modules.payments.depreciation import (
    consumable_assets_total_in_period,
    fixed_assets_summary,
    total_depreciation_in_period,
)
from modules.payments.models import Purchase, PurchaseKind
from modules.payments.service import (
    list_payment_methods_for_dashboard,
    payment_method_balances_map,
    wallet_breakdown,
)
from modules.payments.treasury_service import treasury_summaries
from modules.payables.service import payables_summary
from modules.receivables.service import receivables_summary
from modules.pos_shifts.shortages import shortages_summary as shift_shortages_summary
from modules.reporting.queries import (
    cogs_summary,
    inventory_value,
    sales_summary,
)
from modules.sales.models import Sale, SaleLine, SaleStatus


@dataclass
class PeriodSales:
    count: int = 0
    revenue: Decimal = Decimal("0")
    gross_revenue: Decimal = Decimal("0")
    returns_total: Decimal = Decimal("0")
    avg_basket: Decimal = Decimal("0")


@dataclass
class TopProductRow:
    name_ar: str
    qty: Decimal
    revenue: Decimal


@dataclass
class RecentSaleRow:
    id: int
    created_at: datetime
    total: Decimal
    items: int


@dataclass
class RecentHotelPaymentRow:
    id: int
    booking_id: int
    created_at: datetime
    amount: Decimal
    guest_label: str


@dataclass
class GlTreasuryDashboardCard:
    account_id: int
    code: str
    name_ar: str
    card_kind: str
    balance: Decimal
    wallet_pm_id: int | None = None
    wallet_names: list[str] = field(default_factory=list)


@dataclass
class TreasuryDashboardCard:
    method_id: int
    name_ar: str
    kind: str  # CASH | BANK
    balance: Decimal


@dataclass
class DashboardStats:
    today: PeriodSales = field(default_factory=PeriodSales)
    yesterday: PeriodSales = field(default_factory=PeriodSales)
    month: PeriodSales = field(default_factory=PeriodSales)
    revenue_change_pct: float = 0.0
    revenue_change_dir: str = "flat"  # up / down / flat
    yesterday_date: str = ""  # ISO date لاستخدامه في روابط التقارير

    products_count: int = 0
    categories_count: int = 0
    inventory_value: Decimal = Decimal("0")
    low_stock_count: int = 0

    month_purchases: Decimal = Decimal("0")
    month_expenses: Decimal = Decimal("0")
    month_assets: Decimal = Decimal("0")  # إجمالي الصرف على الأصول/المستلزمات (نقدياً)
    month_assets_consumable: Decimal = Decimal("0")  # المستلزمات الاستهلاكية (مصروف فوري)
    month_assets_depreciation: Decimal = Decimal("0")  # الإهلاك (حصة الفترة من الأصول الثابتة)
    month_recurring_total: Decimal = Decimal("0")  # رواتب وإيجار واشتراكات (شهرياً ثابت)
    month_recurring_share: Decimal = Decimal("0")  # حصة الشهر من ذلك
    month_recurring_breakdown: list = field(default_factory=list)
    month_cogs: Decimal = Decimal("0")
    month_gross_profit: Decimal = Decimal("0")
    month_net_profit: Decimal = Decimal("0")
    month_loyalty_redeem_cost: Decimal = Decimal("0")
    month_loyalty_redeem_count: int = 0
    month_gross_margin_pct: float = 0.0
    month_net_margin_pct: float = 0.0
    revenue_source: str = "pos"  # pos | hotel | combined
    finance_domain_label: str = "الكل"
    gl_pl_revenue: Decimal | None = None
    gl_pl_expense: Decimal | None = None
    gl_pl_net: Decimal | None = None
    # حالة الأصول الثابتة الإجمالية (حتى الآن)
    fixed_assets_count: int = 0
    fixed_assets_cost: Decimal = Decimal("0")
    fixed_assets_book_value: Decimal = Decimal("0")
    fixed_assets_monthly_depr: Decimal = Decimal("0")
    # تحليل التعادل اليومي (CVP)
    daily_burden: object | None = None  # كائن DailyBurden

    top_today: list[TopProductRow] = field(default_factory=list)
    recent_sales: list[RecentSaleRow] = field(default_factory=list)
    recent_hotel_payments: list[RecentHotelPaymentRow] = field(default_factory=list)
    low_rows: list = field(default_factory=list)
    wallets: list = field(default_factory=list)

    cash_current: Decimal | None = None
    cash_last_close: Decimal | None = None
    bank_current: Decimal | None = None
    bank_last_close: Decimal | None = None

    ar_outstanding_total: Decimal = Decimal("0")
    ar_invoice_count: int = 0
    ar_unpaid_count: int = 0
    ar_partial_count: int = 0

    ap_outstanding_total: Decimal = Decimal("0")
    ap_invoice_count: int = 0

    gl_enabled: bool = False
    treasury_source: str = "wallets"  # wallets | gl
    treasury_cards: list = field(default_factory=list)
    gl_treasury_cards: list = field(default_factory=list)

    shortage_cash_total: Decimal = Decimal("0")
    shortage_cash_count: int = 0
    shortage_bank_total: Decimal = Decimal("0")
    shortage_bank_count: int = 0


def _day_bounds(now: datetime, offset_days: int = 0) -> tuple[datetime, datetime]:
    base = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = base + timedelta(days=offset_days)
    return start, start + timedelta(days=1)


def _month_bounds(now: datetime) -> tuple[datetime, datetime]:
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


def _period_sales(db: Session, start: datetime, end: datetime) -> PeriodSales:
    summary = sales_summary(db, start, end)
    return PeriodSales(
        count=summary.invoice_count,
        revenue=summary.net_revenue,
        gross_revenue=summary.gross_revenue,
        returns_total=summary.returns_total,
        avg_basket=summary.avg_basket,
    )


def _period_hotel_revenue(db: Session, start: datetime, end: datetime) -> PeriodSales:
    from modules.hotel.booking_models import HotelBookingPayment
    from modules.hotel.revenue_stats import hotel_cash_collected

    revenue = hotel_cash_collected(db, start, end)
    cnt = int(
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
    avg = (revenue / cnt).quantize(Decimal("0.001")) if cnt else Decimal("0")
    return PeriodSales(
        count=cnt,
        revenue=revenue,
        gross_revenue=revenue,
        returns_total=Decimal("0"),
        avg_basket=avg,
    )


def _period_revenue(
    db: Session, start: datetime, end: datetime, domain=None
) -> tuple[PeriodSales, str]:
    from modules.platform.business_domain import BusinessDomain

    if domain == BusinessDomain.HOTEL:
        return _period_hotel_revenue(db, start, end), "hotel"
    if domain == BusinessDomain.RESTAURANT:
        return _period_sales(db, start, end), "pos"
    pos = _period_sales(db, start, end)
    try:
        hotel = _period_hotel_revenue(db, start, end)
    except Exception:  # noqa: BLE001
        hotel = PeriodSales()
    total_rev = (pos.revenue + hotel.revenue).quantize(Decimal("0.001"))
    total_count = pos.count + hotel.count
    avg = (
        (total_rev / total_count).quantize(Decimal("0.001")) if total_count else Decimal("0")
    )
    return (
        PeriodSales(
            count=total_count,
            revenue=total_rev,
            gross_revenue=(pos.gross_revenue + hotel.gross_revenue).quantize(
                Decimal("0.001")
            ),
            returns_total=pos.returns_total,
            avg_basket=avg,
        ),
        "combined",
    )


def _month_variable_cost(
    db: Session, start: datetime, end: datetime, domain=None
) -> Decimal:
    from modules.platform.business_domain import BusinessDomain

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


def _period_purchase_total(
    db: Session, start: datetime, end: datetime, kind: PurchaseKind, domain=None
) -> Decimal:
    from modules.platform.business_domain import purchase_domain_db_values

    stmt = select(func.coalesce(func.sum(Purchase.amount), 0)).where(
        Purchase.created_at >= start,
        Purchase.created_at < end,
        Purchase.kind == kind,
    )
    domain_vals = purchase_domain_db_values(domain)
    if domain_vals is not None:
        stmt = stmt.where(Purchase.business_domain.in_(domain_vals))
    total = db.execute(stmt).scalar_one()
    return Decimal(str(total or 0))


def _top_today(
    db: Session, start: datetime, end: datetime, limit: int = 5
) -> list[TopProductRow]:
    stmt = (
        select(
            Product.name_ar,
            func.sum(SaleLine.quantity),
            func.sum(SaleLine.line_total),
        )
        .join(Sale, Sale.id == SaleLine.sale_id)
        .join(Product, Product.id == SaleLine.product_id)
        .where(
            Sale.status == SaleStatus.COMPLETED,
            Sale.created_at >= start,
            Sale.created_at < end,
        )
        .group_by(Product.id, Product.name_ar)
        .order_by(func.sum(SaleLine.line_total).desc())
        .limit(limit)
    )
    rows = db.execute(stmt).all()
    return [
        TopProductRow(
            name_ar=str(name),
            qty=Decimal(str(q or 0)),
            revenue=Decimal(str(r or 0)),
        )
        for name, q, r in rows
    ]


def _recent_sales(db: Session, limit: int = 6) -> list[RecentSaleRow]:
    rows = db.execute(
        select(
            Sale.id,
            Sale.created_at,
            Sale.total,
            func.count(SaleLine.id),
        )
        .join(SaleLine, SaleLine.sale_id == Sale.id, isouter=True)
        .where(Sale.status == SaleStatus.COMPLETED)
        .group_by(Sale.id)
        .order_by(Sale.created_at.desc())
        .limit(limit)
    ).all()
    return [
        RecentSaleRow(
            id=int(rid),
            created_at=ca,
            total=Decimal(str(tot or 0)),
            items=int(items or 0),
        )
        for rid, ca, tot, items in rows
    ]


def _recent_hotel_payments(db: Session, limit: int = 6) -> list[RecentHotelPaymentRow]:
    from modules.hotel.booking_models import HotelBooking, HotelBookingPayment

    rows = db.execute(
        select(
            HotelBookingPayment.id,
            HotelBookingPayment.booking_id,
            HotelBookingPayment.created_at,
            HotelBookingPayment.amount,
            HotelBooking.guest_name,
            HotelBooking.guest_phone,
        )
        .join(HotelBooking, HotelBooking.id == HotelBookingPayment.booking_id)
        .order_by(HotelBookingPayment.created_at.desc())
        .limit(limit)
    ).all()
    out: list[RecentHotelPaymentRow] = []
    for pid, bid, ca, amt, name, phone in rows:
        label = (str(name or "").strip() or str(phone or "").strip() or f"حجز #{bid}")
        out.append(
            RecentHotelPaymentRow(
                id=int(pid),
                booking_id=int(bid),
                created_at=ca,
                amount=Decimal(str(amt or 0)),
                guest_label=label,
            )
        )
    return out


def collect(db: Session, domain=None) -> DashboardStats:
    """تجمع كل إحصائيات اللوحة في كائن واحد."""
    from modules.platform.business_domain import BusinessDomain, domain_label

    now = datetime.now(timezone.utc)
    today_s, today_e = _day_bounds(now, 0)
    yest_s, yest_e = _day_bounds(now, -1)
    month_s, month_e = _month_bounds(now)

    stats = DashboardStats()
    stats.finance_domain_label = domain_label(domain) if domain else "الكل"

    stats.today, rev_src = _period_revenue(db, today_s, today_e, domain=domain)
    stats.yesterday, _ = _period_revenue(db, yest_s, yest_e, domain=domain)
    stats.revenue_source = rev_src
    stats.yesterday_date = yest_s.strftime("%Y-%m-%d")
    stats.month, _ = _period_revenue(db, month_s, month_e, domain=domain)

    if stats.yesterday.revenue > 0:
        diff = stats.today.revenue - stats.yesterday.revenue
        pct = (diff / stats.yesterday.revenue) * Decimal("100")
        stats.revenue_change_pct = float(pct.quantize(Decimal("0.1")))
        stats.revenue_change_dir = (
            "up" if diff > 0 else ("down" if diff < 0 else "flat")
        )
    elif stats.today.revenue > 0:
        stats.revenue_change_pct = 100.0
        stats.revenue_change_dir = "up"

    stats.products_count = int(
        db.execute(
            select(func.count(Product.id)).where(Product.is_active.is_(True))
        ).scalar_one()
        or 0
    )
    stats.categories_count = int(
        db.execute(select(func.count(ProductCategory.id))).scalar_one() or 0
    )
    stats.inventory_value = inventory_value(db)
    low_all = low_stock_by_warehouse(db)
    stats.low_stock_count = sum(len(rows) for _wh, rows in low_all)
    low_flat: list = []
    for _wh, rows in low_all:
        low_flat.extend(rows)
    stats.low_rows = low_flat[:6]

    stats.month_purchases = _period_purchase_total(
        db, month_s, month_e, PurchaseKind.INVENTORY, domain=domain
    )
    stats.month_expenses = _period_purchase_total(
        db, month_s, month_e, PurchaseKind.EXPENSE, domain=domain
    )
    stats.month_assets = _period_purchase_total(
        db, month_s, month_e, PurchaseKind.ASSET, domain=domain
    )
    # تفصيل الأصول وفق المعالجة المحاسبية الصحيحة (IAS 16):
    # المستلزمات الاستهلاكية تُخصم بالكامل في فترة الشراء، أما الأصول الثابتة فيُخصم منها
    # «حصة الفترة» من قسط الإهلاك فقط (وليس كامل الكلفة).
    stats.month_assets_consumable = consumable_assets_total_in_period(
        db, month_s, month_e
    )
    stats.month_assets_depreciation = total_depreciation_in_period(
        db, month_s, month_e
    )
    if domain == BusinessDomain.HOTEL:
        stats.month_assets_consumable = Decimal("0")
        stats.month_assets_depreciation = Decimal("0")
    operating_assets_expense = (
        stats.month_assets_consumable + stats.month_assets_depreciation
    ).quantize(Decimal("0.001"))
    # حصة الرواتب والإيجار والاشتراكات الشهرية المنسوبة للشهر الحالي
    rec_monthly_total, rec_period_share, _ = recurring_costs_in_period(
        db, month_s, month_e, domain=domain
    )
    stats.month_recurring_total = rec_monthly_total
    stats.month_recurring_share = rec_period_share
    stats.month_recurring_breakdown = recurring_costs_breakdown_in_period(
        db, month_s, month_e, domain=domain
    )
    cogs_month = _month_variable_cost(db, month_s, month_e, domain=domain)
    stats.month_cogs = cogs_month.quantize(Decimal("0.001"))
    stats.month_gross_profit = (stats.month.revenue - cogs_month).quantize(
        Decimal("0.001")
    )
    from modules.customers.loyalty_shift_reports import loyalty_redeem_cost_in_period
    from modules.reporting.profit_calc import calc_net_profit

    loyalty_cost = Decimal("0")
    if domain in (None, BusinessDomain.RESTAURANT):
        loyalty_month = loyalty_redeem_cost_in_period(db, month_s, month_e)
        stats.month_loyalty_redeem_cost = loyalty_month.dinar_cost
        stats.month_loyalty_redeem_count = loyalty_month.redeem_count
        loyalty_cost = loyalty_month.dinar_cost
    stats.month_net_profit = calc_net_profit(
        stats.month_gross_profit,
        expenses_total=stats.month_expenses,
        rec_period_share=rec_period_share,
        operating_assets_expense=operating_assets_expense,
        loyalty_dinar_cost=loyalty_cost,
    )

    fa = fixed_assets_summary(db)
    if domain == BusinessDomain.HOTEL:
        stats.fixed_assets_count = 0
        stats.fixed_assets_cost = Decimal("0")
        stats.fixed_assets_book_value = Decimal("0")
        stats.fixed_assets_monthly_depr = Decimal("0")
    else:
        stats.fixed_assets_count = fa.asset_count
        stats.fixed_assets_cost = fa.total_cost
        stats.fixed_assets_book_value = fa.total_book_value
        stats.fixed_assets_monthly_depr = fa.total_monthly_depreciation
    if stats.month.revenue > 0:
        stats.month_gross_margin_pct = float(
            (stats.month_gross_profit / stats.month.revenue * 100).quantize(
                Decimal("0.1")
            )
        )
        stats.month_net_margin_pct = float(
            (stats.month_net_profit / stats.month.revenue * 100).quantize(
                Decimal("0.1")
            )
        )

    if domain in (None, BusinessDomain.RESTAURANT):
        stats.top_today = _top_today(db, today_s, today_e, limit=5)
        stats.recent_sales = _recent_sales(db, limit=6)
    if domain in (None, BusinessDomain.HOTEL):
        try:
            stats.recent_hotel_payments = _recent_hotel_payments(db, limit=6)
        except Exception:  # noqa: BLE001
            stats.recent_hotel_payments = []
    stats.wallets = wallet_breakdown(db, None, None)

    treasuries = treasury_summaries(db)
    cash_t = treasuries.get("CASH")
    bank_t = treasuries.get("BANK")
    if cash_t:
        stats.cash_current = cash_t.current_balance
        stats.cash_last_close = cash_t.last_close_balance
    if bank_t:
        stats.bank_current = bank_t.current_balance
        stats.bank_last_close = bank_t.last_close_balance

    bal_map = payment_method_balances_map(db)
    from modules.gl.dashboard import gl_dashboard_treasury_cards
    from modules.gl.service import is_gl_enabled

    gl_cards = gl_dashboard_treasury_cards(db, domain=domain)
    if is_gl_enabled(db):
        stats.gl_enabled = True
        stats.treasury_source = "gl"
        stats.gl_treasury_cards = [
            GlTreasuryDashboardCard(
                account_id=c.account_id,
                code=c.code,
                name_ar=c.name_ar,
                card_kind=c.card_kind,
                balance=c.balance,
                wallet_pm_id=c.wallet_pm_id,
                wallet_names=c.wallet_names or [],
            )
            for c in gl_cards
        ]
        stats.treasury_cards = []
    else:
        stats.gl_enabled = False
        stats.treasury_cards = [
            TreasuryDashboardCard(
                method_id=m.id,
                name_ar=m.name_ar,
                kind=m.kind.value,
                balance=bal_map.get(m.id, Decimal("0")).quantize(Decimal("0.001")),
            )
            for m in list_payment_methods_for_dashboard(
                db, only_active=True, domain=domain
            )
        ]

    if not is_gl_enabled(db):
        stats.treasury_source = "wallets"
        stats.gl_treasury_cards = []

    try:
        from modules.gl.reports import build_profit_loss
        from modules.gl.service import is_gl_enabled

        if is_gl_enabled(db):
            pl = build_profit_loss(
                db,
                from_date=month_s.date(),
                to_date=now.date(),
                domain=domain,
            )
            stats.gl_pl_revenue = pl.total_revenue
            stats.gl_pl_expense = pl.total_expense
            stats.gl_pl_net = pl.net_income
    except Exception:  # noqa: BLE001
        pass

    try:
        if domain in (None, BusinessDomain.RESTAURANT):
            ar = receivables_summary(db)
            stats.ar_outstanding_total = ar.total_outstanding
            stats.ar_invoice_count = ar.invoice_count_with_balance
            stats.ar_unpaid_count = ar.unpaid_count
            stats.ar_partial_count = ar.partial_count
    except Exception:  # noqa: BLE001
        pass

    try:
        ap = payables_summary(db, kind=PurchaseKind.INVENTORY, domain=domain)
        stats.ap_outstanding_total = ap.total_outstanding
        stats.ap_invoice_count = ap.invoice_count_with_balance
    except Exception:  # noqa: BLE001
        pass

    try:
        if domain in (None, BusinessDomain.RESTAURANT):
            sh = shift_shortages_summary(db)
            stats.shortage_cash_total = sh.cash_total
            stats.shortage_cash_count = sh.cash_count
            stats.shortage_bank_total = sh.bank_total
            stats.shortage_bank_count = sh.bank_count
    except Exception:  # noqa: BLE001
        pass

    try:
        stats.daily_burden = compute_daily_burden(db, domain=domain)
    except Exception:  # noqa: BLE001
        stats.daily_burden = None

    return stats
