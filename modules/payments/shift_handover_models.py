"""تسليم عهدة / إعلان تحويل مصرفي — قبل الإقفال أو عنده."""
from __future__ import annotations

import enum
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from infra.db import Base


class ShiftHandoverKind(str, enum.Enum):
    CASH_CARRY = "CASH_CARRY"
    BANK_TRANSFER = "BANK_TRANSFER"


class ShiftHandoverStatus(str, enum.Enum):
    SENT = "SENT"
    CONFIRMED = "CONFIRMED"
    CANCELLED = "CANCELLED"


class ShiftHandover(Base):
    __tablename__ = "shift_handovers"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ref: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    kind: Mapped[ShiftHandoverKind] = mapped_column(
        Enum(ShiftHandoverKind, native_enum=False, length=20), index=True
    )
    status: Mapped[ShiftHandoverStatus] = mapped_column(
        Enum(ShiftHandoverStatus, native_enum=False, length=16),
        default=ShiftHandoverStatus.SENT,
        index=True,
    )
    domain: Mapped[str] = mapped_column(String(16), index=True)
    hotel_shift_id: Mapped[int | None] = mapped_column(
        ForeignKey("hotel_shifts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    pos_shift_id: Mapped[int | None] = mapped_column(
        ForeignKey("pos_shifts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    from_employee_id: Mapped[int | None] = mapped_column(
        ForeignKey("hr_employees.id", ondelete="SET NULL"), nullable=True, index=True
    )
    to_employee_id: Mapped[int | None] = mapped_column(
        ForeignKey("hr_employees.id", ondelete="SET NULL"), nullable=True, index=True
    )
    claimed_cash: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    claimed_bank: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    received_cash: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    received_bank: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    handover_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    bank_name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    bank_ref: Mapped[str | None] = mapped_column(String(80), nullable=True)
    bank_transferred_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    confirmed_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    from_employee = relationship("Employee", foreign_keys=[from_employee_id])
    to_employee = relationship("Employee", foreign_keys=[to_employee_id])
