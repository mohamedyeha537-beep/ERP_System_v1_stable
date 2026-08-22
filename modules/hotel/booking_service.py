"""خدمات الحجز — CRUD ودورة الحياة."""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session

from modules.hotel.audit import log_audit
from modules.hotel.availability import (
    available_rooms_for_type,
    first_room_conflict,
    has_room_conflict,
    is_room_rentable,
    NOT_RENTABLE_STATUSES,
)
from modules.hotel.booking_models import (
    BookingPaymentStatus,
    BookingSource,
    BookingStatus,
    GuestType,
    HotelAuditLog,
    HotelBooking,
    HotelBookingGuest,
    HotelBookingPayment,
    HotelBookingPaymentRefund,
    HotelBookingRoomAssignment,
    HotelBookingService,
    HotelBookingStatusLog,
    HotelCancellationPolicy,
    HotelProperty,
    HotelRoomType,
    QuotationStatus,
    RecordKind,
    RoomPhysicalStatus,
)
from modules.hotel.booking_labels import QUOTATION_NEXT_STATUSES
from modules.hotel.models import HotelRoom, RoomCharge
from modules.hotel.pricing import (
    accommodation_segment_for_room,
    accommodation_total,
    nightly_rate_for_stay,
)


class BookingError(Exception):
    pass


PREPAYMENT_PERCENT_CHOICES = (0, 50, 100)
PREPAYMENT_SETTING_KEY = "hotel_booking_prepayment_percent"


def get_booking_prepayment_percent(db: Session) -> int:
    from modules.settings.service import get_setting

    raw = (get_setting(db, PREPAYMENT_SETTING_KEY, "100") or "100").strip()
    try:
        pct = int(float(raw))
    except (TypeError, ValueError):
        pct = 100
    return pct if pct in PREPAYMENT_PERCENT_CHOICES else 100


def parse_booking_prepayment_percent(raw: str | None) -> int:
    try:
        pct = int(float((raw or "100").strip()))
    except (TypeError, ValueError):
        return 100
    return pct if pct in PREPAYMENT_PERCENT_CHOICES else 100


def prepayment_percent_choices() -> list[tuple[int, str]]:
    return [
        (0, "0% — السماح بالحجز بدون دفع مقدّم"),
        (50, "50% — عربون نصف قيمة الإقامة"),
        (100, "100% — دفع كامل الإقامة عند الحجز"),
    ]


def prepayment_percent_label(pct: int) -> str:
    labels = {v: lbl for v, lbl in prepayment_percent_choices()}
    return labels.get(pct, f"{pct}%")


def booking_amount_due_for_booking(booking: HotelBooking) -> Decimal:
    due = Decimal(str(booking.accommodation_total or 0)) - Decimal(
        str(booking.discount_amount or 0)
    )
    return due.quantize(Decimal("0.001")) if due > 0 else Decimal("0")


def booking_paid_amount(booking: HotelBooking) -> Decimal:
    return Decimal(str(booking.paid_amount or 0)).quantize(Decimal("0.001"))


def required_prepayment_amount(db: Session, *, due: Decimal) -> Decimal:
    pct = get_booking_prepayment_percent(db)
    if pct <= 0 or due <= 0:
        return Decimal("0")
    return (due * Decimal(pct) / Decimal("100")).quantize(Decimal("0.001"))


def prepayment_requirement_message(db: Session, *, due: Decimal, required: Decimal | None = None) -> str:
    pct = get_booking_prepayment_percent(db)
    req = required if required is not None else required_prepayment_amount(db, due=due)
    if pct <= 0:
        return "لا يُشترط دفع مقدّم — يمكن إنشاء الحجز بدون دفع."
    if pct >= 100:
        return f"يجب دفع كامل الإقامة ({req} د.ل) لإتمام الحجز."
    return f"يجب دفع عربون {pct}% على الأقل ({req} د.ل من أصل {due} د.ل)."


def _booking_is_company_account(booking: HotelBooking) -> bool:
    if getattr(booking, "company_customer_id", None):
        return True
    gt = getattr(booking, "guest_type", None)
    return gt == GuestType.COMPANY or str(getattr(gt, "value", gt) or "").upper() == "COMPANY"


def _booking_payer_is_guest(booking: HotelBooking) -> bool:
    payer = (getattr(booking, "booking_payer", None) or "").strip().upper()
    if payer == "GUEST":
        return True
    if payer == "COMPANY":
        return False
    # افتراضي: شركة تدفع إن وُجد حساب شركة، وإلا النزيل
    return not _booking_is_company_account(booking)


def assert_booking_prepayment_met(db: Session, booking: HotelBooking) -> None:
    # تسجيل على حساب الشركة (booking_payer=COMPANY): يُسمح بدون دفع كامل
    if _booking_is_company_account(booking) and not _booking_payer_is_guest(booking):
        return
    due = booking_amount_due_for_booking(booking)
    required = due  # فرد / شركة بدون تفعيل الحساب: كامل الإقامة
    if required <= 0:
        return
    paid = booking_paid_amount(booking)
    if paid + Decimal("0.001") < required:
        raise BookingError(prepayment_requirement_message(db, due=due, required=required))


def booking_amount_due(
    db: Session,
    *,
    room_type_id: int,
    check_in: date,
    check_out: date,
    discount_amount: Decimal | str | float = Decimal("0"),
    nightly_rate: Decimal | None = None,
    check_in_time: str | None = None,
    check_out_time: str | None = None,
) -> Decimal:
    """إجمالي الإقامة المستحق قبل الدفع."""
    from modules.hotel.checkin_stay import booking_billing_start, booking_stay_units

    disc = Decimal(str(discount_amount or "0")).quantize(Decimal("0.001"))
    billing_in = booking_billing_start(db, check_in=check_in)
    units = booking_stay_units(
        db,
        check_in=check_in,
        check_out=check_out,
        check_in_time=check_in_time,
        check_out_time=check_out_time,
    )
    if nightly_rate is not None and Decimal(str(nightly_rate)) > 0:
        total = (Decimal(str(nightly_rate)) * units).quantize(Decimal("0.001"))
    else:
        total = accommodation_total(
            db,
            room_type_id=room_type_id,
            check_in=check_in,
            check_out=check_out,
            first_chargeable_night=billing_in,
        )
        if units > 0 and check_out == check_in:
            rate = nightly_rate_for_stay(
                db, room_type_id=room_type_id, check_in=check_in, check_out=check_out
            )
            total = (rate * units).quantize(Decimal("0.001"))
    due = (total - disc).quantize(Decimal("0.001"))
    return due if due > 0 else Decimal("0")


def validate_booking_prepayment(
    db: Session,
    *,
    amount: Decimal | str | float,
    payment_method_id: int | str | None,
    room_type_id: int,
    check_in: date,
    check_out: date,
    discount_amount: Decimal | str | float = Decimal("0"),
    nightly_rate: Decimal | None = None,
    guest_type: GuestType | str | None = None,
    wallet_amount: Decimal | str | float = Decimal("0"),
    company_account: bool = False,
    payer: str | None = None,
    charge_to_company_account: bool | None = None,
    check_in_time: str | None = None,
    check_out_time: str | None = None,
) -> Decimal:
    """يرفض الحجز إذا لم يُدفع الحد الأدنى المطلوب — يُرجع المبلغ المستحق.

    الأفراد / شركة بدون تفعيل «التسجيل على حساب الشركة»: دفع كامل إلزامي.
    شركة + تفعيل التسجيل على الحساب: يُسمح بدون دفع (دين على الشركة).
    """
    due = booking_amount_due(
        db,
        room_type_id=room_type_id,
        check_in=check_in,
        check_out=check_out,
        discount_amount=discount_amount,
        nightly_rate=nightly_rate,
        check_in_time=check_in_time,
        check_out_time=check_out_time,
    )
    if due <= 0:
        raise BookingError("تعذّر حساب تكلفة الإقامة — تحقق من التواريخ.")

    gt_raw = guest_type.value if isinstance(guest_type, GuestType) else str(guest_type or "")
    is_company_type = gt_raw.strip().upper() == GuestType.COMPANY.value
    is_company = bool(company_account or is_company_type)
    payer_u = (payer or "").strip().upper()
    if charge_to_company_account is None:
        charge_to_company_account = bool(is_company and payer_u == "COMPANY")
    allow_company_charge = bool(is_company and charge_to_company_account)

    if allow_company_charge:
        required = Decimal("0")
    else:
        required = due

    amt = Decimal(str(amount or "0")).quantize(Decimal("0.001"))
    wallet = Decimal(str(wallet_amount or "0")).quantize(Decimal("0.001"))
    if wallet < 0:
        raise BookingError("مبلغ الخصم من الرصيد غير صالح.")
    total_toward = (amt + wallet).quantize(Decimal("0.001"))
    pm_raw = str(payment_method_id or "").strip()

    if required <= 0:
        if amt > 0 and not pm_raw.isdigit():
            raise BookingError("اختر وسيلة الدفع لتسجيل المبلغ المدفوع.")
        return due

    if total_toward <= 0:
        if is_company:
            raise BookingError(
                f"يجب دفع كامل الإقامة ({required} د.ل) لإتمام الحجز، "
                "أو فعّل تشيك بوكس «إتمام الحجز والتسجيل على حساب الشركة»."
            )
        raise BookingError(prepayment_requirement_message(db, due=due, required=required))
    if amt > 0 and not pm_raw.isdigit():
        raise BookingError("اختر وسيلة الدفع — بدونها لا يُسجَّل المبلغ النقدي.")
    if total_toward + Decimal("0.001") < required:
        covered = f"نقداً {amt} د.ل"
        if wallet > 0:
            covered += f" + من الرصيد {wallet} د.ل"
        if is_company:
            raise BookingError(
                f"يجب دفع كامل الإقامة ({required} د.ل). المُدخل: {covered}. "
                "أو فعّل «التسجيل على حساب الشركة»."
            )
        raise BookingError(
            f"{prepayment_requirement_message(db, due=due, required=required)} "
            f"المُدخل: {covered}."
        )
    return due


def credit_tourism_commission_for_payment(
    db: Session,
    booking: HotelBooking,
    *,
    payment_amount: Decimal,
    user_id: int | None = None,
) -> Decimal:
    """يضيف عمولة شركة السياحة إلى محفظة الشركة عند إتمام قبض.

    العمولة تُخصم ضمناً من صافي إيراد الحجز (قيمة الحجز بعد خصم نسبة الوكالة)
    وتُضاف كرصيد مستحق للوكالة حتى التسوية.
    """
    from modules.customers.models import Customer, CustomerType
    from modules.customers.service import CustomersError, adjust_wallet
    from modules.hotel.tourism_agency import commission_from_net_payment

    if not bool(getattr(booking, "is_tourism_agency", False)):
        return Decimal("0")
    cid = getattr(booking, "company_customer_id", None)
    if not cid:
        return Decimal("0")
    pct = Decimal(str(getattr(booking, "tourism_commission_percent", 0) or 0))
    pay = Decimal(str(payment_amount or 0)).quantize(Decimal("0.001"))
    if pct <= 0 or pay <= 0:
        return Decimal("0")
    # الدفع على أساس صافي الحجز (بعد خصم العمولة) → تحويل إلى عمولة كاملة
    commission = commission_from_net_payment(pay, pct)
    if commission <= 0:
        return Decimal("0")
    company = db.get(Customer, int(cid))
    if company is None or company.customer_type != CustomerType.COMPANY:
        return Decimal("0")
    try:
        adjust_wallet(
            db,
            int(cid),
            amount=commission,
            note=f"عمولة شركة سياحة {pct}% — حجز {booking.reference}",
            user_id=user_id,
        )
    except CustomersError as e:
        raise BookingError(str(e)) from e
    log_audit(
        db,
        entity_type="booking",
        entity_id=booking.id,
        action="tourism_commission",
        new_value=str(commission),
        reason=f"عمولة {pct}% لشركة #{cid}",
        user_id=user_id,
    )
    return commission


def apply_customer_wallet_to_booking(
    db: Session,
    booking_id: int,
    amount: Decimal | str | float,
    *,
    user_id: int | None = None,
    note: str | None = None,
    as_deposit: bool = True,
    wallet_customer_id: int | None = None,
    allow_company_debt: bool = False,
) -> Decimal:
    """يخصم من محفظة العميل/الشركة ويطبّق المبلغ كمدفوع على الحجز."""
    from modules.customers.models import Customer, CustomerType
    from modules.customers.service import (
        CustomersError,
        adjust_wallet,
        company_spendable_balance,
    )

    booking = db.get(HotelBooking, int(booking_id))
    if booking is None:
        raise BookingError("الحجز غير موجود.")
    cid = int(
        wallet_customer_id
        or getattr(booking, "company_customer_id", None)
        or booking.customer_id
        or 0
    )
    if not cid:
        raise BookingError("اختر عميلاً/شركة لها رصيد قبل الخصم من المحفظة.")
    amt = Decimal(str(amount or 0)).quantize(Decimal("0.001"))
    if amt <= 0:
        return Decimal("0")
    customer = db.get(Customer, cid)
    if customer is None:
        raise BookingError("العميل غير موجود.")
    available = company_spendable_balance(customer)
    if available <= 0:
        raise BookingError("لا يوجد رصيد (أو حد ائتمان) قابل للخصم في المحفظة.")
    apply_amt = min(amt, available).quantize(Decimal("0.001"))
    if apply_amt <= 0:
        return Decimal("0")
    bal_before = Decimal(str(customer.wallet_balance or 0)).quantize(Decimal("0.001"))
    goes_negative = (bal_before - apply_amt) < Decimal("-0.0005")
    if goes_negative:
        if customer.customer_type != CustomerType.COMPANY:
            raise BookingError("محفظة الفرد لا تُنزل بالسالب.")
        if not allow_company_debt or not bool(customer.allow_company_credit):
            raise BookingError(
                "محفظة الشركة لا تسمح بالدين. فعّل الائتمان من بطاقة الشركة "
                "وامنح صلاحية «حجز على حساب شركة بالدين»."
            )
    try:
        adjust_wallet(
            db,
            cid,
            amount=-apply_amt,
            note=(
                (note or "").strip()
                or f"خصم من الرصيد لحجز {booking.reference}"
            ),
            user_id=user_id,
            allow_company_debt=bool(allow_company_debt),
        )
    except CustomersError as e:
        raise BookingError(str(e)) from e
    booking.paid_amount = (
        Decimal(str(booking.paid_amount or 0)) + apply_amt
    ).quantize(Decimal("0.001"))
    if as_deposit:
        booking.deposit_amount = (
            Decimal(str(booking.deposit_amount or 0)) + apply_amt
        ).quantize(Decimal("0.001"))
    _recalc_payment_status(db, booking)
    try:
        credit_tourism_commission_for_payment(
            db, booking, payment_amount=apply_amt, user_id=user_id
        )
    except Exception:  # noqa: BLE001
        pass
    log_audit(
        db,
        entity_type="booking",
        entity_id=booking.id,
        action="wallet_credit_applied",
        new_value=str(apply_amt),
        reason=(note or "").strip() or "خصم من رصيد محفظة العميل على الحجز",
        user_id=user_id,
    )
    try:
        from modules.hotel.booking_debts import sync_open_debts_with_folio

        sync_open_debts_with_folio(db, int(booking.id), user_id=user_id)
    except Exception:  # noqa: BLE001
        pass
    try:
        from modules.hotel.follow_up import clear_claim_wa_if_settled

        clear_claim_wa_if_settled(db, booking)
    except Exception:  # noqa: BLE001
        pass
    db.flush()
    return apply_amt


AUTO_WALLET_COVER_NOTE = "خصم تلقائي من المحفظة لتغطية متبقي الحجز"


