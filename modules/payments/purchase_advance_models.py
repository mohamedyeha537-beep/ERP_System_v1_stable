"""عهدة مشتريات — أصل مالي لدى موظف، ليست مصروفاً حتى تُسوّى."""
from __future__ import annotations

import enum
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import DateTime, Enum, ForeignKey, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from infra.db import Base


class PurchaseAdvanceStatus(str, enum.Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class PurchaseAdvance(Base):
    __tablename__ = "purchase_advances"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ref: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    status: Mapped[PurchaseAdvanceStatus] = mapped_column(
        Enum(PurchaseAdvanceStatus, native_enum=False, length=12),
        default=PurchaseAdvanceStatus.OPEN,
        index=True,
    )
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("hr_employees.id", ondelete="RESTRICT"), index=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    returned_amount: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    source_pm_id: Mapped[int] = mapped_column(
        ForeignKey("payment_methods.id", ondelete="RESTRICT")
    )
    custody_pm_id: Mapped[int] = mapped_column(
        ForeignKey("payment_methods.id", ondelete="RESTRICT"), index=True
    )
    purpose: Mapped[str | None] = mapped_column(String(200), nullable=True)
    domain: Mapped[str] = mapped_column(String(16), default="restaurant", index=True)
    transfer_id: Mapped[int | None] = mapped_column(
        ForeignKey("payment_transfers.id", ondelete="SET NULL"), nullable=True
    )
    return_transfer_id: Mapped[int | None] = mapped_column(
        ForeignKey("payment_transfers.id", ondelete="SET NULL"), nullable=True
    )
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    employee = relationship("Employee", foreign_keys=[employee_id])
    source_pm = relationship("PaymentMethod", foreign_keys=[source_pm_id])
    custody_pm = relationship("PaymentMethod", foreign_keys=[custody_pm_id])
    created_by = relationship("User", foreign_keys=[created_by_id])
    closed_by = relationship("User", foreign_keys=[closed_by_id])
