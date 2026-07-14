"""خدمات الحجز — CRUD ودورة الحياة."""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from modules.hotel.audit import log_audit
from modules.hotel.availability import (
    available_rooms_for_type,
    first_room_conflict,
    has_room_conflict,
    is_room_rentable,
    NOT_RENTABLE_STATUSES,
)
from modules.hotel.booking_models import (
    BookingPaymentStatus,
    BookingSource,
    BookingStatus,
    GuestType,
    HotelBooking,
    HotelBookingGuest,
    HotelBookingPayment,
    HotelBookingPaymentRefund,
    HotelBookingRoomAssignment,
    HotelBookingService,
    HotelBookingStatusLog,
    HotelCancellationPolicy,
    HotelProperty,
    HotelRoomType,
    QuotationStatus,
    RecordKind,
    RoomPhysicalStatus,
)
from modules.hotel.booking_labels import QUOTATION_NEXT_STATUSES
from modules.hotel.models import HotelRoom, RoomCharge
from modules.hotel.pricing import (
    accommodation_segment_for_room,
    accommodation_total,
    nightly_rate_for_stay,
)


class BookingError(Exception):
    pass


PREPAYMENT_PERCENT_CHOICES = (0, 50, 100)
PREPAYMENT_SETTING_KEY = "hotel_booking_prepayment_percent"


def get_booking_prepayment_percent(db: Session) -> int:
    from modules.settings.service import get_setting

    raw = (get_setting(db, PREPAYMENT_SETTING_KEY, "100") or "100").strip()
    try:
        pct = int(float(raw))
    except (TypeError, ValueError):
        pct = 100
    return pct if pct in PREPAYMENT_PERCENT_CHOICES else 100


def parse_booking_prepayment_percent(raw: str | None) -> int:
    try:
        pct = int(float((raw or "100").strip()))
    except (TypeError, ValueError):
        return 100
    return pct if pct in PREPAYMENT_PERCENT_CHOICES else 100


def prepayment_percent_choices() -> list[tuple[int, str]]:
    return [
        (0, "0% — السماح بالحجز بدون دفع مقدّم"),
        (50, "50% — عربون نصف قيمة الإقامة"),
        (100, "100% — دفع كامل الإقامة عند الحجز"),
    ]


def prepayment_percent_label(pct: int) -> str:
    labels = {v: lbl for v, lbl in prepayment_percent_choices()}
    return labels.get(pct, f"{pct}%")


def booking_amount_due_for_booking(booking: HotelBooking) -> Decimal:
    due = Decimal(str(booking.accommodation_total or 0)) - Decimal(
        str(booking.discount_amount or 0)
    )
    return due.quantize(Decimal("0.001")) if due > 0 else Decimal("0")


def booking_paid_amount(booking: HotelBooking) -> Decimal:
    return Decimal(str(booking.paid_amount or 0)).quantize(Decimal("0.001"))


def required_prepayment_amount(db: Session, *, due: Decimal) -> Decimal:
    pct = get_booking_prepayment_percent(db)
    if pct <= 0 or due <= 0:
        return Decimal("0")
    return (due * Decimal(pct) / Decimal("100")).quantize(Decimal("0.001"))


def prepayment_requirement_message(db: Session, *, due: Decimal, required: Decimal | None = None) -> str:
    pct = get_booking_prepayment_percent(db)
    req = required if required is not None else required_prepayment_amount(db, due=due)
    if pct <= 0:
        return "لا يُشترط دفع مقدّم — يمكن إنشاء الحجز بدون دفع."
    if pct >= 100:
        return f"يجب دفع كامل الإقامة ({req} د.ل) لإتمام الحجز."
    return f"يجب دفع عربون {pct}% على الأقل ({req} د.ل من أصل {due} د.ل)."


def assert_booking_prepayment_met(db: Session, booking: HotelBooking) -> None:
    due = booking_amount_due_for_booking(booking)
    required = required_prepayment_amount(db, due=due)
    if required <= 0:
        return
    paid = booking_paid_amount(booking)
    if paid + Decimal("0.001") < required:
        pct = get_booking_prepayment_percent(db)
        raise BookingError(
            f"الدفع المقدّم غير كافٍ: مطلوب {required} د.ل ({prepayment_percent_label(pct)}). "
            f"المدفوع: {paid} د.ل."
        )


def booking_amount_due(
    db: Session,
    *,
    room_type_id: int,
    check_in: date,
    check_out: date,
    discount_amount: Decimal | str | float = Decimal("0"),
    nightly_rate: Decimal | None = None,
) -> Decimal:
    """إجمالي الإقامة المستحق قبل الدفع."""
    disc = Decimal(str(discount_amount or "0")).quantize(Decimal("0.001"))
    if nightly_rate is not None and Decimal(str(nightly_rate)) > 0:
        nights = max(1, (check_out - check_in).days)
        total = (Decimal(str(nightly_rate)) * Decimal(nights)).quantize(Decimal("0.001"))
    else:
        total = accommodation_total(
            db, room_type_id=room_type_id, check_in=check_in, check_out=check_out
        )
    due = (total - disc).quantize(Decimal("0.001"))
    return due if due > 0 else Decimal("0")


def validate_booking_prepayment(
    db: Session,
    *,
    amount: Decimal | str | float,
    payment_method_id: int | str | None,
    room_type_id: int,
    check_in: date,
    check_out: date,
    discount_amount: Decimal | str | float = Decimal("0"),
    nightly_rate: Decimal | None = None,
) -> Decimal:
    """يرفض الحجز إذا لم يُدفع الحد الأدنى المطلوب — يُرجع المبلغ المستحق."""
    due = booking_amount_due(
        db,
        room_type_id=room_type_id,
        check_in=check_in,
        check_out=check_out,
        discount_amount=discount_amount,
        nightly_rate=nightly_rate,
    )
    if due <= 0:
        raise BookingError("تعذّر حساب تكلفة الإقامة — تحقق من التواريخ.")
    required = required_prepayment_amount(db, due=due)
    amt = Decimal(str(amount or "0")).quantize(Decimal("0.001"))
    pm_raw = str(payment_method_id or "").strip()

    if required <= 0:
        if amt > 0 and not pm_raw.isdigit():
            raise BookingError("اختر وسيلة الدفع لتسجيل المبلغ المدفوع.")
        return due

    if amt <= 0:
        raise BookingError(prepayment_requirement_message(db, due=due, required=required))
    if not pm_raw.isdigit():
        raise BookingError("اختر وسيلة الدفع — بدونها لا يُسجَّل المبلغ ولا يُنشأ الحجز.")
    if amt + Decimal("0.001") < required:
        raise BookingError(
            f"{prepayment_requirement_message(db, due=due, required=required)} "
            f"المُدخل: {amt} د.ل."
        )
    return due


def _ref(*, quotation: bool = False) -> str:
    token = secrets.token_hex(4).upper()
    return f"Q{token}" if quotation else token


@dataclass
class StayingGuestInput:
    full_name: str
    id_number: str | None = None
    id_type: str | None = None
    nationality: str | None = None
    address: str | None = None
    phone: str | None = None


def parse_staying_guests(
    *,
    names: list[str],
    id_numbers: list[str] | None = None,
    id_types: list[str] | None = None,
    nationalities: list[str] | None = None,
    addresses: list[str] | None = None,
    phones: list[str] | None = None,
) -> list[StayingGuestInput]:
    id_numbers = id_numbers or []
    id_types = id_types or []
    nationalities = nationalities or []
    addresses = addresses or []
    phones = phones or []
    guests: list[StayingGuestInput] = []
    for i, raw_name in enumerate(names):
        name = (raw_name or "").strip()
        if not name:
            continue
        guests.append(
            StayingGuestInput(
                full_name=name,
                id_number=(id_numbers[i] if i < len(id_numbers) else "").strip() or None,
                id_type=(id_types[i] if i < len(id_types) else "").strip() or None,
                nationality=(nationalities[i] if i < len(nationalities) else "").strip() or None,
                address=(addresses[i] if i < len(addresses) else "").strip() or None,
                phone=(phones[i] if i < len(phones) else "").strip() or None,
            )
        )
    return guests


