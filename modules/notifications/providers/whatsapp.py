from __future__ import annotations

from sqlalchemy.orm import Session

from modules.messaging.outbox import whatsapp_provider
from modules.messaging.phone_utils import normalize_whatsapp_phone
from modules.messaging.providers.textmebot import send_textmebot
from modules.notifications.providers.base import MessagingProvider, OutboundMessage, SendResult
from modules.settings.service import get_setting


class WhatsAppProvider(MessagingProvider):
    def __init__(self, db: Session) -> None:
        self._db = db

    def send(self, message: OutboundMessage) -> SendResult:
        prov = whatsapp_provider(self._db)
        if prov != "textmebot":
            return SendResult(ok=False, provider=prov, error="Notification engine Phase 1: textmebot only")
        apikey = (get_setting(self._db, "messaging_textmebot_apikey") or "").strip()
        base = (
            get_setting(self._db, "messaging_textmebot_base_url") or "http://api.textmebot.com/send.php"
        ).strip()
        recipient = normalize_whatsapp_phone(
            message.phone,
            country_code=(get_setting(self._db, "messaging_country_code", "218") or "218"),
        )
        if not recipient:
            return SendResult(ok=False, provider="textmebot", error="رقم غير صالح")
        try:
            raw = send_textmebot(
                base_url=base,
                apikey=apikey,
                recipient=recipient,
                text=message.text,
                file_url=message.image_url,
                document_url=message.document_url,
                document_filename=message.document_filename,
                buttons=message.buttons or None,
            )
            return SendResult(ok=True, provider="textmebot", raw=raw)
        except Exception as exc:  # noqa: BLE001
            return SendResult(ok=False, provider="textmebot", error=str(exc)[:500])


def get_provider(db: Session, channel: str) -> MessagingProvider | None:
    ch = (channel or "").strip().lower()
    if ch == "whatsapp":
        return WhatsAppProvider(db)
    return None
