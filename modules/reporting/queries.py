from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import and_, exists, func, select
from sqlalchemy.orm import Session

from modules.payments.models import RefundPayment, SalePayment
from modules.refunds.models import SaleReturn, SaleReturnLine, SaleReturnStatus
from modules.sales.models import Sale, SaleLine, SaleStatus


@dataclass
class SalesSummary:
    invoice_count: int
    return_count: int
    gross_revenue: Decimal
    returns_total: Decimal
    net_revenue: Decimal
    revenue: Decimal
    avg_basket: Decimal


def _range_utc(start: datetime, end: datetime):
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def sales_summary(db: Session, start: datetime, end: datetime) -> SalesSummary:
    s0, s1 = _range_utc(start, end)
    sales_stmt = select(
        func.count(Sale.id),
        func.coalesce(func.sum(Sale.total), 0),
    ).where(
        Sale.status == SaleStatus.COMPLETED,
        Sale.created_at >= s0,
        Sale.created_at < s1,
    )
    returns_stmt = select(
        func.count(SaleReturn.id),
        func.coalesce(func.sum(SaleReturn.total), 0),
    ).where(
        SaleReturn.status == SaleReturnStatus.POSTED,
        SaleReturn.created_at >= s0,
        SaleReturn.created_at < s1,
    )
    cnt, gross_rev = db.execute(sales_stmt).one()
    ret_cnt, ret_total = db.execute(returns_stmt).one()
    cnt = int(cnt or 0)
    ret_cnt = int(ret_cnt or 0)
    gross_rev = Decimal(str(gross_rev or 0)).quantize(Decimal("0.001"))
    ret_total = Decimal(str(ret_total or 0)).quantize(Decimal("0.001"))
    net_rev = (gross_rev - ret_total).quantize(Decimal("0.001"))
    avg = (
        (gross_rev / Decimal(cnt)).quantize(Decimal("0.001"))
        if cnt
        else Decimal("0")
    )
    return SalesSummary(
        invoice_count=cnt,
        return_count=ret_cnt,
        gross_revenue=gross_rev,
        returns_total=ret_total,
        net_revenue=net_rev,
        revenue=net_rev,
        avg_basket=avg,
    )


def _sales_product_map(
    db: Session, start: datetime, end: datetime
) -> dict[int, tuple[Decimal, Decimal]]:
    s0, s1 = _range_utc(start, end)
    rows = db.execute(
        select(
            SaleLine.product_id,
            func.coalesce(func.sum(SaleLine.quantity), 0),
            func.coalesce(func.sum(SaleLine.line_total), 0),
        )
        .join(Sale, SaleLine.sale_id == Sale.id)
        .where(
            Sale.status == SaleStatus.COMPLETED,
            Sale.created_at >= s0,
            Sale.created_at < s1,
        )
        .group_by(SaleLine.product_id)
    ).all()
    return {
        int(pid): (
            Decimal(str(qty or 0)).quantize(Decimal("0.0001")),
            Decimal(str(total or 0)).quantize(Decimal("0.001")),
        )
        for pid, qty, total in rows
    }


def _returns_product_map(
    db: Session, start: datetime, end: datetime
) -> dict[int, tuple[Decimal, Decimal]]:
    s0, s1 = _range_utc(start, end)
    rows = db.execute(
        select(
            SaleReturnLine.product_id,
            func.coalesce(func.sum(SaleReturnLine.quantity), 0),
            func.coalesce(func.sum(SaleReturnLine.line_total), 0),
        )
        .join(SaleReturn, SaleReturnLine.sale_return_id == SaleReturn.id)
        .where(
            SaleReturn.status == SaleReturnStatus.POSTED,
            SaleReturn.created_at >= s0,
            SaleReturn.created_at < s1,
        )
        .group_by(SaleReturnLine.product_id)
    ).all()
    return {
        int(pid): (
            Decimal(str(qty or 0)).quantize(Decimal("0.0001")),
            Decimal(str(total or 0)).quantize(Decimal("0.001")),
        )
        for pid, qty, total in rows
    }


