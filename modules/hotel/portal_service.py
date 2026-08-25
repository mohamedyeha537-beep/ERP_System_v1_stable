"""بوابة الزبون — حجز وطلبات أثناء الإقامة."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from modules.hotel.booking_models import BookingSource, BookingStatus
from modules.hotel.booking_service import (
    BookingError,
    create_booking,
    get_booking_by_token,
    record_payment,
)
from modules.hotel.availability import search_availability
from modules.platform.module_registry import HOTEL_PORTAL, is_module_enabled
from modules.settings.service import get_bool


def portal_enabled(db: Session) -> bool:
    if not is_module_enabled(db, HOTEL_PORTAL):
        return False
    return get_bool(db, "hotel_portal_enabled", False)


def portal_search(
    db: Session,
    *,
    check_in: date,
    check_out: date,
    adults: int = 1,
    children: int = 0,
):
    if not portal_enabled(db):
        raise BookingError("بوابة الحجز غير متاحة.")
    return search_availability(
        db, check_in=check_in, check_out=check_out, adults=adults, children=children
    )


def portal_create_booking(
    db: Session,
    *,
    guest_name: str,
    guest_phone: str,
    guest_email: str | None,
    check_in: date,
    check_out: date,
    room_type_id: int,
    adults: int = 1,
    children: int = 0,
) -> tuple[object, str]:
    if not portal_enabled(db):
        raise BookingError("بوابة الحجز غير متاحة.")
    from modules.customers.service import get_or_create_by_phone

    from modules.customers.models import CustomerBusinessDomain

    cust = get_or_create_by_phone(
        db,
        phone=guest_phone,
        name=guest_name,
        business_domain=CustomerBusinessDomain.HOTEL,
    )
    booking = create_booking(
        db,
        guest_name=guest_name,
        guest_phone=guest_phone,
        guest_email=guest_email,
        check_in=check_in,
        check_out=check_out,
        room_type_id=room_type_id,
        adults=adults,
        children=children,
        source=BookingSource.PORTAL,
        customer_id=cust.id if cust else None,
        auto_confirm=True,
    )
    return booking, booking.access_token


def portal_record_deposit(
    db: Session,
    booking_id: int,
    *,
    amount: Decimal,
    payment_method_id: int | None,
) -> None:
    record_payment(
        db,
        booking_id,
        amount=amount,
        payment_method_id=payment_method_id,
        is_deposit=True,
        note="عربون — بوابة الزبون",
    )


def portal_get_session(db: Session, token: str):
    booking = get_booking_by_token(db, token)
    if booking is None:
        raise BookingError("رابط غير صالح.")
    return booking


def portal_can_order(db: Session, token: str) -> bool:
    booking = get_booking_by_token(db, token)
    return booking is not None and booking.booking_status == BookingStatus.CHECKED_IN


def portal_create_room_order(
    db: Session,
    *,
    token: str,
    product_id: int,
    quantity: Decimal,
    user_id: int | None = None,
) -> int:
    """إنشاء Sale ROOM وقيده على الشقة — يُرجع sale_id."""
    from modules.catalog.models import Product
    from modules.hotel.service import open_room_charge
    from modules.sales.models import SaleContext, SaleSource
    from modules.sales.service import (
        SalesError,
        add_line_to_sale,
        complete_sale,
        create_draft_sale,
        send_draft_to_kitchen,
    )

    booking = get_booking_by_token(db, token)
    if booking is None:
        raise BookingError("رابط غير صالح.")
    if booking.booking_status != BookingStatus.CHECKED_IN:
        raise BookingError("الطلب متاح أثناء الإقامة فقط (بعد Check-in).")
    if booking.room_id is None:
        raise BookingError("لم تُعيَّن غرفة بعد.")

    product = db.get(Product, product_id)
    if product is None:
        raise BookingError("المنتج غير موجود.")

    sale = create_draft_sale(db, user_id=user_id, source=SaleSource.ONLINE)
    sale.context_type = SaleContext.ROOM
    sale.booking_id = booking.id
    add_line_to_sale(db, sale.id, product_id, quantity)
    try:
        send_draft_to_kitchen(db, sale.id, user_id, room_session_ok=True)
    except SalesError:
        pass
    complete_sale(db, sale.id, user_id, pos_shift_id=None)
    open_room_charge(
        db,
        sale_id=sale.id,
        room_id=booking.room_id,
        guest_name=booking.guest_name,
        note="طلب بوابة الزبون",
        user_id=user_id,
        booking_id=booking.id,
    )
    return sale.id
