"""ملاحظات بنود الطلب — خيارات جاهزة لكل صنف ودمج النص."""
from __future__ import annotations

import json

from sqlalchemy.orm import Session

from modules.settings.service import get_setting, set_setting

SETTINGS_KEY = "order_line_modifier_presets"

DEFAULT_PRESETS: list[str] = [
    "بدون هريسة",
    "زيادة هريسة",
    "بدون ملح",
    "زيادة ملح",
    "نادر الاستحمام",
]


def parse_modifier_presets_json(raw: str | None) -> list[str]:
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


def serialize_modifier_presets(presets: list[str] | None) -> str | None:
    clean: list[str] = []
    seen: set[str] = set()
    for item in presets or []:
        s = (item or "").strip()
        if s and s not in seen:
            seen.add(s)
            clean.append(s)
    if not clean:
        return None
    return json.dumps(clean, ensure_ascii=False)


def parse_modifier_presets_form(raw: str | None) -> str | None:
    """سطر لكل خيار من نموذج الإدارة."""
    lines = [ln.strip() for ln in (raw or "").splitlines() if ln.strip()]
    return serialize_modifier_presets(lines)


def parse_preset_form_values(form, field: str = "preset") -> list[str]:
    """يقرأ خانات checkbox متعددة من نموذج HTML (getlist)."""
    raw = form.getlist(field) if hasattr(form, "getlist") else []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        s = str(item).strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def get_product_modifier_presets(
    product, *, pool: list[str] | None = None
) -> list[str]:
    """ملاحظات الصنف المفعّلة — عند تمرير pool تُستبعد العبارات المحذوفة من الإعدادات."""
    if product is None:
        return []
    selected = parse_modifier_presets_json(
        getattr(product, "line_modifier_presets", None)
    )
    if pool is None:
        return selected
    pool_set = set(pool)
    return [s for s in selected if s in pool_set]


def _stored_modifier_presets(db: Session) -> tuple[bool, list[str]]:
    """(configured, presets) — configured=False يعني لم تُحفظ القائمة بعد."""
    raw = get_setting(db, SETTINGS_KEY, "")
    if not (raw or "").strip():
        return False, []
    return True, parse_modifier_presets_json(raw)


def _editable_modifier_list(db: Session) -> list[str]:
    configured, presets = _stored_modifier_presets(db)
    if not configured:
        return list(DEFAULT_PRESETS)
    return presets


def get_modifier_presets(db: Session) -> list[str]:
    """القائمة العامة لملاحظات الكاشير — تُدار من الإعدادات وتُختار منها لكل صنف."""
    return _editable_modifier_list(db)


def save_modifier_presets(db: Session, presets: list[str]) -> None:
    clean: list[str] = []
    seen: set[str] = set()
    for item in presets:
        s = (item or "").strip()
        if s and s not in seen:
            seen.add(s)
            clean.append(s)
    set_setting(db, SETTINGS_KEY, json.dumps(clean, ensure_ascii=False))


def add_modifier_preset(db: Session, label: str) -> list[str]:
    s = (label or "").strip()
    if not s:
        return get_modifier_presets(db)
    cur = get_modifier_presets(db)
    if s in cur:
        return cur
    cur.append(s)
    save_modifier_presets(db, cur)
    return cur


def remove_modifier_preset(db: Session, index: int) -> list[str]:
    cur = _editable_modifier_list(db)
    if 0 <= index < len(cur):
        removed = cur[index]
        cur.pop(index)
        save_modifier_presets(db, cur)
        _remove_modifier_from_products(db, removed)
    return cur


def _remove_modifier_from_products(db: Session, label: str) -> None:
    from sqlalchemy import select

    from modules.catalog.models import Product

    pool = get_modifier_presets(db)
    for product in db.scalars(
        select(Product).where(Product.line_modifier_presets.isnot(None))
    ).all():
        selected = get_product_modifier_presets(product)
        if label not in selected:
            continue
        updated = [item for item in selected if item != label]
        save_product_modifier_selection(product, updated, pool=pool)


def sync_product_modifier_presets(db: Session, product) -> list[str]:
    """يزيل من الصنف أي ملاحظة لم تعد في القائمة العامة."""
    pool = get_modifier_presets(db)
    return save_product_modifier_selection(
        product, get_product_modifier_presets(product), pool=pool
    )


def update_modifier_preset(db: Session, index: int, label: str) -> list[str]:
    new_label = (label or "").strip()
    if not new_label:
        return get_modifier_presets(db)
    cur = _editable_modifier_list(db)
    if not (0 <= index < len(cur)):
        return cur
    if new_label in cur and cur.index(new_label) != index:
        return cur
    old_label = cur[index]
    if old_label == new_label:
        return cur
    cur[index] = new_label
    save_modifier_presets(db, cur)
    _rename_modifier_on_products(db, old_label, new_label)
    return cur


def _rename_modifier_on_products(db: Session, old_label: str, new_label: str) -> None:
    from sqlalchemy import select

    from modules.catalog.models import Product

    for product in db.scalars(
        select(Product).where(Product.line_modifier_presets.isnot(None))
    ).all():
        selected = get_product_modifier_presets(product)
        if old_label not in selected:
            continue
        updated = [new_label if item == old_label else item for item in selected]
        pool = get_modifier_presets(db)
        save_product_modifier_selection(product, updated, pool=pool)


def save_product_modifier_selection(
    product,
    selected: list[str] | None,
    *,
    pool: list[str],
) -> list[str]:
    """يحفظ على الصنف فقط عبارات موجودة في القائمة العامة."""
    pool_set = set(pool)
    clean: list[str] = []
    seen: set[str] = set()
    for item in selected or []:
        s = (item or "").strip()
        if s in pool_set and s not in seen:
            seen.add(s)
            clean.append(s)
    product.line_modifier_presets = serialize_modifier_presets(clean)
    return clean


def add_product_modifier_preset(product, label: str, *, pool: list[str] | None = None) -> list[str]:
    s = (label or "").strip()
    if not s:
        return get_product_modifier_presets(product)
    if pool is not None and s not in pool:
        return get_product_modifier_presets(product)
    cur = get_product_modifier_presets(product)
    if s in cur:
        return cur
    cur.append(s)
    product.line_modifier_presets = serialize_modifier_presets(cur)
    return cur


def remove_product_modifier_preset(product, index: int) -> list[str]:
    cur = get_product_modifier_presets(product)
    if 0 <= index < len(cur):
        cur.pop(index)
        product.line_modifier_presets = serialize_modifier_presets(cur)
    return cur


def compose_line_note(
    selected_presets: list[str] | None,
    extra_note: str | None,
    *,
    allowed_presets: list[str] | None = None,
) -> str | None:
    allowed = set(allowed_presets or [])
    parts: list[str] = []
    for p in selected_presets or []:
        s = (p or "").strip()
        if not s:
            continue
        if allowed and s not in allowed:
            continue
        if s not in parts:
            parts.append(s)
    extra = (extra_note or "").strip()
    if extra:
        for chunk in extra.replace("،", "·").split("·"):
            c = chunk.strip()
            if c and c not in parts:
                parts.append(c)
    return " · ".join(parts) if parts else None


def split_line_note(
    note: str | None, presets: list[str] | None
) -> tuple[list[str], str]:
    if not (note or "").strip():
        return [], ""
    preset_set = set(presets or [])
    parts = [x.strip() for x in (note or "").split(" · ") if x.strip()]
    known = [p for p in parts if p in preset_set]
    extra_parts = [p for p in parts if p not in preset_set]
    return known, " · ".join(extra_parts)
