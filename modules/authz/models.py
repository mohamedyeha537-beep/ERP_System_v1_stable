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

    roles: Mapped[list[Role]] = relationship(
        secondary=user_roles, back_populates="users", lazy="selectin"
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


class Permission(Base):
    __tablename__ = "permissions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    label_ar: Mapped[str] = mapped_column(String(160), default="")

    # select يتجنب سلسلة تحميل عكسية عند قراءة صلاحيات المستخدم في كل طلب
    roles: Mapped[list[Role]] = relationship(
        secondary=role_permissions, back_populates="permissions", lazy="select"
    )
