from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from modules.gl.domain import account_domain_for_code, ensure_gl_account_domains
from modules.gl.models import (
    AccountOpeningBalance,
    FiscalYear,
    GlAccount,
    GlAccountType,
    GlExpenseCategoryMap,
    GlOperationalRoleMap,
    GlPaymentMethodMap,
)
from modules.gl.role_maps import OPERATIONAL_ROLES
from modules.payments.models import (
    HOTEL_TREASURY_BANK_PM_NAME,
    HOTEL_TREASURY_CASH_PM_NAME,
    MAIN_TREASURY_BANK_PM_NAME,
    MAIN_TREASURY_CASH_PM_NAME,
    OWNER_EQUITY_PM_NAME,
    PaymentMethod,
    SUPPLIER_CREDIT_PM_NAME,
)

HOTEL_WALLET_ACCOUNTS: tuple[tuple[str, str, str, GlAccountType], ...] = (
    (HOTEL_TREASURY_CASH_PM_NAME, "1115", "خزينة الفندق — كاش", GlAccountType.ASSET),
    (HOTEL_TREASURY_BANK_PM_NAME, "1125", "خزينة الفندق — مصرف", GlAccountType.ASSET),
)

DEFAULT_ACCOUNTS: tuple[tuple[str, str, GlAccountType, bool], ...] = (
    ("1000", "الأصول", GlAccountType.ASSET, True),
    ("1100", "النقد وما في حكمه", GlAccountType.ASSET, True),
    ("1110", "صندوق / خزينة كاش", GlAccountType.ASSET, True),
    ("1120", "حسابات بنكية", GlAccountType.ASSET, True),
    ("1130", "عهدة مشتريات — مطعم — كاش", GlAccountType.ASSET, True),
    ("1132", "عهدة مشتريات — مطعم — مصرف", GlAccountType.ASSET, True),
    ("1200", "ذمم مدينة (عملاء / غرف)", GlAccountType.ASSET, True),
    ("1300", "المخزون", GlAccountType.ASSET, True),
    ("2000", "الخصوم", GlAccountType.LIABILITY, True),
    ("2100", "ذمم دائنة — موردين", GlAccountType.LIABILITY, True),
    ("2200", "رواتب مستحقة", GlAccountType.LIABILITY, True),
    ("3000", "حقوق الملكية", GlAccountType.EQUITY, True),
    ("3100", "حساب المالك", GlAccountType.EQUITY, True),
    ("3500", "ملخص الدخل", GlAccountType.EQUITY, True),
    ("3600", "الأرباح المحتجزة", GlAccountType.EQUITY, True),
    ("4000", "الإيرادات", GlAccountType.REVENUE, True),
    ("4100", "إيرادات المبيعات", GlAccountType.REVENUE, True),
    ("4150", "إيرادات الإقامة (فندق)", GlAccountType.REVENUE, True),
    ("4200", "مرتجعات المبيعات", GlAccountType.REVENUE, True),
    ("5000", "المصروفات", GlAccountType.EXPENSE, True),
    ("5100", "تكلفة المبيعات", GlAccountType.EXPENSE, True),
    ("5200", "مصروفات تشغيلية", GlAccountType.EXPENSE, True),
    ("5210", "إيجار", GlAccountType.EXPENSE, True),
    ("5220", "كهرباء وماء", GlAccountType.EXPENSE, True),
    ("5230", "نقل وتوصيل", GlAccountType.EXPENSE, True),
    ("5240", "صيانة", GlAccountType.EXPENSE, True),
    ("5250", "مستلزمات ومشتريات", GlAccountType.EXPENSE, True),
    ("5300", "مصروفات الرواتب", GlAccountType.EXPENSE, True),
    ("1410", "أصول ثابتة", GlAccountType.ASSET, True),
    ("1420", "مجمع إهلاك الأصول", GlAccountType.ASSET, True),
    ("5400", "مصروف إهلاك", GlAccountType.EXPENSE, True),
)

# child_code → parent_code (للعرض التجميعي — لا تُرحَّل قيود على الحسابات الأب)
CHART_PARENT_LINKS: tuple[tuple[str, str], ...] = (
    ("1100", "1000"),
    ("1110", "1100"),
    ("1120", "1100"),
    ("1130", "1100"),
    ("1132", "1100"),
    ("1200", "1000"),
    ("1300", "1000"),
    ("1410", "1000"),
    ("1420", "1000"),
    ("2100", "2000"),
    ("2200", "2000"),
    ("3100", "3000"),
    ("3500", "3000"),
    ("3600", "3000"),
    ("4100", "4000"),
    ("4150", "4000"),
    ("4200", "4000"),
    ("5100", "5000"),
    ("5200", "5000"),
    ("5210", "5200"),
    ("5220", "5200"),
    ("5230", "5200"),
    ("5240", "5200"),
    ("5250", "5200"),
    ("5300", "5000"),
    ("5400", "5000"),
)

