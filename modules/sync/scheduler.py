from __future__ import annotations

import logging
import threading
import time

from infra.background import with_db
from infra.config import get_settings
from modules.sync import client
from modules.sync.service import get_sync_status

log = logging.getLogger("sync.scheduler")

_scheduler_thread: threading.Thread | None = None
_stop_event = threading.Event()

# عند انقطاع الإنترنت أو وجود أحداث معلّقة نعيد المحاولة بسرعة أكبر
_OFFLINE_RETRY_SECONDS = 15


def _sync_loop():
    while not _stop_event.is_set():
        settings = get_settings()
        if not settings.sync_enabled:
            time.sleep(max(settings.sync_interval_seconds, 10))
            continue

        need_fast_retry = False
        try:
            with with_db() as db:
                status = get_sync_status(db)
                need_fast_retry = (not status.get("online")) or int(status.get("pending_count") or 0) > 0

                pushed, err_push = client.push_pending(db)
                if err_push:
                    need_fast_retry = True
                    log.warning("scheduled push failed: %s", err_push)
                elif pushed:
                    log.info("scheduled push ok: %s events", pushed)

                if settings.sync_pull_enabled:
                    pulled, err_pull = client.pull_remote(db)
                    if err_pull:
                        need_fast_retry = True
                        log.warning("scheduled pull failed: %s", err_pull)
                    elif pulled:
                        log.info("scheduled pull ok: %s events", pulled)
        except Exception as exc:
            need_fast_retry = True
            log.warning("scheduled sync cycle failed: %s", exc)

        delay = (
            _OFFLINE_RETRY_SECONDS
            if need_fast_retry
            else max(settings.sync_interval_seconds, 10)
        )
        # انتظار قابل للمقاطعة عند الإيقاف
        _stop_event.wait(delay)


def start_scheduler() -> threading.Thread | None:
    global _scheduler_thread
    if _scheduler_thread is not None and _scheduler_thread.is_alive():
        return _scheduler_thread
    _stop_event.clear()
    _scheduler_thread = threading.Thread(target=_sync_loop, name="sync-scheduler", daemon=True)
    _scheduler_thread.start()
    return _scheduler_thread


def stop_scheduler() -> None:
    _stop_event.set()
