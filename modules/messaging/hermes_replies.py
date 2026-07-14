"""Hermes — واجهة الردود على /chat."""
from __future__ import annotations

from sqlalchemy.orm import Session

from modules.messaging.hermes_catalog import HermesReply, build_hermes_reply, wants_human_agent
from modules.messaging.models import WebChatSession
from modules.messaging.web_chat_config import build_web_chat_greeting


def smart_reply(text: str, db: Session | None = None) -> str:
    """نص الرد الأول — للجسر الخارجي."""
    if db is None:
        return "شكراً لتواصلك! اكتب «منيو» أو اسم صنف."
    reply = build_hermes_reply(text, db)
    if not reply.parts:
        return "شكراً لتواصلك!"
    return reply.parts[0].body


def smart_reply_full(text: str, db: Session) -> HermesReply:
    return build_hermes_reply(text, db)


def build_hermes_reply_for_session(
    db: Session, session: WebChatSession, text: str
) -> HermesReply:
    """رد Hermes مع مسار الطلب الكامل في المحادثة."""
    if wants_human_agent(text):
        return build_hermes_reply(text, db)

    from modules.messaging.hermes_feedback import handle_feedback
    from modules.messaging.hermes_loyalty import handle_loyalty_message
    from modules.messaging.hermes_order_flow import handle_chat_order

    fb = handle_feedback(db, session, text)
    if fb is not None:
        return fb

    loyalty = handle_loyalty_message(db, session, text)
    if loyalty is not None:
        return loyalty

    from modules.messaging.hermes_conversation import try_how_to_order_reply

    how_to = try_how_to_order_reply(db, text)
    if how_to is not None:
        return how_to

    order_reply = handle_chat_order(db, session, text)
    if order_reply is not None:
        return order_reply
    return build_hermes_reply(text, db, session=session)


def hermes_start_message(db: Session | None = None) -> str:
    if db is None:
        return (
            "👋 مرحباً!\n"
            "أنا جاهز لمساعدتك في إتمام طلبيتك.\n\n"
            "1 — المبيعات والطلبات"
        )
    return build_web_chat_greeting(db)
