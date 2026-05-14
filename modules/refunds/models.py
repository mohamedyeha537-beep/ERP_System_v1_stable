from __future__ import annotations

import enum
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import DateTime, Enum, ForeignKey, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from infra.db import Base


class SaleReturnType(str, enum.Enum):
    PARTIAL = "PARTIAL"
    FULL = "FULL"


class SaleReturnStatus(str, enum.Enum):
    POSTED = "POSTED"


class SaleReturnMode(str, enum.Enum):
    SAME_METHOD = "SAME_METHOD"
    OVERRIDE_TRANSFER = "OVERRIDE_TRANSFER"
    REDUCE_RECEIVABLE = "REDUCE_RECEIVABLE"
    NO_PAYMENT = "NO_PAYMENT"


class SaleReturn(Base):
    __tablename__ = "sale_returns"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    original_sale_id: Mapped[int] = mapped_column(
        ForeignKey("sales.id", ondelete="CASCADE"),
        index=True,
    )
    return_type: Mapped[SaleReturnType] = mapped_column(
        Enum(SaleReturnType),
        default=SaleReturnType.PARTIAL,
        index=True,
    )
    status: Mapped[SaleReturnStatus] = mapped_column(
        Enum(SaleReturnStatus),
        default=SaleReturnStatus.POSTED,
        index=True,
    )
    mode: Mapped[SaleReturnMode] = mapped_column(
        Enum(SaleReturnMode),
        default=SaleReturnMode.NO_PAYMENT,
        index=True,
    )
    total: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
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
    approved_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    original_payment_method_id: Mapped[int | None] = mapped_column(
        ForeignKey("payment_methods.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    refund_payment_method_id: Mapped[int | None] = mapped_column(
        ForeignKey("payment_methods.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    sale = relationship("Sale", foreign_keys=[original_sale_id], lazy="selectin")
    original_payment_method = relationship(
        "PaymentMethod",
        foreign_keys=[original_payment_method_id],
        lazy="selectin",
    )
    refund_payment_method = relationship(
        "PaymentMethod",
        foreign_keys=[refund_payment_method_id],
        lazy="selectin",
    )
    lines: Mapped[list["SaleReturnLine"]] = relationship(
        back_populates="sale_return",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class SaleReturnLine(Base):
    __tablename__ = "sale_return_lines"
    __table_args__ = (
        UniqueConstraint(
            "sale_return_id",
            "sale_line_id",
            name="uq_sale_return_lines_return_line",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    sale_return_id: Mapped[int] = mapped_column(
        ForeignKey("sale_returns.id", ondelete="CASCADE"),
        index=True,
    )
    sale_line_id: Mapped[int] = mapped_column(
        ForeignKey("sale_lines.id", ondelete="RESTRICT"),
        index=True,
    )
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"),
        index=True,
    )
    quantity: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    line_total: Mapped[Decimal] = mapped_column(Numeric(14, 3))

    sale_return: Mapped[SaleReturn] = relationship(
        back_populates="lines",
        lazy="selectin",
    )
    sale_line = relationship("SaleLine", foreign_keys=[sale_line_id], lazy="selectin")
    product = relationship("Product", foreign_keys=[product_id], lazy="selectin")
