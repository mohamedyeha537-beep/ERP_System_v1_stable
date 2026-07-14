"""أحداث تحليلات الزيارات — متجر المطعم وبوابة الفندق."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from infra.db import Base


class WebAnalyticsEvent(Base):
    __tablename__ = "web_analytics_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    surface: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    page_path: Mapped[str] = mapped_column(String(255), nullable=False, default="/")
    session_id: Mapped[str] = mapped_column(String(64), nullable=False, default="", index=True)
    referrer: Mapped[str | None] = mapped_column(String(500), nullable=True)
    meta_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
