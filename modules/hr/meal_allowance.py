from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from modules.hr.models import (
    Employee,
    EmployeeMealRedemption,
    EmployeeMealWallet,
    EmployeeStatus,
)


class EmployeeMealError(Exception):
    pass


STAFF_MEAL_PAYMENT_METHOD_NAME = "بدل وجبات الموظفين"
STAFF_MEAL_EXPENSE_CATEGORY = "وجبات موظفين"
STAFF_MEAL_EXPENSE_REF_PREFIX = "employee-meal-sale-"


def current_period_label(today: date | None = None) -> str:
    d = today or date.today()
    return f"{d.year:04d}-{d.month:02d}"


def parse_money(raw: str | Decimal | int | float | None) -> Decimal:
    try:
        val = Decimal(str(raw or "0").strip() or "0").quantize(Decimal("0.001"))
    except (InvalidOperation, ValueError):
        raise EmployeeMealError("قيمة بدل الوجبات غير صالحة.") from None
    if val < 0:
        raise EmployeeMealError("لا يمكن إدخال قيمة سالبة.")
    return val


def active_employees(db: Session) -> list[Employee]:
    return list(
        db.scalars(
            select(Employee)
            .where(Employee.status == EmployeeStatus.ACTIVE)
            .order_by(Employee.full_name_ar)
        ).all()
    )


def ensure_meal_wallet(
    db: Session,
    *,
    employee_id: int,
    period_label: str,
    allowance_amount: Decimal,
    notes: str = "",
    is_active: bool = True,
) -> EmployeeMealWallet:
    emp = db.get(Employee, employee_id)
    if emp is None or emp.status != EmployeeStatus.ACTIVE:
        raise EmployeeMealError("الموظف غير موجود أو غير نشط.")
    period = (period_label or "").strip()
    if len(period) != 7 or period[4] != "-":
        raise EmployeeMealError("الفترة يجب أن تكون بصيغة YYYY-MM.")
    row = db.scalar(
        select(EmployeeMealWallet).where(
            EmployeeMealWallet.employee_id == employee_id,
            EmployeeMealWallet.period_label == period,
        )
    )
    if row is None:
        row = EmployeeMealWallet(employee_id=employee_id, period_label=period)
        db.add(row)
    row.allowance_amount = allowance_amount
    row.notes = (notes or "").strip() or None
    row.is_active = is_active
    db.flush()
    return row


def wallet_spent(db: Session, wallet_id: int) -> Decimal:
    total = db.scalar(
        select(func.coalesce(func.sum(EmployeeMealRedemption.covered_amount), 0)).where(
            EmployeeMealRedemption.wallet_id == wallet_id
        )
    )
    return Decimal(str(total or 0)).quantize(Decimal("0.001"))


def wallet_balance(db: Session, wallet: EmployeeMealWallet) -> Decimal:
    return (Decimal(str(wallet.allowance_amount or 0)) - wallet_spent(db, wallet.id)).quantize(
        Decimal("0.001")
    )


def get_employee_wallet(
    db: Session, employee_id: int, period_label: str | None = None
) -> EmployeeMealWallet | None:
    period = period_label or current_period_label()
    return db.scalar(
        select(EmployeeMealWallet).where(
            EmployeeMealWallet.employee_id == employee_id,
            EmployeeMealWallet.period_label == period,
            EmployeeMealWallet.is_active.is_(True),
        )
    )


@dataclass(frozen=True)
class EmployeeMealCheckoutOption:
    employee: Employee
    wallet: EmployeeMealWallet | None
    allowance: Decimal
    spent: Decimal
    balance: Decimal


def checkout_employee_options(
    db: Session, period_label: str | None = None
) -> list[EmployeeMealCheckoutOption]:
    period = period_label or current_period_label()
    out: list[EmployeeMealCheckoutOption] = []
    for emp in active_employees(db):
        wallet = get_employee_wallet(db, emp.id, period)
        allowance = Decimal(str(wallet.allowance_amount or 0)).quantize(Decimal("0.001")) if wallet else Decimal("0")
        spent = wallet_spent(db, wallet.id) if wallet else Decimal("0")
        balance = (allowance - spent).quantize(Decimal("0.001"))
        out.append(
            EmployeeMealCheckoutOption(
                employee=emp,
                wallet=wallet,
                allowance=allowance,
                spent=spent,
                balance=balance,
            )
        )
    return out


