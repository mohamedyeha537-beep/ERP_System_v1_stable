"""ملفات الشرح القابلة للرفع — يستدعيها Hermes في المحادثة."""
from __future__ import annotations

from sqlalchemy.orm import Session

from modules.settings.service import get_setting

GUIDE_KEYS = {
    "loyalty": "web_chat_guide_loyalty_file",
    "referral": "web_chat_guide_referral_file",
    "general": "web_chat_guide_general_file",
}

GUIDE_LABELS = {
    "loyalty": "دليل برنامج الولاء",
    "referral": "دليل كود الإحالة",
    "general": "دليل الخدمة",
}


def guide_setting_key(guide_type: str) -> str | None:
    return GUIDE_KEYS.get((guide_type or "").strip().lower())


def get_guide_url(db: Session, guide_type: str) -> str | None:
    key = guide_setting_key(guide_type)
    if not key:
        return None
    fn = (get_setting(db, key, "") or "").strip()
    if not fn:
        return None
    return f"/static/{fn.replace(chr(92), '/')}"


def format_guide_download_lines(db: Session, *guide_types: str) -> list[str]:
    lines: list[str] = []
    for gt in guide_types:
        url = get_guide_url(db, gt)
        if not url:
            continue
        label = GUIDE_LABELS.get(gt, gt)
        lines.append(f"📥 {label}: {url}")
    return lines


def append_guides_footer(db: Session, body: str, *guide_types: str) -> str:
    links = format_guide_download_lines(db, *guide_types)
    if not links:
        return body
    return body + "\n\n" + "\n".join(links)
