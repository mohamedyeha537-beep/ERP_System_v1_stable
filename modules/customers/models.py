"""Customer & loyalty models.

العملاء يعرَّفون برقم الهاتف (مفتاح فريد). الاسم اختياري.
نقاط الولاء تُجمَع تلقائياً عند الدفع، ويمكن استخدامها كخصم لاحقاً.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from infra.db import Base


class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (UniqueConstraint("phone", name="uq_customers_phone"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    phone: Mapped[str] = mapped_column(String(40), index=True)
    name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    email: Mapped[str | None] = mapped_column(String(160), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    points_balance: Mapped[Decimal] = mapped_column(
        Numeric(14, 3), default=Decimal("0")
    )
    total_spent: Mapped[Decimal] = mapped_column(
        Numeric(14, 3), default=Decimal("0")
    )
    visits_count: Mapped[int] = mapped_column(Integer, default=0)
    last_visit_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class LoyaltyTxnKind(str, enum.Enum):
    EARN = "EARN"          # كسب نقاط من فاتورة
    REDEEM = "REDEEM"      # استخدام نقاط كخصم
    ADJUST = "ADJUST"      # تعديل يدوي من الأدمن


class LoyaltyTransaction(Base):
    """سجل تاريخي لكل حركة على نقاط العميل (كسب/استخدام/تعديل)."""

    __tablename__ = "loyalty_transactions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), index=True
    )
    sale_id: Mapped[int | None] = mapped_column(
        ForeignKey("sales.id", ondelete="SET NULL"), nullable=True, index=True
    )
    kind: Mapped[LoyaltyTxnKind] = mapped_column(
        Enum(LoyaltyTxnKind), default=LoyaltyTxnKind.EARN, index=True
    )
    points: Mapped[Decimal] = mapped_column(
        Numeric(14, 3), default=Decimal("0")
    )  # موجبة للكسب، سالبة للاستخدام
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    customer: Mapped[Customer] = relationship(lazy="selectin")
