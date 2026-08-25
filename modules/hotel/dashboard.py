"""لوحة توفر الشقق — حالة اليوم لكل غرفة."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.hotel.availability import (
    UNAVAILABLE_ROOM_STATUSES,
    open_booking_on_date,
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


def booking_occupants_count(
    booking: HotelBooking | None,
    *,
    db: Session | None = None,
) -> int:
    """عدد النزلاء في الحجز (للإظهار على كرت الشقة).

    الأولوية: صفوف نزلاء الحجز (`hotel_booking_guests`)، ثم بالغون + أطفال.
    """
    if booking is None:
        return 0
    # استعلام مباشر عند توفر db — أوثق من العلاقة إن لم تُحمَّل
    if db is not None and getattr(booking, "id", None) is not None:
        from modules.hotel.booking_models import HotelBookingGuest
        from sqlalchemy import func

        named = int(
            db.scalar(
                select(func.count())
                .select_from(HotelBookingGuest)
                .where(
                    HotelBookingGuest.booking_id == int(booking.id),
                    HotelBookingGuest.full_name.isnot(None),
                    HotelBookingGuest.full_name != "",
                )
            )
            or 0
        )
        if named > 0:
            return named
    guests = list(getattr(booking, "guests", None) or [])
    named = sum(
        1 for g in guests if (getattr(g, "full_name", None) or "").strip()
    )
    if named > 0:
        return named
    adults = max(0, int(getattr(booking, "adults", 0) or 0))
    children = max(0, int(getattr(booking, "children", 0) or 0))
    total = adults + children
    if total > 0:
        return total
    if (getattr(booking, "guest_name", None) or "").strip():
        return 1
    return 0


def first_resident_contact(
    booking: HotelBooking | None,
    *,
    db: Session | None = None,
) -> tuple[str, str]:
    """(اسم النزيل الأول، الهاتف) من جداول الحجز — ثم guest_name المخزّن إن لم يُسجَّل نزيل."""
    if booking is None:
        return "", ""
    name = ""
    phone = ""
    # استعلام مباشر لأوّل نزيل (is_primary ثم id)
    if db is not None and getattr(booking, "id", None) is not None:
        from modules.hotel.booking_models import HotelBookingGuest

        row = db.scalar(
            select(HotelBookingGuest)
            .where(HotelBookingGuest.booking_id == int(booking.id))
            .order_by(
                HotelBookingGuest.is_primary.desc(),
                HotelBookingGuest.id.asc(),
            )
            .limit(1)
        )
        if row is not None:
            name = (getattr(row, "full_name", None) or "").strip()
            phone = (getattr(row, "phone", None) or "").strip()
    if not name or not phone:
        try:
            from modules.hotel.booking_service import primary_staying_guest_contact

            g_name, g_phone = primary_staying_guest_contact(booking)
            # primary_staying_guest_contact قد يُرجع guest_name (الحاجز) — لا نستخدمه إذا لم يكن من صف النزلاء
            if not name and g_name:
                # تحقّق أن الاسم موجود في صفوف النزلاء فقط
                from modules.hotel.booking_models import HotelBookingGuest

                if db is not None and getattr(booking, "id", None) is not None:
                    match = db.scalar(
                        select(HotelBookingGuest.id).where(
                            HotelBookingGuest.booking_id == int(booking.id),
                            HotelBookingGuest.full_name == g_name,
                        ).limit(1)
                    )
                    if match is not None:
                        name = g_name.strip()
                else:
                    guests = list(getattr(booking, "guests", None) or [])
                    if any((getattr(g, "full_name", None) or "").strip() == g_name.strip() for g in guests):
                        name = g_name.strip()
            if not phone:
                phone = (g_phone or "").strip()
        except Exception:  # noqa: BLE001
            pass
    # إن لم يُسجَّل نزيل في الجدول: اسم الحجز من قاعدة البيانات (ليس اسم حساب الشركة)
    if not name:
        name = (getattr(booking, "guest_name", None) or "").strip()
    if not phone:
        phone = (getattr(booking, "guest_phone", None) or "").strip()
    return name, phone


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
    check_in: date | None
    check_out: date | None
    image_url: str | None
    is_active: bool
    room_type_name: str | None
    rooms_count: int = 1
    beds_count: int = 1
    double_beds_count: int = 1
    single_beds_count: int = 0
    allows_infant: bool = False
    balance_due: Decimal = Decimal("0")
    debt_stay: Decimal = Decimal("0")
    debt_laundry: Decimal = Decimal("0")
    debt_restaurant: Decimal = Decimal("0")
    debt_watch: bool = False
    #: unpaid | partial | paid | "" — شارة حالة السداد على الكرت
    pay_status: str = ""
    pay_label: str = ""
    checkout_today: bool = False
    is_overstay: bool = False
    #: تمديد إقامة (check_out بعد المخطط/المجدول)
    is_extended: bool = False
    upcoming_booking_id: int | None = None
    upcoming_check_in: date | None = None
    upcoming_check_out: date | None = None
    upcoming_guest_name: str | None = None
    #: هاتف النزيل الأول
    guest_phone: str | None = None
    #: عدد النزلاء (من صفوف نزلاء الحجز)
    guests_count: int = 0
    adults: int = 0
    children: int = 0


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
    #: إجمالي عدد النزلاء في الشقق المشغولة/المحجوزة
    guests_total: int = 0
    guests_rooms: int = 0


_STATUS_LABELS: dict[RoomPhysicalStatus, str] = {
    RoomPhysicalStatus.MAINTENANCE: "غير متاحة — صيانة",
    RoomPhysicalStatus.OUT_OF_SERVICE: "خارج الخدمة",
    RoomPhysicalStatus.BLOCKED: "محجوبة",
    RoomPhysicalStatus.DIRTY: "للتنظيف",
    RoomPhysicalStatus.CLEANING: "قيد التنظيف",
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
    """اسم عرض الشقة — العقار شقق فقط (لا نُظهر «غرفة»)."""
    import re

    num = (getattr(room, "number", None) or "").strip()
    name = (getattr(room, "name_ar", None) or "").strip()
    if name:
        # «غرفة 104» / «شقة 104» = مجرد تكرار الرقم → نُهمل الاسم
        m = re.match(r"^(?:غرفة|شقة)\s*(.+)$", name)
        if m and m.group(1).strip() == num:
            name = ""
        elif name == num:
            name = ""
        elif name.startswith("غرفة"):
            # وحدة بأسهم اسم مخصص بدأ بـ «غرفة» → أعرضه كشقة
            rest = name[len("غرفة") :].strip()
            name = f"شقة {rest}" if rest else ""
    if name:
        return name
    if num:
        if num.startswith("شقة") or num.upper().startswith("VIP"):
            return num
        return f"شقة {num}"
    return "شقة"


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
        upcoming_check_out = None
        upcoming_guest_name = None
        if key == "maintenance":
            status_detail = maint_notes.get(room.id)
            if status_detail:
                label = f"غير متاحة — صيانة ({status_detail})"
        elif avail and upcoming is not None:
            upcoming_booking_id = upcoming.id
            upcoming_check_in = upcoming.check_in
            upcoming_check_out = upcoming.check_out
            upcoming_guest_name = (upcoming.guest_name or "").strip() or None
            status_detail = (
                f"حجز قادم {upcoming.check_in.strftime('%Y-%m-%d')}"
                + (
                    f" → {upcoming.check_out.strftime('%Y-%m-%d')}"
                    if upcoming.check_out
                    else ""
                )
                + (f" — {upcoming_guest_name}" if upcoming_guest_name else "")
            )

        # اربط الكرت بالحجز الفعلي: معلّق اليوم، أو مسكن حتى يوم المغادرة
        if booking is None and key not in (
            "maintenance",
            "blocked",
            "dirty",
            "cleaning",
            "inactive",
        ):
            open_b = open_booking_on_date(db, room.id, day)
            if open_b is None and upcoming is not None and upcoming.check_in == day:
                open_b = upcoming
            if open_b is not None:
                booking = open_b
                if open_b.booking_status == BookingStatus.CHECKED_IN:
                    avail, label, key = False, "مشغولة", "occupied"
                else:
                    avail, label, key = False, "محجوزة", "reserved"
                if upcoming is not None and upcoming.id == open_b.id:
                    upcoming = None
                    upcoming_booking_id = None
                    upcoming_check_in = None
                    upcoming_check_out = None
                    upcoming_guest_name = None
                    status_detail = None
        rt = room.room_type
        guest = None
        guest_phone = None
        booking_id = None
        check_in = None
        check_out = None
        guests_count = 0
        adults_n = 0
        children_n = 0
        if booking:
            g_name, g_phone = first_resident_contact(booking, db=db)
            guest = g_name or None
            guest_phone = g_phone or None
            booking_id = booking.id
            check_in = booking.check_in
            check_out = booking.check_out
            adults_n = max(0, int(getattr(booking, "adults", 0) or 0))
            children_n = max(0, int(getattr(booking, "children", 0) or 0))
            guests_count = booking_occupants_count(booking, db=db)
        elif room.physical_status == RoomPhysicalStatus.OCCUPIED and room.guest_name:
            guest = room.guest_name.strip()
            guests_count = 1

        if not room.is_active:
            counts["inactive"] += 1
        elif key == "available":
            counts["available"] += 1
        elif key in ("occupied",):
            counts["occupied"] += 1
        elif key == "reserved":
            counts["reserved"] += 1
        elif key in ("dirty", "cleaning"):
            counts["dirty"] += 1
        elif key == "maintenance":
            counts["maintenance"] += 1
        else:
            counts["other"] += 1

        balance_due = Decimal("0")
        debt_stay = Decimal("0")
        debt_laundry = Decimal("0")
        debt_restaurant = Decimal("0")
        debt_watch = False
        pay_status = ""
        pay_label = ""
        checkout_today = False
        is_overstay = False
        is_extended = False

        if booking is not None and booking.booking_status == BookingStatus.CHECKED_IN:
            from modules.hotel.folio import folio_debt_breakdown
            from modules.hotel.late_checkout import is_booking_overstay

            br = folio_debt_breakdown(db, booking.id)
            # صافي مستحق النزيل فقط — لا تُظهر مطعم أحمر إن الرصيد/الدفع يغطي الطلب
            balance_due = max(Decimal("0"), Decimal(str(br.balance or 0))).quantize(
                Decimal("0.001")
            )
            if balance_due > Decimal("0.001"):
                debt_stay = br.stay
                debt_laundry = br.laundry
                debt_restaurant = (br.restaurant + br.other_services).quantize(
                    Decimal("0.001")
                )
                # إن بقي متبقٍ صافٍ دون تقسيم كافٍ، انسبه للمطعم الظاهر في الفوليو
                cat_sum = (debt_stay + debt_laundry + debt_restaurant).quantize(
                    Decimal("0.001")
                )
                if cat_sum < balance_due:
                    debt_restaurant = (
                        debt_restaurant + (balance_due - cat_sum)
                    ).quantize(Decimal("0.001"))
            else:
                debt_stay = Decimal("0")
                debt_laundry = Decimal("0")
                debt_restaurant = Decimal("0")
            checkout_today = booking.check_out == day
            planned_out = getattr(booking, "planned_check_out", None) or getattr(
                booking, "scheduled_check_out", None
            )
            if (
                planned_out
                and booking.check_out
                and booking.check_out > planned_out
            ):
                is_extended = True
            try:
                is_overstay = is_booking_overstay(db, booking)
            except Exception:  # noqa: BLE001
                is_overstay = False
            # أولوية الحالات المرئية (تصميم PMS الاستقبال):
            # Overstay → بانتظار السداد → تمديد → مشغولة
            if is_overstay:
                label = "منتهي وقت المغادرة"
                key = "overstay"
            elif balance_due > Decimal("0.001"):
                label = "بانتظار السداد"
                key = "await_pay"
            elif is_extended:
                label = "تمديد إقامة"
                key = "extended"
            debt_watch = bool(getattr(booking, "claim_wa_until_paid", False))
            paid_amt = Decimal(str(booking.paid_amount or 0))
            total_amt = (paid_amt + balance_due).quantize(Decimal("0.001"))
            if balance_due > Decimal("0.001"):
                if paid_amt > Decimal("0.001"):
                    pay_status, pay_label = "partial", "مدفوعة جزئياً"
                else:
                    pay_status, pay_label = "unpaid", "غير مدفوعة"
            elif total_amt > Decimal("0.001") or paid_amt > Decimal("0.001"):
                pay_status, pay_label = "paid", "مدفوعة"
            else:
                ps = getattr(booking.payment_status, "value", str(booking.payment_status or ""))
                if ps == "FULLY_PAID":
                    pay_status, pay_label = "paid", "مدفوعة"
                elif ps == "PARTIALLY_PAID":
                    pay_status, pay_label = "partial", "مدفوعة جزئياً"
                elif ps == "UNPAID":
                    pay_status, pay_label = "unpaid", "غير مدفوعة"
        elif key == "occupied" and room.is_active:
            # مشغولة بلا حجز مفتوح: لا تُحسب فواتير المطعم ديناً على الشقة هنا
            # (تظهر في تسوية الحسابات حتى تُربط بحجز)
            pass

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
                check_in=check_in,
                check_out=check_out,
                image_url=room_image_public_url(getattr(room, "image_filename", None)),
                is_active=room.is_active,
                room_type_name=rt.name_ar if rt else None,
                rooms_count=max(0, int(getattr(room, "rooms_count", None) or 1)),
                double_beds_count=max(
                    0, int(getattr(room, "double_beds_count", None) or 0)
                ),
                single_beds_count=max(
                    0, int(getattr(room, "single_beds_count", None) or 0)
                ),
                beds_count=max(
                    0,
                    int(getattr(room, "beds_count", None) or 0)
                    or (
                        int(getattr(room, "double_beds_count", None) or 0)
                        + int(getattr(room, "single_beds_count", None) or 0)
                    )
                    or 1,
                ),
                allows_infant=bool(getattr(room, "allows_infant", False)),
                balance_due=balance_due,
                debt_stay=debt_stay,
                debt_laundry=debt_laundry,
                debt_restaurant=debt_restaurant,
                debt_watch=debt_watch,
                pay_status=pay_status,
                pay_label=pay_label,
                checkout_today=checkout_today,
                is_overstay=is_overstay,
                is_extended=is_extended,
                upcoming_booking_id=upcoming_booking_id,
                upcoming_check_in=upcoming_check_in,
                upcoming_check_out=upcoming_check_out,
                upcoming_guest_name=upcoming_guest_name,
                guest_phone=guest_phone,
                guests_count=guests_count,
                adults=adults_n,
                children=children_n,
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

    breakfast_keys = {
        "occupied",
        "reserved",
        "overstay",
        "await_pay",
        "extended",
    }
    guests_total = sum(
        int(c.guests_count or 0)
        for c in cards
        if c.status_key in breakfast_keys and int(c.guests_count or 0) > 0
    )
    guests_rooms = sum(
        1
        for c in cards
        if c.status_key in breakfast_keys and int(c.guests_count or 0) > 0
    )

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
        guests_total=int(guests_total),
        guests_rooms=int(guests_rooms),
    )


# مفاتيح بطاقات الملخص → عنوان عربي للتركيز
DASHBOARD_VIEW_LABELS: dict[str, str] = {
    "available": "الشقق المتاحة",
    "occupied": "الشقق المشغولة",
    "checkout": "مغادرة اليوم",
    "arrivals": "وصول اليوم",
    "online": "طلبات الحجز الأونلاين",
    "reserved": "الشقق المحجوزة",
    "dirty": "شقق للتنظيف",
    "maintenance": "شقق الصيانة",
    "debt_stay": "ديون الإقامة",
    "debt_laundry": "ديون المغسلة",
    "debt_restaurant": "ديون المطعم",
}


def normalize_dashboard_view(raw: str | None) -> str | None:
    key = (raw or "").strip().lower()
    if key in DASHBOARD_VIEW_LABELS:
        return key
    return None


def filter_dashboard_by_view(
    cards: list[RoomDashboardCard],
    summary: FrontDeskSummary,
    view: str | None,
) -> tuple[list[RoomDashboardCard], FrontDeskSummary, str | None]:
    """يُضيّق خريطة الشقق والقوائم حسب بطاقة الملخص المختارة."""
    view = normalize_dashboard_view(view)
    if view is None:
        return cards, summary, None

    zero = Decimal("0")
    filtered = list(cards)
    checkout_list = list(summary.checkout_list)
    arrivals_list = list(summary.arrivals_list)
    online_list = list(summary.online_list)
    debt_alerts = list(summary.debt_alerts)

    if view == "available":
        filtered = [c for c in cards if c.is_active and c.status_key == "available"]
        checkout_list, arrivals_list, online_list, debt_alerts = [], [], [], []
    elif view == "occupied":
        filtered = [
            c
            for c in cards
            if c.is_active
            and c.status_key
            in ("occupied", "overstay", "await_pay", "extended")
        ]
        checkout_list, arrivals_list, online_list = [], [], []
        debt_alerts = [a for a in debt_alerts if a.room_id in {c.room_id for c in filtered}]
    elif view == "checkout":
        room_ids = {int(b.room_id) for b in checkout_list if getattr(b, "room_id", None)}
        filtered = [
            c for c in cards if c.checkout_today or (c.room_id in room_ids)
        ]
        arrivals_list, online_list = [], []
        debt_alerts = [a for a in debt_alerts if a.room_id in {c.room_id for c in filtered}]
    elif view == "arrivals":
        room_ids = {int(b.room_id) for b in arrivals_list if getattr(b, "room_id", None)}
        filtered = [c for c in cards if c.room_id in room_ids] if room_ids else []
        checkout_list, online_list, debt_alerts = [], [], []
    elif view == "online":
        room_ids = {int(b.room_id) for b in online_list if getattr(b, "room_id", None)}
        filtered = [c for c in cards if c.room_id in room_ids] if room_ids else []
        checkout_list, arrivals_list, debt_alerts = [], [], []
    elif view == "reserved":
        filtered = [c for c in cards if c.is_active and c.status_key == "reserved"]
        checkout_list, arrivals_list, online_list, debt_alerts = [], [], [], []
    elif view == "dirty":
        filtered = [
            c for c in cards if c.is_active and c.status_key in ("dirty", "cleaning")
        ]
        checkout_list, arrivals_list, online_list, debt_alerts = [], [], [], []
    elif view == "maintenance":
        filtered = [c for c in cards if c.status_key == "maintenance"]
        checkout_list, arrivals_list, online_list, debt_alerts = [], [], [], []
    elif view == "debt_stay":
        filtered = [c for c in cards if c.debt_stay > zero]
        checkout_list, arrivals_list, online_list = [], [], []
        debt_alerts = [a for a in debt_alerts if a.category == "stay"]
    elif view == "debt_laundry":
        filtered = [c for c in cards if c.debt_laundry > zero]
        checkout_list, arrivals_list, online_list = [], [], []
        debt_alerts = [a for a in debt_alerts if a.category == "laundry"]
    elif view == "debt_restaurant":
        filtered = [c for c in cards if c.debt_restaurant > zero]
        checkout_list, arrivals_list, online_list = [], [], []
        debt_alerts = [a for a in debt_alerts if a.category == "restaurant"]

    focused = FrontDeskSummary(
        available=summary.available,
        occupied=summary.occupied,
        reserved=summary.reserved,
        dirty=summary.dirty,
        maintenance=summary.maintenance,
        checkout_due=summary.checkout_due,
        arrivals_today=summary.arrivals_today,
        online_requests=summary.online_requests,
        debt_stay_total=summary.debt_stay_total,
        debt_laundry_total=summary.debt_laundry_total,
        debt_restaurant_total=summary.debt_restaurant_total,
        debt_grand_total=summary.debt_grand_total,
        debt_stay_rooms=summary.debt_stay_rooms,
        debt_laundry_rooms=summary.debt_laundry_rooms,
        debt_restaurant_rooms=summary.debt_restaurant_rooms,
        checkout_list=checkout_list,
        arrivals_list=arrivals_list,
        online_list=online_list,
        debt_alerts=debt_alerts,
        guests_total=summary.guests_total,
        guests_rooms=summary.guests_rooms,
    )
    return filtered, focused, DASHBOARD_VIEW_LABELS.get(view)
