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
from modules.marketing_room.agent_workspace import (
    agent_capabilities,
    list_agent_artifacts,
    regenerate_artifact_image,
    regenerate_artifact_text,
    run_content_plan,
    run_create_post,
    run_hashtags,
    run_seller_angles,
    update_artifact_body,
)
from modules.marketing_room.agents import AGENT_BY_ROLE, AGENT_CARDS
from modules.marketing_room.campaign_manager import (
    list_directives_for_role,
    meta_connection_status,
    run_campaign_review,
)
from modules.marketing_room.team_service import (
    complete_team_request,
    create_team_request,
    execute_team_request,
    team_board,
)
from modules.marketing_room.config import (
    IMAGE_PROVIDERS,
    marketing_ai_settings,
    marketing_higgsfield_settings,
    marketing_image_settings,
    marketing_meta_settings,
    marketing_n8n_webhook_url,
    save_marketing_meta_settings,
    save_marketing_settings,
)
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
            "image_configured": bool(marketing_image_settings(db)["api_key"]),
            "n8n_configured": bool(marketing_n8n_webhook_url(db)),
            "notice": request.query_params.get("notice"),
            "error": request.query_params.get("error"),
        },
    )


@router.get("/settings", response_class=HTMLResponse)
def marketing_settings_page(request: Request, db: DBSession, _: User = Depends(_view)):
    ai = marketing_ai_settings(db)
    img = marketing_image_settings(db)
    meta = marketing_meta_settings(db)
    hg = marketing_higgsfield_settings(db)
    return templates.TemplateResponse(
        "admin_marketing_settings.html",
        {
            "request": request,
            "ai": ai,
            "img": img,
            "meta": meta,
            "hg": hg,
            "image_providers": IMAGE_PROVIDERS,
            "n8n_url": marketing_n8n_webhook_url(db),
            "notice": request.query_params.get("notice"),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/settings")
def marketing_settings_save(
    db: DBSession,
    _: User = Depends(_approve),
    marketing_ai_api_key: str = Form(""),
    marketing_ai_base_url: str = Form("https://api.openai.com/v1"),
    marketing_ai_model: str = Form("gpt-4o-mini"),
    marketing_image_provider: str = Form("fal"),
    marketing_fal_api_key: str = Form(""),
    marketing_higgsfield_api_key: str = Form(""),
    marketing_higgsfield_image_model: str = Form(""),
    marketing_higgsfield_video_model: str = Form(""),
    marketing_image_api_key: str = Form(""),
    marketing_image_base_url: str = Form(""),
    marketing_image_model: str = Form("fal-ai/flux/schnell"),
    marketing_image_size: str = Form("1024x1024"),
    marketing_n8n_webhook_url: str = Form(""),
    marketing_meta_access_token: str = Form(""),
    marketing_meta_ad_account_id: str = Form(""),
    marketing_meta_page_id: str = Form(""),
    marketing_meta_api_version: str = Form("v21.0"),
):
    from modules.settings.service import invalidate_settings_cache

    save_marketing_settings(
        db,
        ai_api_key=marketing_ai_api_key,
        ai_base_url=marketing_ai_base_url,
        ai_model=marketing_ai_model,
        image_provider=marketing_image_provider,
        fal_api_key=marketing_fal_api_key,
        higgsfield_api_key=marketing_higgsfield_api_key,
        higgsfield_image_model=marketing_higgsfield_image_model,
        higgsfield_video_model=marketing_higgsfield_video_model,
        image_api_key=marketing_image_api_key,
        image_base_url=marketing_image_base_url or marketing_ai_base_url,
        image_model=marketing_image_model,
        image_size=marketing_image_size,
        n8n_webhook=marketing_n8n_webhook_url,
    )
    save_marketing_meta_settings(
        db,
        access_token=marketing_meta_access_token,
        ad_account_id=marketing_meta_ad_account_id,
        page_id=marketing_meta_page_id,
        api_version=marketing_meta_api_version,
    )
    invalidate_settings_cache()
    db.commit()
    return RedirectResponse("/admin/marketing-room/settings?notice=تم الحفظ", status_code=302)


@router.get("/agents/{role}", response_class=HTMLResponse)
def agent_workspace_page(
    role: str,
    request: Request,
    db: DBSession,
    user: User = Depends(_view),
):
    card = AGENT_BY_ROLE.get(role)
    if card is None:
        return RedirectResponse("/admin/marketing-room?error=وكيل غير معروف", status_code=302)
    if role == "customer_service":
        return RedirectResponse("/admin/messaging/inbox", status_code=302)
    caps = agent_capabilities(db, role)
    arts = list_agent_artifacts(db, role, limit=30)
    directives = list_directives_for_role(db, role, limit=12)
    board = team_board(db, for_role=role)
    return templates.TemplateResponse(
        "admin_marketing_agent.html",
        {
            "request": request,
            "user": user,
            "card": card,
            "caps": caps,
            "artifacts": arts,
            "directives": directives,
            "team": board,
            "meta_status": meta_connection_status(db) if role == "campaign_manager" else None,
            "notice": request.query_params.get("notice"),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/agents/{role}/team-request")
def agent_team_request(
    role: str,
    db: DBSession,
    user: User = Depends(_run),
    to_role: str = Form(...),
    message: str = Form(...),
    related_artifact_id: str = Form(""),
):
    try:
        rel = int(related_artifact_id) if (related_artifact_id or "").strip().isdigit() else None
        art = create_team_request(
            db,
            user_id=user.id,
            from_role=role,
            to_role=to_role,
            message=message,
            related_artifact_id=rel,
            auto_execute=True,
        )
        db.commit()
        import json as _json

        meta = {}
        if art.meta_json:
            try:
                meta = _json.loads(art.meta_json) or {}
            except Exception:
                meta = {}
        if meta.get("request_status") == "done":
            notice = meta.get("execution_note") or "تم تنفيذ طلب الفريق تلقائياً"
            return RedirectResponse(
                f"/admin/marketing-room/runs/{art.run_id}?notice={notice}",
                status_code=302,
            )
        err = meta.get("execution_error")
        if err:
            return RedirectResponse(
                f"/admin/marketing-room/runs/{art.run_id}?error=طُلب لكن فشل التنفيذ: {err}",
                status_code=302,
            )
        return RedirectResponse(
            f"/admin/marketing-room/runs/{art.run_id}?notice=تم تسجيل الطلب — اضغط تنفيذ إن لزم",
            status_code=302,
        )
    except MarketingRoomError as exc:
        db.rollback()
        return RedirectResponse(
            f"/admin/marketing-room/agents/{role}?error={exc}", status_code=302
        )


@router.post("/agents/{role}/team-request/{request_id}/execute")
def agent_team_request_execute(
    role: str,
    request_id: int,
    db: DBSession,
    user: User = Depends(_run),
):
    try:
        art = execute_team_request(db, request_id, user_id=user.id)
        db.commit()
        import json as _json

        meta = {}
        if art.meta_json:
            try:
                meta = _json.loads(art.meta_json) or {}
            except Exception:
                meta = {}
        notice = meta.get("execution_note") or "تم تنفيذ الطلب"
        return RedirectResponse(
            f"/admin/marketing-room/runs/{art.run_id}?notice={notice}",
            status_code=302,
        )
    except MarketingRoomError as exc:
        db.rollback()
        return RedirectResponse(
            f"/admin/marketing-room/agents/{role}?error={exc}", status_code=302
        )


@router.post("/agents/{role}/team-request/{request_id}/done")
def agent_team_request_done(
    role: str,
    request_id: int,
    db: DBSession,
    _: User = Depends(_run),
    note: str = Form(""),
):
    try:
        complete_team_request(db, request_id, note=note)
        db.commit()
        return RedirectResponse(
            f"/admin/marketing-room/agents/{role}?notice=تم تعليم الطلب كمنجز",
            status_code=302,
        )
    except MarketingRoomError as exc:
        db.rollback()
        return RedirectResponse(
            f"/admin/marketing-room/agents/{role}?error={exc}", status_code=302
        )


@router.post("/agents/campaign_manager/review")
def agent_campaign_review(
    db: DBSession,
    user: User = Depends(_run),
    date_preset: str = Form("last_7d"),
    notes: str = Form(""),
    campaigns_json: str = Form(""),
):
    try:
        campaigns = None
        source = "meta_api"
        raw = (campaigns_json or "").strip()
        if raw:
            import json as _json

            parsed = _json.loads(raw)
            campaigns = parsed
            source = "mcp_or_paste"
        run = run_campaign_review(
            db,
            user_id=user.id,
            campaigns=campaigns,
            source=source,
            notes=notes,
            date_preset=date_preset or "last_7d",
        )
        db.commit()
        return RedirectResponse(
            f"/admin/marketing-room/runs/{run.id}?notice=تمت مراجعة الحملات وتوجيه الوكلاء",
            status_code=302,
        )
    except MarketingRoomError as exc:
        db.rollback()
        return RedirectResponse(
            f"/admin/marketing-room/agents/campaign_manager?error={exc}",
            status_code=302,
        )
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        return RedirectResponse(
            f"/admin/marketing-room/agents/campaign_manager?error={exc}",
            status_code=302,
        )


@router.post("/agents/content_manager/plan")
def agent_content_plan(
    db: DBSession,
    user: User = Depends(_run),
    days: str = Form("7"),
    focus: str = Form(""),
):
    try:
        d = int((days or "7").strip() or "7")
    except ValueError:
        d = 7
    try:
        run = run_content_plan(db, user_id=user.id, days=d, focus=focus)
        db.commit()
        return RedirectResponse(
            f"/admin/marketing-room/runs/{run.id}?notice=تم إنشاء خطة المحتوى",
            status_code=302,
        )
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        return RedirectResponse(
            f"/admin/marketing-room/agents/content_manager?error={exc}",
            status_code=302,
        )


@router.post("/agents/design/create")
def agent_design_create(
    db: DBSession,
    user: User = Depends(_run),
    topic: str = Form(""),
    platform: str = Form("instagram"),
    content_focus: str = Form("restaurant"),
    image_prompt: str = Form(""),
    with_text: str = Form(""),
    with_image: str = Form(""),
    with_video: str = Form(""),
    team_task_id: str = Form(""),
    fulfill_request_id: str = Form(""),
):
    try:
        tid = int(team_task_id) if (team_task_id or "").strip().isdigit() else None
        rid = int(fulfill_request_id) if (fulfill_request_id or "").strip().isdigit() else None
        # إن لم يُحدَّد أي خيار — نص فقط (توافق قديم)
        any_task = (with_text == "on") or (with_image == "on") or (with_video == "on")
        do_text = (with_text == "on") or (not any_task)
        run = run_create_post(
            db,
            user_id=user.id,
            topic=topic,
            platform=platform,
            with_text=do_text,
            with_image=with_image == "on",
            with_video=with_video == "on",
            image_prompt_custom=image_prompt,
            content_focus=content_focus,
            team_task_id=tid,
            fulfill_request_id=rid,
        )
        db.commit()
        return RedirectResponse(
            f"/admin/marketing-room/runs/{run.id}?notice=تم إنشاء المنشور",
            status_code=302,
        )
    except MarketingRoomError as exc:
        db.rollback()
        return RedirectResponse(
            f"/admin/marketing-room/agents/design?error={exc}",
            status_code=302,
        )
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        return RedirectResponse(
            f"/admin/marketing-room/agents/design?error={exc}",
            status_code=302,
        )


@router.post("/agents/hashtag/generate")
def agent_hashtag_generate(
    db: DBSession,
    user: User = Depends(_run),
    topic: str = Form(""),
    team_task_id: str = Form(""),
    fulfill_request_id: str = Form(""),
):
    try:
        tid = int(team_task_id) if (team_task_id or "").strip().isdigit() else None
        rid = int(fulfill_request_id) if (fulfill_request_id or "").strip().isdigit() else None
        run = run_hashtags(
            db,
            user_id=user.id,
            topic=topic,
            team_task_id=tid,
            fulfill_request_id=rid,
        )
        db.commit()
        return RedirectResponse(
            f"/admin/marketing-room/runs/{run.id}?notice=تم اقتراح الهاشتاجات",
            status_code=302,
        )
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        return RedirectResponse(
            f"/admin/marketing-room/agents/hashtag?error={exc}",
            status_code=302,
        )


@router.post("/agents/website_seller/angles")
def agent_seller_angles(
    db: DBSession,
    user: User = Depends(_run),
    team_task_id: str = Form(""),
    fulfill_request_id: str = Form(""),
):
    try:
        tid = int(team_task_id) if (team_task_id or "").strip().isdigit() else None
        rid = int(fulfill_request_id) if (fulfill_request_id or "").strip().isdigit() else None
        run = run_seller_angles(
            db,
            user_id=user.id,
            team_task_id=tid,
            fulfill_request_id=rid,
        )
        db.commit()
        return RedirectResponse(
            f"/admin/marketing-room/runs/{run.id}?notice=تم إنشاء زوايا البيع",
            status_code=302,
        )
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        return RedirectResponse(
            f"/admin/marketing-room/agents/website_seller?error={exc}",
            status_code=302,
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


@router.post("/artifacts/{artifact_id}/edit")
def artifact_edit(
    artifact_id: int,
    db: DBSession,
    _: User = Depends(_run),
    body_text: str = Form(...),
    title: str = Form(""),
):
    try:
        art = update_artifact_body(db, artifact_id, body_text, title=title or None)
        db.commit()
        return RedirectResponse(
            f"/admin/marketing-room/runs/{art.run_id}?notice=تم حفظ التعديل",
            status_code=302,
        )
    except MarketingRoomError as exc:
        db.rollback()
        art = None
        from modules.marketing_room.models import MarketingArtifact

        art = db.get(MarketingArtifact, artifact_id)
        rid = art.run_id if art else None
        if rid:
            return RedirectResponse(
                f"/admin/marketing-room/runs/{rid}?error={exc}", status_code=302
            )
        return RedirectResponse(f"/admin/marketing-room?error={exc}", status_code=302)


@router.post("/artifacts/{artifact_id}/regen-text")
def artifact_regen_text(
    artifact_id: int,
    db: DBSession,
    _: User = Depends(_run),
    instruction: str = Form(""),
    content_focus: str = Form("restaurant"),
):
    try:
        art = regenerate_artifact_text(
            db,
            artifact_id,
            instruction=instruction,
            content_focus=content_focus,
        )
        db.commit()
        return RedirectResponse(
            f"/admin/marketing-room/runs/{art.run_id}?notice=تم إعادة توليد النص",
            status_code=302,
        )
    except MarketingRoomError as exc:
        db.rollback()
        from modules.marketing_room.models import MarketingArtifact

        art = db.get(MarketingArtifact, artifact_id)
        rid = art.run_id if art else None
        if rid:
            return RedirectResponse(
                f"/admin/marketing-room/runs/{rid}?error={exc}", status_code=302
            )
        return RedirectResponse(f"/admin/marketing-room?error={exc}", status_code=302)


@router.post("/artifacts/{artifact_id}/regen-image")
def artifact_regen_image(
    artifact_id: int,
    db: DBSession,
    _: User = Depends(_run),
):
    try:
        art = regenerate_artifact_image(db, artifact_id)
        db.commit()
        return RedirectResponse(
            f"/admin/marketing-room/runs/{art.run_id}?notice=تم إعادة توليد الصورة",
            status_code=302,
        )
    except MarketingRoomError as exc:
        db.rollback()
        from modules.marketing_room.models import MarketingArtifact

        art = db.get(MarketingArtifact, artifact_id)
        rid = art.run_id if art else None
        if rid:
            return RedirectResponse(
                f"/admin/marketing-room/runs/{rid}?error={exc}", status_code=302
            )
        return RedirectResponse(f"/admin/marketing-room?error={exc}", status_code=302)


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
