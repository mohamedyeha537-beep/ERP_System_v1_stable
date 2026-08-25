"""تحديد نوع المستند المطبوع (إيصال قبض / فاتورة) حسب المجال والحالة."""
from __future__ import annotations

from modules.hotel.booking_models import BookingStatus, HotelBooking
from modules.printing.doc_numbers import PrintDocKind, ensure_doc_number
from modules.sales.models import Sale
from sqlalchemy.orm import Session


def resolve_hotel_doc_kind(
    booking: HotelBooking,
    *,
    requested: str | None = None,
    payment_id: int | None = None,
) -> PrintDocKind:
    """
    - طالما النزيل لم يغادر → إيصال قبض فقط (حتى لو طُلبت فاتورة)
    - بعد تسجيل المغادرة / إقفال الحجز → فاتورة نهائية
    - أي دفعة محددة → إيصال قبض دائماً
    """
    req = (requested or "").strip().lower()
    if payment_id:
        return PrintDocKind.RECEIPT
    if booking.booking_status != BookingStatus.CHECKED_OUT:
        # لا فاتورة نهائية قبل إقفال الحجز
        return PrintDocKind.RECEIPT
    if req in ("receipt", "rcp", "ايصال", "قبض"):
        return PrintDocKind.RECEIPT
    if req in ("invoice", "final", "فاتورة", "نهائية"):
        return PrintDocKind.FINAL_INVOICE
    return PrintDocKind.FINAL_INVOICE


def assign_hotel_doc_number(
    db: Session,
    booking: HotelBooking,
    kind: PrintDocKind,
    *,
    payment=None,
) -> str:
    """يُسند رقم المستند. حفظ الأعمدة اختياري — فشل الـ commit يُعالج من المستدعي."""
    if kind == PrintDocKind.RECEIPT and payment is not None:
        num = ensure_doc_number(
            db, getattr(payment, "receipt_number", None), kind, domain="hotel"
        )
        try:
            payment.receipt_number = num
        except Exception:  # noqa: BLE001
            pass
        return num
    if kind == PrintDocKind.FINAL_INVOICE:
        num = ensure_doc_number(
            db, getattr(booking, "final_invoice_number", None), kind, domain="hotel"
        )
        try:
            booking.final_invoice_number = num
        except Exception:  # noqa: BLE001
            pass
        return num
    # إيصال على مستوى الحجز (بدون دفعة محددة) — نستخدم تسلسل الإيصالات
    return ensure_doc_number(db, None, PrintDocKind.RECEIPT, domain="hotel")


def resolve_sale_doc_kind(
    sale: Sale,
    *,
    requested: str | None = None,
    db: Session | None = None,
) -> PrintDocKind:
    """أوراق المطعم = فاتورة دائماً (لا «إيصال قبض»).

    الترقيم يبقى من تسلسل الفواتير؛ العنوان يُحدَّد عبر ``restaurant_sale_doc_title``.
    """
    _ = (requested, db, sale.status)  # reserved for callers / future filters
    return PrintDocKind.FINAL_INVOICE


def restaurant_sale_doc_title(
    sale: Sale,
    *,
    room_hint: str | None = None,
    is_room_receipt: bool = False,
) -> str:
    """عنوان ورقة المطعم المطبوعة.

    - مقيدة على شقة → «فاتورة مقيدة على شقة رقم …»
    - أي نوع آخر → «فاتورة»
    """
    from modules.sales.models import SaleContext

    hint = (room_hint or "").strip()
    on_room = bool(is_room_receipt) or getattr(sale, "context_type", None) == SaleContext.ROOM
    if on_room:
        if hint:
            return f"فاتورة مقيدة على شقة رقم {hint}"
        return "فاتورة مقيدة على شقة"
    return "فاتورة"


def assign_sale_doc_number(db: Session, sale: Sale, kind: PrintDocKind) -> str:
    if kind == PrintDocKind.FINAL_INVOICE:
        num = ensure_doc_number(
            db, getattr(sale, "final_invoice_number", None), kind, domain="restaurant"
        )
        sale.final_invoice_number = num
        return num
    num = ensure_doc_number(
        db, getattr(sale, "receipt_number", None), kind, domain="restaurant"
    )
    sale.receipt_number = num
    return num
