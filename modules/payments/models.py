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
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from infra.db import Base


class PaymentMethodKind(str, enum.Enum):
    CASH = "CASH"
    BANK = "BANK"
    OTHER = "OTHER"


class PaymentMethod(Base):
    __tablename__ = "payment_methods"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name_ar: Mapped[str] = mapped_column(String(120), unique=True)
    kind: Mapped[PaymentMethodKind] = mapped_column(
        Enum(PaymentMethodKind), default=PaymentMethodKind.BANK
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    #: قبض من الزبائن في نقطة البيع فقط — لا يعني استلام تحويلات.
    can_receive: Mapped[bool] = mapped_column(Boolean, default=True)
    #: صرف للموردين والمصروفات والتحويل الصادر (بما فيها إيداع من حساب المالك).
    can_pay: Mapped[bool] = mapped_column(Boolean, default=True)
    #: استلام تحويلات من حسابات أخرى — منفصل عن نقطة البيع.
    can_fund: Mapped[bool] = mapped_column(Boolean, default=False)
    #: حساب نظامي لا يُحذف (ذمم مورد، سحوبات مالك).
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)
    #: إظهار بطاقة الرصيد في لوحة «الخزينة والذمم».
    show_on_dashboard: Mapped[bool] = mapped_column(Boolean, default=True)


SUPPLIER_CREDIT_PM_NAME = "ذمم دائن — مورد (آجل)"
OWNER_EQUITY_PM_NAME = "حساب المالك — حقوق الملكية"
# أسماء قديمة — تُدمَّج تلقائياً في حساب المالك الموحّد
LEGACY_OWNER_DRAW_PM_NAME = "سحوبات المالك — حقوق الملكية"
LEGACY_OWNER_CAPITAL_PM_NAME = "إيداعات المالك — حقوق الملكية"


class PaymentTransferType(str, enum.Enum):
    REFUND_SETTLEMENT = "REFUND_SETTLEMENT"
    MANUAL = "MANUAL"
    OWNER_DRAW = "OWNER_DRAW"
    OWNER_CAPITAL = "OWNER_CAPITAL"


