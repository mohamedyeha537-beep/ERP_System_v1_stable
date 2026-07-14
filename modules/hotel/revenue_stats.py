"""إيرادات الفندق للتقارير المالية وتحليل التعادل (تحصيلات نقدية صافية)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from modules.hotel.booking_models import (
    HotelBooking,
    HotelBookingPayment,
    HotelBookingPaymentRefund,
)
from modules.platform.business_domain import BusinessDomain


@dataclass
class HotelCollectionRow:
    movement_type: str
    movement_id: int
    booking_id: int
    booking_ref: str
    guest_name: str
    created_at: datetime
    amount: Decimal
    payment_method: str
    is_deposit: bool
    received_by: str
    note: str


@dataclass
class HotelCollectionsSummary:
    payment_count: int
    refund_count: int
    collected_total: Decimal
    refunded_total: Decimal
    net_total: Decimal
    deposit_count: int
    deposit_total: Decimal

def hotel_cash_collected(db: Session, start: datetime, end: datetime) -> Decimal:
    """صافي التحصيلات: مدفوعات الحجز − المرتجعات (حسب تاريخ كل حركة)."""
    paid = db.scalar(
        select(func.coalesce(func.sum(HotelBookingPayment.amount), 0)).where(
            HotelBookingPayment.created_at >= start,
            HotelBookingPayment.created_at < end,
        )
    )
    refunded = db.scalar(
        select(func.coalesce(func.sum(HotelBookingPaymentRefund.amount), 0)).where(
            HotelBookingPaymentRefund.created_at >= start,
            HotelBookingPaymentRefund.created_at < end,
        )
    )
    return (Decimal(str(paid or 0)) - Decimal(str(refunded or 0))).quantize(Decimal("0.001"))


def hotel_variable_cost(db: Session, start: datetime, end: datetime) -> Decimal:
    """تكلفة متغيّرة تقريبية للفندق: مصروفات + مشتريات مخزون مجال الفندق."""
    from modules.reporting.queries import expenses_summary, inventory_purchases_summary

    exp = expenses_summary(db, start, end, domain=BusinessDomain.HOTEL).total
    inv = inventory_purchases_summary(db, start, end, domain=BusinessDomain.HOTEL).total
    return (exp + inv).quantize(Decimal("0.001"))


def hotel_collections_by_payment_method(
    db: Session, start: datetime, end: datetime
) -> list[tuple[str, int, Decimal, int, Decimal, Decimal]]:
    """return: (name, pay_cnt, pay_total, refund_cnt, refund_total, net)"""
    from modules.payments.models import PaymentMethod

    paid_rows = db.execute(
        select(
            PaymentMethod.name_ar,
            func.count(HotelBookingPayment.id),
            func.coalesce(func.sum(HotelBookingPayment.amount), 0),
        )
        .join(PaymentMethod, PaymentMethod.id == HotelBookingPayment.payment_method_id)
        .where(
            HotelBookingPayment.created_at >= start,
            HotelBookingPayment.created_at < end,
        )
        .group_by(PaymentMethod.id, PaymentMethod.name_ar)
        .order_by(PaymentMethod.sort_order, PaymentMethod.id)
    ).all()
    paid_map = {
        str(name): (int(cnt or 0), Decimal(str(total or 0)).quantize(Decimal("0.001")))
        for name, cnt, total in paid_rows
    }

    refund_rows = db.execute(
        select(
            PaymentMethod.name_ar,
            func.count(HotelBookingPaymentRefund.id),
            func.coalesce(func.sum(HotelBookingPaymentRefund.amount), 0),
        )
        .join(
            HotelBookingPayment,
            HotelBookingPayment.id == HotelBookingPaymentRefund.payment_id,
        )
        .join(PaymentMethod, PaymentMethod.id == HotelBookingPayment.payment_method_id)
        .where(
            HotelBookingPaymentRefund.created_at >= start,
            HotelBookingPaymentRefund.created_at < end,
        )
        .group_by(PaymentMethod.id, PaymentMethod.name_ar)
        .order_by(PaymentMethod.sort_order, PaymentMethod.id)
    ).all()
    refund_map = {
        str(name): (int(cnt or 0), Decimal(str(total or 0)).quantize(Decimal("0.001")))
        for name, cnt, total in refund_rows
    }

    names = list(dict.fromkeys([*paid_map.keys(), *refund_map.keys()]))
    out: list[tuple[str, int, Decimal, int, Decimal, Decimal]] = []
    for name in names:
        pc, pt = paid_map.get(name, (0, Decimal("0")))
        rc, rt = refund_map.get(name, (0, Decimal("0")))
        net = (pt - rt).quantize(Decimal("0.001"))
        out.append((name, pc, pt, rc, rt, net))
    return out


def hotel_collections_detail(
    db: Session, start: datetime, end: datetime
) -> tuple[list[HotelCollectionRow], HotelCollectionsSummary]:
    """سجل تحصيلات ومرتجعات الحجز للفترة."""
    from modules.authz.models import User
    from modules.payments.models import PaymentMethod

    pay_rows = db.execute(
        select(
            HotelBookingPayment.id,
            HotelBookingPayment.booking_id,
            HotelBooking.reference,
            HotelBooking.guest_name,
            HotelBookingPayment.created_at,
            HotelBookingPayment.amount,
            PaymentMethod.name_ar,
            HotelBookingPayment.is_deposit,
            User.username,
            HotelBookingPayment.note,
        )
        .join(HotelBooking, HotelBooking.id == HotelBookingPayment.booking_id)
        .outerjoin(PaymentMethod, PaymentMethod.id == HotelBookingPayment.payment_method_id)
        .outerjoin(User, User.id == HotelBookingPayment.received_by_id)
        .where(
            HotelBookingPayment.created_at >= start,
            HotelBookingPayment.created_at < end,
        )
        .order_by(HotelBookingPayment.id.desc())
    ).all()

    refund_rows = db.execute(
        select(
            HotelBookingPaymentRefund.id,
            HotelBookingPayment.booking_id,
            HotelBooking.reference,
            HotelBooking.guest_name,
            HotelBookingPaymentRefund.created_at,
            HotelBookingPaymentRefund.amount,
            PaymentMethod.name_ar,
            HotelBookingPayment.is_deposit,
            User.username,
            HotelBookingPaymentRefund.reason,
        )
        .join(
            HotelBookingPayment,
            HotelBookingPayment.id == HotelBookingPaymentRefund.payment_id,
        )
        .join(HotelBooking, HotelBooking.id == HotelBookingPayment.booking_id)
        .outerjoin(PaymentMethod, PaymentMethod.id == HotelBookingPayment.payment_method_id)
        .outerjoin(User, User.id == HotelBookingPaymentRefund.approved_by_id)
        .where(
            HotelBookingPaymentRefund.created_at >= start,
            HotelBookingPaymentRefund.created_at < end,
        )
        .order_by(
            HotelBookingPaymentRefund.created_at.desc(),
            HotelBookingPaymentRefund.id.desc(),
        )
    ).all()

    rows: list[HotelCollectionRow] = []
    collected = Decimal("0")
    refunded = Decimal("0")
    deposit_count = 0
    deposit_total = Decimal("0")

    for (
        pid,
        bid,
        ref,
        guest,
        ca,
        amt,
        pm_name,
        is_dep,
        user_name,
        note,
    ) in pay_rows:
        amount = Decimal(str(amt or 0)).quantize(Decimal("0.001"))
        collected += amount
        if is_dep:
            deposit_count += 1
            deposit_total += amount
        rows.append(
            HotelCollectionRow(
                movement_type="تحصيل",
                movement_id=int(pid),
                booking_id=int(bid),
                booking_ref=str(ref or f"#{bid}"),
                guest_name=str(guest or "—"),
                created_at=ca,
                amount=amount,
                payment_method=str(pm_name or "—"),
                is_deposit=bool(is_dep),
                received_by=str(user_name or "—"),
                note=str(note or ""),
            )
        )

    for (
        rid,
        bid,
        ref,
        guest,
        ca,
        amt,
        pm_name,
        is_dep,
        user_name,
        reason,
    ) in refund_rows:
        amount = Decimal(str(amt or 0)).quantize(Decimal("0.001"))
        refunded += amount
        rows.append(
            HotelCollectionRow(
                movement_type="مرتجع",
                movement_id=int(rid),
                booking_id=int(bid),
                booking_ref=str(ref or f"#{bid}"),
                guest_name=str(guest or "—"),
                created_at=ca,
                amount=amount,
                payment_method=str(pm_name or "—"),
                is_deposit=bool(is_dep),
                received_by=str(user_name or "—"),
                note=str(reason or ""),
            )
        )

    rows.sort(key=lambda r: r.created_at, reverse=True)
    summary = HotelCollectionsSummary(
        payment_count=len(pay_rows),
        refund_count=len(refund_rows),
        collected_total=collected.quantize(Decimal("0.001")),
        refunded_total=refunded.quantize(Decimal("0.001")),
        net_total=(collected - refunded).quantize(Decimal("0.001")),
        deposit_count=deposit_count,
        deposit_total=deposit_total.quantize(Decimal("0.001")),
    )
    return rows, summary


def hotel_day_financials(
    db: Session, day_start: datetime, day_end: datetime
) -> tuple[Decimal, Decimal, Decimal]:
    """return: (revenue, variable_cost, gross_profit)"""
    rev = hotel_cash_collected(db, day_start, day_end)
    var = hotel_variable_cost(db, day_start, day_end)
    return rev, var, (rev - var).quantize(Decimal("0.001"))


def _merge_payment_method_rows(
    *row_lists: list[tuple[str, int, Decimal, int, Decimal, Decimal]],
) -> list[tuple[str, int, Decimal, int, Decimal, Decimal]]:
    """دمج صفوف التحصيل/الرد حسب اسم طريقة الدفع."""
    buckets: dict[str, list] = {}
    for rows in row_lists:
        for name, sc, st, rc, rt, _net in rows:
            if name not in buckets:
                buckets[name] = [0, Decimal("0"), 0, Decimal("0")]
            buckets[name][0] += int(sc or 0)
            buckets[name][1] += Decimal(str(st or 0))
            buckets[name][2] += int(rc or 0)
            buckets[name][3] += Decimal(str(rt or 0))
    out: list[tuple[str, int, Decimal, int, Decimal, Decimal]] = []
    for name, (sc, st, rc, rt) in buckets.items():
        st = st.quantize(Decimal("0.001"))
        rt = rt.quantize(Decimal("0.001"))
        out.append((name, sc, st, rc, rt, (st - rt).quantize(Decimal("0.001"))))
    return out

