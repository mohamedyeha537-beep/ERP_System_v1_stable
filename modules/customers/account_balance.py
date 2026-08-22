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
    spendable: Decimal = Decimal("0")  # قابل للخصم (رصيد + حد ائتمان شركة)
    company_discount_percent: Decimal = Decimal("0")
    company_credit_limit: Decimal = Decimal("0")
    allow_company_credit: bool = False
    parent_company_id: int | None = None

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
    """حجوزات العميل: بالـ customer_id / company_customer_id، أو بالهاتف."""
    from sqlalchemy import and_

    phone = normalize_phone(customer.phone or "")
    clauses = [
        HotelBooking.customer_id == customer.id,
        HotelBooking.company_customer_id == customer.id,
    ]
    if phone:
        # لا نخلط حجوزات عملاء آخرين يشتركون في نفس الرقم
        clauses.append(
            and_(
                HotelBooking.customer_id.is_(None),
                HotelBooking.company_customer_id.is_(None),
                or_(
                    HotelBooking.guest_phone == phone,
                    HotelBooking.company_contact_phone == phone,
                ),
            )
        )
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


def _folio_due_for_party(db: Session, booking: HotelBooking, customer: Customer) -> Decimal:
    """متبقي الفوليو على جهة هذا العميل (شركة أو نزيل) — ليس وعاء الحجز كله إن وُجدت جهتان."""
    from modules.hotel.folio import build_guest_account, build_party_accounts

    cid = int(customer.id)
    company_id = getattr(booking, "company_customer_id", None)
    guest_id = getattr(booking, "customer_id", None)
    try:
        parties = build_party_accounts(db, int(booking.id))
    except Exception:  # noqa: BLE001
        try:
            return build_guest_account(db, int(booking.id)).amount_due
        except Exception:  # noqa: BLE001
            return Decimal("0")
    if company_id and int(company_id) == cid and (not guest_id or int(guest_id) != cid):
        return parties.company.due
    if guest_id and int(guest_id) == cid and company_id and int(company_id) != cid:
        return parties.guest.due
    try:
        return build_guest_account(db, int(booking.id)).amount_due
    except Exception:  # noqa: BLE001
        return Decimal("0")


def folio_covered_sale_ids_for_customer(db: Session, customer: Customer) -> set[int]:
    """فواتير مطعم/غرفة داخلة أصلاً في فوليو حجوزات العميل — حتى لا تُحسب مرتين."""
    booking_ids = _hotel_booking_ids_for_customer(db, customer)
    if not booking_ids:
        return set()
    ids: set[int] = set()
    from modules.hotel.models import RoomCharge
    from modules.sales.models import Sale

    for sid in db.scalars(select(Sale.id).where(Sale.booking_id.in_(booking_ids))):
        ids.add(int(sid))
    for sid in db.scalars(
        select(RoomCharge.sale_id).where(
            RoomCharge.booking_id.in_(booking_ids),
            RoomCharge.sale_id.is_not(None),
        )
    ):
        ids.add(int(sid))
    try:
        from modules.hotel.folio import list_open_room_charges_for_booking

        for bid in booking_ids:
            for rc in list_open_room_charges_for_booking(db, int(bid), auto_link=False):
                if rc.sale_id:
                    ids.add(int(rc.sale_id))
    except Exception:  # noqa: BLE001
        pass
    return ids


def pos_outstanding_for_customer(
    db: Session,
    customer: Customer,
    *,
    exclude_sale_ids: set[int] | None = None,
) -> Decimal:
    """ذمم فواتير POS/مطعم على ملف العميل، مع استبعاد ما هو داخل فوليو حجز مفتوح."""
    from modules.receivables.service import build_receivable_rows

    skip = exclude_sale_ids or set()
    debt = Decimal("0")
    for row in build_receivable_rows(db, only_with_balance=True):
        if row.customer_id != customer.id or row.outstanding <= 0:
            continue
        if int(row.sale_id) in skip:
            continue
        debt += row.outstanding
    return debt.quantize(Q)


def hotel_outstanding_for_customer(db: Session, customer: Customer) -> Decimal:
    """متبقي حجوزات العميل: فوليو الإقامة + دين مغادرة مسجّل (بدون تكرار لنفس الحجز)."""
    total = Decimal("0")
    for bid in _hotel_booking_ids_for_customer(db, customer):
        booking = db.get(HotelBooking, int(bid))
        if booking is None:
            continue
        folio_due = _folio_due_for_party(db, booking, customer)
        debt_amt = db.scalar(
            select(func.coalesce(func.sum(HotelBookingDebt.amount), 0)).where(
                HotelBookingDebt.booking_id == int(bid),
                HotelBookingDebt.status == HotelBookingDebtStatus.OPEN,
            )
        )
        debt_amt = Decimal(str(debt_amt or 0)).quantize(Q)
        total += max(folio_due, debt_amt)
    return total.quantize(Q)


