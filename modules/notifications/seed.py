"""قوالب وقواعد افتراضية لمحرك الإشعارات — المرحلة 1."""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from modules.notifications.events import (
    CUSTOMER_CREATED,
    HOTEL_BOOKING_CONFIRMED,
    HOTEL_BOOKING_CREATED,
    HOTEL_ONLINE_BOOKING_REQUEST,
    HOTEL_CHECKOUT_REMINDER,
    HOTEL_NIGHT_PAYMENT_DUE,
    HOTEL_PAYMENT_RECEIVED,
    HOTEL_ROOM_MAINTENANCE,
    HOTEL_SHIFT_CLOSED,
    HOTEL_SHIFT_OPENED,
    HOTEL_SHIFT_OVERDUE,
    HOTEL_UNPAID_SERVICE_ADDED,
    INVENTORY_BOM_MISSING_COST,
    INVENTORY_EXPIRED_ITEM,
    INVENTORY_EXPIRY_WARNING,
    INVENTORY_LOW_STOCK,
    INVENTORY_NEGATIVE_STOCK,
    INVENTORY_OUT_OF_STOCK,
    INVENTORY_PURCHASE_NEEDED,
    INVENTORY_PURCHASE_RECEIVED,
    INVENTORY_RECIPE_COST_CHANGED,
    INVENTORY_STOCK_ADJUSTMENT_CREATED,
    INVENTORY_STOCK_REPLENISHED,
    INVENTORY_TRANSFER_RECEIVED,
    INVENTORY_UNUSUAL_STOCK_MOVEMENT,
    INVENTORY_WASTE_RECORDED,
    KITCHEN_ITEM_CANCELLED,
    KITCHEN_TICKET_CREATED,
    LOYALTY_ACCOUNT_CREATED,
    LOYALTY_POINTS_EARNED,
    LOYALTY_POINTS_REDEEMED,
    ORDER_CANCELLED,
    ORDER_CONFIRMED,
    ORDER_CREATED,
    ORDER_DELIVERED,
    ORDER_DRIVER_ASSIGNED,
    ORDER_OUT_FOR_DELIVERY,
    ORDER_SENT_TO_KITCHEN,
    POS_CASH_SHORTAGE,
    POS_INVOICE_CANCEL_REQUESTED,
    POS_ITEM_VOID_REQUESTED,
    POS_SHIFT_CLOSED,
    POS_SHIFT_OPENED,
    POS_CASH_OVERAGE,
    TREASURY_BALANCE_UPDATE,
    TREASURY_MOVEMENT,
    TREASURY_SHIFT_CLOSED,
    REFERRAL_FIRST_ORDER,
    REFERRAL_LINK_CREATED,
    REFERRAL_PRODUCT_SHARED,
    REFERRAL_REFERRER_REWARDED,
    HR_PAYROLL_PAID,
    HR_PAYROLL_POSTED,
)
from modules.notifications.models import NotificationRule, NotificationTemplate


def _upsert_template(
    db: Session,
    *,
    name: str,
    event_key: str,
    recipient_type: str,
    body: str,
    message_type: str = "text",
) -> NotificationTemplate:
    row = db.scalar(
        select(NotificationTemplate).where(
            NotificationTemplate.event_key == event_key,
            NotificationTemplate.recipient_type == recipient_type,
            NotificationTemplate.name == name,
        )
    )
    if row is None:
        row = NotificationTemplate(
            name=name,
            event_key=event_key,
            recipient_type=recipient_type,
            channel="whatsapp",
            message_type=message_type,
            body_template=body,
            is_active=True,
        )
        db.add(row)
        db.flush()
        return row
    if _body_needs_default(row.body_template):
        row.body_template = body
    if message_type and row.message_type == "text" and message_type != "text":
        row.message_type = message_type
    row.is_active = True
    return row


LOYALTY_REDEEM_TEMPLATE_BODY = (
    "🎉 مرحباً {customer_name}،\n\n"
    "تم استخدام {points} نقطة من رصيدك كخصم بقيمة {points_value} د.ل على فاتورتك.\n\n"
    "{earn_line}"
    "💳 رصيدك الحالي: {balance} نقطة ({balance_value} د.ل)\n\n"
    "🧾 الفاتورة: #{sale_id}\n\n"
    "شكراً لزيارتك، ونتطلع لخدمتك مجدداً في {store_name}. ❤️"
)


LOYALTY_EARN_TEMPLATE_BODY = (
    "مرحباً {customer_name}،\n"
    "اكتسبت {points} نقطة من {store_name} (قيمتها {points_value} د.ل).\n"
    "رصيدك الآن: {balance} نقطة = {balance_value} د.ل.\n"
    "فاتورة #{sale_id}"
)


REFERRAL_LINK_TEMPLATE_BODY = (
    "مرحباً {customer_name} في برنامج الولاء!\n"
    "كود إحالتك الشخصي: {referral_code}\n"
    "شاركه مع أصدقائك، وعند استخدامه حسب شروط المطعم تكسبون نقاطاً."
)


REFERRAL_PRODUCT_SHARE_BODY = (
    "مرحباً {customer_name}!\n\n"
    "شارك {product_name} من {store_name} مع أصدقائك:\n"
    "{share_url}\n\n"
    "🎁 كود إحالتك: {referral_code}"
)


REFERRAL_FIRST_ORDER_CUSTOMER_BODY = (
    "🎉 أهلاً بك في {store_name}.\n\n"
    "تم تطبيق كود الإحالة بنجاح، وأُضيفت إلى حسابك {buyer_points} نقطة "
    "= ({points_value} د.ل) كمكافأة ترحيبية.\n\n"
    "🧾 الفاتورة: #{sale_id}\n\n"
    "{referral_share_block}"
    "شكراً لانضمامك إلى برنامج الولاء، ونتمنى لك تجربة مميزة. ❤️"
)


REFERRAL_FIRST_ORDER_ADMIN_BODY = (
    "🎁 إحالة ناجحة — فاتورة #{order_id}\n"
    "المُحيل: {referrer_name} (+{referrer_points} نقطة)\n"
    "المشتري: {customer_name} (+{buyer_points} نقطة)\n"
    "كود الإحالة: {referral_code}"
)


REFERRAL_REFERRER_REWARDED_BODY = (
    "🎉 مبروك {customer_name} تم استخدام كود الإحالة الخاص بك بنجاح.\n\n"
    "أُضيفت إلى حسابك {referrer_points} نقطة = ({points_value} د.ل) كمكافأة إحالة.\n\n"
    "🧾 الفاتورة: #{sale_id}\n\n"
    "شكراً لمشاركتك {store_name} مع أصدقائك، واستمر بدعوتهم لتحصل على المزيد من نقاط المكافآت. ❤️"
)


