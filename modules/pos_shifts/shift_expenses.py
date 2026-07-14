"""مصروفات الجلسة — تسجيل من POS وربطها بالمتوقع في الدرج."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, joinedload

from modules.payments.models import PaymentMethod, PaymentMethodKind, Purchase, PurchaseKind
from modules.payments.service import PaymentsError, record_expense
from modules.payments.shift_handoff_service import list_cashier_wallet_methods
from modules.pos_shifts.expense_categories import active_categories_for_pos, category_expense_label
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
        Purchase.expense_category.like("مصروف جلسة%"),
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
    db: Session, kind: PaymentMethodKind
) -> PaymentMethod | None:
    wallets = list_cashier_wallet_methods(db, kind)
    return wallets[0] if wallets else None


def shift_expense_wallets(db: Session) -> dict[str, list[PaymentMethod]]:
    return {
        "cash": list_cashier_wallet_methods(db, PaymentMethodKind.CASH),
        "bank": list_cashier_wallet_methods(db, PaymentMethodKind.BANK),
    }


def shift_expense_wallet_options(db: Session) -> list[dict]:
    opts: list[dict] = []
    for pm in list_cashier_wallet_methods(db, PaymentMethodKind.CASH):
        opts.append(
            {
                "id": pm.id,
                "kind": "cash",
                "kind_label": "كاش",
                "name_ar": pm.name_ar,
            }
        )
    for pm in list_cashier_wallet_methods(db, PaymentMethodKind.BANK):
        opts.append(
            {
                "id": pm.id,
                "kind": "bank",
                "kind_label": "مصرف",
                "name_ar": pm.name_ar,
            }
        )
    return opts


def shift_expense_ui_context(db: Session, shift_id: int | None) -> dict:
    cats = active_categories_for_pos(db)
    wallets = shift_expense_wallet_options(db)
    single = wallets[0] if len(wallets) == 1 else None
    base = {
        "shift_expense_categories": cats,
        "shift_expense_wallet_options": wallets,
        "shift_expense_single_wallet": single,
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
) -> Purchase:
    sh = db.get(PosShift, shift_id)
    if sh is None:
        raise PosShiftError("الجلسة غير موجودة.")
    if sh.status != PosShiftStatus.OPEN:
        raise PosShiftError("لا يمكن تسجيل مصروف إلا أثناء جلسة مفتوحة.")

    note = (note or "").strip()
    if not note:
        raise PosShiftError("أدخل وصفاً للمصروف.")

    pm: PaymentMethod | None = None
    if payment_method_id:
        pm = db.get(PaymentMethod, payment_method_id)
        if pm is None or not pm.is_active or not pm.can_pay:
            raise PosShiftError("محفظة الصرف غير صالحة.")
    else:
        pm = default_shift_expense_wallet(db, PaymentMethodKind.CASH)
    if pm is None:
        raise PosShiftError("لا توجد محفظة كاشير للصرف — راجع إعدادات المحافظ.")

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
