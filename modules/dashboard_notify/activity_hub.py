"""مركز إشعارات النظام — تجميع كل ما يحدث حسب القسم."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from modules.dashboard_notify import constants as C
from modules.dashboard_notify.hub_prefs import (
    add_mute,
    is_muted,
    item_key_activity,
    item_key_event,
    item_key_pending,
    list_mutes,
    load_item_states,
    load_mutes,
    mute_key_event_type,
    mute_key_section,
    remove_mute,
    set_item_state,
)
from modules.dashboard_notify.models import DashboardActivity
from modules.dashboard_notify.pending import (
    pending_kds_count,
    pending_messaging_chat_orders_count,
    pending_messaging_inbox_count,
    pending_printing_count,
)
from modules.dashboard_notify.service import badge_counts, mark_section_seen

_BELL_CACHE: dict[int, tuple[float, int]] = {}
_BELL_TTL_SEC = 45.0


HUB_SEEN_KEY = "activity_hub"

SECTION_META: dict[str, tuple[str, str, str]] = {
    C.POS: ("الكاشير / الجلسات", "/pos", "sales"),
    C.KDS: ("المطبخ (KDS)", "/admin/kds", "sales"),
    C.REFUNDS: ("المرتجعات", "/refunds", "sales"),
    C.INVOICE_EDIT: ("تعديل الفواتير", "/sales/invoices", "sales"),
    C.PRINTING: ("الطباعة", "/admin/printing", "sales"),
    C.TABLES: ("الطاولات", "/admin/tables", "sales"),
    C.DELIVERY: ("التوصيل", "/reports/delivery", "sales"),
    C.HOTEL_SETTLE: ("تسوية غرف الفندق", "/hotel/settle", "hotel"),
    C.HOTEL_ROOMS: ("الشقق والحجوزات", "/admin/hotel/dashboard", "hotel"),
    C.INVENTORY: ("المخزون", "/inventory", "inventory"),
    C.WAREHOUSES: ("المستودعات", "/admin/warehouses", "inventory"),
    C.PURCHASES: ("المشتريات", "/admin/purchases", "inventory"),
    C.ASSETS: ("الأصول", "/admin/assets", "inventory"),
    C.CATALOG_PRODUCTS: ("الأصناف", "/catalog/products", "inventory"),
    C.CATALOG_CATEGORIES: ("التصنيفات", "/catalog/categories", "inventory"),
    C.PAYMENT_METHODS: ("الخزينة / طرق الدفع", "/pos/treasury", "finance"),
    C.EXPENSES: ("المصروفات", "/admin/expenses", "finance"),
    C.RECURRING_COSTS: ("تكاليف متكررة", "/admin/recurring-costs", "finance"),
    C.RECEIVABLES: ("ديون العملاء", "/reports/receivables?balance_only=1", "finance"),
    C.PAYABLES: ("ذمم دائنة", "/reports/payables", "finance"),
    C.REPORTS: ("التقارير", "/reports", "finance"),
    C.HR_EMPLOYEES: ("الموظفون", "/admin/employees", "hr"),
    C.HR_ATTENDANCE: ("الحضور", "/admin/attendance", "hr"),
    C.HR_PAYROLL: ("الرواتب", "/admin/payroll", "hr"),
    C.HR_ADVANCES: ("السلف", "/admin/advances", "hr"),
    C.CUSTOMERS: ("العملاء", "/admin/customers", "customers"),
    C.LOYALTY: ("الولاء", "/admin/loyalty", "customers"),
    C.MESSAGING_INBOX: ("صندوق وارد الرسائل", "/admin/messaging/inbox", "messaging"),
    C.MESSAGING_CHAT_ORDERS: ("طلبات الشات", "/admin/messaging/chat-orders", "messaging"),
    C.ALERTS: ("التنبيهات", "/admin/alerts", "system"),
    C.SETTINGS: ("الإعدادات", "/admin/settings", "system"),
    C.BRANDING: ("الهوية", "/admin/branding", "system"),
    C.BACKUP: ("النسخ الاحتياطي", "/admin/backup", "system"),
    C.USERS: ("المستخدمون", "/admin/users", "system"),
    C.ROLES: ("الصلاحيات", "/admin/roles", "system"),
}

GROUP_ORDER: list[tuple[str, str, str]] = [
    ("hotel", "🏨 الفندق والحجوزات", "#0d9488"),
    ("sales", "🍽️ المطعم والكاشير", "#2563eb"),
    ("inventory", "📦 المخزون والمشتريات", "#d97706"),
    ("finance", "💰 المالية والخزينة", "#059669"),
    ("hr", "👥 الموارد البشرية", "#7c3aed"),
    ("customers", "🤝 العملاء والولاء", "#db2777"),
    ("messaging", "💬 الرسائل والشات", "#0284c7"),
    ("system", "⚙️ النظام والتنبيهات", "#475569"),
    ("events", "📡 أحداث المنظومة", "#64748b"),
]

EVENT_GROUP: dict[str, str] = {
    "hotel.": "hotel",
    "order.": "sales",
    "pos.": "sales",
    "kitchen.": "sales",
    "treasury.": "finance",
    "inventory.": "inventory",
    "hr.": "hr",
    "loyalty.": "customers",
    "customer.": "customers",
    "referral.": "customers",
}


@dataclass
class ActivityItem:
    item_key: str
    title: str
    detail: str
    at: datetime | None
    href: str
    is_new: bool = True
    kind: str = "activity"
    mute_key: str = ""
    mute_label: str = ""


@dataclass
class ActivitySection:
    key: str
    label_ar: str
    accent: str
    count: int = 0
    items: list[ActivityItem] = field(default_factory=list)


def _aware(dt: datetime | None) -> datetime | None:
    """وحّد التوقيت لتجنب TypeError عند المقارنة/الترتيب."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _hub_last_seen(db: Session, user_id: int) -> datetime | None:
    from modules.dashboard_notify.models import DashboardSectionSeen

    row = db.get(DashboardSectionSeen, {"user_id": user_id, "section_key": HUB_SEEN_KEY})
    return _aware(row.last_seen_at) if row else None


