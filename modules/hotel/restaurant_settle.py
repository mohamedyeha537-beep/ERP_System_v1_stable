"""تسوية مالية فندق → مطعم لفواتير الغرفة (وجبات النزيل)."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.payments.models import (
    HOTEL_TREASURY_BANK_PM_NAME,
    MAIN_TREASURY_BANK_PM_NAME,
    ROOM_SETTLE_CLEARING_PM_NAME,
    PaymentMethod,
    PaymentMethodDomain,
    PaymentMethodKind,
)
from modules.payments.service import (
    PaymentsError,
    ensure_hotel_treasury_payment_methods,
    payment_method_is_strict_domain,
    payment_method_kind_matches,
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


_TAWA_MARKERS = ("توا", "تداول", "تداو", "tawa", "tadawul", "tadao")


def _gl_info_map(db: Session | None) -> dict[int, dict[str, str]]:
    if db is None:
        return {}
    try:
        from modules.gl.wallet_labels import wallet_gl_info_map

        return wallet_gl_info_map(db)
    except Exception:
        return {}


def _wallet_search_text(pm: PaymentMethod, gl_info: dict[int, dict[str, str]] | None) -> str:
    parts = [str(getattr(pm, "name_ar", "") or "")]
    info = (gl_info or {}).get(int(getattr(pm, "id", 0) or 0)) or {}
    parts.append(str(info.get("name") or ""))
    parts.append(str(info.get("label") or ""))
    return " ".join(parts)


def _is_tawa_text(text: str) -> bool:
    name_l = (text or "").lower()
    return any(tok in text or tok in name_l for tok in _TAWA_MARKERS)


def settle_wallet_preference_key(
    pm: PaymentMethod, *, gl_info: dict[int, dict[str, str]] | None = None
) -> tuple:
    """الخزينة الرئيسية للفندق توا / مصرف الفندق أولاً — استقبال الكاش آخراً."""
    text = _wallet_search_text(pm, gl_info)
    name = (pm.name_ar or "").strip()
    tawa = _is_tawa_text(text)
    bank = payment_method_kind_matches(pm, PaymentMethodKind.BANK)
    hotel_main_tawa = tawa and (
        "الخزينة الرئيسية" in text or ("رئيس" in text and "فندق" in text)
    )
    hotel_bank = name == HOTEL_TREASURY_BANK_PM_NAME or (
        ("خزينة" in text and "فندق" in text and bank) or "خزينة الفندق" in text
    )
    main_bank = name == MAIN_TREASURY_BANK_PM_NAME or (
        "الخزينة الرئيسية" in text and bank
    )
    reception = "استقبال" in text
    return (
        0 if hotel_main_tawa else 1,
        0 if tawa else 1,
        0 if hotel_bank else 1,
        0 if main_bank else 1,
        0 if bank else 1,
        1 if reception else 0,
        int(getattr(pm, "sort_order", 0) or 0),
        int(pm.id),
    )


def hotel_settle_source_pm(db: Session, *, kind: PaymentMethodKind | None = None) -> PaymentMethod:
    """خزينة الفندق الافتراضية للتسوية: مصرف/توا ما لم يُطلب كاش صراحة."""
    hotels = ensure_hotel_treasury_payment_methods(db)
    key = "CASH" if kind == PaymentMethodKind.CASH else "BANK"
    return hotels[key]


def restaurant_settle_target_pm(
    db: Session, *, kind: PaymentMethodKind | None = None
) -> PaymentMethod:
    """خزينة المطعم المستهدفة — مصرف/توا افتراضياً."""
    from modules.payments.shift_handoff_service import ensure_main_treasury_payment_methods

    mains = ensure_main_treasury_payment_methods(db)
    key = "CASH" if kind == PaymentMethodKind.CASH else "BANK"
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


def is_settle_source_wallet(pm: PaymentMethod | None, *, allow_main: bool = False) -> bool:
    """مصدر التحويل: محافظ/خزينة الفندق، وللأدمن وأمين الخزينة أيضاً الخزينة الرئيسية."""
    if pm is None:
        return False
    if is_hotel_settle_wallet(pm):
        return True
    if allow_main and is_main_treasury_payment_method(pm):
        return True
    return False


def pick_hotel_treasury_with_balance(
    db: Session, amount: Decimal, *, allow_main: bool = False
) -> PaymentMethod:
    """يختار مصدر فندق برصيد كافٍ — مصرف/توا أولاً ثم الكاش."""
    from modules.payments.service import (
        ensure_hotel_reception_payment_methods,
        ensure_hotel_treasury_payment_methods,
        method_current_balance,
    )
    from modules.payments.shift_handoff_service import (
        ensure_main_treasury_payment_methods,
    )

    need = Decimal(str(amount or 0)).quantize(Decimal("0.001"))
    recv = ensure_hotel_reception_payment_methods(db)
    hotels = ensure_hotel_treasury_payment_methods(db)
    candidates = [
        hotels["BANK"],
        recv["BANK"],
        hotels["CASH"],
        recv["CASH"],
    ]
    if allow_main:
        mains = ensure_main_treasury_payment_methods(db)
        candidates.extend([mains["BANK"], mains["CASH"]])
    gl_info = _gl_info_map(db)
    candidates.sort(key=lambda m: settle_wallet_preference_key(m, gl_info=gl_info))
    for pm in candidates:
        bal = method_current_balance(db, int(pm.id))
        if bal + Decimal("0.0005") >= need:
            return pm
    cash_bal = method_current_balance(db, int(recv["CASH"].id)) + method_current_balance(
        db, int(hotels["CASH"].id)
    )
    bank_bal = method_current_balance(db, int(recv["BANK"].id)) + method_current_balance(
        db, int(hotels["BANK"].id)
    )
    extra = ""
    if allow_main:
        mains = ensure_main_treasury_payment_methods(db)
        main_c = method_current_balance(db, int(mains["CASH"].id))
        main_b = method_current_balance(db, int(mains["BANK"].id))
        extra = f" · الخزينة الرئيسية كاش: {main_c} · مصرف: {main_b}"
    raise PaymentsError(
        f"رصيد محفظة/خزينة الفندق غير كافٍ لتحويل {need} د.ل إلى المطعم "
        f"(كاش: {cash_bal} · مصرف: {bank_bal}{extra})."
    )


def list_settle_source_methods(db: Session, *, user=None) -> list[PaymentMethod]:
    """مصادر التحويل في التسوية: فندق (+ الخزينة الرئيسية للأدمن وأمين الخزينة)."""
    from modules.authz.capability import can_settle_from_main_treasury
    from modules.payments.service import list_hotel_settle_payment_methods

    allow_main = can_settle_from_main_treasury(user)
    rows = list_hotel_settle_payment_methods(
        db, only_active=True, include_restaurant=allow_main, user=user
    )
    out = [m for m in rows if is_settle_source_wallet(m, allow_main=allow_main)]
    gl_info = _gl_info_map(db)
    out.sort(key=lambda m: settle_wallet_preference_key(m, gl_info=gl_info))
    return out


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
    gl_info = _gl_info_map(db)
    rows.sort(key=lambda m: settle_wallet_preference_key(m, gl_info=gl_info))
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
    bank_ref: str | None = None,
    require_operation_ref: bool | None = None,
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
    if not is_settle_source_wallet(from_pm, allow_main=True):
        raise PaymentsError(
            "مصدر التحويل يجب أن يكون خزينة فندق أو الخزينة الرئيسية (كاش أو مصرف)."
        )

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
        bank_ref=bank_ref,
        require_operation_ref=require_operation_ref,
    )
