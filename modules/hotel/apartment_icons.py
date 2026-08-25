"""أيقونات كروت خريطة الشقق — إيموجي أو صورة مخصّصة من الإعدادات."""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from fastapi import UploadFile
from sqlalchemy.orm import Session

from modules.settings.service import get_setting, set_setting

_ICON_KEYS = ("available", "occupied", "dirty", "maint")
_DEFAULT_EMOJI: dict[str, str] = {
    "available": "🏠",
    "occupied": "🛏",
    "dirty": "🧹",
    "maint": "🔧",
}
_EMOJI_SETTING = "hotel_apt_icon_emoji_{key}"
_IMG_SETTING = "hotel_apt_icon_img_{key}"
_MAX_BYTES = 512 * 1024
_ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"}


class ApartmentIconError(Exception):
    pass


def _uploads_dir() -> Path:
    from modules.branding.service import _logo_dir

    d = _logo_dir() / "apartment_icons"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _public_url(filename: str) -> str:
    fn = (filename or "").strip()
    if not fn:
        return ""
    return f"/static/uploads/branding/apartment_icons/{fn}"


def _emoji_for(db: Session, key: str) -> str:
    raw = (get_setting(db, _EMOJI_SETTING.format(key=key), "") or "").strip()
    if raw:
        return raw[:8]
    return _DEFAULT_EMOJI.get(key, "🏠")


def _image_for(db: Session, key: str) -> str:
    return (get_setting(db, _IMG_SETTING.format(key=key), "") or "").strip()


def apartment_icons_context(db: Session) -> dict[str, dict[str, str]]:
    """قاموس للأيقونات يُمرَّر للقوالب: available/occupied/dirty/maint."""
    out: dict[str, dict[str, str]] = {}
    for key in _ICON_KEYS:
        fn = _image_for(db, key)
        out[key] = {
            "emoji": _emoji_for(db, key),
            "image_url": _public_url(fn) if fn else "",
            "filename": fn,
        }
    return out


def resolve_apartment_icon_key(
    *,
    is_maint: bool,
    needs_clean: bool,
    is_occupied: bool,
    is_reserved: bool,
) -> str:
    if is_maint:
        return "maint"
    if needs_clean:
        return "dirty"
    if is_occupied or is_reserved:
        return "occupied"
    return "available"


def save_apartment_icon_emojis(db: Session, values: dict[str, str]) -> None:
    for key in _ICON_KEYS:
        emoji = (values.get(key) or "").strip()[:8] or _DEFAULT_EMOJI[key]
        set_setting(db, _EMOJI_SETTING.format(key=key), emoji)


async def save_apartment_icon_upload(
    db: Session,
    *,
    key: str,
    upload: UploadFile | None,
    clear: bool = False,
) -> None:
    if key not in _ICON_KEYS:
        raise ApartmentIconError("مفتاح أيقونة غير صالح.")
    setting_key = _IMG_SETTING.format(key=key)
    if clear:
        set_setting(db, setting_key, "")
        return
    if upload is None or not getattr(upload, "filename", None):
        return
    name = Path(upload.filename or "").name
    ext = Path(name).suffix.lower()
    if ext not in _ALLOWED_EXT:
        raise ApartmentIconError("صيغة الصورة غير مدعومة (png/jpg/webp/gif/svg).")
    data = await upload.read()
    if not data:
        return
    if len(data) > _MAX_BYTES:
        raise ApartmentIconError("حجم الأيقونة أكبر من 512KB.")
    dest = _uploads_dir() / f"apt_{key}_{uuid.uuid4().hex[:10]}{ext}"
    dest.write_bytes(data)
    set_setting(db, setting_key, dest.name)


def apartment_icons_admin_labels() -> list[tuple[str, str]]:
    return [
        ("available", "متاحة"),
        ("occupied", "مشغولة / محجوزة"),
        ("dirty", "للتنظيف"),
        ("maint", "صيانة"),
    ]


