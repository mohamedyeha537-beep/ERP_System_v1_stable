"""واجهات وحدة الموارد البشرية:
- /admin/employees       — قائمة وإدارة الموظفين
- /admin/attendance      — تسجيل الحضور والانصراف
- /admin/payroll         — قائمة دفعات الرواتب
- /admin/payroll/{id}    — تفاصيل دفعة (تعديل/اعتماد/دفع)
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.deps import DBSession, require_permission
from app.jinja_env import templates
from modules.authz.models import User
from modules.authz.permissions import HR_ATTENDANCE, HR_MANAGE, HR_VIEW
from modules.hr import service as hr
from modules.hr.models import (
    AttendanceSource,
    EmployeeStatus,
    PayType,
    PayrollStatus,
)
from modules.payments.service import list_payment_methods


_view_perm = require_permission(HR_VIEW)
_manage_perm = require_permission(HR_MANAGE)
_attend_perm = require_permission(HR_ATTENDANCE)


def _parse_decimal(raw: str | None, default: str = "0") -> Decimal:
    s = (raw or "").strip() or default
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return Decimal(default)


def _parse_int(raw: str | None, default: int = 0) -> int:
    try:
        return int((raw or "").strip() or str(default))
    except ValueError:
        return default


def _parse_dt_local(raw: str | None) -> datetime | None:
    if not raw:
        return None
    s = raw.strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


# =====================================================================
#                          الموظفون
# =====================================================================
employees_router = APIRouter(prefix="/admin/employees", tags=["hr-employees"])


@employees_router.get("", response_class=HTMLResponse)
def employees_list(
    request: Request,
    db: DBSession,
    _: User = Depends(_view_perm),
    error: str | None = Query(None),
    saved: int = Query(0),
):
    items = hr.list_employees(db, only_active=False)
    total_active_salaries = hr.total_active_monthly_salaries(db)
    return templates.TemplateResponse(
        "admin_employees.html",
        {
            "request": request,
            "items": items,
            "total_active_salaries": total_active_salaries,
            "active_count": sum(1 for e in items if e.is_active),
            "error": error,
            "saved": bool(saved),
        },
    )


def _employee_form_ctx(request: Request, db, error: str | None, emp=None):
    pay_types = [(p.value, _PAY_TYPE_LABELS[p]) for p in PayType]
    statuses = [(s.value, _STATUS_LABELS[s]) for s in EmployeeStatus]
    # المستخدمون المتاحون للربط
    from sqlalchemy import select as _sel
    from modules.authz.models import User as _U

    users = list(db.scalars(_sel(_U).order_by(_U.username)).all())
    return {
        "request": request,
        "emp": emp,
        "pay_types": pay_types,
        "statuses": statuses,
        "users": users,
        "today": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "error": error,
    }


_PAY_TYPE_LABELS = {
    PayType.MONTHLY: "راتب شهري ثابت",
    PayType.HOURLY: "أجر بالساعة",
    PayType.DAILY: "أجر باليوم",
    PayType.MIXED: "شهري + إضافي بالساعة",
}
_STATUS_LABELS = {
    EmployeeStatus.ACTIVE: "نشط",
    EmployeeStatus.SUSPENDED: "موقوف",
    EmployeeStatus.TERMINATED: "منتهية الخدمة",
}


@employees_router.get("/new", response_class=HTMLResponse)
def employee_new(
    request: Request,
    db: DBSession,
    _: User = Depends(_manage_perm),
    error: str | None = Query(None),
):
    return templates.TemplateResponse(
        "admin_employee_form.html",
        _employee_form_ctx(request, db, error, None),
    )


@employees_router.get("/export.csv")
def employees_export_csv(
    db: DBSession,
    _: User = Depends(_view_perm),
):
    """تصدير قائمة الموظفين إلى CSV (UTF-8 BOM)."""
    from modules.reporting.exports import csv_response

    items = hr.list_employees(db, only_active=False)
    headers = [
        "الاسم",
        "الوظيفة",
        "نوع الأجر",
        "الراتب الشهري",
        "الأجر/ساعة",
        "ساعات/يوم",
        "مضاعف الإضافي",
        "الحالة",
        "الهاتف",
        "تاريخ التعيين",
    ]
    rows = [
        [
            e.full_name_ar,
            e.job_title or "",
            _PAY_TYPE_LABELS[e.pay_type],
            e.base_monthly_salary,
            e.hourly_rate,
            e.standard_hours_per_day,
            e.overtime_multiplier,
            _STATUS_LABELS[e.status],
            e.phone or "",
            e.hired_at.strftime("%Y-%m-%d") if e.hired_at else "",
        ]
        for e in items
    ]
    return csv_response("employees", headers, rows)


@employees_router.get("/{emp_id}", response_class=HTMLResponse)
def employee_edit(
    request: Request,
    emp_id: int,
    db: DBSession,
    _: User = Depends(_manage_perm),
    error: str | None = Query(None),
):
    emp = hr.get_employee(db, emp_id)
    if emp is None:
        return RedirectResponse("/admin/employees", status_code=302)
    return templates.TemplateResponse(
        "admin_employee_form.html",
        _employee_form_ctx(request, db, error, emp),
    )


@employees_router.post("/save", response_class=HTMLResponse)
async def employee_save(
    request: Request,
    db: DBSession,
    _: User = Depends(_manage_perm),
):
    form = await request.form()
    emp_id_raw = (form.get("id") or "").strip()
    emp_id = int(emp_id_raw) if emp_id_raw.isdigit() else None
    hired_at_raw = (form.get("hired_at") or "").strip()
    hired_at: datetime | None = None
    if hired_at_raw:
        try:
            hired_at = datetime.fromisoformat(hired_at_raw).replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            hired_at = None

    user_id_raw = (form.get("user_id") or "").strip()
    user_id = int(user_id_raw) if user_id_raw.isdigit() else None

    try:
        emp = hr.upsert_employee(
            db,
            emp_id=emp_id,
            full_name_ar=form.get("full_name_ar") or "",
            job_title=form.get("job_title"),
            national_id=form.get("national_id"),
            phone=form.get("phone"),
            pay_type=form.get("pay_type") or "MONTHLY",
            base_monthly_salary=_parse_decimal(form.get("base_monthly_salary")),
            hourly_rate=_parse_decimal(form.get("hourly_rate")),
            standard_hours_per_day=_parse_decimal(
                form.get("standard_hours_per_day"), default="8"
            ),
            overtime_multiplier=_parse_decimal(
                form.get("overtime_multiplier"), default="1.5"
            ),
            status=form.get("status") or "ACTIVE",
            hired_at=hired_at,
            user_id=user_id,
            notes=form.get("notes"),
        )
        db.commit()
    except hr.HRError as e:
        db.rollback()
        if emp_id:
            return RedirectResponse(
                f"/admin/employees/{emp_id}?error={e}", status_code=302
            )
        return RedirectResponse(f"/admin/employees/new?error={e}", status_code=302)
    return RedirectResponse(f"/admin/employees?saved=1#emp-{emp.id}", status_code=302)


@employees_router.post("/{emp_id}/delete", response_class=HTMLResponse)
def employee_delete(
    emp_id: int,
    db: DBSession,
    _: User = Depends(_manage_perm),
):
    try:
        hr.delete_employee(db, emp_id)
        db.commit()
    except hr.HRError as e:
        db.rollback()
        return RedirectResponse(
            f"/admin/employees?error={e}", status_code=302
        )
    return RedirectResponse("/admin/employees", status_code=302)


# =====================================================================
#                       الحضور والانصراف
# =====================================================================
attendance_router = APIRouter(prefix="/admin/attendance", tags=["hr-attendance"])


@attendance_router.get("", response_class=HTMLResponse)
def attendance_page(
    request: Request,
    db: DBSession,
    user: User = Depends(_view_perm),
    employee_id: int | None = Query(None),
    error: str | None = Query(None),
    saved: int = Query(0),
):
    employees = hr.list_employees(db, only_active=False)
    # الجلسات المفتوحة الآن
    open_sessions = []
    for e in employees:
        if e.is_active:
            o = hr.open_attendance_for(db, e.id)
            if o is not None:
                open_sessions.append((e, o))
    # سجلات اليوم لكل الموظفين
    now = datetime.now(timezone.utc)
    today_s = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_recs = hr.list_attendance(
        db, employee_id=employee_id, start=today_s, limit=200
    )
    # سجلات تاريخية للموظف المحدد
    history = []
    selected_emp = None
    if employee_id:
        selected_emp = hr.get_employee(db, employee_id)
        from datetime import timedelta
        history = hr.list_attendance(
            db, employee_id=employee_id, start=now - timedelta(days=30), limit=200
        )
    can_record = any(
        hasattr(p, "code") and p.code == HR_ATTENDANCE
        for r in (user.roles or [])
        for p in r.permissions
    ) or any(
        hasattr(p, "code") and p.code == HR_MANAGE
        for r in (user.roles or [])
        for p in r.permissions
    )
    return templates.TemplateResponse(
        "admin_attendance.html",
        {
            "request": request,
            "employees": employees,
            "open_sessions": open_sessions,
            "today_recs": today_recs,
            "history": history,
            "selected_emp": selected_emp,
            "selected_id": employee_id,
            "can_record": can_record,
            "error": error,
            "saved": bool(saved),
        },
    )


@attendance_router.post("/check-in", response_class=HTMLResponse)
def attendance_check_in(
    db: DBSession,
    user: User = Depends(_attend_perm),
    employee_id: str = Form(...),
    when: str = Form(""),
    notes: str = Form(""),
):
    eid = _parse_int(employee_id)
    if eid <= 0:
        return RedirectResponse(
            "/admin/attendance?error=" + "اختر موظفاً.", status_code=302
        )
    try:
        hr.check_in(
            db,
            employee_id=eid,
            when=_parse_dt_local(when),
            notes=notes,
            recorded_by_id=user.id,
        )
        db.commit()
    except hr.HRError as e:
        db.rollback()
        return RedirectResponse(f"/admin/attendance?error={e}", status_code=302)
    return RedirectResponse("/admin/attendance?saved=1", status_code=302)


@attendance_router.post("/check-out", response_class=HTMLResponse)
def attendance_check_out(
    db: DBSession,
    _: User = Depends(_attend_perm),
    employee_id: str = Form(...),
    when: str = Form(""),
    notes: str = Form(""),
):
    eid = _parse_int(employee_id)
    if eid <= 0:
        return RedirectResponse(
            "/admin/attendance?error=" + "اختر موظفاً.", status_code=302
        )
    try:
        hr.check_out(
            db, employee_id=eid, when=_parse_dt_local(when), notes=notes
        )
        db.commit()
    except hr.HRError as e:
        db.rollback()
        return RedirectResponse(f"/admin/attendance?error={e}", status_code=302)
    return RedirectResponse("/admin/attendance?saved=1", status_code=302)


@attendance_router.post("/manual-add", response_class=HTMLResponse)
def attendance_manual(
    db: DBSession,
    user: User = Depends(_manage_perm),
    employee_id: str = Form(...),
    check_in: str = Form(...),
    check_out: str = Form(""),
    notes: str = Form(""),
):
    """للمدير: تسجيل جلسة كاملة (دخول + خروج) دفعة واحدة."""
    from modules.hr.models import AttendanceRecord

    eid = _parse_int(employee_id)
    ci = _parse_dt_local(check_in)
    co = _parse_dt_local(check_out)
    if eid <= 0 or ci is None:
        return RedirectResponse(
            "/admin/attendance?error=" + "بيانات غير صالحة.", status_code=302
        )
    if co is not None and co <= ci:
        return RedirectResponse(
            "/admin/attendance?error=" + "وقت الخروج يجب أن يكون بعد الدخول.",
            status_code=302,
        )
    rec = AttendanceRecord(
        employee_id=eid,
        check_in=ci,
        check_out=co,
        source=AttendanceSource.MANAGER,
        notes=(notes or "").strip() or None,
        recorded_by_id=user.id,
    )
    db.add(rec)
    db.commit()
    return RedirectResponse(
        f"/admin/attendance?saved=1&employee_id={eid}", status_code=302
    )


@attendance_router.post("/{rec_id}/delete", response_class=HTMLResponse)
def attendance_delete(
    rec_id: int,
    db: DBSession,
    _: User = Depends(_manage_perm),
):
    from modules.hr.models import AttendanceRecord

    rec = db.get(AttendanceRecord, rec_id)
    if rec is not None:
        eid = rec.employee_id
        db.delete(rec)
        db.commit()
        return RedirectResponse(
            f"/admin/attendance?employee_id={eid}", status_code=302
        )
    return RedirectResponse("/admin/attendance", status_code=302)


# =====================================================================
#                          الرواتب
# =====================================================================
payroll_router = APIRouter(prefix="/admin/payroll", tags=["hr-payroll"])


@payroll_router.get("", response_class=HTMLResponse)
def payroll_list(
    request: Request,
    db: DBSession,
    _: User = Depends(_view_perm),
    error: str | None = Query(None),
    saved: int = Query(0),
):
    runs = hr.list_payroll_runs(db, limit=24)
    now = datetime.now(timezone.utc)
    return templates.TemplateResponse(
        "admin_payroll_list.html",
        {
            "request": request,
            "runs": runs,
            "now_year": now.year,
            "now_month": now.month,
            "status_labels": {
                PayrollStatus.DRAFT.value: "مسودة",
                PayrollStatus.POSTED.value: "معتمدة",
                PayrollStatus.PAID.value: "مدفوعة",
            },
            "error": error,
            "saved": bool(saved),
        },
    )


@payroll_router.post("/new", response_class=HTMLResponse)
def payroll_new(
    db: DBSession,
    user: User = Depends(_manage_perm),
    period_year: str = Form(...),
    period_month: str = Form(...),
):
    try:
        year = int(period_year.strip())
        month = int(period_month.strip())
    except ValueError:
        return RedirectResponse(
            "/admin/payroll?error=" + "فترة غير صالحة.", status_code=302
        )
    try:
        run = hr.create_payroll_run(db, year=year, month=month, user_id=user.id)
        db.commit()
    except hr.HRError as e:
        db.rollback()
        return RedirectResponse(f"/admin/payroll?error={e}", status_code=302)
    return RedirectResponse(f"/admin/payroll/{run.id}?saved=1", status_code=302)


@payroll_router.get("/{run_id}", response_class=HTMLResponse)
def payroll_detail(
    request: Request,
    run_id: int,
    db: DBSession,
    _: User = Depends(_view_perm),
    error: str | None = Query(None),
    saved: int = Query(0),
):
    run = hr.get_payroll_run(db, run_id)
    if run is None:
        return RedirectResponse("/admin/payroll", status_code=302)
    methods = list_payment_methods(db, only_active=True)
    # رصيد السلف القائمة لكل موظف (يُعرض بجانب البند)
    advances_by_emp = {
        e.employee_id: hr.outstanding_advances_for(db, e.employee_id)
        for e in run.entries
    }
    return templates.TemplateResponse(
        "admin_payroll_detail.html",
        {
            "request": request,
            "run": run,
            "methods": methods,
            "advances_by_emp": advances_by_emp,
            "status_labels": {
                PayrollStatus.DRAFT.value: "مسودة",
                PayrollStatus.POSTED.value: "معتمدة",
                PayrollStatus.PAID.value: "مدفوعة",
            },
            "is_draft": run.status == PayrollStatus.DRAFT,
            "is_posted": run.status == PayrollStatus.POSTED,
            "is_paid": run.status == PayrollStatus.PAID,
            "error": error,
            "saved": bool(saved),
        },
    )


@payroll_router.post("/{run_id}/entry/{entry_id}/save", response_class=HTMLResponse)
def payroll_entry_save(
    run_id: int,
    entry_id: int,
    db: DBSession,
    _: User = Depends(_manage_perm),
    base_salary: str = Form("0"),
    overtime_pay: str = Form("0"),
    bonuses: str = Form("0"),
    deductions: str = Form("0"),
    advances: str = Form("0"),
    notes: str = Form(""),
):
    try:
        hr.update_entry(
            db,
            entry_id=entry_id,
            base_salary=_parse_decimal(base_salary),
            overtime_pay=_parse_decimal(overtime_pay),
            bonuses=_parse_decimal(bonuses),
            deductions=_parse_decimal(deductions),
            advances=_parse_decimal(advances),
            notes=notes,
        )
        db.commit()
    except hr.HRError as e:
        db.rollback()
        return RedirectResponse(
            f"/admin/payroll/{run_id}?error={e}", status_code=302
        )
    return RedirectResponse(
        f"/admin/payroll/{run_id}?saved=1#entry-{entry_id}", status_code=302
    )


@payroll_router.post("/{run_id}/post", response_class=HTMLResponse)
def payroll_post(
    run_id: int,
    db: DBSession,
    _: User = Depends(_manage_perm),
):
    try:
        hr.post_run(db, run_id)
        db.commit()
    except hr.HRError as e:
        db.rollback()
        return RedirectResponse(
            f"/admin/payroll/{run_id}?error={e}", status_code=302
        )
    return RedirectResponse(f"/admin/payroll/{run_id}?saved=1", status_code=302)


@payroll_router.post("/{run_id}/pay", response_class=HTMLResponse)
def payroll_pay(
    run_id: int,
    db: DBSession,
    user: User = Depends(_manage_perm),
    payment_method_id: str = Form(...),
):
    pm_id = _parse_int(payment_method_id)
    if pm_id <= 0:
        return RedirectResponse(
            f"/admin/payroll/{run_id}?error=أسلوب الدفع غير صالح.",
            status_code=302,
        )
    try:
        hr.pay_run(
            db, run_id=run_id, payment_method_id=pm_id, user_id=user.id
        )
        db.commit()
    except hr.HRError as e:
        db.rollback()
        return RedirectResponse(
            f"/admin/payroll/{run_id}?error={e}", status_code=302
        )
    except Exception as e:  # noqa: BLE001
        db.rollback()
        return RedirectResponse(
            f"/admin/payroll/{run_id}?error=تعذّر الدفع: {e}", status_code=302
        )
    return RedirectResponse(f"/admin/payroll/{run_id}?saved=1", status_code=302)


@payroll_router.post("/{run_id}/delete", response_class=HTMLResponse)
def payroll_delete(
    run_id: int,
    db: DBSession,
    _: User = Depends(_manage_perm),
):
    try:
        hr.delete_run(db, run_id)
        db.commit()
    except hr.HRError as e:
        db.rollback()
        return RedirectResponse(
            f"/admin/payroll?error={e}", status_code=302
        )
    return RedirectResponse("/admin/payroll", status_code=302)


# =====================================================================
#                            السلف
# =====================================================================
advances_router = APIRouter(prefix="/admin/advances", tags=["hr-advances"])


@advances_router.get("", response_class=HTMLResponse)
def advances_list(
    request: Request,
    db: DBSession,
    _: User = Depends(_view_perm),
    employee_id: int | None = Query(None),
    only_outstanding: int = Query(0, ge=0, le=1),
    error: str | None = Query(None),
    saved: int = Query(0),
):
    items = hr.list_advances(
        db, employee_id=employee_id, only_outstanding=bool(only_outstanding)
    )
    employees = hr.list_employees(db, only_active=False)
    summary = hr.outstanding_advances_summary(db)
    grand_total = hr.grand_total_outstanding_advances(db)
    methods = list_payment_methods(db, only_active=True)
    selected_emp = hr.get_employee(db, employee_id) if employee_id else None
    status_labels = {
        "OUTSTANDING": "قائمة",
        "PARTIALLY_REPAID": "مسترد جزئياً",
        "FULLY_REPAID": "مسددة",
        "CANCELLED": "ملغاة",
    }
    return templates.TemplateResponse(
        "admin_advances.html",
        {
            "request": request,
            "items": items,
            "employees": employees,
            "summary": summary,
            "grand_total": grand_total,
            "methods": methods,
            "selected_emp": selected_emp,
            "selected_id": employee_id,
            "only_outstanding": bool(only_outstanding),
            "status_labels": status_labels,
            "error": error,
            "saved": bool(saved),
            "today_dt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M"),
        },
    )


@advances_router.post("/grant", response_class=HTMLResponse)
def advances_grant(
    db: DBSession,
    user: User = Depends(_manage_perm),
    employee_id: str = Form(...),
    amount: str = Form(...),
    payment_method_id: str = Form(""),
    record_as_expense: str = Form("1"),
    when: str = Form(""),
    notes: str = Form(""),
):
    eid = _parse_int(employee_id)
    amt = _parse_decimal(amount)
    pm_id = _parse_int(payment_method_id) if payment_method_id else None
    record = record_as_expense in ("1", "on", "true")
    if eid <= 0 or amt <= 0:
        return RedirectResponse(
            "/admin/advances?error=" + "بيانات غير صالحة.", status_code=302
        )
    try:
        hr.grant_advance(
            db,
            employee_id=eid,
            amount=amt,
            payment_method_id=pm_id if (pm_id and pm_id > 0) else None,
            record_as_expense=record,
            when=_parse_dt_local(when),
            notes=notes,
            given_by_id=user.id,
        )
        db.commit()
    except hr.HRError as e:
        db.rollback()
        return RedirectResponse(
            f"/admin/advances?error={e}", status_code=302
        )
    return RedirectResponse(
        f"/admin/advances?saved=1&employee_id={eid}", status_code=302
    )


@advances_router.post("/{adv_id}/repay", response_class=HTMLResponse)
def advances_repay(
    adv_id: int,
    db: DBSession,
    _: User = Depends(_manage_perm),
    amount: str = Form(...),
    notes: str = Form(""),
):
    amt = _parse_decimal(amount)
    if amt <= 0:
        return RedirectResponse(
            "/admin/advances?error=" + "أدخل مبلغاً صالحاً.", status_code=302
        )
    try:
        hr.repay_advance_manual(
            db, advance_id=adv_id, amount=amt, notes=notes
        )
        db.commit()
    except hr.HRError as e:
        db.rollback()
        return RedirectResponse(
            f"/admin/advances?error={e}", status_code=302
        )
    return RedirectResponse("/admin/advances?saved=1", status_code=302)


@advances_router.post("/{adv_id}/cancel", response_class=HTMLResponse)
def advances_cancel(
    adv_id: int,
    db: DBSession,
    _: User = Depends(_manage_perm),
):
    try:
        hr.cancel_advance(db, adv_id)
        db.commit()
    except hr.HRError as e:
        db.rollback()
        return RedirectResponse(
            f"/admin/advances?error={e}", status_code=302
        )
    return RedirectResponse("/admin/advances?saved=1", status_code=302)


@advances_router.post("/{adv_id}/delete", response_class=HTMLResponse)
def advances_delete(
    adv_id: int,
    db: DBSession,
    _: User = Depends(_manage_perm),
):
    try:
        hr.delete_advance(db, adv_id)
        db.commit()
    except hr.HRError as e:
        db.rollback()
        return RedirectResponse(
            f"/admin/advances?error={e}", status_code=302
        )
    return RedirectResponse("/admin/advances?saved=1", status_code=302)


@advances_router.get("/export.csv")
def advances_export_csv(
    db: DBSession,
    _: User = Depends(_view_perm),
    only_outstanding: int = Query(0, ge=0, le=1),
):
    """تصدير السلف إلى CSV (UTF-8 BOM)."""
    from modules.reporting.exports import csv_response

    items = hr.list_advances(db, only_outstanding=bool(only_outstanding))
    headers = [
        "تاريخ الصرف",
        "الموظف",
        "الوظيفة",
        "المبلغ الأصلي",
        "المسترد",
        "المتبقي",
        "الحالة",
        "ملاحظة",
    ]
    status_labels = {
        "OUTSTANDING": "قائمة",
        "PARTIALLY_REPAID": "مسترد جزئياً",
        "FULLY_REPAID": "مسددة",
        "CANCELLED": "ملغاة",
    }
    rows = [
        [
            a.given_at.strftime("%Y-%m-%d"),
            a.employee.full_name_ar if a.employee else "",
            (a.employee.job_title if a.employee else "") or "",
            a.amount,
            a.repaid_amount,
            a.remaining_amount,
            status_labels.get(a.status.value, a.status.value),
            a.notes or "",
        ]
        for a in items
    ]
    suffix = "outstanding" if only_outstanding else "all"
    return csv_response(f"advances-{suffix}", headers, rows)


@payroll_router.get("/{run_id}/export.csv")
def payroll_export_csv(
    run_id: int,
    db: DBSession,
    _: User = Depends(_view_perm),
):
    """تصدير دفعة رواتب كاملة إلى CSV."""
    from modules.reporting.exports import csv_response

    run = hr.get_payroll_run(db, run_id)
    if run is None:
        return RedirectResponse("/admin/payroll", status_code=302)
    headers = [
        "الموظف",
        "الوظيفة",
        "ساعات",
        "ساعات إضافية",
        "أساسي",
        "إضافي",
        "مكافآت",
        "استقطاعات",
        "سُلف",
        "صافي",
        "ملاحظة",
    ]
    rows = [
        [
            e.employee.full_name_ar,
            e.employee.job_title or "",
            e.hours_worked,
            e.overtime_hours,
            e.base_salary,
            e.overtime_pay,
            e.bonuses,
            e.deductions,
            e.advances,
            e.net_pay,
            e.notes or "",
        ]
        for e in run.entries
    ]
    rows.append([])
    rows.append(
        ["الإجمالي", "", "", "", "", "", "", "", "", run.total_net, ""]
    )
    return csv_response(f"payroll-{run.label.replace('/', '-')}", headers, rows)


# =====================================================================
#                      تصدير الحضور إلى CSV
# =====================================================================
@attendance_router.get("/export.csv")
def attendance_export_csv(
    db: DBSession,
    _: User = Depends(_view_perm),
    employee_id: int | None = Query(None),
    days: int = Query(30, ge=1, le=365),
):
    """تصدير سجلات الحضور (لكل الموظفين أو لموظف محدد) خلال آخر N يوم."""
    from datetime import timedelta as _td
    from modules.reporting.exports import csv_response

    end = datetime.now(timezone.utc)
    start = end - _td(days=days)
    recs = hr.list_attendance(
        db, employee_id=employee_id, start=start, end=end, limit=10_000
    )
    # خريطة الموظفين
    emps = {e.id: e for e in hr.list_employees(db, only_active=False)}
    headers = [
        "التاريخ",
        "الموظف",
        "الوظيفة",
        "دخول",
        "خروج",
        "ساعات",
        "ملاحظة",
    ]
    rows = []
    for r in recs:
        emp = emps.get(r.employee_id)
        rows.append(
            [
                r.check_in.strftime("%Y-%m-%d"),
                emp.full_name_ar if emp else f"#{r.employee_id}",
                (emp.job_title if emp else "") or "",
                r.check_in.strftime("%H:%M"),
                r.check_out.strftime("%H:%M") if r.check_out else "",
                r.hours_worked if r.check_out else 0,
                r.notes or "",
            ]
        )
    suffix = f"emp-{employee_id}" if employee_id else "all"
    return csv_response(f"attendance-{suffix}-{days}d", headers, rows)
