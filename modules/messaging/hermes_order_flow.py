"""Hermes — مسار الطلب الكامل في المحادثة."""

from __future__ import annotations



import json

import re

from decimal import Decimal



from sqlalchemy.orm import Session



from modules.catalog.models import Product

from modules.messaging.chat_order_service import (

    PHASE_AWAIT_RECEIPT,

    PHASE_BROWSE,

    PHASE_CART,

    PHASE_COMPLETED,

    PHASE_DELIVERY_ADDRESS,

    PHASE_DELIVERY_ZONE,

    PHASE_FEEDBACK,

    PHASE_FULFILLMENT,

    PHASE_GUEST_NAME,

    PHASE_GUEST_PHONE,

    PHASE_PAYMENT,

    PHASE_SUBMITTED,

    attach_receipt_and_submit,

    receipt_action_suffix,

    add_product_to_cart,

    bank_payment_instructions,

    cart_lines,

    cart_total,

    clear_cart,

    customer_total,

    format_cart_summary,

    format_delivery_zones,

    format_payment_options,

    load_order_data,

    order_confirmation_parts,

    order_payment_amount,

    remove_cart_line,

    save_guest_phone,

    select_delivery_zone,

    select_payment_method,

    submit_cash_order,

    try_parse_guest_phone,

)

from modules.messaging.hermes_catalog import (

    HermesReply,

    _normalize,

    _strip_fillers,

    find_products_by_text,

    match_menu_section_code,

    _products_for_section_code,

    _score_product,

)

from modules.messaging.models import WebChatSession

from modules.payments.models import PaymentMethodKind



_ORDER_VERBS = (

    "اطلب", "طلب", "أضف", "اضف", "اضيف", "زود", "عايز", "ابغى", "ابي", "اريد",

    "حد يطلب", "ممكن تطلب", "طلب لي", "طلبلي",

)

_CONFIRM_WORDS = ("تأكيد", "تاكيد", "confirm", "اكمل", "كمل", "تمام الطلب", "نعم")

_CANCEL_WORDS = ("الغ", "الغاء", "إلغاء", "cancel", "فضي السلة", "افرغ")

_CART_WORDS = ("السلة", "سلة", "سلتي", "cart")

_CHECKOUT_WORDS = ("checkout", "ادفع", "الدفع", "كمل الطلب")

_PICKUP_WORDS = ("استلام", "self", "pickup", "من المطعم", "استلم", "اروح")

_DELIVERY_WORDS = ("توصيل", "delivery", "للبيت", "البيت", "يوصل")

_RECEIPT_DONE = ("تم التحويل", "حولت", "حولتلك", "ارسلت", "أرسلت", "دفعت")





from modules.messaging.hermes_intent import is_how_to_order_question, is_question_like



def wants_order_intent(text: str) -> bool:

    if is_how_to_order_question(text):

        return False

    t = _normalize(text)

    if re.search(r"^(اطلب|طلب|اضف|اضيف|زود|order)\s", t):

        return True

    if re.search(r"(×|\bx\s*\d|\d\s*x\s)", t):

        return True

    if re.search(r"(اطلب|نطلب|طلب)\s+\S{3,}", t):

        return True

    if any(w in t for w in ("عايز", "ابغى", "ابي", "اريد", "نبي")) and any(

        w in t for w in ("اطلب", "نطلب", "طلب")

    ):

        return True

    return False





def _parse_quantity(text: str) -> tuple[Decimal, str]:

    t = _normalize(text)

    qty = Decimal("1")

    m = re.search(r"(\d+)\s*[x×]", t) or re.search(r"[x×]\s*(\d+)", t)

    if m:

        qty = Decimal(m.group(1))

    else:

        m2 = re.match(r"^(\d+)\s+(.+)$", t)

        if m2 and len(m2.group(2)) >= 2:

            qty = Decimal(m2.group(1))

    return qty, t





def _product_query_from_order_text(text: str) -> str:

    t = _normalize(_strip_fillers(text))

    for verb in _ORDER_VERBS:

        t = t.replace(_normalize(verb), " ")

    t = re.sub(r"\s+", " ", t).strip()

    _, rest = _parse_quantity(t)

    query = rest or t

    if not query or is_question_like(query) or is_how_to_order_question(text):

        return ""

    return query