DEFAULT_EXPENSE_CATEGORY_MAPS: tuple[tuple[str, str], ...] = (
    ("إيجار", "5210"),
    ("كهرباء", "5220"),
    ("ماء", "5220"),
    ("كهرباء وماء", "5220"),
    ("نقل", "5230"),
    ("توصيل", "5230"),
    ("صيانة", "5240"),
    ("مشتريات", "5250"),
    ("مستلزمات", "5250"),
    ("تشغيل", "5200"),
    ("أخرى", "5200"),
)

_WALLET_MAP: tuple[tuple[str, str], ...] = (
    (MAIN_TREASURY_CASH_PM_NAME, "1110"),
    (MAIN_TREASURY_BANK_PM_NAME, "1120"),
    (SUPPLIER_CREDIT_PM_NAME, "2100"),
    (OWNER_EQUITY_PM_NAME, "3100"),
)

_ACCOUNT_TYPE_LABELS: dict[GlAccountType, str] = {
    GlAccountType.ASSET: "أصول",
    GlAccountType.LIABILITY: "خصوم",
    GlAccountType.EQUITY: "حقوق ملكية",
    GlAccountType.REVENUE: "إيرادات",
    GlAccountType.EXPENSE: "مصروفات",
}


def account_type_label(account_type: GlAccountType) -> str:
    return _ACCOUNT_TYPE_LABELS.get(account_type, account_type.value)


def ensure_default_chart_of_accounts(db: Session) -> None:
    """ينشئ دليل الحسابات الافتراضي إن كان فارغاً."""
    from modules.gl.dashboard import ensure_gl_dashboard_column, ensure_gl_dashboard_defaults

    ensure_gl_dashboard_column(db)
    if db.scalar(select(GlAccount.id).limit(1)) is not None:
        ensure_expense_sub_accounts(db)
        ensure_asset_sub_accounts(db)
        ensure_default_expense_category_maps(db)
        ensure_default_operational_role_maps(db)
        _ensure_wallet_maps(db)
        ensure_hotel_wallet_gl_maps(db)
        ensure_hotel_revenue_account(db)
        _run_hotel_gl_revenue_migration(db)
        from modules.gl.hotel_chart import ensure_purchase_custody_gl_accounts

        ensure_purchase_custody_gl_accounts(db)
        from modules.gl.purchase_custody_wallets import ensure_purchase_custody_wallets

        ensure_purchase_custody_wallets(db)
        ensure_chart_hierarchy(db)
        ensure_gl_account_domains(db)
        ensure_gl_dashboard_defaults(db)
        ensure_closing_accounts(db)
        return
    for sort_idx, (code, name_ar, acc_type, is_system) in enumerate(DEFAULT_ACCOUNTS):
        db.add(
            GlAccount(
                code=code,
                name_ar=name_ar,
                account_type=acc_type,
                is_system=is_system,
                sort_order=sort_idx * 10,
                business_domain=account_domain_for_code(code),
            )
        )
    db.flush()
    _ensure_wallet_maps(db)
    ensure_default_expense_category_maps(db)
    ensure_default_operational_role_maps(db)
    ensure_hotel_wallet_gl_maps(db)
    ensure_hotel_revenue_account(db)
    _run_hotel_gl_revenue_migration(db)
    from modules.gl.hotel_chart import ensure_purchase_custody_gl_accounts

    ensure_purchase_custody_gl_accounts(db)
    from modules.gl.purchase_custody_wallets import ensure_purchase_custody_wallets

    ensure_purchase_custody_wallets(db)
    ensure_chart_hierarchy(db)
    ensure_gl_account_domains(db)
    ensure_gl_dashboard_defaults(db)
    ensure_closing_accounts(db)


def ensure_chart_hierarchy(db: Session) -> None:
    """يربط الحسابات الفرعية بآبائها — للعرض التجميعي دون تكرار في التقارير."""
    code_to_id = {row.code: row.id for row in db.scalars(select(GlAccount)).all()}
    for child_code, parent_code in CHART_PARENT_LINKS:
        child_id = code_to_id.get(child_code)
        parent_id = code_to_id.get(parent_code)
        if child_id is None or parent_id is None:
            continue
        child = db.get(GlAccount, int(child_id))
        if child is None or child.parent_id is not None:
            continue
        child.parent_id = int(parent_id)
    db.flush()


