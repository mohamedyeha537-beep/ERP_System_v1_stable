"""تحليلات أداء محرك الإشعارات."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from modules.notifications.models import NotificationLog


@dataclass
class EventStatRow:
    event_key: str
    sent: int = 0
    failed: int = 0
    skipped: int = 0
    queued: int = 0
    total: int = 0


@dataclass
class NotificationAnalytics:
    days: int
    total: int = 0
    sent: int = 0
    failed: int = 0
    skipped: int = 0
    queued: int = 0
    success_rate: float = 0.0
    by_event: list[EventStatRow] = field(default_factory=list)
    daily_sent: list[tuple[str, int]] = field(default_factory=list)


def build_notification_analytics(db: Session, *, days: int = 30) -> NotificationAnalytics:
    days = max(1, min(int(days), 365))
    since = datetime.now(timezone.utc) - timedelta(days=days)
    out = NotificationAnalytics(days=days)

    rows = list(
        db.execute(
            select(
                NotificationLog.event_key,
                NotificationLog.status,
                func.count(NotificationLog.id),
            )
            .where(NotificationLog.created_at >= since)
            .group_by(NotificationLog.event_key, NotificationLog.status)
        ).all()
    )
    by_key: dict[str, EventStatRow] = {}
    for event_key, status, cnt in rows:
        n = int(cnt or 0)
        out.total += n
        row = by_key.setdefault(event_key, EventStatRow(event_key=event_key))
        row.total += n
        st = (status or "").lower()
        if st == "sent":
            out.sent += n
            row.sent += n
        elif st == "failed":
            out.failed += n
            row.failed += n
        elif st == "skipped":
            out.skipped += n
            row.skipped += n
        elif st == "queued":
            out.queued += n
            row.queued += n

    attempted = out.sent + out.failed
    out.success_rate = round(100.0 * out.sent / attempted, 1) if attempted else 0.0
    out.by_event = sorted(by_key.values(), key=lambda r: (-r.total, r.event_key))

    day_rows = list(
        db.execute(
            select(
                func.date(NotificationLog.sent_at),
                func.count(NotificationLog.id),
            )
            .where(
                NotificationLog.created_at >= since,
                NotificationLog.status == "sent",
                NotificationLog.sent_at.isnot(None),
            )
            .group_by(func.date(NotificationLog.sent_at))
            .order_by(func.date(NotificationLog.sent_at))
        ).all()
    )
    out.daily_sent = [(str(d or ""), int(c or 0)) for d, c in day_rows if d]
    return out
