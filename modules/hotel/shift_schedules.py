"""إعداد مواعيد ورديات الفندق (3 ورديات افتراضياً)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy.orm import Session

from app.datetime_local import now_local, store_timezone
from modules.settings.service import get_int, get_setting, set_setting

SCHEDULES_KEY = "hotel_shift_schedules_json"
OVERDUE_MINUTES_KEY = "hotel_shift_overdue_minutes"
SUPERVISOR_PHONE_KEY = "hotel_shift_supervisor_phone"
OVERDUE_ENABLED_KEY = "hotel_shift_overdue_alert_enabled"

DEFAULT_SCHEDULES: list[dict] = [
    {"number": 1, "name_ar": "الوردية الأولى", "start": "08:00", "end": "16:00"},
    {"number": 2, "name_ar": "الوردية الثانية", "start": "16:00", "end": "00:00"},
    {"number": 3, "name_ar": "الوردية الثالثة", "start": "00:00", "end": "08:00"},
]


@dataclass(frozen=True)
class ShiftSlot:
    number: int
    name_ar: str
    scheduled_start: datetime
    scheduled_end: datetime


def _parse_hm(raw: str) -> tuple[int, int]:
    parts = (raw or "00:00").strip().split(":")
    h = int(parts[0]) if parts else 0
    m = int(parts[1]) if len(parts) > 1 else 0
    return h % 24, m % 60


def load_shift_schedules(db: Session) -> list[dict]:
    raw = (get_setting(db, SCHEDULES_KEY) or "").strip()
    if not raw:
        return [dict(x) for x in DEFAULT_SCHEDULES]
    try:
        data = json.loads(raw)
        if isinstance(data, list) and data:
            out = []
            for row in data:
                if not isinstance(row, dict):
                    continue
                num = int(row.get("number") or 0)
                if num <= 0:
                    continue
                out.append(
                    {
                        "number": num,
                        "name_ar": (row.get("name_ar") or f"الوردية {num}").strip(),
                        "start": (row.get("start") or "08:00").strip(),
                        "end": (row.get("end") or "16:00").strip(),
                    }
                )
            if out:
                return sorted(out, key=lambda r: r["number"])
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    return [dict(x) for x in DEFAULT_SCHEDULES]


def save_shift_schedules(db: Session, schedules: list[dict]) -> None:
    cleaned = []
    for row in schedules:
        num = int(row.get("number") or 0)
        if num <= 0:
            continue
        cleaned.append(
            {
                "number": num,
                "name_ar": (row.get("name_ar") or f"الوردية {num}").strip(),
                "start": (row.get("start") or "08:00").strip()[:5],
                "end": (row.get("end") or "16:00").strip()[:5],
            }
        )
    if not cleaned:
        cleaned = [dict(x) for x in DEFAULT_SCHEDULES]
    set_setting(db, SCHEDULES_KEY, json.dumps(cleaned, ensure_ascii=False))


def window_for_date(day: date, schedule: dict) -> tuple[datetime, datetime]:
    tz = store_timezone()
    sh, sm = _parse_hm(schedule["start"])
    eh, em = _parse_hm(schedule["end"])
    start = datetime.combine(day, time(sh, sm), tzinfo=tz)
    if eh == 0 and em == 0 and (sh, sm) != (0, 0):
        end = datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=tz)
    elif (eh, em) <= (sh, sm) and not (eh == 0 and em == 0):
        end = datetime.combine(day + timedelta(days=1), time(eh, em), tzinfo=tz)
    else:
        end = datetime.combine(day, time(eh, em), tzinfo=tz)
    return start, end


def active_shift_slot(db: Session, now: datetime | None = None) -> ShiftSlot:
    now = now or now_local()
    if now.tzinfo is None:
        now = now.replace(tzinfo=store_timezone())
    else:
        now = now.astimezone(store_timezone())

    schedules = load_shift_schedules(db)
    today = now.date()
    candidates: list[tuple[datetime, datetime, dict]] = []
    for day in (today - timedelta(days=1), today):
        for sch in schedules:
            start, end = window_for_date(day, sch)
            if start <= now < end:
                candidates.append((start, end, sch))
    if not candidates:
        sch = schedules[0]
        start, end = window_for_date(today, sch)
        return ShiftSlot(number=int(sch["number"]), name_ar=sch["name_ar"], scheduled_start=start, scheduled_end=end)
    start, end, sch = max(candidates, key=lambda x: x[0])
    return ShiftSlot(
        number=int(sch["number"]),
        name_ar=sch["name_ar"],
        scheduled_start=start,
        scheduled_end=end,
    )


def overdue_grace_minutes(db: Session) -> int:
    return max(5, get_int(db, OVERDUE_MINUTES_KEY, 30))


def supervisor_phone(db: Session) -> str:
    return (get_setting(db, SUPERVISOR_PHONE_KEY) or "").strip()


def overdue_alerts_enabled(db: Session) -> bool:
    from modules.settings.service import get_bool

    return get_bool(db, OVERDUE_ENABLED_KEY, True)
