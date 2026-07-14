from __future__ import annotations

import bcrypt
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from infra.config import get_settings
from modules.authz.models import Permission, Role, User
from modules.authz.permissions import (
    ADMIN_ROLE_NAME_AR,
    ALL_PERMISSIONS,
    CATALOG_PURCHASES_ROLE_NAME_AR,
    HOTEL_BOOKINGS_ONLY_ROLE_NAME_AR,
    SALES_AGENT_ROLE_NAME_AR,
    SUPPORT_AGENT_ROLE_NAME_AR,
    TREASURY_CLERK_ROLE_NAME_AR,
)
from modules.platform.business_domain import (
    HOTEL_STAFF_ROLE_NAME_AR,
    RESTAURANT_STAFF_ROLE_NAME_AR,
    UserViewScope,
)

KDS_WORKER_ROLE_NAME_AR = "عامل تجهيز KDS"
PURCHASES_DEMO_USERNAME = "purchases"

# أسماء الأدوار كما في seed_if_empty — يجب أن تطابق name_ar في قاعدة البيانات
_DEMO_USER_ROLE_PAIRS: tuple[tuple[str, str], ...] = (
    ("cashier", "كاشير"),
    ("treasury", TREASURY_CLERK_ROLE_NAME_AR),
    ("support", SUPPORT_AGENT_ROLE_NAME_AR),
    ("sales", SALES_AGENT_ROLE_NAME_AR),
    ("catalog", "إدخال أصناف"),
    ("catalog_purchases", CATALOG_PURCHASES_ROLE_NAME_AR),
    (PURCHASES_DEMO_USERNAME, CATALOG_PURCHASES_ROLE_NAME_AR),
    ("hotel_bookings", HOTEL_BOOKINGS_ONLY_ROLE_NAME_AR),
    ("warehouse", "مسؤول مخزن"),
    ("hotel_staff", HOTEL_STAFF_ROLE_NAME_AR),
    ("restaurant_staff", RESTAURANT_STAFF_ROLE_NAME_AR),
    ("kds_worker", KDS_WORKER_ROLE_NAME_AR),
)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def user_has_permission(user: User, code: str) -> bool:
    for role in user.roles:
        for perm in role.permissions:
            if perm.code == code:
                return True
    return False


def get_user_by_username(db: Session, username: str) -> User | None:
    stmt = (
        select(User)
        .where(User.username == username)
        .options(selectinload(User.roles).selectinload(Role.permissions))
    )
    return db.execute(stmt).scalar_one_or_none()


def seed_if_empty(db: Session) -> None:
    if db.execute(select(Permission).limit(1)).scalar_one_or_none() is not None:
        return

    perms = {}
    for code, label in ALL_PERMISSIONS:
        p = Permission(code=code, label_ar=label)
        db.add(p)
        perms[code] = p
    db.flush()

    def make_role(name: str, *codes: str) -> Role:
        r = Role(name_ar=name)
        r.permissions = [perms[c] for c in codes]
        db.add(r)
        return r

    admin_role = make_role(ADMIN_ROLE_NAME_AR, *[c for c, _ in ALL_PERMISSIONS])
    make_role(
        "كاشير",
        "sales:create",
        "customers:view",
        "hotel:charge",
    )
    make_role(
        TREASURY_CLERK_ROLE_NAME_AR,
        "payments:manage",
        "reports:view",
        "purchases:manage",
        "purchases:invoices",
        "hotel:settle",
        "customers:view",
        "sales:correct_payment",
    )
    make_role(
        SUPPORT_AGENT_ROLE_NAME_AR,
        "customers:view",
        "messaging:send",
        "messaging:view",
    )
    make_role(
        SALES_AGENT_ROLE_NAME_AR,
        "customers:view",
        "messaging:send",
    )
    make_role(
        "إدخال أصناف",
        "catalog:write",
        "inventory:view",
    )
    make_role(
        CATALOG_PURCHASES_ROLE_NAME_AR,
        "catalog:write",
        "purchases:invoices",
    )
    make_role(
        "مسؤول مخزن",
        "inventory:view",
        "inventory:adjust",
        "warehouses:manage",
        "reports:view",
    )
    make_role(
        HOTEL_BOOKINGS_ONLY_ROLE_NAME_AR,
        "hotel:booking:view",
        "hotel:booking:create",
        "hotel:booking:manage",
        "hotel:booking:checkin",
        "hotel:booking:checkout",
        "hotel:settle",
    )
    db.flush()

    settings = get_settings()
    admin = User(
        username=settings.default_admin_username,
        password_hash=hash_password(settings.default_admin_password),
        is_active=True,
        roles=[admin_role],
    )
    db.add(admin)
    db.commit()


def sync_admin_role_permissions(db: Session) -> None:
    """دور مدير النظام يملك دائماً جميع الصلاحيات."""
    admin = db.execute(
        select(Role).where(Role.name_ar == ADMIN_ROLE_NAME_AR)
    ).scalar_one_or_none()
    if admin is None:
        return
    all_perms = list(db.scalars(select(Permission)).all())
    admin.permissions = all_perms
    db.flush()


