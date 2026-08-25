"""إحصائيات العميل — مشتريات، مديونية، فواتير."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from modules.customers.models import Customer
from modules.sales.models import Sale, SaleStatus


@dataclass
class CustomerStats:
    purchase_count: int
    sales_total: Decimal
    amount_paid: Decimal
    outstanding_debt: Decimal
    wallet_balance: Decimal
    points_balance: Decimal
    visits_count: int
    total_spent_cached: Decimal


def customer_stats(db: Session, customer: Customer) -> CustomerStats:
    sale_rows = db.execute(
        select(
            func.count(Sale.id),
            func.coalesce(func.sum(Sale.total), 0),
        ).where(
            Sale.customer_id == customer.id,
            Sale.status == SaleStatus.COMPLETED,
        )
    ).one()
    purchase_count = int(sale_rows[0] or 0)
    sales_total = Decimal(str(sale_rows[1] or 0)).quantize(Decimal("0.001"))

    from modules.customers.account_balance import (
        folio_covered_sale_ids_for_customer,
        hotel_outstanding_for_customer,
        pos_outstanding_for_customer,
    )

    covered = folio_covered_sale_ids_for_customer(db, customer)
    pos_debt = pos_outstanding_for_customer(
        db, customer, exclude_sale_ids=covered
    )
    hotel_debt = hotel_outstanding_for_customer(db, customer)
    debt = (pos_debt + hotel_debt).quantize(Decimal("0.001"))

    return CustomerStats(
        purchase_count=purchase_count,
        sales_total=sales_total,
        amount_paid=Decimal(str(customer.total_spent or 0)).quantize(Decimal("0.001")),
        outstanding_debt=debt,
        wallet_balance=Decimal(str(customer.wallet_balance or 0)).quantize(Decimal("0.001")),
        points_balance=Decimal(str(customer.points_balance or 0)).quantize(Decimal("0.001")),
        visits_count=int(customer.visits_count or 0),
        total_spent_cached=Decimal(str(customer.total_spent or 0)).quantize(Decimal("0.001")),
    )


def list_customer_sales(db: Session, customer_id: int, *, limit: int = 50) -> list[Sale]:
    return list(
        db.scalars(
            select(Sale)
            .where(Sale.customer_id == customer_id, Sale.status == SaleStatus.COMPLETED)
            .order_by(Sale.id.desc())
            .limit(limit)
        ).all()
    )
