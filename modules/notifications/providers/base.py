from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SendResult:
    ok: bool
    provider: str
    message_id: str | None = None
    error: str | None = None
    raw: str | None = None


@dataclass
class OutboundMessage:
    phone: str
    text: str
    image_url: str | None = None
    document_url: str | None = None
    document_filename: str | None = None
    buttons: list[dict[str, str]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


class MessagingProvider(ABC):
    @abstractmethod
    def send(self, message: OutboundMessage) -> SendResult:
        raise NotImplementedError
