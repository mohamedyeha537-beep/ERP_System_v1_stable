"""إدارة محرك الإشعارات — /admin/notifications/*"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import joinedload

from app.deps import DBSession, require_permission
from app.jinja_env import templates
from modules.authz.models import User
from modules.authz.permissions import MESSAGING_MANAGE
from modules.notifications.events import ALL_EVENT_KEYS, PHASE1_EVENT_KEYS, PHASE3_EVENT_KEYS, PHASE4_EVENT_KEYS, PHASE5_EVENT_KEYS, WIRED_EVENT_KEYS
from modules.notifications.analytics import build_notification_analytics
from modules.notifications.event_settings import (
    event_enabled_map,
    is_notification_event_enabled,
    toggle_notification_event,
)
from modules.notifications.module_settings import (
    NOTIFICATION_MODULES,
    module_enabled_map,
    toggle_notification_module,
)
from modules.notifications.models import (
    NotificationEvent,
    NotificationEventStatus,
    NotificationLog,
    NotificationRoute,
    NotificationRule,
    NotificationTemplate,
)
from modules.notifications.seed import ensure_notification_defaults
from modules.notifications.service import NotificationService
from modules.settings.service import get_bool, get_setting, invalidate_settings_cache, set_setting

router = APIRouter(prefix="/admin/notifications", tags=["notifications"])
_perm = require_permission(MESSAGING_MANAGE)
_STATIC_ROOT = Path(__file__).resolve().parents[2] / "app" / "static"


def _nav_ctx(active: str) -> dict:
    return {"nav_active": active}


@router.get("", response_class=HTMLResponse)
def notifications_hub(request: Request, db: DBSession, _: User = Depends(_perm)):
    ensure_notification_defaults(db)
    db.commit()
    pending = db.scalar(
        select(func.count())
        .select_from(NotificationEvent)
        .where(NotificationEvent.status == NotificationEventStatus.PENDING.value)
    ) or 0
    failed = db.scalar(
        select(func.count())
        .select_from(NotificationLog)
        .where(NotificationLog.status == "failed")
    ) or 0
    tpl_count = db.scalar(select(func.count()).select_from(NotificationTemplate)) or 0
    rule_count = db.scalar(select(func.count()).select_from(NotificationRule)) or 0
    event_keys = [k for k, _ in ALL_EVENT_KEYS]
    notifications_on = get_bool(db, "notifications_enabled", True)
    messaging_on = get_bool(db, "messaging_enabled", False)
    return templates.TemplateResponse(
        "admin_notifications.html",
        {
            "request": request,
            "enabled": NotificationService.enabled(db),
            "notifications_on": notifications_on,
            "messaging_on": messaging_on,
            "pending_events": pending,
            "failed_logs": failed,
            "template_count": tpl_count,
            "rule_count": rule_count,
            "inventory_phones": get_setting(db, "notification_inventory_phones", ""),
            "hr_phones": get_setting(db, "notification_hr_phones", ""),
            "supervisor_phones": get_setting(db, "notification_supervisor_phones", ""),
            "modules": NOTIFICATION_MODULES,
            "module_enabled": module_enabled_map(db),
            "events": ALL_EVENT_KEYS,
            "event_enabled": event_enabled_map(db, event_keys),
            "phase1": PHASE1_EVENT_KEYS,
            "phase3": PHASE3_EVENT_KEYS,
            "phase4": PHASE4_EVENT_KEYS,
            "phase5": PHASE5_EVENT_KEYS,
            "wired": WIRED_EVENT_KEYS,
            "saved": request.query_params.get("saved"),
            "test": request.query_params.get("test"),
            "err": request.query_params.get("err"),
            **_nav_ctx("hub"),
        },
    )


@router.post("/settings", response_class=HTMLResponse)
def save_settings(
    db: DBSession,
    _: User = Depends(_perm),
    notifications_enabled: str = Form(""),
    messaging_enabled: str = Form(""),
    notification_inventory_phones: str = Form(""),
    notification_hr_phones: str = Form(""),
    notification_supervisor_phones: str = Form(""),
):
    set_setting(db, "notifications_enabled", "1" if notifications_enabled == "on" else "0")
    set_setting(db, "messaging_enabled", "1" if messaging_enabled == "on" else "0")
    set_setting(db, "notification_inventory_phones", notification_inventory_phones.strip())
    set_setting(db, "notification_hr_phones", notification_hr_phones.strip())
    set_setting(db, "notification_supervisor_phones", notification_supervisor_phones.strip())
    db.commit()
    return RedirectResponse("/admin/notifications?saved=1", status_code=302)


@router.get("/events", response_class=HTMLResponse)
def events_catalog(request: Request, db: DBSession, _: User = Depends(_perm)):
    """صفحة الأحداث — أو إعادة توجيه للتبويب إن فُتحت منفصلة."""
    return RedirectResponse("/admin/notifications?tab=events", status_code=302)


@router.post("/events/toggle", response_class=HTMLResponse)
def toggle_event(
    db: DBSession,
    _: User = Depends(_perm),
    event_key: str = Form(...),
):
    key = (event_key or "").strip()
    valid = {k for k, _ in ALL_EVENT_KEYS}
    if key not in valid:
        return RedirectResponse(
            "/admin/notifications?tab=events&err=" + quote("حدث غير معروف."),
            status_code=302,
        )
    new_state = toggle_notification_event(db, key)
    db.commit()
    state = "on" if new_state else "off"
    return RedirectResponse(
        f"/admin/notifications?tab=events&saved=1&event={quote(key)}&state={state}",
        status_code=302,
    )


@router.post("/modules/toggle", response_class=HTMLResponse)
def toggle_module(
    db: DBSession,
    _: User = Depends(_perm),
    module_key: str = Form(...),
):
    key = (module_key or "").strip()
    valid = {k for k, _l, _p in NOTIFICATION_MODULES}
    if key not in valid:
        return RedirectResponse(
            "/admin/notifications?tab=modules&err=" + quote("مجموعة غير معروفة."),
            status_code=302,
        )
    new_state = toggle_notification_module(db, key)
    db.commit()
    state = "on" if new_state else "off"
    return RedirectResponse(
        f"/admin/notifications?tab=modules&saved=1&module={quote(key)}&state={state}",
        status_code=302,
    )


@router.get("/routes", response_class=HTMLResponse)
def routes_list(request: Request, db: DBSession, _: User = Depends(_perm)):
    ensure_notification_defaults(db)
    db.commit()
    from modules.notifications.routing import parse_event_patterns

    rows = list(
        db.scalars(
            select(NotificationRoute).order_by(
                NotificationRoute.sort_order.asc(), NotificationRoute.id.asc()
            )
        ).all()
    )
    enriched = []
    for r in rows:
        enriched.append(
            {
                "id": r.id,
                "name_ar": r.name_ar,
                "phones": r.phones,
                "recipient_scope": r.recipient_scope,
                "sort_order": r.sort_order,
                "is_active": r.is_active,
                "patterns_display": ", ".join(parse_event_patterns(r.event_patterns_json)),
            }
        )
    return templates.TemplateResponse(
        "admin_notifications_routes.html",
        {
            "request": request,
            "routes": enriched,
            "saved": request.query_params.get("saved"),
            "err": request.query_params.get("err"),
            **_nav_ctx("routes"),
        },
    )


@router.post("/routes/save", response_class=HTMLResponse)
def route_save(
    db: DBSession,
    _: User = Depends(_perm),
    route_id: str = Form(""),
    name_ar: str = Form(...),
    event_patterns: str = Form(...),
    phones: str = Form(""),
    recipient_scope: str = Form("*"),
    sort_order: str = Form("100"),
    notes: str = Form(""),
    is_active: str = Form(""),
):
    import json

    rid = int(route_id) if (route_id or "").strip().isdigit() else None
    patterns = [p.strip() for p in (event_patterns or "").replace("\n", ",").split(",") if p.strip()]
    if not patterns:
        return RedirectResponse(
            "/admin/notifications/routes?err=" + quote("أدخل نمط حدث واحد على الأقل."),
            status_code=302,
        )
    if rid:
        row = db.get(NotificationRoute, rid)
        if row is None:
            return RedirectResponse("/admin/notifications/routes?err=missing", status_code=302)
    else:
        row = NotificationRoute()
        db.add(row)
    row.name_ar = name_ar.strip()[:120]
    row.event_patterns_json = json.dumps(patterns, ensure_ascii=False)
    row.phones = phones.strip()
    row.recipient_scope = (recipient_scope or "*").strip()[:32] or "*"
    try:
        row.sort_order = int((sort_order or "100").strip())
    except ValueError:
        row.sort_order = 100
    row.notes = (notes or "").strip() or None
    row.is_active = is_active == "on"
    db.commit()
    return RedirectResponse("/admin/notifications/routes?saved=1", status_code=302)


@router.post("/routes/toggle/{route_id}", response_class=HTMLResponse)
def route_toggle(route_id: int, db: DBSession, _: User = Depends(_perm)):
    row = db.get(NotificationRoute, route_id)
    if row is not None:
        row.is_active = not row.is_active
        db.commit()
    return RedirectResponse("/admin/notifications/routes", status_code=302)


@router.post("/routes/delete/{route_id}", response_class=HTMLResponse)
def route_delete(route_id: int, db: DBSession, _: User = Depends(_perm)):
    row = db.get(NotificationRoute, route_id)
    if row is not None:
        db.delete(row)
        db.commit()
    return RedirectResponse("/admin/notifications/routes?saved=1", status_code=302)


@router.get("/templates", response_class=HTMLResponse)
def templates_list(request: Request, db: DBSession, _: User = Depends(_perm)):
    ensure_notification_defaults(db)
    db.commit()
    rows = list(
        db.scalars(
            select(NotificationTemplate).order_by(
                NotificationTemplate.event_key, NotificationTemplate.id
            )
        ).all()
    )
    return templates.TemplateResponse(
        "admin_notifications_templates.html",
        {
            "request": request,
            "templates": rows,
            "events": ALL_EVENT_KEYS,
            "saved": request.query_params.get("saved"),
            "err": request.query_params.get("err"),
            "imported": request.query_params.get("imported"),
            **_nav_ctx("templates"),
        },
    )


@router.get("/templates/export.json")
def templates_export_json(db: DBSession, _: User = Depends(_perm)):
    from datetime import datetime, timezone

    from modules.notifications.template_transfer import export_notification_templates_json

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    content = export_notification_templates_json(db)
    return Response(
        content=content.encode("utf-8"),
        media_type="application/json; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="notification_templates_{stamp}.json"',
        },
    )


@router.post("/templates/import", response_class=HTMLResponse)
async def templates_import_json(
    db: DBSession,
    _: User = Depends(_perm),
    file: UploadFile = File(...),
):
    from modules.notifications.template_transfer import import_notification_templates_json

    if not file.filename:
        return RedirectResponse(
            "/admin/notifications/templates?err=" + quote("لم يُرفع ملف."),
            status_code=302,
        )
    raw = await file.read()
    text = None
    for enc in ("utf-8-sig", "utf-8", "cp1256", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode("utf-8", errors="replace")

    try:
        result = import_notification_templates_json(db, text)
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        return RedirectResponse(
            "/admin/notifications/templates?err=" + quote(str(exc)[:200]),
            status_code=302,
        )

    msg = f"جديد {result.created}، محدّث {result.updated}"
    if result.skipped:
        msg += f"، تخطي {result.skipped}"
    if result.errors:
        msg += " | " + result.errors[0]
    return RedirectResponse(
        "/admin/notifications/templates?imported=" + quote(msg),
        status_code=302,
    )


@router.get("/delivery", response_class=HTMLResponse)
def delivery_settings_page(request: Request, db: DBSession, _: User = Depends(_perm)):
    from modules.messaging.service import messaging_enabled
    from modules.messaging.outbox import pending_outbox_count, whatsapp_provider

    s = {k: get_setting(db, k, "") for k in (
        "messaging_textmebot_apikey",
        "messaging_textmebot_base_url",
        "messaging_whatsapp_provider",
        "messaging_country_code",
        "messaging_admin_phone",
        "shop_online_staff_phone",
        "hotel_online_staff_phone",
        "messaging_test_phone",
        "messaging_send_delay_seconds",
        "messaging_outbox_batch_size",
        "messaging_worker_interval_seconds",
        "messaging_webhook_url",
        "messaging_webhook_method",
        "messaging_webhook_param",
        "messaging_telegram_bot_token",
        "messaging_inbound_secret",
    )}
    pending = pending_outbox_count(db)
    return templates.TemplateResponse(
        "admin_notifications_delivery.html",
        {
            "request": request,
            "messaging_on": get_bool(db, "messaging_enabled", False),
            "worker_on": get_bool(db, "messaging_outbox_worker_enabled", True),
            "inbound_on": get_bool(db, "messaging_inbound_enabled", False),
            "use_n8n_json": get_bool(db, "messaging_use_n8n_json", True),
            "enabled": messaging_enabled(db),
            "pending": pending,
            "provider": whatsapp_provider(db),
            "s": s,
            "inbound_url": str(request.base_url).rstrip("/") + "/api/messaging/inbound",
            "saved": request.query_params.get("saved"),
            "test": request.query_params.get("test"),
            "err": request.query_params.get("err"),
            **_nav_ctx("delivery"),
        },
    )


@router.post("/delivery/save", response_class=HTMLResponse)
def delivery_settings_save(
    db: DBSession,
    _: User = Depends(_perm),
    messaging_enabled_flag: str = Form("", alias="messaging_enabled"),
    messaging_outbox_worker_enabled: str = Form(""),
    messaging_inbound_enabled: str = Form(""),
    messaging_whatsapp_provider: str = Form("textmebot"),
    messaging_textmebot_apikey: str = Form(""),
    messaging_textmebot_base_url: str = Form("http://api.textmebot.com/send.php"),
    messaging_country_code: str = Form("218"),
    messaging_admin_phone: str = Form(""),
    shop_online_staff_phone: str = Form(""),
    hotel_online_staff_phone: str = Form(""),
    messaging_test_phone: str = Form(""),
    messaging_send_delay_seconds: str = Form("10"),
    messaging_outbox_batch_size: str = Form("1"),
    messaging_worker_interval_seconds: str = Form("30"),
    messaging_webhook_url: str = Form(""),
    messaging_webhook_method: str = Form("POST"),
    messaging_webhook_param: str = Form("text"),
    messaging_use_n8n_json: str = Form(""),
    messaging_telegram_bot_token: str = Form(""),
    messaging_inbound_secret: str = Form(""),
):
    set_setting(db, "messaging_enabled", "1" if messaging_enabled_flag == "on" else "0")
    set_setting(
        db,
        "messaging_outbox_worker_enabled",
        "1" if messaging_outbox_worker_enabled == "on" else "0",
    )
    set_setting(
        db,
        "messaging_inbound_enabled",
        "1" if messaging_inbound_enabled == "on" else "0",
    )
    prov = (messaging_whatsapp_provider or "textmebot").strip().lower()
    if prov not in ("webhook", "textmebot"):
        prov = "textmebot"
    set_setting(db, "messaging_whatsapp_provider", prov)
    if messaging_textmebot_apikey.strip():
        set_setting(db, "messaging_textmebot_apikey", messaging_textmebot_apikey.strip())
    set_setting(
        db,
        "messaging_textmebot_base_url",
        messaging_textmebot_base_url.strip() or "http://api.textmebot.com/send.php",
    )
    set_setting(db, "messaging_country_code", messaging_country_code.strip() or "218")
    set_setting(db, "messaging_admin_phone", messaging_admin_phone.strip()[:40])
    set_setting(db, "shop_online_staff_phone", shop_online_staff_phone.strip()[:40])
    set_setting(db, "hotel_online_staff_phone", hotel_online_staff_phone.strip()[:40])
    set_setting(db, "messaging_test_phone", messaging_test_phone.strip())
    try:
        _delay = max(10, int((messaging_send_delay_seconds or "10").strip() or "10"))
    except ValueError:
        _delay = 10
    set_setting(db, "messaging_send_delay_seconds", str(_delay))
    set_setting(db, "messaging_outbox_batch_size", messaging_outbox_batch_size.strip() or "1")
    set_setting(
        db,
        "messaging_worker_interval_seconds",
        messaging_worker_interval_seconds.strip() or "30",
    )
    set_setting(db, "messaging_webhook_url", messaging_webhook_url.strip())
    set_setting(db, "messaging_webhook_method", messaging_webhook_method.strip().upper())
    set_setting(db, "messaging_webhook_param", messaging_webhook_param.strip() or "text")
    set_setting(db, "messaging_use_n8n_json", "1" if messaging_use_n8n_json == "on" else "0")
    set_setting(db, "messaging_telegram_bot_token", messaging_telegram_bot_token.strip())
    secret = messaging_inbound_secret.strip()
    if messaging_inbound_enabled == "on" and not secret:
        import secrets

        secret = secrets.token_hex(24)
    if secret:
        set_setting(db, "messaging_inbound_secret", secret)
    invalidate_settings_cache()
    db.commit()
    return RedirectResponse("/admin/notifications/delivery?saved=1", status_code=302)


@router.post("/templates/save", response_class=HTMLResponse)
def template_save(
    request: Request,
    db: DBSession,
    _: User = Depends(_perm),
    template_id: str = Form(""),
    name: str = Form(...),
    event_key: str = Form(...),
    recipient_type: str = Form(...),
    message_type: str = Form("text"),
    body_template: str = Form(...),
    buttons_json: str = Form(""),
    image_url: str = Form(""),
    document_url: str = Form(""),
    is_active: str = Form(""),
    image_file: UploadFile | None = File(None),
    document_file: UploadFile | None = File(None),
):
    from modules.catalog.uploads import save_notification_attachment

    cleaned_buttons = (buttons_json or "").strip()
    if cleaned_buttons:
        try:
            parsed_buttons = json.loads(cleaned_buttons)
        except json.JSONDecodeError:
            return RedirectResponse(
                "/admin/notifications/templates?err=" + quote("صيغة أزرار JSON غير صحيحة."),
                status_code=302,
            )
        if not isinstance(parsed_buttons, list):
            return RedirectResponse(
                "/admin/notifications/templates?err=" + quote("الأزرار يجب أن تكون قائمة JSON."),
                status_code=302,
            )

    final_image_url = (image_url or "").strip()
    final_document_url = (document_url or "").strip()
    try:
        base_url = str(request.base_url).rstrip("/")
        if image_file is not None and image_file.filename:
            if Path(image_file.filename).suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
                return RedirectResponse(
                    "/admin/notifications/templates?err=" + quote("ملف الصورة يجب أن يكون jpg أو png أو webp."),
                    status_code=302,
                )
            rel = save_notification_attachment(image_file, _STATIC_ROOT)
            final_image_url = f"{base_url}/{rel}"
        if document_file is not None and document_file.filename:
            if Path(document_file.filename).suffix.lower() != ".pdf":
                return RedirectResponse(
                    "/admin/notifications/templates?err=" + quote("ملف PDF يجب أن يكون بصيغة pdf."),
                    status_code=302,
                )
            rel = save_notification_attachment(document_file, _STATIC_ROOT)
            final_document_url = f"{base_url}/{rel}"
    except ValueError as exc:
        return RedirectResponse(
            "/admin/notifications/templates?err=" + quote(str(exc)),
            status_code=302,
        )

    tid = int(template_id) if (template_id or "").strip().isdigit() else None
    if tid:
        row = db.get(NotificationTemplate, tid)
        if row is None:
            return RedirectResponse("/admin/notifications/templates?err=missing", status_code=302)
    else:
        row = NotificationTemplate()
        db.add(row)
    row.name = name.strip()[:160]
    row.event_key = event_key.strip()[:64]
    row.recipient_type = recipient_type.strip()[:32]
    row.message_type = message_type.strip()[:20] or "text"
    row.body_template = body_template
    row.buttons_json = cleaned_buttons or None
    row.image_url = final_image_url or None
    row.document_url = final_document_url or None
    row.is_active = is_active == "on"
    row.channel = "whatsapp"
    db.commit()
    return RedirectResponse("/admin/notifications/templates?saved=1", status_code=302)


@router.post("/templates/toggle/{template_id}", response_class=HTMLResponse)
def template_toggle(template_id: int, db: DBSession, _: User = Depends(_perm)):
    row = db.get(NotificationTemplate, template_id)
    if row is not None:
        row.is_active = not row.is_active
        db.commit()
    return RedirectResponse("/admin/notifications/templates", status_code=302)


@router.get("/rules", response_class=HTMLResponse)
def rules_list(request: Request, db: DBSession, _: User = Depends(_perm)):
    rows = list(
        db.scalars(
            select(NotificationRule)
            .options(joinedload(NotificationRule.template))
            .order_by(NotificationRule.event_key, NotificationRule.id)
        )
        .unique()
        .all()
    )
    tpls = list(
        db.scalars(
            select(NotificationTemplate)
            .where(NotificationTemplate.is_active.is_(True))
            .order_by(NotificationTemplate.name)
        ).all()
    )
    return templates.TemplateResponse(
        "admin_notifications_rules.html",
        {
            "request": request,
            "rules": rows,
            "templates": tpls,
            "events": ALL_EVENT_KEYS,
            "saved": request.query_params.get("saved"),
            **_nav_ctx("rules"),
        },
    )


@router.post("/rules/save", response_class=HTMLResponse)
def rule_save(
    db: DBSession,
    _: User = Depends(_perm),
    rule_id: str = Form(""),
    event_key: str = Form(...),
    recipient_type: str = Form(...),
    template_id: str = Form(...),
    throttle_minutes: str = Form(""),
    condition_json: str = Form("{}"),
    is_active: str = Form(""),
):
    rid = int(rule_id) if (rule_id or "").strip().isdigit() else None
    if rid:
        row = db.get(NotificationRule, rid)
        if row is None:
            return RedirectResponse("/admin/notifications/rules?err=missing", status_code=302)
    else:
        row = NotificationRule()
        db.add(row)
    row.event_key = event_key.strip()[:64]
    row.recipient_type = recipient_type.strip()[:32]
    row.template_id = int(template_id)
    row.channel = "whatsapp"
    row.is_active = is_active == "on"
    row.condition_json = (condition_json or "{}").strip() or "{}"
    try:
        row.throttle_minutes = int(throttle_minutes) if throttle_minutes.strip() else None
    except ValueError:
        row.throttle_minutes = None
    db.commit()
    return RedirectResponse("/admin/notifications/rules?saved=1", status_code=302)


@router.post("/rules/toggle/{rule_id}", response_class=HTMLResponse)
def rule_toggle(rule_id: int, db: DBSession, _: User = Depends(_perm)):
    row = db.get(NotificationRule, rule_id)
    if row is not None:
        row.is_active = not row.is_active
        db.commit()
    return RedirectResponse("/admin/notifications/rules", status_code=302)


@router.get("/logs", response_class=HTMLResponse)
def logs_list(
    request: Request,
    db: DBSession,
    _: User = Depends(_perm),
    status: str = Query(""),
    event: str = Query(""),
):
    q = select(NotificationLog).order_by(NotificationLog.created_at.desc()).limit(200)
    if status:
        q = q.where(NotificationLog.status == status.strip())
    if event:
        q = q.where(NotificationLog.event_key == event.strip())
    rows = list(db.scalars(q).all())
    return templates.TemplateResponse(
        "admin_notifications_logs.html",
        {
            "request": request,
            "logs": rows,
            "status_filter": status,
            "event_filter": event,
            "events": ALL_EVENT_KEYS,
            "retried": request.query_params.get("retried"),
            **_nav_ctx("logs"),
        },
    )


@router.post("/test", response_class=HTMLResponse)
def test_emit(
    db: DBSession,
    _: User = Depends(_perm),
    event_key: str = Form(...),
    phone: str = Form(""),
):
    ensure_notification_defaults(db)
    payload = {
        "customer_name": "اختبار",
        "phone": phone.strip() or get_setting(db, "messaging_test_phone", ""),
        "order_id": "9999",
        "sale_id": "9999",
        "total": "25.000",
        "points": "10",
        "balance": "100",
        "product_name": "صنف تجريبي",
        "balance_qty": "2",
        "reorder_level": "5",
        "unit": "قطعة",
        "driver_name": "سائق تجريبي",
        "cashier_name": "كاشير",
        "employee_name": "أحمد محمد",
        "shift_id": "1",
        "shortage": "15.000",
        "cash_shortage": "10.000",
        "bank_shortage": "5.000",
        "shortage_detail": "نقداً: 10.000 د.ل · مصرف: 5.000 د.ل",
        "shortage_amount": "15.000",
        "reason": "اختبار",
        "action_id": "1",
        "line_id": "1",
        "message": "رسالة اختبار من محرك الإشعارات",
        "product_id": "1",
        "warehouse_id": "1",
        "lot_code": "LOT-001",
        "days_left": "3",
        "qty": "5",
        "expiry_date": "2026-06-15",
        "movement_qty": "10",
        "purchase_id": "100",
        "supplier": "مورّد تجريبي",
        "amount": "150.000",
        "old_cost": "10.000",
        "new_cost": "12.000",
        "item_count": "3",
    }
    if not payload["phone"]:
        return RedirectResponse(
            "/admin/notifications?err=" + quote("أدخل رقم هاتف للاختبار."),
            status_code=302,
        )
    if not is_notification_event_enabled(db, event_key.strip()):
        return RedirectResponse(
            "/admin/notifications?err=" + quote(f"الحدث {event_key} معطّل — فعّله من تبويب الأحداث."),
            status_code=302,
        )
    eid = NotificationService.emit_event(
        db,
        event_key=event_key.strip(),
        source_type="test",
        source_id=None,
        payload=payload,
    )
    db.commit()
    msg = f"حدث #{eid} — راجع السجل." if eid else "لم يُنشأ حدث (تحقق من التفعيل والقواعد)."
    return RedirectResponse("/admin/notifications?test=" + quote(msg), status_code=302)


@router.post("/retry-failed", response_class=HTMLResponse)
def retry_failed(db: DBSession, _: User = Depends(_perm)):
    n = NotificationService.retry_failed(db, limit=50)
    db.commit()
    return RedirectResponse(f"/admin/notifications/logs?retried={n}", status_code=302)


@router.get("/analytics", response_class=HTMLResponse)
def notifications_analytics(
    request: Request,
    db: DBSession,
    _: User = Depends(_perm),
    days: int = Query(30, ge=1, le=365),
):
    stats = build_notification_analytics(db, days=days)
    return templates.TemplateResponse(
        "admin_notifications_analytics.html",
        {
            "request": request,
            "stats": stats,
            "days": days,
            **_nav_ctx("analytics"),
        },
    )


@router.get("/inventory", response_class=HTMLResponse)
def inventory_notifications(request: Request, db: DBSession, _: User = Depends(_perm)):
    """هيكل المرحلة 3 — إعدادات إشعارات المخزون."""
    return templates.TemplateResponse(
        "admin_inventory_notifications.html",
        {
            "request": request,
            "inventory_phones": get_setting(db, "notification_inventory_phones", ""),
            "digest_enabled": get_bool(db, "inventory_digest_enabled", False),
            "expiry_scan_enabled": get_bool(db, "inventory_expiry_scan_enabled", True),
            "unusual_qty": get_setting(db, "notification_inventory_unusual_qty", ""),
            "events": [k for k, _ in ALL_EVENT_KEYS if k.startswith("inventory.")],
            "phase3": PHASE3_EVENT_KEYS,
            "wired": WIRED_EVENT_KEYS,
            **_nav_ctx("inventory"),
        },
    )


@router.post("/inventory/save", response_class=HTMLResponse)
def inventory_notifications_save(
    db: DBSession,
    _: User = Depends(_perm),
    notification_inventory_phones: str = Form(""),
    inventory_digest_enabled: str = Form(""),
    inventory_expiry_scan_enabled: str = Form(""),
    notification_inventory_unusual_qty: str = Form(""),
):
    set_setting(db, "notification_inventory_phones", notification_inventory_phones.strip())
    set_setting(
        db, "inventory_digest_enabled", "1" if inventory_digest_enabled == "on" else "0"
    )
    set_setting(
        db,
        "inventory_expiry_scan_enabled",
        "1" if inventory_expiry_scan_enabled == "on" else "0",
    )
    set_setting(
        db, "notification_inventory_unusual_qty", notification_inventory_unusual_qty.strip()
    )
    db.commit()
    return RedirectResponse("/admin/notifications/inventory?saved=1", status_code=302)
