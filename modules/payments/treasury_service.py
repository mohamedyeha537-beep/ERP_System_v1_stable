from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.delivery.models import DeliveryCashSettlement
from modules.payments.models import (
    PaymentMethod,
    PaymentMethodKind,
    PaymentTransfer,
    PaymentTransferType,
    Purchase,
    RefundPayment,
    SalePayment,
)
from modules.payments.service import list_payment_methods, wallet_breakdown
from modules.pos_shifts.models import PosShift, PosShiftStatus


@dataclass
class TreasuryKindSummary:
    kind: PaymentMethodKind
    label_ar: str
    current_balance: Decimal
    last_close_balance: Decimal | None
    last_close_shift_id: int | None
    last_close_at: datetime | None


@dataclass
class LedgerEntry:
    at: datetime
    direction: str  # IN | OUT
    amount: Decimal
    category_ar: str
    reference: str
    method_name: str
    method_id: int


@dataclass
class DailyBalanceRow:
    day: date
    opening: Decimal
    total_in: Decimal
    total_out: Decimal
    closing: Decimal


def _transfer_in_label(tr: PaymentTransfer) -> str:
    tt = tr.transfer_type
    if tt == PaymentTransferType.OWNER_CAPITAL:
        return "إيداع رأس مال من المالك"
    if tt == PaymentTransferType.OWNER_DRAW:
        return "تحويل وارد (استلام عهدة/تمويل)"
    if tt == PaymentTransferType.REFUND_SETTLEMENT:
        return "تسوية مرتجع واردة"
    if tt == PaymentTransferType.SHIFT_HANDOFF:
        return "اعتماد جلسة — وارد للخزينة الرئيسية"
    return "تحويل وارد بين الحسابات"


def _transfer_out_label(tr: PaymentTransfer) -> str:
    tt = tr.transfer_type
    if tt == PaymentTransferType.OWNER_CAPITAL:
        return "إيداع رأس مال (مصدر حقوق ملكية)"
    if tt == PaymentTransferType.OWNER_DRAW:
        return "سحب مالك (حقوق ملكية)"
    if tt == PaymentTransferType.REFUND_SETTLEMENT:
        return "تسوية مرتجع صادرة"
    if tt == PaymentTransferType.SHIFT_HANDOFF:
        return "اعتماد جلسة — تصفير خزينة الكاشير"
    return "تحويل صادر بين الحسابات"


def _transfer_reference(tr: PaymentTransfer) -> str:
    if tr.transfer_type == PaymentTransferType.OWNER_CAPITAL:
        return f"إيداع مالك #{tr.id}"
    if tr.transfer_type == PaymentTransferType.OWNER_DRAW:
        return f"سحب مالك #{tr.id}"
    if tr.sale_return_id:
        return f"مرتجع #{tr.sale_return_id} · تحويل #{tr.id}"
    return f"تحويل #{tr.id}"


def _method_ids_for_kind(db: Session, kind: PaymentMethodKind) -> list[int]:
    return [m.id for m in list_payment_methods(db, only_active=False) if m.kind == kind]


def kind_current_balance(db: Session, kind: PaymentMethodKind) -> Decimal:
    total = Decimal("0")
    for row in wallet_breakdown(db, None, None):
        if row.method.kind == kind:
            total += row.net
    return total.quantize(Decimal("0.001"))


