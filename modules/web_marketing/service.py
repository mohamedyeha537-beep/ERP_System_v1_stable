"""إعدادات SEO والتتبع — مطعم (/shop) وفندق (/stay) بشكل مستقل."""
from __future__ import annotations

import re
import uuid
from html import escape
from pathlib import Path
from typing import Any

from markupsafe import Markup
from sqlalchemy.orm import Session

from modules.settings.service import get_setting, set_setting

SURFACE_RESTAURANT_SHOP = "restaurant_shop"
SURFACE_HOTEL_PORTAL = "hotel_portal"

SURFACES: dict[str, dict[str, str]] = {
    SURFACE_RESTAURANT_SHOP: {
        "label_ar": "متجر المطعم",
        "public_path": "/shop",
        "preview_url": "/shop",
        "default_title_suffix": "المتجر",
    },
    SURFACE_HOTEL_PORTAL: {
        "label_ar": "بوابة الفندق (حجز + متجر الغرف)",
        "public_path": "/stay",
        "preview_url": "/stay",
        "default_title_suffix": "حجز إقامة",
    },
}

_FIELDS: tuple[str, ...] = (
    "enabled",
    "meta_title",
    "meta_description",
    "meta_keywords",
    "og_image_filename",
    "robots",
    "google_analytics_id",
    "google_tag_manager_id",
    "facebook_pixel_id",
    "microsoft_clarity_id",
    "tiktok_pixel_id",
    "google_site_verification",
)

_DEFAULTS: dict[str, str] = {
    "enabled": "1",
    "meta_title": "",
    "meta_description": "",
    "meta_keywords": "",
    "og_image_filename": "",
    "robots": "index,follow",
    "google_analytics_id": "",
    "google_tag_manager_id": "",
    "facebook_pixel_id": "",
    "microsoft_clarity_id": "",
    "tiktok_pixel_id": "",
    "google_site_verification": "",
}

_GA4_RE = re.compile(r"^G-[A-Z0-9]+$", re.I)
_GTM_RE = re.compile(r"^GTM-[A-Z0-9]+$", re.I)
_FB_RE = re.compile(r"^\d{5,20}$")
_CLARITY_RE = re.compile(r"^[a-z0-9]{5,40}$", re.I)
_TIKTOK_RE = re.compile(r"^[A-Z0-9]{5,40}$", re.I)
_ALLOWED_OG_EXT = {"png", "jpg", "jpeg", "webp"}
_MAX_OG_BYTES = 2 * 1024 * 1024


def _setting_key(surface: str, field: str) -> str:
    return f"web_mkt_{surface}_{field}"


def _clean_id(raw: str, pattern: re.Pattern[str]) -> str:
    val = (raw or "").strip()
    if not val:
        return ""
    return val if pattern.match(val) else ""


def _public_base(db: Session) -> str:
    base = (get_setting(db, "public_base_url", "") or "").strip().rstrip("/")
    if base:
        return base
    store = (get_setting(db, "store_name", "") or "").strip()
    return store  # fallback only for display hints


def get_surface_admin_config(db: Session, surface: str) -> dict[str, str]:
    if surface not in SURFACES:
        raise ValueError("surface unknown")
    out = dict(_DEFAULTS)
    for field in _FIELDS:
        out[field] = get_setting(db, _setting_key(surface, field), _DEFAULTS.get(field, ""))
    out["surface"] = surface
    out["label_ar"] = SURFACES[surface]["label_ar"]
    out["preview_url"] = SURFACES[surface]["preview_url"]
    og = (out.get("og_image_filename") or "").strip()
    out["og_image_url"] = f"/static/uploads/branding/{og}" if og else ""
    out["public_base_url"] = _public_base(db)
    return out


def save_surface_config(
    db: Session,
    surface: str,
    *,
    enabled: bool = True,
    meta_title: str = "",
    meta_description: str = "",
    meta_keywords: str = "",
    robots: str = "index,follow",
    google_analytics_id: str = "",
    google_tag_manager_id: str = "",
    facebook_pixel_id: str = "",
    microsoft_clarity_id: str = "",
    tiktok_pixel_id: str = "",
    google_site_verification: str = "",
) -> None:
    if surface not in SURFACES:
        raise ValueError("surface unknown")
    robots_val = (robots or "index,follow").strip().lower()
    if robots_val not in ("index,follow", "noindex,nofollow", "noindex,follow"):
        robots_val = "index,follow"
    values = {
        "enabled": "1" if enabled else "0",
        "meta_title": (meta_title or "").strip()[:160],
        "meta_description": (meta_description or "").strip()[:320],
        "meta_keywords": (meta_keywords or "").strip()[:240],
        "robots": robots_val,
        "google_analytics_id": _clean_id(google_analytics_id, _GA4_RE),
        "google_tag_manager_id": _clean_id(google_tag_manager_id, _GTM_RE),
        "facebook_pixel_id": _clean_id(facebook_pixel_id, _FB_RE),
        "microsoft_clarity_id": _clean_id(microsoft_clarity_id, _CLARITY_RE),
        "tiktok_pixel_id": _clean_id(tiktok_pixel_id, _TIKTOK_RE),
        "google_site_verification": (google_site_verification or "").strip()[:120],
    }
    for field, val in values.items():
        set_setting(db, _setting_key(surface, field), val)


