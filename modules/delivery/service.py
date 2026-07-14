from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from modules.delivery.models import DeliveryCashSettlement, DeliveryZone
from modules.payments.models import PaymentMethod, PaymentMethodKind


class DeliveryError(Exception):
    pass


@dataclass
class DeliveryOrderRow:
    sale_id: int
    created_at: datetime
    customer_name: str
    customer_phone: str
    zone_name: str
    order_total: Decimal
    delivery_fee: Decimal
    customer_total: Decimal
    payment_method_name: str


def list_zones(db: Session, *, only_active: bool = False) -> list[DeliveryZone]:
    stmt = select(DeliveryZone).order_by(DeliveryZone.sort_order, DeliveryZone.id)
    if only_active:
        stmt = stmt.where(DeliveryZone.is_active.is_(True))
    return list(db.scalars(stmt).all())


def get_zone(db: Session, zone_id: int) -> DeliveryZone | None:
    return db.get(DeliveryZone, zone_id)


def create_zone(
    db: Session,
    *,
    name_ar: str,
    fee: Decimal,
    sort_order: int = 0,
    notes: str | None = None,
) -> DeliveryZone:
    name = (name_ar or "").strip()
    if not name:
        raise DeliveryError("اسم المنطقة مطلوب.")
    if fee < 0:
        raise DeliveryError("سعر التوصيل لا يمكن أن يكون سالباً.")
    existing = db.execute(
        select(DeliveryZone).where(DeliveryZone.name_ar == name)
    ).scalar_one_or_none()
    if existing is not None:
        raise DeliveryError("اسم المنطقة موجود مسبقاً.")
    zone = DeliveryZone(
        name_ar=name,
        fee=fee.quantize(Decimal("0.001")),
        sort_order=int(sort_order or 0),
        notes=(notes or "").strip() or None,
        is_active=True,
    )
    db.add(zone)
    db.flush()
    return zone


def update_zone(
    db: Session,
    zone_id: int,
    *,
    name_ar: str,
    fee: Decimal,
    sort_order: int = 0,
    notes: str | None = None,
    is_active: bool = True,
) -> DeliveryZone:
    zone = db.get(DeliveryZone, zone_id)
    if zone is None:
        raise DeliveryError("المنطقة غير موجودة.")
    name = (name_ar or "").strip()
    if not name:
        raise DeliveryError("اسم المنطقة مطلوب.")
    if fee < 0:
        raise DeliveryError("سعر التوصيل لا يمكن أن يكون سالباً.")
    clash = db.execute(
        select(DeliveryZone).where(
            DeliveryZone.name_ar == name,
            DeliveryZone.id != zone_id,
        )
    ).scalar_one_or_none()
    if clash is not None:
        raise DeliveryError("اسم المنطقة موجود مسبقاً.")
    zone.name_ar = name
    zone.fee = fee.quantize(Decimal("0.001"))
    zone.sort_order = int(sort_order or 0)
    zone.notes = (notes or "").strip() or None
    zone.is_active = bool(is_active)
    db.flush()
    return zone


def delete_zone(db: Session, zone_id: int) -> None:
    zone = db.get(DeliveryZone, zone_id)
    if zone is None:
        return
    used = db.execute(
        select(DeliveryCashSettlement.id).where(DeliveryCashSettlement.zone_id == zone_id).limit(1)
    ).scalar_one_or_none()
    if used is not None:
        raise DeliveryError("لا يمكن حذف منطقة مستخدمة في طلبات سابقة. عطّلها بدلاً من ذلك.")
    db.delete(zone)
    db.flush()


def get_default_cash_method(db: Session) -> PaymentMethod | None:
    return db.execute(
        select(PaymentMethod)
        .where(
            PaymentMethod.kind == PaymentMethodKind.CASH,
            PaymentMethod.is_active.is_(True),
        )
        .order_by(PaymentMethod.sort_order, PaymentMethod.id)
    ).scalars().first()


def record_delivery_cash_settlement(
    db: Session,
    *,
    sale_id: int,
    amount: Decimal,
    zone_id: int | None,
    user_id: int | None,
    note: str | None = None,
) -> DeliveryCashSettlement:
    if amount <= 0:
        raise DeliveryError("أجرة التوصيل يجب أن تكون أكبر من صفر.")
    cash_method = get_default_cash_method(db)
    if cash_method is None:
        raise DeliveryError("لا توجد محفظة كاش مفعّلة لخصم أجرة التوصيل منها.")
    row = DeliveryCashSettlement(
        sale_id=sale_id,
        cash_method_id=cash_method.id,
        zone_id=zone_id,
        amount=amount.quantize(Decimal("0.001")),
        note=(note or "").strip() or None,
        created_by_id=user_id,
    )
    db.add(row)
    db.flush()
    return row


def get_sale_delivery_cash_settlement(
    db: Session, sale_id: int
) -> DeliveryCashSettlement | None:
    return db.execute(
        select(DeliveryCashSettlement).where(DeliveryCashSettlement.sale_id == sale_id)
    ).scalar_one_or_none()


def delivery_fee_cash_out_total(
    db: Session,
    *,
    start: datetime,
    end: datetime,
) -> Decimal:
    total = db.execute(
        select(func.coalesce(func.sum(DeliveryCashSettlement.amount), 0)).where(
            DeliveryCashSettlement.created_at >= start,
            DeliveryCashSettlement.created_at < end,
        )
    ).scalar_one()
    return Decimal(str(total or 0)).quantize(Decimal("0.001"))


def delivery_orders_report(
    db: Session,
    *,
    start: datetime,
    end: datetime,
) -> list[DeliveryOrderRow]:
    from modules.customers.models import Customer
    from modules.payments.models import PaymentMethod, SalePayment
    from modules.sales.models import ExternalOrderType, Sale, SaleContext

    rows = db.execute(
        select(
            Sale.id,
            Sale.created_at,
            func.coalesce(Customer.name, ""),
            func.coalesce(Customer.phone, ""),
            func.coalesce(Sale.delivery_zone_name, ""),
            Sale.total,
            Sale.delivery_fee,
            func.coalesce(PaymentMethod.name_ar, ""),
        )
        .join(Customer, Customer.id == Sale.customer_id, isouter=True)
        .join(SalePayment, SalePayment.sale_id == Sale.id, isouter=True)
        .join(PaymentMethod, PaymentMethod.id == SalePayment.payment_method_id, isouter=True)
        .where(
            Sale.context_type == SaleContext.EXTERNAL,
            Sale.external_order_type == ExternalOrderType.DELIVERY,
            Sale.created_at >= start,
            Sale.created_at < end,
        )
        .order_by(Sale.id.desc())
    ).all()
    out: list[DeliveryOrderRow] = []
    for sale_id, created_at, customer_name, customer_phone, zone_name, order_total, delivery_fee, payment_method_name in rows:
        order_total_dec = Decimal(str(order_total or 0)).quantize(Decimal("0.001"))
        fee_dec = Decimal(str(delivery_fee or 0)).quantize(Decimal("0.001"))
        out.append(
            DeliveryOrderRow(
                sale_id=int(sale_id),
                created_at=created_at,
                customer_name=str(customer_name or ""),
                customer_phone=str(customer_phone or ""),
                zone_name=str(zone_name or ""),
                order_total=order_total_dec,
                delivery_fee=fee_dec,
                customer_total=(order_total_dec + fee_dec).quantize(Decimal("0.001")),
                payment_method_name=str(payment_method_name or ""),
            )
        )
    return out
