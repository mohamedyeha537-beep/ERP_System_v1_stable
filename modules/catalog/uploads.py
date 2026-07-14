from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import UploadFile

_ALLOWED = {".jpg", ".jpeg", ".png", ".webp"}
_MAX = 2 * 1024 * 1024
_PRODUCT_UPLOAD_DIR = Path("uploads") / "products"


def product_image_public_url(image_filename: str | None) -> str | None:
    """رابط عرض صورة المنتج — /uploads/… (يمر عبر التطبيق، لا nginx static)."""
    raw = (image_filename or "").strip().replace("\\", "/")
    if not raw:
        return None
    if raw.startswith(("http://", "https://")):
        return raw
    if raw.startswith("/uploads/"):
        return raw
    if raw.startswith("/static/"):
        raw = raw[len("/static/") :]
    elif raw.startswith("/"):
        raw = raw.lstrip("/")
    if raw.startswith("uploads/"):
        return f"/{raw}"
    if raw.startswith("products/"):
        return f"/uploads/{raw}"
    return f"/uploads/products/{raw}"


def resolve_product_image_path(static_root: Path, image_filename: str | None) -> Path | None:
    """مسار الملف على القرص إن وُجد."""
    raw = (image_filename or "").strip().replace("\\", "/")
    if not raw or raw.startswith(("http://", "https://")):
        return None
    if raw.startswith("/static/"):
        raw = raw[len("/static/") :]
    elif raw.startswith("/uploads/"):
        raw = raw[len("/uploads/") :]
    elif raw.startswith("/"):
        raw = raw.lstrip("/")
    if raw.startswith("uploads/"):
        raw = raw[len("uploads/") :]
    parts = [p for p in raw.split("/") if p and p != "."]
    if any(p == ".." for p in parts):
        return None
    fp = (static_root / "uploads" / "/".join(parts)).resolve()
    static_root_resolved = static_root.resolve()
    try:
        if not str(fp).startswith(str(static_root_resolved)):
            return None
    except (ValueError, OSError):
        return None
    return fp if fp.is_file() else None


def save_product_image(upload: UploadFile, static_root: Path) -> str:
    if not upload.filename:
        raise ValueError("لا يوجد اسم ملف.")
    ext = Path(upload.filename).suffix.lower()
    if ext not in _ALLOWED:
        raise ValueError("نوع الصورة غير مسموح (jpg, png, webp).")
    data = upload.file.read()
    if len(data) > _MAX:
        raise ValueError("حجم الصورة يتجاوز 2 ميجابايت.")
    rel_dir = _PRODUCT_UPLOAD_DIR
    out_dir = static_root / rel_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{uuid.uuid4().hex}{ext}"
    out_path = out_dir / fname
    out_path.write_bytes(data)
    return str(rel_dir / fname).replace("\\", "/")


def save_sale_payment_proof(upload: UploadFile, static_root: Path) -> str:
    """صورة إيصال تحويل مصرفي مرفقة بدفعة بيع — تحت static/uploads/sale_payments/."""
    if not upload.filename:
        raise ValueError("لا يوجد اسم ملف.")
    ext = Path(upload.filename).suffix.lower()
    if ext not in _ALLOWED:
        raise ValueError("نوع الصورة غير مسموح (jpg, png, webp).")
    data = upload.file.read()
    if len(data) > _MAX:
        raise ValueError("حجم الصورة يتجاوز 2 ميجابايت.")
    rel_dir = Path("uploads") / "sale_payments"
    out_dir = static_root / rel_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{uuid.uuid4().hex}{ext}"
    out_path = out_dir / fname
    out_path.write_bytes(data)
    return str(rel_dir / fname).replace("\\", "/")


def save_purchase_invoice_image(upload: UploadFile, static_root: Path) -> str:
    """صورة فاتورة المورّد — تحت static/uploads/purchases/supplier_invoices/."""
    if not upload.filename:
        raise ValueError("لا يوجد اسم ملف.")
    ext = Path(upload.filename).suffix.lower()
    if ext not in _ALLOWED:
        raise ValueError("نوع الصورة غير مسموح (jpg, png, webp).")
    data = upload.file.read()
    if len(data) > _MAX:
        raise ValueError("حجم الصورة يتجاوز 2 ميجابايت.")
    rel_dir = Path("uploads") / "purchases" / "supplier_invoices"
    out_dir = static_root / rel_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{uuid.uuid4().hex}{ext}"
    out_path = out_dir / fname
    out_path.write_bytes(data)
    return str(rel_dir / fname).replace("\\", "/")


