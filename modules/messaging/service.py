from __future__ import annotations

import json
import logging
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.customers.models import Customer
from modules.messaging.consent import customer_can_receive, update_consent
from modules.messaging.events import (
    CAMPAIGN_BROADCAST,
    CAMPAIGN_MANUAL,
    CUSTOMER_FIRST_LINKED,
    LOYALTY_POINTS_ADJUSTED,
    LOYALTY_POINTS_EARNED,
    LOYALTY_POINTS_REDEEMED,
    PRODUCT_EXPIRY,
    STOCK_LOW,
)
from modules.messaging.models import (
    MessageAudience,
    MessageCampaign,
    MessageChannel,
    MessageRule,
)
from modules.messaging.outbox import (
    _match_conditions,
    _parse_channels,
    enqueue_message,
    process_outbox_batch,
    resolve_channel_for_customer,
    send_outbox_ids_now,
    whatsapp_provider,
)
from modules.messaging.event_registry import resolve_hook_event_types
from modules.messaging.templates_render import render_template
from modules.settings.service import get_bool, get_setting

LOG = logging.getLogger("messaging")


def messaging_enabled(db: Session) -> bool:
    return get_bool(db, "messaging_enabled", False)


def _template_vars(db: Session, payload: dict) -> dict:
    store = get_setting(db, "store_name", "نقطة البيع")
    vars_out = {
        "store_name": store,
        "customer_name": payload.get("customer_name") or payload.get("name") or "عميلنا",
        "phone": payload.get("phone") or "",
        "points": payload.get("points") or "0",
        "points_value": payload.get("points_value") or "0",
        "earned_points": payload.get("earned_points") or "0",
        "earned_points_value": payload.get("earned_points_value") or "0",
        "earn_line": payload.get("earn_line") or "",
        "balance": payload.get("balance") or "0",
        "balance_value": payload.get("balance_value") or "0",
        "sale_id": payload.get("sale_id") or payload.get("order_id") or "",
        "order_id": payload.get("order_id") or payload.get("sale_id") or "",
        "message": payload.get("message") or "",
        "product_name": payload.get("product_name") or "",
        "campaign_name": payload.get("campaign_name") or "",
        "referral_code": payload.get("referral_code") or "",
        "buyer_referral_code": payload.get("buyer_referral_code") or "",
        "referral_code_line": payload.get("referral_code_line") or "",
        "referral_share_block": payload.get("referral_share_block")
        or payload.get("referral_code_line")
        or "",
        "buyer_points": payload.get("buyer_points") or "",
        "buyer_points_value": payload.get("buyer_points_value") or payload.get("points_value") or "0",
        "referrer_points": payload.get("referrer_points") or "",
        "referrer_name": payload.get("referrer_name") or "",
    }
    return vars_out


def _enqueue_for_rule(
    db: Session,
    rule: MessageRule,
    payload: dict,
) -> list[int]:
    if not rule.is_active or rule.template is None:
        return []
    if not _match_conditions(rule.conditions_json, payload):
        return []
    channels = _parse_channels(rule.channels)
    body = render_template(rule.template.body_text, _template_vars(db, payload))
    ids: list[int] = []

    if rule.audience == MessageAudience.ADMIN.value:
        phone = (get_setting(db, "messaging_admin_phone") or "").strip()
        if not phone:
            return []
        # تنبيهات الإدارة → WhatsApp فقط (TextMeBot) — لا Telegram برقم الهاتف
        ch = MessageChannel.WHATSAPP.value
        from modules.messaging.categories import message_kind

        row = enqueue_message(
            db,
            body=body,
            channel=ch,
            phone=phone,
            customer_id=None,
            event_type=rule.event_type,
            rule_id=rule.id,
            meta={"audience": "admin", "kind": message_kind(rule.event_type)},
        )
        ids.append(row.id)
        return ids

    customer_id = payload.get("customer_id")
    if not customer_id:
        return []
    if rule.event_type != CUSTOMER_FIRST_LINKED and not customer_can_receive(
        db, int(customer_id)
    ):
        return []
    customer = db.get(Customer, int(customer_id))
    if customer is None:
        return []
    channel = resolve_channel_for_customer(db, int(customer_id), channels)
    if not channel:
        channel = MessageChannel.WHATSAPP.value
    prof_meta = {}
    from modules.messaging.consent import get_profile

    prof = get_profile(db, int(customer_id))
    if prof and prof.telegram_chat_id:
        prof_meta["telegram_chat_id"] = prof.telegram_chat_id
    from modules.messaging.categories import message_kind

    prof_meta["kind"] = message_kind(rule.event_type)
    row = enqueue_message(
        db,
        body=body,
        channel=channel,
        phone=customer.phone,
        customer_id=customer.id,
        event_type=rule.event_type,
        rule_id=rule.id,
        meta=prof_meta,
    )
    ids.append(row.id)
    return ids


