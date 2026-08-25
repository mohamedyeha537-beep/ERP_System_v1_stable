#!/usr/bin/env python3
"""وكيل محلي لبرمجة بطاقات أقفال الشقق عبر proRFL.dll (USB).

شغّله على جهاز الاستقبال المتصل بمبرمج البطاقات:
  python door_lock_agent.py

الإعداد: config.json بجانب هذا الملف (انظر config.json.example).
"""
from __future__ import annotations

import argparse
import ctypes
import json
import logging
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LOG = logging.getLogger("door_lock_agent")
ROOT = Path(__file__).resolve().parent


def load_config(path: Path) -> dict:
    if not path.exists():
        return {
            "host": "127.0.0.1",
            "port": 9199,
            "dll_path": "",
            "usb_flag": 1,
            "mock": False,
        }
    with path.open(encoding="utf-8") as f:
        return json.load(f)


class ProRFL:
    """غلاف ctypes لـ proRFL.dll (stdcall)."""

    def __init__(self, dll_path: Path, *, mock: bool = False):
        self.mock = mock
        self.dll = None
        self._lock = threading.Lock()
        if mock:
            LOG.warning("وضع المحاكاة (mock) — لن تُكتب بطاقة حقيقية")
            return
        if not dll_path.exists():
            raise FileNotFoundError(f"DLL غير موجود: {dll_path}")
        self.dll = ctypes.WinDLL(str(dll_path))
        self._bind()

    def _bind(self) -> None:
        d = self.dll
        d.GetDLLVersion.argtypes = [ctypes.c_char_p]
        d.GetDLLVersion.restype = ctypes.c_int
        d.initializeUSB.argtypes = [ctypes.c_ubyte]
        d.initializeUSB.restype = ctypes.c_int
        d.CloseUSB.argtypes = [ctypes.c_ubyte]
        d.CloseUSB.restype = None
        d.Buzzer.argtypes = [ctypes.c_ubyte, ctypes.c_int]
        d.Buzzer.restype = ctypes.c_int
        d.ReadCard.argtypes = [ctypes.c_ubyte, ctypes.c_char_p]
        d.ReadCard.restype = ctypes.c_int
        d.GuestCard.argtypes = [
            ctypes.c_ubyte,
            ctypes.c_int,
            ctypes.c_ubyte,
            ctypes.c_ubyte,
            ctypes.c_ubyte,
            ctypes.c_ubyte,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
        ]
        d.GuestCard.restype = ctypes.c_int
        d.CardErase.argtypes = [ctypes.c_ubyte, ctypes.c_int, ctypes.c_char_p]
        d.CardErase.restype = ctypes.c_int
        d.GetCardTypeByCardDataStr.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        d.GetCardTypeByCardDataStr.restype = ctypes.c_int
        d.GetGuestLockNoByCardDataStr.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_char_p,
        ]
        d.GetGuestLockNoByCardDataStr.restype = ctypes.c_int
        d.GetGuestETimeByCardDataStr.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_char_p,
        ]
        d.GetGuestETimeByCardDataStr.restype = ctypes.c_int

    def version(self) -> str:
        if self.mock:
            return "mock-1.0"
        buf = ctypes.create_string_buffer(128)
        st = self.dll.GetDLLVersion(buf)
        if st != 0:
            return f"err:{st}"
        return buf.value.decode("ascii", errors="replace")

    def open_usb(self, usb_flag: int) -> None:
        if self.mock:
            return
        st = self.dll.initializeUSB(ctypes.c_ubyte(usb_flag))
        if st != 0:
            raise RuntimeError(f"فشل فتح USB (كود {st})")

    def close_usb(self, usb_flag: int) -> None:
        if self.mock or self.dll is None:
            return
        try:
            self.dll.CloseUSB(ctypes.c_ubyte(usb_flag))
        except Exception:
            pass

    def buzz(self, usb_flag: int, t: int = 20) -> None:
        if self.mock:
            return
        try:
            self.dll.Buzzer(ctypes.c_ubyte(usb_flag), int(t))
        except Exception:
            pass

    def read_card(self, usb_flag: int) -> str:
        if self.mock:
            return "MOCK0101" + "0" * 40
        buf = ctypes.create_string_buffer(128)
        st = self.dll.ReadCard(ctypes.c_ubyte(usb_flag), buf)
        if st != 0:
            raise RuntimeError(f"فشل قراءة البطاقة (كود {st}) — ضع البطاقة على الجهاز")
        data = buf.value.decode("ascii", errors="replace")
        if len(data) >= 6 and data[4:6] != "01":
            raise RuntimeError("لا توجد بطاقة صالحة على القارئ")
        return data

    def guest_card(self, payload: dict) -> dict:
        usb = int(payload.get("usb_flag", 1))
        with self._lock:
            self.open_usb(usb)
            try:
                # قراءة أولاً كما في مثال الشركة
                card_data = self.read_card(usb)
                out = ctypes.create_string_buffer(128)
                if self.mock:
                    self.buzz(usb)
                    return {
                        "ok": True,
                        "message": "تمت برمجة البطاقة (محاكاة)",
                        "card_data": "MOCK_GUEST",
                        "read_before": card_data,
                    }
                begin = str(payload["begin"]).encode("ascii")
                end = str(payload["end"]).encode("ascii")
                lock_no = str(payload["lock_no"]).encode("ascii")
                st = self.dll.GuestCard(
                    ctypes.c_ubyte(usb),
                    int(payload["co_id"]),
                    ctypes.c_ubyte(int(payload.get("card_no", 0)) % 256),
                    ctypes.c_ubyte(int(payload.get("dai", 0)) % 256),
                    ctypes.c_ubyte(int(payload.get("llock", 0))),
                    ctypes.c_ubyte(int(payload.get("pdoors", 1))),
                    begin,
                    end,
                    lock_no,
                    out,
                )
                self.buzz(usb)
                if st != 0:
                    raise RuntimeError(f"فشل GuestCard (كود {st})")
                return {
                    "ok": True,
                    "message": "تمت برمجة بطاقة النزيل بنجاح",
                    "card_data": out.value.decode("ascii", errors="replace"),
                    "read_before": card_data,
                    "lock_no": payload.get("lock_no"),
                    "end": payload.get("end"),
                }
            finally:
                self.close_usb(usb)

    def erase(self, payload: dict) -> dict:
        usb = int(payload.get("usb_flag", 1))
        with self._lock:
            self.open_usb(usb)
            try:
                self.read_card(usb)
                out = ctypes.create_string_buffer(128)
                if self.mock:
                    self.buzz(usb)
                    return {"ok": True, "message": "تم مسح البطاقة (محاكاة)", "card_data": ""}
                st = self.dll.CardErase(
                    ctypes.c_ubyte(usb),
                    int(payload["co_id"]),
                    out,
                )
                self.buzz(usb)
                if st != 0:
                    raise RuntimeError(f"فشل مسح البطاقة (كود {st})")
                return {
                    "ok": True,
                    "message": "تم مسح البطاقة",
                    "card_data": out.value.decode("ascii", errors="replace"),
                }
            finally:
                self.close_usb(usb)

    def read_info(self, payload: dict) -> dict:
        usb = int(payload.get("usb_flag", 1))
        co_id = int(payload.get("co_id") or 0)
        with self._lock:
            self.open_usb(usb)
            try:
                data = self.read_card(usb)
                info: dict = {"ok": True, "message": "تمت القراءة", "card_data": data}
                if self.mock:
                    info["card_type"] = "6"
                    info["lock_no"] = "01020399"
                    info["expiry"] = "9912311200"
                    return info
                ctype = ctypes.create_string_buffer(16)
                if self.dll.GetCardTypeByCardDataStr(data.encode("ascii"), ctype) == 0:
                    info["card_type"] = ctype.value.decode("ascii", errors="replace")
                if co_id:
                    lock_buf = ctypes.create_string_buffer(32)
                    if (
                        self.dll.GetGuestLockNoByCardDataStr(
                            co_id, data.encode("ascii"), lock_buf
                        )
                        == 0
                    ):
                        info["lock_no"] = lock_buf.value.decode("ascii", errors="replace")
                    exp_buf = ctypes.create_string_buffer(32)
                    if (
                        self.dll.GetGuestETimeByCardDataStr(
                            co_id, data.encode("ascii"), exp_buf
                        )
                        == 0
                    ):
                        info["expiry"] = exp_buf.value.decode("ascii", errors="replace")
                self.buzz(usb, 15)
                return info
            finally:
                self.close_usb(usb)


