"""قفل مؤقت بعد محاولات تسجيل دخول فاشلة."""
from __future__ import annotations

import time
from collections import defaultdict, deque

_LOCKOUT_THRESHOLD = 10
_LOCKOUT_WINDOW_SEC = 900
_LOCKOUT_DURATION_SEC = 900

_attempts: dict[str, deque[float]] = defaultdict(deque)
_locked_until: dict[str, float] = {}


def _key(ip: str, username: str) -> str:
    return f"{(ip or 'unknown').strip()}:{(username or '').strip().lower()}"


def is_locked(ip: str, username: str) -> bool:
    key = _key(ip, username)
    until = _locked_until.get(key, 0.0)
    now = time.monotonic()
    if until > now:
        return True
    if until:
        _locked_until.pop(key, None)
    return False


def record_failure(ip: str, username: str) -> None:
    key = _key(ip, username)
    now = time.monotonic()
    hits = _attempts[key]
    cutoff = now - _LOCKOUT_WINDOW_SEC
    while hits and hits[0] < cutoff:
        hits.popleft()
    hits.append(now)
    if len(hits) >= _LOCKOUT_THRESHOLD:
        _locked_until[key] = now + _LOCKOUT_DURATION_SEC
        hits.clear()


def record_success(ip: str, username: str) -> None:
    key = _key(ip, username)
    _attempts.pop(key, None)
    _locked_until.pop(key, None)
