"""ذمم مدينة — فواتير غير مسدّدة أو مسدّدة جزئياً."""
from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from modules.customers.models import Customer
from modules.payments.models import SalePayment
from modules.refunds.models import SaleReturn, SaleReturnStatus
from modules.sales.models import Sale, SaleContext, SaleStatus


class InvoicePayStatus(str, enum.Enum):
    PAID = "paid"
    PARTIAL = "partial"
    UNPAID = "unpaid"


@dataclass
class ReceivableInvoiceRow:
    sale_id: int
    created_at: datetime | None
    context_type: SaleContext
    party_label: str
    customer_id: int | None
    net_total: Decimal
    paid_total: Decimal
    outstanding: Decimal
    pay_status: InvoicePayStatus
    has_open_room_charge: bool


@dataclass
class CustomerDebtRow:
    customer_id: int | None
    party_label: str
    invoice_count: int
    total_outstanding: Decimal


@dataclass
class ReceivablesSummary:
    total_outstanding: Decimal
    invoice_count_with_balance: int
    unpaid_count: int
    partial_count: int
    paid_count: int


def _classify_pay_status(
    net: Decimal, paid: Decimal, outstanding: Decimal
) -> InvoicePayStatus:
    if net <= 0 or outstanding <= 0:
        return InvoicePayStatus.PAID
    if paid <= 0:
        return InvoicePayStatus.UNPAID
    return InvoicePayStatus.PARTIAL


def _party_label(
    sale: Sale, *, has_room_charge: bool, customer: Customer | None = None
) -> str:
    if sale.context_type == SaleContext.EXTERNAL and sale.customer_id:
        c = customer
        if c:
            name = (c.name or "").strip() or "عميل"
            phone = (c.phone or "").strip()
            return f"{name}" + (f" — {phone}" if phone else "")
    if sale.context_type == SaleContext.ROOM or has_room_charge:
        return "غرفة فندق (حساب مؤجل)"
    if sale.table_id and sale.table:
        return f"طاولة: {sale.table.name_ar}"
    if sale.context_type == SaleContext.EXTERNAL:
        return "طلب خارجي"
    return "طاولة / محلي"


def _load_completed_sales_with_amounts(db: Session) -> list[tuple[Sale, Decimal, Decimal]]:
    paid_sq = (
        select(
            SalePayment.sale_id.label("sale_id"),
            func.coalesce(func.sum(SalePayment.amount), 0).label("paid"),
        )
        .group_by(SalePayment.sale_id)
        .subquery()
    )
    ret_sq = (
        select(
            SaleReturn.original_sale_id.label("sale_id"),
            func.coalesce(func.sum(SaleReturn.total), 0).label("returned"),
        )
        .where(SaleReturn.status == SaleReturnStatus.POSTED)
        .group_by(SaleReturn.original_sale_id)
        .subquery()
    )
    rows = db.execute(
        select(
            Sale,
            func.coalesce(ret_sq.c.returned, 0),
            func.coalesce(paid_sq.c.paid, 0),
        )
        .outerjoin(ret_sq, ret_sq.c.sale_id == Sale.id)
        .outerjoin(paid_sq, paid_sq.c.sale_id == Sale.id)
        .where(Sale.status == SaleStatus.COMPLETED)
        .options(selectinload(Sale.table))
        .order_by(Sale.created_at.desc())
    ).all()
    return [(sale, Decimal(str(ret or 0)), Decimal(str(paid or 0))) for sale, ret, paid in rows]


def _customers_by_id(db: Session, customer_ids: set[int]) -> dict[int, Customer]:
    if not customer_ids:
        return {}
    rows = db.scalars(
        select(Customer).where(Customer.id.in_(customer_ids))
    ).all()
    return {int(c.id): c for c in rows}


def _open_room_charge_sale_ids(db: Session) -> set[int]:
    from modules.hotel.models import RoomCharge

    ids = db.scalars(
        select(RoomCharge.sale_id).where(RoomCharge.is_settled.is_(False))
    ).all()
    return {int(i) for i in ids}


def build_receivable_rows(
    db: Session,
    *,
    pay_filter: str | None = None,
    context_filter: str | None = None,
    only_with_balance: bool = False,
) -> list[ReceivableInvoiceRow]:
    room_open = _open_room_charge_sale_ids(db)
    out: list[ReceivableInvoiceRow] = []
    for sale, returned, paid in _load_completed_sales_with_amounts(db):
        net = Decimal(str(sale.total or 0)) - returned
        if net < 0:
            net = Decimal("0")
        net = net.quantize(Decimal("0.001"))
        paid = paid.quantize(Decimal("0.001"))
        outstanding = (net - paid).quantize(Decimal("0.001"))
        if outstanding < 0:
            outstanding = Decimal("0")
        status = _classify_pay_status(net, paid, outstanding)
        has_room = sale.id in room_open

        if context_filter and context_filter != "all":
            try:
                ctx = SaleContext(context_filter.upper())
            except ValueError:
                ctx = None
            if ctx is not None and sale.context_type != ctx:
                continue

        if pay_filter and pay_filter != "all":
            try:
                pf = InvoicePayStatus(pay_filter.lower())
            except ValueError:
                pf = None
            if pf is not None and status != pf:
                continue

        if only_with_balance and outstanding <= 0:
            continue

        out.append(
            ReceivableInvoiceRow(
                sale_id=sale.id,
                created_at=sale.created_at,
                context_type=sale.context_type,
                party_label=_party_label(
                    sale,
                    has_room_charge=has_room,
                    customer=customers.get(int(sale.customer_id))
                    if sale.customer_id
                    else None,
                ),
                customer_id=sale.customer_id,
                net_total=net,
                paid_total=paid,
                outstanding=outstanding,
                pay_status=status,
                has_open_room_charge=has_room,
            )
        )
    return out


def receivables_summary(db: Session) -> ReceivablesSummary:
    rows = build_receivable_rows(db)
    total = Decimal("0")
    with_bal = 0
    unpaid = partial = paid = 0
    for r in rows:
        if r.outstanding > 0:
            total += r.outstanding
            with_bal += 1
        if r.pay_status == InvoicePayStatus.UNPAID:
            unpaid += 1
        elif r.pay_status == InvoicePayStatus.PARTIAL:
            partial += 1
        else:
            paid += 1
    return ReceivablesSummary(
        total_outstanding=total.quantize(Decimal("0.001")),
        invoice_count_with_balance=with_bal,
        unpaid_count=unpaid,
        partial_count=partial,
        paid_count=paid,
    )


def customer_debt_groups(rows: list[ReceivableInvoiceRow]) -> list[CustomerDebtRow]:
    """تجميع الديون حسب العميل/الطرف (للتجار بعدة فواتير)."""
    buckets: dict[str, CustomerDebtRow] = {}
    for r in rows:
        if r.outstanding <= 0:
            continue
        key = str(r.customer_id) if r.customer_id else r.party_label
        if key not in buckets:
            buckets[key] = CustomerDebtRow(
                customer_id=r.customer_id,
                party_label=r.party_label,
                invoice_count=0,
                total_outstanding=Decimal("0"),
            )
        b = buckets[key]
        b.invoice_count += 1
        b.total_outstanding += r.outstanding
    groups = list(buckets.values())
    for g in groups:
        g.total_outstanding = g.total_outstanding.quantize(Decimal("0.001"))
    groups.sort(key=lambda x: x.total_outstanding, reverse=True)
    return groups