def _hotel_booking_ids_for_customer(db: Session, customer: Customer) -> list[int]:
    stmt = (
        select(HotelBooking.id)
        .where(
            HotelBooking.booking_status.notin_(
                (BookingStatus.CANCELLED, BookingStatus.NO_SHOW, BookingStatus.LATE_CANCELLATION)
            )
        )
        .where(or_(*_customer_booking_match_clauses(customer)))
    )
    return [int(i) for i in db.scalars(stmt).all()]


def _hotel_booking_credit_for_customer(
    db: Session,
    customer: Customer,
    *,
    exclude_booking_id: int | None = None,
) -> Decimal:
    """مجموع الدفع الزائد على حجوزات العميل (فوليو) الذي لم يُحوَّل للمحفظة."""
    from modules.hotel.folio import build_guest_account

    total = Decimal("0")
    for bid in _hotel_booking_ids_for_customer(db, customer):
        if exclude_booking_id is not None and int(bid) == int(exclude_booking_id):
            continue
        try:
            credit = build_guest_account(db, bid).amount_credit
        except Exception:  # noqa: BLE001
            continue
        if credit > Q:
            total += credit
    return total.quantize(Q)


def list_booking_folio_credits(db: Session, customer: Customer) -> list[dict]:
    """حجوزات عليها دفع زائد لم يُرحَّل بعد إلى المحفظة."""
    from modules.hotel.bookings_report import BOOKING_STATUS_LABELS
    from modules.hotel.folio import build_guest_account

    open_st = {
        BookingStatus.PENDING.value,
        BookingStatus.CONFIRMED.value,
        BookingStatus.CHECKED_IN.value,
    }
    rows: list[dict] = []
    for bid in _hotel_booking_ids_for_customer(db, customer):
        booking = db.get(HotelBooking, int(bid))
        if booking is None:
            continue
        try:
            credit = build_guest_account(db, int(bid)).amount_credit
        except Exception:  # noqa: BLE001
            continue
        if credit <= Q:
            continue
        st = (
            booking.booking_status.value
            if hasattr(booking.booking_status, "value")
            else str(booking.booking_status or "")
        )
        rows.append(
            {
                "booking_id": int(bid),
                "reference": (booking.reference or f"#{bid}").strip(),
                "status": st,
                "status_ar": BOOKING_STATUS_LABELS.get(st, st),
                "credit": credit.quantize(Q),
                "is_open": st in open_st,
                "can_post": st == BookingStatus.CHECKED_OUT.value,
                "guest_name": (
                    booking.display_name
                    if getattr(booking, "display_name", None)
                    else (booking.company_name or booking.guest_name or "")
                ),
            }
        )
    rows.sort(key=lambda r: r["booking_id"], reverse=True)
    return rows


def customer_money_balance(
    db: Session,
    customer: Customer,
    *,
    exclude_booking_id: int | None = None,
) -> CustomerMoneyBalance:
    from modules.customers.service import company_spendable_balance

    stats = customer_stats(db, customer)
    covered = folio_covered_sale_ids_for_customer(db, customer)
    hotel_debt = hotel_outstanding_for_customer(db, customer)
    hotel_credit = _hotel_booking_credit_for_customer(
        db, customer, exclude_booking_id=exclude_booking_id
    )
    wallet = stats.wallet_balance
    restaurant = pos_outstanding_for_customer(db, customer, exclude_sale_ids=covered)
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
        spendable=company_spendable_balance(customer),
        company_discount_percent=Decimal(
            str(getattr(customer, "company_discount_percent", 0) or 0)
        ).quantize(Q),
        company_credit_limit=Decimal(
            str(getattr(customer, "company_credit_limit", 0) or 0)
        ).quantize(Q),
        allow_company_credit=bool(getattr(customer, "allow_company_credit", False)),
        parent_company_id=(
            int(customer.parent_company_id)
            if getattr(customer, "parent_company_id", None)
            else None
        ),
    )


def customer_money_balance_by_id(
    db: Session,
    customer_id: int | None,
    *,
    exclude_booking_id: int | None = None,
) -> CustomerMoneyBalance | None:
    if not customer_id:
        return None
    cust = db.get(Customer, int(customer_id))
    if cust is None or not cust.is_active:
        return None
    return customer_money_balance(
        db, cust, exclude_booking_id=exclude_booking_id
    )


