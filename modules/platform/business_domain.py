"""مجال العمل — فصل الحسابات المالية والواجهة بين المطعم والفندق."""
from __future__ import annotations

from enum import Enum

from modules.authz.models import User
from modules.authz.permissions import ADMIN_ROLE_NAME_AR

SESSION_VIEW_MODE_KEY = "business_view_mode"


class BusinessDomain(str, Enum):
    SHARED = "shared"
    RESTAURANT = "restaurant"
    HOTEL = "hotel"


class ViewMode(str, Enum):
    GENERAL = "general"
    RESTAURANT = "restaurant"
    HOTEL = "hotel"


class UserViewScope(str, Enum):
    """ما يراه المستخدم في الواجهة — يحدّده الأدمن."""
    RESTAURANT = "restaurant"
    HOTEL = "hotel"
    BOTH = "both"


HOTEL_STAFF_ROLE_NAME_AR = "موظف فندق"
RESTAURANT_STAFF_ROLE_NAME_AR = "موظف مطعم"

_HOTEL_SCOPE_PERMISSIONS = frozenset(
    {
        "hotel:booking:view",
        "hotel:booking:create",
        "hotel:booking:manage",
        "hotel:booking:checkin",
        "hotel:booking:checkout",
        "hotel:settle",
        "hotel:rooms:manage",
        "hotel:housekeeping",
        "hotel:finance:close_day",
    }
)

_RESTAURANT_SCOPE_PERMISSIONS = frozenset(
    {
        "sales:create",
        "tables:manage",
        "kds:view",
        "delivery:manage",
    }
)

_DOMAIN_LABELS: dict[str, str] = {
    BusinessDomain.SHARED.value: "مشترك (الكل)",
    BusinessDomain.RESTAURANT.value: "مطعم",
    BusinessDomain.HOTEL.value: "فندق",
}

_VIEW_MODE_LABELS: dict[str, str] = {
    ViewMode.GENERAL.value: "عام",
    ViewMode.RESTAURANT.value: "مطعم",
    ViewMode.HOTEL.value: "فندق",
}

_VIEW_SCOPE_LABELS: dict[str, str] = {
    UserViewScope.RESTAURANT.value: "مطعم فقط",
    UserViewScope.HOTEL.value: "فندق فقط",
    UserViewScope.BOTH.value: "المطعم والفندق",
}


def domain_label(value: str | BusinessDomain | None) -> str:
    if value is None:
        return "—"
    key = value.value if isinstance(value, BusinessDomain) else str(value)
    return _DOMAIN_LABELS.get(key, key)


def view_mode_label(value: str | ViewMode | None) -> str:
    if value is None:
        return _VIEW_MODE_LABELS[ViewMode.GENERAL.value]
    key = value.value if isinstance(value, ViewMode) else str(value)
    return _VIEW_MODE_LABELS.get(key, key)


def user_role_names(user: User | None) -> set[str]:
    if user is None:
        return set()
    return {(r.name_ar or "").strip() for r in user.roles}


def is_system_admin(user: User | None) -> bool:
    return ADMIN_ROLE_NAME_AR in user_role_names(user)


def user_permission_codes(user: User | None) -> set[str]:
    if user is None:
        return set()
    return {p.code for r in user.roles for p in r.permissions}


def _has_hotel_scope(user: User) -> bool:
    codes = user_permission_codes(user)
    return bool(codes & _HOTEL_SCOPE_PERMISSIONS)


def _has_restaurant_scope(user: User) -> bool:
    codes = user_permission_codes(user)
    return bool(codes & _RESTAURANT_SCOPE_PERMISSIONS)


def view_scope_label(value: str | UserViewScope | None) -> str:
    if value is None:
        return _VIEW_SCOPE_LABELS[UserViewScope.BOTH.value]
    key = value.value if isinstance(value, UserViewScope) else str(value)
    return _VIEW_SCOPE_LABELS.get(key.strip().lower(), key)


