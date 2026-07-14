"""تقييد مسارات المستخدمين حسب مجال العمل (فندق / مطعم)."""
from __future__ import annotations

from modules.platform.business_domain import (
    is_hotel_scope_user,
    is_restaurant_scope_user,
    is_system_admin,
)

_ALWAYS_ALLOWED_PREFIXES = ("/static/", "/uploads/", "/auth/")


def _allowed_prefix(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(path.startswith(p) for p in prefixes)


def hotel_scope_allowed_path(path: str) -> bool:
    if _allowed_prefix(path, _ALWAYS_ALLOWED_PREFIXES):
        return True
    if path == "/" or path.startswith("/admin/hotel/") or path.startswith("/hotel/"):
        return True
    if path.startswith("/admin/customers"):
        return True
    if path.startswith("/admin/consumables") or path.startswith("/admin/consumable-categories"):
        return True
    if path.startswith("/admin/purchases"):
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


def domain_scope_redirect_path(user, path: str) -> str | None:
    """مسار إعادة التوجيه إن كان المستخدم خارج نطاقه، أو None."""
    if user is None or is_system_admin(user):
        return None
    if is_hotel_scope_user(user) and not hotel_scope_allowed_path(path):
        return "/admin/hotel/dashboard"
    if is_restaurant_scope_user(user) and not restaurant_scope_allowed_path(path):
        return "/pos"
    return None
