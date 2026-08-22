"""Hotel rooms & deferred-payment (room-charge) models."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from infra.db import Base
from modules.hotel.booking_models import RoomPhysicalStatus


class HotelRoom(Base):
    __tablename__ = "hotel_rooms"
    __table_args__ = (UniqueConstraint("number", name="uq_hotel_rooms_number"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    property_id: Mapped[int] = mapped_column(
        ForeignKey("hotel_properties.id", ondelete="RESTRICT"), index=True, default=1
    )
    room_type_id: Mapped[int | None] = mapped_column(
        ForeignKey("hotel_room_types.id", ondelete="SET NULL"), nullable=True, index=True
    )
    number: Mapped[str] = mapped_column(String(40), index=True)
    name_ar: Mapped[str | None] = mapped_column(String(120), nullable=True)
    nightly_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    image_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    show_online: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    online_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    floor: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # رقم القفل لنظام البطاقات (8 خانات كما في SDK — مثال: 01020399)
    # deferred: لا يفشل تحميل الشقة إن لم يُرقَّع العمود بعد
    lock_no: Mapped[str | None] = mapped_column(
        String(16), nullable=True, index=True, deferred=True
    )
    #: عدد الغرف داخل الوحدة (يظهر كرمز على كرت الشقة)
    rooms_count: Mapped[int] = mapped_column(Integer, default=1)
    #: إجمالي الأسرة (زوجية + فردية) — للتوافق مع التقارير القديمة
    beds_count: Mapped[int] = mapped_column(Integer, default=1)
    #: أسرة زوجية (رمز 🛏 على الكرت)
    double_beds_count: Mapped[int] = mapped_column(Integer, default=1)
    #: أسرة فردية (رمز 🛌 على الكرت)
    single_beds_count: Mapped[int] = mapped_column(Integer, default=0)
    #: يظهر رمز رضيع على الكرت عند التفعيل
    allows_infant: Mapped[bool] = mapped_column(Boolean, default=False)
    guest_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    physical_status: Mapped[RoomPhysicalStatus] = mapped_column(
        Enum(RoomPhysicalStatus), default=RoomPhysicalStatus.AVAILABLE, index=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    seo_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    seo_description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    seo_h1: Mapped[str | None] = mapped_column(String(255), nullable=True)
    seo_slug: Mapped[str | None] = mapped_column(String(255), nullable=True)
    seo_keywords: Mapped[str | None] = mapped_column(String(500), nullable=True)
    seo_schema_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    seo_og_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    seo_og_description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    seo_og_image: Mapped[str | None] = mapped_column(String(500), nullable=True)
    seo_indexable: Mapped[bool] = mapped_column(Boolean, default=True)
    seo_canonical_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    seo_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    room_type: Mapped["HotelRoomType | None"] = relationship(
        back_populates="rooms", lazy="selectin"
    )
    media_items: Mapped[list["HotelRoomMedia"]] = relationship(
        back_populates="room",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="HotelRoomMedia.sort_order",
    )


from modules.hotel.store_models import HotelRoomMedia  # noqa: E402


class RoomCharge(Base):
    """فاتورة مفتوحة على حساب غرفة فندق — تنتظر تسوية لاحقة."""

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
    booking_id: Mapped[int | None] = mapped_column(
        ForeignKey("hotel_bookings.id", ondelete="SET NULL"), nullable=True, index=True
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
    #: توجيه فاتورة المطعم/الخدمات: COMPANY | GUEST | SHARED
    service_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    folio_side: Mapped[str | None] = mapped_column(String(16), nullable=True)
    company_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    guest_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)

    room: Mapped[HotelRoom] = relationship("HotelRoom", lazy="selectin")
    sale = relationship("Sale", lazy="selectin")


from modules.hotel.booking_models import HotelRoomType  # noqa: E402
