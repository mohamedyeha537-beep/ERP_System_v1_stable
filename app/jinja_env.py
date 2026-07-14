from pathlib import Path
from typing import Any
from app.datetime_local import format_local_dt
from app.number_format import (
    format_money_markup as _format_money_markup,
    format_money_plain as _format_money_plain,
    format_qty_markup as _format_qty_markup,
    format_qty_plain as _format_qty_plain,
)
from fastapi import Request
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from markupsafe import Markup

APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
templates.env.auto_reload = True


def _pos_is_main_treasury_wallet(pm: Any) -> bool:
    """الخزينة الرئيسية — كاش أو مصرف (فواتير الشراء والمصروفات)."""
    if pm is None:
        return False
    from modules.payments.models import (
        MAIN_TREASURY_BANK_PM_NAME,
        MAIN_TREASURY_CASH_PM_NAME,
    )

    name = str(getattr(pm, "name_ar", "") or "")
    return name in (MAIN_TREASURY_CASH_PM_NAME, MAIN_TREASURY_BANK_PM_NAME)


def _pos_is_supplier_credit_wallet(pm: Any) -> bool:
    if pm is None:
        return False
    from modules.payments.models import SUPPLIER_CREDIT_PM_NAME

    return str(getattr(pm, "name_ar", "") or "") == SUPPLIER_CREDIT_PM_NAME


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


def _pos_is_kiosk(request: Request) -> bool:
    user = _pos_user(request)
    if user is None:
        return False
    from modules.authz.kiosk import is_cashier_kiosk_user

    return is_cashier_kiosk_user(user)


def _pos_display_name(request: Request) -> str:
    """اسم العرض النهائي: يفضّل اسم الهوية، ثم اسم المتجر العام."""
    brand = _pos_brand(request)
    label = str(brand.get("pos_label", "") or "").strip()
    if label and label != "نقطة البيع":
        return label
    return _pos_store_name(request)


def _pos_qty(value: Any) -> Markup:
    """كمية: بدون علامة عشرية إن لم تُحتج — والكسور بحجم أصغر."""
    return _format_qty_markup(value)


def _pos_qty_plain(value: Any) -> str:
    return _format_qty_plain(value)


def _pos_money(value: Any) -> Markup:
    """مبلغ بثلاث خانات عشرية، والجزء العشري أصغر بصرياً."""
    return _format_money_markup(value)


def _pos_money_plain(value: Any) -> str:
    """نص مبلغ للسمات ورسائل التأكيد (بدون HTML)."""
    return _format_money_plain(value)


def _pos_payroll_hours(rec: Any) -> str:
    """ساعات محتسبة للعرض — وردية فقط عند رفض الإضافي."""
    from modules.hr.models import AttendanceRecord

    if not isinstance(rec, AttendanceRecord) or rec.check_out is None:
        return _format_qty_plain(0)
    return _format_qty_plain(rec.payroll_hours_counted)


def _pos_is_admin(request: Request) -> bool:
    from modules.platform.business_domain import is_system_admin

    return is_system_admin(_pos_user(request))


def _pos_view_mode(request: Request) -> str:
    from modules.platform.business_domain import ViewMode, get_admin_view_mode

    session = request.session if hasattr(request, "session") else None
    return get_admin_view_mode(session).value


def _pos_nav_show_hotel(request: Request) -> bool:
    from modules.platform.business_domain import nav_show_hotel

    return nav_show_hotel(_pos_user(request), getattr(request, "session", None), _pos_perm(request))


def _pos_nav_show_restaurant(request: Request) -> bool:
    from modules.platform.business_domain import nav_show_restaurant

    return nav_show_restaurant(
        _pos_user(request), getattr(request, "session", None), _pos_perm(request)
    )


def _pos_reports_show_pos_sections(request: Request) -> bool:
    from modules.platform.business_domain import reports_show_pos_sections

    return reports_show_pos_sections(
        _pos_user(request), getattr(request, "session", None)
    )


def _pos_reports_show_hotel_sections(request: Request) -> bool:
    from modules.platform.business_domain import reports_show_hotel_sections

    return reports_show_hotel_sections(
        _pos_user(request), getattr(request, "session", None)
    )


def _pos_domain_label(value: str) -> str:
    from modules.platform.business_domain import domain_label

    return domain_label(value)


def _pos_view_scope_options() -> list[tuple[str, str]]:
    from modules.platform.business_domain import user_view_scope_choices

    return user_view_scope_choices()


def _pos_view_scope_label(value: str | None) -> str:
    from modules.platform.business_domain import view_scope_label

    return view_scope_label(value)


def _pos_ui_show(request: Request, block_id: str) -> bool:
    from modules.authz.ui_blocks import user_shows_ui_block

    return user_shows_ui_block(_pos_user(request), block_id)


def _pos_user_ui_hidden(user) -> set[str]:
    from modules.authz.ui_blocks import user_ui_hidden_set

    return user_ui_hidden_set(user)


