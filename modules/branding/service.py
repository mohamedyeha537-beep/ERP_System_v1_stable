"""خدمة الهويّة البصرية: الشعار + الألوان.

تُحفظ القيم في `AppSetting` (جدول key/value) كي لا تحتاج هجرات مستقلة،
ومستقبلاً عند تفعيل المالتي-فندور يصبح كل tenant له صف خاص في هذا الجدول.

كل القيم لها افتراضيات آمنة → النظام يعمل قبل أي تخصيص.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from modules.settings.service import get_setting, set_setting
from modules.settings.models import AppSetting

# ============================================================
# Constants & defaults
# ============================================================

LOGO_DIR_NAME = "branding"  # داخل static/uploads
ALLOWED_LOGO_EXT = {"png", "jpg", "jpeg", "webp", "svg", "gif"}
MAX_LOGO_BYTES = 1 * 1024 * 1024  # 1 MiB

# هذه القيم تطابق الـ CSS الافتراضي السابق في base.html
# (gradient أزرق-بنفسجي للهيدر + slate للفوتر)
DEFAULTS: dict[str, str] = {
    "brand_logo_filename": "",
    "brand_print_logo_filename": "",
    # الهيدر: نخزّن لونين (يُستخدمان كـ gradient) + لون النص
    "brand_header_bg_from": "#0f172a",
    "brand_header_bg_to": "#1e293b",
    "brand_header_fg": "#ffffff",
    # الفوتر
    "brand_footer_bg": "#0f172a",
    "brand_footer_fg": "#94a3b8",
    # الألوان الأساسية (الأزرار/الروابط/الـ accent)
    "brand_primary_color": "#3b82f6",
    "brand_accent_color": "#8b5cf6",
    # عرض الشعار في الهيدر بدل الأيقونة الافتراضية
    "brand_show_logo_in_header": "1",
    # عرض اسم المتجر بجانب الشعار
    "brand_show_name_in_header": "1",
    # نصوص الواجهة والطباعة
    "brand_pos_label": "نقطة البيع — بيتك",
    "brand_header_tagline": "Bayatak — Roof Caffee",
    "brand_footer_tagline": (
        "نظام نقطة بيع متكامل لكافيه روف الفندق: مبيعات، مخزون، فندق، "
        "موظفون، رواتب، وتقارير محاسبية."
    ),
    "brand_receipt_title": "فاتورة بيع",
    "brand_receipt_footer_text": "شكراً لزيارتكم بيتك — نتمنى لكم يوماً سعيداً",
    # هوية الفندق (منفصلة عن المطعم)
    "brand_hotel_name": "بيتك للشقق الفندقية",
    "brand_hotel_logo_filename": "",
    "brand_hotel_print_logo_filename": "",
    "brand_hotel_header_tagline": "إدارة الشقق والأجنحة الفندقية",
    # أي هوية تُستخدم في تقارير الوضع العام (طباعة/تصدير)
    "brand_reports_identity": "restaurant",  # restaurant | hotel
    # صفحة المتجر الإلكتروني /shop
    "brand_shop_use_brand_colors": "1",
    "brand_shop_header_bg_from": "",
    "brand_shop_header_bg_to": "",
    "brand_shop_header_fg": "#ffffff",
    "brand_shop_subtitle": "اطلب من كافيه روف بيتك — توصيل أو استلام",
    "brand_shop_store_name": "Bayatak — Roof Caffee",
    "brand_shop_icon_filename": "",
    "brand_shop_cart_emoji": "🛒",
    # روابط التواصل في هيدر المتجر
    "brand_shop_social_whatsapp": "",
    "brand_shop_social_instagram": "",
    "brand_shop_social_facebook": "",
    # روابط القائمة العلوية
    "brand_shop_nav_menu_label": "القائمة",
    "brand_shop_nav_menu_url": "/shop",
    "brand_shop_nav_offers_label": "العروض",
    "brand_shop_nav_offers_url": "",
    "brand_shop_nav_about_label": "من نحن",
    "brand_shop_nav_about_url": "",
    "brand_shop_login_label": "دخول",
    "brand_shop_login_url": "",
    # صفحات معلومات المتجر (/offers و /about)
    "brand_shop_page_offers_title": "العروض",
    "brand_shop_page_offers_body": "اطّلع على أحدث العروض والتخفيضات لدينا.\nتابعنا باستمرار لمعرفة الجديد.",
    "brand_shop_page_about_title": "من نحن",
    "brand_shop_page_about_body": "نرحّب بكم في متجرنا.\nنسعى لتقديم أفضل المنتجات والخدمة بكل حب واهتمام.",
}

_DEFAULT_OFFERS_PATH = "/offers"
_DEFAULT_ABOUT_PATH = "/about"


def _nav_page_url(raw_url: str | None, *, visible: bool, default_path: str) -> str:
    """رابط عنصر القائمة: فارغ = مخفي، وإلا الرابط المخصّص أو المسار الافتراضي."""
    if not visible:
        return ""
    url = _normalize_url(raw_url)
    return url or default_path


def _multiline_text(raw: str | None, fallback: str, max_len: int = 8000) -> str:
    """نص متعدد الأسطر لمحتوى الصفحات."""
    if raw is None:
        return fallback
    text = str(raw).replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return fallback
    return text[:max_len]


# ============================================================
# Helpers
# ============================================================

_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def _normalize_hex(raw: str | None, fallback: str) -> str:
    """يُحوّل #abc → #aabbcc، ويُرجع fallback إن لم يكن لوناً صحيحاً."""
    if not raw:
        return fallback
    s = raw.strip()
    if not _HEX_RE.match(s):
        return fallback
    if len(s) == 4:  # #abc → #aabbcc
        return "#" + "".join(c * 2 for c in s[1:])
    return s.lower()