KITCHEN_TICKET_BODY = (
    "🍳 تذكرة مطبخ #{ticket_id}\n"
    "طلب #{order_id} — {store_name}\n"
    "القسم: {section_name}"
)


KITCHEN_CANCEL_BODY = (
    "❌ إلغاء مطبخ — طلب #{order_id}\n"
    "عدد الأصناف: {item_count}\n"
    "السبب: {reason}"
)


POS_OVERAGE_BODY = (
    "💰 فائض جلسة #{shift_id}\n"
    "الكاشير: {cashier_name}\n"
    "الفائض: {shortage} د.ل"
)


INVENTORY_PURCHASE_NEEDED_BODY = (
    "⚠️ حاجة شراء — {store_name}\n"
    "عدد الأصناف: {item_count} — {digest_date}\n\n"
    "{message}"
)


INVENTORY_PURCHASE_RECEIVED_BODY = (
    "✅ استلام فاتورة شراء #{purchase_id}\n"
    "المورّد: {supplier}\n"
    "المبلغ: {amount} د.ل\n"
    "تم تحديث المخزون."
)


INVENTORY_BOM_MISSING_BODY = (
    "⚠️ مكوّنات بدون تكلفة — {product_name}\n"
    "{message}"
)


HR_PAYROLL_POSTED_BODY = (
    "📋 اعتماد كشف رواتب {period_label}\n"
    "عدد الموظفين: {employee_count}\n"
    "إجمالي الصافي: {total_net} د.ل"
)


def _body_needs_default(body: str | None) -> bool:
    b = (body or "").strip()
    return b in ("", "{message}", "{{message}}")


def _patch_loyalty_earn_templates(db: Session) -> None:
    """ترقية قالب كسب النقاط ليعرض قيمة النقاط بالدينار."""
    rows = list(
        db.scalars(
            select(NotificationTemplate).where(
                NotificationTemplate.event_key == LOYALTY_POINTS_EARNED,
                NotificationTemplate.recipient_type == "customer",
            )
        ).all()
    )
    for row in rows:
        body = row.body_template or ""
        if "{points_value}" in body and "{balance_value}" in body:
            continue
        row.body_template = LOYALTY_EARN_TEMPLATE_BODY
    try:
        from modules.messaging.models import MessageTemplate

        legacy = db.scalar(
            select(MessageTemplate).where(MessageTemplate.code == "loyalty.earned")
        )
        if legacy is not None and "{points_value}" not in (legacy.body_text or ""):
            legacy.body_text = LOYALTY_EARN_TEMPLATE_BODY
    except ImportError:
        pass


def _is_loyalty_redeem_template_current(body: str | None) -> bool:
    b = (body or "").strip()
    return "🎉" in b and "تم استخدام" in b and "💳 رصيدك الحالي" in b


def _patch_loyalty_redeem_templates(db: Session) -> None:
    """ترقية قالب خصم النقاط للصيغة الجديدة."""
    rows = list(
        db.scalars(
            select(NotificationTemplate).where(
                NotificationTemplate.event_key == LOYALTY_POINTS_REDEEMED,
                NotificationTemplate.recipient_type == "customer",
            )
        ).all()
    )
    for row in rows:
        if _is_loyalty_redeem_template_current(row.body_template):
            continue
        row.body_template = LOYALTY_REDEEM_TEMPLATE_BODY
    try:
        from modules.messaging.models import MessageTemplate

        legacy = db.scalar(
            select(MessageTemplate).where(MessageTemplate.code == "loyalty.redeemed")
        )
        if legacy is not None and not _is_loyalty_redeem_template_current(legacy.body_text):
            legacy.body_text = LOYALTY_REDEEM_TEMPLATE_BODY
    except ImportError:
        pass


def _is_referral_first_order_buyer_template_current(body: str | None) -> bool:
    b = (body or "").strip()
    return (
        "🎉 أهلاً بك" in b
        and "مكافأة ترحيبية" in b
        and "{buyer_points_value}" not in b
        and "{referral_code_line}" not in b
        and "{referral_share_block}" in b
    )


def _patch_referral_first_order_buyer_templates(db: Session) -> None:
    """ترقية رسالة المشتري عند استخدام كود إحالة."""
    rows = list(
        db.scalars(
            select(NotificationTemplate).where(
                NotificationTemplate.event_key == REFERRAL_FIRST_ORDER,
                NotificationTemplate.recipient_type == "customer",
            )
        ).all()
    )
    for row in rows:
        if _is_referral_first_order_buyer_template_current(row.body_template):
            continue
        row.body_template = REFERRAL_FIRST_ORDER_CUSTOMER_BODY


def _is_referral_referrer_template_current(body: str | None) -> bool:
    b = (body or "").strip()
    return "🎉 مبروك" in b and "مكافأة إحالة" in b


def _patch_referral_referrer_templates(db: Session) -> None:
    """ترقية قالب إشعار المحيل عند استخدام كود الإحالة."""
    rows = list(
        db.scalars(
            select(NotificationTemplate).where(
                NotificationTemplate.event_key == REFERRAL_REFERRER_REWARDED,
                NotificationTemplate.recipient_type == "referrer",
            )
        ).all()
    )
    for row in rows:
        if _is_referral_referrer_template_current(row.body_template):
            continue
        row.body_template = REFERRAL_REFERRER_REWARDED_BODY


def _patch_referral_link_templates(db: Session) -> None:
    """ترقية قالب كود الإحالة من {message} إلى نص قابل للتعديل."""
    rows = list(
        db.scalars(
            select(NotificationTemplate).where(
                NotificationTemplate.event_key == REFERRAL_LINK_CREATED,
                NotificationTemplate.recipient_type == "customer",
            )
        ).all()
    )
    for row in rows:
        if _body_needs_default(row.body_template):
            row.body_template = REFERRAL_LINK_TEMPLATE_BODY


