"""بنود سندات الصرف — مصروفات تشغيلية وخدمات، يُديرها الأدمن ويربطها بحسابات GL."""
from __future__ import annotations

import json
import re
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.settings.service import get_setting, set_setting

SETTING_KEY = "treasury_pay_categories_json"

KIND_OPERATING = "operating"
KIND_UTILITY = "utility"
VALID_KINDS = frozenset({KIND_OPERATING, KIND_UTILITY})

SALARY_CATEGORY_FULL = "راتب موظف"
SALARY_CATEGORY_BONUS = "مكافأة موظف"
SALARY_CATEGORY_OTHER = "صرف موظف"
SALARY_CATEGORY_ADVANCE = "سلف موظفين"

DEFAULT_CATEGORIES: list[dict] = [
    {"key": "rent", "label": "إيجار", "active": True, "kind": KIND_OPERATING, "gl_code": "5210"},
    {"key": "maint", "label": "صيانة", "active": True, "kind": KIND_OPERATING, "gl_code": "5240"},
    {"key": "transport", "label": "نقل وتوصيل", "active": True, "kind": KIND_OPERATING, "gl_code": "5230"},
    {"key": "supplies", "label": "مستلزمات", "active": True, "kind": KIND_OPERATING, "gl_code": "5250"},
    {"key": "ops-other", "label": "أخرى تشغيلية", "active": True, "kind": KIND_OPERATING, "gl_code": "5200"},
    {"key": "electric", "label": "كهرباء", "active": True, "kind": KIND_UTILITY, "gl_code": "5220"},
    {"key": "water", "label": "ماء", "active": True, "kind": KIND_UTILITY, "gl_code": "5220"},
    {"key": "net", "label": "إنترنت واتصالات", "active": True, "kind": KIND_UTILITY, "gl_code": "5220"},
    {"key": "gas", "label": "غاز", "active": True, "kind": KIND_UTILITY, "gl_code": "5220"},
    {"key": "util-other", "label": "أخرى خدمات", "active": True, "kind": KIND_UTILITY, "gl_code": "5220"},
]


def _slug(raw: str) -> str:
    s = (raw or "").strip().lower()
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"[^a-z0-9\u0600-\u06ff\-_]", "", s)
    return s[:48] or uuid.uuid4().hex[:8]


def _infer_kind(raw_kind: str | None) -> str:
    kind = (raw_kind or "").strip().lower()
    if kind == KIND_UTILITY:
        return KIND_UTILITY
    return KIND_OPERATING


def _gl_id_for_code(db: Session, code: str | None) -> int | None:
    if not code:
        return None
    from modules.gl.models import GlAccount

    row = db.scalar(select(GlAccount).where(GlAccount.code == str(code).strip()))
    return int(row.id) if row is not None else None


def _normalize_item(db: Session | None, item: dict) -> dict | None:
    key = str(item.get("key") or "").strip()[:48]
    label = str(item.get("label") or "").strip()[:80]
    if not key or not label:
        return None
    kind = _infer_kind(str(item.get("kind") or "") or None)
    gl_id = item.get("gl_account_id")
    try:
        gl_id = int(gl_id) if gl_id not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        gl_id = None
    if gl_id is None and db is not None and item.get("gl_code"):
        gl_id = _gl_id_for_code(db, str(item.get("gl_code")))
    return {
        "key": key,
        "label": label,
        "active": bool(item.get("active", True)),
        "kind": kind,
        "gl_account_id": gl_id,
    }


def load_pay_categories(db: Session) -> list[dict]:
    raw = (get_setting(db, SETTING_KEY, "") or "").strip()
    source = DEFAULT_CATEGORIES
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, list) and data:
            source = data
    out: list[dict] = []
    for item in source:
        if not isinstance(item, dict):
            continue
        row = _normalize_item(db, item)
        if row:
            out.append(row)
    seen = {c["key"] for c in out}
    for item in DEFAULT_CATEGORIES:
        row = _normalize_item(db, dict(item))
        if row and row["key"] not in seen:
            out.append(row)
            seen.add(row["key"])
    return out or [
        r
        for c in DEFAULT_CATEGORIES
        if (r := _normalize_item(db, dict(c))) is not None
    ]


def save_pay_categories(db: Session, items: list[dict]) -> None:
    clean: list[dict] = []
    seen: set[str] = set()
    for item in items:
        row = _normalize_item(db, item if isinstance(item, dict) else {})
        if row is None or row["key"] in seen:
            continue
        seen.add(row["key"])
        clean.append(row)
    if not clean:
        clean = [
            r
            for c in DEFAULT_CATEGORIES
            if (r := _normalize_item(db, dict(c))) is not None
        ]
    set_setting(db, SETTING_KEY, json.dumps(clean, ensure_ascii=False))


def builtin_categories(kind: str) -> list[dict]:
    """عينات ثابتة تظهر حتى لو فشل حفظ الإعدادات."""
    want = _infer_kind(kind)
    return [
        {"key": c["key"], "label": c["label"], "active": True, "kind": c["kind"], "gl_account_id": None}
        for c in DEFAULT_CATEGORIES
        if c.get("kind") == want
    ]