def apply_wallet_to_cover_booking_dues(
    db: Session,
    *,
    customer_id: int | None = None,
    booking_ids: list[int] | None = None,
    user_id: int | None = None,
) -> Decimal:
    """يخصم تلقائياً من محفظة العميل لتغطية متبقي الحجوزات النشطة (الأقدم أولاً).

    محفظة الشركة تغطي حساب الشركة فقط — لا تُعتبر فاتورة مطعم النزيل مدفوعة.
    """
    from modules.customers.models import Customer
    from modules.hotel.folio import folio_auto_wallet_due

    ids: list[int] = []
    if booking_ids:
        ids = [int(i) for i in booking_ids]
    elif customer_id:
        rows = db.scalars(
            select(HotelBooking.id)
            .where(
                HotelBooking.customer_id == int(customer_id),
                HotelBooking.record_kind == RecordKind.BOOKING,
                HotelBooking.booking_status.in_(
                    [
                        BookingStatus.PENDING,
                        BookingStatus.CONFIRMED,
                        BookingStatus.CHECKED_IN,
                        BookingStatus.CHECKED_OUT,
                    ]
                ),
            )
            .order_by(HotelBooking.id.asc())
        ).all()
        ids = [int(i) for i in rows]
    if not ids:
        return Decimal("0")

    total_applied = Decimal("0")
    for bid in ids:
        booking = db.get(HotelBooking, bid)
        if booking is None or not booking.customer_id:
            continue
        try:
            repair_auto_wallet_covering_guest_folio(db, booking, user_id=user_id)
        except Exception:  # noqa: BLE001
            pass
        cust = db.get(Customer, int(booking.customer_id))
        if cust is None:
            continue
        wallet = Decimal(str(cust.wallet_balance or 0)).quantize(Decimal("0.001"))
        if wallet <= Decimal("0.0005"):
            continue
        try:
            due = folio_auto_wallet_due(db, booking)
        except Exception:  # noqa: BLE001
            continue
        if due <= Decimal("0.0005"):
            continue
        take = min(wallet, due).quantize(Decimal("0.001"))
        if take <= Decimal("0.0005"):
            continue
        try:
            applied = apply_customer_wallet_to_booking(
                db,
                bid,
                take,
                user_id=user_id,
                note=AUTO_WALLET_COVER_NOTE,
                as_deposit=False,
            )
        except BookingError:
            continue
        total_applied = (total_applied + applied).quantize(Decimal("0.001"))
    return total_applied


def repair_auto_wallet_covering_guest_folio(
    db: Session,
    booking: HotelBooking,
    *,
    user_id: int | None = None,
) -> Decimal:
    """يعيد خصم المحفظة التلقائي الذي سدّد حساب النزيل من محفظة الشركة."""
    from modules.customers.service import CustomersError, adjust_wallet
    from modules.hotel.folio import build_folio

    company_id = getattr(booking, "company_customer_id", None)
    if not company_id:
        return Decimal("0")
    try:
        folio = build_folio(db, int(booking.id))
    except Exception:  # noqa: BLE001
        return Decimal("0")
    guest_total = Decimal(str(folio.guest_total or 0)).quantize(Decimal("0.001"))
    if guest_total <= Decimal("0.0005"):
        return Decimal("0")

    auto_net = Decimal("0")
    try:
        audits = list(
            db.scalars(
                select(HotelAuditLog)
                .where(
                    HotelAuditLog.entity_type == "booking",
                    HotelAuditLog.entity_id == int(booking.id),
                    HotelAuditLog.action.in_(
                        ("wallet_credit_applied", "wallet_credit_reversed")
                    ),
                )
                .order_by(HotelAuditLog.id.asc())
            ).all()
        )
    except Exception:  # noqa: BLE001
        audits = []
    for row in audits:
        try:
            amt = Decimal(str(row.new_value or 0)).quantize(Decimal("0.001"))
        except Exception:  # noqa: BLE001
            continue
        if amt <= 0:
            continue
        reason = row.reason or ""
        if row.action == "wallet_credit_applied" and AUTO_WALLET_COVER_NOTE in reason:
            auto_net += amt
        elif row.action == "wallet_credit_reversed":
            auto_net -= amt
    take = min(guest_total, max(Decimal("0"), auto_net)).quantize(Decimal("0.001"))
    if take <= Decimal("0.0005"):
        return Decimal("0")

    cid = int(company_id)
    try:
        adjust_wallet(
            db,
            cid,
            amount=take,
            note="إلغاء خصم تلقائي — فاتورة على حساب النزيل وليست دفعة",
            user_id=user_id,
        )
    except CustomersError:
        return Decimal("0")
    booking.paid_amount = max(
        Decimal("0"),
        (Decimal(str(booking.paid_amount or 0)) - take).quantize(Decimal("0.001")),
    )
    _recalc_payment_status(db, booking)
    log_audit(
        db,
        entity_type="booking",
        entity_id=booking.id,
        action="wallet_credit_reversed",
        new_value=str(take),
        reason="إلغاء خصم تلقائي من محفظة الشركة على حساب النزيل (مطعم/خدمات)",
        user_id=user_id,
    )
    db.flush()
    return take


def repair_silent_auto_wallet_cover(
    db: Session,
    booking: HotelBooking,
    *,
    user_id: int | None = None,
) -> Decimal:
    """يعيد خصم «تغطية متبقي الحجز» الذي جرى صامتاً من لوحة/قائمة الحجوزات.

    خصم إنشاء الحجز (عربون/مقدّم) يبقى. الخصم اليدوي من زر الاستقبال يبقى.
    """
    from modules.customers.service import CustomersError, adjust_wallet

    cid = int(
        getattr(booking, "company_customer_id", None)
        or getattr(booking, "customer_id", None)
        or 0
    )
    if not cid:
        return Decimal("0")
    auto_net = Decimal("0")
    try:
        audits = list(
            db.scalars(
                select(HotelAuditLog)
                .where(
                    HotelAuditLog.entity_type == "booking",
                    HotelAuditLog.entity_id == int(booking.id),
                    HotelAuditLog.action.in_(
                        ("wallet_credit_applied", "wallet_credit_reversed")
                    ),
                )
                .order_by(HotelAuditLog.id.asc())
            ).all()
        )
    except Exception:  # noqa: BLE001
        audits = []
    for row in audits:
        try:
            amt = Decimal(str(row.new_value or 0)).quantize(Decimal("0.001"))
        except Exception:  # noqa: BLE001
            continue
        if amt <= 0:
            continue
        reason = row.reason or ""
        if row.action == "wallet_credit_applied" and AUTO_WALLET_COVER_NOTE in reason:
            auto_net += amt
        elif row.action == "wallet_credit_reversed":
            auto_net -= amt
    take = max(Decimal("0"), auto_net).quantize(Decimal("0.001"))
    if take <= Decimal("0.0005"):
        return Decimal("0")
    try:
        adjust_wallet(
            db,
            cid,
            amount=take,
            note="إلغاء خصم تلقائي صامت — يُخصم من الرصيد يدوياً عند الطلب",
            user_id=user_id,
        )
    except CustomersError:
        return Decimal("0")
    booking.paid_amount = max(
        Decimal("0"),
        (Decimal(str(booking.paid_amount or 0)) - take).quantize(Decimal("0.001")),
    )
    _recalc_payment_status(db, booking)
    log_audit(
        db,
        entity_type="booking",
        entity_id=booking.id,
        action="wallet_credit_reversed",
        new_value=str(take),
        reason="إلغاء خصم تلقائي صامت من المحفظة (غير طلب الاستقبال)",
        user_id=user_id,
    )
    db.flush()
    return take


def _ref(*, quotation: bool = False) -> str:
    token = secrets.token_hex(4).upper()
    return f"Q{token}" if quotation else token


@dataclass
class StayingGuestInput:
    full_name: str
    id_number: str | None = None
    id_type: str | None = None
    nationality: str | None = None
    address: str | None = None
    phone: str | None = None


def get_primary_staying_guest(booking: HotelBooking | None) -> HotelBookingGuest | None:
    """نزيل رقم 1 (الأول / is_primary) في الشقة."""
    if booking is None:
        return None
    guests = list(getattr(booking, "guests", None) or [])
    if not guests:
        return None
    for g in guests:
        if bool(getattr(g, "is_primary", False)):
            return g
    # أول نزيل حسب ترتيب الإدخال (نزيل رقم 1 في النموذج)
    return sorted(guests, key=lambda g: int(getattr(g, "id", 0) or 0))[0]


def primary_staying_guest_contact(
    booking: HotelBooking | None,
) -> tuple[str, str]:
    """(اسم نزيل 1, هاتف واتساب المفضّل) — للاستخدام في POS / الإشعارات."""
    if booking is None:
        return "", ""
    g = get_primary_staying_guest(booking)
    name = ""
    phone = ""
    if g is not None:
        name = (getattr(g, "full_name", None) or "").strip()
        phone = (getattr(g, "phone", None) or "").strip()
    if not name:
        name = (booking.guest_name or "").strip()
    if not phone:
        if getattr(booking, "guest_type", None) == GuestType.COMPANY:
            phone = (
                (booking.company_contact_phone or booking.guest_phone or "")
            ).strip()
        else:
            phone = (booking.guest_phone or "").strip()
    return name, phone


def build_hotel_invoice_party(booking: HotelBooking | None) -> dict[str, str | int | bool | None]:
    """بيانات فاتورة المغادرة: الجهة الدافعة + النزيل المقيم + الغرفة ومدة الإقامة."""
    empty = {
        "is_company": False,
        "payer_label": "الجهة الدافعة",
        "payer_name": "",
        "payer_phone": "",
        "payer_contact_name": "",
        "resident_name": "",
        "resident_phone": "",
        "room_label": "",
        "room_type": "",
        "check_in": None,
        "check_out": None,
        "nights": 0,
        "payer_is_resident": True,
        "entity_name": "",
        "entity_phone": "",
        "show_entity_block": False,
    }
    if booking is None:
        return empty

    try:
        from modules.customers.service import normalize_phone
    except Exception:  # noqa: BLE001

        def normalize_phone(p: str) -> str:  # type: ignore[misc]
            return (p or "").strip()

    is_company = (
        getattr(booking, "guest_type", None) == GuestType.COMPANY
        or str(getattr(getattr(booking, "guest_type", None), "value", "") or "").upper()
        == "COMPANY"
    )
    payer_raw = (
        (getattr(booking, "booking_payer", None) or "")
        or (getattr(booking, "stay_payer", None) or "")
        or ("COMPANY" if is_company else "GUEST")
    )
    payer_side = str(payer_raw).strip().upper()
    if payer_side not in ("COMPANY", "GUEST", "SHARED"):
        payer_side = "COMPANY" if is_company else "GUEST"

    company_name = (getattr(booking, "company_name", None) or "").strip()
    company_phone = normalize_phone(
        (getattr(booking, "company_contact_phone", None) or "").strip()
    )
    company_contact = (getattr(booking, "company_contact_name", None) or "").strip()

    # هواتف من بطاقات العملاء إن وُجدت
    cust_phone = ""
    co_phone = ""
    try:
        cust = getattr(booking, "customer", None)
        if cust is not None:
            cust_phone = normalize_phone((getattr(cust, "phone", None) or "").strip())
            if not company_name:
                company_name = (
                    getattr(cust, "company_name", None) or getattr(cust, "name", None) or ""
                ).strip()
        co = getattr(booking, "company_customer", None)
        if co is not None:
            co_phone = normalize_phone((getattr(co, "phone", None) or "").strip())
            if not company_name:
                company_name = (
                    getattr(co, "company_name", None) or getattr(co, "name", None) or ""
                ).strip()
    except Exception:  # noqa: BLE001
        pass
    if not company_phone:
        company_phone = co_phone or (cust_phone if is_company else "")

    g = get_primary_staying_guest(booking)
    resident_name = (
        (getattr(g, "full_name", None) or "").strip() if g is not None else ""
    )
    resident_phone = (
        normalize_phone((getattr(g, "phone", None) or "").strip()) if g is not None else ""
    )
    booking_guest_name = (booking.guest_name or "").strip()
    booking_guest_phone = normalize_phone((booking.guest_phone or "").strip())

    if not resident_name:
        # للشركة: guest_name قد يكون اسم الشركة — لا نفترض أنه نزيل الشقة
        if not is_company:
            resident_name = booking_guest_name
        elif booking_guest_name and booking_guest_name != company_name:
            resident_name = booking_guest_name
        else:
            resident_name = company_contact or booking_guest_name or "—"
    if not resident_phone:
        if booking_guest_phone and booking_guest_phone != company_phone:
            resident_phone = booking_guest_phone
        elif not is_company:
            resident_phone = booking_guest_phone or cust_phone

    # الجهة التي دفعت / تُصدر لها الفاتورة
    if payer_side == "COMPANY" and (is_company or company_name):
        payer_name = company_name or booking.display_name or booking_guest_name
        payer_phone = company_phone or booking_guest_phone or co_phone or cust_phone
        payer_label = "الجهة الدافعة (شركة)"
        payer_contact = company_contact
    elif payer_side == "SHARED" and is_company:
        payer_name = company_name or booking.display_name or booking_guest_name
        payer_phone = company_phone or booking_guest_phone or co_phone or cust_phone
        payer_label = "الجهة الحاجّة (مشترك)"
        payer_contact = company_contact
    else:
        payer_name = resident_name or booking_guest_name or booking.display_name
        payer_phone = resident_phone or booking_guest_phone or cust_phone
        payer_label = "الجهة الدافعة (فرد)"
        payer_contact = ""
        if is_company and company_name:
            # حجز شركة والدفعة على النزيل/الضيف
            payer_name = resident_name or booking_guest_name
            payer_phone = resident_phone or booking_guest_phone
            payer_label = "الجهة الدافعة (النزيل)"

    # جهة الحجز (شركة) — تُعرض دائماً عند وجودها
    entity_name = ""
    entity_phone = ""
    if is_company or company_name or getattr(booking, "company_customer_id", None):
        entity_name = company_name or company_contact or ""
        entity_phone = company_phone or co_phone or ""
    show_entity = bool(entity_name) and (
        is_company
        or payer_side in ("COMPANY", "SHARED")
        or bool(getattr(booking, "company_customer_id", None))
    )

    room = getattr(booking, "room", None)
    room_label = ""
    room_type = ""
    if room is not None:
        room_label = (getattr(room, "number", None) or "").strip()
        rt = getattr(room, "room_type", None)
        if rt is not None:
            room_type = (getattr(rt, "name_ar", None) or "").strip()
    if not room_type:
        rt2 = getattr(booking, "room_type", None)
        if rt2 is not None:
            room_type = (getattr(rt2, "name_ar", None) or "").strip()

    nights = int(getattr(booking, "nights", None) or 0)
    if nights <= 0:
        try:
            nights = max(0, (booking.check_out - booking.check_in).days)
            if nights <= 0 and booking.check_out == booking.check_in:
                nights = 1
        except Exception:  # noqa: BLE001
            nights = 0

    payer_name_s = (payer_name or "").strip()
    resident_name_s = (resident_name or "").strip()
    # فرد فقط يمكن دمج البلوكين — حجز الشركة يعرض دائماً جهة + نزيل
    same_person = (
        not show_entity
        and bool(payer_name_s)
        and bool(resident_name_s)
        and payer_name_s == resident_name_s
        and (payer_phone or "") == (resident_phone or "")
        and not is_company
    )

    return {
        "is_company": is_company,
        "payer_label": payer_label,
        "payer_name": payer_name_s or "—",
        "payer_phone": payer_phone or "",
        "payer_contact_name": payer_contact or "",
        "resident_name": resident_name_s or "—",
        "resident_phone": resident_phone or "",
        "room_label": room_label or "—",
        "room_type": room_type,
        "check_in": booking.check_in,
        "check_out": booking.check_out,
        "nights": nights,
        "payer_is_resident": same_person,
        "entity_name": (entity_name or "").strip(),
        "entity_phone": entity_phone or "",
        "show_entity_block": show_entity,
    }


