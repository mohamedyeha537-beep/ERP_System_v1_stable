"""تسليم عهدة قبل/عند الإقفال + إعلان تحويل مصرفي."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from modules.payments.shift_handover_models import (
    ShiftHandover,
    ShiftHandoverKind,
    ShiftHandoverStatus,
)
from modules.payments.shift_variances import money3, record_pair_variances
from modules.payments.shift_variance_models import ShiftVarianceSource


class ShiftHandoverError(ValueError):
    pass


def _next_ref(db: Session) -> str:
    raw = db.scalar(select(func.max(ShiftHandover.id)))
    return f"HO-{int(raw or 0) + 1:05d}"


def _emit(db: Session, event_key: str, row: ShiftHandover, extra: dict | None = None) -> None:
    try:
        from modules.notifications.service import emit_event_safe

        from_name = ""
        to_name = ""
        if row.from_employee_id:
            from modules.hr.models import Employee

            emp = db.get(Employee, int(row.from_employee_id))
            from_name = (getattr(emp, "full_name_ar", None) or "").strip()
        if row.to_employee_id:
            from modules.hr.models import Employee

            emp = db.get(Employee, int(row.to_employee_id))
            to_name = (getattr(emp, "full_name_ar", None) or "").strip()
        payload = {
            "handover_id": row.id,
            "ref": row.ref,
            "kind": row.kind.value if hasattr(row.kind, "value") else str(row.kind),
            "domain": row.domain,
            "from_name": from_name,
            "to_name": to_name,
            "claimed_cash": f"{money3(row.claimed_cash):.3f}",
            "claimed_bank": f"{money3(row.claimed_bank):.3f}",
            "received_cash": f"{money3(row.received_cash):.3f}" if row.received_cash is not None else "",
            "received_bank": f"{money3(row.received_bank):.3f}" if row.received_bank is not None else "",
            "bank_name": row.bank_name or "",
            "bank_ref": row.bank_ref or "",
            "shift_id": row.hotel_shift_id or row.pos_shift_id or 0,
            "hub_detail": f"{row.ref} {from_name} → {to_name}",
        }
        if extra:
            payload.update(extra)
        emit_event_safe(
            db,
            event_key=event_key,
            source_type="shift_handover",
            source_id=row.id,
            payload=payload,
        )
    except Exception:  # noqa: BLE001
        pass


def list_shift_handovers(
    db: Session,
    *,
    hotel_shift_id: int | None = None,
    pos_shift_id: int | None = None,
    kind: ShiftHandoverKind | None = None,
    statuses: tuple[ShiftHandoverStatus, ...] | None = None,
) -> list[ShiftHandover]:
    stmt = select(ShiftHandover).options(
        selectinload(ShiftHandover.from_employee),
        selectinload(ShiftHandover.to_employee),
    )
    if hotel_shift_id is not None:
        stmt = stmt.where(ShiftHandover.hotel_shift_id == int(hotel_shift_id))
    if pos_shift_id is not None:
        stmt = stmt.where(ShiftHandover.pos_shift_id == int(pos_shift_id))
    if kind is not None:
        stmt = stmt.where(ShiftHandover.kind == kind)
    if statuses:
        stmt = stmt.where(ShiftHandover.status.in_(statuses))
    return list(db.scalars(stmt.order_by(ShiftHandover.id.asc())).all())


def handed_claimed_totals(
    db: Session, *, hotel_shift_id: int | None = None, pos_shift_id: int | None = None
) -> tuple[Decimal, Decimal]:
    rows = list_shift_handovers(
        db,
        hotel_shift_id=hotel_shift_id,
        pos_shift_id=pos_shift_id,
        kind=ShiftHandoverKind.CASH_CARRY,
        statuses=(ShiftHandoverStatus.SENT, ShiftHandoverStatus.CONFIRMED),
    )
    cash = sum((money3(r.claimed_cash) for r in rows), Decimal("0"))
    bank = sum((money3(r.claimed_bank) for r in rows), Decimal("0"))
    return money3(cash), money3(bank)


def last_cash_handover_at(
    db: Session, *, hotel_shift_id: int | None = None, pos_shift_id: int | None = None
) -> datetime | None:
    rows = list_shift_handovers(
        db,
        hotel_shift_id=hotel_shift_id,
        pos_shift_id=pos_shift_id,
        kind=ShiftHandoverKind.CASH_CARRY,
        statuses=(ShiftHandoverStatus.SENT, ShiftHandoverStatus.CONFIRMED),
    )
    if not rows:
        return None
    return max(r.handover_at for r in rows if r.handover_at)


def propose_cash_carry(
    db: Session,
    *,
    domain: str,
    hotel_shift_id: int | None = None,
    pos_shift_id: int | None = None,
    from_employee_id: int | None,
    to_employee_id: int,
    claimed_cash,
    claimed_bank,
    user_id: int | None,
    note: str | None = None,
) -> ShiftHandover:
    cash = money3(claimed_cash)
    bank = money3(claimed_bank)
    if cash < 0 or bank < 0:
        raise ShiftHandoverError("مبلغ التسليم غير صالح.")
    if cash == 0 and bank == 0:
        raise ShiftHandoverError("أدخل مبلغ العهدة المراد تسليمها.")
    if not to_employee_id:
        raise ShiftHandoverError("اختر الموظف المستلم.")
    row = ShiftHandover(
        ref=_next_ref(db),
        kind=ShiftHandoverKind.CASH_CARRY,
        status=ShiftHandoverStatus.SENT,
        domain=domain,
        hotel_shift_id=int(hotel_shift_id) if hotel_shift_id else None,
        pos_shift_id=int(pos_shift_id) if pos_shift_id else None,
        from_employee_id=int(from_employee_id) if from_employee_id else None,
        to_employee_id=int(to_employee_id),
        claimed_cash=cash,
        claimed_bank=bank,
        created_by_id=user_id,
        note=(note or "").strip() or None,
    )
    db.add(row)
    db.flush()
    if not row.ref:
        row.ref = f"HO-{int(row.id):05d}"
    from modules.notifications.events import SHIFT_HANDOVER_SENT

    _emit(db, SHIFT_HANDOVER_SENT, row)
    return row


def confirm_cash_carry(
    db: Session,
    *,
    handover_id: int,
    received_cash,
    received_bank,
    user_id: int | None,
    confirmer_employee_id: int | None = None,
) -> ShiftHandover:
    row = db.get(ShiftHandover, int(handover_id))
    if row is None or row.kind != ShiftHandoverKind.CASH_CARRY:
        raise ShiftHandoverError("عملية التسليم غير موجودة.")
    if row.status != ShiftHandoverStatus.SENT:
        raise ShiftHandoverError("هذا التسليم عولج مسبقاً.")
    if (
        confirmer_employee_id
        and row.to_employee_id
        and int(row.to_employee_id) != int(confirmer_employee_id)
    ):
        raise ShiftHandoverError("هذا التسليم موجّه لموظف آخر.")
    rec_c = money3(received_cash)
    rec_b = money3(received_bank)
    if rec_c < 0 or rec_b < 0:
        raise ShiftHandoverError("المبلغ المستلم غير صالح.")
    row.received_cash = rec_c
    row.received_bank = rec_b
    row.confirmed_at = datetime.now(timezone.utc)
    row.confirmed_by_id = user_id
    if confirmer_employee_id:
        row.to_employee_id = int(confirmer_employee_id)
    row.status = ShiftHandoverStatus.CONFIRMED
    src = (
        ShiftVarianceSource.HOTEL_CARRY
        if row.domain == "hotel"
        else ShiftVarianceSource.POS_CARRY
    )
    record_pair_variances(
        db,
        source_type=src,
        claimed_cash=row.claimed_cash,
        received_cash=rec_c,
        claimed_bank=row.claimed_bank,
        received_bank=rec_b,
        hotel_shift_id=row.hotel_shift_id,
        pos_shift_id=row.pos_shift_id,
        from_employee_id=row.from_employee_id,
        to_employee_id=row.to_employee_id,
        note=f"تأكيد تسليم {row.ref}",
    )
    db.flush()
    from modules.notifications.events import SHIFT_HANDOVER_CONFIRMED, SHIFT_VARIANCE_OPENED

    extra = {}
    if rec_c != money3(row.claimed_cash) or rec_b != money3(row.claimed_bank):
        extra["variance"] = "1"
        extra["diff_cash"] = f"{(rec_c - money3(row.claimed_cash)):.3f}"
        extra["diff_bank"] = f"{(rec_b - money3(row.claimed_bank)):.3f}"
    _emit(db, SHIFT_HANDOVER_CONFIRMED, row, extra)
    if extra.get("variance"):
        _emit(db, SHIFT_VARIANCE_OPENED, row, extra)
    return row


def declare_bank_transfer(
    db: Session,
    *,
    domain: str,
    hotel_shift_id: int | None = None,
    pos_shift_id: int | None = None,
    from_employee_id: int | None,
    amount,
    bank_name: str,
    bank_ref: str,
    transferred_at: datetime | None,
    user_id: int | None,
    note: str | None = None,
) -> ShiftHandover:
    amt = money3(amount)
    if amt <= 0:
        raise ShiftHandoverError("أدخل مبلغ التحويل المصرفي.")
    name = (bank_name or "").strip()
    ref = (bank_ref or "").strip()
    if len(name) < 2:
        raise ShiftHandoverError("أدخل اسم المصرف.")
    if len(ref) < 2:
        raise ShiftHandoverError("أدخل رقم العملية.")
    existing = list_shift_handovers(
        db,
        hotel_shift_id=hotel_shift_id,
        pos_shift_id=pos_shift_id,
        kind=ShiftHandoverKind.BANK_TRANSFER,
        statuses=(ShiftHandoverStatus.SENT, ShiftHandoverStatus.CONFIRMED),
    )
    if existing:
        row = existing[-1]
        if row.status == ShiftHandoverStatus.CONFIRMED:
            raise ShiftHandoverError("تم اعتماد هذا التحويل مسبقاً.")
        row.claimed_bank = amt
        row.bank_name = name
        row.bank_ref = ref
        row.bank_transferred_at = transferred_at or datetime.now(timezone.utc)
        row.note = (note or "").strip() or row.note
        db.flush()
        return row
    row = ShiftHandover(
        ref=_next_ref(db),
        kind=ShiftHandoverKind.BANK_TRANSFER,
        status=ShiftHandoverStatus.SENT,
        domain=domain,
        hotel_shift_id=int(hotel_shift_id) if hotel_shift_id else None,
        pos_shift_id=int(pos_shift_id) if pos_shift_id else None,
        from_employee_id=int(from_employee_id) if from_employee_id else None,
        claimed_cash=Decimal("0"),
        claimed_bank=amt,
        bank_name=name,
        bank_ref=ref,
        bank_transferred_at=transferred_at or datetime.now(timezone.utc),
        created_by_id=user_id,
        note=(note or "").strip() or None,
    )
    db.add(row)
    db.flush()
    from modules.notifications.events import SHIFT_BANK_TRANSFER_DECLARED

    _emit(db, SHIFT_BANK_TRANSFER_DECLARED, row)
    return row


def get_bank_declaration(
    db: Session, *, hotel_shift_id: int | None = None, pos_shift_id: int | None = None
) -> ShiftHandover | None:
    rows = list_shift_handovers(
        db,
        hotel_shift_id=hotel_shift_id,
        pos_shift_id=pos_shift_id,
        kind=ShiftHandoverKind.BANK_TRANSFER,
        statuses=(ShiftHandoverStatus.SENT, ShiftHandoverStatus.CONFIRMED),
    )
    return rows[-1] if rows else None


def require_bank_declaration_if_needed(db: Session, *, bank_amount, hotel_shift_id=None, pos_shift_id=None) -> None:
    if money3(bank_amount) <= 0:
        return
    if get_bank_declaration(db, hotel_shift_id=hotel_shift_id, pos_shift_id=pos_shift_id) is None:
        raise ShiftHandoverError(
            "أعلن التحويل المصرفي أولاً (رقم العملية واسم المصرف) قبل اعتماد الخزينة."
        )


@dataclass
class HandoverProgress:
    handed_cash: Decimal
    handed_bank: Decimal
    last_at: datetime | None
    expected_cash: Decimal
    expected_bank: Decimal
    remainder_cash: Decimal
    remainder_bank: Decimal
    rows: list


def _progress(
    db: Session,
    *,
    hotel_shift_id: int | None = None,
    pos_shift_id: int | None = None,
    expected_cash,
    expected_bank,
) -> HandoverProgress:
    handed_c, handed_b = handed_claimed_totals(
        db, hotel_shift_id=hotel_shift_id, pos_shift_id=pos_shift_id
    )
    exp_c, exp_b = money3(expected_cash), money3(expected_bank)
    rows = list_shift_handovers(
        db,
        hotel_shift_id=hotel_shift_id,
        pos_shift_id=pos_shift_id,
        kind=ShiftHandoverKind.CASH_CARRY,
        statuses=(ShiftHandoverStatus.SENT, ShiftHandoverStatus.CONFIRMED),
    )
    return HandoverProgress(
        handed_cash=handed_c,
        handed_bank=handed_b,
        last_at=last_cash_handover_at(
            db, hotel_shift_id=hotel_shift_id, pos_shift_id=pos_shift_id
        ),
        expected_cash=exp_c,
        expected_bank=exp_b,
        remainder_cash=max(exp_c - handed_c, Decimal("0")),
        remainder_bank=max(exp_b - handed_b, Decimal("0")),
        rows=rows,
    )


def hotel_handover_progress(db: Session, shift, *, expected_cash, expected_bank) -> HandoverProgress:
    return _progress(
        db,
        hotel_shift_id=shift.id,
        expected_cash=expected_cash,
        expected_bank=expected_bank,
    )


def pos_handover_progress(db: Session, shift, *, expected_cash, expected_bank) -> HandoverProgress:
    return _progress(
        db,
        pos_shift_id=shift.id,
        expected_cash=expected_cash,
        expected_bank=expected_bank,
    )


def confirmed_received_totals(
    db: Session, *, hotel_shift_id: int | None = None, pos_shift_id: int | None = None
) -> tuple[Decimal, Decimal]:
    rows = list_shift_handovers(
        db,
        hotel_shift_id=hotel_shift_id,
        pos_shift_id=pos_shift_id,
        kind=ShiftHandoverKind.CASH_CARRY,
        statuses=(ShiftHandoverStatus.CONFIRMED,),
    )
    cash = sum((money3(r.received_cash) for r in rows), Decimal("0"))
    bank = sum((money3(r.received_bank) for r in rows), Decimal("0"))
    return money3(cash), money3(bank)


def last_cash_handover_recipient(
    db: Session, *, hotel_shift_id: int | None = None, pos_shift_id: int | None = None
) -> int | None:
    rows = list_shift_handovers(
        db,
        hotel_shift_id=hotel_shift_id,
        pos_shift_id=pos_shift_id,
        kind=ShiftHandoverKind.CASH_CARRY,
        statuses=(ShiftHandoverStatus.SENT, ShiftHandoverStatus.CONFIRMED),
    )
    if not rows:
        return None
    last = rows[-1]
    return int(last.to_employee_id) if last.to_employee_id else None


def list_incoming_cash_handovers(db: Session, employee_id: int) -> list[ShiftHandover]:
    if not employee_id:
        return []
    stmt = (
        select(ShiftHandover)
        .options(
            selectinload(ShiftHandover.from_employee),
            selectinload(ShiftHandover.to_employee),
        )
        .where(
            ShiftHandover.kind == ShiftHandoverKind.CASH_CARRY,
            ShiftHandover.status == ShiftHandoverStatus.SENT,
            ShiftHandover.to_employee_id == int(employee_id),
        )
        .order_by(ShiftHandover.id.asc())
    )
    return list(db.scalars(stmt).all())


def list_unconfirmed_cash_handovers(
    db: Session, *, hotel_shift_id: int | None = None, pos_shift_id: int | None = None
) -> list[ShiftHandover]:
    return list_shift_handovers(
        db,
        hotel_shift_id=hotel_shift_id,
        pos_shift_id=pos_shift_id,
        kind=ShiftHandoverKind.CASH_CARRY,
        statuses=(ShiftHandoverStatus.SENT,),
    )


def mark_bank_declaration_confirmed(
    db: Session,
    *,
    hotel_shift_id: int | None = None,
    pos_shift_id: int | None = None,
    received_bank=None,
    user_id: int | None = None,
) -> ShiftHandover | None:
    row = get_bank_declaration(
        db, hotel_shift_id=hotel_shift_id, pos_shift_id=pos_shift_id
    )
    if row is None:
        return None
    if row.status == ShiftHandoverStatus.CONFIRMED:
        return row
    row.status = ShiftHandoverStatus.CONFIRMED
    row.confirmed_at = datetime.now(timezone.utc)
    row.confirmed_by_id = user_id
    if received_bank is not None:
        row.received_bank = money3(received_bank)
    db.flush()
    return row


def parse_bank_transferred_at(raw: str | None) -> datetime | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        from app.datetime_local import store_timezone

        dt = dt.replace(tzinfo=store_timezone())
    return dt.astimezone(timezone.utc)


def list_pending_bank_transfers(db: Session, *, limit: int = 80) -> list[ShiftHandover]:
    stmt = (
        select(ShiftHandover)
        .options(
            selectinload(ShiftHandover.from_employee),
            selectinload(ShiftHandover.to_employee),
        )
        .where(
            ShiftHandover.kind == ShiftHandoverKind.BANK_TRANSFER,
            ShiftHandover.status == ShiftHandoverStatus.SENT,
        )
        .order_by(ShiftHandover.id.asc())
        .limit(max(1, min(int(limit), 200)))
    )
    return list(db.scalars(stmt).all())


def match_bank_transfer(
    db: Session,
    *,
    handover_id: int,
    received_bank,
    user_id: int | None,
    note: str | None = None,
) -> ShiftHandover:
    """مطابقة أمين الخزينة لمبلغ ظهر في حساب الخزينة — قبل أو مع اعتماد الجلسة."""
    row = db.get(ShiftHandover, int(handover_id))
    if row is None or row.kind != ShiftHandoverKind.BANK_TRANSFER:
        raise ShiftHandoverError("إعلان التحويل المصرفي غير موجود.")
    if row.status == ShiftHandoverStatus.CANCELLED:
        raise ShiftHandoverError("هذا الإعلان ملغي.")
    rec = money3(received_bank)
    if rec < 0:
        raise ShiftHandoverError("المبلغ الظاهر في الحساب غير صالح.")
    row.received_bank = rec
    if row.status != ShiftHandoverStatus.CONFIRMED:
        row.status = ShiftHandoverStatus.CONFIRMED
        row.confirmed_at = datetime.now(timezone.utc)
        row.confirmed_by_id = user_id
    extra = (note or "").strip()
    if extra:
        prev = (row.note or "").strip()
        row.note = (prev + " | " + extra).strip(" | ") if prev else extra
    # الفرق يُسجَّل عند اعتماد الجلسة (مرة واحدة) حتى لا يُكرَّر سجل العجز.
    if row.pos_shift_id:
        from modules.pos_shifts.models import PosShift

        sh = db.get(PosShift, int(row.pos_shift_id))
        if sh is not None and getattr(sh, "treasury_handoff_at", None) is None:
            sh.counted_bank = rec
    if row.hotel_shift_id:
        from modules.hotel.shift_models import HotelShift

        sh = db.get(HotelShift, int(row.hotel_shift_id))
        if sh is not None and getattr(sh, "treasury_handoff_at", None) is None:
            sh.counted_bank = rec
    db.flush()
    return row
