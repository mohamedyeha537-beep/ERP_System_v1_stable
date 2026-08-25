"""لوحة تحكم SEO Center — /admin/seo"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from starlette.requests import Request

from app.deps import DBSession, require_permission
from app.jinja_env import templates
from modules.authz.models import User
from modules.authz.permissions import (
    SEO_APPLY,
    SEO_APPROVE,
    SEO_PRODUCTION_APPROVE,
    SEO_REVIEW,
    SEO_ROLLBACK,
    SEO_VIEW,
)
from modules.seo.apply_service import (
    SeoApplyError,
    approve_fix,
    approve_fix_for_production,
    apply_fix,
    ensure_fix_from_suggestion,
    rollback_fix,
)
from modules.seo.config import seo_config
from modules.seo.models import (
    SeoCrawlRun,
    SeoFixRequest,
    SeoIssue,
    SeoPage,
    SeoReport,
    SeoSuggestion,
)
from modules.seo.public_pages import list_public_pages

router = APIRouter(prefix="/admin/seo", tags=["seo-admin"])
_view = require_permission(SEO_VIEW)
_review = require_permission(SEO_REVIEW)
_approve = require_permission(SEO_APPROVE)
_apply = require_permission(SEO_APPLY)
_rollback = require_permission(SEO_ROLLBACK)
_prod = require_permission(SEO_PRODUCTION_APPROVE)


def _cfg_ctx() -> dict:
    cfg = seo_config()
    return {
        "seo_env": cfg["environment"],
        "seo_enabled": cfg["enabled"],
        "seo_base_url": cfg["public_base_url"],
        "seo_allow_auto_apply": cfg["allow_auto_apply"],
        "seo_require_prod_approval": cfg["require_production_approval"],
        "is_production": cfg["environment"] == "production",
    }


@router.get("", response_class=HTMLResponse)
def seo_dashboard(request: Request, db: DBSession, _: User = Depends(_view)):
    env = seo_config()["environment"]
    last_crawl = db.scalars(
        select(SeoCrawlRun)
        .where(SeoCrawlRun.environment == env)
        .order_by(SeoCrawlRun.id.desc())
        .limit(1)
    ).first()
    sev_rows = db.execute(
        select(SeoIssue.severity, func.count())
        .where(SeoIssue.environment == env, SeoIssue.status == "open")
        .group_by(SeoIssue.severity)
    ).all()
    severity_counts = {str(s): int(c) for s, c in sev_rows}
    suggestions = db.scalars(
        select(SeoSuggestion)
        .where(SeoSuggestion.environment == env)
        .order_by(SeoSuggestion.id.desc())
        .limit(15)
    ).all()
    reports = db.scalars(
        select(SeoReport)
        .where(SeoReport.environment == env)
        .order_by(SeoReport.id.desc())
        .limit(10)
    ).all()
    fixes = db.scalars(
        select(SeoFixRequest)
        .where(SeoFixRequest.environment == env)
        .order_by(SeoFixRequest.id.desc())
        .limit(15)
    ).all()
    return templates.TemplateResponse(
        "admin_seo_dashboard.html",
        {
            "request": request,
            **_cfg_ctx(),
            "last_crawl": last_crawl,
            "severity_counts": severity_counts,
            "suggestions": suggestions,
            "reports": reports,
            "fixes": fixes,
            "flash": request.query_params.get("flash"),
            "error": request.query_params.get("error"),
        },
    )


@router.get("/pages", response_class=HTMLResponse)
def seo_pages_list(request: Request, db: DBSession, _: User = Depends(_view)):
    public = list_public_pages(db)
    stored = db.scalars(
        select(SeoPage)
        .where(SeoPage.environment == seo_config()["environment"])
        .order_by(SeoPage.id.desc())
        .limit(200)
    ).all()
    return templates.TemplateResponse(
        "admin_seo_pages.html",
        {
            "request": request,
            **_cfg_ctx(),
            "public_pages": public,
            "stored_pages": stored,
        },
    )


@router.get("/issues", response_class=HTMLResponse)
def seo_issues_list(
    request: Request,
    db: DBSession,
    _: User = Depends(_view),
    severity: str | None = Query(None),
    issue_type: str | None = Query(None),
    status: str | None = Query(None),
    page_type: str | None = Query(None),
):
    env = seo_config()["environment"]
    q = select(SeoIssue).where(SeoIssue.environment == env)
    if severity:
        q = q.where(SeoIssue.severity == severity)
    if issue_type:
        q = q.where(SeoIssue.issue_type == issue_type)
    if status:
        q = q.where(SeoIssue.status == status)
    q = q.order_by(SeoIssue.id.desc()).limit(300)
    issues = db.scalars(q).all()
    if page_type:
        page_ids = {
            p.id
            for p in db.scalars(
                select(SeoPage).where(
                    SeoPage.environment == env, SeoPage.page_type == page_type
                )
            ).all()
        }
        issues = [i for i in issues if i.page_id in page_ids]
    return templates.TemplateResponse(
        "admin_seo_issues.html",
        {
            "request": request,
            **_cfg_ctx(),
            "issues": issues,
            "filters": {
                "severity": severity or "",
                "issue_type": issue_type or "",
                "status": status or "",
                "page_type": page_type or "",
            },
        },
    )


@router.get("/suggestions", response_class=HTMLResponse)
def seo_suggestions_list(request: Request, db: DBSession, _: User = Depends(_view)):
    env = seo_config()["environment"]
    suggestions = db.scalars(
        select(SeoSuggestion)
        .where(SeoSuggestion.environment == env)
        .order_by(SeoSuggestion.id.desc())
        .limit(200)
    ).all()
    fixes_by_sug = {
        f.suggestion_id: f
        for f in db.scalars(
            select(SeoFixRequest).where(SeoFixRequest.environment == env)
        ).all()
        if f.suggestion_id
    }
    return templates.TemplateResponse(
        "admin_seo_suggestions.html",
        {
            "request": request,
            **_cfg_ctx(),
            "suggestions": suggestions,
            "fixes_by_sug": fixes_by_sug,
            "flash": request.query_params.get("flash"),
            "error": request.query_params.get("error"),
        },
    )


@router.get("/reports", response_class=HTMLResponse)
def seo_reports_list(request: Request, db: DBSession, _: User = Depends(_view)):
    env = seo_config()["environment"]
    reports = db.scalars(
        select(SeoReport)
        .where(SeoReport.environment == env)
        .order_by(SeoReport.id.desc())
        .limit(100)
    ).all()
    return templates.TemplateResponse(
        "admin_seo_reports.html",
        {"request": request, **_cfg_ctx(), "reports": reports},
    )


@router.post("/suggestions/{suggestion_id}/approve")
def seo_suggestion_approve(
    suggestion_id: int,
    db: DBSession,
    user: User = Depends(_approve),
):
    sug = db.get(SeoSuggestion, suggestion_id)
    if not sug:
        return RedirectResponse("/admin/seo/suggestions?error=not_found", status_code=303)
    sug.status = "approved"
    sug.reviewed_by_id = user.id
    from datetime import datetime, timezone

    sug.reviewed_at = datetime.now(timezone.utc)
    fix = ensure_fix_from_suggestion(db, sug)
    approve_fix(db, fix, user.id)
    db.commit()
    return RedirectResponse("/admin/seo/suggestions?flash=approved", status_code=303)


@router.post("/suggestions/{suggestion_id}/reject")
def seo_suggestion_reject(
    suggestion_id: int,
    db: DBSession,
    user: User = Depends(_review),
):
    sug = db.get(SeoSuggestion, suggestion_id)
    if not sug:
        return RedirectResponse("/admin/seo/suggestions?error=not_found", status_code=303)
    sug.status = "rejected"
    sug.reviewed_by_id = user.id
    from datetime import datetime, timezone

    sug.reviewed_at = datetime.now(timezone.utc)
    db.commit()
    return RedirectResponse("/admin/seo/suggestions?flash=rejected", status_code=303)


@router.post("/fixes/{fix_id}/approve")
def seo_admin_approve_fix(
    fix_id: int,
    db: DBSession,
    user: User = Depends(_approve),
):
    fix = db.get(SeoFixRequest, fix_id)
    if not fix:
        return RedirectResponse("/admin/seo?error=not_found", status_code=303)
    try:
        approve_fix(db, fix, user.id)
        db.commit()
    except SeoApplyError as exc:
        return RedirectResponse(
            f"/admin/seo?error={exc.message}", status_code=303
        )
    return RedirectResponse("/admin/seo?flash=approved", status_code=303)


@router.post("/fixes/{fix_id}/approve-production")
def seo_admin_approve_prod(
    fix_id: int,
    db: DBSession,
    user: User = Depends(_prod),
):
    fix = db.get(SeoFixRequest, fix_id)
    if not fix:
        return RedirectResponse("/admin/seo?error=not_found", status_code=303)
    try:
        approve_fix_for_production(db, fix, user.id)
        db.commit()
    except SeoApplyError as exc:
        return RedirectResponse(f"/admin/seo?error={exc.message}", status_code=303)
    return RedirectResponse("/admin/seo?flash=prod_approved", status_code=303)


@router.post("/fixes/{fix_id}/apply")
def seo_admin_apply(
    fix_id: int,
    db: DBSession,
    user: User = Depends(_apply),
):
    fix = db.get(SeoFixRequest, fix_id)
    if not fix:
        return RedirectResponse("/admin/seo/suggestions?error=not_found", status_code=303)
    try:
        apply_fix(db, fix, user.id)
        db.commit()
    except SeoApplyError as exc:
        db.rollback()
        return RedirectResponse(
            f"/admin/seo/suggestions?error={exc.message}", status_code=303
        )
    return RedirectResponse("/admin/seo/suggestions?flash=applied", status_code=303)


@router.post("/fixes/{fix_id}/rollback")
def seo_admin_rollback(
    fix_id: int,
    db: DBSession,
    user: User = Depends(_rollback),
):
    fix = db.get(SeoFixRequest, fix_id)
    if not fix:
        return RedirectResponse("/admin/seo/suggestions?error=not_found", status_code=303)
    try:
        rollback_fix(db, fix, user.id)
        db.commit()
    except SeoApplyError as exc:
        db.rollback()
        return RedirectResponse(
            f"/admin/seo/suggestions?error={exc.message}", status_code=303
        )
    return RedirectResponse("/admin/seo/suggestions?flash=rolled_back", status_code=303)