def _event_label(event_key: str) -> str:
    from modules.notifications.events import ALL_EVENT_KEYS

    for k, label in ALL_EVENT_KEYS:
        if k == event_key:
            return label
    return event_key


def _group_for_event(event_key: str) -> str:
    for prefix, group in EVENT_GROUP.items():
        if event_key.startswith(prefix):
            return group
    return "events"


def _activity_href(section_key: str, ref_id: int | None) -> str:
    meta = SECTION_META.get(section_key)
    base = meta[1] if meta else "/admin"
    if section_key == C.HOTEL_ROOMS and ref_id:
        return f"/admin/hotel/bookings/{ref_id}"
    if section_key in (C.PURCHASES, C.ASSETS) and ref_id:
        return f"/admin/purchases/{ref_id}"
    if section_key == C.REFUNDS and ref_id:
        return f"/refunds/{ref_id}"
    return base


def _new_engine_events_count(db: Session, user_id: int, *, mutes=None, states=None) -> int:
    from modules.notifications.models import NotificationEvent

    mutes = mutes if mutes is not None else load_mutes(db, user_id)
    states = states if states is not None else load_item_states(db, user_id)
    seen = _hub_last_seen(db, user_id)
    since = seen or (datetime.now(timezone.utc) - timedelta(hours=24))
    events = db.scalars(
        select(NotificationEvent)
        .where(NotificationEvent.created_at > since)
        .order_by(desc(NotificationEvent.id))
        .limit(200)
    ).all()
    n = 0
    for ev in events:
        if states.get(item_key_event(ev.id)) in ("read", "deleted"):
            continue
        group_id = _group_for_event(ev.event_key)
        if is_muted(mutes, event_key=ev.event_key, group_id=group_id):
            continue
        n += 1
    return n


def invalidate_bell_cache(user_id: int | None = None) -> None:
    if user_id is None:
        _BELL_CACHE.clear()
    else:
        _BELL_CACHE.pop(int(user_id), None)


