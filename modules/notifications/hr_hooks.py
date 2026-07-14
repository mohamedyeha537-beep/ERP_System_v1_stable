"""إشعارات الموارد البشرية — حضور، رواتب، سلف، خصومات."""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.datetime_local import format_local_dt
from modules.hr.models import AttendanceRecord, Employee, EmployeeDeduction, PayrollEntry, PayrollRun, SalaryAdvance
from modules.notifications.events import (
    HR_ADVANCE_GIVEN,
    HR_ATTENDANCE_CHECK_IN,
    HR_ATTENDANCE_CHECK_OUT,
    HR_DEDUCTION_CREATED,
    HR_PAYROLL_PAID,
    HR_PAYROLL_POSTED,
)
from modules.notifications.service import emit_event_background_safe, emit_event_safe


def _emp_payload(emp: Employee) -> dict[str, Any]:
    return {
        "employee_id": emp.id,
        "employee_name": emp.full_name_ar,
        "employee_phone": emp.phone or "",
        "phone": emp.phone or "",
        "name": emp.full_name_ar,
    }


def _fmt_money(val: Decimal | float | int | None) -> str:
    if val is None:
        return "0"
    return str(Decimal(str(val)).quantize(Decimal("0.001")))


def emit_attendance_check_in(db: Session, rec: AttendanceRecord) -> None:
    emp = db.get(Employee, rec.employee_id)
    if emp is None or not (emp.phone or "").strip():
        return
    payload = {
        **_emp_payload(emp),
        "attendance_id": rec.id,
        "check_in_time": format_local_dt(rec.check_in, "%Y-%m-%d %H:%M"),
        "source": rec.source_label,
    }
    emit_event_background_safe(
        HR_ATTENDANCE_CHECK_IN,
        source_type="hr_attendance",
        source_id=rec.id,
        payload=payload,
    )


def emit_attendance_check_out(db: Session, rec: AttendanceRecord) -> None:
    emp = db.get(Employee, rec.employee_id)
    if emp is None or not (emp.phone or "").strip():
        return
    ot_hours = round((rec.overtime_minutes or 0) / 60.0, 2)
    payload = {
        **_emp_payload(emp),
        "attendance_id": rec.id,
        "check_in_time": format_local_dt(rec.check_in, "%Y-%m-%d %H:%M"),
        "check_out_time": format_local_dt(rec.check_out, "%Y-%m-%d %H:%M") if rec.check_out else "",
        "hours_worked": str(rec.hours_worked),
        "late_minutes": rec.late_minutes or 0,
        "early_leave_minutes": rec.early_leave_minutes or 0,
        "overtime_hours": ot_hours,
        "attendance_status": rec.attendance_status_label,
        "notes": (rec.notes or "").strip(),
    }
    emit_event_background_safe(
        HR_ATTENDANCE_CHECK_OUT,
        source_type="hr_attendance",
        source_id=rec.id,
        payload=payload,
    )


def emit_payroll_paid_for_entry(db: Session, entry: PayrollEntry, *, run: PayrollRun) -> None:
    emp = entry.employee
    if emp is None:
        emp = db.get(Employee, entry.employee_id)
    if emp is None or not (emp.phone or "").strip():
        return
    from modules.notifications.action_handler import create_pending_action

    act = create_pending_action(
        db,
        action_key="payroll_confirm",
        payload={
            "kind": "payroll_confirm",
            "payroll_entry_id": entry.id,
            "employee_id": emp.id,
            "run_id": run.id,
        },
    )
    db.flush()
    payload = {
        **_emp_payload(emp),
        "payroll_entry_id": entry.id,
        "run_id": run.id,
        "period_label": run.label,
        "net_pay": _fmt_money(entry.net_pay),
        "gross_pay": _fmt_money(entry.gross_pay),
        "deductions": _fmt_money(entry.deductions),
        "advances": _fmt_money(entry.advances),
        "bonuses": _fmt_money(entry.bonuses),
        "overtime_pay": _fmt_money(entry.overtime_pay),
        "action_id": act.id,
    }
    emit_event_safe(
        db,
        event_key=HR_PAYROLL_PAID,
        source_type="hr_payroll_entry",
        source_id=entry.id,
        payload=payload,
    )


def notify_payroll_run_paid(db: Session, run: PayrollRun) -> None:
    """يُستدعى بعد pay_run — إشعار كل موظف براتبه (مع زر تأكيد)."""
    for entry in run.entries:
        emit_payroll_paid_for_entry(db, entry, run=run)


def emit_payroll_posted(db: Session, run: PayrollRun) -> None:
    """ملخص لمدير الموارد البشرية عند اعتماد كشف الرواتب."""
    payload = {
        "run_id": run.id,
        "period_label": run.label,
        "employee_count": len(run.entries),
        "total_net": _fmt_money(run.total_net),
        "message": (
            f"تم اعتماد كشف رواتب {run.label} — {len(run.entries)} موظف — "
            f"الإجمالي {_fmt_money(run.total_net)}"
        ),
    }
    emit_event_background_safe(
        HR_PAYROLL_POSTED,
        source_type="hr_payroll_run",
        source_id=run.id,
        payload=payload,
    )


def emit_advance_given(db: Session, adv: SalaryAdvance) -> None:
    emp = adv.employee
    if emp is None:
        emp = db.get(Employee, adv.employee_id)
    if emp is None:
        return
    payload = {
        **_emp_payload(emp),
        "advance_id": adv.id,
        "advance_amount": _fmt_money(adv.amount),
        "notes": (adv.notes or "").strip(),
    }
    if (emp.phone or "").strip():
        emit_event_background_safe(
            HR_ADVANCE_GIVEN,
            source_type="hr_advance",
            source_id=adv.id,
            payload=payload,
        )


def emit_deduction_created(db: Session, ded: EmployeeDeduction) -> None:
    emp = ded.employee
    if emp is None:
        emp = db.get(Employee, ded.employee_id)
    if emp is None:
        return
    payload = {
        **_emp_payload(emp),
        "deduction_id": ded.id,
        "deduction_amount": _fmt_money(ded.amount),
        "deduction_note": (ded.note or "").strip(),
        "source_type": ded.source_type or "",
    }
    if (emp.phone or "").strip():
        emit_event_background_safe(
            HR_DEDUCTION_CREATED,
            source_type="hr_deduction",
            source_id=ded.id,
            payload=payload,
        )