def _find_product_for_order(db: Session, text: str) -> Product | None:

    query = _product_query_from_order_text(text)

    if not query:

        return None

    products = find_products_by_text(db, query, limit=3)

    if not products:

        return None

    best = products[0]

    if _score_product(query, best) >= 80:

        return best

    if len(products) == 1 and _score_product(query, best) >= 40:

        return best

    return None





def _reply_add_product(db: Session, session: WebChatSession, product: Product, qty: Decimal) -> HermesReply:

    add_product_to_cart(db, session, product, qty)

    r = HermesReply()

    r.add(f"✅ تمت إضافة {product.name_ar} × {qty}.\n\n" + format_cart_summary(session))

    return r





def _reply_unavailable(db: Session, text: str) -> HermesReply | None:

    if is_how_to_order_question(text) or is_question_like(text):

        return None

    query = _product_query_from_order_text(text)

    if not query or is_question_like(query):

        return None

    section = match_menu_section_code(query)

    if section:

        products = _products_for_section_code(db, section)

        if not products:

            r = HermesReply()

            r.add(f"عذراً، **{query}** غير متوفر حالياً.\nجرّب صنفاً آخر أو «منيو».")

            return r

    products = find_products_by_text(db, query, limit=1)

    if not products or _score_product(query, products[0]) < 40:

        r = HermesReply()

        r.add(

            f"عذراً، **{query.strip() or 'هذا الصنف'}** غير متاح للطلب حالياً.\n"

            "اكتب «منيو» أو اسم صنف آخر."

        )

        return r

    return None





def _needs_profile(session: WebChatSession) -> bool:

    return not (session.guest_phone or "").strip()





def _phone_prompt() -> str:

    return (

        "📱 **رقم هاتفك مطلوب** قبل إتمام الطلب.\n"

        "نستخدمه لـ:\n"

        "• فتح/ربط حسابك\n"

        "• التنسيق على الاستلام أو التوصيل\n"

        "• احتساب نقاط الولاء\n\n"

        "اكتب رقمك (10 أرقام) مثل: **0912345678**\n"

        "أو أضفه من زر 👤 بالأعلى."

    ).replace("**", "")





def _name_prompt() -> str:

    return (

        "👤 ما **اسمك**؟ (اختياري)\n"

        "اكتب اسمك أو «تخطي» للمتابعة."

    ).replace("**", "")





def _ensure_phone_or_prompt(session: WebChatSession) -> HermesReply | None:

    if not _needs_profile(session):

        return None

    session.order_phase = PHASE_GUEST_PHONE

    r = HermesReply()

    r.add(_phone_prompt())

    return r



