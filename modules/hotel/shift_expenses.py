"""مصروفات وردية الفندق."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, joinedload

from modules.hotel.shift_models import HotelShift, HotelShiftError, HotelShiftStatus
from modules.payments.models import PaymentMethod, PaymentMethodKind, Purchase, PurchaseKind
from modules.payments.service import PaymentsError, record_expense
from modules.platform.business_domain import BusinessDomain


def _known_expense_labels(db: Session) -> list[str]:
    from modules.pos_shifts.expense_categories import known_expense_labels

    return known_expense_labels(db)


def _shift_expense_scope(db: Session, shift_id: int):
    direct = Purchase.hotel_shift_id == shift_id
    sh = db.get(HotelShift, shift_id)
    if sh is None:
        return direct
    labels = _known_expense_labels(db)
    fallback = [
        Purchase.hotel_shift_id.is_(None),
        Purchase.created_by_id == sh.user_id,
        or_(
            Purchase.expense_category.like("مصروف وردية%"),
            Purchase.expense_category.in_(labels) if labels else False,
        ),
    ]
    if sh.opened_at is not None:
        fallback.append(Purchase.created_at >= sh.opened_at)
    fallback.append(Purchase.created_at <= (sh.closed_at or datetime.now(timezone.utc)))
    return or_(direct, and_(*fallback))


def sum_shift_expenses(db: Session, shift_id: int) -> Decimal:
    raw = db.scalar(
        select(func.coalesce(func.sum(Purchase.amount), 0)).where(
            _shift_expense_scope(db, shift_id),
            Purchase.kind == PurchaseKind.EXPENSE,
        )
    )
    return Decimal(str(raw or 0)).quantize(Decimal("0.001"))


def _sum_shift_expenses_by_kind(
    db: Session, shift_id: int, *, cash: bool
) -> Decimal:
    """مصروفات الوردية حسب وسيلة الدفع (كاش أو غير كاش)."""
    rows = list(
        db.scalars(
            select(Purchase)
            .options(joinedload(Purchase.method))
            .where(
                _shift_expense_scope(db, shift_id),
                Purchase.kind == PurchaseKind.EXPENSE,
            )
        )
        .unique()
        .all()
    )
    total = Decimal("0")
    for p in rows:
        kind = getattr(getattr(p, "method", None), "kind", None)
        is_cash = kind == PaymentMethodKind.CASH or (
            kind is not None and str(getattr(kind, "value", kind)).upper() == "CASH"
        )
        if cash and is_cash:
            total += Decimal(str(p.amount or 0))
        elif not cash and not is_cash:
            total += Decimal(str(p.amount or 0))
    return total.quantize(Decimal("0.001"))


def sum_hotel_shift_cash_expenses(db: Session, shift_id: int) -> Decimal:
    return _sum_shift_expenses_by_kind(db, shift_id, cash=True)


def sum_hotel_shift_bank_expenses(db: Session, shift_id: int) -> Decimal:
    return _sum_shift_expenses_by_kind(db, shift_id, cash=False)


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


def _resolve_category_key(db: Session, raw: str) -> str:
    from modules.pos_shifts.expense_categories import load_shift_expense_categories

    raw = (raw or "").strip()
    if not raw:
        raise HotelShiftError("اختر بند المصروف.")
    items = load_shift_expense_categories(db)
    for c in items:
        if c.get("key") == raw or c.get("label") == raw:
            if not c.get("active", True):
                raise HotelShiftError("هذا البند معطّل — راجع بنود المصروف من الإدارة.")
            return str(c["key"])
    leftover = raw
    if leftover.startswith("مصروف وردية"):
        leftover = leftover.split("—", 1)[-1].strip()
    for c in items:
        if c.get("label") == leftover and c.get("active", True):
            return str(c["key"])
    raise HotelShiftError("بند المصروف غير معروف — أضف البند من صفحة بنود مصروف الجلسة.")


def hotel_shift_expense_employees(db: Session) -> list[dict]:
    """موظفون نشطون لاختيار المستفيد من السلفة (فندق + مشترك)."""
    from modules.hr.models import Employee, EmployeeStatus

    rows = list(
        db.scalars(
            select(Employee)
            .where(
                Employee.status == EmployeeStatus.ACTIVE,
                Employee.business_domain.in_(
                    (
                        BusinessDomain.HOTEL.value,
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


def hotel_shift_expense_ui_context(db: Session) -> dict:
    from modules.pos_shifts.expense_categories import (
        active_categories_for_pos,
        advance_category_keys,
        ensure_category_gl_maps,
    )

    ensure_category_gl_maps(db)
    cats = active_categories_for_pos(db)
    return {
        "shift_expense_categories": cats,
        "shift_expense_advance_keys": advance_category_keys(db),
        "shift_expense_employees": hotel_shift_expense_employees(db),
    }


def record_hotel_shift_expense(
    db: Session,
    *,
    shift_id: int,
    amount: Decimal,
    payment_method_id: int,
    category: str,
    note: str | None,
    user_id: int | None,
    employee_id: int | None = None,
) -> Purchase:
    from modules.pos_shifts.expense_categories import (
        category_expense_label,
        ensure_category_gl_maps,
        is_advance_category,
    )

    shift = db.get(HotelShift, shift_id)
    if shift is None or shift.status != HotelShiftStatus.OPEN:
        raise HotelShiftError("لا توجد وردية مفتوحة.")
    ensure_category_gl_maps(db)
    category_key = _resolve_category_key(db, category)
    note_txt = (note or "").strip() or None

    if is_advance_category(db, category_key):
        if not employee_id or int(employee_id) <= 0:
            raise HotelShiftError("اختر الموظف المستفيد من السلفة.")
        try:
            from modules.hr.service import HRError, grant_advance

            adv = grant_advance(
                db,
                employee_id=int(employee_id),
                amount=amount,
                payment_method_id=payment_method_id,
                record_as_expense=True,
                notes=note_txt or None,
                given_by_id=user_id,
                hotel_shift_id=shift_id,
                business_domain=BusinessDomain.HOTEL.value,
                allow_any_pay_wallet=True,
            )
        except HRError as exc:
            raise HotelShiftError(str(exc)) from exc
        purchase = db.get(Purchase, adv.purchase_id) if adv.purchase_id else None
        if purchase is None:
            raise HotelShiftError("تعذّر ربط السلفة بقيد الصرف.")
        return purchase

    label = category_expense_label(db, category_key)
    try:
        purchase = record_expense(
            db,
            amount=amount,
            payment_method_id=payment_method_id,
            expense_category=label,
            supplier=None,
            note=note_txt,
            user_id=user_id,
            business_domain=BusinessDomain.HOTEL.value,
            allow_any_pay_wallet=True,
        )
    except PaymentsError as exc:
        raise HotelShiftError(str(exc)) from exc
    purchase.hotel_shift_id = shift_id
    db.flush()
    return purchase


def list_hotel_expense_methods(db: Session) -> list[PaymentMethod]:
    from modules.payments.service import ensure_hotel_reception_payment_methods

    wallets = ensure_hotel_reception_payment_methods(db)
    return [wallets["CASH"], wallets["BANK"]]