def _normalize_url(raw: str | None, *, max_len: int = 500) -> str:
    """رابط اختياري — فارغ يعني إخفاء العنصر."""
    if raw is None:
        return ""
    s = str(raw).strip()
    if not s:
        return ""
    if s.startswith("/") or s.startswith("#"):
        return s[:max_len]
    if "://" not in s:
        s = "https://" + s
    return s[:max_len]


def _normalize_whatsapp_url(raw: str | None) -> str:
    """يقبل رابط wa.me أو رقماً هاتفياً."""
    s = (raw or "").strip()
    if not s:
        return ""
    lower = s.lower()
    if "wa.me" in lower or "whatsapp.com" in lower or "api.whatsapp.com" in lower:
        return _normalize_url(s)
    digits = re.sub(r"\D", "", s)
    if len(digits) >= 9:
        if digits.startswith("0"):
            digits = "218" + digits[1:]
        elif not digits.startswith("218") and len(digits) <= 10:
            digits = "218" + digits
        return f"https://wa.me/{digits}"[:500]
    return _normalize_url(s)


def _setting_raw(db: Session, key: str, *, default: str = "") -> str:
    """القيمة المخزّنة حرفياً — للنموذج في الأدمن."""
    row = db.get(AppSetting, key)
    if row is None:
        return default
    return (row.value or "").strip() if row.value is not None else default


def shop_store_name(db: Session) -> str:
    """اسم المتجر في /shop — إعداد مخصص أو fallback لـ store_name."""
    custom = _normalize_text(get_setting(db, "brand_shop_store_name", ""), "", 80)
    if custom:
        return custom
    return _normalize_text(
        get_setting(db, "store_name", "نقطة البيع"),
        "نقطة البيع",
        120,
    )


def get_shop_branding_admin(db: Session) -> dict:
    """إعدادات المتجر لنموذج الأدمن — تُعرض القيم المحفوظة كما هي."""
    from modules.branding.shop_banners import get_shop_banners_admin

    data = get_shop_branding(db)
    data["store_name"] = shop_store_name(db)
    data["store_name_raw"] = _setting_raw(db, "brand_shop_store_name")
    data["social_whatsapp"] = _setting_raw(db, "brand_shop_social_whatsapp")
    data["social_instagram"] = _setting_raw(db, "brand_shop_social_instagram")
    data["social_facebook"] = _setting_raw(db, "brand_shop_social_facebook")
    data["nav_menu_label"] = _setting_raw(
        db, "brand_shop_nav_menu_label", default=DEFAULTS["brand_shop_nav_menu_label"]
    )
    data["nav_menu_url"] = _setting_raw(
        db, "brand_shop_nav_menu_url", default=DEFAULTS["brand_shop_nav_menu_url"]
    )
    data["nav_offers_label"] = _setting_raw(
        db, "brand_shop_nav_offers_label", default=DEFAULTS["brand_shop_nav_offers_label"]
    )
    data["nav_offers_url"] = _setting_raw(db, "brand_shop_nav_offers_url")
    data["nav_offers_visible"] = bool((data["nav_offers_url"] or "").strip())
    data["nav_about_label"] = _setting_raw(
        db, "brand_shop_nav_about_label", default=DEFAULTS["brand_shop_nav_about_label"]
    )
    data["nav_about_url"] = _setting_raw(db, "brand_shop_nav_about_url")
    data["nav_about_visible"] = bool((data["nav_about_url"] or "").strip())
    data["login_label"] = _setting_raw(
        db, "brand_shop_login_label", default=DEFAULTS["brand_shop_login_label"]
    )
    data["login_url"] = _setting_raw(db, "brand_shop_login_url")
    data["page_offers_title"] = _setting_raw(
        db, "brand_shop_page_offers_title", default=DEFAULTS["brand_shop_page_offers_title"]
    )
    data["page_offers_body"] = _setting_raw(
        db, "brand_shop_page_offers_body", default=DEFAULTS["brand_shop_page_offers_body"]
    )
    data["page_about_title"] = _setting_raw(
        db, "brand_shop_page_about_title", default=DEFAULTS["brand_shop_page_about_title"]
    )
    data["page_about_body"] = _setting_raw(
        db, "brand_shop_page_about_body", default=DEFAULTS["brand_shop_page_about_body"]
    )
    data["banners_admin"] = get_shop_banners_admin(db)
    return data


