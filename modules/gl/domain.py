"""فلترة GL حسب مجال العمل (مطعم / فندق / مشترك)."""
from __future__ import annotations

from modules.platform.business_domain import (
    BusinessDomain,
    domain_record_visible,
    purchase_domain_db_values,
)

_SHARED_ACCOUNT_CODES = frozenset(
    {"1000", "1100", "2000", "3000", "4000", "5000", "3100", "3500", "3600"}
)
_HOTEL_ACCOUNT_CODES = frozenset(
    {
        "1115",
        "1125",
        "1135",
        "1136",
        "4150",
        "1500",
        "1510",
        "1520",
        "1530",
        "1540",
        "1550",
        "2500",
        "2510",
        "2520",
        "3510",
        "4500",
        "4520",
        "5500",
        "5510",
        "5520",
        "5521",
        "5522",
        "5523",
        "5524",
        "5525",
        "5530",
        "5540",
    }
)
_RESTAURANT_CUSTODY_CODES = frozenset({"1130", "1132"})


def gl_entry_db_values(filter_domain: BusinessDomain | None) -> list[str] | None:
    """قيم business_domain للقيود المرئية في مجال العمل."""
    return purchase_domain_db_values(filter_domain)


def gl_account_visible(
    record_domain: str | BusinessDomain | None,
    *,
    filter_domain: BusinessDomain | None,
) -> bool:
    return domain_record_visible(record_domain, filter_domain=filter_domain)


def filter_gl_accounts(accounts, filter_domain: BusinessDomain | None):
    if filter_domain is None:
        return list(accounts)
    return [
        acc
        for acc in accounts
        if gl_account_visible(
            getattr(acc, "business_domain", BusinessDomain.SHARED.value),
            filter_domain=filter_domain,
        )
    ]


def account_domain_for_code(code: str) -> str:
    c = (code or "").strip()
    if c in _HOTEL_ACCOUNT_CODES:
        return BusinessDomain.HOTEL.value
    if c in _RESTAURANT_CUSTODY_CODES:
        return BusinessDomain.RESTAURANT.value
    if c in _SHARED_ACCOUNT_CODES:
        return BusinessDomain.SHARED.value
    return BusinessDomain.RESTAURANT.value


def purchase_entry_domain(purchase) -> str:
    dom = getattr(purchase, "business_domain", None)
    if dom is None:
        return BusinessDomain.RESTAURANT.value
    return dom.value if hasattr(dom, "value") else str(dom).strip().lower()


def pm_entry_domain(db, payment_method_id: int) -> str:
    from modules.payments.models import PaymentMethod

    pm = db.get(PaymentMethod, int(payment_method_id))
    if pm is None:
        return BusinessDomain.RESTAURANT.value
    dom = getattr(pm, "business_domain", None)
    if dom is None:
        return BusinessDomain.SHARED.value
    return dom.value if hasattr(dom, "value") else str(dom).strip().lower()


def transfer_entry_domain(db, transfer) -> str:
    a = pm_entry_domain(db, int(transfer.from_payment_method_id))
    b = pm_entry_domain(db, int(transfer.to_payment_method_id))
    if a == b:
        return a
    return BusinessDomain.SHARED.value


def infer_entry_domain(
    db,
    *,
    source_type: str | None,
    source_id: int | None,
    business_domain: str | None = None,
) -> str:
    raw = (business_domain or "").strip().lower()
    if raw in ("restaurant", "hotel", "shared"):
        return raw
    st = (source_type or "").strip().lower()
    if st.startswith("hotel_"):
        return BusinessDomain.HOTEL.value
    if st.startswith("sale") or st in ("refund_payment",):
        return BusinessDomain.RESTAURANT.value
    if st.startswith("payroll") and source_id:
        from modules.hr.models import PayrollRun

        run = db.get(PayrollRun, int(source_id))
        if run is not None:
            dom = (getattr(run, "business_domain", None) or "").strip().lower()
            if dom in ("restaurant", "hotel"):
                return dom
        return BusinessDomain.RESTAURANT.value
    if st.startswith("payroll"):
        return BusinessDomain.RESTAURANT.value
    if st in ("purchase_expense", "purchase_inventory", "purchase_asset", "salary_advance"):
        if source_id:
            from modules.payments.models import Purchase

            purchase = db.get(Purchase, int(source_id))
            if purchase is not None:
                return purchase_entry_domain(purchase)
        return BusinessDomain.RESTAURANT.value
    if st == "purchase_payment" and source_id:
        from modules.payments.models import Purchase, PurchasePayment

        pp = db.get(PurchasePayment, int(source_id))
        if pp is not None and pp.purchase_id:
            purchase = db.get(Purchase, int(pp.purchase_id))
            if purchase is not None:
                return purchase_entry_domain(purchase)
    if st == "payment_transfer" and source_id:
        from modules.payments.models import PaymentTransfer

        tf = db.get(PaymentTransfer, int(source_id))
        if tf is not None:
            return transfer_entry_domain(db, tf)
        return BusinessDomain.SHARED.value
    return BusinessDomain.RESTAURANT.value


def ensure_gl_account_domains(db) -> None:
    """ترقية: تعيين business_domain لحسابات GL حسب الرمز."""
    from sqlalchemy import select

    from modules.gl.models import GlAccount

    for acc in db.scalars(select(GlAccount)).all():
        expected = account_domain_for_code(acc.code)
        current = (getattr(acc, "business_domain", None) or "").strip().lower()
        if not current or current != expected:
            acc.business_domain = expected
    db.flush()