def sync_staying_guests(
    db: Session,
    booking: HotelBooking,
    guests: list[StayingGuestInput],
    *,
    fallback_name: str | None = None,
    fallback_phone: str | None = None,
    fallback_id_number: str | None = None,
    fallback_id_type: str | None = None,
    fallback_nationality: str | None = None,
    fallback_address: str | None = None,
) -> list[HotelBookingGuest]:
    if not guests:
        fb_name = (fallback_name or booking.guest_name or "").strip()
        if not fb_name:
            raise BookingError("أدخل اسم نزيل واحد على الأقل في الشقة.")
        guests = [
            StayingGuestInput(
                full_name=fb_name,
                id_number=fallback_id_number or booking.guest_id_number,
                id_type=fallback_id_type or booking.guest_id_type,
                nationality=fallback_nationality or booking.guest_nationality,
                address=fallback_address or booking.guest_address,
                phone=fallback_phone or booking.guest_phone,
            )
        ]
    booking.guests.clear()
    saved: list[HotelBookingGuest] = []
    for i, g in enumerate(guests):
        row = HotelBookingGuest(
            booking_id=booking.id,
            full_name=g.full_name,
            is_primary=(i == 0),
            id_number=g.id_number,
            id_type=g.id_type,
            nationality=g.nationality,
            address=g.address,
            phone=g.phone,
        )
        db.add(row)
        saved.append(row)
    primary = saved[0]
    booking.guest_id_number = primary.id_number
    booking.guest_id_type = primary.id_type
    booking.guest_nationality = primary.nationality
    booking.guest_address = primary.address
    db.flush()
    return saved


def _resolve_guest_fields(
    *,
    guest_type: GuestType,
    guest_name: str,
    guest_phone: str | None,
    guest_email: str | None,
    company_name: str | None = None,
    company_tax_id: str | None = None,
    company_address: str | None = None,
    company_contact_name: str | None = None,
    company_contact_phone: str | None = None,
    company_contact_email: str | None = None,
) -> dict:
    if guest_type == GuestType.COMPANY:
        cname = (company_name or "").strip()
        if not cname:
            raise BookingError("اسم الشركة مطلوب.")
        contact = (company_contact_name or guest_name or "").strip() or cname
        phone = (company_contact_phone or guest_phone or "").strip() or None
        email = (company_contact_email or guest_email or "").strip() or None
        return {
            "guest_name": cname,
            "guest_phone": phone,
            "guest_email": email,
            "company_name": cname,
            "company_tax_id": (company_tax_id or "").strip() or None,
            "company_address": (company_address or "").strip() or None,
            "company_contact_name": contact,
            "company_contact_phone": phone,
            "company_contact_email": email,
        }
    name = (guest_name or "").strip()
    if not name:
        raise BookingError("اسم جهة الحجز مطلوب.")
    phone = (guest_phone or "").strip() or None
    if not phone:
        raise BookingError("رقم الهاتف مطلوب للفرد.")
    return {
        "guest_name": name,
        "guest_phone": phone,
        "guest_email": (guest_email or "").strip() or None,
        "company_name": None,
        "company_tax_id": None,
        "company_address": None,
        "company_contact_name": None,
        "company_contact_phone": None,
        "company_contact_email": None,
    }


def _log_status(
    db: Session,
    booking: HotelBooking,
    to_status: BookingStatus,
    user_id: int | None,
    note: str | None = None,
    from_status: BookingStatus | None = None,
) -> None:
    db.add(
        HotelBookingStatusLog(
            booking_id=booking.id,
            from_status=(from_status or booking.booking_status).value,
            to_status=to_status.value,
            user_id=user_id,
            note=note,
        )
    )


def _log_room_status(
    db: Session,
    room: HotelRoom,
    to_status: RoomPhysicalStatus,
    user_id: int | None,
    note: str | None = None,
) -> None:
    from modules.hotel.booking_models import HotelRoomStatusLog

    old = room.physical_status
    db.add(
        HotelRoomStatusLog(
            room_id=room.id,
            from_status=old.value if old else None,
            to_status=to_status.value,
            user_id=user_id,
            note=note,
        )
    )
    room.physical_status = to_status


def ensure_default_property(db: Session) -> HotelProperty:
    prop = db.scalar(select(HotelProperty).limit(1))
    if prop is not None:
        return prop
    prop = HotelProperty(name_ar="العقار الرئيسي", is_active=True)
    db.add(prop)
    db.flush()
    return prop


def ensure_default_room_types(db: Session, property_id: int = 1) -> None:
    if db.scalar(select(func.count()).select_from(HotelRoomType)):
        return
    defaults = [
        ("غرفة مفردة", "SGL", 1, 0, Decimal("80")),
        ("غرفة مزدوجة", "DBL", 2, 0, Decimal("120")),
        ("جناح", "STE", 2, 2, Decimal("200")),
        ("شقة عائلية", "FAM", 4, 2, Decimal("250")),
    ]
    for name, code, adults, children, price in defaults:
        db.add(
            HotelRoomType(
                property_id=property_id,
                name_ar=name,
                code=code,
                capacity_adults=adults,
                capacity_children=children,
                base_price=price,
                is_active=True,
            )
        )
    db.flush()


def ensure_default_cancellation_policy(db: Session, property_id: int = 1) -> None:
    if db.scalar(select(func.count()).select_from(HotelCancellationPolicy)):
        return
    db.add(
        HotelCancellationPolicy(
            property_id=property_id,
            name_ar="سياسة افتراضية",
            hours_before_free=48,
            penalty_percent=Decimal("50"),
            no_show_nights_penalty=1,
            is_default=True,
            is_active=True,
        )
    )
    db.flush()


def list_room_types(db: Session, *, only_active: bool = False) -> list[HotelRoomType]:
    stmt = select(HotelRoomType).order_by(HotelRoomType.sort_order, HotelRoomType.name_ar)
    if only_active:
        stmt = stmt.where(HotelRoomType.is_active.is_(True))
    return list(db.scalars(stmt).all())


def room_type_usage(db: Session, room_type_id: int) -> dict[str, int]:
    from modules.hotel.booking_models import HotelRatePlan
    from sqlalchemy import inspect as sa_inspect

    table_names = set(sa_inspect(db.bind).get_table_names())

    rooms = 0
    bookings = 0
    rate_plans = 0
    try:
        if "hotel_rooms" in table_names:
            rooms = int(
                db.scalar(
                    select(func.count())
                    .select_from(HotelRoom)
                    .where(HotelRoom.room_type_id == room_type_id)
                )
                or 0
            )
        if "hotel_bookings" in table_names:
            bookings = int(
                db.scalar(
                    select(func.count())
                    .select_from(HotelBooking)
                    .where(HotelBooking.room_type_id == room_type_id)
                )
                or 0
            )
        if "hotel_rate_plans" in table_names:
            rate_plans = int(
                db.scalar(
                    select(func.count())
                    .select_from(HotelRatePlan)
                    .where(HotelRatePlan.room_type_id == room_type_id)
                )
                or 0
            )
    except Exception:  # noqa: BLE001
        pass
    return {"rooms": rooms, "bookings": bookings, "rate_plans": rate_plans}


def room_type_can_delete(db: Session, room_type_id: int) -> bool:
    u = room_type_usage(db, room_type_id)
    return u["rooms"] == 0 and u["bookings"] == 0


def set_room_type_active(db: Session, room_type_id: int, *, active: bool) -> HotelRoomType:
    rt = db.get(HotelRoomType, room_type_id)
    if rt is None:
        raise BookingError("النوع غير موجود.")
    rt.is_active = bool(active)
    db.flush()
    return rt


def delete_room_type(db: Session, room_type_id: int) -> None:
    u = room_type_usage(db, room_type_id)
    if u["rooms"] > 0:
        raise BookingError(
            f"لا يمكن الحذف — {u['rooms']} شقة مرتبطة بهذا النوع. عطّله بدلاً من الحذف."
        )
    if u["bookings"] > 0:
        raise BookingError(
            f"لا يمكن الحذف — {u['bookings']} حجز مرتبط بهذا النوع. عطّله بدلاً من الحذف."
        )
    rt = db.get(HotelRoomType, room_type_id)
    if rt is None:
        raise BookingError("النوع غير موجود.")
    db.delete(rt)
    db.flush()


def get_booking(db: Session, booking_id: int) -> HotelBooking | None:
    return db.get(HotelBooking, booking_id)


def get_booking_by_token(db: Session, token: str) -> HotelBooking | None:
    t = (token or "").strip()
    if not t:
        return None
    return db.scalar(select(HotelBooking).where(HotelBooking.access_token == t))


