"""توفر الغرف وأنواعها حسب التاريخ."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.hotel.booking_models import (
    BookingStatus,
    HotelBooking,
    HotelRoomType,
    RecordKind,
    RoomPhysicalStatus,
)
from modules.hotel.models import HotelRoom


BLOCKING_STATUSES = (BookingStatus.CONFIRMED, BookingStatus.CHECKED_IN)
UNAVAILABLE_ROOM_STATUSES = (
    RoomPhysicalStatus.MAINTENANCE,
    RoomPhysicalStatus.OUT_OF_SERVICE,
    RoomPhysicalStatus.BLOCKED,
)
# لا تُعرَض للحجز/الإيجار حتى تُحرَّر من التنظيف أو الصيانة
NOT_RENTABLE_STATUSES = UNAVAILABLE_ROOM_STATUSES + (
    RoomPhysicalStatus.DIRTY,
    RoomPhysicalStatus.CLEANING,
    RoomPhysicalStatus.OCCUPIED,
)


def is_room_rentable(room: HotelRoom) -> bool:
    return bool(room.is_active) and room.physical_status not in NOT_RENTABLE_STATUSES


@dataclass(frozen=True)
class AvailabilityResult:
    room_type_id: int
    room_type_name: str
    available_count: int
    base_price: Decimal
    total_price: Decimal


def _nights(check_in: date, check_out: date) -> int:
    return max(0, (check_out - check_in).days)


def bookings_overlap_stmt(
    *,
    room_id: int | None = None,
    room_type_id: int | None = None,
    check_in: date,
    check_out: date,
    exclude_booking_id: int | None = None,
):
    stmt = select(HotelBooking).where(
        HotelBooking.booking_status.in_(BLOCKING_STATUSES),
        # نمنع أن ينتهي الحجز في نفس يوم وصول حجز آخر لنفس الشقة.
        # عملياً: إذا كان الحجز المستقبلي بعد 7 أيام، نقبل 6 أيام ونرفض 7.
        HotelBooking.check_in <= check_out,
        HotelBooking.check_out > check_in,
    )
    if room_id is not None:
        stmt = stmt.where(HotelBooking.room_id == room_id)
    if room_type_id is not None:
        stmt = stmt.where(HotelBooking.room_type_id == room_type_id)
    if exclude_booking_id is not None:
        stmt = stmt.where(HotelBooking.id != exclude_booking_id)
    return stmt


def first_room_conflict(
    db: Session,
    *,
    room_id: int,
    check_in: date,
    check_out: date,
    exclude_booking_id: int | None = None,
) -> HotelBooking | None:
    return db.scalar(
        bookings_overlap_stmt(
            room_id=room_id,
            check_in=check_in,
            check_out=check_out,
            exclude_booking_id=exclude_booking_id,
        )
        .order_by(HotelBooking.check_in.asc(), HotelBooking.id.asc())
        .limit(1)
    )


def has_room_conflict(
    db: Session,
    *,
    room_id: int,
    check_in: date,
    check_out: date,
    exclude_booking_id: int | None = None,
) -> bool:
    return first_room_conflict(
        db,
        room_id=room_id,
        check_in=check_in,
        check_out=check_out,
        exclude_booking_id=exclude_booking_id,
    ) is not None


@dataclass(frozen=True)
class RoomStayAvailability:
    """توفر الشقة لفترة مطلوبة — مع تاريخ الإتاحة إن كانت محجوزة."""

    full_stay_ok: bool
    available_from: date | None
    occupied_until: date | None
    status_key: str
    status_label: str


def room_stay_availability(
    db: Session,
    *,
    room_id: int,
    check_in: date,
    check_out: date,
    exclude_booking_id: int | None = None,
) -> RoomStayAvailability:
    """
    إن كانت الفترة محجوزة يُرجع أقرب تاريخ يمكن بدء إقامة بنفس المدة بعده.
    """
    if check_out <= check_in:
        return RoomStayAvailability(
            full_stay_ok=False,
            available_from=None,
            occupied_until=None,
            status_key="invalid",
            status_label="تواريخ غير صالحة",
        )
    nights = max(1, (check_out - check_in).days)
    conflict = first_room_conflict(
        db,
        room_id=room_id,
        check_in=check_in,
        check_out=check_out,
        exclude_booking_id=exclude_booking_id,
    )
    if conflict is None:
        return RoomStayAvailability(
            full_stay_ok=True,
            available_from=check_in,
            occupied_until=None,
            status_key="available",
            status_label="متاحة طوال فترة الإقامة",
        )

    occupied_until = conflict.check_out
    candidate = conflict.check_out
    # نتخطى الحجوزات المتتالية حتى نجد نافذة بطول الإقامة المطلوبة
    for _ in range(40):
        end = candidate + timedelta(days=nights)
        nxt = first_room_conflict(
            db,
            room_id=room_id,
            check_in=candidate,
            check_out=end,
            exclude_booking_id=exclude_booking_id,
        )
        if nxt is None:
            return RoomStayAvailability(
                full_stay_ok=False,
                available_from=candidate,
                occupied_until=occupied_until,
                status_key="booked",
                status_label=f"محجوزة لهذه الفترة — متاحة من {candidate.isoformat()}",
            )
        if nxt.check_out <= candidate:
            break
        occupied_until = nxt.check_out
        candidate = nxt.check_out

    return RoomStayAvailability(
        full_stay_ok=False,
        available_from=occupied_until,
        occupied_until=occupied_until,
        status_key="booked",
        status_label=f"محجوزة — أقرب إتاحة من {occupied_until.isoformat()}"
        if occupied_until
        else "محجوزة حالياً",
    )


def blocking_booking_on_date(
    db: Session, room_id: int, day: date
) -> HotelBooking | None:
    """حجز يمنع الإيجار في يوم محدد (مؤكّد أو مسكّن ضمن فترة الإقامة)."""
    return db.scalar(
        select(HotelBooking)
        .where(
            HotelBooking.room_id == room_id,
            HotelBooking.booking_status.in_(BLOCKING_STATUSES),
            HotelBooking.check_in <= day,
            HotelBooking.check_out > day,
        )
        .order_by(
            (HotelBooking.booking_status == BookingStatus.CHECKED_IN).desc(),
            HotelBooking.id.desc(),
        )
        .limit(1)
    )


def next_blocking_booking(
    db: Session, room_id: int, *, after_day: date | None = None
) -> HotelBooking | None:
    """أقرب حجز مؤكّد بعد يوم معيّن — لا يؤثر على توفر اليوم."""
    after_day = after_day or date.today()
    return db.scalar(
        select(HotelBooking)
        .where(
            HotelBooking.room_id == room_id,
            HotelBooking.booking_status.in_(BLOCKING_STATUSES),
            HotelBooking.check_in > after_day,
        )
        .order_by(HotelBooking.check_in.asc(), HotelBooking.id.asc())
        .limit(1)
    )


def room_rentability_on_date(
    db: Session, room: HotelRoom, day: date
) -> tuple[bool, str, str, HotelBooking | None, HotelBooking | None]:
    """
    توفر الشقة في يوم محدد.
    يُرجع: (متاحة؟, التسمية, المفتاح, حجز اليوم, أقرب حجز مستقبلي)
    """
    if not room.is_active:
        return False, "معطّلة", "inactive", None, None

    ps = room.physical_status
    if ps in UNAVAILABLE_ROOM_STATUSES:
        label = {
            RoomPhysicalStatus.MAINTENANCE: "غير متاحة — صيانة",
            RoomPhysicalStatus.OUT_OF_SERVICE: "خارج الخدمة",
            RoomPhysicalStatus.BLOCKED: "محجوبة",
        }.get(ps, "غير متاحة")
        key = "maintenance" if ps == RoomPhysicalStatus.MAINTENANCE else "blocked"
        return False, label, key, None, None

    booking = blocking_booking_on_date(db, room.id, day)
    if booking is not None:
        if booking.booking_status == BookingStatus.CHECKED_IN:
            return False, "مشغولة", "occupied", booking, None
        return False, "محجوزة", "reserved", booking, None

    if ps == RoomPhysicalStatus.OCCUPIED:
        return False, "مشغولة", "occupied", None, None
    if ps == RoomPhysicalStatus.DIRTY:
        return False, "للتنظيف", "dirty", None, None
    if ps == RoomPhysicalStatus.CLEANING:
        return False, "قيد التنظيف", "cleaning", None, None

    upcoming = next_blocking_booking(db, room.id, after_day=day)
    return True, "متاحة", "available", None, upcoming


def available_rooms_for_type(
    db: Session,
    *,
    room_type_id: int,
    check_in: date,
    check_out: date,
    exclude_booking_id: int | None = None,
) -> list[HotelRoom]:
    rooms = list(
        db.scalars(
            select(HotelRoom).where(
                HotelRoom.room_type_id == room_type_id,
                HotelRoom.is_active.is_(True),
                HotelRoom.physical_status.notin_(NOT_RENTABLE_STATUSES),
            )
        ).all()
    )
    out: list[HotelRoom] = []
    for room in rooms:
        if not has_room_conflict(
            db,
            room_id=room.id,
            check_in=check_in,
            check_out=check_out,
            exclude_booking_id=exclude_booking_id,
        ):
            out.append(room)
    return out


def search_availability(
    db: Session,
    *,
    check_in: date,
    check_out: date,
    adults: int = 1,
    children: int = 0,
    property_id: int = 1,
) -> list[AvailabilityResult]:
    if check_out <= check_in:
        return []
    nights = _nights(check_in, check_out)
    types = list(
        db.scalars(
            select(HotelRoomType).where(
                HotelRoomType.property_id == property_id,
                HotelRoomType.is_active.is_(True),
                HotelRoomType.capacity_adults >= adults,
            )
        ).all()
    )
    results: list[AvailabilityResult] = []
    for rt in types:
        avail = available_rooms_for_type(
            db, room_type_id=rt.id, check_in=check_in, check_out=check_out
        )
        if not avail:
            continue
        from modules.hotel.pricing import nightly_rate_for_stay

        nightly = nightly_rate_for_stay(db, room_type_id=rt.id, check_in=check_in, check_out=check_out)
        total = (nightly * nights).quantize(Decimal("0.001"))
        results.append(
            AvailabilityResult(
                room_type_id=rt.id,
                room_type_name=rt.name_ar,
                available_count=len(avail),
                base_price=rt.base_price,
                total_price=total,
            )
        )
    return results


def calendar_bookings(
    db: Session,
    *,
    start: date,
    end: date,
    property_id: int = 1,
) -> list[HotelBooking]:
    return list(
        db.scalars(
            select(HotelBooking)
            .where(
                HotelBooking.property_id == property_id,
                HotelBooking.record_kind == RecordKind.BOOKING,
                HotelBooking.check_in < end,
                HotelBooking.check_out > start,
                HotelBooking.booking_status.notin_(
                    (BookingStatus.CANCELLED, BookingStatus.NO_SHOW)
                ),
            )
            .order_by(HotelBooking.check_in, HotelBooking.id)
        ).all()
    )
