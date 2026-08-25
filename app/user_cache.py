"""ذاكرة مؤقتة للمستخدم الحالي — قابلة للإبطال عند تغيير الصلاحيات."""
from __future__ import annotations

import time

_USER_CACHE: dict[int, tuple[float, object]] = {}
_USER_CACHE_TTL = 60.0


def cache_get(user_id: int) -> object | None:
    hit = _USER_CACHE.get(int(user_id))
    if hit is None:
        return None
    ts, user = hit
    if (time.monotonic() - ts) >= _USER_CACHE_TTL:
        _USER_CACHE.pop(int(user_id), None)
        return None
    return user


def cache_set(user_id: int, user: object) -> None:
    _USER_CACHE[int(user_id)] = (time.monotonic(), user)


def invalidate_user_cache(user_id: int | None = None) -> None:
    """إبطال مستخدم واحد أو كل الذاكرة."""
    if user_id is None:
        _USER_CACHE.clear()
        return
    _USER_CACHE.pop(int(user_id), None)


# توافق مع الاستدعاءات القديمة
cache_invalidate = invalidate_user_cache
cache_drop_stale = invalidate_user_cache
