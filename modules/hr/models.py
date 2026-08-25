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
    Integer,
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
    work_shift_id: Mapped[int | None] = mapped_column(
        ForeignKey("hr_work_shifts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    is_pos_cashier: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_hotel_front: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_pos_supervisor: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    pos_pin_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    zk_emp_code: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True, index=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    business_domain: Mapped[str] = mapped_column(
        String(20), default="restaurant", index=True
    )
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
    work_shift: Mapped["WorkShift | None"] = relationship(
        foreign_keys="Employee.work_shift_id"
    )
    department_id: Mapped[int | None] = mapped_column(
        ForeignKey("hr_departments.id", ondelete="SET NULL"), nullable=True, index=True
    )
    department: Mapped["HrDepartment | None"] = relationship(
        foreign_keys="Employee.department_id"
    )
    day_schedules: Mapped[list["EmployeeDaySchedule"]] = relationship(
        back_populates="employee",
        cascade="all, delete-orphan",
    )

    @property
    def is_active(self) -> bool:
        return self.status == EmployeeStatus.ACTIVE


class EmployeeMealWallet(Base):
    """رصيد وجبات الموظف لفترة محددة، عادة شهر YYYY-MM."""

    __tablename__ = "hr_employee_meal_wallets"
    __table_args__ = (
        UniqueConstraint("employee_id", "period_label", name="uq_hr_employee_meal_wallet_period"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("hr_employees.id", ondelete="CASCADE"), index=True
    )
    period_label: Mapped[str] = mapped_column(String(7), index=True)
    allowance_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 3), default=Decimal("0")
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    employee: Mapped[Employee] = relationship(lazy="selectin")
    redemptions: Mapped[list["EmployeeMealRedemption"]] = relationship(
        back_populates="wallet",
        cascade="all, delete-orphan",
    )


class EmployeeMealRedemption(Base):
    """استهلاك من رصيد وجبات موظف عند إغلاق فاتورة POS."""

    __tablename__ = "hr_employee_meal_redemptions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    wallet_id: Mapped[int] = mapped_column(
        ForeignKey("hr_employee_meal_wallets.id", ondelete="CASCADE"), index=True
    )
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("hr_employees.id", ondelete="CASCADE"), index=True
    )
    sale_id: Mapped[int] = mapped_column(
        ForeignKey("sales.id", ondelete="CASCADE"), unique=True, index=True
    )
    sale_total: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    covered_amount: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    personal_amount: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )

    wallet: Mapped[EmployeeMealWallet] = relationship(back_populates="redemptions")
    employee: Mapped[Employee] = relationship(lazy="selectin")


