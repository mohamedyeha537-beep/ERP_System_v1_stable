"""لوحة أمين الخزينة — أرصدة، طابور إجراءات، عهد، افتتاح/إقفال يومي."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from modules.payments.models import (
    HOTEL_PURCHASE_CUSTODY_BANK_PM_NAME,
    HOTEL_PURCHASE_CUSTODY_CASH_PM_NAME,
    PaymentMethod,
    PaymentMethodKind,
    PaymentTransfer,
    PaymentTransferType,
    Purchase,
    PurchaseKind,
    RESTAURANT_PURCHASE_CUSTODY_BANK_PM_NAME,
    RESTAURANT_PURCHASE_CUSTODY_CASH_PM_NAME,
)
from modules.payments.purchase_advance_models import (
    PurchaseAdvance,
    PurchaseAdvanceStatus,
)
from modules.payments.service import (
    PaymentsError,
    ensure_hotel_reception_payment_methods,
    ensure_hotel_treasury_payment_methods,
    is_hotel_treasury_payment_method,
    is_purchase_custody_payment_method,
    method_current_balance,
    record_manual_transfer,
)
from modules.payments.shift_handoff_service import (
    ensure_main_treasury_payment_methods,
    is_main_treasury_payment_method,
    list_cashier_wallet_methods,
    list_shifts_pending_handoff,
)
from modules.payments.shift_handover_models import (
    ShiftHandover,
    ShiftHandoverKind,
    ShiftHandoverStatus,
)
from modules.payments.shift_variances import money3
from modules.payments.treasury_session_models import (
    TreasurySession,
    TreasurySessionStatus,
)


class TreasuryDeskError(PaymentsError):
    pass


def _emp_name(emp) -> str:
    if emp is None:
        return ""
    return (getattr(emp, "full_name_ar", None) or "").strip()


def _user_name(user) -> str:
    if user is None:
        return ""
    return (getattr(user, "username", None) or "").strip()


def _shift_person(shift) -> str:
    emp = getattr(shift, "employee", None)
    name = _emp_name(emp)
    if name:
        return name
    return _user_name(getattr(shift, "user", None)) or f"#{getattr(shift, 'id', '')}"


@dataclass
class TreasuryBalances:
    restaurant_cash: Decimal
    restaurant_bank: Decimal
    hotel_cash: Decimal
    hotel_bank: Decimal
    custody_total: Decimal
    pending_variance_count: int
    pending_variance_abs: Decimal

    @property
    def cash_total(self) -> Decimal:
        return (self.restaurant_cash + self.hotel_cash).quantize(Decimal("0.001"))

    @property
    def bank_total(self) -> Decimal:
        return (self.restaurant_bank + self.hotel_bank).quantize(Decimal("0.001"))

    @property
    def restaurant_total(self) -> Decimal:
        return (self.restaurant_cash + self.restaurant_bank).quantize(Decimal("0.001"))

    @property
    def hotel_total(self) -> Decimal:
        return (self.hotel_cash + self.hotel_bank).quantize(Decimal("0.001"))


@dataclass
class DeskAction:
    kind: str  # cash_handoff | bank_match | custody_review | variance
    severity: str  # red | yellow | orange
    title: str
    amount: Decimal
    href: str
    button: str
    domain: str = ""
    shift_id: int | None = None
    handover_id: int | None = None
    advance_id: int | None = None


@dataclass
class DayMovement:
    cash_in: Decimal = Decimal("0")
    bank_in: Decimal = Decimal("0")
    payments_out: Decimal = Decimal("0")
    advances_out: Decimal = Decimal("0")


@dataclass
class HolderRow:
    holder: str
    kind_ar: str
    origin: Decimal
    used: Decimal
    remaining: Decimal
    detail_href: str
    domain: str = ""


@dataclass
class ReceiptRow:
    domain: str
    domain_ar: str
    shift_id: int
    person: str
    claimed_cash: Decimal
    claimed_bank: Decimal
    expected_cash: Decimal
    expected_bank: Decimal
    closed_at: datetime | None
    can_approve: bool
    report_href: str
    bank_ref: str = ""
    bank_name: str = ""
    handover_id: int | None = None
    status_ar: str = "بانتظار الاستلام"


@dataclass
class ReceiptVoucherRow:
    id: str
    created_at: datetime | None
    item: str
    party: str
    amount: Decimal
    print_href: str = ""


@dataclass
class AdvanceView:
    id: int
    ref: str
    employee_name: str
    amount: Decimal
    used: Decimal
    returned: Decimal
    remaining: Decimal
    purpose: str
    domain: str
    domain_ar: str
    status: str
    custody_name: str
    source_name: str
    created_at: datetime | None
    docs: list
    can_close: bool


def _domain_key(domain) -> str | None:
    if domain is None:
        return None
    val = domain.value if hasattr(domain, "value") else str(domain).strip().lower()
    return val if val in ("hotel", "restaurant") else None


def treasury_pay_methods(db: Session, domain=None) -> list[PaymentMethod]:
    mains = ensure_main_treasury_payment_methods(db)
    hotels = ensure_hotel_treasury_payment_methods(db)
    key = _domain_key(domain)
    if key == "hotel":
        return [hotels["CASH"], hotels["BANK"]]
    if key == "restaurant":
        return [mains["CASH"], mains["BANK"]]
    return [mains["CASH"], mains["BANK"], hotels["CASH"], hotels["BANK"]]


def treasury_pay_balances(db: Session, domain=None) -> dict[int, Decimal]:
    return {
        int(m.id): method_current_balance(db, m.id)
        for m in treasury_pay_methods(db, domain=domain)
    }


def parse_charge_domain(raw: str) -> str:
    val = (raw or "").strip().lower()
    if val not in ("restaurant", "hotel"):
        raise TreasuryDeskError("اختر وضعية المصروف: مطعم أو فندق.")
    return val


def pay_method_domain(pm: PaymentMethod) -> str:
    raw = getattr(pm, "business_domain", None)
    val = raw.value if hasattr(raw, "value") else str(raw or "").strip().lower()
    if val == "hotel" or is_hotel_treasury_payment_method(pm):
        return "hotel"
    return "restaurant"


def custody_wallets(db: Session) -> list[PaymentMethod]:
    from modules.gl.purchase_custody_wallets import ensure_purchase_custody_wallets

    ensure_purchase_custody_wallets(db)
    names = (
        RESTAURANT_PURCHASE_CUSTODY_CASH_PM_NAME,
        RESTAURANT_PURCHASE_CUSTODY_BANK_PM_NAME,
        HOTEL_PURCHASE_CUSTODY_CASH_PM_NAME,
        HOTEL_PURCHASE_CUSTODY_BANK_PM_NAME,
    )
    out: list[PaymentMethod] = []
    for name in names:
        pm = db.scalar(select(PaymentMethod).where(PaymentMethod.name_ar == name))
        if pm is not None:
            out.append(pm)
    return out


def load_treasury_balances(db: Session, domain=None) -> TreasuryBalances:
    mains = ensure_main_treasury_payment_methods(db)
    hotels = ensure_hotel_treasury_payment_methods(db)
    key = _domain_key(domain)
    custody = Decimal("0")
    for pm in custody_wallets(db):
        pm_dom = pay_method_domain(pm)
        if key and pm_dom != key:
            continue
        custody += method_current_balance(db, pm.id)
    from modules.payments.shift_variances import count_pending_variances
    from modules.payments.shift_variance_models import (
        ShiftVariance,
        ShiftVarianceStatus,
    )

    pending_abs = db.scalar(
        select(func.coalesce(func.sum(func.abs(ShiftVariance.difference)), 0)).where(
            ShiftVariance.status == ShiftVarianceStatus.PENDING_REVIEW
        )
    )
    return TreasuryBalances(
        restaurant_cash=method_current_balance(db, mains["CASH"].id),
        restaurant_bank=method_current_balance(db, mains["BANK"].id),
        hotel_cash=method_current_balance(db, hotels["CASH"].id),
        hotel_bank=method_current_balance(db, hotels["BANK"].id),
        custody_total=money3(custody),
        pending_variance_count=count_pending_variances(db),
        pending_variance_abs=money3(pending_abs),
    )


def _handoff_claimed(shift) -> tuple[Decimal, Decimal]:
    cash = shift.counted_cash if shift.counted_cash is not None else shift.expected_cash
    bank = shift.counted_bank if shift.counted_bank is not None else shift.expected_bank
    return money3(cash), money3(bank)


def list_cash_receipt_rows(db: Session, domain: str | None = None) -> list[ReceiptRow]:
    from modules.hotel.shift_handoff import (
        get_next_hotel_shift_pending_handoff,
        list_hotel_shifts_pending_handoff,
    )
    from modules.payments.shift_handoff_service import get_next_shift_pending_handoff

    from modules.payments.shift_handovers import get_bank_declaration

    next_pos = get_next_shift_pending_handoff(db)
    next_hotel = get_next_hotel_shift_pending_handoff(db)
    rows: list[ReceiptRow] = []
    for sh in list_shifts_pending_handoff(db, limit=80):
        claimed_c, claimed_b = _handoff_claimed(sh)
        decl = get_bank_declaration(db, pos_shift_id=sh.id)
        if decl is not None and decl.received_bank is not None:
            claimed_b = money3(decl.received_bank)
        rows.append(
            ReceiptRow(
                domain="restaurant",
                domain_ar="مطعم",
                shift_id=int(sh.id),
                person=_shift_person(sh),
                claimed_cash=claimed_c,
                claimed_bank=claimed_b,
                expected_cash=money3(sh.expected_cash),
                expected_bank=money3(sh.expected_bank),
                closed_at=sh.closed_at,
                can_approve=next_pos is not None and next_pos.id == sh.id,
                report_href=f"/reports/shifts/{sh.id}",
            )
        )
    for sh in list_hotel_shifts_pending_handoff(db, limit=80):
        claimed_c, claimed_b = _handoff_claimed(sh)
        decl = get_bank_declaration(db, hotel_shift_id=sh.id)
        if decl is not None and decl.received_bank is not None:
            claimed_b = money3(decl.received_bank)
        rows.append(
            ReceiptRow(
                domain="hotel",
                domain_ar="فندق",
                shift_id=int(sh.id),
                person=_shift_person(sh),
                claimed_cash=claimed_c,
                claimed_bank=claimed_b,
                expected_cash=money3(sh.expected_cash),
                expected_bank=money3(sh.expected_bank),
                closed_at=sh.closed_at,
                can_approve=next_hotel is not None and next_hotel.id == sh.id,
                report_href=f"/admin/hotel/shift/{sh.id}/report",
            )
        )
    if domain in ("hotel", "restaurant"):
        rows = [r for r in rows if r.domain == domain]
    return rows


def list_bank_receipt_rows(db: Session, domain: str | None = None) -> list[ReceiptRow]:
    from modules.payments.shift_handovers import list_pending_bank_transfers

    rows: list[ReceiptRow] = []
    for ho in list_pending_bank_transfers(db):
        row_domain = ho.domain or "restaurant"
        shift_id = int(ho.pos_shift_id or ho.hotel_shift_id or 0)
        href = (
            f"/admin/hotel/shift/{ho.hotel_shift_id}/report"
            if ho.hotel_shift_id
            else (f"/reports/shifts/{ho.pos_shift_id}" if ho.pos_shift_id else "/pos/treasury/banks")
        )
        rows.append(
            ReceiptRow(
                domain=row_domain,
                domain_ar="فندق" if row_domain == "hotel" else "مطعم",
                shift_id=shift_id,
                person=_emp_name(ho.from_employee) or "—",
                claimed_cash=Decimal("0"),
                claimed_bank=money3(ho.claimed_bank),
                expected_cash=Decimal("0"),
                expected_bank=money3(ho.claimed_bank),
                closed_at=ho.bank_transferred_at or ho.handover_at,
                can_approve=True,
                report_href=href,
                bank_ref=ho.bank_ref or "",
                bank_name=ho.bank_name or "",
                handover_id=int(ho.id),
                status_ar="تحقق",
            )
        )
    if domain in ("hotel", "restaurant"):
        rows = [r for r in rows if r.domain == domain]
    return rows


def _used_from_custody(
    db: Session, *, custody_pm_id: int, since: datetime, until: datetime | None = None
) -> Decimal:
    stmt = select(func.coalesce(func.sum(Purchase.amount), 0)).where(
        Purchase.payment_method_id == int(custody_pm_id),
        Purchase.created_at >= since,
    )
    if until is not None:
        stmt = stmt.where(Purchase.created_at <= until)
    return money3(db.scalar(stmt))


def _advance_docs(db: Session, adv: PurchaseAdvance) -> list[dict]:
    stmt = (
        select(Purchase)
        .where(
            Purchase.payment_method_id == int(adv.custody_pm_id),
            Purchase.created_at >= adv.created_at,
        )
        .order_by(Purchase.id.asc())
        .limit(80)
    )
    if adv.closed_at is not None:
        stmt = stmt.where(Purchase.created_at <= adv.closed_at)
    rows = list(db.scalars(stmt).all())
    out = []
    for p in rows:
        kind = p.kind.value if hasattr(p.kind, "value") else str(p.kind or "")
        href = "/admin/expenses" if kind == PurchaseKind.EXPENSE.value else f"/admin/purchases/{p.id}"
        out.append(
            {
                "id": p.id,
                "ref": p.supplier_invoice_ref or f"#{p.id}",
                "supplier": p.supplier or p.expense_category or "—",
                "amount": money3(p.amount),
                "kind_ar": "مصروف" if kind == PurchaseKind.EXPENSE.value else "مشتريات",
                "href": href,
            }
        )
    return out


def build_advance_view(db: Session, adv: PurchaseAdvance) -> AdvanceView:
    used = _used_from_custody(
        db,
        custody_pm_id=adv.custody_pm_id,
        since=adv.created_at,
        until=adv.closed_at,
    )
    used = min(used, money3(adv.amount) - money3(adv.returned_amount))
    remaining = (money3(adv.amount) - used - money3(adv.returned_amount)).quantize(
        Decimal("0.001")
    )
    if remaining < 0:
        remaining = Decimal("0.000")
    emp = getattr(adv, "employee", None)
    return AdvanceView(
        id=int(adv.id),
        ref=adv.ref,
        employee_name=_emp_name(emp) or "—",
        amount=money3(adv.amount),
        used=used,
        returned=money3(adv.returned_amount),
        remaining=remaining,
        purpose=adv.purpose or "",
        domain=adv.domain,
        domain_ar="فندق" if adv.domain == "hotel" else "مطعم",
        status=adv.status.value if hasattr(adv.status, "value") else str(adv.status),
        custody_name=getattr(adv.custody_pm, "name_ar", "") or "",
        source_name=getattr(adv.source_pm, "name_ar", "") or "",
        created_at=adv.created_at,
        docs=_advance_docs(db, adv),
        can_close=adv.status == PurchaseAdvanceStatus.OPEN
        and (used + money3(adv.returned_amount) - money3(adv.amount)).copy_abs()
        <= Decimal("0.001"),
    )


def list_purchase_advances(
    db: Session, *, status: str | None = None, limit: int = 80
) -> list[AdvanceView]:
    stmt = (
        select(PurchaseAdvance)
        .options(
            selectinload(PurchaseAdvance.employee),
            selectinload(PurchaseAdvance.source_pm),
            selectinload(PurchaseAdvance.custody_pm),
        )
        .order_by(PurchaseAdvance.id.desc())
        .limit(limit)
    )
    if status:
        try:
            st = PurchaseAdvanceStatus(status)
        except ValueError:
            st = None
        if st is not None:
            stmt = stmt.where(PurchaseAdvance.status == st)
    return [build_advance_view(db, a) for a in db.scalars(stmt).all()]


def list_open_advances_needing_review(db: Session) -> list[AdvanceView]:
    rows = list_purchase_advances(db, status=PurchaseAdvanceStatus.OPEN.value)
    return [r for r in rows if r.used > 0 or r.remaining > 0]


def list_money_holders(db: Session) -> list[HolderRow]:
    holders: list[HolderRow] = []
    recv = ensure_hotel_reception_payment_methods(db)
    for key, label in (("CASH", "استقبال — كاش"), ("BANK", "استقبال — مصرف")):
        bal = method_current_balance(db, recv[key].id)
        if bal > 0:
            holders.append(
                HolderRow(
                    holder="استقبال الفندق",
                    kind_ar=label,
                    origin=bal,
                    used=Decimal("0"),
                    remaining=bal,
                    detail_href="/pos/treasury?kind=" + ("cash" if key == "CASH" else "bank"),
                    domain="hotel",
                )
            )
    for kind in (PaymentMethodKind.CASH, PaymentMethodKind.BANK):
        for pm in list_cashier_wallet_methods(db, kind):
            bal = method_current_balance(db, pm.id)
            if bal <= 0:
                continue
            holders.append(
                HolderRow(
                    holder=pm.name_ar,
                    kind_ar="محفظة كاشير",
                    origin=bal,
                    used=Decimal("0"),
                    remaining=bal,
                    detail_href=f"/pos/treasury?pm={pm.id}",
                    domain="restaurant",
                )
            )
    sent = list(
        db.scalars(
            select(ShiftHandover)
            .options(
                selectinload(ShiftHandover.from_employee),
                selectinload(ShiftHandover.to_employee),
            )
            .where(
                ShiftHandover.kind == ShiftHandoverKind.CASH_CARRY,
                ShiftHandover.status == ShiftHandoverStatus.SENT,
            )
            .order_by(ShiftHandover.id.asc())
        ).all()
    )
    for ho in sent:
        remaining = money3(ho.claimed_cash) + money3(ho.claimed_bank)
        if remaining <= 0:
            continue
        holders.append(
            HolderRow(
                holder=f"{_emp_name(ho.from_employee) or '—'} → {_emp_name(ho.to_employee) or '—'}",
                kind_ar="تسليم عهدة بانتظار التأكيد",
                origin=remaining,
                used=Decimal("0"),
                remaining=remaining,
                detail_href="/pos/treasury/receipts",
                domain=ho.domain or "",
            )
        )
    for adv in list_purchase_advances(db, status=PurchaseAdvanceStatus.OPEN.value):
        holders.append(
            HolderRow(
                holder=adv.employee_name,
                kind_ar=f"مشتريات · {adv.ref}",
                origin=adv.amount,
                used=adv.used,
                remaining=adv.remaining,
                detail_href=f"/pos/treasury/custody/{adv.id}",
                domain=adv.domain,
            )
        )
    return holders


def _is_treasury_pm(pm: PaymentMethod | None) -> bool:
    if pm is None:
        return False
    return is_main_treasury_payment_method(pm) or is_hotel_treasury_payment_method(pm)


def session_movements(db: Session, opened_at: datetime) -> DayMovement:
    """حركة الخزائن الرئيسية (مطعم+فندق) منذ افتتاح جلسة أمين الخزينة."""
    mains = ensure_main_treasury_payment_methods(db)
    hotels = ensure_hotel_treasury_payment_methods(db)
    ids = {mains["CASH"].id, mains["BANK"].id, hotels["CASH"].id, hotels["BANK"].id}
    cash_ids = {mains["CASH"].id, hotels["CASH"].id}
    xfers = list(
        db.scalars(
            select(PaymentTransfer)
            .options(
                selectinload(PaymentTransfer.from_method),
                selectinload(PaymentTransfer.to_method),
            )
            .where(
                PaymentTransfer.created_at >= opened_at,
                or_(
                    PaymentTransfer.from_payment_method_id.in_(ids),
                    PaymentTransfer.to_payment_method_id.in_(ids),
                ),
            )
        ).all()
    )
    cash_in = bank_in = payments_out = advances_out = Decimal("0")
    for tr in xfers:
        amt = money3(tr.amount)
        to_pm = tr.to_method
        from_pm = tr.from_method
        to_treas = tr.to_payment_method_id in ids
        from_treas = tr.from_payment_method_id in ids
        is_cash = (tr.to_payment_method_id in cash_ids) or (
            tr.from_payment_method_id in cash_ids
        )
        if to_treas and not from_treas:
            if is_cash:
                cash_in += amt
            else:
                bank_in += amt
        elif from_treas and not to_treas:
            if to_pm is not None and is_purchase_custody_payment_method(to_pm):
                advances_out += amt
            else:
                payments_out += amt
    purchases = list(
        db.scalars(
            select(Purchase).where(
                Purchase.created_at >= opened_at,
                Purchase.payment_method_id.in_(ids),
            )
        ).all()
    )
    for p in purchases:
        payments_out += money3(p.amount)
    return DayMovement(
        cash_in=money3(cash_in),
        bank_in=money3(bank_in),
        payments_out=money3(payments_out),
        advances_out=money3(advances_out),
    )


def today_movement(db: Session) -> DayMovement:
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    open_sess = get_open_treasury_session(db)
    if open_sess is not None:
        start = open_sess.opened_at
    return session_movements(db, start)


def _receipt_transfer_item(tr: PaymentTransfer) -> str:
    tt = tr.transfer_type
    if tt == PaymentTransferType.SHIFT_HANDOFF:
        return "استلام وردية"
    if tt == PaymentTransferType.OWNER_CAPITAL:
        return "إيداع رأس مال"
    if tt == PaymentTransferType.OWNER_DRAW:
        return "تحويل وارد"
    return "تحويل وارد"


_HOTEL_SHIFT_NOTE_RE = re.compile(r"جلسة\s+فندق\s*#(\d+)")
_POS_SHIFT_NOTE_RE = re.compile(r"جلسة\s*#(\d+)")


def _receipt_transfer_print_href(tr: PaymentTransfer) -> str:
    text = (tr.note or "").strip()
    if not text:
        return ""
    hotel = _HOTEL_SHIFT_NOTE_RE.search(text)
    if hotel:
        return f"/admin/hotel/shift/{int(hotel.group(1))}/report"
    pos = _POS_SHIFT_NOTE_RE.search(text)
    if pos:
        return f"/pos/shift/{int(pos.group(1))}/report"
    return ""


def list_recent_receipt_vouchers(
    db: Session, *, days: int = 31, limit: int = 40
) -> list[ReceiptVoucherRow]:
    """آخر مقبوضات الخزينة (ورديات + إيصالات حجز) وليس ما بانتظار الاستلام."""
    end = datetime.now(timezone.utc) + timedelta(days=1)
    start = end - timedelta(days=max(int(days), 1))
    mains = ensure_main_treasury_payment_methods(db)
    hotels = ensure_hotel_treasury_payment_methods(db)
    treas_ids = {
        mains["CASH"].id,
        mains["BANK"].id,
        hotels["CASH"].id,
        hotels["BANK"].id,
    }
    rows: list[ReceiptVoucherRow] = []

    xfers = list(
        db.scalars(
            select(PaymentTransfer)
            .options(
                selectinload(PaymentTransfer.from_method),
                selectinload(PaymentTransfer.to_method),
            )
            .where(
                PaymentTransfer.created_at >= start,
                PaymentTransfer.created_at < end,
                PaymentTransfer.to_payment_method_id.in_(treas_ids),
                ~PaymentTransfer.from_payment_method_id.in_(treas_ids),
            )
            .order_by(PaymentTransfer.id.desc())
            .limit(limit)
        ).all()
    )
    for tr in xfers:
        from_name = (tr.from_method.name_ar if tr.from_method else "") or "—"
        rows.append(
            ReceiptVoucherRow(
                id=f"T{tr.id}",
                created_at=tr.created_at,
                item=_receipt_transfer_item(tr),
                party=(tr.note or "").strip() or from_name,
                amount=money3(tr.amount),
                print_href=_receipt_transfer_print_href(tr),
            )
        )

    from modules.hotel.booking_models import HotelBookingPayment

    hp_rows = list(
        db.scalars(
            select(HotelBookingPayment)
            .options(selectinload(HotelBookingPayment.booking))
            .where(
                HotelBookingPayment.created_at >= start,
                HotelBookingPayment.created_at < end,
                HotelBookingPayment.is_refunded.is_(False),
            )
            .order_by(HotelBookingPayment.id.desc())
            .limit(limit)
        ).all()
    )
    for hp in hp_rows:
        booking = hp.booking
        guest = (getattr(booking, "guest_name", None) or "").strip() if booking else ""
        ref = (getattr(booking, "reference", None) or "").strip() if booking else ""
        bid = int(hp.booking_id)
        item = "عربون حجز" if hp.is_deposit else "تحصيل حجز"
        if ref:
            item = f"{item} {ref}"
        rows.append(
            ReceiptVoucherRow(
                id=hp.receipt_number or f"H{hp.id}",
                created_at=hp.created_at,
                item=item,
                party=guest or "—",
                amount=money3(hp.amount),
                print_href=(
                    f"/admin/hotel/bookings/{bid}/receipt?doc=receipt&payment_id={hp.id}"
                ),
            )
        )

    def _at(row: ReceiptVoucherRow) -> datetime:
        at = row.created_at
        if at is None:
            return datetime.min.replace(tzinfo=timezone.utc)
        if at.tzinfo is None:
            return at.replace(tzinfo=timezone.utc)
        return at

    rows.sort(key=_at, reverse=True)
    return rows[:limit]


def build_required_actions(db: Session, domain=None) -> list[DeskAction]:
    actions: list[DeskAction] = []
    key = _domain_key(domain)
    for row in list_cash_receipt_rows(db, domain=key):
        if not row.can_approve:
            continue
        actions.append(
            DeskAction(
                kind="cash_handoff",
                severity="red",
                title=f"{row.person} — تسليم كاش {row.claimed_cash} د.ل ({row.domain_ar})",
                amount=row.claimed_cash + row.claimed_bank,
                href=(
                    "/pos/treasury/hotel-shifts"
                    if row.domain == "hotel"
                    else "/pos/treasury/pos-shifts"
                ),
                button="استلام وعدّ",
                domain=row.domain,
                shift_id=row.shift_id,
            )
        )
    for row in list_bank_receipt_rows(db, domain=key):
        actions.append(
            DeskAction(
                kind="bank_match",
                severity="yellow",
                title=f"{row.person} — تحويل مصرفي {row.claimed_bank} د.ل",
                amount=row.claimed_bank,
                href="/pos/treasury/banks",
                button="مطابقة",
                domain=row.domain,
                handover_id=row.handover_id,
            )
        )
    for adv in list_open_advances_needing_review(db):
        if adv.used <= 0:
            continue
        if key and (adv.domain or "") != key:
            continue
        actions.append(
            DeskAction(
                kind="custody_review",
                severity="orange",
                title=f"عهدة {adv.ref} — {adv.employee_name} مستخدم {adv.used} من {adv.amount}",
                amount=adv.remaining,
                href=f"/pos/treasury/custody/{adv.id}",
                button="مراجعة",
                domain=adv.domain,
                advance_id=adv.id,
            )
        )
    bals = load_treasury_balances(db)
    if bals.pending_variance_count:
        actions.append(
            DeskAction(
                kind="variance",
                severity="red",
                title=f"{bals.pending_variance_count} فروقات تحتاج معالجة — {bals.pending_variance_abs} د.ل",
                amount=bals.pending_variance_abs,
                href="/admin/shift-variances",
                button="مراجعة",
            )
        )
    return actions


def get_open_treasury_session(db: Session) -> TreasurySession | None:
    return db.scalar(
        select(TreasurySession)
        .where(TreasurySession.status == TreasurySessionStatus.OPEN)
        .order_by(TreasurySession.id.desc())
        .limit(1)
    )


def get_last_closed_treasury_session(db: Session) -> TreasurySession | None:
    return db.scalar(
        select(TreasurySession)
        .where(TreasurySession.status == TreasurySessionStatus.CLOSED)
        .order_by(TreasurySession.id.desc())
        .limit(1)
    )


def open_treasury_session(db: Session, *, user_id: int | None) -> TreasurySession:
    if get_open_treasury_session(db) is not None:
        raise TreasuryDeskError("يوجد يوم خزينة مفتوح بالفعل.")
    bals = load_treasury_balances(db)
    row = TreasurySession(
        status=TreasurySessionStatus.OPEN,
        opened_by_id=user_id,
        opening_cash=bals.cash_total,
        opening_bank=bals.bank_total,
    )
    db.add(row)
    db.flush()
    return row


def close_treasury_session(
    db: Session,
    *,
    user_id: int | None,
    counted_cash,
    counted_bank,
    note: str | None = None,
) -> TreasurySession:
    row = get_open_treasury_session(db)
    if row is None:
        raise TreasuryDeskError("لا يوجد يوم خزينة مفتوح لإقفاله.")
    bals = load_treasury_balances(db)
    mov = session_movements(db, row.opened_at)
    expected_cash = bals.cash_total
    expected_bank = bals.bank_total
    rec_c = money3(counted_cash)
    rec_b = money3(counted_bank)
    if rec_c < 0 or rec_b < 0:
        raise TreasuryDeskError("المبلغ المعدود غير صالح.")
    row.status = TreasurySessionStatus.CLOSED
    row.closed_at = datetime.now(timezone.utc)
    row.closed_by_id = user_id
    row.counted_cash = rec_c
    row.counted_bank = rec_b
    row.expected_cash = expected_cash
    row.expected_bank = expected_bank
    row.cash_in = mov.cash_in
    row.cash_out = mov.payments_out
    row.bank_in = mov.bank_in
    row.bank_out = Decimal("0")
    row.advances_out = mov.advances_out
    row.close_note = (note or "").strip() or None
    from modules.payments.shift_variance_models import (
        ShiftVarianceKind,
        ShiftVarianceSource,
    )
    from modules.payments.shift_variances import create_shift_variance

    create_shift_variance(
        db,
        source_type=ShiftVarianceSource.TREASURY_CLOSE,
        kind=ShiftVarianceKind.CASH,
        claimed_amount=expected_cash,
        received_amount=rec_c,
        note=f"إقفال خزينة #{row.id}",
        force_new=True,
    )
    create_shift_variance(
        db,
        source_type=ShiftVarianceSource.TREASURY_CLOSE,
        kind=ShiftVarianceKind.BANK,
        claimed_amount=expected_bank,
        received_amount=rec_b,
        note=f"إقفال خزينة #{row.id}",
        force_new=True,
    )
    db.flush()
    return row


def _next_advance_ref(db: Session) -> str:
    raw = db.scalar(select(func.max(PurchaseAdvance.id)))
    return f"PUR-ADV-{int(raw or 0) + 1:04d}"


def _custody_for_source(db: Session, source: PaymentMethod) -> PaymentMethod:
    from modules.gl.purchase_custody_wallets import ensure_purchase_custody_wallets

    ensure_purchase_custody_wallets(db)
    hotel = is_hotel_treasury_payment_method(source)
    if source.kind == PaymentMethodKind.BANK:
        name = (
            HOTEL_PURCHASE_CUSTODY_BANK_PM_NAME
            if hotel
            else RESTAURANT_PURCHASE_CUSTODY_BANK_PM_NAME
        )
    else:
        name = (
            HOTEL_PURCHASE_CUSTODY_CASH_PM_NAME
            if hotel
            else RESTAURANT_PURCHASE_CUSTODY_CASH_PM_NAME
        )
    pm = db.scalar(select(PaymentMethod).where(PaymentMethod.name_ar == name))
    if pm is None:
        raise TreasuryDeskError("حساب عهدة المشتريات غير مهيّأ.")
    return pm


def create_purchase_advance(
    db: Session,
    *,
    employee_id: int,
    amount,
    source_pm_id: int,
    purpose: str | None,
    user_id: int | None,
) -> PurchaseAdvance:
    amt = money3(amount)
    if amt <= 0:
        raise TreasuryDeskError("أدخل مبلغ العهدة.")
    if not employee_id:
        raise TreasuryDeskError("اختر الموظف المستلم.")
    source = db.get(PaymentMethod, int(source_pm_id))
    if source is None or not _is_treasury_pm(source):
        raise TreasuryDeskError("المصدر يجب أن يكون خزينة رئيسية (كاش أو مصرف).")
    bal = method_current_balance(db, source.id)
    if amt > bal:
        raise TreasuryDeskError(
            f"رصيد «{source.name_ar}» ({bal} د.ل) لا يكفي للعهدة ({amt} د.ل)."
        )
    custody = _custody_for_source(db, source)
    open_same = db.scalar(
        select(PurchaseAdvance).where(
            PurchaseAdvance.status == PurchaseAdvanceStatus.OPEN,
            PurchaseAdvance.custody_pm_id == custody.id,
        )
    )
    if open_same is not None:
        raise TreasuryDeskError(
            f"توجد عهدة مفتوحة على «{custody.name_ar}» ({open_same.ref}). "
            "سوّها أو أرجع المتبقي قبل إنشاء عهدة جديدة."
        )
    xfer = record_manual_transfer(
        db,
        from_payment_method_id=source.id,
        to_payment_method_id=custody.id,
        amount=amt,
        user_id=user_id,
        note=f"عهدة مشتريات — موظف #{employee_id}",
        transfer_type=PaymentTransferType.MANUAL,
        require_operation_ref=False,
    )
    domain = "hotel" if is_hotel_treasury_payment_method(source) else "restaurant"
    row = PurchaseAdvance(
        ref=_next_advance_ref(db),
        status=PurchaseAdvanceStatus.OPEN,
        employee_id=int(employee_id),
        amount=amt,
        source_pm_id=source.id,
        custody_pm_id=custody.id,
        purpose=(purpose or "").strip() or None,
        domain=domain,
        transfer_id=xfer.id if xfer is not None else None,
        created_by_id=user_id,
    )
    db.add(row)
    db.flush()
    if not row.ref:
        row.ref = f"PUR-ADV-{int(row.id):04d}"
    return row


def return_purchase_advance(
    db: Session,
    *,
    advance_id: int,
    amount,
    user_id: int | None,
) -> PurchaseAdvance:
    row = db.get(PurchaseAdvance, int(advance_id))
    if row is None or row.status != PurchaseAdvanceStatus.OPEN:
        raise TreasuryDeskError("العهدة غير موجودة أو مغلقة.")
    view = build_advance_view(db, row)
    amt = money3(amount)
    if amt <= 0:
        raise TreasuryDeskError("أدخل المبلغ المرتجع.")
    if amt - view.remaining > Decimal("0.001"):
        raise TreasuryDeskError(
            f"المرتجع أكبر من المتبقي لدى الموظف ({view.remaining} د.ل)."
        )
    xfer = record_manual_transfer(
        db,
        from_payment_method_id=row.custody_pm_id,
        to_payment_method_id=row.source_pm_id,
        amount=amt,
        user_id=user_id,
        note=f"إرجاع عهدة {row.ref}",
        transfer_type=PaymentTransferType.MANUAL,
        require_operation_ref=False,
    )
    row.returned_amount = (money3(row.returned_amount) + amt).quantize(Decimal("0.001"))
    row.return_transfer_id = xfer.id if xfer is not None else row.return_transfer_id
    db.flush()
    return row


def close_purchase_advance(
    db: Session, *, advance_id: int, user_id: int | None
) -> PurchaseAdvance:
    row = db.get(PurchaseAdvance, int(advance_id))
    if row is None or row.status != PurchaseAdvanceStatus.OPEN:
        raise TreasuryDeskError("العهدة غير موجودة أو مغلقة.")
    view = build_advance_view(db, row)
    balanced = (view.used + view.returned - view.amount).copy_abs() <= Decimal("0.001")
    if not balanced:
        raise TreasuryDeskError(
            "لا تُغلق العهدة إلا إذا: المشتريات المعتمدة + المصروفات + المرتجع = أصل العهدة. "
            f"الآن المستخدم {view.used} + المرتجع {view.returned} ≠ {view.amount}."
        )
    row.status = PurchaseAdvanceStatus.CLOSED
    row.closed_at = datetime.now(timezone.utc)
    row.closed_by_id = user_id
    db.flush()
    return row
