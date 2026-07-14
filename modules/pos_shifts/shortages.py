"""سجل عجز إقفال جلسات الكاشير (كاش / مصرف)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from modules.pos_shifts.models import (
    PosShift,
    PosShiftShortage,
    PosShiftStatus,
    ShortageKind,
)


def shortage_kind_is_cash(kind: ShortageKind | str | None) -> bool:
    """مقارنة آمنة لنوع العجز (enum أو نص من SQLite)."""
    if kind is None:
        return False
    if isinstance(kind, ShortageKind):
        return kind == ShortageKind.CASH
    return str(kind).strip().upper() in ("CASH", ShortageKind.CASH.name)


def shortage_kind_label_ar(kind: ShortageKind | str | None) -> str:
    return "كاش" if shortage_kind_is_cash(kind) else "مصرف"


@dataclass
class ShiftShortageRow:
    id: int
    shift_id: int
    kind: ShortageKind
    is_cash: bool
    kind_ar: str
    shortage_amount: Decimal
    expected_amount: Decimal
    counted_amount: Decimal
    difference: Decimal
    closed_at: datetime | None
    opened_at: datetime | None
    user_id: int
    username: str
    employee_id: int | None
    employee_name: str | None
    closing_note: str | None
    opening_note: str | None


@dataclass
class ShortagesSummary:
    total_shortage: Decimal
    event_count: int
    cash_total: Decimal
    bank_total: Decimal
    cash_count: int
    bank_count: int


def _shortage_amount(diff: Decimal | None) -> Decimal | None:
    if diff is None:
        return None
    d = diff.quantize(Decimal("0.001"))
    if d >= 0:
        return None
    return (-d).quantize(Decimal("0.001"))


def difference_shortage_amount(diff: Decimal | None) -> Decimal:
    """قيمة العجز من فرق العد (معدود − متوقع) — موجب أو صفر."""
    amt = _shortage_amount(diff)
    return amt if amt is not None else Decimal("0")


def difference_surplus_amount(diff: Decimal | None) -> Decimal:
    """قيمة الفائض من فرق العد (معدود − متوقع) — موجب أو صفر."""
    if diff is None:
        return Decimal("0")
    d = diff.quantize(Decimal("0.001"))
    return d if d > 0 else Decimal("0")


def shift_reconciliation_totals(
    cash_difference: Decimal | None,
    bank_difference: Decimal | None,
    *,
    shortage_total: Decimal | None = None,
) -> tuple[Decimal, Decimal]:
    """(عجز, فائض) — العجز يُفضَّل من سجل shortages إن وُجد (بعد استثناء الولاء)."""
    surplus = (
        difference_surplus_amount(cash_difference)
        + difference_surplus_amount(bank_difference)
    ).quantize(Decimal("0.001"))
    if shortage_total is not None:
        shortage = Decimal(str(shortage_total or 0)).quantize(Decimal("0.001"))
    else:
        shortage = (
            difference_shortage_amount(cash_difference)
            + difference_shortage_amount(bank_difference)
        ).quantize(Decimal("0.001"))
    return shortage, surplus


def _shortage_is_resolved(row: PosShiftShortage) -> bool:
    """عجز مُعالَج (خصم راتب أو عفو أو تسوية ولاء) — لا يُعاد فتحه تلقائياً."""
    action = (getattr(row, "resolved_action", None) or "").strip().upper()
    return action in ("DEDUCT", "FORGIVE", "LOYALTY")


def record_shortages_for_closed_shift(
    db: Session,
    sh: PosShift,
    *,
    loyalty_cash_absorbed: Decimal | None = None,
) -> list[PosShiftShortage]:
    """يُسجّل عجز الكاش و/أو المصرف عند إغلاق الجلسة (إن وُجد)."""
    if sh.status != PosShiftStatus.CLOSED:
        return []
    if loyalty_cash_absorbed is None:
        from modules.customers.loyalty_shift_reports import loyalty_redeem_summary_for_shift
        from modules.pos_shifts.loyalty_settlement import loyalty_cash_shortage_absorption

        loyalty = loyalty_redeem_summary_for_shift(db, sh.id)
        raw_cash_short = _shortage_amount(sh.cash_difference) or Decimal("0")
        loyalty_cash_absorbed = loyalty_cash_shortage_absorption(
            raw_cash_short, loyalty.dinar_cost
        )
    loyalty_left = max(
        Decimal(str(loyalty_cash_absorbed or 0)), Decimal("0")
    ).quantize(Decimal("0.001"))
    created: list[PosShiftShortage] = []
    pairs = (
        (ShortageKind.CASH, sh.expected_cash, sh.counted_cash, sh.cash_difference),
        (ShortageKind.BANK, sh.expected_bank, sh.counted_bank, sh.bank_difference),
    )
    for kind, expected, counted, diff in pairs:
        amt = _shortage_amount(diff)
        if amt is None:
            continue
        if kind == ShortageKind.CASH and loyalty_left > 0:
            absorb = min(amt, loyalty_left)
            amt = (amt - absorb).quantize(Decimal("0.001"))
            loyalty_left = (loyalty_left - absorb).quantize(Decimal("0.001"))
            if amt <= 0:
                continue
        existing = db.scalar(
            select(PosShiftShortage).where(
                PosShiftShortage.shift_id == sh.id,
                PosShiftShortage.kind == kind,
            )
        )
        if existing is not None:
            if _shortage_is_resolved(existing):
                continue
            existing.expected_amount = expected
            existing.counted_amount = counted
            existing.difference = diff
            if getattr(existing, "original_shortage_amount", None) is None:
                existing.original_shortage_amount = amt
            existing.shortage_amount = amt
            existing.closed_at = sh.closed_at
            existing.user_id = sh.user_id
            existing.employee_id = sh.employee_id
            existing.closing_note = sh.closing_note
            created.append(existing)
            continue
        row = PosShiftShortage(
            shift_id=sh.id,
            kind=kind,
            expected_amount=expected,
            counted_amount=counted,
            difference=diff,
            original_shortage_amount=amt,
            shortage_amount=amt,
            closed_at=sh.closed_at,
            user_id=sh.user_id,
            employee_id=sh.employee_id,
            closing_note=sh.closing_note,
        )
        db.add(row)
        created.append(row)
    if created:
        db.flush()
    return created


def list_shift_shortages(
    db: Session,
    *,
    kind_filter: str | None = None,
    limit: int = 500,
) -> list[ShiftShortageRow]:
    stmt = (
        select(PosShiftShortage)
        .options(
            selectinload(PosShiftShortage.shift).selectinload(PosShift.user),
            selectinload(PosShiftShortage.shift).selectinload(PosShift.employee),
            selectinload(PosShiftShortage.user),
            selectinload(PosShiftShortage.employee),
        )
        .order_by(PosShiftShortage.closed_at.desc(), PosShiftShortage.id.desc())
        .limit(limit)
    )
    # افتراضاً: اعرض العجز غير المعالج فقط (عندما يصبح 0 يعني تم اتخاذ إجراء)
    stmt = stmt.where(PosShiftShortage.shortage_amount > 0)
    if kind_filter and kind_filter.upper() in ("CASH", "BANK"):
        stmt = stmt.where(PosShiftShortage.kind == ShortageKind(kind_filter.upper()))
    rows = list(db.scalars(stmt).all())
    out: list[ShiftShortageRow] = []
    for r in rows:
        sh = r.shift
        uname = r.user.username if r.user else (sh.user.username if sh and sh.user else "—")
        emp_name = None
        if r.employee is not None:
            emp_name = r.employee.full_name_ar
        elif sh is not None and sh.employee is not None:
            emp_name = sh.employee.full_name_ar
        out.append(
            ShiftShortageRow(
                id=r.id,
                shift_id=r.shift_id,
                kind=r.kind,
                is_cash=shortage_kind_is_cash(r.kind),
                kind_ar=shortage_kind_label_ar(r.kind),
                shortage_amount=Decimal(str(r.shortage_amount or 0)),
                expected_amount=Decimal(str(r.expected_amount or 0)),
                counted_amount=Decimal(str(r.counted_amount or 0)),
                difference=Decimal(str(r.difference or 0)),
                closed_at=r.closed_at,
                opened_at=sh.opened_at if sh else None,
                user_id=r.user_id,
                username=uname,
                employee_id=r.employee_id,
                employee_name=emp_name,
                closing_note=r.closing_note,
                opening_note=sh.opening_note if sh else None,
            )
        )
    return out


def shortages_summary(db: Session) -> ShortagesSummary:
    rows = list_shift_shortages(db, limit=10_000)
    total = Decimal("0")
    cash_t = Decimal("0")
    bank_t = Decimal("0")
    cash_n = bank_n = 0
    for r in rows:
        total += r.shortage_amount
        if shortage_kind_is_cash(r.kind):
            cash_t += r.shortage_amount
            cash_n += 1
        else:
            bank_t += r.shortage_amount
            bank_n += 1
    return ShortagesSummary(
        total_shortage=total.quantize(Decimal("0.001")),
        event_count=len(rows),
        cash_total=cash_t.quantize(Decimal("0.001")),
        bank_total=bank_t.quantize(Decimal("0.001")),
        cash_count=cash_n,
        bank_count=bank_n,
    )


def backfill_shortages_from_closed_shifts(db: Session) -> int:
    """استيراد عجز الجلسات المغلقة التي لا يوجد لها سجل بعد (مرة واحدة لكل جلسة)."""
    shifts = list(
        db.scalars(
            select(PosShift)
            .where(PosShift.status == PosShiftStatus.CLOSED)
            .order_by(PosShift.id.asc())
        ).all()
    )
    n = 0
    for sh in shifts:
        has_row = db.scalar(
            select(PosShiftShortage.id)
            .where(PosShiftShortage.shift_id == sh.id)
            .limit(1)
        )
        if has_row is not None:
            continue
        created = record_shortages_for_closed_shift(db, sh)
        if created:
            n += len(created)
    return n
