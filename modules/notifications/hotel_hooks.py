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
    HOTEL_CHECK_IN_WELCOME,
    HOTEL_CHECKOUT_REMINDER,
    HOTEL_LATE_CHECKOUT_CHARGED,
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
    from modules.hotel.notify_routing import notify_payload_overrides

    folio = build_folio(db, booking.id)
    room_name = ""
    if booking.room is not None:
        room_name = booking.room.number or booking.room.name_ar or ""
    booking_url = _booking_url(db, booking)
    folio_lines = _folio_lines_summary(folio)
    route = notify_payload_overrides(db, booking)
    payload: dict[str, Any] = {
        "booking_id": booking.id,
        "booking_reference": booking.reference,
        "customer_name": route.get("customer_name") or booking.guest_name or "ضيفنا",
        "guest_name": route.get("guest_name") or booking.guest_name or "",
        "phone": route.get("phone") or booking.guest_phone or "",
        "guest_phone": route.get("guest_phone") or booking.guest_phone or "",
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
        # تكييف المستلم — تُدمج بعد الأساسيات حتى لا تُستبدل بالخطأ
        **{k: v for k, v in route.items()},
    }
    # قيم افتراضية للعناوين إن غابت (قوالب قديمة)
    gn = payload.get("guest_name") or "ضيفنا"
    payload.setdefault(
        "service_added_headline",
        f"تمت إضافة خدمة على حساب إقامتك يا {gn}:",
    )
    payload.setdefault(
        "claim_headline",
        f"مطالبة سداد يا {gn} — حجز #{booking.reference}",
    )
    payload.setdefault(
        "night_payment_headline",
        f"تنبيه سداد يا {gn}: توجد ليلة/رصيد مستحق على حجزك #{booking.reference}.",
    )
    payload.setdefault("checkout_headline", f"تذكير لطيف يا {gn}")
    payload.setdefault("claim_note_line", "")
    payload.update(extra)
    # رقم الهاتف من extra لا يُفرَّغ
    if not (payload.get("phone") or "").strip():
        payload["phone"] = route.get("phone") or ""
        payload["guest_phone"] = payload["phone"]
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


def emit_hotel_check_in_welcome(db: Session, booking: HotelBooking) -> None:
    """رسالة ترحيب للنزيل بعد التسكين — مرة واحدة لكل حجز (idempotency)."""
    from modules.hotel.checkin_welcome import get_reception_phone
    from modules.hotel.notify_routing import resolve_booking_notify_target

    target = resolve_booking_notify_target(db, booking)
    phone = (target.phone or "").strip()
    if not phone:
        return
    reception = get_reception_phone(db) or "الاستقبال"
    payload = _booking_payload(
        db,
        booking,
        phone=phone,
        guest_phone=phone,
        reception_phone=reception,
        guest_name=target.guest1_name or target.recipient_name,
    )
    emit_event_safe(
        db,
        event_key=HOTEL_CHECK_IN_WELCOME,
        source_type="hotel_booking",
        source_id=booking.id,
        payload=payload,
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
    immediate: bool = False,
) -> tuple[bool, str]:
    """مطالبة واتساب برصيد مستحق — من صفحة الحجز أو الجدولة اليومية.

    يعيد (نجاح, رسالة_خطأ).
    """
    from datetime import datetime, timezone

    from modules.notifications.models import NotificationLog, NotificationLogStatus
    from modules.notifications.service import NotificationService

    note = (claim_note or "").strip()
    note_line = f"ملاحظة الاستقبال: {note}\n" if note else ""
    payload = _booking_payload(
        db,
        booking,
        claim_note=note,
        claim_note_line=note_line,
    )
    phone = (payload.get("phone") or "").strip()
    if not phone:
        return False, "لا يوجد رقم واتساب للمستلم (شركة أو نزيل 1)."
    # لا نربط بموافقة التسويق — مطالبة دين يجب أن تصل
    payload.pop("customer_id", None)
    if immediate:
        payload["_immediate"] = "1"
        payload["claim_nonce"] = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    try:
        event_id = NotificationService.emit_event(
            db,
            event_key=HOTEL_BALANCE_CLAIM,
            source_type="hotel_booking",
            source_id=booking.id,
            payload=payload,
            process_now=True,
        )
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:240]
    if event_id is None:
        return (
            False,
            "محرك الإشعارات معطّل أو حدث المطالبة غير مفعّل — راجع /admin/notifications.",
        )
    if not immediate:
        return True, ""
    log = db.scalar(
        select(NotificationLog)
        .where(NotificationLog.notification_event_id == int(event_id))
        .order_by(NotificationLog.id.desc())
        .limit(1)
    )
    if log is None:
        return False, "لم يُنشأ سجل إرسال (تحقق من قالب/قاعدة المطالبة)."
    if log.status == NotificationLogStatus.SENT.value:
        return True, ""
    if log.status == NotificationLogStatus.SKIPPED.value:
        reason = (log.body_rendered or "تم تخطي الإرسال").strip()
        return False, f"تم تخطي الإرسال: {reason}"
    if log.status == NotificationLogStatus.FAILED.value:
        return False, (log.error_message or "فشل إرسال واتساب").strip()
    return True, ""