def _lookup_names_from_bookings(
    db: Session,
    *,
    text: str,
    limit: int = 8,
) -> list[CustomerMoneyBalance]:
    """أسماء شركات/نزلاء مكتوبة على الحجوزات حتى لو اختلفت عن سجل العميل."""
    from dataclasses import replace

    from modules.hotel.booking_models import HotelBookingGuest

    like = f"%{text.strip()}%"
    bookings = list(
        db.scalars(
            select(HotelBooking)
            .where(
                or_(
                    HotelBooking.guest_name.like(like),
                    HotelBooking.company_name.like(like),
                    HotelBooking.company_contact_name.like(like),
                )
            )
            .order_by(HotelBooking.id.desc())
            .limit(40)
        ).all()
    )
    guest_rows = list(
        db.execute(
            select(HotelBookingGuest, HotelBooking)
            .join(HotelBooking, HotelBooking.id == HotelBookingGuest.booking_id)
            .where(HotelBookingGuest.full_name.like(like))
            .order_by(HotelBookingGuest.id.desc())
            .limit(20)
        ).all()
    )

    seen: set[tuple[str, str, int]] = set()
    out: list[CustomerMoneyBalance] = []

    def _add(
        *,
        display_name: str,
        company_name: str,
        phone: str,
        customer_id: int | None,
        as_company: bool,
    ) -> None:
        nm = (display_name or "").strip()
        if not nm:
            return
        cid = int(customer_id) if customer_id else 0
        key = (nm.casefold(), (phone or "").strip(), cid)
        if key in seen:
            return
        seen.add(key)
        cust = db.get(Customer, cid) if cid else None
        if cust is not None and cust.is_active:
            bal = customer_money_balance(db, cust)
            out.append(
                replace(
                    bal,
                    name=nm,
                    company_name=(company_name or "").strip() or bal.company_name,
                    phone=(phone or "").strip() or bal.phone,
                    customer_type=(
                        CustomerType.COMPANY.value
                        if as_company
                        else bal.customer_type
                    ),
                )
            )
        else:
            out.append(
                CustomerMoneyBalance(
                    customer_id=0,
                    name=nm,
                    phone=(phone or "").strip(),
                    customer_type=(
                        CustomerType.COMPANY.value
                        if as_company
                        else CustomerType.INDIVIDUAL.value
                    ),
                    company_name=(company_name or "").strip(),
                    email="",
                    wallet_credit=Decimal("0"),
                    hotel_credit=Decimal("0"),
                    restaurant_debt=Decimal("0"),
                    hotel_debt=Decimal("0"),
                    net=Decimal("0"),
                )
            )

    for b in bookings:
        is_co = (getattr(b.guest_type, "value", None) or str(b.guest_type or "")).upper() == "COMPANY"
        co = (b.company_name or "").strip()
        gn = (b.guest_name or "").strip()
        phone = (b.company_contact_phone or b.guest_phone or "").strip()
        cid = b.company_customer_id if is_co else b.customer_id
        if co and text.casefold() in co.casefold():
            _add(
                display_name=co,
                company_name=co,
                phone=phone,
                customer_id=cid or b.company_customer_id or b.customer_id,
                as_company=True,
            )
        if gn and text.casefold() in gn.casefold() and gn.casefold() != (co or "").casefold():
            _add(
                display_name=gn,
                company_name=co,
                phone=(b.guest_phone or phone).strip(),
                customer_id=b.customer_id or cid,
                as_company=is_co,
            )
        elif gn and text.casefold() in gn.casefold():
            _add(
                display_name=gn,
                company_name=co or gn,
                phone=phone,
                customer_id=cid or b.customer_id,
                as_company=is_co,
            )
        if len(out) >= limit:
            return out[:limit]

    for guest, b in guest_rows:
        _add(
            display_name=(guest.full_name or "").strip(),
            company_name=(b.company_name or "").strip(),
            phone=(guest.phone or b.guest_phone or "").strip(),
            customer_id=b.customer_id,
            as_company=False,
        )
        if len(out) >= limit:
            break
    return out[:limit]


def lookup_booking_customers(
    db: Session,
    *,
    q: str,
    guest_type: str | None = None,
    limit: int = 8,
) -> list[CustomerMoneyBalance]:
    """بحث بالاسم / الهاتف / اسم الشركة — من العملاء ومن أسماء الحجوزات المخزّنة."""
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
    for bal in _lookup_names_from_bookings(db, text=text, limit=limit):
        # لا نكرر نفس سجل العميل بنفس الاسم الظاهر
        key_id = int(bal.customer_id or 0)
        already = any(
            int(x.customer_id or 0) == key_id
            and (x.name or "").strip().casefold() == (bal.name or "").strip().casefold()
            for x in out
        )
        if already:
            continue
        out.append(bal)
        if len(out) >= limit:
            break
    return out[:limit]


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
        "spendable": str(bal.spendable),
        "company_discount_percent": str(bal.company_discount_percent),
        "company_credit_limit": str(bal.company_credit_limit),
        "allow_company_credit": bool(bal.allow_company_credit),
        "parent_company_id": bal.parent_company_id,
    }