def bell_unread_count(db: Session, user_id: int, *, force: bool = False) -> int:
    """عداد خفيف للجرس — بدون مسح المخزون المنخفض ولا مزامنة كاملة."""
    now_mono = time.monotonic()
    if not force:
        hit = _BELL_CACHE.get(int(user_id))
        if hit is not None and (now_mono - hit[0]) < _BELL_TTL_SEC:
            return hit[1]

    mutes = load_mutes(db, user_id)
    states = load_item_states(db, user_id)
    total = 0

    cheap_pending = (
        (C.KDS, pending_kds_count),
        (C.PRINTING, pending_printing_count),
        (C.MESSAGING_INBOX, pending_messaging_inbox_count),
        (C.MESSAGING_CHAT_ORDERS, pending_messaging_chat_orders_count),
    )
    for section_key, counter in cheap_pending:
        meta = SECTION_META.get(section_key)
        group_id = meta[2] if meta else "system"
        if is_muted(mutes, section_key=section_key, group_id=group_id):
            continue
        if states.get(item_key_pending(section_key)) in ("read", "deleted"):
            continue
        try:
            total += int(counter(db) or 0)
        except Exception:
            pass

    hub_seen = _hub_last_seen(db, user_id)
    since = hub_seen or (datetime.now(timezone.utc) - timedelta(days=3))
    acts = db.scalars(
        select(DashboardActivity)
        .where(
            DashboardActivity.resolved_at.is_(None),
            DashboardActivity.created_at >= since - timedelta(days=7),
        )
        .order_by(desc(DashboardActivity.id))
        .limit(80)
    ).all()
    for act in acts:
        ik = item_key_activity(act.id)
        if states.get(ik) in ("deleted", "read"):
            continue
        meta = SECTION_META.get(act.section_key)
        group_id = meta[2] if meta else "system"
        if is_muted(mutes, section_key=act.section_key, group_id=group_id):
            continue
        total += 1

    try:
        from modules.notifications.models import NotificationEvent

        ev_since = hub_seen or (datetime.now(timezone.utc) - timedelta(hours=12))
        n_events = db.scalar(
            select(func.count(NotificationEvent.id)).where(
                NotificationEvent.created_at > ev_since
            )
        )
        # سقف تقريبي: لا نحمّل مئات الصفوف لفلترة الكتم هنا
        total += min(int(n_events or 0), 40)
    except Exception:
        pass

    total = int(total)
    _BELL_CACHE[int(user_id)] = (now_mono, total)
    return total


def unread_total(db: Session, user_id: int) -> int:
    """للتوافق — الجرس والـ API يستخدمان المسار الخفيف."""
    return bell_unread_count(db, user_id)


def mark_hub_seen(db: Session, user_id: int) -> None:
    mark_section_seen(db, user_id, HUB_SEEN_KEY)


def mark_all_read(db: Session, user_id: int) -> int:
    sections = build_activity_hub(db, user_id)
    n = 0
    for sec in sections:
        for item in sec.items:
            if item.is_new:
                set_item_state(db, user_id, item.item_key, "read")
                n += 1
    mark_hub_seen(db, user_id)
    invalidate_bell_cache(user_id)
    return n


def apply_item_action(
    db: Session,
    user_id: int,
    *,
    item_key: str,
    action: str,
    mute_key: str = "",
    mute_label: str = "",
) -> str:
    key = (item_key or "").strip()
    action = (action or "").strip().lower()
    if action == "read":
        if not key:
            return "عنصر غير صالح"
        set_item_state(db, user_id, key, "read")
        invalidate_bell_cache(user_id)
        return "تم تعليم الإشعار كمقروء مع الاحتفاظ به"
    if action == "delete":
        if not key:
            return "عنصر غير صالح"
        set_item_state(db, user_id, key, "deleted")
        if key.startswith("activity:"):
            try:
                aid = int(key.split(":", 1)[1])
                act = db.get(DashboardActivity, aid)
                if act is not None and act.resolved_at is None:
                    act.resolved_at = datetime.now(timezone.utc)
                    db.flush()
            except ValueError:
                pass
        invalidate_bell_cache(user_id)
        return "تم حذف الإشعار"
    if action == "mute":
        mk = (mute_key or "").strip()
        if not mk:
            return "نوع الكتم غير صالح"
        add_mute(db, user_id, mk, mute_label or mk)
        if key:
            set_item_state(db, user_id, key, "deleted")
        invalidate_bell_cache(user_id)
        return "تم إيقاف هذا النوع — لن تتلقى إشعارات مماثلة"
    if action == "unmute":
        mk = (mute_key or "").strip()
        if not mk:
            return "نوع الكتم غير صالح"
        remove_mute(db, user_id, mk)
        invalidate_bell_cache(user_id)
        return "تم إعادة تفعيل هذا النوع من الإشعارات"
    return "إجراء غير معروف"


