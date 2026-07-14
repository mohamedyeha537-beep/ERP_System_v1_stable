"""بانرات إعلانية لمتجر الشقق /suites."""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from fastapi import UploadFile
from sqlalchemy.orm import Session

from modules.branding.service import _logo_dir
from modules.settings.service import get_setting, set_setting

_SETTING_JSON = "brand_hotel_banners_json"
_SETTING_INTERVAL = "brand_hotel_banner_interval_seconds"
_DEFAULT_INTERVAL = 10
_MAX_BANNERS = 8
_MAX_BYTES = 2 * 1024 * 1024
_ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp"}


class HotelBannerError(Exception):
    pass


def _banner_url(filename: str) -> str:
    fn = (filename or "").strip()
    if not fn:
        return ""
    return f"/static/uploads/branding/{fn}"


def _load_raw(db: Session) -> list[dict[str, Any]]:
    raw = (get_setting(db, _SETTING_JSON, "[]") or "[]").strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    out: list[dict[str, Any]] = []
    for item in data[:_MAX_BANNERS]:
        if not isinstance(item, dict):
            continue
        fn = str(item.get("filename") or "").strip()
        if not fn:
            continue
        out.append(
            {
                "id": str(item.get("id") or fn),
                "filename": fn,
                "link_url": str(item.get("link_url") or "").strip()[:500],
            }
        )
    return out


def _save_raw(db: Session, items: list[dict[str, Any]]) -> None:
    set_setting(db, _SETTING_JSON, json.dumps(items[:_MAX_BANNERS], ensure_ascii=False))


def banner_interval_seconds(db: Session) -> int:
    raw = (get_setting(db, _SETTING_INTERVAL, str(_DEFAULT_INTERVAL)) or "").strip()
    try:
        n = int(raw)
    except ValueError:
        n = _DEFAULT_INTERVAL
    return max(3, min(120, n))


def set_banner_interval(db: Session, seconds: int) -> None:
    set_setting(db, _SETTING_INTERVAL, str(max(3, min(120, int(seconds)))))


def get_hotel_banners_admin(db: Session) -> dict[str, Any]:
    items = []
    for row in _load_raw(db):
        items.append(
            {
                "id": row["id"],
                "filename": row["filename"],
                "image_url": _banner_url(row["filename"]),
                "link_url": row.get("link_url") or "",
            }
        )
    return {"interval_seconds": banner_interval_seconds(db), "banners": items}


def get_hotel_banners_public(db: Session) -> dict[str, Any]:
    items = []
    for row in _load_raw(db):
        url = _banner_url(row["filename"])
        if not url:
            continue
        items.append({"image_url": url, "link_url": row.get("link_url") or ""})
    return {"interval_seconds": banner_interval_seconds(db), "items": items}


def add_hotel_banner(
    db: Session,
    *,
    upload: UploadFile,
    link_url: str = "",
) -> dict[str, Any]:
    items = _load_raw(db)
    if len(items) >= _MAX_BANNERS:
        raise HotelBannerError(f"الحد الأقصى {_MAX_BANNERS} بانرات.")
    if not upload.filename:
        raise HotelBannerError("لم يُحدَّد ملف.")
    content = upload.file.read()
    if not content:
        raise HotelBannerError("الملف فارغ.")
    if len(content) > _MAX_BYTES:
        raise HotelBannerError("حجم الصورة يتجاوز 2 م.ب.")
    ext = Path(upload.filename).suffix.lower()
    if ext not in _ALLOWED_EXT:
        raise HotelBannerError("نوع الصورة غير مدعوم (jpg, png, webp).")
    fname = f"hotel-banner-{uuid.uuid4().hex[:12]}{ext}"
    path = _logo_dir() / fname
    path.write_bytes(content)
    row = {
        "id": uuid.uuid4().hex[:12],
        "filename": fname,
        "link_url": (link_url or "").strip()[:500],
    }
    items.append(row)
    _save_raw(db, items)
    return row


def delete_hotel_banner(db: Session, banner_id: str) -> bool:
    bid = (banner_id or "").strip()
    if not bid:
        return False
    items = _load_raw(db)
    kept: list[dict[str, Any]] = []
    removed = False
    for row in items:
        if row["id"] == bid:
            removed = True
            fp = _logo_dir() / row["filename"]
            try:
                if fp.is_file():
                    fp.unlink()
            except OSError:
                pass
        else:
            kept.append(row)
    if removed:
        _save_raw(db, kept)
    return removed
