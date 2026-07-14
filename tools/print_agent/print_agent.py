#!/usr/bin/env python3
"""وكيل طباعة محلي — يسحب print_jobs من السيرفر ويطبع ESC/POS على الشبكة."""
from __future__ import annotations

import json
import logging
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

LOG = logging.getLogger("print_agent")


def load_config(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def api_request(
    cfg: dict,
    method: str,
    path: str,
    body: dict | None = None,
) -> dict:
    url = cfg["server_url"].rstrip("/") + path
    data = None
    headers = {
        "X-Agent-Token": cfg["agent_token"],
        "Accept": "application/json",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}


def resolve_printer_target(cfg: dict, job: dict) -> tuple[str, int | str, int]:
    """يُرجع (mode, target, port) — mode: tcp أو windows."""
    payload_raw = job.get("payload") or ""
    host, port = None, 9100
    windows_name: str | None = None
    try:
        payload = json.loads(payload_raw)
        meta = payload.get("printer") or {}
        key = meta.get("local_printer_key") or job.get("local_printer_key")
        printer_cfg = cfg.get("printers", {}).get(key, {}) if key else {}
        windows_name = (
            printer_cfg.get("windows_name")
            or printer_cfg.get("name")
            or cfg.get("fallback_windows_name")
            or ""
        ).strip() or None
        prefer_windows = bool(
            printer_cfg.get("prefer_windows_name") or cfg.get("prefer_windows_name")
        )
        if prefer_windows and windows_name:
            return "windows", windows_name, 0
        if meta.get("ip_address"):
            host = meta["ip_address"]
            port = int(meta.get("port") or 9100)
        elif key and key in cfg.get("printers", {}):
            p = cfg["printers"][key]
            host = p.get("host")
            port = int(p.get("port", 9100))
            windows_name = (p.get("windows_name") or p.get("name") or "").strip() or None
        elif key and sys.platform == "win32":
            windows_name = key.strip()
    except json.JSONDecodeError:
        pass
    if windows_name and not host:
        return "windows", windows_name, 0
    if not host:
        raise ValueError(
            "لا عنوان طابعة في المهمة أو config.json — "
            "أضف IP أو windows_name / اسم طابعة ويندوز في local_printer_key"
        )
    return "tcp", host, port


def parse_job_payload(job: dict) -> tuple[str, str | None, str | None, dict]:
    payload_raw = job.get("payload") or ""
    try:
        payload = json.loads(payload_raw)
        if isinstance(payload, dict):
            text = str(payload.get("text") or "")
            logo = (payload.get("logo_url") or "").strip() or None
            img = (payload.get("image_b64") or "").strip() or None
            if payload.get("print_mode") == "raster" and img:
                return "", None, img, payload
            return text, logo, None, payload
    except json.JSONDecodeError:
        pass
    return payload_raw, None, None, {}


def extract_text(job: dict) -> str:
    text, _, _, _ = parse_job_payload(job)
    return text


def _sanitize_line_for_escpos(line: str) -> str:
    """رموز لا تدعمها معظم طابعات POS-80 الحرارية."""
    import re

    line = re.sub(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069\ufeff]", "", line)
    return (
        line.replace("\u2014", "-")
        .replace("\u2013", "-")
        .replace("\u2190", "<")
        .replace("\u2192", ">")
        .replace("\U0001f4de", "Tel:")
        .replace("📞", "Tel:")
        .replace("↳", ">")
        .replace("—", "-")
    )


def _resolve_profile(cfg: dict, job: dict) -> str:
    """ملف ترميز ESC/POS — انظر config.json."""
    profile = str(cfg.get("escpos_profile") or "cp1256_22").strip().lower()
    try:
        payload = json.loads(job.get("payload") or "{}")
        key = (payload.get("printer") or {}).get("local_printer_key") or job.get(
            "local_printer_key"
        )
        if key and key in cfg.get("printers", {}):
            p = cfg["printers"][key]
            if p.get("profile"):
                profile = str(p["profile"]).strip().lower()
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    return profile


def _printer_cfg_for_job(cfg: dict, job: dict) -> dict:
    try:
        payload = json.loads(job.get("payload") or "{}")
        key = (payload.get("printer") or {}).get("local_printer_key") or job.get(
            "local_printer_key"
        )
        if key and key in cfg.get("printers", {}):
            return cfg["printers"][key] or {}
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    return {}


def _resolve_raster_threshold(cfg: dict, job: dict) -> int:
    """كلما زادت القيمة زادت سواد طباعة الصور الحرارية."""
    val = _printer_cfg_for_job(cfg, job).get("raster_threshold")
    if val is None:
        val = cfg.get("raster_threshold", 160)
    try:
        return max(1, min(254, int(val)))
    except (TypeError, ValueError):
        return 160


def _encode_lines(text: str, encoding: str) -> list[bytes]:
    lines_out: list[bytes] = []
    for line in text.splitlines():
        clean = _sanitize_line_for_escpos(line)
        try:
            lines_out.append(clean.encode(encoding, errors="strict"))
        except (LookupError, UnicodeEncodeError):
            lines_out.append(clean.encode("cp1256", errors="replace"))
    return lines_out


def escpos_encode(text: str, cfg: dict, job: dict) -> bytes:
    """بناء أوامر ESC/POS حسب ملف الترميز (افتراضي UTF-8 لـ POS-80)."""
    profile = _resolve_profile(cfg, job)
    out = bytearray(b"\x1b\x40")  # init
    out.extend(b"\x1b\x21\x00")  # خط عادي

    if profile == "utf8":
        # Xprinter / POS-80: وضع UTF-8 (الأنسب لمعظم الطابعات الصينية)
        out.extend(b"\x1c\x26")
        out.extend(b"\x1c\x43\xff")
        out.extend(b"\x1b\x61\x02")  # محاذاة يمين للعربية
        for chunk in _encode_lines(text, "utf-8"):
            out.extend(chunk + b"\n")
    elif profile == "cp720_21":
        out.extend(bytes([0x1B, 0x74, 21]))
        out.extend(b"\x1b\x61\x02")
        for chunk in _encode_lines(text, "cp720"):
            out.extend(chunk + b"\n")
    elif profile == "cp1256_22":
        out.extend(bytes([0x1B, 0x74, 22]))
        out.extend(b"\x1b\x61\x02")
        for chunk in _encode_lines(text, "cp1256"):
            out.extend(chunk + b"\n")
    else:
        # توافق قديم: escpos_encoding + escpos_code_table
        enc = str(cfg.get("escpos_encoding") or "cp1256").strip()
        try:
            table = int(cfg.get("escpos_code_table", 22))
        except (TypeError, ValueError):
            table = 22
        out.extend(bytes([0x1B, 0x74, table & 0xFF]))
        out.extend(b"\x1b\x61\x02")
        for chunk in _encode_lines(text, enc):
            out.extend(chunk + b"\n")

    out.extend(b"\n\n\x1dV\x00")
    return bytes(out)


def send_raw_tcp(host: str, port: int, data: bytes) -> None:
    with socket.create_connection((host, port), timeout=8) as sock:
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        sock.sendall(data)


def send_raw_windows(printer_name: str, data: bytes) -> None:
    try:
        import win32print  # type: ignore[import-untyped]
    except ImportError as exc:
        raise RuntimeError(
            "لطباعة على طابعة ويندوز ثبّت: pip install pywin32"
        ) from exc
    requested_name = (printer_name or "").strip()
    if not requested_name or requested_name == "__DEFAULT__":
        requested_name = win32print.GetDefaultPrinter()
    try:
        h = win32print.OpenPrinter(requested_name)
    except Exception:
        default_name = win32print.GetDefaultPrinter()
        if default_name and default_name != requested_name:
            LOG.warning(
                "Windows printer %r not found — trying default %r",
                requested_name,
                default_name,
            )
            h = win32print.OpenPrinter(default_name)
        else:
            raise
    try:
        win32print.StartDocPrinter(h, 1, ("POS Receipt", None, "RAW"))
        try:
            win32print.StartPagePrinter(h)
            win32print.WritePrinter(h, data)
            win32print.EndPagePrinter(h)
        finally:
            win32print.EndDocPrinter(h)
    finally:
        win32print.ClosePrinter(h)


def process_job(cfg: dict, job: dict) -> None:
    from escpos_raster import build_logo_block, build_receipt_raster_block

    jid = job["id"]
    try:
        api_request(cfg, "POST", f"/api/print-agent/jobs/{jid}/claim")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            LOG.info("job %s skipped — already claimed or finished", jid)
            return
        raise
    mode, target, port = resolve_printer_target(cfg, job)
    text, logo_url, image_b64, payload = parse_job_payload(job)
    paper = 80
    try:
        paper = int((payload.get("printer") or {}).get("paper_width") or 80)
    except (TypeError, ValueError, AttributeError):
        pass
    if image_b64:
        raw = build_receipt_raster_block(
            image_b64, paper, threshold=_resolve_raster_threshold(cfg, job)
        )
        tag = " (raster)"
    else:
        logo_block = build_logo_block(
            cfg, logo_url, paper, threshold=_resolve_raster_threshold(cfg, job)
        )
        raw = logo_block + escpos_encode(text, cfg, job)
        tag = ""
    if mode == "windows":
        send_raw_windows(str(target), raw)
        LOG.info("printed job %s -> Windows[%s]%s", jid, target, tag)
    else:
        try:
            send_raw_tcp(str(target), int(port), raw)
            LOG.info("printed job %s -> %s:%s%s", jid, target, port, tag)
        except Exception as exc:
            fallback = (cfg.get("fallback_windows_name") or "").strip()
            if not fallback and cfg.get("prefer_windows_name"):
                fallback = "__DEFAULT__"
            if not fallback:
                raise
            LOG.warning(
                "TCP print failed for job %s (%s) — trying Windows fallback %r",
                jid,
                exc,
                fallback,
            )
            send_raw_windows(fallback, raw)
            LOG.info("printed job %s -> Windows[%s]%s", jid, fallback, tag)
    api_request(cfg, "POST", f"/api/print-agent/jobs/{jid}/printed")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(
                Path(__file__).resolve().parent / "print_agent.log",
                encoding="utf-8",
            ),
        ],
    )
    cfg_path = Path(__file__).resolve().parent / "config.json"
    if not cfg_path.exists():
        LOG.error("أنشئ config.json من config.json.example")
        return 1
    cfg = load_config(cfg_path)
    poll_interval = float(cfg.get("poll_interval_seconds", 2))
    heartbeat_interval = float(cfg.get("heartbeat_interval_seconds", 30))
    LOG.info(
        "وكيل الطباعة يعمل — السيرفر: %s (استعلام كل %.1fs)",
        cfg.get("server_url"),
        poll_interval,
    )
    last_heartbeat = 0.0
    while True:
        try:
            now = time.monotonic()
            if now - last_heartbeat >= heartbeat_interval:
                api_request(
                    cfg,
                    "POST",
                    "/api/print-agent/heartbeat",
                    {"hostname": socket.gethostname(), "version": "1.0"},
                )
                last_heartbeat = now
            data = api_request(cfg, "GET", "/api/print-agent/jobs")
            for job in data.get("jobs") or []:
                try:
                    process_job(cfg, job)
                except Exception as exc:
                    LOG.exception("job %s failed: %s", job.get("id"), exc)
                    try:
                        api_request(
                            cfg,
                            "POST",
                            f"/api/print-agent/jobs/{job['id']}/failed",
                            {"error_message": str(exc)[:500]},
                        )
                    except Exception:
                        LOG.exception("could not report failure")
        except urllib.error.HTTPError as exc:
            LOG.warning("HTTP %s: %s", exc.code, exc.read().decode("utf-8", errors="replace"))
        except Exception as exc:
            LOG.warning("poll error: %s", exc)
        time.sleep(poll_interval)


if __name__ == "__main__":
    sys.exit(main())
