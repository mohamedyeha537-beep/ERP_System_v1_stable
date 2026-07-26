"""متجر حجز الشقق الأونلاين — /suites"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.hotel.availability import (
    UNAVAILABLE_ROOM_STATUSES,
    blocking_booking_on_date,
    first_room_conflict,
    is_room_rentable,
    room_stay_availability,
)
from modules.hotel.pricing import nightly_rate_for_stay
from modules.hotel.booking_models import BookingSource, GuestType, RecordKind, RoomPhysicalStatus
from modules.hotel.booking_service import StayingGuestInput, create_booking
from modules.hotel.models import HotelRoom
from modules.hotel.store_models import RoomMediaKind
from modules.hotel.uploads import room_image_public_url, room_media_public_url
from modules.platform.module_registry import HOTEL_BOOKING, HOTEL_STORE, ensure_hotel_store_module, is_module_enabled
from modules.settings.service import get_bool, get_setting


class StoreError(Exception):
    pass


def store_enabled(db: Session) -> bool:
    if not is_module_enabled(db, HOTEL_BOOKING):
        return False
    ensure_hotel_store_module(db)
    if not is_module_enabled(db, HOTEL_STORE):
        return False
    return get_bool(db, "hotel_store_enabled", True)


@dataclass
class StoreRoomCard:
    room: HotelRoom
    nightly_rate: Decimal
    available: bool
    cover_url: str | None
    media_count: int
    available_from: date | None = None
    occupied_until: date | None = None
    status_label: str = ""
    status_key: str = "available"


def _room_nightly(db: Session, room: HotelRoom) -> Decimal:
    if room.nightly_price is not None and Decimal(str(room.nightly_price or 0)) > 0:
        return Decimal(str(room.nightly_price)).quantize(Decimal("0.001"))
    if room.room_type_id and room.room_type:
        return Decimal(str(room.room_type.base_price or 0)).quantize(Decimal("0.001"))
    return Decimal("0")


def _room_cover(room: HotelRoom) -> str | None:
    active = [m for m in (room.media_items or []) if m.is_active]
    images = [m for m in active if m.kind == RoomMediaKind.IMAGE]
    if images:
        return room_media_public_url(images[0].filename)
    return room_image_public_url(room.image_filename)


def _room_available(
    db: Session,
    room: HotelRoom,
    *,
    check_in: date | None,
    check_out: date | None,
) -> bool:
    if not room.is_active or not room.show_online:
        return False
    if not is_room_rentable(room):
        return False
    if not room.room_type_id:
        return False
    if check_in is None or check_out is None:
        return True
    if check_out <= check_in:
        return False
    return first_room_conflict(db, room_id=room.id, check_in=check_in, check_out=check_out) is None


def _card_availability(
    db: Session,
    room: HotelRoom,
    *,
    check_in: date | None,
    check_out: date | None,
) -> tuple[bool, date | None, date | None, str, str]:
    """متاحة؟, من تاريخ, حتى مغادرة, نص الحالة, مفتاح الحالة."""
    if room.physical_status in UNAVAILABLE_ROOM_STATUSES:
        return False, None, None, "غير متاحة حالياً (صيانة/خارج الخدمة)", "blocked"

    if check_in and check_out and check_out > check_in:
        stay = room_stay_availability(
            db, room_id=room.id, check_in=check_in, check_out=check_out
        )
        return (
            stay.full_stay_ok,
            stay.available_from,
            stay.occupied_until,
            stay.status_label,
            stay.status_key,
        )

    # بدون تواريخ بحث: إن كان هناك نزيل اليوم اعرض موعد المغادرة
    today = date.today()
    current = blocking_booking_on_date(db, room.id, today)
    if current is not None:
        return (
            False,
            current.check_out,
            current.check_out,
            f"مشغولة الآن — متاحة من {current.check_out.isoformat()}",
            "booked",
        )
    if room.physical_status == RoomPhysicalStatus.DIRTY:
        return True, today, None, "للتنظيف — تُتاح بعد التجهيز", "dirty"
    if room.physical_status == RoomPhysicalStatus.CLEANING:
        return True, today, None, "قيد التنظيف — تُتاح بعد التجهيز", "cleaning"
    return True, today, None, "متاحة", "available"


def list_store_rooms(
    db: Session,
    *,
    check_in: date | None = None,
    check_out: date | None = None,
) -> list[StoreRoomCard]:
    rooms = list(
        db.scalars(
            select(HotelRoom)
            .where(HotelRoom.is_active.is_(True), HotelRoom.show_online.is_(True))
            .order_by(HotelRoom.number.asc(), HotelRoom.id.asc())
        ).all()
    )
    cards: list[StoreRoomCard] = []
    for room in rooms:
        # نستبعد المعطّلة نهائياً فقط؛ المحجوزة تبقى ظاهرة مع تاريخ الإتاحة
        if room.physical_status in UNAVAILABLE_ROOM_STATUSES:
            continue
        if not room.room_type_id:
            continue
        avail, from_d, until_d, label, key = _card_availability(
            db, room, check_in=check_in, check_out=check_out
        )
        active_media = [m for m in (room.media_items or []) if m.is_active]
        cards.append(
            StoreRoomCard(
                room=room,
                nightly_rate=_room_nightly(db, room),
                available=avail,
                cover_url=_room_cover(room),
                media_count=len(active_media),
                available_from=from_d,
                occupied_until=until_d,
                status_label=label,
                status_key=key,
            )
        )
    # المتاحة أولاً ثم الأقرب إتاحة
    cards.sort(
        key=lambda c: (
            0 if c.available else 1,
            c.available_from or date.max,
            c.room.number or "",
        )
    )
    return cards


def get_store_room(db: Session, room_id: int) -> HotelRoom | None:
    room = db.get(HotelRoom, room_id)
    if room is None or not room.is_active or not room.show_online:
        return None
    return room


def quote_stay(
    db: Session,
    *,
    room_id: int,
    check_in: date,
    check_out: date,
) -> dict:
    room = get_store_room(db, room_id)
    if room is None:
        raise StoreError("الشقة غير متاحة للعرض أونلاين.")
    if check_out <= check_in:
        raise StoreError("تاريخ المغادرة يجب أن يكون بعد الوصول.")
    if not _room_available(db, room, check_in=check_in, check_out=check_out):
        raise StoreError("الشقة غير متاحة في التواريخ المختارة.")
    nights = max(1, (check_out - check_in).days)
    if room.nightly_price is not None and Decimal(str(room.nightly_price or 0)) > 0:
        nightly = Decimal(str(room.nightly_price)).quantize(Decimal("0.001"))
    else:
        nightly = nightly_rate_for_stay(
            db,
            room_type_id=room.room_type_id,
            check_in=check_in,
            check_out=check_out,
        )
    total = (nightly * Decimal(nights)).quantize(Decimal("0.001"))
    return {
        "room_id": room.id,
        "nights": nights,
        "nightly_rate": nightly,
        "total": total,
        "available": True,
    }


def create_online_booking_request(
    db: Session,
    *,
    room_id: int,
    guest_name: str,
    guest_phone: str,
    guest_email: str | None,
    check_in: date,
    check_out: date,
    adults: int = 1,
    children: int = 0,
    guest_type: GuestType = GuestType.INDIVIDUAL,
    company_name: str | None = None,
    company_contact_name: str | None = None,
    company_contact_phone: str | None = None,
    company_contact_email: str | None = None,
    internal_notes: str | None = None,
    staying_guests: list[StayingGuestInput] | None = None,
):
    if not store_enabled(db):
        raise StoreError("متجر الشقق غير متاح حالياً.")
    room = get_store_room(db, room_id)
    if room is None or not room.room_type_id:
        raise StoreError("الشقة غير جاهزة للحجز أونلاين.")
    quote = quote_stay(db, room_id=room_id, check_in=check_in, check_out=check_out)
    nightly = quote["nightly_rate"]
    booking = create_booking(
        db,
        guest_name=guest_name,
        guest_phone=guest_phone,
        guest_email=guest_email,
        guest_type=guest_type,
        company_name=company_name,
        company_contact_name=company_contact_name,
        company_contact_phone=company_contact_phone,
        company_contact_email=company_contact_email,
        check_in=check_in,
        check_out=check_out,
        room_type_id=room.room_type_id,
        room_id=room.id,
        adults=adults,
        children=children,
        nightly_rate=nightly,
        source=BookingSource.ONLINE_STORE,
        internal_notes=internal_notes,
        auto_confirm=False,
        record_kind=RecordKind.BOOKING,
        staying_guests=staying_guests or [],
    )
    try:
        from modules.notifications.hotel_hooks import emit_hotel_online_booking_request

        emit_hotel_online_booking_request(db, booking)
    except Exception:  # noqa: BLE001
        pass
    return booking


def room_media_gallery(room: HotelRoom) -> list[dict]:
    items = []
    for m in sorted(
        [x for x in (room.media_items or []) if x.is_active],
        key=lambda x: (x.sort_order, x.id),
    ):
        url = room_media_public_url(m.filename)
        if not url:
            continue
        items.append(
            {
                "id": m.id,
                "kind": m.kind.value,
                "url": url,
                "caption": m.caption or "",
            }
        )
    cover = room_image_public_url(room.image_filename)
    if cover and not any(i["url"] == cover for i in items):
        items.insert(0, {"id": 0, "kind": "IMAGE", "url": cover, "caption": ""})
    return items


def staff_notify_phone(db: Session) -> str:
    return (get_setting(db, "hotel_online_staff_phone", "") or "").strip()