def parse_staying_guests(
    *,
    names: list[str],
    id_numbers: list[str] | None = None,
    id_types: list[str] | None = None,
    nationalities: list[str] | None = None,
    addresses: list[str] | None = None,
    phones: list[str] | None = None,
) -> list[StayingGuestInput]:
    id_numbers = id_numbers or []
    id_types = id_types or []
    nationalities = nationalities or []
    addresses = addresses or []
    phones = phones or []
    guests: list[StayingGuestInput] = []
    for i, raw_name in enumerate(names):
        name = (raw_name or "").strip()
        if not name:
            continue
        guests.append(
            StayingGuestInput(
                full_name=name,
                id_number=(id_numbers[i] if i < len(id_numbers) else "").strip() or None,
                id_type=(id_types[i] if i < len(id_types) else "").strip() or None,
                nationality=(nationalities[i] if i < len(nationalities) else "").strip() or None,
                address=(addresses[i] if i < len(addresses) else "").strip() or None,
                phone=(phones[i] if i < len(phones) else "").strip() or None,
            )
        )
    return guests


def sync_staying_guests(
    db: Session,
    booking: HotelBooking,
    guests: list[StayingGuestInput],
    *,
    fallback_name: str | None = None,
    fallback_phone: str | None = None,
    fallback_id_number: str | None = None,
    fallback_id_type: str | None = None,
    fallback_nationality: str | None = None,
    fallback_address: str | None = None,
) -> list[HotelBookingGuest]:
    if not guests:
        fb_name = (fallback_name or booking.guest_name or "").strip()
        if not fb_name:
            raise BookingError("أدخل اسم نزيل واحد على الأقل في الشقة.")
        guests = [
            StayingGuestInput(
                full_name=fb_name,
                id_number=fallback_id_number or booking.guest_id_number,
                id_type=fallback_id_type or booking.guest_id_type,
                nationality=fallback_nationality or booking.guest_nationality,
                address=fallback_address or booking.guest_address,
                phone=fallback_phone or booking.guest_phone,
            )
        ]
    booking.guests.clear()
    saved: list[HotelBookingGuest] = []
    for i, g in enumerate(guests):
        row = HotelBookingGuest(
            booking_id=booking.id,
            full_name=g.full_name,
            is_primary=(i == 0),
            id_number=g.id_number,
            id_type=g.id_type,
            nationality=g.nationality,
            address=g.address,
            phone=g.phone,
        )
        db.add(row)
        saved.append(row)
    primary = saved[0]
    booking.guest_id_number = primary.id_number
    booking.guest_id_type = primary.id_type
    booking.guest_nationality = primary.nationality
    booking.guest_address = primary.address
    db.flush()
    return saved


def _resolve_guest_fields(
    *,
    guest_type: GuestType,
    guest_name: str,
    guest_phone: str | None,
    guest_email: str | None,
    company_name: str | None = None,
    company_tax_id: str | None = None,
    company_address: str | None = None,
    company_contact_name: str | None = None,
    company_contact_phone: str | None = None,
    company_contact_email: str | None = None,
) -> dict:
    if guest_type == GuestType.COMPANY:
        cname = (company_name or "").strip()
        if not cname:
            raise BookingError("اسم الشركة مطلوب.")
        contact = (company_contact_name or guest_name or "").strip() or cname
        phone = (company_contact_phone or guest_phone or "").strip() or None
        if not phone:
            raise BookingError("رقم هاتف الشركة مطلوب.")
        email = (company_contact_email or guest_email or "").strip() or None
        return {
            "guest_name": cname,
            "guest_phone": phone,
            "guest_email": email,
            "company_name": cname,
            "company_tax_id": (company_tax_id or "").strip() or None,
            "company_address": (company_address or "").strip() or None,
            "company_contact_name": contact,
            "company_contact_phone": phone,
            "company_contact_email": email,
        }
    name = (guest_name or "").strip()
    if not name:
        raise BookingError("اسم جهة الحجز مطلوب.")
    phone = (guest_phone or "").strip() or None
    if not phone:
        raise BookingError("رقم الهاتف مطلوب للفرد.")
    return {
        "guest_name": name,
        "guest_phone": phone,
        "guest_email": (guest_email or "").strip() or None,
        "company_name": None,
        "company_tax_id": None,
        "company_address": None,
        "company_contact_name": None,
        "company_contact_phone": None,
        "company_contact_email": None,
    }


def _log_status(
    db: Session,
    booking: HotelBooking,
    to_status: BookingStatus,
    user_id: int | None,
    note: str | None = None,
    from_status: BookingStatus | None = None,
) -> None:
    db.add(
        HotelBookingStatusLog(
            booking_id=booking.id,
            from_status=(from_status or booking.booking_status).value,
            to_status=to_status.value,
            user_id=user_id,
            note=note,
        )
    )


def _log_room_status(
    db: Session,
    room: HotelRoom,
    to_status: RoomPhysicalStatus,
    user_id: int | None,
    note: str | None = None,
) -> None:
    from modules.hotel.booking_models import HotelRoomStatusLog
    from modules.hotel.nav_badges import invalidate_hotel_nav_badges

    old = room.physical_status
    db.add(
        HotelRoomStatusLog(
            room_id=room.id,
            from_status=old.value if old else None,
            to_status=to_status.value,
            user_id=user_id,
            note=note,
        )
    )
    room.physical_status = to_status
    try:
        invalidate_hotel_nav_badges()
    except Exception:
        pass
    try:
        from modules.dashboard_notify.pending import invalidate_pending_counts

        invalidate_pending_counts("hotel_housekeeping", "hotel_cleaning")
    except Exception:
        pass


def ensure_default_property(db: Session) -> HotelProperty:
    prop = db.scalar(select(HotelProperty).limit(1))
    if prop is not None:
        return prop
    prop = HotelProperty(name_ar="العقار الرئيسي", is_active=True)
    db.add(prop)
    db.flush()
    return prop


def ensure_default_room_types(db: Session, property_id: int = 1) -> None:
    if db.scalar(select(func.count()).select_from(HotelRoomType)):
        return
    defaults = [
        ("غرفة مفردة", "SGL", 1, 0, Decimal("80")),
        ("غرفة مزدوجة", "DBL", 2, 0, Decimal("120")),
        ("جناح", "STE", 2, 2, Decimal("200")),
        ("شقة عائلية", "FAM", 4, 2, Decimal("250")),
    ]
    for name, code, adults, children, price in defaults:
        db.add(
            HotelRoomType(
                property_id=property_id,
                name_ar=name,
                code=code,
                capacity_adults=adults,
                capacity_children=children,
                base_price=price,
                is_active=True,
            )
        )
    db.flush()


def ensure_default_cancellation_policy(db: Session, property_id: int = 1) -> None:
    if db.scalar(select(func.count()).select_from(HotelCancellationPolicy)):
        return
    db.add(
        HotelCancellationPolicy(
            property_id=property_id,
            name_ar="سياسة افتراضية",
            hours_before_free=48,
            penalty_percent=Decimal("50"),
            no_show_nights_penalty=1,
            is_default=True,
            is_active=True,
        )
    )
    db.flush()


def list_room_types(db: Session, *, only_active: bool = False) -> list[HotelRoomType]:
    stmt = select(HotelRoomType).order_by(HotelRoomType.sort_order, HotelRoomType.name_ar)
    if only_active:
        stmt = stmt.where(HotelRoomType.is_active.is_(True))
    return list(db.scalars(stmt).all())


def room_type_usage(db: Session, room_type_id: int) -> dict[str, int]:
    from modules.hotel.booking_models import HotelRatePlan
    from sqlalchemy import inspect as sa_inspect

    table_names = set(sa_inspect(db.bind).get_table_names())

    rooms = 0
    bookings = 0
    rate_plans = 0
    try:
        if "hotel_rooms" in table_names:
            rooms = int(
                db.scalar(
                    select(func.count())
                    .select_from(HotelRoom)
                    .where(HotelRoom.room_type_id == room_type_id)
                )
                or 0
            )
        if "hotel_bookings" in table_names:
            bookings = int(
                db.scalar(
                    select(func.count())
                    .select_from(HotelBooking)
                    .where(HotelBooking.room_type_id == room_type_id)
                )
                or 0
            )
        if "hotel_rate_plans" in table_names:
            rate_plans = int(
                db.scalar(
                    select(func.count())
                    .select_from(HotelRatePlan)
                    .where(HotelRatePlan.room_type_id == room_type_id)
                )
                or 0
            )
    except Exception:  # noqa: BLE001
        pass
    return {"rooms": rooms, "bookings": bookings, "rate_plans": rate_plans}


def room_type_can_delete(db: Session, room_type_id: int) -> bool:
    u = room_type_usage(db, room_type_id)
    return u["rooms"] == 0 and u["bookings"] == 0


def set_room_type_active(db: Session, room_type_id: int, *, active: bool) -> HotelRoomType:
    rt = db.get(HotelRoomType, room_type_id)
    if rt is None:
        raise BookingError("النوع غير موجود.")
    rt.is_active = bool(active)
    db.flush()
    return rt


def delete_room_type(db: Session, room_type_id: int) -> None:
    u = room_type_usage(db, room_type_id)
    if u["rooms"] > 0:
        raise BookingError(
            f"لا يمكن الحذف — {u['rooms']} شقة مرتبطة بهذا النوع. عطّله بدلاً من الحذف."
        )
    if u["bookings"] > 0:
        raise BookingError(
            f"لا يمكن الحذف — {u['bookings']} حجز مرتبط بهذا النوع. عطّله بدلاً من الحذف."
        )
    rt = db.get(HotelRoomType, room_type_id)
    if rt is None:
        raise BookingError("النوع غير موجود.")
    db.delete(rt)
    db.flush()


def get_booking(db: Session, booking_id: int) -> HotelBooking | None:
    return db.get(HotelBooking, booking_id)


def get_booking_by_token(db: Session, token: str) -> HotelBooking | None:
    t = (token or "").strip()
    if not t:
        return None
    return db.scalar(select(HotelBooking).where(HotelBooking.access_token == t))


def _booking_search_digits(raw: str) -> str:
    return "".join(ch for ch in (raw or "") if ch.isdigit())


def list_bookings(
    db: Session,
    *,
    status: BookingStatus | None = None,
    statuses: list[BookingStatus] | tuple[BookingStatus, ...] | None = None,
    room_id: int | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
    record_kind: RecordKind | None = RecordKind.BOOKING,
    limit: int = 200,
    guest_name: str | None = None,
    reference: str | None = None,
    guest_phone: str | None = None,
) -> list[HotelBooking]:
    stmt = select(HotelBooking).order_by(HotelBooking.id.desc())
    if record_kind is not None:
        stmt = stmt.where(HotelBooking.record_kind == record_kind)
    if statuses:
        stmt = stmt.where(HotelBooking.booking_status.in_(list(statuses)))
    elif status is not None:
        stmt = stmt.where(HotelBooking.booking_status == status)
    if room_id is not None:
        stmt = stmt.where(HotelBooking.room_id == room_id)
    if from_date is not None:
        stmt = stmt.where(HotelBooking.check_out >= from_date)
    if to_date is not None:
        stmt = stmt.where(HotelBooking.check_in <= to_date)

    name_q = (guest_name or "").strip()
    ref_q = (reference or "").strip()
    phone_q = (guest_phone or "").strip()
    if name_q:
        like = f"%{name_q}%"
        stmt = stmt.where(
            or_(
                HotelBooking.guest_name.like(like),
                HotelBooking.company_name.like(like),
                HotelBooking.company_contact_name.like(like),
                exists().where(
                    HotelBookingGuest.booking_id == HotelBooking.id,
                    HotelBookingGuest.full_name.like(like),
                ),
            )
        )
    if ref_q:
        stmt = stmt.where(HotelBooking.reference.like(f"%{ref_q}%"))
    if phone_q:
        phone_like = f"%{phone_q}%"
        digits = _booking_search_digits(phone_q)
        phone_ors = [
            HotelBooking.guest_phone.like(phone_like),
            HotelBooking.company_contact_phone.like(phone_like),
            exists().where(
                HotelBookingGuest.booking_id == HotelBooking.id,
                HotelBookingGuest.phone.like(phone_like),
            ),
        ]
        if digits and digits != phone_q:
            digit_like = f"%{digits}%"
            phone_ors.extend(
                [
                    HotelBooking.guest_phone.like(digit_like),
                    HotelBooking.company_contact_phone.like(digit_like),
                    exists().where(
                        HotelBookingGuest.booking_id == HotelBooking.id,
                        HotelBookingGuest.phone.like(digit_like),
                    ),
                ]
            )
        stmt = stmt.where(or_(*phone_ors))
    if name_q or ref_q or phone_q:
        limit = max(int(limit), 300)
    stmt = stmt.limit(limit)
    return list(db.scalars(stmt).all())


def list_quotations(
    db: Session,
    *,
    quotation_status: QuotationStatus | None = None,
    limit: int = 200,
) -> list[HotelBooking]:
    stmt = (
        select(HotelBooking)
        .where(HotelBooking.record_kind == RecordKind.QUOTATION)
        .order_by(HotelBooking.id.desc())
        .limit(limit)
    )
    if quotation_status is not None:
        stmt = stmt.where(HotelBooking.quotation_status == quotation_status)
    return list(db.scalars(stmt).all())


def _recalc_payment_status(db: Session, booking: HotelBooking) -> None:
    """حالة الدفع من كشف الحساب الكامل (إقامة + خدمات + مطعم)، وليس الإقامة وحدها."""
    paid = Decimal(str(booking.paid_amount or 0))
    try:
        from modules.hotel.folio import build_folio

        folio = build_folio(db, int(booking.id))
        total = Decimal(str(folio.total or 0))
        balance = Decimal(str(folio.balance or 0))
        if total <= 0 and paid <= 0:
            booking.payment_status = BookingPaymentStatus.UNPAID
        elif balance <= Decimal("0.001"):
            booking.payment_status = BookingPaymentStatus.FULLY_PAID
        elif paid > 0:
            booking.payment_status = BookingPaymentStatus.PARTIALLY_PAID
        else:
            booking.payment_status = BookingPaymentStatus.UNPAID
        return
    except Exception:
        pass
    due = Decimal(str(booking.accommodation_total or 0)) - Decimal(
        str(booking.discount_amount or 0)
    )
    if paid <= 0:
        booking.payment_status = BookingPaymentStatus.UNPAID
    elif paid + Decimal("0.001") >= due:
        booking.payment_status = BookingPaymentStatus.FULLY_PAID
    else:
        booking.payment_status = BookingPaymentStatus.PARTIALLY_PAID


def _recalc_booking_accommodation(
    db: Session,
    booking: HotelBooking,
    *,
    room: HotelRoom | None = None,
    through_date: date | None = None,
) -> Decimal:
    """إعادة حساب إجمالي الإقامة حسب تواريخ الحجز الحالية."""
    end = through_date or booking.check_out
    if booking.booking_status == BookingStatus.CHECKED_IN and booking.room_assignments:
        return recompute_accommodation_through(db, booking, end)
    assigned = room
    if assigned is None and booking.room_id:
        assigned = db.get(HotelRoom, booking.room_id)
    return accommodation_segment_for_room(
        db,
        room=assigned,
        room_type_id=booking.room_type_id,
        seg_in=getattr(booking, "first_chargeable_night", None) or booking.check_in,
        seg_out=end,
        arrival_at=getattr(booking, "checked_in_at", None),
        departure_at=getattr(booking, "checked_out_at", None),
    )


def _apply_stay_pricing(
    db: Session,
    booking: HotelBooking,
    *,
    room: HotelRoom | None = None,
    through_date: date | None = None,
) -> None:
    total = _recalc_booking_accommodation(
        db, booking, room=room, through_date=through_date
    )
    booking.accommodation_total = total
    nights = max(0, int(booking.nights or 0))
    if nights <= 0:
        booking.nightly_rate = Decimal("0")
    else:
        booking.nightly_rate = (total / Decimal(nights)).quantize(Decimal("0.001"))
    _recalc_payment_status(db, booking)


