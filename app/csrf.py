"""حماية CSRF للنماذج — Pure ASGI مع buffer كامل للـ body.

BaseHTTPMiddleware + request.form() في الـ middleware كان يُفقد POST body
عند الوصول إلى routes لاحقة. نقرأ الـ body مرة واحدة، نستخرج CSRF، ثم
نُعيد replay للـ body كاملاً downstream.
"""
from __future__ import annotations

import secrets
from collections.abc import MutableMapping
from urllib.parse import parse_qs, quote

from starlette.datastructures import Headers
from starlette.formparsers import MultiPartParser
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

SESSION_KEY = "csrf_token"
FORM_FIELD = "csrf_token"
HEADER_NAME = "x-csrf-token"

_MAX_CSRF_BODY_BYTES = 20 * 1024 * 1024

_EXEMPT_PREFIXES = (
    "/static/",
    "/uploads/",
    "/docs",
    "/redoc",
    "/openapi.json",
)
_EXEMPT_EXACT = (
    "/auth/logout",
    "/auth/login",
)

_MACHINE_OR_GUEST_API_PREFIXES = (
    "/api/sync",
    "/api/integration",
    "/api/print-agent",
    "/api/messaging",
    "/api/marketing-room",
    "/api/shop",
    "/api/chat",
    "/api/hotel/guest",
    "/api/suites",
    "/api/web-analytics",
)

_MACHINE_AUTH_HEADERS = (
    "x-sync-api-key",
    "x-agent-token",
    "x-api-key",
    "x-messaging-secret",
    "x-seo-api-key",
    "x-integration-api-key",
)


def _soft_redirect_path(path: str, session: MutableMapping | None) -> str | None:
    p = path or ""
    if p == "/pos" or p.startswith("/pos/"):
        return "/pos?ctx_err=" + quote("تعذّر تنفيذ الطلب — أعد المحاولة.")
    for prefix in ("/laundry/pin", "/pos/pin", "/admin/hotel/pin"):
        if p == prefix or p.startswith(prefix + "/"):
            return f"{prefix}?e=csrf"
    if session is not None and session.get("user_id"):
        return "/"
    return None


_CSRF_FORBIDDEN_HTML = (
    "<!DOCTYPE html><html lang='ar' dir='rtl'><head><meta charset='utf-8'/>"
    "<meta http-equiv='refresh' content='0;url=/auth/login?e=csrf'/>"
    "<title>طلب غير صالح</title></head><body>"
    "<p>انتهت صلاحية النموذج أو رمز الحماية غير صحيح. "
    "جاري فتح صفحة الدخول…</p>"
    "<p><a href='/auth/login?e=csrf'>فتح تسجيل الدخول</a> · "
    "<a href='/'>الرئيسية</a></p></body></html>"
)


def ensure_csrf_token(session: MutableMapping) -> str:
    token = str(session.get(SESSION_KEY) or "").strip()
    if not token:
        token = secrets.token_urlsafe(32)
        session[SESSION_KEY] = token
    return token


def verify_csrf_token(session: MutableMapping, provided: str) -> bool:
    expected = str(session.get(SESSION_KEY) or "").strip()
    got = (provided or "").strip()
    if not expected or not got:
        return False
    try:
        return secrets.compare_digest(got, expected)
    except (TypeError, ValueError):
        return False


def _session_mapping(scope: Scope) -> MutableMapping | None:
    session = scope.get("session")
    if isinstance(session, MutableMapping):
        return session
    return None


def _has_machine_auth(scope: Scope) -> bool:
    headers = Headers(scope=scope)
    for name in _MACHINE_AUTH_HEADERS:
        if (headers.get(name) or "").strip():
            return True
    auth = (headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer ") and len(auth) > 7:
        return True
    return False


def _path_exempt(path: str, scope: Scope) -> bool:
    p = path or ""
    if p in _EXEMPT_EXACT or p.rstrip("/") in {x.rstrip("/") for x in _EXEMPT_EXACT}:
        return True
    if any(p.startswith(prefix) for prefix in _EXEMPT_PREFIXES):
        return True
    if any(p.startswith(prefix) for prefix in _MACHINE_OR_GUEST_API_PREFIXES):
        return True
    if p.startswith("/api/") and _has_machine_auth(scope):
        return True
    return False


def _token_from_headers(scope: Scope) -> str:
    headers = Headers(scope=scope)
    return (headers.get(HEADER_NAME) or "").strip()


def _token_from_urlencoded(body: bytes) -> str:
    if not body:
        return ""
    try:
        data = parse_qs(body.decode("utf-8"), keep_blank_values=True)
        vals = data.get(FORM_FIELD) or []
        return vals[0].strip() if vals else ""
    except Exception:
        return ""


async def _bytes_stream(body: bytes):
    yield body


async def _token_from_multipart(content_type: str, body: bytes) -> str:
    if not body:
        return ""
    try:
        headers = Headers({"content-type": content_type})
        parser = MultiPartParser(headers, _bytes_stream(body))
        form = await parser.parse()
        val = form.get(FORM_FIELD)
        if val is None:
            return ""
        return str(val).strip()
    except Exception:
        return ""


async def _csrf_token_from_body(content_type: str, body: bytes) -> str:
    ctype = (content_type or "").lower()
    if "multipart/form-data" in ctype:
        tok = await _token_from_multipart(content_type, body)
        if tok:
            return tok
    return _token_from_urlencoded(body)


async def _read_body(receive: Receive) -> bytes:
    chunks: list[bytes] = []
    more_body = True
    total = 0
    while more_body:
        message: Message = await receive()
        chunk = message.get("body", b"") or b""
        total += len(chunk)
        if total > _MAX_CSRF_BODY_BYTES:
            raise ValueError("request body too large")
        chunks.append(chunk)
        more_body = bool(message.get("more_body", False))
    return b"".join(chunks)


def _replay_receive(body: bytes) -> Receive:
    sent = False

    async def inner() -> Message:
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return inner


class CsrfMiddleware:
    """Pure ASGI — يحافظ على POST body كاملاً لـ downstream routes."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = (scope.get("method") or "GET").upper()
        path = scope.get("path") or ""

        try:
            body = await _read_body(receive)
        except ValueError:
            response = HTMLResponse("حجم الطلب كبير جداً.", status_code=413)
            await response(scope, receive, send)
            return

        replay = _replay_receive(body)

        session = _session_mapping(scope)
        if session is not None:
            ensure_csrf_token(session)

        if method in {"POST", "PUT", "PATCH", "DELETE"} and not _path_exempt(path, scope):
            if session is None:
                response = HTMLResponse("جلسة غير صالحة.", status_code=403)
                await response(scope, replay, send)
                return

            headers = Headers(scope=scope)
            content_type = headers.get("content-type", "")
            provided = await _csrf_token_from_body(content_type, body)
            if not provided:
                provided = _token_from_headers(scope)

            if not verify_csrf_token(session, provided):
                session.pop(SESSION_KEY, None)
                ensure_csrf_token(session)

                is_htmx = (headers.get("hx-request") or "").lower() == "true"
                soft = _soft_redirect_path(path, session)
                if is_htmx:
                    response = Response(
                        status_code=403,
                        headers={"HX-Redirect": soft or "/auth/login?e=csrf"},
                    )
                elif soft:
                    response = RedirectResponse(soft, status_code=303)
                else:
                    response = HTMLResponse(_CSRF_FORBIDDEN_HTML, status_code=403)
                await response(scope, replay, send)
                return

        await self.app(scope, replay, send)
