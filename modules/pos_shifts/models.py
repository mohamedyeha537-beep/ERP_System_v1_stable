from __future__ import annotations

import enum
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import DateTime, Enum, ForeignKey, Numeric, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from infra.db import Base


class PosShiftStatus(str, enum.Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class PosShift(Base):
    """جلسة عمل كاشير — من فتحها حتى إغلاقها وعدّ النقد."""

    __tablename__ = "pos_shifts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    status: Mapped[PosShiftStatus] = mapped_column(
        Enum(PosShiftStatus), default=PosShiftStatus.OPEN, index=True
    )
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    opening_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    closing_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    counted_cash: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    expected_cash: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    cash_difference: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)

    counted_bank: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    expected_bank: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    bank_difference: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)

    user = relationship("User", foreign_keys=[user_id])
