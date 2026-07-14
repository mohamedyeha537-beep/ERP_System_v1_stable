"""شاشة المطبخ KDS — عرض وتحريك التذاكر."""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timezone

from fastapi import APIRouter, Depends, Form, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.requests import Request

from app.deps import DBSession, require_permission
from app.jinja_env import templates
from app.number_format import format_qty_plain
from modules.authz.models import User
from modules.authz.permissions import KDS_VIEW
from modules.kds.service import (
    advance_ticket,
    archive_served_tickets_before,
    archived_ticket_count,
    build_master_ticket_sections,
    kds_open_counts,
    kds_pick_default_section,
    list_active_departments,
    list_active_kitchen_sections,
    list_legacy_routed_roots,
    list_tickets,
    repair_missing_tickets_for_sent_sales,
    rejected_ticket_count,
    reject_ticket,
    reset_ticket,
)
from modules.kds.models import KitchenVenue

router = APIRouter(prefix="/admin/kds", tags=["kds"])
_perm = require_permission(KDS_VIEW)
log = logging.getLogger("kds.web")


def _kds_scope(user: User) -> str:
    scope = (getattr(user, "kds_scope", None) or "ALL").strip().upper()
    return scope if scope in ("ALL", "CAFE", "RESTAURANT") else "ALL"


def _section_allowed(scope: str, section) -> bool:
    if scope == "ALL":
        return True
    is_drinks = (getattr(section, "code", "") or "").upper() == "DRINKS"
    return is_drinks if scope == "CAFE" else not is_drinks


def _department_allowed(scope: str, department) -> bool:
    if scope == "ALL":
        return True
    venue = getattr(department, "venue", None)
    val = venue.value if hasattr(venue, "value") else str(venue or "")
    is_cafe = val.upper() == KitchenVenue.CAFE.value
    return is_cafe if scope == "CAFE" else not is_cafe


def _ticket_allowed(scope: str, ticket) -> bool:
    if scope == "ALL":
        return True
    if ticket.kitchen_section is not None:
        return _section_allowed(scope, ticket.kitchen_section)
    if ticket.kitchen_department is not None:
        return _department_allowed(scope, ticket.kitchen_department)
    return scope == "RESTAURANT"


def _limited_kds_worker(user: User) -> bool:
    return _kds_scope(user) != "ALL"


def _parse_section(request: Request) -> tuple[str | None, int | None]:
    """section=sec:1 أو dept:3 أو cat:5"""
    raw = request.query_params.get("section", "")
    if raw == "all":
        return "all", 0
    if raw.startswith("sec:"):
        try:
            return "sec", int(raw.split(":", 1)[1])
        except ValueError:
            return None, None
    if raw.startswith("dept:"):
        try:
            return "dept", int(raw.split(":", 1)[1])
        except ValueError:
            return None, None
    if raw.startswith("cat:"):
        try:
            return "cat", int(raw.split(":", 1)[1])
        except ValueError:
            return None, None
    if raw.isdigit():
        return "cat", int(raw)
    return None, None


@router.get("", response_class=HTMLResponse)
def kds_home(
    request: Request,
    db: DBSession,
    user: User = Depends(_perm),
):
    try:
        return _render_kds_home(request, db, user)
    except Exception:
        log.exception("kds_home failed")
        return templates.TemplateResponse(
            "admin_kds.html",
            {
                "request": request,
                "kds_error": (
                    "تعذّر تحميل شاشة المطبخ. أعد تشغيل السيرفر "
                    "(restart-server.bat) ثم حدّث الصفحة."
                ),
                "kitchen_sections": [],
                "departments": [],
                "legacy_roots": [],
                "selected_dept": None,
                "selected_cat": None,
                "selected_section": None,
                "section_kind": None,
                "section_id": None,
                "tickets": [],
                "only_open": True,
                "open_total": 0,
                "section_open": 0,
                "open_by_section": {},
                "open_by_dept": {},
                "open_by_cat": {},
                "show_all_sections": False,
            },
            status_code=200,
        )


