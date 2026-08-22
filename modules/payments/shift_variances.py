"""إنشاء ومعالجة فروقات العهدة والخزينة — بدون خصم راتب تلقائي."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from modules.payments.shift_variance_models import (
    ShiftVariance,
    ShiftVarianceKind,
    ShiftVarianceSource,
    ShiftVarianceStatus,
)


class ShiftVarianceError(ValueError):
    pass


STATUS_LABELS = {
    ShiftVarianceStatus.PENDING_REVIEW: "قيد المراجعة",
    ShiftVarianceStatus.CHARGED: "تم تحميل الموظف",
    ShiftVarianceStatus.ADMIN_SETTLED: "تسوية إدارية",
    ShiftVarianceStatus.CANCELLED: "ملغي بعد التحقيق",
}

SOURCE_LABELS = {
    ShiftVarianceSource.HOTEL_CARRY: "تسليم عهدة بين ورديات",
    ShiftVarianceSource.HOTEL_CLOSE: "عدّ إقفال فندق",
    ShiftVarianceSource.HOTEL_TREASURY: "اعتماد خزينة فندق",
    ShiftVarianceSource.POS_TREASURY: "اعتماد خزينة مطعم",
    ShiftVarianceSource.POS_CARRY: "تسليم عهدة مطعم",
    ShiftVarianceSource.TREASURY_CLOSE: "إقفال خزينة أمين الخزينة",
    ShiftVarianceSource.TREASURY_BANK_MATCH: "مطابقة تحويل مصرفي",
}


def money3(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.001"))


def _next_ref(db: Session) -> str:
    raw = db.scalar(select(func.max(ShiftVariance.id)))
    n = int(raw or 0) + 1
    return f"VAR-{n:05d}"


def _existing(
    db: Session,
    *,
    source_type: ShiftVarianceSource,
    kind: ShiftVarianceKind,
    hotel_shift_id: int | None,
    pos_shift_id: int | None,
) -> ShiftVariance | None:
    stmt = select(ShiftVariance).where(
        ShiftVariance.source_type == source_type,
        ShiftVariance.kind == kind,
    )
    if hotel_shift_id is not None:
        stmt = stmt.where(ShiftVariance.hotel_shift_id == int(hotel_shift_id))
    else:
        stmt = stmt.where(ShiftVariance.hotel_shift_id.is_(None))
    if pos_shift_id is not None:
        stmt = stmt.where(ShiftVariance.pos_shift_id == int(pos_shift_id))
    else:
        stmt = stmt.where(ShiftVariance.pos_shift_id.is_(None))
    return db.scalar(stmt)


def create_shift_variance(
    db: Session,
    *,
    source_type: ShiftVarianceSource | str,
    kind: ShiftVarianceKind | str,
    claimed_amount,
    received_amount,
    hotel_shift_id: int | None = None,
    pos_shift_id: int | None = None,
    from_employee_id: int | None = None,
    to_employee_id: int | None = None,
    note: str | None = None,
    force_new: bool = False,
) -> ShiftVariance | None:
    """ينشئ سجلاً فقط إذا وُجد فرق. لا يغيّر مبلغ المسلِّم ولا يخصم من الراتب."""
    if isinstance(source_type, str):
        source_type = ShiftVarianceSource(source_type)
    if isinstance(kind, str):
        kind = ShiftVarianceKind(kind)
    claimed = money3(claimed_amount)
    received = money3(received_amount)
    diff = (received - claimed).quantize(Decimal("0.001"))
    if diff == 0:
        return None
    found = None if force_new else _existing(
        db,
        source_type=source_type,
        kind=kind,
        hotel_shift_id=hotel_shift_id,
        pos_shift_id=pos_shift_id,
    )
    if found is not None:
        if found.status != ShiftVarianceStatus.PENDING_REVIEW:
            return found
        found.claimed_amount = claimed
        found.received_amount = received
        found.difference = diff
        found.from_employee_id = from_employee_id
        found.to_employee_id = to_employee_id
        if note:
            found.note = (note or "").strip() or found.note
        db.flush()
        return found
    row = ShiftVariance(
        ref=_next_ref(db),
        source_type=source_type,
        kind=kind,
        status=ShiftVarianceStatus.PENDING_REVIEW,
        hotel_shift_id=int(hotel_shift_id) if hotel_shift_id else None,
        pos_shift_id=int(pos_shift_id) if pos_shift_id else None,
        from_employee_id=int(from_employee_id) if from_employee_id else None,
        to_employee_id=int(to_employee_id) if to_employee_id else None,
        claimed_amount=claimed,
        received_amount=received,
        difference=diff,
        note=(note or "").strip() or None,
    )
    db.add(row)
    db.flush()
    if not row.ref:
        row.ref = f"VAR-{int(row.id):05d}"
    return row


def record_pair_variances(
    db: Session,
    *,
    source_type: ShiftVarianceSource,
    claimed_cash,
    received_cash,
    claimed_bank,
    received_bank,
    hotel_shift_id: int | None = None,
    pos_shift_id: int | None = None,
    from_employee_id: int | None = None,
    to_employee_id: int | None = None,
    note: str | None = None,
) -> list[ShiftVariance]:
    out: list[ShiftVariance] = []
    for kind, claimed, received in (
        (ShiftVarianceKind.CASH, claimed_cash, received_cash),
        (ShiftVarianceKind.BANK, claimed_bank, received_bank),
    ):
        row = create_shift_variance(
            db,
            source_type=source_type,
            kind=kind,
            claimed_amount=claimed,
            received_amount=received,
            hotel_shift_id=hotel_shift_id,
            pos_shift_id=pos_shift_id,
            from_employee_id=from_employee_id,
            to_employee_id=to_employee_id,
            note=note,
        )
        if row is not None:
            out.append(row)
    return out


@dataclass
class ShiftVarianceRow:
    id: int
    ref: str
    source_type: str
    source_ar: str
    kind: str
    kind_ar: str
    status: str
    status_ar: str
    claimed: Decimal
    received: Decimal
    difference: Decimal
    abs_difference: Decimal
    is_shortage: bool
    from_name: str
    to_name: str
    shift_label: str
    report_href: str
    note: str | None
    created_at: datetime | None
    resolved_note: str | None
    can_resolve: bool


def list_shift_variances(
    db: Session,
    *,
    status: str | None = None,
    limit: int = 200,
) -> list[ShiftVarianceRow]:
    stmt = (
        select(ShiftVariance)
        .options(
            selectinload(ShiftVariance.from_employee),
            selectinload(ShiftVariance.to_employee),
        )
        .order_by(ShiftVariance.id.desc())
        .limit(limit)
    )
    if status:
        try:
            st = ShiftVarianceStatus(status)
        except ValueError:
            st = None
        if st is not None:
            stmt = stmt.where(ShiftVariance.status == st)
    rows: list[ShiftVarianceRow] = []
    for v in db.scalars(stmt).all():
        diff = money3(v.difference)
        src = v.source_type
        src_val = src.value if hasattr(src, "value") else str(src)
        kind_val = v.kind.value if hasattr(v.kind, "value") else str(v.kind)
        st_val = v.status.value if hasattr(v.status, "value") else str(v.status)
        href = ""
        label = ""
        if v.hotel_shift_id:
            href = f"/admin/hotel/shift/{v.hotel_shift_id}/report"
            label = f"فندق #{v.hotel_shift_id}"
        elif v.pos_shift_id:
            href = f"/reports/shifts/{v.pos_shift_id}"
            label = f"مطعم #{v.pos_shift_id}"
        from_emp = getattr(v, "from_employee", None)
        to_emp = getattr(v, "to_employee", None)
        rows.append(
            ShiftVarianceRow(
                id=int(v.id),
                ref=v.ref or f"VAR-{v.id:05d}",
                source_type=src_val,
                source_ar=SOURCE_LABELS.get(src, src_val),
                kind=kind_val,
                kind_ar="كاش" if str(kind_val).upper() == "CASH" else "مصرف",
                status=st_val,
                status_ar=STATUS_LABELS.get(v.status, st_val),
                claimed=money3(v.claimed_amount),
                received=money3(v.received_amount),
                difference=diff,
                abs_difference=abs(diff),
                is_shortage=diff < 0,
                from_name=(getattr(from_emp, "full_name_ar", None) or "—"),
                to_name=(getattr(to_emp, "full_name_ar", None) or "—"),
                shift_label=label,
                report_href=href,
                note=v.note,
                created_at=v.created_at,
                resolved_note=v.resolved_note,
                can_resolve=v.status == ShiftVarianceStatus.PENDING_REVIEW,
            )
        )
    return rows


def count_pending_variances(db: Session) -> int:
    raw = db.scalar(
        select(func.count())
        .select_from(ShiftVariance)
        .where(ShiftVariance.status == ShiftVarianceStatus.PENDING_REVIEW)
    )
    return int(raw or 0)


@dataclass
class EmployeeShortageRecurrence:
    employee_id: int
    employee_name: str
    shortage_count: int
    shortage_total: Decimal
    pending_count: int
    charged_count: int
    cancelled_count: int


def employee_shortage_recurrence(
    db: Session, *, limit: int = 40
) -> list[EmployeeShortageRecurrence]:
    """تكرار العجز حسب الموظف المسؤول (المسلِّم) — للمراجعة والتحقيق."""
    rows = list(
        db.scalars(
            select(ShiftVariance)
            .options(selectinload(ShiftVariance.from_employee))
            .where(
                ShiftVariance.difference < 0,
                ShiftVariance.from_employee_id.is_not(None),
            )
            .order_by(ShiftVariance.id.desc())
        ).all()
    )
    by_emp: dict[int, EmployeeShortageRecurrence] = {}
    for v in rows:
        emp_id = int(v.from_employee_id or 0)
        if emp_id <= 0:
            continue
        emp = getattr(v, "from_employee", None)
        name = (getattr(emp, "full_name_ar", None) or "").strip() or f"موظف #{emp_id}"
        bucket = by_emp.get(emp_id)
        if bucket is None:
            bucket = EmployeeShortageRecurrence(
                employee_id=emp_id,
                employee_name=name,
                shortage_count=0,
                shortage_total=money3(0),
                pending_count=0,
                charged_count=0,
                cancelled_count=0,
            )
            by_emp[emp_id] = bucket
        amt = abs(money3(v.difference))
        bucket.shortage_count += 1
        bucket.shortage_total = money3(bucket.shortage_total + amt)
        st = v.status
        if st == ShiftVarianceStatus.PENDING_REVIEW:
            bucket.pending_count += 1
        elif st == ShiftVarianceStatus.CHARGED:
            bucket.charged_count += 1
        elif st == ShiftVarianceStatus.CANCELLED:
            bucket.cancelled_count += 1
    out = sorted(
        by_emp.values(),
        key=lambda r: (r.shortage_count, r.shortage_total),
        reverse=True,
    )
    return out[: max(1, int(limit))]


def _require_pending(db: Session, variance_id: int) -> ShiftVariance:
    row = db.get(ShiftVariance, int(variance_id))
    if row is None:
        raise ShiftVarianceError("سجل الفرق غير موجود.")
    if row.status != ShiftVarianceStatus.PENDING_REVIEW:
        raise ShiftVarianceError("هذا السجل عولج مسبقاً ولا يمكن تغيير حالته إلا بالتحقيق الإداري.")
    return row


def charge_variance_to_employee(
    db: Session,
    *,
    variance_id: int,
    resolved_by_id: int | None,
    note: str | None = None,
    employee_id: int | None = None,
) -> ShiftVariance:
    """تحميل الفرق على الموظف — بقرار إداري فقط، وليس تلقائياً."""
    from modules.hr.service import HRError, create_employee_deduction

    row = _require_pending(db, variance_id)
    emp_id = int(employee_id or row.from_employee_id or 0)
    if emp_id <= 0:
        raise ShiftVarianceError("حدّد الموظف المسؤول عن الفرق.")
    amt = abs(money3(row.difference))
    if amt <= 0:
        raise ShiftVarianceError("لا يوجد مبلغ لتحميله.")
    try:
        ded = create_employee_deduction(
            db,
            employee_id=emp_id,
            amount=amt,
            note=(note or "").strip()
            or f"{row.ref} — فرق {SOURCE_LABELS.get(row.source_type, '')}",
            source_type="SHIFT_VARIANCE",
            source_id=row.id,
            resolved_by_id=resolved_by_id,
        )
    except HRError as exc:
        raise ShiftVarianceError(str(exc)) from exc
    row.status = ShiftVarianceStatus.CHARGED
    row.from_employee_id = emp_id
    row.payroll_deduction_id = ded.id
    row.resolved_at = datetime.now(timezone.utc)
    row.resolved_by_id = resolved_by_id
    row.resolved_note = (note or "").strip() or "تحميل على الموظف بقرار إداري"
    db.flush()
    return row


def settle_variance_admin(
    db: Session,
    *,
    variance_id: int,
    resolved_by_id: int | None,
    note: str,
) -> ShiftVariance:
    row = _require_pending(db, variance_id)
    reason = (note or "").strip()
    if len(reason) < 4:
        raise ShiftVarianceError("اكتب سبب التسوية الإدارية.")
    row.status = ShiftVarianceStatus.ADMIN_SETTLED
    row.resolved_at = datetime.now(timezone.utc)
    row.resolved_by_id = resolved_by_id
    row.resolved_note = reason
    db.flush()
    return row


def cancel_variance_after_investigation(
    db: Session,
    *,
    variance_id: int,
    resolved_by_id: int | None,
    note: str,
) -> ShiftVariance:
    row = _require_pending(db, variance_id)
    reason = (note or "").strip()
    if len(reason) < 4:
        raise ShiftVarianceError("اكتب سبب الإلغاء بعد التحقيق.")
    row.status = ShiftVarianceStatus.CANCELLED
    row.resolved_at = datetime.now(timezone.utc)
    row.resolved_by_id = resolved_by_id
    row.resolved_note = reason
    db.flush()
    return row
