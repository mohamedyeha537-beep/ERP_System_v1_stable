"""رفع صور الحملات وتحويلها لرابط عام لـ TextMeBot."""
from __future__ import annotations

from pathlib import Path

from fastapi import Request, UploadFile
from sqlalchemy.orm import Session

from modules.catalog.uploads import save_messaging_campaign_image
from modules.settings.service import get_setting

_STATIC_ROOT = Path(__file__).resolve().parents[2] / "app" / "static"
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def public_base_url(db: Session, request: Request | None) -> str:
    base = (get_setting(db, "public_base_url", "") or "").strip().rstrip("/")
    if base:
        return base
    if request is not None:
        return str(request.base_url).rstrip("/")
    return ""


def resolve_campaign_image_url(
    db: Session,
    request: Request | None,
    *,
    image_url: str = "",
    image_file: UploadFile | None = None,
) -> str:
    """رابط HTTPS/HTTP عام للصورة — من رفع محلي أو رابط خارجي."""
    uploaded = image_file is not None and bool((image_file.filename or "").strip())
    manual = (image_url or "").strip()
    if uploaded:
        ext = Path(image_file.filename or "").suffix.lower()
        if ext not in _IMAGE_EXTS:
            raise ValueError("ملف الصورة يجب أن يكون jpg أو png أو webp.")
        rel = save_messaging_campaign_image(image_file, _STATIC_ROOT)
        base = public_base_url(db, request)
        if not base:
            raise ValueError("عيّن «رابط الموقع العام» في الإعدادات لرفع الصور.")
        return f"{base}/{rel}"
    return manual
