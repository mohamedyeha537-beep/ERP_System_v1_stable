"""لوحة توفر الشقق — حالة اليوم لكل غرفة."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.hotel.availability import (
    UNAVAILABLE_ROOM_STATUSES,
    room_rentability_on_date,
)
from modules.hotel.booking_models import (
    BookingSource,
    BookingStatus,
    HotelBooking,
    HotelProperty,
    RoomPhysicalStatus,
)
from modules.hotel.models import HotelRoom


@dataclass(frozen=True)
class RoomDashboardCard:
    room_id: int
    display_name: str
    number: str
    floor: str | None
    property_name: str
    nightly_price: Decimal
    capacity_adults: int
    capacity_children: int
    is_available: bool
    status_label: str
    status_key: str
    status_detail: str | None
    guest_name: str | None
    booking_id: int | None
    check_out: date | None
    image_url: str | None
    is_active: bool
    room_type_name: str | None
    balance_due: Decimal = Decimal("0")
    debt_stay: Decimal = Decimal("0")
    debt_laundry: Decimal = Decimal("0")
    debt_restaurant: Decimal = Decimal("0")
    checkout_today: bool = False
    upcoming_booking_id: int | None = None
    upcoming_check_in: date | None = None
    upcoming_guest_name: str | None = None


@dataclass(frozen=True)
class DebtAlertRow:
    room_id: int | None
    room_label: str
    booking_id: int | None
    guest_name: str | None
    amount: Decimal
    category: str
    category_label: str


@dataclass(frozen=True)
class FrontDeskSummary:
    available: int
    occupied: int
    reserved: int
    dirty: int
    maintenance: int
    checkout_due: int
    arrivals_today: int
    online_requests: int
    debt_stay_total: Decimal
    debt_laundry_total: Decimal
    debt_restaurant_total: Decimal
    debt_grand_total: Decimal
    debt_stay_rooms: int
    debt_laundry_rooms: int
    debt_restaurant_rooms: int
    checkout_list: list
    arrivals_list: list
    online_list: list
    debt_alerts: list[DebtAlertRow]


_STATUS_LABELS: dict[RoomPhysicalStatus, str] = {
    RoomPhysicalStatus.MAINTENANCE: "غير متاحة — صيانة",
    RoomPhysicalStatus.OUT_OF_SERVICE: "خارج الخدمة",
    RoomPhysicalStatus.BLOCKED: "محجوبة",
    RoomPhysicalStatus.DIRTY: "للتنظيف",
    RoomPhysicalStatus.OCCUPIED: "مشغولة",
    RoomPhysicalStatus.RESERVED: "محجوزة",
}


def maintenance_notes_for_rooms(db: Session, room_ids: list[int]) -> dict[int, str]:
    """آخر سبب صيانة مسجّل لكل شقة."""
    if not room_ids:
        return {}
    from modules.hotel.booking_models import HotelRoomStatusLog

    rows = list(
        db.scalars(
            select(HotelRoomStatusLog)
            .where(
                HotelRoomStatusLog.room_id.in_(room_ids),
                HotelRoomStatusLog.to_status == RoomPhysicalStatus.MAINTENANCE.value,
            )
            .order_by(HotelRoomStatusLog.room_id.asc(), HotelRoomStatusLog.created_at.desc())
        ).all()
    )
    out: dict[int, str] = {}
    for row in rows:
        rid = int(row.room_id)
        if rid in out:
            continue
        note = (row.note or "").strip()
        if note:
            out[rid] = note
    return out


def room_display_name(room: HotelRoom) -> str:
    name = (getattr(room, "name_ar", None) or "").strip()
    if name:
        return name
    num = (room.number or "").strip()
    if num.startswith("شقة") or num.startswith("VIP"):
        return num
    return f"شقة {num}"


def _nightly_price_for_room(db: Session, room: HotelRoom, day: date) -> Decimal:
    override = getattr(room, "nightly_price", None)
    if override is not None and Decimal(str(override)) > 0:
        return Decimal(str(override)).quantize(Decimal("0.001"))
    if room.room_type_id:
        from modules.hotel.pricing import nightly_rate_for_date

        return nightly_rate_for_date(db, room_type_id=room.room_type_id, night=day)
    return Decimal("0")


def room_availability_for_date(
    db: Session, room: HotelRoom, day: date
) -> tuple[bool, str, str, HotelBooking | None]:
    avail, label, key, booking, _upcoming = room_rentability_on_date(db, room, day)
    return avail, label, key, booking


def build_room_dashboard(
    db: Session,
    *,
    day: date | None = None,
    property_id: int = 1,
) -> tuple[list[RoomDashboardCard], dict[str, int]]:
    day = day or date.today()
    prop = db.get(HotelProperty, property_id)
    property_name = (prop.name_ar if prop else "") or "العقار"

    rooms = list(
        db.scalars(
            select(HotelRoom)
            .where(HotelRoom.property_id == property_id)
            .order_by(HotelRoom.is_active.desc(), HotelRoom.floor, HotelRoom.number)
        ).all()
    )

    from modules.hotel.uploads import room_image_public_url

    maint_ids = [
        r.id for r in rooms if r.physical_status == RoomPhysicalStatus.MAINTENANCE
    ]
    maint_notes = maintenance_notes_for_rooms(db, maint_ids)

    cards: list[RoomDashboardCard] = []
    counts = {
        "available": 0,
        "occupied": 0,
        "reserved": 0,
        "dirty": 0,
        "maintenance": 0,
        "other": 0,
        "inactive": 0,
    }

    for room in rooms:
        avail, label, key, booking, upcoming = room_rentability_on_date(db, room, day)
        status_detail = None
        upcoming_booking_id = None
        upcoming_check_in = None
        upcoming_guest_name = None
        if key == "maintenance":
            status_detail = maint_notes.get(room.id)
            if status_detail:
                label = f"غير متاحة — صيانة ({status_detail})"
        elif avail and upcoming is not None:
            upcoming_booking_id = upcoming.id
            upcoming_check_in = upcoming.check_in
            upcoming_guest_name = (upcoming.guest_name or "").strip() or None
            status_detail = (
                f"حجز قادم {upcoming.check_in.strftime('%Y-%m-%d')}"
                + (f" — {upcoming_guest_name}" if upcoming_guest_name else "")
            )
        rt = room.room_type
        guest = None
        booking_id = None
        check_out = None
        if booking:
            guest = (booking.guest_name or "").strip() or None
            booking_id = booking.id
            check_out = booking.check_out
        elif room.physical_status == RoomPhysicalStatus.OCCUPIED and room.guest_name:
            guest = room.guest_name.strip()

        if not room.is_active:
            counts["inactive"] += 1
        elif key == "available":
            counts["available"] += 1
        elif key in ("occupied",):
            counts["occupied"] += 1
        elif key == "reserved":
            counts["reserved"] += 1
        elif key == "dirty":
            counts["dirty"] += 1
        elif key == "maintenance":
            counts["maintenance"] += 1
        else:
            counts["other"] += 1

        balance_due = Decimal("0")
        debt_stay = Decimal("0")
        debt_laundry = Decimal("0")
        debt_restaurant = Decimal("0")
        checkout_today = False

        if booking is not None and booking.booking_status == BookingStatus.CHECKED_IN:
            from modules.hotel.folio import folio_debt_breakdown

            br = folio_debt_breakdown(db, booking.id)
            debt_stay = br.stay
            debt_laundry = br.laundry
            debt_restaurant = br.restaurant + br.other_services
            balance_due = br.balance
            checkout_today = booking.check_out == day
        elif key == "occupied" and room.is_active:
            from modules.hotel.service import room_open_total

            pos_due = room_open_total(db, room.id)
            if pos_due > Decimal("0"):
                debt_restaurant = pos_due
                balance_due = pos_due

        cards.append(
            RoomDashboardCard(
                room_id=room.id,
                display_name=room_display_name(room),
                number=room.number,
                floor=room.floor,
                property_name=property_name,
                nightly_price=_nightly_price_for_room(db, room, day),
                capacity_adults=int(rt.capacity_adults if rt else 2),
                capacity_children=int(rt.capacity_children if rt else 0),
                is_available=avail,
                status_label=label,
                status_key=key,
                status_detail=status_detail,
                guest_name=guest,
                booking_id=booking_id,
                check_out=check_out,
                image_url=room_image_public_url(getattr(room, "image_filename", None)),
                is_active=room.is_active,
                room_type_name=rt.name_ar if rt else None,
                balance_due=balance_due,
                debt_stay=debt_stay,
                debt_laundry=debt_laundry,
                debt_restaurant=debt_restaurant,
                checkout_today=checkout_today,
                upcoming_booking_id=upcoming_booking_id,
                upcoming_check_in=upcoming_check_in,
                upcoming_guest_name=upcoming_guest_name,
            )
        )

    return cards, counts


def build_front_desk_summary(
    db: Session,
    *,
    cards: list[RoomDashboardCard],
    counts: dict[str, int],
    day: date | None = None,
    property_id: int = 1,
) -> FrontDeskSummary:
    day = day or date.today()
    zero = Decimal("0")

    active_bookings = list(
        db.scalars(
            select(HotelBooking)
            .where(
                HotelBooking.property_id == property_id,
                HotelBooking.booking_status.in_(
                    [
                        BookingStatus.PENDING,
                        BookingStatus.CONFIRMED,
                        BookingStatus.CHECKED_IN,
                    ]
                ),
                HotelBooking.check_out >= day,
            )
            .order_by(HotelBooking.check_in.asc(), HotelBooking.id.asc())
        ).all()
    )

    checkout_list = [
        b
        for b in active_bookings
        if b.check_out == day and b.booking_status == BookingStatus.CHECKED_IN
    ]
    arrivals_list = [
        b
        for b in active_bookings
        if b.check_in == day
        and b.booking_status in (BookingStatus.CONFIRMED, BookingStatus.PENDING)
    ]
    online_list = [
        b
        for b in active_bookings
        if b.booking_status == BookingStatus.PENDING
        or (
            b.source == BookingSource.PORTAL
            and b.booking_status == BookingStatus.CONFIRMED
            and b.room_id is None
        )
    ]

    debt_stay_total = sum((c.debt_stay for c in cards), zero)
    debt_laundry_total = sum((c.debt_laundry for c in cards), zero)
    debt_restaurant_total = sum((c.debt_restaurant for c in cards), zero)
    debt_grand_total = sum((c.balance_due for c in cards), zero)

    debt_stay_rooms = sum(1 for c in cards if c.debt_stay > zero)
    debt_laundry_rooms = sum(1 for c in cards if c.debt_laundry > zero)
    debt_restaurant_rooms = sum(1 for c in cards if c.debt_restaurant > zero)

    alerts: list[DebtAlertRow] = []
    for c in cards:
        label = c.display_name
        if c.debt_stay > zero:
            alerts.append(
                DebtAlertRow(
                    room_id=c.room_id,
                    room_label=label,
                    booking_id=c.booking_id,
                    guest_name=c.guest_name,
                    amount=c.debt_stay,
                    category="stay",
                    category_label="إقامة",
                )
            )
        if c.debt_laundry > zero:
            alerts.append(
                DebtAlertRow(
                    room_id=c.room_id,
                    room_label=label,
                    booking_id=c.booking_id,
                    guest_name=c.guest_name,
                    amount=c.debt_laundry,
                    category="laundry",
                    category_label="مغسلة",
                )
            )
        if c.debt_restaurant > zero:
            alerts.append(
                DebtAlertRow(
                    room_id=c.room_id,
                    room_label=label,
                    booking_id=c.booking_id,
                    guest_name=c.guest_name,
                    amount=c.debt_restaurant,
                    category="restaurant",
                    category_label="مطعم",
                )
            )
    alerts.sort(key=lambda r: (r.amount, r.room_label), reverse=True)

    return FrontDeskSummary(
        available=int(counts.get("available", 0)),
        occupied=int(counts.get("occupied", 0)),
        reserved=int(counts.get("reserved", 0)),
        dirty=int(counts.get("dirty", 0)),
        maintenance=int(counts.get("maintenance", 0)),
        checkout_due=len(checkout_list),
        arrivals_today=len(arrivals_list),
        online_requests=len(online_list),
        debt_stay_total=debt_stay_total.quantize(Decimal("0.001")),
        debt_laundry_total=debt_laundry_total.quantize(Decimal("0.001")),
        debt_restaurant_total=debt_restaurant_total.quantize(Decimal("0.001")),
        debt_grand_total=debt_grand_total.quantize(Decimal("0.001")),
        debt_stay_rooms=debt_stay_rooms,
        debt_laundry_rooms=debt_laundry_rooms,
        debt_restaurant_rooms=debt_restaurant_rooms,
        checkout_list=checkout_list,
        arrivals_list=arrivals_list,
        online_list=online_list,
        debt_alerts=alerts,
    )
