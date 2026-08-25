"""إعادة توجيه إشعارات مركز النشاط إلى واتساب — حسب القسم أو إشعار معيّن."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.settings.service import get_bool, get_setting, set_setting

log = logging.getLogger("dashboard_notify.whatsapp_forward")

SETTING_ENABLED = "activity_hub_whatsapp_enabled"
SETTING_PHONE = "activity_hub_whatsapp_phone"

DEFAULT_EVENT_MESSAGE = (
    "🔔 *إشعار من المركز*\n"
    "{title}\n"
    "{detail}\n"
    "\n"
    "— مركز الإشعارات"
)

WA_GROUPS: list[tuple[str, str]] = [
    ("hotel", "الفندق والحجوزات"),
    ("sales", "المطعم والكاشير"),
    ("inventory", "المخزون والمشتريات"),
    ("finance", "المالية والخزينة"),
    ("hr", "الموارد البشرية"),
    ("customers", "العملاء والولاء"),
    ("messaging", "الرسائل والشات"),
    ("system", "النظام والتنبيهات"),
    ("events", "أحداث المنظومة"),
]


@dataclass
class WaEventOverrideView:
    event_key: str
    label_ar: str
    phones: str
    message_body: str


def group_phones_setting_key(group: str) -> str:
    g = re.sub(r"[^a-z0-9_]+", "_", (group or "").strip().lower())[:40]
    return f"activity_hub_wa_phones_{g}"


def event_phones_setting_key(event_key: str) -> str:
    """مفتاح قديم (توافق) — الأرقام تُفضَّل الآن من جدول activity_hub_wa_overrides."""
    ek = re.sub(r"[^a-z0-9_]+", "_", (event_key or "").strip().lower())[:48]
    return f"activity_hub_wa_event_phones_{ek}"


def hub_whatsapp_forward_enabled(db: Session) -> bool:
    return get_bool(db, SETTING_ENABLED, False)


def _parse_phones(raw: str | None) -> list[str]:
    from modules.notifications.routing import parse_phone_list

    return parse_phone_list(raw)


def event_label(event_key: str) -> str:
    from modules.notifications.events import ALL_EVENT_KEYS

    ek = (event_key or "").strip()
    for k, lab in ALL_EVENT_KEYS:
        if k == ek:
            return lab
    return ek or "—"


def list_selectable_events() -> list[tuple[str, str]]:
    from modules.notifications.events import ALL_EVENT_KEYS

    return list(ALL_EVENT_KEYS)


def hub_whatsapp_forward_phone(db: Session) -> str:
    phone = (get_setting(db, SETTING_PHONE, "") or "").strip()
    if phone:
        parsed = _parse_phones(phone)
        return parsed[0] if parsed else phone
    for gid, _ in WA_GROUPS:
        phones = _parse_phones(get_setting(db, group_phones_setting_key(gid), "") or "")
        if phones:
            return phones[0]
    return (get_setting(db, "messaging_admin_phone", "") or "").strip()


def resolve_hub_group(
    *,
    event_key: str | None = None,
    section_key: str | None = None,
) -> str:
    ek = (event_key or "").strip().lower()
    if ek:
        from modules.dashboard_notify.activity_hub import EVENT_GROUP

        for prefix, gid in EVENT_GROUP.items():
            if ek.startswith(prefix):
                return gid
        return "events"

    sk = (section_key or "").strip()
    if sk:
        from modules.dashboard_notify.activity_hub import SECTION_META

        meta = SECTION_META.get(sk)
        if meta:
            return meta[2]
        if sk in {g for g, _ in WA_GROUPS}:
            return sk
    return "events"


def _get_override_row(db: Session, event_key: str):
    from modules.dashboard_notify.models import ActivityHubWaOverride

    ek = (event_key or "").strip()[:64]
    if not ek:
        return None
    return db.get(ActivityHubWaOverride, ek)


def resolve_hub_forward_phones(
    db: Session,
    *,
    event_key: str | None = None,
    section_key: str | None = None,
) -> list[str]:
    ek = (event_key or "").strip()
    if ek:
        row = _get_override_row(db, ek)
        if row is not None:
            phones = _parse_phones(row.phones)
            if phones:
                return phones
        # توافق: إعداد قديم لكل حدث
        legacy_event = _parse_phones(
            get_setting(db, event_phones_setting_key(ek), "") or ""
        )
        if legacy_event:
            return legacy_event

    group = resolve_hub_group(event_key=ek or None, section_key=section_key)
    group_phones = _parse_phones(
        get_setting(db, group_phones_setting_key(group), "") or ""
    )
    if group_phones:
        return group_phones

    legacy = _parse_phones(get_setting(db, SETTING_PHONE, "") or "")
    if legacy:
        return legacy
    admin = (get_setting(db, "messaging_admin_phone", "") or "").strip()
    return [admin] if admin else []


def resolve_hub_message_body(
    db: Session,
    *,
    event_key: str | None,
    title: str,
    detail: str,
) -> str:
    template = DEFAULT_EVENT_MESSAGE
    ek = (event_key or "").strip()
    if ek:
        row = _get_override_row(db, ek)
        if row is not None and (row.message_body or "").strip():
            template = (row.message_body or "").strip()

    store = (get_setting(db, "store_name_ar", "") or get_setting(db, "brand_name", "") or "").strip()
    title_s = (title or "").strip() or "إشعار نظام"
    detail_s = (detail or "").strip()
    body = template
    for key, val in (
        ("{title}", title_s),
        ("{detail}", detail_s),
        ("{event_key}", ek or ""),
        ("{store_name}", store or "المنشأة"),
        ("{label}", event_label(ek) if ek else title_s),
    ):
        body = body.replace(key, val)
    # تنظيف أسطر فارغة زائدة إن كان detail فارغاً
    lines = [ln.rstrip() for ln in body.splitlines()]
    cleaned: list[str] = []
    prev_blank = False
    for ln in lines:
        blank = not ln.strip()
        if blank and prev_blank:
            continue
        cleaned.append(ln)
        prev_blank = blank
    return "\n".join(cleaned).strip()


def load_wa_group_phones(db: Session) -> dict[str, str]:
    return {
        gid: (get_setting(db, group_phones_setting_key(gid), "") or "").strip()
        for gid, _ in WA_GROUPS
    }


def list_wa_event_overrides(db: Session) -> list[WaEventOverrideView]:
    from modules.dashboard_notify.models import ActivityHubWaOverride

    try:
        rows = list(
            db.scalars(
                select(ActivityHubWaOverride).order_by(ActivityHubWaOverride.event_key)
            ).all()
        )
    except Exception:  # noqa: BLE001
        log.debug("activity_hub_wa_overrides table missing?", exc_info=True)
        return []

    out: list[WaEventOverrideView] = []
    for row in rows:
        out.append(
            WaEventOverrideView(
                event_key=row.event_key,
                label_ar=event_label(row.event_key),
                phones=(row.phones or "").strip(),
                message_body=(row.message_body or "").strip() or DEFAULT_EVENT_MESSAGE,
            )
        )
    return out


def upsert_wa_event_override(
    db: Session,
    *,
    event_key: str,
    phones: str,
    message_body: str,
) -> str | None:
    """يحفظ تخصيص إشعار. يرجع رسالة خطأ أو None عند النجاح."""
    from modules.dashboard_notify.models import ActivityHubWaOverride
    from modules.notifications.events import ALL_EVENT_KEYS

    ek = (event_key or "").strip()[:64]
    if not ek:
        return "اختر نوع الإشعار."
    allowed = {k for k, _ in ALL_EVENT_KEYS}
    if ek not in allowed:
        return "نوع الإشعار غير معروف."
    phone_list = _parse_phones(phones)
    if not phone_list:
        return "أدخل رقم واتساب واحداً على الأقل."
    msg = (message_body or "").strip()
    if not msg:
        msg = DEFAULT_EVENT_MESSAGE
    if len(msg) > 4000:
        msg = msg[:4000]

    row = db.get(ActivityHubWaOverride, ek)
    now = datetime.now(timezone.utc)
    if row is None:
        db.add(
            ActivityHubWaOverride(
                event_key=ek,
                phones=", ".join(phone_list)[:255],
                message_body=msg,
                updated_at=now,
            )
        )
    else:
        row.phones = ", ".join(phone_list)[:255]
        row.message_body = msg
        row.updated_at = now
    db.flush()
    return None


def delete_wa_event_override(db: Session, event_key: str) -> None:
    from modules.dashboard_notify.models import ActivityHubWaOverride

    ek = (event_key or "").strip()[:64]
    if not ek:
        return
    row = db.get(ActivityHubWaOverride, ek)
    if row is not None:
        db.delete(row)
        db.flush()


def save_wa_group_settings(
    db: Session,
    *,
    enabled: bool,
    group_phones: dict[str, str],
    legacy_phone: str = "",
) -> None:
    set_setting(db, SETTING_ENABLED, "1" if enabled else "0")
    phones = _parse_phones(legacy_phone)
    set_setting(db, SETTING_PHONE, ", ".join(phones)[:255] if phones else "")
    allowed_groups = {g for g, _ in WA_GROUPS}
    for gid, raw in (group_phones or {}).items():
        if gid not in allowed_groups:
            continue
        phones = _parse_phones(raw)
        set_setting(db, group_phones_setting_key(gid), ", ".join(phones)[:255])


# توافق مع الاستدعاءات القديمة
def save_wa_forward_settings(
    db: Session,
    *,
    enabled: bool,
    group_phones: dict[str, str],
    event_phones: dict[str, str] | None = None,
    legacy_phone: str = "",
) -> None:
    save_wa_group_settings(
        db, enabled=enabled, group_phones=group_phones, legacy_phone=legacy_phone
    )
    # ترحيل إعدادات الأحداث القديمة إلى الجدول إن وُجدت
    for ek, raw in (event_phones or {}).items():
        if not (raw or "").strip():
            continue
        upsert_wa_event_override(
            db, event_key=ek, phones=raw, message_body=DEFAULT_EVENT_MESSAGE
        )


def forward_hub_item_to_whatsapp(
    db: Session,
    *,
    title: str,
    detail: str = "",
    event_type: str = "activity_hub",
    meta: dict[str, Any] | None = None,
) -> None:
    if not hub_whatsapp_forward_enabled(db):
        return
    try:
        from modules.messaging.service import messaging_enabled

        if not messaging_enabled(db):
            return
    except Exception:  # noqa: BLE001
        return

    meta_d = dict(meta or {})
    event_key = str(meta_d.get("event_key") or "").strip() or None
    section_key = str(meta_d.get("section_key") or "").strip() or None
    phones = resolve_hub_forward_phones(
        db, event_key=event_key, section_key=section_key
    )
    if not phones:
        return

    title_s = (title or "").strip() or "إشعار نظام"
    detail_s = (detail or "").strip()
    body = resolve_hub_message_body(
        db, event_key=event_key, title=title_s, detail=detail_s
    )

    try:
        from modules.messaging.models import MessageChannel
        from modules.messaging.outbox import enqueue_message
        from modules.messaging.phone_utils import normalize_whatsapp_recipient

        cc = (get_setting(db, "messaging_country_code", "218") or "218").strip()
        group = resolve_hub_group(event_key=event_key, section_key=section_key)
        for phone in phones:
            norm = normalize_whatsapp_recipient(phone, country_code=cc)
            if not norm or (not norm.endswith("@g.us") and len(norm) < 8):
                continue
            payload_meta = {
                "kind": "activity_hub_forward",
                "hub_group": group,
                "to_phone": phone,
                **meta_d,
            }
            row = enqueue_message(
                db,
                body=body[:4000],
                channel=MessageChannel.WHATSAPP.value,
                phone=norm,
                event_type=(event_type or "activity_hub")[:64],
                meta=payload_meta,
            )
            db.flush()
    except Exception:  # noqa: BLE001
        log.exception("activity hub WhatsApp forward failed (%s)", title_s)