def _returns_product_qty_restock_only(
    db: Session, start: datetime, end: datetime
) -> dict[int, Decimal]:
    """كميات المرتجع التي أُعيد مكوّنها للمخزن فقط (لعكس تكلفة المباع عند الترجيع)."""
    s0, s1 = _range_utc(start, end)
    rows = db.execute(
        select(
            SaleReturnLine.product_id,
            func.coalesce(func.sum(SaleReturnLine.quantity), 0),
        )
        .join(SaleReturn, SaleReturnLine.sale_return_id == SaleReturn.id)
        .where(
            SaleReturn.status == SaleReturnStatus.POSTED,
            SaleReturn.created_at >= s0,
            SaleReturn.created_at < s1,
            SaleReturnLine.restock.is_(True),
        )
        .group_by(SaleReturnLine.product_id)
    ).all()
    return {
        int(pid): Decimal(str(qty or 0)).quantize(Decimal("0.0001"))
        for pid, qty in rows
    }


def top_products(
    db: Session, start: datetime, end: datetime, limit: int = 20
) -> list[tuple[str, Decimal, Decimal]]:
    """Returns (product_name_ar, qty_sum, revenue_sum)."""
    s0, s1 = _range_utc(start, end)
    from modules.catalog.models import Product

    sales_map = _sales_product_map(db, s0, s1)
    returns_map = _returns_product_map(db, s0, s1)
    product_ids = sorted(set(sales_map) | set(returns_map))
    if not product_ids:
        return []

    names = {
        int(p.id): p.name_ar
        for p in db.scalars(select(Product).where(Product.id.in_(product_ids))).all()
    }
    rows: list[tuple[str, Decimal, Decimal]] = []
    for pid in product_ids:
        sold_qty, sold_total = sales_map.get(pid, (Decimal("0"), Decimal("0")))
        ret_qty, ret_total = returns_map.get(pid, (Decimal("0"), Decimal("0")))
        qty = (sold_qty - ret_qty).quantize(Decimal("0.0001"))
        revenue = (sold_total - ret_total).quantize(Decimal("0.001"))
        if qty == 0 and revenue == 0:
            continue
        rows.append((names.get(pid, f"#{pid}"), qty, revenue))
    rows.sort(key=lambda item: item[2], reverse=True)
    return rows[:limit]


def stock_movements_aggregate(
    db: Session,
    start: datetime,
    end: datetime,
    warehouse_id: int | None = None,
) -> list[tuple[str, Decimal]]:
    from modules.catalog.models import Product
    from modules.inventory.models import StockMovement

    s0, s1 = _range_utc(start, end)
    stmt = (
        select(Product.name_ar, func.sum(StockMovement.quantity))
        .join(Product, StockMovement.product_id == Product.id)
        .where(StockMovement.created_at >= s0, StockMovement.created_at < s1)
    )
    if warehouse_id is not None:
        stmt = stmt.where(StockMovement.warehouse_id == warehouse_id)
    stmt = stmt.group_by(Product.id, Product.name_ar).order_by(func.sum(StockMovement.quantity).asc())
    return [(r[0], Decimal(str(r[1] or 0))) for r in db.execute(stmt).all()]


def period_bounds(period: str) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    if period == "day":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
    elif period == "week":
        start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=7)
    elif period == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if start.month == 12:
            end = start.replace(year=start.year + 1, month=1)
        else:
            end = start.replace(month=start.month + 1)
    elif period == "year":
        start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        end = start.replace(year=start.year + 1)
    else:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
    return start, end


