"""برمجة بطاقات أقفال الشقق عبر وكيل محلي (proRFL.dll)."""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from datetime import datetime, time, timedelta
from typing import Any
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from modules.hotel.booking_models import HotelBooking
from modules.hotel.models import HotelRoom
from modules.settings.service import get_bool, get_int, get_setting

log = logging.getLogger("hotel.lock_cards")

# وكيل USB محلي فقط — ليس proxy عامًا
_ENCODER_ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_ENCODER_PORT_MIN = 9190
_ENCODER_PORT_MAX = 9299
_ENCODER_PATHS = {
    "guest_card": "/guest-card",
    "erase": "/erase",
    "read": "/read",
    "status": "/status",
}


class LockCardError(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """يمنع إعادة توجيه الوكيل المحلي إلى عناوين أخرى (SSRF)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        raise urllib.error.HTTPError(req.full_url, code, "redirect blocked for lock encoder", headers, fp)


def assert_local_encoder_base_url(url: str) -> str:
    """يقيّد رابط وكيل الأقفال إلى loopback + منفذ ضيق + بدون مسار/استعلام."""
    raw = (url or "").strip().rstrip("/")
    if not raw:
        raise LockCardError("رابط وكيل البرمجة فارغ.")
    try:
        parsed = urlparse(raw)
    except Exception as exc:
        raise LockCardError("رابط وكيل البرمجة غير صالح.") from exc
    if (parsed.scheme or "").lower() != "http":
        raise LockCardError("وكيل الأقفال يجب أن يكون HTTP محليًا فقط.")
    if parsed.username is not None or parsed.password is not None:
        raise LockCardError("رابط وكيل الأقفال لا يقبل بيانات مستخدم.")
    host = (parsed.hostname or "").strip().lower()
    if host not in _ENCODER_ALLOWED_HOSTS:
        raise LockCardError("وكيل الأقفال مسموح على localhost فقط.")
    port = parsed.port or 80
    if port < _ENCODER_PORT_MIN or port > _ENCODER_PORT_MAX:
        raise LockCardError(
            f"منفذ وكيل الأقفال يجب أن يكون بين {_ENCODER_PORT_MIN} و {_ENCODER_PORT_MAX}."
        )
    path = (parsed.path or "").rstrip("/") or ""
    if path not in ("",):
        raise LockCardError("رابط وكيل الأقفال يجب أن يكون أساسًا بلا مسار.")
    if parsed.query or parsed.fragment:
        raise LockCardError("رابط وكيل الأقفال لا يقبل استعلامًا أو fragment.")
    # توحيد الشكل
    if host == "::1":
        return f"http://[::1]:{port}"
    return f"http://{host}:{port}"


def lock_encoder_enabled(db: Session) -> bool:
    return get_bool(db, "hotel_lock_encoder_enabled", False)


def encoder_base_url(db: Session) -> str:
    raw = (get_setting(db, "hotel_lock_encoder_url", "http://127.0.0.1:9199") or "").rstrip("/")
    try:
        return assert_local_encoder_base_url(raw or "http://127.0.0.1:9199")
    except LockCardError:
        return "http://127.0.0.1:9199"


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
    try:
        base = assert_local_encoder_base_url(payload.get("encoder_url") or "")
    except LockCardError:
        raise
    action = payload.get("action") or "guest_card"
    path = _ENCODER_PATHS.get(action)
    if not path:
        raise LockCardError(f"عملية غير معروفة: {action}")
    url = base + path
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if action == "status":
        req = urllib.request.Request(base + "/status", method="GET")
    else:
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            data = json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        if "redirect blocked" in (exc.msg or ""):
            raise LockCardError("رُفض إعادة توجيه من وكيل الأقفال.") from exc
        raise LockCardError(f"فشل استدعاء الوكيل: HTTP {exc.code}") from exc
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
    room = None
    lock_no = ""
    try:
        room = booking.room if booking else None
        lock_no = ((getattr(room, "lock_no", None) or "") if room else "").strip()
    except Exception:  # noqa: BLE001
        room = None
        lock_no = ""
    return {
        "lock_encoder_enabled": enabled,
        "lock_encoder_url": encoder_base_url(db),
        "lock_co_id": get_setting(db, "hotel_lock_co_id", "") or "",
        "lock_room_ready": bool(lock_no),
        "lock_no": lock_no,
    }
