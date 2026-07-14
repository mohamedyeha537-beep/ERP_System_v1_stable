"""تسجيل وتحليل زيارات الويب العام."""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.web_marketing.models import WebAnalyticsEvent
from modules.web_marketing.service import SURFACES

EVENT_PAGEVIEW = "pageview"
EVENT_SEARCH = "search"
EVENT_ADD_TO_CART = "add_to_cart"
EVENT_BEGIN_CHECKOUT = "begin_checkout"
EVENT_PURCHASE = "purchase"
EVENT_BOOKING = "booking"
EVENT_ROOM_ORDER = "room_order"

ALLOWED_EVENTS: frozenset[str] = frozenset(
    {
        EVENT_PAGEVIEW,
        EVENT_SEARCH,
        EVENT_ADD_TO_CART,
        EVENT_BEGIN_CHECKOUT,
        EVENT_PURCHASE,
        EVENT_BOOKING,
        EVENT_ROOM_ORDER,
    }
)

_CONVERSION_EVENTS: frozenset[str] = frozenset(
    {EVENT_PURCHASE, EVENT_BOOKING, EVENT_ROOM_ORDER}
)

_SESSION_RE = re.compile(r"^[a-f0-9]{8,64}$", re.I)
_BOT_RE = re.compile(r"bot|crawl|spider|slurp|facebookexternalhit", re.I)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _clean_path(raw: str) -> str:
    path = (raw or "/").strip()
    if not path.startswith("/"):
        path = "/" + path
    return path[:255]


def _clean_session(raw: str) -> str:
    val = (raw or "").strip().lower()
    if not val or not _SESSION_RE.match(val):
        return ""
    return val[:64]


def _is_bot(user_agent: str | None) -> bool:
    ua = (user_agent or "").strip()
    return bool(ua and _BOT_RE.search(ua))


def _conversion_exists(
    db: Session,
    surface: str,
    event_type: str,
    id_key: str,
    id_val: Any,
) -> bool:
    if id_val is None or id_val == "":
        return False
    since = _utcnow() - timedelta(hours=48)
    rows = db.scalars(
        select(WebAnalyticsEvent.meta_json).where(
            WebAnalyticsEvent.surface == surface,
            WebAnalyticsEvent.event_type == event_type,
            WebAnalyticsEvent.created_at >= since,
        )
    ).all()
    target = str(id_val)
    for meta_text in rows:
        try:
            obj = json.loads(meta_text or "{}")
        except json.JSONDecodeError:
            continue
        if str(obj.get(id_key)) == target:
            return True
    return False


def record_event(
    db: Session,
    *,
    surface: str,
    event_type: str,
    page_path: str = "/",
    session_id: str = "",
    referrer: str | None = None,
    meta: dict[str, Any] | None = None,
    user_agent: str | None = None,
    dedupe_seconds: int | None = None,
) -> bool:
    """يُسجّل حدثاً — يُرجع False إذا تُخطّى (بوت أو مكرّر)."""
    if surface not in SURFACES:
        return False
    ev = (event_type or "").strip().lower()
    if ev not in ALLOWED_EVENTS:
        return False
    if _is_bot(user_agent):
        return False

    sid = _clean_session(session_id)
    path = _clean_path(page_path)
    ref = (referrer or "").strip()[:500] or None
    meta_obj = meta if isinstance(meta, dict) else {}
    if ev in _CONVERSION_EVENTS:
        for key in ("sale_id", "booking_id"):
            if key in meta_obj and _conversion_exists(db, surface, ev, key, meta_obj[key]):
                return False
    try:
        meta_text = json.dumps(meta_obj, ensure_ascii=False, separators=(",", ":"))[:4000]
    except (TypeError, ValueError):
        meta_text = "{}"

    if dedupe_seconds and sid:
        since = _utcnow() - timedelta(seconds=dedupe_seconds)
        dup = db.scalar(
            select(WebAnalyticsEvent.id)
            .where(
                WebAnalyticsEvent.surface == surface,
                WebAnalyticsEvent.event_type == ev,
                WebAnalyticsEvent.page_path == path,
                WebAnalyticsEvent.session_id == sid,
                WebAnalyticsEvent.created_at >= since,
            )
            .limit(1)
        )
        if dup:
            return False

    db.add(
        WebAnalyticsEvent(
            surface=surface,
            event_type=ev,
            page_path=path,
            session_id=sid,
            referrer=ref,
            meta_json=meta_text,
        )
    )
    return True