def adjust_stay_dates(
    db: Session,
    booking_id: int,
    *,
    new_check_in: date | None = None,
    new_check_out: date | None = None,
    user_id: int | None = None,
) -> HotelBooking:
    """تعديل مواعيد الإقامة — وصول أبكر أو مغادرة أبكر قبل التسكين."""
    booking = db.get(HotelBooking, booking_id)
    if booking is None:
        raise BookingError("الحجز غير موجود.")
    if booking.record_kind == RecordKind.QUOTATION:
        raise BookingError("عدّل التواريخ من عرض السعر أو حوّله إلى حجز.")
    if booking.booking_status in (
        BookingStatus.CANCELLED,
        BookingStatus.CHECKED_OUT,
        BookingStatus.NO_SHOW,
        BookingStatus.LATE_CANCELLATION,
    ):
        raise BookingError("لا يمكن تعديل تواريخ هذا الحجز.")
    if booking.booking_status == BookingStatus.CHECKED_IN:
        raise BookingError(
            "النزيل مسكّن — للمغادرة المبكرة استخدم «تسجيل المغادرة» مع تاريخ المغادرة الفعلي."
        )

    ci = new_check_in or booking.check_in
    co = new_check_out or booking.check_out
    if co <= ci:
        raise BookingError("تاريخ المغادرة يجب أن يكون بعد تاريخ الوصول.")

    if new_check_in and new_check_in > booking.check_in:
        raise BookingError(
            "لتأجيل الوصول استخدم تاريخاً قبل الموعد الحالي، أو أنشئ حجزاً جديداً."
        )
    if new_check_out and new_check_out >= booking.check_out:
        raise BookingError(
            "لتمديد الإقامة استخدم «تمديد الإقامة»، أو للمغادرة المبكرة اختر تاريخاً قبل الموعد الحالي."
        )

    room_id = booking.room_id
    if room_id and has_room_conflict(
        db,
        room_id=room_id,
        check_in=ci,
        check_out=co,
        exclude_booking_id=booking.id,
    ):
        conflict = first_room_conflict(
            db,
            room_id=room_id,
            check_in=ci,
            check_out=co,
            exclude_booking_id=booking.id,
        )
        if conflict:
            raise BookingError(
                "الشقة غير متاحة في الفترة الجديدة — "
                f"تعارض مع حجز {conflict.reference} "
                f"({conflict.check_in} → {conflict.check_out})."
            )
        raise BookingError("الشقة غير متاحة في الفترة الجديدة.")

    if not new_check_in and not new_check_out:
        raise BookingError("حدّد تاريخ وصول أو مغادرة جديد.")

    old_ci, old_co = booking.check_in, booking.check_out
    if ci == old_ci and co == old_co:
        raise BookingError("التواريخ المدخلة مطابقة للمواعيد الحالية.")

    booking.check_in = ci
    booking.check_out = co
    booking.scheduled_check_out = co
    _apply_stay_pricing(db, booking)

    if new_check_in and new_check_in < old_ci:
        log_audit(
            db,
            entity_type="booking",
            entity_id=booking.id,
            action="early_arrival",
            field_name="check_in",
            old_value=str(old_ci),
            new_value=str(ci),
            user_id=user_id,
        )
    if new_check_out and new_check_out < old_co:
        log_audit(
            db,
            entity_type="booking",
            entity_id=booking.id,
            action="early_departure",
            field_name="check_out",
            old_value=str(old_co),
            new_value=str(co),
            user_id=user_id,
        )
    db.flush()
    return booking


def _is_checkin_assignment(assignment: HotelBookingRoomAssignment) -> bool:
    reason = (assignment.reason or "").strip()
    return "Check-in" in reason or "تعيين غرفة" in reason


def recompute_accommodation_through(
    db: Session, booking: HotelBooking, through_date: date
) -> Decimal:
    """إعادة حساب إجمالي الإقامة حتى تاريخ معيّن (مغادرة مبكرة أو تبديل شقة)."""
    stay_start = getattr(booking, "first_chargeable_night", None) or booking.check_in
    if through_date <= stay_start:
        return Decimal("0")

    transfers = sorted(
        [a for a in booking.room_assignments if not _is_checkin_assignment(a)],
        key=lambda a: (
            a.effective_date or (a.created_at.date() if a.created_at else date.today()),
            a.id,
        ),
    )

    total = Decimal("0")
    cursor = stay_start
    current_room_id = booking.room_id

    if transfers and transfers[0].from_room_id:
        current_room_id = transfers[0].from_room_id

    for assignment in transfers:
        eff = assignment.effective_date or (
            assignment.created_at.date() if assignment.created_at else date.today()
        )
        eff = max(cursor, min(eff, through_date))
        if eff > cursor and current_room_id:
            room = db.get(HotelRoom, current_room_id)
            rt_id = room.room_type_id if room else booking.room_type_id
            total += accommodation_segment_for_room(
                db, room=room, room_type_id=rt_id, seg_in=cursor, seg_out=eff
            )
        cursor = eff
        current_room_id = assignment.to_room_id

    if cursor < through_date:
        room = db.get(HotelRoom, current_room_id or booking.room_id) if (
            current_room_id or booking.room_id
        ) else None
        rt_id = booking.room_type_id
        if room and room.room_type_id:
            rt_id = room.room_type_id
        total += accommodation_segment_for_room(
            db, room=room, room_type_id=rt_id, seg_in=cursor, seg_out=through_date
        )

    if not transfers and not total:
        room = db.get(HotelRoom, booking.room_id) if booking.room_id else None
        total = accommodation_segment_for_room(
            db,
            room=room,
            room_type_id=booking.room_type_id,
            seg_in=stay_start,
            seg_out=through_date,
        )

    return total.quantize(Decimal("0.001"))


def _room_change_pricing(
    db: Session,
    booking: HotelBooking,
    *,
    new_room: HotelRoom,
    transfer_date: date | None = None,
) -> tuple[Decimal, Decimal, Decimal]:
    """(إجمالي الإقامة الجديد، فرق السعر، متوسط السعر الليلي)."""
    old_total = Decimal(str(booking.accommodation_total or 0))
    old_rt = booking.room_type_id
    new_rt = new_room.room_type_id or old_rt
    old_room = db.get(HotelRoom, booking.room_id) if booking.room_id else None

    if booking.booking_status == BookingStatus.CHECKED_IN:
        eff = transfer_date or date.today()
        eff = max(booking.check_in, min(eff, booking.check_out))
        consumed = accommodation_segment_for_room(
            db, room=old_room, room_type_id=old_rt, seg_in=booking.check_in, seg_out=eff
        )
        remaining = accommodation_segment_for_room(
            db,
            room=new_room,
            room_type_id=new_rt,
            seg_in=eff,
            seg_out=booking.check_out,
        )
        new_total = (consumed + remaining).quantize(Decimal("0.001"))
    else:
        new_total = accommodation_segment_for_room(
            db,
            room=new_room,
            room_type_id=new_rt,
            seg_in=booking.check_in,
            seg_out=booking.check_out,
        )

    delta = (new_total - old_total).quantize(Decimal("0.001"))
    nights = max(1, booking.nights)
    avg_rate = (new_total / Decimal(nights)).quantize(Decimal("0.001"))
    return new_total, delta, avg_rate


def _migrate_unsettled_room_charges(
    db: Session, booking_id: int, new_room_id: int
) -> int:
    charges = list(
        db.scalars(
            select(RoomCharge).where(
                RoomCharge.booking_id == booking_id,
                RoomCharge.is_settled.is_(False),
            )
        ).all()
    )
    for rc in charges:
        rc.room_id = new_room_id
    return len(charges)


def available_rooms_for_change(db: Session, booking: HotelBooking) -> list[HotelRoom]:
    rooms = list(
        db.scalars(
            select(HotelRoom).where(HotelRoom.is_active.is_(True)).order_by(HotelRoom.number)
        ).all()
    )
    result: list[HotelRoom] = []
    for room in rooms:
        if room.id == booking.room_id:
            continue
        if room.physical_status in NOT_RENTABLE_STATUSES:
            continue
        if has_room_conflict(
            db,
            room_id=room.id,
            check_in=booking.check_in,
            check_out=booking.check_out,
            exclude_booking_id=booking.id,
        ):
            continue
        result.append(room)
    return result


def create_booking(
    db: Session,
    *,
    guest_name: str,
    guest_phone: str | None,
    guest_email: str | None,
    check_in: date,
    check_out: date,
    room_type_id: int,
    guest_id_number: str | None = None,
    guest_id_type: str | None = None,
    guest_address: str | None = None,
    guest_nationality: str | None = None,
    guest_type: GuestType = GuestType.INDIVIDUAL,
    record_kind: RecordKind = RecordKind.BOOKING,
    quotation_valid_until: date | None = None,
    quotation_notes: str | None = None,
    company_name: str | None = None,
    company_tax_id: str | None = None,
    company_address: str | None = None,
    company_contact_name: str | None = None,
    company_contact_phone: str | None = None,
    company_contact_email: str | None = None,
    room_id: int | None = None,
    adults: int = 1,
    children: int = 0,
    nightly_rate: Decimal | None = None,
    discount_amount: Decimal = Decimal("0"),
    source: BookingSource = BookingSource.RECEPTION,
    internal_notes: str | None = None,
    user_id: int | None = None,
    auto_confirm: bool = False,
    customer_id: int | None = None,
    company_customer_id: int | None = None,
    company_agreement_id: int | None = None,
    booking_payer: str | None = None,
    stay_payer: str | None = None,
    extras_payer: str | None = None,
    notify_to: str | None = None,
    is_tourism_agency: bool = False,
    tourism_commission_percent: Decimal | str | float = Decimal("0"),
    staying_guests: list[StayingGuestInput] | None = None,
    notify_created: bool = True,
    check_in_time: str | None = None,
    check_out_time: str | None = None,
) -> HotelBooking:
    is_quotation = record_kind == RecordKind.QUOTATION
    identity = _resolve_guest_fields(
        guest_type=guest_type,
        guest_name=guest_name,
        guest_phone=guest_phone,
        guest_email=guest_email,
        company_name=company_name,
        company_tax_id=company_tax_id,
        company_address=company_address,
        company_contact_name=company_contact_name,
        company_contact_phone=company_contact_phone,
        company_contact_email=company_contact_email,
    )
    name = identity["guest_name"]
    if check_out < check_in:
        raise BookingError("تاريخ المغادرة يجب أن يكون بعد الوصول.")
    rt = db.get(HotelRoomType, room_type_id)
    if rt is None or not rt.is_active:
        raise BookingError("نوع الغرفة غير صالح.")
    if adults > rt.capacity_adults + rt.capacity_children:
        raise BookingError("عدد الضيوف يتجاوز سعة نوع الغرفة.")

    if room_id is not None:
        room = db.get(HotelRoom, room_id)
        if room is None or (not is_quotation and not is_room_rentable(room)):
            raise BookingError("الغرفة غير جاهزة للحجز (تنظيف أو صيانة أو غير نشطة).")
        if not is_quotation:
            conflict = first_room_conflict(
                db, room_id=room_id, check_in=check_in, check_out=check_out
            )
            if conflict is not None:
                raise BookingError(
                    "الغرفة غير متاحة لأن هناك حجزاً متداخلاً "
                    f"من {conflict.check_in} إلى {conflict.check_out}. "
                    f"يجب أن تكون المغادرة قبل {conflict.check_in}."
                )
    elif not is_quotation and not available_rooms_for_type(
        db, room_type_id=room_type_id, check_in=check_in, check_out=check_out
    ):
        raise BookingError("لا توجد غرف متاحة من هذا النوع في التواريخ المختارة.")

    from modules.hotel.checkin_stay import booking_billing_start, booking_stay_units

    arrival_for_bill = None
    if check_in_time:
        from modules.hotel.checkin_stay import _combine_local

        arrival_for_bill = _combine_local(check_in, check_in_time, "14:00")
    billing_in = booking_billing_start(db, check_in=check_in, when=arrival_for_bill)
    units = booking_stay_units(
        db,
        check_in=check_in,
        check_out=check_out,
        check_in_time=check_in_time,
        check_out_time=check_out_time,
    )
    if units <= 0:
        raise BookingError("وقت المغادرة يجب أن يكون بعد وقت الوصول — أو زد المدة إلى نصف ليلة على الأقل.")
    rate = nightly_rate
    if rate is not None:
        rate = Decimal(str(rate)).quantize(Decimal("0.001"))
        acc_total = (rate * units).quantize(Decimal("0.001"))
    else:
        out_for_rate = check_out if check_out > billing_in else billing_in + timedelta(days=1)
        rate = nightly_rate_for_stay(
            db, room_type_id=room_type_id, check_in=billing_in, check_out=out_for_rate
        )
        acc_total = (rate * units).quantize(Decimal("0.001"))

    policy = db.scalar(
        select(HotelCancellationPolicy).where(HotelCancellationPolicy.is_default.is_(True))
    )

    booking = HotelBooking(
        reference=_ref(quotation=is_quotation),
        property_id=rt.property_id,
        room_type_id=room_type_id,
        room_id=room_id,
        customer_id=customer_id,
        company_customer_id=company_customer_id,
        company_agreement_id=company_agreement_id,
        booking_payer=(booking_payer or "").strip().upper() or None,
        stay_payer=(stay_payer or booking_payer or "").strip().upper() or None,
        extras_payer=(extras_payer or booking_payer or "").strip().upper() or None,
        notify_to=(notify_to or "").strip().upper() or None,
        is_tourism_agency=bool(is_tourism_agency),
        tourism_commission_percent=Decimal(
            str(tourism_commission_percent or 0)
        ).quantize(Decimal("0.001")),
        guest_name=name,
        guest_phone=identity["guest_phone"],
        guest_email=identity["guest_email"],
        guest_id_number=(guest_id_number or "").strip() or None,
        guest_id_type=(guest_id_type or "").strip() or None,
        guest_address=(guest_address or "").strip() or None,
        guest_nationality=(guest_nationality or "").strip() or None,
        guest_type=guest_type,
        record_kind=record_kind,
        quotation_status=QuotationStatus.DRAFT if is_quotation else None,
        company_name=identity["company_name"],
        company_tax_id=identity["company_tax_id"],
        company_address=identity["company_address"],
        company_contact_name=identity["company_contact_name"],
        company_contact_phone=identity["company_contact_phone"],
        company_contact_email=identity["company_contact_email"],
        quotation_valid_until=quotation_valid_until,
        quotation_notes=(quotation_notes or "").strip() or None,
        check_in=check_in,
        check_out=check_out,
        scheduled_check_out=check_out,
        planned_check_in=check_in,
        first_chargeable_night=billing_in,
        adults=max(1, adults),
        children=max(0, children),
        nightly_rate=Decimal(str(rate)).quantize(Decimal("0.001")),
        discount_amount=Decimal(str(discount_amount)).quantize(Decimal("0.001")),
        accommodation_total=acc_total,
        booking_status=BookingStatus.PENDING,
        payment_status=BookingPaymentStatus.UNPAID,
        source=source,
        internal_notes=(internal_notes or "").strip() or None,
        created_by_id=user_id,
        cancellation_policy_id=policy.id if policy else None,
        access_token=secrets.token_urlsafe(32),
    )
    db.add(booking)
    db.flush()
    # عقد «من يدفع» + لقطة القواعد على الحجز
    if not is_quotation:
        try:
            from modules.hotel.company_agreement_service import attach_agreement_to_booking

            attach_agreement_to_booking(
                db,
                booking,
                agreement_id=company_agreement_id
                or getattr(booking, "company_agreement_id", None),
                stay_payer=stay_payer or booking_payer,
                extras_payer=extras_payer or booking_payer,
            )
        except Exception:  # noqa: BLE001
            import logging

            logging.getLogger("hotel.booking").exception(
                "attach company agreement failed booking=%s", booking.id
            )
    # ربط/وسم عميل الفندق برقم الهاتف حتى يظهر في قائمة عملاء الفندق
    try:
        from modules.customers.service import sync_customer_from_hotel_booking

        sync_customer_from_hotel_booking(db, booking)
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger("hotel.booking").exception(
            "sync hotel customer failed booking=%s", getattr(booking, "id", None)
        )
    sync_staying_guests(
        db,
        booking,
        staying_guests or [],
        fallback_name=name,
        fallback_phone=identity["guest_phone"],
        fallback_id_number=(guest_id_number or "").strip() or None,
        fallback_id_type=(guest_id_type or "").strip() or None,
        fallback_nationality=(guest_nationality or "").strip() or None,
        fallback_address=(guest_address or "").strip() or None,
    )
    note = "إنشاء عرض سعر" if is_quotation else "إنشاء حجز"
    _log_status(db, booking, BookingStatus.PENDING, user_id, note=note)
    log_audit(
        db,
        entity_type="booking",
        entity_id=booking.id,
        action="create_quotation" if is_quotation else "create",
        user_id=user_id,
    )
    if auto_confirm and not is_quotation:
        confirm_booking(db, booking.id, user_id=user_id)
        db.refresh(booking)
    elif not is_quotation and booking.room_id and booking.check_in > date.today():
        _keep_room_available_for_future_booking(db, booking, user_id=user_id)
    # لا ترسل «حجز جديد» إذا أُرسل «تأكيد» في نفس العملية — رسالة واحدة كافية
    if (
        notify_created
        and not is_quotation
        and source != BookingSource.ONLINE_STORE
        and booking.booking_status != BookingStatus.CONFIRMED
    ):
        try:
            from modules.notifications.hotel_hooks import emit_hotel_booking_created

            emit_hotel_booking_created(db, booking)
        except Exception:  # noqa: BLE001
            pass
    return booking