def _render_kds_home(request: Request, db: DBSession, user: User) -> HTMLResponse:
    try:
        repaired = repair_missing_tickets_for_sent_sales(db, limit=50)
        if repaired:
            db.commit()
            log.warning("auto-repaired %s KDS orders without tickets", repaired)
    except Exception:
        db.rollback()
        log.exception("kds ticket auto-repair failed")
    scope = _kds_scope(user)
    kitchen_sections = [
        s for s in list_active_kitchen_sections(db) if _section_allowed(scope, s)
    ]
    departments = [
        d for d in list_active_departments(db) if _department_allowed(scope, d)
    ]
    legacy_roots = list_legacy_routed_roots(db) if scope == "ALL" else []
    open_counts = kds_open_counts(db)
    show_mode = request.query_params.get("show", "open")
    show_archived = show_mode == "archived"
    show_rejected = show_mode == "rejected"
    only_open = show_mode != "all" and not show_archived and not show_rejected
    kind, sel_id = _parse_section(request)
    if scope != "ALL":
        if kind == "sec" and not any(s.id == sel_id for s in kitchen_sections):
            kind, sel_id = None, None
        elif kind == "dept" and not any(d.id == sel_id for d in departments):
            kind, sel_id = None, None
        elif kind in ("cat", "all"):
            kind, sel_id = None, None
    if kind is None:
        kind, sel_id = kds_pick_default_section(
            db,
            kitchen_sections=kitchen_sections,
            departments=departments,
            legacy_roots=legacy_roots,
            open_counts=open_counts,
        )

    sec_id = sel_id if kind == "sec" else None
    dept_id = sel_id if kind == "dept" else None
    cat_id = sel_id if kind == "cat" else None
    show_all_sections = kind == "all"
    tickets = list_tickets(
        db,
        kitchen_section_id=sec_id,
        kitchen_department_id=dept_id,
        root_category_id=cat_id,
        only_open=only_open,
        archived=show_archived,
        rejected=show_rejected,
        limit=120 if show_all_sections else 80,
    )
    if scope != "ALL":
        tickets = [tv for tv in tickets if _ticket_allowed(scope, tv.ticket)]
    selected_dept = next((d for d in departments if d.id == dept_id), None) if dept_id else None
    selected_cat = next((c for c in legacy_roots if c.id == cat_id), None) if cat_id else None
    selected_section = (
        next((s for s in kitchen_sections if s.id == sec_id), None) if sec_id else None
    )
    if show_all_sections:
        section_open = len(tickets)
    elif kind == "sec":
        section_open = open_counts["by_section"].get(sec_id or 0, 0)
    elif kind == "dept":
        section_open = open_counts["by_dept"].get(dept_id or 0, 0)
    elif kind == "cat":
        section_open = open_counts["by_cat"].get(cat_id or 0, 0)
    else:
        section_open = 0

    from modules.sales.order_policy import (
        KDS_SOUND_PROFILE_LABELS,
        load_order_policy,
    )

    policy = load_order_policy(db)
    scoped_open_total = int(open_counts["total"])
    if scope != "ALL":
        scoped_open_total = 0
        scoped_open_total += sum(
            int(open_counts["by_section"].get(s.id, 0)) for s in kitchen_sections
        )
        scoped_open_total += sum(
            int(open_counts["by_dept"].get(d.id, 0)) for d in departments
        )
    return templates.TemplateResponse(
        "admin_kds.html",
        {
            "request": request,
            "kds_error": None,
            "kitchen_sections": kitchen_sections,
            "departments": departments,
            "legacy_roots": legacy_roots,
            "selected_dept": selected_dept,
            "selected_cat": selected_cat,
            "selected_section": selected_section,
            "section_kind": kind,
            "section_id": sel_id,
            "tickets": tickets,
            "only_open": only_open,
            "open_total": scoped_open_total,
            "section_open": section_open,
            "open_by_section": open_counts["by_section"],
            "open_by_dept": open_counts["by_dept"],
            "open_by_cat": open_counts["by_cat"],
            "show_all_sections": show_all_sections,
            "limited_kds_worker": _limited_kds_worker(user),
            "show_archived": show_archived,
            "show_rejected": show_rejected,
            "archived_count": archived_ticket_count(db) if not _limited_kds_worker(user) else 0,
            "rejected_count": rejected_ticket_count(db) if not _limited_kds_worker(user) else 0,
            "archive_saved": request.query_params.get("archived"),
            "order_policy": policy,
            "kds_sound_profiles": KDS_SOUND_PROFILE_LABELS,
        },
    )