def save_og_image(
    db: Session,
    surface: str,
    *,
    upload,
    static_uploads_root: Path,
) -> str | None:
    """رفع صورة OG — يُرجع اسم الملف أو None."""
    if surface not in SURFACES:
        raise ValueError("surface unknown")
    if upload is None or not getattr(upload, "filename", None):
        return None
    ext = (Path(upload.filename).suffix or "").lstrip(".").lower()
    if ext not in _ALLOWED_OG_EXT:
        raise ValueError("صورة OG: استخدم PNG أو JPG أو WebP.")
    data = upload.file.read()
    if len(data) > _MAX_OG_BYTES:
        raise ValueError("حجم صورة OG يتجاوز 2 ميغابايت.")
    dest_dir = static_uploads_root / "branding"
    dest_dir.mkdir(parents=True, exist_ok=True)
    fname = f"web_mkt_{surface}_og_{uuid.uuid4().hex[:12]}.{ext}"
    (dest_dir / fname).write_bytes(data)
    set_setting(db, _setting_key(surface, "og_image_filename"), fname)
    return fname


def _build_tracking_head(cfg: dict[str, str]) -> str:
    if (cfg.get("enabled") or "1") != "1":
        return ""
    parts: list[str] = []
    gtm = (cfg.get("google_tag_manager_id") or "").strip()
    ga = (cfg.get("google_analytics_id") or "").strip()
    fb = (cfg.get("facebook_pixel_id") or "").strip()
    clarity = (cfg.get("microsoft_clarity_id") or "").strip()
    tiktok = (cfg.get("tiktok_pixel_id") or "").strip()

    if gtm:
        gid = escape(gtm)
        parts.append(
            f"<script>(function(w,d,s,l,i){{w[l]=w[l]||[];w[l].push({{'gtm.start':"
            f"new Date().getTime(),event:'gtm.js'}});var f=d.getElementsByTagName(s)[0],"
            f"j=d.createElement(s),dl=l!='dataLayer'?'&l='+l:'';j.async=true;j.src="
            f"'https://www.googletagmanager.com/gtm.js?id='+i+dl;f.parentNode.insertBefore(j,f);"
            f"}})(window,document,'script','dataLayer','{gid}');</script>"
        )
    elif ga:
        gid = escape(ga)
        parts.append(
            f'<script async src="https://www.googletagmanager.com/gtag/js?id={gid}"></script>'
            f"<script>window.dataLayer=window.dataLayer||[];function gtag(){{dataLayer.push(arguments);}}"
            f"gtag('js',new Date());gtag('config','{gid}');</script>"
        )

    if fb:
        pid = escape(fb)
        parts.append(
            f"<script>!function(f,b,e,v,n,t,s){{if(f.fbq)return;n=f.fbq=function(){{n.callMethod?"
            f"n.callMethod.apply(n,arguments):n.queue.push(arguments)}};if(!f._fbq)f._fbq=n;"
            f"n.push=n;n.loaded=!0;n.version='2.0';n.queue=[];t=b.createElement(e);t.async=!0;"
            f"t.src=v;s=b.getElementsByTagName(e)[0];s.parentNode.insertBefore(t,s)}}(window,"
            f"document,'script','https://connect.facebook.net/en_US/fbevents.js');"
            f"fbq('init','{pid}');fbq('track','PageView');</script>"
            f'<noscript><img height="1" width="1" style="display:none" alt="" '
            f'src="https://www.facebook.com/tr?id={pid}&ev=PageView&noscript=1"/></noscript>'
        )

    if clarity:
        cid = escape(clarity)
        parts.append(
            f'<script type="text/javascript">(function(c,l,a,r,i,t,y){{c[a]=c[a]||function(){{'
            f'(c[a].q=c[a].q||[]).push(arguments)}};t=l.createElement(r);t.async=1;t.src='
            f'"https://www.clarity.ms/tag/"+i;y=l.getElementsByTagName(r)[0];y.parentNode.insertBefore(t,y);'
            f"}})(window,document,'clarity','script','{cid}');</script>"
        )

    if tiktok:
        tid = escape(tiktok)
        parts.append(
            f"<script>!function(w,d,t){{w.TiktokAnalyticsObject=t;var ttq=w[t]=w[t]||[];"
            f"ttq.methods=['page','track','identify','instances','debug','on','off','once','ready',"
            f"'alias','group','enableCookie','disableCookie'],ttq.setAndDefer=function(t,e){{"
            f"t[e]=function(){{t.push([e].concat(Array.prototype.slice.call(arguments,0)))}}}};"
            f"for(var i=0;i<ttq.methods.length;i++)ttq.setAndDefer(ttq,ttq.methods[i]);"
            f"ttq.instance=function(t){{for(var e=ttq._i[t]||[],n=0;n<ttq.methods.length;n++)"
            f"ttq.setAndDefer(e,ttq.methods[n]);return e}},ttq.load=function(e,n){{var i="
            f"'https://analytics.tiktok.com/i18n/pixel/events.js';ttq._i=ttq._i||{{}},ttq._i[e]=[],"
            f"ttq._i[e]._u=i,ttq._t=ttq._t||{{}},ttq._t[e]=+new Date,ttq._o=ttq._o||{{}},"
            f"ttq._o[e]=n||{{}};var o=document.createElement('script');o.type='text/javascript',"
            f"o.async=!0,o.src=i+'?sdkid='+e+'&lib='+t;var a=document.getElementsByTagName('script')[0];"
            f"a.parentNode.insertBefore(o,a)}};ttq.load('{tid}');ttq.page();}}(window,document,'ttq');</script>"
        )

    return "\n".join(parts)


