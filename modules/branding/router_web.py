"""واجهات إدارة الهويّة البصرية للأدمن."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.requests import Request

from app.deps import DBSession, require_permission
from app.jinja_env import templates
from modules.authz.models import User
from modules.authz.permissions import BRANDING_MANAGE
from modules.branding.service import (
    LogoError,
    delete_logo,
    get_branding,
    reset_to_defaults,
    save_branding,
    save_logo,
)

router = APIRouter(prefix="/admin/branding", tags=["branding"])
_perm = require_permission(BRANDING_MANAGE)


@router.get("", response_class=HTMLResponse)
def branding_page(
    request: Request,
    db: DBSession,
    _: User = Depends(_perm),
):
    return templates.TemplateResponse(
        "admin_branding.html",
        {
            "request": request,
            "brand": get_branding(db),
            "saved": request.query_params.get("saved"),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/save", response_class=HTMLResponse)
def branding_save(
    db: DBSession,
    _: User = Depends(_perm),
    header_bg_from: str = Form(""),
    header_bg_to: str = Form(""),
    header_fg: str = Form(""),
    footer_bg: str = Form(""),
    footer_fg: str = Form(""),
    primary_color: str = Form(""),
    accent_color: str = Form(""),
    show_logo_in_header: str = Form(""),
    show_name_in_header: str = Form(""),
    pos_label: str = Form(""),
    header_tagline: str = Form(""),
    footer_tagline: str = Form(""),
    receipt_title: str = Form(""),
    receipt_footer_text: str = Form(""),
):
    save_branding(
        db,
        header_bg_from=header_bg_from,
        header_bg_to=header_bg_to,
        header_fg=header_fg,
        footer_bg=footer_bg,
        footer_fg=footer_fg,
        primary_color=primary_color,
        accent_color=accent_color,
        show_logo_in_header=(show_logo_in_header == "on"),
        show_name_in_header=(show_name_in_header == "on"),
        pos_label=pos_label,
        header_tagline=header_tagline,
        footer_tagline=footer_tagline,
        receipt_title=receipt_title,
        receipt_footer_text=receipt_footer_text,
    )
    db.commit()
    return RedirectResponse("/admin/branding?saved=1", status_code=302)


@router.post("/upload-logo", response_class=HTMLResponse)
async def branding_upload_logo(
    db: DBSession,
    _: User = Depends(_perm),
    logo: UploadFile = File(...),
):
    try:
        content = await logo.read()
        save_logo(db, filename=logo.filename or "", content=content)
        db.commit()
    except LogoError as e:
        db.rollback()
        return RedirectResponse(
            f"/admin/branding?error={e}", status_code=302
        )
    return RedirectResponse("/admin/branding?saved=1", status_code=302)


@router.post("/upload-print-logo", response_class=HTMLResponse)
async def branding_upload_print_logo(
    db: DBSession,
    _: User = Depends(_perm),
    logo: UploadFile = File(...),
):
    try:
        content = await logo.read()
        save_logo(
            db,
            filename=logo.filename or "",
            content=content,
            setting_key="brand_print_logo_filename",
        )
        db.commit()
    except LogoError as e:
        db.rollback()
        return RedirectResponse(
            f"/admin/branding?error={e}", status_code=302
        )
    return RedirectResponse("/admin/branding?saved=1", status_code=302)


@router.post("/delete-logo", response_class=HTMLResponse)
def branding_delete_logo(
    db: DBSession,
    _: User = Depends(_perm),
):
    delete_logo(db)
    return RedirectResponse("/admin/branding?saved=1", status_code=302)


@router.post("/delete-print-logo", response_class=HTMLResponse)
def branding_delete_print_logo(
    db: DBSession,
    _: User = Depends(_perm),
):
    delete_logo(db, setting_key="brand_print_logo_filename")
    return RedirectResponse("/admin/branding?saved=1", status_code=302)


@router.post("/reset", response_class=HTMLResponse)
def branding_reset(
    db: DBSession,
    _: User = Depends(_perm),
):
    reset_to_defaults(db)
    return RedirectResponse("/admin/branding?saved=1", status_code=302)
