"""شارات تنبيه قائمة الفندق — أعداد تشغيلية حالية لكل قسم."""
from __future__ import annotations

import time
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from modules.hotel.booking_models import (
    BookingStatus,
    HotelBooking,
    QuotationStatus,
    RecordKind,
    RoomPhysicalStatus,
)
from modules.hotel.models import HotelRoom

_BADGE_CACHE: dict[int, tuple[float, dict[str, int]]] = {}
_BADGE_TTL_SEC = 120.0


def invalidate_hotel_nav_badges(property_id: int | None = None) -> None:
    """مسح كاش الشارات بعد تغيير حالة غرفة (تنظيف / تعيين / صيانة)."""
    if property_id is None:
        _BADGE_CACHE.clear()
    else:
        _BADGE_CACHE.pop(int(property_id), None)


def hotel_nav_badge_counts(db: Session, *, property_id: int = 1) -> dict[str, int]:
    """أعداد تظهر كشارة حمراء على كروت تنقل الفندق (واللوحة الرئيسية عند الربط)."""
    now = time.monotonic()
    hit = _BADGE_CACHE.get(property_id)
    if hit is not None and (now - hit[0]) < _BADGE_TTL_SEC:
        return dict(hit[1])

    today = date.today()
    tomorrow = today + timedelta(days=1)
    counts: dict[str, int] = {}

    # use local calendar day when available
    try:
        from app.datetime_local import now_local

        today = now_local().date()
        tomorrow = today + timedelta(days=1)
    except Exception:
        pass

    active_bookings = int(
        db.scalar(
            select(func.count())
            .select_from(HotelBooking)
            .where(
                HotelBooking.property_id == property_id,
                HotelBooking.record_kind == RecordKind.BOOKING,
                HotelBooking.booking_status.in_(
                    [
                        BookingStatus.PENDING,
                        BookingStatus.CONFIRMED,
                        BookingStatus.CHECKED_IN,
                    ]
                ),
            )
        )
        or 0
    )
    if active_bookings:
        counts["hotel_bookings"] = active_bookings

    occupied = int(
        db.scalar(
            select(func.count())
            .select_from(HotelBooking)
            .where(
                HotelBooking.property_id == property_id,
                HotelBooking.record_kind == RecordKind.BOOKING,
                HotelBooking.booking_status == BookingStatus.CHECKED_IN,
            )
        )
        or 0
    )
    if occupied:
        counts["hotel_dashboard"] = occupied

    open_quotations = int(
        db.scalar(
            select(func.count())
            .select_from(HotelBooking)
            .where(
                HotelBooking.property_id == property_id,
                HotelBooking.record_kind == RecordKind.QUOTATION,
                HotelBooking.quotation_status.in_(
                    [
                        QuotationStatus.DRAFT,
                        QuotationStatus.SENT,
                        QuotationStatus.UNDER_REVIEW,
                        QuotationStatus.ACCEPTED,
                    ]
                ),
            )
        )
        or 0
    )
    if open_quotations:
        counts["hotel_quotations"] = open_quotations

    arrivals = int(
        db.scalar(
            select(func.count())
            .select_from(HotelBooking)
            .where(
                HotelBooking.property_id == property_id,
                HotelBooking.record_kind == RecordKind.BOOKING,
                HotelBooking.check_in == today,
                HotelBooking.booking_status.in_(
                    [BookingStatus.PENDING, BookingStatus.CONFIRMED]
                ),
            )
        )
        or 0
    )
    departures = int(
        db.scalar(
            select(func.count())
            .select_from(HotelBooking)
            .where(
                HotelBooking.property_id == property_id,
                HotelBooking.record_kind == RecordKind.BOOKING,
                HotelBooking.check_out.in_([today, tomorrow]),
                HotelBooking.booking_status == BookingStatus.CHECKED_IN,
            )
        )
        or 0
    )
    calendar_n = arrivals + departures
    if calendar_n:
        counts["hotel_calendar"] = calendar_n
    if departures:
        counts["hotel_departures_soon"] = departures

    try:
        from modules.hotel.follow_up import follow_ups_due_count

        fu = int(follow_ups_due_count(db, property_id=property_id))
        if fu:
            counts["hotel_follow_ups"] = fu
    except Exception:
        pass

    # تنبيه التنظيف: يبقى حتى تصبح الشقة نظيفة (AVAILABLE) —
    # يشمل «للتنظيف» و«قيد التنظيف» بعد تعيين الموظف.
    cleaning_pending = int(
        db.scalar(
            select(func.count())
            .select_from(HotelRoom)
            .where(
                HotelRoom.is_active.is_(True),
                HotelRoom.physical_status.in_(
                    [
                        RoomPhysicalStatus.DIRTY,
                        RoomPhysicalStatus.CLEANING,
                    ]
                ),
            )
        )
        or 0
    )
    maintenance_n = int(
        db.scalar(
            select(func.count())
            .select_from(HotelRoom)
            .where(
                HotelRoom.is_active.is_(True),
                HotelRoom.physical_status == RoomPhysicalStatus.MAINTENANCE,
            )
        )
        or 0
    )
    housekeeping = cleaning_pending + maintenance_n
    if housekeeping:
        counts["hotel_housekeeping"] = housekeeping
    if cleaning_pending:
        counts["hotel_cleaning"] = cleaning_pending

    try:
        from modules.dashboard_notify.pending import pending_hotel_settle_count

        settle = int(pending_hotel_settle_count(db))
    except Exception:
        settle = 0
    if settle:
        counts["hotel_settle"] = settle

    try:
        from modules.hotel.booking_debts import debts_nav_badge_count

        debts_badge = int(debts_nav_badge_count(db) or 0)
        if debts_badge:
            counts["hotel_debts"] = debts_badge
    except Exception:
        pass

    try:
        from modules.hotel.shift_service import get_open_shift

        if get_open_shift(db, property_id=property_id) is None:
            counts["hotel_shift"] = 1
    except Exception:
        pass

    _BADGE_CACHE[property_id] = (now, dict(counts))
    return counts
