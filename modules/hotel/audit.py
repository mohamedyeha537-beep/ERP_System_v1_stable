"""سجل تدقيق الفندق."""
from __future__ import annotations

from sqlalchemy.orm import Session

from modules.hotel.booking_models import HotelAuditLog


def log_audit(
    db: Session,
    *,
    entity_type: str,
    entity_id: int,
    action: str,
    field_name: str | None = None,
    old_value: str | None = None,
    new_value: str | None = None,
    reason: str | None = None,
    user_id: int | None = None,
) -> HotelAuditLog:
    row = HotelAuditLog(
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        field_name=field_name,
        old_value=old_value,
        new_value=new_value,
        reason=reason,
        user_id=user_id,
    )
    db.add(row)
    db.flush()
    return row
