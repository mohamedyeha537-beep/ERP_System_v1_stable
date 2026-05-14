from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from modules.delivery.models import DeliveryCashSettlement
from modules.inventory.models import StockMovementType
from modules.inventory.service import apply_movement
from modules.payments.models import (
    PaymentMethod,
    PaymentMethodKind,
    PaymentTransfer,
    Purchase,
    PurchaseKind,
    PurchaseLine,
    RefundPayment,
    SalePayment,
)
from modules.sales.models import Sale, SaleStatus


class PaymentsError(Exception):
    pass


def ensure_default_payment_methods(db: Session) -> None:
    """ينشئ أسلوب «كاش» إن لم يوجد أي أسلوب دفع."""
    existing = db.execute(select(PaymentMethod).limit(1)).scalar_one_or_none()
    if existing is not None:
        return
    db.add(
        PaymentMethod(
            name_ar="كاش", kind=PaymentMethodKind.CASH, is_active=True, sort_order=0
        )
    )
    db.commit()


def list_payment_methods(db: Session, only_active: bool = False) -> list[PaymentMethod]:
    stmt = select(PaymentMethod).order_by(PaymentMethod.sort_order, PaymentMethod.id)
    if only_active:
        stmt = stmt.where(PaymentMethod.is_active.is_(True))
    return list(db.scalars(stmt).all())


def create_payment_method(
    db: Session, name_ar: str, kind: PaymentMethodKind, sort_order: int = 0
) -> PaymentMethod:
    name = name_ar.strip()
    if not name:
        raise PaymentsError("الاسم مطلوب.")
    exists = db.execute(
        select(PaymentMethod).where(PaymentMethod.name_ar == name)
    ).scalar_one_or_none()
    if exists is not None:
        raise PaymentsError("اسم أسلوب الدفع موجود مسبقاً.")
    pm = PaymentMethod(
        name_ar=name, kind=kind, is_active=True, sort_order=sort_order
    )
    db.add(pm)
    db.flush()
    return pm


def update_payment_method(
    db: Session,
    pm_id: int,
    *,
    name_ar: str | None = None,
    kind: PaymentMethodKind | None = None,
    is_active: bool | None = None,
    sort_order: int | None = None,
) -> PaymentMethod:
    pm = db.get(PaymentMethod, pm_id)
    if pm is None:
        raise PaymentsError("أسلوب الدفع غير موجود.")
    if name_ar is not None:
        n = name_ar.strip()
        if not n:
            raise PaymentsError("الاسم مطلوب.")
        clash = db.execute(
            select(PaymentMethod).where(
                PaymentMethod.name_ar == n, PaymentMethod.id != pm_id
            )
        ).scalar_one_or_none()
        if clash is not None:
            raise PaymentsError("اسم أسلوب الدفع موجود مسبقاً.")
        pm.name_ar = n
    if kind is not None:
        pm.kind = kind
    if is_active is not None:
        pm.is_active = is_active
    if sort_order is not None:
        pm.sort_order = sort_order
    db.flush()
    return pm


def delete_payment_method(db: Session, pm_id: int) -> None:
    from modules.delivery.models import DeliveryCashSettlement

    pm = db.get(PaymentMethod, pm_id)
    if pm is None:
        return
    used_in_sale = db.execute(
        select(SalePayment.id).where(SalePayment.payment_method_id == pm_id).limit(1)
    ).scalar_one_or_none()
    used_in_purchase = db.execute(
        select(Purchase.id).where(Purchase.payment_method_id == pm_id).limit(1)
    ).scalar_one_or_none()
    used_in_delivery = db.execute(
        select(DeliveryCashSettlement.id)
        .where(DeliveryCashSettlement.cash_method_id == pm_id)
        .limit(1)
    ).scalar_one_or_none()
    if used_in_sale or used_in_purchase or used_in_delivery:
        raise PaymentsError(
            "لا يمكن حذف أسلوب الدفع لأنه مستخدم في مبيعات أو مشتريات أو تسويات توصيل. يمكنك تعطيله بدلاً من الحذف."
        )
    db.delete(pm)
    db.flush()


def record_sale_payment(
    db: Session, sale_id: int, payment_method_id: int, amount: Decimal
) -> SalePayment:
    """يسجّل دفعة لبيع (تستدعى مع complete_sale)."""
    pm = db.get(PaymentMethod, payment_method_id)
    if pm is None or not pm.is_active:
        raise PaymentsError("أسلوب الدفع غير صالح.")
    sp = SalePayment(
        sale_id=sale_id, payment_method_id=payment_method_id, amount=amount
    )
    db.add(sp)
    db.flush()
    return sp


