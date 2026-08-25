from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import Session, selectinload

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


def _sale_completed_at():
    """وقت إتمام البيع للتقارير: آخر دفعة تحصيل، أو وقت إنشاء الفاتورة إن لم تُسجَّل دفعة."""
    return func.coalesce(
        select(func.max(SalePayment.created_at))
        .where(SalePayment.sale_id == Sale.id)
        .correlate(Sale)
        .scalar_subquery(),
        Sale.created_at,
    )


def _sale_in_period(s0, s1):
    completed = _sale_completed_at()
    return and_(
        Sale.status == SaleStatus.COMPLETED,
        completed >= s0,
        completed < s1,
    )


def sales_summary(db: Session, start: datetime, end: datetime) -> SalesSummary:
    s0, s1 = _range_utc(start, end)
    sales_stmt = select(
        func.count(Sale.id),
        func.coalesce(func.sum(Sale.total), 0),
    ).where(_sale_in_period(s0, s1))
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
        .where(_sale_in_period(s0, s1))
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
    from app.datetime_local import local_period_bounds

    return local_period_bounds(period)


def parse_custom_range(start_str: str | None, end_str: str | None) -> tuple[datetime, datetime] | None:
    """يحلّل نص ISO/تاريخ بسيط ('YYYY-MM-DD') لبداية ونهاية الفترة (تقويم محلي → UTC)."""
    from app.datetime_local import parse_local_date_range

    return parse_local_date_range(start_str, end_str)


@dataclass
class ProductInvoiceRow:
    sale_id: int
    sale_at: datetime
    product_id: int
    product_name: str
    quantity: Decimal
    unit_price: Decimal
    line_total: Decimal
    sale_total: Decimal
    source: str
    context_type: str
    customer_name: str | None


@dataclass
class ProductInvoiceSummary:
    invoice_count: int
    line_count: int
    qty_total: Decimal
    amount_total: Decimal


def search_catalog_products(db: Session, q: str, *, limit: int = 40) -> list:
    """بحث أصناف بالاسم / الباركود / SKU."""
    from modules.catalog.models import Product

    term = (q or "").strip()
    if not term:
        return []
    like = f"%{term}%"
    exact = list(
        db.scalars(
            select(Product)
            .where(
                or_(
                    Product.name_ar == term,
                    Product.barcode == term,
                    Product.sku == term,
                )
            )
            .order_by(Product.name_ar)
            .limit(limit)
        ).all()
    )
    if exact:
        return exact
    return list(
        db.scalars(
            select(Product)
            .where(
                or_(
                    Product.name_ar.like(like),
                    Product.barcode.like(like),
                    Product.sku.like(like),
                )
            )
            .order_by(Product.name_ar)
            .limit(limit)
        ).all()
    )


