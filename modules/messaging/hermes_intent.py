"""Hermes — فهم النية بمرونة (أخطاء إملائية، تكرار حروف، تشابه).

بدون تلقين كل خطأ يدوياً: خوارزميات عامة + (اختياري) API ذكاء اصطناعي.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

from modules.messaging.hermes_catalog import _normalize, _tokens

# كلمات مرجعية — تُقارَن بها الرسالة خوارزمياً (ليست قائمة أخطاء مكتوبة يدوياً)
_MENU_KEYWORDS = (
    "منيو",
    "menu",
    "قائمة",
    "قائمه",
    "الاقسام",
    "الأقسام",
    "اقسام",
    "أقسام",
)

_ORDER_KEYWORDS = (
    "اطلب",
    "طلب",
    "order",
    "أضف",
    "اضف",
    "اضيف",
)

_PRICE_KEYWORDS = ("سعر", "كم", "بكام", "بكم", "ثمن", "تكلف", "price")

_HUMAN_KEYWORDS = (
    "موظف",
    "كلمني",
    "كلموني",
    "حد يرد",
    "خدمة عمل",
    "خدمه عمل",
)

# تشابه ≥ هذا الحد = نفس النية (0.72 يغطي منييو→منيو، mnue→menu…)
_FUZZY_THRESHOLD = 0.72


def _collapse_doubled_chars(text: str) -> str:
    """منييو → منيو، menuu → menu (تكرار حرف متجاور)."""
    if not text:
        return text
    out: list[str] = []
    prev = ""
    for ch in text:
        if ch != prev:
            out.append(ch)
        prev = ch
    return "".join(out)


def _similar(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _candidates(text: str) -> list[str]:
    t = _normalize(text)
    if not t:
        return []
    collapsed = _collapse_doubled_chars(t)
    parts = _tokens(text) or [t]
    seen: set[str] = set()
    out: list[str] = []
    for c in (t, collapsed, *parts):
        c = (c or "").strip()
        if len(c) >= 2 and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _fuzzy_has_keyword(text: str, keywords: tuple[str, ...]) -> bool:
    t = _normalize(text)
    if not t:
        return False
    for kw in keywords:
        kn = _normalize(kw)
        if kn and kn in t:
            return True
    for cand in _candidates(text):
        for kw in keywords:
            kn = _normalize(kw)
            if len(cand) < 3 or len(kn) < 3:
                continue
            if _similar(cand, kn) >= _FUZZY_THRESHOLD:
                return True
            # احتواء جزئي للعبارات القصيرة: «منييو» قريبة من «منيو»
            if len(cand) <= len(kn) + 2 and (kn in cand or cand in kn):
                if _similar(cand, kn) >= 0.65:
                    return True
    return False


def wants_menu(text: str) -> bool:
    """هل يريد الزبون المنيو؟ (مع تحمل الأخطاء الإملائية)."""
    extra = (
        "عندكم",
        "عندك",
        "ماذا عند",
        "ايه عند",
        "إيه عند",
        "وش عند",
        "فيه عند",
        "تبيع",
        "تبيعون",
        "متوفر",
    )
    return _fuzzy_has_keyword(text, _MENU_KEYWORDS + extra)


def wants_order(text: str) -> bool:
    return _fuzzy_has_keyword(text, _ORDER_KEYWORDS)


def wants_price(text: str) -> bool:
    return _fuzzy_has_keyword(text, _PRICE_KEYWORDS)


def wants_human_fuzzy(text: str) -> bool:
    return _fuzzy_has_keyword(text, _HUMAN_KEYWORDS)


_QUESTION_WORDS = (
    "كيف",
    "شلون",
    "شنو",
    "شن",
    "how",
    "why",
    "ممكن",
    "نقدر",
    "هل",
    "ليش",
    "لماذا",
    "وين",
    "اين",
    "أين",
)


def is_question_like(text: str) -> bool:
    """هل النص سؤالاً وليس اسم صنف؟"""
    t = _normalize(text)
    if not t:
        return False
    if "?" in text or "؟" in text:
        return True
    if any(w in t for w in _QUESTION_WORDS):
        return True
    if re.search(r"^(كيف|شلون|شنو|شن|هل|ممكن|how)\s", t):
        return True
    return False


def is_how_to_order_question(text: str) -> bool:
    """«كيف نقدر نطلب؟» — سؤال عن الطريقة وليس طلب صنف."""
    t = _normalize(text)
    if not t:
        return False
    if re.search(
        r"(كيف|شلون|شنو|شن|how).*(طلب|اطلب|نطلب|اكل|ناكل|اتاكل|اطلب|order)",
        t,
    ):
        return True
    if re.search(r"(نقدر|ممكن|نبي|هل).*(طلب|اطلب|نطلب|اكل|ناكل)", t):
        return True
    if re.search(r"(طريقة|خطوات|اش).*(طلب|اطلب|نطلب)", t):
        return True
    return False


def intent_summary(text: str) -> str | None:
    """ملخص النية المكتشفة — للتشخيص أو السجلات."""
    if wants_menu(text):
        return "menu"
    if wants_order(text):
        return "order"
    if wants_price(text):
        return "price"
    if wants_human_fuzzy(text):
        return "human"
    return None
