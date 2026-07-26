"""تمييز وجبات الإفطار المشمولة (تكلفة فندق وليست على النزيل)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from modules.sales.models import Sale

# إعداد: إذا = 1 تُعامل فواتير الإفطار كتكلفة فندق (مشمولة)
# إذا = 0 تُعامل مثل باقي وجبات الغرفة (على حساب النزيل)
BREAKFAST_INCLUDED_SETTING_KEY = "hotel_breakfast_included_enabled"

# كلمات في اسم الصنف أو الفئة تُعتبر إفطاراً مشمولاً
_BREAKFAST_TOKENS = (
    "افطار",
    "إفطار",
    "فطور",
    "breakfast",
)


def _norm(text: str | None) -> str:
    return (text or "").strip().lower().replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")


def text_looks_like_breakfast(text: str | None) -> bool:
    n = _norm(text)
    if not n:
        return False
    return any(tok in n for tok in (_norm(t) for t in _BREAKFAST_TOKENS))


def hotel_breakfast_included_enabled(db: Session) -> bool:
    """هل وضع «إفطار مشمول = تكلفة فندق» مفعّل من إعدادات الحجز؟"""
    from modules.settings.service import get_bool

    return get_bool(db, BREAKFAST_INCLUDED_SETTING_KEY, True)


def product_is_hotel_breakfast(product) -> bool:
    """صنف إفطار مشمول: علم المنتج أو اسم/فئة تحتوي إفطار."""
    if product is None:
        return False
    if bool(getattr(product, "is_hotel_breakfast", False)):
        return True
    if text_looks_like_breakfast(getattr(product, "name_ar", None)):
        return True
    cat = getattr(product, "category", None)
    if cat is not None and text_looks_like_breakfast(getattr(cat, "name_ar", None)):
        return True
    return False


def sale_looks_like_hotel_breakfast(db: Session, sale: Sale | None) -> bool:
    """الفاتورة إفطار (كل أصنافها إفطار) — بغض النظر عن إعداد التفعيل."""
    if sale is None:
        return False
    lines = list(getattr(sale, "lines", None) or [])
    if not lines:
        note = getattr(sale, "notes", None) or getattr(sale, "note", None)
        return text_looks_like_breakfast(str(note or ""))
    breakfast_lines = 0
    other_lines = 0
    for ln in lines:
        product = getattr(ln, "product", None)
        if product is None and getattr(ln, "product_id", None):
            from modules.catalog.models import Product

            product = db.get(Product, int(ln.product_id))
        if product_is_hotel_breakfast(product):
            breakfast_lines += 1
        else:
            snap = getattr(ln, "product_name", None) or getattr(ln, "name_ar", None)
            if text_looks_like_breakfast(snap):
                breakfast_lines += 1
            else:
                other_lines += 1
    if breakfast_lines <= 0:
        return False
    # مختلطة → ليست «إفطار فقط»
    return other_lines == 0


def sale_is_hotel_breakfast(db: Session, sale: Sale | None) -> bool:
    """إفطار مشمول فعلياً (للتسمية والتسوية كتكلفة فندق) — يحترم إعداد الأدمن."""
    if not hotel_breakfast_included_enabled(db):
        return False
    return sale_looks_like_hotel_breakfast(db, sale)


@dataclass(frozen=True)
class BreakfastCostRow:
    sale_id: int
    room_number: str
    booking_ref: str | None
    guest_name: str | None
    amount: Decimal
    status: str  # pending | hotel_cost | on_guest
    at: datetime | None
    items_summary: str


def _day_bounds_utc(d: date) -> tuple[datetime, datetime]:
    start = datetime.combine(d, time.min, tzinfo=timezone.utc)
    end = datetime.combine(d, time.max, tzinfo=timezone.utc)
    return start, end


def _sale_items_summary(sale: Sale | None) -> str:
    if sale is None:
        return ""
    parts: list[str] = []
    for ln in list(getattr(sale, "lines", None) or []):
        name = (
            getattr(ln, "product_name", None)
            or getattr(getattr(ln, "product", None), "name_ar", None)
            or getattr(ln, "name_ar", None)
            or "صنف"
        )
        qty = getattr(ln, "qty", None) or getattr(ln, "quantity", None) or 1
        parts.append(f"{name} × {qty}")
    return " · ".join(parts[:6])


def list_breakfast_cost_rows(
    db: Session,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = 500,
) -> tuple[list[BreakfastCostRow], Decimal, Decimal]:
    """يجمع فواتير الإفطار (معلّقة + مسوّاة كتكلفة فندق / على النزيل).

    يعيد (الصفوف، إجمالي تكلفة الفندق المسجّلة، إجمالي معلّق).
    """
    from modules.hotel.booking_models import HotelBooking, HotelBookingService
    from modules.hotel.models import RoomCharge
    from modules.refunds.service import sale_outstanding_total

    rows: list[BreakfastCostRow] = []
    hotel_cost_total = Decimal("0")
    pending_total = Decimal("0")

    # 1) خدمات حجز مسجّلة كتكلفة فندق (بعد تسوية إفطار)
    svc_q = (
        select(HotelBookingService)
        .options(joinedload(HotelBookingService.booking).joinedload(HotelBooking.room))
        .where(HotelBookingService.charged_to_guest.is_(False))
        .order_by(HotelBookingService.created_at.desc())
        .limit(limit)
    )
    if date_from is not None:
        start, _ = _day_bounds_utc(date_from)
        svc_q = svc_q.where(HotelBookingService.created_at >= start)
    if date_to is not None:
        _, end = _day_bounds_utc(date_to)
        svc_q = svc_q.where(HotelBookingService.created_at <= end)

    for svc in db.scalars(svc_q).unique().all():
        sale = db.get(Sale, int(svc.sale_id)) if svc.sale_id else None
        note_txt = f"{svc.name_ar or ''} {svc.notes or ''}"
        if sale is not None:
            if not sale_looks_like_hotel_breakfast(db, sale) and not text_looks_like_breakfast(
                note_txt
            ):
                continue
        elif not text_looks_like_breakfast(note_txt):
            continue
        booking = svc.booking
        room = getattr(booking, "room", None) if booking else None
        amt = Decimal(str(svc.line_total or 0)).quantize(Decimal("0.001"))
        if amt <= 0 and svc.unit_price is not None:
            qty = Decimal(str(svc.quantity or 1))
            amt = (Decimal(str(svc.unit_price)) * qty).quantize(Decimal("0.001"))
        hotel_cost_total += amt
        rows.append(
            BreakfastCostRow(
                sale_id=int(svc.sale_id or 0),
                room_number=str(getattr(room, "number", "") or "—"),
                booking_ref=getattr(booking, "reference", None) if booking else None,
                guest_name=getattr(booking, "guest_name", None) if booking else None,
                amount=amt,
                status="hotel_cost",
                at=svc.created_at,
                items_summary=_sale_items_summary(sale) or (svc.name_ar or ""),
            )
        )

    # 2) فواتير غرفة معلّقة تبدو إفطاراً
    open_q = (
        select(RoomCharge)
        .options(
            joinedload(RoomCharge.sale).joinedload(Sale.lines),
            joinedload(RoomCharge.room),
        )
        .where(RoomCharge.is_settled.is_(False))
        .order_by(RoomCharge.created_at.desc())
        .limit(limit)
    )
    if date_from is not None:
        start, _ = _day_bounds_utc(date_from)
        open_q = open_q.where(RoomCharge.created_at >= start)
    if date_to is not None:
        _, end = _day_bounds_utc(date_to)
        open_q = open_q.where(RoomCharge.created_at <= end)

    for rc in db.scalars(open_q).unique().all():
        sale = rc.sale or db.get(Sale, int(rc.sale_id))
        if not sale_looks_like_hotel_breakfast(db, sale):
            continue
        due = sale_outstanding_total(db, int(sale.id)).quantize(Decimal("0.001"))
        if due <= Decimal("0.0005"):
            continue
        pending_total += due
        room = rc.room
        booking = db.get(HotelBooking, int(rc.booking_id)) if rc.booking_id else None
        rows.append(
            BreakfastCostRow(
                sale_id=int(sale.id),
                room_number=str(getattr(room, "number", "") or "—"),
                booking_ref=getattr(booking, "reference", None) if booking else None,
                guest_name=(
                    rc.guest_name_snapshot
                    or (getattr(booking, "guest_name", None) if booking else None)
                ),
                amount=due,
                status="pending",
                at=rc.created_at,
                items_summary=_sale_items_summary(sale),
            )
        )

    rows.sort(key=lambda r: r.at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return rows[:limit], hotel_cost_total.quantize(Decimal("0.001")), pending_total.quantize(
        Decimal("0.001")
    )