def list_sale_payments(db: Session, sale_id: int) -> list[SalePayment]:
    return list(
        db.scalars(
            select(SalePayment)
            .where(SalePayment.sale_id == sale_id)
            .order_by(SalePayment.created_at, SalePayment.id)
        ).all()
    )


def sum_sale_payments(db: Session, sale_id: int) -> Decimal:
    total = db.execute(
        select(func.coalesce(func.sum(SalePayment.amount), 0)).where(
            SalePayment.sale_id == sale_id
        )
    ).scalar_one()
    return Decimal(str(total or 0)).quantize(Decimal("0.001"))


def record_refund_payment(
    db: Session,
    *,
    sale_return_id: int,
    payment_method_id: int,
    amount: Decimal,
    user_id: int | None,
    note: str | None = None,
) -> RefundPayment:
    pm = db.get(PaymentMethod, payment_method_id)
    if pm is None or not pm.is_active:
        raise PaymentsError("أسلوب الدفع غير صالح.")
    if amount <= 0:
        raise PaymentsError("مبلغ المرتجع يجب أن يكون أكبر من صفر.")
    rp = RefundPayment(
        sale_return_id=sale_return_id,
        payment_method_id=payment_method_id,
        amount=amount.quantize(Decimal("0.001")),
        note=(note or "").strip() or None,
        created_by_id=user_id,
    )
    db.add(rp)
    db.flush()
    return rp


def record_payment_transfer(
    db: Session,
    *,
    sale_return_id: int,
    from_payment_method_id: int,
    to_payment_method_id: int,
    amount: Decimal,
    user_id: int | None,
    note: str | None = None,
) -> PaymentTransfer:
    from_pm = db.get(PaymentMethod, from_payment_method_id)
    to_pm = db.get(PaymentMethod, to_payment_method_id)
    if from_pm is None or to_pm is None:
        raise PaymentsError("وسيلة التسوية غير صالحة.")
    if amount <= 0:
        raise PaymentsError("مبلغ التسوية يجب أن يكون أكبر من صفر.")
    tf = PaymentTransfer(
        sale_return_id=sale_return_id,
        from_payment_method_id=from_payment_method_id,
        to_payment_method_id=to_payment_method_id,
        amount=amount.quantize(Decimal("0.001")),
        note=(note or "").strip() or None,
        created_by_id=user_id,
    )
    db.add(tf)
    db.flush()
    return tf


def record_expense(
    db: Session,
    *,
    payment_method_id: int,
    amount: Decimal,
    expense_category: str | None,
    supplier: str | None,
    note: str | None,
    user_id: int | None,
    created_at: datetime | None = None,
) -> Purchase:
    """مصروف عام (إيجار/راتب/فاتورة) — لا يضاف للمخزون."""
    pm = db.get(PaymentMethod, payment_method_id)
    if pm is None or not pm.is_active:
        raise PaymentsError("أسلوب الدفع غير صالح.")
    if amount <= 0:
        raise PaymentsError("المبلغ يجب أن يكون أكبر من صفر.")
    p = Purchase(
        payment_method_id=payment_method_id,
        kind=PurchaseKind.EXPENSE,
        amount=amount,
        expense_category=(expense_category or "").strip() or None,
        supplier=(supplier or "").strip() or None,
        note=(note or "").strip() or None,
        created_by_id=user_id,
    )
    if created_at is not None:
        p.created_at = created_at
    db.add(p)
    db.flush()
    return p


