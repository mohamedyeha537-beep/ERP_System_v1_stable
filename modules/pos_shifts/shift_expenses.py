"""مصروفات الجلسة — تسجيل من POS وربطها بالمتوقع في الدرج."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, joinedload

from modules.payments.models import PaymentMethod, PaymentMethodKind, Purchase, PurchaseKind
from modules.payments.service import PaymentsError, record_expense
from modules.payments.shift_handoff_service import list_cashier_wallet_methods
from modules.pos_shifts.expense_categories import (
    active_categories_for_pos,
    category_expense_label,
    is_advance_category,
    known_expense_labels,
)
from modules.pos_shifts.models import PosShift, PosShiftStatus
from modules.pos_shifts.service import PosShiftError


def _shift_expense_scope(db: Session, shift_id: int):
    """مصروفات الجلسة المرتبطة مباشرة، مع التقاط سجلات قديمة بلا pos_shift_id."""
    direct = Purchase.pos_shift_id == shift_id
    sh = db.get(PosShift, shift_id)
    if sh is None:
        return direct
    fallback_filters = [
        Purchase.pos_shift_id.is_(None),
        Purchase.created_by_id == sh.user_id,
        or_(
            Purchase.expense_category.like("مصروف جلسة%"),
            Purchase.expense_category.in_(tuple(known_expense_labels(db) or ["سلف موظفين"])),
        ),
    ]
    if sh.opened_at is not None:
        fallback_filters.append(Purchase.created_at >= sh.opened_at)
    fallback_filters.append(Purchase.created_at <= (sh.closed_at or datetime.now(timezone.utc)))
    return or_(direct, and_(*fallback_filters))


def _sum_shift_expenses(db: Session, shift_id: int, pm_kind: PaymentMethodKind) -> Decimal:
    raw = db.execute(
        select(func.coalesce(func.sum(Purchase.amount), 0))
        .select_from(Purchase)
        .join(PaymentMethod, PaymentMethod.id == Purchase.payment_method_id)
        .where(
            _shift_expense_scope(db, shift_id),
            Purchase.kind == PurchaseKind.EXPENSE,
            PaymentMethod.kind == pm_kind,
        )
    ).scalar_one()
    return Decimal(str(raw or 0)).quantize(Decimal("0.001"))


def sum_shift_cash_expenses(db: Session, shift_id: int) -> Decimal:
    return _sum_shift_expenses(db, shift_id, PaymentMethodKind.CASH)


def sum_shift_bank_expenses(db: Session, shift_id: int) -> Decimal:
    return _sum_shift_expenses(db, shift_id, PaymentMethodKind.BANK)


def list_shift_expenses(db: Session, shift_id: int, *, limit: int = 50) -> list[Purchase]:
    return list(
        db.scalars(
            select(Purchase)
            .options(joinedload(Purchase.method))
            .where(
                _shift_expense_scope(db, shift_id),
                Purchase.kind == PurchaseKind.EXPENSE,
            )
            .order_by(Purchase.id.desc())
            .limit(limit)
        )
        .unique()
        .all()
    )


def default_shift_expense_wallet(
    db: Session, kind: PaymentMethodKind, *, user=None
) -> PaymentMethod | None:
    wallets = list_cashier_wallet_methods(db, kind, user=user)
    return wallets[0] if wallets else None


def shift_expense_wallets(db: Session, *, user=None) -> dict[str, list[PaymentMethod]]:
    return {
        "cash": list_cashier_wallet_methods(db, PaymentMethodKind.CASH, user=user),
        "bank": list_cashier_wallet_methods(db, PaymentMethodKind.BANK, user=user),
    }


def shift_expense_wallet_options(db: Session, *, user=None) -> list[dict]:
    opts: list[dict] = []
    for pm in list_cashier_wallet_methods(db, PaymentMethodKind.CASH, user=user):
        opts.append(
            {
                "id": pm.id,
                "kind": "cash",
                "kind_label": "كاش",
                "name_ar": pm.name_ar,
            }
        )
    for pm in list_cashier_wallet_methods(db, PaymentMethodKind.BANK, user=user):
        opts.append(
            {
                "id": pm.id,
                "kind": "bank",
                "kind_label": "مصرف",
                "name_ar": pm.name_ar,
            }
        )
    return opts


def _allowed_cashier_wallet_ids(db: Session, *, user=None) -> set[int]:
    ids: set[int] = set()
    for kind in (PaymentMethodKind.CASH, PaymentMethodKind.BANK):
        for pm in list_cashier_wallet_methods(db, kind, user=user):
            ids.add(int(pm.id))
    return ids


def shift_expense_employees(db: Session) -> list[dict]:
    """موظفون نشطون لاختيار المستفيد من السلفة (مطعم + مشترك)."""
    from modules.hr.models import Employee, EmployeeStatus
    from modules.platform.business_domain import BusinessDomain
    from sqlalchemy import select

    rows = list(
        db.scalars(
            select(Employee)
            .where(
                Employee.status == EmployeeStatus.ACTIVE,
                Employee.business_domain.in_(
                    (
                        BusinessDomain.RESTAURANT.value,
                        BusinessDomain.SHARED.value,
                    )
                ),
            )
            .order_by(Employee.full_name_ar)
        ).all()
    )
    if not rows:
        from modules.hr.service import list_employees

        rows = list_employees(db, only_active=True)
    return [
        {"id": int(e.id), "name": (e.full_name_ar or "").strip() or f"موظف #{e.id}"}
        for e in rows
    ]


def shift_expense_ui_context(
    db: Session, shift_id: int | None, *, user=None
) -> dict:
    cats = active_categories_for_pos(db)
    wallets = shift_expense_wallet_options(db, user=user)
    single = wallets[0] if len(wallets) == 1 else None
    base = {
        "shift_expense_categories": cats,
        "shift_expense_wallet_options": wallets,
        "shift_expense_single_wallet": single,
        "shift_expense_employees": shift_expense_employees(db),
        "shift_expense_advance_keys": [
            k for k, lab in cats if is_advance_category(db, k) or "سلف" in lab
        ],
    }
    if shift_id is None:
        return {
            **base,
            "shift_expenses": [],
            "shift_expense_total_cash": Decimal("0"),
            "shift_expense_total_bank": Decimal("0"),
        }
    return {
        **base,
        "shift_expenses": list_shift_expenses(db, shift_id),
        "shift_expense_total_cash": sum_shift_cash_expenses(db, shift_id),
        "shift_expense_total_bank": sum_shift_bank_expenses(db, shift_id),
    }


def record_shift_expense(
    db: Session,
    *,
    shift_id: int,
    user_id: int,
    amount: Decimal,
    category_key: str,
    note: str,
    payment_method_id: int | None = None,
    employee_id: int | None = None,
) -> Purchase:
    sh = db.get(PosShift, shift_id)
    if sh is None:
        raise PosShiftError("الجلسة غير موجودة.")
    if sh.status != PosShiftStatus.OPEN:
        raise PosShiftError("لا يمكن تسجيل مصروف إلا أثناء جلسة مفتوحة.")

    note = (note or "").strip()
    if not note:
        raise PosShiftError("أدخل وصفاً للمصروف.")

    from modules.authz.models import User
    from modules.pos_shifts.expense_categories import ensure_category_gl_maps

    ensure_category_gl_maps(db)

    actor = db.get(User, int(user_id)) if user_id else None
    allowed_pms = _allowed_cashier_wallet_ids(db, user=actor)
    pm: PaymentMethod | None = None
    if payment_method_id:
        pm = db.get(PaymentMethod, payment_method_id)
        if (
            pm is None
            or not pm.is_active
            or not pm.can_pay
            or int(pm.id) not in allowed_pms
        ):
            raise PosShiftError(
                "محفظة الصرف غير صالحة — اختر خزينة/محفظة مطعم للكاشير فقط."
            )
    else:
        pm = default_shift_expense_wallet(
            db, PaymentMethodKind.CASH, user=actor
        ) or default_shift_expense_wallet(db, PaymentMethodKind.BANK, user=actor)
    if pm is None:
        raise PosShiftError("لا توجد محفظة كاشير مطعم للصرف — راجع إعدادات المحافظ.")

    # سلفة موظف → صفحة السلف + خصم من الراتب (ليس قيداً عاماً فقط)
    if is_advance_category(db, category_key):
        if not employee_id or int(employee_id) <= 0:
            raise PosShiftError("اختر الموظف المستفيد من السلفة.")
        try:
            from modules.hr.service import HRError, grant_advance

            adv = grant_advance(
                db,
                employee_id=int(employee_id),
                amount=amount,
                payment_method_id=pm.id,
                record_as_expense=True,
                notes=note[:240],
                given_by_id=user_id,
                pos_shift_id=shift_id,
                allow_any_pay_wallet=True,
            )
        except HRError as exc:
            raise PosShiftError(str(exc)) from exc
        purchase = db.get(Purchase, adv.purchase_id) if adv.purchase_id else None
        if purchase is None:
            raise PosShiftError("تعذّر ربط السلفة بقيد الصرف.")
        return purchase

    expense_category = category_expense_label(db, category_key)

    try:
        return record_expense(
            db,
            payment_method_id=pm.id,
            amount=amount,
            expense_category=expense_category,
            supplier=None,
            note=note[:240],
            user_id=user_id,
            pos_shift_id=shift_id,
            allow_any_pay_wallet=True,
        )
    except PaymentsError as exc:
        raise PosShiftError(str(exc)) from exc