def emit_hotel_daily_guest_reminders(db: Session) -> int:
    """تذكيرات مجدولة: مطالبة رصيد + ملخص شركة + تذكير مغادرة."""
    from app.datetime_local import now_local
    from modules.customers.models import Customer, CustomerType
    from modules.hotel.folio import booking_balance_due
    from modules.hotel.follow_up import clear_claim_wa_if_settled
    from modules.hotel.late_checkout import late_checkout_policy
    from modules.hotel.notify_routing import (
        NOTIFY_FREQ_DAILY,
        NOTIFY_FREQ_SUMMARY_DAILY,
        NOTIFY_FREQ_SUMMARY_MONTHLY,
        NOTIFY_FREQ_SUMMARY_WEEKLY,
        booking_notify_frequency,
        build_company_balance_summary_lines,
        company_summary_period_due,
        mark_booking_claim_period_for_freq,
        mark_company_summary_period,
        normalize_notify_freq,
        resolve_booking_notify_target,
        should_send_scheduled_claim,
    )
    from modules.messaging.models import MessageChannel
    from modules.messaging.outbox import enqueue_message
    from modules.settings.service import get_bool

    today = now_local().date()
    tomorrow = today + timedelta(days=1)
    count = 0
    timed_checkout = late_checkout_policy(db).enabled
    daily_claim = get_bool(db, "hotel_daily_balance_claim_enabled", True)
    rows = list(
        db.scalars(
            select(HotelBooking).where(
                HotelBooking.booking_status.in_(
                    (BookingStatus.CHECKED_IN, BookingStatus.CHECKED_OUT)
                ),
            )
        ).all()
    )

    # ── ملخصات شركة مجمّعة حسب التكرار SUMMARY_* ──
    company_ids_done: set[int] = set()
    for booking in rows:
        cid = getattr(booking, "company_customer_id", None)
        if not cid or int(cid) in company_ids_done:
            continue
        company = db.get(Customer, int(cid))
        if company is None:
            continue
        freq = normalize_notify_freq(
            getattr(company, "company_notify_frequency", None)
        )
        if freq not in (
            NOTIFY_FREQ_SUMMARY_DAILY,
            NOTIFY_FREQ_SUMMARY_WEEKLY,
            NOTIFY_FREQ_SUMMARY_MONTHLY,
        ):
            continue
        company_ids_done.add(int(cid))
        if not company_summary_period_due(company, today=today):
            continue
        lines, total = build_company_balance_summary_lines(db, int(cid))
        if total <= Decimal("0.001") or not lines:
            mark_company_summary_period(company, today=today)
            continue
        # هاتف مسؤول: من أول حجز أو ملف الشركة
        phone = (company.phone or "").strip()
        for b in rows:
            if int(getattr(b, "company_customer_id", 0) or 0) != int(cid):
                continue
            t = resolve_booking_notify_target(db, b)
            if t.notify_to == "COMPANY" and t.phone:
                phone = t.phone
                break
        if not phone:
            continue
        bits = [
            f"• حجز #{ln['reference']}"
            + (f" شقة {ln['room']}" if ln["room"] else "")
            + f": {_fmt(ln['balance'])} د.ل"
            for ln in lines[:20]
        ]
        body = (
            f"ملخص مديونية شركتكم «{(company.company_name or company.name or '').strip()}»\n"
            f"عدد الحجوزات ذات المتبقي: {len(lines)}\n"
            + "\n".join(bits)
            + f"\nالإجمالي المستحق: {_fmt(total)} د.ل\n"
            "يرجى التسديد لدى الاستقبال."
        )
        try:
            enqueue_message(
                db,
                body=body[:4000],
                channel=MessageChannel.WHATSAPP.value,
                phone=phone,
                customer_id=int(cid),
                event_type="hotel.company_balance_summary",
                meta={"kind": "company_summary", "company_id": int(cid)},
            )
            mark_company_summary_period(company, today=today)
            count += 1
        except Exception:  # noqa: BLE001
            pass

    for booking in rows:
        target = resolve_booking_notify_target(db, booking)
        phone = (target.phone or "").strip()
        is_in = booking.booking_status == BookingStatus.CHECKED_IN

        if (
            is_in
            and not timed_checkout
            and booking.check_out in (today, tomorrow)
            and phone
        ):
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
                    checkout_date=str(booking.check_out),
                    checkout_time_line="",
                    grace_deadline="—",
                ),
            )
            count += 1

        clear_claim_wa_if_settled(db, booking)
        balance = booking_balance_due(db, booking.id)
        if balance <= Decimal("0.001"):
            continue
        if not phone:
            continue

        freq = booking_notify_frequency(db, booking)
        # حجوزات تحت ملخص شركة: تخطّي مطالبة فردية
        if freq in (
            NOTIFY_FREQ_SUMMARY_DAILY,
            NOTIFY_FREQ_SUMMARY_WEEKLY,
            NOTIFY_FREQ_SUMMARY_MONTHLY,
        ):
            continue

        watch = bool(getattr(booking, "claim_wa_until_paid", False))
        rem = getattr(booking, "follow_up_at", None)
        watch_due = watch and (rem is None or rem <= today)

        period_ok = should_send_scheduled_claim(db, booking, today=today)
        # مطالبة يدوية/تتبّع تتجاوز تقييد التكرار الأسبوعي/الشهري
        if watch_due or (daily_claim and period_ok):
            ok, _err = emit_hotel_balance_claim(
                db,
                booking,
                claim_note=getattr(booking, "follow_up_note", None),
                immediate=False,
            )
            if ok:
                mark_booking_claim_period_for_freq(booking, freq, today=today)
                count += 1
            if daily_claim and is_in and not watch:
                booking.claim_wa_until_paid = True
                if rem is None:
                    booking.follow_up_at = today
            continue

        if not is_in:
            continue

        if freq != NOTIFY_FREQ_DAILY:
            continue
        if booking.check_in < today < booking.check_out:
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

