"""عدادات المهام المعلّقة — تُحدَّث فقط عند إنجاز الإجراء وليس عند زيارة الصفحة."""

from __future__ import annotations

import time
from collections.abc import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from modules.dashboard_notify.constants import (
    HOTEL_BOOKINGS,
    HOTEL_HOUSEKEEPING,
    HOTEL_SETTLE,
    INVENTORY,
    KDS,
    MESSAGING_CHAT_ORDERS,
    MESSAGING_INBOX,
    PRINTING,
)

_PENDING_CACHE: dict[str, tuple[float, int]] = {}
_PENDING_TTL_SEC = 60.0


def _cached_count(key: str, fn: Callable[[], int], *, ttl: float = _PENDING_TTL_SEC) -> int:
    now = time.monotonic()
    hit = _PENDING_CACHE.get(key)
    if hit is not None and (now - hit[0]) < ttl:
        return hit[1]
    try:
        n = int(fn())
    except Exception:
        n = 0
    _PENDING_CACHE[key] = (now, n)
    return n


def invalidate_pending_counts(*keys: str) -> None:
    """مسح كاش العدادات بعد إجراء يغيّر الحالة (تنظيف غرفة، …)."""
    if not keys:
        _PENDING_CACHE.clear()
        return
    for key in keys:
        _PENDING_CACHE.pop(key, None)


def pending_inventory_low_stock_count(db: Session) -> int:
    """أصناف تحت حد إعادة الطلب — تبقى حتى يُعاد التخزين."""

    def _compute() -> int:
        from modules.inventory.service import low_stock_by_warehouse

        return sum(len(rows) for _wh, rows in low_stock_by_warehouse(db))

    return _cached_count("inventory_low_stock", _compute, ttl=120.0)


def pending_hotel_settle_count(db: Session) -> int:
    """عدد الغرف التي عليها رسوم غير مسوّاة (تقريبي سريع للشارة — بدون N+1)."""

    def _compute() -> int:
        from modules.hotel.models import RoomCharge

        n = db.scalar(
            select(func.count(func.distinct(RoomCharge.room_id))).where(
                RoomCharge.is_settled.is_(False)
            )
        )
        return int(n or 0)

    return _cached_count("hotel_settle", _compute, ttl=90.0)


def pending_hotel_bookings_count(db: Session) -> int:
    """تنبيه تشغيلي: مغادرة خلال 24 ساعة أو تذكير متابعة مستحق."""

    def _compute() -> int:
        from modules.hotel.follow_up import follow_ups_due_count

        return int(follow_ups_due_count(db) or 0)

    return _cached_count("hotel_bookings", _compute, ttl=60.0)


def pending_hotel_housekeeping_count(db: Session) -> int:
    """شقق بانتظار تنظيف أو صيانة — تنبيه التنظيف يبقى حتى اكتمال التنظيف."""

    def _compute() -> int:
        from modules.hotel.booking_models import RoomPhysicalStatus
        from modules.hotel.models import HotelRoom

        n = db.scalar(
            select(func.count())
            .select_from(HotelRoom)
            .where(
                HotelRoom.is_active.is_(True),
                HotelRoom.physical_status.in_(
                    [
                        RoomPhysicalStatus.DIRTY,
                        RoomPhysicalStatus.CLEANING,
                        RoomPhysicalStatus.MAINTENANCE,
                    ]
                ),
            )
        )
        return int(n or 0)

    return _cached_count("hotel_housekeeping", _compute, ttl=60.0)


def pending_hotel_cleaning_count(db: Session) -> int:
    """شقق تحتاج تنظيفاً (للتنظيف / قيد التنظيف) — لا يُزال إلا بعد التنظيف."""

    def _compute() -> int:
        from modules.hotel.booking_models import RoomPhysicalStatus
        from modules.hotel.models import HotelRoom

        n = db.scalar(
            select(func.count())
            .select_from(HotelRoom)
            .where(
                HotelRoom.is_active.is_(True),
                HotelRoom.physical_status.in_(
                    [
                        RoomPhysicalStatus.DIRTY,
                        RoomPhysicalStatus.CLEANING,
                    ]
                ),
            )
        )
        return int(n or 0)

    return _cached_count("hotel_cleaning", _compute, ttl=45.0)


def pending_kds_count(db: Session) -> int:
    """عدد تذاكر المطبخ غير المُنجَزة (قيد الانتظار / التحضير / جاهزة)."""

    def _compute() -> int:
        from modules.kds.service import kds_open_counts

        return int(kds_open_counts(db)["total"])

    return _cached_count("kds", _compute, ttl=30.0)


def pending_printing_count(db: Session) -> int:
    """مهام طباعة معلّقة + وكلاء غير متصلين (يظهر في شارة النشاط)."""

    def _compute() -> int:
        from modules.printing.models import PrintJob, PrintJobStatus
        from modules.printing.service import count_offline_print_agents

        n = db.scalar(
            select(func.count(PrintJob.id)).where(
                PrintJob.status.in_(
                    [
                        PrintJobStatus.PENDING.value,
                        PrintJobStatus.CLAIMED.value,
                    ]
                )
            )
        )
        offline = 0
        try:
            offline = count_offline_print_agents(db, older_than_seconds=90)
        except Exception:  # noqa: BLE001
            offline = 0
        return int(n or 0) + int(offline)

    return _cached_count("printing", _compute, ttl=30.0)


def pending_messaging_inbox_count(db: Session) -> int:
    """محادثات مفتوحة آخر رسالة فيها من العميل — تبقى حتى الرد."""

    def _compute() -> int:
        from modules.messaging.inbox_service import inbox_pending_response_count

        return int(inbox_pending_response_count(db))

    return _cached_count("messaging_inbox", _compute, ttl=45.0)


def pending_messaging_chat_orders_count(db: Session) -> int:
    """طلبات محادثة الويب بانتظار تأكيد الكاشير."""

    def _compute() -> int:
        from modules.messaging.admin_nav import pending_chat_orders_count

        return int(pending_chat_orders_count(db))

    return _cached_count("messaging_chat_orders", _compute, ttl=30.0)


# أقسام يُحسب شارتها من حالة النظام الحالية (إجراء مطلوب)
LIVE_PENDING_SECTIONS: dict[str, object] = {
    INVENTORY: pending_inventory_low_stock_count,
    HOTEL_BOOKINGS: pending_hotel_bookings_count,
    HOTEL_HOUSEKEEPING: pending_hotel_housekeeping_count,
    HOTEL_SETTLE: pending_hotel_settle_count,
    KDS: pending_kds_count,
    PRINTING: pending_printing_count,
    MESSAGING_INBOX: pending_messaging_inbox_count,
    MESSAGING_CHAT_ORDERS: pending_messaging_chat_orders_count,
}
