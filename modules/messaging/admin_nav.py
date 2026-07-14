"""سياق شريط التنقّل المشترك لصفحات إدارة المراسلات."""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from modules.messaging.models import MessageOutbox, MessageOutboxStatus


def pending_chat_orders_count(db: Session) -> int:
    from modules.messaging.chat_order_service import (
        PHASE_AWAIT_RECEIPT,
        PHASE_SUBMITTED,
    )
    from modules.messaging.models import WebChatSession
    from modules.messaging.web_chat_service import web_chat_enabled
    from modules.sales.models import Sale, SaleStatus

    if not web_chat_enabled(db):
        return 0
    n = db.scalar(
        select(func.count(WebChatSession.id))
        .join(Sale, Sale.id == WebChatSession.sale_id)
        .where(
            WebChatSession.order_phase.in_([PHASE_AWAIT_RECEIPT, PHASE_SUBMITTED]),
            Sale.status == SaleStatus.DRAFT,
            Sale.sent_to_kitchen_at.is_(None),
        )
    )
    return int(n or 0)


def messaging_admin_nav_ctx(db: Session) -> dict:
    try:
        from modules.messaging.inbox_service import inbox_unread_conversations_count

        inbox_unread = inbox_unread_conversations_count(db)
    except Exception:
        inbox_unread = 0
    try:
        chat_pending = pending_chat_orders_count(db)
    except Exception:
        chat_pending = 0
    failed = db.scalar(
        select(func.count())
        .select_from(MessageOutbox)
        .where(MessageOutbox.status == MessageOutboxStatus.FAILED.value)
    ) or 0
    return {
        "inbox_unread_conversations": inbox_unread,
        "pending_chat_orders": chat_pending,
        "failed": int(failed),
    }
