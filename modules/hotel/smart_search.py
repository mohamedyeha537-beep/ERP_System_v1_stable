"""بحث ذكي لتوفر أنواع الشقق — للمتجر والاستقبال الهاتفي."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from modules.hotel.availability import (
    UNAVAILABLE_ROOM_STATUSES,
    room_stay_availability,
)
from modules.hotel.booking_models import HotelRoomType, RoomPhysicalStatus
from modules.hotel.models import HotelRoom
from modules.hotel.pricing import accommodation_total, nightly_rate_for_stay
from modules.settings.service import get_setting

# عمر الطفل الذي يحتاج سريراً خاصاً (أقل من ذلك غالباً ينام مع البالغين)
DEFAULT_CHILD_BED_AGE = 6


@dataclass(frozen=True)
class SmartSearchUnit:
    room_id: int
    number: str
    display_name: str
    floor: str | None
    beds_note: str
    allows_extra_bed: bool
    clean_label: str
    clean_key: str
    ready_hint: str
    full_stay_ok: bool
    available_from: date | None
    occupied_until: date | None
    stay_status_label: str
    stay_status_key: str


@dataclass
class SmartSearchTypeResult:
    room_type_id: int
    name_ar: str
    available_count: int
    booked_count: int
    base_capacity: int
    max_capacity: int
    base_capacity_label: str
    max_capacity_label: str
    nightly_rate: Decimal
    total_stay: Decimal
    nights: int
    allows_extra_bed: bool
    beds_note: str
    units: list[SmartSearchUnit] = field(default_factory=list)
    suitability: int = 0


def child_bed_age_threshold(db: Session) -> int:
    raw = (get_setting(db, "hotel_child_bed_age") or "").strip()
    try:
        n = int(raw)
        return n if n >= 0 else DEFAULT_CHILD_BED_AGE
    except ValueError:
        return DEFAULT_CHILD_BED_AGE


def parse_child_ages(raw: str | list[str] | None, *, children: int) -> list[int | None]:
    """يحلل أعمار الأطفال من نص مفصول بفواصل أو قائمة."""
    ages: list[int | None] = []
    if isinstance(raw, list):
        parts = raw
    elif raw:
        parts = [p.strip() for p in str(raw).replace(";", ",").split(",") if p.strip()]
    else:
        parts = []
    for p in parts:
        try:
            ages.append(max(0, min(17, int(float(p)))))
        except (TypeError, ValueError):
            ages.append(None)
    while len(ages) < children:
        ages.append(None)
    return ages[: max(0, children)]


def beds_needed(
    *,
    adults: int,
    children: int,
    child_ages: list[int | None],
    bed_age: int,
) -> tuple[int, int, int]:
    """
    يُرجع: (أسرة مطلوبة, أطفال يحتاجون سريراً, أطفال بدون سرير خاص).
    العمر غير المحدد يُعامل كاحتياج سرير (الأكثر أماناً).
    """
    adults = max(0, int(adults))
    children = max(0, int(children))
    ages = list(child_ages) + [None] * max(0, children - len(child_ages))
    ages = ages[:children]
    bed_kids = 0
    lap_kids = 0
    for age in ages:
        if age is None or age >= bed_age:
            bed_kids += 1
        else:
            lap_kids += 1
    return adults + bed_kids, bed_kids, lap_kids


def type_max_capacity(rt: HotelRoomType) -> int:
    explicit = getattr(rt, "max_occupancy", None)
    if explicit is not None and int(explicit) > 0:
        return int(explicit)
    return int(rt.capacity_adults or 0) + int(rt.capacity_children or 0)


def type_fits_party(
    rt: HotelRoomType,
    *,
    adults: int,
    children: int,
    child_ages: list[int | None],
    bed_age: int,
) -> bool:
    max_cap = type_max_capacity(rt)
    if max_cap <= 0:
        return False
    beds, _bed_kids, lap_kids = beds_needed(
        adults=adults, children=children, child_ages=child_ages, bed_age=bed_age
    )
    if beds > max_cap:
        # سرير إضافي قد يغطي شخصاً واحداً إضافياً
        if getattr(rt, "allows_extra_bed", False) and beds <= max_cap + 1:
            return True
        return False
    # الأطفال الصغار يحتاجون مساحة أطفال أو ضمن الحد الأقصى
    if lap_kids and beds + lap_kids > max_cap + (1 if getattr(rt, "allows_extra_bed", False) else 0):
        return False
    return True


def _people_label(n: int) -> str:
    if n <= 0:
        return "—"
    if n == 1:
        return "شخص واحد"
    if n == 2:
        return "شخصان"
    if n <= 10:
        return f"{n} أشخاص"
    return f"{n} شخصاً"


def _clean_status(room: HotelRoom) -> tuple[str, str, str]:
    """تسمية النظافة، المفتاح، وتلميح الجاهزية."""
    ps = room.physical_status
    if ps == RoomPhysicalStatus.AVAILABLE or ps == RoomPhysicalStatus.RESERVED:
        return "جاهزة", "ready", "جاهزة عند الوصول"
    if ps == RoomPhysicalStatus.DIRTY:
        return "للتنظيف", "dirty", "بعد التنظيف — عادةً خلال ساعات من المغادرة السابقة"
    if ps == RoomPhysicalStatus.CLEANING:
        return "قيد التنظيف", "cleaning", "جاري التجهيز — تُتاح فور تأكيد انتهاء التنظيف"
    if ps == RoomPhysicalStatus.OCCUPIED:
        return "مشغولة حالياً", "occupied", "تُجهَّز بعد مغادرة النزيل الحالي"
    if ps == RoomPhysicalStatus.MAINTENANCE:
        return "صيانة", "maintenance", "بعد انتهاء الصيانة"
    if ps == RoomPhysicalStatus.OUT_OF_SERVICE:
        return "خارج الخدمة", "oos", "غير متاحة"
    if ps == RoomPhysicalStatus.BLOCKED:
        return "محجوبة", "blocked", "غير متاحة"
    return str(ps.value if hasattr(ps, "value") else ps), "other", "—"


def default_stay_times(db: Session) -> tuple[str, str]:
    cin = (get_setting(db, "hotel_default_check_in_time") or "14:00").strip() or "14:00"
    cout = (get_setting(db, "hotel_default_check_out_time") or "12:00").strip() or "12:00"
    return cin[:5], cout[:5]


def parse_time_str(raw: str | None, fallback: str) -> str:
    s = (raw or "").strip()
    if not s:
        return fallback
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(s[:8], fmt).strftime("%H:%M")
        except ValueError:
            continue
    return fallback


def smart_search_types(
    db: Session,
    *,
    check_in: date,
    check_out: date,
    adults: int = 2,
    children: int = 0,
    child_ages: list[int | None] | None = None,
    units_needed: int = 1,
    property_id: int = 1,
    online_only: bool = True,
) -> list[SmartSearchTypeResult]:
    if check_out <= check_in:
        return []
    adults = max(1, int(adults))
    children = max(0, int(children))
    units_needed = max(1, int(units_needed))
    if isinstance(child_ages, str):
        ages = parse_child_ages(child_ages, children=children)
    else:
        ages = parse_child_ages(
            ["" if a is None else str(a) for a in (child_ages or [])],
            children=children,
        )
    bed_age = child_bed_age_threshold(db)
    nights = max(1, (check_out - check_in).days)

    types = list(
        db.scalars(
            select(HotelRoomType)
            .where(
                HotelRoomType.property_id == property_id,
                HotelRoomType.is_active.is_(True),
            )
            .order_by(HotelRoomType.sort_order.asc(), HotelRoomType.id.asc())
        ).all()
    )

    results: list[SmartSearchTypeResult] = []
    for rt in types:
        if not type_fits_party(
            rt, adults=adults, children=children, child_ages=ages, bed_age=bed_age
        ):
            continue

        stmt = select(HotelRoom).where(
            HotelRoom.room_type_id == rt.id,
            HotelRoom.is_active.is_(True),
            HotelRoom.physical_status.notin_(UNAVAILABLE_ROOM_STATUSES),
        )
        if online_only:
            stmt = stmt.where(HotelRoom.show_online.is_(True))
        stmt = stmt.options(selectinload(HotelRoom.room_type)).order_by(
            HotelRoom.number.asc(), HotelRoom.id.asc()
        )
        rooms = list(db.scalars(stmt).all())

        units: list[SmartSearchUnit] = []
        for room in rooms:
            stay = room_stay_availability(
                db, room_id=room.id, check_in=check_in, check_out=check_out
            )
            clean_label, clean_key, ready = _clean_status(room)
            beds_note = (getattr(rt, "beds_description", None) or "").strip() or (
                (rt.description or "").strip()[:80] or "—"
            )
            allows = bool(getattr(rt, "allows_extra_bed", False))
            if stay.full_stay_ok:
                ready_hint = ready
            elif stay.available_from is not None:
                ready_hint = f"متاحة من {stay.available_from.isoformat()}"
                if stay.occupied_until:
                    ready_hint = (
                        f"مغادرة النزيل {stay.occupied_until.isoformat()} · {ready_hint}"
                    )
            else:
                ready_hint = stay.status_label
            units.append(
                SmartSearchUnit(
                    room_id=room.id,
                    number=room.number or str(room.id),
                    display_name=(room.name_ar or "").strip() or f"شقة {room.number}",
                    floor=(room.floor or "").strip() or None,
                    beds_note=beds_note,
                    allows_extra_bed=allows,
                    clean_label=clean_label,
                    clean_key=clean_key,
                    ready_hint=ready_hint,
                    full_stay_ok=stay.full_stay_ok,
                    available_from=stay.available_from,
                    occupied_until=stay.occupied_until,
                    stay_status_label=stay.status_label,
                    stay_status_key=stay.status_key,
                )
            )

        if not units:
            continue

        free_units = [u for u in units if u.full_stay_ok]
        booked_units = [u for u in units if not u.full_stay_ok]
        # يكفي وجود شقق كافية متاحة للفترة، أو عرض النوع مع المحجوزة للتخطيط
        if len(free_units) < units_needed and len(units) < units_needed:
            continue

        # المتاحة أولاً ثم الأقرب إتاحة
        units.sort(
            key=lambda u: (
                0 if u.full_stay_ok else 1,
                u.available_from or date.max,
                u.number,
            )
        )

        nightly = nightly_rate_for_stay(
            db, room_type_id=rt.id, check_in=check_in, check_out=check_out
        )
        total = accommodation_total(
            db, room_type_id=rt.id, check_in=check_in, check_out=check_out
        )
        base_cap = int(rt.capacity_adults or 0)
        max_cap = type_max_capacity(rt)
        beds, _, _ = beds_needed(
            adults=adults, children=children, child_ages=ages, bed_age=bed_age
        )
        # الأنسب: أنواع فيها شقق حرة للفترة أولاً، ثم أقل فائض سعة، ثم السعر
        surplus = max(0, max_cap - beds)
        free_n = len(free_units)
        suitability = (0 if free_n >= units_needed else 500_000) + surplus * 1000 + int(nightly)

        results.append(
            SmartSearchTypeResult(
                room_type_id=rt.id,
                name_ar=rt.name_ar,
                available_count=free_n,
                booked_count=len(booked_units),
                base_capacity=base_cap,
                max_capacity=max_cap,
                base_capacity_label=_people_label(base_cap),
                max_capacity_label=_people_label(max_cap),
                nightly_rate=nightly,
                total_stay=total,
                nights=nights,
                allows_extra_bed=bool(getattr(rt, "allows_extra_bed", False)),
                beds_note=(getattr(rt, "beds_description", None) or "").strip() or "—",
                units=units,
                suitability=suitability,
            )
        )

    results.sort(key=lambda r: (r.suitability, r.nightly_rate, -r.available_count, r.name_ar))
    return results
