"""فتح HTTP آمن مع رفض إعادة التوجيه إلى شبكات داخلية."""
from __future__ import annotations

import ssl
import urllib.request
from typing import Any

from modules.common.safe_http_url import assert_safe_http_url


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _redirect_handler(
    *,
    allow_http: bool,
    allowed_hosts: frozenset[str] | None,
) -> urllib.request.HTTPRedirectHandler:
    class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
            assert_safe_http_url(
                newurl,
                allow_http=allow_http,
                allowed_hosts=allowed_hosts,
            )
            return urllib.request.HTTPRedirectHandler.redirect_request(
                self, req, fp, code, msg, headers, newurl
            )

    return _SafeRedirectHandler()


def open_safe_http(
    req: urllib.request.Request,
    *,
    timeout: float = 8,
    allow_http: bool = True,
    allowed_hosts: frozenset[str] | None = None,
) -> Any:
    """urlopen بعد التحقق من URL الهدف (ومتابعة redirects الآمنة فقط)."""
    assert_safe_http_url(
        req.full_url,
        allow_http=allow_http,
        allowed_hosts=allowed_hosts,
    )
    https_handler = urllib.request.HTTPSHandler(context=_ssl_context())
    opener = urllib.request.build_opener(
        _redirect_handler(allow_http=allow_http, allowed_hosts=allowed_hosts),
        https_handler,
    )
    return opener.open(req, timeout=timeout)