def emit_hotel_auto_extend_blocked_staff(
    db: Session,
    booking: HotelBooking,
    *,
    reason: str = "room_conflict",
    old_check_out=None,
    new_check_out=None,
) -> None:
    """تنبيه الاستقبال عند فشل التمديد التلقائي (تعارض شقة)."""
    from modules.messaging.models import MessageChannel
    from modules.messaging.outbox import enqueue_message
    from modules.settings.service import get_setting

    staff = (
        (get_setting(db, "hotel_shift_supervisor_phone") or "").strip()
        or (get_setting(db, "hotel_online_staff_phone") or "").strip()
        or (get_setting(db, "hotel_reception_phone") or "").strip()
    )
    if not staff:
        return
    room = ""
    if booking.room is not None:
        room = booking.room.number or booking.room.name_ar or ""
    body = (
        f"⚠️ فشل التمديد التلقائي — حجز #{booking.reference}\n"
        f"النزيل: {booking.guest_name or '—'}\n"
        f"الشقة: {room or '—'}\n"
        f"المغادرة السابقة: {old_check_out or booking.check_out}\n"
        f"المحاولة إلى: {new_check_out or '—'}\n"
        f"السبب: {'تعارض حجز على الشقة' if reason == 'room_conflict' else reason}\n"
        f"راجع الاستقبال فوراً لتمديد يدوي أو نقل الشقة."
    )
    try:
        enqueue_message(
            db,
            body=body,
            channel=MessageChannel.WHATSAPP.value,
            phone=staff,
            event_type="hotel.auto_extend_blocked",
            meta={"kind": "hotel_staff_alert", "booking_id": booking.id},
        )
    except Exception:  # noqa: BLE001
        pass


