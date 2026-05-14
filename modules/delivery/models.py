from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from infra.db import Base


class DeliveryZone(Base):
    __tablename__ = "delivery_zones"
    __table_args__ = (UniqueConstraint("name_ar", name="uq_delivery_zones_name_ar"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name_ar: Mapped[str] = mapped_column(String(120), index=True)
    fee: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class DeliveryCashSettlement(Base):
    __tablename__ = "delivery_cash_settlements"
    __table_args__ = (UniqueConstraint("sale_id", name="uq_delivery_cash_settlements_sale_id"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    sale_id: Mapped[int] = mapped_column(
        ForeignKey("sales.id", ondelete="CASCADE"),
        index=True,
    )
    cash_method_id: Mapped[int] = mapped_column(
        ForeignKey("payment_methods.id", ondelete="RESTRICT"),
        index=True,
    )
    zone_id: Mapped[int | None] = mapped_column(
        ForeignKey("delivery_zones.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    sale = relationship("Sale", lazy="selectin")
    cash_method = relationship("PaymentMethod", foreign_keys=[cash_method_id], lazy="selectin")
    zone = relationship("DeliveryZone", foreign_keys=[zone_id], lazy="selectin")