def _pos_page_trail(request: Request):
    from app.page_trail import build_page_trail

    path = request.url.path
    hide = getattr(request.state, "page_trail_hide", False)
    back_url = getattr(request.state, "page_trail_back_url", None)
    back_label = getattr(request.state, "page_trail_back_label", None)
    return build_page_trail(
        path,
        hide=hide,
        back_url=back_url,
        back_label=back_label,
    )


def _pos_format_dt(value: Any, fmt: str = "%Y-%m-%d %H:%M") -> str:
    return format_local_dt(value, fmt)


def _pos_activity_unread(request: Request) -> int:
    """عدد الجرس — لا يُحسب هنا حتى لا يبطئ كل صفحة؛ يُحدَّث عبر JS."""
    cached = getattr(request.state, "activity_unread", None)
    if cached is not None:
        return int(cached)
    return 0


# نُتيح هذه الدوال في every القوالب — البادئة `pos_` لتجنّب التعارض
templates.env.globals["pos_page_trail"] = _pos_page_trail
templates.env.globals["pos_is_admin"] = _pos_is_admin
templates.env.globals["pos_activity_unread"] = _pos_activity_unread
templates.env.globals["pos_view_mode"] = _pos_view_mode
templates.env.globals["pos_nav_show_hotel"] = _pos_nav_show_hotel
templates.env.globals["pos_nav_show_restaurant"] = _pos_nav_show_restaurant
templates.env.globals["pos_reports_show_pos_sections"] = _pos_reports_show_pos_sections
templates.env.globals["pos_reports_show_hotel_sections"] = _pos_reports_show_hotel_sections
templates.env.globals["pos_domain_label"] = _pos_domain_label
templates.env.globals["pos_view_scope_options"] = _pos_view_scope_options
templates.env.globals["pos_view_scope_label"] = _pos_view_scope_label
templates.env.globals["pos_ui_show"] = _pos_ui_show
templates.env.globals["pos_user_ui_hidden"] = _pos_user_ui_hidden

from modules.authz.ui_blocks import UI_BLOCK_GROUPS as _UI_BLOCK_GROUPS
from modules.authz.ui_blocks import ui_blocks_by_group as _ui_blocks_by_group_fn

templates.env.globals["ui_block_groups"] = _UI_BLOCK_GROUPS
templates.env.globals["ui_blocks_by_group"] = _ui_blocks_by_group_fn()
templates.env.globals["pos_user"] = _pos_user
templates.env.globals["pos_is_kiosk"] = _pos_is_kiosk
templates.env.globals["pos_perm"] = _pos_perm
templates.env.globals["pos_store_name"] = _pos_store_name
templates.env.globals["pos_display_name"] = _pos_display_name
templates.env.globals["pos_app_version"] = _pos_app_version
templates.env.globals["pos_brand"] = _pos_brand
templates.env.globals["pos_qty"] = _pos_qty
templates.env.globals["pos_qty_plain"] = _pos_qty_plain
templates.env.globals["pos_money"] = _pos_money
templates.env.globals["pos_money_plain"] = _pos_money_plain
templates.env.globals["pos_payroll_hours"] = _pos_payroll_hours
templates.env.globals["pos_format_dt"] = _pos_format_dt
templates.env.globals["pos_is_main_treasury_wallet"] = _pos_is_main_treasury_wallet
templates.env.globals["pos_is_supplier_credit_wallet"] = _pos_is_supplier_credit_wallet
templates.env.filters["qty"] = _pos_qty
templates.env.filters["qty_plain"] = _pos_qty_plain
templates.env.filters["localtime"] = _pos_format_dt


@pass_context
def _product_modifiers(ctx, product) -> list[str]:
    from modules.sales.line_notes import get_product_modifier_presets

    pool = ctx.get("modifier_pool")
    return get_product_modifier_presets(product, pool=pool)


def _json_attr(value) -> str:
    """JSON آمن داخل خاصية HTML (data-*)."""
    import html
    import json

    from markupsafe import Markup

    if value is None:
        value = []
    try:
        payload = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        payload = "[]"
    return Markup(html.escape(payload, quote=True))


def _enum_val(value: Any) -> str:
    """قيمة enum أو نص خام (للتوافق مع SQLite)."""
    import enum as enum_mod

    if value is None:
        return ""
    if isinstance(value, enum_mod.Enum):
        return str(value.value)
    if hasattr(value, "value") and not isinstance(value, (str, bytes)):
        return str(value.value)
    return str(value)


def _driver_btn_label(full_name: Any) -> str:
    from modules.delivery.drivers_service import driver_button_label

    return driver_button_label(str(full_name) if full_name else None)


def _product_image_url(image_filename: str | None) -> str | None:
    from modules.catalog.uploads import product_image_public_url

    return product_image_public_url(image_filename)


def _guest_id_document_url(filename: str | None) -> str | None:
    from modules.hotel.uploads import guest_id_document_public_url

    return guest_id_document_public_url(filename)


templates.env.globals["product_modifiers"] = _product_modifiers
templates.env.globals["product_image_url"] = _product_image_url
templates.env.globals["guest_id_document_url"] = _guest_id_document_url
templates.env.filters["json_attr"] = _json_attr
templates.env.filters["enum_val"] = _enum_val
templates.env.filters["driver_btn_label"] = _driver_btn_label
