"""إرسال فاتورة المطعم/الفندق عبر واتساب."""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy.orm import Session

from modules.branding.service import hotel_display_name
from modules.customers.service import normalize_phone, require_valid_phone
from modules.hotel.booking_models import GuestType, HotelBooking
from modules.messaging.models import MessageChannel, MessageOutboxStatus
from modules.messaging.outbox import enqueue_message, send_outbox_item_now
from modules.receipt_whatsapp.image_store import save_receipt_png_data_url
from modules.sales.models import Sale
from modules.settings.service import get_bool, get_setting


class ReceiptWhatsAppError(Exception):
    pass


POS_SETTING = "pos_send_receipt_whatsapp"
HOTEL_SETTING = "hotel_send_receipt_whatsapp"


def pos_whatsapp_receipt_enabled(db: Session) -> bool:
    return get_bool(db, POS_SETTING, False) and get_bool(db, "messaging_enabled", False)


def hotel_whatsapp_receipt_enabled(db: Session) -> bool:
    return get_bool(db, HOTEL_SETTING, False) and get_bool(db, "messaging_enabled", False)


def public_asset_url(db: Session, rel_static_path: str) -> str:
    base = (get_setting(db, "public_base_url") or "").strip().rstrip("/")
    path = (rel_static_path or "").strip()
    if not path.startswith("/"):
        path = "/" + path
    if base:
        return f"{base}{path}"
    return path


def phone_for_sale(db: Session, sale: Sale) -> str:
    customer = sale.customer
    if customer is None and sale.customer_id:
        from modules.customers.service import get_customer

        customer = get_customer(db, sale.customer_id)
    if customer is None or not (customer.phone or "").strip():
        raise ReceiptWhatsAppError("لا يوجد رقم واتساب للزبون — أدخل الهاتف عند الطلب.")
    return require_valid_phone(customer.phone)


def phone_for_booking(booking: HotelBooking) -> str:
    raw = ""
    if booking.guest_type == GuestType.COMPANY:
        raw = (booking.company_contact_phone or booking.guest_phone or "").strip()
    else:
        raw = (booking.guest_phone or "").strip()
    if not raw:
        raise ReceiptWhatsAppError("لا يوجد رقم واتساب للنزيل — أدخل الهاتف في الحجز.")
    return require_valid_phone(raw)


def _format_money(amount: Decimal | float | str) -> str:
    try:
        val = Decimal(str(amount or 0)).quantize(Decimal("0.001"))
    except Exception:  # noqa: BLE001
        val = Decimal("0")
    return f"{val:.3f}"


def message_for_sale(db: Session, sale: Sale) -> str:
    store = get_setting(db, "store_name", "نقطة البيع") or "نقطة البيع"
    name = ""
    if sale.customer and (sale.customer.name or "").strip():
        name = sale.customer.name.strip()
    greeting = f"مرحباً {name}،\n" if name else "مرحباً،\n"
    paid = Decimal(str(getattr(sale, "total", 0) or 0))
    try:
        from modules.payments.service import get_sale_payment

        sp = get_sale_payment(db, sale.id)
        if sp is not None and getattr(sp, "amount", None) is not None:
            paid = Decimal(str(sp.amount))
    except Exception:  # noqa: BLE001
        pass
    total = Decimal(str(sale.total or 0))
    balance = (total - paid).quantize(Decimal("0.001"))
    if balance < 0:
        balance = Decimal("0")
    return (
        f"{greeting}"
        f"فاتورتكم #{sale.id}\n"
        f"الإجمالي: {_format_money(total)} د.ل\n"
        f"المدفوع: {_format_money(paid)} د.ل\n"
        f"المتبقي: {_format_money(balance)} د.ل\n"
        f"— {store}"
    )