def _normalize_text(raw: str | None, fallback: str, max_len: int = 240) -> str:
    """ينظّف النصوص الحرة مع حد أقصى حتى لا تكسر الواجهة والطباعة."""
    if raw is None:
        return fallback
    text = " ".join(str(raw).strip().split())
    if not text:
        return fallback
    return text[:max_len]


def _logo_dir() -> Path:
    """مجلّد رفع الشعارات (داخل static/uploads/branding)."""
    base = Path(__file__).resolve().parents[2] / "app" / "static" / "uploads" / LOGO_DIR_NAME
    base.mkdir(parents=True, exist_ok=True)
    return base


def _logo_url(filename: str) -> str:
    filename = (filename or "").strip()
    if not filename:
        return ""
    return f"/static/uploads/{LOGO_DIR_NAME}/{filename}"


# ============================================================
# Public API
# ============================================================


def get_branding(db: Session) -> dict:
    """هوية المطعم الأساسية (ألوان + شعار واسم المطعم) — بدون تطبيق وضع العرض."""
    from modules.settings.service import get_settings_bulk

    raw = get_settings_bulk(db, DEFAULTS)

    # تطبيع الألوان لضمان صحّتها قبل وصولها للـ CSS
    return {
        "logo_filename": raw["brand_logo_filename"].strip(),
        "logo_url": _logo_url(raw["brand_logo_filename"]),
        "print_logo_filename": raw["brand_print_logo_filename"].strip(),
        "print_logo_url": _logo_url(raw["brand_print_logo_filename"]),
        "header_bg_from": _normalize_hex(
            raw["brand_header_bg_from"], DEFAULTS["brand_header_bg_from"]
        ),
        "header_bg_to": _normalize_hex(
            raw["brand_header_bg_to"], DEFAULTS["brand_header_bg_to"]
        ),
        "header_fg": _normalize_hex(
            raw["brand_header_fg"], DEFAULTS["brand_header_fg"]
        ),
        "footer_bg": _normalize_hex(
            raw["brand_footer_bg"], DEFAULTS["brand_footer_bg"]
        ),
        "footer_fg": _normalize_hex(
            raw["brand_footer_fg"], DEFAULTS["brand_footer_fg"]
        ),
        "primary_color": _normalize_hex(
            raw["brand_primary_color"], DEFAULTS["brand_primary_color"]
        ),
        "accent_color": _normalize_hex(
            raw["brand_accent_color"], DEFAULTS["brand_accent_color"]
        ),
        "show_logo_in_header": raw["brand_show_logo_in_header"].strip() in (
            "1",
            "true",
            "on",
            "yes",
        ),
        "show_name_in_header": raw["brand_show_name_in_header"].strip() in (
            "1",
            "true",
            "on",
            "yes",
        ),
        "pos_label": _normalize_text(
            raw["brand_pos_label"], DEFAULTS["brand_pos_label"], 80
        ),
        "header_tagline": _normalize_text(
            raw["brand_header_tagline"], DEFAULTS["brand_header_tagline"], 120
        ),
        "footer_tagline": _normalize_text(
            raw["brand_footer_tagline"], DEFAULTS["brand_footer_tagline"], 400
        ),
        "receipt_title": _normalize_text(
            raw["brand_receipt_title"], DEFAULTS["brand_receipt_title"], 120
        ),
        "receipt_footer_text": _normalize_text(
            raw["brand_receipt_footer_text"],
            DEFAULTS["brand_receipt_footer_text"],
            240,
        ),
        "identity": "restaurant",
        "reports_identity": _reports_identity_pref(raw["brand_reports_identity"]),
        "hotel_name": _normalize_text(
            raw["brand_hotel_name"], DEFAULTS["brand_hotel_name"], 80
        ),
        "hotel_logo_filename": raw["brand_hotel_logo_filename"].strip(),
        "hotel_logo_url": _logo_url(raw["brand_hotel_logo_filename"]),
        "hotel_print_logo_filename": raw["brand_hotel_print_logo_filename"].strip(),
        "hotel_print_logo_url": _logo_url(raw["brand_hotel_print_logo_filename"]),
        "hotel_header_tagline": _normalize_text(
            raw["brand_hotel_header_tagline"],
            DEFAULTS["brand_hotel_header_tagline"],
            120,
        ),
    }


