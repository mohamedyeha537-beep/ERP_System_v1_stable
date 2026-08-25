"""إشعار واتساب لموظف المطعم / الكاشير عند طلب أونلاين من /shop أو الشات."""
from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from modules.sales.models import Sale, SaleLine
from modules.settings.service import get_bool, get_setting

log = logging.getLogger("shop.staff_notify")


def shop_staff_notify_phones(db: Session) -> list[str]:
    """أرقام واتساب الكاشير/الموظف — يدعم عدة أرقام مفصولة بفاصلة."""
    from modules.notifications.routing import parse_phone_list

    raw = (get_setting(db, "shop_online_staff_phone", "") or "").strip()
    phones = parse_phone_list(raw)
    if phones:
        return phones
    admin = (get_setting(db, "messaging_admin_phone", "") or "").strip()
    return [admin] if admin else []


def shop_staff_notify_phone(db: Session) -> str:
    phones = shop_staff_notify_phones(db)
    return phones[0] if phones else ""


def _load_sale_with_lines(db: Session, sale: Sale) -> Sale:
    sid = int(sale.id)
    loaded = db.scalar(
        select(Sale)
        .where(Sale.id == sid)
        .options(selectinload(Sale.lines).selectinload(SaleLine.product))
    )
    return loaded or sale


def build_cashier_order_whatsapp_body(db: Session, sale: Sale) -> str:
    """نص واتساب واضح للكاشير يحتوي أصناف الطلب كاملة."""
    sale = _load_sale_with_lines(db, sale)

    guest = ""
    guest_phone = ""
    try:
        from modules.messaging.chat_order_service import web_chat_session_for_sale

        sess = web_chat_session_for_sale(db, sale.id)
        if sess is not None:
            guest = (sess.guest_name or "").strip()
            guest_phone = (sess.guest_phone or "").strip()
    except Exception:  # noqa: BLE001
        pass
    if not guest and sale.customer is not None:
        guest = (sale.customer.name or "").strip()
        guest_phone = (getattr(sale.customer, "phone", None) or "").strip()

    order_type = ""
    if getattr(sale, "external_order_type", None):
        order_type = str(
            sale.external_order_type.value
            if hasattr(sale.external_order_type, "value")
            else sale.external_order_type
        )
    type_ar = {
        "PICKUP": "استلام من المطعم",
        "DELIVERY": "توصيل",
        "DINE_IN": "صالة",
    }.get(order_type.upper(), order_type or "أونلاين")

    total = Decimal(str(sale.total or 0)).quantize(Decimal("0.001"))
    zone = (getattr(sale, "delivery_zone_name", None) or "").strip()
    addr = (getattr(sale, "delivery_address", None) or "").strip()

    lines_out: list[str] = []
    for line in list(getattr(sale, "lines", None) or []):
        pname = ""
        if getattr(line, "product", None) is not None:
            pname = (line.product.name_ar or "").strip()
        if not pname:
            pname = f"صنف #{getattr(line, 'product_id', '?')}"
        qty = Decimal(str(line.quantity or 0)).quantize(Decimal("0.001"))
        line_total = Decimal(str(line.line_total or 0)).quantize(Decimal("0.001"))
        note = (getattr(line, "line_note", None) or "").strip()
        row = f"• {pname} × {qty} = {line_total} د.ل"
        if note:
            row += f"\n  ↳ {note}"
        lines_out.append(row)
    lines_txt = "\n".join(lines_out) if lines_out else "• (لا توجد أصناف مسجّلة)"

    body = (
        f"🛒 *طلب أونلاين للكاشير*\n"
        f"المرجع: *#{sale.id}*\n"
        f"النوع: {type_ar}\n"
        f"الزبون: {guest or '—'}\n"
        f"الهاتف: {guest_phone or '—'}\n"
    )
    if zone:
        body += f"المنطقة: {zone}\n"
    if addr:
        body += f"العنوان: {addr}\n"
    body += f"\n*الأصناف:*\n{lines_txt}\n"
    body += f"\n*الإجمالي: {total} د.ل*\n"
    body += "افتح نقطة البيع ونفّذ الطلب على الجهاز."
    return body[:4000]


def notify_shop_staff_new_order(db: Session, sale: Sale) -> None:
    """يرسل واتساب فوري للكاشير بمحتوى الطلب عند وصول طلب أونلاين."""
    if not get_bool(db, "messaging_enabled", False):
        log.info("shop staff notify skipped: messaging disabled (sale=%s)", getattr(sale, "id", None))
        return
    phones = shop_staff_notify_phones(db)
    if not phones:
        log.info(
            "shop staff notify skipped: no shop_online_staff_phone (sale=%s)",
            getattr(sale, "id", None),
        )
        return
    try:
        from modules.messaging.models import MessageChannel
        from modules.messaging.outbox import enqueue_message, send_outbox_item_now
        from modules.messaging.phone_utils import normalize_whatsapp_phone

        cc = (get_setting(db, "messaging_country_code", "218") or "218").strip()
        body = build_cashier_order_whatsapp_body(db, sale)

        for phone in phones:
            norm = normalize_whatsapp_phone(phone, country_code=cc)
            if len(norm) < 8:
                continue
            row = enqueue_message(
                db,
                body=body,
                channel=MessageChannel.WHATSAPP.value,
                phone=norm,
                event_type="shop.online_order",
                meta={"sale_id": int(sale.id), "kind": "cashier_order_detail"},
            )
            db.flush()
            send_outbox_item_now(db, row)
    except Exception:  # noqa: BLE001
        log.exception("shop staff notify failed for sale %s", getattr(sale, "id", None))