def _keep_room_available_for_future_booking(
    db: Session, booking: HotelBooking, *, user_id: int | None = None
) -> None:
    """الحجز المستقبلي لا يُخرِج الشقة من التأجير قبل يوم الوصول."""
    if not booking.room_id or booking.check_in <= date.today():
        return
    room = db.get(HotelRoom, booking.room_id)
    if room is None:
        return
    if room.physical_status == RoomPhysicalStatus.RESERVED:
        _log_room_status(
            db,
            room,
            RoomPhysicalStatus.AVAILABLE,
            user_id,
            "حجز مستقبلي — متاحة حتى موعد الوصول",
        )
    db.flush()


def confirm_booking(
    db: Session,
    booking_id: int,
    *,
    user_id: int | None = None,
    notify: bool = True,
) -> HotelBooking:
    booking = db.get(HotelBooking, booking_id)
    if booking is None:
        raise BookingError("الحجز غير موجود.")
    if booking.record_kind == RecordKind.QUOTATION:
        raise BookingError("عرض السعر ليس حجزاً — حوّله أولاً إلى حجز فعلي.")
    assert_booking_prepayment_met(db, booking)
    if booking.booking_status not in (BookingStatus.PENDING,):
        raise BookingError("لا يمكن تأكيد هذا الحجز.")
    if booking.room_id and has_room_conflict(
        db,
        room_id=booking.room_id,
        check_in=booking.check_in,
        check_out=booking.check_out,
        exclude_booking_id=booking.id,
    ):
        raise BookingError("الغرفة المحددة لم تعد متاحة.")
    old = booking.booking_status
    booking.booking_status = BookingStatus.CONFIRMED
    _log_status(db, booking, BookingStatus.CONFIRMED, user_id, from_status=old)
    log_audit(db, entity_type="booking", entity_id=booking.id, action="confirm", user_id=user_id)
    _keep_room_available_for_future_booking(db, booking, user_id=user_id)
    db.flush()
    if notify:
        try:
            from modules.notifications.hotel_hooks import emit_hotel_booking_confirmed

            emit_hotel_booking_confirmed(db, booking)
        except Exception:  # noqa: BLE001
            pass
    if booking.source == BookingSource.ONLINE_STORE:
        try:
            from modules.hotel.folio import build_folio
            from modules.receipt_whatsapp.service import send_hotel_receipt_whatsapp

            folio = build_folio(db, booking.id)
            send_hotel_receipt_whatsapp(db, booking, folio_total=folio.total)
        except Exception:  # noqa: BLE001
            pass
    return booking


def update_quotation_status(
    db: Session,
    booking_id: int,
    *,
    new_status: QuotationStatus,
    user_id: int | None = None,
    note: str | None = None,
) -> HotelBooking:
    booking = db.get(HotelBooking, booking_id)
    if booking is None:
        raise BookingError("السجل غير موجود.")
    if booking.record_kind != RecordKind.QUOTATION:
        raise BookingError("هذا السجل ليس عرض سعر.")
    current = booking.quotation_status or QuotationStatus.DRAFT
    if new_status == QuotationStatus.CONVERTED:
        raise BookingError("استخدم «تحويل إلى حجز» بدلاً من تغيير الحالة يدوياً.")
    allowed = QUOTATION_NEXT_STATUSES.get(current, ())
    if new_status not in allowed:
        raise BookingError(f"لا يمكن الانتقال من «{current.value}» إلى «{new_status.value}».")
    old = current
    booking.quotation_status = new_status
    log_audit(
        db,
        entity_type="booking",
        entity_id=booking.id,
        action="quotation_status",
        old_value=old.value,
        new_value=new_status.value,
        reason=(note or "").strip() or None,
        user_id=user_id,
    )
    db.flush()
    return booking


def convert_quotation_to_booking(
    db: Session,
    booking_id: int,
    *,
    user_id: int | None = None,
    auto_confirm: bool = True,
    require_payment: bool = True,
    payment_amount: Decimal | None = None,
    payment_method_id: int | None = None,
) -> HotelBooking:
    booking = db.get(HotelBooking, booking_id)
    if booking is None:
        raise BookingError("السجل غير موجود.")
    if booking.record_kind != RecordKind.QUOTATION:
        raise BookingError("هذا السجل حجز فعلي بالفعل.")
    if booking.quotation_status not in (
        QuotationStatus.ACCEPTED,
        QuotationStatus.UNDER_REVIEW,
        QuotationStatus.SENT,
    ):
        raise BookingError("يجب أن تكون حالة العرض «موافقة الشركة» أو قيد المراجعة قبل التحويل.")
    if booking.quotation_valid_until and booking.quotation_valid_until < date.today():
        raise BookingError("انتهت صلاحية عرض السعر — أنشئ عرضاً جديداً.")

    if booking.room_id:
        room = db.get(HotelRoom, booking.room_id)
        if room is None or not room.is_active:
            raise BookingError("الشقة المحددة غير متاحة.")
        if not is_room_rentable(room):
            raise BookingError("الشقة غير جاهزة (تنظيف أو صيانة).")
        if has_room_conflict(
            db,
            room_id=booking.room_id,
            check_in=booking.check_in,
            check_out=booking.check_out,
            exclude_booking_id=booking.id,
        ):
            raise BookingError("الشقة لم تعد متاحة في هذه الفترة — اختر شقة أخرى أو عدّل التواريخ.")
    elif not available_rooms_for_type(
        db,
        room_type_id=booking.room_type_id or 0,
        check_in=booking.check_in,
        check_out=booking.check_out,
        exclude_booking_id=booking.id,
    ):
        raise BookingError("لا توجد شقق متاحة من هذا النوع في التواريخ المحددة.")

    booking.record_kind = RecordKind.BOOKING
    booking.quotation_status = QuotationStatus.CONVERTED
    log_audit(
        db,
        entity_type="booking",
        entity_id=booking.id,
        action="convert_quotation",
        user_id=user_id,
    )
    db.flush()

    had_payment = False
    if require_payment:
        validate_booking_prepayment(
            db,
            amount=payment_amount or 0,
            payment_method_id=payment_method_id,
            room_type_id=booking.room_type_id or 0,
            check_in=booking.check_in,
            check_out=booking.check_out,
            discount_amount=booking.discount_amount,
            nightly_rate=booking.nightly_rate,
        )
        amt = Decimal(str(payment_amount or 0)).quantize(Decimal("0.001"))
        if amt > 0:
            record_payment(
                db,
                booking.id,
                amount=amt,
                payment_method_id=payment_method_id,
                is_deposit=True,
                user_id=user_id,
                note="دفع عند تحويل عرض السعر إلى حجز",
            )
            had_payment = True

    if auto_confirm:
        # إن وُجدت رسالة سداد فلا نكرر برسالة تأكيد في نفس العملية
        confirm_booking(db, booking.id, user_id=user_id, notify=not had_payment)
        db.refresh(booking)

    # رسالة واحدة: سداد أو تأكيد أو إنشاء — لا الثلاثة معاً
    if not had_payment and booking.booking_status != BookingStatus.CONFIRMED:
        try:
            from modules.notifications.hotel_hooks import emit_hotel_booking_created

            emit_hotel_booking_created(db, booking)
        except Exception:  # noqa: BLE001
            pass
    db.flush()
    return booking


def check_in_booking(
    db: Session,
    booking_id: int,
    *,
    room_id: int,
    user_id: int | None = None,
    actual_arrival: date | None = None,
    actual_arrival_at: datetime | None = None,
    confirm_after_midnight: bool = False,
    waive_previous_night: bool = False,
    waive_reason: str | None = None,
    override_early_block: bool = False,
) -> HotelBooking:
    from app.datetime_local import now_local
    from modules.hotel.checkin_stay import compute_arrival_charge_plan, to_local

    booking = db.get(HotelBooking, booking_id)
    if booking is None:
        raise BookingError("الحجز غير موجود.")
    if booking.record_kind == RecordKind.QUOTATION:
        raise BookingError("عرض السعر ليس حجزاً فعلياً — حوّله أولاً.")
    assert_booking_prepayment_met(db, booking)
    if booking.booking_status == BookingStatus.CANCELLED:
        raise BookingError("لا يمكن Check-in لحجز ملغى.")
    if booking.booking_status not in (BookingStatus.CONFIRMED, BookingStatus.PENDING):
        raise BookingError("الحجز ليس في حالة تسمح بالتسكين.")

    today = date.today()
    scheduled_in = booking.check_in
    booked_first = getattr(booking, "first_chargeable_night", None) or scheduled_in
    explicit_arrival = actual_arrival_at is not None or actual_arrival is not None
    # حفظ الموعد المخطط مرة واحدة
    if getattr(booking, "planned_check_in", None) is None:
        booking.planned_check_in = scheduled_in

    # وقت الوصول الفعلي (محلي)
    if actual_arrival_at is not None:
        act_at = actual_arrival_at
    elif actual_arrival is not None:
        # تاريخ فقط — افترض الآن إذا اليوم، وإلا منتصف نهار التسكين
        if actual_arrival == today:
            act_at = now_local()
        else:
            act_at = datetime(
                actual_arrival.year,
                actual_arrival.month,
                actual_arrival.day,
                14,
                0,
                tzinfo=now_local().tzinfo,
            )
    else:
        # زر «تسكين»: سجّل الوقت الفعلي دون إعادة حساب الليالي المحجوزة
        act_at = now_local()

    act_at = to_local(act_at)
    stay_changed = False
    plan_reason = "BOOKED_STAY"
    first_night = booked_first
    effective_in = scheduled_in

    if explicit_arrival or waive_previous_night or confirm_after_midnight:
        plan = compute_arrival_charge_plan(
            db,
            actual_at=act_at,
            scheduled_check_in=scheduled_in,
            scheduled_check_out=booking.check_out,
            nightly_rate=booking.nightly_rate,
            confirm_after_midnight=confirm_after_midnight,
            force_waive_previous_night=bool(waive_previous_night),
        )
        if plan.needs_reception_confirm:
            raise BookingError(
                "CONFIRM_AFTER_MIDNIGHT::" + plan.confirm_message
            )
        if plan.block_checkin and not override_early_block:
            raise BookingError(plan.block_message or "الدخول المبكر غير مسموح.")

        if waive_previous_night:
            log_audit(
                db,
                entity_type="booking",
                entity_id=booking.id,
                action="waive_previous_night",
                field_name="first_chargeable_night",
                old_value=str(booked_first),
                new_value=str(plan.first_chargeable_night),
                reason=(waive_reason or "").strip() or "إلغاء احتساب ليلة سابقة",
                user_id=user_id,
            )
            first_night = plan.first_chargeable_night
            effective_in = plan.billing_check_in
            stay_changed = True
        else:
            # يُسمح بإضافة ليلة (وصول مبكر) — لا يُختصر حجز مدفوع مسبقاً
            first_night = plan.first_chargeable_night
            if first_night is None or first_night > booked_first:
                first_night = booked_first
            effective_in = plan.billing_check_in
            if effective_in is None or effective_in > scheduled_in:
                effective_in = scheduled_in
            if first_night < booked_first or effective_in < scheduled_in:
                stay_changed = True
        plan_reason = plan.reason
    if effective_in >= booking.check_out:
        raise BookingError("تاريخ الوصول يجب أن يكون قبل تاريخ المغادرة.")

    if has_room_conflict(
        db,
        room_id=room_id,
        check_in=effective_in,
        check_out=booking.check_out,
        exclude_booking_id=booking.id,
    ):
        conflict = first_room_conflict(
            db,
            room_id=room_id,
            check_in=effective_in,
            check_out=booking.check_out,
            exclude_booking_id=booking.id,
        )
        if conflict:
            raise BookingError(
                "الغرفة مشغولة في هذه الفترة — "
                f"حجز {conflict.reference} ({conflict.check_in} → {conflict.check_out})."
            )
        raise BookingError("الغرفة مشغولة في هذه الفترة.")

    room = db.get(HotelRoom, room_id)
    if room is None or not room.is_active:
        raise BookingError("الغرفة غير صالحة.")
    if room.physical_status in NOT_RENTABLE_STATUSES:
        raise BookingError("الغرفة غير جاهزة للتسكين (تنظيف أو صيانة).")

    if effective_in != scheduled_in:
        log_audit(
            db,
            entity_type="booking",
            entity_id=booking.id,
            action="arrival_billing_adjust",
            field_name="check_in",
            old_value=str(scheduled_in),
            new_value=str(effective_in),
            user_id=user_id,
            reason=plan_reason,
        )
        booking.check_in = effective_in

    booking.first_chargeable_night = first_night

    old_room_id = booking.room_id
    if old_room_id and old_room_id != room_id:
        old_room = db.get(HotelRoom, old_room_id)
        if old_room:
            _log_room_status(db, old_room, RoomPhysicalStatus.AVAILABLE, user_id, "تغيير غرفة")
        db.add(
            HotelBookingRoomAssignment(
                booking_id=booking.id,
                from_room_id=old_room_id,
                to_room_id=room_id,
                reason="Check-in / تعيين غرفة",
                assigned_by_id=user_id,
            )
        )

    booking.room_id = room_id
    booking.booking_status = BookingStatus.CHECKED_IN
    # الاحتفاظ بالوقت الفعلي كما هو (لا نُزيّف التاريخ)
    try:
        booking.checked_in_at = act_at.astimezone(timezone.utc)
    except Exception:  # noqa: BLE001
        booking.checked_in_at = datetime.now(timezone.utc)
    booking.checked_in_by_id = user_id
    booking.access_token = secrets.token_urlsafe(32)
    if stay_changed:
        _apply_stay_pricing(db, booking, room=room)

    # رسوم دخول مبكر (إن وُجدت) — فقط عند وصول صريح
    early_fee = Decimal("0")
    early_label = ""
    if explicit_arrival or waive_previous_night or confirm_after_midnight:
        early_fee = getattr(plan, "early_fee_amount", Decimal("0")) or Decimal("0")
        early_label = getattr(plan, "early_fee_label", "") or ""
    if early_fee > Decimal("0.0005"):
        try:
            add_booking_service(
                db,
                booking.id,
                name_ar=early_label or "رسوم دخول مبكر",
                quantity=Decimal("1"),
                unit_price=early_fee,
                notes=f"early_checkin:{plan_reason}",
                user_id=user_id,
                notify=False,
                service_code="EARLY_CHECKIN",
            )
        except Exception:  # noqa: BLE001
            pass

    name, _ = primary_staying_guest_contact(booking)
    room.guest_name = name or booking.guest_name
    _log_room_status(db, room, RoomPhysicalStatus.OCCUPIED, user_id, "Check-in")
    _log_status(db, booking, BookingStatus.CHECKED_IN, user_id, from_status=BookingStatus.CONFIRMED)
    log_audit(
        db,
        entity_type="booking",
        entity_id=booking.id,
        action="check_in",
        new_value=(
            f"actual={act_at.isoformat()} first_night={first_night} "
            f"billing_in={effective_in} reason={plan_reason}"
        ),
        user_id=user_id,
    )
    db.flush()
    try:
        from modules.notifications.hotel_hooks import emit_hotel_check_in_welcome

        emit_hotel_check_in_welcome(db, booking)
    except Exception:  # noqa: BLE001
        pass
    return booking


