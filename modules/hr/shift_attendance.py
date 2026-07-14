"""تحليل الحضور مقابل وردية العمل: تأخير، انصراف مبكر، ساعات إضافية."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from modules.hr.models import AttendanceRecord, OvertimeApprovalStatus, WorkShift
from modules.hr.schedule import DayScheduleSpec, spec_from_shift, validate_hhmm


def _ensure_aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _shift_datetime(base: datetime, hhmm: str) -> datetime:
    base = _ensure_aware(base)
    parts = (hhmm or "00:00").strip().split(":")
    h = int(parts[0]) if parts and parts[0].isdigit() else 0
    m = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    return base.replace(hour=h, minute=m, second=0, microsecond=0)


def analyze_session(
    check_in: datetime,
    check_out: datetime,
    schedule: DayScheduleSpec | WorkShift | None,
) -> tuple[int, int, int]:
    """يُرجع (دقائق التأخير، دقائق الانصراف المبكر، دقائق الإضافي)."""
    if schedule is None:
        return 0, 0, 0
    if isinstance(schedule, WorkShift):
        schedule = spec_from_shift(schedule)

    check_in = _ensure_aware(check_in)
    check_out = _ensure_aware(check_out)
    if check_out <= check_in:
        return 0, 0, 0

    worked_min = int((check_out - check_in).total_seconds() // 60)
    if schedule.is_rest_day:
        return 0, 0, max(0, worked_min)

    sched_start = _shift_datetime(check_in, schedule.start_time)
    sched_end = _shift_datetime(check_in, schedule.end_time)
    if sched_end <= sched_start:
        sched_end += timedelta(days=1)

    grace = max(0, int(schedule.grace_minutes or 0))
    allowed_start = sched_start + timedelta(minutes=grace)

    late = 0
    if check_in > allowed_start:
        late = int((check_in - allowed_start).total_seconds() // 60)

    early = 0
    if check_out < sched_end:
        early = int((sched_end - check_out).total_seconds() // 60)

    overtime = 0
    if check_out > sched_end:
        overtime = int((check_out - sched_end).total_seconds() // 60)

    std_min = int(float(schedule.work_hours or 8) * 60)
    excess = max(0, worked_min - std_min)
    overtime = max(overtime, excess)

    return late, early, overtime


def apply_attendance_metrics(
    rec: AttendanceRecord,
    schedule: DayScheduleSpec | WorkShift | None,
    *,
    fallback_work_hours: Decimal | float | None = None,
) -> None:
    ot = 0
    if rec.check_out is None:
        rec.late_minutes = 0
        rec.early_leave_minutes = 0
        rec.overtime_minutes = 0
        rec.expected_work_hours = None
        return
    if schedule is None and fallback_work_hours is not None:
        check_in = _ensure_aware(rec.check_in)
        check_out = _ensure_aware(rec.check_out)
        worked_min = int((check_out - check_in).total_seconds() // 60)
        std_min = int(float(fallback_work_hours) * 60)
        ot = max(0, worked_min - std_min)
        rec.late_minutes = 0
        rec.early_leave_minutes = 0
        rec.overtime_minutes = ot
        rec.expected_work_hours = Decimal(str(fallback_work_hours)).quantize(Decimal("0.01"))
    else:
        late, early, ot = analyze_session(rec.check_in, rec.check_out, schedule)
        rec.late_minutes = late
        rec.early_leave_minutes = early
        rec.overtime_minutes = ot
        if isinstance(schedule, DayScheduleSpec):
            rec.expected_work_hours = schedule.work_hours
        elif isinstance(schedule, WorkShift):
            rec.expected_work_hours = Decimal(str(schedule.work_hours or 8)).quantize(
                Decimal("0.01")
            )
        else:
            rec.expected_work_hours = None
    if ot > 0:
        if rec.overtime_approval_status not in (
            OvertimeApprovalStatus.APPROVED,
            OvertimeApprovalStatus.REJECTED,
        ):
            rec.overtime_approval_status = OvertimeApprovalStatus.PENDING
            rec.approved_overtime_minutes = 0
    else:
        rec.overtime_approval_status = OvertimeApprovalStatus.NONE
        rec.approved_overtime_minutes = 0