def parse_custom_range(start_str: str | None, end_str: str | None) -> tuple[datetime, datetime] | None:
    """يحلّل نص ISO/تاريخ بسيط ('YYYY-MM-DD') لبداية ونهاية الفترة (UTC).
    end يكون نهاية اليوم (الفترة [start, end+1d) إن كان إدخال تاريخ فقط).
    يعيد None إن كان أحد الإدخالين غير صالح."""
    if not start_str or not end_str:
        return None
    try:
        s = datetime.fromisoformat(start_str)
        e = datetime.fromisoformat(end_str)
    except ValueError:
        return None
    if s.tzinfo is None:
        s = s.replace(tzinfo=timezone.utc)
    if e.tzinfo is None:
        e = e.replace(tzinfo=timezone.utc)
    if len(end_str) == 10:
        e = e + timedelta(days=1)
    if e <= s:
        return None
    return s, e


def sales_by_payment_method(
    db: Session, start: datetime, end: datetime
) -> list[tuple[str, int, Decimal, int, Decimal, Decimal]]:
    """حركة التحصيل ورد المبالغ حسب أسلوب الدفع داخل الفترة."""
    from modules.payments.models import PaymentMethod, RefundPayment, SalePayment

    s0, s1 = _range_utc(start, end)
    sales_stmt = (
        select(
            PaymentMethod.name_ar,
            func.count(SalePayment.id),
            func.coalesce(func.sum(SalePayment.amount), 0),
        )
        .join(PaymentMethod, PaymentMethod.id == SalePayment.payment_method_id)
        .where(
            SalePayment.created_at >= s0,
            SalePayment.created_at < s1,
        )
        .group_by(PaymentMethod.id, PaymentMethod.name_ar)
        .order_by(PaymentMethod.sort_order, PaymentMethod.id)
    )
    refunds_stmt = (
        select(
            PaymentMethod.name_ar,
            func.count(RefundPayment.id),
            func.coalesce(func.sum(RefundPayment.amount), 0),
        )
        .join(PaymentMethod, PaymentMethod.id == RefundPayment.payment_method_id)
        .where(
            RefundPayment.created_at >= s0,
            RefundPayment.created_at < s1,
        )
        .group_by(PaymentMethod.id, PaymentMethod.name_ar)
        .order_by(PaymentMethod.sort_order, PaymentMethod.id)
    )
    sales_rows = {
        str(name): (
            int(cnt or 0),
            Decimal(str(total or 0)).quantize(Decimal("0.001")),
        )
        for name, cnt, total in db.execute(sales_stmt).all()
    }
    refund_rows = {
        str(name): (
            int(cnt or 0),
            Decimal(str(total or 0)).quantize(Decimal("0.001")),
        )
        for name, cnt, total in db.execute(refunds_stmt).all()
    }
    names = list(dict.fromkeys([*sales_rows.keys(), *refund_rows.keys()]))
    return [
        (
            name,
            sales_rows.get(name, (0, Decimal("0")))[0],
            sales_rows.get(name, (0, Decimal("0")))[1],
            refund_rows.get(name, (0, Decimal("0")))[0],
            refund_rows.get(name, (0, Decimal("0")))[1],
            (
                sales_rows.get(name, (0, Decimal("0")))[1]
                - refund_rows.get(name, (0, Decimal("0")))[1]
            ).quantize(Decimal("0.001")),
        )
        for name in names
    ]


def purchases_by_payment_method(
    db: Session,
    start: datetime,
    end: datetime,
    kind: "object | None" = None,
) -> list[tuple[str, int, Decimal]]:
    """فواتير الشراء/المصروفات لكل محفظة. مرّر `kind` للتصفية حسب النوع."""
    from modules.payments.models import PaymentMethod, Purchase, PurchasePayment

    s0, s1 = _range_utc(start, end)
    stmt = (
        select(
            PaymentMethod.name_ar,
            func.count(PurchasePayment.id),
            func.coalesce(func.sum(PurchasePayment.amount), 0),
        )
        .join(Purchase, Purchase.id == PurchasePayment.purchase_id)
        .join(PaymentMethod, PaymentMethod.id == PurchasePayment.payment_method_id)
        .where(PurchasePayment.created_at >= s0, PurchasePayment.created_at < s1)
        .group_by(PaymentMethod.id, PaymentMethod.name_ar)
        .order_by(PaymentMethod.sort_order, PaymentMethod.id)
    )
    if kind is not None:
        stmt = stmt.where(Purchase.kind == kind)
    return [
        (str(name), int(cnt or 0), Decimal(str(total or 0)))
        for name, cnt, total in db.execute(stmt).all()
    ]


