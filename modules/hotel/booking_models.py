"""نماذج حجز الفندق — منفصلة عن RoomCharge/POS."""
from __future__ import annotations

import enum
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from infra.db import Base


class RoomPhysicalStatus(str, enum.Enum):
    AVAILABLE = "AVAILABLE"
    RESERVED = "RESERVED"
    OCCUPIED = "OCCUPIED"
    DIRTY = "DIRTY"
    CLEANING = "CLEANING"  # مهمة تنظيف أُرسلت — بانتظار تأكيد الانتهاء
    MAINTENANCE = "MAINTENANCE"
    OUT_OF_SERVICE = "OUT_OF_SERVICE"
    BLOCKED = "BLOCKED"


class BookingStatus(str, enum.Enum):
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    CHECKED_IN = "CHECKED_IN"
    CHECKED_OUT = "CHECKED_OUT"
    CANCELLED = "CANCELLED"
    NO_SHOW = "NO_SHOW"
    # إلغاء بعد موعد الدخول (الزائر حضر للإلغاء أو ألغى متأخراً) — ليس No Show
    LATE_CANCELLATION = "LATE_CANCELLATION"

# حالات تُحرِّر الشقة من حساب الإشغال
BOOKING_STATUS_ROOM_FREE = frozenset(
    {
        BookingStatus.CANCELLED,
        BookingStatus.NO_SHOW,
        BookingStatus.LATE_CANCELLATION,
        BookingStatus.CHECKED_OUT,
    }
)


class BookingPaymentStatus(str, enum.Enum):
    UNPAID = "UNPAID"
    PARTIALLY_PAID = "PARTIALLY_PAID"
    FULLY_PAID = "FULLY_PAID"
    REFUNDED = "REFUNDED"


class BookingSource(str, enum.Enum):
    RECEPTION = "RECEPTION"
    PORTAL = "PORTAL"
    ONLINE_STORE = "ONLINE_STORE"
    PHONE = "PHONE"
    WALK_IN = "WALK_IN"
    OTHER = "OTHER"


class GuestType(str, enum.Enum):
    INDIVIDUAL = "INDIVIDUAL"
    COMPANY = "COMPANY"


class RecordKind(str, enum.Enum):
    BOOKING = "BOOKING"
    QUOTATION = "QUOTATION"


class SecurityContactChannel(str, enum.Enum):
    WHATSAPP = "WHATSAPP"
    EMAIL = "EMAIL"


class SecurityGuestScope(str, enum.Enum):
    FOREIGN = "FOREIGN"
    LIBYAN = "LIBYAN"
    BOTH = "BOTH"


class QuotationStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    SENT = "SENT"
    UNDER_REVIEW = "UNDER_REVIEW"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CONVERTED = "CONVERTED"


class HotelInvoiceStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    ISSUED = "ISSUED"
    PARTIALLY_PAID = "PARTIALLY_PAID"
    PAID = "PAID"
    CANCELLED = "CANCELLED"
    REFUNDED = "REFUNDED"


