"""سياسة المغادرة المتأخرة (Overstay):

- عند ساعة المغادرة: حالة «منتهي وقت المغادرة» دون رسوم بعد.
- فترة السماح: يمكن تسجيل المغادرة مع سياسة رسوم (مجاني / نصف / كامل / ثابت).
- بعد السماح: خدمة مجدولة تضيف ليلة تلقائياً (Automatic Overstay Charge).
- إلغاء الليلة التلقائية: المدير فقط + سبب — بدون حذف سجل التدقيق.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.datetime_local import now_local, store_timezone
from modules.hotel.availability import has_room_conflict
from modules.hotel.booking_models import BookingStatus, HotelAuditLog, HotelBooking
from modules.settings.service import get_bool, get_int, get_setting

SETTING_ENABLED = "hotel_late_checkout_enabled"
SETTING_CHECKOUT_TIME = "hotel_default_check_out_time"
SETTING_GRACE_HOURS = "hotel_late_checkout_grace_hours"
SETTING_REMINDER_LEAD = "hotel_checkout_reminder_lead_hours"
SETTING_TICK_AT = "hotel_late_checkout_last_tick_at"
SETTING_GRACE_FEE_MODE = "hotel_late_checkout_grace_fee"
SETTING_GRACE_FEE_FIXED = "hotel_late_checkout_grace_fee_fixed"

DEFAULT_CHECKOUT_TIME = "12:00"
DEFAULT_GRACE_HOURS = 3
DEFAULT_REMINDER_LEAD_HOURS = 3
DEFAULT_GRACE_FEE_MODE = "FREE"
TICK_INTERVAL_SECONDS = 5 * 60  # كل ~5 دقائق (Scheduler PMS)

# FREE | HALF | FULL | FIXED — رسوم عند المغادرة أثناء فترة السماح
GRACE_FEE_MODES = ("FREE", "HALF", "FULL", "FIXED")
GRACE_FEE_LABELS_AR = {
    "FREE": "مجاني (بدون رسوم)",
    "HALF": "نصف ليلة",
    "FULL": "ليلة كاملة",
    "FIXED": "مبلغ ثابت",
}


def _parse_hhmm(raw: str) -> tuple[int, int]:
    text = (raw or DEFAULT_CHECKOUT_TIME).strip() or DEFAULT_CHECKOUT_TIME
    parts = text.split(":")
    try:
        hour = int(parts[0])
    except (TypeError, ValueError):
        hour = 12
    try:
        minute = int(parts[1]) if len(parts) > 1 else 0
    except (TypeError, ValueError):
        minute = 0
    return max(0, min(23, hour)), max(0, min(59, minute))


def normalize_checkout_time(raw: str) -> str:
    h, m = _parse_hhmm(raw)
    return f"{h:02d}:{m:02d}"


def normalize_grace_fee_mode(raw: str | None) -> str:
    mode = (raw or DEFAULT_GRACE_FEE_MODE).strip().upper()
    return mode if mode in GRACE_FEE_MODES else DEFAULT_GRACE_FEE_MODE


@dataclass(frozen=True)
class LateCheckoutPolicy:
    enabled: bool
    checkout_time: str
    grace_hours: int
    reminder_lead_hours: int
    grace_fee_mode: str = DEFAULT_GRACE_FEE_MODE
    grace_fee_fixed: Decimal = Decimal("0")

    def checkout_at(self, d: date) -> datetime:
        h, m = _parse_hhmm(self.checkout_time)
        return datetime(d.year, d.month, d.day, h, m, tzinfo=store_timezone())

    def grace_deadline(self, d: date) -> datetime:
        return self.checkout_at(d) + timedelta(hours=max(0, self.grace_hours))

    def reminder_at(self, d: date) -> datetime:
        return self.checkout_at(d) - timedelta(hours=max(0, self.reminder_lead_hours))


def late_checkout_policy(db: Session) -> LateCheckoutPolicy:
    grace = get_int(db, SETTING_GRACE_HOURS, DEFAULT_GRACE_HOURS)
    lead = get_int(db, SETTING_REMINDER_LEAD, DEFAULT_REMINDER_LEAD_HOURS)
    fixed_raw = get_setting(db, SETTING_GRACE_FEE_FIXED, "0") or "0"
    try:
        fixed = Decimal(str(fixed_raw).replace(",", ".")).quantize(Decimal("0.001"))
    except Exception:
        fixed = Decimal("0")
    return LateCheckoutPolicy(
        enabled=get_bool(db, SETTING_ENABLED, True),
        checkout_time=normalize_checkout_time(
            get_setting(db, SETTING_CHECKOUT_TIME, DEFAULT_CHECKOUT_TIME) or DEFAULT_CHECKOUT_TIME
        ),
        grace_hours=max(0, min(24, grace)),
        reminder_lead_hours=max(0, min(48, lead)),
        grace_fee_mode=normalize_grace_fee_mode(
            get_setting(db, SETTING_GRACE_FEE_MODE, DEFAULT_GRACE_FEE_MODE)
        ),
        grace_fee_fixed=max(Decimal("0"), fixed),
    )


def _checked_in_rows(db: Session) -> list[HotelBooking]:
    return list(
        db.scalars(
            select(HotelBooking).where(HotelBooking.booking_status == BookingStatus.CHECKED_IN)
        ).all()
    )


def list_checked_in_due_reminder(
    db: Session,
    now: datetime | None = None,
    *,
    policy: LateCheckoutPolicy | None = None,
) -> list[HotelBooking]:
    """نزلاء يوم المغادرة (أو بعده) حان وقت تنبيههم ولم ينتهِ السماح بعد."""
    pol = policy or late_checkout_policy(db)
    if not pol.enabled:
        return []
    now = now or now_local()
    out: list[HotelBooking] = []
    for booking in _checked_in_rows(db):
        co = booking.check_out
        if co is None or co > now.date():
            continue
        rem_at = pol.reminder_at(co)
        grace_end = pol.grace_deadline(co)
        if now >= rem_at and now < grace_end:
            out.append(booking)
    return out


def list_checked_in_due_auto_night(
    db: Session,
    now: datetime | None = None,
    *,
    policy: LateCheckoutPolicy | None = None,
) -> list[HotelBooking]:
    """نزلاء تجاوزوا نهاية السماح ليوم مغادرتهم الحالي دون تمديد/مغادرة."""
    pol = policy or late_checkout_policy(db)
    if not pol.enabled:
        return []
    now = now or now_local()
    out: list[HotelBooking] = []
    for booking in _checked_in_rows(db):
        co = booking.check_out
        if co is None:
            continue
        if co > now.date():
            continue
        if now < pol.grace_deadline(co):
            continue
        if _already_auto_charged_for_checkout(db, booking.id, co):
            continue
        out.append(booking)
    return out


def _already_auto_charged_for_checkout(
    db: Session, booking_id: int, checkout_day: date
) -> bool:
    row = db.scalars(
        select(HotelAuditLog.id)
        .where(
            HotelAuditLog.entity_type == "booking",
            HotelAuditLog.entity_id == booking_id,
            HotelAuditLog.action == "auto_late_checkout",
            HotelAuditLog.old_value == str(checkout_day),
        )
        .limit(1)
    ).first()
    return row is not None


def apply_auto_late_night(
    db: Session,
    booking: HotelBooking,
    *,
    policy: LateCheckoutPolicy | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """تمديد ليلة واحدة تلقائياً. يعيد ملخص النتيجة."""
    from modules.hotel.audit import log_audit
    from modules.hotel.booking_service import BookingError, extend_stay

    pol = policy or late_checkout_policy(db)
    now = now or now_local()
    if not pol.enabled:
        return {"ok": False, "reason": "disabled"}
    if booking.booking_status != BookingStatus.CHECKED_IN:
        return {"ok": False, "reason": "not_checked_in"}
    co = booking.check_out
    if co is None or co > now.date() or now < pol.grace_deadline(co):
        return {"ok": False, "reason": "not_due"}
    if _already_auto_charged_for_checkout(db, booking.id, co):
        return {"ok": False, "reason": "already_charged"}

    new_out = co + timedelta(days=1)
    if booking.room_id and has_room_conflict(
        db,
        room_id=booking.room_id,
        check_in=booking.check_in,
        check_out=new_out,
        exclude_booking_id=booking.id,
    ):
        log_audit(
            db,
            entity_type="booking",
            entity_id=booking.id,
            action="auto_late_checkout_blocked",
            field_name="check_out",
            old_value=str(co),
            new_value=str(new_out),
            reason="تعارض حجز على الغرفة — لم يُحتسب تمديد تلقائي",
            user_id=None,
        )
        return {
            "ok": False,
            "reason": "room_conflict",
            "booking_id": booking.id,
            "old_check_out": co,
            "new_check_out": new_out,
        }

    nights_before = max(1, int(booking.nights or 0))
    try:
        extend_stay(db, booking.id, new_out, user_id=None)
    except BookingError as exc:
        log_audit(
            db,
            entity_type="booking",
            entity_id=booking.id,
            action="auto_late_checkout_blocked",
            field_name="check_out",
            old_value=str(co),
            new_value=str(new_out),
            reason=str(exc),
            user_id=None,
        )
        return {"ok": False, "reason": "extend_failed", "error": str(exc)}

    log_audit(
        db,
        entity_type="booking",
        entity_id=booking.id,
        action="auto_late_checkout",
        field_name="check_out",
        old_value=str(co),
        new_value=str(new_out),
        reason=(
            f"Automatic Overstay Charge — تجاوز {pol.checkout_time}+{pol.grace_hours}س "
            "بدون مغادرة — احتساب ليلة إضافية تلقائياً بواسطة النظام"
        ),
        user_id=None,
    )
    db.refresh(booking)
    _ensure_overstay_added_a_billable_night(db, booking, nights_before=nights_before)
    db.refresh(booking)
    return {
        "ok": True,
        "booking_id": booking.id,
        "old_check_out": co,
        "new_check_out": new_out,
        "nights": booking.nights,
        "booking_total": booking.accommodation_total,
    }


def _ensure_overstay_added_a_billable_night(
    db: Session,
    booking: HotelBooking,
    *,
    nights_before: int,
) -> bool:
    """التمديد يجب أن يزيد الليالي المفوترة — لا يكفي تحريك تاريخ المغادرة فقط.

    إن كانت أول ليلة قابلة للتحصيل بعد تاريخ الوصول، يبقى الفرق يوماً واحداً
    بعد التمديد وتُعرض «ليلة واحدة» رغم احتساب التأخير.
    """
    from modules.hotel.booking_service import _apply_stay_pricing

    if int(booking.nights or 0) > int(nights_before or 0):
        return False
    start = getattr(booking, "first_chargeable_night", None)
    if start is None or booking.check_in is None or start <= booking.check_in:
        return False
    booking.first_chargeable_night = booking.check_in
    _apply_stay_pricing(db, booking)
    db.flush()
    return True


def repair_overstay_missing_night(db: Session, booking: HotelBooking) -> bool:
    """يلحق ليلة التأخير إن مُدّد الحجز تلقائياً دون زيادة الإقامة المفوترة."""
    auto_n = 0
    try:
        auto_n = len(
            list(
                db.scalars(
                    select(HotelAuditLog.id).where(
                        HotelAuditLog.entity_type == "booking",
                        HotelAuditLog.entity_id == int(booking.id),
                        HotelAuditLog.action == "auto_late_checkout",
                    )
                ).all()
            )
        )
    except Exception:  # noqa: BLE001
        auto_n = 0
    if auto_n <= 0:
        return False
    start = getattr(booking, "first_chargeable_night", None)
    if start is None or booking.check_in is None or start <= booking.check_in:
        return False
    span = max(0, (booking.check_out - booking.check_in).days)
    if int(booking.nights or 0) >= span:
        return False
    return _ensure_overstay_added_a_billable_night(
        db, booking, nights_before=int(booking.nights or 0)
    )


def _notify_auto_late_night(
    db: Session,
    booking: HotelBooking,
    result: dict[str, Any],
    *,
    now: datetime,
) -> None:
    from modules.hotel.folio import booking_balance_due
    from modules.notifications.hotel_hooks import (
        emit_hotel_auto_extend_blocked_staff,
        emit_hotel_balance_claim,
        emit_hotel_late_checkout_charged,
    )

    if result.get("ok"):
        emit_hotel_late_checkout_charged(db, booking, result=result)
        try:
            db.refresh(booking)
            if booking_balance_due(db, booking.id) > Decimal("0.001"):
                booking.claim_wa_until_paid = True
                if getattr(booking, "follow_up_at", None) is None:
                    booking.follow_up_at = now.date()
                if not (booking.follow_up_note or "").strip():
                    booking.follow_up_note = (
                        "مطالبة تلقائية بعد تمديد ليلة لتأخير المغادرة"
                    )
                emit_hotel_balance_claim(
                    db,
                    booking,
                    claim_note=booking.follow_up_note,
                    immediate=True,
                )
        except Exception:  # noqa: BLE001
            pass
        return
    if result.get("reason") == "room_conflict":
        try:
            emit_hotel_auto_extend_blocked_staff(
                db,
                booking,
                reason="room_conflict",
                old_check_out=result.get("old_check_out"),
                new_check_out=result.get("new_check_out"),
            )
        except Exception:  # noqa: BLE001
            pass


def ensure_overstay_nights_caught_up(
    db: Session,
    booking: HotelBooking,
    *,
    policy: LateCheckoutPolicy | None = None,
    now: datetime | None = None,
    notify: bool = True,
) -> list[dict[str, Any]]:
    """يلحق ليالي التأخير إن فاتت جدولة العامل (توقف سيرفر / تأخير التكت).

    يمدّد ليلة فليلة طالما تجاوز النزيل نهاية السماح ليوم المغادرة الحالي.
    """
    pol = policy or late_checkout_policy(db)
    now = now or now_local()
    applied: list[dict[str, Any]] = []
    if booking.booking_status == BookingStatus.CHECKED_IN:
        try:
            if repair_overstay_missing_night(db, booking):
                db.refresh(booking)
        except Exception:  # noqa: BLE001
            pass
    if not pol.enabled or booking.booking_status != BookingStatus.CHECKED_IN:
        return applied
    while True:
        result = apply_auto_late_night(db, booking, policy=pol, now=now)
        if result.get("ok"):
            applied.append(result)
            if notify:
                try:
                    _notify_auto_late_night(db, booking, result, now=now)
                except Exception:  # noqa: BLE001
                    pass
            db.refresh(booking)
            continue
        if notify and result.get("reason") == "room_conflict" and not applied:
            try:
                _notify_auto_late_night(db, booking, result, now=now)
            except Exception:  # noqa: BLE001
                pass
        break
    return applied


def process_late_checkout_tick(db: Session, *, now: datetime | None = None) -> int:
    """تذكيرات حسب الساعة ثم احتساب الليالي المستحقة. يُرجع عدد العمليات."""
    from modules.notifications.hotel_hooks import (
        emit_hotel_timed_checkout_reminders,
        emit_last_chance_checkout_reminder,
    )

    pol = late_checkout_policy(db)
    if not pol.enabled:
        return 0
    now = now or now_local()
    count = emit_hotel_timed_checkout_reminders(db, now=now, policy=pol)

    due = list_checked_in_due_auto_night(db, now=now, policy=pol)
    for booking in due:
        # إن فاتت نافذة التنبيه (توقف سيرفر…) نرسل تنبيهاً أخيراً قبل الاحتساب
        emit_last_chance_checkout_reminder(db, booking, policy=pol)
        count += len(
            ensure_overstay_nights_caught_up(
                db, booking, policy=pol, now=now, notify=True
            )
        )
    return count


def late_checkout_status_for_booking(
    db: Session,
    booking: HotelBooking,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """ملخص للعرض على صفحة الحجز — None إن لم يكن مناسباً."""
    if booking.booking_status != BookingStatus.CHECKED_IN:
        return None
    pol = late_checkout_policy(db)
    if not pol.enabled:
        return None
    now = now or now_local()
    co = booking.check_out
    if co is None or co > now.date():
        return None

    checkout_at = pol.checkout_at(co)
    grace_end = pol.grace_deadline(co)
    rem_at = pol.reminder_at(co)
    fee_label = GRACE_FEE_LABELS_AR.get(pol.grace_fee_mode, pol.grace_fee_mode)
    auto_row = get_last_auto_late_night_audit(db, booking.id)
    waiveable = bool(
        auto_row
        and not _auto_night_was_waived(db, booking.id, str(auto_row.old_value or ""))
        and str(booking.check_out) == str(auto_row.new_value or "")
    )

    if now < rem_at:
        phase = "before_reminder"
        message = (
            f"موعد المغادرة {co} الساعة {pol.checkout_time}. "
            f"سيُرسل تنبيه واتساب حوالي {rem_at.strftime('%H:%M')}."
        )
        overstay = False
    elif now < checkout_at:
        phase = "reminder_window"
        message = (
            f"حان وقت التنبيه — المغادرة الساعة {pol.checkout_time}. "
            f"بعد السماح ({pol.grace_hours} ساعات) يُحتسب تمديد ليلة تلقائياً إن لم يغادر النزيل."
        )
        overstay = False
    elif now < grace_end:
        phase = "in_grace"
        message = (
            f"منتهي وقت المغادرة (Overstay) — فترة السماح حتى {grace_end.strftime('%H:%M')}. "
            "لا تُحتسب ليلة جديدة بعد. عند تسجيل المغادرة تُطبَّق سياسة الفندق: "
            f"{fee_label}."
        )
        overstay = True
    else:
        phase = "past_grace"
        message = (
            "انتهت فترة السماح والنزيل ما زال مسجّلاً — "
            "تُضاف ليلة إضافية تلقائياً (Automatic Overstay Charge) إن لم يوجد تعارض على الغرفة."
        )
        overstay = True

    return {
        "enabled": True,
        "phase": phase,
        "overstay": overstay,
        "status_label_ar": "منتهي وقت المغادرة" if overstay else None,
        "check_out": co,
        "checkout_time": pol.checkout_time,
        "grace_hours": pol.grace_hours,
        "reminder_lead_hours": pol.reminder_lead_hours,
        "reminder_at": rem_at.strftime("%Y-%m-%d %H:%M"),
        "grace_deadline": grace_end.strftime("%Y-%m-%d %H:%M"),
        "grace_fee_mode": pol.grace_fee_mode,
        "grace_fee_label": fee_label,
        "grace_fee_fixed": str(pol.grace_fee_fixed),
        "can_waive_auto_night": waiveable,
        "auto_night_from": str(auto_row.old_value) if auto_row and waiveable else None,
        "auto_night_to": str(auto_row.new_value) if auto_row and waiveable else None,
        "message": message,
    }


def is_booking_overstay(
    db: Session,
    booking: HotelBooking,
    *,
    now: datetime | None = None,
    policy: LateCheckoutPolicy | None = None,
) -> bool:
    """True بعد ساعة المغادرة ويوم المغادرة (أو بعده) والنزيل ما زال مقيماً."""
    if booking.booking_status != BookingStatus.CHECKED_IN:
        return False
    pol = policy or late_checkout_policy(db)
    if not pol.enabled:
        return False
    now = now or now_local()
    co = booking.check_out
    if co is None or co > now.date():
        return False
    return now >= pol.checkout_at(co)


def computed_grace_fee_amount(
    booking: HotelBooking,
    *,
    policy: LateCheckoutPolicy,
) -> Decimal:
    mode = policy.grace_fee_mode
    rate = Decimal(str(booking.nightly_rate or 0)).quantize(Decimal("0.001"))
    if mode == "FREE" or rate < 0:
        return Decimal("0")
    if mode == "HALF":
        return (rate * Decimal("0.5")).quantize(Decimal("0.001"))
    if mode == "FULL":
        return rate
    if mode == "FIXED":
        return Decimal(str(policy.grace_fee_fixed or 0)).quantize(Decimal("0.001"))
    return Decimal("0")


def _already_grace_fee_for_day(db: Session, booking_id: int, checkout_day: date) -> bool:
    row = db.scalars(
        select(HotelAuditLog.id)
        .where(
            HotelAuditLog.entity_type == "booking",
            HotelAuditLog.entity_id == booking_id,
            HotelAuditLog.action == "grace_late_checkout_fee",
            HotelAuditLog.old_value == str(checkout_day),
        )
        .limit(1)
    ).first()
    return row is not None


def apply_grace_late_fee_on_checkout(
    db: Session,
    booking: HotelBooking,
    *,
    user_id: int | None = None,
    now: datetime | None = None,
    policy: LateCheckoutPolicy | None = None,
) -> dict[str, Any] | None:
    """رسوم المغادرة أثناء/بعد Overstay ضمن فترة السماح (لا ليلة تلقائية بعد).

    يُستدعى عند Check-out إن كان النزيل لا يزال في يوم المغاد المرتقب في الحالة Overstay
    ولم تُحتسب ليلة تلقائية لذلك اليوم.
    """
    from modules.hotel.audit import log_audit
    from modules.hotel.booking_service import add_booking_service

    pol = policy or late_checkout_policy(db)
    now = now or now_local()
    if not pol.enabled or booking.booking_status != BookingStatus.CHECKED_IN:
        return None
    co = booking.check_out
    if co is None:
        return None
    # إذا حان موعد الليلة التلقائية نترك auto charge؛ لا رسوم سماح إضافةً
    if now >= pol.grace_deadline(co):
        return None
    if now < pol.checkout_at(co):
        return None
    if _already_auto_charged_for_checkout(db, booking.id, co):
        return None
    if _already_grace_fee_for_day(db, booking.id, co):
        return None

    amount = computed_grace_fee_amount(booking, policy=pol)
    fee_label = GRACE_FEE_LABELS_AR.get(pol.grace_fee_mode, pol.grace_fee_mode)
    if amount <= Decimal("0.0005"):
        log_audit(
            db,
            entity_type="booking",
            entity_id=booking.id,
            action="grace_late_checkout_fee",
            field_name="late_fee",
            old_value=str(co),
            new_value="0",
            reason=f"مغادرة أثناء Overstay — سياسة: {fee_label} (بدون رسوم)",
            user_id=user_id,
        )
        return {"ok": True, "amount": Decimal("0"), "mode": pol.grace_fee_mode}

    svc = add_booking_service(
        db,
        booking.id,
        name_ar=f"رسوم مغادرة متأخرة ({fee_label})",
        quantity=Decimal("1"),
        unit_price=amount,
        notes=f"Overstay grace fee — {pol.grace_fee_mode} · يوم {co}",
        user_id=user_id,
        service_code="LATE_CHECKOUT",
        notify=False,
    )
    log_audit(
        db,
        entity_type="booking",
        entity_id=booking.id,
        action="grace_late_checkout_fee",
        field_name="late_fee",
        old_value=str(co),
        new_value=str(amount),
        reason=f"رسوم Overstay أثناء السماح — {fee_label}",
        user_id=user_id,
    )
    return {
        "ok": True,
        "amount": amount,
        "mode": pol.grace_fee_mode,
        "service_id": svc.id,
    }


def get_last_auto_late_night_audit(
    db: Session, booking_id: int
) -> HotelAuditLog | None:
    return db.scalars(
        select(HotelAuditLog)
        .where(
            HotelAuditLog.entity_type == "booking",
            HotelAuditLog.entity_id == booking_id,
            HotelAuditLog.action == "auto_late_checkout",
        )
        .order_by(HotelAuditLog.id.desc())
        .limit(1)
    ).first()


def _auto_night_was_waived(db: Session, booking_id: int, from_day: str) -> bool:
    row = db.scalars(
        select(HotelAuditLog.id)
        .where(
            HotelAuditLog.entity_type == "booking",
            HotelAuditLog.entity_id == booking_id,
            HotelAuditLog.action == "auto_late_checkout_waived",
            HotelAuditLog.old_value == from_day,
        )
        .limit(1)
    ).first()
    return row is not None


class LateCheckoutError(Exception):
    pass


def waive_auto_late_night(
    db: Session,
    booking_id: int,
    *,
    user_id: int | None,
    reason: str,
) -> HotelBooking:
    """إلغاء ليلة Overstay التلقائية — تخفيض يوم دون حذف سجل الاحتساب."""
    from modules.hotel.audit import log_audit
    from modules.hotel.booking_service import BookingError, _apply_stay_pricing

    reason_s = (reason or "").strip()
    if len(reason_s) < 3:
        raise LateCheckoutError("سبب إلغاء الليلة الإضافية إلزامي (٣ أحرف على الأقل).")

    booking = db.get(HotelBooking, booking_id)
    if booking is None:
        raise LateCheckoutError("الحجز غير موجود.")
    if booking.booking_status != BookingStatus.CHECKED_IN:
        raise LateCheckoutError("الإلغاء متاح فقط لنزيل مسكّن.")

    auto = get_last_auto_late_night_audit(db, booking_id)
    if auto is None:
        raise LateCheckoutError("لا توجد ليلة إضافية تلقائية لإلغائها.")
    from_day = str(auto.old_value or "").strip()
    to_day = str(auto.new_value or "").strip()
    if not from_day or not to_day:
        raise LateCheckoutError("سجل الاحتساب التلقائي غير مكتمل.")
    if _auto_night_was_waived(db, booking_id, from_day):
        raise LateCheckoutError("تم إلغاء هذه الليلة مسبقاً.")
    if str(booking.check_out) != to_day:
        raise LateCheckoutError(
            "تاريخ المغادرة تغيّر بعد الاحتساب التلقائي — "
            "عدّل الإقامة يدوياً إن لزم."
        )

    try:
        restore = date.fromisoformat(from_day)
    except ValueError as exc:
        raise LateCheckoutError("تاريخ الاحتساب غير صالح.") from exc
    if restore <= booking.check_in:
        raise LateCheckoutError("لا يمكن إرجاع المغادرة إلى ما قبل الوصول.")

    old_out = booking.check_out
    booking.check_out = restore
    if booking.scheduled_check_out and str(booking.scheduled_check_out) == to_day:
        booking.scheduled_check_out = restore
    try:
        _apply_stay_pricing(db, booking)
    except BookingError as exc:
        raise LateCheckoutError(str(exc)) from exc

    log_audit(
        db,
        entity_type="booking",
        entity_id=booking.id,
        action="auto_late_checkout_waived",
        field_name="check_out",
        old_value=from_day,
        new_value=str(restore),
        reason=f"Cancelled by Admin — {reason_s} (كان {old_out})",
        user_id=user_id,
    )
    # يبقى سجل auto_late_checkout الأصلي — لن تُعاد إضافة ليلة لنفس يوم from_day
    db.flush()
    return booking
