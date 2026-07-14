"""تصدير/استيراد قوالب المراسلات JSON — للمزامنة بين محلي ولايف."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.messaging.models import MessageTemplate

EXPORT_VERSION = 1


@dataclass
class TemplateImportResult:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list[str] | None = None


def export_templates_json(db: Session) -> str:
    rows = list(
        db.scalars(select(MessageTemplate).order_by(MessageTemplate.code)).all()
    )
    payload = {
        "version": EXPORT_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "templates": [
            {
                "code": row.code,
                "name": row.name,
                "body_text": row.body_text,
                "body_voice_url": row.body_voice_url,
                "is_active": bool(row.is_active),
            }
            for row in rows
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _parse_templates_payload(raw: str) -> list[dict]:
    data = json.loads(raw)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        items = data.get("templates")
        if isinstance(items, list):
            return items
    raise ValueError("صيغة JSON غير صالحة — يُتوقَّع { \"templates\": [...] }.")


def import_templates_json(db: Session, raw: str) -> TemplateImportResult:
    result = TemplateImportResult(errors=[])
    items = _parse_templates_payload(raw)
    if not items:
        result.errors.append("الملف لا يحتوي على قوالب.")
        return result

    for idx, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            result.skipped += 1
            result.errors.append(f"سطر {idx}: عنصر غير صالح.")
            continue
        code = (item.get("code") or "").strip()[:64]
        name = (item.get("name") or "").strip()[:160]
        body_text = item.get("body_text")
        if not code or not name or body_text is None:
            result.skipped += 1
            result.errors.append(f"سطر {idx}: code/name/body_text مطلوب.")
            continue

        voice = item.get("body_voice_url")
        voice = (voice or "").strip()[:500] or None
        is_active = item.get("is_active", True)
        if isinstance(is_active, str):
            is_active = is_active.strip().lower() in ("1", "true", "yes", "on", "نعم")

        row = db.scalar(
            select(MessageTemplate).where(MessageTemplate.code == code)
        )
        if row is None:
            db.add(
                MessageTemplate(
                    code=code,
                    name=name,
                    body_text=str(body_text),
                    body_voice_url=voice,
                    is_active=bool(is_active),
                )
            )
            result.created += 1
        else:
            row.name = name
            row.body_text = str(body_text)
            row.body_voice_url = voice
            row.is_active = bool(is_active)
            result.updated += 1

    db.flush()
    if not result.errors:
        result.errors = None
    return result
