from __future__ import annotations

from sqlalchemy.orm import Session

from modules.settings.models import AppSetting

PAPER_SIZES: tuple[tuple[str, str], ...] = (
    ("A4", "A4 (ورقة كاملة 21×29.7 سم)"),
    ("A5", "A5 (نصف ورقة 14.8×21 سم)"),
    ("80mm", "حرارية 80 ملم (8 سم)"),
    ("58mm", "حرارية 58 ملم (5.8 سم)"),
)

VALID_PAPER = {code for code, _ in PAPER_SIZES}

DEFAULTS: dict[str, str] = {
    "print_paper_size": "A5",
    "store_name": "نقطة البيع",
    # تنبيهات نفاد المخزون
    "alerts_enabled": "0",
    "alerts_email_to": "",
    "smtp_host": "",
    "smtp_port": "587",
    "smtp_user": "",
    "smtp_password": "",
    "smtp_from": "",
    "smtp_use_tls": "1",
    "whatsapp_webhook_url": "",
    "whatsapp_webhook_method": "GET",
    "whatsapp_webhook_param": "text",
    # مخزن خصم مبيعات نقطة البيع (فارغ = المخزن الرئيسي)
    "default_sales_warehouse_id": "",
}


def get_int(db: Session, key: str, default: int = 0) -> int:
    raw = get_setting(db, key, str(default))
    try:
        return int((raw or "").strip())
    except ValueError:
        return default


def get_bool(db: Session, key: str, default: bool = False) -> bool:
    raw = (get_setting(db, key, "1" if default else "0") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def normalize_paper(code: str | None, fallback: str = "A5") -> str:
    if code and code in VALID_PAPER:
        return code
    return fallback if fallback in VALID_PAPER else "A5"


def get_setting(db: Session, key: str, default: str = "") -> str:
    row = db.get(AppSetting, key)
    return row.value if row is not None else default


def set_setting(db: Session, key: str, value: str) -> None:
    row = db.get(AppSetting, key)
    if row is None:
        db.add(AppSetting(key=key, value=value))
    else:
        row.value = value
    db.flush()


def ensure_default_settings(db: Session) -> None:
    changed = False
    for k, v in DEFAULTS.items():
        if db.get(AppSetting, k) is None:
            db.add(AppSetting(key=k, value=v))
            changed = True
    if changed:
        db.commit()


def get_paper_css(paper: str) -> dict[str, str]:
    """يرجّع قيم CSS المناسبة (size + margin + max_width) لكل مقاس."""
    paper = normalize_paper(paper)
    if paper == "A4":
        return {"size": "A4", "margin": "12mm", "max_width": "210mm"}
    if paper == "A5":
        return {"size": "A5", "margin": "10mm", "max_width": "148mm"}
    if paper == "80mm":
        return {"size": "80mm auto", "margin": "4mm 3mm", "max_width": "80mm"}
    if paper == "58mm":
        return {"size": "58mm auto", "margin": "3mm 2mm", "max_width": "58mm"}
    return {"size": "A5", "margin": "10mm", "max_width": "148mm"}
