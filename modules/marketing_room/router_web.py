"""واجهة غرفة وكلاء التسويق — /admin/marketing-room"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from starlette.requests import Request

from app.deps import DBSession, require_permission
from app.jinja_env import templates
from modules.authz.models import User
from modules.authz.permissions import (
    MARKETING_ROOM_APPROVE,
    MARKETING_ROOM_RUN,
    MARKETING_ROOM_VIEW,
)
from modules.marketing_room.agents import AGENT_CARDS
from modules.marketing_room.config import marketing_ai_settings, marketing_n8n_webhook_url
from modules.marketing_room.pipeline import start_daily_pipeline
from modules.marketing_room.service import (
    MarketingRoomError,
    approve_all_pending,
    export_post_text,
    get_run,
    list_recent_runs,
    list_run_artifacts,
    room_stats,
    set_artifact_status,
)

router = APIRouter(prefix="/admin/marketing-room", tags=["marketing-room-admin"])
_view = require_permission(MARKETING_ROOM_VIEW)
_run = require_permission(MARKETING_ROOM_RUN)
_approve = require_permission(MARKETING_ROOM_APPROVE)


def _agent_status_map(db, run) -> dict[str, str]:
    """حالة كل بطاقة وكيل حسب آخر تشغيل."""
    status = {a.role: "ready" for a in AGENT_CARDS}
    if run is None:
        return status
    if run.status == "running":
        for a in AGENT_CARDS:
            if a.pipeline:
                status[a.role] = "running"
        return status
    arts = list_run_artifacts(db, run.id) if run else []
    by_role: dict[str, list] = {}
    for art in arts:
        by_role.setdefault(art.agent_role, []).append(art)
    for role, items in by_role.items():
        if any(x.status == "pending_approval" for x in items):
            status[role] = "awaiting"
        elif items and all(x.status == "approved" for x in items):
            status[role] = "approved"
        elif items and all(x.status == "rejected" for x in items):
            status[role] = "rejected"
        else:
            status[role] = "done"
    status["delivery"] = "manual"
    status["customer_service"] = "linked"
    if run.status == "failed":
        status["chief"] = "failed"
    return status


@router.get("", response_class=HTMLResponse)
def marketing_room_home(request: Request, db: DBSession, user: User = Depends(_view)):
    runs = list_recent_runs(db, limit=12)
    latest = runs[0] if runs else None
    stats = room_stats(db)
    ai = marketing_ai_settings(db)
    return templates.TemplateResponse(
        "admin_marketing_room.html",
        {
            "request": request,
            "user": user,
            "agents": AGENT_CARDS,
            "agent_status": _agent_status_map(db, latest),
            "runs": runs,
            "latest_run": latest,
            "stats": stats,
            "ai_configured": bool(ai["api_key"]),
            "n8n_configured": bool(marketing_n8n_webhook_url(db)),
            "notice": request.query_params.get("notice"),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/run", response_class=HTMLResponse)
def marketing_room_run(
    db: DBSession,
    user: User = Depends(_run),
    business_domain: str = Form("shared"),
):
    try:
        run = start_daily_pipeline(
            db, user_id=user.id, business_domain=business_domain or "shared"
        )
        db.commit()
        if run.status == "failed":
            return RedirectResponse(
                f"/admin/marketing-room?error={run.error_message or 'فشل التشغيل'}",
                status_code=302,
            )
        return RedirectResponse(
            f"/admin/marketing-room/runs/{run.id}?notice=تم إنشاء المسودات — راجعها واعتمدها",
            status_code=302,
        )
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        return RedirectResponse(
            f"/admin/marketing-room?error={exc}",
            status_code=302,
        )


@router.get("/runs/{run_id}", response_class=HTMLResponse)
def marketing_run_detail(
    run_id: int,
    request: Request,
    db: DBSession,
    user: User = Depends(_view),
):
    run = get_run(db, run_id)
    if run is None:
        return RedirectResponse("/admin/marketing-room?error=التشغيل غير موجود", status_code=302)
    arts = list_run_artifacts(db, run_id)
    return templates.TemplateResponse(
        "admin_marketing_run.html",
        {
            "request": request,
            "user": user,
            "run": run,
            "artifacts": arts,
            "agents": AGENT_CARDS,
            "notice": request.query_params.get("notice"),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/artifacts/{artifact_id}/approve")
def artifact_approve(
    artifact_id: int,
    db: DBSession,
    user: User = Depends(_approve),
    note: str = Form(""),
):
    try:
        art = set_artifact_status(
            db, artifact_id, status="approved", user_id=user.id, note=note
        )
        db.commit()
        return RedirectResponse(
            f"/admin/marketing-room/runs/{art.run_id}?notice=تم الاعتماد",
            status_code=302,
        )
    except MarketingRoomError as exc:
        return RedirectResponse(f"/admin/marketing-room?error={exc}", status_code=302)


@router.post("/artifacts/{artifact_id}/reject")
def artifact_reject(
    artifact_id: int,
    db: DBSession,
    user: User = Depends(_approve),
    note: str = Form(""),
):
    try:
        art = set_artifact_status(
            db, artifact_id, status="rejected", user_id=user.id, note=note
        )
        db.commit()
        return RedirectResponse(
            f"/admin/marketing-room/runs/{art.run_id}?notice=تم الرفض",
            status_code=302,
        )
    except MarketingRoomError as exc:
        return RedirectResponse(f"/admin/marketing-room?error={exc}", status_code=302)


@router.post("/runs/{run_id}/approve-all")
def run_approve_all(run_id: int, db: DBSession, user: User = Depends(_approve)):
    try:
        n = approve_all_pending(db, run_id, user_id=user.id)
        db.commit()
        return RedirectResponse(
            f"/admin/marketing-room/runs/{run_id}?notice=تم اعتماد {n} مسودّة",
            status_code=302,
        )
    except MarketingRoomError as exc:
        return RedirectResponse(
            f"/admin/marketing-room/runs/{run_id}?error={exc}", status_code=302
        )


@router.get("/artifacts/{artifact_id}/export.txt")
def artifact_export(
    artifact_id: int,
    db: DBSession,
    _: User = Depends(_view),
):
    try:
        text = export_post_text(db, artifact_id)
    except MarketingRoomError as exc:
        return PlainTextResponse(str(exc), status_code=404)
    return PlainTextResponse(
        text,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="marketing-{artifact_id}.txt"'
        },
    )


@router.get("/artifacts/{artifact_id}/copy", response_class=HTMLResponse)
def artifact_copy_page(
    artifact_id: int,
    request: Request,
    db: DBSession,
    _: User = Depends(_view),
):
    try:
        text = export_post_text(db, artifact_id)
    except MarketingRoomError as exc:
        return RedirectResponse(f"/admin/marketing-room?error={exc}", status_code=302)
    from modules.marketing_room.models import MarketingArtifact

    art = db.get(MarketingArtifact, artifact_id)
    return templates.TemplateResponse(
        "admin_marketing_export.html",
        {
            "request": request,
            "artifact": art,
            "export_text": text,
        },
    )