def release_room_after_departed_booking(
    db: Session,
    booking: HotelBooking,
    *,
    user_id: int | None = None,
    note: str = "تحرير شقة بعد مغادرة مسجّلة",
) -> bool:
    """
    إن بقيت الشقة OCCUPIED بعد حجز مغادر (أو بدون نزيل مسكّن)، حوّلها إلى DIRTY.
    يُرجع True إذا تم تعديل حالة الشقة.
    """
    if not booking.room_id:
        return False
    room = db.get(HotelRoom, booking.room_id)
    if room is None:
        return False
    # لا تحرّر إن وُجد نزيل آخر مسكّن حالياً على نفس الشقة
    other_in = db.scalar(
        select(HotelBooking.id).where(
            HotelBooking.room_id == room.id,
            HotelBooking.booking_status == BookingStatus.CHECKED_IN,
            HotelBooking.id != booking.id,
        ).limit(1)
    )
    if other_in:
        return False
    changed = False
    if room.physical_status == RoomPhysicalStatus.OCCUPIED:
        _log_room_status(db, room, RoomPhysicalStatus.DIRTY, user_id, note)
        changed = True
    if room.guest_name:
        room.guest_name = None
        changed = True
    if changed:
        db.flush()
    return changed


def heal_rooms_stuck_occupied_without_guest(db: Session, *, user_id: int | None = None) -> int:
    """شقق OCCUPIED بلا حجز CHECKED_IN → DIRTY (إصلاح بيانات لايف عالقة)."""
    rooms = list(
        db.scalars(
            select(HotelRoom).where(HotelRoom.physical_status == RoomPhysicalStatus.OCCUPIED)
        ).all()
    )
    fixed = 0
    for room in rooms:
        still_in = db.scalar(
            select(HotelBooking.id).where(
                HotelBooking.room_id == room.id,
                HotelBooking.booking_status == BookingStatus.CHECKED_IN,
            ).limit(1)
        )
        if still_in:
            continue
        _log_room_status(
            db,
            room,
            RoomPhysicalStatus.DIRTY,
            user_id,
            "إصلاح: مشغولة بلا نزيل مسكّن",
        )
        room.guest_name = None
        fixed += 1
    if fixed:
        db.flush()
    return fixed


def check_out_booking(
    db: Session,
    booking_id: int,
    *,
    user_id: int | None = None,
    allow_balance: bool = False,
    post_as_debt: bool = False,
    write_off_balance: bool = False,
    debt_reminder_at: date | None = None,
    debt_reminder_time: str | None = None,
    debt_note: str | None = None,
    actual_departure: date | None = None,
    credit_disposition: str | None = None,
    credit_refund_method_id: int | None = None,
) -> HotelBooking:
    from modules.hotel.booking_debts import (
        create_checkout_debt,
        create_uncollectible_checkout_debt,
    )
    from modules.hotel.departure_settlement import preview_departure_settlement
    from modules.hotel.folio import booking_balance_due

    booking = db.get(HotelBooking, booking_id)
    if booking is None:
        raise BookingError("الحجز غير موجود.")
    # مغادرة مكررة (رجوع المتصفح / إعادة إرسال): اعتبرها ناجحة وحرّر الشقة إن علقت
    if booking.booking_status == BookingStatus.CHECKED_OUT:
        release_room_after_departed_booking(
            db,
            booking,
            user_id=user_id,
            note="مغادرة مكررة — تأكيد تحرير الشقة",
        )
        return booking
    if booking.booking_status != BookingStatus.CHECKED_IN:
        st = (
            booking.booking_status.value
            if hasattr(booking.booking_status, "value")
            else str(booking.booking_status)
        )
        labels = {
            "PENDING": "معلّق",
            "CONFIRMED": "مؤكّد (لم يُسكَّن بعد)",
            "CANCELLED": "ملغى",
            "CHECKED_OUT": "مغادر",
        }
        raise BookingError(
            f"لا يمكن تسجيل المغادرة — حالة الحجز الآن: {labels.get(st, st)}. "
            "المغادرة متاحة فقط للحجز المسجّل دخوله (مسكّن)."
        )

    try:
        from modules.hotel.late_checkout import ensure_overstay_nights_caught_up

        ensure_overstay_nights_caught_up(db, booking, notify=True)
        db.refresh(booking)
    except Exception:  # noqa: BLE001
        pass

    settlement_snapshot = None
    if actual_departure and actual_departure < booking.check_out:
        # يُسمح بمغادرة نفس يوم الوصول (بعد ساعات قليلة مثلاً)
        if actual_departure < booking.check_in:
            raise BookingError("تاريخ المغادرة الفعلي لا يمكن أن يكون قبل تاريخ الوصول.")
        old_out = booking.check_out
        old_acc = Decimal(str(booking.accommodation_total or 0)).quantize(Decimal("0.001"))
        if booking.scheduled_check_out is None:
            booking.scheduled_check_out = old_out
        try:
            settlement_snapshot = preview_departure_settlement(
                db, booking, actual_departure=actual_departure
            )
        except BookingError:
            settlement_snapshot = None
        # تاريخ المغادرة الفعلي يُحفظ كما هو (تحرير الشقة)،
        # أما التسعير: نفس يوم الوصول = ليلة واحدة مستهلكة على الأقل.
        booking.check_out = actual_departure
        # تخفيض نسبي من الإقامة المسجّلة — لا إعادة تسعير من سعر الغرفة (يمنع رصيداً وهمياً)
        from modules.hotel.departure_settlement import accommodation_after_early_departure

        new_total = accommodation_after_early_departure(
            booking, actual_departure=actual_departure, old_check_out=old_out
        )
        booking.accommodation_total = new_total
        nights_left = max(0, (actual_departure - booking.check_in).days)
        if actual_departure == booking.check_in:
            nights_left = 1
        if nights_left > 0 and new_total > 0:
            booking.nightly_rate = (new_total / Decimal(nights_left)).quantize(
                Decimal("0.001")
            )
        _recalc_payment_status(db, booking)
        log_audit(
            db,
            entity_type="booking",
            entity_id=booking.id,
            action="early_departure",
            field_name="check_out",
            old_value=str(old_out),
            new_value=str(actual_departure),
            user_id=user_id,
            reason=(
                "مغادرة بنفس يوم الوصول — تُحسب ليلة واحدة (استهلاك الشقة)"
                if actual_departure == booking.check_in
                else None
            ),
        )
        new_acc = Decimal(str(booking.accommodation_total or 0)).quantize(Decimal("0.001"))
        saved_acc = (
            settlement_snapshot.accommodation_saved
            if settlement_snapshot is not None
            else (old_acc - new_acc).quantize(Decimal("0.001"))
        )
        if saved_acc > Decimal("0.0005"):
            cancelled_n = (
                settlement_snapshot.cancelled_nights if settlement_snapshot else None
            )
            log_audit(
                db,
                entity_type="booking",
                entity_id=booking.id,
                action="charge_reduction",
                field_name="accommodation_total",
                old_value=str(
                    settlement_snapshot.accommodation_booked
                    if settlement_snapshot is not None
                    else old_acc
                ),
                new_value=str(saved_acc),
                reason=(
                    f"تخفيض ليالي ملغاة ({cancelled_n} ليلة)"
                    if cancelled_n is not None
                    else "تخفيض مستحقات الإقامة بعد مغادرة مبكرة"
                ),
                user_id=user_id,
            )
    elif actual_departure and actual_departure > booking.check_out:
        # مغادرة بعد الموعد (Overstay) — لا تُرفض؛ الإقامة المسجّلة تبقى
        try:
            settlement_snapshot = preview_departure_settlement(
                db, booking, actual_departure=actual_departure
            )
        except BookingError:
            settlement_snapshot = None
        log_audit(
            db,
            entity_type="booking",
            entity_id=booking.id,
            action="late_departure",
            field_name="actual_departure",
            old_value=str(booking.check_out),
            new_value=str(actual_departure),
            reason="مغادرة بعد الموعد المجدول (Overstay) — دون رفض التسجيل",
            user_id=user_id,
        )

    # رسوم Overstay أثناء فترة السماح (مجاني/نصف/كامل/ثابت) — قبل حساب المتبقي
    try:
        from modules.hotel.late_checkout import apply_grace_late_fee_on_checkout

        apply_grace_late_fee_on_checkout(db, booking, user_id=user_id)
    except Exception:  # noqa: BLE001
        pass

    balance = booking_balance_due(db, booking.id)
    if balance > Decimal("0.001"):
        # لا يُسمح بأكثر من مسلك واحد
        modes = sum(
            1
            for f in (bool(post_as_debt), bool(write_off_balance), bool(allow_balance))
            if f
        )
        if modes > 1:
            raise BookingError("اختر خياراً واحداً فقط لمعالجة المتبقي.")
        if settlement_snapshot is None and actual_departure:
            try:
                settlement_snapshot = preview_departure_settlement(
                    db, booking, actual_departure=actual_departure
                )
            except BookingError:
                try:
                    settlement_snapshot = preview_departure_settlement(
                        db, booking, actual_departure=booking.check_out
                    )
                except BookingError:
                    settlement_snapshot = None
        snap_json = (
            settlement_snapshot.to_json() if settlement_snapshot else None
        )
        if write_off_balance:
            create_uncollectible_checkout_debt(
                db,
                booking,
                balance,
                user_id=user_id,
                note=debt_note,
                settlement_json=snap_json,
            )
        elif post_as_debt:
            create_checkout_debt(
                db,
                booking,
                balance,
                user_id=user_id,
                note=debt_note,
                settlement_json=snap_json,
                reminder_at=debt_reminder_at,
                reminder_time=debt_reminder_time,
            )
        elif allow_balance:
            # مسار قديم — يُرفض من الراوتر لغير الأدمن ويُفضَّل write_off
            pass
        else:
            raise BookingError(f"لا يمكن Check-out — متبقٍ {balance} د.ل على الحساب.")

    # فائض عند الإقفال: يُحتسب على المستحقات أثناء الإقامة، وعند المغادرة يُسترد نقداً
    # (لا ترحيل إلى محفظة العميل — الرصيد جهة الشركة أو النزيل فقط)
    from modules.hotel.folio import build_guest_account

    credit = build_guest_account(db, booking.id).amount_credit
    credit = Decimal(str(credit or 0)).quantize(Decimal("0.001"))
    if credit > Decimal("0.001"):
        mode = (credit_disposition or "").strip().lower()
        if mode == "wallet":
            raise BookingError(
                f"يوجد رصيد قابل للإرجاع ({credit} د.ل) — يُسترد نقداً بإيصال صرف، "
                "ولا يُرحَّل إلى المحفظة."
            )
        if mode not in ("refund", ""):
            raise BookingError(
                f"يوجد رصيد قابل للإرجاع ({credit} د.ل) — اختر الاسترداد النقدي ووسيلة الصرف."
            )
        if not credit_refund_method_id:
            raise BookingError("اختر وسيلة صرف الاسترداد (كاش أو مصرف) لتسوية الرصيد الفائض.")
        refund_booking_credit(
            db,
            booking,
            amount=credit,
            payment_method_id=int(credit_refund_method_id),
            user_id=user_id,
            reason="استرداد رصيد عند تسجيل المغادرة",
            hotel_shift_id=None,
            employee_id=None,
        )

    try:
        from modules.hotel.follow_up import clear_claim_wa_if_settled

        booking.claim_wa_until_paid = False
        booking.follow_up_at = None
        clear_claim_wa_if_settled(db, booking)
    except Exception:  # noqa: BLE001
        pass

    booking.booking_status = BookingStatus.CHECKED_OUT
    booking.checked_out_at = datetime.now(timezone.utc)
    booking.checked_out_by_id = user_id
    if booking.room_id:
        room = db.get(HotelRoom, booking.room_id)
        if room:
            room.guest_name = None
            _log_room_status(db, room, RoomPhysicalStatus.DIRTY, user_id, "Check-out")
    _log_status(db, booking, BookingStatus.CHECKED_OUT, user_id, from_status=BookingStatus.CHECKED_IN)
    log_audit(
        db,
        entity_type="booking",
        entity_id=booking.id,
        action="check_out",
        user_id=user_id,
        reason=(
            "دين غير قابل للتحصيل (مشطوب)"
            if write_off_balance and balance > 0
            else (
                "دين مُرحّل"
                if post_as_debt and balance > 0
                else ("متبقٍ مسموح" if allow_balance and balance > 0 else None)
            )
        ),
        new_value=settlement_snapshot.to_json() if settlement_snapshot else None,
    )
    db.flush()
    return booking


def cancel_booking(
    db: Session,
    booking_id: int,
    *,
    user_id: int | None = None,
    reason: str | None = None,
    force_late: bool = False,
) -> HotelBooking:
    from modules.hotel.cancellation_flow import (
        CancellationFlowError,
        cancel_booking as _cancel_flow,
    )

    try:
        return _cancel_flow(
            db,
            booking_id,
            user_id=user_id,
            reason=reason,
            force_late=force_late,
        )
    except CancellationFlowError as exc:
        raise BookingError(str(exc)) from exc


def mark_no_show(
    db: Session,
    booking_id: int,
    *,
    user_id: int | None = None,
    automatic: bool = False,
    reason: str | None = None,
) -> HotelBooking:
    from modules.hotel.cancellation_flow import (
        CancellationFlowError,
        mark_no_show as _no_show_flow,
    )

    try:
        return _no_show_flow(
            db,
            booking_id,
            user_id=user_id,
            automatic=automatic,
            reason=reason,
        )
    except CancellationFlowError as exc:
        raise BookingError(str(exc)) from exc


def extend_stay(
    db: Session,
    booking_id: int,
    new_check_out: date,
    *,
    user_id: int | None = None,
) -> HotelBooking:
    booking = db.get(HotelBooking, booking_id)
    if booking is None:
        raise BookingError("الحجز غير موجود.")
    if booking.booking_status not in (BookingStatus.CONFIRMED, BookingStatus.CHECKED_IN):
        raise BookingError("لا يمكن تمديد هذا الحجز.")
    if new_check_out <= booking.check_out:
        raise BookingError("تاريخ المغادرة الجديد يجب أن يكون بعد الحالي.")
    if booking.room_id and has_room_conflict(
        db,
        room_id=booking.room_id,
        check_in=booking.check_in,
        check_out=new_check_out,
        exclude_booking_id=booking.id,
    ):
        raise BookingError("الغرفة محجوزة في الأيام الإضافية.")

    old_out = booking.check_out
    booking.check_out = new_check_out
    booking.scheduled_check_out = new_check_out
    # نفس مسار التسعير المستخدم في معاينة المغادرة (سعر الشقة / تغيير الغرفة)
    _apply_stay_pricing(db, booking)
    log_audit(
        db,
        entity_type="booking",
        entity_id=booking.id,
        action="extend",
        field_name="check_out",
        old_value=str(old_out),
        new_value=str(new_check_out),
        user_id=user_id,
    )
    db.flush()
    return booking