def _patch_placeholder_templates(db: Session) -> None:
    """ترقية القوالب التي تحتوي {message} فقط إلى نصوص قابلة للتعديل."""
    patches: list[tuple[str, str, str | None, str]] = [
        (REFERRAL_PRODUCT_SHARED, "customer", "مشاركة منتج", REFERRAL_PRODUCT_SHARE_BODY),
        (REFERRAL_FIRST_ORDER, "customer", "إحالة ناجحة", REFERRAL_FIRST_ORDER_CUSTOMER_BODY),
        (REFERRAL_FIRST_ORDER, "admin", "إحالة (إدارة)", REFERRAL_FIRST_ORDER_ADMIN_BODY),
        (KITCHEN_TICKET_CREATED, "admin", "تذكرة مطبخ", KITCHEN_TICKET_BODY),
        (KITCHEN_ITEM_CANCELLED, "admin", "إلغاء مطبخ", KITCHEN_CANCEL_BODY),
        (POS_CASH_OVERAGE, "admin", "فائض جلسة", POS_OVERAGE_BODY),
        (INVENTORY_PURCHASE_NEEDED, "inventory_manager", "ملخص حاجة شراء", INVENTORY_PURCHASE_NEEDED_BODY),
        (INVENTORY_PURCHASE_RECEIVED, "inventory_manager", "استلام شراء", INVENTORY_PURCHASE_RECEIVED_BODY),
        (INVENTORY_PURCHASE_RECEIVED, "admin", "استلام شراء", INVENTORY_PURCHASE_RECEIVED_BODY),
        (INVENTORY_BOM_MISSING_COST, "inventory_manager", "مكوّن بدون تكلفة", INVENTORY_BOM_MISSING_BODY),
        (HR_PAYROLL_POSTED, "hr_manager", "اعتماد رواتب", HR_PAYROLL_POSTED_BODY),
    ]
    for event_key, recipient_type, name, body in patches:
        q = select(NotificationTemplate).where(
            NotificationTemplate.event_key == event_key,
            NotificationTemplate.recipient_type == recipient_type,
        )
        if name:
            q = q.where(NotificationTemplate.name == name)
        for row in db.scalars(q).all():
            if _body_needs_default(row.body_template):
                row.body_template = body
    fallback: dict[tuple[str, str], str] = {
        (REFERRAL_PRODUCT_SHARED, "customer"): REFERRAL_PRODUCT_SHARE_BODY,
        (REFERRAL_FIRST_ORDER, "customer"): REFERRAL_FIRST_ORDER_CUSTOMER_BODY,
        (REFERRAL_FIRST_ORDER, "admin"): REFERRAL_FIRST_ORDER_ADMIN_BODY,
        (KITCHEN_TICKET_CREATED, "admin"): KITCHEN_TICKET_BODY,
        (KITCHEN_ITEM_CANCELLED, "admin"): KITCHEN_CANCEL_BODY,
        (POS_CASH_OVERAGE, "admin"): POS_OVERAGE_BODY,
        (INVENTORY_PURCHASE_NEEDED, "inventory_manager"): INVENTORY_PURCHASE_NEEDED_BODY,
        (INVENTORY_PURCHASE_RECEIVED, "inventory_manager"): INVENTORY_PURCHASE_RECEIVED_BODY,
        (INVENTORY_PURCHASE_RECEIVED, "admin"): INVENTORY_PURCHASE_RECEIVED_BODY,
        (INVENTORY_BOM_MISSING_COST, "inventory_manager"): INVENTORY_BOM_MISSING_BODY,
        (HR_PAYROLL_POSTED, "hr_manager"): HR_PAYROLL_POSTED_BODY,
        (REFERRAL_LINK_CREATED, "customer"): REFERRAL_LINK_TEMPLATE_BODY,
    }
    for row in db.scalars(select(NotificationTemplate)).all():
        if not _body_needs_default(row.body_template):
            continue
        body = fallback.get((row.event_key, row.recipient_type))
        if body:
            row.body_template = body


def _upsert_rule(
    db: Session,
    *,
    event_key: str,
    recipient_type: str,
    template_id: int,
    throttle_minutes: int | None = None,
    condition_json: str = "{}",
) -> None:
    existing = db.scalar(
        select(NotificationRule).where(
            NotificationRule.event_key == event_key,
            NotificationRule.recipient_type == recipient_type,
            NotificationRule.template_id == template_id,
        )
    )
    if existing is not None:
        if not existing.is_active:
            existing.is_active = True
        if throttle_minutes is not None and existing.throttle_minutes is None:
            existing.throttle_minutes = throttle_minutes
        if condition_json and condition_json != "{}" and existing.condition_json == "{}":
            existing.condition_json = condition_json
        return
    db.add(
        NotificationRule(
            event_key=event_key,
            recipient_type=recipient_type,
            channel="whatsapp",
            template_id=template_id,
            is_active=True,
            condition_json=condition_json or "{}",
            throttle_minutes=throttle_minutes,
        )
    )


def _import_legacy_messaging_templates(db: Session) -> None:
    """استيراد لمرة واحدة من message_templates إن وُجدت."""
    try:
        from modules.messaging.models import MessageRule, MessageTemplate
    except ImportError:
        return
    count = db.scalar(select(func.count()).select_from(NotificationTemplate)) or 0
    if count > 0:
        return
    legacy = list(db.scalars(select(MessageTemplate).limit(50)).all())
    if not legacy:
        return
    mapping = {
        "loyalty.earned": (LOYALTY_POINTS_EARNED, "customer"),
        "stock.low": (INVENTORY_LOW_STOCK, "admin"),
    }
    for lt in legacy:
        pair = mapping.get((lt.code or "").strip())
        if not pair:
            continue
        event_key, recipient = pair
        tpl = _upsert_template(
            db,
            name=lt.name or lt.code,
            event_key=event_key,
            recipient_type=recipient,
            body=lt.body_text or "",
        )
        _upsert_rule(
            db,
            event_key=event_key,
            recipient_type=recipient,
            template_id=tpl.id,
            throttle_minutes=360 if event_key == INVENTORY_LOW_STOCK else None,
        )


def _seed_treasury(db: Session) -> None:
    shift_tpl = _upsert_template(
        db,
        name="إغلاق جلسة وخزينة",
        event_key=TREASURY_SHIFT_CLOSED,
        recipient_type="admin",
        body=(
            "تم إغلاق جلسة البيع #{shift_id} بواسطة {cashier_name}.\n"
            "كاش: معدود {counted_cash} / متوقع {expected_cash} / فرق {cash_difference}\n"
            "مصرف: معدود {counted_bank} / متوقع {expected_bank} / فرق {bank_difference}\n"
            "رصيد المطعم الآن: كاش {restaurant_cash_balance} · مصرف {restaurant_bank_balance}\n"
            "رصيد الفندق الآن: كاش {hotel_cash_balance} · مصرف {hotel_bank_balance}\n"
            "رصيد مشترك/غير مصنّف: كاش {shared_cash_balance} · مصرف {shared_bank_balance}"
        ),
    )
    movement_tpl = _upsert_template(
        db,
        name="حركة خزينة",
        event_key=TREASURY_MOVEMENT,
        recipient_type="admin",
        body=(
            "حركة خزينة: {movement_label}\n"
            "المبلغ: {amount} د.ل\n"
            "من: {from_method} → إلى: {to_method}\n"
            "رصيد المطعم: كاش {restaurant_cash_balance} · مصرف {restaurant_bank_balance}\n"
            "رصيد الفندق: كاش {hotel_cash_balance} · مصرف {hotel_bank_balance}\n"
            "رصيد مشترك/غير مصنّف: كاش {shared_cash_balance} · مصرف {shared_bank_balance}"
        ),
    )
    balance_tpl = _upsert_template(
        db,
        name="تحديث أرصدة الخزائن",
        event_key=TREASURY_BALANCE_UPDATE,
        recipient_type="admin",
        body=(
            "تحديث أرصدة الخزائن:\n"
            "المطعم: كاش {restaurant_cash_balance} · مصرف {restaurant_bank_balance}\n"
            "الفندق: كاش {hotel_cash_balance} · مصرف {hotel_bank_balance}\n"
            "المشترك/غير المصنّف: كاش {shared_cash_balance} · مصرف {shared_bank_balance}\n"
            "الإجمالي: كاش {cash_balance} · مصرف {bank_balance}"
        ),
    )
    for event_key, tpl_id in (
        (TREASURY_SHIFT_CLOSED, shift_tpl.id),
        (TREASURY_MOVEMENT, movement_tpl.id),
        (TREASURY_BALANCE_UPDATE, balance_tpl.id),
    ):
        _upsert_rule(
            db,
            event_key=event_key,
            recipient_type="admin",
            template_id=tpl_id,
            throttle_minutes=0,
        )


