from __future__ import annotations

import bcrypt
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from infra.config import get_settings
from modules.authz.models import Permission, Role, User
from modules.authz.permissions import ALL_PERMISSIONS

# أسماء الأدوار كما في seed_if_empty — يجب أن تطابق name_ar في قاعدة البيانات
_DEMO_USER_ROLE_PAIRS: tuple[tuple[str, str], ...] = (
    ("cashier", "كاشير"),
    ("catalog", "إدخال أصناف"),
    ("warehouse", "مسؤول مخزن"),
)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


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

    admin_role = make_role("مدير النظام", *[c for c, _ in ALL_PERMISSIONS])
    make_role(
        "كاشير",
        "sales:create",
        "sales:refund",
        "inventory:view",
        "reports:view",
        "customers:view",
        "hotel:charge",
    )
    make_role(
        "إدخال أصناف",
        "catalog:write",
        "inventory:view",
    )
    make_role(
        "مسؤول مخزن",
        "inventory:view",
        "inventory:adjust",
        "reports:view",
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
    admin = db.execute(select(Role).where(Role.name_ar == "مدير النظام")).scalar_one_or_none()
    if admin is not None:
        admin_codes = {p.code for p in admin.permissions}
        for p in db.scalars(select(Permission)).all():
            if p.code not in admin_codes:
                admin.permissions.append(p)
        db.flush()
    db.commit()


def ensure_demo_users(db: Session) -> None:
    """مستخدمون تجريبيون (اسم مستخدم لاتيني + دور واحد) إن لم يكونوا موجودين."""
    settings = get_settings()
    if not settings.seed_demo_users:
        return
    pwd_hash = hash_password(settings.demo_users_password)
    created = False
    for username, role_name_ar in _DEMO_USER_ROLE_PAIRS:
        if get_user_by_username(db, username) is not None:
            continue
        role = db.execute(select(Role).where(Role.name_ar == role_name_ar)).scalar_one_or_none()
        if role is None:
            continue
        db.add(
            User(
                username=username,
                password_hash=pwd_hash,
                is_active=True,
                roles=[role],
            )
        )
        created = True
    if created:
        db.commit()