def sync_permissions(db: Session) -> None:
    """يضيف أي صلاحية جديدة معرّفة في الكود إلى قاعدة البيانات،
    ويمنح دور «مدير النظام» جميع الصلاحيات (الجديدة منها والقديمة)."""
    existing = {p.code: p for p in db.scalars(select(Permission)).all()}
    added = False
    for code, label in ALL_PERMISSIONS:
        if code not in existing:
            p = Permission(code=code, label_ar=label)
            db.add(p)
            added = True
    if added:
        db.flush()
    sync_admin_role_permissions(db)
    sync_cashier_role_permissions(db)
    ensure_treasury_clerk_role(db)
    ensure_support_agent_role(db)
    ensure_sales_agent_role(db)
    ensure_catalog_purchases_role(db)
    ensure_hotel_bookings_only_role(db)
    ensure_hotel_staff_role(db)
    ensure_restaurant_staff_role(db)
    ensure_kds_worker_role(db)
    db.commit()


_CASHIER_ROLE_PERMISSIONS = (
    "sales:create",
    "customers:view",
    "hotel:charge",
)


_TREASURY_CLERK_ROLE_PERMISSIONS = (
    "payments:manage",
    "reports:view",
    "purchases:manage",
    "purchases:invoices",
    "hotel:settle",
    "hotel:booking:view",
    "hotel:booking:create",
    "hotel:booking:checkin",
    "hotel:booking:checkout",
    "customers:view",
    "sales:correct_payment",
)

_HOTEL_STAFF_ROLE_PERMISSIONS = (
    "hotel:booking:view",
    "hotel:booking:create",
    "hotel:booking:checkin",
    "hotel:booking:checkout",
    "hotel:settle",
    "hotel:housekeeping",
    "customers:view",
)

_RESTAURANT_STAFF_ROLE_PERMISSIONS = (
    "sales:create",
    "tables:manage",
    "kds:view",
    "customers:view",
    "inventory:receive",
    "inventory:view",
)


def ensure_treasury_clerk_role(db: Session) -> None:
    """يُنشئ دور أمين الخزينة إن لم يكن موجوداً ويضيف أي صلاحيات مالية جديدة."""
    role = db.execute(
        select(Role).where(Role.name_ar == TREASURY_CLERK_ROLE_NAME_AR)
    ).scalar_one_or_none()
    perms = {p.code: p for p in db.scalars(select(Permission)).all()}
    codes = _TREASURY_CLERK_ROLE_PERMISSIONS
    if role is None:
        role = Role(name_ar=TREASURY_CLERK_ROLE_NAME_AR)
        role.permissions = [perms[c] for c in codes if c in perms]
        db.add(role)
        db.flush()
        return
    existing = {p.code for p in role.permissions}
    for c in codes:
        if c in perms and c not in existing:
            role.permissions.append(perms[c])
    db.flush()


def get_treasury_clerk_role(db: Session) -> Role | None:
    return db.execute(
        select(Role).where(Role.name_ar == TREASURY_CLERK_ROLE_NAME_AR)
    ).scalar_one_or_none()


def is_admin_role(role: Role) -> bool:
    return (role.name_ar or "").strip() == ADMIN_ROLE_NAME_AR


_SUPPORT_AGENT_ROLE_PERMISSIONS = (
    "customers:view",
    "messaging:send",
    "messaging:view",
)

_SALES_AGENT_ROLE_PERMISSIONS = (
    "customers:view",
    "messaging:send",
)

_CATALOG_PURCHASES_ROLE_PERMISSIONS = (
    "catalog:write",
    "purchases:invoices",
)

_HOTEL_BOOKINGS_ONLY_ROLE_PERMISSIONS = (
    "hotel:booking:view",
    "hotel:booking:create",
    "hotel:booking:manage",
    "hotel:booking:checkin",
    "hotel:booking:checkout",
    "hotel:settle",
)


def _ensure_role_with_permissions(
    db: Session, role_name: str, codes: tuple[str, ...]
) -> None:
    role = db.execute(select(Role).where(Role.name_ar == role_name)).scalar_one_or_none()
    perms = {p.code: p for p in db.scalars(select(Permission)).all()}
    if role is None:
        role = Role(name_ar=role_name)
        role.permissions = [perms[c] for c in codes if c in perms]
        db.add(role)
        db.flush()
        return
    existing = {p.code for p in role.permissions}
    for c in codes:
        if c in perms and c not in existing:
            role.permissions.append(perms[c])
    db.flush()


def _sync_role_permissions_exact(db: Session, role_name: str, codes: tuple[str, ...]) -> None:
    role = db.execute(select(Role).where(Role.name_ar == role_name)).scalar_one_or_none()
    perms = {p.code: p for p in db.scalars(select(Permission)).all()}
    if role is None:
        role = Role(name_ar=role_name)
        db.add(role)
    role.permissions = [perms[c] for c in codes if c in perms]
    db.flush()


def ensure_support_agent_role(db: Session) -> None:
    _ensure_role_with_permissions(
        db, SUPPORT_AGENT_ROLE_NAME_AR, _SUPPORT_AGENT_ROLE_PERMISSIONS
    )


