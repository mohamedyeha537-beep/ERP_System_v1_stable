"""توجيه إشعارات النظام إلى أرقام واتساب حسب الحدث أو مجموعة أحداث."""
from __future__ import annotations

import fnmatch
import json
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.notifications.models import NotificationRoute
from modules.settings.service import get_setting

__all__ = [
    "parse_phone_list",
    "parse_event_patterns",
    "event_matches_pattern",
    "resolve_routed_phones",
    "list_active_routes",
]

_SYSTEM_RECIPIENT_TYPES = frozenset(
    {"admin", "supervisor", "inventory_manager", "hr_manager", "treasury_clerk"}
)


def parse_phone_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    out: list[str] = []
    for part in raw.replace(";", ",").split(","):
        p = part.strip()
        if p and p not in out:
            out.append(p)
    return out


def parse_event_patterns(raw: str | None) -> list[str]:
    text = (raw or "").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, list):
            return [str(x).strip() for x in data if str(x).strip()]
    return [p.strip() for p in text.replace("\n", ",").split(",") if p.strip()]


def event_matches_pattern(event_key: str, pattern: str) -> bool:
    key = (event_key or "").strip()
    pat = (pattern or "").strip()
    if not key or not pat:
        return False
    if pat == "*":
        return True
    if "*" in pat or "?" in pat:
        return fnmatch.fnmatch(key, pat)
    return key == pat or key.startswith(pat.rstrip(".") + ".")


def _scope_matches(scope: str, recipient_type: str) -> bool:
    s = (scope or "*").strip().lower() or "*"
    rt = (recipient_type or "").strip().lower()
    if s in ("*", "any", "all"):
        return True
    return s == rt


def list_active_routes(db: Session) -> list[NotificationRoute]:
    return list(
        db.scalars(
            select(NotificationRoute)
            .where(NotificationRoute.is_active.is_(True))
            .order_by(NotificationRoute.sort_order.asc(), NotificationRoute.id.asc())
        ).all()
    )


def resolve_routed_phones(
    db: Session,
    *,
    event_key: str,
    recipient_type: str,
) -> list[str]:
    """أرقام من مسارات مفعّلة تطابق الحدث ونوع المستلم."""
    rt = (recipient_type or "").strip().lower()
    if rt not in _SYSTEM_RECIPIENT_TYPES:
        return []
    phones: list[str] = []
    for route in list_active_routes(db):
        if not _scope_matches(route.recipient_scope, rt):
            continue
        patterns = parse_event_patterns(route.event_patterns_json)
        if not any(event_matches_pattern(event_key, p) for p in patterns):
            continue
        for p in parse_phone_list(route.phones):
            if p not in phones:
                phones.append(p)
    return phones


def fallback_system_phone(db: Session, recipient_type: str) -> str | None:
    rt = (recipient_type or "").strip().lower()
    if rt == "inventory_manager":
        raw = (get_setting(db, "notification_inventory_phones") or "").strip()
        phones = parse_phone_list(raw)
        if phones:
            return phones[0]
    if rt == "hr_manager":
        raw = (get_setting(db, "notification_hr_phones") or "").strip()
        phones = parse_phone_list(raw)
        if phones:
            return phones[0]
    if rt == "supervisor":
        raw = (get_setting(db, "notification_supervisor_phones") or "").strip()
        phones = parse_phone_list(raw)
        if phones:
            return phones[0]
    if rt == "treasury_clerk":
        raw = (get_setting(db, "notification_treasury_phones") or "").strip()
        phones = parse_phone_list(raw)
        if phones:
            return phones[0]
        return None
    return (get_setting(db, "messaging_admin_phone") or "").strip() or None


def resolve_system_phones(
    db: Session,
    *,
    event_key: str,
    recipient_type: str,
) -> list[str]:
    """مسارات مخصّصة ثم الرقم الاحتياطي لنوع المستلم."""
    routed = resolve_routed_phones(db, event_key=event_key, recipient_type=recipient_type)
    if routed:
        return routed
    fb = fallback_system_phone(db, recipient_type)
    return [fb] if fb else []
