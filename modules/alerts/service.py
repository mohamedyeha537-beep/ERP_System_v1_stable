"""تنبيهات نفاد المخزون عبر البريد و/أو WhatsApp webhook.

الفكرة:
- WhatsApp Webhook: عنوان عام (مثل CallMeBot أو خدمة شخصية) يستقبل النص.
  يدعم GET (يُمرّر النص في باراميتر) أو POST (نص مباشر JSON).
- البريد: SMTP عبر مكتبة Python القياسية. يدعم TLS.

كلا القناتين اختياريتان وتُضبط من صفحة `/admin/alerts`.
"""

from __future__ import annotations

import json
import smtplib
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email.mime.text import MIMEText
from email.utils import formatdate

from sqlalchemy.orm import Session

from modules.inventory.service import low_stock_by_warehouse
from modules.settings.service import get_bool, get_int, get_setting


@dataclass
class AlertOutcome:
    email_sent: bool = False
    email_error: str | None = None
    whatsapp_sent: bool = False
    whatsapp_error: str | None = None
    items: int = 0
    message: str = ""


def _format_low_stock_message(rows, store_name: str) -> str:
    if not rows:
        return f"[{store_name}] لا توجد أصناف منخفضة حالياً."
    lines = [f"[{store_name}] تنبيه نفاد مخزون — {len(rows)} صنف:"]
    for p, qty in rows:
        unit = p.unit or ""
        lines.append(
            f"• {p.name_ar}: المتاح {qty} {unit} (الحد الأدنى {p.reorder_level} {unit})"
        )
    return "\n".join(lines)


def _send_email(
    subject: str,
    body: str,
    *,
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_password: str,
    sender: str,
    recipient: str,
    use_tls: bool,
) -> None:
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = sender or smtp_user
    msg["To"] = recipient
    msg["Date"] = formatdate(localtime=True)

    # timeout قصير لتجنّب تعليق العملية الخلفية لفترة طويلة عند عدم وجود نت
    timeout = 5
    if use_tls and smtp_port in (465,):
        srv = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=timeout)
    else:
        srv = smtplib.SMTP(smtp_host, smtp_port, timeout=timeout)
    try:
        if use_tls and smtp_port not in (465,):
            srv.starttls()
        if smtp_user:
            srv.login(smtp_user, smtp_password)
        srv.sendmail(msg["From"], [recipient], msg.as_string())
    finally:
        try:
            srv.quit()
        except Exception:
            pass


def _send_whatsapp_webhook(url: str, method: str, param: str, text: str) -> None:
    method = (method or "GET").upper()
    if method == "GET":
        sep = "&" if "?" in url else "?"
        full = url + sep + urllib.parse.urlencode({param: text})
        req = urllib.request.Request(full, method="GET")
    else:
        body = json.dumps({param: text}).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
    with urllib.request.urlopen(req, timeout=5) as resp:
        if resp.status >= 400:
            raise RuntimeError(f"HTTP {resp.status}")


def send_low_stock_alert(db: Session) -> AlertOutcome:
    """يحضر قائمة الأصناف المنخفضة ويرسل التنبيه عبر القنوات المفعّلة."""
    out = AlertOutcome()
    by_wh = low_stock_by_warehouse(db)
    out.items = sum(len(rows) for _wh, rows in by_wh)
    store_name = get_setting(db, "store_name", "نقطة البيع")
    text = _format_low_stock_message_all(by_wh, store_name)
    out.message = text

    if not by_wh:
        return out

    # ===== Email =====
    email_to = (get_setting(db, "alerts_email_to") or "").strip()
    smtp_host = (get_setting(db, "smtp_host") or "").strip()
    if email_to and smtp_host:
        try:
            _send_email(
                subject=f"[{store_name}] تنبيه نفاد مخزون ({out.items} صنف)",
                body=text,
                smtp_host=smtp_host,
                smtp_port=get_int(db, "smtp_port", 587),
                smtp_user=(get_setting(db, "smtp_user") or "").strip(),
                smtp_password=get_setting(db, "smtp_password") or "",
                sender=(get_setting(db, "smtp_from") or "").strip()
                or (get_setting(db, "smtp_user") or "").strip(),
                recipient=email_to,
                use_tls=get_bool(db, "smtp_use_tls", True),
            )
            out.email_sent = True
        except Exception as e:  # noqa: BLE001
            out.email_error = str(e)

    # ===== WhatsApp webhook =====
    wa_url = (get_setting(db, "whatsapp_webhook_url") or "").strip()
    if wa_url:
        try:
            _send_whatsapp_webhook(
                wa_url,
                get_setting(db, "whatsapp_webhook_method") or "GET",
                get_setting(db, "whatsapp_webhook_param") or "text",
                text,
            )
            out.whatsapp_sent = True
        except Exception as e:  # noqa: BLE001
            out.whatsapp_error = str(e)

    return out