@router.get("/poll.json")
def kds_poll_json(
    db: DBSession,
    user: User = Depends(_perm),
):
    from sqlalchemy import select

    from modules.sales.models import KitchenTicket, TicketStatus
    from modules.sales.order_policy import load_order_policy

    raw_pending_ids = list(
        db.scalars(
            select(KitchenTicket.id)
            .where(KitchenTicket.status == TicketStatus.PENDING)
            .order_by(KitchenTicket.id.desc())
            .limit(500)
        ).all()
    )
    scope = _kds_scope(user)
    pending_ids: list[int] = []
    for tid in raw_pending_ids:
        ticket = db.get(KitchenTicket, int(tid))
        if ticket is not None and _ticket_allowed(scope, ticket):
            pending_ids.append(int(tid))
    policy = load_order_policy(db)
    alert = None
    if pending_ids:
        from modules.kds.service import _lines_for_ticket, _section_label

        top_id = int(pending_ids[0])
        t = db.get(KitchenTicket, top_id)
        if t is not None and t.sale is not None:
            sale = t.sale
            lines = _lines_for_ticket(db, t, sale)
            tbl = sale.table.name_ar if sale.table else None
            alert = {
                "ticket_id": t.id,
                "sale_id": sale.id,
                "section": _section_label(t),
                "table": tbl,
                "lines": [
                    {"name": ln.name_ar, "qty": format_qty_plain(ln.qty)} for ln in lines[:12]
                ],
            }
    return JSONResponse(
        {
            "pending_count": len(pending_ids),
            "pending_max_id": max(pending_ids) if pending_ids else 0,
            "poll_sec": policy.kds_poll_sec,
            "sound_enabled": policy.kds_sound_enabled,
            "sound_repeat_sec": policy.kds_sound_repeat_sec,
            "sound_profile": policy.kds_sound_profile,
            "sound_volume": policy.kds_sound_volume,
            "alert": alert,
        }
    )


@router.post("/archive", response_class=HTMLResponse)
def archive_served(
    db: DBSession,
    user: User = Depends(_perm),
    period: str = Form("last_month"),
    custom_date: str = Form(""),
):
    if _limited_kds_worker(user):
        return RedirectResponse("/admin/kds", status_code=302)
    try:
        cutoff = _archive_cutoff(period, custom_date)
    except ValueError:
        cutoff = _archive_cutoff("last_month")
    n = archive_served_tickets_before(db, cutoff, user_id=user.id)
    db.commit()
    return RedirectResponse(f"/admin/kds?archived={n}", status_code=302)


def _section_query(kind: str | None, sid: int | None) -> str:
    if kind == "all":
        return "all"
    if kind == "sec" and sid is not None:
        return f"sec:{sid}"
    if kind == "dept" and sid:
        return f"dept:{sid}"
    if kind == "cat" and sid:
        return f"cat:{sid}"
    return ""


def _archive_cutoff(period: str, custom_date: str = "") -> datetime:
    today = date.today()
    key = (period or "").strip()
    if key == "last_month":
        cutoff_date = today.replace(day=1)
    elif key == "last_year":
        cutoff_date = date(today.year, 1, 1)
    elif key == "custom" and (custom_date or "").strip():
        cutoff_date = date.fromisoformat((custom_date or "").strip())
    else:
        cutoff_date = today.replace(day=1)
    return datetime.combine(cutoff_date, time.min).replace(tzinfo=timezone.utc)


@router.post("/{tid}/advance", response_class=HTMLResponse)
def advance(
    tid: int,
    db: DBSession,
    request: Request,
    user: User = Depends(_perm),
):
    from modules.sales.models import KitchenTicket

    ticket = db.get(KitchenTicket, tid)
    if ticket is None or not _ticket_allowed(_kds_scope(user), ticket):
        return RedirectResponse("/admin/kds", status_code=302)
    advance_ticket(db, tid)
    db.commit()
    kind, sid = _parse_section(request)
    return RedirectResponse(
        f"/admin/kds?section={_section_query(kind, sid)}", status_code=302
    )


