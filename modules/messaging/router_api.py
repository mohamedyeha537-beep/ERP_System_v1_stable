"""Webhook API — استقبال رسائل واتساب/تليجرام الواردة من n8n أو TextMeBot أو مزود خارجي."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from app.deps import DBSession
from modules.messaging.inbox_service import record_inbound_message
from modules.messaging.service import MessagingError
from modules.settings.service import get_bool, get_setting

router = APIRouter(prefix="/api/messaging", tags=["messaging-api"])


class InboundMessagePayload(BaseModel):
    phone: str = Field(..., description="رقم المرسل")
    text: str = Field(..., description="نص الرسالة")
    channel: str = Field("whatsapp", description="whatsapp | telegram")
    external_id: str | None = Field(None, description="معرّف خارجي لمنع التكرار")
    name: str | None = Field(None, description="اسم المرسل إن وُجد")


def _normalize_inbound_body(raw: dict[str, Any]) -> InboundMessagePayload:
    """قبول JSON POS القياسي أو webhook TextMeBot Pro."""
    if "phone" in raw and "text" in raw:
        return InboundMessagePayload.model_validate(raw)
    if "from" in raw and "message" in raw:
        phone = str(raw.get("from") or "").strip().rstrip(".")
        text = str(raw.get("message") or "").strip()
        if not phone or not text:
            raise HTTPException(status_code=422, detail="from/message مطلوبان.")
        name = str(raw.get("from_name") or "").strip() or None
        ext = f"tmb:{phone}:{text[:80]}"
        return InboundMessagePayload(
            phone=phone,
            text=text,
            channel="whatsapp",
            external_id=ext,
            name=name,
        )
    raise HTTPException(
        status_code=422,
        detail="صيغة غير معروفة — استخدم {phone,text} أو webhook TextMeBot {from,message}.",
    )

def _verify_inbound_secret(
    db: DBSession, secret_header: str | None, request: Request
) -> None:
    if not get_bool(db, "messaging_inbound_enabled", False):
        raise HTTPException(status_code=403, detail="استقبال الرسائل الواردة غير مفعّل.")
    expected = (get_setting(db, "messaging_inbound_secret", "") or "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="لم يُضبط مفتاح استقبال الرسائل.")
    provided = (secret_header or "").strip()
    if not provided:
        provided = (request.query_params.get("secret") or "").strip()
    if provided != expected:
        raise HTTPException(status_code=401, detail="مفتاح غير صالح.")


@router.get("/inbound")
def messaging_inbound_ping(
    request: Request,
    db: DBSession,
    x_messaging_secret: str | None = Header(None, alias="X-Messaging-Secret"),
):
    """فحص سريع من المتصفح — التحقق من المفتاح فقط (لا يُسجّل رسالة)."""
    _verify_inbound_secret(db, x_messaging_secret, request)
    return {
        "ok": True,
        "message": "المفتاح صحيح واستقبال الرسائل مفعّل. أرسل رسائل العملاء عبر POST وليس GET.",
        "method_required": "POST",
        "example_body": {
            "phone": "0912345678",
            "text": "نص رسالة العميل",
            "channel": "whatsapp",
        },
    }


@router.post("/inbound")
async def messaging_inbound(
    request: Request,
    db: DBSession,
    x_messaging_secret: str | None = Header(None, alias="X-Messaging-Secret"),
):
    _verify_inbound_secret(db, x_messaging_secret, request)
    try:
        raw = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="JSON غير صالح.") from exc
    if not isinstance(raw, dict):
        raise HTTPException(status_code=422, detail="المتوقع كائن JSON.")
    payload = _normalize_inbound_body(raw)
    try:
        conv, item = record_inbound_message(
            db,
            phone=payload.phone,
            body=payload.text,
            channel=payload.channel,
            external_id=payload.external_id,
            sender_name=payload.name,
        )
        action_result = None
        try:
            from modules.notifications.action_handler import handle_incoming_action

            action_result = handle_incoming_action(
                db, phone=payload.phone, text=payload.text
            )
        except Exception:  # noqa: BLE001
            pass
        db.commit()
    except MessagingError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "ok": True,
        "conversation_id": conv.id,
        "message_id": item.id,
        "unread_count": conv.unread_count,
        "action": action_result,
    }