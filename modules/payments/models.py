from __future__ import annotations

import enum
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
    false,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from infra.db import Base


class PaymentMethodKind(str, enum.Enum):
    CASH = "CASH"
    BANK = "BANK"
    OTHER = "OTHER"


class PaymentMethodDomain(str, enum.Enum):
    """مجال الحساب المالي — يحدد من يراه في المطعم أو الفندق."""
    SHARED = "shared"
    RESTAURANT = "restaurant"
    HOTEL = "hotel"


class PaymentMethod(Base):
    __tablename__ = "payment_methods"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name_ar: Mapped[str] = mapped_column(String(120), unique=True)
    kind: Mapped[PaymentMethodKind] = mapped_column(
        Enum(PaymentMethodKind), default=PaymentMethodKind.BANK, server_default=PaymentMethodKind.BANK.value
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    #: قبض من الزبائن في نقطة البيع فقط — لا يعني استلام تحويلات.
    can_receive: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    #: صرف للموردين والمصروفات والتحويل الصادر (بما فيها إيداع من حساب المالك).
    can_pay: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    #: استلام تحويلات من حسابات أخرى — منفصل عن نقطة البيع.
    can_fund: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    #: حساب نظامي لا يُحذف (ذمم مورد، سحوبات مالك).
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    #: إظهار بطاقة الرصيد في لوحة «الخزينة والذمم».
    show_on_dashboard: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    #: مطعم · فندق · مشترك — يحدد ظهور الحساب لكل قسم.
    business_domain: Mapped[PaymentMethodDomain] = mapped_column(
        Enum(
            PaymentMethodDomain,
            values_callable=lambda obj: [e.value for e in obj],
            native_enum=False,
            length=20,
        ),
        default=PaymentMethodDomain.SHARED,
        server_default=PaymentMethodDomain.SHARED.value,
        index=True,
    )
    #: أيقونة مخصّصة في نقطة البيع (مسار نسبي تحت static/)
    icon_path: Mapped[str | None] = mapped_column(String(255), nullable=True)


SUPPLIER_CREDIT_PM_NAME = "ذمم دائن — مورد (آجل)"
OWNER_EQUITY_PM_NAME = "حساب المالك — حقوق الملكية"
# أسماء قديمة — تُدمَّج تلقائياً في حساب المالك الموحّد
LEGACY_OWNER_DRAW_PM_NAME = "سحوبات المالك — حقوق الملكية"
LEGACY_OWNER_CAPITAL_PM_NAME = "إيداعات المالك — حقوق الملكية"
MAIN_TREASURY_CASH_PM_NAME = "الخزينة الرئيسية — كاش"
MAIN_TREASURY_BANK_PM_NAME = "الخزينة الرئيسية — مصرف"
HOTEL_TREASURY_CASH_PM_NAME = "خزينة الفندق — كاش"
HOTEL_TREASURY_BANK_PM_NAME = "خزينة الفندق — مصرف"
#: محافظ تحصيل الاستقبال — تستقبل من النزلاء؛ لا تدخل خزينة الفندق إلا بعد اعتماد أمين الخزينة
HOTEL_RECEPTION_CASH_PM_NAME = "استقبال الفندق — كاش"
HOTEL_RECEPTION_BANK_PM_NAME = "استقبال الفندق — مصرف"
ROOM_SETTLE_CLEARING_PM_NAME = "تسوية غرفة — مقاصة فندق/مطعم"
RESTAURANT_PURCHASE_CUSTODY_CASH_PM_NAME = "عهدة مشتريات — مطعم — كاش"
RESTAURANT_PURCHASE_CUSTODY_BANK_PM_NAME = "عهدة مشتريات — مطعم — مصرف"
HOTEL_PURCHASE_CUSTODY_CASH_PM_NAME = "عهدة مشتريات — فندق — كاش"
HOTEL_PURCHASE_CUSTODY_BANK_PM_NAME = "عهدة مشتريات — فندق — مصرف"
# أسماء قديمة — تُرقَّى تلقائياً إلى كاش
LEGACY_RESTAURANT_PURCHASE_CUSTODY_PM_NAME = "عهدة مشتريات — مطعم"
LEGACY_HOTEL_PURCHASE_CUSTODY_PM_NAME = "عهدة مشتريات — فندق"

PURCHASE_CUSTODY_PM_NAMES: frozenset[str] = frozenset(
    {
        RESTAURANT_PURCHASE_CUSTODY_CASH_PM_NAME,
        RESTAURANT_PURCHASE_CUSTODY_BANK_PM_NAME,
        HOTEL_PURCHASE_CUSTODY_CASH_PM_NAME,
        HOTEL_PURCHASE_CUSTODY_BANK_PM_NAME,
        LEGACY_RESTAURANT_PURCHASE_CUSTODY_PM_NAME,
        LEGACY_HOTEL_PURCHASE_CUSTODY_PM_NAME,
    }
)


class PaymentTransferType(str, enum.Enum):
    REFUND_SETTLEMENT = "REFUND_SETTLEMENT"
    MANUAL = "MANUAL"
    OWNER_DRAW = "OWNER_DRAW"
    OWNER_CAPITAL = "OWNER_CAPITAL"
    SALE_PAYMENT_CORRECTION = "SALE_PAYMENT_CORRECTION"
    SHIFT_HANDOFF = "SHIFT_HANDOFF"


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


class PurchaseLineKind(str, enum.Enum):
    """تصنيف بند داخل فاتورة الشراء الموحّدة (مخزون / أصل / استهلاك)."""

    PRODUCT = "PRODUCT"
    FIXED_ASSET = "FIXED_ASSET"
    CONSUMABLE = "CONSUMABLE"


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
    supplier_phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
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
    pos_shift_id: Mapped[int | None] = mapped_column(
        ForeignKey("pos_shifts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    hotel_shift_id: Mapped[int | None] = mapped_column(
        ForeignKey("hotel_shifts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: رقم استلام المخزون — يُولَّد تلقائياً عند حفظ فاتورة الشراء (مثل GR-000042).
    receipt_batch_no: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    business_domain: Mapped[PaymentMethodDomain] = mapped_column(
        Enum(
            PaymentMethodDomain,
            values_callable=lambda obj: [e.value for e in obj],
        ),
        default=PaymentMethodDomain.RESTAURANT,
        index=True,
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
    - PRODUCT: product_id مطلوب (ينعكس على المخزون) — داخل فاتورة INVENTORY.
    - FIXED_ASSET: item_name + useful_life_months > 0 (إهلاك).
    - CONSUMABLE: item_name + useful_life_months = 0 (مصروف فوري).
    - فواتير ASSET القديمة: بدون line_kind؛ يُستنتج من useful_life_months.
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
    #: PRODUCT | FIXED_ASSET | CONSUMABLE — اختياري للتوافق مع الفواتير القديمة.
    line_kind: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
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
    #: رقم الدفعة داخل فاتورة الشراء (مثل GR-000042-L01) — للأصناف ذات الصلاحية.
    lot_code: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    production_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    expiry_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    purchase: Mapped[Purchase] = relationship(back_populates="lines")
    product = relationship("Product", foreign_keys=[product_id])

    @property
    def resolved_line_kind(self) -> str:
        raw = (self.line_kind or "").strip().upper()
        if raw in (
            PurchaseLineKind.PRODUCT.value,
            PurchaseLineKind.FIXED_ASSET.value,
            PurchaseLineKind.CONSUMABLE.value,
        ):
            return raw
        if self.product_id is not None:
            return PurchaseLineKind.PRODUCT.value
        if self.useful_life_months and int(self.useful_life_months) > 0:
            return PurchaseLineKind.FIXED_ASSET.value
        if (self.item_name or "").strip():
            return PurchaseLineKind.CONSUMABLE.value
        return PurchaseLineKind.PRODUCT.value

    @property
    def display_name(self) -> str:
        if self.item_name:
            return self.item_name
        if self.product is not None:
            return self.product.name_ar
        return f"#{self.product_id or '?'}"

    @property
    def is_fixed_asset(self) -> bool:
        if self.resolved_line_kind == PurchaseLineKind.FIXED_ASSET.value:
            return True
        return bool(self.useful_life_months and int(self.useful_life_months) > 0)

    @property
    def is_product_line(self) -> bool:
        return self.resolved_line_kind == PurchaseLineKind.PRODUCT.value

    @property
    def is_consumable_line(self) -> bool:
        return self.resolved_line_kind == PurchaseLineKind.CONSUMABLE.value

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
    business_domain: Mapped[PaymentMethodDomain] = mapped_column(
        Enum(
            PaymentMethodDomain,
            values_callable=lambda obj: [e.value for e in obj],
            native_enum=False,
            length=20,
        ),
        default=PaymentMethodDomain.RESTAURANT,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