def list_bookings(
    db: Session,
    *,
    status: BookingStatus | None = None,
    room_id: int | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
    record_kind: RecordKind | None = RecordKind.BOOKING,
    limit: int = 200,
) -> list[HotelBooking]:
    stmt = select(HotelBooking).order_by(HotelBooking.id.desc())
    if record_kind is not None:
        stmt = stmt.where(HotelBooking.record_kind == record_kind)
    if status is not None:
        stmt = stmt.where(HotelBooking.booking_status == status)
    if room_id is not None:
        stmt = stmt.where(HotelBooking.room_id == room_id)
    if from_date is not None:
        stmt = stmt.where(HotelBooking.check_out >= from_date)
    if to_date is not None:
        stmt = stmt.where(HotelBooking.check_in <= to_date)
    stmt = stmt.limit(limit)
    return list(db.scalars(stmt).all())


def list_quotations(
    db: Session,
    *,
    quotation_status: QuotationStatus | None = None,
    limit: int = 200,
) -> list[HotelBooking]:
    stmt = (
        select(HotelBooking)
        .where(HotelBooking.record_kind == RecordKind.QUOTATION)
        .order_by(HotelBooking.id.desc())
        .limit(limit)
    )
    if quotation_status is not None:
        stmt = stmt.where(HotelBooking.quotation_status == quotation_status)
    return list(db.scalars(stmt).all())


def _recalc_payment_status(booking: HotelBooking) -> None:
    paid = Decimal(str(booking.paid_amount or 0))
    due = Decimal(str(booking.accommodation_total or 0)) - Decimal(
        str(booking.discount_amount or 0)
    )
    if paid <= 0:
        booking.payment_status = BookingPaymentStatus.UNPAID
    elif paid + Decimal("0.001") >= due:
        booking.payment_status = BookingPaymentStatus.FULLY_PAID
    else:
        booking.payment_status = BookingPaymentStatus.PARTIALLY_PAID


def _recalc_booking_accommodation(
    db: Session,
    booking: HotelBooking,
    *,
    room: HotelRoom | None = None,
    through_date: date | None = None,
) -> Decimal:
    """إعادة حساب إجمالي الإقامة حسب تواريخ الحجز الحالية."""
    end = through_date or booking.check_out
    if booking.booking_status == BookingStatus.CHECKED_IN and booking.room_assignments:
        return recompute_accommodation_through(db, booking, end)
    assigned = room
    if assigned is None and booking.room_id:
        assigned = db.get(HotelRoom, booking.room_id)
    return accommodation_segment_for_room(
        db,
        room=assigned,
        room_type_id=booking.room_type_id,
        seg_in=booking.check_in,
        seg_out=end,
    )


def _apply_stay_pricing(
    db: Session,
    booking: HotelBooking,
    *,
    room: HotelRoom | None = None,
    through_date: date | None = None,
) -> None:
    total = _recalc_booking_accommodation(
        db, booking, room=room, through_date=through_date
    )
    booking.accommodation_total = total
    nights = max(1, booking.nights)
    booking.nightly_rate = (total / Decimal(nights)).quantize(Decimal("0.001"))
    _recalc_payment_status(booking)


def adjust_stay_dates(
    db: Session,
    booking_id: int,
    *,
    new_check_in: date | None = None,
    new_check_out: date | None = None,
    user_id: int | None = None,
) -> HotelBooking:
    """تعديل مواعيد الإقامة — وصول أبكر أو مغادرة أبكر قبل التسكين."""
    booking = db.get(HotelBooking, booking_id)
    if booking is None:
        raise BookingError("الحجز غير موجود.")
    if booking.record_kind == RecordKind.QUOTATION:
        raise BookingError("عدّل التواريخ من عرض السعر أو حوّله إلى حجز.")
    if booking.booking_status in (
        BookingStatus.CANCELLED,
        BookingStatus.CHECKED_OUT,
        BookingStatus.NO_SHOW,
    ):
        raise BookingError("لا يمكن تعديل تواريخ هذا الحجز.")
    if booking.booking_status == BookingStatus.CHECKED_IN:
        raise BookingError(
            "النزيل مسكّن — للمغادرة المبكرة استخدم «تسجيل المغادرة» مع تاريخ المغادرة الفعلي."
        )

    ci = new_check_in or booking.check_in
    co = new_check_out or booking.check_out
    if co <= ci:
        raise BookingError("تاريخ المغادرة يجب أن يكون بعد تاريخ الوصول.")

    if new_check_in and new_check_in > booking.check_in:
        raise BookingError(
            "لتأجيل الوصول استخدم تاريخاً قبل الموعد الحالي، أو أنشئ حجزاً جديداً."
        )
    if new_check_out and new_check_out >= booking.check_out:
        raise BookingError(
            "لتمديد الإقامة استخدم «تمديد الإقامة»، أو للمغادرة المبكرة اختر تاريخاً قبل الموعد الحالي."
        )

    room_id = booking.room_id
    if room_id and has_room_conflict(
        db,
        room_id=room_id,
        check_in=ci,
        check_out=co,
        exclude_booking_id=booking.id,
    ):
        conflict = first_room_conflict(
            db,
            room_id=room_id,
            check_in=ci,
            check_out=co,
            exclude_booking_id=booking.id,
        )
        if conflict:
            raise BookingError(
                "الشقة غير متاحة في الفترة الجديدة — "
                f"تعارض مع حجز {conflict.reference} "
                f"({conflict.check_in} → {conflict.check_out})."
            )
        raise BookingError("الشقة غير متاحة في الفترة الجديدة.")

    if not new_check_in and not new_check_out:
        raise BookingError("حدّد تاريخ وصول أو مغادرة جديد.")

    old_ci, old_co = booking.check_in, booking.check_out
    if ci == old_ci and co == old_co:
        raise BookingError("التواريخ المدخلة مطابقة للمواعيد الحالية.")

    booking.check_in = ci
    booking.check_out = co
    booking.scheduled_check_out = co
    _apply_stay_pricing(db, booking)

    if new_check_in and new_check_in < old_ci:
        log_audit(
            db,
            entity_type="booking",
            entity_id=booking.id,
            action="early_arrival",
            field_name="check_in",
            old_value=str(old_ci),
            new_value=str(ci),
            user_id=user_id,
        )
    if new_check_out and new_check_out < old_co:
        log_audit(
            db,
            entity_type="booking",
            entity_id=booking.id,
            action="early_departure",
            field_name="check_out",
            old_value=str(old_co),
            new_value=str(co),
            user_id=user_id,
        )
    db.flush()
    return booking


def _is_checkin_assignment(assignment: HotelBookingRoomAssignment) -> bool:
    reason = (assignment.reason or "").strip()
    return "Check-in" in reason or "تعيين غرفة" in reason


def recompute_accommodation_through(
    db: Session, booking: HotelBooking, through_date: date
) -> Decimal:
    """إعادة حساب إجمالي الإقامة حتى تاريخ معيّن (مغادرة مبكرة أو تبديل شقة)."""
    if through_date <= booking.check_in:
        return Decimal("0")

    transfers = sorted(
        [a for a in booking.room_assignments if not _is_checkin_assignment(a)],
        key=lambda a: (
            a.effective_date or (a.created_at.date() if a.created_at else date.today()),
            a.id,
        ),
    )

    total = Decimal("0")
    cursor = booking.check_in
    current_room_id = booking.room_id

    if transfers and transfers[0].from_room_id:
        current_room_id = transfers[0].from_room_id

    for assignment in transfers:
        eff = assignment.effective_date or (
            assignment.created_at.date() if assignment.created_at else date.today()
        )
        eff = max(cursor, min(eff, through_date))
        if eff > cursor and current_room_id:
            room = db.get(HotelRoom, current_room_id)
            rt_id = room.room_type_id if room else booking.room_type_id
            total += accommodation_segment_for_room(
                db, room=room, room_type_id=rt_id, seg_in=cursor, seg_out=eff
            )
        cursor = eff
        current_room_id = assignment.to_room_id

    if cursor < through_date:
        room = db.get(HotelRoom, current_room_id or booking.room_id) if (
            current_room_id or booking.room_id
        ) else None
        rt_id = booking.room_type_id
        if room and room.room_type_id:
            rt_id = room.room_type_id
        total += accommodation_segment_for_room(
            db, room=room, room_type_id=rt_id, seg_in=cursor, seg_out=through_date
        )

    if not transfers and not total:
        room = db.get(HotelRoom, booking.room_id) if booking.room_id else None
        total = accommodation_segment_for_room(
            db,
            room=room,
            room_type_id=booking.room_type_id,
            seg_in=booking.check_in,
            seg_out=through_date,
        )

    return total.quantize(Decimal("0.001"))


