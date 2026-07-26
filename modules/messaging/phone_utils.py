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
    """0919774743 → +218919774743 (أرقام أفراد فقط)."""
    s = (raw or "").strip().replace(" ", "").replace("-", "")
    if not s:
        return ""
    cc = (country_code or "218").lstrip("+")
    if s.startswith("+"):
        return s
    if s.startswith("00"):
        return "+" + s[2:]
    if s.startswith("0"):
        return f"+{cc}{s[1:]}"
    if s.startswith(cc):
        return "+" + s
    return f"+{cc}{s}"


def normalize_whatsapp_recipient(raw: str, *, country_code: str = "218") -> str:
    """مستقبل واتساب: رقم فرد أو معرّف جروب @g.us."""
    s = (raw or "").strip()
    if not s:
        return ""
    if is_whatsapp_group_id(s):
        return normalize_whatsapp_group_id(s)
    return normalize_whatsapp_phone(s, country_code=country_code)
