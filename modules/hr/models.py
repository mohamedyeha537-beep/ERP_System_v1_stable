"""نماذج وحدة الموارد البشرية: الموظف، الحضور والانصراف، دفعة الرواتب وبنودها.

التصميم يتبع المبادئ المحاسبية:
- الراتب الشهري يُعتبر التزاماً منتظماً (Recurring Cost) لأغراض تحليل التعادل CVP.
- عند دفع راتب فعلياً (PayrollEntry.PAID) يُسجَّل تلقائياً كقيد مصروف
  Purchase(kind=EXPENSE, expense_category="رواتب") ليظهر بشكل صحيح في:
  • قائمة الدخل (طرحه من إجمالي الربح ⇒ صافي الربح)
  • التدفق النقدي (خصم من المحفظة)
  • تقرير المصروفات

المراجع: IAS 19 (Employee Benefits) + الممارسات المحلية للأعمال الصغيرة.
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
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from infra.db import Base


# =====================================================================
#                           الموظف
# =====================================================================
class EmployeeStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"  # على رأس العمل
    SUSPENDED = "SUSPENDED"  # موقوف مؤقتاً
    TERMINATED = "TERMINATED"  # منتهية خدمته


class PayType(str, enum.Enum):
    """نوع الأجر."""

    MONTHLY = "MONTHLY"  # راتب شهري ثابت
    HOURLY = "HOURLY"  # أجر بالساعة
    DAILY = "DAILY"  # أجر باليوم
    MIXED = "MIXED"  # شهري + إضافي بالساعة


class Employee(Base):
    __tablename__ = "hr_employees"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    full_name_ar: Mapped[str] = mapped_column(String(160), index=True)
    job_title: Mapped[str | None] = mapped_column(String(120), nullable=True)
    national_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(40), nullable=True)

    pay_type: Mapped[PayType] = mapped_column(
        Enum(PayType), default=PayType.MONTHLY, index=True
    )
    base_monthly_salary: Mapped[Decimal] = mapped_column(
        Numeric(14, 3), default=Decimal("0")
    )
    hourly_rate: Mapped[Decimal] = mapped_column(
        Numeric(14, 3), default=Decimal("0")
    )
    standard_hours_per_day: Mapped[Decimal] = mapped_column(
        Numeric(6, 2), default=Decimal("8.00")
    )
    overtime_multiplier: Mapped[Decimal] = mapped_column(
        Numeric(4, 2), default=Decimal("1.50")
    )

    status: Mapped[EmployeeStatus] = mapped_column(
        Enum(EmployeeStatus), default=EmployeeStatus.ACTIVE, index=True
    )
    hired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    terminated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, unique=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    attendance: Mapped[list["AttendanceRecord"]] = relationship(
        back_populates="employee", cascade="all, delete-orphan"
    )

    @property
    def is_active(self) -> bool:
        return self.status == EmployeeStatus.ACTIVE


# =====================================================================
#                        سجل الحضور والانصراف
# =====================================================================
class AttendanceSource(str, enum.Enum):
    SELF = "SELF"  # الموظف بنفسه
    MANAGER = "MANAGER"  # المدير سجّل له
    AUTO = "AUTO"  # تلقائي (بعض الأنظمة)


class AttendanceRecord(Base):
    """سجل دخول/خروج واحد. اليوم الواحد قد يحتوي عدة سجلات (مناوبات)."""

    __tablename__ = "hr_attendance"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("hr_employees.id", ondelete="CASCADE"), index=True
    )
    check_in: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    check_out: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    source: Mapped[AttendanceSource] = mapped_column(
        Enum(AttendanceSource), default=AttendanceSource.MANAGER
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    recorded_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    employee: Mapped[Employee] = relationship(back_populates="attendance")

    @property
    def hours_worked(self) -> Decimal:
        """ساعات العمل بين check_in و check_out (0 إن لم يخرج بعد)."""
        if self.check_out is None:
            return Decimal("0")
        delta = self.check_out - self.check_in
        if delta.total_seconds() <= 0:
            return Decimal("0")
        h = Decimal(str(delta.total_seconds() / 3600.0)).quantize(Decimal("0.01"))
        return h

    @property
    def is_open(self) -> bool:
        return self.check_out is None


# =====================================================================
#                        دفعة رواتب شهرية
# =====================================================================
class PayrollStatus(str, enum.Enum):
    DRAFT = "DRAFT"  # مسودة (يمكن تعديلها)
    POSTED = "POSTED"  # مُعتمدة (مغلقة، لم تُدفع)
    PAID = "PAID"  # دُفعت ⇒ تم تسجيلها كمصروفات


class PayrollRun(Base):
    """دفعة رواتب لشهر محدّد."""

    __tablename__ = "hr_payroll_runs"
    __table_args__ = (
        UniqueConstraint("period_year", "period_month", name="uq_payroll_period"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    period_year: Mapped[int] = mapped_column(index=True)
    period_month: Mapped[int] = mapped_column(index=True)  # 1..12
    status: Mapped[PayrollStatus] = mapped_column(
        Enum(PayrollStatus), default=PayrollStatus.DRAFT, index=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    posted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    paid_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    entries: Mapped[list["PayrollEntry"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    @property
    def label(self) -> str:
        return f"{self.period_year}/{self.period_month:02d}"

    @property
    def total_net(self) -> Decimal:
        return sum((e.net_pay for e in self.entries), Decimal("0")).quantize(
            Decimal("0.001")
        )

    @property
    def total_gross(self) -> Decimal:
        return sum((e.gross_pay for e in self.entries), Decimal("0")).quantize(
            Decimal("0.001")
        )


class PayrollEntry(Base):
    """مفردات راتب موظف ضمن دفعة:

      gross_pay   = base_salary + overtime_pay + bonuses
      net_pay     = gross_pay − deductions − advances
    """

    __tablename__ = "hr_payroll_entries"
    __table_args__ = (
        UniqueConstraint("run_id", "employee_id", name="uq_payroll_entry"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("hr_payroll_runs.id", ondelete="CASCADE"), index=True
    )
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("hr_employees.id", ondelete="RESTRICT"), index=True
    )

    base_salary: Mapped[Decimal] = mapped_column(
        Numeric(14, 3), default=Decimal("0")
    )
    hours_worked: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), default=Decimal("0")
    )
    overtime_hours: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), default=Decimal("0")
    )
    overtime_pay: Mapped[Decimal] = mapped_column(
        Numeric(14, 3), default=Decimal("0")
    )
    bonuses: Mapped[Decimal] = mapped_column(
        Numeric(14, 3), default=Decimal("0")
    )
    deductions: Mapped[Decimal] = mapped_column(
        Numeric(14, 3), default=Decimal("0")
    )
    advances: Mapped[Decimal] = mapped_column(
        Numeric(14, 3), default=Decimal("0")
    )
    net_pay: Mapped[Decimal] = mapped_column(
        Numeric(14, 3), default=Decimal("0")
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    paid_purchase_id: Mapped[int | None] = mapped_column(
        ForeignKey("purchases.id", ondelete="SET NULL"), nullable=True
    )
    paid_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    run: Mapped[PayrollRun] = relationship(back_populates="entries")
    employee: Mapped[Employee] = relationship(lazy="selectin")

    @property
    def gross_pay(self) -> Decimal:
        return (self.base_salary + self.overtime_pay + self.bonuses).quantize(
            Decimal("0.001")
        )


# =====================================================================
#                          السلف على الراتب
# =====================================================================
class AdvanceStatus(str, enum.Enum):
    OUTSTANDING = "OUTSTANDING"  # غير مسددة (لم يُسترد منها شيء)
    PARTIALLY_REPAID = "PARTIALLY_REPAID"  # مسترد جزء منها
    FULLY_REPAID = "FULLY_REPAID"  # مسددة بالكامل
    CANCELLED = "CANCELLED"  # ملغاة (مثلاً تنازل المدير عنها)


class SalaryAdvance(Base):
    """سلفة مالية لموظف تُخصم لاحقاً من راتبه.

    التتبّع المحاسبي:
      - عند صرف السلفة: يُسجَّل قيد مصروف (Purchase EXPENSE) بفئة "سلفة موظف"
        لمتابعة التدفق النقدي. هذا هو الخيار الافتراضي. يمكن إيقافه إن كان
        الأدمن يفضّل تتبّعها كذمم مدينة فقط دون تأثير على المصاريف.
      - عند استرداد السلفة عبر خصم من راتب الموظف:
        لا يُنشأ قيد مصروف جديد، فقط يُحدَّث `repaid_amount`.
        صافي راتب الموظف يقلّ بقيمة السلفة المخصومة، فيظهر التأثير صحيحاً
        في إجمالي مصاريف الرواتب.
    """

    __tablename__ = "hr_advances"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("hr_employees.id", ondelete="RESTRICT"), index=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    repaid_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 3), default=Decimal("0")
    )
    status: Mapped[AdvanceStatus] = mapped_column(
        Enum(AdvanceStatus), default=AdvanceStatus.OUTSTANDING, index=True
    )
    given_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    payment_method_id: Mapped[int | None] = mapped_column(
        ForeignKey("payment_methods.id", ondelete="SET NULL"), nullable=True
    )
    purchase_id: Mapped[int | None] = mapped_column(
        ForeignKey("purchases.id", ondelete="SET NULL"), nullable=True
    )
    given_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    employee: Mapped[Employee] = relationship(lazy="selectin")
    repayments: Mapped[list["AdvanceRepayment"]] = relationship(
        back_populates="advance",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    @property
    def remaining_amount(self) -> Decimal:
        return (self.amount - self.repaid_amount).quantize(Decimal("0.001"))

    @property
    def is_settled(self) -> bool:
        return self.status in (
            AdvanceStatus.FULLY_REPAID,
            AdvanceStatus.CANCELLED,
        )


class AdvanceRepayment(Base):
    """سجل استرداد جزئي/كلي من سلفة (يدوي أو عبر خصم من دفعة راتب)."""

    __tablename__ = "hr_advance_repayments"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    advance_id: Mapped[int] = mapped_column(
        ForeignKey("hr_advances.id", ondelete="CASCADE"), index=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    repaid_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    payroll_entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("hr_payroll_entries.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    advance: Mapped[SalaryAdvance] = relationship(back_populates="repayments")
