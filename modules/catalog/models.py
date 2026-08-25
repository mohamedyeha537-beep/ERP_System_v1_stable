from __future__ import annotations

import enum
from datetime import date, datetime
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


class ProductKind(str, enum.Enum):
    FINAL_SELLABLE = "FINAL_SELLABLE"
    STOCK_ONLY = "STOCK_ONLY"


class CategoryRouting(str, enum.Enum):
    """طريقة إرسال طلبات هذا القسم عند إتمام الفاتورة."""

    NONE = "NONE"  # لا توجيه
    SCREEN = "SCREEN"  # شاشة المطبخ KDS
    WHATSAPP = "WHATSAPP"  # رسالة واتساب عبر webhook
    PRINT = "PRINT"  # تذكرة طباعة منفصلة


class ProductCategory(Base):
    """فئة رئيسية (parent_id فارغ) أو فرعية. للفئة الجذر أيضاً إعدادات توجيه (KDS/واتس/طباعة)."""

    __tablename__ = "product_categories"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name_ar: Mapped[str] = mapped_column(String(120))
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("product_categories.id", ondelete="CASCADE"), nullable=True, index=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    color_hex: Mapped[str] = mapped_column(String(7), default="#3b82f6")
    delete_protected: Mapped[bool] = mapped_column(default=False)
    show_in_pos: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    """إن كانت False لا تظهر الفئة ولا أصنافها في جلسة البيع (مثل الخضروات/البهارات)."""
    show_in_shop: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    """إن كانت False لا تظهر الفئة ولا أصنافها في المتجر الإلكتروني."""
    # توجيه طلبات القسم (يُطبَّق فعلياً على الفئة الجذر فقط)
    routing_mode: Mapped[CategoryRouting] = mapped_column(
        Enum(CategoryRouting), default=CategoryRouting.NONE
    )
    routing_target: Mapped[str | None] = mapped_column(String(255), nullable=True)
    """عنوان التوجيه: لـ WhatsApp = رقم/webhook، للطباعة = اسم الطابعة، لـ SCREEN = اختياري."""
    kitchen_section_id: Mapped[int | None] = mapped_column(
        ForeignKey("kitchen_sections.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
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

    parent: Mapped[ProductCategory | None] = relationship(
        remote_side=[id],
        back_populates="children",
    )
    children: Mapped[list[ProductCategory]] = relationship(
        back_populates="parent", cascade="all, delete-orphan", order_by="ProductCategory.sort_order"
    )
    products: Mapped[list["Product"]] = relationship(back_populates="category")


class DiningTable(Base):
    """طاولة في المطعم/المقهى (يمكن ربط الفاتورة بها لتنظيم الطلبات)."""

    __tablename__ = "dining_tables"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name_ar: Mapped[str] = mapped_column(String(80), unique=True)
    section_category_id: Mapped[int | None] = mapped_column(
        ForeignKey("product_categories.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    capacity: Mapped[int] = mapped_column(Integer, default=4)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(default=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    section = relationship("ProductCategory", foreign_keys=[section_category_id])


class ProductUnit(Base):
    __tablename__ = "product_units"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name_ar: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name_ar: Mapped[str] = mapped_column(String(255))
    sku: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    barcode: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    unit: Mapped[str] = mapped_column(String(32), default="قطعة")
    kind: Mapped[ProductKind] = mapped_column(Enum(ProductKind), index=True)
    sell_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    reorder_level: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    is_active: Mapped[bool] = mapped_column(default=True)
    show_in_pos: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    show_in_shop: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    """إن كانت False لا يظهر الصنف في المتجر الإلكتروني (مستقل عن جلسة البيع)."""
    direct_purchase_enabled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    """منتج نهائي قابل للشراء كصنف مباشر وإضافته للمخزون."""
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("product_categories.id", ondelete="SET NULL"), nullable=True, index=True
    )
    sales_warehouse_id: Mapped[int | None] = mapped_column(
        ForeignKey("warehouses.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    kitchen_department_id: Mapped[int | None] = mapped_column(
        ForeignKey("kitchen_departments.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    kitchen_section_id: Mapped[int | None] = mapped_column(
        ForeignKey("kitchen_sections.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    image_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: وجبة إفطار مشمولة — تسويتها تكلفة فندق وليست على حساب النزيل
    is_hotel_breakfast: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    line_modifier_presets: Mapped[str | None] = mapped_column(Text, nullable=True)
    expiry_tracked: Mapped[bool] = mapped_column(Boolean, default=False)
    expiry_production_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    expiry_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    expiry_warn_days: Mapped[int] = mapped_column(Integer, default=7)
    price_linked_to_bom: Mapped[bool] = mapped_column(Boolean, default=False)
    bom_markup_pct: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    #: تكلفة مرجعية يدوية للمكوّن المخزني — تُستخدم في التركيبة إن وُجدت.
    reference_unit_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    # حقول SEO (وكلاء خارجيون / SEO Center)
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

    category: Mapped[ProductCategory | None] = relationship(back_populates="products")
    sales_warehouse = relationship("Warehouse", foreign_keys=[sales_warehouse_id])
    kitchen_department: Mapped["KitchenDepartment | None"] = relationship(  # noqa: F821
        back_populates="products",
        foreign_keys=[kitchen_department_id],
    )
    bom_lines_as_parent: Mapped[list["BillOfMaterialsLine"]] = relationship(
        foreign_keys="BillOfMaterialsLine.parent_product_id",
        back_populates="parent_product",
        cascade="all, delete-orphan",
    )


class BillOfMaterialsLine(Base):
    __tablename__ = "bom_lines"
    __table_args__ = (UniqueConstraint("parent_product_id", "component_product_id", name="uq_bom_parent_component"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    parent_product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    component_product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    qty_per_parent: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("1"))
    packaging_only: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    """مكوّن تغليف — يُخصم ويُحسب في التقارير لطلبات أونلاين/استلام/توصيل فقط."""

    parent_product: Mapped[Product] = relationship(
        foreign_keys=[parent_product_id], back_populates="bom_lines_as_parent"
    )
    component_product: Mapped[Product] = relationship(foreign_keys=[component_product_id])
