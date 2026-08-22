"""تنسيق أرقام واتساب للإرسال (أفراد أو جروبات)."""
from __future__ import annotations

import re

# معرّف جروب واتساب — مثال: 120363040377656518@g.us
_WA_GROUP_RE = re.compile(r"^(\d{10,30})(@g\.us)?$", re.IGNORECASE)


def is_whatsapp_group_id(raw: str) -> bool:
    """هل القيمة معرّف جروب واتساب وليس رقم هاتف؟"""
    s = (raw or "").strip().replace(" ", "")
    if not s:
        return False
    if s.lower().endswith("@g.us"):
        return bool(re.match(r"^\d{10,30}@g\.us$", s, re.IGNORECASE))
    # رقم طويل جداً بدون @ غالباً جروب (أرقام الهاتف الدولية أقصر)
    return bool(re.fullmatch(r"\d{18,30}", s))


def normalize_whatsapp_group_id(raw: str) -> str:
    """يعيد معرّف الجروب بالصيغة: 12036...@g.us"""
    s = (raw or "").strip().replace(" ", "").replace("-", "")
    m = _WA_GROUP_RE.match(s)
    if not m:
        return ""
    return f"{m.group(1)}@g.us"


def normalize_whatsapp_phone(raw: str, *, country_code: str = "218") -> str:
    """ينسّق رقم واتساب دولي بصيغة +E.164.

    - ليبي يبدأ بـ 0: 0919774743 → +218919774743
    - يبدأ بـ + أو 00: يُحافظ على الدولة
    - رقم دولي بدون + (مثل 9665...): يُضاف +
    - رقم محلي قصير بدون 0: يُفترض مفتاح الدولة الافتراضي
    """
    s = (raw or "").strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if not s:
        return ""
    cc = (country_code or "218").lstrip("+")
    if s.startswith("+"):
        digits = re.sub(r"\D", "", s[1:])
        return f"+{digits}" if digits else ""
    if s.startswith("00"):
        digits = re.sub(r"\D", "", s[2:])
        return f"+{digits}" if digits else ""
    digits = re.sub(r"\D", "", s)
    if not digits:
        return ""
    # محلي ليبي يبدأ بـ 0
    if digits.startswith("0") and len(digits) in (9, 10):
        return f"+{cc}{digits[1:]}"
    # يبدأ بمفتاح الدولة الافتراضي
    if digits.startswith(cc) and len(digits) >= len(cc) + 8:
        return f"+{digits}"
    # رقم دولي واضح (أكثر من 10 أرقام بدون 0 في البداية) — لا نفرض ليبيا
    if len(digits) >= 11 and not digits.startswith("0"):
        return f"+{digits}"
    # رقم محلي بدون صفر: 9xxxxxxxx ليبي
    if len(digits) == 9 and digits.startswith("9"):
        return f"+{cc}{digits}"
    return f"+{cc}{digits}"


def require_whatsapp_phone(raw: str, *, country_code: str = "218") -> str:
    """رقم واتساب صالح للإرسال (ليبي أو دولي)."""
    n = normalize_whatsapp_phone(raw, country_code=country_code)
    digits = re.sub(r"\D", "", n)
    if not n.startswith("+") or len(digits) < 8 or len(digits) > 15:
        raise ValueError(
            "رقم واتساب غير صالح. استخدم صيغة دولية مثل +9665xxxxxxx "
            "أو رقم ليبي 09xxxxxxxx."
        )
    return n


def normalize_whatsapp_recipient(raw: str, *, country_code: str = "218") -> str:
    """مستقبل واتساب: رقم فرد أو معرّف جروب @g.us."""
    s = (raw or "").strip()
    if not s:
        return ""
    if is_whatsapp_group_id(s):
        return normalize_whatsapp_group_id(s)
    return normalize_whatsapp_phone(s, country_code=country_code)
