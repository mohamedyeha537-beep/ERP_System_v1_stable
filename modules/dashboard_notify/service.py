"""تسجيل النشاط، عدّ الشارات، وحلّها عند إنجاز الإجراء (وليس عند زيارة الصفحة)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from modules.dashboard_notify.constants import (
    ASSETS,
    CUSTOMERS,
    EXPENSES,
    HOTEL_SETTLE,
    INFO_ONLY_SECTIONS,
    INVENTORY,
    KDS,
    LOYALTY,
    PATH_TO_SECTION,
    PRINTING,
    PURCHASES,
)
from modules.dashboard_notify.models import DashboardActivity, DashboardSectionSeen
from modules.dashboard_notify.pending import LIVE_PENDING_SECTIONS


def section_for_path(path: str) -> str | None:
    """يربط مسار الطلب بمفتاح قسم لوحة التحكم."""
    p = (path or "").split("?")[0].rstrip("/") or "/"
    for prefix, key in PATH_TO_SECTION:
        if p == prefix or p.startswith(prefix + "/"):
            return key
    return None


def record_activity(
    db: Session,
    section_key: str,
    *,
    event_type: str = "change",
    ref_id: int | None = None,
    note: str | None = None,
) -> None:
    """يُسجَّل بعد تعديل يحتاج متابعة في قسم لا يُحسب من حالة مباشرة."""
    key = (section_key or "").strip()
    if not key:
        return
    if key in LIVE_PENDING_SECTIONS and key != INVENTORY:
        return
    et = (event_type or "change")[:64]
    note_s = (note or "").strip()[:255] or None
    db.add(
        DashboardActivity(
            section_key=key,
            event_type=et,
            ref_id=ref_id,
            note=note_s,
        )
    )
    db.flush()
    try:
        from modules.dashboard_notify.whatsapp_forward import forward_hub_item_to_whatsapp

        title = f"نشاط: {key}"
        detail_parts = [et]
        if ref_id is not None:
            detail_parts.append(f"#{ref_id}")
        if note_s:
            detail_parts.append(note_s)
        forward_hub_item_to_whatsapp(
            db,
            title=title,
            detail=" · ".join(detail_parts),
            event_type=f"hub.activity.{key}"[:64],
            meta={"section_key": key, "ref_id": ref_id, "activity_event": et},
        )
    except Exception:  # noqa: BLE001
        pass


def resolve_activity(
    db: Session,
    section_key: str,
    ref_id: int | None = None,
    *,
    event_type: str | None = None,
) -> int:
    """يُعلِّم الأحداث كمُنجَزة — يُستدعى بعد الإجراء الفعلي."""
    key = (section_key or "").strip()
    if not key:
        return 0
    now = datetime.now(timezone.utc)
    stmt = select(DashboardActivity).where(
        DashboardActivity.section_key == key,
        DashboardActivity.resolved_at.is_(None),
    )
    if ref_id is not None:
        stmt = stmt.where(DashboardActivity.ref_id == ref_id)
    if event_type:
        stmt = stmt.where(DashboardActivity.event_type == event_type)
    rows = list(db.scalars(stmt).all())
    for row in rows:
        row.resolved_at = now
    if rows:
        db.flush()
    return len(rows)


def _last_seen_map(db: Session, user_id: int) -> dict[str, datetime]:
    rows = db.scalars(
        select(DashboardSectionSeen).where(DashboardSectionSeen.user_id == user_id)
    ).all()
    return {r.section_key: r.last_seen_at for r in rows}


def _activity_visible_to_user(
    act: DashboardActivity,
    *,
    section_key: str,
    last_seen: dict[str, datetime],
) -> bool:
    """هل يُحسب هذا الحدث في شارة المستخدم؟"""
    if section_key in INFO_ONLY_SECTIONS:
        seen_at = last_seen.get(section_key)
        if seen_at is not None and act.created_at <= seen_at:
            return False
    return True


def mark_section_seen(db: Session, user_id: int, section_key: str) -> None:
    """يُحدّث «آخر زيارة» — للأقسام الإعلامية يُخفّي الشارة بعد فتح القسم."""
    key = (section_key or "").strip()
    if not key:
        return
    now = datetime.now(timezone.utc)
    row = db.get(DashboardSectionSeen, {"user_id": user_id, "section_key": key})
    if row is None:
        db.add(
            DashboardSectionSeen(
                user_id=user_id,
                section_key=key,
                last_seen_at=now,
            )
        )
    else:
        row.last_seen_at = now
    db.flush()


def mark_seen_for_path(db: Session, user_id: int, path: str) -> None:
    key = section_for_path(path)
    if key and key in INFO_ONLY_SECTIONS:
        mark_section_seen(db, user_id, key)


_SYNC_RESOLUTION_AT = 0.0
_SYNC_RESOLUTION_TTL_SEC = 120.0


def sync_activity_resolution(db: Session, *, force: bool = False) -> None:
    """يُحلّ الأحداث القديمة إذا لم يعد لها إجراء معلّق في النظام."""
    global _SYNC_RESOLUTION_AT
    import time

    now_mono = time.monotonic()
    if not force and (now_mono - _SYNC_RESOLUTION_AT) < _SYNC_RESOLUTION_TTL_SEC:
        return
    _SYNC_RESOLUTION_AT = now_mono
    now = datetime.now(timezone.utc)

    from modules.hotel.models import RoomCharge

    for act in db.scalars(
        select(DashboardActivity).where(
            DashboardActivity.section_key == HOTEL_SETTLE,
            DashboardActivity.resolved_at.is_(None),
            DashboardActivity.ref_id.isnot(None),
        )
    ).all():
        open_n = db.scalar(
            select(func.count(RoomCharge.id)).where(
                RoomCharge.sale_id == act.ref_id,
                RoomCharge.is_settled.is_(False),
            )
        )
        if (open_n or 0) == 0:
            act.resolved_at = now

    from modules.sales.models import KitchenTicket, TicketStatus

    open_statuses = [
        TicketStatus.PENDING,
        TicketStatus.IN_PROGRESS,
        TicketStatus.READY,
    ]
    for act in db.scalars(
        select(DashboardActivity).where(
            DashboardActivity.section_key == KDS,
            DashboardActivity.resolved_at.is_(None),
            DashboardActivity.ref_id.isnot(None),
        )
    ).all():
        open_n = db.scalar(
            select(func.count(KitchenTicket.id)).where(
                KitchenTicket.sale_id == act.ref_id,
                KitchenTicket.status.in_(open_statuses),
            )
        )
        if (open_n or 0) == 0:
            act.resolved_at = now

    from modules.printing.models import PrintJob, PrintJobStatus

    for act in db.scalars(
        select(DashboardActivity).where(
            DashboardActivity.section_key == PRINTING,
            DashboardActivity.resolved_at.is_(None),
            DashboardActivity.ref_id.isnot(None),
        )
    ).all():
        job = db.get(PrintJob, act.ref_id)
        if job is None or job.status not in (
            PrintJobStatus.PENDING.value,
            PrintJobStatus.CLAIMED.value,
        ):
            act.resolved_at = now

    for act in db.scalars(
        select(DashboardActivity).where(
            DashboardActivity.section_key.in_((CUSTOMERS, LOYALTY, EXPENSES)),
            DashboardActivity.resolved_at.is_(None),
        )
    ).all():
        act.resolved_at = now

    from modules.payments.models import Purchase, PurchaseKind
    from modules.payments.service import purchase_outstanding

    for act in db.scalars(
        select(DashboardActivity).where(
            DashboardActivity.section_key.in_((PURCHASES, ASSETS)),
            DashboardActivity.resolved_at.is_(None),
            DashboardActivity.ref_id.isnot(None),
        )
    ).all():
        purchase = db.get(Purchase, act.ref_id)
        if purchase is None:
            act.resolved_at = now
            continue
        if act.section_key == PURCHASES and purchase.kind == PurchaseKind.INVENTORY:
            if purchase_outstanding(db, purchase) <= 0:
                act.resolved_at = now

    db.flush()


def load_purchase_domain_map(db: Session, purchase_ids: set[int]) -> dict[int, str]:
    """خريطة purchase_id → مجال العمل (نص صغير)."""
    if not purchase_ids:
        return {}
    from modules.payments.models import Purchase

    out: dict[int, str] = {}
    for pid, pdom in db.execute(
        select(Purchase.id, Purchase.business_domain).where(
            Purchase.id.in_(purchase_ids)
        )
    ).all():
        if pdom is None:
            continue
        out[int(pid)] = (
            pdom.value if hasattr(pdom, "value") else str(pdom)
        ).strip().lower()
    return out


def activity_matches_finance_domain(
    act: DashboardActivity,
    *,
    domain_vals: list[str] | None,
    purchase_domain_by_id: dict[int, str],
) -> bool:
    """True إن كان النشاط يظهر في مجال العرض الحالي (مطعم/فندق)."""
    if domain_vals is None:
        return True
    if act.section_key not in (PURCHASES, ASSETS, EXPENSES):
        return True
    if act.ref_id is None:
        return False
    pdom = purchase_domain_by_id.get(int(act.ref_id))
    return pdom is not None and pdom in domain_vals


def _activity_badge_counts(
    db: Session,
    user_id: int,
    last_seen: dict[str, datetime],
    *,
    domain=None,
) -> dict[str, int]:
    """أقسام تعتمد على سجل النشاط."""
    from modules.platform.business_domain import purchase_domain_db_values

    sync_activity_resolution(db)
    rows = list(
        db.scalars(
            select(DashboardActivity).where(DashboardActivity.resolved_at.is_(None))
        ).all()
    )
    domain_vals = purchase_domain_db_values(domain)
    purchase_domain_by_id: dict[int, str] = {}
    if domain_vals is not None:
        pids = {
            int(act.ref_id)
            for act in rows
            if act.section_key in (PURCHASES, ASSETS, EXPENSES)
            and act.ref_id is not None
        }
        purchase_domain_by_id = load_purchase_domain_map(db, pids)

    seen: dict[tuple[str, int | str], bool] = {}
    counts: dict[str, int] = {}
    for act in rows:
        sk = act.section_key
        if sk in LIVE_PENDING_SECTIONS:
            continue
        if not activity_matches_finance_domain(
            act,
            domain_vals=domain_vals,
            purchase_domain_by_id=purchase_domain_by_id,
        ):
            continue
        if not _activity_visible_to_user(act, section_key=sk, last_seen=last_seen):
            continue
        dedupe_key = (sk, act.ref_id if act.ref_id is not None else f"id:{act.id}")
        if dedupe_key in seen:
            continue
        seen[dedupe_key] = True
        counts[sk] = counts.get(sk, 0) + 1
    return counts


def _inventory_info_activity_count(
    db: Session, user_id: int, last_seen: dict[str, datetime]
) -> int:
    """حركات مخزون إعلامية — تُمسح بزيارة صفحة المخزون."""
    rows = list(
        db.scalars(
            select(DashboardActivity).where(
                DashboardActivity.section_key == INVENTORY,
                DashboardActivity.resolved_at.is_(None),
            )
        ).all()
    )
    seen: set[int | str] = set()
    n = 0
    for act in rows:
        if not _activity_visible_to_user(act, section_key=INVENTORY, last_seen=last_seen):
            continue
        key = act.ref_id if act.ref_id is not None else f"id:{act.id}"
        if key in seen:
            continue
        seen.add(key)
        n += 1
    return n


def badge_counts(
    db: Session,
    user_id: int,
    *,
    domain=None,
) -> dict[str, int]:
    """عدد المهام المعلّقة لكل قسم — مع تصفية مجال العرض (مطعم/فندق) للمشتريات."""
    last_seen = _last_seen_map(db, user_id)
    counts: dict[str, int] = {}

    for section_key, counter in LIVE_PENDING_SECTIONS.items():
        n = int(counter(db))
        if section_key == INVENTORY:
            n += _inventory_info_activity_count(db, user_id, last_seen)
        if n > 0:
            counts[section_key] = n

    for section_key, n in _activity_badge_counts(
        db, user_id, last_seen, domain=domain
    ).items():
        if n > 0:
            counts[section_key] = counts.get(section_key, 0) + n

    return counts