def message_for_booking(
    db: Session,
    booking: HotelBooking,
    *,
    folio_total: Decimal,
    folio_paid: Decimal | None = None,
    folio_balance: Decimal | None = None,
    folio_lines: str | None = None,
    focus_payment_amount: Decimal | None = None,
    doc_title: str = "فاتورة حجز",
) -> str:
    store = hotel_display_name(db)
    name = (booking.guest_name or "").strip() or "ضيفنا"
    paid = folio_paid
    balance = folio_balance
    lines_txt = (folio_lines or "").strip()
    if paid is None or balance is None or not lines_txt:
        try:
            from modules.hotel.folio import build_folio

            folio = build_folio(db, booking.id)
            if paid is None:
                paid = Decimal(str(folio.paid or 0))
            if balance is None:
                balance = Decimal(str(folio.balance or 0))
            if not lines_txt:
                bits: list[str] = []
                for line in list(getattr(folio, "lines", None) or [])[:12]:
                    desc = (getattr(line, "description", None) or "").strip() or "بند"
                    bits.append(f"• {desc}: {_format_money(getattr(line, 'amount', 0))} د.ل")
                lines_txt = "\n".join(bits)
            folio_total = Decimal(str(folio.total or folio_total or 0))
        except Exception:  # noqa: BLE001
            paid = paid if paid is not None else Decimal("0")
            balance = balance if balance is not None else Decimal("0")
    parts = [
        f"مرحباً {name}،",
        f"*{doc_title}* — {store}",
        f"رقم الحجز: {booking.reference}",
    ]
    if focus_payment_amount is not None and Decimal(str(focus_payment_amount)) > 0:
        parts.append(f"مبلغ مستلم الآن: {_format_money(focus_payment_amount)} د.ل")
    if lines_txt:
        parts.append("تفاصيل الحساب:")
        parts.append(lines_txt)
    parts.extend(
        [
            f"الإجمالي: {_format_money(folio_total)} د.ل",
            f"المدفوع: {_format_money(paid)} د.ل",
            f"المتبقي: {_format_money(balance)} د.ل",
        ]
    )
    return "\n".join(parts)


def _image_public_url(db: Session, image_png_b64: str | None) -> str | None:
    if not (image_png_b64 or "").strip():
        return None
    filename, _ = save_receipt_png_data_url(image_png_b64)
    rel = f"/static/uploads/receipts/{filename}"
    url = public_asset_url(db, rel)
    if not (get_setting(db, "public_base_url") or "").strip():
        return None
    return url


def dispatch_whatsapp(
    db: Session,
    *,
    phone: str,
    body: str,
    image_url: str | None = None,
    customer_id: int | None = None,
    event_type: str,
) -> None:
    if not get_bool(db, "messaging_enabled", False):
        raise ReceiptWhatsAppError("المراسلة غير مفعّلة — راجع إعدادات المراسلة.")
    meta: dict = {"kind": "receipt_whatsapp"}
    if image_url:
        meta["image_url"] = image_url
    row = enqueue_message(
        db,
        body=body[:4000],
        channel=MessageChannel.WHATSAPP.value,
        phone=phone,
        customer_id=customer_id,
        event_type=event_type,
        meta=meta,
    )
    db.flush()
    row = send_outbox_item_now(db, row)
    if row.status != MessageOutboxStatus.SENT.value:
        err = (row.error_message or "فشل إرسال واتساب").strip()
        raise ReceiptWhatsAppError(err)


def resolve_sale_phone(db: Session, sale: Sale, *, override: str | None = None) -> str:
    raw = (override or "").strip()
    if raw:
        return require_valid_phone(raw)
    return phone_for_sale(db, sale)


def resolve_booking_phone(booking: HotelBooking, *, override: str | None = None) -> str:
    raw = (override or "").strip()
    if raw:
        return require_valid_phone(raw)
    return phone_for_booking(booking)


def sale_phone_hint(db: Session, sale: Sale) -> str:
    try:
        return normalize_phone(phone_for_sale(db, sale))
    except ReceiptWhatsAppError:
        return ""


def booking_phone_hint(booking: HotelBooking) -> str:
    try:
        return normalize_phone(phone_for_booking(booking))
    except ReceiptWhatsAppError:
        if booking.guest_type == GuestType.COMPANY:
            raw = (booking.company_contact_phone or booking.guest_phone or "").strip()
        else:
            raw = (booking.guest_phone or "").strip()
        return normalize_phone(raw)