def user_view_scope_choices() -> list[tuple[str, str]]:
    return [(s.value, view_scope_label(s)) for s in UserViewScope]


def parse_user_view_scope(raw: str | None) -> UserViewScope:
    try:
        return UserViewScope((raw or UserViewScope.BOTH.value).strip().lower())
    except ValueError:
        return UserViewScope.BOTH


def _infer_view_scope_from_roles(user: User) -> UserViewScope:
    """استنتاج للمستخدمين القدامى قبل إضافة view_scope."""
    if HOTEL_STAFF_ROLE_NAME_AR in user_role_names(user):
        return UserViewScope.HOTEL
    if RESTAURANT_STAFF_ROLE_NAME_AR in user_role_names(user):
        return UserViewScope.RESTAURANT
    has_hotel = _has_hotel_scope(user)
    has_rest = _has_restaurant_scope(user)
    if has_hotel and not has_rest:
        return UserViewScope.HOTEL
    if has_rest and not has_hotel:
        return UserViewScope.RESTAURANT
    return UserViewScope.BOTH


def get_user_view_scope(user: User | None) -> UserViewScope:
    if user is None or is_system_admin(user):
        return UserViewScope.BOTH
    raw = getattr(user, "view_scope", None)
    if raw is None or not str(raw).strip():
        return _infer_view_scope_from_roles(user)
    try:
        return UserViewScope(str(raw).strip().lower())
    except ValueError:
        return _infer_view_scope_from_roles(user)


def is_hotel_scope_user(user: User | None) -> bool:
    """مستخدم فندق فقط — لا يرى واجهة المطعم."""
    if user is None or is_system_admin(user):
        return False
    return get_user_view_scope(user) == UserViewScope.HOTEL


def is_restaurant_scope_user(user: User | None) -> bool:
    """مستخدم مطعم فقط — لا يرى واجهة الفندق."""
    if user is None or is_system_admin(user):
        return False
    return get_user_view_scope(user) == UserViewScope.RESTAURANT


def get_admin_view_mode(session: dict | None) -> ViewMode:
    """الوضع الافتراضي مطعم — الوضع العام للتقارير المجمّعة فقط."""
    if not session:
        return ViewMode.RESTAURANT
    raw = (session.get(SESSION_VIEW_MODE_KEY) or ViewMode.RESTAURANT.value).strip().lower()
    try:
        return ViewMode(raw)
    except ValueError:
        return ViewMode.RESTAURANT


def resolve_finance_domain(
    user: User | None,
    session: dict | None = None,
    *,
    context: BusinessDomain | None = None,
) -> BusinessDomain | None:
    """None = لا تصفية (يرى الكل بما فيها shared)."""
    if context is not None:
        return context
    if user is None:
        return None
    if is_system_admin(user):
        mode = get_admin_view_mode(session)
        if mode == ViewMode.GENERAL:
            return None
        if mode == ViewMode.RESTAURANT:
            return BusinessDomain.RESTAURANT
        return BusinessDomain.HOTEL
    scope = get_user_view_scope(user)
    if scope == UserViewScope.HOTEL:
        return BusinessDomain.HOTEL
    if scope == UserViewScope.RESTAURANT:
        return BusinessDomain.RESTAURANT
    return None


def payment_method_visible_for_domain(
    pm_domain: BusinessDomain | str | None,
    *,
    filter_domain: BusinessDomain | None,
) -> bool:
    if filter_domain is None:
        return True
    if pm_domain is None:
        return True
    if isinstance(pm_domain, BusinessDomain):
        dom = pm_domain
    elif isinstance(pm_domain, str):
        try:
            dom = BusinessDomain(pm_domain.strip().lower())
        except ValueError:
            return True
    elif hasattr(pm_domain, "value"):
        try:
            dom = BusinessDomain(str(pm_domain.value).strip().lower())
        except ValueError:
            return True
    else:
        return True
    return dom in (BusinessDomain.SHARED, filter_domain)


