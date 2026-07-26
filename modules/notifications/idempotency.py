from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.notifications.events import (
    DEFAULT_THROTTLE_MINUTES,
    INVENTORY_PURCHASE_NEEDED,
    INVENTORY_PURCHASE_RECEIVED,
    REFERRAL_PRODUCT_SHARED,
)
from modules.notifications.models import NotificationLog, NotificationLogStatus


def build_idempotency_key(
    event_key: str,
    source_type: str,
    source_id: int | None,
    recipient_type: str,
    payload: dict[str, Any],
    *,
    extra: str = "",
) -> str:
    parts = [event_key, source_type, str(source_id or 0), recipient_type]
    if event_key.startswith("inventory."):
        if event_key == INVENTORY_PURCHASE_NEEDED:
            day = date.today().isoformat()
            parts = [event_key, day, recipient_type]
        elif event_key == INVENTORY_PURCHASE_RECEIVED:
            sid = source_id or payload.get("purchase_id") or payload.get("purchaseId") or 0
            parts = [event_key, source_type or "purchase", str(sid), recipient_type]
        elif source_type == "inventory_lot":
            parts = [event_key, str(source_id or 0), date.today().isoformat(), recipient_type]
        else:
            pid = payload.get("product_id") or payload.get("productId")
            wid = payload.get("warehouse_id") or payload.get("warehouseId")
            day = date.today().isoformat()
            parts = [event_key, str(pid or 0), str(wid or 0), day]
    elif event_key.startswith("order."):
        parts = [event_key, str(source_id or payload.get("order_id") or payload.get("sale_id") or 0)]
    elif event_key.startswith("pos."):
        parts = [event_key, str(source_id or payload.get("shift_id") or 0)]
    elif event_key.startswith("hr."):
        parts = [
            event_key,
            str(source_id or payload.get("employee_id") or payload.get("payroll_entry_id") or 0),
        ]
    elif event_key.startswith("hotel."):
        if event_key in (
            "hotel.checkout_reminder",
            "hotel.night_payment_due",
            "hotel.balance_claim",
        ):
            parts = [event_key, str(source_id or payload.get("booking_id") or 0), date.today().isoformat()]
        elif event_key == "hotel.room_cleaning":
            parts = [
                event_key,
                str(source_id or payload.get("room_id") or 0),
                str(payload.get("task_token") or date.today().isoformat()),
            ]
        elif event_key == "hotel.payment_received":
            # كل دفعة إيصال مستقل — لا نمنع الإرسال بعد أول سداد على نفس الحجز
            pay_id = source_id or payload.get("payment_id") or 0
            parts = [
                event_key,
                str(payload.get("booking_id") or 0),
                str(pay_id),
                str(payload.get("payment_amount") or ""),
            ]
        else:
            parts = [event_key, str(source_id or payload.get("booking_id") or 0)]
    elif event_key == REFERRAL_PRODUCT_SHARED:
        parts = [
            event_key,
            str(payload.get("customer_id") or 0),
            str(payload.get("product_id") or 0),
            str(payload.get("share_nonce") or source_id or 0),
        ]
    elif event_key.startswith(("referral.", "kitchen.", "loyalty.account")):
        parts = [event_key, str(source_id or 0), recipient_type]
    if extra:
        parts.append(extra)
    return ":".join(parts)[:200]


def default_throttle_minutes(event_key: str, rule_throttle: int | None) -> int | None:
    if rule_throttle is not None:
        return rule_throttle
    return DEFAULT_THROTTLE_MINUTES.get(event_key)


def is_throttled(
    db: Session,
    *,
    idempotency_key: str,
    throttle_minutes: int | None,
) -> bool:
    if not throttle_minutes or throttle_minutes <= 0:
        return False
    row = db.scalar(
        select(NotificationLog)
        .where(
            NotificationLog.idempotency_key == idempotency_key,
            NotificationLog.status.in_(
                [NotificationLogStatus.SENT.value, NotificationLogStatus.QUEUED.value]
            ),
        )
        .order_by(NotificationLog.created_at.desc())
        .limit(1)
    )
    if row is None:
        return False
    ts = row.sent_at or row.created_at
    if ts is None:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age_min = (datetime.now(timezone.utc) - ts).total_seconds() / 60.0
    return age_min < throttle_minutes


def log_exists(db: Session, idempotency_key: str, *, include_skipped: bool = False) -> bool:
    conditions = [NotificationLog.idempotency_key == idempotency_key]
    if not include_skipped:
        conditions.append(NotificationLog.status != NotificationLogStatus.SKIPPED.value)
    row = db.scalar(
        select(NotificationLog.id)
        .where(*conditions)
        .limit(1)
    )
    return row is not None
