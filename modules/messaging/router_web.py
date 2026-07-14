"""إدارة بوت المراسلات — إعدادات، قوالب، قواعد، حملات، سجل."""
from __future__ import annotations

import json
from datetime import datetime, timezone
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
from modules.messaging.events import ALL_EVENT_TYPES, MESSAGING_TEST, MESSAGING_TEST_BUTTONS
from modules.messaging.categories import (
    AUTOMATIC_EVENT_TYPES,
    event_label_ar,
    kind_label_ar,
    message_kind,
)
from modules.messaging.models import (
    MessageAudience,
    MessageCampaign,
    MessageEventDefinition,
    MessageOutbox,
    MessageOutboxStatus,
    MessageRule,
    MessageTemplate,
    MessageChannel,
)
from modules.messaging.outbox import (
    cancel_outbox_item,
    cancel_pending_outbox,
    enqueue_message,
    estimate_queue_minutes,
    pending_outbox_count,
    process_outbox_batch,
    retry_outbox_item,
    send_outbox_item_now,
    whatsapp_provider,
)
from modules.messaging.event_registry import (
    EventRegistryError,
    create_custom_event,
    delete_custom_event,
    ensure_system_events_and_hooks,
    hooks_with_mappings,
    list_automatic_event_types,
    save_hook_mappings,
    update_event,
)
from modules.messaging.providers.textmebot import send_textmebot
from modules.messaging.providers.webhook import send_webhook
from modules.messaging.seed import ensure_messaging_defaults
from modules.messaging.service import (
    emit_event_and_send,
    enqueue_broadcast,
    messaging_enabled,
    run_campaign_now,
)
from modules.messaging.admin_nav import messaging_admin_nav_ctx
from modules.messaging.media import resolve_campaign_image_url
from modules.messaging.phone_list_service import (
    PhoneListError,
    import_phone_list,
    list_phone_lists,
    phone_list_template_content,
)
from modules.messaging.phone_utils import normalize_whatsapp_phone
from modules.messaging.templates_render import render_template
from modules.settings.service import get_bool, get_setting, set_setting

router = APIRouter(prefix="/admin/messaging", tags=["messaging"])
_perm = require_permission(MESSAGING_MANAGE)

SETTING_KEYS = [
    "messaging_enabled",
    "messaging_webhook_url",
    "messaging_webhook_method",
    "messaging_webhook_param",
    "messaging_use_n8n_json",
    "messaging_telegram_bot_token",
    "messaging_admin_phone",
    "messaging_test_phone",
    "messaging_whatsapp_provider",
    "messaging_textmebot_apikey",
    "messaging_textmebot_base_url",
    "messaging_country_code",
    "messaging_send_delay_seconds",
    "messaging_outbox_batch_size",
    "messaging_worker_interval_seconds",
    "messaging_outbox_worker_enabled",
    "messaging_inbound_enabled",
    "messaging_inbound_secret",
    "web_chat_enabled",
    "shop_enabled",
    "web_chat_n8n_webhook_url",
    "web_chat_bank_payment_text",
    "web_chat_guide_loyalty_file",
    "web_chat_guide_referral_file",
    "web_chat_guide_general_file",
    "web_chat_bot_name",
    "web_chat_greeting_text",
    "web_chat_menu_options",
    "web_chat_ai_enabled",
    "web_chat_ai_api_key",
    "web_chat_ai_model",
    "web_chat_ai_base_url",
]


def _settings_ctx(db) -> dict:
    return {k: get_setting(db, k, "") for k in SETTING_KEYS}


def _resolve_test_recipient(db: DBSession, test_phone: str) -> tuple[str, str]:
    """رقم الاختبار: الحقل المُدخل → آخر رقم اختبار محفوظ → رقم الإدارة."""
    cc = get_setting(db, "messaging_country_code", "218")
    raw = (test_phone or "").strip()
    if not raw:
        raw = (get_setting(db, "messaging_test_phone") or "").strip()
    if not raw:
        raw = (get_setting(db, "messaging_admin_phone") or "").strip()
    if not raw:
        return "", ""
    return raw, normalize_whatsapp_phone(raw, country_code=cc)


def _test_result_message(row: MessageOutbox, recipient: str) -> str:
    if row.status == MessageOutboxStatus.SENT.value:
        return f"تم الإرسال إلى {recipient} — راجع سجل الإرسال (#{row.id})."
    err = (row.error_message or "خطأ غير معروف").strip()
    return f"فشل الإرسال إلى {recipient} (سجل #{row.id}): {err}"


@router.get("", response_class=HTMLResponse)
def messaging_hub(request: Request, db: DBSession, _: User = Depends(_perm)):
    return RedirectResponse("/admin/notifications/delivery", status_code=302)


