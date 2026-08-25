from __future__ import annotations

import re
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
    last_close_employee: str = ""
    last_close_kind_ar: str = ""


@dataclass
class LedgerEntry:
    at: datetime
    direction: str  # IN | OUT
    amount: Decimal
    category_ar: str
    reference: str
    method_name: str
    method_id: int
    room_label: str = ""
    employee_name: str = ""
    detail_url: str = ""
    notes: str = ""
    session_label: str = ""
    source_kind: str = ""
    source_id: int = 0


_HOTEL_SHIFT_NOTE_RE = re.compile(r"جلسة\s+فندق\s*#(\d+)")
_POS_REST_SHIFT_NOTE_RE = re.compile(r"جلسة\s+مطعم\s*#(\d+)")
_POS_SHIFT_NOTE_RE = re.compile(r"جلسة\s*#(\d+)")
_USER_STMT_RE = re.compile(r"بيان:\s*(.+)$")
_SALARY_CATEGORIES = frozenset(
    {"راتب موظف", "مكافأة موظف", "صرف موظف", "سلف موظفين"}
)


def _user_label(db: Session, user_id: int | None) -> str:
    if not user_id:
        return ""
    from modules.authz.models import User

    u = db.get(User, int(user_id))
    if u is None:
        return ""
    return (u.username or "").strip()


def _hr_employee_name(db: Session, employee_id: int | None) -> str:
    if not employee_id:
        return ""
    from modules.hr.models import Employee

    emp = db.get(Employee, int(employee_id))
    return ((emp.full_name_ar if emp else "") or "").strip()


def _employee_name_for_user_id(db: Session, user_id: int | None) -> str:
    if not user_id:
        return ""
    from modules.hr.models import Employee

    emp = db.scalar(select(Employee).where(Employee.user_id == int(user_id)))
    name = ((emp.full_name_ar if emp else "") or "").strip()
    return name or _user_label(db, user_id)


def _pos_shift_staff_name(db: Session, sh) -> str:
    name = _hr_employee_name(db, getattr(sh, "employee_id", None))
    if name:
        return name
    return _employee_name_for_user_id(db, getattr(sh, "user_id", None))