def record_asset_purchase(
    db: Session,
    *,
    payment_method_id: int,
    supplier: str | None,
    note: str | None,
    lines: list[tuple[str, str | None, Decimal, Decimal, int, Decimal]],
    user_id: int | None,
    created_at: datetime | None = None,
) -> Purchase:
    """فاتورة أصول/أدوات للشركة (لا تباع، لا تأثير على المخزون).
    lines = [(item_name, unit, quantity, unit_cost, useful_life_months, salvage_value), ...]
    - useful_life_months = 0 → بند استهلاكي (مصروف فوري في شهر الشراء).
    - useful_life_months > 0 → أصل ثابت يُهلَك على فترة العمر الإنتاجي (القسط الثابت).
    """
    pm = db.get(PaymentMethod, payment_method_id)
    if pm is None or not pm.is_active:
        raise PaymentsError("أسلوب الدفع غير صالح.")
    if not lines:
        raise PaymentsError("أضف بنداً واحداً على الأقل.")

    total = Decimal("0")
    cleaned: list[tuple[str, str | None, Decimal, Decimal, int, Decimal]] = []
    for name, unit, qty, cost, life, salvage in lines:
        nm = (name or "").strip()
        if not nm:
            raise PaymentsError("اكتب اسم الأداة/الأصل لكل بند.")
        if qty <= 0:
            raise PaymentsError("الكمية يجب أن تكون أكبر من صفر.")
        if cost < 0:
            raise PaymentsError("سعر الوحدة لا يمكن أن يكون سالباً.")
        if life < 0:
            raise PaymentsError("العمر الإنتاجي لا يمكن أن يكون سالباً.")
        if salvage < 0:
            raise PaymentsError("قيمة الخردة لا يمكن أن تكون سالبة.")
        line_total = (qty * cost).quantize(Decimal("0.001"))
        if salvage > line_total:
            raise PaymentsError(
                f"قيمة الخردة لـ «{nm}» ({salvage}) لا يمكن أن تتجاوز إجمالي البند ({line_total})."
            )
        u = (unit or "").strip() or None
        total += line_total
        cleaned.append((nm, u, qty, cost, int(life), salvage))
    total = total.quantize(Decimal("0.001"))
    if total <= 0:
        raise PaymentsError("إجمالي الفاتورة يجب أن يكون أكبر من صفر.")

    p = Purchase(
        payment_method_id=payment_method_id,
        kind=PurchaseKind.ASSET,
        amount=total,
        supplier=(supplier or "").strip() or None,
        note=(note or "").strip() or None,
        created_by_id=user_id,
    )
    if created_at is not None:
        p.created_at = created_at
    db.add(p)
    db.flush()

    for name, unit, qty, cost, life, salvage in cleaned:
        pl = PurchaseLine(
            purchase_id=p.id,
            product_id=None,
            item_name=name,
            unit=unit,
            quantity=qty.quantize(Decimal("0.0001")),
            unit_cost=cost.quantize(Decimal("0.001")),
            line_total=(qty * cost).quantize(Decimal("0.001")),
            useful_life_months=life,
            salvage_value=salvage.quantize(Decimal("0.001")),
        )
        db.add(pl)
    db.flush()
    return p


def record_inventory_purchase(
    db: Session,
    *,
    payment_method_id: int,
    supplier: str | None,
    note: str | None,
    lines: list[tuple[int, Decimal, Decimal]],
    user_id: int | None,
    created_at: datetime | None = None,
) -> Purchase:
    """فاتورة شراء بضاعة: lines = [(product_id, quantity, unit_cost), ...].
    تُضاف الكميات إلى رصيد المخزون عبر apply_movement (PURCHASE)،
    والإجمالي يخصم من المحفظة.
    - يُسمح فقط بمكوّنات المخزون (STOCK_ONLY)؛ المنتجات النهائية لا تُشترى لأنها
      تُصنَّع داخلياً من مكوّناتها.
    """
    from modules.catalog.models import Product, ProductKind

    pm = db.get(PaymentMethod, payment_method_id)
    if pm is None or not pm.is_active:
        raise PaymentsError("أسلوب الدفع غير صالح.")
    if not lines:
        raise PaymentsError("أضف بنداً واحداً على الأقل لفاتورة الشراء.")

    pids = [pid for pid, _q, _c in lines]
    bad_products = list(
        db.scalars(
            select(Product).where(Product.id.in_(pids), Product.kind != ProductKind.STOCK_ONLY)
        ).all()
    )
    if bad_products:
        names = [bp.name_ar for bp in bad_products]
        raise PaymentsError(
            "لا يمكن شراء منتجات نهائية تُباع كما هي: "
            + "، ".join(names)
            + ". اشترِ مكوّنات المخزون التي تُصنَّع منها."
        )

    total = Decimal("0")
    for _pid, qty, cost in lines:
        if qty <= 0:
            raise PaymentsError("الكمية يجب أن تكون أكبر من صفر.")
        if cost < 0:
            raise PaymentsError("سعر الوحدة لا يمكن أن يكون سالباً.")
        total += (qty * cost)
    total = total.quantize(Decimal("0.001"))
    if total <= 0:
        raise PaymentsError("إجمالي الفاتورة يجب أن يكون أكبر من صفر.")

    p = Purchase(
        payment_method_id=payment_method_id,
        kind=PurchaseKind.INVENTORY,
        amount=total,
        supplier=(supplier or "").strip() or None,
        note=(note or "").strip() or None,
        created_by_id=user_id,
    )
    if created_at is not None:
        p.created_at = created_at
    db.add(p)
    db.flush()

    for pid, qty, cost in lines:
        line_total = (qty * cost).quantize(Decimal("0.001"))
        pl = PurchaseLine(
            purchase_id=p.id,
            product_id=pid,
            quantity=qty.quantize(Decimal("0.0001")),
            unit_cost=cost.quantize(Decimal("0.001")),
            line_total=line_total,
        )
        db.add(pl)
        # إضافة الكمية إلى المخزون
        apply_movement(
            db,
            product_id=pid,
            quantity_delta=qty,
            movement_type=StockMovementType.PURCHASE,
            user_id=user_id,
            note=f"شراء فاتورة #{p.id}",
        )
    db.flush()
    return p


