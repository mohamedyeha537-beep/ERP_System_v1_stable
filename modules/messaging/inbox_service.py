"""صندوق الوارد — محادثات واردة/صادرة مع العملاء."""
from __future__ import annotations

from datetime import datetime, timezone

from infra.sql_compat import order_by_nulls_last

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from modules.customers.service import get_by_phone_any, normalize_phone
from modules.messaging.consent import customer_can_receive, get_profile
from modules.messaging.models import (
    MessageChannel,
    MessageConversation,
    MessageConversationStatus,
    MessageDirection,
    MessageThreadItem,
)
from modules.messaging.outbox import enqueue_message, send_outbox_ids_now
from modules.messaging.service import MessagingError, messaging_enabled
from modules.settings.service import get_setting


def _preview(text: str, limit: int = 160) -> str:
    t = (text or "").strip().replace("\n", " ")
    if len(t) <= limit:
        return t
    return t[: limit - 1] + "…"


def _canonical_phone(db: Session, raw: str) -> str:
    s = (raw or "").strip()
    if s.startswith("webchat:"):
        return s
    cc = get_setting(db, "messaging_country_code", "218") or "218"
    from modules.messaging.phone_utils import normalize_whatsapp_phone

    wa = normalize_whatsapp_phone(raw, country_code=cc)
    if wa.startswith("+"):
        return normalize_phone(wa) or wa
    return normalize_phone(raw) or (raw or "").strip()


def find_customer_for_phone(db: Session, phone: str):
    return get_by_phone_any(db, phone)


def get_or_create_conversation(
    db: Session, *, phone: str, customer_id: int | None = None
) -> MessageConversation:
    canonical = _canonical_phone(db, phone)
    if not canonical:
        raise MessagingError("رقم الهاتف غير صالح.")
    conv = db.execute(
        select(MessageConversation).where(MessageConversation.phone == canonical)
    ).scalar_one_or_none()
    if conv is not None:
        if customer_id and conv.customer_id is None:
            conv.customer_id = customer_id
        return conv
    if customer_id is None:
        cust = find_customer_for_phone(db, canonical)
        if cust is not None:
            customer_id = cust.id
    conv = MessageConversation(
        phone=canonical,
        customer_id=customer_id,
        status=MessageConversationStatus.OPEN.value,
    )
    db.add(conv)
    db.flush()
    return conv


def record_inbound_message(
    db: Session,
    *,
    phone: str,
    body: str,
    channel: str = MessageChannel.WHATSAPP.value,
    external_id: str | None = None,
    sender_name: str | None = None,
) -> tuple[MessageConversation, MessageThreadItem]:
    text = (body or "").strip()
    if not text:
        raise MessagingError("نص الرسالة فارغ.")
    if external_id:
        dup = db.execute(
            select(MessageThreadItem.id).where(
                MessageThreadItem.external_id == external_id.strip()
            )
        ).scalar_one_or_none()
        if dup is not None:
            conv = get_or_create_conversation(db, phone=phone)
            existing = db.get(MessageThreadItem, dup)
            if existing:
                return conv, existing
    cust = find_customer_for_phone(db, _canonical_phone(db, phone))
    conv = get_or_create_conversation(
        db, phone=phone, customer_id=cust.id if cust else None
    )
    if conv.status == MessageConversationStatus.CLOSED.value:
        conv.status = MessageConversationStatus.OPEN.value
    now = datetime.now(timezone.utc)
    item = MessageThreadItem(
        conversation_id=conv.id,
        direction=MessageDirection.INBOUND.value,
        body=text,
        channel=(channel or MessageChannel.WHATSAPP.value)[:32],
        external_id=(external_id or "").strip() or None,
    )
    db.add(item)
    conv.last_message_at = now
    conv.last_preview = _preview(text)
    conv.unread_count = int(conv.unread_count or 0) + 1
    if sender_name and cust is None and conv.customer_id is None:
        conv.last_preview = _preview(f"{sender_name}: {text}")
    db.flush()
    return conv, item


