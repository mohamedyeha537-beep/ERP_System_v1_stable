"""Customers & loyalty business logic."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from modules.customers.models import (
    Customer,
    LoyaltyTransaction,
    LoyaltyTxnKind,
)
from modules.settings.service import get_setting


class CustomersError(Exception):
    """خطأ في إدارة العملاء/الولاء."""


# ============================================================
# Phone normalization
# ============================================================

_PHONE_RE = re.compile(r"[^\d+]")


def normalize_phone(raw: str | None) -> str:
    """يُرجِع الهاتف بعد إزالة الفراغات والشرطات. يحتفظ بالأرقام و+."""
    if raw is None:
        return ""
    s = _PHONE_RE.sub("", str(raw))
    return s.strip()


# ============================================================
# CRUD
# ============================================================

def list_customers(
    db: Session,
    *,
    only_active: bool = False,
    search: str | None = None,
    limit: int = 500,
) -> list[Customer]:
    stmt = select(Customer)
    if only_active:
        stmt = stmt.where(Customer.is_active.is_(True))
    if search:
        like = f"%{search.strip()}%"
        stmt = stmt.where(
            (Customer.phone.like(like)) | (Customer.name.like(like))
        )
    stmt = stmt.order_by(Customer.last_visit_at.desc().nulls_last(), Customer.id.desc()).limit(limit)
    return list(db.scalars(stmt))


def get_customer(db: Session, customer_id: int) -> Customer | None:
    return db.get(Customer, customer_id)


def get_by_phone(db: Session, phone: str) -> Customer | None:
    p = normalize_phone(phone)
    if not p:
        return None
    return db.scalar(select(Customer).where(Customer.phone == p))


def create_customer(
    db: Session,
    *,
    phone: str,
    name: str | None = None,
    email: str | None = None,
    notes: str | None = None,
) -> Customer:
    p = normalize_phone(phone)
    if not p:
        raise CustomersError("رقم الهاتف مطلوب.")
    if get_by_phone(db, p) is not None:
        raise CustomersError(f"رقم الهاتف {p} مسجَّل بالفعل.")
    c = Customer(
        phone=p,
        name=(name or "").strip() or None,
        email=(email or "").strip() or None,
        notes=(notes or "").strip() or None,
        is_active=True,
    )
    db.add(c)
    db.flush()
    return c


def get_or_create_by_phone(
    db: Session,
    *,
    phone: str,
    name: str | None = None,
) -> Customer:
    """يبحث عن عميل برقم الهاتف، فإن لم يوجد ينشئه."""
    p = normalize_phone(phone)
    if not p:
        raise CustomersError("رقم الهاتف مطلوب.")
    existing = get_by_phone(db, p)
    if existing is not None:
        # حدّث الاسم إن لم يكن مسجّلاً وأُدخل الآن
        if not existing.name and (name or "").strip():
            existing.name = name.strip()
            db.flush()
        return existing
    return create_customer(db, phone=p, name=name)


def update_customer(
    db: Session,
    customer_id: int,
    *,
    phone: str | None = None,
    name: str | None = None,
    email: str | None = None,
    notes: str | None = None,
    is_active: bool | None = None,
) -> Customer:
    c = db.get(Customer, customer_id)
    if c is None:
        raise CustomersError("العميل غير موجود.")
    if phone is not None:
        p = normalize_phone(phone)
        if not p:
            raise CustomersError("رقم الهاتف مطلوب.")
        existing = get_by_phone(db, p)
        if existing is not None and existing.id != customer_id:
            raise CustomersError(f"رقم الهاتف {p} مسجَّل لعميل آخر.")
        c.phone = p
    if name is not None:
        c.name = name.strip() or None
    if email is not None:
        c.email = email.strip() or None
    if notes is not None:
        c.notes = notes.strip() or None
    if is_active is not None:
        c.is_active = is_active
    db.flush()
    return c


def delete_customer(db: Session, customer_id: int) -> None:
    c = db.get(Customer, customer_id)
    if c is None:
        return
    # لا نحذف العملاء الذين عليهم سجلات نقاط — نعطّلهم بدلاً من ذلك
    txn_count = db.scalar(
        select(func.count(LoyaltyTransaction.id)).where(
            LoyaltyTransaction.customer_id == customer_id
        )
    )
    if (txn_count or 0) > 0:
        raise CustomersError(
            "لا يمكن حذف عميل له سجلات نقاط — يمكنك تعطيله بدلاً من الحذف."
        )
    db.delete(c)
    db.flush()


# ============================================================
# Loyalty engine
# ============================================================

def _setting_decimal(db: Session, key: str, default: str) -> Decimal:
    val = get_setting(db, key, default)
    try:
        return Decimal(str(val).strip() or default)
    except Exception:  # noqa: BLE001
        return Decimal(default)


def _setting_bool(db: Session, key: str, default: bool) -> bool:
    val = get_setting(db, key, "1" if default else "0")
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def loyalty_settings(db: Session) -> dict:
    """يقرأ إعدادات الولاء من جدول AppSetting."""
    return {
        "enabled": _setting_bool(db, "loyalty_enabled", True),
        "earn_per_dinar": _setting_decimal(db, "loyalty_earn_per_dinar", "1"),
        "redeem_value_per_point": _setting_decimal(
            db, "loyalty_redeem_value_per_point", "0.1"
        ),
        "min_points_to_redeem": _setting_decimal(
            db, "loyalty_min_points_to_redeem", "50"
        ),
    }


def points_to_dinars(db: Session, points: Decimal) -> Decimal:
    """يحوّل النقاط إلى قيمة دينار حسب الإعدادات."""
    s = loyalty_settings(db)
    return (Decimal(points) * s["redeem_value_per_point"]).quantize(
        Decimal("0.001")
    )


def grant_points_for_sale(
    db: Session,
    *,
    customer: Customer,
    sale_id: int | None,
    sale_total: Decimal,
    user_id: int | None = None,
) -> Decimal:
    """يضيف نقاط ولاء للعميل بناءً على إجمالي الفاتورة + يحدّث إحصاءاته.

    يعيد عدد النقاط الممنوحة (قد يكون صفر لو الولاء معطّل).
    """
    s = loyalty_settings(db)
    if not s["enabled"]:
        # نحدّث الإحصاءات حتى مع تعطيل الولاء
        _bump_visit_stats(customer, sale_total)
        db.flush()
        return Decimal("0")

    earn_rate = s["earn_per_dinar"]
    points = (Decimal(sale_total) * earn_rate).quantize(Decimal("0.001"))
    if points <= 0:
        _bump_visit_stats(customer, sale_total)
        db.flush()
        return Decimal("0")

    customer.points_balance = (
        Decimal(customer.points_balance or 0) + points
    )
    _bump_visit_stats(customer, sale_total)

    txn = LoyaltyTransaction(
        customer_id=customer.id,
        sale_id=sale_id if sale_id else None,
        kind=LoyaltyTxnKind.EARN,
        points=points,
        note=f"كسب من فاتورة #{sale_id}" if sale_id else "كسب من فاتورة",
        created_by_id=user_id,
    )
    db.add(txn)
    db.flush()
    return points


def _bump_visit_stats(customer: Customer, sale_total: Decimal) -> None:
    customer.total_spent = Decimal(customer.total_spent or 0) + Decimal(sale_total)
    customer.visits_count = int(customer.visits_count or 0) + 1
    customer.last_visit_at = datetime.now(timezone.utc)


def adjust_points(
    db: Session,
    *,
    customer_id: int,
    delta_points: Decimal,
    note: str | None,
    user_id: int | None = None,
) -> LoyaltyTransaction:
    """تعديل يدوي من الأدمن (إضافة أو خصم — استخدم سالب للخصم)."""
    c = db.get(Customer, customer_id)
    if c is None:
        raise CustomersError("العميل غير موجود.")
    delta = Decimal(delta_points).quantize(Decimal("0.001"))
    new_balance = Decimal(c.points_balance or 0) + delta
    if new_balance < 0:
        raise CustomersError(
            f"التعديل سيجعل الرصيد سالباً ({new_balance}). "
            "أَدخل قيمة أصغر."
        )
    c.points_balance = new_balance
    txn = LoyaltyTransaction(
        customer_id=customer_id,
        sale_id=None,
        kind=LoyaltyTxnKind.ADJUST,
        points=delta,
        note=(note or "تعديل يدوي").strip(),
        created_by_id=user_id,
    )
    db.add(txn)
    db.flush()
    return txn


def list_transactions(
    db: Session, customer_id: int, limit: int = 50
) -> list[LoyaltyTransaction]:
    stmt = (
        select(LoyaltyTransaction)
        .where(LoyaltyTransaction.customer_id == customer_id)
        .order_by(LoyaltyTransaction.created_at.desc())
        .limit(limit)
    )
    return list(db.scalars(stmt))


def total_points_grand(db: Session) -> Decimal:
    val = db.scalar(
        select(func.coalesce(func.sum(Customer.points_balance), 0))
    )
    return Decimal(val or 0)
