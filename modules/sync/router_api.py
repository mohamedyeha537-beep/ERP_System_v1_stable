from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deps import LoggedInUser, get_db_session, require_permission
from app.jinja_env import templates
from infra.config import get_settings
from modules.authz.models import User
from modules.authz.permissions import ADMIN_SETTINGS
from modules.sync import client, service
from modules.sync.models import SyncEvent, SyncEventStatus, SyncIncomingEvent
from modules.sync.schemas import SyncPullIn, SyncPullOut, SyncPushIn, SyncPushOut
from modules.sync.service import get_sync_status

router = APIRouter(prefix="/api/sync", tags=["sync"])
web_router = APIRouter(prefix="/admin/sync", tags=["admin-sync"])


@router.get("/local-status")
def sync_local_status(
    _user: LoggedInUser,
    db: Session = Depends(get_db_session),
):
    """حالة الأوفلاين/المزامنة لأي مستخدم مسجّل — للعمل المحلي بدون إنترنت."""
    return get_sync_status(db)


def verify_sync_api_key(
    x_sync_api_key: str | None = Header(default=None, alias="X-Sync-API-Key")
):
    expected = (get_settings().online_sync_api_key or "").strip()
    if not x_sync_api_key or not expected or not secrets.compare_digest(x_sync_api_key, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="مفتاح المزامنة غير صالح.")
    return True


@router.post("/push", response_model=SyncPushOut)
def sync_push(
    body: SyncPushIn,
    _: bool = Depends(verify_sync_api_key),
    db: Session = Depends(get_db_session),
):
    """نقطة استقبال الأحداث القادمة من مواقع محلية أخرى."""
    results = []
    for ev_in in body.events:
        existing = db.scalar(select(SyncIncomingEvent).where(SyncIncomingEvent.event_id == ev_in.event_id))
        if existing:
            results.append({"event_id": ev_in.event_id, "applied": True, "message": "already received"})
            continue

        ev = SyncIncomingEvent(
            event_id=ev_in.event_id,
            site_id=ev_in.site_id,
            action=ev_in.action,
            table_name=ev_in.table_name,
            record_id=ev_in.record_id,
            payload_json=ev_in.payload_json,
            source_updated_at=ev_in.source_updated_at,
        )
        db.add(ev)
        db.flush()
        db.info["_sync_processing"] = True
        try:
            ok, msg = service.apply_remote_event(db, ev)
            if ok:
                ev.applied_at = service.now_utc()
                # أضف الحدث كحدث وارد وأيضاً كحدث يُمكن لمواقع أخرى سحبه
                relay = SyncEvent(
                    event_id=ev_in.event_id,
                    site_id=ev_in.site_id,
                    action=ev_in.action,
                    table_name=ev_in.table_name,
                    record_id=ev_in.record_id,
                    payload_json=ev_in.payload_json,
                    source_updated_at=ev_in.source_updated_at,
                    status=SyncEventStatus.ACKED.value,
                    acked_at=service.now_utc(),
                )
                db.add(relay)
                db.flush()
            else:
                ev.error = msg[:500]
        finally:
            db.info["_sync_processing"] = False
        results.append({"event_id": ev_in.event_id, "applied": ok, "message": msg})
    db.commit()
    return SyncPushOut(results=results)


@router.post("/pull", response_model=SyncPullOut)
def sync_pull(
    body: SyncPullIn,
    _: bool = Depends(verify_sync_api_key),
    db: Session = Depends(get_db_session),
):
    """ترجع أحداث من السيرفر المركزي للموقع الطالب."""
    puller_site = body.site_id or service.get_site_id()
    stmt = select(SyncEvent).where(SyncEvent.site_id != puller_site)
    if body.last_event_id:
        stmt = stmt.where(SyncEvent.event_id > body.last_event_id)
    stmt = stmt.order_by(SyncEvent.event_id).limit(body.limit)
    rows = db.execute(stmt).scalars().all()

    out = [
        {
            "event_id": r.event_id,
            "site_id": r.site_id,
            "action": r.action,
            "table_name": r.table_name,
            "record_id": r.record_id,
            "payload_json": r.payload_json,
            "source_updated_at": r.source_updated_at,
        }
        for r in rows
    ]
    return SyncPullOut(events=out, has_more=len(out) >= body.limit)


@web_router.get("", response_class=HTMLResponse)
def admin_sync_page(
    request: Request,
    db: Session = Depends(get_db_session),
    user: User = Depends(require_permission(ADMIN_SETTINGS)),
):
    status = get_sync_status(db)
    return templates.TemplateResponse(
        "admin_sync.html",
        {"request": request, "status": status},
    )


@web_router.get("/status")
def admin_sync_status(
    request: Request,
    db: Session = Depends(get_db_session),
    user: User = Depends(require_permission(ADMIN_SETTINGS)),
):
    return get_sync_status(db)


@web_router.post("/trigger")
def admin_sync_trigger(
    request: Request,
    db: Session = Depends(get_db_session),
    user: User = Depends(require_permission(ADMIN_SETTINGS)),
):
    """يدفع ويسحب يدوياً."""
    pushed, err_push = client.push_pending(db)
    pulled, err_pull = client.pull_remote(db)
    return {
        "pushed": pushed,
        "pull_error": err_pull,
        "pulled": pulled,
        "push_error": err_push,
    }
