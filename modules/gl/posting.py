"""ترحيل GL في وضع الظل — لا يوقف العمليات التشغيلية عند الفشل."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.gl.models import (
    GlJournalEntry,
    GlJournalEntryStatus,
    GlJournalLine,
    GlPaymentMethodMap,
)
from modules.gl.hierarchy import assert_postable_account, build_children_map
from modules.gl.service import (
    entry_date_on_or_after_cutover,
    get_gl_post_mode,
    is_gl_enabled,
    is_posting_date_allowed,
)
from modules.payments.models import (
    PaymentMethod,
    PaymentMethodKind,
    PaymentTransfer,
    PaymentTransferType,
    Purchase,
    PurchaseKind,
    PurchasePayment,
    RefundPayment,
    SalePayment,
)
from modules.refunds.models import SaleReturn
from modules.sales.models import Sale
from modules.hr.models import PayrollRun
from modules.hotel.booking_models import HotelBookingPayment, HotelBookingPaymentRefund

logger = logging.getLogger(__name__)

# رموز الحسابات النظامية الافتراضية (من seed)
CODE_AR = "1200"
CODE_REVENUE = "4100"
CODE_HOTEL_REVENUE = "4150"
CODE_RETURNS = "4200"
CODE_CASH = "1110"
CODE_BANK = "1120"
CODE_EXPENSE = "5200"
CODE_INVENTORY = "1300"
CODE_AP = "2100"
CODE_EQUITY = "3100"
CODE_PAYROLL_EXPENSE = "5300"
CODE_PAYROLL_PAYABLE = "2200"
CODE_COGS = "5100"
CODE_EMPLOYEE_RECEIVABLE = "1200"  # سلف موظفين — مبسّط

_BACKFILL_IGNORE_CUTOVER = False

_ZERO = Decimal("0")


@dataclass(frozen=True)
class _LineSpec:
    account_code: str
    debit: Decimal
    credit: Decimal
    memo: str = ""


class GlPostingError(Exception):
    pass


def _q(amount: Decimal) -> Decimal:
    return Decimal(str(amount or 0)).quantize(Decimal("0.001"))


def _should_post(db: Session) -> bool:
    if not is_gl_enabled(db):
        return False
    return get_gl_post_mode(db) != "off"


def _account_id(db: Session, code: str) -> int:
    from modules.gl.models import GlAccount

    acc_id = db.scalar(select(GlAccount.id).where(GlAccount.code == code, GlAccount.is_active.is_(True)))
    if acc_id is None:
        raise GlPostingError(f"حساب GL غير موجود أو غير نشط: {code}")
    children_map = build_children_map(db)
    try:
        assert_postable_account(db, int(acc_id), children_map)
    except ValueError as exc:
        raise GlPostingError(str(exc)) from exc
    return int(acc_id)


def _cash_account_code_for_pm(db: Session, payment_method_id: int) -> str:
    mapped = db.scalar(
        select(GlPaymentMethodMap.gl_account_id).where(
            GlPaymentMethodMap.payment_method_id == payment_method_id
        )
    )
    if mapped:
        from modules.gl.models import GlAccount

        code = db.scalar(select(GlAccount.code).where(GlAccount.id == mapped))
        if code:
            return str(code)
    pm = db.get(PaymentMethod, payment_method_id)
    if pm is not None and pm.kind == PaymentMethodKind.BANK:
        return CODE_BANK
    return CODE_CASH


def _entry_date_from_dt(when: datetime | None) -> date:
    if when is None:
        return datetime.now(timezone.utc).date()
    if when.tzinfo is None:
        return when.date()
    return when.astimezone(timezone.utc).date()


def post_balanced_entry(
    db: Session,
    *,
    idempotency_key: str,
    source_type: str,
    source_id: int,
    description_ar: str,
    entry_date: date,
    lines: list[_LineSpec],
    created_by_id: int | None = None,
    ignore_cutover: bool = False,
    business_domain: str | None = None,
) -> GlJournalEntry | None:
    if not _should_post(db):
        return None
    if not entry_date_on_or_after_cutover(
        db, entry_date, ignore_cutover=ignore_cutover or _BACKFILL_IGNORE_CUTOVER
    ):
        return None
    if not is_posting_date_allowed(db, entry_date):
        return None
    key = (idempotency_key or "").strip()
    if not key:
        raise GlPostingError("مفتاح idempotency مطلوب.")
    existing_id = db.scalar(
        select(GlJournalEntry.id).where(GlJournalEntry.idempotency_key == key)
    )
    if existing_id is not None:
        return db.get(GlJournalEntry, int(existing_id))

    filtered: list[_LineSpec] = []
    for ln in lines:
        d, c = _q(ln.debit), _q(ln.credit)
        if d <= 0 and c <= 0:
            continue
        if d > 0 and c > 0:
            raise GlPostingError(f"سطر GL غير صالح: {ln.account_code}")
        filtered.append(_LineSpec(ln.account_code, d, c, ln.memo))
    if not filtered:
        return None

    total_d = sum((ln.debit for ln in filtered), _ZERO)
    total_c = sum((ln.credit for ln in filtered), _ZERO)
    if total_d != total_c:
        raise GlPostingError(f"قيد GL غير متوازن: {total_d} ≠ {total_c}")

    from modules.gl.domain import infer_entry_domain

    entry_dom = infer_entry_domain(
        db,
        source_type=source_type,
        source_id=source_id,
        business_domain=business_domain,
    )
    entry = GlJournalEntry(
        entry_date=entry_date,
        description_ar=description_ar[:255],
        status=GlJournalEntryStatus.POSTED,
        source_type=source_type,
        source_id=source_id,
        idempotency_key=key,
        post_mode=get_gl_post_mode(db),
        created_by_id=created_by_id,
        business_domain=entry_dom,
    )
    db.add(entry)
    db.flush()

    for i, ln in enumerate(filtered, start=1):
        db.add(
            GlJournalLine(
                entry_id=entry.id,
                account_id=_account_id(db, ln.account_code),
                debit=ln.debit,
                credit=ln.credit,
                memo=(ln.memo or "")[:255] or None,
                line_no=i,
            )
        )
    db.flush()
    return entry


def _safe(label: str, fn, *args, **kwargs) -> None:
    try:
        fn(*args, **kwargs)
    except Exception as exc:
        logger.warning("GL shadow post skipped (%s): %s", label, exc, exc_info=True)


def sale_cogs_amount(db: Session, sale: Sale) -> Decimal:
    from modules.inventory.costing import sale_fifo_cogs
    from modules.reporting.queries import _unit_cost_via_bom, avg_unit_cost_per_product

    fifo_total = sale_fifo_cogs(db, sale.id)
    if fifo_total is not None and fifo_total > 0:
        return fifo_total
    avg_costs = avg_unit_cost_per_product(db)
    total = _ZERO
    for line in sale.lines:
        qty = Decimal(str(line.quantity or 0))
        if qty <= 0:
            continue
        unit_cost = _unit_cost_via_bom(db, int(line.product_id), avg_costs)
        total += qty * unit_cost
    return total.quantize(Decimal("0.001"))


def sale_return_cogs_amount(db: Session, sale_return: SaleReturn) -> Decimal:
    from modules.reporting.queries import _unit_cost_via_bom, avg_unit_cost_per_product

    avg_costs = avg_unit_cost_per_product(db)
    total = _ZERO
    for line in sale_return.lines:
        if not line.restock:
            continue
        qty = Decimal(str(line.quantity or 0))
        if qty <= 0:
            continue
        unit_cost = _unit_cost_via_bom(db, int(line.product_id), avg_costs)
        total += qty * unit_cost
    return total.quantize(Decimal("0.001"))


def post_sale_completed_shadow(db: Session, sale: Sale) -> None:
    amount = _q(sale.total)
    if amount <= 0:
        return
    post_balanced_entry(
        db,
        idempotency_key=f"sale:completed:{sale.id}",
        source_type="sale",
        source_id=sale.id,
        description_ar=f"إيراد فاتورة #{sale.id}",
        entry_date=_entry_date_from_dt(sale.created_at),
        lines=[
            _LineSpec(CODE_AR, amount, _ZERO, "ذمم مدينة"),
            _LineSpec(CODE_REVENUE, _ZERO, amount, "إيراد مبيعات"),
        ],
    )


def post_sale_cogs_shadow(db: Session, sale: Sale) -> GlJournalEntry | None:
    amount = sale_cogs_amount(db, sale)
    if amount <= 0:
        return None
    return post_balanced_entry(
        db,
        idempotency_key=f"sale:cogs:{sale.id}",
        source_type="sale_cogs",
        source_id=sale.id,
        description_ar=f"تكلفة مبيعات فاتورة #{sale.id}",
        entry_date=_entry_date_from_dt(sale.created_at),
        lines=[
            _LineSpec(CODE_COGS, amount, _ZERO, "تكلفة البضاعة المباعة"),
            _LineSpec(CODE_INVENTORY, _ZERO, amount, "خصم مخزون"),
        ],
    )


def post_sale_return_cogs_shadow(db: Session, sale_return: SaleReturn) -> GlJournalEntry | None:
    amount = sale_return_cogs_amount(db, sale_return)
    if amount <= 0:
        return None
    return post_balanced_entry(
        db,
        idempotency_key=f"sale_return:cogs:{sale_return.id}",
        source_type="sale_return_cogs",
        source_id=sale_return.id,
        description_ar=f"عكس تكلفة مرتجع #{sale_return.id}",
        entry_date=_entry_date_from_dt(sale_return.created_at),
        lines=[
            _LineSpec(CODE_INVENTORY, amount, _ZERO, "عودة مخزون"),
            _LineSpec(CODE_COGS, _ZERO, amount, "عكس COGS"),
        ],
        created_by_id=sale_return.created_by_id,
    )


def post_sale_payment_shadow(db: Session, sp: SalePayment) -> None:
    amount = _q(sp.amount)
    if amount <= 0:
        return
    cash_code = _cash_account_code_for_pm(db, int(sp.payment_method_id))
    when = sp.created_at if hasattr(sp, "created_at") else None
    post_balanced_entry(
        db,
        idempotency_key=f"sale_payment:{sp.id}",
        source_type="sale_payment",
        source_id=sp.id,
        description_ar=f"تحصيل فاتورة #{sp.sale_id}",
        entry_date=_entry_date_from_dt(when),
        lines=[
            _LineSpec(cash_code, amount, _ZERO, "تحصيل نقد/بنك"),
            _LineSpec(CODE_AR, _ZERO, amount, "تسوية ذمة"),
        ],
    )


def post_sale_return_shadow(db: Session, sale_return: SaleReturn) -> None:
    amount = _q(sale_return.total)
    if amount <= 0:
        return
    post_balanced_entry(
        db,
        idempotency_key=f"sale_return:{sale_return.id}",
        source_type="sale_return",
        source_id=sale_return.id,
        description_ar=f"مرتجع فاتورة #{sale_return.original_sale_id}",
        entry_date=_entry_date_from_dt(sale_return.created_at),
        lines=[
            _LineSpec(CODE_RETURNS, amount, _ZERO, "مرتجعات مبيعات"),
            _LineSpec(CODE_AR, _ZERO, amount, "تخفيض ذمة"),
        ],
        created_by_id=sale_return.created_by_id,
    )


def post_refund_payment_shadow(db: Session, rp: RefundPayment) -> None:
    amount = _q(rp.amount)
    if amount <= 0:
        return
    cash_code = _cash_account_code_for_pm(db, int(rp.payment_method_id))
    post_balanced_entry(
        db,
        idempotency_key=f"refund_payment:{rp.id}",
        source_type="refund_payment",
        source_id=rp.id,
        description_ar=f"رد مبلغ مرتجع #{rp.sale_return_id}",
        entry_date=_entry_date_from_dt(rp.created_at),
        lines=[
            _LineSpec(CODE_AR, amount, _ZERO, "تخفيض ذمة"),
            _LineSpec(cash_code, _ZERO, amount, "صرف نقد/بنك"),
        ],
        created_by_id=rp.created_by_id,
    )


def post_expense_shadow(db: Session, purchase: Purchase) -> None:
    if purchase.kind == PurchaseKind.ASSET:
        return
    amount = _q(purchase.amount)
    if amount <= 0:
        return
    cat = (purchase.expense_category or "").strip()
    if cat == "رواتب":
        return
    if cat == "سلف موظفين":
        cash_code = _cash_account_code_for_pm(db, int(purchase.payment_method_id))
        post_balanced_entry(
            db,
            idempotency_key=f"purchase:advance:{purchase.id}",
            source_type="salary_advance",
            source_id=purchase.id,
            description_ar=f"سلفة موظف #{purchase.id}",
            entry_date=_entry_date_from_dt(purchase.created_at),
            lines=[
                _LineSpec(CODE_EMPLOYEE_RECEIVABLE, amount, _ZERO, "سلفة — ذمة موظف"),
                _LineSpec(cash_code, _ZERO, amount, "صرف سلفة"),
            ],
            created_by_id=purchase.created_by_id,
        )
        return
    cash_code = _cash_account_code_for_pm(db, int(purchase.payment_method_id))
    from modules.gl.expense_maps import expense_account_code_for_category

    expense_code = expense_account_code_for_category(db, cat)
    from modules.gl.domain import purchase_entry_domain

    post_balanced_entry(
        db,
        idempotency_key=f"purchase:expense:{purchase.id}",
        source_type="purchase_expense",
        source_id=purchase.id,
        description_ar=f"مصروف #{purchase.id}" + (f" — {cat}" if cat else ""),
        entry_date=_entry_date_from_dt(purchase.created_at),
        lines=[
            _LineSpec(expense_code, amount, _ZERO, cat or "مصروف"),
            _LineSpec(cash_code, _ZERO, amount, "صرف"),
        ],
        created_by_id=purchase.created_by_id,
        business_domain=purchase_entry_domain(purchase),
    )


def post_asset_purchase_shadow(db: Session, purchase: Purchase) -> GlJournalEntry | None:
    if purchase.kind != PurchaseKind.ASSET:
        return None
    amount = _q(purchase.amount)
    if amount <= 0:
        return None
    from modules.gl.role_maps import role_account_code

    fixed_code = role_account_code(db, "fixed_asset")
    consumable_code = role_account_code(db, "consumable_asset")
    cash_code = _cash_account_code_for_pm(db, int(purchase.payment_method_id))
    debit_lines: list[_LineSpec] = []
    for line in purchase.lines:
        amt = _q(line.line_total)
        if amt <= 0:
            continue
        if int(line.useful_life_months or 0) > 0:
            debit_lines.append(
                _LineSpec(fixed_code, amt, _ZERO, (line.item_name or "أصل ثابت")[:255])
            )
        else:
            debit_lines.append(
                _LineSpec(consumable_code, amt, _ZERO, (line.item_name or "مستلزم")[:255])
            )
    if not debit_lines:
        debit_lines.append(
            _LineSpec(fixed_code, amount, _ZERO, "شراء أصول")
        )
        total_d = amount
    else:
        total_d = sum((ln.debit for ln in debit_lines), _ZERO)
    return post_balanced_entry(
        db,
        idempotency_key=f"purchase:asset:{purchase.id}",
        source_type="purchase_asset",
        source_id=purchase.id,
        description_ar=f"شراء أصول #{purchase.id}",
        entry_date=_entry_date_from_dt(purchase.created_at),
        lines=debit_lines
        + [_LineSpec(cash_code, _ZERO, total_d, "صرف — شراء أصول")],
        created_by_id=purchase.created_by_id,
    )


def post_period_depreciation_shadow(
    db: Session,
    period_start: datetime,
    period_end: datetime,
) -> int:
    """ترحيل إهلاك الفترة إلى GL — idempotent لكل بند أصل."""
    from modules.gl.role_maps import role_account_code
    from modules.payments.depreciation import depreciation_in_period, list_fixed_asset_lines

    dep_code = role_account_code(db, "depreciation_expense")
    accum_code = role_account_code(db, "accumulated_depreciation")
    posted = 0
    p0 = period_start.date().isoformat()
    p1 = period_end.date().isoformat()
    for line in list_fixed_asset_lines(db):
        amt = depreciation_in_period(line, period_start, period_end)
        if amt <= 0:
            continue
        entry = post_balanced_entry(
            db,
            idempotency_key=f"depreciation:{line.id}:{p0}:{p1}",
            source_type="asset_depreciation",
            source_id=int(line.id),
            description_ar=f"إهلاك — {(line.item_name or 'أصل')[:200]}",
            entry_date=period_end.date(),
            lines=[
                _LineSpec(dep_code, amt, _ZERO, "مصروف إهلاك"),
                _LineSpec(accum_code, _ZERO, amt, "مجمع إهلاك"),
            ],
        )
        if entry is not None:
            posted += 1
    return posted


def post_inventory_purchase_shadow(db: Session, purchase: Purchase) -> None:
    if purchase.kind != PurchaseKind.INVENTORY:
        return
    from modules.payments.cost_reference import is_cost_reference_purchase
    from modules.payments.models import PurchaseLineKind
    from modules.gl.role_maps import role_account_code

    if is_cost_reference_purchase(purchase):
        return
    amount = _q(purchase.amount)
    if amount <= 0:
        return

    inv_total = _ZERO
    fixed_total = _ZERO
    consumable_total = _ZERO
    debit_lines: list[_LineSpec] = []
    for line in purchase.lines or []:
        amt = _q(line.line_total)
        if amt <= 0:
            continue
        kind = getattr(line, "resolved_line_kind", None) or (
            PurchaseLineKind.PRODUCT.value
            if line.product_id is not None
            else (
                PurchaseLineKind.FIXED_ASSET.value
                if int(line.useful_life_months or 0) > 0
                else PurchaseLineKind.CONSUMABLE.value
            )
        )
        if kind == PurchaseLineKind.FIXED_ASSET.value:
            fixed_total += amt
            debit_lines.append(
                _LineSpec(
                    role_account_code(db, "fixed_asset"),
                    amt,
                    _ZERO,
                    (line.item_name or "أصل ثابت")[:255],
                )
            )
        elif kind == PurchaseLineKind.CONSUMABLE.value:
            consumable_total += amt
            debit_lines.append(
                _LineSpec(
                    role_account_code(db, "consumable_asset"),
                    amt,
                    _ZERO,
                    (line.item_name or "مستلزم")[:255],
                )
            )
        else:
            inv_total += amt

    if inv_total > 0:
        debit_lines.insert(
            0, _LineSpec(CODE_INVENTORY, inv_total, _ZERO, "استلام مخزون")
        )
    if not debit_lines:
        debit_lines.append(_LineSpec(CODE_INVENTORY, amount, _ZERO, "استلام مخزون"))
        total_d = amount
    else:
        total_d = sum((ln.debit for ln in debit_lines), _ZERO)

    post_balanced_entry(
        db,
        idempotency_key=f"purchase:inventory:{purchase.id}",
        source_type="purchase_inventory",
        source_id=purchase.id,
        description_ar=f"شراء مخزون #{purchase.id}"
        + (" (مختلط)" if fixed_total or consumable_total else ""),
        entry_date=_entry_date_from_dt(purchase.created_at),
        lines=debit_lines
        + [_LineSpec(CODE_AP, _ZERO, total_d, "ذمم مورد")],
        created_by_id=purchase.created_by_id,
    )


def post_hotel_booking_payment_shadow(db: Session, hp: HotelBookingPayment) -> None:
    amount = _q(hp.amount)
    if amount <= 0 or hp.payment_method_id is None:
        return
    cash_code = _cash_account_code_for_pm(db, int(hp.payment_method_id))
    booking = hp.booking
    ref = booking.reference if booking is not None else hp.booking_id
    post_balanced_entry(
        db,
        idempotency_key=f"hotel_booking_payment:{hp.id}",
        source_type="hotel_booking_payment",
        source_id=hp.id,
        description_ar=f"تحصيل حجز فندق {ref}",
        entry_date=_entry_date_from_dt(hp.created_at),
        lines=[
            _LineSpec(cash_code, amount, _ZERO, "تحصيل فندق نقد/بنك"),
            _LineSpec(CODE_HOTEL_REVENUE, _ZERO, amount, "إيراد إقامة فندق"),
        ],
        created_by_id=hp.received_by_id,
    )


def post_hotel_booking_payment_refund_shadow(
    db: Session, ref: HotelBookingPaymentRefund
) -> None:
    amount = _q(ref.amount)
    if amount <= 0:
        return
    hp = ref.payment
    if hp is None or hp.payment_method_id is None:
        return
    cash_code = _cash_account_code_for_pm(db, int(hp.payment_method_id))
    booking = hp.booking
    booking_ref = booking.reference if booking is not None else hp.booking_id
    post_balanced_entry(
        db,
        idempotency_key=f"hotel_booking_payment_refund:{ref.id}",
        source_type="hotel_booking_payment_refund",
        source_id=ref.id,
        description_ar=f"رد دفعة حجز فندق {booking_ref}",
        entry_date=_entry_date_from_dt(ref.created_at),
        lines=[
            _LineSpec(CODE_HOTEL_REVENUE, amount, _ZERO, "مرتجع إيراد إقامة"),
            _LineSpec(cash_code, _ZERO, amount, "رد نقد/بنك"),
        ],
        created_by_id=ref.approved_by_id,
    )


def post_purchase_payment_shadow(db: Session, pp: PurchasePayment) -> None:
    amount = _q(pp.amount)
    if amount <= 0:
        return
    cash_code = _cash_account_code_for_pm(db, int(pp.payment_method_id))
    post_balanced_entry(
        db,
        idempotency_key=f"purchase_payment:{pp.id}",
        source_type="purchase_payment",
        source_id=pp.id,
        description_ar=f"سداد مشتريات #{pp.purchase_id}",
        entry_date=_entry_date_from_dt(pp.created_at),
        lines=[
            _LineSpec(CODE_AP, amount, _ZERO, "سداد مورد"),
            _LineSpec(cash_code, _ZERO, amount, "صرف نقد/بنك"),
        ],
        created_by_id=pp.created_by_id,
    )


def post_payment_transfer_shadow(db: Session, tf: PaymentTransfer) -> None:
    amount = _q(tf.amount)
    if amount <= 0:
        return
    from_code = _cash_account_code_for_pm(db, int(tf.from_payment_method_id))
    to_code = _cash_account_code_for_pm(db, int(tf.to_payment_method_id))
    lines = [
        _LineSpec(to_code, amount, _ZERO, "تحويل وارد"),
        _LineSpec(from_code, _ZERO, amount, "تحويل صادر"),
    ]
    if tf.transfer_type == PaymentTransferType.OWNER_DRAW:
        lines = [
            _LineSpec(CODE_EQUITY, amount, _ZERO, "سحب مالك"),
            _LineSpec(from_code, _ZERO, amount, "صرف"),
        ]
    elif tf.transfer_type == PaymentTransferType.OWNER_CAPITAL:
        lines = [
            _LineSpec(to_code, amount, _ZERO, "إيداع"),
            _LineSpec(CODE_EQUITY, _ZERO, amount, "رأس مال مالك"),
        ]
    post_balanced_entry(
        db,
        idempotency_key=f"payment_transfer:{tf.id}",
        source_type="payment_transfer",
        source_id=tf.id,
        description_ar=(tf.note or f"تحويل #{tf.id}")[:255],
        entry_date=_entry_date_from_dt(tf.created_at),
        lines=lines,
        created_by_id=tf.created_by_id,
    )


def post_payroll_accrual_shadow(db: Session, run: PayrollRun) -> GlJournalEntry | None:
    amount = _q(run.total_net)
    if amount <= 0:
        return None
    return post_balanced_entry(
        db,
        idempotency_key=f"payroll:accrual:{run.id}",
        source_type="payroll_accrual",
        source_id=run.id,
        description_ar=f"استحقاق رواتب {run.label}",
        entry_date=_entry_date_from_dt(run.posted_at or run.created_at),
        lines=[
            _LineSpec(CODE_PAYROLL_EXPENSE, amount, _ZERO, "مصروف رواتب"),
            _LineSpec(CODE_PAYROLL_PAYABLE, _ZERO, amount, "رواتب مستحقة"),
        ],
        business_domain=getattr(run, "business_domain", None),
    )


def post_payroll_payment_shadow(
    db: Session,
    run: PayrollRun,
    *,
    payment_method_id: int,
    purchase_id: int | None = None,
) -> GlJournalEntry | None:
    amount = _q(run.total_net)
    if amount <= 0:
        return None
    cash_code = _cash_account_code_for_pm(db, payment_method_id)
    return post_balanced_entry(
        db,
        idempotency_key=f"payroll:payment:{run.id}",
        source_type="payroll_payment",
        source_id=run.id,
        description_ar=f"صرف رواتب {run.label}",
        entry_date=_entry_date_from_dt(run.paid_at),
        lines=[
            _LineSpec(CODE_PAYROLL_PAYABLE, amount, _ZERO, "سداد مستحق رواتب"),
            _LineSpec(cash_code, _ZERO, amount, "صرف نقد/بنك"),
        ],
        business_domain=getattr(run, "business_domain", None),
    )


def post_sale_completed_shadow_safe(db: Session, sale: Sale) -> None:
    _safe("sale_completed", post_sale_completed_shadow, db, sale)
    _safe("sale_cogs", post_sale_cogs_shadow, db, sale)


def post_sale_cogs_shadow_safe(db: Session, sale: Sale) -> None:
    _safe("sale_cogs", post_sale_cogs_shadow, db, sale)


def post_sale_payment_shadow_safe(db: Session, sp: SalePayment) -> None:
    _safe("sale_payment", post_sale_payment_shadow, db, sp)


def post_sale_return_shadow_safe(db: Session, sale_return: SaleReturn) -> None:
    _safe("sale_return", post_sale_return_shadow, db, sale_return)
    _safe("sale_return_cogs", post_sale_return_cogs_shadow, db, sale_return)


def post_refund_payment_shadow_safe(db: Session, rp: RefundPayment) -> None:
    _safe("refund_payment", post_refund_payment_shadow, db, rp)


def post_hotel_booking_payment_shadow_safe(
    db: Session, hp: HotelBookingPayment
) -> None:
    _safe("hotel_booking_payment", post_hotel_booking_payment_shadow, db, hp)


def post_hotel_booking_payment_refund_shadow_safe(
    db: Session, ref: HotelBookingPaymentRefund
) -> None:
    _safe(
        "hotel_booking_payment_refund",
        post_hotel_booking_payment_refund_shadow,
        db,
        ref,
    )


def post_expense_shadow_safe(db: Session, purchase: Purchase) -> None:
    _safe("expense", post_expense_shadow, db, purchase)


def post_asset_purchase_shadow_safe(db: Session, purchase: Purchase) -> None:
    _safe("asset_purchase", post_asset_purchase_shadow, db, purchase)


def post_inventory_purchase_shadow_safe(db: Session, purchase: Purchase) -> None:
    _safe("inventory_purchase", post_inventory_purchase_shadow, db, purchase)


def post_purchase_payment_shadow_safe(db: Session, pp: PurchasePayment) -> None:
    _safe("purchase_payment", post_purchase_payment_shadow, db, pp)


def post_payment_transfer_shadow_safe(db: Session, tf: PaymentTransfer) -> None:
    _safe("payment_transfer", post_payment_transfer_shadow, db, tf)


def post_payroll_accrual_shadow_safe(db: Session, run: PayrollRun) -> None:
    _safe("payroll_accrual", post_payroll_accrual_shadow, db, run)


def post_payroll_payment_shadow_safe(
    db: Session,
    run: PayrollRun,
    *,
    payment_method_id: int,
    purchase_id: int | None = None,
) -> None:
    _safe(
        "payroll_payment",
        post_payroll_payment_shadow,
        db,
        run,
        payment_method_id=payment_method_id,
        purchase_id=purchase_id,
    )