def change_departure_date(
    db: Session,
    booking_id: int,
    new_check_out: date,
    *,
    user_id: int | None = None,
    reason: str | None = None,
) -> tuple[HotelBooking, int]:
    """تقديم موعد المغادرة مع إعادة تسعير الإقامة فقط — مثل التمديد.

    لا يُصرف ولا يُقبض هنا. إن بقي متبقٍ يُحصَّل بإيصال قبض،
    وإن ظهر رصيد دائن (ليالٍ مدفوعة أُلغيت) يُرجع بإيصال صرف.
    """
    from app.datetime_local import now_local

    booking = db.get(HotelBooking, booking_id)
    if booking is None:
        raise BookingError("الحجز غير موجود.")
    if booking.record_kind == RecordKind.QUOTATION:
        raise BookingError("عدّل التواريخ من عرض السعر أو حوّله إلى حجز.")
    if booking.booking_status not in (
        BookingStatus.PENDING,
        BookingStatus.CONFIRMED,
        BookingStatus.CHECKED_IN,
    ):
        raise BookingError("لا يمكن تغيير موعد المغادرة لهذا الحجز.")
    if new_check_out >= booking.check_out:
        raise BookingError(
            "لتقديم المغادرة اختر تاريخاً قبل الموعد الحالي. للتمديد استخدم «تمديد الإقامة»."
        )
    stay_start = getattr(booking, "first_chargeable_night", None) or booking.check_in
    if new_check_out <= stay_start:
        raise BookingError("تاريخ المغادرة يجب أن يبقى بعد بداية الإقامة (ليلة واحدة على الأقل).")
    today = now_local().date()
    if booking.booking_status == BookingStatus.CHECKED_IN and new_check_out < today:
        raise BookingError("لا يمكن جعل موعد المغادرة قبل اليوم — سجّل المغادرة إن غادر النزيل.")

    old_out = booking.check_out
    old_nights = max(0, int(booking.nights or 0))

    booking.check_out = new_check_out
    booking.scheduled_check_out = new_check_out
    _apply_stay_pricing(db, booking)
    new_nights = max(0, int(booking.nights or 0))
    cancelled = max(0, old_nights - new_nights)

    note = (reason or "").strip()
    audit_reason = f"ليالي ملغاة={cancelled}"
    if note:
        audit_reason = f"{audit_reason} — {note}"

    log_audit(
        db,
        entity_type="booking",
        entity_id=booking.id,
        action="change_departure",
        field_name="check_out",
        old_value=str(old_out),
        new_value=str(new_check_out),
        user_id=user_id,
        reason=audit_reason,
    )

    db.flush()
    return booking, cancelled


def change_room(
    db: Session,
    booking_id: int,
    new_room_id: int,
    *,
    reason: str | None = None,
    transfer_date: date | None = None,
    user_id: int | None = None,
) -> HotelBooking:
    booking = db.get(HotelBooking, booking_id)
    if booking is None:
        raise BookingError("الحجز غير موجود.")
    if booking.booking_status not in (BookingStatus.CONFIRMED, BookingStatus.CHECKED_IN):
        raise BookingError("لا يمكن تغيير الغرفة لهذا الحجز.")
    if booking.room_id == new_room_id:
        raise BookingError("الشقة المختارة هي نفس الشقة الحالية.")
    if has_room_conflict(
        db,
        room_id=new_room_id,
        check_in=booking.check_in,
        check_out=booking.check_out,
        exclude_booking_id=booking.id,
    ):
        raise BookingError("الغرفة الجديدة غير متاحة.")

    new_room = db.get(HotelRoom, new_room_id)
    if new_room is None or not new_room.is_active:
        raise BookingError("الغرفة الجديدة غير صالحة.")

    eff_date = transfer_date
    if booking.booking_status == BookingStatus.CHECKED_IN:
        eff_date = transfer_date or date.today()
        eff_date = max(booking.check_in, min(eff_date, booking.check_out))

    new_total, price_delta, avg_rate = _room_change_pricing(
        db, booking, new_room=new_room, transfer_date=eff_date
    )

    old_room_id = booking.room_id
    if old_room_id:
        old_room = db.get(HotelRoom, old_room_id)
        if old_room:
            st = (
                RoomPhysicalStatus.AVAILABLE
                if booking.booking_status == BookingStatus.CONFIRMED
                else RoomPhysicalStatus.DIRTY
            )
            _log_room_status(db, old_room, st, user_id, reason or "تغيير غرفة")

    db.add(
        HotelBookingRoomAssignment(
            booking_id=booking.id,
            from_room_id=old_room_id,
            to_room_id=new_room_id,
            reason=reason,
            effective_date=eff_date,
            price_delta=price_delta,
            assigned_by_id=user_id,
        )
    )
    booking.room_id = new_room_id
    if new_room.room_type_id:
        booking.room_type_id = new_room.room_type_id
    booking.accommodation_total = new_total
    booking.nightly_rate = avg_rate
    _recalc_payment_status(db, booking)
    _migrate_unsettled_room_charges(db, booking.id, new_room_id)

    if booking.booking_status == BookingStatus.CHECKED_IN:
        _log_room_status(db, new_room, RoomPhysicalStatus.OCCUPIED, user_id, "تغيير غرفة")
        new_room.guest_name = booking.guest_name

    log_audit(
        db,
        entity_type="booking",
        entity_id=booking.id,
        action="change_room",
        field_name="room_id",
        old_value=str(old_room_id),
        new_value=str(new_room_id),
        reason=reason,
        user_id=user_id,
    )
    if price_delta != 0:
        log_audit(
            db,
            entity_type="booking",
            entity_id=booking.id,
            action="room_price_delta",
            new_value=str(price_delta),
            reason=reason,
            user_id=user_id,
        )
    db.flush()
    return booking


def _overpay_wallet_customer_id(
    booking: HotelBooking, *, wallet_customer_id: int | None = None
) -> int | None:
    if wallet_customer_id:
        return int(wallet_customer_id)
    gt = (
        booking.guest_type.value
        if hasattr(booking.guest_type, "value")
        else str(booking.guest_type or "")
    ).upper()
    stay = (getattr(booking, "stay_payer", None) or "").strip().upper()
    if booking.company_customer_id and (
        gt == "COMPANY" or stay == "COMPANY"
    ):
        return int(booking.company_customer_id)
    if booking.customer_id:
        return int(booking.customer_id)
    return None


def transfer_booking_overpay_to_customer_wallet(
    db: Session,
    booking: HotelBooking,
    *,
    user_id: int | None = None,
    note: str | None = None,
    wallet_customer_id: int | None = None,
) -> Decimal:
    """ينقل رصيد الدفع الزائد من الحجز إلى محفظة العميل/الشركة ويصفّر رصيد الفوليو."""
    from modules.customers.service import (
        CustomersError,
        adjust_wallet,
        get_or_create_by_phone,
    )
    from modules.hotel.folio import build_guest_account

    if booking is None:
        return Decimal("0")
    target_id = _overpay_wallet_customer_id(
        booking, wallet_customer_id=wallet_customer_id
    )
    if not target_id:
        phone = (booking.guest_phone or booking.company_contact_phone or "").strip()
        if phone:
            try:
                cust = get_or_create_by_phone(
                    db,
                    phone=phone,
                    name=(booking.guest_name or booking.company_name or "").strip()
                    or None,
                )
                booking.customer_id = int(cust.id)
                target_id = int(cust.id)
            except Exception:  # noqa: BLE001
                return Decimal("0")
        else:
            return Decimal("0")
    try:
        credit = build_guest_account(db, int(booking.id)).amount_credit
    except Exception:  # noqa: BLE001
        return Decimal("0")
    credit = Decimal(str(credit or 0)).quantize(Decimal("0.001"))
    if credit <= Decimal("0.001"):
        return Decimal("0")
    try:
        adjust_wallet(
            db,
            int(target_id),
            amount=credit,
            note=(
                (note or "").strip()
                or f"رصيد دفع زائد من حجز {booking.reference}"
            ),
            user_id=user_id,
        )
    except CustomersError:
        return Decimal("0")
    new_paid = max(
        Decimal("0"),
        (Decimal(str(booking.paid_amount or 0)) - credit).quantize(Decimal("0.001")),
    )
    booking.paid_amount = new_paid
    # العربون المخزّن لا يتجاوز صافي المدفوع المطبّق على الحجز
    dep = Decimal(str(booking.deposit_amount or 0)).quantize(Decimal("0.001"))
    if dep > new_paid:
        booking.deposit_amount = new_paid
    _recalc_payment_status(db, booking)
    log_audit(
        db,
        entity_type="booking",
        entity_id=booking.id,
        action="credit_to_wallet",
        new_value=str(credit),
        reason=(note or "").strip() or "نقل رصيد الحجز إلى محفظة العميل",
        user_id=user_id,
    )
    db.flush()
    return credit


def consolidate_checked_out_credits_to_wallet(
    db: Session,
    customer_id: int,
    *,
    user_id: int | None = None,
) -> Decimal:
    """يرحّل أرصدة الحجوزات المُغلقة إلى المحفظة (إصلاح حجوزات غادرت سابقاً)."""
    from modules.customers.account_balance import _customer_booking_match_clauses
    from modules.customers.models import Customer

    customer = db.get(Customer, int(customer_id))
    if customer is None:
        return Decimal("0")
    rows = list(
        db.scalars(
            select(HotelBooking).where(
                HotelBooking.booking_status == BookingStatus.CHECKED_OUT,
                or_(*_customer_booking_match_clauses(customer)),
            )
        ).all()
    )
    moved = Decimal("0")
    for booking in rows:
        moved += transfer_booking_overpay_to_customer_wallet(
            db,
            booking,
            user_id=user_id,
            wallet_customer_id=int(customer.id),
            note=f"ترحيل رصيد حجز مغلق إلى محفظة العميل #{customer.id}",
        )
    return moved.quantize(Decimal("0.001"))


def apply_prepaid_credit(
    db: Session,
    booking_id: int,
    amount: Decimal,
    *,
    note: str | None = None,
    user_id: int | None = None,
) -> None:
    """يخصم من مدفوعات الحجز عند تسوية فواتير الغرفة من رصيد الزبون (دفع زائد)."""
    booking = db.get(HotelBooking, booking_id)
    if booking is None:
        raise BookingError("الحجز غير موجود.")
    amt = Decimal(str(amount)).quantize(Decimal("0.001"))
    if amt <= 0:
        return
    from modules.hotel.folio import build_guest_account

    credit = build_guest_account(db, booking_id).amount_credit
    if amt > credit + Decimal("0.0005"):
        raise BookingError("رصيد الزبون غير كافٍ لتسوية هذه الفاتورة.")
    booking.paid_amount = max(
        Decimal("0"),
        Decimal(str(booking.paid_amount or 0)) - amt,
    )
    _recalc_payment_status(db, booking)
    log_audit(
        db,
        entity_type="booking",
        entity_id=booking.id,
        action="prepaid_credit_applied",
        new_value=str(amt),
        reason=(note or "").strip() or None,
        user_id=user_id,
    )
    db.flush()


def _resolve_hotel_shift_stamp(
    db: Session,
    *,
    hotel_shift_id: int | None = None,
    employee_id: int | None = None,
) -> tuple[int | None, int | None]:
    """يربط الحركة بجلسة الاستقبال المفتوحة وموظف الرقم السري إن وُجدا."""
    if hotel_shift_id is not None and employee_id is not None:
        return int(hotel_shift_id), int(employee_id)
    try:
        from modules.hotel.shift_service import get_open_shift

        open_s = get_open_shift(db)
    except Exception:  # noqa: BLE001
        open_s = None
    if open_s is None:
        return hotel_shift_id, employee_id
    sid = int(hotel_shift_id) if hotel_shift_id is not None else int(open_s.id)
    eid = (
        int(employee_id)
        if employee_id is not None
        else (int(open_s.employee_id) if open_s.employee_id else None)
    )
    return sid, eid


def record_payment(
    db: Session,
    booking_id: int,
    *,
    amount: Decimal,
    payment_method_id: int | None,
    is_deposit: bool = False,
    note: str | None = None,
    user_id: int | None = None,
    notify: bool = True,
    hotel_shift_id: int | None = None,
    employee_id: int | None = None,
    allow_closed: bool = False,
) -> HotelBookingPayment:
    booking = db.get(HotelBooking, booking_id)
    if booking is None:
        raise BookingError("الحجز غير موجود.")
    # الإيداع/القبض فقط للحجوزات النشطة (محجوزة أو مسكّنة) — لا على مغادر/ملغى
    if not allow_closed and booking.booking_status in (
        BookingStatus.CHECKED_OUT,
        BookingStatus.CANCELLED,
    ):
        raise BookingError(
            "لا يمكن تسجيل إيداع أو قبض على حجز مغلق (مغادر/ملغى). "
            "القبض مسموح فقط للحجوزات المحجوزة أو المسجّل دخولها."
        )
    amt = Decimal(str(amount)).quantize(Decimal("0.001"))
    if amt <= 0:
        raise BookingError("المبلغ يجب أن يكون موجباً.")
    try:
        from modules.authz.models import User
        from modules.payments.service import assert_hotel_payment_method

        pay_user = db.get(User, int(user_id)) if user_id else None
        assert_hotel_payment_method(db, payment_method_id, user=pay_user)
    except Exception as exc:
        raise BookingError(str(exc)) from exc
    shift_id, emp_id = _resolve_hotel_shift_stamp(
        db, hotel_shift_id=hotel_shift_id, employee_id=employee_id
    )
    pay = HotelBookingPayment(
        booking_id=booking.id,
        amount=amt,
        payment_method_id=payment_method_id,
        is_deposit=is_deposit,
        note=(note or "").strip() or None,
        received_by_id=user_id,
        hotel_shift_id=shift_id,
        received_by_employee_id=emp_id,
    )
    db.add(pay)
    db.flush()
    from modules.gl.posting import post_hotel_booking_payment_shadow_safe

    post_hotel_booking_payment_shadow_safe(db, pay)
    booking.paid_amount = Decimal(str(booking.paid_amount or 0)) + amt
    if is_deposit:
        booking.deposit_amount = Decimal(str(booking.deposit_amount or 0)) + amt
    _recalc_payment_status(db, booking)
    try:
        credit_tourism_commission_for_payment(
            db, booking, payment_amount=amt, user_id=user_id
        )
    except Exception:  # noqa: BLE001
        pass
    log_audit(
        db,
        entity_type="booking",
        entity_id=booking.id,
        action="payment",
        new_value=str(amt),
        user_id=user_id,
    )
    if notify:
        try:
            from modules.notifications.hotel_hooks import emit_hotel_payment_received

            method_name = ""
            if payment_method_id:
                from modules.payments.models import PaymentMethod

                pm = db.get(PaymentMethod, payment_method_id)
                method_name = pm.name_ar if pm else ""
            emit_hotel_payment_received(
                db,
                booking,
                payment_amount=amt,
                payment_method=method_name,
                payment_id=pay.id,
                is_deposit=is_deposit,
            )
        except Exception:  # noqa: BLE001
            pass
    try:
        from modules.hotel.follow_up import clear_claim_wa_if_settled

        clear_claim_wa_if_settled(db, booking)
    except Exception:  # noqa: BLE001
        pass
    # مزامنة ذمم المغادرة المفتوحة + مسح شارة «ذمم الحجوزات» بعد السداد
    try:
        from modules.hotel.booking_debts import sync_open_debts_with_folio

        sync_open_debts_with_folio(db, int(booking.id), user_id=user_id)
    except Exception:  # noqa: BLE001
        pass
    try:
        from modules.hotel.nav_badges import invalidate_hotel_nav_badges

        invalidate_hotel_nav_badges()
    except Exception:  # noqa: BLE001
        pass
    return pay


