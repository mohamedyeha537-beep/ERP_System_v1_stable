from __future__ import annotations

import json
import urllib.error
import urllib.request

from modules.common.safe_http import open_safe_http
from modules.common.safe_http_url import assert_safe_http_url


def send_telegram_message(
    bot_token: str,
    chat_id: str,
    text: str,
) -> None:
    token = (bot_token or "").strip()
    cid = (chat_id or "").strip()
    if not token or not cid:
        raise RuntimeError("Telegram bot token أو chat_id غير مُعدّ.")
    # منع حقن مسار عبر التوكن
    if "/" in token or "\\" in token or ".." in token:
        raise RuntimeError("Telegram bot token غير صالح.")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    assert_safe_http_url(url, allow_http=False)
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
    with open_safe_http(req, timeout=8, allow_http=False) as resp:
        raw = resp.read().decode("utf-8")
        data = json.loads(raw) if raw else {}
        if not data.get("ok"):
            raise RuntimeError(data.get("description") or "Telegram API error")
