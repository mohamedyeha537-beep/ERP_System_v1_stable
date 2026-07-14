from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from infra.db import Base


class NotificationEventStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSED = "processed"
    FAILED = "failed"


class NotificationChannel(str, enum.Enum):
    WHATSAPP = "whatsapp"
    TELEGRAM = "telegram"
    INTERNAL = "internal"


class NotificationRecipientType(str, enum.Enum):
    CUSTOMER = "customer"
    ADMIN = "admin"
    CASHIER = "cashier"
    KITCHEN = "kitchen"
    REFERRER = "referrer"
    DRIVER = "driver"
    SUPERVISOR = "supervisor"
    INVENTORY_MANAGER = "inventory_manager"
    HR_MANAGER = "hr_manager"
    EMPLOYEE = "employee"


class NotificationMessageType(str, enum.Enum):
    TEXT = "text"
    IMAGE = "image"
    DOCUMENT = "document"
    INTERACTIVE = "interactive"


class NotificationLogStatus(str, enum.Enum):
    QUEUED = "queued"
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"


class NotificationActionStatus(str, enum.Enum):
    PENDING = "pending"
    HANDLED = "handled"
    FAILED = "failed"


class NotificationRulePriority(str, enum.Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


class NotificationEvent(Base):
    __tablename__ = "notification_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    event_key: Mapped[str] = mapped_column(String(64), index=True)
    source_type: Mapped[str] = mapped_column(String(32), index=True)
    source_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(
        String(20), default=NotificationEventStatus.PENDING.value, index=True
    )
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class NotificationTemplate(Base):
    __tablename__ = "notification_templates"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(160))
    event_key: Mapped[str] = mapped_column(String(64), index=True)
    channel: Mapped[str] = mapped_column(
        String(32), default=NotificationChannel.WHATSAPP.value
    )
    recipient_type: Mapped[str] = mapped_column(String(32), index=True)
    message_type: Mapped[str] = mapped_column(
        String(20), default=NotificationMessageType.TEXT.value
    )
    title: Mapped[str | None] = mapped_column(String(160), nullable=True)
    body_template: Mapped[str] = mapped_column(Text)
    buttons_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    document_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    language: Mapped[str] = mapped_column(String(8), default="ar")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class NotificationRule(Base):
    __tablename__ = "notification_rules"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    event_key: Mapped[str] = mapped_column(String(64), index=True)
    recipient_type: Mapped[str] = mapped_column(String(32), index=True)
    channel: Mapped[str] = mapped_column(
        String(32), default=NotificationChannel.WHATSAPP.value
    )
    template_id: Mapped[int] = mapped_column(
        ForeignKey("notification_templates.id", ondelete="CASCADE")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[str] = mapped_column(
        String(16), default=NotificationRulePriority.NORMAL.value
    )
    delay_seconds: Mapped[int] = mapped_column(Integer, default=0)
    condition_json: Mapped[str] = mapped_column(Text, default="{}")
    throttle_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    template: Mapped[NotificationTemplate] = relationship()


class NotificationLog(Base):
    __tablename__ = "notification_logs"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_notification_logs_idempotency"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    event_key: Mapped[str] = mapped_column(String(64), index=True)
    notification_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("notification_events.id", ondelete="SET NULL"), nullable=True
    )
    source_type: Mapped[str] = mapped_column(String(32), index=True)
    source_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    recipient_type: Mapped[str] = mapped_column(String(32))
    recipient_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    recipient_phone: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    channel: Mapped[str] = mapped_column(String(32))
    message_type: Mapped[str] = mapped_column(String(20))
    template_id: Mapped[int | None] = mapped_column(
        ForeignKey("notification_templates.id", ondelete="SET NULL"), nullable=True
    )
    body_rendered: Mapped[str] = mapped_column(Text)
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    outbox_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    status: Mapped[str] = mapped_column(
        String(20), default=NotificationLogStatus.QUEUED.value, index=True
    )
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )


class NotificationRoute(Base):
    """توجيه أحداث النظام إلى أرقام واتساب محددة (حسب نمط الحدث)."""

    __tablename__ = "notification_routes"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name_ar: Mapped[str] = mapped_column(String(120))
    event_patterns_json: Mapped[str] = mapped_column(Text, default="[]")
    phones: Mapped[str] = mapped_column(Text, default="")
    recipient_scope: Mapped[str] = mapped_column(
        String(32), default="*", index=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=100, index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class NotificationAction(Base):
    __tablename__ = "notification_actions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    notification_log_id: Mapped[int | None] = mapped_column(
        ForeignKey("notification_logs.id", ondelete="CASCADE"), nullable=True, index=True
    )
    action_key: Mapped[str] = mapped_column(String(64), index=True)
    action_payload_json: Mapped[str] = mapped_column(Text, default="{}")
    received_from_phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    handled_status: Mapped[str] = mapped_column(
        String(20), default=NotificationActionStatus.PENDING.value, index=True
    )
    handled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    result_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
