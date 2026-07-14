from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import UploadFile

_ALLOWED = {".jpg", ".jpeg", ".png", ".webp"}
_MAX = 2 * 1024 * 1024
_UPLOAD_DIR = Path("hotel") / "rooms"


def room_image_public_url(image_filename: str | None) -> str | None:
    raw = (image_filename or "").strip().replace("\\", "/")
    if not raw:
        return None
    if raw.startswith(("http://", "https://")):
        return raw
    if raw.startswith("/uploads/"):
        return raw
    if raw.startswith("hotel/"):
        return f"/uploads/{raw}"
    return f"/uploads/hotel/rooms/{raw}"


def save_room_image(upload: UploadFile, static_root: Path) -> str:
    if not upload or not upload.filename:
        raise ValueError("لم تُرفع صورة.")
    ext = Path(upload.filename).suffix.lower()
    if ext not in _ALLOWED:
        raise ValueError("صيغة الصورة غير مدعومة (jpg, png, webp).")
    data = upload.file.read()
    if len(data) > _MAX:
        raise ValueError("حجم الصورة أكبر من 2 ميغابايت.")
    dest_dir = static_root / "uploads" / _UPLOAD_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{uuid.uuid4().hex}{ext}"
    path = dest_dir / fname
    path.write_bytes(data)
    return f"hotel/rooms/{fname}"


_MEDIA_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
_MEDIA_VIDEO_EXT = {".mp4", ".webm"}
_MEDIA_IMAGE_MAX = 3 * 1024 * 1024
_MEDIA_VIDEO_MAX = 25 * 1024 * 1024
_MEDIA_DIR = Path("hotel") / "room_media"


def room_media_public_url(filename: str | None) -> str | None:
    raw = (filename or "").strip().replace("\\", "/")
    if not raw:
        return None
    if raw.startswith(("http://", "https://")):
        return raw
    if raw.startswith("/uploads/"):
        return raw
    if raw.startswith("hotel/"):
        return f"/uploads/{raw}"
    return f"/uploads/hotel/room_media/{raw}"


def save_room_media(upload: UploadFile, static_root: Path, *, kind: str) -> str:
    if not upload or not upload.filename:
        raise ValueError("لم يُرفع ملف.")
    ext = Path(upload.filename).suffix.lower()
    is_video = kind.upper() == "VIDEO"
    allowed = _MEDIA_VIDEO_EXT if is_video else _MEDIA_IMAGE_EXT
    max_bytes = _MEDIA_VIDEO_MAX if is_video else _MEDIA_IMAGE_MAX
    if ext not in allowed:
        raise ValueError("صيغة الملف غير مدعومة.")
    data = upload.file.read()
    if len(data) > max_bytes:
        raise ValueError("حجم الملف كبير جداً.")
    dest_dir = static_root / "uploads" / _MEDIA_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{uuid.uuid4().hex}{ext}"
    path = dest_dir / fname
    path.write_bytes(data)
    return f"hotel/room_media/{fname}"


_GUEST_DOC_EXT = {".jpg", ".jpeg", ".png", ".webp", ".pdf"}
_GUEST_DOC_MAX = 3 * 1024 * 1024
_GUEST_DOC_DIR = Path("hotel") / "guest_documents"


def guest_id_document_public_url(filename: str | None) -> str | None:
    raw = (filename or "").strip().replace("\\", "/")
    if not raw:
        return None
    if raw.startswith(("http://", "https://")):
        return raw
    if raw.startswith("/uploads/"):
        return raw
    if raw.startswith("hotel/"):
        return f"/uploads/{raw}"
    return f"/uploads/hotel/guest_documents/{raw}"


def save_guest_id_document(upload: UploadFile, static_root: Path) -> str:
    if not upload or not upload.filename:
        raise ValueError("لم تُرفع وثيقة.")
    ext = Path(upload.filename).suffix.lower()
    if ext not in _GUEST_DOC_EXT:
        raise ValueError("صيغة الوثيقة غير مدعومة (jpg, png, webp, pdf).")
    data = upload.file.read()
    if len(data) > _GUEST_DOC_MAX:
        raise ValueError("حجم الوثيقة أكبر من 3 ميغابايت.")
    dest_dir = static_root / "uploads" / _GUEST_DOC_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{uuid.uuid4().hex}{ext}"
    path = dest_dir / fname
    path.write_bytes(data)
    return f"hotel/guest_documents/{fname}"
