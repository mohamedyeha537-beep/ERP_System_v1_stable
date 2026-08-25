from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from infra.db import Base


class SyncEventStatus(str, enum.Enum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    ACKED = "acked"


class SyncEventAction(str, enum.Enum):
    INSERT = "INSERT"
    UPDATE = "UPDATE"
    DELETE = "DELETE"


class SyncEvent(Base):
    """سجل أحداث التغيير المحلية المطلوب دفعها للسيرفر المركزي."""

    __tablename__ = "sync_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    site_id: Mapped[str] = mapped_column(String(64), index=True)
    action: Mapped[str] = mapped_column(String(10))
    table_name: Mapped[str] = mapped_column(String(64), index=True)
    record_id: Mapped[str] = mapped_column(String(120), index=True)
    payload_json: Mapped[str] = mapped_column(Text)
    source_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    status: Mapped[str] = mapped_column(
        String(20), default=SyncEventStatus.PENDING.value, index=True
    )
    retries: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class SyncIncomingEvent(Base):
    """أحداث واردة من السيرفر المركزي أو من مواقع أخرى لتطبيقها محلياً."""

    __tablename__ = "sync_incoming_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    site_id: Mapped[str] = mapped_column(String(64), index=True)
    action: Mapped[str] = mapped_column(String(10))
    table_name: Mapped[str] = mapped_column(String(64), index=True)
    record_id: Mapped[str] = mapped_column(String(120), index=True)
    payload_json: Mapped[str] = mapped_column(Text)
    source_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class SyncRecordMap(Base):
    """ربط معرّفات السجلات البعيدة بالمحلية عند المزامنة من مواقع مختلفة."""

    __tablename__ = "sync_record_map"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    site_id: Mapped[str] = mapped_column(String(64), index=True)
    table_name: Mapped[str] = mapped_column(String(64), index=True)
    remote_record_id: Mapped[str] = mapped_column(String(120), index=True)
    local_record_id: Mapped[str] = mapped_column(String(120), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class SyncState(Base):
    """حالة المزامنة العامة للموقع الحالي."""

    __tablename__ = "sync_state"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    site_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    last_push_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_pull_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_event_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_online: Mapped[bool] = mapped_column(default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
