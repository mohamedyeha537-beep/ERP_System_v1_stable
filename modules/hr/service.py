"""خدمات وحدة الموارد البشرية: موظفون، حضور، رواتب."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.orm import Session, joinedload

from modules.hr.models import (
    AdvanceRepayment,
    AdvanceStatus,
    AttendanceRecord,
    AttendanceSource,
    DeductionRepayment,
    DeductionStatus,
    Employee,
    EmployeeDeduction,
    EmployeeStatus,
    OvertimeApprovalStatus,
    PayType,
    PayrollEntry,
    PayrollRun,
    PayrollStatus,
    SalaryAdvance,
    WorkShift,
)
from modules.hr.pos_pin import hash_pos_pin
from modules.hr.shift_attendance import apply_attendance_metrics, validate_hhmm


class HRError(Exception):
    pass


# =====================================================================
#                          الموظفون
# =====================================================================
def list_employees(
    db: Session,
    *,
    only_active: bool = False,
    department_id: int | None = None,
    domain=None,
) -> list[Employee]:
    from modules.platform.business_domain import BusinessDomain

    filter_domain = domain if isinstance(domain, BusinessDomain) or domain is None else None
    if domain is not None and not isinstance(domain, BusinessDomain):
        try:
            filter_domain = BusinessDomain(str(domain).strip().lower())
        except ValueError:
            filter_domain = None

    from modules.hr.models import HrDepartment

    try:
        stmt = select(Employee).options(joinedload(Employee.department))
        if department_id is None:
            stmt = stmt.outerjoin(
                HrDepartment, Employee.department_id == HrDepartment.id
            ).order_by(
                HrDepartment.sort_order,
                HrDepartment.name_ar,
                Employee.full_name_ar,
            )
        else:
            stmt = stmt.order_by(Employee.full_name_ar)
        if only_active:
            stmt = stmt.where(Employee.status == EmployeeStatus.ACTIVE)
        if department_id is not None:
            if department_id == 0:
                stmt = stmt.where(Employee.department_id.is_(None))
            else:
                stmt = stmt.where(Employee.department_id == department_id)
        if filter_domain is not None:
            stmt = stmt.where(Employee.business_domain == filter_domain.value)
        return list(db.scalars(stmt).unique().all())
    except Exception:
        db.rollback()
        stmt = select(Employee).order_by(Employee.full_name_ar)
        if only_active:
            stmt = stmt.where(Employee.status == EmployeeStatus.ACTIVE)
        if department_id is not None:
            if department_id == 0:
                stmt = stmt.where(Employee.department_id.is_(None))
            else:
                stmt = stmt.where(Employee.department_id == department_id)
        if filter_domain is not None:
            stmt = stmt.where(Employee.business_domain == filter_domain.value)
        return list(db.scalars(stmt).all())


# =====================================================================
#                       أقسام العمل
# =====================================================================
def list_departments(db: Session, *, only_active: bool = False) -> list["HrDepartment"]:
    from modules.hr.models import HrDepartment

    stmt = select(HrDepartment).order_by(
        HrDepartment.sort_order, HrDepartment.name_ar
    )
    if only_active:
        stmt = stmt.where(HrDepartment.is_active.is_(True))
    return list(db.scalars(stmt).all())


def get_department(db: Session, dept_id: int) -> "HrDepartment | None":
    from modules.hr.models import HrDepartment

    return db.get(HrDepartment, dept_id)


def ensure_default_departments(db: Session) -> None:
    """أقسام افتراضية عند أول استخدام."""
    from modules.hr.models import HrDepartment

    if db.scalar(select(func.count()).select_from(HrDepartment)):
        return
    defaults = [
        ("المقهى", "CAFE", 10),
        ("المطعم", "REST", 20),
        ("الاستقبال", "RECEP", 30),
        ("التنظيف", "CLEAN", 40),
        ("الحسابات", "ACCT", 50),
        ("الكاشير", "CASH", 60),
        ("المطبخ", "KITCHEN", 70),
    ]
    for name, code, order in defaults:
        db.add(
            HrDepartment(
                name_ar=name,
                code=code,
                sort_order=order,
                is_active=True,
            )
        )
    db.flush()


def upsert_department(
    db: Session,
    *,
    dept_id: int | None,
    name_ar: str,
    code: str | None = None,
    sort_order: int = 0,
    is_active: bool = True,
    notes: str | None = None,
) -> "HrDepartment":
    from modules.hr.models import HrDepartment

    name = (name_ar or "").strip()
    if not name:
        raise HRError("اسم القسم مطلوب.")
    code_val = (code or "").strip() or None
    if code_val:
        clash = db.execute(
            select(HrDepartment).where(
                HrDepartment.code == code_val,
                HrDepartment.id != (dept_id or -1),
            )
        ).scalar_one_or_none()
        if clash is not None:
            raise HRError(f"رمز القسم «{code_val}» مستخدم لقسم «{clash.name_ar}».")

    if dept_id:
        dept = db.get(HrDepartment, dept_id)
        if dept is None:
            raise HRError("القسم غير موجود.")
    else:
        dept = HrDepartment()
        db.add(dept)

    dept.name_ar = name
    dept.code = code_val
    dept.sort_order = max(0, int(sort_order or 0))
    dept.is_active = bool(is_active)
    dept.notes = (notes or "").strip() or None
    db.flush()
    return dept


def delete_department(db: Session, dept_id: int) -> None:
    from modules.hr.models import HrDepartment

    dept = db.get(HrDepartment, dept_id)
    if dept is None:
        raise HRError("القسم غير موجود.")
    assigned = db.execute(
        select(func.count())
        .select_from(Employee)
        .where(Employee.department_id == dept_id)
    ).scalar_one()
    if assigned and int(assigned) > 0:
        raise HRError(
            f"لا يمكن حذف القسم — {assigned} موظفاً مرتبطاً به. انقلهم لقسم آخر أولاً."
        )
    db.delete(dept)


def department_employee_counts(db: Session) -> dict[int, int]:
    rows = db.execute(
        select(Employee.department_id, func.count())
        .where(Employee.department_id.isnot(None))
        .group_by(Employee.department_id)
    ).all()
    return {int(r[0]): int(r[1]) for r in rows if r[0] is not None}


def get_employee(db: Session, emp_id: int) -> Employee | None:
    return db.get(Employee, emp_id)


def get_employee_by_user_id(db: Session, user_id: int) -> Employee | None:
    return db.execute(
        select(Employee).where(Employee.user_id == user_id).limit(1)
    ).scalar_one_or_none()


def create_pos_cashier_user(
    db: Session,
    *,
    username: str,
    password: str,
) -> "User":
    """إنشاء مستخدم بدور كاشير لربطه بموظف نقطة البيع."""
    from modules.authz.models import Role, User
    from modules.authz.service import get_cashier_role, get_user_by_username, hash_password

    name = (username or "").strip()
    if len(name) < 2:
        raise HRError("اسم مستخدم الحساب مطلوب (حرفان على الأقل).")
    if len((password or "").strip()) < 4:
        raise HRError("كلمة مرور الحساب قصيرة جداً (4 أحرف على الأقل).")
    if get_user_by_username(db, name) is not None:
        raise HRError(f"اسم المستخدم «{name}» مستخدم مسبقاً.")
    role = get_cashier_role(db)
    if role is None:
        raise HRError("دور «كاشير» غير موجود في النظام.")
    u = User(
        username=name,
        password_hash=hash_password(password.strip()),
        is_active=True,
        roles=[role],
        view_scope="restaurant",
    )
    db.add(u)
    db.flush()
    return u


def set_employee_pos_pin(
    db: Session,
    emp: Employee,
    *,
    pin_plain: str | None,
    clear_pin: bool = False,
    is_pos_cashier: bool | None = None,
) -> None:
    if is_pos_cashier is not None:
        emp.is_pos_cashier = bool(is_pos_cashier)
    if clear_pin:
        emp.pos_pin_hash = None
        return
    if pin_plain is not None and (pin_plain or "").strip():
        try:
            emp.pos_pin_hash = hash_pos_pin(pin_plain)
        except ValueError as e:
            raise HRError(str(e)) from e
        if is_pos_cashier is not False and not getattr(emp, "is_hotel_front", False):
            emp.is_pos_cashier = True


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
    is_pos_cashier: bool = False,
    is_hotel_front: bool = False,
    is_pos_supervisor: bool = False,
    work_shift_id: int | None = None,
    department_id: int | None = None,
    zk_emp_code: str | None = None,
    business_domain: str | None = None,
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
    emp.is_pos_cashier = bool(is_pos_cashier)
    emp.is_hotel_front = bool(is_hotel_front)
    emp.is_pos_supervisor = bool(is_pos_supervisor)
    emp.notes = (notes or "").strip() or None
    if work_shift_id is not None and work_shift_id <= 0:
        work_shift_id = None
    if work_shift_id is not None:
        shift = db.get(WorkShift, work_shift_id)
        if shift is None:
            raise HRError("الوردية المختارة غير موجودة.")
        emp.work_shift_id = work_shift_id
        emp.standard_hours_per_day = Decimal(str(shift.work_hours)).quantize(
            Decimal("0.01")
        )
    else:
        emp.work_shift_id = None

    if department_id is not None and department_id <= 0:
        department_id = None
    if department_id is not None:
        from modules.hr.models import HrDepartment

        dept = db.get(HrDepartment, department_id)
        if dept is None:
            raise HRError("القسم المختار غير موجود.")
        emp.department_id = department_id
    else:
        emp.department_id = None

    zk_code = (zk_emp_code or "").strip() or None
    if zk_code:
        clash = db.execute(
            select(Employee).where(
                Employee.zk_emp_code == zk_code,
                Employee.id != (emp.id if emp.id else -1),
            )
        ).scalar_one_or_none()
        if clash is not None:
            raise HRError(
                f"كود البصمة «{zk_code}» مستخدم لموظف آخر: {clash.full_name_ar}"
            )
    emp.zk_emp_code = zk_code

    from modules.platform.business_domain import parse_employee_domain

    emp.business_domain = parse_employee_domain(business_domain).value
    if emp.is_pos_cashier and emp.business_domain != parse_employee_domain("restaurant").value:
        raise HRError("كاشير نقطة البيع يجب أن يكون في مجال المطعم.")
    if emp.is_hotel_front and emp.business_domain != parse_employee_domain("hotel").value:
        raise HRError("موظف استقبال الفندق يجب أن يكون في مجال الفندق.")

    if status == EmployeeStatus.TERMINATED and emp.terminated_at is None:
        emp.terminated_at = datetime.now(timezone.utc)
    elif status != EmployeeStatus.TERMINATED:
        emp.terminated_at = None
    db.flush()
    return emp


def delete_employee(db: Session, emp_id: int) -> None:
    """حذف موظف مع تنظيف سجلات HR المرتبطة (حضور، رواتب غير مُقفلة محاسبياً، سلف، خصومات)."""
    emp = db.get(Employee, emp_id)
    if emp is None:
        return

    paid_entries = list(
        db.scalars(
            select(PayrollEntry).where(
                PayrollEntry.employee_id == emp_id,
                PayrollEntry.paid_purchase_id.isnot(None),
            )
        ).all()
    )
    if paid_entries:
        raise HRError(
            "لا يمكن حذف الموظف: له رواتب مُصروفة مسجّلة في المحاسبة. "
            "من «الرواتب» أعد فتح الدفعة أو احذفها أولاً، أو أوقف خدمة الموظف بدلاً من الحذف."
        )

    entry_ids = list(
        db.scalars(
            select(PayrollEntry.id).where(PayrollEntry.employee_id == emp_id)
        ).all()
    )
    if entry_ids:
        db.execute(
            update(AdvanceRepayment)
            .where(AdvanceRepayment.payroll_entry_id.in_(entry_ids))
            .values(payroll_entry_id=None)
        )
        db.execute(
            delete(DeductionRepayment).where(
                DeductionRepayment.payroll_entry_id.in_(entry_ids)
            )
        )
        db.execute(
            delete(PayrollEntry).where(PayrollEntry.employee_id == emp_id)
        )

    ded_ids = list(
        db.scalars(
            select(EmployeeDeduction.id).where(
                EmployeeDeduction.employee_id == emp_id
            )
        ).all()
    )
    if ded_ids:
        db.execute(
            delete(DeductionRepayment).where(
                DeductionRepayment.deduction_id.in_(ded_ids)
            )
        )
        db.execute(
            delete(EmployeeDeduction).where(EmployeeDeduction.employee_id == emp_id)
        )

    advances = list(
        db.scalars(
            select(SalaryAdvance).where(SalaryAdvance.employee_id == emp_id)
        ).all()
    )
    if advances:
        from modules.payments.service import delete_purchase

        for adv in advances:
            db.execute(
                delete(AdvanceRepayment).where(
                    AdvanceRepayment.advance_id == adv.id
                )
            )
            if adv.purchase_id:
                try:
                    delete_purchase(db, adv.purchase_id)
                except Exception:
                    adv.purchase_id = None
            db.delete(adv)

    # سجلات قد تمنع الحذف إن لم تُضبط CASCADE في قاعدة البيانات القديمة
    from modules.hr.models import (
        EmployeeDaySchedule,
        EmployeeMealRedemption,
        EmployeeMealWallet,
    )

    db.execute(
        delete(AttendanceRecord).where(AttendanceRecord.employee_id == emp_id)
    )
    db.execute(
        delete(EmployeeDaySchedule).where(EmployeeDaySchedule.employee_id == emp_id)
    )
    db.execute(
        delete(EmployeeMealRedemption).where(
            EmployeeMealRedemption.employee_id == emp_id
        )
    )
    db.execute(
        delete(EmployeeMealWallet).where(EmployeeMealWallet.employee_id == emp_id)
    )

    try:
        from modules.pos_shifts.models import PosShift, PosShiftExpense

        db.execute(
            update(PosShift)
            .where(PosShift.employee_id == emp_id)
            .values(employee_id=None)
        )
        if hasattr(PosShiftExpense, "employee_id"):
            db.execute(
                update(PosShiftExpense)
                .where(PosShiftExpense.employee_id == emp_id)
                .values(employee_id=None)
            )
    except Exception:
        pass

    db.delete(emp)
    db.flush()


def total_active_monthly_salaries(db: Session, domain=None) -> Decimal:
    """مجموع الرواتب الشهرية الأساسية للموظفين النشطين."""
    from modules.platform.business_domain import BusinessDomain

    filter_domain = domain if isinstance(domain, BusinessDomain) or domain is None else None
    if domain is not None and not isinstance(domain, BusinessDomain):
        try:
            filter_domain = BusinessDomain(str(domain).strip().lower())
        except ValueError:
            filter_domain = None

    stmt = select(func.coalesce(func.sum(Employee.base_monthly_salary), 0)).where(
        Employee.status == EmployeeStatus.ACTIVE
    )
    if filter_domain is not None:
        stmt = stmt.where(Employee.business_domain == filter_domain.value)
    total = db.execute(stmt).scalar_one()
    return Decimal(str(total or 0)).quantize(Decimal("0.001"))


# =====================================================================
#                       ورديات العمل
# =====================================================================
def list_work_shifts(db: Session, *, only_active: bool = False) -> list[WorkShift]:
    stmt = select(WorkShift).order_by(WorkShift.name_ar)
    if only_active:
        stmt = stmt.where(WorkShift.is_active.is_(True))
    return list(db.scalars(stmt).all())


def get_work_shift(db: Session, shift_id: int) -> WorkShift | None:
    return db.get(WorkShift, shift_id)


def upsert_work_shift(
    db: Session,
    *,
    shift_id: int | None,
    name_ar: str,
    code: str | None,
    start_time: str,
    end_time: str,
    work_hours: Decimal,
    grace_minutes: int,
    is_active: bool = True,
    notes: str | None = None,
) -> WorkShift:
    name = (name_ar or "").strip()
    if not name:
        raise HRError("اسم الوردية مطلوب.")
    try:
        start_time = validate_hhmm(start_time)
        end_time = validate_hhmm(end_time)
    except ValueError as e:
        raise HRError(str(e)) from e
    wh = Decimal(str(work_hours or 0)).quantize(Decimal("0.01"))
    if wh <= 0 or wh > Decimal("24"):
        raise HRError("ساعات العمل يجب أن تكون بين 0.25 و 24.")
    grace = max(0, min(int(grace_minutes or 0), 180))

    if shift_id:
        shift = db.get(WorkShift, shift_id)
        if shift is None:
            raise HRError("الوردية غير موجودة.")
    else:
        shift = WorkShift()
        db.add(shift)

    shift.name_ar = name
    shift.code = (code or "").strip() or None
    shift.start_time = start_time
    shift.end_time = end_time
    shift.work_hours = wh
    shift.grace_minutes = grace
    shift.is_active = bool(is_active)
    shift.notes = (notes or "").strip() or None
    db.flush()
    return shift


def delete_work_shift(db: Session, shift_id: int) -> None:
    shift = db.get(WorkShift, shift_id)
    if shift is None:
        raise HRError("الوردية غير موجودة.")
    assigned = db.execute(
        select(func.count())
        .select_from(Employee)
        .where(Employee.work_shift_id == shift_id)
    ).scalar_one()
    if assigned and int(assigned) > 0:
        raise HRError(
            f"لا يمكن حذف الوردية — {assigned} موظفاً مرتبطاً بها. عيّن وردية أخرى أولاً."
        )
    db.delete(shift)


def _resolve_shift_for_record(db: Session, rec: AttendanceRecord) -> WorkShift | None:
    sid = rec.work_shift_id
    if sid is None:
        emp = db.get(Employee, rec.employee_id)
        sid = emp.work_shift_id if emp else None
    if sid is None:
        return None
    return db.get(WorkShift, sid)


def finalize_attendance_record(db: Session, rec: AttendanceRecord) -> None:
    """يُثبّت الوردية ويحسب التأخير/الإضافي بعد اكتمال الجلسة."""
    from modules.hr.schedule import resolve_effective_schedule

    if rec.work_shift_id is None:
        emp = db.get(Employee, rec.employee_id)
        if emp is not None:
            rec.work_shift_id = emp.work_shift_id
    shift = _resolve_shift_for_record(db, rec)
    schedule = resolve_effective_schedule(
        db,
        employee_id=rec.employee_id,
        check_in=rec.check_in,
        work_shift=shift,
    )
    emp = db.get(Employee, rec.employee_id)
    fallback = float(emp.standard_hours_per_day or 8) if emp else None
    apply_attendance_metrics(
        rec,
        schedule,
        fallback_work_hours=fallback if schedule is None else None,
    )


def approve_overtime(
    db: Session,
    rec_id: int,
    *,
    approved_minutes: int | None = None,
    approved_by_id: int | None = None,
) -> AttendanceRecord:
    rec = db.get(AttendanceRecord, rec_id)
    if rec is None or rec.check_out is None:
        raise HRError("سجل الحضور غير موجود أو الجلسة لم تُغلق بعد.")
    if int(rec.overtime_minutes or 0) <= 0:
        raise HRError("لا توجد ساعات إضافية في هذا السجل.")
    mins = (
        int(approved_minutes)
        if approved_minutes is not None
        else int(rec.overtime_minutes or 0)
    )
    if mins < 0:
        raise HRError("دقائق الإضافي غير صالحة.")
    rec.approved_overtime_minutes = mins
    rec.overtime_approval_status = OvertimeApprovalStatus.APPROVED
    rec.overtime_approved_by_id = approved_by_id
    rec.overtime_approved_at = datetime.now(timezone.utc)
    db.flush()
    return rec


def reject_overtime(db: Session, rec_id: int, *, approved_by_id: int | None = None) -> AttendanceRecord:
    rec = db.get(AttendanceRecord, rec_id)
    if rec is None or rec.check_out is None:
        raise HRError("سجل الحضور غير موجود أو الجلسة لم تُغلق بعد.")
    rec.approved_overtime_minutes = 0
    rec.overtime_approval_status = OvertimeApprovalStatus.REJECTED
    rec.overtime_approved_by_id = approved_by_id
    rec.overtime_approved_at = datetime.now(timezone.utc)
    db.flush()
    return rec


def adjust_attendance_record(
    db: Session,
    rec_id: int,
    *,
    check_in: datetime | None = None,
    check_out: datetime | None = None,
    work_hours: Decimal | None = None,
    use_manual_times: bool = False,
    override_metrics: bool = False,
    late_minutes: int | None = None,
    overtime_minutes: int | None = None,
    notes: str | None = None,
) -> AttendanceRecord:
    """تعديل يدوي — ساعات العمل (يُحدَّث الخروج) أو أوقات دخول/خروج أو تأخير/إضافي."""
    rec = db.get(AttendanceRecord, rec_id)
    if rec is None:
        raise HRError("سجل الحضور غير موجود.")
    if notes is not None:
        rec.notes = (notes or "").strip() or None

    used_work_hours = False

    if use_manual_times:
        if check_in is not None:
            rec.check_in = check_in
        if check_out is not None:
            rec.check_out = check_out
        if rec.check_out is None:
            raise HRError("أدخل وقت الخروج.")
        if rec.check_out <= rec.check_in:
            raise HRError("وقت الخروج يجب أن يكون بعد وقت الدخول.")
        finalize_attendance_record(db, rec)
    elif work_hours is not None:
        wh = Decimal(str(work_hours)).quantize(Decimal("0.01"))
        if wh <= 0:
            raise HRError("ساعات العمل يجب أن تكون أكبر من صفر.")
        if check_in is not None:
            rec.check_in = check_in
        cin = rec.check_in
        if cin.tzinfo is None:
            cin = cin.replace(tzinfo=timezone.utc)
        else:
            cin = cin.astimezone(timezone.utc)
        rec.check_out = cin + timedelta(hours=float(wh))
        used_work_hours = True
        finalize_attendance_record(db, rec)
    elif check_in is not None or check_out is not None:
        if check_in is not None:
            rec.check_in = check_in
        if check_out is not None:
            rec.check_out = check_out
        if rec.check_out is None or rec.check_out <= rec.check_in:
            raise HRError("وقت الخروج يجب أن يكون بعد وقت الدخول.")
        finalize_attendance_record(db, rec)
    else:
        raise HRError("أدخل ساعات العمل أو عدّل وقت الدخول/الخروج.")

    rec.source = AttendanceSource.MANAGER

    apply_manual_metrics = override_metrics or not used_work_hours
    if late_minutes is not None and apply_manual_metrics:
        rec.late_minutes = max(0, int(late_minutes))
    if overtime_minutes is not None and apply_manual_metrics:
        rec.overtime_minutes = max(0, int(overtime_minutes))
        if rec.overtime_minutes > 0:
            if rec.overtime_approval_status == OvertimeApprovalStatus.APPROVED:
                rec.approved_overtime_minutes = rec.overtime_minutes
            else:
                rec.overtime_approval_status = OvertimeApprovalStatus.PENDING
                rec.approved_overtime_minutes = 0
        else:
            rec.overtime_approval_status = OvertimeApprovalStatus.NONE
            rec.approved_overtime_minutes = 0
    db.flush()
    return rec


def approved_overtime_minutes_in_period(
    db: Session,
    start: datetime,
    end: datetime,
    *,
    employee_id: int | None = None,
) -> int:
    """مجموع دقائق الإضافي المعتمدة خلال الفترة."""
    stmt = select(AttendanceRecord).where(
        AttendanceRecord.check_out.isnot(None),
        AttendanceRecord.overtime_approval_status == OvertimeApprovalStatus.APPROVED,
        AttendanceRecord.check_in >= start,
        AttendanceRecord.check_in < end,
    )
    if employee_id is not None:
        stmt = stmt.where(AttendanceRecord.employee_id == employee_id)
    rows = db.scalars(stmt).all()
    return sum(int(r.approved_overtime_minutes or 0) for r in rows)


def overtime_summary(
    db: Session, *, employee_id: int | None = None, now: datetime | None = None
) -> dict[str, Decimal]:
    """إجمالي ساعات الإضافي المعتمدة: اليوم، الأسبوع، الشهر."""
    now = now or datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = day_start - timedelta(days=day_start.weekday())
    month_start = day_start.replace(day=1)

    def _hours(start: datetime) -> Decimal:
        mins = approved_overtime_minutes_in_period(
            db, start, now + timedelta(days=1), employee_id=employee_id
        )
        return (Decimal(str(mins)) / Decimal("60")).quantize(Decimal("0.01"))

    return {
        "today_hours": _hours(day_start),
        "week_hours": _hours(week_start),
        "month_hours": _hours(month_start),
    }


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
        work_shift_id=emp.work_shift_id,
    )
    db.add(rec)
    db.flush()
    try:
        from modules.notifications.hr_hooks import emit_attendance_check_in

        emit_attendance_check_in(db, rec)
    except Exception:  # noqa: BLE001
        pass
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
    finalize_attendance_record(db, rec)
    db.flush()
    try:
        from modules.notifications.hr_hooks import emit_attendance_check_out

        emit_attendance_check_out(db, rec)
    except Exception:  # noqa: BLE001
        pass
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


def overtime_hours_in_period(
    db: Session, employee_id: int, start: datetime, end: datetime
) -> Decimal:
    """مجموع ساعات الإضافي المسجَّلة (من الورديات) خلال الفترة."""
    recs = list_attendance(
        db, employee_id=employee_id, start=start, end=end, limit=10_000
    )
    total_min = 0
    start = _ensure_aware(start)
    end = _ensure_aware(end)
    for r in recs:
        if r.check_out is None:
            continue
        rin = _ensure_aware(r.check_in)
        if rin < start or rin >= end:
            continue
        total_min += int(r.payroll_overtime_minutes)
    return (Decimal(str(total_min)) / Decimal("60")).quantize(Decimal("0.01"))


def late_minutes_in_period(
    db: Session, employee_id: int, start: datetime, end: datetime
) -> int:
    recs = list_attendance(
        db, employee_id=employee_id, start=start, end=end, limit=10_000
    )
    total = 0
    start = _ensure_aware(start)
    end = _ensure_aware(end)
    for r in recs:
        if r.check_out is None:
            continue
        rin = _ensure_aware(r.check_in)
        if rin < start or rin >= end:
            continue
        total += int(r.late_minutes or 0)
    return total


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


def list_payroll_runs(
    db: Session, limit: int = 24, domain=None
) -> list[PayrollRun]:
    from modules.platform.business_domain import BusinessDomain

    stmt = (
        select(PayrollRun)
        .order_by(PayrollRun.period_year.desc(), PayrollRun.period_month.desc())
        .limit(limit)
    )
    if isinstance(domain, BusinessDomain):
        stmt = stmt.where(PayrollRun.business_domain == domain.value)
    elif isinstance(domain, str) and domain.strip():
        stmt = stmt.where(PayrollRun.business_domain == domain.strip().lower())
    return list(db.scalars(stmt).all())


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
        ot_hours = overtime_hours_in_period(db, emp.id, s, e)
        if ot_hours <= 0:
            sph = Decimal(str(emp.standard_hours_per_day or 8))
            std_total = sph * Decimal(str(days_in_month))
            if h_actual > std_total:
                ot_hours = (h_actual - std_total).quantize(Decimal("0.01"))
        if ot_hours > 0:
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


def _suggested_payroll_withholdings(
    db: Session, employee_id: int, gross: Decimal
) -> tuple[Decimal, Decimal]:
    """سلف وخصومات مقترحة لبند راتب (بحد أقصى الإجمالي القابل للخصم)."""
    gross = gross.quantize(Decimal("0.001"))
    outstanding_adv = outstanding_advances_for(db, employee_id)
    suggested_adv = min(outstanding_adv, gross)
    remaining = (gross - suggested_adv).quantize(Decimal("0.001"))
    out_ded = outstanding_deductions_for(db, employee_id)
    suggested_ded = min(out_ded, remaining)
    return (
        suggested_adv.quantize(Decimal("0.001")),
        suggested_ded.quantize(Decimal("0.001")),
    )


def pending_deduction_lines_for(
    db: Session, employee_id: int
) -> list[dict[str, object]]:
    """بنود خصم مستحقة (عجز جلسات، جزاءات، …) لعرضها في صفحة الرواتب."""
    rows = list_employee_deductions(
        db, employee_id=employee_id, only_outstanding=True, limit=50
    )
    lines: list[dict[str, object]] = []
    seen: set[tuple[str, int | None]] = set()
    for d in rows:
        amt = Decimal(str(d.amount or 0))
        rep = Decimal(str(d.repaid_amount or 0))
        remaining = (amt - rep).quantize(Decimal("0.001"))
        if remaining <= 0:
            continue
        st = (d.source_type or "").strip()
        sid = d.source_id
        key = (st, sid) if st and sid is not None else ("id", d.id)
        if key in seen:
            continue
        seen.add(key)
        lines.append(
            {
                "id": d.id,
                "remaining": remaining,
                "note": (d.note or "").strip() or "خصم مستحق",
                "source_type": st,
            }
        )
    return lines


def _run_allows_editing(status: PayrollStatus) -> bool:
    return status in (PayrollStatus.DRAFT, PayrollStatus.POSTED)


def reconcile_draft_run_withholdings(
    db: Session, run: PayrollRun, *, full: bool = False
) -> int:
    """يحدّث السلف والاستقطاعات في دفعة رواتب (مسودة أو معتمدة) من المستحقات.

    full=False: يرفع الخصم/السلف فقط إذا كان المدخل أقل من المستحق (لا يمسّ التعديل اليدوي الأعلى).
    full=True: يعيد ضبط البنود بالكامل كما عند إنشاء الدفعة.
    """
    if not _run_allows_editing(run.status):
        return 0
    changed = 0
    for entry in run.entries:
        gross = entry.gross_pay
        sug_adv, sug_ded = _suggested_payroll_withholdings(db, entry.employee_id, gross)
        if full:
            new_adv, new_ded = sug_adv, sug_ded
        else:
            new_adv = max(entry.advances or Decimal("0"), sug_adv)
            new_ded = max(entry.deductions or Decimal("0"), sug_ded)
        new_net = (gross - new_adv - new_ded).quantize(Decimal("0.001"))
        if (
            new_adv != (entry.advances or Decimal("0"))
            or new_ded != (entry.deductions or Decimal("0"))
            or new_net != (entry.net_pay or Decimal("0"))
        ):
            entry.advances = new_adv
            entry.deductions = new_ded
            entry.net_pay = new_net
            changed += 1
    if changed:
        db.flush()
    return changed


def create_payroll_run(
    db: Session,
    *,
    year: int,
    month: int,
    user_id: int | None = None,
    business_domain: str | None = None,
) -> PayrollRun:
    """ينشئ دفعة رواتب لشهر/سنة ويملؤها بموظفي المجال النشط."""
    from modules.platform.business_domain import BusinessDomain, parse_employee_domain

    if not (1 <= month <= 12):
        raise HRError("الشهر غير صالح.")
    dom = parse_employee_domain(business_domain or BusinessDomain.RESTAURANT.value)
    existing = db.execute(
        select(PayrollRun).where(
            PayrollRun.period_year == year,
            PayrollRun.period_month == month,
            PayrollRun.business_domain == dom.value,
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HRError(
            f"دفعة رواتب {dom.value} موجودة بالفعل لشهر {year}/{month:02d}."
        )

    run = PayrollRun(
        period_year=year,
        period_month=month,
        status=PayrollStatus.DRAFT,
        created_by_id=user_id,
        business_domain=dom.value,
    )
    db.add(run)
    db.flush()

    for emp in list_employees(db, only_active=True, domain=dom):
        c = _calc_for_employee(db, emp, year, month)
        gross = (c.base_salary + c.overtime_pay).quantize(Decimal("0.001"))
        suggested_adv, suggested_ded = _suggested_payroll_withholdings(
            db, emp.id, gross
        )
        net = (gross - suggested_adv - suggested_ded).quantize(Decimal("0.001"))
        entry = PayrollEntry(
            run_id=run.id,
            employee_id=emp.id,
            base_salary=c.base_salary,
            hours_worked=c.hours_worked,
            overtime_hours=c.overtime_hours,
            overtime_pay=c.overtime_pay,
            bonuses=Decimal("0"),
            deductions=suggested_ded,
            advances=suggested_adv,
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
    if not _run_allows_editing(entry.run.status):
        raise HRError(
            "لا يمكن تعديل بند ضمن دفعة مدفوعة. أعد فتح الدفعة كمسودة من إجراءات الصفحة."
        )
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
    from modules.gl.posting import post_payroll_accrual_shadow_safe

    post_payroll_accrual_shadow_safe(db, run)
    try:
        from modules.notifications.hr_hooks import emit_payroll_posted

        emit_payroll_posted(db, run)
    except Exception:  # noqa: BLE001
        pass
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
        run.status = PayrollStatus.POSTED
        run.posted_at = datetime.now(timezone.utc)

    from modules.gl.posting import (
        post_payroll_accrual_shadow_safe,
        post_payroll_payment_shadow_safe,
    )

    post_payroll_accrual_shadow_safe(db, run)

    # تطبيق السلف والخصومات المستحقة قبل التحقق والدفع
    reconcile_draft_run_withholdings(db, run, full=True)

    # تحقق قبل الدفع: لا تسمح بتجاوز خصومات مستحقة (عجز/جزاءات) بصرف راتب كامل
    for e in run.entries:
        gross = e.gross_pay
        remaining = (gross - (e.advances or Decimal("0"))).quantize(Decimal("0.001"))
        out_deductions = outstanding_deductions_for(db, e.employee_id)
        required = min(out_deductions, remaining)
        if required > 0 and (e.deductions or Decimal("0")) + Decimal("0.0005") < required:
            raise HRError(
                "لا يمكن دفع الدفعة: يوجد خصم مستحق (عجز/جزاءات) غير مطبّق.\n"
                f"الموظف: #{e.employee_id}\n"
                f"المطلوب خصمه الآن: {required}\n"
                f"الخصم المدخل: {e.deductions or Decimal('0')}\n"
                f"صافي الراتب الصحيح = الإجمالي − السلف − الخصومات."
            )

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
        business_domain=getattr(run, "business_domain", None) or "restaurant",
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
        # تسجيل استرداد الخصومات المستحقة (FIFO على الأقدم)
        if e.deductions and e.deductions > 0:
            apply_employee_deductions(
                db,
                employee_id=e.employee_id,
                amount=Decimal(str(e.deductions)),
                payroll_entry_id=e.id,
                when=now,
            )
    run.status = PayrollStatus.PAID
    run.paid_at = now
    db.flush()
    post_payroll_payment_shadow_safe(
        db,
        run,
        payment_method_id=payment_method_id,
        purchase_id=purchase.id,
    )
    try:
        from modules.notifications.hr_hooks import notify_payroll_run_paid

        notify_payroll_run_paid(db, run)
    except Exception:  # noqa: BLE001
        pass
    return run


def _undo_payroll_entry_settlements(db: Session, entry: PayrollEntry) -> None:
    """يلغي تسجيلات سداد السلف/الخصومات المرتبطة ببند راتب مدفوع."""
    for rep in list(
        db.scalars(
            select(DeductionRepayment).where(
                DeductionRepayment.payroll_entry_id == entry.id
            )
        ).all()
    ):
        d = db.get(EmployeeDeduction, rep.deduction_id)
        if d is not None:
            d.repaid_amount = (
                Decimal(str(d.repaid_amount or 0)) - Decimal(str(rep.amount or 0))
            ).quantize(Decimal("0.001"))
            if d.repaid_amount < 0:
                d.repaid_amount = Decimal("0")
            if d.repaid_amount <= 0:
                d.status = DeductionStatus.OUTSTANDING
                d.resolved_at = None
            elif d.remaining_amount > 0:
                d.status = DeductionStatus.PARTIALLY_REPAID
                d.resolved_at = None
        db.delete(rep)
    for rep in list(
        db.scalars(
            select(AdvanceRepayment).where(
                AdvanceRepayment.payroll_entry_id == entry.id
            )
        ).all()
    ):
        adv = db.get(SalaryAdvance, rep.advance_id)
        if adv is not None:
            adv.repaid_amount = (
                Decimal(str(adv.repaid_amount or 0)) - Decimal(str(rep.amount or 0))
            ).quantize(Decimal("0.001"))
            if adv.repaid_amount < 0:
                adv.repaid_amount = Decimal("0")
            if adv.repaid_amount <= 0:
                adv.status = AdvanceStatus.OUTSTANDING
            elif adv.remaining_amount > 0:
                adv.status = AdvanceStatus.PARTIALLY_REPAID
            else:
                adv.status = AdvanceStatus.FULLY_REPAID
        db.delete(rep)
    db.flush()


def reopen_paid_run_to_draft(db: Session, run_id: int) -> PayrollRun:
    """يعيد دفعة مدفوعة إلى مسودة (لتصحيح الخصومات) ويلغي قيد المصروف."""
    from modules.payments.service import delete_purchase

    run = db.get(PayrollRun, run_id)
    if run is None:
        raise HRError("الدفعة غير موجودة.")
    if run.status != PayrollStatus.PAID:
        raise HRError("إعادة الفتح متاحة فقط للدفعات المدفوعة.")
    purchase_ids = {
        int(e.paid_purchase_id)
        for e in run.entries
        if e.paid_purchase_id is not None
    }
    for entry in run.entries:
        _undo_payroll_entry_settlements(db, entry)
        entry.paid_purchase_id = None
        entry.paid_at = None
    for pid in purchase_ids:
        delete_purchase(db, pid)
    run.status = PayrollStatus.DRAFT
    run.posted_at = None
    run.paid_at = None
    db.flush()
    from modules.gl.reversal import reverse_payroll_run_gl_safe

    reverse_payroll_run_gl_safe(db, int(run.id))
    return run


def delete_run(db: Session, run_id: int) -> None:
    run = db.get(PayrollRun, run_id)
    if run is None:
        return
    if run.status == PayrollStatus.PAID:
        raise HRError("لا يمكن حذف دفعة مدفوعة (لها قيد مصروف مرتبط).")
    if run.status == PayrollStatus.POSTED:
        from modules.gl.reversal import reverse_payroll_run_gl_safe

        reverse_payroll_run_gl_safe(
            db, int(run.id), reverse_payment=False, reverse_accrual=True
        )
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
    try:
        from modules.notifications.hr_hooks import emit_advance_given

        emit_advance_given(db, adv)
    except Exception:  # noqa: BLE001
        pass
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


def list_employee_deductions(
    db: Session,
    *,
    employee_id: int | None = None,
    only_outstanding: bool = False,
    limit: int = 500,
) -> list[EmployeeDeduction]:
    stmt = select(EmployeeDeduction).order_by(EmployeeDeduction.id.desc())
    if employee_id is not None:
        stmt = stmt.where(EmployeeDeduction.employee_id == employee_id)
    if only_outstanding:
        stmt = stmt.where(
            EmployeeDeduction.status.in_(
                [DeductionStatus.OUTSTANDING, DeductionStatus.PARTIALLY_REPAID]
            )
        )
    stmt = stmt.limit(limit)
    return list(db.scalars(stmt).all())


def outstanding_deductions_for(db: Session, employee_id: int) -> Decimal:
    rows = db.execute(
        select(EmployeeDeduction.amount, EmployeeDeduction.repaid_amount).where(
            EmployeeDeduction.employee_id == employee_id,
            EmployeeDeduction.status.in_(
                [DeductionStatus.OUTSTANDING, DeductionStatus.PARTIALLY_REPAID]
            ),
        )
    ).all()
    total = Decimal("0")
    for amt, rep in rows:
        a = Decimal(str(amt or 0))
        r = Decimal(str(rep or 0))
        total += (a - r)
    return total.quantize(Decimal("0.001"))


def create_employee_deduction(
    db: Session,
    *,
    employee_id: int,
    amount: Decimal,
    note: str | None,
    source_type: str | None = None,
    source_id: int | None = None,
    resolved_by_id: int | None = None,
) -> EmployeeDeduction:
    amt = Decimal(str(amount)).quantize(Decimal("0.001"))
    if amt <= 0:
        raise HRError("قيمة الخصم يجب أن تكون أكبر من صفر.")
    st = (source_type or "").strip() or None
    if st and source_id is not None:
        dup = db.execute(
            select(EmployeeDeduction.id).where(
                EmployeeDeduction.employee_id == employee_id,
                EmployeeDeduction.source_type == st,
                EmployeeDeduction.source_id == source_id,
                EmployeeDeduction.status != DeductionStatus.CANCELLED,
            )
        ).scalar_one_or_none()
        if dup is not None:
            raise HRError("خصم مسجّل مسبقاً لهذا المصدر (لا يمكن تكراره).")
    d = EmployeeDeduction(
        employee_id=employee_id,
        amount=amt,
        repaid_amount=Decimal("0"),
        status=DeductionStatus.OUTSTANDING,
        source_type=st,
        source_id=source_id,
        note=(note or "").strip() or None,
        resolved_by_id=resolved_by_id,
    )
    db.add(d)
    db.flush()
    try:
        from modules.notifications.hr_hooks import emit_deduction_created

        emit_deduction_created(db, d)
    except Exception:  # noqa: BLE001
        pass
    return d


def cancel_employee_deduction(
    db: Session,
    *,
    deduction_id: int,
    by_user_id: int | None = None,
    note: str | None = None,
) -> EmployeeDeduction:
    d = db.get(EmployeeDeduction, deduction_id)
    if d is None:
        raise HRError("الخصم غير موجود.")
    if d.status == DeductionStatus.FULLY_REPAID:
        raise HRError("لا يمكن العفو: الخصم مُسدّد بالفعل.")
    d.status = DeductionStatus.CANCELLED
    d.resolved_at = datetime.now(timezone.utc)
    d.resolved_by_id = by_user_id
    if note is not None:
        d.note = (note or "").strip() or None
    db.flush()
    return d


def apply_employee_deductions(
    db: Session,
    *,
    employee_id: int,
    amount: Decimal,
    payroll_entry_id: int | None,
    when: datetime | None = None,
) -> list[DeductionRepayment]:
    """يطبّق خصماً على الخصومات القائمة لموظف بطريقة FIFO (الأقدم أولاً)."""
    remaining = Decimal(str(amount or 0)).quantize(Decimal("0.001"))
    if remaining <= 0:
        return []
    when = when or datetime.now(timezone.utc)
    deductions = list(
        db.scalars(
            select(EmployeeDeduction)
            .where(
                EmployeeDeduction.employee_id == employee_id,
                EmployeeDeduction.status.in_(
                    [DeductionStatus.OUTSTANDING, DeductionStatus.PARTIALLY_REPAID]
                ),
            )
            .order_by(EmployeeDeduction.created_at.asc(), EmployeeDeduction.id.asc())
        ).all()
    )
    reps: list[DeductionRepayment] = []
    for d in deductions:
        if remaining <= 0:
            break
        rem = d.remaining_amount
        if rem <= 0:
            d.status = DeductionStatus.FULLY_REPAID
            continue
        take = min(rem, remaining)
        rep = DeductionRepayment(
            deduction_id=d.id,
            amount=take.quantize(Decimal("0.001")),
            repaid_at=when,
            payroll_entry_id=payroll_entry_id,
            note=None,
        )
        db.add(rep)
        d.repaid_amount = (Decimal(str(d.repaid_amount or 0)) + take).quantize(
            Decimal("0.001")
        )
        if d.remaining_amount <= 0:
            d.status = DeductionStatus.FULLY_REPAID
            d.resolved_at = when
        else:
            d.status = DeductionStatus.PARTIALLY_REPAID
        remaining -= take
        reps.append(rep)
    db.flush()
    return reps
