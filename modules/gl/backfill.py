"""ترحيل GL retroactive للحركات السابقة (idempotent)."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.gl.posting import (
    post_asset_purchase_shadow,
    post_expense_shadow,
    post_inventory_purchase_shadow,
    post_payment_transfer_shadow,
    post_payroll_accrual_shadow,
    post_payroll_payment_shadow,
    post_period_depreciation_shadow,
    post_hotel_booking_payment_refund_shadow,
    post_hotel_booking_payment_shadow,
    post_purchase_payment_shadow,
    post_refund_payment_shadow,
    post_sale_cogs_shadow,
    post_sale_completed_shadow,
    post_sale_payment_shadow,
    post_sale_return_cogs_shadow,
    post_sale_return_shadow,
)
from modules.hotel.booking_models import HotelBookingPayment, HotelBookingPaymentRefund
from modules.gl.service import is_gl_enabled
from modules.hr.models import PayrollRun, PayrollStatus
from modules.payments.models import (
    PaymentTransfer,
    Purchase,
    PurchaseKind,
    PurchasePayment,
    RefundPayment,
    SalePayment,
)
from modules.refunds.models import SaleReturn, SaleReturnStatus
from modules.sales.models import Sale, SaleStatus


def run_gl_backfill(db: Session) -> dict[str, int]:
    """يُنشئ قيود GL للحركات القديمة — آمن للتكرار."""
    if not is_gl_enabled(db):
        return {"skipped": 1}

    from modules.gl import posting as gl_posting

    gl_posting._BACKFILL_IGNORE_CUTOVER = True
    try:
        return _run_gl_backfill_inner(db)
    finally:
        gl_posting._BACKFILL_IGNORE_CUTOVER = False


def _run_gl_backfill_inner(db: Session) -> dict[str, int]:
    counts = {
        "sales": 0,
        "payments": 0,
        "returns": 0,
        "refunds": 0,
        "expenses": 0,
        "inventory": 0,
        "purchase_payments": 0,
        "transfers": 0,
        "payroll_accruals": 0,
        "payroll_payments": 0,
        "cogs": 0,
        "return_cogs": 0,
        "assets": 0,
        "depreciation": 0,
        "hotel_payments": 0,
        "hotel_payment_refunds": 0,
    }

    for sale in db.scalars(select(Sale).where(Sale.status == SaleStatus.COMPLETED)).all():
        if post_sale_completed_shadow(db, sale):
            counts["sales"] += 1
        if post_sale_cogs_shadow(db, sale):
            counts["cogs"] += 1

    for sp in db.scalars(select(SalePayment)).all():
        if post_sale_payment_shadow(db, sp):
            counts["payments"] += 1

    for sr in db.scalars(
        select(SaleReturn).where(SaleReturn.status == SaleReturnStatus.POSTED)
    ).all():
        if post_sale_return_shadow(db, sr):
            counts["returns"] += 1
        if post_sale_return_cogs_shadow(db, sr):
            counts["return_cogs"] += 1

    for rp in db.scalars(select(RefundPayment)).all():
        if post_refund_payment_shadow(db, rp):
            counts["refunds"] += 1

    for hp in db.scalars(select(HotelBookingPayment)).all():
        if post_hotel_booking_payment_shadow(db, hp):
            counts["hotel_payments"] += 1

    for href in db.scalars(select(HotelBookingPaymentRefund)).all():
        if post_hotel_booking_payment_refund_shadow(db, href):
            counts["hotel_payment_refunds"] += 1

    for p in db.scalars(select(Purchase)).all():
        if p.kind == PurchaseKind.EXPENSE and post_expense_shadow(db, p):
            counts["expenses"] += 1
        elif p.kind == PurchaseKind.INVENTORY and post_inventory_purchase_shadow(db, p):
            counts["inventory"] += 1
        elif p.kind == PurchaseKind.ASSET and post_asset_purchase_shadow(db, p):
            counts["assets"] += 1

    from datetime import datetime, timezone
    from modules.payments.depreciation import list_fixed_asset_lines

    if list_fixed_asset_lines(db):
        epoch = datetime(2000, 1, 1, tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        n = post_period_depreciation_shadow(db, epoch, now)
        counts["depreciation"] = n

    for pp in db.scalars(select(PurchasePayment)).all():
        if post_purchase_payment_shadow(db, pp):
            counts["purchase_payments"] += 1

    for tf in db.scalars(select(PaymentTransfer)).all():
        if post_payment_transfer_shadow(db, tf):
            counts["transfers"] += 1

    for run in db.scalars(
        select(PayrollRun).where(
            PayrollRun.status.in_((PayrollStatus.POSTED, PayrollStatus.PAID))
        )
    ).all():
        if post_payroll_accrual_shadow(db, run):
            counts["payroll_accruals"] += 1
        if run.status == PayrollStatus.PAID:
            purchase_id = next(
                (int(e.paid_purchase_id) for e in run.entries if e.paid_purchase_id),
                None,
            )
            pm_id = None
            if purchase_id is not None:
                purchase = db.get(Purchase, purchase_id)
                if purchase is not None:
                    pm_id = int(purchase.payment_method_id)
            if pm_id is not None and post_payroll_payment_shadow(
                db,
                run,
                payment_method_id=pm_id,
                purchase_id=purchase_id,
            ):
                counts["payroll_payments"] += 1

    db.flush()
    from modules.gl.hotel_revenue_migration import migrate_hotel_gl_to_revenue_account

    mig = migrate_hotel_gl_to_revenue_account(db)
    counts["hotel_gl_migrated"] = int(mig.get("updated_lines") or 0)
    return counts
