"""طلبات محادثة الويب — سلة، إنشاء مسودة بيع، وإتمام من الكاشير."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from modules.catalog.models import Product
from modules.customers.service import get_or_create_by_phone, require_valid_phone
from modules.messaging.models import WebChatSession
from modules.messaging.service import MessagingError
from modules.payments.models import PaymentMethodKind
from modules.payments.service import (
    list_pos_sale_payment_methods,
    list_sale_payments,
    record_sale_payment,
)
from modules.sales.models import ExternalOrderType, Sale, SaleContext, SaleSource, SaleStatus
from modules.sales.service import (
    SalesError,
    add_line_to_sale,
    cancel_sale,
    complete_sale,
    create_draft_sale,
    get_draft_sale,
    load_sale_with_lines,
    send_draft_to_kitchen,
)
from modules.delivery.service import get_zone, list_zones, record_delivery_cash_settlement
from modules.messaging.hermes_catalog import _normalize
from modules.settings.service import get_bool, get_setting

PHASE_BROWSE = "browse"
PHASE_CART = "cart"
PHASE_FULFILLMENT = "fulfillment"
PHASE_PAYMENT = "payment"
PHASE_AWAIT_RECEIPT = "await_receipt"
PHASE_SUBMITTED = "submitted"
PHASE_COMPLETED = "completed"
PHASE_DELIVERY_ZONE = "delivery_zone"
PHASE_DELIVERY_ADDRESS = "delivery_address"
PHASE_GUEST_PHONE = "guest_phone"
PHASE_GUEST_NAME = "guest_name"
PHASE_FEEDBACK = "feedback"
PHASE_POS_HANDLING = "pos_handling"

RECEIPT_UPLOAD_MARKER = "[[upload_receipt]]"
DEFAULT_SHOP_ORDER_CONFIRMATION_TEXT = (
    "تم استقبال طلبك رقم {order_id}.\n"
    "سيتم التواصل معك لاحقاً عبر واتساب لتأكيد التفاصيل والدفع."
)


def receipt_action_suffix() -> str:
    return f"\n{RECEIPT_UPLOAD_MARKER}"


def _default_order_data() -> dict[str, Any]:
    return {
        "cart": [],
        "fulfillment": None,
        "delivery_address": "",
        "delivery_zone_id": None,
        "delivery_zone_name": "",
        "delivery_fee": "0",
        "payment_method_id": None,
        "payment_method_name": "",
        "payment_kind": None,
    }


def load_order_data(session: WebChatSession) -> dict[str, Any]:
    raw = (session.order_json or "").strip() or "{}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    base = _default_order_data()
    base.update(data)
    if not isinstance(base.get("cart"), list):
        base["cart"] = []
    return base


def save_order_data(session: WebChatSession, data: dict[str, Any]) -> None:
    session.order_json = json.dumps(data, ensure_ascii=False)
    db_flush_needed = True
    if db_flush_needed:
        pass


def set_order_phase(session: WebChatSession, phase: str) -> None:
    session.order_phase = (phase or PHASE_BROWSE).strip() or PHASE_BROWSE


def cart_lines(session: WebChatSession) -> list[dict[str, Any]]:
    return list(load_order_data(session).get("cart") or [])


def cart_total(session: WebChatSession) -> Decimal:
    total = Decimal("0")
    for ln in cart_lines(session):
        try:
            total += Decimal(str(ln.get("line_total") or 0))
        except Exception:
            continue
    return total.quantize(Decimal("0.001"))


def delivery_fee(session: WebChatSession) -> Decimal:
    data = load_order_data(session)
    if (data.get("fulfillment") or "").upper() != "DELIVERY":
        return Decimal("0")
    try:
        return Decimal(str(data.get("delivery_fee") or 0)).quantize(Decimal("0.001"))
    except Exception:
        return Decimal("0")


def customer_total(session: WebChatSession) -> Decimal:
    return (cart_total(session) + delivery_fee(session)).quantize(Decimal("0.001"))


def order_payment_amount(session: WebChatSession) -> Decimal:
    """مبلغ الدفع على الفاتورة (بدون أجرة التوصيل — كما في POS)."""
    return cart_total(session)


def cart_count(session: WebChatSession) -> int:
    n = Decimal("0")
    for ln in cart_lines(session):
        try:
            n += Decimal(str(ln.get("qty") or 0))
        except Exception:
            continue
    return int(n)


def _fmt_price(value: Decimal | None) -> str:
    if value is None:
        return "—"
    return f"{Decimal(str(value)).quantize(Decimal('0.001'))} د.ل"


def _render_order_template(db: Session, session: WebChatSession, sale: Sale) -> str:
    data = load_order_data(session)
    template = (
        get_setting(db, "shop_order_confirmation_text", DEFAULT_SHOP_ORDER_CONFIRMATION_TEXT)
        or DEFAULT_SHOP_ORDER_CONFIRMATION_TEXT
    ).strip()
    values = {
        "order_id": sale.id,
        "sale_id": sale.id,
        "store_name": get_setting(db, "store_name", "نقطة البيع"),
        "customer_name": (session.guest_name or "").strip() or "عميلنا",
        "phone": (session.guest_phone or "").strip(),
        "total": _fmt_price(Decimal(str(sale.total or 0))),
        "items_total": _fmt_price(Decimal(str(sale.total or 0))),
        "delivery_fee": _fmt_price(delivery_fee(session)),
        "customer_total": _fmt_price(customer_total(session)),
        "payment_method": (data.get("payment_method_name") or "").strip(),
        "fulfillment": "توصيل" if (data.get("fulfillment") or "").upper() == "DELIVERY" else "استلام",
        "delivery_zone": (data.get("delivery_zone_name") or "").strip(),
        "delivery_address": (data.get("delivery_address") or "").strip(),
    }
    try:
        return template.format(**values)
    except Exception:
        return DEFAULT_SHOP_ORDER_CONFIRMATION_TEXT.format(**values)


def order_confirmation_parts(
    db: Session, session: WebChatSession, sale: Sale, *, mark_intro: bool = True
) -> list[str]:
    """رسائل التأكيد: شرح الولاء (مرة واحدة) ثم رسالة الطلب."""
    from modules.messaging.hermes_loyalty import (
        loyalty_order_event_note,
        take_loyalty_intro_if_needed,
    )

    parts: list[str] = []
    phone = (session.guest_phone or "").strip()
    if phone and mark_intro:
        intro = take_loyalty_intro_if_needed(db, phone)
        if intro:
            parts.append(intro)

    order_lines = [_render_order_template(db, session, sale)]
    if phone:
        note = loyalty_order_event_note(db, phone, sale.total)
        if note:
            order_lines.append("")
            order_lines.append(note)
    else:
        order_lines.append("💡 اكتب «نقاطي» لمعرفة برنامج الولاء.")
    parts.append("\n".join(order_lines).replace("**", ""))
    return [p for p in parts if (p or "").strip()]


def _enqueue_customer_order_confirmation(db: Session, session: WebChatSession, sale: Sale) -> None:
    phone = (session.guest_phone or "").strip()
    data = load_order_data(session)
    sent_for = str(data.get("confirmation_sent_for_sale_id") or "").strip()
    if sent_for == str(sale.id):
        return

    parts = order_confirmation_parts(db, session, sale, mark_intro=True)
    data["confirmation_parts"] = parts
    data["confirmation_sent_for_sale_id"] = int(sale.id)
    session.order_json = json.dumps(data, ensure_ascii=False)

    if not get_bool(db, "shop_send_order_confirmation_whatsapp", True):
        return
    if not phone:
        return
    from modules.messaging.models import MessageChannel
    from modules.messaging.outbox import enqueue_message

    for idx, body in enumerate(parts):
        event_type = (
            "loyalty.program_intro" if idx == 0 and len(parts) > 1 else "shop.order_submitted"
        )
        enqueue_message(
            db,
            body=body,
            channel=MessageChannel.WHATSAPP.value,
            phone=phone,
            event_type=event_type,
            meta={
                "sale_id": int(sale.id),
                "session_id": int(session.id),
                "part": idx + 1,
                "parts_total": len(parts),
            },
        )


def format_cart_summary(session: WebChatSession) -> str:
    lines = cart_lines(session)
    if not lines:
        return "🛒 السلة فارغة.\nاكتب اسم صنف للإضافة، مثل: «اطلب برجر» أو «برجر ×2»."
    out = ["🛒 **سلتك:**", ""]
    for i, ln in enumerate(lines, 1):
        out.append(
            f"{i}. {ln.get('name_ar', '—')} × {ln.get('qty', '1')} — "
            f"{_fmt_price(Decimal(str(ln.get('line_total') or 0)))}"
        )
    out.append("")
    out.append(f"💰 **المجموع: {_fmt_price(cart_total(session))}**")
    out.append("")
    out.append("• «تأكيد» — إتمام الطلب")
    out.append("• «السلة» — عرض السلة")
    out.append("• «إلغاء» — تفريغ السلة")
    return "\n".join(out).replace("**", "")


def clear_cart_items(session: WebChatSession) -> None:
    """يفرّغ أصناف السلة فقط — يبقي مرحلة الطلب ورسالة التأكيد."""
    data = load_order_data(session)
    data["cart"] = []
    session.order_json = json.dumps(data, ensure_ascii=False)


def clear_cart(session: WebChatSession) -> None:
    data = load_order_data(session)
    data["cart"] = []
    data["fulfillment"] = None
    data["delivery_address"] = ""
    data["delivery_zone_id"] = None
    data["delivery_zone_name"] = ""
    data["delivery_fee"] = "0"
    data["payment_method_id"] = None
    data["payment_method_name"] = ""
    data["payment_kind"] = None
    session.order_json = json.dumps(data, ensure_ascii=False)
    set_order_phase(session, PHASE_BROWSE)


def add_product_to_cart(
    db: Session, session: WebChatSession, product: Product, qty: Decimal
) -> None:
    if product.sell_price is None:
        raise MessagingError(f"الصنف «{product.name_ar}» بدون سعر.")
    qty = Decimal(str(qty)).quantize(Decimal("0.0001"))
    if qty <= 0:
        raise MessagingError("الكمية غير صالحة.")
    data = load_order_data(session)
    cart: list[dict[str, Any]] = list(data.get("cart") or [])
    unit = Decimal(str(product.sell_price)).quantize(Decimal("0.001"))
    pid = int(product.id)
    merged = False
    for ln in cart:
        if int(ln.get("product_id") or 0) == pid:
            new_qty = Decimal(str(ln.get("qty") or 0)) + qty
            ln["qty"] = str(new_qty)
            ln["line_total"] = str((new_qty * unit).quantize(Decimal("0.001")))
            merged = True
            break
    if not merged:
        cart.append(
            {
                "product_id": pid,
                "name_ar": product.name_ar or "",
                "qty": str(qty),
                "unit_price": str(unit),
                "line_total": str((qty * unit).quantize(Decimal("0.001"))),
            }
        )
    data["cart"] = cart
    session.order_json = json.dumps(data, ensure_ascii=False)
    set_order_phase(session, PHASE_CART)
    db.flush()


def remove_cart_line(session: WebChatSession, index: int) -> bool:
    data = load_order_data(session)
    cart: list[dict[str, Any]] = list(data.get("cart") or [])
    if index < 1 or index > len(cart):
        return False
    cart.pop(index - 1)
    data["cart"] = cart
    session.order_json = json.dumps(data, ensure_ascii=False)
    if not cart:
        set_order_phase(session, PHASE_BROWSE)
    db_flush = True
    if db_flush:
        pass
    return True


def is_web_chat_sale(sale: Sale) -> bool:
    eid = (sale.external_order_id or "").strip()
    return sale.source == SaleSource.ONLINE and eid.startswith("webchat:")


def is_web_store_sale(sale: Sale) -> bool:
    eid = (sale.external_order_id or "").strip()
    return sale.source == SaleSource.ONLINE and eid.startswith("webstore:")


def is_online_guest_sale(sale: Sale) -> bool:
    return is_web_chat_sale(sale) or is_web_store_sale(sale)


def _is_web_chat_sale(sale: Sale) -> bool:
    return is_online_guest_sale(sale)


def _external_order_id(session: WebChatSession) -> str:
    prefix = "webstore" if (session.department or "").strip().lower() == "shop" else "webchat"
    return f"{prefix}:{session.id}"


def _sync_sale_lines(db: Session, sale: Sale, session: WebChatSession) -> Sale:
    sale = load_sale_with_lines(db, sale.id)
    if sale is None:
        raise MessagingError("تعذّر تحميل الطلب.")
    existing = {int(ln.product_id): ln for ln in sale.lines}
    wanted: dict[int, Decimal] = {}
    for ln in cart_lines(session):
        pid = int(ln.get("product_id") or 0)
        if pid <= 0:
            continue
        wanted[pid] = wanted.get(pid, Decimal("0")) + Decimal(str(ln.get("qty") or 0))
    for pid, ln in list(existing.items()):
        if pid not in wanted:
            db.delete(ln)
    db.flush()
    sale = load_sale_with_lines(db, sale.id)
    assert sale is not None
    existing = {int(ln.product_id): ln for ln in sale.lines}
    for pid, qty in wanted.items():
        if pid in existing:
            cur = existing[pid]
            if Decimal(str(cur.quantity)) != qty:
                cur.quantity = qty
                cur.line_total = (qty * cur.unit_price).quantize(Decimal("0.001"))
        else:
            add_line_to_sale(db, sale.id, pid, qty, user_id=None)
    db.flush()
    return load_sale_with_lines(db, sale.id)  # type: ignore[return-value]


def _apply_session_customer(db: Session, session: WebChatSession, sale: Sale) -> None:
    phone = (session.guest_phone or "").strip()
    name = (session.guest_name or "").strip() or None
    if not phone:
        return
    try:
        cust = get_or_create_by_phone(db, phone=phone, name=name)
        sale.customer_id = cust.id
        data = load_order_data(session)
        addr = (data.get("delivery_address") or "").strip()
        if addr and data.get("fulfillment") == "DELIVERY":
            note = f"عنوان توصيل (شات): {addr}"
            if not cust.notes or addr not in (cust.notes or ""):
                cust.notes = f"{(cust.notes or '').strip()}\n{note}".strip()
        db.flush()
    except Exception:
        pass


def try_apply_session_referral_to_sale(
    db: Session, session: WebChatSession, sale: Sale
) -> None:
    """يربط كود الإحالة من جلسة المتجر/الشات بالمسودة قبل إتمام الدفع."""
    from modules.customers.referral_service import ReferralError, apply_referral_to_sale

    data = load_order_data(session)
    code = (data.get("referral_code") or "").strip()
    if not code:
        return
    phone = (session.guest_phone or "").strip()
    if not phone:
        return
    try:
        buyer = get_or_create_by_phone(
            db, phone=phone, name=(session.guest_name or "").strip() or None
        )
        sale.customer_id = buyer.id
        apply_referral_to_sale(db, sale, code=code, buyer=buyer)
    except ReferralError:
        pass


def grant_referral_for_completed_online_sale(
    db: Session, *, sale_id: int, user_id: int | None
) -> None:
    """منح نقاط الإحالة بعد إتمام بيع أونلاين (متجر/شات) — آمن للتكرار."""
    from modules.customers.referral_service import grant_referral_after_sale
    from modules.payments.service import sum_sale_payments

    sale = db.get(Sale, sale_id)
    if sale is None or not is_online_guest_sale(sale):
        return
    paid = sum_sale_payments(db, sale_id)
    if paid <= 0:
        paid = Decimal(str(sale.total or 0))
    grant_referral_after_sale(
        db, sale_id=sale_id, paid_total=paid, user_id=user_id
    )


def _apply_delivery_zone_to_sale(session: WebChatSession, sale: Sale) -> None:
    data = load_order_data(session)
    if (data.get("fulfillment") or "").upper() != "DELIVERY":
        sale.delivery_zone_id = None
        sale.delivery_zone_name = None
        sale.delivery_fee = Decimal("0")
        return
    zid = data.get("delivery_zone_id")
    if zid:
        sale.delivery_zone_id = int(zid)
        sale.delivery_zone_name = (data.get("delivery_zone_name") or "").strip() or None
        try:
            sale.delivery_fee = Decimal(str(data.get("delivery_fee") or 0)).quantize(
                Decimal("0.001")
            )
        except Exception:
            sale.delivery_fee = Decimal("0")
    else:
        sale.delivery_zone_id = None
        sale.delivery_zone_name = None
        sale.delivery_fee = Decimal("0")


def create_or_update_chat_sale(db: Session, session: WebChatSession) -> Sale:
    if not cart_lines(session):
        raise MessagingError("السلة فارغة.")
    data = load_order_data(session)
    sale: Sale | None = None
    if session.sale_id:
        sale = get_draft_sale(db, int(session.sale_id))
    if sale is None:
        sale = create_draft_sale(db, user_id=None, source=SaleSource.ONLINE)
        sale.context_type = SaleContext.EXTERNAL
        ext = (data.get("fulfillment") or "PICKUP").strip().upper()
        sale.external_order_type = (
            ExternalOrderType.DELIVERY
            if ext == ExternalOrderType.DELIVERY.value
            else ExternalOrderType.PICKUP
        )
        sale.external_order_id = _external_order_id(session)
        session.sale_id = sale.id
        db.flush()
    else:
        ext = (data.get("fulfillment") or "PICKUP").strip().upper()
        sale.external_order_type = (
            ExternalOrderType.DELIVERY
            if ext == ExternalOrderType.DELIVERY.value
            else ExternalOrderType.PICKUP
        )
    _apply_session_customer(db, session, sale)
    try_apply_session_referral_to_sale(db, session, sale)
    _apply_delivery_zone_to_sale(session, sale)
    sale = _sync_sale_lines(db, sale, session)
    db.flush()
    return sale


def bank_payment_instructions(db: Session, *, method_name: str = "") -> str:
    custom = (get_setting(db, "web_chat_bank_payment_text", "") or "").strip()
    label = (method_name or "التحويل").strip()
    if custom:
        return f"ادفع عبر **{label}**:\n{custom}".replace("**", "")
    store = (get_setting(db, "store_name", "مطعم ومقهى روف") or "").strip()
    return (
        f"ادفع عبر {label} — حساب {store}:\n"
        "• رقم الحساب / IBAN: (يُضبط من إعدادات المراسلات)\n"
        "• اكتب رقم الطلب في ملاحظة التحويل إن أمكن."
    )


def list_chat_payment_options(db: Session) -> list[dict[str, Any]]:
    """نفس وسائل الدفع المعروضة في جلسة البيع (POS)."""
    out: list[dict[str, Any]] = []
    for pm in list_pos_sale_payment_methods(db, only_active=True):
        out.append(
            {
                "id": pm.id,
                "name": pm.name_ar,
                "kind": pm.kind.value if pm.kind else PaymentMethodKind.OTHER.value,
            }
        )
    return out


def format_delivery_zones(db: Session) -> str:
    zones = list_zones(db, only_active=True)
    if not zones:
        return (
            "⚠️ لا توجد مناطق توصيل مفعّلة.\n"
            "تواصل مع الموظف أو اختر الاستلام من المطعم."
        )
    lines = ["🚚 **اختر منطقة التوصيل:**", ""]
    for i, z in enumerate(zones, 1):
        fee = Decimal(str(z.fee or 0)).quantize(Decimal("0.001"))
        lines.append(f"{i}. {z.name_ar} — {_fmt_price(fee)}")
    lines.append("")
    lines.append("اكتب رقم المنطقة (1، 2، …).")
    return "\n".join(lines).replace("**", "")


def select_delivery_zone(db: Session, session: WebChatSession, choice: str) -> bool:
    zones = list_zones(db, only_active=True)
    if not zones:
        return False
    t = (choice or "").strip()
    picked = None
    if re.fullmatch(r"\d+", t):
        idx = int(t)
        if 1 <= idx <= len(zones):
            picked = zones[idx - 1]
    if picked is None:
        norm = t.lower()
        for z in zones:
            if norm in (z.name_ar or "").lower():
                picked = z
                break
    if picked is None:
        return False
    data = load_order_data(session)
    data["delivery_zone_id"] = int(picked.id)
    data["delivery_zone_name"] = picked.name_ar
    data["delivery_fee"] = str(Decimal(str(picked.fee or 0)).quantize(Decimal("0.001")))
    session.order_json = json.dumps(data, ensure_ascii=False)
    return True


def format_payment_options(db: Session, session: WebChatSession) -> str:
    opts = list_chat_payment_options(db)
    if not opts:
        return (
            "⚠️ لا توجد وسائل دفع مفعّلة في النظام.\n"
            "راجع إعدادات وسائل الدفع أو اكتب «3» للموظف."
        )
    pay_amt = order_payment_amount(session)
    fee = delivery_fee(session)
    total = customer_total(session)
    lines = ["💳 **اختر وسيلة الدفع**", ""]
    lines.append(f"• قيمة الأصناف: {_fmt_price(pay_amt)}")
    if fee > 0:
        data = load_order_data(session)
        zname = (data.get("delivery_zone_name") or "").strip()
        lines.append(f"• توصيل ({zname}): {_fmt_price(fee)}")
        lines.append(f"• **الإجمالي للزبون: {_fmt_price(total)}**")
    else:
        lines.append(f"• **المطلوب: {_fmt_price(pay_amt)}**")
    lines.append("")
    lines.append("الوسائل المتاحة (كما في نقطة البيع):")
    for i, opt in enumerate(opts, 1):
        lines.append(f"{i}. {opt['name']}")
    lines.append("")
    lines.append("اكتب رقم الخيار أو اسمه بالضبط (مثل: موبي كاش).")
    return "\n".join(lines).replace("**", "")


def select_payment_method(db: Session, session: WebChatSession, choice: str) -> PaymentMethodKind | None:
    opts = list_chat_payment_options(db)
    if not opts:
        return None
    t = (choice or "").strip()
    picked = None
    if re.fullmatch(r"\d+", t):
        idx = int(t)
        if 1 <= idx <= len(opts):
            picked = opts[idx - 1]
    if picked is None:
        norm = _normalize(t)
        for opt in opts:
            oname = _normalize(opt["name"] or "")
            if norm == oname or norm in oname or oname in norm:
                picked = opt
                break
    if picked is None:
        return None
    data = load_order_data(session)
    data["payment_method_id"] = int(picked["id"])
    data["payment_method_name"] = picked["name"]
    data["payment_kind"] = picked["kind"]
    session.order_json = json.dumps(data, ensure_ascii=False)
    return PaymentMethodKind(picked["kind"])


def attach_receipt_and_submit(
    db: Session,
    session: WebChatSession,
    *,
    proof_filename: str | None = None,
    note: str = "",
) -> Sale:
    sale = create_or_update_chat_sale(db, session)
    data = load_order_data(session)
    pm_id = data.get("payment_method_id")
    if pm_id:
        existing = list_sale_payments(db, sale.id)
        if not existing:
            record_sale_payment(
                db,
                sale.id,
                int(pm_id),
                sale.total,
                payment_proof_image_filename=proof_filename,
            )
    if proof_filename:
        session.payment_proof_filename = proof_filename
    set_order_phase(session, PHASE_SUBMITTED)
    db.flush()
    _enqueue_customer_order_confirmation(db, session, sale)
    try:
        from modules.notifications.hooks import emit_order_created

        emit_order_created(db, sale)
    except Exception:  # noqa: BLE001
        pass
    try:
        from modules.shop.staff_notify import notify_shop_staff_new_order

        notify_shop_staff_new_order(db, sale)
    except Exception:  # noqa: BLE001
        pass
    return sale


def submit_cash_order(db: Session, session: WebChatSession) -> Sale:
    sale = create_or_update_chat_sale(db, session)
    set_order_phase(session, PHASE_SUBMITTED)
    db.flush()
    _enqueue_customer_order_confirmation(db, session, sale)
    try:
        from modules.notifications.hooks import emit_order_created

        emit_order_created(db, sale)
    except Exception:  # noqa: BLE001
        pass
    try:
        from modules.shop.staff_notify import notify_shop_staff_new_order

        notify_shop_staff_new_order(db, sale)
    except Exception:  # noqa: BLE001
        pass
    return sale


def try_parse_guest_phone(text: str) -> str | None:
    """استخراج رقم هاتف من رسالة الزائر."""
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        return require_valid_phone(raw)
    except Exception:
        pass
    digits = re.sub(r"\D", "", raw)
    if len(digits) >= 9:
        try:
            return require_valid_phone(digits)
        except Exception:
            return None
    return None


def save_guest_phone(db: Session, session: WebChatSession, phone: str) -> None:
    session.guest_phone = phone
    name = (session.guest_name or "").strip() or None
    get_or_create_by_phone(db, phone=phone, name=name)


def order_confirmation_message(db: Session, session: WebChatSession, sale: Sale) -> str:
    data = load_order_data(session)
    stored = data.get("confirmation_parts")
    if isinstance(stored, list) and stored and str(data.get("confirmation_sent_for_sale_id") or "") == str(
        sale.id
    ):
        return "\n\n".join(str(p).strip() for p in stored if str(p).strip())
    # للعرض فقط — لا يعلّم شرح الولاء كمُرسَل (التوسيم عند الإرسال الفعلي)
    return "\n\n".join(order_confirmation_parts(db, session, sale, mark_intro=False))


@dataclass
class ChatOrderRow:
    session_id: int
    token: str
    guest_name: str | None
    guest_phone: str | None
    sale_id: int
    sale_total: Decimal
    order_phase: str
    fulfillment: str | None
    payment_kind: str | None
    has_proof: bool
    created_at: str | None
    cart_preview: str


def web_chat_session_for_sale(db: Session, sale_id: int) -> WebChatSession | None:
    return db.scalars(
        select(WebChatSession).where(WebChatSession.sale_id == int(sale_id)).limit(1)
    ).first()


def _pending_web_chat_sale_ok(sale: Sale | None) -> bool:
    if sale is None or sale.status != SaleStatus.DRAFT:
        return False
    if sale.sent_to_kitchen_at is not None:
        return False
    return is_online_guest_sale(sale)


def _order_cart_preview(session: WebChatSession, sale: Sale | None) -> str:
    """معاينة الأصناف — من السلة، أو من بنود الفاتورة إن فُرّغت السلة بعد الإرسال."""
    cart = cart_lines(session)
    if cart:
        return "، ".join(f"{ln.get('name_ar')}×{ln.get('qty')}" for ln in cart[:4]) or "—"
    if sale is None:
        return "—"
    parts: list[str] = []
    for ln in list(getattr(sale, "lines", None) or [])[:4]:
        prod = getattr(ln, "product", None)
        name = getattr(prod, "name_ar", None) or f"#{ln.product_id}"
        parts.append(f"{name}×{ln.quantity}")
    return "، ".join(parts) or "—"


def list_pending_chat_orders(db: Session, *, limit: int = 50) -> list[ChatOrderRow]:
    rows = db.scalars(
        select(WebChatSession)
        .where(
            WebChatSession.sale_id.isnot(None),
            WebChatSession.order_phase.in_([PHASE_SUBMITTED, PHASE_AWAIT_RECEIPT]),
        )
        .order_by(WebChatSession.last_message_at.desc())
        .limit(limit)
    ).all()
    out: list[ChatOrderRow] = []
    for s in rows:
        if not s.sale_id:
            continue
        sale = db.get(Sale, int(s.sale_id))
        if not _pending_web_chat_sale_ok(sale):
            continue
        data = load_order_data(s)
        out.append(
            ChatOrderRow(
                session_id=s.id,
                token=s.token,
                guest_name=s.guest_name,
                guest_phone=s.guest_phone,
                sale_id=int(s.sale_id),
                sale_total=Decimal(str(sale.total or 0)),
                order_phase=s.order_phase or "",
                fulfillment=data.get("fulfillment"),
                payment_kind=data.get("payment_kind"),
                has_proof=bool(s.payment_proof_filename),
                created_at=sale.created_at.isoformat() if sale.created_at else None,
                cart_preview=_order_cart_preview(s, sale),
            )
        )
    return out


def list_in_progress_chat_orders(db: Session, *, limit: int = 50) -> list[ChatOrderRow]:
    """طلبات شات أُخذت في POS وأُرسلت للمطبخ ولم تُحصّل بعد."""
    rows = db.scalars(
        select(WebChatSession)
        .where(WebChatSession.sale_id.isnot(None))
        .order_by(WebChatSession.last_message_at.desc())
        .limit(limit)
    ).all()
    out: list[ChatOrderRow] = []
    for s in rows:
        if not s.sale_id:
            continue
        sale = db.get(Sale, int(s.sale_id))
        if (
            sale is None
            or sale.status != SaleStatus.DRAFT
            or sale.sent_to_kitchen_at is None
            or not is_online_guest_sale(sale)
        ):
            continue
        data = load_order_data(s)
        out.append(
            ChatOrderRow(
                session_id=s.id,
                token=s.token,
                guest_name=s.guest_name,
                guest_phone=s.guest_phone,
                sale_id=int(s.sale_id),
                sale_total=Decimal(str(sale.total or 0)),
                order_phase=s.order_phase or "",
                fulfillment=data.get("fulfillment"),
                payment_kind=data.get("payment_kind"),
                has_proof=bool(s.payment_proof_filename),
                created_at=sale.created_at.isoformat() if sale.created_at else None,
                cart_preview=_order_cart_preview(s, sale),
            )
        )
    return out


@dataclass
class ChatOrdersStats:
    pending_count: int
    in_pos_count: int
    completed_today_count: int


def chat_orders_stats(db: Session) -> ChatOrdersStats:
    from datetime import datetime, timezone

    pending = len(list_pending_chat_orders(db, limit=500))
    in_pos = len(list_in_progress_chat_orders(db, limit=500))
    today = datetime.now(timezone.utc).date()
    completed_today = 0
    for s in db.scalars(
        select(WebChatSession).where(WebChatSession.sale_id.isnot(None)).limit(500)
    ).all():
        if not s.sale_id:
            continue
        sale = db.get(Sale, int(s.sale_id))
        if sale is None or sale.status != SaleStatus.COMPLETED or not is_web_chat_sale(sale):
            continue
        if sale.created_at and sale.created_at.date() >= today:
            completed_today += 1
    return ChatOrdersStats(
        pending_count=pending,
        in_pos_count=in_pos,
        completed_today_count=completed_today,
    )


def notify_web_chat_checkout_complete(db: Session, sale_id: int) -> None:
    """بعد تحصيل الكاشير — إبلاغ الزبون بالجاهزية وطلب التقييم."""
    sale = db.get(Sale, sale_id)
    if sale is None or not is_web_chat_sale(sale):
        return
    session = web_chat_session_for_sale(db, sale_id)
    if session is None:
        return
    from modules.messaging.hermes_feedback import feedback_prompt, start_feedback_after_order
    from modules.messaging.models import MessageDirection, WebChatSender
    from modules.messaging.web_chat_service import _add_message
    from modules.sales.models import ExternalOrderType

    data = load_order_data(session)
    fulfillment = (data.get("fulfillment") or "").strip().upper()
    is_delivery = (
        fulfillment == "DELIVERY"
        or sale.external_order_type == ExternalOrderType.DELIVERY
    )
    lines = [f"✅ تم إتمام طلبك #{sale.id}!"]
    if is_delivery:
        lines.append("🚚 طلبك جاهز — السائق في الطريق أو سيتواصل معك قريباً.")
    else:
        lines.append("🏪 طلبك جاهز للاستلام من المطعم.")
    lines.append("")
    lines.append(feedback_prompt())
    _add_message(
        db,
        session,
        body="\n".join(lines),
        direction=MessageDirection.OUTBOUND.value,
        sender_type=WebChatSender.BOT.value,
    )
    start_feedback_after_order(db, session)
    db.flush()


def _notify_guest_order_confirmed(db: Session, session: WebChatSession, sale: Sale) -> None:
    from modules.messaging.hermes_feedback import feedback_prompt, start_feedback_after_order
    from modules.messaging.models import MessageDirection, WebChatSender
    from modules.messaging.web_chat_service import _add_message

    earned_line = ""
    if sale.customer_id:
        from modules.customers.models import Customer
        from modules.customers.service import grant_loyalty_after_sale, loyalty_settings

        cust = db.get(Customer, int(sale.customer_id))
        if cust is not None and loyalty_settings(db).get("enabled"):
            pts = grant_loyalty_after_sale(
                db,
                customer=cust,
                sale_id=sale.id,
                paid_total=Decimal(str(sale.total or 0)),
                user_id=None,
            )
            if pts > 0:
                from modules.customers.service import points_to_dinars

                earned_line = (
                    f"\n⭐ ربحت {pts} نقطة "
                    f"(≈ {points_to_dinars(db, pts)} د.ل)."
                )

    body = (
        f"🎉 تم تأكيد طلبك #{sale.id}\n"
        "أُرسل للمطبخ — نبلّغك عند الجاهزية."
        f"{earned_line}\n\n"
        + feedback_prompt()
    )
    _add_message(
        db,
        session,
        body=body,
        direction=MessageDirection.OUTBOUND.value,
        sender_type=WebChatSender.BOT.value,
    )
    start_feedback_after_order(db, session)


def confirm_chat_order_for_kitchen(
    db: Session,
    *,
    sale_id: int,
    user_id: int,
    pos_shift_id: int | None = None,
    complete_payment: bool = True,
) -> Sale:
    sale = get_draft_sale(db, sale_id)
    if sale is None:
        raise SalesError("الطلب غير موجود أو ليس مسودة.")
    if sale.external_order_id and not is_online_guest_sale(sale):
        raise SalesError("هذا ليس طلب أونلاين (متجر أو شات).")
    if sale.sent_to_kitchen_at is None:
        send_draft_to_kitchen(db, sale_id, user_id)
    if complete_payment:
        pays = list_sale_payments(db, sale_id)
        if not pays:
            data_sess = db.scalars(
                select(WebChatSession).where(WebChatSession.sale_id == sale_id).limit(1)
            ).first()
            pm_id = None
            if data_sess is not None:
                pm_id = load_order_data(data_sess).get("payment_method_id")
            if pm_id:
                record_sale_payment(db, sale_id, int(pm_id), sale.total)
        complete_sale(db, sale_id, user_id, pos_shift_id=pos_shift_id)
        sale = db.get(Sale, sale_id)
        if sale is not None:
            from modules.sales.models import ExternalOrderType, SaleContext

            if (
                sale.context_type == SaleContext.EXTERNAL
                and sale.external_order_type == ExternalOrderType.DELIVERY
                and Decimal(str(sale.delivery_fee or 0)) > 0
            ):
                record_delivery_cash_settlement(
                    db,
                    sale_id=sale.id,
                    amount=Decimal(str(sale.delivery_fee or 0)),
                    zone_id=sale.delivery_zone_id,
                    user_id=user_id,
                    note=f"أجرة توصيل — طلب شات #{sale.id}",
                )
            grant_referral_for_completed_online_sale(
                db, sale_id=sale.id, user_id=user_id
            )
    sess = db.scalars(
        select(WebChatSession).where(WebChatSession.sale_id == sale_id).limit(1)
    ).first()
    if sess is not None and sale is not None:
        _notify_guest_order_confirmed(db, sess, sale)
    db.flush()
    sale = db.get(Sale, sale_id)
    if sale is None:
        raise SalesError("الطلب غير موجود.")
    try:
        from modules.notifications.hooks import emit_order_confirmed

        emit_order_confirmed(db, sale)
    except Exception:  # noqa: BLE001
        pass
    return sale


def cancel_chat_order(db: Session, sale_id: int) -> None:
    sale = get_draft_sale(db, sale_id)
    if sale is None:
        raise SalesError("لا يمكن إلغاء هذا الطلب.")
    cancel_sale(db, sale_id)
    sess = db.scalars(
        select(WebChatSession).where(WebChatSession.sale_id == sale_id).limit(1)
    ).first()
    if sess is not None:
        clear_cart(sess)
        sess.sale_id = None
        sess.payment_proof_filename = None
        set_order_phase(sess, PHASE_BROWSE)
    db.flush()


def cancel_all_pending_chat_orders(db: Session, *, limit: int = 100) -> int:
    """يلغي كل طلبات الشات بانتظار مراجعة الكاشير."""
    orders = list_pending_chat_orders(db, limit=limit)
    for row in orders:
        cancel_chat_order(db, row.sale_id)
    return len(orders)
