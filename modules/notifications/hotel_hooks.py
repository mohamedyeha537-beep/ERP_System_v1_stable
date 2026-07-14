from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.hotel.booking_models import BookingStatus, HotelBooking
from modules.notifications.events import (
    HOTEL_BOOKING_CONFIRMED,
    HOTEL_BOOKING_CREATED,
    HOTEL_CHECKOUT_REMINDER,
    HOTEL_NIGHT_PAYMENT_DUE,
    HOTEL_PAYMENT_RECEIVED,
    HOTEL_ROOM_MAINTENANCE,
    HOTEL_UNPAID_SERVICE_ADDED,
)
from modules.notifications.service import emit_event_safe
from modules.settings.service import get_setting


def _fmt(value: Decimal | int | str | None) -> str:
    return str(Decimal(str(value or 0)).quantize(Decimal("0.001")))


def _public_base_url(db: Session) -> str:
    return (get_setting(db, "public_base_url", "") or "").strip().rstrip("/")


def _booking_url(db: Session, booking: HotelBooking) -> str:
    base = _public_base_url(db)
    if not base:
        return ""
    return f"{base}/stay/my/{booking.access_token}"


def emit_hotel_online_booking_request(db: Session, booking: HotelBooking) -> None:
    from modules.hotel.store_service import staff_notify_phone
    from modules.messaging.models import MessageChannel, MessageOutboxStatus
    from modules.messaging.outbox import enqueue_message, send_outbox_item_now
    from modules.messaging.phone_utils import normalize_whatsapp_phone
    from modules.notifications.events import HOTEL_ONLINE_BOOKING_REQUEST
    from modules.settings.service import get_bool, get_setting

    payload = _booking_payload(
        db,
        booking,
        booking_source=booking.source.value if booking.source else "",
        admin_booking_url=f"{_public_base_url(db)}/admin/hotel/bookings/{booking.id}",
    )
    emit_event_safe(
        db,
        event_key=HOTEL_ONLINE_BOOKING_REQUEST,
        source_type="hotel_booking",
        source_id=booking.id,
        payload=payload,
    )
    if not get_bool(db, "messaging_enabled", False):
        return
    phone = staff_notify_phone(db)
    if not phone:
        phone = (get_setting(db, "hotel_reception_phone", "") or "").strip()
    if not phone:
        return
    cc = (get_setting(db, "messaging_default_country_code", "218") or "218").strip()
    norm = normalize_whatsapp_phone(phone, country_code=cc)
    if len(norm) < 8:
        return
    body = (
        f"🏨 *طلب حجز أونلاين جديد*\n"
        f"المرجع: {booking.reference}\n"
        f"الضيف: {booking.guest_name}\n"
        f"الهاتف: {booking.guest_phone or '—'}\n"
        f"الشقة: {payload.get('room_name') or '—'}\n"
        f"الوصول: {booking.check_in} → {booking.check_out}\n"
        f"الإجمالي: {payload.get('booking_total')} د.ل\n"
        f"راجع النظام وأكمل التواصل مع الزبون."
    )
    try:
        row = enqueue_message(
            db,
            body=body[:4000],
            channel=MessageChannel.WHATSAPP.value,
            phone=norm,
            event_type="hotel.online_booking_request",
            meta={"booking_id": booking.id},
        )
        db.flush()
        send_outbox_item_now(db, row)
    except Exception:  # noqa: BLE001
        pass


def _booking_payload(db: Session, booking: HotelBooking, **extra: Any) -> dict[str, Any]:
    from modules.branding.service import hotel_display_name
    from modules.hotel.folio import build_folio

    folio = build_folio(db, booking.id)
    room_name = ""
    if booking.room is not None:
        room_name = booking.room.number or booking.room.name_ar or ""
    booking_url = _booking_url(db, booking)
    payload: dict[str, Any] = {
        "booking_id": booking.id,
        "booking_reference": booking.reference,
        "customer_name": booking.guest_name or "ضيفنا",
        "guest_name": booking.guest_name or "",
        "phone": booking.guest_phone or "",
        "guest_phone": booking.guest_phone or "",
        "room_name": room_name,
        "room_type": booking.room_type.name_ar if booking.room_type else "",
        "check_in_date": booking.check_in.isoformat() if booking.check_in else "",
        "check_out_date": booking.check_out.isoformat() if booking.check_out else "",
        "nights": booking.nights,
        "nightly_rate": _fmt(booking.nightly_rate),
        "booking_total": _fmt(folio.total),
        "paid_amount": _fmt(folio.paid),
        "balance_due": _fmt(folio.balance),
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
) -> None:
    emit_event_safe(
        db,
        event_key=HOTEL_PAYMENT_RECEIVED,
        source_type="hotel_booking_payment",
        source_id=booking.id,
        payload=_booking_payload(
            db,
            booking,
            payment_amount=_fmt(payment_amount),
            payment_method=payment_method,
        ),
    )


def emit_hotel_daily_guest_reminders(db: Session) -> int:
    today = date.today()
    count = 0
    rows = list(
        db.scalars(
            select(HotelBooking).where(
                HotelBooking.booking_status == BookingStatus.CHECKED_IN,
                HotelBooking.guest_phone.is_not(None),
            )
        ).all()
    )
    for booking in rows:
        if booking.check_out == today:
            emit_event_safe(
                db,
                event_key=HOTEL_CHECKOUT_REMINDER,
                source_type="hotel_booking",
                source_id=booking.id,
                payload=_booking_payload(db, booking),
            )
            count += 1
        if booking.check_in < today < booking.check_out:
            from modules.hotel.folio import booking_balance_due

            balance = booking_balance_due(db, booking.id)
            if balance > Decimal("0.001"):
                emit_event_safe(
                    db,
                    event_key=HOTEL_NIGHT_PAYMENT_DUE,
                    source_type="hotel_booking",
                    source_id=booking.id,
                    payload=_booking_payload(db, booking),
                )
                count += 1
    return count


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
