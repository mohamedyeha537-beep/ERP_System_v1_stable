"""ذمم دائن — ديون المتجر للموردين (فواتير شراء)."""
from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from modules.payments.models import (
    Purchase,
    PurchaseKind,
    PurchasePayment,
    SUPPLIER_CREDIT_PM_NAME,
)
from modules.payments.service import is_supplier_credit_payment_method


class PurchasePayStatus(str, enum.Enum):
    PAID = "paid"
    PARTIAL = "partial"
    UNPAID = "unpaid"


@dataclass
class PayablePurchaseRow:
    purchase_id: int
    created_at: datetime | None
    kind: PurchaseKind
    supplier_label: str
    invoice_total: Decimal
    paid_total: Decimal
    outstanding: Decimal
    pay_status: PurchasePayStatus
    wallet_label: str


@dataclass
class SupplierDebtRow:
    supplier_label: str
    invoice_count: int
    total_outstanding: Decimal


@dataclass
class PayablesSummary:
    total_outstanding: Decimal
    invoice_count_with_balance: int
    unpaid_count: int
    partial_count: int
    paid_count: int


def _classify_pay_status(
    total: Decimal, paid: Decimal, outstanding: Decimal
) -> PurchasePayStatus:
    if total <= 0 or outstanding <= 0:
        return PurchasePayStatus.PAID
    if paid <= 0:
        return PurchasePayStatus.UNPAID
    return PurchasePayStatus.PARTIAL


def _wallet_label(purchase: Purchase, paid: Decimal) -> str:
    if purchase.method and is_supplier_credit_payment_method(purchase.method):
        if paid <= 0:
            return SUPPLIER_CREDIT_PM_NAME
    if purchase.method:
        return purchase.method.name_ar
    return "—"


def _load_purchases_with_paid(
    db: Session,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    kind: PurchaseKind | None = None,
) -> list[tuple[Purchase, Decimal]]:
    paid_sq = (
        select(
            PurchasePayment.purchase_id.label("purchase_id"),
            func.coalesce(func.sum(PurchasePayment.amount), 0).label("paid"),
        )
        .group_by(PurchasePayment.purchase_id)
        .subquery()
    )
    stmt = (
        select(
            Purchase,
            func.coalesce(paid_sq.c.paid, 0),
        )
        .outerjoin(paid_sq, paid_sq.c.purchase_id == Purchase.id)
        .options(selectinload(Purchase.method))
        .order_by(Purchase.created_at.desc(), Purchase.id.desc())
    )
    if start is not None:
        stmt = stmt.where(Purchase.created_at >= start)
    if end is not None:
        stmt = stmt.where(Purchase.created_at < end)
    if kind is not None:
        stmt = stmt.where(Purchase.kind == kind)
    rows = db.execute(stmt).all()
    return [(p, Decimal(str(paid or 0))) for p, paid in rows]


def build_payable_rows(
    db: Session,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    kind: PurchaseKind | None = PurchaseKind.INVENTORY,
    pay_filter: str | None = None,
    only_with_balance: bool = False,
) -> list[PayablePurchaseRow]:
    out: list[PayablePurchaseRow] = []
    for purchase, paid_raw in _load_purchases_with_paid(
        db, start=start, end=end, kind=kind
    ):
        total = Decimal(str(purchase.amount or 0)).quantize(Decimal("0.001"))
        paid = paid_raw.quantize(Decimal("0.001"))
        outstanding = (total - paid).quantize(Decimal("0.001"))
        if outstanding < 0:
            outstanding = Decimal("0")
        status = _classify_pay_status(total, paid, outstanding)

        if pay_filter and pay_filter != "all":
            try:
                pf = PurchasePayStatus(pay_filter.lower())
            except ValueError:
                pf = None
            if pf is not None and status != pf:
                continue

        if only_with_balance and outstanding <= 0:
            continue

        supplier = (purchase.supplier or "").strip() or "مورّد غير محدد"
        out.append(
            PayablePurchaseRow(
                purchase_id=purchase.id,
                created_at=purchase.created_at,
                kind=purchase.kind,
                supplier_label=supplier,
                invoice_total=total,
                paid_total=paid,
                outstanding=outstanding,
                pay_status=status,
                wallet_label=_wallet_label(purchase, paid),
            )
        )
    return out


def payables_summary(
    db: Session, kind: PurchaseKind | None = PurchaseKind.INVENTORY
) -> PayablesSummary:
    rows = build_payable_rows(db, kind=kind)
    total = Decimal("0")
    with_bal = 0
    unpaid = partial = paid = 0
    for r in rows:
        if r.outstanding > 0:
            total += r.outstanding
            with_bal += 1
        if r.pay_status == PurchasePayStatus.UNPAID:
            unpaid += 1
        elif r.pay_status == PurchasePayStatus.PARTIAL:
            partial += 1
        else:
            paid += 1
    return PayablesSummary(
        total_outstanding=total.quantize(Decimal("0.001")),
        invoice_count_with_balance=with_bal,
        unpaid_count=unpaid,
        partial_count=partial,
        paid_count=paid,
    )


def supplier_debt_groups(rows: list[PayablePurchaseRow]) -> list[SupplierDebtRow]:
    buckets: dict[str, SupplierDebtRow] = {}
    for r in rows:
        if r.outstanding <= 0:
            continue
        key = r.supplier_label
        if key not in buckets:
            buckets[key] = SupplierDebtRow(
                supplier_label=key, invoice_count=0, total_outstanding=Decimal("0")
            )
        b = buckets[key]
        b.invoice_count += 1
        b.total_outstanding += r.outstanding
    groups = list(buckets.values())
    for g in groups:
        g.total_outstanding = g.total_outstanding.quantize(Decimal("0.001"))
    groups.sort(key=lambda x: x.total_outstanding, reverse=True)
    return groups
