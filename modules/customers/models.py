"""Customer & loyalty models.

العملاء يعرَّفون برقم الهاتف (مفتاح فريد). الاسم اختياري.
نقاط الولاء تُجمَع تلقائياً عند الدفع، ويمكن استخدامها كخصم لاحقاً.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    Boolean,
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


class CustomerType(str, enum.Enum):
    INDIVIDUAL = "INDIVIDUAL"
    COMPANY = "COMPANY"


class CustomerBusinessDomain(str, enum.Enum):
    """فصل عملاء المطعم عن نزلاء الفندق (ومشترك إن تعامل مع الاثنين)."""

    RESTAURANT = "restaurant"
    HOTEL = "hotel"
    SHARED = "shared"


class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (UniqueConstraint("phone", name="uq_customers_phone"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    phone: Mapped[str] = mapped_column(String(40), index=True)
    name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    email: Mapped[str | None] = mapped_column(String(160), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    customer_type: Mapped[CustomerType] = mapped_column(
        Enum(CustomerType), default=CustomerType.INDIVIDUAL, index=True
    )
    company_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: فرد تابع لحساب شركة (محفظة الشركة منفصلة)
    parent_company_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: نسبة خصم تلقائية لحجوزات الشركة (0–100)
    company_discount_percent: Mapped[Decimal] = mapped_column(
        Numeric(7, 3), default=Decimal("0"), server_default="0"
    )
    #: أقصى مديونية مسموحة على محفظة الشركة (بالسالب حتى هذا الحد)
    company_credit_limit: Mapped[Decimal] = mapped_column(
        Numeric(14, 3), default=Decimal("0"), server_default="0"
    )
    #: السماح بنزول محفظة الشركة للأحمر (دين) ضمن حد الائتمان
    allow_company_credit: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0"
    )
    #: تكرار إشعارات/مطالبات الشركة: DAILY|WEEKLY|MONTHLY|SUMMARY_*
    company_notify_frequency: Mapped[str] = mapped_column(
        String(32), default="DAILY", server_default="DAILY"
    )
    #: مستلم الإشعار الافتراضي: COMPANY | GUEST1
    company_default_notify_to: Mapped[str] = mapped_column(
        String(16), default="COMPANY", server_default="COMPANY"
    )
    #: آخر فترة أُرسل فيها ملخص شركة (مفتاح W2026-31 / M2026-08 / D…)
    company_notify_last_period: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    company_notify_last_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    business_domain: Mapped[CustomerBusinessDomain] = mapped_column(
        Enum(
            CustomerBusinessDomain,
            values_callable=lambda obj: [e.value for e in obj],
            native_enum=False,
            length=20,
        ),
        default=CustomerBusinessDomain.RESTAURANT,
        server_default=CustomerBusinessDomain.RESTAURANT.value,
        index=True,
    )

    points_balance: Mapped[Decimal] = mapped_column(
        Numeric(14, 3), default=Decimal("0")
    )
    wallet_balance: Mapped[Decimal] = mapped_column(
        Numeric(14, 3), default=Decimal("0"), server_default="0"
    )
    total_spent: Mapped[Decimal] = mapped_column(
        Numeric(14, 3), default=Decimal("0")
    )
    visits_count: Mapped[int] = mapped_column(Integer, default=0)
    last_visit_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    referral_code: Mapped[str | None] = mapped_column(
        String(32), unique=True, nullable=True, index=True
    )
    loyalty_intro_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class ReferralEvent(Base):
    """إحالة ناجحة — نقاط للمحيل والمشتري."""

    __tablename__ = "referral_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    referrer_customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), index=True
    )
    buyer_customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), index=True
    )
    sale_id: Mapped[int] = mapped_column(
        ForeignKey("sales.id", ondelete="CASCADE"), unique=True, index=True
    )
    referral_code: Mapped[str] = mapped_column(String(32))
    referrer_points: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    buyer_points: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )


class LoyaltyTxnKind(str, enum.Enum):
    EARN = "EARN"          # كسب نقاط من فاتورة
    REDEEM = "REDEEM"      # استخدام نقاط كخصم
    ADJUST = "ADJUST"      # تعديل يدوي من الأدمن


class LoyaltyTransaction(Base):
    """سجل تاريخي لكل حركة على نقاط العميل (كسب/استخدام/تعديل)."""

    __tablename__ = "loyalty_transactions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), index=True
    )
    sale_id: Mapped[int | None] = mapped_column(
        ForeignKey("sales.id", ondelete="SET NULL"), nullable=True, index=True
    )
    kind: Mapped[LoyaltyTxnKind] = mapped_column(
        Enum(LoyaltyTxnKind), default=LoyaltyTxnKind.EARN, index=True
    )
    points: Mapped[Decimal] = mapped_column(
        Numeric(14, 3), default=Decimal("0")
    )  # موجبة للكسب، سالبة للاستخدام
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    customer: Mapped[Customer] = relationship(lazy="selectin")


class WalletTxnKind(str, enum.Enum):
    TOPUP = "TOPUP"
    SPEND = "SPEND"
    ADJUST = "ADJUST"


class CustomerWalletTransaction(Base):
    """سجل حركات محفظة العميل (رصيد نقدي)."""

    __tablename__ = "customer_wallet_transactions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[WalletTxnKind] = mapped_column(
        Enum(WalletTxnKind), default=WalletTxnKind.ADJUST, index=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    customer: Mapped[Customer] = relationship(lazy="selectin")
