"""تفعيل/إيقاف كل حدث إشعار على حدة."""
from __future__ import annotations

import json

from sqlalchemy.orm import Session

from modules.settings.service import get_setting, set_setting

_SETTING_KEY = "notification_events_disabled"


def _load_disabled(db: Session) -> set[str]:
    raw = (get_setting(db, _SETTING_KEY) or "").strip()
    if not raw:
        return set()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return set()
    if isinstance(data, list):
        return {str(x).strip() for x in data if str(x).strip()}
    if isinstance(data, dict):
        return {k for k, v in data.items() if v is False or str(v).lower() in ("0", "false", "off")}
    return set()


def _save_disabled(db: Session, disabled: set[str]) -> None:
    set_setting(db, _SETTING_KEY, json.dumps(sorted(disabled), ensure_ascii=False))


def is_notification_event_enabled(db: Session, event_key: str) -> bool:
    key = (event_key or "").strip()
    if not key:
        return True
    return key not in _load_disabled(db)


def event_enabled_map(db: Session, event_keys: list[str]) -> dict[str, bool]:
    disabled = _load_disabled(db)
    return {k: k not in disabled for k in event_keys}


def set_notification_event_enabled(db: Session, event_key: str, *, enabled: bool) -> bool:
    """يُعيد الحالة الجديدة (True = مفعّل)."""
    key = (event_key or "").strip()
    if not key:
        return True
    disabled = _load_disabled(db)
    if enabled:
        disabled.discard(key)
    else:
        disabled.add(key)
    _save_disabled(db, disabled)
    return enabled


def toggle_notification_event(db: Session, event_key: str) -> bool:
    key = (event_key or "").strip()
    enabled = is_notification_event_enabled(db, key)
    return set_notification_event_enabled(db, key, enabled=not enabled)