class HotelProperty(Base):
    __tablename__ = "hotel_properties"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name_ar: Mapped[str] = mapped_column(String(160))
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    email: Mapped[str | None] = mapped_column(String(120), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class HotelRoomType(Base):
    __tablename__ = "hotel_room_types"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    property_id: Mapped[int] = mapped_column(
        ForeignKey("hotel_properties.id", ondelete="CASCADE"), index=True, default=1
    )
    name_ar: Mapped[str] = mapped_column(String(120), index=True)
    code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    capacity_adults: Mapped[int] = mapped_column(Integer, default=2)
    capacity_children: Mapped[int] = mapped_column(Integer, default=0)
    max_occupancy: Mapped[int | None] = mapped_column(Integer, nullable=True)
    beds_description: Mapped[str | None] = mapped_column(String(200), nullable=True)
    allows_extra_bed: Mapped[bool] = mapped_column(Boolean, default=False)
    base_price: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    property: Mapped[HotelProperty] = relationship(lazy="selectin")
    rooms: Mapped[list["HotelRoom"]] = relationship(
        back_populates="room_type", lazy="selectin"
    )


class HotelServiceCatalog(Base):
    """قائمة الخدمات الإضافية للحجز (غسيل، مواصلات، …)."""

    __tablename__ = "hotel_service_catalog"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name_ar: Mapped[str] = mapped_column(String(160), index=True)
    default_price: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL"), nullable=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
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


class HotelBooking(Base):
    __tablename__ = "hotel_bookings"
    __table_args__ = (UniqueConstraint("reference", name="uq_hotel_bookings_reference"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    property_id: Mapped[int] = mapped_column(
        ForeignKey("hotel_properties.id", ondelete="RESTRICT"), index=True, default=1
    )
    reference: Mapped[str] = mapped_column(String(32), index=True)
    # رقم الفاتورة النهائية عند إقفال الحجز / المغادرة
    final_invoice_number: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    room_type_id: Mapped[int | None] = mapped_column(
        ForeignKey("hotel_room_types.id", ondelete="SET NULL"), nullable=True, index=True
    )
    room_id: Mapped[int | None] = mapped_column(
        ForeignKey("hotel_rooms.id", ondelete="SET NULL"), nullable=True, index=True
    )
    customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: حساب الشركة المرتبط (محفظة شركة منفصلة عن نزيل الفرد)
    company_customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: عقد الشركة المطبق على هذا الحجز (لقطة قواعد «من يدفع»)
    company_agreement_id: Mapped[int | None] = mapped_column(
        ForeignKey("hotel_company_agreements.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    #: من يدفع: COMPANY | GUEST
    booking_payer: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    #: تمهيد لتقسيمة الفاتورة: من يتحمل الإقامة / الخدمات والمطعم
    stay_payer: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    extras_payer: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    #: مستلم رسائل واتساب لهذا الحجز: COMPANY | GUEST1
    notify_to: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    #: آخر فترة أُرسلت فيها مطالبة مجدولة (يتوافق مع تكرار الشركة)
    notify_claim_last_period: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    #: شركة سياحة — عمولة عند إتمام الدفع
    is_tourism_agency: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0"
    )
    tourism_commission_percent: Mapped[Decimal] = mapped_column(
        Numeric(7, 3), default=Decimal("0"), server_default="0"
    )

    guest_name: Mapped[str] = mapped_column(String(160))
    guest_phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    guest_email: Mapped[str | None] = mapped_column(String(120), nullable=True)
    guest_id_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    guest_id_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    guest_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    guest_nationality: Mapped[str | None] = mapped_column(String(80), nullable=True)

    guest_type: Mapped[GuestType] = mapped_column(
        Enum(GuestType), default=GuestType.INDIVIDUAL, index=True
    )
    record_kind: Mapped[RecordKind] = mapped_column(
        Enum(RecordKind), default=RecordKind.BOOKING, index=True
    )
    quotation_status: Mapped[QuotationStatus | None] = mapped_column(
        Enum(QuotationStatus), nullable=True, index=True
    )
    company_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    company_tax_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    company_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    company_contact_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    company_contact_phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    company_contact_email: Mapped[str | None] = mapped_column(String(120), nullable=True)
    quotation_valid_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    quotation_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    check_in: Mapped[date] = mapped_column(Date, index=True)
    check_out: Mapped[date] = mapped_column(Date, index=True)
    scheduled_check_out: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    #: الموعد المجدول الأصلي للوصول (لا يُستبدل بوقت الوصول الفعلي)
    planned_check_in: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    #: أول ليلة تشغيل محتسبة (Business night) — قد تكون قبل تاريخ الوصول الفعلي
    first_chargeable_night: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    adults: Mapped[int] = mapped_column(Integer, default=1)
    children: Mapped[int] = mapped_column(Integer, default=0)

    nightly_rate: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    discount_amount: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    accommodation_total: Mapped[Decimal] = mapped_column(
        Numeric(14, 3), default=Decimal("0")
    )

    booking_status: Mapped[BookingStatus] = mapped_column(
        Enum(BookingStatus), default=BookingStatus.PENDING, index=True
    )
    payment_status: Mapped[BookingPaymentStatus] = mapped_column(
        Enum(BookingPaymentStatus), default=BookingPaymentStatus.UNPAID, index=True
    )
    source: Mapped[BookingSource] = mapped_column(
        Enum(BookingSource), default=BookingSource.RECEPTION
    )

    deposit_amount: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    paid_amount: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))

    access_token: Mapped[str] = mapped_column(
        String(64), default=lambda: uuid.uuid4().hex, index=True
    )
    cancellation_policy_id: Mapped[int | None] = mapped_column(
        ForeignKey("hotel_cancellation_policies.id", ondelete="SET NULL"), nullable=True
    )

    internal_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    checked_in_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    checked_in_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    checked_out_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    checked_out_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    #: موعد تذكير داخلي للاستقبال (متابعة مغادرة / مطالبة)
    follow_up_at: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    follow_up_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: إرسال مطالبة واتساب يومياً حتى سداد رصيد الحجز
    claim_wa_until_paid: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    room_type: Mapped[HotelRoomType | None] = relationship(lazy="selectin")
    room: Mapped["HotelRoom | None"] = relationship(
        foreign_keys=[room_id], lazy="selectin"
    )
    guests: Mapped[list["HotelBookingGuest"]] = relationship(
        back_populates="booking", cascade="all, delete-orphan", lazy="selectin"
    )
    services: Mapped[list["HotelBookingService"]] = relationship(
        back_populates="booking", cascade="all, delete-orphan", lazy="selectin"
    )
    payments: Mapped[list["HotelBookingPayment"]] = relationship(
        back_populates="booking", cascade="all, delete-orphan", lazy="selectin"
    )
    debts: Mapped[list["HotelBookingDebt"]] = relationship(
        back_populates="booking", cascade="all, delete-orphan", lazy="selectin"
    )
    room_assignments: Mapped[list["HotelBookingRoomAssignment"]] = relationship(
        back_populates="booking", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def planned_check_out(self) -> date:
        return self.scheduled_check_out or self.check_out

    @property
    def nights(self) -> int:
        start = self.first_chargeable_night or self.check_in
        n = max(0, (self.check_out - start).days)
        if n <= 0 and Decimal(str(self.accommodation_total or 0)) > 0:
            return 1
        return n

    @property
    def is_quotation(self) -> bool:
        return self.record_kind == RecordKind.QUOTATION

    @property
    def display_name(self) -> str:
        if self.guest_type == GuestType.COMPANY and self.company_name:
            return self.company_name.strip()
        return (self.guest_name or "").strip()


class HotelBookingGuest(Base):
    __tablename__ = "hotel_booking_guests"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    booking_id: Mapped[int] = mapped_column(
        ForeignKey("hotel_bookings.id", ondelete="CASCADE"), index=True
    )
    full_name: Mapped[str] = mapped_column(String(160))
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    id_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    id_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    nationality: Mapped[str | None] = mapped_column(String(80), nullable=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    id_document_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)

    booking: Mapped[HotelBooking] = relationship(back_populates="guests")


class HotelBookingRoomAssignment(Base):
    __tablename__ = "hotel_booking_room_assignments"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    booking_id: Mapped[int] = mapped_column(
        ForeignKey("hotel_bookings.id", ondelete="CASCADE"), index=True
    )
    from_room_id: Mapped[int | None] = mapped_column(
        ForeignKey("hotel_rooms.id", ondelete="SET NULL"), nullable=True
    )
    to_room_id: Mapped[int] = mapped_column(
        ForeignKey("hotel_rooms.id", ondelete="RESTRICT"), index=True
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    effective_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    price_delta: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    assigned_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    booking: Mapped[HotelBooking] = relationship(back_populates="room_assignments")
    from_room: Mapped["HotelRoom | None"] = relationship(
        foreign_keys=[from_room_id], lazy="selectin"
    )
    to_room: Mapped["HotelRoom"] = relationship(
        foreign_keys=[to_room_id], lazy="selectin"
    )


class HotelBookingService(Base):
    __tablename__ = "hotel_booking_services"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    booking_id: Mapped[int] = mapped_column(
        ForeignKey("hotel_bookings.id", ondelete="CASCADE"), index=True
    )
    name_ar: Mapped[str] = mapped_column(String(160))
    quantity: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("1"))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    line_total: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL"), nullable=True
    )
    # deferred: حتى لا يفشل selectin للحجز إن لم يُرقَّع العمود بعد على السيرفر
    sale_id: Mapped[int | None] = mapped_column(
        ForeignKey("sales.id", ondelete="SET NULL"),
        nullable=True,
        deferred=True,
    )
    added_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # False = تكلفة فندق مشمولة (إفطار) — لا تُضاف لمستحق النزيل
    # deferred: لا يفشل selectin للحجز إن لم يُرقَّع العمود بعد
    charged_to_guest: Mapped[bool] = mapped_column(
        Boolean, default=True, index=True, deferred=True
    )
    #: كود البند (ACCOMMODATION / LAUNDRY / POS_RESTAURANT…)
    service_code: Mapped[str | None] = mapped_column(
        String(40), nullable=True, deferred=True
    )
    #: COMPANY | GUEST | SHARED
    folio_side: Mapped[str | None] = mapped_column(
        String(16), nullable=True, deferred=True
    )
    company_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 3), nullable=True, deferred=True
    )
    guest_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 3), nullable=True, deferred=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    booking: Mapped[HotelBooking] = relationship(back_populates="services")