@dataclass
class PurchasesSummary:
    count: int
    total: Decimal


def purchases_summary(db: Session, start: datetime, end: datetime) -> PurchasesSummary:
    from modules.payments.models import Purchase

    s0, s1 = _range_utc(start, end)
    cnt, total = db.execute(
        select(
            func.count(Purchase.id),
            func.coalesce(func.sum(Purchase.amount), 0),
        ).where(Purchase.created_at >= s0, Purchase.created_at < s1)
    ).one()
    return PurchasesSummary(count=int(cnt or 0), total=Decimal(str(total or 0)))


def _purchases_summary_by_kind(
    db: Session, start: datetime, end: datetime, kind
) -> PurchasesSummary:
    from modules.payments.models import Purchase

    s0, s1 = _range_utc(start, end)
    cnt, total = db.execute(
        select(
            func.count(Purchase.id),
            func.coalesce(func.sum(Purchase.amount), 0),
        ).where(
            Purchase.created_at >= s0,
            Purchase.created_at < s1,
            Purchase.kind == kind,
        )
    ).one()
    return PurchasesSummary(count=int(cnt or 0), total=Decimal(str(total or 0)))


def inventory_purchases_summary(
    db: Session, start: datetime, end: datetime
) -> PurchasesSummary:
    from modules.payments.models import PurchaseKind

    return _purchases_summary_by_kind(db, start, end, PurchaseKind.INVENTORY)


def expenses_summary(
    db: Session, start: datetime, end: datetime
) -> PurchasesSummary:
    from modules.payments.models import PurchaseKind

    return _purchases_summary_by_kind(db, start, end, PurchaseKind.EXPENSE)


def assets_summary(
    db: Session, start: datetime, end: datetime
) -> PurchasesSummary:
    from modules.payments.models import PurchaseKind

    return _purchases_summary_by_kind(db, start, end, PurchaseKind.ASSET)


def expenses_by_category(
    db: Session, start: datetime, end: datetime
) -> list[tuple[str, int, Decimal]]:
    """ملخّص المصروفات حسب التصنيف."""
    from modules.payments.models import Purchase, PurchaseKind

    s0, s1 = _range_utc(start, end)
    label = func.coalesce(Purchase.expense_category, "غير مصنّف")
    stmt = (
        select(
            label.label("cat"),
            func.count(Purchase.id),
            func.coalesce(func.sum(Purchase.amount), 0),
        )
        .where(
            Purchase.created_at >= s0,
            Purchase.created_at < s1,
            Purchase.kind == PurchaseKind.EXPENSE,
        )
        .group_by(label)
        .order_by(func.sum(Purchase.amount).desc())
    )
    return [
        (str(name), int(cnt or 0), Decimal(str(total or 0)))
        for name, cnt, total in db.execute(stmt).all()
    ]


def avg_unit_cost_per_product(db: Session) -> dict[int, Decimal]:
    """متوسط سعر شراء كل صنف من جميع فواتير الشراء (السعر الموزون بالكميات)."""
    from modules.payments.models import PurchaseLine

    stmt = (
        select(
            PurchaseLine.product_id,
            func.coalesce(func.sum(PurchaseLine.line_total), 0),
            func.coalesce(func.sum(PurchaseLine.quantity), 0),
        )
        .where(PurchaseLine.product_id.is_not(None))
        .group_by(PurchaseLine.product_id)
    )
    out: dict[int, Decimal] = {}
    for pid, total_cost, total_qty in db.execute(stmt).all():
        if pid is None:
            continue
        try:
            qty = Decimal(str(total_qty or 0))
            cost = Decimal(str(total_cost or 0))
            if qty > 0:
                out[int(pid)] = (cost / qty).quantize(Decimal("0.001"))
            else:
                out[int(pid)] = Decimal("0")
        except Exception:
            out[int(pid)] = Decimal("0")
    return out


