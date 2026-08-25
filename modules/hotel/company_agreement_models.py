"""عقود الشركات: من يتحمّل كل بند خدمة (مسودة PMS / PDF «من سيدفع»).

السقف الائتماني الإجمالي: مصدر وحيد على Customer
  (company_credit_limit + allow_company_credit) عبر company_credit.py
لا يُخزَّن سقف ائتمان ثانٍ على العقد — يُقرأ من ملف الشركة دائماً.
"""
from __future__ import annotations

import enum
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from infra.db import Base


class AgreementType(str, enum.Enum):
    """نوع عقد الشركة."""

    STANDARD = "STANDARD"  # شركة تتحمّل بنوداً حسب الجدول
    BOOKING_ONLY = "BOOKING_ONLY"  # حجز فقط — كل التكاليف على النزيل
    COMMISSION = "COMMISSION"  # عمولة (سياحة/وكالات)


class Bearer(str, enum.Enum):
    COMPANY = "COMPANY"
    GUEST = "GUEST"
    SHARED = "SHARED"  # شركة حتى الحد ثم نزيل


# أكواد بنود معيارية (قابلة للتوسع)
SERVICE_CATALOG: list[tuple[str, str, str]] = [
    ("ACCOMMODATION", "الإقامة", "COMPANY"),
    ("BREAKFAST", "الإفطار", "COMPANY"),
    ("LUNCH", "الغداء", "GUEST"),
    ("DINNER", "العشاء", "GUEST"),
    ("LAUNDRY", "المغسلة", "GUEST"),
    ("MINI_BAR", "الميني بار", "GUEST"),
    ("DAMAGE", "التلفيات", "GUEST"),
    ("LATE_CHECKOUT", "المغادرة المتأخرة", "GUEST"),
    ("EARLY_CHECKIN", "الوصول المبكر", "GUEST"),
    ("INTERNET", "الإنترنت", "COMPANY"),
    ("TRANSPORT", "المواصلات", "GUEST"),
    ("AIRPORT_PICKUP", "استقبال المطار", "GUEST"),
    ("EXTRA_BED", "سرير إضافي", "GUEST"),
    ("CONFERENCE", "قاعة مؤتمرات", "COMPANY"),
    ("PRINTING", "طباعة", "GUEST"),
    ("POS_RESTAURANT", "مطعم / مقهى (POS)", "GUEST"),
    ("OTHER", "أخرى", "GUEST"),
]


class CompanyAgreement(Base):
    """عقد شركة — سياسة «من يدفع ماذا»."""

    __tablename__ = "hotel_company_agreements"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    company_customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(160), default="العقد الافتراضي")
    agreement_type: Mapped[str] = mapped_column(
        String(32), default=AgreementType.STANDARD.value, index=True
    )
    #: طريقة السداد الوصفية (آجل 30 يوم…) — للعرض والتقارير
    payment_terms: Mapped[str | None] = mapped_column(String(80), nullable=True)
    currency: Mapped[str] = mapped_column(String(8), default="LYD", server_default="LYD")
    #: عمولة: نسبة % و/أو مبلغ ثابت (لعقد COMMISSION)
    commission_percent: Mapped[Decimal] = mapped_column(
        Numeric(7, 3), default=Decimal("0"), server_default="0"
    )
    commission_fixed: Mapped[Decimal] = mapped_column(
        Numeric(14, 3), default=Decimal("0"), server_default="0"
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    services: Mapped[list["CompanyAgreementService"]] = relationship(
        back_populates="agreement",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class CompanyAgreementService(Base):
    """بند في عقد الشركة: من يتحمّله + حد اختياري."""

    __tablename__ = "hotel_company_agreement_services"
    __table_args__ = (
        UniqueConstraint(
            "agreement_id", "service_code", name="uq_agreement_service_code"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    agreement_id: Mapped[int] = mapped_column(
        ForeignKey("hotel_company_agreements.id", ondelete="CASCADE"), index=True
    )
    service_code: Mapped[str] = mapped_column(String(40), index=True)
    name_ar: Mapped[str] = mapped_column(String(160))
    bearer: Mapped[str] = mapped_column(String(16), default=Bearer.GUEST.value)
    #: حد مبلغ تتحمّله الشركة (SHARED أو COMPANY مع سقف بند)
    limit_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    #: حد كمية (مثلاً إفطار يومي) — اختياري
    limit_quantity: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    agreement: Mapped[CompanyAgreement] = relationship(back_populates="services")


class AgreementChangeStatus(str, enum.Enum):
    PENDING = "PENDING"
    APPLIED = "APPLIED"
    REJECTED = "REJECTED"


class CompanyAgreementChangeRequest(Base):
    """طلب استقبال لتعديل بنود عقد شركة — ينفّذه الأدمن من بطاقة الشركة."""

    __tablename__ = "hotel_company_agreement_change_requests"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    company_customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), index=True
    )
    agreement_id: Mapped[int | None] = mapped_column(
        ForeignKey("hotel_company_agreements.id", ondelete="SET NULL"), nullable=True
    )
    booking_id: Mapped[int | None] = mapped_column(
        ForeignKey("hotel_bookings.id", ondelete="SET NULL"), nullable=True, index=True
    )
    requested_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(16), default=AgreementChangeStatus.PENDING.value, index=True
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: مسار نسبي تحت static/uploads — صورة أو PDF لطلب الشركة الكتابي
    attachment_path: Mapped[str | None] = mapped_column(String(260), nullable=True)
    attachment_name: Mapped[str | None] = mapped_column(String(180), nullable=True)
    #: JSON: [{code, name_ar, from_bearer, to_bearer}]
    items_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reviewed_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)


class BookingServiceRule(Base):
    """لقطة قواعد «من يدفع» على الحجز (من العقد عند الإنشاء — قابلة للتعديل)."""

    __tablename__ = "hotel_booking_service_rules"
    __table_args__ = (
        UniqueConstraint(
            "booking_id", "service_code", name="uq_booking_service_rule_code"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    booking_id: Mapped[int] = mapped_column(
        ForeignKey("hotel_bookings.id", ondelete="CASCADE"), index=True
    )
    service_code: Mapped[str] = mapped_column(String(40), index=True)
    name_ar: Mapped[str] = mapped_column(String(160))
    bearer: Mapped[str] = mapped_column(String(16), default=Bearer.GUEST.value)
    limit_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    limit_quantity: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    company_used_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 3), default=Decimal("0"), server_default="0"
    )
    company_used_qty: Mapped[Decimal] = mapped_column(
        Numeric(14, 4), default=Decimal("0"), server_default="0"
    )
