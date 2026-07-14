"""API عام لتسجيل أحداث التحليلات من المتصفح."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.deps import DBSession
from modules.web_marketing.analytics import record_event

router = APIRouter(prefix="/api/web-analytics", tags=["web-analytics"])


class AnalyticsEventPayload(BaseModel):
    surface: str
    event_type: str
    page_path: str = "/"
    session_id: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)


@router.post("/event")
def track_analytics_event(payload: AnalyticsEventPayload, request: Request, db: DBSession):
    dedupe = 45 if payload.event_type.strip().lower() == "pageview" else None
    ok = record_event(
        db,
        surface=payload.surface,
        event_type=payload.event_type,
        page_path=payload.page_path,
        session_id=payload.session_id,
        referrer=request.headers.get("referer"),
        meta=payload.meta,
        user_agent=request.headers.get("user-agent"),
        dedupe_seconds=dedupe,
    )
    if not ok and payload.event_type.strip().lower() not in (
        "pageview",
        "search",
        "add_to_cart",
        "begin_checkout",
        "purchase",
        "booking",
        "room_order",
    ):
        raise HTTPException(status_code=400, detail="حدث غير معروف")
    if ok:
        db.commit()
    return {"ok": True, "recorded": ok}
