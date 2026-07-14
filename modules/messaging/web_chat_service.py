"""محادثة الزوار على صفحة /chat — جلسات، رسائل، وربط بصندوق الوارد."""
from __future__ import annotations

import json
import logging
import secrets
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.messaging.models import (
    MessageChannel,
    MessageConversation,
    MessageDirection,
    MessageThreadItem,
    WebChatDepartment,
    WebChatMessage,
    WebChatSender,
    WebChatSession,
    WebChatSessionStatus,
)
from modules.messaging.service import MessagingError
from modules.messaging.hermes_replies import hermes_start_message, smart_reply, smart_reply_full, build_hermes_reply_for_session
from modules.messaging.hermes_catalog import wants_human_agent, human_waiting_reply
from modules.messaging.web_chat_config import department_for_choice, web_chat_bot_name, web_chat_config_revision
from modules.settings.service import get_bool, get_setting

LOG = logging.getLogger("messaging.web_chat")

DEPARTMENT_ACK = {
    WebChatDepartment.SUPPORT.value: "تم اختيار **الدعم الفني**. اكتب سؤالك وسنساعدك.",
    WebChatDepartment.SALES.value: "تم اختيار **المبيعات**. أخبرنا بما تريد طلبه.",
    WebChatDepartment.HUMAN.value: "تم تحويلك لموظف. انتظر قليلاً وسنرد عليك هنا.",
}


def _welcome_message(db: Session) -> str:
    return hermes_start_message(db)


def web_chat_enabled(db: Session) -> bool:
    return get_bool(db, "web_chat_enabled", False)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _session_phone(session_id: int) -> str:
    return f"webchat:{session_id}"


def _parse_department(db: Session, text: str) -> str | None:
    return department_for_choice(db, text)


def get_session_by_token(db: Session, token: str) -> WebChatSession | None:
    tok = (token or "").strip()
    if not tok:
        return None
    return db.execute(
        select(WebChatSession).where(WebChatSession.token == tok)
    ).scalar_one_or_none()


def _add_message(
    db: Session,
    session: WebChatSession,
    *,
    body: str,
    direction: str,
    sender_type: str,
    external_id: str | None = None,
    image_url: str | None = None,
) -> WebChatMessage:
    text = (body or "").strip()
    img = (image_url or "").strip() or None
    if not text and not img:
        raise MessagingError("نص الرسالة فارغ.")
    if external_id:
        dup = db.execute(
            select(WebChatMessage.id).where(WebChatMessage.external_id == external_id.strip())
        ).scalar_one_or_none()
        if dup is not None:
            existing = db.get(WebChatMessage, dup)
            if existing:
                return existing
    now = _now()
    msg = WebChatMessage(
        session_id=session.id,
        direction=direction,
        sender_type=sender_type,
        body=text or " ",
        image_url=img,
        external_id=(external_id or "").strip() or None,
        created_at=now,
    )
    db.add(msg)
    session.last_message_at = now
    db.flush()
    return msg


def create_session(db: Session) -> tuple[WebChatSession, WebChatMessage]:
    token = secrets.token_urlsafe(32)
    session = WebChatSession(token=token, status=WebChatSessionStatus.OPEN.value)
    db.add(session)
    db.flush()
    welcome = _add_message(
        db,
        session,
        body=_welcome_message(db),
        direction=MessageDirection.OUTBOUND.value,
        sender_type=WebChatSender.BOT.value,
    )
    return session, welcome


def update_guest_profile(
    db: Session,
    *,
    token: str,
    name: str | None = None,
    phone: str | None = None,
) -> WebChatSession:
    session = get_session_by_token(db, token)
    if session is None:
        raise MessagingError("جلسة المحادثة غير موجودة.")
    if name is not None:
        session.guest_name = (name or "").strip() or None
    if phone is not None:
        phone_raw = (phone or "").strip()
        if not phone_raw:
            raise MessagingError("رقم الهاتف مطلوب.")
        from modules.customers.service import require_valid_phone

        try:
            session.guest_phone = require_valid_phone(phone_raw)
        except Exception as exc:
            raise MessagingError("رقم الهاتف غير صالح.") from exc
    db.flush()
    return session


def _sync_inbound_to_inbox(
    db: Session, session: WebChatSession, body: str
) -> MessageConversation | None:
    from modules.messaging.inbox_service import record_inbound_message

    try:
        conv, _item = record_inbound_message(
            db,
            phone=_session_phone(session.id),
            body=body,
            channel=MessageChannel.WEB.value,
            sender_name=session.guest_name,
        )
        session.inbox_conversation_id = conv.id
        db.flush()
        return conv
    except Exception:
        LOG.exception("failed syncing web chat to inbox session=%s", session.id)
        return None