def _room_change_pricing(
    db: Session,
    booking: HotelBooking,
    *,
    new_room: HotelRoom,
    transfer_date: date | None = None,
) -> tuple[Decimal, Decimal, Decimal]:
    """(إجمالي الإقامة الجديد، فرق السعر، متوسط السعر الليلي)."""
    old_total = Decimal(str(booking.accommodation_total or 0))
    old_rt = booking.room_type_id
    new_rt = new_room.room_type_id or old_rt
    old_room = db.get(HotelRoom, booking.room_id) if booking.room_id else None

    if booking.booking_status == BookingStatus.CHECKED_IN:
        eff = transfer_date or date.today()
        eff = max(booking.check_in, min(eff, booking.check_out))
        consumed = accommodation_segment_for_room(
            db, room=old_room, room_type_id=old_rt, seg_in=booking.check_in, seg_out=eff
        )
        remaining = accommodation_segment_for_room(
            db,
            room=new_room,
            room_type_id=new_rt,
            seg_in=eff,
            seg_out=booking.check_out,
        )
        new_total = (consumed + remaining).quantize(Decimal("0.001"))
    else:
        new_total = accommodation_segment_for_room(
            db,
            room=new_room,
            room_type_id=new_rt,
            seg_in=booking.check_in,
            seg_out=booking.check_out,
        )

    delta = (new_total - old_total).quantize(Decimal("0.001"))
    nights = max(1, booking.nights)
    avg_rate = (new_total / Decimal(nights)).quantize(Decimal("0.001"))
    return new_total, delta, avg_rate


def _migrate_unsettled_room_charges(
    db: Session, booking_id: int, new_room_id: int
) -> int:
    charges = list(
        db.scalars(
            select(RoomCharge).where(
                RoomCharge.booking_id == booking_id,
                RoomCharge.is_settled.is_(False),
            )
        ).all()
    )
    for rc in charges:
        rc.room_id = new_room_id
    return len(charges)


def available_rooms_for_change(db: Session, booking: HotelBooking) -> list[HotelRoom]:
    rooms = list(
        db.scalars(
            select(HotelRoom).where(HotelRoom.is_active.is_(True)).order_by(HotelRoom.number)
        ).all()
    )
    result: list[HotelRoom] = []
    for room in rooms:
        if room.id == booking.room_id:
            continue
        if room.physical_status in NOT_RENTABLE_STATUSES:
            continue
        if has_room_conflict(
            db,
            room_id=room.id,
            check_in=booking.check_in,
            check_out=booking.check_out,
            exclude_booking_id=booking.id,
        ):
            continue
        result.append(room)
    return result


def create_booking(
    db: Session,
    *,
    guest_name: str,
    guest_phone: str | None,
    guest_email: str | None,
    check_in: date,
    check_out: date,
    room_type_id: int,
    guest_id_number: str | None = None,
    guest_id_type: str | None = None,
    guest_address: str | None = None,
    guest_nationality: str | None = None,
    guest_type: GuestType = GuestType.INDIVIDUAL,
    record_kind: RecordKind = RecordKind.BOOKING,
    quotation_valid_until: date | None = None,
    quotation_notes: str | None = None,
    company_name: str | None = None,
    company_tax_id: str | None = None,
    company_address: str | None = None,
    company_contact_name: str | None = None,
    company_contact_phone: str | None = None,
    company_contact_email: str | None = None,
    room_id: int | None = None,
    adults: int = 1,
    children: int = 0,
    nightly_rate: Decimal | None = None,
    discount_amount: Decimal = Decimal("0"),
    source: BookingSource = BookingSource.RECEPTION,
    internal_notes: str | None = None,
    user_id: int | None = None,
    auto_confirm: bool = False,
    customer_id: int | None = None,
    staying_guests: list[StayingGuestInput] | None = None,
) -> HotelBooking:
    is_quotation = record_kind == RecordKind.QUOTATION
    identity = _resolve_guest_fields(
        guest_type=guest_type,
        guest_name=guest_name,
        guest_phone=guest_phone,
        guest_email=guest_email,
        company_name=company_name,
        company_tax_id=company_tax_id,
        company_address=company_address,
        company_contact_name=company_contact_name,
        company_contact_phone=company_contact_phone,
        company_contact_email=company_contact_email,
    )
    name = identity["guest_name"]
    if check_out <= check_in:
        raise BookingError("تاريخ المغادرة يجب أن يكون بعد الوصول.")
    rt = db.get(HotelRoomType, room_type_id)
    if rt is None or not rt.is_active:
        raise BookingError("نوع الغرفة غير صالح.")
    if adults > rt.capacity_adults + rt.capacity_children:
        raise BookingError("عدد الضيوف يتجاوز سعة نوع الغرفة.")

    if room_id is not None:
        room = db.get(HotelRoom, room_id)
        if room is None or (not is_quotation and not is_room_rentable(room)):
            raise BookingError("الغرفة غير جاهزة للحجز (تنظيف أو صيانة أو غير نشطة).")
        if not is_quotation:
            conflict = first_room_conflict(
                db, room_id=room_id, check_in=check_in, check_out=check_out
            )
            if conflict is not None:
                raise BookingError(
                    "الغرفة غير متاحة لأن هناك حجزاً متداخلاً "
                    f"من {conflict.check_in} إلى {conflict.check_out}. "
                    f"يجب أن تكون المغادرة قبل {conflict.check_in}."
                )
    elif not is_quotation and not available_rooms_for_type(
        db, room_type_id=room_type_id, check_in=check_in, check_out=check_out
    ):
        raise BookingError("لا توجد غرف متاحة من هذا النوع في التواريخ المختارة.")

    nights = max(1, (check_out - check_in).days)
    rate = nightly_rate
    if rate is not None:
        rate = Decimal(str(rate)).quantize(Decimal("0.001"))
        acc_total = (rate * Decimal(nights)).quantize(Decimal("0.001"))
    else:
        acc_total = accommodation_total(
            db, room_type_id=room_type_id, check_in=check_in, check_out=check_out
        )
        rate = nightly_rate_for_stay(
            db, room_type_id=room_type_id, check_in=check_in, check_out=check_out
        )

    policy = db.scalar(
        select(HotelCancellationPolicy).where(HotelCancellationPolicy.is_default.is_(True))
    )

    booking = HotelBooking(
        reference=_ref(quotation=is_quotation),
        property_id=rt.property_id,
        room_type_id=room_type_id,
        room_id=room_id,
        customer_id=customer_id,
        guest_name=name,
        guest_phone=identity["guest_phone"],
        guest_email=identity["guest_email"],
        guest_id_number=(guest_id_number or "").strip() or None,
        guest_id_type=(guest_id_type or "").strip() or None,
        guest_address=(guest_address or "").strip() or None,
        guest_nationality=(guest_nationality or "").strip() or None,
        guest_type=guest_type,
        record_kind=record_kind,
        quotation_status=QuotationStatus.DRAFT if is_quotation else None,
        company_name=identity["company_name"],
        company_tax_id=identity["company_tax_id"],
        company_address=identity["company_address"],
        company_contact_name=identity["company_contact_name"],
        company_contact_phone=identity["company_contact_phone"],
        company_contact_email=identity["company_contact_email"],
        quotation_valid_until=quotation_valid_until,
        quotation_notes=(quotation_notes or "").strip() or None,
        check_in=check_in,
        check_out=check_out,
        scheduled_check_out=check_out,
        adults=max(1, adults),
        children=max(0, children),
        nightly_rate=Decimal(str(rate)).quantize(Decimal("0.001")),
        discount_amount=Decimal(str(discount_amount)).quantize(Decimal("0.001")),
        accommodation_total=acc_total,
        booking_status=BookingStatus.PENDING,
        payment_status=BookingPaymentStatus.UNPAID,
        source=source,
        internal_notes=(internal_notes or "").strip() or None,
        created_by_id=user_id,
        cancellation_policy_id=policy.id if policy else None,
        access_token=secrets.token_urlsafe(32),
    )
    db.add(booking)
    db.flush()
    sync_staying_guests(
        db,
        booking,
        staying_guests or [],
        fallback_name=name,
        fallback_phone=identity["guest_phone"],
        fallback_id_number=(guest_id_number or "").strip() or None,
        fallback_id_type=(guest_id_type or "").strip() or None,
        fallback_nationality=(guest_nationality or "").strip() or None,
        fallback_address=(guest_address or "").strip() or None,
    )
    note = "إنشاء عرض سعر" if is_quotation else "إنشاء حجز"
    _log_status(db, booking, BookingStatus.PENDING, user_id, note=note)
    log_audit(
        db,
        entity_type="booking",
        entity_id=booking.id,
        action="create_quotation" if is_quotation else "create",
        user_id=user_id,
    )
    if auto_confirm and not is_quotation:
        confirm_booking(db, booking.id, user_id=user_id)
        db.refresh(booking)
    elif not is_quotation and booking.room_id and booking.check_in > date.today():
        _keep_room_available_for_future_booking(db, booking, user_id=user_id)
    if not is_quotation and source != BookingSource.ONLINE_STORE:
        try:
            from modules.notifications.hotel_hooks import emit_hotel_booking_created

            emit_hotel_booking_created(db, booking)
        except Exception:  # noqa: BLE001
            pass
    return booking


