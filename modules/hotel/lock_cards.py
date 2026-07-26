"""برمجة بطاقات أقفال الشقق عبر وكيل محلي (proRFL.dll)."""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from datetime import datetime, time, timedelta
from typing import Any

from sqlalchemy.orm import Session

from modules.hotel.booking_models import HotelBooking
from modules.hotel.models import HotelRoom
from modules.settings.service import get_bool, get_int, get_setting

log = logging.getLogger("hotel.lock_cards")


class LockCardError(Exception):
    pass


def lock_encoder_enabled(db: Session) -> bool:
    return get_bool(db, "hotel_lock_encoder_enabled", False)


def encoder_base_url(db: Session) -> str:
    return (get_setting(db, "hotel_lock_encoder_url", "http://127.0.0.1:9199") or "").rstrip("/")


def _fmt_lock_dt(value: datetime) -> str:
    return value.strftime("%y%m%d%H%M")


def _checkout_dt(booking: HotelBooking) -> datetime:
    """وقت انتهاء صلاحية البطاقة — ظهر يوم المغادرة (أو نهاية اليوم)."""
    d = booking.check_out
    if isinstance(d, datetime):
        return d
    return datetime.combine(d, time(12, 0))


def _checkin_dt(booking: HotelBooking) -> datetime:
    d = booking.check_in
    if isinstance(d, datetime):
        return d
    return datetime.combine(d, time(14, 0))


def build_guest_card_payload(db: Session, booking: HotelBooking) -> dict[str, Any]:
    if not lock_encoder_enabled(db):
        raise LockCardError("برمجة البطاقات غير مفعّلة. فعّلها من إعدادات الحجز.")
    co_id = (get_setting(db, "hotel_lock_co_id", "") or "").strip()
    if not co_id.isdigit():
        raise LockCardError("أدخل رقم الفندق (CoID) في إعدادات الأقفال.")
    room: HotelRoom | None = booking.room
    if room is None and booking.room_id:
        room = db.get(HotelRoom, int(booking.room_id))
    if room is None:
        raise LockCardError("لا توجد شقة مربوطة بهذا الحجز.")
    lock_no = (room.lock_no or "").strip()
    if not lock_no:
        raise LockCardError(
            f"الشقة #{room.number} بلا رقم قفل. أضفه من إعداد الشقق ثم أعد المحاولة."
        )
    begin = datetime.now()
    end = _checkout_dt(booking)
    if end <= begin:
        end = begin + timedelta(hours=2)
    card_no = int(booking.id) % 256
    return {
        "action": "guest_card",
        "co_id": int(co_id),
        "usb_flag": get_int(db, "hotel_lock_usb_flag", 1),
        "card_no": card_no,
        "dai": 0,
        "llock": 1 if get_bool(db, "hotel_lock_deadbolt", False) else 0,
        "pdoors": 1 if get_bool(db, "hotel_lock_public_doors", True) else 0,
        "begin": _fmt_lock_dt(begin),
        "end": _fmt_lock_dt(end),
        "lock_no": lock_no,
        "room_number": room.number,
        "booking_id": booking.id,
        "guest_name": (booking.guest_name or "").strip() or None,
        "check_in": str(booking.check_in),
        "check_out": str(booking.check_out),
        "encoder_url": encoder_base_url(db),
    }


def build_erase_payload(db: Session) -> dict[str, Any]:
    if not lock_encoder_enabled(db):
        raise LockCardError("برمجة البطاقات غير مفعّلة.")
    co_id = (get_setting(db, "hotel_lock_co_id", "") or "").strip()
    if not co_id.isdigit():
        raise LockCardError("أدخل رقم الفندق (CoID) في إعدادات الأقفال.")
    return {
        "action": "erase",
        "co_id": int(co_id),
        "usb_flag": get_int(db, "hotel_lock_usb_flag", 1),
        "encoder_url": encoder_base_url(db),
    }


def build_read_payload(db: Session) -> dict[str, Any]:
    if not lock_encoder_enabled(db):
        raise LockCardError("برمجة البطاقات غير مفعّلة.")
    return {
        "action": "read",
        "usb_flag": get_int(db, "hotel_lock_usb_flag", 1),
        "co_id": int((get_setting(db, "hotel_lock_co_id", "0") or "0").strip() or "0"),
        "encoder_url": encoder_base_url(db),
    }


def call_local_encoder(payload: dict[str, Any], *, timeout: float = 25.0) -> dict[str, Any]:
    """يستدعي الوكيل المحلي على جهاز الاستقبال (عادة 127.0.0.1)."""
    base = (payload.get("encoder_url") or "").rstrip("/")
    if not base:
        raise LockCardError("رابط وكيل البرمجة غير مضبوط.")
    action = payload.get("action") or "guest_card"
    path = {
        "guest_card": "/guest-card",
        "erase": "/erase",
        "read": "/read",
        "status": "/status",
    }.get(action)
    if not path:
        raise LockCardError(f"عملية غير معروفة: {action}")
    url = base + path
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST" if action != "status" else "GET",
    )
    if action == "status":
        req = urllib.request.Request(base + "/status", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            data = json.loads(raw) if raw else {}
    except urllib.error.URLError as exc:
        raise LockCardError(
            "تعذّر الاتصال بوكيل برمجة البطاقات على هذا الجهاز. "
            "تأكد أن door_lock_agent يعمل ومتصل بجهاز USB."
        ) from exc
    except Exception as exc:
        raise LockCardError(f"فشل استدعاء الوكيل: {exc}") from exc
    if not data.get("ok"):
        raise LockCardError(data.get("message") or data.get("error") or "فشل برمجة البطاقة")
    return data


def lock_ui_context(db: Session, booking: HotelBooking | None = None) -> dict[str, Any]:
    enabled = lock_encoder_enabled(db)
    room = booking.room if booking else None
    return {
        "lock_encoder_enabled": enabled,
        "lock_encoder_url": encoder_base_url(db),
        "lock_co_id": get_setting(db, "hotel_lock_co_id", "") or "",
        "lock_room_ready": bool(room and (room.lock_no or "").strip()),
        "lock_no": (room.lock_no if room else None) or "",
    }
