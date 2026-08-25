from __future__ import annotations

import enum
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from infra.db import Base


class ShortageKind(str, enum.Enum):
    CASH = "CASH"
    BANK = "BANK"


class PosShiftStatus(str, enum.Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class PosShift(Base):
    """جلسة عمل كاشير — من فتحها حتى إغلاقها وعدّ النقد."""

    __tablename__ = "pos_shifts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    employee_id: Mapped[int | None] = mapped_column(
        ForeignKey("hr_employees.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[PosShiftStatus] = mapped_column(
        Enum(PosShiftStatus), default=PosShiftStatus.OPEN, index=True
    )
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    opening_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    opening_cash: Mapped[Decimal] = mapped_column(
        Numeric(14, 3), default=Decimal("0"), server_default="0"
    )
    closing_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    counted_cash: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    expected_cash: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    cash_difference: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)

    counted_bank: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    expected_bank: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    bank_difference: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)

    counted_room: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    expected_room: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    room_difference: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)

    gl_backfilled_entries: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    gl_operational_net: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    gl_revenue_net: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    gl_gap: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)

    treasury_handoff_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    treasury_handoff_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    warehouse_id: Mapped[int | None] = mapped_column(
        ForeignKey("warehouses.id", ondelete="SET NULL"), nullable=True, index=True
    )
    close_destination: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    opening_bank: Mapped[Decimal] = mapped_column(
        Numeric(14, 3), default=Decimal("0"), server_default="0"
    )
    received_from_shift_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    carried_to_shift_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    carried_to_employee_id: Mapped[int | None] = mapped_column(
        ForeignKey("hr_employees.id", ondelete="SET NULL"), nullable=True, index=True
    )

    user = relationship("User", foreign_keys=[user_id])
    employee = relationship("Employee", foreign_keys=[employee_id])
    shortage_records = relationship(
        "PosShiftShortage", back_populates="shift", cascade="all, delete-orphan"
    )


class PosShiftShortage(Base):
    """سجل عجز عند إقفال جلسة — مبلغ موجب = قيمة العجز."""

    __tablename__ = "pos_shift_shortages"
    __table_args__ = (
        UniqueConstraint("shift_id", "kind", name="uq_pos_shift_shortages_shift_kind"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    shift_id: Mapped[int] = mapped_column(
        ForeignKey("pos_shifts.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[ShortageKind] = mapped_column(Enum(ShortageKind), index=True)
    expected_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    counted_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    difference: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    shortage_amount: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    employee_id: Mapped[int | None] = mapped_column(
        ForeignKey("hr_employees.id", ondelete="SET NULL"), nullable=True, index=True
    )
    closing_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    original_shortage_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 3), nullable=True
    )
    resolved_action: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    resolved_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    payroll_deduction_id: Mapped[int | None] = mapped_column(
        ForeignKey("hr_employee_deductions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    expense_purchase_id: Mapped[int | None] = mapped_column(
        ForeignKey("purchases.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    shift = relationship("PosShift", back_populates="shortage_records")
    user = relationship("User", foreign_keys=[user_id])
    employee = relationship("Employee", foreign_keys=[employee_id])