@router.get("/web-chat", response_class=HTMLResponse)
def messaging_web_chat_settings(request: Request, db: DBSession, _: User = Depends(_perm)):
    """إعدادات الشات الذكي (Hermes) — مسار متقدم تحت محرك الإشعارات."""
    from modules.messaging.web_chat_config import web_chat_menu_options_display

    pending = pending_outbox_count(db)
    failed = db.scalar(
        select(func.count())
        .select_from(MessageOutbox)
        .where(MessageOutbox.status == MessageOutboxStatus.FAILED.value)
    ) or 0
    try:
        nav = messaging_admin_nav_ctx(db)
    except Exception:
        nav = {"inbox_unread_conversations": 0, "pending_chat_orders": 0, "failed": 0}
    cc = get_setting(db, "messaging_country_code", "218")
    admin_phone_raw = (get_setting(db, "messaging_admin_phone") or "").strip()
    admin_phone_wa = (
        normalize_whatsapp_phone(admin_phone_raw, country_code=cc)
        if admin_phone_raw
        else ""
    )
    test_phone_raw = (get_setting(db, "messaging_test_phone") or "").strip() or admin_phone_raw
    test_phone_wa = (
        normalize_whatsapp_phone(test_phone_raw, country_code=cc)
        if test_phone_raw
        else ""
    )
    return templates.TemplateResponse(
        "admin_messaging.html",
        {
            "request": request,
            "s": _settings_ctx(db),
            "web_chat_menu_display": web_chat_menu_options_display(db),
            "enabled": messaging_enabled(db),
            "pending": pending,
            "failed": failed,
            **nav,
            "eta_minutes": estimate_queue_minutes(db, pending),
            "provider": whatsapp_provider(db),
            "saved": request.query_params.get("saved"),
            "test": request.query_params.get("test"),
            "err": request.query_params.get("err"),
            "inbound_url": str(request.base_url).rstrip("/") + "/api/messaging/inbound",
            "chat_url": str(request.base_url).rstrip("/") + "/chat",
            "chat_reply_url": str(request.base_url).rstrip("/") + "/api/chat/reply",
            "admin_phone_raw": admin_phone_raw,
            "admin_phone_wa": admin_phone_wa,
            "test_phone_raw": test_phone_raw,
            "test_phone_wa": test_phone_wa,
            "web_chat_only": True,
        },
    )