def delete_purchase(db: Session, purchase_id: int) -> None:
    """يحذف عملية صرف. لو كانت INVENTORY: نخصم الكميات من المخزون مرة أخرى."""
    p = db.get(Purchase, purchase_id)
    if p is None:
        return
    if p.kind == PurchaseKind.INVENTORY:
        for ln in p.lines:
            apply_movement(
                db,
                product_id=ln.product_id,
                quantity_delta=-ln.quantity,
                movement_type=StockMovementType.ADJUSTMENT,
                user_id=None,
                note=f"إلغاء شراء فاتورة #{p.id}",
            )
    db.delete(p)
    db.flush()


def get_sale_payment(db: Session, sale_id: int) -> SalePayment | None:
    return db.execute(
        select(SalePayment)
        .where(SalePayment.sale_id == sale_id)
        .order_by(SalePayment.created_at, SalePayment.id)
    ).scalars().first()


# --- Wallet calculations ---


@dataclass
class WalletRow:
    method: PaymentMethod
    sales_in: Decimal
    refunds_out: Decimal
    transfers_in: Decimal
    transfers_out: Decimal
    delivery_fees_out: Decimal
    purchases_out: Decimal
    net: Decimal


def wallet_breakdown(
    db: Session, start: datetime | None, end: datetime | None
) -> list[WalletRow]:
    """يحسب لكل أسلوب دفع: مدخل المبيعات، المرتجعات، التسويات، خرج المشتريات، الصافي خلال الفترة.
    إن كانت start/end None: حسبة كل الوقت."""
    methods = list_payment_methods(db, only_active=False)

    sales_q = (
        select(SalePayment.payment_method_id, func.coalesce(func.sum(SalePayment.amount), 0))
        .group_by(SalePayment.payment_method_id)
    )
    if start is not None:
        sales_q = sales_q.where(SalePayment.created_at >= start)
    if end is not None:
        sales_q = sales_q.where(SalePayment.created_at < end)
    sales_map: dict[int, Decimal] = {
        int(mid): Decimal(str(s or 0)) for mid, s in db.execute(sales_q).all()
    }

    refunds_q = (
        select(
            RefundPayment.payment_method_id,
            func.coalesce(func.sum(RefundPayment.amount), 0),
        )
        .group_by(RefundPayment.payment_method_id)
    )
    if start is not None:
        refunds_q = refunds_q.where(RefundPayment.created_at >= start)
    if end is not None:
        refunds_q = refunds_q.where(RefundPayment.created_at < end)
    refunds_map: dict[int, Decimal] = {
        int(mid): Decimal(str(s or 0)) for mid, s in db.execute(refunds_q).all()
    }

    transfers_in_q = (
        select(
            PaymentTransfer.to_payment_method_id,
            func.coalesce(func.sum(PaymentTransfer.amount), 0),
        )
        .group_by(PaymentTransfer.to_payment_method_id)
    )
    if start is not None:
        transfers_in_q = transfers_in_q.where(PaymentTransfer.created_at >= start)
    if end is not None:
        transfers_in_q = transfers_in_q.where(PaymentTransfer.created_at < end)
    transfers_in_map: dict[int, Decimal] = {
        int(mid): Decimal(str(s or 0))
        for mid, s in db.execute(transfers_in_q).all()
    }

    transfers_out_q = (
        select(
            PaymentTransfer.from_payment_method_id,
            func.coalesce(func.sum(PaymentTransfer.amount), 0),
        )
        .group_by(PaymentTransfer.from_payment_method_id)
    )
    if start is not None:
        transfers_out_q = transfers_out_q.where(PaymentTransfer.created_at >= start)
    if end is not None:
        transfers_out_q = transfers_out_q.where(PaymentTransfer.created_at < end)
    transfers_out_map: dict[int, Decimal] = {
        int(mid): Decimal(str(s or 0))
        for mid, s in db.execute(transfers_out_q).all()
    }

    delivery_fees_q = (
        select(
            DeliveryCashSettlement.cash_method_id,
            func.coalesce(func.sum(DeliveryCashSettlement.amount), 0),
        )
        .group_by(DeliveryCashSettlement.cash_method_id)
    )
    if start is not None:
        delivery_fees_q = delivery_fees_q.where(DeliveryCashSettlement.created_at >= start)
    if end is not None:
        delivery_fees_q = delivery_fees_q.where(DeliveryCashSettlement.created_at < end)
    delivery_fees_map: dict[int, Decimal] = {
        int(mid): Decimal(str(s or 0)) for mid, s in db.execute(delivery_fees_q).all()
    }

    purchases_q = select(
        Purchase.payment_method_id, func.coalesce(func.sum(Purchase.amount), 0)
    ).group_by(Purchase.payment_method_id)
    if start is not None:
        purchases_q = purchases_q.where(Purchase.created_at >= start)
    if end is not None:
        purchases_q = purchases_q.where(Purchase.created_at < end)
    purchases_map: dict[int, Decimal] = {
        int(mid): Decimal(str(s or 0)) for mid, s in db.execute(purchases_q).all()
    }

    rows: list[WalletRow] = []
    for m in methods:
        sin = sales_map.get(m.id, Decimal("0"))
        rout = refunds_map.get(m.id, Decimal("0"))
        tin = transfers_in_map.get(m.id, Decimal("0"))
        tout = transfers_out_map.get(m.id, Decimal("0"))
        dfout = delivery_fees_map.get(m.id, Decimal("0"))
        pout = purchases_map.get(m.id, Decimal("0"))
        rows.append(
            WalletRow(
                method=m,
                sales_in=sin,
                refunds_out=rout,
                transfers_in=tin,
                transfers_out=tout,
                delivery_fees_out=dfout,
                purchases_out=pout,
                net=(sin + tin - rout - tout - dfout - pout).quantize(Decimal("0.001")),
            )
        )
    return rows


