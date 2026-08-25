"""روابط تأكيد انتهاء التنظيف عبر واتساب (بدون الاعتماد على webhook الوارد فقط)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import secrets
import time
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from modules.settings.service import get_setting, set_setting

_HK_TOKEN_TTL_SECONDS = 86400  # 24 ساعة


def _signing_secret(db: Session) -> str:
    secret = (get_setting(db, "hotel_hk_done_secret", "") or "").strip()
    if secret:
        return secret
    # إعادة استخدام مفتاح الوارد إن وُجد، وإلا توليد واحد ثابت في الإعدادات
    inbound = (get_setting(db, "messaging_inbound_secret", "") or "").strip()
    if inbound:
        return inbound
    secret = secrets.token_urlsafe(24)
    set_setting(db, "hotel_hk_done_secret", secret)
    return secret


def make_housekeeping_done_token(db: Session, room_id: int) -> str:
    secret = _signing_secret(db)
    ts = int(time.time())
    msg = f"{int(room_id)}:{ts}".encode()
    sig = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()[:32]
    payload = {"r": int(room_id), "t": ts, "s": sig}
    return base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).decode()


def verify_housekeeping_done_token(db: Session, room_id: int, token: str) -> bool:
    raw = (token or "").strip()
    if not raw:
        return False
    try:
        data = json.loads(base64.urlsafe_b64decode(raw.encode()).decode())
    except Exception:
        return False
    if data.get("r") != int(room_id):
        return False
    ts = int(data.get("t", 0))
    if ts <= 0 or int(time.time()) - ts > _HK_TOKEN_TTL_SECONDS:
        return False
    sig = (data.get("s") or "").strip()
    secret = _signing_secret(db)
    msg = f"{int(room_id)}:{ts}".encode()
    expected_sig = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(sig, expected_sig)


def base_url_reachable_from_phone(base_url: str) -> bool:
    """هل يمكن لجوال العامل فتح هذا الأصل؟ (ليس localhost / IP خاص)."""
    raw = (base_url or "").strip()
    if not raw:
        return False
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    host = (parsed.hostname or "").strip().lower()
    if not host or host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return False
    if host.endswith(".local") or host.endswith(".internal"):
        return False
    try:
        ip = ipaddress.ip_address(host)
        return not (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
        )
    except ValueError:
        # اسم نطاق عام — نفترض أنه قابل للوصول إن وُجد مخطط http(s)
        return parsed.scheme in ("http", "https")


def housekeeping_done_url(db: Session, room_id: int, *, base_url: str = "") -> str:
    base = (base_url or get_setting(db, "public_base_url", "") or "").strip().rstrip("/")
    if not base:
        return ""
    tok = make_housekeeping_done_token(db, room_id)
    return f"{base}/api/hotel/housekeeping/done/{int(room_id)}?t={tok}"


def housekeeping_confirm_button_id(
    db: Session, room_id: int, *, base_url: str = ""
) -> str:
    """معرّف زر واتساب: رابط تأكيد (يفتح المتصفح) إن وُجد، وإلا رد سريع يحتاج webhook."""
    done_url = housekeeping_done_url(db, room_id, base_url=base_url)
    # TextMeBot: إن بدأ id بـ http/https يصبح الزر «زر رابط» ولا يعتمد على الوارد.
    if done_url:
        return done_url
    return f"housekeeping_done:{int(room_id)}"


def ensure_inbound_ready_for_buttons(db: Session) -> str | None:
    """يفعّل استقبال الوارد إن لزم — يُرجع تحذيراً إن بقي غير جاهز."""
    from modules.messaging.outbox import whatsapp_provider
    from modules.messaging.service import messaging_enabled
    from modules.settings.service import get_bool, set_setting

    if not messaging_enabled(db):
        return "إرسال واتساب غير مفعّل."
    if whatsapp_provider(db) != "textmebot":
        return None
    if not get_bool(db, "messaging_inbound_enabled", False):
        secret = (get_setting(db, "messaging_inbound_secret", "") or "").strip()
        if not secret:
            secret = secrets.token_urlsafe(18)
            set_setting(db, "messaging_inbound_secret", secret)
        set_setting(db, "messaging_inbound_enabled", "1")
    return None