def handle_chat_order(db: Session, session: WebChatSession, text: str) -> HermesReply | None:

    phase = (session.order_phase or "browse").strip()

    cleaned = _strip_fillers(text)

    t = _normalize(cleaned)



    if phase in (PHASE_COMPLETED, PHASE_FEEDBACK):

        if wants_order_intent(cleaned):

            clear_cart(session)

            session.order_phase = PHASE_BROWSE

            db.flush()

            product = _find_product_for_order(db, cleaned)

            if product is not None:

                qty, _ = _parse_quantity(_normalize(cleaned))

                return _reply_add_product(db, session, product, qty)

            r = HermesReply()

            r.add(

                "طبعاً! 😊 يمكنك الطلب في أي وقت.\n"

                "اكتب اسم الصنف — مثل: «اطلب توماهوك» — أو «منيو» لعرض الأقسام."

            )

            return r

        if phase == PHASE_FEEDBACK and session.feedback_rating is None:

            return None

        if phase == PHASE_COMPLETED:

            return None



    if phase == PHASE_SUBMITTED:

        r = HermesReply()

        r.add("✓ طلبك مسجّل وقيد المراجعة.\nللاستفسار اكتب «3» — للولاء: «نقاطي».")

        return r



    if phase == PHASE_GUEST_PHONE:

        phone = try_parse_guest_phone(cleaned)

        if phone:

            save_guest_phone(db, session, phone)

            db.flush()

            session.order_phase = PHASE_FULFILLMENT

            db.flush()

            r = HermesReply()

            r.add(

                f"✓ تم حفظ رقم {phone}.\n\n"

                "📦 **كيف تستلم الطلب؟**\n\n"

                "1 — استلام من المطعم\n"

                "2 — توصيل\n\n"

                "اكتب 1 أو 2."

            )

            return r

        r = HermesReply()

        r.add(_phone_prompt())

        return r



    if phase == PHASE_GUEST_NAME:

        skip = t in ("تخطي", "skip", "لا", "بدون", "متابعة")

        if not skip and len(cleaned.strip()) >= 2:

            session.guest_name = cleaned.strip()

            if (session.guest_phone or "").strip():

                save_guest_phone(db, session, session.guest_phone.strip())

            db.flush()

        session.order_phase = PHASE_FULFILLMENT

        db.flush()

        r = HermesReply()

        r.add(

            "📦 **كيف تستلم الطلب؟**\n\n"

            "1 — استلام من المطعم\n"

            "2 — توصيل\n\n"

            "اكتب 1 أو 2."

        )

        return r



    if any(w in t for w in _CANCEL_WORDS):

        clear_cart(session)

        r = HermesReply()

        r.add("🗑 تم إلغاء السلة.")

        return r



    if any(w in t for w in _CART_WORDS) and not wants_order_intent(cleaned):

        r = HermesReply()

        r.add(format_cart_summary(session))

        return r



    if phase == PHASE_AWAIT_RECEIPT:

        data = load_order_data(session)

        pm_name = (data.get("payment_method_name") or "").strip()

        if any(w in t for w in _RECEIPT_DONE):

            sale = attach_receipt_and_submit(db, session, note=cleaned)

            r = HermesReply()

            data_conf = load_order_data(session)
            parts = data_conf.get("confirmation_parts")
            if not (isinstance(parts, list) and parts):
                parts = order_confirmation_parts(db, session, sale, mark_intro=True)
            for _part in parts:
                r.add(str(_part))

            return r

        r = HermesReply()

        r.add(

            f"📎 يرجى رفع إيصال {pm_name or 'الدفع'} لتأكيد الطلب.\n"

            "أو اكتب «تم التحويل» بعد إتمام التحويل."

            + receipt_action_suffix()

        )

        return r



    if phase == PHASE_PAYMENT:

        need = _ensure_phone_or_prompt(session)

        if need is not None:

            db.flush()

            return need

        kind = select_payment_method(db, session, cleaned)

        if kind is None:

            r = HermesReply()

            r.add("لم أفهم وسيلة الدفع.\n\n" + format_payment_options(db, session))

            return r

        data = load_order_data(session)

        pm_name = (data.get("payment_method_name") or "").strip()

        pay_total = order_payment_amount(session)

        if kind == PaymentMethodKind.BANK:

            sale = create_or_update_before_bank(db, session)

            session.order_phase = PHASE_AWAIT_RECEIPT

            db.flush()

            r = HermesReply()

            r.add(

                f"💳 **{pm_name}** — {_fmt_pay_summary(session, pay_total)}\n\n"

                + bank_payment_instructions(db, method_name=pm_name)

                + f"\n\nبعد التحويل، اضغط الزر في الرسالة لرفع صورة الإيصال.\n"

                f"رقم طلبك: #{sale.id}"

                + receipt_action_suffix()

            )

            return r

        sale = submit_cash_order(db, session)

        r = HermesReply()

        data_conf = load_order_data(session)
            parts = data_conf.get("confirmation_parts")
            if not (isinstance(parts, list) and parts):
                parts = order_confirmation_parts(db, session, sale, mark_intro=True)
            for _part in parts:
                r.add(str(_part))

        return r



    if phase == PHASE_DELIVERY_ADDRESS:

        need = _ensure_phone_or_prompt(session)

        if need is not None:

            db.flush()

            return need

        session.order_phase = PHASE_PAYMENT

        db.flush()

        r = HermesReply()

        r.add(format_payment_options(db, session))

        return r



    if phase == PHASE_DELIVERY_ZONE:

        if select_delivery_zone(db, session, cleaned):

            session.order_phase = PHASE_PAYMENT

            db.flush()

            data = load_order_data(session)

            r = HermesReply()

            r.add(

                f"✓ منطقة: {data.get('delivery_zone_name')} — "

                f"{_fmt_price(delivery_fee_from_data(data))}\n\n"

                + format_payment_options(db, session)

            )

            return r

        r = HermesReply()

        r.add("لم أفهم المنطقة.\n\n" + format_delivery_zones(db))

        return r



    if phase == PHASE_FULFILLMENT or (

        any(w in t for w in _CONFIRM_WORDS + _CHECKOUT_WORDS) and cart_lines(session)

    ):

        if any(w in t for w in _CONFIRM_WORDS + _CHECKOUT_WORDS) and phase != PHASE_FULFILLMENT:

            need = _ensure_phone_or_prompt(session)

            if need is not None:

                db.flush()

                return need

            session.order_phase = PHASE_FULFILLMENT

            db.flush()

            r = HermesReply()

            r.add(

                "📦 **كيف تستلم الطلب؟**\n\n"

                "1 — استلام من المطعم\n"

                "2 — توصيل\n\n"

                "اكتب 1 أو 2."

            )

            return r



    if phase == PHASE_FULFILLMENT:

        data = load_order_data(session)

        if t in ("1", "١") or any(w in t for w in _PICKUP_WORDS):

            data["fulfillment"] = "PICKUP"

            data["delivery_zone_id"] = None

            data["delivery_fee"] = "0"

            session.order_json = json.dumps(data, ensure_ascii=False)

            session.order_phase = PHASE_PAYMENT

            db.flush()

            r = HermesReply()

            r.add("✓ استلام من المطعم\n\n" + format_payment_options(db, session))

            return r

        if t in ("2", "٢") or any(w in t for w in _DELIVERY_WORDS):

            data["fulfillment"] = "DELIVERY"

            session.order_json = json.dumps(data, ensure_ascii=False)

            session.order_phase = PHASE_DELIVERY_ZONE

            db.flush()

            r = HermesReply()

            r.add(format_delivery_zones(db))

            return r

        r = HermesReply()

        r.add("اكتب **1** للاستلام أو **2** للتوصيل.")

        return r



    if wants_order_intent(cleaned) or (

        phase in (PHASE_CART, PHASE_BROWSE) and _find_product_for_order(db, cleaned)

    ):

        qty, _ = _parse_quantity(_normalize(cleaned))

        product = _find_product_for_order(db, cleaned)

        if product is None:

            unavail = _reply_unavailable(db, cleaned)

            if unavail is not None:

                return unavail

            if wants_order_intent(cleaned):

                r = HermesReply()

                r.add(

                    "اكتب اسم الصنف مع «اطلب» — مثل: «اطلب برجر».\n"

                    "أو «منيو» لعرض الأقسام."

                )

                session.order_phase = PHASE_BROWSE

                db.flush()

                return r

            return None

        return _reply_add_product(db, session, product, qty)



    m = re.match(r"(?:احذف|حذف|remove)\s*(\d+)", t)

    if m and cart_lines(session):

        if remove_cart_line(session, int(m.group(1))):

            r = HermesReply()

            r.add("🗑 تم الحذف.\n\n" + format_cart_summary(session))

            return r



    if phase == PHASE_CART and cart_lines(session):

        return None



    return None