@router.post("/save", response_class=HTMLResponse)
def messaging_save(
    db: DBSession,
    _: User = Depends(_perm),
    messaging_enabled_flag: str = Form("", alias="messaging_enabled"),
    messaging_webhook_url: str = Form(""),
    messaging_webhook_method: str = Form("POST"),
    messaging_webhook_param: str = Form("text"),
    messaging_use_n8n_json: str = Form(""),
    messaging_telegram_bot_token: str = Form(""),
    messaging_admin_phone: str = Form(""),
    messaging_whatsapp_provider: str = Form("webhook"),
    messaging_textmebot_apikey: str = Form(""),
    messaging_textmebot_base_url: str = Form("http://api.textmebot.com/send.php"),
    messaging_country_code: str = Form("218"),
    messaging_send_delay_seconds: str = Form("5"),
    messaging_outbox_batch_size: str = Form("1"),
    messaging_worker_interval_seconds: str = Form("30"),
    messaging_outbox_worker_enabled: str = Form(""),
    messaging_inbound_enabled: str = Form(""),
    messaging_inbound_secret: str = Form(""),
    web_chat_enabled: str = Form(""),
    shop_enabled: str = Form(""),
    web_chat_n8n_webhook_url: str = Form(""),
    web_chat_bank_payment_text: str = Form(""),
    web_chat_bot_name: str = Form(""),
    web_chat_greeting_text: str = Form(""),
    web_chat_menu_options: str = Form(""),
    web_chat_ai_enabled: str = Form(""),
    web_chat_ai_api_key: str = Form(""),
    web_chat_ai_model: str = Form(""),
    web_chat_ai_base_url: str = Form(""),
):
    set_setting(db, "messaging_enabled", "1" if messaging_enabled_flag == "on" else "0")
    set_setting(db, "messaging_webhook_url", messaging_webhook_url.strip())
    set_setting(db, "messaging_webhook_method", messaging_webhook_method.strip().upper())
    set_setting(db, "messaging_webhook_param", messaging_webhook_param.strip() or "text")
    set_setting(
        db,
        "messaging_use_n8n_json",
        "1" if messaging_use_n8n_json == "on" else "0",
    )
    set_setting(db, "messaging_telegram_bot_token", messaging_telegram_bot_token.strip())
    set_setting(db, "messaging_admin_phone", messaging_admin_phone.strip())
    prov = (messaging_whatsapp_provider or "webhook").strip().lower()
    if prov not in ("webhook", "textmebot"):
        prov = "webhook"
    set_setting(db, "messaging_whatsapp_provider", prov)
    if messaging_textmebot_apikey.strip():
        set_setting(db, "messaging_textmebot_apikey", messaging_textmebot_apikey.strip())
    set_setting(
        db,
        "messaging_textmebot_base_url",
        messaging_textmebot_base_url.strip() or "http://api.textmebot.com/send.php",
    )
    set_setting(db, "messaging_country_code", messaging_country_code.strip() or "218")
    set_setting(db, "messaging_send_delay_seconds", messaging_send_delay_seconds.strip() or "5")
    set_setting(db, "messaging_outbox_batch_size", messaging_outbox_batch_size.strip() or "1")
    set_setting(
        db,
        "messaging_worker_interval_seconds",
        messaging_worker_interval_seconds.strip() or "30",
    )
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
    secret = messaging_inbound_secret.strip()
    if messaging_inbound_enabled == "on" and not secret:
        import secrets

        secret = secrets.token_hex(24)
    if secret:
        set_setting(db, "messaging_inbound_secret", secret)
    set_setting(db, "web_chat_enabled", "1" if web_chat_enabled == "on" else "0")
    set_setting(db, "shop_enabled", "1" if shop_enabled == "on" else "0")
    set_setting(db, "web_chat_n8n_webhook_url", web_chat_n8n_webhook_url.strip())
    set_setting(db, "web_chat_bank_payment_text", web_chat_bank_payment_text.strip())
    from modules.messaging.web_chat_config import normalize_menu_options_input

    bot_name = web_chat_bot_name.strip() or "مساعد"
    set_setting(db, "web_chat_bot_name", bot_name)
    set_setting(db, "web_chat_greeting_text", web_chat_greeting_text.strip())
    set_setting(
        db,
        "web_chat_menu_options",
        normalize_menu_options_input(web_chat_menu_options),
    )
    set_setting(
        db,
        "web_chat_ai_enabled",
        "1" if web_chat_ai_enabled == "on" else "0",
    )
    if web_chat_ai_api_key.strip():
        set_setting(db, "web_chat_ai_api_key", web_chat_ai_api_key.strip())
    set_setting(
        db,
        "web_chat_ai_model",
        (web_chat_ai_model.strip() or "gpt-4o-mini"),
    )
    set_setting(
        db,
        "web_chat_ai_base_url",
        (
            web_chat_ai_base_url.strip().rstrip("/")
            or "https://api.openai.com/v1"
        ),
    )
    ensure_messaging_defaults(db)
    db.commit()
    return RedirectResponse("/admin/messaging/web-chat?saved=1", status_code=302)


_MESSAGING_STATIC = Path(__file__).resolve().parents[2] / "app" / "static"


@router.post("/upload-chat-guide", response_class=RedirectResponse)
async def upload_chat_guide(
    db: DBSession,
    _: User = Depends(_perm),
    guide_type: str = Form(...),
    file: UploadFile = File(...),
):
    from modules.catalog.uploads import delete_stored_relative_file, save_web_chat_guide
    from modules.messaging.web_chat_guides import guide_setting_key
    from modules.settings.service import get_setting, set_setting

    key = guide_setting_key(guide_type)
    if key is None:
        return RedirectResponse("/admin/messaging/web-chat?err=guide_type", status_code=302)
    try:
        fn = save_web_chat_guide(file, _MESSAGING_STATIC)
        old = (get_setting(db, key, "") or "").strip()
        if old:
            delete_stored_relative_file(_MESSAGING_STATIC, old)
        set_setting(db, key, fn)
        db.commit()
    except ValueError as exc:
        db.rollback()
        return RedirectResponse(
            f"/admin/messaging?err={quote(str(exc))}", status_code=302
        )
    return RedirectResponse("/admin/messaging/web-chat?saved=1", status_code=302)


@router.post("/toggle", response_class=HTMLResponse)
def messaging_toggle(db: DBSession, _: User = Depends(_perm)):
    """تفعيل/إيقاف سريع دون التمرير لأسفل الصفحة."""
    ensure_messaging_defaults(db)
    cur = get_bool(db, "messaging_enabled", False)
    set_setting(db, "messaging_enabled", "0" if cur else "1")
    db.commit()
    return RedirectResponse("/admin/messaging/web-chat?saved=1", status_code=302)