def _reports_identity_pref(raw: str | None) -> str:
    val = (raw or DEFAULTS["brand_reports_identity"]).strip().lower()
    return "hotel" if val == "hotel" else "restaurant"


def get_hotel_branding(db: Session) -> dict:
    """هوية الفندق فقط (اسم + شعارات)."""
    from modules.settings.service import get_settings_bulk

    keys = (
        "brand_hotel_name",
        "brand_hotel_logo_filename",
        "brand_hotel_print_logo_filename",
        "brand_hotel_header_tagline",
    )
    defaults = {k: DEFAULTS[k] for k in keys}
    raw = get_settings_bulk(db, defaults)
    name = _normalize_text(
        raw["brand_hotel_name"], DEFAULTS["brand_hotel_name"], 80
    )
    logo_url = _logo_url(raw["brand_hotel_logo_filename"])
    print_logo_url = _logo_url(raw["brand_hotel_print_logo_filename"])
    header_tagline = _normalize_text(
        raw["brand_hotel_header_tagline"],
        DEFAULTS["brand_hotel_header_tagline"],
        120,
    )
    return {
        "name": name,
        "hotel_name": name,
        "logo_filename": raw["brand_hotel_logo_filename"].strip(),
        "logo_url": logo_url,
        "hotel_logo_url": logo_url,
        "print_logo_filename": raw["brand_hotel_print_logo_filename"].strip(),
        "print_logo_url": print_logo_url,
        "hotel_print_logo_url": print_logo_url,
        "header_tagline": header_tagline,
        "hotel_header_tagline": header_tagline,
    }


def hotel_display_name(db: Session) -> str:
    """اسم الفندق للواجهة والرسائل — منفصل عن اسم المطعم."""
    return get_hotel_branding(db)["name"]


def get_hotel_store_branding(db: Session) -> dict:
    """هوية متجر الشقق /suites — اسم وشعار الفندق."""
    hotel = get_hotel_branding(db)
    main = get_branding(db)
    return {
        "store_name": hotel["name"],
        "header_tagline": hotel["header_tagline"],
        "logo_url": hotel["logo_url"],
        "header_bg_from": main["header_bg_from"],
        "header_bg_to": main["header_bg_to"],
        "primary_color": main["primary_color"],
        "accent_color": main["accent_color"],
    }


def _identity_key_for_domain(db: Session, domain) -> str:
    """restaurant | hotel حسب مجال العمل أو تفضيل تقارير الوضع العام."""
    from modules.platform.business_domain import BusinessDomain

    if domain == BusinessDomain.HOTEL:
        return "hotel"
    if domain == BusinessDomain.RESTAURANT:
        return "restaurant"
    return _reports_identity_pref(get_setting(db, "brand_reports_identity", "restaurant"))


def resolve_active_branding(
    db: Session,
    *,
    domain=None,
    user=None,
    session: dict | None = None,
) -> dict:
    """الهوية الظاهرة في الواجهة حسب وضع العرض (مطعم / فندق / تقارير عامة)."""
    from modules.platform.business_domain import resolve_finance_domain

    if domain is None and (user is not None or session is not None):
        domain = resolve_finance_domain(user, session)
    base = get_branding(db)
    identity = _identity_key_for_domain(db, domain)
    if identity != "hotel":
        out = dict(base)
        out["identity"] = "restaurant"
        return out

    hotel = get_hotel_branding(db)
    out = dict(base)
    out["identity"] = "hotel"
    out["pos_label"] = hotel["name"] or out["pos_label"]
    out["header_tagline"] = hotel["header_tagline"] or out["header_tagline"]
    if hotel["logo_url"]:
        out["logo_filename"] = hotel["logo_filename"]
        out["logo_url"] = hotel["logo_url"]
    print_url = hotel["print_logo_url"] or hotel["logo_url"]
    if print_url:
        out["print_logo_filename"] = (
            hotel["print_logo_filename"] or hotel["logo_filename"]
        )
        out["print_logo_url"] = print_url
    return out


