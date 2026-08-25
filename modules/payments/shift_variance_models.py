"""سجل فروقات العهدة والخزينة — لا يُحذف بعد إنشائه."""
from __future__ import annotations

import enum
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from infra.db import Base


class ShiftVarianceSource(str, enum.Enum):
    HOTEL_CARRY = "HOTEL_CARRY"
    HOTEL_CLOSE = "HOTEL_CLOSE"
    HOTEL_TREASURY = "HOTEL_TREASURY"
    POS_TREASURY = "POS_TREASURY"
    POS_CARRY = "POS_CARRY"
    TREASURY_CLOSE = "TREASURY_CLOSE"
    TREASURY_BANK_MATCH = "TREASURY_BANK_MATCH"


class ShiftVarianceKind(str, enum.Enum):
    CASH = "CASH"
    BANK = "BANK"


class ShiftVarianceStatus(str, enum.Enum):
    PENDING_REVIEW = "PENDING_REVIEW"
    CHARGED = "CHARGED"
    ADMIN_SETTLED = "ADMIN_SETTLED"
    CANCELLED = "CANCELLED"


class ShiftVariance(Base):
    """فرق عهدة/خزينة معلّق حتى يقرّر الأدمن المسؤولية."""

    __tablename__ = "shift_variances"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ref: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    source_type: Mapped[ShiftVarianceSource] = mapped_column(
        Enum(ShiftVarianceSource, native_enum=False, length=24), index=True
    )
    kind: Mapped[ShiftVarianceKind] = mapped_column(
        Enum(ShiftVarianceKind, native_enum=False, length=8), index=True
    )
    status: Mapped[ShiftVarianceStatus] = mapped_column(
        Enum(ShiftVarianceStatus, native_enum=False, length=24),
        default=ShiftVarianceStatus.PENDING_REVIEW,
        index=True,
    )
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
    claimed_amount: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    received_amount: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    difference: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    resolved_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    payroll_deduction_id: Mapped[int | None] = mapped_column(
        ForeignKey("hr_employee_deductions.id", ondelete="SET NULL"), nullable=True
    )

    from_employee = relationship("Employee", foreign_keys=[from_employee_id])
    to_employee = relationship("Employee", foreign_keys=[to_employee_id])
    resolved_by = relationship("User", foreign_keys=[resolved_by_id])
