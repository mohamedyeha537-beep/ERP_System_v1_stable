"""عدادات المهام المعلّقة — تُحدَّث فقط عند إنجاز الإجراء وليس عند زيارة الصفحة."""

from __future__ import annotations

import time
from collections.abc import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from modules.dashboard_notify.constants import HOTEL_SETTLE, INVENTORY, KDS, MESSAGING_CHAT_ORDERS, MESSAGING_INBOX, PRINTING

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


def pending_inventory_low_stock_count(db: Session) -> int:
    """أصناف تحت حد إعادة الطلب — تبقى حتى يُعاد التخزين."""

    def _compute() -> int:
        from modules.inventory.service import low_stock_by_warehouse

        return sum(len(rows) for _wh, rows in low_stock_by_warehouse(db))

    return _cached_count("inventory_low_stock", _compute, ttl=120.0)


def pending_hotel_settle_count(db: Session) -> int:
    """عدد الغرف التي عليها رصيد مفتوح (تسوية مطلوبة)."""

    def _compute() -> int:
        from modules.hotel.service import rooms_with_open_balance

        return len(rooms_with_open_balance(db))

    return _cached_count("hotel_settle", _compute, ttl=90.0)


def pending_kds_count(db: Session) -> int:
    """عدد تذاكر المطبخ غير المُنجَزة (قيد الانتظار / التحضير / جاهزة)."""

    def _compute() -> int:
        from modules.kds.service import kds_open_counts

        return int(kds_open_counts(db)["total"])

    return _cached_count("kds", _compute, ttl=30.0)


def pending_printing_count(db: Session) -> int:
    """مهام طباعة لم تُنجَز بعد."""

    def _compute() -> int:
        from modules.printing.models import PrintJob, PrintJobStatus

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
        return int(n or 0)

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
    HOTEL_SETTLE: pending_hotel_settle_count,
    KDS: pending_kds_count,
    PRINTING: pending_printing_count,
    MESSAGING_INBOX: pending_messaging_inbox_count,
    MESSAGING_CHAT_ORDERS: pending_messaging_chat_orders_count,
}
