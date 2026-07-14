from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session as ORMSession

from infra.config import get_settings
from modules.sync.models import SyncIncomingEvent
from modules.sync.schemas import SyncPullIn, SyncPushIn
from modules.sync.service import (
    apply_remote_event,
    get_or_create_sync_state,
    get_pending_events,
    get_site_id,
    mark_events_sent,
    update_sync_state,
)

log = logging.getLogger("sync.client")


def _json_default(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


def _http_post(url: str, payload: dict[str, Any], api_key: str) -> dict[str, Any] | None:
    data = json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "X-Sync-API-Key": api_key,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read().decode("utf-8")
        if not body:
            return None
        return json.loads(body)


def _base_url() -> str | None:
    settings = get_settings()
    url = (settings.online_sync_url or "").rstrip("/")
    if not url:
        return None
    return url


def push_pending(db: ORMSession) -> tuple[int, str | None]:
    """يدفع الأحداث المعلّقة إلى السيرفر المركزي.

    يُرجع (عدد المؤكدة، رسالة الخطأ إن وجدت).
    """
    settings = get_settings()
    base = _base_url()
    if not base or not settings.online_sync_api_key:
        return 0, "sync not configured"

    events = get_pending_events(db, limit=settings.sync_batch_size)
    if not events:
        return 0, None

    push = SyncPushIn(
        events=[
            {
                "event_id": ev.event_id,
                "site_id": ev.site_id,
                "action": ev.action,
                "table_name": ev.table_name,
                "record_id": ev.record_id,
                "payload_json": ev.payload_json,
                "source_updated_at": ev.source_updated_at,
            }
            for ev in events
        ]
    )

    try:
        resp = _http_post(
            f"{base}/api/sync/push",
            push.model_dump(mode="json"),
            settings.online_sync_api_key,
        )
        acked = []
        failed = []
        if resp and "results" in resp:
            for result in resp["results"]:
                eid = result.get("event_id")
                if result.get("applied"):
                    acked.append(eid)
                else:
                    failed.append((eid, result.get("message", "apply failed")))

        mark_events_sent(db, acked, acked=True)
        for eid, msg in failed:
            mark_events_sent(db, [eid], error=msg[:500])

        update_sync_state(
            db,
            is_online=True,
            last_push_at=datetime.now(timezone.utc),
            last_error=None,
        )
        db.commit()
        return len(acked), None
    except Exception as exc:
        update_sync_state(
            db,
            is_online=False,
            last_error=str(exc)[:500],
        )
        db.rollback()
        return 0, str(exc)


def pull_remote(db: ORMSession) -> tuple[int, str | None]:
    """يسحب الأحداث الواردة من السيرفر المركزي ويطبقها محلياً."""
    settings = get_settings()
    base = _base_url()
    if not base or not settings.online_sync_api_key or not settings.sync_pull_enabled:
        return 0, None

    state = get_or_create_sync_state(db, get_site_id())
    last_id = state.last_event_id

    pull = SyncPullIn(last_event_id=last_id, limit=settings.sync_batch_size, site_id=get_site_id())
    try:
        resp = _http_post(
            f"{base}/api/sync/pull",
            pull.model_dump(mode="json"),
            settings.online_sync_api_key,
        )
        if not resp or "events" not in resp:
            return 0, None

        applied = 0
        last_event_id_out = last_id
        db.info["_sync_processing"] = True
        try:
            for ev_data in resp["events"]:
                ev = SyncIncomingEvent(
                    event_id=ev_data["event_id"],
                    site_id=ev_data["site_id"],
                    action=ev_data["action"],
                    table_name=ev_data["table_name"],
                    record_id=ev_data["record_id"],
                    payload_json=ev_data["payload_json"],
                    source_updated_at=datetime.fromisoformat(ev_data["source_updated_at"]),
                )
                db.add(ev)
                db.flush()
                ok, msg = apply_remote_event(db, ev)
                if ok:
                    ev.applied_at = datetime.now(timezone.utc)
                    applied += 1
                    last_event_id_out = ev.event_id
                else:
                    ev.error = msg[:500]
                    log.warning("incoming event %s failed: %s", ev.event_id, msg)
                db.flush()
        finally:
            db.info["_sync_processing"] = False

        state.last_event_id = last_event_id_out
        update_sync_state(
            db,
            is_online=True,
            last_pull_at=datetime.now(timezone.utc),
            last_error=None,
        )
        db.commit()
        return applied, None
    except Exception as exc:
        update_sync_state(db, is_online=False, last_error=str(exc)[:500])
        db.rollback()
        return 0, str(exc)
