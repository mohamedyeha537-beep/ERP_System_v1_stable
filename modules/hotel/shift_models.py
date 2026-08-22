"""ورديات الفندق — جلسة استقبال من فتح حتى إقفال."""
from __future__ import annotations

import enum
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from infra.db import Base


class HotelShiftStatus(str, enum.Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class HotelShiftError(Exception):
    pass


class HotelShift(Base):
    __tablename__ = "hotel_shifts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    property_id: Mapped[int] = mapped_column(Integer, default=1, server_default="1", index=True)
    shift_number: Mapped[int] = mapped_column(Integer, index=True)
    shift_name_ar: Mapped[str] = mapped_column(String(80))

    status: Mapped[HotelShiftStatus] = mapped_column(
        Enum(HotelShiftStatus), default=HotelShiftStatus.OPEN, index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    closed_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    employee_id: Mapped[int | None] = mapped_column(
        ForeignKey("hr_employees.id", ondelete="SET NULL"), nullable=True, index=True
    )

    scheduled_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    scheduled_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    opening_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    closing_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    total_revenue: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    total_expenses: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    cash_collected: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    bank_collected: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    payment_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    expense_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    opening_cash: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    counted_cash: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    counted_bank: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    expected_cash: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    expected_bank: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    cash_difference: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    bank_difference: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    expected_bookings_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    counted_bookings_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expected_meals_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    counted_meals_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expected_laundry_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    counted_laundry_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expected_services_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    counted_services_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    close_snapshot_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    overdue_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: اعتماد أمين الخزينة وتحويل إيراد الجلسة من محفظة الاستقبال → خزينة الفندق
    treasury_handoff_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    treasury_handoff_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    #: TREASURY = اعتماد أمين الخزينة · NEXT_SHIFT = تسليم الدرج للوردية التالية
    close_destination: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    opening_bank: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    received_from_shift_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    carried_to_shift_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    carried_to_employee_id: Mapped[int | None] = mapped_column(
        ForeignKey("hr_employees.id", ondelete="SET NULL"), nullable=True, index=True
    )

    user = relationship("User", foreign_keys=[user_id])
    closed_by = relationship("User", foreign_keys=[closed_by_id])
    employee = relationship("Employee", foreign_keys=[employee_id])
    carried_to_employee = relationship("Employee", foreign_keys=[carried_to_employee_id])
    treasury_handoff_by = relationship(
        "User", foreign_keys=[treasury_handoff_by_id]
    )
