"""كشف حساب الشقة (Folio) — إقامة + POS + خدمات."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import and_, or_, select
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
                        ("charge_reduction", "early_departure", "credit_to_wallet", "prepaid_credit_applied")
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
    for svc in db.scalars(
        select(HotelBookingService).where(HotelBookingService.booking_id == booking.id)
    ).all():
        if getattr(svc, "charged_to_guest", True) is False:
            continue
        amt = Decimal(str(svc.line_total or 0)).quantize(Decimal("0.001"))
        if amt <= 0:
            continue
        movements.append(
            BookingCashMovement(
                kind="charge",
                created_at=svc.created_at,
                amount=amt,
                label=f"مستحق خدمة: {svc.name_ar}",
                debit=amt,
            )
        )

    # رسوم مطعم/غرفة غير المسوّاة (نفس منطق الفوليو — يشمل فواتير الغرفة أثناء الإقامة)
    from modules.hotel.breakfast_settle import sale_is_hotel_breakfast

    for rc in list_open_room_charges_for_booking(db, int(booking.id), auto_link=True):
        if sale_is_hotel_breakfast(db, rc.sale):
            continue
        due = sale_outstanding_total(db, rc.sale_id)
        if due <= 0:
            continue
        movements.append(
            BookingCashMovement(
                kind="charge",
                created_at=rc.created_at,
                amount=due,
                label=f"مستحق طلبات غرفة #{rc.sale_id}",
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

    movements.sort(key=_movement_sort_key)
    running = Decimal("0")
    for m in movements:
        running = (running + m.debit - m.credit).quantize(Decimal("0.001"))
        m.running_balance = running

    return movements, gross.quantize(Decimal("0.001")), refunded.quantize(Decimal("0.001"))


def is_laundry_service(name: str | None) -> bool:
    text = (name or "").strip().lower()
    return any(k in text for k in LAUNDRY_KEYWORDS)


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


@dataclass
class GuestAccountSummary:
    """حساب الزبون — ما عليه وما له."""

    total_charges: Decimal
    total_paid: Decimal
    deposit: Decimal
    amount_due: Decimal
    amount_credit: Decimal
    balance: Decimal


def _booking_services_total(db: Session, booking_id: int) -> Decimal:
    """إجمالي الخدمات على النزيل فقط (يستثني تكلفة الإفطار المشمولة)."""
    rows = db.scalars(
        select(HotelBookingService).where(HotelBookingService.booking_id == booking_id)
    ).all()
    total = Decimal("0")
    for svc in rows:
        if getattr(svc, "charged_to_guest", True) is False:
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

    أثناء CHECKED_IN تُحسب كل فواتير الغرفة المفتوحة ضمن حساب النزيل حتى لو
    لم يُربط ``booking_id`` بعد — حتى لا يظهر الإيصال «خالص» وعليه دين وجبة.
    """
    booking = db.get(HotelBooking, booking_id)
    if booking is None:
        return []

    room_id = int(booking.room_id) if booking.room_id else None
    checked_in = booking.booking_status == BookingStatus.CHECKED_IN

    if checked_in and room_id is not None:
        charges = list(
            db.scalars(
                select(RoomCharge).where(
                    RoomCharge.room_id == room_id,
                    RoomCharge.is_settled.is_(False),
                )
            ).all()
        )
    else:
        conds = [RoomCharge.booking_id == booking_id]
        if room_id is not None:
            conds.append(
                and_(
                    RoomCharge.room_id == room_id,
                    RoomCharge.booking_id.is_(None),
                )
            )
        charges = list(
            db.scalars(
                select(RoomCharge).where(
                    RoomCharge.is_settled.is_(False),
                    or_(*conds),
                )
            ).all()
        )

    if auto_link:
        for rc in charges:
            if rc.booking_id is None or int(rc.booking_id) != int(booking_id):
                rc.booking_id = booking_id
            sale = rc.sale
            if sale is not None and getattr(sale, "booking_id", None) is None:
                sale.booking_id = booking_id
        if charges:
            db.flush()

    # إزالة التكرار إن وُجد
    seen: set[int] = set()
    out: list[RoomCharge] = []
    for rc in charges:
        rid = int(rc.id)
        if rid in seen:
            continue
        seen.add(rid)
        out.append(rc)
    return out