@router.post("/save-test-phone", response_class=HTMLResponse)
def messaging_save_test_phone(
    db: DBSession,
    _: User = Depends(_perm),
    test_phone: str = Form(""),
):
    ensure_messaging_defaults(db)
    phone_raw = (test_phone or "").strip()
    if not phone_raw:
        msg = "أدخل رقم واتساب للاختبار ثم اضغط حفظ."
        return RedirectResponse("/admin/notifications/delivery?test=" + quote(msg), status_code=302)
    cc = get_setting(db, "messaging_country_code", "218")
    recipient = normalize_whatsapp_phone(phone_raw, country_code=cc)
    if not recipient:
        msg = "رقم غير صالح."
        return RedirectResponse("/admin/notifications/delivery?test=" + quote(msg), status_code=302)
    set_setting(db, "messaging_test_phone", phone_raw)
    db.commit()
    msg = f"تم حفظ رقم الاختبار: {recipient}"
    return RedirectResponse("/admin/notifications/delivery?test=" + quote(msg), status_code=302)


@router.post("/test", response_class=HTMLResponse)
def messaging_test(
    db: DBSession,
    _: User = Depends(_perm),
    test_phone: str = Form(""),
):
    ensure_messaging_defaults(db)
    phone_raw, recipient = _resolve_test_recipient(db, test_phone)
    if not recipient:
        msg = "أدخل رقم واتساب للاختبار في الحقل أدناه."
        return RedirectResponse("/admin/notifications/delivery?test=" + quote(msg), status_code=302)
    if phone_raw:
        set_setting(db, "messaging_test_phone", phone_raw)
    if whatsapp_provider(db) == "textmebot" and not (
        get_setting(db, "messaging_textmebot_apikey") or ""
    ).strip():
        msg = "مفتاح TextMeBot فارغ — أدخله في الإعدادات واضغط «حفظ الإعدادات»."
        return RedirectResponse("/admin/notifications/delivery?test=" + quote(msg), status_code=302)
    if whatsapp_provider(db) != "textmebot":
        if not messaging_enabled(db):
            msg = "فعّل البوت أو اختر TextMeBot مباشر للاختبار."
            return RedirectResponse("/admin/notifications/delivery?test=" + quote(msg), status_code=302)
        if not (get_setting(db, "messaging_webhook_url") or "").strip():
            msg = "Webhook فارغ — أدخل رابط n8n أو اختر TextMeBot مباشر."
            return RedirectResponse("/admin/notifications/delivery?test=" + quote(msg), status_code=302)
    store = get_setting(db, "store_name", "نقطة البيع")
    row = enqueue_message(
        db,
        body=f"اختبار مراسلات {store}",
        channel=MessageChannel.WHATSAPP.value,
        phone=phone_raw,
        event_type=MESSAGING_TEST,
        meta={"kind": message_kind(MESSAGING_TEST), "test": True},
    )
    row = send_outbox_item_now(db, row)
    msg = _test_result_message(row, recipient)
    db.commit()
    return RedirectResponse("/admin/notifications/delivery?test=" + quote(msg), status_code=302)


@router.post("/test-buttons", response_class=HTMLResponse)
def messaging_test_buttons(
    db: DBSession,
    _: User = Depends(_perm),
    test_phone: str = Form(""),
):
    """اختبار أزرار TextMeBot Pro — يتطلب تفعيل Pro على المفتاح."""
    ensure_messaging_defaults(db)
    if whatsapp_provider(db) != "textmebot":
        msg = "اختبار الأزرار يعمل مع TextMeBot فقط."
        return RedirectResponse("/admin/notifications/delivery?test=" + quote(msg), status_code=302)
    apikey = (get_setting(db, "messaging_textmebot_apikey") or "").strip()
    if not apikey:
        msg = "مفتاح TextMeBot فارغ — أدخله واحفظ الإعدادات."
        return RedirectResponse("/admin/notifications/delivery?test=" + quote(msg), status_code=302)
    phone_raw, recipient = _resolve_test_recipient(db, test_phone)
    if not recipient:
        msg = "أدخل رقم واتساب للاختبار في الحقل أدناه."
        return RedirectResponse("/admin/notifications/delivery?test=" + quote(msg), status_code=302)
    if phone_raw:
        set_setting(db, "messaging_test_phone", phone_raw)
    store = get_setting(db, "store_name", "نقطة البيع")
    row = enqueue_message(
        db,
        body=f"اختبار أزرار {store} — اختر أحد الخيارات:",
        channel=MessageChannel.WHATSAPP.value,
        phone=phone_raw,
        event_type=MESSAGING_TEST_BUTTONS,
        meta={
            "kind": message_kind(MESSAGING_TEST_BUTTONS),
            "test": True,
            "buttons": [
                {"text": "✓ تأكيد", "id": "test_confirm"},
                {"text": "🛒 المتجر", "id": "https://example.com/shop"},
                {"text": "📞 اتصل", "id": "+218900000000"},
            ],
        },
    )
    row = send_outbox_item_now(db, row)
    msg = _test_result_message(row, recipient)
    db.commit()
    return RedirectResponse("/admin/notifications/delivery?test=" + quote(msg), status_code=302)


