"""إدارة SEO والتحليلات — /admin/web-marketing"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.requests import Request

from app.deps import DBSession, require_permission
from app.jinja_env import templates
from modules.authz.models import User
from modules.authz.permissions import BRANDING_MANAGE
from modules.web_marketing.analytics import get_analytics_dashboard
from modules.web_marketing.service import (
    SURFACE_HOTEL_PORTAL,
    SURFACE_RESTAURANT_SHOP,
    get_surface_admin_config,
    save_og_image,
    save_surface_config,
)

router = APIRouter(prefix="/admin/web-marketing", tags=["web-marketing"])
_perm = require_permission(BRANDING_MANAGE)

_STATIC = Path(__file__).resolve().parents[2] / "app" / "static" / "uploads"


@router.get("", response_class=HTMLResponse)
def web_marketing_page(
    request: Request,
    db: DBSession,
    _: User = Depends(_perm),
):
    tab = (request.query_params.get("tab") or "restaurant").strip().lower()
    if tab not in ("restaurant", "hotel"):
        tab = "restaurant"
    surface = SURFACE_HOTEL_PORTAL if tab == "hotel" else SURFACE_RESTAURANT_SHOP
    try:
        stats_days = int(request.query_params.get("days") or "30")
    except ValueError:
        stats_days = 30
    stats_days = max(7, min(stats_days, 365))
    if stats_days not in (7, 30, 90):
        stats_days = 30 if stats_days not in (7, 90) else stats_days
    return templates.TemplateResponse(
        "admin_web_marketing.html",
        {
            "request": request,
            "tab": tab,
            "restaurant": get_surface_admin_config(db, SURFACE_RESTAURANT_SHOP),
            "hotel": get_surface_admin_config(db, SURFACE_HOTEL_PORTAL),
            "stats": get_analytics_dashboard(db, surface, days=stats_days),
            "stats_days": stats_days,
            "saved": request.query_params.get("saved"),
            "error": request.query_params.get("error"),
        },
    )


def _save_surface(
    db: DBSession,
    surface: str,
    *,
    tab: str,
    enabled: str,
    meta_title: str,
    meta_description: str,
    meta_keywords: str,
    robots: str,
    google_analytics_id: str,
    google_tag_manager_id: str,
    facebook_pixel_id: str,
    microsoft_clarity_id: str,
    tiktok_pixel_id: str,
    google_site_verification: str,
    og_image: UploadFile | None,
) -> RedirectResponse:
    try:
        save_surface_config(
            db,
            surface,
            enabled=(enabled == "on"),
            meta_title=meta_title,
            meta_description=meta_description,
            meta_keywords=meta_keywords,
            robots=robots,
            google_analytics_id=google_analytics_id,
            google_tag_manager_id=google_tag_manager_id,
            facebook_pixel_id=facebook_pixel_id,
            microsoft_clarity_id=microsoft_clarity_id,
            tiktok_pixel_id=tiktok_pixel_id,
            google_site_verification=google_site_verification,
        )
        if og_image and og_image.filename:
            save_og_image(
                db,
                surface,
                upload=og_image,
                static_uploads_root=_STATIC,
            )
        db.commit()
    except ValueError as exc:
        db.rollback()
        from urllib.parse import quote

        return RedirectResponse(
            f"/admin/web-marketing?tab={tab}&error={quote(str(exc))}",
            status_code=302,
        )
    return RedirectResponse(
        f"/admin/web-marketing?tab={tab}&saved=1",
        status_code=302,
    )


@router.post("/save-restaurant", response_class=HTMLResponse)
def save_restaurant(
    db: DBSession,
    _: User = Depends(_perm),
    enabled: str = Form(""),
    meta_title: str = Form(""),
    meta_description: str = Form(""),
    meta_keywords: str = Form(""),
    robots: str = Form("index,follow"),
    google_analytics_id: str = Form(""),
    google_tag_manager_id: str = Form(""),
    facebook_pixel_id: str = Form(""),
    microsoft_clarity_id: str = Form(""),
    tiktok_pixel_id: str = Form(""),
    google_site_verification: str = Form(""),
    og_image: UploadFile | None = File(None),
):
    return _save_surface(
        db,
        SURFACE_RESTAURANT_SHOP,
        tab="restaurant",
        enabled=enabled,
        meta_title=meta_title,
        meta_description=meta_description,
        meta_keywords=meta_keywords,
        robots=robots,
        google_analytics_id=google_analytics_id,
        google_tag_manager_id=google_tag_manager_id,
        facebook_pixel_id=facebook_pixel_id,
        microsoft_clarity_id=microsoft_clarity_id,
        tiktok_pixel_id=tiktok_pixel_id,
        google_site_verification=google_site_verification,
        og_image=og_image,
    )


@router.post("/save-hotel", response_class=HTMLResponse)
def save_hotel(
    db: DBSession,
    _: User = Depends(_perm),
    enabled: str = Form(""),
    meta_title: str = Form(""),
    meta_description: str = Form(""),
    meta_keywords: str = Form(""),
    robots: str = Form("index,follow"),
    google_analytics_id: str = Form(""),
    google_tag_manager_id: str = Form(""),
    facebook_pixel_id: str = Form(""),
    microsoft_clarity_id: str = Form(""),
    tiktok_pixel_id: str = Form(""),
    google_site_verification: str = Form(""),
    og_image: UploadFile | None = File(None),
):
    return _save_surface(
        db,
        SURFACE_HOTEL_PORTAL,
        tab="hotel",
        enabled=enabled,
        meta_title=meta_title,
        meta_description=meta_description,
        meta_keywords=meta_keywords,
        robots=robots,
        google_analytics_id=google_analytics_id,
        google_tag_manager_id=google_tag_manager_id,
        facebook_pixel_id=facebook_pixel_id,
        microsoft_clarity_id=microsoft_clarity_id,
        tiktok_pixel_id=tiktok_pixel_id,
        google_site_verification=google_site_verification,
        og_image=og_image,
    )