class HotelBookingPayment(Base):
    __tablename__ = "hotel_booking_payments"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    booking_id: Mapped[int] = mapped_column(
        ForeignKey("hotel_bookings.id", ondelete="CASCADE"), index=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    payment_method_id: Mapped[int | None] = mapped_column(
        ForeignKey("payment_methods.id", ondelete="SET NULL"), nullable=True
    )
    is_deposit: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # رقم إيصال القبض المطبوع عند استلام المبلغ
    receipt_number: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # ربط العملية بجلسة الاستقبال وموظف الرقم السري
    hotel_shift_id: Mapped[int | None] = mapped_column(
        ForeignKey("hotel_shifts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    received_by_employee_id: Mapped[int | None] = mapped_column(
        ForeignKey("hr_employees.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    is_refunded: Mapped[bool] = mapped_column(Boolean, default=False)

    booking: Mapped[HotelBooking] = relationship(back_populates="payments")
    # select (لا selectin): إن نقص جدول الإرجاعات على السيرفر لا يفشل تحميل كل الدفعات دفعة واحدة
    refunds: Mapped[list["HotelBookingPaymentRefund"]] = relationship(
        back_populates="payment", cascade="all, delete-orphan", lazy="select"
    )


class HotelBookingPaymentRefund(Base):
    __tablename__ = "hotel_booking_payment_refunds"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    payment_id: Mapped[int] = mapped_column(
        ForeignKey("hotel_booking_payments.id", ondelete="RESTRICT"), index=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    # وسيلة صرف الإرجاع للنزيل (كاش/مصرف) — قد تختلف عن وسيلة الدفعة الأصلية
    payment_method_id: Mapped[int | None] = mapped_column(
        ForeignKey("payment_methods.id", ondelete="SET NULL"), nullable=True, index=True
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    hotel_shift_id: Mapped[int | None] = mapped_column(
        ForeignKey("hotel_shifts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    approved_by_employee_id: Mapped[int | None] = mapped_column(
        ForeignKey("hr_employees.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    payment: Mapped[HotelBookingPayment] = relationship(back_populates="refunds")


class HotelBookingDebtStatus(str, enum.Enum):
    OPEN = "OPEN"
    COLLECTED = "COLLECTED"
    WRITTEN_OFF = "WRITTEN_OFF"


class HotelBookingDebt(Base):
    """دين حجز — يُنشأ عند المغادرة مع متبقٍ غير مدفوع."""

    __tablename__ = "hotel_booking_debts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    booking_id: Mapped[int] = mapped_column(
        ForeignKey("hotel_bookings.id", ondelete="CASCADE"), index=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    amount_remaining: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 3), nullable=True
    )
    status: Mapped[HotelBookingDebtStatus] = mapped_column(
        Enum(
            HotelBookingDebtStatus,
            values_callable=lambda obj: [e.value for e in obj],
            native_enum=False,
            length=20,
        ),
        default=HotelBookingDebtStatus.OPEN,
        index=True,
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    follow_up_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    reminder_at: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    #: وقت تذكير المطالبة (HH:MM) — مع reminder_at
    reminder_time: Mapped[str | None] = mapped_column(String(8), nullable=True)
    settlement_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    collected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    collected_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    collection_payment_id: Mapped[int | None] = mapped_column(
        ForeignKey("hotel_booking_payments.id", ondelete="SET NULL"), nullable=True
    )
    written_off_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    written_off_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    write_off_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    booking: Mapped[HotelBooking] = relationship(back_populates="debts")
    collection_payment: Mapped["HotelBookingPayment | None"] = relationship(
        foreign_keys=[collection_payment_id], lazy="selectin"
    )


class HotelInvoice(Base):
    __tablename__ = "hotel_invoices"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    booking_id: Mapped[int] = mapped_column(
        ForeignKey("hotel_bookings.id", ondelete="RESTRICT"), index=True
    )
    invoice_number: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[HotelInvoiceStatus] = mapped_column(
        Enum(
            HotelInvoiceStatus,
            values_callable=lambda obj: [e.value for e in obj],
            native_enum=False,
            length=20,
        ),
        default=HotelInvoiceStatus.DRAFT,
        index=True,
    )
    subtotal: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    discount: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    tax: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    total: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    paid: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    issued_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    issued_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    items: Mapped[list["HotelInvoiceItem"]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan", lazy="selectin"
    )


class HotelInvoiceItem(Base):
    __tablename__ = "hotel_invoice_items"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    invoice_id: Mapped[int] = mapped_column(
        ForeignKey("hotel_invoices.id", ondelete="CASCADE"), index=True
    )
    description: Mapped[str] = mapped_column(String(255))
    quantity: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("1"))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    line_total: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    item_type: Mapped[str] = mapped_column(String(40), default="OTHER")

    invoice: Mapped[HotelInvoice] = relationship(back_populates="items")


class HotelBookingStatusLog(Base):
    __tablename__ = "hotel_booking_status_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    booking_id: Mapped[int] = mapped_column(
        ForeignKey("hotel_bookings.id", ondelete="CASCADE"), index=True
    )
    from_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_status: Mapped[str] = mapped_column(String(32))
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class HotelRoomStatusLog(Base):
    __tablename__ = "hotel_room_status_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    room_id: Mapped[int] = mapped_column(
        ForeignKey("hotel_rooms.id", ondelete="CASCADE"), index=True
    )
    from_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_status: Mapped[str] = mapped_column(String(32))
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class HotelAuditLog(Base):
    __tablename__ = "hotel_audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    entity_type: Mapped[str] = mapped_column(String(40), index=True)
    entity_id: Mapped[int] = mapped_column(index=True)
    action: Mapped[str] = mapped_column(String(64))
    field_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )


class HotelCancellationPolicy(Base):
    __tablename__ = "hotel_cancellation_policies"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    property_id: Mapped[int] = mapped_column(
        ForeignKey("hotel_properties.id", ondelete="CASCADE"), default=1
    )
    name_ar: Mapped[str] = mapped_column(String(120))
    hours_before_free: Mapped[int] = mapped_column(Integer, default=48)
    penalty_percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("0"))
    no_show_nights_penalty: Mapped[int] = mapped_column(Integer, default=1)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class HotelRatePlan(Base):
    __tablename__ = "hotel_rate_plans"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    property_id: Mapped[int] = mapped_column(
        ForeignKey("hotel_properties.id", ondelete="CASCADE"), default=1
    )
    room_type_id: Mapped[int] = mapped_column(
        ForeignKey("hotel_room_types.id", ondelete="CASCADE"), index=True
    )
    name_ar: Mapped[str] = mapped_column(String(120))
    base_price: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class HotelPricingRule(Base):
    __tablename__ = "hotel_pricing_rules"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    rate_plan_id: Mapped[int] = mapped_column(
        ForeignKey("hotel_rate_plans.id", ondelete="CASCADE"), index=True
    )
    day_of_week: Mapped[int | None] = mapped_column(Integer, nullable=True)
    date_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    date_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    price: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class HotelDailyClosing(Base):
    __tablename__ = "hotel_daily_closings"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    property_id: Mapped[int] = mapped_column(
        ForeignKey("hotel_properties.id", ondelete="RESTRICT"), default=1
    )
    closing_date: Mapped[date] = mapped_column(Date, index=True)
    bookings_new: Mapped[int] = mapped_column(Integer, default=0)
    check_ins: Mapped[int] = mapped_column(Integer, default=0)
    check_outs: Mapped[int] = mapped_column(Integer, default=0)
    cancellations: Mapped[int] = mapped_column(Integer, default=0)
    no_shows: Mapped[int] = mapped_column(Integer, default=0)
    revenue_total: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    payments_cash: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    payments_card: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    payments_other: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    deposits_total: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    gl_backfilled_payments: Mapped[int] = mapped_column(Integer, default=0)
    gl_backfilled_refunds: Mapped[int] = mapped_column(Integer, default=0)
    gl_operational_net: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    gl_revenue_net: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    gl_gap: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    closed_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    closed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class HotelSecurityAuthority(Base):
    """جهة أمنية تستلم تقارير النزلاء."""

    __tablename__ = "hotel_security_authorities"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name_ar: Mapped[str] = mapped_column(String(160))
    contact_channel: Mapped[SecurityContactChannel] = mapped_column(
        Enum(SecurityContactChannel), default=SecurityContactChannel.WHATSAPP, index=True
    )
    contact_value: Mapped[str] = mapped_column(String(200))
    guest_scope: Mapped[SecurityGuestScope] = mapped_column(
        Enum(SecurityGuestScope), default=SecurityGuestScope.BOTH, index=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
