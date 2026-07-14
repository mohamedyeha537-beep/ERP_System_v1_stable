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
    domain=None,
) -> list[tuple[Purchase, Decimal]]:
    from modules.platform.business_domain import purchase_domain_db_values

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
        .order_by(Purchase.id.desc())
    )
    if start is not None:
        stmt = stmt.where(Purchase.created_at >= start)
    if end is not None:
        stmt = stmt.where(Purchase.created_at < end)
    if kind is not None:
        stmt = stmt.where(Purchase.kind == kind)
    domain_vals = purchase_domain_db_values(domain)
    if domain_vals is not None:
        stmt = stmt.where(Purchase.business_domain.in_(domain_vals))
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
    domain=None,
) -> list[PayablePurchaseRow]:
    out: list[PayablePurchaseRow] = []
    for purchase, paid_raw in _load_purchases_with_paid(
        db, start=start, end=end, kind=kind, domain=domain
    ):
        from modules.payments.cost_reference import is_cost_reference_purchase

        if is_cost_reference_purchase(purchase):
            continue
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
    db: Session, kind: PurchaseKind | None = PurchaseKind.INVENTORY, domain=None
) -> PayablesSummary:
    rows = build_payable_rows(db, kind=kind, domain=domain)
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


PAY_STATUS_LABELS: dict[PurchasePayStatus, str] = {
    PurchasePayStatus.PAID: "مدفوعة",
    PurchasePayStatus.UNPAID: "غير مدفوعة",
    PurchasePayStatus.PARTIAL: "مدفوعة جزئياً",
}


@dataclass
class InventoryPurchaseRowView:
    purchase: Purchase
    amount: Decimal
    paid: Decimal
    outstanding: Decimal
    pay_status: PurchasePayStatus
    status_label: str
    is_cost_reference: bool


@dataclass
class PurchasesListSummary:
    invoice_count: int
    total_purchases: Decimal
    total_paid: Decimal
    total_outstanding: Decimal
    cost_reference_total: Decimal
    cost_reference_count: int
    unpaid_count: int
    partial_count: int
    paid_count: int


def list_inventory_supplier_names(db: Session) -> list[str]:
    rows = db.execute(
        select(Purchase.supplier)
        .where(
            Purchase.kind == PurchaseKind.INVENTORY,
            Purchase.supplier.is_not(None),
            Purchase.supplier != "",
        )
        .distinct()
        .order_by(Purchase.supplier)
    ).all()
    return [str(r[0]).strip() for r in rows if r[0] and str(r[0]).strip()]


def inventory_purchase_row_view(db: Session, purchase: Purchase) -> InventoryPurchaseRowView:
    from modules.payments.cost_reference import (
        effective_purchase_amount,
        is_cost_reference_purchase,
    )
    from modules.payments.service import purchase_outstanding, sum_purchase_payments

    if is_cost_reference_purchase(purchase):
        amt = effective_purchase_amount(purchase)
        return InventoryPurchaseRowView(
            purchase=purchase,
            amount=amt,
            paid=Decimal("0"),
            outstanding=Decimal("0"),
            pay_status=PurchasePayStatus.PAID,
            status_label="مرجع تكلفة",
            is_cost_reference=True,
        )
    amt = Decimal(str(purchase.amount or 0)).quantize(Decimal("0.001"))
    paid = sum_purchase_payments(db, purchase.id)
    outstanding = purchase_outstanding(db, purchase)
    status = _classify_pay_status(amt, paid, outstanding)
    return InventoryPurchaseRowView(
        purchase=purchase,
        amount=amt,
        paid=paid,
        outstanding=outstanding,
        pay_status=status,
        status_label=PAY_STATUS_LABELS[status],
        is_cost_reference=False,
    )


def build_inventory_purchase_list_views(
    db: Session,
    purchases: list[Purchase],
    *,
    pay_filter: str | None = None,
) -> tuple[list[InventoryPurchaseRowView], PurchasesListSummary]:
    views: list[InventoryPurchaseRowView] = []
    for p in purchases:
        v = inventory_purchase_row_view(db, p)
        if pay_filter and pay_filter not in ("", "all"):
            try:
                pf = PurchasePayStatus(pay_filter.lower())
            except ValueError:
                pf = None
            if pf is not None and v.pay_status != pf:
                continue
            if pay_filter == "debt" and v.outstanding <= 0:
                continue
        views.append(v)

    supplier_views = [v for v in views if not v.is_cost_reference]
    cost_ref_views = [v for v in views if v.is_cost_reference]
    total_purchases = sum((v.amount for v in supplier_views), Decimal("0"))
    total_paid = sum((v.paid for v in supplier_views), Decimal("0"))
    total_outstanding = sum((v.outstanding for v in supplier_views), Decimal("0"))
    cost_reference_total = sum((v.amount for v in cost_ref_views), Decimal("0"))
    unpaid = partial = paid = 0
    for v in supplier_views:
        if v.pay_status == PurchasePayStatus.UNPAID:
            unpaid += 1
        elif v.pay_status == PurchasePayStatus.PARTIAL:
            partial += 1
        else:
            paid += 1
    summary = PurchasesListSummary(
        invoice_count=len(views),
        total_purchases=total_purchases.quantize(Decimal("0.001")),
        total_paid=total_paid.quantize(Decimal("0.001")),
        total_outstanding=total_outstanding.quantize(Decimal("0.001")),
        cost_reference_total=cost_reference_total.quantize(Decimal("0.001")),
        cost_reference_count=len(cost_ref_views),
        unpaid_count=unpaid,
        partial_count=partial,
        paid_count=paid,
    )
    return views, summary
