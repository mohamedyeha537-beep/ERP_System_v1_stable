"""رصيد العميل النقدي (دائن/مدين) — محفظة + رصيد حجوزات + ديون مطعم + ديون فندق."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from modules.customers.models import Customer, CustomerType
from modules.customers.service import get_by_phone, list_customers, normalize_phone
from modules.customers.stats import customer_stats
from modules.hotel.booking_models import (
    BookingStatus,
    HotelBooking,
    HotelBookingDebt,
    HotelBookingDebtStatus,
)


Q = Decimal("0.001")


@dataclass
class CustomerMoneyBalance:
    customer_id: int
    name: str
    phone: str
    customer_type: str  # INDIVIDUAL | COMPANY
    company_name: str
    email: str
    wallet_credit: Decimal
    hotel_credit: Decimal  # دفع زائد على الحجوزات لم يُنقل للمحفظة بعد
    restaurant_debt: Decimal
    hotel_debt: Decimal
    net: Decimal  # موجب = دائن للزبون · سالب = عليه دين

    @property
    def has_credit(self) -> bool:
        return self.net > Q

    @property
    def has_debt(self) -> bool:
        return self.net < -Q

    @property
    def label_ar(self) -> str:
        if self.has_credit:
            return f"رصيد دائن: {self.net} د.ل"
        if self.has_debt:
            return f"عليه (مدين): {abs(self.net)} د.ل"
        return "لا يوجد رصيد / مديونية"


def _customer_booking_match_clauses(customer: Customer) -> list:
    phone = normalize_phone(customer.phone or "")
    clauses = [HotelBooking.customer_id == customer.id]
    if phone:
        clauses.append(HotelBooking.guest_phone == phone)
        clauses.append(HotelBooking.company_contact_phone == phone)
    return clauses


def _hotel_open_debt_for_customer(db: Session, customer: Customer) -> Decimal:
    stmt = (
        select(func.coalesce(func.sum(HotelBookingDebt.amount), 0))
        .join(HotelBooking, HotelBooking.id == HotelBookingDebt.booking_id)
        .where(HotelBookingDebt.status == HotelBookingDebtStatus.OPEN)
        .where(or_(*_customer_booking_match_clauses(customer)))
    )
    raw = db.scalar(stmt)
    return Decimal(str(raw or 0)).quantize(Q)


def _hotel_booking_ids_for_customer(db: Session, customer: Customer) -> list[int]:
    stmt = (
        select(HotelBooking.id)
        .where(
            HotelBooking.booking_status.notin_(
                (BookingStatus.CANCELLED, BookingStatus.NO_SHOW)
            )
        )
        .where(or_(*_customer_booking_match_clauses(customer)))
    )
    return [int(i) for i in db.scalars(stmt).all()]


def _hotel_booking_credit_for_customer(db: Session, customer: Customer) -> Decimal:
    """مجموع الدفع الزائد على حجوزات العميل (فوليو) الذي لم يُحوَّل للمحفظة."""
    from modules.hotel.folio import build_guest_account

    total = Decimal("0")
    for bid in _hotel_booking_ids_for_customer(db, customer):
        try:
            credit = build_guest_account(db, bid).amount_credit
        except Exception:  # noqa: BLE001
            continue
        if credit > Q:
            total += credit
    return total.quantize(Q)


def customer_money_balance(db: Session, customer: Customer) -> CustomerMoneyBalance:
    stats = customer_stats(db, customer)
    hotel_debt = _hotel_open_debt_for_customer(db, customer)
    hotel_credit = _hotel_booking_credit_for_customer(db, customer)
    wallet = stats.wallet_balance
    restaurant = stats.outstanding_debt
    net = (wallet + hotel_credit - restaurant - hotel_debt).quantize(Q)
    ctype = getattr(customer.customer_type, "value", None) or str(
        customer.customer_type or CustomerType.INDIVIDUAL.value
    )
    return CustomerMoneyBalance(
        customer_id=int(customer.id),
        name=(customer.name or "").strip(),
        phone=(customer.phone or "").strip(),
        customer_type=str(ctype).upper(),
        company_name=(customer.company_name or "").strip(),
        email=(customer.email or "").strip(),
        wallet_credit=wallet,
        hotel_credit=hotel_credit,
        restaurant_debt=restaurant,
        hotel_debt=hotel_debt,
        net=net,
    )


def customer_money_balance_by_id(
    db: Session, customer_id: int | None
) -> CustomerMoneyBalance | None:
    if not customer_id:
        return None
    cust = db.get(Customer, int(customer_id))
    if cust is None or not cust.is_active:
        return None
    return customer_money_balance(db, cust)


def lookup_booking_customers(
    db: Session,
    *,
    q: str,
    guest_type: str | None = None,
    limit: int = 8,
) -> list[CustomerMoneyBalance]:
    """بحث بالاسم / الهاتف / اسم الشركة لملء نموذج الحجز."""
    text = (q or "").strip()
    if len(text) < 2:
        return []
    results: list[Customer] = []
    phone = normalize_phone(text)
    if phone and len(phone) >= 8:
        by_phone = get_by_phone(db, phone, active_only=True)
        if by_phone is not None:
            results.append(by_phone)
    for row in list_customers(db, only_active=True, search=text, limit=limit):
        if all(r.id != row.id for r in results):
            results.append(row)
    gt = (guest_type or "").strip().upper()
    out: list[CustomerMoneyBalance] = []
    for cust in results[:limit]:
        bal = customer_money_balance(db, cust)
        if gt in ("INDIVIDUAL", "COMPANY") and bal.customer_type != gt:
            # لا نستبعد — نُظهر ونقترح النوع الصحيح في الواجهة
            pass
        out.append(bal)
    return out


def balance_to_dict(bal: CustomerMoneyBalance) -> dict:
    return {
        "customer_id": bal.customer_id,
        "name": bal.name,
        "phone": bal.phone,
        "customer_type": bal.customer_type,
        "company_name": bal.company_name,
        "email": bal.email,
        "wallet_credit": str(bal.wallet_credit),
        "hotel_credit": str(bal.hotel_credit),
        "restaurant_debt": str(bal.restaurant_debt),
        "hotel_debt": str(bal.hotel_debt),
        "net": str(bal.net),
        "has_credit": bal.has_credit,
        "has_debt": bal.has_debt,
        "label_ar": bal.label_ar,
        "abs_net": str(abs(bal.net)),
    }
