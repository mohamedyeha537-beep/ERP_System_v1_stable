from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from modules.authz.models import User
from modules.delivery.models import DeliveryCashSettlement
from modules.payments.models import PaymentMethod, PaymentMethodKind, RefundPayment, SalePayment
from modules.pos_shifts.models import PosShift, PosShiftStatus
from modules.refunds.models import SaleReturn
from modules.sales.models import Sale, SaleStatus


class PosShiftError(Exception):
    pass


def get_shift(db: Session, shift_id: int) -> PosShift | None:
    return db.get(PosShift, shift_id)


def get_open_shift_for_user(db: Session, user_id: int) -> PosShift | None:
    return db.execute(
        select(PosShift)
        .where(
            PosShift.user_id == user_id,
            PosShift.status == PosShiftStatus.OPEN,
        )
        .order_by(PosShift.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def sync_session_pos_shift(request, db: Session, user_id: int) -> PosShift | None:
    """يضبط `session[pos_shift_id]` بحسب الجلسة المفتوحة في قاعدة البيانات."""
    open_s = get_open_shift_for_user(db, user_id)
    if open_s is not None:
        request.session["pos_shift_id"] = open_s.id
        return open_s
    request.session.pop("pos_shift_id", None)
    return None


def require_open_pos_shift(request, db: Session, user: User) -> PosShift | RedirectResponse:
    s = sync_session_pos_shift(request, db, user.id)
    if s is None:
        return RedirectResponse("/pos/shift?need=1", status_code=302)
    return s


def session_pos_shift_id(request) -> int | None:
    raw = request.session.get("pos_shift_id")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def open_shift(
    db: Session,
    user_id: int,
    *,
    opening_note: str | None = None,
) -> PosShift:
    if get_open_shift_for_user(db, user_id) is not None:
        raise PosShiftError("لديك جلسة مفتوحة بالفعل. أغلقها قبل فتح جلسة جديدة.")
    note = (opening_note or "").strip() or None
    sh = PosShift(
        user_id=user_id,
        status=PosShiftStatus.OPEN,
        opening_note=note,
    )
    db.add(sh)
    db.flush()
    return sh


def compute_expected_cash(db: Session, shift_id: int) -> Decimal:
    """نقد متوقع في الدرج = تحصيل كاش الفواتير المرتبطة بالجلسة − أجور توصيل تُخصم من الكاش − مردودات كاش."""
    cash_in = db.execute(
        select(func.coalesce(func.sum(SalePayment.amount), 0))
        .select_from(SalePayment)
        .join(Sale, Sale.id == SalePayment.sale_id)
        .join(PaymentMethod, PaymentMethod.id == SalePayment.payment_method_id)
        .where(
            Sale.pos_shift_id == shift_id,
            PaymentMethod.kind == PaymentMethodKind.CASH,
        )
    ).scalar_one()
    cash_in = Decimal(str(cash_in or 0)).quantize(Decimal("0.001"))

    delivery_out = db.execute(
        select(func.coalesce(func.sum(DeliveryCashSettlement.amount), 0))
        .select_from(DeliveryCashSettlement)
        .join(Sale, Sale.id == DeliveryCashSettlement.sale_id)
        .where(Sale.pos_shift_id == shift_id)
    ).scalar_one()
    delivery_out = Decimal(str(delivery_out or 0)).quantize(Decimal("0.001"))

    refunds_out = db.execute(
        select(func.coalesce(func.sum(RefundPayment.amount), 0))
        .select_from(RefundPayment)
        .join(SaleReturn, SaleReturn.id == RefundPayment.sale_return_id)
        .join(Sale, Sale.id == SaleReturn.original_sale_id)
        .join(PaymentMethod, PaymentMethod.id == RefundPayment.payment_method_id)
        .where(
            Sale.pos_shift_id == shift_id,
            PaymentMethod.kind == PaymentMethodKind.CASH,
        )
    ).scalar_one()
    refunds_out = Decimal(str(refunds_out or 0)).quantize(Decimal("0.001"))

    return (cash_in - delivery_out - refunds_out).quantize(Decimal("0.001"))


def compute_expected_bank(db: Session, shift_id: int) -> Decimal:
    """صافي تحصيلات المصرف في الجلسة − مرتجعات المصرف (بدون أجرة توصيل — تُخصم من الكاش)."""
    bank_in = db.execute(
        select(func.coalesce(func.sum(SalePayment.amount), 0))
        .select_from(SalePayment)
        .join(Sale, Sale.id == SalePayment.sale_id)
        .join(PaymentMethod, PaymentMethod.id == SalePayment.payment_method_id)
        .where(
            Sale.pos_shift_id == shift_id,
            PaymentMethod.kind == PaymentMethodKind.BANK,
        )
    ).scalar_one()
    bank_in = Decimal(str(bank_in or 0)).quantize(Decimal("0.001"))

    refunds_out = db.execute(
        select(func.coalesce(func.sum(RefundPayment.amount), 0))
        .select_from(RefundPayment)
        .join(SaleReturn, SaleReturn.id == RefundPayment.sale_return_id)
        .join(Sale, Sale.id == SaleReturn.original_sale_id)
        .join(PaymentMethod, PaymentMethod.id == RefundPayment.payment_method_id)
        .where(
            Sale.pos_shift_id == shift_id,
            PaymentMethod.kind == PaymentMethodKind.BANK,
        )
    ).scalar_one()
    refunds_out = Decimal(str(refunds_out or 0)).quantize(Decimal("0.001"))

    return (bank_in - refunds_out).quantize(Decimal("0.001"))


def close_shift(
    db: Session,
    *,
    shift_id: int,
    user_id: int,
    counted_cash: Decimal,
    counted_bank: Decimal | None = None,
    closing_note: str | None = None,
) -> PosShift:
    sh = db.get(PosShift, shift_id)
    if sh is None or sh.user_id != user_id:
        raise PosShiftError("الجلسة غير موجودة أو لا تخصّك.")
    if sh.status != PosShiftStatus.OPEN:
        raise PosShiftError("هذه الجلسة مغلقة مسبقاً.")
    expected_c = compute_expected_cash(db, shift_id)
    cash_diff = (counted_cash - expected_c).quantize(Decimal("0.001"))
    sh.status = PosShiftStatus.CLOSED
    sh.closed_at = datetime.now(timezone.utc)
    sh.counted_cash = counted_cash.quantize(Decimal("0.001"))
    sh.expected_cash = expected_c
    sh.cash_difference = cash_diff
    expected_b = compute_expected_bank(db, shift_id)
    cb = counted_bank if counted_bank is not None else expected_b
    sh.counted_bank = cb.quantize(Decimal("0.001"))
    sh.expected_bank = expected_b
    sh.bank_difference = (cb - expected_b).quantize(Decimal("0.001"))
    sh.closing_note = (closing_note or "").strip() or None
    db.flush()
    return sh


def list_completed_sales_for_shift(db: Session, shift_id: int) -> list[Sale]:
    return list(
        db.scalars(
            select(Sale)
            .where(
                Sale.pos_shift_id == shift_id,
                Sale.status == SaleStatus.COMPLETED,
            )
            .order_by(Sale.id.asc())
        ).all()
    )


@dataclass
class ShiftSaleRow:
    sale_id: int
    total: Decimal
    context: str
    created_at: datetime | None
    payment_label: str


def build_shift_sale_rows(db: Session, shift_id: int) -> list[ShiftSaleRow]:
    sales = list_completed_sales_for_shift(db, shift_id)
    rows: list[ShiftSaleRow] = []
    for s in sales:
        pay = db.execute(
            select(SalePayment, PaymentMethod)
            .join(PaymentMethod, PaymentMethod.id == SalePayment.payment_method_id)
            .where(SalePayment.sale_id == s.id)
            .limit(1)
        ).first()
        if pay:
            _, pm = pay
            pl = f"{pm.name_ar} ({pm.kind.value})"
        else:
            pl = "— (غرفة/آجل)"
        ctx = s.context_type.value if s.context_type else ""
        rows.append(
            ShiftSaleRow(
                sale_id=s.id,
                total=Decimal(str(s.total or 0)).quantize(Decimal("0.001")),
                context=ctx,
                created_at=s.created_at,
                payment_label=pl,
            )
        )
    return rows
