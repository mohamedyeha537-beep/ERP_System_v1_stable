"""إعدادات رسالة الترحيب بعد التسكين — رقم الاستقبال + نص القالب."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.notifications.events import HOTEL_CHECK_IN_WELCOME
from modules.notifications.models import NotificationTemplate
from modules.notifications.seed import (
    HOTEL_CHECK_IN_WELCOME_BODY,
    HOTEL_CHECK_IN_WELCOME_TITLE,
)
from modules.settings.service import get_setting


WELCOME_TEMPLATE_NAME = HOTEL_CHECK_IN_WELCOME_TITLE


def get_reception_phone(db: Session) -> str:
    phone = (get_setting(db, "hotel_reception_phone") or "").strip()
    if phone:
        return phone
    return (get_setting(db, "hotel_online_staff_phone") or "").strip()


def get_check_in_welcome_template(db: Session) -> NotificationTemplate | None:
    return db.scalar(
        select(NotificationTemplate)
        .where(
            NotificationTemplate.event_key == HOTEL_CHECK_IN_WELCOME,
            NotificationTemplate.recipient_type == "customer",
        )
        .order_by(NotificationTemplate.id.asc())
        .limit(1)
    )


def welcome_settings_context(db: Session) -> dict:
    from modules.notifications.event_settings import is_notification_event_enabled

    tpl = get_check_in_welcome_template(db)
    title = (tpl.title if tpl and tpl.title else "") or WELCOME_TEMPLATE_NAME
    body = (tpl.body_template if tpl else "") or HOTEL_CHECK_IN_WELCOME_BODY
    return {
        "hotel_checkin_welcome_enabled": is_notification_event_enabled(
            db, HOTEL_CHECK_IN_WELCOME
        ),
        "hotel_reception_phone": get_reception_phone(db),
        "hotel_checkin_welcome_title": title,
        "hotel_checkin_welcome_body": body,
        "hotel_checkin_welcome_placeholders": (
            "{guest_name} · {store_name} · {room_name} · {reception_phone} · "
            "{booking_reference} · {check_in_date} · {check_out_date}"
        ),
    }


def save_check_in_welcome_settings(
    db: Session,
    *,
    enabled: bool,
    reception_phone: str,
    title: str,
    body: str,
) -> None:
    from modules.notifications.event_settings import set_notification_event_enabled
    from modules.notifications.models import NotificationRule
    from modules.settings.service import invalidate_settings_cache, set_setting

    set_setting(db, "hotel_reception_phone", (reception_phone or "").strip()[:40])
    set_notification_event_enabled(db, HOTEL_CHECK_IN_WELCOME, enabled=enabled)

    clean_title = (title or "").strip()[:160] or WELCOME_TEMPLATE_NAME
    clean_body = (body or "").strip() or HOTEL_CHECK_IN_WELCOME_BODY

    tpl = get_check_in_welcome_template(db)
    if tpl is None:
        tpl = NotificationTemplate(
            name=WELCOME_TEMPLATE_NAME,
            event_key=HOTEL_CHECK_IN_WELCOME,
            recipient_type="customer",
            channel="whatsapp",
            message_type="text",
            title=clean_title,
            body_template=clean_body,
            is_active=True,
        )
        db.add(tpl)
        db.flush()
    else:
        tpl.title = clean_title
        tpl.body_template = clean_body
        tpl.name = WELCOME_TEMPLATE_NAME
        tpl.is_active = True
        tpl.message_type = "text"

    rule = db.scalar(
        select(NotificationRule).where(
            NotificationRule.event_key == HOTEL_CHECK_IN_WELCOME,
            NotificationRule.recipient_type == "customer",
        )
    )
    if rule is None:
        rule = NotificationRule(
            event_key=HOTEL_CHECK_IN_WELCOME,
            recipient_type="customer",
            template_id=tpl.id,
            channel="whatsapp",
            is_active=True,
            throttle_minutes=0,
        )
        db.add(rule)
    else:
        rule.template_id = tpl.id
        rule.is_active = True
        rule.throttle_minutes = 0

    invalidate_settings_cache()
