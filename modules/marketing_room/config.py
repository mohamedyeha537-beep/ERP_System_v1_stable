"""إعدادات غرفة التسويق من البيئة وAppSetting."""
from __future__ import annotations

import os

from sqlalchemy.orm import Session

from modules.settings.service import get_setting


def marketing_n8n_webhook_url(db: Session | None = None) -> str:
    env = (os.environ.get("MARKETING_N8N_WEBHOOK_URL") or "").strip()
    if env:
        return env
    if db is not None:
        return (get_setting(db, "marketing_n8n_webhook_url", "") or "").strip()
    return ""


def marketing_ai_settings(db: Session) -> dict[str, str]:
    """مفاتيح AI — إعدادات التسويق ثم سقوط على إعدادات محادثة الويب."""
    api_key = (
        get_setting(db, "marketing_ai_api_key", "")
        or get_setting(db, "web_chat_ai_api_key", "")
        or ""
    ).strip()
    base = (
        get_setting(db, "marketing_ai_base_url", "")
        or get_setting(db, "web_chat_ai_base_url", "https://api.openai.com/v1")
        or "https://api.openai.com/v1"
    ).strip().rstrip("/")
    model = (
        get_setting(db, "marketing_ai_model", "")
        or get_setting(db, "web_chat_ai_model", "gpt-4o-mini")
        or "gpt-4o-mini"
    ).strip()
    return {"api_key": api_key, "base_url": base, "model": model}
