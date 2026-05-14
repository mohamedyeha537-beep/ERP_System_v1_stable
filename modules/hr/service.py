"""خدمات وحدة الموارد البشرية: موظفون، حضور، رواتب."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from modules.hr.models import (
    AdvanceRepayment,
    AdvanceStatus,
    AttendanceRecord,
    AttendanceSource,
    Employee,
    EmployeeStatus,
    PayType,
    PayrollEntry,
    PayrollRun,
    PayrollStatus,
    SalaryAdvance,
)


class HRError(Exception):
    pass


# =====================================================================
#                          الموظفون
# =====================================================================
def list_employees(db: Session, only_active: bool = False) -> list[Employee]:
    stmt = select(Employee).order_by(Employee.full_name_ar)
    if only_active:
        stmt = stmt.where(Employee.status == EmployeeStatus.ACTIVE)
    return list(db.scalars(stmt).all())


def get_employee(db: Session, emp_id: int) -> Employee | None:
    return db.get(Employee, emp_id)


def upsert_employee(
    db: Session,
    *,
    emp_id: int | None,
    full_name_ar: str,
    job_title: str | None,
    national_id: str | None,
    phone: str | None,
    pay_type: PayType | str,
    base_monthly_salary: Decimal,
    hourly_rate: Decimal,
    standard_hours_per_day: Decimal,
    overtime_multiplier: Decimal,
    status: EmployeeStatus | str,
    hired_at: datetime | None,
    user_id: int | None = None,
    notes: str | None = None,
) -> Employee:
    name = (full_name_ar or "").strip()
    if not name:
        raise HRError("اسم الموظف مطلوب.")
    if base_monthly_salary < 0 or hourly_rate < 0:
        raise HRError("الأجر لا يمكن أن يكون سالباً.")
    if isinstance(pay_type, str):
        try:
            pay_type = PayType(pay_type)
        except ValueError:
            pay_type = PayType.MONTHLY
    if isinstance(status, str):
        try:
            status = EmployeeStatus(status)
        except ValueError:
            status = EmployeeStatus.ACTIVE

    if emp_id:
        emp = db.get(Employee, emp_id)
        if emp is None:
            raise HRError("الموظف غير موجود.")
    else:
        emp = Employee()
        db.add(emp)

    emp.full_name_ar = name
    emp.job_title = (job_title or "").strip() or None
    emp.national_id = (national_id or "").strip() or None
    emp.phone = (phone or "").strip() or None
    emp.pay_type = pay_type
    emp.base_monthly_salary = Decimal(str(base_monthly_salary)).quantize(
        Decimal("0.001")
    )
    emp.hourly_rate = Decimal(str(hourly_rate)).quantize(Decimal("0.001"))
    emp.standard_hours_per_day = Decimal(str(standard_hours_per_day)).quantize(
        Decimal("0.01")
    )
    emp.overtime_multiplier = Decimal(str(overtime_multiplier)).quantize(
        Decimal("0.01")
    )
    emp.status = status
    emp.hired_at = hired_at
    emp.user_id = user_id
    emp.notes = (notes or "").strip() or None
    if status == EmployeeStatus.TERMINATED and emp.terminated_at is None:
        emp.terminated_at = datetime.now(timezone.utc)
    elif status != EmployeeStatus.TERMINATED:
        emp.terminated_at = None
    db.flush()
    return emp


def delete_employee(db: Session, emp_id: int) -> None:
    """حذف صعب: نمنع الحذف لو كان الموظف مرتبطاً بأي بند راتب لحفظ السجل."""
    has_payroll = db.execute(
        select(func.count(PayrollEntry.id)).where(PayrollEntry.employee_id == emp_id)
    ).scalar_one()
    if has_payroll:
        raise HRError(
            "لا يمكن حذف الموظف لأن له بنود رواتب سابقة. أوقف خدمته بدلاً من ذلك."
        )
    emp = db.get(Employee, emp_id)
    if emp is not None:
        db.delete(emp)
        db.flush()


def total_active_monthly_salaries(db: Session) -> Decimal:
    """مجموع الرواتب الشهرية الأساسية للموظفين النشطين."""
    total = db.execute(
        select(func.coalesce(func.sum(Employee.base_monthly_salary), 0)).where(
            Employee.status == EmployeeStatus.ACTIVE
        )
    ).scalar_one()
    return Decimal(str(total or 0)).quantize(Decimal("0.001"))


# =====================================================================
#                       الحضور والانصراف
# =====================================================================
def open_attendance_for(
    db: Session, employee_id: int
) -> AttendanceRecord | None:
    """يعيد سجل الحضور المفتوح (لم يُسجَّل خروج بعد) إن وُجد."""
    return db.execute(
        select(AttendanceRecord)
        .where(
            and_(
                AttendanceRecord.employee_id == employee_id,
                AttendanceRecord.check_out.is_(None),
            )
        )
        .order_by(AttendanceRecord.check_in.desc())
    ).scalar_one_or_none()


def check_in(
    db: Session,
    *,
    employee_id: int,
    when: datetime | None = None,
    source: AttendanceSource = AttendanceSource.MANAGER,
    notes: str | None = None,
    recorded_by_id: int | None = None,
) -> AttendanceRecord:
    """يفتح جلسة حضور جديدة. لو هناك جلسة مفتوحة سابقة يرفع خطأ."""
    emp = db.get(Employee, employee_id)
    if emp is None:
        raise HRError("الموظف غير موجود.")
    if emp.status != EmployeeStatus.ACTIVE:
        raise HRError("الموظف غير نشط حالياً.")
    if open_attendance_for(db, employee_id) is not None:
        raise HRError("توجد جلسة حضور مفتوحة لهذا الموظف بالفعل (سجّل الانصراف أولاً).")
    rec = AttendanceRecord(
        employee_id=employee_id,
        check_in=when or datetime.now(timezone.utc),
        source=source,
        notes=(notes or "").strip() or None,
        recorded_by_id=recorded_by_id,
    )
    db.add(rec)
    db.flush()
    return rec


def check_out(
    db: Session,
    *,
    employee_id: int,
    when: datetime | None = None,
    notes: str | None = None,
) -> AttendanceRecord:
    rec = open_attendance_for(db, employee_id)
    if rec is None:
        raise HRError("لا توجد جلسة حضور مفتوحة لهذا الموظف.")
    out = when or datetime.now(timezone.utc)
    # توحيد المنطقة الزمنية لتجنّب مقارنة aware مع naive
    if out.tzinfo is None:
        out = out.replace(tzinfo=timezone.utc)
    rec_in = rec.check_in if rec.check_in.tzinfo else rec.check_in.replace(tzinfo=timezone.utc)
    if out <= rec_in:
        raise HRError("وقت الانصراف يجب أن يكون بعد وقت الحضور.")
    rec.check_out = out
    if notes:
        extra = (notes or "").strip()
        if extra:
            rec.notes = ((rec.notes or "") + " | " + extra).strip(" | ")
    db.flush()
    return rec


def list_attendance(
    db: Session,
    *,
    employee_id: int | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 500,
) -> list[AttendanceRecord]:
    stmt = select(AttendanceRecord).order_by(AttendanceRecord.check_in.desc())
    if employee_id is not None:
        stmt = stmt.where(AttendanceRecord.employee_id == employee_id)
    if start is not None:
        stmt = stmt.where(
            or_(
                AttendanceRecord.check_in >= start,
                AttendanceRecord.check_out >= start,
            )
        )
    if end is not None:
        stmt = stmt.where(AttendanceRecord.check_in < end)
    stmt = stmt.limit(limit)
    return list(db.scalars(stmt).all())


def _ensure_aware(dt: datetime) -> datetime:
    """يعيد التاريخ بمنطقة زمنية UTC إن كان بدون tz."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def hours_worked_in_period(
    db: Session, employee_id: int, start: datetime, end: datetime
) -> Decimal:
    """مجموع ساعات الجلسات المكتملة خلال الفترة (يقص الجلسة على حدود الفترة)."""
    recs = list_attendance(
        db, employee_id=employee_id, start=start, end=end, limit=10_000
    )
    total = Decimal("0")
    start = _ensure_aware(start)
    end = _ensure_aware(end)
    for r in recs:
        if r.check_out is None:
            continue
        rin = _ensure_aware(r.check_in)
        rout = _ensure_aware(r.check_out)
        s = max(rin, start)
        e = min(rout, end)
        if e <= s:
            continue
        h = Decimal(str((e - s).total_seconds() / 3600.0)).quantize(Decimal("0.01"))
        if h > 0:
            total += h
    return total.quantize(Decimal("0.01"))


