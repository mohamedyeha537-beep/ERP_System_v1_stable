"""خدمة عقود الشركة + توجيه البنود إلى حساب الشركة/النزيل."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.hotel.company_agreement_models import (
    SERVICE_CATALOG,
    AgreementChangeStatus,
    AgreementType,
    Bearer,
    BookingServiceRule,
    CompanyAgreement,
    CompanyAgreementChangeRequest,
    CompanyAgreementService,
)

Q = Decimal("0.001")


class AgreementError(Exception):
    pass


@dataclass
class ChargeSplit:
    service_code: str
    bearer: str  # COMPANY | GUEST | SHARED (نتيجة التوزيع النهائية غالباً COMPANY/GUEST)
    company_amount: Decimal
    guest_amount: Decimal
    name_ar: str = ""

    @property
    def total(self) -> Decimal:
        return (self.company_amount + self.guest_amount).quantize(Q)

    @property
    def charged_to_guest(self) -> bool:
        """هل يظهر أي جزء على حساب النزيل (للتوافق مع charged_to_guest القديم"""
        return self.guest_amount > Q

    @property
    def folio_side(self) -> str:
        if self.company_amount > Q and self.guest_amount > Q:
            return "SHARED"
        if self.company_amount > Q:
            return "COMPANY"
        return "GUEST"


def ensure_default_agreement(db: Session, company_customer_id: int) -> CompanyAgreement:
    """يُنشئ عقداً افتراضياً بجدول خدمات إن لم يوجد."""
    row = db.scalar(
        select(CompanyAgreement)
        .where(
            CompanyAgreement.company_customer_id == int(company_customer_id),
            CompanyAgreement.is_active.is_(True),
        )
        .order_by(CompanyAgreement.is_default.desc(), CompanyAgreement.id.desc())
        .limit(1)
    )
    if row is not None:
        if not list(row.services or []):
            _seed_default_services(db, row)
        return row
    row = CompanyAgreement(
        company_customer_id=int(company_customer_id),
        name="العقد الافتراضي",
        agreement_type=AgreementType.STANDARD.value,
        payment_terms="آجل حسب الاتفاق",
        is_active=True,
        is_default=True,
    )
    db.add(row)
    db.flush()
    _seed_default_services(db, row)
    return row


def _seed_default_services(db: Session, agreement: CompanyAgreement) -> None:
    existing = {s.service_code for s in (agreement.services or [])}
    for i, (code, name, bearer) in enumerate(SERVICE_CATALOG):
        if code in existing:
            continue
        db.add(
            CompanyAgreementService(
                agreement_id=agreement.id,
                service_code=code,
                name_ar=name,
                bearer=bearer,
                sort_order=i * 10,
                is_active=True,
            )
        )
    db.flush()


def get_agreement(db: Session, agreement_id: int) -> CompanyAgreement | None:
    return db.get(CompanyAgreement, int(agreement_id))


def get_default_agreement_for_company(
    db: Session, company_customer_id: int | None
) -> CompanyAgreement | None:
    if not company_customer_id:
        return None
    return ensure_default_agreement(db, int(company_customer_id))


def agreement_preview_for_company(db: Session, company_customer_id: int) -> dict:
    """معاينة عقد الشركة للاستقبال (من يدفع ماذا + شروط السداد) — قراءة فقط."""
    from modules.customers.models import Customer

    cid = int(company_customer_id)
    cust = db.get(Customer, cid)
    if cust is None:
        return {"ok": False, "error": "الشركة غير موجودة."}

    agr = db.scalar(
        select(CompanyAgreement)
        .where(
            CompanyAgreement.company_customer_id == cid,
            CompanyAgreement.is_active.is_(True),
        )
        .order_by(CompanyAgreement.is_default.desc(), CompanyAgreement.id.desc())
    )
    label_map = {code: name for code, name, _b in SERVICE_CATALOG}
    services_out: list[dict] = []
    if agr is None:
        for code, name, bearer in SERVICE_CATALOG:
            services_out.append(
                {
                    "code": code,
                    "name_ar": name,
                    "bearer": bearer,
                    "limit_amount": None,
                    "limit_quantity": None,
                }
            )
        return {
            "ok": True,
            "company_id": cid,
            "company_name": (cust.company_name or cust.name or "").strip(),
            "agreement_id": None,
            "agreement_name": "العقد الافتراضي (سيُنشأ عند الحفظ)",
            "agreement_type": "STANDARD",
            "payment_terms": "آجل حسب الاتفاق",
            "commission_percent": "0",
            "commission_fixed": "0",
            "services": services_out,
            "pending_change_requests": 0,
        }

    svc_rows = list(agr.services or [])
    if not svc_rows:
        for code, name, bearer in SERVICE_CATALOG:
            services_out.append(
                {
                    "code": code,
                    "name_ar": name,
                    "bearer": bearer,
                    "limit_amount": None,
                    "limit_quantity": None,
                }
            )
    else:
        for s in sorted(svc_rows, key=lambda r: (r.sort_order or 0, r.id or 0)):
            if not s.is_active:
                continue
            services_out.append(
                {
                    "code": s.service_code,
                    "name_ar": s.name_ar or label_map.get(s.service_code, s.service_code),
                    "bearer": (s.bearer or "GUEST").upper(),
                    "limit_amount": (
                        str(s.limit_amount) if s.limit_amount is not None else None
                    ),
                    "limit_quantity": (
                        str(s.limit_quantity) if s.limit_quantity is not None else None
                    ),
                }
            )

    return {
        "ok": True,
        "company_id": cid,
        "company_name": (cust.company_name or cust.name or "").strip(),
        "agreement_id": agr.id,
        "agreement_name": agr.name,
        "agreement_type": (agr.agreement_type or "STANDARD").upper(),
        "payment_terms": agr.payment_terms or "آجل حسب الاتفاق",
        "commission_percent": str(agr.commission_percent or 0),
        "commission_fixed": str(agr.commission_fixed or 0),
        "services": services_out,
        "pending_change_requests": len(
            list_agreement_change_requests(db, cid, status="PENDING", limit=20)
        ),
    }


def list_agreements_for_company(
    db: Session, company_customer_id: int
) -> list[CompanyAgreement]:
    return list(
        db.scalars(
            select(CompanyAgreement)
            .where(CompanyAgreement.company_customer_id == int(company_customer_id))
            .order_by(CompanyAgreement.is_default.desc(), CompanyAgreement.id.desc())
        ).all()
    )


def update_agreement(
    db: Session,
    agreement_id: int,
    *,
    name: str | None = None,
    agreement_type: str | None = None,
    payment_terms: str | None = None,
    commission_percent: Decimal | str | float | None = None,
    commission_fixed: Decimal | str | float | None = None,
    notes: str | None = None,
    is_active: bool | None = None,
) -> CompanyAgreement:
    row = db.get(CompanyAgreement, int(agreement_id))
    if row is None:
        raise AgreementError("العقد غير موجود.")
    if name is not None:
        row.name = (name or "").strip() or row.name
    if agreement_type is not None:
        at = (agreement_type or "").strip().upper()
        if at not in {e.value for e in AgreementType}:
            raise AgreementError("نوع العقد غير صالح.")
        row.agreement_type = at
    if payment_terms is not None:
        row.payment_terms = (payment_terms or "").strip() or None
    if commission_percent is not None:
        row.commission_percent = Decimal(str(commission_percent or 0)).quantize(
            Decimal("0.001")
        )
    if commission_fixed is not None:
        row.commission_fixed = Decimal(str(commission_fixed or 0)).quantize(Q)
    if notes is not None:
        row.notes = (notes or "").strip() or None
    if is_active is not None:
        row.is_active = bool(is_active)
    db.flush()
    return row


def upsert_agreement_service(
    db: Session,
    agreement_id: int,
    *,
    service_code: str,
    name_ar: str,
    bearer: str,
    limit_amount: Decimal | str | float | None = None,
    limit_quantity: Decimal | str | float | None = None,
    sort_order: int = 0,
    is_active: bool = True,
) -> CompanyAgreementService:
    code = (service_code or "").strip().upper()
    if not code:
        raise AgreementError("رمز الخدمة مطلوب.")
    br = (bearer or Bearer.GUEST.value).strip().upper()
    if br not in {e.value for e in Bearer}:
        raise AgreementError("جهة التحمّل غير صالحة (COMPANY/GUEST/SHARED).")
    row = db.scalar(
        select(CompanyAgreementService).where(
            CompanyAgreementService.agreement_id == int(agreement_id),
            CompanyAgreementService.service_code == code,
        )
    )
    lim_a = None
    if limit_amount is not None and str(limit_amount).strip() != "":
        lim_a = Decimal(str(limit_amount)).quantize(Q)
        if lim_a < 0:
            raise AgreementError("حد المبلغ لا يكون سالباً.")
    lim_q = None
    if limit_quantity is not None and str(limit_quantity).strip() != "":
        lim_q = Decimal(str(limit_quantity)).quantize(Decimal("0.0001"))
    if row is None:
        row = CompanyAgreementService(
            agreement_id=int(agreement_id),
            service_code=code,
            name_ar=(name_ar or code).strip(),
            bearer=br,
            limit_amount=lim_a,
            limit_quantity=lim_q,
            sort_order=int(sort_order or 0),
            is_active=bool(is_active),
        )
        db.add(row)
    else:
        row.name_ar = (name_ar or row.name_ar).strip()
        row.bearer = br
        row.limit_amount = lim_a
        row.limit_quantity = lim_q
        row.sort_order = int(sort_order or 0)
        row.is_active = bool(is_active)
    db.flush()
    return row


def save_agreement_services_bulk(
    db: Session,
    agreement_id: int,
    rows: list[dict],
) -> None:
    """يحفظ جدول الخدمات من نموذج الويب."""
    agreement = db.get(CompanyAgreement, int(agreement_id))
    if agreement is None:
        raise AgreementError("العقد غير موجود.")
    for r in rows:
        code = (r.get("service_code") or "").strip().upper()
        if not code:
            continue
        upsert_agreement_service(
            db,
            agreement_id,
            service_code=code,
            name_ar=r.get("name_ar") or code,
            bearer=r.get("bearer") or Bearer.GUEST.value,
            limit_amount=r.get("limit_amount"),
            limit_quantity=r.get("limit_quantity"),
            sort_order=int(r.get("sort_order") or 0),
            is_active=bool(r.get("is_active", True)),
        )


def copy_agreement_rules_to_booking(
    db: Session,
    booking_id: int,
    agreement: CompanyAgreement | None,
    *,
    stay_payer: str | None = None,
    extras_payer: str | None = None,
) -> list[BookingServiceRule]:
    """ينسخ قواعد العقد إلى الحجز (لقطة) مع احترام إعدادات stay/extras العامة."""
    # امسح القواعد القديمة
    for old in list(
        db.scalars(
            select(BookingServiceRule).where(
                BookingServiceRule.booking_id == int(booking_id)
            )
        ).all()
    ):
        db.delete(old)
    db.flush()

    at = (
        (agreement.agreement_type if agreement else AgreementType.STANDARD.value)
        or AgreementType.STANDARD.value
    ).upper()
    stay = (stay_payer or "GUEST").strip().upper()
    extras = (extras_payer or "GUEST").strip().upper()

    out: list[BookingServiceRule] = []
    if at == AgreementType.BOOKING_ONLY.value or agreement is None:
        # كل شيء على النزيل (إلا إن stay_payer=COMPANY للإقامة فقط)
        for i, (code, name, _b) in enumerate(SERVICE_CATALOG):
            br = stay if code == "ACCOMMODATION" else "GUEST"
            if agreement is None and code == "ACCOMMODATION":
                br = stay
            elif agreement is None and code != "ACCOMMODATION":
                br = extras
            rule = BookingServiceRule(
                booking_id=int(booking_id),
                service_code=code,
                name_ar=name,
                bearer=br if br in ("COMPANY", "GUEST", "SHARED") else "GUEST",
            )
            db.add(rule)
            out.append(rule)
        db.flush()
        return out

    # من العقد
    svc_map = {
        s.service_code: s for s in (agreement.services or []) if s.is_active
    }
    for i, (code, name, default_bearer) in enumerate(SERVICE_CATALOG):
        s = svc_map.get(code)
        if s is not None:
            br = (s.bearer or default_bearer).upper()
            rule = BookingServiceRule(
                booking_id=int(booking_id),
                service_code=code,
                name_ar=s.name_ar or name,
                bearer=br,
                limit_amount=s.limit_amount,
                limit_quantity=s.limit_quantity,
            )
        else:
            # بنود غير معرفة: extras_payer للإضافات
            br = stay if code == "ACCOMMODATION" else extras
            rule = BookingServiceRule(
                booking_id=int(booking_id),
                service_code=code,
                name_ar=name,
                bearer=br if br in ("COMPANY", "GUEST", "SHARED") else "GUEST",
            )
        db.add(rule)
        out.append(rule)
    db.flush()
    return out


def get_booking_rule(
    db: Session, booking_id: int, service_code: str
) -> BookingServiceRule | None:
    code = (service_code or "").strip().upper()
    return db.scalar(
        select(BookingServiceRule).where(
            BookingServiceRule.booking_id == int(booking_id),
            BookingServiceRule.service_code == code,
        )
    )


def infer_service_code(
    *,
    name_ar: str | None = None,
    explicit: str | None = None,
    is_pos: bool = False,
    is_laundry: bool = False,
    is_breakfast: bool = False,
) -> str:
    if explicit:
        return explicit.strip().upper()
    if is_breakfast:
        return "BREAKFAST"
    if is_laundry:
        return "LAUNDRY"
    if is_pos:
        return "POS_RESTAURANT"
    text = (name_ar or "").strip().lower()
    mapping = (
        ("إفطار", "BREAKFAST"),
        ("breakfast", "BREAKFAST"),
        ("غداء", "LUNCH"),
        ("lunch", "LUNCH"),
        ("عشاء", "DINNER"),
        ("dinner", "DINNER"),
        ("غسيل", "LAUNDRY"),
        ("مغسلة", "LAUNDRY"),
        ("laundry", "LAUNDRY"),
        ("مخالفة", "VIOLATION"),
        ("اتلاف", "VIOLATION"),
        ("إتلاف", "VIOLATION"),
        ("فقدان", "VIOLATION"),
        ("violation", "VIOLATION"),
        ("تالف", "DAMAGE"),
        ("تلف", "DAMAGE"),
        ("ميني", "MINI_BAR"),
        ("minibar", "MINI_BAR"),
        ("مغادرة", "LATE_CHECKOUT"),
        ("late", "LATE_CHECKOUT"),
        ("إنترنت", "INTERNET"),
        ("internet", "INTERNET"),
        ("مطار", "AIRPORT_PICKUP"),
        ("مواصل", "TRANSPORT"),
        ("إقامة", "ACCOMMODATION"),
    )
    for key, code in mapping:
        if key in text:
            return code
    return "OTHER"


def allocate_charge(
    db: Session,
    booking_id: int,
    *,
    amount: Decimal | str | float,
    service_code: str,
    quantity: Decimal | str | float = 1,
    name_ar: str = "",
    consume_limit: bool = True,
) -> ChargeSplit:
    """يوزّع المبلغ بين شركة/نزيل حسب لقطة قواعد الحجز + الحدود.

    يحدّث company_used_amount عند consume_limit.
    """
    amt = Decimal(str(amount or 0)).quantize(Q)
    qty = Decimal(str(quantity or 1)).quantize(Decimal("0.0001"))
    if amt < 0:
        amt = Decimal("0")
    code = (service_code or "OTHER").strip().upper()
    rule = get_booking_rule(db, booking_id, code)
    # حجز بلا قواعد: استخدم stay/extras من الحجز
    if rule is None:
        from modules.hotel.booking_models import HotelBooking

        booking = db.get(HotelBooking, int(booking_id))
        if booking is None:
            return ChargeSplit(
                service_code=code,
                bearer="GUEST",
                company_amount=Decimal("0"),
                guest_amount=amt,
                name_ar=name_ar,
            )
        # fallback: copy quick from booking payers
        stay = (getattr(booking, "stay_payer", None) or "GUEST").upper()
        extras = (getattr(booking, "extras_payer", None) or "GUEST").upper()
        if code == "ACCOMMODATION" and stay == "COMPANY":
            return ChargeSplit(
                service_code=code,
                bearer="COMPANY",
                company_amount=amt,
                guest_amount=Decimal("0"),
                name_ar=name_ar or "إقامة",
            )
        if extras == "COMPANY":
            return ChargeSplit(
                service_code=code,
                bearer="COMPANY",
                company_amount=amt,
                guest_amount=Decimal("0"),
                name_ar=name_ar,
            )
        return ChargeSplit(
            service_code=code,
            bearer="GUEST",
            company_amount=Decimal("0"),
            guest_amount=amt,
            name_ar=name_ar,
        )

    bearer = (rule.bearer or Bearer.GUEST.value).upper()
    name = name_ar or rule.name_ar or code

    if bearer == Bearer.GUEST.value:
        return ChargeSplit(
            service_code=code,
            bearer="GUEST",
            company_amount=Decimal("0"),
            guest_amount=amt,
            name_ar=name,
        )

    # COMPANY أو SHARED — طبّق الحد
    rem_limit = None
    if rule.limit_amount is not None:
        used = Decimal(str(rule.company_used_amount or 0)).quantize(Q)
        cap = Decimal(str(rule.limit_amount)).quantize(Q)
        rem_limit = max(Decimal("0"), (cap - used).quantize(Q))

    if bearer == Bearer.COMPANY.value:
        if rem_limit is None:
            company_amt = amt
            guest_amt = Decimal("0")
        else:
            company_amt = min(amt, rem_limit)
            guest_amt = (amt - company_amt).quantize(Q)
    else:  # SHARED
        if rem_limit is None:
            # SHARED بلا حد = شركة كامل (يمكن ضبطه لاحقاً 50/50)
            company_amt = amt
            guest_amt = Decimal("0")
        else:
            company_amt = min(amt, rem_limit)
            guest_amt = (amt - company_amt).quantize(Q)

    if consume_limit and company_amt > Q:
        rule.company_used_amount = (
            Decimal(str(rule.company_used_amount or 0)) + company_amt
        ).quantize(Q)
        rule.company_used_qty = (
            Decimal(str(rule.company_used_qty or 0)) + qty
        ).quantize(Decimal("0.0001"))
        db.flush()

    out_bearer = (
        "SHARED"
        if company_amt > Q and guest_amt > Q
        else ("COMPANY" if company_amt > Q else "GUEST")
    )
    return ChargeSplit(
        service_code=code,
        bearer=out_bearer,
        company_amount=company_amt.quantize(Q),
        guest_amount=guest_amt.quantize(Q),
        name_ar=name,
    )


def attach_agreement_to_booking(
    db: Session,
    booking,
    *,
    agreement_id: int | None = None,
    stay_payer: str | None = None,
    extras_payer: str | None = None,
) -> CompanyAgreement | None:
    """يربط الحجز بالعقد وينسخ القواعد."""
    company_id = getattr(booking, "company_customer_id", None)
    agreement = None
    if agreement_id:
        agreement = get_agreement(db, int(agreement_id))
    elif company_id:
        agreement = get_default_agreement_for_company(db, int(company_id))
    if agreement is not None:
        booking.company_agreement_id = int(agreement.id)
    copy_agreement_rules_to_booking(
        db,
        int(booking.id),
        agreement,
        stay_payer=stay_payer or getattr(booking, "stay_payer", None),
        extras_payer=extras_payer or getattr(booking, "extras_payer", None),
    )
    return agreement


def _parse_change_items(raw) -> list[dict]:
    if isinstance(raw, list):
        rows = raw
    else:
        try:
            rows = json.loads(raw or "[]")
        except (TypeError, ValueError):
            rows = []
    out: list[dict] = []
    label = {code: name for code, name, _b in SERVICE_CATALOG}
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = (row.get("code") or row.get("service_code") or "").strip().upper()
        if not code or code in seen:
            continue
        to_b = (row.get("to_bearer") or row.get("to") or "").strip().upper()
        if to_b not in ("COMPANY", "GUEST", "SHARED"):
            continue
        from_b = (row.get("from_bearer") or row.get("from") or "").strip().upper()
        if from_b == to_b:
            continue
        seen.add(code)
        out.append(
            {
                "code": code,
                "name_ar": (row.get("name_ar") or label.get(code) or code).strip(),
                "from_bearer": from_b or "GUEST",
                "to_bearer": to_b,
            }
        )
    return out


def create_agreement_change_request(
    db: Session,
    *,
    company_customer_id: int,
    items: list[dict] | str,
    note: str | None = None,
    attachment_path: str | None = None,
    attachment_name: str | None = None,
    booking_id: int | None = None,
    user_id: int | None = None,
) -> CompanyAgreementChangeRequest:
    parsed = _parse_change_items(items)
    if not parsed:
        raise AgreementError("حدّد بنداً واحداً على الأقل تريد الشركة تغيير من يتحمّله.")
    if not (attachment_path or "").strip() and not (note or "").strip():
        raise AgreementError("أرفق طلب الشركة الكتابي (صورة/PDF) أو اكتب ملاحظة.")
    agr = get_default_agreement_for_company(db, int(company_customer_id))
    row = CompanyAgreementChangeRequest(
        company_customer_id=int(company_customer_id),
        agreement_id=int(agr.id) if agr is not None else None,
        booking_id=int(booking_id) if booking_id else None,
        requested_by_user_id=int(user_id) if user_id else None,
        status=AgreementChangeStatus.PENDING.value,
        note=(note or "").strip() or None,
        attachment_path=(attachment_path or "").strip() or None,
        attachment_name=(attachment_name or "").strip() or None,
        items_json=json.dumps(parsed, ensure_ascii=False),
    )
    db.add(row)
    db.flush()
    try:
        from modules.dashboard_notify.constants import CUSTOMERS
        from modules.dashboard_notify.service import record_activity

        names = "، ".join(f"{i['name_ar']}←{i['to_bearer']}" for i in parsed[:6])
        record_activity(
            db,
            CUSTOMERS,
            event_type="agreement_change_request",
            ref_id=int(company_customer_id),
            note=f"طلب تعديل عقد شركة #{row.id}: {names}",
        )
    except Exception:  # noqa: BLE001
        pass
    return row


def list_agreement_change_requests(
    db: Session,
    company_customer_id: int,
    *,
    status: str | None = "PENDING",
    limit: int = 20,
) -> list[CompanyAgreementChangeRequest]:
    q = select(CompanyAgreementChangeRequest).where(
        CompanyAgreementChangeRequest.company_customer_id == int(company_customer_id)
    )
    if status:
        q = q.where(CompanyAgreementChangeRequest.status == status.strip().upper())
    q = q.order_by(CompanyAgreementChangeRequest.id.desc()).limit(max(1, min(100, limit)))
    return list(db.scalars(q).all())


def request_items(row: CompanyAgreementChangeRequest) -> list[dict]:
    return _parse_change_items(row.items_json)


def refresh_open_booking_rules_for_company(
    db: Session, company_customer_id: int
) -> int:
    """يعيد نسخ بنود العقد الحالية إلى الحجوزات المفتوحة لنفس الشركة."""
    from modules.hotel.booking_models import BookingStatus, HotelBooking

    agr = get_default_agreement_for_company(db, int(company_customer_id))
    open_st = (
        BookingStatus.PENDING,
        BookingStatus.CONFIRMED,
        BookingStatus.CHECKED_IN,
    )
    bookings = list(
        db.scalars(
            select(HotelBooking).where(
                HotelBooking.company_customer_id == int(company_customer_id),
                HotelBooking.booking_status.in_(open_st),
            )
        ).all()
    )
    n = 0
    for booking in bookings:
        used_map: dict[str, tuple[Decimal, Decimal]] = {}
        for old in list(
            db.scalars(
                select(BookingServiceRule).where(
                    BookingServiceRule.booking_id == int(booking.id)
                )
            ).all()
        ):
            used_map[old.service_code] = (
                Decimal(str(old.company_used_amount or 0)),
                Decimal(str(old.company_used_qty or 0)),
            )
        rules = copy_agreement_rules_to_booking(
            db,
            int(booking.id),
            agr,
            stay_payer=getattr(booking, "stay_payer", None),
            extras_payer=getattr(booking, "extras_payer", None),
        )
        for rule in rules:
            prev = used_map.get(rule.service_code)
            if prev:
                rule.company_used_amount = prev[0]
                rule.company_used_qty = prev[1]
        if agr is not None:
            booking.company_agreement_id = int(agr.id)
        n += 1
    db.flush()
    return n


def apply_agreement_change_request(
    db: Session,
    request_id: int,
    *,
    user_id: int | None = None,
    review_note: str | None = None,
) -> CompanyAgreementChangeRequest:
    row = db.get(CompanyAgreementChangeRequest, int(request_id))
    if row is None:
        raise AgreementError("طلب التعديل غير موجود.")
    if (row.status or "").upper() != AgreementChangeStatus.PENDING.value:
        raise AgreementError("هذا الطلب ليس معلّقاً.")
    items = request_items(row)
    if not items:
        raise AgreementError("الطلب بلا بنود.")
    agr = get_default_agreement_for_company(db, int(row.company_customer_id))
    if agr is None:
        agr = ensure_default_agreement(db, int(row.company_customer_id))
    for item in items:
        existing = db.scalar(
            select(CompanyAgreementService).where(
                CompanyAgreementService.agreement_id == int(agr.id),
                CompanyAgreementService.service_code == item["code"],
            )
        )
        upsert_agreement_service(
            db,
            int(agr.id),
            service_code=item["code"],
            name_ar=item["name_ar"],
            bearer=item["to_bearer"],
            limit_amount=existing.limit_amount if existing else None,
            limit_quantity=existing.limit_quantity if existing else None,
            sort_order=(existing.sort_order if existing else 0),
        )
    refresh_open_booking_rules_for_company(db, int(row.company_customer_id))
    row.status = AgreementChangeStatus.APPLIED.value
    row.reviewed_at = datetime.now(timezone.utc)
    row.reviewed_by_user_id = int(user_id) if user_id else None
    row.review_note = (review_note or "").strip() or None
    row.agreement_id = int(agr.id)
    db.flush()
    return row


def reject_agreement_change_request(
    db: Session,
    request_id: int,
    *,
    user_id: int | None = None,
    review_note: str | None = None,
) -> CompanyAgreementChangeRequest:
    row = db.get(CompanyAgreementChangeRequest, int(request_id))
    if row is None:
        raise AgreementError("طلب التعديل غير موجود.")
    if (row.status or "").upper() != AgreementChangeStatus.PENDING.value:
        raise AgreementError("هذا الطلب ليس معلّقاً.")
    row.status = AgreementChangeStatus.REJECTED.value
    row.reviewed_at = datetime.now(timezone.utc)
    row.reviewed_by_user_id = int(user_id) if user_id else None
    row.review_note = (review_note or "").strip() or None
    db.flush()
    return row