def _keep_room_available_for_future_booking(
    db: Session, booking: HotelBooking, *, user_id: int | None = None
) -> None:
    """الحجز المستقبلي لا يُخرِج الشقة من التأجير قبل يوم الوصول."""
    if not booking.room_id or booking.check_in <= date.today():
        return
    room = db.get(HotelRoom, booking.room_id)
    if room is None:
        return
    if room.physical_status == RoomPhysicalStatus.RESERVED:
        _log_room_status(
            db,
            room,
            RoomPhysicalStatus.AVAILABLE,
            user_id,
            "حجز مستقبلي — متاحة حتى موعد الوصول",
        )
    db.flush()


def confirm_booking(db: Session, booking_id: int, *, user_id: int | None = None) -> HotelBooking:
    booking = db.get(HotelBooking, booking_id)
    if booking is None:
        raise BookingError("الحجز غير موجود.")
    if booking.record_kind == RecordKind.QUOTATION:
        raise BookingError("عرض السعر ليس حجزاً — حوّله أولاً إلى حجز فعلي.")
    assert_booking_prepayment_met(db, booking)
    if booking.booking_status not in (BookingStatus.PENDING,):
        raise BookingError("لا يمكن تأكيد هذا الحجز.")
    if booking.room_id and has_room_conflict(
        db,
        room_id=booking.room_id,
        check_in=booking.check_in,
        check_out=booking.check_out,
        exclude_booking_id=booking.id,
    ):
        raise BookingError("الغرفة المحددة لم تعد متاحة.")
    old = booking.booking_status
    booking.booking_status = BookingStatus.CONFIRMED
    _log_status(db, booking, BookingStatus.CONFIRMED, user_id, from_status=old)
    log_audit(db, entity_type="booking", entity_id=booking.id, action="confirm", user_id=user_id)
    _keep_room_available_for_future_booking(db, booking, user_id=user_id)
    db.flush()
    try:
        from modules.notifications.hotel_hooks import emit_hotel_booking_confirmed

        emit_hotel_booking_confirmed(db, booking)
    except Exception:  # noqa: BLE001
        pass
    if booking.source == BookingSource.ONLINE_STORE:
        try:
            from modules.hotel.folio import build_folio
            from modules.receipt_whatsapp.service import send_hotel_receipt_whatsapp

            folio = build_folio(db, booking.id)
            send_hotel_receipt_whatsapp(db, booking, folio_total=folio.total)
        except Exception:  # noqa: BLE001
            pass
    return booking


def update_quotation_status(
    db: Session,
    booking_id: int,
    *,
    new_status: QuotationStatus,
    user_id: int | None = None,
    note: str | None = None,
) -> HotelBooking:
    booking = db.get(HotelBooking, booking_id)
    if booking is None:
        raise BookingError("السجل غير موجود.")
    if booking.record_kind != RecordKind.QUOTATION:
        raise BookingError("هذا السجل ليس عرض سعر.")
    current = booking.quotation_status or QuotationStatus.DRAFT
    if new_status == QuotationStatus.CONVERTED:
        raise BookingError("استخدم «تحويل إلى حجز» بدلاً من تغيير الحالة يدوياً.")
    allowed = QUOTATION_NEXT_STATUSES.get(current, ())
    if new_status not in allowed:
        raise BookingError(f"لا يمكن الانتقال من «{current.value}» إلى «{new_status.value}».")
    old = current
    booking.quotation_status = new_status
    log_audit(
        db,
        entity_type="booking",
        entity_id=booking.id,
        action="quotation_status",
        old_value=old.value,
        new_value=new_status.value,
        reason=(note or "").strip() or None,
        user_id=user_id,
    )
    db.flush()
    return booking


def convert_quotation_to_booking(
    db: Session,
    booking_id: int,
    *,
    user_id: int | None = None,
    auto_confirm: bool = True,
    require_payment: bool = True,
    payment_amount: Decimal | None = None,
    payment_method_id: int | None = None,
) -> HotelBooking:
    booking = db.get(HotelBooking, booking_id)
    if booking is None:
        raise BookingError("السجل غير موجود.")
    if booking.record_kind != RecordKind.QUOTATION:
        raise BookingError("هذا السجل حجز فعلي بالفعل.")
    if booking.quotation_status not in (
        QuotationStatus.ACCEPTED,
        QuotationStatus.UNDER_REVIEW,
        QuotationStatus.SENT,
    ):
        raise BookingError("يجب أن تكون حالة العرض «موافقة الشركة» أو قيد المراجعة قبل التحويل.")
    if booking.quotation_valid_until and booking.quotation_valid_until < date.today():
        raise BookingError("انتهت صلاحية عرض السعر — أنشئ عرضاً جديداً.")

    if booking.room_id:
        room = db.get(HotelRoom, booking.room_id)
        if room is None or not room.is_active:
            raise BookingError("الشقة المحددة غير متاحة.")
        if not is_room_rentable(room):
            raise BookingError("الشقة غير جاهزة (تنظيف أو صيانة).")
        if has_room_conflict(
            db,
            room_id=booking.room_id,
            check_in=booking.check_in,
            check_out=booking.check_out,
            exclude_booking_id=booking.id,
        ):
            raise BookingError("الشقة لم تعد متاحة في هذه الفترة — اختر شقة أخرى أو عدّل التواريخ.")
    elif not available_rooms_for_type(
        db,
        room_type_id=booking.room_type_id or 0,
        check_in=booking.check_in,
        check_out=booking.check_out,
        exclude_booking_id=booking.id,
    ):
        raise BookingError("لا توجد شقق متاحة من هذا النوع في التواريخ المحددة.")

    booking.record_kind = RecordKind.BOOKING
    booking.quotation_status = QuotationStatus.CONVERTED
    log_audit(
        db,
        entity_type="booking",
        entity_id=booking.id,
        action="convert_quotation",
        user_id=user_id,
    )
    db.flush()

    if require_payment:
        validate_booking_prepayment(
            db,
            amount=payment_amount or 0,
            payment_method_id=payment_method_id,
            room_type_id=booking.room_type_id or 0,
            check_in=booking.check_in,
            check_out=booking.check_out,
            discount_amount=booking.discount_amount,
            nightly_rate=booking.nightly_rate,
        )
        amt = Decimal(str(payment_amount or 0)).quantize(Decimal("0.001"))
        if amt > 0:
            record_payment(
                db,
                booking.id,
                amount=amt,
                payment_method_id=payment_method_id,
                is_deposit=True,
                user_id=user_id,
                note="دفع عند تحويل عرض السعر إلى حجز",
            )

    if auto_confirm:
        confirm_booking(db, booking.id, user_id=user_id)
        db.refresh(booking)

    try:
        from modules.notifications.hotel_hooks import emit_hotel_booking_created

        emit_hotel_booking_created(db, booking)
    except Exception:  # noqa: BLE001
        pass
    db.flush()
    return booking