def emit_system_hook(db: Session, hook_code: str, payload: dict) -> list[int]:
    """تشغيل نقطة ربط → إطلاق الأحداث المرتبطة → إرسال."""
    if not messaging_enabled(db):
        return []
    out: list[int] = []
    for event_type in resolve_hook_event_types(db, hook_code):
        out.extend(emit_event(db, event_type, payload))
    if out:
        send_outbox_ids_now(db, out)
    return out


def emit_system_hook_background(hook_code: str, payload: dict) -> None:
    from infra.background import run_in_background, with_db

    def _run() -> None:
        with with_db() as db:
            emit_system_hook(db, hook_code, payload)
            db.commit()

    run_in_background(_run, name=f"messaging-hook-{hook_code}")


def _emit_via_notification_engine(
    db: Session, event_type: str, payload: dict
) -> list[int] | None:
    """يُفوّض أحداث المرحلة 1 لمحرك الإشعارات — None = استمر بالمسار القديم."""
    from modules.notifications.events import INVENTORY_LOW_STOCK, WIRED_EVENT_KEYS
    from modules.notifications.service import NotificationService

    mapping = {
        "loyalty.points_earned": "loyalty.points_earned",
        "loyalty.points_redeemed": "loyalty.points_redeemed",
        "stock.low": INVENTORY_LOW_STOCK,
    }
    key = mapping.get(event_type, event_type)
    if key not in WIRED_EVENT_KEYS:
        return None
    if not NotificationService.enabled(db):
        return []
    sid = payload.get("sale_id") or payload.get("order_id")
    try:
        sid_int = int(sid) if sid else None
    except (TypeError, ValueError):
        sid_int = None
    NotificationService.emit_event(
        db,
        event_key=key,
        source_type="messaging_legacy",
        source_id=sid_int,
        payload=payload,
    )
    return [1]


def emit_event(db: Session, event_type: str, payload: dict) -> list[int]:
    """مطابقة القواعد وإنشاء رسائل في الطابور."""
    bridged = _emit_via_notification_engine(db, event_type, payload)
    if bridged is not None:
        return bridged
    if not messaging_enabled(db):
        return []
    rules = list(
        db.scalars(
            select(MessageRule)
            .where(
                MessageRule.event_type == event_type,
                MessageRule.is_active.is_(True),
            )
            .order_by(MessageRule.priority.asc(), MessageRule.id.asc())
        ).all()
    )
    out: list[int] = []
    for rule in rules:
        if rule.template is None:
            db.refresh(rule, ["template"])
        try:
            out.extend(_enqueue_for_rule(db, rule, payload))
        except Exception as exc:  # noqa: BLE001
            LOG.warning("rule %s enqueue failed: %s", rule.id, exc)
    return out


def emit_event_and_send(db: Session, event_type: str, payload: dict) -> list[int]:
    ids = emit_event(db, event_type, payload)
    if ids:
        send_outbox_ids_now(db, ids)
    return ids


def emit_event_background(event_type: str, payload: dict) -> None:
    from infra.background import run_in_background, with_db

    def _run() -> None:
        with with_db() as db:
            emit_event_and_send(db, event_type, payload)
            db.commit()

    run_in_background(_run, name=f"messaging-{event_type}")


