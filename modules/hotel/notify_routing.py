"""توجيه إشعارات حجوزات الشركة/النزيل + جدولة مطالبات الشركات."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.hotel.booking_models import HotelBooking

# تكرار مطالبات/ملخصات الشركة (بروفايل الشركة + وراثة عند الحجز)
NOTIFY_FREQ_DAILY = "DAILY"
NOTIFY_FREQ_WEEKLY = "WEEKLY"
NOTIFY_FREQ_MONTHLY = "MONTHLY"
NOTIFY_FREQ_SUMMARY_DAILY = "SUMMARY_DAILY"
NOTIFY_FREQ_SUMMARY_WEEKLY = "SUMMARY_WEEKLY"
NOTIFY_FREQ_SUMMARY_MONTHLY = "SUMMARY_MONTHLY"

NOTIFY_FREQ_CHOICES: list[tuple[str, str]] = [
    (NOTIFY_FREQ_DAILY, "رسالة يومية لكل حجز عليه رصيد"),
    (NOTIFY_FREQ_WEEKLY, "رسالة أسبوعية لكل حجز"),
    (NOTIFY_FREQ_MONTHLY, "رسالة شهرية لكل حجز"),
    (NOTIFY_FREQ_SUMMARY_DAILY, "ملخص يومي لكل حجوزات الشركة"),
    (NOTIFY_FREQ_SUMMARY_WEEKLY, "ملخص أسبوعي لكل حجوزات الشركة"),
    (NOTIFY_FREQ_SUMMARY_MONTHLY, "ملخص شهري لكل حجوزات الشركة"),
]

NOTIFY_TO_COMPANY = "COMPANY"
NOTIFY_TO_GUEST1 = "GUEST1"
NOTIFY_TO_CHOICES: list[tuple[str, str]] = [
    (NOTIFY_TO_COMPANY, "الشركة / مسؤول الحجز"),
    (NOTIFY_TO_GUEST1, "النزيل رقم 1 في الشقة"),
]

PAYER_COMPANY = "COMPANY"
PAYER_GUEST = "GUEST"


@dataclass
class BookingNotifyTarget:
    notify_to: str  # COMPANY | GUEST1
    is_company: bool
    recipient_name: str  # للاستخدام في التحية
    phone: str
    company_name: str
    guest1_name: str
    account_label: str  # «إقامتك» أو «حساب شركتكم»


def _clean(s: str | None) -> str:
    return (s or "").strip()


def normalize_notify_to(raw: str | None, *, default: str = NOTIFY_TO_GUEST1) -> str:
    v = _clean(raw).upper()
    if v in (NOTIFY_TO_COMPANY, "CO", "CORP"):
        return NOTIFY_TO_COMPANY
    if v in (NOTIFY_TO_GUEST1, "GUEST", "G1", "PRIMARY"):
        return NOTIFY_TO_GUEST1
    return default


def normalize_notify_freq(raw: str | None) -> str:
    v = _clean(raw).upper() or NOTIFY_FREQ_DAILY
    allowed = {c[0] for c in NOTIFY_FREQ_CHOICES}
    return v if v in allowed else NOTIFY_FREQ_DAILY


def normalize_payer(raw: str | None, *, default: str = PAYER_GUEST) -> str:
    v = _clean(raw).upper()
    if v == PAYER_COMPANY:
        return PAYER_COMPANY
    if v in (PAYER_GUEST, "INDIVIDUAL", "CLIENT"):
        return PAYER_GUEST
    return default


def company_for_booking(db: Session, booking: HotelBooking):
    cid = getattr(booking, "company_customer_id", None)
    if not cid:
        return None
    from modules.customers.models import Customer

    return db.get(Customer, int(cid))


def resolve_booking_notify_target(
    db: Session, booking: HotelBooking
) -> BookingNotifyTarget:
    """يحدد المستلم: شركة أم نزيل رقم 1 — مع الاسم والهاتف المناسبين."""
    from modules.hotel.booking_service import primary_staying_guest_contact

    g1_name, g1_phone = primary_staying_guest_contact(booking)
    gt = getattr(booking, "guest_type", None)
    gt_val = (gt.value if hasattr(gt, "value") else str(gt or "")).upper()
    # حجز فرد: الرسالة للنزيل فقط — لا تحية «مسؤول الشركة» حتى لو بقي ربط قديم
    if gt_val == "INDIVIDUAL":
        phone = (
            g1_phone
            or _clean(getattr(booking, "guest_phone", None))
        )
        name = g1_name or _clean(getattr(booking, "guest_name", None)) or "ضيفنا"
        return BookingNotifyTarget(
            notify_to=NOTIFY_TO_GUEST1,
            is_company=False,
            recipient_name=name,
            phone=phone,
            company_name="",
            guest1_name=name,
            account_label="حساب إقامتك",
        )

    co_name = _clean(getattr(booking, "company_name", None))
    company = company_for_booking(db, booking)
    if company is not None:
        co_name = co_name or _clean(company.company_name) or _clean(company.name)

    explicit = _clean(getattr(booking, "notify_to", None))
    if explicit:
        notify_to = normalize_notify_to(explicit)
    elif company is not None or co_name:
        default_to = (
            getattr(company, "company_default_notify_to", None) if company else None
        )
        notify_to = normalize_notify_to(
            default_to, default=NOTIFY_TO_COMPANY
        )
    else:
        notify_to = NOTIFY_TO_GUEST1

    if notify_to == NOTIFY_TO_COMPANY:
        contact = _clean(getattr(booking, "company_contact_name", None))
        phone = (
            _clean(getattr(booking, "company_contact_phone", None))
            or (_clean(company.phone) if company else "")
            or _clean(getattr(booking, "guest_phone", None))
            or g1_phone
        )
        recipient = contact or co_name or "مسؤول الشركة"
        return BookingNotifyTarget(
            notify_to=NOTIFY_TO_COMPANY,
            is_company=True,
            recipient_name=recipient,
            phone=phone,
            company_name=co_name or recipient,
            guest1_name=g1_name,
            account_label=f"حساب شركتكم «{co_name or recipient}»",
        )

    phone = (
        g1_phone
        or _clean(getattr(booking, "guest_phone", None))
        or _clean(getattr(booking, "company_contact_phone", None))
    )
    name = g1_name or _clean(getattr(booking, "guest_name", None)) or "ضيفنا"
    return BookingNotifyTarget(
        notify_to=NOTIFY_TO_GUEST1,
        is_company=False,
        recipient_name=name,
        phone=phone,
        company_name=co_name,
        guest1_name=name,
        account_label="حساب إقامتك",
    )


def notify_payload_overrides(
    db: Session, booking: HotelBooking, **extra: Any
) -> dict[str, Any]:
    """متغيّرات القالب المتكيفة مع المستلم (شركة / نزيل 1)."""
    target = resolve_booking_notify_target(db, booking)
    co = target.company_name or "الشركة"
    if target.is_company:
        service_headline = f"تمت إضافة خدمة على {target.account_label}:"
        claim_headline = (
            f"مطالبة سداد — {co} — حجز #{booking.reference}"
        )
        night_headline = (
            f"تنبيه سداد — {co} — حجز #{booking.reference}"
        )
        checkout_headline = (
            f"تذكير مغادرة — نزيل {target.guest1_name or '—'} — {co}"
        )
        welcome_name = target.recipient_name
    else:
        service_headline = (
            f"تمت إضافة خدمة على حساب إقامتك يا {target.recipient_name}:"
        )
        claim_headline = (
            f"مطالبة سداد يا {target.recipient_name} — حجز #{booking.reference}"
        )
        night_headline = (
            f"تنبيه سداد يا {target.recipient_name}: "
            f"توجد ليلة/رصيد مستحق على حجزك #{booking.reference}."
        )
        checkout_headline = f"تذكير لطيف يا {target.recipient_name}"
        welcome_name = target.recipient_name

    out: dict[str, Any] = {
        "notify_to": target.notify_to,
        "is_company_recipient": "1" if target.is_company else "0",
        "guest_name": target.recipient_name,
        "customer_name": target.recipient_name,
        "phone": target.phone,
        "guest_phone": target.phone,
        "company_name": target.company_name,
        "guest1_name": target.guest1_name,
        "account_label": target.account_label,
        "service_added_headline": service_headline,
        "claim_headline": claim_headline,
        "night_payment_headline": night_headline,
        "checkout_headline": checkout_headline,
        "welcome_name": welcome_name,
    }
    out.update(extra)
    return out


def booking_notify_frequency(db: Session, booking: HotelBooking) -> str:
    company = company_for_booking(db, booking)
    if company is None:
        return NOTIFY_FREQ_DAILY
    return normalize_notify_freq(
        getattr(company, "company_notify_frequency", None)
    )


def _period_key(freq: str, day: date) -> str:
    if freq in (NOTIFY_FREQ_WEEKLY, NOTIFY_FREQ_SUMMARY_WEEKLY):
        iso = day.isocalendar()
        return f"W{iso.year}-{iso.week:02d}"
    if freq in (NOTIFY_FREQ_MONTHLY, NOTIFY_FREQ_SUMMARY_MONTHLY):
        return f"M{day.year}-{day.month:02d}"
    return f"D{day.isoformat()}"


def should_send_scheduled_claim(
    db: Session,
    booking: HotelBooking,
    *,
    today: date | None = None,
) -> bool:
    """هل يحين موعد مطالبة/تنبيه حسب تكرار بروفايل الشركة؟"""
    from app.datetime_local import now_local

    day = today or now_local().date()
    freq = booking_notify_frequency(db, booking)
    # ملخص مجمّع يُعالَج في مسار الشركة وليس هنا
    if freq in (
        NOTIFY_FREQ_SUMMARY_DAILY,
        NOTIFY_FREQ_SUMMARY_WEEKLY,
        NOTIFY_FREQ_SUMMARY_MONTHLY,
    ):
        return False

    last = getattr(booking, "notify_claim_last_period", None)
    period = _period_key(freq, day)
    if last and str(last) == period:
        return False
    return True


def mark_booking_claim_period(
    booking: HotelBooking, *, today: date | None = None
) -> None:
    from app.datetime_local import now_local

    day = today or now_local().date()
    # يحفظ على الحجز مباشرة؛ التكرار من بروفايل الشركة وقت الإرسال
    company_freq = NOTIFY_FREQ_DAILY
    booking.notify_claim_last_period = _period_key(company_freq, day)


def mark_booking_claim_period_for_freq(
    booking: HotelBooking,
    freq: str,
    *,
    today: date | None = None,
) -> None:
    from app.datetime_local import now_local

    day = today or now_local().date()
    booking.notify_claim_last_period = _period_key(
        normalize_notify_freq(freq), day
    )


def company_summary_period_due(
    company,
    *,
    today: date | None = None,
) -> bool:
    from app.datetime_local import now_local

    day = today or now_local().date()
    freq = normalize_notify_freq(
        getattr(company, "company_notify_frequency", None)
    )
    if freq not in (
        NOTIFY_FREQ_SUMMARY_DAILY,
        NOTIFY_FREQ_SUMMARY_WEEKLY,
        NOTIFY_FREQ_SUMMARY_MONTHLY,
    ):
        return False
    period = _period_key(freq, day)
    last = getattr(company, "company_notify_last_period", None)
    return not last or str(last) != period


def mark_company_summary_period(company, *, today: date | None = None) -> None:
    from app.datetime_local import now_local

    day = today or now_local().date()
    freq = normalize_notify_freq(
        getattr(company, "company_notify_frequency", None)
    )
    company.company_notify_last_period = _period_key(freq, day)
    company.company_notify_last_sent_at = datetime.now(timezone.utc)


def build_company_balance_summary_lines(
    db: Session,
    company_id: int,
) -> tuple[list[dict[str, Any]], Decimal]:
    """حجوزات الشركة ذات متبقٍ > 0 لملخص واحد."""
    from modules.hotel.booking_models import BookingStatus
    from modules.hotel.folio import booking_balance_due

    rows = list(
        db.scalars(
            select(HotelBooking).where(
                HotelBooking.company_customer_id == int(company_id),
                HotelBooking.booking_status.in_(
                    (BookingStatus.CHECKED_IN, BookingStatus.CHECKED_OUT)
                ),
            )
        ).all()
    )
    lines: list[dict[str, Any]] = []
    total = Decimal("0")
    for b in rows:
        try:
            bal = booking_balance_due(db, int(b.id))
        except Exception:  # noqa: BLE001
            bal = Decimal("0")
        if bal <= Decimal("0.001"):
            continue
        room = ""
        if b.room is not None:
            room = b.room.number or b.room.name_ar or ""
        lines.append(
            {
                "reference": b.reference,
                "room": room,
                "balance": bal.quantize(Decimal("0.001")),
                "guest": _clean(b.guest_name),
            }
        )
        total += bal
    return lines, total.quantize(Decimal("0.001"))
