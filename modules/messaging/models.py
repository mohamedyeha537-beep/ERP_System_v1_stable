from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from infra.db import Base


class MessageChannel(str, enum.Enum):
    WHATSAPP = "whatsapp"
    TELEGRAM = "telegram"
    SMS = "sms"
    WEB = "web"
    NONE = "none"


class MessageOutboxStatus(str, enum.Enum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    CANCELLED = "cancelled"


class MessageAudience(str, enum.Enum):
    CUSTOMER = "customer"
    ADMIN = "admin"


class CustomerMessagingProfile(Base):
    __tablename__ = "customer_messaging_profiles"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), unique=True, index=True
    )
    opt_in: Mapped[bool] = mapped_column(Boolean, default=False)
    preferred_channel: Mapped[str] = mapped_column(
        String(32), default=MessageChannel.WHATSAPP.value
    )
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    consent_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    opt_in_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    opt_out_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class MessageTemplate(Base):
    __tablename__ = "message_templates"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160))
    body_text: Mapped[str] = mapped_column(Text)
    body_voice_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class MessageEventCategory(str, enum.Enum):
    AUTOMATIC = "automatic"
    PROMOTIONAL = "promotional"
    TEST = "test"


class MessageEventDefinition(Base):
    """حدث رسائل — يُنشئه الإدمن أو يأتي افتراضياً مع النظام."""

    __tablename__ = "message_event_definitions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name_ar: Mapped[str] = mapped_column(String(160))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str] = mapped_column(
        String(20), default=MessageEventCategory.AUTOMATIC.value, index=True
    )
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class MessageEventHook(Base):
    """ربط لحظة تشغيل في النظام (hook) بحدث أو أكثر."""

    __tablename__ = "message_event_hooks"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    hook_code: Mapped[str] = mapped_column(String(64), index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class MessageRule(Base):
    __tablename__ = "message_rules"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(160))
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    conditions_json: Mapped[str] = mapped_column(Text, default="{}")
    template_id: Mapped[int] = mapped_column(
        ForeignKey("message_templates.id", ondelete="CASCADE")
    )
    channels: Mapped[str] = mapped_column(String(120), default="whatsapp")
    audience: Mapped[str] = mapped_column(
        String(20), default=MessageAudience.CUSTOMER.value
    )
    priority: Mapped[int] = mapped_column(Integer, default=100)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    template: Mapped[MessageTemplate] = relationship()


class MessageCampaign(Base):
    __tablename__ = "message_campaigns"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(160))
    template_id: Mapped[int] = mapped_column(
        ForeignKey("message_templates.id", ondelete="CASCADE")
    )
    segment_json: Mapped[str] = mapped_column(Text, default="{}")
    starts_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ends_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    last_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    template: Mapped[MessageTemplate] = relationship()


class MessageOutbox(Base):
    __tablename__ = "message_outbox"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    phone: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    channel: Mapped[str] = mapped_column(String(32), default=MessageChannel.WHATSAPP.value)
    event_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(20), default=MessageOutboxStatus.PENDING.value, index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    rule_id: Mapped[int | None] = mapped_column(
        ForeignKey("message_rules.id", ondelete="SET NULL"), nullable=True
    )
    campaign_id: Mapped[int | None] = mapped_column(
        ForeignKey("message_campaigns.id", ondelete="SET NULL"), nullable=True
    )
    meta_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class MessageConversationStatus(str, enum.Enum):
    OPEN = "open"
    CLOSED = "closed"