def check_in_booking(
    db: Session,
    booking_id: int,
    *,
    room_id: int,
    user_id: int | None = None,
    actual_arrival: date | None = None,
) -> HotelBooking:
    booking = db.get(HotelBooking, booking_id)
    if booking is None:
        raise BookingError("الحجز غير موجود.")
    if booking.record_kind == RecordKind.QUOTATION:
        raise BookingError("عرض السعر ليس حجزاً فعلياً — حوّله أولاً.")
    assert_booking_prepayment_met(db, booking)
    if booking.booking_status == BookingStatus.CANCELLED:
        raise BookingError("لا يمكن Check-in لحجز ملغى.")
    if booking.booking_status not in (BookingStatus.CONFIRMED, BookingStatus.PENDING):
        raise BookingError("الحجز ليس في حالة تسمح بالتسكين.")

    today = date.today()
    scheduled_in = booking.check_in
    if actual_arrival:
        if actual_arrival >= booking.check_out:
            raise BookingError("تاريخ الوصول يجب أن يكون قبل تاريخ المغادرة.")
        effective_in = actual_arrival
    elif today < scheduled_in:
        raise BookingError(
            f"موعد الوصول المجدول {scheduled_in}. "
            "للتسكين المبكر حدّد «تاريخ الوصول الفعلي»."
        )
    else:
        effective_in = scheduled_in

    if has_room_conflict(
        db,
        room_id=room_id,
        check_in=effective_in,
        check_out=booking.check_out,
        exclude_booking_id=booking.id,
    ):
        conflict = first_room_conflict(
            db,
            room_id=room_id,
            check_in=effective_in,
            check_out=booking.check_out,
            exclude_booking_id=booking.id,
        )
        if conflict:
            raise BookingError(
                "الغرفة مشغولة في هذه الفترة — "
                f"حجز {conflict.reference} ({conflict.check_in} → {conflict.check_out})."
            )
        raise BookingError("الغرفة مشغولة في هذه الفترة.")

    room = db.get(HotelRoom, room_id)
    if room is None or not room.is_active:
        raise BookingError("الغرفة غير صالحة.")
    if room.physical_status in NOT_RENTABLE_STATUSES:
        raise BookingError("الغرفة غير جاهزة للتسكين (تنظيف أو صيانة).")

    if effective_in < scheduled_in:
        log_audit(
            db,
            entity_type="booking",
            entity_id=booking.id,
            action="early_arrival",
            field_name="check_in",
            old_value=str(scheduled_in),
            new_value=str(effective_in),
            user_id=user_id,
        )
        booking.check_in = effective_in

    old_room_id = booking.room_id
    if old_room_id and old_room_id != room_id:
        old_room = db.get(HotelRoom, old_room_id)
        if old_room:
            _log_room_status(db, old_room, RoomPhysicalStatus.AVAILABLE, user_id, "تغيير غرفة")
        db.add(
            HotelBookingRoomAssignment(
                booking_id=booking.id,
                from_room_id=old_room_id,
                to_room_id=room_id,
                reason="Check-in / تعيين غرفة",
                assigned_by_id=user_id,
            )
        )

    booking.room_id = room_id
    _apply_stay_pricing(db, booking, room=room)
    booking.booking_status = BookingStatus.CHECKED_IN
    booking.checked_in_at = datetime.now(timezone.utc)
    booking.checked_in_by_id = user_id
    booking.access_token = secrets.token_urlsafe(32)
    room.guest_name = booking.guest_name
    _log_room_status(db, room, RoomPhysicalStatus.OCCUPIED, user_id, "Check-in")
    _log_status(db, booking, BookingStatus.CHECKED_IN, user_id, from_status=BookingStatus.CONFIRMED)
    log_audit(db, entity_type="booking", entity_id=booking.id, action="check_in", user_id=user_id)
    db.flush()
    return booking


def check_out_booking(
    db: Session,
    booking_id: int,
    *,
    user_id: int | None = None,
    allow_balance: bool = False,
    post_as_debt: bool = False,
    debt_note: str | None = None,
    actual_departure: date | None = None,
) -> HotelBooking:
    from modules.hotel.booking_debts import create_checkout_debt
    from modules.hotel.departure_settlement import preview_departure_settlement
    from modules.hotel.folio import booking_balance_due

    booking = db.get(HotelBooking, booking_id)
    if booking is None:
        raise BookingError("الحجز غير موجود.")
    if booking.booking_status != BookingStatus.CHECKED_IN:
        raise BookingError("الحجز ليس في حالة Checked In.")

    settlement_snapshot = None
    if actual_departure and actual_departure < booking.check_out:
        if actual_departure <= booking.check_in:
            raise BookingError("تاريخ المغادرة الفعلي يجب أن يكون بعد تاريخ الوصول.")
        old_out = booking.check_out
        if booking.scheduled_check_out is None:
            booking.scheduled_check_out = old_out
        try:
            settlement_snapshot = preview_departure_settlement(
                db, booking, actual_departure=actual_departure
            )
        except BookingError:
            settlement_snapshot = None
        booking.check_out = actual_departure
        _apply_stay_pricing(db, booking, through_date=actual_departure)
        log_audit(
            db,
            entity_type="booking",
            entity_id=booking.id,
            action="early_departure",
            field_name="check_out",
            old_value=str(old_out),
            new_value=str(actual_departure),
            user_id=user_id,
        )

    balance = booking_balance_due(db, booking.id)
    if balance > Decimal("0.001"):
        if post_as_debt:
            if settlement_snapshot is None and actual_departure:
                settlement_snapshot = preview_departure_settlement(
                    db, booking, actual_departure=booking.check_out
                )
            create_checkout_debt(
                db,
                booking,
                balance,
                user_id=user_id,
                note=debt_note,
                settlement_json=settlement_snapshot.to_json() if settlement_snapshot else None,
            )
        elif not allow_balance:
            raise BookingError(f"لا يمكن Check-out — متبقٍ {balance} د.ل على الحساب.")

    booking.booking_status = BookingStatus.CHECKED_OUT
    booking.checked_out_at = datetime.now(timezone.utc)
    booking.checked_out_by_id = user_id
    if booking.room_id:
        room = db.get(HotelRoom, booking.room_id)
        if room:
            room.guest_name = None
            _log_room_status(db, room, RoomPhysicalStatus.DIRTY, user_id, "Check-out")
    _log_status(db, booking, BookingStatus.CHECKED_OUT, user_id, from_status=BookingStatus.CHECKED_IN)
    log_audit(
        db,
        entity_type="booking",
        entity_id=booking.id,
        action="check_out",
        user_id=user_id,
        reason=(
            "دين مُرحّل"
            if post_as_debt and balance > 0
            else ("متبقٍ مسموح" if allow_balance and balance > 0 else None)
        ),
        new_value=settlement_snapshot.to_json() if settlement_snapshot else None,
    )
    db.flush()
    return booking


def cancel_booking(
    db: Session,
    booking_id: int,
    *,
    user_id: int | None = None,
    reason: str | None = None,
) -> HotelBooking:
    booking = db.get(HotelBooking, booking_id)
    if booking is None:
        raise BookingError("الحجز غير موجود.")
    if booking.booking_status in (BookingStatus.CHECKED_OUT, BookingStatus.CANCELLED):
        raise BookingError("لا يمكن إلغاء هذا الحجز.")
    if booking.booking_status == BookingStatus.CHECKED_IN:
        raise BookingError("لا يمكن إلغاء حجز مسكّن — نفّذ Check-out أولاً.")

    if booking.room_id:
        room = db.get(HotelRoom, booking.room_id)
        if room and room.physical_status == RoomPhysicalStatus.RESERVED:
            _log_room_status(db, room, RoomPhysicalStatus.AVAILABLE, user_id, "إلغاء حجز")

    old = booking.booking_status
    booking.booking_status = BookingStatus.CANCELLED
    _log_status(db, booking, BookingStatus.CANCELLED, user_id, note=reason, from_status=old)
    log_audit(
        db,
        entity_type="booking",
        entity_id=booking.id,
        action="cancel",
        user_id=user_id,
        reason=reason,
    )
    db.flush()
    return booking


def mark_no_show(
    db: Session, booking_id: int, *, user_id: int | None = None
) -> HotelBooking:
    booking = db.get(HotelBooking, booking_id)
    if booking is None:
        raise BookingError("الحجز غير موجود.")
    if booking.booking_status != BookingStatus.CONFIRMED:
        raise BookingError("No-show متاح للحجوزات المؤكدة فقط.")
    if booking.room_id:
        room = db.get(HotelRoom, booking.room_id)
        if room:
            _log_room_status(db, room, RoomPhysicalStatus.AVAILABLE, user_id, "No-show")
    policy = (
        db.get(HotelCancellationPolicy, booking.cancellation_policy_id)
        if booking.cancellation_policy_id
        else None
    )
    if policy and policy.no_show_nights_penalty > 0:
        penalty = (
            Decimal(str(booking.nightly_rate)) * policy.no_show_nights_penalty
        ).quantize(Decimal("0.001"))
        booking.accommodation_total = max(booking.accommodation_total, penalty)
    old = booking.booking_status
    booking.booking_status = BookingStatus.NO_SHOW
    _log_status(db, booking, BookingStatus.NO_SHOW, user_id, from_status=old)
    log_audit(db, entity_type="booking", entity_id=booking.id, action="no_show", user_id=user_id)
    db.flush()
    return booking


