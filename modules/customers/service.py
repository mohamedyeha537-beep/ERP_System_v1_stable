"""Customers & loyalty business logic."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session

from modules.customers.models import (
    Customer,
    CustomerBusinessDomain,
    CustomerType,
    LoyaltyTransaction,
    LoyaltyTxnKind,
)
from modules.settings.service import get_setting


class CustomersError(Exception):
    """خطأ في إدارة العملاء/الولاء."""


def parse_customer_domain(raw: str | None) -> CustomerBusinessDomain | None:
    if raw is None or not str(raw).strip():
        return None
    try:
        return CustomerBusinessDomain(str(raw).strip().lower())
    except ValueError:
        return None


def merge_customer_domain(
    current: CustomerBusinessDomain | str | None,
    incoming: CustomerBusinessDomain | str | None,
) -> CustomerBusinessDomain:
    """إن تعامل العميل مع المجالين يصبح مشتركاً."""
    cur = parse_customer_domain(
        current.value if hasattr(current, "value") else current  # type: ignore[arg-type]
    ) or CustomerBusinessDomain.RESTAURANT
    inc = parse_customer_domain(
        incoming.value if hasattr(incoming, "value") else incoming  # type: ignore[arg-type]
    )
    if inc is None:
        return cur
    if cur == inc or cur == CustomerBusinessDomain.SHARED:
        return cur if cur == CustomerBusinessDomain.SHARED else inc
    if inc == CustomerBusinessDomain.SHARED:
        return CustomerBusinessDomain.SHARED
    return CustomerBusinessDomain.SHARED


def apply_customer_domain(
    customer: Customer, domain: CustomerBusinessDomain | str | None
) -> None:
    if domain is None:
        return
    customer.business_domain = merge_customer_domain(
        getattr(customer, "business_domain", None), domain
    )


def customer_visible_for_domain(
    customer_domain: CustomerBusinessDomain | str | None,
    *,
    filter_domain: CustomerBusinessDomain | str | None,
) -> bool:
    """هل يظهر العميل في قائمة مجال معيّن؟ shared يظهر في الفندق والمطعم."""
    if filter_domain is None:
        return True
    filt = parse_customer_domain(
        filter_domain.value if hasattr(filter_domain, "value") else filter_domain  # type: ignore[arg-type]
    )
    if filt is None:
        return True
    cur = parse_customer_domain(
        customer_domain.value if hasattr(customer_domain, "value") else customer_domain  # type: ignore[arg-type]
    ) or CustomerBusinessDomain.RESTAURANT
    if cur == CustomerBusinessDomain.SHARED:
        return True
    return cur == filt


def resolve_customer_list_domain(user, session: dict | None, *, explicit: str | None = None):
    """مجال قائمة العملاء: من نطاق المستخدم أولاً، ثم فلتر الأدمن/وضع العرض."""
    from modules.platform.business_domain import (
        ViewMode,
        get_admin_view_mode,
        get_user_view_scope,
        is_hotel_scope_user,
        is_restaurant_scope_user,
        is_system_admin,
        UserViewScope,
    )

    # موظف محدود المجال لا يتجاوز الفلتر عبر ?domain=
    if is_hotel_scope_user(user):
        return CustomerBusinessDomain.HOTEL
    if is_restaurant_scope_user(user):
        return CustomerBusinessDomain.RESTAURANT
    if is_system_admin(user):
        exp = parse_customer_domain(explicit)
        if exp is not None:
            return exp
        mode = get_admin_view_mode(session)
        if mode == ViewMode.HOTEL:
            return CustomerBusinessDomain.HOTEL
        if mode == ViewMode.RESTAURANT:
            return CustomerBusinessDomain.RESTAURANT
        return None
    scope = get_user_view_scope(user)
    if scope == UserViewScope.HOTEL:
        return CustomerBusinessDomain.HOTEL
    if scope == UserViewScope.RESTAURANT:
        return CustomerBusinessDomain.RESTAURANT
    exp = parse_customer_domain(explicit)
    if exp is not None:
        return exp
    return None


def domain_from_sale(sale) -> CustomerBusinessDomain:
    """استنتاج مجال العميل من فاتورة البيع."""
    if sale is None:
        return CustomerBusinessDomain.RESTAURANT
    if getattr(sale, "booking_id", None):
        return CustomerBusinessDomain.HOTEL
    ctx = str(getattr(getattr(sale, "context_type", None), "value", getattr(sale, "context_type", "")) or "")
    if ctx.upper() == "ROOM":
        return CustomerBusinessDomain.HOTEL
    return CustomerBusinessDomain.RESTAURANT


# ============================================================
# Phone normalization
# ============================================================

_DIGITS_RE = re.compile(r"\D")


def normalize_phone(raw: str | None) -> str:
    """يُوحّد رقم الجوال الليبي إلى 10 أرقام محلية (09xxxxxxxx).

    يقبل إدخالاً بمفتاح +218 أو 218 ثم يحوّله للصيغة المحلية حتى لا يُنشأ عميل مكرر.
    """
    if raw is None:
        return ""
    digits = _DIGITS_RE.sub("", str(raw).strip())
    if not digits:
        return ""
    if digits.startswith("00218"):
        digits = digits[5:]
    elif digits.startswith("218"):
        digits = digits[3:]
    if len(digits) == 9 and digits.startswith("9"):
        digits = "0" + digits
    return digits


def require_valid_phone(raw: str | None) -> str:
    """رقم هاتف صالح للتخزين — 10 أرقام محلية بدون مفتاح دولة."""
    p = normalize_phone(raw)
    if not p:
        raise CustomersError("رقم الهاتف مطلوب.")
    if len(p) != 10:
        raise CustomersError(
            "رقم الهاتف يجب أن يكون 10 أرقام فقط (مثل 0917122552) "
            "بدون إدخال +218 أو 218."
        )
    if not p.startswith("09"):
        raise CustomersError("رقم الجوال الليبي يبدأ عادةً بـ 09 (10 أرقام).")
    return p


def require_customer_name(
    name: str | None,
    *,
    company_name: str | None = None,
    customer_type: str | None = None,
) -> str:
    """اسم ظاهر مطلوب عند إنشاء/تحديث العميل (للشركات يُقبل اسم الشركة كبديل)."""
    nm = (name or "").strip()
    if nm:
        return nm
    cname = (company_name or "").strip()
    ct = (customer_type or "").strip().upper()
    if ct == "COMPANY" and cname:
        return cname
    raise CustomersError("اسم العميل مطلوب.")


def is_company_customer(customer: Customer | None) -> bool:
    if customer is None:
        return False
    ct = getattr(customer, "customer_type", None)
    val = ct.value if hasattr(ct, "value") else str(ct or "")
    return val.strip().upper() == "COMPANY"


def _phone_storage_variants(canonical: str) -> list[str]:
    """صيغ قديمة مخزّنة في قاعدة البيانات قبل توحيد الأرقام."""
    if len(canonical) != 10:
        return [canonical]
    body = canonical[1:]
    return [canonical, f"+218{body}", f"218{body}", body]


def _migrate_phone_to_canonical(db: Session, customer: Customer, canonical: str) -> None:
    if customer.phone == canonical:
        return
    conflict = db.scalar(
        select(Customer.id).where(
            Customer.phone == canonical, Customer.id != customer.id
        )
    )
    if conflict is None:
        customer.phone = canonical
        db.flush()


# ============================================================
# CRUD
# ============================================================

def list_customers(
    db: Session,
    *,
    only_active: bool = False,
    search: str | None = None,
    customer_type: str | None = None,
    business_domain: CustomerBusinessDomain | str | None = None,
    limit: int = 500,
) -> list[Customer]:
    from modules.customers.models import CustomerType

    stmt = select(Customer)
    if only_active:
        stmt = stmt.where(Customer.is_active.is_(True))
    if search:
        like = f"%{search.strip()}%"
        stmt = stmt.where(
            (Customer.phone.like(like))
            | (Customer.name.like(like))
            | (Customer.company_name.like(like))
        )
    if customer_type:
        try:
            ct = CustomerType(customer_type.strip().upper())
            stmt = stmt.where(Customer.customer_type == ct)
        except ValueError:
            pass
    dom = parse_customer_domain(
        business_domain.value if hasattr(business_domain, "value") else business_domain  # type: ignore[arg-type]
    )
    if dom is not None:
        # المجال المطلوب + المشتركون
        # وللفندق: أيضاً أي عميل مربوط بحجز فندقي (إصلاح بيانات قديمة بمجال مطعم)
        if dom == CustomerBusinessDomain.HOTEL:
            from modules.hotel.booking_models import HotelBooking

            booked_ids = select(HotelBooking.customer_id).where(
                HotelBooking.customer_id.is_not(None)
            )
            stmt = stmt.where(
                Customer.business_domain.in_(
                    (dom, CustomerBusinessDomain.SHARED)
                )
                | Customer.id.in_(booked_ids)
            )
        else:
            stmt = stmt.where(
                Customer.business_domain.in_(
                    (dom, CustomerBusinessDomain.SHARED)
                )
            )
    stmt = stmt.order_by(Customer.last_visit_at.is_(None), Customer.last_visit_at.desc(), Customer.id.desc()).limit(limit)
    return list(db.scalars(stmt))


def get_customer(db: Session, customer_id: int) -> Customer | None:
    return db.get(Customer, customer_id)


def _customer_by_phone_row(
    db: Session, phone_value: str, *, active_only: bool
) -> Customer | None:
    stmt = select(Customer).where(Customer.phone == phone_value)
    if active_only:
        stmt = stmt.where(Customer.is_active.is_(True))
    return db.scalar(stmt.limit(1))


def get_by_phone(
    db: Session, phone: str, *, active_only: bool = True
) -> Customer | None:
    """يبحث عن عميل بالهاتف (النشطون فقط في نقطة البيع افتراضياً)."""
    p = normalize_phone(phone)
    if not p:
        return None
    found = _customer_by_phone_row(db, p, active_only=active_only)
    if found is not None:
        return found
    if len(p) == 10:
        for alt in _phone_storage_variants(p):
            if alt == p:
                continue
            legacy = _customer_by_phone_row(db, alt, active_only=active_only)
            if legacy is not None:
                _migrate_phone_to_canonical(db, legacy, p)
                return _customer_by_phone_row(db, p, active_only=active_only)
    return None


def get_by_phone_any(db: Session, phone: str) -> Customer | None:
    return get_by_phone(db, phone, active_only=False)


def create_customer(
    db: Session,
    *,
    phone: str,
    name: str | None = None,
    email: str | None = None,
    notes: str | None = None,
    customer_type: str | None = None,
    company_name: str | None = None,
    business_domain: CustomerBusinessDomain | str | None = None,
) -> Customer:
    from modules.customers.models import CustomerType

    p = require_valid_phone(phone)
    existing = get_by_phone_any(db, p)
    if existing is not None:
        if existing.is_active:
            raise CustomersError(f"رقم الهاتف {p} مسجَّل بالفعل.")
        raise CustomersError(
            f"رقم الهاتف {p} لعميل مؤرشف (#{existing.id}). "
            "فعّله من «عرض المؤرشفين» بدلاً من إنشاء سجل جديد."
        )
    ct = CustomerType.INDIVIDUAL
    if customer_type:
        try:
            ct = CustomerType(customer_type.strip().upper())
        except ValueError:
            ct = CustomerType.INDIVIDUAL
    display_name = require_customer_name(
        name,
        company_name=company_name,
        customer_type=ct.value,
    )
    dom = (
        parse_customer_domain(
            business_domain.value
            if hasattr(business_domain, "value")
            else business_domain  # type: ignore[arg-type]
        )
        or CustomerBusinessDomain.RESTAURANT
    )
    c = Customer(
        phone=p,
        name=display_name,
        email=(email or "").strip() or None,
        notes=(notes or "").strip() or None,
        customer_type=ct,
        company_name=(company_name or "").strip() or None,
        business_domain=dom,
        is_active=True,
    )
    db.add(c)
    db.flush()
    from modules.customers.referral_service import ensure_referral_code

    ensure_referral_code(db, c, notify=False)
    return c


def get_or_create_by_phone(
    db: Session,
    *,
    phone: str,
    name: str | None = None,
    business_domain: CustomerBusinessDomain | str | None = None,
) -> Customer:
    """يبحث عن عميل برقم الهاتف، فإن لم يوجد ينشئه (الاسم مطلوب للعميل الجديد)."""
    p = require_valid_phone(phone)
    existing = get_by_phone_any(db, p)
    if existing is not None:
        if not existing.is_active:
            existing.is_active = True
        if not (existing.name or "").strip() and (name or "").strip():
            existing.name = name.strip()
        apply_customer_domain(existing, business_domain)
        db.flush()
        return existing
    return create_customer(
        db,
        phone=p,
        name=require_customer_name(name),
        business_domain=business_domain,
    )


def ensure_company_customer(
    db: Session,
    *,
    company_name: str,
    phone: str | None = None,
    email: str | None = None,
    company_customer_id: int | None = None,
) -> Customer:
    """يربط/ينشئ حساب شركة للحجز — يدعم الشركة الجديدة من أول حجز."""
    from modules.customers.models import CustomerType

    if company_customer_id:
        c = get_customer(db, int(company_customer_id))
        if c is None or not c.is_active:
            raise CustomersError("حساب الشركة المختار غير صالح.")
        if c.customer_type != CustomerType.COMPANY:
            c.customer_type = CustomerType.COMPANY
        cname = (company_name or "").strip()
        if cname:
            c.company_name = cname
            if not (c.name or "").strip():
                c.name = cname
        apply_customer_domain(c, CustomerBusinessDomain.HOTEL)
        db.flush()
        return c

    cname = (company_name or "").strip()
    if not cname:
        raise CustomersError("أدخل اسم الشركة.")

    phone_raw = (phone or "").strip()
    if phone_raw:
        try:
            p = require_valid_phone(phone_raw)
        except CustomersError as exc:
            raise CustomersError(f"هاتف الشركة: {exc}") from exc
        existing = get_by_phone_any(db, p)
        if existing is not None:
            if not existing.is_active:
                existing.is_active = True
            existing.customer_type = CustomerType.COMPANY
            existing.company_name = cname
            existing.name = cname
            if email and not (existing.email or "").strip():
                existing.email = email.strip()
            apply_customer_domain(existing, CustomerBusinessDomain.HOTEL)
            db.flush()
            return existing
        return create_customer(
            db,
            phone=p,
            name=cname,
            email=email,
            customer_type=CustomerType.COMPANY.value,
            company_name=cname,
            business_domain=CustomerBusinessDomain.HOTEL,
        )

    # بدون هاتف: ابحث عن شركة بنفس الاسم ثم اطلب هاتفاً لإنشاء حساب جديد
    existing = db.scalar(
        select(Customer)
        .where(
            Customer.is_active.is_(True),
            Customer.customer_type == CustomerType.COMPANY,
            func.lower(func.coalesce(Customer.company_name, "")) == cname.lower(),
        )
        .order_by(Customer.id.desc())
        .limit(1)
    )
    if existing is not None:
        apply_customer_domain(existing, CustomerBusinessDomain.HOTEL)
        db.flush()
        return existing

    raise CustomersError(
        "شركة جديدة: أدخل رقم هاتف الشركة لإنشاء حسابها وربط الدين عليه."
    )


def sync_customer_from_hotel_booking(db: Session, booking) -> Customer | None:
    """يربط الحجز بعميل فندق ويحدّث الاسم/الشركة/المجال حتى يظهر في قائمة عملاء الفندق."""
    from modules.customers.models import CustomerType
    from modules.hotel.booking_models import GuestType

    phone = (
        (getattr(booking, "company_contact_phone", None) or "")
        or (getattr(booking, "guest_phone", None) or "")
    ).strip() or None
    company_name = (getattr(booking, "company_name", None) or "").strip() or None
    is_company = (
        getattr(booking, "guest_type", None) == GuestType.COMPANY
        or bool(company_name)
    )
    display_name = (
        company_name
        or (getattr(booking, "guest_name", None) or "").strip()
        or (getattr(booking, "company_contact_name", None) or "").strip()
        or None
    )

    cust = None
    if getattr(booking, "customer_id", None):
        cust = get_customer(db, int(booking.customer_id))
    if cust is None and phone:
        try:
            if display_name:
                cust = get_or_create_by_phone(
                    db,
                    phone=phone,
                    name=display_name,
                    business_domain=CustomerBusinessDomain.HOTEL,
                )
            else:
                cust = get_by_phone_any(db, phone)
        except CustomersError:
            cust = get_by_phone_any(db, phone)

    if cust is None:
        return None

    if not cust.is_active:
        cust.is_active = True
    apply_customer_domain(cust, CustomerBusinessDomain.HOTEL)
    if is_company and company_name:
        cust.customer_type = CustomerType.COMPANY
        cust.company_name = company_name
        cust.name = company_name
    elif display_name and not (cust.name or "").strip():
        cust.name = display_name
    booking.customer_id = int(cust.id)
    if is_company and not getattr(booking, "company_customer_id", None):
        booking.company_customer_id = int(cust.id)
    db.flush()
    return cust


def backfill_hotel_customers_from_bookings(db: Session, *, limit: int = 500) -> int:
    """يصلح عملاء الحجوزات الذين لا يظهرون في قائمة الفندق (مجال خاطئ / بلا ربط)."""
    from modules.hotel.booking_models import HotelBooking

    updated = 0
    rows = list(
        db.scalars(
            select(HotelBooking).order_by(HotelBooking.id.desc()).limit(limit)
        ).all()
    )
    for booking in rows:
        before_id = getattr(booking, "customer_id", None)
        before_dom = None
        before_company = None
        if before_id:
            c0 = get_customer(db, int(before_id))
            if c0 is not None:
                before_dom = getattr(c0, "business_domain", None)
                before_company = getattr(c0, "company_name", None)
        cust = sync_customer_from_hotel_booking(db, booking)
        if cust is None:
            continue
        if (
            before_id != cust.id
            or before_dom != getattr(cust, "business_domain", None)
            or before_company != getattr(cust, "company_name", None)
        ):
            updated += 1
    if updated:
        db.flush()
    return updated


def update_customer(
    db: Session,
    customer_id: int,
    *,
    phone: str | None = None,
    name: str | None = None,
    email: str | None = None,
    notes: str | None = None,
    is_active: bool | None = None,
    customer_type: str | None = None,
    company_name: str | None = None,
    business_domain: str | None = None,
    parent_company_id: int | None | object = ...,
    company_discount_percent: Decimal | str | float | None = None,
    company_credit_limit: Decimal | str | float | None = None,
    allow_company_credit: bool | None = None,
    company_notify_frequency: str | None = None,
    company_default_notify_to: str | None = None,
) -> Customer:
    from modules.customers.models import CustomerType

    c = db.get(Customer, customer_id)
    if c is None:
        raise CustomersError("العميل غير موجود.")
    if phone is not None:
        p = require_valid_phone(phone)
        existing = get_by_phone_any(db, p)
        if existing is not None and existing.id != customer_id:
            raise CustomersError(f"رقم الهاتف {p} مسجَّل لعميل آخر.")
        c.phone = p
    if customer_type is not None:
        try:
            c.customer_type = CustomerType(customer_type.strip().upper())
        except ValueError:
            pass
    if company_name is not None:
        c.company_name = company_name.strip() or None
    if name is not None:
        c.name = require_customer_name(
            name,
            company_name=c.company_name,
            customer_type=c.customer_type.value
            if hasattr(c.customer_type, "value")
            else str(c.customer_type or ""),
        )
    elif company_name is not None and not (c.name or "").strip():
        ct_val = (
            c.customer_type.value
            if hasattr(c.customer_type, "value")
            else str(c.customer_type or "")
        )
        if ct_val == "COMPANY":
            c.name = require_customer_name(
                None,
                company_name=c.company_name,
                customer_type=ct_val,
            )
    if email is not None:
        c.email = email.strip() or None
    if notes is not None:
        c.notes = notes.strip() or None
    if is_active is not None:
        c.is_active = is_active
    if business_domain is not None and str(business_domain).strip():
        parsed = parse_customer_domain(business_domain)
        if parsed is None:
            raise CustomersError("مجال العميل غير صالح (مطعم / فندق / مشترك).")
        c.business_domain = parsed
    if parent_company_id is not ...:
        if parent_company_id is None or parent_company_id == "" or parent_company_id == 0:
            c.parent_company_id = None
        else:
            pid = int(parent_company_id)
            if pid == int(customer_id):
                raise CustomersError("لا يمكن ربط العميل بنفسه كشركة أم.")
            parent = db.get(Customer, pid)
            if parent is None or not parent.is_active:
                raise CustomersError("حساب الشركة الأم غير موجود.")
            if parent.customer_type != CustomerType.COMPANY:
                raise CustomersError("الشركة الأم يجب أن تكون من نوع «شركة».")
            c.parent_company_id = pid
    if company_discount_percent is not None:
        pct = Decimal(str(company_discount_percent or 0)).quantize(Decimal("0.001"))
        if pct < 0 or pct > 100:
            raise CustomersError("نسبة خصم الشركة بين 0 و 100.")
        c.company_discount_percent = pct
    if company_credit_limit is not None:
        lim = Decimal(str(company_credit_limit or 0)).quantize(Decimal("0.001"))
        if lim < 0:
            raise CustomersError("حد الائتمان لا يكون سالباً.")
        c.company_credit_limit = lim
    if allow_company_credit is not None:
        c.allow_company_credit = bool(allow_company_credit)
    if company_notify_frequency is not None:
        from modules.hotel.notify_routing import normalize_notify_freq

        c.company_notify_frequency = normalize_notify_freq(company_notify_frequency)
    if company_default_notify_to is not None:
        from modules.hotel.notify_routing import (
            NOTIFY_TO_COMPANY,
            normalize_notify_to,
        )

        c.company_default_notify_to = normalize_notify_to(
            company_default_notify_to, default=NOTIFY_TO_COMPANY
        )
    db.flush()
    return c


def company_spendable_balance(customer) -> Decimal:
    """المبلغ القابل للخصم من المحفظة (يشمل حد الائتمان للشركات المسموح لها)."""
    from modules.customers.models import CustomerType

    bal = Decimal(str(getattr(customer, "wallet_balance", 0) or 0)).quantize(
        Decimal("0.001")
    )
    ct = getattr(customer, "customer_type", None)
    is_company = ct == CustomerType.COMPANY or str(
        getattr(ct, "value", ct) or ""
    ).upper() == "COMPANY"
    if not is_company or not bool(getattr(customer, "allow_company_credit", False)):
        return max(Decimal("0"), bal)
    limit = Decimal(str(getattr(customer, "company_credit_limit", 0) or 0)).quantize(
        Decimal("0.001")
    )
    if limit < 0:
        limit = Decimal("0")
    return (bal + limit).quantize(Decimal("0.001"))


def company_wallet_floor(customer) -> Decimal:
    """أدنى رصيد مسموح (سالب عند تفعيل الائتمان)."""
    from modules.customers.models import CustomerType

    ct = getattr(customer, "customer_type", None)
    is_company = ct == CustomerType.COMPANY or str(
        getattr(ct, "value", ct) or ""
    ).upper() == "COMPANY"
    if not is_company or not bool(getattr(customer, "allow_company_credit", False)):
        return Decimal("0")
    limit = Decimal(str(getattr(customer, "company_credit_limit", 0) or 0)).quantize(
        Decimal("0.001")
    )
    if limit <= 0:
        return Decimal("0")
    return (-limit).quantize(Decimal("0.001"))


def adjust_wallet(
    db: Session,
    customer_id: int,
    *,
    amount: Decimal,
    note: str = "",
    user_id: int | None = None,
    allow_company_debt: bool = False,
) -> Customer:
    from modules.customers.models import (
        CustomerType,
        CustomerWalletTransaction,
        WalletTxnKind,
    )

    c = db.get(Customer, customer_id)
    if c is None:
        raise CustomersError("العميل غير موجود.")
    delta = Decimal(str(amount)).quantize(Decimal("0.001"))
    if delta == 0:
        raise CustomersError("المبلغ صفر.")
    new_bal = (Decimal(str(c.wallet_balance or 0)) + delta).quantize(Decimal("0.001"))
    if new_bal < 0:
        is_company = c.customer_type == CustomerType.COMPANY
        floor = company_wallet_floor(c) if is_company else Decimal("0")
        if not (is_company and allow_company_debt and bool(c.allow_company_credit)):
            raise CustomersError("رصيد المحفظة لا يمكن أن يكون سالباً.")
        if new_bal < floor - Decimal("0.0005"):
            raise CustomersError(
                f"تجاوز حد ائتمان الشركة ({abs(floor)} د.ل). "
                f"الرصيد بعد العملية سيكون {new_bal} د.ل."
            )
    c.wallet_balance = new_bal
    kind = WalletTxnKind.TOPUP if delta > 0 else WalletTxnKind.SPEND
    if (note or "").strip().lower().startswith("تعديل"):
        kind = WalletTxnKind.ADJUST
    db.add(
        CustomerWalletTransaction(
            customer_id=customer_id,
            kind=kind,
            amount=delta,
            note=(note or "").strip() or None,
            created_by_id=user_id,
        )
    )
    db.flush()
    return c


def list_wallet_transactions(db: Session, customer_id: int, *, limit: int = 100):
    from modules.customers.models import CustomerWalletTransaction

    return list(
        db.scalars(
            select(CustomerWalletTransaction)
            .where(CustomerWalletTransaction.customer_id == customer_id)
            .order_by(CustomerWalletTransaction.id.desc())
            .limit(limit)
        ).all()
    )


def _staff_label(db: Session, *, user_id: int | None, employee_id: int | None = None) -> str:
    if employee_id:
        try:
            from modules.hr.models import Employee

            emp = db.get(Employee, int(employee_id))
            if emp is not None:
                return (emp.full_name_ar or "").strip() or f"موظف #{emp.id}"
        except Exception:  # noqa: BLE001
            pass
    if user_id:
        from modules.authz.models import User

        u = db.get(User, int(user_id))
        if u is not None:
            return (u.username or "").strip() or f"مستخدم #{u.id}"
    return "—"


def company_cash_movements(db: Session, customer_id: int, *, limit: int = 200) -> dict:
    """مقبوضات ومصروفات حساب الشركة من الحجوزات والفواتير وحركات المحفظة."""
    from modules.customers.models import CustomerWalletTransaction
    from modules.hotel.booking_models import HotelBooking, HotelBookingPayment
    from modules.sales.models import Sale, SaleStatus

    receipts: list[dict] = []
    expenses: list[dict] = []

    pays = list(
        db.scalars(
            select(HotelBookingPayment)
            .join(HotelBooking, HotelBooking.id == HotelBookingPayment.booking_id)
            .where(
                or_(
                    HotelBooking.company_customer_id == customer_id,
                    HotelBooking.customer_id == customer_id,
                )
            )
            .order_by(HotelBookingPayment.created_at.desc(), HotelBookingPayment.id.desc())
            .limit(limit)
        ).all()
    )
    for p in pays:
        booking = db.get(HotelBooking, int(p.booking_id))
        ref = (
            (getattr(booking, "reference", None) or str(p.booking_id))
            if booking
            else str(p.booking_id)
        )
        label = "عربون" if p.is_deposit else "إيصال قبض"
        receipts.append(
            {
                "title": f"{label} — حجز {ref}",
                "amount": Decimal(str(p.amount or 0)).quantize(Decimal("0.001")),
                "created_at": p.created_at,
                "employee": _staff_label(
                    db,
                    user_id=p.received_by_id,
                    employee_id=getattr(p, "received_by_employee_id", None),
                ),
                "ref_no": (p.receipt_number or "").strip() or f"HP-{p.id}",
                "print_url": (
                    f"/admin/hotel/bookings/{p.booking_id}/receipt"
                    f"?doc=receipt&payment_id={p.id}"
                ),
                "detail": (p.note or "").strip() or label,
            }
        )

    wallet_rows = list(
        db.scalars(
            select(CustomerWalletTransaction)
            .where(CustomerWalletTransaction.customer_id == customer_id)
            .order_by(
                CustomerWalletTransaction.created_at.desc(),
                CustomerWalletTransaction.id.desc(),
            )
            .limit(limit)
        ).all()
    )
    for t in wallet_rows:
        amt = Decimal(str(t.amount or 0)).quantize(Decimal("0.001"))
        note = (t.note or "").strip()
        kind = t.kind.value if hasattr(t.kind, "value") else str(t.kind or "")
        row = {
            "title": note or ("إيداع محفظة" if amt > 0 else "صرف محفظة"),
            "amount": abs(amt),
            "created_at": t.created_at,
            "employee": _staff_label(db, user_id=t.created_by_id),
            "ref_no": f"W-{t.id}",
            "print_url": f"/admin/customers/{customer_id}/wallet-txn/{t.id}/print",
            "detail": note or kind,
        }
        if amt > 0:
            receipts.append(row)
        elif amt < 0:
            expenses.append(row)

    booking_ids = list(
        db.scalars(
            select(HotelBooking.id).where(
                or_(
                    HotelBooking.company_customer_id == customer_id,
                    HotelBooking.customer_id == customer_id,
                )
            )
        ).all()
    )
    sale_filters = [Sale.customer_id == customer_id]
    if booking_ids:
        sale_filters.append(Sale.booking_id.in_(booking_ids))
    sales = list(
        db.scalars(
            select(Sale)
            .where(Sale.status == SaleStatus.COMPLETED, or_(*sale_filters))
            .order_by(Sale.created_at.desc(), Sale.id.desc())
            .limit(limit)
        ).all()
    )
    seen_sale_ids: set[int] = set()
    for s in sales:
        if int(s.id) in seen_sale_ids:
            continue
        seen_sale_ids.add(int(s.id))
        note_hit = any(
            str(s.id) in (e.get("detail") or "") or str(s.id) in (e.get("title") or "")
            for e in expenses
        )
        if note_hit:
            continue
        inv = (
            (getattr(s, "final_invoice_number", None) or "").strip()
            or (getattr(s, "receipt_number", None) or "").strip()
            or str(s.id)
        )
        expenses.append(
            {
                "title": f"فاتورة مطعم #{inv}",
                "amount": Decimal(str(s.total or 0)).quantize(Decimal("0.001")),
                "created_at": s.created_at,
                "employee": _staff_label(db, user_id=s.created_by_id),
                "ref_no": inv,
                "print_url": f"/pos/receipt/{s.id}",
                "detail": "فاتورة مطعم",
            }
        )

    try:
        from modules.hotel.booking_models import HotelBookingService

        svc_rows = list(
            db.scalars(
                select(HotelBookingService)
                .join(HotelBooking, HotelBooking.id == HotelBookingService.booking_id)
                .where(
                    or_(
                        HotelBooking.company_customer_id == customer_id,
                        HotelBooking.customer_id == customer_id,
                    )
                )
                .order_by(
                    HotelBookingService.created_at.desc(),
                    HotelBookingService.id.desc(),
                )
                .limit(limit)
            ).all()
        )
        for svc in svc_rows:
            sale_id = getattr(svc, "sale_id", None)
            if sale_id and int(sale_id) in seen_sale_ids:
                continue
            side = (getattr(svc, "folio_side", None) or "").upper()
            company_amt = Decimal(str(getattr(svc, "company_amount", None) or 0))
            if side != "COMPANY" and company_amt <= 0:
                continue
            amt = company_amt if company_amt > 0 else Decimal(str(svc.line_total or 0))
            if amt <= 0:
                continue
            booking = db.get(HotelBooking, int(svc.booking_id))
            bref = (
                (getattr(booking, "reference", None) or str(svc.booking_id))
                if booking
                else str(svc.booking_id)
            )
            ref = str(sale_id or svc.id)
            expenses.append(
                {
                    "title": f"{svc.name_ar or 'خدمة فندق'} — حجز {bref}",
                    "amount": amt.quantize(Decimal("0.001")),
                    "created_at": svc.created_at,
                    "employee": _staff_label(
                        db, user_id=getattr(svc, "added_by_id", None)
                    ),
                    "ref_no": ref,
                    "print_url": (
                        f"/pos/receipt/{sale_id}"
                        if sale_id
                        else f"/admin/hotel/bookings/{svc.booking_id}"
                    ),
                    "detail": svc.name_ar or "خدمة فندق",
                }
            )
    except Exception:  # noqa: BLE001
        pass

    def _when(row: dict):
        return row.get("created_at") or datetime.min.replace(tzinfo=timezone.utc)

    receipts.sort(key=_when, reverse=True)
    expenses.sort(key=_when, reverse=True)
    ledger: list[dict] = []
    for r in receipts:
        ledger.append({**r, "direction": "IN"})
    for e in expenses:
        ledger.append({**e, "direction": "OUT"})
    ledger.sort(key=_when, reverse=True)
    return {
        "receipts": receipts[:limit],
        "expenses": expenses[:limit],
        "ledger": ledger[:limit],
    }


def archive_customer(db: Session, customer_id: int) -> Customer:
    """أرشفة: يختفي من القائمة اليومية لكن تبقى الفواتير والنقاط في السجل."""
    c = db.get(Customer, customer_id)
    if c is None:
        raise CustomersError("العميل غير موجود.")
    c.is_active = False
    db.flush()
    return c


def restore_customer(db: Session, customer_id: int) -> Customer:
    c = db.get(Customer, customer_id)
    if c is None:
        raise CustomersError("العميل غير موجود.")
    conflict = get_by_phone(db, c.phone)
    if conflict is not None and conflict.id != c.id:
        raise CustomersError(
            f"لا يمكن التفعيل: الرقم {c.phone} مستخدم لعميل نشط آخر (#{conflict.id})."
        )
    c.is_active = True
    db.flush()
    return c


def purge_customer(db: Session, customer_id: int) -> None:
    """حذف نهائي: يفك ارتباط الفواتير ويحذف سجل النقاط ثم يزيل العميل."""
    from modules.sales.models import Sale

    c = db.get(Customer, customer_id)
    if c is None:
        return
    db.execute(
        update(Sale).where(Sale.customer_id == customer_id).values(customer_id=None)
    )
    db.execute(
        delete(LoyaltyTransaction).where(
            LoyaltyTransaction.customer_id == customer_id
        )
    )
    db.delete(c)
    db.flush()


def delete_customer(db: Session, customer_id: int) -> None:
    """حذف بسيط إن لم يكن للعميل أي سجل نقاط؛ وإلا يُطلب الأرشفة أو الحذف النهائي."""
    c = db.get(Customer, customer_id)
    if c is None:
        return
    txn_count = db.scalar(
        select(func.count(LoyaltyTransaction.id)).where(
            LoyaltyTransaction.customer_id == customer_id
        )
    )
    if (txn_count or 0) > 0:
        raise CustomersError(
            "لا يمكن الحذف البسيط: للعميل سجل نقاط أو فواتير. "
            "استخدم «أرشفة» لإخفائه من القائمة، أو «حذف نهائي» بعد التأكيد."
        )
    purge_customer(db, customer_id)


# ============================================================
# Loyalty engine
# ============================================================

def _setting_decimal(db: Session, key: str, default: str) -> Decimal:
    val = get_setting(db, key, default)
    try:
        return Decimal(str(val).strip() or default)
    except Exception:  # noqa: BLE001
        return Decimal(default)


def _setting_bool(db: Session, key: str, default: bool) -> bool:
    val = get_setting(db, key, "1" if default else "0")
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def loyalty_settings(
    db: Session, domain=None
) -> dict:
    """يقرأ إعدادات الولاء — افتراضياً المطعم/نقطة البيع."""
    from modules.platform.domain_loyalty import loyalty_settings_for_domain

    return loyalty_settings_for_domain(db, domain)


def _max_redeem_percent_of_sale(db: Session) -> Decimal:
    """نسبة قيمة الفاتورة القصوى التي يُسمح خصمها بالنقاط (0 = معطّل، 100 = كامل الفاتورة)."""
    raw = (get_setting(db, "loyalty_max_redeem_percent", "") or "").strip()
    if not raw:
        return Decimal("0")
    try:
        pct = Decimal(raw)
    except Exception:
        return Decimal("0")
    if pct < 0:
        return Decimal("0")
    if pct > 100:
        return Decimal("100")
    return pct


def points_to_dinars(db: Session, points: Decimal, domain=None) -> Decimal:
    """يحوّل النقاط إلى قيمة دينار حسب إعدادات المجال."""
    from modules.platform.domain_loyalty import points_to_dinars_for_domain

    return points_to_dinars_for_domain(db, points, domain)


def grant_points_for_sale(
    db: Session,
    *,
    customer: Customer,
    sale_id: int | None,
    sale_total: Decimal,
    user_id: int | None = None,
    skip_notification: bool = False,
    domain=None,
) -> Decimal:
    """يضيف نقاط ولاء للعميل بناءً على إجمالي الفاتورة + يحدّث إحصاءاته.

    يعيد عدد النقاط الممنوحة (قد يكون صفر لو الولاء معطّل).
    """
    s = loyalty_settings(db, domain)
    if not s["enabled"]:
        # نحدّث الإحصاءات حتى مع تعطيل الولاء
        _bump_visit_stats(customer, sale_total)
        db.flush()
        return Decimal("0")
    if is_company_customer(customer):
        _bump_visit_stats(customer, sale_total)
        db.flush()
        return Decimal("0")

    earn_rate = s["earn_per_dinar"]
    points = (Decimal(sale_total) * earn_rate).quantize(Decimal("0.001"))
    if points <= 0:
        _bump_visit_stats(customer, sale_total)
        db.flush()
        return Decimal("0")

    customer.points_balance = (
        Decimal(customer.points_balance or 0) + points
    )
    _bump_visit_stats(customer, sale_total)

    txn = LoyaltyTransaction(
        customer_id=customer.id,
        sale_id=sale_id if sale_id else None,
        kind=LoyaltyTxnKind.EARN,
        points=points,
        note=f"كسب من فاتورة #{sale_id}" if sale_id else "كسب من فاتورة",
        created_by_id=user_id,
    )
    db.add(txn)
    db.flush()
    if not skip_notification:
        try:
            from modules.notifications.hooks import emit_loyalty_points_earned

            emit_loyalty_points_earned(
                db, customer=customer, sale_id=sale_id, points=points
            )
        except Exception:  # noqa: BLE001
            pass
    return points


def attach_customer_to_sale(
    db: Session,
    sale,
    *,
    phone: str | None = None,
    name: str | None = None,
    business_domain: CustomerBusinessDomain | str | None = None,
) -> Customer | None:
    """يربط عميلاً بالفاتورة (إنشاء أو بحث بالهاتف). يعيد None إن لم يُدخل هاتف."""
    p = normalize_phone(phone or "")
    nm = (name or "").strip()
    dom = business_domain if business_domain is not None else domain_from_sale(sale)
    if p:
        existing = get_by_phone_any(db, p)
        if existing is None and not nm:
            raise CustomersError(
                "اسم العميل مطلوب عند تسجيل عميل جديد بهذا الرقم."
            )
        cust = get_or_create_by_phone(
            db, phone=p, name=nm or None, business_domain=dom
        )
        sale.customer_id = cust.id
        if nm and not (cust.name or "").strip():
            cust.name = nm
            db.flush()
        return cust
    if sale.customer_id:
        cust = get_customer(db, int(sale.customer_id))
        if cust is not None:
            apply_customer_domain(cust, dom)
            db.flush()
        return cust
    return None


def loyalty_redeem_quote(
    db: Session, *, customer: Customer, sale_total: Decimal, domain=None
) -> dict:
    """معاينة أقصى خصم ممكن بالنقاط على فاتورة."""
    from modules.platform.domain_loyalty import loyalty_redeem_quote_for_domain

    quote = loyalty_redeem_quote_for_domain(
        db, customer=customer, sale_total=sale_total, domain=domain
    )
    if is_company_customer(customer):
        quote["can_redeem"] = False
        quote["max_points"] = Decimal("0")
        quote["max_discount"] = Decimal("0")
    return quote


def redeem_points_for_sale(
    db: Session,
    *,
    customer: Customer,
    sale_id: int,
    sale_total: Decimal,
    points_to_use: Decimal | None = None,
    user_id: int | None = None,
    domain=None,
) -> tuple[Decimal, Decimal]:
    """يخصم نقاط العميل ويعيد (النقاط المستخدمة، قيمة الخصم بالدينار)."""
    quote = loyalty_redeem_quote(
        db, customer=customer, sale_total=sale_total, domain=domain
    )
    if not quote["can_redeem"]:
        raise CustomersError(
            "لا يمكن استخدام النقاط — الولاء معطّل، أو النسبة غير مفعّلة، "
            "أو الرصيد أقل من الحد الأدنى."
        )
    pts = (
        Decimal(str(points_to_use))
        if points_to_use is not None
        else quote["max_points"]
    ).quantize(Decimal("0.001"), rounding=ROUND_DOWN)
    if pts <= 0:
        raise CustomersError("حدد عدد نقاط للاستخدام.")
    if pts > quote["max_points"]:
        pts = quote["max_points"]
    if pts < quote["min_points"]:
        raise CustomersError(
            f"الحد الأدنى لاستخدام النقاط هو {quote['min_points']} نقطة."
        )
    discount = points_to_dinars(db, pts, domain)
    new_balance = Decimal(str(customer.points_balance or 0)) - pts
    if new_balance < 0:
        raise CustomersError("رصيد النقاط غير كافٍ.")
    customer.points_balance = new_balance
    txn = LoyaltyTransaction(
        customer_id=customer.id,
        sale_id=sale_id,
        kind=LoyaltyTxnKind.REDEEM,
        points=-pts,
        note=f"خصم نقاط ولاء — فاتورة #{sale_id}",
        created_by_id=user_id,
    )
    db.add(txn)
    db.flush()
    return pts, discount


def loyalty_earn_exists_for_sale(db: Session, sale_id: int) -> bool:
    """هل سبق منح نقاط كسب لهذه الفاتورة؟"""
    n = db.scalar(
        select(func.count(LoyaltyTransaction.id)).where(
            LoyaltyTransaction.sale_id == sale_id,
            LoyaltyTransaction.kind == LoyaltyTxnKind.EARN,
        )
    )
    return int(n or 0) > 0


def points_earned_on_sale(
    db: Session, sale_id: int, customer_id: int | None
) -> Decimal:
    """إجمالي نقاط الكسب المرتبطة بفاتورة لعميل محدد."""
    if not customer_id:
        return Decimal("0")
    total = db.execute(
        select(func.coalesce(func.sum(LoyaltyTransaction.points), 0)).where(
            LoyaltyTransaction.customer_id == customer_id,
            LoyaltyTransaction.sale_id == sale_id,
            LoyaltyTransaction.kind == LoyaltyTxnKind.EARN,
        )
    ).scalar_one()
    return Decimal(str(total or 0)).quantize(Decimal("0.001"))


def points_redeemed_on_sale(
    db: Session, sale_id: int, customer_id: int | None
) -> Decimal:
    """نقاط الاستخدام (موجبة) المرتبطة بفاتورة."""
    if not customer_id:
        return Decimal("0")
    total = db.execute(
        select(func.coalesce(func.sum(LoyaltyTransaction.points), 0)).where(
            LoyaltyTransaction.customer_id == customer_id,
            LoyaltyTransaction.sale_id == sale_id,
            LoyaltyTransaction.kind == LoyaltyTxnKind.REDEEM,
        )
    ).scalar_one()
    pts = abs(Decimal(str(total or 0))).quantize(Decimal("0.001"))
    return pts


def format_loyalty_points_display(pts: Decimal) -> str:
    dec = Decimal(str(pts or 0)).quantize(Decimal("0.001"))
    text = format(dec.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


def loyalty_discount_for_sale(
    db: Session, sale_id: int, customer_id: int | None
) -> Decimal:
    """قيمة خصم نقاط الولاء على فاتورة (د.ل)."""
    redeemed = points_redeemed_on_sale(db, sale_id, customer_id)
    if redeemed <= 0:
        return Decimal("0")
    return points_to_dinars(db, redeemed).quantize(Decimal("0.001"))


def sale_amount_paid(
    db: Session,
    sale_id: int,
    *,
    invoice_total: Decimal,
    loyalty_discount: Decimal,
) -> Decimal:
    """المبلغ المحصّل نقداً/مصرفاً على الطلب (بعد خصم النقاط)."""
    from modules.payments.service import sum_sale_payments

    paid_total = sum_sale_payments(db, sale_id)
    if paid_total > 0:
        return paid_total
    paid = (invoice_total - loyalty_discount).quantize(Decimal("0.001"))
    return paid if paid > 0 else Decimal("0")


def build_receipt_loyalty_context(
    db: Session, sale, sale_payment=None
) -> dict:
    """سياق نقاط الولاء والمبالغ المدفوعة للفاتورة المطبوعة."""
    from modules.customers.models import Customer
    from modules.sales.models import ExternalOrderType, SaleContext

    empty_payment = {
        "receipt_show_payment_breakdown": False,
        "receipt_invoice_total_display": format_money_plain(sale.total or 0),
        "receipt_loyalty_discount_display": "0",
        "receipt_amount_paid_display": format_money_plain(sale.total or 0),
        "receipt_amount_customer_total_display": format_money_plain(
            sale.total or 0
        ),
    }
    customer = sale.customer
    if customer is None and sale.customer_id:
        customer = db.get(Customer, sale.customer_id)

    invoice_total = Decimal(str(sale.total or 0)).quantize(Decimal("0.001"))
    is_delivery = (
        sale.context_type == SaleContext.EXTERNAL
        and sale.external_order_type == ExternalOrderType.DELIVERY
    )
    delivery_fee = (
        Decimal(str(sale.delivery_fee or 0)).quantize(Decimal("0.001"))
        if is_delivery
        else Decimal("0")
    )

    if not sale.customer_id or customer is None:
        amount_paid = sale_amount_paid(
            db,
            sale.id,
            invoice_total=invoice_total,
            loyalty_discount=Decimal("0"),
        )
        customer_total = (amount_paid + delivery_fee).quantize(Decimal("0.001"))
        return {
            "loyalty_show_footer": False,
            "loyalty_show_earned": False,
            "loyalty_points_earned_display": "0",
            "loyalty_points_earned_dinar_display": "0",
            "loyalty_points_redeemed_display": "0",
            "loyalty_redeem_dinar_display": "0",
            "loyalty_balance_display": "0",
            "loyalty_points_display": "0",
            **empty_payment,
            "receipt_amount_paid_display": format_money_plain(amount_paid),
            "receipt_amount_customer_total_display": format_money_plain(
                customer_total
            ),
        }

    earned = points_earned_on_sale(db, sale.id, sale.customer_id)
    redeemed = points_redeemed_on_sale(db, sale.id, sale.customer_id)
    balance = Decimal(str(customer.points_balance or 0)).quantize(Decimal("0.001"))
    redeem_dinar = loyalty_discount_for_sale(db, sale.id, sale.customer_id)

    amount_paid = sale_amount_paid(
        db,
        sale.id,
        invoice_total=invoice_total,
        loyalty_discount=redeem_dinar,
    )
    customer_total = (amount_paid + delivery_fee).quantize(Decimal("0.001"))

    earned_dinar = points_to_dinars(db, earned).quantize(Decimal("0.001"))

    return {
        "loyalty_show_footer": True,
        "loyalty_show_earned": earned > 0,
        "loyalty_points_earned_display": format_loyalty_points_display(earned),
        "loyalty_points_earned_dinar_display": format_money_plain(earned_dinar),
        "loyalty_points_redeemed_display": format_loyalty_points_display(redeemed),
        "loyalty_redeem_dinar_display": format_money_plain(redeem_dinar),
        "loyalty_balance_display": format_loyalty_points_display(balance),
        "loyalty_points_display": format_loyalty_points_display(earned),
        "receipt_show_payment_breakdown": redeem_dinar > 0,
        "receipt_invoice_total_display": format_money_plain(invoice_total),
        "receipt_loyalty_discount_display": format_money_plain(redeem_dinar),
        "receipt_amount_paid_display": format_money_plain(amount_paid),
        "receipt_amount_customer_total_display": format_money_plain(customer_total),
    }


def format_money_plain(value) -> str:
    dec = Decimal(str(value)).quantize(Decimal("0.001"))
    sign = "-" if dec < 0 else ""
    raw = format(abs(dec), "f")
    whole, frac = raw.split(".")
    frac = frac.rstrip("0")
    if frac:
        return f"{sign}{whole}.{frac}"
    return f"{sign}{whole}"


def grant_loyalty_after_sale(
    db: Session,
    *,
    customer: Customer,
    sale_id: int,
    paid_total: Decimal,
    user_id: int | None = None,
    skip_notification: bool = False,
    domain=None,
) -> Decimal:
    """منح نقاط على المبلغ المدفوع فعلياً (بعد خصم النقاط إن وُجد)."""
    if loyalty_earn_exists_for_sale(db, sale_id):
        return Decimal("0")
    paid = Decimal(str(paid_total or 0)).quantize(Decimal("0.001"))
    if paid <= 0:
        return Decimal("0")
    return grant_points_for_sale(
        db,
        customer=customer,
        sale_id=sale_id,
        sale_total=paid,
        user_id=user_id,
        skip_notification=skip_notification,
        domain=domain,
    )


def _bump_visit_stats(customer: Customer, sale_total: Decimal) -> None:
    customer.total_spent = Decimal(customer.total_spent or 0) + Decimal(sale_total)
    customer.visits_count = int(customer.visits_count or 0) + 1
    customer.last_visit_at = datetime.now(timezone.utc)


def adjust_points(
    db: Session,
    *,
    customer_id: int,
    delta_points: Decimal,
    note: str | None,
    user_id: int | None = None,
) -> LoyaltyTransaction:
    """تعديل يدوي من الأدمن (إضافة أو خصم — استخدم سالب للخصم)."""
    c = db.get(Customer, customer_id)
    if c is None:
        raise CustomersError("العميل غير موجود.")
    if is_company_customer(c):
        raise CustomersError("نقاط الولاء للأفراد فقط — حساب الشركة بلا نقاط.")
    delta = Decimal(delta_points).quantize(Decimal("0.001"))
    new_balance = Decimal(c.points_balance or 0) + delta
    if new_balance < 0:
        raise CustomersError(
            f"التعديل سيجعل الرصيد سالباً ({new_balance}). "
            "أَدخل قيمة أصغر."
        )
    c.points_balance = new_balance
    txn = LoyaltyTransaction(
        customer_id=customer_id,
        sale_id=None,
        kind=LoyaltyTxnKind.ADJUST,
        points=delta,
        note=(note or "تعديل يدوي").strip(),
        created_by_id=user_id,
    )
    db.add(txn)
    db.flush()
    return txn


def list_transactions(
    db: Session, customer_id: int, limit: int = 50
) -> list[LoyaltyTransaction]:
    stmt = (
        select(LoyaltyTransaction)
        .where(LoyaltyTransaction.customer_id == customer_id)
        .order_by(LoyaltyTransaction.id.desc())
        .limit(limit)
    )
    return list(db.scalars(stmt))


def total_points_grand(
    db: Session,
    *,
    business_domain: CustomerBusinessDomain | str | None = None,
    only_active: bool = True,
) -> Decimal:
    """إجمالي نقاط الأفراد فقط — الشركات لا تدخل الولاء حتى لو بقي رصيد قديم."""
    stmt = select(func.coalesce(func.sum(Customer.points_balance), 0)).where(
        Customer.customer_type == CustomerType.INDIVIDUAL
    )
    if only_active:
        stmt = stmt.where(Customer.is_active.is_(True))
    dom = parse_customer_domain(
        business_domain.value if hasattr(business_domain, "value") else business_domain  # type: ignore[arg-type]
    )
    if dom is not None:
        stmt = stmt.where(
            Customer.business_domain.in_((dom, CustomerBusinessDomain.SHARED))
        )
    val = db.scalar(stmt)
    return Decimal(val or 0)
