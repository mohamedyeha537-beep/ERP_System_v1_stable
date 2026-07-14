"""جدول العمل الأسبوعي — تخصيص يومي للوردية أو للموظف."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.datetime_local import to_local
from modules.hr.models import (
    Employee,
    EmployeeDaySchedule,
    WorkShift,
    WorkShiftDaySchedule,
)

# Python weekday: 0=الاثنين … 6=الأحد
WEEKDAY_LABELS: list[tuple[int, str]] = [
    (0, "الاثنين"),
    (1, "الثلاثاء"),
    (2, "الأربعاء"),
    (3, "الخميس"),
    (4, "الجمعة"),
    (5, "السبت"),
    (6, "الأحد"),
]


@dataclass(frozen=True)
class DayScheduleSpec:
    start_time: str
    end_time: str
    work_hours: Decimal
    grace_minutes: int
    is_rest_day: bool = False
    source: str = "default"

    @property
    def schedule_label(self) -> str:
        if self.is_rest_day:
            return "يوم راحة"
        return f"{self.start_time} – {self.end_time} ({self.work_hours} س)"


def spec_from_shift(shift: WorkShift) -> DayScheduleSpec:
    return DayScheduleSpec(
        start_time=shift.start_time,
        end_time=shift.end_time,
        work_hours=Decimal(str(shift.work_hours or 8)).quantize(Decimal("0.01")),
        grace_minutes=max(0, int(shift.grace_minutes or 0)),
        source="shift",
    )


def _merge_spec(base: DayScheduleSpec, row: WorkShiftDaySchedule | EmployeeDaySchedule) -> DayScheduleSpec:
    if row.is_rest_day:
        return DayScheduleSpec(
            start_time=base.start_time,
            end_time=base.end_time,
            work_hours=Decimal("0"),
            grace_minutes=base.grace_minutes,
            is_rest_day=True,
            source="day_override",
        )
    return DayScheduleSpec(
        start_time=row.start_time or base.start_time,
        end_time=row.end_time or base.end_time,
        work_hours=Decimal(str(row.work_hours if row.work_hours is not None else base.work_hours)).quantize(
            Decimal("0.01")
        ),
        grace_minutes=(
            int(row.grace_minutes)
            if row.grace_minutes is not None
            else base.grace_minutes
        ),
        source="day_override",
    )


def weekday_for_check_in(check_in: datetime) -> int:
    local = to_local(check_in)
    if local is None:
        return check_in.weekday()
    return local.weekday()


def resolve_effective_schedule(
    db: Session,
    *,
    employee_id: int,
    check_in: datetime,
    work_shift: WorkShift | None = None,
) -> DayScheduleSpec | None:
    """الجدول الفعلي ليوم الحضور: موظف → وردية → افتراضي."""
    emp = db.get(Employee, employee_id)
    if emp is None:
        return None

    dow = weekday_for_check_in(check_in)
    base_shift = work_shift
    if base_shift is None and emp.work_shift_id:
        base_shift = db.get(WorkShift, emp.work_shift_id)

    base: DayScheduleSpec | None
    if base_shift is not None:
        base = spec_from_shift(base_shift)
        shift_day = db.scalar(
            select(WorkShiftDaySchedule).where(
                WorkShiftDaySchedule.work_shift_id == base_shift.id,
                WorkShiftDaySchedule.day_of_week == dow,
            )
        )
        if shift_day is not None:
            base = _merge_spec(base, shift_day)
    else:
        std = Decimal(str(emp.standard_hours_per_day or 8)).quantize(Decimal("0.01"))
        base = DayScheduleSpec(
            start_time="08:00",
            end_time="16:00",
            work_hours=std,
            grace_minutes=15,
            source="employee_default",
        )

    emp_day = db.scalar(
        select(EmployeeDaySchedule).where(
            EmployeeDaySchedule.employee_id == employee_id,
            EmployeeDaySchedule.day_of_week == dow,
        )
    )
    if emp_day is not None:
        return _merge_spec(base, emp_day)
    return base


def list_employee_day_schedules(db: Session, employee_id: int) -> dict[int, EmployeeDaySchedule]:
    rows = db.scalars(
        select(EmployeeDaySchedule).where(EmployeeDaySchedule.employee_id == employee_id)
    ).all()
    return {int(r.day_of_week): r for r in rows}


def list_work_shift_day_schedules(db: Session, work_shift_id: int) -> dict[int, WorkShiftDaySchedule]:
    rows = db.scalars(
        select(WorkShiftDaySchedule).where(
            WorkShiftDaySchedule.work_shift_id == work_shift_id
        )
    ).all()
    return {int(r.day_of_week): r for r in rows}


def list_all_work_shift_day_schedules(db: Session) -> dict[int, dict[int, WorkShiftDaySchedule]]:
    rows = db.scalars(select(WorkShiftDaySchedule)).all()
    out: dict[int, dict[int, WorkShiftDaySchedule]] = {}
    for r in rows:
        out.setdefault(int(r.work_shift_id), {})[int(r.day_of_week)] = r
    return out


def _parse_hhmm(value: str) -> tuple[int, int]:
    parts = (value or "00:00").strip().split(":")
    h = int(parts[0]) if parts and parts[0].isdigit() else 0
    m = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    return h, m


def validate_hhmm(value: str) -> str:
    s = (value or "").strip()
    h, m = _parse_hhmm(s)
    if h < 0 or h > 23 or m < 0 or m > 59:
        raise ValueError("وقت غير صالح (استخدم HH:MM).")
    return f"{h:02d}:{m:02d}"


def _parse_optional_hhmm(raw: str | None) -> str | None:
    s = (raw or "").strip()
    if not s:
        return None
    return validate_hhmm(s[:5] if len(s) >= 5 else s)


def save_employee_day_schedules(
    db: Session,
    employee_id: int,
    day_rows: dict[int, dict[str, object]],
) -> None:
    """day_rows: dow -> {is_rest_day, start_time, end_time, work_hours, grace_minutes} أو حذف."""
    existing = {
        r.day_of_week: r
        for r in db.scalars(
            select(EmployeeDaySchedule).where(
                EmployeeDaySchedule.employee_id == employee_id
            )
        ).all()
    }
    for dow in range(7):
        if dow not in day_rows:
            if dow in existing:
                db.delete(existing[dow])
            continue
        payload = day_rows[dow]
        row = existing.get(dow)
        if row is None:
            row = EmployeeDaySchedule(employee_id=employee_id, day_of_week=dow)
            db.add(row)
        row.is_rest_day = bool(payload.get("is_rest_day"))
        row.start_time = _parse_optional_hhmm(str(payload.get("start_time") or ""))
        row.end_time = _parse_optional_hhmm(str(payload.get("end_time") or ""))
        wh = payload.get("work_hours")
        row.work_hours = (
            Decimal(str(wh)).quantize(Decimal("0.01")) if wh not in (None, "") else None
        )
        gm = payload.get("grace_minutes")
        row.grace_minutes = int(gm) if gm not in (None, "") else None
    db.flush()


def save_work_shift_day_schedules(
    db: Session,
    work_shift_id: int,
    day_rows: dict[int, dict[str, object]],
) -> None:
    existing = {
        r.day_of_week: r
        for r in db.scalars(
            select(WorkShiftDaySchedule).where(
                WorkShiftDaySchedule.work_shift_id == work_shift_id
            )
        ).all()
    }
    for dow in range(7):
        if dow not in day_rows:
            if dow in existing:
                db.delete(existing[dow])
            continue
        payload = day_rows[dow]
        row = existing.get(dow)
        if row is None:
            row = WorkShiftDaySchedule(work_shift_id=work_shift_id, day_of_week=dow)
            db.add(row)
        row.is_rest_day = bool(payload.get("is_rest_day"))
        row.start_time = _parse_optional_hhmm(str(payload.get("start_time") or ""))
        row.end_time = _parse_optional_hhmm(str(payload.get("end_time") or ""))
        wh = payload.get("work_hours")
        row.work_hours = (
            Decimal(str(wh)).quantize(Decimal("0.01")) if wh not in (None, "") else None
        )
        gm = payload.get("grace_minutes")
        row.grace_minutes = int(gm) if gm not in (None, "") else None
    db.flush()


def parse_day_schedules_from_form(form, prefix: str) -> dict[int, dict[str, object]]:
    """يقرأ حقول النموذج: {prefix}_{dow}_override / _rest / _start / _end / _hours / _grace."""
    out: dict[int, dict[str, object]] = {}
    for dow, _label in WEEKDAY_LABELS:
        if form.get(f"{prefix}_{dow}_override") != "on":
            continue
        out[dow] = {
            "is_rest_day": form.get(f"{prefix}_{dow}_rest") == "on",
            "start_time": (form.get(f"{prefix}_{dow}_start") or "").strip(),
            "end_time": (form.get(f"{prefix}_{dow}_end") or "").strip(),
            "work_hours": (form.get(f"{prefix}_{dow}_hours") or "").strip(),
            "grace_minutes": (form.get(f"{prefix}_{dow}_grace") or "").strip(),
        }
    return out