def product_sale_invoices(
    db: Session,
    start: datetime,
    end: datetime,
    *,
    product_ids: list[int],
) -> tuple[list[ProductInvoiceRow], ProductInvoiceSummary]:
    """فواتير البيع المكتملة التي تحتوي الأصناف المحددة خلال الفترة."""
    from modules.catalog.models import Product
    from modules.customers.models import Customer

    ids = sorted({int(x) for x in product_ids if x})
    empty = ProductInvoiceSummary(
        invoice_count=0,
        line_count=0,
        qty_total=Decimal("0"),
        amount_total=Decimal("0"),
    )
    if not ids:
        return [], empty

    s0, s1 = _range_utc(start, end)
    completed = _sale_completed_at()
    stmt = (
        select(
            Sale.id,
            completed.label("sale_at"),
            Product.id,
            Product.name_ar,
            SaleLine.quantity,
            SaleLine.unit_price,
            SaleLine.line_total,
            Sale.total,
            Sale.source,
            Sale.context_type,
            Customer.name,
            Customer.company_name,
            Customer.phone,
        )
        .join(SaleLine, SaleLine.sale_id == Sale.id)
        .join(Product, Product.id == SaleLine.product_id)
        .outerjoin(Customer, Customer.id == Sale.customer_id)
        .where(
            Sale.status == SaleStatus.COMPLETED,
            SaleLine.product_id.in_(ids),
            completed >= s0,
            completed < s1,
        )
        .order_by(completed.desc(), Sale.id.desc(), Product.name_ar)
    )
    rows_raw = db.execute(stmt).all()
    rows: list[ProductInvoiceRow] = []
    sale_ids: set[int] = set()
    qty_total = Decimal("0")
    amount_total = Decimal("0")
    for (
        sid,
        sale_at,
        pid,
        pname,
        qty,
        price,
        line_total,
        sale_total,
        source,
        ctx,
        cust_name,
        company_name,
        phone,
    ) in rows_raw:
        qv = Decimal(str(qty or 0))
        lv = Decimal(str(line_total or 0)).quantize(Decimal("0.001"))
        qty_total += qv
        amount_total += lv
        sale_ids.add(int(sid))
        src = source.value if hasattr(source, "value") else str(source or "")
        ctx_s = ctx.value if hasattr(ctx, "value") else str(ctx or "")
        display = (str(cust_name or "").strip() or str(company_name or "").strip() or str(phone or "").strip() or None)
        rows.append(
            ProductInvoiceRow(
                sale_id=int(sid),
                sale_at=sale_at,
                product_id=int(pid),
                product_name=str(pname or ""),
                quantity=qv,
                unit_price=Decimal(str(price or 0)).quantize(Decimal("0.001")),
                line_total=lv,
                sale_total=Decimal(str(sale_total or 0)).quantize(Decimal("0.001")),
                source=src,
                context_type=ctx_s,
                customer_name=display,
            )
        )
    summary = ProductInvoiceSummary(
        invoice_count=len(sale_ids),
        line_count=len(rows),
        qty_total=qty_total,
        amount_total=amount_total.quantize(Decimal("0.001")),
    )
    return rows, summary


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


def payment_flow_totals(rows) -> tuple[Decimal, Decimal, Decimal]:
    cash_in = sum((row[2] for row in rows), Decimal("0"))
    refunds_out = sum((row[4] for row in rows), Decimal("0"))
    net = (cash_in - refunds_out).quantize(Decimal("0.001"))
    return (
        cash_in.quantize(Decimal("0.001")),
        refunds_out.quantize(Decimal("0.001")),
        net,
    )


def purchases_by_payment_method(
    db: Session,
    start: datetime,
    end: datetime,
    kind: "object | None" = None,
    domain=None,
) -> list[tuple[str, int, Decimal]]:
    """فواتير الشراء/المصروفات لكل محفظة. مرّر `kind` للتصفية حسب النوع."""
    from modules.payments.models import PaymentMethod, Purchase, PurchasePayment
    from modules.platform.business_domain import purchase_domain_db_values

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
    domain_vals = purchase_domain_db_values(domain)
    if domain_vals is not None:
        stmt = stmt.where(Purchase.business_domain.in_(domain_vals))
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
    db: Session, start: datetime, end: datetime, kind, domain=None
) -> PurchasesSummary:
    from modules.payments.models import Purchase
    from modules.platform.business_domain import purchase_domain_db_values

    s0, s1 = _range_utc(start, end)
    stmt = select(
        func.count(Purchase.id),
        func.coalesce(func.sum(Purchase.amount), 0),
    ).where(
        Purchase.created_at >= s0,
        Purchase.created_at < s1,
        Purchase.kind == kind,
    )
    domain_vals = purchase_domain_db_values(domain)
    if domain_vals is not None:
        stmt = stmt.where(Purchase.business_domain.in_(domain_vals))
    cnt, total = db.execute(stmt).one()
    return PurchasesSummary(count=int(cnt or 0), total=Decimal(str(total or 0)))


def inventory_purchases_summary(
    db: Session, start: datetime, end: datetime, domain=None
) -> PurchasesSummary:
    from modules.payments.models import PurchaseKind

    return _purchases_summary_by_kind(db, start, end, PurchaseKind.INVENTORY, domain=domain)


def expenses_summary(
    db: Session, start: datetime, end: datetime, domain=None
) -> PurchasesSummary:
    from modules.payments.models import PurchaseKind

    return _purchases_summary_by_kind(db, start, end, PurchaseKind.EXPENSE, domain=domain)


