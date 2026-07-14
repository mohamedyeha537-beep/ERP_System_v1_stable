#!/usr/bin/env python3
"""
Hermes — جسر ردود /chat (نفس منطق بوت Telegram).

1) شغّل هذا السكربت على السيرفر أو محلياً.
2) في /admin/messaging → محادثة الويب → Webhook n8n:
   http://127.0.0.1:8765/webhook
3) ضع MESSAGING_SECRET من لوحة المراسلات في config.json

عند رسالة زائر من /chat يستقبل POST هنا → smart_reply → POST /api/chat/reply
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.messaging.hermes_replies import smart_reply  # noqa: E402


def load_config() -> dict:
    cfg_path = Path(__file__).with_name("config.json")
    example = Path(__file__).with_name("config.example.json")
    if not cfg_path.exists():
        if example.exists():
            raise SystemExit(
                f"انسخ {example.name} إلى config.json وعدّل pos_url و secret."
            )
        raise SystemExit("أنشئ tools/hermes/config.json")
    return json.loads(cfg_path.read_text(encoding="utf-8"))


def post_chat_reply(cfg: dict, session_token: str, text: str) -> None:
    pos_url = cfg["pos_url"].rstrip("/")
    secret = cfg["messaging_secret"]
    body = json.dumps(
        {"session_token": session_token, "text": text},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{pos_url}/api/chat/reply",
        data=body,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "X-Messaging-Secret": secret,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


class Handler(BaseHTTPRequestHandler):
    config: dict = {}

    def log_message(self, fmt: str, *args) -> None:
        print(f"[Hermes] {self.address_string()} — {fmt % args}")

    def do_POST(self) -> None:
        if self.path.rstrip("/") not in ("/webhook", "/"):
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            self.send_error(400, "JSON غير صالح")
            return

        if payload.get("source") != "web_chat":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok":true,"skipped":true}')
            return

        token = (payload.get("session_token") or "").strip()
        text = (payload.get("text") or "").strip()
        if not token or not text:
            self.send_error(400, "session_token و text مطلوبان")
            return

        reply = smart_reply(text)
        try:
            post_chat_reply(self.config, token, reply)
        except urllib.error.URLError as exc:
            print(f"فشل إرسال الرد إلى POS: {exc}")
            self.send_error(502, "POS unreachable")
            return

        out = json.dumps({"ok": True, "reply": reply}, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(out)


def main() -> None:
    cfg = load_config()
    host = cfg.get("host", "127.0.0.1")
    port = int(cfg.get("port", 8765))
    Handler.config = cfg
    server = HTTPServer((host, port), Handler)
    print(f"Hermes web chat bridge: http://{host}:{port}/webhook")
    print(f"POS: {cfg['pos_url']}")
    server.serve_forever()


if __name__ == "__main__":
    main()
