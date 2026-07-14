from __future__ import annotations

import logging
import threading
import time

from infra.background import with_db
from infra.config import get_settings
from modules.sync import client

log = logging.getLogger("sync.scheduler")

_scheduler_thread: threading.Thread | None = None
_stop_event = threading.Event()


def _sync_loop():
    while not _stop_event.is_set():
        settings = get_settings()
        if not settings.sync_enabled:
            time.sleep(max(settings.sync_interval_seconds, 10))
            continue
        try:
            with with_db() as db:
                client.push_pending(db)
        except Exception as exc:
            log.warning("scheduled push failed: %s", exc)

        if settings.sync_pull_enabled:
            try:
                with with_db() as db:
                    client.pull_remote(db)
            except Exception as exc:
                log.warning("scheduled pull failed: %s", exc)

        time.sleep(max(settings.sync_interval_seconds, 10))


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
