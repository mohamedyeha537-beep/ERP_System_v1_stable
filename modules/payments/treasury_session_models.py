"""افتتاح وإقفال يومي لأمين الخزينة — نقطة مسؤولية مستقلة عن وردية الاستقبال."""
from __future__ import annotations

import enum
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import DateTime, Enum, ForeignKey, Numeric, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from infra.db import Base


class TreasurySessionStatus(str, enum.Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class TreasurySession(Base):
    __tablename__ = "treasury_sessions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    status: Mapped[TreasurySessionStatus] = mapped_column(
        Enum(TreasurySessionStatus, native_enum=False, length=12),
        default=TreasurySessionStatus.OPEN,
        index=True,
    )
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    opened_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    opening_cash: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    opening_bank: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    counted_cash: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    counted_bank: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    expected_cash: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    expected_bank: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    cash_in: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    cash_out: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    bank_in: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    bank_out: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    advances_out: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    close_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    opened_by = relationship("User", foreign_keys=[opened_by_id])
    closed_by = relationship("User", foreign_keys=[closed_by_id])