def refund_booking_credit(
    db: Session,
    booking: HotelBooking,
    *,
    amount: Decimal,
    payment_method_id: int,
    user_id: int | None = None,
    reason: str | None = None,
    hotel_shift_id: int | None = None,
    employee_id: int | None = None,
) -> list[HotelBookingPaymentRefund]:
    """يسترد رصيد النزيل نقداً من آخر دفعات غير مستردة."""
    need = Decimal(str(amount)).quantize(Decimal("0.001"))
    if need <= Decimal("0.001"):
        return []
    pays = sorted(
        [p for p in (booking.payments or []) if not p.is_refunded],
        key=lambda p: int(p.id),
        reverse=True,
    )
    if not pays:
        raise BookingError("لا توجد دفعات قابلة للاسترداد لتسوية رصيد النزيل.")
    left = need
    refs: list[HotelBookingPaymentRefund] = []
    for pay in pays:
        if left <= Decimal("0.001"):
            break
        pay_amt = Decimal(str(pay.amount or 0)).quantize(Decimal("0.001"))
        if pay_amt <= 0:
            continue
        take = min(left, pay_amt)
        refs.append(
            refund_payment(
                db,
                int(pay.id),
                amount=take,
                reason=(reason or "").strip() or "استرداد رصيد عند المغادرة",
                user_id=user_id,
                payment_method_id=payment_method_id,
                hotel_shift_id=hotel_shift_id,
                employee_id=employee_id,
            )
        )
        left = (left - take).quantize(Decimal("0.001"))
    if left > Decimal("0.001"):
        raise BookingError(
            f"تعذّر استرداد كامل الرصيد — تبقّى {left} د.ل بدون دفعة كافية."
        )
    return refs


def refund_payment(
    db: Session,
    payment_id: int,
    *,
    amount: Decimal,
    reason: str | None,
    user_id: int | None = None,
    payment_method_id: int | None = None,
    hotel_shift_id: int | None = None,
    employee_id: int | None = None,
) -> HotelBookingPaymentRefund:
    pay = db.get(HotelBookingPayment, payment_id)
    if pay is None:
        raise BookingError("الدفعة غير موجودة.")
    if pay.is_refunded:
        raise BookingError("الدفعة مستردة بالفعل.")
    amt = Decimal(str(amount)).quantize(Decimal("0.001"))
    if amt <= 0 or amt > pay.amount:
        raise BookingError("مبلغ الاسترداد غير صالح.")
    # وسيلة الصرف للنزيل — افتراضياً نفس وسيلة الدفعة إن لم تُحدَّد
    pm_id = payment_method_id if payment_method_id is not None else pay.payment_method_id
    try:
        from modules.authz.models import User
        from modules.payments.service import assert_hotel_payment_method

        pay_user = db.get(User, int(user_id)) if user_id else None
        assert_hotel_payment_method(db, pm_id, user=pay_user)
    except Exception as exc:
        raise BookingError(str(exc)) from exc
    shift_id, emp_id = _resolve_hotel_shift_stamp(
        db, hotel_shift_id=hotel_shift_id, employee_id=employee_id
    )
    ref = HotelBookingPaymentRefund(
        payment_id=pay.id,
        amount=amt,
        payment_method_id=pm_id,
        reason=(reason or "").strip() or None,
        approved_by_id=user_id,
        hotel_shift_id=shift_id,
        approved_by_employee_id=emp_id,
    )
    db.add(ref)
    db.flush()
    from modules.gl.posting import post_hotel_booking_payment_refund_shadow_safe

    post_hotel_booking_payment_refund_shadow_safe(db, ref)
    pay.is_refunded = True
    booking = db.get(HotelBooking, pay.booking_id)
    if booking:
        new_paid = max(
            Decimal("0"),
            Decimal(str(booking.paid_amount or 0)) - amt,
        )
        booking.paid_amount = new_paid
        dep = Decimal(str(booking.deposit_amount or 0))
        if dep > new_paid:
            booking.deposit_amount = new_paid
        booking.payment_status = BookingPaymentStatus.REFUNDED
    log_audit(
        db,
        entity_type="payment",
        entity_id=pay.id,
        action="refund",
        new_value=str(amt),
        reason=reason,
        user_id=user_id,
    )
    return ref


def add_booking_service(
    db: Session,
    booking_id: int,
    *,
    name_ar: str,
    quantity: Decimal,
    unit_price: Decimal,
    product_id: int | None = None,
    sale_id: int | None = None,
    notes: str | None = None,
    user_id: int | None = None,
    notify: bool = True,
    charged_to_guest: bool = True,
    service_code: str | None = None,
) -> HotelBookingService:
    booking = db.get(HotelBooking, booking_id)
    if booking is None:
        raise BookingError("الحجز غير موجود.")
    qty = Decimal(str(quantity)).quantize(Decimal("0.0001"))
    price = Decimal(str(unit_price)).quantize(Decimal("0.001"))
    line = (qty * price).quantize(Decimal("0.001"))

    from modules.hotel.company_agreement_service import allocate_charge, infer_service_code
    from modules.hotel.folio import is_laundry_service

    code = infer_service_code(
        name_ar=name_ar,
        explicit=service_code,
        is_laundry=is_laundry_service(name_ar),
    )
    # تكلفة فندق صريحة (إفطار مشمول…): لا تُوزَّع على النزيل/الشركة كدين
    if not charged_to_guest:
        split_company = Decimal("0")
        split_guest = Decimal("0")
        folio_side = "HOTEL"
        show_on_guest = False
    elif code in ("VIOLATION", "DAMAGE"):
        # مخالفة تلف/فقدان: دائماً على حساب النزيل — لا تُحمَّل على عقد الشركة
        split_company = Decimal("0")
        split_guest = line
        folio_side = "GUEST"
        show_on_guest = True
    else:
        split = allocate_charge(
            db,
            int(booking_id),
            amount=line,
            service_code=code,
            quantity=qty,
            name_ar=name_ar,
            consume_limit=True,
        )
        split_company = split.company_amount
        split_guest = split.guest_amount
        folio_side = split.folio_side
        show_on_guest = split.guest_amount > Decimal("0.0005")
        if split_company > Decimal("0.0005") and booking.company_customer_id:
            from modules.customers.company_credit import (
                CompanyCreditError,
                assert_company_can_accept_debt,
            )
            from modules.customers.models import Customer

            try:
                assert_company_can_accept_debt(
                    db,
                    db.get(Customer, int(booking.company_customer_id)),
                    split_company,
                    exclude_booking_id=int(booking_id),
                )
            except CompanyCreditError as exc:
                raise BookingError(str(exc)) from exc

    svc = HotelBookingService(
        booking_id=booking.id,
        name_ar=name_ar.strip(),
        quantity=qty,
        unit_price=price,
        line_total=line,
        product_id=product_id,
        sale_id=sale_id,
        notes=(notes or "").strip() or None,
        added_by_id=user_id,
        charged_to_guest=show_on_guest if charged_to_guest else False,
        service_code=code,
        folio_side=folio_side,
        company_amount=split_company if charged_to_guest else Decimal("0"),
        guest_amount=split_guest if charged_to_guest else Decimal("0"),
    )
    db.add(svc)
    log_audit(
        db,
        entity_type="booking",
        entity_id=booking.id,
        action="add_service" if charged_to_guest else "add_hotel_cost",
        new_value=(
            f"{name_ar}: {line}"
            + (
                ""
                if charged_to_guest
                else " (تكلفة فندق — غير على النزيل)"
            )
            + (
                f" [شركة {split_company}/نزيل {split_guest}]"
                if charged_to_guest
                else ""
            )
        ),
        user_id=user_id,
    )
    db.flush()
    _recalc_payment_status(db, booking)
    if notify and charged_to_guest:
        try:
            from modules.notifications.hotel_hooks import emit_hotel_unpaid_service_added

            emit_hotel_unpaid_service_added(
                db,
                booking,
                service_name=svc.name_ar,
                service_amount=svc.line_total,
            )
        except Exception:  # noqa: BLE001
            pass
    return svc


def mark_room_clean(db: Session, room_id: int, *, user_id: int | None = None) -> HotelRoom:
    room = db.get(HotelRoom, room_id)
    if room is None:
        raise BookingError("الغرفة غير موجودة.")
    if room.physical_status not in (
        RoomPhysicalStatus.DIRTY,
        RoomPhysicalStatus.CLEANING,
    ):
        raise BookingError("الغرفة ليست بحاجة تنظيف / قيد التنظيف.")
    _log_room_status(db, room, RoomPhysicalStatus.AVAILABLE, user_id, "Housekeeping")
    db.flush()
    return room


def assign_room_cleaning(
    db: Session,
    room_id: int,
    *,
    user_id: int | None = None,
    cleaning_phone: str | None = None,
    cleaning_staff_name: str | None = None,
    employee_id: int | None = None,
    note: str | None = None,
    send_notification: bool = True,
    base_url: str | None = None,
) -> HotelRoom:
    """إرسال مهمة تنظيف عبر واتساب — الشقة تصبح «قيد التنظيف»."""
    from modules.settings.service import get_setting

    room = db.get(HotelRoom, room_id)
    if room is None:
        raise BookingError("الغرفة غير موجودة.")
    if room.physical_status not in (
        RoomPhysicalStatus.DIRTY,
        RoomPhysicalStatus.CLEANING,
    ):
        raise BookingError("يمكن إرسال مهمة التنظيف فقط للشقق «تحتاج تنظيف» أو «قيد التنظيف».")

    if employee_id:
        from modules.hr.models import Employee

        emp = db.get(Employee, int(employee_id))
        if emp is None:
            raise BookingError("موظف التنظيف غير موجود.")
        cleaning_phone = (emp.phone or cleaning_phone or "").strip()
        cleaning_staff_name = (emp.full_name_ar or cleaning_staff_name or "").strip()
        if send_notification and not cleaning_phone:
            raise BookingError("موظف التنظيف المختار لا يملك رقم هاتف في ملفه.")

    phone = (cleaning_phone or "").strip()
    if not phone:
        phone = (get_setting(db, "hotel_cleaning_phone") or "").strip()
    if send_notification and not phone:
        raise BookingError("أدخل رقم واتساب التنظيف أو اضبط الرقم الافتراضي في الإعدادات.")
    if send_notification:
        from modules.hotel.housekeeping_links import ensure_inbound_ready_for_buttons
        from modules.messaging.service import whatsapp_send_blocker

        block = whatsapp_send_blocker(db)
        if block:
            raise BookingError(block)
        ensure_inbound_ready_for_buttons(db)

    detail = (note or "").strip()
    reason = "إرسال للتنظيف" + (f" — {detail}" if detail else "")
    if room.physical_status != RoomPhysicalStatus.CLEANING:
        _log_room_status(db, room, RoomPhysicalStatus.CLEANING, user_id, reason)
    db.flush()

    if send_notification:
        from modules.notifications.hotel_hooks import emit_hotel_room_cleaning

        emit_hotel_room_cleaning(
            db,
            room,
            cleaning_phone=phone,
            cleaning_staff_name=cleaning_staff_name
            or (get_setting(db, "hotel_cleaning_name") or "").strip(),
            note=detail,
            employee_id=employee_id,
            reported_by="",
            base_url=(base_url or "").strip(),
        )
    return room


_MAINTENANCE_ALLOWED_FROM = frozenset(
    {
        RoomPhysicalStatus.AVAILABLE,
        RoomPhysicalStatus.DIRTY,
        RoomPhysicalStatus.CLEANING,
        RoomPhysicalStatus.RESERVED,
        RoomPhysicalStatus.BLOCKED,
        RoomPhysicalStatus.OUT_OF_SERVICE,
    }
)


def set_room_maintenance_status(
    db: Session,
    room_id: int,
    *,
    user_id: int | None = None,
    note: str | None = None,
    issue_type: str | None = None,
    maintenance_phone: str | None = None,
    maintenance_staff_name: str | None = None,
    employee_id: int | None = None,
    reported_by: str | None = None,
    send_notification: bool = True,
    required_from: RoomPhysicalStatus | frozenset[RoomPhysicalStatus] | None = None,
) -> HotelRoom:
    """وضع الشقة في الصيانة — من متاحة أو بعد التنظيف."""
    from modules.hotel.maintenance import issue_label

    room = db.get(HotelRoom, room_id)
    if room is None:
        raise BookingError("الغرفة غير موجودة.")
    if not room.is_active:
        raise BookingError("الشقة معطّلة — فعّلها أولاً من الإعداد.")
    if room.physical_status == RoomPhysicalStatus.MAINTENANCE:
        raise BookingError("الشقة في الصيانة بالفعل.")
    if room.physical_status == RoomPhysicalStatus.OCCUPIED:
        raise BookingError("الشقة مشغولة بنزيل — لا يمكن إرسالها للصيانة.")
    if active_booking_for_room(db, room_id):
        raise BookingError("الشقة مرتبطة بنزيل مسكّن — لا يمكن إرسالها للصيانة.")
    if required_from is not None:
        allowed = (
            required_from
            if isinstance(required_from, (set, frozenset))
            else frozenset({required_from})
        )
        if room.physical_status not in allowed:
            raise BookingError(
                "يمكن إرسال الشقة للصيانة فقط من حالة «تحتاج تنظيف» أو «قيد التنظيف»."
            )
    elif room.physical_status not in _MAINTENANCE_ALLOWED_FROM:
        raise BookingError(
            f"لا يمكن إرسال الشقة للصيانة من حالة «{room.physical_status.value}»."
        )

    issue_key = (issue_type or "other").strip().lower() or "other"
    label = issue_label(issue_key)
    detail = (note or "").strip()
    reason = f"{label}" + (f" — {detail}" if detail else "")

    if employee_id:
        from modules.hr.models import Employee

        emp = db.get(Employee, int(employee_id))
        if emp is None:
            raise BookingError("موظف الصيانة غير موجود.")
        maintenance_phone = (emp.phone or maintenance_phone or "").strip()
        maintenance_staff_name = (emp.full_name_ar or maintenance_staff_name or "").strip()
        if send_notification and not maintenance_phone:
            raise BookingError("موظف الصيانة المختار لا يملك رقم هاتف في ملفه.")

    _log_room_status(db, room, RoomPhysicalStatus.MAINTENANCE, user_id, reason or "صيانة")
    db.flush()

    if send_notification:
        phone = (maintenance_phone or "").strip()
        if not phone:
            from modules.settings.service import get_setting

            phone = (get_setting(db, "hotel_maintenance_phone") or "").strip()
        if not phone:
            raise BookingError("أدخل رقم موظف الصيانة أو اضبط الرقم الافتراضي في الإعدادات.")
        from modules.notifications.hotel_hooks import emit_hotel_room_maintenance

        emit_hotel_room_maintenance(
            db,
            room,
            issue_type=issue_key,
            issue_details=detail,
            maintenance_phone=phone,
            maintenance_staff_name=maintenance_staff_name or "",
            reported_by=reported_by or "",
            employee_id=employee_id,
        )
    return room


def mark_room_maintenance(
    db: Session,
    room_id: int,
    *,
    user_id: int | None = None,
    note: str | None = None,
    issue_type: str | None = None,
    maintenance_phone: str | None = None,
    maintenance_staff_name: str | None = None,
    employee_id: int | None = None,
    reported_by: str | None = None,
    send_notification: bool = True,
) -> HotelRoom:
    """بعد التفتيش — الشقة تحتاج صيانة ولا تُؤجَّر."""
    if not (issue_type or "").strip():
        raise BookingError("اختر نوع العطل (مكيف، ميكروويف، …).")
    return set_room_maintenance_status(
        db,
        room_id,
        user_id=user_id,
        note=note,
        issue_type=issue_type,
        maintenance_phone=maintenance_phone,
        maintenance_staff_name=maintenance_staff_name,
        employee_id=employee_id,
        reported_by=reported_by,
        send_notification=send_notification,
        required_from=frozenset(
            {RoomPhysicalStatus.DIRTY, RoomPhysicalStatus.CLEANING}
        ),
    )


def mark_maintenance_complete(db: Session, room_id: int, *, user_id: int | None = None) -> HotelRoom:
    room = db.get(HotelRoom, room_id)
    if room is None:
        raise BookingError("الغرفة غير موجودة.")
    if room.physical_status != RoomPhysicalStatus.MAINTENANCE:
        raise BookingError("الغرفة ليست في الصيانة.")
    _log_room_status(db, room, RoomPhysicalStatus.AVAILABLE, user_id, "اكتملت الصيانة")
    db.flush()
    return room


def active_booking_for_room(db: Session, room_id: int) -> HotelBooking | None:
    return db.scalar(
        select(HotelBooking).where(
            HotelBooking.room_id == room_id,
            HotelBooking.booking_status == BookingStatus.CHECKED_IN,
        )
    )