def ensure_sales_agent_role(db: Session) -> None:
    _ensure_role_with_permissions(
        db, SALES_AGENT_ROLE_NAME_AR, _SALES_AGENT_ROLE_PERMISSIONS
    )


def ensure_catalog_purchases_role(db: Session) -> None:
    _sync_role_permissions_exact(
        db, CATALOG_PURCHASES_ROLE_NAME_AR, _CATALOG_PURCHASES_ROLE_PERMISSIONS
    )


def ensure_hotel_bookings_only_role(db: Session) -> None:
    _sync_role_permissions_exact(
        db, HOTEL_BOOKINGS_ONLY_ROLE_NAME_AR, _HOTEL_BOOKINGS_ONLY_ROLE_PERMISSIONS
    )


def ensure_hotel_staff_role(db: Session) -> None:
    _ensure_role_with_permissions(
        db, HOTEL_STAFF_ROLE_NAME_AR, _HOTEL_STAFF_ROLE_PERMISSIONS
    )


def ensure_restaurant_staff_role(db: Session) -> None:
    _ensure_role_with_permissions(
        db, RESTAURANT_STAFF_ROLE_NAME_AR, _RESTAURANT_STAFF_ROLE_PERMISSIONS
    )


def ensure_kds_worker_role(db: Session) -> None:
    _sync_role_permissions_exact(db, KDS_WORKER_ROLE_NAME_AR, ("kds:view",))


def sync_cashier_role_permissions(db: Session) -> None:
    """يضبط دور «كاشير» على صلاحيات نقطة البيع فقط (واجهة مقتصرة + رقم سري)."""
    role = db.execute(select(Role).where(Role.name_ar == "كاشير")).scalar_one_or_none()
    if role is None:
        return
    perms = {p.code: p for p in db.scalars(select(Permission)).all()}
    role.permissions = [perms[c] for c in _CASHIER_ROLE_PERMISSIONS if c in perms]
    db.flush()


def get_cashier_role(db: Session) -> Role | None:
    return db.execute(select(Role).where(Role.name_ar == "كاشير")).scalar_one_or_none()


_DEMO_USER_VIEW_SCOPE: dict[str, str] = {
    "hotel_staff": UserViewScope.HOTEL.value,
    "hotel_bookings": UserViewScope.HOTEL.value,
    "restaurant_staff": UserViewScope.RESTAURANT.value,
    "cashier": UserViewScope.RESTAURANT.value,
    "kds_worker": UserViewScope.RESTAURANT.value,
}


def ensure_demo_users(db: Session) -> None:
    """مستخدمون تجريبيون (اسم مستخدم لاتيني + دور واحد) إن لم يكونوا موجودين."""
    settings = get_settings()
    if not settings.seed_demo_users:
        return
    if not settings.demo_users_password:
        raise ValueError("DEMO_USERS_PASSWORD مطلوب عند تفعيل SEED_DEMO_USERS.")
    pwd_hash = hash_password(settings.demo_users_password)
    changed = False
    for username, role_name_ar in _DEMO_USER_ROLE_PAIRS:
        role = db.execute(select(Role).where(Role.name_ar == role_name_ar)).scalar_one_or_none()
        if role is None:
            continue
        user = get_user_by_username(db, username)
        if user is None:
            db.add(
                User(
                    username=username,
                    password_hash=pwd_hash,
                    is_active=True,
                    roles=[role],
                    view_scope=_DEMO_USER_VIEW_SCOPE.get(username, UserViewScope.BOTH.value),
                )
            )
            changed = True
            continue
        if {r.id for r in user.roles} != {role.id}:
            user.roles = [role]
            changed = True
        expected_scope = _DEMO_USER_VIEW_SCOPE.get(username, UserViewScope.BOTH.value)
        if (user.view_scope or UserViewScope.BOTH.value) != expected_scope:
            user.view_scope = expected_scope
            changed = True
        if not user.is_active:
            user.is_active = True
            changed = True
    if changed:
        db.commit()


def ensure_purchases_clerk_demo_user(db: Session) -> None:
    """مستخدم تجريبي لموظف المشتريات — يُنشأ فقط عند تفعيل بذور المستخدمين."""
    settings = get_settings()
    if not settings.seed_demo_users:
        return
    if not settings.demo_users_password:
        raise ValueError("DEMO_USERS_PASSWORD مطلوب عند تفعيل SEED_DEMO_USERS.")
    ensure_catalog_purchases_role(db)
    role = db.execute(
        select(Role).where(Role.name_ar == CATALOG_PURCHASES_ROLE_NAME_AR)
    ).scalar_one_or_none()
    if role is None:
        return
    pwd_hash = hash_password(settings.demo_users_password)
    user = get_user_by_username(db, PURCHASES_DEMO_USERNAME)
    if user is None:
        db.add(
            User(
                username=PURCHASES_DEMO_USERNAME,
                password_hash=pwd_hash,
                is_active=True,
                roles=[role],
            )
        )
        db.flush()
        return
    if {r.id for r in user.roles} != {role.id}:
        user.roles = [role]
    if not user.is_active:
        user.is_active = True
    db.flush()