@router.get("/events", response_class=HTMLResponse)
def events_list(request: Request, db: DBSession, _: User = Depends(_perm)):
    return RedirectResponse("/admin/notifications?tab=events", status_code=302)


@router.post("/events/save", response_class=HTMLResponse)
def event_save(
    db: DBSession,
    _: User = Depends(_perm),
    event_id: str = Form(""),
    code: str = Form(""),
    name_ar: str = Form(...),
    description: str = Form(""),
    is_active: str = Form(""),
):
    try:
        eid = int(event_id) if (event_id or "").strip().isdigit() else None
        if eid:
            update_event(
                db,
                eid,
                name_ar=name_ar,
                description=description,
                is_active=is_active == "on",
            )
        else:
            create_custom_event(
                db,
                code=code,
                name_ar=name_ar,
                description=description,
            )
        db.commit()
    except EventRegistryError as exc:
        db.rollback()
        return RedirectResponse(
            "/admin/messaging/events?err=" + quote(str(exc)),
            status_code=302,
        )
    return RedirectResponse("/admin/notifications?tab=events&saved=1", status_code=302)


@router.post("/events/delete", response_class=HTMLResponse)
def event_delete(
    db: DBSession,
    _: User = Depends(_perm),
    event_id: int = Form(...),
):
    try:
        delete_custom_event(db, event_id)
        db.commit()
    except EventRegistryError as exc:
        db.rollback()
        return RedirectResponse(
            "/admin/messaging/events?err=" + quote(str(exc)),
            status_code=302,
        )
    return RedirectResponse("/admin/notifications?tab=events&saved=1", status_code=302)


@router.post("/hooks/save", response_class=HTMLResponse)
def hook_save(
    db: DBSession,
    _: User = Depends(_perm),
    hook_code: str = Form(...),
    event_types: list[str] = Form(default=[]),
):
    try:
        save_hook_mappings(db, hook_code.strip(), event_types)
        db.commit()
    except EventRegistryError as exc:
        db.rollback()
        return RedirectResponse(
            "/admin/messaging/events?err=" + quote(str(exc)),
            status_code=302,
        )
    return RedirectResponse("/admin/notifications?tab=events&saved=hooks", status_code=302)


@router.post("/events/test", response_class=HTMLResponse)
def event_test_fire(
    db: DBSession,
    _: User = Depends(_perm),
    event_type: str = Form(...),
    test_phone: str = Form(""),
):
    from modules.customers.service import get_or_create_by_phone, require_valid_phone

    phone = (test_phone or get_setting(db, "messaging_test_phone") or "").strip()
    if not phone:
        return RedirectResponse(
            "/admin/messaging/events?err=" + quote("أدخل رقم اختبار."),
            status_code=302,
        )
    try:
        phone_clean = require_valid_phone(phone)
        cust = get_or_create_by_phone(db, phone=phone_clean, name="اختبار")
        payload = {
            "customer_id": cust.id,
            "customer_name": cust.name or "اختبار",
            "phone": cust.phone,
            "points": "10",
            "balance": "100",
            "sale_id": "0",
            "message": "رسالة اختبار للحدث",
        }
        emit_event_and_send(db, event_type.strip(), payload)
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        return RedirectResponse(
            "/admin/messaging/events?err=" + quote(str(exc)),
            status_code=302,
        )
    return RedirectResponse(
        "/admin/messaging/events?saved=test",
        status_code=302,
    )


@router.get("/templates", response_class=HTMLResponse)
def templates_list(request: Request, db: DBSession, _: User = Depends(_perm)):
    qs = request.url.query
    target = "/admin/notifications/templates"
    if qs:
        target += "?" + qs
    return RedirectResponse(target, status_code=302)


