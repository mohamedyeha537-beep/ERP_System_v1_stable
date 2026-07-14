"""تسوية نقاط الولاء عند إغلاق الجلسة — تكاليف تشغيل وليس عجزاً على الكاشير."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.customers.loyalty_shift_reports import loyalty_redeem_summary_for_shift
from modules.payments.models import Purchase, PurchaseKind
from modules.payments.service import record_accrual_expense
from modules.pos_shifts.models import PosShift, PosShiftShortage, PosShiftStatus, ShortageKind

LOYALTY_OPERATING_EXPENSE_CATEGORY = "نقاط ولاء — تكاليف تشغيل"
LOYALTY_SHIFT_EXPENSE_REF_PREFIX = "pos-shift-loyalty-"


def loyalty_cash_shortage_absorption(
    raw_cash_shortage: Decimal, loyalty_dinar_cost: Decimal
) -> Decimal:
    """جزء من عجز الكاش المُفسَّر بخصم نقاط الولاء (لا يُحمَّل على الموظف)."""
    shortage = max(Decimal(str(raw_cash_shortage or 0)), Decimal("0")).quantize(
        Decimal("0.001")
    )
    loyalty = max(Decimal(str(loyalty_dinar_cost or 0)), Decimal("0")).quantize(
        Decimal("0.001")
    )
    if shortage <= 0 or loyalty <= 0:
        return Decimal("0")
    return min(shortage, loyalty).quantize(Decimal("0.001"))


def _loyalty_expense_ref(shift_id: int) -> str:
    return f"{LOYALTY_SHIFT_EXPENSE_REF_PREFIX}{shift_id}"


def find_loyalty_expense_for_shift(db: Session, shift_id: int) -> Purchase | None:
    ref = _loyalty_expense_ref(shift_id)
    return db.scalar(
        select(Purchase).where(
            Purchase.kind == PurchaseKind.EXPENSE,
            Purchase.supplier_invoice_ref == ref,
        )
    )


def ensure_loyalty_operating_expense_for_shift(
    db: Session,
    sh: PosShift,
    loyalty_dinar_cost: Decimal,
    *,
    user_id: int | None,
) -> Purchase | None:
    """يسجّل تكلفة نقاط الولاء كمصروف تشغيل (استحقاق — بدون خصم من الخزينة)."""
    amt = max(Decimal(str(loyalty_dinar_cost or 0)), Decimal("0")).quantize(
        Decimal("0.001")
    )
    if amt <= 0:
        return None
    existing = find_loyalty_expense_for_shift(db, sh.id)
    if existing is not None:
        if Decimal(str(existing.amount or 0)) != amt:
            existing.amount = amt
            existing.note = _loyalty_expense_note(sh, amt)
        return existing
    return record_accrual_expense(
        db,
        amount=amt,
        expense_category=LOYALTY_OPERATING_EXPENSE_CATEGORY,
        supplier="برنامج الولاء",
        note=_loyalty_expense_note(sh, amt),
        user_id=user_id,
        supplier_invoice_ref=_loyalty_expense_ref(sh.id),
        created_at=sh.closed_at,
    )


def _loyalty_expense_note(sh: PosShift, amt: Decimal) -> str:
    return (
        f"تسوية نقاط ولاء — جلسة #{sh.id} — {amt} د.ل "
        "(تكلفة تشغيل / تسويق — لا يُخصم من درج الكاش)"
    )


def settle_loyalty_on_shift_close(
    db: Session,
    sh: PosShift,
    *,
    counted_cash: Decimal,
    expected_cash: Decimal,
    user_id: int | None,
) -> Decimal:
    """يُسجّل مصروف الولاء ويُرجع جزء العجز المُستثنى (إن وُجد)."""
    loyalty = loyalty_redeem_summary_for_shift(db, sh.id)
    if loyalty.dinar_cost > 0:
        ensure_loyalty_operating_expense_for_shift(
            db, sh, loyalty.dinar_cost, user_id=user_id
        )
    raw_diff = (counted_cash - expected_cash).quantize(Decimal("0.001"))
    if raw_diff >= 0:
        return Decimal("0")
    return loyalty_cash_shortage_absorption(-raw_diff, loyalty.dinar_cost)


def reconcile_loyalty_false_shortages(db: Session) -> int:
    """يُصحّح سجلات عجز ناتجة عن خصم نقاط الولاء (جلسات سابقة)."""
    rows = list(
        db.scalars(
            select(PosShiftShortage)
            .where(
                PosShiftShortage.shortage_amount > 0,
                PosShiftShortage.kind == ShortageKind.CASH,
            )
            .order_by(PosShiftShortage.id.asc())
        ).all()
    )
    n = 0
    now = datetime.now(timezone.utc)
    for row in rows:
        action = (getattr(row, "resolved_action", None) or "").strip().upper()
        if action in ("DEDUCT", "FORGIVE", "LOYALTY"):
            continue
        sh = db.get(PosShift, row.shift_id)
        if sh is None or sh.status != PosShiftStatus.CLOSED:
            continue
        loyalty = loyalty_redeem_summary_for_shift(db, sh.id)
        if loyalty.dinar_cost <= 0:
            continue
        absorb = loyalty_cash_shortage_absorption(
            Decimal(str(row.shortage_amount or 0)), loyalty.dinar_cost
        )
        if absorb <= 0:
            continue
        ensure_loyalty_operating_expense_for_shift(
            db, sh, loyalty.dinar_cost, user_id=sh.user_id
        )
        remaining = (Decimal(str(row.shortage_amount or 0)) - absorb).quantize(
            Decimal("0.001")
        )
        if row.original_shortage_amount is None:
            row.original_shortage_amount = Decimal(str(row.shortage_amount or 0))
        if remaining <= 0:
            row.shortage_amount = Decimal("0")
            row.resolved_action = "LOYALTY"
            row.resolved_note = (
                f"تسوية تلقائية: {absorb} د.ل خصم نقاط ولاء — تكاليف تشغيل "
                f"(جلسة #{sh.id})"
            )
            row.resolved_at = now
        else:
            row.shortage_amount = remaining
            row.resolved_note = (
                f"استُثنِي {absorb} د.ل (نقاط ولاء). المتبقي عجز فعلي: {remaining} د.ل"
            )
        n += 1
    if n:
        db.flush()
    return n