# =====================================================================
#                        أقسام العمل
# =====================================================================
class HrDepartment(Base):
    """قسم/وحدة عمل — مقهى، مطعم، استقبال، …"""

    __tablename__ = "hr_departments"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name_ar: Mapped[str] = mapped_column(String(120), index=True)
    code: Mapped[str | None] = mapped_column(String(32), nullable=True, unique=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    employees: Mapped[list["Employee"]] = relationship(
        back_populates="department",
        foreign_keys="Employee.department_id",
    )


# =====================================================================
#                        ورديات العمل
# =====================================================================
class WorkShift(Base):
    """وردية عمل — توقيت الحضور المتوقع وساعات العمل."""

    __tablename__ = "hr_work_shifts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name_ar: Mapped[str] = mapped_column(String(120), index=True)
    code: Mapped[str | None] = mapped_column(String(32), nullable=True, unique=True)
    start_time: Mapped[str] = mapped_column(String(5), default="08:00")
    end_time: Mapped[str] = mapped_column(String(5), default="16:00")
    work_hours: Mapped[Decimal] = mapped_column(Numeric(4, 2), default=Decimal("8.00"))
    grace_minutes: Mapped[int] = mapped_column(Integer, default=15)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    employees: Mapped[list["Employee"]] = relationship(
        back_populates="work_shift",
        foreign_keys="Employee.work_shift_id",
    )
    day_schedules: Mapped[list["WorkShiftDaySchedule"]] = relationship(
        back_populates="work_shift",
        cascade="all, delete-orphan",
    )

    @property
    def schedule_label(self) -> str:
        return f"{self.start_time} – {self.end_time} ({self.work_hours} س)"


class WorkShiftDaySchedule(Base):
    """تخصيص يوم معيّن في الأسبوع لوردية (مثلاً الجمعة ساعات مختلفة)."""

    __tablename__ = "hr_work_shift_day_schedules"
    __table_args__ = (
        UniqueConstraint("work_shift_id", "day_of_week", name="uq_hr_shift_day"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    work_shift_id: Mapped[int] = mapped_column(
        ForeignKey("hr_work_shifts.id", ondelete="CASCADE"), index=True
    )
    day_of_week: Mapped[int] = mapped_column(Integer, index=True)
    is_rest_day: Mapped[bool] = mapped_column(Boolean, default=False)
    start_time: Mapped[str | None] = mapped_column(String(5), nullable=True)
    end_time: Mapped[str | None] = mapped_column(String(5), nullable=True)
    work_hours: Mapped[Decimal | None] = mapped_column(Numeric(4, 2), nullable=True)
    grace_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    work_shift: Mapped["WorkShift"] = relationship(back_populates="day_schedules")


class EmployeeDaySchedule(Base):
    """تخصيص يوم معيّن للموظف — يتجاوز جدول الوردية."""

    __tablename__ = "hr_employee_day_schedules"
    __table_args__ = (
        UniqueConstraint("employee_id", "day_of_week", name="uq_hr_emp_day"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("hr_employees.id", ondelete="CASCADE"), index=True
    )
    day_of_week: Mapped[int] = mapped_column(Integer, index=True)
    is_rest_day: Mapped[bool] = mapped_column(Boolean, default=False)
    start_time: Mapped[str | None] = mapped_column(String(5), nullable=True)
    end_time: Mapped[str | None] = mapped_column(String(5), nullable=True)
    work_hours: Mapped[Decimal | None] = mapped_column(Numeric(4, 2), nullable=True)
    grace_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    employee: Mapped["Employee"] = relationship(back_populates="day_schedules")


# =====================================================================
#                        سجل الحضور والانصراف
# =====================================================================
class AttendanceSource(str, enum.Enum):
    SELF = "SELF"  # الموظف بنفسه
    MANAGER = "MANAGER"  # المدير سجّل له
    AUTO = "AUTO"  # تلقائي (بعض الأنظمة)


class OvertimeApprovalStatus(str, enum.Enum):
    NONE = "NONE"
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


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
    work_shift_id: Mapped[int | None] = mapped_column(
        ForeignKey("hr_work_shifts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    late_minutes: Mapped[int] = mapped_column(Integer, default=0)
    early_leave_minutes: Mapped[int] = mapped_column(Integer, default=0)
    overtime_minutes: Mapped[int] = mapped_column(Integer, default=0)
    expected_work_hours: Mapped[Decimal | None] = mapped_column(
        Numeric(4, 2), nullable=True
    )
    overtime_approval_status: Mapped[OvertimeApprovalStatus] = mapped_column(
        Enum(OvertimeApprovalStatus), default=OvertimeApprovalStatus.NONE, index=True
    )
    approved_overtime_minutes: Mapped[int] = mapped_column(Integer, default=0)
    overtime_approved_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    overtime_approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    recorded_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    employee: Mapped[Employee] = relationship(back_populates="attendance")
    work_shift: Mapped["WorkShift | None"] = relationship(
        foreign_keys="AttendanceRecord.work_shift_id"
    )

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

    @property
    def source_label(self) -> str:
        if self.source == AttendanceSource.AUTO:
            return "بصمة"
        if self.source == AttendanceSource.SELF:
            return "ذاتي"
        return "يدوي"

    @property
    def payroll_overtime_minutes(self) -> int:
        """ساعات إضافية تُحسب في الرواتب — فقط بعد موافقة المدير."""
        if self.overtime_approval_status == OvertimeApprovalStatus.APPROVED:
            return int(self.approved_overtime_minutes or 0)
        return 0

    @property
    def _shift_standard_hours(self) -> Decimal | None:
        """ساعات الوردية المعيارية للموظف (من الوردية أو من ملف الموظف)."""
        if self.expected_work_hours is not None:
            return Decimal(str(self.expected_work_hours)).quantize(Decimal("0.01"))
        shift = self.work_shift
        if shift is not None and shift.work_hours:
            return Decimal(str(shift.work_hours)).quantize(Decimal("0.01"))
        emp = self.employee
        if emp is not None and emp.standard_hours_per_day:
            return Decimal(str(emp.standard_hours_per_day)).quantize(Decimal("0.01"))
        return None

    @property
    def payroll_hours_counted(self) -> Decimal:
        """ساعات تُعرض وتُحسب في الرواتب — عند رفض الإضافي = وردية فقط."""
        if self.check_out is None:
            return Decimal("0")
        hw = self.hours_worked
        if (
            self.overtime_minutes > 0
            and self.overtime_approval_status == OvertimeApprovalStatus.REJECTED
        ):
            shift_h = self._shift_standard_hours
            if shift_h is not None:
                return min(hw, shift_h).quantize(Decimal("0.01"))
            ot_h = (Decimal(str(self.overtime_minutes)) / Decimal("60")).quantize(
                Decimal("0.01")
            )
            counted = max(Decimal("0"), hw - ot_h).quantize(Decimal("0.01"))
            return counted if counted > 0 else hw
        return hw

    @property
    def standard_hours_counted(self) -> Decimal:
        """ساعات الوردية = إجمالي الحضور ناقص الإضافي المحسوب."""
        if self.check_out is None:
            return Decimal("0")
        if self.overtime_minutes > 0:
            if self.overtime_approval_status == OvertimeApprovalStatus.REJECTED:
                return self.payroll_hours_counted
            ot_h = (Decimal(str(self.overtime_minutes)) / Decimal("60")).quantize(
                Decimal("0.01")
            )
            return max(Decimal("0"), self.hours_worked - ot_h).quantize(Decimal("0.01"))
        return self.hours_worked

    @property
    def regular_hours_counted(self) -> Decimal:
        """ساعات محتسبة في الرواتب (بعد رفض الإضافي = وردية فقط)."""
        if (
            self.overtime_minutes > 0
            and self.overtime_approval_status == OvertimeApprovalStatus.REJECTED
        ):
            return self.payroll_hours_counted
        return self.hours_worked

    @property
    def overtime_status_label(self) -> str:
        if self.overtime_minutes <= 0 or self.check_out is None:
            return ""
        st = self.overtime_approval_status
        if st == OvertimeApprovalStatus.APPROVED:
            return "إضافي معتمد"
        if st == OvertimeApprovalStatus.REJECTED:
            return "إضافي مرفوض"
        if st == OvertimeApprovalStatus.PENDING:
            return "إضافي بانتظار الموافقة"
        return "إضافي"

    @property
    def attendance_status_label(self) -> str:
        if self.check_out is None:
            return "جلسة مفتوحة"
        if self.late_minutes > 0 and self.overtime_minutes > 0:
            return "تأخير + إضافي"
        if self.late_minutes > 0:
            return "تأخير"
        if self.early_leave_minutes > 0:
            return "انصراف مبكر"
        if self.overtime_minutes > 0:
            if self.overtime_approval_status == OvertimeApprovalStatus.PENDING:
                return "إضافي (بانتظار الموافقة)"
            if self.overtime_approval_status == OvertimeApprovalStatus.APPROVED:
                return "ساعات إضافية"
            if self.overtime_approval_status == OvertimeApprovalStatus.REJECTED:
                return "في الموعد"
        return "في الموعد"


class ZkProcessedPunch(Base):
    """بصمات ZKBioTime التي تمت معالجتها (منع التكرار)."""

    __tablename__ = "hr_zk_processed_punches"

    zk_transaction_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int | None] = mapped_column(
        ForeignKey("hr_employees.id", ondelete="SET NULL"), nullable=True, index=True
    )
    action: Mapped[str] = mapped_column(String(32), default="")
    detail: Mapped[str | None] = mapped_column(String(500), nullable=True)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


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
        UniqueConstraint(
            "period_year",
            "period_month",
            "business_domain",
            name="uq_payroll_period_domain",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    period_year: Mapped[int] = mapped_column(index=True)
    period_month: Mapped[int] = mapped_column(index=True)  # 1..12
    business_domain: Mapped[str] = mapped_column(
        String(20), default="restaurant", index=True
    )
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
    salary_receipt_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    salary_receipt_confirmed_via: Mapped[str | None] = mapped_column(
        String(32), nullable=True
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


# =====================================================================
#                 خصومات مستحقة على الموظف (عجز/جزاءات)
# =====================================================================
class DeductionStatus(str, enum.Enum):
    OUTSTANDING = "OUTSTANDING"  # لم يُخصم بعد
    PARTIALLY_REPAID = "PARTIALLY_REPAID"  # خُصم جزء
    FULLY_REPAID = "FULLY_REPAID"  # خُصم بالكامل
    CANCELLED = "CANCELLED"  # عفو/إلغاء


class EmployeeDeduction(Base):
    """خصم مستحق على موظف يُطبَّق عند صرف الرواتب (مثل عجز صندوق).

    لا يخصم من الخزينة عند إنشائه؛ يُسدد عبر PayrollEntry.deductions.
    """

    __tablename__ = "hr_employee_deductions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("hr_employees.id", ondelete="RESTRICT"), index=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    repaid_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 3), default=Decimal("0")
    )
    status: Mapped[DeductionStatus] = mapped_column(
        Enum(DeductionStatus), default=DeductionStatus.OUTSTANDING, index=True
    )

    source_type: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    source_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    employee: Mapped[Employee] = relationship(lazy="selectin")
    repayments: Mapped[list["DeductionRepayment"]] = relationship(
        back_populates="deduction",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    @property
    def remaining_amount(self) -> Decimal:
        return (self.amount - self.repaid_amount).quantize(Decimal("0.001"))

    @property
    def is_settled(self) -> bool:
        return self.status in (DeductionStatus.FULLY_REPAID, DeductionStatus.CANCELLED)


class DeductionRepayment(Base):
    """سجل تسديد خصم (يدوي أو عبر خصم من بند راتب)."""

    __tablename__ = "hr_deduction_repayments"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    deduction_id: Mapped[int] = mapped_column(
        ForeignKey("hr_employee_deductions.id", ondelete="CASCADE"), index=True
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
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    deduction: Mapped[EmployeeDeduction] = relationship(back_populates="repayments")


# =====================================================================
#                 حوافز ومكافآت مستحقة للموظف
# =====================================================================
class BonusStatus(str, enum.Enum):
    OUTSTANDING = "OUTSTANDING"  # لم تُصرف بعد ضمن راتب
    SETTLED = "SETTLED"  # طُبّقت عبر دفعة راتب
    CANCELLED = "CANCELLED"  # ملغاة


class EmployeeBonus(Base):
    """حافز/مكافأة تُضاف تلقائياً إلى بند الراتب عند إنشاء/مزامنة الدفعة.

    لا تُصرف نقداً عند إنشائها؛ تُسدَّد عبر PayrollEntry.bonuses عند دفع الراتب.
    """

    __tablename__ = "hr_employee_bonuses"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("hr_employees.id", ondelete="RESTRICT"), index=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    status: Mapped[BonusStatus] = mapped_column(
        Enum(BonusStatus), default=BonusStatus.OUTSTANDING, index=True
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    settled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    settled_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    payroll_entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("hr_payroll_entries.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    employee: Mapped[Employee] = relationship(lazy="selectin")
