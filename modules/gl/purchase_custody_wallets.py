"""محافظ عهدة المشتريات — مطعم / فندق (كاش + مصرف)."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.gl.models import GlAccount, GlPaymentMethodMap
from modules.payments.models import (
    HOTEL_PURCHASE_CUSTODY_BANK_PM_NAME,
    HOTEL_PURCHASE_CUSTODY_CASH_PM_NAME,
    LEGACY_HOTEL_PURCHASE_CUSTODY_PM_NAME,
    LEGACY_RESTAURANT_PURCHASE_CUSTODY_PM_NAME,
    PaymentMethod,
    PaymentMethodDomain,
    PaymentMethodKind,
    RESTAURANT_PURCHASE_CUSTODY_BANK_PM_NAME,
    RESTAURANT_PURCHASE_CUSTODY_CASH_PM_NAME,
)

# (name_ar, gl_code, domain, kind, sort_order)
_CUSTODY_WALLETS: tuple[tuple[str, str, PaymentMethodDomain, PaymentMethodKind, int], ...] = (
    (
        RESTAURANT_PURCHASE_CUSTODY_CASH_PM_NAME,
        "1130",
        PaymentMethodDomain.RESTAURANT,
        PaymentMethodKind.CASH,
        130,
    ),
    (
        RESTAURANT_PURCHASE_CUSTODY_BANK_PM_NAME,
        "1132",
        PaymentMethodDomain.RESTAURANT,
        PaymentMethodKind.BANK,
        132,
    ),
    (
        HOTEL_PURCHASE_CUSTODY_CASH_PM_NAME,
        "1135",
        PaymentMethodDomain.HOTEL,
        PaymentMethodKind.CASH,
        135,
    ),
    (
        HOTEL_PURCHASE_CUSTODY_BANK_PM_NAME,
        "1136",
        PaymentMethodDomain.HOTEL,
        PaymentMethodKind.BANK,
        137,
    ),
)

_LEGACY_CUSTODY_RENAMES: tuple[tuple[str, str], ...] = (
    (LEGACY_RESTAURANT_PURCHASE_CUSTODY_PM_NAME, RESTAURANT_PURCHASE_CUSTODY_CASH_PM_NAME),
    (LEGACY_HOTEL_PURCHASE_CUSTODY_PM_NAME, HOTEL_PURCHASE_CUSTODY_CASH_PM_NAME),
)


def _migrate_legacy_custody_names(db: Session) -> None:
    for legacy_name, cash_name in _LEGACY_CUSTODY_RENAMES:
        legacy = db.scalar(
            select(PaymentMethod).where(PaymentMethod.name_ar == legacy_name)
        )
        if legacy is None:
            continue
        existing_cash = db.scalar(
            select(PaymentMethod).where(PaymentMethod.name_ar == cash_name)
        )
        if existing_cash is None:
            legacy.name_ar = cash_name
            legacy.kind = PaymentMethodKind.CASH
        elif existing_cash.id != legacy.id:
            legacy.is_active = False
    db.flush()


def ensure_purchase_custody_wallets(db: Session) -> None:
    _migrate_legacy_custody_names(db)
    code_to_id = {row.code: row.id for row in db.scalars(select(GlAccount)).all()}
    for pm_name, gl_code, domain, kind, sort_order in _CUSTODY_WALLETS:
        gl_id = code_to_id.get(gl_code)
        pm = db.scalar(select(PaymentMethod).where(PaymentMethod.name_ar == pm_name))
        if pm is None:
            pm = PaymentMethod(
                name_ar=pm_name,
                kind=kind,
                is_active=True,
                can_receive=False,
                can_pay=True,
                can_fund=True,
                is_system=True,
                show_on_dashboard=True,
                sort_order=sort_order,
                business_domain=domain,
            )
            db.add(pm)
            db.flush()
        else:
            pm.kind = kind
            pm.can_receive = False
            pm.can_pay = True
            pm.can_fund = True
            pm.is_system = True
            pm.is_active = True
            pm.show_on_dashboard = True
            pm.business_domain = domain
            pm.sort_order = sort_order
        if gl_id is None:
            continue
        existing = db.scalar(
            select(GlPaymentMethodMap).where(
                GlPaymentMethodMap.payment_method_id == pm.id
            )
        )
        if existing is None:
            db.add(
                GlPaymentMethodMap(
                    payment_method_id=pm.id,
                    gl_account_id=int(gl_id),
                )
            )
        else:
            existing.gl_account_id = int(gl_id)
    db.flush()