def extend_stay(
    db: Session,
    booking_id: int,
    new_check_out: date,
    *,
    user_id: int | None = None,
) -> HotelBooking:
    booking = db.get(HotelBooking, booking_id)
    if booking is None:
        raise BookingError("الحجز غير موجود.")
    if booking.booking_status not in (BookingStatus.CONFIRMED, BookingStatus.CHECKED_IN):
        raise BookingError("لا يمكن تمديد هذا الحجز.")
    if new_check_out <= booking.check_out:
        raise BookingError("تاريخ المغادرة الجديد يجب أن يكون بعد الحالي.")
    if booking.room_id and has_room_conflict(
        db,
        room_id=booking.room_id,
        check_in=booking.check_in,
        check_out=new_check_out,
        exclude_booking_id=booking.id,
    ):
        raise BookingError("الغرفة محجوزة في الأيام الإضافية.")

    old_out = booking.check_out
    booking.check_out = new_check_out
    booking.scheduled_check_out = new_check_out
    if booking.room_type_id:
        booking.accommodation_total = accommodation_total(
            db,
            room_type_id=booking.room_type_id,
            check_in=booking.check_in,
            check_out=new_check_out,
        )
    log_audit(
        db,
        entity_type="booking",
        entity_id=booking.id,
        action="extend",
        field_name="check_out",
        old_value=str(old_out),
        new_value=str(new_check_out),
        user_id=user_id,
    )
    db.flush()
    return booking


def change_room(
    db: Session,
    booking_id: int,
    new_room_id: int,
    *,
    reason: str | None = None,
    transfer_date: date | None = None,
    user_id: int | None = None,
) -> HotelBooking:
    booking = db.get(HotelBooking, booking_id)
    if booking is None:
        raise BookingError("الحجز غير موجود.")
    if booking.booking_status not in (BookingStatus.CONFIRMED, BookingStatus.CHECKED_IN):
        raise BookingError("لا يمكن تغيير الغرفة لهذا الحجز.")
    if booking.room_id == new_room_id:
        raise BookingError("الشقة المختارة هي نفس الشقة الحالية.")
    if has_room_conflict(
        db,
        room_id=new_room_id,
        check_in=booking.check_in,
        check_out=booking.check_out,
        exclude_booking_id=booking.id,
    ):
        raise BookingError("الغرفة الجديدة غير متاحة.")

    new_room = db.get(HotelRoom, new_room_id)
    if new_room is None or not new_room.is_active:
        raise BookingError("الغرفة الجديدة غير صالحة.")

    eff_date = transfer_date
    if booking.booking_status == BookingStatus.CHECKED_IN:
        eff_date = transfer_date or date.today()
        eff_date = max(booking.check_in, min(eff_date, booking.check_out))

    new_total, price_delta, avg_rate = _room_change_pricing(
        db, booking, new_room=new_room, transfer_date=eff_date
    )

    old_room_id = booking.room_id
    if old_room_id:
        old_room = db.get(HotelRoom, old_room_id)
        if old_room:
            st = (
                RoomPhysicalStatus.AVAILABLE
                if booking.booking_status == BookingStatus.CONFIRMED
                else RoomPhysicalStatus.DIRTY
            )
            _log_room_status(db, old_room, st, user_id, reason or "تغيير غرفة")

    db.add(
        HotelBookingRoomAssignment(
            booking_id=booking.id,
            from_room_id=old_room_id,
            to_room_id=new_room_id,
            reason=reason,
            effective_date=eff_date,
            price_delta=price_delta,
            assigned_by_id=user_id,
        )
    )
    booking.room_id = new_room_id
    if new_room.room_type_id:
        booking.room_type_id = new_room.room_type_id
    booking.accommodation_total = new_total
    booking.nightly_rate = avg_rate
    _recalc_payment_status(booking)
    _migrate_unsettled_room_charges(db, booking.id, new_room_id)

    if booking.booking_status == BookingStatus.CHECKED_IN:
        _log_room_status(db, new_room, RoomPhysicalStatus.OCCUPIED, user_id, "تغيير غرفة")
        new_room.guest_name = booking.guest_name

    log_audit(
        db,
        entity_type="booking",
        entity_id=booking.id,
        action="change_room",
        field_name="room_id",
        old_value=str(old_room_id),
        new_value=str(new_room_id),
        reason=reason,
        user_id=user_id,
    )
    if price_delta != 0:
        log_audit(
            db,
            entity_type="booking",
            entity_id=booking.id,
            action="room_price_delta",
            new_value=str(price_delta),
            reason=reason,
            user_id=user_id,
        )
    db.flush()
    return booking


def apply_prepaid_credit(
    db: Session,
    booking_id: int,
    amount: Decimal,
    *,
    note: str | None = None,
    user_id: int | None = None,
) -> None:
    """يخصم من مدفوعات الحجز عند تسوية فواتير الغرفة من رصيد الزبون (دفع زائد)."""
    booking = db.get(HotelBooking, booking_id)
    if booking is None:
        raise BookingError("الحجز غير موجود.")
    amt = Decimal(str(amount)).quantize(Decimal("0.001"))
    if amt <= 0:
        return
    from modules.hotel.folio import build_guest_account

    credit = build_guest_account(db, booking_id).amount_credit
    if amt > credit + Decimal("0.0005"):
        raise BookingError("رصيد الزبون غير كافٍ لتسوية هذه الفاتورة.")
    booking.paid_amount = max(
        Decimal("0"),
        Decimal(str(booking.paid_amount or 0)) - amt,
    )
    _recalc_payment_status(booking)
    log_audit(
        db,
        entity_type="booking",
        entity_id=booking.id,
        action="prepaid_credit_applied",
        new_value=str(amt),
        reason=(note or "").strip() or None,
        user_id=user_id,
    )
    db.flush()


def record_payment(
    db: Session,
    booking_id: int,
    *,
    amount: Decimal,
    payment_method_id: int | None,
    is_deposit: bool = False,
    note: str | None = None,
    user_id: int | None = None,
) -> HotelBookingPayment:
    booking = db.get(HotelBooking, booking_id)
    if booking is None:
        raise BookingError("الحجز غير موجود.")
    amt = Decimal(str(amount)).quantize(Decimal("0.001"))
    if amt <= 0:
        raise BookingError("المبلغ يجب أن يكون موجباً.")
    try:
        from modules.payments.service import assert_hotel_payment_method

        assert_hotel_payment_method(db, payment_method_id)
    except Exception as exc:
        raise BookingError(str(exc)) from exc
    pay = HotelBookingPayment(
        booking_id=booking.id,
        amount=amt,
        payment_method_id=payment_method_id,
        is_deposit=is_deposit,
        note=(note or "").strip() or None,
        received_by_id=user_id,
    )
    db.add(pay)
    db.flush()
    from modules.gl.posting import post_hotel_booking_payment_shadow_safe

    post_hotel_booking_payment_shadow_safe(db, pay)
    booking.paid_amount = Decimal(str(booking.paid_amount or 0)) + amt
    if is_deposit:
        booking.deposit_amount = Decimal(str(booking.deposit_amount or 0)) + amt
    _recalc_payment_status(booking)
    log_audit(
        db,
        entity_type="booking",
        entity_id=booking.id,
        action="payment",
        new_value=str(amt),
        user_id=user_id,
    )
    try:
        from modules.notifications.hotel_hooks import emit_hotel_payment_received

        method_name = ""
        if payment_method_id:
            from modules.payments.models import PaymentMethod

            pm = db.get(PaymentMethod, payment_method_id)
            method_name = pm.name_ar if pm else ""
        emit_hotel_payment_received(
            db,
            booking,
            payment_amount=amt,
            payment_method=method_name,
        )
    except Exception:  # noqa: BLE001
        pass
    return pay


