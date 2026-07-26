"""واجهات وحدة الموارد البشرية:
- /admin/employees       — قائمة وإدارة الموظفين
- /admin/attendance      — تسجيل الحضور والانصراف
- /admin/payroll         — قائمة دفعات الرواتب
- /admin/payroll/{id}    — تفاصيل دفعة (تعديل/اعتماد/دفع)
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from sqlalchemy import func, select

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.deps import DBSession, require_permission
from app.datetime_local import format_local_dt, now_local
from app.jinja_env import templates
from modules.authz.models import User
from modules.authz.permissions import HR_ATTENDANCE, HR_MANAGE, HR_VIEW
from modules.hr import service as hr
from modules.hr.models import (
    AttendanceSource,
    Employee,
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
    user: User = Depends(_view_perm),
    error: str | None = Query(None),
    saved: int = Query(0),
    department_id: int | None = Query(None),
):
    from modules.platform.business_domain import domain_label, resolve_finance_domain

    hr.ensure_default_departments(db)
    db.commit()
    dept_filter = department_id
    finance_domain = resolve_finance_domain(user, request.session)
    items = hr.list_employees(
        db, only_active=False, department_id=dept_filter, domain=finance_domain
    )
    departments = hr.list_departments(db, only_active=False)
    dept_counts = hr.department_employee_counts(db)
    total_employee_count = int(
        db.scalar(select(func.count()).select_from(Employee)) or 0
    )
    unassigned_count = int(
        db.scalar(
            select(func.count())
            .select_from(Employee)
            .where(Employee.department_id.is_(None))
        )
        or 0
    )
    total_active_salaries = hr.total_active_monthly_salaries(db, domain=finance_domain)
    return templates.TemplateResponse(
        "admin_employees.html",
        {
            "request": request,
            "items": items,
            "departments": departments,
            "dept_counts": dept_counts,
            "department_id": dept_filter,
            "total_employee_count": total_employee_count,
            "unassigned_count": unassigned_count,
            "total_active_salaries": total_active_salaries,
            "active_count": sum(1 for e in items if e.is_active),
            "error": error,
            "saved": bool(saved),
            "finance_domain_filter": finance_domain,
            "domain_label": domain_label(finance_domain) if finance_domain else "الكل",
        },
    )


def _employee_form_ctx(request: Request, db, error: str | None, emp=None, user=None):
    hr.ensure_default_departments(db)
    db.flush()
    from modules.platform.business_domain import (
        domain_label,
        employee_domain_choices,
        is_system_admin,
        resolve_finance_domain,
    )

    pay_types = [(p.value, _PAY_TYPE_LABELS[p]) for p in PayType]
    statuses = [(s.value, _STATUS_LABELS[s]) for s in EmployeeStatus]
    # المستخدمون المتاحون للربط
    from sqlalchemy import select as _sel
    from modules.authz.models import User as _U

    users = list(db.scalars(_sel(_U).order_by(_U.username)).all())
    linked_username = None
    if emp is not None and emp.user_id:
        u = db.get(_U, emp.user_id)
        linked_username = u.username if u else None
    fin_dom = (
        resolve_finance_domain(user, request.session) if user is not None else None
    )
    # الأدمن يختار المجال يدوياً حتى في وضع عرض مطعم/فندق
    can_pick_domain = bool(user and is_system_admin(user)) or fin_dom is None
    return {
        "request": request,
        "emp": emp,
        "pay_types": pay_types,
        "statuses": statuses,
        "users": users,
        "linked_username": linked_username,
        "work_shifts": hr.list_work_shifts(db, only_active=False),
        "departments": hr.list_departments(db, only_active=False),
        "week_days": __import__(
            "modules.hr.schedule", fromlist=["WEEKDAY_LABELS"]
        ).WEEKDAY_LABELS,
        "emp_day_schedules": (
            __import__(
                "modules.hr.schedule", fromlist=["list_employee_day_schedules"]
            ).list_employee_day_schedules(db, emp.id)
            if emp
            else {}
        ),
        "today": now_local().strftime("%Y-%m-%d"),
        "error": error,
        "domain_choices": employee_domain_choices(),
        "finance_domain_filter": None if can_pick_domain else fin_dom,
        "can_pick_employee_domain": can_pick_domain,
        "domain_label": domain_label(fin_dom)
        if user
        else "الكل",
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
    user: User = Depends(_manage_perm),
    error: str | None = Query(None),
):
    return templates.TemplateResponse(
        "admin_employee_form.html",
        _employee_form_ctx(request, db, error, None, user=user),
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
        "القسم",
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
            e.department.name_ar if e.department else "",
            e.job_title or "",
            _PAY_TYPE_LABELS[e.pay_type],
            e.base_monthly_salary,
            e.hourly_rate,
            e.standard_hours_per_day,
            e.overtime_multiplier,
            _STATUS_LABELS[e.status],
            e.phone or "",
            format_local_dt(e.hired_at, "%Y-%m-%d") if e.hired_at else "",
        ]
        for e in items
    ]
    return csv_response("employees", headers, rows)


@employees_router.get("/{emp_id}", response_class=HTMLResponse)
def employee_edit(
    request: Request,
    emp_id: int,
    db: DBSession,
    user: User = Depends(_manage_perm),
    error: str | None = Query(None),
):
    emp = hr.get_employee(db, emp_id)
    if emp is None:
        return RedirectResponse("/admin/employees", status_code=302)
    return templates.TemplateResponse(
        "admin_employee_form.html",
        _employee_form_ctx(request, db, error, emp, user=user),
    )


@employees_router.post("/save", response_class=HTMLResponse)
async def employee_save(
    request: Request,
    db: DBSession,
    user: User = Depends(_manage_perm),
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
    work_shift_raw = (form.get("work_shift_id") or "").strip()
    work_shift_id = int(work_shift_raw) if work_shift_raw.isdigit() else None
    dept_raw = (form.get("department_id") or "").strip()
    department_id = int(dept_raw) if dept_raw.isdigit() else None
    create_pos_user = form.get("create_pos_user") == "on"
    pos_username = (form.get("pos_username") or "").strip()
    pos_password = (form.get("pos_password") or "").strip()
    from modules.platform.business_domain import (
        is_system_admin,
        resolve_finance_domain,
    )

    finance_domain = resolve_finance_domain(user, request.session)
    dom_raw = (form.get("business_domain") or "").strip()
    # موظف محدود المجال فقط يُفرض عليه المجال — الأدمن يختار من النموذج
    if finance_domain is not None and not is_system_admin(user):
        dom_raw = finance_domain.value

    try:
        is_pos_cashier = form.get("is_pos_cashier") == "on"
        is_hotel_front = form.get("is_hotel_front") == "on"
        is_pos_supervisor = form.get("is_pos_supervisor") == "on"
        if create_pos_user:
            if not is_pos_cashier:
                raise hr.HRError("لإنشاء حساب دخول يجب تفعيل «كاشير نقطة بيع».")
            if user_id is not None:
                raise hr.HRError("لا يمكن إنشاء حساب جديد مع اختيار حساب مرتبط من القائمة.")
            new_user = hr.create_pos_cashier_user(
                db, username=pos_username, password=pos_password
            )
            user_id = new_user.id
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
            is_pos_cashier=is_pos_cashier,
            is_hotel_front=is_hotel_front,
            is_pos_supervisor=is_pos_supervisor,
            work_shift_id=work_shift_id,
            department_id=department_id,
            zk_emp_code=(form.get("zk_emp_code") or "").strip() or None,
            business_domain=dom_raw or None,
        )
        from modules.hr.schedule import parse_day_schedules_from_form, save_employee_day_schedules

        save_employee_day_schedules(
            db,
            emp.id,
            parse_day_schedules_from_form(form, "emp_day"),
        )
        try:
            pos_pin = (form.get("pos_pin") or "").strip()
            if form.get("clear_pos_pin") == "on":
                hr.set_employee_pos_pin(db, emp, pin_plain=None, clear_pin=True)
            elif pos_pin:
                hr.set_employee_pos_pin(db, emp, pin_plain=pos_pin)
            if is_pos_cashier and not emp.pos_pin_hash:
                raise hr.HRError("يجب تعيين رقم سري من 4 أرقام لكاشير نقطة البيع.")
            if is_hotel_front and not emp.pos_pin_hash:
                raise hr.HRError("يجب تعيين رقم سري من 4 أرقام لموظف استقبال الفندق.")
        except hr.HRError as pin_err:
            db.rollback()
            if emp_id:
                return RedirectResponse(
                    f"/admin/employees/{emp_id}?error={pin_err}", status_code=302
                )
            return RedirectResponse(f"/admin/employees/new?error={pin_err}", status_code=302)
        db.commit()
    except hr.HRError as e:
        db.rollback()
        if emp_id:
            return RedirectResponse(
                f"/admin/employees/{emp_id}?error={e}", status_code=302
            )
        return RedirectResponse(f"/admin/employees/new?error={e}", status_code=302)
    return RedirectResponse(f"/admin/employees?saved=1#emp-{emp.id}", status_code=302)


@employees_router.get("/{emp_id}/delete", response_class=HTMLResponse)
def employee_delete_get(emp_id: int):
    """GET على مسار الحذف (مثلاً بعد إصلاح المخطط) — أعد للقائمة بدل Method Not Allowed."""
    from urllib.parse import quote

    return RedirectResponse(
        "/admin/employees?error="
        + quote("أعد محاولة الحذف من زر «حذف» في قائمة الموظفين."),
        status_code=302,
    )


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
    return RedirectResponse("/admin/employees?deleted=1", status_code=302)


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
    from infra.schema_bootstrap import ensure_schema_patched

    ensure_schema_patched(force=True)
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
    from modules.hr.zkbio_sync import get_sync_status

    try:
        zk_status = get_sync_status(db)
    except Exception:
        zk_status = {
            "enabled": False,
            "detected": False,
            "configured": False,
            "show_panel": True,
            "local_install": False,
            "connected": False,
            "connection_message": "تعذّر قراءة حالة البصمة",
            "interval_seconds": 30,
            "linked_employees": 0,
            "last_sync_at": "",
            "last_sync_error": "",
            "last_transaction_id": 0,
            "max_transaction_id": 0,
            "cursor_ok": True,
            "host": "",
            "port": 0,
            "dbname": "",
            "db_user": "",
        }
    try:
        ot_summary = hr.overtime_summary(
            db, employee_id=employee_id if employee_id else None
        )
        ot_summary_all = hr.overtime_summary(db) if employee_id else ot_summary
    except Exception:
        ot_summary = {
            "today_hours": Decimal("0"),
            "week_hours": Decimal("0"),
            "month_hours": Decimal("0"),
        }
        ot_summary_all = ot_summary
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
            "can_manage": any(
                hasattr(p, "code") and p.code == HR_MANAGE
                for r in (user.roles or [])
                for p in r.permissions
            ),
            "zk_status": zk_status,
            "ot_summary": ot_summary,
            "ot_summary_all": ot_summary_all,
            "error": error,
            "saved": bool(saved),
            "adjusted_hours": request.query_params.get("adjusted_hours"),
            "zk_synced": request.query_params.get("zk_synced"),
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


@attendance_router.post("/zk-sync", response_class=HTMLResponse)
def attendance_zk_sync(
    db: DBSession,
    _: User = Depends(_manage_perm),
):
    from modules.hr.zkbio_sync import sync_zkbio_punches

    try:
        result = sync_zkbio_punches(db)
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        return RedirectResponse(
            f"/admin/attendance?error={exc}",
            status_code=302,
        )
    if not result.ok:
        return RedirectResponse(
            f"/admin/attendance?error={result.message}",
            status_code=302,
        )
    msg = result.message
    if result.errors:
        msg += " | " + result.errors[0]
    return RedirectResponse(
        f"/admin/attendance?zk_synced={quote(msg)}",
        status_code=302,
    )


@attendance_router.post("/zk-sync-employees", response_class=HTMLResponse)
def attendance_zk_sync_employees(
    db: DBSession,
    _: User = Depends(_manage_perm),
):
    from modules.hr.zkbio_employee_sync import sync_employees_from_zkbio

    try:
        result = sync_employees_from_zkbio(db)
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        return RedirectResponse(
            f"/admin/attendance?error={exc}",
            status_code=302,
        )
    if not result.ok:
        return RedirectResponse(
            f"/admin/attendance?error={result.message}",
            status_code=302,
        )
    msg = result.message
    if result.errors:
        msg += " | " + result.errors[0]
    return RedirectResponse(
        f"/admin/attendance?zk_synced={quote(msg)}",
        status_code=302,
    )


@attendance_router.post("/zk-push-employees", response_class=HTMLResponse)
def attendance_zk_push_employees(
    db: DBSession,
    _: User = Depends(_manage_perm),
):
    from modules.hr.zkbio_employee_sync import sync_employees_to_zkbio

    try:
        result = sync_employees_to_zkbio(db)
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        return RedirectResponse(
            f"/admin/attendance?error={exc}",
            status_code=302,
        )
    if not result.ok:
        return RedirectResponse(
            f"/admin/attendance?error={result.message}",
            status_code=302,
        )
    msg = result.message
    if result.errors:
        msg += " | " + result.errors[0]
    return RedirectResponse(
        f"/admin/attendance?zk_synced={quote(msg)}",
        status_code=302,
    )


@attendance_router.post("/zk-settings", response_class=HTMLResponse)
def attendance_zk_settings(
    db: DBSession,
    _: User = Depends(_manage_perm),
    zk_sync_enabled: str = Form(""),
    zk_sync_interval_seconds: str = Form("30"),
    zk_form: str = Form("sync"),
    zk_biotime_host: str = Form(""),
    zk_biotime_port: str = Form(""),
    zk_biotime_db: str = Form(""),
    zk_biotime_user: str = Form(""),
    zk_biotime_password: str = Form(""),
):
    from modules.settings.service import set_setting

    form_kind = (zk_form or "sync").strip().lower()
    if form_kind == "connection":
        host = (zk_biotime_host or "").strip()
        if host:
            set_setting(db, "zk_biotime_host", host)
        if (zk_biotime_port or "").strip():
            try:
                set_setting(
                    db, "zk_biotime_port", str(max(1, int(zk_biotime_port.strip())))
                )
            except ValueError:
                pass
        dbname = (zk_biotime_db or "").strip()
        if dbname:
            set_setting(db, "zk_biotime_db", dbname)
        user = (zk_biotime_user or "").strip()
        if user:
            set_setting(db, "zk_biotime_user", user)
        pwd = (zk_biotime_password or "").strip()
        if pwd:
            set_setting(db, "zk_biotime_password", pwd)
    else:
        set_setting(db, "zk_sync_enabled", "1" if zk_sync_enabled == "on" else "0")
        try:
            interval = max(15, int((zk_sync_interval_seconds or "30").strip()))
        except ValueError:
            interval = 30
        set_setting(db, "zk_sync_interval_seconds", str(interval))

    db.commit()
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
    db.flush()
    if co is not None:
        hr.finalize_attendance_record(db, rec)
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


@attendance_router.post("/{rec_id}/overtime-approve", response_class=HTMLResponse)
def attendance_overtime_approve(
    rec_id: int,
    db: DBSession,
    user: User = Depends(_manage_perm),
    approved_minutes: str = Form(""),
):
    try:
        mins = int(approved_minutes.strip()) if approved_minutes.strip() else None
        rec = hr.approve_overtime(
            db, rec_id, approved_minutes=mins, approved_by_id=user.id
        )
        db.commit()
        return RedirectResponse(
            f"/admin/attendance?saved=1&employee_id={rec.employee_id}",
            status_code=302,
        )
    except hr.HRError as exc:
        db.rollback()
        return RedirectResponse(
            f"/admin/attendance?error={exc}",
            status_code=302,
        )


@attendance_router.post("/{rec_id}/overtime-reject", response_class=HTMLResponse)
def attendance_overtime_reject(
    rec_id: int,
    db: DBSession,
    user: User = Depends(_manage_perm),
):
    try:
        rec = hr.reject_overtime(db, rec_id, approved_by_id=user.id)
        db.commit()
        return RedirectResponse(
            f"/admin/attendance?saved=1&employee_id={rec.employee_id}",
            status_code=302,
        )
    except hr.HRError as exc:
        db.rollback()
        return RedirectResponse(
            f"/admin/attendance?error={exc}",
            status_code=302,
        )


@attendance_router.post("/{rec_id}/adjust", response_class=HTMLResponse)
def attendance_adjust(
    rec_id: int,
    db: DBSession,
    _: User = Depends(_manage_perm),
    check_in: str = Form(""),
    check_out: str = Form(""),
    work_hours: str = Form(""),
    use_manual_times: str = Form(""),
    override_metrics: str = Form(""),
    late_minutes: str = Form(""),
    overtime_minutes: str = Form(""),
    notes: str = Form(""),
):
    try:
        use_manual = (use_manual_times or "").strip() == "1"
        override = (override_metrics or "").strip() == "1"
        wh: Decimal | None = None
        if (work_hours or "").strip():
            wh = Decimal((work_hours or "").strip().replace(",", "."))
        ci = _parse_dt_local(check_in) if use_manual and check_in.strip() else None
        co = _parse_dt_local(check_out) if use_manual and check_out.strip() else None
        late = (
            int(late_minutes.strip())
            if override and late_minutes.strip()
            else None
        )
        ot = (
            int(overtime_minutes.strip())
            if override and overtime_minutes.strip()
            else None
        )
        rec = hr.adjust_attendance_record(
            db,
            rec_id,
            check_in=ci,
            check_out=co,
            work_hours=wh,
            use_manual_times=use_manual,
            override_metrics=override,
            late_minutes=late,
            overtime_minutes=ot,
            notes=notes if notes.strip() else None,
        )
        db.commit()
        db.refresh(rec)
        return RedirectResponse(
            f"/admin/attendance?saved=1&adjusted_hours={rec.hours_worked}"
            f"&employee_id={rec.employee_id}",
            status_code=302,
        )
    except (hr.HRError, ValueError) as exc:
        db.rollback()
        return RedirectResponse(
            f"/admin/attendance?error={exc}",
            status_code=302,
        )


# =====================================================================
#                          الرواتب
# =====================================================================
payroll_router = APIRouter(prefix="/admin/payroll", tags=["hr-payroll"])


@payroll_router.get("", response_class=HTMLResponse)
def payroll_list(
    request: Request,
    db: DBSession,
    user: User = Depends(_view_perm),
    error: str | None = Query(None),
    saved: int = Query(0),
):
    from modules.platform.business_domain import (
        domain_label,
        employee_domain_choices,
        resolve_finance_domain,
    )

    domain = resolve_finance_domain(user, request.session)
    runs = hr.list_payroll_runs(db, limit=24, domain=domain)
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
            "finance_domain_filter": domain,
            "domain_label": domain_label(domain) if domain else "الكل",
            "employee_domain_choices": employee_domain_choices(),
        },
    )


@payroll_router.post("/new", response_class=HTMLResponse)
def payroll_new(
    request: Request,
    db: DBSession,
    user: User = Depends(_manage_perm),
    period_year: str = Form(...),
    period_month: str = Form(...),
    business_domain: str = Form("restaurant"),
):
    from modules.platform.business_domain import resolve_finance_domain

    try:
        year = int(period_year.strip())
        month = int(period_month.strip())
    except ValueError:
        return RedirectResponse(
            "/admin/payroll?error=" + "فترة غير صالحة.", status_code=302
        )
    domain_filter = resolve_finance_domain(user, request.session)
    dom = domain_filter.value if domain_filter is not None else business_domain
    try:
        run = hr.create_payroll_run(
            db,
            year=year,
            month=month,
            user_id=user.id,
            business_domain=dom,
        )
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
    synced: int = Query(0),
):
    run = hr.get_payroll_run(db, run_id)
    if run is None:
        return RedirectResponse("/admin/payroll", status_code=302)
    auto_synced_entries = 0
    is_editable = run.status in (PayrollStatus.DRAFT, PayrollStatus.POSTED)
    if is_editable:
        auto_synced_entries = hr.reconcile_draft_run_withholdings(
            db, run, full=False
        )
        if auto_synced_entries:
            db.commit()
    methods = list_payment_methods(db, only_active=True)
    advances_by_emp = {
        e.employee_id: hr.outstanding_advances_for(db, e.employee_id)
        for e in run.entries
    }
    deductions_by_emp = {
        e.employee_id: hr.outstanding_deductions_for(db, e.employee_id)
        for e in run.entries
    }
    pending_deductions_by_emp = {
        e.employee_id: hr.pending_deduction_lines_for(db, e.employee_id)
        for e in run.entries
    }
    return templates.TemplateResponse(
        "admin_payroll_detail.html",
        {
            "request": request,
            "run": run,
            "methods": methods,
            "advances_by_emp": advances_by_emp,
            "deductions_by_emp": deductions_by_emp,
            "pending_deductions_by_emp": pending_deductions_by_emp,
            "auto_synced_entries": auto_synced_entries,
            "synced_full": bool(synced),
            "status_labels": {
                PayrollStatus.DRAFT.value: "مسودة",
                PayrollStatus.POSTED.value: "معتمدة",
                PayrollStatus.PAID.value: "مدفوعة",
            },
            "is_draft": run.status == PayrollStatus.DRAFT,
            "is_posted": run.status == PayrollStatus.POSTED,
            "is_paid": run.status == PayrollStatus.PAID,
            "is_editable": is_editable,
            "error": error,
            "saved": bool(saved),
        },
    )


@payroll_router.post("/{run_id}/sync-withholdings", response_class=HTMLResponse)
def payroll_sync_withholdings(
    run_id: int,
    db: DBSession,
    _: User = Depends(_manage_perm),
):
    run = hr.get_payroll_run(db, run_id)
    if run is None:
        return RedirectResponse("/admin/payroll", status_code=302)
    try:
        if run.status not in (PayrollStatus.DRAFT, PayrollStatus.POSTED):
            raise hr.HRError("المزامنة متاحة فقط قبل دفع الدفعة.")
        hr.reconcile_draft_run_withholdings(db, run, full=True)
        db.commit()
    except hr.HRError as e:
        db.rollback()
        return RedirectResponse(
            f"/admin/payroll/{run_id}?error={e}", status_code=302
        )
    return RedirectResponse(
        f"/admin/payroll/{run_id}?saved=1&synced=1", status_code=302
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


@payroll_router.post("/{run_id}/reopen", response_class=HTMLResponse)
def payroll_reopen(
    run_id: int,
    db: DBSession,
    _: User = Depends(_manage_perm),
):
    try:
        run = hr.reopen_paid_run_to_draft(db, run_id)
        hr.reconcile_draft_run_withholdings(db, run, full=True)
        db.commit()
    except hr.HRError as e:
        db.rollback()
        return RedirectResponse(
            f"/admin/payroll/{run_id}?error={e}", status_code=302
        )
    return RedirectResponse(
        f"/admin/payroll/{run_id}?saved=1&reopened=1", status_code=302
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
            "today_dt": now_local().strftime("%Y-%m-%dT%H:%M"),
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
            format_local_dt(a.given_at, "%Y-%m-%d"),
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
        "الحالة",
        "تأخير (د)",
        "إضافي محسوب (س)",
        "إضافي معتمد (س)",
        "حالة الإضافي",
        "ملاحظة",
    ]
    rows = []
    for r in recs:
        emp = emps.get(r.employee_id)
        rows.append(
            [
                format_local_dt(r.check_in, "%Y-%m-%d"),
                emp.full_name_ar if emp else f"#{r.employee_id}",
                (emp.job_title if emp else "") or "",
                format_local_dt(r.check_in, "%H:%M"),
                format_local_dt(r.check_out, "%H:%M") if r.check_out else "",
                r.hours_worked if r.check_out else 0,
                r.attendance_status_label if r.check_out else "",
                r.late_minutes if r.check_out else 0,
                round(r.overtime_minutes / 60, 2) if r.check_out and r.overtime_minutes else 0,
                round(r.payroll_overtime_minutes / 60, 2) if r.check_out else 0,
                r.overtime_status_label if r.check_out else "",
                r.notes or "",
            ]
        )
    suffix = f"emp-{employee_id}" if employee_id else "all"
    return csv_response(f"attendance-{suffix}-{days}d", headers, rows)


# =====================================================================
#                          ورديات العمل
# =====================================================================
work_shifts_router = APIRouter(prefix="/admin/work-shifts", tags=["hr-shifts"])


@work_shifts_router.get("", response_class=HTMLResponse)
def work_shifts_page(
    request: Request,
    db: DBSession,
    _: User = Depends(_view_perm),
    error: str | None = Query(None),
    saved: int = Query(0),
):
    from sqlalchemy import func, select as _sel
    from modules.hr.models import Employee as _Emp

    shifts = hr.list_work_shifts(db, only_active=False)
    counts_raw = db.execute(
        _sel(_Emp.work_shift_id, func.count())
        .where(_Emp.work_shift_id.isnot(None))
        .group_by(_Emp.work_shift_id)
    ).all()
    emp_counts = {int(row[0]): int(row[1]) for row in counts_raw if row[0] is not None}
    from modules.hr.schedule import WEEKDAY_LABELS, list_all_work_shift_day_schedules

    shift_day_schedules = list_all_work_shift_day_schedules(db)
    shift_days_json: dict[str, dict[str, dict[str, object]]] = {}
    for sid, days in shift_day_schedules.items():
        shift_days_json[str(sid)] = {
            str(dow): {
                "is_rest_day": bool(r.is_rest_day),
                "start_time": r.start_time,
                "end_time": r.end_time,
                "work_hours": float(r.work_hours) if r.work_hours is not None else None,
                "grace_minutes": r.grace_minutes,
            }
            for dow, r in days.items()
        }
    return templates.TemplateResponse(
        "admin_work_shifts.html",
        {
            "request": request,
            "shifts": shifts,
            "emp_counts": emp_counts,
            "shift_day_schedules": shift_days_json,
            "week_days": WEEKDAY_LABELS,
            "error": error,
            "saved": bool(saved),
        },
    )


@work_shifts_router.post("/save", response_class=HTMLResponse)
async def work_shift_save(
    request: Request,
    db: DBSession,
    _: User = Depends(_manage_perm),
):
    form = await request.form()
    sid = _parse_int(str(form.get("shift_id") or ""), default=0)
    try:
        shift = hr.upsert_work_shift(
            db,
            shift_id=sid if sid > 0 else None,
            name_ar=form.get("name_ar") or "",
            code=(form.get("code") or "") or None,
            start_time=(form.get("start_time") or "08:00")[:5],
            end_time=(form.get("end_time") or "16:00")[:5],
            work_hours=_parse_decimal(form.get("work_hours"), default="8"),
            grace_minutes=_parse_int(str(form.get("grace_minutes") or "15"), default=15),
            is_active=form.get("is_active") == "on",
            notes=form.get("notes"),
        )
        from modules.hr.schedule import parse_day_schedules_from_form, save_work_shift_day_schedules

        save_work_shift_day_schedules(
            db,
            shift.id,
            parse_day_schedules_from_form(form, "shift_day"),
        )
        db.commit()
    except hr.HRError as e:
        db.rollback()
        return RedirectResponse(f"/admin/work-shifts?error={e}", status_code=302)
    return RedirectResponse("/admin/work-shifts?saved=1", status_code=302)


@work_shifts_router.post("/{shift_id}/delete", response_class=HTMLResponse)
def work_shift_delete(
    shift_id: int,
    db: DBSession,
    _: User = Depends(_manage_perm),
):
    try:
        hr.delete_work_shift(db, shift_id)
        db.commit()
    except hr.HRError as e:
        db.rollback()
        return RedirectResponse(f"/admin/work-shifts?error={e}", status_code=302)
    return RedirectResponse("/admin/work-shifts?saved=1", status_code=302)


# =====================================================================
#                          أقسام العمل
# =====================================================================
departments_router = APIRouter(prefix="/admin/departments", tags=["hr-departments"])


@departments_router.get("", response_class=HTMLResponse)
def departments_page(
    request: Request,
    db: DBSession,
    _: User = Depends(_view_perm),
    error: str | None = Query(None),
    saved: int = Query(0),
):
    hr.ensure_default_departments(db)
    db.commit()
    items = hr.list_departments(db, only_active=False)
    emp_counts = hr.department_employee_counts(db)
    return templates.TemplateResponse(
        "admin_departments.html",
        {
            "request": request,
            "items": items,
            "emp_counts": emp_counts,
            "error": error,
            "saved": bool(saved),
        },
    )


@departments_router.post("/save", response_class=HTMLResponse)
def department_save(
    request: Request,
    db: DBSession,
    _: User = Depends(_manage_perm),
    dept_id: str = Form(""),
    name_ar: str = Form(...),
    code: str = Form(""),
    sort_order: str = Form("0"),
    notes: str = Form(""),
    is_active: str = Form(""),
):
    try:
        hr.upsert_department(
            db,
            dept_id=int(dept_id) if dept_id.strip().isdigit() else None,
            name_ar=name_ar,
            code=code or None,
            sort_order=_parse_int(sort_order, 0),
            is_active=is_active == "on",
            notes=notes or None,
        )
        db.commit()
    except hr.HRError as e:
        db.rollback()
        return RedirectResponse(f"/admin/departments?error={e}", status_code=302)
    return RedirectResponse("/admin/departments?saved=1", status_code=302)


@departments_router.post("/{dept_id}/delete", response_class=HTMLResponse)
def department_delete(
    dept_id: int,
    db: DBSession,
    _: User = Depends(_manage_perm),
):
    try:
        hr.delete_department(db, dept_id)
        db.commit()
    except hr.HRError as e:
        db.rollback()
        return RedirectResponse(f"/admin/departments?error={e}", status_code=302)
    return RedirectResponse("/admin/departments?saved=1", status_code=302)