@router.post("/{tid}/reject", response_class=HTMLResponse)
def reject(
    tid: int,
    db: DBSession,
    request: Request,
    reason: str = Form(""),
    user: User = Depends(_perm),
):
    from modules.sales.models import KitchenTicket

    ticket = db.get(KitchenTicket, tid)
    if ticket is None or not _ticket_allowed(_kds_scope(user), ticket):
        return RedirectResponse("/admin/kds", status_code=302)
    reject_ticket(
        db,
        tid,
        reason=reason,
        user_name=getattr(user, "username", "") or "",
    )
    db.commit()
    kind, sid = _parse_section(request)
    return RedirectResponse(
        f"/admin/kds?section={_section_query(kind, sid)}", status_code=302
    )


@router.get("/{tid}/reject", response_class=HTMLResponse)
def reject_get_fallback(request: Request):
    kind, sid = _parse_section(request)
    return RedirectResponse(
        f"/admin/kds?section={_section_query(kind, sid)}", status_code=302
    )


@router.post("/{tid}/reset", response_class=HTMLResponse)
def reset(
    tid: int,
    db: DBSession,
    request: Request,
    user: User = Depends(_perm),
):
    if _limited_kds_worker(user):
        return RedirectResponse("/admin/kds", status_code=302)
    reset_ticket(db, tid)
    db.commit()
    kind, sid = _parse_section(request)
    return RedirectResponse(
        f"/admin/kds?section={_section_query(kind, sid)}", status_code=302
    )


@router.get("/print/{tid}", response_class=HTMLResponse)
def print_ticket(
    tid: int,
    db: DBSession,
    request: Request,
    user: User = Depends(_perm),
):
    if _limited_kds_worker(user):
        return RedirectResponse("/admin/kds", status_code=302)
    import json

    from modules.sales.models import KitchenTicket

    t = db.get(KitchenTicket, tid)
    if t is None or t.sale is None:
        return RedirectResponse("/admin/kds", status_code=302)
    from modules.kds.service import complimentary_line_for_ticket, _section_label, lines_for_section_ticket

    sale = t.sale
    section_name = _section_label(t)
    section_obj = t.kitchen_section or t.kitchen_department or t.root_category
    supplement_rows: list[dict] = []
    if t.is_supplement and t.supplement_lines_json:
        try:
            supplement_rows = json.loads(t.supplement_lines_json)
        except json.JSONDecodeError:
            supplement_rows = []
    if t.kitchen_section_id and not supplement_rows:
        sale_lines = lines_for_section_ticket(db, t, sale)
    elif supplement_rows:
        sale_lines = []
    else:
        sale_lines = []
        for ln in sale.lines:
            if ln.product is None:
                continue
            if t.kitchen_department_id:
                if ln.product.kitchen_department_id == t.kitchen_department_id:
                    sale_lines.append(ln)
            elif t.root_category_id:
                from modules.sales.receipt_layout import resolve_root_category

                root = resolve_root_category(db, ln.product.category)
                if root and root.id == t.root_category_id:
                    sale_lines.append(ln)
    complimentary_rows = []
    if not t.is_supplement and sale_lines:
        extra = complimentary_line_for_ticket(db, t, sale)
        if extra is not None:
            complimentary_rows.append(extra)
    return templates.TemplateResponse(
        "kitchen_ticket_print.html",
        {
            "request": request,
            "ticket": t,
            "sale": sale,
            "section": section_obj,
            "section_name": section_name,
            "lines": sale_lines,
            "supplement_rows": supplement_rows,
            "complimentary_rows": complimentary_rows,
            "autoprint": int(request.query_params.get("autoprint", "0") or 0),
        },
    )


@router.get("/print-sale/{sale_id}", response_class=HTMLResponse)
def print_sale_master(
    sale_id: int,
    db: DBSession,
    request: Request,
    user: User = Depends(_perm),
):
    if _limited_kds_worker(user):
        return RedirectResponse("/admin/kds", status_code=302)
    from modules.sales.models import Sale

    sale = db.get(Sale, sale_id)
    if sale is None:
        return RedirectResponse("/admin/kds", status_code=302)
    sections = build_master_ticket_sections(db, sale)
    return templates.TemplateResponse(
        "kitchen_master_print.html",
        {
            "request": request,
            "sale": sale,
            "sections": sections,
            "autoprint": int(request.query_params.get("autoprint", "1") or 0),
        },
    )
