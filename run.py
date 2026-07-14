import os
import sys

# ZKBioTime وأدوات أخرى قد تضبط PYTHONHOME/PYTHONPATH عالمياً ويكسر Python 3.13
os.environ.pop("PYTHONHOME", None)
os.environ.pop("PYTHONPATH", None)

import socket
import subprocess

import uvicorn

# على Windows: منع مشاركة المنفذ عند فحص التوفر
_WIN_EXCLUSIVE = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)


def _resolve_port() -> int:
    """يشغّل التطبيق على 8011 افتراضياً مع إمكانية تمرير بورت آخر."""
    raw = os.getenv("POS_PORT") or os.getenv("PORT") or "8011"
    if len(sys.argv) > 1:
        raw = sys.argv[1]
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return 8011


_POS_PORTS = tuple(range(8011, 8031))


def _stop_stale_pos_servers() -> None:
    """إيقاف عملية تستمع على منافذ POS (سريع: netstat + taskkill، بدون WMI)."""
    if sys.platform != "win32":
        return
    if os.getenv("POS_SKIP_STALE_KILL", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        return
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return
    if out.returncode != 0 or not out.stdout:
        return

    import re

    port_needles = tuple(f":{p}" for p in _POS_PORTS)
    pids: set[int] = set()
    for line in out.stdout.splitlines():
        if "LISTENING" not in line:
            continue
        if not any(needle in line for needle in port_needles):
            continue
        match = re.search(r"\s+(\d+)\s*$", line)
        if match:
            pids.add(int(match.group(1)))

    for pid in pids:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"],
            capture_output=True,
            timeout=3,
            check=False,
        )
    if pids:
        import time

        time.sleep(0.3)


def _port_is_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if sys.platform == "win32" and _WIN_EXCLUSIVE is not None:
            sock.setsockopt(socket.SOL_SOCKET, _WIN_EXCLUSIVE, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def _pick_port(host: str, preferred: int) -> int:
    """يختار منفذاً متاحاً — يبدأ بالمطلوب ثم بدائل قريبة."""
    candidates = [preferred]
    for alt in (8011, 8020, 8030, 8888, 9010):
        if alt not in candidates:
            candidates.append(alt)
    for port in candidates:
        if _port_is_free(host, port):
            return port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _reload_enabled() -> bool:
    default = "0" if sys.platform == "win32" else "1"
    return os.getenv("POS_RELOAD", default).strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


if __name__ == "__main__":
    _stop_stale_pos_servers()
    host = "127.0.0.1"
    preferred = _resolve_port()
    port = _pick_port(host, preferred)
    if port != preferred:
        print(
            f"تحذير: المنفذ {preferred} مشغول — التشغيل على المنفذ {port}.",
            file=sys.stderr,
        )
    use_reload = _reload_enabled()
    kwargs: dict = {"host": host, "port": port}
    if use_reload:
        kwargs["reload"] = True
        kwargs["reload_excludes"] = [
            "tools/*",
            "*.pyc",
            "__pycache__/*",
            "pos.db",
            "pos.db-*",
            "pos.zip",
            ".git/*",
            "agent-transcripts/*",
        ]
        kwargs["reload_delay"] = 0.5
    print(f"POS: http://{host}:{port}/")

    import logging

    class _QuietPrintAgentPoll(logging.Filter):
        """إخفاء طلبات heartbeat/jobs المتكررة من وكيل الطباعة."""

        def filter(self, record: logging.LogRecord) -> bool:
            msg = record.getMessage()
            if "/api/print-agent/heartbeat" in msg:
                return False
            if "GET /api/print-agent/jobs" in msg and " 200 " in msg:
                return False
            return True

    logging.getLogger("uvicorn.access").addFilter(_QuietPrintAgentPoll())

    uvicorn.run("app.main:app", **kwargs)