def expenses_total_for_profit(
    db: Session, start: datetime, end: datetime, domain=None
) -> Decimal:
    """مصروفات تُطرح من الربح — تستثني استحقاق نقاط الولاء (تُحسب منفصلة)."""
    from modules.payments.models import Purchase, PurchaseKind
    from modules.platform.business_domain import purchase_domain_db_values
    from modules.pos_shifts.loyalty_settlement import LOYALTY_OPERATING_EXPENSE_CATEGORY

    s0, s1 = _range_utc(start, end)
    stmt = select(func.coalesce(func.sum(Purchase.amount), 0)).where(
        Purchase.created_at >= s0,
        Purchase.created_at < s1,
        Purchase.kind == PurchaseKind.EXPENSE,
        Purchase.expense_category != LOYALTY_OPERATING_EXPENSE_CATEGORY,
    )
    domain_vals = purchase_domain_db_values(domain)
    if domain_vals is not None:
        stmt = stmt.where(Purchase.business_domain.in_(domain_vals))
    total = db.execute(stmt).scalar_one()
    return Decimal(str(total or 0)).quantize(Decimal("0.001"))


def assets_summary(
    db: Session, start: datetime, end: datetime, domain=None
) -> PurchasesSummary:
    from modules.payments.models import PurchaseKind

    return _purchases_summary_by_kind(db, start, end, PurchaseKind.ASSET, domain=domain)


def expenses_by_category(
    db: Session, start: datetime, end: datetime, domain=None
) -> list[tuple[str, int, Decimal]]:
    """ملخّص المصروفات حسب التصنيف."""
    from modules.payments.models import Purchase, PurchaseKind
    from modules.platform.business_domain import purchase_domain_db_values

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
    domain_vals = purchase_domain_db_values(domain)
    if domain_vals is not None:
        stmt = stmt.where(Purchase.business_domain.in_(domain_vals))
    return [
        (str(name), int(cnt or 0), Decimal(str(total or 0)))
        for name, cnt, total in db.execute(stmt).all()
    ]


def avg_unit_cost_per_product(db: Session) -> dict[int, Decimal]:
    """تكلفة الوحدة لكل صنف — FIFO (أقدم دفعة) مع احتياط المتوسط الموزون."""
    from modules.inventory.costing import fifo_unit_cost_map

    fifo = fifo_unit_cost_map(db)
    if fifo:
        legacy = _weighted_avg_unit_cost_per_product(db)
        for pid, cost in legacy.items():
            fifo.setdefault(pid, cost)
        return fifo
    return _weighted_avg_unit_cost_per_product(db)


