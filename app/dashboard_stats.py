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
    month_gross_margin_pct: float = 0.0
    month_net_margin_pct: float = 0.0
    # حالة الأصول الثابتة الإجمالية (حتى الآن)
    fixed_assets_count: int = 0
    fixed_assets_cost: Decimal = Decimal("0")
    fixed_assets_book_value: Decimal = Decimal("0")
    fixed_assets_monthly_depr: Decimal = Decimal("0")
    # تحليل التعادل اليومي (CVP)
    daily_burden: object | None = None  # كائن DailyBurden

    top_today: list[TopProductRow] = field(default_factory=list)
    recent_sales: list[RecentSaleRow] = field(default_factory=list)
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


def _period_purchase_total(
    db: Session, start: datetime, end: datetime, kind: PurchaseKind
) -> Decimal:
    total = db.execute(
        select(func.coalesce(func.sum(Purchase.amount), 0)).where(
            Purchase.created_at >= start,
            Purchase.created_at < end,
            Purchase.kind == kind,
        )
    ).scalar_one()
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


def collect(db: Session) -> DashboardStats:
    """تجمع كل إحصائيات اللوحة في كائن واحد."""
    now = datetime.now(timezone.utc)
    today_s, today_e = _day_bounds(now, 0)
    yest_s, yest_e = _day_bounds(now, -1)
    month_s, month_e = _month_bounds(now)

    stats = DashboardStats()

    stats.today = _period_sales(db, today_s, today_e)
    stats.yesterday = _period_sales(db, yest_s, yest_e)
    stats.yesterday_date = yest_s.strftime("%Y-%m-%d")
    stats.month = _period_sales(db, month_s, month_e)

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
        db, month_s, month_e, PurchaseKind.INVENTORY
    )
    stats.month_expenses = _period_purchase_total(
        db, month_s, month_e, PurchaseKind.EXPENSE
    )
    stats.month_assets = _period_purchase_total(
        db, month_s, month_e, PurchaseKind.ASSET
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
    operating_assets_expense = (
        stats.month_assets_consumable + stats.month_assets_depreciation
    ).quantize(Decimal("0.001"))
    # حصة الرواتب والإيجار والاشتراكات الشهرية المنسوبة للشهر الحالي
    rec_monthly_total, rec_period_share, _ = recurring_costs_in_period(
        db, month_s, month_e
    )
    stats.month_recurring_total = rec_monthly_total
    stats.month_recurring_share = rec_period_share
    stats.month_recurring_breakdown = recurring_costs_breakdown_in_period(
        db, month_s, month_e
    )
    cogs_month = cogs_summary(db, month_s, month_e)
    stats.month_cogs = cogs_month.quantize(Decimal("0.001"))
    stats.month_gross_profit = (stats.month.revenue - cogs_month).quantize(
        Decimal("0.001")
    )
    stats.month_net_profit = (
        stats.month_gross_profit
        - stats.month_expenses
        - rec_period_share
        - operating_assets_expense
    ).quantize(Decimal("0.001"))

    fa = fixed_assets_summary(db)
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

    stats.top_today = _top_today(db, today_s, today_e, limit=5)
    stats.recent_sales = _recent_sales(db, limit=6)
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
    stats.treasury_cards = [
        TreasuryDashboardCard(
            method_id=m.id,
            name_ar=m.name_ar,
            kind=m.kind.value,
            balance=bal_map.get(m.id, Decimal("0")).quantize(Decimal("0.001")),
        )
        for m in list_payment_methods_for_dashboard(db, only_active=True)
    ]

    try:
        ar = receivables_summary(db)
        stats.ar_outstanding_total = ar.total_outstanding
        stats.ar_invoice_count = ar.invoice_count_with_balance
        stats.ar_unpaid_count = ar.unpaid_count
        stats.ar_partial_count = ar.partial_count
    except Exception:  # noqa: BLE001
        pass

    try:
        ap = payables_summary(db, kind=PurchaseKind.INVENTORY)
        stats.ap_outstanding_total = ap.total_outstanding
        stats.ap_invoice_count = ap.invoice_count_with_balance
    except Exception:  # noqa: BLE001
        pass

    try:
        stats.daily_burden = compute_daily_burden(db)
    except Exception:  # noqa: BLE001
        stats.daily_burden = None

    return stats