def _seed_hotel(db: Session) -> None:
    templates = [
        (
            "حجز فندقي جديد",
            HOTEL_BOOKING_CREATED,
            "مرحباً {guest_name}، تم تسجيل حجزك في {store_name}.\n"
            "رقم الحجز: {booking_reference}\n"
            "الوصول: {check_in_date} · المغادرة: {check_out_date}\n"
            "الشقة/الغرفة: {room_name} {room_type}\n"
            "الإجمالي: {booking_total} د.ل · المدفوع: {paid_amount} · المتبقي: {balance_due}\n"
            "{booking_url}",
            "interactive",
            '[{"text":"عرض الحجز","id":"hotel_booking_view:{booking_id}"},{"text":"تأكيد الاستلام","id":"hotel_booking_ack:{booking_id}"}]',
        ),
        (
            "تأكيد حجز فندقي",
            HOTEL_BOOKING_CONFIRMED,
            "تم تأكيد حجزك يا {guest_name}.\n"
            "رقم الحجز: {booking_reference}\n"
            "موعد الوصول: {check_in_date}\n"
            "موعد المغادرة: {check_out_date}\n"
            "المتبقي حتى الآن: {balance_due} د.ل\n"
            "{booking_url}",
            "interactive",
            '[{"text":"عرض الحجز","id":"hotel_booking_view:{booking_id}"}]',
        ),
        (
            "تذكير مغادرة الفندق",
            HOTEL_CHECKOUT_REMINDER,
            "تذكير لطيف يا {guest_name}: موعد مغادرتك اليوم {check_out_date}.\n"
            "المتبقي على الحساب: {balance_due} د.ل\n"
            "يرجى مراجعة الاستقبال قبل المغادرة.",
            "interactive",
            '[{"text":"عرض الحساب","id":"hotel_booking_view:{booking_id}"}]',
        ),
        (
            "ليلة فندقية مستحقة السداد",
            HOTEL_NIGHT_PAYMENT_DUE,
            "تنبيه سداد يا {guest_name}: توجد ليلة/رصيد مستحق على حجزك #{booking_reference}.\n"
            "المتبقي الحالي: {balance_due} د.ل\n"
            "يمكنك السداد لدى الاستقبال.",
            "interactive",
            '[{"text":"عرض الحساب","id":"hotel_booking_view:{booking_id}"}]',
        ),
        (
            "خدمة فندقية غير مدفوعة",
            HOTEL_UNPAID_SERVICE_ADDED,
            "تمت إضافة خدمة على حساب إقامتك يا {guest_name}:\n"
            "{service_name} — {service_amount} د.ل\n"
            "المتبقي الحالي: {balance_due} د.ل",
            "interactive",
            '[{"text":"عرض الحساب","id":"hotel_booking_view:{booking_id}"}]',
        ),
        (
            "سداد حجز فندقي",
            HOTEL_PAYMENT_RECEIVED,
            "تم استلام سداد من حجزك يا {guest_name}.\n"
            "المبلغ: {payment_amount} د.ل\n"
            "طريقة الدفع: {payment_method}\n"
            "المتبقي بعد السداد: {balance_due} د.ل\n"
            "{invoice_url}",
            "interactive",
            '[{"text":"عرض الإيصال","id":"hotel_booking_view:{booking_id}"}]',
        ),
    ]
    for name, event_key, body, msg_type, buttons in templates:
        tpl = _upsert_template(
            db,
            name=name,
            event_key=event_key,
            recipient_type="customer",
            body=body,
            message_type=msg_type,
        )
        if not (tpl.buttons_json or "").strip():
            tpl.buttons_json = buttons
        _upsert_rule(
            db,
            event_key=event_key,
            recipient_type="customer",
            template_id=tpl.id,
            throttle_minutes=0,
        )

    t_online = _upsert_template(
        db,
        name="طلب حجز أونلاين — موظف",
        event_key=HOTEL_ONLINE_BOOKING_REQUEST,
        recipient_type="admin",
        body=(
            "🏨 *طلب حجز من المتجر الأونلاين*\n"
            "المرجع: {booking_reference}\n"
            "الضيف: {guest_name} — {guest_phone}\n"
            "الشقة: {room_name}\n"
            "الوصول: {check_in_date} → {check_out_date}\n"
            "الإجمالي: {booking_total} د.ل\n"
            "تواصل مع الزبون ثم أكّد الحجز من النظام."
        ),
    )
    _upsert_rule(
        db,
        event_key=HOTEL_ONLINE_BOOKING_REQUEST,
        recipient_type="admin",
        template_id=t_online.id,
        throttle_minutes=0,
    )

    t_maint = _upsert_template(
        db,
        name="طلب صيانة شقة",
        event_key=HOTEL_ROOM_MAINTENANCE,
        recipient_type="maintenance_staff",
        body=(
            "🔧 *طلب صيانة — {store_name}*\n\n"
            "الشقة: *{room_name}* (#{room_number})\n"
            "نوع العطل: *{issue_label}*\n"
            "التفاصيل: {issue_details}\n"
            "{reported_by_line}"
            "\nالشقة غير متاحة للإيجار حتى اكتمال الإصلاح."
        ),
    )
    _upsert_rule(
        db,
        event_key=HOTEL_ROOM_MAINTENANCE,
        recipient_type="maintenance_staff",
        template_id=t_maint.id,
        throttle_minutes=0,
    )

    t_hshift_o = _upsert_template(
        db,
        name="فتح وردية فندق",
        event_key=HOTEL_SHIFT_OPENED,
        recipient_type="admin",
        body=(
            "🏨 فتح وردية: {shift_name} (#{shift_number})\n"
            "المسؤول: {operator_name}\n"
            "موعد الإقفال المحدد: {scheduled_end}"
        ),
    )
    _upsert_rule(db, event_key=HOTEL_SHIFT_OPENED, recipient_type="admin", template_id=t_hshift_o.id)

    t_hshift_c = _upsert_template(
        db,
        name="إقفال وردية فندق",
        event_key=HOTEL_SHIFT_CLOSED,
        recipient_type="admin",
        body=(
            "✅ إقفال {shift_name} (#{shift_number})\n"
            "بواسطة: {operator_name}\n"
            "إيراد: {revenue} د.ل · مصروف: {expenses} د.ل · صافي: {net_total} د.ل\n"
            "({payment_count} قبض · {expense_count} مصروف)"
        ),
    )
    _upsert_rule(db, event_key=HOTEL_SHIFT_CLOSED, recipient_type="admin", template_id=t_hshift_c.id)

    t_hshift_od = _upsert_template(
        db,
        name="تأخر إقفال وردية",
        event_key=HOTEL_SHIFT_OVERDUE,
        recipient_type="hotel_shift_supervisor",
        body=(
            "⚠️ *تأخر إقفال وردية — {store_name}*\n\n"
            "الوردية: *{shift_name}* (#{shift_number})\n"
            "المسؤول: {operator_name}\n"
            "كان يجب الإقفال: {scheduled_end}\n"
            "متأخر: {overdue_minutes} دقيقة\n\n"
            "يُرجى تنبيه الموظف لإقفال الوردية فوراً."
        ),
    )
    _upsert_rule(
        db,
        event_key=HOTEL_SHIFT_OVERDUE,
        recipient_type="hotel_shift_supervisor",
        template_id=t_hshift_od.id,
        throttle_minutes=60,
    )


