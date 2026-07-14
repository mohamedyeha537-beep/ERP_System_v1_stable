"""كشف حساب الشقة (Folio) — إقامة + POS + خدمات."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.hotel.booking_models import HotelBooking, HotelBookingService
from modules.hotel.models import RoomCharge
from modules.refunds.service import sale_outstanding_total
from modules.sales.models import Sale, SaleContext, SaleStatus

LAUNDRY_KEYWORDS = ("غسيل", "مغسلة", "laundry", "wash", "غسل")


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
    db: Session, sale: Sale | None, sale_id: int, due: Decimal
) -> str:
    """وصف بند POS على فاتورة الحجز — يوضّح المصدر (مطعم روف) وملخّص الأصناف."""
    if sale is None:
        return f"طلب #{sale_id} — {due} د.ل"
    if sale.status != SaleStatus.COMPLETED:
        return f"فاتورة #{sale_id}"

    source = _pos_charge_source_label(db, sale)
    preview = _pos_charge_items_preview(sale)
    if preview:
        return f"{source} — طلب #{sale_id} ({preview}) — {due} د.ل"
    return f"{source} — طلب #{sale_id} — {due} د.ل"


@dataclass
class FolioLine:
    kind: str
    description: str
    amount: Decimal
    ref_id: int | None = None


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
    rows = db.scalars(
        select(HotelBookingService.line_total).where(
            HotelBookingService.booking_id == booking_id
        )
    ).all()
    return sum((Decimal(str(r or 0)) for r in rows), Decimal("0")).quantize(Decimal("0.001"))


def _pos_charges_total(db: Session, booking_id: int) -> tuple[Decimal, list[FolioLine]]:
    charges = list(
        db.scalars(
            select(RoomCharge).where(
                RoomCharge.booking_id == booking_id,
                RoomCharge.is_settled.is_(False),
            )
        ).all()
    )
    total = Decimal("0")
    lines: list[FolioLine] = []
    for rc in charges:
        due = sale_outstanding_total(db, rc.sale_id)
        if due <= 0:
            continue
        sale = rc.sale
        label = folio_pos_charge_description(db, sale, rc.sale_id, due)
        total += due
        lines.append(FolioLine(kind="pos", description=label, amount=due, ref_id=rc.id))
    return total.quantize(Decimal("0.001")), lines


def build_folio(db: Session, booking_id: int) -> FolioSummary:
    booking = db.get(HotelBooking, booking_id)
    if booking is None:
        raise ValueError("الحجز غير موجود.")

    acc = Decimal(str(booking.accommodation_total or 0)) - Decimal(
        str(booking.discount_amount or 0)
    )
    svc_total = _booking_services_total(db, booking_id)
    pos_total, pos_lines = _pos_charges_total(db, booking_id)
    paid = Decimal(str(booking.paid_amount or 0))
    total = (acc + svc_total + pos_total).quantize(Decimal("0.001"))
    balance = (total - paid).quantize(Decimal("0.001"))

    lines: list[FolioLine] = [
        FolioLine(
            kind="accommodation",
            description=f"إقامة {booking.nights} ليلة × {booking.nightly_rate}",
            amount=acc,
        )
    ]
    for svc in db.scalars(
        select(HotelBookingService).where(HotelBookingService.booking_id == booking_id)
    ).all():
        lines.append(
            FolioLine(
                kind="service",
                description=svc.name_ar,
                amount=Decimal(str(svc.line_total)),
                ref_id=svc.id,
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
