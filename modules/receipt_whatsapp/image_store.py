"""حفظ صورة الفاتورة للإرسال عبر واتساب."""
from __future__ import annotations

import base64
import re
import uuid
from pathlib import Path

MAX_IMAGE_BYTES = 2_000_000

_DATA_URL_RE = re.compile(r"^data:image/(?:png|jpeg|jpg);base64,", re.I)


def _uploads_dir() -> Path:
    root = Path(__file__).resolve().parents[2] / "app" / "static" / "uploads" / "receipts"
    root.mkdir(parents=True, exist_ok=True)
    return root


def save_receipt_png_data_url(data_url: str) -> tuple[str, bytes]:
    """يُرجع (اسم الملف، المحتوى الثنائي)."""
    raw = (data_url or "").strip()
    if not raw:
        raise ValueError("صورة الفاتورة فارغة.")
    if _DATA_URL_RE.match(raw):
        raw = _DATA_URL_RE.sub("", raw, count=1)
    try:
        blob = base64.b64decode(raw, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("صورة الفاتورة غير صالحة.") from exc
    if len(blob) < 100:
        raise ValueError("صورة الفاتورة صغيرة جداً.")
    if len(blob) > MAX_IMAGE_BYTES:
        raise ValueError("حجم صورة الفاتورة كبير — حدّث الصفحة وحاول مجدداً.")
    name = f"wa_{uuid.uuid4().hex}.png"
    path = _uploads_dir() / name
    path.write_bytes(blob)
    return name, blob