# =====================================================================
#                          الرواتب الشهرية
# =====================================================================
def _period_range(year: int, month: int) -> tuple[datetime, datetime]:
    s = datetime(year, month, 1, tzinfo=timezone.utc)
    if month == 12:
        e = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        e = datetime(year, month + 1, 1, tzinfo=timezone.utc)
    return s, e


def list_payroll_runs(db: Session, limit: int = 24) -> list[PayrollRun]:
    return list(
        db.scalars(
            select(PayrollRun)
            .order_by(PayrollRun.period_year.desc(), PayrollRun.period_month.desc())
            .limit(limit)
        ).all()
    )


def get_payroll_run(db: Session, run_id: int) -> PayrollRun | None:
    return db.get(PayrollRun, run_id)


@dataclass
class _Calc:
    base_salary: Decimal
    hours_worked: Decimal
    overtime_hours: Decimal
    overtime_pay: Decimal


def _calc_for_employee(
    db: Session, emp: Employee, year: int, month: int
) -> _Calc:
    """يحسب الراتب الأساسي والإضافي بناءً على نوع الأجر وساعات الحضور المسجَّلة."""
    s, e = _period_range(year, month)
    days_in_month = (e - s).days
    h_actual = hours_worked_in_period(db, emp.id, s, e)

    base = Decimal("0")
    ot_hours = Decimal("0")
    ot_pay = Decimal("0")

    if emp.pay_type == PayType.MONTHLY:
        base = Decimal(str(emp.base_monthly_salary or 0))
    elif emp.pay_type == PayType.HOURLY:
        base = (h_actual * Decimal(str(emp.hourly_rate or 0))).quantize(
            Decimal("0.001")
        )
    elif emp.pay_type == PayType.DAILY:
        # نعتبر كل (standard_hours_per_day) ساعة = يوم عمل
        sph = Decimal(str(emp.standard_hours_per_day or 8))
        days = (h_actual / sph).quantize(Decimal("0.01")) if sph > 0 else Decimal("0")
        base = (days * Decimal(str(emp.hourly_rate or 0))).quantize(Decimal("0.001"))
    elif emp.pay_type == PayType.MIXED:
        base = Decimal(str(emp.base_monthly_salary or 0))
        # ساعات إضافية فوق المعيارية
        sph = Decimal(str(emp.standard_hours_per_day or 8))
        std_total = sph * Decimal(str(days_in_month))  # ساعات معيارية بالشهر كاملاً
        if h_actual > std_total:
            ot_hours = (h_actual - std_total).quantize(Decimal("0.01"))
            mult = Decimal(str(emp.overtime_multiplier or 1))
            ot_pay = (
                ot_hours * Decimal(str(emp.hourly_rate or 0)) * mult
            ).quantize(Decimal("0.001"))

    return _Calc(
        base_salary=base.quantize(Decimal("0.001")),
        hours_worked=h_actual,
        overtime_hours=ot_hours,
        overtime_pay=ot_pay,
    )


