"""تصنيف الرسائل: تلقائية (نظام) مقابل دعائية (حملات)."""
from __future__ import annotations

from modules.messaging.events import (
    CAMPAIGN_BROADCAST,
    CAMPAIGN_MANUAL,
    CUSTOMER_FIRST_LINKED,
    LOYALTY_POINTS_ADJUSTED,
    LOYALTY_POINTS_EARNED,
    LOYALTY_POINTS_REDEEMED,
    MESSAGING_TEST,
    MESSAGING_TEST_BUTTONS,
    PRODUCT_EXPIRY,
    STOCK_LOW,
)

# أحداث تُطلق تلقائياً من النظام — تُضبط من «القواعد» فقط
AUTOMATIC_EVENT_TYPES: list[tuple[str, str]] = [
    (LOYALTY_POINTS_EARNED, "اكتساب نقاط ولاء"),
    (LOYALTY_POINTS_REDEEMED, "خصم نقاط ولاء"),
    (LOYALTY_POINTS_ADJUSTED, "تعديل نقاط يدوي"),
    (CUSTOMER_FIRST_LINKED, "ترحيب عميل جديد"),
    (STOCK_LOW, "تنبيه نقص مخزون (إدارة)"),
    (PRODUCT_EXPIRY, "تنبيه صلاحية منتج (إدارة)"),
]

AUTOMATIC_EVENT_CODES: frozenset[str] = frozenset(c for c, _ in AUTOMATIC_EVENT_TYPES)

# أحداث الحملات — يدوية/مجدولة فقط، لا تُنشأ كقواعد
PROMOTIONAL_EVENT_TYPES: list[tuple[str, str]] = [
    (CAMPAIGN_MANUAL, "حملة / إرسال دعائي"),
    (CAMPAIGN_BROADCAST, "حملة مجدولة"),
    (MESSAGING_TEST, "اختبار إرسال"),
    (MESSAGING_TEST_BUTTONS, "اختبار أزرار"),
]

PROMOTIONAL_EVENT_CODES: frozenset[str] = frozenset(c for c, _ in PROMOTIONAL_EVENT_TYPES)

EVENT_LABELS: dict[str, str] = dict(AUTOMATIC_EVENT_TYPES + PROMOTIONAL_EVENT_TYPES)


def is_automatic_event(event_type: str | None) -> bool:
    return (event_type or "") in AUTOMATIC_EVENT_CODES


def is_promotional_event(event_type: str | None) -> bool:
    return (event_type or "") in PROMOTIONAL_EVENT_CODES


def message_kind(event_type: str | None) -> str:
    if is_promotional_event(event_type):
        return "promotional"
    return "automatic"


def kind_label_ar(kind: str) -> str:
    if kind == "promotional":
        return "دعائية"
    return "تلقائية"


def event_label_ar(event_type: str | None) -> str:
    if not event_type:
        return "—"
    return EVENT_LABELS.get(event_type, event_type)
