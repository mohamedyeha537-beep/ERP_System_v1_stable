"""تصنيفات مصروفات الجلسة — يُدارها الأدمن ويربط كل بند بحساب مصروف GL."""
from __future__ import annotations

import json
import re
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.settings.service import get_setting, set_setting

SETTING_KEY = "pos_shift_expense_categories_json"

KIND_EXPENSE = "expense"
KIND_ADVANCE = "advance"

DEFAULT_CATEGORIES: list[dict] = [
    {"key": "ops", "label": "تشغيل", "active": True, "kind": KIND_EXPENSE, "gl_code": "5200"},
    {"key": "buy", "label": "مشتريات", "active": True, "kind": KIND_EXPENSE, "gl_code": "5250"},
    {"key": "advance", "label": "سلف", "active": True, "kind": KIND_ADVANCE, "gl_code": None},
    {"key": "other", "label": "أخرى", "active": True, "kind": KIND_EXPENSE, "gl_code": "5200"},
    {"key": "hotel_ops", "label": "تشغيل فندق", "active": True, "kind": KIND_EXPENSE, "gl_code": "5520"},
    {"key": "hotel_util", "label": "كهرباء وماء — فندق", "active": True, "kind": KIND_EXPENSE, "gl_code": "5522"},
    {"key": "hotel_maint", "label": "صيانة فندق", "active": True, "kind": KIND_EXPENSE, "gl_code": "5524"},
]


def _slug(raw: str) -> str:
    s = (raw or "").strip().lower()
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"[^a-z0-9\u0600-\u06ff\-_]", "", s)
    return s[:48] or uuid.uuid4().hex[:8]


def _infer_kind(key: str, label: str, raw_kind: str | None) -> str:
    kind = (raw_kind or "").strip().lower()
    if kind == KIND_ADVANCE:
        return KIND_ADVANCE
    if kind == KIND_EXPENSE:
        return KIND_EXPENSE
    if (key or "") == "advance" or "سلف" in (label or ""):
        return KIND_ADVANCE
    return KIND_EXPENSE


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
    kind = _infer_kind(key, label, str(item.get("kind") or "") or None)
    gl_id = item.get("gl_account_id")
    try:
        gl_id = int(gl_id) if gl_id not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        gl_id = None
    if gl_id is None and db is not None and item.get("gl_code"):
        gl_id = _gl_id_for_code(db, str(item.get("gl_code")))
    if gl_id is None and db is not None and kind == KIND_EXPENSE:
        defaults = {"ops": "5200", "buy": "5250", "other": "5200"}
        gl_id = _gl_id_for_code(db, defaults.get(key, "5200"))
    return {
        "key": key,
        "label": label,
        "active": bool(item.get("active", True)),
        "kind": kind,
        "gl_account_id": gl_id,
    }


def load_shift_expense_categories(db: Session) -> list[dict]:
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


def save_shift_expense_categories(db: Session, items: list[dict]) -> None:
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


def active_categories_for_pos(db: Session) -> list[tuple[str, str]]:
    """(key, label) للقائمة المنسدلة في POS والفندق."""
    return [
        (c["key"], c["label"])
        for c in load_shift_expense_categories(db)
        if c.get("active", True)
    ]


def advance_category_keys(db: Session) -> list[str]:
    return [
        c["key"]
        for c in load_shift_expense_categories(db)
        if c.get("active", True) and is_advance_category(db, c["key"])
    ]


def category_expense_label(db: Session, category_key: str) -> str:
    """تسمية فئة المصروف كما تُحفظ في المشتريات/القيود (متوافقة مع خرائط GL)."""
    key = (category_key or "").strip()
    for c in load_shift_expense_categories(db):
        if c["key"] != key:
            continue
        if is_advance_category(db, key):
            return "سلف موظفين"
        return str(c.get("label") or "").strip() or "أخرى"
    return "أخرى"


def is_advance_category(db: Session, category_key: str) -> bool:
    key = (category_key or "").strip()
    for c in load_shift_expense_categories(db):
        if c["key"] != key:
            continue
        if c.get("kind") == KIND_ADVANCE:
            return True
        if key == "advance" or "سلف" in str(c.get("label") or ""):
            return True
    return key == "advance"


