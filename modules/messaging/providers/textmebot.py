"""إرسال واتساب مباشرة عبر TextMeBot API (نص، صورة، أزرار Pro، نسخ كود)."""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import TypedDict

from modules.messaging.send_gap import MIN_SEND_GAP_SEC, wait_send_gap

LOG = logging.getLogger("messaging.textmebot")

DEFAULT_BASE_URL = "http://api.textmebot.com/send.php"
MAX_BUTTONS = 3
_TEXTMEBOT_MIN_GAP_SEC = float(MIN_SEND_GAP_SEC)

class TextMeBotButton(TypedDict):
    """زر واتساب — النوع يُحدَّد تلقائياً من ``id``:

    - نص عادي → رد سريع (Quick Reply) يُرسل ``id`` كرسالة
    - يبدأ بـ http/https → زر رابط
    - رقم هاتف (+218...) → زر اتصال
    """

    text: str
    id: str


def _build_params(
    *,
    apikey: str,
    recipient: str,
    text: str,
    file_url: str | None = None,
    document_url: str | None = None,
    document_filename: str | None = None,
    buttons: list[TextMeBotButton] | None = None,
    copycode: str | None = None,
    copytext: str | None = None,
    json_response: bool = True,
) -> dict[str, str]:
    key = (apikey or "").strip()
    if not key:
        raise RuntimeError("مفتاح TextMeBot غير مُعدّ.")
    if not recipient:
        raise RuntimeError("رقم المستقبل فارغ.")

    body = (text or "").strip()
    img = (file_url or "").strip()
    doc = (document_url or "").strip()
    btns = buttons or []
    code = (copycode or "").strip()
    code_label = (copytext or "").strip()

    if not body and not img and not doc and not btns and not code:
        raise RuntimeError("النص أو الصورة أو المستند أو الأزرار أو copycode مطلوب.")

    params: dict[str, str] = {
        "recipient": recipient,
        "apikey": key,
        "text": body or " ",
        "json": "yes" if json_response else "no",
    }
    if img:
        params["file"] = img
    if doc:
        params["document"] = doc
        if document_filename:
            params["filename"] = document_filename.strip()
        if body:
            params["test"] = body
    if code:
        params["copycode"] = code
        if code_label:
            params["copytext"] = code_label
    for idx, btn in enumerate(btns[:MAX_BUTTONS], start=1):
        label = (btn.get("text") or "").strip()
        action = (btn.get("id") or "").strip()
        if not label or not action:
            continue
        params[f"button{idx}"] = label
        params[f"button{idx}id"] = action
    return params


def _parse_response(body: str) -> str:
    low = body.lower().strip()
    if not body.strip():
        raise RuntimeError("TextMeBot: استجابة فارغة.")
    if "trial is over" in low or "subscribe here" in low:
        raise RuntimeError(body[:300])
    if "apikey parameter is missing" in low or "apikey parameter is <b>missing</b>" in low:
        raise RuntimeError("TextMeBot: المفتاح لم يُرسل — خطأ في طريقة الاتصال بالـ API.")
    if "<p style=\"color:tomato\">" in low or "error:" in low:
        raise RuntimeError(body[:300])

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        if "success" in low:
            return body[:500]
        raise RuntimeError(f"TextMeBot: استجابة غير متوقعة: {body[:200]}")

    if not isinstance(data, dict):
        raise RuntimeError(body[:300])
    status = str(data.get("status") or "").lower()
    if status == "error" or data.get("error"):
        raise RuntimeError(str(data.get("message") or data.get("comment") or data.get("error") or body)[:300])
    if status != "success":
        raise RuntimeError(str(data.get("comment") or data.get("message") or body)[:300])
    return body[:500]


def _parse_delay_seconds(message: str) -> int | None:
    """يستخرج مدة الانتظار من رسالة TextMeBot (مثل: 8 sec. Delay needed)."""
    text = (message or "").strip()
    if not text:
        return None
    match = re.search(r"(\d+)\s*sec(?:ond)?s?\.?\s*delay", text, flags=re.IGNORECASE)
    if match:
        return max(1, int(match.group(1)))
    low = text.lower()
    if "delay needed" in low or "delay is needed" in low:
        return 8
    return None


def _wait_textmebot_gap(min_gap: float = _TEXTMEBOT_MIN_GAP_SEC) -> None:
    """يحترم الحد الأدنى بين رسائل TextMeBot — نفس ساعة الفاصل العامة."""
    wait_send_gap(max(_TEXTMEBOT_MIN_GAP_SEC, float(min_gap or 0)))