def build_activity_hub(
    db: Session,
    user_id: int,
    *,
    limit_per_section: int = 40,
) -> list[ActivitySection]:
    badges = badge_counts(db, user_id)
    hub_seen = _hub_last_seen(db, user_id)
    now = datetime.now(timezone.utc)
    since = hub_seen or (now - timedelta(days=7))
    mutes = load_mutes(db, user_id)
    states = load_item_states(db, user_id)

    sections: dict[str, ActivitySection] = {
        gid: ActivitySection(key=gid, label_ar=label, accent=accent)
        for gid, label, accent in GROUP_ORDER
    }

    for section_key, n in badges.items():
        if n <= 0:
            continue
        meta = SECTION_META.get(section_key)
        if not meta:
            group_id = "system"
            label, href = section_key, "/admin"
        else:
            label, href, group_id = meta
        if is_muted(mutes, section_key=section_key, group_id=group_id):
            continue
        ik = item_key_pending(section_key)
        st = states.get(ik)
        if st == "deleted":
            continue
        is_new = st != "read"
        sec = sections.get(group_id) or sections["system"]
        sec.items.append(
            ActivityItem(
                item_key=ik,
                title=f"{label} — {n} بانتظار المتابعة",
                detail="مهمة معلّقة على لوحة التحكم",
                at=now,
                href=href,
                is_new=is_new,
                kind="pending",
                mute_key=mute_key_section(section_key),
                mute_label=f"كتم تنبيهات: {label}",
            )
        )
        if is_new:
            sec.count += int(n)

    acts = db.scalars(
        select(DashboardActivity)
        .where(DashboardActivity.created_at >= since - timedelta(days=14))
        .order_by(desc(DashboardActivity.id))
        .limit(200)
    ).all()
    for act in acts:
        meta = SECTION_META.get(act.section_key)
        group_id = meta[2] if meta else "system"
        label = meta[0] if meta else act.section_key
        if is_muted(mutes, section_key=act.section_key, group_id=group_id):
            continue
        ik = item_key_activity(act.id)
        st = states.get(ik)
        if st == "deleted":
            continue
        sec = sections.get(group_id) or sections["system"]
        if len([i for i in sec.items if i.kind == "activity"]) >= limit_per_section:
            continue
        status = "مُنجَز" if act.resolved_at else "قيد المتابعة"
        note = (act.note or "").strip() or act.event_type
        created = _aware(act.created_at)
        if st == "read":
            is_new = False
        else:
            is_new = act.resolved_at is None and (
                hub_seen is None or (created is not None and created > hub_seen)
            )
        sec.items.append(
            ActivityItem(
                item_key=ik,
                title=f"{label}: {note}",
                detail=status + (f" · مرجع #{act.ref_id}" if act.ref_id else ""),
                at=created,
                href=_activity_href(act.section_key, act.ref_id),
                is_new=is_new,
                kind="activity",
                mute_key=mute_key_section(act.section_key),
                mute_label=f"كتم تنبيهات: {label}",
            )
        )
        if is_new:
            sec.count += 1

    try:
        from modules.notifications.models import NotificationEvent

        events = db.scalars(
            select(NotificationEvent)
            .where(NotificationEvent.created_at >= since - timedelta(days=3))
            .order_by(desc(NotificationEvent.id))
            .limit(150)
        ).all()
        for ev in events:
            group_id = _group_for_event(ev.event_key)
            if is_muted(mutes, event_key=ev.event_key, group_id=group_id):
                continue
            ik = item_key_event(ev.id)
            st = states.get(ik)
            if st == "deleted":
                continue
            sec = sections.get(group_id) or sections["events"]
            if len([i for i in sec.items if i.kind == "event"]) >= limit_per_section:
                continue
            created = _aware(ev.created_at)
            if st == "read":
                is_new = False
            else:
                is_new = hub_seen is None or (
                    created is not None and created > hub_seen
                )
            payload_hint = ""
            try:
                import json

                payload = json.loads(ev.payload_json or "{}")
                if isinstance(payload, dict):
                    for k in ("room_number", "booking_ref", "sale_id", "product_name", "guest_name"):
                        if payload.get(k) is not None:
                            payload_hint = f"{k}={payload[k]}"
                            break
            except Exception:
                pass
            href = "/admin/notifications/logs"
            if ev.event_key.startswith("hotel."):
                href = "/admin/hotel/bookings"
            elif ev.event_key.startswith("order.") or ev.event_key.startswith("pos."):
                href = "/pos"
            elif ev.event_key.startswith("inventory."):
                href = "/inventory"
            label = _event_label(ev.event_key)
            sec.items.append(
                ActivityItem(
                    item_key=ik,
                    title=label,
                    detail=f"حالة: {ev.status}"
                    + (f" · {payload_hint}" if payload_hint else ""),
                    at=created,
                    href=href,
                    is_new=is_new,
                    kind="event",
                    mute_key=mute_key_event_type(ev.event_key),
                    mute_label=f"كتم: {label}",
                )
            )
            if is_new:
                sec.count += 1
    except Exception:
        pass

    out: list[ActivitySection] = []
    for gid, _label, _accent in GROUP_ORDER:
        sec = sections[gid]
        if not sec.items:
            continue
        sec.items.sort(
            key=lambda i: _aware(i.at) or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        new_n = sum(1 for i in sec.items if i.is_new)
        sec.count = max(sec.count, new_n)
        out.append(sec)
    return out


def muted_list_for_user(db: Session, user_id: int):
    return list_mutes(db, user_id)
