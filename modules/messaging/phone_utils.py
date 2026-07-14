"""تنسيق أرقام واتساب للإرسال."""
from __future__ import annotations


def normalize_whatsapp_phone(raw: str, *, country_code: str = "218") -> str:
    """0919774743 → +218919774743"""
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
