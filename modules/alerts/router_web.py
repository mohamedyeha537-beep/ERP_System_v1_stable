from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.deps import DBSession, require_permission
from app.jinja_env import templates
from modules.alerts.service import send_low_stock_alert
from modules.authz.models import User
from modules.authz.permissions import ADMIN_SETTINGS
from modules.inventory.service import low_stock_by_warehouse, reconcile_all_stock_balances
from modules.settings.service import get_setting, set_setting

router = APIRouter(prefix="/admin/alerts", tags=["alerts"])
_perm = require_permission(ADMIN_SETTINGS)


def _ctx(request: Request, db, **extra):
    keys = [
        "alerts_enabled",
        "alerts_email_to",
        "smtp_host",
        "smtp_port",
        "smtp_user",
        "smtp_password",
        "smtp_from",
        "smtp_use_tls",
        "whatsapp_webhook_url",
        "whatsapp_webhook_method",
        "whatsapp_webhook_param",
    ]
    s = {k: get_setting(db, k, "") for k in keys}
    reconcile_all_stock_balances(db)
    low_all = low_stock_by_warehouse(db)
    low_count = sum(len(rows) for _wh, rows in low_all)
    base = {
        "request": request,
        "s": s,
        "low_count": low_count,
        "low_by_warehouse": low_all,
    }
    base.update(extra)
    return base


@router.get("", response_class=HTMLResponse)
def alerts_page(
    request: Request,
    db: DBSession,
    _: User = Depends(_perm),
    saved: int = Query(0),
    test: str | None = Query(None),
    test_ok: int = Query(0),
):
    return templates.TemplateResponse(
        "admin_alerts.html",
        _ctx(request, db, saved=bool(saved), test=test, test_ok=bool(test_ok)),
    )


@router.post("/save", response_class=HTMLResponse)
def alerts_save(
    request: Request,
    db: DBSession,
    _: User = Depends(_perm),
    alerts_enabled: str = Form(""),
    alerts_email_to: str = Form(""),
    smtp_host: str = Form(""),
    smtp_port: str = Form("587"),
    smtp_user: str = Form(""),
    smtp_password: str = Form(""),
    smtp_from: str = Form(""),
    smtp_use_tls: str = Form(""),
    whatsapp_webhook_url: str = Form(""),
    whatsapp_webhook_method: str = Form("GET"),
    whatsapp_webhook_param: str = Form("text"),
):
    set_setting(db, "alerts_enabled", "1" if alerts_enabled == "on" else "0")
    set_setting(db, "alerts_email_to", alerts_email_to.strip())
    set_setting(db, "smtp_host", smtp_host.strip())
    set_setting(db, "smtp_port", smtp_port.strip() or "587")
    set_setting(db, "smtp_user", smtp_user.strip())
    if smtp_password:
        set_setting(db, "smtp_password", smtp_password)
    set_setting(db, "smtp_from", smtp_from.strip())
    set_setting(db, "smtp_use_tls", "1" if smtp_use_tls == "on" else "0")
    set_setting(db, "whatsapp_webhook_url", whatsapp_webhook_url.strip())
    set_setting(db, "whatsapp_webhook_method", whatsapp_webhook_method.strip().upper())
    set_setting(db, "whatsapp_webhook_param", whatsapp_webhook_param.strip() or "text")
    db.commit()
    return RedirectResponse("/admin/alerts?saved=1", status_code=302)


@router.post("/test", response_class=HTMLResponse)
def alerts_test(
    request: Request,
    db: DBSession,
    _: User = Depends(_perm),
):
    outcome = send_low_stock_alert(db, force=True, all_warehouses=True)
    db.commit()
    parts = []
    ok = False
    if outcome.items == 0:
        parts.append("لا توجد أصناف منخفضة الآن — لم يُرسل شيء.")
    elif outcome.skipped_duplicate:
        parts.append(
            "قائمة النقص لم تتغير منذ آخر تنبيه — لم يُعاد الإرسال (لتجنب التكرار)."
        )
    else:
        if outcome.messaging_sent:
            parts.append(
                f"تم إرسال التنبيه عبر بوت المراسلات ({outcome.messaging_count} رسالة)."
            )
            ok = True
        elif outcome.messaging_error:
            parts.append(f"بوت المراسلات: {outcome.messaging_error}")
        if outcome.email_sent:
            parts.append("تم إرسال البريد بنجاح.")
            ok = True
        elif outcome.email_error:
            parts.append(f"فشل البريد: {outcome.email_error}")
        if outcome.whatsapp_sent:
            parts.append("تم استدعاء WhatsApp (صفحة التنبيهات) بنجاح.")
            ok = True
        elif outcome.whatsapp_error:
            parts.append(f"فشل WhatsApp (صفحة التنبيهات): {outcome.whatsapp_error}")
        if not ok and not parts:
            parts.append(
                "لم تُرسل رسالة — فعّل بوت المراسلات من /admin/messaging "
                "أو اضبط إيميل/Webhook في هذه الصفحة."
            )
    msg = " | ".join(parts) or "—"
    import urllib.parse as _p

    url = "/admin/alerts?test=" + _p.quote(msg, safe="")
    if ok:
        url += "&test_ok=1"
    return RedirectResponse(url, status_code=302)