class SalePayment(Base):
    """دفعة فاتورة بيع — حالياً 1:1 مع الفاتورة، قابلة للتوسيع للدفع المنقسم."""

    __tablename__ = "sale_payments"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    sale_id: Mapped[int] = mapped_column(
        ForeignKey("sales.id", ondelete="CASCADE"), index=True
    )
    payment_method_id: Mapped[int] = mapped_column(
        ForeignKey("payment_methods.id", ondelete="RESTRICT")
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    payment_proof_image_filename: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    method: Mapped[PaymentMethod] = relationship()


class PurchasePayment(Base):
    """دفعة على فاتورة شراء — الخروج الفعلي من المحفظة للمورد."""

    __tablename__ = "purchase_payments"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    purchase_id: Mapped[int] = mapped_column(
        ForeignKey("purchases.id", ondelete="CASCADE"), index=True
    )
    payment_method_id: Mapped[int] = mapped_column(
        ForeignKey("payment_methods.id", ondelete="RESTRICT"), index=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    payment_proof_image_filename: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    method: Mapped[PaymentMethod] = relationship()
    purchase: Mapped["Purchase"] = relationship(back_populates="payments")


class RefundPayment(Base):
    __tablename__ = "refund_payments"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    sale_return_id: Mapped[int] = mapped_column(
        ForeignKey("sale_returns.id", ondelete="CASCADE"),
        index=True,
        unique=True,
    )
    payment_method_id: Mapped[int] = mapped_column(
        ForeignKey("payment_methods.id", ondelete="RESTRICT")
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    method: Mapped[PaymentMethod] = relationship()


class PaymentTransfer(Base):
    __tablename__ = "payment_transfers"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    transfer_type: Mapped[PaymentTransferType] = mapped_column(
        Enum(PaymentTransferType),
        default=PaymentTransferType.MANUAL,
        index=True,
    )
    sale_return_id: Mapped[int | None] = mapped_column(
        ForeignKey("sale_returns.id", ondelete="CASCADE"),
        index=True,
        nullable=True,
    )
    from_payment_method_id: Mapped[int] = mapped_column(
        ForeignKey("payment_methods.id", ondelete="RESTRICT"),
        index=True,
    )
    to_payment_method_id: Mapped[int] = mapped_column(
        ForeignKey("payment_methods.id", ondelete="RESTRICT"),
        index=True,
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    from_method: Mapped[PaymentMethod] = relationship(
        foreign_keys=[from_payment_method_id]
    )
    to_method: Mapped[PaymentMethod] = relationship(
        foreign_keys=[to_payment_method_id]
    )


class PurchaseKind(str, enum.Enum):
    INVENTORY = "INVENTORY"  # فاتورة شراء بضاعة (بنود تضاف للمخزون)
    EXPENSE = "EXPENSE"  # مصروف (إيجار/راتب/فاتورة...) — لا تأثير على المخزون
    ASSET = "ASSET"  # أصول/أدوات تستهلك في الشركة (بنود حرّة، لا تباع)


class Purchase(Base):
    """عملية صرف من المحفظة: قد تكون فاتورة شراء بضاعة (لها بنود) أو مصروفاً عاماً (مبلغ فقط)."""

    __tablename__ = "purchases"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    payment_method_id: Mapped[int] = mapped_column(
        ForeignKey("payment_methods.id", ondelete="RESTRICT"), index=True
    )
    kind: Mapped[PurchaseKind] = mapped_column(
        Enum(PurchaseKind), default=PurchaseKind.EXPENSE, index=True
    )
    supplier: Mapped[str | None] = mapped_column(String(160), nullable=True)
    #: رقم أو مرجع فاتورة المورّد الخارجية (للتوثيق والمطابقة).
    supplier_invoice_ref: Mapped[str | None] = mapped_column(String(120), nullable=True)
    #: مسار نسبي تحت static — صورة فاتورة المورّد (مثل uploads/purchases/supplier_invoices/…).
    invoice_image_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: عند الدفع من مصرف: إيصال/صورة إثبات التحويل (uploads/purchases/payment_receipts/…).
    payment_proof_image_filename: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    expense_category: Mapped[str | None] = mapped_column(String(80), nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    warehouse_id: Mapped[int | None] = mapped_column(
        ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=True, index=True
    )

    method: Mapped[PaymentMethod] = relationship()
    warehouse = relationship("Warehouse", foreign_keys=[warehouse_id])
    lines: Mapped[list["PurchaseLine"]] = relationship(
        back_populates="purchase",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    payments: Mapped[list["PurchasePayment"]] = relationship(
        back_populates="purchase",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class PurchaseLine(Base):
    """بند فاتورة شراء.
    - INVENTORY: product_id مطلوب (ينعكس على المخزون).
    - ASSET: item_name مطلوب (نص حر) ولا تأثير على المخزون.
        * useful_life_months = 0 / NULL → بند استهلاكي (يخصم بالكامل في شهر الشراء).
        * useful_life_months > 0 → أصل ثابت (يُهلَك على فترة العمر الإنتاجي بطريقة القسط الثابت).
    - EXPENSE: لا يستخدم بنوداً (يُحفظ كمبلغ فقط).
    """

    __tablename__ = "purchase_lines"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    purchase_id: Mapped[int] = mapped_column(
        ForeignKey("purchases.id", ondelete="CASCADE"), index=True
    )
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=True
    )
    item_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    unit: Mapped[str | None] = mapped_column(String(32), nullable=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    line_total: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    # حقول الإهلاك (للأصول الثابتة فقط)
    useful_life_months: Mapped[int] = mapped_column(Integer, default=0)
    salvage_value: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    disposal_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    purchase: Mapped[Purchase] = relationship(back_populates="lines")
    product = relationship("Product", foreign_keys=[product_id])

    @property
    def display_name(self) -> str:
        if self.item_name:
            return self.item_name
        if self.product is not None:
            return self.product.name_ar
        return f"#{self.product_id or '?'}"

    @property
    def is_fixed_asset(self) -> bool:
        return bool(self.useful_life_months and int(self.useful_life_months) > 0)

    @property
    def depreciable_base(self) -> Decimal:
        """الكلفة القابلة للإهلاك = الإجمالي − قيمة الخردة (لا يقل عن صفر)."""
        base = (self.line_total or Decimal("0")) - (self.salvage_value or Decimal("0"))
        return base if base > 0 else Decimal("0")

    @property
    def monthly_depreciation(self) -> Decimal:
        """قسط الإهلاك الشهري بطريقة القسط الثابت (Straight-Line)."""
        if not self.is_fixed_asset:
            return Decimal("0")
        return (self.depreciable_base / Decimal(int(self.useful_life_months))).quantize(
            Decimal("0.001")
        )


class RecurringCostCategory(str, enum.Enum):
    """تصنيف التكلفة الشهرية الثابتة لتحليل التعادل (CVP)."""

    SALARY = "SALARY"  # رواتب وأجور
    RENT = "RENT"  # إيجار المحل
    UTILITY = "UTILITY"  # كهرباء/ماء/غاز/إنترنت
    SUBSCRIPTION = "SUBSCRIPTION"  # اشتراكات (POS، استضافة، تأمين)
    INSURANCE = "INSURANCE"  # تأمين
    LICENSE = "LICENSE"  # تراخيص ورسوم دورية
    OTHER = "OTHER"  # أخرى


class RecurringCost(Base):
    """تكلفة شهرية ثابتة (Fixed Cost) — تُستخدم لتحليل التعادل (Break-Even Analysis).

    لا تُسجَّل كحركة دفع تلقائياً — هي «التزام شهري» معروف يُستخدم لتقدير العبء اليومي
    على المصنع/المحل. عند سداد الراتب فعلياً يُسجَّل في `Purchase(kind=EXPENSE)` كالمعتاد.
    الفائدة: حتى قبل دفع المصاريف، يعرف صاحب المحل كم يجب أن يبيع يومياً ليغطّي تكاليفه.
    """

    __tablename__ = "recurring_costs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name_ar: Mapped[str] = mapped_column(String(160))
    category: Mapped[RecurringCostCategory] = mapped_column(
        Enum(RecurringCostCategory),
        default=RecurringCostCategory.OTHER,
        index=True,
    )
    monthly_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 3), default=Decimal("0")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
