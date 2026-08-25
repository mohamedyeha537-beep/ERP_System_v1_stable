"""رقم سري نقطة البيع / الاستقبال (4 أرقام) للموظفين."""
from __future__ import annotations

import re

from modules.authz.service import hash_password, verify_password

# أرقام عربية / فارسية → لاتينية — لوحة المفاتيح العربية تحفظ غالباً ٠١٢… بينما الـ pad يرسل 0-9
_DIGIT_TRANSLATE = str.maketrans(
    "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹",
    "01234567890123456789",
)
# بعد التطبيع نقبل الأرقام اللاتينية فقط (لا \d اليونيكود)
_PIN_RE = re.compile(r"^[0-9]{4}$")
_BCRYPT_PREFIXES = ("$2a$", "$2b$", "$2y$", "$2x$")


def normalize_pos_pin(plain: str | None) -> str:
    """يحذف الفراغات ويحوّل الأرقام العربية/الفارسية إلى 0-9."""
    if plain is None:
        return ""
    text = str(plain).strip().translate(_DIGIT_TRANSLATE)
    # إزالة فواصل نادرة (مسافات، شرطات) بين الأرقام
    return re.sub(r"[\s\-_./]", "", text)


def validate_pos_pin(plain: str) -> str:
    pin = normalize_pos_pin(plain)
    if not _PIN_RE.match(pin):
        raise ValueError("الرقم السري يجب أن يكون 4 أرقام فقط (مثال: 1234).")
    return pin


def hash_pos_pin(plain: str) -> str:
    return hash_password(validate_pos_pin(plain))


def _looks_like_bcrypt(hashed: str) -> bool:
    h = (hashed or "").strip()
    return any(h.startswith(p) for p in _BCRYPT_PREFIXES) and len(h) >= 50


def verify_pos_pin(plain: str, hashed: str | None) -> bool:
    if not hashed:
        return False
    try:
        pin = validate_pos_pin(plain)
    except ValueError:
        return False
    stored = str(hashed).strip()
    if _looks_like_bcrypt(stored):
        if verify_password(pin, stored):
            return True
        # محاولة نادرة: لو حُفظ مع أرقام عربية سابقاً
        try:
            raw = str(plain).strip()
            if raw and raw != pin and verify_password(raw, stored):
                return True
        except Exception:  # noqa: BLE001
            pass
        return False
    # إرث: رقم صريح محفوظ بالخطأ بدل bcrypt
    legacy = normalize_pos_pin(stored)
    return bool(_PIN_RE.match(legacy) and legacy == pin)