def apartment_icons_defaults() -> dict[str, Any]:
    return {
        k: {"emoji": v, "image_url": "", "filename": ""}
        for k, v in _DEFAULT_EMOJI.items()
    }


# ── أيقونات محتويات الشقة على الكرت (غرف / أسرة / رضيع) ──
_AMENITY_KEYS = ("rooms", "double", "single", "infant")
_DEFAULT_AMENITY_EMOJI: dict[str, str] = {
    "rooms": "🚪",
    "double": "🛏",
    "single": "🛌",
    "infant": "👶",
}
_AMENITY_EMOJI_SETTING = "hotel_amenity_icon_emoji_{key}"
_AMENITY_IMG_SETTING = "hotel_amenity_icon_img_{key}"
# خيارات سريعة للاختيار من نموذج التعديل
AMENITY_EMOJI_CHOICES: dict[str, tuple[str, ...]] = {
    "rooms": ("🚪", "🏠", "🔑", "🛋", "🏢", "🪟"),
    "double": ("🛏", "🛏️", "💑", "💕", "👑", "✨"),
    "single": ("🛌", "🛏️", "👤", "🛋", "💤", "🌙"),
    "infant": ("👶", "🍼", "🧸", "🚼", "💗", "🎀"),
}


def amenity_icons_context(db: Session) -> dict[str, dict[str, str]]:
    """رموز محتويات الشقة: emoji و/أو صورة مرفوعة."""
    out: dict[str, dict[str, str]] = {}
    for key in _AMENITY_KEYS:
        raw = (get_setting(db, _AMENITY_EMOJI_SETTING.format(key=key), "") or "").strip()
        fn = (get_setting(db, _AMENITY_IMG_SETTING.format(key=key), "") or "").strip()
        out[key] = {
            "emoji": (raw[:8] if raw else _DEFAULT_AMENITY_EMOJI[key]),
            "image_url": _public_url(fn) if fn else "",
            "filename": fn,
        }
    return out


def save_amenity_icon_emojis(db: Session, values: dict[str, str]) -> None:
    for key in _AMENITY_KEYS:
        emoji = (values.get(key) or "").strip()[:8] or _DEFAULT_AMENITY_EMOJI[key]
        set_setting(db, _AMENITY_EMOJI_SETTING.format(key=key), emoji)


async def save_amenity_icon_upload(
    db: Session,
    *,
    key: str,
    upload: UploadFile | None,
    clear: bool = False,
) -> None:
    if key not in _AMENITY_KEYS:
        raise ApartmentIconError("مفتاح أيقونة محتوى غير صالح.")
    setting_key = _AMENITY_IMG_SETTING.format(key=key)
    if clear:
        set_setting(db, setting_key, "")
        return
    if upload is None or not getattr(upload, "filename", None):
        return
    name = Path(upload.filename or "").name
    ext = Path(name).suffix.lower()
    if ext not in _ALLOWED_EXT:
        raise ApartmentIconError("صيغة الصورة غير مدعومة (png/jpg/webp/gif/svg).")
    data = await upload.read()
    if not data:
        return
    if len(data) > _MAX_BYTES:
        raise ApartmentIconError("حجم الأيقونة أكبر من 512KB.")
    dest = _uploads_dir() / f"amenity_{key}_{uuid.uuid4().hex[:10]}{ext}"
    dest.write_bytes(data)
    set_setting(db, setting_key, dest.name)


def amenity_icons_admin_labels() -> list[tuple[str, str]]:
    return [
        ("rooms", "عدد الغرف"),
        ("double", "أسرة زوجية"),
        ("single", "أسرة فردية"),
        ("infant", "مناسب لرضيع"),
    ]


def amenity_icons_defaults() -> dict[str, dict[str, str]]:
    return {
        k: {"emoji": v, "image_url": "", "filename": ""}
        for k, v in _DEFAULT_AMENITY_EMOJI.items()
    }
