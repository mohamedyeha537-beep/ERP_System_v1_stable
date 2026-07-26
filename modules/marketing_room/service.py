"""عمليات الموافقة والتصدير لغرفة التسويق."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from modules.marketing_room.models import MarketingArtifact, MarketingRun


class MarketingRoomError(Exception):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def get_run(db: Session, run_id: int) -> MarketingRun | None:
    return db.get(MarketingRun, int(run_id))


def list_recent_runs(db: Session, *, limit: int = 20) -> list[MarketingRun]:
    return list(
        db.scalars(
            select(MarketingRun).order_by(MarketingRun.id.desc()).limit(limit)
        ).all()
    )


def list_run_artifacts(db: Session, run_id: int) -> list[MarketingArtifact]:
    return list(
        db.scalars(
            select(MarketingArtifact)
            .where(MarketingArtifact.run_id == int(run_id))
            .order_by(MarketingArtifact.id.asc())
        ).all()
    )


def room_stats(db: Session) -> dict[str, int]:
    pending = db.scalar(
        select(func.count()).select_from(MarketingArtifact).where(
            MarketingArtifact.status == "pending_approval"
        )
    ) or 0
    approved = db.scalar(
        select(func.count()).select_from(MarketingArtifact).where(
            MarketingArtifact.status == "approved"
        )
    ) or 0
    runs = db.scalar(select(func.count()).select_from(MarketingRun)) or 0
    awaiting_runs = db.scalar(
        select(func.count()).select_from(MarketingRun).where(
            MarketingRun.status == "awaiting_approval"
        )
    ) or 0
    return {
        "pending_artifacts": int(pending),
        "approved_artifacts": int(approved),
        "total_runs": int(runs),
        "awaiting_runs": int(awaiting_runs),
    }


def set_artifact_status(
    db: Session,
    artifact_id: int,
    *,
    status: str,
    user_id: int | None,
    note: str = "",
) -> MarketingArtifact:
    if status not in ("approved", "rejected", "pending_approval"):
        raise MarketingRoomError("حالة غير صالحة.")
    art = db.get(MarketingArtifact, int(artifact_id))
    if art is None:
        raise MarketingRoomError("المسودّة غير موجودة.")
    art.status = status
    art.reviewed_by_id = user_id
    art.reviewed_at = _utcnow()
    art.review_note = (note or "").strip()[:2000] or None
    db.flush()
    _maybe_complete_run(db, int(art.run_id))
    return art


def approve_all_pending(db: Session, run_id: int, *, user_id: int | None) -> int:
    arts = [
        a
        for a in list_run_artifacts(db, run_id)
        if a.status == "pending_approval"
    ]
    now = _utcnow()
    for a in arts:
        a.status = "approved"
        a.reviewed_by_id = user_id
        a.reviewed_at = now
    db.flush()
    _maybe_complete_run(db, run_id)
    return len(arts)


def _maybe_complete_run(db: Session, run_id: int) -> None:
    run = get_run(db, run_id)
    if run is None or run.status not in ("awaiting_approval", "running"):
        return
    arts = list_run_artifacts(db, run_id)
    if not arts:
        return
    if any(a.status == "pending_approval" for a in arts):
        return
    run.status = "completed"
    if run.finished_at is None:
        run.finished_at = _utcnow()
    db.flush()


def export_post_text(db: Session, artifact_id: int) -> str:
    art = db.get(MarketingArtifact, int(artifact_id))
    if art is None:
        raise MarketingRoomError("المسودّة غير موجودة.")
    if art.kind != "post" and art.kind != "hashtags":
        # allow exporting any approved-ish text
        pass
    parts = []
    if art.title:
        parts.append(art.title.strip())
    if art.body_text:
        parts.append(art.body_text.strip())
    # attach hashtags from same run if exporting a post
    if art.kind in ("post", "post_with_image"):
        tag_art = db.scalar(
            select(MarketingArtifact).where(
                MarketingArtifact.run_id == art.run_id,
                MarketingArtifact.kind == "hashtags",
                MarketingArtifact.status.in_(("approved", "pending_approval")),
            )
        )
        if tag_art and tag_art.body_text:
            parts.append(tag_art.body_text.strip())
    return "\n\n".join(p for p in parts if p)
