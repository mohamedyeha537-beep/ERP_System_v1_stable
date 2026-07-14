"""تصدير/استيراد قوالب محرك الإشعارات JSON — مع دعم استيراد قوالب المراسلات القديمة."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.notifications.models import NotificationTemplate

EXPORT_VERSION = 1

# ترجمة رموز قوالب المراسلات القديمة → أحداث الإشعارات
_LEGACY_CODE_TO_EVENT: dict[str, str] = {
    "loyalty.earned": "loyalty.points_earned",
    "loyalty.redeemed": "loyalty.points_redeemed",
    "loyalty.adjusted": "loyalty.points_adjusted",
    "stock.low": "inventory.low_stock",
    "product.expiry": "inventory.expiry_warning",
    "consent.welcome": "customer.created",
    "campaign.generic": "marketing.broadcast",
    "campaign.manual": "marketing.manual",
}


@dataclass
class TemplateImportResult:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list[str] | None = None


def export_notification_templates_json(db: Session) -> str:
    rows = list(
        db.scalars(
            select(NotificationTemplate).order_by(
                NotificationTemplate.event_key, NotificationTemplate.id
            )
        ).all()
    )
    payload = {
        "version": EXPORT_VERSION,
        "kind": "notification_templates",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "templates": [
            {
                "name": row.name,
                "event_key": row.event_key,
                "channel": row.channel,
                "recipient_type": row.recipient_type,
                "message_type": row.message_type,
                "title": row.title,
                "body_template": row.body_template,
                "buttons_json": row.buttons_json,
                "image_url": row.image_url,
                "document_url": row.document_url,
                "language": row.language,
                "is_active": bool(row.is_active),
            }
            for row in rows
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _normalize_item(item: dict) -> dict | None:
    if not isinstance(item, dict):
        return None
    # صيغة محرك الإشعارات
    if item.get("event_key") and item.get("body_template") is not None:
        return {
            "name": (item.get("name") or "").strip()[:160],
            "event_key": (item.get("event_key") or "").strip()[:64],
            "channel": (item.get("channel") or "whatsapp").strip()[:32],
            "recipient_type": (item.get("recipient_type") or "customer").strip()[:32],
            "message_type": (item.get("message_type") or "text").strip()[:20],
            "title": (item.get("title") or "").strip()[:160] or None,
            "body_template": str(item.get("body_template") or ""),
            "buttons_json": item.get("buttons_json"),
            "image_url": item.get("image_url"),
            "document_url": item.get("document_url"),
            "language": (item.get("language") or "ar").strip()[:8],
            "is_active": item.get("is_active", True),
        }
    # صيغة بوت المراسلات القديم (code + body_text)
    code = (item.get("code") or "").strip()
    body = item.get("body_text")
    if code and body is not None:
        event_key = _LEGACY_CODE_TO_EVENT.get(code, code.replace(".", "_"))
        return {
            "name": (item.get("name") or code).strip()[:160],
            "event_key": event_key[:64],
            "channel": "whatsapp",
            "recipient_type": "customer",
            "message_type": "text",
            "title": None,
            "body_template": str(body),
            "buttons_json": None,
            "image_url": None,
            "document_url": None,
            "language": "ar",
            "is_active": item.get("is_active", True),
        }
    return None


def _parse_payload(raw: str) -> list[dict]:
    data = json.loads(raw)
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("templates") or []
    else:
        raise ValueError("صيغة JSON غير صالحة.")
    if not isinstance(items, list):
        raise ValueError("قائمة القوالب غير صالحة.")
    return items


def _match_existing(
    db: Session, event_key: str, recipient_type: str, name: str
) -> NotificationTemplate | None:
    row = db.scalar(
        select(NotificationTemplate).where(
            NotificationTemplate.event_key == event_key,
            NotificationTemplate.recipient_type == recipient_type,
            NotificationTemplate.name == name,
        )
    )
    if row is not None:
        return row
    return db.scalar(
        select(NotificationTemplate).where(
            NotificationTemplate.event_key == event_key,
            NotificationTemplate.recipient_type == recipient_type,
        ).limit(1)
    )


def import_notification_templates_json(db: Session, raw: str) -> TemplateImportResult:
    result = TemplateImportResult(errors=[])
    items = _parse_payload(raw)
    if not items:
        result.errors.append("الملف لا يحتوي على قوالب.")
        return result

    for idx, raw_item in enumerate(items, start=1):
        norm = _normalize_item(raw_item)
        if norm is None:
            result.skipped += 1
            result.errors.append(f"سطر {idx}: بيانات غير مكتملة.")
            continue
        if not norm["name"] or not norm["event_key"] or not norm["body_template"].strip():
            result.skipped += 1
            result.errors.append(f"سطر {idx}: name/event_key/body مطلوب.")
            continue

        is_active = norm["is_active"]
        if isinstance(is_active, str):
            is_active = is_active.strip().lower() in ("1", "true", "yes", "on", "نعم")

        buttons = norm["buttons_json"]
        if buttons is not None and not isinstance(buttons, str):
            buttons = json.dumps(buttons, ensure_ascii=False)

        row = _match_existing(
            db, norm["event_key"], norm["recipient_type"], norm["name"]
        )
        if row is None:
            db.add(
                NotificationTemplate(
                    name=norm["name"],
                    event_key=norm["event_key"],
                    channel=norm["channel"],
                    recipient_type=norm["recipient_type"],
                    message_type=norm["message_type"],
                    title=norm["title"],
                    body_template=norm["body_template"],
                    buttons_json=buttons,
                    image_url=norm["image_url"],
                    document_url=norm["document_url"],
                    language=norm["language"],
                    is_active=bool(is_active),
                )
            )
            result.created += 1
        else:
            row.name = norm["name"]
            row.channel = norm["channel"]
            row.message_type = norm["message_type"]
            row.title = norm["title"]
            row.body_template = norm["body_template"]
            if buttons:
                row.buttons_json = buttons
            if norm["image_url"]:
                row.image_url = norm["image_url"]
            if norm["document_url"]:
                row.document_url = norm["document_url"]
            row.language = norm["language"]
            row.is_active = bool(is_active)
            result.updated += 1

    db.flush()
    if not result.errors:
        result.errors = None
    return result