def ensure_notification_defaults(db: Session) -> None:
    t_order_c = _upsert_template(
        db,
        name="تأكيد طلب جديد",
        event_key=ORDER_CREATED,
        recipient_type="customer",
        body=(
            "مرحباً {customer_name}،\n"
            "تم استلام طلبك #{order_id} في {store_name}.\n"
            "الإجمالي: {total}\n"
            "شكراً لثقتك بنا."
        ),
    )
    t_order_d = _upsert_template(
        db,
        name="تسليم الطلب",
        event_key=ORDER_DELIVERED,
        recipient_type="customer",
        message_type="interactive",
        body=(
            "مرحباً {customer_name}،\n"
            "تم تسليم طلبك #{order_id}.\n"
            "نتمنى لك وجبة شهية — {store_name}\n"
            "اضغط «استلمت الطلب» عند الاستلام."
        ),
    )
    if not (t_order_d.buttons_json or "").strip():
        t_order_d.buttons_json = (
            '[{"text":"✓ استلمت الطلب","id":"order_received:{order_id}"}]'
        )
    t_loyalty = _upsert_template(
        db,
        name="اكتساب نقاط",
        event_key=LOYALTY_POINTS_EARNED,
        recipient_type="customer",
        body=LOYALTY_EARN_TEMPLATE_BODY,
    )
    t_stock = _upsert_template(
        db,
        name="مخزون منخفض",
        event_key=INVENTORY_LOW_STOCK,
        recipient_type="admin",
        body=(
            "⚠️ تنبيه مخزون — {store_name}\n"
            "{message}\n"
            "الصنف: {product_name}\n"
            "الرصيد: {balance} {unit}\n"
            "حد إعادة الطلب: {reorder_level}"
        ),
    )
    _upsert_rule(db, event_key=ORDER_CREATED, recipient_type="customer", template_id=t_order_c.id)
    _upsert_rule(db, event_key=ORDER_DELIVERED, recipient_type="customer", template_id=t_order_d.id)
    _upsert_rule(
        db,
        event_key=LOYALTY_POINTS_EARNED,
        recipient_type="customer",
        template_id=t_loyalty.id,
    )
    _upsert_rule(
        db,
        event_key=INVENTORY_LOW_STOCK,
        recipient_type="admin",
        template_id=t_stock.id,
        throttle_minutes=360,
    )
    _seed_phase2(db)
    _seed_phase3(db)
    _seed_phase4(db)
    _seed_phase5(db)
    _seed_treasury(db)
    _seed_hotel(db)
    _seed_default_routes(db)
    _patch_loyalty_earn_templates(db)
    _patch_loyalty_redeem_templates(db)
    _patch_referral_link_templates(db)
    _patch_referral_first_order_buyer_templates(db)
    _patch_referral_referrer_templates(db)
    _patch_placeholder_templates(db)


def _seed_default_routes(db: Session) -> None:
    import json

    from modules.notifications.models import NotificationRoute
    from modules.settings.service import get_setting

    inv_phones = (get_setting(db, "notification_inventory_phones") or "").strip()
    defaults = [
        (
            "المخزون والمشتريات",
            ["inventory.*"],
            inv_phones,
            "inventory_manager",
            10,
        ),
        (
            "الطلبات والتوصيل",
            ["order.*"],
            "",
            "admin",
            20,
        ),
        (
            "نقطة البيع والمطبخ",
            ["pos.*", "kitchen.*"],
            "",
            "admin",
            30,
        ),
        (
            "الخزينة والأرصدة",
            ["treasury.*"],
            "",
            "admin",
            35,
        ),
        (
            "الفندق والحجوزات",
            ["hotel.*"],
            "",
            "admin",
            37,
        ),
        (
            "الموارد البشرية",
            ["hr.*"],
            "",
            "hr_manager",
            40,
        ),
    ]
    for name, patterns, phones, scope, order in defaults:
        exists = db.scalar(
            select(NotificationRoute.id).where(NotificationRoute.name_ar == name)
        )
        if exists is not None:
            continue
        row = NotificationRoute(
            name_ar=name,
            event_patterns_json=json.dumps(patterns, ensure_ascii=False),
            phones=phones,
            recipient_scope=scope,
            is_active=True,
            sort_order=order,
            notes="مسار افتراضي — عدّل الأرقام من إعدادات التوجيه.",
        )
        db.add(row)
    db.flush()