def save_hotel_branding(
    db: Session,
    *,
    hotel_name: str | None = None,
    hotel_header_tagline: str | None = None,
    reports_identity: str | None = None,
) -> None:
    if hotel_name is not None:
        set_setting(
            db,
            "brand_hotel_name",
            _normalize_text(hotel_name, DEFAULTS["brand_hotel_name"], 80),
        )
    if hotel_header_tagline is not None:
        set_setting(
            db,
            "brand_hotel_header_tagline",
            _normalize_text(
                hotel_header_tagline,
                DEFAULTS["brand_hotel_header_tagline"],
                120,
            ),
        )
    if reports_identity is not None:
        set_setting(
            db,
            "brand_reports_identity",
            _reports_identity_pref(reports_identity),
        )


def get_shop_branding(db: Session) -> dict:
    """إعدادات مظهر صفحة المتجر /shop — مع fallback لألوان الهوية العامة."""
    from modules.branding.shop_banners import get_shop_banners_public
    from modules.settings.service import get_settings_bulk

    main = get_branding(db)
    shop_keys = (
        "brand_shop_use_brand_colors",
        "brand_shop_header_bg_from",
        "brand_shop_header_bg_to",
        "brand_shop_header_fg",
        "brand_shop_subtitle",
        "brand_shop_icon_filename",
        "brand_shop_cart_emoji",
        "brand_shop_social_whatsapp",
        "brand_shop_social_instagram",
        "brand_shop_social_facebook",
        "brand_shop_nav_menu_label",
        "brand_shop_nav_menu_url",
        "brand_shop_nav_offers_label",
        "brand_shop_nav_offers_url",
        "brand_shop_nav_about_label",
        "brand_shop_nav_about_url",
        "brand_shop_login_label",
        "brand_shop_login_url",
        "brand_shop_page_offers_title",
        "brand_shop_page_offers_body",
        "brand_shop_page_about_title",
        "brand_shop_page_about_body",
    )
    defaults = {k: DEFAULTS.get(k, "") for k in shop_keys}
    raw = get_settings_bulk(db, defaults)

    use_main = raw["brand_shop_use_brand_colors"].strip() in ("1", "true", "on", "yes")
    bg_from = (raw["brand_shop_header_bg_from"] or "").strip()
    bg_to = (raw["brand_shop_header_bg_to"] or "").strip()
    if use_main or not bg_from:
        bg_from = main["header_bg_from"]
    else:
        bg_from = _normalize_hex(bg_from, main["header_bg_from"])
    if use_main or not bg_to:
        bg_to = main["header_bg_to"]
    else:
        bg_to = _normalize_hex(bg_to, main["header_bg_to"])

    icon_fn = raw["brand_shop_icon_filename"].strip()
    cart_emoji = (raw["brand_shop_cart_emoji"] or DEFAULTS["brand_shop_cart_emoji"]).strip()
    if not cart_emoji:
        cart_emoji = DEFAULTS["brand_shop_cart_emoji"]

    icon_url = _logo_url(icon_fn)
    if not icon_url and main.get("logo_url"):
        icon_url = main["logo_url"]

    banners_cfg = get_shop_banners_public(db)

    return {
        "use_brand_colors": use_main,
        "store_name": shop_store_name(db),
        "header_bg_from": bg_from,
        "header_bg_to": bg_to,
        "primary_color": main["primary_color"],
        "accent_color": main["accent_color"],
        "header_fg": _normalize_hex(
            raw["brand_shop_header_fg"], DEFAULTS["brand_shop_header_fg"]
        ),
        "subtitle": _normalize_text(
            raw["brand_shop_subtitle"], DEFAULTS["brand_shop_subtitle"], 120
        ),
        "icon_filename": icon_fn,
        "icon_url": icon_url,
        "cart_emoji": cart_emoji[:8],
        "social_whatsapp": _normalize_whatsapp_url(raw["brand_shop_social_whatsapp"]),
        "social_instagram": _normalize_url(raw["brand_shop_social_instagram"]),
        "social_facebook": _normalize_url(raw["brand_shop_social_facebook"]),
        "nav_menu_label": _normalize_text(
            raw["brand_shop_nav_menu_label"],
            DEFAULTS["brand_shop_nav_menu_label"],
            40,
        ),
        "nav_menu_url": _normalize_url(
            raw["brand_shop_nav_menu_url"] or DEFAULTS["brand_shop_nav_menu_url"]
        ),
        "nav_offers_label": _normalize_text(
            raw["brand_shop_nav_offers_label"],
            DEFAULTS["brand_shop_nav_offers_label"],
            40,
        ),
        "nav_offers_url": _normalize_url(raw["brand_shop_nav_offers_url"]),
        "nav_about_label": _normalize_text(
            raw["brand_shop_nav_about_label"],
            DEFAULTS["brand_shop_nav_about_label"],
            40,
        ),
        "nav_about_url": _normalize_url(raw["brand_shop_nav_about_url"]),
        "login_label": _normalize_text(
            raw["brand_shop_login_label"],
            DEFAULTS["brand_shop_login_label"],
            40,
        ),
        "login_url": _normalize_url(raw["brand_shop_login_url"]),
        "page_offers_title": _normalize_text(
            raw["brand_shop_page_offers_title"],
            DEFAULTS["brand_shop_page_offers_title"],
            80,
        ),
        "page_offers_body": _multiline_text(
            raw["brand_shop_page_offers_body"],
            DEFAULTS["brand_shop_page_offers_body"],
        ),
        "page_about_title": _normalize_text(
            raw["brand_shop_page_about_title"],
            DEFAULTS["brand_shop_page_about_title"],
            80,
        ),
        "page_about_body": _multiline_text(
            raw["brand_shop_page_about_body"],
            DEFAULTS["brand_shop_page_about_body"],
        ),
        "banners": banners_cfg["items"],
        "banner_interval": banners_cfg["interval_seconds"],
    }


