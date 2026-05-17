from __future__ import annotations

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


def delete_stored_relative_file(static_root: Path, relative: str | None) -> None:
    rel = (relative or "").strip()
    if not rel:
        return
    fp = static_root.joinpath(*rel.split("/"))
    if fp.is_file():
        try:
            fp.unlink()
        except OSError:
            pass