def _seed_phase5(db: Session) -> None:
    from modules.notifications.events import (
        HR_ADVANCE_GIVEN,
        HR_ATTENDANCE_CHECK_IN,
        HR_ATTENDANCE_CHECK_OUT,
        HR_DEDUCTION_CREATED,
        HR_PAYROLL_PAID,
        HR_PAYROLL_POSTED,
    )

    t_in = _upsert_template(
        db,
        name="حضور موظف",
        event_key=HR_ATTENDANCE_CHECK_IN,
        recipient_type="employee",
        body=(
            "مرحباً {employee_name}،\n"
            "تم تسجيل حضورك: {check_in_time}\n"
            "مصدر التسجيل: {source}"
        ),
    )
    t_out = _upsert_template(
        db,
        name="انصراف موظف",
        event_key=HR_ATTENDANCE_CHECK_OUT,
        recipient_type="employee",
        body=(
            "مرحباً {employee_name}،\n"
            "ملخص يوم العمل:\n"
            "دخول: {check_in_time} — خروج: {check_out_time}\n"
            "ساعات العمل: {hours_worked}\n"
            "الحالة: {attendance_status}\n"
            "تأخير: {late_minutes} د — إضافي: {overtime_hours} س — انصراف مبكر: {early_leave_minutes} د"
        ),
    )
    t_pay = _upsert_template(
        db,
        name="صرف راتب",
        event_key=HR_PAYROLL_PAID,
        recipient_type="employee",
        message_type="interactive",
        body=(
            "مرحباً {employee_name}،\n"
            "تم صرف راتب شهر {period_label}.\n"
            "الإجمالي: {gross_pay} — الخصومات: {deductions} — السلف: {advances}\n"
            "صافي المستلم: {net_pay}\n"
            "اضغط «تأكيد الاستلام» بعد استلام المبلغ."
        ),
    )
    if not (t_pay.buttons_json or "").strip():
        t_pay.buttons_json = '[{"text":"✓ تأكيد الاستلام","id":"payroll_confirm:{action_id}"}]'
    t_post = _upsert_template(
        db,
        name="اعتماد رواتب",
        event_key=HR_PAYROLL_POSTED,
        recipient_type="hr_manager",
        body=HR_PAYROLL_POSTED_BODY,
    )
    t_adv = _upsert_template(
        db,
        name="سلفة موظف",
        event_key=HR_ADVANCE_GIVEN,
        recipient_type="employee",
        body=(
            "مرحباً {employee_name}،\n"
            "تم صرف سلفة بقيمة {advance_amount}.\n"
            "{notes}"
        ),
    )
    t_ded = _upsert_template(
        db,
        name="خصم على موظف",
        event_key=HR_DEDUCTION_CREATED,
        recipient_type="employee",
        body=(
            "مرحباً {employee_name}،\n"
            "تم تسجيل خصم بقيمة {deduction_amount}.\n"
            "السبب: {deduction_note}"
        ),
    )
    t_ded_hr = _upsert_template(
        db,
        name="خصم (إدارة HR)",
        event_key=HR_DEDUCTION_CREATED,
        recipient_type="hr_manager",
        body="خصم على {employee_name}: {deduction_amount} — {deduction_note}",
    )
    _upsert_rule(db, event_key=HR_ATTENDANCE_CHECK_IN, recipient_type="employee", template_id=t_in.id, throttle_minutes=60)
    _upsert_rule(db, event_key=HR_ATTENDANCE_CHECK_OUT, recipient_type="employee", template_id=t_out.id, throttle_minutes=60)
    _upsert_rule(db, event_key=HR_PAYROLL_PAID, recipient_type="employee", template_id=t_pay.id, throttle_minutes=1440)
    _upsert_rule(db, event_key=HR_PAYROLL_POSTED, recipient_type="hr_manager", template_id=t_post.id)
    _upsert_rule(db, event_key=HR_ADVANCE_GIVEN, recipient_type="employee", template_id=t_adv.id, throttle_minutes=1440)
    _upsert_rule(db, event_key=HR_DEDUCTION_CREATED, recipient_type="employee", template_id=t_ded.id, throttle_minutes=1440)
    _upsert_rule(db, event_key=HR_DEDUCTION_CREATED, recipient_type="hr_manager", template_id=t_ded_hr.id, throttle_minutes=60)
    _import_legacy_messaging_templates(db)


def _seed_phase2(db: Session) -> None:
    t_confirmed = _upsert_template(
        db,
        name="تأكيد الطلب",
        event_key=ORDER_CONFIRMED,
        recipient_type="customer",
        body=(
            "مرحباً {customer_name}،\n"
            "تم تأكيد طلبك #{order_id} في {store_name}.\n"
            "الإجمالي: {total}"
        ),
    )
    t_kitchen = _upsert_template(
        db,
        name="إرسال للمطبخ",
        event_key=ORDER_SENT_TO_KITCHEN,
        recipient_type="customer",
        body="طلبك #{order_id} في {store_name} — تم إرساله للمطبخ للتحضير.",
    )
    t_driver = _upsert_template(
        db,
        name="تعيين سائق",
        event_key=ORDER_DRIVER_ASSIGNED,
        recipient_type="driver",
        body=(
            "🚗 طلب توصيل #{order_id}\n"
            "العميل: {customer_name} — {phone}\n"
            "الإجمالي: {total}"
        ),
    )
    t_out = _upsert_template(
        db,
        name="خرج للتوصيل",
        event_key=ORDER_OUT_FOR_DELIVERY,
        recipient_type="customer",
        body=(
            "مرحباً {customer_name}،\n"
            "طلبك #{order_id} خرج للتوصيل مع {driver_name}.\n"
            "{store_name}"
        ),
    )
    t_redeem = _upsert_template(
        db,
        name="خصم نقاط",
        event_key=LOYALTY_POINTS_REDEEMED,
        recipient_type="customer",
        body=LOYALTY_REDEEM_TEMPLATE_BODY,
    )
    t_cust = _upsert_template(
        db,
        name="عميل جديد",
        event_key=CUSTOMER_CREATED,
        recipient_type="customer",
        body="مرحباً {customer_name} في {store_name}! سعداء بانضمامك.",
    )
    t_shift = _upsert_template(
        db,
        name="إغلاق جلسة",
        event_key=POS_SHIFT_CLOSED,
        recipient_type="admin",
        body="تم إغلاق جلسة #{shift_id} — الكاشير: {cashier_name}",
    )
    t_short = _upsert_template(
        db,
        name="عجز جلسة",
        event_key=POS_CASH_SHORTAGE,
        recipient_type="admin",
        body="⚠️ عجز جلسة #{shift_id}: {shortage} — {cashier_name}",
    )
    t_void_line = _upsert_template(
        db,
        name="طلب مسح صنف",
        event_key=POS_ITEM_VOID_REQUESTED,
        recipient_type="supervisor",
        message_type="interactive",
        body=(
            "⚠️ طلب مسح صنف — فاتورة #{order_id}\n"
            "الكاشير: {cashier_name}\n"
            "السبب: {reason}"
        ),
    )
    if not (t_void_line.buttons_json or "").strip():
        t_void_line.buttons_json = (
            '[{"text":"✓ موافقة","id":"pos_approve:{action_id}"},'
            '{"text":"✗ رفض","id":"pos_reject:{action_id}"}]'
        )
    t_void_sale = _upsert_template(
        db,
        name="طلب إلغاء فاتورة",
        event_key=POS_INVOICE_CANCEL_REQUESTED,
        recipient_type="supervisor",
        message_type="interactive",
        body=(
            "⚠️ طلب إلغاء فاتورة #{order_id}\n"
            "الكاشير: {cashier_name}\n"
            "السبب: {reason}"
        ),
    )
    if not (t_void_sale.buttons_json or "").strip():
        t_void_sale.buttons_json = (
            '[{"text":"✓ موافقة","id":"pos_approve:{action_id}"},'
            '{"text":"✗ رفض","id":"pos_reject:{action_id}"}]'
        )
    _upsert_rule(db, event_key=ORDER_CONFIRMED, recipient_type="customer", template_id=t_confirmed.id)
    _upsert_rule(db, event_key=ORDER_SENT_TO_KITCHEN, recipient_type="customer", template_id=t_kitchen.id)
    _upsert_rule(db, event_key=ORDER_DRIVER_ASSIGNED, recipient_type="driver", template_id=t_driver.id)
    _upsert_rule(
        db,
        event_key=ORDER_OUT_FOR_DELIVERY,
        recipient_type="customer",
        template_id=t_out.id,
        condition_json='{"order_type": "DELIVERY"}',
    )
    _upsert_rule(db, event_key=LOYALTY_POINTS_REDEEMED, recipient_type="customer", template_id=t_redeem.id)
    _upsert_rule(db, event_key=CUSTOMER_CREATED, recipient_type="customer", template_id=t_cust.id)
    _upsert_rule(db, event_key=POS_SHIFT_CLOSED, recipient_type="admin", template_id=t_shift.id)
    _upsert_rule(db, event_key=POS_CASH_SHORTAGE, recipient_type="admin", template_id=t_short.id)
    _upsert_rule(db, event_key=POS_ITEM_VOID_REQUESTED, recipient_type="supervisor", template_id=t_void_line.id)
    _upsert_rule(db, event_key=POS_INVOICE_CANCEL_REQUESTED, recipient_type="supervisor", template_id=t_void_sale.id)