def notify_loyalty_earned(
    db: Session,
    *,
    customer: Customer,
    sale_id: int,
    points: Decimal,
) -> None:
    from modules.notifications.hooks import emit_loyalty_points_earned

    emit_loyalty_points_earned(
        db, customer=customer, sale_id=sale_id, points=points
    )


def notify_loyalty_redeemed(
    db: Session,
    *,
    customer: Customer,
    sale_id: int,
    points: Decimal,
    earned: Decimal = Decimal("0"),
) -> None:
    from modules.notifications.hooks import emit_loyalty_points_redeemed

    emit_loyalty_points_redeemed(
        db,
        customer=customer,
        sale_id=sale_id,
        points=points,
        earned=earned,
    )


def notify_loyalty_checkout(
    db: Session,
    *,
    customer: Customer,
    sale_id: int,
    redeemed: Decimal,
    earned: Decimal,
) -> None:
    """إشعار واحد بعد الدفع: خصم و/أو كسب بنقاط مع الرصيد النهائي."""
    redeemed = Decimal(str(redeemed or 0)).quantize(Decimal("0.001"))
    earned = Decimal(str(earned or 0)).quantize(Decimal("0.001"))
    if redeemed > 0:
        notify_loyalty_redeemed(
            db,
            customer=customer,
            sale_id=sale_id,
            points=redeemed,
            earned=earned,
        )
    elif earned > 0:
        notify_loyalty_earned(
            db, customer=customer, sale_id=sale_id, points=earned
        )


def notify_loyalty_adjusted(
    db: Session,
    *,
    customer: Customer,
    delta: Decimal,
    note: str | None,
) -> None:
    payload = {
        "customer_id": customer.id,
        "customer_name": customer.name or "",
        "phone": customer.phone,
        "points": f"{delta:.0f}",
        "balance": f"{Decimal(customer.points_balance or 0):.0f}",
        "message": note or "",
    }
    emit_system_hook_background("hook.loyalty.points_adjusted", payload)


def send_stock_low_now(db: Session, message: str) -> list[int]:
    """إرسال تنبيه نقص مخزون فوراً (legacy — يُفوّض للمحرك المركزي)."""
    payload = {"message": message}
    bridged = _emit_via_notification_engine(db, "stock.low", payload)
    if bridged is not None:
        return bridged
    return emit_system_hook(db, "hook.stock.low", payload)


def notify_stock_low(db: Session, message: str) -> None:
    payload = {"message": message}
    emit_system_hook_background("hook.stock.low", payload)


def send_product_expiry_now(db: Session, message: str) -> list[int]:
    return emit_system_hook(db, "hook.product.expiry", {"message": message})


def notify_product_expiry(db: Session, message: str) -> None:
    payload = {"message": message}
    emit_system_hook_background("hook.product.expiry", payload)


def grant_shop_share_consent(db: Session, customer_id: int) -> bool:
    """موافقة ضمنية عند مشاركة منتج — يُرجع True إذا كانت أول موافقة."""
    from modules.messaging.consent import get_profile, update_consent
    from modules.messaging.models import MessageChannel

    prof = get_profile(db, customer_id)
    was_opted_in = prof is not None and prof.opt_in
    update_consent(
        db,
        customer_id=customer_id,
        opt_in=True,
        preferred_channel=MessageChannel.WHATSAPP.value,
        consent_source="shop_share",
    )
    return not was_opted_in


def notify_customer_consent(
    db: Session,
    *,
    customer: Customer,
    consent_source: str = "checkout",
) -> None:
    payload = {
        "customer_id": customer.id,
        "customer_name": customer.name or "",
        "phone": customer.phone,
        "name": customer.name or "",
    }
    emit_system_hook_background("hook.customer.first_consent", payload)


def notify_shop_share_consent(
    db: Session,
    *,
    customer: Customer,
) -> None:
    payload = {
        "customer_id": customer.id,
        "customer_name": customer.name or "",
        "phone": customer.phone,
        "name": customer.name or "",
    }
    emit_system_hook_background("hook.shop.share_consent", payload)