def ensure_asset_sub_accounts(db: Session) -> None:
    extra = (
        ("1410", "أصول ثابتة", GlAccountType.ASSET),
        ("1420", "مجمع إهلاك الأصول", GlAccountType.ASSET),
        ("5400", "مصروف إهلاك", GlAccountType.EXPENSE),
    )
    max_sort = db.scalar(select(func.max(GlAccount.sort_order))) or 0
    for code, name_ar, acc_type in extra:
        if db.scalar(select(GlAccount.id).where(GlAccount.code == code)):
            continue
        max_sort += 10
        db.add(
            GlAccount(
                code=code,
                name_ar=name_ar,
                account_type=acc_type,
                is_system=True,
                is_active=True,
                sort_order=int(max_sort),
            )
        )
    db.flush()


def ensure_default_operational_role_maps(db: Session) -> None:
    from sqlalchemy import inspect

    bind = db.get_bind()
    if bind is None:
        return
    if "gl_operational_role_maps" not in inspect(bind).get_table_names():
        return
    code_to_id = {row.code: row.id for row in db.scalars(select(GlAccount)).all()}
    for role_key, code, _label in OPERATIONAL_ROLES:
        gl_id = code_to_id.get(code)
        if gl_id is None:
            continue
        existing = db.scalar(
            select(GlOperationalRoleMap).where(GlOperationalRoleMap.role_key == role_key)
        )
        if existing is None:
            db.add(GlOperationalRoleMap(role_key=role_key, gl_account_id=int(gl_id)))
    db.flush()


def ensure_expense_sub_accounts(db: Session) -> None:
    """يضيف حسابات مصروفات فرعية إن لم تكن موجودة (ترقية قواعد قديمة)."""
    extra = (
        ("5210", "إيجار", GlAccountType.EXPENSE),
        ("5220", "كهرباء وماء", GlAccountType.EXPENSE),
        ("5230", "نقل وتوصيل", GlAccountType.EXPENSE),
        ("5240", "صيانة", GlAccountType.EXPENSE),
        ("5250", "مستلزمات ومشتريات", GlAccountType.EXPENSE),
    )
    max_sort = db.scalar(select(func.max(GlAccount.sort_order))) or 0
    for code, name_ar, acc_type in extra:
        if db.scalar(select(GlAccount.id).where(GlAccount.code == code)):
            continue
        max_sort += 10
        db.add(
            GlAccount(
                code=code,
                name_ar=name_ar,
                account_type=acc_type,
                is_system=True,
                is_active=True,
                sort_order=int(max_sort),
            )
        )
    db.flush()


def ensure_default_expense_category_maps(db: Session) -> None:
    from sqlalchemy import inspect

    bind = db.get_bind()
    if bind is None:
        return
    if "gl_expense_category_maps" not in inspect(bind).get_table_names():
        return
    code_to_id = {row.code: row.id for row in db.scalars(select(GlAccount)).all()}
    for label, code in DEFAULT_EXPENSE_CATEGORY_MAPS:
        gl_id = code_to_id.get(code)
        if gl_id is None:
            continue
        existing = db.scalar(
            select(GlExpenseCategoryMap).where(GlExpenseCategoryMap.category_label == label)
        )
        if existing is None:
            db.add(GlExpenseCategoryMap(category_label=label, gl_account_id=int(gl_id)))
    db.flush()


def _ensure_wallet_maps(db: Session) -> None:
    code_to_id = {
        row.code: row.id
        for row in db.scalars(select(GlAccount)).all()
    }
    for pm_name, gl_code in _WALLET_MAP:
        gl_id = code_to_id.get(gl_code)
        if gl_id is None:
            continue
        pm = db.scalar(select(PaymentMethod).where(PaymentMethod.name_ar == pm_name))
        if pm is None:
            continue
        existing = db.scalar(
            select(GlPaymentMethodMap).where(
                GlPaymentMethodMap.payment_method_id == pm.id
            )
        )
        if existing is None:
            db.add(
                GlPaymentMethodMap(
                    payment_method_id=pm.id,
                    gl_account_id=gl_id,
                )
            )
    db.flush()


