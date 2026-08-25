"""API اختياري لـ n8n / MCP: نتائج الخط + رؤى حملات فيسبوك."""
from __future__ import annotations

import json
import os
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.deps import DBSession
from modules.marketing_room.campaign_manager import run_campaign_review
from modules.marketing_room.models import MarketingArtifact, MarketingRun
from modules.marketing_room.pipeline import _normalize_bundle, _persist_artifacts
from modules.marketing_room.service import MarketingRoomError

router = APIRouter(prefix="/api/marketing-room", tags=["marketing-room-api"])


def _require_key(x_api_key: str | None) -> None:
    expected = (os.environ.get("MARKETING_AGENT_API_KEY") or "").strip()
    if not expected:
        raise HTTPException(503, "MARKETING_AGENT_API_KEY غير مضبوط")
    if not x_api_key or x_api_key.strip() != expected:
        raise HTTPException(401, "مفتاح غير صالح")


class PipelineResultIn(BaseModel):
    run_id: int
    site_brief: str = ""
    sale_ideas: list[str] = Field(default_factory=list)
    posts: list[dict] = Field(default_factory=list)
    hashtags: list[str] = Field(default_factory=list)
    design_brief: str = ""
    chief_summary: str = ""


@router.get("/health")
def health():
    return {"ok": True, "service": "marketing-room"}


@router.post("/runs/{run_id}/result")
def ingest_pipeline_result(
    run_id: int,
    body: PipelineResultIn,
    db: DBSession,
    x_marketing_api_key: str | None = Header(default=None, alias="X-Marketing-API-Key"),
):
    _require_key(x_marketing_api_key)
    run = db.get(MarketingRun, int(run_id))
    if run is None:
        raise HTTPException(404, "التشغيل غير موجود")
    if body.run_id and int(body.run_id) != int(run_id):
        raise HTTPException(400, "run_id غير متطابق")

    # امسح آثار سابقة إن أُعيد الإرسال
    old = list(
        db.scalars(select(MarketingArtifact).where(MarketingArtifact.run_id == run.id)).all()
    )
    for a in old:
        db.delete(a)
    db.flush()

    ctx = {}
    if run.context_json:
        try:
            ctx = json.loads(run.context_json)
        except json.JSONDecodeError:
            ctx = {}
    bundle = _normalize_bundle(body.model_dump(), ctx)
    _persist_artifacts(db, run, bundle)
    run.status = "awaiting_approval"
    db.commit()
    return {"ok": True, "run_id": run.id, "status": run.status}


class CampaignInsightsIn(BaseModel):
    """حمولة من MCP أو n8n — قائمة حملات/رؤى."""

    campaigns: list[dict[str, Any]] = Field(default_factory=list)
    notes: str = ""
    source: str = "mcp"
    date_preset: str = "last_7d"


@router.post("/campaigns/insights")
def ingest_campaign_insights(
    body: CampaignInsightsIn,
    db: DBSession,
    x_marketing_api_key: str | None = Header(default=None, alias="X-Marketing-API-Key"),
):
    """يستقبل رؤى الحملات من MCP/n8n ويشغّل التحليل + توجيه الوكلاء."""
    _require_key(x_marketing_api_key)
    if not body.campaigns:
        raise HTTPException(400, "campaigns فارغة")
    try:
        run = run_campaign_review(
            db,
            user_id=None,
            campaigns=body.campaigns,
            source=body.source or "mcp",
            notes=body.notes or "",
            date_preset=body.date_preset or "last_7d",
        )
        run_id = run.id
        n_dir = len(
            list(
                db.scalars(
                    select(MarketingArtifact).where(
                        MarketingArtifact.run_id == run_id,
                        MarketingArtifact.kind == "campaign_directive",
                    )
                ).all()
            )
        )
        status = run.status
        db.commit()
    except MarketingRoomError as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc
    return {
        "ok": True,
        "run_id": run_id,
        "status": status,
        "directives": n_dir,
    }
