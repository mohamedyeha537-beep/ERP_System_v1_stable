"""مركز إشعارات النظام للأدمن — /admin/activity"""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.deps import DBSession, require_any_permission
from app.jinja_env import templates
from modules.authz.models import User
from modules.authz.permissions import ADMIN_SETTINGS, MESSAGING_MANAGE, REPORTS_VIEW
from modules.dashboard_notify.activity_hub import (
    apply_item_action,
    bell_unread_count,
    build_activity_hub,
    mark_all_read,
    muted_list_for_user,
)

router = APIRouter(prefix="/admin/activity", tags=["activity-hub"])
_perm = require_any_permission(ADMIN_SETTINGS, REPORTS_VIEW, MESSAGING_MANAGE)


@router.get("", response_class=HTMLResponse)
def activity_hub_page(
    request: Request,
    db: DBSession,
    user: User = Depends(_perm),
    ok: str | None = Query(None),
    error: str | None = Query(None),
):
    sections = build_activity_hub(db, user.id)
    total = sum(1 for sec in sections for item in sec.items if item.is_new)
    mutes = muted_list_for_user(db, user.id)
    return templates.TemplateResponse(
        "admin_activity_hub.html",
        {
            "request": request,
            "user": user,
            "sections": sections,
            "unread_total": total,
            "mutes": mutes,
            "ok": ok,
            "error": error,
        },
    )


@router.get("/api/count")
def activity_count_api(db: DBSession, user: User = Depends(_perm)):
    return JSONResponse({"count": bell_unread_count(db, user.id)})


@router.post("/mark-seen")
def activity_mark_seen(db: DBSession, user: User = Depends(_perm)):
    n = mark_all_read(db, user.id)
    db.commit()
    return RedirectResponse(
        f"/admin/activity?ok={quote(f'تم تعليم {n} إشعار كمقروء')}",
        status_code=302,
    )


@router.post("/item")
def activity_item_action(
    db: DBSession,
    user: User = Depends(_perm),
    item_key: str = Form(""),
    action: str = Form(...),
    mute_key: str = Form(""),
    mute_label: str = Form(""),
):
    msg = apply_item_action(
        db,
        user.id,
        item_key=item_key,
        action=action,
        mute_key=mute_key,
        mute_label=mute_label,
    )
    db.commit()
    if msg.startswith("تم"):
        return RedirectResponse(f"/admin/activity?ok={quote(msg)}", status_code=302)
    return RedirectResponse(f"/admin/activity?error={quote(msg)}", status_code=302)
