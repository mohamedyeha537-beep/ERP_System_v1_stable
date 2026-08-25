"""Rate limiting بسيط في الذاكرة — للمسارات الحساسة والعامة."""
from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


@dataclass(frozen=True)
class RateRule:
    prefix: str
    methods: frozenset[str]
    max_hits: int
    window_seconds: int


# (prefix, methods, max_hits, window_seconds)
_RULES: tuple[RateRule, ...] = (
    RateRule("/auth/login", frozenset({"POST"}), 5, 60),
    RateRule("/api/integration/", frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"}), 100, 3600),
    RateRule("/api/shop/", frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"}), 30, 60),
    RateRule("/api/messaging/", frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"}), 30, 60),
    RateRule("/api/hotel/housekeeping/done/", frozenset({"GET", "POST"}), 10, 3600),
)

_buckets: dict[str, deque[float]] = defaultdict(deque)


def _client_ip(request: Request) -> str:
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if forwarded:
        return forwarded
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _bucket_key(request: Request, rule: RateRule) -> str:
    ip = _client_ip(request)
    api_key = (request.headers.get("x-api-key") or "").strip()
    if rule.prefix.startswith("/api/integration/") and api_key:
        return f"{rule.prefix}:key:{api_key[:16]}"
    return f"{rule.prefix}:ip:{ip}"


def _match_rule(method: str, path: str) -> RateRule | None:
    m = method.upper()
    for rule in _RULES:
        if path.startswith(rule.prefix) and m in rule.methods:
            return rule
    return None


def _is_limited(request: Request) -> RateRule | None:
    rule = _match_rule(request.method or "GET", request.url.path or "")
    if rule is None:
        return None
    now = time.monotonic()
    key = _bucket_key(request, rule)
    hits = _buckets[key]
    cutoff = now - rule.window_seconds
    while hits and hits[0] < cutoff:
        hits.popleft()
    if len(hits) >= rule.max_hits:
        return rule
    hits.append(now)
    return None


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        rule = _is_limited(request)
        if rule is not None:
            if (request.url.path or "").startswith("/api/"):
                return JSONResponse(
                    {"detail": "تم تجاوز حد الطلبات. حاول لاحقاً."},
                    status_code=429,
                )
            return Response(
                "تم تجاوز حد الطلبات. حاول لاحقاً.",
                status_code=429,
                media_type="text/plain; charset=utf-8",
            )
        return await call_next(request)