def list_conversations(
    db: Session,
    *,
    status: str | None = None,
    assigned_to_user_id: int | None = None,
    only_unread: bool = False,
    limit: int = 80,
) -> list[MessageConversation]:
    stmt = (
        select(MessageConversation)
        .options(
            selectinload(MessageConversation.customer),
            selectinload(MessageConversation.assigned_user),
        )
        .order_by(
            *order_by_nulls_last(MessageConversation.last_message_at, descending=True),
            MessageConversation.id.desc(),
        )
        .limit(max(1, min(limit, 200)))
    )
    if status:
        stmt = stmt.where(MessageConversation.status == status)
    if assigned_to_user_id is not None:
        stmt = stmt.where(
            MessageConversation.assigned_to_user_id == assigned_to_user_id
        )
    if only_unread:
        stmt = stmt.where(MessageConversation.unread_count > 0)
    return list(db.scalars(stmt).all())


def get_conversation(db: Session, conversation_id: int) -> MessageConversation | None:
    return db.execute(
        select(MessageConversation)
        .options(
            selectinload(MessageConversation.customer),
            selectinload(MessageConversation.assigned_user),
            selectinload(MessageConversation.messages).selectinload(
                MessageThreadItem.sender
            ),
        )
        .where(MessageConversation.id == conversation_id)
    ).scalar_one_or_none()


def mark_conversation_read(db: Session, conversation_id: int) -> None:
    conv = db.get(MessageConversation, conversation_id)
    if conv is None:
        return
    conv.unread_count = 0
    db.flush()


def assign_conversation(
    db: Session, conversation_id: int, user_id: int | None
) -> MessageConversation:
    conv = db.get(MessageConversation, conversation_id)
    if conv is None:
        raise MessagingError("المحادثة غير موجودة.")
    conv.assigned_to_user_id = user_id
    db.flush()
    return conv


def set_conversation_status(
    db: Session, conversation_id: int, *, open_conversation: bool
) -> MessageConversation:
    conv = db.get(MessageConversation, conversation_id)
    if conv is None:
        raise MessagingError("المحادثة غير موجودة.")
    conv.status = (
        MessageConversationStatus.OPEN.value
        if open_conversation
        else MessageConversationStatus.CLOSED.value
    )
    db.flush()
    return conv


def send_agent_reply(
    db: Session,
    *,
    conversation_id: int,
    user_id: int,
    message: str,
    force: bool = False,
) -> MessageThreadItem:
    text = (message or "").strip()
    if not text:
        raise MessagingError("نص الرسالة مطلوب.")
    conv = db.get(MessageConversation, conversation_id)
    if conv is None:
        raise MessagingError("المحادثة غير موجودة.")
    if (conv.phone or "").strip().startswith("webchat:"):
        from modules.messaging.web_chat_service import record_agent_reply_for_inbox

        return record_agent_reply_for_inbox(
            db, conversation=conv, user_id=user_id, text=text
        )
    if not messaging_enabled(db):
        raise MessagingError("بوت المراسلات غير مفعّل.")
    customer = None
    if conv.customer_id:
        from modules.customers.models import Customer

        customer = db.get(Customer, conv.customer_id)
    if customer is None:
        customer = find_customer_for_phone(db, conv.phone)
        if customer is not None:
            conv.customer_id = customer.id
    if customer is None or not customer.is_active:
        raise MessagingError(
            "لا يوجد عميل مرتبط بهذا الرقم — أنشئ عميلاً بنفس الرقم أولاً."
        )
    if not customer.phone:
        raise MessagingError("العميل بدون رقم هاتف.")
    if not force and not customer_can_receive(db, customer.id):
        raise MessagingError(
            "العميل لم يوافق على المراسلات — فعّل «إرسال رغم ذلك» أو اطلب موافقته."
        )
    store = get_setting(db, "store_name", "نقطة البيع")
    body = f"{text}\n— {store}"
    meta: dict = {
        "conversation_id": conv.id,
        "sent_by_user_id": user_id,
        "kind": "inbox.reply",
    }
    prof = get_profile(db, customer.id)
    ch = MessageChannel.WHATSAPP.value
    if prof and prof.opt_in:
        from modules.messaging.outbox import resolve_channel_for_customer

        resolved = resolve_channel_for_customer(
            db, customer.id, [MessageChannel.WHATSAPP.value, MessageChannel.TELEGRAM.value]
        )
        if resolved:
            ch = resolved
    if prof and prof.telegram_chat_id:
        meta["telegram_chat_id"] = prof.telegram_chat_id
    outbox = enqueue_message(
        db,
        body=body,
        channel=ch,
        phone=customer.phone,
        customer_id=customer.id,
        event_type="inbox.reply",
        meta=meta,
    )
    now = datetime.now(timezone.utc)
    item = MessageThreadItem(
        conversation_id=conv.id,
        direction=MessageDirection.OUTBOUND.value,
        body=text,
        channel=ch,
        user_id=user_id,
        outbox_id=outbox.id,
    )
    db.add(item)
    conv.last_message_at = now
    conv.last_preview = _preview(f"أنت: {text}")
    conv.status = MessageConversationStatus.OPEN.value
    if conv.assigned_to_user_id is None:
        conv.assigned_to_user_id = user_id
    db.flush()
    send_outbox_ids_now(db, [outbox.id])
    return item


