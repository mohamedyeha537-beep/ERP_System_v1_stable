from __future__ import annotations

import enum
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import DateTime, Enum, ForeignKey, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from infra.db import Base


class SaleStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class SaleSource(str, enum.Enum):
    POS = "POS"
    ONLINE = "ONLINE"


class SaleContext(str, enum.Enum):
    """سياق البيع لتمييز نوع الطلب وإجراء الـ checkout المناسب."""

    TABLE = "TABLE"        # طاولة داخل المحل (افتراضي للمطعم/المقهى)
    ROOM = "ROOM"          # شقة/غرفة فندق — حساب مؤجل
    EXTERNAL = "EXTERNAL"  # طلب خارجي/توصيل لعميل (هاتف)


class ExternalOrderType(str, enum.Enum):
    PICKUP = "PICKUP"
    DELIVERY = "DELIVERY"


class Sale(Base):
    __tablename__ = "sales"
    __table_args__ = (
        UniqueConstraint("external_order_id", name="uq_sales_external_order_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    status: Mapped[SaleStatus] = mapped_column(Enum(SaleStatus), default=SaleStatus.DRAFT, index=True)
    total: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    source: Mapped[SaleSource] = mapped_column(Enum(SaleSource), default=SaleSource.POS)
    external_order_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    pos_shift_id: Mapped[int | None] = mapped_column(
        ForeignKey("pos_shifts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    table_id: Mapped[int | None] = mapped_column(
        ForeignKey("dining_tables.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # السياق الجديد: TABLE/ROOM/EXTERNAL
    context_type: Mapped[SaleContext] = mapped_column(
        Enum(SaleContext), default=SaleContext.TABLE, index=True
    )
    customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    booking_id: Mapped[int | None] = mapped_column(
        ForeignKey("hotel_bookings.id", ondelete="SET NULL"), nullable=True, index=True
    )
    external_order_type: Mapped[ExternalOrderType] = mapped_column(
        Enum(ExternalOrderType), default=ExternalOrderType.PICKUP
    )
    delivery_zone_id: Mapped[int | None] = mapped_column(
        ForeignKey("delivery_zones.id", ondelete="SET NULL"), nullable=True, index=True
    )
    delivery_zone_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    delivery_fee: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))

    sent_to_kitchen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    served_to_customer_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    referral_code_used: Mapped[str | None] = mapped_column(String(32), nullable=True)
    referrer_customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True
    )

    lines: Mapped[list["SaleLine"]] = relationship(
        back_populates="sale", cascade="all, delete-orphan", lazy="selectin"
    )
    table = relationship("DiningTable", foreign_keys=[table_id])
    customer = relationship("Customer", foreign_keys=[customer_id])


class SaleLine(Base):
    __tablename__ = "sale_lines"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    sale_id: Mapped[int] = mapped_column(ForeignKey("sales.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"))
    quantity: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    line_total: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    line_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    kitchen_sent_qty: Mapped[Decimal] = mapped_column(
        Numeric(14, 4), default=Decimal("0")
    )

    sale: Mapped[Sale] = relationship(back_populates="lines")
    product = relationship("Product", foreign_keys=[product_id])


class TicketStatus(str, enum.Enum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    READY = "READY"
    SERVED = "SERVED"
    CANCELLED = "CANCELLED"


class KitchenTicket(Base):
    """تذكرة طلب لقسم محدّد. تُنشأ عند إتمام البيع لكل قسم له توجيه نشط."""

    __tablename__ = "kitchen_tickets"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    sale_id: Mapped[int] = mapped_column(
        ForeignKey("sales.id", ondelete="CASCADE"), index=True
    )
    root_category_id: Mapped[int | None] = mapped_column(
        ForeignKey("product_categories.id", ondelete="CASCADE"), nullable=True, index=True
    )
    kitchen_department_id: Mapped[int | None] = mapped_column(
        ForeignKey("kitchen_departments.id", ondelete="CASCADE"), nullable=True, index=True
    )
    kitchen_section_id: Mapped[int | None] = mapped_column(
        ForeignKey("kitchen_sections.id", ondelete="CASCADE"), nullable=True, index=True
    )
    status: Mapped[TicketStatus] = mapped_column(
        Enum(TicketStatus), default=TicketStatus.PENDING, index=True
    )
    routing_mode: Mapped[str] = mapped_column(String(20), default="SCREEN")
    delivery_status: Mapped[str] = mapped_column(String(40), default="PENDING")
    delivery_info: Mapped[str | None] = mapped_column(String(500), nullable=True)
    is_supplement: Mapped[bool] = mapped_column(default=False)
    supplement_lines_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    served_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    sale = relationship("Sale", foreign_keys=[sale_id])
    root_category = relationship(
        "ProductCategory", foreign_keys=[root_category_id]
    )
    kitchen_department = relationship(
        "KitchenDepartment", foreign_keys=[kitchen_department_id]
    )
    kitchen_section = relationship(
        "KitchenSection", foreign_keys=[kitchen_section_id]
    )