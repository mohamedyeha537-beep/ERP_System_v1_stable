"""عرض ملفات /uploads — عام للكتalog، محمي للمستندات الحساسة بمستوى المورد."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.deps import get_current_user, get_db_session
from app.upload_authorization import user_may_access_upload

router = APIRouter(tags=["uploads"])

_PUBLIC_PREFIXES: tuple[str, ...] = (
    "products/",
    "hotel/rooms/",
    "hotel/room_media/",
)


def _static_uploads_root() -> Path:
    return Path(__file__).resolve().parent / "static" / "uploads"


def _resolve_upload_path(relative: str) -> Path | None:
    raw = (relative or "").strip().replace("\\", "/").lstrip("/")
    if not raw or ".." in raw.split("/"):
        return None
    parts = [p for p in raw.split("/") if p and p != "."]
    if not parts:
        return None
    root = _static_uploads_root().resolve()
    fp = (root / "/".join(parts)).resolve()
    try:
        if not fp.is_relative_to(root):
            return None
    except (ValueError, OSError, AttributeError):
        # Python < 3.9 fallback (المشروع على 3.10+)
        try:
            fp.relative_to(root)
        except ValueError:
            return None
    return fp if fp.is_file() else None


def _is_public_path(relative: str) -> bool:
    rel = relative.replace("\\", "/").lstrip("/")
    return any(rel.startswith(p) for p in _PUBLIC_PREFIXES)


@router.get("/uploads/{file_path:path}")
def serve_upload(
    file_path: str,
    request: Request,
    db: Session = Depends(get_db_session),
    user=Depends(get_current_user),
):
    fp = _resolve_upload_path(file_path)
    if fp is None:
        raise HTTPException(status_code=404, detail="الملف غير موجود.")

    if _is_public_path(file_path):
        return FileResponse(fp)

    if user is None:
        raise HTTPException(status_code=401, detail="يجب تسجيل الدخول لعرض هذا الملف.")

    if not user_may_access_upload(db, user, file_path):
        # 404 وليس 403 لتقليل تسريب وجود الملف
        raise HTTPException(status_code=404, detail="الملف غير موجود.")

    return FileResponse(fp)
