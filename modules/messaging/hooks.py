"""نقاط التشغيل الثابتة في النظام — يربطها الإدمن بأحداث من لوحة الإدارة."""
from __future__ import annotations

from modules.messaging.events import (
    CUSTOMER_FIRST_LINKED,
    LOYALTY_POINTS_ADJUSTED,
    LOYALTY_POINTS_EARNED,
    LOYALTY_POINTS_REDEEMED,
    PRODUCT_EXPIRY,
    STOCK_LOW,
)

# (hook_code, label_ar, audience_hint)
SYSTEM_HOOKS: list[tuple[str, str, str]] = [
    ("hook.customer.first_consent", "أول موافقة عميل (دفع نقطة البيع)", "customer"),
    ("hook.shop.share_consent", "مشاركة منتج برقم هاتف (المتجر)", "customer"),
    ("hook.loyalty.points_earned", "اكتساب نقاط ولاء", "customer"),
    ("hook.loyalty.points_redeemed", "خصم نقاط ولاء", "customer"),
    ("hook.loyalty.points_adjusted", "تعديل نقاط يدوي", "customer"),
    ("hook.stock.low", "تنبيه نقص مخزون", "admin"),
    ("hook.product.expiry", "تنبيه قرب انتهاء صلاحية", "admin"),
]

DEFAULT_HOOK_EVENTS: dict[str, list[str]] = {
    "hook.customer.first_consent": [CUSTOMER_FIRST_LINKED],
    "hook.shop.share_consent": [CUSTOMER_FIRST_LINKED],
    "hook.loyalty.points_earned": [LOYALTY_POINTS_EARNED],
    "hook.loyalty.points_redeemed": [LOYALTY_POINTS_REDEEMED],
    "hook.loyalty.points_adjusted": [LOYALTY_POINTS_ADJUSTED],
    "hook.stock.low": [STOCK_LOW],
    "hook.product.expiry": [PRODUCT_EXPIRY],
}

SYSTEM_HOOK_CODES: frozenset[str] = frozenset(h for h, _, _ in SYSTEM_HOOKS)


def hook_label_ar(hook_code: str) -> str:
    for code, label, _ in SYSTEM_HOOKS:
        if code == hook_code:
            return label
    return hook_code


def hook_audience_hint(hook_code: str) -> str:
    for code, _, aud in SYSTEM_HOOKS:
        if code == hook_code:
            return aud
    return "customer"
