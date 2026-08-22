"""اعتماد إيراد جلسة الفندق — مثل جلسة المطعم.

التحصيل اليومي: محفظة «استقبال الفندق».
بعد الإقفال: المبالغ معلّقة حتى يعتمد أمين الخزينة ← تحويل إلى «خزينة الفندق».
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session, selectinload

from modules.hotel.shift_models import HotelShift, HotelShiftStatus
from modules.payments.shift_carry import CLOSE_DEST_NEXT_SHIFT, is_next_shift_destination
from modules.payments.models import PaymentMethodKind, PaymentTransfer, PaymentTransferType
from modules.payments.service import (
    PaymentsError,
    ensure_hotel_reception_payment_methods,
    ensure_hotel_treasury_payment_methods,
    method_current_balance,
    record_manual_transfer,
)


class HotelShiftHandoffError(PaymentsError):
    pass


def _hotel_treasury_pending_clause():
    return or_(
        HotelShift.close_destination.is_(None),
        HotelShift.close_destination != CLOSE_DEST_NEXT_SHIFT,
    )


def issue_hotel_opening_float_transfer(
    db: Session,
    *,
    shift_id: int,
    amount: Decimal,
    user_id: int | None,
    kind: PaymentMethodKind = PaymentMethodKind.CASH,
) -> PaymentTransfer | None:
    """فكة الافتتاح: خصم من خزينة الفندق الرئيسية → محفظة الاستقبال."""
    amount = Decimal(str(amount or 0)).quantize(Decimal("0.001"))
    if amount <= 0:
        return None
    recv = ensure_hotel_reception_payment_methods(db)
    treas = ensure_hotel_treasury_payment_methods(db)
    key = _wallet_key(kind)
    src = treas[key]
    dst = recv[key]
    bal = method_current_balance(db, src.id)
    if amount > bal:
        kind_ar = "كاش" if key == "CASH" else "مصرف"
        raise HotelShiftHandoffError(
            f"رصيد «{src.name_ar}» ({bal} د.ل) لا يكفي لفكة الافتتاح ({amount} د.ل). "
            "أضف رصيداً للخزينة الرئيسية أولاً، أو افتح الجلسة بدون رصيد افتتاح."
        )
    return record_manual_transfer(
        db,
        from_payment_method_id=src.id,
        to_payment_method_id=dst.id,
        amount=amount,
        user_id=user_id,
        note=f"فكة افتتاح جلسة فندق #{shift_id} — إلى {dst.name_ar}",
        transfer_type=PaymentTransferType.MANUAL,
    )


def parse_handoff_amount(raw: str | None) -> Decimal | None:
    s = (raw or "").strip().replace(",", ".")
    if not s:
        return None
    try:
        d = Decimal(s).quantize(Decimal("0.001"))
    except Exception as exc:
        raise HotelShiftHandoffError("مبلغ غير صالح.") from exc
    if d < 0:
        raise HotelShiftHandoffError("المبلغ لا يمكن أن يكون سالباً.")
    return d


@dataclass
class HotelShiftHandoffPanel:
    shift_id: int
    closed_at: datetime | None
    operator_label: str
    cash_amount: Decimal
    bank_amount: Decimal
    expected_cash: Decimal
    expected_bank: Decimal
    total_amount: Decimal
    can_approve: bool
    handoff_at: datetime | None
    treasury_cash_balance: Decimal
    treasury_bank_balance: Decimal


def mark_legacy_hotel_shifts_handed_off(db: Session) -> int:
    """مرة واحدة: جلسات أُغلقت قبل الميزة (كان النقد يدخل الخزينة مباشرة)."""
    from modules.settings.service import get_setting, set_setting

    if (get_setting(db, "hotel_shift_handoff_legacy_stamped") or "").strip() == "1":
        return 0
    result = db.execute(
        update(HotelShift)
        .where(
            HotelShift.status == HotelShiftStatus.CLOSED,
            HotelShift.treasury_handoff_at.is_(None),
            HotelShift.closed_at.is_not(None),
        )
        .values(treasury_handoff_at=HotelShift.closed_at)
    )
    set_setting(db, "hotel_shift_handoff_legacy_stamped", "1")
    db.flush()
    return int(result.rowcount or 0)


def get_next_hotel_shift_pending_handoff(db: Session) -> HotelShift | None:
    return db.execute(
        select(HotelShift)
        .options(
            selectinload(HotelShift.employee),
            selectinload(HotelShift.user),
        )
        .where(
            HotelShift.status == HotelShiftStatus.CLOSED,
            HotelShift.treasury_handoff_at.is_(None),
            _hotel_treasury_pending_clause(),
        )
        .order_by(HotelShift.id.asc())
        .limit(1)
    ).scalar_one_or_none()


def count_hotel_shifts_pending_handoff(db: Session) -> int:
    return int(
        db.execute(
            select(func.count())
            .select_from(HotelShift)
            .where(
                HotelShift.status == HotelShiftStatus.CLOSED,
                HotelShift.treasury_handoff_at.is_(None),
                _hotel_treasury_pending_clause(),
            )
        ).scalar_one()
        or 0
    )


def list_hotel_shifts_pending_handoff(
    db: Session, *, limit: int = 50
) -> list[HotelShift]:
    return list(
        db.execute(
            select(HotelShift)
            .options(
                selectinload(HotelShift.employee),
                selectinload(HotelShift.user),
            )
            .where(
                HotelShift.status == HotelShiftStatus.CLOSED,
                HotelShift.treasury_handoff_at.is_(None),
                _hotel_treasury_pending_clause(),
            )
            .order_by(HotelShift.id.asc())
            .limit(max(1, min(int(limit), 200)))
        )
        .scalars()
        .all()
    )


def _operator_label(sh: HotelShift) -> str:
    if getattr(sh, "employee", None) is not None and sh.employee.full_name_ar:
        return sh.employee.full_name_ar
    if getattr(sh, "user", None) is not None and sh.user.username:
        return sh.user.username
    return f"جلسة فندق #{sh.id}"


def build_hotel_shift_handoff_panel(db: Session) -> HotelShiftHandoffPanel | None:
    sh = get_next_hotel_shift_pending_handoff(db)
    if sh is None:
        return None
    treas = ensure_hotel_treasury_payment_methods(db)
    cash_amt = Decimal(str(sh.counted_cash if sh.counted_cash is not None else sh.expected_cash or 0)).quantize(
        Decimal("0.001")
    )
    bank_amt = Decimal(str(sh.counted_bank if sh.counted_bank is not None else sh.expected_bank or 0)).quantize(
        Decimal("0.001")
    )
    return HotelShiftHandoffPanel(
        shift_id=sh.id,
        closed_at=sh.closed_at,
        operator_label=_operator_label(sh),
        cash_amount=cash_amt,
        bank_amount=bank_amt,
        expected_cash=Decimal(str(sh.expected_cash or 0)).quantize(Decimal("0.001")),
        expected_bank=Decimal(str(sh.expected_bank or 0)).quantize(Decimal("0.001")),
        total_amount=(cash_amt + bank_amt).quantize(Decimal("0.001")),
        can_approve=True,
        handoff_at=None,
        treasury_cash_balance=method_current_balance(db, treas["CASH"].id),
        treasury_bank_balance=method_current_balance(db, treas["BANK"].id),
    )


def _wallet_key(kind: PaymentMethodKind | str) -> str:
    raw = getattr(kind, "value", kind)
    s = str(raw or "").strip().upper()
    if s in ("CASH", "PAYMENTMETHODKIND.CASH"):
        return "CASH"
    return "BANK"


def _transfer_reception_to_treasury(
    db: Session,
    *,
    kind: PaymentMethodKind | str,
    amount: Decimal,
    shift_id: int,
    user_id: int | None,
    note_suffix: str = "",
) -> Decimal:
    amount = Decimal(str(amount or 0)).quantize(Decimal("0.001"))
    if amount <= 0:
        return Decimal("0")
    recv = ensure_hotel_reception_payment_methods(db)
    treas = ensure_hotel_treasury_payment_methods(db)
    key = _wallet_key(kind)
    src = recv[key]
    dst = treas[key]
    bal = method_current_balance(db, src.id)
    # لا نمنع إن نقصت محفظة الاستقبال قليلاً (عجز معدود) — نُحوِّل min(amount, bal) إن rصيد جزئي
    xfer = amount
    if bal + Decimal("0.0005") < amount:
        kind_ar = "كاش" if key == "CASH" else "مصرف"
        cash_bal = method_current_balance(db, recv["CASH"].id)
        bank_bal = method_current_balance(db, recv["BANK"].id)
        if bal <= Decimal("0.0005"):
            raise HotelShiftHandoffError(
                f"رصيد محفظة استقبال الفندق ({kind_ar}) غير كافٍ "
                f"للتحويل {amount} د.ل (المتوفر: {bal} د.ل). "
                f"أرصدة الاستقبال الآن — كاش: {cash_bal} · مصرف: {bank_bal} د.ل. "
                "تحقق من أن التحصيل جرى على «استقبال الفندق» وليس على حساب آخر، "
                "وأن مبلغ الاعتماد (كاش/مصرف) يطابق وسيلة الدفع المستخدمة."
            )
        xfer = bal.quantize(Decimal("0.001"))
    record_manual_transfer(
        db,
        from_payment_method_id=src.id,
        to_payment_method_id=dst.id,
        amount=xfer,
        user_id=user_id,
        note=f"اعتماد وتحويل جلسة فندق #{shift_id} — من {src.name_ar}{note_suffix}",
        transfer_type=PaymentTransferType.SHIFT_HANDOFF,
    )
    return xfer


def _normalize_handoff_vs_wallets(
    db: Session,
    *,
    cash_amt: Decimal,
    bank_amt: Decimal,
) -> tuple[Decimal, Decimal]:
    """يُصحّح توزيع كاش/مصرف إن طابق الإجمالي محفظة واحدة فقط.

    مثال: التحصيل على «استقبال — كاش» لكن الاعتماد يُمرِّر المبلغ كمصرف → نقل التصنيف.
    """
    cash_amt = Decimal(str(cash_amt or 0)).quantize(Decimal("0.001"))
    bank_amt = Decimal(str(bank_amt or 0)).quantize(Decimal("0.001"))
    if cash_amt < 0 or bank_amt < 0:
        raise HotelShiftHandoffError("مبالغ التحويل لا يمكن أن تكون سالبة.")
    recv = ensure_hotel_reception_payment_methods(db)
    cash_bal = method_current_balance(db, recv["CASH"].id)
    bank_bal = method_current_balance(db, recv["BANK"].id)
    eps = Decimal("0.0005")
    total = (cash_amt + bank_amt).quantize(Decimal("0.001"))
    avail = (cash_bal + bank_bal).quantize(Decimal("0.001"))

    # تصحيح شائع: كل المبلغ في حقل مصرف بينما الرصيد في كاش (أو العكس)
    if bank_amt > bank_bal + eps and cash_amt <= eps and cash_bal + eps >= bank_amt:
        cash_amt, bank_amt = bank_amt, Decimal("0.000")
    elif cash_amt > cash_bal + eps and bank_amt <= eps and bank_bal + eps >= cash_amt:
        bank_amt, cash_amt = cash_amt, Decimal("0.000")

    cash_bal = method_current_balance(db, recv["CASH"].id)
    bank_bal = method_current_balance(db, recv["BANK"].id)
    if cash_amt > cash_bal + eps or bank_amt > bank_bal + eps:
        raise HotelShiftHandoffError(
            f"لا يكفي رصيد محفظة الاستقبال للتحويل المطلوب "
            f"(كاش {cash_amt} / مصرف {bank_amt}). "
            f"المتوفّر — كاش: {cash_bal} · مصرف: {bank_bal} د.ل "
            f"(الإجمالي المطلوب {total} · المتاح {avail}). "
            "تأكّد أن قبض النزيل سُجّل على «استقبال الفندق — كاش/مصرف»."
        )
    return cash_amt, bank_amt


def approve_hotel_shift_handoff(
    db: Session,
    *,
    shift_id: int,
    user_id: int,
    admin_username: str = "",
    handoff_cash: Decimal | None = None,
    handoff_bank: Decimal | None = None,
    handoff_note: str | None = None,
) -> HotelShiftHandoffPanel | None:
    sh = db.get(HotelShift, shift_id)
    if sh is None or sh.status != HotelShiftStatus.CLOSED:
        raise HotelShiftHandoffError("جلسة الفندق غير موجودة أو لم تُغلَق بعد.")
    if sh.treasury_handoff_at is not None:
        raise HotelShiftHandoffError("تم اعتماد وتحويل هذه الجلسة مسبقاً.")
    if is_next_shift_destination(getattr(sh, "close_destination", None)):
        raise HotelShiftHandoffError("هذه الجلسة رُحّلت للوردية التالية وليس للخزينة.")
    next_pending = get_next_hotel_shift_pending_handoff(db)
    if next_pending is None or next_pending.id != sh.id:
        if next_pending is not None:
            raise HotelShiftHandoffError(
                f"يجب اعتماد الجلسات بالترتيب. التالية: جلسة فندق #{next_pending.id}."
            )
        raise HotelShiftHandoffError("لا توجد جلسة فندق بانتظار الاعتماد.")

    from modules.payments.shift_carry import money3
    from modules.payments.shift_handovers import handed_claimed_totals

    handed_c, handed_b = handed_claimed_totals(db, hotel_shift_id=sh.id)
    raw_cash = sh.counted_cash if sh.counted_cash is not None else sh.expected_cash
    raw_bank = sh.counted_bank if sh.counted_bank is not None else sh.expected_bank
    default_cash = max(money3(raw_cash) - handed_c, money3(0))
    default_bank = max(money3(raw_bank) - handed_b, money3(0))
    cash_amt = (
        handoff_cash.quantize(Decimal("0.001"))
        if handoff_cash is not None
        else Decimal(str(default_cash or 0)).quantize(Decimal("0.001"))
    )
    bank_amt = (
        handoff_bank.quantize(Decimal("0.001"))
        if handoff_bank is not None
        else Decimal(str(default_bank or 0)).quantize(Decimal("0.001"))
    )
    cash_amt, bank_amt = _normalize_handoff_vs_wallets(
        db, cash_amt=cash_amt, bank_amt=bank_amt
    )
    from modules.payments.shift_handovers import mark_bank_declaration_confirmed

    orig_cash, orig_bank = sh.counted_cash, sh.counted_bank
    from modules.payments.shift_variance_models import ShiftVarianceSource
    from modules.payments.shift_variances import record_pair_variances

    record_pair_variances(
        db,
        source_type=ShiftVarianceSource.HOTEL_TREASURY,
        claimed_cash=orig_cash,
        received_cash=cash_amt,
        claimed_bank=orig_bank,
        received_bank=bank_amt,
        hotel_shift_id=sh.id,
        from_employee_id=sh.employee_id,
        note=f"اعتماد خزينة جلسة فندق #{sh.id}",
    )
    # لا نعدّل معدود الموظف — الفرق يُعلَّق في سجل العجوزات
    note_parts: list[str] = []
    who = admin_username or f"#{user_id}"
    if orig_cash is not None and cash_amt != Decimal(str(orig_cash)).quantize(
        Decimal("0.001")
    ):
        note_parts.append(f"كاش {orig_cash}→{cash_amt}")
    if orig_bank is not None and bank_amt != Decimal(str(orig_bank)).quantize(
        Decimal("0.001")
    ):
        note_parts.append(f"مصرف {orig_bank}→{bank_amt}")
    extra = (handoff_note or "").strip()
    if note_parts or extra:
        line = f"[اعتماد خزينة الفندق — {who}]"
        if note_parts:
            line += " تصحيح: " + "، ".join(note_parts)
        if extra:
            line += " " + extra
        prev = (sh.closing_note or "").strip()
        sh.closing_note = (prev + " | " + line).strip(" | ") if prev else line

    note_suffix = f" — {extra}" if extra else ""
    _transfer_reception_to_treasury(
        db,
        kind=PaymentMethodKind.CASH,
        amount=cash_amt,
        shift_id=shift_id,
        user_id=user_id,
        note_suffix=note_suffix,
    )
    _transfer_reception_to_treasury(
        db,
        kind=PaymentMethodKind.BANK,
        amount=bank_amt,
        shift_id=shift_id,
        user_id=user_id,
        note_suffix=note_suffix,
    )
    sh.treasury_handoff_at = datetime.now(timezone.utc)
    sh.treasury_handoff_by_id = user_id
    mark_bank_declaration_confirmed(
        db, hotel_shift_id=sh.id, received_bank=bank_amt, user_id=user_id
    )
    db.flush()
    try:
        from modules.notifications.treasury_hooks import emit_treasury_handoff_approved

        cashier = ""
        if getattr(sh, "employee", None) is not None:
            cashier = (sh.employee.full_name_ar or "").strip()
        emit_treasury_handoff_approved(
            db,
            shift_id=int(sh.id),
            cashier_name=cashier or admin_username,
            claimed_cash=cash_amt,
            claimed_bank=bank_amt,
            shift_kind="hotel",
        )
    except Exception:  # noqa: BLE001
        pass
    return build_hotel_shift_handoff_panel(db)


def revoke_hotel_shift_handoff(
    db: Session,
    *,
    shift_id: int,
    user_id: int,
    reason: str | None = None,
) -> None:
    """عكس اعتماد الجلسة (إعادة من خزينة الفندق → الاستقبال)."""
    sh = db.get(HotelShift, shift_id)
    if sh is None or sh.treasury_handoff_at is None:
        raise HotelShiftHandoffError("لا يوجد اعتماد لإلغائه على هذه الجلسة.")
    needle = f"اعتماد وتحويل جلسة فندق #{shift_id}"
    forwards = list(
        db.scalars(
            select(PaymentTransfer)
            .where(
                PaymentTransfer.transfer_type == PaymentTransferType.SHIFT_HANDOFF,
                PaymentTransfer.note.contains(needle),
            )
            .order_by(PaymentTransfer.id.asc())
        ).all()
    )
    if not forwards:
        raise HotelShiftHandoffError("لم تُعثر على تحويلات اعتماد لهذه الجلسة.")
    why = (reason or "").strip() or "إلغاء اعتماد"
    for tf in reversed(forwards):
        record_manual_transfer(
            db,
            from_payment_method_id=int(tf.to_payment_method_id),
            to_payment_method_id=int(tf.from_payment_method_id),
            amount=Decimal(str(tf.amount)),
            user_id=user_id,
            note=f"إلغاء اعتماد جلسة فندق #{shift_id} — {why}",
            transfer_type=PaymentTransferType.SHIFT_HANDOFF,
        )
    sh.treasury_handoff_at = None
    sh.treasury_handoff_by_id = None
    db.flush()
