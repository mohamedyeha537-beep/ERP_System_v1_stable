"""اعتماد إلغاء الحجز / الليلة عبر واتساب الأدمن (موافقة / رفض)."""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.hotel.booking_service import BookingError
from modules.notifications.action_handler import create_pending_action
from modules.notifications.models import NotificationAction, NotificationActionStatus
from modules.settings.service import get_setting, set_setting

ADMIN_PHONE_KEY = "hotel_cancel_admin_phone"

KIND_CANCEL = "cancel"
KIND_LATE_CANCEL = "late_cancel"
KIND_NO_SHOW = "no_show"
KIND_WAIVE_AUTO_NIGHT = "waive_auto_night"
KIND_WAIVE_PREVIOUS_NIGHT = "waive_previous_night"

KIND_LABELS = {
    KIND_CANCEL: "إلغاء حجز",
    KIND_LATE_CANCEL: "إلغاء متأخر",
    KIND_NO_SHOW: "عدم حضور (No Show)",
    KIND_WAIVE_AUTO_NIGHT: "إلغاء ليلة إضافية تلقائية",
    KIND_WAIVE_PREVIOUS_NIGHT: "إلغاء ليلة سابقة ثم تسكين",
}


class CancelApprovalError(BookingError):
    pass


def _normalize_phone(raw: str | None) -> str:
    from modules.messaging.phone_utils import normalize_whatsapp_phone

    return normalize_whatsapp_phone((raw or "").strip(), country_code="218")


def cancel_admin_phone(db: Session) -> str:
    for key in (
        ADMIN_PHONE_KEY,
        "messaging_admin_phone",
        "hotel_shift_supervisor_phone",
    ):
        try:
            phone = _normalize_phone(get_setting(db, key, "") or "")
        except Exception:
            phone = ""
        if phone:
            return phone
    return ""


def save_cancel_admin_phone(db: Session, raw: str | None) -> str:
    text = (raw or "").strip()
    if not text:
        set_setting(db, ADMIN_PHONE_KEY, "")
        return ""
    phone = _normalize_phone(text)
    if not phone:
        raise CancelApprovalError("رقم واتساب الأدمن غير صالح.")
    set_setting(db, ADMIN_PHONE_KEY, text.strip())
    return phone


def pending_cancel_for_booking(db: Session, booking_id: int) -> NotificationAction | None:
    rows = list(
        db.scalars(
            select(NotificationAction)
            .where(
                NotificationAction.handled_status == NotificationActionStatus.PENDING.value,
                NotificationAction.action_key.in_(tuple(KIND_LABELS)),
            )
            .order_by(NotificationAction.id.desc())
        ).all()
    )
    for row in rows:
        try:
            payload = json.loads(row.action_payload_json or "{}")
        except json.JSONDecodeError:
            continue
        if str(payload.get("kind") or row.action_key or "").startswith(
            ("cancel", "late_cancel", "no_show", "waive_")
        ) and int(payload.get("booking_id") or 0) == int(booking_id):
            return row
    return None