def seed_default_pay_categories(db: Session) -> list[dict]:
    """يثبّت العينات الافتراضية إن لم يُحفظ شيء بعد، حتى تظهر فوراً في سند الصرف."""
    raw = (get_setting(db, SETTING_KEY, "") or "").strip()
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, list) and data:
            return load_pay_categories(db)
    items = load_pay_categories(db)
    save_pay_categories(db, items)
    return items


def active_categories(db: Session, *, kind: str) -> list[dict]:
    want = _infer_kind(kind)
    rows = [
        c
        for c in load_pay_categories(db)
        if c.get("active", True) and c.get("kind") == want
    ]
    if rows:
        return rows
    return [
        r
        for c in DEFAULT_CATEGORIES
        if (r := _normalize_item(db, dict(c))) is not None
        and r.get("kind") == want
    ]


def category_by_key(db: Session, key: str) -> dict | None:
    want = (key or "").strip()
    if not want:
        return None
    for c in load_pay_categories(db):
        if c["key"] == want:
            return c
    return None


def _sync_gl_map(db: Session, *, label: str, gl_account_id: int | None) -> None:
    if not gl_account_id:
        return
    from modules.gl.expense_maps import set_expense_category_map
    from modules.gl.service import GLError

    try:
        set_expense_category_map(db, category_label=label, gl_account_id=int(gl_account_id))
    except GLError as exc:
        raise ValueError(str(exc)) from exc


def ensure_pay_category_gl_maps(db: Session) -> list[dict]:
    """يربط بنود الصرف الحالية بحسابات المصروف حتى تُخصم من الأرباح."""
    items = load_pay_categories(db)
    for c in items:
        _sync_gl_map(
            db,
            label=str(c.get("label") or ""),
            gl_account_id=c.get("gl_account_id"),
        )
    _ensure_salary_gl_maps(db)
    return items


def _gl_id_or_none(db: Session, code: str) -> int | None:
    return _gl_id_for_code(db, code)


def _ensure_salary_gl_maps(db: Session) -> None:
    salary_id = _gl_id_or_none(db, "5300")
    if salary_id is None:
        return
    for label in (SALARY_CATEGORY_FULL, SALARY_CATEGORY_BONUS, SALARY_CATEGORY_OTHER):
        _sync_gl_map(db, label=label, gl_account_id=salary_id)


def _require_gl_account(db: Session, gl_account_id: int | None) -> int:
    if not gl_account_id:
        raise ValueError("اختر حساب مصروف من دليل الحسابات لهذا البند.")
    from modules.gl.models import GlAccount, GlAccountType

    acc = db.get(GlAccount, int(gl_account_id))
    if acc is None or not acc.is_active:
        raise ValueError("حساب المصروف غير موجود أو غير نشط.")
    if acc.account_type != GlAccountType.EXPENSE:
        raise ValueError("يجب ربط البند بحساب من نوع «مصروفات» حتى يُخصم من الأرباح.")
    return int(acc.id)


def add_pay_category(
    db: Session,
    *,
    label: str,
    kind: str = KIND_OPERATING,
    gl_account_id: int | None = None,
) -> dict:
    label = (label or "").strip()[:80]
    if not label:
        raise ValueError("أدخل اسم البند.")
    kind = _infer_kind(kind)
    gl_id = _require_gl_account(db, gl_account_id)
    items = load_pay_categories(db)
    base = _slug(label)
    key = base
    n = 2
    existing = {c["key"] for c in items}
    while key in existing:
        key = f"{base}-{n}"
        n += 1
    row = {
        "key": key,
        "label": label,
        "active": True,
        "kind": kind,
        "gl_account_id": gl_id,
    }
    items.append(row)
    save_pay_categories(db, items)
    _sync_gl_map(db, label=label, gl_account_id=gl_id)
    return row


def update_pay_category(
    db: Session,
    *,
    key: str,
    label: str,
    active: bool,
    kind: str | None = None,
    gl_account_id: int | None = None,
) -> None:
    key = (key or "").strip()
    label = (label or "").strip()[:80]
    if not key or not label:
        raise ValueError("بيانات البند غير مكتملة.")
    items = load_pay_categories(db)
    found = False
    for c in items:
        if c["key"] != key:
            continue
        resolved_kind = _infer_kind(kind if kind is not None else c.get("kind"))
        gl_id = _require_gl_account(
            db, gl_account_id if gl_account_id is not None else c.get("gl_account_id")
        )
        c["label"] = label
        c["active"] = active
        c["kind"] = resolved_kind
        c["gl_account_id"] = gl_id
        found = True
        _sync_gl_map(db, label=label, gl_account_id=gl_id)
        break
    if not found:
        raise ValueError("البند غير موجود.")
    save_pay_categories(db, items)


def delete_pay_category(db: Session, *, key: str) -> None:
    key = (key or "").strip()
    items = [c for c in load_pay_categories(db) if c["key"] != key]
    if not items:
        items = [
            r
            for c in DEFAULT_CATEGORIES
            if (r := _normalize_item(db, dict(c))) is not None
        ]
    save_pay_categories(db, items)
