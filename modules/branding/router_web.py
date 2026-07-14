"""واجهات إدارة الهويّة البصرية للأدمن."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.requests import Request

from app.deps import DBSession, require_permission
from app.jinja_env import templates
from modules.authz.models import User
from modules.authz.permissions import BRANDING_MANAGE
from modules.branding.hotel_banners import (
    HotelBannerError,
    add_hotel_banner,
    delete_hotel_banner,
    get_hotel_banners_admin,
    set_banner_interval as set_hotel_banner_interval,
)
from modules.branding.shop_banners import (
    ShopBannerError,
    add_shop_banner,
    delete_shop_banner,
    set_banner_interval,
)
from modules.branding.service import (
    LogoError,
    delete_logo,
    get_branding,
    get_shop_branding,
    get_shop_branding_admin,
    reset_to_defaults,
    save_branding,
    save_hotel_branding,
    save_logo,
    save_shop_branding,
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
            "shop": get_shop_branding_admin(db),
            "hotel_banners_admin": get_hotel_banners_admin(db),
            "hotel_saved": request.query_params.get("hotel_saved"),
            "saved": request.query_params.get("saved"),
            "shop_saved": request.query_params.get("shop_saved"),
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
    hotel_name: str = Form(""),
    hotel_header_tagline: str = Form(""),
    reports_identity: str = Form("restaurant"),
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
    save_hotel_branding(
        db,
        hotel_name=hotel_name,
        hotel_header_tagline=hotel_header_tagline,
        reports_identity=reports_identity,
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


@router.post("/upload-hotel-logo", response_class=HTMLResponse)
async def branding_upload_hotel_logo(
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
            setting_key="brand_hotel_logo_filename",
        )
        db.commit()
    except LogoError as e:
        db.rollback()
        return RedirectResponse(f"/admin/branding?error={e}#hotel-branding", status_code=302)
    return RedirectResponse("/admin/branding?saved=1#hotel-branding", status_code=302)


@router.post("/upload-hotel-print-logo", response_class=HTMLResponse)
async def branding_upload_hotel_print_logo(
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
            setting_key="brand_hotel_print_logo_filename",
        )
        db.commit()
    except LogoError as e:
        db.rollback()
        return RedirectResponse(f"/admin/branding?error={e}#hotel-branding", status_code=302)
    return RedirectResponse("/admin/branding?saved=1#hotel-branding", status_code=302)


@router.post("/delete-hotel-logo", response_class=HTMLResponse)
def branding_delete_hotel_logo(
    db: DBSession,
    _: User = Depends(_perm),
):
    delete_logo(db, setting_key="brand_hotel_logo_filename")
    return RedirectResponse("/admin/branding?saved=1#hotel-branding", status_code=302)


@router.post("/delete-hotel-print-logo", response_class=HTMLResponse)
def branding_delete_hotel_print_logo(
    db: DBSession,
    _: User = Depends(_perm),
):
    delete_logo(db, setting_key="brand_hotel_print_logo_filename")
    return RedirectResponse("/admin/branding?saved=1#hotel-branding", status_code=302)


@router.post("/save-shop", response_class=HTMLResponse)
def branding_save_shop(
    db: DBSession,
    _: User = Depends(_perm),
    shop_use_brand_colors: str = Form(""),
    shop_header_bg_from: str = Form(""),
    shop_header_bg_to: str = Form(""),
    shop_header_fg: str = Form(""),
    shop_subtitle: str = Form(""),
    shop_store_name: str = Form(""),
    shop_cart_emoji: str = Form(""),
    shop_social_whatsapp: str = Form(""),
    shop_social_instagram: str = Form(""),
    shop_social_facebook: str = Form(""),
    shop_nav_menu_label: str = Form(""),
    shop_nav_menu_url: str = Form(""),
    shop_nav_offers_label: str = Form(""),
    shop_nav_offers_url: str = Form(""),
    shop_nav_offers_visible: str = Form(""),
    shop_nav_about_label: str = Form(""),
    shop_nav_about_url: str = Form(""),
    shop_nav_about_visible: str = Form(""),
    shop_login_label: str = Form(""),
    shop_login_url: str = Form(""),
    shop_page_offers_title: str = Form(""),
    shop_page_offers_body: str = Form(""),
    shop_page_about_title: str = Form(""),
    shop_page_about_body: str = Form(""),
):
    save_shop_branding(
        db,
        use_brand_colors=(shop_use_brand_colors == "on"),
        header_bg_from=shop_header_bg_from,
        header_bg_to=shop_header_bg_to,
        header_fg=shop_header_fg,
        subtitle=shop_subtitle,
        store_name=shop_store_name,
        cart_emoji=shop_cart_emoji,
        social_whatsapp=shop_social_whatsapp,
        social_instagram=shop_social_instagram,
        social_facebook=shop_social_facebook,
        nav_menu_label=shop_nav_menu_label,
        nav_menu_url=shop_nav_menu_url,
        nav_offers_label=shop_nav_offers_label,
        nav_offers_url=shop_nav_offers_url,
        nav_offers_visible=(shop_nav_offers_visible == "on"),
        nav_about_label=shop_nav_about_label,
        nav_about_url=shop_nav_about_url,
        nav_about_visible=(shop_nav_about_visible == "on"),
        login_label=shop_login_label,
        login_url=shop_login_url,
        page_offers_title=shop_page_offers_title,
        page_offers_body=shop_page_offers_body,
        page_about_title=shop_page_about_title,
        page_about_body=shop_page_about_body,
    )
    db.commit()
    return RedirectResponse("/admin/branding?shop_saved=1#shop-branding", status_code=302)


@router.post("/upload-shop-icon", response_class=HTMLResponse)
async def branding_upload_shop_icon(
    db: DBSession,
    _: User = Depends(_perm),
    icon: UploadFile = File(...),
):
    try:
        content = await icon.read()
        save_logo(
            db,
            filename=icon.filename or "",
            content=content,
            setting_key="brand_shop_icon_filename",
        )
        db.commit()
    except LogoError as e:
        db.rollback()
        return RedirectResponse(f"/admin/branding?error={e}", status_code=302)
    return RedirectResponse("/admin/branding?shop_saved=1", status_code=302)


@router.post("/delete-shop-icon", response_class=HTMLResponse)
def branding_delete_shop_icon(
    db: DBSession,
    _: User = Depends(_perm),
):
    delete_logo(db, setting_key="brand_shop_icon_filename")
    return RedirectResponse("/admin/branding?shop_saved=1", status_code=302)


@router.post("/save-shop-banners", response_class=HTMLResponse)
def branding_save_shop_banners(
    db: DBSession,
    _: User = Depends(_perm),
    banner_interval_seconds: int = Form(10),
):
    try:
        set_banner_interval(db, banner_interval_seconds)
        db.commit()
    except (TypeError, ValueError):
        return RedirectResponse("/admin/branding?error=مدة+البانر+غير+صالحة#shop-branding", status_code=302)
    return RedirectResponse("/admin/branding?shop_saved=1#shop-banners", status_code=302)


@router.post("/upload-shop-banner", response_class=HTMLResponse)
async def branding_upload_shop_banner(
    db: DBSession,
    _: User = Depends(_perm),
    banner_image: UploadFile = File(...),
    banner_link_url: str = Form(""),
):
    try:
        add_shop_banner(db, upload=banner_image, link_url=banner_link_url)
        db.commit()
    except ShopBannerError as exc:
        db.rollback()
        return RedirectResponse(f"/admin/branding?error={exc}#shop-banners", status_code=302)
    return RedirectResponse("/admin/branding?shop_saved=1#shop-banners", status_code=302)


@router.post("/delete-shop-banner/{banner_id}", response_class=HTMLResponse)
def branding_delete_shop_banner(
    banner_id: str,
    db: DBSession,
    _: User = Depends(_perm),
):
    delete_shop_banner(db, banner_id)
    db.commit()
    return RedirectResponse("/admin/branding?shop_saved=1#shop-banners", status_code=302)


@router.post("/save-hotel-banners", response_class=HTMLResponse)
def branding_save_hotel_banners(
    db: DBSession,
    _: User = Depends(_perm),
    hotel_banner_interval_seconds: int = Form(10),
):
    try:
        set_hotel_banner_interval(db, hotel_banner_interval_seconds)
        db.commit()
    except (TypeError, ValueError):
        return RedirectResponse("/admin/branding?error=مدة+البانر+غير+صالحة#hotel-banners", status_code=302)
    return RedirectResponse("/admin/branding?hotel_saved=1#hotel-banners", status_code=302)


@router.post("/upload-hotel-banner", response_class=HTMLResponse)
async def branding_upload_hotel_banner(
    db: DBSession,
    _: User = Depends(_perm),
    banner_image: UploadFile = File(...),
    link_url: str = Form(""),
):
    try:
        add_hotel_banner(db, upload=banner_image, link_url=link_url)
        db.commit()
    except HotelBannerError as exc:
        return RedirectResponse(f"/admin/branding?error={exc}#hotel-banners", status_code=302)
    return RedirectResponse("/admin/branding?hotel_saved=1#hotel-banners", status_code=302)


@router.post("/delete-hotel-banner/{banner_id}", response_class=HTMLResponse)
def branding_delete_hotel_banner(
    banner_id: str,
    db: DBSession,
    _: User = Depends(_perm),
):
    delete_hotel_banner(db, banner_id)
    db.commit()
    return RedirectResponse("/admin/branding?hotel_saved=1#hotel-banners", status_code=302)


@router.post("/reset", response_class=HTMLResponse)
def branding_reset(
    db: DBSession,
    _: User = Depends(_perm),
):
    reset_to_defaults(db)
    return RedirectResponse("/admin/branding?saved=1", status_code=302)
