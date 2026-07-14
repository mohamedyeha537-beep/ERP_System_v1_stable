"""إشعارات ورديات الفندق."""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy.orm import Session

from modules.hotel.shift_models import HotelShift
from modules.notifications.events import HOTEL_SHIFT_CLOSED, HOTEL_SHIFT_OPENED, HOTEL_SHIFT_OVERDUE
from modules.notifications.service import emit_event_safe
from modules.hotel.shift_schedules import supervisor_phone
from app.datetime_local import format_local_dt


def emit_hotel_shift_opened(db: Session, shift: HotelShift, *, operator_name: str = "") -> None:
    emit_event_safe(
        db,
        event_key=HOTEL_SHIFT_OPENED,
        source_type="hotel_shift",
        source_id=shift.id,
        payload={
            "shift_id": shift.id,
            "shift_number": shift.shift_number,
            "shift_name": shift.shift_name_ar,
            "operator_name": operator_name,
            "scheduled_end": format_local_dt(shift.scheduled_end, "%Y-%m-%d %H:%M"),
        },
    )


def emit_hotel_shift_closed(
    db: Session,
    shift: HotelShift,
    *,
    operator_name: str = "",
    revenue: Decimal,
    expenses: Decimal,
) -> None:
    net = (revenue - expenses).quantize(Decimal("0.001"))
    emit_event_safe(
        db,
        event_key=HOTEL_SHIFT_CLOSED,
        source_type="hotel_shift",
        source_id=shift.id,
        payload={
            "shift_id": shift.id,
            "shift_number": shift.shift_number,
            "shift_name": shift.shift_name_ar,
            "operator_name": operator_name,
            "revenue": f"{revenue:.3f}",
            "expenses": f"{expenses:.3f}",
            "net_total": f"{net:.3f}",
            "payment_count": shift.payment_count,
            "expense_count": shift.expense_count,
        },
    )


def emit_hotel_shift_overdue(db: Session, shift: HotelShift, *, overdue_minutes: int) -> None:
    operator = ""
    if shift.user:
        operator = (getattr(shift.user, "username", None) or "").strip()
    phone = supervisor_phone(db)
    payload = {
        "shift_id": shift.id,
        "shift_number": shift.shift_number,
        "shift_name": shift.shift_name_ar,
        "operator_name": operator,
        "overdue_minutes": str(overdue_minutes),
        "scheduled_end": format_local_dt(shift.scheduled_end, "%Y-%m-%d %H:%M"),
        "supervisor_phone": phone,
    }
    emit_event_safe(
        db,
        event_key=HOTEL_SHIFT_OVERDUE,
        source_type="hotel_shift",
        source_id=shift.id,
        payload=payload,
    )


def scan_overdue_hotel_shifts(db: Session) -> int:
    from datetime import datetime, timezone

    from modules.hotel.shift_schedules import overdue_alerts_enabled, overdue_grace_minutes
    from modules.hotel.shift_service import get_open_shift, shift_is_overdue, shift_overdue_minutes

    if not overdue_alerts_enabled(db):
        return 0
    grace = overdue_grace_minutes(db)
    shift = get_open_shift(db)
    if shift is None or not shift_is_overdue(shift, grace_minutes=grace):
        return 0
    now = datetime.now(timezone.utc)
    if shift.overdue_notified_at is not None:
        last = shift.overdue_notified_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if (now - last).total_seconds() < 3600:
            return 0
    mins = shift_overdue_minutes(shift)
    emit_hotel_shift_overdue(db, shift, overdue_minutes=mins)
    shift.overdue_notified_at = now
    db.flush()
    return 1
