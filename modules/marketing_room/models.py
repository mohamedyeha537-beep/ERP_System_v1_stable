"""جداول غرفة وكلاء التسويق — مرحلة 1 (تاجر واحد / instance)."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from infra.db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MarketingRun(Base):
    """تشغيل يومي لخط الوكلاء (بيع → محتوى → هاشتاج → تصميم)."""

    __tablename__ = "marketing_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    business_domain: Mapped[str] = mapped_column(String(20), default="shared", index=True)
    status: Mapped[str] = mapped_column(
        String(30), default="pending", index=True
    )  # pending|running|awaiting_approval|completed|failed
    title: Mapped[str] = mapped_column(String(200), default="")
    triggered_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    context_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    chief_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    artifacts: Mapped[list["MarketingArtifact"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class MarketingArtifact(Base):
    """مخرج وكيل (فكرة بيع / منشور / هاشتاج / موجز تصميم…)."""

    __tablename__ = "marketing_artifacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("marketing_runs.id", ondelete="CASCADE"), index=True
    )
    agent_role: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(
        String(40), nullable=False, index=True
    )  # site_brief|sale_idea|post|hashtags|design_brief|chief_report
    status: Mapped[str] = mapped_column(
        String(30), default="pending_approval", index=True
    )  # draft|pending_approval|approved|rejected
    title: Mapped[str] = mapped_column(String(240), default="")
    body_text: Mapped[str] = mapped_column(Text, default="")
    meta_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # مسار صورة مولّدة نسبي تحت /static/...
    media_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    reviewed_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    run: Mapped[MarketingRun] = relationship(back_populates="artifacts")


class MarketingAgentLog(Base):
    """سجل بسيط لخطوات الوكلاء (تشخيص / n8n)."""

    __tablename__ = "marketing_agent_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("marketing_runs.id", ondelete="CASCADE"), nullable=True, index=True
    )
    agent_role: Mapped[str] = mapped_column(String(40), default="")
    level: Mapped[str] = mapped_column(String(20), default="info")
    message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
