from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.hotel.booking_models import BookingStatus, HotelBooking
from modules.notifications.events import (
    HOTEL_BALANCE_CLAIM,
    HOTEL_BOOKING_CONFIRMED,
    HOTEL_BOOKING_CREATED,
    HOTEL_CHECKOUT_REMINDER,
    HOTEL_NIGHT_PAYMENT_DUE,
    HOTEL_PAYMENT_RECEIVED,
    HOTEL_ROOM_CLEANING,
    HOTEL_ROOM_MAINTENANCE,
    HOTEL_UNPAID_SERVICE_ADDED,
)
from modules.notifications.service import emit_event_safe
from modules.settings.service import get_public_base_url, get_setting


def _fmt(value: Decimal | int | str | None) -> str:
    """مبلغ بصيغة 1,380.000"""
    q = Decimal(str(value or 0)).quantize(Decimal("0.001"))
    return f"{q:,.3f}"


def _fmt_date(value) -> str:
    if value is None:
        return ""
    try:
        return value.strftime("%d-%m-%Y")
    except Exception:
        return str(value)


def _public_base_url(db: Session) -> str:
    return get_public_base_url(db)


def _booking_url(db: Session, booking: HotelBooking) -> str:
    base = _public_base_url(db)
    if not base:
        return ""
    return f"{base}/stay/my/{booking.access_token}"


def emit_hotel_online_booking_request(db: Session, booking: HotelBooking) -> None:
    from modules.notifications.events import HOTEL_ONLINE_BOOKING_REQUEST

    payload = _booking_payload(
        db,
        booking,
        booking_source=booking.source.value if booking.source else "",
        admin_booking_url=f"{_public_base_url(db)}/admin/hotel/bookings/{booking.id}",
    )
    # مسار واحد فقط عبر محرك الإشعارات — لا نُكرّر بإرسال يدوي ثانٍ
    emit_event_safe(
        db,
        event_key=HOTEL_ONLINE_BOOKING_REQUEST,
        source_type="hotel_booking",
        source_id=booking.id,
        payload=payload,
    )


def _folio_lines_summary(folio) -> str:
    """ملخص بنود الحساب كنص واتساب — مطابق لما يظهر في الإيصال."""
    lines_out: list[str] = []
    for line in list(getattr(folio, "lines", None) or [])[:12]:
        desc = (getattr(line, "description", None) or "").strip() or "بند"
        amt = _fmt(getattr(line, "amount", 0))
        lines_out.append(f"• {desc}: {amt} د.ل")
    return "\n".join(lines_out)


def _booking_payload(db: Session, booking: HotelBooking, **extra: Any) -> dict[str, Any]:
    from modules.branding.service import hotel_display_name
    from modules.hotel.folio import build_folio

    folio = build_folio(db, booking.id)
    room_name = ""
    if booking.room is not None:
        room_name = booking.room.number or booking.room.name_ar or ""
    booking_url = _booking_url(db, booking)
    folio_lines = _folio_lines_summary(folio)
    payload: dict[str, Any] = {
        "booking_id": booking.id,
        "booking_reference": booking.reference,
        "customer_name": booking.guest_name or "ضيفنا",
        "guest_name": booking.guest_name or "",
        "phone": booking.guest_phone or "",
        "guest_phone": booking.guest_phone or "",
        "room_name": room_name,
        "room_type": booking.room_type.name_ar if booking.room_type else "",
        "check_in_date": _fmt_date(booking.check_in),
        "check_out_date": _fmt_date(booking.check_out),
        "nights": booking.nights,
        "nightly_rate": _fmt(booking.nightly_rate),
        "booking_total": _fmt(folio.total),
        "paid_amount": _fmt(folio.paid),
        "balance_due": _fmt(folio.balance),
        "folio_lines": folio_lines,
        "folio_summary": (
            f"الإجمالي: {_fmt(folio.total)} د.ل\n"
            f"المدفوع: {_fmt(folio.paid)} د.ل\n"
            f"المتبقي: {_fmt(folio.balance)} د.ل"
        ),
        "booking_url": booking_url,
        "invoice_url": booking_url,
        "store_name": hotel_display_name(db),
    }
    payload.update(extra)
    return payload


