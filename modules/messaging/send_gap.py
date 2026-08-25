"""فاصل زمني مشترك بين رسائل واتساب الصادرة — يمنع الإرسال المتتالي الفوري."""

from __future__ import annotations

import threading
import time

_LOCK = threading.Lock()
_LAST_SEND_MONO = 0.0

# الحد الأدنى المطلوب بين رسالتين (ثوانٍ)
MIN_SEND_GAP_SEC = 10


def wait_send_gap(seconds: float | None = None) -> None:
    """ينتظر حتى يمرّ الفاصل منذ آخر إرسال واتساب في هذه العملية."""
    global _LAST_SEND_MONO
    gap = float(MIN_SEND_GAP_SEC if seconds is None else seconds)
    if gap <= 0:
        with _LOCK:
            _LAST_SEND_MONO = time.monotonic()
        return
    with _LOCK:
        now = time.monotonic()
        wait = gap - (now - _LAST_SEND_MONO)
        if wait > 0:
            time.sleep(wait)
        _LAST_SEND_MONO = time.monotonic()