def get_shop_info_page(db: Session, page: str) -> dict:
    """محتوى صفحة معلومات عامة (/offers أو /about)."""
    shop = get_shop_branding(db)
    if page == "offers":
        url = shop.get("nav_offers_url") or ""
        return {
            "slug": "offers",
            "active_nav": "offers",
            "path": _DEFAULT_OFFERS_PATH,
            "available": bool(url),
            "page_title": shop["page_offers_title"],
            "page_body": shop["page_offers_body"],
            "store_name": shop["store_name"],
            "shop_brand": shop,
        }
    if page == "about":
        url = shop.get("nav_about_url") or ""
        return {
            "slug": "about",
            "active_nav": "about",
            "path": _DEFAULT_ABOUT_PATH,
            "available": bool(url),
            "page_title": shop["page_about_title"],
            "page_body": shop["page_about_body"],
            "store_name": shop["store_name"],
            "shop_brand": shop,
        }
    raise ValueError(f"unknown shop info page: {page}")


def save_branding(
    db: Session,
    *,
    header_bg_from: str | None = None,
    header_bg_to: str | None = None,
    header_fg: str | None = None,
    footer_bg: str | None = None,
    footer_fg: str | None = None,
    primary_color: str | None = None,
    accent_color: str | None = None,
    show_logo_in_header: bool | None = None,
    show_name_in_header: bool | None = None,
    pos_label: str | None = None,
    header_tagline: str | None = None,
    footer_tagline: str | None = None,
    receipt_title: str | None = None,
    receipt_footer_text: str | None = None,
) -> None:
    """يحفظ ألوان الهويّة (مع تطبيع آمن لكل قيمة)."""

    def _save(key: str, raw: str | None, default: str) -> None:
        if raw is None:
            return
        set_setting(db, key, _normalize_hex(raw, default))

    _save("brand_header_bg_from", header_bg_from, DEFAULTS["brand_header_bg_from"])
    _save("brand_header_bg_to", header_bg_to, DEFAULTS["brand_header_bg_to"])
    _save("brand_header_fg", header_fg, DEFAULTS["brand_header_fg"])
    _save("brand_footer_bg", footer_bg, DEFAULTS["brand_footer_bg"])
    _save("brand_footer_fg", footer_fg, DEFAULTS["brand_footer_fg"])
    _save("brand_primary_color", primary_color, DEFAULTS["brand_primary_color"])
    _save("brand_accent_color", accent_color, DEFAULTS["brand_accent_color"])

    if show_logo_in_header is not None:
        set_setting(db, "brand_show_logo_in_header", "1" if show_logo_in_header else "0")
    if show_name_in_header is not None:
        set_setting(db, "brand_show_name_in_header", "1" if show_name_in_header else "0")
    if pos_label is not None:
        normalized_name = _normalize_text(pos_label, DEFAULTS["brand_pos_label"], 80)
        set_setting(
            db,
            "brand_pos_label",
            normalized_name,
        )
        # وحّد اسم العرض العام حتى لا يضطر الأدمن لتغييره من مكانين.
        set_setting(db, "store_name", normalized_name)
    if header_tagline is not None:
        set_setting(
            db,
            "brand_header_tagline",
            _normalize_text(
                header_tagline,
                DEFAULTS["brand_header_tagline"],
                120,
            ),
        )
    if footer_tagline is not None:
        set_setting(
            db,
            "brand_footer_tagline",
            _normalize_text(
                footer_tagline,
                DEFAULTS["brand_footer_tagline"],
                400,
            ),
        )
    if receipt_title is not None:
        set_setting(
            db,
            "brand_receipt_title",
            _normalize_text(receipt_title, DEFAULTS["brand_receipt_title"], 120),
        )
    if receipt_footer_text is not None:
        set_setting(
            db,
            "brand_receipt_footer_text",
            _normalize_text(
                receipt_footer_text,
                DEFAULTS["brand_receipt_footer_text"],
                240,
            ),
        )


