"""اعتماد وتحويل إيراد جلسة الكاشير إلى الخزائن الرئيسية."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from modules.payments.shift_carry import CLOSE_DEST_NEXT_SHIFT, is_next_shift_destination

from modules.payments.models import (
    MAIN_TREASURY_BANK_PM_NAME,
    MAIN_TREASURY_CASH_PM_NAME,
    PaymentMethod,
    PaymentMethodDomain,
    PaymentMethodKind,
    PaymentTransfer,
    PaymentTransferType,
)
from modules.payments.service import (
    PaymentsError,
    is_owner_equity_payment_method,
    is_supplier_credit_payment_method,
    list_payment_methods,
    method_current_balance,
    payment_method_kind_matches,
    record_manual_transfer,
)
from modules.pos_shifts.models import PosShift, PosShiftStatus
from modules.pos_shifts.service import compute_shift_financial_summary


class ShiftHandoffError(PaymentsError):
    pass


def parse_handoff_amount(raw: str | None) -> Decimal | None:
    """None = استخدم المعدود المسجّل عند الإغلاق."""
    s = (raw or "").strip().replace(",", ".")
    if not s:
        return None
    try:
        d = Decimal(s).quantize(Decimal("0.001"))
    except Exception as exc:
        raise ShiftHandoffError("مبلغ غير صالح.") from exc
    if d < 0:
        raise ShiftHandoffError("المبلغ لا يمكن أن يكون سالباً.")
    return d


@dataclass
class ShiftHandoffPanel:
    shift_id: int
    closed_at: datetime | None
    cashier_label: str
    cash_amount: Decimal
    bank_amount: Decimal
    expected_cash: Decimal
    expected_bank: Decimal
    total_amount: Decimal
    can_approve: bool
    handoff_at: datetime | None
    main_cash_balance: Decimal
    main_bank_balance: Decimal


def is_main_treasury_payment_method(pm: PaymentMethod) -> bool:
    return pm.name_ar in (MAIN_TREASURY_CASH_PM_NAME, MAIN_TREASURY_BANK_PM_NAME)


def ensure_main_treasury_payment_method(
    db: Session, kind: PaymentMethodKind
) -> PaymentMethod:
    name = (
        MAIN_TREASURY_CASH_PM_NAME
        if kind == PaymentMethodKind.CASH
        else MAIN_TREASURY_BANK_PM_NAME
    )
    row = db.execute(
        select(PaymentMethod).where(PaymentMethod.name_ar == name)
    ).scalar_one_or_none()
    if row is None:
        row = PaymentMethod(
            name_ar=name,
            kind=kind,
            is_active=True,
            sort_order=50 if kind == PaymentMethodKind.CASH else 51,
            can_receive=False,
            can_pay=True,
            can_fund=True,
            is_system=True,
            show_on_dashboard=True,
            business_domain=PaymentMethodDomain.RESTAURANT,
        )
        db.add(row)
        db.flush()
    else:
        row.kind = kind
        row.is_active = True
        row.is_system = True
        row.can_receive = False
        row.can_pay = True
        row.can_fund = True
        row.show_on_dashboard = True
        row.business_domain = PaymentMethodDomain.RESTAURANT
        db.flush()
    return row


def ensure_main_treasury_payment_methods(db: Session) -> dict[str, PaymentMethod]:
    return {
        "CASH": ensure_main_treasury_payment_method(db, PaymentMethodKind.CASH),
        "BANK": ensure_main_treasury_payment_method(db, PaymentMethodKind.BANK),
    }


def list_cashier_wallet_methods(
    db: Session,
    kind: PaymentMethodKind,
    *,
    only_active: bool = True,
    user=None,
) -> list[PaymentMethod]:
    """محافظ كاشير المطعم فقط (كاش/مصرف قابلة للاستلام) — بدون خزن الفندق."""
    from modules.authz.pos_wallet_access import user_may_use_payment_method_kind
    from modules.payments.service import is_hotel_treasury_payment_method
    from modules.platform.business_domain import BusinessDomain

    if not user_may_use_payment_method_kind(user, kind):
        return []
    out: list[PaymentMethod] = []
    for m in list_payment_methods(
        db, only_active=only_active, domain=BusinessDomain.RESTAURANT
    ):
        if not payment_method_kind_matches(m, kind):
            continue
        if not m.can_receive:
            continue
        if is_main_treasury_payment_method(m):
            continue
        if is_hotel_treasury_payment_method(m):
            continue
        if is_supplier_credit_payment_method(m) or is_owner_equity_payment_method(m):
            continue
        out.append(m)
    return sorted(out, key=lambda x: (x.sort_order, x.id))


def issue_opening_float_transfer(
    db: Session,
    *,
    shift_id: int,
    amount: Decimal,
    user_id: int | None,
) -> PaymentTransfer | None:
    """تحويل فكة الافتتاح من الخزينة الرئيسية إلى محفظة الكاشير."""
    amount = Decimal(str(amount or 0)).quantize(Decimal("0.001"))
    if amount <= 0:
        return None
    mains = ensure_main_treasury_payment_methods(db)
    wallets = list_cashier_wallet_methods(db, PaymentMethodKind.CASH)
    if not wallets:
        raise ShiftHandoffError(
            "لا يوجد محفظة كاشير (كاش) لاستلام فكة الافتتاح — "
            "أنشئ محفظة كاش للكاشير مع «استلام تحويلات»."
        )
    main_pm = mains["CASH"]
    cashier_pm = wallets[0]
    bal = method_current_balance(db, main_pm.id)
    if amount > bal:
        raise ShiftHandoffError(
            f"رصيد «{main_pm.name_ar}» ({bal} د.ل) لا يكفي لفكة الافتتاح ({amount} د.ل)."
        )
    return record_manual_transfer(
        db,
        from_payment_method_id=main_pm.id,
        to_payment_method_id=cashier_pm.id,
        amount=amount,
        user_id=user_id,
        note=f"فكة افتتاح جلسة #{shift_id} — إلى {cashier_pm.name_ar}",
        transfer_type=PaymentTransferType.MANUAL,
    )


def get_last_closed_shift(db: Session) -> PosShift | None:
    return db.execute(
        select(PosShift)
        .options(
            selectinload(PosShift.employee),
            selectinload(PosShift.user),
        )
        .where(PosShift.status == PosShiftStatus.CLOSED)
        .order_by(PosShift.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def _pos_treasury_pending_clause():
    return (
        PosShift.status == PosShiftStatus.CLOSED,
        PosShift.treasury_handoff_at.is_(None),
        or_(
            PosShift.close_destination.is_(None),
            PosShift.close_destination != CLOSE_DEST_NEXT_SHIFT,
        ),
    )


def get_next_shift_pending_handoff(db: Session) -> PosShift | None:
    """أقدم جلسة مغلقة لم يُعتمد إيرادها بعد (FIFO)."""
    return db.execute(
        select(PosShift)
        .options(
            selectinload(PosShift.employee),
            selectinload(PosShift.user),
        )
        .where(*_pos_treasury_pending_clause())
        .order_by(PosShift.id.asc())
        .limit(1)
    ).scalar_one_or_none()


def count_shifts_pending_handoff(db: Session) -> int:
    return int(
        db.execute(
            select(func.count())
            .select_from(PosShift)
            .where(*_pos_treasury_pending_clause())
        ).scalar_one()
        or 0
    )


def list_shifts_pending_handoff(db: Session, *, limit: int = 50) -> list[PosShift]:
    """جلسات مغلقة بانتظار اعتماد الخزينة (الأقدم أولاً)."""
    return list(
        db.execute(
            select(PosShift)
            .options(
                selectinload(PosShift.employee),
                selectinload(PosShift.user),
            )
            .where(*_pos_treasury_pending_clause())
            .order_by(PosShift.id.asc())
            .limit(max(1, min(int(limit), 200)))
        )
        .scalars()
        .all()
    )


def build_shift_handoff_panel(db: Session) -> ShiftHandoffPanel | None:
    sh = get_next_shift_pending_handoff(db)
    if sh is None:
        return None
    mains = ensure_main_treasury_payment_methods(db)
    fin = compute_shift_financial_summary(db, sh.id)
    cash_amt = sh.counted_cash if sh.counted_cash is not None else fin.expected_cash_drawer
    bank_amt = sh.counted_bank if sh.counted_bank is not None else fin.expected_bank
    cash_amt = Decimal(str(cash_amt or 0)).quantize(Decimal("0.001"))
    bank_amt = Decimal(str(bank_amt or 0)).quantize(Decimal("0.001"))
    label = f"جلسة #{sh.id}"
    if getattr(sh, "employee", None) is not None and sh.employee.full_name_ar:
        label = sh.employee.full_name_ar
    elif getattr(sh, "user", None) is not None and sh.user.username:
        label = sh.user.username
    return ShiftHandoffPanel(
        shift_id=sh.id,
        closed_at=sh.closed_at,
        cashier_label=label,
        cash_amount=cash_amt,
        bank_amount=bank_amt,
        expected_cash=Decimal(str(fin.expected_cash_drawer or 0)).quantize(Decimal("0.001")),
        expected_bank=Decimal(str(fin.expected_bank or 0)).quantize(Decimal("0.001")),
        total_amount=(cash_amt + bank_amt).quantize(Decimal("0.001")),
        can_approve=True,
        handoff_at=None,
        main_cash_balance=method_current_balance(db, mains["CASH"].id),
        main_bank_balance=method_current_balance(db, mains["BANK"].id),
    )


def _transfer_counted_to_main(
    db: Session,
    *,
    kind: PaymentMethodKind,
    amount: Decimal,
    main_pm: PaymentMethod,
    shift_id: int,
    user_id: int | None,
    note_suffix: str = "",
) -> Decimal:
    """تحويل مبلغ الجلسة المعدود (كاش/مصرف) من محفظة نقطة البيع الأساسية إلى الخزينة الرئيسية."""
    amount = Decimal(str(amount or 0)).quantize(Decimal("0.001"))
    if amount <= 0:
        return Decimal("0")
    wallets = list_cashier_wallet_methods(db, kind)
    if not wallets:
        kind_label = "كاش" if kind == PaymentMethodKind.CASH else "مصرف"
        raise ShiftHandoffError(
            f"لا يوجد حساب كاشير ({kind_label}) لاستلام مبيعات نقطة البيع — "
            f"تعذّر تحويل {amount} د.ل."
        )
    src = wallets[0]
    record_manual_transfer(
        db,
        from_payment_method_id=src.id,
        to_payment_method_id=main_pm.id,
        amount=amount,
        user_id=user_id,
        note=f"اعتماد وتحويل جلسة #{shift_id} — من {src.name_ar}{note_suffix}",
        transfer_type=PaymentTransferType.SHIFT_HANDOFF,
    )
    return amount


def _apply_treasury_handoff_amounts(
    sh: PosShift,
    *,
    fin: object,
    cash_amt: Decimal,
    bank_amt: Decimal,
    handoff_note: str | None,
    admin_username: str,
) -> None:
    """يُحدّث المعدود على الجلسة عند تصحيح أمين الخزينة قبل التحويل."""
    cash_amt = Decimal(str(cash_amt or 0)).quantize(Decimal("0.001"))
    bank_amt = Decimal(str(bank_amt or 0)).quantize(Decimal("0.001"))
    if cash_amt < 0 or bank_amt < 0:
        raise ShiftHandoffError("مبالغ التحويل لا يمكن أن تكون سالبة.")

    orig_cash = sh.counted_cash
    orig_bank = sh.counted_bank
    expected_c = Decimal(str(getattr(fin, "expected_cash_drawer", 0) or 0)).quantize(
        Decimal("0.001")
    )
    expected_b = Decimal(str(getattr(fin, "expected_bank", 0) or 0)).quantize(
        Decimal("0.001")
    )

    sh.counted_cash = cash_amt
    sh.counted_bank = bank_amt
    sh.expected_cash = expected_c
    sh.expected_bank = expected_b
    sh.cash_difference = (cash_amt - expected_c).quantize(Decimal("0.001"))
    sh.bank_difference = (bank_amt - expected_b).quantize(Decimal("0.001"))

    corrections: list[str] = []
    if orig_cash is not None and cash_amt != Decimal(str(orig_cash)).quantize(
        Decimal("0.001")
    ):
        corrections.append(f"كاش {orig_cash}→{cash_amt}")
    if orig_bank is not None and bank_amt != Decimal(str(orig_bank)).quantize(
        Decimal("0.001")
    ):
        corrections.append(f"مصرف {orig_bank}→{bank_amt}")

    extra = (handoff_note or "").strip()
    if corrections or extra:
        parts = [f"[اعتماد الخزينة — {admin_username}]"]
        if corrections:
            parts.append("تصحيح: " + "، ".join(corrections))
        if extra:
            parts.append(extra)
        line = " ".join(parts)
        prev = (sh.closing_note or "").strip()
        sh.closing_note = (prev + " | " + line).strip(" | ") if prev else line


def approve_shift_handoff(
    db: Session,
    *,
    shift_id: int,
    user_id: int,
    admin_username: str = "",
    handoff_cash: Decimal | None = None,
    handoff_bank: Decimal | None = None,
    handoff_note: str | None = None,
) -> ShiftHandoffPanel | None:
    sh = db.get(PosShift, shift_id)
    if sh is None or sh.status != PosShiftStatus.CLOSED:
        raise ShiftHandoffError("الجلسة غير موجودة أو لم تُغلَق بعد.")
    if sh.treasury_handoff_at is not None:
        raise ShiftHandoffError("تم اعتماد وتحويل هذه الجلسة مسبقاً.")
    if is_next_shift_destination(getattr(sh, "close_destination", None)):
        raise ShiftHandoffError("هذه الجلسة رُحّلت للوردية التالية وليس للخزينة.")
    next_pending = get_next_shift_pending_handoff(db)
    if next_pending is None or next_pending.id != sh.id:
        if next_pending is not None:
            raise ShiftHandoffError(
                f"يجب اعتماد الجلسات بالترتيب. التالية بانتظار الاعتماد: جلسة #{next_pending.id}."
            )
        raise ShiftHandoffError("لا توجد جلسة بانتظار الاعتماد.")
    mains = ensure_main_treasury_payment_methods(db)
    fin = compute_shift_financial_summary(db, shift_id)
    default_cash = sh.counted_cash if sh.counted_cash is not None else fin.expected_cash_drawer
    default_bank = sh.counted_bank if sh.counted_bank is not None else fin.expected_bank
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
    from modules.payments.shift_handovers import mark_bank_declaration_confirmed
    from modules.payments.shift_variance_models import ShiftVarianceSource
    from modules.payments.shift_variances import record_pair_variances

    record_pair_variances(
        db,
        source_type=ShiftVarianceSource.POS_TREASURY,
        claimed_cash=sh.counted_cash if sh.counted_cash is not None else default_cash,
        received_cash=cash_amt,
        claimed_bank=sh.counted_bank if sh.counted_bank is not None else default_bank,
        received_bank=bank_amt,
        pos_shift_id=sh.id,
        from_employee_id=sh.employee_id,
        note=f"اعتماد خزينة جلسة مطعم #{sh.id}",
    )
    _apply_treasury_handoff_amounts(
        sh,
        fin=fin,
        cash_amt=cash_amt,
        bank_amt=bank_amt,
        handoff_note=handoff_note,
        admin_username=admin_username or f"#{user_id}",
    )
    from modules.pos_shifts.shortages import record_shortages_for_closed_shift

    record_shortages_for_closed_shift(db, sh)

    note_suffix = (handoff_note or "").strip()
    transfer_note_extra = ""
    if note_suffix:
        transfer_note_extra = f" — {note_suffix}"

    _transfer_counted_to_main(
        db,
        kind=PaymentMethodKind.CASH,
        amount=cash_amt,
        main_pm=mains["CASH"],
        shift_id=shift_id,
        user_id=user_id,
        note_suffix=transfer_note_extra,
    )
    _transfer_counted_to_main(
        db,
        kind=PaymentMethodKind.BANK,
        amount=bank_amt,
        main_pm=mains["BANK"],
        shift_id=shift_id,
        user_id=user_id,
        note_suffix=transfer_note_extra,
    )
    sh.treasury_handoff_at = datetime.now(timezone.utc)
    sh.treasury_handoff_by_id = user_id
    mark_bank_declaration_confirmed(
        db, pos_shift_id=sh.id, received_bank=bank_amt, user_id=user_id
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
            shift_kind="restaurant",
        )
    except Exception:  # noqa: BLE001
        pass
    # قد لا توجد جلسة تالية بانتظار الاعتماد — هذا نجاح وليس خطأ
    return build_shift_handoff_panel(db)


_HANDOFF_FORWARD_PREFIX = "اعتماد وتحويل جلسة #"
_HANDOFF_REVERSE_PREFIX = "إلغاء اعتماد جلسة #"


def _forward_handoff_transfers(db: Session, shift_id: int) -> list[PaymentTransfer]:
    needle = f"{_HANDOFF_FORWARD_PREFIX}{shift_id}"
    return list(
        db.scalars(
            select(PaymentTransfer)
            .where(
                PaymentTransfer.transfer_type == PaymentTransferType.SHIFT_HANDOFF,
                PaymentTransfer.note.contains(needle),
            )
            .order_by(PaymentTransfer.id.asc())
        ).all()
    )


def _reverse_handoff_transfers(db: Session, shift_id: int) -> list[PaymentTransfer]:
    needle = f"{_HANDOFF_REVERSE_PREFIX}{shift_id}"
    return list(
        db.scalars(
            select(PaymentTransfer).where(
                PaymentTransfer.transfer_type == PaymentTransferType.SHIFT_HANDOFF,
                PaymentTransfer.note.contains(needle),
            )
        ).all()
    )


def _active_handoff_transfers(db: Session, shift_id: int) -> list[PaymentTransfer]:
    """تحويلات اعتماد لم يُلغَ effectها بعد (لم يُعكس لها قيد)."""
    forwards = _forward_handoff_transfers(db, shift_id)
    reverses = _reverse_handoff_transfers(db, shift_id)
    used_rev: set[int] = set()
    active: list[PaymentTransfer] = []
    for fwd in forwards:
        matched = False
        for rev in reverses:
            if rev.id in used_rev:
                continue
            if (
                rev.from_payment_method_id == fwd.to_payment_method_id
                and rev.to_payment_method_id == fwd.from_payment_method_id
                and rev.amount == fwd.amount
            ):
                used_rev.add(rev.id)
                matched = True
                break
        if not matched:
            active.append(fwd)
    return active


def can_revoke_shift_handoff(db: Session, shift_id: int) -> bool:
    sh = db.get(PosShift, shift_id)
    if sh is None or sh.treasury_handoff_at is None:
        return False
    later = int(
        db.execute(
            select(func.count())
            .select_from(PosShift)
            .where(
                PosShift.id > shift_id,
                PosShift.treasury_handoff_at.isnot(None),
            )
        ).scalar_one()
        or 0
    )
    if later > 0:
        return False
    return len(_active_handoff_transfers(db, shift_id)) > 0


def revoke_shift_handoff(
    db: Session,
    *,
    shift_id: int,
    user_id: int,
    admin_username: str = "",
    reason: str = "",
) -> None:
    sh = db.get(PosShift, shift_id)
    if sh is None or sh.status != PosShiftStatus.CLOSED:
        raise ShiftHandoffError("الجلسة غير موجودة أو لم تُغلَق بعد.")
    if sh.treasury_handoff_at is None:
        raise ShiftHandoffError("لم يُعتمد إيراد هذه الجلسة بعد.")
    if not can_revoke_shift_handoff(db, shift_id):
        later = db.execute(
            select(PosShift.id)
            .where(
                PosShift.id > shift_id,
                PosShift.treasury_handoff_at.isnot(None),
            )
            .order_by(PosShift.id.asc())
            .limit(1)
        ).scalar_one_or_none()
        if later is not None:
            raise ShiftHandoffError(
                f"لا يمكن إلغاء اعتماد جلسة #{shift_id} قبل إلغاء اعتماد الجلسات الأحدث "
                f"(مثل #{later})."
            )
        raise ShiftHandoffError("لا توجد تحويلات اعتماد نشطة لهذه الجلسة.")

    reason = (reason or "").strip()
    if len(reason) < 3:
        raise ShiftHandoffError("اذكر سبب إلغاء الاعتماد (3 أحرف على الأقل).")

    active = _active_handoff_transfers(db, shift_id)
    for tr in active:
        record_manual_transfer(
            db,
            from_payment_method_id=tr.to_payment_method_id,
            to_payment_method_id=tr.from_payment_method_id,
            amount=tr.amount,
            user_id=user_id,
            note=f"{_HANDOFF_REVERSE_PREFIX}{shift_id} — {reason}",
            transfer_type=PaymentTransferType.SHIFT_HANDOFF,
        )

    from modules.pos_shifts.models import PosShiftShortage
    from modules.pos_shifts.shortages import _shortage_is_resolved

    for row in db.scalars(
        select(PosShiftShortage).where(PosShiftShortage.shift_id == shift_id)
    ).all():
        if not _shortage_is_resolved(row):
            db.delete(row)

    sh.treasury_handoff_at = None
    sh.treasury_handoff_by_id = None
    line = f"[إلغاء اعتماد الخزينة — {admin_username or user_id}: {reason}]"
    prev = (sh.closing_note or "").strip()
    sh.closing_note = (prev + " | " + line).strip(" | ") if prev else line
    db.flush()
