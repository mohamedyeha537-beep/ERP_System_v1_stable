"""تفعيل/إيقاف مجموعات الإشعارات (الطلبات، المخزون، الموظفين، …)."""
from __future__ import annotations

import json

from sqlalchemy.orm import Session

from modules.settings.service import get_setting, set_setting

_SETTING_KEY = "notification_modules_disabled"

NOTIFICATION_MODULES: list[tuple[str, str, tuple[str, ...]]] = [
    ("orders", "الطلبات والتوصيل", ("order.",)),
    ("pos", "نقطة البيع والجلسات", ("pos.",)),
    ("kitchen", "المطبخ", ("kitchen.",)),
    ("inventory", "المخزون والمشتريات", ("inventory.",)),
    ("treasury", "الخزينة والأرصدة", ("treasury.",)),
    ("hotel", "الفندق والحجوزات", ("hotel.",)),
    ("loyalty", "الولاء والإحالات", ("loyalty.", "referral.")),
    ("customers", "العملاء", ("customer.",)),
    ("hr", "الموظفين (حضور/رواتب/سلف)", ("hr.",)),
]


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
    return set()


def _save_disabled(db: Session, disabled: set[str]) -> None:
    set_setting(db, _SETTING_KEY, json.dumps(sorted(disabled), ensure_ascii=False))


def module_for_event_key(event_key: str) -> str | None:
    key = (event_key or "").strip()
    for mod_key, _label, prefixes in NOTIFICATION_MODULES:
        if any(key.startswith(p) for p in prefixes):
            return mod_key
    return None


def is_notification_module_enabled(db: Session, module_key: str) -> bool:
    key = (module_key or "").strip()
    if not key:
        return True
    return key not in _load_disabled(db)


def is_event_module_enabled(db: Session, event_key: str) -> bool:
    mod = module_for_event_key(event_key)
    if mod is None:
        return True
    return is_notification_module_enabled(db, mod)


def module_enabled_map(db: Session) -> dict[str, bool]:
    disabled = _load_disabled(db)
    return {k: k not in disabled for k, _l, _p in NOTIFICATION_MODULES}


def toggle_notification_module(db: Session, module_key: str) -> bool:
    key = (module_key or "").strip()
    valid = {k for k, _l, _p in NOTIFICATION_MODULES}
    if key not in valid:
        return True
    disabled = _load_disabled(db)
    if key in disabled:
        disabled.discard(key)
        enabled = True
    else:
        disabled.add(key)
        enabled = False
    _save_disabled(db, disabled)
    return enabled


def set_notification_module_enabled(db: Session, module_key: str, *, enabled: bool) -> bool:
    key = (module_key or "").strip()
    disabled = _load_disabled(db)
    if enabled:
        disabled.discard(key)
    else:
        disabled.add(key)
    _save_disabled(db, disabled)
    return enabled