def save_shop_branding(
    db: Session,
    *,
    use_brand_colors: bool | None = None,
    header_bg_from: str | None = None,
    header_bg_to: str | None = None,
    header_fg: str | None = None,
    subtitle: str | None = None,
    store_name: str | None = None,
    cart_emoji: str | None = None,
    social_whatsapp: str | None = None,
    social_instagram: str | None = None,
    social_facebook: str | None = None,
    nav_menu_label: str | None = None,
    nav_menu_url: str | None = None,
    nav_offers_label: str | None = None,
    nav_offers_url: str | None = None,
    nav_offers_visible: bool | None = None,
    nav_about_label: str | None = None,
    nav_about_url: str | None = None,
    nav_about_visible: bool | None = None,
    login_label: str | None = None,
    login_url: str | None = None,
    page_offers_title: str | None = None,
    page_offers_body: str | None = None,
    page_about_title: str | None = None,
    page_about_body: str | None = None,
) -> None:
    """يحفظ إعدادات مظهر صفحة المتجر."""
    main = get_branding(db)
    if use_brand_colors is not None:
        set_setting(db, "brand_shop_use_brand_colors", "1" if use_brand_colors else "0")
    if header_bg_from is not None:
        s = (header_bg_from or "").strip()
        set_setting(
            db,
            "brand_shop_header_bg_from",
            _normalize_hex(s, main["primary_color"]) if s else "",
        )
    if header_bg_to is not None:
        s = (header_bg_to or "").strip()
        set_setting(
            db,
            "brand_shop_header_bg_to",
            _normalize_hex(s, main["accent_color"]) if s else "",
        )
    if header_fg is not None:
        set_setting(
            db,
            "brand_shop_header_fg",
            _normalize_hex(header_fg, DEFAULTS["brand_shop_header_fg"]),
        )
    if subtitle is not None:
        set_setting(
            db,
            "brand_shop_subtitle",
            _normalize_text(subtitle, DEFAULTS["brand_shop_subtitle"], 120),
        )
    if store_name is not None:
        set_setting(
            db,
            "brand_shop_store_name",
            _normalize_text(store_name, "", 120),
        )
    if cart_emoji is not None:
        emoji = (cart_emoji or "").strip()[:8] or DEFAULTS["brand_shop_cart_emoji"]
        set_setting(db, "brand_shop_cart_emoji", emoji)
    if social_whatsapp is not None:
        set_setting(db, "brand_shop_social_whatsapp", _normalize_whatsapp_url(social_whatsapp))
    if social_instagram is not None:
        set_setting(db, "brand_shop_social_instagram", _normalize_url(social_instagram))
    if social_facebook is not None:
        set_setting(db, "brand_shop_social_facebook", _normalize_url(social_facebook))
    if nav_menu_label is not None:
        set_setting(
            db,
            "brand_shop_nav_menu_label",
            _normalize_text(nav_menu_label, DEFAULTS["brand_shop_nav_menu_label"], 40),
        )
    if nav_menu_url is not None:
        set_setting(
            db,
            "brand_shop_nav_menu_url",
            _normalize_url(nav_menu_url or DEFAULTS["brand_shop_nav_menu_url"]),
        )
    if nav_offers_label is not None:
        set_setting(
            db,
            "brand_shop_nav_offers_label",
            _normalize_text(nav_offers_label, DEFAULTS["brand_shop_nav_offers_label"], 40),
        )
    if nav_offers_visible is not None:
        set_setting(
            db,
            "brand_shop_nav_offers_url",
            _nav_page_url(
                nav_offers_url,
                visible=nav_offers_visible,
                default_path=_DEFAULT_OFFERS_PATH,
            ),
        )
    elif nav_offers_url is not None:
        set_setting(db, "brand_shop_nav_offers_url", _normalize_url(nav_offers_url))
    if nav_about_label is not None:
        set_setting(
            db,
            "brand_shop_nav_about_label",
            _normalize_text(nav_about_label, DEFAULTS["brand_shop_nav_about_label"], 40),
        )
    if nav_about_visible is not None:
        set_setting(
            db,
            "brand_shop_nav_about_url",
            _nav_page_url(
                nav_about_url,
                visible=nav_about_visible,
                default_path=_DEFAULT_ABOUT_PATH,
            ),
        )
    elif nav_about_url is not None:
        set_setting(db, "brand_shop_nav_about_url", _normalize_url(nav_about_url))
    if page_offers_title is not None:
        set_setting(
            db,
            "brand_shop_page_offers_title",
            _normalize_text(page_offers_title, DEFAULTS["brand_shop_page_offers_title"], 80),
        )
    if page_offers_body is not None:
        set_setting(
            db,
            "brand_shop_page_offers_body",
            _multiline_text(page_offers_body, DEFAULTS["brand_shop_page_offers_body"]),
        )
    if page_about_title is not None:
        set_setting(
            db,
            "brand_shop_page_about_title",
            _normalize_text(page_about_title, DEFAULTS["brand_shop_page_about_title"], 80),
        )
    if page_about_body is not None:
        set_setting(
            db,
            "brand_shop_page_about_body",
            _multiline_text(page_about_body, DEFAULTS["brand_shop_page_about_body"]),
        )
    if login_label is not None:
        set_setting(
            db,
            "brand_shop_login_label",
            _normalize_text(login_label, DEFAULTS["brand_shop_login_label"], 40),
        )
    if login_url is not None:
        set_setting(db, "brand_shop_login_url", _normalize_url(login_url))