def cogs_summary(db: Session, start: datetime, end: datetime) -> Decimal:
    """تكلفة البضاعة المباعة في الفترة (تقدير محاسبي موحَّد).

    تُحسب لكل بند بيع كالتالي:
        unit_cost = Σ(qty_per_parent × avg_cost(component))   إن وُجدت وصفة تركيب
        unit_cost = avg_cost(product)                         خلاف ذلك (منتج بسيط مُشترى)
        cogs(line) = qty_sold × unit_cost

    الإجمالي = Σ cogs(line) لكل البنود في الفترة.

    لماذا هذه الصيغة وليس الاعتماد على حركات SALE فقط؟
    لأن المبيعات القديمة التي تمّت قبل تعريف وصفات التركيب لا تنتج حركات مخزون،
    فيظهر إجمالي COGS أقل من الحقيقة. هذه الصيغة موحَّدة مع `profit_by_product`
    لذا يتطابق الرقم الكلي مع مجموع عمود «تكلفة المباع» في تقرير الأرباح بالتفصيل.

    مرتجعات بند «بدون عودة للمخزن» تُنقص الإيراد لكن لا تُعكس تكلفة المكوّنات
    (المواد استُهلكت فعلاً).
    """
    from modules.catalog.models import Product  # noqa: F401  registers mapper

    sales_map = _sales_product_map(db, start, end)
    returns_map = _returns_product_map(db, start, end)
    ret_qty_restock = _returns_product_qty_restock_only(db, start, end)
    avg_costs = avg_unit_cost_per_product(db)
    cogs = Decimal("0")
    for pid in set(sales_map) | set(returns_map):
        sold_qty = sales_map.get(pid, (Decimal("0"), Decimal("0")))[0]
        ret_cogs = ret_qty_restock.get(int(pid), Decimal("0"))
        q = (sold_qty - ret_cogs).quantize(Decimal("0.0001"))
        if q == 0:
            continue
        unit_cost = _unit_cost_via_bom(db, int(pid), avg_costs)
        cogs += q * unit_cost
    return cogs.quantize(Decimal("0.001"))


@dataclass
class InventoryRow:
    product_id: int
    name_ar: str
    unit: str
    quantity: Decimal
    reorder_level: Decimal
    sell_price: Decimal | None
    avg_cost: Decimal
    is_low: bool


def inventory_snapshot(
    db: Session,
    search: str | None = None,
    only_low: bool = False,
    warehouse_id: int | None = None,
) -> list[InventoryRow]:
    """صورة للمخزون الحالي مع متوسط التكلفة وتنبيه الحد الأدنى."""
    from modules.catalog.models import Product, ProductKind
    from modules.inventory.models import StockBalance
    from modules.inventory.service import get_main_warehouse, resolve_warehouse_id

    wid = resolve_warehouse_id(db, warehouse_id) if warehouse_id is not None else get_main_warehouse(db).id
    stmt = (
        select(Product, StockBalance.quantity)
        .join(
            StockBalance,
            (StockBalance.product_id == Product.id) & (StockBalance.warehouse_id == wid),
            isouter=True,
        )
        .where(Product.is_active.is_(True))
        .where(Product.kind == ProductKind.STOCK_ONLY)
        .order_by(Product.name_ar)
    )
    if search:
        like = f"%{search.strip()}%"
        stmt = stmt.where(Product.name_ar.like(like))
    rows = db.execute(stmt).all()
    avg_costs = avg_unit_cost_per_product(db)
    out: list[InventoryRow] = []
    for p, qty in rows:
        q = qty if qty is not None else Decimal("0")
        is_low = bool(p.reorder_level and Decimal(str(p.reorder_level)) > 0 and q <= p.reorder_level)
        if only_low and not is_low:
            continue
        out.append(
            InventoryRow(
                product_id=p.id,
                name_ar=p.name_ar,
                unit=p.unit or "",
                quantity=q,
                reorder_level=p.reorder_level or Decimal("0"),
                sell_price=p.sell_price,
                avg_cost=avg_costs.get(p.id, Decimal("0")),
                is_low=is_low,
            )
        )
    return out