def notify_referral_product_shared(
    db: Session,
    *,
    customer: Customer,
    product_id: int,
    product_name: str,
    referral_code: str,
    share_url: str,
    share_message: str,
) -> None:
    from modules.notifications.marketing_hooks import notify_referral_product_shared_background

    notify_referral_product_shared_background(
        customer_id=int(customer.id),
        customer_name=customer.name or "",
        phone=customer.phone or "",
        product_id=int(product_id),
        product_name=product_name,
        referral_code=referral_code,
        share_url=share_url,
        share_message=share_message,
    )


def save_checkout_consent(
    db: Session,
    *,
    customer_id: int,
    opt_in: bool,
    preferred_channel: str,
) -> None:
    update_consent(
        db,
        customer_id=customer_id,
        opt_in=opt_in,
        preferred_channel=preferred_channel,
        consent_source="checkout",
    )


def run_campaign_now(db: Session, campaign_id: int) -> int:
    camp = db.get(MessageCampaign, campaign_id)
    if camp is None or camp.template is None:
        return 0
    db.refresh(camp, ["template"])
    seg: dict = {}
    try:
        import json as _json

        seg = _json.loads(camp.segment_json or "{}")
    except Exception:
        pass
    segment = (seg.get("segment") or "opt_in").strip()
    if segment == "phone_list":
        lid = seg.get("phone_list_id")
        if not lid:
            return 0
        msg = (seg.get("message") or camp.name or "").strip()
        img = (seg.get("image_url") or "").strip()
        from modules.messaging.phone_list_service import enqueue_phone_list_messages

        count = enqueue_phone_list_messages(
            db,
            list_id=int(lid),
            message=msg or render_template(camp.template.body_text, _template_vars(db, {})),
            image_url=img,
            event_type="campaign.external",
            campaign_id=camp.id,
        )
        from datetime import datetime, timezone

        camp.last_sent_at = datetime.now(timezone.utc)
        return count

    customers = list(db.scalars(select(Customer).where(Customer.is_active.is_(True))).all())
    count = 0
    for c in customers:
        if segment == "opt_in" and not customer_can_receive(db, c.id):
            continue
        body = render_template(
            camp.template.body_text,
            _template_vars(
                db,
                {
                    "customer_id": c.id,
                    "customer_name": c.name or "",
                    "phone": c.phone,
                    "campaign_name": camp.name,
                },
            ),
        )
        ch = resolve_channel_for_customer(
            db, c.id, [MessageChannel.WHATSAPP.value, MessageChannel.TELEGRAM.value]
        )
        if not ch:
            continue
        enqueue_message(
            db,
            body=body,
            channel=ch,
            phone=c.phone,
            customer_id=c.id,
            event_type=CAMPAIGN_MANUAL,
            campaign_id=camp.id,
        )
        count += 1
    from datetime import datetime, timezone

    camp.last_sent_at = datetime.now(timezone.utc)
    return count


def process_due_campaigns(db: Session) -> int:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    camps = list(
        db.scalars(
            select(MessageCampaign).where(MessageCampaign.is_active.is_(True))
        ).all()
    )
    total = 0
    for camp in camps:
        if camp.starts_at and camp.starts_at > now:
            continue
        if camp.ends_at and camp.ends_at < now:
            camp.is_active = False
            continue
        if camp.last_sent_at and camp.starts_at and camp.last_sent_at >= camp.starts_at:
            continue
        total += run_campaign_now(db, camp.id)
    return total


