"""مركز إشعارات النظام للأدمن — /admin/activity"""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.deps import DBSession, require_any_permission
from app.jinja_env import templates
from modules.authz.models import User
from modules.authz.permissions import ADMIN_SETTINGS, MESSAGING_MANAGE, REPORTS_VIEW
from modules.dashboard_notify.activity_hub import (
    apply_item_action,
    bell_unread_count,
    build_activity_hub,
    mark_all_read,
    muted_list_for_user,
)
from modules.dashboard_notify.whatsapp_forward import (
    DEFAULT_EVENT_MESSAGE,
    SETTING_PHONE,
    WA_GROUPS,
    delete_wa_event_override,
    hub_whatsapp_forward_enabled,
    hub_whatsapp_forward_phone,
    list_selectable_events,
    list_wa_event_overrides,
    load_wa_group_phones,
    save_wa_group_settings,
    upsert_wa_event_override,
)
from modules.settings.service import get_bool, get_setting

router = APIRouter(prefix="/admin/activity", tags=["activity-hub"])
_perm = require_any_permission(ADMIN_SETTINGS, REPORTS_VIEW, MESSAGING_MANAGE)


@router.get("", response_class=HTMLResponse)
def activity_hub_page(
    request: Request,
    db: DBSession,
    user: User = Depends(_perm),
    ok: str | None = Query(None),
    error: str | None = Query(None),
    edit: str | None = Query(None),
):
    from modules.platform.business_domain import resolve_finance_domain

    finance_domain = resolve_finance_domain(user, request.session)
    sections = build_activity_hub(db, user.id, domain=finance_domain)
    total = sum(1 for sec in sections for item in sec.items if item.is_new)
    mutes = muted_list_for_user(db, user.id)
    overrides = list_wa_event_overrides(db)
    edit_key = (edit or "").strip()
    edit_row = next((o for o in overrides if o.event_key == edit_key), None)
    try:
        from modules.messaging.service import messaging_enabled as _msg_on

        messaging_on = _msg_on(db)
    except Exception:  # noqa: BLE001
        messaging_on = get_bool(db, "messaging_enabled", False)
    return templates.TemplateResponse(
        "admin_activity_hub.html",
        {
            "request": request,
            "user": user,
            "sections": sections,
            "unread_total": total,
            "mutes": mutes,
            "ok": ok,
            "error": error,
            "wa_forward_enabled": hub_whatsapp_forward_enabled(db),
            "wa_forward_phone": get_setting(db, SETTING_PHONE, "") or "",
            "wa_forward_phone_effective": hub_whatsapp_forward_phone(db),
            "wa_groups": WA_GROUPS,
            "wa_group_phones": load_wa_group_phones(db),
            "wa_event_choices": list_selectable_events(),
            "wa_event_overrides": overrides,
            "wa_edit_override": edit_row,
            "wa_default_message": DEFAULT_EVENT_MESSAGE,
            "messaging_enabled": messaging_on,
        },
    )


@router.post("/whatsapp-settings")
async def activity_whatsapp_settings(
    request: Request,
    db: DBSession,
    user: User = Depends(_perm),
):
    form = await request.form()
    action = str(form.get("action") or "save_groups").strip()

    if action == "save_override":
        err = upsert_wa_event_override(
            db,
            event_key=str(form.get("event_key") or ""),
            phones=str(form.get("event_phones") or ""),
            message_body=str(form.get("event_message") or ""),
        )
        db.commit()
        if err:
            return RedirectResponse(
                f"/admin/activity?error={quote(err)}#wa-event-custom",
                status_code=302,
            )
        return RedirectResponse(
            f"/admin/activity?ok={quote('تم حفظ تخصيص الإشعار (الأرقام + الرسالة).')}#wa-event-custom",
            status_code=302,
        )

    if action == "delete_override":
        delete_wa_event_override(db, str(form.get("event_key") or ""))
        db.commit()
        return RedirectResponse(
            f"/admin/activity?ok={quote('تم حذف تخصيص الإشعار.')}#wa-event-custom",
            status_code=302,
        )

    on = str(form.get("enabled") or "") in ("on", "1", "true", "yes")
    legacy = str(form.get("phone") or "").strip()
    group_phones: dict[str, str] = {}
    for gid, _ in WA_GROUPS:
        group_phones[gid] = str(form.get(f"phone_group_{gid}") or "").strip()
    save_wa_group_settings(
        db, enabled=on, group_phones=group_phones, legacy_phone=legacy
    )
    db.commit()
    any_phone = bool(legacy.strip()) or any(
        (v or "").strip() for v in group_phones.values()
    ) or bool(list_wa_event_overrides(db))
    if on and not any_phone:
        return RedirectResponse(
            f"/admin/activity?error={quote('فعّلت الإرسال — أدخل رقم قسم أو خصّص إشعاراً.')}",
            status_code=302,
        )
    if on:
        return RedirectResponse(
            f"/admin/activity?ok={quote('تم حفظ أرقام الأقسام.')}",
            status_code=302,
        )
    return RedirectResponse(
        f"/admin/activity?ok={quote('تم إيقاف إرسال واتساب — الإشعارات تبقى في المركز فقط.')}",
        status_code=302,
    )


@router.get("/api/count")
def activity_count_api(request: Request, db: DBSession, user: User = Depends(_perm)):
    from modules.platform.business_domain import resolve_finance_domain

    finance_domain = resolve_finance_domain(user, request.session)
    return JSONResponse({"count": bell_unread_count(db, user.id, domain=finance_domain)})


@router.post("/mark-seen")
def activity_mark_seen(request: Request, db: DBSession, user: User = Depends(_perm)):
    from modules.platform.business_domain import resolve_finance_domain

    finance_domain = resolve_finance_domain(user, request.session)
    n = mark_all_read(db, user.id, domain=finance_domain)
    db.commit()
    return RedirectResponse(
        f"/admin/activity?ok={quote(f'تم تعليم {n} إشعار كمقروء')}",
        status_code=302,
    )


@router.post("/item")
def activity_item_action(
    db: DBSession,
    user: User = Depends(_perm),
    item_key: str = Form(""),
    action: str = Form(...),
    mute_key: str = Form(""),
    mute_label: str = Form(""),
):
    msg = apply_item_action(
        db,
        user.id,
        item_key=item_key,
        action=action,
        mute_key=mute_key,
        mute_label=mute_label,
    )
    db.commit()
    if msg.startswith("تم"):
        return RedirectResponse(f"/admin/activity?ok={quote(msg)}", status_code=302)
    return RedirectResponse(f"/admin/activity?error={quote(msg)}", status_code=302)
