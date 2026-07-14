"""صيانة الشقق — أنواع الأعطال والإشعارات."""
from __future__ import annotations

MAINTENANCE_ISSUE_TYPES: dict[str, str] = {
    "ac": "مكيف",
    "microwave": "ميكروويف",
    "fridge": "ثلاجة",
    "tv": "تلفزيون",
    "plumbing": "سباكة",
    "electrical": "كهرباء",
    "furniture": "أثاث / باب / نافذة",
    "bathroom": "حمام / دش",
    "kitchen": "مطبخ",
    "other": "أخرى",
}


def issue_label(issue_type: str | None) -> str:
    key = (issue_type or "").strip().lower()
    if not key:
        return "غير محدد"
    return MAINTENANCE_ISSUE_TYPES.get(key, key)