def domain_record_visible(
    record_domain: BusinessDomain | str | None,
    *,
    filter_domain: BusinessDomain | None,
) -> bool:
    """هل السجل (تكلفة شهرية، حساب…) يظهر في مجال العمل الحالي؟"""
    return payment_method_visible_for_domain(record_domain, filter_domain=filter_domain)


def assert_payment_method_for_domain(
    pm,
    *,
    filter_domain: BusinessDomain,
) -> None:
    from modules.payments.service import PaymentsError

    dom = getattr(pm, "business_domain", BusinessDomain.SHARED)
    if isinstance(dom, str):
        try:
            dom = BusinessDomain(dom)
        except ValueError:
            dom = BusinessDomain.SHARED
    if not payment_method_visible_for_domain(dom, filter_domain=filter_domain):
        raise PaymentsError(
            f"حساب «{getattr(pm, 'name_ar', '')}» لا يخص {domain_label(filter_domain)}."
        )


def nav_show_hotel(user: User | None, session: dict | None, perm_fn) -> bool:
    if not perm_fn("hotel:booking:view") and not perm_fn("hotel:rooms:manage") and not perm_fn(
        "hotel:settle"
    ):
        return False
    if is_system_admin(user):
        return get_admin_view_mode(session) in (ViewMode.GENERAL, ViewMode.HOTEL)
    return get_user_view_scope(user) != UserViewScope.RESTAURANT


def nav_show_restaurant(user: User | None, session: dict | None, perm_fn) -> bool:
    if not (
        perm_fn("sales:create")
        or perm_fn("sales:refund")
        or perm_fn("reports:view")
        or perm_fn("tables:manage")
        or perm_fn("kds:view")
    ):
        return False
    if is_system_admin(user):
        return get_admin_view_mode(session) in (ViewMode.GENERAL, ViewMode.RESTAURANT)
    return get_user_view_scope(user) != UserViewScope.HOTEL


_EMPLOYEE_DOMAINS = (BusinessDomain.RESTAURANT, BusinessDomain.HOTEL)


def employee_domain_choices() -> list[tuple[str, str]]:
    return [(d.value, domain_label(d)) for d in _EMPLOYEE_DOMAINS]


def parse_employee_domain(raw: str | None) -> BusinessDomain:
    try:
        dom = BusinessDomain((raw or BusinessDomain.RESTAURANT.value).strip().lower())
    except ValueError:
        return BusinessDomain.RESTAURANT
    if dom not in _EMPLOYEE_DOMAINS:
        return BusinessDomain.RESTAURANT
    return dom


def resolve_record_business_domain(
    filter_domain: BusinessDomain | None,
    explicit: str | None,
    *,
    inherit_from=None,
    allow_shared: bool = True,
) -> str:
    """قيمة business_domain للحفظ (restaurant|hotel|shared)."""
    if filter_domain is not None:
        return filter_domain.value
    raw = (explicit or "").strip().lower()
    if raw in ("restaurant", "hotel", "shared"):
        if raw == "shared" and not allow_shared:
            return BusinessDomain.RESTAURANT.value
        return raw
    if inherit_from is not None:
        dom = getattr(inherit_from, "business_domain", None)
        if dom is not None:
            return dom.value if hasattr(dom, "value") else str(dom).strip().lower()
    return BusinessDomain.RESTAURANT.value


def purchase_domain_db_values(
    filter_domain: BusinessDomain | None,
) -> list[str] | None:
    if filter_domain is None:
        return None
    return [filter_domain.value, BusinessDomain.SHARED.value]


def reports_show_pos_sections(user: User | None, session: dict | None) -> bool:
    """إخفاء تقارير POS (جلسات، توصيل، ديون عملاء) في وضع الفندق."""
    return resolve_finance_domain(user, session) != BusinessDomain.HOTEL


def reports_show_hotel_sections(user: User | None, session: dict | None) -> bool:
    """إخفاء تقارير الفندق في وضع المطعم فقط."""
    return resolve_finance_domain(user, session) != BusinessDomain.RESTAURANT
