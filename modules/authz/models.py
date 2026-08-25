from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, String, Table, Column, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship

from infra.db import Base

user_roles = Table(
    "user_roles",
    Base.metadata,
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("role_id", Integer, ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
)

role_permissions = Table(
    "role_permissions",
    Base.metadata,
    Column("role_id", Integer, ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
    Column("permission_id", Integer, ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True),
)

# صلاحيات إضافية مباشرة للمستخدم (تجاوز الأدوار)
user_permission_grants = Table(
    "user_permission_grants",
    Base.metadata,
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column(
        "permission_id",
        Integer,
        ForeignKey("permissions.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)

# منع صلاحية عن المستخدم حتى لو منحها دوره
user_permission_denies = Table(
    "user_permission_denies",
    Base.metadata,
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column(
        "permission_id",
        Integer,
        ForeignKey("permissions.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    warehouse_id: Mapped[int | None] = mapped_column(
        ForeignKey("warehouses.id", ondelete="SET NULL"), nullable=True, index=True
    )
    kds_scope: Mapped[str] = mapped_column(String(20), default="ALL")
    # نطاق الواجهة: restaurant | hotel | both — يحدّده الأدمن عند إنشاء/تعديل المستخدم
    view_scope: Mapped[str] = mapped_column(String(20), default="both")
    # أجزاء الواجهة المخفية عن المستخدم — JSON array من معرّفات ui_blocks
    ui_hidden: Mapped[str] = mapped_column(String(4000), default="[]")
    # خزائن نقطة البيع التي يراها المستخدم (كاش / مصرف) — يضبطها الأدمن
    pos_show_cash: Mapped[bool] = mapped_column(Boolean, default=True)
    pos_show_bank: Mapped[bool] = mapped_column(Boolean, default=True)

    roles: Mapped[list[Role]] = relationship(
        secondary=user_roles, back_populates="users", lazy="selectin"
    )
    permission_grants: Mapped[list[Permission]] = relationship(
        secondary=user_permission_grants, lazy="selectin"
    )
    permission_denies: Mapped[list[Permission]] = relationship(
        secondary=user_permission_denies, lazy="selectin"
    )


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name_ar: Mapped[str] = mapped_column(String(120), unique=True)

    # لا تستخدم selectin هنا: تحميل مستخدم واحد كان يجلب كل مستخدمي الأدوار
    users: Mapped[list[User]] = relationship(
        secondary=user_roles, back_populates="roles", lazy="select"
    )
    permissions: Mapped[list[Permission]] = relationship(
        secondary=role_permissions, back_populates="roles", lazy="selectin"
    )


class UserWalletAccess(Base):
    """حسابات التحويل المسموحة لموظف معيّن (من / إلى)."""

    __tablename__ = "user_wallet_access"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    payment_method_id: Mapped[int] = mapped_column(
        ForeignKey("payment_methods.id", ondelete="CASCADE"), primary_key=True
    )
    can_send: Mapped[bool] = mapped_column(Boolean, default=False)
    can_receive: Mapped[bool] = mapped_column(Boolean, default=False)


class Permission(Base):
    __tablename__ = "permissions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    label_ar: Mapped[str] = mapped_column(String(160), default="")

    # select يتجنب سلسلة تحميل عكسية عند قراءة صلاحيات المستخدم في كل طلب
    roles: Mapped[list[Role]] = relationship(
        secondary=role_permissions, back_populates="permissions", lazy="select"
    )
