"""وضع كاشير نقطة البيع: واجهة مقتصرة + رقم سري للجلسة."""
from __future__ import annotations

from modules.authz.models import User

# صلاحيات لا تُعتبر «كاشير نقطة بيع فقط» إن وُجدت
_NON_KIOSK_PERMISSIONS = frozenset(
    {
        "admin:settings",
        "admin:users",
        "catalog:write",
        "hr:manage",
        "hr:view",
        "payments:manage",
        "warehouses:manage",
        "purchases:manage",
        "purchases:invoices",
        "backup:manage",
        "reports:view",
        "inventory:view",
        "inventory:adjust",
        "sales:refund",
        "tables:manage",
        "kds:view",
        "delivery:manage",
        "integration:manage",
        "branding:manage",
        "alerts:manage",
    }
)


def user_permission_codes(user: User) -> set[str]:
    return {p.code for r in user.roles for p in r.permissions}


def requires_pos_pin(user: User | None) -> bool:
    """هل يجب إدخال الرقم السري قبل نقطة البيع؟ (كاشير فقط، وليس الأدمن)."""
    return is_cashier_kiosk_user(user)


def is_cashier_kiosk_user(user: User | None) -> bool:
    """مستخدم كاشير: لديه بيع ولا يملك صلاحيات إدارية أخرى."""
    if user is None:
        return False
    codes = user_permission_codes(user)
    if "sales:create" not in codes:
        return False
    return not bool(codes & _NON_KIOSK_PERMISSIONS)


_HOTEL_SHIFT_ADMIN_PERMISSIONS = frozenset(
    {
        "admin:users",
        "admin:roles",
        "admin:settings",
        "hotel:finance:close_day",
        "reports:view",
        "gl:manage",
    }
)


def requires_hotel_shift_pin(user: User | None) -> bool:
    """هل يجب إدخال الرقم السري قبل وردية الفندق؟ (موظف استقبال فقط)."""
    if user is None:
        return False
    from modules.platform.business_domain import is_hotel_scope_user, is_system_admin

    if is_system_admin(user):
        return False
    if not is_hotel_scope_user(user):
        return False
    codes = user_permission_codes(user)
    return not bool(codes & _HOTEL_SHIFT_ADMIN_PERMISSIONS)


def kiosk_allowed_path(path: str) -> bool:
    if path.startswith("/static"):
        return True
    if path.startswith("/auth"):
        return True
    if path.startswith("/pos/treasury"):
        return False
    if path == "/pos" or path.startswith("/pos/"):
        return True
    return False