def _build_tracking_body(cfg: dict[str, str]) -> str:
    gtm = (cfg.get("google_tag_manager_id") or "").strip()
    if (cfg.get("enabled") or "1") != "1" or not gtm:
        return ""
    gid = escape(gtm)
    return (
        f'<noscript><iframe src="https://www.googletagmanager.com/ns.html?id={gid}" '
        f'height="0" width="0" style="display:none;visibility:hidden"></iframe></noscript>'
    )


def get_public_web_config(
    db: Session,
    surface: str,
    *,
    page_path: str | None = None,
    page_title: str | None = None,
    default_title: str | None = None,
) -> dict[str, Any]:
    """إعدادات العرض العام — meta + أكواد التتبع."""
    if surface not in SURFACES:
        raise ValueError("surface unknown")
    cfg = get_surface_admin_config(db, surface)
    meta = SURFACES[surface]
    path = (page_path or meta["public_path"]).strip()
    if not path.startswith("/"):
        path = "/" + path

    title = (page_title or cfg.get("meta_title") or default_title or meta["default_title_suffix"]).strip()
    desc = (cfg.get("meta_description") or "").strip()
    keywords = (cfg.get("meta_keywords") or "").strip()
    robots = (cfg.get("robots") or "index,follow").strip()

    base = _public_base(db)
    canonical = f"{base}{path}" if base.startswith("http") else ""

    og_url = (cfg.get("og_image_url") or "").strip()
    if og_url and base.startswith("http") and og_url.startswith("/"):
        og_abs = f"{base}{og_url}"
    else:
        og_abs = og_url

    gsv = (cfg.get("google_site_verification") or "").strip()
    tracking_head = _build_tracking_head(cfg)
    tracking_body = _build_tracking_body(cfg)

    return {
        "surface": surface,
        "label_ar": meta["label_ar"],
        "page_title": title,
        "meta_title": title,
        "meta_description": desc,
        "meta_keywords": keywords,
        "robots": robots,
        "canonical_url": canonical,
        "og_title": title,
        "og_description": desc,
        "og_image_url": og_abs,
        "og_type": "website",
        "google_site_verification": gsv,
        "tracking_enabled": (cfg.get("enabled") or "1") == "1",
        "tracking_head": Markup(tracking_head) if tracking_head else Markup(""),
        "tracking_body": Markup(tracking_body) if tracking_body else Markup(""),
        "has_google": bool(cfg.get("google_tag_manager_id") or cfg.get("google_analytics_id")),
        "has_facebook": bool(cfg.get("facebook_pixel_id")),
        "has_clarity": bool(cfg.get("microsoft_clarity_id")),
        "has_tiktok": bool(cfg.get("tiktok_pixel_id")),
        "analytics_init": {
            "surface": surface,
            "page_path": path,
            "currency": "LYD",
            "track_external": (cfg.get("enabled") or "1") == "1",
        },
    }