def emit_hotel_booking_created(db: Session, booking: HotelBooking) -> None:
    emit_event_safe(
        db,
        event_key=HOTEL_BOOKING_CREATED,
        source_type="hotel_booking",
        source_id=booking.id,
        payload=_booking_payload(db, booking),
    )


def emit_hotel_booking_confirmed(db: Session, booking: HotelBooking) -> None:
    emit_event_safe(
        db,
        event_key=HOTEL_BOOKING_CONFIRMED,
        source_type="hotel_booking",
        source_id=booking.id,
        payload=_booking_payload(db, booking),
    )


def emit_hotel_unpaid_service_added(
    db: Session,
    booking: HotelBooking,
    *,
    service_name: str,
    service_amount: Decimal,
) -> None:
    emit_event_safe(
        db,
        event_key=HOTEL_UNPAID_SERVICE_ADDED,
        source_type="hotel_booking_service",
        source_id=booking.id,
        payload=_booking_payload(
            db,
            booking,
            service_name=service_name,
            service_amount=_fmt(service_amount),
        ),
    )


def emit_hotel_payment_received(
    db: Session,
    booking: HotelBooking,
    *,
    payment_amount: Decimal,
    payment_method: str = "",
    payment_id: int | None = None,
    is_deposit: bool = False,
) -> None:
    pay_label = "عربون" if is_deposit else "إيصال قبض"
    emit_event_safe(
        db,
        event_key=HOTEL_PAYMENT_RECEIVED,
        source_type="hotel_booking_payment",
        source_id=int(payment_id or 0) or booking.id,
        payload=_booking_payload(
            db,
            booking,
            payment_id=int(payment_id or 0),
            payment_amount=_fmt(payment_amount),
            payment_method=payment_method or "—",
            payment_label=pay_label,
            doc_title=pay_label,
        ),
    )


def emit_hotel_balance_claim(
    db: Session,
    booking: HotelBooking,
    *,
    claim_note: str | None = None,
) -> None:
    """مطالبة واتساب برصيد مستحق — من صفحة الحجز أو الجدولة اليومية."""
    note = (claim_note or "").strip()
    note_line = f"ملاحظة الاستقبال: {note}\n" if note else ""
    phone = (booking.guest_phone or "").strip()
    if not phone:
        phone = (getattr(booking, "company_contact_phone", None) or "").strip()
    payload = _booking_payload(
        db,
        booking,
        claim_note=note,
        claim_note_line=note_line,
        phone=phone,
        guest_phone=phone,
    )
    emit_event_safe(
        db,
        event_key=HOTEL_BALANCE_CLAIM,
        source_type="hotel_booking",
        source_id=booking.id,
        payload=payload,
    )


def emit_hotel_daily_guest_reminders(db: Session) -> int:
    from app.datetime_local import now_local
    from modules.hotel.folio import booking_balance_due
    from modules.hotel.follow_up import clear_claim_wa_if_settled

    today = now_local().date()
    tomorrow = today + timedelta(days=1)
    count = 0
    rows = list(
        db.scalars(
            select(HotelBooking).where(
                HotelBooking.booking_status == BookingStatus.CHECKED_IN,
            )
        ).all()
    )
    for booking in rows:
        phone = (booking.guest_phone or "").strip() or (
            getattr(booking, "company_contact_phone", None) or ""
        ).strip()

        # تذكير المغادرة قبل 24 ساعة + يوم المغادرة
        if booking.check_out in (today, tomorrow) and phone:
            emit_event_safe(
                db,
                event_key=HOTEL_CHECKOUT_REMINDER,
                source_type="hotel_booking",
                source_id=booking.id,
                payload=_booking_payload(
                    db,
                    booking,
                    phone=phone,
                    guest_phone=phone,
                    departure_timing="today" if booking.check_out == today else "24h",
                ),
            )
            count += 1

        balance = booking_balance_due(db, booking.id)
        if clear_claim_wa_if_settled(db, booking):
            continue

        # مطالبة واتساب مفعّلة من صفحة الحجز — يومياً حتى السداد
        if getattr(booking, "claim_wa_until_paid", False) and phone:
            rem = getattr(booking, "follow_up_at", None)
            if rem is None or rem <= today:
                if balance > Decimal("0.001"):
                    emit_hotel_balance_claim(
                        db,
                        booking,
                        claim_note=getattr(booking, "follow_up_note", None),
                    )
                    count += 1
            continue

        # تنبيه تلقائي لمنتصف الإقامة عند وجود متبقٍ
        if booking.check_in < today < booking.check_out and phone:
            if balance > Decimal("0.001"):
                emit_event_safe(
                    db,
                    event_key=HOTEL_NIGHT_PAYMENT_DUE,
                    source_type="hotel_booking",
                    source_id=booking.id,
                    payload=_booking_payload(
                        db, booking, phone=phone, guest_phone=phone
                    ),
                )
                count += 1
    return count


