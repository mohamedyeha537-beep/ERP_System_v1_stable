"""تفضيلات مركز الإشعارات: مقروء / حذف / كتم أنواع."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.dashboard_notify.models import ActivityHubItemState, ActivityHubMute


def item_key_activity(activity_id: int) -> str:
    return f"activity:{int(activity_id)}"


def item_key_event(event_id: int) -> str:
    return f"event:{int(event_id)}"


def item_key_pending(section_key: str) -> str:
    return f"pending:{(section_key or '').strip()}"


def mute_key_section(section_key: str) -> str:
    return f"section:{(section_key or '').strip()}"


def mute_key_event_type(event_key: str) -> str:
    return f"event_type:{(event_key or '').strip()}"


def mute_key_group(group_id: str) -> str:
    return f"group:{(group_id or '').strip()}"


def load_item_states(db: Session, user_id: int) -> dict[str, str]:
    rows = db.scalars(
        select(ActivityHubItemState).where(ActivityHubItemState.user_id == user_id)
    ).all()
    return {r.item_key: r.state for r in rows}


def load_mutes(db: Session, user_id: int) -> dict[str, ActivityHubMute]:
    rows = db.scalars(
        select(ActivityHubMute).where(ActivityHubMute.user_id == user_id)
    ).all()
    return {r.mute_key: r for r in rows}


def set_item_state(db: Session, user_id: int, item_key: str, state: str) -> None:
    key = (item_key or "").strip()[:96]
    if not key or state not in ("read", "deleted"):
        return
    now = datetime.now(timezone.utc)
    row = db.get(ActivityHubItemState, {"user_id": user_id, "item_key": key})
    if row is None:
        db.add(
            ActivityHubItemState(
                user_id=user_id,
                item_key=key,
                state=state,
                updated_at=now,
            )
        )
    else:
        row.state = state
        row.updated_at = now
    db.flush()


def clear_item_state(db: Session, user_id: int, item_key: str) -> None:
    """إلغاء الحذف/القراءة — إعادة إظهار كجديد."""
    key = (item_key or "").strip()[:96]
    row = db.get(ActivityHubItemState, {"user_id": user_id, "item_key": key})
    if row is not None:
        db.delete(row)
        db.flush()


def add_mute(db: Session, user_id: int, mute_key: str, label_ar: str | None = None) -> None:
    key = (mute_key or "").strip()[:96]
    if not key:
        return
    row = db.get(ActivityHubMute, {"user_id": user_id, "mute_key": key})
    if row is None:
        db.add(
            ActivityHubMute(
                user_id=user_id,
                mute_key=key,
                label_ar=(label_ar or "")[:160] or None,
                muted_at=datetime.now(timezone.utc),
            )
        )
    else:
        row.label_ar = (label_ar or row.label_ar or "")[:160] or None
        row.muted_at = datetime.now(timezone.utc)
    db.flush()


def remove_mute(db: Session, user_id: int, mute_key: str) -> None:
    key = (mute_key or "").strip()[:96]
    row = db.get(ActivityHubMute, {"user_id": user_id, "mute_key": key})
    if row is not None:
        db.delete(row)
        db.flush()


def list_mutes(db: Session, user_id: int) -> list[ActivityHubMute]:
    return list(
        db.scalars(
            select(ActivityHubMute)
            .where(ActivityHubMute.user_id == user_id)
            .order_by(ActivityHubMute.muted_at.desc())
        ).all()
    )


def is_muted(
    mutes: dict[str, ActivityHubMute],
    *,
    section_key: str | None = None,
    event_key: str | None = None,
    group_id: str | None = None,
) -> bool:
    if section_key and mute_key_section(section_key) in mutes:
        return True
    if event_key and mute_key_event_type(event_key) in mutes:
        return True
    if group_id and mute_key_group(group_id) in mutes:
        return True
    return False
