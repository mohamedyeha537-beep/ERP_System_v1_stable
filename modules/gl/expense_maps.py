from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from modules.gl.models import GlAccount, GlExpenseCategoryMap, GlAccountType


def list_expense_category_maps(db: Session) -> list[GlExpenseCategoryMap]:
    try:
        return list(
            db.scalars(
                select(GlExpenseCategoryMap)
                .options(selectinload(GlExpenseCategoryMap.gl_account))
                .order_by(GlExpenseCategoryMap.category_label)
            ).all()
        )
    except (OperationalError, SQLAlchemyError):
        return []


def expense_account_code_for_category(db: Session, category: str | None) -> str:
    """يُرجع رمز حساب GL للتصنيف — أو 5200 افتراضياً."""
    label = (category or "").strip()
    if not label:
        return "5200"
    row = db.scalar(
        select(GlExpenseCategoryMap)
        .options(selectinload(GlExpenseCategoryMap.gl_account))
        .where(GlExpenseCategoryMap.category_label == label)
    )
    if row is not None and row.gl_account is not None and row.gl_account.is_active:
        return str(row.gl_account.code)
    # مطابقة جزئية (بدون حساسية لحالة الأحرف)
    for m in list_expense_category_maps(db):
        if m.gl_account is None or not m.gl_account.is_active:
            continue
        if label.lower() in m.category_label.lower() or m.category_label.lower() in label.lower():
            return str(m.gl_account.code)
    return "5200"


def set_expense_category_map(
    db: Session, *, category_label: str, gl_account_id: int
) -> GlExpenseCategoryMap:
    from modules.gl.service import GLError

    label = (category_label or "").strip()
    if len(label) < 2:
        raise GLError("اسم التصنيف قصير جداً.")
    if len(label) > 80:
        raise GLError("اسم التصنيف طويل جداً.")
    acc = db.get(GlAccount, gl_account_id)
    if acc is None:
        raise GLError("حساب GL غير موجود.")
    if acc.account_type != GlAccountType.EXPENSE:
        raise GLError("يجب اختيار حساب من نوع «مصروفات».")
    row = db.scalar(
        select(GlExpenseCategoryMap).where(GlExpenseCategoryMap.category_label == label)
    )
    if row is None:
        row = GlExpenseCategoryMap(category_label=label, gl_account_id=gl_account_id)
        db.add(row)
    else:
        row.gl_account_id = gl_account_id
    db.flush()
    return row


def delete_expense_category_map(db: Session, map_id: int) -> None:
    from modules.gl.service import GLError

    row = db.get(GlExpenseCategoryMap, map_id)
    if row is None:
        raise GLError("الربط غير موجود.")
    db.delete(row)
    db.flush()
