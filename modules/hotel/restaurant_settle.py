"""تسوية مالية فندق → مطعم لفواتير الغرفة (وجبات النزيل)."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.payments.models import (
    ROOM_SETTLE_CLEARING_PM_NAME,
    PaymentMethod,
    PaymentMethodDomain,
    PaymentMethodKind,
)
from modules.payments.service import (
    PaymentsError,
    ensure_hotel_treasury_payment_methods,
    payment_method_is_strict_domain,
    record_manual_transfer,
)
from modules.payments.shift_handoff_service import is_main_treasury_payment_method


def ensure_room_settle_clearing_pm(db: Session) -> PaymentMethod:
    """حساب مقاصة: يُستخدم فقط لتسجيل سداد فاتورة الغرفة بعد تحويل النقد."""
    row = db.scalar(
        select(PaymentMethod).where(PaymentMethod.name_ar == ROOM_SETTLE_CLEARING_PM_NAME)
    )
    if row is None:
        row = PaymentMethod(
            name_ar=ROOM_SETTLE_CLEARING_PM_NAME,
            kind=PaymentMethodKind.OTHER,
            is_active=True,
            sort_order=95,
            can_receive=True,
            can_pay=False,
            can_fund=False,
            is_system=True,
            show_on_dashboard=False,
            business_domain=PaymentMethodDomain.SHARED,
        )
        db.add(row)
        db.flush()
    else:
        row.is_active = True
        row.is_system = True
        row.can_receive = True
        row.can_pay = False
        row.can_fund = False
        row.show_on_dashboard = False
        row.kind = PaymentMethodKind.OTHER
        row.business_domain = PaymentMethodDomain.SHARED
        db.flush()
    return row


def hotel_settle_source_pm(db: Session, *, kind: PaymentMethodKind | None = None) -> PaymentMethod:
    """خزينة الفندق الافتراضية للتسوية (كاش عادةً)."""
    hotels = ensure_hotel_treasury_payment_methods(db)
    key = "BANK" if kind == PaymentMethodKind.BANK else "CASH"
    return hotels[key]


def restaurant_settle_target_pm(
    db: Session, *, kind: PaymentMethodKind | None = None
) -> PaymentMethod:
    """خزينة المطعم الرئيسية المستهدفة بالتحويل."""
    from modules.payments.shift_handoff_service import ensure_main_treasury_payment_methods

    mains = ensure_main_treasury_payment_methods(db)
    key = "BANK" if kind == PaymentMethodKind.BANK else "CASH"
    return mains[key]


def is_hotel_settle_wallet(pm: PaymentMethod | None) -> bool:
    if pm is None:
        return False
    return payment_method_is_strict_domain(pm, PaymentMethodDomain.HOTEL)


def is_restaurant_settle_wallet(pm: PaymentMethod | None) -> bool:
    if pm is None:
        return False
    if is_main_treasury_payment_method(pm) and payment_method_is_strict_domain(
        pm, PaymentMethodDomain.RESTAURANT
    ):
        return True
    return payment_method_is_strict_domain(pm, PaymentMethodDomain.RESTAURANT)


def pick_hotel_treasury_with_balance(
    db: Session, amount: Decimal
) -> PaymentMethod:
    """يختار خزينة فندق (كاش ثم مصرف) لديها رصيد كافٍ للتحويل."""
    from modules.payments.service import method_current_balance

    need = Decimal(str(amount or 0)).quantize(Decimal("0.001"))
    hotels = ensure_hotel_treasury_payment_methods(db)
    for key in ("CASH", "BANK"):
        pm = hotels[key]
        bal = method_current_balance(db, int(pm.id))
        if bal + Decimal("0.0005") >= need:
            return pm
    cash_bal = method_current_balance(db, int(hotels["CASH"].id))
    bank_bal = method_current_balance(db, int(hotels["BANK"].id))
    raise PaymentsError(
        f"رصيد خزينة الفندق غير كافٍ لتحويل {need} د.ل إلى المطعم "
        f"(كاش: {cash_bal} · مصرف: {bank_bal})."
    )


def list_restaurant_settle_targets(db: Session) -> list[PaymentMethod]:
    """خزائن المطعم التي يمكن التحويل إليها عند التسوية."""
    from modules.payments.shift_handoff_service import ensure_main_treasury_payment_methods
    from modules.payments.service import list_payment_methods

    ensure_main_treasury_payment_methods(db)
    rows = [
        m
        for m in list_payment_methods(db, only_active=True)
        if is_restaurant_settle_wallet(m)
    ]
    kind_rank = {"cash": 0, "bank": 1}

    def _key(m: PaymentMethod):
        kind = str(getattr(m.kind, "value", m.kind) or "").strip().lower()
        return (kind_rank.get(kind, 9), m.sort_order, m.id)

    rows.sort(key=_key)
    return rows


def transfer_hotel_to_restaurant_for_meal(
    db: Session,
    *,
    amount: Decimal,
    from_payment_method_id: int,
    user_id: int | None,
    sale_id: int | None = None,
    room_id: int | None = None,
    to_payment_method_id: int | None = None,
) -> object | None:
    """ينقل المبلغ من محفظة الفندق إلى خزينة المطعم.

    - يمكن تحديد الوجهة صراحةً (كاش/مصرف مطعم).
    - إن لم تُحدَّد الوجهة: تُختار خزينة المطعم بنفس نوع المصدر.
    """
    amt = Decimal(str(amount or 0)).quantize(Decimal("0.001"))
    if amt <= Decimal("0.0005"):
        return None
    from_pm = db.get(PaymentMethod, int(from_payment_method_id))
    if from_pm is None:
        raise PaymentsError("محفظة مصدر التسوية غير موجودة.")
    if is_restaurant_settle_wallet(from_pm):
        return None
    if not is_hotel_settle_wallet(from_pm):
        raise PaymentsError("مصدر التحويل يجب أن يكون خزينة فندق (كاش أو مصرف).")

    if to_payment_method_id is not None:
        to_pm = db.get(PaymentMethod, int(to_payment_method_id))
        if to_pm is None or not is_restaurant_settle_wallet(to_pm):
            raise PaymentsError("وجهة التحويل يجب أن تكون خزينة مطعم (كاش أو مصرف).")
    else:
        kind = (
            from_pm.kind
            if from_pm.kind in (PaymentMethodKind.CASH, PaymentMethodKind.BANK)
            else PaymentMethodKind.CASH
        )
        to_pm = restaurant_settle_target_pm(db, kind=kind)

    if int(to_pm.id) == int(from_pm.id):
        return None
    bits = ["تسوية وجبات فندق→مطعم"]
    if sale_id:
        bits.append(f"فاتورة #{sale_id}")
    if room_id:
        bits.append(f"غرفة #{room_id}")
    return record_manual_transfer(
        db,
        from_payment_method_id=int(from_pm.id),
        to_payment_method_id=int(to_pm.id),
        amount=amt,
        user_id=user_id,
        note=" · ".join(bits),
    )