def _request_send(
    *,
    url_base: str,
    params: dict[str, str],
    timeout: float,
    max_retries: int = 2,
) -> str:
    """TextMeBot يقبل GET (موصى به) — POST JSON لا يُمرَّر apikey بشكل صحيح."""
    url_base = (url_base or DEFAULT_BASE_URL).strip().split("?")[0]
    query = urllib.parse.urlencode(params)
    full = f"{url_base}?{query}"
    last_error = "TextMeBot: فشل الإرسال."

    for attempt in range(max_retries + 1):
        # الفاصل بين الرسائل يُفرَض من outbox.send_gap — هنا فقط عند إعادة المحاولة بعد rate-limit
        if attempt > 0:
            _wait_textmebot_gap()
        req = urllib.request.Request(full, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                if resp.status >= 400:
                    raise RuntimeError(f"HTTP {resp.status}: {body[:200]}")
                return _parse_response(body)
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
            last_error = f"TextMeBot HTTP {exc.code}: {err_body[:200]}"
            delay = _parse_delay_seconds(err_body)
            if delay and attempt < max_retries:
                LOG.info("TextMeBot rate limit — waiting %ss before retry", delay)
                time.sleep(delay)
                continue
            raise RuntimeError(last_error) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"TextMeBot اتصال: {exc.reason}") from exc
        except RuntimeError as exc:
            last_error = str(exc)
            delay = _parse_delay_seconds(last_error)
            if delay and attempt < max_retries:
                LOG.info("TextMeBot rate limit — waiting %ss before retry", delay)
                time.sleep(delay)
                continue
            raise

    raise RuntimeError(last_error)

def send_textmebot(
    *,
    base_url: str,
    apikey: str,
    recipient: str,
    text: str,
    file_url: str | None = None,
    document_url: str | None = None,
    document_filename: str | None = None,
    buttons: list[TextMeBotButton] | None = None,
    copycode: str | None = None,
    copytext: str | None = None,
    timeout: float = 45,
) -> str:
    """إرسال عبر ``send.php`` — GET (يدعم Pro: أزرار حتى 3، copycode، document)."""
    params = _build_params(
        apikey=apikey,
        recipient=recipient,
        text=text,
        file_url=file_url,
        document_url=document_url,
        document_filename=document_filename,
        buttons=buttons,
        copycode=copycode,
        copytext=copytext,
        json_response=True,
    )
    return _request_send(url_base=base_url, params=params, timeout=timeout)


def send_textmebot_with_fallback(
    *,
    base_url: str,
    apikey: str,
    recipient: str,
    text: str,
    file_url: str | None = None,
    document_url: str | None = None,
    document_filename: str | None = None,
    buttons: list[TextMeBotButton] | None = None,
    copycode: str | None = None,
    copytext: str | None = None,
    timeout: float = 45,
) -> str:
    """إرسال مع إعادة محاولة — وإن فشلت الصورة يُرسل النص فقط."""
    try:
        return send_textmebot(
            base_url=base_url,
            apikey=apikey,
            recipient=recipient,
            text=text,
            file_url=file_url,
            document_url=document_url,
            document_filename=document_filename,
            buttons=buttons,
            copycode=copycode,
            copytext=copytext,
            timeout=timeout,
        )
    except RuntimeError as exc:
        if not (file_url or "").strip():
            raise
        delay = _parse_delay_seconds(str(exc))
        if delay:
            time.sleep(delay)
        LOG.warning("TextMeBot image send failed — retrying text only: %s", exc)
        return send_textmebot(
            base_url=base_url,
            apikey=apikey,
            recipient=recipient,
            text=text + "\n\n(تعذّر إرفاق صورة الفاتورة — أُرسل النص فقط.)",
            timeout=timeout,
        )

def buttons_from_meta(meta: dict | None) -> list[TextMeBotButton]:
    """استخراج أزرار من ``meta_json`` للطابور."""
    if not meta:
        return []
    raw = meta.get("buttons")
    if not isinstance(raw, list):
        return []
    out: list[TextMeBotButton] = []
    for item in raw[:MAX_BUTTONS]:
        if not isinstance(item, dict):
            continue
        label = str(item.get("text") or item.get("label") or "").strip()
        action = str(item.get("id") or item.get("action") or "").strip()
        if label and action:
            out.append({"text": label, "id": action})
    return out
