"""حسابات GL تشغيلية للفندق — مرآة لأقسام المطعم (أصول، خصوم، إيرادات، مصروفات)."""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from modules.gl.domain import account_domain_for_code
from modules.gl.models import GlAccount, GlAccountType
from modules.platform.business_domain import BusinessDomain

# (code, name_ar, account_type, parent_code)
HOTEL_OPERATING_ACCOUNTS: tuple[tuple[str, str, GlAccountType, str | None], ...] = (
    ("1500", "الأصول — فندق", GlAccountType.ASSET, None),
    ("1510", "النقد وما في حكمه — فندق", GlAccountType.ASSET, "1500"),
    ("1520", "ذمم مدينة — فندق", GlAccountType.ASSET, "1500"),
    ("1530", "المخزون — فندق", GlAccountType.ASSET, "1500"),
    ("1540", "أصول ثابتة — فندق", GlAccountType.ASSET, "1500"),
    ("1550", "مجمع إهلاك — فندق", GlAccountType.ASSET, "1500"),
    ("2500", "الخصوم — فندق", GlAccountType.LIABILITY, None),
    ("2510", "ذمم دائنة — موردين — فندق", GlAccountType.LIABILITY, "2500"),
    ("2520", "رواتب مستحقة — فندق", GlAccountType.LIABILITY, "2500"),
    ("3500", "حقوق الملكية — فندق", GlAccountType.EQUITY, None),
    ("3510", "حساب المالك — فندق", GlAccountType.EQUITY, "3500"),
    ("4500", "الإيرادات — فندق", GlAccountType.REVENUE, None),
    ("4520", "مرتجعات — فندق", GlAccountType.REVENUE, "4500"),
    ("5500", "المصروفات — فندق", GlAccountType.EXPENSE, None),
    ("5510", "تكلفة المبيعات — فندق", GlAccountType.EXPENSE, "5500"),
    ("5520", "مصروفات تشغيلية — فندق", GlAccountType.EXPENSE, "5500"),
    ("5521", "إيجار — فندق", GlAccountType.EXPENSE, "5520"),
    ("5522", "كهرباء وماء — فندق", GlAccountType.EXPENSE, "5520"),
    ("5523", "نقل وتوصيل — فندق", GlAccountType.EXPENSE, "5520"),
    ("5524", "صيانة — فندق", GlAccountType.EXPENSE, "5520"),
    ("5525", "مستلزمات ومشتريات — فندق", GlAccountType.EXPENSE, "5520"),
    ("5530", "مصروفات الرواتب — فندق", GlAccountType.EXPENSE, "5500"),
    ("5540", "مصروف إهلاك — فندق", GlAccountType.EXPENSE, "5500"),
)

HOTEL_EXISTING_ACCOUNT_PARENTS: tuple[tuple[str, str], ...] = (
    ("1115", "1510"),
    ("1125", "1510"),
    ("1135", "1510"),
    ("1136", "1510"),
    ("4150", "4500"),
)

PURCHASE_CUSTODY_GL_ACCOUNTS: tuple[tuple[str, str, str], ...] = (
    ("1130", "عهدة مشتريات — مطعم — كاش", "1100"),
    ("1132", "عهدة مشتريات — مطعم — مصرف", "1100"),
    ("1135", "عهدة مشتريات — فندق — كاش", "1510"),
    ("1136", "عهدة مشتريات — فندق — مصرف", "1510"),
)


def ensure_purchase_custody_gl_accounts(db: Session) -> None:
    """حسابات عهدة المشتريات — مطعم / فندق."""
    ensure_hotel_operating_chart(db)
    code_to_acc = {row.code: row for row in db.scalars(select(GlAccount)).all()}
    max_sort = int(db.scalar(select(func.max(GlAccount.sort_order))) or 0)
    for code, name_ar, parent_code in PURCHASE_CUSTODY_GL_ACCOUNTS:
        parent = code_to_acc.get(parent_code)
        acc = code_to_acc.get(code)
        if acc is None:
            max_sort += 10
            acc = GlAccount(
                code=code,
                name_ar=name_ar,
                account_type=GlAccountType.ASSET,
                parent_id=parent.id if parent else None,
                is_system=True,
                is_active=True,
                show_on_dashboard=True,
                sort_order=max_sort,
                business_domain=account_domain_for_code(code),
            )
            db.add(acc)
            db.flush()
            code_to_acc[code] = acc
        else:
            acc.name_ar = name_ar
            acc.is_active = True
            acc.show_on_dashboard = True
            acc.business_domain = account_domain_for_code(code)
            if parent is not None:
                acc.parent_id = parent.id
    db.flush()


def ensure_hotel_operating_chart(db: Session) -> None:
    """دليل حسابات تشغيلي للفندق في كل الأقسام."""
    code_to_acc = {row.code: row for row in db.scalars(select(GlAccount)).all()}
    max_sort = int(db.scalar(select(func.max(GlAccount.sort_order))) or 0)
    hotel_dom = BusinessDomain.HOTEL.value

    for code, name_ar, acc_type, parent_code in HOTEL_OPERATING_ACCOUNTS:
        parent = code_to_acc.get(parent_code) if parent_code else None
        acc = code_to_acc.get(code)
        if acc is None:
            max_sort += 10
            acc = GlAccount(
                code=code,
                name_ar=name_ar,
                account_type=acc_type,
                parent_id=parent.id if parent else None,
                is_system=True,
                is_active=True,
                sort_order=max_sort,
                business_domain=hotel_dom,
            )
            db.add(acc)
            db.flush()
            code_to_acc[code] = acc
        else:
            acc.name_ar = name_ar
            acc.account_type = acc_type
            acc.business_domain = hotel_dom
            acc.is_active = True
            if parent is not None:
                acc.parent_id = parent.id

    for child_code, parent_code in HOTEL_EXISTING_ACCOUNT_PARENTS:
        child = code_to_acc.get(child_code)
        parent = code_to_acc.get(parent_code)
        if child is None or parent is None:
            continue
        child.parent_id = parent.id
        child.business_domain = hotel_dom

    db.flush()