@dataclass
class PurchasesSummary:
    count: int
    total: Decimal


def _purchases_summary_q(
    db: Session, start: datetime, end: datetime, kind: PurchaseKind | None
) -> PurchasesSummary:
    stmt = select(
        func.count(Purchase.id), func.coalesce(func.sum(Purchase.amount), 0)
    ).where(Purchase.created_at >= start, Purchase.created_at < end)
    if kind is not None:
        stmt = stmt.where(Purchase.kind == kind)
    cnt, total = db.execute(stmt).one()
    return PurchasesSummary(count=int(cnt or 0), total=Decimal(str(total or 0)))


def purchases_summary(
    db: Session, start: datetime, end: datetime
) -> PurchasesSummary:
    """ملخص جميع عمليات الصرف (شراء + مصروف)."""
    return _purchases_summary_q(db, start, end, None)


def inventory_purchases_summary(
    db: Session, start: datetime, end: datetime
) -> PurchasesSummary:
    return _purchases_summary_q(db, start, end, PurchaseKind.INVENTORY)


def expenses_summary(
    db: Session, start: datetime, end: datetime
) -> PurchasesSummary:
    return _purchases_summary_q(db, start, end, PurchaseKind.EXPENSE)


def list_purchases(
    db: Session,
    start: datetime,
    end: datetime,
    kind: PurchaseKind | None = None,
    limit: int = 500,
) -> list[Purchase]:
    stmt = (
        select(Purchase)
        .where(Purchase.created_at >= start, Purchase.created_at < end)
        .order_by(Purchase.created_at.desc(), Purchase.id.desc())
        .limit(limit)
    )
    if kind is not None:
        stmt = stmt.where(Purchase.kind == kind)
    return list(db.scalars(stmt).all())