def refund_payment(
    db: Session,
    payment_id: int,
    *,
    amount: Decimal,
    reason: str | None,
    user_id: int | None = None,
) -> HotelBookingPaymentRefund:
    pay = db.get(HotelBookingPayment, payment_id)
    if pay is None:
        raise BookingError("الدفعة غير موجودة.")
    if pay.is_refunded:
        raise BookingError("الدفعة مستردة بالفعل.")
    amt = Decimal(str(amount)).quantize(Decimal("0.001"))
    if amt <= 0 or amt > pay.amount:
        raise BookingError("مبلغ الاسترداد غير صالح.")
    ref = HotelBookingPaymentRefund(
        payment_id=pay.id,
        amount=amt,
        reason=(reason or "").strip() or None,
        approved_by_id=user_id,
    )
    db.add(ref)
    db.flush()
    from modules.gl.posting import post_hotel_booking_payment_refund_shadow_safe

    post_hotel_booking_payment_refund_shadow_safe(db, ref)
    pay.is_refunded = True
    booking = db.get(HotelBooking, pay.booking_id)
    if booking:
        booking.paid_amount = max(
            Decimal("0"),
            Decimal(str(booking.paid_amount or 0)) - amt,
        )
        booking.payment_status = BookingPaymentStatus.REFUNDED
    log_audit(
        db,
        entity_type="payment",
        entity_id=pay.id,
        action="refund",
        new_value=str(amt),
        reason=reason,
        user_id=user_id,
    )
    return ref


def add_booking_service(
    db: Session,
    booking_id: int,
    *,
    name_ar: str,
    quantity: Decimal,
    unit_price: Decimal,
    product_id: int | None = None,
    sale_id: int | None = None,
    notes: str | None = None,
    user_id: int | None = None,
) -> HotelBookingService:
    booking = db.get(HotelBooking, booking_id)
    if booking is None:
        raise BookingError("الحجز غير موجود.")
    qty = Decimal(str(quantity)).quantize(Decimal("0.0001"))
    price = Decimal(str(unit_price)).quantize(Decimal("0.001"))
    line = (qty * price).quantize(Decimal("0.001"))
    svc = HotelBookingService(
        booking_id=booking.id,
        name_ar=name_ar.strip(),
        quantity=qty,
        unit_price=price,
        line_total=line,
        product_id=product_id,
        sale_id=sale_id,
        notes=(notes or "").strip() or None,
        added_by_id=user_id,
    )
    db.add(svc)
    log_audit(
        db,
        entity_type="booking",
        entity_id=booking.id,
        action="add_service",
        new_value=f"{name_ar}: {line}",
        user_id=user_id,
    )
    db.flush()
    try:
        from modules.notifications.hotel_hooks import emit_hotel_unpaid_service_added

        emit_hotel_unpaid_service_added(
            db,
            booking,
            service_name=svc.name_ar,
            service_amount=svc.line_total,
        )
    except Exception:  # noqa: BLE001
        pass
    return svc


def mark_room_clean(db: Session, room_id: int, *, user_id: int | None = None) -> HotelRoom:
    room = db.get(HotelRoom, room_id)
    if room is None:
        raise BookingError("الغرفة غير موجودة.")
    if room.physical_status != RoomPhysicalStatus.DIRTY:
        raise BookingError("الغرفة ليست بحاجة تنظيف.")
    _log_room_status(db, room, RoomPhysicalStatus.AVAILABLE, user_id, "Housekeeping")
    db.flush()
    return room


_MAINTENANCE_ALLOWED_FROM = frozenset(
    {
        RoomPhysicalStatus.AVAILABLE,
        RoomPhysicalStatus.DIRTY,
        RoomPhysicalStatus.RESERVED,
        RoomPhysicalStatus.BLOCKED,
        RoomPhysicalStatus.OUT_OF_SERVICE,
    }
)


def set_room_maintenance_status(
    db: Session,
    room_id: int,
    *,
    user_id: int | None = None,
    note: str | None = None,
    issue_type: str | None = None,
    maintenance_phone: str | None = None,
    maintenance_staff_name: str | None = None,
    employee_id: int | None = None,
    reported_by: str | None = None,
    send_notification: bool = True,
    required_from: RoomPhysicalStatus | None = None,
) -> HotelRoom:
    """وضع الشقة في الصيانة — من متاحة أو بعد التنظيف."""
    from modules.hotel.maintenance import issue_label

    room = db.get(HotelRoom, room_id)
    if room is None:
        raise BookingError("الغرفة غير موجودة.")
    if not room.is_active:
        raise BookingError("الشقة معطّلة — فعّلها أولاً من الإعداد.")
    if room.physical_status == RoomPhysicalStatus.MAINTENANCE:
        raise BookingError("الشقة في الصيانة بالفعل.")
    if room.physical_status == RoomPhysicalStatus.OCCUPIED:
        raise BookingError("الشقة مشغولة بنزيل — لا يمكن إرسالها للصيانة.")
    if active_booking_for_room(db, room_id):
        raise BookingError("الشقة مرتبطة بنزيل مسكّن — لا يمكن إرسالها للصيانة.")
    if required_from is not None:
        if room.physical_status != required_from:
            raise BookingError("يمكن إرسال الشقة للصيانة فقط من حالة «تحتاج تنظيف».")
    elif room.physical_status not in _MAINTENANCE_ALLOWED_FROM:
        raise BookingError(
            f"لا يمكن إرسال الشقة للصيانة من حالة «{room.physical_status.value}»."
        )

    issue_key = (issue_type or "other").strip().lower() or "other"
    label = issue_label(issue_key)
    detail = (note or "").strip()
    reason = f"{label}" + (f" — {detail}" if detail else "")

    if employee_id:
        from modules.hr.models import Employee

        emp = db.get(Employee, int(employee_id))
        if emp is None:
            raise BookingError("موظف الصيانة غير موجود.")
        maintenance_phone = (emp.phone or maintenance_phone or "").strip()
        maintenance_staff_name = (emp.full_name_ar or maintenance_staff_name or "").strip()
        if send_notification and not maintenance_phone:
            raise BookingError("موظف الصيانة المختار لا يملك رقم هاتف في ملفه.")

    _log_room_status(db, room, RoomPhysicalStatus.MAINTENANCE, user_id, reason or "صيانة")
    db.flush()

    if send_notification:
        phone = (maintenance_phone or "").strip()
        if not phone:
            from modules.settings.service import get_setting

            phone = (get_setting(db, "hotel_maintenance_phone") or "").strip()
        if not phone:
            raise BookingError("أدخل رقم موظف الصيانة أو اضبط الرقم الافتراضي في الإعدادات.")
        from modules.notifications.hotel_hooks import emit_hotel_room_maintenance

        emit_hotel_room_maintenance(
            db,
            room,
            issue_type=issue_key,
            issue_details=detail,
            maintenance_phone=phone,
            maintenance_staff_name=maintenance_staff_name or "",
            reported_by=reported_by or "",
            employee_id=employee_id,
        )
    return room


def mark_room_maintenance(
    db: Session,
    room_id: int,
    *,
    user_id: int | None = None,
    note: str | None = None,
    issue_type: str | None = None,
    maintenance_phone: str | None = None,
    maintenance_staff_name: str | None = None,
    employee_id: int | None = None,
    reported_by: str | None = None,
    send_notification: bool = True,
) -> HotelRoom:
    """بعد التفتيش — الشقة تحتاج صيانة ولا تُؤجَّر."""
    if not (issue_type or "").strip():
        raise BookingError("اختر نوع العطل (مكيف، ميكروويف، …).")
    return set_room_maintenance_status(
        db,
        room_id,
        user_id=user_id,
        note=note,
        issue_type=issue_type,
        maintenance_phone=maintenance_phone,
        maintenance_staff_name=maintenance_staff_name,
        employee_id=employee_id,
        reported_by=reported_by,
        send_notification=send_notification,
        required_from=RoomPhysicalStatus.DIRTY,
    )


def mark_maintenance_complete(db: Session, room_id: int, *, user_id: int | None = None) -> HotelRoom:
    room = db.get(HotelRoom, room_id)
    if room is None:
        raise BookingError("الغرفة غير موجودة.")
    if room.physical_status != RoomPhysicalStatus.MAINTENANCE:
        raise BookingError("الغرفة ليست في الصيانة.")
    _log_room_status(db, room, RoomPhysicalStatus.AVAILABLE, user_id, "اكتملت الصيانة")
    db.flush()
    return room


def active_booking_for_room(db: Session, room_id: int) -> HotelBooking | None:
    return db.scalar(
        select(HotelBooking).where(
            HotelBooking.room_id == room_id,
            HotelBooking.booking_status == BookingStatus.CHECKED_IN,
        )
    )
