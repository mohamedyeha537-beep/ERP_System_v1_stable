"""صندوق الوارد — واجهة المحادثات للوكلاء."""
from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import OperationalError, ProgrammingError

LOG = logging.getLogger("messaging.inbox")

from app.deps import DBSession, require_any_permission
from app.jinja_env import templates
from modules.authz.models import User
from modules.authz.permissions import (
    CUSTOMERS_VIEW,
    MESSAGING_MANAGE,
    MESSAGING_SEND,
    MESSAGING_VIEW,
)
from modules.messaging.inbox_service import (
    assign_conversation,
    get_conversation,
    inbox_unread_total,
    list_conversations,
    mark_conversation_read,
    send_agent_reply,
    set_conversation_status,
)
from modules.messaging.service import MessagingError, messaging_enabled

router = APIRouter(prefix="/admin/messaging/inbox", tags=["messaging-inbox"])

_inbox_view = require_any_permission(
    MESSAGING_MANAGE, MESSAGING_SEND, MESSAGING_VIEW
)
_inbox_send = require_any_permission(MESSAGING_MANAGE, MESSAGING_SEND)

_INBOX_TABLES = ("message_conversations", "message_thread_items")


def _inbox_deploy_check(db: DBSession) -> list[str]:
    """قائمة مشاكل النشر الشائعة على السيرفر الحي."""
    issues: list[str] = []
    tpl_dir = Path(__file__).resolve().parents[2] / "app" / "templates"
    for name in ("admin_messaging_inbox.html", "admin_messaging_inbox_thread.html"):
        if not (tpl_dir / name).is_file():
            issues.append(f"قالب مفقود: app/templates/{name}")
    try:
        names = set(inspect(db.get_bind()).get_table_names())
        for table in _INBOX_TABLES:
            if table not in names:
                issues.append(
                    f"جدول قاعدة البيانات مفقود: {table} — أعد تشغيل السيرفر بعد التحديث"
                )
    except Exception as exc:
        issues.append(f"تعذّر فحص قاعدة البيانات: {exc}")
    return issues


def _inbox_error_page(request: Request, *, title: str, detail: str, issues: list[str] | None = None):
    from html import escape

    items = issues or []
    html_issues = "".join(f"<li>{escape(i)}</li>" for i in items) if items else ""
    body = f"""<!DOCTYPE html>
<html lang="ar" dir="rtl"><head><meta charset="utf-8"/><title>{escape(title)}</title>
<style>body{{font-family:Segoe UI,Tahoma,sans-serif;background:#0f172a;color:#e2e8f0;padding:2rem;max-width:42rem;margin:auto}}
.card{{background:#1e293b;border:1px solid #334155;border-radius:12px;padding:1.25rem}}
code{{background:#0f172a;padding:0.15rem 0.4rem;border-radius:4px}}
a{{color:#38bdf8}}</style></head><body>
<div class="card"><h1 style="margin-top:0">{escape(title)}</h1>
<p>{escape(detail)}</p>
{"<ul>" + html_issues + "</ul>" if html_issues else ""}
<p style="font-size:0.9rem;margin-top:1rem">
<a href="/admin/messaging">← إعدادات المراسلات</a> ·
<a href="/admin/messaging/inbox/diagnostics">تشخيص</a>
</p></div></body></html>"""
    return HTMLResponse(body, status_code=503)


@router.get("/diagnostics", response_class=HTMLResponse)
def inbox_diagnostics(request: Request, db: DBSession, _: User = Depends(_inbox_view)):
    issues = _inbox_deploy_check(db)
    conv_count = None
    if not issues:
        try:
            conv_count = db.scalar(text("SELECT COUNT(*) FROM message_conversations")) or 0
        except Exception as exc:
            issues.append(str(exc))
    return templates.TemplateResponse(
        "admin_messaging_inbox_diagnostics.html",
        {
            "request": request,
            "issues": issues,
            "conv_count": conv_count,
            "ok": not issues,
        },
    )


@router.get("", response_class=HTMLResponse)
def inbox_list(
    request: Request,
    db: DBSession,
    user: User = Depends(_inbox_view),
    filter: str = Query("open"),
    mine: str = Query(""),
):
    deploy_issues = _inbox_deploy_check(db)
    if deploy_issues:
        LOG.error("inbox deploy incomplete: %s", deploy_issues)
        return _inbox_error_page(
            request,
            title="صندوق الوارد — إعداد غير مكتمل",
            detail="المراسلات تعمل محلياً لكن السيرفر الحي ينقصه ملفات أو جداول. راجع القائمة:",
            issues=deploy_issues,
        )
    status = None if filter == "all" else ("open" if filter != "closed" else "closed")
    assigned_id = user.id if mine == "1" else None
    only_unread = filter == "unread"
    try:
        rows = list_conversations(
            db,
            status=status,
            assigned_to_user_id=assigned_id,
            only_unread=only_unread,
        )
        unread = inbox_unread_total(db)
        from modules.messaging.inbox_service import inbox_pending_response_count

        pending_response = inbox_pending_response_count(db)
        enabled = messaging_enabled(db)
    except (OperationalError, ProgrammingError) as exc:
        LOG.exception("inbox list failed")
        return _inbox_error_page(
            request,
            title="صندوق الوارد — خطأ قاعدة البيانات",
            detail="تعذّر قراءة المحادثات. غالباً جداول message_conversations غير موجودة على pos.db.",
            issues=[str(exc), "أعد تشغيل السيرفر: restart-server.bat أو systemctl restart pos"],
        )
    except Exception as exc:
        LOG.exception("inbox list unexpected error")
        return _inbox_error_page(
            request,
            title="صندوق الوارد — خطأ داخلي",
            detail=str(exc),
        )
    return templates.TemplateResponse(
        "admin_messaging_inbox.html",
        {
            "request": request,
            "conversations": rows,
            "filter": filter,
            "mine": mine == "1",
            "unread_total": unread,
            "pending_response": pending_response,
            "enabled": enabled,
            "err": request.query_params.get("err"),
            "ok": request.query_params.get("ok"),
        },
    )


