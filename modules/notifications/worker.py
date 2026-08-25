"""عامل خلفية — معالجة أحداث الإشعارات المعلّقة وإعادة المحاولة."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

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
    n += _maybe_hotel_late_checkout_tick(db)
    n += _maybe_hotel_auto_no_show_tick(db)
    n += _maybe_hotel_shift_overdue(db)
    n += _maybe_hotel_daily_close(db)
    n += _maybe_treasury_handoff_pending(db)
    return n


def _maybe_hotel_daily_close(db: Session) -> int:
    """بعد منتصف الليل المحلي: إقفال أمس (وأيام فائتة) تلقائياً."""
    if not get_bool(db, "hotel_daily_close_auto_enabled", True):
        return 0
    from app.datetime_local import now_local

    yesterday = (now_local().date() - timedelta(days=1)).isoformat()
    if (get_setting(db, "hotel_daily_close_last_date") or "") == yesterday:
        return 0
    from modules.hotel.finance_service import process_auto_daily_close

    return process_auto_daily_close(db)


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
    from app.datetime_local import now_local

    today = now_local().date().isoformat()
    if (get_setting(db, "hotel_guest_reminders_last_date") or "") == today:
        return 0
    from modules.notifications.hotel_hooks import emit_hotel_daily_guest_reminders

    count = emit_hotel_daily_guest_reminders(db)
    set_setting(db, "hotel_guest_reminders_last_date", today)
    return count


def _maybe_hotel_late_checkout_tick(db: Session) -> int:
    """كل ~5 دقائق: تنبيه مغادرة حسب الساعة ثم احتساب ليلة تلقائي."""
    from modules.hotel.late_checkout import (
        SETTING_TICK_AT,
        TICK_INTERVAL_SECONDS,
        late_checkout_policy,
        process_late_checkout_tick,
    )

    if not late_checkout_policy(db).enabled:
        return 0
    raw = (get_setting(db, SETTING_TICK_AT) or "").strip()
    now = datetime.now(timezone.utc)
    if raw:
        try:
            last = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if (now - last).total_seconds() < TICK_INTERVAL_SECONDS:
                return 0
        except ValueError:
            pass
    count = process_late_checkout_tick(db)
    set_setting(db, SETTING_TICK_AT, now.isoformat())
    try:
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
    return count


def _maybe_hotel_auto_no_show_tick(db: Session) -> int:
    """كل ~5 دقائق: تحويل الحجوزات التي لم تصل إلى No Show."""
    from modules.hotel.cancellation_flow import (
        SETTING_AUTO_NO_SHOW,
        SETTING_TICK_AT,
        TICK_INTERVAL_SECONDS,
        process_auto_no_show_tick,
    )

    if not get_bool(db, SETTING_AUTO_NO_SHOW, True):
        return 0
    raw = (get_setting(db, SETTING_TICK_AT) or "").strip()
    now = datetime.now(timezone.utc)
    if raw:
        try:
            last = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if (now - last).total_seconds() < TICK_INTERVAL_SECONDS:
                return 0
        except ValueError:
            pass
    count = process_auto_no_show_tick(db)
    set_setting(db, SETTING_TICK_AT, now.isoformat())
    try:
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
    return count


def _maybe_hotel_shift_overdue(db: Session) -> int:
    from modules.notifications.hotel_shift_hooks import scan_overdue_hotel_shifts

    return scan_overdue_hotel_shifts(db)


def _maybe_treasury_handoff_pending(db: Session) -> int:
    from modules.notifications.treasury_hooks import scan_pending_treasury_handoffs

    return scan_pending_treasury_handoffs(db)
