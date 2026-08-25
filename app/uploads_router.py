"""عرض ملفات /uploads — عام للكتalog، محمي للإيصالات والمستندات."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from app.deps import get_current_user

router = APIRouter(tags=["uploads"])

# مسارات يمكن للزائر/المتجر رؤيتها بدون تسجيل دخول
_PUBLIC_PREFIXES: tuple[str, ...] = (
    "products/",
    "hotel/",
    "hotel/rooms/",
    "hotel/room_media/",
)

_SENSITIVE_PREFIXES: tuple[str, ...] = (
    "sale_payments/",
    "purchases/",
    "hotel/guest_documents/",
    "hotel/agreement_requests/",
    "receipts/",
    "messaging/",
    "notifications/",
    "web_chat_guides/",
    "payment_methods/",
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
        if not str(fp).startswith(str(root)):
            return None
    except (ValueError, OSError):
        return None
    return fp if fp.is_file() else None


def _is_public_path(relative: str) -> bool:
    rel = relative.replace("\\", "/").lstrip("/")
    return any(rel.startswith(p) for p in _PUBLIC_PREFIXES)


def _is_sensitive_path(relative: str) -> bool:
    rel = relative.replace("\\", "/").lstrip("/")
    if _is_public_path(rel):
        return False
    if any(rel.startswith(p) for p in _SENSITIVE_PREFIXES):
        return True
    # أي مسار غير معروف — يتطلب تسجيل دخول
    return True


@router.get("/uploads/{file_path:path}")
def serve_upload(
    file_path: str,
    request: Request,
    user=Depends(get_current_user),
):
    fp = _resolve_upload_path(file_path)
    if fp is None:
        raise HTTPException(status_code=404, detail="الملف غير موجود.")
    if _is_sensitive_path(file_path) and user is None:
        raise HTTPException(status_code=401, detail="يجب تسجيل الدخول لعرض هذا الملف.")
    return FileResponse(fp)
