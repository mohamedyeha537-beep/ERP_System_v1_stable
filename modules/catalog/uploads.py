from __future__ import annotations

import re
import uuid
from pathlib import Path

from fastapi import UploadFile

_ALLOWED = {".jpg", ".jpeg", ".png", ".webp"}
_MAX = 2 * 1024 * 1024


def save_product_image(upload: UploadFile, static_root: Path) -> str:
    if not upload.filename:
        raise ValueError("لا يوجد اسم ملف.")
    ext = Path(upload.filename).suffix.lower()
    if ext not in _ALLOWED:
        raise ValueError("نوع الصورة غير مسموح (jpg, png, webp).")
    data = upload.file.read()
    if len(data) > _MAX:
        raise ValueError("حجم الصورة يتجاوز 2 ميجابايت.")
    rel_dir = Path("uploads") / "products"
    out_dir = static_root / rel_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{uuid.uuid4().hex}{ext}"
    out_path = out_dir / fname
    out_path.write_bytes(data)
    return str(rel_dir / fname).replace("\\", "/")