def consume_wallet_for_sale(
    db: Session,
    *,
    employee_id: int,
    sale_id: int,
    sale_total: Decimal,
    requested_cover: Decimal,
    created_by_id: int | None,
) -> tuple[EmployeeMealRedemption | None, Decimal, Decimal]:
    """Returns (redemption, covered_amount, personal_amount)."""
    total = Decimal(str(sale_total or 0)).quantize(Decimal("0.001"))
    if total <= 0:
        return None, Decimal("0"), Decimal("0")
    wallet = get_employee_wallet(db, employee_id)
    if wallet is None:
        raise EmployeeMealError("لا يوجد رصيد وجبات نشط لهذا الموظف في الشهر الحالي.")
    available = wallet_balance(db, wallet)
    cover = min(total, available, Decimal(str(requested_cover or total))).quantize(Decimal("0.001"))
    if cover < 0:
        cover = Decimal("0")
    personal = (total - cover).quantize(Decimal("0.001"))
    if cover <= 0:
        return None, Decimal("0"), personal
    existing = db.scalar(
        select(EmployeeMealRedemption).where(EmployeeMealRedemption.sale_id == sale_id)
    )
    if existing is not None:
        raise EmployeeMealError("تم تسجيل وجبة موظف لهذه الفاتورة مسبقاً.")
    row = EmployeeMealRedemption(
        wallet_id=wallet.id,
        employee_id=employee_id,
        sale_id=sale_id,
        sale_total=total,
        covered_amount=cover,
        personal_amount=personal,
        created_by_id=created_by_id,
        note=f"استهلاك وجبة من رصيد {wallet.period_label}",
    )
    db.add(row)
    db.flush()
    return row, cover, personal


def ensure_staff_meal_payment_method(db: Session):
    from modules.payments.models import PaymentMethod, PaymentMethodDomain, PaymentMethodKind

    row = db.scalar(
        select(PaymentMethod).where(PaymentMethod.name_ar == STAFF_MEAL_PAYMENT_METHOD_NAME)
    )
    if row is None:
        row = PaymentMethod(
            name_ar=STAFF_MEAL_PAYMENT_METHOD_NAME,
            kind=PaymentMethodKind.OTHER,
            is_active=True,
            sort_order=850,
            can_receive=True,
            can_pay=False,
            can_fund=False,
            is_system=True,
            show_on_dashboard=False,
            business_domain=PaymentMethodDomain.RESTAURANT,
        )
        db.add(row)
    row.kind = PaymentMethodKind.OTHER
    row.is_active = True
    row.can_receive = True
    row.can_pay = False
    row.can_fund = False
    row.is_system = True
    row.show_on_dashboard = False
    row.business_domain = PaymentMethodDomain.RESTAURANT
    db.flush()
    return row


def employee_meal_expense_ref(sale_id: int) -> str:
    return f"{STAFF_MEAL_EXPENSE_REF_PREFIX}{sale_id}"


def ensure_employee_meal_expense_for_sale(
    db: Session,
    *,
    employee: Employee,
    sale_id: int,
    covered_amount: Decimal,
    user_id: int | None,
):
    """يسجّل الجزء الذي تحمله المطعم كمصروف تشغيل بدون خصم خزينة."""
    amt = max(Decimal(str(covered_amount or 0)), Decimal("0")).quantize(Decimal("0.001"))
    if amt <= 0:
        return None
    from modules.payments.models import Purchase, PurchaseKind
    from modules.payments.service import record_accrual_expense
    from modules.gl.posting import post_expense_shadow_safe

    ref = employee_meal_expense_ref(sale_id)
    existing = db.scalar(
        select(Purchase).where(
            Purchase.kind == PurchaseKind.EXPENSE,
            Purchase.supplier_invoice_ref == ref,
        )
    )
    if existing is not None:
        if Decimal(str(existing.amount or 0)).quantize(Decimal("0.001")) != amt:
            existing.amount = amt
            existing.note = (
                f"وجبة موظف #{employee.id} — {employee.full_name_ar} — فاتورة بيع #{sale_id} "
                "(استحقاق داخلي بدون خصم من الخزينة)"
            )
        post_expense_shadow_safe(db, existing)
        return existing
    purchase = record_accrual_expense(
        db,
        amount=amt,
        expense_category=STAFF_MEAL_EXPENSE_CATEGORY,
        supplier=employee.full_name_ar,
        note=(
            f"وجبة موظف #{employee.id} — {employee.full_name_ar} — فاتورة بيع #{sale_id} "
            "(استحقاق داخلي بدون خصم من الخزينة)"
        ),
        user_id=user_id,
        supplier_invoice_ref=ref,
    )
    post_expense_shadow_safe(db, purchase)
    return purchase
