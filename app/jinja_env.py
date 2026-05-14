from pathlib import Path
from typing import Any
from decimal import Decimal, InvalidOperation

from fastapi import Request
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))


# =====================================================================
# Globals متاحة في كل القوالب (الهيدر والفوتر يحتاجانها)
# =====================================================================
# ملاحظة: نُحمّل هذه القيم من request.state إن وُجدت (يوفّرها الـ middleware
# في app/main.py)، وإلا نعيد قيماً افتراضية آمنة.
#
# نستخدم بادئة `pos_` لتجنّب التعارض مع متغيّرات قد يُمرّرها أيّ راوتر
# في الـ context (مثل `store_name` التي تُمرَّر في صفحة الإعدادات).

APP_VERSION = "1.1.0"


def _pos_user(request: Request) -> Any:
    """يعيد المستخدم الحالي إن كان مسجَّلاً، وإلا None."""
    return getattr(request.state, "current_user", None)


def _pos_perm(request: Request):
    """يعيد دالة `perm(code)` تختبر صلاحية المستخدم الحالي."""
    user = _pos_user(request)
    if user is None:
        return lambda _code: False
    from modules.authz.service import user_has_permission

    perm_set = {p for r in user.roles for p in r.permissions}
    perm_codes = {p.code for p in perm_set}

    def _has(code: str) -> bool:
        if code in perm_codes:
            return True
        return user_has_permission(user, code)

    return _has


def _pos_store_name(request: Request) -> str:
    return getattr(request.state, "store_name", "نقطة البيع")


def _pos_app_version(_request: Request) -> str:
    return APP_VERSION


# الافتراضيات تُستخدم لو الـ middleware لم يضع شيئاً في request.state
_DEFAULT_BRAND = {
    "logo_filename": "",
    "logo_url": "",
    "print_logo_filename": "",
    "print_logo_url": "",
    "header_bg_from": "#0f172a",
    "header_bg_to": "#1e293b",
    "header_fg": "#ffffff",
    "footer_bg": "#0f172a",
    "footer_fg": "#94a3b8",
    "primary_color": "#3b82f6",
    "accent_color": "#8b5cf6",
    "show_logo_in_header": True,
    "show_name_in_header": True,
    "pos_label": "نقطة البيع",
    "header_tagline": "نظام نقطة البيع",
    "footer_tagline": (
        "نظام نقطة بيع متكامل يعمل أوفلاين بالكامل مع إدارة للمخزون "
        "والمشتريات والموظفين والرواتب والتقارير المحاسبية."
    ),
    "receipt_title": "فاتورة بيع",
    "receipt_footer_text": "شكراً لتعاملكم معنا",
}


def _pos_brand(request: Request) -> dict:
    """يعيد إعدادات الهويّة (تُحقن من middleware في request.state.brand)."""
    return getattr(request.state, "brand", None) or _DEFAULT_BRAND


def _pos_display_name(request: Request) -> str:
    """اسم العرض النهائي: يفضّل اسم الهوية، ثم اسم المتجر العام."""
    brand = _pos_brand(request)
    label = str(brand.get("pos_label", "") or "").strip()
    if label and label != "نقطة البيع":
        return label
    return _pos_store_name(request)


def _to_decimal(value: Any) -> Decimal:
    try:
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _pos_qty(value: Any) -> str:
    """كمية بدون أصفار زائدة: 1.0000 -> 1 ، 1.5000 -> 1.5"""
    dec = _to_decimal(value)
    text = format(dec.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in ("", "-0"):
        return "0"
    return text


def _pos_money(value: Any) -> Markup:
    """مبلغ بثلاث خانات عشرية، والجزء العشري أصغر بصرياً."""
    dec = _to_decimal(value).quantize(Decimal("0.001"))
    sign = "-" if dec < 0 else ""
    raw = format(abs(dec), "f")
    whole, frac = raw.split(".")
    return Markup(
        f'{escape(sign + whole)}<span class="num-frac">.{escape(frac)}</span>'
    )


# نُتيح هذه الدوال في كل القوالب — البادئة `pos_` لتجنّب التعارض
templates.env.globals["pos_user"] = _pos_user
templates.env.globals["pos_perm"] = _pos_perm
templates.env.globals["pos_store_name"] = _pos_store_name
templates.env.globals["pos_display_name"] = _pos_display_name
templates.env.globals["pos_app_version"] = _pos_app_version
templates.env.globals["pos_brand"] = _pos_brand
templates.env.globals["pos_qty"] = _pos_qty
templates.env.globals["pos_money"] = _pos_money
