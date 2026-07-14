from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

LOG = logging.getLogger("messaging.webhook")


def send_webhook(
    url: str,
    *,
    method: str = "POST",
    param: str = "text",
    text: str,
    phone: str | None = None,
    channel: str = "whatsapp",
    event: str | None = None,
    customer_id: int | None = None,
    meta: dict | None = None,
    use_json_payload: bool = True,
) -> None:
    """إرسال عبر webhook (CallMeBot GET أو n8n POST JSON)."""
    url = (url or "").strip()
    if not url:
        raise RuntimeError("Webhook URL غير مُعدّ.")

    method = (method or "POST").upper()
    if use_json_payload and method == "POST":
        payload = {
            "text": text,
            "phone": phone or "",
            "channel": channel,
            "event": event or "",
            "customer_id": customer_id,
            "meta": meta or {},
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
    elif method == "GET":
        params = {param or "text": text}
        if phone:
            params["phone"] = phone
        sep = "&" if "?" in url else "?"
        full = url + sep + urllib.parse.urlencode(params)
        req = urllib.request.Request(full, method="GET")
    else:
        body = json.dumps({param or "text": text}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
    with urllib.request.urlopen(req, timeout=8) as resp:
        if resp.status >= 400:
            raise RuntimeError(f"HTTP {resp.status}")
