"""حماية API وكلاء SEO — مفتاح + مطابقة البيئة."""
from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, Request

from modules.seo.config import seo_config


def require_seo_agent_headers(
    request: Request,
    x_seo_api_key: str | None = Header(None, alias="X-SEO-API-Key"),
    x_seo_environment: str | None = Header(None, alias="X-SEO-Environment"),
    x_request_id: str | None = Header(None, alias="X-Request-Id"),
) -> dict:
    cfg = seo_config()
    if not cfg["enabled"]:
        raise HTTPException(status_code=503, detail="SEO Agent module is disabled.")
    expected = cfg["api_key"]
    provided = (x_seo_api_key or "").strip()
    if not expected or not provided or not secrets.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="Invalid X-SEO-API-Key.")
    env_hdr = (x_seo_environment or "").strip().lower()
    if env_hdr != cfg["environment"]:
        raise HTTPException(
            status_code=403,
            detail=(
                f"X-SEO-Environment mismatch: got '{env_hdr}', "
                f"expected '{cfg['environment']}'."
            ),
        )
    rid = (x_request_id or "").strip() or None
    request.state.seo_request_id = rid
    request.state.seo_environment = cfg["environment"]
    return {
        "environment": cfg["environment"],
        "request_id": rid,
        "api_ok": True,
    }
