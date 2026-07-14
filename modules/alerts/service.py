"""تنبيهات نفاد المخزون عبر البريد و/أو WhatsApp webhook.

الفكرة:
- WhatsApp Webhook: عنوان عام (مثل CallMeBot أو خدمة شخصية) يستقبل النص.
  يدعم GET (يُمرّر النص في باراميتر) أو POST (نص مباشر JSON).
- البريد: SMTP عبر مكتبة Python القياسية. يدعم TLS.

كلا القناتين اختياريتان وتُضبط من صفحة `/admin/alerts`.
"""

from __future__ import annotations

import hashlib
import json
import smtplib
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from email.mime.text import MIMEText
from email.utils import formatdate

from sqlalchemy.orm import Session

from modules.inventory.service import (
    get_sales_warehouse_id,
    get_warehouse,
    low_stock_by_warehouse,
    low_stock_products,
    reconcile_all_stock_balances,
)
from modules.settings.service import get_bool, get_int, get_setting, set_setting


@dataclass
class AlertOutcome:
    email_sent: bool = False
    email_error: str | None = None
    whatsapp_sent: bool = False
    whatsapp_error: str | None = None
    messaging_sent: bool = False
    messaging_count: int = 0
    messaging_error: str | None = None
    items: int = 0
    message: str = ""
    skipped_duplicate: bool = False


