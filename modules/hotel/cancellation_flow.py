"""سياسة الإلغاء / No Show / Late Cancellation — حسب إعدادات الفندق.

- لا يُحذف الحجز أبداً؛ تُغيَّر الحالة مع أثر مالي وتشغيلي.
- No Show: لم يحضر حتى نهاية يوم الوصول (أو بعد الوقت المحدد) — تلقائي أو يدوي.
- Late Cancellation: إلغاء بعد موعد الدخول دون تسكين (أو صراحة).
- Cancelled: إلغاء عادي قبل/حسب سياسة الاسترجاع.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.datetime_local import now_local, store_timezone
from modules.hotel.booking_models import (
    BookingStatus,
    HotelBooking,
    HotelCancellationPolicy,
    RecordKind,
    RoomPhysicalStatus,
)
from modules.hotel.models import HotelRoom
from modules.settings.service import get_bool, get_setting, set_setting

SETTING_AUTO_NO_SHOW = "hotel_auto_no_show_enabled"
SETTING_NO_SHOW_CUTOFF = "hotel_no_show_cutoff_time"
SETTING_CANCEL_BEFORE_FEE = "hotel_cancel_before_arrival_fee"
SETTING_CANCEL_AFTER_FEE = "hotel_cancel_after_checkin_fee"
SETTING_NO_SHOW_FEE = "hotel_no_show_fee"
SETTING_NO_SHOW_FEE_FIXED = "hotel_no_show_fee_fixed"
SETTING_TICK_AT = "hotel_no_show_last_tick_at"
TICK_INTERVAL_SECONDS = 5 * 60

DEFAULT_CUTOFF = "23:59"
# FULL_REFUND | FIRST_NIGHT | HALF_NIGHT | KEEP_ALL | FIXED
FEE_MODES = ("FULL_REFUND", "FIRST_NIGHT", "HALF_NIGHT", "KEEP_ALL", "FIXED")
FEE_LABELS_AR = {
    "FULL_REFUND": "استرجاع كامل (بدون رسوم)",
    "FIRST_NIGHT": "خصم أول ليلة فقط",
    "HALF_NIGHT": "خصم نصف ليلة",
    "KEEP_ALL": "بدون استرجاع (احتساب كامل)",
    "FIXED": "مبلغ ثابت",
}


def normalize_fee_mode(raw: str | None, *, default: str) -> str:
    mode = (raw or default).strip().upper()
    return mode if mode in FEE_MODES else default


def _parse_hhmm(raw: str) -> tuple[int, int]:
    text = (raw or DEFAULT_CUTOFF).strip() or DEFAULT_CUTOFF
    parts = text.split(":")
    try:
        hour = int(parts[0])
    except (TypeError, ValueError):
        hour = 23
    try:
        minute = int(parts[1]) if len(parts) > 1 else 0
    except (TypeError, ValueError):
        minute = 59
    return max(0, min(23, hour)), max(0, min(59, minute))


def normalize_cutoff_time(raw: str) -> str:
    h, m = _parse_hhmm(raw)
    return f"{h:02d}:{m:02d}"


@dataclass(frozen=True)
class CancellationPolicySettings:
    auto_no_show: bool
    no_show_cutoff: str  # HH:MM end of arrival day
    check_in_time: str
    cancel_before_fee: str
    cancel_after_fee: str
    no_show_fee: str
    fee_fixed: Decimal
    hours_before_free: int  # from DB policy if present
    penalty_percent: Decimal
    no_show_nights: int

    def cutoff_at(self, d: date) -> datetime:
        h, m = _parse_hhmm(self.no_show_cutoff)
        return datetime(d.year, d.month, d.day, h, m, tzinfo=store_timezone())

    def check_in_at(self, d: date) -> datetime:
        h, m = _parse_hhmm(self.check_in_time)
        return datetime(d.year, d.month, d.day, h, m, tzinfo=store_timezone())


def cancellation_policy_settings(db: Session) -> CancellationPolicySettings:
    from modules.hotel.checkin_stay import normalize_checkin_time

    policy = db.scalar(
        select(HotelCancellationPolicy)
        .where(
            HotelCancellationPolicy.is_active.is_(True),
            HotelCancellationPolicy.is_default.is_(True),
        )
        .limit(1)
    )
    if policy is None:
        policy = db.scalar(
            select(HotelCancellationPolicy)
            .where(HotelCancellationPolicy.is_active.is_(True))
            .limit(1)
        )
    fixed_raw = get_setting(db, SETTING_NO_SHOW_FEE_FIXED, "0") or "0"
    try:
        fixed = Decimal(str(fixed_raw).replace(",", ".")).quantize(Decimal("0.001"))
    except Exception:
        fixed = Decimal("0")
    return CancellationPolicySettings(
        auto_no_show=get_bool(db, SETTING_AUTO_NO_SHOW, True),
        no_show_cutoff=normalize_cutoff_time(
            get_setting(db, SETTING_NO_SHOW_CUTOFF, DEFAULT_CUTOFF) or DEFAULT_CUTOFF
        ),
        check_in_time=normalize_checkin_time(
            get_setting(db, "hotel_default_check_in_time", "14:00") or "14:00"
        ),
        cancel_before_fee=normalize_fee_mode(
            get_setting(db, SETTING_CANCEL_BEFORE_FEE, "FULL_REFUND"),
            default="FULL_REFUND",
        ),
        cancel_after_fee=normalize_fee_mode(
            get_setting(db, SETTING_CANCEL_AFTER_FEE, "FIRST_NIGHT"),
            default="FIRST_NIGHT",
        ),
        no_show_fee=normalize_fee_mode(
            get_setting(db, SETTING_NO_SHOW_FEE, "FIRST_NIGHT"),
            default="FIRST_NIGHT",
        ),
        fee_fixed=max(Decimal("0"), fixed),
        hours_before_free=int(policy.hours_before_free) if policy else 48,
        penalty_percent=Decimal(str(policy.penalty_percent or 0)) if policy else Decimal("50"),
        no_show_nights=int(policy.no_show_nights_penalty) if policy else 1,
    )


def cancellation_settings_context(db: Session) -> dict[str, Any]:
    pol = cancellation_policy_settings(db)
    return {
        "hotel_auto_no_show_enabled": pol.auto_no_show,
        "hotel_no_show_cutoff_time": pol.no_show_cutoff,
        "hotel_cancel_before_arrival_fee": pol.cancel_before_fee,
        "hotel_cancel_after_checkin_fee": pol.cancel_after_fee,
        "hotel_no_show_fee": pol.no_show_fee,
        "hotel_no_show_fee_fixed": str(pol.fee_fixed),
        "cancel_fee_modes": [
            {"value": k, "label": FEE_LABELS_AR[k]} for k in FEE_MODES
        ],
    }


class CancellationFlowError(Exception):
    pass


def _free_room_if_needed(
    db: Session, booking: HotelBooking, *, user_id: int | None, note: str
) -> None:
    if not booking.room_id:
        return
    from modules.hotel.booking_service import _log_room_status

    room = db.get(HotelRoom, booking.room_id)
    if room is None:
        return
    if room.physical_status in (
        RoomPhysicalStatus.RESERVED,
        RoomPhysicalStatus.OCCUPIED,
    ):
        # بعد no-show/cancel تصبح متاحة للبيع (قد تحتاج تنظيفاً إن كانت مشغولة — نادر بدون check-in)
        target = (
            RoomPhysicalStatus.DIRTY
            if room.physical_status == RoomPhysicalStatus.OCCUPIED
            else RoomPhysicalStatus.AVAILABLE
        )
        _log_room_status(db, room, target, user_id, note)


def _charge_amount_for_mode(
    booking: HotelBooking,
    mode: str,
    *,
    fixed: Decimal,
    no_show_nights: int = 1,
) -> tuple[Decimal, int]:
    """(مبلغ الإقامة المحتسب, عدد ليالي الإقامة المحتسبة)."""
    rate = Decimal(str(booking.nightly_rate or 0)).quantize(Decimal("0.001"))
    full = Decimal(str(booking.accommodation_total or 0)).quantize(Decimal("0.001"))
    if full <= 0 and rate > 0:
        nights = max(1, int(booking.nights or 1))
        full = (rate * nights).quantize(Decimal("0.001"))

    m = normalize_fee_mode(mode, default="FIRST_NIGHT")
    if m == "FULL_REFUND":
        return Decimal("0"), 0
    if m == "FIRST_NIGHT":
        n = max(1, min(int(no_show_nights or 1), max(1, int(booking.nights or 1))))
        return (rate * n).quantize(Decimal("0.001")), n
    if m == "HALF_NIGHT":
        return (rate * Decimal("0.5")).quantize(Decimal("0.001")), 1
    if m == "FIXED":
        return max(Decimal("0"), fixed).quantize(Decimal("0.001")), 1 if fixed > 0 else 0
    if m == "KEEP_ALL":
        n = max(1, int(booking.nights or 1))
        return full if full > 0 else (rate * n).quantize(Decimal("0.001")), n
    return (rate).quantize(Decimal("0.001")), 1


def _apply_charge_to_booking(
    db: Session,
    booking: HotelBooking,
    *,
    charge: Decimal,
    charge_nights: int,
    fee_label: str,
    user_id: int | None,
    action: str,
) -> None:
    """يُبقي أثر احتساب الليالي/المبلغ دون حذف السجل."""
    from modules.hotel.audit import log_audit
    from modules.hotel.booking_service import _apply_stay_pricing, BookingError

    old_out = booking.check_out
    old_total = booking.accommodation_total
    if charge_nights <= 0 or charge <= Decimal("0.0005"):
        booking.nights = 0
        booking.accommodation_total = Decimal("0")
        booking.total_amount = (
            Decimal(str(booking.services_total or 0))
            + Decimal(str(booking.tax_total or 0))
            - Decimal(str(booking.discount_total or 0))
        ).quantize(Decimal("0.001"))
        if booking.total_amount < 0:
            booking.total_amount = Decimal("0")
    else:
        new_out = booking.check_in + timedelta(days=charge_nights)
        # لا نمدّد وراء المغادرة الأصلية
        if booking.check_out and new_out > booking.check_out:
            new_out = booking.check_out
        booking.check_out = new_out
        try:
            _apply_stay_pricing(db, booking)
        except BookingError:
            booking.nights = charge_nights
            booking.accommodation_total = charge
            booking.total_amount = (
                charge
                + Decimal(str(booking.services_total or 0))
                + Decimal(str(booking.tax_total or 0))
                - Decimal(str(booking.discount_total or 0))
            ).quantize(Decimal("0.001"))
        # إن زاد التسعير عن الرسوم المطلوبة (نادر) نضبط السقف
        if Decimal(str(booking.accommodation_total or 0)) > charge + Decimal("0.001"):
            booking.accommodation_total = charge
            booking.total_amount = (
                charge
                + Decimal(str(booking.services_total or 0))
                + Decimal(str(booking.tax_total or 0))
                - Decimal(str(booking.discount_total or 0))
            ).quantize(Decimal("0.001"))

    log_audit(
        db,
        entity_type="booking",
        entity_id=booking.id,
        action=action,
        field_name="accommodation_total",
        old_value=str(old_total),
        new_value=str(booking.accommodation_total),
        reason=(
            f"{fee_label} · check_out {old_out}→{booking.check_out} · "
            f"nights={booking.nights}"
        ),
        user_id=user_id,
    )


def classify_cancel_kind(
    booking: HotelBooking,
    *,
    policy: CancellationPolicySettings,
    now: datetime | None = None,
    force_late: bool = False,
) -> str:
    """cancelled | late_cancellation"""
    if force_late:
        return "late_cancellation"
    now = now or now_local()
    cin = booking.check_in
    if cin is None:
        return "cancelled"
    if now.date() < cin:
        return "cancelled"
    if now.date() > cin:
        # بعد يوم الوصول بدون تسكين → Late cancel أو No-show path separately
        return "late_cancellation"
    # يوم الوصول
    if now >= policy.check_in_at(cin):
        return "late_cancellation"
    return "cancelled"


def mark_no_show(
    db: Session,
    booking_id: int,
    *,
    user_id: int | None = None,
    automatic: bool = False,
    reason: str | None = None,
) -> HotelBooking:
    from modules.hotel.audit import log_audit
    from modules.hotel.booking_service import BookingError, _log_status

    booking = db.get(HotelBooking, booking_id)
    if booking is None:
        raise CancellationFlowError("الحجز غير موجود.")
    if getattr(booking, "record_kind", None) == RecordKind.QUOTATION:
        raise CancellationFlowError("لا ينطبق No Show على عروض الأسعار.")
    if booking.booking_status not in (BookingStatus.PENDING, BookingStatus.CONFIRMED):
        raise CancellationFlowError(
            "No Show متاح فقط للحجوزات المعلّقة أو المؤكدة (قبل التسكين)."
        )

    policy = cancellation_policy_settings(db)
    fee_mode = policy.no_show_fee
    charge, nights = _charge_amount_for_mode(
        booking,
        fee_mode,
        fixed=policy.fee_fixed,
        no_show_nights=policy.no_show_nights,
    )
    fee_label = FEE_LABELS_AR.get(fee_mode, fee_mode)
    if automatic:
        fee_label = f"Automatic No Show Charge — {fee_label}"
    else:
        fee_label = f"No Show Charge — {fee_label}"

    _apply_charge_to_booking(
        db,
        booking,
        charge=charge,
        charge_nights=nights,
        fee_label=fee_label,
        user_id=user_id,
        action="no_show_charge",
    )
    _free_room_if_needed(
        db, booking, user_id=user_id, note="No Show — تحرير الشقة للبيع"
    )
    old = booking.booking_status
    booking.booking_status = BookingStatus.NO_SHOW
    _log_status(
        db,
        booking,
        BookingStatus.NO_SHOW,
        user_id,
        note=reason or ("تلقائي — لم يحضر" if automatic else "لم يحضر"),
        from_status=old,
    )
    log_audit(
        db,
        entity_type="booking",
        entity_id=booking.id,
        action="no_show",
        field_name="booking_status",
        old_value=str(getattr(old, "value", old)),
        new_value=BookingStatus.NO_SHOW.value,
        reason=reason
        or (
            f"{'تلقائي' if automatic else 'يدوي'} · {fee_label}"
        ),
        user_id=user_id,
    )
    db.flush()
    return booking


def cancel_booking(
    db: Session,
    booking_id: int,
    *,
    user_id: int | None = None,
    reason: str | None = None,
    force_late: bool = False,
) -> HotelBooking:
    from modules.hotel.audit import log_audit
    from modules.hotel.booking_service import BookingError, _log_status

    booking = db.get(HotelBooking, booking_id)
    if booking is None:
        raise CancellationFlowError("الحجز غير موجود.")
    if booking.booking_status in (
        BookingStatus.CHECKED_OUT,
        BookingStatus.CANCELLED,
        BookingStatus.NO_SHOW,
        BookingStatus.LATE_CANCELLATION,
    ):
        raise CancellationFlowError("لا يمكن إلغاء هذا الحجز.")
    if booking.booking_status == BookingStatus.CHECKED_IN:
        raise CancellationFlowError(
            "لا يمكن إلغاء حجز مسكّن — نفّذ تسجيل المغادرة أولاً."
        )
    if booking.booking_status not in (
        BookingStatus.PENDING,
        BookingStatus.CONFIRMED,
    ):
        raise CancellationFlowError("حالة الحجز لا تسمح بالإلغاء.")

    policy = cancellation_policy_settings(db)
    kind = classify_cancel_kind(
        booking, policy=policy, force_late=force_late
    )
    if kind == "late_cancellation":
        fee_mode = policy.cancel_after_fee
        status = BookingStatus.LATE_CANCELLATION
        action = "late_cancellation"
        status_note = "إلغاء متأخر (Late Cancellation)"
    else:
        fee_mode = policy.cancel_before_fee
        status = BookingStatus.CANCELLED
        action = "cancel"
        status_note = "إلغاء حجز"

    charge, nights = _charge_amount_for_mode(
        booking,
        fee_mode,
        fixed=policy.fee_fixed,
        no_show_nights=1,
    )
    fee_label = FEE_LABELS_AR.get(fee_mode, fee_mode)
    _apply_charge_to_booking(
        db,
        booking,
        charge=charge,
        charge_nights=nights,
        fee_label=f"{status_note} — {fee_label}",
        user_id=user_id,
        action=f"{action}_charge",
    )
    _free_room_if_needed(db, booking, user_id=user_id, note=status_note)
    old = booking.booking_status
    booking.booking_status = status
    _log_status(
        db,
        booking,
        status,
        user_id,
        note=reason or status_note,
        from_status=old,
    )
    log_audit(
        db,
        entity_type="booking",
        entity_id=booking.id,
        action=action,
        field_name="booking_status",
        old_value=str(getattr(old, "value", old)),
        new_value=status.value,
        reason=reason or f"{status_note} · {fee_label}",
        user_id=user_id,
    )
    db.flush()
    return booking


def list_due_auto_no_show(
    db: Session,
    *,
    now: datetime | None = None,
    policy: CancellationPolicySettings | None = None,
) -> list[HotelBooking]:
    pol = policy or cancellation_policy_settings(db)
    if not pol.auto_no_show:
        return []
    now = now or now_local()
    rows = list(
        db.scalars(
            select(HotelBooking).where(
                HotelBooking.record_kind == RecordKind.BOOKING,
                HotelBooking.booking_status.in_(
                    [BookingStatus.PENDING, BookingStatus.CONFIRMED]
                ),
                HotelBooking.check_in <= now.date(),
            )
        ).all()
    )
    out: list[HotelBooking] = []
    for b in rows:
        if b.check_in is None:
            continue
        if b.check_in < now.date():
            out.append(b)
            continue
        # يوم الوصول: بعد وقت الـ cutoff
        if now >= pol.cutoff_at(b.check_in):
            out.append(b)
    return out


def process_auto_no_show_tick(db: Session, *, now: datetime | None = None) -> int:
    """كل بضع دقائق: تحويل الحجوزات التي لم تصل إلى No Show."""
    pol = cancellation_policy_settings(db)
    if not pol.auto_no_show:
        return 0
    now = now or now_local()
    count = 0
    for booking in list_due_auto_no_show(db, now=now, policy=pol):
        try:
            mark_no_show(
                db,
                booking.id,
                user_id=None,
                automatic=True,
                reason=(
                    f"لم يحضر حتى {pol.cutoff_at(booking.check_in).strftime('%Y-%m-%d %H:%M')}"
                ),
            )
            count += 1
        except (CancellationFlowError, Exception):  # noqa: BLE001
            continue
    return count