def enqueue_broadcast(
    db: Session,
    *,
    message: str,
    image_url: str = "",
    segment: str = "opt_in",
    phone_list_id: int | None = None,
    event_type: str = "campaign.manual",
) -> int:
    """جدولة رسالة دعائية جماعية — لا تُخلط مع التنبيهات التلقائية."""
    if segment == "phone_list":
        if not phone_list_id:
            return 0
        from modules.messaging.phone_list_service import enqueue_phone_list_messages

        return enqueue_phone_list_messages(
            db,
            list_id=int(phone_list_id),
            message=message,
            image_url=image_url,
            event_type="campaign.external",
        )

    from modules.customers.models import Customer
    from modules.messaging.consent import customer_can_receive
    from modules.messaging.models import MessageChannel
    from modules.messaging.categories import message_kind

    store = get_setting(db, "store_name", "نقطة البيع")
    meta_base: dict = {"kind": message_kind(event_type)}
    img = (image_url or "").strip()
    if img:
        meta_base["image_url"] = img
    count = 0
    for c in db.scalars(select(Customer).where(Customer.is_active.is_(True))).all():
        if segment == "opt_in" and not customer_can_receive(db, c.id):
            continue
        body = f"مرحباً {c.name or 'عميلنا'}،\n{message.strip()}\n— {store}"
        ch = MessageChannel.WHATSAPP.value
        if segment == "opt_in":
            resolved = resolve_channel_for_customer(
                db, c.id, [MessageChannel.WHATSAPP.value, MessageChannel.TELEGRAM.value]
            )
            if not resolved:
                continue
            ch = resolved
        prof_meta = dict(meta_base)
        from modules.messaging.consent import get_profile

        prof = get_profile(db, c.id)
        if prof and prof.telegram_chat_id:
            prof_meta["telegram_chat_id"] = prof.telegram_chat_id
        enqueue_message(
            db,
            body=body,
            channel=ch,
            phone=c.phone,
            customer_id=c.id,
            event_type=event_type,
            meta=prof_meta,
        )
        count += 1
    return count


class MessagingError(Exception):
    pass


def send_message_to_customer(
    db: Session,
    *,
    customer_id: int,
    message: str,
    image_url: str = "",
    force: bool = False,
    user_id: int | None = None,
) -> int:
    """إرسال رسالة لعميل واحد — تدخل الطابور ثم تُرسل."""
    from modules.customers.models import Customer
    from modules.messaging.consent import customer_can_receive, get_profile
    from modules.messaging.models import MessageChannel

    if not messaging_enabled(db):
        raise MessagingError("بوت المراسلات غير مفعّل.")
    text = (message or "").strip()
    if not text and not (image_url or "").strip():
        raise MessagingError("نص الرسالة مطلوب.")
    customer = db.get(Customer, customer_id)
    if customer is None or not customer.is_active:
        raise MessagingError("العميل غير موجود أو غير نشط.")
    if not customer.phone:
        raise MessagingError("العميل بدون رقم هاتف.")
    if not force and not customer_can_receive(db, customer_id):
        raise MessagingError(
            "العميل لم يوافق على المراسلات — فعّل «إرسال رغم ذلك» أو اطلب موافقته عند الدفع."
        )
    store = get_setting(db, "store_name", "نقطة البيع")
    body = f"مرحباً {customer.name or 'عميلنا'}،\n{text}\n— {store}"
    meta: dict = {}
    img = (image_url or "").strip()
    if img:
        meta["image_url"] = img
    prof = get_profile(db, customer_id)
    ch = MessageChannel.WHATSAPP.value
    if prof and prof.opt_in:
        resolved = resolve_channel_for_customer(
            db, customer_id, [MessageChannel.WHATSAPP.value, MessageChannel.TELEGRAM.value]
        )
        if resolved:
            ch = resolved
    if prof and prof.telegram_chat_id:
        meta["telegram_chat_id"] = prof.telegram_chat_id
    from modules.messaging.categories import message_kind

    meta["kind"] = message_kind("campaign.manual")
    if user_id is not None:
        meta["sent_by_user_id"] = user_id
    row = enqueue_message(
        db,
        body=body,
        channel=ch,
        phone=customer.phone,
        customer_id=customer.id,
        event_type="campaign.manual",
        meta=meta,
    )
    send_outbox_ids_now(db, [row.id])
    from modules.messaging.inbox_service import record_outbound_from_legacy_send

    record_outbound_from_legacy_send(
        db,
        customer_id=customer.id,
        phone=customer.phone,
        body=body,
        user_id=user_id,
        outbox_id=row.id,
        channel=ch,
    )
    return row.id