def save_purchase_bank_payment_receipt(upload: UploadFile, static_root: Path) -> str:
    """إيصال إثبات دفع مصرفي لفاتورة شراء — تحت static/uploads/purchases/payment_receipts/."""
    if not upload.filename:
        raise ValueError("لا يوجد اسم ملف.")
    ext = Path(upload.filename).suffix.lower()
    if ext not in _ALLOWED:
        raise ValueError("نوع الصورة غير مسموح (jpg, png, webp).")
    data = upload.file.read()
    if len(data) > _MAX:
        raise ValueError("حجم الصورة يتجاوز 2 ميجابايت.")
    rel_dir = Path("uploads") / "purchases" / "payment_receipts"
    out_dir = static_root / rel_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{uuid.uuid4().hex}{ext}"
    out_path = out_dir / fname
    out_path.write_bytes(data)
    return str(rel_dir / fname).replace("\\", "/")


def save_web_chat_guide(upload: UploadFile, static_root: Path) -> str:
    """ملفات شرح للبوت (PDF أو صورة) — static/uploads/web_chat_guides/."""
    if not upload.filename:
        raise ValueError("لا يوجد اسم ملف.")
    ext = Path(upload.filename).suffix.lower()
    allowed = {".pdf", ".jpg", ".jpeg", ".png", ".webp"}
    if ext not in allowed:
        raise ValueError("نوع الملف غير مسموح (pdf, jpg, png, webp).")
    data = upload.file.read()
    max_size = 5 * 1024 * 1024 if ext == ".pdf" else _MAX
    if len(data) > max_size:
        raise ValueError("حجم الملف كبير جداً.")
    rel_dir = Path("uploads") / "web_chat_guides"
    out_dir = static_root / rel_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{uuid.uuid4().hex}{ext}"
    out_path = out_dir / fname
    out_path.write_bytes(data)
    return str(rel_dir / fname).replace("\\", "/")


def save_notification_attachment(upload: UploadFile, static_root: Path) -> str:
    """مرفق قالب إشعار — صورة أو PDF تحت static/uploads/notifications/."""
    if not upload.filename:
        raise ValueError("لا يوجد اسم ملف.")
    ext = Path(upload.filename).suffix.lower()
    allowed = {".pdf", ".jpg", ".jpeg", ".png", ".webp"}
    if ext not in allowed:
        raise ValueError("نوع الملف غير مسموح (pdf, jpg, png, webp).")
    data = upload.file.read()
    max_size = 5 * 1024 * 1024 if ext == ".pdf" else _MAX
    if len(data) > max_size:
        raise ValueError("حجم الملف كبير جداً.")
    rel_dir = Path("uploads") / "notifications"
    out_dir = static_root / rel_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{uuid.uuid4().hex}{ext}"
    out_path = out_dir / fname
    out_path.write_bytes(data)
    return str(rel_dir / fname).replace("\\", "/")


def save_messaging_campaign_image(upload: UploadFile, static_root: Path) -> str:
    """صورة حملة مراسلات — static/uploads/messaging/campaigns/."""
    if not upload.filename:
        raise ValueError("لا يوجد اسم ملف.")
    ext = Path(upload.filename).suffix.lower()
    if ext not in _ALLOWED:
        raise ValueError("نوع الصورة غير مسموح (jpg, png, webp).")
    data = upload.file.read()
    if len(data) > _MAX:
        raise ValueError("حجم الصورة يتجاوز 2 ميجابايت.")
    rel_dir = Path("uploads") / "messaging" / "campaigns"
    out_dir = static_root / rel_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{uuid.uuid4().hex}{ext}"
    out_path = out_dir / fname
    out_path.write_bytes(data)
    return str(rel_dir / fname).replace("\\", "/")


def save_payment_method_icon(upload: UploadFile, static_root: Path) -> str:
    """أيقونة وسيلة الدفع في نقطة البيع — static/uploads/payment_methods/."""
    if not upload.filename:
        raise ValueError("لا يوجد اسم ملف.")
    ext = Path(upload.filename).suffix.lower()
    if ext not in _ALLOWED:
        raise ValueError("نوع الصورة غير مسموح (jpg, png, webp).")
    data = upload.file.read()
    if len(data) > _MAX:
        raise ValueError("حجم الصورة يتجاوز 2 ميجابايت.")
    rel_dir = Path("uploads") / "payment_methods"
    out_dir = static_root / rel_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{uuid.uuid4().hex}{ext}"
    out_path = out_dir / fname
    out_path.write_bytes(data)
    return str(rel_dir / fname).replace("\\", "/")


def delete_stored_relative_file(static_root: Path, relative: str | None) -> None:
    rel = (relative or "").strip().replace("\\", "/")
    if not rel:
        return
    if rel.startswith("/"):
        rel = rel.lstrip("/")
    parts = [p for p in rel.split("/") if p and p != "."]
    if any(p == ".." for p in parts):
        return
    fp = (static_root / "/".join(parts)).resolve()
    static_root_resolved = static_root.resolve()
    try:
        if not str(fp).startswith(str(static_root_resolved)):
            return
    except (ValueError, OSError):
        return
    if fp.is_file():
        try:
            fp.unlink()
        except OSError:
            pass