def inventory_value(db: Session, warehouse_id: int | None = None) -> Decimal:
    """قيمة المخزون = Σ(الكمية × متوسط التكلفة). بدون warehouse_id: كل المخازن."""
    from modules.inventory.models import StockBalance

    avg_costs = avg_unit_cost_per_product(db)
    stmt = select(StockBalance)
    if warehouse_id is not None:
        stmt = stmt.where(StockBalance.warehouse_id == warehouse_id)
    rows = db.scalars(stmt).all()
    val = Decimal("0")
    for r in rows:
        val += (r.quantity or Decimal("0")) * (avg_costs.get(r.product_id, Decimal("0")))
    return val.quantize(Decimal("0.001"))


def inventory_value_by_warehouse(db: Session) -> list[tuple[str, Decimal]]:
    """قيمة المخزون لكل مخزن مفعّل: [(اسم المخزن, القيمة), ...]."""
    from modules.inventory.service import list_warehouses

    out: list[tuple[str, Decimal]] = []
    for wh in list_warehouses(db):
        out.append((wh.name_ar, inventory_value(db, wh.id)))
    return out


@dataclass
class ProfitRow:
    product_id: int
    name_ar: str
    qty_sold: Decimal
    revenue: Decimal
    avg_cost: Decimal
    cogs: Decimal
    gross_profit: Decimal


def _unit_cost_via_bom(
    db: Session, product_id: int, avg_costs: dict[int, Decimal]
) -> Decimal:
    """تكلفة وحدة المنتج: لو له وصفة تركيب فالتكلفة = Σ(qty_per × cost(component)).
    لو لا وصفة فهي متوسط شراء المنتج نفسه (للمنتجات البسيطة المُشتراة)."""
    from modules.catalog.models import BillOfMaterialsLine

    bom = list(
        db.scalars(
            select(BillOfMaterialsLine).where(
                BillOfMaterialsLine.parent_product_id == product_id
            )
        ).all()
    )
    if not bom:
        return avg_costs.get(int(product_id), Decimal("0"))
    cost = Decimal("0")
    for b in bom:
        cost += b.qty_per_parent * avg_costs.get(int(b.component_product_id), Decimal("0"))
    return cost.quantize(Decimal("0.001"))


def profit_by_product(
    db: Session, start: datetime, end: datetime, limit: int = 100
) -> list[ProfitRow]:
    from modules.catalog.models import Product  # noqa: F401  registers mapper

    sales_map = _sales_product_map(db, start, end)
    returns_map = _returns_product_map(db, start, end)
    ret_qty_restock = _returns_product_qty_restock_only(db, start, end)
    avg_costs = avg_unit_cost_per_product(db)
    out: list[ProfitRow] = []
    for pid in set(sales_map) | set(returns_map):
        sold_qty, sold_rev = sales_map.get(pid, (Decimal("0"), Decimal("0")))
        ret_qty, ret_rev = returns_map.get(pid, (Decimal("0"), Decimal("0")))
        ret_cogs = ret_qty_restock.get(int(pid), Decimal("0"))
        q = (sold_qty - ret_qty).quantize(Decimal("0.0001"))
        r = (sold_rev - ret_rev).quantize(Decimal("0.001"))
        if q == 0 and r == 0:
            continue
        prod = db.get(Product, int(pid))
        name = prod.name_ar if prod else f"#{pid}"
        unit_cost = _unit_cost_via_bom(db, int(pid), avg_costs)
        q_cogs = (sold_qty - ret_cogs).quantize(Decimal("0.0001"))
        cogs = (q_cogs * unit_cost).quantize(Decimal("0.001"))
        out.append(
            ProfitRow(
                product_id=int(pid),
                name_ar=str(name),
                qty_sold=q,
                revenue=r,
                avg_cost=unit_cost,
                cogs=cogs,
                gross_profit=(r - cogs).quantize(Decimal("0.001")),
            )
        )
    out.sort(key=lambda row: row.revenue, reverse=True)
    return out[:limit]