def _weighted_avg_unit_cost_per_product(db: Session) -> dict[int, Decimal]:
    """متوسط سعر شراء كل صنف من فواتير شراء البضاعة (السعر الموزون بالكميات)."""
    from modules.payments.models import Purchase, PurchaseKind, PurchaseLine

    stmt = (
        select(
            PurchaseLine.product_id,
            func.coalesce(func.sum(PurchaseLine.line_total), 0),
            func.coalesce(func.sum(PurchaseLine.quantity), 0),
        )
        .join(Purchase, Purchase.id == PurchaseLine.purchase_id)
        .where(
            PurchaseLine.product_id.is_not(None),
            Purchase.kind == PurchaseKind.INVENTORY,
        )
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
    cogs += packaging_cogs_summary(db, start, end)
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
    from modules.inventory.service import get_balance, get_main_warehouse, is_low_stock, resolve_warehouse_id

    wid = resolve_warehouse_id(db, warehouse_id) if warehouse_id is not None else get_main_warehouse(db).id
    stmt = (
        select(Product)
        .where(Product.is_active.is_(True))
        .where(
            (Product.kind == ProductKind.STOCK_ONLY)
            | (
                (Product.kind == ProductKind.FINAL_SELLABLE)
                & Product.direct_purchase_enabled.is_(True)
            )
        )
        .order_by(Product.name_ar)
    )
    if search:
        like = f"%{search.strip()}%"
        stmt = stmt.where(Product.name_ar.like(like))
    products = list(db.scalars(stmt).all())
    avg_costs = avg_unit_cost_per_product(db)
    out: list[InventoryRow] = []
    for p in products:
        q = get_balance(db, p.id, wid)
        is_low = is_low_stock(q, p.reorder_level)
        if only_low and not is_low:
            continue
        unit_cost = avg_costs.get(p.id, Decimal("0"))
        out.append(
            InventoryRow(
                product_id=p.id,
                name_ar=p.name_ar,
                unit=p.unit or "",
                quantity=q,
                reorder_level=p.reorder_level or Decimal("0"),
                sell_price=p.sell_price,
                avg_cost=unit_cost,
                is_low=is_low,
            )
        )
    return out


def inventory_value(db: Session, warehouse_id: int | None = None) -> Decimal:
    """قيمة المخزون = Σ(الكمية × متوسط التكلفة). بدون warehouse_id: كل المخازن."""
    from modules.catalog.models import Product, ProductKind
    from modules.inventory.service import get_balance, list_warehouses, resolve_warehouse_id

    avg_costs = avg_unit_cost_per_product(db)
    products = list(
        db.scalars(
            select(Product).where(
                Product.is_active.is_(True),
                (
                    (Product.kind == ProductKind.STOCK_ONLY)
                    | (
                        (Product.kind == ProductKind.FINAL_SELLABLE)
                        & Product.direct_purchase_enabled.is_(True)
                    )
                ),
            )
        ).all()
    )
    if warehouse_id is not None:
        warehouse_ids = [resolve_warehouse_id(db, warehouse_id)]
    else:
        warehouse_ids = [w.id for w in list_warehouses(db)]
    val = Decimal("0")
    for p in products:
        unit_cost = avg_costs.get(p.id, Decimal("0"))
        for wid in warehouse_ids:
            val += get_balance(db, p.id, wid) * unit_cost
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
    db: Session, product_id: int, avg_costs: dict[int, Decimal], *, _stack: frozenset[int] | None = None
) -> Decimal:
    """تكلفة وحدة المنتج مع توسيع المنتجات الوسيطة (بدون مواد التغليف)."""
    from modules.catalog.bom_explosion import product_has_recipe
    from modules.catalog.models import BillOfMaterialsLine

    stack = _stack or frozenset()
    if product_id in stack:
        return Decimal("0")
    stack = stack | {product_id}

    bom = list(
        db.scalars(
            select(BillOfMaterialsLine).where(
                BillOfMaterialsLine.parent_product_id == product_id
            )
        ).all()
    )
    if not bom:
        from modules.catalog.models import Product

        prod = db.get(Product, int(product_id))
        if prod is not None and prod.reference_unit_cost:
            ref = Decimal(str(prod.reference_unit_cost)).quantize(Decimal("0.001"))
            if ref > Decimal("0"):
                return ref
        return avg_costs.get(int(product_id), Decimal("0"))
    cost = Decimal("0")
    for b in bom:
        if b.packaging_only:
            continue
        comp_id = int(b.component_product_id)
        if product_has_recipe(db, comp_id):
            unit = _unit_cost_via_bom(db, comp_id, avg_costs, _stack=stack)
        else:
            unit = avg_costs.get(comp_id, Decimal("0"))
        cost += b.qty_per_parent * unit
    return cost.quantize(Decimal("0.001"))


def _packaging_unit_cost_via_bom(
    db: Session, product_id: int, avg_costs: dict[int, Decimal], *, _stack: frozenset[int] | None = None
) -> Decimal:
    """تكلفة مواد التغليف لوحدة واحدة من المنتج."""
    from modules.catalog.bom_explosion import product_has_recipe
    from modules.catalog.models import BillOfMaterialsLine

    stack = _stack or frozenset()
    if product_id in stack:
        return Decimal("0")
    stack = stack | {product_id}

    bom = list(
        db.scalars(
            select(BillOfMaterialsLine).where(
                BillOfMaterialsLine.parent_product_id == product_id,
                BillOfMaterialsLine.packaging_only.is_(True),
            )
        ).all()
    )
    if not bom:
        return Decimal("0")
    cost = Decimal("0")
    for b in bom:
        comp_id = int(b.component_product_id)
        if product_has_recipe(db, comp_id):
            unit = _unit_cost_via_bom(db, comp_id, avg_costs, _stack=stack)
        else:
            unit = avg_costs.get(comp_id, Decimal("0"))
        cost += b.qty_per_parent * unit
    return cost.quantize(Decimal("0.001"))