def ensure_hotel_wallet_gl_maps(db: Session) -> None:
    """ينشئ حسابات GL لخزن الفندق ويربطها بالمحافظ الفندقية."""
    code_to_acc = {row.code: row for row in db.scalars(select(GlAccount)).all()}
    parent_cash = code_to_acc.get("1100")
    parent_bank = code_to_acc.get("1100")
    for pm_name, code, label, acc_type in HOTEL_WALLET_ACCOUNTS:
        acc = code_to_acc.get(code)
        if acc is None:
            acc = GlAccount(
                code=code,
                name_ar=label,
                account_type=acc_type,
                parent_id=(parent_cash.id if code.startswith("111") else parent_bank.id)
                if (parent_cash or parent_bank)
                else None,
                is_system=True,
                is_active=True,
                show_on_dashboard=True,
                sort_order=115 if code.startswith("111") else 125,
                business_domain=account_domain_for_code(code),
            )
            db.add(acc)
            db.flush()
            code_to_acc[code] = acc
        else:
            acc.name_ar = label
            acc.account_type = acc_type
            acc.is_active = True
            acc.show_on_dashboard = True
            acc.business_domain = account_domain_for_code(code)
            if acc.parent_id is None and (parent_cash or parent_bank):
                acc.parent_id = (
                    parent_cash.id if code.startswith("111") else parent_bank.id
                )

        pm = db.scalar(select(PaymentMethod).where(PaymentMethod.name_ar == pm_name))
        if pm is None:
            continue
        existing = db.scalar(
            select(GlPaymentMethodMap).where(
                GlPaymentMethodMap.payment_method_id == pm.id
            )
        )
        if existing is None:
            db.add(
                GlPaymentMethodMap(
                    payment_method_id=pm.id,
                    gl_account_id=acc.id,
                )
            )
        else:
            existing.gl_account_id = acc.id
    db.flush()


def ensure_hotel_revenue_account(db: Session) -> None:
    """حساب إيراد الإقامة 4150 — مجال الفندق."""
    parent = db.scalar(select(GlAccount).where(GlAccount.code == "4000"))
    acc = db.scalar(select(GlAccount).where(GlAccount.code == "4150"))
    if acc is None:
        db.add(
            GlAccount(
                code="4150",
                name_ar="إيرادات الإقامة (فندق)",
                account_type=GlAccountType.REVENUE,
                parent_id=parent.id if parent else None,
                is_system=True,
                is_active=True,
                sort_order=415,
                business_domain=account_domain_for_code("4150"),
            )
        )
    else:
        acc.name_ar = "إيرادات الإقامة (فندق)"
        acc.account_type = GlAccountType.REVENUE
        acc.is_active = True
        acc.business_domain = account_domain_for_code("4150")
        if acc.parent_id is None and parent is not None:
            acc.parent_id = parent.id
    db.flush()


def _run_hotel_gl_revenue_migration(db: Session) -> None:
    from modules.gl.hotel_revenue_migration import migrate_hotel_gl_to_revenue_account

    migrate_hotel_gl_to_revenue_account(db)
    db.flush()


_CLOSING_ACCOUNTS: tuple[tuple[str, str, GlAccountType, str], ...] = (
    ("3500", "ملخص الدخل", GlAccountType.EQUITY, "shared"),
    ("3600", "الأرباح المحتجزة", GlAccountType.EQUITY, "shared"),
)


def ensure_closing_accounts(db: Session) -> None:
    """يضمن وجود حسابات إقفال السنة المالية (ملخص الدخل والأرباح المحتجزة)."""
    code_to_id = {row.code: row.id for row in db.scalars(select(GlAccount)).all()}
    parent_id = code_to_id.get("3000")
    for code, name_ar, acc_type, domain in _CLOSING_ACCOUNTS:
        if code in code_to_id:
            continue
        acc = GlAccount(
            code=code,
            name_ar=name_ar,
            account_type=acc_type,
            is_system=True,
            is_active=True,
            sort_order=2950 if code == "3500" else 2960,
            business_domain=domain,
            parent_id=parent_id,
        )
        db.add(acc)
        db.flush()


def ensure_default_fiscal_year(db: Session) -> FiscalYear | None:
    """ينشئ سنة مالية افتراضية للسنة الحالية إن لم تكن هناك سنوات."""
    if db.scalar(select(FiscalYear.id).limit(1)) is not None:
        return None
    from datetime import date

    today = date.today()
    start = today.replace(month=1, day=1)
    end = today.replace(month=12, day=31)
    name = f"السنة المالية {start.year}"
    fy = FiscalYear(name=name, start_date=start, end_date=end, is_current=True)
    db.add(fy)
    db.flush()
    return fy