def emit_hotel_timed_checkout_reminders(
    db: Session,
    *,
    now: datetime | None = None,
    policy=None,
) -> int:
    """تذكير مغادرة حسب ساعة المغادرة ومدة التنبيه المسبق (أدمن)."""
    from modules.hotel.late_checkout import (
        LateCheckoutPolicy,
        late_checkout_policy,
        list_checked_in_due_reminder,
    )

    pol: LateCheckoutPolicy = policy or late_checkout_policy(db)
    if not pol.enabled:
        return 0
    count = 0
    for booking in list_checked_in_due_reminder(db, now=now, policy=pol):
        if _emit_checkout_reminder_for_booking(db, booking, pol, timing="timed"):
            count += 1
    return count


def emit_last_chance_checkout_reminder(
    db: Session,
    booking: HotelBooking,
    *,
    policy=None,
) -> bool:
    """تنبيه أخير قبل احتساب الليلة (idempotent لنفس يوم المغادرة)."""
    from modules.hotel.late_checkout import LateCheckoutPolicy, late_checkout_policy

    pol: LateCheckoutPolicy = policy or late_checkout_policy(db)
    return _emit_checkout_reminder_for_booking(
        db, booking, pol, timing="last_chance"
    )


def _emit_checkout_reminder_for_booking(
    db: Session,
    booking: HotelBooking,
    pol,
    *,
    timing: str,
) -> bool:
    phone = (booking.guest_phone or "").strip() or (
        getattr(booking, "company_contact_phone", None) or ""
    ).strip()
    if not phone or booking.check_out is None:
        return False
    grace_end = pol.grace_deadline(booking.check_out)
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
            departure_timing=timing,
            checkout_date=str(booking.check_out),
            checkout_time=pol.checkout_time,
            checkout_time_line=f" الساعة {pol.checkout_time}",
            grace_deadline=grace_end.strftime("%H:%M"),
            grace_hours=str(pol.grace_hours),
        ),
    )
    return True


def emit_hotel_late_checkout_charged(
    db: Session,
    booking: HotelBooking,
    *,
    result: dict | None = None,
) -> None:
    """إشعار النزيل بعد احتساب ليلة تلقائية بسبب تأخير المغادرة."""
    result = result or {}
    phone = (booking.guest_phone or "").strip() or (
        getattr(booking, "company_contact_phone", None) or ""
    ).strip()
    if not phone:
        return
    old_co = result.get("old_check_out")
    new_co = result.get("new_check_out")
    emit_event_safe(
        db,
        event_key=HOTEL_LATE_CHECKOUT_CHARGED,
        source_type="hotel_booking",
        source_id=booking.id,
        payload=_booking_payload(
            db,
            booking,
            phone=phone,
            guest_phone=phone,
            old_check_out=str(old_co or ""),
            new_check_out=str(new_co or booking.check_out or ""),
            checkout_date=str(old_co or booking.check_out or ""),
        ),
    )


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
