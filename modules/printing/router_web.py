"""إدارة الطابعات وأقسام المطبخ ومهام الطباعة."""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import joinedload

from app.deps import DBSession, require_permission
from app.jinja_env import templates
from modules.authz.models import User
from modules.authz.permissions import ADMIN_SETTINGS, CATALOG_WRITE
from modules.printing.models import (
    KitchenSection,
    PrintAgent,
    PrintJob,
    PrintJobStatus,
    Printer,
    PrinterConnectionType,
)
from modules.printing.service import (
    cancel_open_print_jobs,
    create_test_print_job,
    generate_agent_token,
    retry_all_failed_print_jobs,
    retry_print_job,
)

router = APIRouter(prefix="/admin/printing", tags=["printing-admin"])
_perm = require_permission(ADMIN_SETTINGS)
_catalog = require_permission(CATALOG_WRITE)


@router.get("", response_class=HTMLResponse)
def printing_hub(request: Request, _: User = Depends(_perm)):
    return templates.TemplateResponse(
        "admin_printing_hub.html",
        {"request": request},
    )


@router.get("/agents", response_class=HTMLResponse)
def list_agents(request: Request, db: DBSession, _: User = Depends(_perm)):
    from datetime import datetime, timedelta, timezone

    rows = list(
        db.scalars(select(PrintAgent).order_by(PrintAgent.id.desc())).all()
    )
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=90)
    agent_online: dict[int, bool] = {}
    for a in rows:
        seen = a.last_seen_at
        if seen is not None and seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
        agent_online[int(a.id)] = bool(
            a.is_active and seen is not None and seen >= cutoff
        )
    return templates.TemplateResponse(
        "admin_print_agents.html",
        {
            "request": request,
            "agents": rows,
            "agent_online": agent_online,
            "new_token": request.query_params.get("token"),
        },
    )


@router.post("/agents/new", response_class=HTMLResponse)
def create_agent(
    db: DBSession,
    _: User = Depends(_perm),
    name: str = Form(...),
):
    plain, token_hash = generate_agent_token()
    agent = PrintAgent(name=(name or "").strip() or "وكيل", token_hash=token_hash)
    db.add(agent)
    db.commit()
    return RedirectResponse(
        f"/admin/printing/agents?token={quote(plain)}", status_code=302
    )


@router.post("/agents/{agent_id}/toggle", response_class=HTMLResponse)
def toggle_agent(agent_id: int, db: DBSession, _: User = Depends(_perm)):
    agent = db.get(PrintAgent, agent_id)
    if agent:
        agent.is_active = not agent.is_active
        db.commit()
    return RedirectResponse("/admin/printing/agents", status_code=302)


@router.get("/printers", response_class=HTMLResponse)
def list_printers(request: Request, db: DBSession, _: User = Depends(_perm)):
    rows = list(db.scalars(select(Printer).order_by(Printer.name)).all())
    agents = list(db.scalars(select(PrintAgent).where(PrintAgent.is_active.is_(True))).all())
    err = request.query_params.get("err")
    ok = request.query_params.get("ok")
    return templates.TemplateResponse(
        "admin_printers.html",
        {
            "request": request,
            "printers": rows,
            "agents": agents,
            "connection_types": list(PrinterConnectionType),
            "err": err,
            "ok": ok,
        },
    )


@router.post("/printers/save", response_class=HTMLResponse)
def save_printer(
    db: DBSession,
    _: User = Depends(_perm),
    printer_id: str = Form(""),
    name: str = Form(...),
    code: str = Form(...),
    connection_type: str = Form("local_agent"),
    agent_id: str = Form(""),
    local_printer_key: str = Form(""),
    ip_address: str = Form(""),
    port: str = Form("9100"),
    paper_width: str = Form("80"),
    is_active: str = Form("on"),
):
    code_val = (code or "").strip().upper()
    if not code_val:
        return RedirectResponse(
            "/admin/printing/printers?err=" + quote("رمز الطابعة مطلوب."),
            status_code=302,
        )
    try:
        conn = PrinterConnectionType(connection_type)
    except ValueError:
        conn = PrinterConnectionType.LOCAL_AGENT
    aid = int(agent_id) if agent_id.strip().isdigit() else None
    try:
        port_val = int(port.strip() or "9100")
    except ValueError:
        port_val = 9100
    try:
        width_val = int(paper_width.strip() or "80")
    except ValueError:
        width_val = 80
    if printer_id.strip().isdigit():
        p = db.get(Printer, int(printer_id))
        if p is None:
            return RedirectResponse("/admin/printing/printers", status_code=302)
    else:
        p = Printer(name="", code=code_val)
        db.add(p)
    p.name = (name or "").strip() or code_val
    p.code = code_val
    p.connection_type = conn.value
    p.agent_id = aid
    p.local_printer_key = (local_printer_key or "").strip() or None
    p.ip_address = (ip_address or "").strip() or None
    p.port = port_val
    p.paper_width = width_val
    p.is_active = is_active == "on"
    db.commit()
    return RedirectResponse("/admin/printing/printers?ok=1", status_code=302)


