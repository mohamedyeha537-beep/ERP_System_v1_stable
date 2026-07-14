"""تقارير مراجعة جلسات الكاشير — قائمة وتفاصيل."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from modules.delivery.models import DeliveryCashSettlement
from modules.payments.models import PaymentMethod, RefundPayment, SalePayment
from modules.pos_shifts.models import PosShift, PosShiftShortage, PosShiftStatus
from modules.pos_shifts.service import (
    ShiftFinancialSummary,
    compute_shift_financial_summary,
)
from modules.pos_shifts.shortages import shortage_kind_label_ar, shift_reconciliation_totals
from modules.refunds.models import SaleReturn
from modules.sales.models import Sale, SaleContext, SaleStatus
from modules.sales.service import sale_context_label_ar, sale_pos_order_label


@dataclass
class ShiftIndexRow:
    shift_id: int
    status: str
    opened_at: datetime | None
    closed_at: datetime | None
    cashier_label: str
    user_label: str
    sales_count: int
    sales_total: Decimal
    expected_cash: Decimal | None
    expected_bank: Decimal | None
    counted_cash: Decimal | None
    counted_bank: Decimal | None
    cash_difference: Decimal | None
    bank_difference: Decimal | None
    shortage_total: Decimal
    surplus_total: Decimal
    handoff_at: datetime | None
    loyalty_redeem_count: int = 0
    loyalty_dinar_cost: Decimal = Decimal("0")


@dataclass
class ShiftPaymentBreakdown:
    method_id: int
    method_name: str
    kind: str
    payment_count: int
    amount: Decimal


@dataclass
class ShiftSaleDetailRow:
    sale_id: int
    label: str
    context: str
    status: str
    total: Decimal
    payment_label: str
    line_count: int
    created_at: datetime | None
    completed_at: datetime | None = None


@dataclass
class ShiftRefundRow:
    return_id: int
    sale_id: int
    amount: Decimal
    method_name: str
    kind: str
    created_at: datetime | None


@dataclass
class ShiftDeliveryRow:
    sale_id: int
    order_label: str
    amount: Decimal
    cash_method_name: str
    created_at: datetime | None


@dataclass
class ShiftContextStat:
    context: str
    label_ar: str
    count: int
    total: Decimal


@dataclass
class ShiftShortageRow:
    kind: str
    kind_label: str
    expected: Decimal | None
    counted: Decimal | None
    difference: Decimal | None
    shortage_amount: Decimal
    resolved_action: str | None


@dataclass
class ShiftDetailReport:
    shift: PosShift
    cashier_label: str
    user_label: str
    handoff_by_label: str | None
    financial: ShiftFinancialSummary
    sales_count: int
    sales_total: Decimal
    total_costs: Decimal
    cancelled_count: int
    draft_count: int
    payment_breakdown: list[ShiftPaymentBreakdown]
    sale_rows: list[ShiftSaleDetailRow]
    refund_rows: list[ShiftRefundRow]
    delivery_rows: list[ShiftDeliveryRow]
    context_stats: list[ShiftContextStat]
    shortage_rows: list[ShiftShortageRow]
    counted_total: Decimal | None


_CONTEXT_LABELS = {
    SaleContext.TABLE.value: "طاولات",
    SaleContext.ROOM.value: "شقق / غرف",
    SaleContext.EXTERNAL.value: "طلبات خارجية",
}


def _shift_cashier_label(sh: PosShift) -> str:
    if getattr(sh, "employee", None) is not None and sh.employee.full_name_ar:
        return sh.employee.full_name_ar
    if getattr(sh, "user", None) is not None and sh.user.username:
        return sh.user.username
    return f"جلسة #{sh.id}"


def _shift_user_label(sh: PosShift) -> str:
    if getattr(sh, "user", None) is not None and sh.user.username:
        return sh.user.username
    return "—"


def list_shifts_index(
    db: Session,
    *,
    status_filter: str = "all",
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    employee_id: int | None = None,
    user_id: int | None = None,
    limit: int = 40,
    offset: int = 0,
) -> tuple[list[ShiftIndexRow], int]:
    base = select(PosShift).options(
        selectinload(PosShift.employee),
        selectinload(PosShift.user),
    )
    count_q = select(func.count()).select_from(PosShift)
    if status_filter == "open":
        base = base.where(PosShift.status == PosShiftStatus.OPEN)
        count_q = count_q.where(PosShift.status == PosShiftStatus.OPEN)
    elif status_filter == "closed":
        base = base.where(PosShift.status == PosShiftStatus.CLOSED)
        count_q = count_q.where(PosShift.status == PosShiftStatus.CLOSED)
    if date_from is not None:
        base = base.where(PosShift.opened_at >= date_from)
        count_q = count_q.where(PosShift.opened_at >= date_from)
    if date_to is not None:
        base = base.where(PosShift.opened_at < date_to)
        count_q = count_q.where(PosShift.opened_at < date_to)
    if employee_id is not None:
        base = base.where(PosShift.employee_id == employee_id)
        count_q = count_q.where(PosShift.employee_id == employee_id)
    if user_id is not None:
        base = base.where(PosShift.user_id == user_id)
        count_q = count_q.where(PosShift.user_id == user_id)

    total = int(db.execute(count_q).scalar_one() or 0)
    shifts = list(
        db.scalars(
            base.order_by(PosShift.id.desc()).offset(offset).limit(limit)
        ).all()
    )
    if not shifts:
        return [], total

    shift_ids = [s.id for s in shifts]
    sales_agg = {
        int(r.shift_id): (int(r.cnt or 0), Decimal(str(r.total or 0)))
        for r in db.execute(
            select(
                Sale.pos_shift_id.label("shift_id"),
                func.count(Sale.id).label("cnt"),
                func.coalesce(func.sum(Sale.total), 0).label("total"),
            )
            .where(
                Sale.pos_shift_id.in_(shift_ids),
                Sale.status == SaleStatus.COMPLETED,
            )
            .group_by(Sale.pos_shift_id)
        ).all()
    }
    shortage_agg: dict[int, Decimal] = {
        int(r.shift_id): Decimal(str(r.total or 0))
        for r in db.execute(
            select(
                PosShiftShortage.shift_id.label("shift_id"),
                func.coalesce(func.sum(PosShiftShortage.shortage_amount), 0).label(
                    "total"
                ),
            )
            .where(PosShiftShortage.shift_id.in_(shift_ids))
            .group_by(PosShiftShortage.shift_id)
        ).all()
    }
    from modules.customers.loyalty_shift_reports import loyalty_redeem_agg_for_shifts

    loyalty_agg = loyalty_redeem_agg_for_shifts(db, shift_ids)

    rows: list[ShiftIndexRow] = []
    for sh in shifts:
        cnt, stotal = sales_agg.get(sh.id, (0, Decimal("0")))
        stotal = stotal.quantize(Decimal("0.001"))
        loy = loyalty_agg.get(sh.id)
        sh_total = shortage_agg.get(sh.id, Decimal("0")).quantize(Decimal("0.001"))
        _, surplus_total = shift_reconciliation_totals(
            sh.cash_difference,
            sh.bank_difference,
            shortage_total=sh_total,
        )
        rows.append(
            ShiftIndexRow(
                shift_id=sh.id,
                status=sh.status.value,
                opened_at=sh.opened_at,
                closed_at=sh.closed_at,
                cashier_label=_shift_cashier_label(sh),
                user_label=_shift_user_label(sh),
                sales_count=cnt,
                sales_total=stotal,
                expected_cash=sh.expected_cash,
                expected_bank=sh.expected_bank,
                counted_cash=sh.counted_cash,
                counted_bank=sh.counted_bank,
                cash_difference=sh.cash_difference,
                bank_difference=sh.bank_difference,
                shortage_total=sh_total,
                surplus_total=surplus_total,
                handoff_at=sh.treasury_handoff_at,
                loyalty_redeem_count=loy.redeem_count if loy else 0,
                loyalty_dinar_cost=loy.dinar_cost if loy else Decimal("0"),
            )
        )
    return rows, total


def get_shift_for_report(db: Session, shift_id: int) -> PosShift | None:
    return db.execute(
        select(PosShift)
        .options(
            selectinload(PosShift.employee),
            selectinload(PosShift.user),
        )
        .where(PosShift.id == shift_id)
    ).scalar_one_or_none()


def compute_shift_total_costs(
    db: Session,
    shift_id: int,
    *,
    completed_sales: list[Sale] | None = None,
) -> Decimal:
    """تكلفة المباع (COGS) لفواتير الجلسة المكتملة، مع خصم مرتجعات العودة للمخزن."""
    from modules.gl.posting import sale_cogs_amount, sale_return_cogs_amount
    from modules.refunds.models import SaleReturnStatus

    if completed_sales is None:
        completed_sales = list(
            db.scalars(
                select(Sale)
                .options(selectinload(Sale.lines))
                .where(
                    Sale.pos_shift_id == shift_id,
                    Sale.status == SaleStatus.COMPLETED,
                )
            ).all()
        )
    total = sum((sale_cogs_amount(db, s) for s in completed_sales), Decimal("0"))

    returns = list(
        db.scalars(
            select(SaleReturn)
            .options(selectinload(SaleReturn.lines))
            .join(Sale, Sale.id == SaleReturn.original_sale_id)
            .where(
                Sale.pos_shift_id == shift_id,
                SaleReturn.status == SaleReturnStatus.POSTED,
            )
        ).all()
    )
    for ret in returns:
        total -= sale_return_cogs_amount(db, ret)

    if total < 0:
        total = Decimal("0")
    return total.quantize(Decimal("0.001"))


def build_shift_detail_report(db: Session, shift_id: int) -> ShiftDetailReport | None:
    sh = get_shift_for_report(db, shift_id)
    if sh is None:
        return None

    financial = compute_shift_financial_summary(db, shift_id)

    sales = list(
        db.scalars(
            select(Sale)
            .options(selectinload(Sale.table), selectinload(Sale.lines))
            .where(Sale.pos_shift_id == shift_id)
            .order_by(Sale.id.asc())
        ).all()
    )
    completed = [s for s in sales if s.status == SaleStatus.COMPLETED]
    sales_total = sum(
        (Decimal(str(s.total or 0)) for s in completed), Decimal("0")
    ).quantize(Decimal("0.001"))
    total_costs = compute_shift_total_costs(db, shift_id, completed_sales=completed)

    payment_breakdown: list[ShiftPaymentBreakdown] = []
    for mid, name, kind, cnt, amt in db.execute(
        select(
            PaymentMethod.id,
            PaymentMethod.name_ar,
            PaymentMethod.kind,
            func.count(SalePayment.id),
            func.coalesce(func.sum(SalePayment.amount), 0),
        )
        .select_from(SalePayment)
        .join(Sale, Sale.id == SalePayment.sale_id)
        .join(PaymentMethod, PaymentMethod.id == SalePayment.payment_method_id)
        .where(Sale.pos_shift_id == shift_id, Sale.status == SaleStatus.COMPLETED)
        .group_by(PaymentMethod.id, PaymentMethod.name_ar, PaymentMethod.kind)
        .order_by(PaymentMethod.id)
    ).all():
        kind_val = kind.value if hasattr(kind, "value") else str(kind)
        payment_breakdown.append(
            ShiftPaymentBreakdown(
                method_id=int(mid),
                method_name=str(name),
                kind=kind_val,
                payment_count=int(cnt or 0),
                amount=Decimal(str(amt or 0)).quantize(Decimal("0.001")),
            )
        )

    sale_rows: list[ShiftSaleDetailRow] = []
    for s in completed:
        pay = db.execute(
            select(SalePayment, PaymentMethod)
            .join(PaymentMethod, PaymentMethod.id == SalePayment.payment_method_id)
            .where(SalePayment.sale_id == s.id)
            .limit(1)
        ).first()
        if pay:
            _, pm = pay
            pl = f"{pm.name_ar} ({pm.kind.value if hasattr(pm.kind, 'value') else pm.kind})"
        else:
            pl = "— (شقة/آجل)"
        ctx = sale_context_label_ar(s, detailed=True)
        sale_rows.append(
            ShiftSaleDetailRow(
                sale_id=s.id,
                label=sale_pos_order_label(s),
                context=ctx,
                status=s.status.value,
                total=Decimal(str(s.total or 0)).quantize(Decimal("0.001")),
                payment_label=pl,
                line_count=len(s.lines or []),
                created_at=s.created_at,
            )
        )

    refund_rows: list[ShiftRefundRow] = []
    for ret_id, sale_id, amt, name, kind, at in db.execute(
        select(
            SaleReturn.id,
            SaleReturn.original_sale_id,
            RefundPayment.amount,
            PaymentMethod.name_ar,
            PaymentMethod.kind,
            RefundPayment.created_at,
        )
        .select_from(RefundPayment)
        .join(SaleReturn, SaleReturn.id == RefundPayment.sale_return_id)
        .join(Sale, Sale.id == SaleReturn.original_sale_id)
        .join(PaymentMethod, PaymentMethod.id == RefundPayment.payment_method_id)
        .where(Sale.pos_shift_id == shift_id)
        .order_by(RefundPayment.id.desc())
    ).all():
        kind_val = kind.value if hasattr(kind, "value") else str(kind)
        refund_rows.append(
            ShiftRefundRow(
                return_id=int(ret_id),
                sale_id=int(sale_id),
                amount=Decimal(str(amt or 0)).quantize(Decimal("0.001")),
                method_name=str(name),
                kind=kind_val,
                created_at=at,
            )
        )

    delivery_rows: list[ShiftDeliveryRow] = []
    for sale_id, amt, pm_name, at in db.execute(
        select(
            DeliveryCashSettlement.sale_id,
            DeliveryCashSettlement.amount,
            PaymentMethod.name_ar,
            DeliveryCashSettlement.created_at,
        )
        .select_from(DeliveryCashSettlement)
        .join(Sale, Sale.id == DeliveryCashSettlement.sale_id)
        .join(
            PaymentMethod,
            PaymentMethod.id == DeliveryCashSettlement.cash_method_id,
        )
        .where(Sale.pos_shift_id == shift_id)
        .order_by(DeliveryCashSettlement.id.desc())
    ).all():
        sale = db.get(Sale, int(sale_id))
        delivery_rows.append(
            ShiftDeliveryRow(
                sale_id=int(sale_id),
                order_label=sale_pos_order_label(sale) if sale else f"#{sale_id}",
                amount=Decimal(str(amt or 0)).quantize(Decimal("0.001")),
                cash_method_name=str(pm_name),
                created_at=at,
            )
        )

    context_stats: list[ShiftContextStat] = []
    for ctx, cnt, total in db.execute(
        select(
            Sale.context_type,
            func.count(Sale.id),
            func.coalesce(func.sum(Sale.total), 0),
        )
        .where(Sale.pos_shift_id == shift_id, Sale.status == SaleStatus.COMPLETED)
        .group_by(Sale.context_type)
    ).all():
        ctx_val = ctx.value if hasattr(ctx, "value") else str(ctx)
        context_stats.append(
            ShiftContextStat(
                context=ctx_val,
                label_ar=_CONTEXT_LABELS.get(ctx_val, ctx_val),
                count=int(cnt or 0),
                total=Decimal(str(total or 0)).quantize(Decimal("0.001")),
            )
        )
    context_stats.sort(key=lambda x: x.count, reverse=True)

    shortage_rows: list[ShiftShortageRow] = []
    for sht in db.scalars(
        select(PosShiftShortage).where(PosShiftShortage.shift_id == shift_id)
    ).all():
        shortage_rows.append(
            ShiftShortageRow(
                kind=sht.kind.value if hasattr(sht.kind, "value") else str(sht.kind),
                kind_label=shortage_kind_label_ar(sht.kind),
                expected=sht.expected_amount,
                counted=sht.counted_amount,
                difference=sht.difference,
                shortage_amount=Decimal(str(sht.shortage_amount or 0)).quantize(
                    Decimal("0.001")
                ),
                resolved_action=sht.resolved_action,
            )
        )

    handoff_by_label: str | None = None
    if sh.treasury_handoff_by_id:
        from modules.authz.models import User

        u = db.get(User, sh.treasury_handoff_by_id)
        if u and u.username:
            handoff_by_label = u.username

    counted_total: Decimal | None = None
    if sh.counted_cash is not None or sh.counted_bank is not None:
        counted_total = (
            Decimal(str(sh.counted_cash or 0)) + Decimal(str(sh.counted_bank or 0))
        ).quantize(Decimal("0.001"))

    return ShiftDetailReport(
        shift=sh,
        cashier_label=_shift_cashier_label(sh),
        user_label=_shift_user_label(sh),
        handoff_by_label=handoff_by_label,
        financial=financial,
        sales_count=len(completed),
        sales_total=sales_total,
        total_costs=total_costs,
        cancelled_count=sum(1 for s in sales if s.status == SaleStatus.CANCELLED),
        draft_count=sum(1 for s in sales if s.status == SaleStatus.DRAFT),
        payment_breakdown=payment_breakdown,
        sale_rows=sale_rows,
        refund_rows=refund_rows,
        delivery_rows=delivery_rows,
        context_stats=context_stats,
        shortage_rows=shortage_rows,
        counted_total=counted_total,
    )


def shift_index_csv_rows(rows: list[ShiftIndexRow]) -> list[tuple]:
    from app.datetime_local import format_local_dt

    out: list[tuple] = []
    for r in rows:
        out.append(
            (
                r.shift_id,
                r.status,
                r.cashier_label,
                r.user_label,
                format_local_dt(r.opened_at, "%Y-%m-%d %H:%M") if r.opened_at else "",
                format_local_dt(r.closed_at, "%Y-%m-%d %H:%M") if r.closed_at else "",
                r.sales_count,
                r.sales_total,
                r.expected_cash if r.expected_cash is not None else "",
                r.expected_bank if r.expected_bank is not None else "",
                r.counted_cash if r.counted_cash is not None else "",
                r.counted_bank if r.counted_bank is not None else "",
                r.cash_difference if r.cash_difference is not None else "",
                r.bank_difference if r.bank_difference is not None else "",
                r.shortage_total,
                r.surplus_total,
                format_local_dt(r.handoff_at, "%Y-%m-%d %H:%M") if r.handoff_at else "",
            )
        )
    return out