def record_purchase(
    db: Session,
    *,
    sale_id: int | None,
    value: str | float | None,
    session_id: str = "",
    page_path: str = "/shop",
) -> None:
    meta: dict[str, Any] = {}
    if sale_id:
        meta["sale_id"] = int(sale_id)
    if value is not None:
        meta["value"] = str(value)
    meta["currency"] = "LYD"
    record_event(
        db,
        surface="restaurant_shop",
        event_type=EVENT_PURCHASE,
        page_path=page_path,
        session_id=session_id,
        meta=meta,
        dedupe_seconds=120,
    )


def record_booking(
    db: Session,
    *,
    booking_id: int | None,
    reference: str | None = None,
    value: str | float | None = None,
    session_id: str = "",
    page_path: str = "/stay/book",
) -> None:
    meta: dict[str, Any] = {}
    if booking_id:
        meta["booking_id"] = int(booking_id)
    if reference:
        meta["reference"] = str(reference)[:80]
    if value is not None:
        meta["value"] = str(value)
    meta["currency"] = "LYD"
    record_event(
        db,
        surface="hotel_portal",
        event_type=EVENT_BOOKING,
        page_path=page_path,
        session_id=session_id,
        meta=meta,
        dedupe_seconds=120,
    )


def record_room_order(
    db: Session,
    *,
    sale_id: int | None,
    product_id: int | None = None,
    value: str | float | None = None,
    session_id: str = "",
    page_path: str = "/stay",
) -> None:
    meta: dict[str, Any] = {}
    if sale_id:
        meta["sale_id"] = int(sale_id)
    if product_id:
        meta["product_id"] = int(product_id)
    if value is not None:
        meta["value"] = str(value)
    meta["currency"] = "LYD"
    record_event(
        db,
        surface="hotel_portal",
        event_type=EVENT_ROOM_ORDER,
        page_path=page_path,
        session_id=session_id,
        meta=meta,
        dedupe_seconds=60,
    )


def _day_key(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).date().isoformat()


def get_analytics_dashboard(
    db: Session,
    surface: str,
    *,
    days: int = 30,
) -> dict[str, Any]:
    if surface not in SURFACES:
        raise ValueError("surface unknown")
    days = max(1, min(int(days or 30), 365))
    since = _utcnow() - timedelta(days=days)

    rows = db.execute(
        select(
            WebAnalyticsEvent.event_type,
            WebAnalyticsEvent.page_path,
            WebAnalyticsEvent.session_id,
            WebAnalyticsEvent.created_at,
        ).where(
            WebAnalyticsEvent.surface == surface,
            WebAnalyticsEvent.created_at >= since,
        )
    ).all()

    pageviews = 0
    unique_sessions: set[str] = set()
    conversions = 0
    by_event: dict[str, int] = {}
    by_day: dict[str, int] = {}
    by_page: dict[str, int] = {}

    for ev_type, page_path, session_id, created_at in rows:
        et = ev_type or ""
        by_event[et] = by_event.get(et, 0) + 1
        if et == EVENT_PAGEVIEW:
            pageviews += 1
            day = _day_key(created_at)
            by_day[day] = by_day.get(day, 0) + 1
            pp = (page_path or "/")[:120]
            by_page[pp] = by_page.get(pp, 0) + 1
            if session_id:
                unique_sessions.add(session_id)
        if et in _CONVERSION_EVENTS:
            conversions += 1

    # سلسلة يومية كاملة (حتى الأيام بدون زيارات = 0)
    start_day = (_utcnow() - timedelta(days=days - 1)).date()
    daily_series: list[dict[str, Any]] = []
    d = start_day
    end_day = _utcnow().date()
    while d <= end_day:
        key = d.isoformat()
        daily_series.append({"date": key, "views": by_day.get(key, 0)})
        d += timedelta(days=1)

    top_pages = sorted(by_page.items(), key=lambda x: (-x[1], x[0]))[:12]
    event_rows = sorted(by_event.items(), key=lambda x: (-x[1], x[0]))

    conv_rate = round((conversions / pageviews * 100), 2) if pageviews else 0.0

    return {
        "surface": surface,
        "label_ar": SURFACES[surface]["label_ar"],
        "days": days,
        "pageviews": pageviews,
        "unique_visitors": len(unique_sessions),
        "conversions": conversions,
        "conversion_rate_pct": conv_rate,
        "daily_series": daily_series,
        "top_pages": [{"path": p, "views": v} for p, v in top_pages],
        "events_by_type": [{"event": e, "count": c} for e, c in event_rows],
        "event_labels": {
            EVENT_PAGEVIEW: "زيارة صفحة",
            EVENT_SEARCH: "بحث",
            EVENT_ADD_TO_CART: "إضافة للسلة",
            EVENT_BEGIN_CHECKOUT: "بدء الدفع",
            EVENT_PURCHASE: "طلب متجر",
            EVENT_BOOKING: "حجز إقامة",
            EVENT_ROOM_ORDER: "طلب غرفة",
        },
    }
