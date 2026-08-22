"""كشف حساب الشقة (Folio) — إقامة + POS + خدمات."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.hotel.booking_models import BookingStatus, HotelBooking, HotelBookingService
from modules.hotel.models import RoomCharge
from modules.refunds.service import sale_outstanding_total
from modules.sales.models import Sale, SaleContext, SaleStatus

LAUNDRY_KEYWORDS = ("غسيل", "مغسلة", "laundry", "wash", "غسل")
_RESTAURANT_KEYWORDS = ("مطعم", "مقهى", "طلب #", "فاتورة #", "roof", "cafe", "pos")
_SALE_NUM_RE = re.compile(r"(?:طلب|فاتورة|invoice|#)\s*#?\s*(\d+)", re.IGNORECASE)


@dataclass
class BookingCashMovement:
    """حركة على حساب الحجز (مستحق / مدفوع / تخفيض / إرجاع / محفظة)."""

    kind: str  # payment | refund | charge | charge_reduction | wallet | prepaid
    created_at: datetime | None
    amount: Decimal
    label: str
    note: str | None = None
    is_deposit: bool = False
    payment_id: int | None = None
    refund_id: int | None = None
    # عليه (+) يزيد ما على النزيل · له (−) يزيد ما له / يقلل المستحق
    debit: Decimal = Decimal("0")
    credit: Decimal = Decimal("0")
    running_balance: Decimal = Decimal("0")


_KIND_SORT = {
    "charge": 0,
    "payment": 1,
    "charge_reduction": 2,
    "refund": 3,
    "prepaid": 4,
    "wallet": 5,
}


def _movement_sort_key(m: BookingCashMovement):
    ts = m.created_at
    kind_ord = _KIND_SORT.get(m.kind, 9)
    if ts is None:
        return (1, 0.0, kind_ord)
    try:
        stamp = float(ts.timestamp())
    except Exception:
        stamp = 0.0
    return (0, stamp, kind_ord)


def _parse_decimal(raw: str | None) -> Decimal | None:
    if raw is None:
        return None
    text = str(raw).strip().replace(",", "")
    if not text:
        return None
    try:
        return Decimal(text).quantize(Decimal("0.001"))
    except Exception:
        return None


def build_payment_ledger(
    booking: HotelBooking,
) -> tuple[list[BookingCashMovement], Decimal, Decimal]:
    """سجل المدفوعات والإرجاعات النقدية فقط (للتوافق مع الشاشات القديمة)."""
    movements: list[BookingCashMovement] = []
    gross = Decimal("0")
    refunded = Decimal("0")
    payments = list(getattr(booking, "payments", None) or [])
    for pay in payments:
        amt = Decimal(str(pay.amount or 0)).quantize(Decimal("0.001"))
        gross += amt
        label = "عربون" if pay.is_deposit else "دفعة"
        movements.append(
            BookingCashMovement(
                kind="payment",
                created_at=pay.created_at,
                amount=amt,
                label=label,
                note=(pay.note or None),
                is_deposit=bool(pay.is_deposit),
                payment_id=int(pay.id) if getattr(pay, "id", None) else None,
                credit=amt,
            )
        )
        try:
            refund_rows = list(getattr(pay, "refunds", None) or [])
        except Exception:
            # جدول hotel_booking_payment_refunds قد يكون غير منشأ بعد على السيرفر
            refund_rows = []
        for ref in refund_rows:
            ramt = Decimal(str(ref.amount or 0)).quantize(Decimal("0.001"))
            refunded += ramt
            reason = (ref.reason or "").strip() or None
            movements.append(
                BookingCashMovement(
                    kind="refund",
                    created_at=ref.created_at,
                    amount=ramt,
                    label="إرجاع نقدي للنزيل",
                    note=reason,
                    payment_id=int(pay.id) if getattr(pay, "id", None) else None,
                    refund_id=int(ref.id) if getattr(ref, "id", None) else None,
                    debit=ramt,
                )
            )

    movements.sort(key=_movement_sort_key)
    running = Decimal("0")
    for m in movements:
        running = (running + m.debit - m.credit).quantize(Decimal("0.001"))
        m.running_balance = running
    return movements, gross.quantize(Decimal("0.001")), refunded.quantize(Decimal("0.001"))


def build_account_ledger(
    db: Session,
    booking: HotelBooking,
) -> tuple[list[BookingCashMovement], Decimal, Decimal]:
    """كشف حركة الحساب بالترتيب: مستحقات، مدفوعات، تخفيضات، إرجاعات، محفظة.

    الرصيد الجاري = ما على النزيل (موجب) أو ما له (سالب).
    """
    from modules.hotel.booking_models import HotelAuditLog

    movements: list[BookingCashMovement] = []
    gross = Decimal("0")
    refunded = Decimal("0")
    zero = Decimal("0")

    # --- مستحقات الإقامة (قبل التخفيض إن وُجدت مغادرة مبكرة) ---
    acc_actual = (
        Decimal(str(booking.accommodation_total or 0))
        - Decimal(str(booking.discount_amount or 0))
    ).quantize(Decimal("0.001"))
    acc_booked = acc_actual
    reduction_amt = zero
    reduction_at = None
    reduction_note = None

    try:
        audits = list(
            db.scalars(
                select(HotelAuditLog)
                .where(
                    HotelAuditLog.entity_type == "booking",
                    HotelAuditLog.entity_id == int(booking.id),
                    HotelAuditLog.action.in_(
                        (
                            "charge_reduction",
                            "early_departure",
                            "credit_to_wallet",
                            "prepaid_credit_applied",
                            "wallet_credit_applied",
                            "wallet_credit_reversed",
                        )
                    ),
                )
                .order_by(HotelAuditLog.id.asc())
            ).all()
        )
    except Exception:
        audits = []

    for row in audits:
        if row.action == "charge_reduction":
            amt = _parse_decimal(row.new_value)
            if amt and amt > 0:
                reduction_amt = amt
                reduction_at = row.created_at
                reduction_note = (row.reason or "").strip() or "تخفيض مستحقات (مغادرة مبكرة)"
                old_acc = _parse_decimal(row.old_value)
                if old_acc is not None and old_acc > acc_actual:
                    acc_booked = (old_acc - Decimal(str(booking.discount_amount or 0))).quantize(
                        Decimal("0.001")
                    )
        elif row.action == "early_departure" and reduction_amt <= 0:
            reduction_at = reduction_at or row.created_at

    # إن لم يُحفظ تخفيض في التدقيق — أعد حساب فرق الإقامة من الموعد المجدول
    if reduction_amt <= 0 and getattr(booking, "scheduled_check_out", None):
        planned = booking.planned_check_out
        if planned and booking.check_out and planned > booking.check_out:
            try:
                from modules.hotel.booking_service import _recalc_booking_accommodation

                booked_raw = _recalc_booking_accommodation(
                    db, booking, through_date=planned
                )
                disc = Decimal(str(booking.discount_amount or 0))
                acc_booked = (Decimal(str(booked_raw or 0)) - disc).quantize(Decimal("0.001"))
                if acc_booked > acc_actual + Decimal("0.0005"):
                    reduction_amt = (acc_booked - acc_actual).quantize(Decimal("0.001"))
                    reduction_note = "تخفيض ليالي ملغاة (مغادرة مبكرة)"
                    if reduction_at is None:
                        reduction_at = getattr(booking, "checked_out_at", None) or booking.created_at
            except Exception:
                pass

    stay_charge = acc_booked if reduction_amt > 0 else acc_actual
    if stay_charge > 0:
        nights = int(booking.nights or 0)
        if nights <= 0 and stay_charge > 0:
            nights = 1
        if reduction_amt > 0 and getattr(booking, "scheduled_check_out", None):
            try:
                nights = max(0, (booking.planned_check_out - booking.check_in).days) or nights
            except Exception:
                pass
        movements.append(
            BookingCashMovement(
                kind="charge",
                created_at=booking.created_at or getattr(booking, "checked_in_at", None),
                amount=stay_charge,
                label=f"مستحق إقامة ({nights} ليلة)",
                note=f"سعر الليلة {booking.nightly_rate}" if booking.nightly_rate else None,
                debit=stay_charge,
            )
        )

    # خدمات على النزيل
    for svc in _list_booking_services(db, int(booking.id)):
        if _svc_charged_to_guest(svc) is False:
            continue
        amt = Decimal(str(svc.line_total or 0)).quantize(Decimal("0.001"))
        if amt <= 0:
            continue
        try:
            svc_code = getattr(svc, "service_code", None)
        except Exception:  # noqa: BLE001
            svc_code = None
        if is_violation_service(svc.name_ar, svc_code if isinstance(svc_code, str) else None):
            svc_label = f"مستحق مخالفة: {svc.name_ar}"
        else:
            svc_label = f"مستحق خدمة: {svc.name_ar}"
        movements.append(
            BookingCashMovement(
                kind="charge",
                created_at=svc.created_at,
                amount=amt,
                label=svc_label,
                debit=amt,
            )
        )

    # رسوم مطعم/غرفة غير المسوّاة (نفس منطق الفوليو — يشمل فواتير الغرفة أثناء الإقامة)
    from modules.hotel.breakfast_settle import sale_is_hotel_breakfast

    covered_sale_ids: set[int] = _service_sale_ids(db, int(booking.id))
    for rc in list_open_room_charges_for_booking(db, int(booking.id), auto_link=True):
        sid = int(rc.sale_id)
        if sid in covered_sale_ids:
            continue
        if sale_is_hotel_breakfast(db, rc.sale):
            covered_sale_ids.add(sid)
            continue
        due = sale_outstanding_total(db, rc.sale_id)
        if due <= 0:
            covered_sale_ids.add(sid)
            continue
        covered_sale_ids.add(sid)
        movements.append(
            BookingCashMovement(
                kind="charge",
                created_at=rc.created_at,
                amount=due,
                label=f"مستحق طلبات غرفة #{rc.sale_id}",
                debit=due.quantize(Decimal("0.001")),
            )
        )

    # فواتير مربوطة بالحجز بلا قيد غرفة مفتوح (نفس دين كشف الحساب)
    orphan_sales: list[Sale] = []
    try:
        orphan_sales = list(
            db.scalars(
                select(Sale).where(
                    Sale.booking_id == int(booking.id),
                    Sale.status == SaleStatus.COMPLETED,
                )
            ).all()
        )
    except Exception:  # noqa: BLE001
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        orphan_sales = []
    for sale in orphan_sales:
        sid = int(sale.id)
        if sid in covered_sale_ids:
            continue
        if sale_is_hotel_breakfast(db, sale):
            continue
        due = sale_outstanding_total(db, sid)
        if due <= 0:
            continue
        covered_sale_ids.add(sid)
        movements.append(
            BookingCashMovement(
                kind="charge",
                created_at=getattr(sale, "created_at", None),
                amount=due,
                label=f"مستحق طلبات غرفة #{sid}",
                debit=due.quantize(Decimal("0.001")),
            )
        )

    if reduction_amt > 0:
        movements.append(
            BookingCashMovement(
                kind="charge_reduction",
                created_at=reduction_at or booking.checked_out_at or booking.created_at,
                amount=reduction_amt,
                label="خصم من المستحق (ليالي ملغاة)",
                note=reduction_note,
                credit=reduction_amt,
            )
        )

    # مدفوعات وإرجاعات نقدية
    for pay in list(getattr(booking, "payments", None) or []):
        amt = Decimal(str(pay.amount or 0)).quantize(Decimal("0.001"))
        if amt <= 0:
            continue
        gross += amt
        movements.append(
            BookingCashMovement(
                kind="payment",
                created_at=pay.created_at,
                amount=amt,
                label="عربون مدفوع" if pay.is_deposit else "مبلغ مدفوع",
                note=(pay.note or None),
                is_deposit=bool(pay.is_deposit),
                payment_id=int(pay.id) if getattr(pay, "id", None) else None,
                credit=amt,
            )
        )
        try:
            refund_rows = list(getattr(pay, "refunds", None) or [])
        except Exception:
            refund_rows = []
        for ref in refund_rows:
            ramt = Decimal(str(ref.amount or 0)).quantize(Decimal("0.001"))
            if ramt <= 0:
                continue
            refunded += ramt
            movements.append(
                BookingCashMovement(
                    kind="refund",
                    created_at=ref.created_at,
                    amount=ramt,
                    label="إرجاع نقدي للنزيل (خصم من رصيد المدفوع)",
                    note=(ref.reason or "").strip() or None,
                    payment_id=int(pay.id) if getattr(pay, "id", None) else None,
                    refund_id=int(ref.id) if getattr(ref, "id", None) else None,
                    debit=ramt,
                )
            )

    # ترحيل للمحفظة / استخدام رصيد مسبق من التدقيق
    # لا نعرض خصم محفظة أُلغي لاحقاً (كان يظهر الوجبة كـ «له» بالخطأ)
    reversed_wallet_left = Decimal("0")
    for row in audits:
        if row.action == "wallet_credit_reversed":
            ramt = _parse_decimal(row.new_value)
            if ramt and ramt > 0:
                reversed_wallet_left += ramt

    for row in audits:
        amt = _parse_decimal(row.new_value)
        if not amt or amt <= 0:
            continue
        if row.action == "credit_to_wallet":
            movements.append(
                BookingCashMovement(
                    kind="wallet",
                    created_at=row.created_at,
                    amount=amt,
                    label="ترحيل رصيد إلى محفظة العميل (خصم من رصيد الحجز)",
                    note=(row.reason or "").strip() or None,
                    debit=amt,
                )
            )
        elif row.action == "wallet_credit_applied":
            reason = (row.reason or "").strip()
            is_auto_cover = "خصم تلقائي من المحفظة لتغطية متبقي الحجز" in reason
            if is_auto_cover and reversed_wallet_left > 0:
                skip = min(amt, reversed_wallet_left)
                reversed_wallet_left = (reversed_wallet_left - skip).quantize(
                    Decimal("0.001")
                )
                amt = (amt - skip).quantize(Decimal("0.001"))
                if amt <= 0:
                    continue
            movements.append(
                BookingCashMovement(
                    kind="prepaid",
                    created_at=row.created_at,
                    amount=amt,
                    label="خصم من محفظة العميل على الحجز",
                    note=(row.reason or "").strip() or None,
                    credit=amt,
                )
            )
        elif row.action == "prepaid_credit_applied":
            movements.append(
                BookingCashMovement(
                    kind="prepaid",
                    created_at=row.created_at,
                    amount=amt,
                    label="استخدام رصيد الحجز لتسوية فاتورة",
                    note=(row.reason or "").strip() or None,
                    debit=amt,
                )
            )
        elif row.action == "wallet_credit_reversed":
            # أُسقطت مع خصم المحفظة الملغى أعلاه — لا سطر منفصل
            continue

    movements.sort(key=_movement_sort_key)
    running = Decimal("0")
    for m in movements:
        running = (running + m.debit - m.credit).quantize(Decimal("0.001"))
        m.running_balance = running

    return movements, gross.quantize(Decimal("0.001")), refunded.quantize(Decimal("0.001"))


def is_laundry_service(name: str | None) -> bool:
    text = (name or "").strip().lower()
    return any(k in text for k in LAUNDRY_KEYWORDS)


def is_violation_service(name: str | None, service_code: str | None = None) -> bool:
    """مخالفة / تلف / فقدان مواد — تُعرض عند لوحة المغادرة."""
    code = (service_code or "").strip().upper()
    if code in ("VIOLATION", "DAMAGE"):
        return True
    text = (name or "").strip().lower()
    if not text:
        return False
    keys = ("مخالفة", "اتلاف", "إتلاف", "تلف", "تالف", "فقدان", "damage", "violation")
    return any(k in text for k in keys)


def classify_booking_services_for_checkout(
    services: list | None,
) -> dict[str, list]:
    """تقسيم خدمات الحجز: مغسلة / مخالفات / أخرى — لوحة موظف الاستقبال عند المغادرة."""
    laundry: list = []
    violations: list = []
    other: list = []
    for svc in services or []:
        name = getattr(svc, "name_ar", None)
        try:
            code = getattr(svc, "service_code", None)
        except Exception:  # noqa: BLE001
            code = None
        code_s = code if isinstance(code, str) else None
        if is_violation_service(name, code_s):
            violations.append(svc)
        elif is_laundry_service(name) or (
            code_s and code_s.strip().upper() == "LAUNDRY"
        ):
            laundry.append(svc)
        else:
            other.append(svc)
    return {
        "laundry": laundry,
        "violations": violations,
        "other": other,
    }


def _pos_charge_source_label(db: Session, sale: Sale) -> str:
    """مصدر الطلب للفاتورة — مطعم روف، مقهى، …"""
    from modules.sales.receipt_layout import build_receipt_sections
    from modules.settings.service import get_setting

    sections = build_receipt_sections(db, sale)
    roots = [
        s.root_title
        for s in sections
        if s.root_title and s.root_title not in ("غير مصنّف", "بدون فئة")
    ]
    if roots:
        unique = list(dict.fromkeys(roots))
        return unique[0] if len(unique) == 1 else " و ".join(unique[:2])

    store = (get_setting(db, "store_name", "") or "").strip()
    if store:
        return store

    if sale.context_type == SaleContext.ROOM:
        return "المطعم"
    if sale.context_type == SaleContext.TABLE:
        from modules.sales.service import sale_context_label_ar

        return sale_context_label_ar(sale, detailed=True)
    if sale.context_type == SaleContext.EXTERNAL:
        from modules.sales.service import sale_context_label_ar

        return sale_context_label_ar(sale, detailed=True)
    return "خدمة"


def _pos_charge_items_preview(sale: Sale, *, max_names: int = 2) -> str:
    names: list[str] = []
    for ln in sale.lines or []:
        prod = ln.product
        if prod and prod.name_ar:
            names.append(f"{prod.name_ar} ×{ln.quantity}")
    if not names:
        return ""
    head = "، ".join(names[:max_names])
    if len(names) > max_names:
        head += f" (+{len(names) - max_names})"
    return head


def folio_pos_charge_description(
    db: Session,
    sale: Sale | None,
    sale_id: int,
    due: Decimal,
    *,
    include_item_details: bool = False,
) -> str:
    """وصف بند POS على فاتورة الحجز — رقم الفاتورة فقط (بدون أصناف)."""
    _ = (db, sale, due, include_item_details)
    return f"فاتورة مطعم #{int(sale_id)}"


@dataclass
class FolioLine:
    kind: str
    description: str
    amount: Decimal
    ref_id: int | None = None
    #: لربط إعادة طباعة فاتورة المطعم/المغسلة من فاتورة الحجز
    sale_id: int | None = None
    folio_side: str = "GUEST"  # COMPANY | GUEST | SHARED


@dataclass
class SideFolio:
    """حساب فرعي: شركة أو نزيل."""

    side: str  # COMPANY | GUEST
    label: str
    total: Decimal
    lines: list[FolioLine]


@dataclass
class FolioSummary:
    accommodation: Decimal
    pos_charges: Decimal
    services: Decimal
    discount: Decimal
    paid: Decimal
    total: Decimal
    balance: Decimal
    lines: list[FolioLine]
    company_folio: SideFolio | None = None
    guest_folio: SideFolio | None = None
    company_total: Decimal = Decimal("0")
    guest_total: Decimal = Decimal("0")


@dataclass
class GuestAccountSummary:
    """حساب الزبون — ما عليه وما له."""

    total_charges: Decimal
    total_paid: Decimal
    deposit: Decimal
    amount_due: Decimal
    amount_credit: Decimal
    balance: Decimal


@dataclass
class PartyAccount:
    """رصيد جهة واحدة: شركة أو نزيل."""

    side: str  # COMPANY | GUEST
    label: str
    charges: Decimal
    paid: Decimal
    due: Decimal
    credit: Decimal
    balance: Decimal


@dataclass
class BookingPartyAccounts:
    company: PartyAccount
    guest: PartyAccount
    has_company: bool


def allocate_paid_to_sides(
    company_total: Decimal,
    guest_total: Decimal,
    paid: Decimal,
) -> tuple[Decimal, Decimal]:
    """يوزّع المدفوع: شركة أولاً ثم نزيل. أي فائض يبقى على حساب النزيل."""
    ct = Decimal(str(company_total or 0)).quantize(Decimal("0.001"))
    p = Decimal(str(paid or 0)).quantize(Decimal("0.001"))
    company_paid = min(p, ct)
    guest_paid = (p - company_paid).quantize(Decimal("0.001"))
    return company_paid, guest_paid


def _party_from_totals(side: str, label: str, charges: Decimal, paid: Decimal) -> PartyAccount:
    ch = Decimal(str(charges or 0)).quantize(Decimal("0.001"))
    pd = Decimal(str(paid or 0)).quantize(Decimal("0.001"))
    bal = (ch - pd).quantize(Decimal("0.001"))
    return PartyAccount(
        side=side,
        label=label,
        charges=ch,
        paid=pd,
        due=max(Decimal("0"), bal).quantize(Decimal("0.001")),
        credit=max(Decimal("0"), -bal).quantize(Decimal("0.001")),
        balance=bal,
    )


def build_party_accounts(db: Session, booking_id: int) -> BookingPartyAccounts:
    """رصيد الشركة ورصيد النزيل — بدون وعاء «محفظة حجز» منفصل."""
    folio = build_folio(db, booking_id)
    company_paid, guest_paid = allocate_paid_to_sides(
        folio.company_total, folio.guest_total, folio.paid
    )
    company = _party_from_totals("COMPANY", "حساب الشركة", folio.company_total, company_paid)
    guest = _party_from_totals("GUEST", "حساب النزيل", folio.guest_total, guest_paid)
    return BookingPartyAccounts(
        company=company,
        guest=guest,
        has_company=folio.company_total > Decimal("0.0005"),
    )


def _svc_charged_to_guest(svc: HotelBookingService) -> bool:
    try:
        return bool(svc.charged_to_guest)
    except Exception:  # noqa: BLE001
        return True


def _list_booking_services(db: Session, booking_id: int) -> list[HotelBookingService]:
    """خدمات الحجز — ترجع فارغة إن نقصت أعمدة مُرقَّعة حديثاً على السيرفر."""
    try:
        return list(
            db.scalars(
                select(HotelBookingService).where(
                    HotelBookingService.booking_id == int(booking_id)
                )
            ).all()
        )
    except Exception:  # noqa: BLE001
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return []


def checkout_service_groups_for_booking(db: Session, booking_id: int) -> dict[str, list]:
    """خدمات الحجز مقسّمة للمغادرة: مغسلة / مخالفات / أخرى."""
    return classify_booking_services_for_checkout(_list_booking_services(db, booking_id))


def _booking_services_total(db: Session, booking_id: int) -> Decimal:
    """إجمالي الخدمات على النزيل فقط (يستثني تكلفة الإفطار المشمولة)."""
    total = Decimal("0")
    for svc in _list_booking_services(db, booking_id):
        if _svc_charged_to_guest(svc) is False:
            continue
        total += Decimal(str(svc.line_total or 0))
    return total.quantize(Decimal("0.001"))


def list_open_room_charges_for_booking(
    db: Session,
    booking_id: int,
    *,
    auto_link: bool = True,
) -> list[RoomCharge]:
    """فواتير المطعم/الغرفة المفتوحة المرتبطة بالحجز أو بالغرفة أثناء الإقامة.

    الدين واحد: إقامة + وجبات + خدمات. لا ننقل فاتورة مربوطة بحجز آخر إلى هذا
    الحجز (كان يُفرّغ إيصال الحجز السابق فيظهر «خالص» وعليه دين مطعم).

    أثناء CHECKED_IN تُحسب فواتير الغرفة غير المربوطة (``booking_id IS NULL``)
    ضمن حساب النزيل حتى لا يُفوَّت دين وجبة.
    """
    booking = db.get(HotelBooking, booking_id)
    if booking is None:
        return []

    room_id = int(booking.room_id) if booking.room_id else None
    checked_in = booking.booking_status == BookingStatus.CHECKED_IN
    bid = int(booking_id)

    seen: set[int] = set()
    out: list[RoomCharge] = []

    def _add(rows: list[RoomCharge]) -> None:
        for rc in rows:
            rid = int(rc.id)
            if rid in seen:
                continue
            # لا تسرق فاتورة حجز آخر
            other = getattr(rc, "booking_id", None)
            if other is not None and int(other) != bid:
                continue
            seen.add(rid)
            out.append(rc)

    # 1) مربوط صراحةً بهذا الحجز
    _add(
        list(
            db.scalars(
                select(RoomCharge).where(
                    RoomCharge.is_settled.is_(False),
                    RoomCharge.booking_id == bid,
                )
            ).all()
        )
    )

    # 2) فاتورة البيع مربوطة بالحجز لكن قيد الغرفة بلا booking_id
    sale_ids: list[int] = []
    try:
        sale_ids = list(
            db.scalars(select(Sale.id).where(Sale.booking_id == bid)).all()
        )
    except Exception:  # noqa: BLE001
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        sale_ids = []
    if sale_ids:
        _add(
            list(
                db.scalars(
                    select(RoomCharge).where(
                        RoomCharge.is_settled.is_(False),
                        RoomCharge.sale_id.in_(sale_ids),
                    )
                ).all()
            )
        )

    # 3) أثناء الإقامة فقط: فواتير الغرفة غير المربوطة بأي حجز
    # (بعد المغادرة لا نُلصق فواتير NULL بحجز قديم — حتى لا تختلط مع نزيل لاحق)
    if checked_in and room_id is not None:
        _add(
            list(
                db.scalars(
                    select(RoomCharge).where(
                        RoomCharge.is_settled.is_(False),
                        RoomCharge.room_id == room_id,
                        RoomCharge.booking_id.is_(None),
                    )
                ).all()
            )
        )

    if auto_link and out:
        for rc in out:
            if rc.booking_id is None:
                rc.booking_id = bid
            sale = rc.sale
            if sale is not None and getattr(sale, "booking_id", None) is None:
                sale.booking_id = bid
        db.flush()

    return out


def _service_sale_ids(db: Session, booking_id: int) -> set[int]:
    ids: set[int] = set()
    for svc in _list_booking_services(db, booking_id):
        try:
            sid = svc.sale_id
        except Exception:  # noqa: BLE001
            # عمود sale_id قد يكون غير مُرقّع بعد على السيرفر
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
            continue
        if sid is not None:
            ids.add(int(sid))
    return ids


def _pos_charges_total(
    db: Session,
    booking_id: int,
    *,
    pos_item_details: bool = False,
) -> tuple[Decimal, list[FolioLine]]:
    """إجمالي وجبات/طلبات الشقة غير المسدّدة ضمن دين الحجز الموحّد."""
    from modules.hotel.breakfast_settle import sale_is_hotel_breakfast

    charges = list_open_room_charges_for_booking(db, booking_id, auto_link=True)
    total = Decimal("0")
    lines: list[FolioLine] = []
    covered_sale_ids: set[int] = _service_sale_ids(db, booking_id)
    for rc in charges:
        sid = int(rc.sale_id)
        # مسجّل مسبقاً كخدمة على الحجز (بعد تسوية فندق→مطعم) — لا تُحسب مرتين
        if sid in covered_sale_ids:
            continue
        # إفطار مشمول: يظهر في التسوية فقط — ليس على حساب النزيل / تفاصيل الحجز
        if sale_is_hotel_breakfast(db, rc.sale):
            covered_sale_ids.add(sid)
            continue
        due = sale_outstanding_total(db, rc.sale_id)
        if due <= 0:
            covered_sale_ids.add(sid)
            continue
        sale = rc.sale
        label = folio_pos_charge_description(
            db,
            sale,
            rc.sale_id,
            due,
            include_item_details=pos_item_details,
        )
        total += due
        covered_sale_ids.add(sid)
        lines.append(
            FolioLine(
                kind="pos",
                description=label,
                amount=due,
                ref_id=rc.id,
                sale_id=sid,
            )
        )

    # فواتير مربوطة بالحجز بلا قيد غرفة / أو قيد مسوّى بالخطأ وما زال عليها رصيد
    orphan_sales: list[Sale] = []
    try:
        orphan_sales = list(
            db.scalars(
                select(Sale).where(
                    Sale.booking_id == int(booking_id),
                    Sale.status == SaleStatus.COMPLETED,
                )
            ).all()
        )
    except Exception:  # noqa: BLE001
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        orphan_sales = []
    for sale in orphan_sales:
        sid = int(sale.id)
        if sid in covered_sale_ids:
            continue
        if sale_is_hotel_breakfast(db, sale):
            continue
        due = sale_outstanding_total(db, sid)
        if due <= 0:
            continue
        label = folio_pos_charge_description(
            db,
            sale,
            sid,
            due,
            include_item_details=pos_item_details,
        )
        total += due
        covered_sale_ids.add(sid)
        lines.append(
            FolioLine(
                kind="pos",
                description=label,
                amount=due,
                ref_id=None,
                sale_id=sid,
            )
        )

    return total.quantize(Decimal("0.001")), lines


def _extract_sale_num(text: str) -> int | None:
    m = _SALE_NUM_RE.search(text or "")
    if not m:
        return None
    try:
        return int(m.group(1))
    except (TypeError, ValueError):
        return None


def _service_folio_label(svc: HotelBookingService) -> tuple[str, str, int | None]:
    """(kind, description, sale_id) — مختصر بدون تفاصيل أصناف."""
    sale_id: int | None = None
    try:
        raw = svc.sale_id
        if raw is not None:
            sale_id = int(raw)
    except Exception:  # noqa: BLE001
        sale_id = None
    name = (svc.name_ar or "").strip()
    if sale_id is None:
        sale_id = _extract_sale_num(name)

    try:
        svc_code = getattr(svc, "service_code", None)
    except Exception:  # noqa: BLE001
        svc_code = None
    if is_violation_service(name, svc_code if isinstance(svc_code, str) else None):
        return "violation", name or "مخالفة", None

    if is_laundry_service(name):
        if sale_id:
            return "laundry", f"فاتورة مغسلة #{sale_id}", sale_id
        return "laundry", "مغسلة", None

    low = name.lower()
    is_restaurant = any(k in low for k in _RESTAURANT_KEYWORDS)
    if sale_id and is_restaurant:
        return "pos", f"فاتورة مطعم #{sale_id}", sale_id
    if sale_id:
        return "service", f"فاتورة خدمة #{sale_id}", sale_id
    if is_restaurant:
        return "pos", "فاتورة مطعم", None
    return "service", name or "خدمة", None


def _amt_side(obj, side: str, full: Decimal) -> Decimal:
    """مبلغ البند على جهة معينة."""
    try:
        ca = getattr(obj, "company_amount", None)
        ga = getattr(obj, "guest_amount", None)
        if ca is not None or ga is not None:
            if side == "COMPANY":
                return Decimal(str(ca or 0)).quantize(Decimal("0.001"))
            return Decimal(str(ga or 0)).quantize(Decimal("0.001"))
        fs = (getattr(obj, "folio_side", None) or "").upper()
        if fs == "COMPANY":
            return full if side == "COMPANY" else Decimal("0")
        if fs == "SHARED":
            # بدون تفصيل: نصف
            half = (full / 2).quantize(Decimal("0.001"))
            return half
        # GUEST / فارغ
        if side == "GUEST":
            return full
        return Decimal("0")
    except Exception:  # noqa: BLE001
        return full if side == "GUEST" else Decimal("0")


def build_folio(
    db: Session,
    booking_id: int,
    *,
    pos_item_details: bool = False,
) -> FolioSummary:
    booking = db.get(HotelBooking, booking_id)
    if booking is None:
        raise ValueError("الحجز غير موجود.")

    cache = getattr(db, "_folio_build_cache", None)
    if cache is None:
        cache = {}
        setattr(db, "_folio_build_cache", cache)
    cache_key = (
        int(booking_id),
        bool(pos_item_details),
        str(booking.paid_amount or 0),
        str(booking.accommodation_total or 0),
    )
    hit = cache.get(cache_key)
    if hit is not None:
        return hit

    acc = Decimal(str(booking.accommodation_total or 0)) - Decimal(
        str(booking.discount_amount or 0)
    )
    # إجمالي ما يظهر في الحساب الكلي (شركة + نزيل)
    svc_guest_total = Decimal("0")
    svc_company_total = Decimal("0")
    pos_guest_total = Decimal("0")
    pos_company_total = Decimal("0")
    pos_total, pos_lines = _pos_charges_total(
        db, booking_id, pos_item_details=False
    )
    _ = pos_item_details
    paid = Decimal(str(booking.paid_amount or 0))

    # توزيع الإقامة
    from modules.hotel.company_agreement_service import get_booking_rule

    stay_rule = get_booking_rule(db, booking_id, "ACCOMMODATION")
    stay_bearer = (
        (stay_rule.bearer if stay_rule else None)
        or getattr(booking, "stay_payer", None)
        or getattr(booking, "booking_payer", None)
        or "GUEST"
    )
    stay_bearer = str(stay_bearer).upper()
    if stay_bearer == "COMPANY":
        acc_company, acc_guest = acc, Decimal("0")
    elif stay_bearer == "SHARED":
        half = (acc / 2).quantize(Decimal("0.001"))
        acc_company, acc_guest = half, (acc - half).quantize(Decimal("0.001"))
    else:
        acc_company, acc_guest = Decimal("0"), acc

    nights_shown = int(booking.nights or 0)
    if nights_shown <= 0 and acc > 0:
        nights_shown = 1
    lines: list[FolioLine] = []
    co_lines: list[FolioLine] = []
    gu_lines: list[FolioLine] = []
    if acc > 0:
        fl = FolioLine(
            kind="accommodation",
            description=f"إقامة {nights_shown} ليلة",
            amount=acc,
            folio_side=stay_bearer if stay_bearer in ("COMPANY", "GUEST", "SHARED") else "GUEST",
        )
        lines.append(fl)
        if acc_company > 0:
            co_lines.append(
                FolioLine(
                    kind="accommodation",
                    description=f"إقامة {nights_shown} ليلة",
                    amount=acc_company,
                    folio_side="COMPANY",
                )
            )
        if acc_guest > 0:
            gu_lines.append(
                FolioLine(
                    kind="accommodation",
                    description=f"إقامة {nights_shown} ليلة",
                    amount=acc_guest,
                    folio_side="GUEST",
                )
            )

    for svc in _list_booking_services(db, booking_id):
        # تكلفة فندق فقط
        if _svc_charged_to_guest(svc) is False:
            ca = _amt_side(svc, "COMPANY", Decimal(str(svc.line_total or 0)))
            ga = _amt_side(svc, "GUEST", Decimal(str(svc.line_total or 0)))
            # إذا كانت cost hotel فقط (لا company ولا guest) — تخطَّ
            if ca <= 0 and ga <= 0:
                # charged_to_guest False وقد تكون كلها شركة!
                fs = (getattr(svc, "folio_side", None) or "").upper()
                if fs == "HOTEL":
                    continue
                # legacy: included breakfast
                continue
        amt = Decimal(str(svc.line_total or 0))
        ca = _amt_side(svc, "COMPANY", amt)
        ga = _amt_side(svc, "GUEST", amt)
        # legacy: لا company_amount — كله نزيل إذا charged_to_guest
        if ca <= 0 and ga <= 0 and _svc_charged_to_guest(svc) is not False:
            ga = amt
        svc_company_total += ca
        svc_guest_total += ga
        kind, label, sale_id = _service_folio_label(svc)
        if ca > 0:
            co_lines.append(
                FolioLine(
                    kind=kind,
                    description=label,
                    amount=ca,
                    ref_id=svc.id,
                    sale_id=sale_id,
                    folio_side="COMPANY",
                )
            )
        if ga > 0:
            gu_lines.append(
                FolioLine(
                    kind=kind,
                    description=label,
                    amount=ga,
                    ref_id=svc.id,
                    sale_id=sale_id,
                    folio_side="GUEST",
                )
            )
            lines.append(
                FolioLine(
                    kind=kind,
                    description=label,
                    amount=ga,
                    ref_id=svc.id,
                    sale_id=sale_id,
                    folio_side="GUEST",
                )
            )
        elif ca > 0:
            # عرض بند الشركة أيضاً في القائمة الشاملة
            lines.append(
                FolioLine(
                    kind=kind,
                    description=f"{label} (شركة)",
                    amount=ca,
                    ref_id=svc.id,
                    sale_id=sale_id,
                    folio_side="COMPANY",
                )
            )

    from modules.hotel.breakfast_settle import sale_is_hotel_breakfast
    from modules.refunds.service import sale_outstanding_total

    covered = _service_sale_ids(db, booking_id)
    for rc in list_open_room_charges_for_booking(db, booking_id, auto_link=True):
        sid = int(rc.sale_id)
        if sid in covered:
            continue
        if sale_is_hotel_breakfast(db, rc.sale):
            continue
        due = sale_outstanding_total(db, rc.sale_id)
        if due <= 0:
            continue
        covered.add(sid)
        ca = _amt_side(rc, "COMPANY", due)
        ga = _amt_side(rc, "GUEST", due)
        if ca <= 0 and ga <= 0:
            ga = due
        pos_company_total += ca
        pos_guest_total += ga
        label = folio_pos_charge_description(
            db, rc.sale, sid, due, include_item_details=False
        )
        if ca > 0:
            co_lines.append(
                FolioLine(
                    kind="pos",
                    description=label,
                    amount=ca,
                    ref_id=rc.id,
                    sale_id=sid,
                    folio_side="COMPANY",
                )
            )
            lines.append(
                FolioLine(
                    kind="pos",
                    description=f"{label} (شركة)",
                    amount=ca,
                    ref_id=rc.id,
                    sale_id=sid,
                    folio_side="COMPANY",
                )
            )
        if ga > 0:
            gu_lines.append(
                FolioLine(
                    kind="pos",
                    description=label,
                    amount=ga,
                    ref_id=rc.id,
                    sale_id=sid,
                    folio_side="GUEST",
                )
            )
            lines.append(
                FolioLine(
                    kind="pos",
                    description=label,
                    amount=ga,
                    ref_id=rc.id,
                    sale_id=sid,
                    folio_side="GUEST",
                )
            )

    company_total = (acc_company + svc_company_total + pos_company_total).quantize(
        Decimal("0.001")
    )
    guest_total = (acc_guest + svc_guest_total + pos_guest_total).quantize(
        Decimal("0.001")
    )
    # الإجمالي الكلي = شركة + نزيل (وليس تكرار pos_total القديم إن وُزّع)
    total = (company_total + guest_total).quantize(Decimal("0.001"))
    # توافق: إن فشل التوزيع وكان total=0 مع acc>0 استخدم القديم
    if total <= 0 and (acc + pos_total) > 0:
        total = (acc + _booking_services_total(db, booking_id) + pos_total).quantize(
            Decimal("0.001")
        )
        guest_total = total
        company_total = Decimal("0")
    balance = (total - paid).quantize(Decimal("0.001"))
    svc_total = (svc_guest_total + svc_company_total).quantize(Decimal("0.001"))
    pos_sum = (pos_guest_total + pos_company_total).quantize(Decimal("0.001"))

    summary = FolioSummary(
        accommodation=acc,
        pos_charges=pos_sum if pos_sum > 0 else pos_total,
        services=svc_total,
        discount=Decimal(str(booking.discount_amount or 0)),
        paid=paid,
        total=total,
        balance=balance,
        lines=lines,
        company_folio=SideFolio(
            side="COMPANY",
            label="حساب الشركة",
            total=company_total,
            lines=co_lines,
        ),
        guest_folio=SideFolio(
            side="GUEST",
            label="حساب النزيل",
            total=guest_total,
            lines=gu_lines,
        ),
        company_total=company_total,
        guest_total=guest_total,
    )
    cache[cache_key] = summary
    return summary


@dataclass
class FolioDebtBreakdown:
    stay: Decimal
    laundry: Decimal
    restaurant: Decimal
    other_services: Decimal
    balance: Decimal


def folio_debt_breakdown(db: Session, booking_id: int) -> FolioDebtBreakdown:
    """تقسيم المتبقي غير المدفوع: إقامة → مغسلة → خدمات → مطعم."""
    folio = build_folio(db, booking_id)
    zero = Decimal("0")
    if folio.balance <= zero:
        return FolioDebtBreakdown(
            stay=zero, laundry=zero, restaurant=zero, other_services=zero, balance=zero
        )

    laundry_total = zero
    other_svc = zero
    for svc in _list_booking_services(db, booking_id):
        if _svc_charged_to_guest(svc) is False:
            continue
        amt = Decimal(str(svc.line_total or 0))
        if is_laundry_service(svc.name_ar):
            laundry_total += amt
        else:
            other_svc += amt
    laundry_total = laundry_total.quantize(Decimal("0.001"))
    other_svc = other_svc.quantize(Decimal("0.001"))

    paid = Decimal(str(folio.paid or 0))
    acc = Decimal(str(folio.accommodation or 0))

    stay_unpaid = max(zero, acc - paid)
    paid_after = max(zero, paid - acc)
    laundry_unpaid = max(zero, laundry_total - paid_after)
    paid_after = max(zero, paid_after - laundry_total)
    other_unpaid = max(zero, other_svc - paid_after)
    paid_after = max(zero, paid_after - other_svc)
    rest_unpaid = max(zero, Decimal(str(folio.pos_charges or 0)) - paid_after)

    return FolioDebtBreakdown(
        stay=stay_unpaid.quantize(Decimal("0.001")),
        laundry=laundry_unpaid.quantize(Decimal("0.001")),
        restaurant=rest_unpaid.quantize(Decimal("0.001")),
        other_services=other_unpaid.quantize(Decimal("0.001")),
        balance=folio.balance.quantize(Decimal("0.001")),
    )


def booking_balance_due(db: Session, booking_id: int) -> Decimal:
    return build_folio(db, booking_id).balance


def folio_auto_wallet_due(db: Session, booking: HotelBooking) -> Decimal:
    """المتبقي الذي يجوز خصمه تلقائياً من محفظة عميل الحجز.

    محفظة الشركة لا تسدّد حساب النزيل (مطعم/خدمات الضيف).
    محفظة النزيل الشخصي على حجز شركة لا تسدّد حساب الشركة.
    حجز فردي بلا شركة: يُغطّى كل المتبقي.
    """
    folio = build_folio(db, int(booking.id))
    paid = Decimal(str(folio.paid or 0)).quantize(Decimal("0.001"))
    company_total = Decimal(str(folio.company_total or 0)).quantize(Decimal("0.001"))
    guest_total = Decimal(str(folio.guest_total or 0)).quantize(Decimal("0.001"))
    company_id = getattr(booking, "company_customer_id", None)
    cust_id = getattr(booking, "customer_id", None)
    if company_id and cust_id and int(company_id) == int(cust_id):
        company_paid = min(paid, company_total)
        return max(Decimal("0"), (company_total - company_paid)).quantize(
            Decimal("0.001")
        )
    if company_id and cust_id and int(company_id) != int(cust_id):
        company_paid = min(paid, company_total)
        guest_paid = max(Decimal("0"), (paid - company_paid)).quantize(Decimal("0.001"))
        return max(Decimal("0"), (guest_total - guest_paid)).quantize(Decimal("0.001"))
    return max(Decimal("0"), Decimal(str(folio.balance or 0))).quantize(Decimal("0.001"))


@dataclass
class FolioDomainSlice:
    """حساب مجال واحد داخل كشف الحجز — فندق أو مطعم."""

    key: str  # hotel | restaurant
    label: str
    lines: list[FolioLine]
    total: Decimal
    paid: Decimal
    balance: Decimal
    amount_due: Decimal
    amount_credit: Decimal


def folio_line_is_restaurant(line: FolioLine) -> bool:
    return (getattr(line, "kind", None) or "") == "pos"


def folio_scope_from_domain(domain) -> str:
    """restaurant | hotel | all — حسب مجال عمل أمين الخزينة."""
    if domain is None:
        return "all"
    val = getattr(domain, "value", domain)
    key = str(val or "").strip().lower()
    if key == "restaurant":
        return "restaurant"
    if key == "hotel":
        return "hotel"
    return "all"


def side_lines_for_scope(side_folio, scope: str) -> list[FolioLine]:
    if side_folio is None or not getattr(side_folio, "lines", None):
        return []
    lines = list(side_folio.lines)
    if scope == "restaurant":
        return [ln for ln in lines if folio_line_is_restaurant(ln)]
    if scope == "hotel":
        return [ln for ln in lines if not folio_line_is_restaurant(ln)]
    return lines


def scoped_party_accounts(folio: FolioSummary, scope: str) -> BookingPartyAccounts:
    """رصيد الشركة/النزيل ضمن مجال واحد حتى لا يختلط إقامة بفواتير المطعم."""
    if scope not in ("restaurant", "hotel"):
        company_paid, guest_paid = allocate_paid_to_sides(
            folio.company_total, folio.guest_total, folio.paid
        )
        return BookingPartyAccounts(
            company=_party_from_totals(
                "COMPANY", "حساب الشركة", folio.company_total, company_paid
            ),
            guest=_party_from_totals(
                "GUEST", "حساب النزيل", folio.guest_total, guest_paid
            ),
            has_company=folio.company_total > Decimal("0.0005"),
        )
    c_lines = side_lines_for_scope(folio.company_folio, scope)
    g_lines = side_lines_for_scope(folio.guest_folio, scope)
    c_total = sum((Decimal(str(ln.amount or 0)) for ln in c_lines), Decimal("0")).quantize(
        Decimal("0.001")
    )
    g_total = sum((Decimal(str(ln.amount or 0)) for ln in g_lines), Decimal("0")).quantize(
        Decimal("0.001")
    )
    if scope == "restaurant":
        c_paid = Decimal("0")
        g_paid = Decimal("0")
    else:
        c_paid, g_paid = allocate_paid_to_sides(c_total, g_total, folio.paid)
    return BookingPartyAccounts(
        company=_party_from_totals("COMPANY", "حساب الشركة", c_total, c_paid),
        guest=_party_from_totals("GUEST", "حساب النزيل", g_total, g_paid),
        has_company=c_total > Decimal("0.0005"),
    )


def folio_domain_slices(folio: FolioSummary) -> dict[str, FolioDomainSlice]:
    """يفصل إقامة/خدمات الفندق عن فواتير المطعم المطلوبة من الحجز."""
    rest_lines = [ln for ln in folio.lines if folio_line_is_restaurant(ln)]
    hotel_lines = [ln for ln in folio.lines if not folio_line_is_restaurant(ln)]
    rest_total = sum((Decimal(str(ln.amount or 0)) for ln in rest_lines), Decimal("0")).quantize(
        Decimal("0.001")
    )
    hotel_total = sum((Decimal(str(ln.amount or 0)) for ln in hotel_lines), Decimal("0")).quantize(
        Decimal("0.001")
    )
    hotel_paid = Decimal(str(folio.paid or 0)).quantize(Decimal("0.001"))
    hotel_bal = (hotel_total - hotel_paid).quantize(Decimal("0.001"))
    rest_paid = Decimal("0")
    rest_bal = rest_total
    return {
        "hotel": FolioDomainSlice(
            key="hotel",
            label="حساب الفندق — إقامة وخدمات",
            lines=hotel_lines,
            total=hotel_total,
            paid=hotel_paid,
            balance=hotel_bal,
            amount_due=max(Decimal("0"), hotel_bal).quantize(Decimal("0.001")),
            amount_credit=max(Decimal("0"), -hotel_bal).quantize(Decimal("0.001")),
        ),
        "restaurant": FolioDomainSlice(
            key="restaurant",
            label="فواتير المطعم المطلوبة من الفندق",
            lines=rest_lines,
            total=rest_total,
            paid=rest_paid,
            balance=rest_bal,
            amount_due=max(Decimal("0"), rest_bal).quantize(Decimal("0.001")),
            amount_credit=max(Decimal("0"), -rest_bal).quantize(Decimal("0.001")),
        ),
    }


def guest_account_from_slice(slice_: FolioDomainSlice) -> GuestAccountSummary:
    return GuestAccountSummary(
        total_charges=slice_.total,
        total_paid=slice_.paid,
        deposit=Decimal("0"),
        amount_due=slice_.amount_due,
        amount_credit=slice_.amount_credit,
        balance=slice_.balance,
    )


def build_guest_account(db: Session, booking_id: int) -> GuestAccountSummary:
    """ملخص حساب الزبون: المستحقات، المدفوعات، والرصيد (دائن/مدين)."""
    folio = build_folio(db, booking_id)
    booking = db.get(HotelBooking, booking_id)
    paid = folio.paid
    raw_deposit = Decimal(str(booking.deposit_amount or 0)) if booking else Decimal("0")
    # العربون المعروض لا يتجاوز صافي المدفوع المطبّق (بعد ترحيل/إرجاع)
    deposit = min(raw_deposit, paid).quantize(Decimal("0.001"))
    balance = folio.balance
    return GuestAccountSummary(
        total_charges=folio.total,
        total_paid=paid,
        deposit=deposit,
        amount_due=max(Decimal("0"), balance).quantize(Decimal("0.001")),
        amount_credit=max(Decimal("0"), -balance).quantize(Decimal("0.001")),
        balance=balance,
    )


def build_guest_account_effective(
    db: Session,
    booking_id: int,
    *,
    actual_departure: date | None = None,
) -> GuestAccountSummary:
    """
    رصيد موحّد للعرض: لنزيل مقيم يطابق معاينة تسوية المغادرة
    (نفس رقم «رصيد النزيل» / المتبقي) وليس فقط الإجمالي المخزّن للفترة الكاملة.
    """
    booking = db.get(HotelBooking, booking_id)
    if booking is None:
        raise ValueError("الحجز غير موجود.")
    base = build_guest_account(db, booking_id)
    if booking.booking_status != BookingStatus.CHECKED_IN:
        return base

    today = date.today()
    dep = actual_departure
    if dep is None:
        dep = today if today < booking.check_out else booking.check_out
    if dep < booking.check_in:
        dep = booking.check_in
    planned = booking.planned_check_out
    if dep > planned:
        dep = planned

    try:
        from modules.hotel.departure_settlement import preview_departure_settlement

        # Overstay: لا نقيّد التاريخ بالموعد — المعاينة تقبل التأخير
        if dep > booking.check_out:
            # الرصيد الرسمي الحالي (بما فيه ليالي overstay إن حُسبت)
            return base
        prev = preview_departure_settlement(db, booking, actual_departure=dep)
    except Exception:
        return base

    total = prev.folio_total_actual
    paid = prev.paid_amount
    balance = (total - paid).quantize(Decimal("0.001"))
    return GuestAccountSummary(
        total_charges=total,
        total_paid=paid,
        deposit=base.deposit,
        amount_due=prev.balance_due,
        amount_credit=prev.refund_due,
        balance=balance,
    )
