"""ترحيل GL عند إقفال جلسة الكاشier — backfill + لقطة مطابقة 4100."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from modules.gl.models import GlJournalEntry, GlJournalLine
from modules.payments.models import RefundPayment, SalePayment
from modules.pos_shifts.models import PosShift
from modules.pos_shifts.service import compute_shift_financial_summary
from modules.refunds.models import SaleReturn, SaleReturnStatus
from modules.sales.models import Sale, SaleStatus


@dataclass
class ShiftCloseGlResult:
    gl_enabled: bool
    backfilled_entries: int
    operational_net: Decimal
    gl_revenue_net: Decimal | None
    gl_gap: Decimal | None


def _entry_exists(db: Session, idempotency_key: str) -> bool:
    return (
        db.scalar(
            select(GlJournalEntry.id).where(
                GlJournalEntry.idempotency_key == idempotency_key
            )
        )
        is not None
    )


def _post_counting(db: Session, key: str, post_fn) -> int:
    if _entry_exists(db, key):
        return 0
    post_fn()
    return 1 if _entry_exists(db, key) else 0


def backfill_shift_gl(db: Session, shift_id: int) -> int:
    from modules.gl.posting import (
        post_refund_payment_shadow,
        post_sale_cogs_shadow,
        post_sale_completed_shadow,
        post_sale_payment_shadow,
        post_sale_return_cogs_shadow,
        post_sale_return_shadow,
    )
    from modules.gl.service import is_gl_enabled

    if not is_gl_enabled(db):
        return 0

    sales = list(
        db.scalars(
            select(Sale).where(
                Sale.pos_shift_id == shift_id,
                Sale.status == SaleStatus.COMPLETED,
            )
        ).all()
    )
    sale_ids = [int(s.id) for s in sales]
    count = 0

    for sale in sales:
        count += _post_counting(
            db,
            f"sale:completed:{sale.id}",
            lambda s=sale: post_sale_completed_shadow(db, s),
        )
        count += _post_counting(
            db,
            f"sale:cogs:{sale.id}",
            lambda s=sale: post_sale_cogs_shadow(db, s),
        )

    if sale_ids:
        payments = list(
            db.scalars(
                select(SalePayment).where(SalePayment.sale_id.in_(sale_ids))
            ).all()
        )
        for sp in payments:
            count += _post_counting(
                db,
                f"sale_payment:{sp.id}",
                lambda p=sp: post_sale_payment_shadow(db, p),
            )

        returns = list(
            db.scalars(
                select(SaleReturn).where(
                    SaleReturn.original_sale_id.in_(sale_ids),
                    SaleReturn.status == SaleReturnStatus.POSTED,
                )
            ).all()
        )
        return_ids = [int(r.id) for r in returns]
        for sr in returns:
            count += _post_counting(
                db,
                f"sale_return:{sr.id}",
                lambda r=sr: post_sale_return_shadow(db, r),
            )
            count += _post_counting(
                db,
                f"sale_return:cogs:{sr.id}",
                lambda r=sr: post_sale_return_cogs_shadow(db, r),
            )
        if return_ids:
            refunds = list(
                db.scalars(
                    select(RefundPayment).where(
                        RefundPayment.sale_return_id.in_(return_ids)
                    )
                ).all()
            )
            for rp in refunds:
                count += _post_counting(
                    db,
                    f"refund_payment:{rp.id}",
                    lambda p=rp: post_refund_payment_shadow(db, p),
                )
    return count


def shift_operational_net(db: Session, shift_id: int) -> Decimal:
    fin = compute_shift_financial_summary(db, shift_id)
    return (
        fin.cash_sales + fin.bank_sales - fin.cash_refunds - fin.bank_refunds
    ).quantize(Decimal("0.001"))


def shift_gl_4100_net(db: Session, shift_id: int, *, account_id: int) -> Decimal:
    sale_ids = list(
        db.scalars(
            select(Sale.id).where(
                Sale.pos_shift_id == shift_id,
                Sale.status == SaleStatus.COMPLETED,
            )
        ).all()
    )
    return_ids = list(
        db.scalars(
            select(SaleReturn.id)
            .join(Sale, SaleReturn.original_sale_id == Sale.id)
            .where(
                Sale.pos_shift_id == shift_id,
                SaleReturn.status == SaleReturnStatus.POSTED,
            )
        ).all()
    )
    filters = []
    if sale_ids:
        filters.append(
            and_(
                GlJournalEntry.source_type == "sale",
                GlJournalEntry.source_id.in_(sale_ids),
            )
        )
    if return_ids:
        filters.append(
            and_(
                GlJournalEntry.source_type == "sale_return",
                GlJournalEntry.source_id.in_(return_ids),
            )
        )
    if not filters:
        return Decimal("0")

    net = db.scalar(
        select(
            func.coalesce(func.sum(GlJournalLine.credit), 0)
            - func.coalesce(func.sum(GlJournalLine.debit), 0)
        )
        .select_from(GlJournalLine)
        .join(GlJournalEntry, GlJournalEntry.id == GlJournalLine.entry_id)
        .where(
            GlJournalLine.account_id == account_id,
            or_(*filters),
        )
    )
    return Decimal(str(net or 0)).quantize(Decimal("0.001"))


def snapshot_shift_gl(
    db: Session, shift_id: int, *, operational_net: Decimal | None = None
) -> ShiftCloseGlResult:
    from modules.gl.models import GlAccount
    from modules.gl.posting import CODE_REVENUE
    from modules.gl.service import is_gl_enabled

    op = (
        operational_net
        if operational_net is not None
        else shift_operational_net(db, shift_id)
    ).quantize(Decimal("0.001"))

    if not is_gl_enabled(db):
        return ShiftCloseGlResult(
            gl_enabled=False,
            backfilled_entries=0,
            operational_net=op,
            gl_revenue_net=None,
            gl_gap=None,
        )

    acc = db.scalar(select(GlAccount).where(GlAccount.code == CODE_REVENUE))
    if acc is None:
        return ShiftCloseGlResult(
            gl_enabled=True,
            backfilled_entries=0,
            operational_net=op,
            gl_revenue_net=Decimal("0"),
            gl_gap=op,
        )

    gl_net = shift_gl_4100_net(db, shift_id, account_id=int(acc.id))
    gap = (op - gl_net).quantize(Decimal("0.001"))
    return ShiftCloseGlResult(
        gl_enabled=True,
        backfilled_entries=0,
        operational_net=op,
        gl_revenue_net=gl_net,
        gl_gap=gap,
    )


def run_shift_close_gl(
    db: Session, shift: PosShift, *, operational_net: Decimal | None = None
) -> ShiftCloseGlResult:
    backfilled = backfill_shift_gl(db, int(shift.id))
    snap = snapshot_shift_gl(db, int(shift.id), operational_net=operational_net)
    return ShiftCloseGlResult(
        gl_enabled=snap.gl_enabled,
        backfilled_entries=backfilled,
        operational_net=snap.operational_net,
        gl_revenue_net=snap.gl_revenue_net,
        gl_gap=snap.gl_gap,
    )