def _fmt_qty(value: Decimal) -> str:
    q = Decimal(str(value or 0)).quantize(Decimal("0.001"))
    s = format(q, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def _low_stock_fingerprint(by_warehouse: list[tuple]) -> str:
    parts: list[str] = []
    for wh, rows in by_warehouse:
        wh_id = wh.id if hasattr(wh, "id") else 0
        for p, qty in sorted(rows, key=lambda x: x[0].id):
            parts.append(f"{wh_id}:{p.id}:{_fmt_qty(qty)}:{_fmt_qty(p.reorder_level)}")
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _collect_low_stock(db: Session, *, all_warehouses: bool) -> list[tuple]:
    reconcile_all_stock_balances(db)
    if all_warehouses:
        return low_stock_by_warehouse(db)
    wid = get_sales_warehouse_id(db)
    rows = low_stock_products(db, wid)
    if not rows:
        return []
    wh = get_warehouse(db, wid)
    if wh is None:
        return []
    return [(wh, rows)]


def _looks_like_real_endpoint(value: str) -> bool:
    """يتجنّب محاولة الإرسال لعناوين placeholder في الإعدادات."""
    v = (value or "").strip().lower()
    if not v:
        return False
    skip = ("example.", "example.invalid", "localhost", "127.0.0.1", "placeholder")
    return not any(s in v for s in skip)


def _format_low_stock_message(rows, store_name: str) -> str:
    if not rows:
        return f"[{store_name}] لا توجد أصناف منخفضة حالياً."
    lines = [f"[{store_name}] تنبيه نفاد مخزون — {len(rows)} صنف:"]
    for p, qty in rows:
        unit = p.unit or ""
        rl = _fmt_qty(p.reorder_level)
        q = _fmt_qty(qty)
        if Decimal(str(qty or 0)) < 0:
            note = "⚠️ نفاد (رصيد سالب)"
        else:
            note = f"حد التنبيه {rl} {unit}".strip()
        lines.append(f"• {p.name_ar}: المتاح {q} {unit} ({note})")
    return "\n".join(lines)


def _format_low_stock_message_all(
    by_warehouse: list[tuple], store_name: str
) -> str:
    """رسالة مجمّعة لكل المخازن التي فيها نقص."""
    if not by_warehouse:
        return f"[{store_name}] لا توجد أصناف منخفضة حالياً."
    total = sum(len(rows) for _wh, rows in by_warehouse)
    lines = [f"[{store_name}] تنبيه نفاد مخزون — {total} صنف في {len(by_warehouse)} مخزن:"]
    for wh, rows in by_warehouse:
        wh_name = wh.name_ar if hasattr(wh, "name_ar") else str(wh)
        lines.append(f"\n— {wh_name} ({len(rows)} صنف) —")
        for p, qty in rows:
            unit = p.unit or ""
            rl = _fmt_qty(p.reorder_level)
            q = _fmt_qty(qty)
            if Decimal(str(qty or 0)) < 0:
                note = "⚠️ نفاد"
            else:
                note = f"حد {rl} {unit}".strip()
            lines.append(f"• {p.name_ar}: المتاح {q} {unit} ({note})")
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


def send_low_stock_alert(
    db: Session, *, force: bool = False, all_warehouses: bool = True
) -> AlertOutcome:
    """يحضر قائمة الأصناف المنخفضة ويرسل التنبيه عبر القنوات المفعّلة."""
    out = AlertOutcome()
    by_wh = _collect_low_stock(db, all_warehouses=all_warehouses)
    out.items = sum(len(rows) for _wh, rows in by_wh)
    store_name = get_setting(db, "store_name", "نقطة البيع")
    text = _format_low_stock_message_all(by_wh, store_name)
    out.message = text

    if not by_wh:
        set_setting(db, "alerts_low_stock_fingerprint", "")
        return out

    fp = _low_stock_fingerprint(by_wh)
    last_fp = (get_setting(db, "alerts_low_stock_fingerprint", "") or "").strip()
    if not force and last_fp and fp == last_fp:
        out.skipped_duplicate = True
        return out

    # ===== Email (اختياري — صفحة التنبيهات القديمة) =====
    email_to = (get_setting(db, "alerts_email_to") or "").strip()
    smtp_host = (get_setting(db, "smtp_host") or "").strip()
    if email_to and smtp_host and _looks_like_real_endpoint(smtp_host):
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

    # ===== WhatsApp webhook (اختياري — صفحة التنبيهات القديمة) =====
    wa_url = (get_setting(db, "whatsapp_webhook_url") or "").strip()
    if wa_url and _looks_like_real_endpoint(wa_url):
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

    # ===== محرك الإشعارات المركزي =====
    try:
        from modules.notifications.hooks import emit_inventory_low_stock_if_needed
        from modules.notifications.service import NotificationService

        if NotificationService.enabled(db):
            for wh, rows in by_wh:
                wid = wh.id if hasattr(wh, "id") else 0
                for p, _qty in rows:
                    emit_inventory_low_stock_if_needed(
                        db, product_id=p.id, warehouse_id=wid
                    )
            out.messaging_sent = True
            out.messaging_count = out.items
    except Exception as e:  # noqa: BLE001
        out.messaging_error = str(e)

    # ===== بوت المراسلات (legacy — يُستبدّ تدريجياً) =====
    if not out.messaging_sent:
        try:
            from modules.messaging.service import messaging_enabled, send_stock_low_now

            if messaging_enabled(db):
                ids = send_stock_low_now(db, text)
                out.messaging_count = len(ids)
                out.messaging_sent = len(ids) > 0
                if not ids:
                    out.messaging_error = (
                        "البوت مفعّل لكن لا توجد قاعدة stock.low نشطة — "
                        "راجع /admin/notifications/rules"
                    )
        except Exception as e:  # noqa: BLE001
            out.messaging_error = str(e)

    if out.email_sent or out.whatsapp_sent or out.messaging_sent:
        set_setting(db, "alerts_low_stock_fingerprint", fp)

    return out


def send_product_expiry_alert(db: Session) -> AlertOutcome:
    """دفعات مخزون دخلت نافذة تنبيه الصلاحية."""
    from modules.catalog.expiry import (
        format_expiry_alert_message,
        list_expiry_warning_lots,
    )

    out = AlertOutcome()
    rows = list_expiry_warning_lots(db)
    out.items = len(rows)
    store_name = get_setting(db, "store_name", "نقطة البيع")
    text = format_expiry_alert_message(rows, store_name=store_name)
    out.message = text

    if not rows:
        return out

    email_to = (get_setting(db, "alerts_email_to") or "").strip()
    smtp_host = (get_setting(db, "smtp_host") or "").strip()
    if email_to and smtp_host and _looks_like_real_endpoint(smtp_host):
        try:
            _send_email(
                subject=f"[{store_name}] تنبيه صلاحية دفعات ({out.items})",
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

    wa_url = (get_setting(db, "whatsapp_webhook_url") or "").strip()
    if wa_url and _looks_like_real_endpoint(wa_url):
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

    try:
        from modules.messaging.service import messaging_enabled, send_product_expiry_now
        from modules.notifications.inventory_hooks import emit_expiry_scan
        from modules.notifications.service import NotificationService

        if NotificationService.enabled(db):
            n = emit_expiry_scan(db)
            out.messaging_count = n
            out.messaging_sent = n > 0
        elif messaging_enabled(db):
            ids = send_product_expiry_now(db, text)
            out.messaging_count = len(ids)
            out.messaging_sent = len(ids) > 0
            if not ids:
                out.messaging_error = (
                    "البوت مفعّل لكن لا توجد قاعدة product.expiry نشطة — "
                    "راجع /admin/messaging/rules"
                )
    except Exception as e:  # noqa: BLE001
        out.messaging_error = str(e)

    return out