# --- تقرير مالي تشغيلي موثّق (بدون دفتر قيود عام بعد) ---


@dataclass
class ReportFilters:
    """فلاتر اختيارية على مستندات التشغيل (ليست فرع/مركز تكلفة — غير موجودة في النموذج حالياً)."""

    user_id: int | None = None
    payment_method_id: int | None = None
    customer_id: int | None = None

    def active(self) -> bool:
        return any(
            x is not None for x in (self.user_id, self.payment_method_id, self.customer_id)
        )


@dataclass
class FinancialOperationalStatement:
    """أرقام مستخرجة من جداول التشغيل المرحّلة فقط — مع تفسير محاسبي تشغيلي."""

    invoice_count: int
    return_count: int
    gross_revenue: Decimal
    returns_total: Decimal
    net_revenue: Decimal
    collections_total: Decimal
    refunds_payment_total: Decimal
    net_cash_effect: Decimal
    taxes_included: Decimal
    line_discounts_included: Decimal


def _sale_period_conditions(s0, s1, filters: ReportFilters):
    conds = [
        Sale.status == SaleStatus.COMPLETED,
        Sale.created_at >= s0,
        Sale.created_at < s1,
    ]
    if filters.user_id is not None:
        conds.append(Sale.created_by_id == filters.user_id)
    if filters.customer_id is not None:
        conds.append(Sale.customer_id == filters.customer_id)
    if filters.payment_method_id is not None:
        conds.append(
            exists(
                select(1)
                .select_from(SalePayment)
                .where(
                    SalePayment.sale_id == Sale.id,
                    SalePayment.payment_method_id == filters.payment_method_id,
                )
            )
        )
    return and_(*conds)