# ----- Logo -----

class LogoError(Exception):
    """خطأ في رفع/معالجة الشعار."""


def save_logo(
    db: Session,
    *,
    filename: str,
    content: bytes,
    setting_key: str = "brand_logo_filename",
) -> str:
    """يحفظ ملف الشعار في القرص ويسجّل اسمه في الإعدادات.

    يُرجع اسم الملف النهائي. يرفع `LogoError` لأي مشكلة (امتداد غير مسموح،
    حجم أكبر من اللازم، اسم فارغ).
    """
    if not filename:
        raise LogoError("لم يُحدَّد اسم ملف.")
    if not content:
        raise LogoError("الملف فارغ.")
    if len(content) > MAX_LOGO_BYTES:
        raise LogoError(
            f"حجم الملف أكبر من {MAX_LOGO_BYTES // 1024}KB."
        )
    ext = Path(filename).suffix.lower().lstrip(".")
    if ext not in ALLOWED_LOGO_EXT:
        raise LogoError(
            f"الامتداد .{ext} غير مدعوم. المدعومة: {', '.join(sorted(ALLOWED_LOGO_EXT))}"
        )

    # احذف الشعار القديم إن وجد
    delete_logo(db, setting_key=setting_key, _commit=False)

    new_name = f"logo-{uuid.uuid4().hex[:12]}.{ext}"
    path = _logo_dir() / new_name
    path.write_bytes(content)

    set_setting(db, setting_key, new_name)
    return new_name


def delete_logo(
    db: Session,
    *,
    setting_key: str = "brand_logo_filename",
    _commit: bool = True,
) -> bool:
    """يحذف الشعار الحالي من القرص ويُفرّغ الإعداد. يعيد True إن حُذف فعلاً."""
    current = (get_setting(db, setting_key, "") or "").strip()
    removed = False
    if current:
        path = _logo_dir() / current
        try:
            if path.exists():
                path.unlink()
                removed = True
        except OSError:
            pass
        set_setting(db, setting_key, "")
    if _commit:
        db.commit()
    return removed


def reset_to_defaults(db: Session) -> None:
    """يُعيد كل ألوان الهويّة لقيمها الافتراضية ويحذف الشعار."""
    delete_logo(db, setting_key="brand_logo_filename", _commit=False)
    delete_logo(db, setting_key="brand_print_logo_filename", _commit=False)
    delete_logo(db, setting_key="brand_hotel_logo_filename", _commit=False)
    delete_logo(db, setting_key="brand_hotel_print_logo_filename", _commit=False)
    delete_logo(db, setting_key="brand_shop_icon_filename", _commit=False)
    for k, v in DEFAULTS.items():
        if k in (
            "brand_logo_filename",
            "brand_print_logo_filename",
            "brand_hotel_logo_filename",
            "brand_hotel_print_logo_filename",
            "brand_shop_icon_filename",
        ):
            continue
        set_setting(db, k, v)
    db.commit()
