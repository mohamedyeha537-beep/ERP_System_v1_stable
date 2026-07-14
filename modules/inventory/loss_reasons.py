"""أسباب الهدر/الهالك — قائمة يُديرها الأدمن من الإعدادات."""
from __future__ import annotations

import json

from sqlalchemy.orm import Session

from modules.settings.service import get_setting, set_setting

SETTINGS_KEY = "inventory_loss_reasons"

DEFAULT_REASONS: list[str] = [
    "انتهت الصلاحية",
    "تلف / مخربة",
    "سرقة",
    "هدر مطبخ",
    "عينة / تذوق",
]


def _parse_json_list(raw: str | None) -> list[str]:
    if not (raw or "").strip():
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in data:
        s = str(item).strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _editable_list(db: Session) -> list[str]:
    raw = get_setting(db, SETTINGS_KEY, "")
    if not (raw or "").strip():
        return list(DEFAULT_REASONS)
    return _parse_json_list(raw)


def get_loss_reasons(db: Session) -> list[str]:
    return _editable_list(db)


def save_loss_reasons(db: Session, reasons: list[str]) -> None:
    clean: list[str] = []
    seen: set[str] = set()
    for item in reasons:
        s = (item or "").strip()
        if s and s not in seen:
            seen.add(s)
            clean.append(s)
    set_setting(db, SETTINGS_KEY, json.dumps(clean, ensure_ascii=False))


def add_loss_reason(db: Session, label: str) -> list[str]:
    s = (label or "").strip()
    if not s:
        return get_loss_reasons(db)
    cur = get_loss_reasons(db)
    if s in cur:
        return cur
    cur.append(s)
    save_loss_reasons(db, cur)
    return cur


def remove_loss_reason(db: Session, index: int) -> list[str]:
    cur = _editable_list(db)
    if 0 <= index < len(cur):
        cur.pop(index)
        save_loss_reasons(db, cur)
    return cur


def update_loss_reason(db: Session, index: int, label: str) -> list[str]:
    new_label = (label or "").strip()
    if not new_label:
        return get_loss_reasons(db)
    cur = _editable_list(db)
    if not (0 <= index < len(cur)):
        return cur
    if new_label in cur and cur.index(new_label) != index:
        return cur
    cur[index] = new_label
    save_loss_reasons(db, cur)
    return cur