def emit_hotel_room_cleaning(
    db: Session,
    room,
    *,
    cleaning_phone: str = "",
    cleaning_staff_name: str = "",
    note: str = "",
    employee_id: int | None = None,
    reported_by: str = "",
    base_url: str = "",
) -> None:
    from modules.branding.service import hotel_display_name
    from modules.hotel.dashboard import room_display_name
    from modules.hotel.housekeeping_links import (
        housekeeping_confirm_button_id,
        housekeeping_done_url,
    )

    store = hotel_display_name(db)
    room_name = room_display_name(room)
    detail = (note or "").strip()
    note_line = f"ملاحظات: {detail}\n" if detail else ""
    task_token = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    done_url = housekeeping_done_url(db, room.id, base_url=base_url)
    confirm_button_id = housekeeping_confirm_button_id(
        db, room.id, base_url=base_url
    )
    # زر الرابط (https…) يفتح صفحة التأكيد مباشرة — لا يحتاج webhook TextMeBot.
    # إن لم يتوفر رابط عام يبقى الرد السريع housekeeping_done:{id}.
    done_link_line = (
        f"أو افتح الرابط لتأكيد الانتهاء:\n{done_url}\n"
        if done_url
        else ""
    )
    emit_event_safe(
        db,
        event_key=HOTEL_ROOM_CLEANING,
        source_type="hotel_room",
        source_id=room.id,
        payload={
            "store_name": store,
            "room_id": room.id,
            "room_name": room_name,
            "room_number": room.number or "",
            "floor": room.floor or "",
            "cleaning_phone": (cleaning_phone or "").strip(),
            "cleaning_staff_name": (cleaning_staff_name or "").strip(),
            "phone": (cleaning_phone or "").strip(),
            "note": detail,
            "note_line": note_line,
            "employee_id": employee_id,
            "reported_by": (reported_by or "").strip(),
            "task_token": task_token,
            "done_url": done_url,
            "confirm_button_id": confirm_button_id,
            "done_link_line": done_link_line,
        },
    )


def emit_hotel_room_maintenance(
    db: Session,
    room,
    *,
    issue_type: str = "",
    issue_details: str = "",
    maintenance_phone: str = "",
    maintenance_staff_name: str = "",
    reported_by: str = "",
    employee_id: int | None = None,
) -> None:
    from modules.branding.service import hotel_display_name
    from modules.hotel.dashboard import room_display_name
    from modules.hotel.maintenance import issue_label

    store = hotel_display_name(db)
    label = issue_label(issue_type)
    details = (issue_details or "").strip() or "—"
    room_name = room_display_name(room)
    reporter = (reported_by or "").strip()
    reported_by_line = f"بلّغ: {reporter}\n" if reporter else ""
    emit_event_safe(
        db,
        event_key=HOTEL_ROOM_MAINTENANCE,
        source_type="hotel_room",
        source_id=room.id,
        payload={
            "store_name": store,
            "room_id": room.id,
            "room_name": room_name,
            "room_number": room.number or "",
            "floor": room.floor or "",
            "issue_type": issue_type or "",
            "issue_label": label,
            "issue_details": details,
            "maintenance_phone": (maintenance_phone or "").strip(),
            "maintenance_staff_name": (maintenance_staff_name or "").strip(),
            "reported_by": reporter,
            "reported_by_line": reported_by_line,
            "employee_id": employee_id,
        },
    )