def _join_notes(*parts: str | None) -> str:
    seen: set[str] = set()
    out: list[str] = []
    for raw in parts:
        text = (raw or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return " — ".join(out)


def _fmt_close_dt(value: datetime | None) -> str:
    if value is None:
        return ""
    dt = value
    if dt.tzinfo is not None:
        dt = dt.astimezone()
    return dt.strftime("%Y-%m-%d %H:%M")


def _last_close_meta(db: Session, sh: PosShift | None) -> tuple[str, str]:
    if sh is None:
        return "", ""
    return _pos_shift_staff_name(db, sh), "كاشير"


def _user_statement_from_note(note: str | None) -> str:
    text = (note or "").strip()
    marked = _USER_STMT_RE.search(text)
    if marked:
        return (marked.group(1) or "").strip()
    return ""


def _handoff_ledger_bits(
    db: Session, tr: PaymentTransfer, *, incoming: bool
) -> tuple[str, str, str, str, str, str]:
    """تصنيف، مرجع، موظف، بيان، رابط، رقم الجلسة."""
    text = (tr.note or "").strip()
    hotel_m = _HOTEL_SHIFT_NOTE_RE.search(text)
    pos_m = _POS_REST_SHIFT_NOTE_RE.search(text) or _POS_SHIFT_NOTE_RE.search(text)
    stmt = _user_statement_from_note(text)
    if hotel_m:
        from modules.hotel.shift_models import HotelShift

        sid = int(hotel_m.group(1))
        sh = db.get(HotelShift, sid)
        emp = _pos_shift_staff_name(db, sh) if sh is not None else ""
        if not stmt and sh is not None:
            stmt = (getattr(sh, "closing_note", None) or "").strip()
        if not stmt:
            stmt = text
        cat = (
            "اعتماد جلسة استقبال — قبض"
            if incoming
            else "اعتماد جلسة استقبال — صرف"
        )
        return (
            cat,
            f"جلسة استقبال #{sid}",
            emp,
            stmt,
            f"/admin/hotel/shift/{sid}/report",
            f"استقبال #{sid}",
        )
    if pos_m:
        sid = int(pos_m.group(1))
        sh = db.get(PosShift, sid)
        emp = _pos_shift_staff_name(db, sh) if sh is not None else ""
        if not stmt and sh is not None:
            stmt = (getattr(sh, "closing_note", None) or "").strip()
        if not stmt:
            stmt = text
        cat = (
            "اعتماد جلسة كاشير — قبض"
            if incoming
            else "اعتماد جلسة كاشير — صرف"
        )
        return (
            cat,
            f"جلسة كاشير #{sid}",
            emp,
            stmt,
            f"/pos/shift/{sid}/report",
            f"كاشير #{sid}",
        )
    cat = _transfer_in_label(tr) if incoming else _transfer_out_label(tr)
    return cat, _transfer_reference(tr), _user_label(db, tr.created_by_id), stmt or text, "", ""


def _purchase_ledger_bits(purchase, pp, amount: Decimal) -> tuple[str, str, str, str]:
    """تصنيف، مرجع، مستلم/موظف، ملاحظات."""
    from modules.payments.models import PurchaseKind

    supplier = (getattr(purchase, "supplier", None) or "").strip()
    cat = (getattr(purchase, "expense_category", None) or "").strip()
    pnote = (getattr(purchase, "note", None) or "").strip()
    pp_note = (getattr(pp, "note", None) or "").strip()
    ref_extra = (getattr(purchase, "supplier_invoice_ref", None) or "").strip()
    kind = getattr(purchase, "kind", None)
    if cat in _SALARY_CATEGORIES:
        notes = _join_notes(
            f"الموظف: {supplier}" if supplier else "",
            f"المبلغ المصروف: {amount} د.ل",
            cat,
            pnote or pp_note,
        )
        return cat or "راتب موظف", f"سند #{purchase.id}", supplier, notes
    if kind == PurchaseKind.INVENTORY:
        notes = _join_notes(
            f"المورد: {supplier}" if supplier else "",
            f"سبب الصرف: سداد فاتورة شراء" + (f" ({ref_extra})" if ref_extra else ""),
            pnote or pp_note,
        )
        return "سداد مورد", f"فاتورة شراء #{purchase.id}", supplier, notes
    reason = cat or "صرف خارجي"
    notes = _join_notes(
        f"المستلم: {supplier}" if supplier else "",
        f"سبب الصرف: {reason}",
        pnote or pp_note,
    )
    return reason, f"سند صرف #{purchase.id}", supplier, notes


def _wallet_label(db: Session, pm, info: dict | None = None) -> str:
    from modules.gl.wallet_labels import label_from_info_map, wallet_gl_info_map

    mapping = info if info is not None else wallet_gl_info_map(db)
    return label_from_info_map(mapping, pm)


def _room_label(number: str | None, name_ar: str | None = None) -> str:
    num = (number or "").strip()
    if num:
        return f"#{num}"
    name = (name_ar or "").strip()
    return name or ""


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
    """رصيد الخزينة الرئيسية فقط (بعد اعتماد الجلسات) — لا يشمل محفظة الكاشير أثناء الوردية."""
    from modules.payments.shift_handoff_service import (
        ensure_main_treasury_payment_method,
        is_main_treasury_payment_method,
    )

    ensure_main_treasury_payment_method(db, kind)
    total = Decimal("0")
    for row in wallet_breakdown(db, None, None):
        if row.method.kind != kind:
            continue
        if not is_main_treasury_payment_method(row.method):
            continue
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
    emp, kind_ar = _last_close_meta(db, last_sh)
    return TreasuryKindSummary(
        kind=pm.kind,
        label_ar=_wallet_label(db, pm),
        current_balance=payment_method_balances_map(db)
        .get(pm.id, Decimal("0"))
        .quantize(Decimal("0.001")),
        last_close_balance=last_bal,
        last_close_shift_id=last_sh.id if last_sh else None,
        last_close_at=last_sh.closed_at if last_sh else None,
        last_close_employee=emp,
        last_close_kind_ar=kind_ar,
    )


def treasury_summary_for_kind(db: Session, kind: PaymentMethodKind) -> TreasuryKindSummary:
    last_sh = get_last_closed_shift(db)
    last_bal, _ = last_close_balance_for_kind(db, kind)
    label = (
        "الخزينة الرئيسية — كاش"
        if kind == PaymentMethodKind.CASH
        else "الخزينة الرئيسية — مصرف"
    )
    emp, kind_ar = _last_close_meta(db, last_sh)
    return TreasuryKindSummary(
        kind=kind,
        label_ar=label,
        current_balance=kind_current_balance(db, kind),
        last_close_balance=last_bal,
        last_close_shift_id=last_sh.id if last_sh else None,
        last_close_at=last_sh.closed_at if last_sh else None,
        last_close_employee=emp,
        last_close_kind_ar=kind_ar,
    )


def treasury_summaries(db: Session) -> dict[str, TreasuryKindSummary]:
    last_sh = get_last_closed_shift(db)
    out: dict[str, TreasuryKindSummary] = {}
    for kind, label in (
        (PaymentMethodKind.CASH, "الخزينة الرئيسية — كاش"),
        (PaymentMethodKind.BANK, "الخزينة الرئيسية — مصرف"),
    ):
        last_bal, _ = last_close_balance_for_kind(db, kind)
        emp, kind_ar = _last_close_meta(db, last_sh)
        out[kind.value] = TreasuryKindSummary(
            kind=kind,
            label_ar=label,
            current_balance=kind_current_balance(db, kind),
            last_close_balance=last_bal,
            last_close_shift_id=last_sh.id if last_sh else None,
            last_close_at=last_sh.closed_at if last_sh else None,
            last_close_employee=emp,
            last_close_kind_ar=kind_ar,
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
    """سجل حركات الخزينة لنوع كاش أو مصرف، أو لحساب واحد.

    العرض الإجمالي (بدون حساب محدد) = الخزينة الرئيسية فقط بعد اعتماد الجلسات،
    وليس فواتير الكاشير أثناء الوردية.
    """
    from modules.payments.shift_handoff_service import (
        ensure_main_treasury_payment_method,
        is_main_treasury_payment_method,
    )

    if payment_method_id is not None:
        method_ids = {payment_method_id}
    else:
        ensure_main_treasury_payment_method(db, kind)
        method_ids = {
            m.id
            for m in list_payment_methods(db, only_active=False)
            if m.kind == kind and is_main_treasury_payment_method(m)
        }
    if not method_ids:
        return []
    methods = {m.id: m for m in list_payment_methods(db, only_active=False) if m.id in method_ids}
    from modules.gl.wallet_labels import wallet_gl_info_map

    gl_info = wallet_gl_info_map(db)
    entries: list[LedgerEntry] = []

    from modules.hotel.booking_models import HotelBooking
    from modules.hotel.models import HotelRoom
    from modules.sales.models import Sale

    sp_rows = db.execute(
        select(
            SalePayment,
            PaymentMethod,
            Sale.booking_id,
            Sale.pos_shift_id,
            HotelRoom.number,
            HotelRoom.name_ar,
        )
        .join(PaymentMethod, PaymentMethod.id == SalePayment.payment_method_id)
        .outerjoin(Sale, Sale.id == SalePayment.sale_id)
        .outerjoin(HotelBooking, HotelBooking.id == Sale.booking_id)
        .outerjoin(HotelRoom, HotelRoom.id == HotelBooking.room_id)
        .where(PaymentMethod.id.in_(method_ids))
    ).all()
    for sp, pm, booking_id, pos_shift_id, room_num, room_name in sp_rows:
        amt = Decimal(str(sp.amount or 0)).quantize(Decimal("0.001"))
        if amt <= 0:
            continue
        sess = f"كاشير #{pos_shift_id}" if pos_shift_id else ""
        entries.append(
            LedgerEntry(
                at=sp.created_at or datetime.now(timezone.utc),
                direction="IN",
                amount=amt,
                category_ar="تحصيل بيع",
                reference=f"فاتورة #{sp.sale_id}",
                method_name=_wallet_label(db, pm, gl_info),
                method_id=pm.id,
                room_label=_room_label(room_num, room_name),
                detail_url=f"/pos/receipt/{sp.sale_id}",
                notes=f"تحصيل فاتورة #{sp.sale_id}",
                session_label=sess,
                source_kind="sale",
                source_id=int(sp.id),
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
                method_name=_wallet_label(db, pm, gl_info),
                method_id=pm.id,
                employee_name=_user_label(db, rp.created_by_id),
                detail_url=f"/refunds/receipt/{rp.sale_return_id}",
                notes=(rp.note or "").strip() or f"مرتجع #{rp.sale_return_id}",
                source_kind="refund",
                source_id=int(rp.id),
            )
        )

    from modules.payments.models import Purchase, PurchasePayment

    pur_rows = db.execute(
        select(PurchasePayment, PaymentMethod, Purchase)
        .join(PaymentMethod, PaymentMethod.id == PurchasePayment.payment_method_id)
        .join(Purchase, Purchase.id == PurchasePayment.purchase_id)
        .where(PaymentMethod.id.in_(method_ids))
    ).all()
    for pp, pm, purchase in pur_rows:
        amt = Decimal(str(pp.amount or 0)).quantize(Decimal("0.001"))
        if amt <= 0:
            continue
        cat, ref, party, notes = _purchase_ledger_bits(purchase, pp, amt)
        entries.append(
            LedgerEntry(
                at=pp.created_at or datetime.now(timezone.utc),
                direction="OUT",
                amount=amt,
                category_ar=cat,
                reference=ref,
                method_name=_wallet_label(db, pm, gl_info),
                method_id=pm.id,
                employee_name=party or _user_label(db, pp.created_by_id),
                detail_url=f"/admin/purchases/{pp.purchase_id}",
                notes=notes,
                source_kind="purchase",
                source_id=int(pp.id),
            )
        )

    from modules.hotel.booking_models import (
        HotelBookingPayment,
        HotelBookingPaymentRefund,
    )

    hp_rows = db.execute(
        select(
            HotelBookingPayment,
            PaymentMethod,
            HotelBooking.reference,
            HotelBooking.id,
            HotelRoom.number,
            HotelRoom.name_ar,
        )
        .join(PaymentMethod, PaymentMethod.id == HotelBookingPayment.payment_method_id)
        .join(HotelBooking, HotelBooking.id == HotelBookingPayment.booking_id)
        .outerjoin(HotelRoom, HotelRoom.id == HotelBooking.room_id)
        .where(HotelBookingPayment.payment_method_id.in_(method_ids))
    ).all()
    for hp, pm, booking_ref, booking_id, room_num, room_name in hp_rows:
        amt = Decimal(str(hp.amount or 0)).quantize(Decimal("0.001"))
        if amt <= 0:
            continue
        ref = (booking_ref or "").strip() or f"#{booking_id}"
        cat = "عربون حجز" if hp.is_deposit else "تحصيل حجز"
        entries.append(
            LedgerEntry(
                at=hp.created_at or datetime.now(timezone.utc),
                direction="IN",
                amount=amt,
                category_ar=cat,
                reference=ref,
                method_name=_wallet_label(db, pm, gl_info),
                method_id=pm.id,
                room_label=_room_label(room_num, room_name),
                employee_name=_user_label(db, hp.received_by_id),
                detail_url=f"/admin/hotel/bookings/{booking_id}",
                notes=cat + (f" {ref}" if ref else ""),
                source_kind="hotel_pay",
                source_id=int(hp.id),
            )
        )

    from sqlalchemy import func as sa_func

    refund_pm_id = sa_func.coalesce(
        HotelBookingPaymentRefund.payment_method_id,
        HotelBookingPayment.payment_method_id,
    )
    hr_rows = db.execute(
        select(
            HotelBookingPaymentRefund,
            PaymentMethod,
            HotelBooking.reference,
            HotelBooking.id,
            HotelRoom.number,
            HotelRoom.name_ar,
        )
        .join(
            HotelBookingPayment,
            HotelBookingPayment.id == HotelBookingPaymentRefund.payment_id,
        )
        .join(PaymentMethod, PaymentMethod.id == refund_pm_id)
        .join(HotelBooking, HotelBooking.id == HotelBookingPayment.booking_id)
        .outerjoin(HotelRoom, HotelRoom.id == HotelBooking.room_id)
        .where(refund_pm_id.in_(method_ids))
    ).all()
    for hr, pm, booking_ref, booking_id, room_num, room_name in hr_rows:
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
                reference=ref,
                method_name=_wallet_label(db, pm, gl_info),
                method_id=pm.id,
                room_label=_room_label(room_num, room_name),
                employee_name=_user_label(db, hr.approved_by_id),
                detail_url=f"/admin/hotel/bookings/{booking_id}",
                notes="مرتجع حجز " + (ref or ""),
                source_kind="hotel_refund",
                source_id=int(hr.id),
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
        sess = ""
        if tr.transfer_type == PaymentTransferType.SHIFT_HANDOFF:
            cat, ref, emp, notes, href, sess = _handoff_ledger_bits(db, tr, incoming=True)
        else:
            cat = _transfer_in_label(tr)
            ref = _transfer_reference(tr)
            emp = _employee_name_for_user_id(db, tr.created_by_id)
            notes = _user_statement_from_note(tr.note) or (tr.note or "").strip()
            href = f"/reports/transactions/transfer/{tr.id}"
        entries.append(
            LedgerEntry(
                at=tr.created_at or datetime.now(timezone.utc),
                direction="IN",
                amount=amt,
                category_ar=cat,
                reference=ref,
                method_name=_wallet_label(db, pm, gl_info),
                method_id=pm.id,
                employee_name=emp,
                detail_url=href,
                notes=notes,
                session_label=sess,
                source_kind="transfer",
                source_id=int(tr.id),
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
        sess = ""
        if tr.transfer_type == PaymentTransferType.SHIFT_HANDOFF:
            cat, ref, emp, notes, href, sess = _handoff_ledger_bits(db, tr, incoming=False)
        else:
            cat = _transfer_out_label(tr)
            ref = _transfer_reference(tr)
            emp = _employee_name_for_user_id(db, tr.created_by_id)
            notes = _user_statement_from_note(tr.note) or (tr.note or "").strip()
            href = f"/reports/transactions/transfer/{tr.id}"
        entries.append(
            LedgerEntry(
                at=tr.created_at or datetime.now(timezone.utc),
                direction="OUT",
                amount=amt,
                category_ar=cat,
                reference=ref,
                method_name=_wallet_label(db, pm, gl_info),
                method_id=pm.id,
                employee_name=emp,
                detail_url=href,
                notes=notes,
                session_label=sess,
                source_kind="transfer",
                source_id=int(tr.id),
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
                    method_name=_wallet_label(db, pm, gl_info),
                    method_id=pm.id,
                    notes=f"أجرة توصيل فاتورة #{d.sale_id}",
                    source_kind="delivery",
                    source_id=int(d.id),
                )
            )

    _epoch = datetime.min.replace(tzinfo=timezone.utc)
    # الأحدث أولاً للعرض في واجهة الخزينة
    entries.sort(key=lambda e: (e.at or _epoch, e.reference), reverse=True)

    if day is not None:
        entries = [e for e in entries if e.at and e.at.date() == day]

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
    _epoch = datetime.min.replace(tzinfo=timezone.utc)
    all_entries = list(entries) if entries is not None else list_ledger_entries(db, kind)
    # الحساب اليومي يحتاج ترتيباً تصاعدياً زمنياً
    all_entries.sort(key=lambda e: (e.at or _epoch, e.reference))
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
    # العرض: الأحدث أعلى — الحساب يبقى تصاعدياً لصحة الرصيد
    rows.reverse()
    return rows


def update_ledger_statement(
    db: Session,
    *,
    source_kind: str,
    source_id: int,
    statement: str,
) -> None:
    """يضيف/يعدّل بيان الحركة — للأدمن عند غياب ملاحظة الموظف."""
    from modules.payments.service import PaymentsError

    text = (statement or "").strip()
    text = re.sub(r"^رقم العملية:\s*[^—]+(?:\s*—\s*)?", "", text).strip()
    text = re.sub(r"^بيان:\s*", "", text).strip()
    if len(text) < 2:
        raise PaymentsError("أدخل بيان المعاملة (وصف واضح).")
    if len(text) > 400:
        raise PaymentsError("البيان طويل جداً.")
    kind = (source_kind or "").strip()
    sid = int(source_id)
    if kind == "transfer":
        row = db.get(PaymentTransfer, sid)
        if row is None:
            raise PaymentsError("حركة التحويل غير موجودة.")
        existing = row.note or ""
        hotel_m = _HOTEL_SHIFT_NOTE_RE.search(existing)
        pos_m = _POS_REST_SHIFT_NOTE_RE.search(existing) or _POS_SHIFT_NOTE_RE.search(
            existing
        )
        if hotel_m or pos_m:
            base = _USER_STMT_RE.sub("", existing).strip(" —")
            row.note = f"{base} — بيان: {text}".strip(" —") if base else f"بيان: {text}"
            if hotel_m:
                from modules.hotel.shift_models import HotelShift

                sh = db.get(HotelShift, int(hotel_m.group(1)))
                if sh is not None:
                    sh.closing_note = text
            elif pos_m:
                sh = db.get(PosShift, int(pos_m.group(1)))
                if sh is not None:
                    sh.closing_note = text
        else:
            from modules.payments.service import compose_transfer_note_with_bank_ref

            ref_m = re.search(r"رقم العملية:\s*([^—]+)", existing)
            ref = ref_m.group(1).strip() if ref_m else ""
            row.note = compose_transfer_note_with_bank_ref(text, ref)
        from modules.gl.models import GlJournalEntry

        for je in db.scalars(
            select(GlJournalEntry).where(
                GlJournalEntry.source_type == "payment_transfer",
                GlJournalEntry.source_id == sid,
            )
        ).all():
            je.description_ar = (row.note or text)[:255]
        db.flush()
        return
    if kind == "refund":
        row = db.get(RefundPayment, sid)
        if row is None:
            raise PaymentsError("حركة المرتجع غير موجودة.")
        row.note = text
        db.flush()
        return
    if kind == "purchase":
        from modules.payments.models import PurchasePayment

        pp = db.get(PurchasePayment, sid)
        if pp is None:
            raise PaymentsError("حركة الصرف غير موجودة.")
        pp.note = text
        purchase = db.get(Purchase, int(pp.purchase_id))
        if purchase is not None:
            purchase.note = text
        db.flush()
        return
    raise PaymentsError("لا يمكن تعديل بيان هذا النوع من الحركات من هنا.")