@router.get("/templates/export.json")
def templates_export_json(db: DBSession, _: User = Depends(_perm)):
    return RedirectResponse("/admin/notifications/templates/export.json", status_code=302)


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
    for enc in ("utf-8-sig", "utf-8", "cp1256", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            text = None
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


@router.post("/templates/save", response_class=HTMLResponse)
def template_save(
    db: DBSession,
    _: User = Depends(_perm),
    template_id: str = Form(""),
    code: str = Form(...),
    name: str = Form(...),
    body_text: str = Form(...),
    is_active: str = Form(""),
):
    tid = int(template_id) if (template_id or "").strip().isdigit() else None
    if tid:
        row = db.get(MessageTemplate, tid)
        if row is None:
            return RedirectResponse("/admin/notifications/templates?err=missing", status_code=302)
    else:
        row = MessageTemplate(code=code.strip(), name=name.strip(), body_text=body_text)
        db.add(row)
    row.code = code.strip()[:64]
    row.name = name.strip()[:160]
    row.body_text = body_text
    row.is_active = is_active == "on"
    db.commit()
    return RedirectResponse("/admin/notifications/templates?saved=1", status_code=302)


@router.get("/rules", response_class=HTMLResponse)
def rules_list(request: Request, db: DBSession, _: User = Depends(_perm)):
    qs = request.url.query
    target = "/admin/notifications/rules"
    if qs:
        target += "?" + qs
    return RedirectResponse(target, status_code=302)


@router.post("/rules/save", response_class=HTMLResponse)
def rule_save(
    db: DBSession,
    _: User = Depends(_perm),
    rule_id: str = Form(""),
    name: str = Form(...),
    event_type: str = Form(...),
    template_id: int = Form(...),
    channels: str = Form("whatsapp"),
    audience: str = Form("customer"),
    priority: int = Form(100),
    is_active: str = Form(""),
):
    from modules.messaging.categories import PROMOTIONAL_EVENT_CODES

    if event_type.strip() in PROMOTIONAL_EVENT_CODES:
        return RedirectResponse(
            "/admin/messaging/rules?err="
            + quote("الحملات الدعائية لا تُضاف كقواعد — استخدم «إرسال جماعي»."),
            status_code=302,
        )
    if audience == MessageAudience.ADMIN.value:
        channels = "whatsapp"
    rid = int(rule_id) if (rule_id or "").strip().isdigit() else None
    if rid:
        row = db.get(MessageRule, rid)
        if row is None:
            return RedirectResponse("/admin/notifications/rules", status_code=302)
    else:
        row = MessageRule(name=name.strip(), event_type=event_type.strip(), template_id=template_id)
        db.add(row)
    row.name = name.strip()[:160]
    row.event_type = event_type.strip()[:64]
    row.template_id = template_id
    row.channels = channels.strip()[:120]
    row.audience = audience.strip()[:20]
    row.priority = priority
    row.is_active = is_active == "on"
    db.commit()
    return RedirectResponse("/admin/notifications/rules?saved=1", status_code=302)


@router.get("/campaigns", response_class=HTMLResponse)
def campaigns_list(request: Request, db: DBSession, _: User = Depends(_perm)):
    rows = list(
        db.scalars(
            select(MessageCampaign)
            .options(joinedload(MessageCampaign.template))
            .order_by(MessageCampaign.id.desc())
        )
        .unique()
        .all()
    )
    tpls = list(db.scalars(select(MessageTemplate).where(MessageTemplate.is_active)).all())
    segment_labels: dict[int, str] = {}
    for row in rows:
        seg_obj: dict = {}
        try:
            seg_obj = json.loads(row.segment_json or "{}")
        except json.JSONDecodeError:
            pass
        seg = (seg_obj.get("segment") or "opt_in").strip()
        if seg == "phone_list":
            lid = seg_obj.get("phone_list_id")
            segment_labels[row.id] = f"قائمة خارجية #{lid}" if lid else "قائمة خارجية"
        elif seg == "all_whatsapp":
            segment_labels[row.id] = "كل العملاء"
        else:
            segment_labels[row.id] = "موافقون"
    return templates.TemplateResponse(
        "admin_messaging_campaigns.html",
        {
            "request": request,
            "campaigns": rows,
            "templates": tpls,
            "phone_lists": list_phone_lists(db),
            "segment_labels": segment_labels,
            "saved": request.query_params.get("saved"),
        },
    )


@router.post("/campaigns/save", response_class=HTMLResponse)
def campaign_save(
    request: Request,
    db: DBSession,
    _: User = Depends(_perm),
    campaign_id: str = Form(""),
    name: str = Form(...),
    template_id: int = Form(...),
    message: str = Form(""),
    image_url: str = Form(""),
    image_file: UploadFile | None = File(None),
    starts_at: str = Form(""),
    ends_at: str = Form(""),
    is_active: str = Form(""),
    segment: str = Form("opt_in"),
    phone_list_id: str = Form(""),
):
    def _parse_dt(raw: str):
        raw = (raw or "").strip()
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    cid = int(campaign_id) if (campaign_id or "").strip().isdigit() else None
    if cid:
        row = db.get(MessageCampaign, cid)
        if row is None:
            return RedirectResponse("/admin/messaging/campaigns", status_code=302)
    else:
        row = MessageCampaign(name=name.strip(), template_id=template_id)
        db.add(row)
    row.name = name.strip()[:160]
    row.template_id = template_id
    try:
        final_image_url = resolve_campaign_image_url(
            db, request, image_url=image_url, image_file=image_file
        )
    except ValueError as exc:
        return RedirectResponse(
            "/admin/messaging/campaigns?err=" + quote(str(exc)),
            status_code=302,
        )
    seg_data: dict = {
        "message": message.strip(),
        "image_url": final_image_url,
        "segment": segment if segment in ("opt_in", "all_whatsapp", "phone_list") else "opt_in",
    }
    if seg_data["segment"] == "phone_list" and (phone_list_id or "").strip().isdigit():
        seg_data["phone_list_id"] = int(phone_list_id.strip())
    row.segment_json = json.dumps(seg_data, ensure_ascii=False)
    row.starts_at = _parse_dt(starts_at)
    row.ends_at = _parse_dt(ends_at)
    row.is_active = is_active == "on"
    db.commit()
    return RedirectResponse("/admin/messaging/campaigns?saved=1", status_code=302)


@router.post("/campaigns/{campaign_id}/send", response_class=HTMLResponse)
def campaign_send(campaign_id: int, db: DBSession, _: User = Depends(_perm)):
    camp = db.get(MessageCampaign, campaign_id)
    if camp is None:
        return RedirectResponse("/admin/messaging/campaigns", status_code=302)
    db.refresh(camp, ["template"])
    if camp.template is None:
        return RedirectResponse("/admin/messaging/campaigns?err=no_tpl", status_code=302)
    count = run_campaign_now(db, camp.id)
    process_outbox_batch(db, limit=1 if whatsapp_provider(db) == "textmebot" else 30)
    db.commit()
    return RedirectResponse(
        f"/admin/messaging/campaigns?saved=1&sent={count}",
        status_code=302,
    )


@router.post("/campaigns/{campaign_id}/toggle", response_class=HTMLResponse)
def campaign_toggle(campaign_id: int, db: DBSession, _: User = Depends(_perm)):
    camp = db.get(MessageCampaign, campaign_id)
    if camp is not None:
        camp.is_active = not camp.is_active
        db.commit()
    return RedirectResponse("/admin/messaging/campaigns", status_code=302)


@router.get("/phone-lists/template")
def phone_list_template(
    db: DBSession,
    _: User = Depends(_perm),
    fmt: str = Query("csv"),
):
    cc = (get_setting(db, "messaging_country_code", "218") or "218").strip()
    body, filename, media_type = phone_list_template_content(
        fmt=fmt if fmt in ("csv", "txt") else "csv",
        country_code=cc,
    )
    return Response(
        content=body.encode("utf-8-sig"),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/phone-lists/import", response_class=HTMLResponse)
def phone_list_import(
    db: DBSession,
    user: User = Depends(_perm),
    name: str = Form(""),
    note: str = Form(""),
    file: UploadFile = File(...),
):
    try:
        content = file.file.read()
        plist, stats = import_phone_list(
            db,
            name=(name or file.filename or "قائمة أرقام").strip(),
            content=content,
            filename=file.filename or "phones.txt",
            note=note,
            user_id=user.id,
        )
        db.commit()
        qs = (
            f"list_imported={stats['imported']}"
            f"&list_name={quote(plist.name)}"
            f"&list_invalid={stats.get('invalid', 0)}"
            f"&list_duplicates={stats.get('duplicates', 0)}"
        )
        return RedirectResponse(f"/admin/messaging/broadcast?{qs}", status_code=302)
    except PhoneListError as exc:
        db.rollback()
        return RedirectResponse(
            "/admin/messaging/broadcast?err=" + quote(str(exc)),
            status_code=302,
        )
    except Exception as exc:
        db.rollback()
        return RedirectResponse(
            "/admin/messaging/broadcast?err=" + quote(f"فشل الاستيراد: {exc}"),
            status_code=302,
        )


@router.get("/broadcast", response_class=HTMLResponse)
def broadcast_page(request: Request, db: DBSession, _: User = Depends(_perm)):
    from modules.customers.models import Customer
    from modules.messaging.consent import customer_can_receive

    total = db.scalar(select(func.count()).select_from(Customer).where(Customer.is_active.is_(True))) or 0
    opt_in = 0
    for c in db.scalars(select(Customer).where(Customer.is_active.is_(True))).all():
        if customer_can_receive(db, c.id):
            opt_in += 1
    pending = pending_outbox_count(db)
    return templates.TemplateResponse(
        "admin_messaging_broadcast.html",
        {
            "request": request,
            "total_customers": total,
            "opt_in_customers": opt_in,
            "pending": pending,
            "eta_minutes": estimate_queue_minutes(db, pending),
            "delay": get_setting(db, "messaging_send_delay_seconds", "5"),
            "batch": get_setting(db, "messaging_outbox_batch_size", "1"),
            "phone_lists": list_phone_lists(db),
            "sent": request.query_params.get("sent"),
            "err": request.query_params.get("err"),
            "list_imported": request.query_params.get("list_imported"),
            "list_name": request.query_params.get("list_name"),
            "list_invalid": request.query_params.get("list_invalid"),
            "list_duplicates": request.query_params.get("list_duplicates"),
        },
    )


@router.post("/broadcast/send", response_class=HTMLResponse)
def broadcast_send(
    request: Request,
    db: DBSession,
    _: User = Depends(_perm),
    message: str = Form(...),
    image_url: str = Form(""),
    image_file: UploadFile | None = File(None),
    segment: str = Form("opt_in"),
    phone_list_id: str = Form(""),
):
    if not messaging_enabled(db):
        return RedirectResponse(
            "/admin/messaging/broadcast?err=" + quote("فعّل بوت المراسلات أولاً."),
            status_code=302,
        )
    if whatsapp_provider(db) == "textmebot" and not (
        get_setting(db, "messaging_textmebot_apikey") or ""
    ).strip():
        return RedirectResponse(
            "/admin/messaging/broadcast?err=" + quote("أدخل مفتاح TextMeBot في الإعدادات."),
            status_code=302,
        )
    seg = segment if segment in ("opt_in", "all_whatsapp", "phone_list") else "opt_in"
    lid = int(phone_list_id.strip()) if (phone_list_id or "").strip().isdigit() else None
    if seg == "phone_list" and not lid:
        return RedirectResponse(
            "/admin/messaging/broadcast?err=" + quote("اختر قائمة أرقام خارجية."),
            status_code=302,
        )
    try:
        final_image_url = resolve_campaign_image_url(
            db, request, image_url=image_url, image_file=image_file
        )
    except ValueError as exc:
        return RedirectResponse(
            "/admin/messaging/broadcast?err=" + quote(str(exc)),
            status_code=302,
        )
    count = enqueue_broadcast(
        db,
        message=message,
        image_url=final_image_url,
        segment=seg,
        phone_list_id=lid,
    )
    if count == 0:
        return RedirectResponse(
            "/admin/messaging/broadcast?err=" + quote("لا يوجد مستلمون مطابقون."),
            status_code=302,
        )
    process_outbox_batch(db, limit=1 if whatsapp_provider(db) == "textmebot" else 30)
    db.commit()
    return RedirectResponse(f"/admin/messaging/broadcast?sent={count}", status_code=302)


@router.post("/outbox/process", response_class=HTMLResponse)
def outbox_process_now(db: DBSession, _: User = Depends(_perm)):
    n = process_outbox_batch(db)
    db.commit()
    return RedirectResponse(f"/admin/messaging/log?processed={n}", status_code=302)


@router.post("/outbox/cancel-pending", response_class=HTMLResponse)
def outbox_cancel_pending(
    db: DBSession,
    _: User = Depends(_perm),
    scope: str = Form("all"),
):
    from modules.messaging.categories import AUTOMATIC_EVENT_CODES, PROMOTIONAL_EVENT_CODES

    if scope == "promo":
        types = PROMOTIONAL_EVENT_CODES
    elif scope == "auto":
        types = AUTOMATIC_EVENT_CODES
    else:
        types = None
    n = cancel_pending_outbox(db, event_types=types)
    db.commit()
    return RedirectResponse(f"/admin/messaging/log?cancelled={n}", status_code=302)


@router.post("/log/{job_id}/cancel", response_class=HTMLResponse)
def log_cancel(job_id: int, db: DBSession, _: User = Depends(_perm)):
    cancel_outbox_item(db, job_id)
    db.commit()
    return RedirectResponse("/admin/messaging/log?status=pending", status_code=302)


@router.get("/log", response_class=HTMLResponse)
def log_list(
    request: Request,
    db: DBSession,
    _: User = Depends(_perm),
    status: str = Query(""),
    kind: str = Query(""),
):
    from modules.messaging.categories import (
        AUTOMATIC_EVENT_CODES,
        PROMOTIONAL_EVENT_CODES,
    )

    stmt = select(MessageOutbox).order_by(MessageOutbox.id.desc()).limit(200)
    if status:
        stmt = stmt.where(MessageOutbox.status == status)
    if kind == "auto":
        stmt = stmt.where(MessageOutbox.event_type.in_(AUTOMATIC_EVENT_CODES))
    elif kind == "promo":
        stmt = stmt.where(MessageOutbox.event_type.in_(PROMOTIONAL_EVENT_CODES))
    rows = list(db.scalars(stmt).all())
    return templates.TemplateResponse(
        "admin_messaging_log.html",
        {
            "request": request,
            "jobs": rows,
            "status_filter": status,
            "kind_filter": kind,
            "statuses": list(MessageOutboxStatus),
            "event_label_ar": event_label_ar,
            "kind_label_ar": kind_label_ar,
            "message_kind": message_kind,
        },
    )


@router.post("/log/{job_id}/retry", response_class=HTMLResponse)
def log_retry(job_id: int, db: DBSession, _: User = Depends(_perm)):
    retry_outbox_item(db, job_id)
    process_outbox_batch(db, limit=1 if whatsapp_provider(db) == "textmebot" else 30)
    db.commit()
    return RedirectResponse("/admin/messaging/log?status=pending", status_code=302)
