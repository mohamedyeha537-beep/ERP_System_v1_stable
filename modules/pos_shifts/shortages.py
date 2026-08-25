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
    source: str = "pos"
    report_href: str = ""


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


def convert_open_shortage_to_deduction(
    db: Session,
    shortage: PosShiftShortage,
    *,
    resolved_by_id: int | None = None,
    note: str | None = None,
    employee_id_override: int | None = None,
):
    """يحوّل عجز جلسة مفتوحاً إلى خصم راتب مستحق (EmployeeDeduction).

    إن وُجد خصم مسبقاً لنفس المصدر يُربَط به دون تكرار.
    """
    from datetime import datetime, timezone

    from modules.hr.models import DeductionStatus, EmployeeDeduction
    from modules.hr.service import HRError, create_employee_deduction

    if shortage is None:
        return None
    if _shortage_is_resolved(shortage) and shortage.payroll_deduction_id:
        return db.get(EmployeeDeduction, shortage.payroll_deduction_id)
    amt = Decimal(str(shortage.shortage_amount or 0)).quantize(Decimal("0.001"))
    if amt <= 0 and shortage.payroll_deduction_id:
        return db.get(EmployeeDeduction, shortage.payroll_deduction_id)
    if amt <= 0:
        return None
    emp_id = int(shortage.employee_id or employee_id_override or 0)
    if emp_id <= 0:
        return None

    note_text = (note or "").strip() or (
        f"عجز {shortage_kind_label_ar(shortage.kind)} جلسة #{shortage.shift_id} "
        f"— خصم تلقائي من الراتب"
    )
    ded = None
    try:
        ded = create_employee_deduction(
            db,
            employee_id=emp_id,
            amount=amt,
            note=note_text,
            source_type="POS_SHIFT_SHORTAGE",
            source_id=shortage.id,
            resolved_by_id=resolved_by_id,
        )
    except HRError as exc:
        msg = str(exc)
        if "مسبقاً" not in msg and "تكراره" not in msg:
            raise
        ded = db.scalar(
            select(EmployeeDeduction).where(
                EmployeeDeduction.employee_id == emp_id,
                EmployeeDeduction.source_type == "POS_SHIFT_SHORTAGE",
                EmployeeDeduction.source_id == shortage.id,
                EmployeeDeduction.status != DeductionStatus.CANCELLED,
            )
        )
        if ded is None:
            raise

    if shortage.employee_id is None:
        shortage.employee_id = emp_id
    # اربط الجلسة أيضاً إن كانت بدون موظف (إغلاق بأدمن)
    sh = db.get(PosShift, int(shortage.shift_id)) if shortage.shift_id else None
    if sh is not None and sh.employee_id is None:
        sh.employee_id = emp_id
    if shortage.original_shortage_amount is None:
        shortage.original_shortage_amount = amt
    shortage.resolved_action = "DEDUCT"
    shortage.resolved_note = (note or "").strip() or "خصم تلقائي من الراتب"
    shortage.resolved_at = datetime.now(timezone.utc)
    if resolved_by_id is not None:
        shortage.resolved_by_id = resolved_by_id
    shortage.payroll_deduction_id = ded.id
    shortage.shortage_amount = Decimal("0.000")
    db.flush()
    return ded


def list_unassigned_open_shortages(
    db: Session, *, limit: int = 100
) -> list[ShiftShortageRow]:
    """عجز مفتوح بلا موظف — يظهر عند إغلاق الجلسة بحساب أدمن بدون ربط موظف."""
    return [r for r in list_shift_shortages(db, limit=limit) if not r.employee_id]


def auto_convert_open_shortages_to_deductions(
    db: Session,
    *,
    employee_ids: list[int] | None = None,
    resolved_by_id: int | None = None,
) -> int:
    """يحوّل كل عجز مفتوح مرتبط بموظف إلى خصم راتب — يُستدعى قبل حساب دفعة الرواتب."""
    stmt = select(PosShiftShortage).where(PosShiftShortage.shortage_amount > 0)
    if employee_ids is not None:
        if not employee_ids:
            return 0
        stmt = stmt.where(PosShiftShortage.employee_id.in_(employee_ids))
    else:
        stmt = stmt.where(PosShiftShortage.employee_id.is_not(None))
    count = 0
    for row in list(db.scalars(stmt).all()):
        if _shortage_is_resolved(row) and row.payroll_deduction_id:
            continue
        try:
            if convert_open_shortage_to_deduction(
                db, row, resolved_by_id=resolved_by_id
            ):
                count += 1
        except Exception:  # noqa: BLE001
            continue
    return count


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
                source="pos",
                report_href=f"/pos/shift/{r.shift_id}/report",
            )
        )
    out.extend(_list_hotel_shift_shortages(db, kind_filter=kind_filter, limit=limit))

    def _closed_sort_key(row: ShiftShortageRow):
        dt = row.closed_at
        if dt is None:
            return (datetime.min, row.shift_id)
        if getattr(dt, "tzinfo", None) is not None:
            dt = dt.replace(tzinfo=None)
        return (dt, row.shift_id)

    out.sort(key=_closed_sort_key, reverse=True)
    return out[:limit]


def _list_hotel_shift_shortages(
    db: Session,
    *,
    kind_filter: str | None = None,
    limit: int = 500,
) -> list[ShiftShortageRow]:
    """عجز جلسات الفندق (لا يُحفظ في جدول عجز المطعم)."""
    from modules.hotel.shift_models import HotelShift, HotelShiftStatus

    want = (kind_filter or "").strip().upper()
    shifts = list(
        db.scalars(
            select(HotelShift)
            .options(
                selectinload(HotelShift.employee),
                selectinload(HotelShift.user),
            )
            .where(HotelShift.status == HotelShiftStatus.CLOSED)
            .order_by(HotelShift.closed_at.desc(), HotelShift.id.desc())
            .limit(max(50, min(int(limit), 500)))
        ).all()
    )
    out: list[ShiftShortageRow] = []
    for sh in shifts:
        pairs = (
            (ShortageKind.CASH, sh.cash_difference, sh.expected_cash, sh.counted_cash),
            (ShortageKind.BANK, sh.bank_difference, sh.expected_bank, sh.counted_bank),
        )
        for kind, diff, expected, counted in pairs:
            amt = _shortage_amount(diff)
            if amt is None or amt <= 0:
                continue
            if want in ("CASH", "BANK") and str(getattr(kind, "value", kind)) != want:
                continue
            uname = (sh.user.username if sh.user else "") or "—"
            emp_name = sh.employee.full_name_ar if sh.employee else None
            kind_code = 1 if shortage_kind_is_cash(kind) else 2
            out.append(
                ShiftShortageRow(
                    id=-(int(sh.id) * 10 + kind_code),
                    shift_id=sh.id,
                    kind=kind,
                    is_cash=shortage_kind_is_cash(kind),
                    kind_ar=shortage_kind_label_ar(kind),
                    shortage_amount=amt,
                    expected_amount=Decimal(str(expected or 0)).quantize(Decimal("0.001")),
                    counted_amount=Decimal(str(counted or 0)).quantize(Decimal("0.001")),
                    difference=Decimal(str(diff or 0)).quantize(Decimal("0.001")),
                    closed_at=sh.closed_at,
                    opened_at=sh.opened_at,
                    user_id=int(sh.user_id or 0),
                    username=uname,
                    employee_id=sh.employee_id,
                    employee_name=emp_name,
                    closing_note=sh.closing_note,
                    opening_note=sh.opening_note,
                    source="hotel",
                    report_href=f"/admin/hotel/shift/{sh.id}/report",
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