def _pos_charges_total(
    db: Session,
    booking_id: int,
    *,
    pos_item_details: bool = False,
) -> tuple[Decimal, list[FolioLine]]:
    from modules.hotel.breakfast_settle import sale_is_hotel_breakfast

    charges = list_open_room_charges_for_booking(db, booking_id, auto_link=True)
    total = Decimal("0")
    lines: list[FolioLine] = []
    for rc in charges:
        # إفطار مشمول: يظهر في التسوية فقط — ليس على حساب النزيل / تفاصيل الحجز
        if sale_is_hotel_breakfast(db, rc.sale):
            continue
        due = sale_outstanding_total(db, rc.sale_id)
        if due <= 0:
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
        lines.append(
            FolioLine(
                kind="pos",
                description=label,
                amount=due,
                ref_id=rc.id,
                sale_id=int(rc.sale_id),
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
    sale_id = int(svc.sale_id) if getattr(svc, "sale_id", None) else None
    name = (svc.name_ar or "").strip()
    if sale_id is None:
        sale_id = _extract_sale_num(name)

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


def build_folio(
    db: Session,
    booking_id: int,
    *,
    pos_item_details: bool = False,
) -> FolioSummary:
    booking = db.get(HotelBooking, booking_id)
    if booking is None:
        raise ValueError("الحجز غير موجود.")

    acc = Decimal(str(booking.accommodation_total or 0)) - Decimal(
        str(booking.discount_amount or 0)
    )
    svc_total = _booking_services_total(db, booking_id)
    # فاتورة النزيل النهائية: أرقام فقط — بدون تفاصيل أصناف المطعم
    pos_total, pos_lines = _pos_charges_total(
        db, booking_id, pos_item_details=False
    )
    _ = pos_item_details
    paid = Decimal(str(booking.paid_amount or 0))
    total = (acc + svc_total + pos_total).quantize(Decimal("0.001"))
    balance = (total - paid).quantize(Decimal("0.001"))

    nights_shown = int(booking.nights or 0)
    # مغادرة بنفس يوم الوصول مع احتساب ليلة مستهلكة
    if nights_shown <= 0 and acc > 0:
        nights_shown = 1
    lines: list[FolioLine] = [
        FolioLine(
            kind="accommodation",
            description=f"إقامة {nights_shown} ليلة",
            amount=acc,
        )
    ]
    for svc in db.scalars(
        select(HotelBookingService).where(HotelBookingService.booking_id == booking_id)
    ).all():
        amt = Decimal(str(svc.line_total or 0))
        if getattr(svc, "charged_to_guest", True) is False:
            # تكلفة فندق (إفطار مشمول…) — لا تُعرض في تفاصيل حساب النزيل
            continue
        kind, label, sale_id = _service_folio_label(svc)
        lines.append(
            FolioLine(
                kind=kind,
                description=label,
                amount=amt,
                ref_id=svc.id,
                sale_id=sale_id,
            )
        )
    lines.extend(pos_lines)

    return FolioSummary(
        accommodation=acc,
        pos_charges=pos_total,
        services=svc_total,
        discount=Decimal(str(booking.discount_amount or 0)),
        paid=paid,
        total=total,
        balance=balance,
        lines=lines,
    )


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
    for svc in db.scalars(
        select(HotelBookingService).where(HotelBookingService.booking_id == booking_id)
    ).all():
        if getattr(svc, "charged_to_guest", True) is False:
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


def build_guest_account(db: Session, booking_id: int) -> GuestAccountSummary:
    """ملخص حساب الزبون: المستحقات، المدفوعات، والرصيد (دائن/مدين)."""
    folio = build_folio(db, booking_id)
    booking = db.get(HotelBooking, booking_id)
    deposit = Decimal(str(booking.deposit_amount or 0)) if booking else Decimal("0")
    balance = folio.balance
    return GuestAccountSummary(
        total_charges=folio.total,
        total_paid=folio.paid,
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
