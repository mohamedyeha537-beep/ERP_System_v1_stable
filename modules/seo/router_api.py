"""API وكلاء SEO الخارجيين (n8n) — محمي بمفتاح API + مسارات اعتماد/تطبيق للجلسة."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.deps import DBSession, require_permission
from modules.authz.models import User
from modules.authz.permissions import (
    SEO_APPLY,
    SEO_APPROVE,
    SEO_PRODUCTION_APPROVE,
    SEO_ROLLBACK,
)
from modules.seo.apply_service import (
    SeoApplyError,
    approve_fix,
    approve_fix_for_production,
    apply_fix,
    rollback_fix,
)
from modules.seo.auth import require_seo_agent_headers
from modules.seo.config import seo_config
from modules.seo.logging_service import log_seo_action
from modules.seo.models import (
    SeoCrawlRun,
    SeoFixRequest,
    SeoIssue,
    SeoPage,
    SeoReport,
    SeoSuggestion,
)
from modules.seo.public_pages import get_public_page_detail, list_public_pages

router = APIRouter(prefix="/api/seo", tags=["seo-agent"])


def _parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


class CrawlRunIn(BaseModel):
    id: int | None = None
    agent_name: str = "technical"
    status: str = "running"
    pages_scanned: int = 0
    issues_found: int = 0
    error_message: str | None = None
    raw_summary: dict[str, Any] | list[Any] | None = None
    finished: bool = False


class IssueIn(BaseModel):
    crawl_run_id: int | None = None
    page_id: int | None = None
    page_url: str | None = None
    page_type: str | None = None
    entity_type: str | None = None
    entity_id: int | None = None
    severity: str = "medium"
    issue_type: str
    title: str
    description: str | None = None
    current_value: str | None = None
    recommended_value: str | None = None
    status: str = "open"


class IssuesBatchIn(BaseModel):
    issues: list[IssueIn] = Field(default_factory=list)


class SuggestionIn(BaseModel):
    page_id: int | None = None
    page_url: str | None = None
    page_type: str | None = None
    entity_type: str | None = None
    entity_id: int | None = None
    agent_name: str = "content"
    suggestion_type: str
    current_value: str | None = None
    suggested_value: str | None = None
    reason: str | None = None
    confidence_score: float | None = None
    create_fix: bool = True


class SuggestionsBatchIn(BaseModel):
    suggestions: list[SuggestionIn] = Field(default_factory=list)


class ReportIn(BaseModel):
    report_type: str
    title: str = ""
    summary: str | None = None
    period_start: str | None = None
    period_end: str | None = None
    data: dict[str, Any] | list[Any] | None = None


def _upsert_page(
    db,
    *,
    environment: str,
    page_id: int | None,
    page_url: str | None,
    page_type: str | None,
    entity_type: str | None,
    entity_id: int | None,
) -> SeoPage | None:
    if page_id:
        row = db.get(SeoPage, page_id)
        if row:
            return row
    if page_url:
        existing = db.scalars(
            select(SeoPage).where(
                SeoPage.environment == environment,
                SeoPage.url == page_url,
            )
        ).first()
        if existing:
            return existing
        row = SeoPage(
            environment=environment,
            page_type=page_type or "unknown",
            entity_type=entity_type,
            entity_id=entity_id,
            url=page_url[:500],
        )
        db.add(row)
        db.flush()
        return row
    return None


def _get_fix(db, fix_id: int) -> SeoFixRequest:
    fix = db.get(SeoFixRequest, fix_id)
    if not fix:
        raise HTTPException(status_code=404, detail="Fix not found.")
    return fix


@router.get("/health")
def seo_health(auth: dict = Depends(require_seo_agent_headers)):
    cfg = seo_config()
    return {
        "ok": True,
        "module": "seo",
        "enabled": cfg["enabled"],
        "environment": cfg["environment"],
        "public_base_url": cfg["public_base_url"],
        "allow_auto_apply": cfg["allow_auto_apply"],
        "require_production_approval": cfg["require_production_approval"],
        "request_id": auth.get("request_id"),
    }


@router.get("/public-pages")
def seo_public_pages(
    db: DBSession,
    auth: dict = Depends(require_seo_agent_headers),
):
    pages = list_public_pages(db)
    log_seo_action(
        db,
        environment=auth["environment"],
        agent_name="external",
        action="list_public_pages",
        request_id=auth.get("request_id"),
        message=f"count={len(pages)}",
    )
    db.commit()
    return {"environment": auth["environment"], "count": len(pages), "pages": pages}


@router.get("/pages/{page_id}")
def seo_page_detail(
    page_id: str,
    db: DBSession,
    auth: dict = Depends(require_seo_agent_headers),
):
    detail = get_public_page_detail(db, page_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Page not found.")
    return detail


@router.post("/crawl-runs")
def seo_crawl_runs(
    body: CrawlRunIn,
    db: DBSession,
    auth: dict = Depends(require_seo_agent_headers),
):
    env = auth["environment"]
    run: SeoCrawlRun | None = None
    if body.id:
        run = db.get(SeoCrawlRun, body.id)
    if run is None:
        run = SeoCrawlRun(
            environment=env,
            agent_name=body.agent_name[:80],
            status=body.status[:30],
        )
        db.add(run)
        db.flush()
    else:
        run.status = body.status[:30]
        run.agent_name = (body.agent_name or run.agent_name)[:80]
    run.pages_scanned = int(body.pages_scanned or 0)
    run.issues_found = int(body.issues_found or 0)
    run.error_message = body.error_message
    if body.raw_summary is not None:
        run.raw_summary_json = json.dumps(body.raw_summary, ensure_ascii=False, default=str)[
            :20000
        ]
    if body.finished or body.status in ("completed", "failed", "done"):
        run.finished_at = datetime.now(timezone.utc)
        if body.status == "running":
            run.status = "completed"
    log_seo_action(
        db,
        environment=env,
        agent_name=body.agent_name,
        action="crawl_run",
        request_id=auth.get("request_id"),
        message=f"id={run.id} status={run.status}",
    )
    db.commit()
    return {
        "id": run.id,
        "environment": run.environment,
        "status": run.status,
        "pages_scanned": run.pages_scanned,
        "issues_found": run.issues_found,
    }


@router.post("/issues")
def seo_issues(
    body: IssuesBatchIn | IssueIn,
    db: DBSession,
    auth: dict = Depends(require_seo_agent_headers),
):
    env = auth["environment"]
    items = body.issues if isinstance(body, IssuesBatchIn) else [body]
    created_ids: list[int] = []
    for item in items:
        page = _upsert_page(
            db,
            environment=env,
            page_id=item.page_id,
            page_url=item.page_url,
            page_type=item.page_type,
            entity_type=item.entity_type,
            entity_id=item.entity_id,
        )
        row = SeoIssue(
            environment=env,
            crawl_run_id=item.crawl_run_id,
            page_id=page.id if page else item.page_id,
            severity=(item.severity or "medium")[:20],
            issue_type=item.issue_type[:60],
            title=item.title[:255],
            description=item.description,
            current_value=item.current_value,
            recommended_value=item.recommended_value,
            status=(item.status or "open")[:30],
        )
        db.add(row)
        db.flush()
        created_ids.append(row.id)
    log_seo_action(
        db,
        environment=env,
        agent_name="technical",
        action="ingest_issues",
        request_id=auth.get("request_id"),
        message=f"count={len(created_ids)}",
        payload={"ids": created_ids[:50]},
    )
    db.commit()
    return {"ok": True, "ids": created_ids, "count": len(created_ids)}


@router.post("/suggestions")
def seo_suggestions(
    body: SuggestionsBatchIn | SuggestionIn,
    db: DBSession,
    auth: dict = Depends(require_seo_agent_headers),
):
    env = auth["environment"]
    items = body.suggestions if isinstance(body, SuggestionsBatchIn) else [body]
    created: list[dict[str, Any]] = []
    for item in items:
        page = _upsert_page(
            db,
            environment=env,
            page_id=item.page_id,
            page_url=item.page_url,
            page_type=item.page_type,
            entity_type=item.entity_type,
            entity_id=item.entity_id,
        )
        sug = SeoSuggestion(
            environment=env,
            page_id=page.id if page else item.page_id,
            agent_name=(item.agent_name or "content")[:80],
            suggestion_type=item.suggestion_type[:60],
            current_value=item.current_value,
            suggested_value=item.suggested_value,
            reason=item.reason,
            confidence_score=item.confidence_score,
            status="pending",
        )
        db.add(sug)
        db.flush()
        fix_id = None
        if item.create_fix:
            payload = {
                "value": item.suggested_value,
                "suggestion_type": item.suggestion_type,
                "entity_type": item.entity_type or (page.entity_type if page else None),
                "entity_id": item.entity_id or (page.entity_id if page else None),
            }
            fix = SeoFixRequest(
                environment=env,
                page_id=page.id if page else item.page_id,
                suggestion_id=sug.id,
                fix_type=item.suggestion_type[:60],
                payload_json=json.dumps(payload, ensure_ascii=False),
                status="pending",
            )
            db.add(fix)
            db.flush()
            fix_id = fix.id
        created.append({"suggestion_id": sug.id, "fix_id": fix_id})
    log_seo_action(
        db,
        environment=env,
        agent_name="content",
        action="ingest_suggestions",
        request_id=auth.get("request_id"),
        message=f"count={len(created)}",
    )
    db.commit()
    return {"ok": True, "items": created, "count": len(created)}


@router.post("/reports")
def seo_reports(
    body: ReportIn,
    db: DBSession,
    auth: dict = Depends(require_seo_agent_headers),
):
    env = auth["environment"]
    row = SeoReport(
        environment=env,
        report_type=body.report_type[:40],
        title=(body.title or body.report_type)[:255],
        summary=body.summary,
        period_start=_parse_dt(body.period_start),
        period_end=_parse_dt(body.period_end),
        data_json=(
            json.dumps(body.data, ensure_ascii=False, default=str)[:50000]
            if body.data is not None
            else None
        ),
    )
    db.add(row)
    db.flush()
    log_seo_action(
        db,
        environment=env,
        agent_name="strategy",
        action="ingest_report",
        request_id=auth.get("request_id"),
        message=f"id={row.id} type={row.report_type}",
    )
    db.commit()
    return {"ok": True, "id": row.id}


@router.post("/fixes/{fix_id}/approve")
def seo_fix_approve(
    fix_id: int,
    db: DBSession,
    user: User = Depends(require_permission(SEO_APPROVE)),
):
    fix = _get_fix(db, fix_id)
    try:
        approve_fix(db, fix, user.id)
        db.commit()
    except SeoApplyError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    return {"ok": True, "id": fix.id, "status": fix.status}


@router.post("/fixes/{fix_id}/approve-production")
def seo_fix_approve_production(
    fix_id: int,
    db: DBSession,
    user: User = Depends(require_permission(SEO_PRODUCTION_APPROVE)),
):
    fix = _get_fix(db, fix_id)
    try:
        approve_fix_for_production(db, fix, user.id)
        db.commit()
    except SeoApplyError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    return {
        "ok": True,
        "id": fix.id,
        "status": fix.status,
        "approved_for_production": fix.approved_for_production,
    }


@router.post("/fixes/{fix_id}/apply")
def seo_fix_apply(
    fix_id: int,
    db: DBSession,
    user: User = Depends(require_permission(SEO_APPLY)),
):
    fix = _get_fix(db, fix_id)
    try:
        apply_fix(db, fix, user.id)
        db.commit()
    except SeoApplyError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    return {
        "ok": True,
        "id": fix.id,
        "status": fix.status,
        "applied_at": fix.applied_at.isoformat() if fix.applied_at else None,
    }


@router.post("/fixes/{fix_id}/rollback")
def seo_fix_rollback(
    fix_id: int,
    db: DBSession,
    user: User = Depends(require_permission(SEO_ROLLBACK)),
):
    fix = _get_fix(db, fix_id)
    try:
        rollback_fix(db, fix, user.id)
        db.commit()
    except SeoApplyError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    return {"ok": True, "id": fix.id, "status": fix.status}