def make_handler(api: ProRFL, default_usb: int):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:
            LOG.info("%s - %s", self.address_string(), fmt % args)

        def _json(self, code: int, payload: dict) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_OPTIONS(self) -> None:  # noqa: N802
            self._json(204, {})

        def do_GET(self) -> None:  # noqa: N802
            if self.path.rstrip("/") in ("", "/status", "/health"):
                self._json(
                    200,
                    {
                        "ok": True,
                        "service": "door_lock_agent",
                        "version": api.version(),
                        "mock": api.mock,
                        "usb_flag": default_usb,
                    },
                )
                return
            self._json(404, {"ok": False, "message": "not found"})

        def _read_body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8"))
            except Exception:
                return {}

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.rstrip("/")
            body = self._read_body()
            body.setdefault("usb_flag", default_usb)
            try:
                if path == "/guest-card":
                    self._json(200, api.guest_card(body))
                elif path == "/erase":
                    self._json(200, api.erase(body))
                elif path == "/read":
                    self._json(200, api.read_info(body))
                else:
                    self._json(404, {"ok": False, "message": "not found"})
            except Exception as exc:
                LOG.exception("agent error")
                self._json(500, {"ok": False, "message": str(exc)})

    return Handler


def resolve_dll(cfg: dict) -> Path:
    configured = (cfg.get("dll_path") or "").strip()
    if configured:
        return Path(configured)
    # مسارات شائعة داخل المشروع
    candidates = [
        ROOT / "proRFL.dll",
        ROOT.parent.parent
        / "New Lock System SDK [V0921]"
        / "Locks SDK [V0921]"
        / "DLL File"
        / "proRFL.dll",
        ROOT.parent.parent
        / "New Lock System SDK [V0921]"
        / "Locks SDK [V0921]"
        / "Example"
        / "Delphi 7.0"
        / "proRFL.dll",
    ]
    for p in candidates:
        if p.exists():
            return p
    return candidates[0]


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    parser = argparse.ArgumentParser(description="Door lock card encoder agent")
    parser.add_argument(
        "--config",
        default=str(ROOT / "config.json"),
        help="مسار config.json",
    )
    parser.add_argument("--mock", action="store_true", help="محاكاة بدون DLL")
    args = parser.parse_args()
    cfg = load_config(Path(args.config))
    mock = bool(args.mock or cfg.get("mock"))
    if sys.platform != "win32" and not mock:
        LOG.error("هذا الوكيل يحتاج ويندوز + proRFL.dll — أو شغّل بـ --mock للتجربة")
        return 2
    dll_path = resolve_dll(cfg)
    try:
        api = ProRFL(dll_path, mock=mock)
    except Exception as exc:
        LOG.error("تعذّر تحميل DLL: %s", exc)
        LOG.error("انسخ proRFL.dll (و d12.dll / d12c.dll إن لزم) إلى tools/door_lock_agent/")
        return 1
    host = cfg.get("host") or "127.0.0.1"
    port = int(cfg.get("port") or 9199)
    usb = int(cfg.get("usb_flag") or 1)
    server = ThreadingHTTPServer((host, port), make_handler(api, usb))
    LOG.info("Door lock agent: http://%s:%s/  dll=%s mock=%s", host, port, dll_path, mock)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