def packaging_cogs_summary(db: Session, start: datetime, end: datetime) -> Decimal:
    """تكلفة مواد التغليف للطلبات التي تتطلب تغليفاً (أونلاين / استلام / توصيل)."""
    from modules.catalog.models import Product  # noqa: F401
    from modules.sales.models import Sale, SaleLine, SaleStatus
    from modules.sales.packaging import sale_requires_packaging

    s0, s1 = _range_utc(start, end)
    sales = list(
        db.scalars(
            select(Sale)
            .where(_sale_in_period(s0, s1))
            .options(selectinload(Sale.lines))
        ).all()
    )
    avg_costs = avg_unit_cost_per_product(db)
    total = Decimal("0")
    for sale in sales:
        if not sale_requires_packaging(sale):
            continue
        for line in sale.lines:
            if not line.product_id:
                continue
            unit = _packaging_unit_cost_via_bom(db, int(line.product_id), avg_costs)
            if unit <= 0:
                continue
            total += Decimal(str(line.quantity or 0)) * unit
    return total.quantize(Decimal("0.001"))


@dataclass
class PackagingCostRow:
    product_id: int
    name_ar: str
    qty_used: Decimal
    unit_cost: Decimal
    total_cost: Decimal


def packaging_cost_breakdown(
    db: Session, start: datetime, end: datetime, limit: int = 50
) -> list[PackagingCostRow]:
    """تفصيل تكلفة التغليف حسب مكوّن التغليف المستخدم."""
    from modules.catalog.models import BillOfMaterialsLine, Product
    from modules.sales.models import Sale
    from modules.sales.packaging import sale_requires_packaging

    s0, s1 = _range_utc(start, end)
    sales = list(
        db.scalars(
            select(Sale)
            .where(_sale_in_period(s0, s1))
            .options(selectinload(Sale.lines))
        ).all()
    )
    avg_costs = avg_unit_cost_per_product(db)
    comp_qty: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
    for sale in sales:
        if not sale_requires_packaging(sale):
            continue
        for line in sale.lines:
            if not line.product_id:
                continue
            parent_id = int(line.product_id)
            sold_qty = Decimal(str(line.quantity or 0))
            if sold_qty <= 0:
                continue
            pkg_lines = db.scalars(
                select(BillOfMaterialsLine).where(
                    BillOfMaterialsLine.parent_product_id == parent_id,
                    BillOfMaterialsLine.packaging_only.is_(True),
                )
            ).all()
            for bl in pkg_lines:
                comp_id = int(bl.component_product_id)
                need = (Decimal(str(bl.qty_per_parent or 0)) * sold_qty).quantize(
                    Decimal("0.0001")
                )
                comp_qty[comp_id] += need
    out: list[PackagingCostRow] = []
    for comp_id, qty in comp_qty.items():
        if qty <= 0:
            continue
        prod = db.get(Product, comp_id)
        unit = avg_costs.get(comp_id, Decimal("0"))
        if unit <= 0 and prod and prod.reference_unit_cost:
            unit = Decimal(str(prod.reference_unit_cost))
        total = (qty * unit).quantize(Decimal("0.001"))
        out.append(
            PackagingCostRow(
                product_id=comp_id,
                name_ar=prod.name_ar if prod else f"#{comp_id}",
                qty_used=qty,
                unit_cost=unit,
                total_cost=total,
            )
        )
    out.sort(key=lambda r: r.total_cost, reverse=True)
    return out[:limit]


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
    conds = [_sale_in_period(s0, s1)]
    if filters.user_id is not None:
        conds.append(Sale.created_by_id == filters.user_id)
    if filters.customer_id is not None:
        conds.append(Sale.customer_id == filters.customer_id)
    if filters.payment_method_id is not None:
        pm_exists = (
            select(1)
            .select_from(SalePayment)
            .where(
                SalePayment.sale_id == Sale.id,
                SalePayment.payment_method_id == filters.payment_method_id,
            )
            .correlate(Sale)
        )
        conds.append(exists(pm_exists))
    return and_(*conds)


