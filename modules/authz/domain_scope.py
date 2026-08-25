"""تقييد مسارات المستخدمين حسب مجال العمل (فندق / مطعم)."""
from __future__ import annotations

import re

from modules.platform.business_domain import (
    _has_hotel_scope,
    _has_restaurant_scope,
    is_hotel_scope_user,
    is_restaurant_scope_user,
    is_system_admin,
)

_ALWAYS_ALLOWED_PREFIXES = ("/static/", "/uploads/", "/auth/")

# أمين الخزينة: قبض/صرف وتقارير واعتماد استلام — بلا تشغيل جلسة كاشير أو استقبال
_TREASURY_ALLOWED_PREFIXES = (
    "/pos/treasury",
    "/reports",
    "/admin/purchases",
    "/admin/consumables",
    "/admin/assets",
    "/admin/expenses",
    "/admin/payroll",
    "/admin/attendance",
    "/admin/work-shifts",
    "/admin/advances",
    "/admin/bonuses",
    "/admin/deductions",
    "/admin/shift-variances",
    "/admin/customers",
    "/admin/employees",
    "/admin/hotel/debts",
    "/admin/hotel/reports",
    "/admin/shifts",
    "/admin/payment-methods",
    "/admin/view-mode",
    "/hotel/settle",
    "/pos/shift/shortages",
    "/pos/receipt/",
)
_TREASURY_HOTEL_SHIFT_REPORT = re.compile(r"^/admin/hotel/shift/\d+/(report|handoff)(/|$)")
_TREASURY_POS_SHIFT_REPORT = re.compile(r"^/pos/shift/\d+/(report|export\.csv)(/|$)")
_TREASURY_GL_LEDGER = re.compile(r"^/admin/gl/accounts/\d+(/|$)")
_TREASURY_BOOKING_DETAIL = re.compile(r"^/admin/hotel/bookings/\d+(/|$)")
_TREASURY_BOOKING_BLOCK = (
    "/check-in",
    "/check-out",
    "/extend",
    "/cancel",
    "/no-show",
    "/adjust-stay",
    "/quotations",
)


def _allowed_prefix(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(path.startswith(p) for p in prefixes)


def treasury_clerk_allowed_path(path: str) -> bool:
    """مسارات أمين الخزينة: استلام واعتماد وعجز + قبض/صرف — دون جلسات البيع."""
    if _allowed_prefix(path, _ALWAYS_ALLOWED_PREFIXES):
        return True
    if path in ("/", ""):
        return True
    if _allowed_prefix(path, _TREASURY_ALLOWED_PREFIXES):
        return True
    if (
        _TREASURY_HOTEL_SHIFT_REPORT.match(path)
        or _TREASURY_POS_SHIFT_REPORT.match(path)
        or _TREASURY_GL_LEDGER.match(path)
    ):
        return True
    if _TREASURY_BOOKING_DETAIL.match(path):
        return not any(seg in path for seg in _TREASURY_BOOKING_BLOCK)
    return False


def hotel_scope_allowed_path(path: str) -> bool:
    if _allowed_prefix(path, _ALWAYS_ALLOWED_PREFIXES):
        return True
    if path == "/" or path.startswith("/admin/hotel/") or path.startswith("/hotel/"):
        return True
    if path.startswith("/admin/customers"):
        return True
    # عرض/طباعة فاتورة مطعم مربوطة بحجز — التحقق من الربط داخل المسار نفسه
    if path.startswith("/pos/receipt/"):
        return True
    if path.startswith("/admin/consumables") or path.startswith("/admin/consumable-categories"):
        return True
    if path.startswith("/admin/purchases"):
        return True
    if path.startswith("/pos/treasury"):
        return True
    if path.startswith("/admin/shift-variances"):
        return True
    if path.startswith("/admin/expenses") or path.startswith("/admin/payroll"):
        return True
    if path.startswith("/reports"):
        return True
    return False


def restaurant_scope_allowed_path(path: str) -> bool:
    if _allowed_prefix(path, _ALWAYS_ALLOWED_PREFIXES):
        return True
    if path == "/" or path == "/pos" or path.startswith("/pos/"):
        return True
    if path.startswith("/admin/tables") or path.startswith("/admin/kds"):
        return True
    if path.startswith("/refunds") or path.startswith("/reports"):
        return True
    if path.startswith("/admin/customers"):
        return True
    if path.startswith("/inventory") or path.startswith("/catalog/"):
        return True
    if path.startswith("/admin/warehouses") or path.startswith("/admin/purchases"):
        return True
    if path.startswith("/sales/"):
        return True
    return False


def lands_on_hotel_dashboard(user) -> bool:
    """هل يجب تجاوز الصفحة الرئيسية العامة إلى لوحة الشقق؟"""
    if user is None or is_system_admin(user):
        return False
    if is_hotel_scope_user(user):
        return True
    return _has_hotel_scope(user) and not _has_restaurant_scope(user)


def default_landing_path(user) -> str:
    """الصفحة الافتراضية بعد الدخول أو زر الرئيسية حسب نطاق العمل."""
    if user is None:
        return "/auth/login"
    from modules.authz.capability import is_treasury_clerk_user

    if is_treasury_clerk_user(user):
        return "/pos/treasury/desk"
    if lands_on_hotel_dashboard(user):
        return "/admin/hotel/dashboard"
    if is_restaurant_scope_user(user):
        return "/pos"
    return "/"


def domain_scope_redirect_path(user, path: str) -> str | None:
    """مسار إعادة التوجيه إن كان المستخدم خارج نطاقه، أو None."""
    if user is None or is_system_admin(user):
        return None
    from modules.authz.capability import is_treasury_clerk_user

    if is_treasury_clerk_user(user):
        if path in ("/", ""):
            return "/pos/treasury/desk"
        if not treasury_clerk_allowed_path(path):
            return "/pos/treasury/desk"
        return None
    if lands_on_hotel_dashboard(user):
        # لا نعرض صفحة الروابط السريعة (/) — لوحة الشقق مباشرة
        if path in ("/", "") or not hotel_scope_allowed_path(path):
            return "/admin/hotel/dashboard"
        return None
    if is_restaurant_scope_user(user) and not restaurant_scope_allowed_path(path):
        return "/pos"
    return None