@router.post("/printers/{printer_id}/test", response_class=HTMLResponse)
def test_printer(printer_id: int, db: DBSession, _: User = Depends(_perm)):
    try:
        create_test_print_job(db, printer_id)
        db.commit()
        return RedirectResponse(
            "/admin/printing/printers?ok=" + quote("تم إنشاء مهمة اختبار."),
            status_code=302,
        )
    except ValueError as e:
        db.rollback()
        return RedirectResponse(
            "/admin/printing/printers?err=" + quote(str(e)), status_code=302
        )


@router.get("/sections", response_class=HTMLResponse)
def list_sections(request: Request, db: DBSession, _: User = Depends(_catalog)):
    rows = list(
        db.scalars(
            select(KitchenSection)
            .options(joinedload(KitchenSection.printer))
            .order_by(KitchenSection.name)
        ).all()
    )
    printers = list(
        db.scalars(select(Printer).where(Printer.is_active.is_(True))).all()
    )
    from modules.kds.sections_seed import DEFAULT_KITCHEN_SECTIONS

    return templates.TemplateResponse(
        "admin_kitchen_sections.html",
        {
            "request": request,
            "sections": rows,
            "printers": printers,
            "default_sections": DEFAULT_KITCHEN_SECTIONS,
        },
    )


@router.post("/sections/save", response_class=HTMLResponse)
def save_section(
    db: DBSession,
    _: User = Depends(_catalog),
    section_id: str = Form(""),
    name: str = Form(...),
    code: str = Form(...),
    printer_id: str = Form(""),
    is_active: str = Form("on"),
):
    code_val = (code or "").strip().upper()
    if not code_val:
        return RedirectResponse(
            "/admin/printing/sections?err=" + quote("رمز القسم مطلوب."),
            status_code=302,
        )
    pid = int(printer_id) if printer_id.strip().isdigit() else None
    if section_id.strip().isdigit():
        sec = db.get(KitchenSection, int(section_id))
        if sec is None:
            return RedirectResponse("/admin/printing/sections", status_code=302)
    else:
        sec = KitchenSection(name="", code=code_val)
        db.add(sec)
    sec.name = (name or "").strip() or code_val
    sec.code = code_val
    sec.printer_id = pid
    sec.is_active = is_active == "on"
    db.commit()
    return RedirectResponse("/admin/printing/sections", status_code=302)


@router.get("/jobs", response_class=HTMLResponse)
def list_jobs(request: Request, db: DBSession, _: User = Depends(_perm)):
    status = request.query_params.get("status", "")
    stmt = (
        select(PrintJob)
        .options(joinedload(PrintJob.printer))
        .order_by(PrintJob.id.desc())
        .limit(200)
    )
    if status:
        stmt = stmt.where(PrintJob.status == status)
    rows = list(db.scalars(stmt).unique().all())
    failed_count = (
        db.scalar(
            select(func.count())
            .select_from(PrintJob)
            .where(PrintJob.status == PrintJobStatus.FAILED.value)
        )
        or 0
    )
    return templates.TemplateResponse(
        "admin_print_jobs.html",
        {
            "request": request,
            "jobs": rows,
            "status_filter": status,
            "statuses": list(PrintJobStatus),
            "has_failed_jobs": failed_count > 0,
            "err": request.query_params.get("err", ""),
            "retried": request.query_params.get("retried", ""),
        },
    )


@router.post("/jobs/{job_id}/retry", response_class=HTMLResponse)
def retry_job(job_id: int, db: DBSession, _: User = Depends(_perm)):
    try:
        retry_print_job(db, job_id)
        db.commit()
    except ValueError as e:
        db.rollback()
        return RedirectResponse(
            "/admin/printing/jobs?status=failed&err=" + quote(str(e)),
            status_code=302,
        )
    return RedirectResponse("/admin/printing/jobs?status=pending", status_code=302)


@router.post("/jobs/retry-failed", response_class=HTMLResponse)
def retry_all_failed(db: DBSession, _: User = Depends(_perm)):
    count = retry_all_failed_print_jobs(db)
    db.commit()
    return RedirectResponse(
        f"/admin/printing/jobs?status=pending&retried={count}",
        status_code=302,
    )


@router.post("/jobs/cancel-open", response_class=HTMLResponse)
def cancel_open_jobs(db: DBSession, _: User = Depends(_perm)):
    count = cancel_open_print_jobs(db)
    db.commit()
    return RedirectResponse(
        f"/admin/printing/jobs?cleaned={count}",
        status_code=302,
    )