def request_cancel_approval(
    db: Session,
    *,
    booking_id: int,
    kind: str,
    requested_by_id: int | None,
    reason: str | None = None,
    extra: dict[str, Any] | None = None,
) -> NotificationAction:
    from modules.authz.models import User
    from modules.hotel.booking_models import HotelBooking
    from modules.messaging.models import MessageChannel
    from modules.messaging.outbox import enqueue_message, send_outbox_item_now

    if kind not in KIND_LABELS:
        raise CancelApprovalError("نوع طلب الإلغاء غير معروف.")
    phone = cancel_admin_phone(db)
    if not phone:
        raise CancelApprovalError(
            "أدخل رقم واتساب الأدمن من إعدادات الحجز أولاً — لا يُنفَّذ الإلغاء دون موافقة."
        )
    booking = db.get(HotelBooking, int(booking_id))
    if booking is None:
        raise CancelApprovalError("الحجز غير موجود.")

    existing = pending_cancel_for_booking(db, int(booking_id))
    payload = {
        "kind": kind,
        "booking_id": int(booking_id),
        "reason": (reason or "").strip(),
        "requested_by_id": requested_by_id,
        **(extra or {}),
    }
    if existing is not None:
        existing.action_payload_json = json.dumps(payload, ensure_ascii=False)
        act = existing
    else:
        act = create_pending_action(db, action_key=kind, payload=payload)

    requester = ""
    if requested_by_id:
        u = db.get(User, int(requested_by_id))
        requester = (getattr(u, "username", None) or "") if u else ""
    room = ""
    if getattr(booking, "room", None) is not None:
        room = booking.room.number or booking.room.name_ar or ""
    guest = (booking.guest_name or booking.display_name or "—").strip()
    label = KIND_LABELS[kind]
    why = (reason or "").strip() or "—"
    body = (
        f"⚠️ طلب {label}\n"
        f"الحجز: {booking.reference or ('#' + str(booking.id))}\n"
        f"النزيل: {guest}\n"
        f"الشقة: {room or '—'}\n"
        f"الموظف: {requester or '—'}\n"
        f"السبب: {why}\n\n"
        f"لن يُنفَّذ الإلغاء إلا بعد ضغط موافقة."
    )
    buttons = [
        {"text": "✓ موافقة", "id": f"hotel_approve:{act.id}"},
        {"text": "✗ رفض", "id": f"hotel_reject:{act.id}"},
    ]
    row = enqueue_message(
        db,
        body=body,
        channel=MessageChannel.WHATSAPP.value,
        phone=phone,
        event_type="hotel.cancel_approval_requested",
        meta={
            "kind": kind,
            "booking_id": int(booking_id),
            "action_id": act.id,
            "buttons": buttons,
        },
    )
    db.flush()
    send_outbox_item_now(db, row)
    return act


def _parse_iso_date(raw: Any) -> date | None:
    if raw in (None, ""):
        return None
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def _parse_iso_dt(raw: Any) -> datetime | None:
    if raw in (None, ""):
        return None
    if isinstance(raw, datetime):
        return raw
    try:
        return datetime.fromisoformat(str(raw).replace(" ", "T")[:19])
    except ValueError:
        return None


def execute_approved_cancel(db: Session, payload: dict[str, Any]) -> str:
    from modules.hotel.booking_service import (
        cancel_booking,
        check_in_booking,
        mark_no_show,
    )
    from modules.hotel.late_checkout import LateCheckoutError, waive_auto_late_night

    kind = str(payload.get("kind") or "").strip()
    booking_id = int(payload.get("booking_id") or 0)
    reason = (payload.get("reason") or "").strip() or None
    user_id = payload.get("requested_by_id")
    uid = int(user_id) if user_id else None

    if kind == KIND_CANCEL:
        cancel_booking(db, booking_id, user_id=uid, reason=reason, force_late=False)
        return "تم إلغاء الحجز بعد موافقة الأدمن."
    if kind == KIND_LATE_CANCEL:
        cancel_booking(db, booking_id, user_id=uid, reason=reason, force_late=True)
        return "تم تسجيل الإلغاء المتأخر بعد موافقة الأدمن."
    if kind == KIND_NO_SHOW:
        mark_no_show(db, booking_id, user_id=uid, automatic=False, reason=reason)
        return "تم تسجيل عدم الحضور بعد موافقة الأدمن."
    if kind == KIND_WAIVE_AUTO_NIGHT:
        try:
            waive_auto_late_night(db, booking_id, user_id=uid, reason=reason or "موافقة أدمن واتساب")
        except LateCheckoutError as exc:
            raise CancelApprovalError(str(exc)) from exc
        return "تم إلغاء الليلة الإضافية التلقائية بعد موافقة الأدمن."
    if kind == KIND_WAIVE_PREVIOUS_NIGHT:
        check_in_booking(
            db,
            booking_id,
            room_id=int(payload.get("room_id") or 0),
            user_id=uid,
            actual_arrival=_parse_iso_date(payload.get("actual_arrival")),
            actual_arrival_at=_parse_iso_dt(payload.get("actual_arrival_at")),
            confirm_after_midnight=True,
            waive_previous_night=True,
            waive_reason=reason or "موافقة أدمن واتساب",
            override_early_block=bool(payload.get("override_early_block")),
        )
        return "تم إلغاء الليلة السابقة وتسكين النزيل بعد موافقة الأدمن."
    raise CancelApprovalError("نوع طلب الإلغاء غير معروف.")


def phones_match(admin_phone: str, incoming: str | None) -> bool:
    try:
        a = _normalize_phone(admin_phone)
        b = _normalize_phone(incoming)
    except Exception:
        return False
    return bool(a and b and a == b)