def send_pos_receipt_whatsapp(
    db: Session,
    sale: Sale,
    *,
    image_png_b64: str | None = None,
    phone_override: str | None = None,
) -> dict:
    if not pos_whatsapp_receipt_enabled(db):
        raise ReceiptWhatsAppError(
            "إرسال الفاتورة عبر واتساب غير مفعّل — فعّله من الإعدادات → إرسال الفاتورة على واتساب."
        )
    phone = resolve_sale_phone(db, sale, override=phone_override)
    body = message_for_sale(db, sale)
    image_url = _image_public_url(db, image_png_b64)
    if image_png_b64 and not image_url:
        body += "\n\n(تعذّر إرفاق صورة — اضبط الرابط العام في الإعدادات لإرسال صورة الفاتورة.)"
    dispatch_whatsapp(
        db,
        phone=phone,
        body=body,
        image_url=image_url,
        customer_id=sale.customer_id,
        event_type="pos.receipt_whatsapp",
    )
    return {"ok": True, "phone": normalize_phone(phone)}


def send_hotel_receipt_whatsapp(
    db: Session,
    booking: HotelBooking,
    *,
    folio_total: Decimal | None = None,
    folio_paid: Decimal | None = None,
    folio_balance: Decimal | None = None,
    focus_payment_amount: Decimal | None = None,
    doc_title: str = "فاتورة حجز",
    image_png_b64: str | None = None,
    phone_override: str | None = None,
) -> dict:
    if not hotel_whatsapp_receipt_enabled(db):
        raise ReceiptWhatsAppError(
            "إرسال الفاتورة عبر واتساب غير مفعّل — فعّله من الإعدادات → إرسال الفاتورة على واتساب."
        )
    phone = resolve_booking_phone(booking, override=phone_override)
    from modules.hotel.folio import build_folio

    folio = build_folio(db, booking.id)
    body = message_for_booking(
        db,
        booking,
        folio_total=Decimal(str(folio_total if folio_total is not None else folio.total)),
        folio_paid=Decimal(str(folio_paid if folio_paid is not None else folio.paid)),
        folio_balance=Decimal(
            str(folio_balance if folio_balance is not None else folio.balance)
        ),
        focus_payment_amount=focus_payment_amount,
        doc_title=doc_title,
    )
    image_url = _image_public_url(db, image_png_b64)
    if image_png_b64 and not image_url:
        body += "\n\n(تعذّر إرفاق صورة — اضبط الرابط العام في الإعدادات لإرسال صورة الفاتورة.)"
    dispatch_whatsapp(
        db,
        phone=phone,
        body=body,
        image_url=image_url,
        customer_id=booking.customer_id,
        event_type="hotel.receipt_whatsapp",
    )
    return {"ok": True, "phone": normalize_phone(phone)}


def whatsapp_receipt_ctx(
    db: Session,
    *,
    domain: str,
    phone: str | None,
    send_url: str,
) -> dict:
    phone_norm = normalize_phone(phone or "")
    return {
        "whatsapp_receipt_enabled": True,
        "whatsapp_receipt_phone": phone_norm,
        "whatsapp_send_url": send_url,
    }


def pos_whatsapp_sidebar_ctx(db: Session, sale: Sale | None) -> dict:
    """سياق زر واتساب في شريط الفاتورة (فواتير مكتملة)."""
    from modules.sales.models import SaleStatus

    if sale is None or sale.status != SaleStatus.COMPLETED:
        return {
            "pos_whatsapp_receipt_show": False,
            "pos_whatsapp_receipt_phone": "",
            "pos_whatsapp_send_url": "",
        }
    return {
        "pos_whatsapp_receipt_show": True,
        "pos_whatsapp_receipt_phone": sale_phone_hint(db, sale),
        "pos_whatsapp_send_url": f"/pos/receipt/{sale.id}/send-whatsapp",
    }


def hotel_whatsapp_detail_ctx(db: Session, booking: HotelBooking) -> dict:
    return {
        "hotel_whatsapp_receipt_show": True,
        "hotel_whatsapp_receipt_phone": booking_phone_hint(booking),
        "hotel_whatsapp_send_url": f"/admin/hotel/bookings/{booking.id}/receipt/send-whatsapp",
        "hotel_whatsapp_receipt_url": f"/admin/hotel/bookings/{booking.id}/receipt",
    }
