"""ائتمان الشركة وحد الدين على حجوزات الفندق (محفظة + ديون مفتوحة).

═══ سقف ائتماني — مصدر واحد ═══
الحقول على customers فقط:
  • company_credit_limit
  • allow_company_credit

تُستخدم هنا للتحقق من:
  • دين الحجز (create_checkout_debt / charge-to-company)
  • قيود المطعم عندما تتحمّل الشركة البند
  • محفظة الشركة (company_spendable_balance / company_wallet_floor)

عقد الشركة (CompanyAgreement) لا يخزّن سقفاً ائتمانياً منفصلاً.
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from modules.customers.models import Customer, CustomerType
from modules.hotel.booking_models import (
    BookingStatus,
    HotelBooking,
    HotelBookingDebt,
    HotelBookingDebtStatus,
)

Q = Decimal("0.001")


class CompanyCreditError(Exception):
    """تجاوز حد دين الشركة / غير مسموح بالدين."""


def _is_company(customer: Customer | None) -> bool:
    if customer is None:
        return False
    ct = getattr(customer, "customer_type", None)
    return ct == CustomerType.COMPANY or str(
        getattr(ct, "value", ct) or ""
    ).upper() == "COMPANY"


def company_open_hotel_debt_total(db: Session, company_id: int) -> Decimal:
    """مجموع ديون الفندق المفتوحة المسجّلة على حجوزات الشركة."""
    raw = db.scalar(
        select(
            func.coalesce(
                func.sum(
                    func.coalesce(
                        HotelBookingDebt.amount_remaining, HotelBookingDebt.amount
                    )
                ),
                0,
            )
        )
        .join(HotelBooking, HotelBooking.id == HotelBookingDebt.booking_id)
        .where(
            HotelBookingDebt.status == HotelBookingDebtStatus.OPEN,
            HotelBooking.company_customer_id == int(company_id),
        )
    )
    return Decimal(str(raw or 0)).quantize(Q)


def company_active_folio_exposure(
    db: Session,
    company_id: int,
    *,
    exclude_booking_id: int | None = None,
) -> Decimal:
    """متبقي فوليو حجوزات الشركة النشطة بلا دين مفتوح (لتجنّب الازدواج)."""
    from modules.hotel.folio import booking_balance_due

    rows = list(
        db.scalars(
            select(HotelBooking).where(
                HotelBooking.company_customer_id == int(company_id),
                HotelBooking.booking_status.in_(
                    (
                        BookingStatus.PENDING,
                        BookingStatus.CONFIRMED,
                        BookingStatus.CHECKED_IN,
                        BookingStatus.CHECKED_OUT,
                    )
                ),
                or_(
                    HotelBooking.booking_payer == "COMPANY",
                    HotelBooking.stay_payer == "COMPANY",
                    HotelBooking.extras_payer == "COMPANY",
                ),
            )
        ).all()
    )
    total = Decimal("0")
    for b in rows:
        if exclude_booking_id is not None and int(b.id) == int(exclude_booking_id):
            continue
        open_debt = db.scalar(
            select(HotelBookingDebt.id).where(
                HotelBookingDebt.booking_id == int(b.id),
                HotelBookingDebt.status == HotelBookingDebtStatus.OPEN,
            )
        )
        if open_debt is not None:
            continue
        try:
            bal = booking_balance_due(db, int(b.id))
        except Exception:  # noqa: BLE001
            bal = Decimal("0")
        if bal > Q:
            total += bal
    return total.quantize(Q)


def company_hotel_exposure(
    db: Session,
    company: Customer,
    *,
    exclude_booking_id: int | None = None,
) -> Decimal:
    """إجمالي ما على الشركة حالياً (ديون + متبقي فوليو شركة)."""
    if not _is_company(company):
        return Decimal("0")
    cid = int(company.id)
    debt = company_open_hotel_debt_total(db, cid)
    folio = company_active_folio_exposure(
        db, cid, exclude_booking_id=exclude_booking_id
    )
    return (debt + folio).quantize(Q)


def company_debt_capacity_remaining(
    db: Session,
    company: Customer,
    *,
    exclude_booking_id: int | None = None,
) -> Decimal:
    """كم دين إضافي مسموح ضمن حد الائتمان (والمحفظة الموجبة تُوسّع السعة)."""
    if not _is_company(company):
        return Decimal("0")
    if not bool(getattr(company, "allow_company_credit", False)):
        # بدون تفعيل الائتمان: فقط الرصيد الموجب للمحفظة يُعدّ «سعة»
        wallet = max(
            Decimal("0"),
            Decimal(str(getattr(company, "wallet_balance", 0) or 0)).quantize(Q),
        )
        exposure = company_hotel_exposure(
            db, company, exclude_booking_id=exclude_booking_id
        )
        # ديون مفتوحة بلا حد = السعة 0 إن وُجدت مديونية
        if exposure > Q:
            return Decimal("0")
        return wallet
    limit = Decimal(str(getattr(company, "company_credit_limit", 0) or 0)).quantize(Q)
    if limit < 0:
        limit = Decimal("0")
    wallet = Decimal(str(getattr(company, "wallet_balance", 0) or 0)).quantize(Q)
    # السعة = حد الائتمان + رصيد موجب − ما هو مفتوح فعلاً
    capacity_ceiling = (limit + max(Decimal("0"), wallet)).quantize(Q)
    exposure = company_hotel_exposure(
        db, company, exclude_booking_id=exclude_booking_id
    )
    rem = (capacity_ceiling - exposure).quantize(Q)
    return rem if rem > 0 else Decimal("0")


def assert_company_can_accept_debt(
    db: Session,
    company: Customer | None,
    amount: Decimal | str | float,
    *,
    exclude_booking_id: int | None = None,
) -> None:
    """يرفع CompanyCreditError إن تجاوز الإضافة حد الدين."""
    amt = Decimal(str(amount or 0)).quantize(Q)
    if amt <= Q:
        return
    if company is None or not _is_company(company):
        raise CompanyCreditError("حساب الشركة غير صالح لتسجيل الدين.")
    if not bool(getattr(company, "allow_company_credit", False)):
        raise CompanyCreditError(
            "الشركة غير مفعّل لها «الدين على الحساب». "
            "فعّل السماح بالدين وحد الائتمان من ملف الشركة، أو اطلب التسديد أولاً."
        )
    rem = company_debt_capacity_remaining(
        db, company, exclude_booking_id=exclude_booking_id
    )
    if amt > rem + Q:
        limit = Decimal(str(getattr(company, "company_credit_limit", 0) or 0)).quantize(
            Q
        )
        exp = company_hotel_exposure(
            db, company, exclude_booking_id=exclude_booking_id
        )
        raise CompanyCreditError(
            f"تجاوز حد دين الشركة. الحد: {limit} د.ل — المفتوح: {exp} د.ل — "
            f"المتبقي المسموح: {rem} د.ل — المطلوب الآن: {amt} د.ل. "
            "يجب التسديد أولاً قبل تسجيل دين إضافي أو متابعة الحجز."
        )


def company_covers_charge_kind(booking, kind: str) -> bool:
    """هل الشركة تتحمل بنداً: stay | extras (تمهيد لتقسيمة لاحقة)."""
    k = (kind or "stay").strip().lower()
    payer = (getattr(booking, "booking_payer", None) or "").strip().upper()
    if k == "extras":
        raw = (getattr(booking, "extras_payer", None) or "").strip().upper()
        return (raw or payer or "GUEST") == "COMPANY"
    raw = (getattr(booking, "stay_payer", None) or "").strip().upper()
    return (raw or payer or "GUEST") == "COMPANY"