@router.post("/dismiss-pending", response_class=HTMLResponse)
def inbox_dismiss_pending(
    db: DBSession,
    _: User = Depends(_inbox_send),
):
    from modules.messaging.inbox_service import close_conversations_pending_response

    try:
        n = close_conversations_pending_response(db)
        db.commit()
    except MessagingError as exc:
        db.rollback()
        return RedirectResponse(
            "/admin/messaging/inbox?err=" + quote(str(exc)),
            status_code=302,
        )
    return RedirectResponse(
        f"/admin/messaging/inbox?ok=dismissed&n={n}",
        status_code=302,
    )


@router.get("/{conversation_id:int}", response_class=HTMLResponse)
def inbox_thread(
    request: Request,
    conversation_id: int,
    db: DBSession,
    user: User = Depends(_inbox_view),
):
    conv = get_conversation(db, conversation_id)
    if conv is None:
        return RedirectResponse(
            "/admin/messaging/inbox?err=" + quote("المحادثة غير موجودة."),
            status_code=302,
        )
    mark_conversation_read(db, conversation_id)
    db.commit()
    agents = list(
        db.scalars(select(User).where(User.is_active.is_(True)).order_by(User.username))
    )
    can_send = True
    return templates.TemplateResponse(
        "admin_messaging_inbox_thread.html",
        {
            "request": request,
            "conv": conv,
            "agents": agents,
            "can_send": can_send,
            "can_manage": user_has_any(user, MESSAGING_MANAGE),
            "inbound_url_hint": _inbound_url_hint(request),
            "err": request.query_params.get("err"),
            "ok": request.query_params.get("ok"),
        },
    )


def user_has_any(user: User, code: str) -> bool:
    from modules.authz.service import user_has_permission

    return user_has_permission(user, code)


def _inbound_url_hint(request: Request) -> str:
    base = str(request.base_url).rstrip("/")
    return f"{base}/api/messaging/inbound"


@router.post("/{conversation_id:int}/reply", response_class=HTMLResponse)
def inbox_reply(
    conversation_id: int,
    db: DBSession,
    user: User = Depends(_inbox_send),
    message: str = Form(...),
    force: str = Form(""),
):
    try:
        send_agent_reply(
            db,
            conversation_id=conversation_id,
            user_id=user.id,
            message=message,
            force=force in ("on", "1", "true", "yes"),
        )
        db.commit()
    except MessagingError as exc:
        db.rollback()
        return RedirectResponse(
            f"/admin/messaging/inbox/{conversation_id}?err={quote(str(exc))}",
            status_code=302,
        )
    return RedirectResponse(
        f"/admin/messaging/inbox/{conversation_id}?ok=1",
        status_code=302,
    )


@router.post("/{conversation_id:int}/assign", response_class=HTMLResponse)
def inbox_assign(
    conversation_id: int,
    db: DBSession,
    user: User = Depends(_inbox_send),
    assignee_id: str = Form(""),
):
    uid: int | None
    raw = (assignee_id or "").strip()
    if raw == "me":
        uid = user.id
    elif raw == "" or raw == "none":
        uid = None
    else:
        try:
            uid = int(raw)
        except ValueError:
            uid = user.id
    try:
        assign_conversation(db, conversation_id, uid)
        db.commit()
    except MessagingError as exc:
        db.rollback()
        return RedirectResponse(
            f"/admin/messaging/inbox/{conversation_id}?err={quote(str(exc))}",
            status_code=302,
        )
    return RedirectResponse(
        f"/admin/messaging/inbox/{conversation_id}?ok=assign",
        status_code=302,
    )


@router.post("/{conversation_id:int}/close", response_class=HTMLResponse)
def inbox_close(
    conversation_id: int,
    db: DBSession,
    _: User = Depends(_inbox_send),
):
    try:
        set_conversation_status(db, conversation_id, open_conversation=False)
        db.commit()
    except MessagingError as exc:
        db.rollback()
        return RedirectResponse(
            f"/admin/messaging/inbox/{conversation_id}?err={quote(str(exc))}",
            status_code=302,
        )
    return RedirectResponse("/admin/messaging/inbox?ok=closed", status_code=302)


@router.post("/{conversation_id:int}/reopen", response_class=HTMLResponse)
def inbox_reopen(
    conversation_id: int,
    db: DBSession,
    _: User = Depends(_inbox_send),
):
    try:
        set_conversation_status(db, conversation_id, open_conversation=True)
        db.commit()
    except MessagingError as exc:
        db.rollback()
        return RedirectResponse(
            f"/admin/messaging/inbox/{conversation_id}?err={quote(str(exc))}",
            status_code=302,
        )
    return RedirectResponse(
        f"/admin/messaging/inbox/{conversation_id}?ok=reopened",
        status_code=302,
    )
