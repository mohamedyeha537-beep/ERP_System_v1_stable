"""قوالب وقواعد افتراضية لبوت المراسلات."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.messaging.events import (
    CUSTOMER_FIRST_LINKED,
    LOYALTY_POINTS_ADJUSTED,
    LOYALTY_POINTS_EARNED,
    LOYALTY_POINTS_REDEEMED,
    PRODUCT_EXPIRY,
    STOCK_LOW,
)
from modules.messaging.models import (
    MessageAudience,
    MessageRule,
    MessageTemplate,
)
from modules.messaging.event_registry import ensure_system_events_and_hooks


def _upsert_template(
    db: Session, *, code: str, name: str, body: str
) -> MessageTemplate:
    row = db.scalar(select(MessageTemplate).where(MessageTemplate.code == code))
    if row is None:
        row = MessageTemplate(code=code, name=name, body_text=body, is_active=True)
        db.add(row)
        db.flush()
        return row
    row.name = name
    if not (row.body_text or "").strip():
        row.body_text = body
    row.is_active = True
    return row


def _upsert_rule(
    db: Session,
    *,
    name: str,
    event_type: str,
    template_id: int,
    channels: str = "whatsapp",
    audience: str = MessageAudience.CUSTOMER.value,
    priority: int = 100,
) -> None:
    existing = db.scalar(
        select(MessageRule).where(
            MessageRule.event_type == event_type,
            MessageRule.name == name,
        )
    )
    if existing is not None:
        if not existing.is_active:
            existing.is_active = True
        return
    db.add(
        MessageRule(
            name=name,
            event_type=event_type,
            template_id=template_id,
            channels=channels,
            audience=audience,
            conditions_json="{}",
            priority=priority,
            is_active=True,
        )
    )


def _is_loyalty_redeem_template_current(body: str | None) -> bool:
    b = (body or "").strip()
    return "🎉" in b and "تم استخدام" in b and "💳 رصيدك الحالي" in b


def _patch_loyalty_redeem_template(db: Session) -> None:
    """ترقية قالب خصم النقاط للصيغة الجديدة."""
    from modules.notifications.seed import LOYALTY_REDEEM_TEMPLATE_BODY

    row = db.scalar(select(MessageTemplate).where(MessageTemplate.code == "loyalty.redeemed"))
    if row is None:
        return
    if _is_loyalty_redeem_template_current(row.body_text):
        return
    row.body_text = LOYALTY_REDEEM_TEMPLATE_BODY


def ensure_messaging_defaults(db: Session) -> None:
    ensure_system_events_and_hooks(db)
    t_earn = _upsert_template(
        db,
        code="loyalty.earned",
        name="اكتساب نقاط",
        body=(
            "مرحباً {customer_name}،\n"
            "اكتسبت {points} نقطة من {store_name} (قيمتها {points_value} د.ل).\n"
            "رصيدك الآن: {balance} نقطة = {balance_value} د.ل.\n"
            "فاتورة #{sale_id}"
        ),
    )
    t_redeem = _upsert_template(
        db,
        code="loyalty.redeemed",
        name="خصم نقاط",
        body=(
            "🎉 مرحباً {customer_name}،\n\n"
            "تم استخدام {points} نقطة من رصيدك كخصم بقيمة {points_value} د.ل على فاتورتك.\n\n"
            "{earn_line}"
            "💳 رصيدك الحالي: {balance} نقطة ({balance_value} د.ل)\n\n"
            "🧾 الفاتورة: #{sale_id}\n\n"
            "شكراً لزيارتك، ونتطلع لخدمتك مجدداً في {store_name}. ❤️"
        ),
    )
    t_adjust = _upsert_template(
        db,
        code="loyalty.adjusted",
        name="تعديل نقاط",
        body=(
            "مرحباً {customer_name}،\n"
            "تم تعديل رصيد نقاطك في {store_name} بمقدار {points}.\n"
            "الرصيد الحالي: {balance}.\n"
            "{message}"
        ),
    )
    t_stock = _upsert_template(
        db,
        code="stock.low",
        name="نقص مخزون",
        body="{message}",
    )
    t_expiry = _upsert_template(
        db,
        code="product.expiry",
        name="قرب انتهاء صلاحية",
        body="{message}",
    )
    t_consent = _upsert_template(
        db,
        code="consent.welcome",
        name="ترحيب + موافقة",
        body=(
            "مرحباً {customer_name}،\n"
            "شكراً لتسجيلك في {store_name}.\n"
            "ستصلك رسائل العروض ونقاط الولاء على القناة التي اخترتها.\n"
            "لإلغاء الاشتراك تواصل معنا."
        ),
    )
    t_campaign = _upsert_template(
        db,
        code="campaign.generic",
        name="حملة عامة",
        body=(
            "مرحباً {customer_name}،\n"
            "{message}\n"
            "— {store_name}"
        ),
    )
    db.flush()

    _upsert_rule(
        db,
        name="إشعار اكتساب نقاط",
        event_type=LOYALTY_POINTS_EARNED,
        template_id=t_earn.id,
    )
    _upsert_rule(
        db,
        name="إشعار خصم نقاط",
        event_type=LOYALTY_POINTS_REDEEMED,
        template_id=t_redeem.id,
    )
    _upsert_rule(
        db,
        name="إشعار تعديل نقاط",
        event_type=LOYALTY_POINTS_ADJUSTED,
        template_id=t_adjust.id,
    )
    _upsert_rule(
        db,
        name="تنبيه نقص مخزون للإدارة",
        event_type=STOCK_LOW,
        template_id=t_stock.id,
        audience=MessageAudience.ADMIN.value,
        channels="whatsapp",
        priority=10,
    )
    _upsert_rule(
        db,
        name="تنبيه قرب انتهاء صلاحية للإدارة",
        event_type=PRODUCT_EXPIRY,
        template_id=t_expiry.id,
        audience=MessageAudience.ADMIN.value,
        channels="whatsapp",
        priority=10,
    )
    _upsert_rule(
        db,
        name="ترحيب عميل موافق",
        event_type=CUSTOMER_FIRST_LINKED,
        template_id=t_consent.id,
    )
    _normalize_admin_rules(db)
    _patch_loyalty_redeem_template(db)


def _normalize_admin_rules(db: Session) -> None:
    """تنبيهات الإدارة — WhatsApp فقط."""
    rows = list(
        db.scalars(
            select(MessageRule).where(MessageRule.audience == MessageAudience.ADMIN.value)
        ).all()
    )
    for row in rows:
        if row.channels != "whatsapp":
            row.channels = "whatsapp"