def _seed_phase3(db: Session) -> None:
    t_out = _upsert_template(
        db,
        name="نفاد مخزون",
        event_key=INVENTORY_OUT_OF_STOCK,
        recipient_type="inventory_manager",
        body=(
            "🔴 نفاد مخزون — {store_name}\n"
            "الصنف: {product_name}\n"
            "الرصيد: {balance} {unit}"
        ),
    )
    t_neg = _upsert_template(
        db,
        name="مخزون سالب",
        event_key=INVENTORY_NEGATIVE_STOCK,
        recipient_type="inventory_manager",
        body=(
            "⛔ مخزون سالب — {store_name}\n"
            "الصنف: {product_name}\n"
            "الرصيد: {balance} {unit}"
        ),
    )
    t_repl = _upsert_template(
        db,
        name="تعبئة مخزون",
        event_key=INVENTORY_STOCK_REPLENISHED,
        recipient_type="inventory_manager",
        body="✅ {product_name} — الرصيد {balance} {unit} (فوق حد إعادة الطلب {reorder_level})",
    )
    t_need = _upsert_template(
        db,
        name="ملخص حاجة شراء",
        event_key=INVENTORY_PURCHASE_NEEDED,
        recipient_type="inventory_manager",
        body=INVENTORY_PURCHASE_NEEDED_BODY,
    )
    t_purch = _upsert_template(
        db,
        name="استلام شراء",
        event_key=INVENTORY_PURCHASE_RECEIVED,
        recipient_type="inventory_manager",
        body=INVENTORY_PURCHASE_RECEIVED_BODY,
    )
    t_adj = _upsert_template(
        db,
        name="تسوية مخزون",
        event_key=INVENTORY_STOCK_ADJUSTMENT_CREATED,
        recipient_type="admin",
        body=(
            "تسوية مخزون: {product_name}\n"
            "الكمية: {movement_qty} {unit}\n"
            "{reason}"
        ),
    )
    t_waste = _upsert_template(
        db,
        name="هالك مخزون",
        event_key=INVENTORY_WASTE_RECORDED,
        recipient_type="inventory_manager",
        body="هدر/هالك: {product_name} — {movement_qty} {unit}\n{reason}",
    )
    t_exp = _upsert_template(
        db,
        name="قرب انتهاء صلاحية",
        event_key=INVENTORY_EXPIRY_WARNING,
        recipient_type="inventory_manager",
        body=(
            "⚠️ صلاحية: {product_name} — دفعة {lot_code}\n"
            "ينتهي خلال {days_left} يوم — الكمية {qty} {unit}"
        ),
    )
    t_expired = _upsert_template(
        db,
        name="صلاحية منتهية",
        event_key=INVENTORY_EXPIRED_ITEM,
        recipient_type="admin",
        body=(
            "⛔ منتهي: {product_name} — دفعة {lot_code}\n"
            "انتهى {expiry_date} — متبقي {qty} {unit}"
        ),
    )
    t_xfer = _upsert_template(
        db,
        name="استلام تحويل",
        event_key=INVENTORY_TRANSFER_RECEIVED,
        recipient_type="inventory_manager",
        body="استلام تحويل: {product_name} +{movement_qty} {unit}",
    )
    t_unusual = _upsert_template(
        db,
        name="حركة غير عادية",
        event_key=INVENTORY_UNUSUAL_STOCK_MOVEMENT,
        recipient_type="admin",
        body=(
            "⚠️ حركة مخزون كبيرة: {product_name}\n"
            "Δ {movement_qty} — الرصيد {balance} {unit}\n"
            "{note}"
        ),
    )
    t_recipe = _upsert_template(
        db,
        name="تغيّر تكلفة تركيبة",
        event_key=INVENTORY_RECIPE_COST_CHANGED,
        recipient_type="admin",
        body=(
            "تكلفة {product_name}: {old_cost} → {new_cost} د.ل"
        ),
    )
    t_bom = _upsert_template(
        db,
        name="مكوّن بدون تكلفة",
        event_key=INVENTORY_BOM_MISSING_COST,
        recipient_type="inventory_manager",
        body=INVENTORY_BOM_MISSING_BODY,
    )
    _upsert_rule(db, event_key=INVENTORY_OUT_OF_STOCK, recipient_type="inventory_manager", template_id=t_out.id, throttle_minutes=60)
    _upsert_rule(db, event_key=INVENTORY_OUT_OF_STOCK, recipient_type="admin", template_id=t_out.id, throttle_minutes=60)
    _upsert_rule(db, event_key=INVENTORY_NEGATIVE_STOCK, recipient_type="inventory_manager", template_id=t_neg.id, throttle_minutes=30)
    _upsert_rule(db, event_key=INVENTORY_NEGATIVE_STOCK, recipient_type="admin", template_id=t_neg.id, throttle_minutes=30)
    _upsert_rule(db, event_key=INVENTORY_STOCK_REPLENISHED, recipient_type="inventory_manager", template_id=t_repl.id, throttle_minutes=360)
    _upsert_rule(db, event_key=INVENTORY_PURCHASE_NEEDED, recipient_type="inventory_manager", template_id=t_need.id, throttle_minutes=1440)
    _upsert_rule(db, event_key=INVENTORY_PURCHASE_RECEIVED, recipient_type="inventory_manager", template_id=t_purch.id)
    _upsert_rule(db, event_key=INVENTORY_PURCHASE_RECEIVED, recipient_type="admin", template_id=t_purch.id)
    _upsert_rule(db, event_key=INVENTORY_STOCK_ADJUSTMENT_CREATED, recipient_type="admin", template_id=t_adj.id)
    _upsert_rule(db, event_key=INVENTORY_WASTE_RECORDED, recipient_type="inventory_manager", template_id=t_waste.id, throttle_minutes=60)
    _upsert_rule(db, event_key=INVENTORY_EXPIRY_WARNING, recipient_type="inventory_manager", template_id=t_exp.id, throttle_minutes=1440)
    _upsert_rule(db, event_key=INVENTORY_EXPIRED_ITEM, recipient_type="admin", template_id=t_expired.id, throttle_minutes=1440)
    _upsert_rule(db, event_key=INVENTORY_TRANSFER_RECEIVED, recipient_type="inventory_manager", template_id=t_xfer.id)
    _upsert_rule(db, event_key=INVENTORY_UNUSUAL_STOCK_MOVEMENT, recipient_type="admin", template_id=t_unusual.id, throttle_minutes=120)
    _upsert_rule(db, event_key=INVENTORY_RECIPE_COST_CHANGED, recipient_type="admin", template_id=t_recipe.id, throttle_minutes=360)
    _upsert_rule(db, event_key=INVENTORY_BOM_MISSING_COST, recipient_type="inventory_manager", template_id=t_bom.id, throttle_minutes=1440)
    _upsert_rule(
        db,
        event_key=INVENTORY_LOW_STOCK,
        recipient_type="inventory_manager",
        template_id=_upsert_template(
            db,
            name="مخزون منخفض (مدير)",
            event_key=INVENTORY_LOW_STOCK,
            recipient_type="inventory_manager",
            body=(
                "⚠️ {product_name} — الرصيد {balance} {unit}\n"
                "حد إعادة الطلب: {reorder_level}"
            ),
        ).id,
        throttle_minutes=360,
    )