def list_expense_gl_accounts(db: Session) -> list:
    from modules.gl.models import GlAccount, GlAccountType

    return list(
        db.scalars(
            select(GlAccount)
            .where(
                GlAccount.is_active.is_(True),
                GlAccount.account_type == GlAccountType.EXPENSE,
            )
            .order_by(GlAccount.code)
        ).all()
    )


def _sync_gl_map(db: Session, *, label: str, gl_account_id: int | None, kind: str) -> None:
    if kind == KIND_ADVANCE or not gl_account_id:
        return
    from modules.gl.expense_maps import set_expense_category_map
    from modules.gl.service import GLError

    try:
        set_expense_category_map(db, category_label=label, gl_account_id=int(gl_account_id))
    except GLError as exc:
        raise ValueError(str(exc)) from exc


def ensure_category_gl_maps(db: Session) -> list[dict]:
    """يربط بنود المصروف الحالية بحسابات GL حتى تُخصم من الأرباح."""
    items = load_shift_expense_categories(db)
    for c in items:
        _sync_gl_map(
            db,
            label=str(c.get("label") or ""),
            gl_account_id=c.get("gl_account_id"),
            kind=str(c.get("kind") or KIND_EXPENSE),
        )
    return items


def known_expense_labels(db: Session) -> list[str]:
    labels = ["سلف موظفين"]
    for c in load_shift_expense_categories(db):
        lab = str(c.get("label") or "").strip()
        if lab and lab not in labels:
            labels.append(lab)
    return labels


def _require_gl_account(db: Session, gl_account_id: int | None, *, kind: str) -> int | None:
    if kind == KIND_ADVANCE:
        return None
    if not gl_account_id:
        raise ValueError("اختر حساب مصروف من دليل الحسابات لهذا البند.")
    from modules.gl.models import GlAccount, GlAccountType

    acc = db.get(GlAccount, int(gl_account_id))
    if acc is None or not acc.is_active:
        raise ValueError("حساب المصروف غير موجود أو غير نشط.")
    if acc.account_type != GlAccountType.EXPENSE:
        raise ValueError("يجب ربط البند بحساب من نوع «مصروفات» حتى يُخصم من الأرباح.")
    return int(acc.id)


def add_category(
    db: Session,
    *,
    label: str,
    kind: str = KIND_EXPENSE,
    gl_account_id: int | None = None,
) -> dict:
    label = (label or "").strip()[:80]
    if not label:
        raise ValueError("أدخل اسم البند.")
    kind = KIND_ADVANCE if (kind or "").strip().lower() == KIND_ADVANCE or "سلف" in label else KIND_EXPENSE
    gl_id = _require_gl_account(db, gl_account_id, kind=kind)
    items = load_shift_expense_categories(db)
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
    save_shift_expense_categories(db, items)
    _sync_gl_map(db, label=label, gl_account_id=gl_id, kind=kind)
    return row


def update_category(
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
    items = load_shift_expense_categories(db)
    found = False
    for c in items:
        if c["key"] != key:
            continue
        resolved_kind = _infer_kind(key, label, kind if kind is not None else c.get("kind"))
        gl_id = _require_gl_account(db, gl_account_id if gl_account_id is not None else c.get("gl_account_id"), kind=resolved_kind)
        c["label"] = label
        c["active"] = active
        c["kind"] = resolved_kind
        c["gl_account_id"] = gl_id
        found = True
        _sync_gl_map(db, label=label, gl_account_id=gl_id, kind=resolved_kind)
        break
    if not found:
        raise ValueError("البند غير موجود.")
    save_shift_expense_categories(db, items)


def delete_category(db: Session, *, key: str) -> None:
    key = (key or "").strip()
    items = [c for c in load_shift_expense_categories(db) if c["key"] != key]
    if not items:
        items = [
            r
            for c in DEFAULT_CATEGORIES
            if (r := _normalize_item(db, dict(c))) is not None
        ]
    save_shift_expense_categories(db, items)