def financial_operational_statement(
    db: Session, start: datetime, end: datetime, filters: ReportFilters
) -> FinancialOperationalStatement:
    """ملخص مالي تشغيلي:
    - الإيراد: مجموع ``Sale.total`` لفواتير **مكتملة** ضمن الفترة (تاريخ الإتمام = آخر دفعة تحصيل أو إنشاء الفاتورة).
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
        .join(Sale, SaleReturn.original_sale_id == Sale.id)
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
    *,
    source: str = "pos",
) -> list[tuple[str, str, Decimal, str]]:
    """صفوف توضيحية لعرض «منطق القيد» المشتق من الأرقام أعلاه (ليست قيوداً مرحّلة في دفتر)."""
    if source == "hotel":
        return [
            (
                "إيراد الإقامة (تحصيلات نقدية)",
                "Credit",
                stmt.gross_revenue,
                "مصدر: hotel_booking_payments.amount",
            ),
            (
                "مرتجعات تحصيلات الحجز",
                "Debit",
                stmt.returns_total,
                "مصدر: hotel_booking_payment_refunds.amount",
            ),
            ("صافي إيراد التحصيل", "—", stmt.net_revenue, "تحصيل − مرتجعات"),
            (
                "تحصيلات طرق الدفع (فندق)",
                "Debit",
                stmt.collections_total,
                "نفس إجمالي التحصيل — أساس نقدي",
            ),
            (
                "ردود مبالغ التحصيل",
                "Credit",
                stmt.refunds_payment_total,
                "مصدر: hotel_booking_payment_refunds",
            ),
            (
                "صافي أثر نقدي تشغيلي",
                "—",
                stmt.net_cash_effect,
                "للمقارنة مع خزائن الفندق",
            ),
        ]
    if source == "combined":
        return [
            (
                "إيراد تشغيلي (POS استحقاق + فندق تحصيل)",
                "Credit",
                stmt.gross_revenue,
                "sales.total + hotel_booking_payments",
            ),
            (
                "مرتجعات (POS + فندق)",
                "Debit",
                stmt.returns_total,
                "sale_returns + hotel refunds",
            ),
            ("صافي الإيراد التشغيلي", "—", stmt.net_revenue, "gross − returns"),
            ("مجموع التحصيل", "Debit", stmt.collections_total, "POS + فندق"),
            ("مجموع الردود", "Credit", stmt.refunds_payment_total, "POS + فندق"),
            ("صافي أثر نقدي", "—", stmt.net_cash_effect, "تحصيل − ردود"),
        ]
    rows: list[tuple[str, str, Decimal, str]] = [
        ("إيراد مبيعات (استحقاق — فواتير مكتملة)", "Credit", stmt.gross_revenue, "مصدر: sales.total"),
        ("خصم مرتجعات بيع (سندات مرحّلة)", "Debit", stmt.returns_total, "مصدر: sale_returns.total"),
        ("صافي إيراد الاستحقاق", "—", stmt.net_revenue, "gross − returns"),
        ("تحصيلات طرق الدفع (مرتبطة بفواتير الفترة)", "Debit", stmt.collections_total, "مصدر: sale_payments.amount"),
        ("ردود مبالغ المرتجعات", "Credit", stmt.refunds_payment_total, "مصدر: refund_payments.amount"),
        ("صافي أثر نقدي تشغيلي (تحصيل − ردود)", "—", stmt.net_cash_effect, "للمقارنة مع المحافظ — ليس قيداً متوازناً حتى يُبنى GL"),
    ]
    return rows


def _merge_financial_operational_statements(
    *parts: FinancialOperationalStatement,
) -> FinancialOperationalStatement:
    total = FinancialOperationalStatement(
        invoice_count=0,
        return_count=0,
        gross_revenue=Decimal("0"),
        returns_total=Decimal("0"),
        net_revenue=Decimal("0"),
        collections_total=Decimal("0"),
        refunds_payment_total=Decimal("0"),
        net_cash_effect=Decimal("0"),
        taxes_included=Decimal("0"),
        line_discounts_included=Decimal("0"),
    )
    for p in parts:
        total.invoice_count += int(p.invoice_count or 0)
        total.return_count += int(p.return_count or 0)
        total.gross_revenue += Decimal(str(p.gross_revenue or 0))
        total.returns_total += Decimal(str(p.returns_total or 0))
        total.net_revenue += Decimal(str(p.net_revenue or 0))
        total.collections_total += Decimal(str(p.collections_total or 0))
        total.refunds_payment_total += Decimal(str(p.refunds_payment_total or 0))
        total.net_cash_effect += Decimal(str(p.net_cash_effect or 0))
    for field in (
        "gross_revenue",
        "returns_total",
        "net_revenue",
        "collections_total",
        "refunds_payment_total",
        "net_cash_effect",
    ):
        setattr(total, field, getattr(total, field).quantize(Decimal("0.001")))
    return total


def hotel_financial_operational_statement(
    db: Session, start: datetime, end: datetime, filters: ReportFilters
) -> FinancialOperationalStatement:
    """ملخص تشغيلي للفندق — أساس نقدي (تحصيلات الحجز − مرتجعات)."""
    from modules.hotel.booking_models import HotelBookingPayment, HotelBookingPaymentRefund

    s0, s1 = _range_utc(start, end)
    pay_conds = [
        HotelBookingPayment.created_at >= s0,
        HotelBookingPayment.created_at < s1,
    ]
    if filters.user_id is not None:
        pay_conds.append(HotelBookingPayment.received_by_id == filters.user_id)
    if filters.payment_method_id is not None:
        pay_conds.append(
            HotelBookingPayment.payment_method_id == filters.payment_method_id
        )

    pay_cnt, pay_total = db.execute(
        select(
            func.count(HotelBookingPayment.id),
            func.coalesce(func.sum(HotelBookingPayment.amount), 0),
        ).where(and_(*pay_conds))
    ).one()
    collections_total = Decimal(str(pay_total or 0)).quantize(Decimal("0.001"))

    ref_conds = [
        HotelBookingPaymentRefund.created_at >= s0,
        HotelBookingPaymentRefund.created_at < s1,
    ]
    if filters.user_id is not None or filters.payment_method_id is not None:
        ref_stmt = (
            select(
                func.count(HotelBookingPaymentRefund.id),
                func.coalesce(func.sum(HotelBookingPaymentRefund.amount), 0),
            )
            .join(
                HotelBookingPayment,
                HotelBookingPayment.id == HotelBookingPaymentRefund.payment_id,
            )
            .where(and_(*ref_conds))
        )
        if filters.user_id is not None:
            ref_stmt = ref_stmt.where(
                HotelBookingPayment.received_by_id == filters.user_id
            )
        if filters.payment_method_id is not None:
            ref_stmt = ref_stmt.where(
                HotelBookingPayment.payment_method_id == filters.payment_method_id
            )
    else:
        ref_stmt = select(
            func.count(HotelBookingPaymentRefund.id),
            func.coalesce(func.sum(HotelBookingPaymentRefund.amount), 0),
        ).where(and_(*ref_conds))

    ret_cnt, ret_total = db.execute(ref_stmt).one()
    returns_total = Decimal(str(ret_total or 0)).quantize(Decimal("0.001"))
    net_revenue = (collections_total - returns_total).quantize(Decimal("0.001"))

    return FinancialOperationalStatement(
        invoice_count=int(pay_cnt or 0),
        return_count=int(ret_cnt or 0),
        gross_revenue=collections_total,
        returns_total=returns_total,
        net_revenue=net_revenue,
        collections_total=collections_total,
        refunds_payment_total=returns_total,
        net_cash_effect=net_revenue,
        taxes_included=Decimal("0"),
        line_discounts_included=Decimal("0"),
    )


def financial_operational_statement_for_domain(
    db: Session,
    start: datetime,
    end: datetime,
    filters: ReportFilters,
    domain=None,
) -> tuple[FinancialOperationalStatement, str]:
    """return: (statement, source) where source is pos|hotel|combined."""
    from modules.platform.business_domain import BusinessDomain

    if domain == BusinessDomain.HOTEL:
        return hotel_financial_operational_statement(db, start, end, filters), "hotel"
    pos = financial_operational_statement(db, start, end, filters)
    if domain == BusinessDomain.RESTAURANT:
        return pos, "pos"
    hotel = hotel_financial_operational_statement(db, start, end, filters)
    if hotel.invoice_count == 0 and hotel.gross_revenue == 0 and pos.invoice_count > 0:
        return pos, "pos"
    if pos.invoice_count == 0 and pos.gross_revenue == 0 and hotel.invoice_count > 0:
        return hotel, "hotel"
    return _merge_financial_operational_statements(pos, hotel), "combined"

