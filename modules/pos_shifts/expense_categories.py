"""تصنيفات مصروفات الجلسة — يُدارها المدير من الإعدادات."""
from __future__ import annotations

import json
import re
import uuid

from sqlalchemy.orm import Session

from modules.settings.service import get_setting, set_setting

SETTING_KEY = "pos_shift_expense_categories_json"

DEFAULT_CATEGORIES: list[dict] = [
    {"key": "ops", "label": "تشغيل", "active": True},
    {"key": "buy", "label": "مشتريات", "active": True},
    {"key": "advance", "label": "سلف", "active": True},
    {"key": "other", "label": "أخرى", "active": True},
]


def _slug(raw: str) -> str:
    s = (raw or "").strip().lower()
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"[^a-z0-9\u0600-\u06ff\-_]", "", s)
    return s[:48] or uuid.uuid4().hex[:8]


def load_shift_expense_categories(db: Session) -> list[dict]:
    raw = (get_setting(db, SETTING_KEY, "") or "").strip()
    if not raw:
        return [dict(c) for c in DEFAULT_CATEGORIES]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return [dict(c) for c in DEFAULT_CATEGORIES]
    if not isinstance(data, list):
        return [dict(c) for c in DEFAULT_CATEGORIES]
    out: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        label = str(item.get("label") or "").strip()
        if not key or not label:
            continue
        out.append(
            {
                "key": key[:48],
                "label": label[:80],
                "active": bool(item.get("active", True)),
            }
        )
    return out or [dict(c) for c in DEFAULT_CATEGORIES]


def save_shift_expense_categories(db: Session, items: list[dict]) -> None:
    clean: list[dict] = []
    seen: set[str] = set()
    for item in items:
        key = str(item.get("key") or "").strip()[:48]
        label = str(item.get("label") or "").strip()[:80]
        if not key or not label or key in seen:
            continue
        seen.add(key)
        clean.append({"key": key, "label": label, "active": bool(item.get("active", True))})
    if not clean:
        clean = [dict(c) for c in DEFAULT_CATEGORIES]
    set_setting(db, SETTING_KEY, json.dumps(clean, ensure_ascii=False))


def active_categories_for_pos(db: Session) -> list[tuple[str, str]]:
    """(key, label) للقائمة المنسدلة في POS."""
    return [
        (c["key"], c["label"])
        for c in load_shift_expense_categories(db)
        if c.get("active", True)
    ]


def category_expense_label(db: Session, category_key: str) -> str:
    key = (category_key or "").strip()
    for c in load_shift_expense_categories(db):
        if c["key"] == key:
            return f"مصروف جلسة — {c['label']}"
    return "مصروف جلسة"


def add_category(db: Session, *, label: str) -> dict:
    label = (label or "").strip()[:80]
    if not label:
        raise ValueError("أدخل اسم التصنيف.")
    items = load_shift_expense_categories(db)
    base = _slug(label)
    key = base
    n = 2
    existing = {c["key"] for c in items}
    while key in existing:
        key = f"{base}-{n}"
        n += 1
    row = {"key": key, "label": label, "active": True}
    items.append(row)
    save_shift_expense_categories(db, items)
    return row


def update_category(db: Session, *, key: str, label: str, active: bool) -> None:
    key = (key or "").strip()
    label = (label or "").strip()[:80]
    if not key or not label:
        raise ValueError("بيانات التصنيف غير مكتملة.")
    items = load_shift_expense_categories(db)
    found = False
    for c in items:
        if c["key"] == key:
            c["label"] = label
            c["active"] = active
            found = True
            break
    if not found:
        raise ValueError("التصنيف غير موجود.")
    save_shift_expense_categories(db, items)


def delete_category(db: Session, *, key: str) -> None:
    key = (key or "").strip()
    items = [c for c in load_shift_expense_categories(db) if c["key"] != key]
    if not items:
        items = [dict(c) for c in DEFAULT_CATEGORIES]
    save_shift_expense_categories(db, items)