def delivery_fee_from_data(data: dict) -> Decimal:

    try:

        return Decimal(str(data.get("delivery_fee") or 0))

    except Exception:

        return Decimal("0")





def _fmt_price(value: Decimal) -> str:

    return f"{Decimal(str(value)).quantize(Decimal('0.001'))} د.ل"





def _fmt_pay_summary(session: WebChatSession, pay_amt: Decimal) -> str:

    fee = customer_total(session) - pay_amt

    if fee > 0:

        return f"الأصناف {_fmt_price(pay_amt)} + توصيل {_fmt_price(fee)} = {_fmt_price(customer_total(session))}"

    return f"المبلغ {_fmt_price(pay_amt)}"





def create_or_update_before_bank(db: Session, session: WebChatSession):

    from modules.messaging.chat_order_service import create_or_update_chat_sale



    return create_or_update_chat_sale(db, session)





def handle_receipt_upload(

    db: Session, session: WebChatSession, proof_filename: str

) -> HermesReply:

    phase = (session.order_phase or "").strip()

    if phase != PHASE_AWAIT_RECEIPT:

        r = HermesReply()

        if phase == PHASE_SUBMITTED:

            r.add("تم استلام طلبك مسبقاً.")

        else:

            r.add(

                "رفع الإيصال متاح فقط بعد اختيار الدفع المصرفي وإتمام التحويل.\n"

                "أكمل الطلب أولاً ثم سيظهر زر رفع الإيصال."

            )

        return r

    sale = attach_receipt_and_submit(db, session, proof_filename=proof_filename)

    r = HermesReply()

    data_conf = load_order_data(session)
            parts = data_conf.get("confirmation_parts")
            if not (isinstance(parts, list) and parts):
                parts = order_confirmation_parts(db, session, sale, mark_intro=True)
            for _part in parts:
                r.add(str(_part))

    return r


