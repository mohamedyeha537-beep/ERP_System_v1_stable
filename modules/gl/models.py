from __future__ import annotations

import enum
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from infra.db import Base


class GlAccountType(str, enum.Enum):
    ASSET = "ASSET"
    LIABILITY = "LIABILITY"
    EQUITY = "EQUITY"
    REVENUE = "REVENUE"
    EXPENSE = "EXPENSE"


class GlJournalEntryStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    POSTED = "POSTED"
    REVERSED = "REVERSED"


class GlAccount(Base):
    """حساب في دليل الحسابات."""

    __tablename__ = "gl_accounts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    name_ar: Mapped[str] = mapped_column(String(160))
    account_type: Mapped[GlAccountType] = mapped_column(Enum(GlAccountType), index=True)
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("gl_accounts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    show_on_dashboard: Mapped[bool] = mapped_column(Boolean, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    business_domain: Mapped[str] = mapped_column(
        String(20), default="shared", index=True
    )

    parent = relationship("GlAccount", remote_side="GlAccount.id")
    payment_method_maps = relationship(
        "GlPaymentMethodMap", back_populates="gl_account", cascade="all, delete-orphan"
    )


class GlExpenseCategoryMap(Base):
    """ربط تصنيف مصروف (نص حر) بحساب GL — لظهور الإيجار/النقل… في التقارير."""

    __tablename__ = "gl_expense_category_maps"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    category_label: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    gl_account_id: Mapped[int] = mapped_column(
        ForeignKey("gl_accounts.id", ondelete="RESTRICT"), index=True
    )

    gl_account = relationship("GlAccount")


class GlOperationalRoleMap(Base):
    """ربط دور تشغيلي (أصول ثابتة، إهلاك…) بحساب في شجرة GL — مصدر واحد للحقيقة."""

    __tablename__ = "gl_operational_role_maps"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    role_key: Mapped[str] = mapped_column(String(48), unique=True, index=True)
    gl_account_id: Mapped[int] = mapped_column(
        ForeignKey("gl_accounts.id", ondelete="RESTRICT"), index=True
    )

    gl_account = relationship("GlAccount")


class GlPaymentMethodMap(Base):
    """ربط محفظة (payment_method) بحساب GL — للمطابقة مع الخزينة."""

    __tablename__ = "gl_payment_method_maps"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    payment_method_id: Mapped[int] = mapped_column(
        ForeignKey("payment_methods.id", ondelete="CASCADE"), unique=True, index=True
    )
    gl_account_id: Mapped[int] = mapped_column(
        ForeignKey("gl_accounts.id", ondelete="RESTRICT"), index=True
    )

    gl_account = relationship("GlAccount", back_populates="payment_method_maps")
    payment_method = relationship("PaymentMethod")


class GlJournalEntry(Base):
    """قيد يومية — يُربَط بمصدر تشغيلي عبر source_type/source_id."""

    __tablename__ = "gl_journal_entries"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_gl_journal_entries_idempotency"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    entry_date: Mapped[date] = mapped_column(Date, index=True)
    description_ar: Mapped[str] = mapped_column(String(255))
    status: Mapped[GlJournalEntryStatus] = mapped_column(
        Enum(GlJournalEntryStatus), default=GlJournalEntryStatus.POSTED, index=True
    )
    source_type: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    source_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    post_mode: Mapped[str] = mapped_column(String(16), default="shadow")
    reversed_entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("gl_journal_entries.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    business_domain: Mapped[str] = mapped_column(
        String(20), default="restaurant", index=True
    )

    lines = relationship(
        "GlJournalLine",
        back_populates="entry",
        cascade="all, delete-orphan",
        order_by="GlJournalLine.line_no",
    )
    created_by = relationship("User", foreign_keys=[created_by_id])


class GlJournalLine(Base):
    __tablename__ = "gl_journal_lines"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    entry_id: Mapped[int] = mapped_column(
        ForeignKey("gl_journal_entries.id", ondelete="CASCADE"), index=True
    )
    account_id: Mapped[int] = mapped_column(
        ForeignKey("gl_accounts.id", ondelete="RESTRICT"), index=True
    )
    debit: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    credit: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    memo: Mapped[str | None] = mapped_column(String(255), nullable=True)
    line_no: Mapped[int] = mapped_column(Integer, default=1)

    entry = relationship("GlJournalEntry", back_populates="lines")
    account = relationship("GlAccount")


class FiscalYearStatus(str, enum.Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class FiscalYear(Base):
    """سنة مالية — تحدد فترة الحسابات وتوفر نقطة بداية لأرصدة الافتتاح."""

    __tablename__ = "fiscal_years"
    __table_args__ = (UniqueConstraint("name", name="uq_fiscal_years_name"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(80))
    start_date: Mapped[date] = mapped_column(Date, index=True)
    end_date: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[FiscalYearStatus] = mapped_column(
        Enum(FiscalYearStatus), default=FiscalYearStatus.OPEN, index=True
    )
    is_current: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    opening_balances = relationship(
        "AccountOpeningBalance",
        back_populates="fiscal_year",
        cascade="all, delete-orphan",
    )


class AccountOpeningBalance(Base):
    """رصيد افتتاحي لحساب في سنة مالية محددة."""

    __tablename__ = "account_opening_balances"
    __table_args__ = (
        UniqueConstraint("fiscal_year_id", "account_id", name="uq_aob_fy_account"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    fiscal_year_id: Mapped[int] = mapped_column(
        ForeignKey("fiscal_years.id", ondelete="CASCADE"), index=True
    )
    account_id: Mapped[int] = mapped_column(
        ForeignKey("gl_accounts.id", ondelete="RESTRICT"), index=True
    )
    debit: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    credit: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    fiscal_year = relationship("FiscalYear", back_populates="opening_balances")
    account = relationship("GlAccount")