def _forward_to_n8n(db: Session, session: WebChatSession, message: WebChatMessage) -> None:
    url = (get_setting(db, "web_chat_n8n_webhook_url", "") or "").strip()
    if not url:
        return
    payload = {
        "source": "web_chat",
        "session_token": session.token,
        "session_id": session.id,
        "message_id": message.id,
        "text": message.body,
        "name": session.guest_name or "",
        "phone": session.guest_phone or "",
        "department": session.department or "",
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def _post() -> None:
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                resp.read()
        except urllib.error.URLError as exc:
            LOG.warning("web chat n8n forward failed: %s", exc)

    threading.Thread(target=_post, daemon=True, name="web-chat-n8n").start()


def _maybe_local_bot_reply(
    db: Session, session: WebChatSession, guest_text: str
) -> list[WebChatMessage]:
    """Hermes محلي — يعمل عندما لا يوجد webhook خارجي."""
    if (get_setting(db, "web_chat_n8n_webhook_url", "") or "").strip():
        return []

    if wants_human_agent(guest_text):
        if session.department != WebChatDepartment.HUMAN.value:
            session.department = WebChatDepartment.HUMAN.value
            db.flush()
            hermes = smart_reply_full(guest_text, db)
        else:
            hermes = human_waiting_reply()
    else:
        dept = _parse_department(db, guest_text)
        if dept:
            session.department = dept
            db.flush()
            ack = DEPARTMENT_ACK.get(dept, smart_reply(guest_text, db))
            return [
                _add_message(
                    db,
                    session,
                    body=ack.replace("**", ""),
                    direction=MessageDirection.OUTBOUND.value,
                    sender_type=WebChatSender.BOT.value,
                )
            ]
        if session.department == WebChatDepartment.HUMAN.value:
            hermes = human_waiting_reply()
        else:
            hermes = build_hermes_reply_for_session(db, session, guest_text)

    out: list[WebChatMessage] = []
    for part in hermes.parts:
        out.append(
            _add_message(
                db,
                session,
                body=part.body,
                direction=MessageDirection.OUTBOUND.value,
                sender_type=WebChatSender.BOT.value,
                image_url=part.image_url,
            )
        )
    return out


def send_guest_message(
    db: Session, *, token: str, text: str
) -> tuple[WebChatMessage, list[WebChatMessage]]:
    session = get_session_by_token(db, token)
    if session is None:
        raise MessagingError("جلسة المحادثة غير موجودة.")
    if session.status == WebChatSessionStatus.CLOSED.value:
        raise MessagingError("انتهت هذه المحادثة.")
    guest_text = (text or "").strip()
    if not guest_text:
        raise MessagingError("نص الرسالة فارغ.")

    dept = _parse_department(db, guest_text)
    if dept and not session.department:
        session.department = dept
        db.flush()
    elif wants_human_agent(guest_text):
        session.department = WebChatDepartment.HUMAN.value
        db.flush()

    inbound = _add_message(
        db,
        session,
        body=guest_text,
        direction=MessageDirection.INBOUND.value,
        sender_type=WebChatSender.GUEST.value,
    )
    _sync_inbound_to_inbox(db, session, guest_text)
    _forward_to_n8n(db, session, inbound)
    bot_replies = _maybe_local_bot_reply(db, session, guest_text)
    return inbound, bot_replies


def send_guest_receipt_image(
    db: Session,
    *,
    token: str,
    proof_filename: str,
    caption: str = "",
) -> tuple[WebChatMessage, list[WebChatMessage]]:
    """رفع إيصال دفع من الزبون أثناء المحادثة."""
    from modules.messaging.chat_order_service import PHASE_AWAIT_RECEIPT

    session = get_session_by_token(db, token)
    if session is None:
        raise MessagingError("جلسة المحادثة غير موجودة.")
    if session.status == WebChatSessionStatus.CLOSED.value:
        raise MessagingError("انتهت هذه المحادثة.")
    if (session.order_phase or "").strip() != PHASE_AWAIT_RECEIPT:
        raise MessagingError(
            "رفع الإيصال متاح فقط بعد اختيار الدفع المصرفي وإتمام التحويل."
        )

    img_url = f"/static/{proof_filename.replace(chr(92), '/')}"
    body = (caption or "").strip() or "📎 إيصال تحويل"
    inbound = _add_message(
        db,
        session,
        body=body,
        direction=MessageDirection.INBOUND.value,
        sender_type=WebChatSender.GUEST.value,
        image_url=img_url,
    )
    _sync_inbound_to_inbox(db, session, body)
    _forward_to_n8n(db, session, inbound)

    if (get_setting(db, "web_chat_n8n_webhook_url", "") or "").strip():
        return inbound, []

    from modules.messaging.hermes_order_flow import handle_receipt_upload

    hermes = handle_receipt_upload(db, session, proof_filename)
    bot_replies: list[WebChatMessage] = []
    for part in hermes.parts:
        bot_replies.append(
            _add_message(
                db,
                session,
                body=part.body,
                direction=MessageDirection.OUTBOUND.value,
                sender_type=WebChatSender.BOT.value,
                image_url=part.image_url,
            )
        )
    return inbound, bot_replies


def list_messages(
    db: Session, *, token: str, since_id: int = 0
) -> tuple[WebChatSession, list[WebChatMessage]]:
    session = get_session_by_token(db, token)
    if session is None:
        raise MessagingError("جلسة المحادثة غير موجودة.")
    sid = max(0, int(since_id or 0))
    rows = db.scalars(
        select(WebChatMessage)
        .where(
            WebChatMessage.session_id == session.id,
            WebChatMessage.id > sid,
        )
        .order_by(WebChatMessage.id.asc())
        .limit(200)
    ).all()
    return session, list(rows)


def record_bot_reply(
    db: Session,
    *,
    session_token: str,
    text: str,
    external_id: str | None = None,
) -> WebChatMessage:
    session = get_session_by_token(db, session_token)
    if session is None:
        raise MessagingError("جلسة المحادثة غير موجودة.")
    return _add_message(
        db,
        session,
        body=text,
        direction=MessageDirection.OUTBOUND.value,
        sender_type=WebChatSender.BOT.value,
        external_id=external_id,
    )


def record_agent_reply_for_inbox(
    db: Session,
    *,
    conversation: MessageConversation,
    user_id: int,
    text: str,
) -> MessageThreadItem:
    phone = (conversation.phone or "").strip()
    if not phone.startswith("webchat:"):
        raise MessagingError("ليست محادثة ويب.")
    try:
        session_id = int(phone.split(":", 1)[1])
    except (IndexError, ValueError) as exc:
        raise MessagingError("معرّف جلسة الويب غير صالح.") from exc
    session = db.get(WebChatSession, session_id)
    if session is None:
        raise MessagingError("جلسة المحادثة غير موجودة.")

    plain = (text or "").strip()
    if not plain:
        raise MessagingError("نص الرسالة مطلوب.")

    _add_message(
        db,
        session,
        body=plain,
        direction=MessageDirection.OUTBOUND.value,
        sender_type=WebChatSender.AGENT.value,
    )
    now = _now()
    item = MessageThreadItem(
        conversation_id=conversation.id,
        direction=MessageDirection.OUTBOUND.value,
        body=plain,
        channel=MessageChannel.WEB.value,
        user_id=user_id,
    )
    db.add(item)
    conversation.last_message_at = now
    conversation.last_preview = plain[:160]
    conversation.status = "open"
    if conversation.assigned_to_user_id is None:
        conversation.assigned_to_user_id = user_id
    db.flush()
    return item


def session_to_dict(session: WebChatSession, db: Session | None = None) -> dict:
    bot_name = web_chat_bot_name(db) if db is not None else "مساعد"
    cfg_rev = web_chat_config_revision(db) if db is not None else "0"
    return {
        "token": session.token,
        "guest_name": session.guest_name,
        "guest_phone": session.guest_phone,
        "department": session.department,
        "status": session.status,
        "order_phase": getattr(session, "order_phase", None) or "browse",
        "feedback_rating": getattr(session, "feedback_rating", None),
        "sale_id": getattr(session, "sale_id", None),
        "bot_name": bot_name,
        "config_revision": cfg_rev,
        "last_message_at": session.last_message_at.isoformat() if session.last_message_at else None,
    }


def message_to_dict(msg: WebChatMessage) -> dict:
    return {
        "id": msg.id,
        "direction": msg.direction,
        "sender_type": msg.sender_type,
        "body": msg.body,
        "image_url": msg.image_url,
        "created_at": msg.created_at.isoformat() if msg.created_at else None,
    }
