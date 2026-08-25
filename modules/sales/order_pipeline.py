"""مسار حالة الطلب للكاشير — ربط المطبخ، التقديم، التوصيل، والدفع."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from modules.delivery.models import DeliveryHandoff
from modules.sales.models import (
    ExternalOrderType,
    KitchenTicket,
    Sale,
    SaleContext,
    SaleSource,
    SaleStatus,
    TicketStatus,
)


@dataclass
class PipelineView:
    label: str
    key: str
    hint: str
    is_delivery: bool
    has_driver: bool
    driver_label: str | None


_OPEN_KDS = (
    TicketStatus.PENDING,
    TicketStatus.IN_PROGRESS,
    TicketStatus.READY,
)


def sale_is_delivery(sale: Sale) -> bool:
    return (
        sale.context_type == SaleContext.EXTERNAL
        and sale.external_order_type == ExternalOrderType.DELIVERY
    )


def sale_allows_split_payment(sale: Sale) -> bool:
    """الدفع المقسّم (كاش+مصرف) مسموح للطاولة فقط.

    طلبات التوصيل والأونلاين: وسيلة دفع واحدة فقط لتجنب خلط احتساب أجرة التوصيل.
    """
    if sale_is_delivery(sale):
        return False
    if getattr(sale, "source", None) == SaleSource.ONLINE:
        return False
    return True


def _kitchen_agg(
    db: Session, sale_ids: list[int]
) -> dict[int, dict[str, int]]:
    if not sale_ids:
        return {}
    out: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for sid, status, n in db.execute(
        select(KitchenTicket.sale_id, KitchenTicket.status, func.count())
        .where(KitchenTicket.sale_id.in_(sale_ids))
        .group_by(KitchenTicket.sale_id, KitchenTicket.status)
    ).all():
        key = status.value if hasattr(status, "value") else str(status)
        out[int(sid)][key] = int(n or 0)
    return {k: dict(v) for k, v in out.items()}


def _all_tickets_served(agg: dict[str, int]) -> bool:
    if not agg:
        return False
    total = sum(agg.values())
    served = agg.get(TicketStatus.SERVED.value, 0) + agg.get("SERVED", 0)
    return total > 0 and served >= total


def _kitchen_ready(agg: dict[str, int]) -> bool:
    if not agg:
        return False
    open_n = sum(
        agg.get(s.value, 0) + agg.get(str(s.value), 0) for s in _OPEN_KDS
    )
    return open_n > 0 and all(
        agg.get(TicketStatus.READY.value, 0) + agg.get("READY", 0)
        + agg.get(TicketStatus.SERVED.value, 0) + agg.get("SERVED", 0)
        >= agg.get(k, 0)
        for k in list(agg.keys())
        if k not in ("READY", "SERVED", TicketStatus.READY.value, TicketStatus.SERVED.value)
    ) and (agg.get("READY", 0) + agg.get(TicketStatus.READY.value, 0)) > 0


def _kitchen_in_progress(agg: dict[str, int]) -> bool:
    return (
        agg.get(TicketStatus.IN_PROGRESS.value, 0)
        + agg.get("IN_PROGRESS", 0)
        > 0
    )


def sync_served_from_kds(db: Session, sale: Sale) -> bool:
    """هجين: تلقائي للطاولة/السفري عند اكتمال تذاكر KDS."""
    if sale.status != SaleStatus.DRAFT or sale.served_to_customer_at:
        return False
    if sale_is_delivery(sale):
        return False
    if not sale.sent_to_kitchen_at:
        return False
    agg = _kitchen_agg(db, [sale.id]).get(sale.id, {})
    if not _all_tickets_served(agg):
        return False
    sale.served_to_customer_at = datetime.now(timezone.utc)
    db.flush()
    return True


def ensure_sale_shift(db: Session, sale: Sale, pos_shift_id: int | None) -> None:
    migrate_draft_to_shift(db, sale, pos_shift_id)


def migrate_draft_to_shift(
    db: Session, sale: Sale, pos_shift_id: int | None
) -> bool:
    """نقل مسودة إلى جلسة الكاشير الحالية إن كانت بلا جلسة أو على جلسة مغلقة."""
    if pos_shift_id is None or sale.status != SaleStatus.DRAFT:
        return False
    target = int(pos_shift_id)
    if sale.pos_shift_id == target:
        return False
    if sale.pos_shift_id is None:
        sale.pos_shift_id = target
        db.flush()
        return True
    from modules.pos_shifts.models import PosShift, PosShiftStatus

    old = db.get(PosShift, sale.pos_shift_id)
    if old is None or old.status == PosShiftStatus.CLOSED:
        sale.pos_shift_id = target
        db.flush()
        return True
    return False


def migrate_user_stale_shift_drafts(
    db: Session, *, user_id: int, pos_shift_id: int | None
) -> bool:
    """نقل كل مسودات المستخدم من جلسات مغلقة إلى الجلسة المفتوحة."""
    if pos_shift_id is None:
        return False
    rows = list(
        db.scalars(
            select(Sale).where(
                Sale.source == SaleSource.POS,
                Sale.status == SaleStatus.DRAFT,
                Sale.created_by_id == user_id,
                Sale.pos_shift_id.isnot(None),
                Sale.pos_shift_id != int(pos_shift_id),
            )
        ).all()
    )
    changed = False
    for sale in rows:
        if migrate_draft_to_shift(db, sale, pos_shift_id):
            changed = True
    return changed


def build_pipeline_view(
    db: Session,
    sale: Sale,
    *,
    kitchen_agg: dict[str, int] | None = None,
    handoff: DeliveryHandoff | None = None,
) -> PipelineView:
    is_del = sale_is_delivery(sale)
    driver_lbl = None
    has_driver = False
    if handoff is not None:
        has_driver = True
        driver_lbl = f"{handoff.driver_name} — {handoff.driver_phone}"

    if sale.status == SaleStatus.CANCELLED:
        return PipelineView("ملغي", "cancelled", "—", is_del, has_driver, driver_lbl)

    if sale.status == SaleStatus.COMPLETED:
        return PipelineView("مغلق — منتهي", "completed", "يمكن تعديل وسيلة الدفع", is_del, has_driver, driver_lbl)

    agg = kitchen_agg
    if agg is None:
        agg = _kitchen_agg(db, [sale.id]).get(sale.id, {})

    if not sale.sent_to_kitchen_at:
        return PipelineView(
            "قيد الإدخال — لم يُرسل للتجهيز",
            "entering",
            "أرسل للمطبخ من السلة",
            is_del,
            has_driver,
            driver_lbl,
        )

    if not agg:
        return PipelineView(
            "مُرسَل للتجهيز",
            "sent",
            "بانتظار المطبخ",
            is_del,
            has_driver,
            driver_lbl,
        )

    if _all_tickets_served(agg) and not sale.served_to_customer_at:
        if is_del:
            return PipelineView(
                "جاهز — بانتظار التقديم",
                "ready_serve",
                "اضغط «تم التقديم» أو سلّم للسائق",
                is_del,
                has_driver,
                driver_lbl,
            )
        return PipelineView(
            "جاهز من المطبخ",
            "ready",
            "يمكن التحصيل",
            is_del,
            has_driver,
            driver_lbl,
        )

    if sale.served_to_customer_at:
        if is_del and not has_driver:
            return PipelineView(
                "مُقدَّم — بانتظار تسليم السائق",
                "served_need_driver",
                "سجّل السائق",
                is_del,
                has_driver,
                driver_lbl,
            )
        if is_del and has_driver:
            return PipelineView(
                "مع السائق",
                "with_driver",
                "بانتظار التحصيل",
                is_del,
                has_driver,
                driver_lbl,
            )
        return PipelineView(
            "مُقدَّم — بانتظار التحصيل",
            "served_await_pay",
            "أكمل التحصيل",
            is_del,
            has_driver,
            driver_lbl,
        )

    if _kitchen_in_progress(agg):
        return PipelineView(
            "قيد التجهيز في المطبخ",
            "kitchen_working",
            "المطبخ يجهّز",
            is_del,
            has_driver,
            driver_lbl,
        )

    ready_n = agg.get("READY", 0) + agg.get(TicketStatus.READY.value, 0)
    if ready_n > 0:
        return PipelineView(
            "جاهز من المطبخ",
            "kitchen_ready",
            "بانتظار التقديم أو التحصيل",
            is_del,
            has_driver,
            driver_lbl,
        )

    return PipelineView(
        "مُرسَل — بانتظار المطبخ",
        "sent_pending",
        "تابع في شاشة المطبخ",
        is_del,
        has_driver,
        driver_lbl,
    )
