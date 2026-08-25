"""فواتير الإقامة والإقفال اليومي."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from modules.hotel.booking_models import (
    BookingStatus,
    HotelBooking,
    HotelBookingPayment,
    HotelDailyClosing,
    HotelInvoice,
    HotelInvoiceItem,
    HotelInvoiceStatus,
)
from modules.hotel.folio import build_folio
from modules.hotel.models import HotelRoom
from modules.platform.module_registry import HOTEL_FINANCE, is_module_enabled


class FinanceError(Exception):
    pass


def finance_enabled(db: Session) -> bool:
    return is_module_enabled(db, HOTEL_FINANCE)


def issue_checkout_invoice(
    db: Session,
    booking_id: int,
    *,
    user_id: int | None = None,
) -> HotelInvoice:
    if not finance_enabled(db):
        raise FinanceError("وحدة المالية الفندقية غير مفعّلة.")
    booking = db.get(HotelBooking, booking_id)
    if booking is None:
        raise FinanceError("الحجز غير موجود.")
    folio = build_folio(db, booking_id)
    from modules.printing.doc_numbers import PrintDocKind, ensure_doc_number

    inv_num = ensure_doc_number(
        db,
        getattr(booking, "final_invoice_number", None),
        PrintDocKind.FINAL_INVOICE,
        domain="hotel",
    )
    booking.final_invoice_number = inv_num
    subtotal = (folio.total + folio.discount).quantize(Decimal("0.001"))
    inv = HotelInvoice(
        booking_id=booking_id,
        invoice_number=inv_num,
        status=HotelInvoiceStatus.ISSUED,
        subtotal=subtotal,
        discount=folio.discount,
        total=folio.total,
        paid=folio.paid,
        issued_at=datetime.now(timezone.utc),
        issued_by_id=user_id,
    )
    db.add(inv)
    db.flush()
    for line in folio.lines:
        db.add(
            HotelInvoiceItem(
                invoice_id=inv.id,
                description=line.description,
                quantity=Decimal("1"),
                unit_price=line.amount,
                line_total=line.amount,
                item_type=line.kind,
            )
        )
    db.flush()
    return inv


def try_post_booking_gl(db: Session, booking_id: int) -> None:
    """ترحيل GL اختياري عند Check-out."""
    from modules.gl.service import is_gl_enabled

    if not is_gl_enabled(db):
        return
    booking = db.get(
        __import__("modules.hotel.booking_models", fromlist=["HotelBooking"]).HotelBooking,
        booking_id,
    )
    if booking is None:
        return
    # Shadow mode — log only; full journal rules can extend posting.py later.
    from modules.hotel.audit import log_audit

    log_audit(
        db,
        entity_type="booking",
        entity_id=booking_id,
        action="gl_post_skipped",
        new_value=str(booking.accommodation_total),
        reason="hotel.finance tier — extend modules/gl/posting.py for live entries",
    )


def close_daily(
    db: Session,
    closing_date: date,
    *,
    user_id: int | None = None,
    notes: str | None = None,
) -> tuple[HotelDailyClosing, object]:
    if not finance_enabled(db):
        raise FinanceError("وحدة المالية الفندقية غير مفعّلة.")
    existing = db.scalar(
        select(HotelDailyClosing).where(HotelDailyClosing.closing_date == closing_date)
    )
    if existing is not None:
        raise FinanceError("تم إقفال هذا اليوم مسبقاً.")

    from app.datetime_local import local_day_start_utc
    from modules.hotel.daily_close_gl import run_hotel_daily_close_gl
    from modules.hotel.revenue_stats import hotel_cash_collected

    day_start = local_day_start_utc(closing_date)
    day_end = local_day_start_utc(closing_date + timedelta(days=1))

    bookings_new = db.scalar(
        select(func.count())
        .select_from(HotelBooking)
        .where(func.date(HotelBooking.created_at) == closing_date)
    ) or 0
    check_ins = db.scalar(
        select(func.count())
        .select_from(HotelBooking)
        .where(
            HotelBooking.booking_status == BookingStatus.CHECKED_IN,
            func.date(HotelBooking.checked_in_at) == closing_date,
        )
    ) or 0
    check_outs = db.scalar(
        select(func.count())
        .select_from(HotelBooking)
        .where(
            HotelBooking.booking_status == BookingStatus.CHECKED_OUT,
            func.date(HotelBooking.checked_out_at) == closing_date,
        )
    ) or 0
    cancellations = db.scalar(
        select(func.count())
        .select_from(HotelBooking)
        .where(
            HotelBooking.booking_status == BookingStatus.CANCELLED,
            func.date(HotelBooking.updated_at) == closing_date,
        )
    ) or 0
    no_shows = db.scalar(
        select(func.count())
        .select_from(HotelBooking)
        .where(
            HotelBooking.booking_status == BookingStatus.NO_SHOW,
            func.date(HotelBooking.updated_at) == closing_date,
        )
    ) or 0

    revenue = hotel_cash_collected(db, day_start, day_end)
    deposits = db.scalar(
        select(func.coalesce(func.sum(HotelBookingPayment.amount), 0)).where(
            HotelBookingPayment.created_at >= day_start,
            HotelBookingPayment.created_at < day_end,
            HotelBookingPayment.is_deposit.is_(True),
        )
    )
    deposits_total = Decimal(str(deposits or 0)).quantize(Decimal("0.001"))

    gl_result = run_hotel_daily_close_gl(db, closing_date, operational_net=revenue)

    row = HotelDailyClosing(
        closing_date=closing_date,
        bookings_new=int(bookings_new),
        check_ins=int(check_ins),
        check_outs=int(check_outs),
        cancellations=int(cancellations),
        no_shows=int(no_shows),
        revenue_total=revenue.quantize(Decimal("0.001")),
        deposits_total=deposits_total,
        notes=(notes or "").strip() or None,
        closed_by_id=user_id,
        gl_backfilled_payments=int(gl_result.backfilled_payments),
        gl_backfilled_refunds=int(gl_result.backfilled_refunds),
        gl_operational_net=gl_result.operational_net,
        gl_revenue_net=gl_result.gl_revenue_net,
        gl_gap=gl_result.gl_gap,
    )
    db.add(row)
    db.flush()
    return row, gl_result


def process_auto_daily_close(db: Session, *, max_days: int = 7) -> int:
    """إقفال تلقائي لأيام تقويمية اكتملت (أمس وما قبله إن فُقدت).

    اليوم الحالي لا يُقفل أثناء سريانه — كل 24 ساعة = يوم كامل بعد منتصف الليل المحلي.
    يعيد عدد الأيام التي أُقفلت في هذه الدورة.
    """
    from app.datetime_local import now_local
    from modules.settings.service import get_bool, get_setting, set_setting

    if not finance_enabled(db):
        return 0
    if not get_bool(db, "hotel_daily_close_auto_enabled", True):
        return 0

    today = now_local().date()
    # لا نُقفل «اليوم» قبل انتهائه
    newest_target = today - timedelta(days=1)
    oldest_target = today - timedelta(days=max(1, min(31, int(max_days))))

    closed_n = 0
    last_ok: date | None = None
    d = oldest_target
    while d <= newest_target:
        existing = db.scalar(
            select(HotelDailyClosing).where(HotelDailyClosing.closing_date == d)
        )
        if existing is None:
            try:
                close_daily(
                    db,
                    d,
                    user_id=None,
                    notes="إقفال تلقائي — نهاية اليوم التقويمي",
                )
                closed_n += 1
                last_ok = d
            except FinanceError:
                # يوم مقفول بالفعل أو وحدة غير جاهزة — نتجاوز
                pass
            except Exception:  # noqa: BLE001
                # لا نوقف الدورة بالكامل؛ نعيد المحاولة لاحقاً
                break
        else:
            last_ok = d
        d += timedelta(days=1)

    if last_ok is not None:
        set_setting(db, "hotel_daily_close_last_date", last_ok.isoformat())
    elif (get_setting(db, "hotel_daily_close_last_date") or "") != newest_target.isoformat():
        # كل الأيام حتى أمس موجودة مسبقاً
        if db.scalar(
            select(HotelDailyClosing).where(
                HotelDailyClosing.closing_date == newest_target
            )
        ):
            set_setting(db, "hotel_daily_close_last_date", newest_target.isoformat())

    return closed_n


def occupancy_stats(db: Session, *, on_date: date, property_id: int = 1) -> dict:
    total_rooms = db.scalar(
        select(func.count())
        .select_from(HotelRoom)
        .where(HotelRoom.is_active.is_(True), HotelRoom.property_id == property_id)
    ) or 0
    occupied = db.scalar(
        select(func.count())
        .select_from(HotelBooking)
        .where(
            HotelBooking.property_id == property_id,
            HotelBooking.booking_status == BookingStatus.CHECKED_IN,
            HotelBooking.check_in <= on_date,
            HotelBooking.check_out > on_date,
        )
    ) or 0
    rate = (occupied / total_rooms * 100) if total_rooms else 0
    return {
        "total_rooms": int(total_rooms),
        "occupied": int(occupied),
        "occupancy_rate": round(rate, 1),
    }