def _seed_phase4(db: Session) -> None:
    t_ref_link = _upsert_template(
        db,
        name="كود إحالة",
        event_key=REFERRAL_LINK_CREATED,
        recipient_type="customer",
        body=REFERRAL_LINK_TEMPLATE_BODY,
    )
    t_ref_share = _upsert_template(
        db,
        name="مشاركة منتج",
        event_key=REFERRAL_PRODUCT_SHARED,
        recipient_type="customer",
        body=REFERRAL_PRODUCT_SHARE_BODY,
    )
    t_ref_order = _upsert_template(
        db,
        name="إحالة ناجحة",
        event_key=REFERRAL_FIRST_ORDER,
        recipient_type="customer",
        body=REFERRAL_FIRST_ORDER_CUSTOMER_BODY,
    )
    t_ref_admin = _upsert_template(
        db,
        name="إحالة (إدارة)",
        event_key=REFERRAL_FIRST_ORDER,
        recipient_type="admin",
        body=REFERRAL_FIRST_ORDER_ADMIN_BODY,
    )
    t_ref_referrer = _upsert_template(
        db,
        name="مكافأة المحيل",
        event_key=REFERRAL_REFERRER_REWARDED,
        recipient_type="referrer",
        body=REFERRAL_REFERRER_REWARDED_BODY,
    )
    t_loy_acct = _upsert_template(
        db,
        name="حساب ولاء",
        event_key=LOYALTY_ACCOUNT_CREATED,
        recipient_type="customer",
        body=(
            "مرحباً {customer_name} في {store_name}!\n"
            "حساب الولاء جاهز — رصيدك {balance} نقطة."
        ),
    )
    t_cancel = _upsert_template(
        db,
        name="إلغاء طلب",
        event_key=ORDER_CANCELLED,
        recipient_type="customer",
        body="تم إلغاء طلبك #{order_id} في {store_name}. {reason}",
    )
    t_k_t = _upsert_template(
        db,
        name="تذكرة مطبخ",
        event_key=KITCHEN_TICKET_CREATED,
        recipient_type="admin",
        body=KITCHEN_TICKET_BODY,
    )
    t_k_c = _upsert_template(
        db,
        name="إلغاء مطبخ",
        event_key=KITCHEN_ITEM_CANCELLED,
        recipient_type="admin",
        body=KITCHEN_CANCEL_BODY,
    )
    t_shift_o = _upsert_template(
        db,
        name="فتح جلسة",
        event_key=POS_SHIFT_OPENED,
        recipient_type="admin",
        body="فُتحت جلسة #{shift_id} — الكاشير: {cashier_name}",
    )
    t_over = _upsert_template(
        db,
        name="فائض جلسة",
        event_key=POS_CASH_OVERAGE,
        recipient_type="admin",
        body=POS_OVERAGE_BODY,
    )
    _upsert_rule(db, event_key=REFERRAL_LINK_CREATED, recipient_type="customer", template_id=t_ref_link.id, throttle_minutes=1440)
    _upsert_rule(db, event_key=REFERRAL_PRODUCT_SHARED, recipient_type="customer", template_id=t_ref_share.id)
    _upsert_rule(db, event_key=REFERRAL_FIRST_ORDER, recipient_type="customer", template_id=t_ref_order.id)
    _upsert_rule(db, event_key=REFERRAL_FIRST_ORDER, recipient_type="admin", template_id=t_ref_admin.id)
    _upsert_rule(db, event_key=REFERRAL_REFERRER_REWARDED, recipient_type="referrer", template_id=t_ref_referrer.id)
    _upsert_rule(db, event_key=LOYALTY_ACCOUNT_CREATED, recipient_type="customer", template_id=t_loy_acct.id)
    _upsert_rule(db, event_key=ORDER_CANCELLED, recipient_type="customer", template_id=t_cancel.id)
    _upsert_rule(db, event_key=KITCHEN_TICKET_CREATED, recipient_type="admin", template_id=t_k_t.id, throttle_minutes=60)
    _upsert_rule(db, event_key=KITCHEN_ITEM_CANCELLED, recipient_type="admin", template_id=t_k_c.id)
    _upsert_rule(db, event_key=POS_SHIFT_OPENED, recipient_type="admin", template_id=t_shift_o.id)
    _upsert_rule(db, event_key=POS_CASH_OVERAGE, recipient_type="admin", template_id=t_over.id)
