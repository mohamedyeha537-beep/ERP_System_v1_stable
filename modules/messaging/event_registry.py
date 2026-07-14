"""سجل أحداث المراسلات — إنشاء واستعلام وربط نقاط التشغيل."""
from __future__ import annotations

import re

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from modules.messaging.hooks import DEFAULT_HOOK_EVENTS, SYSTEM_HOOKS, hook_label_ar
from modules.messaging.models import MessageEventCategory, MessageEventDefinition, MessageEventHook

_EVENT_CODE_RE = re.compile(r"^[a-z][a-z0-9._-]{2,63}$")


class EventRegistryError(Exception):
    pass


def normalize_event_code(raw: str) -> str:
    code = (raw or "").strip().lower().replace(" ", ".")
    if not _EVENT_CODE_RE.match(code):
        raise EventRegistryError(
            "رمز الحدث: 3–64 حرفاً (حروف إنجليزية صغيرة، أرقام، نقطة، شرطة)."
        )
    return code


def list_automatic_event_types(db: Session) -> list[tuple[str, str]]:
    rows = list(
        db.scalars(
            select(MessageEventDefinition)
            .where(
                MessageEventDefinition.category == MessageEventCategory.AUTOMATIC.value,
                MessageEventDefinition.is_active.is_(True),
            )
            .order_by(MessageEventDefinition.is_system.desc(), MessageEventDefinition.name_ar)
        ).all()
    )
    return [(r.code, r.name_ar) for r in rows]


def event_label(db: Session, event_type: str | None) -> str:
    if not event_type:
        return "—"
    row = db.scalar(
        select(MessageEventDefinition).where(MessageEventDefinition.code == event_type)
    )
    if row is not None:
        return row.name_ar
    from modules.messaging.categories import EVENT_LABELS

    return EVENT_LABELS.get(event_type, event_type)


def resolve_hook_event_types(db: Session, hook_code: str) -> list[str]:
    rows = list(
        db.scalars(
            select(MessageEventHook)
            .where(
                MessageEventHook.hook_code == hook_code,
                MessageEventHook.is_active.is_(True),
            )
            .order_by(MessageEventHook.priority.asc(), MessageEventHook.id.asc())
        ).all()
    )
    if rows:
        out: list[str] = []
        seen: set[str] = set()
        for row in rows:
            if row.event_type in seen:
                continue
            evt = db.scalar(
                select(MessageEventDefinition).where(
                    MessageEventDefinition.code == row.event_type,
                    MessageEventDefinition.is_active.is_(True),
                )
            )
            if evt is None:
                continue
            seen.add(row.event_type)
            out.append(row.event_type)
        if out:
            return out
    return list(DEFAULT_HOOK_EVENTS.get(hook_code, []))


def _upsert_event(
    db: Session,
    *,
    code: str,
    name_ar: str,
    description: str = "",
    category: str = MessageEventCategory.AUTOMATIC.value,
    is_system: bool = False,
) -> MessageEventDefinition:
    row = db.scalar(select(MessageEventDefinition).where(MessageEventDefinition.code == code))
    if row is None:
        row = MessageEventDefinition(
            code=code,
            name_ar=name_ar.strip()[:160],
            description=(description or "").strip() or None,
            category=category,
            is_system=is_system,
            is_active=True,
        )
        db.add(row)
        db.flush()
        return row
    row.name_ar = name_ar.strip()[:160]
    if description and not (row.description or "").strip():
        row.description = description.strip()
    row.is_system = is_system or row.is_system
    row.is_active = True
    return row


def _ensure_hook_mapping(
    db: Session, *, hook_code: str, event_type: str, priority: int = 100
) -> None:
    row = db.scalar(
        select(MessageEventHook).where(
            MessageEventHook.hook_code == hook_code,
            MessageEventHook.event_type == event_type,
        )
    )
    if row is None:
        db.add(
            MessageEventHook(
                hook_code=hook_code,
                event_type=event_type,
                is_active=True,
                priority=priority,
            )
        )
    else:
        row.is_active = True


