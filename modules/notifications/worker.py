"""عامل خلفية — معالجة أحداث الإشعارات المعلّقة وإعادة المحاولة."""
from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from modules.notifications.service import NotificationService
from modules.settings.service import get_bool, get_setting, set_setting


def run_cycle(db: Session, *, event_limit: int = 20, retry_limit: int = 25) -> int:
    if not get_bool(db, "notifications_enabled", True):
        return 0
    n = NotificationService.process_pending_events(db, limit=event_limit)
    n += NotificationService.retry_failed(db, limit=retry_limit)
    n += _maybe_inventory_digest(db)
    n += _maybe_expiry_scan(db)
    n += _maybe_hotel_guest_reminders(db)
    n += _maybe_hotel_shift_overdue(db)
    return n


def _maybe_inventory_digest(db: Session) -> int:
    if not get_bool(db, "inventory_digest_enabled", False):
        return 0
    today = date.today().isoformat()
    if (get_setting(db, "inventory_digest_last_date") or "") == today:
        return 0
    from modules.notifications.inventory_hooks import emit_purchase_needed_digest

    if emit_purchase_needed_digest(db):
        set_setting(db, "inventory_digest_last_date", today)
        return 1
    return 0


def _maybe_expiry_scan(db: Session) -> int:
    if not get_bool(db, "inventory_expiry_scan_enabled", True):
        return 0
    from modules.notifications.inventory_hooks import emit_expiry_scan

    return emit_expiry_scan(db)


def _maybe_hotel_guest_reminders(db: Session) -> int:
    if not get_bool(db, "hotel_guest_reminders_enabled", True):
        return 0
    today = date.today().isoformat()
    if (get_setting(db, "hotel_guest_reminders_last_date") or "") == today:
        return 0
    from modules.notifications.hotel_hooks import emit_hotel_daily_guest_reminders

    count = emit_hotel_daily_guest_reminders(db)
    set_setting(db, "hotel_guest_reminders_last_date", today)
    return count


def _maybe_hotel_shift_overdue(db: Session) -> int:
    from modules.notifications.hotel_shift_hooks import scan_overdue_hotel_shifts

    return scan_overdue_hotel_shifts(db)