def financial_operational_statement(
    db: Session, start: datetime, end: datetime, filters: ReportFilters
) -> FinancialOperationalStatement:
    """ملخص مالي تشغيلي:
    - الإيراد: مجموع ``Sale.total`` لفواتير **مكتملة** ضمن الفترة (أساس الاستحقاق على تاريخ إتمام الفاتورة).
    - المرتجعات: مجموع ``SaleReturn.total`` لسندات **مرحّلة** ضمن الفترة (منفصلة — لا تُضاعف مع الإيراد الخام).
    - التحصيل: مجموع ``SalePayment.amount`` لدفعات فواتير تدخل ضمن نفس فلتر الفواتير أعلاه
      (بغض النظر عن ``created_at`` للدفعة — مرتبط بفاتورة أُنجزت في الفترة).
    - ردود المبالغ: مجموع ``RefundPayment.amount`` لمرتجعات مرحّلة في الفترة ومطابقة للفلاتر.
    """
    s0, s1 = _range_utc(start, end)
    sale_conds = _sale_period_conditions(s0, s1, filters)

    cnt_gross = db.execute(
        select(func.count(Sale.id), func.coalesce(func.sum(Sale.total), 0)).where(sale_conds)
    ).one()
    invoice_count = int(cnt_gross[0] or 0)
    gross_revenue = Decimal(str(cnt_gross[1] or 0)).quantize(Decimal("0.001"))

    ret_conds = [
        SaleReturn.status == SaleReturnStatus.POSTED,
        SaleReturn.created_at >= s0,
        SaleReturn.created_at < s1,
    ]
    if filters.user_id is not None:
        ret_conds.append(SaleReturn.created_by_id == filters.user_id)
    if filters.customer_id is not None:
        ret_conds.append(Sale.customer_id == filters.customer_id)
    if filters.payment_method_id is not None:
        ret_conds.append(
            or_(
                SaleReturn.refund_payment_method_id == filters.payment_method_id,
                SaleReturn.original_payment_method_id == filters.payment_method_id,
            )
        )

    cnt_ret = db.execute(
        select(func.count(SaleReturn.id), func.coalesce(func.sum(SaleReturn.total), 0))
        .select_from(ret_join)
        .where(and_(*ret_conds))
    ).one()
    return_count = int(cnt_ret[0] or 0)
    returns_total = Decimal(str(cnt_ret[1] or 0)).quantize(Decimal("0.001"))
    net_revenue = (gross_revenue - returns_total).quantize(Decimal("0.001"))

    coll = db.execute(
        select(func.coalesce(func.sum(SalePayment.amount), 0))
        .join(Sale, SalePayment.sale_id == Sale.id)
        .where(sale_conds)
    ).scalar_one()
    collections_total = Decimal(str(coll or 0)).quantize(Decimal("0.001"))

    rp_conds = [
        SaleReturn.status == SaleReturnStatus.POSTED,
        SaleReturn.created_at >= s0,
        SaleReturn.created_at < s1,
    ]
    if filters.user_id is not None:
        rp_conds.append(SaleReturn.created_by_id == filters.user_id)
    if filters.customer_id is not None:
        rp_conds.append(Sale.customer_id == filters.customer_id)
    if filters.payment_method_id is not None:
        rp_conds.append(RefundPayment.payment_method_id == filters.payment_method_id)

    ref_sum = db.execute(
        select(func.coalesce(func.sum(RefundPayment.amount), 0))
        .join(SaleReturn, RefundPayment.sale_return_id == SaleReturn.id)
        .join(Sale, SaleReturn.original_sale_id == Sale.id)
        .where(and_(*rp_conds))
    ).scalar_one()
    refunds_payment_total = Decimal(str(ref_sum or 0)).quantize(Decimal("0.001"))
    net_cash_effect = (collections_total - refunds_payment_total).quantize(Decimal("0.001"))

    return FinancialOperationalStatement(
        invoice_count=invoice_count,
        return_count=return_count,
        gross_revenue=gross_revenue,
        returns_total=returns_total,
        net_revenue=net_revenue,
        collections_total=collections_total,
        refunds_payment_total=refunds_payment_total,
        net_cash_effect=net_cash_effect,
        taxes_included=Decimal("0"),
        line_discounts_included=Decimal("0"),
    )


def financial_operational_logical_lines(
    stmt: FinancialOperationalStatement,
) -> list[tuple[str, str, Decimal, str]]:
    """صفوف توضيحية لعرض «منطق القيد» المشتق من الأرقام أعلاه (ليست قيوداً مرحّلة في دفتر)."""
    rows: list[tuple[str, str, Decimal, str]] = [
        ("إيراد مبيعات (استحقاق — فواتير مكتملة)", "Credit", stmt.gross_revenue, "مصدر: sales.total"),
        ("خصم مرتجعات بيع (سندات مرحّلة)", "Debit", stmt.returns_total, "مصدر: sale_returns.total"),
        ("صافي إيراد الاستحقاق", "—", stmt.net_revenue, "gross − returns"),
        ("تحصيلات طرق الدفع (مرتبطة بفواتير الفترة)", "Debit", stmt.collections_total, "مصدر: sale_payments.amount"),
        ("ردود مبالغ المرتجعات", "Credit", stmt.refunds_payment_total, "مصدر: refund_payments.amount"),
        ("صافي أثر نقدي تشغيلي (تحصيل − ردود)", "—", stmt.net_cash_effect, "للمقارنة مع المحافظ — ليس قيداً متوازناً حتى يُبنى GL"),
    ]
    return rows