def create_payroll_run(
    db: Session,
    *,
    year: int,
    month: int,
    user_id: int | None = None,
) -> PayrollRun:
    """ينشئ دفعة رواتب لشهر/سنة ويملؤها بكل الموظفين النشطين تلقائياً."""
    if not (1 <= month <= 12):
        raise HRError("الشهر غير صالح.")
    existing = db.execute(
        select(PayrollRun).where(
            PayrollRun.period_year == year,
            PayrollRun.period_month == month,
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HRError(f"دفعة رواتب موجودة بالفعل لشهر {year}/{month:02d}.")

    run = PayrollRun(
        period_year=year,
        period_month=month,
        status=PayrollStatus.DRAFT,
        created_by_id=user_id,
    )
    db.add(run)
    db.flush()

    for emp in list_employees(db, only_active=True):
        c = _calc_for_employee(db, emp, year, month)
        # خصم تلقائي للسلف القائمة بحد أقصى صافي الراتب القابل للخصم
        gross = c.base_salary + c.overtime_pay
        outstanding = outstanding_advances_for(db, emp.id)
        suggested_deduction = min(outstanding, gross)
        net = (gross - suggested_deduction).quantize(Decimal("0.001"))
        entry = PayrollEntry(
            run_id=run.id,
            employee_id=emp.id,
            base_salary=c.base_salary,
            hours_worked=c.hours_worked,
            overtime_hours=c.overtime_hours,
            overtime_pay=c.overtime_pay,
            bonuses=Decimal("0"),
            deductions=Decimal("0"),
            advances=suggested_deduction.quantize(Decimal("0.001")),
            net_pay=net,
        )
        db.add(entry)
    db.flush()
    return run


def update_entry(
    db: Session,
    *,
    entry_id: int,
    base_salary: Decimal | None = None,
    overtime_pay: Decimal | None = None,
    bonuses: Decimal | None = None,
    deductions: Decimal | None = None,
    advances: Decimal | None = None,
    notes: str | None = None,
) -> PayrollEntry:
    entry = db.get(PayrollEntry, entry_id)
    if entry is None:
        raise HRError("بند الراتب غير موجود.")
    if entry.run.status != PayrollStatus.DRAFT:
        raise HRError("لا يمكن تعديل بند ضمن دفعة معتمدة أو مدفوعة.")
    if base_salary is not None:
        entry.base_salary = Decimal(str(base_salary)).quantize(Decimal("0.001"))
    if overtime_pay is not None:
        entry.overtime_pay = Decimal(str(overtime_pay)).quantize(Decimal("0.001"))
    if bonuses is not None:
        entry.bonuses = Decimal(str(bonuses)).quantize(Decimal("0.001"))
    if deductions is not None:
        entry.deductions = Decimal(str(deductions)).quantize(Decimal("0.001"))
    if advances is not None:
        entry.advances = Decimal(str(advances)).quantize(Decimal("0.001"))
    if notes is not None:
        entry.notes = (notes or "").strip() or None
    # احسب الصافي
    entry.net_pay = (
        entry.base_salary + entry.overtime_pay + entry.bonuses
        - entry.deductions - entry.advances
    ).quantize(Decimal("0.001"))
    db.flush()
    return entry


def post_run(db: Session, run_id: int) -> PayrollRun:
    run = db.get(PayrollRun, run_id)
    if run is None:
        raise HRError("الدفعة غير موجودة.")
    if run.status != PayrollStatus.DRAFT:
        raise HRError("الدفعة معتمدة أو مدفوعة بالفعل.")
    if not run.entries:
        raise HRError("لا توجد بنود في الدفعة.")
    run.status = PayrollStatus.POSTED
    run.posted_at = datetime.now(timezone.utc)
    db.flush()
    return run


def pay_run(
    db: Session,
    *,
    run_id: int,
    payment_method_id: int,
    user_id: int | None = None,
) -> PayrollRun:
    """يدفع الدفعة كاملةً: ينشئ قيد مصروف موحَّد ضمن قيود المشتريات/المصروفات.

    التصميم: قيد واحد بإجمالي الرواتب لتبسيط التقارير، مع ربط جميع البنود بنفس
    معرّف القيد (paid_purchase_id). هذا يسمح بتقرير واحد لإجمالي الرواتب الشهرية
    وفي نفس الوقت يحفظ سجل تفصيلي لكل موظف.
    """
    from modules.payments.service import record_expense

    run = db.get(PayrollRun, run_id)
    if run is None:
        raise HRError("الدفعة غير موجودة.")
    if run.status == PayrollStatus.PAID:
        raise HRError("الدفعة مدفوعة بالفعل.")
    if run.status == PayrollStatus.DRAFT:
        # اعتمدها تلقائياً
        run.status = PayrollStatus.POSTED
        run.posted_at = datetime.now(timezone.utc)

    total = run.total_net
    if total <= 0:
        raise HRError("إجمالي صافي الرواتب صفر.")

    purchase = record_expense(
        db,
        payment_method_id=payment_method_id,
        amount=total,
        expense_category="رواتب",
        supplier=f"رواتب {run.label}",
        note=f"دفع دفعة رواتب شهر {run.label} ({len(run.entries)} موظف)",
        user_id=user_id,
    )
    now = datetime.now(timezone.utc)
    for e in run.entries:
        e.paid_purchase_id = purchase.id
        e.paid_at = now
        # تسجيل الاسترداد مقابل السلف القائمة (FIFO على الأقدم)
        if e.advances and e.advances > 0:
            apply_advance_deductions(
                db,
                employee_id=e.employee_id,
                amount=Decimal(str(e.advances)),
                payroll_entry_id=e.id,
                when=now,
            )
    run.status = PayrollStatus.PAID
    run.paid_at = now
    db.flush()
    return run


def delete_run(db: Session, run_id: int) -> None:
    run = db.get(PayrollRun, run_id)
    if run is None:
        return
    if run.status == PayrollStatus.PAID:
        raise HRError("لا يمكن حذف دفعة مدفوعة (لها قيد مصروف مرتبط).")
    db.delete(run)
    db.flush()


# =====================================================================
#                            السلف
# =====================================================================
def list_advances(
    db: Session,
    *,
    employee_id: int | None = None,
    only_outstanding: bool = False,
    limit: int = 500,
) -> list[SalaryAdvance]:
    stmt = select(SalaryAdvance).order_by(SalaryAdvance.given_at.desc())
    if employee_id is not None:
        stmt = stmt.where(SalaryAdvance.employee_id == employee_id)
    if only_outstanding:
        stmt = stmt.where(
            SalaryAdvance.status.in_(
                [AdvanceStatus.OUTSTANDING, AdvanceStatus.PARTIALLY_REPAID]
            )
        )
    stmt = stmt.limit(limit)
    return list(db.scalars(stmt).all())


def get_advance(db: Session, advance_id: int) -> SalaryAdvance | None:
    return db.get(SalaryAdvance, advance_id)


def outstanding_advances_for(db: Session, employee_id: int) -> Decimal:
    """مجموع المتبقّي من السلف غير المسددة لموظف."""
    rows = db.execute(
        select(SalaryAdvance.amount, SalaryAdvance.repaid_amount).where(
            SalaryAdvance.employee_id == employee_id,
            SalaryAdvance.status.in_(
                [AdvanceStatus.OUTSTANDING, AdvanceStatus.PARTIALLY_REPAID]
            ),
        )
    ).all()
    total = Decimal("0")
    for amount, repaid in rows:
        a = Decimal(str(amount or 0))
        r = Decimal(str(repaid or 0))
        rem = a - r
        if rem > 0:
            total += rem
    return total.quantize(Decimal("0.001"))


def outstanding_advances_summary(
    db: Session,
) -> list[tuple[Employee, Decimal, int]]:
    """يعيد قائمة (الموظف، إجمالي المتبقي، عدد السلف القائمة) لكل موظف لديه سلف.

    يُستخدم في تقرير الأدمن قبل صرف الرواتب.
    """
    out: list[tuple[Employee, Decimal, int]] = []
    employees = list_employees(db, only_active=False)
    for emp in employees:
        adv = list_advances(db, employee_id=emp.id, only_outstanding=True)
        if not adv:
            continue
        rem = sum((a.remaining_amount for a in adv), Decimal("0"))
        if rem > 0:
            out.append((emp, rem.quantize(Decimal("0.001")), len(adv)))
    out.sort(key=lambda x: x[1], reverse=True)
    return out


def grand_total_outstanding_advances(db: Session) -> Decimal:
    """مجموع كل السلف القائمة على كل الموظفين."""
    rows = db.execute(
        select(SalaryAdvance.amount, SalaryAdvance.repaid_amount).where(
            SalaryAdvance.status.in_(
                [AdvanceStatus.OUTSTANDING, AdvanceStatus.PARTIALLY_REPAID]
            )
        )
    ).all()
    total = Decimal("0")
    for amount, repaid in rows:
        rem = Decimal(str(amount or 0)) - Decimal(str(repaid or 0))
        if rem > 0:
            total += rem
    return total.quantize(Decimal("0.001"))


def grant_advance(
    db: Session,
    *,
    employee_id: int,
    amount: Decimal,
    payment_method_id: int | None = None,
    record_as_expense: bool = True,
    when: datetime | None = None,
    notes: str | None = None,
    given_by_id: int | None = None,
) -> SalaryAdvance:
    """صرف سلفة لموظف.

    - record_as_expense=True (الافتراضي): يُسجَّل قيد مصروف بفئة «سلف موظفين»
      ليظهر في التدفق النقدي. لا يدخل في تقرير «رواتب» الأساسي.
    - يجب تمرير `payment_method_id` إذا كان `record_as_expense=True`.
    """
    emp = db.get(Employee, employee_id)
    if emp is None:
        raise HRError("الموظف غير موجود.")
    amt = Decimal(str(amount or 0))
    if amt <= 0:
        raise HRError("قيمة السلفة يجب أن تكون أكبر من صفر.")

    purchase_id: int | None = None
    if record_as_expense:
        if payment_method_id is None:
            raise HRError("اختر أسلوب الدفع لتسجيل السلفة كصرف نقدي.")
        from modules.payments.service import record_expense

        purchase = record_expense(
            db,
            payment_method_id=payment_method_id,
            amount=amt,
            expense_category="سلف موظفين",
            supplier=emp.full_name_ar,
            note=(notes or "").strip() or f"سلفة لـ {emp.full_name_ar}",
            user_id=given_by_id,
        )
        purchase_id = purchase.id

    adv = SalaryAdvance(
        employee_id=employee_id,
        amount=amt.quantize(Decimal("0.001")),
        repaid_amount=Decimal("0"),
        status=AdvanceStatus.OUTSTANDING,
        given_at=when or datetime.now(timezone.utc),
        payment_method_id=payment_method_id,
        purchase_id=purchase_id,
        given_by_id=given_by_id,
        notes=(notes or "").strip() or None,
    )
    db.add(adv)
    db.flush()
    return adv


def cancel_advance(db: Session, advance_id: int) -> SalaryAdvance:
    """إلغاء سلفة (تنازل المدير عنها). لا يتم استرداد قيد المصروف."""
    adv = db.get(SalaryAdvance, advance_id)
    if adv is None:
        raise HRError("السلفة غير موجودة.")
    if adv.status == AdvanceStatus.FULLY_REPAID:
        raise HRError("السلفة مسددة بالكامل بالفعل.")
    adv.status = AdvanceStatus.CANCELLED
    db.flush()
    return adv


def delete_advance(db: Session, advance_id: int) -> None:
    """حذف سلفة (يُمنع لو كان قد تم استرداد منها فعلياً)."""
    adv = db.get(SalaryAdvance, advance_id)
    if adv is None:
        return
    if adv.repaid_amount and adv.repaid_amount > 0:
        raise HRError(
            "لا يمكن حذف سلفة تم استرداد جزء منها — يمكنك إلغاؤها بدلاً من ذلك."
        )
    db.delete(adv)
    db.flush()


def repay_advance_manual(
    db: Session,
    *,
    advance_id: int,
    amount: Decimal,
    notes: str | None = None,
) -> AdvanceRepayment:
    """تسجيل استرداد يدوي (الموظف دفع نقداً مثلاً)."""
    adv = db.get(SalaryAdvance, advance_id)
    if adv is None:
        raise HRError("السلفة غير موجودة.")
    if adv.is_settled:
        raise HRError("السلفة مسواة بالفعل.")
    amt = Decimal(str(amount or 0))
    if amt <= 0:
        raise HRError("المبلغ يجب أن يكون أكبر من صفر.")
    if amt > adv.remaining_amount:
        amt = adv.remaining_amount
    rep = AdvanceRepayment(
        advance_id=adv.id,
        amount=amt.quantize(Decimal("0.001")),
        repaid_at=datetime.now(timezone.utc),
        notes=(notes or "").strip() or None,
    )
    db.add(rep)
    adv.repaid_amount = (adv.repaid_amount + amt).quantize(Decimal("0.001"))
    if adv.remaining_amount <= 0:
        adv.status = AdvanceStatus.FULLY_REPAID
    else:
        adv.status = AdvanceStatus.PARTIALLY_REPAID
    db.flush()
    return rep


def apply_advance_deductions(
    db: Session,
    *,
    employee_id: int,
    amount: Decimal,
    payroll_entry_id: int,
    when: datetime | None = None,
) -> list[AdvanceRepayment]:
    """يطبّق خصماً على السلف القائمة لموظف بطريقة FIFO (الأقدم أولاً).

    تُستدعى عند دفع دفعة رواتب: لو خُصم 200 د.ل من راتب موظف وكانت لديه سلفتان
    (الأولى متبقي 150، الثانية متبقي 300) → تُسدَّد الأولى كاملةً (150) ثم يُخصم
    50 من الثانية، ويسجَّل لكل عملية AdvanceRepayment مرتبط بـ payroll_entry_id.
    """
    remaining = Decimal(str(amount or 0))
    if remaining <= 0:
        return []
    advances = list(
        db.scalars(
            select(SalaryAdvance)
            .where(
                SalaryAdvance.employee_id == employee_id,
                SalaryAdvance.status.in_(
                    [AdvanceStatus.OUTSTANDING, AdvanceStatus.PARTIALLY_REPAID]
                ),
            )
            .order_by(SalaryAdvance.given_at.asc())
        ).all()
    )
    when = when or datetime.now(timezone.utc)
    repayments: list[AdvanceRepayment] = []
    for adv in advances:
        if remaining <= 0:
            break
        rem_on_adv = adv.remaining_amount
        if rem_on_adv <= 0:
            continue
        take = min(rem_on_adv, remaining)
        rep = AdvanceRepayment(
            advance_id=adv.id,
            amount=take.quantize(Decimal("0.001")),
            repaid_at=when,
            payroll_entry_id=payroll_entry_id,
        )
        db.add(rep)
        adv.repaid_amount = (adv.repaid_amount + take).quantize(Decimal("0.001"))
        if adv.remaining_amount <= 0:
            adv.status = AdvanceStatus.FULLY_REPAID
        else:
            adv.status = AdvanceStatus.PARTIALLY_REPAID
        remaining -= take
        repayments.append(rep)
    db.flush()
    return repayments
