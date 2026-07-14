from __future__ import annotations

import json
import urllib.error
import urllib.request


def send_telegram_message(
    bot_token: str,
    chat_id: str,
    text: str,
) -> None:
    token = (bot_token or "").strip()
    cid = (chat_id or "").strip()
    if not token or not cid:
        raise RuntimeError("Telegram bot token أو chat_id غير مُعدّ.")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.dumps(
        {"chat_id": cid, "text": text, "parse_mode": "HTML"},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=8) as resp:
        raw = resp.read().decode("utf-8")
        data = json.loads(raw) if raw else {}
        if not data.get("ok"):
            raise RuntimeError(data.get("description") or "Telegram API error")
