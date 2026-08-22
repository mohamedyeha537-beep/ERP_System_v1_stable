"""وكالات السياحة: اسم الشركة + نسبة العمولة (إدارة أدمن) + تسوية المحفظة."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.customers.models import Customer, CustomerType
from modules.hotel.company_agreement_models import AgreementType, CompanyAgreement
from modules.hotel.company_agreement_service import (
    AgreementError,
    ensure_default_agreement,
    update_agreement,
)

Q = Decimal("0.001")


class TourismAgencyError(ValueError):
    pass


@dataclass
class TourismAgencyRow:
    customer_id: int
    name: str
    phone: str | None
    commission_percent: Decimal
    wallet_balance: Decimal
    agreement_id: int | None
    is_active: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.customer_id,
            "name": self.name,
            "phone": self.phone or "",
            "commission_percent": str(self.commission_percent.quantize(Q)),
            "wallet_balance": str(self.wallet_balance.quantize(Q)),
            "agreement_id": self.agreement_id,
            "is_active": self.is_active,
        }


def _label(c: Customer) -> str:
    return (c.company_name or c.name or "").strip() or f"شركة #{c.id}"


def _pct(row: CompanyAgreement | None) -> Decimal:
    if row is None:
        return Decimal("0")
    try:
        return Decimal(str(row.commission_percent or 0)).quantize(Q)
    except (InvalidOperation, ValueError):
        return Decimal("0")


def list_tourism_agencies(
    db: Session,
    *,
    only_active: bool = True,
    only_with_rate: bool = True,
) -> list[TourismAgencyRow]:
    """شركات مسجّلة كوكالات سياحة (عقد COMMISSION)."""
    agr_q = select(CompanyAgreement).where(
        CompanyAgreement.agreement_type == AgreementType.COMMISSION.value,
    )
    if only_active:
        agr_q = agr_q.where(CompanyAgreement.is_active.is_(True))
    agreements = list(db.scalars(agr_q).all())
    # أحدث/افتراضي لكل شركة
    by_company: dict[int, CompanyAgreement] = {}
    for a in agreements:
        cid = int(a.company_customer_id)
        prev = by_company.get(cid)
        if prev is None:
            by_company[cid] = a
            continue
        # فضّل is_default ثم أعلى id
        score = (1 if a.is_default else 0, a.id or 0)
        pscore = (1 if prev.is_default else 0, prev.id or 0)
        if score > pscore:
            by_company[cid] = a

    out: list[TourismAgencyRow] = []
    for cid, agr in by_company.items():
        c = db.get(Customer, cid)
        if c is None or c.customer_type != CustomerType.COMPANY:
            continue
        if only_active and not c.is_active:
            continue
        pct = _pct(agr)
        if only_with_rate and pct <= 0:
            continue
        out.append(
            TourismAgencyRow(
                customer_id=cid,
                name=_label(c),
                phone=(c.phone or None),
                commission_percent=pct,
                wallet_balance=Decimal(str(c.wallet_balance or 0)).quantize(Q),
                agreement_id=int(agr.id) if agr.id else None,
                is_active=bool(c.is_active and agr.is_active),
            )
        )
    out.sort(key=lambda r: r.name.casefold())
    return out


def get_agency_rate(db: Session, company_customer_id: int | None) -> Decimal | None:
    """نسبة العمولة إن كانت الشركة وكالة سياحة نشطة، وإلا None."""
    if not company_customer_id:
        return None
    cid = int(company_customer_id)
    agr = db.scalar(
        select(CompanyAgreement)
        .where(
            CompanyAgreement.company_customer_id == cid,
            CompanyAgreement.agreement_type == AgreementType.COMMISSION.value,
            CompanyAgreement.is_active.is_(True),
        )
        .order_by(CompanyAgreement.is_default.desc(), CompanyAgreement.id.desc())
        .limit(1)
    )
    if agr is None:
        return None
    c = db.get(Customer, cid)
    if c is None or not c.is_active or c.customer_type != CustomerType.COMPANY:
        return None
    pct = _pct(agr)
    if pct <= 0:
        return None
    return pct


def resolve_tourism_for_booking(
    db: Session, *, tourism_agency_id: int | None, company_customer_id: int | None
) -> tuple[bool, Decimal, int | None]:
    """
    يُرجع (is_tourism, commission_percent, company_customer_id).
    المصدر الوحيد للنسبة: عقد الأدمن — ليس إدخال الموظف.
    """
    cid = tourism_agency_id or company_customer_id
    if not cid:
        return False, Decimal("0"), company_customer_id
    pct = get_agency_rate(db, int(cid))
    if pct is None:
        # اختيار صريح لوكالة غير صالحة
        if tourism_agency_id:
            raise TourismAgencyError(
                "شركة السياحة المختارة غير معرّفة أو بلا نسبة عمولة. "
                "أضفها من «وكالات السياحة» في الإعدادات."
            )
        return False, Decimal("0"), company_customer_id
    return True, pct, int(cid)


def create_or_update_tourism_agency(
    db: Session,
    *,
    company_name: str,
    commission_percent: Decimal | str | float,
    phone: str | None = None,
    customer_id: int | None = None,
    notes: str | None = None,
) -> Customer:
    """إنشاء/تحديث شركة كوكالة سياحة بنسبة عمولة (أدمن فقط)."""
    from modules.customers.service import (
        CustomersError,
        ensure_company_customer,
        update_customer,
    )

    name = (company_name or "").strip()
    if len(name) < 2:
        raise TourismAgencyError("أدخل اسم شركة السياحة (حرفان على الأقل).")
    try:
        pct = Decimal(str(commission_percent or "0").replace(",", ".")).quantize(Q)
    except (InvalidOperation, ValueError) as exc:
        raise TourismAgencyError("نسبة العمولة غير صالحة.") from exc
    if pct <= 0 or pct > 100:
        raise TourismAgencyError("نسبة العمولة يجب أن تكون بين 0.001 و 100.")

    try:
        if customer_id:
            c = db.get(Customer, int(customer_id))
            if c is None or c.customer_type != CustomerType.COMPANY:
                raise TourismAgencyError("حساب الشركة غير موجود.")
            kwargs: dict[str, object] = {"name": name, "company_name": name}
            phone_s = (phone or "").strip()
            if phone_s:
                kwargs["phone"] = phone_s
            update_customer(db, int(customer_id), **kwargs)
            company = c
        else:
            company = ensure_company_customer(
                db,
                company_name=name,
                phone=(phone or "").strip() or None,
                company_customer_id=None,
            )
            company.company_name = name
            company.name = name
    except CustomersError as exc:
        raise TourismAgencyError(str(exc)) from exc

    agr = ensure_default_agreement(db, int(company.id))
    try:
        update_agreement(
            db,
            agr.id,
            name="عقد وكالة سياحة",
            agreement_type=AgreementType.COMMISSION.value,
            commission_percent=pct,
            commission_fixed=Decimal("0"),
            notes=(notes or "").strip() or "عمولة سياحة — تُدار من لوحة الوكالات",
            is_active=True,
        )
    except AgreementError as exc:
        raise TourismAgencyError(str(exc)) from exc
    db.flush()
    return company


def deactivate_tourism_agency(db: Session, company_customer_id: int) -> None:
    """إيقاف عقد العمولة (لا حذف السجل التاريخي)."""
    cid = int(company_customer_id)
    rows = list(
        db.scalars(
            select(CompanyAgreement).where(
                CompanyAgreement.company_customer_id == cid,
                CompanyAgreement.agreement_type == AgreementType.COMMISSION.value,
            )
        ).all()
    )
    if not rows:
        raise TourismAgencyError("لا يوجد عقد وكالة لهذه الشركة.")
    for a in rows:
        a.is_active = False
    db.flush()


def settle_agency_commission(
    db: Session,
    company_customer_id: int,
    *,
    user_id: int | None = None,
    note: str | None = None,
    amount: Decimal | str | float | None = None,
) -> Decimal:
    """
    تسوية رصيد عمولات الشركة: خصم من المحفظة (صفر أو مبلغ محدد موجب).
    يُمثّل صرف المستحق للوكالة وبدء دورة معاملات جديدة.
    """
    from modules.customers.service import CustomersError, adjust_wallet
    from modules.hotel.audit import log_audit

    c = db.get(Customer, int(company_customer_id))
    if c is None or c.customer_type != CustomerType.COMPANY:
        raise TourismAgencyError("حساب الشركة غير موجود.")
    bal = Decimal(str(c.wallet_balance or 0)).quantize(Q)
    if bal <= Decimal("0.0005"):
        raise TourismAgencyError("لا يوجد رصيد عمولات موجب لتسويته.")

    if amount is None or str(amount).strip() == "":
        settle_amt = bal
    else:
        try:
            settle_amt = Decimal(str(amount).replace(",", ".")).quantize(Q)
        except (InvalidOperation, ValueError) as exc:
            raise TourismAgencyError("مبلغ التسوية غير صالح.") from exc
        if settle_amt <= 0:
            raise TourismAgencyError("مبلغ التسوية يجب أن يكون أكبر من صفر.")
        if settle_amt > bal + Decimal("0.0005"):
            raise TourismAgencyError(
                f"المبلغ ({settle_amt}) يتجاوز رصيد المحفظة ({bal})."
            )

    reason = (note or "").strip() or "تسوية عمولات وكالة سياحة — تصفير المستحق"
    try:
        adjust_wallet(
            db,
            int(company_customer_id),
            amount=-settle_amt,
            note=f"تسوية عمولات: {reason}",
            user_id=user_id,
        )
    except CustomersError as exc:
        raise TourismAgencyError(str(exc)) from exc

    try:
        log_audit(
            db,
            entity_type="customer",
            entity_id=int(company_customer_id),
            action="tourism_commission_settle",
            new_value=str(settle_amt),
            reason=reason,
            user_id=user_id,
        )
    except Exception:  # noqa: BLE001
        pass
    return settle_amt


def tourism_discount_from_gross(gross: Decimal, commission_percent: Decimal) -> Decimal:
    """مبلغ يُخصم من قيمة الإقامة = العمولة (نسبة من الإجمالي قبل الخصم)."""
    g = Decimal(str(gross or 0)).quantize(Q)
    pct = Decimal(str(commission_percent or 0)).quantize(Q)
    if g <= 0 or pct <= 0:
        return Decimal("0")
    return (g * pct / Decimal("100")).quantize(Q)


def commission_from_net_payment(
    payment_amount: Decimal, commission_percent: Decimal
) -> Decimal:
    """
    عند خصم العمولة من الحجز مسبقاً (الضيف يدفع الصافي):
    عمولة = دفع × pct / (100 − pct)
    بحيث إجمالي العمولة على دفع كامل الصافي = عمولة الإجمالي.
    """
    pay = Decimal(str(payment_amount or 0)).quantize(Q)
    pct = Decimal(str(commission_percent or 0)).quantize(Q)
    if pay <= 0 or pct <= 0:
        return Decimal("0")
    if pct >= Decimal("100"):
        return Decimal("0")
    denom = Decimal("100") - pct
    if denom <= 0:
        return Decimal("0")
    return (pay * pct / denom).quantize(Q)
