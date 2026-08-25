"""حماية CSRF للنماذج — token في الجلسة + حقول مخفية تلقائية."""
from __future__ import annotations

import secrets

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

SESSION_KEY = "csrf_token"
FORM_FIELD = "csrf_token"
HEADER_NAME = "x-csrf-token"

_EXEMPT_PREFIXES = (
    "/static/",
    "/uploads/",
    "/api/",
    "/docs",
    "/redoc",
    "/openapi.json",
)

_EXEMPT_EXACT = (
    "/auth/logout",
)


def ensure_csrf_token(session: dict) -> str:
    token = (session.get(SESSION_KEY) or "").strip()
    if not token:
        token = secrets.token_urlsafe(32)
        session[SESSION_KEY] = token
    return token


def _path_exempt(path: str) -> bool:
    p = path or ""
    if p in _EXEMPT_EXACT:
        return True
    return any(p.startswith(prefix) for prefix in _EXEMPT_PREFIXES)


def _extract_provided_token(request: Request) -> str:
    header = (request.headers.get(HEADER_NAME) or "").strip()
    if header:
        return header
    # multipart/form-data — starlette parses lazily; use query for rare cases
    return (request.query_params.get(FORM_FIELD) or "").strip()


async def _extract_form_token(request: Request) -> str:
    ctype = (request.headers.get("content-type") or "").lower()
    if "application/x-www-form-urlencoded" in ctype or "multipart/form-data" in ctype:
        try:
            form = await request.form()
            val = form.get(FORM_FIELD)
            if val is not None:
                return str(val).strip()
        except Exception:
            return ""
    return _extract_provided_token(request)


def verify_csrf_token(request: Request, session: dict, provided: str) -> bool:
    expected = (session.get(SESSION_KEY) or "").strip()
    if not expected or not provided:
        return False
    return secrets.compare_digest(provided, expected)


class CsrfMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        session = getattr(request, "session", None)
        if isinstance(session, dict):
            ensure_csrf_token(session)

        method = (request.method or "GET").upper()
        path = request.url.path or ""
        if method in {"POST", "PUT", "PATCH", "DELETE"} and not _path_exempt(path):
            if not isinstance(session, dict):
                return HTMLResponse("جلسة غير صالحة.", status_code=403)
            provided = await _extract_form_token(request)
            if not verify_csrf_token(request, session, provided):
                return HTMLResponse(
                    "<!DOCTYPE html><html lang='ar' dir='rtl'><head><meta charset='utf-8'/>"
                    "<title>طلب غير صالح</title></head><body>"
                    "<p>انتهت صلاحية النموذج أو رمز الحماية غير صحيح. "
                    "حدّث الصفحة وحاول مرة أخرى.</p>"
                    "<p><a href='javascript:history.back()'>رجوع</a> · "
                    "<a href='/'>الرئيسية</a></p></body></html>",
                    status_code=403,
                )
        return await call_next(request)