def get_last_closed_shift(db: Session) -> PosShift | None:
    return db.execute(
        select(PosShift)
        .where(PosShift.status == PosShiftStatus.CLOSED)
        .order_by(PosShift.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def last_close_balance_for_kind(db: Session, kind: PaymentMethodKind) -> tuple[Decimal | None, PosShift | None]:
    sh = get_last_closed_shift(db)
    if sh is None:
        return None, None
    if kind == PaymentMethodKind.CASH:
        return sh.counted_cash, sh
    if kind == PaymentMethodKind.BANK:
        return sh.counted_bank, sh
    return None, sh


def treasury_summary_for_method(db: Session, payment_method_id: int) -> TreasuryKindSummary:
    from modules.payments.service import payment_method_balances_map

    pm = db.get(PaymentMethod, payment_method_id)
    if pm is None:
        raise ValueError("payment method not found")
    last_sh = get_last_closed_shift(db)
    last_bal: Decimal | None = None
    if last_sh and pm.kind == PaymentMethodKind.CASH:
        last_bal = last_sh.counted_cash
    elif last_sh and pm.kind == PaymentMethodKind.BANK:
        last_bal = last_sh.counted_bank
    return TreasuryKindSummary(
        kind=pm.kind,
        label_ar=pm.name_ar,
        current_balance=payment_method_balances_map(db)
        .get(pm.id, Decimal("0"))
        .quantize(Decimal("0.001")),
        last_close_balance=last_bal,
        last_close_shift_id=last_sh.id if last_sh else None,
        last_close_at=last_sh.closed_at if last_sh else None,
    )


def treasury_summary_for_kind(db: Session, kind: PaymentMethodKind) -> TreasuryKindSummary:
    last_sh = get_last_closed_shift(db)
    last_bal, _ = last_close_balance_for_kind(db, kind)
    label = (
        "خزينة الكاش" if kind == PaymentMethodKind.CASH else "خزينة المصرف"
    )
    return TreasuryKindSummary(
        kind=kind,
        label_ar=label,
        current_balance=kind_current_balance(db, kind),
        last_close_balance=last_bal,
        last_close_shift_id=last_sh.id if last_sh else None,
        last_close_at=last_sh.closed_at if last_sh else None,
    )


def treasury_summaries(db: Session) -> dict[str, TreasuryKindSummary]:
    last_sh = get_last_closed_shift(db)
    out: dict[str, TreasuryKindSummary] = {}
    for kind, label in (
        (PaymentMethodKind.CASH, "خزينة الكاش"),
        (PaymentMethodKind.BANK, "خزينة المصرف"),
    ):
        last_bal, _ = last_close_balance_for_kind(db, kind)
        out[kind.value] = TreasuryKindSummary(
            kind=kind,
            label_ar=label,
            current_balance=kind_current_balance(db, kind),
            last_close_balance=last_bal,
            last_close_shift_id=last_sh.id if last_sh else None,
            last_close_at=last_sh.closed_at if last_sh else None,
        )
    return out


def list_ledger_entries(
    db: Session,
    kind: PaymentMethodKind,
    *,
    direction: str | None = None,
    day: date | None = None,
    payment_method_id: int | None = None,
) -> list[LedgerEntry]:
    """سجل حركات الخزينة لنوع كاش أو مصرف، أو لحساب واحد."""
    if payment_method_id is not None:
        method_ids = {payment_method_id}
    else:
        method_ids = set(_method_ids_for_kind(db, kind))
    if not method_ids:
        return []
    methods = {m.id: m for m in list_payment_methods(db, only_active=False) if m.id in method_ids}
    entries: list[LedgerEntry] = []

    sp_rows = db.execute(
        select(SalePayment, PaymentMethod)
        .join(PaymentMethod, PaymentMethod.id == SalePayment.payment_method_id)
        .where(PaymentMethod.id.in_(method_ids))
    ).all()
    for sp, pm in sp_rows:
        amt = Decimal(str(sp.amount or 0)).quantize(Decimal("0.001"))
        if amt <= 0:
            continue
        entries.append(
            LedgerEntry(
                at=sp.created_at or datetime.now(timezone.utc),
                direction="IN",
                amount=amt,
                category_ar="تحصيل بيع",
                reference=f"فاتورة #{sp.sale_id}",
                method_name=pm.name_ar,
                method_id=pm.id,
            )
        )

    rp_rows = db.execute(
        select(RefundPayment, PaymentMethod)
        .join(PaymentMethod, PaymentMethod.id == RefundPayment.payment_method_id)
        .where(PaymentMethod.id.in_(method_ids))
    ).all()
    for rp, pm in rp_rows:
        amt = Decimal(str(rp.amount or 0)).quantize(Decimal("0.001"))
        if amt <= 0:
            continue
        entries.append(
            LedgerEntry(
                at=rp.created_at or datetime.now(timezone.utc),
                direction="OUT",
                amount=amt,
                category_ar="مرتجع",
                reference=f"مرتجع #{rp.sale_return_id}",
                method_name=pm.name_ar,
                method_id=pm.id,
            )
        )

    from modules.payments.models import PurchasePayment

    pur_rows = db.execute(
        select(PurchasePayment, PaymentMethod)
        .join(PaymentMethod, PaymentMethod.id == PurchasePayment.payment_method_id)
        .where(PaymentMethod.id.in_(method_ids))
    ).all()
    for pp, pm in pur_rows:
        amt = Decimal(str(pp.amount or 0)).quantize(Decimal("0.001"))
        if amt <= 0:
            continue
        entries.append(
            LedgerEntry(
                at=pp.created_at or datetime.now(timezone.utc),
                direction="OUT",
                amount=amt,
                category_ar="صرف (شراء/مصروف)",
                reference=f"عملية #{pp.purchase_id}",
                method_name=pm.name_ar,
                method_id=pm.id,
            )
        )

    from modules.hotel.booking_models import (
        HotelBooking,
        HotelBookingPayment,
        HotelBookingPaymentRefund,
    )

    hp_rows = db.execute(
        select(HotelBookingPayment, PaymentMethod, HotelBooking.reference)
        .join(PaymentMethod, PaymentMethod.id == HotelBookingPayment.payment_method_id)
        .join(HotelBooking, HotelBooking.id == HotelBookingPayment.booking_id)
        .where(HotelBookingPayment.payment_method_id.in_(method_ids))
    ).all()
    for hp, pm, booking_ref in hp_rows:
        amt = Decimal(str(hp.amount or 0)).quantize(Decimal("0.001"))
        if amt <= 0:
            continue
        ref = (booking_ref or "").strip() or f"#{hp.booking_id}"
        cat = "عربون حجز" if hp.is_deposit else "تحصيل حجز"
        entries.append(
            LedgerEntry(
                at=hp.created_at or datetime.now(timezone.utc),
                direction="IN",
                amount=amt,
                category_ar=cat,
                reference=f"حجز {ref}",
                method_name=pm.name_ar,
                method_id=pm.id,
            )
        )

    hr_rows = db.execute(
        select(
            HotelBookingPaymentRefund,
            PaymentMethod,
            HotelBooking.reference,
            HotelBooking.id,
        )
        .join(
            HotelBookingPayment,
            HotelBookingPayment.id == HotelBookingPaymentRefund.payment_id,
        )
        .join(PaymentMethod, PaymentMethod.id == HotelBookingPayment.payment_method_id)
        .join(HotelBooking, HotelBooking.id == HotelBookingPayment.booking_id)
        .where(HotelBookingPayment.payment_method_id.in_(method_ids))
    ).all()
    for hr, pm, booking_ref, booking_id in hr_rows:
        amt = Decimal(str(hr.amount or 0)).quantize(Decimal("0.001"))
        if amt <= 0:
            continue
        ref = (booking_ref or "").strip() or f"#{booking_id}"
        entries.append(
            LedgerEntry(
                at=hr.created_at or datetime.now(timezone.utc),
                direction="OUT",
                amount=amt,
                category_ar="مرتجع حجز",
                reference=f"حجز {ref}",
                method_name=pm.name_ar,
                method_id=pm.id,
            )
        )

    tin = db.execute(
        select(PaymentTransfer, PaymentMethod)
        .join(PaymentMethod, PaymentMethod.id == PaymentTransfer.to_payment_method_id)
        .where(PaymentTransfer.to_payment_method_id.in_(method_ids))
    ).all()
    for tr, pm in tin:
        amt = Decimal(str(tr.amount or 0)).quantize(Decimal("0.001"))
        if amt <= 0:
            continue
        entries.append(
            LedgerEntry(
                at=tr.created_at or datetime.now(timezone.utc),
                direction="IN",
                amount=amt,
                category_ar=_transfer_in_label(tr),
                reference=_transfer_reference(tr),
                method_name=pm.name_ar,
                method_id=pm.id,
            )
        )

    tout = db.execute(
        select(PaymentTransfer, PaymentMethod)
        .join(PaymentMethod, PaymentMethod.id == PaymentTransfer.from_payment_method_id)
        .where(PaymentTransfer.from_payment_method_id.in_(method_ids))
    ).all()
    for tr, pm in tout:
        amt = Decimal(str(tr.amount or 0)).quantize(Decimal("0.001"))
        if amt <= 0:
            continue
        entries.append(
            LedgerEntry(
                at=tr.created_at or datetime.now(timezone.utc),
                direction="OUT",
                amount=amt,
                category_ar=_transfer_out_label(tr),
                reference=_transfer_reference(tr),
                method_name=pm.name_ar,
                method_id=pm.id,
            )
        )

    show_delivery = kind == PaymentMethodKind.CASH
    if payment_method_id is not None:
        pm0 = methods.get(payment_method_id)
        show_delivery = pm0 is not None and pm0.kind == PaymentMethodKind.CASH
    if show_delivery:
        drows = db.execute(
            select(DeliveryCashSettlement, PaymentMethod)
            .join(PaymentMethod, PaymentMethod.id == DeliveryCashSettlement.cash_method_id)
            .where(DeliveryCashSettlement.cash_method_id.in_(method_ids))
        ).all()
        for d, pm in drows:
            amt = Decimal(str(d.amount or 0)).quantize(Decimal("0.001"))
            if amt <= 0:
                continue
            entries.append(
                LedgerEntry(
                    at=d.created_at or datetime.now(timezone.utc),
                    direction="OUT",
                    amount=amt,
                    category_ar="أجرة توصيل",
                    reference=f"فاتورة #{d.sale_id}",
                    method_name=pm.name_ar,
                    method_id=pm.id,
                )
            )

    entries.sort(key=lambda e: (e.at, e.reference))

    if day is not None:
        entries = [e for e in entries if e.at.date() == day]

    if direction == "in":
        entries = [e for e in entries if e.direction == "IN"]
    elif direction == "out":
        entries = [e for e in entries if e.direction == "OUT"]

    return entries


def daily_balance_rows(
    db: Session,
    kind: PaymentMethodKind,
    *,
    entries: list[LedgerEntry] | None = None,
) -> list[DailyBalanceRow]:
    """رصيد افتتاحي/ختامي لكل يوم حسب ترتيب الحركات."""
    all_entries = entries if entries is not None else list_ledger_entries(db, kind)
    if not all_entries:
        return []

    by_day: dict[date, list[LedgerEntry]] = {}
    for e in all_entries:
        by_day.setdefault(e.at.date(), []).append(e)

    running = Decimal("0")
    rows: list[DailyBalanceRow] = []
    for day in sorted(by_day.keys()):
        opening = running
        day_in = Decimal("0")
        day_out = Decimal("0")
        for e in by_day[day]:
            if e.direction == "IN":
                day_in += e.amount
                running += e.amount
            else:
                day_out += e.amount
                running -= e.amount
        rows.append(
            DailyBalanceRow(
                day=day,
                opening=opening.quantize(Decimal("0.001")),
                total_in=day_in.quantize(Decimal("0.001")),
                total_out=day_out.quantize(Decimal("0.001")),
                closing=running.quantize(Decimal("0.001")),
            )
        )
    return rows