def inbox_unread_total(db: Session) -> int:
    return int(
        db.scalar(
            select(func.coalesce(func.sum(MessageConversation.unread_count), 0))
        )
        or 0
    )


def inbox_unread_conversations_count(db: Session) -> int:
    """عدد المحادثات التي فيها رسائل واردة غير مقروءة."""
    return int(
        db.scalar(
            select(func.count(MessageConversation.id)).where(
                MessageConversation.unread_count > 0
            )
        )
        or 0
    )


def inbox_pending_response_count(db: Session) -> int:
    """محادثات مفتوحة آخر رسالة فيها من العميل — تبقى الشارة حتى يرد الموظف."""
    return len(_open_conversation_ids_pending_response(db))


def _open_conversation_ids_pending_response(db: Session) -> list[int]:
    from sqlalchemy import desc

    open_convs = list(
        db.scalars(
            select(MessageConversation.id).where(
                MessageConversation.status == MessageConversationStatus.OPEN.value
            )
        ).all()
    )
    if not open_convs:
        return []
    pending: list[int] = []
    for cid in open_convs:
        last = db.scalar(
            select(MessageThreadItem.direction)
            .where(MessageThreadItem.conversation_id == cid)
            .order_by(desc(MessageThreadItem.created_at), desc(MessageThreadItem.id))
            .limit(1)
        )
        if last == MessageDirection.INBOUND.value:
            pending.append(int(cid))
    return pending


def close_conversations_pending_response(db: Session) -> int:
    """يُغلق المحادثات المفتوحة التي آخر رسالة فيها من العميل."""
    ids = _open_conversation_ids_pending_response(db)
    for cid in ids:
        set_conversation_status(db, cid, open_conversation=False)
    if ids:
        db.flush()
    return len(ids)


def record_outbound_from_legacy_send(
    db: Session,
    *,
    customer_id: int,
    phone: str,
    body: str,
    user_id: int | None,
    outbox_id: int,
    channel: str,
) -> None:
    """يربط إرسالاً من صفحة العميل بصندوق الوارد."""
    conv = get_or_create_conversation(db, phone=phone, customer_id=customer_id)
    now = datetime.now(timezone.utc)
    plain = body
    if "—" in plain:
        plain = plain.split("—")[0].strip()
    if plain.startswith("مرحباً"):
        lines = plain.split("\n")
        if len(lines) >= 2:
            plain = "\n".join(lines[1:]).strip()
    item = MessageThreadItem(
        conversation_id=conv.id,
        direction=MessageDirection.OUTBOUND.value,
        body=plain,
        channel=channel,
        user_id=user_id,
        outbox_id=outbox_id,
    )
    db.add(item)
    conv.last_message_at = now
    conv.last_preview = _preview(f"أنت: {plain}")
    db.flush()