class MessageDirection(str, enum.Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class MessageConversation(Base):
    """محادثة عميل — صندوق الوارد."""

    __tablename__ = "message_conversations"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    phone: Mapped[str] = mapped_column(String(40), index=True)
    customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(
        String(20), default=MessageConversationStatus.OPEN.value, index=True
    )
    assigned_to_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    last_message_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    last_preview: Mapped[str | None] = mapped_column(String(200), nullable=True)
    unread_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    customer: Mapped["Customer | None"] = relationship()  # noqa: F821
    assigned_user: Mapped["User | None"] = relationship()  # noqa: F821
    messages: Mapped[list["MessageThreadItem"]] = relationship(
        back_populates="conversation", order_by="MessageThreadItem.created_at"
    )


class MessageThreadItem(Base):
    """رسالة واحدة داخل المحادثة (واردة أو صادرة)."""

    __tablename__ = "message_thread_items"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("message_conversations.id", ondelete="CASCADE"), index=True
    )
    direction: Mapped[str] = mapped_column(String(16), index=True)
    body: Mapped[str] = mapped_column(Text)
    channel: Mapped[str] = mapped_column(String(32), default=MessageChannel.WHATSAPP.value)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    outbox_id: Mapped[int | None] = mapped_column(
        ForeignKey("message_outbox.id", ondelete="SET NULL"), nullable=True
    )
    external_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )

    conversation: Mapped[MessageConversation] = relationship(back_populates="messages")
    sender: Mapped["User | None"] = relationship()  # noqa: F821


class WebChatDepartment(str, enum.Enum):
    SUPPORT = "support"
    SALES = "sales"
    HUMAN = "human"


class WebChatSessionStatus(str, enum.Enum):
    OPEN = "open"
    CLOSED = "closed"


class WebChatSender(str, enum.Enum):
    GUEST = "guest"
    BOT = "bot"
    AGENT = "agent"


class WebChatSession(Base):
    """جلسة محادثة زائر على صفحة /chat."""

    __tablename__ = "web_chat_sessions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    guest_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    guest_phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    department: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    status: Mapped[str] = mapped_column(
        String(20), default=WebChatSessionStatus.OPEN.value, index=True
    )
    inbox_conversation_id: Mapped[int | None] = mapped_column(
        ForeignKey("message_conversations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: browse | cart | fulfillment | payment | await_receipt | submitted | completed
    order_phase: Mapped[str] = mapped_column(String(32), default="browse", index=True)
    order_json: Mapped[str] = mapped_column(Text, default="{}")
    sale_id: Mapped[int | None] = mapped_column(
        ForeignKey("sales.id", ondelete="SET NULL"), nullable=True, index=True
    )
    payment_proof_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    feedback_rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    feedback_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_message_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    messages: Mapped[list["WebChatMessage"]] = relationship(
        back_populates="session", order_by="WebChatMessage.created_at"
    )


class WebChatMessage(Base):
    """رسالة داخل جلسة محادثة الويب."""

    __tablename__ = "web_chat_messages"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("web_chat_sessions.id", ondelete="CASCADE"), index=True
    )
    direction: Mapped[str] = mapped_column(String(16), index=True)
    sender_type: Mapped[str] = mapped_column(String(16), default=WebChatSender.GUEST.value)
    body: Mapped[str] = mapped_column(Text)
    image_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    external_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )

    session: Mapped[WebChatSession] = relationship(back_populates="messages")


class MessagePhoneList(Base):
    """قائمة أرقام خارجية للحملات — لا تُنشئ عملاء تلقائياً."""

    __tablename__ = "message_phone_lists"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(160))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )

    entries: Mapped[list["MessagePhoneListEntry"]] = relationship(
        back_populates="phone_list", cascade="all, delete-orphan", lazy="selectin"
    )


class MessagePhoneListEntry(Base):
    __tablename__ = "message_phone_list_entries"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    list_id: Mapped[int] = mapped_column(
        ForeignKey("message_phone_lists.id", ondelete="CASCADE"), index=True
    )
    phone: Mapped[str] = mapped_column(String(40), index=True)
    display_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    is_valid: Mapped[bool] = mapped_column(Boolean, default=True)
    skip_reason: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    phone_list: Mapped[MessagePhoneList] = relationship(back_populates="entries")
