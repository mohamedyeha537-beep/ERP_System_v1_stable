"""التحقق من عناوين URL قبل طلبات HTTP الخارجية — يمنع SSRF."""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


_BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "127.0.0.1",
        "::1",
        "0.0.0.0",
        "metadata.google.internal",
    }
)


def _hostname_blocked(hostname: str) -> bool:
    host = (hostname or "").strip().lower().rstrip(".")
    if not host:
        return True
    if host in _BLOCKED_HOSTNAMES:
        return True
    if host.endswith(".local") or host.endswith(".internal"):
        return True
    return False


def _ip_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def is_safe_http_url(
    url: str,
    *,
    allow_http: bool = False,
    allowed_hosts: frozenset[str] | None = None,
) -> bool:
    """يرفض file:// والشبكات الداخلية وmetadata link-local."""
    raw = (url or "").strip()
    if not raw:
        return False
    try:
        parsed = urlparse(raw)
    except Exception:
        return False
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        return False
    if scheme == "http" and not allow_http:
        return False
    host = (parsed.hostname or "").strip().lower()
    if _hostname_blocked(host):
        return False
    if allowed_hosts is not None and host not in allowed_hosts:
        return False
    try:
        ip = ipaddress.ip_address(host)
        if _ip_blocked(ip):
            return False
    except ValueError:
        try:
            for info in socket.getaddrinfo(host, parsed.port or (443 if scheme == "https" else 80)):
                addr = info[4][0]
                ip = ipaddress.ip_address(addr)
                if _ip_blocked(ip):
                    return False
        except OSError:
            return False
    return True


def assert_safe_http_url(
    url: str,
    *,
    allow_http: bool = False,
    allowed_hosts: frozenset[str] | None = None,
) -> None:
    if not is_safe_http_url(url, allow_http=allow_http, allowed_hosts=allowed_hosts):
        raise ValueError(f"عنوان URL غير مسموح: {(url or '')[:120]}")
