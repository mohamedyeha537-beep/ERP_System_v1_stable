"""تفعيل/إيقاف وحدات النظام — جاهز لخطط الاشتراك وmulti-vendor لاحقاً."""
from __future__ import annotations

import json

from sqlalchemy.orm import Session

from modules.settings.service import get_setting, set_setting

_SETTING_KEY = "enabled_modules"

# مفاتيح الوحدات
HOTEL_CORE = "hotel.core"
HOTEL_BOOKING = "hotel.booking"
HOTEL_PORTAL = "hotel.portal"
HOTEL_STORE = "hotel.store"
HOTEL_FINANCE = "hotel.finance"

ALL_MODULES: list[tuple[str, str]] = [
    (HOTEL_CORE, "فندق — غرف وحساب شقة (POS)"),
    (HOTEL_BOOKING, "فندق — حجز وCheck-in/out"),
    (HOTEL_PORTAL, "فندق — بوابة حجز الزبون"),
    (HOTEL_STORE, "فندق — متجر الشقق الأونلاين"),
    (HOTEL_FINANCE, "فندق — فواتير إقامة وإقفال يومي"),
]

_DEFAULT_ENABLED = {k for k, _ in ALL_MODULES}


def _load_enabled(db: Session) -> set[str]:
    raw = (get_setting(db, _SETTING_KEY) or "").strip()
    if not raw:
        return set(_DEFAULT_ENABLED)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return set(_DEFAULT_ENABLED)
    if isinstance(data, list):
        return {str(x).strip() for x in data if str(x).strip()}
    return set(_DEFAULT_ENABLED)


def _save_enabled(db: Session, enabled: set[str]) -> None:
    set_setting(db, _SETTING_KEY, json.dumps(sorted(enabled), ensure_ascii=False))


def is_module_enabled(db: Session, module_key: str) -> bool:
    key = (module_key or "").strip()
    if not key:
        return True
    return key in _load_enabled(db)


def module_enabled_map(db: Session) -> dict[str, bool]:
    enabled = _load_enabled(db)
    return {k: k in enabled for k, _ in ALL_MODULES}


def set_module_enabled(db: Session, module_key: str, *, enabled: bool) -> None:
    key = (module_key or "").strip()
    valid = {k for k, _ in ALL_MODULES}
    if key not in valid:
        return
    cur = _load_enabled(db)
    if enabled:
        cur.add(key)
    else:
        cur.discard(key)
    _save_enabled(db, cur)


# وحدات تُفعَّل تلقائياً عند الترقية إن كانت وحدات الفندق مفعّلة
_AUTO_ENABLE_ON_UPGRADE = (HOTEL_STORE,)


def ensure_default_modules(db: Session) -> None:
    """أول استخدام — تفعيل وحدات الفندق؛ وإضافة وحدات جديدة بعد الترقية."""
    raw = (get_setting(db, _SETTING_KEY) or "").strip()
    if not raw:
        _save_enabled(db, set(_DEFAULT_ENABLED))
        return
    cur = _load_enabled(db)
    if not cur.intersection({HOTEL_CORE, HOTEL_BOOKING, HOTEL_FINANCE}):
        return
    added = False
    for key in _AUTO_ENABLE_ON_UPGRADE:
        if key not in cur:
            cur.add(key)
            added = True
    if added:
        _save_enabled(db, cur)


def ensure_hotel_store_module(db: Session) -> None:
    """تفعيل متجر الشقق تلقائياً إن كانت وحدات الفندق مفعّلة."""
    cur = _load_enabled(db)
    if HOTEL_STORE in cur:
        return
    if not cur.intersection({HOTEL_CORE, HOTEL_BOOKING, HOTEL_FINANCE}):
        return
    cur.add(HOTEL_STORE)
    _save_enabled(db, cur)
