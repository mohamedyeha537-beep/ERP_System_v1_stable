"""نماذج سجل النشاط وآخر زيارة لكل مستخدم/قسم."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from infra.db import Base


class DashboardActivity(Base):
    """حدث واحد يظهر كإشعار على كرت القسم المناسب."""

    __tablename__ = "dashboard_activities"
    __table_args__ = (
        Index("ix_dashboard_activities_section_created", "section_key", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    section_key: Mapped[str] = mapped_column(String(64), index=True)
    event_type: Mapped[str] = mapped_column(String(64), default="change")
    ref_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )


class DashboardSectionSeen(Base):
    """آخر مرة دخل فيها المستخدم قسمًا — ما بعدها يُحسب «جديدًا»."""

    __tablename__ = "dashboard_section_seen"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    section_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )


class ActivityHubItemState(Base):
    """حالة عنصر في مركز الإشعارات لكل مستخدم: مقروء / محذوف."""

    __tablename__ = "activity_hub_item_states"
    __table_args__ = (
        Index("ix_activity_hub_item_states_user", "user_id", "state"),
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    item_key: Mapped[str] = mapped_column(String(96), primary_key=True)
    state: Mapped[str] = mapped_column(String(16), default="read")  # read | deleted
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )


class ActivityHubMute(Base):
    """كتم نوع إشعار — لا يُعرض للمستخدم ولا يُحسب في الجرس."""

    __tablename__ = "activity_hub_mutes"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    mute_key: Mapped[str] = mapped_column(String(96), primary_key=True)
    label_ar: Mapped[str | None] = mapped_column(String(160), nullable=True)
    muted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )
