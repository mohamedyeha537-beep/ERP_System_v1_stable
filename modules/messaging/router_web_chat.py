"""صفحة محادثة الزوار /chat وواجهة API."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from app.deps import DBSession, require_permission
from app.jinja_env import templates
from modules.authz.models import User
from modules.authz.permissions import MESSAGING_MANAGE
from modules.catalog.uploads import save_sale_payment_proof
from modules.messaging.service import MessagingError
from modules.messaging.web_chat_service import (
    create_session,
    list_messages,
    message_to_dict,
    record_bot_reply,
    send_guest_message,
    send_guest_receipt_image,
    session_to_dict,
    update_guest_profile,
    web_chat_enabled,
)
from modules.settings.service import get_setting

router = APIRouter(tags=["web-chat"])
api_router = APIRouter(prefix="/api/chat", tags=["web-chat-api"])
admin_router = APIRouter(prefix="/admin/messaging/chat-orders", tags=["web-chat-admin"])

_STATIC_ROOT = Path(__file__).resolve().parents[2] / "app" / "static"


class GuestProfilePayload(BaseModel):
    token: str
    name: str | None = None
    phone: str | None = None


class GuestSendPayload(BaseModel):
    token: str
    text: str = Field(..., min_length=1, max_length=4000)


class BotReplyPayload(BaseModel):
    session_token: str
    text: str = Field(..., min_length=1, max_length=8000)
    external_id: str | None = None


def _verify_reply_secret(
    db: DBSession, secret_header: str | None, request: Request
) -> None:
    if not web_chat_enabled(db):
        raise HTTPException(status_code=403, detail="محادثة الويب غير مفعّلة.")
    expected = (get_setting(db, "messaging_inbound_secret", "") or "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="لم يُضبط مفتاح الأمان.")
    provided = (secret_header or "").strip()
    if not provided:
        provided = (request.query_params.get("secret") or "").strip()
    if provided != expected:
        raise HTTPException(status_code=401, detail="مفتاح غير صالح.")


def _require_web_chat(db: DBSession) -> None:
    if not web_chat_enabled(db):
        raise HTTPException(status_code=403, detail="محادثة الويب غير مفعّلة حالياً.")


@router.get("/chat", response_class=HTMLResponse)
def guest_chat_page(request: Request, db: DBSession):
    from modules.messaging.web_chat_config import web_chat_bot_name
    from modules.shop.service import shop_enabled as is_shop_enabled

    enabled = web_chat_enabled(db)
    return templates.TemplateResponse(
        "guest_chat.html",
        {
            "request": request,
            "enabled": enabled,
            "store_name": get_setting(db, "store_name", "نقطة البيع"),
            "web_chat_bot_name": web_chat_bot_name(db),
            "shop_enabled": is_shop_enabled(db),
        },
    )


@api_router.post("/session")
def chat_create_session(db: DBSession):
    _require_web_chat(db)
    try:
        session, welcome = create_session(db)
        db.commit()
    except MessagingError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "ok": True,
        "session": session_to_dict(session, db),
        "messages": [message_to_dict(welcome)],
    }


@api_router.get("/session")
def chat_get_session(token: str, db: DBSession):
    _require_web_chat(db)
    from modules.messaging.web_chat_service import get_session_by_token

    session = get_session_by_token(db, token.strip())
    if session is None:
        raise HTTPException(status_code=404, detail="جلسة غير موجودة.")
    return {"ok": True, "session": session_to_dict(session, db)}


@api_router.patch("/profile")
def chat_update_profile(payload: GuestProfilePayload, db: DBSession):
    _require_web_chat(db)
    try:
        session = update_guest_profile(
            db,
            token=payload.token,
            name=payload.name,
            phone=payload.phone,
        )
        bot_replies: list = []
        phone = (session.guest_phone or "").strip()
        if phone:
            from modules.messaging.hermes_loyalty import profile_saved_loyalty_message
            from modules.messaging.web_chat_service import _add_message
            from modules.messaging.models import MessageDirection, WebChatSender

            msg = profile_saved_loyalty_message(db, phone)
            if msg:
                bot_replies.append(
                    _add_message(
                        db,
                        session,
                        body=msg,
                        direction=MessageDirection.OUTBOUND.value,
                        sender_type=WebChatSender.BOT.value,
                    )
                )
        db.commit()
    except MessagingError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    out = {"ok": True, "session": session_to_dict(session, db)}
    if bot_replies:
        out["messages"] = [message_to_dict(m) for m in bot_replies]
    return out


@api_router.post("/send")
def chat_send(payload: GuestSendPayload, db: DBSession):
    _require_web_chat(db)
    try:
        inbound, bot_replies = send_guest_message(db, token=payload.token, text=payload.text)
        db.commit()
    except MessagingError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    from modules.messaging.web_chat_service import get_session_by_token

    session = get_session_by_token(db, payload.token.strip())
    out = [message_to_dict(inbound)]
    for bot in bot_replies:
        out.append(message_to_dict(bot))
    return {
        "ok": True,
        "session": session_to_dict(session, db) if session else None,
        "messages": out,
    }


@api_router.get("/messages")
def chat_poll_messages(token: str, db: DBSession, since_id: int = 0):
    _require_web_chat(db)
    try:
        session, rows = list_messages(db, token=token, since_id=since_id)
    except MessagingError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "ok": True,
        "session": session_to_dict(session, db),
        "messages": [message_to_dict(m) for m in rows],
    }


@api_router.get("/reply")
def chat_reply_ping(
    request: Request,
    db: DBSession,
    x_messaging_secret: str | None = Header(None, alias="X-Messaging-Secret"),
):
    _verify_reply_secret(db, x_messaging_secret, request)
    return {
        "ok": True,
        "message": "المفتاح صحيح. أرسل ردود البوت عبر POST.",
        "method_required": "POST",
        "example_body": {
            "session_token": "…",
            "text": "نص الرد",
        },
    }


@api_router.post("/reply")
def chat_bot_reply(
    payload: BotReplyPayload,
    request: Request,
    db: DBSession,
    x_messaging_secret: str | None = Header(None, alias="X-Messaging-Secret"),
):
    _verify_reply_secret(db, x_messaging_secret, request)
    try:
        msg = record_bot_reply(
            db,
            session_token=payload.session_token,
            text=payload.text,
            external_id=payload.external_id,
        )
        db.commit()
    except MessagingError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "message_id": msg.id}


@api_router.post("/upload-receipt")
async def chat_upload_receipt(
    db: DBSession,
    token: str = Form(...),
    caption: str = Form(""),
    file: UploadFile = File(...),
):
    _require_web_chat(db)
    from modules.messaging.chat_order_service import PHASE_AWAIT_RECEIPT
    from modules.messaging.web_chat_service import get_session_by_token

    session = get_session_by_token(db, token.strip())
    if session is None:
        raise HTTPException(status_code=404, detail="جلسة المحادثة غير موجودة.")
    if (session.order_phase or "").strip() != PHASE_AWAIT_RECEIPT:
        raise HTTPException(
            status_code=400,
            detail="رفع الإيصال متاح فقط بعد اختيار الدفع المصرفي وإتمام التحويل.",
        )
    try:
        proof_fn = save_sale_payment_proof(file, _STATIC_ROOT)
        inbound, bot_replies = send_guest_receipt_image(
            db, token=token.strip(), proof_filename=proof_fn, caption=caption
        )
        db.commit()
    except (MessagingError, ValueError) as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session = get_session_by_token(db, token.strip())
    out = [message_to_dict(inbound)]
    for bot in bot_replies:
        out.append(message_to_dict(bot))
    return {
        "ok": True,
        "session": session_to_dict(session, db) if session else None,
        "messages": out,
    }


@admin_router.get("", response_class=HTMLResponse)
def admin_chat_orders_page(request: Request, db: DBSession, user: User = Depends(require_permission(MESSAGING_MANAGE))):
    from modules.messaging.admin_nav import messaging_admin_nav_ctx
    from modules.messaging.chat_order_service import (
        chat_orders_stats,
        list_in_progress_chat_orders,
        list_pending_chat_orders,
    )

    orders = list_pending_chat_orders(db)
    in_progress = list_in_progress_chat_orders(db)
    stats = chat_orders_stats(db)
    return templates.TemplateResponse(
        "admin_chat_orders.html",
        {
            "request": request,
            "orders": orders,
            "in_progress_orders": in_progress,
            "chat_stats": stats,
            **messaging_admin_nav_ctx(db),
            "saved": request.query_params.get("saved"),
            "err": request.query_params.get("err"),
        },
    )


@admin_router.post("/cancel-all-pending", response_class=RedirectResponse)
def admin_cancel_all_pending_chat_orders(
    db: DBSession,
    _: User = Depends(require_permission(MESSAGING_MANAGE)),
):
    from modules.messaging.chat_order_service import cancel_all_pending_chat_orders
    from modules.sales.service import SalesError

    try:
        n = cancel_all_pending_chat_orders(db)
        db.commit()
    except SalesError as exc:
        db.rollback()
        return RedirectResponse(
            f"/admin/messaging/chat-orders?err={quote(str(exc))}",
            status_code=302,
        )
    return RedirectResponse(
        f"/admin/messaging/chat-orders?saved=cancelled&n={n}",
        status_code=302,
    )


@admin_router.post("/{sale_id}/confirm", response_class=RedirectResponse)
def admin_confirm_chat_order(
    sale_id: int,
    db: DBSession,
    user: User = Depends(require_permission(MESSAGING_MANAGE)),
):
    from modules.messaging.chat_order_service import confirm_chat_order_for_kitchen
    from modules.sales.service import SalesError

    try:
        confirm_chat_order_for_kitchen(
            db, sale_id=sale_id, user_id=user.id, pos_shift_id=None, complete_payment=True
        )
        db.commit()
    except (SalesError, MessagingError) as exc:
        db.rollback()
        return RedirectResponse(
            f"/admin/messaging/chat-orders?err={quote(str(exc))}",
            status_code=302,
        )
    return RedirectResponse("/admin/messaging/chat-orders?saved=1", status_code=302)


@admin_router.post("/{sale_id}/cancel", response_class=RedirectResponse)
def admin_cancel_chat_order(
    sale_id: int,
    db: DBSession,
    _: User = Depends(require_permission(MESSAGING_MANAGE)),
):
    from modules.messaging.chat_order_service import cancel_chat_order
    from modules.sales.service import SalesError

    try:
        cancel_chat_order(db, sale_id)
        db.commit()
    except SalesError as exc:
        db.rollback()
        return RedirectResponse(
            f"/admin/messaging/chat-orders?err={quote(str(exc))}",
            status_code=302,
        )
    return RedirectResponse("/admin/messaging/chat-orders?saved=1", status_code=302)
