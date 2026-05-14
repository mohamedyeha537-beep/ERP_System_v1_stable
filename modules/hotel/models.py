"""Hotel rooms & deferred-payment (room-charge) models.

نظام بسيط:
- HotelRoom: رقم الغرفة + اسم النزيل الحالي (اختياري) + ملاحظات.
- RoomCharge: ربط فاتورة بيع بغرفة، تبقى قائمة حتى تتم تسويتها لاحقاً.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from infra.db import Base


class HotelRoom(Base):
    __tablename__ = "hotel_rooms"
    __table_args__ = (UniqueConstraint("number", name="uq_hotel_rooms_number"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    number: Mapped[str] = mapped_column(String(40), index=True)
    guest_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class RoomCharge(Base):
    """فاتورة مفتوحة على حساب غرفة فندق — تنتظر تسوية لاحقة.

    الفاتورة (Sale) تكتمل وتخصم المخزون وترسل للمطبخ كالعادة،
    لكن لا يُسجَّل لها SalePayment حتى تتم تسوية الغرفة.
    """

    __tablename__ = "hotel_room_charges"
    __table_args__ = (
        UniqueConstraint("sale_id", name="uq_hotel_room_charges_sale_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    sale_id: Mapped[int] = mapped_column(
        ForeignKey("sales.id", ondelete="CASCADE"), index=True
    )
    room_id: Mapped[int] = mapped_column(
        ForeignKey("hotel_rooms.id", ondelete="RESTRICT"), index=True
    )
    guest_name_snapshot: Mapped[str | None] = mapped_column(String(160), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    is_settled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    settled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    settled_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    settlement_payment_method_id: Mapped[int | None] = mapped_column(
        ForeignKey("payment_methods.id", ondelete="SET NULL"), nullable=True
    )

    room: Mapped[HotelRoom] = relationship("HotelRoom", lazy="selectin")
    sale = relationship("Sale", lazy="selectin")
