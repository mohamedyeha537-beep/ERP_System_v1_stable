"""تسجيل عمليات وكلاء SEO."""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from modules.seo.models import SeoAgentLog


def log_seo_action(
    db: Session,
    *,
    environment: str,
    agent_name: str,
    action: str,
    request_id: str | None = None,
    status: str = "ok",
    message: str | None = None,
    payload: Any = None,
) -> SeoAgentLog:
    raw = None
    if payload is not None:
        try:
            raw = json.dumps(payload, ensure_ascii=False, default=str)[:8000]
        except Exception:  # noqa: BLE001
            raw = str(payload)[:8000]
    row = SeoAgentLog(
        environment=environment,
        agent_name=(agent_name or "")[:80],
        action=(action or "")[:80],
        request_id=(request_id or None),
        status=(status or "ok")[:30],
        message=(message or None),
        payload_json=raw,
    )
    db.add(row)
    db.flush()
    return row
