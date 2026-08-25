"""تقرير الجهات الأمنية — إدارة الجهات والإرسال."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.hotel.booking_models import (
    BookingStatus,
    HotelBooking,
    HotelBookingGuest,
    HotelSecurityAuthority,
    SecurityContactChannel,
    SecurityGuestScope,
)
from modules.messaging.models import MessageChannel, MessageOutboxStatus
from modules.messaging.outbox import enqueue_message, send_outbox_item_now
from modules.messaging.phone_utils import (
    is_whatsapp_group_id,
    normalize_whatsapp_recipient,
)
from modules.settings.service import get_bool, get_int, get_setting

_LIBYAN_RE = re.compile(
    r"ليبيا|ليبي|ليبية|libya|libyan|ly\b",
    re.IGNORECASE,
)

CONTACT_CHANNEL_LABELS = {
    SecurityContactChannel.WHATSAPP: "واتساب",
    SecurityContactChannel.EMAIL: "بريد إلكتروني",
}

GUEST_SCOPE_LABELS = {
    SecurityGuestScope.FOREIGN: "أجانب فقط",
    SecurityGuestScope.LIBYAN: "ليبيون فقط",
    SecurityGuestScope.BOTH: "أجانب وليبيون",
}


class SecurityReportError(Exception):
    pass


def contact_channel_label(value: SecurityContactChannel | str) -> str:
    try:
        ch = SecurityContactChannel(value)
    except ValueError:
        return str(value or "—")
    return CONTACT_CHANNEL_LABELS.get(ch, ch.value)


def guest_scope_label(value: SecurityGuestScope | str) -> str:
    try:
        sc = SecurityGuestScope(value)
    except ValueError:
        return str(value or "—")
    return GUEST_SCOPE_LABELS.get(sc, sc.value)


def is_libyan_nationality(nationality: str | None) -> bool:
    raw = (nationality or "").strip()
    if not raw:
        return False
    return bool(_LIBYAN_RE.search(raw))


def _guest_nationality(guest: HotelBookingGuest, booking: HotelBooking) -> str | None:
    return (guest.nationality or booking.guest_nationality or "").strip() or None


def _booking_matches_scope(booking: HotelBooking, scope: SecurityGuestScope) -> bool:
    guests = [g for g in (booking.guests or []) if (g.full_name or "").strip()]
    if not guests:
        nat = booking.guest_nationality
        if scope == SecurityGuestScope.LIBYAN:
            return is_libyan_nationality(nat)
        if scope == SecurityGuestScope.FOREIGN:
            return bool(nat) and not is_libyan_nationality(nat)
        return True
    for g in guests:
        nat = _guest_nationality(g, booking)
        if scope == SecurityGuestScope.LIBYAN and is_libyan_nationality(nat):
            return True
        if scope == SecurityGuestScope.FOREIGN and nat and not is_libyan_nationality(nat):
            return True
    return False


def filter_bookings_by_scope(
    bookings: list[HotelBooking],
    scope: SecurityGuestScope | str | None,
) -> list[HotelBooking]:
    if scope in (None, "", SecurityGuestScope.BOTH, SecurityGuestScope.BOTH.value):
        return list(bookings)
    try:
        sc = SecurityGuestScope(scope)
    except ValueError:
        return list(bookings)
    return [b for b in bookings if _booking_matches_scope(b, sc)]


@dataclass
class SecurityGuestRow:
    booking: HotelBooking
    guest: HotelBookingGuest
    row_index: int


def _guest_matches_scope(
    guest: HotelBookingGuest,
    booking: HotelBooking,
    scope: SecurityGuestScope | str | None,
) -> bool:
    if scope in (None, "", SecurityGuestScope.BOTH, SecurityGuestScope.BOTH.value):
        return True
    try:
        sc = SecurityGuestScope(scope)
    except ValueError:
        return True
    nat = _guest_nationality(guest, booking)
    if sc == SecurityGuestScope.LIBYAN:
        return is_libyan_nationality(nat)
    if sc == SecurityGuestScope.FOREIGN:
        return bool(nat) and not is_libyan_nationality(nat)
    return True


def iter_security_guest_rows(
    bookings: list[HotelBooking],
    *,
    scope: SecurityGuestScope | str | None = None,
) -> list[SecurityGuestRow]:
    """صفوف النزلاء — مع فلترة حسب نطاق الجهة (أجانب / ليبيون / الكل)."""
    rows: list[SecurityGuestRow] = []
    idx = 1
    for booking in bookings:
        guests = [g for g in (booking.guests or []) if (g.full_name or "").strip()]
        if not guests:
            guests = [
                HotelBookingGuest(
                    full_name=booking.guest_name,
                    id_number=booking.guest_id_number,
                    id_type=booking.guest_id_type,
                    nationality=booking.guest_nationality,
                    address=booking.guest_address,
                    phone=booking.guest_phone,
                    is_primary=True,
                )
            ]
        for guest in guests:
            if not _guest_matches_scope(guest, booking, scope):
                continue
            rows.append(SecurityGuestRow(booking=booking, guest=guest, row_index=idx))
            idx += 1
    return rows


SECURITY_EXPORT_HEADERS = (
    "م",
    "المرجع",
    "اسم النزيل",
    "رقم الهوية",
    "نوع الهوية",
    "الجنسية",
    "قادم من",
    "الوصول",
    "الخروج",
    "الشقة",
    "الهاتف",
)


def security_export_table(
    bookings: list[HotelBooking],
    *,
    scope: SecurityGuestScope | str | None = None,
) -> tuple[tuple[str, ...], list[list]]:
    guest_rows = iter_security_guest_rows(bookings, scope=scope)
    rows: list[list] = []
    for row in guest_rows:
        g = row.guest
        b = row.booking
        rows.append(
            [
                row.row_index,
                b.reference,
                g.full_name,
                g.id_number or "",
                g.id_type or "",
                _guest_nationality(g, b) or "",
                g.address or b.guest_address or "",
                b.check_in.isoformat() if b.check_in else "",
                b.check_out.isoformat() if b.check_out else "",
                _room_label(b),
                g.phone or b.guest_phone or "",
            ]
        )
    return SECURITY_EXPORT_HEADERS, rows


def list_security_authorities(
    db: Session, *, only_active: bool = False
) -> list[HotelSecurityAuthority]:
    q = select(HotelSecurityAuthority).order_by(
        HotelSecurityAuthority.sort_order.asc(),
        HotelSecurityAuthority.name_ar.asc(),
        HotelSecurityAuthority.id.asc(),
    )
    if only_active:
        q = q.where(HotelSecurityAuthority.is_active.is_(True))
    return list(db.scalars(q).all())


def get_security_authority(db: Session, authority_id: int) -> HotelSecurityAuthority | None:
    return db.get(HotelSecurityAuthority, authority_id)


def save_security_authority(
    db: Session,
    *,
    authority_id: int | None,
    name_ar: str,
    contact_channel: str,
    contact_value: str,
    guest_scope: str,
    notes: str | None = None,
    is_active: bool = True,
    sort_order: int = 0,
) -> HotelSecurityAuthority:
    name = (name_ar or "").strip()
    if not name:
        raise SecurityReportError("اسم الجهة مطلوب")
    try:
        channel = SecurityContactChannel((contact_channel or "").strip().upper())
    except ValueError as exc:
        raise SecurityReportError("قناة التواصل غير صالحة") from exc
    try:
        scope = SecurityGuestScope((guest_scope or "").strip().upper())
    except ValueError as exc:
        raise SecurityReportError("نطاق النزلاء غير صالح") from exc
    contact = (contact_value or "").strip()
    if not contact:
        raise SecurityReportError("بيانات التواصل مطلوبة")
    if channel == SecurityContactChannel.EMAIL and "@" not in contact:
        raise SecurityReportError("أدخل بريداً إلكترونياً صالحاً")
    if channel == SecurityContactChannel.WHATSAPP:
        cc = (get_setting(db, "messaging_default_country_code", "218") or "218").strip()
        # رقم فرد (+218...) أو معرّف جروب (12036...@g.us)
        contact = normalize_whatsapp_recipient(contact, country_code=cc)
        if is_whatsapp_group_id(contact):
            if len(contact) < 15:
                raise SecurityReportError("معرّف جروب واتساب غير صالح")
        elif len(contact) < 8:
            raise SecurityReportError("رقم واتساب أو معرّف الجروب غير صالح")

    row = db.get(HotelSecurityAuthority, authority_id) if authority_id else None
    if row is None:
        row = HotelSecurityAuthority()
        db.add(row)
    row.name_ar = name
    row.contact_channel = channel
    row.contact_value = contact
    row.guest_scope = scope
    row.notes = (notes or "").strip() or None
    row.is_active = bool(is_active)
    row.sort_order = int(sort_order or 0)
    db.flush()
    return row


def delete_security_authority(db: Session, authority_id: int) -> None:
    row = db.get(HotelSecurityAuthority, authority_id)
    if row is None:
        raise SecurityReportError("الجهة غير موجودة")
    db.delete(row)


def fetch_security_report_bookings(
    db: Session,
    *,
    start: date,
    end: date,
) -> list[HotelBooking]:
    return list(
        db.scalars(
            select(HotelBooking)
            .where(
                HotelBooking.check_in <= end,
                HotelBooking.check_out >= start,
                HotelBooking.booking_status.notin_(
                    (BookingStatus.CANCELLED, BookingStatus.NO_SHOW, BookingStatus.LATE_CANCELLATION)
                ),
            )
            .order_by(
                HotelBooking.check_in.asc(),
                HotelBooking.room_id.asc(),
                HotelBooking.id.asc(),
            )
        ).all()
    )


def _room_label(booking: HotelBooking) -> str:
    room = booking.room
    if room is None:
        return "—"
    return room.name_ar or f"شقة {room.number}"


def format_report_text(
    bookings: list[HotelBooking],
    *,
    from_date: date,
    to_date: date,
    authority: HotelSecurityAuthority | None = None,
    store_name: str = "الفندق",
) -> str:
    lines = [f"تقرير الجهات الأمنية — {store_name}"]
    if authority is not None:
        lines.append(f"الجهة: {authority.name_ar}")
        lines.append(f"نطاق النزلاء: {guest_scope_label(authority.guest_scope)}")
    lines.append(f"الفترة: {from_date} إلى {to_date}")
    scope = authority.guest_scope if authority is not None else None
    guest_rows = iter_security_guest_rows(bookings, scope=scope)
    lines.append(f"عدد الحجوزات: {len(bookings)}")
    lines.append(f"عدد النزلاء: {len(guest_rows)}")
    lines.append("")
    if not guest_rows:
        lines.append("لا توجد حجوزات في هذه الفترة.")
        return "\n".join(lines)
    for row in guest_rows:
        g = row.guest
        b = row.booking
        lines.append(
            f"{row.row_index}. {g.full_name} | {g.id_number or '—'} | "
            f"{_guest_nationality(g, b) or '—'} | {b.check_in} → {b.check_out} | "
            f"{_room_label(b)} | {g.phone or b.guest_phone or '—'}"
        )
    return "\n".join(lines)


def _send_email_report(db: Session, *, recipient: str, subject: str, body: str) -> None:
    from modules.alerts.service import _send_email, _looks_like_real_endpoint

    smtp_host = (get_setting(db, "smtp_host") or "").strip()
    if not smtp_host or not _looks_like_real_endpoint(smtp_host):
        raise SecurityReportError("إعدادات SMTP غير مكتملة — راجع صفحة التنبيهات")
    sender = (get_setting(db, "smtp_from") or "").strip() or (
        get_setting(db, "smtp_user") or ""
    ).strip()
    _send_email(
        subject=subject,
        body=body,
        smtp_host=smtp_host,
        smtp_port=get_int(db, "smtp_port", 587),
        smtp_user=(get_setting(db, "smtp_user") or "").strip(),
        smtp_password=get_setting(db, "smtp_password") or "",
        sender=sender,
        recipient=recipient,
        use_tls=get_bool(db, "smtp_use_tls", True),
    )


def _send_whatsapp_report(db: Session, *, phone: str, body: str) -> None:
    if not get_bool(db, "messaging_enabled", False):
        raise SecurityReportError("المراسلة غير مفعّلة — راجع إعدادات المراسلة")
    row = enqueue_message(
        db,
        body=body[:4000],
        channel=MessageChannel.WHATSAPP.value,
        phone=phone,
        event_type="hotel_security_report",
        meta={"kind": "security_report"},
    )
    db.flush()
    row = send_outbox_item_now(db, row)
    if row.status != MessageOutboxStatus.SENT.value:
        err = (row.error_message or "فشل الإرسال").strip()
        raise SecurityReportError(err)


def send_security_report(
    db: Session,
    *,
    authority: HotelSecurityAuthority,
    bookings: list[HotelBooking],
    from_date: date,
    to_date: date,
) -> None:
    from modules.branding.service import hotel_display_name

    store_name = hotel_display_name(db)
    body = format_report_text(
        bookings,
        from_date=from_date,
        to_date=to_date,
        authority=authority,
        store_name=store_name,
    )
    subject = f"[{store_name}] تقرير الجهات الأمنية ({from_date} — {to_date})"
    if authority.contact_channel == SecurityContactChannel.EMAIL:
        _send_email_report(db, recipient=authority.contact_value, subject=subject, body=body)
        return
    _send_whatsapp_report(db, phone=authority.contact_value, body=body)
