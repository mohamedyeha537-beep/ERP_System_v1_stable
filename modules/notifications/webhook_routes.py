"""Webhook inbound — يُفوّض لـ modules/messaging/router_api مع معالجة الأزرار."""
from __future__ import annotations

from modules.messaging.router_api import (
    messaging_inbound,
    messaging_inbound_ping,
    router,
)

__all__ = ["router", "messaging_inbound", "messaging_inbound_ping"]