def ensure_system_events_and_hooks(db: Session) -> None:
    from modules.messaging.events import (
        CUSTOMER_FIRST_LINKED,
        LOYALTY_POINTS_ADJUSTED,
        LOYALTY_POINTS_EARNED,
        LOYALTY_POINTS_REDEEMED,
        PRODUCT_EXPIRY,
        STOCK_LOW,
    )

    system_events = [
        (LOYALTY_POINTS_EARNED, "اكتساب نقاط ولاء", "عند منح نقاط من عملية بيع"),
        (LOYALTY_POINTS_REDEEMED, "خصم نقاط ولاء", "عند استخدام النقاط في الدفع"),
        (LOYALTY_POINTS_ADJUSTED, "تعديل نقاط يدوي", "تعديل رصيد النقاط من لوحة العملاء"),
        (CUSTOMER_FIRST_LINKED, "ترحيب عميل جديد", "أول موافقة على المراسلات"),
        (STOCK_LOW, "تنبيه نقص مخزون", "تنبيه للإدارة"),
        (PRODUCT_EXPIRY, "تنبيه صلاحية منتج", "تنبيه للإدارة"),
    ]
    for code, name, desc in system_events:
        _upsert_event(db, code=code, name_ar=name, description=desc, is_system=True)
    db.flush()

    for hook_code, events in DEFAULT_HOOK_EVENTS.items():
        for i, event_type in enumerate(events):
            _ensure_hook_mapping(db, hook_code=hook_code, event_type=event_type, priority=100 + i)
    db.flush()


def create_custom_event(
    db: Session,
    *,
    code: str,
    name_ar: str,
    description: str = "",
) -> MessageEventDefinition:
    norm = normalize_event_code(code)
    if not (name_ar or "").strip():
        raise EventRegistryError("اسم الحدث مطلوب.")
    existing = db.scalar(
        select(MessageEventDefinition).where(MessageEventDefinition.code == norm)
    )
    if existing is not None:
        raise EventRegistryError("رمز الحدث مستخدم مسبقاً.")
    row = MessageEventDefinition(
        code=norm,
        name_ar=name_ar.strip()[:160],
        description=(description or "").strip() or None,
        category=MessageEventCategory.AUTOMATIC.value,
        is_system=False,
        is_active=True,
    )
    db.add(row)
    db.flush()
    return row


def update_event(
    db: Session,
    event_id: int,
    *,
    name_ar: str,
    description: str = "",
    is_active: bool,
) -> MessageEventDefinition:
    row = db.get(MessageEventDefinition, event_id)
    if row is None:
        raise EventRegistryError("الحدث غير موجود.")
    if not (name_ar or "").strip():
        raise EventRegistryError("اسم الحدث مطلوب.")
    row.name_ar = name_ar.strip()[:160]
    row.description = (description or "").strip() or None
    row.is_active = is_active
    db.flush()
    return row


def delete_custom_event(db: Session, event_id: int) -> None:
    row = db.get(MessageEventDefinition, event_id)
    if row is None:
        raise EventRegistryError("الحدث غير موجود.")
    if row.is_system:
        raise EventRegistryError("لا يمكن حذف حدث نظامي — عطّله فقط.")
    db.execute(delete(MessageEventHook).where(MessageEventHook.event_type == row.code))
    db.delete(row)


def save_hook_mappings(db: Session, hook_code: str, event_types: list[str]) -> None:
    from modules.messaging.hooks import SYSTEM_HOOK_CODES

    if hook_code not in SYSTEM_HOOK_CODES:
        raise EventRegistryError("نقطة التشغيل غير معروفة.")
    cleaned: list[str] = []
    for raw in event_types:
        code = (raw or "").strip()
        if not code or code in cleaned:
            continue
        evt = db.scalar(
            select(MessageEventDefinition).where(
                MessageEventDefinition.code == code,
                MessageEventDefinition.is_active.is_(True),
                MessageEventDefinition.category == MessageEventCategory.AUTOMATIC.value,
            )
        )
        if evt is None:
            raise EventRegistryError(f"الحدث «{code}» غير موجود أو غير نشط.")
        cleaned.append(code)
    db.execute(delete(MessageEventHook).where(MessageEventHook.hook_code == hook_code))
    for i, code in enumerate(cleaned):
        db.add(
            MessageEventHook(
                hook_code=hook_code,
                event_type=code,
                is_active=True,
                priority=100 + i,
            )
        )
    db.flush()


def hooks_with_mappings(db: Session) -> list[dict]:
    rows = list(
        db.scalars(
            select(MessageEventHook)
            .where(MessageEventHook.is_active.is_(True))
            .order_by(MessageEventHook.hook_code, MessageEventHook.priority)
        ).all()
    )
    by_hook: dict[str, list[str]] = {}
    for row in rows:
        by_hook.setdefault(row.hook_code, []).append(row.event_type)
    out: list[dict] = []
    for hook_code, label, aud in SYSTEM_HOOKS:
        mapped = by_hook.get(hook_code) or DEFAULT_HOOK_EVENTS.get(hook_code, [])
        out.append(
            {
                "hook_code": hook_code,
                "label_ar": label,
                "audience": aud,
                "event_types": mapped,
            }
        )
    return out
